"""
Ручки PWA инженера.

Отдельный набор от диспетчерских: инженер не запрашивает список вызовов
и не ищет себя в нём, а получает свой текущий вызов одним запросом.
Телефон в руках человека на перроне должен делать минимум обращений.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.calls import to_response as call_to_response
from auth.dependencies import Context, get_context
from clock import utc_now
from constants import STATUS_BUSY, STATUS_EN_ROUTE, VEHICLE_RESERVED
from database import get_db
from graph_registry import get_graph
from models.call import STATUS_ACCEPTED as CALL_ACCEPTED
from models.call import STATUS_ARRIVED as CALL_ARRIVED
from models.call import STATUS_CLOSED as CALL_CLOSED
from models.employee import Employee
from schemas.auth import MessageResponse
from schemas.call import CallResponse
from services.assignment import (
    advance_after_close,
    mark_arrived,
    queued_calls_of,
    working_call_of,
)

router = APIRouter(prefix="/api/me", tags=["PWA инженера"])

ERROR_NOT_ENGINEER = "Учётная запись не связана с карточкой сотрудника"
ERROR_NO_ACTIVE_CALL = "Активного вызова нет"

ACTION_ACCEPTED = "accepted"
ACTION_ARRIVED = "arrived"
ACTION_CLOSED = "closed"

# Действие инженера -> статус вызова и статус самого сотрудника.
# Таблица вместо ветвлений: список действий короткий и фиксированный,
# а видеть его целиком удобнее, чем собирать из if-ов. Закрытие здесь
# только меняет статус вызова, а освобождение сотрудника решает очередь.
ENGINEER_ACTIONS = {
    ACTION_ACCEPTED: (CALL_ACCEPTED, STATUS_EN_ROUTE),
    ACTION_ARRIVED: (CALL_ARRIVED, STATUS_BUSY),
    ACTION_CLOSED: (CALL_CLOSED, None),
}


class EngineerStatusRequest(BaseModel):
    """Смена статуса инженером с телефона."""

    action: str = Field(description="accepted, arrived или closed")


class CurrentCallResponse(BaseModel):
    """
    Текущий вызов инженера вместе с геометрией маршрута.

    Координаты узлов маршрута отдаются здесь же, а не отдельным запросом
    к графу: PWA не должна тянуть на телефон весь граф аэропорта ради
    отрисовки одной ломаной.
    """

    call: CallResponse
    route_points: list[dict]
    eta_minutes: float | None
    # Сколько вызовов ждёт инженера после текущего. Инженер должен знать,
    # что после закрытия его не отпустят, а сразу отправят к следующему борту.
    queued_count: int = 0
    # Машина для этого вызова. pickup = True — её сначала надо забрать:
    # телефон показывает, где она стоит, иначе инженер пойдёт пешком.
    vehicle: dict | None = None


def load_own_employee(context, db):
    """Карточка сотрудника, связанная с учётной записью инженера."""
    if not context.user.employee_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_NOT_ENGINEER)
    employee = db.get(Employee, context.user.employee_id)
    if employee is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, ERROR_NOT_ENGINEER)
    return employee


def route_points(call):
    """Маршрут в виде списка точек с координатами для карты в телефоне."""
    if not call.route_node_ids:
        return []
    graph = get_graph(call.airport_icao)
    points = []
    for node_id in call.route_node_ids:
        node = graph.node(node_id)
        if node:
            points.append({"id": node["id"], "lat": node["lat"], "lon": node["lon"]})
    return points


def vehicle_info(call):
    """Машина вызова для телефона: позывной, где стоит, надо ли её забрать."""
    vehicle = call.vehicle
    if vehicle is None:
        return None
    lat, lon = vehicle.position()
    return {
        "call_sign": vehicle.call_sign,
        "kind": vehicle.kind,
        "pickup": vehicle.status == VEHICLE_RESERVED,
        "lat": lat,
        "lon": lon,
    }


@router.get(
    "/current-call",
    response_model=CurrentCallResponse | MessageResponse,
    summary="Текущий вызов инженера",
)
def current_call(context: Context = Depends(get_context), db: Session = Depends(get_db)):
    """
    Текущий вызов с маршрутом либо сообщение «вызовов нет».

    Отдаётся только текущий вызов, а не вызовы из очереди: к очередному
    борту инженер поедет после закрытия текущего, и показывать ему маршрут
    туда раньше времени — значит сбить с толку.
    """
    employee = load_own_employee(context, db)
    call = working_call_of(db, employee)
    if call is None:
        return MessageResponse(message=ERROR_NO_ACTIVE_CALL)

    return CurrentCallResponse(
        call=call_to_response(call),
        route_points=route_points(call),
        eta_minutes=call.eta_minutes,
        queued_count=len(queued_calls_of(db, employee)),
        vehicle=vehicle_info(call),
    )


@router.post("/status", response_model=CallResponse, summary="Принял / прибыл / завершил")
def update_status(
    payload: EngineerStatusRequest,
    context: Context = Depends(get_context),
    db: Session = Depends(get_db),
):
    """
    Инженер подтверждает приём вызова, прибытие или завершение работ.

    Статус вызова и статус сотрудника меняются вместе: расхождение между
    ними означало бы, что диспетчер видит на карте не то, что происходит.
    После завершения следующий вызов из очереди сразу становится текущим.
    """
    if payload.action not in ENGINEER_ACTIONS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Действие должно быть одним из: {', '.join(ENGINEER_ACTIONS)}",
        )

    employee = load_own_employee(context, db)
    call = working_call_of(db, employee)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_NO_ACTIVE_CALL)

    call_status, employee_status = ENGINEER_ACTIONS[payload.action]
    call.status = call_status

    if payload.action == ACTION_CLOSED:
        call.closed_at = utc_now()
        advance_after_close(db, employee, call, was_working=True)
    else:
        employee.status = employee_status
        if payload.action == ACTION_ARRIVED:
            mark_arrived(db, employee, call)

    db.commit()
    return call_to_response(call)
