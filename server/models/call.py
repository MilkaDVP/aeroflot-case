"""
Вызов инженера к воздушному судну.

Хранит и решение системы, и решение человека. Разделение принципиально:
если начальник смены переопределил предложенного кандидата, в записи
остаётся и то, кого предлагала система, и причина отклонения. Без этого
невозможно ни разобрать инцидент, ни оценить качество алгоритма.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from clock import utc_now
from database import Base

STATUS_NEW = "new"
STATUS_SUGGESTED = "suggested"
# Вызов поставлен в очередь к занятому инженеру и ждёт, пока тот закончит
# текущие работы. Статус добавлен к контракту §8 по согласованию команды.
STATUS_QUEUED = "queued"
STATUS_ASSIGNED = "assigned"
STATUS_ACCEPTED = "accepted"
STATUS_ARRIVED = "arrived"
STATUS_CLOSED = "closed"

CALL_STATUSES = (
    STATUS_NEW,
    STATUS_SUGGESTED,
    STATUS_QUEUED,
    STATUS_ASSIGNED,
    STATUS_ACCEPTED,
    STATUS_ARRIVED,
    STATUS_CLOSED,
)

# Вызовы, по которым работа ещё не завершена.
ACTIVE_CALL_STATUSES = (
    STATUS_NEW,
    STATUS_SUGGESTED,
    STATUS_QUEUED,
    STATUS_ASSIGNED,
    STATUS_ACCEPTED,
    STATUS_ARRIVED,
)

# «Текущие» вызовы — те, которыми инженер занят прямо сейчас. Инвариант:
# у одного сотрудника такой вызов не больше одного, остальные его вызовы
# стоят в очереди. Нарушение инварианта означает вызов, к которому никто
# не едет, при том что система считает его обслуживаемым.
WORKING_CALL_STATUSES = (
    STATUS_ASSIGNED,
    STATUS_ACCEPTED,
    STATUS_ARRIVED,
)


class Call(Base):
    """Вызов: борт, стоянка, дефект и назначенный исполнитель."""

    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    airport_icao: Mapped[str] = mapped_column(ForeignKey("airports.icao"), index=True)
    aircraft_id: Mapped[int] = mapped_column(ForeignKey("aircraft.id"))
    stand_node_id: Mapped[str] = mapped_column(String)
    defect_code: Mapped[str] = mapped_column(String)

    # Отметка, требуемая под этот дефект и тип ВС. Считается один раз при
    # создании вызова и сохраняется: справочник может измениться, а причина
    # решения по конкретному вызову должна остаться воспроизводимой.
    required_mark: Mapped[str] = mapped_column(String)

    status: Mapped[str] = mapped_column(String, default=STATUS_NEW)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Кого предложила система и кого назначил человек.
    suggested_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id"), nullable=True
    )
    assigned_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id"), nullable=True, index=True
    )
    override_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    eta_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    route_node_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Ожидаемый момент прибытия, UTC. Для вызова в очереди «через N минут»
    # теряет смысл — инженер сначала закончит текущие работы, — поэтому
    # хранится абсолютное время: по нему видно, насколько нарушен регламент.
    eta_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Когда вызов поставлен в очередь. Определяет порядок очереди и остаётся
    # в записи после продвижения — для разбора, сколько вызов ждал.
    queued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    aircraft = relationship("Aircraft", lazy="selectin")
    assigned_employee = relationship(
        "Employee", foreign_keys=[assigned_employee_id], lazy="selectin"
    )

    @property
    def is_override(self):
        """Отличается ли назначение от предложения системы."""
        return (
            self.assigned_employee_id is not None
            and self.suggested_employee_id is not None
            and self.assigned_employee_id != self.suggested_employee_id
        )
