import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.redis import new_redis
from app.core.survey_settings import survey_settings as sv
from app.models.enums import QualityStatus, SurveyStatus
from app.models.survey import Survey, SurveyResponse
from app.services import survey_ingest, survey_profiles, survey_quality, survey_trust
from app.services.survey_admin import ai_ready
from app.services.survey_ingest import definition_of
from app.tasks.celery_app import celery_app
from app.tasks.runtime import for_each_tenant, run, task_db


@celery_app.task(name="surveys.evaluate", autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 5})
def evaluate(tenant_id: str, response_id: str):
    async def _job():
        tid = uuid.UUID(tenant_id)
        redis = new_redis()
        try:
            async with task_db(tid) as db:
                res = await survey_quality.evaluate_response(db, tid, uuid.UUID(response_id))
                await db.commit()
                if res and res[1]:
                    await survey_trust.refresh_user(db, redis, tid, res[1])
                    await db.commit()
                return res[0].value if res else None
        finally:
            await redis.aclose()
    return run(_job())


@celery_app.task(name="surveys.sweep_pending")
def sweep_pending():
    """Reprend les réponses restées en attente (file perdue, photos jamais finalisées)."""
    async def _job(db, tid):
        now = datetime.now(timezone.utc)
        R = SurveyResponse
        ids = (await db.execute(select(R.id).where(
            R.tenant_id == tid, R.quality_status == QualityStatus.pending, R.created_at < now - timedelta(seconds=60),
            (R.needs_evidence.is_(False)) | (R.evidence_finalized_at.is_not(None))
            | (R.created_at < now - timedelta(minutes=sv.SV_EVIDENCE_WINDOW_MINUTES + 1))).limit(300))).scalars().all()
        for rid in ids:
            await survey_quality.evaluate_response(db, tid, rid)
            await db.commit()
        return len(ids)
    return run(for_each_tenant(_job))


@celery_app.task(name="surveys.reevaluate")
def reevaluate():
    """Les voisins et les références évoluent : les réponses récentes sont réévaluées (décisions admin conservées)."""
    async def _job(db, tid):
        now = datetime.now(timezone.utc)
        R = SurveyResponse
        ids = (await db.execute(select(R.id).where(
            R.tenant_id == tid, R.quality_status.in_([QualityStatus.indicative, QualityStatus.validated]),
            R.submitted_at >= now - timedelta(days=21),
            R.evaluated_at < now - timedelta(hours=sv.SQ_REEVAL_AFTER_HOURS)).order_by(R.evaluated_at).limit(300))).scalars().all()
        for rid in ids:
            await survey_quality.evaluate_response(db, tid, rid, force=True)
            await db.commit()
        return len(ids)
    return run(for_each_tenant(_job))


@celery_app.task(name="surveys.evolve_profiles")
def evolve_profiles():
    """Fait évoluer les profils des enquêtes actives selon les choix réels des répondants."""
    async def _job(db, tid):
        done = 0
        surveys = (await db.execute(select(Survey).where(Survey.tenant_id == tid, Survey.status == SurveyStatus.active))).scalars().all()
        for s in surveys:
            if await survey_profiles.due_for_evolution(db, s):
                await survey_profiles.evolve(db, s, await definition_of(db, s), use_ai=ai_ready())
                await db.commit()
                done += 1
        return done
    return run(for_each_tenant(_job))


@celery_app.task(name="surveys.refresh_trust")
def refresh_trust():
    async def _all():
        redis = new_redis()
        try:
            async def _job(db, tid):
                since = datetime.now(timezone.utc) - timedelta(days=3)
                users = (await db.execute(select(SurveyResponse.user_id).where(
                    SurveyResponse.tenant_id == tid, SurveyResponse.user_id.is_not(None),
                    SurveyResponse.submitted_at >= since).distinct().limit(5000))).scalars().all()
                for u in users:
                    await survey_trust.refresh_user(db, redis, tid, u)
                return len(users)
            return await for_each_tenant(_job)
        finally:
            await redis.aclose()
    return run(_all())


@celery_app.task(name="surveys.privacy")
def privacy():
    async def _job(db, tid):
        return await survey_ingest.privacy_sweep(db, tid)
    return run(for_each_tenant(_job))