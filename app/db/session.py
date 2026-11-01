import uuid

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import settings

_SET_TENANT = text("SELECT set_config('app.tenant_id', :tid, true)")

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    hide_parameters=True,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(Session, "after_begin")
def _apply_tenant_context(session, transaction, connection):
    """Chaque transaction démarre avec le tenant de la session (échec fermé si absent : RLS ne renvoie rien)."""
    connection.execute(_SET_TENANT, {"tid": session.info.get("tenant_id") or ""})


async def bind_tenant(session: AsyncSession, tenant_id: uuid.UUID | str) -> None:
    session.info["tenant_id"] = str(tenant_id)
    await session.execute(_SET_TENANT, {"tid": str(tenant_id)})


def task_session_factory():
    """Moteur dédié aux tâches Celery (une boucle asyncio par tâche, donc sans pool)."""
    task_engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool, hide_parameters=True)
    return task_engine, async_sessionmaker(task_engine, expire_on_commit=False)