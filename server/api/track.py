"""Единая ручка приёма геопозиций по контракту финальных испытаний."""

import hmac
import os

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from database import get_dispatch_db
from schemas.track import TrackBatch, TrackPoint, TrackResponse
from services.tracking import TrackPayloadError, apply_track_payload

router = APIRouter(prefix="/api/v1/track", tags=["Геопозиции агентов"])


@router.post(
    "",
    response_model=TrackResponse | list[TrackResponse],
    summary="Принять одну GPS-точку или пакет точек",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "oneOf": [
                            TrackPoint.model_json_schema(),
                            TrackBatch.model_json_schema(),
                        ]
                    },
                    "examples": {
                        "single": {
                            "summary": "Одна точка",
                            "value": {
                                "obj": "e-01",
                                "lat": 55.8312,
                                "lon": 37.6359,
                                "t": "2026-12-03T09:14:20.123Z",
                                "acc": 5,
                                "spd": 0,
                            },
                        },
                        "batch": {
                            "summary": "Пакет точек",
                            "value": {
                                "points": [
                                    {
                                        "obj": "e-01",
                                        "lat": 55.8312,
                                        "lon": 37.6359,
                                        "t": "2026-12-03T09:14:20.123Z",
                                    },
                                    {
                                        "obj": "v-01",
                                        "lat": 55.8314,
                                        "lon": 37.6361,
                                        "t": "2026-12-03T09:14:21.123Z",
                                    },
                                ]
                            },
                        },
                    },
                }
            },
        }
    },
)
def receive_track(
    payload: dict = Body(...),
    x_api_key: str | None = Header(default=None),
    db: Session = Depends(get_dispatch_db),
):
    """
    Принимает точки инженеров (`e-01`), машин (`v-01`) и ВС (`RA-73142`).

    По умолчанию отдельный ключ не нужен: так же работает имитатор. При
    открытом развёртывании можно задать TRACK_API_KEY и передавать его в X-API-Key.
    """
    expected_key = os.environ.get("TRACK_API_KEY", "").strip()
    if expected_key and (
        x_api_key is None or not hmac.compare_digest(x_api_key, expected_key)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный X-API-Key")

    try:
        result = apply_track_payload(db, payload)
        db.commit()
        return result
    except TrackPayloadError as error:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
