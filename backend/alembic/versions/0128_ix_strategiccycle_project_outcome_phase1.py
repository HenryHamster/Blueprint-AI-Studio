"""Add composite index on strategiccycle(project_id, outcome, phase1_started_at).

Covers the SR-trend hot query run during claim_task enrichment:
``WHERE project_id=? AND outcome='completed' ORDER BY phase1_started_at DESC
LIMIT 4`` (compute_fresh_reviewer_trend / compute_edges_trend /
compute_sr_*_signal_trends / _refresh_sr_catch_rate). Without it the query
full-scanned + sorted every completed cycle for the project and hit the 120s
statement_timeout, aborting the claim session and 500-ing task claims
(incident 2026-05-29).

Built CONCURRENTLY on PostgreSQL (matching the Task index migrations, e.g.
0113/0115/0116) so the index build never blocks StrategicCycle writes and is
not subject to a statement timeout on a large table. The SQLite branch uses a
plain op.create_index with literal args — also what the model↔migration
index-parity invariant (test_invariants.TestSchemaRevisionSync) scans for.

Revision ID: 0128
Revises: 0127
Create Date: 2026-05-29 05:00:00.000000

"""

from alembic import op


revision = "0128"
down_revision = "0127"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite fallback (also the form the index-parity invariant detects).
        op.create_index(
            "ix_strategiccycle_project_outcome_phase1",
            "strategiccycle",
            ["project_id", "outcome", "phase1_started_at"],
        )
        return
    # CONCURRENTLY cannot run inside a transaction; commit first.
    op.execute("COMMIT")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS
            ix_strategiccycle_project_outcome_phase1
        ON strategiccycle (project_id, outcome, phase1_started_at)
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        op.drop_index(
            "ix_strategiccycle_project_outcome_phase1",
            table_name="strategiccycle",
        )
        return
    op.execute("COMMIT")
    op.execute(
        "DROP INDEX CONCURRENTLY IF EXISTS ix_strategiccycle_project_outcome_phase1"
    )
