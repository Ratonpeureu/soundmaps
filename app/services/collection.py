import math
import random
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.collection import (CollectionProposal, CollectionSchedule, TenantAlgorithmState, UserResponseStat)
from app.models.enums import ConsentType, DayPart, ProposalStatus, QualityStatus, ScheduleMode, TriggerMode
from app.models.geo import GeographicCell, NoiseAggregation
from app.models.measurement import Measurement, MeasurementQuality
from app.models.tenancy import User
from app.schemas.schemas import ProposalDecisionOut, ProposalOut, ProposalRequest
from app.services.acoustics import clamp01, leq_from_energy
from app.services.geo import TZ, cell_for, day_part_of, get_or_create_cell, ring, to_local
from app.services.map import confidence_of
from app.services.user import latest_consents

FEATURES = ("freshness", "noise_history", "coverage_gap", "time_relevance", "availability")
_REWARD = {QualityStatus.validated: 1.0, QualityStatus.indicative: 0.5, QualityStatus.rejected: 0.0}


def base_weights() -> dict[str, float]:
    return {"freshness": settings.W_FRESHNESS, "noise_history": settings.W_NOISE_HISTORY,
            "coverage_gap": settings.W_COVERAGE_GAP, "time_relevance": settings.W_TIME_RELEVANCE,
            "availability": settings.W_AVAILABILITY}


# ------------------------------------------------------------------ fonctions pures

@dataclass
class CellStats:
    last_measurement_at: datetime | None = None
    window_counts: dict[str, int] = field(default_factory=dict)   # plage horaire -> nb de mesures (fenêtre de couverture)
    daily_levels: list[float] = field(default_factory=list)
    level_db: float | None = None


def compute_features(stats: CellStats | None, now: datetime, current_part: DayPart, others_present: int) -> dict[str, float]:
    s = stats or CellStats()
    if s.last_measurement_at is None:
        freshness = 1.0
    else:
        hours = max(0.0, (now - s.last_measurement_at).total_seconds() / 3600)
        freshness = 1.0 - math.exp(-hours / settings.FRESHNESS_TAU_HOURS)

    if s.level_db is None:
        history = settings.UNKNOWN_HISTORY_PRIOR
    else:
        level_term = clamp01((s.level_db - settings.NOISE_REF_MIN_DB)
                             / (settings.NOISE_REF_MAX_DB - settings.NOISE_REF_MIN_DB))
        var_term = (clamp01(statistics.pstdev(s.daily_levels) / settings.NOISE_STD_REF_DB)
                    if len(s.daily_levels) >= 2 else settings.UNKNOWN_HISTORY_PRIOR)
        history = settings.NOISE_LEVEL_SHARE * level_term + (1 - settings.NOISE_LEVEL_SHARE) * var_term

    missing = sum(1 for p in DayPart if s.window_counts.get(p.value, 0) == 0) / len(DayPart)
    total = sum(s.window_counts.values())
    deficit = clamp01(1.0 - total / settings.TARGET_MEASUREMENTS_PER_CELL)
    coverage_gap = 0.5 * missing + 0.5 * deficit

    per_part_target = max(1.0, settings.TARGET_MEASUREMENTS_PER_CELL / len(DayPart))
    time_relevance = clamp01(1.0 - s.window_counts.get(current_part.value, 0) / per_part_target)

    return {"freshness": freshness, "noise_history": history, "coverage_gap": coverage_gap,
            "time_relevance": time_relevance, "availability": 1.0 / (1.0 + max(0, others_present))}


def score_of(features: dict[str, float], weights: dict[str, float]) -> float:
    return sum(weights[k] * features[k] for k in FEATURES)


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[int(q * (len(ordered) - 1))]


def acceptance_prob(rows: list[tuple[int, int, int]], hour: int) -> float:
    """rows = [(heure, acceptées, refusées)] ; rétrécissement à deux niveaux (global -> utilisateur -> heure)."""
    s, p0 = settings.ACCEPTANCE_PRIOR_STRENGTH, settings.ACCEPTANCE_PRIOR
    a_tot = sum(r[1] for r in rows)
    d_tot = sum(r[2] for r in rows)
    p_user = (a_tot + s * p0) / (a_tot + d_tot + s)
    a_h = sum(r[1] for r in rows if r[0] == hour)
    d_h = sum(r[2] for r in rows if r[0] == hour)
    return (a_h + s * p_user) / (a_h + d_h + s)


def schedule_allows(schedule: CollectionSchedule | None, local_now: datetime) -> bool:
    if schedule is None or not schedule.active:
        return False
    days, t = schedule.days_of_week, local_now.time()
    s, e = schedule.window_start, schedule.window_end
    dow = local_now.weekday()
    if s <= e:
        return dow in days and s <= t <= e
    if t >= s:
        return dow in days
    if t <= e:
        return ((dow - 1) % 7) in days
    return False


def near_preferred_time(schedule: CollectionSchedule, local_now: datetime) -> bool:
    now_min = local_now.hour * 60 + local_now.minute
    for t in schedule.preferred_times or []:
        diff = abs(now_min - (t.hour * 60 + t.minute))
        if min(diff, 1440 - diff) <= settings.SCHEDULED_TOLERANCE_MINUTES:
            return True
    return False


def eg_update(weights: dict[str, float], samples: list[tuple[dict[str, float], float]],
              base: dict[str, float]) -> dict[str, float]:
    """Exponentiated Gradient sur le simplexe : minimise (w·x - récompense)^2, avec rétrécissement vers les poids de base."""
    n = len(samples)
    grad = {k: 0.0 for k in FEATURES}
    for x, r in samples:
        err = sum(weights[k] * x[k] for k in FEATURES) - r
        for k in FEATURES:
            grad[k] += err * x[k]
    new = {k: weights[k] * math.exp(-settings.ADAPT_LEARNING_RATE * grad[k] / n) for k in FEATURES}
    total = sum(new.values())
    new = {k: v / total for k, v in new.items()}
    a = settings.ADAPT_SHRINKAGE
    new = {k: (1 - a) * new[k] + a * base[k] for k in FEATURES}
    for _ in range(10):                                      # projection approchée sur [min, max] avec somme = 1
        new = {k: min(max(v, settings.ADAPT_MIN_WEIGHT), settings.ADAPT_MAX_WEIGHT) for k, v in new.items()}
        total = sum(new.values())
        new = {k: v / total for k, v in new.items()}
    return new


# ------------------------------------------------------------------ accès données

async def get_weights(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, float]:
    state = await db.get(TenantAlgorithmState, tenant_id)
    return dict(state.weights) if state and set(state.weights) == set(FEATURES) else base_weights()


async def load_cell_stats(db: AsyncSession, tenant_id: uuid.UUID, h3_list: list[str],
                          now: datetime) -> dict[str, CellStats]:
    cells = (await db.execute(select(GeographicCell.id, GeographicCell.h3_index, GeographicCell.last_measurement_at)
                              .where(GeographicCell.tenant_id == tenant_id,
                                     GeographicCell.h3_index.in_(h3_list)))).all()
    by_id = {c.id: c.h3_index for c in cells}
    stats = {c.h3_index: CellStats(last_measurement_at=c.last_measurement_at) for c in cells}
    if not by_id:
        return stats
    local_now = now.astimezone(TZ)
    hist_since = (local_now - timedelta(days=settings.HISTORY_WINDOW_DAYS)).date()
    cov_since = (local_now - timedelta(days=settings.COVERAGE_WINDOW_DAYS)).date()
    rows = (await db.execute(
        select(NoiseAggregation.cell_id, NoiseAggregation.bucket_date, NoiseAggregation.day_part,
               func.sum(NoiseAggregation.measurement_count), func.sum(NoiseAggregation.energy_sum),
               func.sum(NoiseAggregation.duration_total))
        .where(NoiseAggregation.tenant_id == tenant_id, NoiseAggregation.cell_id.in_(list(by_id)),
               NoiseAggregation.bucket_date >= hist_since)
        .group_by(NoiseAggregation.cell_id, NoiseAggregation.bucket_date, NoiseAggregation.day_part))).all()
    energy: dict[str, float] = {}
    dur: dict[str, float] = {}
    day_e: dict[tuple[str, object], list[float]] = {}
    for cell_id, d, part, n, en, du in rows:
        h = by_id[cell_id]
        if d >= cov_since:
            stats[h].window_counts[part.value] = stats[h].window_counts.get(part.value, 0) + int(n)
        energy[h] = energy.get(h, 0.0) + float(en)
        dur[h] = dur.get(h, 0.0) + float(du)
        acc = day_e.setdefault((h, d), [0.0, 0.0])
        acc[0] += float(en)
        acc[1] += float(du)
    for h in stats:
        if dur.get(h, 0) > 0 and energy.get(h, 0) > 0:
            stats[h].level_db = leq_from_energy(energy[h], dur[h])
    for (h, _), (en, du) in day_e.items():
        if du > 0 and en > 0:
            stats[h].daily_levels.append(leq_from_energy(en, du))
    return stats


async def _presence(redis: Redis, tenant_id: uuid.UUID, h3_list: list[str], now_ts: float) -> dict[str, int]:
    window = settings.AVAILABILITY_WINDOW_MINUTES * 60
    pipe = redis.pipeline()
    for h in h3_list:
        key = f"t:{tenant_id}:avail:{h}"
        pipe.zremrangebyscore(key, 0, now_ts - window)
        pipe.zcard(key)
    res = await pipe.execute()
    return {h: int(res[2 * i + 1]) for i, h in enumerate(h3_list)}


async def confidence_snapshot(db: AsyncSession, tenant_id: uuid.UUID, h3_index: str, now: datetime) -> float:
    from sqlalchemy import case
    from app.models.enums import CalibrationStatus
    since = now - timedelta(days=settings.HISTORY_WINDOW_DAYS)
    n, devs, cal, last = (await db.execute(
        select(func.count(), func.count(func.distinct(Measurement.contributor_hash)),
               func.sum(case((Measurement.calibration_status == CalibrationStatus.calibrated, 1), else_=0)),
               func.max(Measurement.measured_at))
        .where(Measurement.tenant_id == tenant_id, Measurement.h3_index == h3_index,
               Measurement.measured_at >= since,
               Measurement.quality_status.in_([QualityStatus.validated, QualityStatus.indicative])))).one()
    return confidence_of(int(n), int(devs), int(cal or 0), last, now) if n else 0.0


def _proposal_out(p: CollectionProposal) -> ProposalOut:
    return ProposalOut(id=p.id, status=p.status, trigger=p.trigger, h3_index=p.h3_index,
                       priority=round(p.priority_score * 100, 1), expires_at=p.expires_at,
                       session_duration_seconds=settings.SESSION_DURATION_SECONDS)


async def _bump_response(db: AsyncSession, tenant_id, user_id, hour: int, accepted: bool) -> None:
    col = "accepted" if accepted else "declined"
    stmt = pg_insert(UserResponseStat).values(
        id=uuid.uuid4(), tenant_id=tenant_id, user_id=user_id, hour=hour,
        accepted=1 if accepted else 0, declined=0 if accepted else 1)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_response_stats_user_hour",
        set_={col: getattr(UserResponseStat, col) + 1})
    await db.execute(stmt)


# ------------------------------------------------------------------ décision de proposition

async def request_proposal(db: AsyncSession, redis: Redis, user: User, req: ProposalRequest) -> ProposalDecisionOut:
    now = datetime.now(timezone.utc)
    no = lambda reason: ProposalDecisionOut(proposed=False, reason=reason)

    if user.participation_paused:
        return no("participation_paused")
    if not user.hybrid_collection_enabled:
        return no("hybrid_disabled")
    consents = await latest_consents(db, user.id)
    if not all(t in consents and consents[t].granted for t in (ConsentType.geolocation, ConsentType.microphone)):
        return no("consent_missing")
    if req.battery_level < settings.MIN_BATTERY_LEVEL:
        return no("battery_low")

    schedule = (await db.execute(select(CollectionSchedule).where(CollectionSchedule.user_id == user.id))).scalar_one_or_none()
    local_now = now.astimezone(ZoneInfo(schedule.timezone)) if schedule else to_local(now)
    if not schedule_allows(schedule, local_now):
        return no("outside_schedule")
    wanted = {TriggerMode.scheduled: (ScheduleMode.scheduled, ScheduleMode.both),
              TriggerMode.contextual: (ScheduleMode.contextual, ScheduleMode.both)}[req.trigger]
    if schedule.mode not in wanted:
        return no("trigger_not_enabled")
    if req.trigger == TriggerMode.scheduled and not near_preferred_time(schedule, local_now):
        return no("not_a_scheduled_slot")

    open_p = (await db.execute(select(CollectionProposal).where(
        CollectionProposal.user_id == user.id,
        CollectionProposal.status.in_([ProposalStatus.pending, ProposalStatus.accepted]),
        CollectionProposal.expires_at > now).order_by(CollectionProposal.created_at.desc()).limit(1))).scalar_one_or_none()
    if open_p is not None:
        return ProposalDecisionOut(proposed=True, reason="existing_proposal", proposal=_proposal_out(open_p))

    day_start = datetime.combine(local_now.date(), time.min, local_now.tzinfo).astimezone(timezone.utc)
    today_count = (await db.execute(select(func.count()).where(
        CollectionProposal.user_id == user.id, CollectionProposal.created_at >= day_start))).scalar_one()
    if today_count >= settings.MAX_PROPOSALS_PER_USER_PER_DAY:
        return no("daily_cap_reached")
    last = (await db.execute(select(func.max(CollectionProposal.created_at)).where(
        CollectionProposal.user_id == user.id))).scalar_one()
    if last and now - last < timedelta(minutes=settings.MIN_MINUTES_BETWEEN_PROPOSALS):
        return no("too_soon")

    own = cell_for(req.latitude, req.longitude)
    cell = await get_or_create_cell(db, user.tenant_id, own)
    candidates = ring(own, settings.CANDIDATE_RING_K)
    stats = await load_cell_stats(db, user.tenant_id, candidates, now)      # uniquement les cellules déjà connues
    now_ts = now.timestamp()
    presence = await _presence(redis, user.tenant_id, list(stats), now_ts)
    others = presence.get(own, 0)
    key = f"t:{user.tenant_id}:avail:{own}"
    await redis.zadd(key, {user.pseudonym: now_ts})
    await redis.expire(key, settings.AVAILABILITY_WINDOW_MINUTES * 120)

    weights = await get_weights(db, user.tenant_id)
    part = day_part_of(to_local(now))
    features = compute_features(stats.get(own), now, part, others)
    own_score = score_of(features, weights)

    ring_scores = [score_of(compute_features(s, now, part, presence.get(h, 0)), weights)
                   for h, s in stats.items() if h != own]
    dyn = quantile(ring_scores, settings.PROPOSAL_PERCENTILE) if len(ring_scores) >= settings.MIN_RING_CELLS_FOR_PERCENTILE else 0.0
    threshold = max(settings.PROPOSAL_MIN_SCORE, dyn)

    rows = (await db.execute(select(UserResponseStat.hour, UserResponseStat.accepted, UserResponseStat.declined)
                             .where(UserResponseStat.user_id == user.id))).all()
    p_accept = acceptance_prob([tuple(r) for r in rows], local_now.hour)
    decision = own_score * (0.5 + 0.5 * p_accept)

    exploration = False
    if req.trigger == TriggerMode.scheduled:
        reason = "scheduled_slot"
    elif decision >= threshold:
        reason = "priority"
    elif random.random() < settings.EXPLORATION_EPSILON:
        reason, exploration = "exploration", True
    else:
        return no("low_priority")

    proposal = CollectionProposal(
        tenant_id=user.tenant_id, user_id=user.id, cell_id=cell.id, h3_index=own, trigger=req.trigger,
        status=ProposalStatus.pending, priority_score=own_score, decision_score=decision,
        is_exploration=exploration, features=features, weights_used=weights,
        confidence_before=await confidence_snapshot(db, user.tenant_id, own, now),
        expires_at=now + timedelta(minutes=settings.PROPOSAL_TTL_MINUTES))
    db.add(proposal)
    await db.commit()
    return ProposalDecisionOut(proposed=True, reason=reason, proposal=_proposal_out(proposal))


async def list_open_proposals(db: AsyncSession, user: User) -> list[ProposalOut]:
    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(CollectionProposal).where(
        CollectionProposal.user_id == user.id,
        CollectionProposal.status.in_([ProposalStatus.pending, ProposalStatus.accepted]),
        CollectionProposal.expires_at > now).order_by(CollectionProposal.created_at.desc()))).scalars()
    return [_proposal_out(p) for p in rows]


async def respond(db: AsyncSession, user: User, proposal_id: uuid.UUID, accept: bool) -> ProposalOut:
    p = await db.get(CollectionProposal, proposal_id, with_for_update=True)
    if p is None or p.user_id != user.id:
        raise HTTPException(404, "proposal_not_found")
    now = datetime.now(timezone.utc)
    if p.status != ProposalStatus.pending or p.expires_at <= now:
        raise HTTPException(409, "proposal_not_pending")
    p.status = ProposalStatus.accepted if accept else ProposalStatus.declined
    p.responded_at = now
    await _bump_response(db, p.tenant_id, user.id, to_local(p.created_at).hour, accept)
    await db.commit()
    return _proposal_out(p)


# ------------------------------------------------------------------ tâches de fond

async def expire_proposals(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    now = datetime.now(timezone.utc)
    ignored = (await db.execute(
        update(CollectionProposal).where(
            CollectionProposal.tenant_id == tenant_id, CollectionProposal.status == ProposalStatus.pending,
            CollectionProposal.expires_at <= now)
        .values(status=ProposalStatus.expired, responded_at=now)
        .returning(CollectionProposal.user_id, CollectionProposal.created_at))).all()
    for user_id, created in ignored:                 # une proposition ignorée compte comme un refus
        await _bump_response(db, tenant_id, user_id, to_local(created).hour, accepted=False)
    grace = timedelta(minutes=settings.PROPOSAL_COMPLETION_GRACE_MINUTES)
    stale = await db.execute(update(CollectionProposal).where(
        CollectionProposal.tenant_id == tenant_id, CollectionProposal.status == ProposalStatus.accepted,
        CollectionProposal.expires_at + grace <= now).values(status=ProposalStatus.expired))
    return len(ignored) + (stale.rowcount or 0)


async def adapt_tenant_weights(db: AsyncSession, tenant_id: uuid.UUID) -> dict | None:
    """Apprentissage en ligne : récompense = qualité de la mesure x (1 - confiance de la cellule avant la mesure)."""
    rows = (await db.execute(
        select(CollectionProposal.id, CollectionProposal.features, CollectionProposal.confidence_before,
               MeasurementQuality.status)
        .join(Measurement, Measurement.proposal_id == CollectionProposal.id)
        .join(MeasurementQuality, MeasurementQuality.measurement_id == Measurement.id)
        .where(CollectionProposal.tenant_id == tenant_id, CollectionProposal.status == ProposalStatus.completed,
               CollectionProposal.learned_at.is_(None))
        .limit(settings.ADAPT_BATCH))).all()
    if len(rows) < settings.ADAPT_MIN_SAMPLES:
        return None
    samples = [({k: float(f[k]) for k in FEATURES}, _REWARD[status] * (1.0 - conf)) for _, f, conf, status in rows]
    current = await get_weights(db, tenant_id)
    new = eg_update(current, samples, base_weights())
    now = datetime.now(timezone.utc)
    state = await db.get(TenantAlgorithmState, tenant_id)
    if state is None:
        db.add(TenantAlgorithmState(tenant_id=tenant_id, weights=new, samples_used=len(rows), updated_at=now))
    else:
        state.weights, state.samples_used, state.updated_at = new, state.samples_used + len(rows), now
    await db.execute(update(CollectionProposal).where(CollectionProposal.id.in_([r[0] for r in rows]))
                     .values(learned_at=now))
    return new


async def reset_weights(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    state = await db.get(TenantAlgorithmState, tenant_id)
    now = datetime.now(timezone.utc)
    if state is None:
        db.add(TenantAlgorithmState(tenant_id=tenant_id, weights=base_weights(), samples_used=0, updated_at=now))
    else:
        state.weights, state.samples_used, state.updated_at = base_weights(), 0, now