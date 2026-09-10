"""
Назначение вызовов и очередь вызовов у инженера.

Здесь собрано всё, что меняет занятость сотрудника: назначение сразу,
постановка в очередь и продвижение очереди после закрытия вызова.

Почему отдельный модуль. Раньше закрытие вызова было продублировано
в двух ручках — диспетчерской и в PWA — и в обеих освобождало инженера
целиком. При двух назначенных вызовах это давало вызов, к которому никто
не едет: инженер закрывал один и становился «свободен», а второй оставался
назначенным без исполнителя. Одно место — одно правило.

Инвариант: у сотрудника не больше одного текущего вызова (назначен,
принят, на месте). Остальные его вызовы стоят в очереди в порядке
постановки и выполняются по одному.
"""

from datetime import timedelta

from algorithm.routing import Route, route_from_point, shortest_path
from clock import utc_now
from constants import (
    DEFAULT_WORK_DURATION_MIN,
    MODE_VEHICLE,
    MODE_WALK,
    STATUS_ASSIGNED,
    STATUS_FREE,
    STATUSES_BUSY,
)
from graph_registry import get_graph
from models.call import STATUS_ASSIGNED as CALL_ASSIGNED
from models.call import STATUS_QUEUED as CALL_QUEUED
from models.call import WORKING_CALL_STATUSES, Call

WORK_DURATION = timedelta(minutes=DEFAULT_WORK_DURATION_MIN)


def movement_mode(employee):
    """Способ передвижения сотрудника: своим ходом или на спецтранспорте."""
    return MODE_VEHICLE if employee.has_vehicle else MODE_WALK


def working_call_of(db, employee):
    """Текущий вызов сотрудника: назначен, принят или на месте."""
    return (
        db.query(Call)
        .filter(
            Call.assigned_employee_id == employee.id,
            Call.status.in_(WORKING_CALL_STATUSES),
        )
        .order_by(Call.created_at, Call.id)
        .first()
    )


def queued_calls_of(db, employee):
    """Вызовы в очереди сотрудника — в порядке постановки."""
    return (
        db.query(Call)
        .filter(Call.assigned_employee_id == employee.id, Call.status == CALL_QUEUED)
        .order_by(Call.queued_at, Call.id)
        .all()
    )


def is_busy(db, employee):
    """
    Занят ли сотрудник.

    Проверяются и вызовы, и статус карточки. Проверка по одному лишь
    «предложению системы» недостаточна: предложение могло устареть, пока
    этого же сотрудника назначали на другой борт.
    """
    return working_call_of(db, employee) is not None or employee.status in STATUSES_BUSY


def route_from_position(employee, stand_node_id, graph):
    """Маршрут от текущих координат сотрудника до стоянки."""
    if employee.lat is None or employee.lon is None:
        return None
    return route_from_point(
        graph,
        employee.lat,
        employee.lon,
        stand_node_id,
        movement_mode(employee),
        employee.speed_kmh,
    )


def travel_between(graph, employee, from_stand, to_stand):
    """
    Переезд к следующему вызову цепочки.

    from_stand = None означает «от текущих координат». Иначе — от стоянки
    предыдущего вызова: к новому борту инженер поедет оттуда, где закончит
    работу, а не оттуда, где стоит сейчас.
    """
    if from_stand is None:
        return route_from_position(employee, to_stand, graph)

    mode = movement_mode(employee)
    route = shortest_path(graph, from_stand, to_stand, mode)
    if route is None:
        return None
    # Пересборка ради индивидуальной скорости: shortest_path знает только
    # нормативную скорость способа передвижения.
    return Route(route.node_ids, route.distance_m, mode, 0.0, employee.speed_kmh)


def assign_now(db, call, employee, route):
    """Назначение на свободного сотрудника: он выезжает сразу."""
    call.assigned_employee_id = employee.id
    call.status = CALL_ASSIGNED
    call.eta_minutes = round(route.minutes, 1)
    call.eta_at = utc_now() + timedelta(minutes=route.minutes)
    call.route_node_ids = route.node_ids
    employee.status = STATUS_ASSIGNED
    replan_queue(db, employee)


def enqueue(db, call, employee):
    """
    Постановка вызова в очередь к занятому сотруднику.

    Время прибытия считается по всей цепочке: сначала сотрудник закончит
    текущий вызов и всё, что уже стоит перед этим в очереди, и только
    потом поедет сюда.
    """
    call.assigned_employee_id = employee.id
    call.status = CALL_QUEUED
    call.queued_at = utc_now()
    replan_queue(db, employee)


def replan_queue(db, employee):
    """
    Пересчитывает время прибытия по всей цепочке вызовов сотрудника.

    Цепочка начинается с текущего вызова: его конец — прибытие плюс
    норматив работ. Если текущего вызова в базе нет (занятость выставлена
    по карточке), отсчёт идёт от времени освобождения по карточке.
    Каждый вызов очереди — переезд от стоянки предыдущего плюс работы.
    Конец цепочки и есть время освобождения сотрудника.
    """
    # Сессия работает без автосброса: изменения, сделанные вызывающим кодом,
    # должны попасть в базу до выборок, иначе цепочка посчитается по старым
    # статусам.
    db.flush()

    graph = get_graph(employee.airport_icao)
    now = utc_now()
    working = working_call_of(db, employee)
    queue = queued_calls_of(db, employee)

    if working is not None:
        cursor_stand = working.stand_node_id
        cursor_time = max((working.eta_at or now) + WORK_DURATION, now)
    else:
        cursor_stand = None
        cursor_time = max(employee.busy_until or now, now)

    for queued in queue:
        route = travel_between(graph, employee, cursor_stand, queued.stand_node_id)
        if route is None:
            # Маршрута нет — прежняя оценка честнее выдуманной.
            continue
        queued.eta_at = cursor_time + timedelta(minutes=route.minutes)
        queued.eta_minutes = round((queued.eta_at - now).total_seconds() / 60, 1)
        queued.route_node_ids = route.node_ids
        cursor_time = queued.eta_at + WORK_DURATION
        cursor_stand = queued.stand_node_id

    if working is not None or queue:
        employee.busy_until = cursor_time


def advance_after_close(db, employee, closed_call, was_working):
    """
    Продвигает очередь после закрытия вызова.

    Закрыт текущий вызов и в очереди что-то есть — первый вызов очереди
    становится текущим и сразу уходит инженеру в телефон. Очередь пуста —
    сотрудник освобождается. Закрыт (отменён) вызов из очереди — сотрудник
    по-прежнему занят, пересчитывается только цепочка.
    """
    db.flush()

    if not was_working or working_call_of(db, employee) is not None:
        replan_queue(db, employee)
        return

    queue = queued_calls_of(db, employee)
    if not queue:
        employee.status = STATUS_FREE
        employee.busy_until = None
        return

    next_call = queue[0]
    graph = get_graph(employee.airport_icao)
    # Сначала от фактических координат, без них — от стоянки только что
    # закрытого вызова: инженер там и заканчивал работу.
    route = route_from_position(employee, next_call.stand_node_id, graph)
    if route is None:
        route = travel_between(
            graph, employee, closed_call.stand_node_id, next_call.stand_node_id
        )

    next_call.status = CALL_ASSIGNED
    employee.status = STATUS_ASSIGNED
    if route is not None:
        next_call.eta_minutes = round(route.minutes, 1)
        next_call.eta_at = utc_now() + timedelta(minutes=route.minutes)
        next_call.route_node_ids = route.node_ids

    replan_queue(db, employee)


def mark_arrived(db, employee, call):
    """
    Прибытие подтверждено: фактическое время вместо расчётного.

    По нему пересчитывается очередь — прогноз для следующих вызовов
    становится точнее.
    """
    call.eta_at = utc_now()
    replan_queue(db, employee)
