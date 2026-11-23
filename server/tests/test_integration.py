"""
Сквозные сценарии на стыке веток feature/lean-ui, codex/lean-ui-bugfixes
и victor.

Каждая ветка покрывала свою часть тестами; здесь проверяется, что части
работают вместе: учётные записи и роли (victor) с жизненным циклом смены
инженера (codex), учебные инженеры (codex) с переносом объектов на карте
(victor), перенос борта вне стоянки (victor) с назначением и контролем
срока (lean-ui), захват вызова (victor) с учениями (codex).
"""

from datetime import timedelta

from clock import utc_now
from database import SessionLocal
from graph_registry import get_graph
from models.employee import Employee, TrainingEngineer
from models.user import User
from services.shifts import expire_shifts
from tests.test_api import auth_header
from tests.test_drill import DrillCase


class TestRealEngineerLifecycle(DrillCase):
    """Сценарий 1. Реальный инженер: от учётной записи до конца смены."""

    def create_engineer(self, login="volnov"):
        local = self.login("local_uuee", "local_uuee")
        graph = get_graph("UUEE")
        base = graph.nodes_of_type("tech_center")[0]
        response = self.client.post(
            "/api/employees",
            json={
                "full_name": "Вольнов В. В.",
                "airport_icao": "UUEE",
                "shift": "day",
                "qualifications": [
                    {"category": "B1.1", "aircraft_types": ["A320"], "valid_until": "2030-12-31"}
                ],
                "lat": base["lat"],
                "lon": base["lon"],
                "on_shift": False,
                "login": login,
                "password": "secret",
            },
            headers=auth_header(local),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_полный_цикл(self):
        employee = self.create_engineer()

        # Вход: сессия не истекает (связь на перроне может пропадать),
        # активная сессия выводит инженера на смену.
        token = self.client.post(
            "/api/auth/login", json={"login": "volnov", "password": "secret"}
        ).json()["token"]
        headers = auth_header(token)
        me = self.client.get("/api/auth/me", headers=headers).json()
        self.assertEqual(me["employee_id"], employee["id"])
        self.assertEqual(me["role"], "engineer")
        self.assertTrue(me["on_shift"])
        self.assertFalse(employee["always_on_shift"])

        # Начало смены: срок окончания выставлен.
        shift = self.client.post(
            f"/api/employees/{employee['id']}/shift", json={"on_shift": True}, headers=headers
        )
        self.assertEqual(shift.status_code, 200, shift.text)
        me = self.client.get("/api/auth/me", headers=headers).json()
        self.assertTrue(me["on_shift"])
        self.assertIsNotNone(me["shift_ends_at"])

        # Диспетчер назначает вызов именно ему.
        call = self.register()
        self.assign(call["id"], employee["id"], reason="проверка", token=self.supervisor)
        current = self.client.get("/api/me/current-call", headers=headers).json()
        self.assertEqual(current["call"]["id"], call["id"])

        # Инженер принимает вызов.
        accepted = self.client.post(
            "/api/me/status", json={"action": "accepted", "call_id": call["id"]}, headers=headers
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["status"], "accepted")

        # Возвращаем вызов в подбор, затем назначаем того же сотрудника.
        leave = self.client.post(
            f"/api/employees/{employee['id']}/shift", json={"on_shift": False}, headers=headers
        )
        self.assertEqual(leave.status_code, 200, leave.text)
        self.client.post(f"/api/employees/{employee['id']}/shift", json={"on_shift": True}, headers=headers)
        self.assign(call["id"], employee["id"], reason="повторное назначение", token=self.supervisor)
        again = self.client.post("/api/me/status", json={"action": "accepted", "call_id": call["id"]}, headers=headers)
        self.assertEqual(again.status_code, 200, again.text)

        for action in ("arrived", "closed"):
            done = self.client.post(
                "/api/me/status", json={"action": action, "call_id": call["id"]}, headers=headers
            )
            self.assertEqual(done.status_code, 200, done.text)

        leave = self.client.post(
            f"/api/employees/{employee['id']}/shift", json={"on_shift": False}, headers=headers
        )
        self.assertEqual(leave.status_code, 200, leave.text)
        self.assertEqual(leave.json()["status"], "offline")
        # Конец смены — не выход из системы: вход сохраняется.
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)

    def test_учётная_запись_инженера_видна_в_учётных_записях(self):
        self.create_engineer(login="volnov2")
        local = self.login("local_uuee", "local_uuee")
        users = self.client.get("/api/admin/users", headers=auth_header(local)).json()
        account = next(item for item in users if item["login"] == "volnov2")
        self.assertEqual(account["role"], "engineer")
        self.assertIsNotNone(account["employee_id"])


class TestTrainingIsolation(DrillCase):
    """Сценарий 2. Учебный инженер не затрагивает реальные данные."""

    def snapshot_real(self):
        with SessionLocal() as db:
            return {
                person.id: (person.lat, person.lon, person.status, person.shift)
                for person in db.query(Employee).filter_by(airport_icao="UUEE")
            }

    def test_учения_не_трогают_штат(self):
        before = self.snapshot_real()
        self.assertEqual(self.enable().status_code, 200)

        call = self.register()
        bot = self.assign_best(call["id"])["employee_id"]
        with SessionLocal() as db:
            self.assertIsNotNone(db.get(TrainingEngineer, bot))
            self.assertIsNone(db.get(Employee, bot))

        # Перенос учебного инженера на карте (как перетаскивание мышью)
        # пересчитывает маршрут от нового места.
        graph = get_graph("UUEE")
        route = self.call(call["id"])["route_node_ids"]
        middle = graph.node(route[len(route) // 2])
        moved = self.send_location(bot, middle["lat"], middle["lon"])
        self.assertEqual(moved.status_code, 200, moved.text)
        after_move = self.call(call["id"])["route_node_ids"]
        self.assertLess(len(after_move), len(route))

        self.step(120)
        self.assertEqual(self.enable(False).status_code, 200)
        self.assertEqual(self.snapshot_real(), before)

    def test_учебный_инженер_не_получает_учётную_запись(self):
        self.enable()
        with SessionLocal() as db:
            bot = db.query(TrainingEngineer).filter_by(airport_icao="UUEE").first()
            self.assertIsNone(db.query(User).filter(User.employee_id == bot.id).first())
            bot_id = bot.id
        # В учётных записях и в составе аэропорта его нет.
        local = auth_header(self.login("local_uuee", "local_uuee"))
        roster = self.client.get("/api/employees?all_shifts=true", headers=local).json()
        self.assertNotIn(bot_id, [item["id"] for item in roster])
        # Карточку учебного инженера нельзя править как сотрудника.
        patch = self.client.patch(f"/api/employees/{bot_id}", json={"shift": "night"}, headers=local)
        self.assertEqual(patch.status_code, 404, patch.text)

    def test_в_учениях_реального_не_назначить(self):
        self.enable()
        call = self.register()
        real = self.employee_id("Соколов А. В.")
        response = self.client.post(
            f"/api/calls/{call['id']}/assign",
            json={"employee_id": real, "override_reason": "проверка"},
            headers=auth_header(self.supervisor),
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_учебный_вызов_и_захват(self):
        # Захват вызова работает и в учениях: второй диспетчер не назначит.
        self.enable()
        call = self.register()
        second = self.login("dispatcher2", "dispatcher")
        bot = self.suggest(call["id"])["best"]["employee_id"]
        response = self.client.post(
            f"/api/calls/{call['id']}/assign",
            json={"employee_id": bot},
            headers=auth_header(second),
        )
        self.assertEqual(response.status_code, 409, response.text)


class TestAircraftMoveWithAssignment(DrillCase):
    """Сценарий 4. Борт с назначенным инженером уходит со стоянки и возвращается."""

    def aircraft(self, board):
        items = self.client.get("/api/aircraft", headers=auth_header(self.dispatcher)).json()
        return next(item for item in items if item["board_number"] == board)

    def move(self, aircraft_id, body):
        return self.client.post(
            f"/api/aircraft/{aircraft_id}/location", json=body, headers=auth_header(self.dispatcher)
        )

    def test_маршрут_пересчитывается_и_стоянка_освобождается(self):
        call = self.register("VP-BZQ")
        self.assign_best(call["id"])
        before = self.call(call["id"])
        plane = self.aircraft("VP-BZQ")
        home = plane["stand_node_id"]

        # Свободная точка подальше от стоянок.
        graph = get_graph("UUEE")
        stands = graph.nodes_of_type("stand")
        far = max(
            (n for n in graph.nodes.values() if n["type"] != "stand"),
            key=lambda n: min(abs(n["lat"] - s["lat"]) + abs(n["lon"] - s["lon"]) for s in stands),
        )
        moved = self.move(plane["id"], {"lat": far["lat"], "lon": far["lon"], "free": True})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertFalse(moved.json()["on_stand"])

        after = self.call(call["id"])
        self.assertNotEqual(after["route_node_ids"], before["route_node_ids"])
        self.assertIsNone(after["stand_ref"])
        self.assertIsNotNone(after["control"])

        # Освободившуюся стоянку может занять другой борт...
        other = self.aircraft("VP-BES")
        taken = self.move(other["id"], {"lat": 0, "lon": 0, "stand_node_id": home})
        self.assertEqual(taken.status_code, 200, taken.text)
        # ...а вернуть первый на занятую уже нельзя.
        back = self.move(plane["id"], {"lat": 0, "lon": 0, "stand_node_id": home})
        self.assertEqual(back.status_code, 409, back.text)

    def test_инженер_не_двигает_борт(self):
        engineer = self.login("sokolov", "engineer")
        plane = self.aircraft("VP-BZQ")
        response = self.client.post(
            f"/api/aircraft/{plane['id']}/location",
            json={"lat": 55.97, "lon": 37.41, "free": True},
            headers=auth_header(engineer),
        )
        self.assertEqual(response.status_code, 403, response.text)


class TestShiftExpiryIndependence(DrillCase):
    """Сценарий 5. Конец смены инженера не зависит от входа и расписания аэропорта."""

    def test_истёкшая_смена_не_разлогинивает(self):
        token = self.login("sokolov", "engineer")
        headers = auth_header(token)
        me = self.client.get("/api/auth/me", headers=headers).json()
        self.assertTrue(me["on_shift"])
        with SessionLocal() as db:
            db.get(Employee, me["employee_id"]).shift_ends_at = utc_now() - timedelta(seconds=1)
            db.flush()
            expire_shifts(db)
            db.commit()
        me = self.client.get("/api/auth/me", headers=headers)
        self.assertEqual(me.status_code, 200)
        self.assertTrue(me.json()["on_shift"])
        self.assertIsNone(me.json()["shift_ends_at"])

    def test_расписание_аэропорта_не_снимает_инженера(self):
        # Смена аэропорта (shift_clock) лишь подсказывает смену при входе;
        # срок смены инженера ведёт services/shifts.
        supervisor = auth_header(self.supervisor)
        response = self.client.put(
            "/api/airports/UUEE/shift-schedule",
            json={"timezone": "Asia/Vladivostok", "day_start": "07:00", "day_end": "19:00"},
            headers=supervisor,
        )
        self.assertEqual(response.status_code, 200, response.text)
        token = self.login("sokolov", "engineer")
        me = self.client.get("/api/auth/me", headers=auth_header(token)).json()
        self.assertTrue(me["on_shift"])


class TestPermissionsAcrossFeatures(DrillCase):
    """Сценарий 7. Права на стыке функций."""

    def test_диспетчер_не_включает_учения(self):
        response = self.enable(token=self.dispatcher)
        self.assertEqual(response.status_code, 403, response.text)

    def test_администратор_аэропорта_включает_учения(self):
        local = self.login("local_uuee", "local_uuee")
        self.assertEqual(self.enable(token=local).status_code, 200)

    def test_реального_не_задержать_в_учениях(self):
        self.enable()
        real = self.employee_id("Соколов А. В.")
        self.assertEqual(self.hold(real).status_code, 409)

    def test_чужой_аэропорт_не_двигает_сотрудника(self):
        dme = self.login("dispatcher_uudd", "dispatcher", airport="UUDD")
        real = self.employee_id("Соколов А. В.")
        response = self.send_location(real, 55.97, 37.41, token=dme)
        self.assertEqual(response.status_code, 404, response.text)
