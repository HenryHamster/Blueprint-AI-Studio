"""Add immutable human-tool ArtifactRefs.

Revision ID: 0628
Revises: 0627
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0628"
down_revision = "0627"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifactref",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("author_user_id", sa.String(length=255), nullable=True),
        sa.Column(
            "content_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "retrieval_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('notes','whiteboard')", name="chk_artifactref_kind"),
        sa.CheckConstraint("revision >= 0", name="chk_artifactref_revision_nonnegative"),
        sa.CheckConstraint("length(checksum) = 64", name="chk_artifactref_checksum_length"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "tool_id", "revision", name="uq_artifactref_project_tool_revision"
        ),
    )
    op.create_index(
        "ix_artifactref_project_created", "artifactref", ["project_id", "created_at"]
    )
    op.create_index(
        "ix_artifactref_tool_revision", "artifactref", ["tool_id", "revision"]
    )
    op.add_column(
        "projectmessageattachmentupload",
        sa.Column("artifact_ref_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_messageattachment_artifact_ref",
        "projectmessageattachmentupload",
        "artifactref",
        ["artifact_ref_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_projectmessageattachmentupload_artifact_ref_id",
        "projectmessageattachmentupload",
        ["artifact_ref_id"],
    )
    op.create_table(
        "artifactrefcleanupintent",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_prefix", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending','processing','complete')",
            name="chk_artifactrefcleanupintent_state",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="chk_artifactrefcleanupintent_attempt_nonnegative",
        ),
        sa.PrimaryKeyConstraint("project_id"),
    )
    op.create_index(
        "ix_artifactrefcleanupintent_due",
        "artifactrefcleanupintent",
        ["state", "next_attempt_at"],
    )
    op.create_table(
        "artifactrefpromotionintent",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", sa.String(length=255), nullable=False),
        sa.Column("source_storage_uri", sa.Text(), nullable=False),
        sa.Column("candidate_storage_uri", sa.Text(), nullable=False),
        sa.Column("expected_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=False),
        sa.Column("render_width", sa.Integer(), nullable=True),
        sa.Column("render_height", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("effect_deadline_at", sa.DateTime(), nullable=True),
        sa.Column("settle_until", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("destination_generation", sa.BigInteger(), nullable=True),
        sa.Column("settlement_absent_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("artifact_ref_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("state IN ('pending','copying','promoted','owned','cleanup','cleanup_claimed','settled')", name="chk_artifactrefpromotionintent_state"),
        sa.CheckConstraint("revision >= 0", name="chk_artifactrefpromotionintent_revision"),
        sa.CheckConstraint("expected_size_bytes > 0", name="chk_artifactrefpromotionintent_size"),
        sa.CheckConstraint("length(expected_sha256) = 64", name="chk_artifactrefpromotionintent_checksum"),
        sa.CheckConstraint("attempt_count >= 0 AND settlement_absent_count >= 0", name="chk_artifactrefpromotionintent_counts"),
        sa.CheckConstraint(
            "state != 'owned' OR (artifact_ref_id IS NOT NULL AND completed_at IS NOT NULL AND destination_generation IS NOT NULL)",
            name="chk_artifactrefpromotionintent_owned",
        ),
        sa.CheckConstraint(
            "state NOT IN ('promoted','owned') OR destination_generation IS NOT NULL",
            name="chk_artifactrefpromotionintent_generation",
        ),
        sa.CheckConstraint(
            "state NOT IN ('copying','promoted','cleanup_claimed') OR (claim_id IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="chk_artifactrefpromotionintent_claim",
        ),
        sa.CheckConstraint(
            "state != 'copying' OR (effect_deadline_at IS NOT NULL AND settle_until IS NOT NULL AND settle_until >= effect_deadline_at)",
            name="chk_artifactrefpromotionintent_effect_window",
        ),
        sa.CheckConstraint(
            "state != 'settled' OR completed_at IS NOT NULL",
            name="chk_artifactrefpromotionintent_settled",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("receipt_id", name="uq_artifactrefpromotionintent_receipt"),
    )
    op.create_index("ix_artifactrefpromotionintent_due", "artifactrefpromotionintent", ["state", "next_attempt_at"])
    op.create_index("ix_artifactrefpromotionintent_project", "artifactrefpromotionintent", ["project_id", "state"])
    op.create_index("ix_artifactrefpromotionintent_logical_ref", "artifactrefpromotionintent", ["project_id", "tool_id", "revision"])


def downgrade() -> None:
    op.drop_index("ix_artifactrefpromotionintent_logical_ref", table_name="artifactrefpromotionintent")
    op.drop_index("ix_artifactrefpromotionintent_project", table_name="artifactrefpromotionintent")
    op.drop_index("ix_artifactrefpromotionintent_due", table_name="artifactrefpromotionintent")
    op.drop_table("artifactrefpromotionintent")
    op.drop_index(
        "ix_artifactrefcleanupintent_due",
        table_name="artifactrefcleanupintent",
    )
    op.drop_table("artifactrefcleanupintent")
    op.drop_constraint(
        "fk_messageattachment_artifact_ref",
        "projectmessageattachmentupload",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_projectmessageattachmentupload_artifact_ref_id",
        table_name="projectmessageattachmentupload",
    )
    op.drop_column("projectmessageattachmentupload", "artifact_ref_id")
    op.drop_index("ix_artifactref_tool_revision", table_name="artifactref")
    op.drop_index("ix_artifactref_project_created", table_name="artifactref")
    op.drop_table("artifactref")
