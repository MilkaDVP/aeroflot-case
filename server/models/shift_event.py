"""
Замечание Наставника — оценка действия диспетчера или обстановки.

Наставник — вымышленный опытный диспетчер, который смотрит за сменой
и коротко говорит: это хорошо, это не очень, это плохо, и почему.
Оценки хранятся в базе, а не считаются на лету: разбор смены должен
пережить перезагрузку сервера, а оценка — остаться той, что была вынесена
в момент решения, даже если обстановка потом изменилась.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from clock import utc_now
from database import Base

VERDICT_GOOD = "good"
VERDICT_FAIR = "fair"
VERDICT_BAD = "bad"


class ShiftEvent(Base):
    """Одно замечание: что произошло, оценка, объяснение и цифры к нему."""

    __tablename__ = "shift_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    airport_icao: Mapped[str] = mapped_column(ForeignKey("airports.icao"), index=True)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id"), nullable=True, index=True)
    # Сотрудник, о котором замечание. Вместе с вызовом и видом события
    # защищает от повторов: наблюдение «не успевает» выносится один раз
    # на исполнителя, а после переназначения — заново.
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("route_participants.id"), nullable=True)

    kind: Mapped[str] = mapped_column(String)
    verdict: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(String)
    # Цифры, на которых стоит оценка: по ним считается итог смены,
    # и по ним же оценку можно перепроверить.
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    # Чьё решение оценено; у наблюдений за обстановкой пусто.
    actor_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
