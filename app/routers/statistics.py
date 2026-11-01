from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db, require_roles
from app.core.rate_limit import limit_by_user
from app.models.enums import Role
from app.models.tenancy import User
from app.schemas.schemas import TimeFilter, OverviewOut, TimeseriesPoint
from app.services import statistics as stats_service

router = APIRouter(prefix="/statistics", tags=["statistics"],
                   dependencies=[Depends(limit_by_user("stats", settings.RATE_LIMIT_API_PER_MINUTE))])
_staff = require_roles(Role.tenant_admin, Role.analyst)


@router.get("", response_model=OverviewOut)
async def overview(tf: TimeFilter = Depends(), user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    return await stats_service.overview(db, user.tenant_id, tf)


@router.get("/timeseries", response_model=list[TimeseriesPoint])
async def timeseries(tf: TimeFilter = Depends(), include_indicative: bool = Query(False),
                     user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    return await stats_service.timeseries(db, user.tenant_id, tf, include_indicative)