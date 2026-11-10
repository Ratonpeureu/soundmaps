import uuid
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db, require_roles
from app.core.rate_limit import limit_by_user
from app.core.study_settings import study_settings as sts
from app.models.enums import AssetKind, QualityStatus, RewardStatus, Role, StudyKind, StudyStatus
from app.models.study import Study, StudyAsset, StudyParticipation, StudyResponse, StudyReward, StudySponsor, StudyVersion
from app.models.tenancy import User
from app.schemas.common import Msg
from app.schemas.studies import (CodesIn, DecideIn, DrawIn, RewardActionIn, SponsorIn, SponsorUpdate, StatusIn,
                                 StudyCreate, StudyUpdate, TargetingPreviewIn)
from app.services import study_admin as adm
from app.services import study_ingest as ing
from app.services import study_rewards as rwd
from app.services import study_targeting as tgt
from app.services.audit import audit
from app.services.study_schema import (KIND_LABELS, SHARE_FIELDS, SPONSOR_LABELS, Targeting, attrs_public, public_view)
from app.tasks.celery_app import celery_app

router = APIRouter(prefix="/study-admin", tags=["study-admin"],
                   dependencies=[Depends(limit_by_user("study_admin", settings.RATE_LIMIT_API_PER_MINUTE))])
_admin = require_roles(Role.tenant_admin)
_staff = require_roles(Role.tenant_admin, Role.analyst)
_DEFAULT = [QualityStatus.validated, QualityStatus.indicative]

BLOCK_KINDS = [("info", "Texte d'information"), ("media", "Image / vidéo"), ("single_choice", "Choix unique"),
               ("multi_choice", "Choix multiple"), ("yes_no", "Oui / Non"), ("number", "Nombre"), ("slider", "Curseur"),
               ("scale", "Échelle"), ("stars", "Étoiles"), ("nps", "Recommandation 0-10"), ("text", "Texte court"),
               ("long_text", "Texte long"), ("ranking", "Classement"), ("matrix", "Grille"), ("date", "Date"),
               ("product_rating", "Notation de produits")]


@router.get("/meta")
async def meta(user: User = Depends(_staff)):
    return {"kinds": [{"value": k, "label": v} for k, v in KIND_LABELS.items()],
            "sponsor_kinds": [{"value": k, "label": v} for k, v in SPONSOR_LABELS.items()],
            "block_kinds": [{"value": k, "label": v} for k, v in BLOCK_KINDS],
            "reward_kinds": [{"value": k, "label": v} for k, v in rwd.KIND_LABELS.items()],
            "refusal_reasons": rwd.REASONS, "share_fields": list(SHARE_FIELDS), "attrs": attrs_public()}


# ------------------------------------------------------------------ médiathèque
@router.post("/assets", status_code=201)
async def upload_asset(request: Request, file: UploadFile = File(...), name: str | None = Form(None),
                       admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    data = await file.read(sts.ST_MAX_VIDEO_BYTES + 1)
    out = await adm.upload_asset(db, admin, file.filename or "", data, name)
    await audit(db, admin, "study.asset.upload", "study_asset", out["id"], {"kind": out["kind"]}, request)
    await db.commit()
    return out


@router.get("/assets")
async def list_assets(kind: AssetKind | None = None, user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    return await adm.list_assets(db, user.tenant_id, kind)


@router.delete("/assets/{asset_id}", response_model=Msg)
async def delete_asset(asset_id: uuid.UUID, request: Request, admin: User = Depends(_admin),
                       db: AsyncSession = Depends(get_db)):
    a = await db.get(StudyAsset, asset_id)
    if a is None or a.tenant_id != admin.tenant_id:
        raise HTTPException(404, "asset_not_found")
    tid = admin.tenant_id
    used = (await db.execute(select(func.count()).select_from(StudyVersion).where(
        StudyVersion.tenant_id == tid, cast(StudyVersion.definition, String).like(f"%{asset_id}%")))).scalar_one()
    used += (await db.execute(select(func.count()).select_from(Study).where(
        Study.tenant_id == tid, Study.cover_asset_id == asset_id))).scalar_one()
    used += (await db.execute(select(func.count()).select_from(StudySponsor).where(
        StudySponsor.tenant_id == tid, (StudySponsor.logo_asset_id == asset_id) | (StudySponsor.cover_asset_id == asset_id)))).scalar_one()
    if used:
        raise HTTPException(409, "asset_in_use")
    await db.delete(a)
    await audit(db, admin, "study.asset.delete", "study_asset", asset_id, request=request)
    await db.commit()
    return Msg(detail="deleted")


# ------------------------------------------------------------------ initiateurs
@router.get("/sponsors")
async def list_sponsors(user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(StudySponsor).where(StudySponsor.tenant_id == user.tenant_id)
                             .order_by(StudySponsor.name))).scalars().all()
    n = dict((await db.execute(select(Study.sponsor_id, func.count()).where(Study.tenant_id == user.tenant_id)
                               .group_by(Study.sponsor_id))).all())
    return [{**adm.sponsor_out(s), "studies": n.get(s.id, 0)} for s in rows]


@router.post("/sponsors", status_code=201)
async def create_sponsor(payload: SponsorIn, request: Request, admin: User = Depends(_admin),
                         db: AsyncSession = Depends(get_db)):
    sp = await adm.save_sponsor(db, admin, payload.model_dump())
    await audit(db, admin, "study.sponsor.create", "study_sponsor", sp.id, {"name": sp.name}, request)
    await db.commit()
    return adm.sponsor_out(sp)


async def _sponsor(db, tenant_id, sponsor_id) -> StudySponsor:
    sp = await db.get(StudySponsor, sponsor_id)
    if sp is None or sp.tenant_id != tenant_id:
        raise HTTPException(404, "sponsor_not_found")
    return sp


@router.get("/sponsors/{sponsor_id}")
async def get_sponsor(sponsor_id: uuid.UUID, user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    return adm.sponsor_out(await _sponsor(db, user.tenant_id, sponsor_id))


@router.patch("/sponsors/{sponsor_id}")
async def update_sponsor(sponsor_id: uuid.UUID, payload: SponsorUpdate, request: Request,
                         admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    sp = await _sponsor(db, admin.tenant_id, sponsor_id)
    await adm.save_sponsor(db, admin, payload.model_dump(exclude_unset=True), sp)
    await audit(db, admin, "study.sponsor.update", "study_sponsor", sp.id, request=request)
    await db.commit()
    return adm.sponsor_out(sp)


@router.delete("/sponsors/{sponsor_id}", response_model=Msg)
async def delete_sponsor(sponsor_id: uuid.UUID, request: Request, admin: User = Depends(_admin),
                         db: AsyncSession = Depends(get_db)):
    sp = await _sponsor(db, admin.tenant_id, sponsor_id)
    if (await db.execute(select(func.count()).select_from(Study).where(Study.sponsor_id == sp.id))).scalar_one():
        raise HTTPException(409, "sponsor_has_studies")
    await db.delete(sp)
    await audit(db, admin, "study.sponsor.delete", "study_sponsor", sponsor_id, request=request)
    await db.commit()
    return Msg(detail="deleted")


# ------------------------------------------------------------------ études
async def _study(db, tenant_id, study_id) -> Study:
    return await adm.get_study(db, tenant_id, study_id)


@router.get("/studies")
async def list_studies(status: StudyStatus | None = None, kind: StudyKind | None = None,
                       sponsor_id: uuid.UUID | None = None, user: User = Depends(_staff),
                       db: AsyncSession = Depends(get_db)):
    q = (select(Study, StudySponsor).join(StudySponsor, StudySponsor.id == Study.sponsor_id)
         .where(Study.tenant_id == user.tenant_id))
    if status:
        q = q.where(Study.status == status)
    if kind:
        q = q.where(Study.kind == kind)
    if sponsor_id:
        q = q.where(Study.sponsor_id == sponsor_id)
    rows = (await db.execute(q.order_by(Study.created_at.desc()).limit(300))).all()
    counts = dict((await db.execute(select(StudyResponse.study_id, func.count()).where(
        StudyResponse.tenant_id == user.tenant_id, StudyResponse.outcome == "completed")
        .group_by(StudyResponse.study_id))).all())
    return [adm.study_out(s, sp, counts.get(s.id, 0)) for s, sp in rows]


@router.post("/studies", status_code=201)
async def create_study(payload: StudyCreate, request: Request, admin: User = Depends(_admin),
                       db: AsyncSession = Depends(get_db)):
    s = await adm.create_study(db, admin, payload.model_dump())
    await audit(db, admin, "study.create", "study", s.id, {"kind": s.kind.value}, request)
    sp = await db.get(StudySponsor, s.sponsor_id)
    await db.commit()
    return adm.study_out(s, sp, 0)


@router.post("/targeting/preview")
async def targeting_preview(payload: TargetingPreviewIn, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    """Estime la taille de la cible (utilisateurs ayant consenti « Études » uniquement)."""
    t = adm.parse(Targeting, payload.targeting, "invalid_targeting")
    return await tgt.preview(db, user.tenant_id, t)


@router.get("/studies/{study_id}")
async def get_study(study_id: uuid.UUID, user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    s = await _study(db, user.tenant_id, study_id)
    sp = await db.get(StudySponsor, s.sponsor_id)
    d = await ing.definition_of(db, s)
    return adm.study_out(s, sp, await adm.n_resp(db, s.id), d.model_dump(mode="json", exclude_none=True)) | {
        "preview": public_view(d)}


@router.patch("/studies/{study_id}")
async def update_study(study_id: uuid.UUID, payload: StudyUpdate, request: Request, admin: User = Depends(_admin),
                       db: AsyncSession = Depends(get_db)):
    s = await _study(db, admin.tenant_id, study_id)
    res = await adm.update_study(db, admin, s, payload.model_dump(exclude_unset=True))
    await audit(db, admin, "study.update", "study", s.id, res, request)
    sp = await db.get(StudySponsor, s.sponsor_id)
    await db.commit()
    return {**adm.study_out(s, sp, await adm.n_resp(db, s.id)), **res}


@router.post("/studies/{study_id}/status")
async def set_status(study_id: uuid.UUID, payload: StatusIn, request: Request, admin: User = Depends(_admin),
                     db: AsyncSession = Depends(get_db)):
    s = await _study(db, admin.tenant_id, study_id)
    info = await adm.set_status(db, s, payload.status)
    await audit(db, admin, "study.status", "study", s.id, {"status": payload.status, **info}, request)
    sp = await db.get(StudySponsor, s.sponsor_id)
    await db.commit()
    if payload.status == "active":
        try:
            celery_app.send_task("studies.invite")
        except Exception:
            pass                                   # le balayeur périodique reprendra
    return {**adm.study_out(s, sp), **info}


@router.post("/studies/{study_id}/duplicate", status_code=201)
async def duplicate(study_id: uuid.UUID, request: Request, admin: User = Depends(_admin),
                    db: AsyncSession = Depends(get_db)):
    n = await adm.duplicate(db, admin, await _study(db, admin.tenant_id, study_id))
    await audit(db, admin, "study.duplicate", "study", n.id, request=request)
    sp = await db.get(StudySponsor, n.sponsor_id)
    await db.commit()
    return adm.study_out(n, sp, 0)


@router.delete("/studies/{study_id}", response_model=Msg)
async def delete_study(study_id: uuid.UUID, request: Request, admin: User = Depends(_admin),
                       db: AsyncSession = Depends(get_db)):
    s = await _study(db, admin.tenant_id, study_id)
    if s.status != StudyStatus.draft or await adm.n_resp(db, s.id):
        raise HTTPException(409, "study_not_deletable")
    await db.delete(s)
    await audit(db, admin, "study.delete", "study", study_id, request=request)
    await db.commit()
    return Msg(detail="deleted")


@router.post("/studies/{study_id}/invite")
async def invite_now(study_id: uuid.UUID, request: Request, admin: User = Depends(_admin),
                     db: AsyncSession = Depends(get_db)):
    """Lance tout de suite le ciblage et l'envoi des invitations (toutes les études actives du tenant)."""
    await _study(db, admin.tenant_id, study_id)
    res = await tgt.invite_all(db, admin.tenant_id)
    await audit(db, admin, "study.invite", "study", study_id, res, request)
    await db.commit()
    return res


@router.get("/studies/{study_id}/stats")
async def stats(study_id: uuid.UUID, user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    return await adm.stats(db, await _study(db, user.tenant_id, study_id))


@router.get("/studies/{study_id}/results")
async def results(study_id: uuid.UUID, by: str | None = Query(None, max_length=24),
                  status: list[QualityStatus] | None = Query(None), user: User = Depends(_staff),
                  db: AsyncSession = Depends(get_db)):
    return await adm.results(db, await _study(db, user.tenant_id, study_id), by, status or _DEFAULT)


@router.get("/studies/{study_id}/responses")
async def responses(study_id: uuid.UUID, status: list[QualityStatus] | None = Query(None),
                    outcome: Literal["completed", "screened_out"] | None = None, limit: int = Query(50, ge=1, le=200),
                    offset: int = Query(0, ge=0), user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    return await adm.list_responses(db, await _study(db, user.tenant_id, study_id), status, outcome, limit, offset)


@router.post("/studies/{study_id}/responses/decide")
async def decide(study_id: uuid.UUID, payload: DecideIn, request: Request, admin: User = Depends(_admin),
                 db: AsyncSession = Depends(get_db)):
    s = await _study(db, admin.tenant_id, study_id)
    n = await adm.decide_responses(db, admin, s, payload.ids, QualityStatus(payload.status), payload.reason)
    await audit(db, admin, "study.response.decide", "study", s.id,
                {"count": n, "status": payload.status, "reason": payload.reason}, request)
    await db.commit()
    return {"updated": n, "skipped": len(payload.ids) - n}


@router.get("/studies/{study_id}/export")
async def export(study_id: uuid.UUID, request: Request, fmt: Literal["csv", "json"] = "csv",
                 status: list[QualityStatus] | None = Query(None), admin: User = Depends(_admin),
                 db: AsyncSession = Depends(get_db)):
    s = await _study(db, admin.tenant_id, study_id)
    content, mime, filename = await adm.export(db, s, fmt, status or _DEFAULT)
    await audit(db, admin, "study.export", "study", s.id, {"fmt": fmt}, request)
    await db.commit()
    return Response(content, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                                       "Cache-Control": "no-store"})


# ------------------------------------------------------------------ récompenses
@router.get("/studies/{study_id}/rewards")
async def rewards(study_id: uuid.UUID, status: RewardStatus | None = None, limit: int = Query(50, ge=1, le=200),
                  offset: int = Query(0, ge=0), user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    return await adm.list_rewards(db, await _study(db, user.tenant_id, study_id), status, limit, offset)


@router.post("/rewards/{reward_id}/action", response_model=Msg)
async def reward_action(reward_id: uuid.UUID, payload: RewardActionIn, request: Request, admin: User = Depends(_admin),
                        db: AsyncSession = Depends(get_db)):
    rw = await db.get(StudyReward, reward_id, with_for_update=True)
    if rw is None or rw.tenant_id != admin.tenant_id:
        raise HTTPException(404, "reward_not_found")
    await adm.reward_action(db, admin, rw, payload)
    await audit(db, admin, "study.reward." + payload.action, "study_reward", rw.id,
                {"reason_code": payload.reason_code}, request)
    await db.commit()
    return Msg(detail="ok")


@router.post("/studies/{study_id}/codes")
async def add_codes(study_id: uuid.UUID, payload: CodesIn, request: Request, admin: User = Depends(_admin),
                    db: AsyncSession = Depends(get_db)):
    s = await _study(db, admin.tenant_id, study_id)
    res = await adm.add_codes(db, s, payload.codes)
    await audit(db, admin, "study.codes.add", "study", s.id, res, request)
    await db.commit()
    return res


@router.post("/studies/{study_id}/draw")
async def draw(study_id: uuid.UUID, payload: DrawIn, request: Request, admin: User = Depends(_admin),
               db: AsyncSession = Depends(get_db)):
    s = await _study(db, admin.tenant_id, study_id)
    res = await adm.draw(db, admin, s, payload.winners, payload.seed)
    await audit(db, admin, "study.draw", "study", s.id, res, request)
    await db.commit()
    return res