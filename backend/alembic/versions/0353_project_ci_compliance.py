"""project_ci_compliance

Adds nullable ``ci_compliance`` JSONB column to ``project`` so the per-merge
CI-compliance subsystem (Direction E) can durably store module-size guardrail
compliance for each automain-HEAD commit, keyed by SHA.

Shape: {sha: {ref, compliant: bool, violations: [{section, rel, count, cap}]}}.
NULL = the project's HEAD has never been evaluated.

Revision ID: 0353
Revises: 0352
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = "0353"
down_revision = "0352"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column("ci_compliance", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project", "ci_compliance")
