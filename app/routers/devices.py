from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db
from app.core.rate_limit import limit_by_user
from app.models.tenancy import User
from app.schemas.schemas import DeviceOut, DeviceRegister
from app.services import device as device_service

router = APIRouter(prefix="/devices", tags=["devices"],
                   dependencies=[Depends(limit_by_user("devices", settings.RATE_LIMIT_API_PER_MINUTE))])


@router.post("/register", response_model=DeviceOut)
async def register(payload: DeviceRegister, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await device_service.register_device(db, user, payload)


@router.get("/me", response_model=list[DeviceOut])
async def mine(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await device_service.my_devices(db, user)