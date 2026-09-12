"""
Тесты контрольных сценариев из §10 задания.

Отчёт `docs/scenarios.md` создаётся скриптом `server/scenarios.py` и потому
может незаметно разойтись с алгоритмом: кто-то поправит подбор — таблица
в репозитории останется прежней. Здесь проверяется не текст отчёта,
а утверждения, на которых он держится: сценарии действительно дают тот
результат, который в нём напечатан.

База наполняется тем же seed.py, что и стенд, поэтому тесты работают
на настоящих графах аэропортов.
"""

import unittest

import scenarios
from constants import AIRPORTS_DIR, REGULATION_ARRIVAL_LIMIT_MIN
from database import SessionLocal
from tests.test_api import ApiTestCase

HAS_DME = (AIRPORTS_DIR / "UUDD.json").exists()


class TestScenarioReport(ApiTestCase):
    """Каждый сценарий отчёта проверяется отдельным утверждением."""

    def setUp(self):
        self.reset_database()
        self.db = SessionLocal()
        self.rows = {}
        for scenario in scenarios.SCENARIOS:
            if scenario["airport"] == "UUDD" and not HAS_DME:
                continue
            self.rows[scenario["title"]] = scenarios.run_scenario(self.db, scenario)

    def tearDown(self):
        self.db.close()

    def test_scenario_count(self):
        """§10 требует не менее пяти контрольных сценариев."""
        self.assertGreaterEqual(len(scenarios.SCENARIOS), 5)

    def test_calculation_within_time_limit(self):
        """§10: расчёт должен укладываться в 10 секунд."""
        for title, row in self.rows.items():
            with self.subTest(title=title):
                self.assertLess(row["elapsed_ms"], 10_000)

    def test_nearest_by_straight_line_is_not_eligible(self):
        """
        Главное утверждение отчёта: ближайший по прямой часто недопустим.

        Именно на этом строится сравнение с интуитивным выбором диспетчера,
        поэтому сценарии обязаны такие случаи содержать.
        """
        illegal = [
            row
            for row in self.rows.values()
            if row["intuitive"] is not None and not row["intuitive"]["legal"]
        ]
        self.assertGreaterEqual(len(illegal), 5)

    def test_qualified_candidate_chosen_over_nearest(self):
        """Сценарий 1: система берёт дальнего с допуском, а не ближнего без."""
        row = self.rows["Разное расположение и допуск"]
        best = row["result"].best

        self.assertIsNotNone(best)
        self.assertEqual(best.employee["full_name"], "Соколов А. В.")
        self.assertTrue(best.within_regulation)
        # Ближайший по прямой стоит у самого борта — и всё равно не годится.
        self.assertEqual(row["intuitive"]["employee"]["full_name"], "Волков И. Н.")
        self.assertFalse(row["intuitive"]["legal"])

    def test_pickup_vehicle_keeps_regulation(self):
        """Сценарий 2: пеший укладывается в регламент, забрав машину парка."""
        row = self.rows["Спецтранспорт: ближайший с машиной занят"]
        best = row["result"].best

        self.assertIsNotNone(best)
        self.assertTrue(best.route.pickup, "маршрут должен идти через машину парка")
        self.assertEqual(best.route.vehicle["call_sign"], "ТМ-04")
        self.assertTrue(best.within_regulation)
        # Без машины тот же человек не успел бы: в этом и смысл сценария.
        self.assertGreater(best.route.legs[0].minutes, 0)

    def test_avionics_requires_b2(self):
        """Сценарий 4: механики с B1 не подходят для работ по авионике."""
        row = self.rows["Авионика: нужна отметка B2"]

        self.assertEqual(row["mark"], "B2")
        for candidate in row["result"].candidates:
            with self.subTest(name=candidate.employee["full_name"]):
                self.assertEqual(candidate.qualification["category"], "B2")

    def test_regulation_excess_is_reported(self):
        """Сценарий 5: превышение регламента показывается, а не скрывается."""
        row = self.rows["Другой тип ВС: B777"]
        best = row["result"].best

        self.assertIsNotNone(best, "кандидат есть, хоть и не успевает")
        self.assertGreater(best.minutes, REGULATION_ARRIVAL_LIMIT_MIN)
        self.assertFalse(row["result"].within_regulation)

    def test_no_candidates_is_explained(self):
        """Сценарий 6: без допуска система молчать не должна."""
        row = self.rows["Внештатная: допуска нет ни у кого"]

        self.assertIsNone(row["result"].best)
        self.assertTrue(row["result"].message)
        self.assertTrue(row["result"].rejected, "причины отказа обязаны быть")

    def test_other_shift_is_not_considered(self):
        """Сценарий 7: сотрудники чужой смены не рассматриваются."""
        row = self.rows["Ночная смена"]

        self.assertIsNone(row["result"].best)
        reasons = {item["reason"] for item in row["result"].rejected}
        self.assertIn("не на этой смене", reasons)

    @unittest.skipUnless(HAS_DME, "нет data/airports/UUDD.json — выполните выгрузку")
    def test_domodedovo_needs_vehicle(self):
        """Сценарий 8: в Домодедово дальняя стоянка достижима только на машине."""
        row = self.rows["Домодедово: дальняя стоянка"]
        best = row["result"].best

        self.assertIsNotNone(best)
        self.assertIsNotNone(best.route.vehicle)
        self.assertTrue(best.within_regulation)

    def test_report_renders(self):
        """Отчёт собирается целиком: в нём есть таблица, разбор и выводы."""
        report = scenarios.render(list(self.rows.values()))

        self.assertIn("| № | Сценарий |", report)
        self.assertIn("## Разбор сценария 1", report)
        self.assertIn("## Практическая оптимизация", report)


if __name__ == "__main__":
    unittest.main()
