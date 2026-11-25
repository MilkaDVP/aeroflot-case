"""Приближение ВС: борт рулит к инженеру — предупреждение инженеру, диспетчеру и в журнал."""

import math
import unittest
from datetime import timedelta

from algorithm.graph import load_airport
from clock import utc_now
from database import SessionLocal
from models.aircraft import Aircraft
from models.employee import Employee
from models.event_log import EventLog
from models.user import User
from services import proximity
from services.tracking import format_time, reset_memory
from tests.test_api import ApiTestCase, auth_header
from tests.test_polygon import HAS_POLYGON


@unittest.skipUnless(HAS_POLYGON, "нет графа полигона")
class TestProximity(ApiTestCase):
    def setUp(self):
        self.reset_database()
        reset_memory()
        self.graph = load_airport("VDNH")
        # Прямая рулёжка подлиннее: борт едет по ней, инженер — впереди.
        edge = max((e for e in self.graph.edges if e["kind"] == "taxi" and not e["points"]), key=lambda e: e["distance_m"])
        self.a, self.b = self.graph.node(edge["from_id"]), self.graph.node(edge["to_id"])
        self.length = edge["distance_m"]
        self.head = auth_header(self.login("dispatcher_vdnh", "dispatcher", airport="VDNH"))

    def at(self, metres, side=0):
        k = metres / self.length
        lat = self.a["lat"] + (self.b["lat"] - self.a["lat"]) * k
        lon = self.a["lon"] + (self.b["lon"] - self.a["lon"]) * k
        dx, dy = proximity.offset(self.a["lat"], self.a["lon"], self.b["lat"], self.b["lon"])
        nx, ny = -dy / self.length, dx / self.length
        k = math.pi / 180 * proximity.EARTH_M
        return lat - ny * side / k, lon + nx * side / (k * math.cos(math.radians(lat)))

    def place(self, metres, side=0):
        """Один инженер у рулёжки, остальные не на смене."""
        with SessionLocal() as db:
            people = db.query(Employee).filter_by(airport_icao="VDNH").order_by(Employee.id).all()
            for person in people:
                person.status = "offline"
            people[0].status, people[0].location_source = "free", "manual"
            people[0].lat, people[0].lon = self.at(metres, side)
            db.commit()
            return people[0].id

    def drive(self, spots, spd=None):
        now = utc_now()
        for index, metres in enumerate(spots):
            lat, lon = self.at(metres)
            body = {"obj": "RA-73101", "lat": lat, "lon": lon, "acc": 3,
                    "t": format_time(now - timedelta(seconds=2 * (len(spots) - 1 - index)))}
            if spd is not None:
                body["spd"] = spd
            answer = self.client.post("/api/v1/track", json=body).json()
            self.assertTrue(answer["applied"], answer)
        with SessionLocal() as db:
            proximity.tick(db)
            db.commit()
        return self.client.get("/api/proximity", headers=self.head).json()

    def test_борт_едет_к_инженеру(self):
        person = self.place(self.length * 0.5 + 60)
        spots = [self.length * 0.5 - 80 + 12 * i for i in range(4)]
        found = self.drive(spots)
        self.assertEqual([w["employee_id"] for w in found], [person])
        self.assertEqual(found[0]["board_number"], "RA-73101")
        self.assertTrue(0 < found[0]["seconds"] <= 60)
        with SessionLocal() as db:
            login = db.query(User).filter_by(employee_id=person).first().login
            proximity.tick(db)
            db.commit()
            events = db.query(EventLog).filter_by(type="aircraft_proximity").all()
            self.assertEqual(len(events), 1)  # повторный такт второй записи не даёт
            self.assertEqual(events[0].board_number, "RA-73101")
        me = auth_header(self.login(login, "engineer", airport="VDNH"))
        self.assertEqual(len(self.client.get("/api/me/proximity", headers=me).json()), 1)

    def test_уезжает_стоит_или_далеко(self):
        self.place(self.length * 0.5 - 150)
        self.assertEqual(self.drive([self.length * 0.5 + 12 * i for i in range(4)]), [])  # уезжает
        reset_memory()
        self.place(self.length * 0.5 + 60)
        self.assertEqual(self.drive([self.length * 0.5] * 3, spd=0), [])  # стоит
        reset_memory()
        self.place(self.length * 0.5 + 60, side=120)
        self.assertEqual(self.drive([self.length * 0.5 - 80 + 12 * i for i in range(4)]), [])  # в стороне
