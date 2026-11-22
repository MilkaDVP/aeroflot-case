"""
Смена по местному времени аэропорта.

Сервер хранит время в UTC; смена определяется по часовому поясу
аэропорта и границе дневной смены, которую задают начальник смены
и администраторы.
"""

import unittest
from datetime import datetime
from types import SimpleNamespace

from services import shift_clock
from tests.test_api import ApiTestCase, auth_header


def airport(tz=None, start=None, end=None):
    return SimpleNamespace(timezone=tz, day_shift_start=start, day_shift_end=end)


class TestShiftClock(unittest.TestCase):
    def test_по_умолчанию_москва_с_восьми_до_восьми(self):
        moscow = airport()
        # 05:30 UTC — 08:30 в Москве.
        self.assertEqual(shift_clock.shift_at(moscow, datetime(2026, 1, 10, 5, 30)), "day")
        # 04:59 UTC — 07:59 в Москве, ещё ночь.
        self.assertEqual(shift_clock.shift_at(moscow, datetime(2026, 1, 10, 4, 59)), "night")
        # 17:00 UTC — 20:00 в Москве, уже ночь.
        self.assertEqual(shift_clock.shift_at(moscow, datetime(2026, 1, 10, 17, 0)), "night")

    def test_другой_часовой_пояс(self):
        vladivostok = airport("Asia/Vladivostok")
        # 00:00 UTC — 10:00 во Владивостоке (UTC+10): там день, в Москве ночь.
        moment = datetime(2026, 1, 10, 0, 0)
        self.assertEqual(shift_clock.shift_at(vladivostok, moment), "day")
        self.assertEqual(shift_clock.shift_at(airport(), moment), "night")

    def test_дневная_смена_через_полночь(self):
        station = airport("UTC", "20:00", "08:00")
        self.assertEqual(shift_clock.shift_at(station, datetime(2026, 1, 10, 23, 0)), "day")
        self.assertEqual(shift_clock.shift_at(station, datetime(2026, 1, 10, 3, 0)), "day")
        self.assertEqual(shift_clock.shift_at(station, datetime(2026, 1, 10, 12, 0)), "night")

    def test_проверка_расписания(self):
        with self.assertRaises(ValueError):
            shift_clock.validate("Mars/Olympus", "08:00", "20:00")
        with self.assertRaises(ValueError):
            shift_clock.validate("Europe/Moscow", "8:00", "20:00")
        with self.assertRaises(ValueError):
            shift_clock.validate("Europe/Moscow", "08:00", "08:00")
        shift_clock.validate("Asia/Yekaterinburg", "07:30", "19:30")


class TestShiftScheduleApi(ApiTestCase):
    def setUp(self):
        self.reset_database()

    def put_schedule(self, token, icao, body):
        return self.client.put(
            f"/api/airports/{icao}/shift-schedule", json=body, headers=auth_header(token)
        )

    def schedule(self, tz="Asia/Vladivostok", start="07:00", end="19:00"):
        return {"timezone": tz, "day_start": start, "day_end": end}

    def test_вход_сам_выбирает_смену_по_часам(self):
        body = self.client.post(
            "/api/auth/login", json={"login": "dispatcher", "password": "dispatcher"}
        ).json()
        airport_brief = body["airports"][0]
        self.assertIn(airport_brief["current_shift"], ("day", "night"))
        me = self.client.get("/api/auth/me", headers=auth_header(body["token"])).json()
        self.assertEqual(me["shift"], airport_brief["current_shift"])

    def test_начальник_смены_меняет_расписание_своего_аэропорта(self):
        supervisor = self.login("supervisor", "supervisor")
        response = self.put_schedule(supervisor, "UUEE", self.schedule())
        self.assertEqual(response.status_code, 200, response.text)
        brief = response.json()
        self.assertEqual(brief["timezone"], "Asia/Vladivostok")
        self.assertEqual(brief["day_start"], "07:00")

        # Новое расписание видно при следующем входе.
        login = self.client.post(
            "/api/auth/login", json={"login": "dispatcher", "password": "dispatcher"}
        ).json()
        self.assertEqual(login["airports"][0]["timezone"], "Asia/Vladivostok")

    def test_чужой_аэропорт_нельзя(self):
        supervisor = self.login("supervisor", "supervisor")
        response = self.put_schedule(supervisor, "UUDD", self.schedule())
        self.assertEqual(response.status_code, 403, response.text)

    def test_технический_администратор_меняет_любой(self):
        admin = self.login("admin", "admin")
        response = self.put_schedule(admin, "UUDD", self.schedule())
        self.assertEqual(response.status_code, 200, response.text)

    def test_диспетчер_расписание_не_меняет(self):
        dispatcher = self.login("dispatcher", "dispatcher")
        response = self.put_schedule(dispatcher, "UUEE", self.schedule())
        self.assertEqual(response.status_code, 403, response.text)

    def test_неверный_часовой_пояс(self):
        supervisor = self.login("supervisor", "supervisor")
        response = self.put_schedule(supervisor, "UUEE", self.schedule(tz="Mars/Olympus"))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("часовой пояс", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
