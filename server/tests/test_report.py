"""Отчёты PDF: доступ, период, карточка вызова."""

from datetime import timedelta

from clock import utc_now
from tests.test_api import ApiTestCase, auth_header


class TestReport(ApiTestCase):
    def test_отчёт_и_карточка(self):
        self.reset_database()
        now = utc_now()
        params = {"date_from": (now - timedelta(days=1)).isoformat() + "Z", "date_to": now.isoformat() + "Z"}
        for login, password in (("dispatcher", "dispatcher"), ("supervisor", "supervisor"), ("local_uuee", "local_uuee")):
            head = auth_header(self.login(login, password, airport="UUEE"))
            response = self.client.get("/api/analytics/report.pdf", params=params, headers=head)
            self.assertEqual((response.status_code, response.content[:4]), (200, b"%PDF"), login)
        call = self.client.get("/api/calls", headers=head).json()[0]
        self.assertEqual(self.client.get(f"/api/analytics/calls/{call['id']}.pdf", headers=head).content[:4], b"%PDF")
        self.assertEqual(self.client.get("/api/analytics/calls/99999.pdf", headers=head).status_code, 404)
        bad = {"date_from": params["date_to"], "date_to": params["date_from"]}
        self.assertEqual(self.client.get("/api/analytics/report.pdf", params=bad, headers=head).status_code, 422)
        engineer = auth_header(self.login("sokolov", "engineer"))
        self.assertEqual(self.client.get("/api/analytics/report.pdf", params=params, headers=engineer).status_code, 403)
