"""Pydantic schemas for error responses.

These models exist so every error path emits the same JSON shape. The body
MUST never contain credentials — see ``app/core/errors.py`` for the
``safe_message`` enforcement.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    """Public error envelope.

    Fields:

    - ``code``: machine-readable error code (e.g. ``canvas_token_invalid``).
    - ``message``: human-readable, redacted message; never carries tokens,
      ciphertext, ``Authorization`` headers, or DB URLs.
    - ``correlation_id``: UUID v4 shared with the response header and the
      server log line for the same request.
    - ``details``: optional, additional structured context (still redacted).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(..., min_length=1, max_length=64)
    message: str = Field(..., min_length=1, max_length=512)
    correlation_id: str = Field(..., min_length=1, max_length=64)
    details: dict[str, Any] | None = None
