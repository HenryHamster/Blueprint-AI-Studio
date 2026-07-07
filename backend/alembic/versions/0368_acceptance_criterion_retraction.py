"""acceptance_criterion_retraction

Add lifecycle/audit fields for retracting or superseding version acceptance
criteria without deleting their evidence history.

Revision ID: 0368
Revises: 0367
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0368"
down_revision = "0367"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Alembic autogenerate also reported long-standing metadata/history drift
    # on unrelated tables. Those changes are intentionally omitted; this
    # migration is scoped to the new VersionAcceptanceCriterion lifecycle fields.
    op.add_column(
        "versionacceptancecriterion",
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
    )
    op.add_column(
        "versionacceptancecriterion",
        sa.Column("retraction_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "versionacceptancecriterion",
        sa.Column("retracted_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "versionacceptancecriterion",
        sa.Column("retracted_by", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "versionacceptancecriterion",
        sa.Column("superseded_by_key", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("versionacceptancecriterion", "superseded_by_key")
    op.drop_column("versionacceptancecriterion", "retracted_by")
    op.drop_column("versionacceptancecriterion", "retracted_at")
    op.drop_column("versionacceptancecriterion", "retraction_reason")
    op.drop_column("versionacceptancecriterion", "status")
