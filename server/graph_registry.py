"""
Реестр загруженных графов аэропортов.

Граф читается один раз и живёт в памяти всё время работы сервера. Иначе
каждый подбор начинался бы с разбора JSON на 700 килобайт, а подбор
вызывается на каждый вызов и при каждом пересчёте маршрута.

Источников два: файлы репозитория (встроенные аэропорты) и база (аэропорты,
добавленные администратором — выгруженные из OSM или нарисованные руками).
Для остальной системы разницы нет: она спрашивает граф по коду ИКАО.
"""

from fastapi import HTTPException, status

from algorithm.graph import AirportGraph, load_airport
from constants import AIRPORTS_DIR

ERROR_NO_GRAPH = "Граф аэропорта {icao} не найден"

# Кэш графов по коду ИКАО.
_graphs = {}

# Коды аэропортов, чьи графы пришли из базы, а не из файлов. Нужны отдельно:
# по ним нельзя обратиться к диску, и в списке доступных они должны
# появляться наравне со встроенными.
_from_database = set()


def file_icaos():
    """Коды аэропортов, для которых на диске есть подготовленный граф."""
    if not AIRPORTS_DIR.exists():
        return []
    return sorted(path.stem for path in AIRPORTS_DIR.glob("*.json"))


def available_icaos():
    """Все аэропорты, готовые к работе: из файлов и из базы."""
    return sorted(set(file_icaos()) | _from_database)


def get_graph(icao):
    """Граф аэропорта из кэша, при первом обращении — с диска."""
    key = icao.upper()
    if key not in _graphs:
        try:
            _graphs[key] = load_airport(key)
        except FileNotFoundError:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ERROR_NO_GRAPH.format(icao=key)
            )
    return _graphs[key]


def register(payload):
    """
    Кладёт в реестр граф, пришедший не из файла.

    Вызывается при загрузке из базы на старте и сразу после того, как
    администратор создал или изменил аэропорт: карта должна открыться
    без перезапуска сервера.
    """
    graph = AirportGraph(payload)
    _graphs[graph.icao] = graph
    _from_database.add(graph.icao)
    return graph


def forget(icao):
    """Убирает аэропорт из реестра — после удаления администратором."""
    key = icao.upper()
    _graphs.pop(key, None)
    _from_database.discard(key)


def load_from_database(db):
    """
    Загружает в реестр графы аэропортов, созданных во время работы.

    Вызывается на старте: без этого аэропорт, добавленный администратором,
    пропал бы после перезапуска контейнера, хотя в базе он есть.
    """
    # Импорт внутри функции: модели тянут за собой базу, а реестром
    # пользуются и там, где база не нужна (тесты алгоритма).
    from models.airport import Airport

    loaded = []
    for airport in db.query(Airport).filter(Airport.graph.isnot(None)):
        register(airport.graph)
        loaded.append(airport.icao)
    return loaded


def preload_all():
    """
    Загружает все доступные графы при старте сервера.

    Первый вызов диспетчера не должен ждать чтения файла: лучше потратить
    секунду на старте, чем секунду в момент реального вызова к борту.
    """
    for icao in file_icaos():
        get_graph(icao)
    return list(_graphs)
