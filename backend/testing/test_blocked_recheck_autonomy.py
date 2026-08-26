"""Regression coverage for BLOCKED-task rechecks under a held autonomy flag."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.blocked_task_unblock_sweeps import (
    _BLOCKED_BASE_STATE_RECHECK_CAP,
    _BLOCKED_ESCALATED_PARK,
    _BLOCKED_ESCALATED_RECHECK_MAX,
    _BLOCKED_PARKED_REEVAL_INTERVAL,
    _escalated_status_message,
    _next_recheck_delay,
    sweep_unblock_blocked_tasks,
)
from app.models import Project, Task, TaskStatus
from app.time_utils import _utcnow


@pytest.mark.asyncio
async def test_due_file_conflict_requeues_with_autonomous_spawning_disabled(
    db_session, mocker
):
    """Disabling new autonomous roots must not strand an existing task graph."""

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    now = _utcnow()
    conflicting_file = "agent/src/prompts/pr-helper.ts"
    project = Project(
        name="blocked-recheck-autonomy",
        user_id="blocked-recheck-user",
        autonomous_enabled=False,
        paused=False,
        archived=False,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    completed_writer = Task(
        project_id=project.id,
        title="completed conflicting writer",
        description="The writer already landed before the blocked recheck became due.",
        status=TaskStatus.COMPLETED,
        created_by="test-user",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"related_files": [conflicting_file]},
        status_log=[],
    )
    blocked = Task(
        project_id=project.id,
        title="blocked sibling",
        description="Must resume after the completed writer releases the file.",
        status=TaskStatus.BLOCKED,
        created_by="test-user",
        requires_approval=False,
        payloads=[{"type": "refactor"}],
        context={
            "related_files": [conflicting_file],
            "_precondition_defer": {
                "key": f"file conflict on {conflicting_file}",
                "count": 3,
                "files": [conflicting_file],
            },
        },
        status_log=[],
        blocker_kind="file_conflict",
        blocked_reason=(
            f"Waiting: file conflict on {conflicting_file} "
            "(blocked by: completed conflicting writer)"
        ),
        blocked_at=now - timedelta(minutes=40),
        recheck_after=now - timedelta(minutes=30),
    )
    db_session.add_all([completed_writer, blocked])
    await db_session.commit()

    requeued = await sweep_unblock_blocked_tasks(db_session, now)

    assert requeued == 1
    await db_session.refresh(blocked)
    assert blocked.status == TaskStatus.QUEUED
    assert blocked.blocker_kind is None
    assert blocked.recheck_after is None
    assert blocked.attempt_history[-1]["status"] == "BLOCKED"


# -- Structured base-state recheck: the one-way-latch fix ---------------------
# Live incident (2026-08-25, Blueprint task d58aa94c): an agent reported a
# precondition it could not clear, naming no machine-resolvable foundation. Both
# halves of that precondition were repaired on the integration branch within
# hours. Twenty hours later the task was still BLOCKED and still escalated to a
# human, because nothing in the engine could re-test the condition: the
# prose-matching gate revalidation path did not recognize the wording, and the
# need-resolver had no need to resolve. Elapsed time could cancel such a row
# (sibling task b215b3ac, "Cancelled - blocked need never became satisfiable")
# and elapsed time could escalate it, but nothing could ever CLEAR it.
#
# These tests pin BOTH directions of the now two-way latch: a satisfiable
# condition returns to QUEUED with no human action, and an unsatisfiable one
# still stops - bounded, escalated, and explaining what was re-checked.

_D58AA94C_BLOCKER_TEXT = (
    "BLOCKER: Publication gate cannot pass without unrelated artifact repairs: "
    "backend/.test_durations reports 250 new uncovered files relative to "
    "origin/automain, and backend/testing/check_size_limits.py is an inherited "
    "1806-line breach against cap 1800"
)


async def _make_resumable_project(db, name):
    project = Project(
        name=name,
        user_id="blocked-recheck-user",
        autonomous_enabled=True,
        paused=False,
        archived=False,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _base_state_blocked_task(db, project, now, *, context_extra=None, **kwargs):
    """A BLOCKED row shaped exactly like the live d58aa94c incident."""
    context = {
        "generated_head_sha": "a" * 40,
        "blocker": {
            "source": "agent",
            "waiting_on": "precondition",
            "reason": _D58AA94C_BLOCKER_TEXT,
            "blocker_kind": "precondition",
            "recheck": {
                "predicate": "base_state",
                "observed_at": (now - timedelta(days=1)).isoformat(),
                "baseline": "a" * 40,
            },
        },
    }
    context.update(context_extra or {})
    defaults = {
        "project_id": project.id,
        "title": "fix(ci): repo git reads flip verdict between CI and the harness",
        "description": "x",
        "status": TaskStatus.BLOCKED,
        "created_by": "test-user",
        "requires_approval": False,
        "payloads": [{"type": "create"}],
        "context": context,
        "status_log": [],
        "blocker_kind": "precondition",
        "blocked_reason": _D58AA94C_BLOCKER_TEXT,
        "blocked_at": now - timedelta(days=1),
        "recheck_after": now - timedelta(minutes=1),
    }
    defaults.update(kwargs)
    task = Task(**defaults)
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


def _patch_gate_state(mocker, *, head_sha, green=True):
    return mocker.patch(
        "app.gate_revalidation.resolve_gate_state",
        new_callable=AsyncMock,
        return_value={
            "head_sha": head_sha,
            "green_at_head": green,
            "checked_at_utc": _utcnow().isoformat(),
        },
    )


def test_live_incident_text_is_invisible_to_prose_gate_matching():
    """Premise pin: the prose path could not have recovered d58aa94c.

    ``is_gate_failure_blocked`` recognizes an inherited-base blocker by matching
    the agent's free text against two hardcoded phrase lists. The live wording
    hit neither, so the revalidation machinery that exists precisely for this
    blocker class never ran. The structural predicate does not depend on wording
    at all, which is why it is the fix.
    """
    from app.gate_revalidation import is_gate_failure_blocked

    task = Task(
        title="t",
        description="x",
        status=TaskStatus.BLOCKED,
        created_by="test-user",
        payloads=[{"type": "create"}],
        context={"blocker": {"source": "agent", "waiting_on": "precondition"}},
        status_log=[],
        blocker_kind="precondition",
        blocked_reason=_D58AA94C_BLOCKER_TEXT,
    )
    assert is_gate_failure_blocked(task) is False


def test_enter_blocked_state_records_base_state_predicate_for_needless_precondition():
    """The re-test predicate is recorded from blocker SHAPE, not from wording."""
    from app.task_blocked import BLOCKER_RECHECK_BASE_STATE, enter_blocked_state

    task = Task(
        title="t",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="test-user",
        payloads=[{"type": "create"}],
        context={"generated_head_sha": "A" * 40},
        status_log=[],
    )
    meta = enter_blocked_state(
        task,
        waiting_on="precondition",
        reason=_D58AA94C_BLOCKER_TEXT,
        source="agent",
        blocker_kind="precondition",
    )
    assert meta["recheck"]["predicate"] == BLOCKER_RECHECK_BASE_STATE
    # Baseline is frozen at block time so "advanced since the failure" is a
    # self-describing comparison rather than one inferred from timestamps.
    assert meta["recheck"]["baseline"] == "a" * 40


def test_enter_blocked_state_records_no_predicate_for_owned_blocker_shapes():
    """Shapes that already have a resolver keep their behaviour unchanged."""
    from app.task_blocked import build_need, enter_blocked_state

    def _fresh():
        return Task(
            title="t",
            description="x",
            status=TaskStatus.IN_PROGRESS,
            created_by="test-user",
            payloads=[{"type": "create"}],
            context={},
            status_log=[],
        )

    # Names a machine-resolvable foundation -> the need resolver owns it.
    with_need = enter_blocked_state(
        _fresh(),
        waiting_on="task",
        reason="waiting on the parent",
        source="agent",
        blocker_kind="upstream_dependency",
        need=build_need("task", "11111111-1111-1111-1111-111111111111"),
    )
    assert "recheck" not in with_need

    # Server-owned transient blocker -> blocked_retry_authority owns it.
    transient = enter_blocked_state(
        _fresh(),
        waiting_on="file conflict on a.py",
        reason="Waiting: file conflict on a.py",
        source="server",
        blocker_kind="file_conflict",
    )
    assert "recheck" not in transient


@pytest.mark.asyncio
async def test_escalated_precondition_returns_to_queued_when_base_advances(
    db_session, mocker
):
    """POSITIVE: the condition became satisfiable, so the task resumes itself.

    No operator touched anything: an escalated, need-less, prose-unrecognized
    precondition block is re-checked against a fresh integration base and
    requeued to re-evaluate the precondition there.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    _patch_gate_state(mocker, head_sha="b" * 40, green=True)
    now = _utcnow()
    project = await _make_resumable_project(db_session, "base-state-positive")
    task = await _base_state_blocked_task(
        db_session,
        project,
        now,
        context_extra={"_blocked_escalated": True},
    )

    requeued = await sweep_unblock_blocked_tasks(db_session, now)

    assert requeued == 1
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.blocker_kind is None
    # Bounded: the requeue consumed one of the three base-state slots, and the
    # consumed HEAD is recorded so the same base can never requeue it twice.
    assert task.context["_blocked_base_state_rechecks"] == 1
    assert task.context["_gate_revalidated_head"] == "b" * 40
    # The escalation marker is gone: the condition lifecycle ended, so the
    # operator-attention lifecycle must end with it.
    assert "_blocked_escalated" not in task.context
    assert "fresh integration base" in (task.latest_status_message or "")
    assert "(1/3)" in (task.latest_status_message or "")


@pytest.mark.asyncio
async def test_escalated_precondition_holds_when_base_has_not_advanced(
    db_session, mocker
):
    """NEGATIVE: nothing changed, so nothing is retried - and it says so."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    _patch_gate_state(mocker, head_sha="a" * 40, green=True)
    now = _utcnow()
    project = await _make_resumable_project(db_session, "base-state-unchanged")
    task = await _base_state_blocked_task(
        db_session,
        project,
        now,
        context_extra={"_blocked_escalated": True},
    )

    requeued = await sweep_unblock_blocked_tasks(db_session, now)

    assert requeued == 0
    await db_session.refresh(task)
    assert task.status == TaskStatus.BLOCKED
    assert task.context.get("_blocked_base_state_rechecks", 0) == 0
    assert task.context["_blocked_escalated"] is True
    message = task.latest_status_message or ""
    assert "Blocked (escalated" in message
    assert "re-checked 0/3" in message
    assert "integration base unchanged" in message
    # Symmetry: a row the sweep can actually re-test comes back on the parked
    # cadence, not the escalated known-hard backoff.
    #
    # This must be an EXACT pin, not an upper bound. The backoff alternative at
    # this point is ``_next_recheck_delay(1)`` = 10 minutes, which also sits
    # under the parked interval — so ``recheck_after <= now + 6h`` is satisfied
    # by BOTH branches and cannot fail when the parked-cadence branch is
    # deleted. Assert the two are distinguishable first, then pin the value.
    fallback = _next_recheck_delay(1, cap=_BLOCKED_ESCALATED_RECHECK_MAX)
    assert fallback < _BLOCKED_PARKED_REEVAL_INTERVAL - timedelta(hours=1), (
        "the parked cadence and the escalated backoff have converged; this "
        "assertion can no longer tell which branch ran"
    )
    assert task.recheck_after is not None
    assert abs(
        (task.recheck_after - (now + _BLOCKED_PARKED_REEVAL_INTERVAL)).total_seconds()
    ) <= 60


@pytest.mark.asyncio
async def test_base_state_recheck_is_bounded_and_still_escalates_when_exhausted(
    db_session, mocker
):
    """NEGATIVE: a green, advanced base does NOT retry past the budget.

    Bounded exhaustion is the whole reason this is safe to ship ON: a
    mis-classified blocker cannot quietly burn the fleet, and the row it leaves
    behind is escalated with a message naming how many bases were tried.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    _patch_gate_state(mocker, head_sha="c" * 40, green=True)
    now = _utcnow()
    project = await _make_resumable_project(db_session, "base-state-exhausted")
    task = await _base_state_blocked_task(
        db_session,
        project,
        now,
        context_extra={
            "_blocked_escalated": True,
            "_blocked_base_state_rechecks": _BLOCKED_BASE_STATE_RECHECK_CAP,
            # A row that consumed its budget necessarily carries the last base
            # it was requeued onto; the exhausted message reports that base.
            "_gate_revalidated_head": "b" * 40,
        },
    )

    requeued = await sweep_unblock_blocked_tasks(db_session, now)

    assert requeued == 0
    await db_session.refresh(task)
    assert task.status == TaskStatus.BLOCKED
    assert task.context["_blocked_escalated"] is True
    assert (
        task.context["_blocked_base_state_rechecks"]
        == _BLOCKED_BASE_STATE_RECHECK_CAP
    )
    message = task.latest_status_message or ""
    assert "automatic base-state recovery exhausted after 3/3" in message
    # ...and it names the base it actually tried. An exhausted row deliberately
    # spends no GitHub call, so the head comes from the last base it was
    # requeued onto; rendering "(last seen unknown)" would make the one string
    # whose job is to report what was tried report nothing.
    assert "(last seen bbbbbbbbbbbb)" in message
    # Exhaustion is not a tombstone: the row still carries a bounded next look,
    # on the escalated backoff and never the multi-year park sentinel.
    assert task.recheck_after is not None
    assert now < task.recheck_after <= now + _BLOCKED_ESCALATED_RECHECK_MAX + timedelta(
        minutes=1
    )
    assert task.recheck_after < now + _BLOCKED_ESCALATED_PARK - timedelta(days=1)


@pytest.mark.asyncio
async def test_non_escalated_precondition_also_recovers_on_a_fresh_base(
    db_session, mocker
):
    """The recheck is not escalation-only: it runs on the ordinary due path too."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    _patch_gate_state(mocker, head_sha="b" * 40, green=True)
    now = _utcnow()
    project = await _make_resumable_project(db_session, "base-state-live")
    task = await _base_state_blocked_task(db_session, project, now)

    requeued = await sweep_unblock_blocked_tasks(db_session, now)

    assert requeued == 1
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.context["_blocked_base_state_rechecks"] == 1


@pytest.mark.asyncio
async def test_base_state_recheck_fail_closes_when_the_fresh_base_is_not_green(
    db_session, mocker
):
    """NEGATIVE: an ADVANCED base that is not green must not requeue anything.

    This is the safety property that makes an automatic requeue defensible at
    all. The base-state predicate deliberately does not evaluate the blocked
    precondition itself - it hands the task back to its own gate on a fresh
    base - so the ONE thing it must get right is refusing to hand it back onto
    a base that is red or still pending. Nothing else in the sweep re-checks
    that, and a mis-fire here spends a real VM run per row per HEAD.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    _patch_gate_state(mocker, head_sha="b" * 40, green=False)
    now = _utcnow()
    project = await _make_resumable_project(db_session, "base-state-red-ci")
    task = await _base_state_blocked_task(
        db_session,
        project,
        now,
        context_extra={"_blocked_escalated": True},
    )

    requeued = await sweep_unblock_blocked_tasks(db_session, now)

    assert requeued == 0
    await db_session.refresh(task)
    assert task.status == TaskStatus.BLOCKED
    # A refused re-check spends no budget: the row keeps all three slots for a
    # base that is actually a healthy retry target.
    assert task.context.get("_blocked_base_state_rechecks", 0) == 0
    assert task.context["_blocked_escalated"] is True
    assert "_gate_revalidated_head" not in task.context
    message = task.latest_status_message or ""
    assert "not a healthy retry target" in message
    assert "bbbbbbbbbbbb" in message


@pytest.mark.asyncio
async def test_first_escalation_of_a_base_state_row_parks_on_the_low_cadence(
    db_session, mocker
):
    """The escalation path itself must honour the base-state predicate.

    ``_escalate_blocked_task`` chooses between the low parked cadence and the
    known-hard exponential backoff. A row the sweep can genuinely re-test
    belongs on the former for the same reason a resolvable need does. Without
    this test the entire ``_base_state_recheck_has_budget`` call site is
    unexercised - neutering it to ``return False`` leaves the rest of this file
    green.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    # Base unchanged, so the re-check holds and the row reaches escalation.
    _patch_gate_state(mocker, head_sha="a" * 40, green=True)
    now = _utcnow()
    project = await _make_resumable_project(db_session, "base-state-first-escalation")
    # Not yet escalated, but blocked_at is a day old - past the 6h deadline.
    task = await _base_state_blocked_task(db_session, project, now)
    assert "_blocked_escalated" not in task.context

    requeued = await sweep_unblock_blocked_tasks(db_session, now)

    assert requeued == 0
    await db_session.refresh(task)
    assert task.status == TaskStatus.BLOCKED
    assert task.context.get("_blocked_escalated") is True
    # Distinguishable from the backoff the same row would get without the
    # predicate: _next_recheck_delay(0) is 5 minutes, the parked interval 6h.
    fallback = _next_recheck_delay(0, cap=_BLOCKED_ESCALATED_RECHECK_MAX)
    assert fallback < _BLOCKED_PARKED_REEVAL_INTERVAL - timedelta(hours=1), (
        "the parked cadence and the escalated backoff have converged; this "
        "assertion can no longer tell which branch ran"
    )
    assert task.recheck_after is not None
    assert abs(
        (task.recheck_after - (now + _BLOCKED_PARKED_REEVAL_INTERVAL)).total_seconds()
    ) <= 60


@pytest.mark.asyncio
async def test_production_blocked_write_path_is_recovered_by_the_sweep(
    db_session, mocker
):
    """End-to-end pin: the REAL block call site produces a recoverable row.

    Every other test here either hand-builds the blocker dict or calls
    ``enter_blocked_state`` with an explicit ``blocker_kind='precondition'``.
    The production call site (``routers/agent_status.py``) passes no
    ``blocker_kind`` at all and lets ``_default_blocker_kind`` derive one from
    the agent's free text - a derivation that returns ``upstream_dependency``
    for wording that trips its marker list, in which case no predicate is
    recorded and this whole fix does nothing. The two halves were only ever
    verified apart; this joins them with the exact production argument shape.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    _patch_gate_state(mocker, head_sha="b" * 40, green=True)
    from sqlalchemy.orm.attributes import flag_modified

    from app.task_blocked import enter_blocked_state

    now = _utcnow()
    project = await _make_resumable_project(db_session, "base-state-production-path")
    task = Task(
        project_id=project.id,
        title="fix(ci): repo git reads flip verdict between CI and the harness",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="test-user",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"generated_head_sha": "a" * 40},
        status_log=[],
        latest_status_message=_D58AA94C_BLOCKER_TEXT,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Verbatim the call in routers/agent_status.py: no blocker_kind, no need.
    meta = enter_blocked_state(
        task,
        waiting_on=(task.context or {}).get("blocker_waiting_on") or "precondition",
        reason=task.latest_status_message or "Precondition not met",
        source="agent",
        need=None,
        publication_gate_receipt=None,
    )
    assert meta["blocker_kind"] == "precondition"
    assert meta["recheck"]["predicate"] == "base_state"
    flag_modified(task, "context")
    db_session.add(task)
    await db_session.commit()

    # Past the default recheck window the block itself armed.
    requeued = await sweep_unblock_blocked_tasks(db_session, now + timedelta(hours=1))

    assert requeued == 1
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.context["_blocked_base_state_rechecks"] == 1


def test_escalated_status_message_never_truncates_away_its_recheck_time():
    """Surfaced-reasons pin: the WHEN survives an over-long WHY.

    ``_escalated_status_message`` promises the operator both the blocker and
    the next automatic recheck time. It used to assemble the whole string and
    cut it at the ceiling, so a long detail - the base-state re-check clause
    prepended to a full agent blocker reason assembles to 520 characters -
    amputated the trailing timestamp and left "next automatic recheck at"
    dangling with no time after it. The budget belongs to the free text, never
    to the fixed tail.
    """
    recheck_at = _utcnow().replace(microsecond=0) + timedelta(hours=6)
    clause = (
        "automatic base-state recovery exhausted after 3/3 requeues onto fresh "
        "integration bases (last seen bbbbbbbbbbbb); the precondition did not "
        "clear on any of them"
    )
    detail = clause + " - " + _D58AA94C_BLOCKER_TEXT
    assert len(detail) > 300, "fixture no longer exercises the truncation path"
    message = _escalated_status_message("precondition", detail, recheck_at)
    # 500 is pinned literally: this is the ceiling the truncation bug hid behind.
    assert len(message) <= 500
    assert message.startswith("Blocked (escalated")
    assert "precondition" in message
    assert message.endswith(
        "next automatic recheck at "
        + recheck_at.strftime("%Y-%m-%d %H:%M UTC")
    )
    # The detail is what gets shortened, and it is still recognisable.
    assert "automatic base-state recovery exhausted after 3/3" in message
