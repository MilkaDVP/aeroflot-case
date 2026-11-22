"""
Вызов инженера к воздушному судну.

Хранит и решение системы, и решение человека. Разделение принципиально:
если начальник смены переопределил предложенного кандидата, в записи
остаётся и то, кого предлагала система, и причина отклонения. Без этого
невозможно ни разобрать инцидент, ни оценить качество алгоритма.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, String, text
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
STATUS_CANCELLED = "cancelled"

CALL_STATUSES = (
    STATUS_NEW,
    STATUS_SUGGESTED,
    STATUS_QUEUED,
    STATUS_ASSIGNED,
    STATUS_ACCEPTED,
    STATUS_ARRIVED,
    STATUS_CANCELLED,
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
    # Что именно сообщил экипаж — своими словами. Тип неисправности нужен
    # для подбора, а инженеру у борта важны подробности: какая стойка,
    # где течь, что показывает индикация.
    defect_description: Mapped[str | None] = mapped_column(String, nullable=True)

    # Отметка, требуемая под этот дефект и тип ВС. Считается один раз при
    # создании вызова и сохраняется: справочник может измениться, а причина
    # решения по конкретному вызову должна остаться воспроизводимой.
    required_mark: Mapped[str] = mapped_column(String)

    status: Mapped[str] = mapped_column(String, default=STATUS_NEW)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Когда о неисправности сообщил экипаж, если вызов создан из входящего
    # сообщения. От этого момента, а не от регистрации диспетчером,
    # отсчитывается регламент прибытия. Пусто — вызов заведён диспетчером
    # напрямую, и отсчёт идёт от регистрации.
    reported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Источник сообщения (имя интеграционного ключа) — происхождение вызова.
    source: Mapped[str | None] = mapped_column(String, nullable=True)

    # Срочность выставляет диспетчер: срочные вызовы стоят первыми в списке.
    urgent: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    # AOG (Aircraft on Ground) — борт не может лететь. Статус ставит только
    # инженер после осмотра и диагностики: ни экипаж, ни диспетчер этого
    # определить не вправе.
    aog: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    # Этап работ у борта (constants.WORK_STAGES); пусто до прибытия.
    work_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    # Первое прибытие исполнителя. Отделено от eta_at: инженера, ждущего
    # запчасти, могут переключить на другой борт, и его возвращение —
    # не новое «прибытие по регламенту».
    first_arrived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Кого предложила система и кого назначил человек.
    suggested_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("route_participants.id"), nullable=True
    )
    assigned_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("route_participants.id"), nullable=True, index=True
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

    # История снятия исполнителя. Диспетчер снимает назначение, когда
    # обстановка изменилась: инженер застрял, борт переставили, работы
    # оказались другого объёма. Запись о снятии остаётся в вызове —
    # без неё в разборе не отличить «долго искали исполнителя» от
    # «назначили и передумали».
    previous_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("route_participants.id"), nullable=True
    )
    unassign_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    unassigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Машина, на которой исполнитель едет к этому борту, и узел, где он
    # её забирает. pickup_node_id пуст, если машина уже была при нём
    # или он идёт пешком: тогда маршрут из одного участка.
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True)
    pickup_node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Сколько метров маршрута пройдено в режиме учений. Пусто, пока
    # исполнитель не начал движение или идёт не по учебному сценарию.
    sim_progress_m: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Вызов поддержки: ещё один инженер на тот же борт по тому же вызову.
    # Здесь — основной вызов. Отдельная запись, а не список исполнителей
    # в одной: у каждого инженера свой маршрут, своя машина, свой статус
    # и своя очередь, и вся эта логика уже работает для вызова целиком.
    parent_call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id"), nullable=True)

    aircraft = relationship("Aircraft", lazy="selectin")
    vehicle = relationship("Vehicle", lazy="selectin")
    # Тип неисправности по коду. Внешнего ключа в схеме нет: таблица вызовов
    # старше справочника, а в SQLite ключ к существующей таблице без её
    # пересоздания не добавить. Для чтения названия и главы он не нужен.
    defect_type = relationship(
        "DefectType",
        primaryjoin="foreign(Call.defect_code) == DefectType.code",
        viewonly=True,
        lazy="selectin",
    )
    assigned_employee = relationship(
        "RouteParticipant", foreign_keys=[assigned_employee_id], lazy="selectin"
    )
    previous_employee = relationship(
        "RouteParticipant", foreign_keys=[previous_employee_id], lazy="selectin"
    )

    @property
    def is_override(self):
        """Отличается ли назначение от предложения системы."""
        return (
            self.assigned_employee_id is not None
            and self.suggested_employee_id is not None
            and self.assigned_employee_id != self.suggested_employee_id
        )
