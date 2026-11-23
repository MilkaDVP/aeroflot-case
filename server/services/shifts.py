from datetime import datetime, timedelta

from clock import utc_now
from constants import SHIFT_WINDOW_H, STATUS_OFFLINE
from models.call import ACTIVE_CALL_STATUSES, STATUS_ARRIVED, STATUS_QUEUED, Call
from models.employee import Employee
from services.assignment import release_vehicle, unassign


def start_clock(employee):
    employee.shift_ends_at = utc_now() + timedelta(hours=SHIFT_WINDOW_H)


def end_shift(db, employee, reason):
    calls = db.query(Call).filter(Call.assigned_employee_id == employee.id,
                                  Call.status.in_(ACTIVE_CALL_STATUSES)).all()
    for call in sorted(calls, key=lambda c: c.status != STATUS_QUEUED):
        unassign(db, call, reason)
        call.allow_previous_employee = True
        db.flush()
    release_vehicle(employee)
    employee.status = STATUS_OFFLINE
    employee.shift_ends_at = None


def initialize_shift_clocks(db):
    from models.user import AuthSession, User

    # Сохранить действующие входы при обновлении уже установленного приложения.
    engineer_accounts = db.query(User.id).filter(User.employee_id.is_not(None))
    db.query(AuthSession).filter(
        AuthSession.user_id.in_(engineer_accounts), AuthSession.expires_at > utc_now(),
        AuthSession.expires_at < datetime.max,
    ).update({AuthSession.expires_at: datetime.max}, synchronize_session=False)

    active = db.query(Employee).join(User, User.employee_id == Employee.id).filter(
        Employee.status != STATUS_OFFLINE, Employee.shift_ends_at.is_(None),
        Employee.archived.is_(False),
    )
    for employee in active:
        employee.shift_ends_at = None


def expire_shifts(db):
    for employee in db.query(Employee).filter(Employee.shift_ends_at <= utc_now()):
        from models.user import User, AuthSession
        online = db.query(AuthSession.token).join(User).filter(User.employee_id == employee.id).first()
        working = db.query(Call.id).filter(
            Call.assigned_employee_id == employee.id, Call.status == STATUS_ARRIVED).first()
        if working:
            # Начатые работы сами не снимаются: смена кончится после них.
            continue
        if employee.always_on_shift or online:
            employee.shift_ends_at = None
        else:
            end_shift(db, employee, "Смена завершена")
