import hashlib
import json
import math
import statistics
from datetime import datetime, timedelta, timezone

import h3 as h3lib
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.survey_settings import survey_settings as sv
from app.models.enums import QualityStatus
from app.models.geo import GeographicCell
from app.models.survey import ReferenceValue, Survey, SurveyAnswer
from app.services import catalog
from app.services.geo import cell_center, cell_polygon

CAVEAT = "Échantillon participatif : indicatif, ce n'est pas un recensement."


# ------------------------------------------------------------------ helpers

def res_for_zoom(z: float | None) -> int:
    if z is None:
        return sv.SM_DEFAULT_RES
    return 5 if z < 8 else 6 if z < 9.5 else 7 if z < 11 else 8 if z < 13 else 9


def parent(h: str, res: int) -> str:
    return h if res >= settings.H3_RESOLUTION else h3lib.cell_to_parent(h, res)


def children(h: str) -> list[str]:
    r = h3lib.get_resolution(h)
    return [h] if r >= settings.H3_RESOLUTION else list(h3lib.cell_to_children(h, settings.H3_RESOLUTION))


def resolve(key: str):
    if key in catalog.INDICATORS:
        return catalog.INDICATORS[key], None
    if key in catalog.DERIVED:
        return None, catalog.DERIVED[key]
    raise HTTPException(404, "unknown_indicator")


def k_min(ind: catalog.Indicator) -> int:
    if ind.unit_of == "facility":
        return sv.SM_MIN_CONTRIBUTORS_FACILITY
    return sv.SM_MIN_CONTRIBUTORS_SENSITIVE if ind.sensitive else sv.SM_MIN_CONTRIBUTORS


def metrics_for(ind: catalog.Indicator) -> list[str]:
    if ind.kind == "number":
        return ["mean", "sum"]
    if ind.kind == "share":
        return ["share"]
    if ind.kind == "scale":
        return ["mean"]
    out = ["top", *[f"cat:{c.code}" for c in ind.cats]]
    return out + (["mean"] if any(c.num is not None for c in ind.cats) else [])


def default_metric(ind: catalog.Indicator | None) -> str:
    if ind is None:
        return "ratio"
    return {"number": "mean", "share": "share", "scale": "mean", "category": "top"}[ind.kind]


def confidence(n: int, contribs: int, last: datetime, proved: float, now: datetime) -> float:
    age = max(0.0, (now - last).total_seconds() / 86400)
    return round(0.4 * min(1.0, n / sv.SM_TARGET_N) + 0.3 * min(1.0, contribs / sv.SM_TARGET_CONTRIBUTORS)
                 + 0.2 * math.exp(-age / sv.SM_RECENCY_TAU_DAYS) + 0.1 * proved, 3)


# ------------------------------------------------------------------ lecture et regroupement

async def _fetch(db: AsyncSession, tenant_id, key: str, *, mode: str, since, until, stats, bbox=None, margin=0.0,
                 only: list[str] | None = None, bucket: bool = False) -> list[tuple]:
    A = SurveyAnswer
    ind = catalog.INDICATORS[key]
    conds = [A.tenant_id == tenant_id, A.indicator_key == key, A.status.in_(stats), A.answered_at >= since,
             A.answered_at < until]
    cols, group = [A.h3_index, A.contributor_hash], [A.h3_index, A.contributor_hash]
    if mode == "cat":
        cols.append(A.value_cat)
        group.append(A.value_cat)
    elif ind.kind == "category":
        conds += [A.value_cat != "__all__", A.value_num.is_not(None)]
    else:
        conds.append(A.value_num.is_not(None))
    if bucket:
        b = func.date_trunc("month", A.answered_at)
        cols.append(b)
        group.append(b)
    vn = func.coalesce(A.value_num, 0.0)
    stmt = select(*cols, func.count(), func.sum(A.weight), func.sum(A.weight * vn), func.sum(vn),
                  func.sum(case((A.proved.is_(True), 1), else_=0)), func.max(A.answered_at)).where(*conds).group_by(*group)
    if bbox:
        stmt = stmt.join(GeographicCell, GeographicCell.id == A.cell_id).where(
            func.ST_Intersects(GeographicCell.center, func.ST_MakeEnvelope(
                bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin, 4326)))
    if only is not None:
        stmt = stmt.where(A.h3_index.in_(only))
    return (await db.execute(stmt.limit(sv.SM_MAX_ROWS))).all()


def _roll(rows: list[tuple], mode: str, res: int, bucket: bool, metric: str) -> dict:
    cells: dict = {}
    for r in rows:
        i, cat, bk = 2, None, None
        if mode == "cat":
            cat = r[i]
            i += 1
        if bucket:
            bk = r[i]
            i += 1
        n, w, wv, raw, proved, last = r[i:i + 6]
        c = cells.setdefault((parent(r[0], res), bk), {"con": {}, "n": 0, "proved": 0, "last": last})
        c["last"] = max(c["last"], last)
        if mode == "num":
            c["n"] += n
            c["proved"] += int(proved or 0)
            x = c["con"].setdefault(r[1], [0.0, 0.0, 0.0])
            x[0] += float(w or 0)
            x[1] += float(wv or 0)
            x[2] += float(raw or 0)
        else:
            x = c["con"].setdefault(r[1], {})
            x[cat] = x.get(cat, 0.0) + float(w or 0)
            if cat == "__all__":
                c["n"] += n
                c["proved"] += int(proved or 0)
    out = {}
    for k, c in cells.items():
        if mode == "num":
            tw = tv = raw = 0.0
            ncon = 0
            for w, wv, rw in c["con"].values():
                if w <= 0:
                    continue
                cw = min(w, 1.0)
                tw += cw
                tv += cw * (wv / w)
                raw += rw
                ncon += 1
            if tw <= 0:
                continue
            out[k] = {"value": raw if metric == "sum" else tv / tw, "n": c["n"], "contributors": ncon,
                      "last": c["last"], "proved": c["proved"] / max(1, c["n"])}
        else:
            dist: dict = {}
            tw = 0.0
            ncon = 0
            for d in c["con"].values():
                allw = d.get("__all__", 0.0)
                if allw <= 0:
                    continue
                cw = min(allw, 1.0)
                tw += cw
                ncon += 1
                for cat, w in d.items():
                    if cat != "__all__":
                        dist[cat] = dist.get(cat, 0.0) + cw * min(1.0, w / allw)
            if tw <= 0:
                continue
            dist = {a: b / tw for a, b in dist.items()}
            out[k] = {"dist": dist, "n": c["n"], "contributors": ncon, "last": c["last"],
                      "proved": c["proved"] / max(1, c["n"])}
    return out


async def _cells_for(db, tenant_id, key, metric, res, *, since, until, stats, bbox=None, margin=0.0, only=None,
                     bucket=False) -> dict:
    ind = catalog.INDICATORS[key]
    use_cat = ind.kind == "category" and metric != "mean"
    rows = await _fetch(db, tenant_id, key, mode="cat" if use_cat else "num", since=since, until=until, stats=stats,
                        bbox=bbox, margin=margin, only=only, bucket=bucket)
    cells = _roll(rows, "cat" if use_cat else "num", res, bucket, metric)
    if use_cat:
        for c in cells.values():
            if metric == "top":
                top = max(c["dist"].items(), key=lambda t: t[1], default=(None, 0))
                c["top"], c["value"] = top[0], top[1]
            else:
                c["top"], c["value"] = None, c["dist"].get(metric.split(":", 1)[1], 0.0)
    return cells


async def _derived_cells(db, tenant_id, dv: catalog.Derived, res, **kw) -> dict:
    comps = {k: await _cells_for(db, tenant_id, k, "mean", res, **kw) for k in {*dv.num, *dv.den}}
    keys = None
    for c in comps.values():
        keys = set(c) if keys is None else keys & set(c)
    out = {}
    for k in keys or ():
        num = sum(comps[x][k]["value"] for x in dv.num)
        den = sum(comps[x][k]["value"] for x in dv.den)
        if den <= 0:
            continue
        parts = [comps[x][k] for x in comps]
        out[k] = {"value": dv.factor * num / den, "n": min(p["n"] for p in parts),
                  "contributors": min(p["contributors"] for p in parts), "last": max(p["last"] for p in parts),
                  "proved": sum(p["proved"] for p in parts) / len(parts)}
    return out


# ------------------------------------------------------------------ références

async def _refs(db, tenant_id, key) -> list[ReferenceValue]:
    return list((await db.execute(select(ReferenceValue).where(
        ReferenceValue.tenant_id == tenant_id, ReferenceValue.indicator_key == key))).scalars())


def _pick_ref(refs, cell: str, res: int, stat: str, category: str | None) -> ReferenceValue | None:
    best, spec = None, -1
    for v in refs:
        if v.stat != stat or (v.category or None) != (category or None):
            continue
        if v.scope_h3 is None:
            s = 0
        elif v.scope_res and v.scope_res <= res and h3lib.cell_to_parent(cell, v.scope_res) == v.scope_h3:
            s = int(v.scope_res)
        else:
            continue
        if s > spec or (s == spec and best and (v.period_year or 0) > (best.period_year or 0)):
            best, spec = v, s
    return best


# ------------------------------------------------------------------ formatage

def _unit_of(ind, dv, metric) -> str:
    if dv:
        return dv.unit
    return "%" if metric in ("share", "top") or metric.startswith("cat:") else ind.unit


def _display(ind, dv, metric, v: float) -> float:
    return v * 100 if (metric in ("share", "top") or metric.startswith("cat:")) else v


def _label(ind, dv, metric, v: float, top: str | None) -> str:
    u = _unit_of(ind, dv, metric)
    d = _display(ind, dv, metric, v)
    txt = f"{d:.0f} %" if u == "%" else f"{d:,.1f} {u}".replace(",", " ").strip()
    if metric == "top" and top:
        name = next((c.label for c in ind.cats if c.code == top), top)
        return f"{name} · {txt}"
    return txt


def _intensity(ind, dv, metric, values: list[float]) -> callable:
    if ind is not None and metric != "sum":
        if ind.kind == "scale":
            return lambda x: max(0.0, min(1.0, (x - 1) / 4))
        if ind.kind == "share" or metric in ("share", "top") or metric.startswith("cat:"):
            return lambda x: max(0.0, min(1.0, x))
    if len(values) >= 2:
        s = sorted(values)
        lo, hi = s[int(0.05 * (len(s) - 1))], s[int(0.95 * (len(s) - 1))]
        if hi > lo:
            return lambda x: max(0.0, min(1.0, (x - lo) / (hi - lo)))
    return lambda x: 0.5


# ------------------------------------------------------------------ carte

def parse_bbox(bbox: str | None):
    if not bbox:
        return None
    try:
        a = tuple(float(x) for x in bbox.split(","))
        assert len(a) == 4 and -180 <= a[0] < a[2] <= 180 and -90 <= a[1] < a[3] <= 90
        if a[2] - a[0] > 12 or a[3] - a[1] > 12:
            raise HTTPException(422, "bbox_too_large")
        return a
    except (ValueError, AssertionError):
        raise HTTPException(422, "invalid_bbox")


def _snap(b, step=0.05):
    if b is None:
        return None
    return (math.floor(b[0] / step) * step, math.floor(b[1] / step) * step,
            math.ceil(b[2] / step) * step, math.ceil(b[3] / step) * step)


async def build_map(db: AsyncSession, redis: Redis, tenant_id, key: str, *, metric: str | None, res: int, days: int,
                    include_indicative: bool, bbox) -> dict:
    ind, dv = resolve(key)
    metric = metric or default_metric(ind)
    if ind is not None and metric not in metrics_for(ind):
        raise HTTPException(422, {"code": "invalid_metric", "allowed": metrics_for(ind)})
    box = _snap(bbox)
    raw = json.dumps([str(tenant_id), key, metric, res, days, include_indicative, box])
    ck = f"t:{tenant_id}:dmap:{hashlib.sha256(raw.encode()).hexdigest()}"
    hit = await redis.get(ck)
    if hit:
        return json.loads(hit)

    now = datetime.now(timezone.utc)
    since, until = now - timedelta(days=days), now + timedelta(minutes=1)
    stats = [QualityStatus.validated] + ([QualityStatus.indicative] if include_indicative else [])
    margin = h3lib.average_hexagon_edge_length(res, unit="km") / 111.0 * 1.5
    kw = dict(since=since, until=until, stats=stats, bbox=box, margin=margin)
    if dv:
        cells = await _derived_cells(db, tenant_id, dv, res, **kw)
        kmin = max(k_min(catalog.INDICATORS[x]) for x in {*dv.num, *dv.den})
        domain = dv.domain
    else:
        cells = await _cells_for(db, tenant_id, key, metric, res, **kw)
        kmin, domain = k_min(ind), ind.domain
    cells = {k[0]: v for k, v in cells.items()}
    visible = {h: c for h, c in cells.items() if c["contributors"] >= kmin}
    hidden = len(cells) - len(visible)
    refs = await _refs(db, tenant_id, key) if ind is not None and metric != "sum" else []
    to_int = _intensity(ind, dv, metric, [c["value"] for c in visible.values()])
    feats, vals = [], []
    for h, c in sorted(visible.items(), key=lambda t: -t[1]["n"])[:sv.SM_MAX_FEATURES]:
        disp = _display(ind, dv, metric, c["value"])
        ref = dev = None
        if refs:
            stat = "mean" if metric == "mean" else "share"
            cat = None
            if metric.startswith("cat:"):
                cat = metric.split(":", 1)[1]
            elif metric == "top":
                cat = c.get("top")
            rv = _pick_ref(refs, h, res, stat, cat)
            if rv:
                ref = rv.value * 100 if stat == "share" else rv.value
                dev = round(disp - ref, 2)
        props = {"h3": h, "value": round(disp, 2), "label": _label(ind, dv, metric, c["value"], c.get("top")),
                 "n": c["n"], "contributors": c["contributors"], "confidence": confidence(c["n"], c["contributors"], c["last"], c["proved"], now),
                 "intensity": round(to_int(c["value"]), 3), "proved_share": round(c["proved"], 2),
                 "last_at": c["last"].isoformat(), "reference": None if ref is None else round(ref, 2),
                 "deviation": dev, "top_category": c.get("top"), "center": dict(zip(("lat", "lon"), cell_center(h)))}
        if c.get("dist"):
            labs = {x.code: x.label for x in ind.cats}
            props["categories"] = [{"code": a, "label": labs.get(a, a), "share": round(b * 100, 1)}
                                   for a, b in sorted(c["dist"].items(), key=lambda t: -t[1])[:6]]
        feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [cell_polygon(h)]},
                      "properties": props})
        vals.append((disp, c["n"]))
    nums = [v for v, _ in vals]
    dom = catalog.DOMAINS[domain]
    out = {
        "type": "FeatureCollection", "features": feats,
        "indicator": {"key": key, "label": dv.label if dv else ind.label, "domain": domain, "domain_label": dom.label,
                      "kind": "derived" if dv else ind.kind, "unit": _unit_of(ind, dv, metric),
                      "polarity": 0 if dv else ind.polarity, "color": dom.color, "sensitive": bool(ind and ind.sensitive)},
        "metric": metric, "metrics": metrics_for(ind) if ind else ["ratio"], "viz": (dv.viz if dv else ind.viz) or dom.viz,
        "period": {"days": days, "include_indicative": include_indicative}, "resolution": res,
        "legend": {"min": min(nums) if nums else None, "max": max(nums) if nums else None,
                   "unit": _unit_of(ind, dv, metric)},
        "stats": {"cells": len(feats), "hidden_cells": hidden, "min_contributors": kmin,
                  "responses": sum(c["n"] for c in visible.values()),
                  "mean": round(sum(v * n for v, n in vals) / max(1, sum(n for _, n in vals)), 2) if vals else None,
                  "median": round(statistics.median(nums), 2) if nums else None,
                  "min": min(nums) if nums else None, "max": max(nums) if nums else None},
        "caveat": CAVEAT + (" Total déclaré par les répondants, pas une estimation de population." if metric == "sum" else ""),
        "generated_at": now.isoformat()}
    await redis.set(ck, json.dumps(out, default=str), ex=sv.SM_CACHE_TTL_SECONDS)
    return out


# ------------------------------------------------------------------ détail d'une zone et évolution

async def cell_detail(db: AsyncSession, tenant_id, h3_index: str, *, domain: str, days: int,
                      include_indicative: bool) -> dict:
    if not h3lib.is_valid_cell(h3_index):
        raise HTTPException(422, "invalid_h3_index")
    res = h3lib.get_resolution(h3_index)
    dom = catalog.DOMAINS.get(domain)
    if dom is None:
        raise HTTPException(404, "unknown_domain")
    now = datetime.now(timezone.utc)
    stats = [QualityStatus.validated] + ([QualityStatus.indicative] if include_indicative else [])
    kw = dict(since=now - timedelta(days=days), until=now + timedelta(minutes=1), stats=stats, only=children(h3_index))
    items = []
    for ind in dom.indicators:
        metric = default_metric(ind)
        c = (await _cells_for(db, tenant_id, ind.key, metric, res, **kw)).get((h3_index, None))
        base = {"indicator": ind.key, "label": ind.label, "kind": ind.kind, "unit": _unit_of(ind, None, metric),
                "metric": metric}
        if c is None:
            continue
        if c["contributors"] < k_min(ind):
            items.append({**base, "hidden": True, "reason": "not_enough_contributors"})
            continue
        item = {**base, "hidden": False, "value": round(_display(ind, None, metric, c["value"]), 2),
                "label_value": _label(ind, None, metric, c["value"], c.get("top")), "n": c["n"],
                "contributors": c["contributors"], "confidence": confidence(c["n"], c["contributors"], c["last"], c["proved"], now)}
        if c.get("dist"):
            labs = {x.code: x.label for x in ind.cats}
            item["categories"] = [{"code": a, "label": labs.get(a, a), "share": round(b * 100, 1)}
                                  for a, b in sorted(c["dist"].items(), key=lambda t: -t[1])[:6]]
        items.append(item)
    for dv in (d for d in catalog.DERIVED.values() if d.domain == domain):
        c = (await _derived_cells(db, tenant_id, dv, res, **kw)).get((h3_index, None))
        if c is None:
            continue
        kmin = max(k_min(catalog.INDICATORS[x]) for x in {*dv.num, *dv.den})
        if c["contributors"] < kmin:
            items.append({"indicator": dv.key, "label": dv.label, "kind": "derived", "hidden": True,
                          "reason": "not_enough_contributors"})
        else:
            items.append({"indicator": dv.key, "label": dv.label, "kind": "derived", "unit": dv.unit, "hidden": False,
                          "value": round(c["value"], 2), "label_value": f"{c['value']:.1f} {dv.unit}",
                          "n": c["n"], "contributors": c["contributors"],
                          "confidence": confidence(c["n"], c["contributors"], c["last"], c["proved"], now)})
    clat, clon = cell_center(h3_index)
    return {"h3": h3_index, "resolution": res, "center": {"lat": clat, "lon": clon}, "polygon": cell_polygon(h3_index),
            "domain": {"key": dom.key, "label": dom.label, "color": dom.color}, "period_days": days,
            "items": items, "caveat": CAVEAT}


async def trend(db: AsyncSession, tenant_id, key: str, h3_index: str | None, *, metric: str | None, months: int,
                include_indicative: bool) -> dict:
    ind, dv = resolve(key)
    if dv:
        raise HTTPException(422, "trend_not_available_for_derived")
    metric = metric or default_metric(ind)
    if metric not in metrics_for(ind):
        raise HTTPException(422, {"code": "invalid_metric", "allowed": metrics_for(ind)})
    now = datetime.now(timezone.utc)
    res = h3lib.get_resolution(h3_index) if h3_index else 0
    stats = [QualityStatus.validated] + ([QualityStatus.indicative] if include_indicative else [])
    cells = await _cells_for(db, tenant_id, key, metric, res if h3_index else 5, since=now - timedelta(days=31 * months),
                             until=now + timedelta(minutes=1), stats=stats, only=children(h3_index) if h3_index else None,
                             bucket=True)
    agg: dict = {}
    for (p, bk), c in cells.items():
        if h3_index and p != h3_index:
            continue
        a = agg.setdefault(bk, [0.0, 0, 0])
        a[0] += c["value"] * c["n"]
        a[1] += c["n"]
        a[2] += c["contributors"]
    kmin = k_min(ind)
    pts = [{"month": bk.date().isoformat() if hasattr(bk, "date") else str(bk),
            "value": round(_display(ind, None, metric, s / n), 2), "n": n, "contributors": cn}
           for bk, (s, n, cn) in sorted(agg.items()) if cn >= kmin and n > 0]
    return {"indicator": {"key": key, "label": ind.label, "unit": _unit_of(ind, None, metric)}, "metric": metric,
            "h3": h3_index, "points": pts, "min_contributors": kmin, "caveat": CAVEAT}


# ------------------------------------------------------------------ vue d'ensemble

async def summary(db: AsyncSession, tenant_id, days: int) -> dict:
    A = SurveyAnswer
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(select(A.domain, func.count(func.distinct(A.response_id)), func.count(func.distinct(A.contributor_hash)),
                                    func.count(func.distinct(A.h3_index)), func.max(A.answered_at)).where(
        A.tenant_id == tenant_id, A.status.in_([QualityStatus.validated, QualityStatus.indicative]),
        A.answered_at >= since).group_by(A.domain))).all()
    got = {d: (r, c, z, last) for d, r, c, z, last in rows}
    active = dict((await db.execute(select(Survey.domain, func.count()).where(
        Survey.tenant_id == tenant_id, Survey.status == "active").group_by(Survey.domain))).all())
    out = []
    for d in catalog.DOMAINS.values():
        r, c, z, last = got.get(d.key, (0, 0, 0, None))
        out.append({"key": d.key, "label": d.label, "icon": d.icon, "color": d.color, "summary": d.summary,
                    "viz": d.viz, "responses": r, "contributors": c, "zones": z, "last_at": last,
                    "active_surveys": sum(v for k, v in active.items() if getattr(k, "value", k) == d.key),
                    "n_indicators": len(d.indicators)})
    return {"period_days": days, "domains": out}