from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "HackVerse API"
    app_env: str = "local"
    debug: bool = False
    api_prefix: str = "/api/v1"

    database_url: str = Field(
        default="postgresql+asyncpg://hackverse:hackverse@localhost:5432/hackverse"
    )
    redis_url: str = "redis://localhost:6379/0"

    session_secret: str = "change-me"
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/v1/auth/google/callback"
    frontend_base_url: str = "http://localhost:5173"

    session_cookie_name: str = "hackverse_session"
    session_ttl_seconds: int = 60 * 60 * 24 * 3
    session_cookie_secure: bool = False

    # Origins allowed for CORS and for the WebSocket handshake. The frontend
    # origin is always allowed implicitly; add extras (preview deploys) here.
    allowed_origins: list[str] = Field(default_factory=list)

    # Database pool. Every WebSocket message that touches Postgres borrows from
    # this pool, so the default of 5 is far too small for a live event.
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_timeout_seconds: int = 10

    # Realtime tuning.
    presence_ttl_seconds: int = 60
    movement_rate_limit_per_second: int = 20
    chat_rate_limit_per_minute: int = 30
    interact_rate_limit_per_minute: int = 60
    chat_max_length: int = 2000
    proximity_tile_radius: float = 3.0
    interest_radius_tiles: float = 20.0
    max_media_peers: int = 6
    world_max_coordinate: float = 100_000.0

    # Assets. We author the campus map and serve it from backend/assets, so no
    # object storage is provisioned. Set this to a CDN origin to front it.
    max_upload_bytes: int = 8 * 1024 * 1024
    storage_public_base_url: str = ""

    # Chat retention, in days. 0 disables the cleanup worker.
    chat_retention_days: int = 30

    # WebRTC. Without a TURN relay, media fails on restrictive college networks
    # — the PRD's highest-ranked technical risk.
    stun_urls: list[str] = Field(default_factory=lambda: ["stun:stun.l.google.com:19302"])
    turn_urls: list[str] = Field(default_factory=list)
    turn_shared_secret: str = ""
    turn_credential_ttl_seconds: int = 3600

    # Undocumented in the PRD; off until the product question is settled.
    enable_gamification: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def is_production_like(self) -> bool:
        return self.app_env not in {"local", "test"}

    @property
    def cors_origins(self) -> list[str]:
        origins = [self.frontend_base_url, *self.allowed_origins]
        return sorted({origin.rstrip("/") for origin in origins if origin})

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        """Refuse to boot a production-like environment with scaffold defaults.

        A deploy that silently falls back to ``change-me`` lets anyone forge a
        session cookie for any user, including admins.
        """
        if not self.is_production_like:
            return self

        problems: list[str] = []
        if self.session_secret in {"", "change-me"}:
            problems.append("SESSION_SECRET must be set to a strong random value")
        if len(self.session_secret) < 32:
            problems.append("SESSION_SECRET must be at least 32 characters")
        if not self.session_cookie_secure:
            problems.append("SESSION_COOKIE_SECURE must be true outside local development")
        if not self.google_client_id or not self.google_client_secret:
            problems.append("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set")

        if problems:
            raise ValueError(
                f"Invalid configuration for APP_ENV={self.app_env}: " + "; ".join(problems)
            )
        return self

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        value = value.strip().strip("'").strip('"')
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @property
    def asyncpg_database_url(self) -> str:
        split_url = urlsplit(self.database_url)
        query = dict(parse_qsl(split_url.query, keep_blank_values=True))
        query.pop("sslmode", None)
        query.pop("channel_binding", None)
        return urlunsplit(
            (
                split_url.scheme,
                split_url.netloc,
                split_url.path,
                urlencode(query),
                split_url.fragment,
            )
        )

    @property
    def asyncpg_connect_args(self) -> dict[str, str]:
        split_url = urlsplit(self.database_url)
        query = dict(parse_qsl(split_url.query, keep_blank_values=True))
        if query.get("sslmode") in {"require", "verify-ca", "verify-full"}:
            return {"ssl": "require"}
        return {}


@lru_cache
def get_settings() -> Settings:
    return Settings()
