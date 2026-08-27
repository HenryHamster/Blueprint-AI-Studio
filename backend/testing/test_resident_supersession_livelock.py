"""Regression coverage for the phase-2 resident supersession livelock.

Production incident (2026-08-25/26, project 37d445e1): strategic cycles minted
and died at ``resident_worker_supersession`` at a rate of ~10/hour with ZERO
completions across 27 consecutive hours. Every cancelled row had
``started_at IS NULL``, ``last_heartbeat IS NULL`` and ``heartbeat_count = 0``:
the operator's local runner had never pulled the pin, so there was no worker and
never had been one. ``worker_generation`` climbed to 177.

Root cause: ``_resident_worker_health_verdict`` judged a runner pin still
AWAITING ITS PULL by the ``_RESIDENT_START_CLAIM_STALE_SECONDS`` (300s) start
clock. Observed pull latency on the single backlogged runner was 15-90 minutes
(one measured pin waited 3h23m and then ran fine), so the pin was condemned long
before the runner could reach it -- and because supersession CANCELS the task
and spawns a replacement, each generation re-entered the same queue at the BACK.
The queue position was destroyed faster than it could ever be reached, so the
loop could never converge: a self-inflicted livelock, not a genuine race.

These tests pin the fix: an unpulled runner pin is condemned ONLY by its device
going away, never by the start clock; every other supersession path is
unchanged; and a superseded row states WHY it lost and WHICH generation won.
"""

import uuid
from datetime import datetime, timedelta

import pytest

import app.strategic_resident_session as resident
from app.models import Task, TaskStatus


# Naive UTC on purpose. The resident session code stores and compares naive UTC
# datetimes throughout (``_utcnow()`` returns naive), so a tz-aware constant here
# would raise "can't subtract offset-naive and offset-aware datetimes" the moment
# the verdict function did its staleness arithmetic.
NOW = datetime(2026, 8, 26, 21, 44, 37)  # noqa: DTZ001
RUNNER_UUID = uuid.UUID("707de5fb-7b6e-406a-9105-774b28ea23be")


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _blob(started_at: datetime, **extra) -> dict:
    """The vision session blob as the ensure path reads it."""
    blob = {
        "session_id": "sess-winner",
        "task_id": str(uuid.uuid4()),
        "worker_generation": 177,
        "started_at": _iso(started_at),
    }
    blob.update(extra)
    return blob


def _pinned_awaiting_pull_task(**overrides) -> Task:
    """The exact production shape: pinned, PROVISIONING, never pulled."""
    fields = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "status": TaskStatus.PROVISIONING,
        "vm_instance_id": f"runner:{RUNNER_UUID}",
        "started_at": None,
        "last_heartbeat": None,
        "context": {},
    }
    fields.update(overrides)
    return Task(**fields)


class _FakeSession:
    """Minimal stand-in for AsyncSession.get used by _pinned_runner_is_live."""

    def __init__(self, runner):
        self._runner = runner
        self.gets: list = []

    async def get(self, _model, key):
        self.gets.append(key)
        return self._runner


class _FakeRunner:
    def __init__(self, *, status="active", last_seen_at=None):
        self.id = RUNNER_UUID
        self.status = status
        self.last_seen_at = last_seen_at


# --------------------------------------------------------------------------
# The decisive regression: the livelock itself.
# --------------------------------------------------------------------------


def test_unpulled_runner_pin_survives_a_long_dead_start_claim():
    """THE regression. A pin the runner has not reached yet is NOT superseded.

    Start claim is 3h23m old -- the real measured pull latency of the pin that
    eventually ran successfully in production. Before the fix this returned
    ``start_claim_stale`` at the 300s mark and the cycle was cancelled.
    """
    stale_by = timedelta(hours=3, minutes=23)
    task = _pinned_awaiting_pull_task()
    blob = _blob(NOW - stale_by)

    # Precondition: the claim really is far past the start-claim window, so this
    # test cannot pass by accident on a claim that simply was not stale yet.
    claim_age = (NOW - datetime.fromisoformat(blob["started_at"])).total_seconds()
    assert claim_age > resident._RESIDENT_START_CLAIM_STALE_SECONDS

    verdict = resident._resident_worker_health_verdict(
        task, blob, NOW, pinned_runner_live=True
    )
    assert verdict is None, (
        "an unpulled runner pin must not be condemned by the start clock -- "
        f"got {verdict!r}"
    )
    assert resident._resident_worker_task_is_healthy(
        task, blob, NOW, pinned_runner_live=True
    )


def test_unpulled_runner_pin_survives_when_runner_liveness_is_unknown():
    """Unresolved liveness must never condemn: a false offline destroys work."""
    task = _pinned_awaiting_pull_task()
    blob = _blob(NOW - timedelta(hours=2))

    assert (
        resident._resident_worker_health_verdict(
            task, blob, NOW, pinned_runner_live=None
        )
        is None
    )
    # Default argument must be the same non-condemning behaviour.
    assert resident._resident_worker_health_verdict(task, blob, NOW) is None


def test_unpulled_queued_runner_pin_also_survives():
    """The carve-out covers QUEUED pins, not just PROVISIONING ones."""
    task = _pinned_awaiting_pull_task(status=TaskStatus.QUEUED)
    blob = _blob(NOW - timedelta(hours=1))

    assert (
        resident._resident_worker_health_verdict(
            task, blob, NOW, pinned_runner_live=True
        )
        is None
    )


# --------------------------------------------------------------------------
# The legitimate purpose of supersession must survive the fix.
# --------------------------------------------------------------------------


def test_unpulled_pin_is_superseded_when_its_device_is_gone():
    """The one condemnation that remains for an unpulled pin."""
    task = _pinned_awaiting_pull_task()
    blob = _blob(NOW - timedelta(hours=2))

    assert (
        resident._resident_worker_health_verdict(
            task, blob, NOW, pinned_runner_live=False
        )
        == resident.RESIDENT_SUPERSEDE_PINNED_RUNNER_OFFLINE
    )


def test_unpinned_queued_task_still_supersedes_on_a_stale_start_claim():
    """Cloud-fallback (unpinned) tasks keep the original start-clock contract.

    Guards against the fix being a blanket disable of supersession.
    """
    task = _pinned_awaiting_pull_task(vm_instance_id=None)
    blob = _blob(NOW - timedelta(seconds=600))

    assert (
        resident._resident_worker_health_verdict(
            task, blob, NOW, pinned_runner_live=True
        )
        == resident.RESIDENT_SUPERSEDE_START_CLAIM_STALE
    )


def test_pin_already_pulled_is_still_judged_on_its_worker_heartbeat():
    """Once the runner pulls (started_at stamped) the carve-out stops applying."""
    task = _pinned_awaiting_pull_task(
        status=TaskStatus.IN_PROGRESS,
        started_at=NOW - timedelta(hours=1),
        last_heartbeat=NOW - timedelta(hours=1),
    )
    blob = _blob(
        NOW - timedelta(hours=2),
        worker_heartbeat_at=_iso(NOW - timedelta(minutes=30)),
    )

    assert (
        resident._resident_worker_health_verdict(
            task, blob, NOW, pinned_runner_live=True
        )
        == resident.RESIDENT_SUPERSEDE_WORKER_HEARTBEAT_STALE
    )


def test_live_worker_heartbeat_is_healthy():
    task = _pinned_awaiting_pull_task(
        status=TaskStatus.IN_PROGRESS,
        started_at=NOW - timedelta(hours=1),
        last_heartbeat=NOW - timedelta(seconds=10),
    )
    blob = _blob(
        NOW - timedelta(hours=2),
        worker_heartbeat_at=_iso(NOW - timedelta(seconds=10)),
    )

    assert resident._resident_worker_health_verdict(task, blob, NOW) is None


def test_terminal_backing_task_is_reported_as_terminal():
    task = _pinned_awaiting_pull_task(status=TaskStatus.COMPLETED)
    assert (
        resident._resident_worker_health_verdict(task, _blob(NOW), NOW)
        == resident.RESIDENT_SUPERSEDE_WORKER_TASK_TERMINAL
    )


def test_missing_backing_task_is_reported_as_missing():
    assert (
        resident._resident_worker_health_verdict(None, _blob(NOW), NOW)
        == resident.RESIDENT_SUPERSEDE_WORKER_TASK_MISSING
    )


# --------------------------------------------------------------------------
# The carve-out must not drift from the reaper's canonical classifier.
# --------------------------------------------------------------------------


def test_awaiting_pull_delegates_to_the_canonical_reaper_classifier():
    from app.reaper_orphan_classify import _is_runner_pinned_awaiting_pull

    pinned = _pinned_awaiting_pull_task()
    pulled = _pinned_awaiting_pull_task(started_at=NOW)
    cloud = _pinned_awaiting_pull_task(vm_instance_id="instance-1234")

    for task in (pinned, pulled, cloud):
        assert resident._resident_task_awaiting_runner_pull(
            task
        ) is _is_runner_pinned_awaiting_pull(task)

    assert resident._resident_task_awaiting_runner_pull(pinned) is True
    assert resident._resident_task_awaiting_runner_pull(pulled) is False
    assert resident._resident_task_awaiting_runner_pull(cloud) is False
    assert resident._resident_task_awaiting_runner_pull(None) is False


# --------------------------------------------------------------------------
# Pinned-runner liveness resolution.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pinned_runner_liveness_is_none_for_unpinned_tasks():
    session = _FakeSession(_FakeRunner())
    for vm_id in (None, "", "instance-1234", "runner:not-a-uuid"):
        task = _pinned_awaiting_pull_task(vm_instance_id=vm_id)
        assert await resident._pinned_runner_is_live(session, task) is None
    assert session.gets == [], "unpinned tasks must not hit the runner table"


@pytest.mark.asyncio
async def test_pinned_runner_liveness_false_when_runner_row_is_gone():
    session = _FakeSession(None)
    task = _pinned_awaiting_pull_task()
    assert await resident._pinned_runner_is_live(session, task) is False
    assert session.gets == [RUNNER_UUID]


@pytest.mark.asyncio
async def test_pinned_runner_liveness_false_when_runner_is_deleted():
    session = _FakeSession(_FakeRunner(status="deleted"))
    task = _pinned_awaiting_pull_task()
    assert await resident._pinned_runner_is_live(session, task) is False


@pytest.mark.asyncio
async def test_pinned_runner_liveness_true_for_a_recently_seen_runner():
    from app.models import _utcnow

    session = _FakeSession(_FakeRunner(last_seen_at=_utcnow()))
    task = _pinned_awaiting_pull_task()
    assert await resident._pinned_runner_is_live(session, task) is True


# --------------------------------------------------------------------------
# Surface REASONS, not states.
# --------------------------------------------------------------------------


def test_superseded_row_records_why_it_lost_and_which_generation_won():
    task = _pinned_awaiting_pull_task(context={"existing": "kept"})

    resident._mark_resident_task_superseded(
        task,
        now_iso=_iso(NOW),
        replacement_generation=178,
        reason=resident.RESIDENT_SUPERSEDE_PINNED_RUNNER_OFFLINE,
        replacement_session_id="sess-winner",
    )

    assert task.status == TaskStatus.CANCELLED
    ctx = task.context
    assert ctx["existing"] == "kept"
    assert ctx["_resident_superseded"] is True
    assert ctx["_resident_superseded_at"] == _iso(NOW)
    assert ctx["_resident_superseded_by_generation"] == 178
    assert ctx["_resident_superseded_by_session_id"] == "sess-winner"
    assert (
        ctx["_resident_superseded_reason"]
        == resident.RESIDENT_SUPERSEDE_PINNED_RUNNER_OFFLINE
    )

    # The operator-facing sentence must name the winner AND the cause.
    message = task.latest_status_message
    assert "178" in message
    assert "sess-winner" in message
    assert "offline" in message


def test_supersede_message_distinguishes_causes():
    """The pre-fix code hard-coded one sentence for every cause."""
    messages = set()
    for reason in (
        resident.RESIDENT_SUPERSEDE_WORKER_HEARTBEAT_STALE,
        resident.RESIDENT_SUPERSEDE_START_CLAIM_STALE,
        resident.RESIDENT_SUPERSEDE_PINNED_RUNNER_OFFLINE,
        resident.RESIDENT_SUPERSEDE_ORPHANED_IDENTITY,
        resident.RESIDENT_SUPERSEDE_WORKER_TASK_TERMINAL,
        resident.RESIDENT_SUPERSEDE_WORKER_TASK_MISSING,
    ):
        task = _pinned_awaiting_pull_task()
        resident._mark_resident_task_superseded(
            task,
            now_iso=_iso(NOW),
            replacement_generation=2,
            reason=reason,
            replacement_session_id="sess-b",
        )
        messages.add(task.latest_status_message)
    assert len(messages) == 6, "each cause needs its own operator-facing sentence"


def test_every_verdict_code_has_operator_facing_text():
    codes = {
        value
        for name, value in vars(resident).items()
        if name.startswith("RESIDENT_SUPERSEDE_") and isinstance(value, str)
    }
    assert codes, "reason codes must be discoverable as module constants"
    missing = codes - set(resident.RESIDENT_SUPERSEDE_REASON_TEXT)
    assert not missing, f"reason codes with no operator text: {sorted(missing)}"


# --------------------------------------------------------------------------
# Literal pins on the governance-relevant windows.
# --------------------------------------------------------------------------


def test_resident_staleness_windows_are_pinned_to_literals():
    """Literal pins -- never assert a constant against itself.

    Widening either window silently would mask a supersession regression rather
    than fix it, so both are pinned to the exact reviewed value.
    """
    assert resident._RESIDENT_START_CLAIM_STALE_SECONDS == 300
    assert resident._RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS == 180
    assert resident._RESIDENT_TASK_HEARTBEAT_CORROBORATION_GRACE_SECONDS == 120


def test_supersede_reason_codes_are_pinned_to_literals():
    assert resident.RESIDENT_SUPERSEDE_WORKER_TASK_MISSING == "worker_task_missing"
    assert resident.RESIDENT_SUPERSEDE_WORKER_TASK_TERMINAL == "worker_task_terminal"
    assert (
        resident.RESIDENT_SUPERSEDE_WORKER_HEARTBEAT_STALE
        == "worker_heartbeat_stale"
    )
    assert resident.RESIDENT_SUPERSEDE_START_CLAIM_STALE == "start_claim_stale"
    assert (
        resident.RESIDENT_SUPERSEDE_PINNED_RUNNER_OFFLINE == "pinned_runner_offline"
    )
    assert (
        resident.RESIDENT_SUPERSEDE_ORPHANED_IDENTITY
        == "orphaned_predecessor_identity"
    )
