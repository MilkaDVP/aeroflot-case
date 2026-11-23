"""
Подключение к базе данных.

SQLite выбран сознательно: у проекта не должно быть внешних зависимостей,
которые жюри придётся поднимать отдельно. Вся база — один файл, он
создаётся при первом запуске и наполняется тестовыми данными.
"""

import os

from fastapi import Depends
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


def begin_dispatch_write(db):
    """Резервируем запись до чтения назначений, очередей и свободных стоянок."""
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))


def get_dispatch_db(db=Depends(get_db)):
    begin_dispatch_write(db)
    return db


def create_all():
    """Создаёт таблицы, которых ещё нет, и досоздаёт новые колонки."""
    # Импорт нужен ради регистрации моделей в метаданных Base.
    import models  # noqa: F401

    with engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.execute(text("BEGIN IMMEDIATE"))
        schema = inspect(connection)
        legacy = schema.has_table("employees") and not schema.has_table("route_participants")
        if legacy:
            connection.execute(text("ALTER TABLE employees RENAME TO route_participants"))
        Base.metadata.create_all(bind=connection)
        if legacy:
            columns = {c["name"] for c in inspect(connection).get_columns("route_participants")}
            flag = "is_training" if "is_training" in columns else "0"
            connection.execute(text(f"INSERT INTO employees (id) SELECT id FROM route_participants WHERE {flag} = 0"))
    added = add_missing_columns(engine)
    if added:
        print(f"База дополнена колонками: {', '.join(added)}")

    retire_legacy_training(engine)


def retire_legacy_training(target_engine):
    """Удаляет старую команду, сохраняя историю закрытых вызовов и выданный транспорт."""
    with target_engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        cols = {col["name"] for col in inspect(conn).get_columns("route_participants")}
        if "is_training" not in cols:
            return
        bots = "SELECT id FROM route_participants WHERE is_training = 1"
        active = "status NOT IN ('closed', 'cancelled')"
        conn.exec_driver_sql(f"""UPDATE vehicles SET status='free', employee_id=NULL
            WHERE employee_id IN ({bots}) OR (status='reserved' AND employee_id IS NULL
            AND id IN (SELECT vehicle_id FROM calls WHERE assigned_employee_id IN ({bots}) AND {active}))""")
        conn.exec_driver_sql(f"""UPDATE calls SET status=CASE WHEN parent_call_id IS NULL THEN 'new' ELSE 'cancelled' END,
            closed_at=CASE WHEN parent_call_id IS NOT NULL THEN CURRENT_TIMESTAMP END,
            previous_employee_id=assigned_employee_id, assigned_employee_id=NULL, suggested_employee_id=NULL,
            route_node_ids=NULL, vehicle_id=NULL, pickup_node_id=NULL, eta_at=NULL, eta_minutes=NULL,
            claimed_by_user_id=NULL, claimed_at=NULL, queued_at=NULL, first_arrived_at=NULL, work_stage=NULL,
            aog=0, require_vehicle_pickup=0, override_reason=NULL, unassign_reason='Завершение учебного режима'
            WHERE assigned_employee_id IN ({bots}) AND {active}""")
        conn.exec_driver_sql(f"""UPDATE calls SET suggested_employee_id=NULL,
            status=CASE WHEN status='suggested' THEN 'new' ELSE status END
            WHERE suggested_employee_id IN ({bots}) AND {active}""")
        conn.exec_driver_sql("DROP TABLE IF EXISTS training_engineers")
        # Оставляем только архивные карточки, на которые ссылается история.
        conn.exec_driver_sql(f"""DELETE FROM route_participants WHERE id IN ({bots})
            AND NOT EXISTS (SELECT 1 FROM calls WHERE route_participants.id IN
                            (previous_employee_id, assigned_employee_id, suggested_employee_id))
            AND NOT EXISTS (SELECT 1 FROM shift_events WHERE employee_id=route_participants.id)
            AND NOT EXISTS (SELECT 1 FROM engineer_actions WHERE employee_id=route_participants.id)""")
        conn.exec_driver_sql(f"UPDATE route_participants SET archived=1,status='offline' WHERE id IN ({bots})")
        conn.exec_driver_sql(f"INSERT OR IGNORE INTO employees (id) {bots}")
        for table, obsolete in {"airports": ("drill_enabled", "drill_speed"),
                                "calls": ("drill_work_s", "sim_progress_m"),
                                "route_participants": ("drill_return_path", "held", "is_training")}.items():
            existing = {col["name"] for col in inspect(conn).get_columns(table)}
            for name in obsolete:
                if name in existing:
                    conn.exec_driver_sql(f'ALTER TABLE "{table}" DROP COLUMN "{name}"')


def add_missing_columns(target_engine):
    """
    Досоздаёт колонки, появившиеся в моделях после создания базы.

    Зачем. База в Docker лежит в томе и переживает пересборку образа,
    а create_all создаёт только отсутствующие таблицы — новую колонку
    в существующую таблицу он не добавит. Без этого после обновления
    сервер падал бы на старой базе при первом же запросе.

    Ограничение сознательное: добавляются только колонки, допускающие NULL,
    либо имеющие значение по умолчанию на стороне базы — их можно дописать
    в SQLite одной командой, не перенося данные. Всё сложнее (переименование,
    смена типа, NOT NULL без умолчания) требует полноценных миграций через
    Alembic, и такая колонка здесь остановит запуск с понятным сообщением,
    а не испортит данные молча.

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
                default = column_default_sql(column)
                if not column.nullable and default is None:
                    raise RuntimeError(
                        f"Колонку {table.name}.{column.name} (NOT NULL) нельзя "
                        "добавить автоматически — нужна миграция"
                    )
                column_type = column.type.compile(dialect=target_engine.dialect)
                suffix = "" if column.nullable else f" NOT NULL DEFAULT {default}"
                connection.execute(
                    text(
                        f'ALTER TABLE "{table.name}" '
                        f'ADD COLUMN "{column.name}" {column_type}{suffix}'
                    )
                )
                added.append(f"{table.name}.{column.name}")

    return added


def column_default_sql(column):
    """
    Значение по умолчанию для ALTER TABLE или None, если его нет.

    Берётся только server_default — умолчание, записанное в саму базу.
    Питоновский default применяется при вставке через ORM и существующим
    строкам ничем не поможет.
    """
    if column.server_default is None:
        return None
    return column.server_default.arg.text
