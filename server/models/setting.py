"""Настройки системы: ключ и значение."""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Setting(Base):
    """Одна настройка, общая для всех."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)
