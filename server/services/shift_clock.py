"""
Какая смена идёт сейчас в аэропорту — по его местному времени.

У аэропорта свой часовой пояс и граница дневной смены («с 08:00 до 20:00»);
всё остальное время — ночная. Задают их начальник смены и администраторы:
на разных станциях смены начинаются по-разному.

Сервер хранит время в UTC, поэтому местное время считается здесь, по
базе часовых поясов IANA. Это важно для аэропортов в других поясах:
во Владивостоке дневная смена начинается, когда в Москве ещё ночь.
"""

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from constants import SHIFT_DAY, SHIFT_NIGHT

DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_DAY_START = "08:00"
DEFAULT_DAY_END = "20:00"

HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

ERROR_BAD_TIMEZONE = "Неизвестный часовой пояс {tz}: нужен код IANA, например Europe/Moscow"
ERROR_BAD_TIME = "Время смены задаётся как ЧЧ:ММ, а не {value}"
ERROR_SAME_TIME = "Начало и конец дневной смены не могут совпадать"


def zone(name):
    """Часовой пояс по коду IANA; ValueError с понятным текстом, если такого нет."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(ERROR_BAD_TIMEZONE.format(tz=name)) from error


def minutes_of(value):
    """«08:30» → 510. ValueError, если формат не ЧЧ:ММ."""
    match = HHMM.match(value or "")
    if not match:
        raise ValueError(ERROR_BAD_TIME.format(value=value))
    return int(match.group(1)) * 60 + int(match.group(2))


def validate(timezone_name, day_start, day_end):
    """Проверяет расписание целиком. ValueError с текстом для диспетчера."""
    zone(timezone_name)
    if minutes_of(day_start) == minutes_of(day_end):
        raise ValueError(ERROR_SAME_TIME)


def schedule_of(airport):
    """Часовой пояс и границы дневной смены; пустые поля — значения по умолчанию."""
    return (
        airport.timezone or DEFAULT_TIMEZONE,
        airport.day_shift_start or DEFAULT_DAY_START,
        airport.day_shift_end or DEFAULT_DAY_END,
    )


def local_now(airport, now_utc=None):
    """Местное время аэропорта. now_utc — наивное UTC, как хранит сервер."""
    tz_name, _, _ = schedule_of(airport)
    now = now_utc or datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(tzinfo=timezone.utc).astimezone(zone(tz_name))


def shift_at(airport, now_utc=None):
    """
    Смена по местному времени. Если граница дневной смены переходит
    через полночь (например, 20:00–08:00 для станции, где «день» ночью),
    это тоже учитывается.
    """
    _, start, end = schedule_of(airport)
    local = local_now(airport, now_utc)
    current = local.hour * 60 + local.minute
    begin, finish = minutes_of(start), minutes_of(end)
    if begin < finish:
        is_day = begin <= current < finish
    else:
        is_day = current >= begin or current < finish
    return SHIFT_DAY if is_day else SHIFT_NIGHT


def info(airport, now_utc=None):
    """Расписание и текущее положение дел — для экрана выбора смены."""
    tz_name, start, end = schedule_of(airport)
    return {
        "timezone": tz_name,
        "day_start": start,
        "day_end": end,
        "local_time": local_now(airport, now_utc).strftime("%H:%M"),
        "current_shift": shift_at(airport, now_utc),
    }
