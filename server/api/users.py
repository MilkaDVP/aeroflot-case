"""
Учётные записи: кто кого заводит и кем управляет.

Правила иерархии:

* технический администратор заводит учётные записи любого уровня
  в любом аэропорту;
* администратор аэропорта — только в своём аэропорту и только младших
  ролей: начальников смены и диспетчеров;
* начальник смены учётные записи не заводит, но вправе завершить сеанс
  младшего (диспетчера или инженера) своего аэропорта — например,
  если человек ушёл со смены и не вышел из системы.

Учётная запись инженера заводится вместе с его карточкой на вкладке
«Сотрудники»: без карточки инженер не участвует в подборе, и отдельная
запись без неё была бы бесполезной.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from auth.dependencies import Context, require_roles
from auth.security import hash_password
from clock import utc_now
from database import get_db
from models.airport import Airport
from models.user import (
    ALL_ROLES,
    ROLE_ADMIN,
    ROLE_ENGINEER,
    ROLE_LOCAL_ADMIN,
    ROLE_RANK,
    ROLE_SHIFT_SUPERVISOR,
    AuthSession,
    User,
)
from schemas.user import UserCreateRequest, UserResponse, UserUpdateRequest
from services import claim as claims

router = APIRouter(prefix="/api/admin/users", tags=["Учётные записи"])

ERROR_NOT_FOUND = "Учётная запись не найдена"
ERROR_LOGIN_TAKEN = "Логин {login} уже занят"
ERROR_BAD_ROLE = "Неизвестная роль {role}"
ERROR_ROLE_TOO_HIGH = "Заводить и назначать можно только роли ниже своей"
ERROR_ENGINEER_HERE = (
    "Инженер заводится вместе с карточкой сотрудника — POST /api/employees (в интерфейсе: роль «инженер»)"
)
ERROR_NEED_AIRPORT = "Укажите аэропорт: учётная запись привязывается ровно к одному"
ERROR_NO_AIRPORT = "Аэропорт {icao} не заведён в системе"
ERROR_FOREIGN = "Учётная запись относится к другому аэропорту"
ERROR_NOT_MANAGEABLE = "Этой учётной записью вы управлять не можете"


def rank(role):
    """Старшинство роли; у инженера — ниже диспетчера, он вне цепочки."""
    return ROLE_RANK.get(role, 0)


def own_airport(user):
    """Аэропорт учётной записи; у технического администратора его нет."""
    icaos = user.airport_icaos
    return icaos[0] if icaos else None


def same_airport(actor, target):
    """Технический администратор видит всех, остальные — свой аэропорт."""
    if actor.role == ROLE_ADMIN:
        return True
    return own_airport(target) is not None and own_airport(target) == own_airport(actor)


def can_edit(actor, target):
    """
    Править и удалять можно младших своего аэропорта. Технический
    администратор управляет всеми, кроме себя самого.
    """
    if actor.id == target.id:
        return False
    if actor.role == ROLE_ADMIN:
        return True
    return (
        rank(actor.role) >= rank(ROLE_LOCAL_ADMIN)
        and same_airport(actor, target)
        and rank(target.role) < rank(actor.role)
    )


def can_logout(actor, target):
    """Завершить чужой сеанс вправе начальник смены и старше — для младших."""
    if actor.id == target.id:
        return False
    if actor.role == ROLE_ADMIN:
        return True
    return (
        rank(actor.role) >= rank(ROLE_SHIFT_SUPERVISOR)
        and same_airport(actor, target)
        and rank(target.role) < rank(actor.role)
    )


def check_assignable_role(actor, role):
    """Назначать можно только известную роль ниже своей (тех. админ — любую)."""
    if role not in ALL_ROLES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_BAD_ROLE.format(role=role))
    if role == ROLE_ENGINEER:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_ENGINEER_HERE)
    if actor.role != ROLE_ADMIN and rank(role) >= rank(actor.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_ROLE_TOO_HIGH)


def online_user_ids(db):
    """Пользователи с действующей сессией."""
    rows = db.query(AuthSession.user_id).filter(AuthSession.expires_at > utc_now()).distinct()
    return {row.user_id for row in rows}


def to_response(actor, user, online_ids):
    return UserResponse(
        id=user.id,
        login=user.login,
        full_name=user.full_name,
        role=user.role,
        airport_icao=own_airport(user),
        employee_id=user.employee_id,
        online=user.id in online_ids,
        can_edit=can_edit(actor, user),
        can_logout=can_logout(actor, user) and user.id in online_ids,
    )


def load_user(db, actor, user_id):
    user = db.get(User, user_id)
    if user is None or (user.employee and user.employee.archived):
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_NOT_FOUND)
    # Чужой аэропорт для не-администратора системы выглядит как «нет такой».
    if not same_airport(actor, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_FOREIGN)
    return user


@router.get("", response_model=list[UserResponse], summary="Учётные записи")
def list_users(
    context: Context = Depends(require_roles(ROLE_SHIFT_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """
    Технический администратор видит все учётные записи системы, остальные —
    своего аэропорта. Порядок: по старшинству роли, затем по ФИО.
    """
    actor = context.user
    online = online_user_ids(db)
    users = [
        user
        for user in db.query(User).all()
        if same_airport(actor, user) and not (user.employee and user.employee.archived)
    ]
    users.sort(key=lambda user: (-rank(user.role), user.role, user.full_name))
    return [to_response(actor, user, online) for user in users]


@router.post(
    "", response_model=UserResponse, status_code=status.HTTP_201_CREATED, summary="Завести"
)
def create_user(
    payload: UserCreateRequest,
    context: Context = Depends(require_roles(ROLE_LOCAL_ADMIN)),
    db: Session = Depends(get_db),
):
    actor = context.user
    check_assignable_role(actor, payload.role)

    login = payload.login.strip()
    if db.query(User).filter(User.login == login).first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_LOGIN_TAKEN.format(login=login))

    user = User(
        login=login,
        password_hash=hash_password(payload.password),
        role=payload.role,
        full_name=payload.full_name.strip(),
    )

    # Технический администратор привязки не имеет; остальные — ровно один
    # аэропорт. Администратор аэропорта заводит людей только у себя.
    if payload.role != ROLE_ADMIN:
        icao = (payload.airport_icao or "").upper() or None
        if actor.role != ROLE_ADMIN:
            if icao is not None and icao != own_airport(actor):
                raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_FOREIGN)
            icao = own_airport(actor)
        if icao is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_NEED_AIRPORT)
        airport = db.get(Airport, icao)
        if airport is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_NO_AIRPORT.format(icao=icao))
        user.airports.append(airport)

    db.add(user)
    db.commit()
    return to_response(actor, user, set())


@router.patch("/{user_id}", response_model=UserResponse, summary="Изменить")
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    context: Context = Depends(require_roles(ROLE_LOCAL_ADMIN)),
    db: Session = Depends(get_db),
):
    actor = context.user
    user = load_user(db, actor, user_id)
    if not can_edit(actor, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_NOT_MANAGEABLE)

    if payload.role is not None and payload.role != user.role:
        if user.role == ROLE_ENGINEER:
            # Инженер привязан к карточке: сменить ему роль значило бы
            # оставить карточку без учётной записи и наоборот.
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_ENGINEER_HERE)
        check_assignable_role(actor, payload.role)
        user.role = payload.role
        if payload.role == ROLE_ADMIN:
            user.airports.clear()
    if payload.airport_icao is not None:
        if actor.role != ROLE_ADMIN or user.role in (ROLE_ADMIN, ROLE_ENGINEER):
            raise HTTPException(403, ERROR_FOREIGN)
        airport = db.get(Airport, payload.airport_icao.upper())
        if airport is None:
            raise HTTPException(404, ERROR_NO_AIRPORT.format(icao=payload.airport_icao))
        user.airports = [airport]
        drop_sessions(db, user)
    if payload.login and payload.login.strip() != user.login:
        login = payload.login.strip()
        if not login or db.query(User.id).filter_by(login=login).first():
            raise HTTPException(409, ERROR_LOGIN_TAKEN.format(login=login))
        user.login = login
    if payload.full_name:
        user.full_name = payload.full_name.strip()
    if payload.password:
        user.password_hash = hash_password(payload.password)
        # Новый пароль — повод закрыть старые сеансы: иначе тот, кто знал
        # прежний пароль, остался бы в системе.
        drop_sessions(db, user)

    db.commit()
    return to_response(actor, user, online_user_ids(db))


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Удалить")
def delete_user(
    user_id: int,
    context: Context = Depends(require_roles(ROLE_LOCAL_ADMIN)),
    db: Session = Depends(get_db),
):
    """
    Удаляет учётную запись. У инженера остаётся карточка сотрудника:
    удалить его из аэропорта — отдельное действие на вкладке «Сотрудники».
    """
    actor = context.user
    user = load_user(db, actor, user_id)
    if not can_edit(actor, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_NOT_MANAGEABLE)
    drop_sessions(db, user)
    user.airports.clear()
    db.delete(user)
    db.commit()


@router.post("/{user_id}/logout", response_model=UserResponse, summary="Завершить сеанс")
def force_logout(
    user_id: int,
    context: Context = Depends(require_roles(ROLE_SHIFT_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """Закрывает все сеансы младшего: ему придётся войти заново."""
    actor = context.user
    user = load_user(db, actor, user_id)
    if not can_logout(actor, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_NOT_MANAGEABLE)
    drop_sessions(db, user)
    db.commit()
    return to_response(actor, user, online_user_ids(db))


def drop_sessions(db, user):
    # Вместе с сеансами освобождаются вызовы, которые человек разбирал.
    if user.employee and not user.employee.always_on_shift:
        from services.shifts import end_shift
        end_shift(db, user.employee, "Сеанс завершён")
    claims.release_all(db, user.id)
    db.query(AuthSession).filter(AuthSession.user_id == user.id).delete(
        synchronize_session=False
    )
