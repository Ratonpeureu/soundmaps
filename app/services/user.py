import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import verify_password
from app.models.collection import CollectionProposal, CollectionSchedule
from app.models.enums import ConsentType, QualityStatus
from app.models.measurement import Measurement
from app.models.tenancy import User, UserConsent
from app.schemas.schemas import ConsentOut, ConsentUpdate, ContributionsOut, ScheduleIn
from app.services.privacy import round_locations


async def latest_consents(db: AsyncSession, user_id: uuid.UUID) -> dict[ConsentType, UserConsent]:
    rows = (await db.execute(
        select(UserConsent).where(UserConsent.user_id == user_id)
        .order_by(UserConsent.consent_type, UserConsent.created_at.desc())
        .distinct(UserConsent.consent_type))).scalars()
    return {r.consent_type: r for r in rows}


async def list_consents(db: AsyncSession, user: User) -> list[ConsentOut]:
    current = await latest_consents(db, user.id)
    return [ConsentOut(consent_type=t, granted=c.granted, policy_version=c.policy_version, updated_at=c.created_at)
            for t, c in current.items()]


async def set_consent(db: AsyncSession, user: User, payload: ConsentUpdate) -> list[ConsentOut]:
    db.add(UserConsent(tenant_id=user.tenant_id, user_id=user.id, consent_type=payload.consent_type,
                       granted=payload.granted, policy_version=settings.CONSENT_POLICY_VERSION))
    await db.commit()
    return await list_consents(db, user)


async def require_consents(db: AsyncSession, user: User) -> None:
    current = await latest_consents(db, user.id)
    for t in (ConsentType.geolocation, ConsentType.microphone):
        if t not in current or not current[t].granted:
            raise HTTPException(403, "consent_required")


async def upsert_schedule(db: AsyncSession, user: User, payload: ScheduleIn) -> CollectionSchedule:
    schedule = (await db.execute(select(CollectionSchedule).where(CollectionSchedule.user_id == user.id))).scalar_one_or_none()
    data = payload.model_dump()
    data["timezone"] = data["timezone"] or settings.TIMEZONE
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(data["timezone"])
    except Exception:
        raise HTTPException(422, "invalid_timezone")
    if schedule is None:
        schedule = CollectionSchedule(tenant_id=user.tenant_id, user_id=user.id, **data)
        db.add(schedule)
    else:
        for k, v in data.items():
            setattr(schedule, k, v)
    await db.commit()
    return schedule


async def get_schedule(db: AsyncSession, user: User) -> CollectionSchedule:
    schedule = (await db.execute(select(CollectionSchedule).where(CollectionSchedule.user_id == user.id))).scalar_one_or_none()
    if schedule is None:
        raise HTTPException(404, "schedule_not_set")
    return schedule


async def contributions(db: AsyncSession, user: User) -> ContributionsOut:
    rows = (await db.execute(
        select(Measurement.quality_status, func.count()).where(
            Measurement.tenant_id == user.tenant_id, Measurement.user_id == user.id)
        .group_by(Measurement.quality_status))).all()
    by_status = {s: 0 for s in QualityStatus}
    by_status.update({s: n for s, n in rows})
    cells, last = (await db.execute(
        select(func.count(distinct(Measurement.h3_index)), func.max(Measurement.measured_at)).where(
            Measurement.tenant_id == user.tenant_id, Measurement.user_id == user.id,
            Measurement.quality_status != QualityStatus.rejected))).one()
    return ContributionsOut(total_measurements=sum(by_status.values()), by_status=by_status,
                            cells_covered=cells or 0, last_measurement_at=last)


async def erase_account(db: AsyncSession, user: User, password: str) -> None:
    """Droit à l'effacement : position arrondie, liens rompus, puis suppression du compte et des données associées."""
    if not verify_password(user.password_hash, password):
        raise HTTPException(403, "invalid_password")
    await round_locations(db, user.tenant_id, [Measurement.user_id == user.id])
    from sqlalchemy import update
    await db.execute(update(Measurement).where(
        Measurement.tenant_id == user.tenant_id, Measurement.user_id == user.id
    ).values(user_id=None, device_id=None, proposal_id=None))
    await db.execute(delete(CollectionProposal).where(CollectionProposal.user_id == user.id))
    await db.delete(user)   # consentements, appareils, planning, statistiques : suppression en cascade
    await db.commit()