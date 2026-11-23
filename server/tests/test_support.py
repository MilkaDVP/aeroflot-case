"""
Тесты нескольких инженеров на одном вызове и переписки по вызову.

Второй инженер — отдельный вызов поддержки на тот же борт: у каждого свой
маршрут, свой статус в телефоне и своя машина. Срок прибытия исполняет
первый прибывший из группы.
"""

from tests.test_api import auth_header
from tests.test_stages import StageCase


class SupportCase(StageCase):
    """Общая подготовка: вызов на VP-BZQ с двумя подходящими инженерами."""

    def two_candidates(self, call_id):
        candidates = self.suggest(call_id)["candidates"]
        self.assertGreaterEqual(len(candidates), 2, "в данных нужен второй инженер с допуском")
        return candidates[0], candidates[1]

    def add_support(self, call_id, employee_id, token=None, reason=None):
        return self.client.post(
            f"/api/calls/{call_id}/support",
            json={"employee_id": employee_id, "override_reason": reason},
            headers=auth_header(token or self.dispatcher),
        )

    def team(self):
        """Назначены двое: первый кандидат — основной, второй — поддержка."""
        call = self.register()
        first, second = self.two_candidates(call["id"])
        self.assign(call["id"], first["employee_id"])
        response = self.add_support(call["id"], second["employee_id"])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), first, second

    def employee(self, employee_id):
        employees = self.client.get("/api/employees", headers=auth_header(self.dispatcher)).json()
        return next(item for item in employees if item["id"] == employee_id)


class TestSecondEngineer(SupportCase):
    """Диспетчер отправляет к борту ещё одного инженера."""

    def test_второй_инженер_виден_в_основном_вызове(self):
        call, first, second = self.team()
        self.assertEqual(call["assigned_employee_id"], first["employee_id"])
        self.assertEqual(len(call["support"]), 1)
        self.assertEqual(call["support"][0]["employee_id"], second["employee_id"])
        self.assertEqual(call["support"][0]["status"], "assigned")

    def test_второй_инженер_занят_и_не_предлагается_снова(self):
        call, _, second = self.team()
        self.assertNotEqual(self.employee(second["employee_id"])["status"], "free")
        other = self.register(board="VP-BES")
        free_ids = [item["employee_id"] for item in self.suggest(other["id"])["candidates"]]
        self.assertNotIn(second["employee_id"], free_ids)

    def test_выбор_второго_не_требует_причины(self):
        # Предложение системы относится к основному исполнителю; второго
        # диспетчер выбирает сам, и это не переопределение.
        call = self.register()
        candidates = self.suggest(call["id"])["candidates"]
        self.assign(call["id"], candidates[0]["employee_id"])
        last = candidates[-1]
        self.assertEqual(self.add_support(call["id"], last["employee_id"]).status_code, 200)

    def test_одного_инженера_нельзя_добавить_дважды(self):
        call, first, second = self.team()
        self.assertEqual(self.add_support(call["id"], first["employee_id"]).status_code, 409)
        self.assertEqual(self.add_support(call["id"], second["employee_id"]).status_code, 409)

    def test_без_основного_исполнителя_второго_не_добавить(self):
        call = self.register()
        first, _ = self.two_candidates(call["id"])
        self.assertEqual(self.add_support(call["id"], first["employee_id"]).status_code, 409)

    def test_без_допуска_вторым_не_назначить(self):
        call = self.register()
        result = self.suggest(call["id"])
        self.assign(call["id"], result["best"]["employee_id"])
        rejected = next(item for item in result["rejected"] if "отметки" in item["reason"])
        response = self.add_support(call["id"], rejected["employee_id"])
        self.assertEqual(response.status_code, 422, response.text)
        # Неудачная попытка не оставляет после себя пустого вызова поддержки.
        self.assertEqual(self.call(call["id"])["support"], [])


class TestTeamLifecycle(SupportCase):
    """Прибытие, закрытие и снятие в группе."""

    def support_call_id(self, call):
        return call["support"][0]["call_id"]

    def arrive(self, call_id):
        response = self.client.patch(
            f"/api/calls/{call_id}/status",
            json={"status": "arrived"},
            headers=auth_header(self.dispatcher),
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_срок_исполняет_первый_прибывший(self):
        # Второй приехал первым: основной ещё в пути, но тревоги по вызову
        # быть не должно, даже когда пятнадцать минут истекут.
        call, _, _ = self.team()
        self.arrive(self.support_call_id(call))
        self.age_call(call["id"], 30)

        control = self.control(call["id"])
        self.assertEqual(control["level"], "arrived", control["reason"])
        self.assertFalse(control["alert"])

    def test_опоздание_второго_не_считается_нарушением(self):
        call, _, _ = self.team()
        self.arrive(call["id"])
        self.age_call(self.support_call_id(call), 30)
        self.arrive(self.support_call_id(call))

        arrivals = [event for event in self.feed()["events"] if event["kind"] == "arrival"]
        self.assertEqual(len(arrivals), 1)
        self.assertEqual(arrivals[0]["verdict"], "good")

    def test_закрытие_основного_освобождает_обоих(self):
        call, first, second = self.team()
        self.arrive(call["id"])
        self.client.patch(
            f"/api/calls/{call['id']}/status",
            json={"status": "closed"},
            headers=auth_header(self.dispatcher),
        )
        self.assertEqual(self.employee(first["employee_id"])["status"], "free")
        self.assertEqual(self.employee(second["employee_id"])["status"], "free")
        self.assertEqual(self.call(self.support_call_id(call))["status"], "closed")

    def test_снятие_второго_оставляет_основного(self):
        call, first, second = self.team()
        response = self.client.post(
            f"/api/calls/{self.support_call_id(call)}/unassign",
            json={"reason": "нужен на другом борту"},
            headers=auth_header(self.dispatcher),
        )
        self.assertEqual(response.status_code, 200, response.text)
        fresh = response.json()
        self.assertEqual(fresh["id"], call["id"])
        self.assertEqual(fresh["support"], [])
        self.assertEqual(fresh["assigned_employee_id"], first["employee_id"])
        self.assertEqual(self.employee(second["employee_id"])["status"], "free")

    def test_вызов_поддержки_не_виден_без_исполнителя(self):
        # Снятый помощник не должен оставлять в списке «вызов без исполнителя».
        call, _, _ = self.team()
        support_id = self.support_call_id(call)
        self.client.post(
            f"/api/calls/{support_id}/unassign",
            json={"reason": "ошибка"},
            headers=auth_header(self.dispatcher),
        )
        active = self.client.get("/api/calls", headers=auth_header(self.dispatcher)).json()
        self.assertNotIn(support_id, [item["id"] for item in active])


class TestMessages(SupportCase):
    """Инженер пишет диспетчеру свободным текстом, диспетчер отвечает."""

    def test_сообщение_инженера_видно_на_вызове(self):
        call, engineer = self.sokolov_at_aircraft()
        response = self.me(engineer, "message", {"text": "Нужен  подъёмник, жду"})
        self.assertEqual(response.status_code, 200, response.text)

        messages = self.call(call["id"])["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], "Нужен подъёмник, жду")
        self.assertEqual(messages[0]["author_role"], "engineer")
        self.assertEqual(messages[0]["author_name"], "Соколов А. В.")

    def test_проблема_отличается_от_обычного_сообщения(self):
        call, engineer = self.sokolov_at_aircraft()
        self.me(engineer, "message", {"text": "Приступил"})
        self.me(engineer, "message", {"text": "Течь стойки", "is_problem": True})
        messages = self.call(call["id"])["messages"]
        self.assertEqual([item["is_problem"] for item in messages], [False, True])

    def test_проблема_без_ответа_не_теряется_в_длинной_переписке(self):
        from services.support import MESSAGES_SHOWN

        call, engineer = self.sokolov_at_aircraft()
        self.me(engineer, "message", {"text": "Течь стойки", "is_problem": True})
        for number in range(MESSAGES_SHOWN + 1):
            self.me(engineer, "message", {"text": f"Сообщение {number}"})
        self.assertTrue(any(item["is_problem"] for item in self.call(call["id"])["messages"]))

    def send_problem(self):
        call, engineer = self.sokolov_at_aircraft()
        self.me(engineer, "message", {"text": "Течь стойки", "is_problem": True})
        return call, engineer, self.call(call["id"])["messages"][-1]["id"]

    def dispatch_post(self, call, action, body):
        return self.client.post(f"/api/calls/{call['id']}/{action}", json=body,
                                headers=auth_header(self.dispatcher))

    def test_ответ_диспетчера_не_закрывает_проблему(self):
        call, _, _ = self.send_problem()
        self.dispatch_post(call, "message", {"text": "Принял"})
        self.assertIsNone(self.call(call["id"])["messages"][0]["resolved_at"])

    def test_проблему_отмечают_обе_стороны(self):
        call, engineer, problem = self.send_problem()
        done = self.dispatch_post(call, "problem", {"message_id": problem, "resolved": True})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertTrue(done.json()["messages"][0]["resolved_at"])
        again = self.me(engineer, "problem", {"message_id": problem, "resolved": False})
        self.assertIsNone(again.json()["messages"][0]["resolved_at"])
        self.me(engineer, "problem", {"message_id": problem, "resolved": True})
        self.assertEqual(self.call(call["id"])["messages"][0]["resolved_by"], "Соколов А. В.")

    def test_чужое_или_обычное_сообщение_проблемой_не_отметить(self):
        call, engineer, problem = self.send_problem()
        self.me(engineer, "message", {"text": "Обычное"})
        plain = self.call(call["id"])["messages"][-1]["id"]
        other = self.register(board="VP-BES")
        self.assertEqual(self.dispatch_post(
            call, "problem", {"message_id": plain, "resolved": True}).status_code, 404)
        self.assertEqual(self.dispatch_post(
            other, "problem", {"message_id": problem, "resolved": True}).status_code, 404)

    def test_прочитанное_запоминается_и_не_откатывается(self):
        call, engineer, first = self.send_problem()
        self.me(engineer, "message", {"text": "Ещё"})
        second = self.call(call["id"])["messages"][-1]["id"]
        self.assertEqual(self.call(call["id"])["read_message_id"], 0)
        self.assertEqual(self.dispatch_post(call, "read", {"message_id": second}).status_code, 200)
        self.dispatch_post(call, "read", {"message_id": first})
        self.assertEqual(self.call(call["id"])["read_message_id"], second)

    def test_диспетчер_отвечает_и_инженер_видит(self):
        call, engineer = self.sokolov_at_aircraft()
        self.me(engineer, "message", {"text": "Нужен подъёмник"})
        response = self.client.post(
            f"/api/calls/{call['id']}/message",
            json={"text": "Подъёмник выехал"},
            headers=auth_header(self.dispatcher),
        )
        self.assertEqual(response.status_code, 200, response.text)

        current = self.client.get("/api/me/current-call", headers=auth_header(engineer)).json()
        texts = [item["text"] for item in current["call"]["messages"]]
        self.assertEqual(texts, ["Нужен подъёмник", "Подъёмник выехал"])

    def test_пустое_сообщение_отклоняется(self):
        _, engineer = self.sokolov_at_aircraft()
        self.assertEqual(self.me(engineer, "message", {"text": ""}).status_code, 422)

    def test_без_вызова_писать_некуда(self):
        engineer = self.login("sokolov", "engineer")
        self.assertEqual(self.me(engineer, "message", {"text": "приём"}).status_code, 404)

    def test_переписка_группы_общая(self):
        # Сообщение второго инженера диспетчер читает на основном вызове.
        call, _, second = self.team()
        self.client.post(
            f"/api/calls/{call['support'][0]['call_id']}/message",
            json={"text": "Второму: возьмите стремянку"},
            headers=auth_header(self.dispatcher),
        )
        self.assertEqual(len(self.call(call["id"])["messages"]), 1)


class TestTravel(SupportCase):
    """Как добирается каждый: решает диспетчер, по умолчанию — как быстрее."""

    def assign_travel(self, call_id, employee_id, travel):
        return self.client.post(
            f"/api/calls/{call_id}/assign",
            json={"employee_id": employee_id, "travel": travel},
            headers=auth_header(self.dispatcher),
        )

    def support_travel(self, call_id, employee_id, travel):
        return self.client.post(
            f"/api/calls/{call_id}/support",
            json={"employee_id": employee_id, "travel": travel},
            headers=auth_header(self.dispatcher),
        )

    def test_пешком_машину_не_занимает(self):
        call = self.register()
        best = self.suggest(call["id"])["best"]
        self.assertIsNotNone(best["vehicle_call_sign"], "в данных лучший едет на машине")
        response = self.assign_travel(call["id"], best["employee_id"], "walk")
        self.assertEqual(response.status_code, 200, response.text)
        assigned = response.json()
        self.assertIsNone(assigned["vehicle_call_sign"])
        self.assertIsNone(assigned["pickup_node_id"])
        # Пешком дольше, чем расчёт системы через машину.
        self.assertGreater(assigned["eta_minutes"], best["minutes"])

    def test_второй_едет_в_машине_ответственного(self):
        call = self.register()
        first, second = self.two_candidates(call["id"])
        lead = self.assign(call["id"], first["employee_id"])
        self.assertIsNotNone(lead["vehicle_call_sign"])

        response = self.support_travel(call["id"], second["employee_id"], "with_lead")
        self.assertEqual(response.status_code, 200, response.text)
        member = response.json()["support"][0]
        self.assertEqual(member["vehicle_call_sign"], lead["vehicle_call_sign"])
        self.assertIsNotNone(member["pickup_node_id"])

    def test_один_пешком_другой_на_машине(self):
        call = self.register()
        first, second = self.two_candidates(call["id"])
        lead = self.assign(call["id"], first["employee_id"])
        member = self.support_travel(call["id"], second["employee_id"], "walk").json()["support"][0]
        self.assertIsNotNone(lead["vehicle_call_sign"])
        self.assertIsNone(member["vehicle_call_sign"])

    def test_с_ответственным_без_машины_нельзя(self):
        call = self.register()
        first, second = self.two_candidates(call["id"])
        self.assign_travel(call["id"], first["employee_id"], "walk")
        response = self.support_travel(call["id"], second["employee_id"], "with_lead")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.call(call["id"])["support"], [])

    def test_с_ответственным_только_для_второго(self):
        call = self.register()
        best = self.suggest(call["id"])["best"]
        self.assertEqual(self.assign_travel(call["id"], best["employee_id"], "with_lead").status_code, 422)

    def test_неизвестный_способ_отклоняется(self):
        call = self.register()
        best = self.suggest(call["id"])["best"]
        self.assertEqual(self.assign_travel(call["id"], best["employee_id"], "вертолёт").status_code, 422)
