"""Named relational query templates."""

from app.text_to_sql.allow_list import ALLOW_LIST


def _selftest() -> None:
    assert len(ALLOW_LIST.names()) == 5


if __name__ == "__main__":
    _selftest()
