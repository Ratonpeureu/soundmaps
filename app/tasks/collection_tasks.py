from app.services import collection as collection_service
from app.tasks.celery_app import celery_app
from app.tasks.runtime import for_each_tenant, run


@celery_app.task(name="collection.expire_proposals")
def expire_proposals():
    async def _job(db, tid):
        return await collection_service.expire_proposals(db, tid)
    return run(for_each_tenant(_job))


@celery_app.task(name="collection.adapt_weights")
def adapt_weights():
    async def _job(db, tid):
        new = await collection_service.adapt_tenant_weights(db, tid)
        return new is not None
    return run(for_each_tenant(_job))