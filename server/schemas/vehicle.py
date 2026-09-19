"""Схемы спецтранспорта."""

from pydantic import BaseModel, Field


class VehicleCreateRequest(BaseModel):
    """
    Постановка машины в парк. Доступно только администратору.

    Машина заводится свободной и стоящей в указанной точке: занятой она
    становится только через назначение вызова, иначе появилась бы вторая
    правда о том, кто куда едет.
    """

    call_sign: str = Field(min_length=1, description="Позывной, например ТМ-04")
    kind: str = Field(default="Техпомощь", description="Тип машины")
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


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
