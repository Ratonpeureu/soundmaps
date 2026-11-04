import json
import uuid
from datetime import datetime, timezone

import jwt
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account_settings import account_settings as acs
from app.core.config import settings
from app.core.deps import Principal
from app.core.security import (create_token, decode_token, hash_email, hash_password, hash_ip, make_pseudonym,
                               password_needs_rehash, recovery_hash, verify_password, verify_password_ex)
from app.db.session import bind_tenant
from app.models.enums import Role
from app.models.social import LoginEvent, RecoveryCode, UserProfile
from app.models.tenancy import Tenant, User
from app.schemas.accounts import LoginIn, RegisterIn
from app.schemas.schemas import TokenPair
from app.services import accounts
from app.services.audit import audit

_DUMMY_HASH = hash_password("sunusound-timing-equalizer")


def platform_of(ua: str | None) -> str:
    u = (ua or "").lower()
    if "dart" in u or "okhttp" in u:
        return "Application"
    if "android" in u:
        return "Android (navigateur)"
    if "iphone" in u or "ipad" in u:
        return "iOS (navigateur)"
    if any(x in u for x in ("windows", "macintosh", "linux", "x11")):
        return "Ordinateur (navigateur)"
    return "Autre"


async def _default_tenant(db: AsyncSession) -> Tenant | None:
    return (await db.execute(select(Tenant).where(
        Tenant.slug == settings.DEFAULT_TENANT_SLUG, Tenant.is_active.is_(True)))).scalar_one_or_none()


async def register(db: AsyncSession, p: RegisterIn) -> tuple[User, UserProfile, list[str]]:
    tenant = await _default_tenant(db)
    if tenant is None or not tenant.allow_registration:
        raise HTTPException(400, "registration_unavailable")
    await bind_tenant(db, tenant.id)
    if not p.accept_terms:
        raise HTTPException(422, "terms_not_accepted")
    first, last = accounts.clean(p.first_name, 60), accounts.clean(p.last_name, 60)
    name = " ".join(p.display_name.split())
    accounts.validate_password_policy(p.password, email=p.email, names=[name, first, last])
    key = accounts.check_display_name(name)
    if (await db.execute(select(UserProfile.user_id).where(
            UserProfile.tenant_id == tenant.id, UserProfile.display_key == key))).first():
        raise HTTPException(409, "display_name_taken")

    inv = None
    if p.invite_code:
        inv = await accounts.consume_invitation(db, tenant.id, p.invite_code)
    elif acs.INVITE_REQUIRED:
        raise HTTPException(403, "invitation_required")

    now = datetime.now(timezone.utc)
    user_id = uuid.uuid4()
    user = User(id=user_id, tenant_id=tenant.id, email_hash=hash_email(tenant.id, p.email),
                password_hash=hash_password(p.password), role=Role.contributor,
                pseudonym=make_pseudonym(tenant.id, user_id), created_at=now)
    profile = UserProfile(user_id=user_id, tenant_id=tenant.id, display_name=name, display_key=key,
                          first_name=first, last_name=last, phone=accounts.clean(p.phone, 24),
                          city=accounts.clean(p.city, 80), district=accounts.clean(p.district, 80),
                          birth_year=p.birth_year, invited_by=inv.created_by if inv else None,
                          invitation_id=inv.id if inv else None, terms_accepted_at=now, xp=0)
    codes, rows = accounts.make_recovery_codes(tenant.id, user_id)
    try:
        db.add(user)
        await db.flush()
        db.add(profile)
        db.add_all(rows)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "display_name_taken" if "display" in str(exc.orig) else "account_exists")
    return user, profile, codes


async def _issue_tokens(redis: Redis, user: User, meta: dict) -> TokenPair:
    access, _, access_ttl = create_token("access", user.id, user.tenant_id, user.role.value)
    refresh, rjti, refresh_ttl = create_token("refresh", user.id, user.tenant_id, user.role.value)
    now = datetime.now(timezone.utc).isoformat()
    meta = {**meta, "created": meta.get("created", now), "last_used": now}
    pipe = redis.pipeline()
    pipe.set(f"rt:{rjti}", str(user.id), ex=refresh_ttl)
    pipe.set(f"rts:{rjti}", json.dumps(meta), ex=refresh_ttl)
    pipe.sadd(f"rt:user:{user.id}", rjti)
    pipe.expire(f"rt:user:{user.id}", refresh_ttl)
    await pipe.execute()
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=access_ttl)


async def login(db: AsyncSession, redis: Redis, p: LoginIn, ip: str | None, ua: str | None) -> TokenPair:
    tenant = await _default_tenant(db)
    if tenant is None:
        verify_password(_DUMMY_HASH, p.password)
        raise HTTPException(401, "invalid_credentials")
    email_hash = hash_email(tenant.id, p.email)
    lock_key = f"login:lock:{tenant.id}:{email_hash}"
    if await redis.exists(lock_key):
        raise HTTPException(429, "account_temporarily_locked")
    await bind_tenant(db, tenant.id)
    user = (await db.execute(select(User).where(User.tenant_id == tenant.id,
                                                User.email_hash == email_hash))).scalar_one_or_none()
    valid, legacy = verify_password_ex(user.password_hash if user else _DUMMY_HASH, p.password)
    ok = valid and user is not None and user.is_active
    ua_s = (ua or "")[:200]
    event = dict(tenant_id=tenant.id, user_agent=ua_s, platform=platform_of(ua_s), ip_hash=hash_ip(ip) if ip else None)
    if not ok:
        attempts_key = f"login:att:{tenant.id}:{email_hash}"
        attempts = await redis.incr(attempts_key)
        await redis.expire(attempts_key, settings.LOGIN_LOCK_SECONDS)
        if attempts >= settings.LOGIN_MAX_ATTEMPTS:
            await redis.set(lock_key, "1", ex=settings.LOGIN_LOCK_SECONDS)
        if user is not None:
            db.add(LoginEvent(user_id=user.id, success=False, **event))
            await db.commit()
        raise HTTPException(401, "invalid_credentials")
    await redis.delete(f"login:att:{tenant.id}:{email_hash}")
    user.last_login_at = datetime.now(timezone.utc)
    if legacy or password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(p.password)
    db.add(LoginEvent(user_id=user.id, success=True, **event))
    await db.commit()
    return await _issue_tokens(redis, user, {"platform": event["platform"], "ua": ua_s})


async def refresh(db: AsyncSession, redis: Redis, refresh_token: str) -> TokenPair:
    try:
        claims = decode_token(refresh_token, "refresh")
        user_id, tenant_id, jti = uuid.UUID(claims["sub"]), uuid.UUID(claims["tid"]), claims["jti"]
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "invalid_token")
    if not await redis.get(f"rt:{jti}"):
        await accounts.revoke_all_sessions(redis, user_id)           # réutilisation détectée : toutes les sessions tombent
        raise HTTPException(401, "refresh_token_reuse_detected")
    raw = await redis.get(f"rts:{jti}")
    meta = json.loads(raw) if raw else {}
    await redis.delete(f"rt:{jti}", f"rts:{jti}")
    await redis.srem(f"rt:user:{user_id}", jti)
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(401, "invalid_token")
    await bind_tenant(db, tenant_id)
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(401, "invalid_token")
    return await _issue_tokens(redis, user, meta)


async def logout(redis: Redis, principal: Principal, refresh_token: str | None) -> None:
    remaining = max(1, principal.exp - int(datetime.now(timezone.utc).timestamp()))
    await redis.set(f"deny:{principal.jti}", "1", ex=remaining)
    if refresh_token:
        try:
            claims = decode_token(refresh_token, "refresh")
            if claims["sub"] == str(principal.user_id):
                await redis.delete(f"rt:{claims['jti']}", f"rts:{claims['jti']}")
                await redis.srem(f"rt:user:{principal.user_id}", claims["jti"])
        except (jwt.PyJWTError, KeyError):
            pass


async def logout_all(redis: Redis, principal: Principal) -> None:
    await accounts.revoke_all_sessions(redis, principal.user_id)
    remaining = max(1, principal.exp - int(datetime.now(timezone.utc).timestamp()))
    await redis.set(f"deny:{principal.jti}", "1", ex=remaining)


async def change_password(db: AsyncSession, redis: Redis, user: User, current: str, new: str, request=None) -> None:
    if not verify_password(user.password_hash, current):
        raise HTTPException(403, "invalid_current_password")
    profile = await accounts.ensure_profile(db, user)
    accounts.validate_password_policy(new, names=[profile.display_name, profile.first_name, profile.last_name])
    user.password_hash = hash_password(new)
    await audit(db, user, "auth.password_change", "user", user.id, request=request)
    await db.commit()
    await accounts.revoke_all_sessions(redis, user.id)


async def reset_password(db: AsyncSession, redis: Redis, email: str, code: str, new: str) -> None:
    tenant = await _default_tenant(db)
    if tenant is None:
        raise HTTPException(400, "invalid_recovery_code")
    await bind_tenant(db, tenant.id)
    eh = hash_email(tenant.id, email)
    att_key = f"pwreset:att:{tenant.id}:{eh}"
    if int(await redis.get(att_key) or 0) >= 5:
        raise HTTPException(429, "account_temporarily_locked")
    user = (await db.execute(select(User).where(User.tenant_id == tenant.id, User.email_hash == eh))).scalar_one_or_none()
    row = None
    if user is not None and user.is_active:
        row = (await db.execute(select(RecoveryCode).where(
            RecoveryCode.user_id == user.id, RecoveryCode.code_hash == recovery_hash(user.id, code),
            RecoveryCode.used_at.is_(None)))).scalar_one_or_none()
    if row is None:
        await redis.incr(att_key)
        await redis.expire(att_key, 900)
        raise HTTPException(400, "invalid_recovery_code")
    profile = await accounts.ensure_profile(db, user)
    accounts.validate_password_policy(new, email=email, names=[profile.display_name, profile.first_name, profile.last_name])
    user.password_hash = hash_password(new)
    row.used_at = datetime.now(timezone.utc)
    await audit(db, user, "auth.password_reset", "user", user.id)
    await db.commit()
    await redis.delete(att_key)
    await accounts.revoke_all_sessions(redis, user.id)


async def regenerate_recovery_codes(db: AsyncSession, user: User, password: str) -> list[str]:
    from sqlalchemy import delete
    if not verify_password(user.password_hash, password):
        raise HTTPException(403, "invalid_current_password")
    await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    codes, rows = accounts.make_recovery_codes(user.tenant_id, user.id)
    db.add_all(rows)
    await db.commit()
    return codes