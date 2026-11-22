"""
Входящие сообщения о неисправностях и их разбор диспетчером.

Две стороны одной цепочки. Внешняя — приём сообщения от экипажа или
системы авиакомпании по интеграционному ключу: это точка, через которую
решение встраивается в существующую инфраструктуру, а не живёт отдельным
приложением. Внутренняя — ящик диспетчера: сообщение разбирается человеком
и превращается в вызов либо отклоняется с причиной. Пилот не ставит задачу
инженеру напрямую — между ними всегда диспетчерская.
"""

import hmac
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.calls import new_call
from api.calls import to_response as call_to_response
from auth.dependencies import Context, check_same_airport, require_airport, require_roles
from clock import to_iso_utc, utc_now
from constants import (
    DEFAULT_INTAKE_API_KEYS,
    INTAKE_CLOCK_SKEW_S,
    INTAKE_MAX_AGE_H,
    REGULATION_ARRIVAL_LIMIT_MIN,
)
from database import get_db, get_dispatch_db
from models.aircraft import Aircraft
from models.airport import Airport
from models.report import REPORT_ACCEPTED, REPORT_NEW, REPORT_REJECTED, Report
from models.user import DISPATCH_ROLES
from schemas.call import CallResponse
from services.defects import find as find_defect

router = APIRouter(prefix="/api", tags=["Сообщения о неисправностях"])

ERROR_BAD_KEY = "Неверный интеграционный ключ"
ERROR_NO_AIRPORT = "Аэропорт {icao} не найден"
ERROR_BAD_TIME = "Время сообщения вне допустимых пределов: не из будущего и не старше суток"
ERROR_NOT_FOUND = "Сообщение не найдено"
ERROR_ALREADY_PROCESSED = "Сообщение уже разобрано"
ERROR_NO_AIRCRAFT = "Борт не найден: выберите воздушное судно, к которому относится сообщение"
ERROR_NO_DEFECT = "Тип неисправности {code} не найден в справочнике"


class ReportCreateRequest(BaseModel):
    """Сообщение о неисправности от экипажа или внешней системы."""

    airport_icao: str
    board_number: str = Field(min_length=2, max_length=20)
    description: str = Field(min_length=3, max_length=500)
    reporter: str | None = Field(default=None, max_length=80)
    # Когда неисправность замечена, с часовым поясом. Не указано — сейчас.
    reported_at: datetime | None = None


class ReportResponse(BaseModel):
    """Сообщение в ящике диспетчера."""

    id: int
    airport_icao: str
    board_number: str
    aircraft_id: int | None
    aircraft_type: str | None
    description: str
    reporter: str | None
    source: str
    reported_at: str
    received_at: str
    # Контрольный срок прибытия уже идёт — от сообщения экипажа.
    deadline_at: str
    status: str
    call_id: int | None
    resolution_note: str | None


class ReportAcceptRequest(BaseModel):
    """Разбор сообщения: какой это борт и какая неисправность."""

    defect_code: str
    aircraft_id: int | None = None
    urgent: bool = False


class ReportRejectRequest(BaseModel):
    """Отклонение сообщения. Причина обязательна: борт остаётся без вызова."""

    reason: str = Field(min_length=3, max_length=300)


class IntakeAircraft(BaseModel):
    """Борт для формы экипажа: только то, что нужно, чтобы выбрать свой."""

    board_number: str
    aircraft_type: str


def intake_keys():
    """Интеграционные ключи: ключ -> имя источника."""
    raw = os.environ.get("INTAKE_API_KEYS", DEFAULT_INTAKE_API_KEYS)
    keys = {}
    for pair in raw.split(","):
        name, _, key = pair.strip().partition(":")
        if name and key:
            keys[key] = name
    return keys


def intake_source(x_api_key: str | None = Header(default=None)):
    """
    Источник сообщения по интеграционному ключу.

    Сравнение через compare_digest: обычное сравнение строк завершается
    на первом несовпавшем символе, и по времени ответа ключ можно подобрать.
    """
    # Сравниваются байты: compare_digest на строках с не-ASCII символами
    # бросает исключение, и чужой ключ с кириллицей дал бы ошибку 500.
    given = (x_api_key or "").encode("utf-8")
    for key, name in intake_keys().items():
        if given and hmac.compare_digest(given, key.encode("utf-8")):
            return name
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, ERROR_BAD_KEY)


def to_response(report):
    """Сообщение в схему ответа."""
    deadline = report.reported_at + timedelta(minutes=REGULATION_ARRIVAL_LIMIT_MIN)
    return ReportResponse(
        id=report.id,
        airport_icao=report.airport_icao,
        board_number=report.board_number,
        aircraft_id=report.aircraft_id,
        aircraft_type=report.aircraft.aircraft_type if report.aircraft else None,
        description=report.description,
        reporter=report.reporter,
        source=report.source,
        reported_at=to_iso_utc(report.reported_at),
        received_at=to_iso_utc(report.received_at),
        deadline_at=to_iso_utc(deadline),
        status=report.status,
        call_id=report.call_id,
        resolution_note=report.resolution_note,
    )


def normalize_board(text):
    """Бортовой номер без пробелов и в верхнем регистре — как его сравнивать."""
    return "".join(text.split()).upper()


def reported_moment(value, now):
    """
    Время сообщения в UTC без пояса — как хранится всё время в базе.

    Время без пояса считается UTC. Сообщение «из будущего» или суточной
    давности отклоняется: от этого момента идёт отсчёт регламента,
    и ошибка в нём исказила бы контроль срока.
    """
    if value is None:
        return now
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    too_new = value > now + timedelta(seconds=INTAKE_CLOCK_SKEW_S)
    too_old = value < now - timedelta(hours=INTAKE_MAX_AGE_H)
    if too_new or too_old:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_BAD_TIME)
    return min(value, now)


# --- Внешняя сторона: приём сообщений ---------------------------------------


@router.post(
    "/intake/reports",
    response_model=ReportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Сообщить о неисправности (экипаж, внешние системы)",
)
def create_report(
    payload: ReportCreateRequest,
    source: str = Depends(intake_source),
    db: Session = Depends(get_db),
):
    """
    Приём сообщения по интеграционному ключу (заголовок X-API-Key).

    Сообщение не назначает исполнителя и не создаёт вызов: оно попадает
    в ящик диспетчерской. Неизвестный борт — не отказ: сообщение
    принимается, а борт уточнит диспетчер.
    """
    icao = payload.airport_icao.upper()
    if db.get(Airport, icao) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_NO_AIRPORT.format(icao=icao))

    now = utc_now()
    board = normalize_board(payload.board_number)
    aircraft = (
        db.query(Aircraft)
        .filter(Aircraft.airport_icao == icao, Aircraft.board_number == board)
        .first()
    )
    report = Report(
        airport_icao=icao,
        board_number=board,
        aircraft=aircraft,
        description=" ".join(payload.description.split()),
        reporter=(payload.reporter or "").strip() or None,
        source=source,
        reported_at=reported_moment(payload.reported_at, now),
        received_at=now,
    )
    db.add(report)
    db.commit()
    return to_response(report)


@router.get(
    "/intake/aircraft",
    response_model=list[IntakeAircraft],
    summary="Борта аэропорта для формы экипажа",
)
def intake_aircraft(
    airport: str = Query(description="Код ИКАО"),
    source: str = Depends(intake_source),
    db: Session = Depends(get_db),
):
    """Список бортов, чтобы экипаж выбрал свой, а не набирал номер вручную."""
    aircraft = (
        db.query(Aircraft)
        .filter(Aircraft.airport_icao == airport.upper())
        .order_by(Aircraft.board_number)
        .all()
    )
    return [
        IntakeAircraft(board_number=item.board_number, aircraft_type=item.aircraft_type)
        for item in aircraft
    ]


# --- Внутренняя сторона: ящик диспетчера ------------------------------------


def load_report(db, context, report_id):
    """Неразобранное сообщение своего аэропорта."""
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, ERROR_NOT_FOUND)
    check_same_airport(context, report.airport_icao)
    if report.status != REPORT_NEW:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_ALREADY_PROCESSED)
    return report


@router.get("/reports", response_model=list[ReportResponse], summary="Входящие сообщения")
def list_reports(
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
    airport: Context = Depends(require_airport),
    db: Session = Depends(get_db),
):
    """Неразобранные сообщения аэропорта, самые давние первыми — у них меньше времени."""
    reports = (
        db.query(Report)
        .filter(Report.airport_icao == context.airport_icao, Report.status == REPORT_NEW)
        .order_by(Report.reported_at, Report.id)
        .all()
    )
    return [to_response(report) for report in reports]


@router.post(
    "/reports/{report_id}/accept", response_model=CallResponse, summary="Принять: создать вызов"
)
def accept_report(
    report_id: int,
    payload: ReportAcceptRequest,
    db: Session = Depends(get_dispatch_db),
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
    airport: Context = Depends(require_airport),
):
    """
    Диспетчер разобрал сообщение: определил борт и тип неисправности.

    Вызов наследует время сообщения экипажа — регламент отсчитывается
    от него, и минуты, пока заявка ждала разбора, уже потрачены.
    """
    report = load_report(db, context, report_id)

    aircraft = db.get(Aircraft, payload.aircraft_id) if payload.aircraft_id else report.aircraft
    if aircraft is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_NO_AIRCRAFT)
    check_same_airport(context, aircraft.airport_icao)

    defect = find_defect(db, payload.defect_code)
    if defect is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_NO_DEFECT.format(code=payload.defect_code)
        )

    call = new_call(db, aircraft, defect, report.description, report.reported_at, report.source)
    call.urgent = payload.urgent
    report.status = REPORT_ACCEPTED
    report.call_id = call.id
    report.aircraft = aircraft
    report.processed_by = context.user.full_name
    report.processed_at = utc_now()
    db.commit()
    return call_to_response(call, db)


@router.post(
    "/reports/{report_id}/reject", response_model=ReportResponse, summary="Отклонить сообщение"
)
def reject_report(
    report_id: int,
    payload: ReportRejectRequest,
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
    airport: Context = Depends(require_airport),
    db: Session = Depends(get_db),
):
    """Отклонение с причиной: повтор, ошибочное сообщение, не относится к ОТО."""
    report = load_report(db, context, report_id)
    report.status = REPORT_REJECTED
    report.resolution_note = payload.reason.strip()
    report.processed_by = context.user.full_name
    report.processed_at = utc_now()
    db.commit()
    return to_response(report)
