"""Бесплатные live-позиции ADSB.lol и их адаптация к общей синхронизации.

ADSB.lol не требует API-ключа для базового доступа. Сервис отдаёт формат
readsb: регистрация — ``r``, тип ВС — ``t``, скорость — ``gs``. Здесь эти
поля преобразуются в объект, который понимает существующий алгоритм
сопоставления бортов со стоянками. Сырой ответ и внешние ID не сохраняются.
"""

import math
import os
from types import SimpleNamespace

import httpx

from algorithm.geo import haversine_m
from services.flightradar import FlightradarError

DEFAULT_BASE_URL = "https://api.adsb.lol"
MAX_POSITION_AGE_SECONDS = 60.0
MAX_RADIUS_NM = 10


def airport_radius_nm(graph):
    """Целый радиус в морских милях, покрывающий все стоянки аэропорта."""
    stands = graph.nodes_of_type("stand")
    if not stands:
        raise FlightradarError("В аэропорту нет стоянок для сопоставления бортов", 409)

    centre = graph.centre
    farthest_m = max(
        haversine_m(centre["lat"], centre["lon"], stand["lat"], stand["lon"])
        for stand in stands
    )
    # Добавляем одну милю к самой удалённой стоянке: борт может находиться
    # на соседней рулёжке, а общий алгоритм всё равно отберёт только 250 м.
    return min(MAX_RADIUS_NM, max(1, math.ceil(farthest_m / 1852.0) + 1))


def finite_number(value):
    """Возвращает только конечное число, отсекая строки, bool и NaN."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def fetch_adsblol_positions(graph):
    """Получает свежие позиции вокруг аэропорта без ключа и фонового опроса."""
    try:
        timeout = float(os.environ.get("ADSBLOL_API_TIMEOUT_SECONDS", "15"))
        if timeout <= 0 or not math.isfinite(timeout):
            raise ValueError
    except ValueError as error:
        raise FlightradarError(
            "ADSBLOL_API_TIMEOUT_SECONDS должен быть положительным числом", 503
        ) from error

    base_url = os.environ.get("ADSBLOL_API_BASE_URL", DEFAULT_BASE_URL).strip()
    base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
    centre = graph.centre
    url = (
        f"{base_url}/v2/lat/{centre['lat']:.6f}"
        f"/lon/{centre['lon']:.6f}/dist/{airport_radius_nm(graph)}"
    )

    try:
        response = httpx.get(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "aeroflot-smart-dispatch/1.0",
            },
            timeout=timeout,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 429:
            raise FlightradarError(
                "Превышен лимит бесплатных запросов ADSB.lol. Повторите позже", 503
            ) from error
        raise FlightradarError("ADSB.lol вернул ошибку запроса", 502) from error
    except (httpx.TimeoutException, httpx.RequestError) as error:
        raise FlightradarError("ADSB.lol недоступен. Повторите попытку позже", 503) from error

    try:
        payload = response.json()
    except ValueError as error:
        raise FlightradarError("ADSB.lol вернул некорректный JSON", 502) from error
    if not isinstance(payload, dict) or not isinstance(payload.get("ac"), list):
        raise FlightradarError("ADSB.lol вернул неожиданную структуру ответа", 502)

    positions = []
    for item in payload["ac"]:
        if not isinstance(item, dict):
            continue
        lat = finite_number(item.get("lat"))
        lon = finite_number(item.get("lon"))
        age = finite_number(item.get("seen_pos"))
        # lastPosition намеренно не используем: для постановки на стоянку
        # нужна наблюдаемая сейчас точка, а не архивная координата.
        if (
            lat is None
            or lon is None
            or age is None
            or age < 0
            or age > MAX_POSITION_AGE_SECONDS
        ):
            continue
        speed = finite_number(item.get("gs"))
        positions.append(
            SimpleNamespace(
                # Общий алгоритм использует внешний ID только для стабильной
                # сортировки. В базу проекта он не записывается.
                fr24_id=item.get("hex") if isinstance(item.get("hex"), str) else None,
                reg=item.get("r") if isinstance(item.get("r"), str) else None,
                type=item.get("t") if isinstance(item.get("t"), str) else None,
                gspeed=speed,
                lat=lat,
                lon=lon,
            )
        )
    return positions
