import re
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    PROJECT_NAME: str = "B2B Marketplace API"
    ENVIRONMENT: str = "development"

    SECRET_KEY: str = "dev-only-insecure-key-change-me"  # == DEFAULT_SECRET_KEY
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7

    DATABASE_URL: str = (
        "postgresql+asyncpg://marketplace:marketplace@localhost:5433/marketplace"
    )
    DB_ECHO: bool = False

    # Reserved for later phases; declared so .env stays a single source of truth.
    QDRANT_URL: str = "http://localhost:6333"
    REDIS_URL: str = "redis://localhost:6379/0"
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    # OLLAMA_MODEL: str = "gpt-oss:120b"
    OLLAMA_MODEL: str = "qwen3-coder-next:q4_K_M"
    OLLAMA_NUM_CTX: int = 65536
    EMBEDDING_MODEL: str = "qwen3-embedding:8b"
    EMBEDDING_TIMEOUT_SECONDS: int = 180
    QDRANT_COLLECTION: str = "marketplace_rfq_v2"
    # Cosine similarity below this is a different product, not a weak match.
    # Measured against the seed corpus with product-only embeddings: the right
    # product scores 0.74-0.84, a different product in the same category scores
    # 0.55-0.67. At 0.70 every test query returns results and none of the
    # near-miss products get in.
    RELEVANCE_FLOOR: float = 0.70
    # Used only when nothing clears the floor, so an unusual phrasing returns
    # the closest things rather than an empty table.
    RELEVANCE_FALLBACK_FLOOR: float = 0.60
    OLLAMA_TIMEOUT_SECONDS: int = 45
    # Use the LLM and prompt-based agent for conversational search
    DIRECT_SEARCH_LLM: bool = True
    # AI Conversational Business Assistant (Ollama)
    AI_CHAT_MODEL: str = OLLAMA_MODEL
    AI_CHAT_TIMEOUT_SECONDS: int = 180
    AI_CHAT_NUM_CTX: int = 0  # 0 = use native model context to prevent Ollama reload/deadlocks

    # Real-time Deal Room Translation
    TRANSLATION_MODEL: str = "gpt-oss:20b"
    TRANSLATION_TIMEOUT_SECONDS: float = 10.0
    # When the local translation model fails, fall back to an external
    # translation service. Off by default: deal-room text must not leave the
    # host unless the operator opts in.
    TRANSLATION_EXTERNAL_FALLBACK: bool = False

    # Image safety moderation via vision model
    IMAGE_MODERATION_MODEL: str = "qwen3-vl:8b"
    IMAGE_MODERATION_TIMEOUT_SECONDS: int = 45
    IMAGE_MODERATION_ENABLED: bool = True
    IMAGE_MODERATION_FAIL_CLOSED: bool = True

    HOST: str = "127.0.0.1"
    PORT: int = 8011

    # Optional map and geocoding keys
    MAPBOX_ACCESS_TOKEN: str = ""
    GEOCODING_API_KEY: str = ""

    # NoDecode stops pydantic-settings from trying to JSON-parse this out of
    # the .env file, so it can be written as a plain comma-separated list.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:5174", "http://localhost:5175"]
    )

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    UPLOAD_DIR: str = "uploads"

    # Lifetime of the HMAC-signed URLs handed out for private media (deal-room
    # live captures). Browsers load these through <img src>, which cannot send
    # an Authorization header, so the signature is the credential.
    MEDIA_SIGNED_URL_TTL_SECONDS: int = 6 * 60 * 60

    # --- Rate limiting (see core/rate_limit.py) ------------------------------
    # Each limit is "<max requests>/<window seconds>". The limiter is in-memory
    # and per process: with several workers the effective limit is multiplied
    # by the worker count until it moves to Redis.
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_LOGIN: str = "10/300"  # failed logins per IP + email
    RATE_LIMIT_LOGIN_IP: str = "100/300"  # failed logins per IP, any email
    RATE_LIMIT_SIGNUP: str = "20/3600"  # signups per IP
    RATE_LIMIT_REFRESH: str = "60/300"  # token refreshes per IP
    RATE_LIMIT_MODERATION: str = "120/60"  # /moderation/check per user
    RATE_LIMIT_UPLOAD: str = "20/3600"  # certificate uploads per user

    @field_validator(
        "RATE_LIMIT_LOGIN",
        "RATE_LIMIT_LOGIN_IP",
        "RATE_LIMIT_SIGNUP",
        "RATE_LIMIT_REFRESH",
        "RATE_LIMIT_MODERATION",
        "RATE_LIMIT_UPLOAD",
    )
    @classmethod
    def _check_rate(cls, value: str) -> str:
        if not _RATE_RE.match(value.strip()):
            raise ValueError("rate limits must look like '<count>/<seconds>', e.g. '10/300'")
        return value.strip()

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() == "production"

    @property
    def is_dev_like(self) -> bool:
        """Environments where the built-in development secret is tolerated."""
        return self.ENVIRONMENT.strip().lower() in DEV_ENVIRONMENTS


_RATE_RE = re.compile(r"^\d+/\d+(?:\.\d+)?$")

# Anything not listed here (production, prod, staging, qa, ...) must run with a
# real secret. An allow-list rather than a check for "production" so a typo or a
# new environment name fails closed.
DEV_ENVIRONMENTS = frozenset({"development", "dev", "local", "test"})
DEFAULT_SECRET_KEY = "dev-only-insecure-key-change-me"
MIN_SECRET_KEY_LENGTH = 32


def check_secret_key(settings: Settings) -> None:
    if settings.is_dev_like:
        return
    key = settings.SECRET_KEY or ""
    if key == DEFAULT_SECRET_KEY or key.startswith("dev-only") or len(key) < MIN_SECRET_KEY_LENGTH:
        raise RuntimeError(
            f"SECRET_KEY must be a random secret of at least {MIN_SECRET_KEY_LENGTH} characters "
            f"when ENVIRONMENT={settings.ENVIRONMENT!r} (generate one with "
            "python -c \"import secrets; print(secrets.token_urlsafe(64))\")"
        )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    check_secret_key(settings)
    return settings


settings = get_settings()
