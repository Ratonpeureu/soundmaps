import uuid
from datetime import date, datetime

from geoalchemy2 import Geometry
from sqlalchemy import (Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, String,
                        UniqueConstraint, Uuid, text)
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import CalibrationStatus, DayPart, NoiseContext, QualityStatus
from app.models.mixins import CreatedMixin, IdMixin, TenantMixin, pg_enum


class Measurement(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "measurements"
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("devices.id", ondelete="SET NULL"))
    device_model_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("device_models.id", ondelete="SET NULL"), index=True)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("collection_proposals.id", ondelete="SET NULL"), unique=True)
    cell_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("geographic_cells.id", ondelete="RESTRICT"))
    client_measurement_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    contributor_hash: Mapped[str] = mapped_column(String(64), index=True)

    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    local_date: Mapped[date] = mapped_column(Date)
    day_part: Mapped[DayPart] = mapped_column(pg_enum(DayPart))

    latitude: Mapped[float] = mapped_column(DOUBLE_PRECISION)
    longitude: Mapped[float] = mapped_column(DOUBLE_PRECISION)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=False)
    h3_index: Mapped[str] = mapped_column(String(16))
    gps_accuracy: Mapped[float] = mapped_column(Float)
    location_anonymized: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    # Rattachement au réseau routier (renseigné par la tâche roads.match)
    road_segment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("road_segments.id", ondelete="SET NULL"))
    road_distance_m: Mapped[float | None] = mapped_column(Float)
    road_matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    duration_seconds: Mapped[float] = mapped_column(Float)
    effective_duration_seconds: Mapped[float] = mapped_column(Float)
    sample_rate_hz: Mapped[int | None] = mapped_column(Integer)
    laeq: Mapped[float] = mapped_column(Float)
    lmax: Mapped[float] = mapped_column(Float)
    lmin: Mapped[float] = mapped_column(Float)
    saturation_ratio: Mapped[float] = mapped_column(Float)
    moved_distance_m: Mapped[float] = mapped_column(Float)
    noise_context: Mapped[NoiseContext | None] = mapped_column(pg_enum(NoiseContext))

    correction_db_applied: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    calibration_status: Mapped[CalibrationStatus] = mapped_column(
        pg_enum(CalibrationStatus), default=CalibrationStatus.uncalibrated)
    quality_status: Mapped[QualityStatus] = mapped_column(pg_enum(QualityStatus), default=QualityStatus.pending)
    audio_retained: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    aggregated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "client_measurement_id", name="uq_measurement_client_id"),
        CheckConstraint("audio_retained = false", name="no_raw_audio"),
        Index("ix_meas_tenant_status_time", "tenant_id", "quality_status", "measured_at"),
        Index("ix_meas_tenant_h3_time", "tenant_id", "h3_index", "measured_at"),
        Index("ix_meas_tenant_aggregated", "tenant_id", "aggregated_at"),
        Index("ix_meas_tenant_road_time", "tenant_id", "road_segment_id", "measured_at"),
        Index("ix_meas_tenant_road_matched", "tenant_id", "road_matched_at"),
    )


class MeasurementQuality(Base, IdMixin, TenantMixin):
    __tablename__ = "measurement_quality"
    measurement_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("measurements.id", ondelete="CASCADE"), unique=True)
    status: Mapped[QualityStatus] = mapped_column(pg_enum(QualityStatus))
    score: Mapped[float] = mapped_column(Float)
    sub_scores: Mapped[dict] = mapped_column(JSONB, default=dict)
    reasons: Mapped[list] = mapped_column(JSONB, default=list)
    evaluator: Mapped[str] = mapped_column(String(10), default="auto")   # auto | admin
    override_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))