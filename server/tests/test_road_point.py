"""Машина посреди дороги: маршрут доходит до неё самой, а не до перекрёстка."""

import unittest

from algorithm.geo import haversine_m
from algorithm.graph import load_airport
from algorithm.routing import shortest_path
from algorithm.transport import plan_for, plan_via
from constants import MODE_VEHICLE, MODE_WALK
from tests.test_polygon import HAS_POLYGON

CAR = {"id": 3, "call_sign": "vehicle-03", "lat": 55.83533110068074, "lon": 37.634000292714525, "speed_kmh": None}


@unittest.skipUnless(HAS_POLYGON, "нет графа полигона")
class TestRoadPoint(unittest.TestCase):
    def setUp(self):
        self.graph = load_airport("VDNH")

    def test_машина_на_петле(self):
        node, offset = self.graph.nearest_road(CAR["lat"], CAR["lon"], MODE_VEHICLE, MODE_WALK)
        self.assertLess(offset, 0.5)
        self.assertLess(haversine_m(node["lat"], node["lon"], CAR["lat"], CAR["lon"]), 0.5)
        # Идентификатор читается и другим экземпляром графа — после перезапуска.
        again = load_airport("VDNH").node(node["id"])
        self.assertEqual((again["lat"], again["lon"]), (node["lat"], node["lon"]))

    def test_путь_по_дороге(self):
        point = self.graph.nearest_road(CAR["lat"], CAR["lon"], MODE_VEHICLE)[0]["id"]
        for start, end in (("v102", point), (point, "v101")):
            route = shortest_path(self.graph, start, end, MODE_VEHICLE)
            self.assertEqual(route.node_ids, [start, end])
            a, b = self.graph.node(start), self.graph.node(end)
            line = [(a["lat"], a["lon"]), *self.graph.bends(start, end), (b["lat"], b["lon"])]
            length = sum(haversine_m(*p, *q) for p, q in zip(line, line[1:]))
            self.assertAlmostEqual(length, route.distance_m, delta=0.5)
            self.assertEqual(self.graph.bends(end, start), self.graph.bends(start, end)[::-1])
        whole = sum(shortest_path(self.graph, point, n, MODE_VEHICLE).distance_m for n in ("v101", "v102"))
        self.assertAlmostEqual(whole, 33.16, delta=0.1)

    def test_план_через_машину(self):
        walker = {"lat": 55.8345, "lon": 37.6340, "vehicle": None, "speed_kmh": None}
        plan = plan_via(self.graph, walker, "stand_2", CAR)
        spot = self.graph.node(plan.pickup_node_id)
        self.assertLess(haversine_m(spot["lat"], spot["lon"], CAR["lat"], CAR["lon"]), 0.5)
        self.assertEqual(plan.node_ids.count(plan.pickup_node_id), 1)
        # За рулём — маршрут начинается с места машины.
        driver = {"lat": CAR["lat"], "lon": CAR["lon"], "vehicle": CAR, "speed_kmh": None}
        self.assertEqual(plan_for(self.graph, driver, "stand_2", []).node_ids[0], plan.pickup_node_id)

    def test_граф_не_меняется(self):
        before = len(self.graph.nodes)
        point = self.graph.nearest_road(CAR["lat"], CAR["lon"], MODE_VEHICLE)[0]["id"]
        shortest_path(self.graph, "v102", point, MODE_VEHICLE)
        self.assertEqual(len(self.graph.nodes), before)
        self.assertNotIn(point, [n for n, _ in self.graph.neighbours("v102", MODE_VEHICLE)])
        self.assertIsNone(self.graph.node("~v101~v102~1~1"))
