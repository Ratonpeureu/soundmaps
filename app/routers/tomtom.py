from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from redis.asyncio import Redis

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.rate_limit import _hit, limit_by_user
from app.core.redis import get_redis
from app.models.tenancy import User
from app.services import tomtom as tt
from app.services.geo import cell_center, is_valid_cell

router = APIRouter(prefix="/tomtom", tags=["tomtom"])
_rl = Depends(limit_by_user("tomtom", settings.RATE_LIMIT_TOMTOM_PER_MINUTE))


class TileSessionOut(BaseModel):
    expires_at: datetime
    layers: dict[str, str]


class TrafficFlowOut(BaseModel):
    current_speed_kmh: float
    free_flow_speed_kmh: float
    current_travel_time_s: int | None = None
    free_flow_travel_time_s: int | None = None
    confidence: float | None = None
    road_closure: bool
    congestion: float
    level: Literal["free", "moderate", "heavy"]


class IncidentOut(BaseModel):
    id: str | None = None
    category: int | None = None
    delay_magnitude: int | None = None
    description: str | None = None
    from_: str | None = None
    to: str | None = None
    delay_s: int | None = None
    length_m: int | None = None
    road_numbers: list[str] = []
    start: str | None = None
    end: str | None = None
    lat: float
    lon: float


class PlaceOut(BaseModel):
    id: str | None = None
    name: str
    address: str | None = None
    municipality: str | None = None
    category: str | None = None
    lat: float
    lon: float
    distance_m: float | None = None


class AddressOut(BaseModel):
    address: str | None = None
    street: str | None = None
    municipality: str | None = None
    country: str | None = None


class RouteOut(BaseModel):
    length_m: int | None = None
    travel_time_s: int | None = None
    traffic_delay_s: int | None = None
    departure: str | None = None
    arrival: str | None = None
    points: list[list[float]]


def _parse_bbox(bbox: str) -> tuple[float, float, float, float]:
    try:
        a = tuple(float(x) for x in bbox.split(","))
        assert len(a) == 4 and -180 <= a[0] < a[2] <= 180 and -90 <= a[1] < a[3] <= 90
        return a
    except (ValueError, AssertionError):
        raise HTTPException(422, "invalid_bbox")


@router.get("/tile-session", response_model=TileSessionOut, dependencies=[_rl])
async def tile_session(user: User = Depends(get_current_user)):
    return tt.issue_tile_session(user.id, user.tenant_id)


@router.get("/tiles/{layer}/{z}/{x}/{y}.png", include_in_schema=False)
async def tile(layer: str, z: int, x: int, y: int, t: str = Query(..., min_length=20),
               redis: Redis = Depends(get_redis)):
    claims = tt.verify_tile_token(t)
    await _hit(redis, f"rl:tiles:{claims['sub']}", settings.RATE_LIMIT_TILES_PER_MINUTE)
    content = await tt.fetch_tile(layer, z, x, y)
    if content is None:
        return Response(status_code=204)
    return Response(content, media_type="image/png", headers={"Cache-Control": "private, max-age=60"})


@router.get("/traffic/flow", response_model=TrafficFlowOut, dependencies=[_rl])
async def traffic_flow(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
                       user: User = Depends(get_current_user), redis: Redis = Depends(get_redis)):
    return await tt.flow_point(redis, lat, lon)


@router.get("/traffic/incidents", response_model=list[IncidentOut], dependencies=[_rl])
async def traffic_incidents(bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat"),
                            user: User = Depends(get_current_user), redis: Redis = Depends(get_redis)):
    items = await tt.incidents(redis, _parse_bbox(bbox))
    return [{**i, "from_": i.get("from")} for i in items]


@router.get("/cells/{h3_index}/traffic", response_model=TrafficFlowOut, dependencies=[_rl])
async def cell_traffic(h3_index: str, user: User = Depends(get_current_user), redis: Redis = Depends(get_redis)):
    if not is_valid_cell(h3_index):
        raise HTTPException(422, "invalid_h3_index")
    lat, lon = cell_center(h3_index)
    return await tt.flow_point(redis, lat, lon)


@router.get("/search", response_model=list[PlaceOut], dependencies=[_rl])
async def search(q: str = Query(..., min_length=2, max_length=120), lat: float | None = Query(None, ge=-90, le=90),
                 lon: float | None = Query(None, ge=-180, le=180), limit: int = Query(8, ge=1, le=20),
                 user: User = Depends(get_current_user), redis: Redis = Depends(get_redis)):
    return await tt.search(redis, q, lat, lon, limit)


@router.get("/reverse", response_model=AddressOut, dependencies=[_rl])
async def reverse(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
                  user: User = Depends(get_current_user), redis: Redis = Depends(get_redis)):
    return await tt.reverse_geocode(redis, lat, lon)


@router.get("/route", response_model=RouteOut, dependencies=[_rl])
async def route(o_lat: float = Query(..., ge=-90, le=90), o_lon: float = Query(..., ge=-180, le=180),
                d_lat: float = Query(..., ge=-90, le=90), d_lon: float = Query(..., ge=-180, le=180),
                mode: Literal["car", "pedestrian", "bicycle", "motorcycle", "taxi", "bus"] = "car",
                user: User = Depends(get_current_user)):
    return await tt.route(o_lat, o_lon, d_lat, d_lon, mode)