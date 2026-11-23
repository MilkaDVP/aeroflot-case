"""Единый приём GPS-точек от PWA, GPSLogger и имитатора организаторов."""

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from algorithm.geo import haversine_m
from clock import utc_now
from constants import (
    AIRCRAFT_ARRIVAL_RADIUS_M,
    LOCATION_GPS,
    LOCATION_MANUAL,
    LOCATION_SIMULATION,
    STATUS_BUSY,
)
from graph_registry import get_graph
from models.aircraft import Aircraft
from models.call import STATUS_ACCEPTED, STATUS_ARRIVED, Call
from models.employee import Employee
from models.vehicle import Vehicle
from services import mentor
from services.assignment import mark_arrived, record_position, working_call_of

MAX_FUTURE_S = 300
MAX_BATCH_POINTS = 1000
MAX_SPEED_MPS = {"employee": 100.0, "vehicle": 100.0, "aircraft": 350.0}
TRACK_SOURCES = (LOCATION_GPS, LOCATION_SIMULATION, LOCATION_MANUAL)


class TrackPayloadError(ValueError):
    """Ошибка JSON или полей: по контракту такой запрос отклоняется с 400."""


@dataclass(frozen=True)
class ParsedTrackPoint:
    obj: str
    lat: float
    lon: float
    measured_at: datetime
    accuracy_m: float | None
    speed_mps: float | None
    source: str


def track_iso(value):
    """UTC ISO 8601 с миллисекундами и суффиксом Z, как в приложении к заданию."""
    return (
        value.replace(tzinfo=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def finite_number(raw, name, minimum=None, maximum=None, required=True):
    if raw is None:
        if required:
            raise TrackPayloadError(f"Поле {name} обязательно")
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TrackPayloadError(f"Поле {name} должно быть числом")
    value = float(raw)
    if not math.isfinite(value):
        raise TrackPayloadError(f"Поле {name} должно быть конечным числом")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise TrackPayloadError(f"Поле {name} вне допустимого диапазона")
    return value


def parse_time(raw, received_at):
    if raw is None:
        return received_at
    if not isinstance(raw, str) or not raw.strip():
        raise TrackPayloadError("Поле t должно быть строкой ISO 8601 UTC")
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise TrackPayloadError("Поле t должно быть строкой ISO 8601 UTC") from error
    if parsed.tzinfo is None:
        # Контракт требует UTC. Наивную строку толкуем именно как UTC, а не как
        # локальное время сервера, чтобы результат не зависел от контейнера.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def parse_point(raw, received_at):
    if not isinstance(raw, dict):
        raise TrackPayloadError("Каждая точка должна быть JSON-объектом")
    obj = raw.get("obj")
    if not isinstance(obj, str) or not obj.strip():
        raise TrackPayloadError("Поле obj обязательно и должно быть непустой строкой")
    source = raw.get("source", LOCATION_GPS)
    if source not in TRACK_SOURCES:
        raise TrackPayloadError("Поле source должно быть gps, simulation или manual")
    return ParsedTrackPoint(
        obj=obj.strip(),
        lat=finite_number(raw.get("lat"), "lat", -90, 90),
        lon=finite_number(raw.get("lon"), "lon", -180, 180),
        measured_at=parse_time(raw.get("t"), received_at),
        accuracy_m=finite_number(raw.get("acc"), "acc", 0, required=False),
        speed_mps=finite_number(raw.get("spd"), "spd", 0, required=False),
        source=source,
    )


def parse_payload(payload, received_at=None):
    """Разбирает одиночную точку или пакет, сохраняя порядок точек."""
    received_at = received_at or utc_now()
    if not isinstance(payload, dict):
        raise TrackPayloadError("Тело запроса должно быть JSON-объектом")
    if "points" in payload:
        raw_points = payload["points"]
        if not isinstance(raw_points, list):
            raise TrackPayloadError("Поле points должно быть массивом")
        if len(raw_points) > MAX_BATCH_POINTS:
            raise TrackPayloadError(f"В пакете не более {MAX_BATCH_POINTS} точек")
    else:
        raw_points = [payload]
    return received_at, [parse_point(item, received_at) for item in raw_points]


def find_target(db, obj):
    """Возвращает (тип, ORM-объект), либо (None, None) для неизвестного ID."""
    match = re.fullmatch(r"e-(\d+)", obj, flags=re.IGNORECASE)
    if match:
        employee = db.get(Employee, int(match.group(1)))
        if employee is not None and not employee.archived:
            return "employee", employee
        return None, None

    match = re.fullmatch(r"v-(\d+)", obj, flags=re.IGNORECASE)
    if match:
        vehicle = db.get(Vehicle, int(match.group(1)))
        if vehicle is not None and not vehicle.archived:
            return "vehicle", vehicle
        return None, None

    # Регистрационный номер ВС должен быть уникален как внешний идентификатор.
    # Если в разных аэропортах заведены одинаковые борта, молча выбирать один
    # из них нельзя: такая точка считается непривязанной.
    aircraft = db.query(Aircraft).filter(Aircraft.board_number == obj.upper()).all()
    if len(aircraft) == 1:
        return "aircraft", aircraft[0]
    return None, None


def applicable(kind, target, point, received_at):
    """Отбрасывает старые, будущие и физически невозможные фиксации."""
    if point.measured_at > received_at + timedelta(seconds=MAX_FUTURE_S):
        return False

    current_at = target.location_at
    # Исходная ручная расстановка — не предыдущий GPS-фикс. Первую реальную
    # точку нельзя отклонять из-за расстояния от демонстрационной позиции.
    if target.location_source == LOCATION_MANUAL:
        return True

    if current_at is not None and point.measured_at < current_at:
        return False

    if current_at is not None and target.lat is not None and target.lon is not None:
        elapsed = (point.measured_at - current_at).total_seconds()
        distance = haversine_m(target.lat, target.lon, point.lat, point.lon)
        accuracy_allowance = 50.0 + (target.location_accuracy_m or 0) + (point.accuracy_m or 0)
        allowed = MAX_SPEED_MPS[kind] * max(elapsed, 0) + accuracy_allowance
        if distance > allowed:
            return False
    return True


def set_live_position(target, point, received_at):
    target.lat = point.lat
    target.lon = point.lon
    target.location_at = point.measured_at
    target.seen_at = received_at
    target.location_source = point.source
    target.location_accuracy_m = point.accuracy_m
    target.location_speed_mps = point.speed_mps
    if hasattr(target, "updated_at"):
        target.updated_at = received_at


def apply_employee_point(db, employee, point, received_at):
    age_s = max(0, (received_at - point.measured_at).total_seconds())
    record_position(employee, point.lat, point.lon, point.source, age_s, now=received_at)
    employee.location_accuracy_m = point.accuracy_m
    employee.location_speed_mps = point.speed_mps

    # Автоматическое прибытие возможно только после явного приёма заявки.
    # Назначение диспетчером само по себе не переводит заявку в работу.
    call = working_call_of(db, employee)
    if call is None or call.status != STATUS_ACCEPTED:
        return
    graph = get_graph(employee.airport_icao)
    stand = graph.node(call.stand_node_id)
    if stand is None:
        return
    distance = haversine_m(point.lat, point.lon, stand["lat"], stand["lon"])
    if distance > AIRCRAFT_ARRIVAL_RADIUS_M:
        return

    call.status = STATUS_ARRIVED
    employee.status = STATUS_BUSY
    if mark_arrived(db, employee, call, point.measured_at):
        mentor.review_arrival(db, call, employee, point.measured_at)


def apply_track_payload(db, payload):
    """Применяет запрос и возвращает один результат либо список результатов."""
    received_at, points = parse_payload(payload)
    results = []
    for point in points:
        kind, target = find_target(db, point.obj)
        applied = target is not None and applicable(kind, target, point, received_at)
        if applied:
            if kind == "employee":
                apply_employee_point(db, target, point, received_at)
            else:
                set_live_position(target, point, received_at)
        results.append(
            {
                "accepted": True,
                "applied": applied,
                "obj": point.obj,
                "t": track_iso(point.measured_at),
            }
        )
    return results[0] if "points" not in payload else results
