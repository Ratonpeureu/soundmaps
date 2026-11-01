import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.devices import DeviceModel
from app.models.enums import CalibrationStatus, QualityStatus
from app.models.geo import GeographicCell
from app.models.measurement import Measurement, MeasurementQuality
from app.models.tenancy import Tenant
from app.services.acoustics import clamp01
from app.services.geo import ring
from app.services.thresholds import Thresholds, build_thresholds


@dataclass(frozen=True)
class MeasurementFacts:
    laeq: float
    lmax: float
    lmin: float
    gps_accuracy_m: float
    duration_seconds: float
    effective_duration_seconds: float
    saturation_ratio: float
    moved_distance_m: float
    measured_at: datetime
    calibrated: bool
    correction_db: float


@dataclass
class QualityResult:
    status: QualityStatus
    score: float
    sub_scores: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)


def evaluate(facts: MeasurementFacts, neighbor_levels: list[float], th: Thresholds,
             now: datetime | None = None) -> QualityResult:
    """Algorithme B : décide si une mesure est assez fiable pour alimenter la carte (fonction pure)."""
    now = now or datetime.now(timezone.utc)
    reasons: list[dict] = []
    hard = False
    sub: dict[str, float] = {}

    def reject(code: str, **detail):
        nonlocal hard
        hard = True
        reasons.append({"code": code, "severity": "reject", **detail})

    corrected = facts.laeq + facts.correction_db
    if not (th.laeq_min_db <= corrected <= th.laeq_max_db):
        reject("level_out_of_range", value=round(corrected, 1))
    tol = th.level_tolerance_db
    if not (facts.lmin - tol <= facts.laeq <= facts.lmax + tol):
        reject("inconsistent_levels")

    if facts.measured_at > now + timedelta(seconds=th.future_tolerance_s):
        reject("timestamp_in_future")
    if now - facts.measured_at > timedelta(hours=th.max_age_hours):
        reject("measurement_too_old")

    # GPS
    if facts.gps_accuracy_m > th.gps_reject_m:
        reject("gps_accuracy_too_low", value=facts.gps_accuracy_m)
        sub["gps"] = 0.0
    elif facts.gps_accuracy_m <= th.gps_good_m:
        sub["gps"] = 1.0
    else:
        sub["gps"] = clamp01(1 - (facts.gps_accuracy_m - th.gps_good_m) / (th.gps_reject_m - th.gps_good_m))

    # Durée effective
    ratio = facts.effective_duration_seconds / facts.duration_seconds if facts.duration_seconds > 0 else 0.0
    if ratio < th.min_duration_ratio:
        reject("capture_too_short", ratio=round(ratio, 3))
        sub["duration"] = 0.0
    else:
        sub["duration"] = 1.0

    # Saturation
    if facts.saturation_ratio > th.max_saturation_ratio:
        reject("audio_saturation", value=facts.saturation_ratio)
        sub["saturation"] = 0.0
    else:
        sub["saturation"] = clamp01(1 - facts.saturation_ratio / th.max_saturation_ratio)

    # Déplacement pendant la capture
    if facts.moved_distance_m > th.max_movement_m:
        reject("moved_during_capture", value=facts.moved_distance_m)
        sub["movement"] = 0.0
    else:
        sub["movement"] = clamp01(1 - facts.moved_distance_m / th.max_movement_m)

    # Calibration
    sub["calibration"] = 1.0 if facts.calibrated else th.uncalibrated_subscore
    if not facts.calibrated:
        reasons.append({"code": "uncalibrated_device", "severity": "info"})

    # Cohérence avec les voisines (z-score robuste médiane / MAD)
    outlier = False
    if len(neighbor_levels) >= th.neighbor_min_samples:
        med = statistics.median(neighbor_levels)
        mad = statistics.median(abs(v - med) for v in neighbor_levels)
        sigma = max(1.4826 * mad, th.neighbor_sigma_floor_db)
        z = abs(corrected - med) / sigma
        if z > th.neighbor_z_max:
            outlier = True
            sub["neighbor"] = clamp01(0.5 - 0.1 * (z - th.neighbor_z_max))
            reasons.append({"code": "neighbor_outlier", "severity": "warn", "z": round(z, 2)})
        else:
            sub["neighbor"] = 1.0 - 0.5 * (z / th.neighbor_z_max)
    else:
        sub["neighbor"] = th.neighbor_neutral
        reasons.append({"code": "insufficient_neighbors", "severity": "info", "count": len(neighbor_levels)})

    score = sum(th.weights[k] * sub[k] for k in th.weights)
    cap_indicative = outlier or (not facts.calibrated and not th.allow_uncalibrated_validated)
    if hard:
        status, score = QualityStatus.rejected, 0.0
    elif score >= th.validated_min_score and not cap_indicative:
        status = QualityStatus.validated
    elif score >= th.indicative_min_score:
        status = QualityStatus.indicative
    else:
        status = QualityStatus.rejected
        reasons.append({"code": "low_score", "severity": "reject", "score": round(score, 3)})
    return QualityResult(status=status, score=round(score, 4), sub_scores={k: round(v, 3) for k, v in sub.items()},
                         reasons=reasons)


async def _neighbor_levels(db: AsyncSession, m: Measurement, th: Thresholds) -> list[float]:
    window = timedelta(days=th.neighbor_window_days)
    q = select(Measurement.laeq + Measurement.correction_db_applied).where(
        Measurement.tenant_id == m.tenant_id, Measurement.h3_index.in_(ring(m.h3_index, 1)),
        Measurement.day_part == m.day_part, Measurement.quality_status == QualityStatus.validated,
        Measurement.measured_at >= m.measured_at - window, Measurement.measured_at <= m.measured_at + window,
        Measurement.id != m.id).limit(500)
    return [float(v) for v in (await db.execute(q)).scalars()]


async def evaluate_measurement(db: AsyncSession, tenant_id: uuid.UUID, measurement_id: uuid.UUID,
                               force: bool = False) -> QualityStatus | None:
    m = await db.get(Measurement, measurement_id, with_for_update=True)
    if m is None or m.tenant_id != tenant_id:
        return None
    existing = (await db.execute(select(MeasurementQuality).where(
        MeasurementQuality.measurement_id == m.id))).scalar_one_or_none()
    if existing is not None and existing.evaluator == "admin":
        return existing.status                       # une décision admin n'est jamais écrasée
    if existing is not None and not force and m.quality_status != QualityStatus.pending:
        pass
    tenant = await db.get(Tenant, tenant_id)
    th = build_thresholds(tenant.settings if tenant else None)

    correction, calibrated = 0.0, False
    if m.device_model_id:
        dm = await db.get(DeviceModel, m.device_model_id)
        if dm is not None and dm.calibration_status == CalibrationStatus.calibrated and dm.correction_db is not None:
            correction, calibrated = float(dm.correction_db), True

    facts = MeasurementFacts(m.laeq, m.lmax, m.lmin, m.gps_accuracy, m.duration_seconds,
                             m.effective_duration_seconds, m.saturation_ratio, m.moved_distance_m,
                             m.measured_at, calibrated, correction)
    result = evaluate(facts, await _neighbor_levels(db, m, th), th)
    now = datetime.now(timezone.utc)

    await db.execute(pg_insert(MeasurementQuality).values(
        id=uuid.uuid4(), tenant_id=tenant_id, measurement_id=m.id, status=result.status, score=result.score,
        sub_scores=result.sub_scores, reasons=result.reasons, evaluator="auto", evaluated_at=now,
    ).on_conflict_do_update(index_elements=["measurement_id"], set_={
        "status": result.status, "score": result.score, "sub_scores": result.sub_scores,
        "reasons": result.reasons, "evaluator": "auto", "override_by": None, "evaluated_at": now}))

    m.correction_db_applied = correction
    m.calibration_status = CalibrationStatus.calibrated if calibrated else CalibrationStatus.uncalibrated
    m.quality_status = result.status
    m.aggregated_at = None                           # force le recalcul des agrégats concernés
    if result.status != QualityStatus.rejected:
        await db.execute(update(GeographicCell).where(GeographicCell.id == m.cell_id).values(
            last_measurement_at=func.greatest(func.coalesce(GeographicCell.last_measurement_at, m.measured_at),
                                              m.measured_at)))
    return result.status