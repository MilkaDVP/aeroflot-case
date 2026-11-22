"""Аэропорты, их графы и воздушные суда на стоянках."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from algorithm.geo import haversine_m
from api.auth import airport_briefs, brief_of
from auth.dependencies import (
    Context,
    get_context,
    require_airport,
    require_roles,
    require_roles_in_airport,
)
from constants import AIRCRAFT_GROUP_BY_TYPE
from database import get_db, get_dispatch_db
from graph_registry import get_graph
from models.aircraft import Aircraft
from models.airport import Airport
from models.call import ACTIVE_CALL_STATUSES, STATUS_QUEUED, Call
from models.user import DISPATCH_ROLES, ROLE_ADMIN, ROLE_LOCAL_ADMIN, ROLE_SHIFT_SUPERVISOR
from schemas.airport import AircraftCreateRequest, AircraftResponse, AirportGraphResponse
from schemas.employee import LocationRequest
from services.assignment import assigned_route, replan_queue, update_assigned_route
from schemas.auth import AirportBrief, ShiftScheduleRequest
from services import shift_clock

router = APIRouter(prefix="/api", tags=["Аэропорты"])

ERROR_AIRPORT_NOT_FOUND = "Аэропорт {icao} не заведён в системе"
ERROR_FOREIGN_AIRPORT = "Расписание смен меняют только в своём аэропорту"
ERROR_NOT_A_STAND = "Узел {node} не является стоянкой этого аэропорта"
ERROR_UNKNOWN_TYPE = (
    "Тип ВС {type} не описан в справочнике: по нему определяется допуск. "
    "Известные типы: {known}"
)
ERROR_DUPLICATE_BOARD = "Борт {board} уже стоит в этом аэропорту"
ERROR_AIRCRAFT_NOT_FOUND = "Борт не найден"
ERROR_AIRCRAFT_HAS_CALL = (
    "По борту {board} открыт вызов. Закройте его прежде, чем убирать борт"
)


@router.get("/airports", response_model=list[AirportBrief], summary="Доступные аэропорты")
def list_airports(context: Context = Depends(get_context), db: Session = Depends(get_db)):
    """Аэропорты учётной записи; техническому администратору — все."""
    return airport_briefs(db, context.user)


@router.put(
    "/airports/{icao}/shift-schedule",
    response_model=AirportBrief,
    summary="Расписание смен",
)
def update_shift_schedule(
    icao: str,
    payload: ShiftScheduleRequest,
    context: Context = Depends(require_roles(ROLE_SHIFT_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """
    Часовой пояс и граница дневной смены. По ним при входе сама
    выбирается идущая смена. Начальник смены и администратор аэропорта
    меняют расписание своего аэропорта, технический администратор — любого.
    """
    icao = icao.upper()
    if context.user.role != ROLE_ADMIN and icao not in context.user.airport_icaos:
        raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_FOREIGN_AIRPORT)
    airport = db.get(Airport, icao)
    if airport is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_AIRPORT_NOT_FOUND.format(icao=icao))

    try:
        shift_clock.validate(payload.timezone, payload.day_start, payload.day_end)
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error

    airport.timezone = payload.timezone
    airport.day_shift_start = payload.day_start
    airport.day_shift_end = payload.day_end
    db.commit()
    return brief_of(airport)


@router.get(
    "/airports/{icao}", response_model=AirportGraphResponse, summary="Граф аэропорта"
)
def get_airport_graph(
    icao: str,
    context: Context = Depends(get_context),
    db: Session = Depends(get_db),
):
    """Узлы и рёбра для отрисовки карты. Запрашивается один раз при входе."""
    graph = get_graph(icao)
    airport = db.get(Airport, icao.upper())

    return AirportGraphResponse(
        icao=graph.icao,
        name=airport.name if airport else graph.name,
        city=airport.city if airport else graph.city,
        ref_point=graph.ref_point,
        nodes=list(graph.nodes.values()),
        edges=graph.edges,
        source=airport.source if airport else "builtin",
    )


@router.get("/aircraft", response_model=list[AircraftResponse], summary="ВС на стоянках")
def list_aircraft(
    context: Context = Depends(require_airport),
    db: Session = Depends(get_db),
):
    """Борта текущего аэропорта с номерами стоянок."""
    graph = get_graph(context.airport_icao)
    aircraft = (
        db.query(Aircraft)
        .filter(Aircraft.airport_icao == context.airport_icao)
        .order_by(Aircraft.board_number)
        .all()
    )

    return [aircraft_response(graph, item) for item in aircraft]


def stand_ref(graph, node_id):
    """Номер стоянки по узлу графа — диспетчер мыслит номерами, не узлами."""
    node = graph.node(node_id)
    return node["ref"] if node else None


def aircraft_response(graph, item):
    """Модель борта в схему ответа с номером стоянки."""
    return AircraftResponse(
        id=item.id,
        board_number=item.board_number,
        aircraft_type=item.aircraft_type,
        airport_icao=item.airport_icao,
        stand_node_id=item.stand_node_id,
        stand_ref=stand_ref(graph, item.stand_node_id),
    )


@router.post(
    "/aircraft",
    response_model=AircraftResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Поставить борт на стоянку",
)
def create_aircraft(
    payload: AircraftCreateRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles_in_airport(ROLE_LOCAL_ADMIN)),
):
    """
    Ставит борт на стоянку аэропорта сессии.

    Без бортов в новом аэропорту нельзя зарегистрировать вызов, а значит
    и проверить подбор — поэтому борт заводится тем же администратором,
    что и сам аэропорт.

    Стоянка обязана быть стоянкой графа, а тип ВС — известным справочнику.
    Иначе ошибка всплыла бы только при регистрации вызова, у борта,
    которого уже ждут.
    """
    graph = get_graph(context.airport_icao)
    node = graph.node(payload.stand_node_id)
    if node is None or node["type"] != "stand":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ERROR_NOT_A_STAND.format(node=payload.stand_node_id),
        )

    if payload.aircraft_type not in AIRCRAFT_GROUP_BY_TYPE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ERROR_UNKNOWN_TYPE.format(
                type=payload.aircraft_type,
                known=", ".join(sorted(AIRCRAFT_GROUP_BY_TYPE)),
            ),
        )

    board_number = payload.board_number.strip().upper()
    duplicate = (
        db.query(Aircraft)
        .filter(
            Aircraft.airport_icao == context.airport_icao,
            Aircraft.board_number == board_number,
        )
        .first()
    )
    if duplicate is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, ERROR_DUPLICATE_BOARD.format(board=board_number)
        )

    check_stand_available(db, context.airport_icao, node)
    aircraft = Aircraft(
        board_number=board_number,
        aircraft_type=payload.aircraft_type,
        airport_icao=context.airport_icao,
        stand_node_id=payload.stand_node_id,
    )
    db.add(aircraft)
    db.commit()
    return aircraft_response(graph, aircraft)


def check_stand_available(db, icao, node, aircraft_id=None):
    occupied = db.query(Aircraft).filter(
        Aircraft.airport_icao == icao,
        Aircraft.stand_node_id == node["id"],
        Aircraft.id != aircraft_id,
    ).first()
    if occupied:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Стоянка {node['ref']} занята бортом {occupied.board_number}")


@router.post("/aircraft/{aircraft_id}/location", response_model=AircraftResponse, summary="Переместить борт")
def move_aircraft(
    aircraft_id: int,
    payload: LocationRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles_in_airport(*DISPATCH_ROLES)),
):
    aircraft = db.get(Aircraft, aircraft_id)
    if aircraft is None or aircraft.airport_icao != context.airport_icao:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_AIRCRAFT_NOT_FOUND)
    graph = get_graph(context.airport_icao)
    stand = min(graph.nodes_of_type("stand"), default=None,
                key=lambda node: haversine_m(payload.lat, payload.lon, node["lat"], node["lon"]))
    if stand is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "В аэропорту нет стоянок")
    check_stand_available(db, context.airport_icao, stand, aircraft.id)
    if aircraft.stand_node_id == stand["id"]:
        return aircraft_response(graph, aircraft)

    aircraft.stand_node_id = stand["id"]
    calls = db.query(Call).filter(
        Call.aircraft_id == aircraft.id, Call.status.in_(ACTIVE_CALL_STATUSES)
    ).all()
    employees = set()
    for call in calls:
        call.stand_node_id = stand["id"]
        if call.assigned_employee is None:
            call.suggested_employee_id = None
            continue
        employees.add(call.assigned_employee)
        if call.status != STATUS_QUEUED:
            plan = assigned_route(call, graph)
            if plan is None:
                raise HTTPException(status.HTTP_409_CONFLICT, "Нет маршрута до выбранной стоянки")
            update_assigned_route(call, plan)
    for employee in employees:
        if not replan_queue(db, employee):
            raise HTTPException(status.HTTP_409_CONFLICT, "Нет маршрута между вызовами в очереди")
    db.commit()
    return aircraft_response(graph, aircraft)


@router.delete(
    "/aircraft/{aircraft_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Убрать борт со стоянки",
)
def delete_aircraft(
    aircraft_id: int,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles_in_airport(ROLE_LOCAL_ADMIN)),
):
    """
    Убирает борт. Пока по нему открыт вызов, это запрещено: вызов остался бы
    без борта, и диспетчер не понял бы, к чему он относится.
    """
    aircraft = db.get(Aircraft, aircraft_id)
    if aircraft is None or aircraft.airport_icao != context.airport_icao:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_AIRCRAFT_NOT_FOUND)

    open_call = (
        db.query(Call)
        .filter(Call.aircraft_id == aircraft.id, Call.status.in_(ACTIVE_CALL_STATUSES))
        .first()
    )
    if open_call is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            ERROR_AIRCRAFT_HAS_CALL.format(board=aircraft.board_number),
        )

    db.delete(aircraft)
    db.commit()
