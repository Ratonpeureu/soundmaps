import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db
from app.core.rate_limit import limit_by_user
from app.core.study_settings import study_settings as sts
from app.models.tenancy import User
from app.schemas.studies import CheckIn, ClaimIn, ProfileIn, SubmitIn
from app.services import study_ingest as ing
from app.services.study_schema import ATTRS

router = APIRouter(prefix="/studies", tags=["studies"],
                   dependencies=[Depends(limit_by_user("studies", settings.RATE_LIMIT_API_PER_MINUTE))])
assets_router = APIRouter(prefix="/study-assets", tags=["studies"],
                          dependencies=[Depends(limit_by_user("study_asset", sts.RATE_LIMIT_STUDY_ASSET_PER_MINUTE))])


def _opts(key: str) -> list[dict]:
    return [{"value": a, "label": b} for a, b in ATTRS[key]["options"]]


# ------------------------------------------------------------------ routes statiques (avant /{study_id})
@router.get("")
async def available(lat: float | None = Query(None, ge=-90, le=90), lon: float | None = Query(None, ge=-180, le=180),
                    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Études ouvertes et ciblant l'utilisateur (invitations d'abord). Nécessite le consentement « Études »."""
    return await ing.list_available(db, user, lat, lon)


@router.get("/participations")
async def participations(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await ing.my_participations(db, user)


@router.get("/rewards")
async def rewards(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await ing.my_rewards(db, user)


@router.get("/rewards/{reward_id}")
async def reward_detail(reward_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await ing.reward_detail(db, user, reward_id)


@router.post("/rewards/{reward_id}/claim")
async def claim(reward_id: uuid.UUID, payload: ClaimIn, user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db)):
    return await ing.claim(db, user, reward_id, payload.phone, payload.name)


@router.get("/profile")
async def get_profile(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await ing.get_profile(db, user)


@router.put("/profile")
async def put_profile(payload: ProfileIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await ing.put_profile(db, user, payload.model_dump(exclude_unset=True))


@router.get("/profile/options")
async def profile_options(user: User = Depends(get_current_user)):
    return {k: _opts(k) for k in ("gender", "occupation_group", "education_level", "income_band", "languages", "interests")}


@router.get("/sponsors/{sponsor_id}")
async def sponsor(sponsor_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await ing.sponsor_public(db, user, sponsor_id)


# ------------------------------------------------------------------ une étude
@router.get("/{study_id}")
async def get_one(study_id: uuid.UUID, lat: float | None = Query(None, ge=-90, le=90),
                  lon: float | None = Query(None, ge=-180, le=180), user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db)):
    """Fiche complète : initiateur, règles de récompense, confidentialité, questionnaire dynamique."""
    return await ing.get_for_user(db, user, study_id, lat, lon)


@router.post("/{study_id}/start")
async def start(study_id: uuid.UUID, lat: float | None = Query(None, ge=-90, le=90),
                lon: float | None = Query(None, ge=-180, le=180), user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db)):
    return await ing.start(db, user, study_id, lat, lon)


@router.post("/{study_id}/decline")
async def decline(study_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await ing.decline(db, user, study_id)


@router.post("/{study_id}/check")
async def check(study_id: uuid.UUID, payload: CheckIn, user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db)):
    """Question filtre : dit tout de suite si la personne est dans la cible."""
    return await ing.check(db, user, study_id, payload.answers)


@router.post("/{study_id}/submit", status_code=201)
async def submit(study_id: uuid.UUID, payload: SubmitIn, user: User = Depends(get_current_user),
                 db: AsyncSession = Depends(get_db)):
    return await ing.submit(db, user, study_id, payload)


# ------------------------------------------------------------------ médias (images / vidéos des études)
@assets_router.get("/{asset_id}")
async def asset(asset_id: uuid.UUID, request: Request, user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db)):
    return await ing.serve_asset(db, user.tenant_id, asset_id, request)