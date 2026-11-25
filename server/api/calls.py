"""
Вызовы: регистрация, подбор исполнителя, назначение, смена статуса.

Главный рабочий цикл системы. Подбор вынесен отдельной ручкой, а не
выполняется при создании вызова: диспетчер должен увидеть предложение
и основания к нему до того, как кого-то назначит.
"""

import time

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from algorithm.dispatch import suggest
from algorithm.qualification import find_qualification, parse_mark, required_mark_for_category
from auth.dependencies import (
    Context,
    check_same_airport,
    require_airport,
    require_roles,
)
from clock import to_iso_utc, utc_now, utc_today
from constants import DEFECT_TYPES, TRAVEL_MODES, TRAVEL_WALK, TRAVEL_WITH_LEAD, WORK_STAGES
from database import get_db, get_dispatch_db
from graph_registry import get_graph
from models.aircraft import Aircraft
from models.call import ACTIVE_CALL_STATUSES, CALL_STATUSES, WORKING_CALL_STATUSES, Call
from models.call import STATUS_ARRIVED as CALL_ARRIVED
from models.call import STATUS_CANCELLED as CALL_CANCELLED
from models.call import STATUS_CLOSED as CALL_CLOSED
from models.call import STATUS_NEW as CALL_NEW
from models.call import STATUS_QUEUED as CALL_QUEUED
from models.call import STATUS_SUGGESTED as CALL_SUGGESTED
from models.employee import Employee
from models.vehicle import Vehicle
from constants import VEHICLE_FREE, VEHICLE_RESERVED, VEHICLE_IN_USE
from services.assignment import (
    assigned_route,
    depots_for,
    release_vehicle,
    remaining_waypoints,
    update_assigned_route,
    waypoint_records,
)
from models.user import DISPATCH_ROLES, ROLE_DISPATCHER, ROLE_SHIFT_SUPERVISOR, role_at_least
from schemas.call import (
    AssignRequest,
    CallCreateRequest,
    CallResponse,
    CallStatusRequest,
    ClaimRequest,
    SuggestResponse,
    UnassignRequest,
    MessageRequest,
    ProblemRequest,
    ReadRequest,
    UrgencyRequest,
    TravelPreviewRequest,
    WaypointPlanRequest,
    WaypointsRequest,
)
from services import claim as claims
from services import alternatives, journal, mentor, waypoint_plan
from services.assignment import (
    advance_after_close,
    assign_now,
    enqueue,
    free_vehicles,
    can_switch_from,
    is_busy,
    mark_arrived,
    plan_route,
    plan_with,
    switch_to,
    working_call_of,
    unassign,
)
from models.call_message import AUTHOR_DISPATCHER
from services import support
from services.deadline import assess
from services.defects import classify as classify_defect
from services.defects import find as find_defect

router = APIRouter(prefix="/api/calls", tags=["Вызовы"])

ERROR_CALL_NOT_FOUND = "Вызов не найден"
ERROR_AIRCRAFT_NOT_FOUND = "Борт не найден"
ERROR_EMPLOYEE_NOT_FOUND = "Сотрудник не найден"
ERROR_UNKNOWN_DEFECT = (
    "Тип неисправности {code} не найден в справочнике. Найдите подходящий "
    "или заведите новый"
)
ERROR_BAD_STATUS = "Недопустимый статус вызова: {status}"
ERROR_OVERRIDE_NEEDS_REASON = (
    "Назначен не тот сотрудник, которого предложила система. "
    "Укажите причину переопределения (override_reason)"
)
ERROR_OVERRIDE_FORBIDDEN = (
    "Переопределять решение системы вправе только начальник смены"
)
ERROR_NO_ROUTE = "Для выбранного сотрудника не удалось построить маршрут"
ERROR_ALREADY_ASSIGNED = "Вызов уже назначен. Повторное назначение не допускается"
ERROR_NO_PERMIT = "Назначение недопустимо: {reason}"
ERROR_QUEUE_FORBIDDEN = (
    "Сотрудник занят. Ставить вызов в очередь к занятому вправе "
    "только начальник смены"
)
ERROR_QUEUE_NEEDS_REASON = (
    "Сотрудник занят, вызов встанет в очередь и регламент, скорее всего, "
    "будет нарушен. Укажите причину (override_reason)"
)
ERROR_QUEUED_BY_ASSIGN_ONLY = (
    "В очередь вызов ставится только назначением на занятого сотрудника"
)
ERROR_QUEUE_PROMOTES_ITSELF = (
    "Вызов в очереди становится текущим сам, когда исполнитель закроет "
    "предыдущий вызов"
)
ERROR_NOT_SWITCHABLE = "Переключить нельзя: {name} — {note}"
ERROR_SUPPORT_NEEDS_MAIN = (
    "Второго инженера добавляют к вызову, у которого уже есть исполнитель"
)
ERROR_ALREADY_ON_CALL = "{name} уже назначен на этот вызов"
ERROR_BAD_TRAVEL = "Способ передвижения должен быть одним из: {modes}"
ERROR_NO_LEAD_VEHICLE = "У ответственного инженера нет машины — ехать с ним не на чем"
ERROR_LEAD_ONLY_FOR_SUPPORT = "Ехать с ответственным может только второй инженер"
ERROR_NOT_ASSIGNED = "У вызова нет исполнителя: снимать нечего"
ERROR_CLOSED_CALL = "Вызов уже в истории. Изменить назначение или статус нельзя"
ERROR_UNASSIGN_NEEDS_REASON = (
    "Снятие исполнителя меняет ход обслуживания борта. Укажите причину (reason)"
)
ERROR_STARTED = "Работы начаты: снять исполнителя или отменить вызов вправе только начальник смены"
ERROR_STATUS_BY_ACTION = (
    "Статус меняют назначение и действия инженера. Диспетчер может только отменить вызов"
)
ERROR_UNASSIGNED_REASON = "снят с этого вызова: {reason}"

# Кто вправе назначить не того, кого предложила система, и поставить вызов
# в очередь к занятому. Обычный диспетчер такого права не имеет — это прямо
# следует из разграничения ролей в ТЗ.
def can_override(context):
    """Переопределять решение системы вправе начальник смены и старше."""
    return role_at_least(context.user.role, ROLE_SHIFT_SUPERVISOR)


def ensure_may_remove(context, *calls):
    """Снять исполнителя после начала работ вправе только начальник смены."""
    started = any(
        item.status == CALL_ARRIVED or (item.status == CALL_QUEUED and item.first_arrived_at is not None)
        for item in calls
    )
    if started and not can_override(context):
        raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_STARTED)


# Вызов можно назначить, пока им никто не занят.
ASSIGNABLE_STATUSES = (CALL_NEW, CALL_SUGGESTED)


def load_call(db, context, call_id):
    """Вызов по идентификатору с проверкой аэропорта сессии."""
    call = db.get(Call, call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_CALL_NOT_FOUND)
    check_same_airport(context, call.airport_icao)
    return call


def stand_ref_of(call):
    """Номер стоянки вызова по графу аэропорта."""
    node = get_graph(call.airport_icao).node(call.stand_node_id)
    return node["ref"] if node else None


def to_response(call, db=None, now=None):
    """
    Модель вызова в схему ответа, с подстановкой данных борта.

    now передаётся при выдаче списка: все вызовы оцениваются на один
    и тот же момент, иначе соседние строки расходились бы на миллисекунды.
    """
    aircraft = call.aircraft
    assigned = call.assigned_employee
    previous = call.previous_employee
    defect = defect_summary(call)

    return CallResponse(
        id=call.id,
        airport_icao=call.airport_icao,
        aircraft_id=call.aircraft_id,
        board_number=aircraft.board_number if aircraft else "—",
        aircraft_type=aircraft.aircraft_type if aircraft else "—",
        stand_node_id=call.stand_node_id,
        # Борт вне стоянки: номера нет — вершина сети, к которой он
        # привязан для маршрута, могла бы оказаться чужой стоянкой.
        stand_ref=None if aircraft and aircraft.lat is not None else stand_ref_of(call),
        defect_code=call.defect_code,
        defect_name=defect["name"],
        defect_ata=defect["ata"],
        defect_category=defect["category"],
        defect_description=call.defect_description,
        required_mark=call.required_mark,
        status=call.status,
        created_at=to_iso_utc(call.created_at),
        closed_at=to_iso_utc(call.closed_at),
        reported_at=to_iso_utc(call.reported_at),
        source=call.source,
        urgent=bool(call.urgent),
        mel_category=call.mel_category,
        missing_tools=call.missing_tools,
        aog=bool(call.aog),
        work_stage=call.work_stage,
        work_stage_title=WORK_STAGES.get(call.work_stage),
        suggested_employee_id=call.suggested_employee_id,
        assigned_employee_id=call.assigned_employee_id,
        assigned_employee_name=assigned.full_name if assigned else None,
        override_reason=call.override_reason,
        eta_minutes=call.eta_minutes,
        route_node_ids=call.route_node_ids,
        eta_at=to_iso_utc(call.eta_at),
        queued_at=to_iso_utc(call.queued_at),
        vehicle_call_sign=call.vehicle.call_sign if call.vehicle else None,
        pickup_node_id=call.pickup_node_id,
        needs_tools=bool(call.needs_tools),
        tools_taken_at=to_iso_utc(call.tools_taken_at),
        route_stops=call.route_stops or [],
        waypoints=call.waypoints or [],
        previous_employee_name=previous.full_name if previous else None,
        unassign_reason=call.unassign_reason,
        unassigned_at=to_iso_utc(call.unassigned_at),
        control=assess(call, now).as_dict(),
        parent_call_id=call.parent_call_id,
        support=support.support_summary(call) if call.parent_call_id is None else [],
        messages=support.messages_of(call),
        read_message_id=support.root_of(call).read_message_id or 0,
        **claims.summary(call, now),
    )


def defect_summary(call):
    """
    Название, глава и категория неисправности вызова.

    Тип берётся из справочника в базе. Если записи там нет (вызов старше
    справочника, база не досоздана), выручает встроенное описание из
    constants.py, а в крайнем случае — сам код: вызов без названия
    неисправности лучше, чем ошибка при выводе списка вызовов.
    """
    if call.defect_type is not None:
        return {
            "name": call.defect_type.name,
            "ata": call.defect_type.ata,
            "category": call.defect_type.category,
        }
    builtin = DEFECT_TYPES.get(call.defect_code, {})
    return {
        "name": builtin.get("name", call.defect_code),
        "ata": builtin.get("ata"),
        "category": builtin.get("category"),
    }


def shift_employees(db, context):
    """Сотрудники аэропорта сессии в виде словарей для алгоритма."""
    employees = db.query(Employee).filter_by(
        archived=False, airport_icao=context.airport_icao,
    ).all()
    return [employee.as_algorithm_dict() for employee in employees]


def candidates_for(db, context, call):
    """
    Кто участвует в подборе по этому вызову.

    Снятый с него исполнитель исключается: диспетчер уже решил, что этот
    сотрудник сюда не поедет, и предлагать его снова означало бы спорить
    с человеком, который видит обстановку. В отказах он остаётся —
    с причиной снятия, чтобы решение можно было пересмотреть.
    """
    employees = shift_employees(db, context)
    if call.previous_employee_id is None or call.allow_previous_employee:
        return employees, None

    removed = None
    remaining = []
    for employee in employees:
        if employee["id"] == call.previous_employee_id:
            removed = employee
        else:
            remaining.append(employee)
    return remaining, removed


def run_suggestion(db, context, call, purpose=None):
    """
    Запускает подбор по вызову и возвращает результат алгоритма.

    В подбор уходят только свободные машины парка: зарезервированную
    за другим вызовом или едущую с другим инженером предложить нельзя.

    purpose — зачем подбор; с ним список кандидатов попадает в журнал
    событий вместе с затраченным временем. Без него (план ИИ-диспетчера,
    который опрашивается каждые несколько секунд) — не попадает.
    """
    started = time.perf_counter()
    graph = get_graph(call.airport_icao)
    aircraft = call.aircraft
    payload = {
        "aircraft_type": aircraft.aircraft_type,
        "defect_code": call.defect_code,
        "stand_node_id": call.stand_node_id,
        # Отметка, записанная при регистрации: для типа, заведённого
        # диспетчером, во встроенном справочнике её не вычислить.
        "required_mark": call.required_mark,
        # Инструмент со склада — маршрут через склад, пока не получен.
        "needs_tools": bool(call.needs_tools) and call.tools_taken_at is None,
        # Промежуточные точки, ещё не пройденные.
        "waypoints": remaining_waypoints(call),
    }
    vehicles = [vehicle.as_algorithm_dict() for vehicle in free_vehicles(db, call.airport_icao)]
    employees, removed = candidates_for(db, context, call)
    result = suggest(graph, payload, employees, context.shift, vehicles=vehicles)

    if removed is not None:
        result.rejected.append(
            {
                "employee_id": removed["id"],
                "full_name": removed["full_name"],
                "reason": ERROR_UNASSIGNED_REASON.format(
                    reason=call.unassign_reason or "без указания причины"
                ),
            }
        )
    for entry in result.busy:
        describe_busy(db, entry)
    if purpose:
        journal.candidates(
            db, call, result, (time.perf_counter() - started) * 1000, len(employees), purpose
        )
    return result


def describe_busy(db, entry):
    """
    Дополняет запись о занятом: на каком он борте и можно ли его переключить.

    Диспетчер, выбирая из занятых, должен видеть не только «освободится
    в 14:40», но и чем человек занят: того, кто ждёт запчасти, можно
    переключить без вреда, а того, кто вскрыл агрегат, — нельзя.
    """
    employee = db.get(Employee, entry["employee_id"])
    working = working_call_of(db, employee) if employee is not None else None
    if working is None:
        return
    switchable, note = can_switch_from(working)
    entry["current_board"] = mentor.board_of(working)
    entry["work_stage_title"] = WORK_STAGES.get(working.work_stage)
    entry["switchable"] = switchable
    entry["switch_note"] = note


def check_permit(call, employee):
    """
    Проверка допуска при назначении.

    Переопределение решения системы позволяет выбрать другого подходящего
    сотрудника, а не любого: назначение без действующей отметки на этот
    тип ВС юридически недопустимо (§3), какой бы ни была причина. Интерфейс
    и так предлагает только сотрудников с допуском — проверка здесь
    закрывает прямой запрос к API в обход интерфейса.
    """
    qualification, reason = find_qualification(
        employee.as_algorithm_dict(),
        parse_mark(call.required_mark),
        call.aircraft.aircraft_type,
        utc_today(),
    )
    if qualification is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_NO_PERMIT.format(reason=reason)
        )


@router.get("", response_model=list[CallResponse], summary="Активные вызовы")
def list_calls(
    include_closed: bool = False,
    context: Context = Depends(require_airport),
    db: Session = Depends(get_db),
):
    """Вызовы текущего аэропорта, свежие первыми."""
    query = db.query(Call).filter(Call.airport_icao == context.airport_icao)
    if not include_closed:
        query = query.filter(Call.status.in_(ACTIVE_CALL_STATUSES))

    # Второй ключ — идентификатор: вызовы, созданные в одну миллисекунду,
    # иначе шли бы в порядке, зависящем от разрешения системных часов.
    # Сначала AOG и срочные: при нескольких вызовах сразу диспетчер должен
    # видеть главное наверху, а не искать его в списке по времени.
    calls = query.order_by(
        Call.aog.desc(), Call.urgent.desc(), Call.created_at.desc(), Call.id.desc()
    ).all()
    now = utc_now()
    responses = [to_response(call, db, now) for call in calls]
    if role_at_least(context.user.role, ROLE_DISPATCHER):
        # Вариант решения — тем, кто вправе его принять.
        for call, response in zip(calls, responses):
            control = assess(call, now)
            if control.level in alternatives.LEVELS:
                try:
                    response.control.alternative = alternatives.cached(db, call, control, context.shift)
                except Exception as error:  # список вызовов важнее подсказки к одному из них
                    print(f"Вариант решения по вызову {call.id} не посчитан — {error!r}")
    return responses


@router.post(
    "",
    response_model=CallResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Зарегистрировать вызов",
)
def create_call(
    payload: CallCreateRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
):
    """
    Регистрация вызова: борт, тип неисправности и описание экипажа.

    Стоянка и требуемая отметка не передаются клиентом, а выводятся на
    сервере: стоянка известна из карточки борта, отметка — из категории
    типа неисправности и типа ВС. Клиент не должен иметь возможности задать
    их сам, иначе появится способ обойти проверку допуска.

    Тип ищется в открытом справочнике. Если подходящего нет, клиент сначала
    заводит его (POST /api/defect-types), а затем регистрирует вызов.
    """
    if payload.defect_code:
        defect = find_defect(db, payload.defect_code)
        if defect is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ERROR_UNKNOWN_DEFECT.format(code=payload.defect_code),
            )
    else:
        # Тип не выбран — по словам экипажа. Описание обязательно: без него
        # инженеру нечего читать, а системе не по чему подбирать допуск.
        if not (payload.description or "").strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Опишите неисправность или выберите её тип",
            )
        defect = classify_defect(db, payload.description)

    aircraft = db.get(Aircraft, payload.aircraft_id)
    if aircraft is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_AIRCRAFT_NOT_FOUND)
    check_same_airport(context, aircraft.airport_icao)

    call = new_call(db, aircraft, defect, payload.description)
    call.urgent = payload.urgent
    call.needs_tools = payload.needs_tools
    call.waypoints = waypoint_records(get_graph(call.airport_icao), payload.waypoints)
    # Кто зарегистрировал вызов, тот его и разбирает: он открывается у него
    # сразу, и второй диспетчер не должен перехватить его на полпути.
    claims.take(db, call, context.user)
    db.commit()
    return to_response(call, db)


def new_call(db, aircraft, defect, description, reported_at=None, source=None):
    """
    Создаёт вызов — общий путь для диспетчера и для входящего сообщения.

    Стоянка и требуемая отметка выводятся здесь же, из карточки борта
    и категории неисправности. reported_at передаётся, когда вызов создан
    из сообщения экипажа: регламент отсчитывается от него.
    """
    call = Call(
        airport_icao=aircraft.airport_icao,
        aircraft_id=aircraft.id,
        stand_node_id=aircraft.stand_node_id,
        defect_code=defect.code,
        defect_description=" ".join((description or "").split()) or None,
        required_mark=str(required_mark_for_category(defect.category, aircraft.aircraft_type)),
        status=CALL_NEW,
        reported_at=reported_at,
        source=source,
    )
    db.add(call)
    db.flush()
    return call


@router.post(
    "/{call_id}/suggest", response_model=SuggestResponse, summary="Подобрать исполнителя"
)
def suggest_employee(
    call_id: int,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
):
    """
    Подбор кандидата с маршрутом, временем прибытия и обоснованием.

    Предложение системы запоминается в вызове: без этого потом нельзя
    будет отличить согласие диспетчера с системой от переопределения.
    """
    call = load_call(db, context, call_id)
    result = run_suggestion(db, context, call, purpose="подбор исполнителя по заявке")

    # Вызов разбирает другой диспетчер: подбор можно посмотреть, но
    # записывать предложение в чужой вызов нельзя. Журнал — пишется.
    if claims.held_by_other(call, context.user):
        db.commit()
        return SuggestResponse(**result.as_dict())

    # Предложение фиксируется только пока вызов не назначен. После
    # назначения поле хранит историю: кого система предлагала в момент
    # решения. Повторный подбор (диспетчер просто открыл вызов в списке)
    # переписал бы её, и назначение задним числом стало бы выглядеть
    # как переопределение — или наоборот.
    if call.status in ASSIGNABLE_STATUSES:
        claims.ensure_can_change(db, call, context.user)
        call.suggested_employee_id = result.best.employee["id"] if result.best else None
        if call.status == CALL_NEW:
            call.status = CALL_SUGGESTED
    db.commit()

    return SuggestResponse(**result.as_dict())


@router.post("/{call_id}/assign", response_model=CallResponse, summary="Назначить")
def assign_employee(
    call_id: int,
    payload: AssignRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
):
    """
    Назначение исполнителя на вызов.

    Свободный сотрудник выезжает сразу; если по расчёту ему быстрее дойти
    до свободной машины парка, она резервируется за ним. Занятый — только
    по решению начальника смены с причиной: вызов встаёт к нему в очередь
    и станет текущим, когда тот закончит предыдущие работы.

    Назначить можно и не того, кого предложила система, — но только
    начальнику смены, только с причиной и только сотрудника с допуском.
    """
    call = load_call(db, context, call_id)
    if call.status not in ASSIGNABLE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_ALREADY_ASSIGNED)
    claims.ensure_can_change(db, call, context.user)
    perform_assign(db, context, call, payload)
    # Назначенный вызов больше никто не разбирает.
    claims.clear(call)
    db.commit()
    return to_response(call, db)


def perform_assign(db, context, call, payload, free_choice=False, lead=None):
    """
    Назначает исполнителя на вызов; коммит остаётся за вызывающим.

    free_choice — для второго инженера: предложение системы на него не
    распространяется, поэтому выбор диспетчера не считается
    переопределением и причины не требует. lead — основной вызов группы:
    его машина нужна, если второй едет вместе с ответственным.
    """
    if payload.travel not in TRAVEL_MODES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ERROR_BAD_TRAVEL.format(modes=", ".join(TRAVEL_MODES)),
        )
    ride_vehicle = None
    if payload.travel == TRAVEL_WITH_LEAD:
        if lead is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_LEAD_ONLY_FOR_SUPPORT)
        ride_vehicle = lead.vehicle
        if ride_vehicle is None:
            raise HTTPException(status.HTTP_409_CONFLICT, ERROR_NO_LEAD_VEHICLE)
    employee = db.get(Employee, payload.employee_id)
    if employee is None or employee.archived:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_EMPLOYEE_NOT_FOUND)
    check_same_airport(context, employee.airport_icao)
    check_permit(call, employee)

    # Подбор выполняется перед каждым назначением. Предложение системы
    # должно существовать — иначе переопределение не отличить от обычного
    # назначения, — а свежий результат нужен Наставнику: по нему видно,
    # кого система предлагала в эту минуту и сколько стоит отступление.
    result = run_suggestion(db, context, call, purpose="проверка перед назначением")
    if free_choice:
        call.suggested_employee_id = employee.id
    elif call.suggested_employee_id is None:
        call.suggested_employee_id = result.best.employee["id"] if result.best else None

    # Занятость проверяется по фактическому состоянию, а не по предложению:
    # предложение могло устареть, пока этого сотрудника назначали на другой
    # борт. Именно так раньше получался второй «текущий» вызов у одного
    # инженера, и первый из них терялся.
    if is_busy(db, employee):
        if not can_override(context):
            raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_QUEUE_FORBIDDEN)
        if not payload.override_reason:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_QUEUE_NEEDS_REASON
            )
        call.override_reason = payload.override_reason
        if payload.switch:
            switch_employee(db, call, employee, result, context.user)
        else:
            enqueue(db, call, employee)
            mentor.review_queue(db, call, employee, result, context.user)
        return

    is_override = call.suggested_employee_id != employee.id
    if is_override:
        if not payload.override_reason:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_OVERRIDE_NEEDS_REASON
            )
        if not can_override(context):
            raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_OVERRIDE_FORBIDDEN)

    graph = get_graph(call.airport_icao)
    plan, pickup_vehicle = plan_route(
        db,
        employee,
        call.stand_node_id,
        graph,
        payload.travel,
        ride_vehicle,
        depots_for(call, graph),
        remaining_waypoints(call),
    )
    if plan is None:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_NO_ROUTE)

    call.override_reason = payload.override_reason if is_override else None
    assign_now(db, call, employee, plan, pickup_vehicle)
    if payload.travel == TRAVEL_WALK:
        # Своя машина, если она есть, остаётся там, где стоит: в этот
        # вызов она не входит, и на карточке её быть не должно.
        call.vehicle = None
        if employee.vehicle and employee.vehicle.status == VEHICLE_IN_USE:
            employee.return_vehicle = True
            # Сначала отвести машину на место: путь и время — с учётом этого.
            back = assigned_route(call, graph)
            if back is not None:
                update_assigned_route(call, back)
        else:
            release_vehicle(employee)
    elif ride_vehicle is not None:
        # Машина остаётся за ответственным: он её забирает и отвечает за
        # неё; второй едет пассажиром, поэтому за ним она не числится.
        call.vehicle = ride_vehicle
    mentor.review_assignment(db, call, employee, result, context.user, is_override)


@router.post("/{call_id}/route-preview", summary="Путь при выбранном способе поездки")
def route_preview(
    call_id: int,
    payload: TravelPreviewRequest,
    db: Session = Depends(get_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
):
    """
    Маршрут и время для выбора «пешком / на машине / с первым» до того,
    как диспетчер его подтвердил. Ничего не меняет.
    """
    call = load_call(db, context, call_id)
    graph = get_graph(call.airport_icao)
    try:
        plan = preview_plan(db, call, graph, payload)
        if plan is None:
            raise HTTPException(status.HTTP_409_CONFLICT, ERROR_NO_ROUTE)
        return plan.as_dict()
    finally:
        db.rollback()


def preview_plan(db, call, graph, payload):
    """Тот же расчёт, что и при назначении или смене транспорта."""
    if payload.employee_id is None:
        employee = call.assigned_employee
        if employee is None:
            raise HTTPException(status.HTTP_409_CONFLICT, ERROR_NOT_ASSIGNED)
    else:
        employee = db.get(Employee, payload.employee_id)
        if employee is None or employee.airport_icao != call.airport_icao:
            raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_EMPLOYEE_NOT_FOUND)
    if employee.lat is None or employee.lon is None:
        return None

    if payload.employee_id is None:
        vehicle = db.get(Vehicle, payload.vehicle_id) if payload.vehicle_id else None
        if payload.vehicle_id and (vehicle is None or vehicle.airport_icao != call.airport_icao):
            raise HTTPException(status.HTTP_409_CONFLICT, "Машина недоступна")
        riding = vehicle is not None and vehicle is employee.vehicle and vehicle.status == VEHICLE_IN_USE
        return plan_with(employee, call, graph, vehicle, riding)

    if payload.travel not in TRAVEL_MODES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_BAD_TRAVEL.format(modes=", ".join(TRAVEL_MODES)))
    if payload.travel == TRAVEL_WALK:
        return plan_with(employee, call, graph, None, False)
    ride = None
    if payload.travel == TRAVEL_WITH_LEAD:
        ride = call.vehicle
        if ride is None and payload.lead_vehicle:
            ride = db.query(Vehicle).filter_by(
                airport_icao=call.airport_icao, call_sign=payload.lead_vehicle, archived=False).first()
        if ride is None:
            raise HTTPException(status.HTTP_409_CONFLICT, ERROR_NO_LEAD_VEHICLE)
    return plan_route(db, employee, call.stand_node_id, graph, payload.travel, ride,
                      depots_for(call, graph), remaining_waypoints(call))[0]


@router.post("/{call_id}/transport", response_model=CallResponse)
def change_transport(call_id: int, vehicle_id: int | None = None,
                     db: Session = Depends(get_dispatch_db),
                     context: Context = Depends(require_roles(*DISPATCH_ROLES))):
    call = load_call(db, context, call_id)
    claims.ensure_can_change(db, call, context.user)
    employee = call.assigned_employee
    if not employee or call.status not in ("assigned", "accepted"):
        raise HTTPException(409, "Транспорт меняют у инженера в пути")
    if call.vehicle_id == vehicle_id:
        return to_response(call, db)
    target = db.get(Vehicle, vehicle_id) if vehicle_id else None
    if vehicle_id is not None and (not target or target.archived or target.airport_icao != call.airport_icao
                                   or (target.status != VEHICLE_FREE and target != employee.vehicle)):
        raise HTTPException(409, "Машина недоступна")
    if call.vehicle and call.vehicle.status == VEHICLE_RESERVED and not call.vehicle.employee:
        call.vehicle.status = VEHICLE_FREE
    old = employee.vehicle
    employee.return_vehicle = bool(old and old != target and old.status == VEHICLE_IN_USE)
    if old and old != target and not employee.return_vehicle:
        release_vehicle(employee)
    call.vehicle = target
    call.require_vehicle_pickup = target is not None and target.status != VEHICLE_IN_USE
    if target and target != old:
        target.status = VEHICLE_RESERVED
        if not employee.return_vehicle:
            target.employee = employee
    plan = assigned_route(call, get_graph(call.airport_icao))
    if not plan:
        raise HTTPException(409, ERROR_NO_ROUTE)
    update_assigned_route(call, plan)
    db.commit()
    return to_response(call, db)


@router.post(
    "/{call_id}/support", response_model=CallResponse, summary="Добавить второго инженера"
)
def add_support(
    call_id: int,
    payload: AssignRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
):
    """
    Ещё один инженер на тот же вызов.

    Создаётся вызов поддержки — копия основного по борту, неисправности
    и времени отсчёта — и назначается обычным путём: свободный выезжает
    сразу своим способом (пешком или через машину парка), занятый встаёт
    в очередь по решению начальника смены. Если назначить не удалось,
    вызов поддержки не создаётся. Отвечает основным вызовом.
    """
    call = load_call(db, context, call_id)
    root = support.root_of(call)
    if root.status not in ACTIVE_CALL_STATUSES or root.assigned_employee_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_SUPPORT_NEEDS_MAIN)

    team = [root.assigned_employee_id] + [
        item.assigned_employee_id for item in support.support_calls(root)
    ]
    if payload.employee_id in team:
        employee = db.get(Employee, payload.employee_id)
        raise HTTPException(
            status.HTTP_409_CONFLICT, ERROR_ALREADY_ON_CALL.format(name=employee.full_name)
        )

    extra = Call(
        airport_icao=root.airport_icao,
        aircraft_id=root.aircraft_id,
        stand_node_id=root.stand_node_id,
        defect_code=root.defect_code,
        defect_description=root.defect_description,
        required_mark=root.required_mark,
        status=CALL_NEW,
        created_at=root.created_at,
        reported_at=root.reported_at,
        source=root.source,
        urgent=root.urgent,
        parent_call_id=root.id,
    )
    db.add(extra)
    db.flush()
    try:
        perform_assign(db, context, extra, payload, free_choice=True, lead=root)
    except HTTPException:
        db.rollback()
        raise
    db.commit()
    return to_response(root, db)


@router.post("/{call_id}/message", response_model=CallResponse, summary="Ответить инженеру")
def send_message(
    call_id: int,
    payload: MessageRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
):
    """Сообщение диспетчера инженерам вызова: его увидят в телефоне."""
    call = load_call(db, context, call_id)
    support.add_message(db, call, AUTHOR_DISPATCHER, context.user.full_name, payload.text)
    db.commit()
    return to_response(support.root_of(call), db)


@router.post("/{call_id}/problem", response_model=CallResponse, summary="Проблема исправлена или нет")
def set_problem(
    call_id: int,
    payload: ProblemRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
):
    call = load_call(db, context, call_id)
    if not support.set_problem(db, call, payload.message_id, payload.resolved, context.user.full_name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Проблема не найдена")
    db.commit()
    return to_response(support.root_of(call), db)


@router.post("/{call_id}/read", response_model=CallResponse, summary="Сообщения прочитаны")
def mark_read(
    call_id: int,
    payload: ReadRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
):
    call = load_call(db, context, call_id)
    if not support.mark_read(db, call, payload.message_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сообщение не найдено")
    db.commit()
    return to_response(support.root_of(call), db)


def switch_employee(db, call, employee, result, actor):
    """
    Переключение занятого исполнителя на этот вызов.

    Разрешено только с ожидания запчастей; правило проверяет сервер,
    чтобы запрос в обход интерфейса не мог прервать начатый ремонт.
    """
    working = working_call_of(db, employee)
    switchable, note = can_switch_from(working) if working is not None else (False, "занят")
    if not switchable:
        raise HTTPException(
            status.HTTP_409_CONFLICT, ERROR_NOT_SWITCHABLE.format(name=employee.full_name, note=note)
        )
    plan, _vehicle = plan_route(db, employee, call.stand_node_id, get_graph(call.airport_icao))
    if plan is None:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_NO_ROUTE)

    switch_to(db, working, call, employee, plan)
    mentor.review_switch(db, call, working, employee, result, actor)


@router.post("/{call_id}/urgency", response_model=CallResponse, summary="Срочность вызова")
def set_urgency(
    call_id: int,
    payload: UrgencyRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
):
    """Диспетчер отмечает вызов срочным или снимает отметку."""
    call = load_call(db, context, call_id)
    claims.ensure_can_change(db, call, context.user)
    call.urgent = payload.urgent
    db.commit()
    return to_response(call, db)


@router.post("/{call_id}/waypoints", response_model=CallResponse, summary="Промежуточные точки")
def set_waypoints(
    call_id: int,
    payload: WaypointsRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
):
    """
    Диспетчер задаёт промежуточные точки маршрута — по порядку.

    Склад — получить инструмент (30 с), стоянка спецтехники — пересадка
    на машину, любая другая точка — маршрут проходит через неё. Если
    исполнитель уже в пути, маршрут и прогноз пересчитываются сразу.
    """
    call = load_call(db, context, call_id)
    claims.ensure_can_change(db, call, context.user)
    if call.status in (CALL_CLOSED, CALL_CANCELLED, "arrived"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Исполнитель уже у борта — точки маршрута не нужны")
    graph = get_graph(call.airport_icao)
    call.waypoints = waypoint_records(graph, payload.node_ids)
    if call.assigned_employee is not None and call.status in ("assigned", "accepted"):
        plan = assigned_route(call, graph)
        if plan is not None:
            update_assigned_route(call, plan)
    db.commit()
    return to_response(call, db)


@router.post("/{call_id}/waypoints/plan", summary="Лучший порядок промежуточных точек")
def plan_waypoints(
    call_id: int,
    payload: WaypointPlanRequest,
    db: Session = Depends(get_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
):
    """
    В каком порядке пройти точки, чтобы путь был самым быстрым. Ничего
    не меняет: точки записывает запрос «Промежуточные точки».
    """
    call = load_call(db, context, call_id)
    try:
        return waypoint_plan.plan(
            db, call, get_graph(call.airport_icao), payload.groups, payload.keep_order,
            lambda: run_suggestion(db, context, call),
        )
    finally:
        db.rollback()


@router.post("/{call_id}/claim", response_model=CallResponse, summary="Взять в работу")
def claim_call(
    call_id: int,
    payload: ClaimRequest | None = None,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
):
    """
    Диспетчер берёт вызов в работу — или подтверждает, что всё ещё
    разбирает его (клиент повторяет запрос раз в полминуты).

    Пока вызов в работе у одного, остальные видят, кто им занят, но
    назначить или изменить его не могут. Начальник смены и старше
    забирают чужой вызов флагом takeover.
    """
    call = load_call(db, context, call_id)
    claims.claim(db, call, context.user, takeover=bool(payload and payload.takeover))
    db.commit()
    return to_response(call, db)


@router.post("/{call_id}/release", response_model=CallResponse, summary="Отпустить")
def release_call(
    call_id: int,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
):
    """Диспетчер отпускает свой вызов — его может взять другой."""
    call = load_call(db, context, call_id)
    claims.release(call, context.user)
    db.commit()
    return to_response(call, db)


@router.post(
    "/{call_id}/unassign", response_model=CallResponse, summary="Снять исполнителя"
)
def unassign_employee(
    call_id: int,
    payload: UnassignRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
):
    """
    Снятие исполнителя: вызов возвращается к подбору, можно назначить другого.

    Нужно, когда обстановка изменилась уже после назначения: инженер
    задержался на предыдущем борте, машина не завелась, вылет перенесли.
    Закрывать такой вызов нельзя — работа не сделана, борт по-прежнему ждёт.

    Сотрудник освобождается или берётся за следующий вызов своей очереди,
    зарезервированная под него машина возвращается в парк, расчёт прошлого
    назначения стирается. В вызове остаётся запись, с кого и почему сняли.
    """
    call = load_call(db, context, call_id)

    if call.status not in ACTIVE_CALL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_CLOSED_CALL)
    if call.assigned_employee_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_NOT_ASSIGNED)

    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_UNASSIGN_NEEDS_REASON
        )

    team = [call] if call.parent_call_id is not None else [call, *support.support_calls(call)]
    ensure_may_remove(context, *team)

    # Оценка снятия опирается на обстановку до него: после снятия вызов
    # уже «без исполнителя», и понять, успевал ли снятый, будет нельзя.
    removed = call.assigned_employee
    control_before = assess(call)
    unassign(db, call, reason)
    mentor.review_unassign(db, call, removed, control_before, reason, context.user)
    if call.parent_call_id is not None:
        # Вызов поддержки без исполнителя не нужен: это не борт, который
        # ждёт инженера, а запись о том, что второго сняли.
        call.status = CALL_CLOSED
        call.closed_at = utc_now()
    else:
        # Основного сняли — помощники без ответственного не остаются:
        # вызов целиком возвращается к подбору.
        support.close_support(db, call)
    db.commit()
    return to_response(support.root_of(call), db)


@router.patch("/{call_id}/status", response_model=CallResponse, summary="Статус вызова")
def update_call_status(
    call_id: int,
    payload: CallStatusRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
):
    """
    Отмена вызова — единственный статус, который ставит диспетчер.

    Остальное — следствие действий: «назначен» ставит назначение, «принят»,
    «у борта» и «выполнен» — инженер. Отменить начатые работы вправе только
    начальник смены. Очередь исполнителей продвигается сама.
    """
    if payload.status not in CALL_STATUSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ERROR_BAD_STATUS.format(status=payload.status),
        )
    if payload.status == CALL_QUEUED:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_QUEUED_BY_ASSIGN_ONLY)

    call = load_call(db, context, call_id)
    if call.status not in ACTIVE_CALL_STATUSES:
        if call.status == payload.status:
            return to_response(call, db)
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_CLOSED_CALL)
    if payload.status != CALL_CANCELLED:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_STATUS_BY_ACTION)

    root = support.root_of(call)
    ensure_may_remove(context, root, *support.support_calls(root))
    claims.ensure_can_change(db, call, context.user)
    claims.clear(call)
    now = utc_now()
    for item in [root, *support.support_calls(root)]:
        was_working = item.status in WORKING_CALL_STATUSES
        arrived = item.status == CALL_ARRIVED
        item.status = CALL_CANCELLED
        item.closed_at = now
        if item.assigned_employee is not None:
            advance_after_close(db, item.assigned_employee, item, was_working, arrived)
    db.commit()
    return to_response(root, db)
