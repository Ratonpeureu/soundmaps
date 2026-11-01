import uuid
from datetime import datetime, time

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Index, Integer, SmallInteger, String,
                        Time, UniqueConstraint, Uuid, text)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ProposalStatus, ScheduleMode, TriggerMode
from app.models.mixins import CreatedMixin, IdMixin, TenantMixin, pg_enum


class CollectionSchedule(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "collection_schedules"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    days_of_week: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger))     # 0 = lundi
    window_start: Mapped[time] = mapped_column(Time)
    window_end: Mapped[time] = mapped_column(Time)
    preferred_times: Mapped[list[time]] = mapped_column(ARRAY(Time), default=list, server_default=text("'{}'"))
    mode: Mapped[ScheduleMode] = mapped_column(pg_enum(ScheduleMode), default=ScheduleMode.both)
    timezone: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class CollectionProposal(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "collection_proposals"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    cell_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("geographic_cells.id", ondelete="CASCADE"))
    h3_index: Mapped[str] = mapped_column(String(16))
    trigger: Mapped[TriggerMode] = mapped_column(pg_enum(TriggerMode))
    status: Mapped[ProposalStatus] = mapped_column(pg_enum(ProposalStatus), default=ProposalStatus.pending)
    priority_score: Mapped[float] = mapped_column(Float)
    decision_score: Mapped[float] = mapped_column(Float)
    is_exploration: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    features: Mapped[dict] = mapped_column(JSONB)
    weights_used: Mapped[dict] = mapped_column(JSONB)
    confidence_before: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    learned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_proposals_user_created", "user_id", "created_at"),
        Index("ix_proposals_status_expires", "tenant_id", "status", "expires_at"),
    )


class TenantAlgorithmState(Base, TenantMixin):
    """Poids adaptatifs de l'algorithme de priorité, par tenant."""
    __tablename__ = "tenant_algorithm_state"
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    weights: Mapped[dict] = mapped_column(JSONB)
    samples_used: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserResponseStat(Base, IdMixin, TenantMixin):
    """Acceptations / refus par utilisateur et heure locale (modèle bêta-binomial à deux niveaux)."""
    __tablename__ = "user_response_stats"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    hour: Mapped[int] = mapped_column(SmallInteger)
    accepted: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    declined: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    __table_args__ = (UniqueConstraint("user_id", "hour", name="uq_response_stats_user_hour"),)