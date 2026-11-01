import hmac
import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import get_redis
from app.core.security import decode_token
from app.db.session import SessionLocal
from app.models.enums import Role
from app.models.tenancy import Tenant, User

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: Role
    jti: str
    exp: int


def _unauthorized(detail: str = "not_authenticated") -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


async def get_principal(creds: HTTPAuthorizationCredentials | None = Depends(bearer),
                        redis: Redis = Depends(get_redis)) -> Principal:
    if creds is None:
        raise _unauthorized()
    try:
        claims = decode_token(creds.credentials, "access")
        principal = Principal(uuid.UUID(claims["sub"]), uuid.UUID(claims["tid"]),
                              Role(claims["role"]), claims["jti"], int(claims["exp"]))
    except (jwt.PyJWTError, ValueError, KeyError):
        raise _unauthorized("invalid_token")
    if await redis.exists(f"deny:{principal.jti}"):
        raise _unauthorized("token_revoked")
    return principal


async def get_db(principal: Principal = Depends(get_principal)):
    """Session liée au tenant du jeton : toutes les requêtes passent sous RLS."""
    async with SessionLocal() as session:
        session.info["tenant_id"] = str(principal.tenant_id)
        yield session


async def get_public_db():
    """Session sans tenant (routes d'authentification). Le tenant est lié ensuite via bind_tenant."""
    async with SessionLocal() as session:
        yield session


async def get_current_user(principal: Principal = Depends(get_principal),
                           db: AsyncSession = Depends(get_db)) -> User:
    tenant = await db.get(Tenant, principal.tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "tenant_inactive")
    user = await db.get(User, principal.user_id)
    if user is None or not user.is_active or user.tenant_id != principal.tenant_id:
        raise _unauthorized("user_inactive")
    return user


def require_roles(*roles: Role):
    async def _dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient_role")
        return user
    return _dep


async def verify_platform_key(x_platform_key: str = Header(default="")) -> None:
    expected = settings.PLATFORM_ADMIN_KEY.get_secret_value()
    if not x_platform_key or not hmac.compare_digest(x_platform_key, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")