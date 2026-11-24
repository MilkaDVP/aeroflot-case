"""Маршрут по ходу движения: посадка, точки, выбор транспорта, скорость."""

import unittest
from types import SimpleNamespace as Obj

from algorithm.graph import load_airport
from algorithm.transport import STOP_RETURN, plan_after_return, plan_walk, warehouses
from constants import MODE_VEHICLE, MODE_WALK
from database import SessionLocal
from models.call import Call
from models.employee import Employee
from models.vehicle import Vehicle
from services.assignment import check_waypoints_reached, smooth
from tests.test_api import ApiTestCase, auth_header
from tests.test_polygon import HAS_POLYGON


class TestSpeed(unittest.TestCase):
    def test_рывок_не_переворачивает_скорость(self):
        self.assertEqual(smooth(None, 4), 4)
        self.assertAlmostEqual(smooth(5, 10), 6.5)


@unittest.skipUnless(HAS_POLYGON, "нет графа полигона")
class TestReached(unittest.TestCase):
    def setUp(self):
        self.graph = load_airport("VDNH")
        self.depot = sorted(warehouses(self.graph))[0]
        node = self.graph.node(self.depot)
        self.person = Obj(lat=node["lat"], lon=node["lon"])

    def call(self, **fields):
        return Obj(**{"needs_tools": True, "tools_taken_at": None, "waypoints": None,
                      "route_stops": [{"kind": "warehouse", "node_id": self.depot}], **fields})

    def test_склад_системы_отмечается_сам(self):
        call = self.call()
        self.assertTrue(check_waypoints_reached(call, self.person, self.graph))
        self.assertIsNotNone(call.tools_taken_at)
        self.assertFalse(check_waypoints_reached(call, self.person, self.graph))

    def test_склад_после_непройденной_точки_не_отмечается(self):
        other = self.graph.find_stand("4")["id"]
        call = self.call(waypoints=[{"node_id": other, "done_at": None}],
                         route_stops=[{"kind": "pass", "node_id": other}, {"kind": "warehouse", "node_id": self.depot}])
        self.assertFalse(check_waypoints_reached(call, self.person, self.graph))

    def test_возврат_машины_перед_новым_путём(self):
        stand = self.graph.find_stand("9")["id"]
        home = self.graph.nodes_of_type("vehicle_parking")[0]
        person = {"id": 1, "lat": self.person.lat, "lon": self.person.lon, "speed_kmh": None, "vehicle": None}
        plan = plan_after_return(self.graph, person, {"speed_kmh": None}, (home["lat"], home["lon"]),
                                 lambda start: plan_walk(self.graph, start, stand))
        self.assertEqual([leg.mode for leg in plan.legs], [MODE_VEHICLE, MODE_WALK])
        self.assertEqual(plan.stops[0]["kind"], STOP_RETURN)
        self.assertIsNone(plan.pickup_node_id)
        self.assertEqual(plan.node_ids[-1], stand)


@unittest.skipUnless(HAS_POLYGON, "нет графа полигона")
class TestTravelChoice(ApiTestCase):
    def setUp(self):
        self.reset_database()
        self.dispatcher = self.login("dispatcher_vdnh", "dispatcher", airport="VDNH")

    def board(self, number):
        return next(item for item in self.client.get("/api/aircraft", headers=auth_header(self.dispatcher)).json()
                    if item["board_number"] == number)

    def test_посадка_и_предпросмотр_транспорта(self):
        head = auth_header(self.dispatcher)
        call = self.client.post("/api/calls", json={"aircraft_id": self.board("RA-73004")["id"],
                                                    "defect_code": "hydraulic_leak"}, headers=head).json()
        best = self.client.post(f"/api/calls/{call['id']}/suggest", headers=head).json()["best"]
        employee = best["employee_id"]

        def preview(**body):
            response = self.client.post(f"/api/calls/{call['id']}/route-preview", json=body, headers=head)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        auto = preview(employee_id=employee, travel="auto")
        walk = preview(employee_id=employee, travel="walk")
        self.assertAlmostEqual(auto["minutes"], best["minutes"], places=1)
        self.assertIsNone(walk["vehicle"])
        self.assertIsNone(walk["pickup_node_id"])
        self.assertGreaterEqual(walk["minutes"], auto["minutes"])

        self.client.post(f"/api/calls/{call['id']}/assign",
                         json={"employee_id": employee, "override_reason": None, "travel": "walk"}, headers=head)
        with SessionLocal() as db:
            vehicle = db.query(Vehicle).filter_by(airport_icao="VDNH", status="free").first()
            vehicle_id, lat, lon = vehicle.id, vehicle.lat, vehicle.lon
        planned = preview(vehicle_id=vehicle_id)
        changed = self.client.post(f"/api/calls/{call['id']}/transport?vehicle_id={vehicle_id}", headers=head).json()
        # На карте до подтверждения — ровно то, что будет после него.
        self.assertEqual(planned["node_ids"], changed["route_node_ids"])
        self.assertIsNotNone(changed["pickup_node_id"])

        # Дошёл до машины: отметка и пеший участок уходят сразу.
        moved = self.client.post(f"/api/employees/{employee}/location",
                                 json={"lat": lat, "lon": lon, "source": "manual"}, headers=head)
        if moved.status_code == 405:
            moved = self.client.put(f"/api/employees/{employee}/location",
                                    json={"lat": lat, "lon": lon, "source": "manual"}, headers=head)
        self.assertEqual(moved.status_code, 200, moved.text)
        with SessionLocal() as db:
            record = db.get(Call, call["id"])
            self.assertEqual(db.get(Employee, employee).vehicle.status, "in_use")
            self.assertIsNone(record.pickup_node_id)
            self.assertNotIn("board", [stop["kind"] for stop in record.route_stops])
