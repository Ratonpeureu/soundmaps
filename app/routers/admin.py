import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db, require_roles
from app.core.rate_limit import limit_by_user
from app.models.devices import DeviceCalibration, DeviceModel
from app.models.enums import CalibrationStatus, QualityStatus, Role
from app.models.geo import GeographicCell
from app.models.measurement import Measurement, MeasurementQuality
from app.models.tenancy import AuditLog, Tenant, User
from app.schemas.schemas import (AdminMeasurementOut, AdminUserOut, AdminUserUpdate, AlgorithmStateOut, AuditLogOut,
                               CalibrationCreate, CalibrationOut, CellOut, DeviceModelCreate, DeviceModelOut,
                               OverrideRequest, QualitySummaryOut, SettingsOverrides)
from app.schemas.common import Msg, Page
from app.models.collection import TenantAlgorithmState
from app.services import collection as collection_service
from app.services.audit import audit
from app.tasks.celery_app import celery_app

_admin = require_roles(Role.tenant_admin)
router = APIRouter(prefix="/admin", tags=["admin"],
                   dependencies=[Depends(limit_by_user("admin", settings.RATE_LIMIT_API_PER_MINUTE))])


# ----- utilisateurs
@router.get("/users", response_model=Page[AdminUserOut])
async def users(role: Role | None = None, is_active: bool | None = None, limit: int = Query(50, ge=1, le=200),
                offset: int = Query(0, ge=0), admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    conds = [User.tenant_id == admin.tenant_id]
    if role:
        conds.append(User.role == role)
    if is_active is not None:
        conds.append(User.is_active == is_active)
    total = (await db.execute(select(func.count()).select_from(User).where(*conds))).scalar_one()
    rows = (await db.execute(select(User).where(*conds).order_by(User.created_at.desc())
                             .limit(limit).offset(offset))).scalars().all()
    return Page(items=rows, total=total, limit=limit, offset=offset)


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(user_id: uuid.UUID, payload: AdminUserUpdate, request: Request,
                      admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "user_not_found")
    if target.id == admin.id:
        raise HTTPException(409, "cannot_modify_self")
    changes = payload.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(target, k, v)
    await audit(db, admin, "user.update", "user", target.id, {k: str(v) for k, v in changes.items()}, request)
    await db.commit()
    return target


# ----- mesures (vue anonymisée : jamais de coordonnées ni d'identité)
@router.get("/measurements", response_model=Page[AdminMeasurementOut])
async def measurements(status: QualityStatus | None = None, h3_index: str | None = None,
                       limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                       admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    conds = [Measurement.tenant_id == admin.tenant_id]
    if status:
        conds.append(Measurement.quality_status == status)
    if h3_index:
        conds.append(Measurement.h3_index == h3_index)
    total = (await db.execute(select(func.count()).select_from(Measurement).where(*conds))).scalar_one()
    rows = (await db.execute(
        select(Measurement, MeasurementQuality)
        .outerjoin(MeasurementQuality, MeasurementQuality.measurement_id == Measurement.id)
        .where(*conds).order_by(Measurement.measured_at.desc()).limit(limit).offset(offset))).all()
    items = [AdminMeasurementOut(
        id=m.id, measured_at=m.measured_at, h3_index=m.h3_index, day_part=m.day_part,
        laeq_db=round(m.laeq + m.correction_db_applied, 1), lmax_db=round(m.lmax + m.correction_db_applied, 1),
        calibration_status=m.calibration_status, quality_status=m.quality_status,
        quality_score=q.score if q else None, reason_codes=[r["code"] for r in (q.reasons if q else [])],
        evaluator=q.evaluator if q else None, contributor_token=m.contributor_hash[:12],
        noise_context=m.noise_context) for m, q in rows]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.post("/measurements/{measurement_id}/override", response_model=Msg)
async def override(measurement_id: uuid.UUID, payload: OverrideRequest, request: Request,
                   admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    m = await db.get(Measurement, measurement_id, with_for_update=True)
    if m is None:
        raise HTTPException(404, "measurement_not_found")
    status = QualityStatus(payload.status)
    now = datetime.now(timezone.utc)
    q = (await db.execute(select(MeasurementQuality).where(MeasurementQuality.measurement_id == m.id))).scalar_one_or_none()
    if q is None:
        q = MeasurementQuality(tenant_id=m.tenant_id, measurement_id=m.id, status=status, score=0.0,
                               sub_scores={}, reasons=[], evaluator="admin", evaluated_at=now)
        db.add(q)
    q.status, q.evaluator, q.override_by, q.evaluated_at = status, "admin", admin.id, now
    q.reasons = [*(q.reasons or []), {"code": "admin_override", "severity": "info", "reason": payload.reason}]
    m.quality_status, m.aggregated_at = status, None
    await audit(db, admin, "measurement.override", "measurement", m.id, {"status": payload.status}, request)
    await db.commit()
    return Msg(detail="overridden")


# ----- appareils et calibration
@router.get("/device-models", response_model=list[DeviceModelOut])
async def device_models(admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(DeviceModel).where(DeviceModel.tenant_id == admin.tenant_id)
                             .order_by(DeviceModel.manufacturer, DeviceModel.model))).scalars().all()


@router.post("/device-models", response_model=DeviceModelOut, status_code=201)
async def create_device_model(payload: DeviceModelCreate, request: Request,
                              admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    dm = DeviceModel(tenant_id=admin.tenant_id, **payload.model_dump())
    db.add(dm)
    try:
        await audit(db, admin, "device_model.create", "device_model", dm.id, request=request)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "device_model_exists")
    return dm


@router.get("/device-models/{model_id}/calibrations", response_model=list[CalibrationOut])
async def calibrations(model_id: uuid.UUID, admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(DeviceCalibration).where(DeviceCalibration.device_model_id == model_id)
                             .order_by(DeviceCalibration.created_at.desc()))).scalars().all()


@router.post("/device-models/{model_id}/calibrations", response_model=CalibrationOut, status_code=201)
async def add_calibration(model_id: uuid.UUID, payload: CalibrationCreate, request: Request,
                          admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    dm = await db.get(DeviceModel, model_id, with_for_update=True)
    if dm is None:
        raise HTTPException(404, "device_model_not_found")
    await db.execute(update(DeviceCalibration).where(DeviceCalibration.device_model_id == model_id)
                     .values(is_active=False))
    cal = DeviceCalibration(tenant_id=admin.tenant_id, device_model_id=model_id, calibrated_by=admin.id,
                            is_active=True, **payload.model_dump())
    db.add(cal)
    dm.calibration_status, dm.correction_db = CalibrationStatus.calibrated, payload.correction_db
    await audit(db, admin, "device_model.calibrate", "device_model", model_id,
                {"correction_db": payload.correction_db}, request)
    await db.commit()
    celery_app.send_task("measurements.reevaluate_model", args=[str(admin.tenant_id), str(model_id)])
    return cal


@router.post("/device-models/{model_id}/uncalibrate", response_model=Msg)
async def uncalibrate(model_id: uuid.UUID, request: Request, admin: User = Depends(_admin),
                      db: AsyncSession = Depends(get_db)):
    dm = await db.get(DeviceModel, model_id, with_for_update=True)
    if dm is None:
        raise HTTPException(404, "device_model_not_found")
    await db.execute(update(DeviceCalibration).where(DeviceCalibration.device_model_id == model_id)
                     .values(is_active=False))
    dm.calibration_status, dm.correction_db = CalibrationStatus.uncalibrated, None
    await audit(db, admin, "device_model.uncalibrate", "device_model", model_id, request=request)
    await db.commit()
    celery_app.send_task("measurements.reevaluate_model", args=[str(admin.tenant_id), str(model_id)])
    return Msg(detail="uncalibrated")


# ----- zones
@router.get("/cells", response_model=Page[CellOut])
async def cells(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    total = (await db.execute(select(func.count()).select_from(GeographicCell).where(
        GeographicCell.tenant_id == admin.tenant_id))).scalar_one()
    rows = (await db.execute(
        select(GeographicCell.h3_index, func.ST_Y(GeographicCell.center), func.ST_X(GeographicCell.center),
               GeographicCell.last_measurement_at).where(GeographicCell.tenant_id == admin.tenant_id)
        .order_by(GeographicCell.last_measurement_at.desc().nulls_last()).limit(limit).offset(offset))).all()
    return Page(items=[CellOut(h3_index=h, center_lat=la, center_lon=lo, last_measurement_at=last)
                       for h, la, lo, last in rows], total=total, limit=limit, offset=offset)


# ----- algorithme de collecte
@router.get("/collection/algorithm", response_model=AlgorithmStateOut)
async def algorithm(admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    state = await db.get(TenantAlgorithmState, admin.tenant_id)
    return AlgorithmStateOut(weights=await collection_service.get_weights(db, admin.tenant_id),
                             base_weights=collection_service.base_weights(),
                             samples_used=state.samples_used if state else 0,
                             updated_at=state.updated_at if state else None)


@router.post("/collection/algorithm/reset", response_model=Msg)
async def reset_algorithm(request: Request, admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    await collection_service.reset_weights(db, admin.tenant_id)
    await audit(db, admin, "algorithm.reset", "algorithm", admin.tenant_id, request=request)
    await db.commit()
    return Msg(detail="weights_reset")


# ----- qualité
@router.get("/quality/summary", response_model=QualitySummaryOut)
async def quality_summary(admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    by_status = dict((await db.execute(select(MeasurementQuality.status, func.count()).where(
        MeasurementQuality.tenant_id == admin.tenant_id).group_by(MeasurementQuality.status))).all())
    reasons = (await db.execute(text(
        "SELECT r->>'code' AS code, count(*) AS n FROM measurement_quality q, "
        "jsonb_array_elements(q.reasons) r WHERE q.tenant_id = :tid GROUP BY 1 ORDER BY 2 DESC LIMIT 20"),
        {"tid": admin.tenant_id})).all()
    return QualitySummaryOut(by_status={k.value: v for k, v in by_status.items()},
                             top_reason_codes={code: n for code, n in reasons})


# ----- paramètres du tenant
@router.get("/settings", response_model=SettingsOverrides)
async def get_settings(admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    tenant = await db.get(Tenant, admin.tenant_id)
    return SettingsOverrides(**(tenant.settings or {}))


@router.put("/settings", response_model=SettingsOverrides)
async def put_settings(payload: SettingsOverrides, request: Request, admin: User = Depends(_admin),
                       db: AsyncSession = Depends(get_db)):
    tenant = await db.get(Tenant, admin.tenant_id)
    values = payload.model_dump(exclude_none=True)
    from app.services.thresholds import build_thresholds
    th = build_thresholds(values)
    if th.indicative_min_score > th.validated_min_score:
        raise HTTPException(422, "indicative_min_score_must_be_lte_validated_min_score")
    tenant.settings = values
    await audit(db, admin, "settings.update", "tenant", tenant.id, values, request)
    await db.commit()
    return payload


# ----- journal d'audit
@router.get("/audit-logs", response_model=Page[AuditLogOut])
async def audit_logs(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                     admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    total = (await db.execute(select(func.count()).select_from(AuditLog).where(
        AuditLog.tenant_id == admin.tenant_id))).scalar_one()
    rows = (await db.execute(select(AuditLog).where(AuditLog.tenant_id == admin.tenant_id)
                             .order_by(AuditLog.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return Page(items=rows, total=total, limit=limit, offset=offset)