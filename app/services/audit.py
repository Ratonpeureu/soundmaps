import uuid

from fastapi import Request
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_ip
from app.models.tenancy import AuditLog, User


async def audit(db: AsyncSession, actor: User | None, action: str, resource_type: str,
                resource_id: str | uuid.UUID | None = None, details: dict | None = None,
                request: Request | None = None, tenant_id: uuid.UUID | None = None) -> None:
    """Ajoute une ligne de journal (sans commit). L'IP n'est stockée que hachée."""
    ip = request.client.host if request and request.client else None
    db.add(AuditLog(
        tenant_id=tenant_id or actor.tenant_id,
        actor_user_id=actor.id if actor else None,
        action=action, resource_type=resource_type,
        resource_id=str(resource_id) if resource_id else None,
        ip_hash=hash_ip(ip) if ip else None,
        details=details or {},
    ))