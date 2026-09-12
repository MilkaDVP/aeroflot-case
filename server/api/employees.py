"""
Сотрудники ОТО: список, создание, обновление, координаты, смена.

Ручки координат и смены вызывает PWA инженера, остальные — рабочее место
диспетчера и администратор.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from auth.dependencies import (
    Context,
    check_same_airport,
    get_context,
    require_airport,
    require_roles,
)
from clock import to_iso_utc, utc_now
from constants import SHIFT_DAY, SHIFT_NIGHT, STATUS_FREE, STATUS_OFFLINE
from database import get_db
from models.employee import Employee
from models.user import ROLE_ADMIN, ROLE_ENGINEER
from services.assignment import release_vehicle, working_call_of
from schemas.employee import (
    EmployeeCreateRequest,
    EmployeeResponse,
    EmployeeUpdateRequest,
    LocationRequest,
    ShiftRequest,
)

router = APIRouter(prefix="/api/employees", tags=["Сотрудники"])

ERROR_NOT_FOUND = "Сотрудник не найден"
ERROR_NOT_SELF = "Инженер может изменять только собственную карточку"
ERROR_BAD_SHIFT = "Смена должна быть day или night"
ERROR_LEAVE_WITH_CALL = (
    "Нельзя уйти со смены с незакрытым вызовом: завершите его "
    "или попросите диспетчера передать вызов другому"
)


def to_response(employee):
    """Модель БД в схему ответа."""
    return EmployeeResponse(
        id=employee.id,
        full_name=employee.full_name,
        airport_icao=employee.airport_icao,
        qualifications=employee.qualifications or [],
        shift=employee.shift,
        status=employee.status,
        lat=employee.lat,
        lon=employee.lon,
        has_vehicle=employee.has_vehicle,
        vehicle_call_sign=employee.vehicle.call_sign if employee.vehicle else None,
        speed_kmh=employee.speed_kmh,
        busy_until=to_iso_utc(employee.busy_until),
        updated_at=to_iso_utc(employee.updated_at),
    )


def load_employee(db, context, employee_id):
    """
    Сотрудник по идентификатору с проверкой аэропорта сессии.

    Проверка обязательна даже там, где кажется избыточной: без неё
    диспетчер одного аэропорта мог бы менять статус сотрудника другого,
    просто подставив чужой идентификатор.
    """
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_NOT_FOUND)
    if context.airport_icao:
        check_same_airport(context, employee.airport_icao)
    return employee


def ensure_own_card(context, employee):
    """
    Инженер правит только свою карточку.

    Без этой проверки любой инженер мог бы двигать по карте чужие метки
    и снимать коллег со смены.
    """
    if context.user.role == ROLE_ADMIN:
        return
    if context.user.role == ROLE_ENGINEER and context.user.employee_id != employee.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_NOT_SELF)


@router.get("", response_model=list[EmployeeResponse], summary="Сотрудники аэропорта")
def list_employees(
    shift: str | None = None,
    context: Context = Depends(require_airport),
    db: Session = Depends(get_db),
):
    """
    Сотрудники текущего аэропорта.

    По умолчанию — смена из контекста сессии: диспетчер работает со своей
    сменой. Параметр shift позволяет посмотреть состав другой смены.
    """
    query = db.query(Employee).filter(Employee.airport_icao == context.airport_icao)
    selected_shift = shift or context.shift
    if selected_shift:
        query = query.filter(Employee.shift == selected_shift)

    return [to_response(employee) for employee in query.order_by(Employee.full_name).all()]


@router.post(
    "",
    response_model=EmployeeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать сотрудника",
)
def create_employee(
    payload: EmployeeCreateRequest,
    context: Context = Depends(require_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """Создание карточки сотрудника. Только администратор."""
    if payload.shift not in (SHIFT_DAY, SHIFT_NIGHT):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_BAD_SHIFT)

    employee = Employee(
        full_name=payload.full_name,
        airport_icao=payload.airport_icao,
        shift=payload.shift,
        status=STATUS_OFFLINE,
        qualifications=[item.model_dump() for item in payload.qualifications],
        speed_kmh=payload.speed_kmh,
        lat=payload.lat,
        lon=payload.lon,
    )
    db.add(employee)
    db.commit()
    return to_response(employee)


@router.patch("/{employee_id}", response_model=EmployeeResponse, summary="Обновить")
def update_employee(
    employee_id: int,
    payload: EmployeeUpdateRequest,
    context: Context = Depends(get_context),
    db: Session = Depends(get_db),
):
    """Частичное обновление карточки: статус, смена, квалификации."""
    employee = load_employee(db, context, employee_id)
    ensure_own_card(context, employee)

    changes = payload.model_dump(exclude_unset=True)
    if "qualifications" in changes and changes["qualifications"] is not None:
        changes["qualifications"] = [
            item.model_dump() for item in payload.qualifications
        ]

    for field, value in changes.items():
        if value is not None:
            setattr(employee, field, value)

    db.commit()
    return to_response(employee)


@router.post(
    "/{employee_id}/location", response_model=EmployeeResponse, summary="Обновить координаты"
)
def update_location(
    employee_id: int,
    payload: LocationRequest,
    context: Context = Depends(get_context),
    db: Session = Depends(get_db),
):
    """
    Приём координат от PWA инженера.

    Источник координат серверу безразличен: настоящий GPS или режим
    имитации для демонстрации — по условиям задания допустимо и то, и другое.
    """
    employee = load_employee(db, context, employee_id)
    ensure_own_card(context, employee)

    employee.lat = payload.lat
    employee.lon = payload.lon
    employee.updated_at = utc_now()
    db.commit()
    return to_response(employee)


@router.post(
    "/{employee_id}/shift", response_model=EmployeeResponse, summary="Выход на смену"
)
def toggle_shift(
    employee_id: int,
    payload: ShiftRequest,
    context: Context = Depends(get_context),
    db: Session = Depends(get_db),
):
    """
    Выход на смену и уход с неё.

    Выход на смену переводит сотрудника в статус free — только с этого
    момента он становится кандидатом на назначение.
    """
    employee = load_employee(db, context, employee_id)
    ensure_own_card(context, employee)

    # Уйти со смены с незакрытым вызовом нельзя: вызов остался бы
    # назначенным на человека, которого на перроне уже нет.
    working = working_call_of(db, employee)
    if not payload.on_shift and working is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_LEAVE_WITH_CALL)

    if payload.shift is not None:
        if payload.shift not in (SHIFT_DAY, SHIFT_NIGHT):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_BAD_SHIFT)
        employee.shift = payload.shift

    if not payload.on_shift:
        # Уходя со смены, инженер оставляет машину там, где стоит: иначе
        # она числилась бы за человеком, которого на перроне нет.
        release_vehicle(employee)
        employee.status = STATUS_OFFLINE
    elif working is None:
        employee.status = STATUS_FREE
    # Иначе статус не трогаем. Повторный выход на смену — обычное дело
    # после перезапуска телефона, и он не должен делать занятого инженера
    # свободным: подбор предложил бы его на второй борт.

    db.commit()
    return to_response(employee)
