"""
Сотрудник оперативного технического обслуживания.

Квалификации хранятся полем JSON, а не отдельной таблицей. Причина:
алгоритм подбора работает с ними как со списком словарей и ничего не знает
про базу, а связь «сотрудник — отметка — типы ВС» нигде не нужна как
самостоятельная сущность. Отдельная таблица дала бы три JOIN на каждый
подбор без единого выигрыша.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from clock import to_iso_utc, utc_now
from constants import STATUS_OFFLINE
from database import Base


class Employee(Base):
    """Карточка сотрудника ОТО: квалификация, смена, статус, местоположение."""

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String)
    airport_icao: Mapped[str] = mapped_column(ForeignKey("airports.icao"), index=True)

    # [{"category": "B1.1", "aircraft_types": ["A320"], "valid_until": "2027-03-15"}]
    qualifications: Mapped[list] = mapped_column(JSON, default=list)

    shift: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default=STATUS_OFFLINE)

    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    has_vehicle: Mapped[bool] = mapped_column(Boolean, default=False)
    # Индивидуальная скорость передвижения. None — берётся нормативная
    # скорость способа передвижения из constants.py.
    speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Когда освободится, если сейчас занят на вызове.
    busy_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now
    )

    def as_algorithm_dict(self):
        """
        Представление для алгоритма подбора.

        Алгоритм намеренно не знает про ORM: он принимает обычные словари.
        Преобразование живёт здесь — в одном месте на всю систему.
        """
        return {
            "id": self.id,
            "full_name": self.full_name,
            "shift": self.shift,
            "status": self.status,
            "lat": self.lat,
            "lon": self.lon,
            "has_vehicle": self.has_vehicle,
            "speed_kmh": self.speed_kmh,
            "qualifications": self.qualifications or [],
            # С поясом: эта строка доходит до диспетчера как «освободится
            # до 14:11», и без пояса браузер сдвинет её на разницу с UTC.
            "busy_until": to_iso_utc(self.busy_until),
        }
