"""Add first-class normalised ``direction`` table + backfill from project.directions.

P2 scale work (design-audit roadmap ``p2-direction-table``). Historically every
direction lived inside the unbounded ``Project.directions`` JSONB blob, so every
per-direction mutation (research_history append, status flip, note add, proposal
expiry, ...) took the project-row lock and rewrote (+ TOAST-churned) the whole
array — ~112 access sites contended on one per-project mutex, and the
``_expire_stale_proposals`` sweep bulk-locked every autonomous project at once.

This migration promotes each direction to its own row keyed on
``(project_id, direction_key)`` (``direction_key`` is the string ``id`` the rest
of the system already uses, e.g. ``d-1a2b3c4d`` / ``crosscutting``). A
single-direction write becomes a single-row write — no project-row lock, no
whole-array rewrite.

Promoted columns (``status``, ``priority``, ``text``, timestamps) exist for
indexed querying; the full direction dict round-trips in the ``data`` JSONB
column so no field is lost and reads off the blob remain behaviour-identical
during the transition.

Backfill (Postgres): explode each ``project.directions`` array with
``jsonb_array_elements`` and insert one row per element that carries an ``id``,
de-duplicating on ``(project_id, id)`` (DISTINCT ON keeps the first occurrence)
so the unique constraint holds even if a blob contained an accidental dup.
Elements without an ``id`` have no natural key and are skipped (they are also
not addressable by the accessor, which keys on the string id). On SQLite (the
local test fixture) the table is created from ``SQLModel.metadata.create_all``;
the backfill is Postgres-only (JSONB functions), so the SQLite branch is a no-op
beyond the column-parity the create provides.

Revision ID: 0131
Revises: 0130
Create Date: 2026-05-31 02:00:00.000000

"""

import sqlmodel.sql.sqltypes
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "direction",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("direction_key", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
        sa.Column("priority", sa.String(length=16), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column(
            "data",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
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
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "direction_key",
            name="ux_direction_project_key",
        ),
    )
    op.create_index(
        "ix_direction_project_status",
        "direction",
        ["project_id", "status"],
        unique=False,
    )

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite test fixture builds tables via create_all and has no JSONB
        # functions; the backfill is Postgres-only.
        return

    # Backfill: one row per direction element that carries an ``id``.
    # DISTINCT ON (project_id, id) keeps the first occurrence so a blob with an
    # accidental duplicate id can't violate ux_direction_project_key.
    op.execute(
        """
        INSERT INTO direction (
            id, project_id, direction_key, status, priority, text, data,
            created_at, updated_at
        )
        SELECT DISTINCT ON (p.id, elem->>'id')
            gen_random_uuid(),
            p.id,
            LEFT(elem->>'id', 128),
            COALESCE(NULLIF(elem->>'status', ''), 'active'),
            NULLIF(elem->>'priority', ''),
            NULLIF(elem->>'text', ''),
            elem,
            NOW(),
            NOW()
        FROM project p
        CROSS JOIN LATERAL jsonb_array_elements(
            CASE
                WHEN jsonb_typeof(p.directions) = 'array' THEN p.directions
                ELSE '[]'::jsonb
            END
        ) AS elem
        WHERE elem->>'id' IS NOT NULL AND elem->>'id' <> ''
        ORDER BY p.id, elem->>'id'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_direction_project_status", table_name="direction")
    op.drop_table("direction")
