import json
import math
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import h3
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.road_settings import road_settings as rs
from app.models.enums import MissionStatus, QualityStatus
from app.models.geo import GeographicCell, NoiseAggregation
from app.models.measurement import Measurement
from app.models.quota import Mission
from app.models.tenancy import User, UserConsent
from app.services import quota as quota_service
from app.services import road_missions
from app.services.acoustics import clamp01
from app.services.geo import TZ

_ACTIVE = (MissionStatus.offered, MissionStatus.accepted)
_USABLE = [QualityStatus.validated, QualityStatus.indicative]


@dataclass
class Zone:
    zone_id: str
    lat: float
    lon: float
    deficit: float
    need: float
    radius_m: float


@dataclass
class Profile:
    user_id: uuid.UUID
    anchor: tuple[float, float] | None
    n14: int
    R: float
    C: float
    acc: float
    load: int
    score: float


# ------------------------------------------------------------------ zones en manque

async def zone_deficits(db: AsyncSession, redis: Redis, tenant_id: uuid.UUID, now: datetime) -> list[Zone]:
    key = f"t:{tenant_id}:mission:zones"
    hit = await redis.get(key)
    if hit:
        return [Zone(**z) for z in json.loads(hit)]
    since = (now.astimezone(TZ) - timedelta(days=settings.COVERAGE_WINDOW_DAYS)).date()
    n = func.coalesce(func.sum(NoiseAggregation.measurement_count), 0)
    rows = (await db.execute(
        select(GeographicCell.h3_index, func.ST_Y(GeographicCell.center), func.ST_X(GeographicCell.center), n)
        .outerjoin(NoiseAggregation, (NoiseAggregation.cell_id == GeographicCell.id)
                   & (NoiseAggregation.bucket_date >= since) & NoiseAggregation.quality_status.in_(_USABLE))
        .where(GeographicCell.tenant_id == tenant_id).group_by(GeographicCell.id).limit(50000))).all()
    target = settings.TARGET_MEASUREMENTS_PER_CELL
    acc: dict = {}
    for h, lat, lon, cnt in rows:
        zid = h3.cell_to_parent(h, settings.MISSION_ZONE_H3_RES)
        a = acc.setdefault(zid, {"cells": 0, "deficit": 0.0, "sl": 0.0, "so": 0.0, "pts": []})
        a["cells"] += 1
        d = max(0, target - int(cnt))
        if d > 0:
            a["deficit"] += d
            a["sl"] += lat * d
            a["so"] += lon * d
            a["pts"].append((lat, lon))
    zones: list[Zone] = []
    for zid, a in acc.items():
        if a["deficit"] < settings.MISSION_MIN_ZONE_DEFICIT:
            continue
        need = clamp01(a["deficit"] / (a["cells"] * target))
        if need < 0.25:
            continue
        lat, lon = a["sl"] / a["deficit"], a["so"] / a["deficit"]
        far = max(quota_service.haversine_m(lat, lon, p[0], p[1]) for p in a["pts"])
        zones.append(Zone(zid, lat, lon, a["deficit"], need, min(2500.0, max(400.0, far + 250.0))))
    zones.sort(key=lambda z: z.deficit * z.need, reverse=True)
    zones = zones[:settings.MISSION_MAX_ZONES]
    await redis.set(key, json.dumps([asdict(z) for z in zones]), ex=300)
    return zones


async def _committed(db: AsyncSession, tenant_id: uuid.UUID, now: datetime):
    """Engagements sur les missions de zone uniquement (les missions routes ont leur propre logique)."""
    committed: dict[str, int] = defaultdict(int)
    pairs: set = set()
    rows = (await db.execute(select(Mission.zone_h3, Mission.user_id, Mission.status, Mission.required_count,
                                    Mission.done_count, Mission.created_at).where(
        Mission.tenant_id == tenant_id, Mission.kind == "zone",
        or_(Mission.status.in_(_ACTIVE),
            Mission.created_at >= now - timedelta(days=settings.MISSION_DECLINE_COOLDOWN_DAYS))))).all()
    for zone, uid, st, req, done, _ in rows:
        pairs.add((uid, zone))
        if st in _ACTIVE:
            committed[zone] += max(0, req - done)
    return committed, pairs


# ------------------------------------------------------------------ profils d'utilisateurs

async def profiles(db: AsyncSession, tenant_id: uuid.UUID, now: datetime,
                   only: list[uuid.UUID] | None = None) -> dict[uuid.UUID, Profile]:
    q = select(User.id).where(User.tenant_id == tenant_id, User.is_active.is_(True),
                              User.participation_paused.is_(False))
    if only:
        q = q.where(User.id.in_(only))
    ids = list((await db.execute(q.limit(settings.QUOTA_BATCH * 5))).scalars())
    if not ids:
        return {}
    rows = (await db.execute(
        select(UserConsent.user_id, UserConsent.consent_type, UserConsent.granted)
        .where(UserConsent.tenant_id == tenant_id, UserConsent.user_id.in_(ids))
        .order_by(UserConsent.user_id, UserConsent.consent_type, UserConsent.created_at.desc())
        .distinct(UserConsent.user_id, UserConsent.consent_type))).all()
    granted: dict = defaultdict(int)
    for uid, _, g in rows:
        granted[uid] += 1 if g else 0
    ids = [u for u in ids if granted[u] >= 2]
    if not ids:
        return {}

    last = {uid: (lat, lon) for uid, lat, lon in (await db.execute(
        select(Measurement.user_id, Measurement.latitude, Measurement.longitude)
        .where(Measurement.tenant_id == tenant_id, Measurement.user_id.in_(ids),
               Measurement.measured_at >= now - timedelta(days=30),
               Measurement.quality_status != QualityStatus.rejected)
        .distinct(Measurement.user_id).order_by(Measurement.user_id, Measurement.measured_at.desc()))).all()}
    n14 = dict((await db.execute(
        select(Measurement.user_id, func.count()).where(
            Measurement.tenant_id == tenant_id, Measurement.user_id.in_(ids),
            Measurement.measured_at >= now - timedelta(days=14),
            Measurement.quality_status != QualityStatus.rejected).group_by(Measurement.user_id))).all())
    mets = await quota_service.metrics_bulk(db, tenant_id, ids, now)

    pos: dict = defaultdict(int)
    neg: dict = defaultdict(int)
    load: dict = defaultdict(int)
    for uid, st, n in (await db.execute(
            select(Mission.user_id, Mission.status, func.count()).where(
                Mission.tenant_id == tenant_id, Mission.user_id.in_(ids),
                Mission.created_at >= now - timedelta(days=60)).group_by(Mission.user_id, Mission.status))).all():
        if st in (MissionStatus.accepted, MissionStatus.completed, MissionStatus.failed):
            pos[uid] += n
        elif st in (MissionStatus.declined, MissionStatus.expired):
            neg[uid] += n
        if st in _ACTIVE:
            load[uid] += n

    out = {}
    for uid in ids:
        acc = (pos[uid] + 1.5) / (pos[uid] + neg[uid] + 3)
        act = min(1.0, int(n14.get(uid, 0)) / settings.MISSION_ACTIVITY_TARGET)
        score = (settings.MS_W_ACTIVITY * act + settings.MS_W_RELIABILITY * mets[uid]["R"]
                 + settings.MS_W_COMPLETION * mets[uid]["C"] + settings.MS_W_ACCEPTANCE * acc)
        out[uid] = Profile(uid, last.get(uid), int(n14.get(uid, 0)), mets[uid]["R"], mets[uid]["C"], acc,
                           load[uid], score)
    return out


# ------------------------------------------------------------------ affectation (missions de zone)

async def _assign(db: AsyncSession, tenant_id: uuid.UUID, zones: list[Zone], profs: dict[uuid.UUID, Profile],
                  now: datetime, max_new: int) -> list[Mission]:
    committed, pairs = await _committed(db, tenant_id, now)
    created: list[Mission] = []
    new_count: dict = defaultdict(int)
    mx = settings.MISSION_MAX_COUNT
    mn = settings.MISSION_MIN_COUNT
    for z in zones:
        remaining = z.deficit - committed.get(z.zone_id, 0)
        if remaining < mn:
            continue
        cands = []
        for uid, p in profs.items():
            if p.anchor is None or p.score < settings.MISSION_MIN_SCORE:
                continue
            used = p.load + new_count[uid]
            if used >= settings.MISSION_MAX_PENDING or new_count[uid] >= max_new or (uid, z.zone_id) in pairs:
                continue
            d_km = quota_service.haversine_m(p.anchor[0], p.anchor[1], z.lat, z.lon) / 1000
            if d_km > settings.MISSION_MAX_RADIUS_KM:
                continue
            fit = (p.score * (1 - d_km / settings.MISSION_MAX_RADIUS_KM) ** 0.7
                   * max(0.1, 1 - used / settings.MISSION_MAX_PENDING))
            cands.append((fit, str(uid), uid, d_km))
        cands.sort(key=lambda c: c[0], reverse=True)
        for fit, _, uid, d_km in cands[:settings.MISSION_USERS_PER_ZONE]:
            if remaining < mn:
                break
            p = profs[uid]
            size = mn + (mx - mn) * p.score * (0.6 + 0.4 * z.need)
            size = int(max(mn, min(round(size), math.ceil(remaining), mx)))
            db.add(m := Mission(
                tenant_id=tenant_id, user_id=uid, kind="zone", status=MissionStatus.offered, zone_h3=z.zone_id,
                center_lat=z.lat, center_lon=z.lon, radius_m=z.radius_m, distance_km=round(d_km, 1),
                required_count=size, done_count=0, confirmed_count=0,
                reward_credits=round(size * settings.MISSION_REWARD_PER_MEASUREMENT * (0.75 + 0.5 * z.need), 1),
                priority=round(100 * clamp01(0.6 * z.need + 0.4 * fit), 1),
                features={"zone_deficit": z.deficit, "zone_need": round(z.need, 3), "score": round(p.score, 3),
                          "fit": round(fit, 3)},
                offer_expires_at=now + timedelta(hours=settings.MISSION_OFFER_HOURS)))
            created.append(m)
            remaining -= size
            committed[z.zone_id] += size
            new_count[uid] += 1
            pairs.add((uid, z.zone_id))
    await db.flush()
    return created


async def generate_for_tenant(db: AsyncSession, redis: Redis, tenant_id: uuid.UUID) -> int:
    now = datetime.now(timezone.utc)
    zones = await zone_deficits(db, redis, tenant_id, now)
    profs = await profiles(db, tenant_id, now)
    if not profs:
        return 0
    created: list[Mission] = []
    if zones:
        created = await _assign(db, tenant_id, zones, profs, now, settings.MISSION_MAX_NEW_PER_USER_PER_RUN)
        for m in created:
            profs[m.user_id].load += 1
    if rs.ROAD_MISSIONS_ENABLED:
        created += await road_missions.assign(db, tenant_id, profs, now, rs.ROAD_MAX_NEW_PER_USER_PER_RUN)
    return len(created)


async def sync_for_user(db: AsyncSession, redis: Redis, user: User, lat: float, lon: float) -> None:
    """Génération à la demande depuis la position courante (non stockée), au plus 1 fois par heure."""
    if not await redis.set(f"t:{user.tenant_id}:msync:{user.id}", "1", nx=True,
                           ex=settings.MISSION_SYNC_MIN_MINUTES * 60):
        return
    now = datetime.now(timezone.utc)
    zones = await zone_deficits(db, redis, user.tenant_id, now)
    profs = await profiles(db, user.tenant_id, now, only=[user.id])
    p = profs.get(user.id)
    if p is None:
        return
    p.anchor = (lat, lon)
    if zones:
        created = await _assign(db, user.tenant_id, zones, {user.id: p}, now, max_new=2)
        p.load += len(created)
    if rs.ROAD_MISSIONS_ENABLED:
        await road_missions.assign(db, user.tenant_id, {user.id: p}, now, rs.ROAD_SYNC_MAX_NEW)


async def ranking(db: AsyncSession, tenant_id: uuid.UUID, limit: int = 50) -> list[dict]:
    now = datetime.now(timezone.utc)
    profs = await profiles(db, tenant_id, now)
    pseudo = dict((await db.execute(select(User.id, User.pseudonym).where(User.id.in_(list(profs))))).all()) if profs else {}
    ordered = sorted(profs.values(), key=lambda p: p.score, reverse=True)[:limit]
    return [{"pseudonym": pseudo.get(p.user_id), "score": round(p.score, 3), "activity_14d": p.n14,
             "reliability": round(p.R, 3), "completion": round(p.C, 3), "acceptance": round(p.acc, 3),
             "load": p.load} for p in ordered]


# ------------------------------------------------------------------ cycle de vie

async def expire(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    now = datetime.now(timezone.utc)
    res = await db.execute(update(Mission).where(
        Mission.tenant_id == tenant_id, Mission.status == MissionStatus.offered,
        Mission.offer_expires_at <= now).values(status=MissionStatus.expired, finished_at=now))
    return res.rowcount or 0


async def reconcile(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID | None = None) -> int:
    """Recalcule la progression depuis les mesures réelles ; clôture (réussite + récompense, ou échec).
    Mission route : mesures dans la tolérance du point cible, dans la plage horaire et la fenêtre visées."""
    now = datetime.now(timezone.utc)
    conds = [Mission.tenant_id == tenant_id, Mission.status == MissionStatus.accepted]
    if user_id:
        conds.append(Mission.user_id == user_id)
    missions = (await db.execute(select(Mission).where(*conds).with_for_update(skip_locked=True).limit(1000))).scalars().all()
    for m in missions:
        dist = func.ST_DistanceSphere(Measurement.geom,
                                      func.ST_SetSRID(func.ST_MakePoint(m.center_lon, m.center_lat), 4326))
        since = m.accepted_at
        if m.window_start_at is not None and m.window_start_at > since:
            since = m.window_start_at
        mconds = [Measurement.tenant_id == tenant_id, Measurement.user_id == m.user_id,
                  Measurement.measured_at >= since, Measurement.quality_status != QualityStatus.rejected,
                  dist <= m.radius_m]
        if m.kind == "road":
            if m.target_day_part is not None:
                mconds.append(Measurement.day_part == m.target_day_part)
            if m.window_end_at is not None:
                mconds.append(Measurement.measured_at <= m.window_end_at)
        total, ok = (await db.execute(select(
            func.count(), func.coalesce(func.sum(case((Measurement.quality_status.in_(_USABLE), 1), else_=0)), 0)
        ).where(*mconds))).one()
        m.done_count, m.confirmed_count = int(total), int(ok)
        if m.confirmed_count >= m.required_count:
            m.status, m.finished_at = MissionStatus.completed, now
            await quota_service.grant(db, tenant_id, m.user_id, m.reward_credits, now)
        elif m.deadline_at is not None and m.deadline_at <= now:
            m.status, m.finished_at = MissionStatus.failed, now
    return len(missions)


async def list_for_user(db: AsyncSession, user: User) -> list[Mission]:
    now = datetime.now(timezone.utc)
    return list((await db.execute(
        select(Mission).where(Mission.user_id == user.id, or_(
            Mission.status.in_(_ACTIVE), Mission.finished_at >= now - timedelta(days=7)))
        .order_by(Mission.created_at.desc()).limit(50))).scalars())


async def respond(db: AsyncSession, user: User, mission_id: uuid.UUID, accept: bool) -> Mission:
    m = await db.get(Mission, mission_id, with_for_update=True)
    if m is None or m.user_id != user.id:
        raise HTTPException(404, "mission_not_found")
    now = datetime.now(timezone.utc)
    if m.status != MissionStatus.offered or m.offer_expires_at <= now:
        raise HTTPException(409, "mission_not_offered")
    if accept:
        active = (await db.execute(select(func.count()).select_from(Mission).where(
            Mission.user_id == user.id, Mission.status == MissionStatus.accepted))).scalar_one()
        if active >= settings.MISSION_MAX_ACTIVE:
            raise HTTPException(409, "mission_limit_reached")
        m.status, m.accepted_at = MissionStatus.accepted, now
        m.deadline_at = now + timedelta(hours=settings.MISSION_COMPLETION_HOURS)
        if m.window_end_at is not None:                    # mission route : jamais au-delà de la plage visée
            m.deadline_at = min(m.deadline_at, m.window_end_at)
    else:
        m.status, m.finished_at = MissionStatus.declined, now
    await db.commit()
    return m