"""Add QualificationDimensionResult table for per-dimension verdict ledger.

Phase 1a of the per-dimension fact ledger redesign (knowledge doc 28f9d31a):
dual-write one row per gate alongside the existing last_qualification /
QualificationVerdict path; no behavior change to the decision path.

Revision ID: 0469
Revises: 0468
Create Date: 2026-07-10 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0469"
down_revision = "0468"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "qualificationdimensionresult",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("release_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_sha", sa.String(length=40), nullable=False),
        sa.Column("dimension", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("sub_score", sa.Float(), nullable=True),
        sa.Column("blocker", sa.String(length=64), nullable=True),
        sa.Column("audited_surface_hash", sa.String(length=64), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("producing_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "computed_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["release_version_id"],
            ["releaseversion.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "release_version_id",
            "candidate_sha",
            "dimension",
            name="uq_qualificationdimensionresult_version_sha_dim",
        ),
    )
    op.create_index(
        "ix_qualificationdimensionresult_project_id",
        "qualificationdimensionresult",
        ["project_id"],
    )
    op.create_index(
        "ix_qualificationdimensionresult_release_version_id",
        "qualificationdimensionresult",
        ["release_version_id"],
    )
    op.create_index(
        "ix_qualificationdimensionresult_project_computed",
        "qualificationdimensionresult",
        ["project_id", "computed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_qualificationdimensionresult_project_computed",
        table_name="qualificationdimensionresult",
    )
    op.drop_index(
        "ix_qualificationdimensionresult_release_version_id",
        table_name="qualificationdimensionresult",
    )
    op.drop_index(
        "ix_qualificationdimensionresult_project_id",
        table_name="qualificationdimensionresult",
    )
    op.drop_table("qualificationdimensionresult")
