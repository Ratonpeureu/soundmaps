import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.models.enums import QualityStatus
from app.models.measurement import Measurement
from app.services import quality as quality_service
from app.tasks.celery_app import celery_app
from app.tasks.runtime import for_each_tenant, run, task_db


@celery_app.task(name="measurements.evaluate", autoretry_for=(Exception,), retry_backoff=True,
                 retry_kwargs={"max_retries": 5})
def evaluate(tenant_id: str, measurement_id: str):
    async def _job():
        tid = uuid.UUID(tenant_id)
        async with task_db(tid) as db:
            status = await quality_service.evaluate_measurement(db, tid, uuid.UUID(measurement_id))
            await db.commit()
        if status in (QualityStatus.validated, QualityStatus.indicative):
            celery_app.send_task("reviews.dispatch", args=[tenant_id, measurement_id], countdown=2)
        return status.value if status else None
    return run(_job())


@celery_app.task(name="measurements.sweep_pending")
def sweep_pending():
    """Reprend les mesures restées 'pending' (file perdue, worker arrêté)."""
    async def _job(db, tid):
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
        ids = (await db.execute(select(Measurement.id).where(
            Measurement.tenant_id == tid, Measurement.quality_status == QualityStatus.pending,
            Measurement.created_at < cutoff).limit(500))).scalars().all()
        for mid in ids:
            await quality_service.evaluate_measurement(db, tid, mid)
            await db.commit()
        return len(ids)
    return run(for_each_tenant(_job))


@celery_app.task(name="measurements.reevaluate_model")
def reevaluate_model(tenant_id: str, device_model_id: str):
    """Réévalue les mesures récentes d'un modèle après changement de calibration (décisions admin et avis conservés)."""
    async def _job():
        tid = uuid.UUID(tenant_id)
        since = datetime.now(timezone.utc) - timedelta(days=settings.REEVALUATION_DAYS)
        async with task_db(tid) as db:
            ids = (await db.execute(select(Measurement.id).where(
                Measurement.tenant_id == tid, Measurement.device_model_id == uuid.UUID(device_model_id),
                Measurement.measured_at >= since))).scalars().all()
            for mid in ids:
                await quality_service.evaluate_measurement(db, tid, mid, force=True)
                await db.commit()
            return len(ids)
    return run(_job())