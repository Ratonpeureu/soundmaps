import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.core.config import settings
from app.core.logging import configure_logging
from app.core.redis import close_redis
from app.core.road_settings import road_settings
from app.db.session import engine
from app.routers import (account, admin, ai, auth, collection, devices, health, leaderboard, map as map_router,
                         measurements, missions, platform, quota, reports, reviews, roads, statistics, tomtom,
                         users, validation)

from app.services.tomtom import close_tomtom
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    if road_settings.ROAD_AUTO_IMPORT:
        try:                                     # import initial du réseau routier (idempotent, verrouillé côté worker)
            celery_app.send_task("roads.ensure_network", countdown=20)
        except Exception:
            log.exception("roads_bootstrap_failed")
    yield
    await close_tomtom()
    await close_redis()
    await engine.dispose()


def create_app() -> FastAPI:
    prod = settings.APP_ENV == "production"
    app = FastAPI(
        title=settings.APP_NAME,
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None if prod else "/docs",
        redoc_url=None,
        openapi_url=None if prod else "/openapi.json",
    )

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts_list)

    extra_origins = [
        "https://soundmaps.rhmanager.site",
        "http://localhost:8093",
        "http://localhost:8097",
        "http://localhost:8000",
        "http://0.0.0.0:8093",
        "http://127.0.0.1:8093",
        "http://localhost:5173",
    ]
    allowed_origins = list({*settings.cors_origins_list, *extra_origins})

    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "If-None-Match"],
            expose_headers=["ETag"],
        )

    @app.middleware("http")
    async def secure(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.MAX_BODY_BYTES:
            return JSONResponse({"detail": "payload_too_large"}, status_code=413)
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Cache-Control", "no-store")     # une route peut fixer sa propre politique
        if prod:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response

    prefix = settings.API_V1_PREFIX
    app.include_router(health.router)
    for r in (platform, auth, users, devices, collection, measurements, map_router, statistics, admin, ai, tomtom,
              reports, quota, missions, validation, account, leaderboard, reviews, roads):
        app.include_router(r.router, prefix=prefix)
    app.include_router(roads.platform_router, prefix=prefix)

    Instrumentator().instrument(app)
    return app


app = create_app()