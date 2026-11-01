from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db, require_roles
from app.core.rate_limit import limit_by_user
from app.core.redis import get_redis
from app.models.enums import Role
from app.models.insights import AIInsight
from app.models.tenancy import User
from app.schemas.schemas import TimeFilter,CellAnalysis, InsightOut
from app.services import ai as ai_service

router = APIRouter(prefix="/ai", tags=["ai"])
_staff = require_roles(Role.tenant_admin, Role.analyst)


@router.post("/cells/{h3_index}/analysis", response_model=CellAnalysis,
             dependencies=[Depends(limit_by_user("ai", settings.RATE_LIMIT_AI_PER_MINUTE))])
async def analyze(h3_index: str, tf: TimeFilter = Depends(), user: User = Depends(_staff),
                  db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis)):
    result = await ai_service.analyze_cell(db, redis, user.tenant_id, h3_index, tf)
    return result if isinstance(result, dict) else result.content


@router.get("/insights", response_model=list[InsightOut])
async def insights(h3_index: str | None = None, limit: int = Query(50, ge=1, le=200),
                   user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    q = select(AIInsight).where(AIInsight.tenant_id == user.tenant_id)
    if h3_index:
        q = q.where(AIInsight.h3_index == h3_index)
    return (await db.execute(q.order_by(AIInsight.created_at.desc()).limit(limit))).scalars().all()