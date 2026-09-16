"""
Точка входа сервера.

Запуск для разработки:
    uvicorn main:app --reload --port 8000
Автодокументация API: http://localhost:8000/docs
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import (
    admin_airports,
    airports,
    auth,
    calls,
    employees,
    me,
    reference,
    vehicles,
)
from database import SessionLocal, create_all
from graph_registry import load_from_database, preload_all

# Веб-диспетчер и PWA раздаются отдельными статическими сервисами, поэтому
# обращения к API идут с другого источника. Для конкурсного стенда список
# источников открыт; в эксплуатации сюда прописывается конкретный домен.
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Подготовка при старте: таблицы, тестовые данные, графы в память."""
    create_all()

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
    finally:
        db.close()

    print(f"Загружены графы аэропортов: {', '.join(loaded) or 'нет'}")
    if custom:
        print(f"Аэропорты из базы: {', '.join(custom)}")
    yield


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
app.include_router(vehicles.router)
app.include_router(admin_airports.router)


@app.get("/api/health", tags=["Служебное"], summary="Проверка живости")
def health():
    """Используется docker compose и мониторингом развёртывания."""
    return {"status": "ok"}
