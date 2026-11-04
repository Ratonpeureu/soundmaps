import hashlib
import json
from datetime import timedelta

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import Float, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
import uuid

from app.core.config import settings
from app.models.enums import DayPart
from app.models.geo import GeographicCell, NoiseAggregation
from app.models.measurement import Measurement
from app.schemas.schemas import TimeFilter
from app.services.geo import cell_margin_degrees, cell_polygon, period_dates, resolve_period
from app.services.map import statuses

_TEN = literal_column("10.0")
PARTS = [p.value for p in DayPart]


async def build(db: AsyncSession, redis: Redis, tenant_id: uuid.UUID, tf: TimeFilter, step: str,
                include_indicative: bool, bbox, min_devices: int) -> dict:
    start, end = resolve_period(tf)
    d_from, d_to = period_dates(start, end)
    days = [d_from + timedelta(days=i) for i in range((d_to - d_from).days + 1)]
    if step == "daypart":
        parts = [tf.day_part] if tf.day_part != "all" else PARTS
    else:
        parts = [None]
    if len(days) * len(parts) > settings.TIMELAPSE_MAX_FRAMES:
        raise HTTPException(422, "too_many_frames")

    raw = json.dumps([str(tenant_id), d_from.isoformat(), d_to.isoformat(), tf.day_part, step,
                      include_indicative, bbox, min_devices])
    key = f"t:{tenant_id}:tl:{hashlib.sha256(raw.encode()).hexdigest()}"
    hit = await redis.get(key)
    if hit:
        return json.loads(hit)

    stats = statuses(include_indicative)
    en, du = func.sum(NoiseAggregation.energy_sum), func.sum(NoiseAggregation.duration_total)
    cols = [NoiseAggregation.bucket_date, GeographicCell.h3_index,
            func.sum(NoiseAggregation.measurement_count), _TEN * func.log(en / du, type_=Float)]
    group = [NoiseAggregation.bucket_date, GeographicCell.h3_index]
    if step == "daypart":
        cols.insert(1, NoiseAggregation.day_part)
        group.insert(1, NoiseAggregation.day_part)
    stmt = (select(*cols).join(GeographicCell, GeographicCell.id == NoiseAggregation.cell_id)
            .where(NoiseAggregation.tenant_id == tenant_id, NoiseAggregation.bucket_date >= d_from,
                   NoiseAggregation.bucket_date <= d_to, NoiseAggregation.quality_status.in_(stats))
            .group_by(*group).limit(settings.TIMELAPSE_MAX_ROWS))
    if tf.day_part != "all":
        stmt = stmt.where(NoiseAggregation.day_part == DayPart(tf.day_part))
    if bbox:
        m = cell_margin_degrees()
        stmt = stmt.where(func.ST_Intersects(GeographicCell.center, func.ST_MakeEnvelope(
            bbox[0] - m, bbox[1] - m, bbox[2] + m, bbox[3] + m, 4326)))

    eligible = None
    if min_devices > 1:                                   # k-anonymat : même règle que la carte
        conds = [Measurement.tenant_id == tenant_id, Measurement.measured_at >= start,
                 Measurement.measured_at < end, Measurement.quality_status.in_(stats)]
        if tf.day_part != "all":
            conds.append(Measurement.day_part == DayPart(tf.day_part))
        sub = (select(Measurement.h3_index).where(*conds).group_by(Measurement.h3_index)
               .having(func.count(func.distinct(Measurement.contributor_hash)) >= min_devices))
        eligible = set((await db.execute(sub)).scalars())

    frames: dict = {(d, p): [] for d in days for p in parts}
    index: dict[str, int] = {}
    lo, hi = 1e9, -1e9
    for row in (await db.execute(stmt)).all():
        if step == "daypart":
            d, part, h, n, laeq = row
            part = part.value
        else:
            d, h, n, laeq = row
            part = None
        if laeq is None or (eligible is not None and h not in eligible) or (d, part) not in frames:
            continue
        idx = index.setdefault(h, len(index))
        v = round(float(laeq), 1)
        lo, hi = min(lo, v), max(hi, v)
        frames[(d, part)].append([idx, v, int(n)])

    out = {"step": step, "min_db": None if lo > hi else lo, "max_db": None if lo > hi else hi,
           "cells": [{"h3": h, "polygon": cell_polygon(h)} for h in index],
           "frames": [{"t": d.isoformat(), "part": p, "cells": frames[(d, p)]} for d in days for p in parts]}
    await redis.set(key, json.dumps(out), ex=settings.TIMELAPSE_CACHE_TTL_SECONDS)
    return out