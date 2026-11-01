import uuid
from datetime import date, datetime

from geoalchemy2 import Geometry
from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, SmallInteger, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import DayPart, QualityStatus
from app.models.mixins import CreatedMixin, IdMixin, TenantMixin, pg_enum


class GeographicCell(Base, IdMixin, TenantMixin, CreatedMixin):
    __tablename__ = "geographic_cells"
    h3_index: Mapped[str] = mapped_column(String(16))
    resolution: Mapped[int] = mapped_column(SmallInteger)
    center = mapped_column(Geometry("POINT", srid=4326), nullable=False)
    last_measurement_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "h3_index", name="uq_cells_tenant_h3"),)


class NoiseAggregation(Base, IdMixin, TenantMixin):
    """Agrégat énergétique par cellule, jour local, plage horaire et classe de qualité."""
    __tablename__ = "noise_aggregations"
    cell_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("geographic_cells.id", ondelete="CASCADE"), index=True)
    bucket_date: Mapped[date] = mapped_column(Date)
    day_part: Mapped[DayPart] = mapped_column(pg_enum(DayPart))
    quality_status: Mapped[QualityStatus] = mapped_column(pg_enum(QualityStatus))
    measurement_count: Mapped[int] = mapped_column(Integer)
    device_count: Mapped[int] = mapped_column(Integer)
    calibrated_count: Mapped[int] = mapped_column(Integer)
    duration_total: Mapped[float] = mapped_column(Float)
    energy_sum: Mapped[float] = mapped_column(Float)      # somme(durée x 10^(L/10))
    laeq_db: Mapped[float] = mapped_column(Float)
    lmax_db: Mapped[float] = mapped_column(Float)
    last_measurement_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "cell_id", "bucket_date", "day_part", "quality_status",
                                       name="uq_noise_agg_bucket"),)