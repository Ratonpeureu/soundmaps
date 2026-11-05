from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SurveySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Enquêtes
    SV_MAX_QUESTIONS: int = 60
    SV_MAX_SLOTS: int = 8
    SV_COOLDOWN_DEFAULT_DAYS: int = 30
    SV_MAX_PER_DAY: int = 30
    SV_TARGET_PER_CELL: int = 10
    SV_ESTIMATED_SECONDS_PER_QUESTION: float = 6.0

    # Profils
    SV_PROFILES_PER_SLOT: int = 10
    SV_PROFILES_MIN: int = 4
    SV_PROFILES_MAX: int = 16
    SV_SHOWN_MAX: int = 12
    SV_EVOLVE_MIN_NEW: int = 30
    SV_EVOLVE_INTERVAL_HOURS: int = 24
    SV_RETIRE_MIN_SHOWN: int = 60
    SV_RETIRE_RELATIVE: float = 0.25           # part de choix < 25 % de la part uniforme
    SV_CLUSTER_MIN: int = 40
    SV_CLUSTER_K: int = 4
    SV_NONE_RATE_TRIGGER: float = 0.15
    SV_LOCAL_DAYS: int = 90
    SV_NEW_PROFILE_MIN_DIST: float = 0.35      # distance minimale (normalisée) à un profil existant

    # Qualité : poids (somme = 1)
    SQ_W_CONSISTENCY: float = 0.15
    SQ_W_TIMING: float = 0.15
    SQ_W_EFFORT: float = 0.10
    SQ_W_LOCATION: float = 0.10
    SQ_W_PEERS: float = 0.15
    SQ_W_REFERENCE: float = 0.05
    SQ_W_EVIDENCE: float = 0.10
    SQ_W_REPUTATION: float = 0.10
    SQ_W_BEHAVIOR: float = 0.10
    SQ_VALIDATED_MIN: float = 0.72
    SQ_INDICATIVE_MIN: float = 0.45
    SQ_BASE_SECONDS: float = 6.0
    SQ_SECONDS_PER_SLOT: float = 4.0
    SQ_SECONDS_PER_OVERRIDE: float = 3.0
    SQ_SECONDS_PER_DIRECT: float = 4.0
    SQ_TOO_FAST_HARD: float = 0.15
    SQ_GPS_GOOD_M: float = 30
    SQ_GPS_REJECT_M: float = 150
    SQ_SPEED_WARN_KMH: float = 120
    SQ_SPEED_REJECT_KMH: float = 500
    SQ_PEER_MIN_SAMPLES: int = 8
    SQ_PEER_Z_MAX: float = 4.0
    SQ_PEER_WINDOW_DAYS: int = 120
    SQ_PEER_RING_K: int = 1
    SQ_REF_FACTOR: float = 6.0
    SQ_FINGERPRINT_WINDOW_HOURS: int = 24
    SQ_FINGERPRINT_MAX: int = 3
    SQ_STRAIGHT_MIN_SCALE: int = 5
    SQ_REEVAL_AFTER_HOURS: int = 6

    # Preuves photo
    SV_EVIDENCE_MAX_BYTES: int = 900_000
    SV_EVIDENCE_BODY_BYTES: int = 1_100_000
    SV_EVIDENCE_PER_RESPONSE: int = 6
    SV_EVIDENCE_WINDOW_MINUTES: int = 30
    SV_EVIDENCE_MAX_AGE_HOURS: int = 36
    SV_EVIDENCE_RETENTION_DAYS: int = 90
    RATE_LIMIT_EVIDENCE_PER_MINUTE: int = 20

    # Confiance du répondant
    ST_PRIOR: float = 0.6
    ST_STRENGTH: float = 5
    ST_WINDOW_DAYS: int = 90

    # Carte des données
    SM_MIN_CONTRIBUTORS: int = 5
    SM_MIN_CONTRIBUTORS_SENSITIVE: int = 10
    SM_MIN_CONTRIBUTORS_FACILITY: int = 3
    SM_DEFAULT_RES: int = 8
    SM_MAX_FEATURES: int = 3000
    SM_MAX_ROWS: int = 100_000
    SM_TARGET_N: int = 20
    SM_TARGET_CONTRIBUTORS: int = 8
    SM_RECENCY_TAU_DAYS: float = 60
    SM_CACHE_TTL_SECONDS: int = 30

    # Tâches (secondes)
    BEAT_SURVEY_SWEEP_SECONDS: int = 120
    BEAT_SURVEY_REEVAL_SECONDS: int = 3600
    BEAT_SURVEY_EVOLVE_SECONDS: int = 3600
    BEAT_SURVEY_TRUST_SECONDS: int = 21600
    BEAT_SURVEY_PRIVACY_SECONDS: int = 86400

    # IA
    GROQ_MODEL_SURVEY: str = ""

    @model_validator(mode="after")
    def _validate(self):
        w = (self.SQ_W_CONSISTENCY + self.SQ_W_TIMING + self.SQ_W_EFFORT + self.SQ_W_LOCATION + self.SQ_W_PEERS
             + self.SQ_W_REFERENCE + self.SQ_W_EVIDENCE + self.SQ_W_REPUTATION + self.SQ_W_BEHAVIOR)
        if abs(w - 1.0) > 1e-6:
            raise ValueError("Les poids SQ_W_* doivent totaliser 1.0")
        if self.SQ_INDICATIVE_MIN > self.SQ_VALIDATED_MIN:
            raise ValueError("SQ_INDICATIVE_MIN doit être <= SQ_VALIDATED_MIN")
        if not (5 <= self.SM_DEFAULT_RES <= 9):
            raise ValueError("SM_DEFAULT_RES doit être entre 5 et 9")
        return self


survey_settings = SurveySettings()