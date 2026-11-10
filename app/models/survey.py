import uuid
from datetime import date, datetime

from sqlalchemy import (Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, SmallInteger,
                        String, Text, UniqueConstraint, Uuid, func, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (EvidenceStatus, ProfileStatus, QualityStatus, SurveyDomain, SurveyStatus)
from app.models.mixins import CreatedMixin, IdMixin, TenantMixin, pg_enum


class Survey(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "surveys"
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    domain: Mapped[SurveyDomain] = mapped_column(pg_enum(SurveyDomain))
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[SurveyStatus] = mapped_column(pg_enum(SurveyStatus), default=SurveyStatus.draft)
    unit: Mapped[str] = mapped_column(String(16), default="household")
    current_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    geo_scope: Mapped[dict | None] = mapped_column(JSONB)
    cooldown_days: Mapped[int] = mapped_column(Integer, default=30, server_default=text("30"))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    target_responses: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    allow_sensitive_axes: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    profile_generation: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    last_evolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responses_at_evolution: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_surveys_tenant_status", "tenant_id", "status"),)


class SurveyVersion(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "survey_versions"
    survey_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("surveys.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("survey_id", "version", name="uq_survey_versions"),)


class SurveyProfile(Base, IdMixin, TenantMixin, CreatedMixin):
    """Profil-réponse : un ensemble de réponses cohérentes qu'un répondant peut reconnaître comme les siennes."""
    __tablename__ = "survey_profiles"
    survey_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("surveys.id", ondelete="CASCADE"), index=True)
    slot: Mapped[str] = mapped_column(String(48))
    key: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(String(300))
    axes: Mapped[dict] = mapped_column(JSONB, default=dict)
    answers: Mapped[dict] = mapped_column(JSONB)
    origin: Mapped[str] = mapped_column(String(16))           # deterministic | cluster | ai | admin
    status: Mapped[ProfileStatus] = mapped_column(pg_enum(ProfileStatus), default=ProfileStatus.active)
    generation: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    shown_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    picked_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    override_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("survey_id", "key", name="uq_survey_profile_key"),
                      Index("ix_survey_profiles_slot", "survey_id", "slot", "status"))


class ProfileEvent(Base, IdMixin, TenantMixin, CreatedMixin):
    """Journal de l'évolution des profils (traçabilité des changements, y compris ceux proposés par l'IA)."""
    __tablename__ = "survey_profile_events"
    survey_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("surveys.id", ondelete="CASCADE"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(16))
    added: Mapped[int] = mapped_column(Integer, default=0)
    retired: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)


class SurveyResponse(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "survey_responses"
    survey_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("surveys.id", ondelete="CASCADE"))
    survey_version: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    contributor_hash: Mapped[str] = mapped_column(String(64), index=True)
    client_response_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    unit_ref_hash: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    local_date: Mapped[date] = mapped_column(Date)
    cell_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("geographic_cells.id", ondelete="RESTRICT"))
    h3_index: Mapped[str] = mapped_column(String(16))             # seule localisation conservée (pas de coordonnées)
    gps_accuracy: Mapped[float] = mapped_column(Float)
    duration_seconds: Mapped[float] = mapped_column(Float)
    mode: Mapped[str] = mapped_column(String(10))                 # profile | mixed | direct
    profile_picks: Mapped[dict] = mapped_column(JSONB, default=dict)
    answers: Mapped[dict] = mapped_column(JSONB)
    overrides: Mapped[list] = mapped_column(JSONB, default=list)
    none_slots: Mapped[list] = mapped_column(JSONB, default=list)
    custom_fingerprint: Mapped[str | None] = mapped_column(String(40))
    flags: Mapped[list] = mapped_column(JSONB, default=list)
    features: Mapped[dict] = mapped_column(JSONB, default=dict)
    needs_evidence: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    evidence_finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quality_status: Mapped[QualityStatus] = mapped_column(pg_enum(QualityStatus), default=QualityStatus.pending)
    weight: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "client_response_id", name="uq_survey_resp_client"),
        Index("ix_survey_resp_survey_status", "tenant_id", "survey_id", "quality_status"),
        Index("ix_survey_resp_cell_time", "tenant_id", "h3_index", "submitted_at"),
        Index("ix_survey_resp_status_created", "tenant_id", "quality_status", "created_at"),
        Index("ix_survey_resp_fp", "tenant_id", "survey_id", "custom_fingerprint"),
    )


class SurveyAnswer(Base, IdMixin, TenantMixin):
    """Réponse normalisée (une ligne par valeur) : base de toutes les cartes."""
    __tablename__ = "survey_answers"
    response_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("survey_responses.id", ondelete="CASCADE"))
    survey_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    indicator_key: Mapped[str] = mapped_column(String(48))
    question_key: Mapped[str] = mapped_column(String(48))
    domain: Mapped[str] = mapped_column(String(24))
    cell_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("geographic_cells.id", ondelete="RESTRICT"))
    h3_index: Mapped[str] = mapped_column(String(16))
    local_date: Mapped[date] = mapped_column(Date)
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    contributor_hash: Mapped[str] = mapped_column(String(64))
    unit_ref_hash: Mapped[str | None] = mapped_column(String(64))
    value_num: Mapped[float | None] = mapped_column(Float)
    value_cat: Mapped[str | None] = mapped_column(String(48))      # '__all__' = ligne repère d'une réponse catégorielle
    status: Mapped[QualityStatus] = mapped_column(pg_enum(QualityStatus), default=QualityStatus.pending)
    weight: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    proved: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    __table_args__ = (
        Index("ix_sv_answers_ind_date_cell", "tenant_id", "indicator_key", "local_date", "h3_index"),
        Index("ix_sv_answers_response", "response_id"),
        Index("ix_sv_answers_cell_ind", "tenant_id", "h3_index", "indicator_key"),
    )


class SurveyResponseQuality(Base, IdMixin, TenantMixin):
    __tablename__ = "survey_response_quality"
    response_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("survey_responses.id", ondelete="CASCADE"), unique=True)
    status: Mapped[QualityStatus] = mapped_column(pg_enum(QualityStatus))
    score: Mapped[float] = mapped_column(Float)
    sub_scores: Mapped[dict] = mapped_column(JSONB, default=dict)
    reasons: Mapped[list] = mapped_column(JSONB, default=list)
    evaluator: Mapped[str] = mapped_column(String(10), default="auto")     # auto | admin
    override_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SurveyEvidence(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "survey_evidence"
    response_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("survey_responses.id", ondelete="CASCADE"), index=True)
    question_key: Mapped[str] = mapped_column(String(48))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    mime: Mapped[str] = mapped_column(String(20))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes | None] = mapped_column(LargeBinary)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)               # exif : taken_at, lat, lon, width, height
    status: Mapped[EvidenceStatus] = mapped_column(pg_enum(EvidenceStatus), default=EvidenceStatus.pending)


class RespondentTrust(Base, TenantMixin):
    """Confiance et profil de comportement d'un répondant (interne, jamais exposé aux autres utilisateurs)."""
    __tablename__ = "respondent_trust"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    trust: Mapped[float] = mapped_column(Float, default=0.6)
    n_responses: Mapped[int] = mapped_column(Integer, default=0)
    persona: Mapped[str] = mapped_column(String(24), default="nouveau")
    trend: Mapped[float] = mapped_column(Float, default=0.0)
    features: Mapped[dict] = mapped_column(JSONB, default=dict)
    flags: Mapped[list] = mapped_column(JSONB, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReferenceValue(Base, IdMixin, TenantMixin, CreatedMixin):
    """Valeur de référence fournie par l'organisation (recensement, enquête officielle…), jamais inventée."""
    __tablename__ = "reference_values"
    indicator_key: Mapped[str] = mapped_column(String(48), index=True)
    stat: Mapped[str] = mapped_column(String(8))                  # mean | share
    category: Mapped[str | None] = mapped_column(String(48))
    value: Mapped[float] = mapped_column(Float)
    scope_h3: Mapped[str | None] = mapped_column(String(16))
    scope_res: Mapped[int | None] = mapped_column(SmallInteger)
    period_year: Mapped[int | None] = mapped_column(SmallInteger)
    source: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)