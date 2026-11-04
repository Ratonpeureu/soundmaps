import uuid
from datetime import date, datetime

from sqlalchemy import (Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, SmallInteger, String,
                        UniqueConstraint, Uuid, func, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import CreatedMixin, IdMixin, TenantMixin


def _user_fk(nullable: bool = False, set_null: bool = False):
    return mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL" if set_null else "CASCADE"),
                         nullable=nullable)


class UserProfile(Base, TenantMixin):
    """Profil enrichi : données privées (nom, téléphone…) jamais exposées aux autres utilisateurs."""
    __tablename__ = "user_profiles"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(32))
    display_key: Mapped[str] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(60))
    last_name: Mapped[str | None] = mapped_column(String(60))
    phone: Mapped[str | None] = mapped_column(String(24))
    city: Mapped[str | None] = mapped_column(String(80))
    district: Mapped[str | None] = mapped_column(String(80))
    birth_year: Mapped[int | None] = mapped_column(SmallInteger)
    invited_by: Mapped[uuid.UUID | None] = _user_fk(nullable=True, set_null=True)
    invitation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    invite_credited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    show_in_leaderboard: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    analyst_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    analyst_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analyst_suspended_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    xp: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    xp_through: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("tenant_id", "display_key", name="uq_profiles_tenant_display"),
                      Index("ix_profiles_invited_by", "invited_by"))


class Invitation(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "invitations"
    code: Mapped[str] = mapped_column(String(24))
    created_by: Mapped[uuid.UUID | None] = _user_fk(nullable=True, set_null=True)
    kind: Mapped[str] = mapped_column(String(10))                     # admin | user
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    uses: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    note: Mapped[str | None] = mapped_column(String(120))
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_invitations_tenant_code"),)


class LoginEvent(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "login_events"
    user_id: Mapped[uuid.UUID] = _user_fk()
    success: Mapped[bool] = mapped_column(Boolean)
    user_agent: Mapped[str | None] = mapped_column(String(200))
    platform: Mapped[str | None] = mapped_column(String(30))
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (Index("ix_login_events_user_created", "user_id", "created_at"),)


class RecoveryCode(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "recovery_codes"
    user_id: Mapped[uuid.UUID] = _user_fk()
    code_hash: Mapped[str] = mapped_column(String(64))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_recovery_codes_user", "user_id"),)


class LeaderboardEntry(Base, IdMixin, TenantMixin):
    __tablename__ = "leaderboard_entries"
    period_kind: Mapped[str] = mapped_column(String(8))
    period_key: Mapped[str] = mapped_column(String(12))
    user_id: Mapped[uuid.UUID] = _user_fk()
    score: Mapped[float] = mapped_column(Float)
    rank: Mapped[int] = mapped_column(Integer)                        # 0 = classement masqué par l'utilisateur
    reliability: Mapped[float] = mapped_column(Float)
    components: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "period_kind", "period_key", "user_id", name="uq_lb_entry"),
                      Index("ix_lb_entry_rank", "tenant_id", "period_kind", "period_key", "rank"))


class LeaderboardSnapshot(Base, IdMixin, TenantMixin):
    __tablename__ = "leaderboard_snapshots"
    user_id: Mapped[uuid.UUID] = _user_fk()
    period_kind: Mapped[str] = mapped_column(String(8))
    snap_date: Mapped[date] = mapped_column(Date)
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("user_id", "period_kind", "snap_date", name="uq_lb_snapshot"),)


class UserBadge(Base, IdMixin, TenantMixin):
    __tablename__ = "user_badges"
    user_id: Mapped[uuid.UUID] = _user_fk()
    code: Mapped[str] = mapped_column(String(40))
    awarded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("user_id", "code", name="uq_user_badge"),)


class MeasurementReview(Base, IdMixin, TenantMixin, CreatedMixin):
    """Une manche d'avis en temps réel sur une mesure."""
    __tablename__ = "measurement_reviews"
    measurement_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("measurements.id", ondelete="CASCADE"),
                                                      unique=True)
    status: Mapped[str] = mapped_column(String(10), default="open")       # open | closed
    outcome: Mapped[str | None] = mapped_column(String(20))               # confirmed | adjusted_up | adjusted_down | rejected | no_quorum | admin_skip
    adjust_db: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    requested: Mapped[int] = mapped_column(Integer, default=0)
    votes: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    agree_ratio: Mapped[float | None] = mapped_column(Float)
    down_ratio: Mapped[float | None] = mapped_column(Float)
    up_ratio: Mapped[float | None] = mapped_column(Float)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_reviews_status_expires", "tenant_id", "status", "expires_at"),)


class ReviewVote(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "review_votes"
    review_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("measurement_reviews.id", ondelete="CASCADE"))
    reviewer_id: Mapped[uuid.UUID] = _user_fk()
    status: Mapped[str] = mapped_column(String(10), default="pending")    # pending | voted | expired | cancelled
    vote: Mapped[str | None] = mapped_column(String(8))                   # agree | down | up
    strong: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    level_db: Mapped[float] = mapped_column(Float)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    distance_m: Mapped[float] = mapped_column(Float)
    weight: Mapped[float] = mapped_column(Float)
    matched: Mapped[bool | None] = mapped_column(Boolean)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    voted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("review_id", "reviewer_id", name="uq_review_vote"),
                      Index("ix_review_votes_reviewer", "reviewer_id", "status"))