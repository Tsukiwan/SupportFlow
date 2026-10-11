"""Application settings, loaded from environment variables.

Never instantiated at import time: importing this module (and any module
that imports it) works without a DeepSeek API key, so local tests and
development run with a missing key.

Env names are explicit per field (no prefix magic): the ``SUPPORTFLOW_*``
names are canonical, and the bare ``DEEPSEEK_*`` names also work for the
API key and model, so a hand-written ``.env`` with bare names works too.
"""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = Field(
        default="development", validation_alias="SUPPORTFLOW_APP_ENV"
    )
    log_level: str = Field(default="INFO", validation_alias="SUPPORTFLOW_LOG_LEVEL")

    # Optional on Day 1: the agent loop is not implemented yet.
    deepseek_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SUPPORTFLOW_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"
        ),
    )
    deepseek_model: str = Field(
        default="deepseek-chat",
        validation_alias=AliasChoices("SUPPORTFLOW_DEEPSEEK_MODEL", "DEEPSEEK_MODEL"),
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias="SUPPORTFLOW_DEEPSEEK_BASE_URL",
    )

    # SQLite default keeps local dev dependency-free; the postgres backend
    # lands with the persistence layer (Day 4/5).
    database_url: str = Field(
        default="sqlite:///supportflow.db",
        validation_alias="SUPPORTFLOW_DATABASE_URL",
    )

    # Which store backend the agent runs on. "postgres" is explicit and
    # fail-loud: it requires a PostgreSQL URL and never falls back to the
    # in-memory stores (see ``_validate_store_backend``).
    store_backend: Literal["memory", "postgres"] = Field(
        default="memory", validation_alias="SUPPORTFLOW_STORE_BACKEND"
    )

    # Agent guardrails.
    max_agent_iterations: int = Field(
        default=10, gt=0, validation_alias="SUPPORTFLOW_MAX_AGENT_ITERATIONS"
    )
    tool_timeout_seconds: float = Field(
        default=30.0, gt=0, validation_alias="SUPPORTFLOW_TOOL_TIMEOUT_SECONDS"
    )

    @model_validator(mode="after")
    def _validate_store_backend(self) -> "Settings":
        # Choosing postgres is explicit; a mismatched URL is a configuration
        # error raised at startup — never a silent fallback to memory.
        if self.store_backend == "postgres" and not self.database_url.startswith(
            "postgresql+psycopg://"
        ):
            raise ValueError(
                "store_backend=postgres requires a PostgreSQL database_url "
                "(postgresql+psycopg://...); refusing to mix backends"
            )
        return self

    @property
    def has_deepseek_key(self) -> bool:
        return self.deepseek_api_key is not None and bool(
            self.deepseek_api_key.get_secret_value()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
