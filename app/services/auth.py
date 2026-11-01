import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
import jwt

from app.core.config import settings
from app.core.deps import Principal
from app.core.security import (create_token, decode_token, hash_email, hash_password, make_pseudonym,
                               password_needs_rehash, verify_password)
from app.db.session import bind_tenant
from app.models.enums import Role
from app.models.tenancy import Tenant, User
from app.schemas.schemas import TokenPair

_DUMMY_HASH = hash_password("sunusound-timing-equalizer")


def validate_password_policy(password: str) -> None:
    if (len(password) < settings.PASSWORD_MIN_LENGTH
            or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password)):
        raise HTTPException(422, "password_policy_violation")


async def _active_tenant_by_slug(db: AsyncSession, slug: str) -> Tenant | None:
    return (await db.execute(select(Tenant).where(Tenant.slug == slug, Tenant.is_active.is_(True)))).scalar_one_or_none()


async def register(db: AsyncSession, tenant_slug: str, email: str, password: str) -> User:
    validate_password_policy(password)
    tenant = await _active_tenant_by_slug(db, tenant_slug)
    if tenant is None or not tenant.allow_registration:
        raise HTTPException(400, "registration_unavailable")
    await bind_tenant(db, tenant.id)
    user_id = uuid.uuid4()
    user = User(id=user_id, tenant_id=tenant.id, email_hash=hash_email(tenant.id, email),
                password_hash=hash_password(password), role=Role.contributor,
                pseudonym=make_pseudonym(tenant.id, user_id))
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "account_exists")
    return user


async def _issue_tokens(redis: Redis, user: User) -> TokenPair:
    access, _, access_ttl = create_token("access", user.id, user.tenant_id, user.role.value)
    refresh, rjti, refresh_ttl = create_token("refresh", user.id, user.tenant_id, user.role.value)
    pipe = redis.pipeline()
    pipe.set(f"rt:{rjti}", str(user.id), ex=refresh_ttl)
    pipe.sadd(f"rt:user:{user.id}", rjti)
    pipe.expire(f"rt:user:{user.id}", refresh_ttl)
    await pipe.execute()
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=access_ttl)


async def login(db: AsyncSession, redis: Redis, tenant_slug: str, email: str, password: str) -> TokenPair:
    tenant = await _active_tenant_by_slug(db, tenant_slug)
    if tenant is None:
        verify_password(_DUMMY_HASH, password)
        raise HTTPException(401, "invalid_credentials")
    email_hash = hash_email(tenant.id, email)
    lock_key = f"login:lock:{tenant.id}:{email_hash}"
    if await redis.exists(lock_key):
        raise HTTPException(429, "account_temporarily_locked")
    await bind_tenant(db, tenant.id)
    user = (await db.execute(select(User).where(User.tenant_id == tenant.id,
                                                User.email_hash == email_hash))).scalar_one_or_none()
    ok = verify_password(user.password_hash if user else _DUMMY_HASH, password) and user is not None and user.is_active
    if not ok:
        attempts_key = f"login:att:{tenant.id}:{email_hash}"
        attempts = await redis.incr(attempts_key)
        await redis.expire(attempts_key, settings.LOGIN_LOCK_SECONDS)
        if attempts >= settings.LOGIN_MAX_ATTEMPTS:
            await redis.set(lock_key, "1", ex=settings.LOGIN_LOCK_SECONDS)
        raise HTTPException(401, "invalid_credentials")
    await redis.delete(f"login:att:{tenant.id}:{email_hash}")
    user.last_login_at = datetime.now(timezone.utc)
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    await db.commit()
    return await _issue_tokens(redis, user)


async def refresh(db: AsyncSession, redis: Redis, refresh_token: str) -> TokenPair:
    try:
        claims = decode_token(refresh_token, "refresh")
        user_id, tenant_id, jti = uuid.UUID(claims["sub"]), uuid.UUID(claims["tid"]), claims["jti"]
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "invalid_token")
    if not await redis.get(f"rt:{jti}"):
        # Jeton déjà utilisé ou révoqué : on révoque toute la famille de sessions de l'utilisateur.
        members = await redis.smembers(f"rt:user:{user_id}")
        pipe = redis.pipeline()
        for m in members:
            pipe.delete(f"rt:{m}")
        pipe.delete(f"rt:user:{user_id}")
        await pipe.execute()
        raise HTTPException(401, "refresh_token_reuse_detected")
    await redis.delete(f"rt:{jti}")
    await redis.srem(f"rt:user:{user_id}", jti)
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(401, "invalid_token")
    await bind_tenant(db, tenant_id)
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(401, "invalid_token")
    return await _issue_tokens(redis, user)


async def logout(redis: Redis, principal: Principal, refresh_token: str | None) -> None:
    remaining = max(1, principal.exp - int(datetime.now(timezone.utc).timestamp()))
    await redis.set(f"deny:{principal.jti}", "1", ex=remaining)
    if refresh_token:
        try:
            claims = decode_token(refresh_token, "refresh")
            if claims["sub"] == str(principal.user_id):
                await redis.delete(f"rt:{claims['jti']}")
                await redis.srem(f"rt:user:{principal.user_id}", claims["jti"])
        except (jwt.PyJWTError, KeyError):
            pass