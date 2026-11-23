"""
Режим учений: включение и задержка сотрудника.

Включает учения только администратор: в учениях сервер сам двигает людей
по карте и сам подтверждает прибытие, и в настоящей смене это недопустимо.
Задержать исполнителя может диспетчер — это и есть его тренировка.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from models.airport import Airport
from sqlalchemy.orm import Session

from auth.dependencies import Context, check_same_airport, require_airport, require_roles
from database import get_db, get_dispatch_db
from models.employee import RouteParticipant
from models.user import DISPATCH_ROLES, ROLE_LOCAL_ADMIN
from services import drill

router = APIRouter(prefix="/api/drill", tags=["Учения"])

ERROR_DRILL_OFF = "Режим учений выключен: задерживать сотрудников можно только в учениях"
ERROR_EMPLOYEE_NOT_FOUND = "Сотрудник не найден"


class DrillState(BaseModel):
    """Идут ли учения в аэропорту сессии."""

    enabled: bool
    speed: int | None = Field(default=None, ge=1, le=32)


class HoldRequest(BaseModel):
    """Задержать сотрудника на месте или отпустить."""

    held: bool


class HoldResponse(BaseModel):
    employee_id: int
    held: bool


@router.get("", response_model=DrillState, summary="Идут ли учения")
def drill_state(context: Context = Depends(require_airport), db: Session = Depends(get_db)):
    """Состояние режима учений — рабочее место показывает его в шапке."""
    airport = db.get(Airport, context.airport_icao)
    return DrillState(enabled=airport.drill_enabled, speed=airport.drill_speed)


@router.post("", response_model=DrillState, summary="Включить или выключить учения")
def set_drill(
    payload: DrillState,
    context: Context = Depends(require_roles(ROLE_LOCAL_ADMIN)),
    airport: Context = Depends(require_airport),
    db: Session = Depends(get_dispatch_db),
):
    """Администратор аэропорта и старше. При выключении все задержки снимаются."""
    settings = drill.set_enabled(db, context.airport_icao, payload.enabled)
    if payload.speed is not None:
        settings.drill_speed = payload.speed
    db.commit()
    return DrillState(enabled=payload.enabled, speed=settings.drill_speed)


@router.post("/hold/{employee_id}", response_model=HoldResponse, summary="Задержать сотрудника")
def hold_employee(
    employee_id: int,
    payload: HoldRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
    airport: Context = Depends(require_airport),
):
    """
    Останавливает сотрудника на маршруте — учебная вводная «исполнитель встал».

    Вне учений недоступно: в настоящей смене сотрудника останавливает
    жизнь, а не кнопка диспетчера.
    """
    employee = db.get(RouteParticipant, employee_id)
    if employee is None or employee.archived:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_EMPLOYEE_NOT_FOUND)
    check_same_airport(context, employee.airport_icao)
    if not drill.is_enabled(db, context.airport_icao):
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_DRILL_OFF)

    if not employee.is_training:
        raise HTTPException(status.HTTP_409_CONFLICT, "Задержать можно только учебного инженера")
    employee.held = payload.held
    db.commit()
    return HoldResponse(employee_id=employee.id, held=employee.held)
