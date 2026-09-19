"""
Учётные записи, привязка к аэропортам и сессии входа.

Аэропорт — контекст сессии, а не свойство пользователя: диспетчер может
быть привязан к нескольким аэропортам, но работает в один момент времени
в одном. Поэтому выбранный аэропорт хранится в сессии, а список доступных —
в связке пользователь–аэропорт.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship

from clock import utc_now
from database import Base

ROLE_DISPATCHER = "dispatcher"
ROLE_SHIFT_SUPERVISOR = "shift_supervisor"
ROLE_ENGINEER = "engineer"
ROLE_ADMIN = "admin"

ALL_ROLES = (ROLE_DISPATCHER, ROLE_SHIFT_SUPERVISOR, ROLE_ENGINEER, ROLE_ADMIN)

# Роли, работающие на экране диспетчера. Начальник смены отличается от
# диспетчера только правом переопределить решение системы.
DISPATCH_ROLES = (ROLE_DISPATCHER, ROLE_SHIFT_SUPERVISOR)

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
