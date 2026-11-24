"""
ИИ-диспетчер: план назначений по всем вызовам, ждущим исполнителя.

Логика простая и проверяемая:

1. Берём вызовы аэропорта, которые ждут назначения и которые не разбирает
   другой диспетчер.
2. Срочное — первым: AOG, затем «срочный», затем у кого меньше осталось
   из 15 минут.
3. На каждый вызов — лучший кандидат встроенного подбора (допуск, маршрут,
   регламент), которого план ещё не занял другим вызовом: один инженер —
   один вызов.
4. Итог по каждому вызову — одно из трёх: «назначить» (успевает
   в регламент), «проверить» (лучший не успевает — решает человек),
   «некого» (нет никого с допуском).

Назначает всегда человек — кнопкой в окне ИИ-диспетчера; захват вызова
и права проверяются так же, как при обычном назначении. GigaChat только
пересказывает план обычным языком (services/gigachat.py); без него
пояснение собирается по шаблону.
"""

import json

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.calls import ASSIGNABLE_STATUSES, run_suggestion, shift_employees, stand_ref_of
from services.assignment import free_vehicles
from auth.dependencies import Context, require_airport, require_roles
from clock import utc_now
from database import get_db
from models.call import Call
from models.user import DISPATCH_ROLES
from services import claim as claims
from services import agent, gigachat
from services.deadline import assess

router = APIRouter(prefix="/api/ai", tags=["ИИ-диспетчер"])

VERDICT_ASSIGN = "assign"
VERDICT_CHECK = "check"
VERDICT_NOBODY = "nobody"

SYSTEM_PROMPT = (
    "Ты помощник диспетчера оперативного техобслуживания самолётов в аэропорту. "
    "Тебе дают готовый план назначений инженеров на вызовы, составленный системой "
    "по допускам, маршрутам и регламенту: инженер должен прибыть к борту за 15 минут. "
    "Одним-двумя короткими предложениями (до 25 слов), по-русски скажи диспетчеру, "
    "что сделать первым и где нужно его решение. Цифры по плану он уже видит — не пересказывай их. "
    "Не придумывай людей, борта и цифры, которых нет в плане. Без приветствий и списков."
)


class PlanItem(BaseModel):
    call_id: int
    board_number: str
    aircraft_type: str
    stand_ref: str | None
    defect_name: str
    required_mark: str
    aog: bool
    urgent: bool
    minutes_left: float | None
    verdict: str
    employee_id: int | None = None
    employee_name: str | None = None
    eta_minutes: float | None = None
    vehicle_call_sign: str | None = None
    # Для показа на карте: стоянка вызова и маршрут предложенного инженера.
    stand_node_id: str | None = None
    route_node_ids: list[str] = []
    pickup_node_id: str | None = None
    # «Горит» — как красная карточка в активных вызовах (контроль срока).
    alert: bool = False
    # Предложен не лучший кандидат: лучший нужен на более срочном вызове.
    # Назначить можно после того вызова — «Назначить всех» идёт по порядку.
    needs_order: bool = False
    reason: str


class PlanResponse(BaseModel):
    items: list[PlanItem]
    summary: str
    # Кто написал пояснение: gigachat или rules (шаблон).
    summary_source: str
    gigachat_configured: bool


def priority(call, now):
    control = assess(call, now)
    left = control.as_dict().get("minutes_left")
    return (not call.aog, not call.urgent, left if left is not None else 999, call.id)


def plan_item(call, result, taken, now):
    """Лучший ещё не занятый планом кандидат и вердикт по вызову."""
    control = assess(call, now).as_dict()
    aircraft = call.aircraft
    base = {
        "call_id": call.id,
        "board_number": aircraft.board_number if aircraft else "—",
        "aircraft_type": aircraft.aircraft_type if aircraft else "—",
        "stand_ref": None if aircraft and aircraft.lat is not None else stand_ref_of(call),
        "defect_name": call.defect_type.name if call.defect_type else call.defect_code,
        "required_mark": call.required_mark,
        "aog": bool(call.aog),
        "urgent": bool(call.urgent),
        "minutes_left": control.get("minutes_left"),
        "stand_node_id": call.stand_node_id,
        "alert": bool(control.get("alert")),
    }
    choice = next((c for c in result.candidates if c.employee["id"] not in taken), None)
    if choice is None:
        reason = (
            "все подходящие заняты на других вызовах" if result.candidates
            else f"нет свободных с допуском {result.required_mark}"
        )
        return PlanItem(**base, verdict=VERDICT_NOBODY, reason=reason)

    taken.add(choice.employee["id"])
    data = choice.as_dict()
    needs_order = choice is not result.best
    if needs_order:
        # Лучший нужен на более срочном вызове. Когда тот назначен, этот
        # кандидат станет лучшим для этого вызова — поэтому назначать по
        # порядку (это и делает «Назначить всех»), а не раньше срочного.
        verdict, reason = VERDICT_CHECK, "лучший нужен на более срочном вызове"
    elif choice.within_regulation:
        verdict, reason = VERDICT_ASSIGN, "успевает"
    else:
        verdict, reason = VERDICT_CHECK, "не успевает за 15 минут"
    return PlanItem(
        **base,
        verdict=verdict,
        employee_id=data["employee_id"],
        employee_name=data["full_name"],
        eta_minutes=data["minutes"],
        vehicle_call_sign=data["vehicle_call_sign"],
        route_node_ids=data["route"].get("node_ids") or [],
        pickup_node_id=data["route"].get("pickup_node_id"),
        needs_order=needs_order,
        reason=reason,
    )


def rules_summary(items):
    """Пояснение без GigaChat: что назначить, что проверить, где некого."""
    if not items:
        return "Вызовов, ждущих назначения, нет."
    # Коротко: цифры диспетчер видит на счётчиках, здесь — что делать.
    ready = [i for i in items if i.verdict == VERDICT_ASSIGN]
    check = [i for i in items if i.verdict == VERDICT_CHECK]
    nobody = [i for i in items if i.verdict == VERDICT_NOBODY]
    parts = []
    first = items[0]
    if first.aog or first.urgent:
        parts.append(f"Сначала {first.board_number} ({'AOG' if first.aog else 'срочный'}).")
    if check:
        parts.append("Решите по " + ", ".join(i.board_number for i in check) + ".")
    elif ready:
        parts.append("Все успевают — можно назначать.")
    if nobody and len(parts) < 2:
        parts.append("Некого на " + ", ".join(i.board_number for i in nobody) + ".")
    return " ".join(parts)


def gigachat_summary(items):
    return gigachat.complete(SYSTEM_PROMPT, json.dumps(plan_facts(items), ensure_ascii=False))


class ChatTurn(BaseModel):
    question: str = Field(max_length=1000)
    answer: str = Field(max_length=1200)


class AskRequest(BaseModel):
    history: list[ChatTurn] = Field(default_factory=list, max_length=10)
    question: str = Field(min_length=1, max_length=500)


class AskResponse(BaseModel):
    answer: str
    source: str


ASK_PROMPT = (
    "Ты ИИ-диспетчер — помощник диспетчера оперативного техобслуживания самолётов "
    "в аэропорту. Ниже — текущая обстановка смены: вызовы, ждущие назначения (с планом "
    "системы), вызовы в работе, люди смены и свободные машины. Регламент: инженер должен "
    "прибыть к борту за 15 минут. Отвечай по-русски, просто и коротко — до 4 предложений, "
    "опираясь на обстановку. Если вызовов нет — так и скажи и опиши, кто свободен. "
    "На приветствие или вопрос не по делу ответь одной фразой и предложи помощь по смене. "
    "Не придумывай людей, борта и цифры, которых нет в обстановке. Решение о назначении "
    "всегда за диспетчером."
)

STATUS_WORDS = {
    "free": "свободен",
    "assigned": "назначен",
    "en_route": "в пути",
    "busy": "у борта",
    "offline": "не на смене",
}
CALL_WORDS = {
    "assigned": "назначен, не принят",
    "accepted": "принят, едет",
    "arrived": "у борта",
    "queued": "в очереди",
}


def situation_text(db, context, items, now):
    """Обстановка смены обычным текстом — для вопросов к GigaChat."""
    lines = [f"Аэропорт {context.airport_icao}, смена: {context.shift or '—'}."]

    # «Что горит?» — вызовы, у которых срок под угрозой (красные в списке).
    burning = []
    for call in db.query(Call).filter(
        Call.airport_icao == context.airport_icao,
        Call.status.in_(tuple(CALL_WORDS) + tuple(ASSIGNABLE_STATUSES)),
        Call.parent_call_id.is_(None),
    ):
        control = assess(call, now).as_dict()
        if control.get("alert"):
            board = call.aircraft.board_number if call.aircraft else "—"
            burning.append(f"{board} — {control.get('reason') or 'срок под угрозой'}")
    lines.append(
        "Горят (срок под угрозой): " + "; ".join(burning) + "." if burning
        else "Горящих вызовов нет."
    )

    lines.append("Ждут назначения:" if items else "Вызовов, ждущих назначения, нет.")
    verdict_words = {"assign": "можно назначить", "check": "нужно решение", "nobody": "некого"}
    for i in items:
        who = f"{i.employee_name}, прибудет через {i.eta_minutes} мин" if i.employee_name else "кандидата нет"
        flags = ", ".join(f for f in ("AOG" if i.aog else "", "срочный" if i.urgent else "") if f)
        lines.append(
            f"- {i.board_number} ({i.aircraft_type}, {i.defect_name}"
            f"{', ' + flags if flags else ''}), осталось {i.minutes_left} мин: "
            f"{who}; {verdict_words[i.verdict]} — {i.reason}."
        )

    active = db.query(Call).filter(
        Call.airport_icao == context.airport_icao,
        Call.status.in_(tuple(CALL_WORDS)),
    ).all()
    if active:
        lines.append("В работе:")
    for call in active:
        control = assess(call, now).as_dict()
        engineer = call.assigned_employee.full_name if call.assigned_employee else "—"
        note = f" ({control['reason']})" if control.get("alert") and control.get("reason") else ""
        lines.append(
            f"- {call.aircraft.board_number if call.aircraft else '—'}: {engineer}, "
            f"{CALL_WORDS.get(call.status, call.status)}{note}."
        )

    people = [p for p in shift_employees(db, context) if p["shift"] == context.shift]
    free = [p for p in people if p["status"] == "free"]
    lines.append(f"Люди смены: {len(people)}, свободны: {len(free)}.")
    for person in people:
        marks = "; ".join(
            f"{q['category']} ({', '.join(q['aircraft_types'])})" for q in person["qualifications"]
        )
        lines.append(f"- {person['full_name']}: {STATUS_WORDS.get(person['status'], person['status'])}, {marks or 'без отметок'}.")
    lines.append(f"Свободных машин: {len(free_vehicles(db, context.airport_icao))}.")
    db.rollback()
    return "\n".join(lines)


def build_plan(db, context, now):
    """Вызовы, ждущие назначения, в порядке срочности, и план по ним."""
    calls = [
        call
        for call in db.query(Call).filter(
            Call.airport_icao == context.airport_icao,
            Call.status.in_(ASSIGNABLE_STATUSES),
            Call.parent_call_id.is_(None),
        )
        if not claims.held_by_other(call, context.user, now)
    ]
    calls.sort(key=lambda call: priority(call, now))
    taken = set()
    items = [plan_item(call, run_suggestion(db, context, call), taken, now) for call in calls]
    # Подбор только читает: ничего не записываем.
    db.rollback()
    return items


def plan_facts(items):
    return [
        {
            "борт": i.board_number,
            "тип": i.aircraft_type,
            "стоянка": i.stand_ref,
            "неисправность": i.defect_name,
            "нужна_отметка": i.required_mark,
            "AOG": i.aog,
            "срочный": i.urgent,
            "осталось_мин": i.minutes_left,
            "решение": {"assign": "назначить", "check": "проверить", "nobody": "некого"}[i.verdict],
            "инженер": i.employee_name,
            "прибытие_мин": i.eta_minutes,
            "почему": i.reason,
        }
        for i in items
    ]


@router.post("/ask", response_model=AskResponse, summary="Спросить ИИ-диспетчера")
def ai_ask(
    payload: AskRequest,
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
    airport: Context = Depends(require_airport),
    db: Session = Depends(get_db),
):
    """
    Свободный вопрос об обстановке: «кого лучше отправить на VP-BZQ?»,
    «что горит?». Отвечает GigaChat по текущему плану; без него — честно
    говорит, что ответить некому, и показывает пояснение по правилам.
    """
    items = build_plan(db, context, utc_now())
    if not gigachat.configured():
        return AskResponse(
            answer="GigaChat не подключён. По плану: " + rules_summary(items),
            source="rules",
        )
    question = payload.question.strip()
    situation = situation_text(db, context, items, utc_now())
    if payload.history:
        situation += "\nПредыдущие реплики для контекста: " + json.dumps([t.model_dump() for t in payload.history], ensure_ascii=False)
    answer = gigachat.complete(ASK_PROMPT, f"Обстановка:\n{situation}\n\nВопрос диспетчера: {question}")
    if not answer:
        return AskResponse(
            answer="GigaChat сейчас не ответил. По плану: " + rules_summary(items),
            source="rules",
        )
    return AskResponse(answer=answer, source="gigachat")


class AgentMessage(BaseModel):
    """Реплика разговора: просьба или результаты действий — user, ход агента — assistant."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=agent.MAX_CONTENT)


class AgentRequest(BaseModel):
    # Просьба первым полем: её видно в журнале действий.
    command: str = Field(min_length=1, max_length=1000)
    messages: list[AgentMessage] = Field(min_length=1, max_length=agent.MAX_MESSAGES)


ROLE_WORDS = {
    "dispatcher": "диспетчер",
    "shift_supervisor": "начальник смены",
    "local_admin": "администратор аэропорта",
}


@router.post("/agent", summary="Просьба ИИ-агенту")
@router.post("/agent/step", summary="Следующий ход ИИ-агента")
def ai_agent(
    payload: AgentRequest,
    request: Request,
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
    airport: Context = Depends(require_airport),
):
    """
    Один ход агента: действия, которые экран выполнит от имени диспетчера
    и пришлёт результат следующим ходом, и ответ, когда всё сделано.
    Просьба пишется в журнал, промежуточные ходы (/agent/step) — нет:
    сами действия попадают туда как обычные действия диспетчера.
    """
    messages = [item.model_dump() for item in payload.messages]
    if len(json.dumps(messages, ensure_ascii=False)) > agent.MAX_TOTAL:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Слишком длинный разговор — начните новую просьбу")
    if not gigachat.configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GigaChat не подключён")
    role = ROLE_WORDS.get(context.user.role, context.user.role)
    reply = agent.step(agent.system_prompt(airport, role), messages,
                       first=not request.url.path.endswith("/step"))
    if reply is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GigaChat сейчас не отвечает")
    return reply


@router.get("/plan", response_model=PlanResponse, summary="План ИИ-диспетчера")
def ai_plan(
    explain: bool = True,
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
    airport: Context = Depends(require_airport),
    db: Session = Depends(get_db),
):
    """
    План назначений по всем ждущим вызовам аэропорта. Только чтение:
    ничего не назначает и вызовы не захватывает.
    """
    items = build_plan(db, context, utc_now())

    summary, source = None, "rules"
    if explain and items:
        summary = gigachat_summary(items)
        if summary:
            source = "gigachat"
    return PlanResponse(
        items=items,
        summary=summary or rules_summary(items),
        summary_source=source,
        gigachat_configured=gigachat.configured(),
    )
