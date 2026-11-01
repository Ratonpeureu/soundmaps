import uuid
from datetime import datetime, timezone

from sqlalchemy import Float, case, distinct, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.collection import CollectionProposal
from app.models.devices import DeviceModel
from app.models.enums import CalibrationStatus, DayPart, QualityStatus
from app.models.geo import GeographicCell, NoiseAggregation
from app.models.measurement import Measurement
from app.schemas.schemas import TimeFilter, OverviewOut, TimeseriesPoint, UnderDocumentedCell
from app.services.geo import period_dates, resolve_period

_TEN = literal_column("10.0")


async def overview(db: AsyncSession, tenant_id: uuid.UUID, tf: TimeFilter) -> OverviewOut:
    start, end = resolve_period(tf)
    base = [Measurement.tenant_id == tenant_id, Measurement.measured_at >= start, Measurement.measured_at < end]
    usable = [*base, Measurement.quality_status.in_([QualityStatus.validated, QualityStatus.indicative])]

    by_status = dict((await db.execute(select(Measurement.quality_status, func.count()).where(*base)
                                       .group_by(Measurement.quality_status))).all())
    by_part = dict((await db.execute(select(Measurement.day_part, func.count()).where(*usable)
                                     .group_by(Measurement.day_part))).all())
    active = (await db.execute(select(func.count(distinct(Measurement.contributor_hash))).where(*usable))).scalar_one()
    covered = (await db.execute(select(func.count(distinct(Measurement.cell_id))).where(
        *base, Measurement.quality_status == QualityStatus.validated))).scalar_one()
    total_usable, calibrated = (await db.execute(select(
        func.count(), func.sum(case((Measurement.calibration_status == CalibrationStatus.calibrated, 1), else_=0)))
        .where(*usable))).one()
    models = dict((await db.execute(select(DeviceModel.calibration_status, func.count()).where(
        DeviceModel.tenant_id == tenant_id).group_by(DeviceModel.calibration_status))).all())
    proposals = dict((await db.execute(select(CollectionProposal.status, func.count()).where(
        CollectionProposal.tenant_id == tenant_id, CollectionProposal.created_at >= start,
        CollectionProposal.created_at < end).group_by(CollectionProposal.status))).all())

    counts = (select(Measurement.cell_id, func.count().label("n")).where(
        *base, Measurement.quality_status != QualityStatus.rejected).group_by(Measurement.cell_id).subquery())
    n = func.coalesce(counts.c.n, 0)
    weak = (await db.execute(
        select(GeographicCell.h3_index, n).outerjoin(counts, counts.c.cell_id == GeographicCell.id)
        .where(GeographicCell.tenant_id == tenant_id, n < settings.TARGET_MEASUREMENTS_PER_CELL)
        .order_by(n, GeographicCell.h3_index).limit(50))).all()

    return OverviewOut(
        period_start=start, period_end=end,
        measurements_by_status={k.value: v for k, v in by_status.items()},
        measurements_by_day_part={k.value: v for k, v in by_part.items()},
        active_contributors=active, cells_covered=covered,
        calibrated_share=round(calibrated / total_usable, 3) if total_usable else None,
        device_models_by_calibration={k.value: v for k, v in models.items()},
        proposals_by_status={k.value: v for k, v in proposals.items()},
        under_documented_cells=[UnderDocumentedCell(h3_index=h, measurement_count=c) for h, c in weak])


async def timeseries(db: AsyncSession, tenant_id: uuid.UUID, tf: TimeFilter,
                     include_indicative: bool) -> list[TimeseriesPoint]:
    start, end = resolve_period(tf)
    d_from, d_to = period_dates(start, end)
    stats = [QualityStatus.validated] + ([QualityStatus.indicative] if include_indicative else [])
    conds = [NoiseAggregation.tenant_id == tenant_id, NoiseAggregation.bucket_date >= d_from,
             NoiseAggregation.bucket_date <= d_to, NoiseAggregation.quality_status.in_(stats)]
    if tf.day_part != "all":
        conds.append(NoiseAggregation.day_part == DayPart(tf.day_part))
    en, du = func.sum(NoiseAggregation.energy_sum), func.sum(NoiseAggregation.duration_total)
    rows = (await db.execute(
        select(NoiseAggregation.bucket_date, func.sum(NoiseAggregation.measurement_count),
               _TEN * func.log(en / du, type_=Float), func.max(NoiseAggregation.lmax_db))
        .where(*conds).group_by(NoiseAggregation.bucket_date).order_by(NoiseAggregation.bucket_date))).all()
    return [TimeseriesPoint(date=d, measurement_count=int(n), laeq_db=round(float(l), 1) if l is not None else None,
                            lmax_db=round(float(mx), 1) if mx is not None else None) for d, n, l, mx in rows]