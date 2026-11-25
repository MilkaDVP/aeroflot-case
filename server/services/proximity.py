"""
Предупреждения о приближении ВС (п. 3.2 положения).

Борт, который движется, продолжается вперёд по рулёжным дорожкам и ВПП
(по всем ответвлениям, кроме разворота назад), вне их — по прямой.
Инженер продолжается по своему маршруту с нынешней скоростью; стоит —
остаётся на месте. Окажутся рядом в близкие моменты — предупреждение
инженеру, диспетчеру и в журнал.

Скорость и курс борта берутся из точек /api/v1/track: spd, если он
передан, иначе по двум последним точкам. Борт, переставленный
диспетчером на карте, движущимся не считается.

Прогноз, а не разрешение на движение: отсутствие предупреждения не
означает, что пересекать дорожку безопасно.
"""

import math
import time
from collections import defaultdict
from datetime import timedelta

from algorithm.routing import agent_speed
from clock import to_iso_utc, utc_now
from constants import LOCATION_GPS, MODE_VEHICLE, MODE_WALK, STATUS_OFFLINE
from graph_registry import get_graph
from models.aircraft import Aircraft
from models.call import STATUS_ACCEPTED, STATUS_ASSIGNED
from models.employee import Employee
from services import journal

TICK_S = 1
HORIZON_S = 60
STEP_S = 2
# Ближе этого — опасное сближение: половина размаха крыла узкофюзеляжного
# борта (18 м) и запас на точность GPS.
DANGER_M = 35
# Насколько могут разойтись моменты прохода одного места.
WINDOW_S = 14
# Меньше — борт стоит: это дрожание координат.
MIN_SPEED_MS = 1.5
# Точка борта старше — где он, неизвестно.
MOTION_FRESH_S = 15
ENGINEER_FRESH_S = 60
SNAP_M = 30
MAX_PATHS = 12
MAX_TURN_DEG = 110
URGENT_S = 20
# Пропал прогноз ненадолго — это то же сближение, а не новое.
HOLD_S = 6
EARTH_M = 6371008.8

_motion = {}
_active = {}
_networks = {}


# --- Движение бортов ---------------------------------------------------------


def note_aircraft(aircraft, point, previous, received):
    """Новая принятая точка борта; previous — прежняя (t, lat, lon, приём)."""
    old = _motion.get(aircraft.id)
    heading, speed = (old["heading"] if old else None), 0.0
    if previous is not None:
        moment, lat, lon, _ = previous
        dt = (point["t"] - moment).total_seconds()
        dx, dy = offset(lat, lon, point["lat"], point["lon"])
        moved = math.hypot(dx, dy)
        if dt > 0:
            speed = moved / dt
        # Курс — по заметному смещению: у стоящего борта он шумит.
        if moved >= 3:
            heading = (dx, dy)
    if point.get("spd") is not None:
        speed = point["spd"]
    _motion[aircraft.id] = {
        "icao": aircraft.airport_icao, "lat": point["lat"], "lon": point["lon"],
        "speed": speed, "heading": heading, "at": time.monotonic(),
    }


def forget():
    _motion.clear()
    _active.clear()


def busy():
    """Есть ли что считать."""
    now = time.monotonic()
    return bool(_active) or any(now - m["at"] <= MOTION_FRESH_S and m["speed"] >= MIN_SPEED_MS for m in _motion.values())


def offset(lat1, lon1, lat2, lon2):
    """Смещение в метрах: на восток и на юг — как у карты."""
    k = math.pi / 180 * EARTH_M
    return (lon2 - lon1) * k * math.cos(math.radians(lat1)), (lat1 - lat2) * k


def to_xy(graph, lat, lon):
    ref = graph.ref_point
    return offset(ref["lat"], ref["lon"], lat, lon)


def to_ll(graph, x, y):
    ref = graph.ref_point
    k = math.pi / 180 * EARTH_M
    return round(ref["lat"] - y / k, 7), round(ref["lon"] + x / (k * math.cos(math.radians(ref["lat"]))), 7)


# --- Путь борта ------------------------------------------------------------------


class Taxiways:
    """Дорожки для ВС: рулёжки и ВПП; в графах без вида дорог — все рёбра."""

    def __init__(self, graph):
        self.lines = []
        self.by_node = defaultdict(list)
        for edge in graph.edges:
            if edge.get("kind") not in (None, "taxi", "runway"):
                continue
            a, b = graph.node(edge["from_id"]), graph.node(edge["to_id"])
            if not a or not b:
                continue
            line = [(a["x"], a["y"]), *(tuple(p) for p in edge.get("points") or []), (b["x"], b["y"])]
            index = len(self.lines)
            self.lines.append((line, edge["from_id"], edge["to_id"]))
            self.by_node[edge["from_id"]].append((index, False))
            self.by_node[edge["to_id"]].append((index, True))

    def oriented(self, index, reverse):
        line, start, end = self.lines[index]
        return (line[::-1], start) if reverse else (line, end)

    def nearest(self, point, heading):
        """Ближайшая дорожка, вдоль которой борт едет: (ребро, обратно?, отрезок, точка)."""
        best = None
        for index, (line, _, _) in enumerate(self.lines):
            for seg in range(len(line) - 1):
                q, distance = closest(point, line[seg], line[seg + 1])
                if distance > SNAP_M or (best is not None and distance >= best[0]):
                    continue
                turn = angle(heading, sub(line[seg + 1], line[seg]))
                if 60 < turn < 120:
                    continue  # едет поперёк — не по этой дорожке
                best = (distance, index, turn >= 120, seg, q)
        return best


def sub(a, b):
    return a[0] - b[0], a[1] - b[1]


def angle(a, b):
    length = math.hypot(*a) * math.hypot(*b)
    if length == 0:
        return 0
    return math.degrees(math.acos(max(-1, min(1, (a[0] * b[0] + a[1] * b[1]) / length))))


def closest(p, a, b):
    dx, dy = sub(b, a)
    length2 = dx * dx + dy * dy
    t = 0 if length2 == 0 else max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    q = (a[0] + t * dx, a[1] + t * dy)
    return q, math.dist(p, q)


def network(graph):
    known = _networks.get(graph.icao)
    if known is None or known[0] is not graph:
        known = _networks[graph.icao] = (graph, Taxiways(graph))
    return known[1]


def aircraft_paths(graph, position, heading, reach):
    """Куда борт может приехать: ломаные длиной до reach метров."""
    net = network(graph)
    found = net.nearest(position, heading)
    if found is None:
        length = math.hypot(*heading)
        return [[position, (position[0] + heading[0] / length * reach, position[1] + heading[1] / length * reach)]]
    _, index, reverse, seg, q = found
    line, end = net.oriented(index, reverse)
    rest = line[len(line) - 1 - seg:] if reverse else line[seg + 1:]
    done, queue = [], [([position, q, *rest], end, index)]
    while queue:
        path, node, used = queue.pop()
        length = sum(math.dist(a, b) for a, b in zip(path, path[1:]))
        going = sub(path[-1], next((p for p in reversed(path[:-1]) if math.dist(p, path[-1]) > 0.5), path[0]))
        further = []
        if length < reach and len(done) + len(queue) < MAX_PATHS:
            for nxt, back in net.by_node[node]:
                if nxt == used:
                    continue
                line, end = net.oriented(nxt, back)
                if angle(going, sub(line[1], line[0])) <= MAX_TURN_DEG:
                    further.append((path + line[1:], end, nxt))
        if further:
            queue.extend(further)
        else:
            done.append(path)
    return done


def walk(path, speed):
    """Где объект будет каждые STEP_S секунд: [(t, (x, y))]; дошёл до конца — стоит."""
    result, t, seg, passed = [], 0, 0, 0.0
    while t <= HORIZON_S:
        target = speed * t
        while seg < len(path) - 1 and passed + math.dist(path[seg], path[seg + 1]) < target:
            passed += math.dist(path[seg], path[seg + 1])
            seg += 1
        if seg >= len(path) - 1:
            result.append((t, path[-1]))
        else:
            span = math.dist(path[seg], path[seg + 1]) or 1
            k = (target - passed) / span
            a, b = path[seg], path[seg + 1]
            result.append((t, (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)))
        t += STEP_S
    return result


# --- Путь инженера ------------------------------------------------------------------


def engineer_path(db, graph, person):
    """Ломаная впереди инженера и его скорость; стоит — одна точка."""
    from services.assignment import navigation_leg, route_geometry, working_call_of

    here = to_xy(graph, person.lat, person.lon)
    call = working_call_of(db, person)
    nodes = []
    if person.return_vehicle:
        leg = navigation_leg(person)
        nodes = leg["route_points"] if leg else []
    elif call is not None and call.status in (STATUS_ASSIGNED, STATUS_ACCEPTED):
        nodes = route_geometry(call, graph)
    line = [to_xy(graph, n["lat"], n["lon"]) for n in nodes]
    if len(line) < 2:
        return [here, *line], speed_of(person), call
    best = min(range(len(line) - 1), key=lambda i: closest(here, line[i], line[i + 1])[1])
    q, _ = closest(here, line[best], line[best + 1])
    return [here, q, *line[best + 1:]], speed_of(person), call


def speed_of(person):
    if person.has_vehicle:
        return agent_speed(MODE_VEHICLE, person.vehicle.speed_kmh) / 3.6
    return agent_speed(MODE_WALK, person.speed_kmh) / 3.6


def conflict(plane, man):
    """Первое опасное сближение: (секунд до него, расстояние, точка) или None."""
    here = man[0][1]
    best = None
    for t, p in plane:
        if best is not None and t >= best[0]:
            break
        # Где инженер окажется около этого момента — и там, где он сейчас:
        # он может остановиться.
        spots = [q for s, q in man if abs(s - t) <= WINDOW_S] + [here]
        near = min(spots, key=lambda q: math.dist(p, q))
        distance = math.dist(p, near)
        if distance <= DANGER_M:
            best = (t, distance, near)
    return best


# --- Такт ------------------------------------------------------------------------------


def tick(db):
    """Пересчёт предупреждений; новые — в журнал. Коммит — за вызывающим."""
    now, clock = utc_now(), time.monotonic()
    moving = {}
    for aircraft_id, m in _motion.items():
        if clock - m["at"] <= MOTION_FRESH_S and m["speed"] >= MIN_SPEED_MS and m["heading"]:
            moving[aircraft_id] = m
    people = defaultdict(list)
    icaos = {m["icao"] for m in moving.values()}
    if icaos:
        for person in db.query(Employee).filter(
                Employee.airport_icao.in_(icaos), Employee.archived.is_(False), Employee.status != STATUS_OFFLINE):
            if person.lat is None or person.lon is None:
                continue
            if person.location_source == LOCATION_GPS and (
                    person.location_at is None or (now - person.location_at).total_seconds() > ENGINEER_FRESH_S):
                continue
            people[person.airport_icao].append(person)

    walks = {}
    for aircraft_id, m in moving.items():
        aircraft = db.get(Aircraft, aircraft_id)
        if aircraft is None:
            continue
        graph = get_graph(m["icao"])
        position = to_xy(graph, m["lat"], m["lon"])
        passed = m["speed"] * (clock - m["at"])
        paths = aircraft_paths(graph, position, m["heading"], m["speed"] * HORIZON_S + passed)
        tracks = [(path, [(t - (clock - m["at"]), p) for t, p in walk(path, m["speed"]) if t >= clock - m["at"]])
                  for path in paths]
        for person in people[m["icao"]]:
            if person.id not in walks:
                path, speed, call = engineer_path(db, graph, person)
                walks[person.id] = (walk(path, speed), call)
            man, call = walks[person.id]
            hits = [(hit, path) for path, track in tracks if (hit := conflict(track, man))]
            if not hits:
                continue
            (seconds, distance, spot), path = min(hits, key=lambda item: item[0][0])
            warn(db, graph, aircraft, person, call, m, seconds, spot, path, position, now, clock)

    for key in [k for k, w in _active.items() if clock - w["seen"] > HOLD_S]:
        _active.pop(key)


def warn(db, graph, aircraft, person, call, motion, seconds, spot, path, position, now, clock):
    key = (aircraft.id, person.id)
    here = to_xy(graph, person.lat, person.lon)
    gap = math.dist(position, here)
    seconds = max(0, round(seconds))
    lat, lon = to_ll(graph, *spot)
    crosses = math.dist(spot, here) > DANGER_M
    what = "пересечёт ваш маршрут" if crosses else "приближается к вам"
    known = _active.get(key)
    _active[key] = {
        "id": known["id"] if known else f"{aircraft.id}-{person.id}-{int(now.timestamp())}",
        "icao": aircraft.airport_icao, "seen": clock, "started_at": known["started_at"] if known else to_iso_utc(now),
        "aircraft_id": aircraft.id, "board_number": aircraft.board_number,
        "employee_id": person.id, "employee_name": person.full_name, "call_id": call.id if call else None,
        "seconds": seconds, "distance_m": round(gap), "speed_kmh": round(motion["speed"] * 3.6),
        "from": side(position, here), "urgent": seconds <= URGENT_S or gap <= DANGER_M, "crosses": crosses,
        "lat": motion["lat"], "lon": motion["lon"], "point": {"lat": lat, "lon": lon},
        "path": [dict(zip(("lat", "lon"), to_ll(graph, *p))) for p in cut(path, motion["speed"] * HORIZON_S)],
        "text": f"ВС {aircraft.board_number} {what}, {'сейчас' if seconds <= 3 else f'через {seconds} с'}",
    }
    if known is None:
        journal.record(
            db, "aircraft_proximity",
            f"Приближение ВС {aircraft.board_number} к инженеру {person.full_name}: "
            f"{'пересечение маршрута' if crosses else 'сближение'} через {seconds} с, до борта {gap:.0f} м",
            call=call, airport_icao=aircraft.airport_icao, board=aircraft.board_number, employee=person, actor=None, ts=now,
            params={"борт": aircraft.board_number, "инженер": person.full_name, "секунд_до_сближения": seconds,
                    "до_борта_м": round(gap, 1), "скорость_борта_кмч": round(motion["speed"] * 3.6, 1),
                    "место": {"lat": lat, "lon": lon}, "момент": to_iso_utc(now + timedelta(seconds=seconds))},
        )


def cut(path, reach):
    """Начало ломаной длиной reach — для карты."""
    result, left = [path[0]], reach
    for a, b in zip(path, path[1:]):
        span = math.dist(a, b)
        if span >= left:
            k = left / (span or 1)
            result.append((a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k))
            break
        result.append(b)
        left -= span
    return result


def side(position, here):
    """С какой стороны света борт."""
    names = ("с севера", "с северо-востока", "с востока", "с юго-востока", "с юга", "с юго-запада", "с запада", "с северо-запада")
    dx, dy = sub(position, here)
    return names[round(math.degrees(math.atan2(dx, -dy)) % 360 / 45) % 8]


def warnings(icao=None, employee_id=None):
    """Действующие предупреждения, ближайшие по времени первыми."""
    found = [
        {k: v for k, v in w.items() if k not in ("seen", "icao")} for w in list(_active.values())
        if (icao is None or w["icao"] == icao) and (employee_id is None or w["employee_id"] == employee_id)
        and time.monotonic() - w["seen"] <= HOLD_S
    ]
    return sorted(found, key=lambda w: w["seconds"])
