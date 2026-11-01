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
from app.db.session import engine
from app.routers import (admin, ai, auth, collection, devices, health, map as map_router, measurements,
                         platform, reports, statistics, users)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    yield
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

    # Fusionne les origines de settings + celles codées en dur (frontend local + prod)
    extra_origins = [
        "https://soundmaps.rhmanager.site",
        "http://localhost:8093",
        "http://0.0.0.0:8093",
        "http://127.0.0.1:8093",
        "http://localhost:5173",
    ]
    allowed_origins = list({*settings.cors_origins_list, *extra_origins})

    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,       # ← un seul allow_origins
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
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
        response.headers["Cache-Control"] = "no-store"
        if prod:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response

    prefix = settings.API_V1_PREFIX
    app.include_router(health.router)
    for r in (platform, auth, users, devices, collection, measurements, map_router,
              statistics, admin, ai, reports):
        app.include_router(r.router, prefix=prefix)

    Instrumentator().instrument(app)
    return app


app = create_app()