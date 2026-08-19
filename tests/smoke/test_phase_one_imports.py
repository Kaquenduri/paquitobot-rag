"""Import smoke for every module introduced by Phase 1 / PR 1.

These tests fail loudly if any module is renamed or removed during a future
PR, ensuring the scaffold stays importable throughout the chained-PR
sequence.
"""

from __future__ import annotations

import importlib

import pytest

PHASE_ONE_MODULES = (
    "app",
    "app.main",
    "app.core",
    "app.core.config",
    "app.core.logging",
    "app.core.errors",
    "app.schemas",
    "app.schemas.errors",
)


@pytest.mark.parametrize("module_name", PHASE_ONE_MODULES)
def test_phase_one_module_imports(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module.__name__ == module_name


def test_app_version_is_string() -> None:
    from app import __version__

    assert isinstance(__version__, str)
    assert __version__  # non-empty


def test_settings_fail_closed_when_required_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If SUPABASE_DATABASE_URL is missing, Settings must raise at instantiation."""
    from pydantic import ValidationError

    from app.core.config import Settings, get_settings

    # Remove every required key so the validation chain is exhausted.
    for key in (
        "SUPABASE_DATABASE_URL",
        "TENANT_TOKEN_KEY",
        "BACKEND_SECRET",
        "MINIMAX_API_KEY",
        "OLLAMA_HOST",
        "CANVAS_API_BASE_URL",
        "GOOGLE_CLIENT_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    # Ensure the lru_cache doesn't hand us a previously-instantiated Settings.
    get_settings.cache_clear()

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_settings_validator_rejects_invalid_fernet_key(
    monkeypatch: pytest.MonkeyPatch, app_settings
) -> None:
    """A non-Fernet key (wrong length) must raise at startup."""
    from pydantic import ValidationError

    from app.core.config import Settings

    monkeypatch.setenv("TENANT_TOKEN_KEY", "too-short")

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_settings_validator_rejects_non_async_db_url(
    monkeypatch: pytest.MonkeyPatch, app_settings
) -> None:
    """A sync postgresql:// URL must NOT pass the scheme validator."""
    from pydantic import ValidationError

    from app.core.config import Settings

    monkeypatch.setenv(
        "SUPABASE_DATABASE_URL", "postgresql://user:pwd@host:5432/db"
    )

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_safe_message_masks_known_credential_patterns() -> None:
    """safe_message must scrub Fernet, Bearer, and postgres URLs."""
    from app.core.errors import safe_message

    scrubbed = safe_message(
        "token=gAAAAAbcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig "
        "db=postgresql+psycopg://u:p@h:5432/d"
    )
    assert "gAAAAA" not in scrubbed
    assert "Bearer eyJ" not in scrubbed
    assert "postgresql+psycopg" not in scrubbed
    assert "***REDACTED***" in scrubbed
