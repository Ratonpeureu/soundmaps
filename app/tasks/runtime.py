import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from sqlalchemy import select

from app.db.session import task_session_factory
from app.models.tenancy import Tenant

log = logging.getLogger(__name__)


def run(coro):
    return asyncio.run(coro)


@asynccontextmanager
async def task_db(tenant_id: uuid.UUID | None = None):
    engine, factory = task_session_factory()
    try:
        async with factory() as session:
            if tenant_id is not None:
                session.info["tenant_id"] = str(tenant_id)
            yield session
    finally:
        await engine.dispose()


async def for_each_tenant(fn) -> dict:
    """Exécute fn(session, tenant_id) pour chaque tenant actif, avec une session liée à ce seul tenant (RLS)."""
    engine, factory = task_session_factory()
    results: dict[str, object] = {}
    try:
        async with factory() as s:
            tenant_ids = (await s.execute(select(Tenant.id).where(Tenant.is_active.is_(True)))).scalars().all()
        for tid in tenant_ids:
            async with factory() as session:
                session.info["tenant_id"] = str(tid)
                try:
                    results[str(tid)] = await fn(session, tid)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    log.exception("tenant_task_failed", extra={"tenant_id": str(tid)})
    finally:
        await engine.dispose()
    return results