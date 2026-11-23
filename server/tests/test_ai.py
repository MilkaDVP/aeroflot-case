"""
ИИ-диспетчер: план назначений по ждущим вызовам.

GigaChat в тестах не вызывается: план строит встроенный подбор, а
пояснение подменяется — проверяется, что без GigaChat экран работает
на шаблоне, а с ним берёт его текст.
"""

from unittest import mock

from tests.test_api import auth_header
from tests.test_deadline import DeadlineCase


class TestAiPlan(DeadlineCase):
    def plan(self, token=None, explain=False):
        response = self.client.get(
            f"/api/ai/plan?explain={str(explain).lower()}",
            headers=auth_header(token or self.dispatcher),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def item(self, plan, call_id):
        return next(item for item in plan["items"] if item["call_id"] == call_id)

    def test_вызов_с_успевающим_кандидатом_можно_назначить(self):
        self.register("VP-BZQ")
        ready = [i for i in self.plan()["items"] if i["verdict"] == "assign"]
        self.assertTrue(ready, "в тестовых данных должен быть вызов, на который успевают")
        item = ready[0]
        # Это тот же, кого предложит обычный подбор, — назначение пройдёт
        # у обычного диспетчера без переопределения и без начальника смены.
        best = self.suggest(item["call_id"])["best"]
        self.assertEqual(item["employee_id"], best["employee_id"])
        self.assertTrue(best["within_regulation"])
        self.assign(item["call_id"], item["employee_id"])

    def test_решите_сами_объясняет_почему(self):
        self.register("VP-BZQ")
        for item in self.plan()["items"]:
            self.assertTrue(item["reason"])
            if item["verdict"] != "nobody":
                self.assertIsNotNone(item["employee_id"])

    def test_один_инженер_на_один_вызов(self):
        first = self.register("VP-BZQ")
        second = self.register("VP-BES")
        plan = self.plan()
        ids = [i["employee_id"] for i in plan["items"] if i["call_id"] in (first["id"], second["id"])]
        self.assertEqual(len([i for i in ids if i is not None]), len({i for i in ids if i is not None}))

    def test_срочный_первым(self):
        normal = self.register("VP-BZQ")
        urgent = self.register("VP-BES")
        self.client.post(
            f"/api/calls/{urgent['id']}/urgency", json={"urgent": True}, headers=auth_header(self.dispatcher)
        )
        order = [i["call_id"] for i in self.plan()["items"]]
        self.assertLess(order.index(urgent["id"]), order.index(normal["id"]))

    def test_чужой_захваченный_вызов_не_в_плане(self):
        call = self.register("VP-BZQ")  # заводит dispatcher — вызов у него в работе
        other = self.login("dispatcher2", "dispatcher")
        ids = [i["call_id"] for i in self.plan(token=other)["items"]]
        self.assertNotIn(call["id"], ids)

    def test_план_ничего_не_меняет(self):
        call = self.register("VP-BZQ")
        before = self.control(call["id"])
        self.plan()
        calls = self.client.get("/api/calls", headers=auth_header(self.dispatcher)).json()
        after = next(item for item in calls if item["id"] == call["id"])
        self.assertIsNone(after["assigned_employee_id"])
        self.assertEqual(after["control"]["level"], before["level"])

    def test_без_gigachat_пояснение_по_правилам(self):
        self.register("VP-BZQ")
        with mock.patch("services.gigachat.complete", return_value=None):
            plan = self.plan(explain=True)
        self.assertEqual(plan["summary_source"], "rules")
        self.assertTrue(plan["summary"])

    def test_с_gigachat_берётся_его_текст(self):
        self.register("VP-BZQ")
        with mock.patch("services.gigachat.complete", return_value="Назначьте Соколова на VP-BZQ."):
            plan = self.plan(explain=True)
        self.assertEqual(plan["summary_source"], "gigachat")
        self.assertEqual(plan["summary"], "Назначьте Соколова на VP-BZQ.")

    def test_инженеру_недоступно(self):
        engineer = self.login("sokolov", "engineer")
        response = self.client.get("/api/ai/plan?explain=false", headers=auth_header(engineer))
        self.assertEqual(response.status_code, 403, response.text)

    def test_в_плане_есть_маршрут_для_карты(self):
        self.register("VP-BZQ")
        item = next(i for i in self.plan()["items"] if i["employee_id"])
        self.assertTrue(item["stand_node_id"])
        self.assertGreater(len(item["route_node_ids"]), 0)

    def ask(self, question):
        response = self.client.post(
            "/api/ai/ask", json={"question": question}, headers=auth_header(self.dispatcher)
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_вопрос_без_gigachat(self):
        self.register("VP-BZQ")
        answer = self.ask("Что горит?")
        self.assertEqual(answer["source"], "rules")
        self.assertIn("GigaChat", answer["answer"])

    def test_вопрос_уходит_в_gigachat_с_обстановкой(self):
        self.register("VP-BZQ")
        with mock.patch("services.gigachat.configured", return_value=True),                 mock.patch("services.gigachat.complete", return_value="Отправьте Соколова.") as complete:
            answer = self.ask("Кого на VP-BZQ?")
        self.assertEqual(answer, {"answer": "Отправьте Соколова.", "source": "gigachat"})
        prompt = complete.call_args[0][1]
        self.assertIn("VP-BZQ", prompt)
        self.assertIn("Кого на VP-BZQ?", prompt)

    def test_пустой_вопрос_отклоняется(self):
        response = self.client.post(
            "/api/ai/ask", json={"question": ""}, headers=auth_header(self.dispatcher)
        )
        self.assertEqual(response.status_code, 422)

    def test_назначить_всех_по_порядку_у_обычного_диспетчера(self):
        # Два борта рядом — на оба лучшим может оказаться один человек.
        # План раздаёт разных, «Назначить всех» идёт по порядку срочности,
        # и обычный диспетчер назначает всех без переопределения.
        self.register("VP-BZQ")
        self.register("VP-BES")
        plan = self.plan()
        for item in [i for i in plan["items"] if i["employee_id"]]:
            response = self.client.post(
                f"/api/calls/{item['call_id']}/assign",
                json={"employee_id": item["employee_id"], "override_reason": None, "travel": "auto"},
                headers=auth_header(self.dispatcher),
            )
            self.assertEqual(response.status_code, 200, f"{item['board_number']}: {response.text}")

    def test_признак_горит_и_порядок_в_плане(self):
        self.register("VP-BZQ")
        for item in self.plan()["items"]:
            self.assertIn("alert", item)
            self.assertIn("needs_order", item)
            if item["needs_order"]:
                self.assertEqual(item["verdict"], "check")

    def test_в_вопрос_уходит_вся_обстановка(self):
        call = self.register("VP-BZQ")
        self.assign(call["id"], self.suggest(call["id"])["best"]["employee_id"])
        with mock.patch("services.gigachat.configured", return_value=True),                 mock.patch("services.gigachat.complete", return_value="Ок.") as complete:
            self.ask("Что сейчас происходит?")
        prompt = complete.call_args[0][1]
        self.assertIn("В работе:", prompt)
        self.assertIn("VP-BZQ", prompt)
        self.assertIn("Люди смены", prompt)
        self.assertIn("Свободных машин", prompt)
