from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db, require_roles
from app.core.rate_limit import limit_by_user
from app.core.redis import get_redis
from app.core.survey_settings import survey_settings as sv
from app.models.enums import Role
from app.models.tenancy import User
from app.services import catalog, datamap, survey_ai

router = APIRouter(prefix="/datamap", tags=["datamap"],
                   dependencies=[Depends(limit_by_user("datamap", settings.RATE_LIMIT_API_PER_MINUTE))])
_staff = require_roles(Role.tenant_admin, Role.analyst)


@router.get("/catalog")
async def get_catalog(user: User = Depends(get_current_user)):
    """Les 12 domaines, leurs indicateurs, leurs unités et le type de rendu cartographique."""
    return catalog.public()


@router.get("/summary")
async def summary(days: int = Query(180, ge=1, le=730), user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db)):
    return await datamap.summary(db, user.tenant_id, days)


@router.get("/map")
async def map_(indicator: str = Query(..., max_length=48), metric: str | None = Query(None, max_length=60),
               res: int | None = Query(None, ge=5, le=9), zoom: float | None = Query(None, ge=0, le=22),
               days: int = Query(180, ge=1, le=730), include_indicative: bool = Query(False),
               bbox: str | None = Query(None), user: User = Depends(get_current_user),
               db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis)):
    """Carte d'un indicateur : zones H3 colorées / barres / marqueurs selon `viz`. Rien n'est affiché sans données réelles."""
    return await datamap.build_map(db, redis, user.tenant_id, indicator, metric=metric, res=res or datamap.res_for_zoom(zoom),
                                   days=days, include_indicative=include_indicative, bbox=datamap.parse_bbox(bbox))


@router.get("/cells/{h3_index}")
async def cell(h3_index: str, domain: str = Query(...), days: int = Query(180, ge=1, le=730),
               include_indicative: bool = Query(False), user: User = Depends(get_current_user),
               db: AsyncSession = Depends(get_db)):
    return await datamap.cell_detail(db, user.tenant_id, h3_index, domain=domain, days=days,
                                     include_indicative=include_indicative)


@router.get("/trend")
async def trend(indicator: str = Query(..., max_length=48), h3_index: str | None = Query(None, max_length=16),
                metric: str | None = Query(None, max_length=60), months: int = Query(12, ge=2, le=36),
                include_indicative: bool = Query(False), user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db)):
    return await datamap.trend(db, user.tenant_id, indicator, h3_index, metric=metric, months=months,
                               include_indicative=include_indicative)


@router.post("/analysis", dependencies=[Depends(limit_by_user("ai", settings.RATE_LIMIT_AI_PER_MINUTE))])
async def analysis(indicator: str = Query(..., max_length=48), metric: str | None = Query(None, max_length=60),
                   res: int = Query(sv.SM_DEFAULT_RES, ge=5, le=9), days: int = Query(180, ge=1, le=730),
                   include_indicative: bool = Query(False), bbox: str | None = Query(None),
                   user: User = Depends(_staff), db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis)):
    """Lecture assistée par IA d'une carte (agrégats anonymes seulement ; hypothèses présentées comme telles)."""
    m = await datamap.build_map(db, redis, user.tenant_id, indicator, metric=metric, res=res, days=days,
                                include_indicative=include_indicative, bbox=datamap.parse_bbox(bbox))
    return await survey_ai.analyze_indicator(db, redis, user.tenant_id, m)