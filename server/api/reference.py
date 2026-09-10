"""
Справочники.

Отдаются сервером, а не зашиваются в клиент: справочник неисправностей
и соответствие «дефект → категория» должны быть в одном месте, иначе
веб-диспетчер и PWA однажды разойдутся в трактовке.
"""

from fastapi import APIRouter, Depends

from auth.dependencies import Context, get_context
from constants import AIRCRAFT_GROUP_BY_TYPE, DEFECT_TYPES
from schemas.call import DefectTypeResponse

router = APIRouter(prefix="/api", tags=["Справочники"])


@router.get(
    "/defect-types", response_model=list[DefectTypeResponse], summary="Неисправности"
)
def list_defect_types(context: Context = Depends(get_context)):
    """Коды дефектов, главы ATA и требуемые категории допуска."""
    return [
        DefectTypeResponse(
            code=code,
            name=defect["name"],
            ata=defect["ata"],
            category=defect["category"],
        )
        for code, defect in DEFECT_TYPES.items()
    ]


@router.get("/aircraft-types", response_model=dict[str, int], summary="Типы ВС")
def list_aircraft_types(context: Context = Depends(get_context)):
    """Типы ВС и их подкатегория допуска (1..4)."""
    return AIRCRAFT_GROUP_BY_TYPE
