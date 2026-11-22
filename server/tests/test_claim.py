"""
Захват вызова: один вызов разбирает один диспетчер.

Второй диспетчер видит, кто занят вызовом, но назначить, отменить
или изменить его не может. Захват истекает без подтверждения, снимается
при выходе, а начальник смены вправе забрать вызов себе.
"""

from datetime import timedelta

from clock import utc_now
from services.claim import CLAIM_TTL
from tests.test_api import ApiTestCase, auth_header


class TestClaim(ApiTestCase):
    def setUp(self):
        self.reset_database()
        self.first = self.login("dispatcher", "dispatcher")
        self.second = self.login("dispatcher2", "dispatcher")

    def post(self, token, path, body=None):
        return self.client.post(path, json=body, headers=auth_header(token))

    def new_call_id(self):
        """
        Свежий вызов на VP-BZQ, у которого заведомо есть кандидаты.
        Регистрирует его первый диспетчер, но захват сразу отпускает:
        тесты сами решают, кто его возьмёт.
        """
        aircraft = self.client.get("/api/aircraft", headers=auth_header(self.first)).json()
        board = next(item for item in aircraft if item["board_number"] == "VP-BZQ")
        created = self.post(
            self.first, "/api/calls", {"aircraft_id": board["id"], "defect_code": "hydraulic_leak"}
        ).json()
        self.post(self.first, f"/api/calls/{created['id']}/release")
        return created["id"]

    def call(self, token, call_id):
        calls = self.client.get("/api/calls", headers=auth_header(token)).json()
        return next(call for call in calls if call["id"] == call_id)

    def best_employee(self, token, call_id):
        result = self.post(token, f"/api/calls/{call_id}/suggest").json()
        return result["best"]["employee_id"]

    def test_взятый_вызов_виден_второму_с_именем(self):
        call_id = self.new_call_id()
        response = self.post(self.first, f"/api/calls/{call_id}/claim")
        self.assertEqual(response.status_code, 200, response.text)

        seen = self.call(self.second, call_id)
        self.assertEqual(seen["claimed_by_login"], "dispatcher")
        self.assertEqual(seen["claimed_by_name"], "Смирнова О. В.")

    def test_второй_не_может_взять_и_назначить(self):
        call_id = self.new_call_id()
        employee_id = self.best_employee(self.first, call_id)

        taken = self.post(self.second, f"/api/calls/{call_id}/claim")
        self.assertEqual(taken.status_code, 409, taken.text)
        self.assertIn("Смирнова О. В.", taken.json()["detail"])

        assigned = self.post(
            self.second, f"/api/calls/{call_id}/assign", {"employee_id": employee_id}
        )
        self.assertEqual(assigned.status_code, 409, assigned.text)

        cancelled = self.client.patch(
            f"/api/calls/{call_id}/status",
            json={"status": "cancelled"},
            headers=auth_header(self.second),
        )
        self.assertEqual(cancelled.status_code, 409, cancelled.text)

    def test_второй_может_посмотреть_подбор(self):
        call_id = self.new_call_id()
        self.post(self.first, f"/api/calls/{call_id}/claim")
        response = self.post(self.second, f"/api/calls/{call_id}/suggest")
        self.assertEqual(response.status_code, 200, response.text)
        # Подбор не перевёл вызов на второго.
        self.assertEqual(self.call(self.first, call_id)["claimed_by_login"], "dispatcher")

    def test_первый_назначает_и_захват_снимается(self):
        call_id = self.new_call_id()
        employee_id = self.best_employee(self.first, call_id)
        response = self.post(
            self.first, f"/api/calls/{call_id}/assign", {"employee_id": employee_id}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["claimed_by_login"])

    def test_отпущенный_вызов_берёт_второй(self):
        call_id = self.new_call_id()
        self.post(self.first, f"/api/calls/{call_id}/claim")
        self.post(self.first, f"/api/calls/{call_id}/release")
        response = self.post(self.second, f"/api/calls/{call_id}/claim")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["claimed_by_login"], "dispatcher2")

    def test_взял_следующий_предыдущий_освободился(self):
        waiting = [self.new_call_id(), self.new_call_id()]
        self.post(self.first, f"/api/calls/{waiting[0]}/claim")
        self.post(self.first, f"/api/calls/{waiting[1]}/claim")
        self.assertIsNone(self.call(self.second, waiting[0])["claimed_by_login"])

    def test_выход_освобождает_вызов(self):
        call_id = self.new_call_id()
        self.post(self.first, f"/api/calls/{call_id}/claim")
        self.post(self.first, "/api/auth/logout")
        response = self.post(self.second, f"/api/calls/{call_id}/claim")
        self.assertEqual(response.status_code, 200, response.text)

    def test_захват_истекает_без_подтверждения(self):
        from database import SessionLocal
        from models.call import Call

        call_id = self.new_call_id()
        self.post(self.first, f"/api/calls/{call_id}/claim")

        db = SessionLocal()
        try:
            call = db.get(Call, call_id)
            call.claimed_at = utc_now() - CLAIM_TTL - timedelta(seconds=1)
            db.commit()
        finally:
            db.close()

        self.assertIsNone(self.call(self.second, call_id)["claimed_by_login"])
        response = self.post(self.second, f"/api/calls/{call_id}/claim")
        self.assertEqual(response.status_code, 200, response.text)

    def test_начальник_смены_забирает_вызов(self):
        call_id = self.new_call_id()
        self.post(self.first, f"/api/calls/{call_id}/claim")
        supervisor = self.login("supervisor", "supervisor")

        plain = self.post(supervisor, f"/api/calls/{call_id}/claim")
        self.assertEqual(plain.status_code, 409, plain.text)

        taken = self.post(supervisor, f"/api/calls/{call_id}/claim", {"takeover": True})
        self.assertEqual(taken.status_code, 200, taken.text)
        self.assertEqual(taken.json()["claimed_by_login"], "supervisor")

    def test_диспетчер_не_забирает_чужой_флагом(self):
        call_id = self.new_call_id()
        self.post(self.first, f"/api/calls/{call_id}/claim")
        response = self.post(self.second, f"/api/calls/{call_id}/claim", {"takeover": True})
        self.assertEqual(response.status_code, 409, response.text)

    def test_новый_вызов_сразу_у_того_кто_его_завёл(self):
        aircraft = self.client.get("/api/aircraft", headers=auth_header(self.first)).json()[0]
        created = self.post(
            self.first,
            "/api/calls",
            {"aircraft_id": aircraft["id"], "defect_code": "hydraulic_leak"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["claimed_by_login"], "dispatcher")
