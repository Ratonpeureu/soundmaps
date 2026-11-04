from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db
from app.core.rate_limit import limit_by_user
from app.models.tenancy import Tenant, User
from app.schemas.common import Msg
from app.schemas.schemas import (AccountDeleteRequest, ConsentOut, ConsentUpdate, ContributionsOut, TimeFilter,
                                 UserMe, UserUpdate)
from app.services import impact as impact_service
from app.services import user as user_service
from app.services.thresholds import build_thresholds

router = APIRouter(prefix="/users", tags=["users"],
                   dependencies=[Depends(limit_by_user("users", settings.RATE_LIMIT_API_PER_MINUTE))])


@router.get("/me", response_model=UserMe)
async def me(user: User = Depends(get_current_user)):
    return user


@router.patch("/me", response_model=UserMe)
async def update_me(payload: UserUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(user, k, v)
    await db.commit()
    return user


@router.get("/me/consents", response_model=list[ConsentOut])
async def consents(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await user_service.list_consents(db, user)


@router.put("/me/consents", response_model=list[ConsentOut])
async def set_consent(payload: ConsentUpdate, user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    return await user_service.set_consent(db, user, payload)


@router.get("/me/contributions", response_model=ContributionsOut)
async def contributions(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await user_service.contributions(db, user)


@router.get("/me/impact")
async def impact(tf: TimeFilter = Depends(), user: User = Depends(get_current_user),
                 db: AsyncSession = Depends(get_db)):
    """Mes mesures et leur effet sur la carte générale (qui, elle, reste unique pour tous)."""
    tenant = await db.get(Tenant, user.tenant_id)
    return await impact_service.my_impact(db, user, tf, build_thresholds(tenant.settings).public_min_devices)


@router.delete("/me", response_model=Msg)
async def erase(payload: AccountDeleteRequest, user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db)):
    await user_service.erase_account(db, user, payload.password)
    return Msg(detail="account_erased")