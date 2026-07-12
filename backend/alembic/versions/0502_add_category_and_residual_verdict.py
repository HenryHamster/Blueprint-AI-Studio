"""Add CategoryVerdict and ResidualVerdict tables (re-gradeable verdict store).

Persistence layer of the goal-verification redesign.  Replaces the monotonic
``CriterionEvidence`` accumulation with a per-(candidate_sha, category) verdict
that a re-grade OVERWRITES, plus a per-candidate residual "uncaptured goal
aspect" record.  See :mod:`app.category_verdict_ledger`.

Revision ID: 0502
Revises: 0501
Create Date: 2026-07-11

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0502"
down_revision = "0501"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "categoryverdict",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_sha", sa.String(length=40), nullable=True),
        sa.Column("category_key", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("gap", sa.Text(), nullable=True),
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "graded_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("grader_run_id", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["releaseversion.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "version_id",
            "candidate_sha",
            "category_key",
            name="uq_categoryverdict_version_sha_category",
        ),
    )
    op.create_index(
        "ix_categoryverdict_version_id",
        "categoryverdict",
        ["version_id"],
    )

    op.create_table(
        "residualverdict",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_sha", sa.String(length=40), nullable=True),
        sa.Column("uncaptured_gap", sa.Text(), nullable=True),
        sa.Column(
            "graded_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["releaseversion.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "version_id",
            "candidate_sha",
            name="uq_residualverdict_version_sha",
        ),
    )
    op.create_index(
        "ix_residualverdict_version_id",
        "residualverdict",
        ["version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_residualverdict_version_id",
        table_name="residualverdict",
    )
    op.drop_table("residualverdict")
    op.drop_index(
        "ix_categoryverdict_version_id",
        table_name="categoryverdict",
    )
    op.drop_table("categoryverdict")
