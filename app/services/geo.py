import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import h3
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.enums import DayPart
from app.models.geo import GeographicCell
from app.schemas.schemas import Period, TimeFilter

TZ = ZoneInfo(settings.TIMEZONE)


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(TZ)


def day_part_of(local: datetime) -> DayPart:
    h = local.hour
    if settings.DAYPART_MORNING_START <= h < settings.DAYPART_AFTERNOON_START:
        return DayPart.morning
    if settings.DAYPART_AFTERNOON_START <= h < settings.DAYPART_EVENING_START:
        return DayPart.afternoon
    if settings.DAYPART_EVENING_START <= h < settings.DAYPART_NIGHT_START:
        return DayPart.evening
    return DayPart.night


def cell_for(lat: float, lon: float) -> str:
    return h3.latlng_to_cell(lat, lon, settings.H3_RESOLUTION)


def cell_center(h3_index: str) -> tuple[float, float]:
    lat, lon = h3.cell_to_latlng(h3_index)
    return lat, lon


def cell_polygon(h3_index: str) -> list[list[float]]:
    ring = [[lng, lat] for lat, lng in h3.cell_to_boundary(h3_index)]
    ring.append(ring[0])
    return ring


def ring(h3_index: str, k: int) -> list[str]:
    return list(h3.grid_disk(h3_index, k))


def is_valid_cell(value: str) -> bool:
    try:
        return h3.is_valid_cell(value)
    except Exception:
        return False


def cell_margin_degrees() -> float:
    return h3.average_hexagon_edge_length(settings.H3_RESOLUTION, unit="km") / 111.0


async def get_or_create_cell(db: AsyncSession, tenant_id: uuid.UUID, h3_index: str) -> GeographicCell:
    lat, lon = cell_center(h3_index)
    stmt = pg_insert(GeographicCell).values(
        id=uuid.uuid4(), tenant_id=tenant_id, h3_index=h3_index, resolution=settings.H3_RESOLUTION,
        center=func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326),
    ).on_conflict_do_nothing(index_elements=["tenant_id", "h3_index"])
    await db.execute(stmt)
    return (await db.execute(select(GeographicCell).where(
        GeographicCell.tenant_id == tenant_id, GeographicCell.h3_index == h3_index))).scalar_one()


def resolve_period(tf: TimeFilter, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Retourne (début, fin) en UTC. fin exclusive."""
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(TZ)
    if tf.period == Period.today:
        start = datetime.combine(local_now.date(), time.min, TZ)
        end = now
    elif tf.period == Period.last_7_days:
        start, end = now - timedelta(days=7), now
    elif tf.period == Period.last_30_days:
        start, end = now - timedelta(days=30), now
    else:
        if tf.date_from is None or tf.date_to is None or tf.date_to < tf.date_from:
            raise HTTPException(422, "custom_period_requires_valid_date_from_and_date_to")
        start = datetime.combine(tf.date_from, time.min, TZ)
        end = datetime.combine(tf.date_to + timedelta(days=1), time.min, TZ)
    if (end - start).days > settings.MAX_RANGE_DAYS:
        raise HTTPException(422, "period_too_long")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def period_dates(start: datetime, end: datetime) -> tuple[date, date]:
    return start.astimezone(TZ).date(), (end - timedelta(microseconds=1)).astimezone(TZ).date()