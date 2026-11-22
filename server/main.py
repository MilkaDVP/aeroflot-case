"""
Точка входа сервера.

Запуск для разработки:
    uvicorn main:app --reload --port 8000
Автодокументация API: http://localhost:8000/docs
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import (
    admin_airports,
    airports,
    auth,
    calls,
    drill,
    employees,
    me,
    reference,
    reports,
    shift,
    vehicles,
)
from database import SessionLocal, begin_dispatch_write, create_all
from graph_registry import load_from_database, preload_all
from constants import DRILL_MAX_STEP_S, DRILL_TICK_S
from services import drill as drill_service
from services.defects import ensure_builtin_defects

# Веб-диспетчер и PWA раздаются отдельными статическими сервисами, поэтому
# обращения к API идут с другого источника. Для конкурсного стенда список
# источников открыт; в эксплуатации сюда прописывается конкретный домен.
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")


async def drill_loop():
    """Раз в несколько секунд продвигает сотрудников в аэропортах с учениями."""
    last = time.monotonic()
    while True:
        await asyncio.sleep(DRILL_TICK_S)
        now = time.monotonic()
        dt_s = min(now - last, DRILL_MAX_STEP_S)
        last = now
        # Работа с базой синхронная — в отдельном потоке, чтобы такт
        # не задерживал обработку запросов.
        await asyncio.to_thread(drill_tick, dt_s)


def drill_tick(dt_s):
    """Один такт учений по всем аэропортам, где они включены."""
    db = SessionLocal()
    try:
        begin_dispatch_write(db)
        for icao in drill_service.enabled_airports(db):
            drill_service.step(db, icao, dt_s)
        db.commit()
    except Exception as error:  # такт не должен ронять сервер
        db.rollback()
        print(f"Учения: такт пропущен — {error}")
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
        from seed import seed_if_empty

        seed_if_empty()

    loaded = preload_all()

    # Аэропорты, добавленные администратором, лежат в базе: их графы тоже
    # поднимаются в память, иначе после перезапуска они перестали бы
    # открываться, хотя запись о них есть.
    db = SessionLocal()
    try:
        custom = load_from_database(db)
        for icao in drill_service.enabled_airports(db):
            drill_service.ensure_training_team(db, icao)
        db.commit()
    finally:
        db.close()

    print(f"Загружены графы аэропортов: {', '.join(loaded) or 'нет'}")
    if custom:
        print(f"Аэропорты из базы: {', '.join(custom)}")

    # Такты режима учений. Задача живёт всё время работы сервера, но
    # что-то делает только там, где администратор учения включил.
    ticker = None
    if os.environ.get("DRILL_LOOP", "1") == "1":
        ticker = asyncio.create_task(drill_loop())
    yield
    if ticker is not None:
        ticker.cancel()


app = FastAPI(
    title="Умная диспетчеризация ОТО воздушных судов",
    description=(
        "Автоматический подбор ближайшего свободного сотрудника с действующим "
        "допуском на тип ВС и вид работ, укладывающегося в регламент 15 минут."
    ),
    version="0.3.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(airports.router)
app.include_router(employees.router)
app.include_router(calls.router)
app.include_router(me.router)
app.include_router(reference.router)
app.include_router(shift.router)
app.include_router(drill.router)
app.include_router(reports.router)
app.include_router(vehicles.router)
app.include_router(admin_airports.router)


@app.get("/api/health", tags=["Служебное"], summary="Проверка живости")
def health():
    """Используется docker compose и мониторингом развёртывания."""
    return {"status": "ok"}
