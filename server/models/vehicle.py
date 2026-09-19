"""
Спецтранспорт — общий парк машин аэропорта.

Машина — отдельная сущность, а не свойство инженера. Раньше «инженер
на машине» было флагом в его карточке: система не знала, где стоят
свободные машины, и не могла предложить пешему инженеру дойти до
ближайшей. Требование §10 — «учёт наличия поблизости спецтранспорта
и его влияния на маршрут» — без этого не выполнялось.

Факт «у инженера машина» выводится отсюда, по employee_id, а не хранится
у инженера: один источник правды вместо двух, которые рано или поздно
разойдутся.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from clock import utc_now
from constants import VEHICLE_FREE, VEHICLE_IN_USE
from database import Base


class Vehicle(Base):
    """Машина парка: позывной, где стоит и у кого сейчас."""

    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    airport_icao: Mapped[str] = mapped_column(ForeignKey("airports.icao"), index=True)
    call_sign: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default=VEHICLE_FREE)

    # Где машину оставили. Пока она едет с инженером, эти координаты
    # не обновляются: её место берётся из места инженера.
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)

    # У кого машина: у инженера, который на ней едет, либо за кем она
    # зарезервирована на вызов и ждёт, пока он до неё дойдёт.
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id"), nullable=True, index=True
    )
    # Скорость именно этой машины; None — нормативная скорость спецтранспорта.
    speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now
    )

    employee = relationship("Employee", back_populates="vehicle", lazy="selectin")

    def position(self):
        """
        Где машина сейчас.

        Едет с инженером — там же, где он: отдельные координаты машины
        отставали бы от его GPS. Свободна или ждёт инженера — там, где стоит.
        """
        if (
            self.status == VEHICLE_IN_USE
            and self.employee is not None
            and self.employee.lat is not None
        ):
            return self.employee.lat, self.employee.lon
        return self.lat, self.lon

    def as_algorithm_dict(self):
        """Представление для алгоритма подбора — обычный словарь, без ORM."""
        lat, lon = self.position()
        return {
            "id": self.id,
            "call_sign": self.call_sign,
            "lat": lat,
            "lon": lon,
            "speed_kmh": self.speed_kmh,
        }
