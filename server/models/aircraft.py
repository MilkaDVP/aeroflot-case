"""Воздушное судно на стоянке."""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Aircraft(Base):
    """
    Борт, находящийся на стоянке аэропорта.

    stand_node_id — идентификатор узла в графе аэропорта, а не отдельная
    таблица стоянок: перечень стоянок целиком приходит из выгрузки OSM
    и в базе дублироваться не должен.
    """

    __tablename__ = "aircraft"

    id: Mapped[int] = mapped_column(primary_key=True)
    board_number: Mapped[str] = mapped_column(String, index=True)
    aircraft_type: Mapped[str] = mapped_column(String)
    airport_icao: Mapped[str] = mapped_column(ForeignKey("airports.icao"), index=True)
    stand_node_id: Mapped[str] = mapped_column(String)

    # Борт не на стоянке (на ВПП, рулёжке, у ангара): его точное место.
    # Тогда stand_node_id — ближайшая к нему вершина рулёжной сети, до
    # которой строится маршрут инженера. Пусто — борт стоит на стоянке.
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Прибытие ВС по предполагаемому расписанию, UTC; пусто — не задано.
    scheduled_arrival_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
