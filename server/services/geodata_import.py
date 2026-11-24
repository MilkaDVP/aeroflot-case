"""
Граф аэропорта из геоданных GeoJSON (формат испытательного полигона).

Вход — четыре слоя, как их выдают организаторы (QGIS, WGS 84):

- roads.geojson — линии дорог: type (runway, taxi, service, pedestrian)
  и spd_kmh — скоростной режим участка;
- service_zones.geojson — зоны обслуживания ВС (места стоянки);
- objects.geojson — объекты: терминал, ангар, ВПП, стоянка спецтехники,
  склады;
- airport_boundaries.geojson — контур территории.

Выход — JSON графа в формате data/airports/*.json, который понимает
весь остальной код: вершины, рёбра с допуском агентов и скоростью,
плюс объекты, зоны и контур для карты.

Правила допуска (из атрибутов слоя дорог):
- service — спецтранспорт и инженер пешком, скорость по spd_kmh;
- pedestrian — только пешком;
- taxi, runway — только воздушные суда: в маршруты агентов не входят,
  на карте рисуются.

Линии в геоданных стыкуются не всегда идеально: конец служебной дороги
может не доходить до соседней линии на метр-два. Такие концы
«пришиваются» к ближайшей линии того же класса (дороги агентов или
дороги ВС) в пределах SNAP_M — иначе граф развалился бы на куски, и
маршрут «не находился» бы там, где дорога на карте явно есть.
"""

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

EARTH_RADIUS_M = 6371008.8
SNAP_M = 3.0

AGENT_KINDS = ("service", "pedestrian")
AIRCRAFT_KINDS = ("taxi", "runway")

# Как объект называется в геоданных → вид вершины графа.
OBJECT_KINDS = (
    ("склад", "warehouse"),
    ("стоянка спецтех", "vehicle_parking"),
    ("ангар", "tech_center"),
    ("терминал", "terminal"),
    ("взлётно", "runway"),
    ("взлетно", "runway"),
)
# Объекты, к которым инженер ходит: у них есть вершина входа в граф.
ROUTED_OBJECTS = ("warehouse", "vehicle_parking", "tech_center")
# Подъезд к объекту и к стоянке ВС — манёвр, 5 км/ч.
ACCESS_SPEED_KMH = 5.0


def haversine_m(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin(math.radians(lat2 - lat1) / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def project_xy(lat, lon, ref_lat, ref_lon):
    """Метры от опорной точки: X — на восток, Y — на юг (как на экране)."""
    x = math.radians(lon - ref_lon) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = math.radians(ref_lat - lat) * EARTH_RADIUS_M
    return round(x, 2), round(y, 2)


def read_layer(directory, name):
    path = Path(directory) / f"{name}.geojson"
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)["features"]


def read_points(directory, name):
    """Точечный слой, если он есть: [(широта, долгота)]."""
    if not (Path(directory) / f"{name}.geojson").exists():
        return []
    return [(f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0]) for f in read_layer(directory, name)]


def polygons_of(geometry):
    """Кольца полигона в [[lat, lon], ...]; MultiPolygon разворачивается."""
    coordinates = geometry["coordinates"]
    if geometry["type"] == "Polygon":
        coordinates = [coordinates]
    rings = []
    for polygon in coordinates:
        # Внешнее кольцо; отверстия для карты и попадания в зону не нужны.
        rings.append([[point[1], point[0]] for point in polygon[0]])
    return rings


def centroid(ring):
    """Центр тяжести кольца [[lat, lon]] (плоское приближение)."""
    # Считаем от первой точки: в полных градусах площадь зоны в 15 м тонула
    # в погрешности, и «центр» оказывался вне зоны.
    y0, x0 = ring[0]
    local = [(y - y0, x - x0) for y, x in ring]
    area = cx = cy = 0.0
    for (y1, x1), (y2, x2) in zip(local, local[1:] + local[:1]):
        cross = x1 * y2 - x2 * y1
        area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(area) < 1e-20:
        lats = [p[0] for p in ring]
        lons = [p[1] for p in ring]
        return sum(lats) / len(lats), sum(lons) / len(lons)
    area *= 0.5
    return y0 + cy / (6 * area), x0 + cx / (6 * area)


def point_in_ring(lat, lon, ring):
    inside = False
    for (lat1, lon1), (lat2, lon2) in zip(ring, ring[1:] + ring[:1]):
        if (lat1 > lat) != (lat2 > lat):
            if lon < lon1 + (lat - lat1) * (lon2 - lon1) / (lat2 - lat1):
                inside = not inside
    return inside


class Local:
    """Плоская проекция в метрах для геометрии привязки (небольшая территория)."""

    def __init__(self, lat, lon):
        self.lat0, self.lon0 = lat, lon
        self.kx = math.radians(1) * EARTH_RADIUS_M * math.cos(math.radians(lat))
        self.ky = math.radians(1) * EARTH_RADIUS_M

    def to_xy(self, lat, lon):
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky

    def to_ll(self, x, y):
        return self.lat0 + y / self.ky, self.lon0 + x / self.kx


def project_on_segment(p, a, b):
    """Ближайшая к p точка отрезка ab, параметр t и расстояние."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    q = (ax + t * dx, ay + t * dy)
    return q, t, math.hypot(p[0] - q[0], p[1] - q[1])


# --- Дороги -----------------------------------------------------------------

def read_roads(directory):
    lines = []
    for feature in read_layer(directory, "roads"):
        props = feature["properties"]
        geometry = feature["geometry"]
        parts = [geometry["coordinates"]] if geometry["type"] == "LineString" else geometry["coordinates"]
        for part in parts:
            points = [(round(p[1], 8), round(p[0], 8)) for p in part]
            if len(points) >= 2:
                lines.append({
                    "kind": props.get("type") or "service",
                    "speed_kmh": float(props["spd_kmh"]) if props.get("spd_kmh") else None,
                    "points": points,
                })
    return lines


def snap_dangling(lines, local):
    """
    Пришивает висящие концы линий к ближайшей линии того же класса.

    Конец, совпадающий с вершиной другой линии, уже соединён. Иначе
    ищется ближайшая точка на чужих отрезках в пределах SNAP_M: если это
    вершина — конец переносится в неё, если середина отрезка — в отрезок
    вставляется новая вершина, и конец переносится туда.
    """
    def group(kind):
        return "agent" if kind in AGENT_KINDS else "aircraft"

    usage = defaultdict(int)
    for line in lines:
        for point in set(line["points"]):
            usage[point] += 1

    snapped = 0
    for index, line in enumerate(lines):
        for end in (0, -1):
            point = line["points"][end]
            if usage[point] > 1:
                continue
            target = local.to_xy(*point)
            best = None
            for other_index, other in enumerate(lines):
                if other_index == index or group(other["kind"]) != group(line["kind"]):
                    continue
                xy = [local.to_xy(*p) for p in other["points"]]
                for seg in range(len(xy) - 1):
                    q, t, distance = project_on_segment(target, xy[seg], xy[seg + 1])
                    if distance <= SNAP_M and (best is None or distance < best[0]):
                        best = (distance, other_index, seg, t, q)
            if best is None:
                continue
            _, other_index, seg, t, q = best
            other = lines[other_index]["points"]
            if t <= 1e-6:
                joint = other[seg]
            elif t >= 1 - 1e-6:
                joint = other[seg + 1]
            else:
                lat, lon = local.to_ll(*q)
                joint = (round(lat, 8), round(lon, 8))
                other.insert(seg + 1, joint)
            usage[point] -= 1
            line["points"][end] = joint
            usage[joint] += 1
            snapped += 1
    return snapped


def build_network(lines):
    """
    Вершины — концы линий и общие точки; между ними — рёбра с изгибами.
    """
    usage = defaultdict(int)
    for line in lines:
        for point in set(line["points"]):
            usage[point] += 1
    vertex_ids = {}

    def vertex(point):
        if point not in vertex_ids:
            vertex_ids[point] = f"v{len(vertex_ids) + 1}"
        return vertex_ids[point]

    edges = []
    for line in lines:
        points = line["points"]
        start = 0
        for i in range(1, len(points)):
            if i == len(points) - 1 or usage[points[i]] > 1:
                chunk = points[start:i + 1]
                length = sum(haversine_m(*a, *b) for a, b in zip(chunk, chunk[1:]))
                if length > 0.01:
                    edges.append(edge_of(vertex(chunk[0]), vertex(chunk[-1]), length, line["kind"],
                                         line["speed_kmh"], chunk[1:-1]))
                start = i
    nodes = [
        {"id": node_id, "type": "junction", "ref": None, "lat": lat, "lon": lon}
        for (lat, lon), node_id in vertex_ids.items()
    ]
    return nodes, edges


def edge_of(from_id, to_id, length, kind, speed_kmh, points=()):
    return {
        "from_id": from_id,
        "to_id": to_id,
        "distance_m": round(length, 2),
        "kind": kind,
        "speed_kmh": speed_kmh,
        "vehicle_allowed": kind == "service",
        "walk_allowed": kind in AGENT_KINDS,
        "points": [list(p) for p in points],
    }


def accessible(nodes, edges, flag):
    """Перекрёстки дорог, доступных агенту: к ним подводятся подъезды объектов."""
    ids = set()
    for edge in edges:
        if edge[flag]:
            ids.add(edge["from_id"])
            ids.add(edge["to_id"])
    return [node for node in nodes if node["id"] in ids and node["type"] == "junction"]


def nearest(candidates, lat, lon):
    best = None
    for node in candidates:
        distance = haversine_m(lat, lon, node["lat"], node["lon"])
        if best is None or distance < best[0]:
            best = (distance, node)
    return best


def attach(nodes, edges, node, walk_only=False):
    """
    Подводит к вершине объекта подъезд от ближайшей дороги агентов.

    Подъезд — участок 5 км/ч (манёвр у объекта). Если ближайшая дорога
    пешеходная, а машине тоже нужно подъехать (склад, стоянка ВС), второй
    подъезд — от ближайшей служебной дороги.
    """
    walkable = accessible(nodes, edges, "walk_allowed")
    drivable = accessible(nodes, edges, "vehicle_allowed")
    found = nearest(walkable, node["lat"], node["lon"])
    if found:
        distance, target = found
        kind = "service" if target in drivable and not walk_only else "pedestrian"
        edges.append(edge_of(node["id"], target["id"], max(distance, 0.5), kind, ACCESS_SPEED_KMH))
    if walk_only:
        return
    found_drive = nearest(drivable, node["lat"], node["lon"])
    if found_drive and (not found or found_drive[1] is not found[1]):
        distance, target = found_drive
        edges.append(edge_of(node["id"], target["id"], max(distance, 0.5), "service", ACCESS_SPEED_KMH))


# --- Сборка ------------------------------------------------------------------

def object_kind(name):
    lowered = (name or "").lower()
    for needle, kind in OBJECT_KINDS:
        if needle in lowered:
            return kind
    return "object"


def build_payload(directory, icao, name, city):
    lines = read_roads(directory)
    first = lines[0]["points"][0]
    local = Local(*first)
    snapped = snap_dangling(lines, local)
    nodes, edges = build_network(lines)

    # Зоны обслуживания ВС — стоянки. Вершина стоянки — место борта в зоне,
    # если оно задано, иначе центр зоны; подъезд — от ближайшей дороги агентов.
    zones = []
    boards = read_points(directory, "aircrafts_positions")
    for feature in read_layer(directory, "service_zones"):
        number = str(feature["properties"].get("id"))
        ref = feature["properties"].get("name") or number
        rings = polygons_of(feature["geometry"])
        lat, lon = centroid(rings[0])
        if not point_in_ring(lat, lon, rings[0]):
            lat, lon = rings[0][0]
        lat, lon = next((p for p in boards if point_in_ring(*p, rings[0])), (lat, lon))
        node = {"id": f"stand_{number}", "type": "stand", "ref": ref, "lat": round(lat, 8), "lon": round(lon, 8)}
        nodes.append(node)
        attach(nodes, edges, node)
        zones.append({"ref": ref, "node_id": node["id"], "rings_ll": rings})

    objects = []
    for feature in read_layer(directory, "objects"):
        title = feature["properties"].get("name") or "Объект"
        kind = object_kind(title)
        rings = polygons_of(feature["geometry"])
        item = {"name": title, "kind": kind, "rings_ll": rings, "node_id": None}
        if kind in ROUTED_OBJECTS:
            item["node_id"] = entrance(nodes, edges, rings[0], kind, title)
        lat, lon = centroid(rings[0])
        item["label_ll"] = [lat, lon]
        objects.append(item)

    boundary = []
    for feature in read_layer(directory, "airport_boundaries"):
        boundary.extend(polygons_of(feature["geometry"]))

    nodes, edges = keep_reachable(nodes, edges)

    lats = [n["lat"] for n in nodes] + [p[0] for ring in boundary for p in ring]
    lons = [n["lon"] for n in nodes] + [p[1] for ring in boundary for p in ring]
    ref_point = {"lat": round((min(lats) + max(lats)) / 2, 7), "lon": round((min(lons) + max(lons)) / 2, 7)}

    def xy(lat, lon):
        return list(project_xy(lat, lon, ref_point["lat"], ref_point["lon"]))

    for node in nodes:
        node["x"], node["y"] = xy(node["lat"], node["lon"])
    for edge in edges:
        edge["points"] = [xy(lat, lon) for lat, lon in edge["points"]]
    for zone in zones:
        zone["rings"] = [[xy(*p) for p in ring] for ring in zone["rings_ll"]]
    for item in objects:
        item["rings"] = [[xy(*p) for p in ring] for ring in item["rings_ll"]]
        item["label"] = xy(*item["label_ll"])
    boundary_xy = [[xy(*p) for p in ring] for ring in boundary]

    return {
        "icao": icao,
        "name": name,
        "city": city,
        "ref_point": ref_point,
        "bbox": [min(lats), min(lons), max(lats), max(lons)],
        "source": "Геоданные испытательного полигона (GeoJSON, WGS 84)",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": {"snapped_ends": snapped},
        "nodes": nodes,
        "edges": edges,
        "zones": zones,
        "objects": objects,
        "boundary": boundary_xy,
        "boundary_ll": boundary,
    }


def entrance(nodes, edges, ring, kind, title):
    """
    Вершина объекта — вход: точка контура, ближайшая к дорогам агентов.
    Внутрь здания маршрут не ведёт.
    """
    walkable = accessible(nodes, edges, "walk_allowed")
    best = None
    for corner in ring:
        found = nearest(walkable, *corner)
        if found and (best is None or found[0] < best[0]):
            best = (found[0], found[1])
    if best is None:
        return None
    road = best[1]
    local = Local(road["lat"], road["lon"])
    target = local.to_xy(road["lat"], road["lon"])
    door = None
    xy = [local.to_xy(*p) for p in ring]
    for a, b in zip(xy, xy[1:] + xy[:1]):
        q, _, distance = project_on_segment(target, a, b)
        if door is None or distance < door[0]:
            door = (distance, q)
    lat, lon = local.to_ll(*door[1])
    node_id = f"{kind}_{len([n for n in nodes if n['type'] == kind]) + 1}"
    node = {"id": node_id, "type": kind, "ref": title, "lat": round(lat, 8), "lon": round(lon, 8)}
    nodes.append(node)
    attach(nodes, edges, node, walk_only=kind == "tech_center")
    return node_id


def keep_reachable(nodes, edges):
    """
    Оставляет дороги ВС целиком (их только рисуем), а из сети агентов —
    наибольшую связную часть: обрывок, куда не дойти, дал бы «пути нет».
    """
    neighbours = defaultdict(set)
    for edge in edges:
        if edge["walk_allowed"]:
            neighbours[edge["from_id"]].add(edge["to_id"])
            neighbours[edge["to_id"]].add(edge["from_id"])
    seen, best = set(), set()
    for start in list(neighbours):
        if start in seen:
            continue
        component, queue = set(), [start]
        seen.add(start)
        while queue:
            current = queue.pop()
            component.add(current)
            for nxt in neighbours[current]:
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        if len(component) > len(best):
            best = component
    aircraft_ids = set()
    for edge in edges:
        if not edge["walk_allowed"]:
            aircraft_ids.add(edge["from_id"])
            aircraft_ids.add(edge["to_id"])
    kept_edges = [
        edge for edge in edges
        if not edge["walk_allowed"] or (edge["from_id"] in best and edge["to_id"] in best)
    ]
    used = {edge["from_id"] for edge in kept_edges} | {edge["to_id"] for edge in kept_edges}
    kept_nodes = [node for node in nodes if node["id"] in used]
    return kept_nodes, kept_edges
