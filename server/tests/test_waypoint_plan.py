"""Порядок промежуточных точек: самый быстрый, без лишних возвратов."""

import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from algorithm.routing import shortest_path
from algorithm.transport import plan_for
from constants import MODE_WALK
from services import waypoint_plan
from tests.test_dispatch import make_chain_airport, make_engineer


def make_call(**fields):
    return SimpleNamespace(**{
        "stand_node_id": "c4", "waypoints": None, "needs_tools": False, "tools_taken_at": None,
        "route_stops": None, "assigned_employee": None, "vehicle": None, "airport_icao": "TEST", **fields,
    })


class TestOrder(unittest.TestCase):
    """Линейка c0 … c4, борт в c4, инженер в c0: лишний возврат виден сразу."""

    def setUp(self):
        self.graph = make_chain_airport()

    def plan(self, groups, keep_order=False, call=None):
        people = SimpleNamespace(candidates=[SimpleNamespace(employee=make_engineer(1, node_index=0))])
        with mock.patch("services.waypoint_plan.free_vehicles", return_value=[]):
            return waypoint_plan.plan(None, call or make_call(), self.graph, groups, keep_order, lambda: people)

    def test_названные_вразнобой_точки_идут_по_пути(self):
        result = self.plan([["c3"], ["c1"], ["c2"]])
        self.assertEqual(result["node_ids"], ["c1", "c2", "c3"])
        self.assertEqual(result["repeats"], 0)
        self.assertEqual(result["minutes"], result["direct_minutes"])
        self.assertGreater(result["as_named"]["minutes"], result["minutes"])

    def test_порядок_диспетчера_не_меняется(self):
        result = self.plan([["c3"], ["c1"]], keep_order=True)
        self.assertEqual(result["node_ids"], ["c3", "c1"])
        self.assertGreater(result["repeats"], 0)

    def test_из_любых_берётся_по_пути(self):
        # Обратно за c0 ехать не нужно — годится c2.
        self.assertEqual(self.plan([["c3", "c2"], ["c2"]])["node_ids"], ["c2", "c3"])

    def test_борт_повторы_и_пройденное_убираются(self):
        call = make_call(waypoints=[{"node_id": "c1", "done_at": "2026-01-01T00:00:00Z"}])
        result = self.plan([["c4"], ["c2"], ["c2"], ["c1"]], call=call)
        self.assertEqual(result["node_ids"], ["c2"])
        self.assertEqual(len(result["skipped"]), 1)

    def test_несуществующая_точка_и_перебор(self):
        with self.assertRaises(HTTPException):
            self.plan([["нет"]])
        with self.assertRaises(HTTPException):
            waypoint_plan.orders_of([[str(i)] for i in range(8)], False)

    def test_ехать_некому(self):
        empty = SimpleNamespace(candidates=[])
        with mock.patch("services.waypoint_plan.free_vehicles", return_value=[]):
            result = waypoint_plan.plan(None, make_call(), self.graph, [["c2"]], False, lambda: empty)
        self.assertEqual(result["node_ids"], ["c2"])
        self.assertIsNone(result["minutes"])

    def test_только_чтение_времени_совпадает_с_назначением(self):
        result = self.plan([["c2"], ["c1"]])
        plan = plan_for(self.graph, make_engineer(1, node_index=0), "c4", [], waypoints=result["node_ids"])
        self.assertAlmostEqual(plan.minutes, result["minutes"], places=1)

    def test_склад_не_обязан_быть_первым(self):
        # Точка c1, склад c3: сначала c1, потом склад — без возврата.
        plan = plan_for(self.graph, make_engineer(1, node_index=0), "c4", [], depots=["c3"], waypoints=["c1"])
        self.assertEqual(plan.node_ids, ["c0", "c1", "c2", "c3", "c4"])


class TestAvoid(unittest.TestCase):
    """При равном времени путь не повторяет пройденное, но и не удлиняется."""

    def graph(self, long_way):
        edges = {"A": [("B", 100), ("C", 100)], "B": [("A", 100), ("D", 100)],
                 "C": [("A", 100), ("D", long_way)], "D": [("B", 100), ("C", long_way)]}
        return SimpleNamespace(nodes=set(edges), links=lambda node, mode: [(n, m, None) for n, m in edges[node]])

    def test_равные_пути(self):
        self.assertEqual(shortest_path(self.graph(100), "A", "D", MODE_WALK, None, {"B"}).node_ids, ["A", "C", "D"])
        self.assertEqual(shortest_path(self.graph(100), "A", "D", MODE_WALK, None, {"C"}).node_ids, ["A", "B", "D"])

    def test_время_важнее_повторов(self):
        self.assertEqual(shortest_path(self.graph(150), "A", "D", MODE_WALK, None, {"B"}).node_ids, ["A", "B", "D"])
