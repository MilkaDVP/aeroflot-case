"""Схемы графа аэропорта и воздушных судов."""

from pydantic import BaseModel, Field


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
    # Происхождение графа: builtin и osm — данные OpenStreetMap, manual —
    # размечен администратором. Нужно для честной подписи под картой.
    source: str = "builtin"


class AirportImportRequest(BaseModel):
    """
    Добавление аэропорта по коду ИКАО.

    Границы лётного поля берутся из OpenStreetMap по самому коду —
    администратору не нужно знать координаты рамки.
    """

    icao: str = Field(min_length=4, max_length=4)
    # Служебные проезды перрона: по ним ездит спецтранспорт, но размечены
    # они не везде и местами дублируют рулёжки. По умолчанию не берём.
    service_roads: bool = False


class AirportCreateRequest(BaseModel):
    """Создание пустого аэропорта, который администратор нарисует сам."""

    icao: str = Field(min_length=3, max_length=8)
    name: str = Field(min_length=1)
    city: str = ""
    # Точка, вокруг которой откроется карта редактора: без неё неизвестно,
    # какой участок земли показывать.
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class NodeInput(BaseModel):
    """Вершина, поставленная администратором на карте."""

    id: str
    type: str = Field(description="stand, tech_center или junction")
    ref: str | None = None
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class EdgeInput(BaseModel):
    """Связь между двумя вершинами. Длина считается сервером по координатам."""

    from_id: str
    to_id: str
    vehicle_allowed: bool = True


class GraphUpdateRequest(BaseModel):
    """
    Сохранение нарисованного графа целиком.

    Граф приходит полностью, а не изменениями: редактор работает с ним
    в памяти браузера, и частичные правки пришлось бы сверять с версией
    на сервере ради выигрыша в несколько килобайт.
    """

    nodes: list[NodeInput]
    edges: list[EdgeInput]


class AirportSummary(BaseModel):
    """Аэропорт в списке администратора: размер графа и происхождение."""

    icao: str
    name: str
    city: str
    source: str
    nodes: int
    stands: int
    tech_centers: int
    edges: int
    # Можно ли редактировать: встроенные графы лежат в файлах репозитория
    # и правятся скриптом выгрузки, а не через интерфейс.
    editable: bool


class AircraftCreateRequest(BaseModel):
    """
    Постановка борта на стоянку. Доступно только администратору.

    Стоянка указывается узлом графа: только так вызов на этот борт получит
    маршрут. Тип ВС — из справочника: по нему определяется подкатегория
    допуска, и неизвестный тип дал бы неверный подбор.
    """

    board_number: str = Field(min_length=1, description="Бортовой номер, например RA-89001")
    aircraft_type: str = Field(description="Тип ВС из справочника, например A320")
    stand_node_id: str


class AircraftResponse(BaseModel):
    """Борт на стоянке."""

    id: int
    board_number: str
    aircraft_type: str
    airport_icao: str
    stand_node_id: str
    stand_ref: str | None
