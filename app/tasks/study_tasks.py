from app.services import study_maintenance as maint
from app.services import study_targeting as tgt
from app.tasks.celery_app import celery_app
from app.tasks.runtime import for_each_tenant, run


@celery_app.task(name="studies.invite")
def invite():
    """Ciblage : calcule le profil dérivé des utilisateurs et envoie les invitations triées par score."""
    return run(for_each_tenant(tgt.invite_all))


@celery_app.task(name="studies.maintain")
def maintain():
    return run(for_each_tenant(maint.maintain))


@celery_app.task(name="studies.privacy")
def privacy():
    return run(for_each_tenant(maint.privacy_sweep))