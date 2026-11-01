from fastapi import APIRouter, Depends, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import Principal, get_principal, get_public_db
from app.core.rate_limit import limit_by_ip
from app.core.redis import get_redis
from app.schemas.common import Msg
from app.schemas.schemas import UserMe,LoginRequest, LogoutRequest, RefreshRequest, RegisterRequest, TokenPair
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"],
                   dependencies=[Depends(limit_by_ip("auth", settings.RATE_LIMIT_AUTH_PER_MINUTE))])


@router.post("/register", response_model=UserMe, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_public_db)):
    return await auth_service.register(db, payload.tenant_slug, payload.email, payload.password)


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_public_db), redis: Redis = Depends(get_redis)):
    return await auth_service.login(db, redis, payload.tenant_slug, payload.email, payload.password)


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_public_db), redis: Redis = Depends(get_redis)):
    return await auth_service.refresh(db, redis, payload.refresh_token)


@router.post("/logout", response_model=Msg)
async def logout(payload: LogoutRequest, principal: Principal = Depends(get_principal),
                 redis: Redis = Depends(get_redis)):
    await auth_service.logout(redis, principal, payload.refresh_token)
    return Msg(detail="logged_out")