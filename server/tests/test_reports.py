"""
Тесты цепочки заявки: экипаж → диспетчерская → вызов.

Проверяется приём сообщений по интеграционному ключу, разбор их
диспетчером и главное следствие: регламент отсчитывается от сообщения
экипажа, а не от момента, когда диспетчер до него дошёл.
"""

from datetime import datetime, timedelta, timezone

from tests.test_api import auth_header
from tests.test_mentor import MentorCase

CREW_KEY = {"X-API-Key": "demo-crew-key"}


class ReportCase(MentorCase):
    """Общая подготовка: отправка сообщения от экипажа."""

    def report(self, board="VP-BZQ", minutes_ago=0, headers=None, **extra):
        body = {
            "airport_icao": "UUEE",
            "board_number": board,
            "description": "Течь гидрожидкости у левой стойки",
            "reporter": "КВС Орлов",
        }
        if minutes_ago:
            moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
            body["reported_at"] = moment.isoformat()
        body.update(extra)
        return self.client.post(
            "/api/intake/reports", json=body, headers=CREW_KEY if headers is None else headers
        )

    def inbox(self, token=None):
        response = self.client.get("/api/reports", headers=auth_header(token or self.dispatcher))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def accept(self, report_id, defect="hydraulic_leak", aircraft_id=None):
        return self.client.post(
            f"/api/reports/{report_id}/accept",
            json={"defect_code": defect, "aircraft_id": aircraft_id},
            headers=auth_header(self.dispatcher),
        )


class TestIntake(ReportCase):
    """Приём сообщений от экипажа и внешних систем."""

    def test_сообщение_попадает_в_ящик_а_не_в_вызовы(self):
        calls_before = len(self.client.get("/api/calls", headers=auth_header(self.dispatcher)).json())
        response = self.report()
        self.assertEqual(response.status_code, 201, response.text)

        body = response.json()
        self.assertEqual(body["source"], "crew-tablet")
        self.assertEqual(body["status"], "new")
        self.assertEqual(body["aircraft_type"], "A320")
        self.assertEqual([item["id"] for item in self.inbox()], [body["id"]])
        # Экипаж не создаёт вызов и никого не назначает.
        calls_after = len(self.client.get("/api/calls", headers=auth_header(self.dispatcher)).json())
        self.assertEqual(calls_after, calls_before)

    def test_без_ключа_и_с_чужим_ключом_отказ(self):
        self.assertEqual(self.report(headers={}).status_code, 401)
        self.assertEqual(self.report(headers={"X-API-Key": "wrong-key"}).status_code, 401)
        # Не-ASCII байты в ключе — тоже отказ, а не ошибка сервера.
        self.assertEqual(self.report(headers={"X-API-Key": b"\xd0\xba\xd0\xbb"}).status_code, 401)

    def test_источник_определяется_ключом(self):
        body = self.report(headers={"X-API-Key": "demo-airline-key"}).json()
        self.assertEqual(body["source"], "airline-system")

    def test_неизвестный_борт_принимается_без_привязки(self):
        # Потерять сообщение хуже, чем разобрать его вручную.
        body = self.report(board="vp-xxx").json()
        self.assertEqual(body["board_number"], "VP-XXX")
        self.assertIsNone(body["aircraft_id"])

    def test_номер_борта_нормализуется(self):
        body = self.report(board=" vp-bzq ").json()
        self.assertIsNotNone(body["aircraft_id"])

    def test_время_из_будущего_и_суточной_давности_отклоняется(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        self.assertEqual(self.report(reported_at=future).status_code, 422)
        self.assertEqual(self.report(reported_at=old).status_code, 422)

    def test_срок_в_ящике_идёт_от_сообщения(self):
        body = self.report(minutes_ago=5).json()
        reported = datetime.fromisoformat(body["reported_at"])
        deadline = datetime.fromisoformat(body["deadline_at"])
        self.assertEqual(deadline - reported, timedelta(minutes=15))
        self.assertAlmostEqual(
            (datetime.now(timezone.utc) - reported).total_seconds(), 300, delta=5
        )

    def test_список_бортов_для_формы_экипажа(self):
        response = self.client.get("/api/intake/aircraft?airport=UUEE", headers=CREW_KEY)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("VP-BZQ", [item["board_number"] for item in response.json()])
        self.assertEqual(self.client.get("/api/intake/aircraft?airport=UUEE").status_code, 401)

    def test_неизвестный_аэропорт(self):
        self.assertEqual(self.report(airport_icao="ZZZZ").status_code, 404)


class TestInbox(ReportCase):
    """Разбор сообщения диспетчером."""

    def test_принятое_сообщение_становится_вызовом(self):
        report = self.report().json()
        response = self.accept(report["id"])
        self.assertEqual(response.status_code, 200, response.text)

        call = response.json()
        self.assertEqual(call["board_number"], "VP-BZQ")
        self.assertEqual(call["required_mark"], "B1.1")
        self.assertEqual(call["defect_description"], "Течь гидрожидкости у левой стойки")
        self.assertEqual(call["source"], "crew-tablet")
        self.assertEqual(call["reported_at"], report["reported_at"])
        self.assertEqual(self.inbox(), [])

    def test_регламент_считается_от_сообщения_экипажа(self):
        # Экипаж сообщил 6 минут назад, диспетчер разобрал только сейчас:
        # до срока осталось 9 минут, а не 15.
        report = self.report(minutes_ago=6).json()
        call = self.accept(report["id"]).json()

        control = self.control(call["id"])
        self.assertAlmostEqual(control["minutes_left"], 9.0, delta=0.2)
        self.assertEqual(control["deadline_at"], report["deadline_at"])

    def test_наставник_видит_время_разбора(self):
        # Назначение мгновенное, но с сообщения прошло 6 минут — Наставник
        # считает от сообщения и отмечает задержку.
        report = self.report(minutes_ago=6).json()
        call = self.accept(report["id"]).json()
        self.assign_best(call["id"])

        event = self.last("assign", call["id"])
        self.assertEqual(event["verdict"], "fair", event["text"])
        self.assertIn("через 6 мин после поступления", event["text"])

    def test_борт_уточняется_при_разборе(self):
        report = self.report(board="VP-XXX").json()
        self.assertEqual(self.accept(report["id"]).status_code, 422)

        board = self.board_id(self.dispatcher, "VP-BZQ")
        response = self.accept(report["id"], aircraft_id=board)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["board_number"], "VP-BZQ")

    def test_отклонение_требует_причины_и_убирает_из_ящика(self):
        report = self.report().json()
        url = f"/api/reports/{report['id']}/reject"
        headers = auth_header(self.dispatcher)
        self.assertEqual(self.client.post(url, json={"reason": ""}, headers=headers).status_code, 422)

        response = self.client.post(url, json={"reason": "повтор заявки"}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "rejected")
        self.assertEqual(self.inbox(), [])

    def test_повторный_разбор_невозможен(self):
        report = self.report().json()
        self.accept(report["id"])
        self.assertEqual(self.accept(report["id"]).status_code, 409)

    def test_чужой_аэропорт_сообщение_не_видит(self):
        self.report()
        domodedovo = self.login("dispatcher_uudd", "dispatcher", airport="UUDD")
        self.assertEqual(self.inbox(domodedovo), [])

    def test_инженеру_ящик_недоступен(self):
        engineer = self.login("sokolov", "engineer")
        response = self.client.get("/api/reports", headers=auth_header(engineer))
        self.assertEqual(response.status_code, 403)
