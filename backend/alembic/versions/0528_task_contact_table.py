"""Add ``taskcontact`` — TASK-scoped off-row liveness/progress signal.

``vmcontact`` (0142 + 0144) is primary-keyed on ``vm_instance_id`` — "one row per
VM". That identity holds for GCE agent VMs (one task per VM) but is FALSE for
fleet runners: ``scripts/local_runner.py`` multiplexes MANY concurrent tasks onto
one ``vm_instance_id='runner:<id>'``. Every task on a runner therefore shares ONE
``vmcontact`` row, whose ``last_tool_use_at`` the heartbeat handler advances with
``GREATEST(...)`` from ANY agent on that runner.

That produced a permanent zombie class (2026-07-12): a hung-but-heartbeating
agent (wedged LLM call / spin loop) co-resident with any actively working sibling
is NEVER stall-reaped, because the sibling's tool calls keep the shared progress
stamp fresh and the reaper's ``agent_stalled`` override reads that shared stamp.
The runner self-report fix (#14413) cannot help by construction — a hung-but-alive
process is still in the runner's ``active_task_ids``. With no absolute IN_PROGRESS
runtime ceiling, nothing else ever fires: the task holds a runner slot, an account
slot, and pins its parent lineage indefinitely.

``taskcontact`` restores the "one row per unit of work" identity the reaper's
overrides actually assume. The heartbeat handler upserts BOTH rows (shared
``vmcontact`` unchanged, plus this per-task row); the reaper reads THIS row for
``runner:``-prefixed vm ids and keeps reading ``vmcontact`` for 1-task-per-VM GCE
ids, where the shared row is already task-scoped by construction. GCE
lock-starvation false-reap protection is therefore preserved byte-for-byte.

Rollout-safe: an ABSENT row is never evidence of death — the reaper falls back to
the shared ``vmcontact`` row (today's behaviour) when no per-task row exists yet,
so a task that has not yet heartbeated under the new handler cannot be
false-reaped. No backfill is needed or possible (there is no per-task history to
reconstruct); live tasks acquire a row within one heartbeat interval (~30-60s).

Per CLAUDE.md this is a NEW revision chained from the current head — no existing
migration file is modified. The chain tail is
0503 -> 0525 -> 0526 (``project_compute_image``) -> 0527
(``task_updated_at_vm_dispatched_at_indexes``) -> 0528 (this), leaving exactly one
head. ``EXPECTED_SCHEMA_REVISION`` in ``backend/app/main.py`` is bumped to match.
See ``app.models.TaskContact``.

Revision ID: 0528
Revises: 0527
Create Date: 2026-07-12 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0528"
down_revision = "0527"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent guard mirroring 0140/0142/0144: never wedge a deploy on a
    # duplicate CREATE TABLE (re-run / back-merge renumber). On a fresh DB the
    # table does not exist and is created normally.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("taskcontact"):
        return
    op.create_table(
        "taskcontact",
        sa.Column(
            "task_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("last_contact_at", sa.DateTime(), nullable=False),
        sa.Column("last_tool_use_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    # Mirror the ``upgrade`` guard: ``drop_table`` raises if the table is absent
    # (e.g. downgrading a DB that never ran the guarded ``upgrade`` because the
    # table pre-existed, or a re-run downgrade). Never wedge on a missing table.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("taskcontact"):
        return
    op.drop_table("taskcontact")
