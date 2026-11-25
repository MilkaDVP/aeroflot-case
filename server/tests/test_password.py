"""Смена своего пароля."""

from database import SessionLocal
from models.event_log import EventLog
from tests.test_api import ApiTestCase, auth_header


class TestPassword(ApiTestCase):
    def test_смена(self):
        self.reset_database()
        other = auth_header(self.login("admin", "admin", airport="VDNH"))
        head = auth_header(self.login("admin", "admin", airport="VDNH"))
        send = lambda a, b: self.client.post("/api/auth/password", json={"current_password": a, "new_password": b}, headers=head).status_code
        self.assertEqual(send("wrong", "Novyi-parol-1"), 403)
        self.assertEqual(send("admin", "short"), 422)
        self.assertEqual(send("admin", "Novyi-parol-1"), 200)
        self.assertEqual(self.client.get("/api/auth/me", headers=head).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me", headers=other).status_code, 401)
        enter = lambda p: self.client.post("/api/auth/login", json={"login": "admin", "password": p}).status_code
        self.assertEqual((enter("admin"), enter("Novyi-parol-1")), (401, 200))
        with SessionLocal() as db:
            for event in db.query(EventLog):
                self.assertNotIn("Novyi-parol", f"{event.text}{event.params}")
