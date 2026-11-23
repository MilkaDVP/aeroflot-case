"""
Ручки PWA инженера.

Отдельный набор от диспетчерских: инженер не запрашивает список вызовов
и не ищет себя в нём, а получает свой текущий вызов одним запросом.
Телефон в руках человека на перроне должен делать минимум обращений.
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from typing import Literal
from api.calls import to_response as call_to_response
from auth.dependencies import Context, get_context
from clock import utc_now
from constants import (
    OFFLINE_ACTION_MAX_AGE_S,
    STATUS_BUSY,
    STATUS_EN_ROUTE,
    VEHICLE_RESERVED,
    WORK_STAGES,
)
from database import get_db, get_dispatch_db
from graph_registry import get_graph
from models.call import STATUS_ACCEPTED as CALL_ACCEPTED
from models.call import STATUS_ARRIVED as CALL_ARRIVED
from models.call import STATUS_CLOSED as CALL_CLOSED
from models.call import Call
from models.call_message import AUTHOR_ENGINEER
from models.employee import Employee
from models.engineer_action import EngineerAction
from schemas.auth import MessageResponse
from schemas.call import CallResponse, EngineerMessageRequest, ProblemRequest
from services import mentor, support
from services.assignment import (
    advance_after_close,
    mark_arrived,
    queued_calls_of,
    working_call_of,
)

router = APIRouter(prefix="/api/me", tags=["PWA инженера"])

ERROR_NOT_ENGINEER = "Учётная запись не связана с карточкой сотрудника"
ERROR_NOT_AT_AIRCRAFT = "Этап работ и AOG выставляются у борта: сначала подтвердите прибытие"
ERROR_OTHER_CALL = "Действие относится к другому вызову: этот вызов с вас снят или уже закрыт"
ERROR_WRONG_ORDER = "Действие не подходит к состоянию вызова ({status})"
ERROR_NO_ACTIVE_CALL = "Активного вызова нет"

ACTION_ACCEPTED = "accepted"
ACTION_ARRIVED = "arrived"
ACTION_CLOSED = "closed"

# Из каких статусов вызова действие допустимо. Раньше порядок не
# проверялся; с отложенной отправкой это стало важно: действия, накопленные
# без связи, приходят пачкой, и каждое обязано лечь на своё место.
ACTION_ALLOWED_FROM = {
    "checkpoint": ("accepted",),
    "declined": ("assigned",),
    "assessment": ("arrived",),
    "equipment": ("arrived",),
    ACTION_ACCEPTED: ("assigned",),
    ACTION_ARRIVED: ("assigned", "accepted"),
    ACTION_CLOSED: ("arrived",),
}

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
    # Идентификатор действия, выданный телефоном: повторная отправка того
    # же действия (ответ потерялся, связь мигнула) не применится дважды.
    checkpoint_id: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    mel_category: Literal["A", "B", "C", "D"] | None = None
    missing_tools: bool | None = None
    action_id: str | None = Field(default=None, min_length=8, max_length=64)
    # К какому вызову относится действие. Пока телефон был без связи,
    # диспетчер мог снять вызов: чужой вызов действие не тронет.
    call_id: int | None = None
    # Сколько секунд назад действие совершено. Возраст, а не время:
    # часы телефона могут расходиться с сервером.
    age_s: float | None = Field(default=None, ge=0, le=OFFLINE_ACTION_MAX_AGE_S)


class ReturnNavigation(MessageResponse):
    instruction: str
    route_points: list[dict]


class CurrentCallResponse(BaseModel):
    """
    Текущий вызов инженера вместе с геометрией маршрута.

    Координаты узлов маршрута отдаются здесь же, а не отдельным запросом
    к графу: PWA не должна тянуть на телефон весь граф аэропорта ради
    отрисовки одной ломаной.
    """

    aircraft_point: dict | None = None
    instruction: str | None = None
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
    from services.assignment import route_geometry
    return [{key: node[key] for key in ("id", "lat", "lon")}
            for node in route_geometry(call, graph)]


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
    response_model=CurrentCallResponse | ReturnNavigation | MessageResponse,
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
    from services.assignment import navigation_leg
    leg = navigation_leg(employee)
    if call is None:
        if leg:
            return ReturnNavigation(message=leg["instruction"], **leg)
        return MessageResponse(message=ERROR_NO_ACTIVE_CALL)

    aircraft = call.aircraft
    node = get_graph(call.airport_icao).node(call.stand_node_id)
    point = ({"lat": aircraft.lat, "lon": aircraft.lon} if aircraft and aircraft.lat is not None and aircraft.lon is not None
             else {"lat": node["lat"], "lon": node["lon"]} if node else None)
    return CurrentCallResponse(
        aircraft_point=point,
        call=call_to_response(call),
        instruction=leg["instruction"] if leg else None,
        route_points=leg["route_points"] if leg else route_points(call),
        eta_minutes=call.eta_minutes,
        queued_count=len(queued_calls_of(db, employee)),
        vehicle=vehicle_info(call),
    )


@router.post("/status", response_model=CallResponse, summary="Принял / прибыл / завершил")
def update_status(
    payload: EngineerStatusRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(get_context),
):
    """
    Инженер подтверждает приём вызова, прибытие или завершение работ.

    Статус вызова и статус сотрудника меняются вместе: расхождение между
    ними означало бы, что диспетчер видит на карте не то, что происходит.
    После завершения следующий вызов из очереди сразу становится текущим.
    """
    if payload.action not in ACTION_ALLOWED_FROM:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Действие должно быть одним из: {', '.join(ENGINEER_ACTIONS)}",
        )

    employee = load_own_employee(context, db)

    # Повтор уже применённого действия: отвечаем состоянием вызова,
    # ничего не меняя. Телефон получает тот же ответ, что и в первый раз.
    if payload.action_id:
        done = db.get(EngineerAction, payload.action_id)
        if done is not None and done.employee_id == employee.id:
            return call_to_response(db.get(Call, done.call_id))

    call = working_call_of(db, employee)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_NO_ACTIVE_CALL)
    if payload.call_id is not None and payload.call_id != call.id:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_OTHER_CALL)
    if payload.action == ACTION_ARRIVED and call.status == CALL_ARRIVED and payload.call_id == call.id:
        return call_to_response(call)
    if call.status not in ACTION_ALLOWED_FROM[payload.action]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            ERROR_WRONG_ORDER.format(status=call.status),
        )

    if payload.action == ACTION_ARRIVED and employee.return_vehicle:
        raise HTTPException(409, "Сначала верните транспорт")
    if payload.action == ACTION_ARRIVED and call.require_vehicle_pickup and call.vehicle and call.vehicle.status == VEHICLE_RESERVED:
        raise HTTPException(409, "Сначала заберите назначенный транспорт")
    now = utc_now()
    performed = now - timedelta(seconds=payload.age_s or 0)
    if payload.action == "declined":
        from services.assignment import unassign
        unassign(db, call, "Инженер отказался от вызова")
        if call.parent_call_id:
            call.status, call.closed_at = CALL_CLOSED, performed
        else:
            support.close_support(db, call, performed)
    elif payload.action == "checkpoint":
        from algorithm.geo import haversine_m
        from services.assignment import mark_waypoint_done, remaining_waypoints, assigned_route, update_assigned_route
        from constants import VEHICLE_IN_USE
        graph = get_graph(call.airport_icao)
        if payload.lat is None or payload.lon is None or employee.return_vehicle:
            raise HTTPException(409, "Нельзя подтвердить точку маршрута")
        if payload.checkpoint_id == "vehicle":
            vehicle = call.vehicle
            if not vehicle or vehicle.employee_id != employee.id or vehicle.status != VEHICLE_RESERVED:
                raise HTTPException(409, "Назначение транспорта изменилось")
            if haversine_m(payload.lat, payload.lon, vehicle.lat, vehicle.lon) > 25:
                raise HTTPException(409, "Точка вне стоянки транспорта")
            vehicle.status = VEHICLE_IN_USE
        else:
            pending = remaining_waypoints(call)
            node = graph.node(payload.checkpoint_id)
            if not node or not pending or pending[0] != payload.checkpoint_id:
                raise HTTPException(409, "Маршрут изменился")
            if haversine_m(payload.lat, payload.lon, node["lat"], node["lon"]) > 25:
                raise HTTPException(409, "Точка вне маршрута")
            mark_waypoint_done(call, pending[0], performed)
        plan = assigned_route(call, graph)
        if plan:
            update_assigned_route(call, plan, reset_eta=False)
    elif payload.action == "assessment":
        if not payload.mel_category:
            raise HTTPException(422, "Укажите категорию MEL")
        call.mel_category = payload.mel_category
    elif payload.action == "equipment":
        if payload.missing_tools is None:
            raise HTTPException(422, "Укажите наличие инструмента")
        call.missing_tools = payload.missing_tools
    else:
        if payload.action == ACTION_CLOSED and not call.mel_category:
            raise HTTPException(409, "Сначала укажите категорию MEL")
        call_status, employee_status = ENGINEER_ACTIONS[payload.action]
        call.status = call_status

        if payload.action == ACTION_CLOSED:
            call.closed_at = performed
            advance_after_close(db, employee, call, was_working=True)
            if call.parent_call_id is None:
                # Основной инженер закрыл вызов — работы окончены для всех.
                support.close_support(db, call, performed)
        else:
            employee.status = employee_status
            if payload.action == ACTION_ARRIVED:
                # Прибытие засчитывается по моменту нажатия, а не доставки:
                # инженер, прибывший вовремя без связи, регламент не нарушил.
                if mark_arrived(db, employee, call, performed):
                    mentor.review_arrival(db, call, employee, performed)

    if payload.action_id:
        db.add(
            EngineerAction(
                action_id=payload.action_id,
                employee_id=employee.id,
                call_id=call.id,
                action=payload.action,
                performed_at=performed,
                received_at=now,
            )
        )
    db.commit()
    return call_to_response(call)


class StageRequest(BaseModel):
    """Этап работ у борта."""

    stage: str = Field(description="inspection, waiting_parts или repair")


class AogRequest(BaseModel):
    """Статус AOG по итогам осмотра."""

    aog: bool


def arrived_call(context, db):
    """Вызов, у борта которого инженер сейчас находится."""
    employee = load_own_employee(context, db)
    call = working_call_of(db, employee)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_NO_ACTIVE_CALL)
    if call.status != CALL_ARRIVED:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_NOT_AT_AIRCRAFT)
    return employee, call


@router.post("/stage", response_model=CallResponse, summary="Этап работ")
def set_stage(
    payload: StageRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(get_context),
):
    """
    Инженер сообщает, на каком этапе работы: осмотр, ждёт запчасти, ремонт.

    По этапу диспетчер решает, можно ли переключить инженера на другой
    борт: ожидание запчастей — простой, начатый ремонт прерывать нельзя.
    Пересчитывается и время освобождения по его очереди.
    """
    if payload.stage not in WORK_STAGES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Этап должен быть одним из: {', '.join(WORK_STAGES)}",
        )
    employee, call = arrived_call(context, db)
    call.work_stage = payload.stage
    db.commit()
    return call_to_response(call)


class ToolsRequest(BaseModel):
    """Наличие инструмента: получен со склада или ещё нет."""

    taken: bool = True


@router.post("/tools", response_model=CallResponse, summary="Инструмент получен")
def set_tools(
    payload: ToolsRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(get_context),
):
    """
    Инженер отмечает, что получил инструмент на складе (или что его нет).

    Получил — маршрут перестраивается от склада прямо к борту, и прогноз
    больше не закладывает 30 секунд на склад. Снял отметку — маршрут
    снова идёт через склад.
    """
    from services.assignment import assigned_route, update_assigned_route
    from graph_registry import get_graph

    employee = load_own_employee(context, db)
    call = working_call_of(db, employee)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_NO_ACTIVE_CALL)
    if not call.needs_tools:
        raise HTTPException(status.HTTP_409_CONFLICT, "По этому вызову инструмент со склада не нужен")
    call.tools_taken_at = utc_now() if payload.taken else None
    if payload.taken:
        from services.assignment import mark_waypoint_done

        for item in call.waypoints or []:
            if item.get("kind") == "warehouse" and not item.get("done_at"):
                mark_waypoint_done(call, item["node_id"])
                break
    if call.status != CALL_ARRIVED and employee.lat is not None:
        plan = assigned_route(call, get_graph(call.airport_icao))
        if plan is not None:
            update_assigned_route(call, plan)
    db.commit()
    return call_to_response(call)


class WaypointDoneRequest(BaseModel):
    """Пройденная промежуточная точка; пусто — следующая по порядку."""

    node_id: str | None = None


@router.post("/waypoint", response_model=CallResponse, summary="Точка маршрута пройдена")
def waypoint_done(
    payload: WaypointDoneRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(get_context),
):
    """
    Инженер отмечает промежуточную точку пройденной (склад — инструмент
    получен). Маршрут дальше строится уже без неё. Обычно отметка ставится
    сама, когда инженер подходит к точке ближе 25 м.
    """
    from services.assignment import assigned_route, mark_waypoint_done, update_assigned_route

    employee = load_own_employee(context, db)
    call = working_call_of(db, employee)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_NO_ACTIVE_CALL)
    if not mark_waypoint_done(call, payload.node_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Непройденных точек маршрута нет")
    if call.status != CALL_ARRIVED and employee.lat is not None:
        plan = assigned_route(call, get_graph(call.airport_icao))
        if plan is not None:
            update_assigned_route(call, plan)
    db.commit()
    return call_to_response(call)


@router.post("/aog", response_model=CallResponse, summary="Статус AOG")
def set_aog(
    payload: AogRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(get_context),
):
    """
    Инженер ставит или снимает AOG — борт не может выполнять полёт.

    Только инженер и только у борта: статус определяется после осмотра
    и диагностики, а не со слов экипажа и не решением диспетчера.
    """
    employee, call = arrived_call(context, db)
    # AOG — свойство борта, а не инженера: кто бы из группы его ни
    # поставил, отметка ложится на основной вызов, который видит диспетчер.
    support.root_of(call).aog = payload.aog
    call.aog = payload.aog
    db.commit()
    return call_to_response(call)


@router.post("/message", response_model=CallResponse, summary="Написать диспетчеру")
def send_message(
    payload: EngineerMessageRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(get_context),
):
    """
    Сообщение инженера диспетчеру по текущему вызову.

    Свободный текст вместо набора готовых статусов: «нужен подъёмник»,
    «запчасть привезут к 15:00», «прошу второго инженера». Диспетчер
    видит его на карточке вызова.
    """
    employee = load_own_employee(context, db)
    call = working_call_of(db, employee)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_NO_ACTIVE_CALL)
    support.add_message(db, call, AUTHOR_ENGINEER, employee.full_name, payload.text,
                        is_problem=payload.is_problem)
    db.commit()
    return call_to_response(call)


@router.post("/problem", response_model=CallResponse, summary="Проблема исправлена или нет")
def set_problem(
    payload: ProblemRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(get_context),
):
    """Инженер отмечает проблему по текущему вызову исправленной или нет."""
    employee = load_own_employee(context, db)
    call = working_call_of(db, employee)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_NO_ACTIVE_CALL)
    if not support.set_problem(db, call, payload.message_id, payload.resolved, employee.full_name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Проблема не найдена")
    db.commit()
    return call_to_response(call)
