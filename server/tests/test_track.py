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
        return next(e for e in employees if e["full_name"] == self.objects[obj]["name"])

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
            "obj": self.engineer, "lat": lat, "lon": lon, "t": iso(120)}).json()
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

    def test_номера_полигона_по_порядку(self):
        names = [item["name"] for obj, item in self.objects.items()
                 if item["airport_icao"] == "VDNH" and item["kind"] == "engineer"]
        self.assertEqual(self.objects["e-01"]["name"], names[0])
        self.assertEqual(self.objects["v-01"]["airport_icao"], "VDNH")
        self.assertIn("UUEE:e-01", self.objects)

    def post(self, **point):
        return self.client.post("/api/v1/track", json=point).json()

    def test_сценарное_время_имитатора(self):
        lat, lon = self.near(self.engineer)
        for year in ("2026-12-03", "2020-01-01"):
            reset_memory()
            for second in (20, 22):
                body = self.post(obj=self.engineer, lat=lat, lon=lon, t=f"{year}T09:14:{second}.123Z")
                self.assertTrue(body["applied"], (year, second))
            self.assertFalse(self.post(obj=self.engineer, lat=lat, lon=lon, t=f"{year}T09:14:21.000Z")["applied"])

    def test_пакет_сценарного_времени_применяется_весь(self):
        lat, lon = self.near(self.engineer)
        points = [{"obj": self.engineer, "lat": lat + i * 1e-5, "lon": lon, "t": f"2030-01-01T00:00:0{i}Z"}
                  for i in range(4)]
        body = self.client.post("/api/v1/track", json={"points": points}).json()
        self.assertEqual([r["applied"] for r in body], [True] * 4)
        self.assertAlmostEqual(self.employee(self.engineer)["lat"], lat + 3e-5)

    def test_расстановка_вручную_не_выброс(self):
        from database import SessionLocal
        from models.employee import Employee

        with SessionLocal() as db:
            db.query(Employee).filter_by(full_name=self.objects[self.engineer]["name"]).one().location_source = "manual"
            db.commit()
        lat, lon = self.near(self.engineer, 0.009)
        self.assertTrue(self.post(obj=self.engineer, lat=lat, lon=lon)["applied"])

    def test_скорость_не_портит_норматив(self):
        from database import SessionLocal
        from models.vehicle import Vehicle

        name = self.objects[self.vehicle]["name"]
        with SessionLocal() as db:
            before = db.query(Vehicle).filter_by(call_sign=name, airport_icao="UUEE").one()
            lat, lon = before.position()
            speed = before.speed_kmh
        self.post(obj=self.vehicle, lat=lat, lon=lon, spd=20)
        with SessionLocal() as db:
            after = db.query(Vehicle).filter_by(call_sign=name, airport_icao="UUEE").one()
            self.assertEqual(after.speed_kmh, speed)

    def test_машина_с_инженером_двигает_его(self):
        from database import SessionLocal
        from models.vehicle import Vehicle

        vehicles = self.client.get("/api/vehicles", headers=auth_header(self.supervisor)).json()
        car = next(v for v in vehicles if v["status"] == "in_use")
        # Телефон молчит две минуты.
        with SessionLocal() as db:
            driver = db.query(Vehicle).filter_by(call_sign=car["call_sign"], airport_icao="UUEE").one().employee
            driver.location_at = utc_now() - timedelta(seconds=120)
            db.commit()
        for step in (1, 2):
            body = self.post(obj="UUEE:" + car["call_sign"], lat=car["lat"] + step * 1e-4, lon=car["lon"])
            self.assertTrue(body["applied"])
            now = {v["call_sign"]: v for v in
                   self.client.get("/api/vehicles", headers=auth_header(self.supervisor)).json()}
            self.assertAlmostEqual(now[car["call_sign"]]["lat"], car["lat"] + step * 1e-4)

    def test_нет_маршрута_борт_остаётся(self):
        from unittest import mock

        from fastapi import HTTPException

        board = self.client.get("/api/aircraft", headers=auth_header(self.supervisor)).json()[0]
        # Далеко от своей стоянки — чтобы сменилась ближайшая вершина.
        with mock.patch("api.airports.apply_aircraft_move", side_effect=HTTPException(409, "нет маршрута")) as move:
            body = self.post(obj=board["board_number"], lat=board["lat"] + 0.004, lon=board["lon"] + 0.004)
        move.assert_called_once()
        self.assertFalse(body["applied"])
        after = next(a for a in self.client.get("/api/aircraft", headers=auth_header(self.supervisor)).json()
                     if a["id"] == board["id"])
        self.assertEqual((after["lat"], after["stand_node_id"]), (board["lat"], board["stand_node_id"]))

    def test_журнал_ошибки_есть_потока_нет(self):
        from database import SessionLocal
        from models.event_log import EventLog

        def rows():
            with SessionLocal() as db:
                return [event.type for event in db.query(EventLog).order_by(EventLog.id)]

        lat, lon = self.near(self.engineer)
        before = len(rows())
        self.post(obj=self.engineer, lat=lat, lon=lon)
        self.assertEqual(len(rows()), before)
        self.post(obj="x-1", lat=lat, lon=lon)
        self.post(obj="x-1", lat=lat, lon=lon)
        self.post(obj="x-1")
        self.assertEqual(rows()[before:], ["data_error", "data_error"])

    def test_прибытие_фиксируется_по_точкам(self):
        from database import SessionLocal
        from graph_registry import get_graph
        from models.aircraft import Aircraft
        from models.call import Call
        from models.employee import Employee

        name = self.objects["e-01"]["name"]
        with SessionLocal() as db:
            board = db.query(Aircraft).filter_by(board_number="RA-73001", airport_icao="VDNH").one()
            engineer = db.query(Employee).filter_by(full_name=name, airport_icao="VDNH").one()
            engineer.status = "en_route"
            call = Call(airport_icao="VDNH", aircraft_id=board.id, stand_node_id=board.stand_node_id,
                        defect_code="hydraulic_leak", required_mark="B1.1", status="accepted",
                        assigned_employee_id=engineer.id)
            db.add(call)
            db.commit()
            call_id, engineer_id = call.id, engineer.id
        node = get_graph("VDNH").node("stand_1")
        self.post(obj="e-01", lat=node["lat"], lon=node["lon"], acc=5, t="2030-01-01T00:00:00Z")
        with SessionLocal() as db:
            call = db.get(Call, call_id)
            self.assertEqual(call.status, "accepted")
            # Первая точка была 11 секунд назад.
            back = timedelta(seconds=11)
            db.get(Employee, engineer_id).location_at -= back
            call.near_aircraft_since -= back
            call.last_aircraft_fix -= back
            db.commit()
        self.post(obj="e-01", lat=node["lat"], lon=node["lon"], acc=5, t="2030-01-01T00:00:11Z")
        with SessionLocal() as db:
            self.assertEqual(db.get(Call, call_id).status, "arrived")
            self.assertEqual(db.get(Call, call_id).arrival_mode, "auto")
