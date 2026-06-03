"""Add nullable merge_gate_runtime JSONB column to project.

Backs ``Project.merge_gate_runtime`` — a runtime-backed record of what the no-CI
merge gate ACTUALLY did the last time it ran a PR to a terminal outcome (last
run timestamp, whether a real suite executed to a pass/fail verdict, and the
most recent UNVERIFIED fall-open reason). This complements the read-only,
config-derived ``compute_merge_verification_verdict`` advisory with the live
outcome, so an operator can tell a config that *claims* tests are gated from one
whose live path fell open to an UNVERIFIED merge.

Nullable with no server default => a metadata-only ADD COLUMN (instant even on a
large project table); existing rows read NULL until the gate next runs, so the
rollout is a pure additive no-op for them and older clients are unaffected.

Per CLAUDE.md this migration is chained from the latest head (0138,
``task_blocked_state_fields``) and ``EXPECTED_SCHEMA_REVISION`` is bumped to 0139
to match.

Revision ID: 0139
Revises: 0138
Create Date: 2026-06-03 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column("merge_gate_runtime", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project", "merge_gate_runtime")
