import importlib
import importlib.util

import pytest

APP_MODULES = (
    "app",
    "app.core",
    "app.security",
    "app.canvas",
    "app.rag",
    "app.text_to_sql",
    "app.sync",
)


@pytest.mark.parametrize("module_name", APP_MODULES)
def test_application_modules_import(module_name: str) -> None:
    try:
        available = importlib.util.find_spec(module_name)
    except ModuleNotFoundError:
        available = None
    if available is None:
        pytest.skip(f"{module_name} is introduced by a later chained PR")
    assert importlib.import_module(module_name).__name__ == module_name


def test_pytest_is_installed_in_the_venv() -> None:
    module = importlib.import_module("pytest")
    assert isinstance(getattr(module, "__version__", None), str)
