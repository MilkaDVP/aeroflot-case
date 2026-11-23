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
    require_roles_in_airport,
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
from database import get_db, get_dispatch_db
from models.airport import Airport
from models.call import ACTIVE_CALL_STATUSES, Call
from services.shifts import end_shift, start_clock
from models.employee import Employee
from models.user import (
    ROLE_ADMIN,
    ROLE_ENGINEER,
    ROLE_LOCAL_ADMIN,
    ROLE_SHIFT_SUPERVISOR,
    AuthSession,
    User,
    role_at_least,
)
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
ERROR_CARD_FORBIDDEN = "Менять статус и смену сотрудников может начальник смены и старше"
ERROR_FIELDS_FORBIDDEN = (
    "Квалификацию, ФИО и скорость меняет администратор аэропорта; "
    "начальнику смены доступны статус и смена"
)

# Поля карточки, которые вправе менять начальник смены: присутствие
# и смена. Квалификация — решение администратора аэропорта.
SUPERVISOR_FIELDS = {"status", "shift"}
ERROR_BAD_SHIFT = "Смена должна быть day или night"
ERROR_NO_AIRPORT = "Аэропорт {icao} не заведён в системе"
ERROR_FOREIGN_AIRPORT = "Заводить сотрудников можно только в своём аэропорту, а не в {icao}"
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
        always_on_shift=employee.always_on_shift,
    )


def load_employee(db, context, employee_id, model=Employee):
    """
    Сотрудник по идентификатору с проверкой аэропорта сессии.

    Проверка обязательна даже там, где кажется избыточной: без неё
    диспетчер одного аэропорта мог бы менять статус сотрудника другого,
    просто подставив чужой идентификатор.
    """
    employee = db.get(model, employee_id)
    if employee is None or employee.archived:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_NOT_FOUND)
    if context.user.role != ROLE_ADMIN:
        if context.airport_icao:
            check_same_airport(context, employee.airport_icao)
        if employee.airport_icao not in context.user.airport_icaos:
            raise HTTPException(404, ERROR_NOT_FOUND)
    return employee


def ensure_card_editor(context, employee, fields):
    """
    Кто какие поля карточки вправе менять.

    Инженер — статус и смену только своей карточки. Начальник смены —
    статус и смену любого сотрудника своего аэропорта. Администратор
    аэропорта и старше — всё. Диспетчер карточки не правит: он
    распределяет вызовы, а присутствием людей управляет начальник смены.
    """
    role = context.user.role
    if role == ROLE_ENGINEER:
        ensure_own_card(context, employee)
        allowed = SUPERVISOR_FIELDS
    elif role_at_least(role, ROLE_LOCAL_ADMIN):
        return
    elif role_at_least(role, ROLE_SHIFT_SUPERVISOR):
        allowed = SUPERVISOR_FIELDS
    else:
        raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_CARD_FORBIDDEN)
    if set(fields) - allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_FIELDS_FORBIDDEN)


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


@router.get("/roster", response_model=list[EmployeeResponse])
def admin_roster(context: Context = Depends(require_roles(ROLE_SHIFT_SUPERVISOR)),
                 db: Session = Depends(get_db)):
    query = db.query(Employee).filter_by(archived=False)
    if context.user.role != ROLE_ADMIN:
        query = query.filter(Employee.airport_icao.in_(context.user.airport_icaos))
    return [to_response(e) for e in query.order_by(Employee.full_name)]


@router.get("", response_model=list[EmployeeResponse], summary="Сотрудники аэропорта")
def list_employees(
    shift: str | None = None,
    all_shifts: bool = False,
    context: Context = Depends(require_airport),
    db: Session = Depends(get_db),
):
    """
    Сотрудники текущего аэропорта.

    По умолчанию — смена из контекста сессии: диспетчер работает со своей
    сменой. Параметр shift позволяет посмотреть состав другой смены,
    shift=all — всех сотрудников аэропорта (для учётных записей).
    """
    query = db.query(Employee).filter_by(
        archived=False, airport_icao=context.airport_icao,
    )
    selected_shift = shift or context.shift
    # all_shifts=true и shift=all — одно и то же: весь состав аэропорта
    # (учётные записи администрирования), без фильтра по смене сессии.
    if selected_shift and not all_shifts and selected_shift != "all":
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
    context: Context = Depends(require_roles(ROLE_LOCAL_ADMIN)),
    db: Session = Depends(get_db),
):
    """
    Регистрация сотрудника. Администратор аэропорта — только в своём
    аэропорту, технический администратор — в любом.

    Можно сразу поставить на смену — тогда сотрудник участвует в подборе,
    не заходя в приложение. И можно выдать логин и пароль — тогда он сам
    войдёт в приложение инженера и будет получать вызовы на телефон.
    """
    if payload.shift not in (SHIFT_DAY, SHIFT_NIGHT):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_BAD_SHIFT)

    if (
        context.user.role != ROLE_ADMIN
        and payload.airport_icao.upper() not in context.user.airport_icaos
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            ERROR_FOREIGN_AIRPORT.format(icao=payload.airport_icao.upper()),
        )

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
        always_on_shift=payload.on_shift,
        shift=payload.shift,
        status=STATUS_FREE if payload.on_shift else STATUS_OFFLINE,
        qualifications=[item.model_dump() for item in payload.qualifications],
        speed_kmh=payload.speed_kmh,
        waiting_lat=payload.lat, waiting_lon=payload.lon,
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


@router.delete("/{employee_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Удалить сотрудника")
def delete_employee(
    employee_id: int,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles_in_airport(ROLE_LOCAL_ADMIN)),
):
    employee = load_employee(db, context, employee_id)
    check_same_airport(context, employee.airport_icao)
    active = db.query(Call.id).filter(
        Call.assigned_employee_id == employee.id, Call.status.in_(ACTIVE_CALL_STATUSES)
    ).first()
    if active:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Сотрудник назначен на вызов №{active.id}")
    release_vehicle(employee)
    employee.archived = True
    employee.status = STATUS_OFFLINE
    accounts = db.query(User.id).filter(User.employee_id == employee.id)
    db.query(AuthSession).filter(AuthSession.user_id.in_(accounts)).delete(synchronize_session=False)
    db.commit()


@router.patch("/{employee_id}", response_model=EmployeeResponse, summary="Обновить")
def update_employee(
    employee_id: int,
    payload: EmployeeUpdateRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(get_context),
):
    """Частичное обновление карточки: статус, смена, квалификации."""
    employee = load_employee(db, context, employee_id)
    changes = payload.model_dump(exclude_unset=True)
    ensure_card_editor(
        context, employee, [field for field, value in changes.items() if value is not None]
    )

    account = db.query(User).filter_by(employee_id=employee.id).first()
    login = changes.pop("login", None)
    password = changes.pop("password", None)
    if login or password:
        if not account and not (login and password):
            raise HTTPException(422, ERROR_LOGIN_PAIR)
        if login:
            existing = db.query(User).filter_by(login=login.strip()).first()
            if existing and existing != account:
                raise HTTPException(409, ERROR_LOGIN_TAKEN.format(login=login))
        if not account:
            account = User(role=ROLE_ENGINEER, employee_id=employee.id, full_name=employee.full_name)
            account.airports.append(db.get(Airport, employee.airport_icao))
            db.add(account)
        if login:
            account.login = login.strip()
        if password:
            from api.users import drop_sessions
            account.password_hash = hash_password(password)
            if account.id:
                drop_sessions(db, account)
    if account and changes.get("full_name"):
        account.full_name = changes["full_name"]
    if "always_on_shift" in changes:
        online = account and account.id and db.query(AuthSession.token).filter_by(user_id=account.id).first()
        if changes["always_on_shift"] or online:
            if employee.status == STATUS_OFFLINE:
                employee.status = STATUS_FREE
            employee.shift_ends_at = None
        elif employee.status != STATUS_OFFLINE:
            end_shift(db, employee, "Смена завершена")

    if "qualifications" in changes and changes["qualifications"] is not None:
        changes["qualifications"] = [
            item.model_dump() for item in payload.qualifications
        ]

    if changes.get("status") == STATUS_OFFLINE:
        if context.user.role != ROLE_ADMIN and working_call_of(db, employee):
            raise HTTPException(409, ERROR_LEAVE_WITH_CALL)
        end_shift(db, employee, "Сотрудник снят со смены")
    elif employee.status == STATUS_OFFLINE and changes.get("status") == STATUS_FREE:
        start_clock(employee)

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
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_airport),
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

    if context.user.role != ROLE_ENGINEER and employee.location_source == "gps" and payload.source == LOCATION_MANUAL:
        raise HTTPException(409, "Сотрудник передаёт координаты GPS")
    if payload.source not in LOCATION_SOURCES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ERROR_BAD_SOURCE.format(source=payload.source),
        )

    if (context.user.role != ROLE_ENGINEER and employee.status in (STATUS_FREE, STATUS_OFFLINE)
            and not employee.return_vehicle and not employee.return_to_waiting):
        employee.waiting_lat, employee.waiting_lon = payload.lat, payload.lon
    changed = record_position(employee, payload.lat, payload.lon, payload.source, payload.fix_age_s)
    from services.assignment import refresh_navigation
    if changed:
        refresh_navigation(db, employee)
        if context.user.role == ROLE_ENGINEER:
            from services.assignment import check_aircraft_arrival
            check_aircraft_arrival(db, employee, payload.accuracy_m)
    db.commit()
    return to_response(employee)


@router.post(
    "/{employee_id}/shift", response_model=EmployeeResponse, summary="Выход на смену"
)
def toggle_shift(
    employee_id: int,
    payload: ShiftRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(get_context),
):
    """
    Выход на смену и уход с неё.

    Выход на смену переводит сотрудника в статус free — только с этого
    момента он становится кандидатом на назначение.
    """
    employee = load_employee(db, context, employee_id)
    ensure_card_editor(context, employee, ["status", "shift"])

    # Уйти со смены с незакрытым вызовом нельзя: вызов остался бы
    # назначенным на человека, которого на перроне уже нет.
    working = working_call_of(db, employee)
    if not payload.on_shift and employee.always_on_shift:
        raise HTTPException(409, "Администратор закрепил сотрудника на смене")


    if payload.shift is not None:
        if payload.shift not in (SHIFT_DAY, SHIFT_NIGHT):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_BAD_SHIFT)
        employee.shift = payload.shift

    if payload.extend and (not payload.on_shift or employee.status == STATUS_OFFLINE):
        raise HTTPException(409, "Смена уже завершена")
    if payload.on_shift and (not employee.shift_ends_at or payload.extend):
        start_clock(employee)
    if not payload.on_shift:
        end_shift(db, employee, "Инженер завершил смену")
    elif working is None:
        employee.status = STATUS_FREE
    # Иначе статус не трогаем. Повторный выход на смену — обычное дело
    # после перезапуска телефона, и он не должен делать занятого инженера
    # свободным: подбор предложил бы его на второй борт.

    db.commit()
    return to_response(employee)
