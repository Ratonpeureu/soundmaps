import uuid
from datetime import datetime, timedelta, timezone

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.survey_settings import survey_settings as sv
from app.models.enums import QualityStatus
from app.models.survey import RespondentTrust, SurveyResponse, SurveyResponseQuality

PERSONAS = {"nouveau": "Nouveau répondant", "attentif": "Répondant attentif", "presse": "Répondant pressé",
            "uniforme": "Réponses très uniformes", "extreme": "Réponses tranchées",
            "incoherent": "Réponses souvent incohérentes", "regulier": "Répondant régulier"}
TIPS = {"presse": "Prenez le temps de lire les profils avant de choisir : les réponses trop rapides comptent moins.",
        "uniforme": "Variez vos réponses quand la situation le justifie : répondre pareil partout est signalé.",
        "extreme": "Nuancez : les échelles 1 et 5 ne sont pas toujours les plus justes.",
        "incoherent": "Vérifiez vos chiffres avant d'envoyer (totaux, nombres de personnes…).",
        "nouveau": "Quelques réponses fiables suffisent pour que votre confiance se construise.",
        "attentif": "Merci : vos réponses ont un poids élevé sur les cartes.",
        "regulier": "Merci pour votre régularité."}


def _rate(n, d):
    return n / d if d else 0.0


async def refresh_user(db: AsyncSession, redis: Redis, tenant_id: uuid.UUID, user_id: uuid.UUID) -> RespondentTrust | None:
    since = datetime.now(timezone.utc) - timedelta(days=sv.ST_WINDOW_DAYS)
    rows = (await db.execute(
        select(SurveyResponse.quality_status, SurveyResponse.features, SurveyResponse.submitted_at,
               SurveyResponse.survey_id, SurveyResponseQuality.reasons, SurveyResponseQuality.score)
        .join(SurveyResponseQuality, SurveyResponseQuality.response_id == SurveyResponse.id)
        .where(SurveyResponse.tenant_id == tenant_id, SurveyResponse.user_id == user_id,
               SurveyResponse.submitted_at >= since, SurveyResponse.quality_status != QualityStatus.pending)
        .order_by(SurveyResponse.submitted_at.desc()).limit(300))).all()
    n = len(rows)
    if n == 0:
        return None
    v = sum(1 for r in rows if r[0] == QualityStatus.validated)
    i = sum(1 for r in rows if r[0] == QualityStatus.indicative)
    codes = [{c.get("code") for c in (r[4] or [])} for r in rows]

    def cnt(*names):
        return sum(1 for s in codes if s & set(names))

    fast, uniform = _rate(cnt("too_fast", "fast_answer"), n), _rate(cnt("straight_lining", "all_yes"), n)
    dup_photo = _rate(cnt("evidence_duplicate", "evidence_reused"), n)
    shift, outlier = _rate(cnt("behavior_shift", "copy_pattern", "impossible_travel"), n), _rate(cnt("peer_outlier"), n)
    attempts = int(await redis.get(f"svq:att:{tenant_id}:{user_id}") or 0)
    incoh = min(1.0, attempts / max(5, n))
    ext = [r[1].get("extreme_share") for r in rows if (r[1] or {}).get("extreme_share") is not None]
    extremity = sum(ext) / len(ext) if ext else 0.0
    s, p0 = sv.ST_STRENGTH, sv.ST_PRIOR
    rel = (v + 0.5 * i + s * p0) / (n + s)
    penalty = 1 - min(0.6, 0.35 * uniform + 0.25 * fast + 0.3 * dup_photo + 0.3 * incoh + 0.2 * shift + 0.15 * outlier)
    trust = max(0.05, min(1.0, rel * penalty))
    if n < 3:
        persona = "nouveau"
    elif incoh > 0.4 or _rate(sum(1 for r in rows if r[0] == QualityStatus.rejected), n) > 0.4:
        persona = "incoherent"
    elif uniform > 0.3:
        persona = "uniforme"
    elif fast > 0.3:
        persona = "presse"
    elif extremity > 0.7 and len(ext) >= 5:
        persona = "extreme"
    elif trust >= 0.75 and len({r[3] for r in rows}) >= 3:
        persona = "regulier"
    else:
        persona = "attentif" if trust >= 0.6 else "nouveau"
    scores = [r[5] for r in rows]
    trend = (sum(scores[:5]) / len(scores[:5]) - sum(scores[5:15]) / len(scores[5:15])) if len(scores) >= 10 else 0.0
    feats = {"fast_rate": round(fast, 3), "uniform_rate": round(uniform, 3), "dup_photo_rate": round(dup_photo, 3),
             "shift_rate": round(shift, 3), "outlier_rate": round(outlier, 3), "inconsistent_attempts": attempts,
             "extremity": round(extremity, 3), "validated_rate": round(_rate(v, n), 3)}
    flags = [k for k, x in (("fast", fast > 0.3), ("uniform", uniform > 0.3), ("photo", dup_photo > 0.1),
                            ("shift", shift > 0.2), ("incoherent", incoh > 0.4)) if x]
    now = datetime.now(timezone.utc)
    await db.execute(pg_insert(RespondentTrust).values(
        user_id=user_id, tenant_id=tenant_id, trust=round(trust, 4), n_responses=n, persona=persona,
        trend=round(trend, 4), features=feats, flags=flags, updated_at=now,
    ).on_conflict_do_update(index_elements=["user_id"], set_={
        "trust": round(trust, 4), "n_responses": n, "persona": persona, "trend": round(trend, 4),
        "features": feats, "flags": flags, "updated_at": now}))
    return await db.get(RespondentTrust, user_id)


def view(t: RespondentTrust | None) -> dict:
    if t is None:
        return {"persona": "nouveau", "label": PERSONAS["nouveau"], "trust_band": "en construction",
                "tip": TIPS["nouveau"], "responses": 0}
    band = "élevée" if t.trust >= 0.75 else "bonne" if t.trust >= 0.55 else "à renforcer"
    return {"persona": t.persona, "label": PERSONAS.get(t.persona, t.persona), "trust_band": band,
            "trust": round(t.trust, 2), "trend": "en hausse" if t.trend > 0.03 else "en baisse" if t.trend < -0.03 else "stable",
            "tip": TIPS.get(t.persona), "responses": t.n_responses}