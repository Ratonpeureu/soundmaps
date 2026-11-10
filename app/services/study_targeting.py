import math
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

import h3
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.study_settings import study_settings as sts
from app.models.enums import ConsentType, ParticipationStatus, QualityStatus, StudyStatus
from app.models.measurement import Measurement
from app.models.social import UserProfile
from app.models.study import Study, StudyParticipation, StudyResponse, UserStudyProfile
from app.models.survey import SurveyAnswer, SurveyResponse
from app.models.tenancy import User, UserConsent
from app.services.leaderboard import level_info
from app.services.quota import haversine_m, metrics_bulk
from app.services.study_schema import ATTRS, Targeting

USABLE = [QualityStatus.validated, QualityStatus.indicative]
_INF = {"household_income_band": "income_band", "education_level_max": "education_level",
        "occupation_group": "occupation_group", "household_persons": "household_size",
        "internet_access": "internet_access", "mobile_money_use": "mobile_money", "employment_status": "occupation_group"}
_EMP = {"etudiant", "chomeur", "retraite"}


def norm(s) -> str | None:
    if not s:
        return None
    t = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return " ".join(t.split()) or None


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def age_band(a):
    if a is None:
        return None
    return "<18" if a < 18 else "18-24" if a < 25 else "25-34" if a < 35 else "35-44" if a < 45 else "45-54" if a < 55 else "55+"


async def consented_ids(db: AsyncSession, tenant_id, ids: list) -> set:
    if not ids:
        return set()
    rows = (await db.execute(
        select(UserConsent.user_id, UserConsent.granted).where(
            UserConsent.tenant_id == tenant_id, UserConsent.user_id.in_(ids), UserConsent.consent_type == ConsentType.study_data)
        .order_by(UserConsent.user_id, UserConsent.created_at.desc()).distinct(UserConsent.user_id))).all()
    return {u for u, g in rows if g}


async def derive_bulk(db: AsyncSession, tenant_id, ids: list, now: datetime, pos: dict | None = None) -> dict:
    """Profil dérivé : compte + profil + réponses d'enquêtes + profil études déclaré + activité + position."""
    out: dict = {i: {} for i in ids}
    if not ids:
        return out
    for uid, created, last, paused, active in (await db.execute(select(
            User.id, User.created_at, User.last_login_at, User.participation_paused, User.is_active).where(User.id.in_(ids)))).all():
        out[uid].update(account_age_days=(now - created).days, last_login_at=last, paused=bool(paused), active=bool(active))
    for p in (await db.execute(select(UserProfile).where(UserProfile.user_id.in_(ids)))).scalars():
        d = out[p.user_id]
        d.update(city=norm(p.city), district=norm(p.district), analyst=bool(p.analyst_enabled), level=level_info(p.xp or 0)["index"])
        if p.birth_year:
            d["age"] = now.year - p.birth_year
    # inféré des enquêtes de l'application
    rows = (await db.execute(
        select(SurveyResponse.user_id, SurveyAnswer.indicator_key, SurveyAnswer.value_num, SurveyAnswer.value_cat)
        .select_from(SurveyAnswer).join(SurveyResponse, SurveyResponse.id == SurveyAnswer.response_id)
        .where(SurveyResponse.user_id.in_(ids), SurveyAnswer.indicator_key.in_(list(_INF)), SurveyAnswer.status.in_(USABLE),
               SurveyAnswer.answered_at >= now - timedelta(days=365))
        .order_by(SurveyAnswer.answered_at.desc()).limit(len(ids) * 40 + 200))).all()
    seen = set()
    for uid, key, num, cat in rows:
        if cat == "__all__" or (uid, key) in seen:
            continue
        seen.add((uid, key))
        tgt, d = _INF[key], out[uid]
        if key == "employment_status":
            if cat in _EMP and "occupation_group" not in d:
                d["occupation_group"] = cat
        elif key == "household_persons":
            d[tgt] = int(num) if num is not None else None
        elif key == "mobile_money_use":
            d[tgt] = bool(num)
        elif cat:
            d[tgt] = cat
    # déclaré (prioritaire)
    for sp in (await db.execute(select(UserStudyProfile).where(UserStudyProfile.user_id.in_(ids)))).scalars():
        d = out[sp.user_id]
        for f in ("gender", "occupation_group", "education_level", "household_size", "income_band"):
            if getattr(sp, f) is not None:
                d[f] = getattr(sp, f)
        d["languages"], d["interests"] = list(sp.languages or []), list(sp.interests or [])
    # position récente
    for uid, la, lo in (await db.execute(
            select(Measurement.user_id, Measurement.latitude, Measurement.longitude)
            .where(Measurement.tenant_id == tenant_id, Measurement.user_id.in_(ids), Measurement.measured_at >= now - timedelta(days=30),
                   Measurement.quality_status != QualityStatus.rejected)
            .distinct(Measurement.user_id).order_by(Measurement.user_id, Measurement.measured_at.desc()))).all():
        out[uid]["lat"], out[uid]["lon"] = la, lo
    for uid, (la, lo) in (pos or {}).items():
        if uid in out and la is not None and lo is not None:
            out[uid]["lat"], out[uid]["lon"] = la, lo
    for uid, m in (await metrics_bulk(db, tenant_id, ids, now)).items():
        out[uid]["reliability"] = round(m["R"], 3)
    for uid, n in (await db.execute(select(Measurement.user_id, func.count()).where(
            Measurement.tenant_id == tenant_id, Measurement.user_id.in_(ids), Measurement.measured_at >= now - timedelta(days=90),
            Measurement.quality_status == QualityStatus.validated).group_by(Measurement.user_id))).all():
        out[uid]["validated_count"] = int(n)
    for uid, n, avg in (await db.execute(select(StudyResponse.user_id, func.count(), func.avg(StudyResponse.score)).where(
            StudyResponse.user_id.in_(ids), StudyResponse.outcome == "completed", StudyResponse.quality_status.in_(USABLE))
            .group_by(StudyResponse.user_id))).all():
        out[uid]["studies_done"], out[uid]["study_trust"] = int(n), float(avg or 0)
    for d in out.values():
        d.setdefault("validated_count", 0)
        d.setdefault("studies_done", 0)
        d.setdefault("reliability", 0.6)
        if d["studies_done"] < 3:
            d["study_trust"] = None
    return out


# ------------------------------------------------------------------ évaluation
def eval_criterion(c, prof: dict):
    spec, v = ATTRS[c.attr], prof.get(c.attr)
    if v is None or v == []:
        return None
    op, ref = c.op, c.value
    try:
        if op in ("true", "false"):
            return bool(v) == (op == "true")
        k = spec["kind"]
        if k == "number":
            x = float(v)
            if op == "between":
                return float(ref[0]) <= x <= float(ref[1])
            y = float(ref)
            return {"eq": x == y, "ne": x != y, "gt": x > y, "gte": x >= y, "lt": x < y, "lte": x <= y}[op]
        if k == "multi":
            vs, rs = set(v), set(ref if isinstance(ref, list) else [ref])
            return rs <= vs if op == "has" else bool(vs & rs)
        free = spec.get("free")
        val = norm(v) if free else v
        refs = [norm(r) if free else r for r in (ref if isinstance(ref, list) else [ref])]
        hit = val in refs
        return hit if op in ("eq", "in") else not hit
    except (TypeError, ValueError, KeyError, IndexError):
        return None


def eval_geo(g, prof: dict):
    res = []
    la, lo = prof.get("lat"), prof.get("lon")
    if la is not None:
        res += [haversine_m(la, lo, c.lat, c.lon) <= c.radius_km * 1000 for c in g.circles]
        if g.h3_cells and g.h3_res:
            res.append(h3.latlng_to_cell(la, lo, g.h3_res) in set(g.h3_cells))
    if g.cities and prof.get("city"):
        res.append(prof["city"] in {norm(x) for x in g.cities})
    return any(res) if res else None


def evaluate(t: Targeting, prof: dict) -> dict:
    res = {"eligible": False, "score": 0.0, "matched": [], "unknown": []}
    if prof.get("paused") or prof.get("active") is False:
        return res
    if t.min_reliability and prof.get("reliability", 0) < t.min_reliability:
        return res
    ok, matched, unknown, pw, po, n = True, [], [], 0.0, 0.0, 0
    for c in t.include:
        n += 1
        r = eval_criterion(c, prof)
        if r is None:
            unknown.append(c.attr)
            if c.mode == "must" and t.strict_unknown:
                ok = False
            if c.mode == "prefer":
                pw += c.weight
            continue
        if c.mode == "must" and not r:
            ok = False
        if c.mode == "prefer":
            pw += c.weight
            po += c.weight if r else 0
        if r and not ATTRS[c.attr].get("sensitive"):
            matched.append(ATTRS[c.attr]["label"])
    for c in t.exclude:
        if eval_criterion(c, prof) is True:
            ok = False
    if t.geo and (t.geo.circles or t.geo.cities or t.geo.h3_cells):
        n += 1
        r = eval_geo(t.geo, prof)
        if r is None:
            unknown.append("position")
            if t.geo.mode == "must" and t.strict_unknown:
                ok = False
        elif t.geo.mode == "must" and not r:
            ok = False
        elif r:
            matched.append("Zone géographique")
        if t.geo.mode == "prefer":
            pw += 1
            po += 1 if r else 0
    fit = po / pw if pw else 1.0
    known = (n - len(unknown)) / n if n else 1.0
    trust = prof.get("study_trust") if prof.get("study_trust") is not None else 0.6
    score = 0.55 * fit + 0.2 * known + 0.15 * prof.get("reliability", 0.6) + 0.1 * trust
    return {"eligible": ok, "score": round(score, 4), "matched": matched, "unknown": unknown}


# ------------------------------------------------------------------ estimation pour l'admin
async def preview(db: AsyncSession, tenant_id, t: Targeting) -> dict:
    now = datetime.now(timezone.utc)
    ids = list((await db.execute(select(User.id).where(User.tenant_id == tenant_id, User.is_active.is_(True),
                                                          User.participation_paused.is_(False))
                                 .limit(sts.STT_USERS_LIMIT))).scalars())
    tot = cons = elig = 0
    unknown, gender, ages, cities = Counter(), Counter(), Counter(), Counter()
    for chunk in chunks(ids, 1000):
        ok = await consented_ids(db, tenant_id, chunk)
        profs = await derive_bulk(db, tenant_id, chunk, now)
        for uid in chunk:
            tot += 1
            if uid not in ok:
                continue
            cons += 1
            r = evaluate(t, profs[uid])
            unknown.update(r["unknown"])
            if r["eligible"]:
                elig += 1
                p = profs[uid]
                gender[p.get("gender") or "inconnu"] += 1
                ages[age_band(p.get("age")) or "inconnu"] += 1
                cities[p.get("city") or "inconnue"] += 1
    return {"users_total": tot, "users_consented": cons, "eligible": elig,
            "expected_responses": round(elig * sts.STT_RESPONSE_RATE_PRIOR),
            "unknown_by_attribute": dict(unknown), "by_gender": dict(gender), "by_age": dict(ages),
            "by_city": dict(cities.most_common(10)),
            "note": "Seuls les utilisateurs ayant autorisé « Études » sont ciblables. Les données manquantes réduisent la cible si « strict » est actif."}


# ------------------------------------------------------------------ invitations
async def invite_all(db: AsyncSession, tenant_id) -> dict:
    now = datetime.now(timezone.utc)
    studies = (await db.execute(select(Study).where(
        Study.tenant_id == tenant_id, Study.status == StudyStatus.active, Study.ends_at > now)
        .order_by(Study.ends_at))).scalars().all()
    plan = {}
    for s in studies:
        if s.starts_at and s.starts_at > now:
            continue
        t = Targeting.model_validate(s.targeting or {})
        existing = set((await db.execute(select(StudyParticipation.user_id).where(StudyParticipation.study_id == s.id))).scalars())
        outstanding = (await db.execute(select(func.count()).where(
            StudyParticipation.study_id == s.id, StudyParticipation.status.in_(
                [ParticipationStatus.invited, ParticipationStatus.seen, ParticipationStatus.started])))).scalar_one()
        done = (await db.execute(select(func.count()).where(
            StudyResponse.study_id == s.id, StudyResponse.outcome == "completed", StudyResponse.quality_status.in_(USABLE + [QualityStatus.pending])))).scalar_one()
        if s.quota_total:
            if done >= s.quota_total:
                continue
            need = math.ceil((s.quota_total - done) / sts.STT_RESPONSE_RATE_PRIOR * (t.sampling.oversample or sts.STT_OVERSAMPLE)) - outstanding
        else:
            need = (t.sampling.max_invites or sts.STT_DEFAULT_MAX_INVITES) - len(existing)
        if need > 0:
            plan[s.id] = (s, t, need, existing)
    if not plan:
        return {"invited": 0}
    ids = list((await db.execute(select(User.id).where(
        User.tenant_id == tenant_id, User.is_active.is_(True), User.participation_paused.is_(False),
        User.last_login_at >= now - timedelta(days=sts.STT_ACTIVE_DAYS)).limit(sts.STT_USERS_LIMIT))).scalars())
    caps: Counter = Counter()
    cands = {sid: [] for sid in plan}
    for chunk in chunks(ids, 1000):
        ok = await consented_ids(db, tenant_id, chunk)
        for uid, n in (await db.execute(select(StudyParticipation.user_id, func.count()).where(
                StudyParticipation.user_id.in_(chunk), StudyParticipation.source == "invited",
                StudyParticipation.invited_at >= now - timedelta(days=7)).group_by(StudyParticipation.user_id))).all():
            caps[uid] = int(n)
        profs = await derive_bulk(db, tenant_id, [u for u in chunk if u in ok], now)
        for uid, prof in profs.items():
            for sid, (s, t, need, existing) in plan.items():
                if uid in existing:
                    continue
                r = evaluate(t, prof)
                if r["eligible"]:
                    cands[sid].append((r["score"], uid))
    rows = []
    for sid, (s, t, need, existing) in plan.items():
        got = 0
        for score, uid in sorted(cands[sid], key=lambda x: -x[0]):
            if got >= need:
                break
            if caps[uid] >= sts.STT_INVITE_MAX_PER_USER_WEEK:
                continue
            caps[uid] += 1
            got += 1
            rows.append({"id": uuid.uuid4(), "tenant_id": tenant_id, "study_id": sid, "user_id": uid, "source": "invited",
                         "status": ParticipationStatus.invited, "match_score": score, "invited_at": now,
                         "expires_at": min(s.ends_at, now + timedelta(days=sts.STT_INVITE_TTL_DAYS))})
    for chunk in chunks(rows, 1000):
        await db.execute(pg_insert(StudyParticipation).values(chunk).on_conflict_do_nothing(constraint="uq_study_participation"))
    return {"invited": len(rows), "studies": len(plan)}