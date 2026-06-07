"""migration_reservation_table

Revision ID: 0155
Revises: 0154
Create Date: 2026-06-06 03:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision = "0155"
down_revision = "0154"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "migrationreservation",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("task_id", sqlmodel.sql.sqltypes.GUID(), nullable=True),
        sa.Column("provisional_revision", sa.String(length=64), nullable=False),
        sa.Column(
            "provisional_down_revision", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "status", sa.String(), server_default="ACTIVE", nullable=False
        ),
        sa.Column("finalized_revision", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # task_id detaches (SET NULL) on task delete: a reservation is durable
        # allocation history that must outlive its originating task so a merge-
        # time finalize/expiry can still reconcile the provisional id.
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_migrationreservation_task_id"),
        "migrationreservation",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_migrationreservation_provisional_revision"),
        "migrationreservation",
        ["provisional_revision"],
        unique=False,
    )
    op.create_index(
        "ix_migrationreservation_project_status",
        "migrationreservation",
        ["project_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_migrationreservation_project_status",
        table_name="migrationreservation",
    )
    op.drop_index(
        op.f("ix_migrationreservation_provisional_revision"),
        table_name="migrationreservation",
    )
    op.drop_index(
        op.f("ix_migrationreservation_task_id"),
        table_name="migrationreservation",
    )
    op.drop_table("migrationreservation")
