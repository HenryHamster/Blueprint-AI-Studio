"""capability_proof_records

Adds nullable ``capability_proof_records`` JSONB column to ``project`` so the
pipeline can durably store evidence-grounded proof records for each capability
a task advertises via ``provides_capability``.

Shape: {normalized_cap_name: [{task_id, proved_at, proof_kind, proof_ref,
commit_sha}]} — capped at 10 entries per capability. NULL = capability has
never been proven; absent key = no proof for that specific capability.

Revision ID: 0208
Revises: 0207
Create Date: 2026-06-09

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = "0208"
down_revision = "0207"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column("capability_proof_records", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project", "capability_proof_records")
