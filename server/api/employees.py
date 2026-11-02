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
from auth.security import hash_password
from clock import to_iso_utc, utc_now
from constants import (
    LOCATION_MANUAL,
    LOCATION_SOURCES,
    SHIFT_DAY,
    SHIFT_NIGHT,
    STATUS_FREE,
    STATUS_OFFLINE,
)
from database import get_db
from models.airport import Airport
from models.employee import Employee
from models.user import ROLE_ADMIN, ROLE_ENGINEER, User
from services.assignment import record_position, release_vehicle, working_call_of
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
ERROR_NO_AIRPORT = "Аэропорт {icao} не заведён в системе"
ERROR_BAD_SOURCE = "Источник координат должен быть gps, simulation или manual, а не {source}"
ERROR_LOGIN_PAIR = "Логин и пароль задаются вместе: одного без другого недостаточно"
ERROR_LOGIN_TAKEN = "Логин {login} уже занят"
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
        location_at=to_iso_utc(employee.location_at),
        location_source=employee.location_source,
        seen_at=to_iso_utc(employee.seen_at),
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
    """
    Регистрация сотрудника. Только администратор.

    Можно сразу поставить на смену — тогда сотрудник участвует в подборе,
    не заходя в приложение. И можно выдать логин и пароль — тогда он сам
    войдёт в приложение инженера и будет получать вызовы на телефон.
    """
    if payload.shift not in (SHIFT_DAY, SHIFT_NIGHT):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_BAD_SHIFT)

    airport = db.get(Airport, payload.airport_icao.upper())
    if airport is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            ERROR_NO_AIRPORT.format(icao=payload.airport_icao.upper()),
        )

    login = (payload.login or "").strip()
    password = payload.password or ""
    if bool(login) != bool(password):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_LOGIN_PAIR)
    if login and db.query(User).filter(User.login == login).first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_LOGIN_TAKEN.format(login=login))

    employee = Employee(
        full_name=payload.full_name.strip(),
        airport_icao=airport.icao,
        shift=payload.shift,
        status=STATUS_FREE if payload.on_shift else STATUS_OFFLINE,
        qualifications=[item.model_dump() for item in payload.qualifications],
        speed_kmh=payload.speed_kmh,
        lat=payload.lat,
        lon=payload.lon,
    )
    if payload.lat is not None and payload.lon is not None:
        # Точку новому сотруднику ставит администратор — это ручная расстановка.
        employee.location_source = LOCATION_MANUAL
        employee.location_at = utc_now()
    db.add(employee)

    if login:
        # Учётная запись создаётся вместе с карточкой и привязывается к ней:
        # по этой связи приложение инженера находит «свой» вызов.
        db.flush()
        user = User(
            login=login,
            password_hash=hash_password(password),
            role=ROLE_ENGINEER,
            full_name=employee.full_name,
            employee_id=employee.id,
        )
        user.airports.append(airport)
        db.add(user)

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
    Приём координат: от PWA инженера или ручная перестановка на карте.

    По условиям задания допустимы и настоящий GPS, и ручной ввод, но
    серверу источник уже не безразличен: от него зависит, может ли точка
    устареть. Прогноз прибытия по GPS-фиксации, которой больше
    полутора минут, ненадёжен, а точку, поставленную вручную, не обновляют намеренно.
    """
    employee = load_employee(db, context, employee_id)
    ensure_own_card(context, employee)

    if payload.source not in LOCATION_SOURCES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ERROR_BAD_SOURCE.format(source=payload.source),
        )

    record_position(employee, payload.lat, payload.lon, payload.source, payload.fix_age_s)
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
