import hashlib
import hmac
import io
import json
import logging
import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone

import h3 as h3lib
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import contributor_token
from app.core.survey_settings import survey_settings as sv
from app.models.enums import ConsentType, QualityStatus, SurveyStatus
from app.models.survey import (RespondentTrust, Survey, SurveyAnswer, SurveyEvidence, SurveyProfile,
                               SurveyResponse, SurveyResponseQuality, SurveyVersion)
from app.models.tenancy import User
from app.schemas.surveys import SurveySubmit
from app.services import survey_profiles as profiles
from app.services import survey_trust
from app.services.acoustics import clamp01
from app.services.geo import cell_for, get_or_create_cell, ring, to_local
from app.services.survey_schema import (SurveyDefinition, all_rules, answer_rows, check_rules, coerce_answer,
                                        merge_profiles, public_view)
from app.services.user import latest_consents
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)
_USABLE = [QualityStatus.validated, QualityStatus.indicative]


def _offer_key(tid, uid, sid) -> str:
    return f"t:{tid}:svoffer:{uid}:{sid}"


def unit_hash(tenant_id: uuid.UUID, ref: str) -> str:
    s = unicodedata.normalize("NFKD", ref).encode("ascii", "ignore").decode().lower()
    s = " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())
    return hmac.new(settings.HASH_PEPPER.get_secret_value().encode(), f"unit:{tenant_id}:{s}".encode(),
                    hashlib.sha256).hexdigest()


async def require_consent(db: AsyncSession, user: User, photo: bool = False) -> None:
    c = await latest_consents(db, user.id)
    for t, code in [(ConsentType.survey_data, "survey_consent_required"), (ConsentType.geolocation, "consent_required"),
                    *([(ConsentType.photo, "photo_consent_required")] if photo else [])]:
        if t not in c or not c[t].granted:
            raise HTTPException(403, code)


def in_scope(scope: dict | None, h3_index: str) -> bool:
    if not scope or not scope.get("cells"):
        return True
    return h3lib.cell_to_parent(h3_index, int(scope["res"])) in set(scope["cells"])


async def definition_of(db: AsyncSession, survey: Survey, version: int | None = None) -> SurveyDefinition:
    ver = (await db.execute(select(SurveyVersion).where(
        SurveyVersion.survey_id == survey.id, SurveyVersion.version == (version or survey.current_version)))).scalar_one()
    return SurveyDefinition.model_validate(ver.definition)


def _open(s: Survey, now: datetime) -> bool:
    return s.status == SurveyStatus.active and (s.starts_at is None or s.starts_at <= now) and (s.ends_at is None or s.ends_at >= now)


def _survey_card(s: Survey, defn: SurveyDefinition, need: float) -> dict:
    return {"id": str(s.id), "title": s.title, "description": s.description, "domain": s.domain.value, "unit": s.unit,
            "n_questions": len(defn.questions), "version": s.current_version,
            "estimated_minutes": max(1, round(len(defn.slots) * 0.5 + len(defn.questions) * sv.SV_ESTIMATED_SECONDS_PER_QUESTION / 60)),
            "needs_evidence": any(q.evidence == "required" for q in defn.questions), "need": round(need, 3)}


async def list_available(db: AsyncSession, user: User, lat: float | None, lon: float | None) -> list[dict]:
    now = datetime.now(timezone.utc)
    h3_index = cell_for(lat, lon) if lat is not None and lon is not None else None
    surveys = [s for s in (await db.execute(select(Survey).where(
        Survey.tenant_id == user.tenant_id, Survey.status == SurveyStatus.active))).scalars() if _open(s, now)]
    if not surveys:
        return []
    ring_n: dict = {}
    if h3_index:
        ring_n = dict((await db.execute(select(SurveyResponse.survey_id, func.count()).where(
            SurveyResponse.h3_index.in_(ring(h3_index, 2)), SurveyResponse.submitted_at >= now - timedelta(days=90),
            SurveyResponse.quality_status.in_(_USABLE)).group_by(SurveyResponse.survey_id))).all())
    out = []
    for s in surveys:
        if h3_index and not in_scope(s.geo_scope, h3_index):
            continue
        if s.cooldown_days > 0 and s.unit in ("person", "household", "dwelling"):
            done = (await db.execute(select(func.count()).where(
                SurveyResponse.user_id == user.id, SurveyResponse.survey_id == s.id,
                SurveyResponse.submitted_at >= now - timedelta(days=s.cooldown_days),
                SurveyResponse.quality_status != QualityStatus.rejected))).scalar_one()
            if done:
                continue
        need = clamp01(1 - ring_n.get(s.id, 0) / max(5, sv.SV_TARGET_PER_CELL)) if h3_index else 0.5
        out.append(_survey_card(s, await definition_of(db, s), need))
    out.sort(key=lambda c: c["need"], reverse=True)
    return out


async def get_for_user(db: AsyncSession, redis: Redis, user: User, survey_id: uuid.UUID, lat: float | None,
                       lon: float | None) -> dict:
    s = await db.get(Survey, survey_id)
    now = datetime.now(timezone.utc)
    if s is None or s.tenant_id != user.tenant_id or not _open(s, now):
        raise HTTPException(404, "survey_not_found")
    defn = await definition_of(db, s)
    h3_index = cell_for(lat, lon) if lat is not None and lon is not None else None
    if h3_index and not in_scope(s.geo_scope, h3_index):
        raise HTTPException(409, "outside_survey_scope")
    slots, offer = [], {}
    for slot in defn.slots:
        item = {**slot.model_dump(), "profiles": []}
        if slot.mode == "profile":
            ranked = await profiles.ranked_profiles(db, user.id, s, slot.key, h3_index)
            item["profiles"] = [profiles.profile_out(defn, p) for p in ranked]
            offer[slot.key] = [str(p.id) for p in ranked]
        slots.append(item)
    await redis.set(_offer_key(s.tenant_id, user.id, s.id), json.dumps(offer), ex=7200)
    view = public_view(defn)
    view["slots"] = slots
    return {**_survey_card(s, defn, 0.5), "definition": view}


def _ack(r: SurveyResponse, duplicate: bool, evidence_keys: list[str] | None = None) -> dict:
    return {"id": str(r.id), "client_response_id": str(r.client_response_id), "quality_status": r.quality_status.value,
            "duplicate": duplicate, "evidence_required": evidence_keys or [],
            "evidence_deadline": (r.created_at + timedelta(minutes=sv.SV_EVIDENCE_WINDOW_MINUTES)).isoformat()
            if r.needs_evidence and r.created_at else None}


async def submit(db: AsyncSession, redis: Redis, user: User, p: SurveySubmit) -> dict:
    existing = (await db.execute(select(SurveyResponse).where(
        SurveyResponse.tenant_id == user.tenant_id, SurveyResponse.user_id == user.id,
        SurveyResponse.client_response_id == p.client_response_id))).scalar_one_or_none()
    if existing is not None:
        return _ack(existing, True)
    if user.participation_paused:
        raise HTTPException(403, "participation_paused")
    await require_consent(db, user)
    s = await db.get(Survey, p.survey_id)
    now = datetime.now(timezone.utc)
    if s is None or s.tenant_id != user.tenant_id or not _open(s, now):
        raise HTTPException(404, "survey_not_found")
    if p.survey_version != s.current_version:
        raise HTTPException(409, "survey_version_changed")
    if p.submitted_at > now + timedelta(minutes=5):
        raise HTTPException(422, "timestamp_in_future")
    if now - p.submitted_at > timedelta(hours=settings.MAX_MEASUREMENT_AGE_HOURS) or p.started_at > p.submitted_at:
        raise HTTPException(422, "invalid_timestamps")
    defn = await definition_of(db, s, p.survey_version)
    h3_index = cell_for(p.latitude, p.longitude)
    if not in_scope(s.geo_scope, h3_index):
        raise HTTPException(422, "outside_survey_scope")
    uh = unit_hash(user.tenant_id, p.unit_ref) if p.unit_ref and p.unit_ref.strip() else None
    day_start = datetime.combine(to_local(now).date(), datetime.min.time(), to_local(now).tzinfo).astimezone(timezone.utc)
    per_day = (await db.execute(select(func.count()).where(
        SurveyResponse.user_id == user.id, SurveyResponse.created_at >= day_start))).scalar_one()
    if per_day >= sv.SV_MAX_PER_DAY:
        raise HTTPException(429, "daily_survey_cap_reached")
    if s.cooldown_days > 0:
        q = select(func.count()).where(
            SurveyResponse.user_id == user.id, SurveyResponse.survey_id == s.id,
            SurveyResponse.submitted_at >= now - timedelta(days=s.cooldown_days),
            SurveyResponse.quality_status != QualityStatus.rejected,
            SurveyResponse.unit_ref_hash == uh if uh else SurveyResponse.unit_ref_hash.is_(None))
        if (await db.execute(q)).scalar_one():
            raise HTTPException(409, "already_answered")

    # --- fusion profils + corrections + réponses directes
    qm = {q.key: q for q in defn.questions}
    answers: dict = {}
    none_slots, picked_ids = [], {}
    for slot in defn.slots:
        pick = p.picks.get(slot.key)
        if pick is None:
            continue
        if pick.none:
            if not slot.allow_none:
                raise HTTPException(422, {"code": "none_not_allowed", "slot": slot.key})
            none_slots.append(slot.key)
        elif pick.profile_ids and slot.mode == "profile":
            ids = list(dict.fromkeys(pick.profile_ids))
            if len(ids) > 1 and not slot.multi:
                raise HTTPException(422, {"code": "slot_single_choice", "slot": slot.key})
            rows = {pr.id: pr for pr in (await db.execute(select(SurveyProfile).where(
                SurveyProfile.id.in_(ids), SurveyProfile.survey_id == s.id, SurveyProfile.slot == slot.key))).scalars()}
            if len(rows) != len(ids):
                raise HTTPException(404, "profile_not_found")
            answers.update(merge_profiles(defn, slot.key, [rows[i].answers for i in ids]))
            picked_ids[slot.key] = ids
    custom: dict = {}
    overridden: list[str] = []
    direct_keys: list[str] = []
    for src in (p.overrides, p.direct):
        for k, raw in src.items():
            q = qm.get(k)
            if q is None:
                raise HTTPException(422, {"code": "unknown_question", "question": k})
            try:
                v = coerce_answer(q, raw)
            except ValueError as e:
                raise HTTPException(422, {"code": "invalid_answer", "question": k, "error": str(e)})
            if k in answers:
                if not q.allow_override:
                    raise HTTPException(422, {"code": "override_not_allowed", "question": k})
                if answers[k] != v:
                    overridden.append(k)
            else:
                direct_keys.append(k)
            answers[k] = v
            custom[k] = v

    missing = [q.key for q in defn.questions if q.required and q.key not in answers]
    if missing:
        raise HTTPException(422, {"code": "missing_answers", "questions": missing})

    # --- cohérence interne : les règles « reject » bloquent dès la saisie (l'utilisateur corrige)
    hits = check_rules(all_rules(defn), answers)
    rejects = [h for h in hits if h["severity"] == "reject"]
    if rejects:
        akey = f"svq:att:{user.tenant_id}:{user.id}"
        await redis.incr(akey)
        await redis.expire(akey, 86400 * sv.ST_WINDOW_DAYS)
        raise HTTPException(422, {"code": "inconsistent_answers", "rules": rejects})
    flags = [h for h in hits if h["severity"] == "warn"]

    slot_of = {q.key: q.slot for q in defn.questions}
    overridden_slots = {slot_of[k] for k in overridden}
    mode = "direct" if not picked_ids else ("mixed" if custom else "profile")
    fp = hashlib.sha1(json.dumps(custom, sort_keys=True, default=str).encode()).hexdigest()[:40] \
        if len(custom) >= 3 else None
    evidence_keys = [q.key for q in defn.questions if q.evidence == "required" and q.key in answers]
    local = to_local(p.submitted_at)
    duration = min(14400.0, max(0.0, (p.submitted_at - p.started_at).total_seconds()))

    raw_offer = await redis.get(_offer_key(user.tenant_id, user.id, s.id))
    offered = json.loads(raw_offer) if raw_offer else {}

    try:
        cell = await get_or_create_cell(db, user.tenant_id, h3_index)
        r = SurveyResponse(
            id=uuid.uuid4(), tenant_id=user.tenant_id, survey_id=s.id, survey_version=p.survey_version,
            user_id=user.id, contributor_hash=contributor_token(user.tenant_id, user.id, f"{local:%Y-%m}"),
            client_response_id=p.client_response_id, unit_ref_hash=uh, started_at=p.started_at,
            submitted_at=p.submitted_at, local_date=local.date(), cell_id=cell.id, h3_index=h3_index,
            gps_accuracy=p.gps_accuracy_m, duration_seconds=duration, mode=mode,
            profile_picks={k: [str(i) for i in v] for k, v in picked_ids.items()}, answers=answers,
            overrides=overridden, none_slots=none_slots, custom_fingerprint=fp, flags=flags,
            features={"n_slots_picked": len(picked_ids), "n_overrides": len(overridden),
                      "n_direct": len(direct_keys), "n_questions": len(answers)},
            needs_evidence=bool(evidence_keys), quality_status=QualityStatus.pending)
        db.add(r)
        await db.flush()
        for row in answer_rows(defn, answers):
            db.add(SurveyAnswer(tenant_id=user.tenant_id, response_id=r.id, survey_id=s.id, domain=s.domain.value,
                                cell_id=cell.id, h3_index=h3_index, local_date=local.date(),
                                answered_at=p.submitted_at, contributor_hash=r.contributor_hash, unit_ref_hash=uh,
                                status=QualityStatus.pending, weight=0.0, **row))
        await profiles.credit_offer(db, offered, picked_ids, overridden_slots)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        again = (await db.execute(select(SurveyResponse).where(
            SurveyResponse.tenant_id == user.tenant_id, SurveyResponse.user_id == user.id,
            SurveyResponse.client_response_id == p.client_response_id))).scalar_one_or_none()
        if again is None:
            raise HTTPException(409, "conflict")
        return _ack(again, True)
    await db.refresh(r)
    await redis.delete(_offer_key(user.tenant_id, user.id, s.id))
    if not r.needs_evidence:
        _enqueue(user.tenant_id, r.id)
    return _ack(r, False, evidence_keys)


def _enqueue(tenant_id: uuid.UUID, response_id: uuid.UUID, countdown: int = 0) -> None:
    try:
        celery_app.send_task("surveys.evaluate", args=[str(tenant_id), str(response_id)], countdown=countdown)
    except Exception:
        log.exception("survey_enqueue_failed")      # le balayeur reprendra la réponse


# ------------------------------------------------------------------ preuves photo

_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"))


def _sniff(data: bytes) -> str | None:
    for sig, mime in _MAGIC:
        if data.startswith(sig):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _dms(v, ref) -> float | None:
    try:
        d, m, s = (float(x) for x in v)
        out = d + m / 60 + s / 3600
        return -out if ref in ("S", "W") else out
    except Exception:
        return None


def _exif(data: bytes) -> dict:
    """Date et position de prise de vue si présentes (Pillow facultatif)."""
    meta: dict = {}
    try:
        from PIL import Image
        from app.services.geo import TZ
        im = Image.open(io.BytesIO(data))
        meta["width"], meta["height"] = im.size
        ex = im.getexif()
        raw = ex.get_ifd(0x8769).get(36867) or ex.get(306)
        if raw:
            meta["taken_at"] = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S").replace(tzinfo=TZ).isoformat()
        gps = ex.get_ifd(0x8825)
        if gps and 2 in gps and 4 in gps:
            lat, lon = _dms(gps[2], gps.get(1)), _dms(gps[4], gps.get(3))
            if lat is not None and lon is not None:
                meta["lat"], meta["lon"] = lat, lon
    except Exception:
        pass
    return meta


async def add_evidence(db: AsyncSession, user: User, response_id: uuid.UUID, question_key: str,
                       data: bytes) -> dict:
    await require_consent(db, user, photo=True)
    r = await db.get(SurveyResponse, response_id)
    if r is None or r.user_id != user.id:
        raise HTTPException(404, "response_not_found")
    now = datetime.now(timezone.utc)
    if r.evidence_finalized_at is not None or now > r.created_at + timedelta(minutes=sv.SV_EVIDENCE_WINDOW_MINUTES):
        raise HTTPException(409, "evidence_window_closed")
    defn = await definition_of(db, (await db.get(Survey, r.survey_id)), r.survey_version)
    q = next((x for x in defn.questions if x.key == question_key), None)
    if q is None or q.evidence == "none" or question_key not in r.answers:
        raise HTTPException(422, "evidence_not_expected")
    if not data or len(data) > sv.SV_EVIDENCE_MAX_BYTES:
        raise HTTPException(413, "evidence_too_large")
    mime = _sniff(data)
    if mime is None:
        raise HTTPException(415, "unsupported_image")
    digest = hashlib.sha256(data).hexdigest()
    have = (await db.execute(select(SurveyEvidence).where(SurveyEvidence.response_id == r.id))).scalars().all()
    for e in have:
        if e.sha256 == digest:
            return {"id": str(e.id), "duplicate": True}
    if len(have) >= sv.SV_EVIDENCE_PER_RESPONSE:
        raise HTTPException(409, "too_many_evidence")
    ev = SurveyEvidence(tenant_id=user.tenant_id, response_id=r.id, question_key=question_key, sha256=digest,
                        mime=mime, size_bytes=len(data), content=data, meta=_exif(data))
    db.add(ev)
    await db.commit()
    await db.refresh(ev)
    return {"id": str(ev.id), "duplicate": False, "has_exif": bool(ev.meta.get("taken_at") or ev.meta.get("lat"))}


async def finalize_evidence(db: AsyncSession, user: User, response_id: uuid.UUID) -> dict:
    r = await db.get(SurveyResponse, response_id, with_for_update=True)
    if r is None or r.user_id != user.id:
        raise HTTPException(404, "response_not_found")
    if r.evidence_finalized_at is None:
        r.evidence_finalized_at = datetime.now(timezone.utc)
        await db.commit()
        _enqueue(user.tenant_id, r.id)
    return {"ok": True}


# ------------------------------------------------------------------ lecture côté répondant

REASONS = {
    "too_fast": ("Réponses données beaucoup trop vite", "Lisez les profils avant de choisir."),
    "fast_answer": ("Rythme de saisie rapide", "Prenez quelques secondes de plus par rubrique."),
    "straight_lining": ("Mêmes notes partout", "Variez les notes quand la situation le justifie."),
    "all_yes": ("« Oui » à presque tout", None),
    "gps_accuracy_too_low": ("Position GPS trop imprécise", "Attendez un meilleur signal ou sortez à l'air libre."),
    "impossible_travel": ("Déplacement impossible depuis votre dernière réponse", None),
    "peer_outlier": ("Très éloigné des réponses voisines", "Vérifiez vos chiffres."),
    "peer_unusual": ("Réponse rare dans votre secteur", None),
    "insufficient_peers": ("Peu de réponses voisines pour comparer", None),
    "reference_gap": ("Écart avec la valeur de référence", None),
    "evidence_missing": ("Photo demandée manquante", "Ajoutez la photo dans les 30 minutes."),
    "evidence_duplicate": ("Photo déjà utilisée par quelqu'un d'autre", "Prenez une photo qui vous est propre."),
    "evidence_reused": ("Photo déjà utilisée dans une autre réponse", None),
    "evidence_time_mismatch": ("La date de la photo ne correspond pas", "Prenez la photo au moment de répondre."),
    "evidence_location_mismatch": ("Le lieu de la photo ne correspond pas", None),
    "new_respondent": ("Nouveau répondant", None),
    "behavior_shift": ("Très différent de vos réponses habituelles", None),
    "copy_pattern": ("Mêmes réponses libres que d'autres répondants", None),
    "low_score": ("Score global trop bas", None),
    "admin_override": ("Décision d'un administrateur", None),
}


def reason_out(x: dict) -> dict:
    code = x.get("code", "")
    label, tip = REASONS.get(code, (code, None))
    if code == "rule_warning":
        label = x.get("message") or "Réponses peu cohérentes entre elles"
    return {"code": code, "label": label, "tip": tip, "severity": x.get("severity")}


async def my_history(db: AsyncSession, user: User, limit: int, offset: int) -> list[dict]:
    rows = (await db.execute(
        select(SurveyResponse, SurveyResponseQuality, Survey.title, Survey.domain)
        .join(Survey, Survey.id == SurveyResponse.survey_id)
        .outerjoin(SurveyResponseQuality, SurveyResponseQuality.response_id == SurveyResponse.id)
        .where(SurveyResponse.tenant_id == user.tenant_id, SurveyResponse.user_id == user.id)
        .order_by(SurveyResponse.submitted_at.desc()).limit(limit).offset(offset))).all()
    return [{"id": str(r.id), "survey_id": str(r.survey_id), "title": t, "domain": d.value,
             "submitted_at": r.submitted_at, "quality_status": r.quality_status.value,
             "score": q.score if q else None, "mode": r.mode,
             "reasons": [reason_out(x) for x in (q.reasons if q else [])
                         if x.get("code") not in ("insufficient_peers", "new_respondent")],
             "needs_evidence": r.needs_evidence and r.evidence_finalized_at is None,
             "location_precision": "zone"} for r, q, t, d in rows]


async def response_detail(db: AsyncSession, user: User, response_id: uuid.UUID) -> dict:
    r = await db.get(SurveyResponse, response_id)
    if r is None or r.user_id != user.id:
        raise HTTPException(404, "response_not_found")
    s = await db.get(Survey, r.survey_id)
    defn = await definition_of(db, s, r.survey_version)
    q = (await db.execute(select(SurveyResponseQuality).where(SurveyResponseQuality.response_id == r.id))).scalar_one_or_none()
    evs = (await db.execute(select(SurveyEvidence).where(SurveyEvidence.response_id == r.id))).scalars().all()
    from app.services.survey_schema import describe_answer
    qm = {x.key: x for x in defn.questions}
    return {"id": str(r.id), "title": s.title, "submitted_at": r.submitted_at, "quality_status": r.quality_status.value,
            "score": q.score if q else None, "sub_scores": q.sub_scores if q else {},
            "reasons": [reason_out(x) for x in (q.reasons if q else [])],
            "answers": [{"question": qm[k].short or qm[k].text, "answer": describe_answer(qm[k], v)}
                        for k, v in r.answers.items() if k in qm],
            "evidence": [{"id": str(e.id), "question": e.question_key, "status": e.status.value} for e in evs]}


async def trust_of(db: AsyncSession, user: User) -> dict:
    return survey_trust.view(await db.get(RespondentTrust, user.id))


# ------------------------------------------------------------------ vie privée

async def erase_user_survey_data(db: AsyncSession, user: User) -> None:
    ids = select(SurveyResponse.id).where(SurveyResponse.user_id == user.id)
    await db.execute(delete(SurveyEvidence).where(SurveyEvidence.response_id.in_(ids)))
    await db.execute(update(SurveyResponse).where(SurveyResponse.user_id == user.id).values(
        user_id=None, unit_ref_hash=None, custom_fingerprint=None))
    await db.execute(update(SurveyAnswer).where(SurveyAnswer.contributor_hash.in_(
        select(SurveyResponse.contributor_hash).where(SurveyResponse.user_id.is_(None)).limit(0))).values(unit_ref_hash=None))


async def privacy_sweep(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    now = datetime.now(timezone.utc)
    ev = await db.execute(update(SurveyEvidence).where(
        SurveyEvidence.tenant_id == tenant_id, SurveyEvidence.content.is_not(None),
        SurveyEvidence.created_at < now - timedelta(days=sv.SV_EVIDENCE_RETENTION_DAYS)).values(content=None))
    cut = now - timedelta(days=settings.IDENTITY_UNLINK_DAYS)
    ids = select(SurveyResponse.id).where(
        SurveyResponse.tenant_id == tenant_id, SurveyResponse.submitted_at < cut, SurveyResponse.user_id.is_not(None),
        SurveyResponse.quality_status != QualityStatus.pending).limit(20000)
    un = await db.execute(update(SurveyResponse).where(SurveyResponse.id.in_(ids)).values(
        user_id=None, unit_ref_hash=None, custom_fingerprint=None))
    old = await db.execute(update(SurveyAnswer).where(
        SurveyAnswer.tenant_id == tenant_id, SurveyAnswer.unit_ref_hash.is_not(None), SurveyAnswer.answered_at < cut)
        .values(unit_ref_hash=None))
    return {"evidence_purged": ev.rowcount or 0, "identities_unlinked": un.rowcount or 0,
            "unit_refs_cleared": old.rowcount or 0}