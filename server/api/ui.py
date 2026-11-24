"""
Оформление системы: прежний стиль или стиль Аэрофлота.

Читать можно без входа — стиль нужен уже на экране входа. Меняет его
технический администратор, сразу для всех экранов.
"""

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth.dependencies import Context, require_roles
from database import get_db
from models.setting import Setting
from models.user import ROLE_ADMIN

router = APIRouter(prefix="/api/ui", tags=["Оформление"])

KEY = "ui_style"
DEFAULT = "aero"


class UiStyle(BaseModel):
    style: Literal["legacy", "aero"]


@router.get("", response_model=UiStyle, summary="Стиль оформления")
def ui_style(db: Session = Depends(get_db)):
    record = db.get(Setting, KEY)
    return UiStyle(style=record.value if record else DEFAULT)


@router.put("", response_model=UiStyle, summary="Сменить стиль для всех")
def set_ui_style(
    payload: UiStyle,
    context: Context = Depends(require_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    db.merge(Setting(key=KEY, value=payload.style))
    db.commit()
    return payload
