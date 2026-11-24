"""Предупреждение о сроке содержит конкретный вариант: кто ещё может успеть (критерий 21)."""

from datetime import timedelta

from clock import utc_now
from database import SessionLocal
from models.call import Call
from models.employee import Employee
from models.event_log import EventLog
from services import alternatives, journal
from tests.test_api import ApiTestCase, auth_header


class TestAlternative(ApiTestCase):
    def setUp(self):
        self.reset_database()
        alternatives._cache.clear()
        self.head = auth_header(self.login("dispatcher_vdnh", "dispatcher", airport="VDNH"))
        board = next(a for a in self.client.get("/api/aircraft", headers=self.head).json() if a["board_number"] == "RA-73101")
        self.call = self.post("/api/calls", {"aircraft_id": board["id"], "defect_code": "hydraulic_leak"}).json()
        self.best = self.post(f"/api/calls/{self.call['id']}/suggest").json()["best"]

    def post(self, path, body=None):
        return self.client.post(path, json=body, headers=self.head)

    def option(self):
        alternatives._cache.clear()
        calls = self.client.get("/api/calls", headers=self.head).json()
        return next(c for c in calls if c["id"] == self.call["id"])["control"].get("alternative")

    def age(self, minutes):
        with SessionLocal() as db:
            db.get(Call, self.call["id"]).created_at = utc_now() - timedelta(minutes=minutes)
            db.commit()

    def test_без_исполнителя(self):
        self.assertIsNone(self.option())  # угрозы ещё нет
        self.age(9)
        option = self.option()
        self.assertEqual((option["kind"], option["employee_id"]), ("free", self.best["employee_id"]))
        self.assertTrue(option["in_time"])
        self.assertIn("успевает с запасом", option["text"])
        with SessionLocal() as db:
            journal.watch(db)
            db.commit()
            event = db.query(EventLog).filter_by(type="deadline_risk", call_id=self.call["id"]).one()
            self.assertIn("Вариант решения", event.text)
            self.assertEqual(event.params["кто_успеет"], option["name"])

    def test_исполнитель_не_успевает(self):
        self.post(f"/api/calls/{self.call['id']}/assign", {"employee_id": self.best["employee_id"], "override_reason": None})
        with SessionLocal() as db:
            person = db.get(Employee, self.best["employee_id"])
            person.lat, person.lon = 55.8288, 37.6393  # другой конец поля
            person.location_source = "manual"
            db.commit()
        self.age(9)
        option = self.option()
        self.assertEqual(option["kind"], "free")
        self.assertNotEqual(option["employee_id"], self.best["employee_id"])
        engineer = auth_header(self.login("orlov", "engineer", airport="VDNH"))
        shown = [c["control"].get("alternative") for c in self.client.get("/api/calls", headers=engineer).json()]
        self.assertFalse(any(shown))

    def test_некого(self):
        with SessionLocal() as db:
            for person in db.query(Employee).filter_by(airport_icao="VDNH"):
                person.status = "offline"
            db.commit()
        self.age(9)
        self.assertEqual(self.option()["kind"], "nobody")
