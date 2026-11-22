"""
Управление учётными записями и правка карточек по иерархии ролей.

Технический администратор заводит кого угодно где угодно; администратор
аэропорта — младших в своём аэропорту; начальник смены учётные записи
не заводит, но может завершить сеанс младшего и поменять статус и смену
сотрудника (не квалификацию); диспетчер ничем из этого не управляет.
"""

from tests.test_api import ApiTestCase, auth_header


class TestUsers(ApiTestCase):
    def setUp(self):
        self.reset_database()

    def call(self, method, token, path, body=None):
        return self.client.request(method, path, json=body, headers=auth_header(token))

    def users(self, token):
        response = self.call("GET", token, "/api/admin/users")
        self.assertEqual(response.status_code, 200, response.text)
        return {item["login"]: item for item in response.json()}

    def new_user(self, role, login="newbie", airport=None):
        body = {"login": login, "password": "secret", "full_name": "Новиков Н. Н.", "role": role}
        if airport:
            body["airport_icao"] = airport
        return body

    def employee_id(self, token, full_name):
        response = self.call("GET", token, "/api/employees")
        return next(item["id"] for item in response.json() if item["full_name"] == full_name)

    # --- Список ---

    def test_администратор_аэропорта_видит_только_свой_аэропорт(self):
        logins = self.users(self.login("local_uuee", "local_uuee"))
        self.assertIn("dispatcher", logins)
        self.assertIn("sokolov", logins)
        self.assertNotIn("dispatcher_uudd", logins)
        self.assertNotIn("admin", logins)

    def test_технический_администратор_видит_всех(self):
        logins = self.users(self.login("admin", "admin"))
        self.assertIn("dispatcher_uudd", logins)
        self.assertIn("local_uuee", logins)

    def test_диспетчер_списка_не_видит(self):
        response = self.call("GET", self.login("dispatcher", "dispatcher"), "/api/admin/users")
        self.assertEqual(response.status_code, 403, response.text)

    # --- Создание ---

    def test_администратор_аэропорта_заводит_диспетчера_у_себя(self):
        local = self.login("local_uuee", "local_uuee")
        created = self.call("POST", local, "/api/admin/users", self.new_user("dispatcher"))
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["airport_icao"], "UUEE")

        # Новый диспетчер входит и сразу попадает в свой аэропорт.
        login = self.client.post(
            "/api/auth/login", json={"login": "newbie", "password": "secret"}
        ).json()
        self.assertEqual(login["airport_icao"], "UUEE")

    def test_администратор_аэропорта_не_заводит_равных_и_старших(self):
        local = self.login("local_uuee", "local_uuee")
        for role in ("local_admin", "admin"):
            response = self.call("POST", local, "/api/admin/users", self.new_user(role))
            self.assertEqual(response.status_code, 403, f"{role}: {response.text}")

    def test_администратор_аэропорта_не_заводит_в_чужом(self):
        local = self.login("local_uuee", "local_uuee")
        response = self.call(
            "POST", local, "/api/admin/users", self.new_user("dispatcher", airport="UUDD")
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_технический_администратор_заводит_администратора_аэропорта(self):
        admin = self.login("admin", "admin")
        response = self.call(
            "POST", admin, "/api/admin/users", self.new_user("local_admin", airport="UUDD")
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["airport_icao"], "UUDD")

    def test_без_аэропорта_нельзя(self):
        admin = self.login("admin", "admin")
        response = self.call("POST", admin, "/api/admin/users", self.new_user("dispatcher"))
        self.assertEqual(response.status_code, 422, response.text)

    def test_инженер_заводится_через_карточку(self):
        admin = self.login("admin", "admin")
        response = self.call(
            "POST", admin, "/api/admin/users", self.new_user("engineer", airport="UUEE")
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_занятый_логин(self):
        local = self.login("local_uuee", "local_uuee")
        response = self.call(
            "POST", local, "/api/admin/users", self.new_user("dispatcher", login="dispatcher")
        )
        self.assertEqual(response.status_code, 409, response.text)

    # --- Правка и удаление ---

    def test_смена_пароля_закрывает_сеансы(self):
        dispatcher = self.login("dispatcher", "dispatcher")
        local = self.login("local_uuee", "local_uuee")
        target = self.users(local)["dispatcher"]

        response = self.call(
            "PATCH", local, f"/api/admin/users/{target['id']}", {"password": "new-pass"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.call("GET", dispatcher, "/api/auth/me").status_code, 401)

    def test_повысить_до_своей_роли_нельзя(self):
        local = self.login("local_uuee", "local_uuee")
        target = self.users(local)["dispatcher"]
        response = self.call(
            "PATCH", local, f"/api/admin/users/{target['id']}", {"role": "local_admin"}
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_начальник_смены_не_правит_учётные_записи(self):
        supervisor = self.login("supervisor", "supervisor")
        target = self.users(supervisor)["dispatcher"]
        self.assertFalse(target["can_edit"])
        response = self.call("DELETE", supervisor, f"/api/admin/users/{target['id']}")
        self.assertEqual(response.status_code, 403, response.text)

    def test_удаление_и_нельзя_удалить_себя(self):
        local = self.login("local_uuee", "local_uuee")
        logins = self.users(local)
        self.assertFalse(logins["local_uuee"]["can_edit"])

        response = self.call("DELETE", local, f"/api/admin/users/{logins['dispatcher2']['id']}")
        self.assertEqual(response.status_code, 204, response.text)
        self.assertNotIn("dispatcher2", self.users(local))

        response = self.call("DELETE", local, f"/api/admin/users/{logins['local_uuee']['id']}")
        self.assertEqual(response.status_code, 403, response.text)

    def test_чужой_аэропорт_не_найден(self):
        admin_logins = self.users(self.login("admin", "admin"))
        local = self.login("local_uuee", "local_uuee")
        foreign = admin_logins["dispatcher_uudd"]
        response = self.call("PATCH", local, f"/api/admin/users/{foreign['id']}", {"full_name": "X"})
        self.assertEqual(response.status_code, 404, response.text)

    # --- Сеансы ---

    def test_начальник_смены_завершает_сеанс_диспетчера(self):
        dispatcher = self.login("dispatcher", "dispatcher")
        supervisor = self.login("supervisor", "supervisor")
        target = self.users(supervisor)["dispatcher"]
        self.assertTrue(target["online"])
        self.assertTrue(target["can_logout"])

        response = self.call("POST", supervisor, f"/api/admin/users/{target['id']}/logout")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["online"])
        self.assertEqual(self.call("GET", dispatcher, "/api/auth/me").status_code, 401)

    def test_начальник_смены_не_выгоняет_старшего(self):
        self.login("local_uuee", "local_uuee")
        supervisor = self.login("supervisor", "supervisor")
        target = self.users(supervisor)["local_uuee"]
        response = self.call("POST", supervisor, f"/api/admin/users/{target['id']}/logout")
        self.assertEqual(response.status_code, 403, response.text)

    # --- Карточки сотрудников ---

    def test_начальник_смены_меняет_статус_но_не_квалификацию(self):
        supervisor = self.login("supervisor", "supervisor")
        sokolov = self.employee_id(supervisor, "Соколов А. В.")

        status = self.call(
            "PATCH", supervisor, f"/api/employees/{sokolov}", {"status": "offline", "shift": "night"}
        )
        self.assertEqual(status.status_code, 200, status.text)

        marks = self.call(
            "PATCH",
            supervisor,
            f"/api/employees/{sokolov}",
            {"qualifications": [{"category": "C", "aircraft_types": ["A320"], "valid_until": "2030-01-01"}]},
        )
        self.assertEqual(marks.status_code, 403, marks.text)

    def test_диспетчер_карточки_не_правит(self):
        dispatcher = self.login("dispatcher", "dispatcher")
        sokolov = self.employee_id(dispatcher, "Соколов А. В.")
        response = self.call("PATCH", dispatcher, f"/api/employees/{sokolov}", {"status": "offline"})
        self.assertEqual(response.status_code, 403, response.text)

    def test_администратор_аэропорта_меняет_квалификацию(self):
        local = self.login("local_uuee", "local_uuee")
        sokolov = self.employee_id(local, "Соколов А. В.")
        response = self.call(
            "PATCH",
            local,
            f"/api/employees/{sokolov}",
            {"qualifications": [{"category": "B2", "aircraft_types": ["A320"], "valid_until": "2030-01-01"}]},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_инженер_не_меняет_себе_квалификацию(self):
        engineer = self.login("sokolov", "engineer")
        me = self.call("GET", engineer, "/api/auth/me").json()
        response = self.call(
            "PATCH",
            engineer,
            f"/api/employees/{me['employee_id']}",
            {"qualifications": [{"category": "C", "aircraft_types": ["A320"], "valid_until": "2030-01-01"}]},
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_все_сотрудники_аэропорта_без_фильтра_смены(self):
        local = self.login("local_uuee", "local_uuee", shift="day")
        day = self.call("GET", local, "/api/employees").json()
        everyone = self.call("GET", local, "/api/employees?shift=all").json()
        self.assertTrue(all(item["shift"] == "day" for item in day))
        self.assertGreater(len(everyone), len(day))
        self.assertEqual({item["shift"] for item in everyone}, {"day", "night"})
