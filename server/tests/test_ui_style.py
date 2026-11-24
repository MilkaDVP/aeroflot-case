"""Стиль оформления: читают все, меняет технический администратор."""

from tests.test_api import ApiTestCase, auth_header


class TestUiStyle(ApiTestCase):
    def test_стиль(self):
        self.reset_database()
        self.assertEqual(self.client.get("/api/ui").json(), {"style": "aero"})
        low = auth_header(self.login("local_vdnh", "local_vdnh", airport="VDNH"))
        self.assertEqual(self.client.put("/api/ui", json={"style": "legacy"}, headers=low).status_code, 403)
        admin = auth_header(self.login("admin", "admin", airport="VDNH"))
        self.assertEqual(self.client.put("/api/ui", json={"style": "legacy"}, headers=admin).status_code, 200)
        self.assertEqual(self.client.get("/api/ui").json(), {"style": "legacy"})
        self.assertEqual(self.client.put("/api/ui", json={"style": "x"}, headers=admin).status_code, 422)
