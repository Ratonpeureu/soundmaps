from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db, require_roles
from app.core.rate_limit import limit_by_user
from app.core.redis import get_redis
from app.models.collection import CollectionProposal
from app.models.enums import ProposalStatus, Role
from app.models.quota import UserQuota
from app.models.tenancy import User
from app.schemas.quota import QuotaCheckIn, QuotaCheckOut, QuotaOut
from app.services import quota as quota_service
from datetime import datetime, timezone

router = APIRouter(prefix="/quota", tags=["quota"],
                   dependencies=[Depends(limit_by_user("quota", settings.RATE_LIMIT_API_PER_MINUTE))])
_staff = require_roles(Role.tenant_admin, Role.analyst)


@router.get("/me", response_model=QuotaOut)
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
             redis: Redis = Depends(get_redis)):
    out = await quota_service.state(db, redis, user)
    await db.commit()
    return out


@router.post("/check", response_model=QuotaCheckOut)
async def check(payload: QuotaCheckIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                redis: Redis = Depends(get_redis)):
    """Pré-contrôle avant de démarrer une mesure (sans débit). Le serveur reste l'arbitre à l'envoi."""
    free = False
    if payload.proposal_id is not None:
        p = await db.get(CollectionProposal, payload.proposal_id)
        free = p is not None and p.user_id == user.id and p.status == ProposalStatus.accepted
    d = await quota_service.evaluate(db, redis, user, payload.latitude, payload.longitude,
                                     datetime.now(timezone.utc), proposal=free, hard=True, consume=False)
    await db.commit()
    return QuotaCheckOut(allowed=d.allowed, reason=d.reason, cost=round(d.cost, 2), balance=round(d.balance, 2),
                         cooldown_minutes=round(d.cooldown_minutes, 1), retry_at=d.retry_at,
                         cell_need=round(d.cell_need, 3), free=d.free)


@router.get("/overview")
async def overview(user: User = Depends(_staff), db: AsyncSession = Depends(get_db),
                   redis: Redis = Depends(get_redis)):
    ctx = await quota_service.global_context(db, redis, user.tenant_id, datetime.now(timezone.utc))
    n, avg_bal, avg_target = (await db.execute(
        select(func.count(), func.avg(UserQuota.balance), func.avg(UserQuota.daily_target)).where(
            UserQuota.tenant_id == user.tenant_id))).one()
    return {"context": ctx, "users": int(n), "avg_balance": round(float(avg_bal or 0), 2),
            "avg_daily_target": round(float(avg_target or 0), 2)}