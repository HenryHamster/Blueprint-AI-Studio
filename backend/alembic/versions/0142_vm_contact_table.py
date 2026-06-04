"""Add the uncontended VM-liveness signal table (``vmcontact``).

Hotfix for live agent VMs being falsely reaped as ``AGENT_TIMEOUT``. The
heartbeat endpoint (``routers/agent.py``) updates the contended ``Task`` row
under ``with_for_update(skip_locked=True)``; while the rollup
(``rollup_management`` locks all sibling children FOR UPDATE) holds that row,
the heartbeat is skipped and ``Task.last_heartbeat`` never advances even though
the agent is alive and calling in. The reaper reads only ``Task.last_heartbeat``
so a lock-starved-but-live VM is misclassified as dead and reaped mid-flight.

This table is the liveness signal recorded OFF the contended task row: the
heartbeat handler upserts ``vmcontact(vm_instance_id, last_contact_at=now())``
on its own row (committed independently, before the contended task-row read),
so the write can never be blocked by the rollup's task-row lock. The reaper
treats a ``last_contact_at`` within the SAME heartbeat-stale cutoff as proof the
agent is alive and suppresses the reap; a genuinely dead agent (no requests →
no/stale row) is still reaped on the existing schedule. One row per VM, churned
by UPSERT.

Per CLAUDE.md this migration is chained from the latest head (0141) and
``EXPECTED_SCHEMA_REVISION`` is bumped to match. On SQLite (local test fixture)
the table is created from ``SQLModel.metadata.create_all``; this migration is
the Postgres path. See ``app.models.VmContact``.

Revision ID: 0142
Revises: 0141
Create Date: 2026-06-04 09:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0142"
down_revision = "0141"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent guard mirroring 0140: never wedge a deploy on a duplicate
    # CREATE if the table is somehow already present (back-merge renumbering /
    # re-run). On a fresh DB the table doesn't exist and is created normally.
    bind = op.get_bind()
    if sa.inspect(bind).has_table("vmcontact"):
        return
    op.create_table(
        "vmcontact",
        sa.Column("vm_instance_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("last_contact_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("vmcontact")
