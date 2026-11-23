"""
Журнал событий (п. 3.2): что пишется, когда и как.

Источники записей:

1. Изменения заявки. Код назначения, снятия, прибытия, учений и
   перестроения маршрута разбросан по модулям, и вызов «записать
   в журнал» в каждом месте рано или поздно где-нибудь забыли бы. Поэтому
   журнал слушает саму сессию базы: перед каждой записью он сравнивает
   заявку с тем, что было в базе, и превращает разницу в события —
   фиксация, назначение, переназначение, снятие, смена статуса, маршрут,
   прогноз прибытия, фактическое прибытие, норматив. Событие попадает
   в журнал в той же транзакции, что и само изменение: откатилось
   изменение — нет и записи, записано — запись есть.

2. Подбор кандидатов — явной записью в run_suggestion: с обоснованием,
   причинами отказов и затраченным временем.

3. Контроль норматива — такт (watch): прогноз прибытия по текущим
   координатам, предупреждения о риске и факт нарушения, даже когда
   никто ничего не нажимает.

4. Запросы к API (services/journal_http.py): действия пользователей,
   ошибки приёма данных, отказы и сбои сервера.

Все записи журнала связаны цепочкой хэшей (см. models/event_log.py).
"""

import hashlib
import json
from datetime import timedelta

from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session, object_session
from sqlalchemy.orm.base import NEVER_SET, NO_VALUE

from clock import to_iso_utc, utc_now
from constants import REGULATION_ARRIVAL_LIMIT_MIN
from models.event_log import EventLog

# --- Перечень событий ---

CATEGORY_TITLES = {
    "call": "Заявка",
    "selection": "Подбор",
    "assignment": "Назначение",
    "status": "Статус",
    "route": "Маршрут",
    "forecast": "Прибытие",
    "deadline": "Норматив",
    "error": "Ошибка",
    "user": "Действие пользователя",
}

# Тип → (раздел, название). Первые строки — перечень п. 3.2.
EVENT_TYPES = {
    "report_received": ("call", "Сообщение о неисправности принято"),
    "call_created": ("call", "Фиксация заявки"),
    "candidates": ("selection", "Формирование списка кандидатов"),
    "assigned": ("assignment", "Назначение исполнителя"),
    "reassigned": ("assignment", "Переназначение заявки"),
    "unassigned": ("assignment", "Исполнитель снят с заявки"),
    "status": ("status", "Изменение статуса заявки"),
    "call_flags": ("status", "Изменение признаков заявки"),
    "route_built": ("route", "Построение маршрута"),
    "route_rebuilt": ("route", "Пересчёт маршрута"),
    "forecast": ("forecast", "Прогноз прибытия к ВС"),
    "arrival": ("forecast", "Фактическое прибытие к ВС"),
    "deadline_risk": ("deadline", "Предупреждение о риске нарушения норматива"),
    "deadline_violation": ("deadline", "Нарушение норматива"),
    "deadline_met": ("deadline", "Норматив выполнен"),
    "data_error": ("error", "Ошибка приёма данных"),
    "action_rejected": ("error", "Действие отклонено"),
    "server_error": ("error", "Ошибка сервера"),
    "login": ("user", "Вход в систему"),
    "login_failed": ("user", "Неудачный вход"),
    "user_action": ("user", "Действие пользователя"),
}

STATUS_TITLES = {
    "new": "Новая",
    "suggested": "Подобран кандидат",
    "queued": "В очереди",
    "assigned": "Назначена",
    "accepted": "Принята инженером",
    "arrived": "Инженер у борта",
    "closed": "Выполнена",
    "cancelled": "Отменена",
}

ROLE_TITLES = {
    "admin": "Технический администратор",
    "local_admin": "Администратор аэропорта",
    "shift_supervisor": "Начальник смены",
    "dispatcher": "Диспетчер",
    "engineer": "Инженер",
    "external": "Интеграция",
}

ACTOR_KEY = "journal_actor"
PENDING_KEY = "journal_pending"
CHAIN_KEY = "journal_chain"

# Поля заявки, изменения которых становятся событиями.
TRACKED = ("status", "route_node_ids", "eta_at", "first_arrived_at", "urgent", "aog", "work_stage")
_MISSING = object()


# --- Инициатор ---

def actor_of(user):
    """Кто действует: пользователь системы."""
    if user is None:
        return None
    return {"id": user.id, "name": user.full_name, "role": user.role}


def set_actor(db, user):
    """Запоминает в сессии базы, от чьего имени идёт запрос."""
    db.info[ACTOR_KEY] = actor_of(user)


# --- Запись ---

def clean(params):
    """Параметры в виде, который JSON-колонка вернёт без изменений."""
    return json.loads(json.dumps(params or {}, ensure_ascii=False, default=str))


def record(db, type_, text_, *, airport_icao=None, call=None, call_id=None, board=None,
           employee=None, employee_id=None, employee_name=None, duration_ms=None,
           params=None, actor=_MISSING, dedup_key=None, ts=None):
    """Добавляет событие в сессию; запишется вместе с её транзакцией."""
    category = EVENT_TYPES[type_][0]
    if actor is _MISSING:
        actor = db.info.get(ACTOR_KEY)
    if call is not None:
        call_id = call.id
        airport_icao = airport_icao or call.airport_icao
        board = board or board_of(db, call)
    if employee is not None:
        employee_id, employee_name = employee.id, employee.full_name
    entry = EventLog(
        ts=ts or utc_now(),
        airport_icao=airport_icao,
        category=category,
        type=type_,
        text=text_,
        call_id=call_id,
        board_number=board,
        actor_user_id=actor["id"] if actor else None,
        actor_name=actor["name"] if actor else None,
        actor_role=actor["role"] if actor else None,
        employee_id=employee_id,
        employee_name=employee_name,
        duration_ms=round(duration_ms, 3) if duration_ms is not None else None,
        params=clean(params),
        dedup_key=dedup_key,
    )
    db.add(entry)
    return entry


def already(db, key):
    """Было ли уже событие с этим ключом (в базе или в этой транзакции)."""
    for item in db.new:
        if isinstance(item, EventLog) and item.dedup_key == key:
            return True
    return db.query(EventLog.id).filter(EventLog.dedup_key == key).first() is not None


def board_of(db, call):
    from models.aircraft import Aircraft

    aircraft = db.get(Aircraft, call.aircraft_id) if call.aircraft_id else None
    return aircraft.board_number if aircraft else None


def person(db, employee_id):
    from models.employee import RouteParticipant

    return db.get(RouteParticipant, employee_id) if employee_id else None


# --- Цепочка хэшей ---

def digest(entry):
    """SHA-256 содержимого записи вместе с хэшем предыдущей."""
    payload = [
        entry.ts.isoformat(timespec="microseconds"),
        entry.airport_icao, entry.category, entry.type, entry.text,
        entry.call_id, entry.board_number,
        entry.actor_user_id, entry.actor_name, entry.actor_role,
        entry.employee_id, entry.employee_name,
        entry.duration_ms,
        json.dumps(entry.params or {}, ensure_ascii=False, sort_keys=True),
        entry.dedup_key, entry.prev_hash,
    ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@event.listens_for(EventLog, "before_insert")
def _chain(mapper, connection, target):
    session = object_session(target)
    info = session.info if session is not None else connection.info
    previous = info.get(CHAIN_KEY, _MISSING)
    if previous is _MISSING:
        row = connection.execute(text("SELECT hash FROM event_log ORDER BY id DESC LIMIT 1")).first()
        previous = row[0] if row else None
    if target.ts is None:
        target.ts = utc_now()
    target.params = clean(target.params)
    target.prev_hash = previous
    target.hash = digest(target)
    info[CHAIN_KEY] = target.hash


def verify(db):
    """
    Проверка целостности: пересчитывает хэши по порядку записи.

    Возвращает (всего записей, номер первой испорченной записи или None).
    """
    previous = None
    count = 0
    query = db.query(EventLog).order_by(EventLog.id)
    for entry in query:
        count += 1
        if entry.prev_hash != previous or entry.hash != digest(entry):
            return count, entry.id
        previous = entry.hash
    return count, None


# --- Изменения заявки ---

def _old(state, key):
    """Значение в базе до изменения; _MISSING — поле не менялось."""
    value = state.committed_state.get(key, _MISSING)
    if value is _MISSING:
        return _MISSING
    # Поле не было загружено до изменения: прежнее значение неизвестно.
    return None if value is NO_VALUE or value is NEVER_SET else value


def snapshot(call):
    """Разница между заявкой в сессии и в базе — до записи."""
    state = inspect(call)
    changes = {}
    for key in TRACKED:
        old = _old(state, key)
        if old is _MISSING:
            continue
        new = getattr(call, key)
        if old != new:
            changes[key] = (old, new)

    # Исполнителя код меняет через связь, внешний ключ обновится только
    # при записи: старое значение — в колонке, новое — в связи.
    if "assigned_employee" in state.committed_state or "assigned_employee_id" in state.committed_state:
        old_id = _old(state, "assigned_employee_id")
        if old_id is _MISSING:
            old_id = call.assigned_employee_id
        new_id = call.assigned_employee.id if call.assigned_employee is not None else None
        if "assigned_employee" not in state.committed_state:
            new_id = call.assigned_employee_id
        if old_id != new_id:
            changes["employee"] = (old_id, new_id)
    return changes


@event.listens_for(Session, "before_flush")
def _collect(session, flush_context, instances):
    from models.call import Call

    session.info.pop(CHAIN_KEY, None)
    pending = []
    for item in session.new:
        if isinstance(item, Call):
            pending.append((item, None))
    for item in session.dirty:
        if isinstance(item, Call) and session.is_modified(item, include_collections=False):
            changes = snapshot(item)
            if changes:
                pending.append((item, changes))
    session.info[PENDING_KEY] = (pending, utc_now(), session.info.get(ACTOR_KEY))


@event.listens_for(Session, "after_flush_postexec")
def _emit(session, flush_context):
    pending, ts, actor = session.info.pop(PENDING_KEY, ([], None, None))
    for call, changes in pending:
        if changes is None:
            created(session, call, ts, actor)
        else:
            changed(session, call, changes, ts, actor)


@event.listens_for(Session, "after_soft_rollback")
def _forget(session, previous_transaction):
    session.info.pop(PENDING_KEY, None)
    session.info.pop(CHAIN_KEY, None)


def deadline_at(call):
    return (call.reported_at or call.created_at) + timedelta(minutes=REGULATION_ARRIVAL_LIMIT_MIN)


def minutes(value):
    return f"{value:.1f}".replace(".", ",")


def clock(db, call, value):
    """Время по местным часам аэропорта заявки — как его видит диспетчер."""
    from models.airport import Airport
    from services.shift_clock import local_now

    if value is None:
        return "—"
    airport = db.get(Airport, call.airport_icao)
    if airport is None:
        return value.strftime("%H:%M:%S") + " UTC"
    return local_now(airport, value).strftime("%H:%M:%S")


def created(db, call, ts, actor):
    from models.defect_type import DefectType

    defect = db.query(DefectType).filter(DefectType.code == call.defect_code).first()
    source = call.source or "диспетчер"
    flags = ", ".join(flag for flag, on in (("AOG", call.aog), ("срочная", call.urgent)) if on)
    record(
        db, "call_created",
        f"Заявка зафиксирована: {defect.name if defect else call.defect_code}"
        f"{' (' + flags + ')' if flags else ''}, источник — {source}",
        call=call, ts=ts, actor=actor,
        params={
            "неисправность": defect.name if defect else call.defect_code,
            "код": call.defect_code,
            "описание": call.defect_description,
            "допуск": call.required_mark,
            "стоянка": call.stand_node_id,
            "источник": source,
            "AOG": bool(call.aog),
            "срочная": bool(call.urgent),
            "сообщено_экипажем": to_iso_utc(call.reported_at),
            "контрольный_срок": to_iso_utc(deadline_at(call)),
        },
    )


def changed(db, call, changes, ts, actor):
    if "employee" in changes:
        employee_change(db, call, *changes["employee"], ts, actor)
    if "status" in changes:
        old, new = changes["status"]
        record(
            db, "status",
            f"Статус: {STATUS_TITLES.get(old, old)} → {STATUS_TITLES.get(new, new)}",
            call=call, employee=call.assigned_employee, ts=ts, actor=actor,
            params={"было": old, "стало": new},
        )
    if "route_node_ids" in changes:
        route_change(db, call, *changes["route_node_ids"], ts, actor)
    if "first_arrived_at" in changes and changes["first_arrived_at"][1] is not None:
        arrival(db, call, changes["first_arrived_at"][1], ts, actor)
    elif "eta_at" in changes and changes["eta_at"][1] is not None and call.first_arrived_at is None:
        forecast(db, call, changes["eta_at"][1], ts, actor, "расчёт маршрута")
    flags = {key: changes[key] for key in ("urgent", "aog", "work_stage") if key in changes}
    if flags:
        names = {"urgent": "срочность", "aog": "AOG", "work_stage": "этап работ"}
        record(
            db, "call_flags",
            "Изменено: " + ", ".join(f"{names[k]} {v[0]} → {v[1]}" for k, v in flags.items()),
            call=call, ts=ts, actor=actor,
            params={names[k]: {"было": v[0], "стало": v[1]} for k, v in flags.items()},
        )


def employee_change(db, call, old_id, new_id, ts, actor):
    new = person(db, new_id)
    old = person(db, old_id)
    params = {
        "исполнитель": new.full_name if new else None,
        "предложен_системой": person(db, call.suggested_employee_id).full_name
        if call.suggested_employee_id else None,
        "переопределение": bool(call.override_reason),
        "причина_переопределения": call.override_reason,
        "прогноз_мин": call.eta_minutes,
        "прогноз_прибытия": to_iso_utc(call.eta_at),
        "транспорт": call.vehicle.call_sign if call.vehicle else "пешком",
        "в_очередь": call.status == "queued",
    }
    if new_id is None:
        record(
            db, "unassigned",
            f"Снят исполнитель {old.full_name if old else '—'}: {call.unassign_reason or 'без причины'}",
            call=call, employee=old, ts=ts, actor=actor,
            params={"снят": old.full_name if old else None, "причина": call.unassign_reason},
        )
        return
    previous = old or (person(db, call.previous_employee_id) if call.previous_employee_id != new_id else None)
    if previous is not None:
        params["был"] = previous.full_name
        params["причина_снятия"] = call.unassign_reason
        record(
            db, "reassigned",
            f"Переназначено: {previous.full_name} → {new.full_name}",
            call=call, employee=new, ts=ts, actor=actor, params=params,
        )
        return
    queued = " (в очередь)" if call.status == "queued" else ""
    eta = f", прибытие через {minutes(call.eta_minutes)} мин" if call.eta_minutes is not None and not queued else ""
    record(
        db, "assigned",
        f"Назначен {new.full_name}{queued}{eta}",
        call=call, employee=new, ts=ts, actor=actor, params=params,
    )


def route_length_m(call, node_ids):
    from algorithm.geo import haversine_m
    from graph_registry import get_graph

    try:
        graph = get_graph(call.airport_icao)
        points = [graph.node(node_id) for node_id in node_ids]
    except Exception:  # граф не загружен — длина неизвестна
        return None
    return round(sum(
        haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) for a, b in zip(points, points[1:])
    ), 1)


def route_change(db, call, old, new, ts, actor):
    if not new:
        return
    length = route_length_m(call, new)
    rebuilt = bool(old)
    record(
        db, "route_rebuilt" if rebuilt else "route_built",
        ("Маршрут пересчитан" if rebuilt else "Маршрут построен")
        + f": {len(new)} узл., {round(length) if length is not None else '—'} м"
        + (f", {minutes(call.eta_minutes)} мин" if call.eta_minutes is not None else ""),
        call=call, employee=call.assigned_employee, ts=ts, actor=actor,
        params={
            "узлов": len(new),
            "длина_м": length,
            "в_пути_мин": call.eta_minutes,
            "транспорт": call.vehicle.call_sign if call.vehicle else "пешком",
            "точка_посадки": call.pickup_node_id,
            "старт": [call.route_start_lat, call.route_start_lon],
            "узлы": new,
        },
    )


def forecast(db, call, at, ts, actor, basis, extra=None):
    deadline = deadline_at(call)
    margin = (deadline - at).total_seconds() / 60
    _last_forecast[call.id] = at
    record(
        db, "forecast",
        f"Прогноз прибытия к ВС: {clock(db, call, at)}, "
        + (f"запас {minutes(margin)} мин" if margin >= 0 else f"опоздание {minutes(-margin)} мин"),
        call=call, employee=call.assigned_employee, ts=ts, actor=actor,
        params={
            "прогноз_прибытия": to_iso_utc(at),
            "контрольный_срок": to_iso_utc(deadline),
            "запас_мин": round(margin, 1),
            "основание": basis,
            **(extra or {}),
        },
    )


def arrival(db, call, at, ts, actor):
    deadline = deadline_at(call)
    started = call.reported_at or call.created_at
    spent = (at - started).total_seconds() / 60
    margin = (deadline - at).total_seconds() / 60
    employee = call.assigned_employee
    params = {
        "прибыл": to_iso_utc(at),
        "заявка_с": to_iso_utc(started),
        "контрольный_срок": to_iso_utc(deadline),
        "затрачено_мин": round(spent, 1),
        "норматив_мин": REGULATION_ARRIVAL_LIMIT_MIN,
    }
    record(
        db, "arrival",
        f"Прибыл к ВС в {clock(db, call, at)}: через {minutes(spent)} мин после фиксации",
        call=call, employee=employee, ts=ts, actor=actor, params=params,
    )
    if margin >= 0:
        record(
            db, "deadline_met",
            f"Норматив {REGULATION_ARRIVAL_LIMIT_MIN} мин выполнен, запас {minutes(margin)} мин",
            call=call, employee=employee, ts=ts, actor=actor,
            params={**params, "запас_мин": round(margin, 1)},
            dedup_key=f"deadline:{call.id}",
        )
    elif not already(db, f"violation:{call.id}"):
        record(
            db, "deadline_violation",
            f"Норматив {REGULATION_ARRIVAL_LIMIT_MIN} мин нарушен: прибытие с опозданием {minutes(-margin)} мин",
            call=call, employee=employee, ts=ts, actor=actor,
            params={**params, "опоздание_мин": round(-margin, 1)},
            dedup_key=f"violation:{call.id}",
        )


# --- Подбор кандидатов ---

def candidates(db, call, result, duration_ms, considered, purpose):
    """Список кандидатов с обоснованием и затраченным временем."""
    data = result.as_dict()
    best = data["best"]
    text_ = (
        f"Список кандидатов за {duration_ms:.0f} мс: подходят {len(data['candidates'])}, "
        f"отклонены {len(data['rejected'])}, заняты {len(data['busy'])}"
        + (f"; лучший — {best['full_name']} ({minutes(best['minutes'])} мин)" if best else "; подходящих нет")
    )
    record(
        db, "candidates", text_,
        call=call, employee_id=best["employee_id"] if best else None,
        employee_name=best["full_name"] if best else None,
        duration_ms=duration_ms,
        params={
            "назначение": purpose,
            "допуск": data["required_mark"],
            "рассмотрено": considered,
            "время_алгоритма_мс": data["elapsed_ms"],
            "обоснование": data["message"],
            "в_нормативе": data["within_regulation"],
            "кандидаты": [
                {
                    "сотрудник": c["full_name"],
                    "отметка": c["mark"],
                    "мин": c["minutes"],
                    "транспорт": c["vehicle_call_sign"] or "пешком",
                    "в_нормативе": c["within_regulation"],
                }
                for c in data["candidates"]
            ],
            "отклонены": [
                {"сотрудник": r.get("full_name"), "причина": r.get("reason")} for r in data["rejected"]
            ],
            "заняты": [
                {"сотрудник": b.get("full_name"), "до": b.get("busy_until")} for b in data["busy"]
            ],
        },
    )


# --- Такт контроля норматива ---

# Последний записанный прогноз по заявке: новый пишется, когда он
# сдвинулся на минуту и больше, — не на каждом такте.
_last_forecast = {}
FORECAST_STEP_S = 60


def watch(db, now=None):
    """Прогнозы, предупреждения о риске и нарушения норматива по всем заявкам."""
    from models.call import ACTIVE_CALL_STATUSES, Call
    from services.deadline import (
        LEVEL_LATE,
        LEVEL_NO_SIGNAL,
        LEVEL_UNASSIGNED,
        LEVEL_WARN,
        assess,
    )
    from services.support import first_arrival

    now = now or utc_now()
    risky = (LEVEL_WARN, LEVEL_LATE, LEVEL_NO_SIGNAL, LEVEL_UNASSIGNED)
    calls = db.query(Call).filter(
        Call.status.in_(ACTIVE_CALL_STATUSES), Call.parent_call_id.is_(None)
    ).all()
    for call in calls:
        try:
            control = assess(call, now)
        except Exception:  # граф не загружен и т. п. — пропускаем заявку
            continue
        arrived = call.first_arrived_at is not None or first_arrival(call) is not None
        employee = call.assigned_employee

        if control.forecast_at is not None and not arrived:
            last = _last_forecast.get(call.id)
            if last is None or abs((control.forecast_at - last).total_seconds()) >= FORECAST_STEP_S:
                forecast(db, call, control.forecast_at, now, None, "текущие координаты исполнителя",
                         {"возраст_координат_с": control.position_age_s})

        if control.level in risky and not arrived:
            key = f"risk:{call.id}:{control.level}:{employee.id if employee else 0}"
            if not already(db, key):
                record(
                    db, "deadline_risk",
                    f"Риск нарушения норматива: {control.reason}",
                    call=call, employee=employee, ts=now, actor=None, dedup_key=key,
                    params={
                        "уровень": control.level,
                        "причина": control.reason,
                        "решение": control.advice,
                        "осталось_мин": control.minutes_left,
                        "прогноз_прибытия": to_iso_utc(control.forecast_at),
                        "контрольный_срок": to_iso_utc(control.deadline_at),
                    },
                )

        if not arrived and now > control.deadline_at:
            key = f"violation:{call.id}"
            if not already(db, key):
                record(
                    db, "deadline_violation",
                    f"Норматив {REGULATION_ARRIVAL_LIMIT_MIN} мин нарушен: срок истёк, исполнителя у борта нет",
                    call=call, employee=employee, ts=now, actor=None, dedup_key=key,
                    params={
                        "контрольный_срок": to_iso_utc(control.deadline_at),
                        "исполнитель": employee.full_name if employee else None,
                        "статус": call.status,
                    },
                )
