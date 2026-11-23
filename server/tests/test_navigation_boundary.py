"""Границы навигации: метры до отрезка, тип маршрута и последовательность возврата."""
import math
import unittest
from types import SimpleNamespace as Obj
from unittest.mock import patch

from algorithm.geo import EARTH_RADIUS_M
from constants import MODE_VEHICLE, MODE_WALK
from services import assignment as service


class TestRouteBoundary(unittest.TestCase):
    def setUp(self):
        self.nodes = {"a": {"lat": 0, "lon": 0}, "b": {"lat": 0, "lon": .001}}
        self.person = Obj(lat=0, lon=.0005, airport_icao="TEST", return_vehicle=False,
                          return_to_waiting=False)
        self.call = Obj(status="accepted", route_node_ids=["a", "b"], route_start_lat=None, route_start_lon=None)

    def check_replan(self, expected):
        with (patch.object(service, "working_call_of", return_value=self.call),
              patch.object(service, "get_graph", return_value=Obj(node=self.nodes.get)),
              patch.object(service, "assigned_route") as plan,
              patch.object(service, "update_assigned_route") as update):
            service.refresh_navigation(None, self.person)
            self.assertEqual(plan.called, expected)
            self.assertEqual(update.called, expected)

    def test_perpendicular_offsets(self):
        for meters in (0, 1, 19, 19.99, 20, 20.01, 21, 49, 51):
            for sign in (-1, 1):
                with self.subTest(meters=meters, sign=sign):
                    self.person.lat = math.degrees(sign * meters / EARTH_RADIUS_M)
                    self.check_replan(meters > 20)

    def test_distance_beyond_end_of_segment(self):
        for meters in (5, 19.99, 20.01, 80):
            with self.subTest(meters=meters):
                self.person.lon = .001 + math.degrees(meters / EARTH_RADIUS_M)
                self.check_replan(meters > 20)

    def test_approach_from_actual_start_is_part_of_route(self):
        self.call.route_start_lat, self.call.route_start_lon = -.001, 0
        self.person.lat, self.person.lon = -.0005, 0
        self.check_replan(False)
        self.person.lon = math.degrees(21 / EARTH_RADIUS_M)
        self.check_replan(True)

    def test_single_node(self):
        self.call.route_node_ids = ["a"]
        for meters in (0, 19, 21):
            with self.subTest(meters=meters):
                self.person.lon = math.degrees(meters / EARTH_RADIUS_M)
                self.check_replan(meters > 20)

    def test_missing_route(self):
        for nodes in ([], None, ["missing"]):
            with self.subTest(nodes=nodes):
                self.call.route_node_ids = nodes
                self.check_replan(True)

    def test_closed_leg_is_not_replanned(self):
        self.call.status = "arrived"
        self.person.lon = 1
        self.check_replan(False)

    def test_idle_engineer_needs_no_call_route(self):
        self.call = None
        self.check_replan(False)

    def test_all_return_leg_combinations(self):
        self.person.vehicle = Obj(lat=.01, lon=.02)
        self.person.waiting_lat, self.person.waiting_lon = .03, .04
        for vehicle, waiting, mode in ((False, False, None), (True, False, MODE_VEHICLE),
                                       (False, True, MODE_WALK), (True, True, MODE_VEHICLE)):
            with self.subTest(vehicle=vehicle, waiting=waiting):
                self.person.return_vehicle, self.person.return_to_waiting = vehicle, waiting
                target = service.return_destination(self.person)
                self.assertEqual(target[2] if target else None, mode)
                if target:
                    self.assertEqual(target[:2], (.01, .02) if vehicle else (.03, .04))

    def test_queue_does_not_interrupt_vehicle_return(self):
        self.person.return_vehicle = True
        self.person.vehicle = Obj()
        queued = Obj()
        with patch.object(service, "replan_queue"):
            service.enqueue(None, queued, self.person)
        self.assertTrue(self.person.return_vehicle)
        self.assertEqual(queued.status, "queued")

class TestPositionUpdates(unittest.TestCase):
    def setUp(self):
        from datetime import datetime
        self.start = datetime(2026, 11, 23, 12)
        self.person = Obj(lat=0, lon=0, location_source="gps", location_at=self.start,
                          seen_at=self.start, has_vehicle=False, vehicle=None, speed_kmh=None)

    def update(self, meters, seconds, **options):
        from datetime import timedelta
        return service.record_position(self.person, 0, math.degrees(meters / EARTH_RADIUS_M),
                                       "gps", now=self.start + timedelta(seconds=seconds), **options)

    def test_walking_speed_from_successive_fixes(self):
        self.update(10, 10)
        self.assertAlmostEqual(self.person.speed_kmh, 3.6)

    def test_vehicle_speed_is_separate_from_walking_speed(self):
        self.person.has_vehicle = True
        self.person.vehicle = Obj(status="in_use", speed_kmh=None)
        self.update(100, 10)
        self.assertAlmostEqual(self.person.vehicle.speed_kmh, 36)
        self.assertIsNone(self.person.speed_kmh)

    def test_stationary_gps_noise_does_not_replace_speed(self):
        self.person.speed_kmh = 4
        self.update(.1, 10)
        self.assertEqual(self.person.speed_kmh, 4)

    def test_duplicate_fix_updates_presence_only(self):
        changed = self.update(1000, 15, fix_age_s=15)
        self.assertFalse(changed)
        self.assertEqual(self.person.lon, 0)
        self.assertEqual(self.person.location_at, self.start)
        self.assertGreater(self.person.seen_at, self.start)

    def test_out_of_order_fix_cannot_move_engineer_back(self):
        self.update(10, 10)
        position = self.person.lon
        self.assertFalse(self.update(0, 20, fix_age_s=15))
        self.assertEqual(self.person.lon, position)

    def test_gps_jump_does_not_replace_walking_speed(self):
        self.person.speed_kmh = 4
        self.update(500, 3)
        self.assertEqual(self.person.speed_kmh, 4)
