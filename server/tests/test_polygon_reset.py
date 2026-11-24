"""Сброс полигона: всё ровно как в геоданных организаторов."""

import unittest

from database import SessionLocal
from models.call import Call
from models.employee import Employee
from models.vehicle import Vehicle
from services import polygon
from tests.test_api import ApiTestCase, auth_header
from tests.test_polygon import HAS_POLYGON


@unittest.skipUnless(HAS_POLYGON, "нет графа полигона")
class TestPolygonReset(ApiTestCase):
    def test_сброс(self):
        self.reset_database()
        boss = auth_header(self.login("local_vdnh", "local_vdnh", airport="VDNH"))
        low = auth_header(self.login("dispatcher_vdnh", "dispatcher", airport="VDNH"))
        path = "/api/admin/airports/polygon/reset"
        self.assertEqual(self.client.post(path, headers=low).status_code, 403)
        board = self.client.get("/api/aircraft", headers=low).json()[0]
        call = self.client.post("/api/calls", json={"aircraft_id": board["id"], "defect_code": "hydraulic_leak"}, headers=low).json()
        best = self.client.post(f"/api/calls/{call['id']}/suggest", headers=low).json()["best"]
        self.client.post(f"/api/calls/{call['id']}/assign", json={"employee_id": best["employee_id"], "override_reason": None}, headers=low)
        with SessionLocal() as db:
            for person in db.query(Employee).filter_by(airport_icao="VDNH"):
                person.lat += 0.001
            db.query(Vehicle).filter_by(airport_icao="VDNH").first().lat += 0.001
            db.commit()
        for _ in range(2):
            response = self.client.post(path, headers=boss)
            self.assertEqual(response.json(), {"engineers": 7, "vehicles": 3, "aircraft": 10}, response.text)
        with SessionLocal() as db:
            people = db.query(Employee).filter_by(airport_icao="VDNH", archived=False).order_by(Employee.id).all()
            self.assertEqual([(p.lat, p.lon) for p in people], [r[1:] for r in polygon.layer("engineers_positions")])
            self.assertTrue(all(p.status == "free" and p.vehicle is None for p in people))
            cars = db.query(Vehicle).filter_by(airport_icao="VDNH", archived=False).order_by(Vehicle.id).all()
            self.assertEqual([(v.call_sign, v.lat, v.lon, v.status) for v in cars],
                             [(r[0]["object_id"], r[1], r[2], "free") for r in polygon.layer("service_vehicles_positions")])
            self.assertEqual(db.get(Call, call["id"]).status, "cancelled")
        boards = self.client.get("/api/aircraft", headers=low).json()
        self.assertEqual(len({b["stand_ref"] for b in boards}), 10)
        for path in ("/api/calls", "/api/employees", "/api/vehicles"):
            self.assertEqual(self.client.get(path, headers=low).status_code, 200)
