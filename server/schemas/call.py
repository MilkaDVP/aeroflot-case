"""Схемы вызовов, подбора и назначения."""

from pydantic import BaseModel, Field


class CallCreateRequest(BaseModel):
    """Регистрация вызова диспетчером: борт и код дефекта."""

    aircraft_id: int
    defect_code: str


class CallResponse(BaseModel):
    """Вызов в списке активных и в карточке."""

    id: int
    airport_icao: str
    aircraft_id: int
    board_number: str
    aircraft_type: str
    stand_node_id: str
    # Номер стоянки на перроне. Диспетчер мыслит номерами, а не
    # идентификаторами узлов графа.
    stand_ref: str | None
    defect_code: str
    defect_name: str
    required_mark: str
    status: str
    created_at: str
    closed_at: str | None
    suggested_employee_id: int | None
    assigned_employee_id: int | None
    assigned_employee_name: str | None
    override_reason: str | None
    eta_minutes: float | None
    route_node_ids: list[str] | None
    # Ожидаемое прибытие, UTC с поясом. Для вызова в очереди «через N минут»
    # теряет смысл — инженер сначала закончит текущие работы, — поэтому
    # отдаётся абсолютное время: по нему видно, насколько нарушен регламент.
    eta_at: str | None = None
    # Когда вызов поставлен в очередь к занятому инженеру.
    queued_at: str | None = None
    # Машина, на которой исполнитель едет к борту, и узел, где он её
    # забирает (пусто — машина уже при нём или он идёт пешком).
    vehicle_call_sign: str | None = None
    pickup_node_id: str | None = None


class LegSchema(BaseModel):
    """Участок маршрута одним способом передвижения."""

    node_ids: list[str]
    distance_m: float
    approach_m: float
    mode: str
    minutes: float


class RouteSchema(BaseModel):
    """
    Маршрут кандидата для отрисовки на карте.

    Через свободную машину маршрут из двух участков: пешком до машины
    и на машине до борта. Карта рисует их по-разному, иначе диспетчер
    не отличит «идёт пешком» от «едет».
    """

    node_ids: list[str]
    distance_m: float
    approach_m: float
    mode: str = Field(description="walk, vehicle или walk_vehicle")
    minutes: float
    legs: list[LegSchema] = []
    vehicle: dict | None = None
    # Узел, где исполнитель забирает машину; пусто — машина не нужна
    # или уже при нём.
    pickup_node_id: str | None = None


class CandidateSchema(BaseModel):
    """Кандидат с временем прибытия и признаками соблюдения регламента."""

    employee_id: int
    full_name: str
    mark: str
    valid_until: str
    has_vehicle: bool
    # Позывной машины, на которой он поедет, и нужно ли её сначала забрать.
    vehicle_call_sign: str | None = None
    pickup: bool = False
    minutes: float
    within_regulation: bool
    near_limit: bool
    route: RouteSchema


class RejectedSchema(BaseModel):
    """Отсеянный сотрудник и конкретная причина отказа."""

    employee_id: int
    full_name: str
    reason: str


class BusySchema(BaseModel):
    """Сотрудник с допуском, занятый на другом вызове."""

    employee_id: int
    full_name: str
    mark: str
    status: str
    busy_until: str | None


class SuggestResponse(BaseModel):
    """
    Результат подбора целиком.

    Кроме рекомендации отдаются основания: кто отсеян и почему, кто занят
    и когда освободится. Диспетчер отвечает за назначение лично и обязан
    видеть, что именно система от него скрыла.
    """

    required_mark: str
    within_regulation: bool
    message: str
    elapsed_ms: float
    best: CandidateSchema | None
    candidates: list[CandidateSchema]
    rejected: list[RejectedSchema]
    busy: list[BusySchema]


class AssignRequest(BaseModel):
    """
    Назначение исполнителя.

    Если назначен не тот, кого предложила система, причина обязательна —
    это требование ТЗ к роли начальника смены.
    """

    employee_id: int
    override_reason: str | None = None


class CallStatusRequest(BaseModel):
    """Смена статуса вызова."""

    status: str = Field(description="new, suggested, assigned, accepted, arrived, closed")


class DefectTypeResponse(BaseModel):
    """Запись справочника неисправностей."""

    code: str
    name: str
    ata: str
    category: str
