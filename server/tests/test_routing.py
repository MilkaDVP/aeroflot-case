"""
Тесты поиска маршрута.

Часть тестов работает на маленьком синтетическом графе, где правильный
ответ известен заранее и посчитан руками. Часть — на настоящем графе
Шереметьево: он проверяет, что выгрузка из OSM пригодна для работы,
а не только формально валидна.
"""

import unittest

from constants import (
    AIRPORTS_DIR,
    MODE_VEHICLE,
    MODE_WALK,
    REGULATION_ARRIVAL_LIMIT_MIN,
    VEHICLE_SPEED_KMH,
    WALK_SPEED_KMH,
)
from algorithm.geo import haversine_m
from algorithm.graph import AirportGraph, load_airport
from algorithm.routing import (
    route_from_point,
    shortest_path,
    shortest_paths_from,
    travel_time_minutes,
)


def make_test_graph():
    """
    Синтетический граф-«вилка» для проверки выбора из двух путей.

        A ──100── B ──100── D        верхний путь: 200 м, проезд разрешён
        │                   │
        └──── C ────────────┘        нижний путь: 60 + 60 = 120 м,
             60        60            но только пешком

    Пешком кратчайший путь — через C (120 м).
    Спецтранспорту низ закрыт, он обязан ехать через B (200 м).
    Узел E не связан ни с чем: проверка отсутствия маршрута.
    """
    payload = {
        "icao": "TEST",
        "name": "Тестовый",
        "city": "—",
        "ref_point": {"lat": 55.0, "lon": 37.0},
        "nodes": [
            {"id": "A", "type": "tech_center", "ref": "ТЦ", "lat": 55.0000, "lon": 37.0000, "x": 0, "y": 0},
            {"id": "B", "type": "junction", "ref": None, "lat": 55.0009, "lon": 37.0000, "x": 0, "y": -100},
            {"id": "C", "type": "junction", "ref": None, "lat": 54.9995, "lon": 37.0000, "x": 0, "y": 60},
            {"id": "D", "type": "stand", "ref": "31", "lat": 55.0009, "lon": 37.0016, "x": 100, "y": -100},
            {"id": "E", "type": "stand", "ref": "99", "lat": 55.0100, "lon": 37.0100, "x": 900, "y": -900},
        ],
        "edges": [
            {"from_id": "A", "to_id": "B", "distance_m": 100.0, "vehicle_allowed": True},
            {"from_id": "B", "to_id": "D", "distance_m": 100.0, "vehicle_allowed": True},
            {"from_id": "A", "to_id": "C", "distance_m": 60.0, "vehicle_allowed": False},
            {"from_id": "C", "to_id": "D", "distance_m": 60.0, "vehicle_allowed": False},
        ],
    }
    return AirportGraph(payload)


class TestShortestPath(unittest.TestCase):
    """Поиск пути на графе с заранее известным ответом."""

    def setUp(self):
        self.graph = make_test_graph()

    def test_выбирает_кратчайший_из_двух_путей(self):
        route = shortest_path(self.graph, "A", "D", MODE_WALK)
        self.assertEqual(route.node_ids, ["A", "C", "D"])
        self.assertAlmostEqual(route.distance_m, 120.0)

    def test_спецтранспорт_объезжает_пешеходный_участок(self):
        route = shortest_path(self.graph, "A", "D", MODE_VEHICLE)
        # Короткий путь через C закрыт флагом vehicle_allowed=False,
        # значит машина обязана ехать длинным путём через B.
        self.assertEqual(route.node_ids, ["A", "B", "D"])
        self.assertAlmostEqual(route.distance_m, 200.0)

    def test_маршрут_симметричен(self):
        forward = shortest_path(self.graph, "A", "D", MODE_WALK)
        backward = shortest_path(self.graph, "D", "A", MODE_WALK)
        self.assertAlmostEqual(forward.distance_m, backward.distance_m)
        self.assertEqual(forward.node_ids, list(reversed(backward.node_ids)))

    def test_нет_маршрута_до_изолированной_вершины(self):
        self.assertIsNone(shortest_path(self.graph, "A", "E", MODE_WALK))

    def test_нет_маршрута_до_несуществующей_вершины(self):
        self.assertIsNone(shortest_path(self.graph, "A", "нет-такой", MODE_WALK))

    def test_путь_в_саму_себя_нулевой(self):
        route = shortest_path(self.graph, "A", "A", MODE_WALK)
        self.assertEqual(route.node_ids, ["A"])
        self.assertEqual(route.distance_m, 0.0)

    def test_дейкстра_от_вершины_ко_всем(self):
        distances = shortest_paths_from(self.graph, "A", MODE_WALK)
        self.assertAlmostEqual(distances["D"], 120.0)
        self.assertAlmostEqual(distances["B"], 100.0)
        # Изолированная вершина недостижима и в словарь не попадает.
        self.assertNotIn("E", distances)


class TestTravelTime(unittest.TestCase):
    """Перевод расстояния во время в пути."""

    def test_пешком_по_нормативной_скорости(self):
        # 1 км при 4.5 км/ч — ровно 1/4.5 часа.
        minutes = travel_time_minutes(1000.0, MODE_WALK)
        self.assertAlmostEqual(minutes, 60.0 / WALK_SPEED_KMH)

    def test_спецтранспорт_быстрее_пешехода(self):
        walk = travel_time_minutes(1000.0, MODE_WALK)
        vehicle = travel_time_minutes(1000.0, MODE_VEHICLE)
        self.assertLess(vehicle, walk)
        self.assertAlmostEqual(vehicle, 60.0 / VEHICLE_SPEED_KMH)

    def test_расстояние_за_регламентные_15_минут_пешком(self):
        # Сколько метров успевает пройти сотрудник за норматив — цифра,
        # по которой сразу видно масштаб задачи: пешком это чуть больше
        # километра, то есть половина перрона Шереметьево уже вне норматива.
        reach_m = WALK_SPEED_KMH * 1000.0 / 60.0 * REGULATION_ARRIVAL_LIMIT_MIN
        self.assertAlmostEqual(
            travel_time_minutes(reach_m, MODE_WALK), REGULATION_ARRIVAL_LIMIT_MIN
        )


class TestRouteFromPoint(unittest.TestCase):
    """Посадка GPS-координат сотрудника на граф."""

    def setUp(self):
        self.graph = make_test_graph()

    def test_подход_добавляется_к_длине_маршрута(self):
        # Точка рядом с A, но не в ней: маршрут должен стать длиннее
        # ровно на расстояние подхода к ближайшей вершине.
        route = route_from_point(self.graph, 55.0001, 37.0000, "D", MODE_WALK)
        self.assertEqual(route.node_ids, ["A", "C", "D"])
        self.assertGreater(route.approach_m, 0.0)
        self.assertAlmostEqual(route.distance_m, 120.0 + route.approach_m)

    def test_ближайшая_вершина_учитывает_способ_передвижения(self):
        # Для спецтранспорта вершины без проезжих рёбер не годятся
        # как точка входа в граф.
        node, _ = self.graph.nearest_node(54.9995, 37.0000, MODE_VEHICLE)
        self.assertNotEqual(node["id"], "C")


class TestRealAirportGraph(unittest.TestCase):
    """
    Проверки на настоящем графе Шереметьево.

    Если файла нет — тесты пропускаются, а не падают: выгрузка делается
    отдельным шагом и требует доступа к Overpass API.
    """

    @classmethod
    def setUpClass(cls):
        if not (AIRPORTS_DIR / "UUEE.json").exists():
            raise unittest.SkipTest("нет data/airports/UUEE.json — выполните выгрузку")
        cls.graph = load_airport("UUEE")

    def test_граф_содержит_стоянки_и_техцентры(self):
        self.assertGreater(len(self.graph.nodes_of_type("stand")), 100)
        self.assertGreaterEqual(len(self.graph.nodes_of_type("tech_center")), 1)

    def test_у_каждой_стоянки_есть_номер(self):
        # Стоянка без номера бесполезна: диспетчер не сможет её выбрать.
        without_ref = [
            node for node in self.graph.nodes_of_type("stand") if not node["ref"]
        ]
        self.assertEqual(without_ref, [])

    def test_из_техцентра_достижимы_все_стоянки(self):
        # Ключевая проверка пригодности выгрузки: если хоть одна стоянка
        # недостижима, на демонстрации подбор на неё молча провалится.
        tech_center = self.graph.nodes_of_type("tech_center")[0]
        distances = shortest_paths_from(self.graph, tech_center["id"], MODE_WALK)
        unreachable = [
            node["ref"]
            for node in self.graph.nodes_of_type("stand")
            if node["id"] not in distances
        ]
        self.assertEqual(unreachable, [], f"недостижимы стоянки: {unreachable}")

    def test_маршрут_длиннее_прямой_но_не_абсурдно(self):
        # Здравый смысл: путь по рулёжкам не может быть короче прямой,
        # но и не должен превышать её в разы — иначе граф рваный.
        tech_center = self.graph.nodes_of_type("tech_center")[0]
        stand = self.graph.nodes_of_type("stand")[0]
        route = shortest_path(self.graph, tech_center["id"], stand["id"], MODE_WALK)
        straight = haversine_m(
            tech_center["lat"], tech_center["lon"], stand["lat"], stand["lon"]
        )
        self.assertIsNotNone(route)
        self.assertGreaterEqual(route.distance_m, straight * 0.99)
        self.assertLess(route.distance_m, straight * 3.0)


if __name__ == "__main__":
    unittest.main()
