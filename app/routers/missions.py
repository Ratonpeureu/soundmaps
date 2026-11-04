import uuid

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db, require_roles
from app.core.rate_limit import limit_by_user
from app.core.redis import get_redis
from app.models.enums import Role
from app.models.tenancy import User
from app.schemas.quota import MissionOut, MissionSyncIn
from app.services import missions as missions_service

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