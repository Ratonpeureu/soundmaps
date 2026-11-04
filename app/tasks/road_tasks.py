from app.core.redis import new_redis
from app.core.road_settings import road_settings as rs
from app.services import roads as roads_service
from app.tasks.celery_app import celery_app
from app.tasks.runtime import for_each_tenant, run, task_db

_LOCK = "roads:overpass:lock"


@celery_app.task(name="roads.ensure_network")
def ensure_network(force: bool = False):
    """Planifie l'import initial (une seule fois) puis relance les tuiles manquantes, échouées ou bloquées."""
    async def _job():
        redis = new_redis()
        try:
            if not await redis.set("roads:ensure:lock", "1", nx=True, ex=120):
                return {"status": "locked", "queued": []}
            async with task_db() as db:
                return await roads_service.ensure_network(db, redis, force)
        finally:
            await redis.aclose()
    res = run(_job())
    keys = res.get("queued", [])
    for i, key in enumerate(keys):
        import_tile.apply_async(args=[key], countdown=i * rs.ROAD_TILE_SPACING_SECONDS)
    return {**{k: v for k, v in res.items() if k != "queued"}, "queued": len(keys)}


@celery_app.task(name="roads.import_tile")
def import_tile(key: str):
    """Importe une tuile Overpass (une seule requête Overpass à la fois, toutes instances confondues)."""
    async def _job():
        redis = new_redis()
        try:
            if not await redis.set(_LOCK, "1", nx=True, ex=290):
                return {"deferred": True}
            try:
                async with task_db() as db:
                    return await roads_service.run_tile(db, redis, key)
            finally:
                await redis.delete(_LOCK)
        finally:
            await redis.aclose()
    res = run(_job())
    if res.get("deferred"):
        import_tile.apply_async(args=[key], countdown=15)
    if res.get("retry_in"):
        import_tile.apply_async(args=[key], countdown=res["retry_in"])
    for i, child in enumerate(res.get("children", [])):
        import_tile.apply_async(args=[child], countdown=rs.ROAD_TILE_SPACING_SECONDS * (i + 1))
    return {k: v for k, v in res.items() if k != "children"}


@celery_app.task(name="roads.match")
def match():
    """Rattache les mesures au tronçon de route le plus proche (pour chaque tenant)."""
    async def _job(db, tid):
        return await roads_service.match_measurements(db, tid)
    return run(for_each_tenant(_job))