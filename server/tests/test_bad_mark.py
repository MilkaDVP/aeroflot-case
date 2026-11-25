"""Испорченная отметка одного инженера не роняет список вызовов и подбор."""

from datetime import timedelta

from clock import utc_now
from database import SessionLocal
from models.call import Call
from models.employee import Employee
from services import alternatives
from tests.test_api import ApiTestCase, auth_header


class TestBadMark(ApiTestCase):
    def test_список_и_сохранение(self):
        self.reset_database()
        alternatives._cache.clear()
        head = auth_header(self.login("local_vdnh", "local_vdnh", airport="VDNH"))
        board = self.client.get("/api/aircraft", headers=head).json()[0]
        call = self.client.post("/api/calls", json={"aircraft_id": board["id"], "defect_code": "hydraulic_leak"}, headers=head).json()
        with SessionLocal() as db:
            person = db.query(Employee).filter_by(airport_icao="VDNH").order_by(Employee.id).first()
            person.qualifications = [{"category": "", "aircraft_types": ["A320"], "valid_until": "2027-12-31"}]
            key = person.id
            db.get(Call, call["id"]).created_at = utc_now() - timedelta(minutes=9)
            db.commit()
        self.assertEqual(self.client.get("/api/calls", headers=head).status_code, 200)
        self.assertEqual(self.client.post(f"/api/calls/{call['id']}/suggest", headers=head).status_code, 200)
        for mark, code in (("", 422), ("Z9", 422), ("AM", 200)):
            body = {"qualifications": [{"category": mark, "aircraft_types": ["A320"], "valid_until": "2027-12-31"}]}
            self.assertEqual(self.client.patch(f"/api/employees/{key}", json=body, headers=head).status_code, code, mark)
