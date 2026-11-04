import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import DayPart, MissionStatus
from app.models.mixins import CreatedMixin, IdMixin, TenantMixin, pg_enum


class UserQuota(Base, TenantMixin):
    """Solde cumulable de prélèvements d'un utilisateur (crédits décimaux)."""
    __tablename__ = "user_quotas"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    balance: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    daily_target: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    rate_per_hour: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    reliability: Mapped[float] = mapped_column(Float, default=0.6, server_default=text("0.6"))
    utilization: Mapped[float] = mapped_column(Float, default=0.7, server_default=text("0.7"))
    mission_rate: Mapped[float] = mapped_column(Float, default=0.5, server_default=text("0.5"))
    retention_per_day: Mapped[float] = mapped_column(Float, default=0.8, server_default=text("0.8"))
    cap: Mapped[float] = mapped_column(Float, default=6.0, server_default=text("6"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_user_quotas_updated", "tenant_id", "updated_at"),)


class QuotaDay(Base, IdMixin, TenantMixin):
    """Agrégat journalier (crédité / utilisé / perdu) : sert au calcul du taux d'utilisation."""
    __tablename__ = "quota_days"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    local_date: Mapped[date] = mapped_column(Date)
    credited: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    used: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    decayed: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    measurements: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    target: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    __table_args__ = (UniqueConstraint("user_id", "local_date", name="uq_quota_days_user_date"),)


class Mission(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "missions"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(8), default="zone", server_default=text("'zone'"))   # zone | road
    status: Mapped[MissionStatus] = mapped_column(pg_enum(MissionStatus), default=MissionStatus.offered)
    zone_h3: Mapped[str] = mapped_column(String(16))
    center_lat: Mapped[float] = mapped_column(Float)       # mission route : point cible précis
    center_lon: Mapped[float] = mapped_column(Float)
    radius_m: Mapped[float] = mapped_column(Float)         # mission route : tolérance autour du point cible
    distance_km: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    required_count: Mapped[int] = mapped_column(Integer)
    done_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    confirmed_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    reward_credits: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    priority: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    features: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    road_segment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("road_segments.id", ondelete="SET NULL"))
    road_name: Mapped[str | None] = mapped_column(String(160))
    road_class: Mapped[str | None] = mapped_column(String(24))
    target_day_part: Mapped[DayPart | None] = mapped_column(pg_enum(DayPart))
    window_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offer_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_missions_user_status", "user_id", "status"),
        Index("ix_missions_tenant_status_expires", "tenant_id", "status", "offer_expires_at"),
        Index("ix_missions_tenant_kind_status", "tenant_id", "kind", "status"),
    )