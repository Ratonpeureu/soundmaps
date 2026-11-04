import json
import math

import httpx
from redis.asyncio import Redis

from app.core.field_settings import field_settings as fs


async def wind_at(redis: Redis, lat: float, lon: float) -> dict | None:
    """Vent courant (Open-Meteo, sans clé), calé sur 0,1° et mis en cache : identique pour tous les utilisateurs."""
    la, lo = round(lat, 1), round(lon, 1)
    key = f"wx:{la}:{lo}"
    hit = await redis.get(key)
    if hit:
        return json.loads(hit)
    if await redis.exists(key + ":fail"):
        return None
    try:
        async with httpx.AsyncClient(timeout=fs.WIND_TIMEOUT_SECONDS) as client:
            r = await client.get(fs.OPEN_METEO_URL, params={
                "latitude": la, "longitude": lo, "current": "wind_speed_10m,wind_direction_10m",
                "wind_speed_unit": "kmh", "timezone": "auto"})
        r.raise_for_status()
        cur = r.json()["current"]
        speed_kmh, dir_from = float(cur["wind_speed_10m"]), float(cur["wind_direction_10m"])
    except Exception:
        await redis.set(key + ":fail", "1", ex=60)
        return None
    to = math.radians(dir_from + 180.0)       # le vent souffle vers dir_from + 180
    ms = speed_kmh / 3.6
    data = {"speed_kmh": round(speed_kmh, 1), "speed_ms": ms, "dir_from": dir_from,
            "dir_to": (dir_from + 180.0) % 360.0, "u": math.sin(to) * ms, "v": math.cos(to) * ms,
            "observed_at": cur.get("time")}
    await redis.set(key, json.dumps(data), ex=fs.WIND_CACHE_TTL_SECONDS)
    return data