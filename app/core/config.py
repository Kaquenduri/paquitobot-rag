"""Application settings loaded from environment variables.

Fail-closed: every required field must be present at startup, otherwise
:class:`Settings` raises :class:`pydantic.ValidationError` before the app
serves traffic.
"""

from __future__ import annotations

import base64
import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class Settings(BaseSettings):
    """Strongly-typed runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Keep pytest isolated from deployment ``.env`` and secret files."""
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return init_settings, env_settings
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

    # --- Required secrets and endpoints (no defaults → fail-closed) ---
    supabase_database_url: str = Field(
        ...,
        description="Async SQLAlchemy URL for the Supabase Postgres database.",
    )
    tenant_token_key: str = Field(
        ...,
        description="Fernet key (urlsafe-base64, 32-byte payload) used to encrypt "
        "Canvas tokens at rest.",
    )
    backend_secret: str = Field(
        ...,
        description="HMAC secret for backend JWT (HS256) and signature checks.",
    )
    ollama_host: str = Field(
        ...,
        description="Base URL of the local Ollama server (e.g. http://127.0.0.1:11434).",
    )

    # --- LLM de chat (cualquier proveedor compatible con OpenAI) ---
    # Ollama expone /v1 compatible con OpenAI, igual que DeepSeek, Qwen o Kimi.
    # Cambiar de proveedor es cambiar estas tres variables, sin tocar codigo.
    llm_base_url: str = Field(
        default="http://127.0.0.1:11434/v1",
        description="Endpoint OpenAI-compatible de chat completions.",
    )
    llm_model: str = Field(
        default="qwen2.5:3b",
        description="Identificador del modelo de chat.",
    )
    llm_api_key: str = Field(
        default="ollama",
        description="API key del proveedor. Ollama la ignora; las APIs de pago no.",
    )
    llm_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        description="Timeout por llamada al LLM. Generoso porque en CPU es lento.",
    )
    gemini_api_key: str | None = Field(
        default=None,
        description="Solo para el pipeline legacy (LEGACY_MODE=1). El backend no lo usa.",
    )
    canvas_api_base_url: str = Field(
        ...,
        description="Base URL of the Canvas REST API (e.g. https://example.instructure.com/api/v1).",
    )

    # --- Embedding model ---
    ollama_embedding_model: str = Field(
        default="qwen3-embedding:8b",
        description="Ollama embedding model identifier.",
    )
    ollama_embed_dim: int = Field(
        default=1024,
        ge=64,
        le=4096,
        description="Embedding vector dimension; must match the PGVector column.",
    )

    # --- Sync / scheduler ---
    sync_interval_seconds: int = Field(
        default=21600,
        ge=60,
        description="Periodic sync interval (default 6h).",
    )
    sync_jitter_seconds: int = Field(
        default=60,
        ge=0,
        description="Per-tenant jitter applied on top of the sync interval.",
    )
    manual_sync_min_interval_seconds: int = Field(
        default=60,
        ge=1,
        description="Minimum seconds between manual sync calls per tenant.",
    )

    # --- SQL safety ---
    sql_statement_timeout_ms: int = Field(
        default=2000,
        ge=100,
        description="Postgres statement_timeout for the text-to-SQL executor.",
    )
    sql_row_limit: int = Field(
        default=200,
        ge=1,
        le=10_000,
        description="Server-side LIMIT applied to text-to-SQL queries.",
    )

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Root log level for structlog/uvicorn.",
    )

    # --- Feature flags (read by other modules) ---
    scheduler_enabled: bool = Field(
        default=True,
        description="Enable the APScheduler background sync (PR 4).",
    )
    disable_rag_routes: bool = Field(
        default=False,
        description="When true, /query and other RAG routes return 503 (PR 6).",
    )
    dev_ui_enabled: bool = Field(
        default=False,
        description=(
            "Monta /dev: la UI de prueba y un emisor de JWT sin login. "
            "SOLO para desarrollo local; jamas en un despliegue real."
        ),
    )
    dev_ui_user: str = Field(
        default="joshua-tecsup",
        description="backend_user_id que firma el JWT de la UI de prueba.",
    )

    @field_validator("tenant_token_key")
    @classmethod
    def _validate_fernet_key(cls, value: str) -> str:
        """The Fernet key MUST decode as urlsafe-base64 of 32 raw bytes."""
        try:
            decoded = base64.urlsafe_b64decode(value)
        except (ValueError, TypeError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
            raise ValueError("TENANT_TOKEN_KEY must be urlsafe-base64") from exc
        if len(decoded) != 32:
            raise ValueError(
                "TENANT_TOKEN_KEY must decode to 32 bytes for Fernet "
                f"(got {len(decoded)} bytes)"
            )
        return value

    @field_validator("supabase_database_url")
    @classmethod
    def _validate_db_scheme(cls, value: str) -> str:
        """Only async SQLAlchemy schemes are allowed at startup."""
        if not value.startswith(("postgresql+psycopg://", "postgresql+asyncpg://")):
            raise ValueError(
                "SUPABASE_DATABASE_URL must use an async driver "
                "(postgresql+psycopg:// or postgresql+asyncpg://)"
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached :class:`Settings` instance.

    The cache means monkeypatch-style env-var tests must clear the cache
    (``get_settings.cache_clear()``) before re-instantiating.
    """
    return Settings()  # type: ignore[call-arg]
