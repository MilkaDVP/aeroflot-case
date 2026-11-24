"""Прибытие ВС по расписанию и фактическое — в журнале (п. 3.2)."""

from datetime import timedelta

from clock import utc_now
from database import SessionLocal
from models.event_log import EventLog
from tests.test_api import ApiTestCase, auth_header


class TestSchedule(ApiTestCase):
    def test_расписание_и_прибытие(self):
        self.reset_database()
        head = auth_header(self.login("dispatcher_vdnh", "dispatcher", airport="VDNH"))
        board = self.client.get("/api/aircraft", headers=head).json()[0]
        planned = (utc_now() - timedelta(minutes=10)).replace(microsecond=0).isoformat() + "Z"
        url = f"/api/aircraft/{board['id']}"
        for _ in range(2):  # повтор того же времени второй записи не даёт
            saved = self.client.put(f"{url}/schedule", json={"scheduled_arrival_at": planned}, headers=head)
            self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["scheduled_arrival_at"])
        point = {"lat": board["lat"] + 0.0005, "lon": board["lon"], "free": True}
        self.client.post(f"{url}/location", json=point, headers=head)
        back = {"lat": board["lat"], "lon": board["lon"], "stand_node_id": board["stand_node_id"]}
        self.assertEqual(self.client.post(f"{url}/location", json=back, headers=head).status_code, 200)
        self.client.post("/api/calls", json={"aircraft_id": board["id"], "defect_code": "hydraulic_leak"}, headers=head)
        with SessionLocal() as db:
            rows = db.query(EventLog).filter(EventLog.type.like("aircraft_%")).order_by(EventLog.id).all()
            self.assertEqual([r.type for r in rows], ["aircraft_schedule", "aircraft_arrival", "aircraft_schedule"])
            self.assertAlmostEqual(rows[1].params["отклонение_мин"], 10, delta=1)
            self.assertIsNotNone(rows[2].call_id)
        boss = auth_header(self.login("supervisor_vdnh", "supervisor", airport="VDNH"))
        self.assertIn("по расписанию", self.client.get("/api/events/export?format=csv", headers=boss).text)
