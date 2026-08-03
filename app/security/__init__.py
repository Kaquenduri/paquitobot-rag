"""Security primitives: token custody, backend JWT, log redaction.

This package is the only place the application is allowed to touch
plaintext Canvas tokens or backend JWT credentials. Every other module
MUST go through :mod:`app.security.token_crypto`,
:mod:`app.security.backend_auth`, or :mod:`app.security.redaction` to
reach a credential.
"""

from __future__ import annotations

__all__ = ["backend_auth", "redaction", "token_crypto"]
