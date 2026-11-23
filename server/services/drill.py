"""
Режим учений: сотрудники сами движутся по назначенному маршруту.

Зачем. На финале решение показывают в работе, а у тестовых сотрудников
нет телефонов: назначенный инженер стоял бы на карте неподвижно, и ни
живой прогноз прибытия, ни тревогу «не успевает», ни оценку Наставника
показать было бы нельзя. В режиме учений сервер продвигает таких
сотрудников по маршруту с их настоящей расчётной скоростью, а кнопка
«задержать» останавливает выбранного — так переход «успевает → впритык →
не успевает» разыгрывается на глазах у экспертов, в реальном времени.

Множитель аэропорта ускоряет движение и выполнение работ учебной команды.

Двигаются только учебные инженеры без учётных записей.
"""

from algorithm.geo import haversine_m
from algorithm.transport import plan_for
from clock import utc_now
from constants import (
    AIRCRAFT_GROUP_BY_TYPE,
    SHIFT_DAY,
    SHIFT_NIGHT,
    STATUS_FREE,
    LOCATION_MANUAL,
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
from models.employee import TrainingEngineer
from models.shift_event import ShiftEvent
from models.engineer_action import EngineerAction
from services.shifts import end_shift
from services import mentor, support
from services.assignment import advance_after_close, navigation_leg, refresh_navigation, return_destination, assigned_route, mark_arrived, record_position, release_vehicle, working_call_of

MOVING_STATUSES = (STATUS_ASSIGNED, STATUS_ACCEPTED)


def ensure_training_team(db, icao):
    if db.query(TrainingEngineer.id).filter_by(airport_icao=icao, archived=False).first():
        return
    graph = get_graph(icao)
    bases = graph.nodes_of_type("tech_center") or graph.nodes_of_type("stand")
    for shift in (SHIFT_DAY, SHIFT_NIGHT):
        for index, mark in enumerate(("B1.1", "B2", "A1")):
            node = bases[index % len(bases)]
            db.add(TrainingEngineer(
                full_name=f"Учебный инженер {index + 1} · {mark}", is_training=True,
                airport_icao=icao, shift=shift, status=STATUS_FREE,
                lat=node["lat"], lon=node["lon"], location_source=LOCATION_MANUAL,
                location_at=utc_now(), qualifications=[{
                    "category": mark, "aircraft_types": list(AIRCRAFT_GROUP_BY_TYPE),
                    "valid_until": "9999-12-31",
                }],
            ))


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
    if enabled:
        ensure_training_team(db, airport_icao)
    else:
        team = db.query(TrainingEngineer).filter_by(airport_icao=airport_icao, archived=False).all()
        for employee in team:
            end_shift(db, employee, "Учения завершены")
            employee.held = False
            db.flush()
            key = employee.id
            referenced = (db.query(Call.id).filter((Call.assigned_employee_id == key)
                          | (Call.previous_employee_id == key) | (Call.suggested_employee_id == key)).first()
                          or db.query(ShiftEvent.id).filter_by(employee_id=key).first()
                          or db.query(EngineerAction.employee_id).filter_by(employee_id=key).first())
            if referenced:
                employee.archived = True
            else:
                db.delete(employee)
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
    airport = db.get(Airport, airport_icao)
    if not airport or not airport.drill_enabled:
        return 0
    dt_s *= airport.drill_speed
    team = db.query(TrainingEngineer).filter_by(airport_icao=airport_icao, archived=False).all()
    moved = 0
    for person in team:
        if person.held:
            continue
        if return_destination(person):
            walk_return_leg(db, person, dt_s, now)
            continue
        call = working_call_of(db, person)
        if not call:
            continue
        if call.status == STATUS_ARRIVED:
            finish_work(db, call, dt_s, now)
        elif call.status in MOVING_STATUSES and advance(db, call, dt_s, now):
            moved += 1
    return moved


def finish_work(db, call, seconds, now):
    call.drill_work_s = (call.drill_work_s or 0) + seconds
    if call.drill_work_s >= 10:
        call.status = "closed"
        call.closed_at = now
        advance_after_close(db, call.assigned_employee, call, True)
        if call.parent_call_id is None:
            support.close_support(db, call, now)


def walk_return_leg(db, person, seconds, now):
    target = return_destination(person)
    if person.drill_return_path is None:
        leg = navigation_leg(person)
        if not leg or not leg["route_points"]:
            return
        points = [[p["lat"], p["lon"]] for p in leg["route_points"][1:]]
    else:
        points = list(person.drill_return_path)
    distance = seconds * speed_mps(person)
    lat, lon = person.lat, person.lon
    while points:
        end_lat, end_lon = points[0]
        span = haversine_m(lat, lon, end_lat, end_lon)
        if span > distance:
            lat += (end_lat - lat) * distance / span
            lon += (end_lon - lon) * distance / span
            break
        lat, lon = points.pop(0)
        distance -= span
    record_position(person, lat, lon, LOCATION_MANUAL, now=now)
    refresh_navigation(db, person)
    person.drill_return_path = points if points and target == return_destination(person) else None


def reroute_after_move(db, employee):
    """Перестроить остаток пути, сохранив транспорт и контрольный срок."""
    call = working_call_of(db, employee)
    if call is None or call.status not in MOVING_STATUSES:
        return True
    graph = get_graph(employee.airport_icao)
    vehicle = employee.vehicle
    if vehicle and vehicle.status == VEHICLE_RESERVED and not any(
        item.vehicle_id == vehicle.id and item.status in MOVING_STATUSES
        for item in support.support_calls(call)
    ):
        # После переноса пешком до борта может быть быстрее, чем через машину.
        plan = plan_for(graph, employee.as_algorithm_dict(), call.stand_node_id,
                        [vehicle.as_algorithm_dict()])
        if plan and not plan.pickup:
            release_vehicle(employee)
            call.vehicle = None
    else:
        plan = assigned_route(call, graph)
    if plan is None or not plan.node_ids:
        return False
    nodes = plan.node_ids
    entry = graph.node(nodes[0])
    if len(nodes) > 1 and nodes[0] != plan.pickup_node_id:
        ahead = graph.node(nodes[1])
        to_entry = haversine_m(employee.lat, employee.lon, entry["lat"], entry["lon"])
        to_ahead = haversine_m(employee.lat, employee.lon, ahead["lat"], ahead["lon"])
        span = haversine_m(entry["lat"], entry["lon"], ahead["lat"], ahead["lon"])
        # Точка уже лежит на первом ребре. Продолжаем к дальнему узлу.
        if to_entry < span and to_ahead < span and to_entry + to_ahead <= span + 1:
            nodes = nodes[1:]
            entry = ahead
    call.route_node_ids = nodes
    call.pickup_node_id = plan.pickup_node_id
    # Ноль — первый узел графа. До него остаётся пройти от точки клика.
    call.sim_progress_m = -haversine_m(employee.lat, employee.lon, entry["lat"], entry["lon"])
    return True


def advance(db, call, dt_s, now):
    """Продвигает исполнителя одного вызова; False — если он не движется."""
    employee = call.assigned_employee
    if employee is None or employee.held or not employee.is_training:
        return False
    if not call.route_node_ids and not reroute_after_move(db, employee):
        return False
    if not call.route_node_ids:
        return False

    graph = get_graph(call.airport_icao)
    marks = route_marks(graph, call.route_node_ids)
    if not marks:
        return False
    if call.sim_progress_m is None:
        call.sim_progress_m = -haversine_m(employee.lat, employee.lon, marks[0][2], marks[0][3])
    if (call.sim_progress_m or 0) < 0:
        marks.insert(0, (call.sim_progress_m, None, employee.lat, employee.lon))
    total = marks[-1][0]

    # Движение началось — значит, вызов принят: у сотрудника без телефона
    # подтвердить приём больше некому.
    if call.status == STATUS_ASSIGNED:
        call.status = STATUS_ACCEPTED
        employee.status = STATUS_EN_ROUTE

    start = call.sim_progress_m or 0.0
    pickup = pickup_distance(call, marks)
    car = employee.vehicle or call.vehicle
    if pickup is not None and car and not employee.has_vehicle:
        walk = (employee.speed_kmh or WALK_SPEED_KMH) / 3.6
        drive = (car.speed_kmh or VEHICLE_SPEED_KMH) / 3.6
        travel_s = max(0, pickup - start) / walk + max(0, total - max(start, pickup)) / drive
    else:
        travel_s = max(0, total - start) / speed_mps(employee)
    progress = min(next_progress(call, employee, marks, dt_s), total)
    call.sim_progress_m = progress
    lat, lon = point_at(marks, progress)
    record_position(employee, lat, lon, LOCATION_MANUAL, now=now)

    if progress >= total:
        call.status = STATUS_ARRIVED
        employee.status = STATUS_BUSY
        if mark_arrived(db, employee, call):
            mentor.review_arrival(db, call, employee, now)
        finish_work(db, call, max(0, dt_s - travel_s), now)
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

    if vehicle is None and call.vehicle is not None and pickup_at is not None:
        # Второй инженер в машине ответственного: идёт до неё пешком,
        # дальше едет с её скоростью. Статус машины ведёт ответственный.
        return passenger_progress(call, employee, progress, pickup_at, dt_s)

    if vehicle is not None and vehicle.status == VEHICLE_RESERVED and pickup_at is not None:
        walk = speed_mps(employee)
        seconds_to_vehicle = max(pickup_at - progress, 0.0) / walk
        if seconds_to_vehicle > dt_s:
            return progress + walk * dt_s
        vehicle.status = VEHICLE_IN_USE
        return pickup_at + speed_mps(employee) * (dt_s - seconds_to_vehicle)

    return progress + speed_mps(employee) * dt_s


def passenger_progress(call, employee, progress, pickup_at, dt_s):
    """Продвижение пассажира: пешком до точки посадки, затем со скоростью машины."""
    walk = (employee.speed_kmh or WALK_SPEED_KMH) / 3.6
    drive = (call.vehicle.speed_kmh or VEHICLE_SPEED_KMH) / 3.6
    seconds_to_vehicle = max(pickup_at - progress, 0.0) / walk
    if seconds_to_vehicle > dt_s:
        return progress + walk * dt_s
    return max(progress, pickup_at) + drive * (dt_s - seconds_to_vehicle)


def pickup_distance(call, marks):
    """Сколько метров от начала маршрута до узла, где ждёт машина."""
    if not call.pickup_node_id:
        return None
    for distance, node_id, _lat, _lon in marks:
        if node_id == call.pickup_node_id:
            return distance
    return None
