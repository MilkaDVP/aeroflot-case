"""Схемы единого приёма геопозиций по контракту финального задания."""

from pydantic import BaseModel, Field


class TrackPoint(BaseModel):
    """Одна точка от инженера, спецтранспорта или ВС."""

    obj: str = Field(description="e-01, v-01 или регистрационный номер ВС")
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    t: str | None = Field(default=None, description="Время фиксации ISO 8601 UTC")
    acc: float | None = Field(default=None, ge=0, description="Точность, метры")
    spd: float | None = Field(default=None, ge=0, description="Скорость, м/с")


class TrackBatch(BaseModel):
    """Пакет точек; результаты возвращаются в том же порядке."""

    points: list[TrackPoint]


class TrackResponse(BaseModel):
    """Результат обработки одной синтаксически корректной точки."""

    accepted: bool
    applied: bool
    obj: str
    t: str
