"""Backfill all legacy co-change state without overriding authority.

Revision ID: 0698
Revises: 0697
Create Date: 2026-07-23

Migration 0695 backfilled ``cochange_state`` only when both legacy design-doc
keys existed. Preserve projects that contain only one design-doc side or an
older dedicated ``cochange_index`` / ``cochange_applied_prs`` telemetry row
before ``with_design_doc_locked`` strips the migrated design-doc keys.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0698"
down_revision = "0697"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO project_telemetry (id, project_id, key, value, updated_at)
            SELECT
              gen_random_uuid(),
              p.id,
              'cochange_state',
              jsonb_build_object(
                'index', COALESCE(
                  legacy_index.value,
                  p.design_doc -> 'cochange_index',
                  '{}'::jsonb
                ),
                'applied_prs', COALESCE(
                  legacy_applied.value,
                  p.design_doc -> 'cochange_applied_prs',
                  '[]'::jsonb
                )
              ),
              CURRENT_TIMESTAMP
            FROM project p
            LEFT JOIN project_telemetry legacy_index
              ON legacy_index.project_id = p.id
              AND legacy_index.key = 'cochange_index'
            LEFT JOIN project_telemetry legacy_applied
              ON legacy_applied.project_id = p.id
              AND legacy_applied.key = 'cochange_applied_prs'
            WHERE (
              p.design_doc ? 'cochange_index'
              OR p.design_doc ? 'cochange_applied_prs'
              OR legacy_index.id IS NOT NULL
              OR legacy_applied.id IS NOT NULL
            )
            ON CONFLICT ON CONSTRAINT uq_project_telemetry_project_key
            DO NOTHING
            """
        )
    )


def downgrade() -> None:
    # Forward-only data copy. Rows cannot be distinguished from newer live
    # cochange_state writes, so rollback must not delete them.
    pass
