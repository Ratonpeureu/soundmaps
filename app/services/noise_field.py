import asyncio
import base64
import hashlib
import json
import math
import uuid
from datetime import datetime, timezone

import h3
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.field_settings import field_settings as fs
from app.schemas.schemas import TimeFilter
from app.services import weather
from app.services.geo import cell_center
from app.services.map import cell_rows

_DB0, _DBSTEP = 20.0, 0.5            # niveau = 20 + octet x 0,5 dB (0 = pas de donnée)


def _compute(sources: list, bounds: tuple, cols: int, rows: int, wind: dict | None, sigma0: float) -> dict:
    west, south, east, north = bounds
    lat0, lon0 = (south + north) / 2, (west + east) / 2
    mx, my = 111320.0 * math.cos(math.radians(lat0)), 110574.0
    width, height = (east - west) * mx, (north - south) * my
    sx, sy = width / cols, height / rows
    x0, y0 = -width / 2, -height / 2
    sigma = max(sigma0, 1.5 * max(sx, sy))

    speed = wind["speed_ms"] if wind else 0.0
    ux = (wind["u"] / speed) if speed > 0.05 else 0.0
    uy = (wind["v"] / speed) if speed > 0.05 else 0.0
    asym = min(1.0, speed / 3.0)

    num = [0.0] * (cols * rows)
    den = [0.0] * (cols * rows)
    for lat, lon, level, w in sources:
        px, py = (lon - lon0) * mx, (lat - lat0) * my
        loud = min(1.0, max(0.0, (level - 45.0) / 45.0))
        stretch = 1.0 + min(1.5, 0.12 * speed * (0.6 + loud))
        s_dn = sigma * stretch * (1.0 + 0.25 * asym)          # côté aval : le nuage s'étire
        s_up = sigma * (1.0 - 0.25 * asym)                    # côté amont : bruit atténué
        s_pp = sigma * max(0.65, 1.0 - 0.04 * speed)          # largeur transversale
        shift = min(fs.FIELD_MAX_DRIFT_M, speed * fs.FIELD_ADVECTION_SECONDS * (0.5 + 0.5 * loud), 0.6 * s_dn)
        cx, cy = px + ux * shift, py + uy * shift
        reach = 3.0 * max(s_dn, s_up, s_pp)
        i0, i1 = max(0, int((cx - reach - x0) / sx)), min(cols - 1, int((cx + reach - x0) / sx))
        j0, j1 = max(0, int((cy - reach - y0) / sy)), min(rows - 1, int((cy + reach - y0) / sy))
        if i0 > i1 or j0 > j1:
            continue
        e = 10.0 ** (level / 10.0)
        for j in range(j0, j1 + 1):
            dy = y0 + (j + 0.5) * sy - cy
            base = j * cols
            for i in range(i0, i1 + 1):
                dx = x0 + (i + 0.5) * sx - cx
                par = dx * ux + dy * uy
                per = -dx * uy + dy * ux
                sp = s_dn if par >= 0 else s_up
                k = w * math.exp(-0.5 * ((par / sp) ** 2 + (per / s_pp) ** 2))
                num[base + i] += k * e
                den[base + i] += k

    w0 = fs.FIELD_BACKGROUND_WEIGHT
    level_b, support_b = bytearray(cols * rows), bytearray(cols * rows)
    lo, hi = 999.0, -999.0
    for idx in range(cols * rows):
        d = den[idx]
        if d <= 0:
            continue
        support = d / (d + w0)
        if support < 0.03:
            continue
        db = 10.0 * math.log10(max(num[idx] / (d + w0), 1e-12))
        if db < 30.0:
            continue
        level_b[idx] = max(1, min(255, int(round((db - _DB0) / _DBSTEP))))
        support_b[idx] = int(round(255 * support))
        lo, hi = min(lo, db), max(hi, db)
    return {"level": bytes(level_b), "support": bytes(support_b),
            "min_db": None if lo > hi else round(lo, 1), "max_db": None if lo > hi else round(hi, 1)}


async def build(db: AsyncSession, redis: Redis, tenant_id: uuid.UUID, tf: TimeFilter, include_indicative: bool,
                min_devices: int, bbox: tuple[float, float, float, float] | None) -> dict:
    if bbox is None:
        raise HTTPException(422, "bbox_required")
    if bbox[2] - bbox[0] > fs.FIELD_MAX_BBOX_DEG or bbox[3] - bbox[1] > fs.FIELD_MAX_BBOX_DEG:
        raise HTTPException(422, "bbox_too_large")
    snap = 0.01                                   # bbox calée : tous les utilisateurs partagent le même résultat en cache
    b = (math.floor(bbox[0] / snap) * snap, math.floor(bbox[1] / snap) * snap,
         math.ceil(bbox[2] / snap) * snap, math.ceil(bbox[3] / snap) * snap)
    clat, clon = (b[1] + b[3]) / 2, (b[0] + b[2]) / 2
    wind = await weather.wind_at(redis, clat, clon)

    raw = json.dumps([str(tenant_id), tf.period.value, str(tf.date_from), str(tf.date_to), tf.day_part,
                      include_indicative, min_devices, [round(v, 4) for v in b],
                      None if wind is None else [round(wind["speed_kmh"]), round(wind["dir_from"] / 5)]])
    key = f"t:{tenant_id}:field:{hashlib.sha256(raw.encode()).hexdigest()}"
    hit = await redis.get(key)
    if hit:
        return json.loads(hit)

    mx = 111320.0 * math.cos(math.radians(clat))
    width_m, height_m = (b[2] - b[0]) * mx, (b[3] - b[1]) * 110574.0
    cols = int(min(fs.FIELD_MAX_GRID, max(fs.FIELD_MIN_GRID, math.ceil(width_m / fs.FIELD_TARGET_STEP_M))))
    rows = int(min(fs.FIELD_MAX_GRID, max(fs.FIELD_MIN_GRID, math.ceil(height_m / fs.FIELD_TARGET_STEP_M))))
    cell_r = h3.average_hexagon_edge_length(settings.H3_RESOLUTION, unit="m")
    sigma0 = fs.FIELD_SIGMA_FACTOR * cell_r
    sigma_eff = max(sigma0, 1.5 * max(width_m / cols, height_m / rows))
    margin_m = 11.5 * sigma_eff                    # portée maximale d'une source (étirement + décalage compris)
    mdx = min(0.5, margin_m / (111320.0 * math.cos(math.radians(clat))))
    mdy = min(0.5, margin_m / 110574.0)
    ebbox = (max(-179.9, b[0] - mdx), max(-89.9, b[1] - mdy), min(179.9, b[2] + mdx), min(89.9, b[3] + mdy))

    cells = await cell_rows(db, tenant_id, tf, include_indicative, min_devices, ebbox)
    cells.sort(key=lambda r: r["measurement_count"], reverse=True)
    sources = []
    for r in cells[:fs.FIELD_MAX_SOURCES]:
        lat, lon = cell_center(r["h3_index"])
        sources.append((lat, lon, float(r["laeq_db"]), max(0.1, float(r["confidence"]))))

    res = await asyncio.to_thread(_compute, sources, b, cols, rows, wind, sigma0)
    out = {
        "bounds": [b[0], b[1], b[2], b[3]], "cols": cols, "rows": rows,
        "db_offset": _DB0, "db_step": _DBSTEP,
        "level_b64": base64.b64encode(res["level"]).decode(), "support_b64": base64.b64encode(res["support"]).decode(),
        "min_db": res["min_db"], "max_db": res["max_db"], "sources": len(sources),
        "wind": None if wind is None else {k: wind[k] for k in ("speed_kmh", "dir_from", "dir_to", "u", "v")},
        "generated_at": datetime.now(timezone.utc).isoformat()}
    await redis.set(key, json.dumps(out), ex=fs.FIELD_CACHE_TTL_SECONDS)
    return out