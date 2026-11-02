"""
Тип неисправности — запись открытого справочника.

Справочник хранится в базе, а не в коде. Закрытый список из дюжины пунктов
не покрывает того, что на перроне случается на самом деле, и диспетчер
должен иметь возможность завести нестандартный дефект в ту же минуту,
когда о нём сообщил экипаж, — с главой ATA и категорией допуска, по которым
система подберёт инженера.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from clock import utc_now
from database import Base


class DefectType(Base):
    """Тип неисправности: название, глава ATA и требуемое семейство отметки."""

    __tablename__ = "defect_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Код — постоянный ключ, на который ссылаются вызовы. Название можно
    # уточнить, код — нет: иначе прошлые вызовы потеряли бы свой дефект.
    code: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    # Глава ATA строкой: у встроенного «отказа запуска» это диапазон «71-80».
    ata: Mapped[str] = mapped_column(String)
    # Семейство отметки: A, B1 или B2. Подкатегорию (B1.1 и т. п.) даёт тип ВС.
    category: Mapped[str] = mapped_column(String)
    # Слова, по которым тип находится поиском, кроме самого названия:
    # экипаж говорит «течь», а в справочнике записано «утечка».
    keywords: Mapped[str] = mapped_column(String, default="")

    # Встроенный тип пришёл из constants.py, остальные заведены людьми.
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
