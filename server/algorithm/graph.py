"""
Граф аэропорта: загрузка подготовленного JSON и доступ к вершинам.

Граф неориентированный: по рулёжке ходят и ездят в обе стороны.
Рёбра различаются флагом vehicle_allowed — спецтранспорт проходит
не везде, где проходит человек.
"""

import json
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

    def neighbours(self, node_id, mode):
        """
        Соседи вершины, достижимые выбранным способом передвижения.

        Возвращает пары (идентификатор соседа, длина ребра в метрах).
        """
        if mode == MODE_VEHICLE:
            return [
                (neighbour_id, distance)
                for neighbour_id, distance, vehicle_allowed in self._adjacency[node_id]
                if vehicle_allowed
            ]
        return [
            (neighbour_id, distance)
            for neighbour_id, distance, _ in self._adjacency[node_id]
        ]

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
    """
    adjacency = defaultdict(list)
    for edge in edges:
        distance = edge["distance_m"]
        vehicle_allowed = edge["vehicle_allowed"]
        adjacency[edge["from_id"]].append((edge["to_id"], distance, vehicle_allowed))
        adjacency[edge["to_id"]].append((edge["from_id"], distance, vehicle_allowed))
    return adjacency


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
