"""Persist qualification-attempt identity and dimension reuse provenance.

Revision ID: 0607
Revises: 0566
Create Date: 2026-07-15 21:34:10.811084
Nullable columns keep mixed-version rollouts compatible. Backfill links matching
projection/verdict/dimension rows and assigns distinct UUIDs when provenance
cannot safely be reconstructed.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql
revision = "0607"
down_revision = "0566"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Auto-generated column/index DDL.  The generator also proposed rebuilding
    # two unrelated DESC expression indexes because SQLite introspection loses
    # expression ordering; those false-positive operations were removed.
    guid = sqlmodel.sql.sqltypes.GUID()
    op.add_column("qualificationdimensionresult", sa.Column("origin_attempt_id", guid, nullable=True))
    op.add_column("qualificationdimensionresult", sa.Column("reused_by_attempt_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_index(
        op.f("ix_qualificationdimensionresult_origin_attempt_id"),
        "qualificationdimensionresult",
        ["origin_attempt_id"],
    )
    op.add_column("qualificationverdict", sa.Column("qualification_attempt_id", sqlmodel.sql.sqltypes.GUID(), nullable=True))
    op.create_index(
        op.f("ix_qualificationverdict_qualification_attempt_id"),
        "qualificationverdict",
        ["qualification_attempt_id"],
    )
    op.add_column("releaseversion", sa.Column("active_qualification_attempt_id", sqlmodel.sql.sqltypes.GUID(), nullable=True))
    op.add_column(
        "releaseversion",
        sa.Column("qualification_retry_episode", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_index(
        op.f("ix_releaseversion_active_qualification_attempt_id"),
        "releaseversion",
        ["active_qualification_attempt_id"],
    )

    bind = op.get_bind()
    # Legacy pending children have no attempt key; their JSON is stamped at finalization.
    if bind.dialect.name == "postgresql":
        op.execute(
            "UPDATE qualificationverdict "
            "SET qualification_attempt_id = gen_random_uuid() "
            "WHERE qualification_attempt_id IS NULL"
        )
        op.execute(
            """
            UPDATE releaseversion AS rv
            SET active_qualification_attempt_id = (
                SELECT q.qualification_attempt_id
                FROM qualificationverdict AS q
                WHERE q.release_version_id = rv.id
                  AND q.candidate_sha IS NOT DISTINCT FROM
                      (rv.last_qualification ->> 'candidate_sha')
                  AND q.terminal_status = (rv.last_qualification ->> 'status')
                ORDER BY q.finalized_at DESC NULLS LAST, q.created_at DESC, q.id DESC
                LIMIT 1
            )
            WHERE rv.last_qualification IS NOT NULL
              AND rv.active_qualification_attempt_id IS NULL
            """
        )
        op.execute(
            "UPDATE releaseversion SET active_qualification_attempt_id = gen_random_uuid() "
            "WHERE active_qualification_attempt_id IS NULL "
            "AND (last_qualification IS NOT NULL OR qualification_attempts > 0)"
        )
        op.execute(
            """
            UPDATE releaseversion
            SET last_qualification = jsonb_set(
                last_qualification,
                '{qualification_attempt_id}',
                to_jsonb(active_qualification_attempt_id::text),
                true
            )
            WHERE last_qualification IS NOT NULL
              AND active_qualification_attempt_id IS NOT NULL
              AND NOT (last_qualification ? 'qualification_attempt_id')
              AND (last_qualification ->> 'status') IS DISTINCT FROM 'pending'
            """
        )
        op.execute(
            """
            UPDATE qualificationdimensionresult AS d
            SET origin_attempt_id = (
                SELECT q.qualification_attempt_id
                FROM qualificationverdict AS q
                WHERE q.release_version_id = d.release_version_id
                  AND q.candidate_sha IS NOT DISTINCT FROM d.candidate_sha
                ORDER BY q.finalized_at DESC NULLS LAST, q.created_at DESC, q.id DESC
                LIMIT 1
            )
            WHERE d.origin_attempt_id IS NULL
            """
        )
        op.execute(
            "UPDATE qualificationdimensionresult "
            "SET origin_attempt_id = gen_random_uuid() "
            "WHERE origin_attempt_id IS NULL"
        )
    else:
        # SQLite migration tests; production uses PostgreSQL above.
        uuid_sql = (
            "lower(hex(randomblob(4)) || '-' || hex(randomblob(2)) || '-4' || "
            "substr(hex(randomblob(2)),2) || '-' || "
            "substr('89ab',abs(random()) % 4 + 1,1) || "
            "substr(hex(randomblob(2)),2) || '-' || hex(randomblob(6)))"
        )
        op.execute(
            f"UPDATE qualificationverdict SET qualification_attempt_id = {uuid_sql} "
            "WHERE qualification_attempt_id IS NULL"
        )
        op.execute(
            """
            UPDATE releaseversion
            SET active_qualification_attempt_id = (
                SELECT q.qualification_attempt_id
                FROM qualificationverdict AS q
                WHERE q.release_version_id = releaseversion.id
                  AND q.candidate_sha IS json_extract(
                      releaseversion.last_qualification, '$.candidate_sha'
                  )
                  AND q.terminal_status = json_extract(
                      releaseversion.last_qualification, '$.status'
                  )
                ORDER BY (q.finalized_at IS NULL), q.finalized_at DESC,
                         q.created_at DESC, q.id DESC
                LIMIT 1
            )
            WHERE last_qualification IS NOT NULL
              AND active_qualification_attempt_id IS NULL
            """
        )
        op.execute(
            f"UPDATE releaseversion SET active_qualification_attempt_id = {uuid_sql} "
            "WHERE active_qualification_attempt_id IS NULL "
            "AND (last_qualification IS NOT NULL OR qualification_attempts > 0)"
        )
        op.execute(
            """
            UPDATE releaseversion
            SET last_qualification = json_set(
                last_qualification,
                '$.qualification_attempt_id',
                active_qualification_attempt_id
            )
            WHERE last_qualification IS NOT NULL
              AND active_qualification_attempt_id IS NOT NULL
              AND json_extract(
                  last_qualification, '$.qualification_attempt_id'
              ) IS NULL
              AND json_extract(last_qualification, '$.status') IS NOT 'pending'
            """
        )
        op.execute(
            """
            UPDATE qualificationdimensionresult
            SET origin_attempt_id = (
                SELECT q.qualification_attempt_id
                FROM qualificationverdict AS q
                WHERE q.release_version_id = qualificationdimensionresult.release_version_id
                  AND q.candidate_sha IS qualificationdimensionresult.candidate_sha
                ORDER BY (q.finalized_at IS NULL), q.finalized_at DESC,
                         q.created_at DESC, q.id DESC
                LIMIT 1
            )
            WHERE origin_attempt_id IS NULL
            """
        )
        op.execute(
            f"UPDATE qualificationdimensionresult SET origin_attempt_id = {uuid_sql} "
            "WHERE origin_attempt_id IS NULL"
        )


def downgrade() -> None:
    # Only additive provenance is discarded; legacy scoring data remains.
    op.drop_index(op.f("ix_releaseversion_active_qualification_attempt_id"), table_name="releaseversion")
    op.drop_column("releaseversion", "qualification_retry_episode")
    op.drop_column("releaseversion", "active_qualification_attempt_id")
    op.drop_index(op.f("ix_qualificationverdict_qualification_attempt_id"), table_name="qualificationverdict")
    op.drop_column("qualificationverdict", "qualification_attempt_id")
    op.drop_index(op.f("ix_qualificationdimensionresult_origin_attempt_id"), table_name="qualificationdimensionresult")
    op.drop_column("qualificationdimensionresult", "reused_by_attempt_ids")
    op.drop_column("qualificationdimensionresult", "origin_attempt_id")
