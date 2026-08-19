"""Pydantic schemas for the auth endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    """Body for ``POST /auth/login``.

    The ``id_token`` is the Google Sign-In id_token that the mobile
    client obtained via the GoogleSignIn SDK; PaquitoBot only verifies
    it, never receives a code or a redirect.
    """

    model_config = ConfigDict(extra="forbid")

    id_token: str = Field(
        ...,
        min_length=20,
        max_length=4096,
        description=(
            "Google Sign-In id_token obtained by the mobile client; the "
            "backend verifies its signature against Google's public keys "
            "and issues a backend JWT in exchange."
        ),
    )


class LoginResponse(BaseModel):
    """Body returned by ``POST /auth/login`` on success."""

    model_config = ConfigDict(extra="forbid")

    access_token: str = Field(..., min_length=20)
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(..., ge=1, le=86400)
    sub: str = Field(..., min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=320)


__all__ = ["LoginRequest", "LoginResponse"]