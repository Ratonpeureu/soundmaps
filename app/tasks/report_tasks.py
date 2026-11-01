import uuid

from app.core.redis import new_redis
from app.services import report as report_service
from app.tasks.celery_app import celery_app
from app.tasks.runtime import run, task_db


@celery_app.task(name="reports.generate", autoretry_for=(Exception,), retry_backoff=True,
                 retry_kwargs={"max_retries": 3})
def generate(tenant_id: str, report_id: str):
    async def _job():
        tid = uuid.UUID(tenant_id)
        redis = new_redis()
        try:
            async with task_db(tid) as db:
                await report_service.generate(db, redis, tid, uuid.UUID(report_id))
        finally:
            await redis.aclose()
    return run(_job())