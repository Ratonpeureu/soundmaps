import csv
import io
import json
import random
import secrets
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.study_settings import study_settings as sts
from app.models.enums import (AssetKind, ParticipationStatus, QualityStatus, RewardStatus, StudyStatus)
from app.models.study import (Study, StudyAsset, StudyParticipation, StudyResponse, StudyReward, StudySponsor,
                              StudyVersion, StudyVoucherCode)
from app.models.tenancy import User
from app.services import study_rewards as rwd
from app.services import study_targeting as tgt
from app.services.study_ingest import definition_of
from app.services.study_schema import (ANSWERABLE, KIND_LABELS, PrivacyCfg, RewardCfg, StudyDefinition, Targeting, all_blocks,
                                       asset_ids, describe, export_columns, public_view)

USABLE = [QualityStatus.validated, QualityStatus.indicative]


def _utc():
    return datetime.now(timezone.utc)


def parse(model, raw, code):
    try:
        return model.model_validate(raw or {})
    except ValidationError as exc:
        raise HTTPException(422, {"code": code, "errors": [f"{'.'.join(map(str, e['loc']))}: {str(e['msg']).replace('Value error, ', '')}" for e in exc.errors()][:12]})


# ------------------------------------------------------------------ médias
def sniff(data: bytes):
    if data[:3] == b"\xff\xd8\xff":
        return AssetKind.image, "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return AssetKind.image, "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return AssetKind.image, "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return AssetKind.image, "image/gif"
    if data[4:8] == b"ftyp":
        return AssetKind.video, "video/mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return AssetKind.video, "video/webm"
    return None, None


async def upload_asset(db: AsyncSession, admin: User, filename: str, data: bytes, name: str | None) -> dict:
    kind, mime = sniff(data)
    if kind is None:
        raise HTTPException(415, "unsupported_media")
    if len(data) > (sts.ST_MAX_IMAGE_BYTES if kind == AssetKind.image else sts.ST_MAX_VIDEO_BYTES):
        raise HTTPException(413, "media_too_large")
    w = h = None
    if kind == AssetKind.image:
        try:
            import io as _io
            from PIL import Image
            w, h = Image.open(_io.BytesIO(data)).size
        except Exception:
            pass
    import hashlib
    a = StudyAsset(tenant_id=admin.tenant_id, kind=kind, name=(name or filename or "média")[:160], mime=mime, size_bytes=len(data),
                   sha256=hashlib.sha256(data).hexdigest(), width=w, height=h, created_by=admin.id, content=data)
    db.add(a)
    await db.flush()
    return asset_out(a)


def asset_out(a: StudyAsset) -> dict:
    return {"id": str(a.id), "kind": a.kind.value, "name": a.name, "mime": a.mime, "size_bytes": a.size_bytes,
            "width": a.width, "height": a.height, "created_at": a.created_at}


async def list_assets(db: AsyncSession, tenant_id, kind=None, limit=200) -> list[dict]:
    q = select(StudyAsset.id, StudyAsset.kind, StudyAsset.name, StudyAsset.mime, StudyAsset.size_bytes, StudyAsset.width,
               StudyAsset.height, StudyAsset.created_at).where(StudyAsset.tenant_id == tenant_id)
    if kind:
        q = q.where(StudyAsset.kind == kind)
    return [{"id": str(i), "kind": k.value, "name": n, "mime": m, "size_bytes": s, "width": w, "height": h, "created_at": c}
            for i, k, n, m, s, w, h, c in (await db.execute(q.order_by(StudyAsset.created_at.desc()).limit(limit))).all()]


async def check_assets(db: AsyncSession, tenant_id, ids: set) -> None:
    if not ids:
        return
    have = set((await db.execute(select(StudyAsset.id).where(StudyAsset.tenant_id == tenant_id, StudyAsset.id.in_(list(ids))))).scalars())
    if ids - have:
        raise HTTPException(422, {"code": "asset_not_found", "ids": [str(i) for i in ids - have]})


# ------------------------------------------------------------------ initiateurs
def sponsor_out(sp: StudySponsor) -> dict:
    return {"id": str(sp.id), "kind": sp.kind.value, "name": sp.name, "tagline": sp.tagline, "description": sp.description,
            "sector": sp.sector, "city": sp.city, "country": sp.country, "website": sp.website, "contact_public": sp.contact_public,
            "logo_asset_id": str(sp.logo_asset_id) if sp.logo_asset_id else None,
            "cover_asset_id": str(sp.cover_asset_id) if sp.cover_asset_id else None, "activities": sp.activities or [],
            "facts": sp.facts or {}, "verified": sp.verified, "active": sp.active, "created_at": sp.created_at}


async def save_sponsor(db: AsyncSession, admin: User, p: dict, sp: StudySponsor | None = None) -> StudySponsor:
    await check_assets(db, admin.tenant_id, {uuid.UUID(str(p[k])) for k in ("logo_asset_id", "cover_asset_id") if p.get(k)})
    if sp is None:
        sp = StudySponsor(tenant_id=admin.tenant_id, activities=[], facts={})
        db.add(sp)
    for k, v in p.items():
        setattr(sp, k, v)
    sp.updated_at = _utc()
    await db.flush()
    return sp


# ------------------------------------------------------------------ études
def study_out(s: Study, sp: StudySponsor | None = None, n_resp: int = 0, definition: dict | None = None) -> dict:
    out = {"id": str(s.id), "sponsor_id": str(s.sponsor_id), "sponsor_name": sp.name if sp else None, "kind": s.kind.value,
           "kind_label": KIND_LABELS[s.kind.value], "title": s.title, "summary": s.summary, "description": s.description,
           "cover_asset_id": str(s.cover_asset_id) if s.cover_asset_id else None, "status": s.status.value,
           "version": s.current_version, "starts_at": s.starts_at, "ends_at": s.ends_at, "quota_total": s.quota_total,
           "estimated_minutes": s.estimated_minutes, "targeting": s.targeting or {}, "reward": s.reward or {},
           "privacy": s.privacy or {}, "terms": s.terms, "tags": s.tags or [], "settings": s.settings or {},
           "responses": n_resp, "created_at": s.created_at, "published_at": s.published_at, "closed_at": s.closed_at}
    if definition is not None:
        out["definition"] = definition
    return out


async def get_study(db: AsyncSession, tenant_id, study_id) -> Study:
    s = await db.get(Study, study_id)
    if s is None or s.tenant_id != tenant_id:
        raise HTTPException(404, "study_not_found")
    return s


async def n_resp(db: AsyncSession, study_id) -> int:
    return (await db.execute(select(func.count()).where(StudyResponse.study_id == study_id))).scalar_one()


async def _validated_parts(db, admin, p: dict):
    d = parse(StudyDefinition, p["definition"], "invalid_definition") if p.get("definition") is not None else None
    t = parse(Targeting, p.get("targeting"), "invalid_targeting") if "targeting" in p else None
    r = parse(RewardCfg, p.get("reward"), "invalid_reward") if "reward" in p else None
    pv = parse(PrivacyCfg, p.get("privacy"), "invalid_privacy") if "privacy" in p else None
    if d is not None:
        await check_assets(db, admin.tenant_id, asset_ids(d))
    return d, t, r, pv


async def create_study(db: AsyncSession, admin: User, p: dict) -> Study:
    sp = await db.get(StudySponsor, p["sponsor_id"])
    if sp is None or sp.tenant_id != admin.tenant_id:
        raise HTTPException(404, "sponsor_not_found")
    d, t, r, pv = await _validated_parts(db, admin, p)
    if d is None:
        raise HTTPException(422, "definition_required")
    cover = p.get("cover_asset_id")
    await check_assets(db, admin.tenant_id, {cover} if cover else set())
    s = Study(tenant_id=admin.tenant_id, created_by=admin.id, sponsor_id=p["sponsor_id"], kind=p["kind"], title=p["title"].strip(),
              summary=p.get("summary"), description=p.get("description"), cover_asset_id=cover, status=StudyStatus.draft,
              starts_at=p.get("starts_at"), ends_at=p.get("ends_at"), quota_total=p.get("quota_total", 0),
              estimated_minutes=p.get("estimated_minutes", 5), terms=p.get("terms"), tags=p.get("tags") or [],
              targeting=(t or Targeting()).model_dump(mode="json"), reward=(r or RewardCfg()).model_dump(mode="json"),
              privacy=(pv or PrivacyCfg()).model_dump(mode="json"), settings={}, current_version=1)
    db.add(s)
    await db.flush()
    db.add(StudyVersion(tenant_id=admin.tenant_id, study_id=s.id, version=1, definition=d.model_dump(mode="json", exclude_none=True)))
    await db.flush()
    return s


async def update_study(db: AsyncSession, admin: User, s: Study, p: dict) -> dict:
    d, t, r, pv = await _validated_parts(db, admin, p)
    for f in ("title", "summary", "description", "starts_at", "ends_at", "quota_total", "estimated_minutes", "terms", "tags", "kind", "cover_asset_id"):
        if f in p:
            if f == "cover_asset_id" and p[f]:
                await check_assets(db, admin.tenant_id, {p[f]})
            setattr(s, f, p[f])
    if "sponsor_id" in p and p["sponsor_id"]:
        sp = await db.get(StudySponsor, p["sponsor_id"])
        if sp is None or sp.tenant_id != admin.tenant_id:
            raise HTTPException(404, "sponsor_not_found")
        s.sponsor_id = p["sponsor_id"]
    if t is not None:
        s.targeting = t.model_dump(mode="json")
    if r is not None:
        s.reward = r.model_dump(mode="json")
    if pv is not None:
        s.privacy = pv.model_dump(mode="json")
    bumped = False
    if d is not None:
        raw = d.model_dump(mode="json", exclude_none=True)
        ver = (await db.execute(select(StudyVersion).where(StudyVersion.study_id == s.id, StudyVersion.version == s.current_version))).scalar_one()
        if ver.definition != raw:
            if await n_resp(db, s.id) == 0 and s.status == StudyStatus.draft:
                ver.definition = raw
            else:
                s.current_version += 1
                db.add(StudyVersion(tenant_id=s.tenant_id, study_id=s.id, version=s.current_version, definition=raw))
                bumped = True
    s.updated_at = _utc()
    await db.flush()
    return {"version": s.current_version, "version_bumped": bumped}


async def set_status(db: AsyncSession, s: Study, status: str) -> dict:
    new, warnings, now = StudyStatus(status), [], _utc()
    if new == StudyStatus.active:
        sp = await db.get(StudySponsor, s.sponsor_id)
        if sp is None or not sp.active:
            raise HTTPException(409, "sponsor_inactive")
        if s.ends_at is None or s.ends_at <= now:
            raise HTTPException(409, "end_date_required_in_future")
        cfg = rwd.cfg_of(s)
        if cfg.enabled and cfg.kind == "voucher" and cfg.mode == "guaranteed":
            free = (await db.execute(select(func.count()).where(StudyVoucherCode.study_id == s.id, StudyVoucherCode.reward_id.is_(None)))).scalar_one()
            if free < (cfg.max_rewarded or s.quota_total or 1):
                warnings.append(f"Seulement {free} code(s) de bon disponible(s).")
        if cfg.enabled and not cfg.budget_total and not cfg.max_rewarded and not s.quota_total and cfg.mode == "guaranteed":
            warnings.append("Aucune limite de budget ni de quota : le coût de la récompense est illimité.")
        s.published_at = s.published_at or now
        s.closed_at = None
    if new == StudyStatus.closed:
        s.closed_at = now
    s.status = new
    await db.flush()
    return {"warnings": warnings}


async def duplicate(db: AsyncSession, admin: User, s: Study) -> Study:
    d = await definition_of(db, s)
    n = Study(tenant_id=s.tenant_id, created_by=admin.id, sponsor_id=s.sponsor_id, kind=s.kind, title=f"{s.title} (copie)"[:160],
              summary=s.summary, description=s.description, cover_asset_id=s.cover_asset_id, status=StudyStatus.draft, quota_total=s.quota_total,
              estimated_minutes=s.estimated_minutes, targeting=s.targeting, reward=s.reward, privacy=s.privacy, terms=s.terms,
              tags=s.tags, settings={}, current_version=1)
    db.add(n)
    await db.flush()
    db.add(StudyVersion(tenant_id=s.tenant_id, study_id=n.id, version=1, definition=d.model_dump(mode="json", exclude_none=True)))
    await db.flush()
    return n


# ------------------------------------------------------------------ résultats
def _num_stats(xs):
    xs = sorted(float(x) for x in xs)
    if not xs:
        return {}
    lo, hi = xs[0], xs[-1]
    bins = [0] * 8
    for x in xs:
        bins[min(7, int((x - lo) / (hi - lo) * 8)) if hi > lo else 0] += 1
    return {"mean": round(statistics.fmean(xs), 3), "median": statistics.median(xs), "min": lo, "max": hi,
            "p25": xs[int(.25 * (len(xs) - 1))], "p75": xs[int(.75 * (len(xs) - 1))], "hist": bins}


def agg_block(b, vals: list) -> dict:
    k, lab, n = b.kind, {o.value: o.label for o in b.options}, len(vals)
    out = {"n": n}
    if k in ("single_choice", "yes_no"):
        c = Counter(vals)
        keys = [o.value for o in b.options] if k == "single_choice" else [True, False]
        out["counts"] = [{"value": str(x), "label": lab.get(x, "Oui" if x is True else "Non" if x is False else str(x)), "n": c.get(x, 0)} for x in keys]
    elif k == "multi_choice":
        c = Counter(x for v in vals for x in v)
        out["counts"] = [{"value": o.value, "label": o.label, "n": c.get(o.value, 0)} for o in b.options]
    elif k in ("number", "slider"):
        out.update(_num_stats(vals))
    elif k in ("scale", "stars", "nps"):
        c = Counter(vals)
        out["counts"] = [{"value": str(i), "label": str(i), "n": c.get(i, 0)} for i in range(int(b.min), int(b.max) + 1)]
        out["mean"] = round(statistics.fmean(vals), 3) if vals else None
        if k == "nps" and vals:
            pr, de = sum(v >= 9 for v in vals), sum(v <= 6 for v in vals)
            out["nps"] = round(100 * (pr - de) / n, 1)
            out["promoters"], out["detractors"] = pr, de
    elif k in ("text", "long_text"):
        out["texts"] = vals[-40:]
    elif k == "ranking":
        pos = defaultdict(list)
        for v in vals:
            for i, x in enumerate(v):
                pos[x].append(i + 1)
        out["ranking"] = sorted([{"value": o.value, "label": o.label, "avg_rank": round(statistics.fmean(pos[o.value]), 2) if pos[o.value] else None,
                                  "first": sum(1 for v in vals if v and v[0] == o.value), "n": len(pos[o.value])} for o in b.options],
                                key=lambda r: (r["avg_rank"] is None, r["avg_rank"] or 0))
    elif k == "matrix":
        out["rows"] = [{"row": r.label, "counts": [{"label": c.label, "n": sum(1 for v in vals if v.get(r.value) == c.value)} for c in b.options]} for r in b.rows]
    elif k == "product_rating":
        items = []
        for i in b.items:
            crit = []
            for c in b.criteria:
                xs = [v[i.key][c.value] for v in vals if i.key in v and c.value in v[i.key]]
                crit.append({"criterion": c.label, "n": len(xs), "mean": round(statistics.fmean(xs), 2) if xs else None,
                             "counts": [Counter(xs).get(r, 0) for r in range(int(b.min), int(b.max) + 1)]})
            items.append({"item": i.label, "criteria": crit, "comments": [v[i.key]["comment"] for v in vals if i.key in v and v[i.key].get("comment")][-15:]})
        out["items"] = items
    elif k == "date":
        out["counts"] = [{"value": m, "label": m, "n": c} for m, c in sorted(Counter(v[:7] for v in vals).items())]
    return out


async def results(db: AsyncSession, s: Study, by: str | None, statuses: list[QualityStatus]) -> dict:
    d = await definition_of(db, s)
    rows = (await db.execute(select(StudyResponse.answers, StudyResponse.profile_snapshot).where(
        StudyResponse.study_id == s.id, StudyResponse.outcome == "completed", StudyResponse.quality_status.in_(statuses)).limit(20000))).all()
    groups = defaultdict(list)
    if by:
        for a, snap in rows:
            groups[str((snap or {}).get(by) or "inconnu")].append(a)
    top = [g for g, _ in sorted(groups.items(), key=lambda t: -len(t[1]))[:8]]
    qs = []
    for b in all_blocks(d):
        if b.kind not in ANSWERABLE:
            continue
        vals = [a[b.key] for a, _ in rows if b.key in a]
        item = {"key": b.key, "kind": b.kind, "text": b.text, **agg_block(b, vals)}
        if by and b.kind not in ("text", "long_text", "product_rating", "matrix", "ranking", "date"):
            item["groups"] = {g: agg_block(b, [a[b.key] for a in groups[g] if b.key in a]) for g in top}
        qs.append(item)
    return {"n": len(rows), "by": by, "groups": {g: len(groups[g]) for g in top}, "questions": qs}


def _csv(rows: list[dict], cols: list[str]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") if not isinstance(r.get(c), (list, dict)) else json.dumps(r.get(c), ensure_ascii=False) for c in cols])
    return buf.getvalue().encode("utf-8-sig")


async def export(db: AsyncSession, s: Study, fmt: str, statuses: list[QualityStatus]) -> tuple[bytes, str, str]:
    d = await definition_of(db, s)
    rs = (await db.execute(select(StudyResponse).where(StudyResponse.study_id == s.id, StudyResponse.outcome == "completed",
                                                       StudyResponse.quality_status.in_(statuses)).order_by(StudyResponse.submitted_at))).scalars().all()
    cols = export_columns(d)
    snap_keys = sorted({k for r in rs for k in (r.profile_snapshot or {})})
    flat = []
    for r in rs:
        row = {"response_id": str(r.id), "participant": r.participant_token[:12], "submitted_at": r.submitted_at.isoformat(),
               "duration_s": round(r.duration_seconds), "quality": r.quality_status.value, "score": r.score,
               **{f"profil_{k}": (r.profile_snapshot or {}).get(k) for k in snap_keys},
               "phone": (r.contact or {}).get("phone") if r.contact_consent else None}
        for name, fn in cols:
            row[name] = fn(r.answers)
        flat.append(row)
    names = ["response_id", "participant", "submitted_at", "duration_s", "quality", "score", *[f"profil_{k}" for k in snap_keys], "phone", *[c for c, _ in cols]]
    base = f"etude-{str(s.id)[:8]}"
    if fmt == "json":
        return json.dumps({"study": s.title, "n": len(flat), "responses": flat}, ensure_ascii=False, default=str).encode(), "application/json", f"{base}.json"
    return _csv(flat, names), "text/csv", f"{base}.csv"


async def list_responses(db: AsyncSession, s: Study, status, outcome, limit, offset) -> dict:
    d = await definition_of(db, s)
    bm = {b.key: b for b in all_blocks(d)}
    q = select(StudyResponse).where(StudyResponse.study_id == s.id)
    if status:
        q = q.where(StudyResponse.quality_status.in_(status))
    if outcome:
        q = q.where(StudyResponse.outcome == outcome)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(StudyResponse.submitted_at.desc()).limit(limit).offset(offset))).scalars().all()
    rw = {r.response_id: r for r in (await db.execute(select(StudyReward).where(StudyReward.response_id.in_([x.id for x in rows])))).scalars()} if rows else {}
    return {"total": total, "items": [{
        "id": str(r.id), "participant": r.participant_token[:12], "submitted_at": r.submitted_at, "outcome": r.outcome,
        "duration_seconds": round(r.duration_seconds), "quality_status": r.quality_status.value, "score": r.score,
        "sub_scores": r.sub_scores, "reasons": r.reasons, "evaluator": r.evaluator, "profile": r.profile_snapshot,
        "contact": r.contact if r.contact_consent else None,
        "answers": [{"question": bm[k].text, "answer": describe(bm[k], v)} for k, v in r.answers.items() if k in bm],
        "reward": rwd.reward_out(rw[r.id]) if r.id in rw else None} for r in rows]}


async def decide_responses(db: AsyncSession, admin: User, s: Study, ids: list, status: QualityStatus, reason: str) -> int:
    rs = (await db.execute(select(StudyResponse).where(StudyResponse.study_id == s.id, StudyResponse.id.in_(ids)).with_for_update())).scalars().all()
    cfg, now = rwd.cfg_of(s), _utc()
    for r in rs:
        r.quality_status, r.evaluator, r.evaluated_at = status, "admin", now
        r.reasons = [*(r.reasons or []), {"code": "admin_override", "severity": "info", "reason": reason}]
        rw = (await db.execute(select(StudyReward).where(StudyReward.response_id == r.id))).scalar_one_or_none()
        if status == QualityStatus.rejected:
            if rw and rw.status in (RewardStatus.pending, RewardStatus.entered):
                await rwd.refuse(db, rw, "admin_refused", reason, admin.id)
            elif rw and rw.status in (RewardStatus.granted, RewardStatus.delivered) and cfg.forfeit.revoke_on_admin_reject:
                await rwd.revoke(db, rw, "response_invalidated", reason, admin.id)
        elif cfg.enabled and r.user_id and cfg.mode == "guaranteed":
            if rw is None:
                prof = (await tgt.derive_bulk(db, s.tenant_id, [r.user_id], now))[r.user_id]
                rw = await rwd.decide(db, s, r, r.user_id, cfg, prof)
            if rw and (rw.status == RewardStatus.pending or (rw.status == RewardStatus.refused and rw.reason_code in
                                                            ("quality_low", "status_rejected", "attention_failed", "too_fast"))):
                await rwd.grant(db, rw, s, cfg, admin.id)
    return len(rs)


# ------------------------------------------------------------------ récompenses
async def list_rewards(db: AsyncSession, s: Study, status, limit, offset) -> dict:
    q = select(StudyReward, StudyResponse.participant_token, StudyResponse.contact, StudyResponse.contact_consent).outerjoin(
        StudyResponse, StudyResponse.id == StudyReward.response_id).where(StudyReward.study_id == s.id)
    if status:
        q = q.where(StudyReward.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    by = dict((await db.execute(select(StudyReward.status, func.count(), func.coalesce(func.sum(StudyReward.amount), 0)).where(
        StudyReward.study_id == s.id).group_by(StudyReward.status))).all().__iter__() and [(k.value, (n, float(a))) for k, n, a in
              (await db.execute(select(StudyReward.status, func.count(), func.coalesce(func.sum(StudyReward.amount), 0)).where(
                  StudyReward.study_id == s.id).group_by(StudyReward.status))).all()])
    rows = (await db.execute(q.order_by(StudyReward.created_at.desc()).limit(limit).offset(offset))).all()
    codes_free = (await db.execute(select(func.count()).where(StudyVoucherCode.study_id == s.id, StudyVoucherCode.reward_id.is_(None)))).scalar_one()
    return {"total": total, "summary": {k: {"n": v[0], "amount": v[1]} for k, v in by.items()}, "codes_free": codes_free,
            "items": [{**rwd.reward_out(r), "participant": (tok or "")[:12], "phone": (r.delivery or {}).get("phone"),
                       "name": (r.delivery or {}).get("name"), "reference": (r.delivery or {}).get("reference")} for r, tok, _c, _k in rows]}


async def reward_action(db: AsyncSession, admin: User, rw: StudyReward, p) -> None:
    s = await db.get(Study, rw.study_id)
    cfg = RewardCfg.model_validate((rw.snapshot or {}).get("cfg") or {"enabled": True, "label": rw.label})
    a = p.action
    if a == "approve":
        if rw.status not in (RewardStatus.pending, RewardStatus.entered, RewardStatus.refused):
            raise HTTPException(409, "invalid_transition")
        await rwd.grant(db, rw, s, cfg, admin.id)
    elif a == "refuse":
        if rw.status not in (RewardStatus.pending, RewardStatus.entered):
            raise HTTPException(409, "invalid_transition")
        await rwd.refuse(db, rw, p.reason_code or "admin_refused", p.note, admin.id)
    elif a == "revoke":
        if rw.status not in (RewardStatus.granted, RewardStatus.delivered):
            raise HTTPException(409, "invalid_transition")
        await rwd.revoke(db, rw, p.reason_code or "admin_revoked", p.note, admin.id)
    elif a == "deliver":
        if rw.status != RewardStatus.granted:
            raise HTTPException(409, "invalid_transition")
        await rwd.deliver(db, rw, p.note, p.reference, admin.id)


async def add_codes(db: AsyncSession, s: Study, codes: list[str]) -> dict:
    have = set((await db.execute(select(StudyVoucherCode.code).where(StudyVoucherCode.study_id == s.id))).scalars())
    new = [c.strip() for c in dict.fromkeys(codes) if c.strip() and c.strip() not in have]
    db.add_all([StudyVoucherCode(tenant_id=s.tenant_id, study_id=s.id, code=c[:120]) for c in new])
    await db.flush()
    return {"added": len(new), "assigned_to_waiting": await rwd.fill_waiting_vouchers(db, s)}


async def draw(db: AsyncSession, admin: User, s: Study, winners: int | None, seed: int | None) -> dict:
    cfg = rwd.cfg_of(s)
    if not cfg.enabled or cfg.mode != "lottery":
        raise HTTPException(409, "not_a_lottery")
    if (s.settings or {}).get("draw"):
        raise HTTPException(409, "already_drawn")
    entered = (await db.execute(select(StudyReward).where(StudyReward.study_id == s.id, StudyReward.status == RewardStatus.entered)
                                .order_by(StudyReward.id))).scalars().all()
    n = min(winners or cfg.winners, len(entered))
    seed = seed if seed is not None else secrets.randbits(32)
    chosen = {r.id for r in random.Random(seed).sample(entered, n)}
    for r in entered:
        if r.id in chosen:
            await rwd.grant(db, r, s, cfg, admin.id)
        else:
            await rwd.refuse(db, r, "not_drawn", None, admin.id)
    s.settings = {**(s.settings or {}), "draw": {"at": _utc().isoformat(), "winners": n, "entries": len(entered), "seed": seed, "by": str(admin.id)}}
    return s.settings["draw"]


# ------------------------------------------------------------------ statistiques
async def stats(db: AsyncSession, s: Study) -> dict:
    parts = dict((await db.execute(select(StudyParticipation.status, func.count()).where(StudyParticipation.study_id == s.id)
                                   .group_by(StudyParticipation.status))).all())
    parts = {k.value: v for k, v in parts.items()}
    R = StudyResponse
    q = dict((await db.execute(select(R.quality_status, func.count()).where(R.study_id == s.id, R.outcome == "completed")
                               .group_by(R.quality_status))).all())
    dur = (await db.execute(select(func.avg(R.duration_seconds), func.avg(R.score)).where(R.study_id == s.id, R.outcome == "completed"))).one()
    per_day = [{"date": d.isoformat(), "n": n} for d, n in (await db.execute(
        select(func.date(R.submitted_at), func.count()).where(R.study_id == s.id).group_by(func.date(R.submitted_at)).order_by(func.date(R.submitted_at)))).all()]
    started = sum(parts.get(k, 0) for k in ("started", "completed", "screened_out"))
    return {"funnel": {**{k: parts.get(k, 0) for k in ("invited", "seen", "started", "completed", "screened_out", "declined", "expired")}},
            "quality": {k.value: v for k, v in q.items()}, "avg_duration_s": round(float(dur[0] or 0)), "avg_score": round(float(dur[1] or 0), 3),
            "completion_rate": round(parts.get("completed", 0) / started, 3) if started else None, "per_day": per_day,
            "quota_total": s.quota_total, "valid": sum(q.get(x, 0) for x in USABLE + [QualityStatus.pending])}