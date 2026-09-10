"""
Подключение к базе данных.

SQLite выбран сознательно: у проекта не должно быть внешних зависимостей,
которые жюри придётся поднимать отдельно. Вся база — один файл, он
создаётся при первом запуске и наполняется тестовыми данными.
"""

import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./aeroflot.db")

# check_same_thread отключается только для SQLite: FastAPI обслуживает
# запросы в пуле потоков, а SQLite по умолчанию запрещает работу
# с соединением из другого потока.
CONNECT_ARGS = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=CONNECT_ARGS)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Базовый класс всех моделей."""


def get_db():
    """Сессия БД на время одного запроса. Зависимость FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_all():
    """Создаёт таблицы, которых ещё нет, и досоздаёт новые колонки."""
    # Импорт нужен ради регистрации моделей в метаданных Base.
    import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    added = add_missing_columns(engine)
    if added:
        print(f"База дополнена колонками: {', '.join(added)}")


def add_missing_columns(target_engine):
    """
    Досоздаёт колонки, появившиеся в моделях после создания базы.

    Зачем. База в Docker лежит в томе и переживает пересборку образа,
    а create_all создаёт только отсутствующие таблицы — новую колонку
    в существующую таблицу он не добавит. Без этого после обновления
    сервер падал бы на старой базе при первом же запросе.

    Ограничение сознательное: добавляются только колонки, допускающие NULL,
    — их можно дописать в SQLite без значения по умолчанию и без переноса
    данных. Всё сложнее (переименование, смена типа, NOT NULL) требует
    полноценных миграций через Alembic, и такая колонка здесь остановит
    запуск с понятным сообщением, а не испортит данные молча.

    Возвращает список добавленных колонок вида «таблица.колонка».
    """
    inspector = inspect(target_engine)
    added = []

    with target_engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {column["name"] for column in inspector.get_columns(table.name)}

            for column in table.columns:
                if column.name in existing:
                    continue
                if not column.nullable:
                    raise RuntimeError(
                        f"Колонку {table.name}.{column.name} (NOT NULL) нельзя "
                        "добавить автоматически — нужна миграция"
                    )
                column_type = column.type.compile(dialect=target_engine.dialect)
                connection.execute(
                    text(
                        f'ALTER TABLE "{table.name}" '
                        f'ADD COLUMN "{column.name}" {column_type}'
                    )
                )
                added.append(f"{table.name}.{column.name}")

    return added
