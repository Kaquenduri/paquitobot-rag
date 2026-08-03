"""HTTP controllers package (PR 4).

Each sub-module mounts its own ``APIRouter``; ``app.main`` optionally
includes them via ``register_*_router`` helpers. Tests instantiate
dedicated FastAPI apps and ``include_router`` directly so PR-level
scopes stay isolated.
"""

from __future__ import annotations

__all__ = ["auth", "sync"]
