"""
Тесты этапов работ, AOG, срочности и переключения исполнителя.

Правила взяты у экспертов Аэрофлота: статус AOG ставит инженер после
осмотра; начатый ремонт не прерывают; того, кто ждёт запчасти, можно
переключить на срочный борт — и он вернётся к прежнему.
"""

from tests.test_api import auth_header
from tests.test_mentor import MentorCase


class StageCase(MentorCase):
    """Общая подготовка: инженер у борта."""

    def me(self, token, path, body):
        return self.client.post(f"/api/me/{path}", json=body, headers=auth_header(token))

    def call(self, call_id):
        calls = self.client.get(
            "/api/calls?include_closed=true", headers=auth_header(self.dispatcher)
        ).json()
        return next(item for item in calls if item["id"] == call_id)

    def sokolov_at_aircraft(self):
        """Соколов назначен на VP-BZQ и подтвердил прибытие."""
        call = self.register()
        self.assign_best(call["id"])
        engineer = self.login("sokolov", "engineer")
        self.me(engineer, "status", {"action": "arrived"})
        return call, engineer

    def busy_entry(self, call_id, full_name):
        result = self.suggest(call_id, self.supervisor)
        return next(item for item in result["busy"] if item["full_name"] == full_name)


class TestStageAndAog(StageCase):
    """Этап работ и AOG выставляет инженер у борта."""

    def test_после_прибытия_этап_осмотр(self):
        call, _ = self.sokolov_at_aircraft()
        fresh = self.call(call["id"])
        self.assertEqual(fresh["work_stage"], "inspection")
        self.assertEqual(fresh["work_stage_title"], "осмотр")
        self.assertFalse(fresh["aog"])

    def test_инженер_меняет_этап(self):
        call, engineer = self.sokolov_at_aircraft()
        response = self.me(engineer, "stage", {"stage": "waiting_parts"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.call(call["id"])["work_stage_title"], "ждёт запчасти")

    def test_неизвестный_этап_отклоняется(self):
        _, engineer = self.sokolov_at_aircraft()
        self.assertEqual(self.me(engineer, "stage", {"stage": "обед"}).status_code, 422)

    def test_aog_ставит_инженер_у_борта(self):
        call, engineer = self.sokolov_at_aircraft()
        self.assertEqual(self.me(engineer, "aog", {"aog": True}).status_code, 200)
        self.assertTrue(self.call(call["id"])["aog"])

    def test_aog_до_прибытия_поставить_нельзя(self):
        # Статус определяется осмотром: в пути к борту ставить его не на чем.
        call = self.register()
        self.assign_best(call["id"])
        engineer = self.login("sokolov", "engineer")
        self.assertEqual(self.me(engineer, "aog", {"aog": True}).status_code, 409)
        self.assertEqual(self.me(engineer, "stage", {"stage": "repair"}).status_code, 409)

    def test_диспетчер_aog_поставить_не_может(self):
        self.sokolov_at_aircraft()
        response = self.me(self.dispatcher, "aog", {"aog": True})
        self.assertEqual(response.status_code, 403, response.text)

    def test_aog_и_срочные_идут_первыми(self):
        plain = self.register(board="RA-89001")
        urgent = self.client.post(
            "/api/calls",
            json={"aircraft_id": self.board_id(self.dispatcher, "VP-BES"),
                  "defect_code": "hydraulic_leak", "urgent": True},
            headers=auth_header(self.dispatcher),
        ).json()
        self.assertTrue(urgent["urgent"])
        aog_call, engineer = self.sokolov_at_aircraft()
        self.me(engineer, "aog", {"aog": True})

        order = [item["id"] for item in self.client.get(
            "/api/calls", headers=auth_header(self.dispatcher)).json()]
        self.assertEqual(order[0], aog_call["id"])
        self.assertEqual(order[1], urgent["id"])
        self.assertGreater(order.index(plain["id"]), 1)

    def test_срочность_меняет_диспетчер(self):
        call = self.register()
        response = self.client.post(
            f"/api/calls/{call['id']}/urgency", json={"urgent": True},
            headers=auth_header(self.dispatcher),
        )
        self.assertTrue(response.json()["urgent"])


class TestSwitch(StageCase):
    """Переключение исполнителя: можно с ожидания запчастей, нельзя с ремонта."""

    def test_занятый_показан_с_бортом_и_этапом(self):
        # В тестовых данных Гусев у борта VP-BKB ждёт запчасти.
        call = self.register()
        entry = self.busy_entry(call["id"], "Гусев Р. О.")
        self.assertEqual(entry["current_board"], "VP-BKB")
        self.assertEqual(entry["work_stage_title"], "ждёт запчасти")
        self.assertTrue(entry["switchable"])

    def test_ремонт_прерывать_нельзя(self):
        _, engineer = self.sokolov_at_aircraft()
        self.me(engineer, "stage", {"stage": "repair"})

        second = self.register(board="VP-BES")
        entry = self.busy_entry(second["id"], "Соколов А. В.")
        self.assertFalse(entry["switchable"])
        self.assertIn("прерывать нельзя", entry["switch_note"])

        response = self.client.post(
            f"/api/calls/{second['id']}/assign",
            json={"employee_id": entry["employee_id"], "override_reason": "срочно", "switch": True},
            headers=auth_header(self.supervisor),
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("ремонт начат", response.json()["detail"])

    def test_осмотр_прерывать_нельзя(self):
        self.sokolov_at_aircraft()
        second = self.register(board="VP-BES")
        entry = self.busy_entry(second["id"], "Соколов А. В.")
        self.assertFalse(entry["switchable"])

    def switch_sokolov(self):
        """Соколов ждёт запчасти на VP-BZQ и переключён на VP-BES."""
        first, engineer = self.sokolov_at_aircraft()
        self.me(engineer, "stage", {"stage": "waiting_parts"})
        second = self.register(board="VP-BES")
        response = self.client.post(
            f"/api/calls/{second['id']}/assign",
            json={"employee_id": self.employee_id("Соколов А. В."),
                  "override_reason": "ждёт запчасти, борт рядом", "switch": True},
            headers=auth_header(self.supervisor),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return first, second, engineer

    def test_переключение_с_ожидания_запчастей(self):
        first, second, engineer = self.switch_sokolov()

        self.assertEqual(self.call(second["id"])["status"], "assigned")
        paused = self.call(first["id"])
        self.assertEqual(paused["status"], "queued")
        self.assertEqual(paused["work_stage"], "waiting_parts")
        # Приостановленный вызов — не тревога: прибытие по нему уже было.
        self.assertEqual(paused["control"]["level"], "paused")
        self.assertFalse(paused["control"]["alert"])

        # В телефоне инженера — новый вызов.
        current = self.client.get("/api/me/current-call", headers=auth_header(engineer)).json()
        self.assertEqual(current["call"]["id"], second["id"])
        self.assertEqual(current["queued_count"], 1)

    def test_наставник_оценивает_переключение(self):
        _, second, _ = self.switch_sokolov()
        event = self.last("switch", second["id"])
        self.assertIn(event["verdict"], ("good", "fair"), event["text"])
        self.assertIn("VP-BZQ", event["text"])

    def test_после_закрытия_возвращается_к_прежнему_борту(self):
        first, second, engineer = self.switch_sokolov()
        self.me(engineer, "status", {"action": "arrived"})
        self.me(engineer, "status", {"action": "assessment", "mel_category": "A"})
        self.me(engineer, "status", {"action": "closed"})

        back = self.call(first["id"])
        self.assertEqual(back["status"], "assigned")
        self.assertEqual(back["work_stage"], "waiting_parts")
        self.assertFalse(back["control"]["alert"])

        # Возвращение к борту — не второе «прибытие по регламенту».
        self.me(engineer, "status", {"action": "arrived"})
        arrivals = [e for e in self.feed()["events"]
                    if e["kind"] == "arrival" and e["call_id"] == first["id"]]
        self.assertEqual(len(arrivals), 1)
        self.assertIn("в срок", self.call(first["id"])["control"]["reason"])

    def test_диспетчер_переключить_не_вправе(self):
        call = self.register()
        response = self.client.post(
            f"/api/calls/{call['id']}/assign",
            json={"employee_id": self.employee_id("Гусев Р. О."),
                  "override_reason": "срочно", "switch": True},
            headers=auth_header(self.dispatcher),
        )
        self.assertEqual(response.status_code, 403, response.text)
