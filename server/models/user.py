"""
Учётные записи, привязка к аэропортам и сессии входа.

Иерархия ролей (сверху вниз):

    admin             — технический администратор системы: все аэропорты,
                        любые учётные записи, внештатные ситуации;
    local_admin       — администратор аэропорта: заводит учётные записи
                        и справочники своего аэропорта;
    shift_supervisor  — начальник смены: управляет статусами людей ниже
                        себя и вправе переопределить решение системы;
    dispatcher        — распределяет вызовы и ведёт связь с инженерами;
    engineer          — выполняет работы; в цепочку наследования не входит.

Старшая роль получает все права младших (кроме особых прав инженера).

Каждая учётная запись, кроме технического администратора, привязана
ровно к одному аэропорту. Технический администратор привязки не имеет:
ему доступны все аэропорты системы. Выбранный аэропорт хранится в сессии.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship

from clock import utc_now
from database import Base

ROLE_DISPATCHER = "dispatcher"
ROLE_SHIFT_SUPERVISOR = "shift_supervisor"
ROLE_ENGINEER = "engineer"
ROLE_LOCAL_ADMIN = "local_admin"
ROLE_ADMIN = "admin"

ALL_ROLES = (
    ROLE_DISPATCHER,
    ROLE_SHIFT_SUPERVISOR,
    ROLE_LOCAL_ADMIN,
    ROLE_ENGINEER,
    ROLE_ADMIN,
)

# Старшинство ролей: старшая наследует права всех младших. Инженера здесь
# нет — его права особые (своя карточка, свой вызов), и ни одна роль
# диспетчерской цепочки их не получает.
ROLE_RANK = {
    ROLE_DISPATCHER: 1,
    ROLE_SHIFT_SUPERVISOR: 2,
    ROLE_LOCAL_ADMIN: 3,
    ROLE_ADMIN: 4,
}

# Роли, работающие на экране диспетчера: диспетчер и все, кто старше.
DISPATCH_ROLES = (ROLE_DISPATCHER, ROLE_SHIFT_SUPERVISOR, ROLE_LOCAL_ADMIN)


def role_at_least(role, minimum):
    """Не ниже ли роль заданной по старшинству. Инженер не выше никого."""
    return role in ROLE_RANK and ROLE_RANK[role] >= ROLE_RANK[minimum]

# Пользователь может быть привязан к нескольким аэропортам.
user_airports = Table(
    "user_airports",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
    Column("airport_icao", String, ForeignKey("airports.icao"), primary_key=True),
)


class User(Base):
    """Учётная запись пользователя системы."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)
    full_name: Mapped[str] = mapped_column(String)

    # Заполняется только у роли engineer: связь учётной записи с карточкой
    # сотрудника ОТО, по которой идёт подбор.
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id"), nullable=True
    )

    airports = relationship("Airport", secondary=user_airports, lazy="selectin")
    employee = relationship("Employee", lazy="selectin")

    @property
    def airport_icaos(self):
        """Коды аэропортов, к которым привязана учётная запись."""
        return [airport.icao for airport in self.airports]


class AuthSession(Base):
    """
    Сессия входа: токен, пользователь и выбранный контекст работы.

    Контекст (аэропорт и смена) живёт здесь, а не на клиенте, потому что
    от него зависит выборка данных на сервере. Клиент не должен иметь
    возможности запросить чужой аэропорт, подменив параметр.
    """

    __tablename__ = "auth_sessions"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    airport_icao: Mapped[str | None] = mapped_column(String, nullable=True)
    shift: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    user = relationship("User", lazy="selectin")

    def is_expired(self, now=None):
        """Истёк ли срок действия сессии."""
        return self.expires_at <= (now or utc_now())
