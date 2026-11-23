"""
Приём местоположения агентов по контракту POST /api/v1/track.

Один путь для всех источников — имитатора организаторов, страницы
полевого инженера, сторонних скриптов: разбор, проверка, отбраковка
выбросов и привязка к графу аэропорта. Источник на логику не влияет.

Идентификаторы объектов:
  e-NN      — сотрудник ОТО с id = NN          (e-01, e-7)
  v-NN      — машина парка с id = NN            (v-01)
  позывной  — машина парка по позывному         (ТМ-01)
  бортовой  — воздушное судно по номеру         (RA-73142, RA73142)
"""

import math
import re
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func

from algorithm.geo import haversine_m
from clock import utc_now
from constants import LOCATION_GPS, MODE_WALK
from graph_registry import get_graph
from models.aircraft import Aircraft
from models.employee import Employee
from models.vehicle import Vehicle

KIND_ENGINEER = "engineer"
KIND_VEHICLE = "vehicle"
KIND_AIRCRAFT = "aircraft"

# Точность хуже этой — точка не годится ни для карты, ни для фиксации
# прибытия в зону обслуживания.
MAX_ACCURACY_M = 100
# Предельные скорости, выше которых скачок считается выбросом GPS.
# Инженер может ехать на спецтранспорте, поэтому предел у него «машинный».
MAX_SPEED_MS = {KIND_ENGINEER: 25, KIND_VEHICLE: 25, KIND_AIRCRAFT: 120}
# Скачки короче не проверяются: дрожание координат на месте даёт
# огромную «скорость» на секундном интервале.
JUMP_TOLERANCE_M = 30
# Дальше этого от ближайшей вершины графа точка — заведомо не в аэропорту.
MAX_GRAPH_DISTANCE_M = 5000
# Время из будущего допускаем с запасом на расхождение часов.
MAX_FUTURE_S = 60
# Борт ближе этого к свободной стоянке считается вставшим на неё.
STAND_SNAP_M = 25

# Последняя принятая точка машин и бортов: (момент, lat, lon). У сотрудника
# это хранится в карточке (location_at), у машин и бортов колонок нет,
# а для отбраковки устаревших точек и выбросов памяти процесса достаточно.
_last_points = {}


class BadPoint(ValueError):
    """Точка синтаксически некорректна — accepted: false / HTTP 400."""


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_time(value):
    """ISO 8601 → naive UTC (как всё время в базе)."""
    if not isinstance(value, str):
        raise BadPoint("t должно быть строкой ISO 8601")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        raise BadPoint(f"t не в формате ISO 8601: {value}") from error
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def format_time(moment):
    """naive UTC → 2026-12-03T09:14:20.123Z, как в контракте."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def parse_point(raw, now):
    """Проверка синтаксиса одной точки. Возвращает нормализованный словарь."""
    if not isinstance(raw, dict):
        raise BadPoint("точка должна быть объектом JSON")
    obj = raw.get("obj")
    if not isinstance(obj, str) or not obj.strip():
        raise BadPoint("obj обязателен и должен быть строкой")
    lat, lon = raw.get("lat"), raw.get("lon")
    if not _is_number(lat) or not _is_number(lon):
        raise BadPoint("lat и lon обязательны и должны быть числами")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise BadPoint("lat/lon вне допустимого диапазона WGS-84")
    for key in ("acc", "spd"):
        if raw.get(key) is not None and (not _is_number(raw[key]) or raw[key] < 0):
            raise BadPoint(f"{key} должно быть неотрицательным числом")
    t = parse_time(raw["t"]) if raw.get("t") is not None else now
    return {
        "obj": obj.strip(), "lat": float(lat), "lon": float(lon), "t": t,
        "acc": raw.get("acc"), "spd": raw.get("spd"),
    }


def _normalize(code):
    return re.sub(r"[\s\-_]", "", code).upper()


def resolve(db, obj):
    """Объект по идентификатору из контракта: (вид, запись) или (None, None)."""
    match = re.fullmatch(r"([evEV])-?(\d+)", obj)
    if match:
        prefix, number = match.group(1).lower(), int(match.group(2))
        model = Employee if prefix == "e" else Vehicle
        record = db.get(model, number)
        if record is not None and not record.archived:
            return (KIND_ENGINEER if prefix == "e" else KIND_VEHICLE), record

    code = _normalize(obj)
    vehicle = next(
        (v for v in db.query(Vehicle).filter(Vehicle.archived.is_(False)).all()
         if _normalize(v.call_sign) == code),
        None,
    )
    if vehicle is not None:
        return KIND_VEHICLE, vehicle
    aircraft = (
        db.query(Aircraft)
        .filter(func.upper(func.replace(Aircraft.board_number, "-", "")) == code)
        .first()
    )
    if aircraft is not None:
        return KIND_AIRCRAFT, aircraft
    return None, None


def track_id(kind, record):
    """Идентификатор объекта, под которым его ждёт POST /api/v1/track."""
    if kind == KIND_ENGINEER:
        return f"e-{record.id:02d}"
    if kind == KIND_VEHICLE:
        return f"v-{record.id:02d}"
    return record.board_number


def _graph(icao):
    """Граф аэропорта или None, если он не загружен."""
    try:
        return get_graph(icao)
    except HTTPException:
        return None


def _previous(kind, record):
    if kind == KIND_ENGINEER:
        if record.location_at is None or record.lat is None:
            return None
        return record.location_at, record.lat, record.lon
    return _last_points.get((kind, record.id))


def is_outlier(kind, record, point, now):
    """Причина отбраковки точки или None."""
    if point["acc"] is not None and point["acc"] > MAX_ACCURACY_M:
        return "низкая точность"
    if (point["t"] - now).total_seconds() > MAX_FUTURE_S:
        return "время из будущего"
    graph = _graph(record.airport_icao)
    if graph is not None:
        _, distance = graph.nearest_node(point["lat"], point["lon"], MODE_WALK)
        if distance is not None and distance > MAX_GRAPH_DISTANCE_M:
            return "точка вне аэропорта"
    previous = _previous(kind, record)
    if previous is None:
        return None
    moment, lat, lon = previous
    seconds = (point["t"] - moment).total_seconds()
    if seconds <= 0:
        return "точка старее уже принятой"
    jump = haversine_m(lat, lon, point["lat"], point["lon"])
    if jump > JUMP_TOLERANCE_M and jump / seconds > MAX_SPEED_MS[kind]:
        return "нереальная скорость перемещения"
    return None


def _apply_engineer(db, employee, point, now):
    from services.assignment import record_position, refresh_navigation

    # Возраст фиксации, а не время: record_position сам вычтет его
    # из момента приёма — так же, как для PWA инженера.
    age = max(0.0, (now - point["t"]).total_seconds())
    changed = record_position(employee, point["lat"], point["lon"], LOCATION_GPS, age, now=now)
    if changed:
        if point["spd"] is not None and 0.3 <= point["spd"] <= 3.5 and not employee.has_vehicle:
            employee.speed_kmh = point["spd"] * 3.6
        refresh_navigation(db, employee)
    return changed


def _apply_vehicle(vehicle, point):
    # Пока машина едет с инженером, её место берётся из его GPS
    # (Vehicle.position), а сохранённые координаты станут местом стоянки.
    vehicle.lat, vehicle.lon = point["lat"], point["lon"]
    if point["spd"] is not None and 1 <= point["spd"] <= MAX_SPEED_MS[KIND_VEHICLE]:
        vehicle.speed_kmh = point["spd"] * 3.6
    return True


def _apply_aircraft(db, aircraft, point):
    """
    Борт: встал на свободную стоянку — занимает её, иначе стоит свободно
    в точке (рулёжка, ВПП), и маршруты к нему ведут к ближайшей вершине.
    Маршруты открытых вызовов по борту перестраиваются.
    """
    from api.airports import apply_aircraft_move

    graph = _graph(aircraft.airport_icao)
    if graph is None:
        return False
    lat, lon = point["lat"], point["lon"]
    occupied = {
        stand for (stand,) in db.query(Aircraft.stand_node_id).filter(
            Aircraft.airport_icao == aircraft.airport_icao,
            Aircraft.id != aircraft.id,
            Aircraft.lat.is_(None),
        )
    }
    stand = min(
        (n for n in graph.nodes_of_type("stand") if n["id"] not in occupied),
        default=None,
        key=lambda n: haversine_m(lat, lon, n["lat"], n["lon"]),
    )
    if stand is not None and haversine_m(lat, lon, stand["lat"], stand["lon"]) <= STAND_SNAP_M:
        if aircraft.lat is None and aircraft.stand_node_id == stand["id"]:
            return True
        aircraft.lat = aircraft.lon = None
        target = stand
    else:
        target, _ = graph.nearest_node(lat, lon, MODE_WALK)
        if target is None:
            return False
        aircraft.lat, aircraft.lon = lat, lon
        if aircraft.stand_node_id == target["id"]:
            return True

    savepoint = db.begin_nested()
    try:
        apply_aircraft_move(db, graph, aircraft, target)
    except HTTPException:
        # Нет маршрута до нового места — оставляем борт, где был.
        savepoint.rollback()
        return False
    savepoint.commit()
    return True


def ingest(db, point, now=None):
    """
    Применение одной синтаксически верной точки.
    Возвращает applied: принята ли точка в состояние объекта.
    """
    now = now or utc_now()
    kind, record = resolve(db, point["obj"])
    if record is None:
        return False
    if is_outlier(kind, record, point, now):
        return False
    if kind == KIND_ENGINEER:
        applied = _apply_engineer(db, record, point, now)
    elif kind == KIND_VEHICLE:
        applied = _apply_vehicle(record, point)
    else:
        applied = _apply_aircraft(db, record, point)
    if applied and kind != KIND_ENGINEER:
        _last_points[(kind, record.id)] = (point["t"], point["lat"], point["lon"])
    return applied


def known_objects(db):
    """Все объекты, координаты которых принимает /api/v1/track."""
    result = []
    for employee in db.query(Employee).filter(Employee.archived.is_(False)).order_by(Employee.id):
        result.append({"obj": track_id(KIND_ENGINEER, employee), "kind": KIND_ENGINEER,
                       "name": employee.full_name, "airport_icao": employee.airport_icao})
    for vehicle in db.query(Vehicle).filter(Vehicle.archived.is_(False)).order_by(Vehicle.id):
        result.append({"obj": track_id(KIND_VEHICLE, vehicle), "kind": KIND_VEHICLE,
                       "name": vehicle.call_sign, "airport_icao": vehicle.airport_icao})
    for aircraft in db.query(Aircraft).order_by(Aircraft.id):
        result.append({"obj": track_id(KIND_AIRCRAFT, aircraft), "kind": KIND_AIRCRAFT,
                       "name": f"{aircraft.board_number} ({aircraft.aircraft_type})",
                       "airport_icao": aircraft.airport_icao})
    return result


def reset_memory():
    """Для тестов: забыть последние точки машин и бортов."""
    _last_points.clear()

