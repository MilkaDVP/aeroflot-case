"""
Реестр загруженных графов аэропортов.

Граф читается с диска один раз и живёт в памяти всё время работы сервера.
Иначе каждый подбор начинался бы с разбора JSON на 700 килобайт, а подбор
вызывается на каждый вызов и при каждом пересчёте маршрута.
"""

from fastapi import HTTPException, status

from algorithm.graph import load_airport
from constants import AIRPORTS_DIR

ERROR_NO_GRAPH = "Граф аэропорта {icao} не найден"

# Кэш графов по коду ИКАО.
_graphs = {}


def available_icaos():
    """Коды аэропортов, для которых на диске есть подготовленный граф."""
    if not AIRPORTS_DIR.exists():
        return []
    return sorted(path.stem for path in AIRPORTS_DIR.glob("*.json"))


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


def preload_all():
    """
    Загружает все доступные графы при старте сервера.

    Первый вызов диспетчера не должен ждать чтения файла: лучше потратить
    секунду на старте, чем секунду в момент реального вызова к борту.
    """
    for icao in available_icaos():
        get_graph(icao)
    return list(_graphs)
