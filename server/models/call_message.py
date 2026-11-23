"""
Сообщение по вызову: переписка инженера с диспетчером.

Инженеру у борта проще написать «нужен подъёмник, жду 20 минут», чем
выбирать из готовых статусов: реальных ситуаций больше, чем кнопок.
Сообщения привязаны к вызову, а не к человеку: диспетчер читает их
на карточке вызова, и после смены исполнителя переписка остаётся.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from clock import utc_now
from database import Base

AUTHOR_ENGINEER = "engineer"
AUTHOR_DISPATCHER = "dispatcher"


class CallMessage(Base):
    """Одно сообщение: кто написал, от чьего имени и когда."""

    __tablename__ = "call_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Всегда основной вызов: переписка двух инженеров одного борта общая.
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id"), index=True)
    author_role: Mapped[str] = mapped_column(String)
    author_name: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(String)
    # Сообщение о проблеме: вызов горит красным, пока её не исправят.
    is_problem: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sql_text("0"))
    # Когда и кем проблема отмечена исправленной; пусто — ещё открыта.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
