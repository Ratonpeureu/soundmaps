from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.study_settings import study_settings as sts
from app.models.enums import ParticipationStatus, RewardStatus, StudyStatus
from app.models.study import Study, StudyParticipation, StudyResponse, StudyReward
from app.services import study_rewards as rwd


async def maintain(db: AsyncSession, tenant_id) -> dict:
    """Clôture à la date de fin, expiration des invitations, réclamations perdues, bons en attente."""
    now = datetime.now(timezone.utc)
    closed = (await db.execute(update(Study).where(
        Study.tenant_id == tenant_id, Study.status == StudyStatus.active, Study.ends_at.is_not(None),
        Study.ends_at <= now).values(status=StudyStatus.closed, closed_at=now))).rowcount or 0
    expired = (await db.execute(update(StudyParticipation).where(
        StudyParticipation.tenant_id == tenant_id,
        StudyParticipation.status.in_([ParticipationStatus.invited, ParticipationStatus.seen]),
        StudyParticipation.expires_at.is_not(None), StudyParticipation.expires_at <= now)
        .values(status=ParticipationStatus.expired, finished_at=now))).rowcount or 0
    closed_ids = select(Study.id).where(Study.tenant_id == tenant_id, Study.status == StudyStatus.closed)
    expired += (await db.execute(update(StudyParticipation).where(
        StudyParticipation.tenant_id == tenant_id, StudyParticipation.status == ParticipationStatus.started,
        StudyParticipation.study_id.in_(closed_ids))
        .values(status=ParticipationStatus.expired, finished_at=now))).rowcount or 0

    lost = 0
    rows = (await db.execute(select(StudyReward).where(
        StudyReward.tenant_id == tenant_id, StudyReward.status == RewardStatus.granted,
        StudyReward.claim_deadline_at.is_not(None), StudyReward.claim_deadline_at < now)
        .with_for_update(skip_locked=True).limit(500))).scalars().all()
    for rw in rows:
        if (rw.delivery or {}).get("phone"):               # coordonnées fournies : plus de délai à surveiller
            rw.claim_deadline_at = None
            continue
        await rwd.revoke(db, rw, "claim_expired", None, None, expired=True)
        lost += 1

    filled = 0
    waiting = select(StudyReward.study_id).where(
        StudyReward.tenant_id == tenant_id, StudyReward.kind == "voucher", StudyReward.status == RewardStatus.granted,
        StudyReward.code.is_(None)).distinct()
    for s in (await db.execute(select(Study).where(Study.id.in_(waiting)))).scalars():
        filled += await rwd.fill_waiting_vouchers(db, s)
    return {"closed": closed, "invitations_expired": expired, "rewards_expired": lost, "vouchers_filled": filled}


async def privacy_sweep(db: AsyncSession, tenant_id) -> dict:
    """Numéros de téléphone supprimés après rétention ; identité détachée des réponses après la clôture."""
    now = datetime.now(timezone.utc)
    cut_c = now - timedelta(days=sts.ST_CONTACT_RETENTION_DAYS)
    contacts = (await db.execute(update(StudyResponse).where(
        StudyResponse.tenant_id == tenant_id, StudyResponse.contact.is_not(None), StudyResponse.submitted_at < cut_c)
        .values(contact=None, contact_consent=False))).rowcount or 0
    phones = (await db.execute(text(
        "UPDATE study_rewards SET delivery = (delivery - 'phone') - 'name' WHERE tenant_id = :t "
        "AND status IN ('delivered','refused','revoked','expired') AND (delivery ->> 'phone') IS NOT NULL "
        "AND COALESCE(delivered_at, decided_at, created_at) < :cut"), {"t": tenant_id, "cut": cut_c})).rowcount or 0
    cut_i = now - timedelta(days=sts.ST_IDENTITY_UNLINK_DAYS_AFTER_CLOSE)
    old = select(Study.id).where(Study.tenant_id == tenant_id, Study.status == StudyStatus.closed, Study.closed_at < cut_i)
    unlinked = (await db.execute(update(StudyResponse).where(
        StudyResponse.tenant_id == tenant_id, StudyResponse.user_id.is_not(None), StudyResponse.study_id.in_(old))
        .values(user_id=None))).rowcount or 0
    return {"contacts_cleared": contacts, "reward_phones_cleared": phones, "identities_unlinked": unlinked}