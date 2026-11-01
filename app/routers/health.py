from fastapi import APIRouter, Depends, Header, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis
from sqlalchemy import text

import hmac

from app.core.config import settings
from app.core.redis import get_redis
from app.db.session import SessionLocal

router = APIRouter()


@router.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


@router.get("/ready", include_in_schema=False)
async def ready(redis: Redis = Depends(get_redis)):
    try:
        async with SessionLocal() as s:
            await s.execute(text("SELECT 1"))
        await redis.ping()
    except Exception:
        raise HTTPException(503, "not_ready")
    return {"status": "ready"}


@router.get("/metrics", include_in_schema=False)
async def metrics(authorization: str = Header(default="")):
    expected = "Bearer " + settings.METRICS_TOKEN.get_secret_value()
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(403, "forbidden")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)