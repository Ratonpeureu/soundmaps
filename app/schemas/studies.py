import uuid
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.models.enums import SponsorKind, StudyKind


class SponsorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: SponsorKind
    name: str = Field(min_length=2, max_length=160)
    tagline: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=3000)
    sector: str | None = Field(None, max_length=80)
    city: str | None = Field(None, max_length=80)
    country: str | None = Field(None, max_length=60)
    website: str | None = Field(None, max_length=200)
    contact_public: str | None = Field(None, max_length=120)
    logo_asset_id: uuid.UUID | None = None
    cover_asset_id: uuid.UUID | None = None
    activities: list[str] = Field(default_factory=list, max_length=12)
    facts: dict[str, str] = Field(default_factory=dict)
    verified: bool = False
    active: bool = True


class SponsorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: SponsorKind | None = None
    name: str | None = Field(None, min_length=2, max_length=160)
    tagline: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=3000)
    sector: str | None = Field(None, max_length=80)
    city: str | None = Field(None, max_length=80)
    country: str | None = Field(None, max_length=60)
    website: str | None = Field(None, max_length=200)
    contact_public: str | None = Field(None, max_length=120)
    logo_asset_id: uuid.UUID | None = None
    cover_asset_id: uuid.UUID | None = None
    activities: list[str] | None = Field(None, max_length=12)
    facts: dict[str, str] | None = None
    verified: bool | None = None
    active: bool | None = None


class StudyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sponsor_id: uuid.UUID
    kind: StudyKind
    title: str = Field(min_length=3, max_length=160)
    summary: str | None = Field(None, max_length=280)
    description: str | None = Field(None, max_length=8000)
    cover_asset_id: uuid.UUID | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    quota_total: int = Field(0, ge=0, le=10_000_000)
    estimated_minutes: int = Field(5, ge=1, le=240)
    terms: str | None = Field(None, max_length=8000)
    tags: list[str] = Field(default_factory=list, max_length=10)
    targeting: dict = Field(default_factory=dict)
    reward: dict = Field(default_factory=dict)
    privacy: dict = Field(default_factory=dict)
    definition: dict


class StudyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sponsor_id: uuid.UUID | None = None
    kind: StudyKind | None = None
    title: str | None = Field(None, min_length=3, max_length=160)
    summary: str | None = Field(None, max_length=280)
    description: str | None = Field(None, max_length=8000)
    cover_asset_id: uuid.UUID | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    quota_total: int | None = Field(None, ge=0, le=10_000_000)
    estimated_minutes: int | None = Field(None, ge=1, le=240)
    terms: str | None = Field(None, max_length=8000)
    tags: list[str] | None = Field(None, max_length=10)
    targeting: dict | None = None
    reward: dict | None = None
    privacy: dict | None = None
    definition: dict | None = None


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["draft", "active", "paused", "closed"]


class TargetingPreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targeting: dict


class DecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    status: Literal["validated", "indicative", "rejected"]
    reason: str = Field(min_length=3, max_length=500)


class RewardActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "refuse", "revoke", "deliver"]
    reason_code: str | None = Field(None, max_length=40)
    note: str | None = Field(None, max_length=300)
    reference: str | None = Field(None, max_length=120)


class CodesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    codes: list[str] = Field(min_length=1, max_length=5000)


class DrawIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    winners: int | None = Field(None, ge=1, le=10000)
    seed: int | None = None


# ----- côté participant
class SubmitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_response_id: uuid.UUID
    study_version: int = Field(ge=1)
    answers: dict[str, Any] = Field(default_factory=dict)
    share_contact: bool = False
    contact: dict | None = None


class CheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: dict[str, Any] = Field(default_factory=dict)


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gender: str | None = Field(None, max_length=10)
    occupation_group: str | None = Field(None, max_length=24)
    education_level: str | None = Field(None, max_length=24)
    household_size: int | None = Field(None, ge=1, le=60)
    income_band: str | None = Field(None, max_length=12)
    languages: list[str] | None = Field(None, max_length=12)
    interests: list[str] | None = Field(None, max_length=20)


class ClaimIn(BaseModel):
    ...