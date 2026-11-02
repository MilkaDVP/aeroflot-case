"""
Тесты живого контроля регламента прибытия.

Пятнадцать минут — договорное условие со штрафами. Проверяется, что система
не ограничивается проверкой в момент назначения: прогноз пересчитывается
по текущим координатам исполнителя, и как только срок под угрозой,
вызов получает тревогу с понятной причиной.

Течение времени моделируется сдвигом момента регистрации вызова в базе
назад: «вызов поступил 9 минут назад, а инженер так и не сдвинулся».
Часы при этом не подменяются — проверяется та же логика, что на стенде.
"""

import math
from datetime import timedelta

from clock import utc_now
from database import SessionLocal
from models.call import Call
from models.employee import Employee
from services.deadline import minutes_text, minutes_until, overrun_text
from tests.test_api import ApiTestCase, auth_header


class DeadlineCase(ApiTestCase):
    """Общая подготовка: чистая база, диспетчер и начальник смены."""

    def setUp(self):
        self.reset_database()
        self.dispatcher = self.login("dispatcher", "dispatcher")
        self.supervisor = self.login("supervisor", "supervisor")

    # --- вспомогательное ---

    def register(self, board="VP-BZQ", defect="hydraulic_leak"):
        response = self.client.post(
            "/api/calls",
            json={"aircraft_id": self.board_id(self.dispatcher, board), "defect_code": defect},
            headers=auth_header(self.dispatcher),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def suggest(self, call_id, token=None):
        return self.client.post(
            f"/api/calls/{call_id}/suggest", headers=auth_header(token or self.dispatcher)
        ).json()

    def assign(self, call_id, employee_id, reason=None, token=None):
        response = self.client.post(
            f"/api/calls/{call_id}/assign",
            json={"employee_id": employee_id, "override_reason": reason},
            headers=auth_header(token or self.dispatcher),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assign_best(self, call_id):
        best = self.suggest(call_id)["best"]
        self.assertIsNotNone(best)
        self.assign(call_id, best["employee_id"])
        return best

    def control(self, call_id):
        calls = self.client.get("/api/calls", headers=auth_header(self.dispatcher)).json()
        return next(item for item in calls if item["id"] == call_id)["control"]

    def employee_id(self, full_name):
        employees = self.client.get("/api/employees", headers=auth_header(self.dispatcher)).json()
        return next(item for item in employees if item["full_name"] == full_name)["id"]

    def age_call(self, call_id, minutes):
        """Вызов «поступил» minutes минут назад."""
        db = SessionLocal()
        try:
            call = db.get(Call, call_id)
            call.created_at = utc_now() - timedelta(minutes=minutes)
            db.commit()
        finally:
            db.close()

    def edit_employee(self, employee_id, **fields):
        db = SessionLocal()
        try:
            employee = db.get(Employee, employee_id)
            for name, value in fields.items():
                setattr(employee, name, value)
            db.commit()
        finally:
            db.close()

    def send_location(self, employee_id, lat, lon, source="manual", fix_age_s=None, token=None):
        return self.client.post(
            f"/api/employees/{employee_id}/location",
            json={"lat": lat, "lon": lon, "source": source, "fix_age_s": fix_age_s},
            headers=auth_header(token or self.dispatcher),
        )

    def stand_point(self, call):
        graph = self.client.get(
            "/api/airports/UUEE", headers=auth_header(self.dispatcher)
        ).json()
        node = next(item for item in graph["nodes"] if item["id"] == call["stand_node_id"])
        return node["lat"], node["lon"]


class TestEnRoute(DeadlineCase):
    """Исполнитель назначен и едет: прогноз от его текущих координат."""

    def test_сразу_после_назначения_успевает(self):
        call = self.register()
        best = self.assign_best(call["id"])

        control = self.control(call["id"])
        self.assertEqual(control["level"], "ok", control["reason"])
        self.assertFalse(control["alert"])
        # Прогноз от тех же координат совпадает с расчётом при назначении.
        self.assertAlmostEqual(control["margin_min"], 15 - best["minutes"], delta=0.3)
        self.assertIn("Успевает", control["reason"])

    def test_инженер_стоит_на_месте_и_срок_уходит(self):
        # Вызов поступил 9 минут назад, а Соколову по-прежнему ехать 6,2 мин:
        # 9 + 6,2 > 15. В момент назначения всё было зелёным.
        call = self.register()
        self.assign_best(call["id"])
        self.age_call(call["id"], 9)

        control = self.control(call["id"])
        self.assertEqual(control["level"], "late", control["reason"])
        self.assertTrue(control["alert"])
        self.assertIn("Не успевает", control["reason"])
        self.assertIsNotNone(control["advice"])
        self.assertLess(control["margin_min"], 0)

    def test_впритык_жёлтый_без_тревоги(self):
        # 7,5 + 6,2 = 13,7: запас 1,3 мин — меньше порога в 2 минуты.
        call = self.register()
        self.assign_best(call["id"])
        self.age_call(call["id"], 7.5)

        control = self.control(call["id"])
        self.assertEqual(control["level"], "warn", control["reason"])
        self.assertFalse(control["alert"])

    def test_срок_истёк_а_исполнитель_в_пути(self):
        call = self.register()
        self.assign_best(call["id"])
        self.age_call(call["id"], 16)

        control = self.control(call["id"])
        self.assertEqual(control["level"], "overdue", control["reason"])
        self.assertTrue(control["alert"])
        self.assertLess(control["minutes_left"], 0)

    def test_приблизился_к_борту_и_снова_успевает(self):
        # 10 минут прошло, но инженер уже у стоянки — опасности нет.
        call = self.register()
        best = self.assign_best(call["id"])
        self.age_call(call["id"], 10)
        self.assertEqual(self.control(call["id"])["level"], "late")

        lat, lon = self.stand_point(call)
        self.send_location(best["employee_id"], lat, lon)

        control = self.control(call["id"])
        self.assertEqual(control["level"], "ok", control["reason"])

    def test_отставание_от_плана_видно(self):
        # Инженер ушёл не туда. Точка — исходное место, отражённое от борта:
        # она заведомо дальше от стоянки, чем при назначении.
        call = self.register()
        best = self.assign_best(call["id"])
        employees = self.client.get("/api/employees", headers=auth_header(self.dispatcher)).json()
        start = next(item for item in employees if item["id"] == best["employee_id"])
        stand_lat, stand_lon = self.stand_point(call)
        self.send_location(
            best["employee_id"],
            2 * start["lat"] - stand_lat,
            2 * start["lon"] - stand_lon,
        )

        control = self.control(call["id"])
        self.assertGreater(control["delay_min"], 0.5)


class TestUnassigned(DeadlineCase):
    """Время идёт, а исполнителя всё нет."""

    def test_только_что_поступивший_без_тревоги(self):
        call = self.register()
        control = self.control(call["id"])
        self.assertEqual(control["level"], "pending")
        self.assertFalse(control["alert"])

    def test_несколько_минут_без_исполнителя_тревога(self):
        call = self.register()
        self.age_call(call["id"], 4)

        control = self.control(call["id"])
        self.assertEqual(control["level"], "unassigned", control["reason"])
        self.assertTrue(control["alert"])
        self.assertIn("без исполнителя", control["reason"])

    def test_срок_истёк_без_исполнителя(self):
        call = self.register()
        self.age_call(call["id"], 17)
        self.assertEqual(self.control(call["id"])["level"], "overdue")


class TestSignal(DeadlineCase):
    """Можно ли верить координатам: GPS, связь, ручная точка."""

    def assigned_to_sokolov(self):
        call = self.register()
        best = self.assign_best(call["id"])
        self.assertEqual(best["full_name"], "Соколов А. В.")
        return call, best["employee_id"]

    def test_старая_gps_фиксация_даёт_тревогу(self):
        call, employee_id = self.assigned_to_sokolov()
        engineer = self.login("sokolov", "engineer")
        employees = self.client.get("/api/employees", headers=auth_header(self.dispatcher)).json()
        sokolov = next(item for item in employees if item["id"] == employee_id)

        # Телефон на связи, но фиксации уже две минуты: GPS потерян.
        response = self.send_location(
            employee_id, sokolov["lat"], sokolov["lon"], "gps", 120, token=engineer
        )
        self.assertEqual(response.status_code, 200, response.text)

        control = self.control(call["id"])
        self.assertEqual(control["level"], "no_signal", control["reason"])
        self.assertTrue(control["alert"])
        self.assertIn("GPS не определяет место", control["reason"])
        self.assertGreaterEqual(control["position_age_s"], 119)
        # Прогноз по последней точке всё равно показывается — как ориентир.
        self.assertIsNotNone(control["forecast_at"])

    def test_свежая_gps_фиксация_без_тревоги(self):
        call, employee_id = self.assigned_to_sokolov()
        engineer = self.login("sokolov", "engineer")
        self.send_location(employee_id, 55.97868, 37.39716, "gps", 3, token=engineer)
        self.assertEqual(self.control(call["id"])["level"], "ok")

    def test_телефон_не_на_связи_отличается_от_потери_gps(self):
        call, employee_id = self.assigned_to_sokolov()
        engineer = self.login("sokolov", "engineer")
        self.send_location(employee_id, 55.97868, 37.39716, "gps", 2, token=engineer)
        self.edit_employee(
            employee_id,
            seen_at=utc_now() - timedelta(minutes=3),
            location_at=utc_now() - timedelta(minutes=3),
        )

        control = self.control(call["id"])
        self.assertEqual(control["level"], "no_signal")
        self.assertIn("не на связи", control["reason"])

    def test_точка_имитации_не_устаревает(self):
        # Имитацию не обновляют намеренно — тревога «нет сигнала» была бы ложной.
        call, employee_id = self.assigned_to_sokolov()
        engineer = self.login("sokolov", "engineer")
        self.send_location(employee_id, 55.97868, 37.39716, "simulation", token=engineer)
        self.edit_employee(employee_id, location_at=utc_now() - timedelta(minutes=10))

        self.assertNotEqual(self.control(call["id"])["level"], "no_signal")

    def test_неизвестный_источник_отклоняется(self):
        employee_id = self.employee_id("Соколов А. В.")
        response = self.send_location(employee_id, 55.97, 37.39, source="bluetooth")
        self.assertEqual(response.status_code, 422, response.text)

    def test_координаты_отдаются_со_свежестью(self):
        employee_id = self.employee_id("Соколов А. В.")
        self.send_location(employee_id, 55.97, 37.39, "manual")
        employees = self.client.get("/api/employees", headers=auth_header(self.dispatcher)).json()
        sokolov = next(item for item in employees if item["id"] == employee_id)
        self.assertEqual(sokolov["location_source"], "manual")
        self.assertIsNotNone(sokolov["location_at"])
        self.assertIsNotNone(sokolov["seen_at"])


class TestPickupAndArrival(DeadlineCase):
    """Машина парка и подтверждение прибытия."""

    def test_дошёл_до_машины_значит_сел_в_неё(self):
        # Морозов без машины, в 326 м от него свободная ТМ-04: план — дойти
        # до неё и доехать. Назначает начальник смены (система предлагает
        # Соколова), поэтому с причиной.
        call = self.register()
        morozov = self.employee_id("Морозов Д. С.")
        assigned = self.assign(call["id"], morozov, "проверка посадки в машину", self.supervisor)
        self.assertEqual(assigned["vehicle_call_sign"], "ТМ-04")

        vehicles = self.client.get("/api/vehicles", headers=auth_header(self.dispatcher)).json()
        tm04 = next(item for item in vehicles if item["call_sign"] == "ТМ-04")
        self.assertEqual(tm04["status"], "reserved")
        walking_forecast = self.control(call["id"])["margin_min"]

        # Дошёл до машины — с этого момента она при нём.
        self.send_location(morozov, tm04["lat"], tm04["lon"])
        vehicles = self.client.get("/api/vehicles", headers=auth_header(self.dispatcher)).json()
        self.assertEqual(
            next(item for item in vehicles if item["call_sign"] == "ТМ-04")["status"], "in_use"
        )
        # Пеший участок пройден — запас вырос, а не «вернулся к машине».
        self.assertGreater(self.control(call["id"])["margin_min"], walking_forecast)

    def test_далеко_от_машины_она_ещё_ждёт(self):
        call = self.register()
        morozov = self.employee_id("Морозов Д. С.")
        self.assign(call["id"], morozov, "проверка посадки в машину", self.supervisor)
        self.send_location(morozov, 55.9790, 37.3975)

        vehicles = self.client.get("/api/vehicles", headers=auth_header(self.dispatcher)).json()
        tm04 = next(item for item in vehicles if item["call_sign"] == "ТМ-04")
        self.assertEqual(tm04["status"], "reserved")

    def test_прибытие_в_срок_фиксируется(self):
        call = self.register()
        self.assign_best(call["id"])
        engineer = self.login("sokolov", "engineer")
        self.client.post("/api/me/status", json={"action": "accepted"}, headers=auth_header(engineer))
        self.client.post("/api/me/status", json={"action": "arrived"}, headers=auth_header(engineer))

        control = self.control(call["id"])
        self.assertEqual(control["level"], "arrived")
        self.assertFalse(control["alert"])
        self.assertIn("в срок", control["reason"])

    def test_прибытие_с_опозданием_фиксируется(self):
        call = self.register()
        self.assign_best(call["id"])
        self.age_call(call["id"], 20)
        engineer = self.login("sokolov", "engineer")
        self.client.post("/api/me/status", json={"action": "arrived"}, headers=auth_header(engineer))

        control = self.control(call["id"])
        self.assertEqual(control["level"], "arrived")
        self.assertIn("опозданием", control["reason"])

    def test_инженер_видит_контроль_в_своём_вызове(self):
        call = self.register()
        self.assign_best(call["id"])
        self.age_call(call["id"], 9)

        engineer = self.login("sokolov", "engineer")
        current = self.client.get("/api/me/current-call", headers=auth_header(engineer)).json()
        self.assertEqual(current["call"]["control"]["level"], "late")


class TestQueued(DeadlineCase):
    """Вызов в очереди к занятому: прогноз — по расчёту цепочки."""

    def test_очередь_за_долгими_работами_срывает_срок(self):
        # Гусев занят на VP-BKB: прибыл, работы займут до получаса. Вызов
        # в очередь к нему — сознательное решение начальника смены, но срок
        # при этом будет сорван, и тревога должна это показывать.
        call = self.register()
        gusev = self.employee_id("Гусев Р. О.")
        queued = self.assign(call["id"], gusev, "ждём именно его, у него инструмент", self.supervisor)
        self.assertEqual(queued["status"], "queued")

        control = self.control(call["id"])
        self.assertEqual(control["level"], "late", control["reason"])
        self.assertTrue(control["alert"])
        self.assertIn("В очереди", control["reason"])
        self.assertEqual(control["forecast_at"], queued["eta_at"])


class TestWording(DeadlineCase):
    """Цифры в тексте причины и в отметке на вызове совпадают."""

    def test_остаток_в_тексте_и_в_поле_одинаковый(self):
        # 10 мин 59 с до срока: поле округляется до 11,0, и текст обязан
        # сказать «11», иначе в одной плашке будет «ост. 11» и «до срока 10».
        now = utc_now()
        left = minutes_until(now + timedelta(minutes=10, seconds=59), now)
        self.assertEqual(left, 11.0)
        self.assertEqual(minutes_text(left), f"{math.floor(left)} мин")

    def test_опоздание_округляется_вверх(self):
        # 12 секунд опоздания — «1 мин», а не «0 мин» и не «меньше минуты».
        self.assertEqual(overrun_text(0.2), "1 мин")
        self.assertEqual(overrun_text(3.01), "4 мин")
