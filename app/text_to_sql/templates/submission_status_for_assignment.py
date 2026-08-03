"""Submission status template."""
from app.text_to_sql.allow_list import ALLOW_LIST

NAME = "submission_status_for_assignment"
def render(*, tenant_id, assignment_id): return ALLOW_LIST.resolve(NAME, {"tenant_id": tenant_id, "assignment_id": assignment_id})
def _selftest(): assert "workflow_state" in render(tenant_id="t", assignment_id=1)
if __name__ == "__main__": _selftest()
