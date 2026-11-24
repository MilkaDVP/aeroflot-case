"""
Подготовка стенда: очистка базы и случайные данные.

Только технический администратор: обе операции затрагивают рабочие данные,
а очистка — сразу всех аэропортов.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.dependencies import Context, require_roles, require_roles_in_airport
from constants import SHIFT_DAY
from database import get_dispatch_db
from models.user import ROLE_ADMIN
from services import polygon, stand

router = APIRouter(prefix="/api/admin/stand", tags=["Администрирование"])

CONFIRM = "ОЧИСТИТЬ"


class ClearRequest(BaseModel):
    confirm: str


class RandomRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=60)
    count: int = Field(default=60, ge=1, le=300)


@router.post("/clear", summary="Очистить базу")
def clear(
    payload: ClearRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(ROLE_ADMIN)),
):
    """Вызовы, переписка, сообщения и журнал стираются, люди и машины — по местам."""
    if payload.confirm.strip().upper() != CONFIRM:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Для подтверждения введите {CONFIRM}")
    from models.airport import Airport
    from seed import ensure_polygon_users

    result = stand.clear(db, context.shift or SHIFT_DAY)
    airport = db.get(Airport, polygon.ICAO)
    if airport is not None:
        ensure_polygon_users(db, airport)
    db.commit()
    return result


@router.post("/random", summary="Случайные данные")
def random_data(
    payload: RandomRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles_in_airport(ROLE_ADMIN)),
):
    """История, журнал, переписка и текущие вызовы в выбранном аэропорту."""
    context.shift = context.shift or SHIFT_DAY
    result = stand.generate(db, context, payload.days, payload.count)
    db.commit()
    return result
