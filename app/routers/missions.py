import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db, require_roles
from app.core.rate_limit import limit_by_user
from app.core.redis import get_redis
from app.models.enums import MissionStatus, Role
from app.models.quota import Mission
from app.models.tenancy import User
from app.schemas.quota import MissionOut, MissionSyncIn, MissionTarget
from app.services import missions as missions_service
from app.services import tomtom as tt


router = APIRouter(prefix="/missions", tags=["missions"],
                   dependencies=[Depends(limit_by_user("missions", settings.RATE_LIMIT_API_PER_MINUTE))])


_staff = require_roles(Role.tenant_admin, Role.analyst)


@router.get("", response_model=list[MissionOut])
async def list_missions(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await missions_service.reconcile(db, user.tenant_id, user.id)
    await db.commit()
    return await missions_service.list_for_user(db, user)


@router.post("/sync", response_model=list[MissionOut])
async def sync(payload: MissionSyncIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
               redis: Redis = Depends(get_redis)):
    await missions_service.reconcile(db, user.tenant_id, user.id)
    await missions_service.sync_for_user(db, redis, user, payload.latitude, payload.longitude)
    await db.commit()
    return await missions_service.list_for_user(db, user)


@router.post("/{mission_id}/accept", response_model=MissionOut)
async def accept(mission_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await missions_service.respond(db, user, mission_id, True)


@router.post("/{mission_id}/decline", response_model=MissionOut)
async def decline(mission_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await missions_service.respond(db, user, mission_id, False)


@router.get("/{mission_id}/route",
            dependencies=[Depends(limit_by_user("tomtom", settings.RATE_LIMIT_TOMTOM_PER_MINUTE))])
async def mission_route(mission_id: uuid.UUID, o_lat: float = Query(..., ge=-90, le=90),
                        o_lon: float = Query(..., ge=-180, le=180),
                        mode: Literal["car", "pedestrian", "bicycle", "motorcycle", "taxi", "bus"] = "car",
                        user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Itinéraire (TomTom) de la position de l'utilisateur jusqu'au point cible de la mission."""
    m = await db.get(Mission, mission_id)
    if m is None or m.user_id != user.id:
        raise HTTPException(404, "mission_not_found")
    if m.status not in (MissionStatus.offered, MissionStatus.accepted):
        raise HTTPException(409, "mission_closed")
    route = await tt.route(o_lat, o_lon, m.center_lat, m.center_lon, mode)
    target = MissionTarget(lat=m.center_lat, lon=m.center_lon, radius_m=m.radius_m, day_part=m.target_day_part,
                           window_start_at=m.window_start_at, window_end_at=m.window_end_at,
                           segment_id=m.road_segment_id, road_name=m.road_name, road_class=m.road_class)
    return {"mission_id": str(m.id), "target": target, "route": route}


@router.get("/ranking/active")
async def ranking(user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    """Utilisateurs les plus actifs / fiables (pseudonymes uniquement)."""
    return await missions_service.ranking(db, user.tenant_id)


@router.post("/generate")
async def generate(user: User = Depends(_staff), db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis)):
    """Déclenche immédiatement la génération de missions pour le tenant."""
    created = await missions_service.generate_for_tenant(db, redis, user.tenant_id)
    await db.commit()
    return {"created": created}