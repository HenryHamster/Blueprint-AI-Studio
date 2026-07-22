"""Ensure persistent Workday conversation identity on project messages.

Revision ID: 0671
Revises: 0670
"""

from alembic import op
import sqlalchemy as sa


revision = "0671"
down_revision = "0670"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent on two axes:
    # (a) Production DBs (v25 → v26 in one `alembic upgrade head`): 0657 (fixed)
    #     no longer drops this column/index, so they already exist when 0671 runs.
    #     The column guard and if_not_exists make both operations no-ops.
    # (b) CI environments that ran the original 0657 (which did drop the column):
    #     the column guard adds it; if_not_exists handles the index.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("projectmessage")}
    if "resident_session_id" not in existing_columns:
        op.add_column(
            "projectmessage",
            sa.Column("resident_session_id", sa.String(length=255), nullable=True),
        )
    op.create_index(
        "ix_projectmessage_project_session_created",
        "projectmessage",
        ["project_id", "resident_session_id", "created_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    # The canonical amended chain preserves this revision-0396 column and index
    # through 0657, so 0671 normally creates nothing. There is no durable marker
    # proving that this revision created either object on a partially upgraded
    # nonproduction database; removing them would destroy valid Workday history.
    pass
