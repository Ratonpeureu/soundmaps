import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Uuid

from app.db.base import Base
from app.models.enums import InsightKind, ReportKind, ReportStatus
from app.models.mixins import CreatedMixin, IdMixin, TenantMixin, pg_enum


class AIInsight(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "ai_insights"
    kind: Mapped[InsightKind] = mapped_column(pg_enum(InsightKind))
    h3_index: Mapped[str | None] = mapped_column(String(16), index=True)
    model: Mapped[str] = mapped_column(String(100))
    period_from: Mapped[date | None] = mapped_column(Date)
    period_to: Mapped[date | None] = mapped_column(Date)
    input_digest: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict] = mapped_column(JSONB)


class Report(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "reports"
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    kind: Mapped[ReportKind] = mapped_column(pg_enum(ReportKind))
    status: Mapped[ReportStatus] = mapped_column(pg_enum(ReportStatus), default=ReportStatus.pending)
    params: Mapped[dict] = mapped_column(JSONB)
    content: Mapped[bytes | None] = mapped_column(LargeBinary)
    content_type: Mapped[str | None] = mapped_column(String(60))
    filename: Mapped[str | None] = mapped_column(String(120))
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))