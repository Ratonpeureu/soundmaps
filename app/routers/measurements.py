from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db
from app.core.rate_limit import limit_by_user
from app.models.tenancy import User
from app.schemas.schemas import (BatchResult, HistoryItem, MeasurementAck, MeasurementBatch, MeasurementCreate)
from app.services import measurement as measurement_service

router = APIRouter(prefix="/measurements", tags=["measurements"],
                   dependencies=[Depends(limit_by_user("measurements", settings.RATE_LIMIT_MEASUREMENTS_PER_MINUTE))])


@router.post("", response_model=MeasurementAck, status_code=202)
async def create(payload: MeasurementCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await measurement_service.ingest(db, user, payload)


@router.post("/batch", response_model=BatchResult, status_code=207)
async def create_batch(payload: MeasurementBatch, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    """Synchronisation hors-ligne : mesures stockées localement puis envoyées au retour du réseau (idempotent)."""
    return await measurement_service.ingest_batch(db, user, payload)


@router.get("/history", response_model=list[HistoryItem])
async def history(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                  user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await measurement_service.history(db, user, limit, offset)