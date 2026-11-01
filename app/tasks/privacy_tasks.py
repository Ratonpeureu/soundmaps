from app.services import privacy as privacy_service
from app.tasks.celery_app import celery_app
from app.tasks.runtime import for_each_tenant, run


@celery_app.task(name="privacy.anonymize")
def anonymize():
    async def _job(db, tid):
        a = await privacy_service.anonymize_tenant(db, tid)
        b = await privacy_service.purge_expired(db, tid)
        return {**a, **b}
    return run(for_each_tenant(_job))