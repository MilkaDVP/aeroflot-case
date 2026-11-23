"""
Спецтранспорт — общий парк машин аэропорта.

Машина — отдельная сущность, а не свойство инженера. Раньше «инженер
на машине» было флагом в его карточке: система не знала, где стоят
свободные машины, и не могла предложить пешему инженеру дойти до
ближайшей. Требование §10 — «учёт наличия поблизости спецтранспорта
и его влияние на маршрут» — без этого не выполнялось.

Факт «у инженера машина» выводится отсюда, по employee_id, а не хранится
у машины: один источник правды вместо двух, которые рано или поздно
разойдутся.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from clock import utc_now
from constants import VEHICLE_FREE, VEHICLE_IN_USE
from database import Base


class Vehicle(Base):
    """Машина парка: позывной, где стоит и у кого сейчас."""

    __tablename__ = "vehicles"

    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
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

    # Собственный GPS трекера машины. Если машина едет с инженером, на карте
    # используется более свежая из двух позиций — его телефона или трекера.
    location_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    location_source: Mapped[str | None] = mapped_column(String, nullable=True)
    location_accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_speed_mps: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Скорость именно этой машины; None — нормативная скорость спецтранспорта.
    speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now
    )

    employee = relationship("Employee", back_populates="vehicle", lazy="selectin")

    def uses_employee_position(self):
        """Позиция инженера свежее собственной фиксации машины."""
        if (
            self.status != VEHICLE_IN_USE
            or self.employee is None
            or self.employee.lat is None
        ):
            return False
        if self.employee.location_at is None:
            return self.location_at is None
        return self.location_at is None or self.employee.location_at >= self.location_at

    def position(self):
        """
        Где машина сейчас.

        Едет с инженером — используется более свежая из позиций его телефона
        и собственного трекера машины. Свободна или ждёт инженера — там, где стоит.
        """
        if self.uses_employee_position():
            return self.employee.lat, self.employee.lon
        return self.lat, self.lon

    def location_metadata(self):
        """Время и источник той позиции, которую вернул position()."""
        if self.uses_employee_position():
            return self.employee
        return self

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
