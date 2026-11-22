"""
Иерархия ролей: технический администратор → администратор аэропорта →
начальник смены → диспетчер; инженер стоит особняком.

Старшая роль получает права всех младших, но администратор аэропорта
ограничен своим аэропортом, а инженер — своей карточкой и своим вызовом.
"""

import unittest

from auth.dependencies import RoleGuard
from models.user import (
    DISPATCH_ROLES,
    ROLE_ADMIN,
    ROLE_DISPATCHER,
    ROLE_ENGINEER,
    ROLE_LOCAL_ADMIN,
    ROLE_SHIFT_SUPERVISOR,
    role_at_least,
)
from tests.test_api import ApiTestCase, auth_header

# Точка внутри Шереметьево — для машин, которые заводят в тестах.
SVO_POINT = {"lat": 55.9726, "lon": 37.4146}


class TestRoleRanks(unittest.TestCase):
    """Правило наследования прав само по себе, без HTTP."""

    def test_старшая_роль_не_ниже_младшей(self):
        self.assertTrue(role_at_least(ROLE_ADMIN, ROLE_DISPATCHER))
        self.assertTrue(role_at_least(ROLE_LOCAL_ADMIN, ROLE_SHIFT_SUPERVISOR))
        self.assertTrue(role_at_least(ROLE_SHIFT_SUPERVISOR, ROLE_SHIFT_SUPERVISOR))
        self.assertFalse(role_at_least(ROLE_DISPATCHER, ROLE_SHIFT_SUPERVISOR))

    def test_инженер_не_наследует_и_не_наследуется(self):
        self.assertFalse(role_at_least(ROLE_ENGINEER, ROLE_DISPATCHER))
        guard = RoleGuard(DISPATCH_ROLES)
        self.assertFalse(guard.allows(ROLE_ENGINEER))
        self.assertTrue(RoleGuard([ROLE_ENGINEER]).allows(ROLE_ENGINEER))
        self.assertFalse(RoleGuard([ROLE_ENGINEER]).allows(ROLE_DISPATCHER))

    def test_допуск_роли_пропускает_всех_старших(self):
        guard = RoleGuard([ROLE_DISPATCHER])
        for role in (ROLE_DISPATCHER, ROLE_SHIFT_SUPERVISOR, ROLE_LOCAL_ADMIN, ROLE_ADMIN):
            self.assertTrue(guard.allows(role), role)

    def test_допуск_администратора_аэропорта_не_пропускает_младших(self):
        guard = RoleGuard([ROLE_LOCAL_ADMIN])
        self.assertTrue(guard.allows(ROLE_ADMIN))
        self.assertFalse(guard.allows(ROLE_SHIFT_SUPERVISOR))
        self.assertFalse(guard.allows(ROLE_DISPATCHER))


class TestRolesOverHttp(ApiTestCase):
    """Те же правила на настоящих ручках."""

    def setUp(self):
        self.reset_database()

    def post(self, token, path, body):
        return self.client.post(path, json=body, headers=auth_header(token))

    def vehicle(self, call_sign):
        return {"call_sign": call_sign, **SVO_POINT}

    def test_администратор_аэропорта_ставит_машину_диспетчер_нет(self):
        local = self.login("local_uuee", "local_uuee")
        dispatcher = self.login("dispatcher", "dispatcher")

        created = self.post(local, "/api/vehicles", self.vehicle("ТМ-77"))
        self.assertEqual(created.status_code, 201, created.text)

        refused = self.post(dispatcher, "/api/vehicles", self.vehicle("ТМ-78"))
        self.assertEqual(refused.status_code, 403, refused.text)

    def test_администратор_аэропорта_заводит_сотрудника_только_у_себя(self):
        local = self.login("local_uuee", "local_uuee")
        body = {"full_name": "Тестов Т. Т.", "shift": "day"}

        own = self.post(local, "/api/employees", {**body, "airport_icao": "UUEE"})
        self.assertEqual(own.status_code, 201, own.text)

        foreign = self.post(local, "/api/employees", {**body, "airport_icao": "UUDD"})
        self.assertEqual(foreign.status_code, 403, foreign.text)

    def test_технический_администратор_заводит_сотрудника_где_угодно(self):
        admin = self.login("admin", "admin")
        response = self.post(
            admin,
            "/api/employees",
            {"full_name": "Тестов Т. Т.", "shift": "day", "airport_icao": "UUDD"},
        )
        self.assertEqual(response.status_code, 201, response.text)

    def test_аэропорты_системы_только_у_технического_администратора(self):
        local = self.login("local_uuee", "local_uuee")
        response = self.client.get("/api/admin/airports", headers=auth_header(local))
        self.assertEqual(response.status_code, 403, response.text)

        admin = self.login("admin", "admin")
        response = self.client.get("/api/admin/airports", headers=auth_header(admin))
        self.assertEqual(response.status_code, 200, response.text)

    def test_старшие_роли_работают_на_экране_диспетчера(self):
        for login, password in (
            ("dispatcher", "dispatcher"),
            ("supervisor", "supervisor"),
            ("local_uuee", "local_uuee"),
        ):
            token = self.login(login, password)
            response = self.client.get("/api/shift/feed", headers=auth_header(token))
            self.assertEqual(response.status_code, 200, f"{login}: {response.text}")

    def test_инженер_не_видит_экран_диспетчера(self):
        token = self.login("sokolov", "engineer")
        response = self.client.get("/api/shift/feed", headers=auth_header(token))
        self.assertEqual(response.status_code, 403, response.text)


if __name__ == "__main__":
    unittest.main()
