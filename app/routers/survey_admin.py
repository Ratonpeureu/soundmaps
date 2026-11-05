import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db, require_roles
from app.core.rate_limit import limit_by_user
from app.models.enums import QualityStatus, Role, SurveyDomain, SurveyStatus
from app.models.survey import ReferenceValue, Survey, SurveyEvidence, SurveyProfile, SurveyResponse
from app.models.tenancy import User
from app.schemas.common import Msg
from app.schemas.surveys import (BulkIn, DesignIn, EvolveIn, GenerateIn, ProfileCreate, ProfileUpdate, ReferenceBulk,
                                 ReferenceIn, ReleaseIn, StatusIn, SurveyCreate, SurveyUpdate)
from app.services import catalog
from app.services import survey_admin as adm
from app.services import survey_ai
from app.services import survey_profiles as profiles
from app.services.audit import audit
from app.services.survey_ingest import definition_of
from app.services.survey_quality import apply_decision
from app.services.survey_schema import public_view
from app.tasks.celery_app import celery_app

router = APIRouter(prefix="/survey-admin", tags=["survey-admin"],
                   dependencies=[Depends(limit_by_user("survey_admin", settings.RATE_LIMIT_API_PER_MINUTE))])
_admin = require_roles(Role.tenant_admin)
_staff = require_roles(Role.tenant_admin, Role.analyst)


# ------------------------------------------------------------------ enquêtes
@router.get("/surveys")
async def list_surveys(status: SurveyStatus | None = None, domain: SurveyDomain | None = None,
                       user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    q = select(Survey).where(Survey.tenant_id == user.tenant_id)
    if status:
        q = q.where(Survey.status == status)
    if domain:
        q = q.where(Survey.domain == domain)
    rows = (await db.execute(q.order_by(Survey.created_at.desc()).limit(200))).scalars().all()
    counts = dict((await db.execute(select(SurveyResponse.survey_id, func.count()).where(
        SurveyResponse.tenant_id == user.tenant_id).group_by(SurveyResponse.survey_id))).all())
    return [adm.survey_out(s, counts.get(s.id, 0)) for s in rows]


@router.post("/surveys", status_code=201)
async def create(payload: SurveyCreate, request: Request, admin: User = Depends(_admin),
                 db: AsyncSession = Depends(get_db)):
    s = await adm.create_survey(db, admin, payload)
    await audit(db, admin, "survey.create", "survey", s.id, {"domain": s.domain.value}, request)
    await db.commit()
    return adm.survey_out(s)


@router.post("/surveys/design")
async def design(payload: DesignIn, user: User = Depends(_admin),
                 _rl=Depends(limit_by_user("ai", settings.RATE_LIMIT_AI_PER_MINUTE))):
    """Brouillon d'enquête proposé par l'IA, limité au catalogue ; à relire puis à créer via POST /surveys."""
    draft = await survey_ai.design_survey(payload.domain.value, payload.goal, payload.indicators, payload.unit,
                                          payload.max_questions)
    adm.check_def(adm.parse_def(draft["definition"]), payload.domain.value, False)
    return {**draft, "domain": payload.domain.value, "ai_generated": True}


@router.get("/surveys/{survey_id}")
async def get_one(survey_id: uuid.UUID, user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    s = await adm.get_survey(db, user.tenant_id, survey_id)
    defn = await definition_of(db, s)
    return {**adm.survey_out(s, await adm.n_responses(db, s.id)), "definition": defn.model_dump(by_alias=True),
            "preview": public_view(defn)}


@router.patch("/surveys/{survey_id}")
async def update(survey_id: uuid.UUID, payload: SurveyUpdate, request: Request, admin: User = Depends(_admin),
                 db: AsyncSession = Depends(get_db)):
    s = await adm.get_survey(db, admin.tenant_id, survey_id)
    res = await adm.update_survey(db, s, payload)
    await audit(db, admin, "survey.update", "survey", s.id, res, request)
    await db.commit()
    return {**adm.survey_out(s, await adm.n_responses(db, s.id)), **res}


@router.post("/surveys/{survey_id}/status")
async def status(survey_id: uuid.UUID, payload: StatusIn, request: Request, admin: User = Depends(_admin),
                 db: AsyncSession = Depends(get_db)):
    s = await adm.get_survey(db, admin.tenant_id, survey_id)
    info = await adm.set_status(db, s, payload.status, payload.use_ai)
    await audit(db, admin, "survey.status", "survey", s.id, {"status": payload.status, **info}, request)
    await db.commit()
    return {**adm.survey_out(s), **info}


@router.get("/surveys/{survey_id}/stats")
async def stats(survey_id: uuid.UUID, days: int = Query(30, ge=1, le=365), user: User = Depends(_staff),
                db: AsyncSession = Depends(get_db)):
    return await adm.survey_stats(db, await adm.get_survey(db, user.tenant_id, survey_id), days)


# ------------------------------------------------------------------ profils
@router.get("/surveys/{survey_id}/profiles")
async def list_profiles(survey_id: uuid.UUID, user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    return await adm.profiles_report(db, await adm.get_survey(db, user.tenant_id, survey_id))


@router.post("/surveys/{survey_id}/profiles", status_code=201)
async def add_profile(survey_id: uuid.UUID, payload: ProfileCreate, request: Request, admin: User = Depends(_admin),
                      db: AsyncSession = Depends(get_db)):
    s = await adm.get_survey(db, admin.tenant_id, survey_id)
    sp = await adm.admin_profile(db, s, payload)
    await audit(db, admin, "survey.profile.create", "survey_profile", sp.id, {"slot": sp.slot}, request)
    await db.commit()
    return {"id": str(sp.id)}


@router.patch("/profiles/{profile_id}", response_model=Msg)
async def patch_profile(profile_id: uuid.UUID, payload: ProfileUpdate, request: Request, admin: User = Depends(_admin),
                        db: AsyncSession = Depends(get_db)):
    sp = await db.get(SurveyProfile, profile_id)
    if sp is None or sp.tenant_id != admin.tenant_id:
        raise HTTPException(404, "profile_not_found")
    await adm.update_profile(db, sp, payload)
    await audit(db, admin, "survey.profile.update", "survey_profile", sp.id, payload.model_dump(exclude_none=True), request)
    await db.commit()
    return Msg(detail="updated")


@router.post("/surveys/{survey_id}/profiles/generate")
async def generate(survey_id: uuid.UUID, payload: GenerateIn, request: Request, admin: User = Depends(_admin),
                   db: AsyncSession = Depends(get_db)):
    s = await adm.get_survey(db, admin.tenant_id, survey_id)
    defn = await definition_of(db, s)
    slots = [x for x in defn.slots if x.mode == "profile" and (payload.slot is None or x.key == payload.slot)]
    if not slots:
        raise HTTPException(422, "unknown_slot")
    added = []
    for sl in slots:
        added += await profiles.generate_slot(db, s, defn, sl.key, n=payload.n, use_ai=payload.use_ai and adm.ai_ready())
    await audit(db, admin, "survey.profile.generate", "survey", s.id, {"added": len(added)}, request)
    await db.commit()
    return {"added": len(added), "profiles": [profiles.profile_out(defn, p) for p in added]}


@router.post("/surveys/{survey_id}/evolve")
async def evolve(survey_id: uuid.UUID, payload: EvolveIn, request: Request, admin: User = Depends(_admin),
                 db: AsyncSession = Depends(get_db)):
    """Fait évoluer les profils maintenant : retire les moins choisis, ajoute ceux que les réponses réelles réclament."""
    s = await adm.get_survey(db, admin.tenant_id, survey_id)
    res = await profiles.evolve(db, s, await definition_of(db, s), use_ai=payload.use_ai and adm.ai_ready())
    await audit(db, admin, "survey.profile.evolve", "survey", s.id, {"generation": res["generation"]}, request)
    await db.commit()
    return res


@router.get("/surveys/{survey_id}/profile-events")
async def events(survey_id: uuid.UUID, user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    await adm.get_survey(db, user.tenant_id, survey_id)
    return await adm.profile_events(db, survey_id)


# ------------------------------------------------------------------ validation des réponses
@router.get("/responses")
async def queue(survey_id: uuid.UUID | None = None, status: list[QualityStatus] | None = Query(None),
                evaluator: str | None = Query(None, pattern="^(auto|admin)$"), reason: str | None = Query(None, max_length=60),
                min_score: float | None = Query(None, ge=0, le=1), max_score: float | None = Query(None, ge=0, le=1),
                date_from: date | None = None, date_to: date | None = None, limit: int = Query(50, ge=1, le=200),
                offset: int = Query(0, ge=0), user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    return await adm.queue(db, user.tenant_id, survey_id=survey_id, status=status, evaluator=evaluator, reason=reason,
                           min_score=min_score, max_score=max_score, date_from=date_from, date_to=date_to,
                           limit=limit, offset=offset)


@router.post("/responses/bulk")
async def bulk(payload: BulkIn, request: Request, admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    status_ = QualityStatus(payload.status)
    rs = (await db.execute(select(SurveyResponse).where(SurveyResponse.tenant_id == admin.tenant_id,
                                                         SurveyResponse.id.in_(payload.ids)).with_for_update())).scalars().all()
    for r in rs:
        await apply_decision(db, r, status_, admin.id, payload.reason)
    await audit(db, admin, "survey.response.bulk", "survey_response", None,
                {"count": len(rs), "status": payload.status, "reason": payload.reason}, request)
    await db.commit()
    return {"updated": len(rs), "skipped": len(payload.ids) - len(rs)}


@router.post("/responses/release")
async def release(payload: ReleaseIn, request: Request, admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    ids = await adm.release(db, admin.tenant_id, payload.ids)
    await audit(db, admin, "survey.response.release", "survey_response", None, {"count": len(ids)}, request)
    await db.commit()
    for i in ids:
        celery_app.send_task("surveys.evaluate", args=[str(admin.tenant_id), str(i)])
    return {"released": len(ids)}


@router.get("/evidence/{evidence_id}/content")
async def evidence_content(evidence_id: uuid.UUID, request: Request, admin: User = Depends(_admin),
                           db: AsyncSession = Depends(get_db)):
    e = await db.get(SurveyEvidence, evidence_id)
    if e is None or e.tenant_id != admin.tenant_id or e.content is None:
        raise HTTPException(404, "evidence_not_available")
    await audit(db, admin, "survey.evidence.view", "survey_evidence", e.id, request=request)
    await db.commit()
    return Response(e.content, media_type=e.mime, headers={"Cache-Control": "no-store"})


@router.get("/respondents")
async def respondents(limit: int = Query(30, ge=1, le=200), user: User = Depends(_admin),
                      db: AsyncSession = Depends(get_db)):
    """Profils de comportement (pseudonymes uniquement) : répartition et répondants à plus faible confiance."""
    return await adm.respondents(db, user.tenant_id, limit)


# ------------------------------------------------------------------ valeurs de référence fournies par l'organisation
def _ref_out(v: ReferenceValue) -> dict:
    return {"id": str(v.id), "indicator_key": v.indicator_key, "stat": v.stat, "category": v.category, "value": v.value,
            "scope_h3": v.scope_h3, "scope_res": v.scope_res, "period_year": v.period_year, "source": v.source,
            "created_at": v.created_at}


@router.get("/reference")
async def list_refs(indicator_key: str | None = None, user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    q = select(ReferenceValue).where(ReferenceValue.tenant_id == user.tenant_id)
    if indicator_key:
        q = q.where(ReferenceValue.indicator_key == indicator_key)
    return [_ref_out(v) for v in (await db.execute(q.order_by(ReferenceValue.indicator_key).limit(500))).scalars()]


@router.post("/reference", status_code=201)
async def add_refs(payload: ReferenceBulk, request: Request, admin: User = Depends(_admin),
                   db: AsyncSession = Depends(get_db)):
    for it in payload.items:
        try:
            it.check()
        except ValueError as e:
            raise HTTPException(422, {"code": str(e), "indicator_key": it.indicator_key})
        db.add(ReferenceValue(tenant_id=admin.tenant_id, created_by=admin.id, **it.model_dump()))
    await audit(db, admin, "survey.reference.add", "reference", None, {"count": len(payload.items)}, request)
    await db.commit()
    return {"added": len(payload.items)}


@router.delete("/reference/{ref_id}", response_model=Msg)
async def del_ref(ref_id: uuid.UUID, request: Request, admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    v = await db.get(ReferenceValue, ref_id)
    if v is None or v.tenant_id != admin.tenant_id:
        raise HTTPException(404, "reference_not_found")
    await db.delete(v)
    await audit(db, admin, "survey.reference.delete", "reference", ref_id, request=request)
    await db.commit()
    return Msg(detail="deleted")