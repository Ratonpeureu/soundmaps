import uuid
from datetime import date, datetime, time
from enum import StrEnum
from typing import Literal

from fastapi import Query
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
)

from app.core.config import settings
from app.models.enums import (
    CalibrationStatus,
    ConsentType,
    DayPart,
    InsightKind,
    NoiseContext,
    ProposalStatus,
    QualityStatus,
    ReportKind,
    ReportStatus,
    Role,
    ScheduleMode,
    TriggerMode,
)

DayPartFilter = Literal["all", "morning", "afternoon", "evening", "night"]

class TenantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
    name: str = Field(min_length=2, max_length=200)
    allow_registration: bool = True
    admin_email: EmailStr
    admin_password: str = Field(min_length=8, max_length=128)


class TenantUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=2, max_length=200)
    is_active: bool | None = None
    allow_registration: bool | None = None


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    slug: str
    name: str
    is_active: bool
    allow_registration: bool
    created_at: datetime



from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int




class UserMe(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    pseudonym: str
    role: Role
    hybrid_collection_enabled: bool
    participation_paused: bool
    created_at: datetime


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hybrid_collection_enabled: bool | None = None
    participation_paused: bool | None = None


class ConsentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consent_type: ConsentType
    granted: bool


class ConsentOut(BaseModel):
    consent_type: ConsentType
    granted: bool
    policy_version: str
    updated_at: datetime


class ScheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days_of_week: list[int] = Field(min_length=1, max_length=7)
    window_start: time
    window_end: time
    preferred_times: list[time] = Field(default_factory=list, max_length=12)
    mode: ScheduleMode = ScheduleMode.both
    timezone: str | None = None
    active: bool = True

    @field_validator("days_of_week")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v) or len(set(v)) != len(v):
            raise ValueError("days_of_week : entiers 0 (lundi) à 6 (dimanche), sans doublon")
        return sorted(v)

    @field_validator("window_start", "window_end", "preferred_times", mode="after")
    @classmethod
    def _naive(cls, v):
        values = v if isinstance(v, list) else [v]
        if any(t.tzinfo is not None for t in values):
            raise ValueError("Les heures doivent être locales, sans fuseau")
        return v


class ScheduleOut(ScheduleIn):
    model_config = ConfigDict(from_attributes=True)


class ContributionsOut(BaseModel):
    total_measurements: int
    by_status: dict[QualityStatus, int]
    cells_covered: int
    last_measurement_at: datetime | None


class AccountDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str





class DeviceRegister(BaseModel):
    model_config = ConfigDict(extra="forbid")
    install_id: str = Field(min_length=8, max_length=128)
    manufacturer: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    android_version: str = Field(min_length=1, max_length=20)
    microphone: str = Field("internal", max_length=40)
    sample_rate_hz: int | None = Field(None, ge=8000, le=192000)


class DeviceOut(BaseModel):
    id: uuid.UUID
    device_model_id: uuid.UUID
    calibration_status: CalibrationStatus
    sample_rate_hz: int | None




class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger: TriggerMode
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    battery_level: int = Field(ge=0, le=100)


class ProposalOut(BaseModel):
    id: uuid.UUID
    status: ProposalStatus
    trigger: TriggerMode
    h3_index: str
    priority: float
    expires_at: datetime
    session_duration_seconds: int


class ProposalDecisionOut(BaseModel):
    proposed: bool
    reason: str
    proposal: ProposalOut | None = None




class MeasurementCreate(BaseModel):
    """extra=forbid : tout champ audio ou inconnu est refusé (aucun audio brut n'est accepté)."""
    model_config = ConfigDict(extra="forbid")
    client_measurement_id: uuid.UUID
    device_id: uuid.UUID
    proposal_id: uuid.UUID | None = None
    measured_at: AwareDatetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    gps_accuracy_m: float = Field(ge=0, le=100000)
    duration_seconds: float = Field(gt=0, le=3600)
    effective_duration_seconds: float = Field(ge=0, le=3600)
    sample_rate_hz: int | None = Field(None, ge=8000, le=192000)
    laeq: float = Field(ge=0, le=200)
    lmax: float = Field(ge=0, le=200)
    lmin: float = Field(ge=0, le=200)
    saturation_ratio: float = Field(ge=0, le=1)
    moved_distance_m: float = Field(ge=0, le=100000)
    noise_context: NoiseContext | None = None


class MeasurementBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[MeasurementCreate] = Field(min_length=1, max_length=settings.BATCH_MAX_ITEMS)


class MeasurementAck(BaseModel):
    id: uuid.UUID
    client_measurement_id: uuid.UUID
    quality_status: QualityStatus
    duplicate: bool


class BatchItemResult(BaseModel):
    client_measurement_id: uuid.UUID
    result: Literal["accepted", "duplicate", "error"]
    id: uuid.UUID | None = None
    error: str | None = None


class BatchResult(BaseModel):
    results: list[BatchItemResult]


class HistoryItem(BaseModel):
    id: uuid.UUID
    measured_at: datetime
    h3_index: str
    laeq_db: float
    lmax_db: float
    day_part: DayPart
    calibration_status: CalibrationStatus
    quality_status: QualityStatus
    quality_score: float | None
    reason_codes: list[str]
    noise_context: NoiseContext | None
    location_anonymized: bool








class Period(StrEnum):
    today = "today"
    last_7_days = "7d"
    last_30_days = "30d"
    custom = "custom"


class TimeFilter:
    """Dépendance FastAPI commune : filtres temporels de la carte, des stats, de l'IA et des rapports."""

    def __init__(self,
                 period: Period = Query(Period.last_7_days),
                 date_from: date | None = Query(None),
                 date_to: date | None = Query(None),
                 day_part: DayPartFilter = Query("all")):
        self.period = period
        self.date_from = date_from
        self.date_to = date_to
        self.day_part = day_part




from datetime import date, datetime

from pydantic import BaseModel


class UnderDocumentedCell(BaseModel):
    h3_index: str
    measurement_count: int


class OverviewOut(BaseModel):
    period_start: datetime
    period_end: datetime
    measurements_by_status: dict[str, int]
    measurements_by_day_part: dict[str, int]
    active_contributors: int
    cells_covered: int
    calibrated_share: float | None
    device_models_by_calibration: dict[str, int]
    proposals_by_status: dict[str, int]
    under_documented_cells: list[UnderDocumentedCell]


class TimeseriesPoint(BaseModel):
    date: date
    measurement_count: int
    laeq_db: float | None
    lmax_db: float | None




class AdminUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    pseudonym: str
    role: Role
    is_active: bool
    hybrid_collection_enabled: bool
    participation_paused: bool
    created_at: datetime
    last_login_at: datetime | None


class AdminUserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_active: bool | None = None
    role: Role | None = None


class AdminMeasurementOut(BaseModel):
    id: uuid.UUID
    measured_at: datetime
    h3_index: str
    day_part: DayPart
    laeq_db: float
    lmax_db: float
    calibration_status: CalibrationStatus
    quality_status: QualityStatus
    quality_score: float | None
    reason_codes: list[str]
    evaluator: str | None
    contributor_token: str
    noise_context: NoiseContext | None


class OverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["validated", "indicative", "rejected"]
    reason: str = Field(min_length=3, max_length=500)


class DeviceModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manufacturer: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    android_version: str = Field(min_length=1, max_length=20)
    microphone: str = Field("internal", max_length=40)


class DeviceModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    manufacturer: str
    model: str
    android_version: str
    microphone: str
    calibration_status: CalibrationStatus
    correction_db: float | None


class CalibrationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correction_db: float = Field(ge=-60, le=60)
    reference_instrument: str = Field(min_length=2, max_length=200)
    conditions: str | None = Field(None, max_length=2000)


class CalibrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    correction_db: float
    reference_instrument: str
    conditions: str | None
    is_active: bool
    created_at: datetime


class CellOut(BaseModel):
    h3_index: str
    center_lat: float
    center_lon: float
    last_measurement_at: datetime | None


class AlgorithmStateOut(BaseModel):
    weights: dict[str, float]
    base_weights: dict[str, float]
    samples_used: int
    updated_at: datetime | None


class SettingsOverrides(BaseModel):
    """Seuils surchargeables par tenant (les autres restent ceux du .env)."""
    model_config = ConfigDict(extra="forbid")
    validated_min_score: float | None = Field(None, ge=0, le=1)
    indicative_min_score: float | None = Field(None, ge=0, le=1)
    gps_reject_m: float | None = Field(None, gt=0, le=1000)
    max_movement_m: float | None = Field(None, gt=0, le=1000)
    public_min_devices: int | None = Field(None, ge=1, le=1000)
    allow_uncalibrated_validated: bool | None = None


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    created_at: datetime
    actor_user_id: uuid.UUID | None
    action: str
    resource_type: str
    resource_id: str | None
    details: dict


class QualitySummaryOut(BaseModel):
    by_status: dict[str, int]
    top_reason_codes: dict[str, int]









class SourceHypothesis(BaseModel):
    label: str
    rationale: str
    likelihood: Literal["low", "medium", "high"]


class CellAnalysis(BaseModel):
    """Sortie JSON exigée du LLM (validée strictement)."""
    model_config = ConfigDict(extra="ignore")
    summary: str
    temporal_pattern: str
    probable_sources: list[SourceHypothesis] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    data_limitations: list[str] = Field(default_factory=list)


class ReportNarrative(BaseModel):
    model_config = ConfigDict(extra="ignore")
    executive_summary: str
    key_findings: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class InsightOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: InsightKind
    h3_index: str | None
    model: str
    period_from: date | None
    period_to: date | None
    content: dict
    created_at: datetime








class ReportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: ReportKind
    period: Period = Period.last_30_days
    date_from: date | None = None
    date_to: date | None = None
    day_part: DayPartFilter = "all"
    include_indicative: bool = False
    include_ai_summary: bool = False


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: ReportKind
    status: ReportStatus
    params: dict
    filename: str | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


    




