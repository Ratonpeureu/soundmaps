import uuid

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db
from app.core.rate_limit import limit_by_user
from app.core.redis import get_redis
from app.models.tenancy import User
from app.schemas.schemas import ProposalDecisionOut, ProposalOut, ProposalRequest,ScheduleIn, ScheduleOut
from app.services import collection as collection_service
from app.services import user as user_service

router = APIRouter(prefix="/collection", tags=["collection"],
                   dependencies=[Depends(limit_by_user("collection", settings.RATE_LIMIT_API_PER_MINUTE))])


@router.put("/schedule", response_model=ScheduleOut)
async def put_schedule(payload: ScheduleIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await user_service.upsert_schedule(db, user, payload)


@router.get("/schedule", response_model=ScheduleOut)
async def get_schedule(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await user_service.get_schedule(db, user)


@router.post("/proposals/request", response_model=ProposalDecisionOut)
async def request_proposal(payload: ProposalRequest, user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis)):
    return await collection_service.request_proposal(db, redis, user, payload)


@router.get("/proposals", response_model=list[ProposalOut])
async def proposals(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await collection_service.list_open_proposals(db, user)


@router.post("/proposals/{proposal_id}/accept", response_model=ProposalOut)
async def accept(proposal_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await collection_service.respond(db, user, proposal_id, True)


@router.post("/proposals/{proposal_id}/decline", response_model=ProposalOut)
async def decline(proposal_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await collection_service.respond(db, user, proposal_id, False)