import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account_settings import account_settings as acs
from app.core.config import settings
from app.models.enums import MissionStatus, QualityStatus
from app.models.measurement import Measurement, MeasurementQuality
from app.models.quota import Mission
from app.models.social import MeasurementReview, ReviewVote, UserProfile
from app.models.tenancy import User
from app.services.geo import to_local

_P = "t:{}:presence"


# ------------------------------------------------------------------ éligibilité

def _crit(key, label, value, required, reverse=False):
    ok = value <= required if reverse else value >= required
    return {"key": key, "label": label, "value": value, "required": required, "ok": bool(ok), "reverse": reverse}


async def eligibility_bulk(db: AsyncSession, tenant_id: uuid.UUID, user_ids: list[uuid.UUID], now: datetime) -> dict:
    out = {u: {"v": 0, "n": 0, "reward": 0.0, "last": None, "days": 0, "missions": 0, "created": None} for u in user_ids}
    if not user_ids:
        return out
    since = now - timedelta(days=90)
    reward = {QualityStatus.validated: 1.0, QualityStatus.indicative: 0.5, QualityStatus.rejected: 0.0}
    for uid, st, n in (await db.execute(select(Measurement.user_id, Measurement.quality_status, func.count()).where(
            Measurement.tenant_id == tenant_id, Measurement.user_id.in_(user_ids), Measurement.measured_at >= since,
            Measurement.quality_status != QualityStatus.pending).group_by(Measurement.user_id, Measurement.quality_status))).all():
        out[uid]["n"] += n
        out[uid]["reward"] += reward[st] * n
        out[uid]["v"] += n if st == QualityStatus.validated else 0
    for uid, last in (await db.execute(select(Measurement.user_id, func.max(Measurement.measured_at)).where(
            Measurement.tenant_id == tenant_id, Measurement.user_id.in_(user_ids),
            Measurement.measured_at >= since).group_by(Measurement.user_id))).all():
        out[uid]["last"] = last
    for uid, d in (await db.execute(select(Measurement.user_id, func.count(func.distinct(Measurement.local_date))).where(
            Measurement.tenant_id == tenant_id, Measurement.user_id.in_(user_ids),
            Measurement.measured_at >= now - timedelta(days=60),
            Measurement.quality_status != QualityStatus.rejected).group_by(Measurement.user_id))).all():
        out[uid]["days"] = d
    for uid, n in (await db.execute(select(Mission.user_id, func.count()).where(
            Mission.tenant_id == tenant_id, Mission.user_id.in_(user_ids),
            Mission.status == MissionStatus.completed).group_by(Mission.user_id))).all():
        out[uid]["missions"] = n
    for uid, c in (await db.execute(select(User.id, User.created_at).where(User.id.in_(user_ids)))).all():
        out[uid]["created"] = c
    res = {}
    for uid, d in out.items():
        rel = (d["reward"] + 3.0) / (d["n"] + 5.0)
        since_last = (now - d["last"]).days if d["last"] else 999
        crit = [_crit("validated", "Mesures validées (90 j)", d["v"], acs.ANALYST_MIN_VALIDATED),
                _crit("missions", "Missions réussies", d["missions"], acs.ANALYST_MIN_MISSIONS),
                _crit("reliability", "Fiabilité (%)", round(rel * 100), round(acs.ANALYST_MIN_RELIABILITY * 100)),
                _crit("account_days", "Ancienneté du compte (jours)",
                      (now - d["created"]).days if d["created"] else 0, acs.ANALYST_MIN_ACCOUNT_DAYS),
                _crit("active_days", "Jours d'activité (60 j)", d["days"], acs.ANALYST_MIN_ACTIVE_DAYS),
                _crit("recent", "Jours depuis la dernière mesure (maximum)", since_last,
                      acs.ANALYST_MAX_INACTIVE_DAYS, reverse=True)]
        res[uid] = {"eligible": all(c["ok"] for c in crit), "criteria": crit, "reliability": rel}
    return res


async def agreement_bulk(db: AsyncSession, tenant_id: uuid.UUID, user_ids: list[uuid.UUID], now: datetime) -> dict:
    rows = (await db.execute(select(ReviewVote.reviewer_id, func.sum(case((ReviewVote.matched.is_(True), 1), else_=0)),
                                    func.count()).where(
        ReviewVote.tenant_id == tenant_id, ReviewVote.reviewer_id.in_(user_ids), ReviewVote.matched.is_not(None),
        ReviewVote.voted_at >= now - timedelta(days=60)).group_by(ReviewVote.reviewer_id))).all()
    return {u: (int(m or 0), int(n)) for u, m, n in rows}


async def analyst_state(db: AsyncSession, user: User) -> dict:
    from app.services.accounts import ensure_profile
    now = datetime.now(timezone.utc)
    p = await ensure_profile(db, user)
    el = (await eligibility_bulk(db, user.tenant_id, [user.id], now))[user.id]
    m, n = (await agreement_bulk(db, user.tenant_id, [user.id], now)).get(user.id, (0, 0))
    return {"enabled": p.analyst_enabled, "suspended_until": p.analyst_suspended_until,
            "eligibility": {"eligible": el["eligible"], "criteria": el["criteria"]},
            "stats": {"votes": n, "matched": m, "agreement": round((m + 3) / (n + 5), 3)}}


async def set_analyst(db: AsyncSession, user: User, enable: bool) -> None:
    from app.services.accounts import ensure_profile
    from app.services.user import latest_consents
    from app.models.enums import ConsentType
    p = await ensure_profile(db, user)
    if enable:
        c = await latest_consents(db, user.id)
        if ConsentType.geolocation not in c or not c[ConsentType.geolocation].granted:
            raise HTTPException(403, "consent_required")
        el = (await eligibility_bulk(db, user.tenant_id, [user.id], datetime.now(timezone.utc)))[user.id]
        if not el["eligible"]:
            raise HTTPException(409, "analyst_not_eligible")
        if not p.analyst_enabled:
            p.analyst_since = datetime.now(timezone.utc)
    p.analyst_enabled = enable
    await db.commit()


# ------------------------------------------------------------------ présence (éphémère, Redis)

async def heartbeat(db: AsyncSession, redis: Redis, user: User, lat: float, lon: float) -> int:
    from app.services.accounts import ensure_profile
    p = await ensure_profile(db, user)
    now = datetime.now(timezone.utc)
    if not p.analyst_enabled or (p.analyst_suspended_until and p.analyst_suspended_until > now):
        raise HTTPException(403, "not_analyst")
    key = _P.format(user.tenant_id)
    pipe = redis.pipeline()
    pipe.geoadd(key, (round(lon, 4), round(lat, 4), str(user.id)))
    pipe.zadd(key + ":ts", {str(user.id): now.timestamp()})
    pipe.expire(key, 3600)
    pipe.expire(key + ":ts", 3600)
    await pipe.execute()
    return (await db.execute(select(func.count()).select_from(ReviewVote).where(
        ReviewVote.reviewer_id == user.id, ReviewVote.status == "pending", ReviewVote.expires_at > now))).scalar_one()


async def purge_presence(redis: Redis, tenant_id: uuid.UUID) -> None:
    key = _P.format(tenant_id)
    stale = await redis.zrangebyscore(key + ":ts", 0, datetime.now(timezone.utc).timestamp() - acs.PRESENCE_TTL_SECONDS)
    if stale:
        await redis.zrem(key, *stale)
        await redis.zrem(key + ":ts", *stale)


async def _nearby(redis: Redis, tenant_id: uuid.UUID, lat: float, lon: float) -> list[tuple[uuid.UUID, float]]:
    key = _P.format(tenant_id)
    res = await redis.geosearch(key, longitude=lon, latitude=lat, radius=acs.REVIEW_RADIUS_M, unit="m",
                                sort="ASC", count=60, withdist=True)
    if not res:
        return []
    ids = [r[0] for r in res]
    ts = await redis.zmscore(key + ":ts", ids)
    limit = datetime.now(timezone.utc).timestamp() - acs.PRESENCE_TTL_SECONDS
    return [(uuid.UUID(i), float(d)) for (i, d), t in zip(res, ts) if t is not None and t >= limit]


# ------------------------------------------------------------------ envoi des demandes d'avis

async def dispatch(db: AsyncSession, redis: Redis, tenant_id: uuid.UUID, measurement_id: uuid.UUID) -> str:
    m = await db.get(Measurement, measurement_id)
    if m is None or m.user_id is None:
        return "gone"
    now = datetime.now(timezone.utc)
    if now - m.measured_at > timedelta(minutes=acs.REVIEW_MAX_AGE_MINUTES):
        return "stale"                       # on ne juge que le volume du moment
    if m.quality_status not in (QualityStatus.validated, QualityStatus.indicative):
        return "not_eligible"
    q = (await db.execute(select(MeasurementQuality).where(MeasurementQuality.measurement_id == m.id))).scalar_one_or_none()
    if q is not None and q.evaluator in ("admin", "peer"):
        return "admin_priority"              # la décision d'un admin prime : aucun avis demandé
    if (await db.execute(select(MeasurementReview.id).where(MeasurementReview.measurement_id == m.id))).first():
        return "exists"
    near = [(u, d) for u, d in await _nearby(redis, tenant_id, m.latitude, m.longitude) if u != m.user_id]
    if len(near) < acs.REVIEW_MIN_VOTES:
        return "no_reviewers"
    ids = [u for u, _ in near]
    profiles = {p.user_id: p for p in (await db.execute(select(UserProfile).where(UserProfile.user_id.in_(ids)))).scalars()}
    pend = dict((await db.execute(select(ReviewVote.reviewer_id, func.count()).where(
        ReviewVote.reviewer_id.in_(ids), ReviewVote.status == "pending", ReviewVote.expires_at > now)
        .group_by(ReviewVote.reviewer_id))).all())
    cand = [(u, d) for u, d in near if (p := profiles.get(u)) and p.analyst_enabled
            and not (p.analyst_suspended_until and p.analyst_suspended_until > now)
            and pend.get(u, 0) < acs.REVIEW_MAX_PENDING_PER_USER]
    el = await eligibility_bulk(db, tenant_id, [u for u, _ in cand], now)
    agr = await agreement_bulk(db, tenant_id, [u for u, _ in cand], now)
    scored = []
    for u, d in cand:
        if not el[u]["eligible"]:
            continue
        mt, n = agr.get(u, (0, 0))
        w = (0.5 + el[u]["reliability"]) * (1.0 - 0.4 * d / acs.REVIEW_RADIUS_M) * (0.7 + 0.6 * (mt + 3) / (n + 5))
        scored.append((w, u, d))
    scored.sort(key=lambda t: t[0], reverse=True)
    chosen = scored[:acs.REVIEW_MAX_REVIEWERS]
    if len(chosen) < acs.REVIEW_MIN_VOTES:
        return "no_reviewers"
    exp = now + timedelta(minutes=acs.REVIEW_TTL_MINUTES)
    level = round(m.laeq + m.correction_db_applied, 1)
    review = MeasurementReview(tenant_id=tenant_id, measurement_id=m.id, status="open", requested=len(chosen),
                               expires_at=exp)
    db.add(review)
    await db.flush()
    for w, u, d in chosen:
        db.add(ReviewVote(tenant_id=tenant_id, review_id=review.id, reviewer_id=u, status="pending", level_db=level,
                          measured_at=m.measured_at, distance_m=d, weight=w, expires_at=exp))
    return "dispatched"


# ------------------------------------------------------------------ vote et décision

def decide(votes: list[tuple[str, bool, float]]) -> dict:
    tw = sum(w for _, _, w in votes) or 1.0
    a = sum(w for v, _, w in votes if v == "agree") / tw
    d = sum(w for v, _, w in votes if v == "down") / tw
    u = sum(w for v, _, w in votes if v == "up") / tw
    base = {"agree": a, "down": d, "up": u, "adjust": 0.0}
    if a >= acs.REVIEW_CONFIRM_RATIO:
        return {**base, "outcome": "confirmed"}
    direction, ratio = ("down", d) if d >= u else ("up", u)
    if ratio >= acs.REVIEW_ADJUST_RATIO:
        dir_w = sum(w for v, _, w in votes if v == direction)
        strong_w = sum(w for v, s, w in votes if v == direction and s)
        if dir_w and strong_w / dir_w >= acs.REVIEW_STRONG_REJECT_SHARE:
            return {**base, "outcome": "rejected"}          # écart important : trop grand pour un simple ajustement
        mag = min(acs.REVIEW_ADJUST_MAX_DB, acs.REVIEW_ADJUST_STEP_DB * (1 + 4 * (ratio - 0.5)))
        mag = round(mag * 2) / 2
        return {**base, "outcome": f"adjusted_{direction}", "adjust": -mag if direction == "down" else mag}
    return {**base, "outcome": "rejected"}                  # réponses contradictoires


async def finalize(db: AsyncSession, review_id: uuid.UUID, now: datetime) -> str | None:
    r = await db.get(MeasurementReview, review_id, with_for_update=True)
    if r is None or r.status != "open":
        return None
    votes = list((await db.execute(select(ReviewVote).where(ReviewVote.review_id == r.id))).scalars())
    for v in votes:
        if v.status == "pending":
            v.status = "expired"
    voted = [v for v in votes if v.status == "voted"]
    m = await db.get(Measurement, r.measurement_id, with_for_update=True)
    q = (await db.execute(select(MeasurementQuality).where(MeasurementQuality.measurement_id == r.measurement_id))).scalar_one_or_none()
    r.status, r.closed_at, r.votes = "closed", now, len(voted)
    if m is None or q is None or q.evaluator == "admin":
        r.outcome = "admin_skip"
        return r.outcome
    if len(voted) < acs.REVIEW_MIN_VOTES:
        r.outcome = "no_quorum"
        return r.outcome
    res = decide([(v.vote, v.strong, v.weight) for v in voted])
    r.outcome, r.adjust_db = res["outcome"], res["adjust"]
    r.agree_ratio, r.down_ratio, r.up_ratio = round(res["agree"], 3), round(res["down"], 3), round(res["up"], 3)
    reason = {"code": f"peer_{res['outcome']}" if res["outcome"] != "confirmed" else "peer_confirmed",
              "severity": "info", "votes": len(voted), "agree": r.agree_ratio, "adjust_db": res["adjust"]}
    if res["outcome"] == "rejected":
        reason["code"], reason["severity"] = "peer_rejected", "reject"
        m.quality_status, q.status, q.score = QualityStatus.rejected, QualityStatus.rejected, 0.0
    else:
        m.quality_status, q.status = QualityStatus.validated, QualityStatus.validated
        q.score = max(q.score, settings.VALIDATED_MIN_SCORE)
        if res["adjust"]:
            m.correction_db_applied = float(m.correction_db_applied or 0) + res["adjust"]
    q.evaluator, q.override_by, q.evaluated_at = "peer", None, now
    q.reasons = [*(q.reasons or []), reason]
    m.aggregated_at = None                                   # recalcul des agrégats concernés
    for v in voted:
        v.matched = (v.vote == "agree") if res["outcome"] in ("confirmed", "rejected") and res["outcome"] == "confirmed" \
            else (v.vote != "agree") if res["outcome"] == "rejected" else (v.vote == res["outcome"].split("_")[1])
    return r.outcome


async def cast_vote(db: AsyncSession, user: User, vote_id: uuid.UUID, vote: str, strong: bool) -> dict:
    v = await db.get(ReviewVote, vote_id, with_for_update=True)
    if v is None or v.reviewer_id != user.id:
        raise HTTPException(404, "review_not_found")
    now = datetime.now(timezone.utc)
    if v.status != "pending" or v.expires_at <= now:
        raise HTTPException(409, "review_expired")
    v.vote, v.strong, v.status, v.voted_at = vote, strong and vote != "agree", "voted", now
    await db.flush()
    left = (await db.execute(select(func.count()).select_from(ReviewVote).where(
        ReviewVote.review_id == v.review_id, ReviewVote.status == "pending"))).scalar_one()
    outcome = await finalize(db, v.review_id, now) if left == 0 else None
    await db.commit()
    return {"ok": True, "closed": outcome is not None}


async def close_expired(db: AsyncSession, redis: Redis, tenant_id: uuid.UUID) -> int:
    now = datetime.now(timezone.utc)
    ids = (await db.execute(select(MeasurementReview.id).where(
        MeasurementReview.tenant_id == tenant_id, MeasurementReview.status == "open",
        MeasurementReview.expires_at <= now).limit(200))).scalars().all()
    for rid in ids:
        await finalize(db, rid, now)
    await purge_presence(redis, tenant_id)
    return len(ids)


async def cancel_rounds(db: AsyncSession, measurement_ids: list[uuid.UUID]) -> None:
    """Une décision admin saute le processus d'avis : manches ouvertes closes, demandes annulées."""
    now = datetime.now(timezone.utc)
    rids = (await db.execute(select(MeasurementReview.id).where(
        MeasurementReview.measurement_id.in_(measurement_ids), MeasurementReview.status == "open"))).scalars().all()
    if rids:
        await db.execute(update(ReviewVote).where(ReviewVote.review_id.in_(rids), ReviewVote.status == "pending")
                         .values(status="cancelled"))
        await db.execute(update(MeasurementReview).where(MeasurementReview.id.in_(rids))
                         .values(status="closed", outcome="admin_skip", closed_at=now))


async def audit_analysts(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(ReviewVote.reviewer_id, func.sum(case((ReviewVote.matched.is_(True), 1), else_=0)),
                                    func.count()).where(
        ReviewVote.tenant_id == tenant_id, ReviewVote.matched.is_not(None),
        ReviewVote.voted_at >= now - timedelta(days=60)).group_by(ReviewVote.reviewer_id)
        .having(func.count() >= acs.ANALYST_SUSPEND_MIN_VOTES))).all()
    bad = [u for u, m, n in rows if (m or 0) / n < acs.ANALYST_SUSPEND_AGREEMENT]
    if not bad:
        return 0
    res = await db.execute(update(UserProfile).where(
        UserProfile.user_id.in_(bad), UserProfile.analyst_enabled.is_(True),
        (UserProfile.analyst_suspended_until.is_(None)) | (UserProfile.analyst_suspended_until < now))
        .values(analyst_suspended_until=now + timedelta(days=acs.ANALYST_SUSPEND_DAYS)))
    return res.rowcount or 0


# ------------------------------------------------------------------ lecture

async def pending_for(db: AsyncSession, user: User) -> list[dict]:
    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(ReviewVote).where(
        ReviewVote.reviewer_id == user.id, ReviewVote.status == "pending", ReviewVote.expires_at > now)
        .order_by(ReviewVote.expires_at))).scalars().all()
    return [{"id": str(v.id), "level_db": v.level_db, "distance_m": round(v.distance_m, -1),
             "measured_at": v.measured_at, "expires_at": v.expires_at} for v in rows]


async def history_for(db: AsyncSession, user: User, limit: int) -> list[dict]:
    rows = (await db.execute(select(ReviewVote, MeasurementReview.outcome)
                             .join(MeasurementReview, MeasurementReview.id == ReviewVote.review_id)
                             .where(ReviewVote.reviewer_id == user.id, ReviewVote.status.in_(["voted", "expired"]))
                             .order_by(ReviewVote.created_at.desc()).limit(limit))).all()
    return [{"id": str(v.id), "vote": v.vote, "strong": v.strong, "level_db": v.level_db, "outcome": o,
             "matched": v.matched, "voted_at": v.voted_at or v.expires_at, "status": v.status} for v, o in rows]


async def analysts_admin(db: AsyncSession, tenant_id: uuid.UUID) -> list[dict]:
    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(UserProfile, User.pseudonym).join(User, User.id == UserProfile.user_id).where(
        UserProfile.tenant_id == tenant_id, UserProfile.analyst_enabled.is_(True)))).all()
    agr = await agreement_bulk(db, tenant_id, [p.user_id for p, _ in rows], now) if rows else {}
    return [{"user_id": str(p.user_id), "pseudonym": ps, "display_name": p.display_name, "since": p.analyst_since,
             "suspended_until": p.analyst_suspended_until, "votes": agr.get(p.user_id, (0, 0))[1],
             "agreement": round((agr.get(p.user_id, (0, 0))[0] + 3) / (agr.get(p.user_id, (0, 0))[1] + 5), 3)}
            for p, ps in rows]