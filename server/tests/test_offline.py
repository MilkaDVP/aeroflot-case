"""
Тесты отложенных действий инженера.

Телефон без связи копит действия и отправляет их позже. Сервер обязан
применить каждое один раз, засчитать по исходному времени и не тронуть
вызов, который с инженера уже сняли.
"""

from tests.test_api import auth_header
from tests.test_mentor import MentorCase


class OfflineCase(MentorCase):
    def setUp(self):
        super().setUp()
        self.call = self.register()
        self.assign_best(self.call["id"])
        self.engineer = self.login("sokolov", "engineer")

    def act(self, action, **extra):
        return self.client.post(
            "/api/me/status", json={"action": action, **extra},
            headers=auth_header(self.engineer),
        )

    def fresh(self):
        calls = self.client.get(
            "/api/calls?include_closed=true", headers=auth_header(self.dispatcher)
        ).json()
        return next(item for item in calls if item["id"] == self.call["id"])


class TestDelayedActions(OfflineCase):
    def test_повтор_действия_не_применяется_дважды(self):
        first = self.act("arrived", action_id="a1b2c3d4-0001", call_id=self.call["id"])
        again = self.act("arrived", action_id="a1b2c3d4-0001", call_id=self.call["id"])
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["status"], "arrived")

        arrivals = [e for e in self.feed()["events"] if e["kind"] == "arrival"]
        self.assertEqual(len(arrivals), 1)

    def test_прибытие_засчитывается_по_времени_нажатия(self):
        # Инженер прибыл на 10-й минуте без связи, действие дошло на 18-й:
        # срок исполнен, регламент не нарушен.
        self.age_call(self.call["id"], 18)
        response = self.act(
            "arrived", action_id="a1b2c3d4-0002", call_id=self.call["id"], age_s=8 * 60
        )
        self.assertEqual(response.status_code, 200, response.text)

        event = self.last("arrival", self.call["id"])
        self.assertEqual(event["verdict"], "good", event["text"])
        self.assertIn("через 10 мин после вызова", event["text"])
        self.assertIn("в срок", self.fresh()["control"]["reason"])

    def test_пачка_накопленных_действий_ложится_по_порядку(self):
        for index, action in enumerate(("accepted", "arrived", "assessment", "closed")):
            response = self.act(
                action, action_id=f"batch-000{index}", call_id=self.call["id"],
                age_s=(3 - index) * 60,
                **({"mel_category":"C"} if action == "assessment" else {}),
            )
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.fresh()["status"], "closed")

    def test_decline_returns_call_and_is_idempotent(self):
        fields = {"call_id": self.call["id"], "action_id": "declined-001"}
        for _ in range(2):
            result = self.act("declined", **fields)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["status"], "new")
            self.assertIsNone(result.json()["assigned_employee_id"])

    def test_arrival_requires_ten_seconds_inside_aircraft_radius(self):
        from datetime import timedelta
        from unittest.mock import patch
        from clock import utc_now
        self.act("accepted")
        headers = auth_header(self.engineer)
        payload = self.client.get("/api/me/current-call", headers=headers).json()
        point = payload["aircraft_point"]
        employee = payload["call"]["assigned_employee_id"]
        start = utc_now()
        for seconds, offset, expected in [(0, 0, "accepted"), (8, .001, "accepted"),
                                          (9, 0, "accepted"), (18, 0, "accepted"), (19, 0, "arrived")]:
            with patch("services.assignment.utc_now", return_value=start + timedelta(seconds=seconds)):
                response = self.client.post(f"/api/employees/{employee}/location", headers=headers,
                                            json={"source":"gps", "lat":point["lat"] + offset, "lon":point["lon"]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.fresh()["status"], expected)

    def test_mel_category_and_equipment(self):
        self.assertEqual(self.act("equipment", missing_tools=True).status_code, 409)
        self.act("accepted")
        self.act("arrived")
        self.assertEqual(self.act("closed").status_code, 409)
        self.assertEqual(self.act("assessment").status_code, 422)
        self.assertEqual(self.act("assessment", mel_category="E").status_code, 422)
        for category in "ABCD":
            result = self.act("assessment", mel_category=category)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["mel_category"], category)
        self.assertTrue(self.act("equipment", missing_tools=True).json()["missing_tools"])
        self.assertFalse(self.act("equipment", missing_tools=False).json()["missing_tools"])
        self.assertEqual(self.act("closed").status_code, 200)

    def test_снятый_вызов_действие_не_трогает(self):
        # Пока телефон был без связи, диспетчер снял исполнителя.
        self.client.post(
            f"/api/calls/{self.call['id']}/unassign", json={"reason": "нет связи с инженером"},
            headers=auth_header(self.dispatcher),
        )
        response = self.act("arrived", action_id="a1b2c3d4-0003", call_id=self.call["id"])
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.fresh()["status"], "new")

    def test_действие_для_другого_вызова_отклоняется(self):
        response = self.act("arrived", action_id="a1b2c3d4-0004", call_id=self.call["id"] + 999)
        self.assertEqual(response.status_code, 409, response.text)

    def test_порядок_действий_проверяется(self):
        # Завершить работы, не прибыв к борту, нельзя.
        self.assertEqual(self.act("closed").status_code, 409)

    def test_старый_клиент_без_идентификатора_работает(self):
        self.assertEqual(self.act("accepted").status_code, 200)
        self.assertEqual(self.act("arrived").status_code, 200)
