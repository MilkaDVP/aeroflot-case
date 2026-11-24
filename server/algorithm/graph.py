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

import json
import math
from collections import defaultdict

from constants import AIRPORTS_DIR, MODE_VEHICLE
from algorithm.geo import haversine_m


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
        return self.nodes.get(node_id)

    def links(self, node_id, mode):
        """
        Рёбра от вершины, разрешённые выбранному способу передвижения.

        Возвращает тройки (сосед, длина в метрах, ограничение скорости
        в км/ч или None).
        """
        index = 1 if mode == MODE_VEHICLE else 2
        return [
            (link[0], link[3], link[4]) for link in self._adjacency[node_id] if link[index]
        ]

    def bends(self, start_id, end_id):
        """
        Изгибы дороги между соседними вершинами — точки (широта, долгота).

        Без них путь по дуге выглядел бы прямой через здания, а идущий
        по дороге инженер казался бы ушедшим с маршрута.
        """
        return self._bends.get((start_id, end_id), [])

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
        """Стоянка по её номеру на перроне, например «31» или «102A»."""
        for node in self.nodes.values():
            if node["type"] == "stand" and node["ref"] == ref:
                return node
        return None

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
