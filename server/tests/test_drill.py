"""
Тесты режима учений.

Движение проверяется прямым вызовом шага с заданным временем: фоновые
такты в тестах выключены, поэтому результат не зависит от часов.
"""

from database import SessionLocal
from services import drill
from tests.test_api import auth_header
from tests.test_mentor import MentorCase


class DrillCase(MentorCase):
    """Общая подготовка: учения включены администратором."""

    def setUp(self):
        super().setUp()
        self.admin = self.login("admin", "admin")

    def enable(self, enabled=True, token=None):
        return self.client.post(
            "/api/drill", json={"enabled": enabled}, headers=auth_header(token or self.admin)
        )

    def step(self, seconds):
        db = SessionLocal()
        try:
            moved = drill.step(db, "UUEE", seconds)
            db.commit()
            return moved
        finally:
            db.close()

    def employee(self, employee_id):
        employees = self.client.get("/api/employees?include_training=true", headers=auth_header(self.dispatcher)).json()
        return next(item for item in employees if item["id"] == employee_id)

    def call(self, call_id):
        calls = self.client.get(
            "/api/calls?include_closed=true", headers=auth_header(self.dispatcher)
        ).json()
        return next(item for item in calls if item["id"] == call_id)

    def hold(self, employee_id, held=True):
        return self.client.post(
            f"/api/drill/hold/{employee_id}",
            json={"held": held},
            headers=auth_header(self.dispatcher),
        )


class TestSwitch(DrillCase):
    """Кто включает учения и что происходит вне их."""

    def test_учебная_команда_отдельна_от_штата(self):
        from models.employee import Employee, TrainingEngineer

        self.enable()
        with SessionLocal() as db:
            self.assertFalse(db.query(Employee).filter_by(is_training=True).first())
            self.assertEqual(db.query(TrainingEngineer).filter_by(airport_icao="UUEE").count(), 6)
        headers = auth_header(self.admin)
        roster = self.client.get("/api/employees?all_shifts=true", headers=headers).json()
        names = [person["full_name"] for person in roster]
        self.assertIn("Соколов А. В.", names)
        self.assertIn("Тарасов Е. И.", names)
        self.assertFalse(any(name.startswith("Учебный инженер") for name in names))
        people = self.client.get("/api/employees?include_training=true", headers=headers).json()
        self.assertTrue(any(person["full_name"].startswith("Учебный инженер") for person in people))
        self.assertTrue(any(person["full_name"] == "Соколов А. В." for person in people))

    def test_по_умолчанию_выключены(self):
        state = self.client.get("/api/drill", headers=auth_header(self.dispatcher)).json()
        self.assertFalse(state["enabled"])

    def test_диспетчер_включить_не_вправе(self):
        self.assertEqual(self.enable(token=self.dispatcher).status_code, 403)

    def test_администратор_включает(self):
        self.assertEqual(self.enable().status_code, 200)
        state = self.client.get("/api/drill", headers=auth_header(self.dispatcher)).json()
        self.assertTrue(state["enabled"])

    def test_учения_одного_аэропорта_не_трогают_другой(self):
        self.enable()
        db = SessionLocal()
        try:
            self.assertEqual(drill.enabled_airports(db), ["UUEE"])
        finally:
            db.close()

    def test_вне_учений_задержать_нельзя(self):
        response = self.hold(self.employee_id("Соколов А. В."))
        self.assertEqual(response.status_code, 409, response.text)


class TestMovement(DrillCase):
    """Движение по маршруту с расчётной скоростью."""

    def test_прогноз_сходится_с_расчётом_при_назначении(self):
        # Минуту ехал — прогноз прибытия не изменился: учебное движение
        # идёт с той же скоростью и по тем же длинам, что и расчёт.
        self.enable()
        call = self.register()
        best = self.assign_best(call["id"])
        start = self.employee(best["employee_id"])
        planned = self.call(call["id"])["eta_at"]

        self.step(60)

        moved = self.employee(best["employee_id"])
        self.assertNotEqual((start["lat"], start["lon"]), (moved["lat"], moved["lon"]))
        self.assertEqual(self.call(call["id"])["status"], "accepted")
        self.assertEqual(moved["status"], "en_route")
        # Точка осталась «ручной»: тревоги о потере GPS по ней быть не должно.
        self.assertEqual(moved["location_source"], "manual")
        self.assertAlmostEqual(self.control(call["id"])["delay_min"], -1.0, delta=0.25)
        self.assertEqual(self.call(call["id"])["eta_at"], planned)

    def test_прибытие_подтверждается_само_и_оценивается(self):
        self.enable()
        call = self.register()
        best = self.assign_best(call["id"])

        self.step(best["minutes"] * 60 + 30)

        self.assertEqual(self.call(call["id"])["status"], "arrived")
        self.assertEqual(self.employee(best["employee_id"])["status"], "busy")
        self.assertEqual(self.last("arrival", call["id"])["verdict"], "good")

    def test_пеший_забирает_машину_по_пути(self):
        # Учебный механик идёт до ТМ-04 пешком, дальше едет: 9,5 мин на весь путь.
        self.enable()
        call = self.register()
        mechanic = self.suggest(call["id"])["best"]["employee_id"]
        self.assign(call["id"], mechanic, "учения", self.supervisor)

        self.step(5 * 60)
        vehicles = self.client.get("/api/vehicles", headers=auth_header(self.dispatcher)).json()
        tm04 = next(item for item in vehicles if item["call_sign"] == "ТМ-04")
        self.assertEqual(tm04["status"], "in_use")
        self.assertEqual(self.call(call["id"])["status"], "accepted")

        self.step(5 * 60)
        self.assertEqual(self.call(call["id"])["status"], "arrived")

    def test_перенос_между_узлами_меняет_начало_пути(self):
        from algorithm.geo import haversine_m

        self.enable()
        call = self.register()
        bot = self.assign_best(call["id"])["employee_id"]
        self.step(60)
        lat, lon = 55.98095, 37.42906
        self.assertEqual(self.send_location(bot, lat, lon).status_code, 200)
        self.step(1)
        position = self.employee(bot)
        distance = haversine_m(lat, lon, position["lat"], position["lon"])
        self.assertGreater(distance, 0)
        self.assertLess(distance, 20)

    def test_перенос_на_последнее_ребро_ведёт_к_борту(self):
        from algorithm.geo import haversine_m
        from graph_registry import get_graph

        self.enable()
        call = self.register()
        bot = self.assign_best(call["id"])["employee_id"]
        route = self.call(call["id"])
        self.assertTrue(route["pickup_node_id"])
        graph = get_graph("UUEE")
        start, goal = [graph.node(n) for n in route["route_node_ids"][-2:]]
        lat, lon = [start[k] * 0.7 + goal[k] * 0.3 for k in ("lat", "lon")]
        self.assertEqual(self.send_location(bot, lat, lon).status_code, 200)
        updated = self.call(call["id"])
        self.assertIsNone(updated["pickup_node_id"])
        self.assertEqual(updated["route_node_ids"], [goal["id"]])
        self.step(1)
        pos = self.employee(bot)
        self.assertLess(haversine_m(pos["lat"], pos["lon"], goal["lat"], goal["lon"]),
                        haversine_m(lat, lon, goal["lat"], goal["lon"]))

    def test_без_учений_никто_не_движется(self):
        call = self.register()
        best = self.assign_best(call["id"])
        start = self.employee(best["employee_id"])

        db = SessionLocal()
        try:
            self.assertEqual(drill.enabled_airports(db), [])
        finally:
            db.close()
        self.assertEqual(
            (start["lat"], start["lon"]),
            (self.employee(best["employee_id"])["lat"], self.employee(best["employee_id"])["lon"]),
        )

    def test_сотрудника_с_телефоном_сервер_не_двигает(self):
        call = self.register()
        real = self.assign_best(call["id"])["employee_id"]
        engineer = self.login("sokolov", "engineer")
        self.enable()
        training = self.register()
        bot = self.assign_best(training["id"])["employee_id"]
        self.assertNotEqual(bot, real)
        self.assertEqual(self.hold(real).status_code, 409)
        for source in ("gps", "simulation", "manual"):
            response = self.send_location(real, 55.97868, 37.39716, source, token=engineer)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.step(10), 1)
        self.enable(False)
        self.assertAlmostEqual(self.employee(real)["lat"], 55.97868, places=5)
        self.assertEqual(self.call(call["id"])["status"], "assigned")


class TestHold(DrillCase):
    """Задержка исполнителя: учебная вводная «не успевает»."""

    def test_задержанный_стоит_и_срок_срывается(self):
        self.enable()
        call = self.register()
        best = self.assign_best(call["id"])
        self.step(60)
        self.assertEqual(self.hold(best["employee_id"]).status_code, 200)
        stopped = self.employee(best["employee_id"])
        self.assertTrue(stopped["held"])

        self.step(300)
        still = self.employee(best["employee_id"])
        self.assertEqual((stopped["lat"], stopped["lon"]), (still["lat"], still["lon"]))

        # Девять минут с вызова, а ехать ещё больше пяти: тревога и оценка.
        self.age_call(call["id"], 10)
        self.assertEqual(self.control(call["id"])["level"], "late")
        self.assertEqual(self.last("watch_late", call["id"])["verdict"], "bad")

    def test_отпущенный_едет_дальше(self):
        self.enable()
        call = self.register()
        best = self.assign_best(call["id"])
        self.hold(best["employee_id"])
        self.step(60)
        self.hold(best["employee_id"], held=False)
        before = self.employee(best["employee_id"])
        self.step(60)
        after = self.employee(best["employee_id"])
        self.assertNotEqual((before["lat"], before["lon"]), (after["lat"], after["lon"]))

    def test_выключение_учений_снимает_задержки(self):
        self.enable()
        call = self.register()
        best = self.assign_best(call["id"])
        self.hold(best["employee_id"])
        self.enable(False)
        self.enable()
        self.assertFalse(self.employee(best["employee_id"])["held"])
