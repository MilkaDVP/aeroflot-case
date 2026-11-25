"""
Граф аэропорта: загрузка подготовленного JSON и доступ к вершинам.

Граф неориентированный: по рулёжке ходят и ездят в обе стороны.

У ребра есть допуск по агентам и скоростной режим:
- vehicle_allowed — проезд спецтранспорта;
- walk_allowed — проход пешком (по умолчанию да). Рулёжные дорожки и ВПП
  на испытательном полигоне — только для воздушных судов: оба флага
  ложны, и маршрут через них не строится вовсе;
- speed_kmh — ограничение скорости на участке (по умолчанию нет).
  Время в пути по ребру считается по меньшей из скоростей — агента
  и ограничения, поэтому маршрут выбирается самый быстрый, а не самый
  короткий.
- kind — вид дороги для карты: service, pedestrian, taxi, runway.

Помимо графа, полигон может нести объекты (склады, стоянка спецтехники,
ангар, терминал), зоны обслуживания ВС и контур территории — для карты
и для фиксации прибытия по попаданию в зону стоянки.
"""

import copy
import json
import math
from collections import defaultdict

from constants import AIRPORTS_DIR, MODE_VEHICLE
from algorithm.geo import haversine_m


EARTH_M = 6371008.8
# Точка посреди дороги: «~вершина~вершина~длина ребра, см~от первой, см».
# Идентификатор сам себя описывает: сохранённый в вызове маршрут читается
# и после перезапуска, а граф аэропорта не меняется.
ROAD_POINT = "~"
# Ближе этого к концу ребра — это сама вершина.
ROAD_END_M = 1


class AirportGraph:
    """
    Граф одного аэропорта, готовый к поиску маршрута.

    Хранит вершины по идентификатору и списки смежности. Списки строятся
    один раз при загрузке: подбор кандидатов вызывает поиск пути десятки
    раз подряд, пересобирать смежность на каждый вызов недопустимо
    при ограничении в 10 секунд.
    """

    def __init__(self, payload):
        self.icao = payload["icao"]
        self.name = payload["name"]
        self.city = payload["city"]
        self.ref_point = payload["ref_point"]
        self.nodes = {node["id"]: node for node in payload["nodes"]}
        self.edges = payload["edges"]
        self._adjacency = build_adjacency(payload["edges"])
        self._bends = build_bends(payload["edges"], self.ref_point)
        self._roads = {(e["from_id"], e["to_id"], round(e["distance_m"] * 100)): e for e in payload["edges"]}
        self._points = {}
        self._anchors = {}
        self._snapped = {}
        self.objects = payload.get("objects", [])
        self.zones = payload.get("zones", [])
        self.boundary = payload.get("boundary", [])
        self.centre = self.operational_centre()

    def operational_centre(self):
        """
        Центр рабочей зоны: середина охвата стоянок и техцентров.

        Не путать с опорной точкой проекции ref_point — та считается
        по всем узлам графа. В Шереметьево рулёжка к третьей полосе уходит
        на километры западнее перронов, и центр всех данных оказывается
        у забора аэродрома. Показывать там карту человеку без координат
        бессмысленно: ему нужен перрон.
        """
        working = [
            node for node in self.nodes.values() if node["type"] in ("stand", "tech_center")
        ]
        if not working:
            return self.ref_point

        lats = [node["lat"] for node in working]
        lons = [node["lon"] for node in working]
        return {
            "lat": round((min(lats) + max(lats)) / 2, 7),
            "lon": round((min(lons) + max(lons)) / 2, 7),
        }

    def node(self, node_id):
        """Вершина по идентификатору или None, если такой нет."""
        found = self.nodes.get(node_id)
        if found is None and isinstance(node_id, str) and node_id.startswith(ROAD_POINT):
            point = self.road_point(node_id)
            return point["node"] if point else None
        return found

    # --- Точки посреди дорог ---

    def _xy(self, lat, lon):
        ref = self.ref_point
        k = math.radians(1) * EARTH_M
        return (lon - ref["lon"]) * k * math.cos(math.radians(ref["lat"])), (ref["lat"] - lat) * k

    def _ll(self, x, y):
        ref = self.ref_point
        k = math.radians(1) * EARTH_M
        return ref["lat"] - y / k, ref["lon"] + x / (k * math.cos(math.radians(ref["lat"])))

    def _line(self, edge):
        """Ломаная ребра в метрах вместе с изгибами."""
        a, b = self.nodes.get(edge["from_id"]), self.nodes.get(edge["to_id"])
        if a is None or b is None or a is b:
            return None
        return [self._xy(a["lat"], a["lon"]), *(tuple(p) for p in edge.get("points") or []),
                self._xy(b["lat"], b["lon"])]

    def road_point(self, node_id):
        """Разбор идентификатора точки посреди дороги; None — такой дороги нет."""
        if node_id in self._points:
            return self._points[node_id]
        parts = node_id.split(ROAD_POINT)
        try:
            _, start, end, length, offset = parts
            edge, offset = self._roads.get((start, end, int(length))), int(offset) / 100
        except ValueError:
            return None
        line = self._line(edge) if edge else None
        if line is None or not 0 <= offset <= edge["distance_m"]:
            return None
        spans = [math.dist(a, b) for a, b in zip(line, line[1:])]
        left = offset / edge["distance_m"] * sum(spans)
        index = 0
        while index < len(spans) - 1 and left > spans[index]:
            left -= spans[index]
            index += 1
        k = min(1, left / spans[index]) if spans[index] else 0
        (ax, ay), (bx, by) = line[index], line[index + 1]
        x, y = ax + (bx - ax) * k, ay + (by - ay) * k
        lat, lon = self._ll(x, y)
        point = {
            "edge": edge, "offset": offset, "line": line, "index": index,
            "node": {"id": node_id, "type": "junction", "ref": None, "name": None,
                     "lat": lat, "lon": lon, "x": x, "y": y},
        }
        if len(self._points) > 2000:
            self._points.clear()
        self._points[node_id] = point
        return point

    def nearest_road(self, lat, lon, mode, also=None):
        """
        Ближайшее к точке место дороги: (вершина, расстояние).

        Не только перекрёстки: машина посреди проезда стоит на нём, а не
        у его конца. also — способ, которым место тоже должно быть доступно.
        """
        key = (lat, lon, mode, also)
        if key not in self._snapped:
            if len(self._snapped) > 2000:
                self._snapped.clear()
            self._snapped[key] = self._find_road(lat, lon, mode, also)
        return self._snapped[key]

    def _find_road(self, lat, lon, mode, also):
        node, node_m = self.nearest_node(lat, lon, mode)
        flags = {m: ("vehicle_allowed", False) if m == MODE_VEHICLE else ("walk_allowed", True)
                 for m in (mode, also) if m}
        px, py = self._xy(lat, lon)
        best = None
        for edge in self.edges:
            if not all(edge.get(name, default) for name, default in flags.values()):
                continue
            line = self._line(edge)
            if line is None or edge["distance_m"] <= 2 * ROAD_END_M:
                continue
            passed = 0.0
            for (ax, ay), (bx, by) in zip(line, line[1:]):
                dx, dy = bx - ax, by - ay
                span = math.hypot(dx, dy)
                t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / span ** 2)) if span else 0
                distance = math.hypot(px - ax - t * dx, py - ay - t * dy)
                if best is None or distance < best[0]:
                    best = (distance, edge, passed + t * span, line)
                passed += span
        if best is None or (node is not None and best[0] >= node_m - 0.5):
            return node, node_m
        distance, edge, along, line = best
        offset = along / (sum(math.dist(a, b) for a, b in zip(line, line[1:])) or 1) * edge["distance_m"]
        for end, far in ((edge["from_id"], offset), (edge["to_id"], edge["distance_m"] - offset)):
            if far < ROAD_END_M:
                found = self.nodes[end]
                return found, haversine_m(lat, lon, found["lat"], found["lon"])
        point = self.node(ROAD_POINT.join(
            ["", edge["from_id"], edge["to_id"], str(round(edge["distance_m"] * 100)), str(round(offset * 100))]))
        return (point, distance) if point else (node, node_m)

    def with_points(self, node_ids):
        """Тот же граф, в котором до этих точек посреди дорог можно доехать."""
        extra = {n for n in node_ids if isinstance(n, str) and n not in self.nodes and self.node(n)}
        if extra <= {p for points in self._anchors.values() for p in points}:
            return self
        graph = copy.copy(self)
        graph._anchors = {key: list(value) for key, value in self._anchors.items()}
        for node_id in extra:
            edge = self.road_point(node_id)["edge"]
            for end in (edge["from_id"], edge["to_id"]):
                points = graph._anchors.setdefault(end, [])
                if node_id not in points:
                    points.append(node_id)
        return graph

    def _road_links(self, node_id):
        """Связи точки посреди дороги: концы ребра и другие точки на нём."""
        point = self.road_point(node_id)
        if point is None:
            return []
        edge, offset = point["edge"], point["offset"]
        vehicle, walk = bool(edge["vehicle_allowed"]), bool(edge.get("walk_allowed", True))
        limit = edge.get("speed_kmh")
        found = [(edge["from_id"], vehicle, walk, offset, limit),
                 (edge["to_id"], vehicle, walk, edge["distance_m"] - offset, limit)]
        for other in self._anchors.get(edge["from_id"], []):
            twin = self.road_point(other)
            if other != node_id and twin["edge"] is edge:
                found.append((other, vehicle, walk, abs(twin["offset"] - offset), limit))
        return found

    def _all_links(self, node_id):
        if node_id not in self.nodes:
            return self._road_links(node_id) if isinstance(node_id, str) and node_id.startswith(ROAD_POINT) else []
        found = self._adjacency[node_id]
        points = self._anchors.get(node_id)
        if points:
            found = found + [
                (key, *link[1:]) for key in points for link in self._road_links(key) if link[0] == node_id
            ]
        return found

    def links(self, node_id, mode):
        """
        Рёбра от вершины, разрешённые выбранному способу передвижения.

        Возвращает тройки (сосед, длина в метрах, ограничение скорости
        в км/ч или None).
        """
        index = 1 if mode == MODE_VEHICLE else 2
        return [
            (link[0], link[3], link[4]) for link in self._all_links(node_id) if link[index]
        ]

    def bends(self, start_id, end_id):
        """
        Изгибы дороги между соседними вершинами — точки (широта, долгота).

        Без них путь по дуге выглядел бы прямой через здания, а идущий
        по дороге инженер казался бы ушедшим с маршрута.
        """
        for here, there, forward in ((start_id, end_id, True), (end_id, start_id, False)):
            if isinstance(here, str) and here.startswith(ROAD_POINT) and here not in self.nodes:
                line = self._part(here, there)
                return line if forward else line[::-1]
        return self._bends.get((start_id, end_id), [])

    def _part(self, point_id, other_id):
        """Изгибы от точки посреди дороги до конца ребра или другой точки на нём."""
        point = self.road_point(point_id)
        if point is None:
            return []
        edge, line, index = point["edge"], point["line"], point["index"]
        if other_id == edge["from_id"]:
            part = line[1:index + 1][::-1]
        elif other_id == edge["to_id"]:
            part = line[index + 1:-1]
        else:
            twin = self.road_point(other_id) if isinstance(other_id, str) else None
            if twin is None or twin["edge"] is not edge:
                return []
            part = (line[index + 1:twin["index"] + 1] if twin["offset"] >= point["offset"]
                    else line[twin["index"] + 1:index + 1][::-1])
        return [self._ll(x, y) for x, y in part]

    def neighbours(self, node_id, mode):
        """
        Соседи вершины, достижимые выбранным способом передвижения.

        Возвращает пары (идентификатор соседа, длина ребра в метрах).
        """
        return [(neighbour, length) for neighbour, length, _ in self.links(node_id, mode)]

    def zone_at(self, lat, lon):
        """Зона обслуживания ВС, в которой лежит точка, или None."""
        for zone in self.zones:
            if any(point_in_ring(lat, lon, ring) for ring in zone.get("rings_ll", [])):
                return zone
        return None

    def nodes_of_type(self, node_type):
        """Все вершины заданного типа: stand, tech_center или junction."""
        return [node for node in self.nodes.values() if node["type"] == node_type]

    def find_stand(self, ref):
        """Стоянка по её номеру на перроне, например «31» или «102A»; «9» — это и «мс-09»."""
        stands = self.nodes_of_type("stand")
        return next((node for node in stands if node["ref"] == ref), None) or next(
            (node for node in stands if (node["ref"] or "").startswith("мс-")
             and node["ref"][3:].lstrip("0") == str(ref).lstrip("0")), None)

    def nearest_node(self, lat, lon, mode):
        """
        Ближайшая к точке вершина графа, доступная выбранным способом.

        Нужна, чтобы посадить GPS-координаты сотрудника на граф: телефон
        отдаёт точку посреди перрона, а Дейкстра работает по вершинам.
        Вершины без единого подходящего ребра пропускаются — иначе
        спецтранспорт мог бы «стартовать» с пешеходного тупика.
        """
        best_node = None
        best_distance = None

        for node in self.nodes.values():
            if not self.neighbours(node["id"], mode):
                continue
            distance = haversine_m(lat, lon, node["lat"], node["lon"])
            if best_distance is None or distance < best_distance:
                best_node = node
                best_distance = distance

        return best_node, best_distance


def build_adjacency(edges):
    """
    Списки смежности из плоского списка рёбер.

    Каждое ребро добавляется в обе стороны: граф неориентированный.
    Элемент списка: (сосед, проезд машины, проход пешком, длина, скорость).
    Ребро, закрытое для всех агентов (рулёжка, ВПП), в смежность не входит.
    """
    adjacency = defaultdict(list)
    for edge in edges:
        vehicle = bool(edge["vehicle_allowed"])
        walk = bool(edge.get("walk_allowed", True))
        if not (vehicle or walk):
            continue
        distance = edge["distance_m"]
        limit = edge.get("speed_kmh")
        adjacency[edge["from_id"]].append((edge["to_id"], vehicle, walk, distance, limit))
        adjacency[edge["to_id"]].append((edge["from_id"], vehicle, walk, distance, limit))
    return adjacency


def build_bends(edges, ref_point):
    """
    (вершина, вершина) → изгибы в порядке движения.

    Между двумя вершинами бывает несколько дорог: берётся самая быстрая —
    по ней же строится маршрут.
    """
    earth_m = 6371008.8
    lat0, lon0 = ref_point["lat"], ref_point["lon"]
    best = {}
    for edge in edges:
        if not (edge["vehicle_allowed"] or edge.get("walk_allowed", True)):
            continue
        cost = edge["distance_m"] / min(20, edge.get("speed_kmh") or 20)
        key = (edge["from_id"], edge["to_id"])
        if key in best and best[key][0] <= cost:
            continue
        best[key] = best[key[::-1]] = (cost, edge)
    result = {}
    for _, edge in best.values():
        line = [
            (lat0 - math.degrees(y / earth_m),
             lon0 + math.degrees(x / (earth_m * math.cos(math.radians(lat0)))))
            for x, y in edge.get("points") or []
        ]
        if line:
            result[(edge["from_id"], edge["to_id"])] = line
            result[(edge["to_id"], edge["from_id"])] = line[::-1]
    return result


def point_in_ring(lat, lon, ring):
    """Лежит ли точка внутри кольца [[lat, lon], ...] — луч по долготе."""
    inside = False
    count = len(ring)
    for i in range(count):
        lat1, lon1 = ring[i]
        lat2, lon2 = ring[(i + 1) % count]
        if (lat1 > lat) != (lat2 > lat):
            cross = lon1 + (lat - lat1) * (lon2 - lon1) / (lat2 - lat1)
            if lon < cross:
                inside = not inside
    return inside


def load_airport(icao, airports_dir=None):
    """Читает граф аэропорта из подготовленного офлайн JSON-файла."""
    directory = airports_dir or AIRPORTS_DIR
    path = directory / f"{icao.upper()}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Нет графа аэропорта {icao}: {path}. "
            "Выгрузите его командой: python data/fetch_airport.py " + icao.upper()
        )
    with path.open(encoding="utf-8") as handle:
        return AirportGraph(json.load(handle))
