"""Query-shape regressions for the operator attention read model."""

from __future__ import annotations

import uuid

from sqlalchemy.dialects import postgresql

from app.models import Task, TaskStatus
from app.operator_attention import _local_runner_hold_preview_statement


def test_local_runner_hold_union_keeps_narrow_task_projection():
    project_id = uuid.uuid4()
    statement = _local_runner_hold_preview_statement(
        (
            Task.project_id == project_id,
            Task.status == TaskStatus.QUEUED,
        ),
        (
            Task.project_id == project_id,
            Task.status == TaskStatus.PROVISIONING,
        ),
    )

    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "UNION ALL" in sql
    assert sql.count("LIMIT 4") == 2
    assert "status_log" not in sql
    assert "payloads" not in sql
    assert "api_call_summary" not in sql
    assert "fix_loop_history" not in sql
