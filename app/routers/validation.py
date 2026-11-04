import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.services import reviews as reviews_service
from app.core.config import settings
from app.core.deps import get_db, require_roles
from app.core.rate_limit import limit_by_user
from app.models.enums import CalibrationStatus, QualityStatus, Role
from app.models.measurement import Measurement, MeasurementQuality
from app.models.tenancy import User
from app.services.audit import audit
from app.services.geo import to_local
from app.tasks.celery_app import celery_app

router = APIRouter(prefix="/validation", tags=["validation"],
                   dependencies=[Depends(limit_by_user("validation", settings.RATE_LIMIT_API_PER_MINUTE))])
_staff = require_roles(Role.tenant_admin, Role.analyst)
_admin = require_roles(Role.tenant_admin)


class BulkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    status: Literal["validated", "indicative", "rejected"]
    reason: str = Field(min_length=3, max_length=500)


class ReleaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


def _item(m: Measurement, q: MeasurementQuality | None) -> dict:
    return {
        "id": str(m.id), "measured_at": m.measured_at, "created_at": m.created_at, "h3_index": m.h3_index,
        "day_part": m.day_part.value, "laeq_db": round(m.laeq + m.correction_db_applied, 1),
        "lmax_db": round(m.lmax + m.correction_db_applied, 1), "lmin_db": round(m.lmin + m.correction_db_applied, 1),
        "duration_seconds": m.duration_seconds, "effective_duration_seconds": m.effective_duration_seconds,
        "gps_accuracy_m": m.gps_accuracy, "saturation_ratio": m.saturation_ratio,
        "moved_distance_m": m.moved_distance_m, "sample_rate_hz": m.sample_rate_hz,
        "noise_context": m.noise_context.value if m.noise_context else None,
        "calibration_status": m.calibration_status.value, "quality_status": m.quality_status.value,
        "quality_score": q.score if q else None, "sub_scores": (q.sub_scores if q else {}) or {},
        "reasons": (q.reasons if q else []) or [], "evaluator": q.evaluator if q else None,
        "contributor_token": m.contributor_hash[:12]}


@router.get("/queue")
async def queue(
        status: list[QualityStatus] | None = Query(None), min_score: float | None = Query(None, ge=0, le=1),
        max_score: float | None = Query(None, ge=0, le=1), calibration: CalibrationStatus | None = None,
        evaluator: Literal["auto", "admin"] | None = None, reason: str | None = Query(None, max_length=60),
        h3_index: str | None = Query(None, max_length=16), date_from: date | None = None, date_to: date | None = None,
        sort: Literal["recent", "score_asc", "score_desc", "laeq_desc"] = "recent",
        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
        user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    conds = [Measurement.tenant_id == user.tenant_id]
    if status:
        conds.append(Measurement.quality_status.in_(status))
    if min_score is not None:
        conds.append(MeasurementQuality.score >= min_score)
    if max_score is not None:
        conds.append(MeasurementQuality.score <= max_score)
    if calibration:
        conds.append(Measurement.calibration_status == calibration)
    if evaluator:
        conds.append(MeasurementQuality.evaluator == evaluator)
    if reason:
        conds.append(MeasurementQuality.reasons.contains([{"code": reason}]))
    if h3_index:
        conds.append(Measurement.h3_index == h3_index)
    if date_from:
        conds.append(Measurement.local_date >= date_from)
    if date_to:
        conds.append(Measurement.local_date <= date_to)

    join = Measurement.id == MeasurementQuality.measurement_id
    total = (await db.execute(select(func.count()).select_from(Measurement)
                              .outerjoin(MeasurementQuality, join).where(*conds))).scalar_one()
    order = {"recent": Measurement.measured_at.desc(), "score_asc": MeasurementQuality.score.asc().nulls_last(),
             "score_desc": MeasurementQuality.score.desc().nulls_last(), "laeq_desc": Measurement.laeq.desc()}[sort]
    rows = (await db.execute(select(Measurement, MeasurementQuality).outerjoin(MeasurementQuality, join)
                             .where(*conds).order_by(order, Measurement.id).limit(limit).offset(offset))).all()
    return {"items": [_item(m, q) for m, q in rows], "total": total, "limit": limit, "offset": offset}


@router.post("/bulk")
async def bulk(payload: BulkIn, request: Request, admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    status = QualityStatus(payload.status)
    now = datetime.now(timezone.utc)
    ms = (await db.execute(select(Measurement).where(
        Measurement.tenant_id == admin.tenant_id, Measurement.id.in_(payload.ids)).with_for_update())).scalars().all()
    qs = {q.measurement_id: q for q in (await db.execute(select(MeasurementQuality).where(
        MeasurementQuality.measurement_id.in_([m.id for m in ms])))).scalars()} if ms else {}
    for m in ms:
        q = qs.get(m.id)
        if q is None:
            q = MeasurementQuality(tenant_id=m.tenant_id, measurement_id=m.id, status=status, score=0.0,
                                   sub_scores={}, reasons=[], evaluator="admin", evaluated_at=now)
            db.add(q)
        q.status, q.evaluator, q.override_by, q.evaluated_at = status, "admin", admin.id, now
        q.reasons = [*(q.reasons or []), {"code": "admin_override", "severity": "info", "reason": payload.reason}]
        m.quality_status, m.aggregated_at = status, None            # force le recalcul des agrégats
    await reviews_service.cancel_rounds(db, [m.id for m in ms])
    await audit(db, admin, "validation.bulk", "measurement", None,
                {"count": len(ms), "status": payload.status, "reason": payload.reason}, request)
    await db.commit()
    return {"updated": len(ms), "skipped": len(payload.ids) - len(ms)}


@router.post("/release")
async def release(payload: ReleaseIn, request: Request, admin: User = Depends(_admin),
                  db: AsyncSession = Depends(get_db)):
    """Annule la décision admin : la mesure repasse en évaluation automatique."""
    ms = (await db.execute(select(Measurement).where(
        Measurement.tenant_id == admin.tenant_id, Measurement.id.in_(payload.ids)).with_for_update())).scalars().all()
    qs = (await db.execute(select(MeasurementQuality).where(
        MeasurementQuality.measurement_id.in_([m.id for m in ms])))).scalars().all() if ms else []
    for q in qs:
        q.evaluator, q.override_by = "auto", None
    for m in ms:
        m.quality_status, m.aggregated_at = QualityStatus.pending, None
    await audit(db, admin, "validation.release", "measurement", None, {"count": len(ms)}, request)
    await db.commit()
    for m in ms:
        celery_app.send_task("measurements.evaluate", args=[str(admin.tenant_id), str(m.id)])
    return {"released": len(ms)}


@router.get("/stats")
async def stats(days: int = Query(14, ge=1, le=90), user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    since = to_local(now).date() - timedelta(days=days - 1)
    per_day: dict = {}
    totals = {s.value: 0 for s in QualityStatus}
    for d, st, n in (await db.execute(
            select(Measurement.local_date, Measurement.quality_status, func.count()).where(
                Measurement.tenant_id == user.tenant_id, Measurement.local_date >= since)
            .group_by(Measurement.local_date, Measurement.quality_status))).all():
        per_day.setdefault(d.isoformat(), {s.value: 0 for s in QualityStatus})[st.value] = n
        totals[st.value] += n
    stuck = (await db.execute(select(func.count()).select_from(Measurement).where(
        Measurement.tenant_id == user.tenant_id, Measurement.quality_status == QualityStatus.pending,
        Measurement.created_at < now - timedelta(minutes=5)))).scalar_one()
    overrides = (await db.execute(select(func.count()).select_from(MeasurementQuality).where(
        MeasurementQuality.tenant_id == user.tenant_id, MeasurementQuality.evaluator == "admin",
        MeasurementQuality.evaluated_at >= now - timedelta(days=days)))).scalar_one()
    reasons = (await db.execute(text(
        "SELECT r->>'code' AS code, count(*) AS n FROM measurement_quality q, jsonb_array_elements(q.reasons) r "
        "WHERE q.tenant_id = :tid AND q.evaluated_at >= :since GROUP BY 1 ORDER BY 2 DESC LIMIT 15"),
        {"tid": user.tenant_id, "since": now - timedelta(days=days)})).all()
    return {"days": [{"date": d, **v} for d, v in sorted(per_day.items())], "totals": totals,
            "pending_stuck": stuck, "admin_overrides": overrides,
            "top_reasons": [{"code": c, "n": int(n)} for c, n in reasons]}