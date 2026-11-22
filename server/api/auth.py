"""
Вход, выход и выбор контекста сессии.

Реализует сценарий входа из ТЗ: логин → выбор аэропорта → выбор смены →
рабочий экран. Если аэропорт у пользователя один, сервер выбирает его сам
и сообщает клиенту, что шаг можно пропустить.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from auth.dependencies import Context, get_context
from auth.security import new_token, session_expiry, verify_password
from constants import SHIFT_DAY, SHIFT_NIGHT
from database import get_db
from graph_registry import get_graph
from models.airport import SOURCE_MANUAL, Airport
from models.user import ROLE_ADMIN, AuthSession, User
from services import claim as claims
from services import shift_clock
from schemas.auth import (
    AirportBrief,
    CurrentUserResponse,
    LoginRequest,
    LoginResponse,
    MessageResponse,
    SelectAirportRequest,
)

router = APIRouter(prefix="/api", tags=["Аутентификация"])

ERROR_BAD_CREDENTIALS = "Неверный логин или пароль"
ERROR_NO_ACCESS_TO_AIRPORT = "Учётная запись не привязана к аэропорту {icao}"
ERROR_BAD_SHIFT = "Смена должна быть day или night"


def available_airports(db, user):
    """
    Аэропорты, в которых пользователь может работать.

    Технический администратор привязки не имеет: ему доступны все
    аэропорты системы, включая только что созданные. Остальные роли
    работают в своём аэропорту.
    """
    if user.role == ROLE_ADMIN:
        return db.query(Airport).order_by(Airport.icao).all()
    return list(user.airports)


def airport_briefs(db, user):
    """Список доступных пользователю аэропортов с опорными точками."""
    return [brief_of(airport) for airport in available_airports(db, user)]


def brief_of(airport):
    """
    Карточка аэропорта для списка.

    Опорная точка берётся из графа. Если графа нет на диске, карточка
    всё равно отдаётся: без координат список аэропортов остаётся
    пригодным для выбора, а падать на этом нельзя.
    """
    ref_point = None
    centre = None
    try:
        graph = get_graph(airport.icao)
        ref_point = graph.ref_point
        centre = graph.ref_point if airport.source == SOURCE_MANUAL else graph.centre
    except HTTPException:
        pass

    return AirportBrief(
        icao=airport.icao,
        name=airport.name,
        city=airport.city,
        ref_point=ref_point,
        centre=centre,
        **shift_clock.info(airport),
    )


@router.post("/auth/login", response_model=LoginResponse, summary="Вход")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    """Проверяет учётные данные и открывает сессию."""
    user = db.query(User).filter(User.login == payload.login).one_or_none()

    # Ответ одинаков и при неверном логине, и при неверном пароле:
    # иначе по ответу можно перебором узнать существующие учётные записи.
    if user is None or (user.employee and user.employee.archived) or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, ERROR_BAD_CREDENTIALS)

    airports = airport_briefs(db, user)
    single = airports[0] if len(airports) == 1 else None
    session = AuthSession(
        token=new_token(),
        user_id=user.id,
        # Один аэропорт — шаг выбора пропускается, а смена берётся
        # по местному времени (её можно сменить на экране выбора).
        airport_icao=single.icao if single else None,
        shift=single.current_shift if single else None,
        expires_at=session_expiry(),
    )
    db.add(session)
    db.commit()

    return LoginResponse(
        token=session.token,
        role=user.role,
        full_name=user.full_name,
        airports=airports,
        airport_icao=session.airport_icao,
        employee_id=user.employee_id,
    )


@router.post("/auth/logout", response_model=MessageResponse, summary="Выход")
def logout(context: Context = Depends(get_context), db: Session = Depends(get_db)):
    """
    Закрывает сессию: запись токена удаляется, повторно он не сработает.
    Вызовы, которые человек разбирал, освобождаются для других.
    """
    claims.release_all(db, context.user.id)
    db.delete(context.session)
    db.commit()
    return MessageResponse(message="Сессия закрыта")


@router.get("/auth/me", response_model=CurrentUserResponse, summary="Текущий пользователь")
def current_user(context: Context = Depends(get_context), db: Session = Depends(get_db)):
    """Пользователь, его роль, аэропорты и выбранный контекст работы."""
    user = context.user
    return CurrentUserResponse(
        login=user.login,
        role=user.role,
        full_name=user.full_name,
        airports=airport_briefs(db, user),
        airport_icao=context.airport_icao,
        shift=context.shift,
        employee_id=user.employee_id,
    )


@router.post(
    "/session/airport", response_model=MessageResponse, summary="Выбрать аэропорт и смену"
)
def select_airport(
    payload: SelectAirportRequest,
    context: Context = Depends(get_context),
    db: Session = Depends(get_db),
):
    """
    Закрепляет аэропорт и смену за сессией.

    Аэропорт проверяется по привязке учётной записи: выбрать чужой нельзя,
    даже зная его код.
    """
    if payload.shift not in (SHIFT_DAY, SHIFT_NIGHT):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_BAD_SHIFT)

    allowed = [airport.icao for airport in available_airports(db, context.user)]
    if payload.airport_icao not in allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            ERROR_NO_ACCESS_TO_AIRPORT.format(icao=payload.airport_icao),
        )

    context.session.airport_icao = payload.airport_icao
    context.session.shift = payload.shift
    db.commit()

    return MessageResponse(
        message=f"Контекст сессии: {payload.airport_icao}, смена {payload.shift}"
    )
