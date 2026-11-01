import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_public_db, verify_platform_key
from app.schemas.schemas import TenantCreate, TenantOut, TenantUpdate
from app.services import tenant as tenant_service

router = APIRouter(prefix="/platform", tags=["platform"], dependencies=[Depends(verify_platform_key)])


@router.post("/tenants", response_model=TenantOut, status_code=201)
async def create(payload: TenantCreate, db: AsyncSession = Depends(get_public_db)):
    return await tenant_service.create_tenant(db, payload)


@router.get("/tenants", response_model=list[TenantOut])
async def list_all(db: AsyncSession = Depends(get_public_db)):
    return await tenant_service.list_tenants(db)


@router.patch("/tenants/{tenant_id}", response_model=TenantOut)
async def update(tenant_id: uuid.UUID, payload: TenantUpdate, db: AsyncSession = Depends(get_public_db)):
    return await tenant_service.update_tenant(db, tenant_id, payload)