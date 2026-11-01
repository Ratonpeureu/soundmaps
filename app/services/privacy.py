import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.collection import CollectionProposal
from app.models.enums import ProposalStatus, QualityStatus
from app.models.geo import GeographicCell
from app.models.insights import Report
from app.models.measurement import Measurement
from app.models.tenancy import AuditLog

_BATCH = 10_000


def _center_values():
    lat = select(func.ST_Y(GeographicCell.center)).where(GeographicCell.id == Measurement.cell_id).scalar_subquery()
    lon = select(func.ST_X(GeographicCell.center)).where(GeographicCell.id == Measurement.cell_id).scalar_subquery()
    geom = select(GeographicCell.center).where(GeographicCell.id == Measurement.cell_id).scalar_subquery()
    return {"latitude": lat, "longitude": lon, "geom": geom, "location_anonymized": True}


async def round_locations(db: AsyncSession, tenant_id: uuid.UUID, where: list) -> int:
    """Remplace la position précise par le centre de la cellule H3."""
    total = 0
    while True:
        ids = select(Measurement.id).where(
            Measurement.tenant_id == tenant_id, Measurement.location_anonymized.is_(False), *where
        ).limit(_BATCH)
        res = await db.execute(update(Measurement).where(Measurement.id.in_(ids)).values(**_center_values()))
        total += res.rowcount or 0
        if (res.rowcount or 0) < _BATCH:
            return total


async def anonymize_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    now = datetime.now(timezone.utc)
    done = and_ready = [Measurement.quality_status != QualityStatus.pending, Measurement.aggregated_at.is_not(None)]

    # 1) Position précise -> centre de cellule
    loc_cutoff = now - timedelta(days=settings.LOCATION_RETENTION_DAYS)
    rounded = await round_locations(db, tenant_id, [Measurement.measured_at < loc_cutoff, *and_ready])

    # 2) Rupture du lien d'identité (utilisateur, appareil, proposition)
    id_cutoff = now - timedelta(days=settings.IDENTITY_UNLINK_DAYS)
    unlinked = 0
    while True:
        ids = select(Measurement.id).where(
            Measurement.tenant_id == tenant_id, Measurement.measured_at < id_cutoff,
            Measurement.user_id.is_not(None), *and_ready).limit(_BATCH)
        res = await db.execute(update(Measurement).where(Measurement.id.in_(ids)).values(
            user_id=None, device_id=None, proposal_id=None))
        unlinked += res.rowcount or 0
        if (res.rowcount or 0) < _BATCH:
            break

    # 3) Propositions terminées et anciennes (elles portent user_id)
    purged = await db.execute(delete(CollectionProposal).where(
        CollectionProposal.tenant_id == tenant_id, CollectionProposal.created_at < id_cutoff,
        CollectionProposal.status.in_([ProposalStatus.completed, ProposalStatus.declined, ProposalStatus.expired]),
        CollectionProposal.learned_at.is_not(None) | (CollectionProposal.status != ProposalStatus.completed)))
    return {"locations_rounded": rounded, "identities_unlinked": unlinked,
            "proposals_purged": purged.rowcount or 0}


async def purge_expired(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    now = datetime.now(timezone.utc)
    reports = await db.execute(delete(Report).where(
        Report.tenant_id == tenant_id, Report.created_at < now - timedelta(days=settings.REPORT_RETENTION_DAYS)))
    audits = await db.execute(delete(AuditLog).where(
        AuditLog.tenant_id == tenant_id, AuditLog.created_at < now - timedelta(days=settings.AUDIT_RETENTION_DAYS)))
    return {"reports": reports.rowcount or 0, "audit_logs": audits.rowcount or 0}