"""
Назначение вызовов, очередь вызовов у инженера и машины парка.

Здесь собрано всё, что меняет занятость сотрудника и машины: назначение
сразу, постановка в очередь, продвижение очереди после закрытия вызова,
резерв и возврат машины.

Почему отдельный модуль. Раньше закрытие вызова было продублировано
в двух ручках — диспетчерской и в PWA — и в обеих освобождало инженера
целиком. При двух назначенных вызовах это давало вызов, к которому никто
не едет: инженер закрывал один и становился «свободен», а второй оставался
назначенным без исполнителя. Одно место — одно правило.

Инвариант: у сотрудника не больше одного текущего вызова (назначен,
принят, на месте). Остальные его вызовы стоят в очереди в порядке
постановки и выполняются по одному.

Машины. Машина парка резервируется при назначении и ждёт инженера там,
где стоит. Когда он доедет до борта, она едет с ним по всей его цепочке
вызовов, а после последнего остаётся у борта — там её подберёт следующий.
Незабранная машина при отмене вызова возвращается в парк на прежнее место.
"""

from datetime import timedelta

from algorithm.routing import Route, route_from_point, shortest_path
from algorithm.transport import plan_for
from clock import utc_now
from constants import (
    DEFAULT_WORK_DURATION_MIN,
    MODE_VEHICLE,
    MODE_WALK,
    STATUS_ASSIGNED,
    STATUS_FREE,
    STATUSES_BUSY,
    VEHICLE_FREE,
    VEHICLE_IN_USE,
    VEHICLE_RESERVED,
)
from graph_registry import get_graph
from models.call import STATUS_ASSIGNED as CALL_ASSIGNED
from models.call import STATUS_QUEUED as CALL_QUEUED
from models.call import WORKING_CALL_STATUSES, Call
from models.vehicle import Vehicle

WORK_DURATION = timedelta(minutes=DEFAULT_WORK_DURATION_MIN)


def movement_mode(employee):
    """
    Как сотрудник перемещается по своей цепочке вызовов.

    Машина за ним (едет с ним или ждёт его) — значит, между бортами
    цепочки он едет. Иначе — пешком.
    """
    return MODE_VEHICLE if employee.vehicle is not None else MODE_WALK


def movement_speed(employee):
    """Скорость перемещения по цепочке: машины, если она за ним, иначе своя."""
    if employee.vehicle is not None:
        return employee.vehicle.speed_kmh
    return employee.speed_kmh


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


def free_vehicles(db, airport_icao):
    """Свободные машины парка аэропорта — только их можно предложить."""
    return (
        db.query(Vehicle)
        .filter(Vehicle.airport_icao == airport_icao, Vehicle.status == VEHICLE_FREE)
        .order_by(Vehicle.call_sign)
        .all()
    )


def plan_route(db, employee, stand_node_id, graph):
    """
    Как сотрудник доберётся до стоянки — тот же расчёт, что в подборе.

    Возвращает пару: план и машину парка, которую ему надо забрать
    (None, если он идёт пешком или машина уже при нём).
    """
    if employee.lat is None or employee.lon is None:
        return None, None

    pool = free_vehicles(db, employee.airport_icao)
    plan = plan_for(
        graph,
        employee.as_algorithm_dict(),
        stand_node_id,
        [vehicle.as_algorithm_dict() for vehicle in pool],
    )
    if plan is None or not plan.pickup:
        return plan, None

    chosen = next((vehicle for vehicle in pool if vehicle.id == plan.vehicle["id"]), None)
    return plan, chosen


def travel_between(graph, employee, from_stand, to_stand):
    """
    Переезд к следующему вызову цепочки.

    from_stand = None означает «от текущих координат». Иначе — от стоянки
    предыдущего вызова: к новому борту инженер поедет оттуда, где закончит
    работу, а не оттуда, где стоит сейчас.
    """
    mode = movement_mode(employee)
    speed = movement_speed(employee)

    if from_stand is None:
        if employee.lat is None or employee.lon is None:
            return None
        return route_from_point(graph, employee.lat, employee.lon, to_stand, mode, speed)

    route = shortest_path(graph, from_stand, to_stand, mode)
    if route is None:
        return None
    # Пересборка ради скорости машины или человека: shortest_path знает
    # только нормативную скорость способа передвижения.
    return Route(route.node_ids, route.distance_m, mode, 0.0, speed)


def assign_now(db, call, employee, plan, pickup_vehicle):
    """
    Назначение на свободного сотрудника: он выезжает сразу.

    Если по плану он забирает свободную машину, она резервируется за ним:
    второму её уже не предложат.
    """
    # Всюду присваиваются связи, а не внешние ключи. Сессия не перечитывает
    # объекты после коммита (expire_on_commit=False), и связь, загруженная
    # раньше как пустая, осталась бы пустой: ответ на назначение отдавал бы
    # исполнителя и машину как None, хотя в базе они записаны.
    call.assigned_employee = employee
    call.status = CALL_ASSIGNED
    call.eta_minutes = round(plan.minutes, 1)
    call.eta_at = utc_now() + timedelta(minutes=plan.minutes)
    call.route_node_ids = plan.node_ids
    call.pickup_node_id = plan.pickup_node_id

    if pickup_vehicle is not None:
        # Через связь обновится и обратная сторона (employee.vehicle),
        # и дальнейший расчёт цепочки увидит машину сразу.
        pickup_vehicle.employee = employee
        pickup_vehicle.status = VEHICLE_RESERVED
        call.vehicle = pickup_vehicle
    else:
        call.vehicle = employee.vehicle

    employee.status = STATUS_ASSIGNED
    replan_queue(db, employee)


def enqueue(db, call, employee):
    """
    Постановка вызова в очередь к занятому сотруднику.

    Время прибытия считается по всей цепочке: сначала сотрудник закончит
    текущий вызов и всё, что уже стоит перед этим в очереди, и только
    потом поедет сюда.
    """
    call.assigned_employee = employee
    call.status = CALL_QUEUED
    call.queued_at = utc_now()
    call.vehicle = employee.vehicle
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
    становится текущим и сразу уходит инженеру в телефон; машина остаётся
    при нём. Очередь пуста — сотрудник освобождается, машина остаётся
    у борта. Закрыт (отменён) вызов из очереди — сотрудник по-прежнему
    занят, пересчитывается только цепочка.
    """
    db.flush()

    if not was_working or working_call_of(db, employee) is not None:
        replan_queue(db, employee)
        return

    graph = get_graph(employee.airport_icao)
    queue = queued_calls_of(db, employee)
    if not queue:
        release_vehicle(employee, graph, closed_call.stand_node_id)
        employee.status = STATUS_FREE
        employee.busy_until = None
        return

    next_call = queue[0]
    # Сначала от фактических координат, без них — от стоянки только что
    # закрытого вызова: инженер там и заканчивал работу.
    route = travel_between(graph, employee, None, next_call.stand_node_id)
    if route is None:
        route = travel_between(graph, employee, closed_call.stand_node_id, next_call.stand_node_id)

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

    Если инженер ехал на зарезервированной машине, с этого момента она
    при нём: доехал до борта — значит, забрал. По фактическому времени
    пересчитывается очередь.
    """
    call.eta_at = utc_now()
    vehicle = employee.vehicle
    if vehicle is not None and vehicle.status == VEHICLE_RESERVED:
        vehicle.status = VEHICLE_IN_USE
    replan_queue(db, employee)


def release_vehicle(employee, graph=None, stand_node_id=None):
    """
    Машина возвращается в парк.

    Ехала с инженером — остаётся там, где он закончил работу: у борта,
    если известна стоянка, иначе в его точке. Так и не была забрана —
    стоит там же, где стояла.
    """
    vehicle = employee.vehicle
    if vehicle is None:
        return

    if vehicle.status == VEHICLE_IN_USE:
        lat, lon = parking_point(employee, graph, stand_node_id)
        if lat is not None:
            vehicle.lat = lat
            vehicle.lon = lon

    vehicle.status = VEHICLE_FREE
    vehicle.employee = None


def parking_point(employee, graph, stand_node_id):
    """Где оставить машину: у стоянки, если она известна, иначе у инженера."""
    if graph is not None and stand_node_id:
        node = graph.node(stand_node_id)
        if node is not None:
            return node["lat"], node["lon"]
    return employee.lat, employee.lon
