import hashlib
import json
import math

from fastapi import APIRouter, Depends, HTTPException, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db
from app.core.field_settings import field_settings as fs
from app.core.rate_limit import limit_by_user
from app.core.redis import get_redis
from app.models.tenancy import Tenant, User
from app.schemas.schemas import TimeFilter
from app.services import map as map_service
from app.services import noise_field as field_service
from app.services import timelapse as timelapse_service
from app.services.thresholds import build_thresholds
from typing import Literal

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


def _snap(bbox, step: float = 0.01):
    if bbox is None:
        return None
    return (math.floor(bbox[0] / step) * step, math.floor(bbox[1] / step) * step,
            math.ceil(bbox[2] / step) * step, math.ceil(bbox[3] / step) * step)


async def public_min_devices(db: AsyncSession, user: User) -> int:
    """Même seuil pour tout le monde (tous rôles) : la carte générale est unique et vient du serveur."""
    tenant = await db.get(Tenant, user.tenant_id)
    return build_thresholds(tenant.settings).public_min_devices


async def _cached(redis: Redis, raw: list, ttl: int, loader):
    key = "map:" + hashlib.sha256(json.dumps(raw, default=str).encode()).hexdigest()
    hit = await redis.get(key)
    if hit:
        return json.loads(hit)
    data = await loader()
    await redis.set(key, json.dumps(data, default=str), ex=ttl)
    return data


@router.get("/noise")
async def noise(tf: TimeFilter = Depends(), include_indicative: bool = Query(False),
                min_confidence: float = Query(0.0, ge=0, le=1), bbox: str | None = Query(None),
                user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                redis: Redis = Depends(get_redis)):
    box = _snap(_parse_bbox(bbox))
    min_dev = await public_min_devices(db, user)
    raw = [str(user.tenant_id), "noise", tf.period.value, str(tf.date_from), str(tf.date_to), tf.day_part,
           include_indicative, min_confidence, box, min_dev]
    return await _cached(redis, raw, fs.FIELD_MAP_CACHE_TTL_SECONDS, lambda: map_service.noise_geojson(
        db, user.tenant_id, tf, include_indicative, min_confidence, min_dev, box))


@router.get("/field")
async def field(tf: TimeFilter = Depends(), include_indicative: bool = Query(False), bbox: str = Query(...),
                user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                redis: Redis = Depends(get_redis)):
    """Champ de bruit continu (nuage) poussé par le vent : calculé une seule fois côté serveur pour tous."""
    return await field_service.build(db, redis, user.tenant_id, tf, include_indicative,
                                     await public_min_devices(db, user), _parse_bbox(bbox))


@router.get("/timelapse")
async def timelapse(tf: TimeFilter = Depends(), step: Literal["day", "daypart"] = Query("day"),
                    include_indicative: bool = Query(False), bbox: str | None = Query(None),
                    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                    redis: Redis = Depends(get_redis)):
    return await timelapse_service.build(db, redis, user.tenant_id, tf, step, include_indicative,
                                         _parse_bbox(bbox), await public_min_devices(db, user))


@router.get("/cells/{h3_index}")
async def cell(h3_index: str, tf: TimeFilter = Depends(), include_indicative: bool = Query(False),
               user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    detail = await map_service.cell_detail(db, user.tenant_id, h3_index, tf, include_indicative,
                                           await public_min_devices(db, user))
    if detail is None:
        raise HTTPException(404, "insufficient_data")
    return detail