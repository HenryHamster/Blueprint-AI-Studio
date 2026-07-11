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


# ---------------------------------------------------------------------------
# Rollback-flow allowlist — functions that call session.rollback() and have
# post-rollback ORM attribute accesses that the flow-aware scanner flags but
# that are genuinely safe for reasons the static scanner cannot verify (e.g.
# a re-fetch via a helper function, a separate session scope, a Pydantic model
# mistaken for an ORM object, or a guarded deferred-refresh pattern).
#
# Each entry is ``module_filename::function_name`` and must carry an inline
# comment explaining why the function is safe despite the scanner's flag.
# ---------------------------------------------------------------------------
ROLLBACK_FLOW_ALLOWLIST: frozenset[str] = frozenset({
    # analytics rollbacks inside contextlib.suppress() between L1924-L2018;
    # each suppress block catches the rollback exception and the ORM attribute
    # task.context at L2061 is read on the previously-committed task instance
    # which is not owned by the rolling-back analytics sub-session.
    "agent.py::claim_task",
    # three rollback sites all guarded: L3446 (task.context written after
    # rollback inside suppress, not a lazy-load read); L1878 (rollback inside
    # suppress, task.context at L2634 is an ORM write `task.context = _ctx`,
    # not a read that triggers lazy-load); L3586 scanner flags payload.status
    # but payload is a Pydantic model, not an ORM object.
    "agent_status.py::update_agent_status",
    # r.flagged at L469 where r is a CheckResult dataclass (@dataclass frozen=True),
    # not an ORM-mapped model; scanner cannot distinguish dataclass fields from
    # mapped ORM columns.
    "metrics_integrity_sweep.py::run_metrics_integrity_sweep",
    # _crashed_task.status at L3957 is on an instance freshly loaded via
    # _lock_dispatchable_task_for_mutation(session, _qt_id) after the rollback
    # at L3815; scanner cannot see the re-fetch because it's wrapped in a helper.
    "orchestrator.py::orchestrator_worker",
    # ci_task.status at L2884 is on an instance freshly re-fetched via
    # _lock_task_for_mutation(session, ci_task_id, ...) at L2873 immediately
    # after the rollback at L2867; scanner cannot trace through the helper.
    "orchestrator_ci_monitor.py::_monitor_evaluating_tasks",
    # version.id at L1364 is accessed after rollback of fire_session (a scoped
    # sub-session); version is bound to the outer session's identity map so
    # fire_session.rollback() does not expire it.
    "release_qualifier_sweep.py::_run_auto_qualification_trigger_for_version",
    # existing.id at L1219 is read after IntegrityError rollback at L1213;
    # existing is immediately re-fetched via _get_active_pipeline_task_by_key
    # (which issues session.execute internally) before the .id read; scanner
    # cannot trace the re-fetch through the helper call.
    "review_pipeline.py::_spawn_verify_tests_task",
    # auto_resolved and still_unproven at L3537-L3541 are plain Python lists,
    # not ORM-mapped attributes; scanner cannot distinguish list reads from
    # ORM lazy-load reads that would trigger a detached-instance error.
    "metrics_integrity_sweep.py::check_capability_proof_gate_gap",
})

# Monotonic ceiling for the rollback-flow allowlist: can only shrink as entries
# are fixed at the production code level. Bump when adding a new entry.
ROLLBACK_FLOW_ALLOWLIST_CEILING: int = 8


def rollback_flow_allowlist_within_ceiling() -> bool:
    """Return ``True`` when the rollback-flow allowlist has not grown past its
    monotonic ceiling.

    This is the growth ratchet: adding an entry to ``ROLLBACK_FLOW_ALLOWLIST``
    without also raising ``ROLLBACK_FLOW_ALLOWLIST_CEILING`` makes this return
    ``False``, causing ``test_rollback_flow_allowlist_cannot_grow_beyond_ceiling``
    to fail.
    """
    return len(ROLLBACK_FLOW_ALLOWLIST) <= ROLLBACK_FLOW_ALLOWLIST_CEILING
