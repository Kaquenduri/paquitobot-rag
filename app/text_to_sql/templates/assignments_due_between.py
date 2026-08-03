"""Assignments due-between template."""
from app.text_to_sql.allow_list import ALLOW_LIST

NAME = "assignments_due_between"
def render(*, tenant_id, start_at, end_at): return ALLOW_LIST.resolve(NAME, {"tenant_id": tenant_id, "start_at": start_at, "end_at": end_at})
def _selftest(): assert "due_at" in render(tenant_id="t", start_at="a", end_at="b")
if __name__ == "__main__": _selftest()
