"""Add the cross-instance shared digest/health cache table (``digestcache``).

The leader-only digest precompute worker (``digest_precompute_service``) caches
the heavy 5000-row ``_compute_digest`` / ``_compute_health_overview`` payloads in
an in-process dict (``DigestPrecomputeCache``). That helps ONLY the leader: the
other Cloud Run instances serving dashboard traffic have a cold in-memory cache
and every request live-computes → recurring ``statement_timeout`` 500s (prod
2026-06-03: ~58 digest/health timeouts/24h), and every cache resets on each
deploy (~25 min cadence).

This table is a shared L2 cache on the EXISTING Cloud SQL instance (no new infra
cost). The leader writes each precomputed payload here; every instance reads it
with a single primary-key SELECT when its in-memory L1 misses (non-leader, or
post-deploy cold start), so the expensive scan runs at most once per tick on the
leader instead of once per request per instance.

``cache_key`` encodes ``"{kind}:{window}:{project_id|global}"`` (kind ∈
{digest, health}). ``payload`` is the byte-identical compute output; the read
path serves it only while ``computed_at`` is within
``PRECOMPUTE_STALE_FALLBACK_SECONDS`` and otherwise degrades to the existing
on-demand ``DigestCache`` fallback. The table is tiny (bounded by the hot-key
set: 1 global + <=25 active projects per kind) and churned by UPSERT; stale rows
for no-longer-hot keys are pruned by the worker tick.

Per CLAUDE.md this migration is chained from the latest head and
``EXPECTED_SCHEMA_REVISION`` is bumped to match. It was originally authored on
``main`` as revision 0138; the ``main -> automain`` back-merge renumbered it onto
automain's tail (0138 ``task_blocked_state_fields`` -> 0139
``project_merge_gate_runtime``) to keep a single, monotonic head, so it now
chains from 0139 as 0140. On SQLite (local test fixture) the table is created
from ``SQLModel.metadata.create_all``; this migration is the Postgres path.

Revision ID: 0140
Revises: 0139
Create Date: 2026-06-03 09:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0140"
down_revision = "0139"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent guard. This migration was originally authored as revision 0138
    # on ``main`` (where it CREATEd ``digestcache``); the ``main -> automain``
    # back-merge renumbered it to 0140, so on prod the table already exists while
    # ``alembic_version`` is still 0139. A bare ``CREATE TABLE`` then raises
    # ``DuplicateTableError`` and wedges every deploy (the migrate job exits 1, so
    # the schema never advances and all 0140-expecting code boot-crashes). Skip
    # the create when the table is already present so the revision can advance; on
    # a fresh DB the table doesn't exist and it is created normally.
    bind = op.get_bind()
    if sa.inspect(bind).has_table("digestcache"):
        return
    op.create_table(
        "digestcache",
        sa.Column("cache_key", sa.String(), primary_key=True, nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.String(),
            nullable=False,
            server_default="worker",
        ),
    )


def downgrade() -> None:
    op.drop_table("digestcache")
