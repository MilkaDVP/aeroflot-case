"""
Несколько инженеров на одном вызове и переписка по вызову.

Второй инженер оформляется вызовом поддержки — отдельной записью,
ссылающейся на основной вызов. Так каждый едет своим способом (один
на машине, другой пешком), у каждого свой статус в телефоне, а очередь,
машины и учения работают без особых случаев. Здесь — то, что относится
к группе целиком: кто в ней, когда прибыл первый, закрытие и переписка.

Срок прибытия исполнен, когда у борта появился первый из группы:
договор требует, чтобы к борту пришёл специалист, а не все назначенные.
"""

from sqlalchemy.orm import object_session

from clock import to_iso_utc, utc_now
from models.call import ACTIVE_CALL_STATUSES, STATUS_CLOSED, WORKING_CALL_STATUSES, Call
from models.call_message import CallMessage
from services.assignment import advance_after_close

MESSAGES_SHOWN = 20


def root_of(call):
    """Основной вызов группы: сам вызов либо тот, который он поддерживает."""
    if call.parent_call_id is None:
        return call
    return object_session(call).get(Call, call.parent_call_id) or call


def support_calls(call):
    """Незакрытые вызовы поддержки основного вызова, в порядке назначения."""
    session = object_session(call)
    if session is None or call.id is None:
        return []
    return (
        session.query(Call)
        .filter(Call.parent_call_id == call.id, Call.status.in_(ACTIVE_CALL_STATUSES))
        .order_by(Call.id)
        .all()
    )


def first_arrival(call):
    """
    Когда к борту прибыл первый инженер группы; None — ещё никто.

    Смотрит на всю группу, включая закрытые вызовы поддержки: помощник,
    который приехал первым и уже ушёл, срок всё равно исполнил.
    """
    session = object_session(call)
    root = root_of(call)
    moments = [root.first_arrived_at]
    if session is not None and root.id is not None:
        rows = session.query(Call.first_arrived_at).filter(Call.parent_call_id == root.id).all()
        moments.extend(row[0] for row in rows)
    known = [moment for moment in moments if moment is not None]
    return min(known) if known else None


def support_summary(call):
    """Инженеры поддержки для ответа API: кто едет вторым и где он сейчас."""
    return [
        {
            "call_id": item.id,
            "employee_id": item.assigned_employee_id,
            "employee_name": item.assigned_employee.full_name if item.assigned_employee else None,
            "status": item.status,
            "eta_at": to_iso_utc(item.eta_at),
            "vehicle_call_sign": item.vehicle.call_sign if item.vehicle else None,
            "route_node_ids": item.route_node_ids,
            "pickup_node_id": item.pickup_node_id,
        }
        for item in support_calls(call)
    ]


def close_support(db, call, closed_at=None):
    """
    Основной вызов закрыт — работы окончены, помощники свободны.

    Каждый вызов поддержки закрывается так же, как закрыл бы его сам
    инженер: его очередь продвигается, машина остаётся у борта.
    """
    for item in support_calls(call):
        was_working = item.status in WORKING_CALL_STATUSES
        item.status = STATUS_CLOSED
        item.closed_at = closed_at or utc_now()
        if item.assigned_employee is not None:
            advance_after_close(db, item.assigned_employee, item, was_working)


def add_message(db, call, author_role, author_name, text):
    """Записывает сообщение в переписку группы (к основному вызову)."""
    message = CallMessage(
        call_id=root_of(call).id,
        author_role=author_role,
        author_name=author_name,
        text=" ".join(text.split()),
    )
    db.add(message)
    return message


def messages_of(call):
    """Последние сообщения по вызову, старые первыми."""
    session = object_session(call)
    if session is None or call.id is None:
        return []
    rows = (
        session.query(CallMessage)
        .filter(CallMessage.call_id == root_of(call).id)
        .order_by(CallMessage.id.desc())
        .limit(MESSAGES_SHOWN)
        .all()
    )
    return [
        {
            "id": row.id,
            "author_role": row.author_role,
            "author_name": row.author_name,
            "text": row.text,
            "created_at": to_iso_utc(row.created_at),
        }
        for row in reversed(rows)
    ]
