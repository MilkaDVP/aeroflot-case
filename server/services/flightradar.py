"""Онлайн-загрузка бортов из официального Flightradar24 API.

Внешний сервис возвращает live-позиции, а не готовый список бортов на
стоянках. Поэтому здесь выполняется прикладное преобразование: регистрация
и тип ВС сопоставляются со справочником проекта, а координата — с ближайшей
стоянкой графа. Сырые ответы API и история позиций не сохраняются.
"""

import os
from datetime import datetime, timezone
from time import monotonic

from fr24sdk.client import Client
from fr24sdk.exceptions import (
    ApiError,
    AuthenticationError,
    NoApiKeyError,
    PaymentRequiredError,
    RateLimitError,
    TransportError,
)

from algorithm.geo import haversine_m
from constants import AIRCRAFT_GROUP_BY_TYPE
from models.aircraft import Aircraft
from schemas.flightradar import FlightradarSyncItem, FlightradarSyncResponse

MAX_STAND_DISTANCE_M = 250.0
MAX_GROUND_SPEED_KNOTS = 30
POSITIONS_LIMIT = 500
BOUNDS_PADDING_DEGREES = 0.005

# FR24 отдаёт точные обозначения моделей, а квалификационный справочник
# проекта работает с семействами. Неизвестный тип не угадываем: такой борт
# попадёт в пропущенные с понятной причиной.
AIRCRAFT_TYPE_ALIASES = {
    "A319": "A319",
    "A19N": "A319",
    "A320": "A320",
    "A20N": "A320",
    "A321": "A321",
    "A21N": "A321",
    "A330": "A330",
    "A332": "A330",
    "A333": "A330",
    "A338": "A330",
    "A339": "A330",
    "A350": "A350",
    "A359": "A350",
    "A35K": "A350",
    "B737": "B737",
    "B736": "B737",
    "B738": "B737",
    "B739": "B737",
    "B73G": "B737",
    "B73H": "B737",
    "B73J": "B737",
    "B37M": "B737",
    "B38M": "B737",
    "B39M": "B737",
    "B777": "B777",
    "B772": "B777",
    "B773": "B777",
    "B77L": "B777",
    "B77W": "B777",
    "B778": "B777",
    "B779": "B777",
    "B787": "B787",
    "B788": "B787",
    "B789": "B787",
    "B78X": "B787",
    "SU95": "SSJ-100",
    "SSJ100": "SSJ-100",
    "SSJ-100": "SSJ-100",
    "MC21": "MC-21",
    "MC-21": "MC-21",
    "T214": "Ту-214",
    "ТУ-214": "Ту-214",
    "IL96": "Ил-96",
    "ИЛ-96": "Ил-96",
    "L410": "L-410",
    "L-410": "L-410",
    "AN2": "Ан-2",
    "АН-2": "Ан-2",
    "MI8": "Ми-8",
    "МИ-8": "Ми-8",
    "KA32": "Ка-32",
    "КА-32": "Ка-32",
    "R44": "Robinson R44",
    "ROBINSON R44": "Robinson R44",
}


class FlightradarError(Exception):
    """Ошибка внешнего сервиса в форме, безопасной для ответа клиенту."""

    def __init__(self, detail, http_status=502):
        super().__init__(detail)
        self.detail = detail
        self.http_status = http_status


def normalize_aircraft_type(source_type):
    """Переводит обозначение FR24 в тип из квалификационного справочника."""
    if not source_type:
        return None
    key = source_type.strip().upper()
    normalized = AIRCRAFT_TYPE_ALIASES.get(key, key)
    return normalized if normalized in AIRCRAFT_GROUP_BY_TYPE else None


def airport_bounds(graph):
    """Рамка N,S,W,E вокруг стоянок — обязательный фильтр live-запроса."""
    stands = graph.nodes_of_type("stand")
    if not stands:
        raise FlightradarError("В аэропорту нет стоянок для сопоставления бортов", 409)

    padding = BOUNDS_PADDING_DEGREES
    north = max(node["lat"] for node in stands) + padding
    south = min(node["lat"] for node in stands) - padding
    west = min(node["lon"] for node in stands) - padding
    east = max(node["lon"] for node in stands) + padding
    return f"{north:.6f},{south:.6f},{west:.6f},{east:.6f}"


def fetch_flightradar_positions(graph):
    """Получает live-позиции через официальный SDK, не раскрывая ключ клиенту."""
    token = os.environ.get("FR24_API_TOKEN", "").strip()
    if not token:
        raise FlightradarError(
            "Не задан FR24_API_TOKEN. Добавьте ключ Flightradar24 в окружение "
            "сервера и перезапустите его",
            503,
        )

    try:
        timeout = float(os.environ.get("FR24_API_TIMEOUT_SECONDS", "15"))
    except ValueError as error:
        raise FlightradarError("FR24_API_TIMEOUT_SECONDS должен быть числом", 503) from error

    options = {"api_token": token, "timeout": timeout}
    base_url = os.environ.get("FR24_API_BASE_URL", "").strip()
    if base_url:
        options["base_url"] = base_url

    try:
        with Client(**options) as client:
            response = client.live.flight_positions.get_full(
                bounds=airport_bounds(graph),
                limit=POSITIONS_LIMIT,
            )
            return response.data or []
    except NoApiKeyError as error:
        raise FlightradarError("Не задан FR24_API_TOKEN", 503) from error
    except AuthenticationError as error:
        # Нельзя возвращать 401 нашему клиенту: он воспримет его как конец
        # собственной сессии и выйдет из учётной записи администратора.
        raise FlightradarError("Flightradar24 отклонил ключ доступа", 502) from error
    except PaymentRequiredError as error:
        raise FlightradarError("На счету Flightradar24 недостаточно кредитов", 503) from error
    except RateLimitError as error:
        raise FlightradarError("Превышен лимит запросов Flightradar24", 503) from error
    except TransportError as error:
        raise FlightradarError("Flightradar24 недоступен. Повторите попытку позже", 503) from error
    except ApiError as error:
        raise FlightradarError("Flightradar24 вернул ошибку запроса", 502) from error


def sync_flightradar_aircraft(db, graph, positions, started=None):
    """Создаёт новые борта по live-позициям и возвращает построчный результат.

    Существующие борта не перемещаются: у них могут быть активные вызовы и
    построенные маршруты. Занятая стоянка тоже не переназначается — конфликт
    показывается администратору вместо молчаливой подмены операционных данных.
    """
    started = monotonic() if started is None else started
    icao = graph.icao
    stands = graph.nodes_of_type("stand")
    if not stands:
        raise FlightradarError("В аэропорту нет стоянок для сопоставления бортов", 409)

    existing = db.query(Aircraft).filter(Aircraft.airport_icao == icao).all()
    by_board = {item.board_number: item for item in existing}
    by_stand = {item.stand_node_id: item for item in existing}

    items = []
    created = 0
    existing_count = 0
    seen_boards = set()
    ordered = sorted(
        positions, key=lambda item: (item.reg or "", str(item.fr24_id or ""))
    )

    for position in ordered:
        board = (position.reg or "").strip().upper()
        if not board or board in seen_boards:
            continue
        seen_boards.add(board)

        source_type = (position.type or "").strip().upper() or None
        aircraft_type = normalize_aircraft_type(source_type)
        stand_ref = None

        def result(status, detail):
            return FlightradarSyncItem(
                board_number=board,
                source_type=source_type,
                aircraft_type=aircraft_type,
                stand_ref=stand_ref,
                status=status,
                detail=detail,
            )

        if board in by_board:
            existing_count += 1
            items.append(result("existing", "Борт уже есть в этом аэропорте"))
            continue
        if aircraft_type is None:
            items.append(result("skipped", "Тип ВС отсутствует в справочнике проекта"))
            continue
        if position.gspeed is None or position.gspeed > MAX_GROUND_SPEED_KNOTS:
            items.append(result("skipped", "Скорость выше порога стоянки или неизвестна"))
            continue

        stand = min(
            stands,
            key=lambda node: haversine_m(
                position.lat, position.lon, node["lat"], node["lon"]
            ),
        )
        distance = haversine_m(position.lat, position.lon, stand["lat"], stand["lon"])
        stand_ref = stand["ref"] or stand["id"]
        if distance > MAX_STAND_DISTANCE_M:
            items.append(result("skipped", f"До ближайшей стоянки {distance:.0f} м"))
            continue
        if stand["id"] in by_stand:
            occupied = by_stand[stand["id"]]
            items.append(
                result("skipped", f"Стоянка занята бортом {occupied.board_number}")
            )
            continue

        aircraft = Aircraft(
            board_number=board,
            aircraft_type=aircraft_type,
            airport_icao=icao,
            stand_node_id=stand["id"],
        )
        db.add(aircraft)
        by_board[board] = aircraft
        by_stand[stand["id"]] = aircraft
        created += 1
        items.append(result("created", f"Загружен на стоянку {stand_ref}"))

    db.commit()
    return FlightradarSyncResponse(
        airport_icao=icao,
        fetched_at=datetime.now(timezone.utc),
        received=len(positions),
        created=created,
        existing=existing_count,
        skipped=len(items) - created - existing_count,
        elapsed_ms=round((monotonic() - started) * 1000, 1),
        items=items,
    )
