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
    # Sent as the api-key header by the Qdrant client; must equal QDRANT_API_KEY given to the container.
    QDRANT_API_KEY: str = ""
    # Nothing connects to Redis yet; the password matches docker-compose's REDIS_PASSWORD default.
    REDIS_URL: str = "redis://:change-me-dev@localhost:6379/0"
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "gpt-oss:120b"
    OLLAMA_NUM_CTX: int = 65536
    LLM_PROVIDER: str = "ollama"
    VLLM_BASE_URL: str = "http://127.0.0.1:8001/v1"
    VLLM_MODEL: str = "Qwen/Qwen3-8B"
    VLLM_TIMEOUT_SECONDS: float = 60.0
    EMBEDDING_MODEL: str = "qwen3-embedding:8b"
    EMBEDDING_TIMEOUT_SECONDS: int = 180
    # GPU layers for the embedding model; -1 lets Ollama decide, 0 runs it on CPU
    # (set 0 where the GPU cannot hold it next to the chat model, see embeddings._post).
    EMBEDDING_NUM_GPU: int = -1
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
    # Open-world AI-first message understanding (services/ai_query_parser.py).
    # Empty model = reuse AI_CHAT_MODEL (small router models misread Hinglish); failures fall back to the rule parser.
    QUERY_AI_ENABLED: bool = True
    QUERY_AI_MODEL: str = ""
    # The shared model runner serves one request at a time, so a parse can queue behind a chat turn
    # (measured p90 wait ~12 s); 10 s made the AI parser time out almost every call.
    QUERY_AI_TIMEOUT_SECONDS: float = 20.0
    # AI lexicon (services/ai_lexicon): the model's word/place/product knowledge, cached on disk.
    LEXICON_AI_ENABLED: bool = True
    LEXICON_AI_TIMEOUT_SECONDS: float = 20.0
    AI_LEXICON_PATH: str = "data/ai_lexicon.json"
    # AI-assisted listing matching: total time budget per search and max candidate names sent to the model.
    MATCH_AI_ENABLED: bool = True
    MATCH_AI_BUDGET_SECONDS: float = 15.0
    MATCH_AI_MAX_NAMES: int = 40
    # Optional LLM HSN classification, used ONLY when a listing is saved (never in
    # match scoring). Empty model = AI_CHAT_MODEL.
    HSN_AI_ENABLED: bool = True
    HSN_AI_MODEL: str = ""
    HSN_AI_TIMEOUT_SECONDS: float = 20.0
    # Optional LLM Product Attribute recommendation based on product name
    ATTRIBUTE_AI_ENABLED: bool = True
    # Empty = AI_CHAT_MODEL. A different model makes Ollama unload the chat model to load it (evicted it 5x in one morning).
    ATTRIBUTE_AI_MODEL: str = ""
    ATTRIBUTE_AI_TIMEOUT_SECONDS: float = 45.0
    # Vocabulary learned from grounded AI parses; relative paths resolve against backend/.
    LEARNED_VOCAB_PATH: str = "data/learned_vocab.json"
    # AI Conversational Business Assistant (Ollama gpt-oss:120b)
    AI_CHAT_PROVIDER: str = "ollama"
    AI_CHAT_MODEL: str = OLLAMA_MODEL
    AI_CHAT_TIMEOUT_SECONDS: int = 180
    # Ollama keeps one runner per (model, num_ctx). 0 = send no num_ctx, i.e. the Ollama SERVER default
    # (8192 here), NOT the model's native context. It must equal the num_ctx of every other caller of the
    # same model, or Ollama reloads the model on each switch.
    AI_CHAT_NUM_CTX: int = 0
    # Ollama applies the latest request's keep-alive; a short value evicts a model other callers still use.
    OLLAMA_KEEP_ALIVE: str = "24h"

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

    # Accounts allowed to use the /ops endpoints (approve KYC and certificates,
    # confirm escrow deposits, suspend accounts). There is no admin role on
    # User: operators are named here so nobody can grant it to themselves.
    OPERATOR_EMAILS: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("OPERATOR_EMAILS", mode="before")
    @classmethod
    def _split_operator_emails(cls, value: object) -> object:
        if isinstance(value, str):
            return [email.strip().lower() for email in value.split(",") if email.strip()]
        return value

    UPLOAD_DIR: str = "uploads"
    # Global cap on any request body (413 beyond it). Above the 10 MB image limit plus multipart overhead.
    MAX_REQUEST_BODY_BYTES: int = 12 * 1024 * 1024

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
    # Failed logins per email from ANY address: rotating X-Forwarded-For or a botnet cannot
    # reset it. Generous enough that an attacker locking out a victim is the worse case (15 min).
    RATE_LIMIT_LOGIN_EMAIL: str = "20/900"
    RATE_LIMIT_AI_CHAT: str = "20/60"  # AI chat turns per user
    RATE_LIMIT_TRANSLATE: str = "30/60"  # translate + batch requests per user
    RATE_LIMIT_RFQ_WRITE: str = "30/300"  # RFQ create + edit per user
    RATE_LIMIT_LIVE_CAPTURE: str = "30/600"  # deal-room photos (each runs a vision model) per user
    RATE_LIMIT_SUGGEST: str = "30/300"  # /rfqs/suggest-attributes per user

    @field_validator(
        "RATE_LIMIT_LOGIN",
        "RATE_LIMIT_LOGIN_IP",
        "RATE_LIMIT_SIGNUP",
        "RATE_LIMIT_REFRESH",
        "RATE_LIMIT_MODERATION",
        "RATE_LIMIT_UPLOAD",
        "RATE_LIMIT_LOGIN_EMAIL",
        "RATE_LIMIT_AI_CHAT",
        "RATE_LIMIT_TRANSLATE",
        "RATE_LIMIT_RFQ_WRITE",
        "RATE_LIMIT_LIVE_CAPTURE",
        "RATE_LIMIT_SUGGEST",
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
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def check_secret_key(settings: Settings) -> None:
    key = settings.SECRET_KEY or ""
    # The built-in key is only ever acceptable on a loopback-bound dev server: a deploy that
    # forgot ENVIRONMENT (default "development") but listens on a public address must not start.
    if (
        (key == DEFAULT_SECRET_KEY or key.startswith("dev-only"))
        and settings.HOST.strip().lower() not in LOOPBACK_HOSTS
    ):
        raise RuntimeError(
            "refusing to start with the built-in development SECRET_KEY while HOST="
            f"{settings.HOST!r} is not loopback; set SECRET_KEY (python -c "
            "\"import secrets; print(secrets.token_urlsafe(64))\")"
        )
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
