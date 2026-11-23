"""ADSB.lol: реальный сервис в тестах подменяется, платные запросы не нужны."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from algorithm.graph import AirportGraph
from graph_registry import get_graph
from services.adsblol import airport_radius_nm, fetch_adsblol_positions
from services.flightradar import FlightradarError
from tests.test_api import ApiTestCase, auth_header


def response(payload, status=200):
    request = httpx.Request("GET", "https://api.adsb.lol/v2/test")
    return httpx.Response(status, json=payload, request=request)


def raw_aircraft(**changes):
    item = {
        "hex": "151dd8",
        "r": "RA-73176",
        "t": "A320",
        "gs": 0,
        "lat": 55.0,
        "lon": 38.0,
        "seen_pos": 2.5,
        "alt_baro": "ground",
    }
    item.update(changes)
    return item


def live_position(stand, board="RA-TEST-ADSB"):
    return SimpleNamespace(
        fr24_id="external-id-not-for-storage",
        reg=board,
        type="A20N",
        gspeed=0,
        lat=stand["lat"],
        lon=stand["lon"],
    )


class AdsblolServiceTests(unittest.TestCase):
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

    def test_радиус_покрывает_стоянки(self):
        self.assertEqual(airport_radius_nm(self.graph), 1)

    def test_запрос_без_ключа_и_адаптация_полей_readsb(self):
        payload = {
            "ac": [
                raw_aircraft(),
                raw_aircraft(hex="stale", r="RA-STALE", seen_pos=61),
                raw_aircraft(hex="old", r="RA-OLD", seen_pos=None),
                raw_aircraft(hex="no-position", r="RA-NONE", lat=None),
            ],
            "total": 4,
        }
        with patch("services.adsblol.httpx.get", return_value=response(payload)) as get:
            positions = fetch_adsblol_positions(self.graph)

        self.assertEqual(len(positions), 1)
        position = positions[0]
        self.assertEqual(position.reg, "RA-73176")
        self.assertEqual(position.type, "A320")
        self.assertEqual(position.gspeed, 0.0)
        self.assertEqual(position.lat, 55.0)
        self.assertEqual(position.lon, 38.0)
        get.assert_called_once_with(
            "https://api.adsb.lol/v2/lat/55.000000/lon/38.000000/dist/1",
            headers={
                "Accept": "application/json",
                "User-Agent": "aeroflot-smart-dispatch/1.0",
            },
            timeout=15.0,
        )

    def test_граница_свежести_60_секунд(self):
        payload = {"ac": [raw_aircraft(seen_pos=60)]}
        with patch("services.adsblol.httpx.get", return_value=response(payload)):
            self.assertEqual(len(fetch_adsblol_positions(self.graph)), 1)

    def test_некорректная_скорость_не_роняет_общий_алгоритм(self):
        payload = {"ac": [raw_aircraft(gs="fast")]}
        with patch("services.adsblol.httpx.get", return_value=response(payload)):
            positions = fetch_adsblol_positions(self.graph)
        self.assertIsNone(positions[0].gspeed)

    def test_неверный_timeout_не_выполняет_запрос(self):
        with patch.dict("os.environ", {"ADSBLOL_API_TIMEOUT_SECONDS": "0"}):
            with patch("services.adsblol.httpx.get") as get:
                with self.assertRaises(FlightradarError) as raised:
                    fetch_adsblol_positions(self.graph)
        self.assertEqual(raised.exception.http_status, 503)
        get.assert_not_called()

    def test_http_и_transport_ошибки_безопасно_преобразуются(self):
        request = httpx.Request("GET", "https://api.adsb.lol/v2/test")
        cases = [
            (response({"ac": []}, 429), 503),
            (response({"ac": []}, 500), 502),
            (httpx.TimeoutException("timeout", request=request), 503),
        ]
        for result, expected_status in cases:
            with self.subTest(result=result):
                with patch(
                    "services.adsblol.httpx.get",
                    return_value=result if isinstance(result, httpx.Response) else None,
                    side_effect=result if isinstance(result, Exception) else None,
                ):
                    with self.assertRaises(FlightradarError) as raised:
                        fetch_adsblol_positions(self.graph)
                self.assertEqual(raised.exception.http_status, expected_status)

    def test_некорректный_json_и_структура_отклоняются(self):
        request = httpx.Request("GET", "https://api.adsb.lol/v2/test")
        responses = [
            httpx.Response(200, text="not-json", request=request),
            response({"aircraft": []}),
        ]
        for external_response in responses:
            with self.subTest(body=external_response.text):
                with patch("services.adsblol.httpx.get", return_value=external_response):
                    with self.assertRaises(FlightradarError) as raised:
                        fetch_adsblol_positions(self.graph)
                self.assertEqual(raised.exception.http_status, 502)


class TestAdsblolApi(ApiTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.graph = get_graph("UUEE")

    def setUp(self):
        self.reset_database()
        self.admin = self.login("admin", "admin")

    def free_stand(self):
        response = self.client.get("/api/aircraft", headers=auth_header(self.admin))
        occupied = {item["stand_node_id"] for item in response.json()}
        return next(
            node for node in self.graph.nodes_of_type("stand") if node["id"] not in occupied
        )

    def test_администратор_добавляет_борт_без_api_ключа(self):
        stand = self.free_stand()
        with patch(
            "api.adsblol.fetch_adsblol_positions", return_value=[live_position(stand)]
        ) as fetch:
            result = self.client.post(
                "/api/aircraft/adsblol/sync", headers=auth_header(self.admin)
            )

        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertEqual(body["provider"], "adsblol")
        self.assertEqual(body["created"], 1)
        self.assertEqual(body["items"][0]["aircraft_type"], "A320")
        fetch.assert_called_once()
        aircraft = self.client.get("/api/aircraft", headers=auth_header(self.admin)).json()
        created = next(item for item in aircraft if item["board_number"] == "RA-TEST-ADSB")
        self.assertEqual(created["stand_node_id"], stand["id"])
        self.assertNotIn("hex", created)

    def test_диспетчер_не_может_запускать_синхронизацию(self):
        dispatcher = self.login("dispatcher", "dispatcher")
        with patch("api.adsblol.fetch_adsblol_positions") as fetch:
            result = self.client.post(
                "/api/aircraft/adsblol/sync", headers=auth_header(dispatcher)
            )
        self.assertEqual(result.status_code, 403, result.text)
        fetch.assert_not_called()

    def test_внешний_лимит_не_завершает_локальную_сессию(self):
        with patch(
            "api.adsblol.fetch_adsblol_positions",
            side_effect=FlightradarError("Превышен лимит ADSB.lol", 503),
        ):
            result = self.client.post(
                "/api/aircraft/adsblol/sync", headers=auth_header(self.admin)
            )
        self.assertEqual(result.status_code, 503, result.text)
        session = self.client.get("/api/auth/me", headers=auth_header(self.admin))
        self.assertEqual(session.status_code, 200)


if __name__ == "__main__":
    unittest.main()
