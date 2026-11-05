import math
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import Float, case, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
import uuid
from sqlalchemy import or_
from app.models.measurement import Measurement, MeasurementQuality
from app.core.config import settings
from app.models.enums import CalibrationStatus, DayPart, QualityStatus
from app.models.geo import GeographicCell, NoiseAggregation
from app.models.measurement import Measurement
from app.schemas.schemas import TimeFilter
from app.services.acoustics import confidence_index
from app.services.geo import (cell_center, cell_margin_degrees, cell_polygon, is_valid_cell, period_dates,
                              resolve_period)

_TEN = literal_column("10.0")


def statuses(include_indicative: bool) -> list[QualityStatus]:
    return [QualityStatus.validated] + ([QualityStatus.indicative] if include_indicative else [])


def confidence_of(count: int, devices: int, calibrated: int, last: datetime | None, now: datetime) -> float:
    hours = (now - last).total_seconds() / 3600 if last else 1e9
    return round(confidence_index(
        count, devices, (calibrated / count) if count else 0.0, hours,
        target_count=settings.CONF_TARGET_MEASUREMENTS, target_devices=settings.CONF_TARGET_DEVICES,
        recency_tau_hours=settings.CONF_RECENCY_TAU_HOURS, w_count=settings.CONF_W_COUNT,
        w_devices=settings.CONF_W_DEVICES, w_calibration=settings.CONF_W_CALIBRATION,
        w_recency=settings.CONF_W_RECENCY), 3)


def _agg_cols():
    corrected = Measurement.laeq + Measurement.correction_db_applied
    energy = func.sum(Measurement.duration_seconds * func.power(_TEN, corrected / _TEN))
    duration = func.sum(Measurement.duration_seconds)
    return {
        "n": func.count(Measurement.id),
        "devices": func.count(func.distinct(Measurement.contributor_hash)),
        "laeq": _TEN * func.log(energy / duration, type_=Float),
        "lmax": func.max(Measurement.lmax + Measurement.correction_db_applied),
        "last": func.max(Measurement.measured_at),
        "calibrated": func.sum(case((Measurement.calibration_status == CalibrationStatus.calibrated, 1), else_=0)),
    }


def _conditions(tenant_id, start, end, day_part: str, stats: list[QualityStatus]):
    conds = [Measurement.tenant_id == tenant_id, Measurement.measured_at >= start, Measurement.measured_at < end,
             Measurement.quality_status.in_(stats)]
    if day_part != "all":
        conds.append(Measurement.day_part == DayPart(day_part))
    return conds


async def cell_rows(db: AsyncSession, tenant_id: uuid.UUID, tf: TimeFilter, include_indicative: bool,
                    min_devices: int, bbox: tuple[float, float, float, float] | None = None) -> list[dict]:
    start, end = resolve_period(tf)
    cols = _agg_cols()
    stmt = (select(Measurement.h3_index, *[c.label(k) for k, c in cols.items()])
            .where(*_conditions(tenant_id, start, end, tf.day_part, statuses(include_indicative)))
            .group_by(Measurement.h3_index).limit(settings.MAP_MAX_CELLS))
    if bbox:
        m = cell_margin_degrees()
        stmt = stmt.join(GeographicCell, GeographicCell.id == Measurement.cell_id).where(
            func.ST_Intersects(GeographicCell.center, func.ST_MakeEnvelope(
                bbox[0] - m, bbox[1] - m, bbox[2] + m, bbox[3] + m, 4326)))
    #if min_devices > 1:
    #    stmt = stmt.having(cols["devices"] >= min_devices)

    if min_devices > 1:
        stmt = stmt.outerjoin(MeasurementQuality, MeasurementQuality.measurement_id == Measurement.id)
        admin_ok = func.bool_or(MeasurementQuality.evaluator == 'admin')
        stmt = stmt.having(or_(cols["devices"] >= min_devices, admin_ok))
        
    now = datetime.now(timezone.utc)
    out = []
    for r in (await db.execute(stmt)).all():
        out.append({"h3_index": r.h3_index, "laeq_db": round(float(r.laeq), 1), "lmax_db": round(float(r.lmax), 1),
                    "measurement_count": r.n, "device_count": r.devices, "last_measurement_at": r.last,
                    "calibrated_share": round((r.calibrated or 0) / r.n, 3),
                    "confidence": confidence_of(r.n, r.devices, int(r.calibrated or 0), r.last, now)})
    return out


async def noise_geojson(db: AsyncSession, tenant_id: uuid.UUID, tf: TimeFilter, include_indicative: bool,
                        min_confidence: float, min_devices: int, bbox) -> dict:
    rows = await cell_rows(db, tenant_id, tf, include_indicative, min_devices, bbox)
    features = [{
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [cell_polygon(r["h3_index"])]},
        "properties": {**r, "last_measurement_at": r["last_measurement_at"].isoformat()},
    } for r in rows if r["confidence"] >= min_confidence]
    return {"type": "FeatureCollection", "features": features}


async def cell_detail(db: AsyncSession, tenant_id: uuid.UUID, h3_index: str, tf: TimeFilter,
                      include_indicative: bool, min_devices: int) -> dict | None:
    if not is_valid_cell(h3_index):
        raise HTTPException(422, "invalid_h3_index")
    start, end = resolve_period(tf)
    stats = statuses(include_indicative)
    cols = _agg_cols()
    r = (await db.execute(
        select(*[c.label(k) for k, c in cols.items()]).where(
            Measurement.h3_index == h3_index, *_conditions(tenant_id, start, end, tf.day_part, stats)))).one()
    if not r.n or r.devices < min_devices:
        return None
    now = datetime.now(timezone.utc)

    ctx_rows = (await db.execute(
        select(Measurement.noise_context, func.count()).where(
            Measurement.h3_index == h3_index, *_conditions(tenant_id, start, end, tf.day_part, stats),
            Measurement.noise_context.is_not(None)).group_by(Measurement.noise_context))).all()

    d_from, d_to = period_dates(start, end)
    cell_ids = select(GeographicCell.id).where(GeographicCell.tenant_id == tenant_id,
                                               GeographicCell.h3_index == h3_index).scalar_subquery()
    agg_conds = [NoiseAggregation.tenant_id == tenant_id, NoiseAggregation.cell_id == cell_ids,
                 NoiseAggregation.bucket_date >= d_from, NoiseAggregation.bucket_date <= d_to,
                 NoiseAggregation.quality_status.in_(stats)]
    if tf.day_part != "all":
        agg_conds.append(NoiseAggregation.day_part == DayPart(tf.day_part))
    en, du = func.sum(NoiseAggregation.energy_sum), func.sum(NoiseAggregation.duration_total)
    daily = (await db.execute(
        select(NoiseAggregation.bucket_date, _TEN * func.log(en / du, type_=Float), func.max(NoiseAggregation.lmax_db),
               func.sum(NoiseAggregation.measurement_count)).where(*agg_conds)
        .group_by(NoiseAggregation.bucket_date).order_by(NoiseAggregation.bucket_date))).all()
    parts = (await db.execute(
        select(NoiseAggregation.day_part, _TEN * func.log(en / du, type_=Float),
               func.sum(NoiseAggregation.measurement_count)).where(*agg_conds)
        .group_by(NoiseAggregation.day_part))).all()

    lat, lon = cell_center(h3_index)
    return {
        "h3_index": h3_index, "center": {"lat": lat, "lon": lon},
        "polygon": cell_polygon(h3_index),
        "summary": {"laeq_db": round(float(r.laeq), 1), "lmax_db": round(float(r.lmax), 1),
                    "measurement_count": r.n, "device_count": r.devices, "last_measurement_at": r.last,
                    "calibrated_share": round((r.calibrated or 0) / r.n, 3),
                    "confidence": confidence_of(r.n, r.devices, int(r.calibrated or 0), r.last, now)},
        "by_day_part": [{"day_part": p.value, "laeq_db": round(float(l), 1), "measurement_count": int(n)}
                        for p, l, n in parts],
        "daily": [{"date": d.isoformat(), "laeq_db": round(float(l), 1), "lmax_db": round(float(mx), 1),
                   "measurement_count": int(n)} for d, l, mx, n in daily],
        "context_breakdown": {c.value: n for c, n in ctx_rows},
        "period": {"from": d_from.isoformat(), "to": d_to.isoformat(), "day_part": tf.day_part,
                   "include_indicative": include_indicative},
    }