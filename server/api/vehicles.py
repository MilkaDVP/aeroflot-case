"""
Спецтранспорт: машины парка текущего аэропорта.

Парком управляет администратор: машину можно поставить в парк и вывести
из него. А вот резерв и возврат при работе — побочный эффект назначения
и закрытия вызова (services/assignment.py), а не отдельные ручки: машина,
которую можно «зарезервировать» мимо вызова, стала бы второй правдой
о том, кто куда едет.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from auth.dependencies import Context, require_airport, require_roles_in_airport
from constants import VEHICLE_FREE
from database import get_db
from models.user import ROLE_ADMIN
from models.vehicle import Vehicle
from schemas.vehicle import VehicleCreateRequest, VehicleResponse

router = APIRouter(prefix="/api/vehicles", tags=["Спецтранспорт"])

ERROR_NOT_FOUND = "Машина не найдена"
ERROR_DUPLICATE = "Машина с позывным {call_sign} уже есть в этом аэропорту"
ERROR_IN_WORK = (
    "Машина {call_sign} сейчас задействована на вызове. Вывести её из парка "
    "можно после завершения работ"
)


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


@router.post(
    "",
    response_model=VehicleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Поставить машину в парк",
)
def create_vehicle(
    payload: VehicleCreateRequest,
    context: Context = Depends(require_roles_in_airport(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """
    Заводит машину в парке текущего аэропорта.

    Позывной обязан быть уникальным в пределах аэропорта: диспетчер видит
    в подборе именно его («пешком → ТМ-04»), и две одинаковые надписи
    означали бы, что непонятно, к какой машине идти.
    """
    call_sign = payload.call_sign.strip()
    exists = (
        db.query(Vehicle)
        .filter(
            Vehicle.airport_icao == context.airport_icao,
            Vehicle.call_sign == call_sign,
        )
        .first()
    )
    if exists is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, ERROR_DUPLICATE.format(call_sign=call_sign)
        )

    vehicle = Vehicle(
        airport_icao=context.airport_icao,
        call_sign=call_sign,
        kind=payload.kind.strip() or "Техпомощь",
        status=VEHICLE_FREE,
        lat=payload.lat,
        lon=payload.lon,
    )
    db.add(vehicle)
    db.commit()
    return to_response(vehicle)


@router.delete(
    "/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Вывести из парка"
)
def delete_vehicle(
    vehicle_id: int,
    context: Context = Depends(require_roles_in_airport(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """
    Убирает машину из парка.

    Занятую машину удалять нельзя: за ней едет или на ней едет инженер,
    и вызов остался бы с ссылкой на несуществующую машину.
    """
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None or vehicle.airport_icao != context.airport_icao:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_NOT_FOUND)

    if vehicle.status != VEHICLE_FREE:
        raise HTTPException(
            status.HTTP_409_CONFLICT, ERROR_IN_WORK.format(call_sign=vehicle.call_sign)
        )

    db.delete(vehicle)
    db.commit()
