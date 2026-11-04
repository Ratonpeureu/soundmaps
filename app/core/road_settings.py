from pydantic_settings import BaseSettings, SettingsConfigDict


class RoadSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- Import du réseau (OpenStreetMap / Overpass)
    ROAD_AUTO_IMPORT: bool = True
    ROAD_OVERPASS_URLS: str = ("https://overpass-api.de/api/interpreter,"
                               "https://overpass.kumi.systems/api/interpreter,"
                               "https://overpass.private.coffee/api/interpreter")
    ROAD_OVERPASS_TIMEOUT_SECONDS: int = 180
    ROAD_HTTP_TIMEOUT_SECONDS: float = 200
    ROAD_COUNTRY_ISO: str = "SN"                       # "" = pas de filtre pays (bbox seule)
    ROAD_BBOX: str = "-17.60,12.25,-11.30,16.75"       # west,south,east,north
    ROAD_TILE_SIZE_DEG: float = 0.25
    ROAD_TILE_MIN_DEG: float = 0.0625
    ROAD_TILE_MAX_WAYS: int = 40000
    ROAD_TILE_SPACING_SECONDS: int = 10
    ROAD_TILE_MAX_ATTEMPTS: int = 5
    ROAD_STALE_MINUTES: int = 120
    ROAD_HIGHWAYS: str = ("motorway,motorway_link,trunk,trunk_link,primary,primary_link,secondary,secondary_link,"
                          "tertiary,tertiary_link,unclassified,residential,living_street")
    ROAD_SEGMENT_TARGET_M: float = 250
    ROAD_SEGMENT_MIN_M: float = 60
    ROAD_INSERT_CHUNK: int = 400

    # ---- Rattachement mesure -> tronçon
    ROAD_MATCH_MAX_M: float = 40
    ROAD_MATCH_BATCH: int = 5000

    # ---- Carte
    ROAD_BASELINE_DB: float = 0.0                      # niveau initial : calme (vert)
    ROAD_MAP_MAX_FEATURES: int = 6000
    ROAD_MAP_MAX_BBOX_DEG: float = 8.0
    ROAD_NETWORK_CACHE_TTL_SECONDS: int = 86400
    ROAD_NETWORK_CLIENT_MAX_AGE: int = 3600
    ROAD_STATE_CACHE_TTL_SECONDS: int = 20

    # ---- Missions sur les routes
    ROAD_MISSIONS_ENABLED: bool = True
    ROAD_MISSION_MAX_RANK: int = 6                     # 1 = autoroutes/nationales ... 6 = résidentielles
    ROAD_MISSION_MAX_RADIUS_KM: float = 8
    ROAD_TARGET_RADIUS_M: float = 50                   # tolérance autour du point cible
    ROAD_MISSION_COUNT: int = 1
    ROAD_TARGET_PER_PART: int = 3                      # mesures visées par tronçon et par plage horaire
    ROAD_COVERAGE_WINDOW_DAYS: int = 7
    ROAD_PART_WEIGHTS: str = "morning:1.0,afternoon:0.9,evening:0.85,night:0.5"
    ROAD_LOOKAHEAD_HOURS: float = 6
    ROAD_MIN_WINDOW_MINUTES: int = 45
    ROAD_MIN_NEED: float = 0.25
    ROAD_MAX_NEW_PER_USER_PER_RUN: int = 2
    ROAD_SYNC_MAX_NEW: int = 2
    ROAD_MAX_PENDING_PER_USER: int = 4
    ROAD_MIN_SPACING_M: float = 400
    ROAD_REPEAT_COOLDOWN_HOURS: int = 24
    ROAD_REWARD_PER_MEASUREMENT: float = 0.8
    ROAD_CANDIDATE_LIMIT: int = 6000
    ROAD_MAX_USERS_PER_RUN: int = 400
    ROAD_GROUP_H3_RES: int = 7

    # ---- Tâches planifiées (secondes)
    ROAD_BEAT_MATCH_SECONDS: int = 120
    ROAD_BEAT_ENSURE_SECONDS: int = 21600

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        w, s, e, n = (float(x) for x in self.ROAD_BBOX.split(","))
        return w, s, e, n

    @property
    def urls(self) -> list[str]:
        return [u.strip() for u in self.ROAD_OVERPASS_URLS.split(",") if u.strip()]

    @property
    def highways(self) -> list[str]:
        return [h.strip() for h in self.ROAD_HIGHWAYS.split(",") if h.strip()]

    @property
    def part_weights(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for item in self.ROAD_PART_WEIGHTS.split(","):
            k, _, v = item.partition(":")
            if k.strip():
                out[k.strip()] = float(v or 1.0)
        return out


road_settings = RoadSettings()