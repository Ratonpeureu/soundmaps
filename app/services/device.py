from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_device
from app.models.devices import Device, DeviceModel
from app.models.tenancy import User
from app.schemas.schemas import DeviceOut, DeviceRegister
import uuid


async def register_device(db: AsyncSession, user: User, payload: DeviceRegister) -> DeviceOut:
    await db.execute(pg_insert(DeviceModel).values(
        id=uuid.uuid4(), tenant_id=user.tenant_id, manufacturer=payload.manufacturer.strip(),
        model=payload.model.strip(), android_version=payload.android_version.strip(),
        microphone=payload.microphone.strip(),
    ).on_conflict_do_nothing(constraint="uq_device_models_identity"))
    dm = (await db.execute(select(DeviceModel).where(
        DeviceModel.tenant_id == user.tenant_id, DeviceModel.manufacturer == payload.manufacturer.strip(),
        DeviceModel.model == payload.model.strip(), DeviceModel.android_version == payload.android_version.strip(),
        DeviceModel.microphone == payload.microphone.strip()))).scalar_one()
    dhash = hash_device(user.tenant_id, payload.install_id)
    device = (await db.execute(select(Device).where(
        Device.tenant_id == user.tenant_id, Device.device_hash == dhash))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if device is None:
        device = Device(tenant_id=user.tenant_id, user_id=user.id, device_model_id=dm.id, device_hash=dhash,
                        sample_rate_hz=payload.sample_rate_hz, last_seen_at=now)
        db.add(device)
    else:
        if device.user_id != user.id:
            raise HTTPException(409, "device_belongs_to_another_account")
        device.device_model_id = dm.id
        device.sample_rate_hz = payload.sample_rate_hz
        device.last_seen_at = now
    await db.commit()
    return DeviceOut(id=device.id, device_model_id=dm.id, calibration_status=dm.calibration_status,
                     sample_rate_hz=device.sample_rate_hz)


async def my_devices(db: AsyncSession, user: User) -> list[DeviceOut]:
    rows = (await db.execute(select(Device, DeviceModel).join(DeviceModel, DeviceModel.id == Device.device_model_id)
                             .where(Device.user_id == user.id))).all()
    return [DeviceOut(id=d.id, device_model_id=m.id, calibration_status=m.calibration_status,
                      sample_rate_hz=d.sample_rate_hz) for d, m in rows]