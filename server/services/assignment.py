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

from algorithm.geo import haversine_m
from algorithm.routing import route_from_point, shortest_path
from algorithm.transport import STOP_WAREHOUSE, plan_for, plan_via, plan_walk, warehouses
from clock import to_iso_utc, utc_now
from fastapi import HTTPException
from constants import (
    DEFAULT_WORK_DURATION_MIN,
    LOCATION_GPS,
    MODE_VEHICLE,
    MODE_WALK,
    PICKUP_RADIUS_M,
    ROUTE_DEVIATION_M,
    STAGE_INSPECTION,
    STAGE_REPAIR,
    STAGE_WAITING_PARTS,
    STATUS_ASSIGNED,
    STATUS_FREE,
    TRAVEL_CURRENT,
    TRAVEL_WALK,
    TRAVEL_WITH_LEAD,
    STATUSES_BUSY,
    VEHICLE_FREE,
    VEHICLE_IN_USE,
    VEHICLE_RESERVED,
)
from graph_registry import get_graph
from models.call import STATUS_ARRIVED as CALL_ARRIVED
from models.call import STATUS_ASSIGNED as CALL_ASSIGNED
from models.call import STATUS_NEW as CALL_NEW
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
        .filter(Vehicle.airport_icao == airport_icao, Vehicle.status == VEHICLE_FREE, Vehicle.archived.is_(False))
        .order_by(Vehicle.call_sign)
        .all()
    )


WAYPOINT_DONE_RADIUS_M = 25


def waypoint_records(graph, node_ids):
    """Промежуточные точки вызова по узлам графа; несуществующие отбрасываются."""
    from algorithm.transport import place_name, waypoint_kind

    records = []
    for node_id in node_ids or []:
        node = graph.node(node_id)
        if node is None:
            raise HTTPException(422, f"Точки {node_id} нет на карте аэропорта")
        records.append({
            "node_id": node_id,
            "name": place_name(node),
            "type": node.get("type"),
            "kind": waypoint_kind(graph, node_id),
            "done_at": None,
        })
    return records or None


def remaining_waypoints(call):
    """Ещё не пройденные промежуточные точки — узлы по порядку."""
    return [item["node_id"] for item in (getattr(call, "waypoints", None) or []) if not item.get("done_at")]


def mark_waypoint_done(call, node_id=None, at=None):
    """
    Отмечает пройденной точку (по узлу или ближайшую по порядку).

    Склад пройден — значит, инструмент получен. Возвращает True, если что-то
    отмечено.
    """
    items = [dict(item) for item in (call.waypoints or [])]
    for item in items:
        if item.get("done_at") or (node_id is not None and item["node_id"] != node_id):
            continue
        item["done_at"] = to_iso_utc(at or utc_now())
        call.waypoints = items
        if item.get("kind") == "warehouse" and call.needs_tools and call.tools_taken_at is None:
            call.tools_taken_at = at or utc_now()
        return True
    return False


def check_waypoints_reached(call, employee, graph):
    """Инженер подошёл к следующей точке ближе 25 м — она пройдена."""
    remaining = remaining_waypoints(call)
    if not remaining or employee.lat is None:
        return False
    node = graph.node(remaining[0])
    if node and haversine_m(employee.lat, employee.lon, node["lat"], node["lon"]) <= WAYPOINT_DONE_RADIUS_M:
        return mark_waypoint_done(call, remaining[0])
    return False


def depots_for(call, graph):
    """
    Склады, через которые должен пройти маршрут исполнителя.

    Инструмент нужен и ещё не получен: если склад уже выбран при
    назначении — он же (инженер туда и идёт), иначе любой склад
    аэропорта. Склада в аэропорту нет — маршрут без него.
    """
    if not call.needs_tools or call.tools_taken_at is not None:
        return None
    planned = [
        stop["node_id"] for stop in (call.route_stops or [])
        if stop.get("kind") == STOP_WAREHOUSE and graph.node(stop["node_id"])
    ]
    return planned[:1] or warehouses(graph) or None


def plan_route(db, employee, stand_node_id, graph, travel=None, ride_vehicle=None, depots=None,
               waypoints=None):
    """
    Как сотрудник доберётся до стоянки.

    По умолчанию — тот же расчёт, что в подборе: как быстрее. Диспетчер
    может решить иначе: отправить пешком, не занимая машину, или посадить
    второго инженера в машину ответственного (ride_vehicle).

    Возвращает пару: план и машину парка, которую ему надо забрать
    (None, если он идёт пешком, машина уже при нём или она чужая).
    """
    if employee.lat is None or employee.lon is None:
        return None, None
    if travel == TRAVEL_CURRENT:
        if not employee.has_vehicle:
            raise HTTPException(409, "У инженера больше нет этого транспорта")
        return plan_for(graph, employee.as_algorithm_dict(), stand_node_id, [], depots, waypoints), None
    if travel == TRAVEL_WALK:
        return plan_walk(graph, employee.as_algorithm_dict(), stand_node_id, depots, waypoints), None
    if travel == TRAVEL_WITH_LEAD:
        if ride_vehicle is None:
            return None, None
        return plan_via(graph, employee.as_algorithm_dict(), stand_node_id,
                        ride_vehicle.as_algorithm_dict(), waypoints), None

    pool = free_vehicles(db, employee.airport_icao)
    plan = plan_for(
        graph,
        employee.as_algorithm_dict(),
        stand_node_id,
        [vehicle.as_algorithm_dict() for vehicle in pool],
        depots,
        waypoints,
    )
    if plan is None or not plan.pickup:
        return plan, None

    chosen = next((vehicle for vehicle in pool if vehicle.id == plan.vehicle["id"]), None)
    return plan, chosen


def assigned_route(call, graph):
    """Перестроить путь, сохранив назначенную машину и способ поездки."""
    employee = call.assigned_employee
    if employee.lat is None or employee.lon is None:
        return None
    person = employee.as_algorithm_dict()
    vehicle = call.vehicle
    depots = depots_for(call, graph)
    waypoints = remaining_waypoints(call)
    if vehicle is None:
        return plan_walk(graph, person, call.stand_node_id, depots, waypoints)
    if vehicle.status == VEHICLE_RESERVED or vehicle.employee_id != employee.id:
        if depots or waypoints:
            # Машина ждёт, а впереди ещё точки маршрута: выбор пути между
            # ними и этой машиной — как при назначении, но только с ней.
            plan = plan_for(graph, {**person, "vehicle": None}, call.stand_node_id,
                            [vehicle.as_algorithm_dict()], depots, waypoints)
            if plan is not None:
                return plan
        return plan_via(graph, person, call.stand_node_id, vehicle.as_algorithm_dict(), waypoints)
    return plan_for(graph, person, call.stand_node_id, [], depots, waypoints)


def update_assigned_route(call, plan, *, reset_eta=True):
    call.route_start_lat = call.assigned_employee.lat
    call.route_start_lon = call.assigned_employee.lon
    call.route_node_ids = plan.node_ids
    call.pickup_node_id = plan.pickup_node_id
    call.route_stops = plan.stops
    if reset_eta and call.status != CALL_ARRIVED:
        call.eta_minutes = round(plan.minutes, 1)
        call.eta_at = utc_now() + timedelta(minutes=plan.minutes)


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

    return shortest_path(graph, from_stand, to_stand, mode, speed)


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
    if employee.waiting_lat is None:
        employee.waiting_lat, employee.waiting_lon = employee.lat, employee.lon
    employee.return_to_waiting = False
    employee.return_vehicle = False
    call.assigned_employee = employee
    call.status = CALL_ASSIGNED
    call.require_vehicle_pickup = False
    call.eta_minutes = round(plan.minutes, 1)
    call.eta_at = utc_now() + timedelta(minutes=plan.minutes)
    call.route_start_lat = call.assigned_employee.lat
    call.route_start_lon = call.assigned_employee.lon
    call.route_node_ids = plan.node_ids
    call.pickup_node_id = plan.pickup_node_id
    call.route_stops = plan.stops

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

    reachable = True
    for queued in queue:
        route = travel_between(graph, employee, cursor_stand, queued.stand_node_id)
        if route is None:
            reachable = False
            # Маршрута нет — прежняя оценка честнее выдуманной.
            continue
        queued.eta_at = cursor_time + timedelta(minutes=route.minutes)
        queued.eta_minutes = round((queued.eta_at - now).total_seconds() / 60, 1)
        queued.route_node_ids = route.node_ids
        cursor_time = queued.eta_at + WORK_DURATION
        cursor_stand = queued.stand_node_id

    if working is not None or queue:
        employee.busy_until = cursor_time
    return reachable


def advance_after_close(db, employee, closed_call, was_working, park_at_stand=True):
    """
    Продвигает очередь после закрытия вызова.

    Закрыт текущий вызов и в очереди что-то есть — первый вызов очереди
    становится текущим и сразу уходит инженеру в телефон; машина остаётся
    при нём. Очередь пуста — сотрудник освобождается, машина остаётся
    у борта. Закрыт (отменён) вызов из очереди — сотрудник по-прежнему
    занят, пересчитывается только цепочка.

    park_at_stand=False нужен при снятии назначения: инженер до борта
    не доехал, и оставлять машину у этой стоянки нельзя — она остаётся
    там, где он находится фактически.
    """
    db.flush()

    if not was_working or working_call_of(db, employee) is not None:
        replan_queue(db, employee)
        return

    if closed_call.vehicle and not closed_call.vehicle.employee and closed_call.vehicle.status == VEHICLE_RESERVED:
        closed_call.vehicle.status = VEHICLE_FREE
    graph = get_graph(employee.airport_icao)
    queue = queued_calls_of(db, employee)
    if not queue:
        employee.return_vehicle = bool(employee.vehicle and employee.vehicle.status == VEHICLE_IN_USE)
        if not employee.return_vehicle:
            release_vehicle(employee)
        employee.return_to_waiting = False
        employee.status = STATUS_FREE
        employee.busy_until = None
        return

    next_call = queue[0]
    # Сначала от фактических координат, без них — от стоянки только что
    # закрытого вызова: инженер там и заканчивал работу.
    next_call.vehicle = employee.vehicle
    plan = assigned_route(next_call, graph)
    if plan is not None:
        update_assigned_route(next_call, plan)
    else:
        route = travel_between(graph, employee, closed_call.stand_node_id, next_call.stand_node_id)
        if route is not None:
            next_call.eta_minutes = round(route.minutes, 1)
            next_call.eta_at = utc_now() + timedelta(minutes=route.minutes)
            next_call.route_node_ids = route.node_ids

    next_call.status = CALL_ASSIGNED
    employee.status = STATUS_ASSIGNED
    replan_queue(db, employee)


def unassign(db, call, reason):
    """
    Снятие исполнителя с вызова: вызов возвращается к подбору.

    Отличие от закрытия принципиальное: работа не выполнена, поэтому вызов
    не уходит в историю, а снова становится неназначенным и его можно
    отдать другому. Сотрудник освобождается (или берётся за следующий
    вызов очереди), зарезервированная под него машина возвращается в парк.

    Расчёт прошлого назначения стирается: маршрут, время прибытия и машина
    относились к снятому исполнителю, и показывать их рядом с новым
    означало бы врать диспетчеру.
    """
    employee = call.assigned_employee
    was_working = call.status in WORKING_CALL_STATUSES
    # Машину оставляем у борта, только если инженер туда доехал.
    park_at_stand = call.status == CALL_ARRIVED

    # Через связь, а не через внешний ключ: сессия не перечитывает объекты
    # после коммита, и previous_employee остался бы пустым в ответе.
    call.near_aircraft_since = call.last_aircraft_fix = None
    call.mel_category = None
    call.missing_tools = False
    call.allow_previous_employee = False
    call.previous_employee = employee
    call.unassign_reason = reason
    call.unassigned_at = utc_now()

    if call.vehicle and not call.vehicle.employee and call.vehicle.status == VEHICLE_RESERVED:
        call.vehicle.status = VEHICLE_FREE
    call.assigned_employee = None
    call.vehicle = None
    call.pickup_node_id = None
    call.route_node_ids = None
    call.route_stops = None
    call.eta_minutes = None
    call.eta_at = None
    call.queued_at = None
    call.override_reason = None

    # Прошлое предложение тоже стирается: оно указывало на снятого, и без
    # сброса назначение любого другого выглядело бы как переопределение
    # решения системы — с требованием причины и роли начальника смены.
    # Вызов возвращается в состояние «подбор не проводился».
    call.suggested_employee_id = None
    call.status = CALL_NEW

    if employee is not None:
        advance_after_close(db, employee, call, was_working, park_at_stand)


def mark_arrived(db, employee, call, at=None):
    """
    Прибытие подтверждено: фактическое время вместо расчётного.

    Если инженер ехал на зарезервированной машине, с этого момента она
    при нём: доехал до борта — значит, забрал. По фактическому времени
    пересчитывается очередь. Возвращает True, если это первое прибытие.
    """
    first = call.first_arrived_at is None
    if first:
        # Только первое прибытие — «прибытие по регламенту». Возвращение
        # к борту после переключения на другой вызов срок не переписывает.
        # at — когда инженер нажал «прибыл», если нажатие дошло позже:
        # телефон был без связи, а срок исполнен в момент нажатия.
        call.eta_at = at or utc_now()
        call.first_arrived_at = call.eta_at
    if call.work_stage is None:
        call.work_stage = STAGE_INSPECTION
    vehicle = employee.vehicle
    if vehicle is not None and vehicle.status == VEHICLE_RESERVED:
        vehicle.status = VEHICLE_IN_USE
    replan_queue(db, employee)
    return first


def can_switch_from(call):
    """
    Можно ли переключить исполнителя с этого вызова на другой — и почему.

    Возвращает пару (можно, объяснение). Правило экспертов: начатый ремонт
    не прерывают — вскрытый агрегат нельзя бросить, — а пока инженер ждёт
    запчасти, он простаивает, и его время можно отдать срочному борту.
    """
    if call.status != CALL_ARRIVED:
        return False, "в пути к борту"
    if call.work_stage == STAGE_WAITING_PARTS:
        return True, "ждёт запчасти — можно переключить"
    if call.work_stage == STAGE_REPAIR:
        return False, "ремонт начат — прерывать нельзя"
    return False, "ведёт осмотр — диагноз ещё не поставлен"


def switch_to(db, working, call, employee, plan):
    """
    Переключает исполнителя с ожидания запчастей на другой вызов.

    Прежний вызов не закрывается и не теряет исполнителя: он встаёт
    в его же очередь и снова станет текущим, когда новый будет закрыт.
    Этап работ и первое прибытие по нему сохраняются.
    """
    working.status = CALL_QUEUED
    working.queued_at = utc_now()
    db.flush()
    assign_now(db, call, employee, plan, None)


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


def record_position(employee, lat, lon, source, fix_age_s=None, now=None):
    """
    Новые координаты сотрудника.

    Время измерения для GPS — момент приёма минус возраст фиксации, который
    сообщил телефон: повторная отправка старой точки при живой связи не должна
    делать её свежей. Точку, поставленную вручную, считаем измеренной сейчас.
    Отдельно запоминается, когда телефон выходил на связь, — это позволяет
    отличить «потерян GPS» от «пропала связь».
    """
    now = now or utc_now()
    age = timedelta(seconds=fix_age_s) if source == LOCATION_GPS and fix_age_s else timedelta(0)

    if source == LOCATION_GPS and employee.location_source == LOCATION_GPS and employee.location_at:
        seconds = (now - age - employee.location_at).total_seconds()
        if seconds <= 0:
            employee.seen_at = now
            return False
        if 2 <= seconds <= 90 and employee.lat is not None:
            speed = haversine_m(employee.lat, employee.lon, lat, lon) * 3.6 / seconds
            if employee.has_vehicle and 1 <= speed <= 80:
                employee.vehicle.speed_kmh = speed
            elif not employee.has_vehicle and 1 <= speed <= 12:
                employee.speed_kmh = speed
    employee.lat = lat
    employee.lon = lon
    employee.location_source = source
    employee.location_at = now - age
    employee.seen_at = now
    employee.updated_at = now

    pick_up_if_near(employee)
    return True


def return_destination(employee):
    if employee.return_vehicle and employee.vehicle:
        return employee.vehicle.lat, employee.vehicle.lon, MODE_VEHICLE, "Верните спецтранспорт на исходную стоянку"
    return None


def navigation_leg(employee):
    target = return_destination(employee)
    if not target or employee.lat is None:
        return None
    lat, lon, mode, instruction = target
    graph = get_graph(employee.airport_icao)
    node, _ = graph.nearest_node(lat, lon, mode)
    route = route_from_point(graph, employee.lat, employee.lon, node["id"], mode) if node else None
    points = [{"lat": employee.lat, "lon": employee.lon}]
    if not route:
        return {"instruction": instruction + " — нет доступного маршрута", "route_points": []}
    points.extend(graph.node(n) for n in route.node_ids)
    points.append({"lat": lat, "lon": lon})
    return {"instruction": instruction, "route_points": points}


def route_geometry(call, graph):
    nodes = [node for key in call.route_node_ids or [] if (node := graph.node(key))]
    if nodes and call.route_start_lat is not None and call.route_start_lon is not None:
        start = {"id": None, "lat": call.route_start_lat, "lon": call.route_start_lon}
        if haversine_m(start["lat"], start["lon"], nodes[0]["lat"], nodes[0]["lon"]) > 0.1:
            nodes.insert(0, start)
    return nodes


def check_aircraft_arrival(db, employee, accuracy_m=None):
    from services import mentor
    from constants import STATUS_BUSY
    call = working_call_of(db, employee)
    if not call or call.status != "accepted":
        return
    point = get_graph(call.airport_icao).node(call.stand_node_id)
    if call.aircraft and call.aircraft.lat is not None and call.aircraft.lon is not None:
        point = {"lat": call.aircraft.lat, "lon": call.aircraft.lon}
    at = employee.location_at
    if (not point or not at or (utc_now() - at).total_seconds() > 15 or employee.return_vehicle
            or (accuracy_m is not None and accuracy_m > WAYPOINT_DONE_RADIUS_M)
            or any(not w.get("done_at") for w in call.waypoints or [])
            or (call.vehicle and call.vehicle.status == VEHICLE_RESERVED)
            or haversine_m(employee.lat, employee.lon, point["lat"], point["lon"]) > WAYPOINT_DONE_RADIUS_M):
        call.near_aircraft_since = call.last_aircraft_fix = None
        return
    if not call.last_aircraft_fix or (at - call.last_aircraft_fix).total_seconds() > 15:
        call.near_aircraft_since = at
    call.last_aircraft_fix = at
    if (at - call.near_aircraft_since).total_seconds() >= 10:
        call.status = CALL_ARRIVED
        employee.status = STATUS_BUSY
        if mark_arrived(db, employee, call, at):
            mentor.review_arrival(db, call, employee, at)


def refresh_navigation(db, employee):
    from algorithm.geo import distance_to_segment
    target = return_destination(employee)
    call = working_call_of(db, employee)
    if target:
        if haversine_m(employee.lat, employee.lon, *target[:2]) > PICKUP_RADIUS_M:
            return
        if employee.return_vehicle and employee.vehicle:
            old = employee.vehicle
            old.status = VEHICLE_FREE
            old.employee = None
            employee.return_vehicle = False
            if call and call.vehicle and call.vehicle.status == VEHICLE_RESERVED and not call.vehicle.employee:
                call.vehicle.employee = employee
    if not call or call.status == CALL_ARRIVED:
        return
    graph = get_graph(employee.airport_icao)
    # Дошёл до промежуточной точки — она пройдена, маршрут дальше без неё.
    if check_waypoints_reached(call, employee, graph):
        plan = assigned_route(call, graph)
        if plan:
            update_assigned_route(call, plan, reset_eta=False)
        return
    nodes = route_geometry(call, graph)
    segments = zip(nodes, nodes[1:] or nodes)
    if target or not any(distance_to_segment(employee.lat, employee.lon, a, b) <= ROUTE_DEVIATION_M
                         for a, b in segments):
        plan = assigned_route(call, graph)
        if plan:
            update_assigned_route(call, plan, reset_eta=False)


def pick_up_if_near(employee):
    """
    Инженер дошёл до зарезервированной машины — значит, сел в неё.

    Раньше машина числилась «ждущей» до самого прибытия к борту. Пока
    по ней ничего не считалось, это было безвредно, но живой прогноз
    прибытия заставлял бы человека, уже едущего на машине, мысленно
    возвращаться к месту её стоянки и пугал бы диспетчера ложным
    «не успевает».
    """
    vehicle = employee.vehicle
    if vehicle is None or vehicle.status != VEHICLE_RESERVED:
        return
    if None in (employee.lat, employee.lon, vehicle.lat, vehicle.lon):
        return
    if haversine_m(employee.lat, employee.lon, vehicle.lat, vehicle.lon) <= PICKUP_RADIUS_M:
        vehicle.status = VEHICLE_IN_USE
