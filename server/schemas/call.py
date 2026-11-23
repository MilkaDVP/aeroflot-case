"""Схемы вызовов, подбора и назначения."""

from pydantic import BaseModel, Field


class CallCreateRequest(BaseModel):
    """Регистрация вызова диспетчером: борт, тип неисправности и описание."""

    aircraft_id: int
    # Тип из справочника. Не выбран — определится по описанию экипажа.
    defect_code: str | None = None
    # Сообщение экипажа своими словами. Необязательно: тип неисправности
    # уже даёт подбору всё нужное, описание — подробности для инженера.
    description: str | None = Field(default=None, max_length=500)
    # Срочный вызов стоит первым в списке диспетчера.
    urgent: bool = False
    # Нужен инструмент со склада: маршрут исполнителя пройдёт через склад.
    needs_tools: bool = False
    # Промежуточные точки маршрута по порядку — узлы графа (склад, стоянка
    # спецтехники, любая точка). Пусто — маршрут прямо к борту.
    waypoints: list[str] = Field(default_factory=list, max_length=10)


class WaypointsRequest(BaseModel):
    """Промежуточные точки маршрута по вызову — узлы графа по порядку."""

    node_ids: list[str] = Field(default_factory=list, max_length=10)


class CallControl(BaseModel):
    """
    Контроль регламента по вызову на момент ответа.

    Прогноз прибытия пересчитывается от текущих координат исполнителя при
    каждом запросе. alert — красное уведомление диспетчеру: срок будет
    сорван, уже сорван, исполнителя нет или его координатам нельзя верить.
    """

    level: str = Field(
        description="ok, warn, late, overdue, unassigned, no_signal, pending, arrived, closed"
    )
    alert: bool
    reason: str
    advice: str | None = None
    deadline_at: str
    minutes_left: float
    forecast_at: str | None = None
    margin_min: float | None = None
    # Отставание прогноза от плана, записанного при назначении.
    delay_min: float | None = None
    position_age_s: int | None = None


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
    defect_ata: str | None = None
    defect_category: str | None = None
    defect_description: str | None = None
    required_mark: str
    status: str
    created_at: str
    closed_at: str | None
    # Сообщение экипажа и его источник — если вызов создан из заявки.
    reported_at: str | None = None
    source: str | None = None
    urgent: bool = False
    # AOG ставит только инженер после осмотра.
    aog: bool = False
    work_stage: str | None = None
    work_stage_title: str | None = None
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
    # Инструмент со склада: нужен ли, получен ли, и промежуточные точки
    # маршрута (склад, посадка, высадка) с задержками.
    needs_tools: bool = False
    tools_taken_at: str | None = None
    route_stops: list[dict] = []
    waypoints: list[dict] = []
    # С кого сняли назначение, когда и почему. Остаётся в вызове после
    # переназначения: диспетчеру видно, что борт уже ждал другого.
    previous_employee_name: str | None = None
    unassign_reason: str | None = None
    unassigned_at: str | None = None
    control: CallControl | None = None
    # Основной вызов, если это вызов поддержки (второй инженер на борт).
    parent_call_id: int | None = None
    # Инженеры поддержки основного вызова: кто, в каком статусе, когда будет.
    support: list[dict] = []
    # Переписка инженеров с диспетчером по вызову, старые первыми.
    messages: list[dict] = []
    # Кто из диспетчеров разбирает вызов, пока он ждёт назначения.
    claimed_by_login: str | None = None
    claimed_by_name: str | None = None
    claim_expires_at: str | None = None


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
    # Промежуточные точки и остановки: склад, посадка, высадка.
    stops: list[dict] = []
    dwell_s: float = 0


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
    # Чем занят и можно ли переключить его на этот вызов: начатый ремонт
    # не прерывают, ожидание запчастей — можно.
    current_board: str | None = None
    work_stage_title: str | None = None
    switchable: bool = False
    switch_note: str | None = None


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
    # Переключить занятого исполнителя, ждущего запчасти: его текущий вызов
    # уходит в его же очередь, а этот становится текущим.
    switch: bool = False
    # Как добираться: auto, walk или with_lead (см. constants.TRAVEL_MODES).
    travel: str = "auto"


class MessageRequest(BaseModel):
    """Сообщение по вызову — от инженера диспетчеру или обратно."""

    text: str = Field(min_length=1, max_length=500)


class ClaimRequest(BaseModel):
    """Взять вызов в работу. takeover — забрать у другого (начальник смены)."""

    takeover: bool = False


class UrgencyRequest(BaseModel):
    """Срочность вызова выставляет диспетчер."""

    urgent: bool


class UnassignRequest(BaseModel):
    """
    Снятие исполнителя с вызова.

    Причина обязательна: снятие означает, что борт ждал зря, и в разборе
    инцидента должно быть видно, чьё это решение и почему.
    """

    reason: str = Field(min_length=3)


class CallStatusRequest(BaseModel):
    """Смена статуса вызова."""

    status: str = Field(description="new, suggested, assigned, accepted, arrived, closed, cancelled")


class DefectTypeResponse(BaseModel):
    """Запись справочника неисправностей."""

    code: str
    name: str
    ata: str
    ata_title: str = ""
    category: str
    keywords: str = ""
    is_builtin: bool = True


class DefectTypeCreateRequest(BaseModel):
    """
    Новый тип неисправности, заведённый диспетчером.

    Категория необязательна: по умолчанию берётся та, что обычно требуется
    по главе ATA. Диспетчер меняет её, когда знает лучше — например, для
    посадочных фар, которые по главе «Освещение», но работа уровня B1.
    """

    name: str = Field(min_length=3, max_length=120)
    ata: str = Field(description="Номер главы ATA, например 29")
    category: str | None = Field(default=None, description="A, B1 или B2")
    keywords: str = Field(default="", max_length=200)


class AtaChapterResponse(BaseModel):
    """Глава ATA и категория допуска, которая по ней требуется обычно."""

    ata: str
    title: str
    default_category: str
