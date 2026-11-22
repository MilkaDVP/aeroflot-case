"""Ручная онлайн-загрузка воздушных судов из Flightradar24."""

from time import monotonic

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth.dependencies import Context, require_roles_in_airport
from database import begin_dispatch_write, get_db
from graph_registry import get_graph
from models.user import ROLE_ADMIN
from schemas.flightradar import FlightradarSyncResponse
from services.flightradar import (
    FlightradarError,
    fetch_flightradar_positions,
    sync_flightradar_aircraft,
)

router = APIRouter(prefix="/api", tags=["Flightradar24"])


@router.post(
    "/aircraft/flightradar/sync",
    response_model=FlightradarSyncResponse,
    summary="Загрузить борта из Flightradar24",
)
def sync_aircraft_from_flightradar(
    db: Session = Depends(get_db),
    context: Context = Depends(require_roles_in_airport(ROLE_ADMIN)),
):
    """По команде администратора добавляет стоящие борта из live-позиций FR24."""
    graph = get_graph(context.airport_icao)
    started = monotonic()
    try:
        # Внешний HTTP-запрос выполняется до BEGIN IMMEDIATE: ожидание сети
        # не должно блокировать диспетчерские записи в SQLite.
        positions = fetch_flightradar_positions(graph)
        begin_dispatch_write(db)
        return sync_flightradar_aircraft(db, graph, positions, started=started)
    except FlightradarError as error:
        db.rollback()
        raise HTTPException(error.http_status, error.detail) from error
