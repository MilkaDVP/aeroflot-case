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
