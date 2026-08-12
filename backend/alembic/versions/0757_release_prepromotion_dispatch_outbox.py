"""Persist candidate-bound prepromotion rehearsal dispatch claims.

Revision ID: 0757
Revises: 0756
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0757"
down_revision = "0756"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "releaseprepromotiondispatch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("release_version_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_sha", sa.String(length=40), nullable=False),
        sa.Column(
            "workflow_ref", sa.String(length=255), nullable=False, server_default="main"
        ),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="claimed"
        ),
        sa.Column(
            "dispatch_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("workflow_run_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["release_version_id"], ["releaseversion.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "release_version_id",
            "candidate_sha",
            name="uq_releaseprepromotiondispatch_version_candidate",
        ),
    )
    op.create_index(
        "ix_releaseprepromotiondispatch_status_retry",
        "releaseprepromotiondispatch",
        ["status", "next_retry_at", "claimed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_releaseprepromotiondispatch_status_retry",
        table_name="releaseprepromotiondispatch",
    )
    op.drop_table("releaseprepromotiondispatch")
