"""
Принятое действие инженера — запись для защиты от повторов.

Телефон на перроне теряет связь. Действие «прибыл», нажатое без сети,
уходит на сервер позже и может уйти дважды: телефон не знает, дошёл ли
первый запрос, если ответ потерялся. Каждое действие поэтому несёт
идентификатор, выданный на телефоне, и сервер применяет его один раз.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from clock import utc_now
from database import Base


class EngineerAction(Base):
    """Действие, уже применённое сервером, по его идентификатору с телефона."""

    __tablename__ = "engineer_actions"

    action_id: Mapped[str] = mapped_column(String, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("route_participants.id"), index=True)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id"), nullable=True)
    action: Mapped[str] = mapped_column(String)
    # Когда действие совершено на телефоне и когда дошло до сервера:
    # разница — время, которое инженер работал без связи.
    performed_at: Mapped[datetime] = mapped_column(DateTime)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
