"""Course aggregate template."""
from app.text_to_sql.allow_list import ALLOW_LIST

NAME = "course_aggregate"
def render(*, tenant_id, course_id): return ALLOW_LIST.resolve(NAME, {"tenant_id": tenant_id, "course_id": course_id})
def _selftest(): assert "GROUP BY" in render(tenant_id="t", course_id=1)
if __name__ == "__main__": _selftest()
