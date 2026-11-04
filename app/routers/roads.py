import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db, require_roles, verify_platform_key
from app.core.rate_limit import limit_by_user
from app.core.redis import get_redis
from app.core.road_settings import road_settings as rs
from app.models.enums import Role
from app.models.tenancy import Tenant, User
from app.schemas.schemas import TimeFilter
from app.services import road_missions
from app.services import roads as roads_service
from app.services.thresholds import build_thresholds
from app.tasks.celery_app import celery_app

router = APIRouter(prefix="/roads", tags=["roads"],
                   dependencies=[Depends(limit_by_user("roads", settings.RATE_LIMIT_API_PER_MINUTE))])
platform_router = APIRouter(prefix="/platform/roads", tags=["platform"], dependencies=[Depends(verify_platform_key)])
_staff = require_roles(Role.tenant_admin, Role.analyst)


def _parse_bbox(bbox: str) -> tuple[float, float, float, float]:
    try:
        a = tuple(float(x) for x in bbox.split(","))
        assert len(a) == 4 and -180 <= a[0] < a[2] <= 180 and -90 <= a[1] < a[3] <= 90
        return a
    except (ValueError, AssertionError):
        raise HTTPException(422, "invalid_bbox (min_lon,min_lat,max_lon,max_lat)")


async def _min_devices(db: AsyncSession, user: User) -> int:
    tenant = await db.get(Tenant, user.tenant_id)
    return build_thresholds(tenant.settings).public_min_devices


# ------------------------------------------------------------------ carte

@router.get("/network")
async def network(request: Request, bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat"),
                  zoom: float | None = Query(None, ge=0, le=22), max_rank: int | None = Query(None, ge=1, le=6),
                  user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                  redis: Redis = Depends(get_redis)):
    """Tracé réel et connecté des routes (statique, mis en cache côté serveur et côté client via ETag)."""
    box, rank = _parse_bbox(bbox), roads_service.rank_for(zoom, max_rank)
    data = await roads_service.network_features(db, redis, box, rank)
    headers = {"ETag": data["etag"], "Cache-Control": f"private, max-age={rs.ROAD_NETWORK_CLIENT_MAX_AGE}"}
    if request.headers.get("if-none-match") == data["etag"]:
        return Response(status_code=304, headers=headers)
    return JSONResponse({"type": "FeatureCollection", "features": data["features"], "truncated": data["truncated"]},
                        headers=headers)


@router.get("/state")
async def state(tf: TimeFilter = Depends(), include_indicative: bool = Query(False),
                bbox: str = Query(...), zoom: float | None = Query(None, ge=0, le=22),
                max_rank: int | None = Query(None, ge=1, le=6), user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis)):
    """État de bruit des tronçons mesurés (id -> état). Tout tronçon absent est au niveau de base : calme, vert."""
    box, rank = _parse_bbox(bbox), roads_service.rank_for(zoom, max_rank)
    states = await roads_service.cached_states(db, redis, user.tenant_id, tf, include_indicative, box, rank,
                                               await _min_devices(db, user))
    return {"baseline": roads_service.baseline_state(), "segments": states}


@router.get("/map")
async def road_map(tf: TimeFilter = Depends(), include_indicative: bool = Query(False),
                   bbox: str = Query(...), zoom: float | None = Query(None, ge=0, le=22),
                   max_rank: int | None = Query(None, ge=1, le=6), user: User = Depends(get_current_user),
                   db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis)):
    """Réseau + état de bruit fusionnés (GeoJSON prêt à afficher)."""
    box, rank = _parse_bbox(bbox), roads_service.rank_for(zoom, max_rank)
    net = await roads_service.network_features(db, redis, box, rank)
    states = await roads_service.cached_states(db, redis, user.tenant_id, tf, include_indicative, box, rank,
                                               await _min_devices(db, user))
    return roads_service.merge(net, states)


@router.get("/segments/{segment_id}")
async def segment(segment_id: uuid.UUID, tf: TimeFilter = Depends(), include_indicative: bool = Query(False),
                  user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await roads_service.segment_detail(db, user.tenant_id, segment_id, tf, include_indicative,
                                              await _min_devices(db, user))


# ------------------------------------------------------------------ staff

@router.get("/stats")
async def stats(user: User = Depends(_staff), db: AsyncSession = Depends(get_db),
                redis: Redis = Depends(get_redis)):
    return {"network": await roads_service.import_status(db, redis),
            "coverage": await roads_service.stats(db, user.tenant_id)}


@router.get("/needs")
async def needs(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
                radius_km: float = Query(5, gt=0, le=15), limit: int = Query(50, ge=1, le=200),
                user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    """Routes qui manquent le plus de mesures autour d'un point, avec la plage horaire conseillée."""
    return await road_missions.needs_near(db, user.tenant_id, lat, lon, radius_km, limit)


# ------------------------------------------------------------------ plateforme (clé X-Platform-Key)

async def _public_db():
    from app.db.session import SessionLocal
    async with SessionLocal() as session:
        yield session


@platform_router.get("/import")
async def import_progress(db: AsyncSession = Depends(_public_db), redis: Redis = Depends(get_redis)):
    return await roads_service.import_status(db, redis)


@platform_router.post("/import", status_code=202)
async def import_start(force: bool = Query(False)):
    """Lance (ou relance) l'import du réseau routier ; force=true relance aussi les tuiles récentes."""
    celery_app.send_task("roads.ensure_network", args=[force])
    return {"detail": "import_scheduled", "at": datetime.now(timezone.utc)}