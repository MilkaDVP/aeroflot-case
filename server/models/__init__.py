"""
Модели данных.

Импорт всех моделей в одном месте: SQLAlchemy должна увидеть каждый класс
до создания таблиц, иначе связи между ними не разрешатся.
"""

from models.aircraft import Aircraft
from models.airport import Airport
from models.call import Call
from models.defect_type import DefectType
from models.employee import Employee
from models.report import Report
from models.shift_event import ShiftEvent
from models.user import AuthSession, User
from models.vehicle import Vehicle

__all__ = [
    "Aircraft",
    "Airport",
    "AuthSession",
    "Call",
    "DefectType",
    "Employee",
    "Report",
    "ShiftEvent",
    "User",
    "Vehicle",
]
