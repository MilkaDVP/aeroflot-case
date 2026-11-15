"""
Наставник — разбор действий диспетчера и обстановки на смене.

Наставник — вымышленный опытный диспетчер. Он смотрит за каждым решением
и коротко говорит: это хорошо, это не очень, это плохо — и всякий раз
объясняет почему, с цифрами. Эксперты Аэрофлота просили показывать не только
итог назначения, но и логику решения; Наставник делает её видимой и в момент
решения, и в разборе смены.

Оценку выносят детерминированные правила этого модуля, а не нейросеть.
Причины три. Система обязана работать без интернета. Одна и та же обстановка
должна давать одну и ту же оценку — иначе её нельзя ни проверить тестом,
ни защитить перед экспертом. И оценка опирается только на то, что система
уже посчитала: подбор, время в пути, контроль регламента — выдумать цифру
здесь неоткуда.

Что оценивается:
  - назначение: тот ли исполнитель, какой запас, сколько ждали с назначением;
  - переопределение решения системы: во сколько минут оно обошлось;
  - постановка в очередь к занятому: был ли свободный, кто успевал;
  - снятие исполнителя: вовремя или зря;
  - прибытие: в срок или с опозданием;
  - обстановка: вызов без исполнителя, исполнитель не успевает, нет сигнала.
"""

from datetime import timedelta

from clock import to_iso_utc, utc_now
from constants import (
    DEADLINE_MARGIN_MIN,
    OVERRIDE_TOLERANCE_MIN,
    SHIFT_WINDOW_H,
    UNASSIGNED_ALERT_AFTER_MIN,
)
from models.call import ACTIVE_CALL_STATUSES, STATUS_QUEUED, Call
from models.shift_event import VERDICT_BAD, VERDICT_FAIR, VERDICT_GOOD, ShiftEvent
from services.deadline import (
    ALERT_LEVELS,
    LEVEL_LATE,
    LEVEL_NO_SIGNAL,
    LEVEL_OVERDUE,
    LEVEL_UNASSIGNED,
    assess,
    deadline_of,
    minutes_text,
    overrun_text,
    started_at,
)

KIND_ASSIGN = "assign"
KIND_QUEUE = "queue"
KIND_UNASSIGN = "unassign"
KIND_ARRIVAL = "arrival"
KIND_WATCH_UNASSIGNED = "watch_unassigned"
KIND_WATCH_LATE = "watch_late"
KIND_WATCH_OVERDUE = "watch_overdue"
KIND_WATCH_SIGNAL = "watch_signal"

# Уровень контроля регламента -> вид наблюдения и оценка обстановки.
WATCH_BY_LEVEL = {
    LEVEL_UNASSIGNED: (KIND_WATCH_UNASSIGNED, VERDICT_FAIR),
    LEVEL_LATE: (KIND_WATCH_LATE, VERDICT_BAD),
    LEVEL_OVERDUE: (KIND_WATCH_OVERDUE, VERDICT_BAD),
    LEVEL_NO_SIGNAL: (KIND_WATCH_SIGNAL, VERDICT_FAIR),
}

GRADE_GOOD = "Смена идёт хорошо"
GRADE_FAIR = "Есть замечания"
GRADE_BAD = "Есть нарушения регламента"
GRADE_EMPTY = "Оценивать пока нечего"


# --- Запись замечания -------------------------------------------------------


def record(db, call, kind, verdict, text, facts=None, employee=None, actor=None, now=None):
    """Сохраняет замечание. Коммит — за вызывающим: вместе с самим действием."""
    event = ShiftEvent(
        airport_icao=call.airport_icao,
        call_id=call.id,
        employee_id=employee.id if employee is not None else None,
        kind=kind,
        verdict=verdict,
        text=text,
        facts=facts or {},
        actor_name=actor.full_name if actor is not None else None,
        created_at=now or utc_now(),
    )
    db.add(event)
    return event


def board_of(call):
    """Бортовой номер вызова — так диспетчер называет вызов вслух."""
    return call.aircraft.board_number if call.aircraft else f"№{call.id}"


def minutes_between(start, end):
    """Минуты между двумя моментами, до десятых."""
    return round((end - start).total_seconds() / 60, 1)


def candidate_of(result, employee_id):
    """Кандидат из результата подбора по идентификатору сотрудника."""
    for candidate in result.candidates:
        if candidate.employee["id"] == employee_id:
            return candidate
    return None


def transport_text(candidate):
    """Как кандидат добирается — словами, которыми это сказал бы диспетчер."""
    vehicle = candidate.route.vehicle
    if vehicle is None:
        return "пешком"
    if candidate.route.pickup:
        return f"через машину {vehicle['call_sign']}"
    return f"на {vehicle['call_sign']}"


def reason_tail(reason):
    """Причина решения, если человек её указал."""
    return f" Причина: {reason}." if reason else ""


# --- Назначение -------------------------------------------------------------


def review_assignment(db, call, employee, result, actor, is_override, now=None):
    """
    Оценка назначения свободного исполнителя.

    Вызывается после назначения: в вызове уже записаны время прибытия
    и машина. result — подбор, выполненный непосредственно перед
    назначением: по нему видно, кого предлагала система и сколько минут
    стоит отступление от её предложения.
    """
    now = now or utc_now()
    margin = minutes_between(call.eta_at, deadline_of(call))
    waited = minutes_between(started_at(call), now)
    best = result.best
    chosen = candidate_of(result, employee.id)

    facts = {
        "minutes_to_assign": waited,
        "margin_min": margin,
        "override": is_override,
        "override_cost_min": 0.0,
    }

    if is_override and best is not None and chosen is not None:
        verdict, text = judge_override(call, employee, best, chosen, margin, facts)
    else:
        verdict, text = judge_plain(employee, chosen, margin, waited)

    return record(db, call, KIND_ASSIGN, verdict, text, facts, employee, actor, now)


def judge_override(call, employee, best, chosen, margin, facts):
    """
    Переопределение решения системы: во сколько минут оно обошлось.

    Само отступление от предложения — не ошибка: человек видит то, чего
    не видит система. Ошибка — когда из-за него срывается срок, который
    предложенный исполнитель выдерживал.
    """
    cost = round(chosen.minutes - best.minutes, 1)
    facts["override_cost_min"] = max(cost, 0.0)
    best_name = best.employee["full_name"]
    tail = reason_tail(call.override_reason)

    if margin < 0 and best.within_regulation:
        return VERDICT_BAD, (
            f"Решение системы переопределено: {employee.full_name} опоздает "
            f"на {overrun_text(-margin)}, а {best_name} ({transport_text(best)}) "
            f"успевал за {minutes_text(best.minutes)}.{tail}"
        )
    if cost >= OVERRIDE_TOLERANCE_MIN:
        return VERDICT_FAIR, (
            f"Решение системы переопределено: {employee.full_name} приедет "
            f"на {overrun_text(cost)} позже, чем {best_name}. "
            f"Запас до срока {minutes_text(max(margin, 0))}.{tail}"
        )
    return VERDICT_GOOD, (
        f"Выбран не тот, кого предложила система, но без потери времени: "
        f"{employee.full_name}, запас до срока {minutes_text(margin)}.{tail}"
    )


def judge_plain(employee, chosen, margin, waited):
    """Назначение предложенного системой: запас и скорость самого решения."""
    how = f", {transport_text(chosen)}" if chosen is not None else ""

    if margin < 0:
        return VERDICT_FAIR, (
            f"Назначен ближайший с допуском — {employee.full_name}{how}, но и он "
            f"опоздает на {overrun_text(-margin)}: в регламент не успевал никто."
        )
    if waited >= UNASSIGNED_ALERT_AFTER_MIN:
        return VERDICT_FAIR, (
            f"Исполнитель выбран верно — {employee.full_name}{how}, но назначение "
            f"через {minutes_text(waited)} после поступления вызова. "
            f"Запас до срока сократился до {minutes_text(margin)}."
        )
    if margin < DEADLINE_MARGIN_MIN:
        return VERDICT_FAIR, (
            f"Назначен ближайший с допуском — {employee.full_name}{how}, но запас "
            f"до срока всего {minutes_text(margin)}. Держите вызов на контроле."
        )
    return VERDICT_GOOD, (
        f"Назначен ближайший с допуском — {employee.full_name}{how}, "
        f"запас до срока {minutes_text(margin)}."
    )


# --- Очередь к занятому -----------------------------------------------------


def review_queue(db, call, employee, result, actor, now=None):
    """
    Оценка постановки вызова в очередь к занятому инженеру.

    Главный вопрос: был ли свободный исполнитель, который успевал.
    Если был, а вызов всё равно отправлен ждать, — это самая дорогая
    ошибка диспетчера: срыв договорного срока при наличии выхода.
    """
    now = now or utc_now()
    deadline = deadline_of(call)
    margin = minutes_between(call.eta_at, deadline) if call.eta_at else None
    best = result.best
    tail = reason_tail(call.override_reason)

    facts = {
        "minutes_to_assign": minutes_between(started_at(call), now),
        "margin_min": margin,
        "override": True,
        "override_cost_min": 0.0,
    }

    best_arrival = now + timedelta(minutes=best.minutes) if best is not None else None
    if best_arrival is not None and call.eta_at is not None and call.eta_at > best_arrival:
        facts["override_cost_min"] = minutes_between(best_arrival, call.eta_at)

    if margin is not None and margin >= 0:
        verdict = VERDICT_GOOD
        text = (
            f"Вызов в очереди, исполнитель — {employee.full_name}; срок при этом "
            f"выдерживается, запас {minutes_text(margin)}.{tail}"
        )
    elif best_arrival is not None and best_arrival <= deadline:
        verdict = VERDICT_BAD
        text = (
            f"Вызов поставлен в очередь, исполнитель — {employee.full_name}{late_part(margin)}, "
            f"а свободный {best.employee['full_name']} ({transport_text(best)}) "
            f"успевал за {minutes_text(best.minutes)}.{tail}"
        )
    elif best_arrival is not None and facts["override_cost_min"] >= OVERRIDE_TOLERANCE_MIN:
        verdict = VERDICT_FAIR
        text = (
            f"Вызов в очереди, исполнитель — {employee.full_name}{late_part(margin)}. "
            f"Свободный {best.employee['full_name']} тоже не успевал, но приехал бы "
            f"на {overrun_text(facts['override_cost_min'])} раньше.{tail}"
        )
    else:
        verdict = VERDICT_FAIR
        text = (
            f"Свободных с допуском нет: вызов в очереди, исполнитель — {employee.full_name}"
            f"{late_part(margin)}.{earlier_busy_note(result, employee)}{tail}"
        )

    return record(db, call, KIND_QUEUE, verdict, text, facts, employee, actor, now)


def late_part(margin):
    """«, срыв срока на N мин» — если срыв посчитан."""
    if margin is None or margin >= 0:
        return ""
    return f", срыв срока на {overrun_text(-margin)}"


def earlier_busy_note(result, employee):
    """
    Замечание, если раньше освободится другой занятый.

    Список занятых в подборе уже отсортирован по времени освобождения:
    если выбран не первый, диспетчеру стоит знать, кто первый.
    """
    for entry in result.busy:
        if not entry.get("busy_until"):
            break
        if entry["employee_id"] == employee.id:
            return ""
        return f" Раньше освободится {entry['full_name']}."
    return ""


# --- Снятие исполнителя -----------------------------------------------------


def review_unassign(db, call, employee, control_before, reason, actor, now=None):
    """
    Оценка снятия исполнителя.

    control_before — контроль регламента по вызову до снятия. Снять того,
    кто по прогнозу не успевает или пропал со связи, — своевременное
    вмешательство. Снять того, кто успевал, — потерянное время борта.
    """
    now = now or utc_now()
    board = board_of(call)
    waited = minutes_between(started_at(call), now)
    facts = {"waited_min": waited, "control_level": control_before.level}

    if control_before.level in ALERT_LEVELS:
        verdict = VERDICT_GOOD
        text = (
            f"{employee.full_name} снят с вызова {board} вовремя: "
            f"{lower_first(control_before.reason)}. Теперь назначьте замену.{reason_tail(reason)}"
        )
    else:
        verdict = VERDICT_FAIR
        text = (
            f"{employee.full_name} снят с вызова {board}, хотя по прогнозу укладывался "
            f"в срок. Борт ждёт уже {minutes_text(waited)}.{reason_tail(reason)}"
        )
    return record(db, call, KIND_UNASSIGN, verdict, text, facts, employee, actor, now)


def lower_first(text):
    """Первая буква строчной — чтобы причина встала в середину фразы."""
    return text[:1].lower() + text[1:] if text else text


# --- Прибытие ---------------------------------------------------------------


def review_arrival(db, call, employee, now=None):
    """Исполнитель у борта: в срок или с опозданием — итог по вызову."""
    now = now or utc_now()
    margin = minutes_between(now, deadline_of(call))
    took = minutes_between(started_at(call), now)
    facts = {"margin_min": margin, "in_time": margin >= 0, "arrival_min": took}

    if margin >= 0:
        verdict = VERDICT_GOOD
        text = (
            f"{employee.full_name} у борта {board_of(call)} через {minutes_text(took)} "
            f"после вызова — в срок, запас {minutes_text(margin)}."
        )
    else:
        verdict = VERDICT_BAD
        text = (
            f"{employee.full_name} прибыл к борту {board_of(call)} с опозданием "
            f"на {overrun_text(-margin)} — регламент нарушен."
        )
    return record(db, call, KIND_ARRIVAL, verdict, text, facts, employee, None, now)


# --- Наблюдение за обстановкой ----------------------------------------------


def observe(db, airport_icao, now=None):
    """
    Замечания по обстановке, которые не вызваны ничьим действием.

    Бездействие — тоже решение: вызов, который три минуты висит без
    исполнителя, никто не «сделал плохо», но Наставник обязан об этом
    сказать. Каждое наблюдение выносится один раз на вызов и исполнителя;
    вызовы в очереди пропускаются — их срыв уже оценён при постановке.
    """
    now = now or utc_now()
    calls = (
        db.query(Call)
        .filter(Call.airport_icao == airport_icao, Call.status.in_(ACTIVE_CALL_STATUSES))
        .all()
    )

    created = []
    for call in calls:
        if call.status == STATUS_QUEUED:
            continue
        control = assess(call, now)
        watch = WATCH_BY_LEVEL.get(control.level)
        if watch is None:
            continue
        kind, verdict = watch
        if already_noted(db, call, kind):
            continue
        facts = {"minutes_left": control.minutes_left, "margin_min": control.margin_min}
        text = f"{board_of(call)}: {lower_first(control.reason)}."
        if control.advice:
            text += f" {control.advice}."
        created.append(
            record(db, call, kind, verdict, text, facts, call.assigned_employee, None, now)
        )
    return created


def already_noted(db, call, kind):
    """Вынесено ли уже такое наблюдение по этому вызову и исполнителю."""
    return (
        db.query(ShiftEvent.id)
        .filter(
            ShiftEvent.call_id == call.id,
            ShiftEvent.kind == kind,
            ShiftEvent.employee_id == call.assigned_employee_id,
        )
        .first()
        is not None
    )


# --- Лента и итог смены -----------------------------------------------------


def window_start(now=None):
    """С какого момента собираются лента и итог."""
    return (now or utc_now()) - timedelta(hours=SHIFT_WINDOW_H)


def feed(db, airport_icao, limit, now=None):
    """Последние замечания по аэропорту, свежие первыми."""
    return (
        db.query(ShiftEvent)
        .filter(
            ShiftEvent.airport_icao == airport_icao,
            ShiftEvent.created_at >= window_start(now),
        )
        .order_by(ShiftEvent.created_at.desc(), ShiftEvent.id.desc())
        .limit(limit)
        .all()
    )


def summary(db, airport_icao, now=None):
    """
    Итог смены в цифрах — то, с чего начинается разбор.

    Считается по сохранённым замечаниям, а не по текущему состоянию
    вызовов: итог отвечает на вопрос «какие решения принимались»,
    а закрытый вызов о своих решениях уже ничего не помнит.
    """
    events = (
        db.query(ShiftEvent)
        .filter(
            ShiftEvent.airport_icao == airport_icao,
            ShiftEvent.created_at >= window_start(now),
        )
        .all()
    )

    verdicts = {VERDICT_GOOD: 0, VERDICT_FAIR: 0, VERDICT_BAD: 0}
    arrivals = 0
    in_time = 0
    assign_waits = []
    overrides = 0
    override_cost = 0.0

    for event in events:
        verdicts[event.verdict] = verdicts.get(event.verdict, 0) + 1
        facts = event.facts or {}
        if event.kind == KIND_ARRIVAL:
            arrivals += 1
            in_time += 1 if facts.get("in_time") else 0
        if event.kind in (KIND_ASSIGN, KIND_QUEUE):
            assign_waits.append(facts.get("minutes_to_assign") or 0.0)
            if facts.get("override"):
                overrides += 1
                override_cost += facts.get("override_cost_min") or 0.0

    return {
        "grade": grade_of(verdicts, len(events)),
        "verdicts": verdicts,
        "arrivals": arrivals,
        "arrivals_in_time": in_time,
        "in_time_percent": round(100 * in_time / arrivals) if arrivals else None,
        "assignments": len(assign_waits),
        "avg_minutes_to_assign": (
            round(sum(assign_waits) / len(assign_waits), 1) if assign_waits else None
        ),
        "overrides": overrides,
        "override_cost_min": round(override_cost, 1),
        "window_hours": SHIFT_WINDOW_H,
    }


def grade_of(verdicts, total):
    """Общая оценка смены одной фразой."""
    if total == 0:
        return GRADE_EMPTY
    if verdicts[VERDICT_BAD]:
        return GRADE_BAD
    if verdicts[VERDICT_FAIR]:
        return GRADE_FAIR
    return GRADE_GOOD


def event_dict(event, boards):
    """Замечание для API. boards — бортовые номера по идентификатору вызова."""
    return {
        "id": event.id,
        "call_id": event.call_id,
        "board_number": boards.get(event.call_id),
        "kind": event.kind,
        "verdict": event.verdict,
        "text": event.text,
        "actor_name": event.actor_name,
        "created_at": to_iso_utc(event.created_at),
    }
