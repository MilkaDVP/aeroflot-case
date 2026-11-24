"""
Учения: включить, выключить, ускорить.

Видят все, кто работает на экране диспетчера, включает администратор
аэропорта и старше: в учениях сервер сам ведёт инженеров и подтверждает
прибытие, в настоящей смене это недопустимо.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from auth.dependencies import Context, require_airport, require_roles
from database import get_db
from models.user import DISPATCH_ROLES, ROLE_LOCAL_ADMIN
from services import drill

router = APIRouter(prefix="/api/drill", tags=["Учения"])


class DrillRequest(BaseModel):
    enabled: bool
    speed: int | None = Field(default=None, description="Во сколько раз быстрее")

    @field_validator("speed")
    @classmethod
    def known_speed(cls, value):
        if value is not None and value not in drill.SPEEDS:
            raise ValueError(f"Ускорение — одно из {drill.SPEEDS}")
        return value


@router.get("", summary="Идут ли учения")
def drill_state(
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
    airport: Context = Depends(require_airport),
    db: Session = Depends(get_db),
):
    state = drill.state(context.airport_icao)
    return {**state, **drill.stats(db, context.airport_icao)} if state["enabled"] else state


@router.post("", summary="Начать или завершить учения")
def set_drill(
    payload: DrillRequest,
    context: Context = Depends(require_roles(ROLE_LOCAL_ADMIN)),
    airport: Context = Depends(require_airport),
):
    return drill.set_state(context.airport_icao, payload.enabled, payload.speed)
