from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    APP_NAME: str = "Data Map"
    APP_ENV: Literal["development", "staging", "production"] = "development"
    API_V1_PREFIX: str = "/api/v1"
    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: str = ""
    ALLOWED_HOSTS: str = "*"
    MAX_BODY_BYTES: int = 1_048_576
    METRICS_TOKEN: SecretStr

    # PostgreSQL
    DATABASE_URL: str
    MIGRATION_DATABASE_URL: str
    APP_DB_USER: str
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # Redis / Celery
    REDIS_URL: str
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str
    CELERY_TASK_TIME_LIMIT: int = 300
    BEAT_AGGREGATION_SECONDS: int = 120
    BEAT_SWEEP_SECONDS: int = 120
    BEAT_EXPIRE_PROPOSALS_SECONDS: int = 60
    BEAT_ADAPT_WEIGHTS_SECONDS: int = 3600
    BEAT_PRIVACY_SECONDS: int = 86400
    BEAT_AI_DIGEST_SECONDS: int = 86400
    BEAT_QUOTA_SECONDS: int = 1800
    BEAT_MISSIONS_SECONDS: int = 10800
    BEAT_MISSIONS_RECONCILE_SECONDS: int = 600

    # Sécurité
    JWT_SECRET_KEY: SecretStr
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    HASH_PEPPER: SecretStr
    PSEUDONYM_SALT: SecretStr
    PLATFORM_ADMIN_KEY: SecretStr
    PASSWORD_MIN_LENGTH: int = 10
    LOGIN_MAX_ATTEMPTS: int = 5
    LOGIN_LOCK_SECONDS: int = 900
    RATE_LIMIT_AUTH_PER_MINUTE: int = 10
    RATE_LIMIT_API_PER_MINUTE: int = 120
    RATE_LIMIT_MEASUREMENTS_PER_MINUTE: int = 60
    RATE_LIMIT_AI_PER_MINUTE: int = 10

    # API
    TOMTOM_API_KEY: SecretStr = SecretStr("")
    TOMTOM_BASE_URL: str = "https://api.tomtom.com"
    TOMTOM_COUNTRY_SET: str = "SN"
    TOMTOM_LANGUAGE: str = "fr-FR"
    TOMTOM_TIMEOUT_SECONDS: float = 8
    TOMTOM_TILE_TOKEN_TTL_SECONDS: int = 900
    TOMTOM_CACHE_TTL_SECONDS: int = 60
    TOMTOM_SEARCH_CACHE_TTL_SECONDS: int = 3600
    RATE_LIMIT_TOMTOM_PER_MINUTE: int = 60
    RATE_LIMIT_TILES_PER_MINUTE: int = 1200

    # Géographie
    H3_RESOLUTION: int = 9
    TIMEZONE: str = "Africa/Dakar"
    DAYPART_MORNING_START: int = 6
    DAYPART_AFTERNOON_START: int = 12
    DAYPART_EVENING_START: int = 18
    DAYPART_NIGHT_START: int = 22
    MAP_MAX_CELLS: int = 5000
    MAX_RANGE_DAYS: int = 366

    # Collecte
    SESSION_DURATION_SECONDS: int = 15
    PROPOSAL_TTL_MINUTES: int = 30
    PROPOSAL_COMPLETION_GRACE_MINUTES: int = 30
    MIN_BATTERY_LEVEL: int = 15
    MAX_PROPOSALS_PER_USER_PER_DAY: int = 6
    MIN_MINUTES_BETWEEN_PROPOSALS: int = 30
    SCHEDULED_TOLERANCE_MINUTES: int = 15
    CANDIDATE_RING_K: int = 3
    AVAILABILITY_WINDOW_MINUTES: int = 30
    BATCH_MAX_ITEMS: int = 200

    # Algorithme adaptatif
    W_FRESHNESS: float = 0.30
    W_NOISE_HISTORY: float = 0.25
    W_COVERAGE_GAP: float = 0.20
    W_TIME_RELEVANCE: float = 0.15
    W_AVAILABILITY: float = 0.10
    FRESHNESS_TAU_HOURS: float = 12
    NOISE_REF_MIN_DB: float = 40
    NOISE_REF_MAX_DB: float = 85
    NOISE_STD_REF_DB: float = 10
    NOISE_LEVEL_SHARE: float = 0.6
    UNKNOWN_HISTORY_PRIOR: float = 0.5
    COVERAGE_WINDOW_DAYS: int = 7
    HISTORY_WINDOW_DAYS: int = 30
    TARGET_MEASUREMENTS_PER_CELL: int = 20
    PROPOSAL_MIN_SCORE: float = 0.35
    PROPOSAL_PERCENTILE: float = 0.6
    MIN_RING_CELLS_FOR_PERCENTILE: int = 5
    EXPLORATION_EPSILON: float = 0.05
    ACCEPTANCE_PRIOR: float = 0.5
    ACCEPTANCE_PRIOR_STRENGTH: float = 4
    ADAPT_LEARNING_RATE: float = 0.5
    ADAPT_SHRINKAGE: float = 0.1
    ADAPT_MIN_WEIGHT: float = 0.05
    ADAPT_MAX_WEIGHT: float = 0.5
    ADAPT_MIN_SAMPLES: int = 30
    ADAPT_BATCH: int = 500

    # Quota dynamique (solde cumulable)
    QUOTA_ENABLED: bool = True
    QUOTA_INITIAL_BALANCE: float = 3.0
    QUOTA_MIN_DAILY: float = 2.0
    QUOTA_MAX_DAILY: float = 40.0
    QUOTA_COLD_START_DAILY: float = 10.0
    QUOTA_COLD_START_CELLS: int = 10
    QUOTA_MIN_CAP: float = 6.0
    QUOTA_CAP_DAYS: float = 2.5
    QUOTA_COST_MIN: float = 0.5
    QUOTA_COST_MAX: float = 2.0
    QUOTA_COOLDOWN_MIN_MINUTES: float = 3
    QUOTA_COOLDOWN_MAX_MINUTES: float = 180
    QUOTA_RETENTION_MIN: float = 0.55
    QUOTA_RETENTION_MAX: float = 0.92
    QUOTA_RELIABILITY_PRIOR: float = 0.6
    QUOTA_RELIABILITY_STRENGTH: float = 5
    QUOTA_LAZY_REFRESH_SECONDS: int = 300
    QUOTA_HARD_TOLERANCE_MINUTES: int = 15
    QUOTA_CONTEXT_TTL_SECONDS: int = 600
    QUOTA_BATCH: int = 2000
    QUOTA_DAYS_RETENTION: int = 90

    # Missions
    MISSION_OFFER_HOURS: int = 48
    MISSION_COMPLETION_HOURS: int = 72
    MISSION_MAX_RADIUS_KM: float = 25
    MISSION_ZONE_H3_RES: int = 7
    MISSION_MIN_COUNT: int = 2
    MISSION_MAX_COUNT: int = 10
    MISSION_MAX_PENDING: int = 10
    MISSION_MAX_ACTIVE: int = 5
    MISSION_MIN_SCORE: float = 0.35
    MISSION_MIN_ZONE_DEFICIT: int = 6
    MISSION_MAX_ZONES: int = 150
    MISSION_USERS_PER_ZONE: int = 3
    MISSION_MAX_NEW_PER_USER_PER_RUN: int = 2
    MISSION_REWARD_PER_MEASUREMENT: float = 0.5
    MISSION_DECLINE_COOLDOWN_DAYS: int = 7
    MISSION_ACTIVITY_TARGET: int = 20
    MISSION_SYNC_MIN_MINUTES: int = 60
    MS_W_ACTIVITY: float = 0.35
    MS_W_RELIABILITY: float = 0.30
    MS_W_COMPLETION: float = 0.20
    MS_W_ACCEPTANCE: float = 0.15

    # Historique animé
    TIMELAPSE_MAX_FRAMES: int = 120
    TIMELAPSE_MAX_ROWS: int = 200000
    TIMELAPSE_CACHE_TTL_SECONDS: int = 300

    # Qualité
    VALIDATED_MIN_SCORE: float = 0.75
    INDICATIVE_MIN_SCORE: float = 0.45
    ALLOW_UNCALIBRATED_VALIDATED: bool = False
    GPS_GOOD_M: float = 20
    GPS_REJECT_M: float = 100
    MIN_DURATION_RATIO: float = 0.9
    MAX_SATURATION_RATIO: float = 0.05
    MAX_MOVEMENT_M: float = 30
    LAEQ_MIN_DB: float = 20
    LAEQ_MAX_DB: float = 140
    LEVEL_CONSISTENCY_TOLERANCE_DB: float = 0.5
    TIMESTAMP_FUTURE_TOLERANCE_SECONDS: int = 300
    MAX_MEASUREMENT_AGE_HOURS: int = 168
    NEIGHBOR_Z_MAX: float = 3.0
    NEIGHBOR_MIN_SAMPLES: int = 5
    NEIGHBOR_WINDOW_DAYS: int = 14
    NEIGHBOR_SIGMA_FLOOR_DB: float = 3.0
    QUALITY_UNCALIBRATED_SUBSCORE: float = 0.5
    QUALITY_NEIGHBOR_NEUTRAL: float = 0.7
    QW_GPS: float = 0.25
    QW_DURATION: float = 0.20
    QW_SATURATION: float = 0.15
    QW_MOVEMENT: float = 0.10
    QW_CALIBRATION: float = 0.15
    QW_NEIGHBOR: float = 0.15
    REEVALUATION_DAYS: int = 30
    CONF_TARGET_MEASUREMENTS: int = 20
    CONF_TARGET_DEVICES: int = 5
    CONF_RECENCY_TAU_HOURS: float = 72
    CONF_W_COUNT: float = 0.40
    CONF_W_DEVICES: float = 0.25
    CONF_W_CALIBRATION: float = 0.20
    CONF_W_RECENCY: float = 0.15
    PUBLIC_MIN_DEVICES: int = 3
    DEFAULT_TENANT_SLUG: str = "sunusound"

    # Confidentialité
    LOCATION_RETENTION_DAYS: int = 30
    IDENTITY_UNLINK_DAYS: int = 90
    REPORT_RETENTION_DAYS: int = 30
    AUDIT_RETENTION_DAYS: int = 365
    CONSENT_POLICY_VERSION: str = "1"

    # Groq
    API_GROQ_KEY: SecretStr = SecretStr("")
    GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    GROQ_MODEL_ANALYSIS: str = ""
    GROQ_MODEL_REPORT: str = ""
    GROQ_TIMEOUT_SECONDS: float = 30
    GROQ_MAX_TOKENS: int = 1200
    GROQ_TEMPERATURE: float = 0.2
    GROQ_MAX_RETRIES: int = 3
    AI_CACHE_TTL_SECONDS: int = 86400
    ANOMALY_Z_THRESHOLD: float = 2.5
    ANOMALY_MIN_BASELINE_DAYS: int = 3
    ANOMALY_BASELINE_DAYS: int = 7
    AI_DIGEST_MAX_CELLS: int = 10

    @model_validator(mode="after")
    def _validate(self):
        w = (self.W_FRESHNESS + self.W_NOISE_HISTORY + self.W_COVERAGE_GAP
             + self.W_TIME_RELEVANCE + self.W_AVAILABILITY)
        if abs(w - 1.0) > 1e-6:
            raise ValueError("Les poids W_* doivent totaliser 1.0")
        q = (self.QW_GPS + self.QW_DURATION + self.QW_SATURATION + self.QW_MOVEMENT
             + self.QW_CALIBRATION + self.QW_NEIGHBOR)
        if abs(q - 1.0) > 1e-6:
            raise ValueError("Les poids QW_* doivent totaliser 1.0")
        c = self.CONF_W_COUNT + self.CONF_W_DEVICES + self.CONF_W_CALIBRATION + self.CONF_W_RECENCY
        if abs(c - 1.0) > 1e-6:
            raise ValueError("Les poids CONF_W_* doivent totaliser 1.0")
        ms = self.MS_W_ACTIVITY + self.MS_W_RELIABILITY + self.MS_W_COMPLETION + self.MS_W_ACCEPTANCE
        if abs(ms - 1.0) > 1e-6:
            raise ValueError("Les poids MS_W_* doivent totaliser 1.0")
        if not (0 <= self.DAYPART_MORNING_START < self.DAYPART_AFTERNOON_START
                < self.DAYPART_EVENING_START < self.DAYPART_NIGHT_START <= 23):
            raise ValueError("DAYPART_* doivent être strictement croissants (0-23)")
        if self.INDICATIVE_MIN_SCORE > self.VALIDATED_MIN_SCORE:
            raise ValueError("INDICATIVE_MIN_SCORE doit être <= VALIDATED_MIN_SCORE")
        if (self.QUOTA_COST_MIN > self.QUOTA_COST_MAX
                or self.QUOTA_COOLDOWN_MIN_MINUTES > self.QUOTA_COOLDOWN_MAX_MINUTES
                or self.QUOTA_RETENTION_MIN > self.QUOTA_RETENTION_MAX
                or self.QUOTA_MIN_DAILY > self.QUOTA_MAX_DAILY):
            raise ValueError("Bornes QUOTA_* incohérentes")
        if self.MISSION_ZONE_H3_RES >= self.H3_RESOLUTION:
            raise ValueError("MISSION_ZONE_H3_RES doit être < H3_RESOLUTION")
        if self.MISSION_MAX_RADIUS_KM > 25:
            raise ValueError("MISSION_MAX_RADIUS_KM doit rester <= 25")
        if self.APP_ENV == "production":
            for name in ("JWT_SECRET_KEY", "HASH_PEPPER", "PSEUDONYM_SALT",
                         "PLATFORM_ADMIN_KEY", "METRICS_TOKEN"):
                if len(getattr(self, name).get_secret_value()) < 32:
                    raise ValueError(f"{name} doit contenir au moins 32 caractères en production")
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.ALLOWED_HOSTS.split(",") if h.strip()] or ["*"]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()