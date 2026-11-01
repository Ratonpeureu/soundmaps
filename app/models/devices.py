import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import CalibrationStatus
from app.models.mixins import CreatedMixin, IdMixin, TenantMixin, pg_enum


class DeviceModel(Base, IdMixin, TenantMixin, CreatedMixin):
    """Registre des modèles de téléphones."""
    __tablename__ = "device_models"
    manufacturer: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(120))
    android_version: Mapped[str] = mapped_column(String(20))
    microphone: Mapped[str] = mapped_column(String(40), default="internal", server_default="internal")
    calibration_status: Mapped[CalibrationStatus] = mapped_column(
        pg_enum(CalibrationStatus), default=CalibrationStatus.uncalibrated)
    correction_db: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("tenant_id", "manufacturer", "model", "android_version", "microphone",
                                       name="uq_device_models_identity"),)


class DeviceCalibration(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "device_calibrations"
    device_model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("device_models.id", ondelete="CASCADE"), index=True)
    correction_db: Mapped[float] = mapped_column(Float)
    reference_instrument: Mapped[str] = mapped_column(String(200))
    conditions: Mapped[str | None] = mapped_column(Text)
    calibrated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class Device(Base, IdMixin, TenantMixin, CreatedMixin):
    """Installation d'un utilisateur ; l'identifiant d'installation n'est conservé que haché."""
    __tablename__ = "devices"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    device_model_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("device_models.id", ondelete="RESTRICT"))
    device_hash: Mapped[str] = mapped_column(String(64))
    sample_rate_hz: Mapped[int | None] = mapped_column()
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "device_hash", name="uq_devices_tenant_hash"),)