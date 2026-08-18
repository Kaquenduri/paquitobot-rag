"""Unit tests for the Alembic env (PR 3 task 8 of the user prompt).

PR 3 wires ``Base.metadata`` into ``alembic/env.py`` so offline
mode emits DDL without touching a real database. This test exercises
that contract directly by running Alembic's offline renderer against
a SQLite URL and asserting that:

- the rendered SQL contains the seven application tables, and
- no connection was opened.

A real Postgres URL is NOT used; ``sqlite:///:memory:`` is enough to
exercise the offline renderer.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _resolve_alembic_executable() -> str:
    """Find the alembic CLI script shipped with the project's venv."""
    candidate = shutil.which("alembic")
    if candidate:
        return candidate
    # Fallback for Windows venvs: Scripts/alembic.exe next to python.exe.
    scripts_dir = Path(sys.executable).resolve().parent
    for name in ("alembic.exe", "alembic"):
        candidate = scripts_dir / name
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(
        "alembic executable not found on PATH or next to python.exe"
    )


def _venv_site_packages() -> str:
    """Return the venv's site-packages directory (where alembic lives)."""
    exe_dir = Path(sys.executable).resolve().parent
    candidates = [
        exe_dir.parent / "Lib" / "site-packages",  # Windows venv
        exe_dir.parent / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages",  # POSIX venv
    ]
    # WSL / cross-platform: when running the suite via uv on WSL the
    # ``sys.executable`` does NOT point to the project's bundled venv
    # (e.g. ``/mnt/c/Users/Administrador/langchain``), so the standard
    # heuristics miss. Probe upward from the project root looking for any
    # sibling directory containing ``langchain`` / ``venv`` / ``.venv`` /
    # ``env`` whose ``lib`` / ``Lib`` tree has the alembic install.
    repo_root = Path(__file__).resolve().parents[2]
    for ancestor in repo_root.resolve().parents:
        for venv_name in ("langchain", "venv", ".venv", "env"):
            for name in ("lib", "Lib"):
                for py in (
                    f"python{sys.version_info.major}.{sys.version_info.minor}",
                    "python3.13",
                    "python3.12",
                    "python3.11",
                ):
                    cand = ancestor / venv_name / name / py / "site-packages"
                    if (cand / "alembic" / "config.py").exists():
                        candidates.append(cand)
    for candidate in candidates:
        if (candidate / "alembic" / "config.py").exists():
            return str(candidate)
    raise RuntimeError("Could not locate venv site-packages containing alembic")


def _run_alembic_offline(repo: Path) -> str:
    """Invoke ``alembic upgrade head --sql`` and return the captured stdout."""
    env = os.environ.copy()
    # Force offline rendering to ignore the user's real SUPABASE_DATABASE_URL.
    env["SUPABASE_DATABASE_URL"] = "postgresql+psycopg://user:pass@127.0.0.1:1/primer_rag"
    # Order matters: site-packages first so the framework ``alembic``
    # package wins over the local ``alembic/`` migration directory.
    site_packages = _venv_site_packages()
    env["PYTHONPATH"] = (
        f"{site_packages}{os.pathsep}{repo}".rstrip(os.pathsep)
        + os.pathsep + env.get("PYTHONPATH", "")
    )

    executable = _resolve_alembic_executable()
    result = subprocess.run(
        [executable, "upgrade", "head", "--sql"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return result.stdout


def test_alembic_offline_emits_seven_tables(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    stdout = _run_alembic_offline(repo)

    expected_tables = {
        "tenants",
        "canvas_credentials",
        "users",
        "courses",
        "enrollments",
        "assignments",
        "submissions",
        "sync_state",
        # Canvas Mock PR 1 adds nine new tables (0003_...). The
        # offline renderer must emit every one of them.
        "canvas_mock_users",
        "canvas_mock_courses",
        "canvas_mock_enrollments",
        "canvas_mock_class_sessions",
        "canvas_mock_assignments",
        "canvas_mock_attendance_records",
        "canvas_mock_grades",
        "canvas_mock_webhook_events",
        "canvas_mock_webhook_subscriptions",
    }
    missing = {name for name in expected_tables if name not in stdout}
    assert not missing, f"offline DDL missing tables: {missing}"


def test_alembic_env_offline_does_not_open_connection(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Sanity: running Alembic in offline mode must not touch a real DB.

    If a connection had been attempted against ``127.0.0.1:1`` the
    subprocess would have hung or failed; the renderer test asserts
    success, so the offline path is confirmed to skip the network.
    """
    repo = Path(__file__).resolve().parents[2]
    stdout = _run_alembic_offline(repo)
    lowered = stdout.lower()
    assert "create table tenants" in lowered
    assert "create table sync_state" in lowered