"""
Зависимости FastAPI: кто вошёл, с какими правами и в каком аэропорту.

Здесь же держится главное правило разграничения доступа из ТЗ: диспетчер
видит только свой аэропорт. Проверка вынесена в зависимость, а не
повторяется в каждой ручке, — иначе однажды её где-нибудь забудут.
"""

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from clock import utc_now
from database import get_db
from models.user import ROLE_ADMIN, AuthSession

ERROR_NO_TOKEN = "Требуется вход в систему"
ERROR_BAD_TOKEN = "Сессия недействительна или истекла"
ERROR_NO_AIRPORT = "Аэропорт не выбран: сначала выполните POST /api/session/airport"
ERROR_FORBIDDEN = "Недостаточно прав для этой операции"
ERROR_WRONG_AIRPORT = "Объект относится к другому аэропорту"


class Context:
    """Контекст запроса: пользователь, его сессия и выбранный аэропорт."""

    def __init__(self, session, user):
        self.session = session
        self.user = user
        self.airport_icao = session.airport_icao
        self.shift = session.shift


def get_token(authorization=Header(default=None)):
    """Извлекает токен из заголовка Authorization: Bearer <токен>."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, ERROR_NO_TOKEN)
    return authorization[len("Bearer ") :].strip()


def get_context(token=Depends(get_token), db: Session = Depends(get_db)):
    """
    Текущая сессия. Истёкшая сессия удаляется сразу, а не копится в базе.
    """
    session = db.get(AuthSession, token)
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, ERROR_BAD_TOKEN)

    if session.is_expired(utc_now()):
        db.delete(session)
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, ERROR_BAD_TOKEN)

    return Context(session, session.user)


class RoleGuard:
    """
    Зависимость FastAPI, пропускающая только перечисленные роли.

    Сделана классом с __call__, а не функцией, возвращающей вложенную
    функцию: FastAPI одинаково принимает оба варианта, а стандарт проекта
    запрещает вложенные функции. Экземпляр хранит список ролей, а
    __call__ получает контекст запроса через Depends, как обычная ручка.

    Администратор проходит всегда: иначе управление справочниками
    пришлось бы перечислять в каждом ограничении отдельно.
    """

    def __init__(self, roles):
        self.roles = tuple(roles)

    def __call__(self, context: Context = Depends(get_context)):
        if context.user.role == ROLE_ADMIN or context.user.role in self.roles:
            return context
        raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_FORBIDDEN)


def require_roles(*roles):
    """Зависимость, пропускающая только перечисленные роли (и администратора)."""
    return RoleGuard(roles)


class AirportRoleGuard:
    """
    Пропускает перечисленные роли и требует выбранного аэропорта.

    Нужна там, где операция и ограничена ролью, и относится к конкретному
    аэропорту — например, постановка машины в парк. Без второй проверки
    администратор, не выбравший аэропорт, получил бы невнятную ошибку
    вместо указания сначала выбрать аэропорт.

    Классом, а не функцией с вложенной функцией: стандарт проекта (§11)
    запрещает вложенные функции.
    """

    def __init__(self, roles):
        self.guard = RoleGuard(roles)

    def __call__(self, context: Context = Depends(get_context)):
        checked = self.guard(context)
        if not checked.airport_icao:
            raise HTTPException(status.HTTP_409_CONFLICT, ERROR_NO_AIRPORT)
        return checked


def require_roles_in_airport(*roles):
    """Зависимость: нужная роль и выбранный аэропорт сессии."""
    return AirportRoleGuard(roles)


def require_airport(context: Context = Depends(get_context)):
    """
    Требует выбранного аэропорта.

    Без него любая выборка данных бессмысленна: неизвестно, чьих
    сотрудников и чьи вызовы показывать.
    """
    if not context.airport_icao:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_NO_AIRPORT)
    return context


def check_same_airport(context, airport_icao):
    """
    Сверяет аэропорт объекта с аэропортом сессии.

    Вызывается перед любым доступом к сотруднику, борту или вызову:
    данные разных аэропортов не смешиваются, и подмена идентификатора
    в запросе не должна давать доступ к чужим данным.
    """
    if airport_icao != context.airport_icao:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_WRONG_AIRPORT)
