"""Схемы ручной онлайн-синхронизации Flightradar24."""

from datetime import datetime

from pydantic import BaseModel, Field


class FlightradarSyncItem(BaseModel):
    """Решение по одному борту из онлайн-выборки Flightradar24."""

    board_number: str
    source_type: str | None
    aircraft_type: str | None
    stand_ref: str | None
    status: str = Field(description="created, existing или skipped")
    detail: str


class FlightradarSyncResponse(BaseModel):
    """Итог синхронизации без сохранения сырого ответа API."""

    airport_icao: str
    provider: str = Field(description="Источник позиций: adsblol или flightradar24")
    fetched_at: datetime
    received: int
    created: int
    existing: int
    skipped: int
    elapsed_ms: float
    items: list[FlightradarSyncItem]
