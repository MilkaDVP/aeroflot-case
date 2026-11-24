"""
Журнал событий — неизменяемая хронология работы системы (п. 3.2).

Одна строка — одно событие: когда (до микросекунды, в UTC), что
(тип и описание), с какой заявкой и бортом, кто инициатор и кто
исполнитель, сколько заняло (для подбора кандидатов) и параметры.

Неизменяемость обеспечена трижды:
- в API нет ни правки, ни удаления записей журнала;
- триггеры базы отклоняют UPDATE и DELETE этой таблицы, кто бы их ни
  выполнил — сервер, скрипт или руками в консоли SQLite;
- каждая запись хранит хэш SHA-256 своего содержимого вместе с хэшем
  предыдущей. Подмена, удаление или вставка задним числом рвёт цепочку,
  и проверка целостности показывает, с какой записи.
"""

from datetime import datetime

from sqlalchemy import DDL, JSON, DateTime, Float, Integer, String, event
from sqlalchemy.orm import Mapped, mapped_column

from clock import utc_now
from database import Base


class EventLog(Base):
    """Одно событие журнала."""

    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Момент события в UTC с микросекундами; наружу — до миллисекунды.
    ts: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    # Аэропорт события — по нему журнал разграничивается между
    # администраторами аэропортов. Пусто — событие всей системы.
    airport_icao: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # Раздел (заявка, подбор, маршрут, норматив, ошибка, действие) и тип.
    category: Mapped[str] = mapped_column(String, index=True)
    type: Mapped[str] = mapped_column(String, index=True)
    # Одна строка по-русски: что произошло.
    text: Mapped[str] = mapped_column(String)

    # Заявка и борт. Не внешний ключ: журнал переживает удаление записей,
    # о которых в нём рассказано.
    call_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    board_number: Mapped[str | None] = mapped_column(String, nullable=True)

    # Инициатор: пользователь, от чьего имени выполнено действие.
    # Пусто — система (такт контроля, подбор, учения).
    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    actor_name: Mapped[str | None] = mapped_column(String, nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String, nullable=True)
    # Исполнитель, о котором событие.
    employee_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    employee_name: Mapped[str | None] = mapped_column(String, nullable=True)

    # Сколько заняло действие, мс: формирование списка кандидатов,
    # обработка запроса.
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)

    # Защита от повторной записи одного и того же наблюдения
    # («риск нарушения» по вызову выносится один раз на исполнителя).
    dedup_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    prev_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    hash: Mapped[str | None] = mapped_column(String, nullable=True)


# Триггеры создаются вместе с таблицей — и в рабочей базе, и в тестовой.
TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS event_log_no_update BEFORE UPDATE ON event_log "
    "BEGIN SELECT RAISE(ABORT, 'Журнал событий неизменяем: правка запрещена'); END",
    "CREATE TRIGGER IF NOT EXISTS event_log_no_delete BEFORE DELETE ON event_log "
    "BEGIN SELECT RAISE(ABORT, 'Журнал событий неизменяем: удаление запрещено'); END",
)
for _statement in TRIGGERS:
    event.listen(
        EventLog.__table__, "after_create", DDL(_statement).execute_if(dialect="sqlite")
    )
