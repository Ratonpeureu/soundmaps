from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class StudySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Contenu
    ST_MAX_BLOCKS: int = 150
    ST_MAX_OPTIONS: int = 60
    ST_MAX_IMAGE_BYTES: int = 3_000_000
    ST_MAX_VIDEO_BYTES: int = 25_000_000
    ST_ASSET_BODY_BYTES: int = 26_000_000
    ST_MAX_PER_DAY: int = 20
    ST_CLAIM_DEFAULT_DAYS: int = 30
    RATE_LIMIT_STUDY_ASSET_PER_MINUTE: int = 600

    # Qualité des réponses (poids : somme = 1)
    STQ_VALIDATED_MIN: float = 0.70
    STQ_INDICATIVE_MIN: float = 0.45
    STQ_TOO_FAST_HARD: float = 0.20
    STQ_W_TIMING: float = 0.30
    STQ_W_EFFORT: float = 0.25
    STQ_W_TEXT: float = 0.15
    STQ_W_ATTENTION: float = 0.15
    STQ_W_REPUTATION: float = 0.15
    STQ_FINGERPRINT_WINDOW_HOURS: int = 24

    # Ciblage / invitations
    STT_USERS_LIMIT: int = 50000
    STT_ACTIVE_DAYS: int = 45
    STT_INVITE_MAX_PER_USER_WEEK: int = 3
    STT_OVERSAMPLE: float = 2.5
    STT_RESPONSE_RATE_PRIOR: float = 0.35
    STT_INVITE_TTL_DAYS: int = 7
    STT_DEFAULT_MAX_INVITES: int = 300

    # Confidentialité
    ST_IDENTITY_UNLINK_DAYS_AFTER_CLOSE: int = 60
    ST_CONTACT_RETENTION_DAYS: int = 90

    # Tâches (secondes)
    BEAT_STUDY_INVITE_SECONDS: int = 900
    BEAT_STUDY_MAINT_SECONDS: int = 600
    BEAT_STUDY_PRIVACY_SECONDS: int = 86400

    @model_validator(mode="after")
    def _validate(self):
        w = (self.STQ_W_TIMING + self.STQ_W_EFFORT + self.STQ_W_TEXT + self.STQ_W_ATTENTION + self.STQ_W_REPUTATION)
        if abs(w - 1.0) > 1e-6:
            raise ValueError("Les poids STQ_W_* doivent totaliser 1.0")
        if self.STQ_INDICATIVE_MIN > self.STQ_VALIDATED_MIN:
            raise ValueError("STQ_INDICATIVE_MIN doit être <= STQ_VALIDATED_MIN")
        return self


study_settings = StudySettings()