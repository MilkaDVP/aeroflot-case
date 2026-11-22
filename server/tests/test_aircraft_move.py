"""
Перенос борта: на стоянку или свободно — на ВПП, рулёжку.

Борт вне стоянки стоит ровно там, куда его поставили; маршрут инженера
к нему ведёт до ближайшей вершины рулёжной сети. Стоянку такой борт
не занимает.
"""

from algorithm.geo import haversine_m
from graph_registry import get_graph
from tests.test_api import ApiTestCase, auth_header


class TestAircraftMove(ApiTestCase):
    def setUp(self):
        self.reset_database()
        self.token = self.login("dispatcher", "dispatcher")

    def aircraft(self, board="VP-BZQ"):
        items = self.client.get("/api/aircraft", headers=auth_header(self.token)).json()
        return next(item for item in items if item["board_number"] == board)

    def move(self, aircraft_id, body):
        return self.client.post(
            f"/api/aircraft/{aircraft_id}/location", json=body, headers=auth_header(self.token)
        )

    def far_point(self):
        """Вершина сети подальше от всех стоянок — «на ВПП»."""
        graph = get_graph("UUEE")
        stands = graph.nodes_of_type("stand")
        best, best_gap = None, 0
        for node in graph.nodes.values():
            if node["type"] == "stand":
                continue
            gap = min(haversine_m(node["lat"], node["lon"], s["lat"], s["lon"]) for s in stands)
            if gap > best_gap:
                best, best_gap = node, gap
        return {"lat": best["lat"] + 0.0001, "lon": best["lon"] + 0.0001}

    def test_свободно_борт_встаёт_туда_куда_поставили(self):
        plane = self.aircraft()
        point = self.far_point()
        response = self.move(plane["id"], {**point, "free": True})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["on_stand"])
        self.assertIsNone(body["stand_ref"])
        self.assertAlmostEqual(body["lat"], point["lat"], places=6)
        self.assertAlmostEqual(body["lon"], point["lon"], places=6)

    def test_освобождённая_стоянка_свободна(self):
        plane = self.aircraft()
        old_stand = plane["stand_node_id"]
        self.move(plane["id"], {**self.far_point(), "free": True})
        other = self.aircraft("VP-BES")
        response = self.move(other["id"], {"lat": 0, "lon": 0, "stand_node_id": old_stand})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["stand_node_id"], old_stand)

    def test_на_указанную_стоянку(self):
        plane = self.aircraft()
        self.move(plane["id"], {**self.far_point(), "free": True})
        graph = get_graph("UUEE")
        occupied = {item["stand_node_id"] for item in self.client.get(
            "/api/aircraft", headers=auth_header(self.token)).json()}
        stand = next(n for n in graph.nodes_of_type("stand") if n["id"] not in occupied)
        response = self.move(plane["id"], {"lat": 0, "lon": 0, "stand_node_id": stand["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["on_stand"])
        self.assertEqual(body["stand_ref"], stand["ref"])

    def test_не_стоянка_отклоняется(self):
        plane = self.aircraft()
        graph = get_graph("UUEE")
        junction = next(n for n in graph.nodes.values() if n["type"] != "stand")
        response = self.move(plane["id"], {"lat": 0, "lon": 0, "stand_node_id": junction["id"]})
        self.assertEqual(response.status_code, 422, response.text)

    def test_без_указаний_как_раньше_ближайшая_стоянка(self):
        plane = self.aircraft()
        graph = get_graph("UUEE")
        node = graph.node(plane["stand_node_id"])
        response = self.move(plane["id"], {"lat": node["lat"], "lon": node["lon"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["on_stand"])

    def test_вызов_на_борт_вне_стоянки_подбирается(self):
        plane = self.aircraft()
        self.move(plane["id"], {**self.far_point(), "free": True})
        created = self.client.post(
            "/api/calls",
            json={"aircraft_id": plane["id"], "defect_code": "hydraulic_leak"},
            headers=auth_header(self.token),
        )
        self.assertEqual(created.status_code, 201, created.text)
        result = self.client.post(
            f"/api/calls/{created.json()['id']}/suggest", headers=auth_header(self.token)
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["candidates"])
