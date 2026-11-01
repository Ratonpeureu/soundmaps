from app.services import aggregation as aggregation_service
from app.tasks.celery_app import celery_app
from app.tasks.runtime import for_each_tenant, run


@celery_app.task(name="aggregation.run")
def aggregate_all():
    async def _job(db, tid):
        return await aggregation_service.aggregate_dirty(db, tid)
    return run(for_each_tenant(_job))