import uuid

from app.core.redis import new_redis
from app.services import accounts as accounts_service
from app.services import leaderboard as lb_service
from app.services import reviews as reviews_service
from app.tasks.celery_app import celery_app
from app.tasks.runtime import for_each_tenant, run, task_db


@celery_app.task(name="reviews.dispatch", autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 2})
def dispatch(tenant_id: str, measurement_id: str):
    async def _job():
        tid = uuid.UUID(tenant_id)
        redis = new_redis()
        try:
            async with task_db(tid) as db:
                res = await reviews_service.dispatch(db, redis, tid, uuid.UUID(measurement_id))
                await db.commit()
                return res
        finally:
            await redis.aclose()
    return run(_job())


@celery_app.task(name="reviews.close")
def close():
    async def _all():
        redis = new_redis()
        try:
            async def _job(db, tid):
                return await reviews_service.close_expired(db, redis, tid)
            return await for_each_tenant(_job)
        finally:
            await redis.aclose()
    return run(_all())


@celery_app.task(name="leaderboard.refresh")
def refresh():
    async def _job(db, tid):
        n = await lb_service.refresh_tenant(db, tid)
        s = await reviews_service.audit_analysts(db, tid)
        return {"entries": n, "suspended": s}
    return run(for_each_tenant(_job))


@celery_app.task(name="leaderboard.xp")
def xp():
    async def _job(db, tid):
        return await lb_service.xp_catchup(db, tid)
    return run(for_each_tenant(_job))


@celery_app.task(name="accounts.cleanup")
def cleanup():
    async def _job(db, tid):
        return await accounts_service.cleanup(db, tid)
    return run(for_each_tenant(_job))