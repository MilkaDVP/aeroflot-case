"""Схемы управления учётными записями."""

from pydantic import BaseModel, Field


class UserCreateRequest(BaseModel):
    """
    Новая учётная запись.

    Аэропорт обязателен для всех ролей, кроме технического администратора.
    Администратор аэропорта заводит учётные записи только в своём, поэтому
    для него аэропорт можно не указывать — подставится сам.
    """

    login: str = Field(min_length=1)
    password: str = Field(min_length=1)
    full_name: str = Field(min_length=1)
    role: str
    airport_icao: str | None = None


class UserUpdateRequest(BaseModel):
    """Правка учётной записи: ФИО, новый пароль, роль. Всё необязательно."""

    full_name: str | None = None
    password: str | None = None
    role: str | None = None


class UserResponse(BaseModel):
    """Учётная запись в списке администрирования."""

    id: int
    login: str
    full_name: str
    role: str
    airport_icao: str | None
    employee_id: int | None
    # Есть ли действующая сессия — «в сети» ли человек.
    online: bool
    # Что текущему пользователю разрешено с этой записью: интерфейс
    # не показывает кнопок, на которые сервер всё равно ответит отказом.
    can_edit: bool
    can_logout: bool
