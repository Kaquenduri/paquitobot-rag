"""Unit tests for ``src.chroma_db`` after the PR 3 task 3.6 update.

The destructive ``shutil.rmtree(CHROMA_PATH)`` was removed from the
default path; both the env flag (``CHROMA_RESET=1``) and the CLI
flag (``--reset``) must be set for a wipe to happen.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def fresh_chroma_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a stub chroma directory and patch the module constant."""
    target = tmp_path / "chroma"
    target.mkdir()
    (target / "index.bin").write_bytes(b"placeholder")
    monkeypatch.setattr("src.chroma_db.CHROMA_PATH", str(target))
    return target


def test_reset_is_noop_when_no_flags_are_set(
    fresh_chroma_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHROMA_RESET", raising=False)
    monkeypatch.setattr(sys, "argv", ["prog"])

    from src.chroma_db import reset_chroma_db

    removed = reset_chroma_db()
    assert removed is False
    assert fresh_chroma_path.exists()


def test_reset_is_noop_when_only_env_flag_is_set(
    fresh_chroma_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHROMA_RESET", "1")
    monkeypatch.setattr(sys, "argv", ["prog"])

    from src.chroma_db import reset_chroma_db

    removed = reset_chroma_db()
    assert removed is False
    assert fresh_chroma_path.exists()


def test_reset_is_noop_when_only_argv_flag_is_set(
    fresh_chroma_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHROMA_RESET", raising=False)
    monkeypatch.setattr(sys, "argv", ["prog", "--reset"])

    from src.chroma_db import reset_chroma_db

    removed = reset_chroma_db()
    assert removed is False
    assert fresh_chroma_path.exists()


def test_reset_removes_directory_when_both_flags_are_set(
    fresh_chroma_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHROMA_RESET", "1")
    monkeypatch.setattr(sys, "argv", ["prog", "--reset"])

    from src.chroma_db import reset_chroma_db

    removed = reset_chroma_db()
    assert removed is True
    assert not fresh_chroma_path.exists()


def test_save_to_chroma_db_is_upsert_by_default(
    fresh_chroma_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``save_to_chroma_db`` MUST NOT delete the existing directory."""
    from src import chroma_db

    calls: list[Path] = []

    class _StubChroma:
        @staticmethod
        def from_documents(docs: list[Any], *, persist_directory: str, embedding: Any) -> Any:
            calls.append(Path(persist_directory))
            return object()

    monkeypatch.setattr(chroma_db, "Chroma", _StubChroma)
    monkeypatch.delenv("CHROMA_RESET", raising=False)
    monkeypatch.setattr(sys, "argv", ["prog"])

    chroma_db.save_to_chroma_db(
        chunks=[],
        embedding_model=object(),
        chroma_path=str(fresh_chroma_path),
    )

    assert fresh_chroma_path.exists()
    assert calls and calls[0] == fresh_chroma_path


def test_save_to_chroma_db_respects_explicit_reset(
    fresh_chroma_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``reset=True`` + explicit gates wipes the directory before upserting."""
    from src import chroma_db

    calls: list[Path] = []

    class _StubChroma:
        @staticmethod
        def from_documents(docs: list[Any], *, persist_directory: str, embedding: Any) -> Any:
            calls.append(Path(persist_directory))
            return object()

    monkeypatch.setattr(chroma_db, "Chroma", _StubChroma)
    monkeypatch.setenv("CHROMA_RESET", "1")
    monkeypatch.setattr(sys, "argv", ["prog", "--reset"])

    chroma_db.save_to_chroma_db(chunks=[], embedding_model=object(), reset=True)

    assert not fresh_chroma_path.exists()
    assert calls and calls[0] == fresh_chroma_path


def test_save_to_chroma_db_reset_param_without_gates_is_noop(
    fresh_chroma_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing ``reset=True`` without both gates MUST NOT delete the directory."""
    from src import chroma_db

    class _StubChroma:
        @staticmethod
        def from_documents(docs: list[Any], *, persist_directory: str, embedding: Any) -> Any:
            return object()

    monkeypatch.setattr(chroma_db, "Chroma", _StubChroma)
    monkeypatch.delenv("CHROMA_RESET", raising=False)
    monkeypatch.setattr(sys, "argv", ["prog"])

    chroma_db.save_to_chroma_db(chunks=[], embedding_model=object(), reset=True)

    assert fresh_chroma_path.exists()