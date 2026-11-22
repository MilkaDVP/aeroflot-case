"""Онлайн-синхронизация FR24: внешний API всегда подменяется, сеть не нужна."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from fr24sdk.exceptions import (
    ApiError,
    AuthenticationError,
    PaymentRequiredError,
    RateLimitError,
    TransportError,
)

from algorithm.graph import AirportGraph
from graph_registry import get_graph
from services.flightradar import (
    POSITIONS_LIMIT,
    FlightradarError,
    airport_bounds,
    fetch_flightradar_positions,
    normalize_aircraft_type,
)
from tests.test_api import ApiTestCase, auth_header


def position(stand, board="RA-TEST1", source_type="A20N", speed=0, **changes):
    """Минимальная live-позиция с полями, которые использует сервис."""
    values = {
        "fr24_id": "external-id-not-for-storage",
        "reg": board,
        "type": source_type,
        "gspeed": speed,
        "lat": stand["lat"],
        "lon": stand["lon"],
    }
    values.update(changes)
    return SimpleNamespace(**values)


class FlightradarServiceTests(unittest.TestCase):
    """Контракт SDK и преобразование ошибок без реального HTTP-запроса."""

    def setUp(self):
        self.graph = AirportGraph(
            {
                "icao": "TEST",
                "name": "Тест",
                "city": "",
                "ref_point": {"lat": 55.0, "lon": 38.0},
                "nodes": [
                    {
                        "id": "s1",
                        "type": "stand",
                        "ref": "1",
                        "lat": 55.0,
                        "lon": 38.0,
                    }
                ],
                "edges": [],
            }
        )

    def sdk_client(self, response=None, error=None):
        client = MagicMock()
        client.__enter__.return_value = client
        call = client.live.flight_positions.get_full
        call.return_value = SimpleNamespace(data=response or [])
        call.side_effect = error
        return client

    @staticmethod
    def api_error(error_class, status=500):
        request = httpx.Request("GET", "https://example.test/api")
        response = httpx.Response(status, request=request)
        if error_class is TransportError:
            return error_class("network", request=request)
        return error_class("external", request=request, response=response)

    def test_точные_модели_приводятся_к_семействам_справочника(self):
        expected = {
            "A20N": "A320",
            "A21N": "A321",
            "SU95": "SSJ-100",
            "B38M": "B737",
            "B77W": "B777",
            "B78X": "B787",
            "T214": "Ту-214",
        }
        for source, normalized in expected.items():
            with self.subTest(source=source):
                self.assertEqual(normalize_aircraft_type(source), normalized)
        self.assertEqual(normalize_aircraft_type("a320"), "A320")
        self.assertIsNone(normalize_aircraft_type("UNKNOWN"))
        self.assertIsNone(normalize_aircraft_type(None))

    def test_рамка_запроса_имеет_порядок_n_s_w_e(self):
        self.assertEqual(airport_bounds(self.graph), "55.005000,54.995000,37.995000,38.005000")

    def test_sdk_получает_ключ_рамку_и_лимит(self):
        client = self.sdk_client(response=[position(self.graph.node("s1"))])
        with patch.dict(os.environ, {"FR24_API_TOKEN": "server-secret"}, clear=False):
            with patch("services.flightradar.Client", return_value=client) as client_type:
                result = fetch_flightradar_positions(self.graph)

        self.assertEqual(len(result), 1)
        client_type.assert_called_once_with(api_token="server-secret", timeout=15.0)
        client.live.flight_positions.get_full.assert_called_once_with(
            bounds="55.005000,54.995000,37.995000,38.005000",
            limit=POSITIONS_LIMIT,
        )

    def test_ошибки_sdk_не_становятся_локальным_401(self):
        cases = [
            (AuthenticationError, 401, 502),
            (PaymentRequiredError, 402, 503),
            (RateLimitError, 429, 503),
            (TransportError, 500, 503),
            (ApiError, 500, 502),
        ]
        for error_class, external_status, expected_status in cases:
            with self.subTest(error=error_class.__name__):
                error = self.api_error(error_class, external_status)
                client = self.sdk_client(error=error)
                with patch.dict(os.environ, {"FR24_API_TOKEN": "secret"}, clear=False):
                    with patch("services.flightradar.Client", return_value=client):
                        with self.assertRaises(FlightradarError) as raised:
                            fetch_flightradar_positions(self.graph)
                self.assertEqual(raised.exception.http_status, expected_status)

    def test_без_ключа_sdk_не_вызывается(self):
        with patch.dict(os.environ, {"FR24_API_TOKEN": ""}, clear=False):
            with patch("services.flightradar.Client") as client_type:
                with self.assertRaises(FlightradarError) as raised:
                    fetch_flightradar_positions(self.graph)
        self.assertEqual(raised.exception.http_status, 503)
        client_type.assert_not_called()


class TestFlightradarApi(ApiTestCase):
    """REST-контракт и защита операционных данных проекта."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.graph = get_graph("UUEE")

    def setUp(self):
        self.reset_database()
        self.admin = self.login("admin", "admin")

    def aircraft(self):
        response = self.client.get("/api/aircraft", headers=auth_header(self.admin))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def free_stand(self):
        occupied = {item["stand_node_id"] for item in self.aircraft()}
        return next(
            node for node in self.graph.nodes_of_type("stand") if node["id"] not in occupied
        )

    def sync(self, positions, token=None):
        with patch("api.flightradar.fetch_flightradar_positions", return_value=positions) as fetch:
            response = self.client.post(
                "/api/aircraft/flightradar/sync",
                headers=auth_header(token or self.admin),
            )
        return response, fetch

    def test_администратор_добавляет_стоящий_борт(self):
        stand = self.free_stand()
        response, fetch = self.sync([position(stand, speed=30)])

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["airport_icao"], "UUEE")
        self.assertEqual(body["received"], 1)
        self.assertEqual(body["created"], 1)
        self.assertEqual(body["existing"], 0)
        self.assertEqual(body["skipped"], 0)
        self.assertEqual(body["items"][0]["status"], "created")
        self.assertEqual(body["items"][0]["aircraft_type"], "A320")
        self.assertEqual(body["items"][0]["stand_ref"], stand["ref"])
        fetch.assert_called_once()

        created = next(item for item in self.aircraft() if item["board_number"] == "RA-TEST1")
        self.assertEqual(created["stand_node_id"], stand["id"])
        self.assertNotIn("fr24_id", created)

    def test_существующий_борт_не_перемещается(self):
        before = self.aircraft()[0]
        stand = self.graph.node(before["stand_node_id"])
        live = position(
            stand,
            board=before["board_number"].lower(),
            source_type="B77W",
        )
        response, _ = self.sync([live])

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["existing"], 1)
        self.assertEqual(response.json()["created"], 0)
        after = next(
            item for item in self.aircraft() if item["board_number"] == before["board_number"]
        )
        self.assertEqual(after, before)

    def test_занятая_стоянка_не_переназначается(self):
        before = self.aircraft()[0]
        stand = self.graph.node(before["stand_node_id"])
        response, _ = self.sync([position(stand, board="RA-NEW-1")])

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["created"], 0)
        self.assertEqual(body["skipped"], 1)
        self.assertIn("занята", body["items"][0]["detail"])
        self.assertNotIn("RA-NEW-1", {item["board_number"] for item in self.aircraft()})

    def test_неизвестный_тип_и_скорость_пропускаются(self):
        stand = self.free_stand()
        positions = [
            position(stand, board="RA-UNKNOWN", source_type="X999"),
            position(stand, board="RA-FAST", speed=31),
            position(stand, board="RA-NO-SPEED", speed=None),
        ]
        response, _ = self.sync(positions)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["created"], 0)
        self.assertEqual(body["skipped"], 3)
        self.assertEqual({item["status"] for item in body["items"]}, {"skipped"})

    def test_удалённая_позиция_не_считается_стоянкой(self):
        stand = self.free_stand()
        live = position(stand, lat=stand["lat"] + 0.02)
        response, _ = self.sync([live])

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["skipped"], 1)
        self.assertIn("До ближайшей стоянки", response.json()["items"][0]["detail"])

    def test_два_новых_борта_не_занимают_одну_стоянку(self):
        stand = self.free_stand()
        response, _ = self.sync(
            [position(stand, board="RA-B"), position(stand, board="RA-A")]
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["created"], 1)
        self.assertEqual(body["skipped"], 1)
        self.assertEqual([item["status"] for item in body["items"]], ["created", "skipped"])

    def test_повтор_live_позиции_не_создаёт_дубликат(self):
        stand = self.free_stand()
        first = position(stand)
        second = position(stand, fr24_id="another-external-id")
        response, _ = self.sync([first, second])

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["received"], 2)
        self.assertEqual(response.json()["created"], 1)
        self.assertEqual(
            len([item for item in self.aircraft() if item["board_number"] == "RA-TEST1"]), 1
        )

    def test_пустая_выборка_ничего_не_меняет(self):
        before = self.aircraft()
        response, _ = self.sync([])

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["received"], 0)
        self.assertEqual(response.json()["items"], [])
        self.assertEqual(self.aircraft(), before)

    def test_диспетчер_не_может_запускать_синхронизацию(self):
        dispatcher = self.login("dispatcher", "dispatcher")
        response, fetch = self.sync([], token=dispatcher)

        self.assertEqual(response.status_code, 403, response.text)
        fetch.assert_not_called()

    def test_ошибка_fr24_не_завершает_локальную_сессию(self):
        with patch(
            "api.flightradar.fetch_flightradar_positions",
            side_effect=FlightradarError("Flightradar24 отклонил ключ доступа", 502),
        ):
            response = self.client.post(
                "/api/aircraft/flightradar/sync", headers=auth_header(self.admin)
            )

        self.assertEqual(response.status_code, 502, response.text)
        self.assertNotEqual(response.status_code, 401)
        self.assertEqual(self.client.get("/api/auth/me", headers=auth_header(self.admin)).status_code, 200)


if __name__ == "__main__":
    unittest.main()
