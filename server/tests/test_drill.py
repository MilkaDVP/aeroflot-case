"""Учения: инженер без приложения проходит вызов сам, в приложении — нет."""

import unittest
from unittest import mock

from database import SessionLocal
from models.call import Call
from models.employee import Employee
from models.user import User
from services import drill
from tests.test_api import ApiTestCase, auth_header
from tests.test_polygon import HAS_POLYGON


@unittest.skipUnless(HAS_POLYGON, "нет графа полигона")
class TestDrill(ApiTestCase):
    def setUp(self):
        self.reset_database()
        for store in (drill._active, drill._seen, drill._trips):
            store.clear()
        self.head = auth_header(self.login("dispatcher_vdnh", "dispatcher", airport="VDNH"))

    def tearDown(self):
        drill._active.clear()

    def run_ticks(self, count, dt_s=2):
        for _ in range(count):
            with SessionLocal() as db:
                drill.tick(db, dt_s)
                db.commit()

    def test_вызов_целиком(self):
        post = lambda path, body=None: self.client.post(path, json=body, headers=self.head)
        self.assertEqual(post("/api/drill", {"enabled": True}).status_code, 403)
        self.assertFalse(self.client.get("/api/drill", headers=self.head).json()["enabled"])

        board = next(a for a in self.client.get("/api/aircraft", headers=self.head).json()
                     if a["board_number"] == "RA-73101")
        call = post("/api/calls", {"aircraft_id": board["id"], "defect_code": "hydraulic_leak", "needs_tools": True}).json()
        best = post(f"/api/calls/{call['id']}/suggest").json()["best"]
        with mock.patch("services.assignment.AUTO_ACCEPT", True):
            post(f"/api/calls/{call['id']}/assign", {"employee_id": best["employee_id"], "override_reason": None})

        def snapshot():
            with SessionLocal() as db:
                c, e = db.get(Call, call["id"]), db.get(Employee, best["employee_id"])
                return c.status, (e.lat, e.lon), c.tools_taken_at, e.status

        start = snapshot()
        self.run_ticks(5)
        self.assertEqual(snapshot(), start)  # учения не начаты

        drill.set_state("VDNH", True)
        # Зашёл в приложение — сервер его не трогает; вышел — трогает.
        with SessionLocal() as db:
            login = db.query(User).filter(User.employee_id == best["employee_id"]).one().login
        engineer = auth_header(self.login(login, "engineer", airport="VDNH"))
        self.client.get("/api/me/current-call", headers=engineer)
        self.run_ticks(5)
        self.assertEqual(snapshot()[:2], start[:2])
        drill.forget_app(best["employee_id"])
        # Переставили на карте посреди пути — едет оттуда, а не прыгает.
        self.run_ticks(3)
        spot = self.client.get("/api/vehicles", headers=self.head).json()[0]
        self.client.post(f"/api/employees/{best['employee_id']}/location",
                         json={"lat": spot["lat"], "lon": spot["lon"], "source": "manual"}, headers=self.head)
        self.run_ticks(1)
        lat, lon = snapshot()[1]
        self.assertLess(abs(lat - spot["lat"]) + abs(lon - spot["lon"]), 0.0004)

        seconds, statuses = 0, []
        while seconds < 60 * 40:
            self.run_ticks(1)
            seconds += 2
            statuses.append(snapshot()[0])
            if statuses[-1] == "closed":
                break
        self.assertEqual(list(dict.fromkeys(statuses)), ["accepted", "arrived", "closed"])
        self.assertIsNotNone(snapshot()[2])
