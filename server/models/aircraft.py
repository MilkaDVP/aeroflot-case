"""Воздушное судно на стоянке."""

from sqlalchemy import ForeignKey, String
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
