"""
Точка входа сервера.

Запуск для разработки:
    uvicorn main:app --reload --port 8000
Автодокументация API: http://localhost:8000/docs
"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import (
    admin_airports,
    ai,
    airports,
    auth,
    calls,
    employees,
    events,
    me,
    reference,
    reports,
    shift,
    track,
    users,
    vehicles,
)
from database import SessionLocal, begin_dispatch_write, create_all
from graph_registry import load_from_database, preload_all
from services.shifts import expire_shifts, initialize_shift_clocks
from services.defects import ensure_builtin_defects
from services import journal
from services.journal_http import JournalMiddleware

# Такт контроля норматива для журнала: прогноз прибытия, риск, нарушение.
JOURNAL_TICK_S = 5

# Веб-диспетчер и PWA раздаются отдельными статическими сервисами, поэтому
# обращения к API идут с другого источника. Для конкурсного стенда список
# источников открыт; в эксплуатации сюда прописывается конкретный домен.
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")


async def shift_loop():
    while True:
        await asyncio.sleep(2)
        await asyncio.to_thread(update_shifts)


def update_shifts():
    db = SessionLocal()
    try:
        begin_dispatch_write(db)
        expire_shifts(db)
        db.commit()
    except Exception as error:  # такт не должен ронять сервер
        db.rollback()
        print(f"Не удалось обновить смены — {error}")
    finally:
        db.close()


async def journal_loop():
    """Раз в несколько секунд пишет в журнал прогнозы, риски и нарушения норматива."""
    while True:
        await asyncio.sleep(JOURNAL_TICK_S)
        await asyncio.to_thread(journal_tick)


def journal_tick():
    db = SessionLocal()
    try:
        begin_dispatch_write(db)
        journal.watch(db)
        db.commit()
    except Exception as error:  # такт не должен ронять сервер
        db.rollback()
        print(f"Журнал: такт пропущен — {error}")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Подготовка при старте: таблицы, справочники, тестовые данные, графы в память."""
    create_all()

    # Встроенные типы неисправностей досоздаются при каждом запуске, а не
    # только при наполнении пустой базы: в базе из Docker-тома, созданной
    # до появления открытого справочника, его нет вовсе.
    db = SessionLocal()
    try:
        ensure_builtin_defects(db)
    finally:
        db.close()

    # Наполнение базы при первом запуске. Жюри разворачивает проект одной
    # командой и сразу видит рабочую систему, а не пустые экраны.
    if os.environ.get("SEED_ON_START", "1") == "1":
        from seed import initialize_base

        initialize_base()

    loaded = preload_all()

    # Испытательный полигон досоздаётся и в уже наполненной базе.
    if os.environ.get("SEED_ON_START", "1") == "1":
        from seed import ensure_polygon

        with SessionLocal() as db:
            ensure_polygon(db)

    # Аэропорты, добавленные администратором, лежат в базе: их графы тоже
    # поднимаются в память, иначе после перезапуска они перестали бы
    # открываться, хотя запись о них есть.
    db = SessionLocal()
    try:
        initialize_shift_clocks(db)
        expire_shifts(db)
        custom = load_from_database(db)
        db.commit()
    finally:
        db.close()

    print(f"Загружены графы аэропортов: {', '.join(loaded) or 'нет'}")
    if custom:
        print(f"Аэропорты из базы: {', '.join(custom)}")

    ticker = asyncio.create_task(shift_loop())
    # Такт журнала: прогнозы прибытия, риски и нарушения норматива.
    watcher = None
    if os.environ.get("JOURNAL_LOOP", "1") == "1":
        watcher = asyncio.create_task(journal_loop())
    try:
        yield
    finally:
        tasks = [task for task in (ticker, watcher) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(
    title="Умная диспетчеризация ОТО воздушных судов",
    description=(
        "Автоматический подбор ближайшего свободного сотрудника с действующим "
        "допуском на тип ВС и вид работ, укладывающегося в регламент 15 минут."
    ),
    version="0.3.0",
    lifespan=lifespan,
)

# Журнал действий и ошибок приёма данных — внутри CORS, чтобы ответы
# об ошибках, которые он видит, были уже окончательными.
app.add_middleware(JournalMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Имя файла выгрузки журнала браузер читает из этого заголовка.
    expose_headers=["Content-Disposition"],
)

app.include_router(auth.router)
app.include_router(airports.router)
app.include_router(employees.router)
app.include_router(calls.router)
app.include_router(me.router)
app.include_router(reference.router)
app.include_router(shift.router)
app.include_router(reports.router)
app.include_router(vehicles.router)
app.include_router(admin_airports.router)
app.include_router(ai.router)
app.include_router(events.router)
app.include_router(users.router)
app.include_router(track.router)


@app.get("/api/health", tags=["Служебное"], summary="Проверка живости")
def health():
    """Используется docker compose и мониторингом развёртывания."""
    return {"status": "ok"}
