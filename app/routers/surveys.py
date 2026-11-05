import uuid

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db
from app.core.rate_limit import limit_by_user
from app.core.redis import get_redis
from app.core.survey_settings import survey_settings as sv
from app.models.tenancy import User
from app.schemas.surveys import SurveySubmit
from app.services import survey_ingest as ing

router = APIRouter(prefix="/surveys", tags=["surveys"],
                   dependencies=[Depends(limit_by_user("surveys", settings.RATE_LIMIT_API_PER_MINUTE))])


@router.get("")
async def available(lat: float | None = Query(None, ge=-90, le=90), lon: float | None = Query(None, ge=-180, le=180),
                    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Enquêtes ouvertes pour moi (zone, délai de repos), triées par besoin autour de ma position."""
    return await ing.list_available(db, user, lat, lon)


@router.get("/responses/mine")
async def mine(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
               user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await ing.my_history(db, user, limit, offset)


@router.get("/me/trust")
async def my_trust(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await ing.trust_of(db, user)


@router.get("/responses/{response_id}")
async def response_detail(response_id: uuid.UUID, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    return await ing.response_detail(db, user, response_id)


@router.post("/responses", status_code=202)
async def submit(payload: SurveySubmit, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                 redis: Redis = Depends(get_redis)):
    return await ing.submit(db, redis, user, payload)


@router.post("/responses/{response_id}/evidence", status_code=201,
             dependencies=[Depends(limit_by_user("evidence", sv.RATE_LIMIT_EVIDENCE_PER_MINUTE))])
async def add_evidence(response_id: uuid.UUID, question_key: str = Form(..., max_length=48),
                       file: UploadFile = File(...), user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    data = await file.read(sv.SV_EVIDENCE_MAX_BYTES + 1)
    return await ing.add_evidence(db, user, response_id, question_key, data)


@router.post("/responses/{response_id}/evidence/done")
async def evidence_done(response_id: uuid.UUID, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    return await ing.finalize_evidence(db, user, response_id)


@router.get("/{survey_id}")
async def get_survey(survey_id: uuid.UUID, lat: float | None = Query(None, ge=-90, le=90),
                     lon: float | None = Query(None, ge=-180, le=180), user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis)):
    """Questionnaire + profils proposés (classés pour moi). Position utilisée uniquement pour classer."""
    return await ing.get_for_user(db, redis, user, survey_id, lat, lon)