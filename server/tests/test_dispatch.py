"""
Контрольные сценарии подбора исполнителя.

Пять сценариев из требований конкурсного задания плюс проверка инварианта
и ограничения по времени расчёта. Сценарии считаются на графе-линейке,
где правильный ответ известен заранее и проверяется в уме:

    c0 ──300── c1 ──300── c2 ──300── c3 ──300── c4
   стоянка 1                                 стоянка 10

Расстояния до стоянки 10 и время пешком при 4.5 км/ч:
    c0 — 1200 м — 16.0 мин   (регламент нарушен)
    c1 —  900 м — 12.0 мин   (жёлтая зона)
    c2 —  600 м —  8.0 мин
    c3 —  300 м —  4.0 мин
На спецтранспорте при 20 км/ч те же 1200 м — 3.6 мин.
"""

import math
import random
import time
import unittest
from datetime import date

from algorithm.dispatch import suggest
from algorithm.graph import AirportGraph, load_airport
from constants import (
    AIRPORTS_DIR,
    REGULATION_ARRIVAL_LIMIT_MIN,
    SHIFT_DAY,
    SHIFT_NIGHT,
    STATUS_EN_ROUTE,
    STATUS_FREE,
    SUGGESTION_TIMEOUT_S,
)

TODAY = date(2026, 9, 3)
BASE_LAT = 55.97
BASE_LON = 37.40
SPACING_M = 300.0
CHAIN_SIZE = 5

# Вызов по умолчанию: утечка гидрожидкости на A320 требует отметки B1.1.
CALL_A320 = {"aircraft_type": "A320", "defect_code": "hydraulic_leak", "stand_node_id": "c4"}


def meters_per_degree_lon(lat):
    """Длина градуса долготы на данной широте, в метрах."""
    return math.radians(1.0) * 6371008.8 * math.cos(math.radians(lat))


def make_chain_airport():
    """Граф-линейка из пяти узлов с шагом 300 м, стоянки по краям."""
    step_deg = SPACING_M / meters_per_degree_lon(BASE_LAT)
    nodes = []
    edges = []

    for index in range(CHAIN_SIZE):
        is_stand = index in (0, CHAIN_SIZE - 1)
        nodes.append(
            {
                "id": f"c{index}",
                "type": "stand" if is_stand else "junction",
                "ref": {0: "1", CHAIN_SIZE - 1: "10"}.get(index),
                "lat": BASE_LAT,
                "lon": BASE_LON + index * step_deg,
                "x": index * SPACING_M,
                "y": 0.0,
            }
        )
        if index > 0:
            edges.append(
                {
                    "from_id": f"c{index - 1}",
                    "to_id": f"c{index}",
                    "distance_m": SPACING_M,
                    "vehicle_allowed": True,
                }
            )

    return AirportGraph(
        {
            "icao": "TEST",
            "name": "Тестовый",
            "city": "—",
            "ref_point": {"lat": BASE_LAT, "lon": BASE_LON},
            "nodes": nodes,
            "edges": edges,
        }
    )


def make_engineer(
    employee_id,
    node_index,
    category="B1.1",
    aircraft_types=("A320",),
    valid_until="2027-12-31",
    status=STATUS_FREE,
    shift=SHIFT_DAY,
    has_vehicle=False,
    busy_until=None,
):
    """Сотрудник, стоящий ровно в узле графа — чтобы подход был нулевым."""
    step_deg = SPACING_M / meters_per_degree_lon(BASE_LAT)
    return {
        "id": employee_id,
        "full_name": f"Инженер №{employee_id}",
        "shift": shift,
        "status": status,
        "lat": BASE_LAT,
        "lon": BASE_LON + node_index * step_deg,
        # Своя машина — машина парка, которая уже у сотрудника.
        "vehicle": own_vehicle(employee_id) if has_vehicle else None,
        "busy_until": busy_until,
        "qualifications": [
            {
                "category": category,
                "aircraft_types": list(aircraft_types),
                "valid_until": valid_until,
            }
        ],
    }


def own_vehicle(employee_id):
    """Машина парка, которая уже у сотрудника: едет с ним, координаты его."""
    return {"id": 900 + employee_id, "call_sign": f"ТМ-{employee_id}", "speed_kmh": None}


class TestScenarios(unittest.TestCase):
    """Контрольные сценарии из требований задания."""

    def setUp(self):
        self.graph = make_chain_airport()

    def test_сценарий_1_разное_расположение_побеждает_ближайший(self):
        far = make_engineer(1, node_index=0)
        near = make_engineer(2, node_index=2)

        result = suggest(self.graph, CALL_A320, [far, near], SHIFT_DAY, TODAY)

        self.assertEqual(result.best.employee["id"], 2)
        self.assertAlmostEqual(result.best.minutes, 8.0, places=1)
        self.assertTrue(result.within_regulation)

    def test_сценарий_2_ближайший_без_допуска_отсеивается(self):
        # Вызов на B777. Ближайший инженер имеет допуск только на A320 —
        # назначить его нельзя, хотя он в четырёх минутах.
        call = dict(CALL_A320, aircraft_type="B777")
        near_wrong_type = make_engineer(1, node_index=3, aircraft_types=("A320",))
        far_correct = make_engineer(2, node_index=1, aircraft_types=("B777",))

        result = suggest(self.graph, call, [near_wrong_type, far_correct], SHIFT_DAY, TODAY)

        self.assertEqual(result.best.employee["id"], 2)
        self.assertAlmostEqual(result.best.minutes, 12.0, places=1)
        # Диспетчер должен видеть, почему ближайший не назначен.
        reasons = [item["reason"] for item in result.rejected if item["employee_id"] == 1]
        self.assertEqual(len(reasons), 1)
        self.assertIn("B777", reasons[0])

    def test_сценарий_3_спецтранспорт_обгоняет_пешехода(self):
        # Пешеход стоит вчетверо ближе, но приедет позже машины.
        walker = make_engineer(1, node_index=3, has_vehicle=False)
        driver = make_engineer(2, node_index=0, has_vehicle=True)

        result = suggest(self.graph, CALL_A320, [walker, driver], SHIFT_DAY, TODAY)

        self.assertEqual(result.best.employee["id"], 2)
        self.assertAlmostEqual(result.best.minutes, 3.6, places=1)

    def test_сценарий_4_никто_не_успевает_в_регламент(self):
        # Единственный подходящий сотрудник идёт 16 минут. Система обязана
        # его показать с явным предупреждением, а не промолчать.
        lonely = make_engineer(1, node_index=0)

        result = suggest(self.graph, CALL_A320, [lonely], SHIFT_DAY, TODAY)

        self.assertIsNotNone(result.best)
        self.assertAlmostEqual(result.best.minutes, 16.0, places=1)
        self.assertFalse(result.within_regulation)
        self.assertFalse(result.best.within_regulation)
        self.assertIn("превышение регламента", result.message)

    def test_сценарий_5_все_с_допуском_заняты(self):
        # Свободных с допуском нет. Диспетчер получает список занятых
        # и время их освобождения — это его единственный рабочий вариант.
        busy_engineer = make_engineer(
            1, node_index=2, status=STATUS_EN_ROUTE, busy_until="2026-09-03T10:40:00"
        )
        free_but_unqualified = make_engineer(2, node_index=3, category="B2")

        result = suggest(
            self.graph, CALL_A320, [busy_engineer, free_but_unqualified], SHIFT_DAY, TODAY
        )

        self.assertIsNone(result.best)
        self.assertEqual(result.candidates, [])
        self.assertEqual(len(result.busy), 1)
        self.assertEqual(result.busy[0]["employee_id"], 1)
        self.assertEqual(result.busy[0]["busy_until"], "2026-09-03T10:40:00")
        self.assertIn("A320", result.message)

    def test_сценарий_6_несколько_вызовов_подряд(self):
        # Первый вызов забирает ближайшего. На втором вызове он уже занят,
        # и система обязана предложить следующего, а не того же самого.
        first = make_engineer(1, node_index=3)
        second = make_engineer(2, node_index=1)

        result_one = suggest(self.graph, CALL_A320, [first, second], SHIFT_DAY, TODAY)
        self.assertEqual(result_one.best.employee["id"], 1)

        # Назначение произошло: статус первого меняется на en_route.
        first["status"] = STATUS_EN_ROUTE
        call_two = dict(CALL_A320, stand_node_id="c0")
        result_two = suggest(self.graph, call_two, [first, second], SHIFT_DAY, TODAY)

        self.assertEqual(result_two.best.employee["id"], 2)
        self.assertEqual([item["employee_id"] for item in result_two.busy], [1])

    def test_смена_отсекает_кандидата(self):
        # Сотрудник ночной смены не рассматривается днём, даже стоя вплотную
        # к борту и с идеальным допуском.
        night = make_engineer(1, node_index=3, shift=SHIFT_NIGHT)
        day = make_engineer(2, node_index=0, shift=SHIFT_DAY)

        result = suggest(self.graph, CALL_A320, [night, day], SHIFT_DAY, TODAY)

        self.assertEqual(result.best.employee["id"], 2)
        self.assertIn(1, [item["employee_id"] for item in result.rejected])

    def test_жёлтая_зона_отмечается(self):
        # 12 минут — успевает, но запаса почти нет. Для диспетчера это
        # отдельный визуальный сигнал, а не просто «уложился».
        engineer = make_engineer(1, node_index=1)
        result = suggest(self.graph, CALL_A320, [engineer], SHIFT_DAY, TODAY)

        self.assertTrue(result.best.within_regulation)
        self.assertTrue(result.best.near_limit)


class TestInvariant(unittest.TestCase):
    """
    Инвариант из технического задания.

    Среди кандидатов, прошедших фильтры допуска и доступности, система
    никогда не назначает сотрудника, который будет в пути дольше другого
    доступного кандидата. Проверяется перебором случайных расстановок
    с фиксированным зерном — тест повторяем.
    """

    def test_назначенный_всегда_минимален_по_времени(self):
        graph = make_chain_airport()
        generator = random.Random(20260903)

        for _ in range(200):
            employees = []
            for employee_id in range(1, generator.randint(2, 8)):
                employees.append(
                    make_engineer(
                        employee_id,
                        node_index=generator.randint(0, CHAIN_SIZE - 1),
                        has_vehicle=generator.random() < 0.3,
                        status=generator.choice([STATUS_FREE, STATUS_FREE, STATUS_EN_ROUTE]),
                        aircraft_types=generator.choice([("A320",), ("A320", "B777"), ("B777",)]),
                    )
                )

            result = suggest(graph, CALL_A320, employees, SHIFT_DAY, TODAY)
            if result.best is None:
                self.assertEqual(result.candidates, [])
                continue

            fastest = min(candidate.minutes for candidate in result.candidates)
            self.assertAlmostEqual(result.best.minutes, fastest, places=6)


class TestPerformanceOnRealAirport(unittest.TestCase):
    """Ограничение задания: подбор не дольше 10 секунд."""

    @classmethod
    def setUpClass(cls):
        if not (AIRPORTS_DIR / "UUEE.json").exists():
            raise unittest.SkipTest("нет data/airports/UUEE.json — выполните выгрузку")
        cls.graph = load_airport("UUEE")

    def test_подбор_среди_пятидесяти_сотрудников_на_реальном_графе(self):
        stands = self.graph.nodes_of_type("stand")
        generator = random.Random(1)
        employees = []

        for employee_id in range(1, 51):
            position = generator.choice(stands)
            employees.append(
                {
                    "id": employee_id,
                    "full_name": f"Инженер №{employee_id}",
                    "shift": SHIFT_DAY,
                    "status": STATUS_FREE,
                    "lat": position["lat"],
                    "lon": position["lon"],
                    "vehicle": own_vehicle(employee_id) if employee_id % 3 == 0 else None,
                    "qualifications": [
                        {
                            "category": "B1.1",
                            "aircraft_types": ["A320"],
                            "valid_until": "2027-12-31",
                        }
                    ],
                }
            )

        call = dict(CALL_A320, stand_node_id=stands[0]["id"])
        started = time.perf_counter()
        result = suggest(self.graph, call, employees, SHIFT_DAY, TODAY)
        elapsed_s = time.perf_counter() - started

        self.assertIsNotNone(result.best)
        self.assertLess(elapsed_s, SUGGESTION_TIMEOUT_S)
        self.assertEqual(len(result.candidates), 50)
        # Кандидаты отсортированы по времени в пути.
        minutes = [candidate.minutes for candidate in result.candidates]
        self.assertEqual(minutes, sorted(minutes))
        # Хоть кто-то обязан укладываться в регламент: 50 инженеров
        # на перроне — это заведомо достаточная плотность.
        self.assertLessEqual(result.best.minutes, REGULATION_ARRIVAL_LIMIT_MIN)


if __name__ == "__main__":
    unittest.main()
