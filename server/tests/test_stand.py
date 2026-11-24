"""Случайные данные и очистка базы."""

from pathlib import Path

from sqlalchemy import text

from database import SessionLocal, engine
from models.call import Call
from models.call_message import CallMessage
from models.employee import Employee
from models.event_log import EventLog
from models.report import Report
from models.vehicle import Vehicle
from services import journal, stand
from tests.test_api import ApiTestCase, auth_header


class TestStand(ApiTestCase):
    def test_случайные_данные_и_очистка(self):
        self.reset_database()
        low = auth_header(self.login("local_vdnh", "local_vdnh", airport="VDNH"))
        head = auth_header(self.login("admin", "admin", airport="VDNH"))
        for path in ("clear", "random"):
            self.assertEqual(self.client.post(f"/api/admin/stand/{path}", json={"confirm": "ОЧИСТИТЬ"}, headers=low).status_code, 403)
        made = self.client.post("/api/admin/stand/random", json={"days": 7, "count": 40}, headers=head)
        self.assertEqual(made.status_code, 200, made.text)
        body = made.json()
        print(body)
        self.assertEqual(sum(body["outcomes"].values()), 40)
        self.assertTrue(body["outcomes"]["в срок"] and body["outcomes"]["опоздание"] + body["outcomes"]["отменён"])
        for path in ("/api/calls", "/api/calls?include_closed=true", "/api/reports", "/api/events", "/api/vehicles",
                     "/api/ai/plan?explain=false", "/api/shift/report",
                     "/api/analytics/report.pdf?date_from=2026-01-01T00:00:00Z&date_to=2026-01-02T00:00:00Z"):
            self.assertIn(self.client.get(path, headers=head).status_code, (200, 404), path)
        with SessionLocal() as db:
            self.assertIsNone(journal.verify(db)[1])
            users = db.execute(text("select count(*) from users")).scalar()

        self.assertEqual(self.client.post("/api/admin/stand/clear", json={"confirm": "да"}, headers=head).status_code, 422)
        done = self.client.post("/api/admin/stand/clear", json={"confirm": "очистить"}, headers=head)
        self.assertEqual(done.status_code, 200, done.text)
        with SessionLocal() as db:
            for model in (Call, CallMessage, Report):
                self.assertEqual(db.query(model).count(), 0)
            # Остаётся только запись о самой очистке.
            self.assertLessEqual(db.query(EventLog).count(), 2)
            self.assertIsNone(journal.verify(db)[1])
            self.assertTrue(all(v.status == "free" for v in db.query(Vehicle)))
            self.assertFalse([p for p in db.query(Employee) if p.status not in ("free", "offline")])
            self.assertEqual(db.execute(text("select count(*) from users")).scalar(), users)
            with self.assertRaises(Exception):
                db.execute(text("delete from event_log"))
        folder = Path(engine.url.database).parent
        for copy in folder.glob(f"*{stand.BACKUP_MARK}*"):
            copy.unlink()
