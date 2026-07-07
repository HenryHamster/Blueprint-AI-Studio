"""dedup_archived_gaps

One-time data repair: de-duplicate archived_gaps entries in the
problem_model JSONB column.  The archive_closed_gaps helper previously
appended a closed/abandoned gap unconditionally on every apply_agent_update
call, so a stale strategist payload that re-submitted an already-archived
gap would create byte-identical duplicate rows.  This migration removes all
duplicates, keeping the first occurrence (array order preserved).

Revision ID: 0375
Revises: 0374
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "0375"
down_revision = "0374"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # For every project row whose archived_gaps array contains duplicate gap
    # ids, rebuild the array keeping only the first occurrence of each id
    # (preserving insertion order).
    #
    # Inner subquery assigns a stable row number to each element; second
    # subquery computes the minimum rn per gap id via a window function;
    # outer aggregation reassembles the deduplicated list in original order.
    op.execute(
        """
        UPDATE project
        SET problem_model = jsonb_set(
            problem_model,
            '{archived_gaps}',
            (
                SELECT jsonb_agg(elem ORDER BY rn)
                FROM (
                    SELECT elem, rn, MIN(rn) OVER (PARTITION BY elem->>'id') AS first_rn
                    FROM (
                        SELECT elem, ROW_NUMBER() OVER () AS rn
                        FROM jsonb_array_elements(problem_model->'archived_gaps') AS elem
                    ) numbered
                ) ranked
                WHERE rn = first_rn
            )
        )
        WHERE
            problem_model ? 'archived_gaps'
            AND jsonb_typeof(problem_model->'archived_gaps') = 'array'
            AND (
                SELECT COUNT(DISTINCT gap_id) < COUNT(*)
                FROM (
                    SELECT elem->>'id' AS gap_id
                    FROM jsonb_array_elements(problem_model->'archived_gaps') AS elem
                ) all_rows
            )
        """
    )


def downgrade() -> None:
    pass  # One-way data fix; duplicates cannot be safely restored.
