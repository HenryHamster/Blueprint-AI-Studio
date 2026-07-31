"""Add Strategic Memory V2 source receipts and control-plane state.

Revision ID: 0716
Revises: 0715
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0716"
down_revision = "0715"
branch_labels = None
depends_on = None


_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    # Batch mode keeps the additive state-table alteration executable under the
    # SQLite migration harness while producing ordinary ADD COLUMN DDL on
    # PostgreSQL. Existing 0715 rows satisfy every non-null default.
    with op.batch_alter_table("strategicmemoryprojectstate") as batch_op:
        batch_op.add_column(
            sa.Column("source_high_watermark", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("incorporated_source_sequence", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("backfill_status", sa.String(length=16), nullable=False, server_default="pending")
        )
        batch_op.add_column(
            sa.Column("extraction_status", sa.String(length=16), nullable=False, server_default="healthy")
        )
        batch_op.add_column(sa.Column("last_error", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"))
        batch_op.create_check_constraint("ck_smem_state_source_high", "source_high_watermark >= 0")
        batch_op.create_check_constraint(
            "ck_smem_state_source_incorporated", "incorporated_source_sequence >= 0"
        )
        batch_op.create_check_constraint(
            "ck_smem_state_source_watermark",
            "incorporated_source_sequence <= source_high_watermark",
        )
        batch_op.create_check_constraint("ck_smem_state_attempt", "attempt >= 0")
        batch_op.create_check_constraint(
            "ck_smem_state_backfill_status",
            "backfill_status IN ('pending','running','complete','failed')",
        )
        batch_op.create_check_constraint(
            "ck_smem_state_extraction_status",
            "extraction_status IN ('healthy','lagging','quarantined','failed')",
        )

    op.create_table(
        "strategicmemorysource",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_sequence", sa.BigInteger(), nullable=False),
        sa.Column("native_sequence", sa.BigInteger(), nullable=True),
        sa.Column("ingestion_origin", sa.String(length=16), nullable=False, server_default="live"),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_key", sa.String(length=512), nullable=False),
        sa.Column("source_revision", sa.String(length=255), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("actor_kind", sa.String(length=64), nullable=True),
        sa.Column("actor_id", sa.String(length=255), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("resident_session_id", sa.String(length=255), nullable=True),
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default="project"),
        sa.Column("sensitivity", sa.String(length=32), nullable=False, server_default="normal"),
        sa.Column("payload_state", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("tombstoned_at", sa.DateTime(), nullable=True),
        sa.Column("metadata", _JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("canonical_payload", sa.LargeBinary(), nullable=True),
        sa.CheckConstraint("source_sequence >= 1", name="ck_smem_source_sequence"),
        sa.CheckConstraint(
            "native_sequence IS NULL OR native_sequence >= 0",
            name="ck_smem_source_native_sequence",
        ),
        sa.CheckConstraint("ingestion_origin IN ('live','backfill')", name="ck_smem_source_origin"),
        sa.CheckConstraint(
            "source_type IN ('project_message','artifact_revision','task_result',"
            "'problem_model_revision','decision','operator_action',"
            "'legacy_knowledge_document_revision')",
            name="ck_smem_source_type",
        ),
        sa.CheckConstraint(
            "payload_state IN ('active','redacted','deleted')",
            name="ck_smem_source_payload_state",
        ),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_smem_source_hash_len"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "source_sequence", name="uq_smem_source_project_sequence"),
        sa.UniqueConstraint(
            "project_id", "source_type", "source_key", "source_revision", name="uq_smem_source_receipt"
        ),
    )
    op.create_index("ix_smem_source_project_sequence", "strategicmemorysource", ["project_id", "source_sequence"])
    op.create_index("ix_smem_source_project_type_key", "strategicmemorysource", ["project_id", "source_type", "source_key"])
    op.create_index("ix_smem_source_project_hash", "strategicmemorysource", ["project_id", "content_hash"])
    op.create_index("ix_smem_source_project_occurred", "strategicmemorysource", ["project_id", "occurred_at"])

    op.create_table(
        "strategicmemorybackfillcursor",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("producer", sa.String(length=128), nullable=False),
        sa.Column("cutoff_spec", _JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("cursor", _JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("scanned_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("admitted_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("scanned_count >= 0", name="ck_smem_backfill_scanned"),
        sa.CheckConstraint("admitted_count >= 0", name="ck_smem_backfill_admitted"),
        sa.CheckConstraint("skipped_count >= 0", name="ck_smem_backfill_skipped"),
        sa.CheckConstraint("admitted_count + skipped_count <= scanned_count", name="ck_smem_backfill_counts"),
        sa.CheckConstraint(
            "status IN ('pending','running','complete','failed')", name="ck_smem_backfill_status"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "producer", name="uq_smem_backfill_producer"),
    )
    op.create_index("ix_smem_backfill_project_status", "strategicmemorybackfillcursor", ["project_id", "status"])

    op.create_table(
        "strategicmemoryconsolidationjob",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_start", sa.BigInteger(), nullable=True),
        sa.Column("source_end", sa.BigInteger(), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=False, server_default="contiguous"),
        sa.Column("target_source_id", sa.Uuid(), nullable=True),
        sa.Column("base_ledger_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("context_authority_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("privacy_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("prompt_version", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("mode IN ('contiguous','reconsolidation')", name="ck_smem_job_mode"),
        sa.CheckConstraint(
            "status IN ('queued','running','retry','complete','dead')", name="ck_smem_job_status"
        ),
        sa.CheckConstraint("base_ledger_revision >= 0", name="ck_smem_job_base_ledger"),
        sa.CheckConstraint(
            "context_authority_generation >= 0", name="ck_smem_job_context_gen"
        ),
        sa.CheckConstraint("privacy_generation >= 0", name="ck_smem_job_privacy_gen"),
        sa.CheckConstraint("lease_generation >= 0", name="ck_smem_job_lease_gen"),
        sa.CheckConstraint("attempt >= 0", name="ck_smem_job_attempt"),
        sa.CheckConstraint(
            "(source_start IS NULL AND source_end IS NULL) OR "
            "(source_start >= 1 AND source_end >= source_start)",
            name="ck_smem_job_source_range",
        ),
        sa.CheckConstraint(
            "(mode = 'contiguous' AND source_start IS NOT NULL AND source_end IS NOT NULL "
            "AND target_source_id IS NULL) OR "
            "(mode = 'reconsolidation' AND target_source_id IS NOT NULL)",
            name="ck_smem_job_mode_target",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_source_id"], ["strategicmemorysource.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_smem_job_project_status", "strategicmemoryconsolidationjob", ["project_id", "status"])
    op.create_index("ix_smem_job_project_range", "strategicmemoryconsolidationjob", ["project_id", "source_start", "source_end"])
    op.create_index(
        "uq_smem_job_active_project",
        "strategicmemoryconsolidationjob",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running','retry')"),
        sqlite_where=sa.text("status IN ('queued','running','retry')"),
    )

    op.create_table(
        "strategicmemorysourcedisposition",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("classifier_version", sa.String(length=128), nullable=True),
        sa.Column("extractor_model", sa.String(length=255), nullable=True),
        sa.Column("prompt_version", sa.String(length=128), nullable=True),
        sa.Column("consolidation_job_id", sa.Uuid(), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','incorporated','no_memory','quarantined')",
            name="ck_smem_disposition_status",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["strategicmemorysource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["consolidation_job_id"], ["strategicmemoryconsolidationjob.id"]),
        sa.PrimaryKeyConstraint("source_id"),
    )
    op.create_index("ix_smem_disposition_job", "strategicmemorysourcedisposition", ["consolidation_job_id"])
    op.create_index("ix_smem_disposition_status", "strategicmemorysourcedisposition", ["status"])


def downgrade() -> None:
    op.drop_index("ix_smem_disposition_status", table_name="strategicmemorysourcedisposition")
    op.drop_index("ix_smem_disposition_job", table_name="strategicmemorysourcedisposition")
    op.drop_table("strategicmemorysourcedisposition")
    op.drop_index("uq_smem_job_active_project", table_name="strategicmemoryconsolidationjob")
    op.drop_index("ix_smem_job_project_range", table_name="strategicmemoryconsolidationjob")
    op.drop_index("ix_smem_job_project_status", table_name="strategicmemoryconsolidationjob")
    op.drop_table("strategicmemoryconsolidationjob")
    op.drop_index("ix_smem_backfill_project_status", table_name="strategicmemorybackfillcursor")
    op.drop_table("strategicmemorybackfillcursor")
    op.drop_index("ix_smem_source_project_occurred", table_name="strategicmemorysource")
    op.drop_index("ix_smem_source_project_hash", table_name="strategicmemorysource")
    op.drop_index("ix_smem_source_project_type_key", table_name="strategicmemorysource")
    op.drop_index("ix_smem_source_project_sequence", table_name="strategicmemorysource")
    op.drop_table("strategicmemorysource")

    with op.batch_alter_table("strategicmemoryprojectstate") as batch_op:
        batch_op.drop_constraint("ck_smem_state_extraction_status", type_="check")
        batch_op.drop_constraint("ck_smem_state_backfill_status", type_="check")
        batch_op.drop_constraint("ck_smem_state_attempt", type_="check")
        batch_op.drop_constraint("ck_smem_state_source_watermark", type_="check")
        batch_op.drop_constraint("ck_smem_state_source_incorporated", type_="check")
        batch_op.drop_constraint("ck_smem_state_source_high", type_="check")
        batch_op.drop_column("attempt")
        batch_op.drop_column("last_error")
        batch_op.drop_column("extraction_status")
        batch_op.drop_column("backfill_status")
        batch_op.drop_column("incorporated_source_sequence")
        batch_op.drop_column("source_high_watermark")
