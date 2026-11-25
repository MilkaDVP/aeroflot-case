"""
Учения: инженеры, которые не зашли в приложение, действуют сами.

Учётные записи, вызовы, машины и маршруты — обычные. Назначенный инженер
сам идёт и едет по своему маршруту с расчётной скоростью
(забирает машину, проходит точки и склад), отмечает прибытие, работает
и закрывает вызов. Зашёл в приложение — дальше управляет сам, сервер его
больше не двигает; вышел — снова движется сам.

Состояние учений живёт в памяти: после перезапуска сервера учения
выключены, и по ошибке остаться включёнными они не могут.
"""

import time

from algorithm.geo import haversine_m
from algorithm.routing import agent_speed, edge_seconds
from clock import utc_now
from constants import LOCATION_GPS, LOCATION_MANUAL, MODE_VEHICLE, STATUS_BUSY, STATUS_EN_ROUTE
from graph_registry import get_graph
from models.call import STATUS_ACCEPTED, STATUS_ARRIVED, STATUS_ASSIGNED, STATUS_CLOSED, Call
from models.employee import Employee
from services import mentor, support
from services.assignment import (
    advance_after_close,
    assigned_route,
    mark_arrived,
    navigation_leg,
    note_arrival,
    record_position,
    refresh_navigation,
    remaining_waypoints,
    return_destination,
    update_assigned_route,
    working_call_of,
)

TICK_S = 1
SPEEDS = (1, 2, 4, 8, 16, 32)
# Сколько секунд после обращения считать приложение инженера активным.
APP_ACTIVE_S = 120
ACCEPT_S = 5
REPAIR_S = 20
WORK_S = 60
WORK_MEL = "C"

# аэропорт → ускорение
_active = {}
# инженер → когда приложение последний раз обращалось к серверу
_seen = {}
# инженер → его путь и таймеры
_trips = {}


def state(icao):
    return {"enabled": icao in _active, "speed": _active.get(icao, 1)}


def set_state(icao, enabled, speed=None):
    if enabled:
        _active[icao] = speed or _active.get(icao, 1)
    else:
        _active.pop(icao, None)
        # Где остановились, там и стоят: остаток пути никому не нужен.
        _trips.clear()
    return state(icao)


def stop_all():
    """База очищена — учебного движения больше нет."""
    _active.clear()
    _trips.clear()


def forget_trips():
    """Людей расставили заново — прежние пути не действуют."""
    _trips.clear()


def note_app(employee_id):
    _seen[employee_id] = time.monotonic()


def forget_app(employee_id):
    _seen.pop(employee_id, None)


def in_app(employee, now):
    """Инженер сейчас в приложении или его место передаёт трекер."""
    seen = _seen.get(employee.id)
    if seen is not None and time.monotonic() - seen < APP_ACTIVE_S:
        return True
    return bool(employee.location_source == LOCATION_GPS and employee.seen_at
                and (now - employee.seen_at).total_seconds() < APP_ACTIVE_S)


def tick(db, dt_s):
    """Один такт по всем аэропортам с учениями. Коммит — за вызывающим."""
    now = utc_now()
    for icao, speed in list(_active.items()):
        people = db.query(Employee).filter_by(airport_icao=icao, archived=False).all()
        for person in people:
            if person.lat is None or person.lon is None:
                continue
            if in_app(person, now):
                _trips.pop(person.id, None)
                continue
            step(db, person, dt_s * speed, now)


def step(db, person, seconds, now):
    call = working_call_of(db, person)
    if call is None:
        if return_destination(person):
            move(db, person, None, seconds, now)
        else:
            _trips.pop(person.id, None)
        return
    trip = _trips.get(person.id)
    if trip is None or trip["call"] != call.id:
        trip = _trips[person.id] = {"call": call.id, "spent": 0.0, "status": call.status, "key": None, "path": []}
    if trip["status"] != call.status:
        trip["status"], trip["spent"] = call.status, 0.0
    trip["spent"] += seconds

    if call.status == STATUS_ASSIGNED:
        if trip["spent"] >= ACCEPT_S:
            call.status = STATUS_ACCEPTED
            person.status = STATUS_EN_ROUTE
    elif call.status == STATUS_ACCEPTED:
        move(db, person, call, seconds, now)
    elif call.status == STATUS_ARRIVED:
        work(db, person, call, trip["spent"], now)


def work(db, person, call, spent, now):
    """У борта: осмотр, ремонт, закрытие — как это сделал бы инженер."""
    if spent >= REPAIR_S and call.work_stage == "inspection":
        call.work_stage = "repair"
    # Ожидание запчастей и открытые вопросы решает человек.
    if spent < WORK_S or call.work_stage == "waiting_parts":
        return
    call.mel_category = call.mel_category or WORK_MEL
    call.status = STATUS_CLOSED
    call.closed_at = now
    _trips.pop(person.id, None)
    advance_after_close(db, person, call, was_working=True)
    if call.parent_call_id is None:
        support.close_support(db, call, now)


# --- Движение ------------------------------------------------------------------


def move(db, person, call, seconds, now):
    """
    Проходит seconds секунд пути. Путь — список (широта, долгота, секунды,
    остановка): на остановке такт кончается, чтобы сервер увидел инженера
    ровно в точке и отметил её пройденной.
    """
    trip = _trips.setdefault(person.id, {"call": None, "spent": 0.0, "status": None, "key": None, "path": []})
    key = path_key(person, call)
    # Инженера переставили на карте — прежний путь вёл бы напрямик.
    if trip["key"] != key or trip.get("at", (person.lat, person.lon)) != (person.lat, person.lon):
        # Стоянку в только что достигнутой точке не теряем.
        wait = [item for item in trip["path"][:1] if item[:2] == (person.lat, person.lon)]
        path = build_path(person, call)
        if path is None:
            return
        trip["path"] = wait + path
        # Построение могло уточнить маршрут вызова.
        trip["key"] = path_key(person, call)

    path = trip["path"]
    lat, lon = person.lat, person.lon
    while path and seconds > 0:
        end_lat, end_lon, left, stop = path[0]
        if left > seconds:
            share = seconds / left
            lat += (end_lat - lat) * share
            lon += (end_lon - lon) * share
            path[0] = (end_lat, end_lon, left - seconds, stop)
            break
        seconds -= left
        lat, lon = end_lat, end_lon
        path.pop(0)
        if stop:
            break

    if (lat, lon) != (person.lat, person.lon):
        record_position(person, lat, lon, LOCATION_MANUAL, now=now, learn_speed=False)
        refresh_navigation(db, person)
    trip["at"] = (person.lat, person.lon)
    if call is not None and not path and path_key(person, call) == trip["key"]:
        arrive(db, person, call, now)


def path_key(person, call):
    """Что определяет путь: сменилось — путь строится заново."""
    vehicle = person.vehicle
    if call is None:
        return ("return", vehicle.id if vehicle else None)
    return (
        call.id, call.stand_node_id, tuple(call.route_node_ids or ()), call.vehicle_id,
        tuple(remaining_waypoints(call)), person.return_vehicle,
    )


def build_path(person, call):
    graph = get_graph(person.airport_icao)
    if call is None:
        leg = navigation_leg(person)
        points = leg["route_points"][1:] if leg else []
        return straight(person.lat, person.lon, points, agent_speed(MODE_VEHICLE))

    plan = assigned_route(call, graph)
    if plan is None or not plan.node_ids:
        return None
    if plan.node_ids != call.route_node_ids:
        update_assigned_route(call, plan, reset_eta=False)

    path = []
    lat, lon = person.lat, person.lon
    stops = list(plan.stops)
    previous = None
    for leg in plan.legs:
        speed = agent_speed(leg.mode, leg.speed_kmh)
        for node_id in leg.node_ids:
            node = graph.node(node_id)
            if node_id == previous:
                continue
            length, limit = link(graph, previous, node_id, leg.mode)
            if length is None:
                length = haversine_m(lat, lon, node["lat"], node["lon"])
            # По изгибам дороги; время ребра делится по длине отрезков.
            line = [*(graph.bends(previous, node_id) if previous else []), (node["lat"], node["lon"])]
            spans = []
            for point in line:
                spans.append(haversine_m(lat, lon, *point))
                lat, lon = point
            total = sum(spans) or 1
            seconds = edge_seconds(length, limit, speed)
            path.extend((*point, seconds * span / total, False) for point, span in zip(line, spans))
            lat, lon, previous = node["lat"], node["lon"], node_id
        dwell, here = 0, False
        while stops and stops[0]["node_id"] == previous:
            dwell += stops.pop(0)["dwell_s"]
            here = True
        if here:
            if path:
                path[-1] = (*path[-1][:3], True)
            if dwell:
                path.append((lat, lon, dwell, False))
    return path


def link(graph, start_id, end_id, mode):
    """Длина и ограничение скорости ребра; (None, None) — ребра нет."""
    if start_id is not None:
        for neighbour, length, limit in graph.links(start_id, mode):
            if neighbour == end_id:
                return length, limit
    return None, None


def straight(lat, lon, points, speed):
    path = []
    for point in points:
        length = haversine_m(lat, lon, point["lat"], point["lon"])
        path.append((point["lat"], point["lon"], edge_seconds(length, None, speed), False))
        lat, lon = point["lat"], point["lon"]
    return path


def arrive(db, person, call, now):
    if person.return_vehicle or remaining_waypoints(call):
        # До конца ещё не дошёл — путь построится заново.
        _trips[person.id]["key"] = None
        return
    note_arrival(call, "auto", person.lat, person.lon)
    call.status = STATUS_ARRIVED
    person.status = STATUS_BUSY
    if mark_arrived(db, person, call, now):
        mentor.review_arrival(db, call, person, now)


def stats(db, icao):
    """Сколько инженеров сейчас двигает сервер, а сколько — в приложении."""
    now = utc_now()
    people = db.query(Employee).filter_by(airport_icao=icao, archived=False).all()
    on_call = {c.assigned_employee_id for c in db.query(Call).filter(
        Call.airport_icao == icao, Call.status.in_((STATUS_ASSIGNED, STATUS_ACCEPTED, STATUS_ARRIVED)))}
    return {"auto": sum(1 for p in people if p.id in on_call and not in_app(p, now)),
            "in_app": sum(1 for p in people if in_app(p, now))}
