import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.survey_settings import survey_settings as sv
from app.models.enums import EvidenceStatus, QualityStatus
from app.models.survey import (ReferenceValue, RespondentTrust, Survey, SurveyAnswer, SurveyEvidence,
                               SurveyResponse, SurveyResponseQuality, SurveyVersion)
from app.services import catalog
from app.services.acoustics import clamp01
from app.services.geo import cell_center, ring
from app.services.quality import QualityResult
from app.services.quota import haversine_m
from app.services.survey_schema import SurveyDefinition


@dataclass
class PeerStat:
    kind: str                         # num | cat
    n: int
    values: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)


@dataclass
class RefStat:
    kind: str                         # num | cat
    value: float
    category: str | None = None


@dataclass
class EvFacts:
    key: str
    dup_other: bool = False
    dup_elsewhere_self: bool = False
    time_bad: bool = False
    gps_bad: bool = False
    has_exif: bool = False

    @property
    def ok(self) -> bool:
        return not (self.dup_other or self.time_bad or self.gps_bad)


@dataclass
class Facts:
    values: dict                       # indicator_key -> valeur
    kinds: dict                        # indicator_key -> kind de question
    duration_seconds: float
    expected_seconds: float
    gps_accuracy_m: float
    speed_kmh: float | None
    fingerprint_dups: int
    scale_values: list
    yes_values: list
    own_median_duration: float | None
    own_n: int
    trust: float | None
    trust_n: int
    evidence_required: list
    evidence: dict                     # question_key -> [EvFacts]
    q_by_ind: dict                     # indicator_key -> question_key
    peers: dict
    refs: dict
    rule_warns: list


# ------------------------------------------------------------------ fonctions pures

def peer_agreement(ind: catalog.Indicator, value, st: PeerStat) -> tuple[float | None, dict | None]:
    if st.n < sv.SQ_PEER_MIN_SAMPLES:
        return None, None
    if st.kind == "num":
        med = statistics.median(st.values)
        mad = statistics.median(abs(v - med) for v in st.values)
        sigma = max(1.4826 * mad, 0.1 * abs(med), 0.02 * (ind.hi - ind.lo), 1e-9)
        z = abs(float(value) - med) / sigma
        if z <= sv.SQ_PEER_Z_MAX:
            return clamp01(1 - 0.5 * z / sv.SQ_PEER_Z_MAX), None
        return clamp01(0.5 - 0.1 * (z - sv.SQ_PEER_Z_MAX)), {"code": "peer_outlier", "z": round(z, 2),
                                                             "median": round(med, 2)}
    k = max(2, len(ind.cats)) if ind.kind == "category" else 2
    vals = value if isinstance(value, list) else [value]
    ps = [((st.counts.get(str(v), 0) + 1) / (st.n + k)) for v in vals]
    p = min(ps)
    ag = max(0.6, min(1.0, p * k) ** 0.5)
    info = {"code": "peer_unusual", "p": round(p, 3)} if p < 0.02 and st.n >= 20 else None
    return ag, info


def ref_agreement(ind: catalog.Indicator, value, ref: RefStat) -> float | None:
    import math
    if ref.kind == "num":
        x = float(value)
        if x <= 0 or ref.value <= 0:
            return None
        lr, soft, hard = abs(math.log(x / ref.value)), math.log(2), math.log(sv.SQ_REF_FACTOR)
        return 1.0 if lr <= soft else (0.3 if lr > hard else clamp01(1 - 0.7 * (lr - soft) / (hard - soft)))
    vals = value if isinstance(value, list) else [value]
    if ref.category not in vals and not (ind.kind == "share"):
        return 0.8
    return 1.0


def evaluate(f: Facts) -> QualityResult:
    reasons, sub = [], {}
    hard = cap = False

    def reject(code, **d):
        nonlocal hard
        hard = True
        reasons.append({"code": code, "severity": "reject", **d})

    # cohérence interne (avertissements de règles, les rejets sont bloqués dès la saisie)
    for w in f.rule_warns:
        reasons.append({"code": "rule_warning", "severity": "warn", "rule": w["id"], "message": w["message"]})
    sub["consistency"] = clamp01(1 - 0.25 * len(f.rule_warns))

    # rythme de saisie
    ratio = f.duration_seconds / max(f.expected_seconds, 1e-6) if f.duration_seconds < 7200 else 1.0
    if ratio < sv.SQ_TOO_FAST_HARD:
        reject("too_fast", ratio=round(ratio, 2))
        sub["timing"] = 0.0
    else:
        sub["timing"] = clamp01((ratio - 0.2) / 0.6)
        if sub["timing"] < 0.5:
            reasons.append({"code": "fast_answer", "severity": "warn", "ratio": round(ratio, 2)})

    # effort / uniformité
    eff = 1.0
    if len(f.scale_values) >= sv.SQ_STRAIGHT_MIN_SCALE and len(set(f.scale_values)) == 1:
        eff = 0.3
        reasons.append({"code": "straight_lining", "severity": "warn"})
    if len(f.yes_values) >= 6 and all(f.yes_values):
        eff = min(eff, 0.6)
        reasons.append({"code": "all_yes", "severity": "warn"})
    sub["effort"] = eff

    # lieu
    if f.gps_accuracy_m > sv.SQ_GPS_REJECT_M:
        reject("gps_accuracy_too_low", value=f.gps_accuracy_m)
        loc = 0.0
    elif f.gps_accuracy_m <= sv.SQ_GPS_GOOD_M:
        loc = 1.0
    else:
        loc = clamp01(1 - (f.gps_accuracy_m - sv.SQ_GPS_GOOD_M) / (sv.SQ_GPS_REJECT_M - sv.SQ_GPS_GOOD_M))
    if f.speed_kmh is not None:
        if f.speed_kmh > sv.SQ_SPEED_REJECT_KMH:
            reject("impossible_travel", speed_kmh=round(f.speed_kmh))
        elif f.speed_kmh > sv.SQ_SPEED_WARN_KMH:
            loc *= 0.4
            reasons.append({"code": "impossible_travel", "severity": "warn", "speed_kmh": round(f.speed_kmh)})
    sub["location"] = loc

    # recoupement avec les voisins
    ags = []
    for key, st in f.peers.items():
        ind = catalog.INDICATORS[key]
        ag, info = peer_agreement(ind, f.values[key], st)
        if ag is None:
            continue
        ags.append(ag)
        if info:
            reasons.append({"severity": "warn" if info["code"] == "peer_outlier" else "info", "indicator": key, **info})
            cap = cap or info["code"] == "peer_outlier"
    if ags:
        sub["peers"] = sum(ags) / len(ags)
    else:
        sub["peers"] = 0.7
        reasons.append({"code": "insufficient_peers", "severity": "info"})

    # recoupement avec les valeurs de référence fournies
    rags = []
    for key, ref in f.refs.items():
        ag = ref_agreement(catalog.INDICATORS[key], f.values[key], ref)
        if ag is not None:
            rags.append(ag)
            if ag < 0.5:
                reasons.append({"code": "reference_gap", "severity": "warn", "indicator": key})
    sub["reference"] = sum(rags) / len(rags) if rags else 0.8

    # preuves photo
    allev = [e for lst in f.evidence.values() for e in lst]
    if f.evidence_required:
        have = {k for k in f.evidence_required if any(e.ok for e in f.evidence.get(k, []))}
        share = len(have) / len(f.evidence_required)
        sub["evidence"] = share
        if share < 1:
            cap = True
            reasons.append({"code": "evidence_missing", "severity": "warn",
                            "questions": [k for k in f.evidence_required if k not in have]})
    else:
        sub["evidence"] = 1.0 if any(e.ok for e in allev) else 0.85
    for e in allev:
        if e.dup_other:
            reject("evidence_duplicate", question=e.key)
        if e.time_bad:
            reasons.append({"code": "evidence_time_mismatch", "severity": "warn", "question": e.key})
        if e.gps_bad:
            reasons.append({"code": "evidence_location_mismatch", "severity": "warn", "question": e.key})
        if e.dup_elsewhere_self:
            reasons.append({"code": "evidence_reused", "severity": "warn", "question": e.key})
    if any(not e.ok or e.dup_elsewhere_self for e in allev):
        sub["evidence"] = max(0.0, sub["evidence"] - 0.3)

    # réputation du répondant (calculée sur ses réponses passées uniquement)
    if f.trust is not None and f.trust_n >= 3:
        sub["reputation"] = f.trust
    else:
        sub["reputation"] = 0.6
        reasons.append({"code": "new_respondent", "severity": "info"})

    # comportement personnel et motifs de copie
    beh = 1.0
    if f.own_n >= 5 and f.own_median_duration and f.duration_seconds < 0.4 * f.own_median_duration:
        beh = 0.4
        reasons.append({"code": "behavior_shift", "severity": "warn"})
    if f.fingerprint_dups >= sv.SQ_FINGERPRINT_MAX:
        beh = min(beh, 0.2)
        cap = True
        reasons.append({"code": "copy_pattern", "severity": "warn", "count": f.fingerprint_dups})
    sub["behavior"] = beh

    w = {"consistency": sv.SQ_W_CONSISTENCY, "timing": sv.SQ_W_TIMING, "effort": sv.SQ_W_EFFORT,
         "location": sv.SQ_W_LOCATION, "peers": sv.SQ_W_PEERS, "reference": sv.SQ_W_REFERENCE,
         "evidence": sv.SQ_W_EVIDENCE, "reputation": sv.SQ_W_REPUTATION, "behavior": sv.SQ_W_BEHAVIOR}
    score = sum(w[k] * sub[k] for k in w)
    if hard:
        status, score = QualityStatus.rejected, 0.0
    elif score >= sv.SQ_VALIDATED_MIN and not cap:
        status = QualityStatus.validated
    elif score >= sv.SQ_INDICATIVE_MIN:
        status = QualityStatus.indicative
    else:
        status = QualityStatus.rejected
        reasons.append({"code": "low_score", "severity": "reject", "score": round(score, 3)})
    return QualityResult(status=status, score=round(score, 4),
                         sub_scores={k: round(v, 3) for k, v in sub.items()}, reasons=reasons)


# ------------------------------------------------------------------ collecte des éléments de comparaison

async def _peers(db: AsyncSession, r: SurveyResponse, keys: list[str]) -> dict[str, PeerStat]:
    A = SurveyAnswer
    since = r.submitted_at - timedelta(days=sv.SQ_PEER_WINDOW_DAYS)
    rows = (await db.execute(select(A.indicator_key, A.value_num, A.value_cat).where(
        A.tenant_id == r.tenant_id, A.indicator_key.in_(keys), A.h3_index.in_(ring(r.h3_index, sv.SQ_PEER_RING_K)),
        A.status == QualityStatus.validated, A.answered_at >= since, A.response_id != r.id,
        A.contributor_hash != r.contributor_hash).limit(30000))).all()
    out: dict[str, PeerStat] = {}
    for key, num, cat in rows:
        ind = catalog.INDICATORS[key]
        st = out.setdefault(key, PeerStat("num" if ind.kind in ("number", "scale") else "cat", 0))
        if st.kind == "num":
            if num is not None and st.n < 600:
                st.values.append(num)
                st.n += 1
        elif ind.kind == "share":
            if num is not None and st.n < 600:
                st.counts[str(bool(num))] = st.counts.get(str(bool(num)), 0) + 1
                st.n += 1
        elif cat == "__all__":
            st.n += 1
        elif cat is not None:
            st.counts[cat] = st.counts.get(cat, 0) + 1
    return out


def _peer_value(ind, value):
    return value


async def _refs(db: AsyncSession, r: SurveyResponse, keys: list[str]) -> dict[str, RefStat]:
    rows = (await db.execute(select(ReferenceValue).where(
        ReferenceValue.tenant_id == r.tenant_id, ReferenceValue.indicator_key.in_(keys)))).scalars().all()
    import h3 as h3lib
    parents = {res: h3lib.cell_to_parent(r.h3_index, res) for res in range(5, 10)}
    best: dict[str, tuple[int, ReferenceValue]] = {}
    for v in rows:
        if v.scope_h3 is None:
            spec = 0
        elif v.scope_res and parents.get(v.scope_res) == v.scope_h3:
            spec = int(v.scope_res)
        else:
            continue
        cur = best.get(v.indicator_key)
        if cur is None or spec > cur[0] or (spec == cur[0] and (v.period_year or 0) > (cur[1].period_year or 0)):
            best[v.indicator_key] = (spec, v)
    out = {}
    for k, (_, v) in best.items():
        ind = catalog.INDICATORS[k]
        if ind.kind in ("number", "scale") and v.stat == "mean":
            out[k] = RefStat("num", v.value)
    return out


def _ev_facts(r: SurveyResponse, ev: SurveyEvidence, dup_other: bool, dup_self: bool) -> EvFacts:
    meta = ev.meta or {}
    f = EvFacts(ev.question_key, dup_other=dup_other, dup_elsewhere_self=dup_self, has_exif=bool(meta.get("taken_at") or meta.get("lat")))
    if meta.get("taken_at"):
        taken = datetime.fromisoformat(meta["taken_at"])
        delta = r.submitted_at - taken
        f.time_bad = delta < timedelta(minutes=-10) or delta > timedelta(hours=sv.SV_EVIDENCE_MAX_AGE_HOURS)
    if meta.get("lat") is not None and meta.get("lon") is not None:
        clat, clon = cell_center(r.h3_index)
        f.gps_bad = haversine_m(clat, clon, meta["lat"], meta["lon"]) > 900 + r.gps_accuracy
    return f


async def evaluate_response(db: AsyncSession, tenant_id: uuid.UUID, response_id: uuid.UUID,
                            force: bool = False) -> tuple[QualityStatus, uuid.UUID | None] | None:
    r = await db.get(SurveyResponse, response_id, with_for_update=True)
    if r is None or r.tenant_id != tenant_id:
        return None
    qrow = (await db.execute(select(SurveyResponseQuality).where(SurveyResponseQuality.response_id == r.id))).scalar_one_or_none()
    if qrow is not None and qrow.evaluator == "admin":
        return qrow.status, r.user_id
    ver = (await db.execute(select(SurveyVersion).where(
        SurveyVersion.survey_id == r.survey_id, SurveyVersion.version == r.survey_version))).scalar_one()
    defn = SurveyDefinition.model_validate(ver.definition)
    qm = {q.key: q for q in defn.questions}
    values = {qm[k].indicator: v for k, v in r.answers.items() if k in qm}
    kinds = {qm[k].indicator: qm[k].kind for k in r.answers if k in qm}
    q_by_ind = {qm[k].indicator: k for k in r.answers if k in qm}
    ft = r.features or {}
    expected = (sv.SQ_BASE_SECONDS + sv.SQ_SECONDS_PER_SLOT * ft.get("n_slots_picked", 0)
                + sv.SQ_SECONDS_PER_OVERRIDE * ft.get("n_overrides", 0) + sv.SQ_SECONDS_PER_DIRECT * ft.get("n_direct", 0))

    # déplacement impossible depuis la réponse précédente
    speed = None
    if r.user_id:
        prev = (await db.execute(select(SurveyResponse.h3_index, SurveyResponse.submitted_at).where(
            SurveyResponse.user_id == r.user_id, SurveyResponse.submitted_at < r.submitted_at,
            SurveyResponse.id != r.id).order_by(SurveyResponse.submitted_at.desc()).limit(1))).first()
        if prev:
            (a_lat, a_lon), (b_lat, b_lon) = cell_center(prev[0]), cell_center(r.h3_index)
            d = max(0.0, haversine_m(a_lat, a_lon, b_lat, b_lon) - 400.0)
            gap_h = max((r.submitted_at - prev[1]).total_seconds() / 3600.0, 1 / 60)
            speed = d / 1000.0 / gap_h
    # comportement personnel
    durs, trust_row = [], None
    if r.user_id:
        durs = [d for (d,) in (await db.execute(select(SurveyResponse.duration_seconds).where(
            SurveyResponse.user_id == r.user_id, SurveyResponse.id != r.id,
            SurveyResponse.submitted_at < r.submitted_at).order_by(SurveyResponse.submitted_at.desc()).limit(20))).all()]
        trust_row = await db.get(RespondentTrust, r.user_id)
    # empreintes identiques (réponses personnalisées copiées par d'autres répondants)
    dups = 0
    if r.custom_fingerprint:
        dups = (await db.execute(select(func.count(func.distinct(SurveyResponse.contributor_hash))).where(
            SurveyResponse.tenant_id == tenant_id, SurveyResponse.survey_id == r.survey_id,
            SurveyResponse.custom_fingerprint == r.custom_fingerprint, SurveyResponse.id != r.id,
            SurveyResponse.contributor_hash != r.contributor_hash,
            SurveyResponse.submitted_at >= r.submitted_at - timedelta(hours=sv.SQ_FINGERPRINT_WINDOW_HOURS)))).scalar_one()
    # preuves
    evs = list((await db.execute(select(SurveyEvidence).where(SurveyEvidence.response_id == r.id))).scalars())
    evid: dict[str, list[EvFacts]] = {}
    for e in evs:
        dup_other = dup_self = False
        if r.user_id:
            others = (await db.execute(select(SurveyResponse.user_id).join(
                SurveyEvidence, SurveyEvidence.response_id == SurveyResponse.id).where(
                SurveyEvidence.sha256 == e.sha256, SurveyEvidence.id != e.id))).scalars().all()
            dup_other = any(u != r.user_id for u in others)
            dup_self = any(u == r.user_id for u in others)
        evid.setdefault(e.question_key, []).append(_ev_facts(r, e, dup_other, dup_self))
    required = [q.key for q in defn.questions if q.evidence == "required" and q.key in r.answers]
    # voisins et références
    keys = [k for k in values if catalog.INDICATORS[k].kind != "category" or True]
    peers = await _peers(db, r, keys)
    refs = await _refs(db, r, keys)
    scale_vals = [v for k, v in r.answers.items() if k in qm and qm[k].kind == "scale"]
    yes_vals = [v for k, v in r.answers.items() if k in qm and qm[k].kind == "yes_no"]
    extreme = [v in (1, 5) for v in scale_vals]
    facts = Facts(values=values, kinds=kinds, duration_seconds=r.duration_seconds, expected_seconds=expected,
                  gps_accuracy_m=r.gps_accuracy, speed_kmh=speed, fingerprint_dups=int(dups), scale_values=scale_vals,
                  yes_values=yes_vals, own_median_duration=statistics.median(durs) if durs else None, own_n=len(durs),
                  trust=trust_row.trust if trust_row else None, trust_n=trust_row.n_responses if trust_row else 0,
                  evidence_required=required, evidence=evid, q_by_ind=q_by_ind, peers=peers, refs=refs,
                  rule_warns=[h for h in (r.flags or []) if h.get("severity") == "warn"])
    res = evaluate(facts)
    now = datetime.now(timezone.utc)
    await db.execute(pg_insert(SurveyResponseQuality).values(
        id=uuid.uuid4(), tenant_id=tenant_id, response_id=r.id, status=res.status, score=res.score,
        sub_scores=res.sub_scores, reasons=res.reasons, evaluator="auto", evaluated_at=now,
    ).on_conflict_do_update(index_elements=["response_id"], set_={
        "status": res.status, "score": res.score, "sub_scores": res.sub_scores, "reasons": res.reasons,
        "evaluator": "auto", "override_by": None, "evaluated_at": now}))
    trust_eff = facts.trust if facts.trust is not None else 0.6
    weight = round(res.score * (0.5 + 0.5 * trust_eff), 4) if res.status != QualityStatus.rejected else 0.0
    r.features = {**ft, "extreme_share": round(sum(extreme) / len(extreme), 3) if extreme else None,
                  "yes_share": round(sum(yes_vals) / len(yes_vals), 3) if yes_vals else None, "scale_n": len(scale_vals)}
    r.quality_status, r.weight, r.evaluated_at = res.status, weight, now
    proved = any(e.ok for lst in evid.values() for e in lst) and res.status != QualityStatus.rejected
    await db.execute(update(SurveyAnswer).where(SurveyAnswer.response_id == r.id).values(
        status=res.status, weight=weight, proved=proved))
    for e in evs:
        bad = any(not x.ok for x in evid.get(e.question_key, []))
        e.status = EvidenceStatus.rejected if bad else EvidenceStatus.accepted
    return res.status, r.user_id


async def apply_decision(db: AsyncSession, r: SurveyResponse, status: QualityStatus, admin_id: uuid.UUID,
                         reason: str) -> None:
    now = datetime.now(timezone.utc)
    q = (await db.execute(select(SurveyResponseQuality).where(SurveyResponseQuality.response_id == r.id))).scalar_one_or_none()
    entry = {"code": "admin_override", "severity": "info", "reason": reason}
    if q is None:
        db.add(SurveyResponseQuality(tenant_id=r.tenant_id, response_id=r.id, status=status, score=0.0, sub_scores={},
                                     reasons=[entry], evaluator="admin", override_by=admin_id, evaluated_at=now))
    else:
        q.status, q.evaluator, q.override_by, q.evaluated_at = status, "admin", admin_id, now
        q.reasons = [*(q.reasons or []), entry]
    weight = {QualityStatus.validated: 1.0, QualityStatus.indicative: 0.5}.get(status, 0.0)
    r.quality_status, r.weight, r.evaluated_at = status, weight, now
    await db.execute(update(SurveyAnswer).where(SurveyAnswer.response_id == r.id).values(status=status, weight=weight))