from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db
from app.core.rate_limit import limit_by_user
from app.models.tenancy import User
from app.services import leaderboard as lb

router = APIRouter(prefix="/leaderboard", tags=["leaderboard"],
                   dependencies=[Depends(limit_by_user("leaderboard", settings.RATE_LIMIT_API_PER_MINUTE))])


@router.get("")
async def board(period: str = Query("week"), limit: int = Query(30, ge=1, le=100),
                user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await lb.board(db, user, period, limit)


@router.get("/me")
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    out = await lb.my_detail(db, user)
    await db.commit()
    return out