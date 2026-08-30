"""Persist append-only project qualification-start control receipts.

Revision ID: 0769
Revises: 0768
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa


revision = "0769"
down_revision = "0768"
branch_labels = None
depends_on = None

_TABLE = "releasequalificationcontrolrevision"


def _create_append_only_guard() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION release_qualification_control_append_only()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' AND NOT EXISTS (
                    SELECT 1 FROM project WHERE id = OLD.project_id
                ) THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'Release qualification control rows are append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"CREATE TRIGGER trg_{_TABLE}_append_only "
            f"BEFORE UPDATE OR DELETE ON {_TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION release_qualification_control_append_only()"
        )
    elif bind.dialect.name == "sqlite":
        op.execute(
            f"CREATE TRIGGER trg_{_TABLE}_no_update BEFORE UPDATE ON {_TABLE} "
            "BEGIN SELECT RAISE(ABORT, "
            "'Release qualification control rows are append-only'); END"
        )
        op.execute(
            f"CREATE TRIGGER trg_{_TABLE}_no_delete BEFORE DELETE ON {_TABLE} "
            "WHEN EXISTS (SELECT 1 FROM project WHERE id = OLD.project_id) "
            "BEGIN SELECT RAISE(ABORT, "
            "'Release qualification control rows are append-only'); END"
        )


def _drop_append_only_guard() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_append_only ON {_TABLE}")
        op.execute(
            "DROP FUNCTION IF EXISTS release_qualification_control_append_only()"
        )
    elif bind.dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_no_update")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_no_delete")


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "automatic_starts_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("specification_hash", sa.String(length=64), nullable=False),
        sa.Column("actor_user_id", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_releasequalificationcontrol_revision",
        ),
        sa.CheckConstraint(
            "length(specification_hash) = 64",
            name="ck_releasequalificationcontrol_specification_hash",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "revision",
            name="uq_releasequalificationcontrol_project_revision",
        ),
        sa.UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_releasequalificationcontrol_project_key",
        ),
    )
    op.create_index(
        "ix_releasequalificationcontrol_project_latest",
        _TABLE,
        ["project_id", "revision"],
        unique=False,
    )
    _create_append_only_guard()


def downgrade() -> None:
    _drop_append_only_guard()
    op.drop_index(
        "ix_releasequalificationcontrol_project_latest",
        table_name=_TABLE,
    )
    op.drop_table(_TABLE)
