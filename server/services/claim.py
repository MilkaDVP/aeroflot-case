"""
Захват вызова диспетчером.

На смене несколько диспетчеров, и один вызов не должны разбирать двое
сразу: каждый подобрал бы своего исполнителя, и второй назначил бы
поверх первого. Поэтому вызов, который ещё ждёт назначения, диспетчер
«берёт в работу» — остальные видят его с пометкой, кто им занят, но
назначить, отменить или изменить его не могут.

Захват живёт, пока диспетчер активен: клиент подтверждает его раз
в полминуты, и без подтверждения он истекает через CLAIM_TTL. Так вызов
не остаётся заблокированным, если диспетчер закрыл вкладку или ушёл.
При выходе из системы захваты снимаются сразу. Начальник смены и старше
вправе забрать чужой захват себе.

У одного диспетчера в работе один вызов: взял следующий — предыдущий
освободился. Назначенный, отменённый или закрытый вызов захвата не имеет.
"""

from datetime import timedelta

from fastapi import HTTPException, status

from clock import to_iso_utc, utc_now
from models.call import STATUS_NEW, STATUS_SUGGESTED, Call
from models.user import ROLE_SHIFT_SUPERVISOR, role_at_least

# Сколько живёт захват без подтверждения от клиента.
CLAIM_TTL = timedelta(minutes=2)

# Захватывать имеет смысл только вызов, который ещё ждёт назначения.
CLAIMABLE_STATUSES = (STATUS_NEW, STATUS_SUGGESTED)

ERROR_CLAIMED = "Вызов разбирает {name}. Назначить и изменить его может только он"
ERROR_NOT_CLAIMABLE = "Вызов уже не ждёт назначения — брать его в работу не нужно"


def is_active(call, now=None):
    """Есть ли у вызова действующий захват."""
    if call.claimed_by_user_id is None or call.claimed_at is None:
        return False
    if call.status not in CLAIMABLE_STATUSES:
        return False
    return (now or utc_now()) - call.claimed_at < CLAIM_TTL


def holder_id(call, now=None):
    """Кто держит вызов, или None."""
    return call.claimed_by_user_id if is_active(call, now) else None


def held_by_other(call, user, now=None):
    holder = holder_id(call, now)
    return holder is not None and holder != user.id


def clear(call):
    call.claimed_by_user_id = None
    call.claimed_at = None


def take(db, call, user, now=None):
    """
    Отдаёт вызов пользователю и освобождает другие его вызовы:
    у диспетчера в работе один вызов.
    """
    now = now or utc_now()
    others = db.query(Call).filter(
        Call.claimed_by_user_id == user.id, Call.id != call.id
    )
    for other in others:
        clear(other)
    call.claimed_by_user_id = user.id
    call.claimed_at = now
    # Связь с пользователем только для чтения и сама не обновится:
    # без этого ответ показал бы прежнего держателя.
    db.flush()
    db.expire(call, ["claimed_by"])


def claim(db, call, user, takeover=False):
    """
    Взять вызов в работу (или подтвердить, что всё ещё в работе).

    Чужой действующий захват забрать может только начальник смены
    и старше, и только явно — флагом takeover.
    """
    if call.status not in CLAIMABLE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_NOT_CLAIMABLE)
    if held_by_other(call, user):
        if not (takeover and role_at_least(user.role, ROLE_SHIFT_SUPERVISOR)):
            raise HTTPException(
                status.HTTP_409_CONFLICT, ERROR_CLAIMED.format(name=holder_name(call))
            )
    take(db, call, user)


def release(call, user):
    """Отпустить свой вызов. Чужой захват так не снимается."""
    if call.claimed_by_user_id == user.id:
        clear(call)


def ensure_can_change(db, call, user):
    """
    Проверка перед изменением вызова, ждущего назначения.

    Если вызов держит другой диспетчер — отказ (кроме начальника смены
    и старше). Если не держит никто —
    действие само берёт вызов в работу: так и запрос к API без явного
    захвата не обходит правило, и интерфейс не обязан помнить про захват.
    """
    if call.status not in CLAIMABLE_STATUSES:
        return
    # Начальник смены и старше вмешиваются в чужой вызов — действие
    # переводит его на них.
    if held_by_other(call, user) and not role_at_least(user.role, ROLE_SHIFT_SUPERVISOR):
        raise HTTPException(
            status.HTTP_409_CONFLICT, ERROR_CLAIMED.format(name=holder_name(call))
        )
    take(db, call, user)


def release_all(db, user_id):
    """Снимает все захваты пользователя — при выходе из системы."""
    for call in db.query(Call).filter(Call.claimed_by_user_id == user_id):
        clear(call)


def holder_name(call):
    return call.claimed_by.full_name if call.claimed_by else "другой диспетчер"


def summary(call, now=None):
    """Поля захвата для ответа API."""
    if not is_active(call, now):
        return {"claimed_by_login": None, "claimed_by_name": None, "claim_expires_at": None}
    return {
        "claimed_by_login": call.claimed_by.login if call.claimed_by else None,
        "claimed_by_name": holder_name(call),
        "claim_expires_at": to_iso_utc(call.claimed_at + CLAIM_TTL),
    }
