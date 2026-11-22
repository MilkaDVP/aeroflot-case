"""
Вызовы: регистрация, подбор исполнителя, назначение, смена статуса.

Главный рабочий цикл системы. Подбор вынесен отдельной ручкой, а не
выполняется при создании вызова: диспетчер должен увидеть предложение
и основания к нему до того, как кого-то назначит.
"""

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
from services.drill import is_enabled
from models.employee import RouteParticipant
from models.user import DISPATCH_ROLES, ROLE_ADMIN, ROLE_SHIFT_SUPERVISOR
from schemas.call import (
    AssignRequest,
    CallCreateRequest,
    CallResponse,
    CallStatusRequest,
    SuggestResponse,
    UnassignRequest,
    MessageRequest,
    UrgencyRequest,
)
from services import mentor
from services.assignment import (
    advance_after_close,
    assign_now,
    enqueue,
    free_vehicles,
    can_switch_from,
    is_busy,
    mark_arrived,
    plan_route,
    switch_to,
    working_call_of,
    unassign,
)
from models.call_message import AUTHOR_DISPATCHER
from services import support
from services.deadline import assess
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
ERROR_UNASSIGNED_REASON = "снят с этого вызова: {reason}"

# Кто вправе назначить не того, кого предложила система, и поставить вызов
# в очередь к занятому. Обычный диспетчер такого права не имеет — это прямо
# следует из разграничения ролей в ТЗ.
OVERRIDE_ROLES = (ROLE_SHIFT_SUPERVISOR, ROLE_ADMIN)

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
        stand_ref=stand_ref_of(call),
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
        previous_employee_name=previous.full_name if previous else None,
        unassign_reason=call.unassign_reason,
        unassigned_at=to_iso_utc(call.unassigned_at),
        control=assess(call, now).as_dict(),
        parent_call_id=call.parent_call_id,
        support=support.support_summary(call) if call.parent_call_id is None else [],
        messages=support.messages_of(call),
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
    employees = db.query(RouteParticipant).filter_by(
        archived=False, airport_icao=context.airport_icao,
        is_training=is_enabled(db, context.airport_icao),
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
    if call.previous_employee_id is None:
        return employees, None

    removed = None
    remaining = []
    for employee in employees:
        if employee["id"] == call.previous_employee_id:
            removed = employee
        else:
            remaining.append(employee)
    return remaining, removed


def run_suggestion(db, context, call):
    """
    Запускает подбор по вызову и возвращает результат алгоритма.

    В подбор уходят только свободные машины парка: зарезервированную
    за другим вызовом или едущую с другим инженером предложить нельзя.
    """
    graph = get_graph(call.airport_icao)
    aircraft = call.aircraft
    payload = {
        "aircraft_type": aircraft.aircraft_type,
        "defect_code": call.defect_code,
        "stand_node_id": call.stand_node_id,
        # Отметка, записанная при регистрации: для типа, заведённого
        # диспетчером, во встроенном справочнике её не вычислить.
        "required_mark": call.required_mark,
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
    return result


def describe_busy(db, entry):
    """
    Дополняет запись о занятом: на каком он борте и можно ли его переключить.

    Диспетчер, выбирая из занятых, должен видеть не только «освободится
    в 14:40», но и чем человек занят: того, кто ждёт запчасти, можно
    переключить без вреда, а того, кто вскрыл агрегат, — нельзя.
    """
    employee = db.get(RouteParticipant, entry["employee_id"])
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
    return [to_response(call, db, now) for call in calls]


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
    defect = find_defect(db, payload.defect_code)
    if defect is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ERROR_UNKNOWN_DEFECT.format(code=payload.defect_code),
        )

    aircraft = db.get(Aircraft, payload.aircraft_id)
    if aircraft is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_AIRCRAFT_NOT_FOUND)
    check_same_airport(context, aircraft.airport_icao)

    call = new_call(db, aircraft, defect, payload.description)
    call.urgent = payload.urgent
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
    result = run_suggestion(db, context, call)

    # Предложение фиксируется только пока вызов не назначен. После
    # назначения поле хранит историю: кого система предлагала в момент
    # решения. Повторный подбор (диспетчер просто открыл вызов в списке)
    # переписал бы её, и назначение задним числом стало бы выглядеть
    # как переопределение — или наоборот.
    if call.status in ASSIGNABLE_STATUSES:
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
    perform_assign(db, context, call, payload)
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
    employee = db.get(RouteParticipant, payload.employee_id)
    if employee is None or employee.archived:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_EMPLOYEE_NOT_FOUND)
    check_same_airport(context, employee.airport_icao)
    if employee.is_training != is_enabled(db, context.airport_icao):
        raise HTTPException(status.HTTP_409_CONFLICT, "Исполнитель недоступен в текущем режиме")
    check_permit(call, employee)

    # Подбор выполняется перед каждым назначением. Предложение системы
    # должно существовать — иначе переопределение не отличить от обычного
    # назначения, — а свежий результат нужен Наставнику: по нему видно,
    # кого система предлагала в эту минуту и сколько стоит отступление.
    result = run_suggestion(db, context, call)
    if free_choice:
        call.suggested_employee_id = employee.id
    elif call.suggested_employee_id is None:
        call.suggested_employee_id = result.best.employee["id"] if result.best else None

    # Занятость проверяется по фактическому состоянию, а не по предложению:
    # предложение могло устареть, пока этого сотрудника назначали на другой
    # борт. Именно так раньше получался второй «текущий» вызов у одного
    # инженера, и первый из них терялся.
    if is_busy(db, employee):
        if context.user.role not in OVERRIDE_ROLES:
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
        if context.user.role not in OVERRIDE_ROLES:
            raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_OVERRIDE_FORBIDDEN)

    plan, pickup_vehicle = plan_route(
        db,
        employee,
        call.stand_node_id,
        get_graph(call.airport_icao),
        payload.travel,
        ride_vehicle,
    )
    if plan is None:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_NO_ROUTE)

    call.override_reason = payload.override_reason if is_override else None
    assign_now(db, call, employee, plan, pickup_vehicle)
    if payload.travel == TRAVEL_WALK:
        # Своя машина, если она есть, остаётся там, где стоит: в этот
        # вызов она не входит, и на карточке её быть не должно.
        call.vehicle = None
    elif ride_vehicle is not None:
        # Машина остаётся за ответственным: он её забирает и отвечает за
        # неё; второй едет пассажиром, поэтому за ним она не числится.
        call.vehicle = ride_vehicle
    mentor.review_assignment(db, call, employee, result, context.user, is_override)


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
        employee = db.get(RouteParticipant, payload.employee_id)
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
    call.urgent = payload.urgent
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
    Смена статуса вызова.

    Закрытие вызова продвигает очередь исполнителя: следующий вызов
    становится текущим, а если очереди нет — сотрудник освобождается.
    Раньше закрытие освобождало сотрудника безусловно, и его остальные
    вызовы оставались назначенными без исполнителя.
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

    if payload.status == CALL_CANCELLED:
        root = support.root_of(call)
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

    if call.status == CALL_QUEUED and payload.status in WORKING_CALL_STATUSES:
        # Ручной перевод вызова из очереди в текущие дал бы инженеру
        # два текущих вызова одновременно — ровно тот дефект, от которого
        # очередь и защищает.
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_QUEUE_PROMOTES_ITSELF)

    was_working = call.status in WORKING_CALL_STATUSES
    arrived_now = payload.status == CALL_ARRIVED and call.status != CALL_ARRIVED
    call.status = payload.status
    if arrived_now and call.assigned_employee is not None:
        if mark_arrived(db, call.assigned_employee, call):
            mentor.review_arrival(db, call, call.assigned_employee)

    if payload.status == CALL_CLOSED:
        call.closed_at = utc_now()
        if call.assigned_employee is not None:
            advance_after_close(db, call.assigned_employee, call, was_working)
        if call.parent_call_id is None:
            support.close_support(db, call, call.closed_at)

    db.commit()
    return to_response(call, db)
