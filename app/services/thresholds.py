from dataclasses import dataclass

from app.core.config import settings


@dataclass(frozen=True)
class Thresholds:
    validated_min_score: float
    indicative_min_score: float
    gps_good_m: float
    gps_reject_m: float
    min_duration_ratio: float
    max_saturation_ratio: float
    max_movement_m: float
    laeq_min_db: float
    laeq_max_db: float
    level_tolerance_db: float
    future_tolerance_s: int
    max_age_hours: int
    neighbor_z_max: float
    neighbor_min_samples: int
    neighbor_window_days: int
    neighbor_sigma_floor_db: float
    uncalibrated_subscore: float
    neighbor_neutral: float
    allow_uncalibrated_validated: bool
    public_min_devices: int
    weights: dict


def build_thresholds(overrides: dict | None = None) -> Thresholds:
    o = overrides or {}

    def pick(key, default):
        v = o.get(key)
        return default if v is None else v

    return Thresholds(
        validated_min_score=pick("validated_min_score", settings.VALIDATED_MIN_SCORE),
        indicative_min_score=pick("indicative_min_score", settings.INDICATIVE_MIN_SCORE),
        gps_good_m=settings.GPS_GOOD_M,
        gps_reject_m=pick("gps_reject_m", settings.GPS_REJECT_M),
        min_duration_ratio=settings.MIN_DURATION_RATIO,
        max_saturation_ratio=settings.MAX_SATURATION_RATIO,
        max_movement_m=pick("max_movement_m", settings.MAX_MOVEMENT_M),
        laeq_min_db=settings.LAEQ_MIN_DB,
        laeq_max_db=settings.LAEQ_MAX_DB,
        level_tolerance_db=settings.LEVEL_CONSISTENCY_TOLERANCE_DB,
        future_tolerance_s=settings.TIMESTAMP_FUTURE_TOLERANCE_SECONDS,
        max_age_hours=settings.MAX_MEASUREMENT_AGE_HOURS,
        neighbor_z_max=settings.NEIGHBOR_Z_MAX,
        neighbor_min_samples=settings.NEIGHBOR_MIN_SAMPLES,
        neighbor_window_days=settings.NEIGHBOR_WINDOW_DAYS,
        neighbor_sigma_floor_db=settings.NEIGHBOR_SIGMA_FLOOR_DB,
        uncalibrated_subscore=settings.QUALITY_UNCALIBRATED_SUBSCORE,
        neighbor_neutral=settings.QUALITY_NEIGHBOR_NEUTRAL,
        allow_uncalibrated_validated=pick("allow_uncalibrated_validated", settings.ALLOW_UNCALIBRATED_VALIDATED),
        public_min_devices=pick("public_min_devices", settings.PUBLIC_MIN_DEVICES),
        weights={"gps": settings.QW_GPS, "duration": settings.QW_DURATION, "saturation": settings.QW_SATURATION,
                 "movement": settings.QW_MOVEMENT, "calibration": settings.QW_CALIBRATION,
                 "neighbor": settings.QW_NEIGHBOR},
    )