"""Heal poisoned problem_model rows by stripping stale packer-marker keys.

The packer that serialises the problem_model for agent context occasionally
stamped a ``truncated: true`` marker at the top level before emitting the
payload to the agent.  If that payload was then written back to the durable
JSONB column it became a poison pill: every subsequent ProblemModel parse
raises a ValidationError because extra='forbid' rejects unknown keys.

apply_agent_update() already self-heals on the next agent write by stripping
unknown keys, but projects with no pending strategic cycle stay poisoned
indefinitely.  This migration strips ``truncated``, ``_gap_memory_trimmed``,
and ``_agent_view_floor_applied`` — the full set of ephemeral directive keys
known to have leaked into stored rows — from every project row so no project
stays wedged waiting for an agent write.

Revision ID: 0672
Revises: 0671
Create Date: 2026-07-22

"""

from alembic import op
import sqlalchemy as sa

revision = "0672"
down_revision = "0671"
branch_labels = None
depends_on = None

# Keys that are ephemeral packer/agent-view directives and must never be
# persisted in the durable problem_model JSONB.
_EPHEMERAL_KEYS = ("truncated", "_gap_memory_trimmed", "_agent_view_floor_applied")


def upgrade() -> None:
    conn = op.get_bind()
    for key in _EPHEMERAL_KEYS:
        conn.execute(
            sa.text(
                "UPDATE project "
                "SET problem_model = problem_model - :key "
                "WHERE problem_model ? :key"
            ),
            {"key": key},
        )


def downgrade() -> None:
    # Ephemeral keys are invalid state; there is nothing meaningful to restore.
    pass
