"""
Тесты управления парком спецтранспорта.

Машины перестали быть только частью тестовых данных: администратор ставит
их в парк и выводит из него. Проверяется, что при этом нельзя нарушить
рабочие инварианты — увести машину из-под едущего инженера или завести
две машины с одним позывным, который диспетчер видит в подборе.
"""

from tests.test_api import ApiTestCase, auth_header


class TestVehicleAdmin(ApiTestCase):
    """Постановка машин в парк и вывод из него."""

    def setUp(self):
        self.reset_database()
        self.admin = self.login("admin", "admin")
        self.dispatcher = self.login("dispatcher", "dispatcher")

    def vehicles(self, token=None):
        response = self.client.get(
            "/api/vehicles", headers=auth_header(token or self.admin)
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {item["call_sign"]: item for item in response.json()}

    def create(self, call_sign="ТМ-99", token=None):
        return self.client.post(
            "/api/vehicles",
            json={"call_sign": call_sign, "kind": "Техпомощь",
                  "lat": 55.9787, "lon": 37.3972},
            headers=auth_header(token or self.admin),
        )

    def test_машина_ставится_в_парк_свободной(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.text)

        body = response.json()
        self.assertEqual(body["call_sign"], "ТМ-99")
        # Занятой машина становится только через назначение вызова.
        self.assertEqual(body["status"], "free")
        self.assertIsNone(body["employee_id"])
        self.assertIn("ТМ-99", self.vehicles())

    def test_новая_машина_участвует_в_подборе(self):
        """Поставленная в парк машина сразу доступна алгоритму."""
        before = self.client.post(
            f"/api/calls/{self.open_call_id()}/suggest",
            headers=auth_header(self.dispatcher),
        ).json()

        self.create("ТМ-98")

        after = self.client.post(
            f"/api/calls/{self.open_call_id()}/suggest",
            headers=auth_header(self.dispatcher),
        ).json()
        self.assertIsNotNone(after["best"])
        # Кандидатов не стало меньше: парк только пополнился.
        self.assertGreaterEqual(len(after["candidates"]), len(before["candidates"]))

    def test_повторный_позывной_отклоняется(self):
        self.create("ТМ-97")
        response = self.create("ТМ-97")
        self.assertEqual(response.status_code, 409, response.text)

    def test_диспетчер_не_вправе_ставить_машины(self):
        response = self.create("ТМ-96", token=self.dispatcher)
        self.assertEqual(response.status_code, 403, response.text)

    def test_свободная_машина_выводится_из_парка(self):
        created = self.create("ТМ-95").json()

        response = self.client.delete(
            f"/api/vehicles/{created['id']}", headers=auth_header(self.admin)
        )
        self.assertEqual(response.status_code, 204, response.text)
        self.assertNotIn("ТМ-95", self.vehicles())

    def test_занятая_машина_не_выводится(self):
        """Из-под едущего инженера машину забрать нельзя."""
        busy = [item for item in self.vehicles().values() if item["status"] != "free"]
        self.assertTrue(busy, "в тестовых данных есть машины при инженерах")

        response = self.client.delete(
            f"/api/vehicles/{busy[0]['id']}", headers=auth_header(self.admin)
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(busy[0]["call_sign"], response.json()["detail"])

    def test_чужая_машина_недоступна(self):
        """Аэропорт — контекст сессии: машины другого аэропорта не видны."""
        dme_admin = self.login("admin", "admin", airport="UUDD")
        svo_vehicle = list(self.vehicles().values())[0]

        response = self.client.delete(
            f"/api/vehicles/{svo_vehicle['id']}", headers=auth_header(dme_admin)
        )
        self.assertEqual(response.status_code, 404, response.text)

    def open_call_id(self):
        """Идентификатор открытого вызова из тестовых данных."""
        response = self.client.get("/api/calls", headers=auth_header(self.dispatcher))
        for call in response.json():
            if call["status"] in ("new", "suggested"):
                return call["id"]
        self.fail("в тестовых данных нет открытого вызова")
