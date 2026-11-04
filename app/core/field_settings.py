from pydantic_settings import BaseSettings, SettingsConfigDict


class FieldSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    OPEN_METEO_URL: str = "https://api.open-meteo.com/v1/forecast"
    WIND_CACHE_TTL_SECONDS: int = 600
    WIND_TIMEOUT_SECONDS: float = 6
    FIELD_CACHE_TTL_SECONDS: int = 30
    FIELD_MAP_CACHE_TTL_SECONDS: int = 20
    FIELD_MIN_GRID: int = 24
    FIELD_MAX_GRID: int = 96
    FIELD_TARGET_STEP_M: float = 60
    FIELD_SIGMA_FACTOR: float = 1.6          # sigma de base = facteur x rayon d'une cellule H3
    FIELD_BACKGROUND_WEIGHT: float = 0.08    # plus il est grand, plus le bruit décroît vite loin des mesures
    FIELD_ADVECTION_SECONDS: float = 90      # décalage du nuage = vitesse du vent x cette durée
    FIELD_MAX_DRIFT_M: float = 1500
    FIELD_MAX_BBOX_DEG: float = 0.8
    FIELD_MAX_SOURCES: int = 1500


field_settings = FieldSettings()