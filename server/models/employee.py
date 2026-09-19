"""
Сотрудник оперативного технического обслуживания.

Квалификации хранятся полем JSON, а не отдельной таблицей. Причина:
алгоритм подбора работает с ними как со списком словарей и ничего не знает
про базу, а связь «сотрудник — отметка — типы ВС» нигде не нужна как
самостоятельная сущность. Отдельная таблица дала бы три JOIN на каждый
подбор без единого выигрыша.

Машины здесь нет. Раньше был флаг has_vehicle, но машина — не свойство
человека, а объект со своим местом и статусом (models/vehicle.py).
Флаг остался только как выводимое свойство: «едет ли сотрудник на машине
прямо сейчас» определяется по самой машине.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from clock import to_iso_utc, utc_now
from constants import STATUS_OFFLINE, VEHICLE_IN_USE
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

    # Индивидуальная скорость ходьбы. None — нормативная скорость пешехода
    # из constants.py. Скорость машины — свойство машины, а не человека.
    speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Когда освободится, если сейчас занят на вызове.
    busy_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now
    )

    # Машина парка, которая сейчас у сотрудника (едет с ним или ждёт его).
    vehicle = relationship(
        "Vehicle", back_populates="employee", uselist=False, lazy="selectin"
    )

    @property
    def has_vehicle(self):
        """Едет ли сотрудник на машине прямо сейчас. Выводится из машины."""
        return self.vehicle is not None and self.vehicle.status == VEHICLE_IN_USE

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
            "speed_kmh": self.speed_kmh,
            # Своя машина — та, на которой сотрудник уже едет. Свободные
            # машины парка алгоритм получает отдельным списком.
            "vehicle": self.vehicle.as_algorithm_dict() if self.has_vehicle else None,
            "qualifications": self.qualifications or [],
            # С поясом: эта строка доходит до диспетчера как «освободится
            # до 14:11», и без пояса браузер сдвинет её на разницу с UTC.
            "busy_until": to_iso_utc(self.busy_until),
        }
