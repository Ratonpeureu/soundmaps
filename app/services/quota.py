import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.enums import DayPart, MissionStatus, QualityStatus, Role
from app.models.geo import GeographicCell, NoiseAggregation
from app.models.measurement import Measurement
from app.models.quota import Mission, QuotaDay, UserQuota
from app.models.tenancy import User
from app.services.acoustics import clamp01
from app.services.collection import compute_features, load_cell_stats
from app.services.geo import TZ, cell_for, day_part_of, ring, to_local

HOUR_WEIGHTS = [0.25] * 6 + [1.0] * 16 + [0.25] * 2      # nuit 0,25 ; jour 1,0 (somme = 18)
HOUR_WEIGHT_SUM = sum(HOUR_WEIGHTS)
_USABLE = [QualityStatus.validated, QualityStatus.indicative]
_REWARD = {QualityStatus.validated: 1.0, QualityStatus.indicative: 0.5, QualityStatus.rejected: 0.0}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(a))


# ------------------------------------------------------------------ contexte global (besoins du territoire)

async def global_context(db: AsyncSession, redis: Redis, tenant_id: uuid.UUID, now: datetime) -> dict:
    key = f"t:{tenant_id}:quota:ctx"
    hit = await redis.get(key)
    if hit:
        return json.loads(hit)
    since = (now.astimezone(TZ) - timedelta(days=settings.COVERAGE_WINDOW_DAYS)).date()
    target = settings.TARGET_MEASUREMENTS_PER_CELL
    part_target = max(1.0, target / len(DayPart))
    total_cells = (await db.execute(select(func.count()).select_from(GeographicCell).where(
        GeographicCell.tenant_id == tenant_id))).scalar_one()
    rows = (await db.execute(
        select(NoiseAggregation.cell_id, NoiseAggregation.day_part, func.sum(NoiseAggregation.measurement_count))
        .where(NoiseAggregation.tenant_id == tenant_id, NoiseAggregation.bucket_date >= since,
               NoiseAggregation.quality_status.in_(_USABLE))
        .group_by(NoiseAggregation.cell_id, NoiseAggregation.day_part))).all()
    per_cell: dict = {}
    cover = {p.value: 0.0 for p in DayPart}
    for cell_id, part, n in rows:
        per_cell[cell_id] = per_cell.get(cell_id, 0) + int(n)
        cover[part.value] += min(1.0, int(n) / part_target)
    deficit = (sum(max(0, target - n) for n in per_cell.values())
               + max(0, total_cells - len(per_cell)) * target)
    need_by_part = {p: (clamp01(1 - c / total_cells) if total_cells else 0.5) for p, c in cover.items()}
    need_global = sum(need_by_part.values()) / len(need_by_part)
    supply = (await db.execute(select(func.count(func.distinct(Measurement.user_id))).where(
        Measurement.tenant_id == tenant_id, Measurement.user_id.is_not(None),
        Measurement.measured_at >= now - timedelta(days=7)))).scalar_one()
    demand = deficit / (settings.COVERAGE_WINDOW_DAYS * max(1, supply))
    daily = min(max(demand, settings.QUOTA_MIN_DAILY), settings.QUOTA_MAX_DAILY)
    if total_cells < settings.QUOTA_COLD_START_CELLS:            # démarrage : on ne bride pas la collecte
        daily = max(daily, settings.QUOTA_COLD_START_DAILY)
    ctx = {"need_by_part": need_by_part, "need_global": need_global, "supply": int(supply),
           "global_daily": daily, "total_cells": int(total_cells), "deficit": float(deficit)}
    await redis.set(key, json.dumps(ctx), ex=settings.QUOTA_CONTEXT_TTL_SECONDS)
    return ctx


# ------------------------------------------------------------------ indicateurs par utilisateur

async def metrics_bulk(db: AsyncSession, tenant_id: uuid.UUID, user_ids: list[uuid.UUID],
                       now: datetime) -> dict[uuid.UUID, dict]:
    """R = fiabilité (30 j, bayésienne), U = utilisation du solde (7 j), C = réussite des missions (60 j)."""
    out = {u: {"R": settings.QUOTA_RELIABILITY_PRIOR, "U": 0.7, "C": 0.5} for u in user_ids}
    if not user_ids:
        return out
    s, p0 = settings.QUOTA_RELIABILITY_STRENGTH, settings.QUOTA_RELIABILITY_PRIOR
    rows = (await db.execute(
        select(Measurement.user_id, Measurement.quality_status, func.count()).where(
            Measurement.tenant_id == tenant_id, Measurement.user_id.in_(user_ids),
            Measurement.measured_at >= now - timedelta(days=30),
            Measurement.quality_status != QualityStatus.pending)
        .group_by(Measurement.user_id, Measurement.quality_status))).all()
    acc: dict = {}
    for uid, st, n in rows:
        a = acc.setdefault(uid, [0.0, 0])
        a[0] += _REWARD[st] * n
        a[1] += n
    for uid, (rw, n) in acc.items():
        out[uid]["R"] = (rw + s * p0) / (n + s)

    today = to_local(now).date()
    rows = (await db.execute(
        select(QuotaDay.user_id, func.sum(QuotaDay.credited), func.sum(QuotaDay.used)).where(
            QuotaDay.tenant_id == tenant_id, QuotaDay.user_id.in_(user_ids),
            QuotaDay.local_date >= today - timedelta(days=7)).group_by(QuotaDay.user_id))).all()
    for uid, cr, us in rows:
        cr, us = float(cr or 0), float(us or 0)
        if cr >= 1.0:
            out[uid]["U"] = min(1.5, us / cr)

    rows = (await db.execute(
        select(Mission.user_id, Mission.status, func.count()).where(
            Mission.tenant_id == tenant_id, Mission.user_id.in_(user_ids),
            Mission.status.in_([MissionStatus.completed, MissionStatus.failed]),
            Mission.created_at >= now - timedelta(days=60)).group_by(Mission.user_id, Mission.status))).all()
    done: dict = {}
    for uid, st, n in rows:
        d = done.setdefault(uid, [0, 0])
        d[0 if st == MissionStatus.completed else 1] += n
    for uid, (ok, ko) in done.items():
        out[uid]["C"] = (ok + 1.5) / (ok + ko + 3)
    return out


# ------------------------------------------------------------------ formules

def allowance_target(ctx: dict, r: float, u: float, c: float) -> float:
    v = ctx["global_daily"] * (0.5 + r) * (0.6 + 0.6 * min(1.0, u)) * (0.9 + 0.3 * c)
    return min(max(v, settings.QUOTA_MIN_DAILY), settings.QUOTA_MAX_DAILY)


def retention_per_day(need: float, util: float) -> float:
    base = settings.QUOTA_RETENTION_MIN + (settings.QUOTA_RETENTION_MAX - settings.QUOTA_RETENTION_MIN) * need
    idle = 0.6 + 0.4 * min(1.0, util / 0.5)
    return min(max(base * idle, 0.2), 0.98)


def step(balance: float, target: float, need: float, util: float, hour: int, dt_h: float, cap: float):
    keep = retention_per_day(need, util) ** (dt_h / 24.0)
    decayed = balance * (1 - keep) if balance > 0 else 0.0     # une dette ne s'efface pas toute seule
    base = balance - decayed
    credit = target * HOUR_WEIGHTS[hour] / HOUR_WEIGHT_SUM * dt_h * (0.7 + 0.6 * need)
    new = min(base + credit, cap)
    return new, max(0.0, new - base), decayed


def project(balance: float, start: datetime, hours: float, target: float, util: float, ctx: dict, cap: float):
    n = max(1, math.ceil(hours))
    sdt = hours / n
    bal, cr, dc = balance, 0.0, 0.0
    for i in range(n):
        lt = to_local(start + timedelta(hours=sdt * (i + 0.5)))
        need = ctx["need_by_part"][day_part_of(lt).value]
        bal, c, d = step(bal, target, need, util, lt.hour, sdt, cap)
        cr, dc = cr + c, dc + d
    return bal, cr, dc


def cooldown_minutes(need: float) -> float:
    lo, hi = settings.QUOTA_COOLDOWN_MIN_MINUTES, settings.QUOTA_COOLDOWN_MAX_MINUTES
    return lo + (hi - lo) * (1 - need) ** 1.5


def cost_of(need: float) -> float:
    return settings.QUOTA_COST_MIN + (settings.QUOTA_COST_MAX - settings.QUOTA_COST_MIN) * (1 - need)


# ------------------------------------------------------------------ persistance

async def _bump_days(db: AsyncSession, rows: list[dict]) -> None:
    if not rows:
        return
    stmt = pg_insert(QuotaDay).values(rows)
    stmt = stmt.on_conflict_do_update(constraint="uq_quota_days_user_date", set_={
        "credited": QuotaDay.credited + stmt.excluded.credited,
        "used": QuotaDay.used + stmt.excluded.used,
        "decayed": QuotaDay.decayed + stmt.excluded.decayed,
        "measurements": QuotaDay.measurements + stmt.excluded.measurements,
        "target": func.coalesce(func.nullif(stmt.excluded.target, 0), QuotaDay.target)})
    await db.execute(stmt)


def _day_row(q: UserQuota, now: datetime, *, credited=0.0, used=0.0, decayed=0.0, measurements=0, target=0.0) -> dict:
    return {"id": uuid.uuid4(), "tenant_id": q.tenant_id, "user_id": q.user_id,
            "local_date": to_local(now).date(), "credited": credited, "used": used, "decayed": decayed,
            "measurements": measurements, "target": target}


def _apply(q: UserQuota, ctx: dict, m: dict, now: datetime, paused: bool) -> dict | None:
    local = to_local(now)
    target = allowance_target(ctx, m["R"], m["U"], m["C"])
    cap = max(settings.QUOTA_MIN_CAP, target * settings.QUOTA_CAP_DAYS)
    need_now = ctx["need_by_part"][day_part_of(local).value]
    credited = decayed = 0.0
    if not paused:
        dt_h = min(24.0, max(0.0, (now - q.updated_at).total_seconds() / 3600))
        if dt_h > 0:
            q.balance, credited, decayed = project(q.balance, q.updated_at, dt_h, target, m["U"], ctx, cap)
    q.daily_target, q.cap = round(target, 2), round(cap, 2)
    q.rate_per_hour = target * HOUR_WEIGHTS[local.hour] / HOUR_WEIGHT_SUM * (0.7 + 0.6 * need_now)
    q.reliability, q.utilization, q.mission_rate = m["R"], m["U"], m["C"]
    q.retention_per_day = retention_per_day(need_now, m["U"])
    q.updated_at = now
    if not (credited or decayed):
        return None
    return _day_row(q, now, credited=credited, decayed=decayed, target=target)


async def ensure_fresh(db: AsyncSession, redis: Redis, user: User, now: datetime, lock: bool = False) -> UserQuota:
    await db.execute(pg_insert(UserQuota).values(
        user_id=user.id, tenant_id=user.tenant_id, balance=settings.QUOTA_INITIAL_BALANCE, updated_at=now,
    ).on_conflict_do_nothing(index_elements=["user_id"]))
    stmt = select(UserQuota).where(UserQuota.user_id == user.id)
    if lock:
        stmt = stmt.with_for_update()
    q = (await db.execute(stmt)).scalar_one()
    if q.daily_target <= 0 or (now - q.updated_at).total_seconds() >= settings.QUOTA_LAZY_REFRESH_SECONDS:
        ctx = await global_context(db, redis, user.tenant_id, now)
        m = (await metrics_bulk(db, user.tenant_id, [user.id], now))[user.id]
        row = _apply(q, ctx, m, now, user.participation_paused)
        if row:
            await _bump_days(db, [row])
    return q


async def grant(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, amount: float, now: datetime) -> None:
    """Bonus (récompense de mission) : peut dépasser le plafond de 50 %."""
    await db.execute(pg_insert(UserQuota).values(
        user_id=user_id, tenant_id=tenant_id, balance=settings.QUOTA_INITIAL_BALANCE, updated_at=now,
    ).on_conflict_do_nothing(index_elements=["user_id"]))
    q = (await db.execute(select(UserQuota).where(UserQuota.user_id == user_id).with_for_update())).scalar_one()
    q.balance = min(q.balance + amount, max(q.cap, settings.QUOTA_MIN_CAP) * 1.5)
    await _bump_days(db, [_day_row(q, now, credited=amount)])


# ------------------------------------------------------------------ décision par prélèvement

@dataclass
class Decision:
    allowed: bool
    reason: str
    cost: float = 0.0
    balance: float = 0.0
    cooldown_minutes: float = 0.0
    retry_at: datetime | None = None
    cell_need: float = 0.0
    free: bool = False


async def _cell_need(db: AsyncSession, redis: Redis, tenant_id: uuid.UUID, h3_index: str, now: datetime) -> float:
    part = day_part_of(to_local(now))
    key = f"t:{tenant_id}:cneed:{h3_index}:{part.value}"
    hit = await redis.get(key)
    if hit is not None:
        return float(hit)
    stats = await load_cell_stats(db, tenant_id, [h3_index], now)
    f = compute_features(stats.get(h3_index), now, part, 0)
    need = clamp01(0.5 * f["coverage_gap"] + 0.3 * f["time_relevance"] + 0.2 * f["freshness"])
    await redis.set(key, f"{need:.4f}", ex=60)
    return need


async def _last_in_ring(db: AsyncSession, user: User, h3_index: str, until: datetime, horizon_min: float) -> dict:
    rows = (await db.execute(
        select(Measurement.h3_index, func.max(Measurement.measured_at)).where(
            Measurement.tenant_id == user.tenant_id, Measurement.user_id == user.id,
            Measurement.h3_index.in_(ring(h3_index, 1)),
            Measurement.measured_at >= until - timedelta(minutes=horizon_min),
            Measurement.measured_at <= until).group_by(Measurement.h3_index))).all()
    return {h: t for h, t in rows}


async def _mission_for(db: AsyncSession, user: User, lat: float, lon: float, now: datetime) -> Mission | None:
    rows = (await db.execute(select(Mission).where(
        Mission.user_id == user.id, Mission.status == MissionStatus.accepted, Mission.deadline_at > now,
        Mission.done_count < Mission.required_count).order_by(Mission.deadline_at))).scalars()
    for m in rows:
        if haversine_m(lat, lon, m.center_lat, m.center_lon) <= m.radius_m:
            return m
    return None


async def evaluate(db: AsyncSession, redis: Redis, user: User, lat: float, lon: float, measured_at: datetime,
                   *, proposal: bool, hard: bool, consume: bool) -> Decision:
    if user.role != Role.contributor:
        return Decision(True, "staff", free=True)
    now = datetime.now(timezone.utc)
    q = await ensure_fresh(db, redis, user, now, lock=consume)
    h3_index = cell_for(lat, lon)
    need = await _cell_need(db, redis, user.tenant_id, h3_index, now)
    cd, cost = cooldown_minutes(need), cost_of(need)
    mission = await _mission_for(db, user, lat, lon, now)
    base = dict(balance=q.balance, cooldown_minutes=cd, cell_need=need)

    if proposal or mission is not None:
        d = Decision(True, "proposal_free" if proposal else "mission_free", 0.0, free=True, **base)
    else:
        wait = 0.0
        for h, t in (await _last_in_ring(db, user, h3_index, measured_at, cd)).items():
            wait = max(wait, cd * (1.0 if h == h3_index else 0.5) - (measured_at - t).total_seconds() / 60)
        if wait > 0 and hard:
            return Decision(False, "cell_cooldown", cost, retry_at=measured_at + timedelta(minutes=wait), **base)
        if q.balance + 1e-9 < cost and hard:
            hours = min(48.0, (cost - q.balance) / max(q.rate_per_hour, 0.05))
            return Decision(False, "quota_exhausted", cost, retry_at=now + timedelta(hours=hours), **base)
        d = Decision(True, "ok", cost, **base)

    if consume:
        if d.cost > 0:
            q.balance = max(-q.cap, q.balance - d.cost)       # une synchro hors-ligne peut passer en négatif
            d.balance = q.balance
        await _bump_days(db, [_day_row(q, now, used=d.cost, measurements=1)])
        if mission is not None:
            mission.done_count += 1
    return d


async def state(db: AsyncSession, redis: Redis, user: User) -> dict:
    now = datetime.now(timezone.utc)
    q = await ensure_fresh(db, redis, user, now)
    ctx = await global_context(db, redis, user.tenant_id, now)
    need_now = ctx["need_by_part"][day_part_of(to_local(now)).value]
    day = (await db.execute(select(QuotaDay).where(
        QuotaDay.user_id == user.id, QuotaDay.local_date == to_local(now).date()))).scalar_one_or_none()
    proj, _, _ = project(q.balance, now, 24, q.daily_target, q.utilization, ctx, q.cap)
    return {"balance": round(q.balance, 2), "measurements_left": max(0, int(q.balance)),
            "daily_target": q.daily_target, "rate_per_hour": round(q.rate_per_hour, 3), "cap": q.cap,
            "reliability": round(q.reliability, 3), "utilization": round(q.utilization, 3),
            "mission_rate": round(q.mission_rate, 3), "retention_per_day": round(q.retention_per_day, 3),
            "need_now": round(need_now, 3), "projected_24h": round(proj, 2),
            "today_credited": round(day.credited, 2) if day else 0.0,
            "today_used": round(day.used, 2) if day else 0.0,
            "today_decayed": round(day.decayed, 2) if day else 0.0}


# ------------------------------------------------------------------ tâches de fond

async def rebalance_tenant(db: AsyncSession, redis: Redis, tenant_id: uuid.UUID) -> int:
    now = datetime.now(timezone.utc)
    ctx = await global_context(db, redis, tenant_id, now)
    done = 0
    while True:
        rows = (await db.execute(
            select(UserQuota, User.participation_paused).join(User, User.id == UserQuota.user_id)
            .where(UserQuota.tenant_id == tenant_id, User.is_active.is_(True),
                   UserQuota.updated_at < now - timedelta(minutes=10))
            .order_by(UserQuota.updated_at).limit(settings.QUOTA_BATCH)
            .with_for_update(of=UserQuota, skip_locked=True))).all()
        if not rows:
            break
        mets = await metrics_bulk(db, tenant_id, [r[0].user_id for r in rows], now)
        days = [d for q, paused in rows if (d := _apply(q, ctx, mets[q.user_id], now, paused))]
        await _bump_days(db, days)
        await db.flush()
        done += len(rows)
        if len(rows) < settings.QUOTA_BATCH:
            break
    return done


async def cleanup_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    from sqlalchemy import delete
    cutoff = to_local(datetime.now(timezone.utc)).date() - timedelta(days=settings.QUOTA_DAYS_RETENTION)
    res = await db.execute(delete(QuotaDay).where(QuotaDay.tenant_id == tenant_id, QuotaDay.local_date < cutoff))
    return res.rowcount or 0