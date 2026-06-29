"""criterionevidence_unique

Adds a partial unique index on ``criterionevidence(criterion_id, source_kind,
source_id) WHERE source_id IS NOT NULL`` to prevent duplicate task-merge
evidence rows under concurrent ``record_task_merge_evidence`` calls.

Rows with ``source_id IS NULL`` (operator attestations) are excluded from the
constraint so multiple null-source attestations can coexist for the same
criterion.

Revision ID: 0365
Revises: 0354
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0365"
down_revision = "0354"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_criterionevidence_dedup",
        "criterionevidence",
        ["criterion_id", "source_kind", "source_id"],
        unique=True,
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_criterionevidence_dedup", table_name="criterionevidence")
