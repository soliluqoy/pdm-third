"""
PREDICT — configuration. Everything optional; sane defaults for the 2-service
docker-compose stack. Loaded from env / .env via pydantic-settings.
"""
from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8",
        case_sensitive=False, extra="ignore",
    )

    APP_NAME: str = "PREDICT"
    APP_VERSION: str = "1.0.0"
    SQL_ECHO: bool = False

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = (
        "postgresql+asyncpg://predict:predict_dev_password@localhost:5432/predict"
    )

    # ── Teltonika TCP listener ────────────────────────────────────────────────
    TELTONIKA_HOST: str = "0.0.0.0"
    TELTONIKA_PORT: int = 5123
    TELTONIKA_IDLE_TIMEOUT: int = 300  # close silent connections after N s

    # ── Frontend / dev ────────────────────────────────────────────────────────
    # Directory with the built SPA (inside the image: /app/static).
    STATIC_DIR: str = "static"
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Pre-fills the SMS setup templates shown in Settings (not used by the listener).
    TRACKER_PUBLIC_HOST: str = "<YOUR_SERVER_IP>"

    # ── Optional single-admin login ───────────────────────────────────────────
    # When ADMIN_PASSWORD is set, the dashboard + API + WebSocket require login.
    # Leave empty to disable auth entirely (local dev / no auth).
    ADMIN_PASSWORD: str = ""
    # Secret used to sign session cookies. Auto-generated and persisted to a
    # file if left empty.
    AUTH_SECRET: str = ""

    # ── Freshness / lifecycle ─────────────────────────────────────────────────
    # Rules only fire for records fresher than this; older (buffered) records
    # are stored but never alert.
    RULE_MAX_RECORD_AGE_SECONDS: int = 300
    # Live tiles / health treat a car as offline after this many seconds.
    OFFLINE_AFTER_SECONDS: int = 300
    WATCHDOG_INTERVAL_SECONDS: int = 60
    # TimescaleDB: compress after 7 days, drop raw readings after N days.
    COMPRESS_AFTER_DAYS: int = 7
    READINGS_RETENTION_DAYS: int = 365
    # Baselines / anomaly job cadence
    BASELINES_INTERVAL_SECONDS: int = 6 * 3600

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
