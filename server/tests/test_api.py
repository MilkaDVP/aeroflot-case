"""
Тесты REST API.

Проверяется контракт из §8 и разграничение доступа: контекст аэропорта,
роли, право переопределения решения системы. Каждый тест работает с
отдельной временной базой, наполненной тем же seed.py, что и боевой стенд, —
то есть проверяется ровно та конфигурация, которую увидит жюри.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

# Переменные окружения выставляются до импорта модулей сервера: адрес базы
# читается при импорте database.py и позже уже не меняется.
_TEMP_DIR = tempfile.mkdtemp(prefix="aeroflot-test-")
_DB_PATH = Path(_TEMP_DIR) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"

from fastapi.testclient import TestClient  # noqa: E402

from constants import AIRPORTS_DIR  # noqa: E402
from main import app  # noqa: E402

HAS_GRAPH = (AIRPORTS_DIR / "UUEE.json").exists()


def auth_header(token):
    """Заголовок авторизации с токеном сессии."""
    return {"Authorization": f"Bearer {token}"}


@unittest.skipUnless(HAS_GRAPH, "нет data/airports/UUEE.json — выполните выгрузку")
class ApiTestCase(unittest.TestCase):
    """Общая подготовка: поднятый клиент и вход нужными ролями."""

    @classmethod
    def reset_database(cls):
        """Пересоздаёт базу и наполняет её тем же seed.py, что и стенд."""
        from database import Base, engine
        from seed import seed_if_empty

        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        seed_if_empty(verbose=False)

    @classmethod
    def setUpClass(cls):
        # Каждый класс начинает с чистой базы. Тесты меняют статусы
        # сотрудников и создают вызовы, и без сброса порядок выполнения
        # начал бы влиять на результат.
        cls.reset_database()

        # Контекстный менеджер запускает lifespan: создание таблиц,
        # наполнение тестовыми данными и загрузку графов.
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def login(self, login, password, shift="day", airport="UUEE"):
        """Вход и выбор контекста. Возвращает токен."""
        response = self.client.post(
            "/api/auth/login", json={"login": login, "password": password}
        )
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["token"]

        self.client.post(
            "/api/session/airport",
            json={"airport_icao": airport, "shift": shift},
            headers=auth_header(token),
        )
        return token

    def board_id(self, token, board_number):
        """Идентификатор борта по бортовому номеру."""
        response = self.client.get("/api/aircraft", headers=auth_header(token))
        for item in response.json():
            if item["board_number"] == board_number:
                return item["id"]
        self.fail(f"борт {board_number} не найден в тестовых данных")


class TestAuth(ApiTestCase):
    """Вход, контекст сессии и разграничение доступа."""

    def test_вход_с_верными_данными(self):
        response = self.client.post(
            "/api/auth/login", json={"login": "dispatcher", "password": "dispatcher"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["role"], "dispatcher")
        self.assertTrue(body["token"])
        # У диспетчера два аэропорта, поэтому сервер не выбирает за него:
        # шаг выбора аэропорта из §5 обязателен.
        self.assertIsNone(body["airport_icao"])
        self.assertEqual(len(body["airports"]), 2)

    def test_единственный_аэропорт_выбирается_сам(self):
        # У начальника смены аэропорт один — шаг выбора пропускается.
        response = self.client.post(
            "/api/auth/login", json={"login": "supervisor", "password": "supervisor"}
        )
        body = response.json()
        self.assertEqual(body["airport_icao"], "UUEE")
        self.assertEqual(len(body["airports"]), 1)

    def test_неверный_пароль_отклоняется(self):
        response = self.client.post(
            "/api/auth/login", json={"login": "dispatcher", "password": "нет"}
        )
        self.assertEqual(response.status_code, 401)

    def test_несуществующий_логин_отвечает_так_же(self):
        # Ответ не должен позволять определить, существует ли учётная запись.
        missing = self.client.post(
            "/api/auth/login", json={"login": "нет-такого", "password": "нет"}
        )
        wrong = self.client.post(
            "/api/auth/login", json={"login": "dispatcher", "password": "нет"}
        )
        self.assertEqual(missing.status_code, wrong.status_code)
        self.assertEqual(missing.json()["detail"], wrong.json()["detail"])

    def test_без_токена_доступа_нет(self):
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_поддельный_токен_не_проходит(self):
        # Токен только из ASCII: заголовки HTTP не переносят кириллицу.
        response = self.client.get("/api/auth/me", headers=auth_header("forged-token"))
        self.assertEqual(response.status_code, 401)

    def test_выход_закрывает_сессию(self):
        token = self.login("dispatcher", "dispatcher")
        self.client.post("/api/auth/logout", headers=auth_header(token))
        # Токен после выхода недействителен — сессия удалена из базы.
        response = self.client.get("/api/auth/me", headers=auth_header(token))
        self.assertEqual(response.status_code, 401)

    def test_чужой_аэропорт_выбрать_нельзя(self):
        # Начальник смены привязан только к Шереметьево: Домодедово
        # для него чужой аэропорт, даже если знать его код.
        token = self.login("supervisor", "supervisor")
        response = self.client.post(
            "/api/session/airport",
            json={"airport_icao": "UUDD", "shift": "day"},
            headers=auth_header(token),
        )
        self.assertEqual(response.status_code, 403)

    def test_смена_проверяется(self):
        token = self.login("dispatcher", "dispatcher")
        response = self.client.post(
            "/api/session/airport",
            json={"airport_icao": "UUEE", "shift": "вечерняя"},
            headers=auth_header(token),
        )
        self.assertEqual(response.status_code, 422)


class TestEmployees(ApiTestCase):
    """Выборка сотрудников по аэропорту и смене."""

    def test_список_ограничен_сменой_из_сессии(self):
        token = self.login("dispatcher", "dispatcher", shift="day")
        employees = self.client.get("/api/employees", headers=auth_header(token)).json()

        self.assertTrue(employees)
        self.assertTrue(all(item["shift"] == "day" for item in employees))
        # Ночная смена в дневном контексте не видна.
        names = [item["full_name"] for item in employees]
        self.assertNotIn("Тарасов Е. И.", names)

    def test_состав_другой_смены_запрашивается_явно(self):
        token = self.login("dispatcher", "dispatcher", shift="day")
        night = self.client.get(
            "/api/employees?shift=night", headers=auth_header(token)
        ).json()
        self.assertTrue(all(item["shift"] == "night" for item in night))

    def test_создание_сотрудника_только_администратору(self):
        token = self.login("dispatcher", "dispatcher")
        response = self.client.post(
            "/api/employees",
            json={"full_name": "Тест", "airport_icao": "UUEE", "shift": "day"},
            headers=auth_header(token),
        )
        self.assertEqual(response.status_code, 403)


class TestCallFlow(ApiTestCase):
    """Полный рабочий цикл: вызов → подбор → назначение → закрытие."""

    def test_подбор_отсеивает_ближайших_без_допуска(self):
        token = self.login("dispatcher", "dispatcher")
        aircraft_id = self.board_id(token, "VP-BZQ")  # A320 на стоянке 31

        call = self.client.post(
            "/api/calls",
            json={"aircraft_id": aircraft_id, "defect_code": "hydraulic_leak"},
            headers=auth_header(token),
        ).json()
        self.assertEqual(call["required_mark"], "B1.1")

        result = self.client.post(
            f"/api/calls/{call['id']}/suggest", headers=auth_header(token)
        ).json()

        self.assertIsNotNone(result["best"])
        self.assertTrue(result["within_regulation"])
        # Расчёт укладывается в норматив задания с большим запасом.
        self.assertLess(result["elapsed_ms"], 10_000)

        # Волков стоит вплотную к борту, но его допуск — только на B777.
        rejected = {item["full_name"]: item["reason"] for item in result["rejected"]}
        self.assertIn("Волков И. Н.", rejected)
        self.assertIn("A320", rejected["Волков И. Н."])

        # У Орлова отметка на A320 есть, но она истекла.
        self.assertIn("истекла", rejected["Орлов С. М."])

        # Занятый Гусев с допуском попал в отдельный список.
        busy_names = [item["full_name"] for item in result["busy"]]
        self.assertIn("Гусев Р. О.", busy_names)

    def test_нет_допуска_ни_у_кого(self):
        # A330 нет в допусках ни одного сотрудника дневной смены.
        token = self.login("dispatcher", "dispatcher")
        aircraft_id = self.board_id(token, "VQ-BQX")

        call = self.client.post(
            "/api/calls",
            json={"aircraft_id": aircraft_id, "defect_code": "engine_start_fault"},
            headers=auth_header(token),
        ).json()
        result = self.client.post(
            f"/api/calls/{call['id']}/suggest", headers=auth_header(token)
        ).json()

        self.assertIsNone(result["best"])
        self.assertEqual(result["candidates"], [])
        self.assertIn("A330", result["message"])

    def test_назначение_и_закрытие_вызова(self):
        token = self.login("dispatcher", "dispatcher")
        aircraft_id = self.board_id(token, "VP-BES")  # A321 на стоянке 33

        call = self.client.post(
            "/api/calls",
            json={"aircraft_id": aircraft_id, "defect_code": "hydraulic_leak"},
            headers=auth_header(token),
        ).json()
        result = self.client.post(
            f"/api/calls/{call['id']}/suggest", headers=auth_header(token)
        ).json()
        best_id = result["best"]["employee_id"]

        assigned = self.client.post(
            f"/api/calls/{call['id']}/assign",
            json={"employee_id": best_id},
            headers=auth_header(token),
        )
        self.assertEqual(assigned.status_code, 200, assigned.text)
        body = assigned.json()
        self.assertEqual(body["assigned_employee_id"], best_id)
        self.assertEqual(body["status"], "assigned")
        self.assertIsNotNone(body["eta_minutes"])
        self.assertTrue(body["route_node_ids"])

        # Назначенный сотрудник занят и выбыл из числа свободных.
        employees = self.client.get("/api/employees", headers=auth_header(token)).json()
        assignee = next(item for item in employees if item["id"] == best_id)
        self.assertEqual(assignee["status"], "assigned")
        self.assertIsNotNone(assignee["busy_until"])

        # Закрытие возвращает сотрудника в строй.
        closed = self.client.patch(
            f"/api/calls/{call['id']}/status",
            json={"status": "closed"},
            headers=auth_header(token),
        ).json()
        self.assertEqual(closed["status"], "closed")

        employees = self.client.get("/api/employees", headers=auth_header(token)).json()
        assignee = next(item for item in employees if item["id"] == best_id)
        self.assertEqual(assignee["status"], "free")
        self.assertIsNone(assignee["busy_until"])

    def test_неизвестный_код_дефекта_отклоняется(self):
        token = self.login("dispatcher", "dispatcher")
        aircraft_id = self.board_id(token, "VP-BZQ")
        response = self.client.post(
            "/api/calls",
            json={"aircraft_id": aircraft_id, "defect_code": "поломалось"},
            headers=auth_header(token),
        )
        self.assertEqual(response.status_code, 422)


class TestOverride(ApiTestCase):
    """Переопределение решения системы."""

    def setUp(self):
        # Каждый тест этого класса назначает кого-то и тем самым выводит
        # его из числа свободных. Без сброса до последнего теста доживает
        # один кандидат, и переопределять становится не на кого.
        self.reset_database()

    def create_call_and_suggest(self, token, board_number="VP-BZQ"):
        """
        Создаёт вызов и получает предложение. Возвращает вызов и результат.

        Борт выбран так, чтобы кандидатов было заведомо больше одного:
        на A320 допуск B1.1 есть и у Соколова, и у Морозова. Иначе
        переопределять было бы не на кого.
        """
        aircraft_id = self.board_id(token, board_number)
        call = self.client.post(
            "/api/calls",
            json={"aircraft_id": aircraft_id, "defect_code": "hydraulic_leak"},
            headers=auth_header(token),
        ).json()
        result = self.client.post(
            f"/api/calls/{call['id']}/suggest", headers=auth_header(token)
        ).json()
        return call, result

    def test_переопределение_без_причины_отклоняется(self):
        token = self.login("supervisor", "supervisor")
        call, result = self.create_call_and_suggest(token)

        other = next(
            item
            for item in result["candidates"]
            if item["employee_id"] != result["best"]["employee_id"]
        )
        response = self.client.post(
            f"/api/calls/{call['id']}/assign",
            json={"employee_id": other["employee_id"]},
            headers=auth_header(token),
        )
        self.assertEqual(response.status_code, 422)

    def test_диспетчер_переопределять_не_вправе(self):
        token = self.login("dispatcher", "dispatcher")
        call, result = self.create_call_and_suggest(token)

        other = next(
            item
            for item in result["candidates"]
            if item["employee_id"] != result["best"]["employee_id"]
        )
        response = self.client.post(
            f"/api/calls/{call['id']}/assign",
            json={"employee_id": other["employee_id"], "override_reason": "так надо"},
            headers=auth_header(token),
        )
        self.assertEqual(response.status_code, 403)

    def test_начальник_смены_переопределяет_с_причиной(self):
        token = self.login("supervisor", "supervisor")
        call, result = self.create_call_and_suggest(token)

        other = next(
            item
            for item in result["candidates"]
            if item["employee_id"] != result["best"]["employee_id"]
        )
        response = self.client.post(
            f"/api/calls/{call['id']}/assign",
            json={
                "employee_id": other["employee_id"],
                "override_reason": "сотрудник уже находится у соседнего борта",
            },
            headers=auth_header(token),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        # Сохранены оба решения: предложение системы и выбор человека.
        self.assertEqual(body["assigned_employee_id"], other["employee_id"])
        self.assertNotEqual(body["suggested_employee_id"], other["employee_id"])
        self.assertIn("соседнего борта", body["override_reason"])


class TestEngineerPwa(ApiTestCase):
    """Ручки PWA инженера."""

    def test_инженер_видит_свой_вызов_с_маршрутом(self):
        dispatcher = self.login("dispatcher", "dispatcher")
        aircraft_id = self.board_id(dispatcher, "VP-BZQ")  # A320 на стоянке 31

        call = self.client.post(
            "/api/calls",
            json={"aircraft_id": aircraft_id, "defect_code": "hydraulic_leak"},
            headers=auth_header(dispatcher),
        ).json()
        result = self.client.post(
            f"/api/calls/{call['id']}/suggest", headers=auth_header(dispatcher)
        ).json()

        # Назначаем инженера, у которого есть учётная запись в PWA.
        candidate = next(
            item for item in result["candidates"] if item["full_name"] == "Соколов А. В."
        )
        self.client.post(
            f"/api/calls/{call['id']}/assign",
            json={
                "employee_id": candidate["employee_id"],
                "override_reason": "проверка сценария PWA",
            },
            headers=auth_header(self.login("supervisor", "supervisor")),
        )

        engineer = self.login("sokolov", "engineer")
        current = self.client.get(
            "/api/me/current-call", headers=auth_header(engineer)
        ).json()

        self.assertEqual(current["call"]["id"], call["id"])
        self.assertTrue(current["route_points"])
        self.assertIn("lat", current["route_points"][0])

        # Инженер принимает вызов — статусы вызова и сотрудника меняются вместе.
        accepted = self.client.post(
            "/api/me/status", json={"action": "accepted"}, headers=auth_header(engineer)
        ).json()
        self.assertEqual(accepted["status"], "accepted")

    def test_у_инженера_без_вызова_пустой_ответ(self):
        engineer = self.login("morozov", "engineer")
        response = self.client.get(
            "/api/me/current-call", headers=auth_header(engineer)
        ).json()
        self.assertIn("message", response)

    def test_диспетчер_не_имеет_карточки_сотрудника(self):
        token = self.login("dispatcher", "dispatcher")
        response = self.client.get("/api/me/current-call", headers=auth_header(token))
        self.assertEqual(response.status_code, 403)


class TestTimestamps(ApiTestCase):
    """
    Отметки времени отдаются с явным часовым поясом.

    Сервер в контейнере живёт по UTC, браузер диспетчера — по местному
    времени. Строка без пояса читается браузером как местное время,
    и вызов, поступивший минуту назад, показывается случившимся несколько
    часов назад. По времени поступления вызова считается соблюдение
    регламента, поэтому это искажение рабочих данных, а не косметика.
    """

    def test_время_вызова_содержит_часовой_пояс(self):
        token = self.login("dispatcher", "dispatcher")
        aircraft_id = self.board_id(token, "VP-BZQ")
        call = self.client.post(
            "/api/calls",
            json={"aircraft_id": aircraft_id, "defect_code": "hydraulic_leak"},
            headers=auth_header(token),
        ).json()

        moment = datetime.fromisoformat(call["created_at"])
        self.assertIsNotNone(moment.tzinfo, "created_at отдан без часового пояса")

    def test_время_вызова_соответствует_настоящему_моменту(self):
        token = self.login("dispatcher", "dispatcher")
        aircraft_id = self.board_id(token, "VP-BES")
        call = self.client.post(
            "/api/calls",
            json={"aircraft_id": aircraft_id, "defect_code": "hydraulic_leak"},
            headers=auth_header(token),
        ).json()

        # Расхождение больше минуты означает потерянный или неверный пояс.
        delta = abs(
            (datetime.now(timezone.utc) - datetime.fromisoformat(call["created_at"]))
            .total_seconds()
        )
        self.assertLess(delta, 60, f"время вызова разошлось на {delta:.0f} с")

    def test_время_освобождения_сотрудника_с_поясом(self):
        token = self.login("dispatcher", "dispatcher")
        employees = self.client.get("/api/employees", headers=auth_header(token)).json()
        busy = [item for item in employees if item["busy_until"]]

        self.assertTrue(busy, "в тестовых данных нет занятого сотрудника")
        for employee in busy:
            moment = datetime.fromisoformat(employee["busy_until"])
            self.assertIsNotNone(moment.tzinfo)

    def test_время_освобождения_в_подборе_с_поясом(self):
        # Отдельная проверка: в подбор время попадает другим путём,
        # через представление сотрудника для алгоритма, и однажды уже
        # разошлось с ответом /api/employees.
        token = self.login("dispatcher", "dispatcher")
        aircraft_id = self.board_id(token, "VP-BZQ")
        call = self.client.post(
            "/api/calls",
            json={"aircraft_id": aircraft_id, "defect_code": "hydraulic_leak"},
            headers=auth_header(token),
        ).json()
        result = self.client.post(
            f"/api/calls/{call['id']}/suggest", headers=auth_header(token)
        ).json()

        self.assertTrue(result["busy"], "в тестовых данных нет занятого с допуском")
        for item in result["busy"]:
            moment = datetime.fromisoformat(item["busy_until"])
            self.assertIsNotNone(moment.tzinfo, "busy_until в подборе без пояса")
            # Занятый освобождается в будущем, а не в прошлом.
            self.assertGreater(moment, datetime.now(timezone.utc))


class TestReference(ApiTestCase):
    """Справочники."""

    def test_справочник_дефектов_полный(self):
        token = self.login("dispatcher", "dispatcher")
        defects = self.client.get(
            "/api/defect-types", headers=auth_header(token)
        ).json()
        self.assertEqual(len(defects), 12)
        codes = {item["code"] for item in defects}
        self.assertIn("hydraulic_leak", codes)
        self.assertIn("autopilot_fault", codes)

    def test_граф_аэропорта_отдаётся_целиком(self):
        token = self.login("dispatcher", "dispatcher")
        graph = self.client.get("/api/airports/UUEE", headers=auth_header(token)).json()
        self.assertGreater(len(graph["nodes"]), 1000)
        self.assertGreater(len(graph["edges"]), 1000)


if __name__ == "__main__":
    unittest.main()
