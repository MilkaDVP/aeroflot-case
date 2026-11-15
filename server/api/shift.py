"""
Лента Наставника и итог смены.

Одна ручка отдаёт и последние замечания, и итог в цифрах: рабочее место
опрашивает её вместе с вызовами, и лента с итогом не должны расходиться.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth.dependencies import Context, require_airport, require_roles
from database import get_db
from models.call import Call
from models.user import DISPATCH_ROLES
from services import mentor

router = APIRouter(prefix="/api/shift", tags=["Наставник"])

FEED_LIMIT = 30


class ShiftEventResponse(BaseModel):
    """Одно замечание Наставника."""

    id: int
    call_id: int | None
    board_number: str | None
    kind: str
    verdict: str
    text: str
    actor_name: str | None
    created_at: str


class ShiftSummaryResponse(BaseModel):
    """Итог смены в цифрах."""

    grade: str
    verdicts: dict[str, int]
    arrivals: int
    arrivals_in_time: int
    in_time_percent: int | None
    assignments: int
    avg_minutes_to_assign: float | None
    overrides: int
    override_cost_min: float
    window_hours: int


class ShiftFeedResponse(BaseModel):
    """Лента замечаний и итог смены."""

    events: list[ShiftEventResponse]
    summary: ShiftSummaryResponse


def boards_of(db, events):
    """Бортовые номера вызовов, упомянутых в замечаниях, одним запросом."""
    call_ids = {event.call_id for event in events if event.call_id is not None}
    if not call_ids:
        return {}
    calls = db.query(Call).filter(Call.id.in_(call_ids)).all()
    return {call.id: mentor.board_of(call) for call in calls}


@router.get("/feed", response_model=ShiftFeedResponse, summary="Лента Наставника")
def shift_feed(
    limit: int = Query(default=FEED_LIMIT, ge=1, le=200),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
    airport: Context = Depends(require_airport),
    db: Session = Depends(get_db),
):
    """
    Замечания Наставника по аэропорту сессии и итог смены.

    Перед выдачей выносятся наблюдения по обстановке (вызов без
    исполнителя, исполнитель не успевает). Запись при чтении здесь
    осознанна: наблюдение должно появиться, когда обстановка изменилась,
    а не когда кто-то что-то нажал, и опрос ленты — те самые «глаза»
    Наставника. Каждое наблюдение выносится один раз.
    """
    if mentor.observe(db, context.airport_icao):
        db.commit()

    events = mentor.feed(db, context.airport_icao, limit)
    boards = boards_of(db, events)
    return ShiftFeedResponse(
        events=[mentor.event_dict(event, boards) for event in events],
        summary=mentor.summary(db, context.airport_icao),
    )
