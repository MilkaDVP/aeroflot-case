"""Подход к машине и борту: вплотную, с запасом только на погрешность GPS."""

import unittest
from types import SimpleNamespace as Obj

from services.assignment import approach_radius, record_position

M = 1 / 111195


class TestApproach(unittest.TestCase):
    def near(self, metres, source, accuracy=None):
        car = Obj(status="reserved", lat=55.0, lon=37.0, speed_kmh=None)
        person = Obj(id=1, lat=55.1, lon=37.0, location_source="manual", location_at=None, seen_at=None,
                     has_vehicle=False, vehicle=car, speed_kmh=None)
        record_position(person, 55.0 + metres * M, 37.0, source, accuracy_m=accuracy)
        return car.status == "in_use"

    def test_машина(self):
        for metres, source, accuracy, taken in (
                (2, "manual", None, True), (5, "manual", None, False), (5, "simulation", None, False),
                (20, "gps", 8, True), (25, "gps", 8, False), (29, "gps", 60, True), (32, "gps", 60, False),
                (19, "gps", None, True), (22, "gps", None, False)):
            self.assertEqual(self.near(metres, source, accuracy), taken, (metres, source, accuracy))

    def test_радиус(self):
        self.assertEqual(approach_radius(Obj(location_source="manual")), 3)
        self.assertEqual(approach_radius(Obj(location_source="gps"), 5), 20)
