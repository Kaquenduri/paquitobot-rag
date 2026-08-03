"""Class score statistics template."""
from app.text_to_sql.allow_list import ALLOW_LIST

NAME = "class_score_statistics"
def render(*, tenant_id, course_id): return ALLOW_LIST.resolve(NAME, {"tenant_id": tenant_id, "course_id": course_id})
def _selftest(): assert "average_score" in render(tenant_id="t", course_id=1)
if __name__ == "__main__": _selftest()
