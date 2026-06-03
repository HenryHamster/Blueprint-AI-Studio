"""Add the warm-VM pool registry table (``poolvm``).

Foundation for the pre-warmed VM pool (efficiency direction d-5fd69fe1). One row
per pre-provisioned, credentialed, idle agent VM, leasable to a newly-queued task
via ``SELECT ... FOR UPDATE SKIP LOCKED`` instead of cold-provisioning. See
``app.models.PoolVm`` / ``app.warm_pool`` for the model + lease helpers.

NOTE: although the parent feature decomposed migration generation into its own
sibling task, the table has to exist for the schema to round-trip — adding the
model to ``models.py`` without the matching migration breaks the Postgres test
harness (the shared conftest teardown ``DELETE``s from every metadata table, and
``alembic upgrade head`` builds the CI schema). Per CLAUDE.md this migration is
chained from the latest head (0132) and ``EXPECTED_SCHEMA_REVISION`` is bumped to
match. On SQLite (local test fixture) the table is created from
``SQLModel.metadata.create_all``; this migration is the Postgres path.

Revision ID: 0133
Revises: 0132
Create Date: 2026-06-01 09:00:00.000000

"""

import sqlmodel.sql.sqltypes
from alembic import op
import sqlalchemy as sa


revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "poolvm",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("vm_instance_id", sa.String(), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("auth_mode", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("agent_image_fingerprint", sa.String(), nullable=True),
        sa.Column(
            "state",
            sa.String(),
            nullable=False,
            server_default="PROVISIONING",
        ),
        sa.Column("idle_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("leased_task_id", sqlmodel.sql.sqltypes.GUID(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idle_cost_banked", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Unique natural key — one registry row per VM instance.
    op.create_index(
        "ix_poolvm_vm_instance_id",
        "poolvm",
        ["vm_instance_id"],
        unique=True,
    )
    # Single-column project index (mirrors the model's foreign_key index=True).
    op.create_index(
        "ix_poolvm_project_id",
        "poolvm",
        ["project_id"],
        unique=False,
    )
    # Lease hot path: WHERE project_id = ? AND state = 'IDLE' AND <identity>.
    op.create_index(
        "ix_poolvm_project_state",
        "poolvm",
        ["project_id", "state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_poolvm_project_state", table_name="poolvm")
    op.drop_index("ix_poolvm_project_id", table_name="poolvm")
    op.drop_index("ix_poolvm_vm_instance_id", table_name="poolvm")
    op.drop_table("poolvm")
