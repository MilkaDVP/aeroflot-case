"""Контракт POST /api/v1/track (Приложение к положению)."""

from datetime import timedelta

from clock import utc_now
from services.tracking import format_time, reset_memory
from tests.test_api import ApiTestCase, auth_header


def iso(seconds_ago=0):
    return format_time(utc_now() - timedelta(seconds=seconds_ago))


class TestTrackApi(ApiTestCase):
    def setUp(self):
        self.reset_database()
        reset_memory()
        self.supervisor = self.login("supervisor", "supervisor")
        self.objects = {item["obj"]: item for item in self.client.get("/api/v1/track/objects").json()}
        self.engineer = next(o for o, item in self.objects.items()
                             if item["kind"] == "engineer" and item["airport_icao"] == "UUEE")
        self.vehicle = next(o for o, item in self.objects.items()
                            if item["kind"] == "vehicle" and item["airport_icao"] == "UUEE")

    def employee(self, obj):
        employees = self.client.get("/api/employees", headers=auth_header(self.supervisor)).json()
        return next(e for e in employees if e["id"] == int(obj[2:]))

    def near(self, obj, d_lat=0.0001):
        """Точка рядом с текущим местом сотрудника."""
        employee = self.employee(obj)
        return employee["lat"] + d_lat, employee["lon"]

    def test_одна_точка_принята(self):
        lat, lon = self.near(self.engineer)
        t = iso()
        response = self.client.post("/api/v1/track", json={
            "obj": self.engineer, "lat": lat, "lon": lon, "t": t, "acc": 5, "spd": 1.2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"accepted": True, "applied": True, "obj": self.engineer, "t": t})
        employee = self.employee(self.engineer)
        self.assertAlmostEqual(employee["lat"], lat)
        self.assertEqual(employee["location_source"], "gps")

    def test_без_времени_берётся_серверное(self):
        lat, lon = self.near(self.engineer)
        body = self.client.post("/api/v1/track", json={"obj": self.engineer, "lat": lat, "lon": lon}).json()
        self.assertTrue(body["applied"])
        self.assertTrue(body["t"].endswith("Z"))

    def test_нет_координат_400(self):
        for body in ({"obj": "e-01", "lat": 55.8}, {"obj": "e-01", "lat": "55.8", "lon": 37.6}, "мусор"):
            response = self.client.post("/api/v1/track", json=body)
            self.assertEqual(response.status_code, 400, body)

    def test_неизвестный_объект_200_не_применён(self):
        response = self.client.post("/api/v1/track", json={"obj": "x-999", "lat": 55.8, "lon": 37.6})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accepted"], True)
        self.assertEqual(response.json()["applied"], False)

    def test_пакет_в_порядке_запроса(self):
        lat, lon = self.near(self.engineer)
        vehicle = self.objects[self.vehicle]
        response = self.client.post("/api/v1/track", json={"points": [
            {"obj": self.engineer, "lat": lat, "lon": lon, "t": iso(-2)},
            {"obj": "нет-такого", "lat": lat, "lon": lon},
            {"obj": self.vehicle, "lat": lat, "lon": lon, "t": iso(-1)},
            {"obj": self.engineer, "lon": lon},
        ]})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual([r["applied"] for r in body], [True, False, True, False])
        self.assertEqual([r["accepted"] for r in body], [True, True, True, False])
        self.assertEqual(body[2]["obj"], self.vehicle)
        vehicles = self.client.get("/api/vehicles", headers=auth_header(self.supervisor)).json()
        moved = next(v for v in vehicles if v["call_sign"] == vehicle["name"])
        if moved["status"] != "in_use":
            self.assertAlmostEqual(moved["lat"], lat)

    def test_выброс_отбрасывается(self):
        lat, lon = self.near(self.engineer)
        first = self.client.post("/api/v1/track", json={
            "obj": self.engineer, "lat": lat, "lon": lon, "t": iso(-5)}).json()
        self.assertTrue(first["applied"])
        # 1 км за секунду
        jump = self.client.post("/api/v1/track", json={
            "obj": self.engineer, "lat": lat + 0.009, "lon": lon, "t": iso(-6)}).json()
        self.assertFalse(jump["applied"])
        poor = self.client.post("/api/v1/track", json={
            "obj": self.engineer, "lat": lat, "lon": lon, "t": iso(-7), "acc": 500}).json()
        self.assertFalse(poor["applied"])
        stale = self.client.post("/api/v1/track", json={
            "obj": self.engineer, "lat": lat, "lon": lon, "t": iso(60)}).json()
        self.assertFalse(stale["applied"])

    def test_борт_по_бортовому_номеру(self):
        aircraft = self.client.get("/api/aircraft", headers=auth_header(self.supervisor)).json()[0]
        response = self.client.post("/api/v1/track", json={
            "obj": aircraft["board_number"].replace("-", ""),
            "lat": aircraft["lat"] + 0.001, "lon": aircraft["lon"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["applied"])
        moved = next(a for a in self.client.get("/api/aircraft", headers=auth_header(self.supervisor)).json()
                     if a["id"] == aircraft["id"])
        self.assertAlmostEqual(moved["lat"], aircraft["lat"] + 0.001)
