"""Add durable Strategic requests/watches and compute execution provenance.

Revision ID: 0708
Revises: 0707
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0708"
down_revision = "0707"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategicspawnrequest",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column(
            "specification",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("specification_hash", sa.String(length=64), nullable=False),
        sa.Column("direction_id", sa.String(length=128), nullable=True),
        sa.Column(
            "state",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'accepted'"),
        ),
        sa.Column("admission_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "policy_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(
            ["parent_task_id"], ["task.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "parent_task_id",
            "idempotency_key",
            name="uq_strategicspawnrequest_parent_idempotency",
        ),
    )
    op.create_index(
        "ix_strategicspawnrequest_project_id",
        "strategicspawnrequest",
        ["project_id"],
    )
    op.create_index(
        "ix_strategicspawnrequest_parent_task_id",
        "strategicspawnrequest",
        ["parent_task_id"],
    )
    op.create_index(
        "ix_strategicspawnrequest_task_id",
        "strategicspawnrequest",
        ["task_id"],
    )
    op.create_index(
        "ix_strategicspawnrequest_state_notbefore_created",
        "strategicspawnrequest",
        ["state", "not_before", "created_at"],
    )
    op.create_index(
        "ix_strategicspawnrequest_project_state",
        "strategicspawnrequest",
        ["project_id", "state"],
    )

    op.create_table(
        "strategicwatch",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("strategic_session_id", sa.String(length=128), nullable=True),
        sa.Column("target_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "purpose",
            sa.String(length=1000),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "state",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("terminal_status", sa.String(length=32), nullable=True),
        sa.Column(
            "receipt",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "acknowledgement_disposition", sa.String(length=32), nullable=True
        ),
        sa.Column("acknowledgement_note", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(
            ["created_by_task_id"], ["task.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["target_task_id"], ["task.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "created_by_task_id",
            "target_task_id",
            name="uq_strategicwatch_creator_target",
        ),
    )
    op.create_index("ix_strategicwatch_project_id", "strategicwatch", ["project_id"])
    op.create_index(
        "ix_strategicwatch_created_by_task_id",
        "strategicwatch",
        ["created_by_task_id"],
    )
    op.create_index(
        "ix_strategicwatch_target_task_id",
        "strategicwatch",
        ["target_task_id"],
    )
    op.create_index(
        "ix_strategicwatch_project_state",
        "strategicwatch",
        ["project_id", "state"],
    )
    op.create_index(
        "ix_strategicwatch_state_deadline",
        "strategicwatch",
        ["state", "deadline_at"],
    )

    op.add_column(
        "computejob",
        sa.Column(
            "execution_manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "computejob",
        sa.Column(
            "runtime_provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "knowledgedocument",
        sa.Column(
            "source_compute_job_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_knowledgedocument_source_compute_job_id",
        "knowledgedocument",
        "computejob",
        ["source_compute_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_knowledgedocument_source_compute_job_id",
        "knowledgedocument",
        ["source_compute_job_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledgedocument_source_compute_job_id",
        table_name="knowledgedocument",
    )
    op.drop_constraint(
        "fk_knowledgedocument_source_compute_job_id",
        "knowledgedocument",
        type_="foreignkey",
    )
    op.drop_column("knowledgedocument", "source_compute_job_id")
    op.drop_column("computejob", "runtime_provenance")
    op.drop_column("computejob", "execution_manifest")

    op.drop_index("ix_strategicwatch_state_deadline", table_name="strategicwatch")
    op.drop_index("ix_strategicwatch_project_state", table_name="strategicwatch")
    op.drop_index("ix_strategicwatch_target_task_id", table_name="strategicwatch")
    op.drop_index("ix_strategicwatch_created_by_task_id", table_name="strategicwatch")
    op.drop_index("ix_strategicwatch_project_id", table_name="strategicwatch")
    op.drop_table("strategicwatch")

    op.drop_index(
        "ix_strategicspawnrequest_project_state",
        table_name="strategicspawnrequest",
    )
    op.drop_index(
        "ix_strategicspawnrequest_state_notbefore_created",
        table_name="strategicspawnrequest",
    )
    op.drop_index(
        "ix_strategicspawnrequest_task_id",
        table_name="strategicspawnrequest",
    )
    op.drop_index(
        "ix_strategicspawnrequest_parent_task_id",
        table_name="strategicspawnrequest",
    )
    op.drop_index(
        "ix_strategicspawnrequest_project_id",
        table_name="strategicspawnrequest",
    )
    op.drop_table("strategicspawnrequest")
