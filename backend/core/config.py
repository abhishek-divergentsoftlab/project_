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

    SECRET_KEY: str = "dev-only-insecure-key-change-me"
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
    OLLAMA_MODEL: str = "qwen3-coder-next:q4_K_M"
    OLLAMA_NUM_CTX: int = 65536
    EMBEDDING_MODEL: str = "nomic-embed-text"
    EMBEDDING_TIMEOUT_SECONDS: int = 30
    QDRANT_COLLECTION: str = "marketplace_rfq"
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
    AI_CHAT_TIMEOUT_SECONDS: int = 120
    AI_CHAT_NUM_CTX: int = 65536

    # Image safety moderation via vision model
    IMAGE_MODERATION_MODEL: str = "qwen3-vl:8b"
    IMAGE_MODERATION_TIMEOUT_SECONDS: int = 45
    IMAGE_MODERATION_ENABLED: bool = True
    IMAGE_MODERATION_FAIL_CLOSED: bool = True

    HOST: str = "127.0.0.1"
    PORT: int = 8011

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

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.is_production and settings.SECRET_KEY.startswith("dev-only"):
        raise RuntimeError("SECRET_KEY must be set to a real secret in production")
    return settings


settings = get_settings()
