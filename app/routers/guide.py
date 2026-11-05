from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db
from app.core.rate_limit import limit_by_user
from app.models.tenancy import Tenant, User
from app.services import guide as guide_service

router = APIRouter(prefix="/guide", tags=["guide"],
                   dependencies=[Depends(limit_by_user("guide", settings.RATE_LIMIT_API_PER_MINUTE))])


@router.get("")
async def guide(response: Response, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Guide utilisateur dynamique : les valeurs viennent de la configuration réelle (et des seuils du tenant)."""
    tenant = await db.get(Tenant, user.tenant_id)
    response.headers["Cache-Control"] = "private, max-age=300"
    return guide_service.build(tenant.settings if tenant else None, user.role.value)