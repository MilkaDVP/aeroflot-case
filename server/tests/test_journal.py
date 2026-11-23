"""
Журнал событий (п. 3.2, критерий 22): полнота, точность времени,
выгрузка, неизменяемость и разграничение доступа.
"""

import csv
import io
import re
import zipfile
from datetime import timedelta

# Первым — тестовый модуль: он выставляет адрес временной базы до того,
# как database.py прочитает его. Иначе тест, запущенный отдельно,
# работал бы с рабочей базой стенда.
from tests.test_api import auth_header  # noqa: I001

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from clock import utc_now
from database import SessionLocal
from models.call import Call
from models.event_log import EventLog
from models.user import User
from services import journal
from tests.test_deadline import DeadlineCase

MS_STAMP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}$")


class JournalCase(DeadlineCase):
    def events(self, token=None, **filters):
        response = self.client.get(
            "/api/events", params={"limit": 500, **filters},
            headers=auth_header(token or self.supervisor),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"]

    def types_for(self, call_id, token=None):
        return [item["type"] for item in reversed(self.events(token, call_id=call_id))]

    def engineer_token(self, employee_id):
        with SessionLocal() as db:
            login = db.query(User).filter(User.employee_id == employee_id).one().login
        return self.login(login, "engineer")

    def act(self, token, action, call_id, **fields):
        response = self.client.post(
            "/api/me/status", json={"action": action, "call_id": call_id, **fields}, headers=auth_header(token)
        )
        self.assertEqual(response.status_code, 200, response.text)

    def full_run(self):
        """Заявка от фиксации до выполнения."""
        call = self.register("VP-BZQ")
        best = self.assign_best(call["id"])
        engineer = self.engineer_token(best["employee_id"])
        self.act(engineer, "accepted", call["id"])
        self.act(engineer, "arrived", call["id"])
        self.act(engineer, "assessment", call["id"], mel_category="B")
        self.act(engineer, "closed", call["id"])
        return call, best


class TestCompleteness(JournalCase):
    def test_прогон_заявки_целиком(self):
        call, best = self.full_run()
        types = self.types_for(call["id"])
        for expected in ("call_created", "candidates", "assigned", "status", "route_built",
                         "forecast", "arrival", "deadline_met"):
            self.assertIn(expected, types)
        self.assertLess(types.index("call_created"), types.index("candidates"))
        self.assertLess(types.index("candidates"), types.index("assigned"))
        self.assertLess(types.index("assigned"), types.index("arrival"))

        statuses = [(e["params"]["было"], e["params"]["стало"])
                    for e in reversed(self.events(call_id=call["id"], type="status"))]
        self.assertIn(("suggested", "assigned"), statuses)
        self.assertIn(("assigned", "accepted"), statuses)
        self.assertIn(("accepted", "arrived"), statuses)
        self.assertIn(("arrived", "closed"), statuses)

        assigned = self.events(call_id=call["id"], type="assigned")[0]
        self.assertEqual(assigned["employee_name"], best["full_name"])
        self.assertEqual(assigned["actor_name"], "Смирнова О. В.")
        self.assertEqual(assigned["board_number"], "VP-BZQ")

    def test_список_кандидатов_с_обоснованием_и_временем(self):
        call = self.register("VP-BZQ")
        result = self.suggest(call["id"])
        entry = self.events(call_id=call["id"], type="candidates")[0]
        self.assertIsNotNone(entry["duration_ms"])
        self.assertGreater(entry["duration_ms"], 0)
        params = entry["params"]
        self.assertEqual(params["обоснование"], result["message"])
        self.assertEqual(len(params["кандидаты"]), len(result["candidates"]))
        self.assertEqual(len(params["отклонены"]), len(result["rejected"]))
        self.assertTrue(all(r["причина"] for r in params["отклонены"]))

    def test_переназначение_и_снятие(self):
        call = self.register("VP-BZQ")
        best = self.assign_best(call["id"])
        response = self.client.post(
            f"/api/calls/{call['id']}/unassign", json={"reason": "сломалась машина"},
            headers=auth_header(self.supervisor),
        )
        self.assertEqual(response.status_code, 200, response.text)
        unassigned = self.events(call_id=call["id"], type="unassigned")[0]
        self.assertIn("сломалась машина", unassigned["text"])
        other = self.suggest(call["id"], self.supervisor)["best"]
        self.assertNotEqual(other["employee_id"], best["employee_id"])
        self.assign(call["id"], other["employee_id"], token=self.supervisor)
        reassigned = self.events(call_id=call["id"], type="reassigned")[0]
        self.assertEqual(reassigned["params"]["был"], best["full_name"])
        self.assertEqual(reassigned["employee_name"], other["full_name"])

    def test_риск_и_нарушение_норматива(self):
        call = self.register("VP-BZQ")
        self.age_call(call["id"], 14)
        with SessionLocal() as db:
            journal.watch(db)
            db.commit()
        self.assertTrue(self.events(call_id=call["id"], type="deadline_risk"))
        self.age_call(call["id"], 16)
        with SessionLocal() as db:
            journal.watch(db)
            journal.watch(db)  # повтор не дублирует запись
            db.commit()
        self.assertEqual(len(self.events(call_id=call["id"], type="deadline_violation")), 1)

    def test_опоздание_при_прибытии_фиксируется(self):
        call = self.register("VP-BZQ")
        best = self.assign_best(call["id"])
        self.age_call(call["id"], 20)
        engineer = self.engineer_token(best["employee_id"])
        self.act(engineer, "accepted", call["id"])
        self.act(engineer, "arrived", call["id"])
        violation = self.events(call_id=call["id"], type="deadline_violation")[0]
        self.assertGreater(violation["params"]["опоздание_мин"], 0)

    def test_ошибки_приёма_данных(self):
        bad = self.client.post("/api/intake/reports", json={"airport_icao": "UUEE"},
                               headers={"X-API-Key": "wrong"})
        self.assertGreaterEqual(bad.status_code, 400)
        response = self.client.post(
            "/api/employees/999999/location", json={"lat": "север", "lon": 37.4},
            headers=auth_header(self.dispatcher),
        )
        self.assertEqual(response.status_code, 422)
        errors = self.events(self.login("admin", "admin"), type="data_error")
        paths = [e["params"]["путь"] for e in errors]
        self.assertIn("/api/intake/reports", paths)
        self.assertIn("/api/employees/999999/location", paths)
        location = next(e for e in errors if e["params"]["путь"].endswith("/location"))
        self.assertIn("lat", location["params"]["ошибка"])

    def test_действия_пользователей_и_вход(self):
        self.client.post("/api/auth/login", json={"login": "dispatcher", "password": "нет"})
        admin = self.login("admin", "admin")
        types = {e["type"] for e in self.events(admin)}
        self.assertIn("login", types)
        self.assertIn("login_failed", types)
        self.assertIn("user_action", types)
        self.assertTrue(all("нет" not in str(e["params"]) for e in self.events(admin, type="login_failed")))


class TestTimeAndExport(JournalCase):
    def test_время_до_миллисекунды(self):
        call = self.register("VP-BZQ")
        entry = self.events(call_id=call["id"])[0]
        self.assertRegex(entry["ts"], r"\.\d{3}\+00:00$")
        self.assertRegex(entry["local_time"], MS_STAMP)

    def test_csv_хронологический_одна_строка_на_событие(self):
        call, _ = self.full_run()
        response = self.client.get(
            "/api/events/export", params={"format": "csv", "call_id": call["id"]},
            headers=auth_header(self.supervisor),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("attachment", response.headers["content-disposition"])
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
        header, body = rows[0], rows[1:]
        self.assertEqual(header[1], "Время (UTC)")
        self.assertEqual(len(body), len(self.events(call_id=call["id"])))
        stamps = [row[1] for row in body]
        self.assertTrue(all(MS_STAMP.match(s) for s in stamps))
        self.assertEqual(stamps, sorted(stamps))
        candidates = next(row for row in body if row[5] == "candidates")
        self.assertTrue(candidates[13])  # длительность, мс

    def test_excel(self):
        self.full_run()
        response = self.client.get(
            "/api/events/export", params={"format": "xlsx"}, headers=auth_header(self.supervisor)
        )
        self.assertEqual(response.status_code, 200)
        book = zipfile.ZipFile(io.BytesIO(response.content))
        sheet = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("Время (UTC)", sheet)
        self.assertIn("Формирование списка кандидатов", sheet)
        self.assertIn("yyyy-mm-dd hh:mm:ss.000", book.read("xl/styles.xml").decode("utf-8"))


class TestImmutability(JournalCase):
    def test_правка_и_удаление_запрещены_базой(self):
        self.register("VP-BZQ")
        with SessionLocal() as db:
            for statement in ("UPDATE event_log SET text = 'подмена'", "DELETE FROM event_log"):
                with self.assertRaises(DBAPIError):
                    db.execute(text(statement))
                db.rollback()

    def test_цепочка_хэшей(self):
        self.full_run()
        response = self.client.get("/api/events/integrity", headers=auth_header(self.supervisor))
        self.assertTrue(response.json()["ok"], response.text)
        self.assertGreater(response.json()["count"], 5)

        # Подмена в обход триггеров — проверка должна её найти.
        with SessionLocal() as db:
            target = db.query(EventLog).order_by(EventLog.id).offset(2).first().id
            db.execute(text("DROP TRIGGER event_log_no_update"))
            db.execute(text("UPDATE event_log SET text = 'подмена' WHERE id = :id"), {"id": target})
            db.commit()
        result = self.client.get("/api/events/integrity", headers=auth_header(self.supervisor)).json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["broken_id"], target)

    def test_в_api_нет_правки(self):
        entry = self.events()[0]
        for method in ("put", "patch", "delete"):
            response = getattr(self.client, method)(
                f"/api/events/{entry['id']}", headers=auth_header(self.login("admin", "admin"))
            )
            self.assertIn(response.status_code, (404, 405))


class TestAccess(JournalCase):
    def test_диспетчер_и_инженер_не_видят(self):
        self.assertEqual(self.client.get("/api/events", headers=auth_header(self.dispatcher)).status_code, 403)
        engineer = self.login("sokolov", "engineer")
        self.assertEqual(self.client.get("/api/events", headers=auth_header(engineer)).status_code, 403)

    def test_администратор_аэропорта_видит_только_свой(self):
        self.register("VP-BZQ")
        uudd = self.login("dispatcher_uudd", "dispatcher", airport="UUDD")
        aircraft = self.client.get("/api/aircraft", headers=auth_header(uudd)).json()[0]
        response = self.client.post(
            "/api/calls", json={"aircraft_id": aircraft["id"], "defect_code": "hydraulic_leak"},
            headers=auth_header(uudd),
        )
        self.assertEqual(response.status_code, 201, response.text)

        for login, own in (("local_uuee", "UUEE"), ("local_uudd", "UUDD")):
            token = self.login(login, login, airport=own)
            airports = {e["airport_icao"] for e in self.events(token) if e["actor_name"] != login}
            self.assertEqual(airports - {None}, {own}, login)
        supervisor = {e["airport_icao"] for e in self.events(self.supervisor)}
        self.assertNotIn("UUDD", supervisor)

        admin = self.login("admin", "admin")
        everything = {e["airport_icao"] for e in self.events(admin)}
        self.assertTrue({"UUEE", "UUDD"} <= everything)

    def test_выгрузка_тоже_ограничена(self):
        self.register("VP-BZQ")
        token = self.login("local_uudd", "local_uudd", airport="UUDD")
        response = self.client.get("/api/events/export", params={"format": "csv"},
                                   headers=auth_header(token))
        self.assertNotIn("VP-BZQ", response.content.decode("utf-8-sig"))


class TestTiming(JournalCase):
    def test_время_события_совпадает_с_фактом(self):
        before = utc_now()
        call = self.register("VP-BZQ")
        after = utc_now()
        with SessionLocal() as db:
            created = db.get(Call, call["id"]).created_at
            entry = db.query(EventLog).filter_by(call_id=call["id"], type="call_created").one()
        self.assertTrue(before - timedelta(seconds=1) <= entry.ts <= after + timedelta(seconds=1))
        self.assertLess(abs((entry.ts - created).total_seconds()), 1)
