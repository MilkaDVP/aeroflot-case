"""
Аэропорт.

В базе хранится только карточка аэропорта. Граф — узлы и рёбра — лежит
в файле data/airports/<ICAO>.json и загружается в память при старте:
он неизменен во время работы, а держать 1800 узлов и 2600 рёбер в SQLite
ради чтения на каждый подбор бессмысленно.
"""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Airport(Base):
    """Карточка аэропорта. Ключ — код ИКАО, он же используется в маршрутах API."""

    __tablename__ = "airports"

    icao: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    city: Mapped[str] = mapped_column(String)
