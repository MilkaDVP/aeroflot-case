"""
Справочники.

Отдаются сервером, а не зашиваются в клиент: справочник неисправностей
и соответствие «дефект → категория» должны быть в одном месте, иначе
веб-диспетчер и PWA однажды разойдутся в трактовке.

Справочник неисправностей открытый: его можно искать словами экипажа
и пополнять прямо из формы вызова (см. services/defects.py).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from auth.dependencies import Context, get_context, require_roles
from constants import AIRCRAFT_GROUP_BY_TYPE, ATA_CHAPTERS
from database import get_db
from models.user import DISPATCH_ROLES
from schemas.call import AtaChapterResponse, DefectTypeCreateRequest, DefectTypeResponse
from services.defects import (
    SEARCH_LIMIT,
    DuplicateDefect,
    InvalidDefect,
    chapter_title,
    create,
    search,
)

router = APIRouter(prefix="/api", tags=["Справочники"])

ERROR_DUPLICATE = "Такой тип уже есть в справочнике: «{name}» (ATA {ata})"


def defect_response(defect):
    """Запись справочника в схему ответа."""
    return DefectTypeResponse(
        code=defect.code,
        name=defect.name,
        ata=defect.ata,
        ata_title=chapter_title(defect.ata),
        category=defect.category,
        keywords=defect.keywords or "",
        is_builtin=defect.is_builtin,
    )


@router.get(
    "/defect-types", response_model=list[DefectTypeResponse], summary="Неисправности"
)
def list_defect_types(
    q: str = Query(default="", description="Слова экипажа или номер главы ATA"),
    limit: int = Query(default=SEARCH_LIMIT, ge=1, le=200),
    context: Context = Depends(get_context),
    db: Session = Depends(get_db),
):
    """
    Поиск по справочнику неисправностей.

    Без запроса — весь справочник по порядку глав. С запросом — типы,
    в названии, синонимах или главе которых нашлось каждое слово,
    лучшие совпадения первыми.
    """
    return [defect_response(defect) for defect in search(db, q, limit)]


@router.post(
    "/defect-types",
    response_model=DefectTypeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Завести новый тип неисправности",
)
def create_defect_type(
    payload: DefectTypeCreateRequest,
    context: Context = Depends(require_roles(*DISPATCH_ROLES)),
    db: Session = Depends(get_db),
):
    """
    Новый тип неисправности — когда экипаж сообщил о том, чего в справочнике нет.

    Заводит диспетчер, а не только администратор: нестандартный дефект
    случается ночью, когда администратора нет, а инженер нужен через
    пятнадцать минут. Тип сразу участвует в подборе по своей категории.
    """
    try:
        defect = create(
            db,
            payload.name,
            payload.ata,
            payload.category,
            payload.keywords,
            context.user.id,
        )
    except DuplicateDefect as duplicate:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            ERROR_DUPLICATE.format(name=duplicate.existing.name, ata=duplicate.existing.ata),
        )
    except InvalidDefect as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))

    db.commit()
    return defect_response(defect)


@router.get("/ata-chapters", response_model=list[AtaChapterResponse], summary="Главы ATA")
def list_ata_chapters(context: Context = Depends(get_context)):
    """Главы ATA для формы нового типа — с категорией допуска по умолчанию."""
    return [
        AtaChapterResponse(ata=ata, title=title, default_category=category)
        for ata, (title, category) in ATA_CHAPTERS.items()
    ]


@router.get("/aircraft-types", response_model=dict[str, int], summary="Типы ВС")
def list_aircraft_types(context: Context = Depends(get_context)):
    """Типы ВС и их подкатегория допуска (1..4)."""
    return AIRCRAFT_GROUP_BY_TYPE
