import time

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis

from app.core.deps import Principal, get_principal
from app.core.redis import get_redis
from app.core.security import hash_ip


async def _hit(redis: Redis, key: str, limit: int) -> None:
    window = int(time.time() // 60)
    full = f"{key}:{window}"
    pipe = redis.pipeline()
    pipe.incr(full)
    pipe.expire(full, 70)
    count, _ = await pipe.execute()
    if count > limit:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited",
                            headers={"Retry-After": str(60 - int(time.time() % 60))})


def limit_by_ip(name: str, per_minute: int):
    async def _dep(request: Request, redis: Redis = Depends(get_redis)) -> None:
        ip = request.client.host if request.client else "unknown"
        await _hit(redis, f"rl:{name}:ip:{hash_ip(ip)}", per_minute)
    return _dep


def limit_by_user(name: str, per_minute: int):
    async def _dep(principal: Principal = Depends(get_principal), redis: Redis = Depends(get_redis)) -> None:
        await _hit(redis, f"rl:{name}:u:{principal.tenant_id}:{principal.user_id}", per_minute)
    return _dep