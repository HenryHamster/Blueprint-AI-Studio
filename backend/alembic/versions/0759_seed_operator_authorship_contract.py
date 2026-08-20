"""Arm the candidate authorship gate on already-open release versions.

Revision ID: 0759
Revises: 0758
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0759"
down_revision = "0758"
branch_labels = None
depends_on = None


_CRITERION_KEY = "c-operator-authorship"
_CRITERION_TEXT = (
    "The candidate contains no operator-authored commits across its complete release range."
)


def upgrade() -> None:
    """Declare the product default without overwriting operator policy."""
    op.execute(
        sa.text(
            """
            INSERT INTO versionacceptancecriterion (
                id,
                project_id,
                release_version_id,
                criterion_key,
                text,
                evidence_contract,
                evidence_contract_updated_at,
                evidence_contract_updated_by,
                created_by
            )
            SELECT
                gen_random_uuid(),
                rv.project_id,
                rv.id,
                :criterion_key,
                :criterion_text,
                jsonb_build_object(
                    'type', 'operator_authored_commits',
                    'predicate', jsonb_build_object('max_count', 0)
                ),
                now(),
                'system:release_default',
                'system'
            FROM releaseversion AS rv
            WHERE rv.status = 'open'
              AND NOT EXISTS (
                  SELECT 1
                  FROM versionacceptancecriterion AS vac
                  WHERE vac.release_version_id = rv.id
                    AND (
                        vac.criterion_key = :criterion_key
                        OR (
                            vac.status = 'active'
                            AND vac.evidence_contract->>'type' = 'operator_authored_commits'
                        )
                    )
              )
            """
        ).bindparams(
            criterion_key=_CRITERION_KEY,
            criterion_text=_CRITERION_TEXT,
        )
    )


def downgrade() -> None:
    """Remove only untouched rows created by this migration."""
    op.execute(
        sa.text(
            """
            DELETE FROM versionacceptancecriterion
            WHERE criterion_key = :criterion_key
              AND created_by = 'system'
              AND evidence_contract_updated_by = 'system:release_default'
            """
        ).bindparams(criterion_key=_CRITERION_KEY)
    )
