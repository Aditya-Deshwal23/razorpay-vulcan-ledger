"""
Centralized application configuration for Razorpay Vulcan Ledger.

Loads and validates every environment-driven setting the backend needs from a
single typed object, so no module ever reads os.environ directly and no setting
can be silently missing, out of range, or the wrong type at runtime.

Secret handling:
    Every credential is pydantic.SecretStr, so `repr(settings)`, a logged
    exception, a pytest assertion diff, or a FastAPI validation error can never
    print a password or an API key -- they render as `SecretStr('**********')`.
    Call .get_secret_value() explicitly at the single point of use. That
    explicitness is the point: an accidental leak now requires someone to type
    the words `get_secret_value`.

Exception vectors handled:
    - Missing required environment variable -> pydantic.ValidationError on the
      first get_settings() call (fail fast at startup, not mid-request).
    - Out-of-range numeric setting (negative timeout, confidence above 1.0)
      -> pydantic.ValidationError, not a subtly broken pipeline hours later.
    - Malformed .env line -> python-dotenv skips it silently; the missing
      variable then surfaces as the ValidationError above rather than as a
      confusing downstream KeyError.
    - Empty GOOGLE_API_KEY with a real agent run -> require_google_api_key()
      raises a clear, actionable RuntimeError instead of letting an opaque
      provider auth error surface from inside LangChain.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchored to this file's location on disk, not the process's working
# directory -- so `.env` at the project root is found the same way whether the
# app is launched from backend/, as a pytest run from the repo root, or inside
# a container with a different WORKDIR.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"

class Settings(BaseSettings):
    """
    Typed, validated application settings sourced from environment variables and
    the project-root `.env` file (see `.env.example` for the full list).

    Attributes:
        database_url: Async SQLAlchemy DSN (postgresql+asyncpg://...) for all
            ORM reads/writes.
        checkpointer_database_url: Plain psycopg3 DSN (postgresql://...) used
            exclusively by LangGraph's AsyncPostgresSaver. A separate URL on
            purpose: langgraph-checkpoint-postgres needs psycopg3, not asyncpg.
        google_api_key: Gemini API key. Empty is allowed so the whole test suite
            and every offline tool can import this module; the real agent path
            calls require_google_api_key() first.
        gemini_model: Model id, config-driven so switching or falling back needs
            no code change. Kept in step with `.env.example`.
        razorpay_key_id / razorpay_key_secret / razorpay_webhook_secret:
            Razorpay API and webhook-signature credentials.
        environment: Controls logging verbosity and docs exposure only. It must
            never branch financial logic.
        sql_echo: Whether SQLAlchemy echoes every statement. Explicit and
            default-off: it used to be derived from `environment`, which meant a
            development run dumped every monetary value of every row into the
            console and buried real errors in noise.
        log_level: Root log level for the backend's logging setup.
        llm_timeout_seconds: Hard per-attempt ceiling on one LLM call. Without
            it a hung provider connection stalls the whole batch indefinitely.
        llm_max_attempts: Bounded retry count for a malformed/failed LLM
            response before the graph falls back to human review.
        llm_retry_backoff_seconds: Base delay between LLM attempts. Retrying a
            rate-limited or overloaded provider with no pause makes the outage
            worse, not shorter.
        ai_auto_approve_min_confidence: Deterministic floor the agent's own
            confidence must clear before its verdict may be auto-approved. This
            is enforced in our code, not requested of the model: an LLM claiming
            "AUTO_APPROVE" at 0.10 confidence gets overridden to human review.
        bank_date_match_window_days: How many days either side of a settlement
            date a bank credit may fall and still be considered the same payout
            when no UTR is available to prove it.
    """

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: SecretStr
    checkpointer_database_url: SecretStr
    google_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-3.6-flash"
    razorpay_key_id: str = ""
    razorpay_key_secret: SecretStr = SecretStr("")
    razorpay_webhook_secret: SecretStr = SecretStr("")
    environment: Literal["development", "staging", "production"] = "development"

    sql_echo: bool = False
    log_level: str = "INFO"

    llm_timeout_seconds: float = Field(default=30.0, gt=0.0, le=600.0)
    llm_max_attempts: int = Field(default=2, ge=1, le=10)
    llm_retry_backoff_seconds: float = Field(default=0.5, ge=0.0, le=60.0)
    ai_auto_approve_min_confidence: float = Field(default=0.85, ge=0.0, le=1.0)
    bank_date_match_window_days: int = Field(default=3, ge=0, le=90)

    @field_validator("gemini_model")
    @classmethod
    def _model_must_be_nonempty(cls, value: str) -> str:
        """Reject a blank GEMINI_MODEL, which would otherwise fail deep inside
        the provider SDK with an unhelpful message."""
        if not value.strip():
            raise ValueError("gemini_model must not be empty")
        return value.strip()

    @property
    def sync_database_url(self) -> str:
        """
        The same database, addressed with a synchronous psycopg3 driver.

        Used by the migration runner, which executes multi-statement SQL files
        (DO blocks, temp tables) that asyncpg's prepared-statement protocol
        cannot handle. Derived from database_url rather than being a separate
        setting, so the two can never point at different databases.

        Returns:
            A `postgresql://...` DSN with any `+asyncpg` / `+psycopg` driver
            qualifier stripped.
        """
        raw = self.database_url.get_secret_value()
        scheme, _, rest = raw.partition("://")
        return f"{scheme.split('+', 1)[0]}://{rest}"

    def require_google_api_key(self) -> str:
        """
        Return the Gemini API key, or fail with an actionable message.

        Returns:
            The plaintext API key.

        Raises:
            RuntimeError: if GOOGLE_API_KEY is unset or blank. Raised here, at
                the boundary, rather than letting an empty key travel into
                LangChain and come back as an opaque authentication error with
                no hint about which environment variable is missing.
        """
        key = self.google_api_key.get_secret_value()
        if not key.strip():
            raise RuntimeError(
                "GOOGLE_API_KEY is not set. Add it to the project-root .env "
                "(see .env.example) before running the real Gemini agent. "
                "The test suite does not need it -- it injects a fake LLM."
            )
        return key


@lru_cache
def get_settings() -> Settings:
    """
    Return the process-wide cached Settings instance.

    lru_cache means the .env file is parsed and validated exactly once per
    process, and FastAPI's Depends(get_settings) reuses the same validated
    object on every request instead of re-reading the environment each time.

    Tests that need to vary configuration should call
    get_settings.cache_clear() after patching the environment.
    """
    return Settings()
