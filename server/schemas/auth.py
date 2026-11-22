"""Схемы входа, выхода и выбора контекста сессии."""

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """Учётные данные."""

    login: str
    password: str


class AirportBrief(BaseModel):
    """Карточка аэропорта в списке доступных пользователю."""

    icao: str
    name: str
    city: str
    # Опорная точка проекции графа: от неё считаются метровые x/y.
    ref_point: dict | None = None
    # Центр рабочей зоны (стоянки и техцентры). На него PWA инженера
    # наводит карту, пока нет координат: тянуть ради этого весь граф
    # на телефон было бы расточительно, а ref_point для этого не годится —
    # он смещён к краю аэродрома.
    centre: dict | None = None
    # Расписание смен и что сейчас по местному времени: экран входа
    # сам предлагает идущую смену, но её можно сменить вручную.
    timezone: str | None = None
    day_start: str | None = None
    day_end: str | None = None
    local_time: str | None = None
    current_shift: str | None = None


class ShiftScheduleRequest(BaseModel):
    """Расписание смен аэропорта."""

    timezone: str = Field(description="Код IANA, например Europe/Moscow")
    day_start: str = Field(description="Начало дневной смены, ЧЧ:ММ")
    day_end: str = Field(description="Конец дневной смены, ЧЧ:ММ")


class LoginResponse(BaseModel):
    """Ответ на вход: токен и всё, что нужно для следующего шага сценария."""

    token: str
    role: str
    full_name: str
    airports: list[AirportBrief]
    # Если аэропорт один, клиент пропускает шаг выбора и сразу открывает
    # рабочий экран — сервер сообщает об этом явно, чтобы логика ветвления
    # не дублировалась в веб-клиенте и в PWA.
    airport_icao: str | None = None
    employee_id: int | None = None


class CurrentUserResponse(BaseModel):
    """Текущий пользователь и контекст его сессии."""

    login: str
    role: str
    full_name: str
    airports: list[AirportBrief]
    airport_icao: str | None
    shift: str | None
    employee_id: int | None


class SelectAirportRequest(BaseModel):
    """Выбор аэропорта и смены как контекста сессии."""

    airport_icao: str
    shift: str = Field(description="day или night")


class MessageResponse(BaseModel):
    """Короткий ответ без данных."""

    message: str
