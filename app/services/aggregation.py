import uuid
from datetime import datetime, timezone

from sqlalchemy import case, delete, func, literal_column, select, tuple_, update, Float
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import CalibrationStatus, QualityStatus
from app.models.geo import NoiseAggregation
from app.models.measurement import Measurement

_TEN = literal_column("10.0")
_CHUNK = 1000


async def aggregate_dirty(db: AsyncSession, tenant_id: uuid.UUID, limit: int = 20_000) -> int:
    """Réclame atomiquement les mesures évaluées non agrégées, puis recalcule intégralement
    les agrégats (cellule, jour local, plage horaire) concernés. Idempotent."""
    now = datetime.now(timezone.utc)
    sub = (select(Measurement.id).where(
        Measurement.tenant_id == tenant_id, Measurement.quality_status != QualityStatus.pending,
        Measurement.aggregated_at.is_(None)).limit(limit).with_for_update(skip_locked=True))
    claimed = (await db.execute(
        update(Measurement).where(Measurement.id.in_(sub)).values(aggregated_at=now)
        .returning(Measurement.cell_id, Measurement.local_date, Measurement.day_part))).all()
    triples = sorted({(r[0], r[1], r[2].value) for r in claimed}, key=lambda t: (str(t[0]), t[1], t[2]))
    if not triples:
        return 0

    corrected = Measurement.laeq + Measurement.correction_db_applied
    for i in range(0, len(triples), _CHUNK):
        chunk = triples[i:i + _CHUNK]
        key = tuple_(Measurement.cell_id, Measurement.local_date, Measurement.day_part)
        agg_key = tuple_(NoiseAggregation.cell_id, NoiseAggregation.bucket_date, NoiseAggregation.day_part)
        await db.execute(delete(NoiseAggregation).where(NoiseAggregation.tenant_id == tenant_id, agg_key.in_(chunk)))
        energy = func.sum(Measurement.duration_seconds * func.power(_TEN, corrected / _TEN / _TEN * 10 / 1))
        # (10^(L/10)) : power(10, L/10)
        energy = func.sum(Measurement.duration_seconds * func.power(_TEN, corrected / _TEN))
        duration = func.sum(Measurement.duration_seconds)
        rows = (await db.execute(
            select(Measurement.cell_id, Measurement.local_date, Measurement.day_part, Measurement.quality_status,
                   func.count(Measurement.id), func.count(func.distinct(Measurement.contributor_hash)),
                   func.sum(case((Measurement.calibration_status == CalibrationStatus.calibrated, 1), else_=0)),
                   duration, energy, func.max(Measurement.lmax + Measurement.correction_db_applied),
                   func.max(Measurement.measured_at))
            .where(Measurement.tenant_id == tenant_id, key.in_(chunk),
                   Measurement.quality_status.in_([QualityStatus.validated, QualityStatus.indicative]))
            .group_by(Measurement.cell_id, Measurement.local_date, Measurement.day_part, Measurement.quality_status)
        )).all()
        import math
        for cell_id, d, part, status, n, devs, cal, dur, en, lmax, last in rows:
            db.add(NoiseAggregation(
                tenant_id=tenant_id, cell_id=cell_id, bucket_date=d, day_part=part, quality_status=status,
                measurement_count=n, device_count=devs, calibrated_count=int(cal or 0),
                duration_total=float(dur), energy_sum=float(en), laeq_db=10.0 * math.log10(float(en) / float(dur)),
                lmax_db=float(lmax), last_measurement_at=last, updated_at=now))
    return len(claimed)