"""
Тесты очереди вызовов у инженера.

Исходный дефект, ради которого очередь появилась: назначение не проверяло
занятость сотрудника. Начальник смены мог назначить уже занятого инженера,
и тогда один из двух его вызовов терялся — телефон показывал только новый,
а закрытие нового делало инженера «свободным», хотя старый так и оставался
назначенным. Вызов, к которому никто не едет, при том что система считает
его обслуживаемым.

Сценарий везде один: Соколов (B1.1, A320/A321, на машине) назначается
на утечку гидрожидкости у VP-BZQ, затем начальник смены ставит к нему
в очередь такой же дефект у VP-BES.
"""

from datetime import datetime
from operator import itemgetter

from tests.test_api import ApiTestCase, auth_header

SOKOLOV = "Соколов А. В."
NIKITIN = "Никитин П. А."


class TestQueue(ApiTestCase):
    """Очередь вызовов у инженера и инвариант одного текущего вызова."""

    def setUp(self):
        # Каждый тест с чистой базы: очередь меняет статусы сотрудников,
        # и без сброса порядок тестов влиял бы на результат.
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

    def assign(self, token, call_id, employee_id, reason=None):
        return self.client.post(
            f"/api/calls/{call_id}/assign",
            json={"employee_id": employee_id, "override_reason": reason},
            headers=auth_header(token),
        )

    def calls(self):
        response = self.client.get("/api/calls", headers=auth_header(self.supervisor))
        return {item["id"]: item for item in response.json()}

    def busy_sokolov(self):
        """Соколов назначен на первый вызов; возвращает этот вызов."""
        first = self.create_call("VP-BZQ")
        sokolov = self.employee(SOKOLOV)
        response = self.assign(self.supervisor, first["id"], sokolov["id"], "тестовый сценарий")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "assigned")
        return response.json()

    def queue_to_sokolov(self, board_number="VP-BES"):
        call = self.create_call(board_number)
        sokolov = self.employee(SOKOLOV)
        response = self.assign(
            self.supervisor, call["id"], sokolov["id"], "других с B1.1 рядом нет"
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # --- Постановка в очередь ---

    def test_занятый_сотрудник_получает_вызов_в_очередь(self):
        first = self.busy_sokolov()
        second = self.queue_to_sokolov()

        self.assertEqual(second["status"], "queued")
        self.assertEqual(second["assigned_employee_id"], first["assigned_employee_id"])
        self.assertIsNotNone(second["queued_at"])

        # К очередному борту он поедет не раньше, чем закончит первый:
        # прибытие + норматив работ 30 минут.
        first_eta = datetime.fromisoformat(first["eta_at"])
        second_eta = datetime.fromisoformat(second["eta_at"])
        self.assertGreaterEqual((second_eta - first_eta).total_seconds(), 30 * 60)

        # Время освобождения растянулось на всю цепочку.
        sokolov = self.employee(SOKOLOV)
        busy_until = datetime.fromisoformat(sokolov["busy_until"])
        self.assertGreaterEqual(busy_until, second_eta)

    def test_диспетчер_не_вправе_ставить_в_очередь(self):
        self.busy_sokolov()
        call = self.create_call("VP-BES")
        response = self.assign(
            self.dispatcher, call["id"], self.employee(SOKOLOV)["id"], "очень надо"
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertNotIn(self.calls()[call["id"]]["status"], ("queued", "assigned"))

    def test_без_причины_в_очередь_нельзя(self):
        self.busy_sokolov()
        call = self.create_call("VP-BES")
        response = self.assign(self.supervisor, call["id"], self.employee(SOKOLOV)["id"])
        self.assertEqual(response.status_code, 422, response.text)

    def test_один_текущий_вызов_на_инженера(self):
        # Инвариант, нарушение которого и было исходным дефектом.
        self.busy_sokolov()
        self.queue_to_sokolov("VP-BES")
        self.queue_to_sokolov("RA-89001")

        sokolov_id = self.employee(SOKOLOV)["id"]
        mine = [c for c in self.calls().values() if c["assigned_employee_id"] == sokolov_id]
        working = [c for c in mine if c["status"] in ("assigned", "accepted", "arrived")]
        queued = sorted(
            (c for c in mine if c["status"] == "queued"), key=itemgetter("queued_at")
        )

        self.assertEqual(len(working), 1)
        self.assertEqual(len(queued), 2)
        # Очередь выполняется по порядку: второй в очереди прибудет позже первого.
        self.assertLess(queued[0]["eta_at"], queued[1]["eta_at"])

    # --- Продвижение очереди ---

    def test_после_закрытия_инженер_сразу_получает_следующий_вызов(self):
        # Прямой регрессионный тест исходного дефекта — через PWA инженера.
        first = self.busy_sokolov()
        second = self.queue_to_sokolov()
        engineer = self.login("sokolov", "engineer")

        current = self.client.get("/api/me/current-call", headers=auth_header(engineer)).json()
        self.assertEqual(current["call"]["id"], first["id"])
        self.assertEqual(current["queued_count"], 1)

        for action in ("accepted", "arrived", "closed"):
            response = self.client.post(
                "/api/me/status", json={"action": action}, headers=auth_header(engineer)
            )
            self.assertEqual(response.status_code, 200, response.text)

        current = self.client.get("/api/me/current-call", headers=auth_header(engineer)).json()
        self.assertEqual(current["call"]["id"], second["id"])
        self.assertEqual(current["call"]["status"], "assigned")
        self.assertEqual(current["queued_count"], 0)
        self.assertTrue(current["route_points"], "у продвинутого вызова нет маршрута")

        # И главное: инженер не «свободен», пока у него есть вызов.
        self.assertEqual(self.employee(SOKOLOV)["status"], "assigned")

    def test_закрытие_диспетчером_тоже_продвигает_очередь(self):
        first = self.busy_sokolov()
        second = self.queue_to_sokolov()

        response = self.client.patch(
            f"/api/calls/{first['id']}/status",
            json={"status": "closed"},
            headers=auth_header(self.supervisor),
        )
        self.assertEqual(response.status_code, 200, response.text)

        self.assertEqual(self.calls()[second["id"]]["status"], "assigned")
        self.assertEqual(self.employee(SOKOLOV)["status"], "assigned")

    def test_после_последнего_вызова_инженер_свободен(self):
        first = self.busy_sokolov()
        self.client.patch(
            f"/api/calls/{first['id']}/status",
            json={"status": "closed"},
            headers=auth_header(self.supervisor),
        )
        sokolov = self.employee(SOKOLOV)
        self.assertEqual(sokolov["status"], "free")
        self.assertIsNone(sokolov["busy_until"])

    def test_отмена_вызова_из_очереди_не_освобождает_инженера(self):
        self.busy_sokolov()
        second = self.queue_to_sokolov()

        self.client.patch(
            f"/api/calls/{second['id']}/status",
            json={"status": "closed"},
            headers=auth_header(self.supervisor),
        )
        self.assertEqual(self.employee(SOKOLOV)["status"], "assigned")

    # --- Запреты ---

    def test_повторное_назначение_запрещено(self):
        first = self.busy_sokolov()
        morozov = self.employee("Морозов Д. С.")
        response = self.assign(self.supervisor, first["id"], morozov["id"], "передумали")
        self.assertEqual(response.status_code, 409, response.text)

    def test_нельзя_вручную_поставить_в_очередь(self):
        call = self.create_call("VP-BES")
        response = self.client.patch(
            f"/api/calls/{call['id']}/status",
            json={"status": "queued"},
            headers=auth_header(self.supervisor),
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_нельзя_вручную_сделать_очередной_вызов_текущим(self):
        self.busy_sokolov()
        second = self.queue_to_sokolov()
        response = self.client.patch(
            f"/api/calls/{second['id']}/status",
            json={"status": "assigned"},
            headers=auth_header(self.supervisor),
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_нельзя_назначить_сотрудника_без_допуска(self):
        # Никитин — B2 (авионика). Утечка гидрожидкости требует B1.1.
        # Причина начальника смены не делает назначение законным (§3).
        call = self.create_call("VP-BZQ")
        response = self.assign(
            self.supervisor, call["id"], self.employee(NIKITIN)["id"], "он ближе"
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("B1.1", response.json()["detail"])


class TestNoSilentRelease(ApiTestCase):
    """
    Пути, которыми занятый инженер мог «освободиться» без закрытия вызова.

    Все три — того же семейства, что исходный дефект очереди: система
    считает вызов обслуживаемым, а к борту никто не едет.
    """

    def setUp(self):
        self.reset_database()
        self.supervisor = self.login("supervisor", "supervisor")

    def assign_sokolov(self):
        """Соколов назначен на VP-BZQ; возвращает вызов и id Соколова."""
        response = self.client.post(
            "/api/calls",
            json={
                "aircraft_id": self.board_id(self.supervisor, "VP-BZQ"),
                "defect_code": "hydraulic_leak",
            },
            headers=auth_header(self.supervisor),
        )
        call = response.json()
        employees = self.client.get(
            "/api/employees", headers=auth_header(self.supervisor)
        ).json()
        sokolov_id = next(e["id"] for e in employees if e["full_name"] == SOKOLOV)
        assigned = self.client.post(
            f"/api/calls/{call['id']}/assign",
            json={"employee_id": sokolov_id, "override_reason": "тестовый сценарий"},
            headers=auth_header(self.supervisor),
        )
        self.assertEqual(assigned.status_code, 200, assigned.text)
        return assigned.json(), sokolov_id

    def test_повторный_выход_на_смену_не_освобождает_занятого(self):
        # Перезапуск телефона — и «Заступить на смену» нажато снова.
        _, sokolov_id = self.assign_sokolov()
        engineer = self.login("sokolov", "engineer")
        response = self.client.post(
            f"/api/employees/{sokolov_id}/shift",
            json={"on_shift": True},
            headers=auth_header(engineer),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "assigned")

    def test_уйти_со_смены_с_незакрытым_вызовом_нельзя(self):
        _, sokolov_id = self.assign_sokolov()
        engineer = self.login("sokolov", "engineer")
        response = self.client.post(
            f"/api/employees/{sokolov_id}/shift",
            json={"on_shift": False},
            headers=auth_header(engineer),
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_повторный_подбор_не_переписывает_историю_назначения(self):
        # После назначения Соколов занят, и новый подбор предложил бы
        # другого. Поле «кого предлагала система» при этом меняться не должно.
        call, _ = self.assign_sokolov()
        before = call["suggested_employee_id"]

        self.client.post(f"/api/calls/{call['id']}/suggest", headers=auth_header(self.supervisor))

        calls = self.client.get("/api/calls", headers=auth_header(self.supervisor)).json()
        after = next(c for c in calls if c["id"] == call["id"])["suggested_employee_id"]
        self.assertEqual(after, before)


class TestSeededWorkingCall(ApiTestCase):
    """Занятость в тестовых данных подкреплена настоящим вызовом."""

    def test_у_занятого_гусева_есть_вызов(self):
        # Раньше Гусев был «занят» без вызова: диспетчер видел занятого
        # человека, но не видел, чем он занят, а очередь к нему никогда
        # бы не продвинулась — закрывать было нечего.
        token = self.login("dispatcher", "dispatcher")
        calls = self.client.get("/api/calls", headers=auth_header(token)).json()
        gusev_calls = [c for c in calls if c["assigned_employee_name"] == "Гусев Р. О."]

        self.assertEqual(len(gusev_calls), 1)
        self.assertEqual(gusev_calls[0]["board_number"], "VP-BKB")
        self.assertEqual(gusev_calls[0]["status"], "arrived")
