import json
import math
import random
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import combinations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.survey_settings import survey_settings as sv
from app.models.enums import ProfileStatus, QualityStatus
from app.models.survey import ProfileEvent, Survey, SurveyProfile, SurveyResponse
from app.services import catalog
from app.services.geo import ring
from app.services.survey_schema import (QuestionDef, SurveyDefinition, all_rules, check_rules, questions_of,
                                        summarize, validate_answers)

_USABLE = [QualityStatus.validated, QualityStatus.indicative]


# ------------------------------------------------------------------ fonctions pures

def _norm(ind, x: float) -> float:
    if ind.hi / max(ind.lo, 1.0) > 50:
        return math.log1p(max(x, 0)) / math.log1p(ind.hi)
    return (x - ind.lo) / (ind.hi - ind.lo) if ind.hi > ind.lo else 0.0


def encode(qs: list[QuestionDef], answers: dict) -> list[float]:
    v: list[float] = []
    for q in qs:
        ind, a = catalog.INDICATORS[q.indicator], answers.get(q.key)
        if q.kind == "single_choice":
            v += [1.0 if a == o.value else 0.0 for o in q.options]
        elif q.kind == "multi_choice":
            v += [1.0 if a and o.value in a else 0.0 for o in q.options]
        elif q.kind == "number":
            v.append(_norm(ind, float(a)) if a is not None else 0.0)
        elif q.kind == "scale":
            v.append((a - 1) / 4 if a is not None else 0.0)
        else:
            v.append(1.0 if a else 0.0)
    return v


def dist(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def candidate_values(q: QuestionDef) -> list:
    ind = catalog.INDICATORS[q.indicator]
    if q.kind == "single_choice":
        return [o.value for o in q.options]
    if q.kind == "multi_choice":
        vals = [o.value for o in q.options]
        return [[v] for v in vals] + [list(c) for c in list(combinations(vals[:6], 2))[:6]]
    if q.kind == "scale":
        return [1, 2, 3, 4, 5]
    if q.kind == "yes_no":
        return [True, False]
    lo = max(q.min if q.min is not None else ind.lo, ind.lo)
    hi = min(q.max if q.max is not None else ind.hi, ind.hi)
    base = q.anchors or list(ind.anchors) or [lo + (hi - lo) * t for t in (0.05, 0.15, 0.3, 0.5, 0.8)]
    out = []
    for x in base:
        if lo <= x <= hi:
            out.append(int(round(x)) if ind.int_ else round(float(x), 2))
    return out or [lo]


def _bucket(x: float, lo: float, hi: float) -> str:
    t = (x - lo) / (hi - lo) if hi > lo else 0
    return "faible" if t < 0.34 else "moyen" if t < 0.67 else "élevé"


def axes_of(qs: list[QuestionDef], answers: dict) -> dict:
    out = {}
    for q in qs:
        if not q.axis or q.key not in answers:
            continue
        ind, a = catalog.INDICATORS[q.indicator], answers[q.key]
        if q.kind == "single_choice":
            out[q.axis] = next((o.label for o in q.options if o.value == a), str(a))
        elif q.kind == "yes_no":
            out[q.axis] = "oui" if a else "non"
        elif q.kind == "scale":
            out[q.axis] = _bucket(a, 1, 5)
        elif q.kind == "number":
            out[q.axis] = _bucket(math.log1p(a), 0, math.log1p(ind.hi)) if ind.hi / max(ind.lo, 1) > 50 \
                else _bucket(a, ind.lo, ind.hi)
    return out


def auto_label(qs: list[QuestionDef], answers: dict) -> tuple[str, str]:
    from app.services.survey_schema import describe_answer
    axis_qs = [q for q in qs if q.axis and q.key in answers] or [q for q in qs if q.key in answers]
    label = " · ".join(describe_answer(q, answers[q.key]) for q in axis_qs[:3])[:110]
    desc = "; ".join(f"{q.short or q.text}: {describe_answer(q, answers[q.key])}" for q in qs[:4] if q.key in answers)
    return label, desc[:290]


def kmedoids(vecs: list[list[float]], k: int, rng: random.Random, iters: int = 10):
    n = len(vecs)
    k = max(1, min(k, n))
    med = [rng.randrange(n)]
    while len(med) < k:
        med.append(max(range(n), key=lambda i: min(dist(vecs[i], vecs[m]) for m in med)))
    clusters: dict[int, list[int]] = {}
    for _ in range(iters):
        clusters = {m: [] for m in med}
        for i in range(n):
            clusters[min(med, key=lambda m: dist(vecs[i], vecs[m]))].append(i)
        new = [min(idx, key=lambda c: sum(dist(vecs[c], vecs[j]) for j in idx)) for idx in clusters.values() if idx]
        if set(new) == set(med):
            break
        med = new
    sizes = {m: len(idx) for m, idx in clusters.items()}
    return med, sizes


def generate_deterministic(defn: SurveyDefinition, slot: str, n: int, rng: random.Random,
                           existing: list[list[float]]) -> list[dict]:
    """Archétypes couvrant l'espace des réponses : tirage cohérent (règles) puis échantillonnage de diversité maximale."""
    qs = questions_of(defn, slot)
    rules = all_rules(defn)
    cands = {q.key: candidate_values(q) for q in qs}
    pool, seen = [], set()
    for _ in range(3000):
        combo = {q.key: rng.choice(cands[q.key]) for q in qs}
        key = json.dumps(combo, sort_keys=True)
        if key in seen or any(h["severity"] == "reject" for h in check_rules(rules, combo)):
            continue
        seen.add(key)
        pool.append(combo)
        if len(pool) >= 1200:
            break
    if not pool:
        return []
    vecs = [encode(qs, c) for c in pool]
    targets = {(q.key, o) for q in qs if q.axis and q.kind in ("single_choice", "yes_no")
               for o in (cands[q.key] if q.kind == "yes_no" else [x.value for x in q.options])}
    chosen: list[int] = []
    pts = list(existing)

    def covers(i):
        return {(q.key, pool[i][q.key]) for q in qs if q.axis and q.kind in ("single_choice", "yes_no")} & targets

    uncovered = set(targets)
    while uncovered and len(chosen) < n:
        i = max((i for i in range(len(pool)) if i not in chosen),
                key=lambda i: (len(covers(i) & uncovered),
                               min((dist(vecs[i], p) for p in pts), default=1.0)), default=None)
        if i is None or not (covers(i) & uncovered):
            break
        chosen.append(i)
        pts.append(vecs[i])
        uncovered -= covers(i)
    while len(chosen) < n and len(chosen) < len(pool):
        i = max((i for i in range(len(pool)) if i not in chosen),
                key=lambda i: min((dist(vecs[i], p) for p in pts), default=1.0))
        chosen.append(i)
        pts.append(vecs[i])
    out = []
    for i in chosen:
        label, desc = auto_label(qs, pool[i])
        out.append({"answers": pool[i], "axes": axes_of(qs, pool[i]), "label": label, "description": desc,
                    "origin": "deterministic", "meta": {}})
    return out


def cluster_candidates(defn: SurveyDefinition, slot: str, observed: list[dict], k: int, rng: random.Random) -> list[dict]:
    qs = questions_of(defn, slot)
    complete = [o for o in observed if all(q.key in o for q in qs if q.required)]
    if len(complete) < sv.SV_CLUSTER_MIN:
        return []
    sample = rng.sample(complete, min(400, len(complete)))
    vecs = [encode(qs, o) for o in sample]
    med, sizes = kmedoids(vecs, k, rng)
    total = sum(sizes.values()) or 1
    out = []
    for m in med:
        a = sample[m]
        label, desc = auto_label(qs, a)
        out.append({"answers": a, "axes": axes_of(qs, a), "label": label, "description": desc, "origin": "cluster",
                    "meta": {"support": sizes.get(m, 0), "support_share": round(sizes.get(m, 0) / total, 3)}})
    return out


def profile_valid(defn: SurveyDefinition, slot: str, answers: dict) -> dict | None:
    """Vérifie types, bornes, complétude et cohérence d'un profil ; renvoie les réponses normalisées ou None."""
    try:
        clean = validate_answers(defn, answers)
    except (ValueError, KeyError, TypeError):
        return None
    qs = questions_of(defn, slot)
    if any(k not in {q.key for q in qs} for k in clean) or any(q.required and q.key not in clean for q in qs):
        return None
    if any(h["severity"] == "reject" for h in check_rules(all_rules(defn), clean)):
        return None
    return clean


# ------------------------------------------------------------------ accès données

async def active_profiles(db: AsyncSession, survey_id: uuid.UUID, slot: str | None = None) -> list[SurveyProfile]:
    q = select(SurveyProfile).where(SurveyProfile.survey_id == survey_id, SurveyProfile.status == ProfileStatus.active)
    if slot:
        q = q.where(SurveyProfile.slot == slot)
    return list((await db.execute(q.order_by(SurveyProfile.created_at))).scalars())


async def _observed(db: AsyncSession, survey: Survey, slot: str, *, only_residual: bool = False) -> list[dict]:
    q = select(SurveyResponse.answers, SurveyResponse.none_slots, SurveyResponse.overrides).where(
        SurveyResponse.survey_id == survey.id, SurveyResponse.quality_status.in_(_USABLE),
        SurveyResponse.survey_version == survey.current_version).order_by(SurveyResponse.submitted_at.desc()).limit(2000)
    rows = (await db.execute(q)).all()
    out = []
    for answers, none_slots, overrides in rows:
        if only_residual and slot not in (none_slots or []) and not overrides:
            continue
        out.append(answers or {})
    return out


def _new(survey: Survey, tenant_id, slot: str, c: dict, gen: int) -> SurveyProfile:
    return SurveyProfile(tenant_id=tenant_id, survey_id=survey.id, slot=slot, key=uuid.uuid4().hex[:8],
                         label=c["label"][:120], description=(c.get("description") or "")[:300],
                         axes=c.get("axes") or {}, answers=c["answers"], origin=c["origin"],
                         status=ProfileStatus.active, generation=gen, meta=c.get("meta") or {})


def _far_from(vec, pts, thr) -> bool:
    return all(dist(vec, p) >= thr for p in pts)


async def generate_slot(db: AsyncSession, survey: Survey, defn: SurveyDefinition, slot: str, *, n: int | None = None,
                        use_ai: bool = False, rng: random.Random | None = None) -> list[SurveyProfile]:
    from app.services import survey_ai
    rng = rng or random.Random()
    qs = questions_of(defn, slot)
    want = n or sv.SV_PROFILES_PER_SLOT
    existing = await active_profiles(db, survey.id, slot)
    room = max(0, min(want, sv.SV_PROFILES_MAX - len(existing)))
    if room == 0:
        return []
    pts = [encode(qs, p.answers) for p in existing]
    thr = sv.SV_NEW_PROFILE_MIN_DIST * math.sqrt(max(1, len(qs)))
    cands: list[dict] = []
    observed = await _observed(db, survey, slot)
    cands += cluster_candidates(defn, slot, observed, min(room, sv.SV_CLUSTER_K), rng)
    if use_ai:
        cands += await survey_ai.propose_profiles(defn, survey, slot, existing, need=room, observed_n=len(observed))
    if len(cands) < room:
        cands += generate_deterministic(defn, slot, room - len(cands) + 2, rng, pts)
    added: list[SurveyProfile] = []
    for c in cands:
        clean = profile_valid(defn, slot, c["answers"])
        if clean is None:
            continue
        c["answers"] = clean
        c["axes"] = c.get("axes") or axes_of(qs, clean)
        if not c.get("label"):
            c["label"], c["description"] = auto_label(qs, clean)
        v = encode(qs, clean)
        if not _far_from(v, pts, thr):
            continue
        pts.append(v)
        added.append(_new(survey, survey.tenant_id, slot, c, survey.profile_generation))
        if len(added) >= room:
            break
    db.add_all(added)
    await db.flush()
    return added


async def ensure_profiles(db: AsyncSession, survey: Survey, defn: SurveyDefinition, *, use_ai: bool = False) -> int:
    total = 0
    for s in defn.slots:
        if s.mode != "profile":
            continue
        have = len(await active_profiles(db, survey.id, s.key))
        if have < sv.SV_PROFILES_MIN:
            total += len(await generate_slot(db, survey, defn, s.key, n=sv.SV_PROFILES_PER_SLOT - have, use_ai=use_ai))
    return total


async def reconcile_after_update(db: AsyncSession, survey: Survey, defn: SurveyDefinition) -> int:
    """Nouvelle version de l'enquête : les profils devenus invalides sont retirés."""
    retired = 0
    slots = {s.key: s for s in defn.slots}
    for p in await active_profiles(db, survey.id):
        s = slots.get(p.slot)
        if s is None or s.mode != "profile" or profile_valid(defn, p.slot, p.answers) is None:
            p.status, p.retired_at = ProfileStatus.retired, datetime.now(timezone.utc)
            retired += 1
    await db.flush()
    return retired


# ------------------------------------------------------------------ évolution (profils hauts / bas, résidus)

def _coverage_ok(defn: SurveyDefinition, slot: str, profiles: list[SurveyProfile]) -> bool:
    qs = questions_of(defn, slot)
    for q in qs:
        if q.axis and q.kind == "single_choice":
            present = {p.answers.get(q.key) for p in profiles}
            if not {o.value for o in q.options} <= present:
                return False
    return True


async def evolve(db: AsyncSession, survey: Survey, defn: SurveyDefinition, *, use_ai: bool = True,
                 rng: random.Random | None = None) -> dict:
    from app.services import survey_ai
    rng = rng or random.Random()
    gen = survey.profile_generation + 1
    now = datetime.now(timezone.utc)
    summary: dict = {"generation": gen, "slots": {}}
    tot_added = tot_retired = 0
    for s in defn.slots:
        if s.mode != "profile":
            continue
        qs = questions_of(defn, s.key)
        profs = await active_profiles(db, survey.id, s.key)
        observed_all = await _observed(db, survey, s.key)
        residual = await _observed(db, survey, s.key, only_residual=True)
        none_rate = len(residual) / max(1, len(observed_all))
        # 1) profils bas : part de choix très inférieure à la part uniforme
        total_picks = sum(p.picked_count for p in profs) or 0
        retired: list[SurveyProfile] = []
        if total_picks >= sv.SV_RETIRE_MIN_SHOWN and len(profs) > sv.SV_PROFILES_MIN:
            uniform = 1 / len(profs)
            for p in sorted(profs, key=lambda p: p.picked_count):
                if p.meta.get("pinned") or p.shown_count < sv.SV_RETIRE_MIN_SHOWN:
                    continue
                if p.picked_count / total_picks >= sv.SV_RETIRE_RELATIVE * uniform:
                    continue
                keep = [x for x in profs if x.id != p.id and x not in retired]
                if len(keep) < sv.SV_PROFILES_MIN or not _coverage_ok(defn, s.key, keep):
                    continue
                retired.append(p)
                profs = keep
        for p in retired:
            p.status, p.retired_at = ProfileStatus.retired, now
        # 2) profils manquants : clusters des réponses résiduelles + (option) IA
        pts = [encode(qs, p.answers) for p in profs]
        thr = sv.SV_NEW_PROFILE_MIN_DIST * math.sqrt(max(1, len(qs)))
        room = max(0, sv.SV_PROFILES_MAX - len(profs))
        cands = cluster_candidates(defn, s.key, residual, min(sv.SV_CLUSTER_K, room), rng) if room else []
        want_ai = use_ai and room and (none_rate >= sv.SV_NONE_RATE_TRIGGER or len(profs) < sv.SV_PROFILES_MIN)
        if want_ai:
            cands += await survey_ai.propose_profiles(defn, survey, s.key, profs, need=min(room, 4),
                                                      observed_n=len(observed_all), residual=cands, none_rate=none_rate)
        if len(profs) + len(cands) < sv.SV_PROFILES_MIN:
            cands += generate_deterministic(defn, s.key, sv.SV_PROFILES_MIN - len(profs), rng, pts)
        added = []
        for c in cands:
            clean = profile_valid(defn, s.key, c["answers"])
            if clean is None:
                continue
            v = encode(qs, clean)
            if not _far_from(v, pts, thr) or len(profs) + len(added) >= sv.SV_PROFILES_MAX:
                continue
            pts.append(v)
            c["answers"] = clean
            c["axes"] = c.get("axes") or axes_of(qs, clean)
            if not c.get("label"):
                c["label"], c["description"] = auto_label(qs, clean)
            added.append(_new(survey, survey.tenant_id, s.key, c, gen))
        db.add_all(added)
        tot_added += len(added)
        tot_retired += len(retired)
        summary["slots"][s.key] = {"none_rate": round(none_rate, 3), "retired": [p.label for p in retired],
                                   "added": [{"label": a.label, "origin": a.origin} for a in added],
                                   "kept": len(profs)}
    survey.profile_generation = gen
    survey.last_evolved_at = now
    n_resp = (await db.execute(select(func.count()).select_from(SurveyResponse).where(
        SurveyResponse.survey_id == survey.id))).scalar_one()
    survey.responses_at_evolution = n_resp
    db.add(ProfileEvent(tenant_id=survey.tenant_id, survey_id=survey.id, generation=gen,
                        method="ai+data" if use_ai else "data", added=tot_added, retired=tot_retired, details=summary))
    await db.flush()
    return summary


async def due_for_evolution(db: AsyncSession, survey: Survey) -> bool:
    now = datetime.now(timezone.utc)
    if survey.last_evolved_at and now - survey.last_evolved_at < timedelta(hours=sv.SV_EVOLVE_INTERVAL_HOURS):
        return False
    n = (await db.execute(select(func.count()).select_from(SurveyResponse).where(
        SurveyResponse.survey_id == survey.id))).scalar_one()
    return n - survey.responses_at_evolution >= sv.SV_EVOLVE_MIN_NEW


# ------------------------------------------------------------------ classement personnalisé

async def _affinity(db: AsyncSession, user_id: uuid.UUID) -> tuple[Counter, int]:
    rows = (await db.execute(select(SurveyResponse.profile_picks).where(SurveyResponse.user_id == user_id)
                             .order_by(SurveyResponse.submitted_at.desc()).limit(20))).all()
    ids = {uuid.UUID(i) for (picks,) in rows for lst in (picks or {}).values() for i in lst}
    if not ids:
        return Counter(), 0
    axes = (await db.execute(select(SurveyProfile.axes).where(SurveyProfile.id.in_(ids)))).scalars().all()
    c: Counter = Counter()
    for ax in axes:
        for a, v in (ax or {}).items():
            c[(a, v)] += 1
    return c, len(rows)


async def ranked_profiles(db: AsyncSession, user_id: uuid.UUID, survey: Survey, slot: str, h3_index: str | None,
                          limit: int | None = None) -> list[SurveyProfile]:
    profs = await active_profiles(db, survey.id, slot)
    if not profs:
        return []
    n = len(profs)
    rng = random.Random(f"{user_id}:{survey.id}:{slot}:{datetime.now(timezone.utc).date()}")
    local: Counter = Counter()
    local_n = 0
    if h3_index:
        since = datetime.now(timezone.utc) - timedelta(days=sv.SV_LOCAL_DAYS)
        rows = (await db.execute(select(SurveyResponse.profile_picks).where(
            SurveyResponse.survey_id == survey.id, SurveyResponse.h3_index.in_(ring(h3_index, 1)),
            SurveyResponse.submitted_at >= since, SurveyResponse.quality_status.in_(_USABLE)).limit(500))).all()
        for (picks,) in rows:
            ids = (picks or {}).get(slot) or []
            if ids:
                local.update(ids)
                local_n += 1
    aff, n_resp = await _affinity(db, user_id)
    scored = []
    for p in profs:
        g = rng.betavariate(p.picked_count + 1, max(0, p.shown_count - p.picked_count) + 1)
        lr = min(2.0, ((local[str(p.id)] + 1) / (local_n + n)) * n)
        ax = list((p.axes or {}).items())
        af = (sum(min(1.0, aff[(a, v)] / max(1, n_resp)) for a, v in ax) / len(ax)) if ax and n_resp else 0.0
        scored.append((g * (0.6 + 0.4 * lr) * (0.8 + 0.2 * af), p))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in scored[:limit or sv.SV_SHOWN_MAX]]


def profile_out(defn: SurveyDefinition, p: SurveyProfile) -> dict:
    return {"id": str(p.id), "key": p.key, "slot": p.slot, "label": p.label, "description": p.description,
            "axes": p.axes, "origin": p.origin, "summary": summarize(defn, p.slot, p.answers),
            "support_share": (p.meta or {}).get("support_share")}


async def credit_offer(db: AsyncSession, offered: dict[str, list[str]], picked: dict[str, list[uuid.UUID]],
                       overridden_slots: set[str]) -> None:
    shown = [uuid.UUID(i) for ids in offered.values() for i in ids]
    if shown:
        await db.execute(update(SurveyProfile).where(SurveyProfile.id.in_(shown))
                         .values(shown_count=SurveyProfile.shown_count + 1))
    for slot, ids in picked.items():
        if not ids:
            continue
        vals = {"picked_count": SurveyProfile.picked_count + 1}
        if slot in overridden_slots:
            vals["override_count"] = SurveyProfile.override_count + 1
        await db.execute(update(SurveyProfile).where(SurveyProfile.id.in_(ids)).values(**vals))