"""Назначенный вызов сразу закреплён: принять или отказаться инженеру не предлагается."""

from unittest import mock

from database import SessionLocal
from models.user import User
from tests.test_api import ApiTestCase, auth_header


class TestAutoAccept(ApiTestCase):
    def test_закрепляется_сразу(self):
        self.reset_database()
        head = auth_header(self.login("dispatcher_vdnh", "dispatcher", airport="VDNH"))
        board = self.client.get("/api/aircraft", headers=head).json()[0]
        call = self.client.post("/api/calls", json={"aircraft_id": board["id"], "defect_code": "hydraulic_leak"}, headers=head).json()
        best = self.client.post(f"/api/calls/{call['id']}/suggest", headers=head).json()["best"]
        with mock.patch("services.assignment.AUTO_ACCEPT", True):
            done = self.client.post(f"/api/calls/{call['id']}/assign", headers=head,
                                    json={"employee_id": best["employee_id"], "override_reason": None}).json()
        self.assertEqual(done["status"], "accepted")
        people = self.client.get("/api/employees", headers=head).json()
        self.assertEqual(next(p for p in people if p["id"] == best["employee_id"])["status"], "en_route")
        with SessionLocal() as db:
            login = db.query(User).filter_by(employee_id=best["employee_id"]).one().login
        me = auth_header(self.login(login, "engineer", airport="VDNH"))
        self.assertEqual(self.client.post("/api/me/status", json={"action": "declined"}, headers=me).status_code, 409)
        self.assertEqual(self.client.post("/api/me/status", json={"action": "accepted"}, headers=me).json()["status"], "accepted")
