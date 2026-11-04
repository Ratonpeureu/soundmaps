import json
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account_settings import account_settings as acs
from app.core.config import settings
from app.core.security import new_code, recovery_hash
from app.models.devices import Device, DeviceModel
from app.models.social import Invitation, LoginEvent, RecoveryCode, UserProfile
from app.models.tenancy import User

_COMMON = {"password", "motdepasse", "1234567890", "123456789a", "azertyuiop", "azerty1234", "qwerty1234",
           "password1", "password123", "motdepasse1", "admin12345", "sunusound", "sunusound1", "senegal123",
           "dakar12345", "bonjour123", "iloveyou12", "0123456789", "abcd123456", "a1b2c3d4e5"}
_RESERVED = ("contrib-", "admin", "support", "sunusound", "moderat", "system")


def clean(s: str | None, n: int) -> str | None:
    if s is None:
        return None
    s = " ".join(s.split())
    return s[:n] or None


def display_key(name: str) -> str:
    return unicodedata.normalize("NFKC", " ".join(name.split())).casefold()


def validate_password_policy(password: str, *, email: str | None = None, names: tuple | list = ()) -> None:
    low = password.lower()
    classes = sum([any(c.islower() for c in password), any(c.isupper() for c in password),
                   any(c.isdigit() for c in password), any(not c.isalnum() for c in password)])
    if (len(password) < settings.PASSWORD_MIN_LENGTH or len(password) > 128
            or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password)
            or (len(password) < 14 and classes < 3) or len(set(password)) < 5):
        raise HTTPException(422, "password_policy_violation")
    if low in _COMMON or low.rstrip("0123456789!") in _COMMON:
        raise HTTPException(422, "password_too_common")
    parts = [p.lower() for p in names if p] + ([email.split("@")[0].lower()] if email else [])
    for p in parts:
        for token in [p, *p.split()]:
            if len(token) >= 4 and token in low:
                raise HTTPException(422, "password_contains_identity")


def check_display_name(name: str) -> str:
    key = display_key(name)
    if any(key.startswith(r) for r in _RESERVED):
        raise HTTPException(422, "display_name_reserved")
    return key


# ------------------------------------------------------------------ invitations

async def create_invitation(db: AsyncSession, tenant_id: uuid.UUID, creator_id: uuid.UUID | None, kind: str,
                            max_uses: int, ttl_days: int, note: str | None = None) -> Invitation:
    for _ in range(6):
        inv = Invitation(tenant_id=tenant_id, code=new_code(acs.INVITE_CODE_LENGTH), created_by=creator_id,
                         kind=kind, max_uses=max_uses, uses=0, active=True, note=note,
                         expires_at=datetime.now(timezone.utc) + timedelta(days=ttl_days))
        try:
            async with db.begin_nested():
                db.add(inv)
        except IntegrityError:
            continue
        return inv
    raise HTTPException(500, "invitation_generation_failed")


async def consume_invitation(db: AsyncSession, tenant_id: uuid.UUID, code: str) -> Invitation:
    c = code.strip().upper().replace(" ", "")
    inv = (await db.execute(select(Invitation).where(
        Invitation.tenant_id == tenant_id, Invitation.code == c).with_for_update())).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if (inv is None or not inv.active or (inv.expires_at and inv.expires_at <= now) or inv.uses >= inv.max_uses):
        raise HTTPException(400, "invalid_invitation")
    inv.uses += 1
    if inv.uses >= inv.max_uses:
        inv.active = False
    return inv


def invitation_out(i: Invitation) -> dict:
    return {"id": str(i.id), "code": i.code, "kind": i.kind, "uses": i.uses, "max_uses": i.max_uses,
            "expires_at": i.expires_at, "active": i.active, "note": i.note, "created_at": i.created_at}


# ------------------------------------------------------------------ codes de récupération

def make_recovery_codes(tenant_id: uuid.UUID, user_id: uuid.UUID) -> tuple[list[str], list[RecoveryCode]]:
    plain = [f"{new_code(5)}-{new_code(5)}" for _ in range(acs.RECOVERY_CODES_COUNT)]
    rows = [RecoveryCode(tenant_id=tenant_id, user_id=user_id, code_hash=recovery_hash(user_id, c)) for c in plain]
    return plain, rows


# ------------------------------------------------------------------ profil

async def ensure_profile(db: AsyncSession, user: User) -> UserProfile:
    p = await db.get(UserProfile, user.id)
    if p is None:                                         # comptes créés avant cette version
        p = UserProfile(user_id=user.id, tenant_id=user.tenant_id, display_name=user.pseudonym,
                        display_key=display_key(user.pseudonym), xp=0)
        db.add(p)
        await db.flush()
    return p


async def update_profile(db: AsyncSession, user: User, data: dict) -> UserProfile:
    p = await ensure_profile(db, user)
    if "display_name" in data and data["display_name"] is not None:
        name = " ".join(data["display_name"].split())
        key = check_display_name(name)
        if key != p.display_key:
            taken = (await db.execute(select(UserProfile.user_id).where(
                UserProfile.tenant_id == user.tenant_id, UserProfile.display_key == key,
                UserProfile.user_id != user.id))).first()
            if taken:
                raise HTTPException(409, "display_name_taken")
        p.display_name, p.display_key = name, key
    for k, n in (("first_name", 60), ("last_name", 60), ("phone", 24), ("city", 80), ("district", 80)):
        if k in data:
            setattr(p, k, clean(data[k], n))
    if data.get("birth_year") is not None:
        p.birth_year = data["birth_year"]
    if data.get("show_in_leaderboard") is not None:
        p.show_in_leaderboard = data["show_in_leaderboard"]
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "display_name_taken")
    return p


# ------------------------------------------------------------------ connexions et sessions

async def list_sessions(redis: Redis, user_id: uuid.UUID) -> list[dict]:
    out = []
    for jti in await redis.smembers(f"rt:user:{user_id}"):
        raw = await redis.get(f"rts:{jti}")
        if raw is None:
            continue
        d = json.loads(raw)
        d["expires_in"] = await redis.ttl(f"rt:{jti}")
        out.append(d)
    out.sort(key=lambda d: d.get("last_used", ""), reverse=True)
    return out


async def revoke_all_sessions(redis: Redis, user_id: uuid.UUID) -> None:
    members = await redis.smembers(f"rt:user:{user_id}")
    pipe = redis.pipeline()
    for m in members:
        pipe.delete(f"rt:{m}")
        pipe.delete(f"rts:{m}")
    pipe.delete(f"rt:user:{user_id}")
    await pipe.execute()


async def connections(db: AsyncSession, redis: Redis, user: User) -> dict:
    events = (await db.execute(select(LoginEvent).where(LoginEvent.user_id == user.id)
                               .order_by(LoginEvent.created_at.desc()).limit(acs.LOGIN_EVENTS_LIST))).scalars().all()
    devices = (await db.execute(select(Device, DeviceModel).join(DeviceModel, DeviceModel.id == Device.device_model_id)
                                .where(Device.user_id == user.id).order_by(Device.last_seen_at.desc().nulls_last()))).all()
    return {"last_login_at": user.last_login_at,
            "events": [{"at": e.created_at, "success": e.success, "platform": e.platform} for e in events],
            "sessions": [{"created": s.get("created"), "last_used": s.get("last_used"), "platform": s.get("platform")}
                         for s in await list_sessions(redis, user.id)],
            "devices": [{"manufacturer": m.manufacturer, "model": m.model, "android_version": m.android_version,
                         "last_seen_at": d.last_seen_at} for d, m in devices]}


async def cleanup(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    from sqlalchemy import delete
    cutoff = datetime.now(timezone.utc) - timedelta(days=acs.LOGIN_EVENTS_RETENTION_DAYS)
    r = await db.execute(delete(LoginEvent).where(LoginEvent.tenant_id == tenant_id, LoginEvent.created_at < cutoff))
    r2 = await db.execute(delete(Invitation).where(
        Invitation.tenant_id == tenant_id, Invitation.expires_at < datetime.now(timezone.utc) - timedelta(days=30)))
    return (r.rowcount or 0) + (r2.rowcount or 0)