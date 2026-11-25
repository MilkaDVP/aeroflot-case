"""Прибытие к борту в поле: точность GPS, кнопка «Я у борта», журнал."""

from datetime import timedelta

from clock import utc_now
from database import SessionLocal
from graph_registry import get_graph
from models.aircraft import Aircraft
from models.call import Call
from models.employee import Employee
from models.event_log import EventLog
from models.user import User
from services.assignment import check_aircraft_arrival
from tests.test_api import ApiTestCase, auth_header


class TestArrival(ApiTestCase):
    def setUp(self):
        self.reset_database()
        with SessionLocal() as db:
            board = db.query(Aircraft).filter_by(stand_node_id="stand_1", airport_icao="VDNH").one()
            engineer = db.query(Employee).filter_by(airport_icao="VDNH").order_by(Employee.id).first()
            engineer.status = "en_route"
            call = Call(airport_icao="VDNH", aircraft_id=board.id, stand_node_id=board.stand_node_id,
                        defect_code="hydraulic_leak", required_mark="B1.1", status="accepted",
                        assigned_employee_id=engineer.id)
            db.add(call)
            db.commit()
            self.call_id, self.engineer_id = call.id, engineer.id
        self.stand = get_graph("VDNH").node("stand_1")
        with SessionLocal() as db:
            login = db.query(User).filter_by(employee_id=self.engineer_id).one().login
        self.token = self.login(login, "engineer", airport="VDNH")

    def stay(self, accuracy, seconds, d_lat=0.0):
        """Инженер стоит у борта: точка каждые 5 секунд."""
        with SessionLocal() as db:
            engineer = db.get(Employee, self.engineer_id)
            engineer.location_source = "gps"
            engineer.lat, engineer.lon = self.stand["lat"] + d_lat, self.stand["lon"]
            end = utc_now()
            for back in range(seconds, -1, -5):
                engineer.location_at = end - timedelta(seconds=back)
                check_aircraft_arrival(db, engineer, accuracy)
            db.commit()
            return db.get(Call, self.call_id).status

    def arrive(self, **fields):
        return self.client.post("/api/me/status", json={"action": "arrived", "call_id": self.call_id, **fields},
                                headers=auth_header(self.token))

    def test_хорошая_точность_десять_секунд(self):
        self.assertEqual(self.stay(8, 5), "accepted")

    def test_хорошая_точность_засчитана(self):
        self.assertEqual(self.stay(8, 10), "arrived")

    def test_посредственной_мало_десяти_секунд(self):
        self.assertEqual(self.stay(35, 15), "accepted")

    def test_посредственная_за_двадцать_секунд(self):
        self.assertEqual(self.stay(35, 20), "arrived")

    def test_плохая_сама_не_засчитывается(self):
        self.assertEqual(self.stay(80, 25), "accepted")

    def test_соседняя_стоянка_не_засчитывается(self):
        other = get_graph("VDNH").node("stand_2")
        self.stand = other
        for accuracy in (8, 35):
            self.assertEqual(self.stay(accuracy, 25), "accepted")

    def test_кнопка_без_gps(self):
        self.assertEqual(self.arrive(arrival="manual").status_code, 200)
        with SessionLocal() as db:
            event = db.query(EventLog).filter_by(type="arrival", call_id=self.call_id).one()
            self.assertIn("отмечено инженером", event.text)
            self.assertEqual(event.params["отмечено"], "кнопкой")

    def test_кнопка_издалека_отклоняется(self):
        response = self.arrive(lat=self.stand["lat"] + 0.003, lon=self.stand["lon"], accuracy_m=10)
        self.assertEqual(response.status_code, 409)
        # Погрешность в пользу инженера.
        response = self.arrive(lat=self.stand["lat"] + 0.003, lon=self.stand["lon"], accuracy_m=250)
        self.assertEqual(response.status_code, 200, response.text)

    def test_свежий_gps_важнее_отсутствия_координат(self):
        with SessionLocal() as db:
            engineer = db.get(Employee, self.engineer_id)
            engineer.location_source, engineer.location_at = "gps", utc_now()
            engineer.lat, engineer.lon = self.stand["lat"] + 0.004, self.stand["lon"]
            db.commit()
        self.assertEqual(self.arrive().status_code, 409)

    def test_автоматическое_в_журнале(self):
        self.arrive(arrival="auto", lat=self.stand["lat"], lon=self.stand["lon"], accuracy_m=6)
        with SessionLocal() as db:
            event = db.query(EventLog).filter_by(type="arrival", call_id=self.call_id).one()
            self.assertIn("автоматически", event.text)
            self.assertEqual(event.params["точность_gps_м"], 6)
