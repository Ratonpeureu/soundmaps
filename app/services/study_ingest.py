import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import contributor_token
from app.core.study_settings import study_settings as sts
from app.models.enums import ParticipationStatus, QualityStatus, RewardStatus, StudyStatus
from app.models.study import (Study, StudyAsset, StudyParticipation, StudyResponse, StudyReward, StudySponsor,
                              StudyVersion, UserStudyProfile)
from app.models.tenancy import User
from app.services import study_rewards as rwd
from app.services import study_targeting as tgt
from app.services.study_quality import QFacts, evaluate as quality_evaluate
from app.services.study_schema import (ANSWERABLE, ATTRS, KIND_LABELS, SPONSOR_LABELS, RewardCfg, StudyDefinition,
                                       Targeting, all_blocks, attention_stats, clean_answers, expected_seconds,
                                       public_view, screen_check, visible_blocks)

COUNTED = [QualityStatus.validated, QualityStatus.indicative, QualityStatus.pending]
PHONE_RE = re.compile(r"^\+?[0-9 ()-]{6,20}$")


def _utc():
    return datetime.now(timezone.utc)


async def require_consent(db: AsyncSession, user: User) -> None:
    if user.id not in await tgt.consented_ids(db, user.tenant_id, [user.id]):
        raise HTTPException(403, "study_consent_required")


def is_open(s: Study, now: datetime) -> bool:
    return s.status == StudyStatus.active and (s.starts_at is None or s.starts_at <= now) and (s.ends_at is None or s.ends_at > now)


async def definition_of(db: AsyncSession, s: Study, version: int | None = None) -> StudyDefinition:
    v = (await db.execute(select(StudyVersion).where(StudyVersion.study_id == s.id,
                                                      StudyVersion.version == (version or s.current_version)))).scalar_one()
    return StudyDefinition.model_validate(v.definition)


def sponsor_brief(sp: StudySponsor) -> dict:
    return {"id": str(sp.id), "name": sp.name, "kind": sp.kind.value, "kind_label": SPONSOR_LABELS[sp.kind.value],
            "logo_asset_id": str(sp.logo_asset_id) if sp.logo_asset_id else None, "verified": sp.verified, "sector": sp.sector}


def sponsor_full(sp: StudySponsor, studies: list | None = None) -> dict:
    return {**sponsor_brief(sp), "tagline": sp.tagline, "description": sp.description, "city": sp.city, "country": sp.country,
            "website": sp.website, "contact_public": sp.contact_public,
            "cover_asset_id": str(sp.cover_asset_id) if sp.cover_asset_id else None, "activities": sp.activities or [],
            "facts": sp.facts or {}, "member_since": sp.created_at, "studies": studies or [], "studies_count": len(studies or [])}


async def _valid_count(db: AsyncSession, study_id) -> int:
    return (await db.execute(select(func.count()).where(StudyResponse.study_id == study_id, StudyResponse.outcome == "completed",
                                                         StudyResponse.quality_status.in_(COUNTED)))).scalar_one()


def card_of(s: Study, sp: StudySponsor, part, ev, valid: int) -> dict:
    cfg = rwd.cfg_of(s)
    return {"id": str(s.id), "title": s.title, "summary": s.summary, "kind": s.kind.value, "kind_label": KIND_LABELS[s.kind.value],
            "sponsor": sponsor_brief(sp), "cover_asset_id": str(s.cover_asset_id) if s.cover_asset_id else None,
            "estimated_minutes": s.estimated_minutes, "starts_at": s.starts_at, "ends_at": s.ends_at, "tags": s.tags or [],
            "quota_left": max(0, s.quota_total - valid) if s.quota_total else None, "reward": rwd.card(cfg),
            "invited": bool(part and part.source == "invited" and part.status in (ParticipationStatus.invited, ParticipationStatus.seen)),
            "why": (ev or {}).get("matched", [])[:3], "participation_status": part.status.value if part else None}


async def _load(db: AsyncSession, user: User, study_id):
    row = (await db.execute(select(Study, StudySponsor).join(StudySponsor, StudySponsor.id == Study.sponsor_id)
                            .where(Study.id == study_id, Study.tenant_id == user.tenant_id))).first()
    if row is None or row[0].status == StudyStatus.draft:
        raise HTTPException(404, "study_not_found")
    part = (await db.execute(select(StudyParticipation).where(StudyParticipation.study_id == study_id,
                                                              StudyParticipation.user_id == user.id))).scalar_one_or_none()
    return row[0], row[1], part


async def list_available(db: AsyncSession, user: User, lat, lon) -> list[dict]:
    await require_consent(db, user)
    now = _utc()
    rows = (await db.execute(select(Study, StudySponsor).join(StudySponsor, StudySponsor.id == Study.sponsor_id).where(
        Study.tenant_id == user.tenant_id, Study.status == StudyStatus.active, StudySponsor.active.is_(True))
        .order_by(Study.ends_at))).all()
    if not rows:
        return []
    parts = {p.study_id: p for p in (await db.execute(select(StudyParticipation).where(StudyParticipation.user_id == user.id))).scalars()}
    counts = dict((await db.execute(select(StudyResponse.study_id, func.count()).where(
        StudyResponse.tenant_id == user.tenant_id, StudyResponse.outcome == "completed", StudyResponse.quality_status.in_(COUNTED))
        .group_by(StudyResponse.study_id))).all())
    prof = (await tgt.derive_bulk(db, user.tenant_id, [user.id], now, {user.id: (lat, lon)} if lat is not None and lon is not None else None))[user.id]
    out = []
    for s, sp in rows:
        if not is_open(s, now):
            continue
        p = parts.get(s.id)
        if p and p.status in (ParticipationStatus.completed, ParticipationStatus.screened_out, ParticipationStatus.declined):
            continue
        valid = counts.get(s.id, 0)
        if s.quota_total and valid >= s.quota_total:
            continue
        t = Targeting.model_validate(s.targeting or {})
        invited = bool(p and p.source == "invited")
        started = bool(p and p.status == ParticipationStatus.started)
        if t.sampling.mode == "invite_only" and not invited and not started:
            continue
        ev = tgt.evaluate(t, prof)
        if not ev["eligible"] and not started and not invited:
            continue
        c = card_of(s, sp, p, ev, valid)
        c["_score"] = ev["score"] + (1 if c["invited"] else 0)
        out.append(c)
    out.sort(key=lambda c: (-c["_score"], c["ends_at"]))
    for c in out:
        c.pop("_score")
    return out


async def _gate(db, user, study_id, lat=None, lon=None):
    await require_consent(db, user)
    s, sp, part = await _load(db, user, study_id)
    now = _utc()
    if not is_open(s, now):
        raise HTTPException(409, "study_closed")
    if part and part.status in (ParticipationStatus.completed, ParticipationStatus.screened_out):
        raise HTTPException(409, "already_participated")
    prof = (await tgt.derive_bulk(db, user.tenant_id, [user.id], now, {user.id: (lat, lon)} if lat is not None and lon is not None else None))[user.id]
    ev = tgt.evaluate(Targeting.model_validate(s.targeting or {}), prof)
    if not ev["eligible"] and not (part and part.source == "invited"):
        raise HTTPException(403, "study_not_eligible")
    return s, sp, part, ev, prof, now


async def get_for_user(db: AsyncSession, user: User, study_id, lat=None, lon=None) -> dict:
    s, sp, part, ev, prof, now = await _gate(db, user, study_id, lat, lon)
    if part and part.status == ParticipationStatus.invited:
        part.status, part.seen_at = ParticipationStatus.seen, now
        await db.commit()
    d = await definition_of(db, s)
    cfg, pv = rwd.cfg_of(s), rwd.privacy_of(s)
    return {**card_of(s, sp, part, ev, await _valid_count(db, s.id)), "description": s.description, "terms": s.terms,
            "version": s.current_version, "rules": rwd.rules_sections(s, cfg, pv), "definition": public_view(d),
            "collect_contact": pv.collect_contact, "contact_label": cfg.delivery.contact_label if cfg.delivery.requires_contact else None,
            "contact_purpose": pv.contact_purpose, "sponsor_full": sponsor_full(sp), "started_at": part.started_at if part else None}


async def start(db: AsyncSession, user: User, study_id, lat=None, lon=None) -> dict:
    s, sp, part, ev, prof, now = await _gate(db, user, study_id, lat, lon)
    if s.quota_total and await _valid_count(db, s.id) >= s.quota_total:
        raise HTTPException(409, "study_full")
    if part is None:
        part = StudyParticipation(tenant_id=user.tenant_id, study_id=s.id, user_id=user.id, source="organic", match_score=ev["score"])
        db.add(part)
    if part.status == ParticipationStatus.declined:
        raise HTTPException(409, "already_participated")
    if part.started_at is None:
        part.started_at = now
    part.status = ParticipationStatus.started
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
    return {"started_at": part.started_at, "version": s.current_version}


async def decline(db: AsyncSession, user: User, study_id) -> dict:
    s, sp, part = await _load(db, user, study_id)
    if part is None:
        part = StudyParticipation(tenant_id=user.tenant_id, study_id=s.id, user_id=user.id, source="organic")
        db.add(part)
    if part.status not in (ParticipationStatus.completed, ParticipationStatus.screened_out):
        part.status, part.finished_at = ParticipationStatus.declined, _utc()
    await db.commit()
    return {"ok": True}


async def check(db: AsyncSession, user: User, study_id, answers: dict) -> dict:
    s, sp, part = await _load(db, user, study_id)
    if part is None or part.started_at is None:
        raise HTTPException(409, "study_not_started")
    d = await definition_of(db, s)
    try:
        clean, _ = clean_answers(d, answers, partial=True)
    except ValueError as e:
        raise HTTPException(422, {"code": "invalid_answer", "error": str(e)})
    ok, msg = screen_check(d, clean)
    return {"ok": ok, "message": msg}


def _snapshot(fields: list, prof: dict) -> dict:
    out = {}
    for f in fields:
        if f == "age_band":
            out[f] = tgt.age_band(prof.get("age"))
        elif f == "household_size":
            n = prof.get("household_size")
            out[f] = None if n is None else "1-3" if n <= 3 else "4-6" if n <= 6 else "7-10" if n <= 10 else "11+"
        else:
            out[f] = prof.get(f)
    return {k: v for k, v in out.items() if v is not None}


def _ack(r: StudyResponse, rw, duplicate=False, message=None) -> dict:
    return {"id": str(r.id), "client_response_id": str(r.client_response_id), "status": r.quality_status.value,
            "outcome": r.outcome, "screened_out": r.outcome == "screened_out", "score": r.score, "duplicate": duplicate,
            "message": message, "reward": rwd.reward_out(rw) if rw else None, "reasons": [x["code"] for x in r.reasons or []]}


async def submit(db: AsyncSession, user: User, study_id, p) -> dict:
    prev = (await db.execute(select(StudyResponse).where(StudyResponse.tenant_id == user.tenant_id, StudyResponse.user_id == user.id,
                                                          StudyResponse.client_response_id == p.client_response_id))).scalar_one_or_none()
    if prev is not None:
        rw = (await db.execute(select(StudyReward).where(StudyReward.response_id == prev.id))).scalar_one_or_none()
        return _ack(prev, rw, True)
    await require_consent(db, user)
    if user.participation_paused:
        raise HTTPException(403, "participation_paused")
    s, sp, part = await _load(db, user, study_id)
    now = _utc()
    if not is_open(s, now):
        raise HTTPException(409, "study_closed")
    if p.study_version != s.current_version:
        raise HTTPException(409, "study_version_changed")
    if part is None or part.started_at is None:
        raise HTTPException(409, "study_not_started")
    if part.status in (ParticipationStatus.completed, ParticipationStatus.screened_out):
        raise HTTPException(409, "already_participated")
    if s.quota_total and await _valid_count(db, s.id) >= s.quota_total:
        raise HTTPException(409, "study_full")
    day0 = now - timedelta(days=1)
    if (await db.execute(select(func.count()).where(StudyResponse.user_id == user.id, StudyResponse.created_at >= day0))).scalar_one() >= sts.ST_MAX_PER_DAY:
        raise HTTPException(429, "daily_study_cap_reached")
    d = await definition_of(db, s, p.study_version)
    try:
        partial, _ = clean_answers(d, p.answers, partial=True)
    except ValueError as e:
        raise HTTPException(422, {"code": "invalid_answer", "error": str(e)})
    prof = (await tgt.derive_bulk(db, user.tenant_id, [user.id], now))[user.id]
    cfg, pv = rwd.cfg_of(s), rwd.privacy_of(s)
    duration = min(14400.0, max(0.0, (now - part.started_at).total_seconds()))
    base = dict(id=uuid.uuid4(), tenant_id=user.tenant_id, study_id=s.id, study_version=p.study_version, user_id=user.id,
                participant_token=contributor_token(user.tenant_id, user.id, f"study:{s.id}"), client_response_id=p.client_response_id,
                started_at=part.started_at, submitted_at=now, duration_seconds=duration, evaluated_at=now,
                profile_snapshot=_snapshot(pv.share_profile, prof))
    ok, msg = screen_check(d, partial)
    if not ok:
        r = StudyResponse(**base, outcome="screened_out", answers=partial, quality_status=QualityStatus.rejected, score=0.0,
                          reasons=[{"code": "screened_out", "severity": "info"}])
        part.status, part.finished_at = ParticipationStatus.screened_out, now
        db.add(r)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, "already_participated")
        return _ack(r, None, False, msg)
    try:
        clean, missing = clean_answers(d, p.answers)
    except ValueError as e:
        raise HTTPException(422, {"code": "invalid_answer", "error": str(e)})
    if missing:
        raise HTTPException(422, {"code": "missing_answers", "questions": missing})
    contact, consent = None, False
    if pv.collect_contact and p.share_contact and p.contact:
        phone = str(p.contact.get("phone", "")).strip()
        if phone and not PHONE_RE.match(phone):
            raise HTTPException(422, "invalid_phone")
        if phone:
            contact, consent = {"phone": phone, "name": str(p.contact.get("name") or "")[:80] or None}, True
    ratings, texts = [], []
    for b in all_blocks(d):
        v = clean.get(b.key)
        if v is None:
            continue
        if b.kind in ("scale", "stars", "nps"):
            ratings.append(v)
        elif b.kind == "matrix":
            ratings += list(v.values())
        elif b.kind == "product_rating":
            ratings += [x for it in v.values() for k, x in it.items() if k != "comment"]
        elif b.kind in ("text", "long_text"):
            texts.append(v)
    fp = hashlib.sha1("|".join(sorted(re.sub(r"\W+", " ", t.lower()).strip() for t in texts if len(t) >= 15)).encode()).hexdigest()[:40] if any(len(t) >= 15 for t in texts) else None
    dup = 0
    if fp:
        dup = (await db.execute(select(func.count(func.distinct(StudyResponse.participant_token))).where(
            StudyResponse.study_id == s.id, StudyResponse.fingerprint == fp, StudyResponse.user_id != user.id,
            StudyResponse.submitted_at >= now - timedelta(hours=sts.STQ_FINGERPRINT_WINDOW_HOURS)))).scalar_one()
    n_att, n_fail = attention_stats(d, clean)
    res = quality_evaluate(QFacts(duration=duration, expected=expected_seconds(d, clean), ratings=ratings, texts=texts,
                                  att_total=n_att, att_failed=n_fail, trust=prof.get("study_trust"), trust_n=prof.get("studies_done", 0), dup_count=int(dup)))
    r = StudyResponse(**base, outcome="completed", answers=clean, quality_status=res.status, score=res.score, sub_scores=res.sub_scores,
                      reasons=res.reasons, fingerprint=fp, contact=contact, contact_consent=consent)
    part.status, part.finished_at = ParticipationStatus.completed, now
    db.add(r)
    try:
        await db.flush()
        rw = await rwd.decide(db, s, r, user.id, cfg, prof)
        if rw and contact:
            rw.delivery = {**(rw.delivery or {}), "phone": contact["phone"], "name": contact.get("name")}
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "already_participated")
    return _ack(r, rw)


# ------------------------------------------------------------------ historique et récompenses
async def my_participations(db: AsyncSession, user: User) -> list[dict]:
    rows = (await db.execute(select(StudyParticipation, Study, StudySponsor).join(Study, Study.id == StudyParticipation.study_id)
                             .join(StudySponsor, StudySponsor.id == Study.sponsor_id)
                             .where(StudyParticipation.user_id == user.id, StudyParticipation.status.in_(
                                 [ParticipationStatus.started, ParticipationStatus.completed, ParticipationStatus.screened_out]))
                             .order_by(StudyParticipation.created_at.desc()).limit(100))).all()
    rws = {r.study_id: r for r in (await db.execute(select(StudyReward).where(StudyReward.user_id == user.id))).scalars()}
    return [{"study_id": str(s.id), "title": s.title, "kind_label": KIND_LABELS[s.kind.value], "sponsor": sponsor_brief(sp),
             "status": p.status.value, "started_at": p.started_at, "finished_at": p.finished_at, "ends_at": s.ends_at,
             "reward": rwd.reward_out(rws[s.id], s.title, sp.name) if s.id in rws else None} for p, s, sp in rows]


async def my_rewards(db: AsyncSession, user: User) -> dict:
    rows = (await db.execute(select(StudyReward, Study.title, StudySponsor.name).join(Study, Study.id == StudyReward.study_id)
                             .join(StudySponsor, StudySponsor.id == Study.sponsor_id).where(StudyReward.user_id == user.id)
                             .order_by(StudyReward.created_at.desc()).limit(200))).all()
    items = [rwd.reward_out(r, t, n) for r, t, n in rows]
    cnt = {k: sum(1 for i in items if i["status"] == k) for k in ("pending", "entered", "granted", "delivered", "refused", "revoked", "expired")}
    n, avg = (await db.execute(select(func.count(), func.avg(StudyResponse.score)).where(
        StudyResponse.user_id == user.id, StudyResponse.outcome == "completed", StudyResponse.quality_status.in_(COUNTED)))).one()
    ok = [i for i in items if i["status"] in ("granted", "delivered")]
    return {"summary": {**cnt, "participations": int(n), "avg_score": round(float(avg or 0), 3),
                        "points_total": sum(i["amount"] for i in ok if i["kind"] == "points"),
                        "credits_total": sum(i["amount"] for i in ok if i["kind"] == "credits"),
                        "cash_total": sum(i["amount"] for i in ok if i["kind"] in ("cash", "airtime"))}, "items": items}


async def reward_detail(db: AsyncSession, user: User, reward_id) -> dict:
    row = (await db.execute(select(StudyReward, Study, StudySponsor).join(Study, Study.id == StudyReward.study_id)
                            .join(StudySponsor, StudySponsor.id == Study.sponsor_id)
                            .where(StudyReward.id == reward_id, StudyReward.user_id == user.id))).first()
    if row is None:
        raise HTTPException(404, "reward_not_found")
    rw, s, sp = row
    cfg = RewardCfg.model_validate((rw.snapshot or {}).get("cfg") or {"enabled": True, "label": rw.label})
    return {**rwd.reward_out(rw, s.title, sp.name), "rules": rwd.rules_sections(s, cfg, rwd.privacy_of(s)),
            "contact_label": cfg.delivery.contact_label if cfg.delivery.requires_contact else None,
            "phone": (rw.delivery or {}).get("phone"), "instructions": cfg.delivery.instructions}


async def claim(db: AsyncSession, user: User, reward_id, phone: str, name: str | None) -> dict:
    rw = (await db.execute(select(StudyReward).where(StudyReward.id == reward_id, StudyReward.user_id == user.id).with_for_update())).scalar_one_or_none()
    if rw is None:
        raise HTTPException(404, "reward_not_found")
    if rw.status != RewardStatus.granted:
        raise HTTPException(409, "reward_not_claimable")
    if rw.claim_deadline_at and rw.claim_deadline_at < _utc():
        raise HTTPException(409, "claim_expired")
    if not PHONE_RE.match(phone.strip()):
        raise HTTPException(422, "invalid_phone")
    rw.delivery = {**(rw.delivery or {}), "phone": phone.strip(), "name": (name or "").strip()[:80] or None, "claimed_at": _utc().isoformat()}
    await db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ profil études déclaré
def profile_out(p: UserStudyProfile | None) -> dict:
    return {"gender": p.gender if p else None, "occupation_group": p.occupation_group if p else None,
            "education_level": p.education_level if p else None, "household_size": p.household_size if p else None,
            "income_band": p.income_band if p else None, "languages": (p.languages if p else []) or [],
            "interests": (p.interests if p else []) or []}


async def get_profile(db: AsyncSession, user: User) -> dict:
    return profile_out(await db.get(UserStudyProfile, user.id))


async def put_profile(db: AsyncSession, user: User, data: dict) -> dict:
    p = await db.get(UserStudyProfile, user.id)
    if p is None:
        p = UserStudyProfile(user_id=user.id, tenant_id=user.tenant_id, languages=[], interests=[])
        db.add(p)
    for f in ("gender", "occupation_group", "education_level", "income_band"):
        if f in data:
            v = data[f]
            if v is not None and v not in {o for o, _ in ATTRS[f]["options"]}:
                raise HTTPException(422, {"code": "invalid_value", "field": f})
            setattr(p, f, v)
    if "household_size" in data:
        p.household_size = data["household_size"]
    for f in ("languages", "interests"):
        if f in data:
            ok = {o for o, _ in ATTRS[f]["options"]}
            p.languages if f == "languages" else p.interests
            setattr(p, f, [x for x in dict.fromkeys(data[f] or []) if x in ok])
    p.updated_at = _utc()
    await db.commit()
    return profile_out(p)


async def sponsor_public(db: AsyncSession, user: User, sponsor_id) -> dict:
    sp = await db.get(StudySponsor, sponsor_id)
    if sp is None or sp.tenant_id != user.tenant_id or not sp.active:
        raise HTTPException(404, "sponsor_not_found")
    n = (await db.execute(select(func.count()).where(Study.sponsor_id == sp.id, Study.status == StudyStatus.active))).scalar_one()
    out = sponsor_full(sp)
    out["studies_count"] = int(n)
    return out


# ------------------------------------------------------------------ médias
async def serve_asset(db: AsyncSession, tenant_id, asset_id, request: Request) -> Response:
    meta = (await db.execute(select(StudyAsset.mime, StudyAsset.size_bytes, StudyAsset.sha256).where(
        StudyAsset.id == asset_id, StudyAsset.tenant_id == tenant_id))).first()
    if meta is None:
        raise HTTPException(404, "asset_not_found")
    mime, size, sha = meta
    etag = f'"{sha[:32]}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=86400", "Accept-Ranges": "bytes"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    data = (await db.execute(select(StudyAsset.content).where(StudyAsset.id == asset_id))).scalar_one()
    rng = request.headers.get("range")
    if rng and rng.startswith("bytes="):
        try:
            a, _, b = rng[6:].split(",")[0].partition("-")
            start = int(a) if a else max(0, size - int(b))
            end = min(int(b), size - 1) if (a and b) else size - 1
            if start > end or start >= size:
                raise ValueError
        except ValueError:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        return Response(data[start:end + 1], status_code=206, media_type=mime,
                        headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}"})
    return Response(data, media_type=mime, headers=headers)


# ------------------------------------------------------------------ vie privée
async def erase_user_study_data(db: AsyncSession, user: User) -> None:
    await db.execute(update(StudyResponse).where(StudyResponse.user_id == user.id).values(user_id=None, contact=None, contact_consent=False))
    # participations, récompenses et profil études : supprimés en cascade avec le compte