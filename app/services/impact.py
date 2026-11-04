import math

from sqlalchemy import case, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import QualityStatus
from app.models.measurement import Measurement, MeasurementQuality
from app.models.tenancy import User
from app.schemas.schemas import TimeFilter
from app.services.geo import cell_center, cell_polygon, resolve_period
from app.services.map import statuses

_TEN = literal_column("10.0")


def _db(energy: float, duration: float) -> float | None:
    return 10.0 * math.log10(energy / duration) if energy > 0 and duration > 0 else None


async def my_impact(db: AsyncSession, user: User, tf: TimeFilter, min_devices: int,
                    include_indicative: bool = False) -> dict:
    start, end = resolve_period(tf)
    rows = (await db.execute(
        select(Measurement, MeasurementQuality.score)
        .outerjoin(MeasurementQuality, MeasurementQuality.measurement_id == Measurement.id)
        .where(Measurement.tenant_id == user.tenant_id, Measurement.user_id == user.id,
               Measurement.measured_at >= start, Measurement.measured_at < end)
        .order_by(Measurement.measured_at.desc()).limit(500))).all()

    by_status = {s.value: 0 for s in QualityStatus}
    points, mine = [], {}
    for m, score in rows:
        by_status[m.quality_status.value] += 1
        lv = m.laeq + m.correction_db_applied
        points.append({"id": str(m.id), "measured_at": m.measured_at, "lat": m.latitude, "lon": m.longitude,
                       "h3_index": m.h3_index, "laeq_db": round(lv, 1), "quality_status": m.quality_status.value,
                       "quality_score": score})
        c = mine.setdefault(m.h3_index, {"n": 0, "v": 0, "e": 0.0})
        c["n"] += 1
        c["v"] += 1 if m.quality_status == QualityStatus.validated else 0
        c["e"] += 10.0 ** (lv / 10.0)

    agg = {}
    if mine:
        corrected = Measurement.laeq + Measurement.correction_db_applied
        w = Measurement.duration_seconds
        energy = w * func.power(_TEN, corrected / _TEN)
        mine_cond = Measurement.user_id == user.id
        for h, n, mn, dev, du, en, mdu, men in (await db.execute(
                select(Measurement.h3_index, func.count(), func.sum(case((mine_cond, 1), else_=0)),
                       func.count(func.distinct(Measurement.contributor_hash)), func.sum(w), func.sum(energy),
                       func.sum(case((mine_cond, w), else_=0)), func.sum(case((mine_cond, energy), else_=0)))
                .where(Measurement.tenant_id == user.tenant_id, Measurement.h3_index.in_(list(mine)),
                       Measurement.measured_at >= start, Measurement.measured_at < end,
                       Measurement.quality_status.in_(statuses(include_indicative)))
                .group_by(Measurement.h3_index))).all():
            agg[h] = (int(n), int(mn or 0), int(dev), float(du or 0), float(en or 0), float(mdu or 0), float(men or 0))

    cells, on_map, visible_cells = [], 0, 0
    for h, c in mine.items():
        n, mn, dev, du, en, mdu, men = agg.get(h, (0, 0, 0, 0.0, 0.0, 0.0, 0.0))
        visible = n > 0 and dev >= min_devices
        map_db = _db(en, du) if visible else None
        without = _db(en - men, du - mdu) if visible else None
        lat, lon = cell_center(h)
        if visible:
            visible_cells += 1
            on_map += c["v"]
        cells.append({
            "h3_index": h, "polygon": cell_polygon(h), "center": {"lat": lat, "lon": lon},
            "my_count": c["n"], "my_validated": c["v"], "my_laeq_db": round(10.0 * math.log10(c["e"] / c["n"]), 1),
            "visible": visible,
            "map_laeq_db": None if map_db is None else round(map_db, 1),
            "map_count": n if visible else None, "map_devices": dev if visible else None,
            "my_share": round(mn / n, 3) if visible and n else None,
            "delta_db": None if map_db is None or without is None else round(map_db - without, 2)})
    cells.sort(key=lambda c: c["my_count"], reverse=True)
    d_from, d_to = start.date(), end.date()
    return {"summary": {"total": len(rows), "by_status": by_status, "cells": len(cells),
                        "visible_cells": visible_cells, "on_map": on_map, "min_devices": min_devices,
                        "period": {"from": d_from.isoformat(), "to": d_to.isoformat()}},
            "points": points, "cells": cells}