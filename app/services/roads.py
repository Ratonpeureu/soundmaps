import hashlib
import json
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import or_
from app.models.measurement import Measurement, MeasurementQuality
import h3
import httpx
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.road_settings import road_settings as rs
from app.models.enums import QualityStatus
from app.models.measurement import Measurement
from app.models.roads import RoadImportTile, RoadSegment
from app.schemas.schemas import TimeFilter
from app.services.acoustics import clamp01
from app.services.geo import resolve_period
from app.services.map import _agg_cols, _conditions, confidence_of, statuses

log = logging.getLogger(__name__)

_NS = uuid.UUID("a3c1f0de-7a55-4b1e-8d52-3f6f6f1e9a10")
_USABLE = [QualityStatus.validated, QualityStatus.indicative]

# highway -> (rang, poids de trafic attendu)
HIGHWAYS: dict[str, tuple[int, float]] = {
    "motorway": (1, 1.0), "motorway_link": (1, 0.8), "trunk": (1, 0.95), "trunk_link": (1, 0.75),
    "primary": (2, 0.85), "primary_link": (2, 0.65), "secondary": (3, 0.7), "secondary_link": (3, 0.55),
    "tertiary": (4, 0.55), "tertiary_link": (4, 0.45), "unclassified": (5, 0.4),
    "residential": (6, 0.3), "living_street": (6, 0.15), "service": (6, 0.12),
}


class OverpassBusy(Exception):
    pass


class OverpassTooHeavy(Exception):
    pass


# ------------------------------------------------------------------ géométrie

def segment_uuid(way_id: int, seq: int) -> uuid.UUID:
    return uuid.uuid5(_NS, f"{way_id}:{seq}")


def _hav(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = (math.radians(v) for v in (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(h))


def split_way(pts: list[tuple[float, float]], target_m: float, min_m: float) -> list[tuple[int, int, float]]:
    """Découpe une route en tronçons coupés sur de vrais sommets (la connectivité est conservée)."""
    n = len(pts)
    if n < 2:
        return []
    steps = [_hav(pts[i], pts[i + 1]) for i in range(n - 1)]
    cuts, acc = [0], 0.0
    for i in range(1, n):
        acc += steps[i - 1]
        if acc >= target_m and i < n - 1:
            cuts.append(i)
            acc = 0.0
    cuts.append(n - 1)
    if len(cuts) > 2 and sum(steps[cuts[-2]:cuts[-1]]) < min_m:
        cuts.pop(-2)                                        # le petit reste est fusionné au tronçon précédent
    return [(a, b, sum(steps[a:b])) for a, b in zip(cuts, cuts[1:])]


def _point_along(pts: list[tuple[float, float]], frac: float) -> tuple[float, float]:
    steps = [_hav(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    target, acc = sum(steps) * frac, 0.0
    for k, s in enumerate(steps):
        if s > 0 and acc + s >= target:
            t = (target - acc) / s
            return (pts[k][0] + (pts[k + 1][0] - pts[k][0]) * t, pts[k][1] + (pts[k + 1][1] - pts[k][1]) * t)
        acc += s
    return pts[-1]


def rows_from_elements(elements: list[dict], now: datetime) -> tuple[list[dict], list[int]]:
    allowed = set(rs.highways)
    rows: list[dict] = []
    way_ids: list[int] = []
    for el in elements:
        if el.get("type") != "way":
            continue
        tags = el.get("tags") or {}
        hw = tags.get("highway")
        if hw not in allowed or hw not in HIGHWAYS or tags.get("area") == "yes":
            continue
        geom, nodes = el.get("geometry") or [], el.get("nodes") or []
        if len(geom) != len(nodes):
            continue
        pts, nds = [], []
        for g, nid in zip(geom, nodes):
            if g:
                pts.append((g["lat"], g["lon"]))
                nds.append(int(nid))
        if len(pts) < 2:
            continue
        rank, weight = HIGHWAYS[hw]
        way_id = int(el["id"])
        way_ids.append(way_id)
        name = (tags.get("name") or "").strip()[:160] or None
        ref = (tags.get("ref") or "").strip()[:60] or None
        oneway = tags.get("oneway") in ("yes", "true", "1") or tags.get("junction") == "roundabout"
        maxspeed = (tags.get("maxspeed") or "")[:20] or None
        try:
            lanes = int(str(tags.get("lanes", "")).split(";")[0])
        except ValueError:
            lanes = None
        lanes = lanes if lanes and 0 < lanes <= 20 else None
        for seq, (a, b, length) in enumerate(split_way(pts, rs.ROAD_SEGMENT_TARGET_M, rs.ROAD_SEGMENT_MIN_M)):
            sub = pts[a:b + 1]
            if length < 1.0:
                continue
            mlat, mlon = _point_along(sub, 0.5)
            rows.append({
                "id": segment_uuid(way_id, seq), "osm_way_id": way_id, "seq": seq,
                "start_node": nds[a], "end_node": nds[b], "highway": hw, "road_rank": rank,
                "class_weight": weight, "name": name, "ref": ref, "oneway": oneway, "maxspeed": maxspeed,
                "lanes": lanes, "length_m": round(length, 1), "mid_lat": mlat, "mid_lon": mlon,
                "h3_index": h3.latlng_to_cell(mlat, mlon, settings.H3_RESOLUTION),
                "wkt": "LINESTRING(" + ",".join(f"{lo:.6f} {la:.6f}" for la, lo in sub) + ")",
                "imported_at": now})
    return rows, way_ids


# ------------------------------------------------------------------ Overpass

def build_query(bbox: tuple[float, float, float, float]) -> str:
    w, s, e, n = bbox
    hw = "|".join(rs.highways)
    area = flt = ""
    if rs.ROAD_COUNTRY_ISO:
        area = f'area["ISO3166-1"="{rs.ROAD_COUNTRY_ISO}"][admin_level=2]->.a;'
        flt = "(area.a)"
    return (f"[out:json][timeout:{rs.ROAD_OVERPASS_TIMEOUT_SECONDS}];{area}"
            f'way["highway"~"^({hw})$"]({s},{w},{n},{e}){flt};out body geom;')


async def fetch_overpass(bbox: tuple[float, float, float, float]) -> dict:
    query = build_query(bbox)
    last: Exception = OverpassBusy("no_endpoint")
    headers = {"User-Agent": "SunuSound/1.0 (road network import)"}
    async with httpx.AsyncClient(timeout=rs.ROAD_HTTP_TIMEOUT_SECONDS, headers=headers) as client:
        for url in rs.urls:
            try:
                r = await client.post(url, data={"data": query})
            except httpx.HTTPError as exc:
                last = OverpassBusy(type(exc).__name__)
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    last = OverpassBusy("bad_json")
                    continue
                remark = str(data.get("remark") or "").lower()
                if "runtime error" in remark or "out of memory" in remark or "timed out" in remark:
                    raise OverpassTooHeavy(remark[:120])
                return data
            last = OverpassBusy(f"http_{r.status_code}")
    raise last


# ------------------------------------------------------------------ tuiles d'import

def tile_key(b: tuple[float, float, float, float]) -> str:
    return f"{b[0]:.5f}_{b[1]:.5f}_{b[2]:.5f}_{b[3]:.5f}"


async def plan_tiles(db: AsyncSession) -> int:
    w0, s0, e0, n0 = rs.bbox
    step = rs.ROAD_TILE_SIZE_DEG
    now = datetime.now(timezone.utc)
    rows = []
    x = w0
    while x < e0 - 1e-9:
        y = s0
        while y < n0 - 1e-9:
            b = (round(x, 5), round(y, 5), round(min(x + step, e0), 5), round(min(y + step, n0), 5))
            rows.append({"key": tile_key(b), "west": b[0], "south": b[1], "east": b[2], "north": b[3],
                         "size_deg": step, "status": "pending", "attempts": 0, "updated_at": now})
            y += step
        x += step
    for i in range(0, len(rows), 500):
        await db.execute(pg_insert(RoadImportTile).values(rows[i:i + 500]).on_conflict_do_nothing(
            index_elements=["key"]))
    await db.commit()
    return len(rows)


async def network_ready(db: AsyncSession) -> bool:
    total, open_ = (await db.execute(
        select(func.count(), func.count().filter(RoadImportTile.status.in_(("pending", "queued", "running", "failed"))))
        .select_from(RoadImportTile))).one()
    return int(total) > 0 and int(open_) == 0


async def version(redis: Redis) -> int:
    return int(await redis.get("roads:version") or 0)


async def ensure_network(db: AsyncSession, redis: Redis, force: bool = False) -> dict:
    """Planifie l'import au premier lancement, puis relance les tuiles en attente, échouées ou bloquées."""
    total = (await db.execute(select(func.count()).select_from(RoadImportTile))).scalar_one()
    if total == 0:
        if not rs.ROAD_AUTO_IMPORT and not force:
            return {"status": "disabled", "queued": []}
        planned = await plan_tiles(db)
        log.info("roads_import_planned", extra={"task": f"tiles={planned}"})
    now = datetime.now(timezone.utc)
    stale = now - timedelta(minutes=rs.ROAD_STALE_MINUTES)
    retry_failed = now - timedelta(hours=6)
    tiles = (await db.execute(select(RoadImportTile).where(RoadImportTile.status.in_(
        ("pending", "failed", "queued", "running"))).order_by(RoadImportTile.key))).scalars().all()
    keys: list[str] = []
    for t in tiles:
        if t.status == "failed" and not force and t.updated_at > retry_failed:
            continue
        if t.status in ("queued", "running") and t.updated_at > stale and not force:
            continue
        if t.status == "failed":
            t.attempts = 0
        t.status, t.updated_at = "queued", now
        keys.append(t.key)
    await db.commit()
    return {"status": "ok", "queued": keys, "ready": await network_ready(db)}


async def _split(db: AsyncSession, tile: RoadImportTile) -> list[str]:
    w, s, e, n = tile.west, tile.south, tile.east, tile.north
    mw, mn = round((w + e) / 2, 5), round((s + n) / 2, 5)
    boxes = [(w, s, mw, mn), (mw, s, e, mn), (w, mn, mw, n), (mw, mn, e, n)]
    size = max(e - w, n - s) / 2
    now = datetime.now(timezone.utc)
    keys = []
    for b in boxes:
        k = tile_key(b)
        keys.append(k)
        await db.execute(pg_insert(RoadImportTile).values(
            key=k, west=b[0], south=b[1], east=b[2], north=b[3], size_deg=size, status="queued",
            attempts=0, updated_at=now).on_conflict_do_nothing(index_elements=["key"]))
    tile.status, tile.error, tile.finished_at, tile.updated_at = "split", None, now, now
    await db.commit()
    return keys


_UPSERT = text("""
INSERT INTO road_segments (id, osm_way_id, seq, start_node, end_node, highway, road_rank, class_weight, name, ref,
    oneway, maxspeed, lanes, length_m, mid_lat, mid_lon, h3_index, geom, active, imported_at)
VALUES (:id, :osm_way_id, :seq, :start_node, :end_node, :highway, :road_rank, :class_weight, :name, :ref,
    :oneway, :maxspeed, :lanes, :length_m, :mid_lat, :mid_lon, :h3_index,
    ST_SetSRID(ST_GeomFromText(:wkt), 4326), true, :imported_at)
ON CONFLICT (osm_way_id, seq) DO UPDATE SET
    start_node = EXCLUDED.start_node, end_node = EXCLUDED.end_node, highway = EXCLUDED.highway,
    road_rank = EXCLUDED.road_rank, class_weight = EXCLUDED.class_weight, name = EXCLUDED.name,
    ref = EXCLUDED.ref, oneway = EXCLUDED.oneway, maxspeed = EXCLUDED.maxspeed, lanes = EXCLUDED.lanes,
    length_m = EXCLUDED.length_m, mid_lat = EXCLUDED.mid_lat, mid_lon = EXCLUDED.mid_lon,
    h3_index = EXCLUDED.h3_index, geom = EXCLUDED.geom, active = true, imported_at = EXCLUDED.imported_at
""")
_DEACTIVATE = text("UPDATE road_segments SET active = false "
                   "WHERE osm_way_id = ANY(:ids) AND imported_at < :since AND active")


async def run_tile(db: AsyncSession, redis: Redis, key: str) -> dict:
    """Importe une tuile. Retourne {children}, {retry_in}, {failed}, {done} ou {skipped}."""
    tile = await db.get(RoadImportTile, key)
    if tile is None or tile.status in ("done", "split"):
        return {"skipped": True}
    started = datetime.now(timezone.utc)
    tile.status, tile.attempts, tile.updated_at = "running", tile.attempts + 1, started
    await db.commit()
    bbox = (tile.west, tile.south, tile.east, tile.north)
    splittable = max(tile.east - tile.west, tile.north - tile.south) > rs.ROAD_TILE_MIN_DEG * 1.01
    try:
        data = await fetch_overpass(bbox)
        elements = data.get("elements") or []
        if len(elements) > rs.ROAD_TILE_MAX_WAYS and splittable:
            return {"children": await _split(db, tile), "split": True}
        rows, way_ids = rows_from_elements(elements, started)
        for i in range(0, len(rows), rs.ROAD_INSERT_CHUNK):
            await db.execute(_UPSERT, rows[i:i + rs.ROAD_INSERT_CHUNK])
        for i in range(0, len(way_ids), 20000):
            await db.execute(_DEACTIVATE, {"ids": way_ids[i:i + 20000], "since": started})
        tile.status, tile.ways, tile.segments, tile.error = "done", len(way_ids), len(rows), None
        tile.finished_at = tile.updated_at = datetime.now(timezone.utc)
        await db.commit()
    except (OverpassTooHeavy, OverpassBusy) as exc:
        await db.rollback()
        tile = await db.get(RoadImportTile, key)
        if (isinstance(exc, OverpassTooHeavy) or tile.attempts >= 2) and splittable:
            return {"children": await _split(db, tile), "split": True}
        if tile.attempts >= rs.ROAD_TILE_MAX_ATTEMPTS:
            tile.status, tile.error = "failed", str(exc)[:200]
            tile.updated_at = datetime.now(timezone.utc)
            await db.commit()
            return {"failed": True}
        tile.status, tile.error, tile.updated_at = "queued", str(exc)[:200], datetime.now(timezone.utc)
        await db.commit()
        return {"retry_in": min(900, 60 * tile.attempts)}
    except Exception as exc:
        await db.rollback()
        tile = await db.get(RoadImportTile, key)
        tile.status, tile.error = "failed", f"{type(exc).__name__}: {exc}"[:200]
        tile.updated_at = datetime.now(timezone.utc)
        await db.commit()
        log.exception("road_tile_failed", extra={"task": key})
        return {"failed": True}
    if await network_ready(db):
        await redis.incr("roads:version")                 # invalide les caches du réseau
        log.info("roads_network_ready")
    return {"done": True, "ways": len(way_ids), "segments": len(rows)}


async def import_status(db: AsyncSession, redis: Redis) -> dict:
    by = dict((await db.execute(select(RoadImportTile.status, func.count())
                                .group_by(RoadImportTile.status))).all())
    segs, km = (await db.execute(select(func.count(), func.coalesce(func.sum(RoadSegment.length_m), 0))
                                 .where(RoadSegment.active.is_(True)))).one()
    return {"tiles": by, "ready": await network_ready(db), "segments": int(segs),
            "km": round(float(km) / 1000, 1), "version": await version(redis)}


# ------------------------------------------------------------------ rattachement mesure -> tronçon

_MATCH = text("""
WITH todo AS (
    SELECT id, geom FROM measurements
    WHERE tenant_id = :tid AND road_matched_at IS NULL AND location_anonymized = false
    ORDER BY measured_at DESC LIMIT :lim FOR UPDATE SKIP LOCKED
), best AS (
    SELECT t.id AS mid, r.id AS sid, r.dist AS dist
    FROM todo t LEFT JOIN LATERAL (
        SELECT s.id, ST_Distance(s.geom::geography, t.geom::geography) AS dist
        FROM road_segments s
        WHERE s.active AND ST_DWithin(s.geom, t.geom, :deg)
        ORDER BY s.geom <-> t.geom LIMIT 1
    ) r ON true
)
UPDATE measurements m SET
    road_segment_id = CASE WHEN best.dist <= :max_m THEN best.sid END,
    road_distance_m = CASE WHEN best.dist <= :max_m THEN best.dist END,
    road_matched_at = CASE WHEN best.dist <= :max_m OR :final THEN now() END
FROM best WHERE m.id = best.mid
""")


async def match_measurements(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Rattache les mesures non traitées au tronçon le plus proche. Tant que l'import n'est pas terminé,
    une mesure sans tronçon reste à retraiter (elle peut tomber dans une zone pas encore importée)."""
    if not (await db.execute(select(RoadSegment.id).limit(1))).first():
        return 0
    ready = await network_ready(db)
    deg = rs.ROAD_MATCH_MAX_M / 111320.0 * 1.6
    res = await db.execute(_MATCH, {"tid": tenant_id, "lim": rs.ROAD_MATCH_BATCH, "deg": deg,
                                    "max_m": rs.ROAD_MATCH_MAX_M, "final": ready})
    return res.rowcount or 0


# ------------------------------------------------------------------ carte : réseau et état de bruit

def rank_for(zoom: float | None, max_rank: int | None) -> int:
    if max_rank:
        return max_rank
    if zoom is None:
        return 3
    for z, r in ((8, 2), (11, 3), (13, 4), (15, 5)):
        if zoom < z:
            return r
    return 6


def check_bbox(bbox: tuple[float, float, float, float]) -> None:
    if bbox[2] - bbox[0] > rs.ROAD_MAP_MAX_BBOX_DEG or bbox[3] - bbox[1] > rs.ROAD_MAP_MAX_BBOX_DEG:
        raise HTTPException(422, "bbox_too_large")


def _snap(bbox, step: float):
    return (round(math.floor(bbox[0] / step) * step, 5), round(math.floor(bbox[1] / step) * step, 5),
            round(math.ceil(bbox[2] / step) * step, 5), round(math.ceil(bbox[3] / step) * step, 5))


def intensity(db: float) -> float:
    return round(clamp01((db - settings.NOISE_REF_MIN_DB) / (settings.NOISE_REF_MAX_DB - settings.NOISE_REF_MIN_DB)), 3)


def baseline_state() -> dict:
    return {"has_data": False, "laeq_db": rs.ROAD_BASELINE_DB, "lmax_db": None, "measurement_count": 0,
            "device_count": 0, "confidence": 0.0, "last_measurement_at": None,
            "intensity": intensity(rs.ROAD_BASELINE_DB)}


def _state(r, now: datetime) -> dict:
    laeq = round(float(r.laeq), 1)
    return {"has_data": True, "laeq_db": laeq, "lmax_db": round(float(r.lmax), 1), "measurement_count": r.n,
            "device_count": r.devices,
            "confidence": confidence_of(r.n, r.devices, int(r.calibrated or 0), r.last, now),
            "last_measurement_at": r.last.isoformat() if r.last else None, "intensity": intensity(laeq)}


async def network_features(db: AsyncSession, redis: Redis, bbox, max_rank: int) -> dict:
    """Géométrie statique du réseau (mise en cache : identique pour tous, change seulement après un import)."""
    check_bbox(bbox)
    b = _snap(bbox, 0.02)
    key = f"roads:net:{await version(redis)}:{max_rank}:{b[0]}:{b[1]}:{b[2]}:{b[3]}"
    etag = '"' + hashlib.sha1(key.encode()).hexdigest()[:20] + '"'
    hit = await redis.get(key)
    if hit:
        d = json.loads(hit)
        return {**d, "etag": etag}
    cap = rs.ROAD_MAP_MAX_FEATURES
    rows = (await db.execute(
        select(RoadSegment.id, RoadSegment.highway, RoadSegment.name, RoadSegment.ref, RoadSegment.road_rank,
               RoadSegment.length_m, RoadSegment.oneway, RoadSegment.maxspeed,
               func.ST_AsGeoJSON(RoadSegment.geom, 6))
        .where(RoadSegment.active.is_(True), RoadSegment.road_rank <= max_rank,
               func.ST_Intersects(RoadSegment.geom, func.ST_MakeEnvelope(b[0], b[1], b[2], b[3], 4326)))
        .order_by(RoadSegment.road_rank, RoadSegment.length_m.desc()).limit(cap + 1))).all()
    truncated = len(rows) > cap
    feats = [{"type": "Feature", "id": str(sid), "geometry": json.loads(gj),
              "properties": {"id": str(sid), "highway": hw, "name": name, "ref": ref, "rank": rank,
                             "length_m": ln, "oneway": ow, "maxspeed": ms}}
             for sid, hw, name, ref, rank, ln, ow, ms, gj in rows[:cap]]
    payload = {"features": feats, "truncated": truncated}
    await redis.set(key, json.dumps(payload), ex=rs.ROAD_NETWORK_CACHE_TTL_SECONDS)
    return {**payload, "etag": etag}


async def road_states(db: AsyncSession, tenant_id: uuid.UUID, tf: TimeFilter, include_indicative: bool, bbox,
                      max_rank: int, min_devices: int) -> dict[str, dict]:
    start, end = resolve_period(tf)
    cols = _agg_cols()
    now = datetime.now(timezone.utc)
    stmt = (select(Measurement.road_segment_id.label("sid"), *[c.label(k) for k, c in cols.items()])
            .join(RoadSegment, RoadSegment.id == Measurement.road_segment_id)
            .where(*_conditions(tenant_id, start, end, tf.day_part, statuses(include_indicative)),
                   RoadSegment.active.is_(True), RoadSegment.road_rank <= max_rank,
                   func.ST_Intersects(RoadSegment.geom,
                                      func.ST_MakeEnvelope(bbox[0], bbox[1], bbox[2], bbox[3], 4326)))
            .group_by(Measurement.road_segment_id).limit(rs.ROAD_MAP_MAX_FEATURES * 2))
    #if min_devices > 1:                                    # k-anonymat : même règle que la carte
    #    stmt = stmt.having(cols["devices"] >= min_devices)

    if min_devices > 1:
        stmt = stmt.outerjoin(MeasurementQuality, MeasurementQuality.measurement_id == Measurement.id)
        admin_ok = func.bool_or(MeasurementQuality.evaluator == 'admin')
        stmt = stmt.having(or_(cols["devices"] >= min_devices, admin_ok))
    return {str(r.sid): _state(r, now) for r in (await db.execute(stmt)).all()}


async def cached_states(db: AsyncSession, redis: Redis, tenant_id: uuid.UUID, tf: TimeFilter,
                        include_indicative: bool, bbox, max_rank: int, min_devices: int) -> dict[str, dict]:
    check_bbox(bbox)
    b = _snap(bbox, 0.02)
    raw = json.dumps([str(tenant_id), tf.period.value, str(tf.date_from), str(tf.date_to), tf.day_part,
                      include_indicative, b, max_rank, min_devices], default=str)
    key = "roads:st:" + hashlib.sha256(raw.encode()).hexdigest()
    hit = await redis.get(key)
    if hit:
        return json.loads(hit)
    data = await road_states(db, tenant_id, tf, include_indicative, b, max_rank, min_devices)
    await redis.set(key, json.dumps(data), ex=rs.ROAD_STATE_CACHE_TTL_SECONDS)
    return data


def merge(net: dict, states: dict[str, dict]) -> dict:
    feats = []
    for f in net["features"]:
        st = states.get(f["properties"]["id"]) or baseline_state()
        feats.append({**f, "properties": {**f["properties"], **st}})
    return {"type": "FeatureCollection", "features": feats, "truncated": net["truncated"],
            "baseline_db": rs.ROAD_BASELINE_DB}


async def segment_detail(db: AsyncSession, tenant_id: uuid.UUID, segment_id: uuid.UUID, tf: TimeFilter,
                         include_indicative: bool, min_devices: int) -> dict:
    row = (await db.execute(select(RoadSegment, func.ST_AsGeoJSON(RoadSegment.geom, 6))
                            .where(RoadSegment.id == segment_id))).first()
    if row is None:
        raise HTTPException(404, "road_segment_not_found")
    seg, gj = row
    start, end = resolve_period(tf)
    stats = statuses(include_indicative)
    now = datetime.now(timezone.utc)
    cols = _agg_cols()
    r = (await db.execute(select(*[c.label(k) for k, c in cols.items()]).where(
        *_conditions(tenant_id, start, end, tf.day_part, stats), Measurement.road_segment_id == segment_id))).one()
    state = _state(r, now) if r.n and r.devices >= min_devices else baseline_state()
    pcols = _agg_cols()
    pstmt = (select(Measurement.day_part, *[c.label(k) for k, c in pcols.items()])
             .where(*_conditions(tenant_id, start, end, "all", stats), Measurement.road_segment_id == segment_id)
             .group_by(Measurement.day_part))
    if min_devices > 1:
        pstmt = pstmt.having(pcols["devices"] >= min_devices)
    parts = [{"day_part": p.day_part.value, "laeq_db": round(float(p.laeq), 1), "measurement_count": p.n}
             for p in (await db.execute(pstmt)).all()]
    nodes = [seg.start_node, seg.end_node]
    conn = (await db.execute(select(RoadSegment.id, RoadSegment.name, RoadSegment.highway).where(
        RoadSegment.active.is_(True), RoadSegment.id != seg.id,
        or_(RoadSegment.start_node.in_(nodes), RoadSegment.end_node.in_(nodes))).limit(20))).all()
    return {"id": str(seg.id), "osm_way_id": seg.osm_way_id, "highway": seg.highway, "rank": seg.road_rank,
            "name": seg.name, "ref": seg.ref, "oneway": seg.oneway, "maxspeed": seg.maxspeed, "lanes": seg.lanes,
            "length_m": seg.length_m, "target": {"lat": seg.mid_lat, "lon": seg.mid_lon,
                                                 "radius_m": rs.ROAD_TARGET_RADIUS_M},
            "geometry": json.loads(gj), "state": state, "by_day_part": parts,
            "connected": [{"id": str(i), "name": n, "highway": h} for i, n, h in conn]}


async def stats(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    now = datetime.now(timezone.utc)
    by_rank = (await db.execute(select(RoadSegment.road_rank, func.count(), func.sum(RoadSegment.length_m))
                                .where(RoadSegment.active.is_(True)).group_by(RoadSegment.road_rank)
                                .order_by(RoadSegment.road_rank))).all()
    total = sum(int(n) for _, n, _ in by_rank)
    covered = (await db.execute(select(func.count(func.distinct(Measurement.road_segment_id))).where(
        Measurement.tenant_id == tenant_id, Measurement.road_segment_id.is_not(None),
        Measurement.measured_at >= now - timedelta(days=rs.ROAD_COVERAGE_WINDOW_DAYS),
        Measurement.quality_status.in_(_USABLE)))).scalar_one()
    n_all, n_matched = (await db.execute(select(
        func.count(), func.count().filter(Measurement.road_segment_id.is_not(None))).where(
        Measurement.tenant_id == tenant_id, Measurement.measured_at >= now - timedelta(days=30)))).one()
    return {"segments": total,
            "by_rank": [{"rank": r, "segments": int(n), "km": round(float(km or 0) / 1000, 1)} for r, n, km in by_rank],
            "covered_segments": int(covered),
            "coverage_share": round(int(covered) / total, 4) if total else 0.0,
            "measurements_30d": int(n_all), "matched_to_road_30d": int(n_matched),
            "matched_share": round(int(n_matched) / int(n_all), 3) if n_all else 0.0}