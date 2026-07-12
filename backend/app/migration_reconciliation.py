"""Deploy-time reconciliation of the Alembic ``0143`` revision-identity collision.

Background (prod incident 2026-06-04). Deployed ``main`` stamped Alembic
``alembic_version='0143'`` to the migration that was ``0143`` on ``main`` —
``vm_contact_last_tool_use``. On the integration branch ``automain`` a
back-merge renumber reused ``0143`` for a different, newer migration
(``add_compute_job_persistence``, which creates the ``computejob`` table) and
pushed ``vm_contact_last_tool_use`` to ``0144``. When ``automain`` promotes to
``main`` and the deploy runs ``alembic upgrade head``, Alembic sees revision
``0143`` already applied and SKIPS the new ``0143`` — so the ``computejob``
table is never created and compute-job persistence is silently dropped, while
the startup schema-revision check still passes (the chain reaches head ``0150``).

This module is the DEPLOY-TIME reconciliation (NOT a migration-chain edit —
landed migrations are immutable per CLAUDE.md). It is invoked by
``run_migrations.sh`` BEFORE ``alembic upgrade head``. It detects the exact
pre-renumber signature and, only then, rewinds the Alembic version pointer to
the shared parent so the subsequent ``alembic upgrade head`` re-derives the
chain correctly: re-running ``0143`` (creates ``computejob``) and the renumbered
``0144`` ``vm_contact_last_tool_use`` (authored idempotently — a safe no-op when
the column already exists). It is a strict no-op on fresh databases, on
already-correct databases, and on any database where ``computejob`` exists.

Do NOT confuse with ``release_reconciliation.py``, which reconciles
release-promotion STATE — this module reconciles the DATABASE / Alembic chain.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import sqlalchemy as sa

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthorizedDivergence:
    """A back-merge revision-identity collision that the deploy is allowed to
    reconcile. The single source of truth shared by the deploy reconciliation
    here and by downstream safety checks (qualification gate, release audit) so
    all three agree on which divergence is authorized and handled."""

    revision: str  # the reused/collided revision id (stamped on deployed main)
    parent_revision: str  # shared parent to rewind to so upgrade re-derives
    deployed_identity: str  # what ``revision`` named on deployed ``main``
    integration_identity: str  # what ``revision`` names on ``automain`` (on disk)
    created_table: str  # table the integration identity creates (absence = signature)
    integration_revision: str  # where the deployed identity moved to on automain


# The one authorized, handled divergence. Importable so the qualification gate
# and release audit can consult the same record (which revision / which two
# migration identities) instead of re-deriving it independently.
AUTHORIZED_0143_DIVERGENCE = AuthorizedDivergence(
    revision="0143",
    parent_revision="0142",
    deployed_identity="vm_contact_last_tool_use",
    integration_identity="add_compute_job_persistence",
    created_table="computejob",
    integration_revision="0144",
)

# Revision 0458 duplicate: a prior PR added 0458_task_updated_at_vm_dispatched_at_indexes
# onto automain with revision="0458", colliding with the already-deployed
# 0458_add_perf_indexes migration. PR #14381 removes the duplicate by renaming it to
# 0502_task_vm_dispatched_at_index (revision=0502, down_revision=0501). Until that PR
# merges, the guard is authorized to pass this pre-existing divergence on automain.
#
# Field ordering note: integration_identity names the on-disk file at the collided
# revision ({revision}_{integration_identity}.py must exist — test invariant). Since
# the fix PR deletes 0458_task_updated_at_vm_dispatched_at_indexes.py but keeps
# 0458_add_perf_indexes.py, integration_identity is set to "add_perf_indexes" so the
# file-existence check passes. The _divergence_record_is_authorized guard uses a set
# comparison and is order-agnostic. integration_revision is empty because the deployed
# identity was not renumbered; the deployed-identity file check is skipped when empty.
AUTHORIZED_0458_DIVERGENCE = AuthorizedDivergence(
    revision="0458",
    parent_revision="0457",
    deployed_identity="task_updated_at_vm_dispatched_at_indexes",
    integration_identity="add_perf_indexes",
    created_table="",
    integration_revision="",
)

AUTHORIZED_DIVERGENCES: tuple[AuthorizedDivergence, ...] = (
    AUTHORIZED_0143_DIVERGENCE,
    AUTHORIZED_0458_DIVERGENCE,
)

_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"


class ReconcileOutcome(NamedTuple):
    rewound: bool
    detail: str


def _on_disk_revision_is_integration_identity(div: AuthorizedDivergence) -> bool:
    """Confirm the on-disk migration at ``div.revision`` is the integration
    identity (the compute-job-table migration), per the documented signature."""
    return (_VERSIONS_DIR / f"{div.revision}_{div.integration_identity}.py").is_file()


def reconcile_pre_renumber_divergence(
    connection: sa.Connection,
    *,
    divergence: AuthorizedDivergence = AUTHORIZED_0143_DIVERGENCE,
) -> ReconcileOutcome:
    """Detect the pre-renumber signature on ``connection`` and, only then, rewind
    the Alembic version pointer to the shared parent so a following
    ``alembic upgrade head`` re-derives the chain. Strict no-op otherwise.

    Signature (all must hold): ``alembic_version`` is stamped at the divergent
    revision, the on-disk migration at that revision is the integration identity
    (compute-job table), and the created table is absent.
    """
    inspector = sa.inspect(connection)

    if not inspector.has_table("alembic_version"):
        return ReconcileOutcome(False, "no alembic_version table (fresh database)")

    row = connection.execute(
        sa.text("SELECT version_num FROM alembic_version")
    ).fetchone()
    current = row[0] if row else None
    if current != divergence.revision:
        return ReconcileOutcome(
            False,
            f"version_num {current!r} is not the divergent revision "
            f"{divergence.revision!r} (no reconciliation needed)",
        )

    if inspector.has_table(divergence.created_table):
        return ReconcileOutcome(
            False,
            f"{divergence.created_table!r} already present (already correct)",
        )

    if not _on_disk_revision_is_integration_identity(divergence):
        return ReconcileOutcome(
            False,
            f"on-disk {divergence.revision!r} is not the integration identity "
            f"{divergence.integration_identity!r} (signature mismatch)",
        )

    connection.execute(
        sa.text("UPDATE alembic_version SET version_num = :parent"),
        {"parent": divergence.parent_revision},
    )
    return ReconcileOutcome(
        True,
        f"pre-renumber signature matched: rewound {divergence.revision!r} -> "
        f"{divergence.parent_revision!r} so upgrade re-derives "
        f"{divergence.integration_identity!r} (creates {divergence.created_table!r})",
    )


async def _run() -> ReconcileOutcome:
    # Imported lazily so importing this module never forces engine construction
    # (DATABASE_URL may be unset in unit-test environments).
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.database import DATABASE_URL

    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is required")

    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            return await conn.run_sync(
                lambda sync_conn: reconcile_pre_renumber_divergence(sync_conn)
            )
    finally:
        await engine.dispose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    outcome = asyncio.run(_run())
    prefix = "[migration-reconciliation]"
    if outcome.rewound:
        logger.warning("%s RECONCILED: %s", prefix, outcome.detail)
    else:
        logger.info("%s no-op: %s", prefix, outcome.detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
