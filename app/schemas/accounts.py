import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import Role


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    display_name: str = Field(min_length=3, max_length=24, pattern=r"^[\w .'-]+$")
    first_name: str | None = Field(None, max_length=60)
    last_name: str | None = Field(None, max_length=60)
    phone: str | None = Field(None, pattern=r"^\+?[0-9 ()-]{6,20}$")
    city: str | None = Field(None, max_length=80)
    district: str | None = Field(None, max_length=80)
    birth_year: int | None = None
    invite_code: str | None = Field(None, max_length=24)
    accept_terms: bool

    @field_validator("birth_year")
    @classmethod
    def _year(cls, v):
        if v is not None and not (1900 <= v <= datetime.now(timezone.utc).year - 13):
            raise ValueError("année de naissance invalide (13 ans minimum)")
        return v


class RegisterOut(BaseModel):
    id: uuid.UUID
    pseudonym: str
    display_name: str
    role: Role
    created_at: datetime
    recovery_codes: list[str]


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class PasswordChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


class PasswordResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    recovery_code: str = Field(min_length=8, max_length=24)
    new_password: str = Field(min_length=10, max_length=128)


class PasswordConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=128)


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(None, min_length=3, max_length=24, pattern=r"^[\w .'-]+$")
    first_name: str | None = Field(None, max_length=60)
    last_name: str | None = Field(None, max_length=60)
    phone: str | None = Field(None, pattern=r"^\+?[0-9 ()-]{6,20}$")
    city: str | None = Field(None, max_length=80)
    district: str | None = Field(None, max_length=80)
    birth_year: int | None = None
    show_in_leaderboard: bool | None = None

    @field_validator("birth_year")
    @classmethod
    def _year(cls, v):
        if v is not None and not (1900 <= v <= datetime.now(timezone.utc).year - 13):
            raise ValueError("année de naissance invalide")
        return v


class InviteAdminIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_uses: int = Field(1, ge=1, le=1000)
    ttl_days: int = Field(30, ge=1, le=365)
    count: int = Field(1, ge=1, le=50)
    note: str | None = Field(None, max_length=120)


class VoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vote: Literal["agree", "down", "up"]
    strong: bool = False


class PresenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class SuspendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: int = Field(14, ge=1, le=365)