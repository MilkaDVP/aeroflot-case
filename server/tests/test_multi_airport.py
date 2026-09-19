"""
Тесты второго аэропорта: выбор контекста и разделение данных.

Аэропортов два не для красоты: §5 требует шага выбора аэропорта, а при
единственном аэропорте этот экран никогда не показывается. Здесь же
проверяется главное правило §5 — данные разных аэропортов не смешиваются.

Домодедово: пешком в регламент попадают 2 стоянки из 73, поэтому
спецтранспорт решает всё — это отдельный, непохожий на Шереметьево
контрольный случай для алгоритма.
"""

import unittest

from constants import AIRPORTS_DIR
from tests.test_api import ApiTestCase, auth_header

HAS_DME = (AIRPORTS_DIR / "UUDD.json").exists()

DME_BOARDS = {"VQ-BDU", "VP-BWW", "RA-89012", "VQ-BTS"}
SVO_BOARDS = {"VP-BZQ", "VP-BES", "RA-89001", "VP-BKB", "VP-BGB", "VQ-BQX"}


@unittest.skipUnless(HAS_DME, "нет data/airports/UUDD.json — выполните выгрузку")
class TestMultiAirport(ApiTestCase):
    """Диспетчер работает то в одном аэропорту, то в другом."""

    def setUp(self):
        self.reset_database()

    def in_airport(self, icao, login="dispatcher"):
        """Вход и выбор аэропорта как контекста сессии."""
        return self.login(login, login) if icao == "UUEE" else self.login_to(icao, login)

    def login_to(self, icao, login):
        response = self.client.post(
            "/api/auth/login", json={"login": login, "password": login}
        )
        token = response.json()["token"]
        chosen = self.client.post(
            "/api/session/airport",
            json={"airport_icao": icao, "shift": "day"},
            headers=auth_header(token),
        )
        self.assertEqual(chosen.status_code, 200, chosen.text)
        return token

    def get(self, token, path):
        response = self.client.get(path, headers=auth_header(token))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # --- Контекст ---

    def test_диспетчер_видит_оба_аэропорта(self):
        token = self.login_to("UUDD", "dispatcher")
        me = self.get(token, "/api/auth/me")
        self.assertEqual({a["icao"] for a in me["airports"]}, {"UUEE", "UUDD"})
        self.assertEqual(me["airport_icao"], "UUDD")

    def test_граф_второго_аэропорта_отдаётся(self):
        token = self.login_to("UUDD", "dispatcher")
        graph = self.get(token, "/api/airports/UUDD")
        self.assertGreater(len(graph["nodes"]), 500)
        tech = [node for node in graph["nodes"] if node["type"] == "tech_center"]
        self.assertEqual(len(tech), 2)

    # --- Разделение данных ---

    def test_сотрудники_не_смешиваются(self):
        dme = self.get(self.login_to("UUDD", "dispatcher"), "/api/employees")
        svo = self.get(self.login("dispatcher", "dispatcher"), "/api/employees")

        self.assertTrue(dme)
        self.assertTrue(all(item["airport_icao"] == "UUDD" for item in dme))
        self.assertTrue(all(item["airport_icao"] == "UUEE" for item in svo))
        self.assertIn("Беляев С. Н.", [item["full_name"] for item in dme])
        self.assertNotIn("Соколов А. В.", [item["full_name"] for item in dme])

    def test_борта_и_машины_не_смешиваются(self):
        token = self.login_to("UUDD", "dispatcher")
        boards = {item["board_number"] for item in self.get(token, "/api/aircraft")}
        vehicles = {item["call_sign"] for item in self.get(token, "/api/vehicles")}

        self.assertEqual(boards, DME_BOARDS)
        self.assertFalse(boards & SVO_BOARDS)
        self.assertEqual(vehicles, {"ДМ-01", "ДМ-02"})

    def test_вызовы_не_смешиваются(self):
        dme_boards = {
            call["board_number"]
            for call in self.get(self.login_to("UUDD", "dispatcher"), "/api/calls")
        }
        self.assertTrue(dme_boards)
        self.assertFalse(dme_boards & SVO_BOARDS)

    def test_чужой_вызов_недоступен_по_идентификатору(self):
        # Подстановка чужого идентификатора не должна давать доступ
        # к данным другого аэропорта.
        svo_token = self.login("dispatcher", "dispatcher")
        svo_call = self.get(svo_token, "/api/calls")[0]

        dme_token = self.login_to("UUDD", "dispatcher")
        response = self.client.post(
            f"/api/calls/{svo_call['id']}/suggest", headers=auth_header(dme_token)
        )
        self.assertEqual(response.status_code, 404, response.text)

    # --- Алгоритм на втором графе ---

    def test_подбор_в_домодедово_идёт_через_машины(self):
        token = self.login_to("UUDD", "dispatcher")
        call = next(
            item for item in self.get(token, "/api/calls") if item["board_number"] == "VQ-BDU"
        )
        result = self.client.post(
            f"/api/calls/{call['id']}/suggest", headers=auth_header(token)
        ).json()

        by_name = {item["full_name"]: item for item in result["candidates"]}
        self.assertIn("Беляев С. Н.", by_name)
        self.assertIn("Гончаров П. И.", by_name)

        # Беляев едет на своей машине, Гончаров доходит до свободной.
        self.assertEqual(by_name["Беляев С. Н."]["vehicle_call_sign"], "ДМ-01")
        self.assertFalse(by_name["Беляев С. Н."]["pickup"])
        self.assertTrue(by_name["Гончаров П. И."]["pickup"])
        self.assertEqual(by_name["Гончаров П. И."]["vehicle_call_sign"], "ДМ-02")

        # Пешком до этой стоянки больше получаса — без машин регламент
        # не выполнить, а с ними выполняется.
        self.assertTrue(result["within_regulation"])
        self.assertLess(by_name["Гончаров П. И."]["minutes"], 15)


if __name__ == "__main__":
    unittest.main()
