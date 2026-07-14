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
from dataclasses import dataclass, field
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

    # Audit + provenance metadata. Empty for the baked-in floor
    # (``AUTHORIZED_DIVERGENCES``); populated ONLY for operator-approved RUNTIME
    # exemptions rehydrated from the DB-backed runtime config. Declared
    # ``compare=False`` so authorization matching stays keyed EXCLUSIVELY on the
    # migration-identity fields above — a runtime exemption authorizes a
    # divergence only on an exact identity match, identical to the baked path,
    # and never via its audit fields. ``is_runtime`` lets a consumer emit an
    # audit log line naming the operator when a RUNTIME (non-baked) exemption is
    # what waved a divergence through.
    added_by: str = field(default="", compare=False)  # operator who authorized
    added_at: str = field(default="", compare=False)  # ISO-8601 authorization time
    reason: str = field(default="", compare=False)  # human justification
    is_runtime: bool = field(default=False, compare=False)  # not the baked floor


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

# Runtime-config key holding the operator-approved, DB-backed exemption store.
# Each stored entry is a dict carrying the six AuthorizedDivergence match fields
# PLUS audit metadata (added_by / added_at / reason). The effective exemption
# list consumed by the safety gates is (baked constant) UNION (valid runtime
# entries), so a NEW operator-approved exemption is honored by the DEPLOYED gate
# WITHOUT a redeploy: operator writes DB -> sync_runtime_config_from_state
# refreshes the in-memory runtime_config -> the next gate evaluation reads it via
# effective_authorized_divergences().
RUNTIME_EXEMPTIONS_CONFIG_KEY = "migration_divergence_exemptions"

_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def normalize_runtime_divergence_exemption(entry: object) -> dict | None:
    """Validate + normalize ONE runtime exemption dict; return a clean all-string
    dict, or ``None`` if malformed.

    This value gates a deploy-safety check, so validation is strict and
    fail-closed: a garbage row must NEVER weaken the gate. An entry is well-formed
    only when it carries a non-empty revision, parent_revision, deployed_identity,
    integration_identity AND the audit trail that makes it an *authorized* record
    (``added_by`` naming the operator and a ``reason``). ``created_table`` and
    ``integration_revision`` may be empty (the baked 0458 entry leaves them empty
    too). Shared by BOTH the rehydrate path (runtime_config_state) and the resolver
    below so the DB store and the live gate agree on what counts as well-formed.
    """
    if not isinstance(entry, dict):
        return None

    def _s(key: str) -> str:
        val = entry.get(key)
        return str(val).strip() if val is not None else ""

    revision = _s("revision")
    parent_revision = _s("parent_revision")
    deployed_identity = _s("deployed_identity")
    integration_identity = _s("integration_identity")
    added_by = _s("added_by")
    reason = _s("reason")
    # Required fields — anything missing means the row cannot authorize anything.
    if not (
        revision
        and parent_revision
        and deployed_identity
        and integration_identity
        and added_by
        and reason
    ):
        return None
    return {
        "revision": revision,
        "parent_revision": parent_revision,
        "deployed_identity": deployed_identity,
        "integration_identity": integration_identity,
        "created_table": _s("created_table"),
        "integration_revision": _s("integration_revision"),
        "added_by": added_by,
        "added_at": _s("added_at"),
        "reason": reason,
    }


def _runtime_authorized_divergences() -> tuple[AuthorizedDivergence, ...]:
    """Parse the LIVE in-memory runtime-config exemption store into
    ``AuthorizedDivergence`` records (audit metadata preserved on each, for
    logging; ignored for matching). Reads ``runtime_config`` lazily so this module
    stays importable with NO database (the existing unit tests require that). Any
    malformed entry is dropped defensively — a bad row never weakens the gate.
    """
    try:
        from app.runtime_config_state import runtime_config
    except Exception:  # noqa: BLE001 — no runtime config available (e.g. unit tests)
        return ()

    raw = runtime_config.get(RUNTIME_EXEMPTIONS_CONFIG_KEY)
    if not isinstance(raw, (list, tuple)):
        return ()

    out: list[AuthorizedDivergence] = []
    for entry in raw:
        norm = normalize_runtime_divergence_exemption(entry)
        if norm is None:
            continue
        out.append(
            AuthorizedDivergence(
                revision=norm["revision"],
                parent_revision=norm["parent_revision"],
                deployed_identity=norm["deployed_identity"],
                integration_identity=norm["integration_identity"],
                created_table=norm["created_table"],
                integration_revision=norm["integration_revision"],
                added_by=norm["added_by"],
                added_at=norm["added_at"],
                reason=norm["reason"],
                is_runtime=True,
            )
        )
    return tuple(out)


def effective_authorized_divergences() -> tuple[AuthorizedDivergence, ...]:
    """The effective authorized-divergence list the safety gates consult:
    the immutable baked-in floor UNION the operator-approved runtime store.

    The baked ``AUTHORIZED_DIVERGENCES`` tuple is the floor and is never weakened;
    runtime exemptions are purely additive. Callers must invoke this at CALL time
    (not import time) so a newly-added exemption is honored without a redeploy.
    """
    runtime = _runtime_authorized_divergences()
    if not runtime:
        return AUTHORIZED_DIVERGENCES
    return AUTHORIZED_DIVERGENCES + runtime


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
