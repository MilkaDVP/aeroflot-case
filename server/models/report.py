"""
Сообщение о неисправности — то, с чего в жизни начинается вызов.

Пилот не ставит задачу инженеру. Он сообщает о неисправности, сообщение
попадает в информационную систему авиакомпании, диспетчерская его
разбирает и только после этого назначает специалиста — так цепочку
описали эксперты Аэрофлота. Сообщение хранится отдельно от вызова:
диспетчер может принять его (и тогда появляется вызов) или отклонить
с причиной, и оба исхода должны остаться в записи.

Главное поле — reported_at. Пятнадцать минут на прибытие отсчитываются
от сообщения экипажа, а не от момента, когда диспетчер до него дошёл:
время разбора отнимается у инженера, и система обязана это показывать.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from clock import utc_now
from database import Base

REPORT_NEW = "new"
REPORT_ACCEPTED = "accepted"
REPORT_REJECTED = "rejected"


class Report(Base):
    """Сообщение экипажа или внешней системы о неисправности на борту."""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    airport_icao: Mapped[str] = mapped_column(ForeignKey("airports.icao"), index=True)
    # Бортовой номер, как его передал источник, и найденный по нему борт.
    # Борт может не найтись (опечатка, борт не заведён) — сообщение всё
    # равно принимается: потерять его хуже, чем разобрать вручную.
    board_number: Mapped[str] = mapped_column(String)
    aircraft_id: Mapped[int | None] = mapped_column(ForeignKey("aircraft.id"), nullable=True)
    description: Mapped[str] = mapped_column(String)
    reporter: Mapped[str | None] = mapped_column(String, nullable=True)
    # Кто прислал: имя интеграционного ключа (планшет экипажа, система
    # авиакомпании) — по нему видно происхождение каждой заявки.
    source: Mapped[str] = mapped_column(String)

    reported_at: Mapped[datetime] = mapped_column(DateTime)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    status: Mapped[str] = mapped_column(String, default=REPORT_NEW, index=True)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id"), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(String, nullable=True)
    processed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    aircraft = relationship("Aircraft", lazy="selectin")
