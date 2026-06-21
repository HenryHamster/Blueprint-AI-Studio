"""Add QualificationVerdict blind merge-grade goal-coverage columns.

Durably preserves the blind merge-grade verdict (dir d-c0fe439f) computed
during release finalization in the immutable per-verdict calibration trail: an
uninvested ``release_merge_grade`` agent grades the merged diffs against the
frozen version goal and emits a goal-coverage verdict + the single biggest gap.

Purely additive: all four columns are nullable, so pre-existing verdict rows
(written before this gate) carry no merge-grade data and persist NULL — fully
backward compatible.  Nullable columns default to NULL on existing rows, which
is the safe default here (an absent grade reads as "ungraded", never as a pass).

Columns:
  * merge_grade_status        — "pass" | "fail" | "not_applicable" (empty goal).
  * merge_grade_coverage      — goal-coverage score in [0,1] (NULL when ungraded).
  * merge_grade_biggest_gap   — one-sentence biggest thing not done toward goal.
  * merge_grade_detail        — the raw graded artifact (covers_goal, scores).

Revision ID: 0330
Revises: 0329
Create Date: 2026-06-20 00:00:00.000000

NOTE: 0330 chains from 0329 (qualificationverdict merge_grade columns)
which itself chains from 0328 -> 0327 -> 0317 -> 0310.  This keeps the Alembic
migration graph linear — a single head — so ``alembic upgrade head`` applies
all migrations cleanly on merge.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0330"
down_revision = "0329"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "qualificationverdict",
        sa.Column("merge_grade_status", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("merge_grade_coverage", sa.Float(), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("merge_grade_biggest_gap", sa.Text(), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column(
            "merge_grade_detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("qualificationverdict", "merge_grade_detail")
    op.drop_column("qualificationverdict", "merge_grade_biggest_gap")
    op.drop_column("qualificationverdict", "merge_grade_coverage")
    op.drop_column("qualificationverdict", "merge_grade_status")
