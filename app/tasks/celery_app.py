from celery import Celery
from app.core.study_settings import study_settings as sts

from app.core.account_settings import account_settings as acs
from app.core.config import settings
from app.core.road_settings import road_settings as rs
from app.core.survey_settings import survey_settings as sv
celery_app = Celery(
    "sunusound",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.tasks.measurement_tasks", "app.tasks.aggregation_tasks", "app.tasks.collection_tasks",
             "app.tasks.privacy_tasks", "app.tasks.ai_tasks", "app.tasks.report_tasks",
             "app.tasks.quota_tasks", "app.tasks.social_tasks", "app.tasks.road_tasks","app.tasks.survey_tasks"],
)
celery_app.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",
    timezone="UTC", enable_utc=True,
    task_acks_late=True, worker_prefetch_multiplier=1, task_ignore_result=True,
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT, task_soft_time_limit=settings.CELERY_TASK_TIME_LIMIT - 30,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "sweep-pending-measurements": {"task": "measurements.sweep_pending", "schedule": settings.BEAT_SWEEP_SECONDS},
        "aggregate-noise": {"task": "aggregation.run", "schedule": settings.BEAT_AGGREGATION_SECONDS},
        "expire-proposals": {"task": "collection.expire_proposals", "schedule": settings.BEAT_EXPIRE_PROPOSALS_SECONDS},
        "adapt-weights": {"task": "collection.adapt_weights", "schedule": settings.BEAT_ADAPT_WEIGHTS_SECONDS},
        "privacy-anonymize": {"task": "privacy.anonymize", "schedule": settings.BEAT_PRIVACY_SECONDS},
        "ai-anomaly-digest": {"task": "ai.anomaly_digest", "schedule": settings.BEAT_AI_DIGEST_SECONDS},
        "quota-rebalance": {"task": "quota.rebalance", "schedule": settings.BEAT_QUOTA_SECONDS},
        "quota-cleanup": {"task": "quota.cleanup", "schedule": settings.BEAT_PRIVACY_SECONDS},
        "missions-generate": {"task": "missions.generate", "schedule": settings.BEAT_MISSIONS_SECONDS},
        "missions-reconcile": {"task": "missions.reconcile", "schedule": settings.BEAT_MISSIONS_RECONCILE_SECONDS},
        "reviews-close": {"task": "reviews.close", "schedule": acs.BEAT_REVIEWS_SECONDS},
        "leaderboard-refresh": {"task": "leaderboard.refresh", "schedule": acs.BEAT_LEADERBOARD_SECONDS},
        "leaderboard-xp": {"task": "leaderboard.xp", "schedule": acs.BEAT_XP_SECONDS},
        "accounts-cleanup": {"task": "accounts.cleanup", "schedule": settings.BEAT_PRIVACY_SECONDS},
        "roads-match": {"task": "roads.match", "schedule": rs.ROAD_BEAT_MATCH_SECONDS},
        "roads-ensure-network": {"task": "roads.ensure_network", "schedule": rs.ROAD_BEAT_ENSURE_SECONDS},
        "surveys-sweep": {"task": "surveys.sweep_pending", "schedule": sv.BEAT_SURVEY_SWEEP_SECONDS},
        "surveys-reevaluate": {"task": "surveys.reevaluate", "schedule": sv.BEAT_SURVEY_REEVAL_SECONDS},
        "surveys-evolve": {"task": "surveys.evolve_profiles", "schedule": sv.BEAT_SURVEY_EVOLVE_SECONDS},
        "surveys-trust": {"task": "surveys.refresh_trust", "schedule": sv.BEAT_SURVEY_TRUST_SECONDS},
        "surveys-privacy": {"task": "surveys.privacy", "schedule": sv.BEAT_SURVEY_PRIVACY_SECONDS},
        "studies-invite": {"task": "studies.invite", "schedule": sts.BEAT_STUDY_INVITE_SECONDS},
        "studies-maintain": {"task": "studies.maintain", "schedule": sts.BEAT_STUDY_MAINT_SECONDS},
        "studies-privacy": {"task": "studies.privacy", "schedule": sts.BEAT_STUDY_PRIVACY_SECONDS},
    },
)