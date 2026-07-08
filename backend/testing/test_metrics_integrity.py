"""Acceptance evidence for data-integrity release criterion c-1a3ec09d.

A single durable DB-backed test that asserts BOTH seams:

1. The v22 backfill resolves 13 carried low-severity findings into
   AuditFindingResolution rows, and the new count-guard helper detects
   (or does not detect, for the canonical count of 13) divergence.
2. The standing metrics-integrity sweep runs (mode=observe returns a
   non-None tick summary) and is gated out when mode=off.

This is the named_test acceptance evidence declared by the parent
direction — a proven-verdict merge records honest deployed-engine
coverage for criterion c-1a3ec09d.
"""

from __future__ import annotations

import pytest
from sqlmodel import select

from app.metrics_integrity_sweep import (
    _reset_observability_for_tests,
    get_metrics_integrity_observability,
    run_metrics_integrity_sweep,
)
from app.models import AuditFindingResolution
from app.release_readiness import (
    _V22_BACKFILL_RESOLVING_REF,
    _V22_EXPECTED_LOW_SEVERITY_COUNT,
    _v22_count_diverges,
    _reset_v22_backfill_guard_for_tests,
    backfill_v22_carried_findings_resolved,
    get_v22_backfill_guard_state,
)
from app.shared_state import runtime_config as _rc
from app.time_utils import _utcnow

# Reuse the well-tested v22 seeding helpers from test_acceptance_criteria.py
# rather than duplicating them.
from testing.test_acceptance_criteria import (
    _make_project,
    _make_release_version,
    _make_v22_low_severity_findings,
    _seed_v22_verdict,
)


@pytest.fixture(autouse=True)
def _manage_sweep_state():
    """Save / restore metrics-integrity sweep config and reset observability.

    Each test starts with a clean cadence gate, mode=observe, and the
    interval pinned to 60 minutes.  Restored after the test so this
    module's config changes cannot leak into other test modules.
    """
    _reset_observability_for_tests()
    saved = {
        k: v for k, v in _rc.items() if k.startswith("metrics_integrity_sweep_")
    }
    _rc["metrics_integrity_sweep_mode"] = "observe"
    _rc["metrics_integrity_sweep_interval_minutes"] = 60
    yield
    for k in list(_rc):
        if k.startswith("metrics_integrity_sweep_"):
            _rc.pop(k, None)
    _rc.update(saved)
    _reset_observability_for_tests()


# ---------------------------------------------------------------------------
# _v22_count_diverges — pure helper unit tests
# ---------------------------------------------------------------------------


def test_count_diverges_returns_false_for_canonical_13():
    """The canonical count (13) is NOT flagged as divergent."""
    assert not _v22_count_diverges(13)
    assert not _v22_count_diverges(13, expected_count=13)


def test_count_diverges_returns_true_for_mismatch():
    """Any count != 13 IS flagged as divergent."""
    assert _v22_count_diverges(7)
    assert _v22_count_diverges(7, expected_count=13)
    assert _v22_count_diverges(0)
    assert _v22_count_diverges(20)


def test_count_diverges_respects_custom_expected():
    """The expected_count parameter is honoured."""
    assert not _v22_count_diverges(5, expected_count=5)
    assert _v22_count_diverges(5, expected_count=10)


# ---------------------------------------------------------------------------
# Integrated DB test — both seams in one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_integrity_sweep_and_v22_findings_resolved(db_session):
    """Assert BOTH v22 backfill correctness AND standing sweep execution.

    v22 findings resolved:
      - Seed a promoted v22 ReleaseVersion with exactly 13 synthetic
        low-severity carried findings (via a QualificationVerdict).
      - Call backfill_v22_carried_findings_resolved.
      - Assert exactly 13 AuditFindingResolution rows exist for the
        version, each state='resolved' with the canonical resolving_ref.
      - Exercise the count-guard: for the canonical 13, match=True and
        _v22_count_diverges returns False.
      - Cover the guard's mismatch path: a count of 7 IS flagged by
        _v22_count_diverges.

    Standing sweep runs:
      - With mode=observe and the cadence gate reset, call
        run_metrics_integrity_sweep — it MUST return a non-None tick
        summary (the sweep actually executes registered checks).
      - With mode=off (and cadence reset), the call MUST return None.
    """
    project = await _make_project(db_session, "MISweepAndV22")

    # ------------------------------------------------------------------
    # Part 1 — v22 backfill correctness + count guard
    # ------------------------------------------------------------------
    _reset_v22_backfill_guard_for_tests()

    version = await _make_release_version(
        db_session, project, 22, status="promoted"
    )
    findings = _make_v22_low_severity_findings(13)
    await _seed_v22_verdict(
        db_session, project, version, findings, sha="f" * 40
    )

    count = await backfill_v22_carried_findings_resolved(
        db_session, project.id, version
    )

    # The backfill must resolve exactly 13 findings.
    assert count == 13, f"expected 13 resolution rows; got {count}"

    # AuditFindingResolution rows must match the expected count.
    rows = list(
        (
            await db_session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 13, (
        f"expected 13 AuditFindingResolution rows; got {len(rows)}"
    )
    for row in rows:
        assert row.state == "resolved", (
            f"row {row.finding_key} state={row.state!r}"
        )
        assert row.resolving_ref == _V22_BACKFILL_RESOLVING_REF, (
            f"expected resolving_ref={_V22_BACKFILL_RESOLVING_REF!r}; "
            f"got {row.resolving_ref!r}"
        )
        assert row.severity == "low"
        assert row.project_id == project.id

    # Guard: the canonical count of 13 must NOT trigger divergence.
    guard = get_v22_backfill_guard_state()
    assert guard == {
        "expected": 13,
        "resolved": 13,
        "match": True,
    }, f"unexpected guard state: {guard}"
    assert not _v22_count_diverges(count)

    # Cover the guard's mismatch path: a count of 7 IS flagged.
    assert _v22_count_diverges(7)
    assert _v22_count_diverges(7, expected_count=_V22_EXPECTED_LOW_SEVERITY_COUNT)

    # ------------------------------------------------------------------
    # Part 2 — standing metrics-integrity sweep runs
    # ------------------------------------------------------------------
    from app import metrics_integrity_sweep as mis

    # mode=observe + reset cadence → MUST return a tick summary
    mis._reset_observability_for_tests()
    _rc["metrics_integrity_sweep_mode"] = "observe"
    tick = await run_metrics_integrity_sweep(db_session, now=_utcnow())
    assert tick is not None, (
        "run_metrics_integrity_sweep must return a tick summary "
        "when mode=observe and the cadence gate is reset"
    )
    obs = get_metrics_integrity_observability()
    assert obs["ticks"] >= 1, f"expected at least 1 tick; got {obs['ticks']}"

    # mode=off → MUST return None (sweep is gated out)
    _rc["metrics_integrity_sweep_mode"] = "off"
    mis._reset_observability_for_tests()
    assert (
        await run_metrics_integrity_sweep(db_session, now=_utcnow()) is None
    ), "run_metrics_integrity_sweep must return None when mode=off"
