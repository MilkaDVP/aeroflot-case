"""
Построение графа аэропорта по данным OpenStreetMap.

Здесь лежит ядро выгрузки: запрос к Overpass, разбор ответа в узлы и рёбра,
отсечение обрывков и проекция в метры. Тем же кодом пользуются двое:

* `data/fetch_airport.py` — офлайн-подготовка графов, которые коммитятся
  в репозиторий и работают без сети;
* администратор в интерфейсе — «добавить аэропорт по коду ИКАО».

Одно место — один формат графа. Раньше выгрузка жила только в скрипте,
и добавление аэропорта в работающую систему означало бы второй разбор OSM
со своими расхождениями.

Важно: сервер обращается в интернет ТОЛЬКО в момент добавления аэропорта
по явному действию администратора. Подбор, маршруты и вся остальная работа
идут по графу в памяти и сеть не используют.
"""

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from operator import itemgetter

# Публичные серверы Overpass с одинаковыми данными OSM. Основной регулярно
# обрывает соединение (SSL EOF, 429), поэтому при сбое запрос уходит
# на следующее зеркало, а не повторяется в тот же отказавший сервер.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
OVERPASS_TIMEOUT_S = 180
# Полных обходов списка зеркал.
REQUEST_RETRIES = 2
RETRY_PAUSE_S = 3

EARTH_RADIUS_M = 6371008.8

# Типы линий OSM, которые становятся рёбрами графа.
# runway (ВПП) сознательно исключена: маршрут сотрудника не может
# пролегать через взлётно-посадочную полосу.
ROUTABLE_AEROWAY = ("taxiway", "taxilane", "parking_position")

# Запас к границам аэродрома: рамка ровно по контуру обрезала бы рулёжки
# по краям поля.
BBOX_MARGIN_DEG = 0.004

# Если аэродром найден в OSM одной точкой, а не контуром, берём квадрат
# вокруг неё: примерно 4 километра в каждую сторону.
POINT_BBOX_HALF_DEG = 0.035


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


def build_overpass_query(bbox, with_service_roads=False):
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


def build_aerodrome_query(icao):
    """
    Запрос на поиск самого аэродрома по коду ИКАО.

    Нужен, чтобы администратору хватило одного кода: границы лётного поля
    берутся из OSM, а не задаются руками. Ищем и по тегу icao, и по ref —
    в разных странах заполнен то один, то другой.
    """
    code = icao.upper()
    return (
        "[out:json][timeout:60];\n"
        "(\n"
        f'  way["aeroway"="aerodrome"]["icao"="{code}"];\n'
        f'  relation["aeroway"="aerodrome"]["icao"="{code}"];\n'
        f'  node["aeroway"="aerodrome"]["icao"="{code}"];\n'
        f'  way["aeroway"="aerodrome"]["ref"="{code}"];\n'
        f'  relation["aeroway"="aerodrome"]["ref"="{code}"];\n'
        ");\n"
        "out tags bb;\n"
    )


def request_overpass(query, on_retry=None):
    """
    Отправляет запрос с повторами: публичный Overpass часто отвечает 429.

    on_retry — необязательный обработчик сообщения о неудачной попытке.
    Модуль не печатает сам: в скрипте сообщение идёт в stderr, а на сервере
    попадает в лог, и решать это должен вызывающий код.
    """
    payload = urllib.parse.urlencode({"data": query}).encode("utf-8")

    last_error = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        for url in OVERPASS_URLS:
            try:
                return fetch_json(url, payload)
            except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as error:
                # ValueError — зеркало ответило не JSON (страница перегрузки).
                last_error = error
                if on_retry is not None:
                    host = urllib.parse.urlparse(url).netloc
                    on_retry(f"{host}: попытка {attempt}/{REQUEST_RETRIES} не удалась: {error}")
        if attempt < REQUEST_RETRIES:
            time.sleep(RETRY_PAUSE_S * attempt)

    raise RuntimeError(f"Overpass API недоступен: {last_error}")


def fetch_json(url, payload):
    """Один запрос к одному серверу Overpass."""
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"User-Agent": "aeroflot-dispatch-case/1.0 (offline map prep)"},
    )
    with urllib.request.urlopen(request, timeout=OVERPASS_TIMEOUT_S + 30) as response:
        return json.loads(response.read().decode("utf-8"))


def find_aerodrome(icao, on_retry=None):
    """
    Находит аэродром по коду ИКАО и возвращает его название и границы.

    Возвращает словарь с name, city и bbox либо None, если такого кода
    в OpenStreetMap нет.
    """
    data = request_overpass(build_aerodrome_query(icao), on_retry)
    elements = data.get("elements", [])
    if not elements:
        return None

    # Из нескольких совпадений берём то, у которого есть контур: по нему
    # известны настоящие границы поля.
    with_bounds = [item for item in elements if item.get("bounds")]
    element = with_bounds[0] if with_bounds else elements[0]
    tags = element.get("tags", {})

    return {
        "icao": icao.upper(),
        "name": aerodrome_name(tags, icao),
        "city": tags.get("addr:city") or tags.get("is_in:city") or "",
        "bbox": aerodrome_bbox(element),
    }


def aerodrome_name(tags, icao):
    """Название аэропорта: по-русски, если оно есть в OSM."""
    return (
        tags.get("name:ru")
        or tags.get("name")
        or tags.get("name:en")
        or icao.upper()
    )


def aerodrome_bbox(element):
    """
    Границы лётного поля с небольшим запасом.

    Контур аэродрома даёт рамку сразу; одиночная точка — только центр,
    тогда строим квадрат вокруг неё.
    """
    bounds = element.get("bounds")
    if bounds:
        return (
            round(bounds["minlat"] - BBOX_MARGIN_DEG, 4),
            round(bounds["minlon"] - BBOX_MARGIN_DEG, 4),
            round(bounds["maxlat"] + BBOX_MARGIN_DEG, 4),
            round(bounds["maxlon"] + BBOX_MARGIN_DEG, 4),
        )

    lat = element["lat"]
    lon = element["lon"]
    return (
        round(lat - POINT_BBOX_HALF_DEG, 4),
        round(lon - POINT_BBOX_HALF_DEG, 4),
        round(lat + POINT_BBOX_HALF_DEG, 4),
        round(lon + POINT_BBOX_HALF_DEG, 4),
    )


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


# --- Сборка -----------------------------------------------------------------


def build_airport(icao, osm_data, meta):
    """Превращает ответ Overpass в граф аэропорта нашего формата."""
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
        "icao": icao.upper(),
        "name": meta["name"],
        "city": meta["city"],
        "ref_point": None,
        "bbox": list(meta["bbox"]),
        "source": "OpenStreetMap (ODbL), выгрузка через Overpass API",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nodes": nodes,
        "edges": edges,
    }


def finalise(airport):
    """
    Доводит собранный граф до рабочего вида.

    Отсекает несвязанные обрывки, считает опорную точку и проецирует всё
    в метры. Вынесено отдельно, потому что порядок здесь важен: проекция
    должна считаться уже после отбрасывания обрывков, иначе опорная точка
    уедет к дальнему краю рамки.
    """
    airport["nodes"], airport["edges"] = largest_connected_component(
        airport["nodes"], airport["edges"]
    )
    airport["ref_point"] = compute_ref_point(airport["nodes"])
    project_nodes(airport["nodes"], airport["ref_point"])
    project_edges(airport["edges"], airport["ref_point"])
    return airport


def import_airport(icao, with_service_roads=False, on_retry=None):
    """
    Полный путь «код ИКАО → готовый граф».

    Возвращает пару (граф, сведения об аэродроме) либо (None, None), если
    такого кода в OpenStreetMap нет. Граф уже связный и спроецированный,
    его можно сразу отдавать карте.
    """
    aerodrome = find_aerodrome(icao, on_retry)
    if aerodrome is None:
        return None, None

    osm_data = request_overpass(
        build_overpass_query(aerodrome["bbox"], with_service_roads), on_retry
    )
    airport = build_airport(icao, osm_data, aerodrome)
    if not airport["nodes"] or not airport["edges"]:
        return None, aerodrome

    return finalise(airport), aerodrome
