from app.core.redis import new_redis
from app.services import ai as ai_service
from app.tasks.celery_app import celery_app
from app.tasks.runtime import for_each_tenant, run


@celery_app.task(name="ai.anomaly_digest")
def anomaly_digest():
    async def _all():
        redis = new_redis()
        try:
            async def _job(db, tid):
                return await ai_service.anomaly_digest(db, redis, tid)
            return await for_each_tenant(_job)
        finally:
            await redis.aclose()
    return run(_all())