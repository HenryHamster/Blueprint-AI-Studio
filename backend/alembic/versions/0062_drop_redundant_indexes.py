"""Drop redundant indexes shadowed by broader composites or unique constraints.

Audit 2026-04-29 (DB comprehensive remediation, Phase D1 / findings H-S1, H-S2,
H-S3): three indexes are redundant on Postgres because a broader composite or
a unique constraint already covers their use cases via left-prefix matching:

  - ``ix_task_project_id_created_at`` (added in 0053) on
    ``task(project_id, created_at)`` is fully shadowed by
    ``ix_task_project_status_created`` on
    ``task(project_id, status, created_at)``. Postgres can use the leftmost
    column ``project_id`` for equality and the trailing ``created_at`` for
    ordering even when ``status`` is unconstrained, so the narrower index
    only adds write amplification.
  - ``ix_task_status_priority`` on ``task(status, priority)`` is shadowed by
    ``ix_task_status_priority_created`` on
    ``task(status, priority, created_at)`` for the same left-prefix reason.
  - ``ix_userintegration_user_id`` (a UNIQUE index added in the original
    0001 schema) is replaced by the named ``uq_userintegration_user_id``
    UNIQUE constraint added in 0049. The constraint is the canonical
    enforcement; the older index is duplicate work on every INSERT/UPDATE.

Net effect: three fewer index trees on every ``task`` and
``userintegration`` write. The bytes saved are modest; the WAL volume saved
on the heartbeat-heavy ``task`` table is the more meaningful win.

Revision ID: 0060
Revises: 0059
Create Date: 2026-04-29 00:00:00.000000

"""
from alembic import op


revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_task_project_id_created_at", table_name="task")
    op.drop_index("ix_task_status_priority", table_name="task")
    op.drop_index("ix_userintegration_user_id", table_name="userintegration")


def downgrade() -> None:
    op.create_index(
        "ix_userintegration_user_id",
        "userintegration",
        ["user_id"],
        unique=True,
    )
    op.create_index(
        "ix_task_status_priority",
        "task",
        ["status", "priority"],
    )
    op.create_index(
        "ix_task_project_id_created_at",
        "task",
        ["project_id", "created_at"],
    )
