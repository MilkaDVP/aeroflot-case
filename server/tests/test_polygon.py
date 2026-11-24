"""
Испытательный полигон ВДНХ: адаптация (критерий 4), маршрутизация со
скоростным режимом (5) и маршруты через промежуточные точки (6).
"""

import unittest

from tests.test_api import ApiTestCase, auth_header  # noqa: I001 — окружение тестов первым

from algorithm.graph import AirportGraph, load_airport
from algorithm.routing import shortest_path
from algorithm.transport import STOP_BOARD, STOP_WAREHOUSE, plan_for, warehouses
from constants import (
    AIRPORTS_DIR,
    MODE_VEHICLE,
    MODE_WALK,
    TOOL_PICKUP_S,
    VEHICLE_BOARDING_S,
)

HAS_POLYGON = (AIRPORTS_DIR / "VDNH.json").exists()
AGENT_KINDS = {"service", "pedestrian"}


def edge_kinds(graph, node_ids):
    """Виды дорог, по которым идёт маршрут (быстрейшее из параллельных рёбер)."""
    index = {}
    for edge in graph.edges:
        index.setdefault((edge["from_id"], edge["to_id"]), []).append(edge)
        index.setdefault((edge["to_id"], edge["from_id"]), []).append(edge)
    return [
        {edge.get("kind") for edge in index[(a, b)]} for a, b in zip(node_ids, node_ids[1:])
    ]


@unittest.skipUnless(HAS_POLYGON, "нет data/airports/VDNH.json — python data/import_geodata.py")
class TestPolygonGraph(unittest.TestCase):
    """Граф полигона из геоданных организаторов."""

    @classmethod
    def setUpClass(cls):
        cls.graph = load_airport("VDNH")

    def test_объекты_и_зоны_на_месте(self):
        self.assertEqual(len(self.graph.nodes_of_type("stand")), 10)
        self.assertEqual(len(self.graph.zones), 10)
        self.assertEqual(len(self.graph.nodes_of_type("warehouse")), 2)
        self.assertEqual(len(self.graph.nodes_of_type("vehicle_parking")), 1)
        self.assertEqual(len(self.graph.nodes_of_type("tech_center")), 1)
        names = {item["name"] for item in self.graph.objects}
        self.assertTrue({"Терминал", "Ангар", "Склад №1", "Склад №2", "Стоянка спецтехники"} <= names)
        self.assertTrue(self.graph.boundary)
        kinds = {edge["kind"] for edge in self.graph.edges}
        self.assertEqual(kinds, {"service", "pedestrian", "taxi", "runway"})

    def test_все_стоянки_достижимы_пешком_и_на_машине(self):
        hangar = self.graph.nodes_of_type("tech_center")[0]["id"]
        parking = self.graph.nodes_of_type("vehicle_parking")[0]["id"]
        for stand in self.graph.nodes_of_type("stand"):
            self.assertIsNotNone(shortest_path(self.graph, hangar, stand["id"], MODE_WALK), stand["ref"])
            self.assertIsNotNone(shortest_path(self.graph, parking, stand["id"], MODE_VEHICLE), stand["ref"])

    def test_маршрут_не_идёт_по_рулёжкам_и_впп(self):
        starts = [n["id"] for n in self.graph.nodes_of_type("tech_center") + self.graph.nodes_of_type("vehicle_parking")]
        for start in starts:
            for stand in self.graph.nodes_of_type("stand"):
                for mode in (MODE_WALK, MODE_VEHICLE):
                    route = shortest_path(self.graph, start, stand["id"], mode)
                    if route is None:
                        continue
                    for kinds in edge_kinds(self.graph, route.node_ids):
                        self.assertTrue(kinds & AGENT_KINDS, f"{mode} {stand['ref']}: {kinds}")

    def test_машина_только_по_служебным_дорогам(self):
        parking = self.graph.nodes_of_type("vehicle_parking")[0]["id"]
        for stand in self.graph.nodes_of_type("stand"):
            route = shortest_path(self.graph, parking, stand["id"], MODE_VEHICLE)
            for kinds in edge_kinds(self.graph, route.node_ids):
                self.assertIn("service", kinds)

    def test_время_машины_по_скоростному_режиму(self):
        # Время маршрута — сумма участков по их ограничениям (5/8/10/20 км/ч),
        # а не длина, делённая на одну скорость.
        parking = self.graph.nodes_of_type("vehicle_parking")[0]["id"]
        stand = self.graph.find_stand("9")["id"]
        route = shortest_path(self.graph, parking, stand, MODE_VEHICLE)
        expected = 0.0
        for a, b in zip(route.node_ids, route.node_ids[1:]):
            expected += min(
                edge["distance_m"] / (min(20.0, edge["speed_kmh"] or 20.0) / 3.6)
                for edge in self.graph.edges
                if {edge["from_id"], edge["to_id"]} == {a, b} and edge["vehicle_allowed"]
            )
        self.assertAlmostEqual(route.minutes * 60, expected, places=3)
        self.assertGreater(route.minutes, route.distance_m / (20 / 3.6) / 60)

    def test_зона_обслуживания_по_координатам(self):
        stand = self.graph.find_stand("3")
        zone = self.graph.zone_at(stand["lat"], stand["lon"])
        self.assertIsNotNone(zone)
        self.assertEqual(zone["ref"], "3")


def fork_graph():
    """
    Скоростной режим против длины:

        A ──200 м, 5 км/ч── D         прямо, но медленно: 144 с
        A ──150── B ──150── D         в объезд, 20 км/ч: 54 с

    Кратчайший путь — прямой, самый быстрый — объезд.
    """
    node = lambda i, lat: {"id": i, "type": "junction", "ref": None, "lat": lat, "lon": 37.0, "x": 0, "y": 0}
    return AirportGraph({
        "icao": "FORK", "name": "—", "city": "—", "ref_point": {"lat": 55.0, "lon": 37.0},
        "nodes": [node("A", 55.0), node("B", 55.001), node("D", 55.002)],
        "edges": [
            {"from_id": "A", "to_id": "D", "distance_m": 200, "vehicle_allowed": True, "speed_kmh": 5},
            {"from_id": "A", "to_id": "B", "distance_m": 150, "vehicle_allowed": True, "speed_kmh": 20},
            {"from_id": "B", "to_id": "D", "distance_m": 150, "vehicle_allowed": True, "speed_kmh": 20},
            {"from_id": "A", "to_id": "D", "distance_m": 10, "vehicle_allowed": False,
             "walk_allowed": False, "kind": "taxi"},
        ],
    })


class TestSpeedRegime(unittest.TestCase):
    def test_выбирается_самый_быстрый_а_не_кратчайший(self):
        route = shortest_path(fork_graph(), "A", "D", MODE_VEHICLE)
        self.assertEqual(route.node_ids, ["A", "B", "D"])
        self.assertAlmostEqual(route.minutes * 60, 300 / (20 / 3.6), places=6)

    def test_пешком_ограничение_не_ускоряет(self):
        # Пешеход идёт 5 км/ч и по дороге с ограничением 20 — прямой путь короче.
        route = shortest_path(fork_graph(), "A", "D", MODE_WALK)
        self.assertEqual(route.node_ids, ["A", "D"])
        self.assertAlmostEqual(route.minutes * 60, 200 / (5 / 3.6), places=6)

    def test_рулёжка_закрыта_для_всех_агентов(self):
        # Ребро-рулёжка в 10 м не используется ни пешком, ни на машине.
        graph = fork_graph()
        for mode in (MODE_WALK, MODE_VEHICLE):
            self.assertGreater(shortest_path(graph, "A", "D", mode).distance_m, 10)


@unittest.skipUnless(HAS_POLYGON, "нет графа полигона")
class TestWaypoints(unittest.TestCase):
    """Маршрут через склад и стоянку спецтехники (две промежуточные точки)."""

    @classmethod
    def setUpClass(cls):
        cls.graph = load_airport("VDNH")
        hangar = cls.graph.nodes_of_type("tech_center")[0]
        parking = cls.graph.nodes_of_type("vehicle_parking")[0]
        cls.engineer = {"id": 1, "lat": hangar["lat"], "lon": hangar["lon"], "vehicle": None}
        cls.vehicles = [{"id": 7, "call_sign": "ТМ-01", "lat": parking["lat"], "lon": parking["lon"],
                         "speed_kmh": None}]

    def test_через_склад_и_машину(self):
        stand = self.graph.find_stand("9")["id"]
        plan = plan_for(self.graph, self.engineer, stand, self.vehicles, warehouses(self.graph))
        kinds = [stop["kind"] for stop in plan.waypoints]
        self.assertEqual(kinds, [STOP_WAREHOUSE, STOP_BOARD])
        self.assertTrue(plan.pickup)
        # Порядок в маршруте: склад раньше посадки, посадка раньше борта.
        nodes = plan.node_ids
        self.assertLess(nodes.index(plan.warehouse_node_id), nodes.index(plan.pickup_node_id))
        self.assertEqual(nodes[-1], stand)
        # Задержки по регламенту входят во время.
        self.assertEqual(plan.dwell_s, TOOL_PICKUP_S + 2 * VEHICLE_BOARDING_S)
        legs = sum(leg.minutes for leg in plan.legs)
        self.assertAlmostEqual(plan.minutes, legs + plan.dwell_s / 60, places=6)

    def test_одна_промежуточная_точка(self):
        stand = self.graph.find_stand("1")["id"]
        plan = plan_for(self.graph, self.engineer, stand, [], warehouses(self.graph))
        self.assertEqual([stop["kind"] for stop in plan.waypoints], [STOP_WAREHOUSE])

    def test_склад_увеличивает_время_не_меньше_чем_на_30_с(self):
        for ref in ("1", "5", "9"):
            stand = self.graph.find_stand(ref)["id"]
            direct = plan_for(self.graph, self.engineer, stand, self.vehicles)
            via = plan_for(self.graph, self.engineer, stand, self.vehicles, warehouses(self.graph))
            self.assertGreaterEqual(via.minutes, direct.minutes + TOOL_PICKUP_S / 60 - 1e-9)

    def test_выбран_быстрейший_из_вариантов(self):
        # Перебор: любой ручной вариант не быстрее выбранного системой.
        stand = self.graph.find_stand("9")["id"]
        best = plan_for(self.graph, self.engineer, stand, self.vehicles, warehouses(self.graph))
        for depot in warehouses(self.graph):
            for vehicles in ([], self.vehicles):
                other = plan_for(self.graph, self.engineer, stand, vehicles, [depot])
                self.assertLessEqual(best.minutes, other.minutes + 1e-9)


@unittest.skipUnless(HAS_POLYGON, "нет графа полигона")
class TestChosenWaypoints(unittest.TestCase):
    """Точки, выбранные диспетчером: порядок, вид точки, пересадка."""

    @classmethod
    def setUpClass(cls):
        cls.graph = load_airport("VDNH")
        hangar = cls.graph.nodes_of_type("tech_center")[0]
        parking = cls.graph.nodes_of_type("vehicle_parking")[0]
        cls.engineer = {"id": 1, "lat": hangar["lat"], "lon": hangar["lon"], "vehicle": None}
        cls.vehicles = [{"id": 7, "call_sign": "ТМ-01", "lat": parking["lat"], "lon": parking["lon"],
                         "speed_kmh": None}]
        cls.parking = parking["id"]
        cls.w1, cls.w2 = sorted(warehouses(cls.graph))

    def test_точки_проходятся_по_порядку(self):
        goal = self.graph.find_stand("1")["id"]
        through = self.graph.find_stand("4")["id"]
        plan = plan_for(self.graph, self.engineer, goal, self.vehicles, waypoints=[through, self.w1])
        nodes = plan.node_ids
        self.assertLess(nodes.index(through), nodes.index(self.w1))
        kinds = [stop["kind"] for stop in plan.waypoints if stop["kind"] in ("pass", "warehouse")]
        self.assertEqual(kinds, ["pass", "warehouse"])

    def test_выбранный_склад_а_не_ближайший(self):
        goal = self.graph.find_stand("9")["id"]
        for depot in (self.w1, self.w2):
            plan = plan_for(self.graph, self.engineer, goal, self.vehicles, waypoints=[depot])
            self.assertEqual(plan.warehouse_node_id, depot)

    def test_стоянка_спецтехники_это_пересадка(self):
        goal = self.graph.find_stand("8")["id"]
        # К стоянке 8 из ангара быстрее пешком, но точка «стоянка спецтехники»
        # выбрана — значит, там садятся в машину.
        direct = plan_for(self.graph, self.engineer, goal, self.vehicles)
        self.assertFalse(direct.pickup)
        plan = plan_for(self.graph, self.engineer, goal, self.vehicles, waypoints=[self.parking])
        self.assertTrue(plan.pickup)
        self.assertIn("board", [stop["kind"] for stop in plan.stops])

    def test_три_точки(self):
        goal = self.graph.find_stand("10")["id"]
        plan = plan_for(self.graph, self.engineer, goal, self.vehicles,
                        waypoints=[self.w2, self.parking, self.graph.find_stand("7")["id"]])
        nodes = plan.node_ids
        self.assertLess(nodes.index(self.w2), nodes.index(plan.pickup_node_id))
        self.assertEqual(nodes[-1], goal)


@unittest.skipUnless(HAS_POLYGON, "нет графа полигона")
class TestPolygonApi(ApiTestCase):
    """Полигон в работающей системе: карта, вызов с инструментом, назначение."""

    def setUp(self):
        self.reset_database()
        self.dispatcher = self.login("dispatcher_vdnh", "dispatcher", airport="VDNH")

    def test_карта_полигона(self):
        graph = self.client.get("/api/airports/VDNH", headers=auth_header(self.dispatcher)).json()
        self.assertEqual(len(graph["zones"]), 10)
        self.assertTrue(any(item["kind"] == "warehouse" for item in graph["objects"]))
        self.assertTrue(graph["boundary"])
        self.assertTrue(all("kind" in edge and "speed_kmh" in edge for edge in graph["edges"]))

    def test_вызов_с_инструментом_от_подбора_до_склада(self):
        aircraft = next(item for item in self.client.get(
            "/api/aircraft", headers=auth_header(self.dispatcher)).json() if item["board_number"] == "RA-73004")
        call = self.client.post(
            "/api/calls",
            json={"aircraft_id": aircraft["id"], "defect_code": "hydraulic_leak", "needs_tools": True},
            headers=auth_header(self.dispatcher),
        ).json()
        self.assertTrue(call["needs_tools"])
        suggestion = self.client.post(f"/api/calls/{call['id']}/suggest",
                                      headers=auth_header(self.dispatcher)).json()
        best = suggestion["best"]
        kinds = [stop["kind"] for stop in best["route"]["stops"]]
        self.assertIn("warehouse", kinds)

        assigned = self.client.post(
            f"/api/calls/{call['id']}/assign",
            json={"employee_id": best["employee_id"], "override_reason": None},
            headers=auth_header(self.dispatcher),
        )
        self.assertEqual(assigned.status_code, 200, assigned.text)
        body = assigned.json()
        warehouse = next(stop for stop in body["route_stops"] if stop["kind"] == "warehouse")
        self.assertIn(warehouse["node_id"], body["route_node_ids"])
        self.assertAlmostEqual(body["eta_minutes"], best["minutes"], places=1)

        # Инженер получил инструмент — маршрут перестроен без склада.
        from database import SessionLocal
        from models.user import User

        with SessionLocal() as db:
            login = db.query(User).filter(User.employee_id == best["employee_id"]).one().login
        engineer = self.login(login, "engineer", airport="VDNH")
        response = self.client.post("/api/me/tools", json={"taken": True}, headers=auth_header(engineer))
        self.assertEqual(response.status_code, 200, response.text)
        after = response.json()
        self.assertIsNotNone(after["tools_taken_at"])
        self.assertNotIn("warehouse", [stop["kind"] for stop in after["route_stops"]])


    def board(self, number):
        return next(item for item in self.client.get(
            "/api/aircraft", headers=auth_header(self.dispatcher)).json() if item["board_number"] == number)

    def test_точки_при_регистрации_и_после_назначения(self):
        graph = load_airport("VDNH")
        depot = sorted(warehouses(graph))[0]
        call = self.client.post(
            "/api/calls",
            json={"aircraft_id": self.board("RA-73004")["id"], "defect_code": "hydraulic_leak",
                  "waypoints": [depot]},
            headers=auth_header(self.dispatcher),
        ).json()
        self.assertEqual([item["node_id"] for item in call["waypoints"]], [depot])
        best = self.client.post(f"/api/calls/{call['id']}/suggest",
                                headers=auth_header(self.dispatcher)).json()["best"]
        self.assertEqual([s["node_id"] for s in best["route"]["stops"] if s["kind"] == "warehouse"], [depot])
        assigned = self.client.post(
            f"/api/calls/{call['id']}/assign",
            json={"employee_id": best["employee_id"], "override_reason": None},
            headers=auth_header(self.dispatcher),
        ).json()
        self.assertIn(depot, assigned["route_node_ids"])

        # Добавили точку после назначения — маршрут пересчитан сразу.
        through = graph.find_stand("4")["id"]
        changed = self.client.post(f"/api/calls/{call['id']}/waypoints",
                                   json={"node_ids": [depot, through]}, headers=auth_header(self.dispatcher))
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertIn(through, changed.json()["route_node_ids"])

        # Расчёт лучшего порядка: ничего не меняет, время не хуже названного.
        for keep in (True, False):
            planned = self.client.post(
                f"/api/calls/{call['id']}/waypoints/plan",
                json={"groups": [[through], warehouses(graph)], "keep_order": keep},
                headers=auth_header(self.dispatcher))
            self.assertEqual(planned.status_code, 200, planned.text)
            if keep:
                kept = planned.json()
                self.assertEqual(kept["node_ids"][0], through)
        self.assertLessEqual(planned.json()["minutes"], kept["minutes"])
        self.assertEqual(set(planned.json()) >= {"names", "repeats", "direct_minutes", "as_named"}, True)
        same = next(item for item in self.client.get("/api/calls", headers=auth_header(self.dispatcher)).json()
                    if item["id"] == call["id"])
        self.assertEqual([item["node_id"] for item in same["waypoints"]], [depot, through])

        # Инженер отмечает точки пройденными — по одной, по порядку.
        from database import SessionLocal
        from models.user import User

        with SessionLocal() as db:
            login = db.query(User).filter(User.employee_id == best["employee_id"]).one().login
        engineer = self.login(login, "engineer", airport="VDNH")
        first = self.client.post("/api/me/waypoint", json={}, headers=auth_header(engineer)).json()
        done = [item for item in first["waypoints"] if item["done_at"]]
        self.assertEqual([item["node_id"] for item in done], [depot])
        self.assertNotIn("warehouse", [s["kind"] for s in first["route_stops"]])

    def test_точка_вне_карты_отклоняется(self):
        response = self.client.post(
            "/api/calls",
            json={"aircraft_id": self.board("RA-73001")["id"], "defect_code": "hydraulic_leak",
                  "waypoints": ["нет-такой"]},
            headers=auth_header(self.dispatcher),
        )
        self.assertEqual(response.status_code, 422)

    def test_точка_засчитывается_при_подходе(self):
        graph = load_airport("VDNH")
        depot = sorted(warehouses(graph))[0]
        call = self.client.post(
            "/api/calls",
            json={"aircraft_id": self.board("RA-73001")["id"], "defect_code": "hydraulic_leak",
                  "waypoints": [depot]},
            headers=auth_header(self.dispatcher),
        ).json()
        best = self.client.post(f"/api/calls/{call['id']}/suggest",
                                headers=auth_header(self.dispatcher)).json()["best"]
        self.client.post(f"/api/calls/{call['id']}/assign",
                         json={"employee_id": best["employee_id"], "override_reason": None},
                         headers=auth_header(self.dispatcher))
        from database import SessionLocal
        from models.user import User

        with SessionLocal() as db:
            login = db.query(User).filter(User.employee_id == best["employee_id"]).one().login
        engineer = self.login(login, "engineer", airport="VDNH")
        node = graph.node(depot)
        response = self.client.post(
            f"/api/employees/{best['employee_id']}/location",
            json={"lat": node["lat"], "lon": node["lon"], "source": "gps"},
            headers=auth_header(engineer),
        )
        self.assertEqual(response.status_code, 200, response.text)
        calls = self.client.get("/api/calls", headers=auth_header(self.dispatcher)).json()
        current = next(item for item in calls if item["id"] == call["id"])
        self.assertTrue(current["waypoints"][0]["done_at"])


if __name__ == "__main__":
    unittest.main()
