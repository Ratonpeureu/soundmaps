import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.core.survey_settings import survey_settings as sv
from app.models.enums import SurveyDomain
from app.services import catalog
from app.services.geo import is_valid_cell


class Pick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5)
    none: bool = False


class SurveySubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_response_id: uuid.UUID
    survey_id: uuid.UUID
    survey_version: int = Field(ge=1)
    started_at: AwareDatetime
    submitted_at: AwareDatetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    gps_accuracy_m: float = Field(ge=0, le=100000)
    unit_ref: str | None = Field(None, max_length=120)
    picks: dict[str, Pick] = Field(default_factory=dict)
    overrides: dict[str, Any] = Field(default_factory=dict)
    direct: dict[str, Any] = Field(default_factory=dict)

    @field_validator("picks", "overrides", "direct")
    @classmethod
    def _size(cls, v):
        if len(v) > sv.SV_MAX_QUESTIONS:
            raise ValueError("too_many_items")
        return v


class GeoScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    res: int = Field(ge=3, le=9)
    cells: list[str] = Field(min_length=1, max_length=500)

    @field_validator("cells")
    @classmethod
    def _cells(cls, v, info):
        import h3
        res = info.data.get("res")
        for c in v:
            if not is_valid_cell(c) or (res is not None and h3.get_resolution(c) != res):
                raise ValueError("invalid_scope_cell")
        return v


class SurveyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: SurveyDomain
    title: str = Field(min_length=3, max_length=160)
    description: str | None = Field(None, max_length=1500)
    definition: dict
    geo_scope: GeoScope | None = None
    cooldown_days: int = Field(30, ge=0, le=365)
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    target_responses: int = Field(0, ge=0, le=10_000_000)
    allow_sensitive_axes: bool = False


class SurveyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(None, min_length=3, max_length=160)
    description: str | None = Field(None, max_length=1500)
    definition: dict | None = None
    geo_scope: GeoScope | None = None
    clear_geo_scope: bool = False
    cooldown_days: int | None = Field(None, ge=0, le=365)
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    target_responses: int | None = Field(None, ge=0, le=10_000_000)
    allow_sensitive_axes: bool | None = None


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "paused", "closed", "draft"]
    use_ai: bool = False


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: str
    label: str = Field(min_length=2, max_length=120)
    description: str | None = Field(None, max_length=300)
    answers: dict


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(None, min_length=2, max_length=120)
    description: str | None = Field(None, max_length=300)
    retire: bool | None = None
    pinned: bool | None = None


class GenerateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: str | None = None
    n: int | None = Field(None, ge=1, le=16)
    use_ai: bool = False


class EvolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    use_ai: bool = True


class DesignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: SurveyDomain
    goal: str = Field(min_length=5, max_length=500)
    indicators: list[str] = Field(default_factory=list, max_length=40)
    unit: Literal["person", "household", "dwelling", "enterprise", "farm", "facility", "community"] = "household"
    max_questions: int = Field(12, ge=3, le=60)


class BulkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    status: Literal["validated", "indicative", "rejected"]
    reason: str = Field(min_length=3, max_length=500)


class ReleaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


class ReferenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    indicator_key: str
    stat: Literal["mean", "share"]
    category: str | None = Field(None, max_length=48)
    value: float
    scope_h3: str | None = Field(None, max_length=16)
    scope_res: int | None = Field(None, ge=3, le=9)
    period_year: int | None = Field(None, ge=1900, le=2100)
    source: str = Field(min_length=3, max_length=200)

    @field_validator("indicator_key")
    @classmethod
    def _ind(cls, v):
        if v not in catalog.INDICATORS:
            raise ValueError("unknown_indicator")
        return v

    @field_validator("scope_h3")
    @classmethod
    def _scope(cls, v):
        if v is not None and not is_valid_cell(v):
            raise ValueError("invalid_h3")
        return v

    def check(self) -> None:
        ind = catalog.INDICATORS[self.indicator_key]
        if self.stat == "share":
            if not 0 <= self.value <= 1:
                raise ValueError("share_must_be_fraction_0_1")
            if ind.kind == "category" and self.category not in {c.code for c in ind.cats}:
                raise ValueError("unknown_category")
            if ind.kind != "category" and self.category is not None:
                raise ValueError("category_not_allowed")
        elif ind.kind not in ("number", "scale") or self.category is not None:
            raise ValueError("mean_only_for_numeric")
        if (self.scope_h3 is None) != (self.scope_res is None):
            raise ValueError("scope_incomplete")


class ReferenceBulk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ReferenceIn] = Field(min_length=1, max_length=500)