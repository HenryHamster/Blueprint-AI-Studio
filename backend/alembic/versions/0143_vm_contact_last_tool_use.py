"""Add ``vmcontact.last_tool_use_at`` (off-row progress signal).

Follow-up to the heartbeat false-reap fix (0142 ``vmcontact``). The base
``vmcontact`` table broke the ``last_heartbeat`` false-reap: a VM whose
contended ``Task`` row is held FOR UPDATE by the rollup gets its heartbeat
skipped (409), so ``Task.last_heartbeat`` never advances even though the agent
is alive. But the SAME lock-starved heartbeat also drops
``Task.last_tool_use_at`` — so the reaper's progress-stall branch
(``_classify_orphan`` IN_PROGRESS: fresh heartbeat + stale ``last_tool_use_at``
→ ``agent_stalled``) can still false-reap a live, actively-PROGRESSING agent
whose tool-use stamp was merely lock-dropped.

This migration adds an off-row ``last_tool_use_at`` column to ``vmcontact``,
keyed (like ``last_contact_at``) on ``vm_instance_id`` so the heartbeat handler
can upsert it on its OWN row, never the contended Task row. The reaper consults
it in the progress-stall branch: a tool-use more recent than the progress-stale
cutoff proves the agent is making tool calls (its on-row stamp was just
lock-dropped) → suppress the reap. A genuinely stalled agent (no tool calls
anywhere) leaves it stale/NULL and is still reaped on the existing schedule.

Nullable: heartbeats without tool telemetry leave it NULL, which falls back to
the on-row ``Task.last_tool_use_at`` behaviour (no regression). On SQLite (local
test fixture) the column is created from ``SQLModel.metadata.create_all``; this
migration is the Postgres path. See ``app.models.VmContact``.

Per CLAUDE.md this migration is chained from the latest head (0142) and
``EXPECTED_SCHEMA_REVISION`` is bumped to match.

Revision ID: 0143
Revises: 0142
Create Date: 2026-06-04 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0143"
down_revision = "0142"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent guard mirroring 0140/0142: never wedge a deploy on a duplicate
    # ADD COLUMN if the column is somehow already present (back-merge renumber /
    # re-run). On a fresh DB the column doesn't exist and is added normally.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("vmcontact"):
        # Defensive: 0142 must have created the base table. If it is somehow
        # absent, create it whole (matching 0142) so this revision is safe to
        # apply standalone — then the column exists by construction.
        op.create_table(
            "vmcontact",
            sa.Column(
                "vm_instance_id", sa.String(), primary_key=True, nullable=False
            ),
            sa.Column("last_contact_at", sa.DateTime(), nullable=False),
            sa.Column("last_tool_use_at", sa.DateTime(), nullable=True),
        )
        return
    existing_cols = {c["name"] for c in insp.get_columns("vmcontact")}
    if "last_tool_use_at" in existing_cols:
        return
    op.add_column(
        "vmcontact",
        sa.Column("last_tool_use_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("vmcontact", "last_tool_use_at")
