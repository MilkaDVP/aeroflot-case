"""
Тесты Наставника — разбора действий диспетчера.

Каждое правило оценки проверяется на тестовых данных стенда: вызов к A320
на стоянке 31, где Соколов на своей машине успевает за 6,2 мин, Морозов
через свободную ТМ-04 — за 9,5 мин, а Гусев занят на другом борте.
Время моделируется сдвигом момента регистрации вызова назад.
"""

from models.employee import Employee
from services import mentor
from tests.test_api import auth_header
from tests.test_deadline import DeadlineCase


class MentorCase(DeadlineCase):
    """Общая подготовка и доступ к ленте."""

    def feed(self, token=None):
        response = self.client.get(
            "/api/shift/feed", headers=auth_header(token or self.dispatcher)
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def last(self, kind, call_id):
        """Последнее замечание данного вида по вызову."""
        for event in self.feed()["events"]:
            if event["kind"] == kind and event["call_id"] == call_id:
                return event
        self.fail(f"нет замечания {kind} по вызову {call_id}")


class TestAssignment(MentorCase):
    """Оценка назначения свободного исполнителя."""

    def test_лучший_кандидат_сразу_хорошо(self):
        call = self.register()
        self.assign_best(call["id"])

        event = self.last("assign", call["id"])
        self.assertEqual(event["verdict"], "good", event["text"])
        self.assertIn("Соколов", event["text"])
        self.assertIn("ТМ-01", event["text"])
        self.assertIn("запас до срока 8 мин", event["text"])
        self.assertEqual(event["board_number"], "VP-BZQ")
        self.assertEqual(event["actor_name"], "Смирнова О. В.")

    def test_поздно_назначенный_не_очень(self):
        # Исполнитель верный, но четыре минуты вызов висел без решения.
        call = self.register()
        self.age_call(call["id"], 4)
        self.assign_best(call["id"])

        event = self.last("assign", call["id"])
        self.assertEqual(event["verdict"], "fair", event["text"])
        self.assertIn("через 4 мин после поступления", event["text"])

    def test_никто_не_успевал_не_вина_диспетчера(self):
        # 10 минут прошло, лучшему ехать 6,2: опоздание неизбежно, и выбор
        # лучшего из возможных — не «плохо».
        call = self.register()
        self.age_call(call["id"], 10)
        self.assign_best(call["id"])

        event = self.last("assign", call["id"])
        self.assertEqual(event["verdict"], "fair", event["text"])
        self.assertIn("не успевал никто", event["text"])

    def test_впритык_не_очень(self):
        # В тестовых данных нет кандидата с дорогой в 13–15 минут, поэтому
        # правило проверяется напрямую: запас 1,5 мин при быстром назначении.
        verdict, text = mentor.judge_plain(Employee(full_name="Иванов И. И."), None, 1.5, 1.0)
        self.assertEqual(verdict, "fair")
        self.assertIn("запас до срока всего 1 мин", text)
        self.assertIn("Держите вызов на контроле", text)


class TestOverride(MentorCase):
    """Переопределение решения системы: во сколько минут оно обошлось."""

    def test_переопределение_с_потерей_минут_не_очень(self):
        call = self.register()
        morozov = self.employee_id("Морозов Д. С.")
        self.assign(call["id"], morozov, "Соколов нужен на другом борте", self.supervisor)

        event = self.last("assign", call["id"])
        self.assertEqual(event["verdict"], "fair", event["text"])
        self.assertIn("позже, чем Соколов", event["text"])
        self.assertIn("Причина: Соколов нужен на другом борте", event["text"])

        summary = self.feed()["summary"]
        self.assertEqual(summary["overrides"], 1)
        # 9,5 против 6,2 мин.
        self.assertAlmostEqual(summary["override_cost_min"], 3.3, delta=0.3)

    def test_переопределение_со_срывом_срока_плохо(self):
        # Через 7 минут Соколов ещё успевает (13,2), Морозов уже нет (16,5).
        call = self.register()
        self.age_call(call["id"], 7)
        morozov = self.employee_id("Морозов Д. С.")
        self.assign(call["id"], morozov, "так решил", self.supervisor)

        event = self.last("assign", call["id"])
        self.assertEqual(event["verdict"], "bad", event["text"])
        self.assertIn("Морозов Д. С. опоздает", event["text"])
        self.assertIn("Соколов А. В. (на ТМ-01) успевал за 6 мин", event["text"])


class TestQueue(MentorCase):
    """Очередь к занятому: был ли свободный, который успевал."""

    def test_очередь_при_свободном_успевающем_плохо(self):
        call = self.register()
        gusev = self.employee_id("Гусев Р. О.")
        self.assign(call["id"], gusev, "ждём именно его", self.supervisor)

        event = self.last("queue", call["id"])
        self.assertEqual(event["verdict"], "bad", event["text"])
        self.assertIn("в очередь, исполнитель — Гусев Р. О.", event["text"])
        self.assertIn("срыв срока", event["text"])
        self.assertIn("свободный Соколов А. В. (на ТМ-01) успевал за 6 мин", event["text"])

    def test_очередь_без_свободных_не_очень(self):
        # На B737 допуск B1.1 только у занятого Гусева: выбора нет.
        call = self.register(board="VP-BKB")
        gusev = self.employee_id("Гусев Р. О.")
        self.assign(call["id"], gusev, "других с допуском нет", self.supervisor)

        event = self.last("queue", call["id"])
        self.assertEqual(event["verdict"], "fair", event["text"])
        self.assertIn("Свободных с допуском нет", event["text"])


class TestUnassign(MentorCase):
    """Снятие исполнителя: вовремя или зря."""

    def unassign(self, call_id, reason):
        response = self.client.post(
            f"/api/calls/{call_id}/unassign",
            json={"reason": reason},
            headers=auth_header(self.dispatcher),
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_снят_успевающий_не_очень(self):
        call = self.register()
        self.assign_best(call["id"])
        self.unassign(call["id"], "передумал")

        event = self.last("unassign", call["id"])
        self.assertEqual(event["verdict"], "fair", event["text"])
        self.assertIn("хотя по прогнозу укладывался", event["text"])

    def test_снят_опаздывающий_хорошо(self):
        call = self.register()
        self.assign_best(call["id"])
        self.age_call(call["id"], 9)
        self.unassign(call["id"], "машина не завелась")

        event = self.last("unassign", call["id"])
        self.assertEqual(event["verdict"], "good", event["text"])
        self.assertIn("снят с вызова VP-BZQ вовремя", event["text"])
        self.assertIn("Причина: машина не завелась", event["text"])


class TestArrival(MentorCase):
    """Прибытие: итог по вызову."""

    def arrive(self):
        engineer = self.login("sokolov", "engineer")
        self.client.post("/api/me/status", json={"action": "arrived"}, headers=auth_header(engineer))

    def test_прибытие_в_срок_хорошо(self):
        call = self.register()
        self.assign_best(call["id"])
        self.age_call(call["id"], 6)
        self.arrive()

        event = self.last("arrival", call["id"])
        self.assertEqual(event["verdict"], "good", event["text"])
        self.assertIn("через 6 мин после вызова", event["text"])

    def test_опоздание_плохо(self):
        call = self.register()
        self.assign_best(call["id"])
        self.age_call(call["id"], 18)
        self.arrive()

        event = self.last("arrival", call["id"])
        self.assertEqual(event["verdict"], "bad", event["text"])
        self.assertIn("с опозданием на 3 мин", event["text"])
        self.assertIn("регламент нарушен", event["text"])


class TestObservation(MentorCase):
    """Замечания по обстановке без чьего-либо действия."""

    def test_вызов_без_исполнителя_замечается_один_раз(self):
        call = self.register()
        self.assertFalse(
            [e for e in self.feed()["events"] if e["call_id"] == call["id"]],
            "свежий вызов замечаний не заслуживает",
        )

        self.age_call(call["id"], 4)
        first = self.last("watch_unassigned", call["id"])
        self.assertEqual(first["verdict"], "fair")
        self.assertIn("без исполнителя уже 4 мин", first["text"])

        # Повторные опросы ленты не плодят то же замечание.
        self.feed()
        same = [
            e for e in self.feed()["events"]
            if e["kind"] == "watch_unassigned" and e["call_id"] == call["id"]
        ]
        self.assertEqual(len(same), 1)

    def test_исполнитель_не_успевает_плохо(self):
        call = self.register()
        self.assign_best(call["id"])
        self.age_call(call["id"], 9)

        event = self.last("watch_late", call["id"])
        self.assertEqual(event["verdict"], "bad", event["text"])
        self.assertIn("VP-BZQ", event["text"])

    def test_срыв_в_очереди_не_дублируется_наблюдением(self):
        # Срыв вызова в очереди оценён при постановке; второго замечания
        # о том же от наблюдения быть не должно.
        call = self.register()
        self.assign(call["id"], self.employee_id("Гусев Р. О."), "ждём его", self.supervisor)

        kinds = [e["kind"] for e in self.feed()["events"] if e["call_id"] == call["id"]]
        self.assertEqual(kinds, ["queue"])


class TestSummaryAndAccess(MentorCase):
    """Итог смены и разграничение доступа."""

    def test_итог_смены_в_цифрах(self):
        empty = self.feed()["summary"]
        self.assertEqual(empty["grade"], "Оценивать пока нечего")
        self.assertIsNone(empty["in_time_percent"])

        call = self.register()
        self.age_call(call["id"], 2)
        self.assign_best(call["id"])
        engineer = self.login("sokolov", "engineer")
        self.client.post("/api/me/status", json={"action": "arrived"}, headers=auth_header(engineer))

        summary = self.feed()["summary"]
        self.assertEqual(summary["assignments"], 1)
        self.assertAlmostEqual(summary["avg_minutes_to_assign"], 2.0, delta=0.2)
        self.assertEqual(summary["arrivals"], 1)
        self.assertEqual(summary["in_time_percent"], 100)
        self.assertEqual(summary["verdicts"], {"good": 2, "fair": 0, "bad": 0})
        self.assertEqual(summary["grade"], "Смена идёт хорошо")

    def test_нарушение_меняет_общую_оценку(self):
        call = self.register()
        self.assign(call["id"], self.employee_id("Гусев Р. О."), "ждём его", self.supervisor)
        self.assertEqual(self.feed()["summary"]["grade"], "Есть нарушения регламента")

    def test_инженеру_лента_недоступна(self):
        engineer = self.login("sokolov", "engineer")
        response = self.client.get("/api/shift/feed", headers=auth_header(engineer))
        self.assertEqual(response.status_code, 403, response.text)

    def test_другой_аэропорт_чужих_замечаний_не_видит(self):
        call = self.register()
        self.assign_best(call["id"])
        self.assertTrue(self.feed()["events"])

        domodedovo = self.login("dispatcher_uudd", "dispatcher", airport="UUDD")
        self.assertEqual(self.feed(domodedovo)["events"], [])
