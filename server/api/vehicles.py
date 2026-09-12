"""
Спецтранспорт: машины парка текущего аэропорта.

Только чтение. Резерв и возврат машин — побочный эффект назначения
и закрытия вызова (services/assignment.py), а не отдельные ручки:
машина, которую можно «зарезервировать» мимо вызова, стала бы
второй правдой о том, кто куда едет.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from auth.dependencies import Context, require_airport
from database import get_db
from models.vehicle import Vehicle
from schemas.vehicle import VehicleResponse

router = APIRouter(prefix="/api/vehicles", tags=["Спецтранспорт"])


def to_response(vehicle):
    """Модель машины в схему ответа с её текущим местом."""
    lat, lon = vehicle.position()
    return VehicleResponse(
        id=vehicle.id,
        call_sign=vehicle.call_sign,
        kind=vehicle.kind,
        status=vehicle.status,
        lat=lat,
        lon=lon,
        employee_id=vehicle.employee_id,
        employee_name=vehicle.employee.full_name if vehicle.employee else None,
    )


@router.get("", response_model=list[VehicleResponse], summary="Машины парка")
def list_vehicles(
    context: Context = Depends(require_airport),
    db: Session = Depends(get_db),
):
    """Машины аэропорта сессии: где стоят и у кого сейчас."""
    vehicles = (
        db.query(Vehicle)
        .filter(Vehicle.airport_icao == context.airport_icao)
        .order_by(Vehicle.call_sign)
        .all()
    )
    return [to_response(vehicle) for vehicle in vehicles]
