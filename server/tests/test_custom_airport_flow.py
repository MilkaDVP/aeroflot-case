"""
Сквозной тест: аэропорт, собранный администратором, работает в диспетчеризации.

Ради этого конструктор и делался — «поставить точки и смотреть». Если
нарисованный аэропорт только сохраняется, но подбор на нём не идёт, толку
от него нет. Поэтому здесь весь путь целиком: разметить точки, завести
сотрудника с учётной записью и борт, зарегистрировать вызов, получить
подбор с маршрутом по нарисованным рулёжкам, назначить — и увидеть вызов
в приложении инженера.

Схема маленькая и понятная, чтобы ожидаемый ответ был очевиден без
расчётов: техцентр слева, перекрёсток в центре, две стоянки справа.
Единственный инженер с допуском стоит в техцентре и должен дойти до
стоянки 1 через перекрёсток.
"""

import graph_registry
from tests.test_api import ApiTestCase, auth_header

ICAO = "TSTF"

NODES = [
    {"id": "t1", "type": "tech_center", "ref": "ТЦ-1", "lat": 55.4000, "lon": 37.8950},
    {"id": "j1", "type": "junction", "ref": None, "lat": 55.4000, "lon": 37.9000},
    {"id": "s1", "type": "stand", "ref": "1", "lat": 55.4030, "lon": 37.9030},
    {"id": "s2", "type": "stand", "ref": "2", "lat": 55.3970, "lon": 37.9030},
]
EDGES = [
    {"from_id": "t1", "to_id": "j1", "vehicle_allowed": True},
    {"from_id": "j1", "to_id": "s1", "vehicle_allowed": True},
    {"from_id": "j1", "to_id": "s2", "vehicle_allowed": True},
]

# Отметка «на всякий случай» с действующим сроком и допуском на A320.
QUALIFICATIONS = [
    {"category": "B1.1", "aircraft_types": ["A320"], "valid_until": "2030-12-31"}
]


class TestCustomAirportFlow(ApiTestCase):
    """От пустой карты до вызова в телефоне инженера."""

    def setUp(self):
        self.reset_database()
        self.admin = self.login("admin", "admin")

    def tearDown(self):
        graph_registry.forget(ICAO)

    def build_airport(self):
        """Создаёт и размечает аэропорт, возвращает токен админа в нём."""
        response = self.client.post(
            "/api/admin/airports",
            json={"icao": ICAO, "name": "Полигон", "city": "", "lat": 55.4, "lon": 37.9},
            headers=auth_header(self.admin),
        )
        self.assertEqual(response.status_code, 201, response.text)

        response = self.client.put(
            f"/api/admin/airports/{ICAO}/graph",
            json={"nodes": NODES, "edges": EDGES},
            headers=auth_header(self.admin),
        )
        self.assertEqual(response.status_code, 200, response.text)

        # Администратор переключает сессию на новый аэропорт — ровно так же,
        # как диспетчер выбирает аэропорт перед сменой.
        return self.login("admin", "admin", airport=ICAO)

    def register_engineer(self, token):
        response = self.client.post(
            "/api/employees",
            json={
                "full_name": "Полигонов П. П.",
                "airport_icao": ICAO,
                "shift": "day",
                "qualifications": QUALIFICATIONS,
                "lat": 55.4000,
                "lon": 37.8950,
                "on_shift": True,
                "login": "poligonov",
                "password": "engineer",
            },
            headers=auth_header(token),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def put_aircraft(self, token, stand="s1"):
        response = self.client.post(
            "/api/aircraft",
            json={"board_number": "ra-00001", "aircraft_type": "A320", "stand_node_id": stand},
            headers=auth_header(token),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    # --- Весь путь ---

    def test_подбор_на_нарисованном_аэропорту(self):
        token = self.build_airport()
        engineer = self.register_engineer(token)
        board = self.put_aircraft(token)

        self.assertEqual(engineer["status"], "free")
        self.assertEqual(board["board_number"], "RA-00001")
        self.assertEqual(board["stand_ref"], "1")

        call = self.client.post(
            "/api/calls",
            json={"aircraft_id": board["id"], "defect_code": "hydraulic_leak"},
            headers=auth_header(token),
        ).json()

        result = self.client.post(
            f"/api/calls/{call['id']}/suggest", headers=auth_header(token)
        ).json()

        best = result["best"]
        self.assertIsNotNone(best, result["message"])
        self.assertEqual(best["full_name"], "Полигонов П. П.")
        # Маршрут идёт по нарисованным рулёжкам: техцентр → перекрёсток →
        # стоянка 1, а не напрямую по прямой.
        self.assertEqual(best["route"]["node_ids"], ["t1", "j1", "s1"])
        self.assertTrue(best["within_regulation"])

    def test_назначенный_вызов_приходит_в_приложение_инженера(self):
        token = self.build_airport()
        engineer = self.register_engineer(token)
        board = self.put_aircraft(token)

        call = self.client.post(
            "/api/calls",
            json={"aircraft_id": board["id"], "defect_code": "hydraulic_leak"},
            headers=auth_header(token),
        ).json()
        self.client.post(f"/api/calls/{call['id']}/suggest", headers=auth_header(token))
        response = self.client.post(
            f"/api/calls/{call['id']}/assign",
            json={"employee_id": engineer["id"]},
            headers=auth_header(token),
        )
        self.assertEqual(response.status_code, 200, response.text)

        # Инженер входит под выданными администратором логином и паролем.
        login = self.client.post(
            "/api/auth/login", json={"login": "poligonov", "password": "engineer"}
        )
        self.assertEqual(login.status_code, 200, login.text)
        engineer_token = login.json()["token"]

        current = self.client.get(
            "/api/me/current-call", headers=auth_header(engineer_token)
        ).json()
        self.assertIsNotNone(current["call"])
        self.assertEqual(current["call"]["board_number"], "RA-00001")

    def test_roles_and_vehicle_return(self):
        admin = self.build_airport()

        def post(token, path, **payload):
            r = self.client.post("/api/" + path, json=payload, headers=auth_header(token))
            self.assertIn(r.status_code, (200, 201), r.text)
            return r.json()

        def create_account(actor, role):
            post(actor, "admin/users", full_name=role, role=role, login="test-" + role,
                 password="demo", airport_icao=ICAO)
            return self.login("test-" + role, "demo", airport=ICAO)

        local = create_account(admin, "local_admin")
        supervisor = create_account(local, "shift_supervisor")
        dispatcher = create_account(local, "dispatcher")
        person = self.register_engineer(local)
        board = self.put_aircraft(local)
        def point(index):
            return {key: NODES[index][key] for key in ("lat", "lon")}
        old = post(local, "vehicles", call_sign="V1", **point(0))
        new = post(local, "vehicles", call_sign="V2", **point(3))
        for actor in (local, supervisor, dispatcher):
            for index in (1, 0):
                post(actor, f"employees/{person['id']}/location", **point(index))
                post(actor, f"vehicles/{old['id']}/location", **point(index))
                post(actor, f"aircraft/{board['id']}/location", free=True, **point(index))
        post(local, f"aircraft/{board['id']}/location", stand_node_id="s1", **point(2))
        job = post(dispatcher, "calls", aircraft_id=board["id"], defect_code="hydraulic_leak")
        path = f"calls/{job['id']}"
        post(dispatcher, path + "/assign", employee_id=person["id"], travel="walk")
        engineer = self.login("poligonov", "engineer", airport=ICAO)
        def location(index):
            post(engineer, f"employees/{person['id']}/location", source="gps", **point(index))
        def current():
            return self.client.get("/api/me/current-call", headers=auth_header(engineer)).json()
        post(engineer, "me/status", action="accepted")
        location(3)
        self.assertEqual(current()["route_points"][0]["id"], "s2")
        post(dispatcher, path + "/transport?vehicle_id=" + str(old["id"]))
        location(0)
        location(1)
        post(dispatcher, path + "/transport?vehicle_id=" + str(new["id"]))
        self.assertIn("Верните", current()["instruction"])
        # Повтор запроса после потери ответа не снимает резерв.
        post(dispatcher, path + "/transport?vehicle_id=" + str(new["id"]))
        denied = self.client.post("/api/me/status", json={"action": "arrived"}, headers=auth_header(engineer))
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(current()["call"]["status"], "accepted")
        location(0)
        self.assertTrue(current()["vehicle"]["pickup"])
        denied = self.client.post("/api/me/status", json={"action": "arrived"}, headers=auth_header(engineer))
        self.assertEqual(denied.status_code, 409)
        location(3)
        self.assertFalse(current()["vehicle"]["pickup"])
        location(2)
        post(supervisor, path + "/transport")
        self.assertIn("Верните", current()["instruction"])
        location(3)
        self.assertIsNone(current()["vehicle"])
        location(2)
        post(engineer, "me/status", action="arrived")
        post(engineer, "me/status", action="assessment", mel_category="A")
        post(engineer, "me/status", action="closed")
        self.assertNotIn("instruction", current())
        location(0)
        self.assertNotIn("instruction", current())

    def test_close_returns_car_and_finishes_navigation_at_parking(self):
        admin = self.build_airport()
        person = self.register_engineer(admin)
        aircraft = self.put_aircraft(admin)
        engineer = self.login("poligonov", "engineer", airport=ICAO)

        def post(actor, path, **data):
            r = self.client.post("/api/" + path, json=data, headers=auth_header(actor))
            self.assertIn(r.status_code, (200, 201), r.text)
            return r.json()

        def location(node):
            post(engineer, f"employees/{person['id']}/location", source="gps",
                 lat=node["lat"], lon=node["lon"])

        def leg():
            return self.client.get("/api/me/current-call", headers=auth_header(engineer)).json()

        parking = NODES[1]
        car = post(admin, "vehicles", call_sign="RETURN", lat=parking["lat"], lon=parking["lon"])
        job = post(admin, "calls", aircraft_id=aircraft["id"], defect_code="hydraulic_leak")
        path = f"calls/{job['id']}"
        post(admin, path + "/assign", employee_id=person["id"], travel="walk")
        post(admin, path + "/transport?vehicle_id=" + str(car["id"]))
        post(engineer, "me/status", action="accepted")
        location(parking)
        location(NODES[2])
        post(engineer, "me/status", action="arrived")
        post(engineer, "me/status", action="assessment", mel_category="A")
        post(engineer, "me/status", action="closed")
        self.assertIn("Верните спецтранспорт", leg()["instruction"])
        self.assertEqual(leg()["route_points"][-1]["lon"], parking["lon"])
        location(NODES[0])  # Приехать на место ожидания первым недостаточно.
        self.assertIn("Верните спецтранспорт", leg()["instruction"])
        location(parking)
        self.assertNotIn("instruction", leg())
        fleet = self.client.get("/api/vehicles", headers=auth_header(admin)).json()
        self.assertEqual(fleet[0]["status"], "free")
        self.assertEqual(fleet[0]["lon"], parking["lon"])
        location(NODES[0])
        self.assertNotIn("instruction", leg())

    # --- Проверки при заведении ---

    def test_борт_ставится_только_на_стоянку(self):
        token = self.build_airport()
        response = self.client.post(
            "/api/aircraft",
            json={"board_number": "RA-00002", "aircraft_type": "A320", "stand_node_id": "j1"},
            headers=auth_header(token),
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("не является стоянкой", response.json()["detail"])

    def test_неизвестный_тип_вс_отклоняется(self):
        token = self.build_airport()
        response = self.client.post(
            "/api/aircraft",
            json={"board_number": "RA-00003", "aircraft_type": "Кукурузник",
                  "stand_node_id": "s1"},
            headers=auth_header(token),
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("A320", response.json()["detail"])

    def test_логин_без_пароля_отклоняется(self):
        token = self.build_airport()
        response = self.client.post(
            "/api/employees",
            json={"full_name": "Без Пароля", "airport_icao": ICAO, "shift": "day",
                  "login": "nopass"},
            headers=auth_header(token),
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_занятый_логин_отклоняется(self):
        token = self.build_airport()
        response = self.client.post(
            "/api/employees",
            json={"full_name": "Двойник", "airport_icao": ICAO, "shift": "day",
                  "login": "sokolov", "password": "x"},
            headers=auth_header(token),
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_сотрудник_в_незаведённом_аэропорту(self):
        response = self.client.post(
            "/api/employees",
            json={"full_name": "Никто", "airport_icao": "NOPE", "shift": "day"},
            headers=auth_header(self.admin),
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_борт_с_открытым_вызовом_не_убирается(self):
        token = self.build_airport()
        board = self.put_aircraft(token)
        self.client.post(
            "/api/calls",
            json={"aircraft_id": board["id"], "defect_code": "hydraulic_leak"},
            headers=auth_header(token),
        )

        response = self.client.delete(
            f"/api/aircraft/{board['id']}", headers=auth_header(token)
        )
        self.assertEqual(response.status_code, 409, response.text)
