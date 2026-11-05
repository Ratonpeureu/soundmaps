import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.survey_settings import survey_settings as sv
from app.models.enums import ProfileStatus, QualityStatus, SurveyStatus
from app.models.survey import (ProfileEvent, RespondentTrust, Survey, SurveyEvidence, SurveyProfile,
                               SurveyResponse, SurveyResponseQuality, SurveyVersion)
from app.models.tenancy import User
from app.schemas.surveys import (ProfileCreate, ProfileUpdate, SurveyCreate, SurveyUpdate)
from app.services import survey_profiles as profiles
from app.services.survey_ingest import definition_of, reason_out
from app.services.survey_schema import SurveyDefinition, definition_errors, describe_answer, public_view

_USABLE = [QualityStatus.validated, QualityStatus.indicative]


def ai_ready() -> bool:
    return bool(settings.API_GROQ_KEY.get_secret_value() and (sv.GROQ_MODEL_SURVEY or settings.GROQ_MODEL_ANALYSIS))


def parse_def(raw: dict) -> SurveyDefinition:
    try:
        return SurveyDefinition.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(422, {"code": "invalid_definition",
                                  "errors": [str(e["msg"]).replace("Value error, ", "") for e in exc.errors()][:12]})


def check_def(defn: SurveyDefinition, domain: str, allow_sensitive: bool) -> None:
    errs = definition_errors(defn, domain, allow_sensitive)
    if errs:
        raise HTTPException(422, {"code": "invalid_definition", "errors": errs})


def survey_out(s: Survey, n_responses: int = 0) -> dict:
    return {"id": str(s.id), "title": s.title, "description": s.description, "domain": s.domain.value,
            "status": s.status.value, "unit": s.unit, "version": s.current_version, "geo_scope": s.geo_scope,
            "cooldown_days": s.cooldown_days, "starts_at": s.starts_at, "ends_at": s.ends_at,
            "target_responses": s.target_responses, "allow_sensitive_axes": s.allow_sensitive_axes,
            "ai_generated": s.ai_generated, "profile_generation": s.profile_generation,
            "last_evolved_at": s.last_evolved_at, "responses": n_responses, "created_at": s.created_at}


async def get_survey(db: AsyncSession, tenant_id: uuid.UUID, survey_id: uuid.UUID) -> Survey:
    s = await db.get(Survey, survey_id)
    if s is None or s.tenant_id != tenant_id:
        raise HTTPException(404, "survey_not_found")
    return s


async def n_responses(db: AsyncSession, survey_id: uuid.UUID) -> int:
    return (await db.execute(select(func.count()).select_from(SurveyResponse).where(
        SurveyResponse.survey_id == survey_id))).scalar_one()


async def create_survey(db: AsyncSession, admin: User, p: SurveyCreate, ai_generated: bool = False) -> Survey:
    defn = parse_def(p.definition)
    check_def(defn, p.domain.value, p.allow_sensitive_axes)
    s = Survey(tenant_id=admin.tenant_id, created_by=admin.id, domain=p.domain, title=p.title.strip(),
               description=p.description, status=SurveyStatus.draft, unit=defn.unit,
               geo_scope=p.geo_scope.model_dump() if p.geo_scope else None, cooldown_days=p.cooldown_days,
               starts_at=p.starts_at, ends_at=p.ends_at, target_responses=p.target_responses,
               allow_sensitive_axes=p.allow_sensitive_axes, ai_generated=ai_generated, current_version=1)
    db.add(s)
    await db.flush()
    db.add(SurveyVersion(tenant_id=admin.tenant_id, survey_id=s.id, version=1, definition=defn.model_dump(by_alias=True)))
    await db.flush()
    return s


async def update_survey(db: AsyncSession, s: Survey, p: SurveyUpdate) -> dict:
    changed_def = False
    if p.allow_sensitive_axes is not None:
        s.allow_sensitive_axes = p.allow_sensitive_axes
    for f in ("title", "description", "cooldown_days", "starts_at", "ends_at", "target_responses"):
        v = getattr(p, f)
        if v is not None:
            setattr(s, f, v.strip() if f == "title" else v)
    if p.clear_geo_scope:
        s.geo_scope = None
    elif p.geo_scope is not None:
        s.geo_scope = p.geo_scope.model_dump()
    retired = 0
    if p.definition is not None:
        defn = parse_def(p.definition)
        check_def(defn, s.domain.value, s.allow_sensitive_axes)
        n = await n_responses(db, s.id)
        if n == 0 and s.status == SurveyStatus.draft:
            ver = (await db.execute(select(SurveyVersion).where(
                SurveyVersion.survey_id == s.id, SurveyVersion.version == s.current_version))).scalar_one()
            ver.definition = defn.model_dump(by_alias=True)
        else:
            s.current_version += 1
            db.add(SurveyVersion(tenant_id=s.tenant_id, survey_id=s.id, version=s.current_version,
                                 definition=defn.model_dump(by_alias=True)))
        s.unit = defn.unit
        changed_def = True
        retired = await profiles.reconcile_after_update(db, s, defn)
        if s.status == SurveyStatus.active:
            await profiles.ensure_profiles(db, s, defn, use_ai=False)
    s.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return {"version": s.current_version, "definition_changed": changed_def, "profiles_retired": retired}


async def set_status(db: AsyncSession, s: Survey, status: str, use_ai: bool) -> dict:
    new = SurveyStatus(status)
    info: dict = {}
    if new == SurveyStatus.active:
        defn = await definition_of(db, s)
        created = await profiles.ensure_profiles(db, s, defn, use_ai=use_ai and ai_ready())
        for slot in defn.slots:
            if slot.mode == "profile" and not await profiles.active_profiles(db, s.id, slot.key):
                raise HTTPException(409, {"code": "no_profiles_for_slot", "slot": slot.key})
        info["profiles_created"] = created
    s.status = new
    await db.flush()
    return info


async def admin_profile(db: AsyncSession, s: Survey, p: ProfileCreate) -> SurveyProfile:
    defn = await definition_of(db, s)
    slot = next((x for x in defn.slots if x.key == p.slot and x.mode == "profile"), None)
    if slot is None:
        raise HTTPException(422, "unknown_slot")
    clean = profiles.profile_valid(defn, p.slot, p.answers)
    if clean is None:
        raise HTTPException(422, "invalid_profile_answers")
    qs = [q for q in defn.questions if q.slot == p.slot]
    sp = SurveyProfile(tenant_id=s.tenant_id, survey_id=s.id, slot=p.slot, key=uuid.uuid4().hex[:8], label=p.label,
                       description=p.description, axes=profiles.axes_of(qs, clean), answers=clean, origin="admin",
                       generation=s.profile_generation, meta={"pinned": True})
    db.add(sp)
    await db.flush()
    return sp


async def update_profile(db: AsyncSession, sp: SurveyProfile, p: ProfileUpdate) -> None:
    if p.label is not None:
        sp.label = p.label
    if p.description is not None:
        sp.description = p.description
    if p.pinned is not None:
        sp.meta = {**(sp.meta or {}), "pinned": p.pinned}
    if p.retire is True:
        sp.status, sp.retired_at = ProfileStatus.retired, datetime.now(timezone.utc)
    elif p.retire is False and sp.status == ProfileStatus.retired:
        sp.status, sp.retired_at = ProfileStatus.active, None


async def profiles_report(db: AsyncSession, s: Survey) -> list[dict]:
    defn = await definition_of(db, s)
    rows = list((await db.execute(select(SurveyProfile).where(SurveyProfile.survey_id == s.id)
                                  .order_by(SurveyProfile.slot, SurveyProfile.status, SurveyProfile.created_at))).scalars())
    tot: dict[str, int] = {}
    for p in rows:
        if p.status == ProfileStatus.active:
            tot[p.slot] = tot.get(p.slot, 0) + p.picked_count
    out = []
    for p in rows:
        out.append({**profiles.profile_out(defn, p), "status": p.status.value, "generation": p.generation,
                    "shown": p.shown_count, "picked": p.picked_count, "overridden": p.override_count,
                    "pick_rate": round(p.picked_count / p.shown_count, 3) if p.shown_count else None,
                    "share_of_picks": round(p.picked_count / tot[p.slot], 3) if tot.get(p.slot) else None,
                    "override_rate": round(p.override_count / p.picked_count, 3) if p.picked_count else None,
                    "pinned": bool((p.meta or {}).get("pinned")), "retired_at": p.retired_at})
    return out


async def survey_stats(db: AsyncSession, s: Survey, days: int) -> dict:
    R = SurveyResponse
    since = datetime.now(timezone.utc) - timedelta(days=days)
    by_status = dict((await db.execute(select(R.quality_status, func.count()).where(R.survey_id == s.id)
                                       .group_by(R.quality_status))).all())
    per_day: dict = {}
    for d, st, n in (await db.execute(select(R.local_date, R.quality_status, func.count()).where(
            R.survey_id == s.id, R.submitted_at >= since).group_by(R.local_date, R.quality_status))).all():
        per_day.setdefault(d.isoformat(), {x.value: 0 for x in QualityStatus})[st.value] = n
    mode = dict((await db.execute(select(R.mode, func.count()).where(R.survey_id == s.id).group_by(R.mode))).all())
    total = sum(by_status.values()) or 0
    cells = (await db.execute(select(func.count(func.distinct(R.h3_index))).where(
        R.survey_id == s.id, R.quality_status.in_(_USABLE)))).scalar_one()
    contribs = (await db.execute(select(func.count(func.distinct(R.contributor_hash))).where(
        R.survey_id == s.id, R.quality_status.in_(_USABLE)))).scalar_one()
    defn = await definition_of(db, s)
    none_rate = {}
    for sl in defn.slots:
        c = (await db.execute(select(func.count()).where(R.survey_id == s.id, R.none_slots.contains([sl.key])))).scalar_one()
        none_rate[sl.key] = round(c / total, 3) if total else 0.0
    reasons = (await db.execute(text(
        "SELECT x->>'code' AS code, count(*) FROM survey_response_quality q "
        "JOIN survey_responses r ON r.id = q.response_id, jsonb_array_elements(q.reasons) x "
        "WHERE r.survey_id = :sid AND r.tenant_id = :tid GROUP BY 1 ORDER BY 2 DESC LIMIT 15"),
        {"sid": s.id, "tid": s.tenant_id})).all()
    return {"total": total, "by_status": {k.value: v for k, v in by_status.items()},
            "days": [{"date": d, **v} for d, v in sorted(per_day.items())], "modes": mode,
            "cells_covered": cells, "contributors": contribs, "none_rate_by_slot": none_rate,
            "progress": round(total / s.target_responses, 3) if s.target_responses else None,
            "top_reasons": [{"code": c, "label": reason_out({"code": c})["label"], "n": int(n)} for c, n in reasons],
            "profile_generation": s.profile_generation}


async def queue(db: AsyncSession, tenant_id: uuid.UUID, *, survey_id=None, status=None, evaluator=None, reason=None,
                min_score=None, max_score=None, date_from=None, date_to=None, limit=50, offset=0) -> dict:
    R, Q = SurveyResponse, SurveyResponseQuality
    conds = [R.tenant_id == tenant_id]
    if survey_id:
        conds.append(R.survey_id == survey_id)
    if status:
        conds.append(R.quality_status.in_(status))
    if evaluator:
        conds.append(Q.evaluator == evaluator)
    if reason:
        conds.append(Q.reasons.contains([{"code": reason}]))
    if min_score is not None:
        conds.append(Q.score >= min_score)
    if max_score is not None:
        conds.append(Q.score <= max_score)
    if date_from:
        conds.append(R.local_date >= date_from)
    if date_to:
        conds.append(R.local_date <= date_to)
    join = Q.response_id == R.id
    total = (await db.execute(select(func.count()).select_from(R).outerjoin(Q, join).where(*conds))).scalar_one()
    rows = (await db.execute(select(R, Q, Survey.title).join(Survey, Survey.id == R.survey_id).outerjoin(Q, join)
                             .where(*conds).order_by(R.submitted_at.desc(), R.id).limit(limit).offset(offset))).all()
    ids = [r.id for r, _, _ in rows]
    evs: dict = {}
    if ids:
        for e in (await db.execute(select(SurveyEvidence).where(SurveyEvidence.response_id.in_(ids)))).scalars():
            evs.setdefault(e.response_id, []).append({"id": str(e.id), "question": e.question_key,
                                                       "status": e.status.value, "size": e.size_bytes,
                                                       "has_exif": bool(e.meta.get("taken_at") or e.meta.get("lat"))})
    defs: dict = {}
    items = []
    for r, q, title in rows:
        key = (r.survey_id, r.survey_version)
        if key not in defs:
            defs[key] = {x.key: x for x in (await definition_of(db, await db.get(Survey, r.survey_id), r.survey_version)).questions}
        qm = defs[key]
        items.append({"id": str(r.id), "survey": title, "submitted_at": r.submitted_at, "h3_index": r.h3_index,
                      "mode": r.mode, "duration_seconds": round(r.duration_seconds), "gps_accuracy_m": r.gps_accuracy,
                      "quality_status": r.quality_status.value, "score": q.score if q else None,
                      "sub_scores": q.sub_scores if q else {}, "reasons": [reason_out(x) | {"raw": x} for x in (q.reasons if q else [])],
                      "evaluator": q.evaluator if q else None, "weight": r.weight,
                      "overrides": r.overrides, "none_slots": r.none_slots, "contributor_token": r.contributor_hash[:12],
                      "answers": [{"question": qm[k].short or qm[k].text, "answer": describe_answer(qm[k], v)}
                                  for k, v in r.answers.items() if k in qm],
                      "evidence": evs.get(r.id, [])})
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def respondents(db: AsyncSession, tenant_id: uuid.UUID, limit: int) -> dict:
    personas = dict((await db.execute(select(RespondentTrust.persona, func.count()).where(
        RespondentTrust.tenant_id == tenant_id).group_by(RespondentTrust.persona))).all())
    rows = (await db.execute(select(RespondentTrust, User.pseudonym).join(User, User.id == RespondentTrust.user_id)
                             .where(RespondentTrust.tenant_id == tenant_id, RespondentTrust.n_responses >= 3)
                             .order_by(RespondentTrust.trust).limit(limit))).all()
    return {"personas": personas,
            "lowest_trust": [{"pseudonym": ps, "trust": round(t.trust, 2), "persona": t.persona, "flags": t.flags,
                              "responses": t.n_responses, "trend": round(t.trend, 3)} for t, ps in rows]}


async def release(db: AsyncSession, tenant_id: uuid.UUID, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    rs = (await db.execute(select(SurveyResponse).where(SurveyResponse.tenant_id == tenant_id,
                                                         SurveyResponse.id.in_(ids)).with_for_update())).scalars().all()
    from app.models.survey import SurveyAnswer
    for r in rs:
        r.quality_status, r.weight = QualityStatus.pending, 0.0
        await db.execute(update(SurveyResponseQuality).where(SurveyResponseQuality.response_id == r.id).values(
            evaluator="auto", override_by=None))
        await db.execute(update(SurveyAnswer).where(SurveyAnswer.response_id == r.id).values(
            status=QualityStatus.pending, weight=0.0))
    return [r.id for r in rs]


async def profile_events(db: AsyncSession, survey_id: uuid.UUID) -> list[dict]:
    rows = (await db.execute(select(ProfileEvent).where(ProfileEvent.survey_id == survey_id)
                             .order_by(ProfileEvent.created_at.desc()).limit(50))).scalars().all()
    return [{"generation": e.generation, "method": e.method, "added": e.added, "retired": e.retired,
             "details": e.details, "at": e.created_at} for e in rows]