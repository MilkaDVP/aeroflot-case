"""
Офлайн-выгрузка графа аэропорта из OpenStreetMap через Overpass API.

Запускается один раз на аэропорт, результат коммитится в репозиторий
(data/airports/<ICAO>.json). Сервер во время работы в интернет не ходит:
жюри разворачивает проект без доступа к сети.

Запуск:
    python fetch_airport.py UUEE
    python fetch_airport.py UUEE --no-patch      # без ручных правок
    python fetch_airport.py --list               # список известных аэропортов
"""

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from operator import itemgetter
from pathlib import Path

# --- Константы выгрузки -----------------------------------------------------

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT_S = 180
REQUEST_RETRIES = 3
RETRY_PAUSE_S = 5

EARTH_RADIUS_M = 6371008.8

# Типы линий OSM, которые становятся рёбрами графа.
# runway (ВПП) сознательно исключена: маршрут сотрудника не может
# пролегать через взлётно-посадочную полосу.
ROUTABLE_AEROWAY = ("taxiway", "taxilane", "parking_position")

# Каталог аэропортов. bbox = (юг, запад, север, восток) в градусах.
# Границы подобраны по лётному полю, без привокзальной территории и парковок.
AIRPORTS = {
    "UUEE": {
        "name": "Шереметьево",
        "city": "Москва",
        "bbox": (55.9420, 37.3200, 55.9990, 37.4700),
    },
    "UUDD": {
        "name": "Домодедово",
        "city": "Москва",
        # Границы взяты из отношения аэродрома в OSM с небольшим запасом:
        # рамка уже лётного поля обрезала бы рулёжки по краям.
        "bbox": (55.3840, 37.8550, 55.4330, 37.9620),
    },
    "ULLI": {
        "name": "Пулково",
        "city": "Санкт-Петербург",
        "bbox": (59.7830, 30.2100, 59.8130, 30.3400),
    },
}

OUTPUT_DIR = Path(__file__).parent / "airports"
RAW_DIR = OUTPUT_DIR / "raw"
PATCH_DIR = Path(__file__).parent / "patches"


# --- Геометрия --------------------------------------------------------------


def haversine_m(lat1, lon1, lat2, lon2):
    """Расстояние между двумя точками по поверхности Земли, в метрах."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def project_xy(lat, lon, ref_lat, ref_lon):
    """
    Линейная (равнопромежуточная) проекция lat/lon в метры относительно
    опорной точки аэропорта. На масштабе нескольких километров искажение
    пренебрежимо, зато карта рисуется обычным SVG без библиотек.

    Ось X — на восток, ось Y — на юг (как в экранных координатах).
    """
    x = math.radians(lon - ref_lon) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = math.radians(ref_lat - lat) * EARTH_RADIUS_M
    return round(x, 2), round(y, 2)


# --- Обращение к Overpass ---------------------------------------------------


def build_overpass_query(bbox, with_service_roads):
    """Собирает текст запроса Overpass QL для одного лётного поля."""
    south, west, north, east = bbox
    area = f"{south},{west},{north},{east}"
    aeroway_filter = "|".join(ROUTABLE_AEROWAY)

    parts = [f'way["aeroway"~"^({aeroway_filter})$"]({area});']
    if with_service_roads:
        # Служебные проезды перрона: по ним ездит спецтранспорт.
        parts.append(f'way["highway"="service"]["aeroway"!~"."]({area});')
    parts.append(f'node["aeroway"="parking_position"]({area});')

    body = "\n  ".join(parts)
    return (
        f"[out:json][timeout:{OVERPASS_TIMEOUT_S}];\n"
        f"(\n  {body}\n);\n"
        "out body;\n"
        ">;\n"
        "out skel qt;\n"
    )


def request_overpass(query):
    """Отправляет запрос с повторами: публичный Overpass часто отвечает 429."""
    payload = urllib.parse.urlencode({"data": query}).encode("utf-8")
    request = urllib.request.Request(
        OVERPASS_URL,
        data=payload,
        headers={"User-Agent": "aeroflot-dispatch-case/1.0 (offline map prep)"},
    )

    last_error = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=OVERPASS_TIMEOUT_S + 30) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            print(f"  попытка {attempt}/{REQUEST_RETRIES} не удалась: {error}", file=sys.stderr)
            if attempt < REQUEST_RETRIES:
                time.sleep(RETRY_PAUSE_S * attempt)

    raise RuntimeError(f"Overpass API недоступен: {last_error}")


# --- Разбор ответа OSM в граф ----------------------------------------------


def split_osm_elements(elements):
    """Разделяет плоский список элементов Overpass на точки и линии."""
    osm_nodes = {}
    osm_ways = []
    for element in elements:
        if element["type"] == "node":
            osm_nodes[element["id"]] = element
        elif element["type"] == "way" and len(element.get("nodes", [])) >= 2:
            osm_ways.append(element)
    return osm_nodes, osm_ways


def is_vehicle_way(tags):
    """Проходимо ли ребро для спецтранспорта."""
    # Рулёжки и служебные проезды — да. Позиция стоянки — тоже (машина
    # заезжает к борту). Пешеходные связки добавляются только вручную.
    return tags.get("aeroway") in ("taxiway", "taxilane", "parking_position") or (
        tags.get("highway") == "service"
    )


def count_node_usage(osm_ways):
    """Сколько линий проходит через каждую точку — так находим перекрёстки."""
    usage = defaultdict(int)
    for way in osm_ways:
        for node_id in set(way["nodes"]):
            usage[node_id] += 1
    return usage


def collect_vertex_ids(osm_ways, usage, stand_node_ids):
    """
    Выбирает точки, которые станут вершинами графа: перекрёстки линий,
    концы линий и помеченные стоянки. Остальные точки — просто геометрия
    изгиба рулёжки, в графе они не нужны.
    """
    vertices = set(stand_node_ids)
    for way in osm_ways:
        node_ids = way["nodes"]
        vertices.add(node_ids[0])
        vertices.add(node_ids[-1])
        for node_id in node_ids[1:-1]:
            if usage[node_id] > 1:
                vertices.add(node_id)
    return vertices


def build_edges(osm_ways, osm_nodes, vertex_ids):
    """
    Режет линии по вершинам. Каждый отрезок между двумя соседними вершинами
    становится ребром, его длина — сумма расстояний по всем промежуточным
    точкам, то есть реальная длина рулёжки, а не хорда.
    """
    edges = {}
    for way in osm_ways:
        tags = way.get("tags", {})
        vehicle_allowed = is_vehicle_way(tags)

        segment_start = None
        segment_length = 0.0
        segment_points = []
        previous = None

        for node_id in way["nodes"]:
            node = osm_nodes.get(node_id)
            if node is None:
                # Точка не попала в выгрузку (линия обрезана рамкой bbox).
                continue

            if previous is not None:
                segment_length += haversine_m(
                    previous["lat"], previous["lon"], node["lat"], node["lon"]
                )
            previous = node

            if node_id not in vertex_ids:
                # Промежуточная точка: в графе она не нужна, но без неё
                # изогнутая рулёжка нарисуется прямой хордой.
                if segment_start is not None:
                    segment_points.append((node["lat"], node["lon"]))
                continue

            if segment_start is not None and segment_length > 0:
                key = (min(segment_start, node_id), max(segment_start, node_id))
                # Геометрия хранится в направлении from_id -> to_id,
                # а ключ ребра отсортирован — при развороте разворачиваем
                # и список точек.
                points = (
                    list(segment_points)
                    if key[0] == segment_start
                    else list(reversed(segment_points))
                )
                # При наложении линий оставляем более короткий вариант.
                if key not in edges or edges[key]["distance_m"] > segment_length:
                    edges[key] = {
                        "from_id": f"n{key[0]}",
                        "to_id": f"n{key[1]}",
                        "distance_m": round(segment_length, 1),
                        "vehicle_allowed": vehicle_allowed,
                        "points": points,
                    }
                elif vehicle_allowed:
                    edges[key]["vehicle_allowed"] = True

            segment_start = node_id
            segment_length = 0.0
            segment_points = []

    return list(edges.values())


def build_nodes(vertex_ids, osm_nodes, stand_refs):
    """Собирает вершины графа с типом и названием стоянки, без проекции."""
    nodes = []
    for node_id in sorted(vertex_ids):
        node = osm_nodes.get(node_id)
        if node is None:
            continue
        # Стоянкой считаем только пронумерованную позицию: на безномерную
        # нельзя поставить борт, значит и вызова на неё быть не может.
        # Остальные точки parking_position остаются узлами геометрии.
        node_type = "stand" if stand_refs.get(node_id) else "junction"
        nodes.append(
            {
                "id": f"n{node_id}",
                "type": node_type,
                "ref": stand_refs.get(node_id),
                "lat": round(node["lat"], 7),
                "lon": round(node["lon"], 7),
                "x": None,
                "y": None,
            }
        )
    return nodes


def compute_ref_point(nodes):
    """
    Опорная точка проекции — центр реального охвата данных, а не центр bbox.

    Рамка запроса всегда шире лётного поля, к тому же Overpass возвращает
    линии целиком, даже если они выходят за рамку. Если считать от центра
    рамки, карта окажется смещённой и половина холста будет пустой.
    """
    lats = [node["lat"] for node in nodes]
    lons = [node["lon"] for node in nodes]
    return {
        "lat": round((min(lats) + max(lats)) / 2, 7),
        "lon": round((min(lons) + max(lons)) / 2, 7),
    }


def project_nodes(nodes, ref_point):
    """Проставляет узлам метровые координаты x/y для отрисовки карты."""
    for node in nodes:
        node["x"], node["y"] = project_xy(
            node["lat"], node["lon"], ref_point["lat"], ref_point["lon"]
        )


def project_edges(edges, ref_point):
    """
    Переводит геометрию рёбер в метровые координаты.

    Промежуточные точки хранятся только в x/y: в градусах они заняли бы
    вдвое больше места, а нужны они исключительно для отрисовки карты,
    которая и так работает в метрах.
    """
    for edge in edges:
        edge["points"] = [
            list(project_xy(lat, lon, ref_point["lat"], ref_point["lon"]))
            for lat, lon in edge.get("points", [])
        ]


def collect_stand_refs(osm_nodes, osm_ways):
    """
    Находит стоянки ВС. В OSM parking_position встречается и точкой,
    и короткой линией — во втором случае стоянкой считаем конец линии,
    обращённый к зданию терминала (первую точку линии).
    """
    stand_refs = {}
    for node in osm_nodes.values():
        tags = node.get("tags", {})
        if tags.get("aeroway") == "parking_position":
            stand_refs[node["id"]] = tags.get("ref") or tags.get("name")

    for way in osm_ways:
        tags = way.get("tags", {})
        if tags.get("aeroway") != "parking_position":
            continue
        stand_node_id = way["nodes"][0]
        if stand_node_id not in stand_refs:
            stand_refs[stand_node_id] = tags.get("ref") or tags.get("name")

    return stand_refs


# --- Связность --------------------------------------------------------------


def largest_connected_component(nodes, edges):
    """
    Оставляет только наибольшую связную компоненту.

    OSM у краёв bbox даёт обрывки рулёжек, не соединённые с основным полем.
    Если их не убрать, Дейкстра на таком узле молча вернёт «пути нет»,
    и на демонстрации это будет выглядеть как ошибка алгоритма.
    """
    neighbours = defaultdict(set)
    for edge in edges:
        neighbours[edge["from_id"]].add(edge["to_id"])
        neighbours[edge["to_id"]].add(edge["from_id"])

    seen = set()
    best = set()
    for node in nodes:
        start = node["id"]
        if start in seen:
            continue
        component = set()
        queue = [start]
        seen.add(start)
        while queue:
            current = queue.pop()
            component.add(current)
            for neighbour in neighbours[current]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
        if len(component) > len(best):
            best = component

    kept_nodes = [node for node in nodes if node["id"] in best]
    kept_edges = [
        edge for edge in edges if edge["from_id"] in best and edge["to_id"] in best
    ]
    return kept_nodes, kept_edges


# --- Ручные правки ----------------------------------------------------------


def nearest_nodes(target, candidates, count):
    """
    Возвращает `count` ближайших к точке узлов из числа кандидатов.

    Расстояние считается один раз на узел и кладётся в кортеж вместе
    с порядковым номером: номер разрешает равенство расстояний, иначе
    сортировка попыталась бы сравнивать сами словари узлов.
    """
    measured = [
        (haversine_m(target["lat"], target["lon"], node["lat"], node["lon"]), index, node)
        for index, node in enumerate(candidates)
    ]
    measured.sort(key=itemgetter(0, 1))
    return [node for _, _, node in measured[:count]]


def connect_node_to_graph(node, graph_nodes, options):
    """
    Соединяет добавленный вручную узел с ближайшими узлами графа.

    Иначе техцентр останется изолированной точкой, и Дейкстра не найдёт
    из него ни одного маршрута. Число связей задаётся в файле правок:
    одна связь — тупиковый съезд, две и больше — сквозной проезд.
    """
    count = options.get("count", 2)
    vehicle_allowed = options.get("vehicle_allowed", True)
    new_edges = []

    for neighbour in nearest_nodes(node, graph_nodes, count):
        distance = haversine_m(
            node["lat"], node["lon"], neighbour["lat"], neighbour["lon"]
        )
        new_edges.append(
            {
                "from_id": node["id"],
                "to_id": neighbour["id"],
                "distance_m": round(distance, 1),
                "vehicle_allowed": vehicle_allowed,
                "points": [],
            }
        )
    return new_edges


def apply_patch(airport, patch):
    """
    Накладывает ручные правки поверх выгрузки OSM.

    Зачем это нужно: в OSM нет техцентра ОТО, часть стоянок без номеров,
    а пешеходные проходы по перрону не размечены вовсе. Правки лежат
    отдельным файлом, чтобы всегда было видно, где реальные данные,
    а где наше дополнение.
    """
    by_id = {node["id"]: node for node in airport["nodes"]}
    ref_lat = airport["ref_point"]["lat"]
    ref_lon = airport["ref_point"]["lon"]
    # Узлы из OSM запоминаем до правок: новые узлы цепляем к настоящему
    # графу, а не друг к другу.
    osm_nodes = list(airport["nodes"])

    for node in patch.get("add_nodes", []):
        x, y = project_xy(node["lat"], node["lon"], ref_lat, ref_lon)
        prepared = {
            "id": node["id"],
            "type": node["type"],
            "ref": node.get("ref"),
            "lat": node["lat"],
            "lon": node["lon"],
            "x": x,
            "y": y,
        }
        by_id[node["id"]] = prepared
        airport["nodes"].append(prepared)

        if "connect_nearest" in node:
            airport["edges"].extend(
                connect_node_to_graph(prepared, osm_nodes, node["connect_nearest"])
            )

    for change in patch.get("set_nodes", []):
        target = by_id.get(change["id"])
        if target is None:
            print(f"  ! правка узла {change['id']}: узла нет в выгрузке", file=sys.stderr)
            continue
        target.update({key: value for key, value in change.items() if key != "id"})

    for edge in patch.get("add_edges", []):
        source = by_id.get(edge["from_id"])
        target = by_id.get(edge["to_id"])
        if source is None or target is None:
            print(
                f"  ! ребро {edge['from_id']}—{edge['to_id']}: узла нет в выгрузке",
                file=sys.stderr,
            )
            continue
        distance = edge.get("distance_m")
        if distance is None:
            distance = round(
                haversine_m(source["lat"], source["lon"], target["lat"], target["lon"]), 1
            )
        airport["edges"].append(
            {
                "from_id": edge["from_id"],
                "to_id": edge["to_id"],
                "distance_m": distance,
                "vehicle_allowed": edge.get("vehicle_allowed", True),
                # Ручные связки рисуются прямой: промежуточной геометрии
                # у них нет по определению.
                "points": [],
            }
        )

    return airport


def load_patch(icao):
    """Читает файл ручных правок, если он есть."""
    path = PATCH_DIR / f"{icao}.patch.json"
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


# --- Сборка -----------------------------------------------------------------


def build_airport(icao, osm_data):
    """Превращает ответ Overpass в граф аэропорта нашего формата."""
    meta = AIRPORTS[icao]
    osm_nodes, osm_ways = split_osm_elements(osm_data.get("elements", []))
    stand_refs = collect_stand_refs(osm_nodes, osm_ways)
    routable_ways = [
        way
        for way in osm_ways
        if way.get("tags", {}).get("aeroway") != "parking_position"
        or len(way["nodes"]) >= 2
    ]

    usage = count_node_usage(routable_ways)
    vertex_ids = collect_vertex_ids(routable_ways, usage, set(stand_refs))
    edges = build_edges(routable_ways, osm_nodes, vertex_ids)
    nodes = build_nodes(vertex_ids, osm_nodes, stand_refs)

    return {
        "icao": icao,
        "name": meta["name"],
        "city": meta["city"],
        "ref_point": None,
        "bbox": list(meta["bbox"]),
        "source": "OpenStreetMap (ODbL), выгрузка через Overpass API",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nodes": nodes,
        "edges": edges,
    }


def report(airport, raw_counts, connected_counts):
    """Печатает сводку — по ней видно, годится ли граф для маршрутизации."""
    stands = [node for node in airport["nodes"] if node["type"] == "stand"]
    tech = [node for node in airport["nodes"] if node["type"] == "tech_center"]
    dropped = raw_counts[0] - connected_counts[0]

    print(f"  из OSM:          {raw_counts[0]} узлов, {raw_counts[1]} рёбер")
    print(f"  после связности: {connected_counts[0]} узлов, {connected_counts[1]} рёбер"
          f" (отброшено обрывков: {dropped})")
    print(f"  после правок:    {len(airport['nodes'])} узлов, {len(airport['edges'])} рёбер")
    print(f"  стоянок:         {len(stands)}")
    print(f"  техцентров:      {len(tech)}")
    if not tech:
        print("  ! техцентр не задан — добавьте его в файл правок", file=sys.stderr)


def save_json(path, payload):
    """Пишет JSON с отступами: файл лежит в git, диффы должны читаться."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("icao", nargs="?", help="код ИКАО аэропорта, например UUEE")
    parser.add_argument("--list", action="store_true", help="показать известные аэропорты")
    parser.add_argument("--no-patch", action="store_true", help="не накладывать ручные правки")
    parser.add_argument(
        "--service-roads", action="store_true", help="добавить служебные проезды (highway=service)"
    )
    parser.add_argument("--raw", help="взять сырой ответ OSM из файла вместо запроса к Overpass")
    args = parser.parse_args()

    if args.list:
        for icao, meta in AIRPORTS.items():
            print(f"{icao}  {meta['name']} ({meta['city']})")
        return 0

    if not args.icao:
        parser.error("укажите код ИКАО или --list")

    icao = args.icao.upper()
    if icao not in AIRPORTS:
        print(f"Аэропорт {icao} не описан в AIRPORTS", file=sys.stderr)
        return 1

    if args.raw:
        print(f"Читаю сырой ответ из {args.raw}")
        with open(args.raw, encoding="utf-8") as handle:
            osm_data = json.load(handle)
    else:
        print(f"Запрашиваю {icao} у Overpass API…")
        query = build_overpass_query(AIRPORTS[icao]["bbox"], args.service_roads)
        osm_data = request_overpass(query)
        save_json(RAW_DIR / f"{icao}.osm.json", osm_data)
        print(f"  сырой ответ сохранён: {RAW_DIR / (icao + '.osm.json')}")

    airport = build_airport(icao, osm_data)
    raw_counts = (len(airport["nodes"]), len(airport["edges"]))

    airport["nodes"], airport["edges"] = largest_connected_component(
        airport["nodes"], airport["edges"]
    )
    connected_counts = (len(airport["nodes"]), len(airport["edges"]))
    airport["ref_point"] = compute_ref_point(airport["nodes"])
    project_nodes(airport["nodes"], airport["ref_point"])
    project_edges(airport["edges"], airport["ref_point"])

    if not args.no_patch:
        patch = load_patch(icao)
        if patch:
            print(f"  накладываю правки: {PATCH_DIR / (icao + '.patch.json')}")
            airport = apply_patch(airport, patch)
        else:
            print("  файла правок нет, пропускаю")

    report(airport, raw_counts, connected_counts)

    output_path = OUTPUT_DIR / f"{icao}.json"
    save_json(output_path, airport)
    print(f"Готово: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
