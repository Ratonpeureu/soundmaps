import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.enums import DayPart, MissionStatus


class QuotaOut(BaseModel):
    balance: float
    measurements_left: int
    daily_target: float
    rate_per_hour: float
    cap: float
    reliability: float
    utilization: float
    mission_rate: float
    retention_per_day: float
    need_now: float
    projected_24h: float
    today_credited: float
    today_used: float
    today_decayed: float


class QuotaCheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    proposal_id: uuid.UUID | None = None


class QuotaCheckOut(BaseModel):
    allowed: bool
    reason: str
    cost: float
    balance: float
    cooldown_minutes: float
    retry_at: datetime | None
    cell_need: float
    free: bool


class MissionTarget(BaseModel):
    """Point précis à rejoindre (trajet calculé côté mobile). Une mesure à moins de radius_m est valide."""
    lat: float
    lon: float
    radius_m: float
    day_part: DayPart | None = None
    window_start_at: datetime | None = None
    window_end_at: datetime | None = None
    segment_id: uuid.UUID | None = None
    road_name: str | None = None
    road_class: str | None = None


class MissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: str
    status: MissionStatus
    zone_h3: str
    center_lat: float
    center_lon: float
    radius_m: float
    distance_km: float
    required_count: int
    done_count: int
    confirmed_count: int
    reward_credits: float
    priority: float
    road_segment_id: uuid.UUID | None = None
    road_name: str | None = None
    road_class: str | None = None
    target_day_part: DayPart | None = None
    window_start_at: datetime | None = None
    window_end_at: datetime | None = None
    created_at: datetime
    offer_expires_at: datetime
    accepted_at: datetime | None
    deadline_at: datetime | None
    finished_at: datetime | None

    @computed_field
    @property
    def target(self) -> MissionTarget | None:
        if self.kind != "road":
            return None
        return MissionTarget(lat=self.center_lat, lon=self.center_lon, radius_m=self.radius_m,
                             day_part=self.target_day_part, window_start_at=self.window_start_at,
                             window_end_at=self.window_end_at, segment_id=self.road_segment_id,
                             road_name=self.road_name, road_class=self.road_class)


class MissionSyncIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)