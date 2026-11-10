import uuid
from datetime import datetime

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, SmallInteger, String, Text,
                        UniqueConstraint, Uuid, func, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (AssetKind, ParticipationStatus, QualityStatus, RewardStatus, SponsorKind, StudyKind,
                              StudyStatus)
from app.models.mixins import CreatedMixin, IdMixin, TenantMixin, pg_enum


class StudyAsset(Base, IdMixin, TenantMixin, CreatedMixin):
    """Image ou vidéo de la médiathèque (diffusée dans les études)."""
    __tablename__ = "study_assets"
    kind: Mapped[AssetKind] = mapped_column(pg_enum(AssetKind))
    name: Mapped[str] = mapped_column(String(160))
    mime: Mapped[str] = mapped_column(String(40))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    content: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)


class StudySponsor(Base, IdMixin, TenantMixin, CreatedMixin):
    """Initiateur : entreprise, personne, ONG, service ou institution qui demande l'étude."""
    __tablename__ = "study_sponsors"
    kind: Mapped[SponsorKind] = mapped_column(pg_enum(SponsorKind))
    name: Mapped[str] = mapped_column(String(160))
    tagline: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    sector: Mapped[str | None] = mapped_column(String(80))
    city: Mapped[str | None] = mapped_column(String(80))
    country: Mapped[str | None] = mapped_column(String(60))
    website: Mapped[str | None] = mapped_column(String(200))
    contact_public: Mapped[str | None] = mapped_column(String(120))
    logo_asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("study_assets.id", ondelete="SET NULL"))
    cover_asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("study_assets.id", ondelete="SET NULL"))
    activities: Mapped[list] = mapped_column(JSONB, default=list)
    facts: Mapped[dict] = mapped_column(JSONB, default=dict)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Study(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "studies"
    sponsor_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("study_sponsors.id", ondelete="RESTRICT"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    kind: Mapped[StudyKind] = mapped_column(pg_enum(StudyKind))
    title: Mapped[str] = mapped_column(String(160))
    summary: Mapped[str | None] = mapped_column(String(280))
    description: Mapped[str | None] = mapped_column(Text)
    cover_asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("study_assets.id", ondelete="SET NULL"))
    status: Mapped[StudyStatus] = mapped_column(pg_enum(StudyStatus), default=StudyStatus.draft)
    current_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quota_total: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    estimated_minutes: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"))
    targeting: Mapped[dict] = mapped_column(JSONB, default=dict)
    reward: Mapped[dict] = mapped_column(JSONB, default=dict)
    privacy: Mapped[dict] = mapped_column(JSONB, default=dict)
    terms: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_studies_tenant_status_end", "tenant_id", "status", "ends_at"),)


class StudyVersion(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "study_versions"
    study_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("studies.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("study_id", "version", name="uq_study_versions"),)


class StudyParticipation(Base, IdMixin, TenantMixin, CreatedMixin):
    """Invitation puis participation d'un utilisateur à une étude."""
    __tablename__ = "study_participations"
    study_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("studies.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(10), default="organic")          # invited | organic
    status: Mapped[ParticipationStatus] = mapped_column(pg_enum(ParticipationStatus), default=ParticipationStatus.invited)
    match_score: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("study_id", "user_id", name="uq_study_participation"),
                      Index("ix_study_part_user_status", "user_id", "status"))


class StudyResponse(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "study_responses"
    study_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("studies.id", ondelete="CASCADE"), index=True)
    study_version: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    participant_token: Mapped[str] = mapped_column(String(64))
    client_response_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    outcome: Mapped[str] = mapped_column(String(12), default="completed")       # completed | screened_out
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float] = mapped_column(Float)
    answers: Mapped[dict] = mapped_column(JSONB)
    quality_status: Mapped[QualityStatus] = mapped_column(pg_enum(QualityStatus), default=QualityStatus.pending)
    score: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    sub_scores: Mapped[dict] = mapped_column(JSONB, default=dict)
    reasons: Mapped[list] = mapped_column(JSONB, default=list)
    evaluator: Mapped[str] = mapped_column(String(10), default="auto")
    profile_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    contact: Mapped[dict | None] = mapped_column(JSONB)
    contact_consent: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    fingerprint: Mapped[str | None] = mapped_column(String(40))
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("study_id", "user_id", name="uq_study_resp_user"),
                      UniqueConstraint("tenant_id", "user_id", "client_response_id", name="uq_study_resp_client"),
                      Index("ix_study_resp_study_status", "study_id", "quality_status"),
                      Index("ix_study_resp_fp", "study_id", "fingerprint"))


class StudyReward(Base, IdMixin, TenantMixin, CreatedMixin):
    """Registre des récompenses : de la participation à la remise, avec motif de refus ou de retrait."""
    __tablename__ = "study_rewards"
    study_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("studies.id", ondelete="CASCADE"), index=True)
    response_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("study_responses.id", ondelete="SET NULL"), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(12))
    mode: Mapped[str] = mapped_column(String(10), default="guaranteed")
    label: Mapped[str] = mapped_column(String(160))
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    unit: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[RewardStatus] = mapped_column(pg_enum(RewardStatus), default=RewardStatus.pending)
    score: Mapped[float | None] = mapped_column(Float)
    reason_code: Mapped[str | None] = mapped_column(String(40))
    reason_text: Mapped[str | None] = mapped_column(String(300))
    code: Mapped[str | None] = mapped_column(String(120))
    delivery: Mapped[dict] = mapped_column(JSONB, default=dict)
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_study_rewards_user_status", "user_id", "status"),
                      Index("ix_study_rewards_study_status", "study_id", "status"))


class StudyVoucherCode(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "study_voucher_codes"
    study_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("studies.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(120))
    reward_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("study_id", "code", name="uq_study_voucher"),)


class UserStudyProfile(Base, TenantMixin):
    """Profil déclaré par l'utilisateur, utilisé uniquement pour le ciblage (jamais transmis aux initiateurs)."""
    __tablename__ = "user_study_profiles"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    gender: Mapped[str | None] = mapped_column(String(10))
    occupation_group: Mapped[str | None] = mapped_column(String(24))
    education_level: Mapped[str | None] = mapped_column(String(24))
    household_size: Mapped[int | None] = mapped_column(SmallInteger)
    income_band: Mapped[str | None] = mapped_column(String(12))
    languages: Mapped[list] = mapped_column(JSONB, default=list)
    interests: Mapped[list] = mapped_column(JSONB, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())