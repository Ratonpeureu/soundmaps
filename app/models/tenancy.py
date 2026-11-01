import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ConsentType, Role
from app.models.mixins import CreatedMixin, IdMixin, TenantMixin, pg_enum


class Tenant(Base, IdMixin, CreatedMixin):
    __tablename__ = "tenants"
    slug: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    allow_registration: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    settings: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))


class User(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "users"
    email_hash: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(pg_enum(Role), default=Role.contributor)
    pseudonym: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    participation_paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    hybrid_collection_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "email_hash", name="uq_users_tenant_email"),
        UniqueConstraint("tenant_id", "pseudonym", name="uq_users_tenant_pseudonym"),
    )


class UserConsent(Base, IdMixin, TenantMixin, CreatedMixin):
    """Historique append-only des consentements."""
    __tablename__ = "user_consents"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    consent_type: Mapped[ConsentType] = mapped_column(pg_enum(ConsentType))
    granted: Mapped[bool] = mapped_column(Boolean)
    policy_version: Mapped[str] = mapped_column(String(20))
    __table_args__ = (Index("ix_consents_user_type_created", "user_id", "consent_type", "created_at"),)


class AuditLog(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "audit_logs"
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(100), index=True)
    resource_type: Mapped[str] = mapped_column(String(50))
    resource_id: Mapped[str | None] = mapped_column(String(100))
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))