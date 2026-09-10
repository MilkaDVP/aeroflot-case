"""Аэропорты, их графы и воздушные суда на стоянках."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.auth import airport_briefs
from auth.dependencies import Context, get_context, require_airport
from database import get_db
from graph_registry import get_graph
from models.aircraft import Aircraft
from models.airport import Airport
from schemas.airport import AircraftResponse, AirportGraphResponse
from schemas.auth import AirportBrief

router = APIRouter(prefix="/api", tags=["Аэропорты"])


@router.get("/airports", response_model=list[AirportBrief], summary="Доступные аэропорты")
def list_airports(context: Context = Depends(get_context)):
    """Только те аэропорты, к которым привязана учётная запись."""
    return airport_briefs(context.user)


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

    return [
        AircraftResponse(
            id=item.id,
            board_number=item.board_number,
            aircraft_type=item.aircraft_type,
            airport_icao=item.airport_icao,
            stand_node_id=item.stand_node_id,
            stand_ref=stand_ref(graph, item.stand_node_id),
        )
        for item in aircraft
    ]


def stand_ref(graph, node_id):
    """Номер стоянки по узлу графа — диспетчер мыслит номерами, не узлами."""
    node = graph.node(node_id)
    return node["ref"] if node else None
