"""
Исходная расстановка испытательного полигона — из геоданных организаторов.

Слои лежат в data/geodata/vdnh: борта, инженеры, спецтехника. Расстановка
применяется при первом наполнении базы и кнопкой «Сбросить полигон»
в администрировании — перед испытанием.

Инженеры и машины не создаются заново, а возвращаются к исходным данным
по порядку: учётные записи остаются при своих карточках, а номера e-01,
v-01 приёма координат не сдвигаются.
"""

import json

from algorithm.geo import haversine_m
from clock import utc_now
from constants import AIRPORTS_DIR, LOCATION_MANUAL, STATUS_FREE, VEHICLE_FREE
from graph_registry import get_graph
from models.aircraft import Aircraft
from models.call import ACTIVE_CALL_STATUSES, STATUS_CANCELLED, Call
from models.employee import Employee
from models.report import REPORT_NEW, REPORT_REJECTED, Report
from models.user import AuthSession, User
from models.vehicle import Vehicle
from services import drill, tracking

ICAO = "VDNH"
LAYERS = AIRPORTS_DIR.parent / "geodata" / "vdnh"
VALID_UNTIL = "2027-12-31"
RESET_NOTE = "Полигон возвращён к исходной расстановке"

AIRCRAFT_TYPES = {"Airbus A320": "A320"}
# Категории из слоя инженеров → отметки системы. AM и CAB самостоятельного
# допуска к работам не дают: показываются, но подбор их не предлагает.
MARKS = {"A": "A1", "B1": "B1.1", "B2": "B2"}


def layer(name):
    with (LAYERS / f"{name}.geojson").open(encoding="utf-8") as handle:
        features = json.load(handle)["features"]
    return [
        (item["properties"], item["geometry"]["coordinates"][1], item["geometry"]["coordinates"][0])
        for item in features
    ]


def reset(db, shift):
    """Вернуть людей, машины и борта полигона к слоям. Коммит — за вызывающим."""
    cancel_calls(db)
    reset_vehicles(db)
    reset_engineers(db, shift)
    reset_aircraft(db)
    db.flush()
    # Иначе первая точка после сброса сравнивалась бы со старым местом.
    tracking.forget_points()
    drill.forget_trips()
    return {
        "engineers": len(layer("engineers_positions")),
        "vehicles": len(layer("service_vehicles_positions")),
        "aircraft": len(layer("aircrafts_positions")),
    }


def cancel_calls(db):
    """Незавершённое отменяется и остаётся в истории; люди и машины свободны."""
    now = utc_now()
    for call in db.query(Call).filter(Call.airport_icao == ICAO, Call.status.in_(ACTIVE_CALL_STATUSES)):
        call.status = STATUS_CANCELLED
        call.closed_at = now
        call.claimed_by_user_id = call.claimed_at = None
    for report in db.query(Report).filter_by(airport_icao=ICAO, status=REPORT_NEW):
        report.status = REPORT_REJECTED
        report.resolution_note = RESET_NOTE
        report.processed_at = now
    db.flush()


def reset_engineers(db, shift):
    cards = db.query(Employee).filter_by(airport_icao=ICAO).order_by(Employee.id).all()
    rows = layer("engineers_positions")
    now = utc_now()
    for index, (props, lat, lon) in enumerate(rows):
        if index < len(cards):
            card = cards[index]
        else:
            card = Employee(airport_icao=ICAO)
            db.add(card)
        card.full_name = f"{props['tabel_id']} · {props['grade']}"
        card.qualifications = [
            {"category": MARKS.get(skill, skill), "aircraft_types": list(AIRCRAFT_TYPES.values()),
             "valid_until": VALID_UNTIL}
            for skill in props["skill"].split(",")
        ]
        card.shift, card.status, card.archived = shift, STATUS_FREE, False
        card.lat, card.lon = lat, lon
        card.waiting_lat, card.waiting_lon = lat, lon
        card.location_source, card.location_at, card.seen_at = LOCATION_MANUAL, now, None
        card.return_to_waiting = card.return_vehicle = False
        # Скорость, выученная на прежних перемещениях, — тоже не исходная.
        card.speed_kmh = card.busy_until = card.shift_ends_at = None
        for user in db.query(User).filter_by(employee_id=card.id) if card.id else ():
            user.full_name = card.full_name
    for card in cards[len(rows):]:
        card.archived = True
        accounts = db.query(User.id).filter(User.employee_id == card.id)
        db.query(AuthSession).filter(AuthSession.user_id.in_(accounts)).delete(synchronize_session=False)
    db.flush()


def reset_vehicles(db):
    parked = db.query(Vehicle).filter_by(airport_icao=ICAO).order_by(Vehicle.id).all()
    rows = layer("service_vehicles_positions")
    for index, (props, lat, lon) in enumerate(rows):
        if index < len(parked):
            vehicle = parked[index]
        else:
            vehicle = Vehicle(airport_icao=ICAO)
            db.add(vehicle)
        vehicle.call_sign, vehicle.kind = props["object_id"], props["model"]
        vehicle.lat, vehicle.lon = lat, lon
        vehicle.status, vehicle.employee, vehicle.archived = VEHICLE_FREE, None, False
        vehicle.speed_kmh = None
    for vehicle in parked[len(rows):]:
        vehicle.status, vehicle.employee, vehicle.archived = VEHICLE_FREE, None, True
    db.flush()


def reset_aircraft(db):
    graph = get_graph(ICAO)
    boards = {item.board_number: item for item in db.query(Aircraft).filter_by(airport_icao=ICAO)}
    for props, lat, lon in layer("aircrafts_positions"):
        number = props["object_id"]
        aircraft = boards.pop(number, None)
        if aircraft is None:
            aircraft = Aircraft(board_number=number, airport_icao=ICAO)
            db.add(aircraft)
        aircraft.aircraft_type = AIRCRAFT_TYPES.get(props["name"], props["name"])
        # Вершина стоянки стоит ровно в точке борта из этого же слоя.
        stand = min(graph.nodes_of_type("stand"), key=lambda n: haversine_m(lat, lon, n["lat"], n["lon"]))
        aircraft.stand_node_id = stand["id"]
        aircraft.lat = aircraft.lon = aircraft.scheduled_arrival_at = None
    for extra in boards.values():
        # Как кнопка удаления борта: завершённые вызовы остаются в истории.
        db.delete(extra)
    db.flush()
