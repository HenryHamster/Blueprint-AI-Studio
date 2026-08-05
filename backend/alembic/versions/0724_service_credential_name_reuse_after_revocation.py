"""Allow service-credential name reuse after revocation.

Revision ID: 0724
Revises: 0723
Create Date: 2026-08-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0724"
down_revision = "0723"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The full UNIQUE on name (migration 0723) blocked re-minting a revoked
    # name even though the mint pre-check intentionally allows it. Replace it
    # with a partial unique index so at most one ACTIVE (non-revoked)
    # credential holds a given name, while revoked names may be reused.
    op.drop_constraint("uq_servicecredential_name", "servicecredential", type_="unique")
    op.create_index(
        "uq_servicecredential_name_unrevoked",
        "servicecredential",
        ["name"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        sqlite_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_servicecredential_name_unrevoked", table_name="servicecredential")
    op.create_unique_constraint("uq_servicecredential_name", "servicecredential", ["name"])
