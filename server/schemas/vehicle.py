"""Схемы спецтранспорта."""

from pydantic import BaseModel, Field


class VehicleResponse(BaseModel):
    """Машина парка для карты диспетчера."""

    id: int
    call_sign: str
    kind: str
    status: str = Field(description="free, reserved или in_use")
    # Где машина сейчас: у инженера, если едет с ним, иначе где стоит.
    lat: float
    lon: float
    employee_id: int | None
    employee_name: str | None
