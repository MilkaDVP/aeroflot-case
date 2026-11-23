"""Воздушное судно на стоянке и его последняя внешняя геопозиция."""

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

    # Live-позиция не заменяет стоянку: вызовы и маршруты остаются привязаны
    # к обслуживаемой стоянке, а на карте можно показать фактическое движение ВС.
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    location_source: Mapped[str | None] = mapped_column(String, nullable=True)
    location_accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_speed_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
