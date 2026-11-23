"""Ручная бесплатная онлайн-загрузка воздушных судов из ADSB.lol."""

from time import monotonic

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth.dependencies import Context, require_roles_in_airport
from database import begin_dispatch_write, get_db
from graph_registry import get_graph
from models.user import ROLE_ADMIN
from schemas.flightradar import FlightradarSyncResponse
from services.adsblol import fetch_adsblol_positions
from services.flightradar import FlightradarError, sync_flightradar_aircraft

router = APIRouter(prefix="/api", tags=["ADSB.lol"])


@router.post(
    "/aircraft/adsblol/sync",
    response_model=FlightradarSyncResponse,
    summary="Бесплатно загрузить борта из ADSB.lol",
)
def sync_aircraft_from_adsblol(
    db: Session = Depends(get_db),
    context: Context = Depends(require_roles_in_airport(ROLE_ADMIN)),
):
    """Добавляет стоящие борта по свежим live-позициям ADSB.lol без API-ключа."""
    graph = get_graph(context.airport_icao)
    started = monotonic()
    try:
        # Как и для FR24, сеть не удерживает блокировку записи SQLite.
        positions = fetch_adsblol_positions(graph)
        begin_dispatch_write(db)
        return sync_flightradar_aircraft(
            db, graph, positions, started=started, provider="adsblol"
        )
    except FlightradarError as error:
        db.rollback()
        raise HTTPException(error.http_status, error.detail) from error
