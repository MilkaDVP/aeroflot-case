"""Точки для карты: лёгкий запрос, частые координаты не теряются."""

from datetime import timedelta
from types import SimpleNamespace as Obj

from clock import utc_now
from services import assignment
from services.assignment import record_position
from tests.test_api import ApiTestCase, auth_header


class TestPositions(ApiTestCase):
    def test_запрос(self):
        self.reset_database()
        head = auth_header(self.login("dispatcher_vdnh", "dispatcher", airport="VDNH"))
        data = self.client.get("/api/positions", headers=head).json()
        self.assertEqual((len(data["employee"]), len(data["vehicle"]), len(data["aircraft"])), (7, 3, 10))
        me = auth_header(self.login("orlov", "engineer", airport="VDNH"))
        self.assertEqual(self.client.get("/api/positions", headers=me).status_code, 403)
        self.assertEqual(self.client.get("/api/me/position", headers=me).json()["lat"], data["employee"][0][1])

    def test_точки_дважды_в_секунду(self):
        assignment._speed_ref.clear()
        now = utc_now()
        person = Obj(id=1, lat=55.0, lon=37.0, location_source="gps", location_at=now, seen_at=now, has_vehicle=False,
                     vehicle=None, speed_kmh=None)
        for step in range(1, 11):  # 5 км/ч, точка каждые полсекунды
            lat = 55.0 + step * 0.5 * (5 / 3.6) / 111195
            self.assertTrue(record_position(person, lat, 37.0, "gps", 0, now + timedelta(seconds=step * 0.5)))
        self.assertAlmostEqual(person.speed_kmh, 5, delta=0.3)
