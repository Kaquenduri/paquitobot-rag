"""Unit tests for ``app.security.redaction.RedactionFilter``."""

from __future__ import annotations

import logging

import pytest

from app.security.redaction import RedactionFilter


@pytest.fixture
def redactor() -> RedactionFilter:
    return RedactionFilter()


def test_redaction_filter_masks_bearer_token(redactor: RedactionFilter) -> None:
    out = redactor.redact_message(
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
    )
    assert "Bearer eyJ" not in out
    assert "***REDACTED***" in out


def test_redaction_filter_masks_fernet_ciphertext(redactor: RedactionFilter) -> None:
    out = redactor.redact_message(
        "ciphertext=gAAAAAbcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH"
    )
    assert "gAAAAA" not in out
    assert "***REDACTED***" in out


def test_redaction_filter_masks_postgres_url(redactor: RedactionFilter) -> None:
    out = redactor.redact_message(
        "db=postgresql://u:p@host:5432/db driver=postgresql+psycopg://u:p@host:5432/db"
    )
    assert "postgresql://" not in out
    assert "postgresql+psycopg://" not in out
    assert "***REDACTED***" in out


def test_redaction_filter_redacts_dict_with_authorization_key(
    redactor: RedactionFilter,
) -> None:
    payload = {
        "Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "method": "POST",
        "ciphertext": "gAAAAAbcdefghijklmnopqrstuvwxyz0123456789",
        "password": "hunter2",
        "database_url": "postgresql+psycopg://u:p@h/d",
    }
    safe = redactor.redact_dict(payload)
    assert safe["Authorization"] == "***REDACTED***"
    assert safe["ciphertext"] == "***REDACTED***"
    assert safe["password"] == "***REDACTED***"
    assert safe["database_url"] == "***REDACTED***"
    assert safe["method"] == "POST"


def test_redaction_filter_recurses_into_nested_dicts(redactor: RedactionFilter) -> None:
    payload = {
        "request": {
            "headers": {"Authorization": "Bearer xyz", "X-Trace": "abc"},
            "body": {"token": "gAAAAAabcdefghijklmnop"},
        }
    }
    safe = redactor.redact_dict(payload)
    assert safe["request"]["headers"]["Authorization"] == "***REDACTED***"
    assert safe["request"]["headers"]["X-Trace"] == "abc"
    assert safe["request"]["body"]["token"] == "***REDACTED***"


def test_redaction_filter_redacts_log_record_message_and_args(
    redactor: RedactionFilter,
) -> None:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="login attempt with Authorization: Bearer eyJ.payload.sig",
        args=({"token": "gAAAAAbcdefghijklmnop"},),
        exc_info=None,
    )

    assert redactor.filter(record) is True
    assert "Bearer eyJ" not in record.msg
    assert "***REDACTED***" in record.msg
    # Python's logging module unwraps a single-element tuple args into
    # the dict directly (see logging.LogRecord.__init__), so we assert
    # the redacted key against ``record.args`` rather than ``[0]``.
    assert record.args["token"] == "***REDACTED***"


def test_redaction_filter_redacts_string_inside_list(redactor: RedactionFilter) -> None:
    payload = {
        "events": [
            "login with Bearer eyJ.payload.sig",
            "fetched postgresql+psycopg://u:p@h/d",
        ],
        "safe": ["ok"],
    }
    safe = redactor.redact_dict(payload)
    assert all("Bearer eyJ" not in v for v in safe["events"])
    assert all("postgresql+psycopg://" not in v for v in safe["events"])
    assert safe["safe"] == ["ok"]
