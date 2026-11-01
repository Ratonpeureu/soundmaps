from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_email, hash_password, make_pseudonym
from app.db.session import bind_tenant
from app.models.collection import TenantAlgorithmState
from app.models.enums import Role
from app.models.tenancy import Tenant, User
from app.schemas.schemas import TenantCreate, TenantUpdate
from app.services.collection import base_weights
from app.services.audit import audit
from datetime import datetime, timezone
import uuid


async def create_tenant(db: AsyncSession, payload: TenantCreate) -> Tenant:
    tenant = Tenant(id=uuid.uuid4(), slug=payload.slug, name=payload.name,
                    allow_registration=payload.allow_registration)
    db.add(tenant)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "slug_already_used")
    await bind_tenant(db, tenant.id)
    admin_id = uuid.uuid4()
    db.add(User(id=admin_id, tenant_id=tenant.id, email_hash=hash_email(tenant.id, payload.admin_email),
                password_hash=hash_password(payload.admin_password), role=Role.tenant_admin,
                pseudonym=make_pseudonym(tenant.id, admin_id)))
    db.add(TenantAlgorithmState(tenant_id=tenant.id, weights=base_weights(), samples_used=0,
                                updated_at=datetime.now(timezone.utc)))
    await audit(db, None, "tenant.create", "tenant", tenant.id, tenant_id=tenant.id)
    await db.commit()
    return tenant


async def list_tenants(db: AsyncSession) -> list[Tenant]:
    return list((await db.execute(select(Tenant).order_by(Tenant.created_at))).scalars())


async def update_tenant(db: AsyncSession, tenant_id: uuid.UUID, payload: TenantUpdate) -> Tenant:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "tenant_not_found")
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(tenant, field, value)
    await db.commit()
    return tenant