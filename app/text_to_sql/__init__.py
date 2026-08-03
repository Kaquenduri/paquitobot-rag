"""Text-to-SQL package."""

from .allow_list import ALLOW_LIST, AllowList, TemplateNotAllowed

__all__ = ["ALLOW_LIST", "AllowList", "TemplateNotAllowed"]


def _selftest() -> None:
    assert "assignment_score" in ALLOW_LIST.names()


if __name__ == "__main__":
    _selftest()
