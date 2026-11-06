import hashlib
import json
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
import jwt
from fastapi import HTTPException
from redis.asyncio import Redis

from app.core.config import settings
from app.core.security import decode_token

log = logging.getLogger(__name__)

# couches de tuiles autorisées (liste blanche : le front ne choisit jamais une URL TomTom)
TILE_LAYERS = {
    "flow": "/traffic/map/4/tile/flow/relative/{z}/{x}/{y}.png",
    "incidents": "/traffic/map/4/tile/incidents/s3/{z}/{x}/{y}.png",
}

_http: httpx.AsyncClient | None = None


class TomTomUnavailable(HTTPException):
    def __init__(self, detail: str = "tomtom_unavailable"):
        super().__init__(503, detail)


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(base_url=settings.TOMTOM_BASE_URL, timeout=settings.TOMTOM_TIMEOUT_SECONDS,
                                  limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
    return _http


async def close_tomtom() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


def _key() -> str:
    key = settings.TOMTOM_API_KEY.get_secret_value()
    if not key:
        raise TomTomUnavailable("tomtom_not_configured")
    return key


async def _request(path: str, params: dict | None = None, allow: tuple[int, ...] = ()) -> httpx.Response:
    key = _key()
    try:
        resp = await _client().get(path, params={**(params or {}), "key": key})
    except httpx.HTTPError:
        # on ne propage jamais l'exception httpx : son message contient l'URL donc la clé
        raise TomTomUnavailable("tomtom_unreachable")
    if resp.status_code in allow:
        return resp
    if resp.status_code in (401, 403):
        log.error("tomtom_key_rejected", extra={"task": path.split("/")[1] if "/" in path else path})
        raise TomTomUnavailable("tomtom_key_rejected")
    if resp.status_code == 429:
        raise TomTomUnavailable("tomtom_rate_limited")
    if resp.status_code >= 400:
        raise TomTomUnavailable("tomtom_upstream_error")
    return resp


async def _cached_json(redis: Redis, raw_key: str, ttl: int, loader):
    k = "tt:" + hashlib.sha256(raw_key.encode()).hexdigest()
    hit = await redis.get(k)
    if hit is not None:
        return json.loads(hit)
    data = await loader()
    await redis.set(k, json.dumps(data, ensure_ascii=False), ex=ttl)
    return data


# ------------------------------------------------------------------ tuiles
def issue_tile_session(user_id: uuid.UUID, tenant_id: uuid.UUID) -> dict:
    """Jeton opaque, court, limité aux tuiles (typ=tile : inutilisable comme access token)."""
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=settings.TOMTOM_TILE_TOKEN_TTL_SECONDS)
    token = jwt.encode(
        {"sub": str(user_id), "tid": str(tenant_id), "typ": "tile", "jti": uuid.uuid4().hex, "iat": now, "exp": exp},
        settings.JWT_SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM)
    prefix = settings.API_V1_PREFIX
    return {"expires_at": exp,
            "layers": {name: f"{prefix}/tomtom/tiles/{name}/{{z}}/{{x}}/{{y}}.png?t={token}" for name in TILE_LAYERS}}


def verify_tile_token(token: str) -> dict:
    try:
        return decode_token(token, "tile")
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid_token")


async def fetch_tile(layer: str, z: int, x: int, y: int) -> bytes | None:
    template = TILE_LAYERS.get(layer)
    if template is None:
        raise HTTPException(404, "unknown_layer")
    if not (0 <= z <= 22) or not (0 <= x < 2 ** z) or not (0 <= y < 2 ** z):
        raise HTTPException(422, "invalid_tile")
    resp = await _request(template.format(z=z, x=x, y=y), allow=(204, 404))
    return resp.content if resp.status_code == 200 else None


# ------------------------------------------------------------------ trafic
async def flow_point(redis: Redis, lat: float, lon: float) -> dict:
    async def load():
        resp = await _request("/traffic/services/4/flowSegmentData/absolute/10/json",
                              {"point": f"{lat},{lon}", "unit": "KMPH"}, allow=(400,))
        if resp.status_code == 400:                 # point trop éloigné d'une route
            return None
        f = resp.json()["flowSegmentData"]
        cur, free = float(f.get("currentSpeed", 0)), float(f.get("freeFlowSpeed", 0))
        congestion = max(0.0, min(1.0, 1 - cur / free)) if free > 0 else 0.0
        return {"current_speed_kmh": cur, "free_flow_speed_kmh": free,
                "current_travel_time_s": f.get("currentTravelTime"),
                "free_flow_travel_time_s": f.get("freeFlowTravelTime"),
                "confidence": f.get("confidence"), "road_closure": bool(f.get("roadClosure", False)),
                "congestion": round(congestion, 3),
                "level": "free" if congestion < 0.2 else "moderate" if congestion < 0.5 else "heavy"}

    data = await _cached_json(redis, f"flow:{lat:.4f}:{lon:.4f}", settings.TOMTOM_CACHE_TTL_SECONDS, load)
    if data is None:
        raise HTTPException(404, "no_traffic_data")
    return data


_INCIDENT_FIELDS = ("{incidents{type,geometry{type,coordinates},properties{id,iconCategory,magnitudeOfDelay,"
                    "events{description,code,iconCategory},startTime,endTime,from,to,length,delay,roadNumbers}}}")


async def incidents(redis: Redis, bbox: tuple[float, float, float, float]) -> list[dict]:
    min_lon, min_lat, max_lon, max_lat = bbox
    if max_lon - min_lon > 0.8 or max_lat - min_lat > 0.8:
        raise HTTPException(422, "bbox_too_large")
    # bbox calée sur une grille de 0,05° : meilleur taux de cache, requête identique à la clé
    snap_lo = lambda v: math.floor(v * 20) / 20
    snap_hi = lambda v: math.ceil(v * 20) / 20
    b = (snap_lo(min_lon), snap_lo(min_lat), snap_hi(max_lon), snap_hi(max_lat))

    async def load():
        resp = await _request("/traffic/services/5/incidentDetails", {
            "bbox": f"{b[0]},{b[1]},{b[2]},{b[3]}", "fields": _INCIDENT_FIELDS,
            "language": settings.TOMTOM_LANGUAGE, "timeValidityFilter": "present"})
        out = []
        for f in resp.json().get("incidents", []):
            g, p = f.get("geometry") or {}, f.get("properties") or {}
            coords = g.get("coordinates") or []
            line = [coords] if g.get("type") == "Point" and coords else coords
            line = [[c[1], c[0]] for c in line if isinstance(c, list) and len(c) >= 2]
            if not line:
                continue
            mid = line[len(line) // 2]
            events = p.get("events") or []
            out.append({"id": p.get("id"), "category": p.get("iconCategory"),
                        "delay_magnitude": p.get("magnitudeOfDelay"),
                        "description": events[0].get("description") if events else None,
                        "from": p.get("from"), "to": p.get("to"), "delay_s": p.get("delay"),
                        "length_m": p.get("length"), "road_numbers": p.get("roadNumbers") or [],
                        "start": p.get("startTime"), "end": p.get("endTime"), "lat": mid[0], "lon": mid[1]})
        return out

    return await _cached_json(redis, "inc:" + ":".join(f"{v:.2f}" for v in b),
                              settings.TOMTOM_CACHE_TTL_SECONDS, load)


# ------------------------------------------------------------------ recherche / géocodage
async def search(redis: Redis, query: str, lat: float | None, lon: float | None, limit: int) -> list[dict]:
    q = query.strip()
    params = {"limit": limit, "language": settings.TOMTOM_LANGUAGE, "countrySet": settings.TOMTOM_COUNTRY_SET}
    if lat is not None and lon is not None:
        params.update({"lat": round(lat, 3), "lon": round(lon, 3)})

    async def load():
        resp = await _request(f"/search/2/search/{quote(q, safe='')}.json", params)
        out = []
        for r in resp.json().get("results", []):
            pos, addr, poi = r.get("position") or {}, r.get("address") or {}, r.get("poi") or {}
            if "lat" not in pos or "lon" not in pos:
                continue
            out.append({"id": r.get("id"), "name": poi.get("name") or addr.get("freeformAddress") or q,
                        "address": addr.get("freeformAddress"), "municipality": addr.get("municipality"),
                        "category": (poi.get("categories") or [None])[0], "lat": pos["lat"], "lon": pos["lon"],
                        "distance_m": r.get("dist")})
        return out

    return await _cached_json(redis, f"s:{q.lower()}:{params.get('lat')}:{params.get('lon')}:{limit}",
                              settings.TOMTOM_SEARCH_CACHE_TTL_SECONDS, load)


async def reverse_geocode(redis: Redis, lat: float, lon: float) -> dict:
    async def load():
        resp = await _request(f"/search/2/reverseGeocode/{lat:.5f},{lon:.5f}.json",
                              {"language": settings.TOMTOM_LANGUAGE}, allow=(400, 404))
        if resp.status_code != 200:
            return None
        addrs = resp.json().get("addresses") or []
        if not addrs:
            return None
        a = addrs[0].get("address") or {}
        return {"address": a.get("freeformAddress"), "street": a.get("streetName"),
                "municipality": a.get("municipality"), "country": a.get("country")}

    data = await _cached_json(redis, f"rev:{lat:.4f}:{lon:.4f}", settings.TOMTOM_SEARCH_CACHE_TTL_SECONDS, load)
    if data is None:
        raise HTTPException(404, "address_not_found")
    return data


# ------------------------------------------------------------------ itinéraire
def _hav_m(la1: float, lo1: float, la2: float, lo2: float) -> float:
    p1, p2 = math.radians(la1), math.radians(la2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(a))


def _length(pts: list) -> float:
    return sum(_hav_m(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:]))


def _trim_points(pts: list, lat: float, lon: float, radius_m: float) -> tuple[list, float]:
    """Coupe l'itinéraire dès qu'il entre dans le cercle de tolérance de la cible (plus de boucle ni de dépassement)."""
    if len(pts) < 2:
        return pts, 1.0
    stop = max(15.0, radius_m * 0.6)
    cos_l = math.cos(math.radians(lat))

    def xy(p):
        return (p[1] - lon) * 111320.0 * cos_l, (p[0] - lat) * 110574.0

    for i in range(len(pts) - 1):
        ax, ay = xy(pts[i])
        bx, by = xy(pts[i + 1])
        t = None
        if ax * ax + ay * ay <= stop * stop:
            t = 0.0
        else:
            dx, dy = bx - ax, by - ay
            a = dx * dx + dy * dy
            if a > 0:
                b = 2 * (ax * dx + ay * dy)
                c = ax * ax + ay * ay - stop * stop
                disc = b * b - 4 * a * c
                if disc >= 0:
                    tt = (-b - math.sqrt(disc)) / (2 * a)
                    if 0 <= tt <= 1:
                        t = tt
        if t is None:
            continue
        out = pts[:i + 1]
        if t > 0:
            out.append([pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t])
        if len(out) < 2:
            out = [pts[0], pts[0]]
        full = _length(pts) or 1.0
        return out, min(1.0, _length(out) / full)
    return pts, 1.0


async def route(o_lat: float, o_lon: float, d_lat: float, d_lon: float, mode: str, *,
                shortest: bool = False, stop_radius_m: float | None = None) -> dict:
    params = {"travelMode": mode, "routeType": "shortest" if shortest else "fastest",
              "language": settings.TOMTOM_LANGUAGE}
    if shortest:
        params["traffic"] = "false"          # chemin le plus court, indépendant du trafic
    else:
        params.update({"traffic": "true", "computeTravelTimeFor": "all"})
    resp = await _request(f"/routing/1/calculateRoute/{o_lat:.6f},{o_lon:.6f}:{d_lat:.6f},{d_lon:.6f}/json",
                          params, allow=(400,))
    if resp.status_code == 400:
        raise HTTPException(404, "route_not_found")
    routes = resp.json().get("routes") or []
    if not routes:
        raise HTTPException(404, "route_not_found")
    r = routes[0]
    pts = [[p["latitude"], p["longitude"]] for leg in r.get("legs", []) for p in leg.get("points", [])]
    ratio = 1.0
    if stop_radius_m is not None:
        pts, ratio = _trim_points(pts, d_lat, d_lon, stop_radius_m)
    step = max(1, len(pts) // 800)
    pts = pts[::step] + ([pts[-1]] if pts and (len(pts) - 1) % step else [])
    s = r.get("summary") or {}
    length, tt, delay = s.get("lengthInMeters"), s.get("travelTimeInSeconds"), s.get("trafficDelayInSeconds", 0)
    return {"length_m": None if length is None else int(length * ratio),
            "travel_time_s": None if tt is None else int(tt * ratio),
            "traffic_delay_s": int((delay or 0) * ratio), "departure": s.get("departureTime"),
            "arrival": s.get("arrivalTime"), "points": pts}











