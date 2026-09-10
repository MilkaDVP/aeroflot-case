"""Схемы графа аэропорта и воздушных судов."""

from pydantic import BaseModel


class NodeSchema(BaseModel):
    """Вершина графа: стоянка, техцентр или узел рулёжек."""

    id: str
    type: str
    ref: str | None
    lat: float
    lon: float
    x: float
    y: float


class EdgeSchema(BaseModel):
    """Ребро графа: участок рулёжки с длиной и признаком проезда."""

    from_id: str
    to_id: str
    distance_m: float
    vehicle_allowed: bool
    # Промежуточные точки [x, y] для отрисовки изгиба рулёжки. Пустой
    # список — участок рисуется прямой линией между вершинами.
    points: list[list[float]] = []


class AirportGraphResponse(BaseModel):
    """
    Граф аэропорта целиком — веб-диспетчер рисует по нему карту.

    Отдаётся одним запросом при открытии рабочего экрана и больше
    не запрашивается: граф неизменен, а его объём (около 1800 узлов)
    для повторного опроса раз в несколько секунд слишком велик.
    """

    icao: str
    name: str
    city: str
    ref_point: dict
    nodes: list[NodeSchema]
    edges: list[EdgeSchema]


class AircraftResponse(BaseModel):
    """Борт на стоянке."""

    id: int
    board_number: str
    aircraft_type: str
    airport_icao: str
    stand_node_id: str
    stand_ref: str | None
