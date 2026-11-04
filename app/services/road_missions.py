import math
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

import h3
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.road_settings import road_settings as rs
from app.models.enums import DayPart, MissionStatus
from app.models.quota import Mission
from app.services.acoustics import clamp01
from app.services.geo import TZ, to_local
from app.services.quota import haversine_m

_ACTIVE = (MissionStatus.offered, MissionStatus.accepted)

_CAND = text("""
SELECT s.id, s.highway, s.name, s.ref, s.road_rank, s.class_weight, s.length_m, s.mid_lat, s.mid_lon
FROM road_segments s
WHERE s.active AND s.road_rank <= :max_rank
  AND ST_DWithin(s.geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), :deg)
ORDER BY s.class_weight DESC, s.length_m DESC
LIMIT :lim""")

_COUNTS = text("""
SELECT m.road_segment_id, m.day_part, count(*) AS n
FROM measurements m JOIN road_segments s ON s.id = m.road_segment_id
WHERE m.tenant_id = :tid AND m.measured_at >= :since AND m.quality_status IN ('validated', 'indicative')
  AND s.active AND s.road_rank <= :max_rank
  AND ST_DWithin(s.geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), :deg)
GROUP BY m.road_segment_id, m.day_part""")


@dataclass
class Candidate:
    id: uuid.UUID
    highway: str
    name: str | None
    ref: str | None
    rank: int
    weight: float
    length_m: float
    lat: float
    lon: float
    counts: dict[str, int] = field(default_factory=dict)


# ------------------------------------------------------------------ créneaux horaires

def _bounds() -> dict[str, tuple[int, int]]:
    return {"morning": (settings.DAYPART_MORNING_START, settings.DAYPART_AFTERNOON_START),
            "afternoon": (settings.DAYPART_AFTERNOON_START, settings.DAYPART_EVENING_START),
            "evening": (settings.DAYPART_EVENING_START, settings.DAYPART_NIGHT_START),
            "night": (settings.DAYPART_NIGHT_START, settings.DAYPART_MORNING_START)}


def next_window(now: datetime, part: str) -> tuple[datetime, datetime]:
    """Créneau en cours ou prochain créneau de la plage horaire (UTC)."""
    local = to_local(now)
    s_h, e_h = _bounds()[part]
    for off in (-1, 0, 1):
        d0 = local.date() + timedelta(days=off)
        start = datetime.combine(d0, time(s_h), TZ)
        end = datetime.combine(d0 + timedelta(days=1 if e_h <= s_h else 0), time(e_h), TZ)
        if end > local:
            return max(start, local).astimezone(timezone.utc), end.astimezone(timezone.utc)
    raise RuntimeError("window_not_found")


def eligible_windows(now: datetime) -> dict[str, tuple[datetime, datetime]]:
    out = {}
    for part in _bounds():
        start, end = next_window(now, part)
        if start - now > timedelta(hours=rs.ROAD_LOOKAHEAD_HOURS):
            continue
        if end - max(start, now) < timedelta(minutes=rs.ROAD_MIN_WINDOW_MINUTES):
            continue
        out[part] = (start, end)
    return out


# ------------------------------------------------------------------ besoins

def slots(c: Candidate, wins: dict) -> list[tuple[float, str]]:
    """Besoin de mesure (0-1) du tronçon pour chaque plage encore atteignable, du plus fort au plus faible."""
    pw = rs.part_weights
    out = []
    for part in wins:
        gap = clamp01(1.0 - c.counts.get(part, 0) / max(1, rs.ROAD_TARGET_PER_PART))
        need = c.weight * pw.get(part, 1.0) * gap
        if need >= rs.ROAD_MIN_NEED:
            out.append((need, part))
    out.sort(reverse=True)
    return out


def _deg(radius_m: float) -> float:
    return radius_m / 111320.0 * 1.6


async def load_candidates(db: AsyncSession, tenant_id: uuid.UUID, lat: float, lon: float, radius_km: float,
                          now: datetime) -> list[Candidate]:
    p = {"max_rank": rs.ROAD_MISSION_MAX_RANK, "lat": lat, "lon": lon, "deg": _deg(radius_km * 1000)}
    rows = (await db.execute(_CAND, {**p, "lim": rs.ROAD_CANDIDATE_LIMIT})).all()
    cands = {r[0]: Candidate(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]) for r in rows}
    if not cands:
        return []
    since = now - timedelta(days=rs.ROAD_COVERAGE_WINDOW_DAYS)
    for sid, part, n in (await db.execute(_COUNTS, {**p, "tid": tenant_id, "since": since})).all():
        c = cands.get(sid)
        if c is not None:
            c.counts[str(part)] = int(n)
    return list(cands.values())


# ------------------------------------------------------------------ affectation

def _make(tenant_id, p, c: Candidate, part: str, win, d_km: float, need: float, fit: float) -> Mission:
    start, end = win
    reward = round(rs.ROAD_MISSION_COUNT * rs.ROAD_REWARD_PER_MEASUREMENT * (0.75 + 0.5 * need)
                   * (0.8 + 0.4 * c.weight), 1)
    return Mission(
        tenant_id=tenant_id, user_id=p.user_id, kind="road", status=MissionStatus.offered,
        zone_h3=h3.latlng_to_cell(c.lat, c.lon, settings.MISSION_ZONE_H3_RES),
        center_lat=c.lat, center_lon=c.lon, radius_m=rs.ROAD_TARGET_RADIUS_M, distance_km=round(d_km, 1),
        required_count=rs.ROAD_MISSION_COUNT, done_count=0, confirmed_count=0, reward_credits=reward,
        priority=round(100 * clamp01(0.65 * need + 0.35 * fit), 1),
        features={"segment_need": round(need, 3), "fit": round(fit, 3), "score": round(p.score, 3),
                  "length_m": c.length_m},
        road_segment_id=c.id, road_name=c.name or c.ref, road_class=c.highway, target_day_part=DayPart(part),
        window_start_at=start, window_end_at=end, offer_expires_at=end)


async def assign(db: AsyncSession, tenant_id: uuid.UUID, profs: dict, now: datetime, max_new: int) -> list[Mission]:
    """Propose à chaque utilisateur (position connue) des points précis sur des routes qui manquent de mesures
    pour une plage horaire atteignable. Les profils sont typés comme missions.Profile (user_id, anchor, score, load)."""
    elig = [p for p in profs.values() if p.anchor is not None and p.score >= settings.MISSION_MIN_SCORE]
    wins = eligible_windows(now)
    if not elig or not wins:
        return []
    rows = (await db.execute(select(Mission.user_id, Mission.road_segment_id, Mission.target_day_part,
                                    Mission.status).where(
        Mission.tenant_id == tenant_id, Mission.kind == "road",
        or_(Mission.status.in_(_ACTIVE),
            Mission.created_at >= now - timedelta(hours=rs.ROAD_REPEAT_COOLDOWN_HOURS))))).all()
    committed: set = set()
    pairs: set = set()
    road_load: dict = defaultdict(int)
    for uid, sid, part, st in rows:
        pairs.add((uid, sid))
        if st in _ACTIVE:
            committed.add((sid, part.value if part else None))
            road_load[uid] += 1

    radius_km = rs.ROAD_MISSION_MAX_RADIUS_KM
    edge_km = h3.average_hexagon_edge_length(rs.ROAD_GROUP_H3_RES, unit="km")
    cache: dict = {}
    created: list[Mission] = []
    for p in sorted(elig, key=lambda x: x.score, reverse=True)[:rs.ROAD_MAX_USERS_PER_RUN]:
        uid = p.user_id
        cap = min(max_new, rs.ROAD_MAX_PENDING_PER_USER - road_load[uid], settings.MISSION_MAX_PENDING - p.load)
        if cap <= 0:
            continue
        gk = h3.latlng_to_cell(p.anchor[0], p.anchor[1], rs.ROAD_GROUP_H3_RES)
        if gk not in cache:                                    # une requête par groupe de proximité
            clat, clon = h3.cell_to_latlng(gk)
            cands = await load_candidates(db, tenant_id, clat, clon, radius_km + edge_km + 0.5, now)
            cache[gk] = [(c, s) for c in cands if (s := slots(c, wins))]
        ranked = []
        for c, sl in cache[gk]:
            if (uid, c.id) in pairs:
                continue
            pick = next(((n, part) for n, part in sl if (c.id, part) not in committed), None)
            if pick is None:
                continue
            d_km = haversine_m(p.anchor[0], p.anchor[1], c.lat, c.lon) / 1000
            if d_km > radius_km:
                continue
            need, part = pick
            fit = (1 - d_km / radius_km) ** 0.7
            ranked.append((need * (0.35 + 0.65 * fit) * (0.6 + 0.4 * p.score), need, fit, c, part, d_km))
        ranked.sort(key=lambda t: t[0], reverse=True)
        chosen: list[Candidate] = []
        for _, need, fit, c, part, d_km in ranked:
            if len(chosen) >= cap:
                break
            if (c.id, part) in committed:
                continue
            if any(haversine_m(c.lat, c.lon, o.lat, o.lon) < rs.ROAD_MIN_SPACING_M for o in chosen):
                continue
            chosen.append(c)
            created.append(_make(tenant_id, p, c, part, wins[part], d_km, need, fit))
            committed.add((c.id, part))
            pairs.add((uid, c.id))
            road_load[uid] += 1
            p.load += 1
    if created:
        db.add_all(created)
        await db.flush()
    return created


async def needs_near(db: AsyncSession, tenant_id: uuid.UUID, lat: float, lon: float, radius_km: float,
                     limit: int) -> list[dict]:
    """Vue admin : routes qui manquent le plus de mesures autour d'un point, avec la plage conseillée."""
    now = datetime.now(timezone.utc)
    wins = eligible_windows(now)
    out = []
    for c in await load_candidates(db, tenant_id, lat, lon, radius_km, now):
        sl = slots(c, wins)
        d = haversine_m(lat, lon, c.lat, c.lon)
        if not sl or d > radius_km * 1000:
            continue
        need, part = sl[0]
        out.append({"segment_id": str(c.id), "name": c.name or c.ref, "highway": c.highway, "rank": c.rank,
                    "lat": c.lat, "lon": c.lon, "distance_m": round(d), "need": round(need, 3),
                    "best_day_part": part, "window_start_at": wins[part][0], "window_end_at": wins[part][1],
                    "counts_by_day_part": c.counts})
    out.sort(key=lambda r: r["need"], reverse=True)
    return out[:limit]