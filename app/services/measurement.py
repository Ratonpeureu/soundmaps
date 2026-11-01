import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import contributor_token
from app.models.collection import CollectionProposal
from app.models.devices import Device, DeviceModel
from app.models.enums import ProposalStatus, QualityStatus
from app.models.measurement import Measurement, MeasurementQuality
from app.models.tenancy import User
from app.schemas.schemas import (BatchItemResult, BatchResult, HistoryItem, MeasurementAck,
                                      MeasurementBatch, MeasurementCreate)
from app.services.geo import cell_for, day_part_of, get_or_create_cell, to_local
from app.services.user import require_consents
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


def _enqueue_evaluation(tenant_id: uuid.UUID, measurement_id: uuid.UUID) -> None:
    try:
        celery_app.send_task("measurements.evaluate", args=[str(tenant_id), str(measurement_id)])
    except Exception:
        log.exception("enqueue_failed", extra={"tenant_id": str(tenant_id)})   # le balayeur reprendra la mesure


async def _find_existing(db: AsyncSession, user: User, client_id: uuid.UUID) -> Measurement | None:
    return (await db.execute(select(Measurement).where(
        Measurement.tenant_id == user.tenant_id, Measurement.user_id == user.id,
        Measurement.client_measurement_id == client_id))).scalar_one_or_none()


async def ingest(db: AsyncSession, user: User, p: MeasurementCreate) -> MeasurementAck:
    existing = await _find_existing(db, user, p.client_measurement_id)
    if existing is not None:
        return MeasurementAck(id=existing.id, client_measurement_id=p.client_measurement_id,
                              quality_status=existing.quality_status, duplicate=True)
    if user.participation_paused:
        raise HTTPException(403, "participation_paused")
    await require_consents(db, user)

    device = await db.get(Device, p.device_id)
    if device is None or device.user_id != user.id:
        raise HTTPException(404, "device_not_found")

    proposal = None
    if p.proposal_id is not None:
        proposal = await db.get(CollectionProposal, p.proposal_id, with_for_update=True)
        if proposal is None or proposal.user_id != user.id or proposal.status != ProposalStatus.accepted:
            raise HTTPException(409, "proposal_not_accepted")

    local = to_local(p.measured_at)
    h3_index = cell_for(p.latitude, p.longitude)
    cell = await get_or_create_cell(db, user.tenant_id, h3_index)
    m = Measurement(
        id=uuid.uuid4(), tenant_id=user.tenant_id, user_id=user.id, device_id=device.id,
        device_model_id=device.device_model_id, proposal_id=p.proposal_id, cell_id=cell.id,
        client_measurement_id=p.client_measurement_id,
        contributor_hash=contributor_token(user.tenant_id, user.id, f"{local:%Y-%m}"),
        measured_at=p.measured_at, local_date=local.date(), day_part=day_part_of(local),
        latitude=p.latitude, longitude=p.longitude,
        geom=func.ST_SetSRID(func.ST_MakePoint(p.longitude, p.latitude), 4326),
        h3_index=h3_index, gps_accuracy=p.gps_accuracy_m,
        duration_seconds=p.duration_seconds, effective_duration_seconds=p.effective_duration_seconds,
        sample_rate_hz=p.sample_rate_hz, laeq=p.laeq, lmax=p.lmax, lmin=p.lmin,
        saturation_ratio=p.saturation_ratio, moved_distance_m=p.moved_distance_m,
        noise_context=p.noise_context, audio_retained=False)
    db.add(m)
    if proposal is not None:
        proposal.status = ProposalStatus.completed
        proposal.completed_at = datetime.now(timezone.utc)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await _find_existing(db, user, p.client_measurement_id)
        if existing is None:
            raise HTTPException(409, "conflict")
        return MeasurementAck(id=existing.id, client_measurement_id=p.client_measurement_id,
                              quality_status=existing.quality_status, duplicate=True)
    _enqueue_evaluation(user.tenant_id, m.id)
    return MeasurementAck(id=m.id, client_measurement_id=p.client_measurement_id,
                          quality_status=QualityStatus.pending, duplicate=False)


async def ingest_batch(db: AsyncSession, user: User, batch: MeasurementBatch) -> BatchResult:
    results: list[BatchItemResult] = []
    for item in batch.items:
        try:
            ack = await ingest(db, user, item)
            results.append(BatchItemResult(client_measurement_id=item.client_measurement_id,
                                           result="duplicate" if ack.duplicate else "accepted", id=ack.id))
        except HTTPException as exc:
            await db.rollback()
            results.append(BatchItemResult(client_measurement_id=item.client_measurement_id,
                                           result="error", error=str(exc.detail)))
    return BatchResult(results=results)


async def history(db: AsyncSession, user: User, limit: int, offset: int) -> list[HistoryItem]:
    rows = (await db.execute(
        select(Measurement, MeasurementQuality)
        .outerjoin(MeasurementQuality, MeasurementQuality.measurement_id == Measurement.id)
        .where(Measurement.tenant_id == user.tenant_id, Measurement.user_id == user.id)
        .order_by(Measurement.measured_at.desc()).limit(limit).offset(offset))).all()
    return [HistoryItem(
        id=m.id, measured_at=m.measured_at, h3_index=m.h3_index,
        laeq_db=round(m.laeq + m.correction_db_applied, 1), lmax_db=round(m.lmax + m.correction_db_applied, 1),
        day_part=m.day_part, calibration_status=m.calibration_status, quality_status=m.quality_status,
        quality_score=q.score if q else None, reason_codes=[r["code"] for r in (q.reasons if q else [])],
        noise_context=m.noise_context, location_anonymized=m.location_anonymized) for m, q in rows]