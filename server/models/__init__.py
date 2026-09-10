"""
Модели данных.

Импорт всех моделей в одном месте: SQLAlchemy должна увидеть каждый класс
до создания таблиц, иначе связи между ними не разрешатся.
"""

from models.aircraft import Aircraft
from models.airport import Airport
from models.call import Call
from models.employee import Employee
from models.user import AuthSession, User

__all__ = ["Aircraft", "Airport", "AuthSession", "Call", "Employee", "User"]
