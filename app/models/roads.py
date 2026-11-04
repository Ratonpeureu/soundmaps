import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import (BigInteger, Boolean, DateTime, Float, Index, Integer, SmallInteger, String,
                        UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin


class RoadSegment(Base, IdMixin):
    """Tronçon de route réel (OpenStreetMap). Donnée géographique publique, partagée par tous les tenants
    (sans tenant_id ni RLS) : l'état de bruit par tenant est calculé à partir des mesures."""
    __tablename__ = "road_segments"
    osm_way_id: Mapped[int] = mapped_column(BigInteger)
    seq: Mapped[int] = mapped_column(Integer)
    start_node: Mapped[int] = mapped_column(BigInteger)          # nœuds OSM : conservent la connectivité
    end_node: Mapped[int] = mapped_column(BigInteger)
    highway: Mapped[str] = mapped_column(String(24))
    road_rank: Mapped[int] = mapped_column(SmallInteger)          # 1 = autoroute/nationale ... 6 = résidentielle
    class_weight: Mapped[float] = mapped_column(Float)            # poids de trafic attendu (0-1)
    name: Mapped[str | None] = mapped_column(String(160))
    ref: Mapped[str | None] = mapped_column(String(60))
    oneway: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    maxspeed: Mapped[str | None] = mapped_column(String(20))
    lanes: Mapped[int | None] = mapped_column(SmallInteger)
    length_m: Mapped[float] = mapped_column(Float)
    mid_lat: Mapped[float] = mapped_column(DOUBLE_PRECISION)      # point cible des missions
    mid_lon: Mapped[float] = mapped_column(DOUBLE_PRECISION)
    h3_index: Mapped[str] = mapped_column(String(16))
    geom = mapped_column(Geometry("LINESTRING", srid=4326), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("osm_way_id", "seq", name="uq_road_segments_way_seq"),
        Index("ix_road_segments_start_node", "start_node"),
        Index("ix_road_segments_end_node", "end_node"),
        Index("ix_road_segments_rank", "road_rank"),
        Index("ix_road_segments_h3", "h3_index"),
    )


class RoadImportTile(Base):
    """Suivi de l'import par tuiles (reprise possible)."""
    __tablename__ = "road_import_tiles"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    west: Mapped[float] = mapped_column(DOUBLE_PRECISION)
    south: Mapped[float] = mapped_column(DOUBLE_PRECISION)
    east: Mapped[float] = mapped_column(DOUBLE_PRECISION)
    north: Mapped[float] = mapped_column(DOUBLE_PRECISION)
    size_deg: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(10), default="pending")   # pending|queued|running|done|split|failed
    ways: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    segments: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_road_tiles_status", "status"),)