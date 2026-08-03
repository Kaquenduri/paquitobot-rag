"""Legacy Chroma persistence helper (PR 3 task 3.6).

PR 3 removes the destructive ``shutil.rmtree(CHROMA_PATH)`` that the
original script used to wipe the on-disk vector store on every call.
The new contract:

- Default behaviour is **upsert**: existing chunks stay, new chunks
  are added. Repeated calls with the same inputs are idempotent at
  the user-facing level (Chroma's own storage handles deduplication
  by document id).
- A destructive reset is still possible but is **explicit**. The
  caller must pass ``reset=True`` *and* either set the environment
  variable ``CHROMA_RESET=1`` *or* include ``--reset`` in
  ``sys.argv``. Both gates are required so an unattended script
  cannot accidentally wipe the store by importing this module.

PR 5 will replace this helper with ``app.rag.vector_store`` (which
talks to PGVector instead of Chroma). Until then the helper stays
in place so the LEGACY_MODE script-mode pipeline still runs.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable

from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document

CHROMA_PATH = "chroma"

# Workflow-state values that trigger a destructive reset. Kept in
# this module so the gate is auditable in one place.
_ALLOWED_RESET_FLAGS = ("--reset", "--wipe")
_RESET_ENV_VAR = "CHROMA_RESET"


def _reset_is_explicitly_requested(argv: Iterable[str] | None = None) -> bool:
    """Return True only when the caller asks for a destructive reset."""
    argv = list(argv if argv is not None else sys.argv[1:])
    env_value = os.environ.get(_RESET_ENV_VAR, "").strip().lower()
    env_flag = env_value in {"1", "true", "yes", "on"}
    argv_flag = any(flag in argv for flag in _ALLOWED_RESET_FLAGS)
    return env_flag and argv_flag


def reset_chroma_db(path: str | None = None) -> bool:
    """Delete ``path`` on disk when both reset gates are set.

    Returns ``True`` when the directory was removed, ``False``
    otherwise. The function is a no-op unless both the env flag and
    the CLI flag are present so an unattended script that imports
    this module cannot wipe state by accident.
    """
    import shutil

    target = path if path is not None else CHROMA_PATH

    if not _reset_is_explicitly_requested():
        return False
    if not os.path.exists(target):
        return False
    shutil.rmtree(target)
    return True


def save_to_chroma_db(
    chunks: list[Document],
    embedding_model,
    *,
    reset: bool = False,
    chroma_path: str | None = None,
) -> Chroma:
    """Upsert ``chunks`` into the local Chroma store.

    Parameters
    ----------
    chunks:
        Documents to add. The function never deletes the directory
        unless the caller explicitly passes ``reset=True`` AND one
        of the reset gates (``CHROMA_RESET=1`` env or ``--reset``
        CLI flag) is set.
    embedding_model:
        LangChain embeddings instance (e.g. ``OllamaEmbeddings``).
    reset:
        Opt-in to a destructive wipe. Both gates are required; see
        :func:`reset_chroma_db`.
    chroma_path:
        Override for the on-disk path (tests use ``tmp_path``).
        Defaults to :data:`CHROMA_PATH`; the lookup happens at call
        time so monkeypatching the module constant works.
    """
    target_path = chroma_path if chroma_path is not None else CHROMA_PATH

    if reset:
        # Honors the gate; never deletes unless both env + argv agree.
        reset_chroma_db(target_path)

    db = Chroma.from_documents(
        chunks,
        persist_directory=target_path,
        embedding=embedding_model,
    )

    print(f"Save {len(chunks)} documents to Chroma DB at {target_path}")
    return db


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI parser used by the legacy script-mode entry point."""
    parser = argparse.ArgumentParser(
        description=(
            "Legacy Chroma helper. The --reset flag must also be "
            "paired with CHROMA_RESET=1 in the environment; both "
            "gates are required to wipe the on-disk store."
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Destructive wipe. Requires CHROMA_RESET=1 in env too.",
    )
    return parser


__all__ = [
    "CHROMA_PATH",
    "reset_chroma_db",
    "save_to_chroma_db",
]