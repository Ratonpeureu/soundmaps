import uuid

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db, require_roles
from app.core.rate_limit import limit_by_user
from app.core.redis import get_redis
from app.models.enums import Role
from app.models.social import UserProfile
from app.models.tenancy import User
from app.schemas.accounts import PresenceIn, SuspendIn, VoteIn
from app.schemas.common import Msg
from app.services import reviews as rv
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException

router = APIRouter(tags=["reviews"], dependencies=[Depends(limit_by_user("reviews", settings.RATE_LIMIT_API_PER_MINUTE))])
_admin = require_roles(Role.tenant_admin)


@router.get("/analyst/me")
async def analyst_me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    out = await rv.analyst_state(db, user)
    await db.commit()
    return out


@router.post("/analyst/enable", response_model=Msg)
async def enable(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await rv.set_analyst(db, user, True)
    return Msg(detail="analyst_enabled")


@router.post("/analyst/disable", response_model=Msg)
async def disable(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await rv.set_analyst(db, user, False)
    return Msg(detail="analyst_disabled")


@router.post("/presence")
async def presence(payload: PresenceIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                   redis: Redis = Depends(get_redis)):
    """Battement de présence d'un analyseur (position éphémère, 3 min) : renvoie le nombre d'avis en attente."""
    return {"pending": await rv.heartbeat(db, redis, user, payload.latitude, payload.longitude)}


@router.get("/reviews/pending")
async def pending(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await rv.pending_for(db, user)


@router.post("/reviews/{vote_id}/vote")
async def vote(vote_id: uuid.UUID, payload: VoteIn, user: User = Depends(get_current_user),
               db: AsyncSession = Depends(get_db)):
    return await rv.cast_vote(db, user, vote_id, payload.vote, payload.strong)


@router.get("/reviews/history")
async def history(limit: int = Query(40, ge=1, le=100), user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db)):
    return await rv.history_for(db, user, limit)


@router.get("/analyst/admin/list")
async def admin_list(admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    return await rv.analysts_admin(db, admin.tenant_id)


@router.post("/analyst/admin/{user_id}/suspend", response_model=Msg)
async def admin_suspend(user_id: uuid.UUID, payload: SuspendIn, admin: User = Depends(_admin),
                        db: AsyncSession = Depends(get_db)):
    p = await db.get(UserProfile, user_id)
    if p is None or p.tenant_id != admin.tenant_id:
        raise HTTPException(404, "user_not_found")
    p.analyst_suspended_until = datetime.now(timezone.utc) + timedelta(days=payload.days)
    await db.commit()
    return Msg(detail="suspended")


@router.post("/analyst/admin/{user_id}/reinstate", response_model=Msg)
async def admin_reinstate(user_id: uuid.UUID, admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    p = await db.get(UserProfile, user_id)
    if p is None or p.tenant_id != admin.tenant_id:
        raise HTTPException(404, "user_not_found")
    p.analyst_suspended_until = None
    await db.commit()
    return Msg(detail="reinstated")