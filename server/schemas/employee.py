"""Схемы сотрудников, их квалификаций и местоположения."""

from pydantic import BaseModel, Field


class QualificationSchema(BaseModel):
    """Квалификационная отметка из свидетельства специалиста по ТО."""

    category: str = Field(description="Отметка: A1..A4, B1.1..B1.4, B2, C")
    aircraft_types: list[str] = Field(description="Типы ВС, на которые есть допуск")
    valid_until: str = Field(description="Срок действия отметки, ГГГГ-ММ-ДД")


class EmployeeResponse(BaseModel):
    """Сотрудник ОТО для панели диспетчера и карты."""

    id: int
    full_name: str
    airport_icao: str
    qualifications: list[QualificationSchema]
    shift: str
    status: str
    lat: float | None
    lon: float | None
    # Едет ли на машине парка прямо сейчас — выводится из машины.
    has_vehicle: bool
    vehicle_call_sign: str | None = None
    speed_kmh: float | None
    busy_until: str | None
    updated_at: str


class EmployeeCreateRequest(BaseModel):
    """
    Регистрация сотрудника. Доступно только администратору.

    Учётная запись для приложения инженера необязательна: сотрудник без неё
    виден диспетчеру и участвует в подборе, но сам войти в систему не может.
    """

    full_name: str = Field(min_length=1)
    airport_icao: str
    shift: str
    qualifications: list[QualificationSchema] = []
    speed_kmh: float | None = None
    lat: float | None = None
    lon: float | None = None
    # Сразу на смене — чтобы проверить подбор, не заходя в приложение
    # инженера. Иначе новый сотрудник «не на смене» и в подбор не попадает.
    on_shift: bool = False
    # Логин и пароль для входа в приложение инженера. Заполняются вместе.
    login: str | None = None
    password: str | None = None


class EmployeeUpdateRequest(BaseModel):
    """Частичное обновление: передаются только изменяемые поля."""

    full_name: str | None = None
    shift: str | None = None
    status: str | None = None
    speed_kmh: float | None = None
    qualifications: list[QualificationSchema] | None = None


class LocationRequest(BaseModel):
    """Координаты от PWA инженера — настоящий GPS либо режим имитации."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class ShiftRequest(BaseModel):
    """Выход на смену или уход с неё."""

    on_shift: bool
    shift: str | None = Field(default=None, description="day или night")
