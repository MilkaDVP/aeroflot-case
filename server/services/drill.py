"""
Режим учений: сотрудники сами движутся по назначенному маршруту.

Зачем. На финале решение показывают в работе, а у тестовых сотрудников
нет телефонов: назначенный инженер стоял бы на карте неподвижно, и ни
живой прогноз прибытия, ни тревогу «не успевает», ни оценку Наставника
показать было бы нельзя. В режиме учений сервер продвигает таких
сотрудников по маршруту с их настоящей расчётной скоростью, а кнопка
«задержать» останавливает выбранного — так переход «успевает → впритык →
не успевает» разыгрывается на глазах у экспертов, в реальном времени.

Время не ускоряется намеренно: контрольный срок идёт по настоящим часам,
и ускоренное движение при настоящем сроке дало бы картину, в которой
успевают все.

Двигаются только сотрудники с ручной точкой. Того, чей телефон шлёт
координаты (GPS или имитация в приложении), сервер не трогает: его место
определяет он сам.
"""

from algorithm.geo import haversine_m
from clock import utc_now
from constants import (
    LOCATION_GPS,
    LOCATION_MANUAL,
    LOCATION_SIMULATION,
    MODE_WALK,
    STATUS_BUSY,
    STATUS_EN_ROUTE,
    VEHICLE_IN_USE,
    VEHICLE_RESERVED,
    VEHICLE_SPEED_KMH,
    WALK_SPEED_KMH,
)
from graph_registry import get_graph
from models.airport import Airport
from models.call import STATUS_ACCEPTED, STATUS_ARRIVED, STATUS_ASSIGNED, Call
from models.employee import Employee
from services import mentor
from services.assignment import mark_arrived, record_position

MOVING_STATUSES = (STATUS_ASSIGNED, STATUS_ACCEPTED)
# Источники координат, при которых место сотрудника задаёт его телефон.
PHONE_SOURCES = (LOCATION_GPS, LOCATION_SIMULATION)


def is_enabled(db, airport_icao):
    """Включён ли режим учений в аэропорту."""
    airport = db.get(Airport, airport_icao)
    return bool(airport and airport.drill_enabled)


def set_enabled(db, airport_icao, enabled):
    """
    Включает или выключает учения.

    При выключении все задержки снимаются: «задержанный» сотрудник вне
    учений — это состояние, из которого его никто уже не выведет.
    """
    airport = db.get(Airport, airport_icao)
    airport.drill_enabled = enabled
    if not enabled:
        held = db.query(Employee).filter(
            Employee.airport_icao == airport_icao, Employee.held.is_(True)
        )
        for employee in held:
            employee.held = False
    return airport


def enabled_airports(db):
    """Коды аэропортов, где идут учения."""
    return [
        airport.icao
        for airport in db.query(Airport).filter(Airport.drill_enabled.is_(True))
    ]


def step(db, airport_icao, dt_s, now=None):
    """
    Один такт учений: все едущие сотрудники аэропорта проходят dt_s секунд пути.

    Возвращает число сдвинутых сотрудников. Коммит — за вызывающим.
    """
    now = now or utc_now()
    calls = (
        db.query(Call)
        .filter(Call.airport_icao == airport_icao, Call.status.in_(MOVING_STATUSES))
        .all()
    )
    moved = 0
    for call in calls:
        if advance(db, call, dt_s, now):
            moved += 1
    return moved


def advance(db, call, dt_s, now):
    """Продвигает исполнителя одного вызова; False — если он не движется."""
    employee = call.assigned_employee
    if employee is None or employee.held or employee.location_source in PHONE_SOURCES:
        return False
    if not call.route_node_ids:
        return False

    graph = get_graph(call.airport_icao)
    marks = route_marks(graph, call.route_node_ids)
    if not marks:
        return False
    total = marks[-1][0]

    # Движение началось — значит, вызов принят: у сотрудника без телефона
    # подтвердить приём больше некому.
    if call.status == STATUS_ASSIGNED:
        call.status = STATUS_ACCEPTED
        employee.status = STATUS_EN_ROUTE

    progress = min(next_progress(call, employee, marks, dt_s), total)
    call.sim_progress_m = progress
    lat, lon = point_at(marks, progress)
    record_position(employee, lat, lon, LOCATION_MANUAL, now=now)

    if progress >= total:
        call.status = STATUS_ARRIVED
        employee.status = STATUS_BUSY
        mark_arrived(db, employee, call)
        mentor.review_arrival(db, call, employee, now)
    return True


def speed_mps(employee):
    """Скорость сотрудника сейчас: на машине, если она уже при нём, иначе пешком."""
    vehicle = employee.vehicle
    if vehicle is not None and vehicle.status == VEHICLE_IN_USE:
        return (vehicle.speed_kmh or VEHICLE_SPEED_KMH) / 3.6
    return (employee.speed_kmh or WALK_SPEED_KMH) / 3.6


def route_marks(graph, node_ids):
    """
    Маршрут как список отметок: (метров от начала, узел, широта, долгота).

    Длина участка берётся из ребра графа, а не по прямой между узлами:
    по этим же длинам считалось время прибытия при назначении, и учебное
    движение должно с ним сходиться.
    """
    marks = []
    travelled = 0.0
    previous = None
    for node_id in node_ids:
        node = graph.node(node_id)
        if node is None:
            continue
        if previous is not None:
            travelled += edge_length(graph, previous, node)
        marks.append((travelled, node["id"], node["lat"], node["lon"]))
        previous = node
    return marks


def edge_length(graph, start, end):
    """Длина ребра между соседними узлами маршрута; без ребра — по прямой."""
    for neighbour_id, distance in graph.neighbours(start["id"], MODE_WALK):
        if neighbour_id == end["id"]:
            return distance
    return haversine_m(start["lat"], start["lon"], end["lat"], end["lon"])


def point_at(marks, progress):
    """Координаты точки маршрута на расстоянии progress метров от начала."""
    for index in range(1, len(marks)):
        start, end = marks[index - 1], marks[index]
        if progress <= end[0]:
            span = end[0] - start[0]
            share = (progress - start[0]) / span if span > 0 else 1.0
            return (
                start[2] + (end[2] - start[2]) * share,
                start[3] + (end[3] - start[3]) * share,
            )
    return marks[-1][2], marks[-1][3]


def next_progress(call, employee, marks, dt_s):
    """
    Где сотрудник окажется через dt_s секунд.

    Если на этом такте он доходит до ждущей машины, остаток такта он уже
    едет: посадка привязана к узлу маршрута, а не к близости координат —
    узел графа и точка стоянки машины могут разойтись на десятки метров.
    Без деления такта пеший шаг «проскакивал» бы машину пешей скоростью.
    """
    progress = call.sim_progress_m or 0.0
    vehicle = employee.vehicle
    pickup_at = pickup_distance(call, marks)

    if vehicle is not None and vehicle.status == VEHICLE_RESERVED and pickup_at is not None:
        walk = speed_mps(employee)
        seconds_to_vehicle = max(pickup_at - progress, 0.0) / walk
        if seconds_to_vehicle > dt_s:
            return progress + walk * dt_s
        vehicle.status = VEHICLE_IN_USE
        return pickup_at + speed_mps(employee) * (dt_s - seconds_to_vehicle)

    return progress + speed_mps(employee) * dt_s


def pickup_distance(call, marks):
    """Сколько метров от начала маршрута до узла, где ждёт машина."""
    if not call.pickup_node_id:
        return None
    for distance, node_id, _lat, _lon in marks:
        if node_id == call.pickup_node_id:
            return distance
    return None
