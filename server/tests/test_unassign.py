"""
Тесты снятия исполнителя с вызова и переназначения.

Зачем это нужно. Назначение не окончательно: инженер застревает
на предыдущем борте, машина не заводится, вылет переносят. Закрыть такой
вызов нельзя — работа не сделана и борт по-прежнему ждёт, — поэтому
исполнителя снимают, и вызов возвращается к подбору.

Проверяется главное: после снятия не остаётся «висящих» состояний —
ни занятого сотрудника без вызова, ни зарезервированной машины,
ни чужого маршрута в карточке.
"""

from tests.test_api import ApiTestCase, auth_header

SOKOLOV = "Соколов А. В."
MOROZOV = "Морозов Д. С."


class TestUnassign(ApiTestCase):
    """Снятие исполнителя возвращает вызов к подбору."""

    def setUp(self):
        self.reset_database()
        self.supervisor = self.login("supervisor", "supervisor")
        self.dispatcher = self.login("dispatcher", "dispatcher")

    # --- Вспомогательное ---

    def create_call(self, board_number, defect_code="hydraulic_leak"):
        response = self.client.post(
            "/api/calls",
            json={
                "aircraft_id": self.board_id(self.supervisor, board_number),
                "defect_code": defect_code,
            },
            headers=auth_header(self.supervisor),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def employee(self, full_name):
        response = self.client.get("/api/employees", headers=auth_header(self.supervisor))
        for item in response.json():
            if item["full_name"] == full_name:
                return item
        self.fail(f"сотрудник {full_name} не найден")

    def suggest(self, call_id):
        response = self.client.post(
            f"/api/calls/{call_id}/suggest", headers=auth_header(self.dispatcher)
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assign(self, call_id, employee_id, reason=None, token=None):
        return self.client.post(
            f"/api/calls/{call_id}/assign",
            json={"employee_id": employee_id, "override_reason": reason},
            headers=auth_header(token or self.supervisor),
        )

    def unassign(self, call_id, reason="инженер задержан на предыдущем борте", token=None):
        return self.client.post(
            f"/api/calls/{call_id}/unassign",
            json={"reason": reason},
            headers=auth_header(token or self.dispatcher),
        )

    def assigned_call(self, board_number="VP-BZQ"):
        """Вызов с назначенным исполнителем, выбранным системой."""
        call = self.create_call(board_number)
        result = self.suggest(call["id"])
        best = result["best"]
        self.assertIsNotNone(best, "подбор обязан найти кандидата")
        response = self.assign(call["id"], best["employee_id"])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), best

    def vehicles(self):
        response = self.client.get("/api/vehicles", headers=auth_header(self.supervisor))
        return {item["call_sign"]: item for item in response.json()}

    # --- Снятие ---

    def test_снятие_возвращает_вызов_к_подбору(self):
        call, best = self.assigned_call()

        response = self.unassign(call["id"])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        self.assertEqual(body["status"], "new")
        self.assertIsNone(body["assigned_employee_id"])
        self.assertIsNone(body["suggested_employee_id"])
        # Расчёт прошлого назначения относился к снятому — показывать его
        # рядом с новым исполнителем нельзя.
        self.assertIsNone(body["eta_minutes"])
        self.assertIsNone(body["eta_at"])
        self.assertIsNone(body["route_node_ids"])
        self.assertIsNone(body["vehicle_call_sign"])

    def test_в_вызове_остаётся_запись_кого_и_почему_сняли(self):
        call, best = self.assigned_call()

        body = self.unassign(call["id"], "борт переставили на другую стоянку").json()

        self.assertEqual(body["previous_employee_name"], best["full_name"])
        self.assertEqual(body["unassign_reason"], "борт переставили на другую стоянку")
        self.assertIsNotNone(body["unassigned_at"])

    def test_снятый_сотрудник_освобождается(self):
        call, best = self.assigned_call()
        self.assertEqual(self.employee(best["full_name"])["status"], "assigned")

        self.unassign(call["id"])

        released = self.employee(best["full_name"])
        self.assertEqual(released["status"], "free")
        self.assertIsNone(released["busy_until"])

    def test_причина_снятия_обязательна(self):
        call, _ = self.assigned_call()

        response = self.client.post(
            f"/api/calls/{call['id']}/unassign",
            json={"reason": ""},
            headers=auth_header(self.dispatcher),
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_нельзя_снять_с_вызова_без_исполнителя(self):
        call = self.create_call("VP-BZQ")

        response = self.unassign(call["id"])
        self.assertEqual(response.status_code, 409, response.text)

    def test_нельзя_снять_с_закрытого_вызова(self):
        call, _ = self.assigned_call()
        self.client.patch(
            f"/api/calls/{call['id']}/status",
            json={"status": "closed"},
            headers=auth_header(self.dispatcher),
        )

        response = self.unassign(call["id"])
        self.assertEqual(response.status_code, 409, response.text)

    # --- Переназначение ---

    def test_после_снятия_можно_назначить_другого(self):
        call, best = self.assigned_call()
        self.unassign(call["id"])

        result = self.suggest(call["id"])
        new_best = result["best"]
        self.assertIsNotNone(new_best)
        # Снятый не предлагается повторно: диспетчер уже решил, что он
        # сюда не поедет.
        self.assertNotEqual(new_best["employee_id"], best["employee_id"])

        response = self.assign(call["id"], new_best["employee_id"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "assigned")
        self.assertEqual(response.json()["assigned_employee_id"], new_best["employee_id"])

    def test_снятый_показан_в_отказах_с_причиной(self):
        call, best = self.assigned_call()
        self.unassign(call["id"], "не выходит на связь")

        result = self.suggest(call["id"])
        rejected = {item["employee_id"]: item["reason"] for item in result["rejected"]}

        self.assertIn(best["employee_id"], rejected)
        self.assertIn("не выходит на связь", rejected[best["employee_id"]])

    def test_переназначение_на_другого_не_требует_причины_переопределения(self):
        """
        После снятия подбор начинается заново.

        Прошлое предложение указывало на снятого; если бы оно осталось,
        обычный диспетчер не смог бы назначить никого другого без роли
        начальника смены — и переназначение оказалось бы заблокировано.
        """
        call, _ = self.assigned_call()
        self.unassign(call["id"])

        result = self.suggest(call["id"])
        response = self.assign(
            call["id"], result["best"]["employee_id"], token=self.dispatcher
        )
        self.assertEqual(response.status_code, 200, response.text)

    # --- Машины ---

    def test_зарезервированная_машина_возвращается_в_парк(self):
        """
        Морозов идёт пешком до свободной ТМ-04 — она резервируется за ним.
        После снятия машина снова свободна и доступна другим.
        """
        call = self.create_call("VP-BZQ")
        self.suggest(call["id"])
        morozov = self.employee(MOROZOV)
        response = self.assign(call["id"], morozov["id"], "проверка резерва машины")
        self.assertEqual(response.status_code, 200, response.text)

        call_sign = response.json()["vehicle_call_sign"]
        self.assertIsNotNone(call_sign, "по расчёту Морозов едет на машине парка")
        self.assertEqual(self.vehicles()[call_sign]["status"], "reserved")

        self.unassign(call["id"])
        self.assertEqual(self.vehicles()[call_sign]["status"], "free")

    # --- Очередь ---

    def test_снятие_текущего_вызова_продвигает_очередь(self):
        """Снят текущий вызов — следующий из очереди становится текущим."""
        first, best = self.assigned_call("VP-BZQ")
        employee_id = first["assigned_employee_id"]

        second = self.create_call("VP-BES")
        queued = self.assign(second["id"], employee_id, "других с B1.1 рядом нет").json()
        self.assertEqual(queued["status"], "queued")

        self.unassign(first["id"])

        response = self.client.get("/api/calls", headers=auth_header(self.supervisor))
        calls = {item["id"]: item for item in response.json()}

        self.assertIsNone(calls[first["id"]]["assigned_employee_id"])
        self.assertEqual(calls[second["id"]]["status"], "assigned")
        self.assertEqual(calls[second["id"]]["assigned_employee_id"], employee_id)
        # Сотрудник по-прежнему занят — но уже вторым вызовом.
        self.assertEqual(self.employee(best["full_name"])["status"], "assigned")

    def test_снятие_вызова_из_очереди_не_трогает_текущий(self):
        first, best = self.assigned_call("VP-BZQ")
        employee_id = first["assigned_employee_id"]

        second = self.create_call("VP-BES")
        queued = self.assign(second["id"], employee_id, "других с B1.1 рядом нет").json()

        self.unassign(queued["id"])

        response = self.client.get("/api/calls", headers=auth_header(self.supervisor))
        calls = {item["id"]: item for item in response.json()}

        self.assertEqual(calls[first["id"]]["status"], "assigned")
        self.assertEqual(calls[first["id"]]["assigned_employee_id"], employee_id)
        self.assertIsNone(calls[queued["id"]]["assigned_employee_id"])
        self.assertEqual(self.employee(best["full_name"])["status"], "assigned")

    # --- Права ---

    def test_инженер_не_вправе_снимать_исполнителя(self):
        call, _ = self.assigned_call()
        engineer = self.login("sokolov", "engineer")

        response = self.unassign(call["id"], token=engineer)
        self.assertEqual(response.status_code, 403, response.text)
