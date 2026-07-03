"""acceptance_criteria

Adds the additive data foundation for the canonical version-goal-to-evidence
acceptance contract:

  * ``versionacceptancecriterion`` table — version-scoped criteria with stable
    string IDs, FK to project (CASCADE) and releaseversion (CASCADE).
  * ``criterionevidence`` table — per-criterion evidence ledger, FK to project
    (CASCADE) and versionacceptancecriterion (CASCADE).
  * ``direction.criterion_keys`` JSONB column — list of criterion_key strings
    the direction is mapped to (captured at assignment).
  * ``task.criterion_ids`` JSONB column — list of criterion_key strings the
    task provides evidence for (captured at dispatch).

No existing tables or columns are modified.

Revision ID: 0354
Revises: 0353
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = "0354"
down_revision = "0353"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "versionacceptancecriterion",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("release_version_id", sa.UUID(), nullable=False),
        sa.Column("criterion_key", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["release_version_id"], ["releaseversion.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "release_version_id", "criterion_key", name="uq_vac_version_key"
        ),
    )
    op.create_index(
        "ix_vac_version",
        "versionacceptancecriterion",
        ["release_version_id"],
    )
    op.create_index(
        "ix_vac_project",
        "versionacceptancecriterion",
        ["project_id"],
    )

    op.create_table(
        "criterionevidence",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("criterion_id", sa.UUID(), nullable=False),
        sa.Column("source_kind", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("detail", JSONB(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["criterion_id"],
            ["versionacceptancecriterion.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_criterionevidence_criterion_id",
        "criterionevidence",
        ["criterion_id"],
    )
    op.create_index(
        "ix_criterionevidence_project",
        "criterionevidence",
        ["project_id"],
    )

    op.add_column(
        "direction",
        sa.Column(
            "criterion_keys",
            JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "task",
        sa.Column(
            "criterion_ids",
            JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("task", "criterion_ids")
    op.drop_column("direction", "criterion_keys")
    op.drop_index("ix_criterionevidence_project", table_name="criterionevidence")
    op.drop_index(
        "ix_criterionevidence_criterion_id", table_name="criterionevidence"
    )
    op.drop_table("criterionevidence")
    op.drop_index("ix_vac_project", table_name="versionacceptancecriterion")
    op.drop_index("ix_vac_version", table_name="versionacceptancecriterion")
    op.drop_table("versionacceptancecriterion")
