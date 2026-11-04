from app.core.redis import new_redis
from app.services import missions as missions_service
from app.services import quota as quota_service
from app.tasks.celery_app import celery_app
from app.tasks.runtime import for_each_tenant, run


@celery_app.task(name="quota.rebalance")
def rebalance():
    async def _all():
        redis = new_redis()
        try:
            async def _job(db, tid):
                return await quota_service.rebalance_tenant(db, redis, tid)
            return await for_each_tenant(_job)
        finally:
            await redis.aclose()
    return run(_all())


@celery_app.task(name="quota.cleanup")
def cleanup():
    async def _job(db, tid):
        return await quota_service.cleanup_tenant(db, tid)
    return run(for_each_tenant(_job))


@celery_app.task(name="missions.generate")
def generate_missions():
    async def _all():
        redis = new_redis()
        try:
            async def _job(db, tid):
                await missions_service.expire(db, tid)
                return await missions_service.generate_for_tenant(db, redis, tid)
            return await for_each_tenant(_job)
        finally:
            await redis.aclose()
    return run(_all())


@celery_app.task(name="missions.reconcile")
def reconcile_missions():
    async def _job(db, tid):
        await missions_service.expire(db, tid)
        return await missions_service.reconcile(db, tid)
    return run(for_each_tenant(_job))