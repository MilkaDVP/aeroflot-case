"""
Кто ещё может успеть — конкретный вариант решения к предупреждению о сроке.

«Назначьте того, кто ближе» заставляет диспетчера искать самому, когда
времени уже нет. Здесь для вызова под угрозой считается то же, что при
подборе: ближайший свободный с допуском, сколько ему ехать и с каким
запасом он успевает. Нет свободных — инженер, ждущий запчасти на другом
борту. Быстрее нынешнего никого нет — так и сказано: переназначать незачем.
"""

import time
from datetime import timedelta

from algorithm.dispatch import suggest
from algorithm.transport import Network, best_plan, warehouses
from clock import to_iso_utc, utc_now
from constants import STAGE_WAITING_PARTS
from graph_registry import get_graph
from models.airport import Airport
from models.call import STATUS_ARRIVED, STATUS_QUEUED, Call
from models.employee import Employee
from models.user import AuthSession, User
from services import shift_clock
from services.assignment import free_vehicles, remaining_waypoints
from services.deadline import (
    LEVEL_LATE,
    LEVEL_NO_SIGNAL,
    LEVEL_OVERDUE,
    LEVEL_UNASSIGNED,
    LEVEL_WARN,
    minutes_text,
    overrun_text,
)

LEVELS = (LEVEL_WARN, LEVEL_LATE, LEVEL_NO_SIGNAL, LEVEL_UNASSIGNED, LEVEL_OVERDUE)
# Список вызовов опрашивают каждые 4 секунды несколько экранов: подбор
# по одному вызову повторяется не чаще.
CACHE_S = 8
# Выигрыш меньше этого переназначения не стоит.
GAIN_MIN = 0.5
_cache = {}


def cached(db, call, control, shift=None):
    """Вариант решения для вызова под угрозой; None — предлагать не нужно."""
    if control.level not in LEVELS or call.parent_call_id is not None:
        return None
    key = (call.id, call.assigned_employee_id, call.status, call.stand_node_id, shift)
    known = _cache.get(key)
    if known is None or time.monotonic() - known[0] > CACHE_S:
        if len(_cache) > 300:
            _cache.clear()
        known = _cache[key] = (time.monotonic(), choose(db, call, control, shift))
    return known[1]


def choose(db, call, control, shift=None):
    now = utc_now()
    graph = get_graph(call.airport_icao)
    if shift is None:
        shift = current_shift(db, call)
    team = {call.assigned_employee_id}
    team.update(row[0] for row in db.query(Call.assigned_employee_id).filter(Call.parent_call_id == call.id))
    if not call.allow_previous_employee:
        team.add(call.previous_employee_id)
    people = [p for p in db.query(Employee).filter_by(airport_icao=call.airport_icao, archived=False) if p.id not in team]
    vehicles = [v.as_algorithm_dict() for v in free_vehicles(db, call.airport_icao)]
    needs_tools = bool(call.needs_tools) and call.tools_taken_at is None
    result = suggest(graph, {
        "aircraft_type": call.aircraft.aircraft_type, "defect_code": call.defect_code,
        "stand_node_id": call.stand_node_id, "required_mark": call.required_mark,
        "needs_tools": needs_tools, "waypoints": remaining_waypoints(call),
    }, [p.as_algorithm_dict() for p in people], shift, vehicles=vehicles)

    left = (control.deadline_at - now).total_seconds() / 60
    if result.best is not None:
        best = result.best
        option = describe(best.employee["id"], best.employee["full_name"], best.route, left, now, "free")
    else:
        option = waiting_parts(db, graph, call, people, result, vehicles, needs_tools, left, now)

    # Нынешний исполнитель доедет не позже — менять его смысла нет. При потере
    # сигнала прогнозу верить нельзя, поэтому вариант показывается всегда.
    current = None
    if call.assigned_employee is not None and control.level != LEVEL_NO_SIGNAL and call.status != STATUS_QUEUED:
        current = (control.forecast_at - now).total_seconds() / 60 if control.forecast_at else None
    if option is not None and current is not None and option["minutes"] > current - GAIN_MIN:
        return {"kind": "keep", "in_time": False,
                "text": f"Быстрее никого нет: ближайший — {option['name']}, {number(option['minutes'])} мин. Оставьте нынешнего"}
    if option is None:
        return {"kind": "nobody", "in_time": False,
                "text": "Свободных с допуском нет, переключить некого. Сообщите начальнику смены"}
    return option


def current_shift(db, call):
    """Смена исполнителя; нет его — та, в которой сейчас работают диспетчеры."""
    if call.assigned_employee is not None:
        return call.assigned_employee.shift
    row = db.query(AuthSession.shift).join(User).filter(
        AuthSession.airport_icao == call.airport_icao, AuthSession.shift.is_not(None),
        User.employee_id.is_(None), AuthSession.expires_at > utc_now(),
    ).order_by(AuthSession.created_at.desc()).first()
    # Диспетчер мог выбрать смену не по часам — его выбор важнее.
    return row[0] if row else shift_clock.shift_at(db.get(Airport, call.airport_icao))


def describe(employee_id, name, plan, left, now, kind):
    margin = left - plan.minutes
    verdict = (f"успевает с запасом {minutes_text(margin)}" if margin >= 0
               else f"опоздает на {overrun_text(-margin)}" if left >= 0
               else "срок уже истёк, это быстрее всего")
    how = f"на машине {plan.vehicle['call_sign']}" if plan.vehicle else "пешком"
    who = "ближайший свободный" if kind == "free" else "ждёт запчасти, можно переключить"
    return {
        "kind": kind, "employee_id": employee_id, "name": name, "minutes": round(plan.minutes, 1),
        "margin_min": round(margin, 1), "in_time": margin >= 0,
        "arrival_at": to_iso_utc(now + timedelta(minutes=plan.minutes)),
        "vehicle": plan.vehicle["call_sign"] if plan.vehicle else None,
        "text": f"{name} — {who}, {number(plan.minutes)} мин {how}, {verdict}",
    }


def waiting_parts(db, graph, call, people, result, vehicles, needs_tools, left, now):
    """Занятые с допуском, ждущие запчасти: их можно переключить сразу."""
    able = {entry["employee_id"] for entry in result.busy}
    waiting = {row[0] for row in db.query(Call.assigned_employee_id).filter(
        Call.airport_icao == call.airport_icao, Call.status == STATUS_ARRIVED,
        Call.work_stage == STAGE_WAITING_PARTS)}
    net = Network(graph, call.stand_node_id, vehicles, (warehouses(graph) or None) if needs_tools else None,
                  remaining_waypoints(call))
    best = None
    for person in people:
        if person.id in able and person.id in waiting and person.lat is not None:
            plan = best_plan(net, person.as_algorithm_dict())
            if plan is not None and (best is None or plan.minutes < best[1].minutes):
                best = (person, plan)
    return describe(best[0].id, best[0].full_name, best[1], left, now, "switch") if best else None


def number(value):
    return f"{value:.1f}".replace(".", ",")

