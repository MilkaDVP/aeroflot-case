"""Предупреждения о приближении ВС: инженеру — свои, диспетчеру — по аэропорту."""

from fastapi import APIRouter, Depends

from auth.dependencies import Context, get_context, require_roles_in_airport
from models.user import DISPATCH_ROLES
from services import proximity

router = APIRouter(prefix="/api", tags=["Приближение ВС"])


@router.get("/me/proximity", summary="Приближение ВС ко мне")
def own_warnings(context: Context = Depends(get_context)):
    if not context.user.employee_id:
        return []
    return proximity.warnings(employee_id=context.user.employee_id)


@router.get("/proximity", summary="Приближения ВС к инженерам аэропорта")
def airport_warnings(context: Context = Depends(require_roles_in_airport(*DISPATCH_ROLES))):
    return proximity.warnings(icao=context.airport_icao)
