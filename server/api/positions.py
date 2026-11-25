"""
Места людей, машин и бортов — для карты, дважды в секунду.

Только координаты: остальное экран берёт общим опросом раз в 4 секунды.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from auth.dependencies import Context, get_context, require_roles_in_airport
from clock import to_iso_utc
from constants import VEHICLE_IN_USE
from database import get_db
from graph_registry import get_graph
from models.aircraft import Aircraft
from models.employee import Employee
from models.user import DISPATCH_ROLES
from models.vehicle import Vehicle

router = APIRouter(prefix="/api", tags=["Места на карте"])


@router.get("/positions", summary="Места на карте")
def positions(
    context: Context = Depends(require_roles_in_airport(*DISPATCH_ROLES)),
    db: Session = Depends(get_db),
):
    icao = context.airport_icao
    graph = get_graph(icao)
    boards = []
    for key, lat, lon, stand in db.query(Aircraft.id, Aircraft.lat, Aircraft.lon, Aircraft.stand_node_id).filter(
            Aircraft.airport_icao == icao):
        if lat is None or lon is None:
            node = graph.node(stand)
            lat, lon = (node["lat"], node["lon"]) if node else (None, None)
        boards.append((key, lat, lon))
    return {
        "employee": [tuple(row) for row in db.query(Employee.id, Employee.lat, Employee.lon).filter(
            Employee.airport_icao == icao, Employee.archived.is_(False))],
        "vehicle": [tuple(row) for row in db.query(Vehicle.id, Vehicle.lat, Vehicle.lon).filter(
            Vehicle.airport_icao == icao, Vehicle.archived.is_(False), Vehicle.status != VEHICLE_IN_USE)],
        "aircraft": boards,
    }


@router.get("/me/position", summary="Где меня видит диспетчер")
def own_position(context: Context = Depends(get_context), db: Session = Depends(get_db)):
    row = db.query(Employee.lat, Employee.lon, Employee.location_at).filter(
        Employee.id == context.user.employee_id).first() if context.user.employee_id else None
    if row is None:
        return {"lat": None, "lon": None, "location_at": None}
    return {"lat": row[0], "lon": row[1], "location_at": to_iso_utc(row[2])}
