"""Совместная работа диспетчера, администратора и инженера на отдельной SQLite."""
from concurrent.futures import ThreadPoolExecutor

from graph_registry import get_graph
from tests.test_api import auth_header
from tests.test_support import SupportCase


class TestDispatchChanges(SupportCase):
    def setUp(self):
        super().setUp()
        self.admin = self.login("admin", "admin")
        self.engineer = self.login("sokolov", "engineer")

    def cancel(self, call_id):
        return self.client.patch(
            f"/api/calls/{call_id}/status", headers=auth_header(self.dispatcher),
            json={"status": "cancelled"},
        )

    def current(self):
        return self.client.get("/api/me/current-call", headers=auth_header(self.engineer)).json()

    def test_cancel_team_and_advance_once(self):
        call, lead, helper = self.team()
        queue = []
        for _ in range(2):
            queued = self.register("VP-BES")
            queue.append(queued["id"])
            self.assign(queued["id"], lead["employee_id"], "Очередь смены", self.supervisor)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(self.cancel, [call["id"], call["id"]]))
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(responses[0].json()["closed_at"], responses[1].json()["closed_at"])
        self.assertEqual(self.current()["call"]["id"], queue[0])
        self.assertEqual(self.current()["queued_count"], 1)
        self.assertEqual(self.call(queue[1])["status"], "queued")
        self.assertEqual(self.call(call["support"][0]["call_id"])["status"], "cancelled")
        self.assertEqual(self.employee(helper["employee_id"])["status"], "free")
        blocked = self.add_support(call["id"], helper["employee_id"])
        self.assertEqual(blocked.status_code, 409)

    def test_move_team_and_keep_history(self):
        past = self.register()
        response = self.client.patch(
            f"/api/calls/{past['id']}/status", headers=auth_header(self.dispatcher),
            json={"status": "cancelled"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        active, _, _ = self.team()
        graph = get_graph("UUEE")
        boards = self.client.get("/api/aircraft", headers=auth_header(self.dispatcher)).json()
        occupied = {board["stand_node_id"] for board in boards}
        target = next(node for node in graph.nodes_of_type("stand") if node["id"] not in occupied)
        url = f"/api/aircraft/{active['aircraft_id']}/location"
        response = self.client.post(url, json=target, headers=auth_header(self.dispatcher))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.current()["route_points"][-1]["id"], target["id"])
        moved = self.call(active["id"])
        self.assertEqual(moved["support"][0]["route_node_ids"][-1], target["id"])
        self.assertEqual(self.call(past["id"])["stand_ref"], past["stand_ref"])

        other = next(b for b in boards if b["id"] != active["aircraft_id"])
        conflict = self.client.post(url, json=graph.node(other["stand_node_id"]), headers=auth_header(self.dispatcher))
        self.assertEqual(conflict.status_code, 409)
        self.assertIn(other["board_number"], conflict.json()["detail"])
        self.assertEqual(self.current()["call"]["stand_node_id"], target["id"])
        self.assertEqual(self.client.post(url, json=target, headers=auth_header(self.engineer)).status_code, 403)

    def test_cancel_queued_call_keeps_reserved_car_at_pickup(self):
        call = self.register()
        engineer_id = self.employee_id("Морозов Д. С.")
        working = self.assign(call["id"], engineer_id, "Проверка машины", self.supervisor)
        self.assertIsNotNone(working["pickup_node_id"])
        fleet = self.client.get("/api/vehicles", headers=auth_header(self.admin)).json()
        car = next(v for v in fleet if v["call_sign"] == working["vehicle_call_sign"])

        queued = self.register("VP-BES")
        self.assign(queued["id"], engineer_id, "Очередь смены", self.supervisor)
        self.assertEqual(self.cancel(queued["id"]).status_code, 200)
        self.assertEqual(self.call(call["id"])["status"], "assigned")
        self.assertEqual(self.employee(engineer_id)["vehicle_call_sign"], car["call_sign"])
        self.assertEqual(self.cancel(call["id"]).status_code, 200)
        after = self.client.get("/api/vehicles", headers=auth_header(self.admin)).json()
        released = next(v for v in after if v["id"] == car["id"])
        self.assertEqual(released["status"], "free")
        self.assertEqual((released["lat"], released["lon"]), (car["lat"], car["lon"]))

    def test_archive_with_history_and_revoke_session(self):
        call, lead, _ = self.team()
        employee = lead["employee_id"]
        fleet = self.client.get("/api/vehicles", headers=auth_header(self.admin)).json()
        car = next(v for v in fleet if v["call_sign"] == call["vehicle_call_sign"])
        urls = [f"/api/employees/{employee}", f"/api/vehicles/{car['id']}"]
        other_airport = self.login("admin", "admin", airport="UUDD")
        for url in urls:
            self.assertEqual(self.client.delete(url, headers=auth_header(self.dispatcher)).status_code, 403)
            self.assertEqual(self.client.delete(url, headers=auth_header(other_airport)).status_code, 404)
            self.assertEqual(self.client.delete(url, headers=auth_header(self.admin)).status_code, 409)
        self.cancel(call["id"])
        for url in urls:
            self.assertEqual(self.client.delete(url, headers=auth_header(self.admin)).status_code, 204)
        self.assertEqual(self.client.get("/api/auth/me", headers=auth_header(self.engineer)).status_code, 401)
        login = self.client.post("/api/auth/login", json={"login": "sokolov", "password": "engineer"})
        self.assertEqual(login.status_code, 401)
        past = self.call(call["id"])
        self.assertEqual(past["assigned_employee_name"], lead["full_name"])
        self.assertEqual(past["vehicle_call_sign"], car["call_sign"])
        new_call = self.register()
        result = self.suggest(new_call["id"])
        for group in ("candidates", "busy", "rejected"):
            self.assertNotIn(employee, [item["employee_id"] for item in result[group]])
        for collection, object_id in (("employees", employee), ("vehicles", car["id"])):
            items = self.client.get(f"/api/{collection}", headers=auth_header(self.admin)).json()
            self.assertNotIn(object_id, [item["id"] for item in items])
