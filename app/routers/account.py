import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account_settings import account_settings as acs
from app.core.config import settings
from app.core.deps import get_current_user, get_db, require_roles
from app.core.rate_limit import limit_by_user
from app.core.redis import get_redis
from app.models.enums import Role
from app.models.social import Invitation
from app.models.tenancy import User
from app.schemas.accounts import InviteAdminIn, ProfileUpdate
from app.schemas.common import Msg
from app.services import accounts
from app.services.leaderboard import level_info

router = APIRouter(prefix="/account", tags=["account"],
                   dependencies=[Depends(limit_by_user("account", settings.RATE_LIMIT_API_PER_MINUTE))])
_admin = require_roles(Role.tenant_admin)


def _profile_out(p, user: User) -> dict:
    return {"display_name": p.display_name, "first_name": p.first_name, "last_name": p.last_name, "phone": p.phone,
            "city": p.city, "district": p.district, "birth_year": p.birth_year,
            "show_in_leaderboard": p.show_in_leaderboard, "analyst_enabled": p.analyst_enabled,
            "member_since": user.created_at, "level": level_info(p.xp or 0)}


@router.get("/me")
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await accounts.ensure_profile(db, user)
    await db.commit()
    return _profile_out(p, user)


@router.patch("/me")
async def update_me(payload: ProfileUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await accounts.update_profile(db, user, payload.model_dump(exclude_unset=True))
    return _profile_out(p, user)


@router.get("/connections")
async def connections(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                      redis: Redis = Depends(get_redis)):
    return await accounts.connections(db, redis, user)


@router.get("/invitations")
async def my_invitations(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Invitation).where(Invitation.created_by == user.id)
                             .order_by(Invitation.created_at.desc()).limit(20))).scalars().all()
    return [accounts.invitation_out(i) for i in rows]


@router.post("/invitations", status_code=201)
async def create_my_invitation(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    active = (await db.execute(select(func.count()).select_from(Invitation).where(
        Invitation.created_by == user.id, Invitation.active.is_(True), Invitation.expires_at > now))).scalar_one()
    if active >= acs.INVITE_USER_MAX_ACTIVE:
        raise HTTPException(409, "invite_limit_reached")
    inv = await accounts.create_invitation(db, user.tenant_id, user.id, "user", acs.INVITE_USER_MAX_USES,
                                           acs.INVITE_USER_TTL_DAYS)
    await db.commit()
    return accounts.invitation_out(inv)


@router.delete("/invitations/{invitation_id}", response_model=Msg)
async def revoke_invitation(invitation_id: uuid.UUID, user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    inv = await db.get(Invitation, invitation_id)
    if inv is None or (inv.created_by != user.id and user.role != Role.tenant_admin):
        raise HTTPException(404, "invitation_not_found")
    inv.active = False
    await db.commit()
    return Msg(detail="revoked")


@router.get("/invitations/all")
async def all_invitations(admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Invitation).where(Invitation.tenant_id == admin.tenant_id)
                             .order_by(Invitation.created_at.desc()).limit(300))).scalars().all()
    return [accounts.invitation_out(i) for i in rows]


@router.post("/invitations/admin", status_code=201)
async def create_admin_invitations(payload: InviteAdminIn, admin: User = Depends(_admin),
                                   db: AsyncSession = Depends(get_db)):
    out = [await accounts.create_invitation(db, admin.tenant_id, admin.id, "admin", payload.max_uses,
                                            payload.ttl_days, payload.note) for _ in range(payload.count)]
    await db.commit()
    return [accounts.invitation_out(i) for i in out]