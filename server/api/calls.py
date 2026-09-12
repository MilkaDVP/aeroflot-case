"""
Вызовы: регистрация, подбор исполнителя, назначение, смена статуса.

Главный рабочий цикл системы. Подбор вынесен отдельной ручкой, а не
выполняется при создании вызова: диспетчер должен увидеть предложение
и основания к нему до того, как кого-то назначит.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from algorithm.dispatch import suggest
from algorithm.qualification import find_qualification, parse_mark, required_mark
from auth.dependencies import (
    Context,
    check_same_airport,
    require_airport,
    require_roles,
)
from clock import to_iso_utc, utc_now, utc_today
from constants import DEFECT_TYPES
from database import get_db
from graph_registry import get_graph
from models.aircraft import Aircraft
from models.call import ACTIVE_CALL_STATUSES, CALL_STATUSES, WORKING_CALL_STATUSES, Call
from models.call import STATUS_CLOSED as CALL_CLOSED
from models.call import STATUS_NEW as CALL_NEW
from models.call import STATUS_QUEUED as CALL_QUEUED
from models.call import STATUS_SUGGESTED as CALL_SUGGESTED
from models.employee import Employee
from models.user import DISPATCH_ROLES, ROLE_ADMIN, ROLE_SHIFT_SUPERVISOR
from schemas.call import (
    AssignRequest,
    CallCreateRequest,
    CallResponse,
    CallStatusRequest,
    SuggestResponse,
)
from services.assignment import (
    advance_after_close,
    assign_now,
    enqueue,
    free_vehicles,
    is_busy,
    plan_route,
)

router = APIRouter(prefix="/api/calls", tags=["Вызовы"])

ERROR_CALL_NOT_FOUND = "Вызов не найден"
ERROR_AIRCRAFT_NOT_FOUND = "Борт не найден"
ERROR_EMPLOYEE_NOT_FOUND = "Сотрудник не найден"
ERROR_UNKNOWN_DEFECT = "Неизвестный код дефекта: {code}"
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


def to_response(call, db=None):
    """Модель вызова в схему ответа, с подстановкой данных борта."""
    aircraft = call.aircraft
    assigned = call.assigned_employee
    defect = DEFECT_TYPES.get(call.defect_code, {})

    return CallResponse(
        id=call.id,
        airport_icao=call.airport_icao,
        aircraft_id=call.aircraft_id,
        board_number=aircraft.board_number if aircraft else "—",
        aircraft_type=aircraft.aircraft_type if aircraft else "—",
        stand_node_id=call.stand_node_id,
        stand_ref=stand_ref_of(call),
        defect_code=call.defect_code,
        defect_name=defect.get("name", call.defect_code),
        required_mark=call.required_mark,
        status=call.status,
        created_at=to_iso_utc(call.created_at),
        closed_at=to_iso_utc(call.closed_at),
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
    )


def shift_employees(db, context):
    """Сотрудники аэропорта сессии в виде словарей для алгоритма."""
    employees = (
        db.query(Employee).filter(Employee.airport_icao == context.airport_icao).all()
    )
    return [employee.as_algorithm_dict() for employee in employees]


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
    }
    vehicles = [vehicle.as_algorithm_dict() for vehicle in free_vehicles(db, call.airport_icao)]
    return suggest(
        graph, payload, shift_employees(db, context), context.shift, vehicles=vehicles
    )


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

    calls = query.order_by(Call.created_at.desc()).all()
    return [to_response(call, db) for call in calls]


@router.post(
    "",
    response_model=CallResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Зарегистрировать вызов",
)
def create_call(
    payload: CallCreateRequest,
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
    db: Session = Depends(get_db),
):
    """
    Регистрация вызова: борт и код дефекта.

    Стоянка и требуемая отметка не передаются клиентом, а выводятся на
    сервере: стоянка известна из карточки борта, отметка — из справочника
    дефектов и типа ВС. Клиент не должен иметь возможности задать их сам,
    иначе появится способ обойти проверку допуска.
    """
    if payload.defect_code not in DEFECT_TYPES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ERROR_UNKNOWN_DEFECT.format(code=payload.defect_code),
        )

    aircraft = db.get(Aircraft, payload.aircraft_id)
    if aircraft is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_AIRCRAFT_NOT_FOUND)
    check_same_airport(context, aircraft.airport_icao)

    call = Call(
        airport_icao=aircraft.airport_icao,
        aircraft_id=aircraft.id,
        stand_node_id=aircraft.stand_node_id,
        defect_code=payload.defect_code,
        required_mark=str(required_mark(payload.defect_code, aircraft.aircraft_type)),
        status=CALL_NEW,
    )
    db.add(call)
    db.commit()
    return to_response(call, db)


@router.post(
    "/{call_id}/suggest", response_model=SuggestResponse, summary="Подобрать исполнителя"
)
def suggest_employee(
    call_id: int,
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
    db: Session = Depends(get_db),
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
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
    db: Session = Depends(get_db),
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

    employee = db.get(Employee, payload.employee_id)
    if employee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_EMPLOYEE_NOT_FOUND)
    check_same_airport(context, employee.airport_icao)
    check_permit(call, employee)

    # Предложение системы должно существовать до назначения: иначе
    # переопределение невозможно отличить от обычного назначения.
    if call.suggested_employee_id is None:
        result = run_suggestion(db, context, call)
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
        enqueue(db, call, employee)
        db.commit()
        return to_response(call, db)

    is_override = call.suggested_employee_id != employee.id
    if is_override:
        if not payload.override_reason:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_OVERRIDE_NEEDS_REASON
            )
        if context.user.role not in OVERRIDE_ROLES:
            raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_OVERRIDE_FORBIDDEN)

    plan, pickup_vehicle = plan_route(
        db, employee, call.stand_node_id, get_graph(call.airport_icao)
    )
    if plan is None:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_NO_ROUTE)

    call.override_reason = payload.override_reason if is_override else None
    assign_now(db, call, employee, plan, pickup_vehicle)
    db.commit()
    return to_response(call, db)


@router.patch("/{call_id}/status", response_model=CallResponse, summary="Статус вызова")
def update_call_status(
    call_id: int,
    payload: CallStatusRequest,
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
    db: Session = Depends(get_db),
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
    if call.status == CALL_QUEUED and payload.status in WORKING_CALL_STATUSES:
        # Ручной перевод вызова из очереди в текущие дал бы инженеру
        # два текущих вызова одновременно — ровно тот дефект, от которого
        # очередь и защищает.
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_QUEUE_PROMOTES_ITSELF)

    was_working = call.status in WORKING_CALL_STATUSES
    call.status = payload.status

    if payload.status == CALL_CLOSED:
        call.closed_at = utc_now()
        if call.assigned_employee is not None:
            advance_after_close(db, call.assigned_employee, call, was_working)

    db.commit()
    return to_response(call, db)
