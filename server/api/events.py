"""
Журнал событий: просмотр, выгрузка в CSV и Excel, проверка целостности.

Кто что видит:
- технический администратор — весь журнал, по всем аэропортам;
- администратор аэропорта и начальник смены — события своих аэропортов
  (заявки, сотрудники, машины, учётные записи там) и свои собственные
  действия.
Диспетчер и инженер журнала не видят.

Записи только читаются: правки и удаления в API нет (см. models/event_log.py).
"""

import csv
import io
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from auth.dependencies import Context, require_roles
from clock import utc_now
from database import get_db
from models.airport import Airport
from models.event_log import EventLog
from models.user import ROLE_ADMIN, ROLE_SHIFT_SUPERVISOR
from services import journal
from services import xlsx
from services.shift_clock import schedule_of, zone

router = APIRouter(prefix="/api/events", tags=["Журнал событий"])

MAX_PAGE = 500
EXPORT_LIMIT = 200_000

HEADERS = [
    "№", "Время (UTC)", "Местное время", "Аэропорт", "Раздел", "Тип", "Событие",
    "Описание", "Заявка", "Борт", "Инициатор", "Роль", "Исполнитель",
    "Длительность, мс", "Параметры",
]
WIDTHS = [7, 24, 24, 9, 16, 18, 30, 60, 8, 10, 22, 22, 22, 12, 80]


class EventItem(BaseModel):
    id: int
    ts: str
    local_time: str
    airport_icao: str | None
    category: str
    category_title: str
    type: str
    type_title: str
    text: str
    call_id: int | None
    board_number: str | None
    actor_name: str | None
    actor_role: str | None
    employee_name: str | None
    duration_ms: float | None
    params: dict
    hash: str | None


class EventPage(BaseModel):
    items: list[EventItem]
    total: int
    scope: str


class IntegrityResponse(BaseModel):
    ok: bool
    count: int
    broken_id: int | None
    checked_at: str


class EventTypeItem(BaseModel):
    type: str
    title: str
    category: str
    category_title: str


def utc_naive(value):
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def scoped(db, context):
    """Записи, которые этому пользователю разрешено видеть."""
    query = db.query(EventLog)
    user = context.user
    if user.role == ROLE_ADMIN:
        return query
    icaos = user.airport_icaos or ([context.airport_icao] if context.airport_icao else [])
    return query.filter(or_(EventLog.airport_icao.in_(icaos), EventLog.actor_user_id == user.id))


def filtered(db, context, date_from, date_to, category, type_, call_id, airport, q):
    query = scoped(db, context)
    if airport:
        query = query.filter(EventLog.airport_icao == airport.upper())
    if date_from:
        query = query.filter(EventLog.ts >= utc_naive(date_from))
    if date_to:
        query = query.filter(EventLog.ts <= utc_naive(date_to))
    if category:
        query = query.filter(EventLog.category.in_(category.split(",")))
    if type_:
        query = query.filter(EventLog.type.in_(type_.split(",")))
    if call_id:
        query = query.filter(EventLog.call_id == call_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(
            EventLog.text.ilike(like), EventLog.board_number.ilike(like),
            EventLog.actor_name.ilike(like), EventLog.employee_name.ilike(like),
        ))
    return query


def zones(db):
    """Часовой пояс каждого аэропорта — для местного времени."""
    return {airport.icao: zone(schedule_of(airport)[0]) for airport in db.query(Airport).all()}


def stamp(value):
    """Время с точностью до миллисекунды."""
    return value.strftime("%Y-%m-%d %H:%M:%S.") + f"{value.microsecond // 1000:03d}"


def local_of(entry, tz_map):
    tz = tz_map.get(entry.airport_icao)
    if tz is None:
        return None
    return entry.ts.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)


def to_item(entry, tz_map):
    category, title = journal.EVENT_TYPES.get(entry.type, (entry.category, entry.type))
    local = local_of(entry, tz_map)
    return EventItem(
        id=entry.id,
        ts=entry.ts.replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds"),
        local_time=stamp(local) if local else "",
        airport_icao=entry.airport_icao,
        category=entry.category,
        category_title=journal.CATEGORY_TITLES.get(entry.category, entry.category),
        type=entry.type,
        type_title=title,
        text=entry.text,
        call_id=entry.call_id,
        board_number=entry.board_number,
        actor_name=entry.actor_name,
        actor_role=journal.ROLE_TITLES.get(entry.actor_role, entry.actor_role),
        employee_name=entry.employee_name,
        duration_ms=entry.duration_ms,
        params=entry.params or {},
        hash=entry.hash,
    )


def scope_title(context):
    if context.user.role == ROLE_ADMIN:
        return "весь журнал, все аэропорты"
    return "аэропорт " + (", ".join(context.user.airport_icaos) or context.airport_icao or "—")


@router.get("", response_model=EventPage, summary="Журнал событий")
def list_events(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    category: str | None = None,
    type: str | None = None,
    call_id: int | None = None,
    airport: str | None = None,
    q: str | None = None,
    limit: int = Query(200, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    context: Context = Depends(require_roles(ROLE_SHIFT_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """Свежие события первыми; фильтры — период, раздел, тип, заявка, поиск."""
    query = filtered(db, context, date_from, date_to, category, type, call_id, airport, q)
    total = query.count()
    rows = query.order_by(EventLog.ts.desc(), EventLog.id.desc()).offset(offset).limit(limit).all()
    tz_map = zones(db)
    return EventPage(items=[to_item(row, tz_map) for row in rows], total=total, scope=scope_title(context))


@router.get("/types", response_model=list[EventTypeItem], summary="Типы событий")
def event_types(context: Context = Depends(require_roles(ROLE_SHIFT_SUPERVISOR))):
    return [
        EventTypeItem(type=key, title=title, category=category,
                      category_title=journal.CATEGORY_TITLES[category])
        for key, (category, title) in journal.EVENT_TYPES.items()
    ]


@router.get("/integrity", response_model=IntegrityResponse, summary="Проверка целостности")
def integrity(
    context: Context = Depends(require_roles(ROLE_SHIFT_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """
    Пересчитывает цепочку хэшей всего журнала. Цепочка общая для всех
    аэропортов, поэтому проверяется целиком; содержимое чужих записей
    при этом не раскрывается.
    """
    count, broken = journal.verify(db)
    return IntegrityResponse(
        ok=broken is None, count=count, broken_id=broken,
        checked_at=utc_now().replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds"),
    )


@router.get("/export", summary="Выгрузить журнал (CSV или Excel)")
def export_events(
    format: str = "csv",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    category: str | None = None,
    type: str | None = None,
    call_id: int | None = None,
    airport: str | None = None,
    q: str | None = None,
    context: Context = Depends(require_roles(ROLE_SHIFT_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """
    Хронологический лог: одна строка — одно событие, время до миллисекунды,
    тип, участники, параметры; у подбора кандидатов — затраченное время.
    """
    if format not in ("csv", "xlsx"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Формат: csv или xlsx")
    query = filtered(db, context, date_from, date_to, category, type, call_id, airport, q)
    entries = query.order_by(EventLog.ts, EventLog.id).limit(EXPORT_LIMIT).all()
    tz_map = zones(db)

    rows = []
    for entry in entries:
        category_, title = journal.EVENT_TYPES.get(entry.type, (entry.category, entry.type))
        rows.append([
            entry.id,
            entry.ts,
            local_of(entry, tz_map),
            entry.airport_icao,
            journal.CATEGORY_TITLES.get(entry.category, entry.category),
            entry.type,
            title,
            entry.text,
            entry.call_id,
            entry.board_number,
            entry.actor_name or "система",
            journal.ROLE_TITLES.get(entry.actor_role, entry.actor_role) or "",
            entry.employee_name,
            entry.duration_ms,
            json.dumps(entry.params or {}, ensure_ascii=False, sort_keys=True),
        ])

    suffix = (airport or ("ALL" if context.user.role == ROLE_ADMIN else context.airport_icao or "")).upper()
    name = f"journal_{suffix}_{utc_now():%Y%m%d-%H%M%S}"
    if format == "xlsx":
        content = xlsx.build(HEADERS, rows, WIDTHS)
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'},
        )

    buffer = io.StringIO()
    # Точка с запятой и BOM — чтобы русский Excel открыл файл двойным
    # щелчком с правильными столбцами и кириллицей.
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(HEADERS)
    for row in rows:
        writer.writerow([
            stamp(value) if isinstance(value, datetime)
            else str(value).replace(".", ",") if isinstance(value, float)
            else ("" if value is None else value)
            for value in row
        ])
    return Response(
        "﻿" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )
