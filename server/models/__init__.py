"""
Модели данных.

Импорт всех моделей в одном месте: SQLAlchemy должна увидеть каждый класс
до создания таблиц, иначе связи между ними не разрешатся.
"""

from models.aircraft import Aircraft
from models.airport import Airport
from models.call import Call
from models.call_message import CallMessage
from models.defect_type import DefectType
from models.employee import Employee
from models.engineer_action import EngineerAction
from models.event_log import EventLog
from models.report import Report
from models.setting import Setting
from models.shift_event import ShiftEvent
from models.user import AuthSession, User
from models.vehicle import Vehicle

__all__ = [
    "Aircraft",
    "Airport",
    "AuthSession",
    "Call",
    "CallMessage",
    "DefectType",
    "Employee",
    "EngineerAction",
    "EventLog",
    "Report",
    "Setting",
    "ShiftEvent",
    "User",
    "Vehicle",
]
