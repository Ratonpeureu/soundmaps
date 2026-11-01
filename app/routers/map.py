from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db
from app.core.rate_limit import limit_by_user
from app.models.enums import Role
from app.models.tenancy import Tenant, User
from app.schemas.schemas import TimeFilter
from app.services import map as map_service
from app.services.thresholds import build_thresholds

router = APIRouter(prefix="/map", tags=["map"],
                   dependencies=[Depends(limit_by_user("map", settings.RATE_LIMIT_API_PER_MINUTE))])


def _parse_bbox(bbox: str | None):
    if not bbox:
        return None
    try:
        a = tuple(float(x) for x in bbox.split(","))
        assert len(a) == 4 and -180 <= a[0] < a[2] <= 180 and -90 <= a[1] < a[3] <= 90
        return a
    except (ValueError, AssertionError):
        raise HTTPException(422, "invalid_bbox (min_lon,min_lat,max_lon,max_lat)")


async def _min_devices(db: AsyncSession, user: User) -> int:
    """k-anonymat : les contributeurs ne voient que les cellules ayant assez de contributeurs distincts."""
    if user.role in (Role.tenant_admin, Role.analyst):
        return 1
    tenant = await db.get(Tenant, user.tenant_id)
    return build_thresholds(tenant.settings).public_min_devices


@router.get("/noise")
async def noise(tf: TimeFilter = Depends(), include_indicative: bool = Query(False),
                min_confidence: float = Query(0.0, ge=0, le=1), bbox: str | None = Query(None),
                user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await map_service.noise_geojson(db, user.tenant_id, tf, include_indicative, min_confidence,
                                           await _min_devices(db, user), _parse_bbox(bbox))


@router.get("/cells/{h3_index}")
async def cell(h3_index: str, tf: TimeFilter = Depends(), include_indicative: bool = Query(False),
               user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    detail = await map_service.cell_detail(db, user.tenant_id, h3_index, tf, include_indicative,
                                           await _min_devices(db, user))
    if detail is None:
        raise HTTPException(404, "insufficient_data")
    return detail