"""
POST /api/v1/track — приём местоположения агентов (Приложение к положению,
«Контракт интерфейса приёма данных о местоположении агентов системы»).

Тело разбирается вручную, а не моделью pydantic: контракт требует 400
(а не 422 FastAPI) при отсутствующих или нечисловых lat/lon и отдельный
результат по каждой точке пакета.

Авторизация: если задана переменная окружения TRACK_API_KEY, клиент
передаёт её в заголовке X-Track-Key. Без переменной ручка открыта —
имитатор организаторов работает без учётной записи.
"""

import json
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from clock import utc_now
from database import get_db, get_dispatch_db
from services.tracking import BadPoint, format_time, ingest, known_objects, parse_point

router = APIRouter(prefix="/api/v1", tags=["Приём геопозиции"])

MAX_BATCH = 1000


def check_key(x_track_key: str | None = Header(default=None)):
    expected = os.environ.get("TRACK_API_KEY")
    if expected and x_track_key != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный X-Track-Key")


def _result(point, applied):
    return {"accepted": True, "applied": applied, "obj": point["obj"], "t": format_time(point["t"])}


@router.post(
    "/track",
    summary="Принять точку или пакет точек",
    dependencies=[Depends(check_key)],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"examples": {
                "point": {"summary": "Одна точка", "value": {
                    "obj": "e-01", "lat": 55.8312, "lon": 37.6359,
                    "t": "2026-12-03T09:14:20.123Z", "acc": 5, "spd": 0}},
                "batch": {"summary": "Пакет точек", "value": {"points": [
                    {"obj": "e-01", "lat": 55.8312, "lon": 37.6359, "t": "2026-12-03T09:14:20.123Z"},
                    {"obj": "v-01", "lat": 55.8314, "lon": 37.6361, "t": "2026-12-03T09:14:21.123Z"}]}},
            }}},
        }
    },
)
async def track(request: Request, db: Session = Depends(get_dispatch_db)):
    """
    Одна точка → объект результата; пакет {"points": [...]} → массив
    результатов в порядке точек.

    - lat/lon отсутствуют или не числа → 400 (для одной точки и для
      пакета, где нет ни одной верной точки); в пакете с верными точками
      неверная получает accepted: false;
    - объект неизвестен или точка отброшена как выброс → 200, applied: false;
    - всё верно → 200, accepted: true, applied: true.
    """
    try:
        body = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Тело запроса — не JSON")

    now = utc_now()
    if isinstance(body, dict) and "points" in body:
        points = body["points"]
        if not isinstance(points, list) or not points:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "points должен быть непустым массивом")
        if len(points) > MAX_BATCH:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Не больше {MAX_BATCH} точек в пакете")
        parsed = []
        for raw in points:
            try:
                parsed.append(parse_point(raw, now))
            except BadPoint as error:
                parsed.append(error)
        if all(isinstance(item, BadPoint) for item in parsed):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(parsed[0]))

        # Точки одного объекта применяются в хронологическом порядке, иначе
        # перепутанный пакет отбросил бы более ранние как устаревшие.
        # Ответ — в исходном порядке.
        order = sorted(
            (i for i, item in enumerate(parsed) if not isinstance(item, BadPoint)),
            key=lambda i: parsed[i]["t"],
        )
        applied = {i: ingest(db, parsed[i], now) for i in order}
        db.commit()
        results = []
        for i, item in enumerate(parsed):
            if isinstance(item, BadPoint):
                raw = points[i] if isinstance(points[i], dict) else {}
                obj = raw.get("obj") if isinstance(raw.get("obj"), str) else None
                results.append({"accepted": False, "applied": False, "obj": obj,
                                "t": None, "error": str(item)})
            else:
                results.append(_result(item, applied[i]))
        return results

    try:
        point = parse_point(body, now)
    except BadPoint as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error))
    applied = ingest(db, point, now)
    db.commit()
    return _result(point, applied)


@router.get(
    "/track/objects",
    summary="Идентификаторы объектов для /track",
    dependencies=[Depends(check_key)],
)
def track_objects(db: Session = Depends(get_db)):
    """Сотрудники (e-NN), машины (v-NN) и борта (бортовой номер)."""
    return known_objects(db)
