"""
Приём местоположения агентов по контракту POST /api/v1/track.

Один путь для всех источников — имитатора организаторов, страницы
полевого инженера, сторонних скриптов: разбор, проверка, отбраковка
выбросов и привязка к графу аэропорта. Источник на логику не влияет.

Идентификаторы объектов:
  e-NN      — NN-й по порядку сотрудник ОТО полигона   (e-01, e-7)
  v-NN      — NN-я по порядку машина полигона          (v-01)
  позывной  — машина парка по позывному                (ТМ-01)
  бортовой  — воздушное судно по номеру                (RA-73142, RA73142)
Полигон — аэропорт TRACK_AIRPORT (по умолчанию VDNH). Объекты других
аэропортов — с кодом впереди: «UUEE:e-01», «UUEE:ТМ-01».
"""

import math
import os
import re
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from algorithm.geo import haversine_m
from clock import utc_now
from constants import LOCATION_GPS, MODE_WALK, VEHICLE_IN_USE
from graph_registry import get_graph
from models.aircraft import Aircraft
from models.employee import Employee
from services import journal
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
# Насколько время точки может расходиться с часами сервера, чтобы ему
# верить. Больше — у источника свои часы (сценарий имитатора): порядок его
# точек проверяется по t, а моментом измерения считается момент приёма.
CLOCK_TRUST_S = 60
# Источник молчал столько и прислал более раннее время — его перезапустили.
RESTART_AFTER_S = 60
# Фиксация прибытия по точкам /track: имитатор шлёт их реже телефона.
ARRIVAL_MAX_GAP_S = 60
# Об одинаковой отбраковке объекта — в журнал не чаще.
REJECT_LOG_EVERY_S = 60
# Телефон инженера сообщал место недавнее этого — трекер машины его не двигает.
PHONE_FRESH_S = 10
# Борт ближе этого к свободной стоянке считается вставшим на неё.
STAND_SNAP_M = 25

# Последняя принятая по /track точка: (t, lat, lon, момент приёма). Для
# отбраковки устаревших точек и выбросов памяти процесса достаточно.
_last_points = {}
_logged = {}


def forget_points():
    """Забыть прежние места: объекты расставлены заново."""
    _last_points.clear()


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


def track_airport():
    return os.environ.get("TRACK_AIRPORT", "VDNH").upper()


def resolve(db, obj):
    """Объект по идентификатору из контракта: (вид, запись) или (None, None)."""
    prefix, _, rest = obj.partition(":")
    icao, code_text = (prefix.strip().upper(), rest.strip()) if rest else (track_airport(), obj)
    code = _normalize(code_text)
    # Позывной важнее порядкового номера: машину могли назвать «v-01».
    vehicles = db.query(Vehicle).filter_by(airport_icao=icao).order_by(Vehicle.id).all()
    for vehicle in vehicles:
        if not vehicle.archived and _normalize(vehicle.call_sign) == code:
            return KIND_VEHICLE, vehicle
    # Табельный номер — первое слово имени: «AT-0001 · Инженер».
    for person in db.query(Employee).filter_by(airport_icao=icao, archived=False):
        if _normalize(person.full_name.split(" ")[0]) == code:
            return KIND_ENGINEER, person
    match = re.fullmatch(r"([EV])(\d+)", code)
    if match:
        kind = KIND_ENGINEER if match.group(1) == "E" else KIND_VEHICLE
        # Номер — по порядку заведения, вместе с архивными: удаление
        # человека не должно сдвинуть номера остальных.
        records = vehicles if kind == KIND_VEHICLE else (
            db.query(Employee).filter_by(airport_icao=icao).order_by(Employee.id).all())
        index = int(match.group(2)) - 1
        if 0 <= index < len(records) and not records[index].archived:
            return kind, records[index]
        return None, None
    boards = [
        aircraft for aircraft in db.query(Aircraft).order_by(Aircraft.id)
        if _normalize(aircraft.board_number) == code and (not rest or aircraft.airport_icao == icao)
    ]
    if boards:
        # Бортовой номер единственный в мире; полигон — первым.
        return KIND_AIRCRAFT, next((b for b in boards if b.airport_icao == icao), boards[0])
    return None, None


def _graph(icao):
    """Граф аэропорта или None, если он не загружен."""
    try:
        return get_graph(icao)
    except HTTPException:
        return None


def _moment(point, now):
    """Момент измерения: t источника, если его часам можно верить."""
    if abs((now - point["t"]).total_seconds()) <= CLOCK_TRUST_S:
        return min(point["t"], now)
    return now


def is_outlier(kind, record, point, now):
    """Причина отбраковки точки или None."""
    if point["acc"] is not None and point["acc"] > MAX_ACCURACY_M:
        return "низкая точность"
    graph = _graph(record.airport_icao)
    if graph is not None:
        _, distance = graph.nearest_node(point["lat"], point["lon"], MODE_WALK)
        if distance is not None and distance > MAX_GRAPH_DISTANCE_M:
            return "точка вне аэропорта"
    previous = _last_points.get((kind, record.id))
    if previous is not None:
        moment, lat, lon, received = previous
        seconds = (point["t"] - moment).total_seconds()
        if seconds <= 0:
            if (now - received).total_seconds() < RESTART_AFTER_S:
                return "точка старее уже принятой"
            return None
        # Точка, стоявшая до сбоя часов, могла прийти с любым t: скорость
        # считаем по большему из двух интервалов.
        seconds = max(seconds, (now - received).total_seconds())
    elif kind == KIND_ENGINEER and not _placed_by_vehicle(record) and record.location_source == LOCATION_GPS and record.location_at and record.lat is not None:
        # Место передал телефон. Заданное вручную с точкой не сравнивают:
        # расстановка перед испытанием — не перемещение.
        lat, lon = record.lat, record.lon
        seconds = (_moment(point, now) - record.location_at).total_seconds()
        if seconds <= 0:
            return "точка старее уже принятой"
    else:
        return None
    jump = haversine_m(lat, lon, point["lat"], point["lon"])
    if jump > JUMP_TOLERANCE_M and jump / seconds > MAX_SPEED_MS[kind]:
        return "нереальная скорость перемещения"
    return None


def _placed_by_vehicle(employee):
    """Последнее место инженера задал трекер его машины, а не он сам."""
    return employee.location_at is not None and _from_vehicle.get(employee.id) == employee.location_at


def _apply_engineer(db, employee, point, now):
    from services.assignment import check_aircraft_arrival, record_position, refresh_navigation

    moment = _moment(point, now)
    if moment == now or _placed_by_vehicle(employee):
        # Пакет точек с чужими часами приходит в одно мгновение: каждая
        # следующая должна остаться позже предыдущей.
        if employee.location_source == LOCATION_GPS and employee.location_at and moment <= employee.location_at:
            now = moment = employee.location_at + timedelta(milliseconds=1)
    age = max(0.0, (now - moment).total_seconds())
    # Скорость из точек не выводится: нормативные скорости положения
    # фиксированы, а имитатор может идти в ускоренном времени.
    changed = record_position(employee, point["lat"], point["lon"], LOCATION_GPS, age, now=now, learn_speed=False,
                              accuracy_m=point["acc"])
    if changed:
        refresh_navigation(db, employee)
        check_aircraft_arrival(db, employee, point["acc"], max_gap_s=ARRIVAL_MAX_GAP_S)
    return changed


def _apply_vehicle(db, vehicle, point, now):
    vehicle.lat, vehicle.lon = point["lat"], point["lon"]
    employee = vehicle.employee
    if vehicle.status == VEHICLE_IN_USE and employee is not None:
        # Машина едет с инженером: её место — его место. Свежий GPS его
        # телефона точнее, иначе двигаем инженера по трекеру машины.
        fresh = (employee.location_source == LOCATION_GPS and employee.location_at
                 and (now - employee.location_at).total_seconds() < PHONE_FRESH_S)
        if not fresh or _placed_by_vehicle(employee):
            if _apply_engineer(db, employee, point, now):
                _from_vehicle[employee.id] = employee.location_at
    return True


# Чьё место последний раз задал трекер машины — и каким моментом.
_from_vehicle = {}


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
    parked = stand is not None and haversine_m(lat, lon, stand["lat"], stand["lon"]) <= STAND_SNAP_M
    target = stand if parked else graph.nearest_node(lat, lon, MODE_WALK)[0]
    if target is None:
        return False
    place = (None, None) if parked else (lat, lon)
    if aircraft.stand_node_id == target["id"] and (aircraft.lat, aircraft.lon) == place:
        return True
    same_target = aircraft.stand_node_id == target["id"]
    # Точка сохранения — до изменения борта: нет маршрута до нового
    # места — борт остаётся там, где был.
    savepoint = db.begin_nested()
    try:
        landed = parked and aircraft.lat is not None
        aircraft.lat, aircraft.lon = place
        if not same_target:
            apply_aircraft_move(db, graph, aircraft, target)
        if landed:
            journal.aircraft_parked(db, aircraft, target["ref"])
        db.flush()
    except HTTPException:
        savepoint.rollback()
        return False
    savepoint.commit()
    return True


def _reject(db, point, record, reason, now):
    """Отбракованная точка — ошибка приёма данных в журнале, без повторов."""
    key = (point["obj"], reason)
    last = _logged.get(key)
    if last is not None and (now - last).total_seconds() < REJECT_LOG_EVERY_S:
        return
    if len(_logged) > 1000:
        _logged.clear()
    _logged[key] = now
    journal.record(
        db, "data_error", f"Точка {point['obj'][:60]} не применена: {reason}",
        airport_icao=record.airport_icao if record is not None else track_airport(),
        actor={"id": None, "name": "Приём геопозиции", "role": "external"},
        params={"obj": point["obj"][:60], "lat": point["lat"], "lon": point["lon"],
                "t": format_time(point["t"]), "причина": reason},
    )


def ingest(db, point, now=None):
    """
    Применение одной синтаксически верной точки.
    Возвращает applied: принята ли точка в состояние объекта.
    """
    now = now or utc_now()
    kind, record = resolve(db, point["obj"])
    if record is None:
        _reject(db, point, None, "объект неизвестен", now)
        return False
    reason = is_outlier(kind, record, point, now)
    if reason:
        _reject(db, point, record, reason, now)
        return False
    if kind == KIND_ENGINEER:
        applied = _apply_engineer(db, record, point, now)
    elif kind == KIND_VEHICLE:
        applied = _apply_vehicle(db, record, point, now)
    else:
        applied = _apply_aircraft(db, record, point)
    if applied:
        _last_points[(kind, record.id)] = (point["t"], point["lat"], point["lon"], now)
    else:
        _reject(db, point, record, "нет маршрута до нового места" if kind == KIND_AIRCRAFT else "точка старее уже принятой", now)
    return applied


def known_objects(db):
    """Все объекты, координаты которых принимает /api/v1/track."""
    result = []

    def add(kind, icao, code, name):
        obj = code if icao == track_airport() or kind == KIND_AIRCRAFT else f"{icao}:{code}"
        result.append({"obj": obj, "kind": kind, "name": name, "airport_icao": icao})

    for kind, model, letter in ((KIND_ENGINEER, Employee, "e"), (KIND_VEHICLE, Vehicle, "v")):
        counts = {}
        for record in db.query(model).order_by(model.id):
            counts[record.airport_icao] = counts.get(record.airport_icao, 0) + 1
            if not record.archived:
                add(kind, record.airport_icao, f"{letter}-{counts[record.airport_icao]:02d}",
                    record.full_name if kind == KIND_ENGINEER else record.call_sign)
    for aircraft in db.query(Aircraft).order_by(Aircraft.id):
        add(KIND_AIRCRAFT, aircraft.airport_icao, aircraft.board_number,
            f"{aircraft.board_number} ({aircraft.aircraft_type})")
    return sorted(result, key=lambda item: item["airport_icao"] != track_airport())


def reset_memory():
    """Для тестов: забыть последние точки."""
    _last_points.clear()
    _logged.clear()
    _from_vehicle.clear()

