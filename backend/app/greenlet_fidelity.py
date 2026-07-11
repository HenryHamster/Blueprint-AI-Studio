"""Canonical registry for the MissingGreenlet test-fidelity guard.

Background
----------
The root ``backend/conftest.py`` globally no-ops ``InstanceState._expire`` so the
aiosqlite test backend does not raise ``MissingGreenlet`` on lazy-loads after
``session.rollback()``/``session.commit()``. That shim keeps the cheap SQLite CI
shard usable, but it also means a test that *relies* on rollback-triggered expiry
to reproduce the asyncpg failure mode passes tautologically on SQLite — even if
the production fix it guards were reverted.

``TestMissingGreenletTestsFaithful`` in
``backend/testing/test_audit_finding_class_guards.py`` scans every ``test_*.py``
file and flags any test whose docstring mentions ``greenlet`` but which uses no
recognised fidelity mechanism (fault injection, ``@pytest.mark.skipif(not
USE_POSTGRES)``, the ``expire_fidelity``/``missing_greenlet_proxy`` fixtures,
``expunge_all()``/``expire_all()``, ``raiseload=True``, or ``.expired``/``.unloaded``
reads).

This module is the **canonical, importable source** for the guard's growth
ratchet: the acknowledged-debt allowlist and its monotonic ceiling. The guard
test imports these names rather than defining them locally, mirroring how
``backend/app/database.py::ROLLBACK_SAFE_PATTERNS`` is the canonical source for
the sibling rollback-safety guard. Keeping the registry in production (rather
than inside the test module) makes the debt greppable from application code,
keeps the burn-down visible as a production diff, and lets the ceiling be
lowered deliberately as entries migrate to faithful patterns.

The module has no database or framework dependency, so it imports cleanly in the
``--standalone --no-postgres`` roster.
"""

from __future__ import annotations

# Tests that mention the MissingGreenlet hazard (``greenlet`` in their docstring)
# but cannot faithfully reproduce the asyncpg failure mode on SQLite, and are
# acknowledged technical debt rather than a fidelity regression. Each entry is a
# ``module::test_name`` selector. Ideally every entry eventually either gets a
# ``@pytest.mark.skipif(not USE_POSTGRES, ...)`` decorator, uses the
# ``expire_fidelity``/``missing_greenlet_proxy`` fixtures, or injects the fault
# directly — at which point it is removed here and the ceiling is lowered.
KNOWN_UNFAITHFUL_ALLOWLIST: frozenset[str] = frozenset({
    # Structured to AVOID the fault (keeps a sibling IN_PROGRESS so the
    # project-aggregation commit path never runs) rather than reproduce it, so it
    # carries genuine non-tautological SQLite coverage and must NOT be made
    # Postgres-only.
    "test_agent.py::test_agent_status_complete_with_artifacts_creates_artifact_record",
    # Owned by the parallel "expire_all() soundness hole" task — left untouched
    # here to avoid a cross-task merge conflict on test_local_runner.py.
    "test_local_runner.py::test_heartbeat_survives_declined_continuation_renewal_race",
    # Verifies hold-branch idempotency across cycles — real SQLite coverage the
    # docstring itself notes is separate from the asyncpg-pool-specific crash, so
    # skipif would drop legitimate coverage.
    "test_local_runner_holds.py::test_repeated_hold_no_change_does_not_crash",
    # alembic serialization test: docstring describes greenlet-bridged DDL
    # concurrency but the test exercises no ORM session — uses a fake engine, so
    # a Postgres-only skip would be semantically wrong.
    "test_migration_apply_gate.py::test_concurrent_applies_are_serialized",
})

# Monotonic ceiling for the allowlist: the list can ONLY shrink. Bump this
# constant when adding an entry (requires deliberate, visible action in the
# diff alongside the new entry). Burn-down is tracked by lowering the ceiling.
KNOWN_UNFAITHFUL_ALLOWLIST_CEILING: int = 4


def allowlist_within_ceiling() -> bool:
    """Return ``True`` when the known-unfaithful allowlist has not grown past its
    monotonic ceiling.

    This is the growth ratchet the guard enforces: adding an entry to
    ``KNOWN_UNFAITHFUL_ALLOWLIST`` without also raising
    ``KNOWN_UNFAITHFUL_ALLOWLIST_CEILING`` makes this return ``False``.
    """
    return len(KNOWN_UNFAITHFUL_ALLOWLIST) <= KNOWN_UNFAITHFUL_ALLOWLIST_CEILING
