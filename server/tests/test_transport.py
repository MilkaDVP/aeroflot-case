"""
Тесты выбора способа добраться: пешком, на своей машине, через свободную.

Граф-линейка из test_dispatch, где ответ считается в уме:

    c0 ──300── c1 ──300── c2 ──300── c3 ──300── c4 (стоянка)

Пешком 5 км/ч (регламент): 300 м — 3.6 мин. На машине 20 км/ч: 300 м — 0.9 мин.
Посадка в машину и высадка — по 10 с.
"""

import unittest

from algorithm.dispatch import suggest
from algorithm.transport import plan_for
from constants import MODE_PICKUP, MODE_VEHICLE, MODE_WALK, SHIFT_DAY
from tests.test_dispatch import (
    BASE_LAT,
    BASE_LON,
    CALL_A320,
    SPACING_M,
    TODAY,
    make_chain_airport,
    make_engineer,
    meters_per_degree_lon,
)

STAND = "c4"


def parked_vehicle(vehicle_id, node_index):
    """Свободная машина, стоящая ровно в узле линейки."""
    step_deg = SPACING_M / meters_per_degree_lon(BASE_LAT)
    return {
        "id": vehicle_id,
        "call_sign": f"ТМ-{vehicle_id}",
        "lat": BASE_LAT,
        "lon": BASE_LON + node_index * step_deg,
        "speed_kmh": None,
    }


class TestPlan(unittest.TestCase):
    """Выбор способа для одного сотрудника."""

    def setUp(self):
        self.graph = make_chain_airport()

    def test_без_машин_весь_путь_пешком(self):
        plan = plan_for(self.graph, make_engineer(1, node_index=0), STAND, [])
        self.assertEqual(plan.mode, MODE_WALK)
        self.assertEqual(len(plan.legs), 1)
        self.assertAlmostEqual(plan.minutes, 14.4, places=1)

    def test_свободная_машина_рядом_выигрывает_у_ходьбы(self):
        # Пешком 1200 м — 14.4 мин. До машины в c1 — 300 м пешком (3.6),
        # посадка 10 с, 900 м на машине (2.7), высадка 10 с: 6.63 мин.
        plan = plan_for(
            self.graph, make_engineer(1, node_index=0), STAND, [parked_vehicle(7, 1)]
        )
        self.assertTrue(plan.pickup)
        self.assertEqual(plan.mode, MODE_PICKUP)
        self.assertEqual(plan.vehicle["call_sign"], "ТМ-7")
        self.assertEqual(plan.pickup_node_id, "c1")
        self.assertEqual([leg.mode for leg in plan.legs], [MODE_WALK, MODE_VEHICLE])
        self.assertAlmostEqual(plan.minutes, 6.63, places=2)
        # Посадка и высадка — остановки маршрута.
        self.assertEqual([stop["kind"] for stop in plan.stops], ["board", "alight"])
        # Узел пересадки не повторяется в общем маршруте.
        self.assertEqual(plan.node_ids, ["c0", "c1", "c2", "c3", "c4"])

    def test_машина_далеко_не_помогает(self):
        # Машина у самой стоянки: идти до неё столько же, сколько до борта.
        # При равенстве система не занимает машину — идём пешком.
        plan = plan_for(
            self.graph, make_engineer(1, node_index=0), STAND, [parked_vehicle(7, 4)]
        )
        self.assertFalse(plan.pickup)
        self.assertEqual(plan.mode, MODE_WALK)

    def test_выбирается_ближайшая_по_времени_машина(self):
        vehicles = [parked_vehicle(7, 3), parked_vehicle(8, 1)]
        plan = plan_for(self.graph, make_engineer(1, node_index=0), STAND, vehicles)
        # Через c1: 3.6 + 2.7 + 20 с = 6.6; через c3: 10.8 + 0.9 + 20 с = 12.0.
        self.assertEqual(plan.vehicle["call_sign"], "ТМ-8")

    def test_своя_машина_без_пешего_участка(self):
        plan = plan_for(
            self.graph,
            make_engineer(1, node_index=0, has_vehicle=True),
            STAND,
            [parked_vehicle(7, 1)],
        )
        self.assertFalse(plan.pickup)
        self.assertEqual(plan.mode, MODE_VEHICLE)
        self.assertEqual(len(plan.legs), 1)
        # 1200 м на машине — 3.6 мин, плюс высадка у борта 10 с.
        self.assertAlmostEqual(plan.minutes, 3.6 + 10 / 60, places=2)


class TestSuggestWithVehicles(unittest.TestCase):
    """Машины парка в подборе."""

    def setUp(self):
        self.graph = make_chain_airport()

    def test_пешему_предлагают_дойти_до_машины(self):
        walker = make_engineer(1, node_index=0)
        result = suggest(
            self.graph, CALL_A320, [walker], SHIFT_DAY, TODAY, vehicles=[parked_vehicle(7, 1)]
        )
        best = result.best.as_dict()
        self.assertTrue(best["pickup"])
        self.assertEqual(best["vehicle_call_sign"], "ТМ-7")
        self.assertEqual(len(best["route"]["legs"]), 2)
        # Пешком 14.4 мин — впритык; с машиной — 6.6 мин.
        self.assertTrue(result.within_regulation)

    def test_машина_меняет_порядок_кандидатов(self):
        # Пешком ближе второй (c2, 7.2 мин), но первому (c0) рядом машина:
        # 6.6 мин. Без учёта машин система выбрала бы не того.
        far = make_engineer(1, node_index=0)
        near = make_engineer(2, node_index=2)
        result = suggest(
            self.graph, CALL_A320, [far, near], SHIFT_DAY, TODAY, vehicles=[parked_vehicle(7, 1)]
        )
        self.assertEqual(result.best.employee["id"], 1)

    def test_без_списка_машин_поведение_прежнее(self):
        result = suggest(self.graph, CALL_A320, [make_engineer(1, node_index=2)], SHIFT_DAY, TODAY)
        self.assertFalse(result.best.as_dict()["pickup"])
        self.assertAlmostEqual(result.best.minutes, 7.2, places=1)


if __name__ == "__main__":
    unittest.main()
