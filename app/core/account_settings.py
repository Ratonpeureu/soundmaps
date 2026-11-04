from pydantic_settings import BaseSettings, SettingsConfigDict


class AccountSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Comptes
    INVITE_REQUIRED: bool = False
    INVITE_CODE_LENGTH: int = 10
    INVITE_USER_MAX_ACTIVE: int = 3
    INVITE_USER_MAX_USES: int = 5
    INVITE_USER_TTL_DAYS: int = 14
    RECOVERY_CODES_COUNT: int = 8
    LOGIN_EVENTS_LIST: int = 15
    LOGIN_EVENTS_RETENTION_DAYS: int = 180
    RATE_LIMIT_PWRESET_PER_MINUTE: int = 5

    # Analyseurs
    ANALYST_MIN_VALIDATED: int = 20
    ANALYST_MIN_MISSIONS: int = 2
    ANALYST_MIN_RELIABILITY: float = 0.75
    ANALYST_MIN_ACCOUNT_DAYS: int = 30
    ANALYST_MIN_ACTIVE_DAYS: int = 8
    ANALYST_MAX_INACTIVE_DAYS: int = 15
    ANALYST_SUSPEND_MIN_VOTES: int = 20
    ANALYST_SUSPEND_AGREEMENT: float = 0.4
    ANALYST_SUSPEND_DAYS: int = 14

    # Avis en temps réel
    REVIEW_RADIUS_M: float = 500
    REVIEW_TTL_MINUTES: int = 10
    REVIEW_MAX_AGE_MINUTES: int = 10
    REVIEW_MAX_REVIEWERS: int = 7
    REVIEW_MIN_VOTES: int = 3
    REVIEW_MAX_PENDING_PER_USER: int = 5
    REVIEW_CONFIRM_RATIO: float = 0.6
    REVIEW_ADJUST_RATIO: float = 0.5
    REVIEW_STRONG_REJECT_SHARE: float = 0.6
    REVIEW_ADJUST_STEP_DB: float = 2.0
    REVIEW_ADJUST_MAX_DB: float = 3.0
    PRESENCE_TTL_SECONDS: int = 180

    # Classement
    LB_PTS_VALIDATED: float = 10.0
    LB_PTS_INDICATIVE: float = 4.0
    LB_PTS_REJECTED: float = -2.0
    LB_PTS_CELL: float = 3.0
    LB_PTS_DAY: float = 5.0
    LB_PTS_MISSION: float = 40.0
    LB_PTS_MISSION_UNIT: float = 4.0
    LB_PTS_REVIEW: float = 2.0
    LB_PTS_INVITE: float = 25.0
    LB_RELIABILITY_PRIOR: float = 0.6
    LB_RELIABILITY_STRENGTH: float = 5.0
    LB_MAX_LIMIT: int = 100

    # Tâches planifiées (secondes)
    BEAT_REVIEWS_SECONDS: int = 60
    BEAT_LEADERBOARD_SECONDS: int = 600
    BEAT_XP_SECONDS: int = 21600


account_settings = AccountSettings()