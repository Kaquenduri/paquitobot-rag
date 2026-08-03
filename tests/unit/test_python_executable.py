import re
import subprocess
from pathlib import Path


def _pinned_pytest_version() -> str:
    path = Path(__file__).resolve().parents[2] / "requirements.txt"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("pytest=="):
            return line.removeprefix("pytest==")
    raise AssertionError("requirements.txt does not contain an exact pytest pin")


def test_python_executable_reports_version(python_executable: str) -> None:
    result = subprocess.run(
        [python_executable, "--version"], check=True, capture_output=True, text=True, timeout=30
    )
    assert f"{result.stdout}{result.stderr}".strip().startswith("Python ")


def test_venv_pytest_version_matches_requirements(python_executable: str) -> None:
    result = subprocess.run(
        [python_executable, "-m", "pytest", "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    match = re.search(r"\bpytest\s+([0-9][^\s]*)", f"{result.stdout}{result.stderr}")
    assert match is not None
    assert match.group(1) == _pinned_pytest_version()
