"""Контракт /api/v1/track и полевой сценарий живого инженера."""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from clock import utc_now
from models.call import STATUS_ARRIVED, STATUS_ASSIGNED
from tests.test_api import auth_header
from tests.test_mentor import MentorCase


def iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_iso():
    return iso(datetime.now(timezone.utc).replace(tzinfo=None))


class TrackCase(MentorCase):
    def post_track(self, payload, headers=None):
        return self.client.post("/api/v1/track", json=payload, headers=headers or {})

    def point(self, obj, lat=55.97, lon=37.41, **extra):
        return {"obj": obj, "lat": lat, "lon": lon, "t": now_iso(), **extra}

    def employee(self, employee_id):
        rows = self.client.get("/api/employees", headers=auth_header(self.dispatcher)).json()
        return next(item for item in rows if item["id"] == employee_id)

    def call(self, call_id):
        rows = self.client.get(
            "/api/calls?include_closed=true", headers=auth_header(self.dispatcher)
        ).json()
        return next(item for item in rows if item["id"] == call_id)


class TestTrackContract(TrackCase):
    def test_одна_точка_инженера_принимается_без_авторизации(self):
        employee_id = self.employee_id("Соколов А. В.")
        measured = now_iso()
        response = self.post_track(
            {
                "obj": f"e-{employee_id:02d}",
                "lat": 55.8312,
                "lon": 37.6359,
                "t": measured,
                "acc": 5,
                "spd": 1.4,
            }
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {"accepted": True, "applied": True, "obj": f"e-{employee_id:02d}", "t": measured},
        )

        fresh = self.employee(employee_id)
        self.assertEqual(fresh["lat"], 55.8312)
        self.assertEqual(fresh["lon"], 37.6359)
        self.assertEqual(fresh["location_accuracy_m"], 5)
        self.assertEqual(fresh["location_speed_mps"], 1.4)
        self.assertEqual(fresh["location_source"], "gps")
        self.assertEqual(fresh["track_id"], f"e-{employee_id:02d}")

    def test_неизвестный_объект_синтаксически_принят_но_не_применён(self):
        response = self.post_track(self.point("e-999999"))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["accepted"])
        self.assertFalse(body["applied"])
        self.assertEqual(body["obj"], "e-999999")

    def test_пакет_обновляет_все_типы_и_сохраняет_порядок(self):
        employee_id = self.employee_id("Соколов А. В.")
        points = [
            self.point(f"e-{employee_id:02d}", 55.971, 37.411, acc=4),
            self.point("v-04", 55.972, 37.412, spd=3.5),
            self.point("RA-89001", 55.973, 37.413),
        ]
        before = self.client.get("/api/aircraft", headers=auth_header(self.dispatcher)).json()
        stand_before = next(item for item in before if item["board_number"] == "RA-89001")["stand_node_id"]

        response = self.post_track({"points": points})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual([item["obj"] for item in body], [point["obj"] for point in points])
        self.assertTrue(all(item["accepted"] and item["applied"] for item in body))

        vehicles = self.client.get("/api/vehicles", headers=auth_header(self.dispatcher)).json()
        vehicle = next(item for item in vehicles if item["track_id"] == "v-04")
        self.assertEqual(vehicle["lat"], 55.972)
        self.assertEqual(vehicle["location_speed_mps"], 3.5)

        aircraft = self.client.get("/api/aircraft", headers=auth_header(self.dispatcher)).json()
        board = next(item for item in aircraft if item["board_number"] == "RA-89001")
        self.assertEqual(board["lat"], 55.973)
        self.assertEqual(board["stand_node_id"], stand_before, "live GPS не меняет стоянку")

    def test_неверные_координаты_возвращают_400(self):
        invalid = [
            {"obj": "e-01", "lon": 37.4},
            {"obj": "e-01", "lat": 55.9},
            {"obj": "e-01", "lat": "55.9", "lon": 37.4},
            {"obj": "e-01", "lat": True, "lon": 37.4},
            {"obj": "e-01", "lat": 91, "lon": 37.4},
            {"obj": "e-01", "lat": 55.9, "lon": 181},
            {"obj": "e-01", "lat": 55.9, "lon": 37.4, "acc": -1},
            {"obj": "e-01", "lat": 55.9, "lon": 37.4, "t": "не время"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(self.post_track(payload).status_code, 400)

    def test_старая_точка_не_перезаписывает_новую(self):
        employee_id = self.employee_id("Соколов А. В.")
        current = utc_now()
        first = self.post_track(self.point(f"e-{employee_id:02d}", 55.98, 37.42, t=iso(current)))
        self.assertTrue(first.json()["applied"], first.text)

        old = self.post_track(
            self.point(f"e-{employee_id:02d}", 55.99, 37.43, t=iso(current - timedelta(seconds=1)))
        )
        self.assertEqual(old.status_code, 200, old.text)
        self.assertFalse(old.json()["applied"])
        self.assertEqual(self.employee(employee_id)["lat"], 55.98)

    def test_точка_из_будущего_отбрасывается(self):
        future = iso(utc_now() + timedelta(seconds=301))
        response = self.post_track(self.point("e-01", t=future))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["applied"])

    def test_физически_невозможный_скачок_отбрасывается(self):
        employee_id = self.employee_id("Соколов А. В.")
        current = self.employee(employee_id)
        start = utc_now()
        first = self.post_track(
            self.point(f"e-{employee_id:02d}", current["lat"], current["lon"], t=iso(start))
        )
        self.assertTrue(first.json()["applied"], first.text)

        jump = self.post_track(
            self.point(f"e-{employee_id:02d}", current["lat"] + 1, current["lon"], t=iso(start + timedelta(seconds=1)))
        )
        self.assertFalse(jump.json()["applied"], jump.text)

    def test_без_времени_сервер_возвращает_своё_utc(self):
        response = self.post_track({"obj": "e-01", "lat": 55.97, "lon": 37.41})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["applied"])
        self.assertRegex(body["t"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_необязательный_ключ_проверяется_если_задан(self):
        with patch.dict(os.environ, {"TRACK_API_KEY": "field-secret"}):
            self.assertEqual(self.post_track(self.point("e-01")).status_code, 401)
            self.assertEqual(
                self.post_track(self.point("e-01"), {"X-API-Key": "wrong"}).status_code, 401
            )
            response = self.post_track(self.point("e-01"), {"X-API-Key": "field-secret"})
            self.assertEqual(response.status_code, 200, response.text)


class TestTrackArrival(TrackCase):
    def accept(self, call_id, employee_id):
        engineer = self.login("sokolov", "engineer")
        response = self.client.post(
            "/api/me/status",
            json={"action": "accepted", "call_id": call_id},
            headers=auth_header(engineer),
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_прибытие_фиксируется_автоматически_в_геозоне(self):
        call = self.register()
        best = self.assign_best(call["id"])
        self.accept(call["id"], best["employee_id"])
        lat, lon = self.stand_point(call)

        response = self.post_track(self.point(f"e-{best['employee_id']:02d}", lat, lon))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["applied"])
        self.assertEqual(self.call(call["id"])["status"], STATUS_ARRIVED)
        self.assertEqual(self.employee(best["employee_id"])["status"], "busy")
        self.assertIsNotNone(self.last("arrival", call["id"]))

    def test_без_принятия_заявки_автоматической_работы_нет(self):
        call = self.register()
        best = self.assign_best(call["id"])
        lat, lon = self.stand_point(call)

        response = self.post_track(self.point(f"e-{best['employee_id']:02d}", lat, lon))
        self.assertTrue(response.json()["applied"], response.text)
        self.assertEqual(self.call(call["id"])["status"], STATUS_ASSIGNED)


if __name__ == "__main__":
    import unittest

    unittest.main()
