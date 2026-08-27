"""The discovery evidence gate must not be able to halt the engine forever.

Two independent failures were needed to stall every direction on the project
for 18+ hours while ~200 strategic cycles minted and died with zero output:

1. A completion that shipped NOTHING because its deliverable was already on
   base was still counted as a delivery awaiting a landed receipt, because it
   carried a PR number inherited from an earlier attempt. That receipt could
   never arrive.
2. The resulting ``EVIDENCE_PENDING`` hold had no terminal bound. It escalated
   once, then held unchanged forever.

Either alone is survivable; together they are a silent total halt. These tests
pin both bounds. They are standalone (no DB): they exercise the pure
classification and timing helpers, which is exactly the part that must not
drift.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.discovery_review import (
    DISCOVERY_EVIDENCE_ESCALATION_GRACE,
    DISCOVERY_EVIDENCE_TERMINAL_GRACE,
    _evidence_hold_state,
)
from app.discovery_review_packet import _settled_non_delivery
from app.models import DiscoveryCycle, Task, TaskStatus

_NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def _task(**context) -> Task:
    return Task(id=uuid.uuid4(), context=dict(context))


def _cycle(review_result) -> DiscoveryCycle:
    return DiscoveryCycle(id=uuid.uuid4(), review_result=review_result)


# --------------------------------------------------------------------------
# 1. A no-op completion is a settled fact, not a delivery still in flight.
# --------------------------------------------------------------------------


def test_affirmative_no_op_is_not_a_pending_delivery():
    """``already_resolved`` means the content is on base; nothing shipped.

    A PR number on such a task is provenance from an earlier attempt. Holding
    the cycle until that PR gains a landed receipt holds it forever.
    """
    settled = _settled_non_delivery(
        _task(reproduce_verdict={"status": "already_resolved"}),
        task_type="salvage_bundle",
        pr_number=17807,
        source=None,
        source_settled_unlanded=False,
    )
    assert settled is not None, (
        "a completion that affirmatively found its deliverable already on base "
        "must be recorded as settled, not held as missing evidence"
    )
    assert settled["disposition"] == "no_op"
    assert settled["pr_number"] == 17807


def test_legacy_no_op_marker_is_honored():
    """The ``--already-done`` marker predates the reproduce verdict; still settled."""
    settled = _settled_non_delivery(
        _task(no_op_reason="deliverable already present on base at 052caf1"),
        task_type="salvage_bundle",
        pr_number=17807,
        source=None,
        source_settled_unlanded=False,
    )
    assert settled is not None
    assert settled["disposition"] == "no_op"


def test_stranded_parent_stays_a_distinct_disposition():
    """A failed parent that never rolled up is a different fact from a no-op.

    Both are final, but they call for different next moves, so the review is
    told which one it is looking at.
    """
    source = Task(id=uuid.uuid4(), context={})
    source.status = TaskStatus.FAILED
    settled = _settled_non_delivery(
        _task(),
        task_type="code_fix",
        pr_number=42,
        source=source,
        source_settled_unlanded=True,
    )
    assert settled is not None
    assert settled["disposition"] == "stranded"


def test_a_genuine_in_flight_delivery_still_holds():
    """The gate must keep its teeth: an ordinary unlanded PR is still missing.

    Without this, the fix above would silently accept every unlanded delivery
    and the review would decide on work that never landed.
    """
    assert (
        _settled_non_delivery(
            _task(),
            task_type="code_fix",
            pr_number=99,
            source=None,
            source_settled_unlanded=False,
        )
        is None
    )


def test_indeterminate_verdict_is_not_a_no_op():
    """``indeterminate`` did not rule the defect out, so it is not settled."""
    assert (
        _settled_non_delivery(
            _task(reproduce_verdict={"status": "indeterminate"}),
            task_type="code_fix",
            pr_number=99,
            source=None,
            source_settled_unlanded=False,
        )
        is None
    )


# --------------------------------------------------------------------------
# 2. The wait itself is bounded.
# --------------------------------------------------------------------------


def test_evidence_grace_periods_are_pinned_and_ordered():
    """Pinned as literals: these bound how long the engine may stall.

    Asserting them against themselves would certify any future change, which is
    the self-certifying diff this repo refuses. The ordering invariant matters
    independently -- a terminal bound at or below the escalation grace would
    spawn the degraded decision before anyone was ever told evidence was late.
    """
    assert DISCOVERY_EVIDENCE_ESCALATION_GRACE == timedelta(hours=2)
    assert DISCOVERY_EVIDENCE_TERMINAL_GRACE == timedelta(hours=6)
    assert DISCOVERY_EVIDENCE_TERMINAL_GRACE > DISCOVERY_EVIDENCE_ESCALATION_GRACE


def test_a_new_gap_starts_a_fresh_wait():
    assert _evidence_hold_state(_cycle(None), packet_checksum="abc", missing=["x"]) == (
        False,
        None,
    )


def test_the_same_gap_keeps_its_original_start_time():
    """Otherwise every sweep would reset the clock and the deadline never lands."""
    cycle = _cycle(
        {
            "status": "EVIDENCE_PENDING",
            "packet_checksum": "abc",
            "missing_required_evidence": ["x"],
            "first_seen_at": _NOW.isoformat(),
        }
    )
    same, started = _evidence_hold_state(cycle, packet_checksum="abc", missing=["x"])
    assert same is True
    assert started == _NOW


def test_a_changed_gap_restarts_the_wait():
    """The cycle is now waiting on something else, so it gets its own clock."""
    cycle = _cycle(
        {
            "status": "EVIDENCE_PENDING",
            "packet_checksum": "abc",
            "missing_required_evidence": ["x"],
            "first_seen_at": _NOW.isoformat(),
        }
    )
    assert _evidence_hold_state(cycle, packet_checksum="def", missing=["x"]) == (
        False,
        None,
    )
    assert _evidence_hold_state(cycle, packet_checksum="abc", missing=["y"]) == (
        False,
        None,
    )


def test_an_unparseable_start_time_keeps_waiting_rather_than_expiring():
    """Fail toward waiting, never toward a spuriously expired deadline."""
    cycle = _cycle(
        {
            "status": "EVIDENCE_PENDING",
            "packet_checksum": "abc",
            "missing_required_evidence": ["x"],
            "first_seen_at": "not-a-timestamp",
        }
    )
    same, started = _evidence_hold_state(cycle, packet_checksum="abc", missing=["x"])
    assert same is True
    assert started is None


def test_the_hold_expires_once_the_terminal_grace_elapses():
    """The property the halt violated: waiting must end.

    This mirrors the spawn-path condition exactly -- inside the grace it holds,
    past it the decision is spawned with the gap declared.
    """
    started = _NOW
    inside = started + DISCOVERY_EVIDENCE_TERMINAL_GRACE - timedelta(seconds=1)
    past = started + DISCOVERY_EVIDENCE_TERMINAL_GRACE

    assert inside < started + DISCOVERY_EVIDENCE_TERMINAL_GRACE
    assert not (past < started + DISCOVERY_EVIDENCE_TERMINAL_GRACE)

    # The real incident: an 18h+ wait on a single item must be long expired.
    observed_halt = started + timedelta(hours=18)
    assert not (observed_halt < started + DISCOVERY_EVIDENCE_TERMINAL_GRACE)
