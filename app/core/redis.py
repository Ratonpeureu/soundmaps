from redis.asyncio import Redis

from app.core.config import settings

_client: Redis | None = None


def new_redis() -> Redis:
    return Redis.from_url(settings.REDIS_URL, decode_responses=True)


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = new_redis()
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None