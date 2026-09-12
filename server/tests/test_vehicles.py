"""
Тесты парка машин через API.

Демонстрационные данные: ТМ-01 у Соколова, ТМ-02 у Никитина, ТМ-03
у Фомина, ТМ-04 свободна в 326 м от ТЦ-Запад, ТМ-05 свободна у стоянки 60.
Морозов в ТЦ-Запад без машины.
"""

from database import SessionLocal
from services.assignment import free_vehicles
from tests.test_api import ApiTestCase, auth_header

SOKOLOV = "Соколов А. В."
MOROZOV = "Морозов Д. С."


class TestVehiclePool(ApiTestCase):
    def setUp(self):
        self.reset_database()
        self.supervisor = self.login("supervisor", "supervisor")

    # --- Вспомогательное ---

    def vehicles(self):
        response = self.client.get("/api/vehicles", headers=auth_header(self.supervisor))
        self.assertEqual(response.status_code, 200, response.text)
        return {item["call_sign"]: item for item in response.json()}

    def employee(self, full_name):
        employees = self.client.get("/api/employees", headers=auth_header(self.supervisor)).json()
        return next(item for item in employees if item["full_name"] == full_name)

    def create_call(self, board_number):
        response = self.client.post(
            "/api/calls",
            json={
                "aircraft_id": self.board_id(self.supervisor, board_number),
                "defect_code": "hydraulic_leak",
            },
            headers=auth_header(self.supervisor),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def assign(self, call_id, full_name):
        response = self.client.post(
            f"/api/calls/{call_id}/assign",
            json={"employee_id": self.employee(full_name)["id"], "override_reason": "сценарий"},
            headers=auth_header(self.supervisor),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def stand_point(self, stand_node_id):
        graph = self.client.get("/api/airports/UUEE", headers=auth_header(self.supervisor)).json()
        node = next(item for item in graph["nodes"] if item["id"] == stand_node_id)
        return node["lat"], node["lon"]

    # --- Парк ---

    def test_парк_машин_аэропорта(self):
        vehicles = self.vehicles()
        self.assertEqual(sorted(vehicles), ["ТМ-01", "ТМ-02", "ТМ-03", "ТМ-04", "ТМ-05"])
        self.assertEqual(vehicles["ТМ-01"]["status"], "in_use")
        self.assertEqual(vehicles["ТМ-01"]["employee_name"], SOKOLOV)
        self.assertEqual(vehicles["ТМ-04"]["status"], "free")

    def test_машина_при_инженере_там_же_где_он(self):
        # Отдельные координаты машины отставали бы от его GPS.
        sokolov = self.employee(SOKOLOV)
        tm01 = self.vehicles()["ТМ-01"]
        self.assertAlmostEqual(tm01["lat"], sokolov["lat"], places=6)
        self.assertTrue(sokolov["has_vehicle"])
        self.assertEqual(sokolov["vehicle_call_sign"], "ТМ-01")

    # --- Подбор ---

    def test_пешему_морозову_предлагают_ближайшую_свободную_машину(self):
        call = self.create_call("VP-BZQ")
        result = self.client.post(
            f"/api/calls/{call['id']}/suggest", headers=auth_header(self.supervisor)
        ).json()
        morozov = next(c for c in result["candidates"] if c["full_name"] == MOROZOV)

        self.assertTrue(morozov["pickup"])
        self.assertEqual(morozov["vehicle_call_sign"], "ТМ-04")
        self.assertEqual([leg["mode"] for leg in morozov["route"]["legs"]], ["walk", "vehicle"])
        # Пешком до стоянки 31 было бы 27.3 мин — нарушение регламента.
        self.assertLess(morozov["minutes"], 15)

    def test_инженер_со_своей_машиной_едет_без_пересадки(self):
        call = self.create_call("VP-BZQ")
        result = self.client.post(
            f"/api/calls/{call['id']}/suggest", headers=auth_header(self.supervisor)
        ).json()
        sokolov = next(c for c in result["candidates"] if c["full_name"] == SOKOLOV)
        self.assertFalse(sokolov["pickup"])
        self.assertEqual(sokolov["vehicle_call_sign"], "ТМ-01")
        self.assertEqual(len(sokolov["route"]["legs"]), 1)

    # --- Резерв и возврат ---

    def test_назначение_резервирует_машину(self):
        call = self.assign(self.create_call("VP-BZQ")["id"], MOROZOV)
        # Ответ на назначение сразу содержит исполнителя и машину. Раньше
        # связи в ответе оставались пустыми до следующего запроса.
        self.assertEqual(call["assigned_employee_name"], MOROZOV)
        self.assertEqual(call["vehicle_call_sign"], "ТМ-04")
        self.assertIsNotNone(call["pickup_node_id"])

        tm04 = self.vehicles()["ТМ-04"]
        self.assertEqual(tm04["status"], "reserved")
        self.assertEqual(tm04["employee_name"], MOROZOV)

    def test_зарезервированную_машину_второму_не_предлагают(self):
        self.assign(self.create_call("VP-BZQ")["id"], MOROZOV)
        db = SessionLocal()
        try:
            free = [vehicle.call_sign for vehicle in free_vehicles(db, "UUEE")]
        finally:
            db.close()
        self.assertNotIn("ТМ-04", free)
        self.assertIn("ТМ-05", free)

    def test_после_работ_машина_остаётся_у_борта(self):
        call = self.assign(self.create_call("VP-BZQ")["id"], MOROZOV)
        engineer = self.login("morozov", "engineer")

        current = self.client.get("/api/me/current-call", headers=auth_header(engineer)).json()
        self.assertEqual(current["vehicle"]["call_sign"], "ТМ-04")
        self.assertTrue(current["vehicle"]["pickup"])

        for action in ("accepted", "arrived", "closed"):
            response = self.client.post(
                "/api/me/status", json={"action": action}, headers=auth_header(engineer)
            )
            self.assertEqual(response.status_code, 200, response.text)

        tm04 = self.vehicles()["ТМ-04"]
        self.assertEqual(tm04["status"], "free")
        self.assertIsNone(tm04["employee_name"])
        lat, lon = self.stand_point(call["stand_node_id"])
        self.assertAlmostEqual(tm04["lat"], lat, places=6)
        self.assertAlmostEqual(tm04["lon"], lon, places=6)

    def test_отменённый_вызов_возвращает_незабранную_машину_на_место(self):
        before = self.vehicles()["ТМ-04"]
        call = self.assign(self.create_call("VP-BZQ")["id"], MOROZOV)
        self.client.patch(
            f"/api/calls/{call['id']}/status",
            json={"status": "closed"},
            headers=auth_header(self.supervisor),
        )
        after = self.vehicles()["ТМ-04"]
        self.assertEqual(after["status"], "free")
        # Инженер до неё не дошёл — машина там же, где стояла.
        self.assertAlmostEqual(after["lat"], before["lat"], places=6)

    def test_уход_со_смены_оставляет_машину_на_месте(self):
        sokolov = self.employee(SOKOLOV)
        engineer = self.login("sokolov", "engineer")
        response = self.client.post(
            f"/api/employees/{sokolov['id']}/shift",
            json={"on_shift": False},
            headers=auth_header(engineer),
        )
        self.assertEqual(response.status_code, 200, response.text)

        tm01 = self.vehicles()["ТМ-01"]
        self.assertEqual(tm01["status"], "free")
        self.assertIsNone(tm01["employee_name"])
        self.assertAlmostEqual(tm01["lat"], sokolov["lat"], places=6)

    def test_очередь_сохраняет_машину_при_инженере(self):
        first = self.assign(self.create_call("VP-BZQ")["id"], SOKOLOV)
        second = self.assign(self.create_call("VP-BES")["id"], SOKOLOV)
        self.assertEqual(second["status"], "queued")

        self.client.patch(
            f"/api/calls/{first['id']}/status",
            json={"status": "closed"},
            headers=auth_header(self.supervisor),
        )
        tm01 = self.vehicles()["ТМ-01"]
        self.assertEqual(tm01["employee_name"], SOKOLOV)
        self.assertEqual(tm01["status"], "in_use")
