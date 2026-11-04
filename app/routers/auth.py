from fastapi import APIRouter, Depends, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account_settings import account_settings as acs
from app.core.config import settings
from app.core.deps import Principal, get_current_user, get_db, get_principal, get_public_db
from app.core.rate_limit import limit_by_ip
from app.core.redis import get_redis
from app.models.tenancy import User
from app.schemas.accounts import (LoginIn, PasswordChangeIn, PasswordConfirmIn, PasswordResetIn, RegisterIn,
                                  RegisterOut)
from app.schemas.common import Msg
from app.schemas.schemas import LogoutRequest, RefreshRequest, TokenPair
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"],
                   dependencies=[Depends(limit_by_ip("auth", settings.RATE_LIMIT_AUTH_PER_MINUTE))])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/register", response_model=RegisterOut, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterIn, db: AsyncSession = Depends(get_public_db)):
    user, profile, codes = await auth_service.register(db, payload)
    return RegisterOut(id=user.id, pseudonym=user.pseudonym, display_name=profile.display_name, role=user.role,
                       created_at=user.created_at, recovery_codes=codes)


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginIn, request: Request, db: AsyncSession = Depends(get_public_db),
                redis: Redis = Depends(get_redis)):
    return await auth_service.login(db, redis, payload, _ip(request), request.headers.get("user-agent"))


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_public_db), redis: Redis = Depends(get_redis)):
    return await auth_service.refresh(db, redis, payload.refresh_token)


@router.post("/logout", response_model=Msg)
async def logout(payload: LogoutRequest, principal: Principal = Depends(get_principal),
                 redis: Redis = Depends(get_redis)):
    await auth_service.logout(redis, principal, payload.refresh_token)
    return Msg(detail="logged_out")


@router.post("/logout-all", response_model=Msg)
async def logout_all(principal: Principal = Depends(get_principal), redis: Redis = Depends(get_redis)):
    await auth_service.logout_all(redis, principal)
    return Msg(detail="all_sessions_revoked")


@router.post("/password/change", response_model=Msg)
async def password_change(payload: PasswordChangeIn, request: Request, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis)):
    await auth_service.change_password(db, redis, user, payload.current_password, payload.new_password, request)
    return Msg(detail="password_changed")


@router.post("/password/reset", response_model=Msg,
             dependencies=[Depends(limit_by_ip("pwreset", acs.RATE_LIMIT_PWRESET_PER_MINUTE))])
async def password_reset(payload: PasswordResetIn, db: AsyncSession = Depends(get_public_db),
                         redis: Redis = Depends(get_redis)):
    await auth_service.reset_password(db, redis, payload.email, payload.recovery_code, payload.new_password)
    return Msg(detail="password_reset")


@router.post("/recovery-codes", response_model=list[str])
async def recovery_codes(payload: PasswordConfirmIn, user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    """Régénère les codes de récupération (les anciens sont invalidés)."""
    return await auth_service.regenerate_recovery_codes(db, user, payload.password)