"""Unit tests for filter_file_conflicts in orchestrator_filters.

Mirrors the test pattern in test_direction_health.TestFilterHeldDirections
for the sibling filter function in the same module.
"""

import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.orm import load_only
from sqlmodel import select

from app.models import Task, TaskStatus
from app.orchestrator import ACTIVE_TASKS_LIGHT_COLUMNS
from app.orchestrator_filters import filter_file_conflicts
from testing.factories import make_alert, make_project, make_task


def _queued(project_id, files, scope="small", ttype="fix", parent=None):
    task = make_task(
        project_id=project_id,
        status=TaskStatus.QUEUED,
        context={"related_files": files, "scope": scope},
        payloads=[{"type": ttype}],
        parent_task_id=parent,
    )
    return task


def _in_progress(project_id, files, parent=None):
    return make_task(
        project_id=project_id,
        status=TaskStatus.IN_PROGRESS,
        context={"related_files": files},
        payloads=[{"type": "fix"}],
        parent_task_id=parent,
    )


class TestFilterFileConflicts:
    @pytest.mark.asyncio
    async def test_no_in_progress_passes_through(self):
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py"])
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [], session, broadcast_fn)
        assert result == [c.id]
        broadcast_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_candidate_without_related_files_passes_through(self):
        pid = uuid.uuid4()
        c = _queued(pid, [])
        ip = _in_progress(pid, ["a.py"])
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == [c.id]

    @pytest.mark.asyncio
    async def test_large_scope_deferred_on_single_file_overlap(self):
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py", "c.py"], scope="large")
        ip = _in_progress(pid, ["a.py"])
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == []
        assert "file conflict" in (c.latest_status_message or "")

    @pytest.mark.asyncio
    async def test_review_type_never_deferred_even_on_full_overlap(self):
        """A review is read-only — it posts a verdict, not a diff — so it can
        never conflict and must never be held behind in-flight writes (the
        2026-06-03 spurious-hold bug: Review PR #7411 blocked by Fix #7415 on
        hot-file overlap)."""
        pid = uuid.uuid4()
        # Full overlap + large scope: a WRITE task here would be deferred.
        c = _queued(pid, ["a.py", "b.py"], scope="large", ttype="review")
        ip = _in_progress(pid, ["a.py", "b.py"])
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == [c.id]
        assert "file conflict" not in (c.latest_status_message or "")

    @pytest.mark.asyncio
    async def test_plan_type_not_deferred_because_read_only(self):
        """plan is a read-only type (it produces a plan + spawns children, no PR),
        so it is exempt from the write-conflict gate. (Previously plan deferred on
        any overlap — that mis-applied a write-conflict gate to a no-diff task.)"""
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py"], scope="small", ttype="plan")
        ip = _in_progress(pid, ["a.py"])
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == [c.id]

    @pytest.mark.asyncio
    async def test_research_type_not_deferred_read_only(self):
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py", "c.py"], scope="large", ttype="research")
        ip = _in_progress(pid, ["a.py", "b.py", "c.py"])
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == [c.id]

    @pytest.mark.asyncio
    async def test_write_type_still_deferred_on_overlap(self):
        """Regression guard: WRITE types (fix here) are still gated — the
        exemption must not disable the conflict filter for code-producing work."""
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py"], scope="large", ttype="fix")
        ip = _in_progress(pid, ["a.py"])
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == []
        assert "file conflict" in (c.latest_status_message or "")

    @pytest.mark.asyncio
    async def test_cross_parent_overlap_not_deferred(self):
        """Two WRITE tasks under DIFFERENT parents (different rollups) overlapping
        a hot file are NOT held — they target different branches, so it's rarely a
        real conflict; the merge/back-merge resolver catches the rare one."""
        pid = uuid.uuid4()
        parent_a, parent_b = uuid.uuid4(), uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py", "c.py", "d.py"], scope="small", parent=parent_a)
        ip = _in_progress(pid, ["a.py", "b.py"], parent=parent_b)
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == [c.id]
        assert "file conflict" not in (c.latest_status_message or "")

    @pytest.mark.asyncio
    async def test_same_parent_overlap_still_deferred(self):
        """Same-parent siblings (one plan's coordinated decomposition) merge to the
        same rollup branch — keep gating their file overlap."""
        pid = uuid.uuid4()
        parent = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py", "c.py", "d.py"], scope="small", parent=parent)
        ip = _in_progress(pid, ["a.py", "b.py"], parent=parent)
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == []
        assert "file conflict" in (c.latest_status_message or "")

    @pytest.mark.asyncio
    async def test_large_scope_cross_parent_still_deferred(self):
        """Large-scope tasks (wide rewrites) conflict broadly even across rollups —
        the cross-parent looseness must NOT exempt them."""
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py"], scope="large", parent=uuid.uuid4())
        ip = _in_progress(pid, ["a.py"], parent=uuid.uuid4())
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == []
        assert "file conflict" in (c.latest_status_message or "")

    @pytest.mark.asyncio
    async def test_null_parent_overlap_stays_conservative_and_defers(self):
        """Missing parent on either side → can't confirm independence → keep the
        hold (conservative)."""
        pid = uuid.uuid4()
        # candidate has a parent, blocker does not → not confidently cross-parent.
        c = _queued(pid, ["a.py", "b.py", "c.py", "d.py"], scope="small", parent=uuid.uuid4())
        ip = _in_progress(pid, ["a.py", "b.py"], parent=None)
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == []

    @pytest.mark.asyncio
    async def test_standard_not_deferred_on_single_file_overlap(self):
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py", "c.py"], scope="small")
        ip = _in_progress(pid, ["a.py"])
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == [c.id]

    @pytest.mark.asyncio
    async def test_standard_deferred_when_two_files_overlap_at_half(self):
        pid = uuid.uuid4()
        # 2 of 4 overlap = 50%, meets 2+ AND >= 50% threshold
        c = _queued(pid, ["a.py", "b.py", "c.py", "d.py"], scope="small")
        ip = _in_progress(pid, ["a.py", "b.py"])
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == []

    @pytest.mark.asyncio
    async def test_project_isolation(self):
        pid1 = uuid.uuid4()
        pid2 = uuid.uuid4()
        c = _queued(pid1, ["a.py"], scope="large")
        ip = _in_progress(pid2, ["a.py"])
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == [c.id]

    @pytest.mark.asyncio
    async def test_status_message_and_broadcast_on_deferral(self):
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py"], scope="large")
        ip = _in_progress(pid, ["a.py"])
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == []
        assert c.latest_status_message is not None
        assert "file conflict" in c.latest_status_message
        session.add.assert_called()
        broadcast_fn.assert_called_once()
        args, _ = broadcast_fn.call_args
        assert args[0] == str(pid)
        assert args[1] == "task_updated"
        assert args[2]["task_id"] == str(c.id)
        assert args[2]["status"] == "QUEUED"

    @pytest.mark.asyncio
    async def test_single_defer_does_not_escalate(self, mocker):
        esc = mocker.patch(
            "app.alert_dispatch._maybe_create_precondition_blocker_alert",
            new_callable=AsyncMock,
        )
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py"], scope="large")
        ip = _in_progress(pid, ["a.py"])
        session = MagicMock()
        broadcast_fn = AsyncMock()

        await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        esc.assert_not_called()
        # Persistence counter is seeded but below the escalation threshold.
        assert c.context["_precondition_defer"]["count"] == 1

    @pytest.mark.asyncio
    async def test_repeated_defer_on_same_blocker_escalates_once(self, mocker):
        esc = mocker.patch(
            "app.alert_dispatch._maybe_create_precondition_blocker_alert",
            new_callable=AsyncMock,
        )
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py"], scope="large")
        ip = _in_progress(pid, ["a.py"])
        session = MagicMock()
        session.commit = AsyncMock()
        broadcast_fn = AsyncMock()

        # Defer across several dispatch cycles on the SAME blocker identity.
        for _ in range(5):
            await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)

        # Escalated exactly once at the threshold, not re-fired every cycle.
        assert esc.call_count == 1
        _, kwargs = esc.call_args
        assert kwargs["blocker_kind"] == "file conflict on a.py"
        assert c.context["_precondition_escalated_key"] == "file conflict on a.py"

    @pytest.mark.asyncio
    async def test_changing_blocker_resets_counter(self, mocker):
        esc = mocker.patch(
            "app.alert_dispatch._maybe_create_precondition_blocker_alert",
            new_callable=AsyncMock,
        )
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py"], scope="large")
        session = MagicMock()
        broadcast_fn = AsyncMock()

        ip_a = _in_progress(pid, ["a.py"])
        await filter_file_conflicts([c.id], [c], [ip_a], session, broadcast_fn)
        await filter_file_conflicts([c.id], [c], [ip_a], session, broadcast_fn)
        assert c.context["_precondition_defer"]["count"] == 2

        # Blocker identity shifts to a different file → counter resets to 1.
        ip_b = _in_progress(pid, ["b.py"])
        await filter_file_conflicts([c.id], [c], [ip_b], session, broadcast_fn)
        assert c.context["_precondition_defer"]["key"] == "file conflict on b.py"
        assert c.context["_precondition_defer"]["count"] == 1
        # Counter never reached the threshold, so no escalation fired.
        esc.assert_not_called()

    @pytest.mark.asyncio
    async def test_persistent_conflict_parks_task_in_blocked(self, mocker):
        """At the escalation threshold the candidate is parked in BLOCKED with
        durable blocker metadata so the queue path stops re-judging it."""
        mocker.patch(
            "app.alert_dispatch._maybe_create_precondition_blocker_alert",
            new_callable=AsyncMock,
        )
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py"], scope="large")
        ip = _in_progress(pid, ["a.py"])
        session = MagicMock()
        session.commit = AsyncMock()
        broadcast_fn = AsyncMock()

        # Below the threshold the task stays QUEUED (just deferred this cycle).
        await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert c.status == TaskStatus.QUEUED
        assert c.blocker_kind is None

        # Cross the threshold (default 3) → parked in BLOCKED with metadata.
        await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert c.status == TaskStatus.BLOCKED
        assert c.blocker_kind == "file_conflict"
        assert c.blocked_reason and "file conflict" in c.blocked_reason
        assert c.blocked_at is not None
        assert c.recheck_after is not None and c.recheck_after > c.blocked_at
        assert (c.latest_status_message or "").startswith("Blocked:")

    @pytest.mark.asyncio
    async def test_broadcast_emitted_on_blocked_transition(self, mocker):
        """When parking a candidate into BLOCKED on the escalation threshold,
        emit a task_updated broadcast carrying BLOCKED status so the UI reflects
        the committed state immediately."""
        mocker.patch(
            "app.alert_dispatch._maybe_create_precondition_blocker_alert",
            new_callable=AsyncMock,
        )
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py"], scope="large")
        ip = _in_progress(pid, ["a.py"])
        session = MagicMock()
        session.commit = AsyncMock()
        broadcast_fn = AsyncMock()

        # First deferral: broadcasts QUEUED
        await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert c.status == TaskStatus.QUEUED
        broadcast_fn.assert_called_once()
        args, _ = broadcast_fn.call_args
        assert args[2]["status"] == "QUEUED"
        broadcast_fn.reset_mock()

        # Second deferral: message unchanged, so no QUEUED broadcast (line 406 guard).
        await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert c.status == TaskStatus.QUEUED  # Still QUEUED at count=2
        broadcast_fn.assert_not_called()

        # Third deferral crosses the threshold — now parked in BLOCKED.
        # Broadcasts BLOCKED on the transition.
        await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert c.status == TaskStatus.BLOCKED
        broadcast_fn.assert_called_once()
        args, _ = broadcast_fn.call_args
        assert args[0] == str(pid)
        assert args[1] == "task_updated"
        assert args[2]["task_id"] == str(c.id)
        assert args[2]["status"] == "BLOCKED"
        assert "Blocked:" in args[2]["message"]

    @pytest.mark.asyncio
    async def test_blocked_at_onset_preserved_across_reblock(self, mocker):
        """When a same-identity onset stamp survives an optimistic requeue, a
        re-block measures blocked_at from the original onset — not a fresh now —
        so the 6h deadline escalation can actually fire for a stuck conflict."""
        from datetime import datetime, timedelta

        mocker.patch(
            "app.alert_dispatch._maybe_create_precondition_blocker_alert",
            new_callable=AsyncMock,
        )
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py"], scope="large")
        # Simulate the state after an optimistic requeue of a long-standing
        # conflict: _precondition_defer wiped, but the onset stamp preserved.
        onset = datetime.fromisoformat(
            (datetime.now().astimezone() - timedelta(hours=7)).isoformat()
        )
        c.context["_blocked_first_at"] = {
            "key": "file conflict on a.py",
            "at": onset.isoformat(),
        }
        ip = _in_progress(pid, ["a.py"])
        session = MagicMock()
        session.commit = AsyncMock()
        broadcast_fn = AsyncMock()

        # Three deferrals cross the threshold and re-block the task.
        for _ in range(3):
            await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)

        assert c.status == TaskStatus.BLOCKED
        # blocked_at reflects the preserved onset (~7h ago), not a fresh now,
        # so now - blocked_at exceeds the escalation deadline.
        assert c.blocked_at == onset

    @pytest.mark.asyncio
    async def test_reblock_with_existing_alert_broadcasts_after_commit(
        self, db_session, mocker
    ):
        """When the same blocker reoccurs after its alert already exists, the
        helper must commit the parked task and then broadcast BLOCKED."""
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        pid = uuid.uuid4()
        project = make_project(id=pid)
        db_session.add(project)
        await db_session.commit()

        c = _queued(pid, ["a.py", "b.py"], scope="large")
        ip = _in_progress(pid, ["a.py"])
        db_session.add(c)
        db_session.add(ip)
        await db_session.commit()
        await db_session.refresh(c)
        await db_session.refresh(ip)

        existing = make_alert(
            project_id=pid,
            task_id=c.id,
            severity="WARNING",
            title="Precondition blocker: file conflict on a.py",
            message="Waiting: file conflict on a.py",
        )
        db_session.add(existing)
        await db_session.commit()

        broadcast_fn = AsyncMock()

        await filter_file_conflicts([c.id], [c], [ip], db_session, broadcast_fn)
        assert broadcast_fn.call_count == 1
        args, _ = broadcast_fn.call_args
        assert args[2]["status"] == "QUEUED"
        broadcast_fn.reset_mock()

        await filter_file_conflicts([c.id], [c], [ip], db_session, broadcast_fn)
        await filter_file_conflicts([c.id], [c], [ip], db_session, broadcast_fn)

        await db_session.refresh(c)
        assert c.status == TaskStatus.BLOCKED
        broadcast_fn.assert_called_once()
        args, _ = broadcast_fn.call_args
        assert args[0] == str(pid)
        assert args[1] == "task_updated"
        assert args[2]["task_id"] == str(c.id)
        assert args[2]["status"] == "BLOCKED"
        assert "Blocked:" in args[2]["message"]

    @pytest.mark.asyncio
    async def test_new_alert_path_caller_commits_then_broadcasts_blocked(
        self, mocker
    ):
        """Newly-created-alert path: the CALLER commits the parked task itself
        (it no longer relies on the alert helper's side-effect commit) and then
        broadcasts BLOCKED. The mocked helper returns True (alert created); under
        the OLD contract a truthy return meant the caller skipped session.commit
        entirely, so asserting commit was awaited pins the new explicit contract."""
        esc = mocker.patch(
            "app.alert_dispatch._maybe_create_precondition_blocker_alert",
            new_callable=AsyncMock,
            return_value=True,
        )
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py"], scope="large")
        ip = _in_progress(pid, ["a.py"])
        session = MagicMock()
        session.commit = AsyncMock()
        broadcast_fn = AsyncMock()

        # Three deferrals cross the threshold and park the task in BLOCKED.
        for _ in range(3):
            await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)

        assert c.status == TaskStatus.BLOCKED
        # Caller owns the commit (exactly once, on the escalation cycle).
        session.commit.assert_awaited_once()
        # Operator alert still raised, after the durable commit.
        esc.assert_awaited_once()
        blocked_calls = [
            cc
            for cc in broadcast_fn.call_args_list
            if cc.args[2].get("status") == "BLOCKED"
        ]
        assert len(blocked_calls) == 1
        assert "Blocked:" in blocked_calls[0].args[2]["message"]

    @pytest.mark.asyncio
    async def test_failed_commit_suppresses_blocked_broadcast(self, mocker):
        """Failed-commit path: when the BLOCKED parking commit raises, the
        transition did not persist, so NO task_updated BLOCKED event may escape
        the aborted transaction — the UI must never show a phantom BLOCKED. The
        operator alert is gated behind the (failed) commit too."""
        esc = mocker.patch(
            "app.alert_dispatch._maybe_create_precondition_blocker_alert",
            new_callable=AsyncMock,
        )
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py"], scope="large")
        ip = _in_progress(pid, ["a.py"])
        session = MagicMock()
        session.commit = AsyncMock(side_effect=RuntimeError("db down"))
        broadcast_fn = AsyncMock()

        # First two cycles stay QUEUED (count < threshold → no commit attempted).
        await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert c.status == TaskStatus.QUEUED
        broadcast_fn.reset_mock()

        # Third cycle crosses the threshold: status flips to BLOCKED in memory,
        # the commit raises, and the exception propagates — the BLOCKED broadcast
        # is never reached.
        with pytest.raises(RuntimeError):
            await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)

        assert c.status == TaskStatus.BLOCKED  # mutated in memory only
        session.commit.assert_awaited_once()  # commit was attempted...
        # ...but no BLOCKED task_updated event was emitted (txn did not persist).
        assert all(
            cc.args[2].get("status") != "BLOCKED"
            for cc in broadcast_fn.call_args_list
        )
        # Alert helper sits AFTER the commit, so a failed commit never alerts.
        esc.assert_not_called()

    @pytest.mark.asyncio
    async def test_shared_test_files_not_deferred(self):
        pid = uuid.uuid4()
        c = _queued(pid, ["backend/testing/test_foo.py", "backend/testing/test_bar.py"])
        ip = _in_progress(
            pid, ["backend/testing/test_foo.py", "backend/testing/test_bar.py"]
        )
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == [c.id]
        broadcast_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_prod_overlap_still_defers_with_mixed_files(self):
        pid = uuid.uuid4()
        c = _queued(pid, ["app/a.py", "app/b.py", "testing/test_a.py"])
        ip = _in_progress(pid, ["app/a.py", "app/b.py", "testing/test_a.py"])
        session = MagicMock()
        broadcast_fn = AsyncMock()

        result = await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)
        assert result == []

    @pytest.mark.asyncio
    async def test_active_tasks_loaded_with_orchestrator_load_only_no_lazy_load(
        self, db_session
    ):
        """Regression: the cross-parent check must not sync-lazy-load parent_task_id.

        The dispatch loop fetches active (IN_PROGRESS) tasks with the
        ACTIVE_TASKS_LIGHT_COLUMNS load_only set. filter_file_conflicts' cross-
        parent looseness branch reads ``t.parent_task_id`` on the in-progress
        blockers, so parent_task_id MUST be in that set — otherwise the access
        is a sync IO load from async context (MissingGreenlet) that crashes the
        ENTIRE dispatch cycle (prod 2026-06-03, 10x; introduced by PR #7431).
        Importing the constant means if someone drops Task.parent_task_id from
        it, this test re-breaks. The unit tests above can't catch it: their
        make_task objects aren't load_only-deferred ORM instances.
        """
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        # Real parent rows: parent_task_id carries a FK (fk_task_parent_task_id),
        # so the blockers must reference existing tasks (different parents = two
        # different rollups).
        parent_a = make_task(project_id=project.id, status=TaskStatus.EVALUATING)
        parent_b = make_task(project_id=project.id, status=TaskStatus.EVALUATING)
        db_session.add_all([parent_a, parent_b])
        await db_session.commit()

        # Significant overlap (2/4 files, ≥50%) so the deferral path is reached,
        # but cross-parent so the looseness branch (which reads parent_task_id)
        # decides NOT to defer.
        ip = make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            context={"related_files": ["a.py", "b.py"]},
            payloads=[{"type": "fix"}],
            parent_task_id=parent_b.id,
        )
        cand = make_task(
            project_id=project.id,
            status=TaskStatus.QUEUED,
            context={"related_files": ["a.py", "b.py", "c.py", "d.py"], "scope": "small"},
            payloads=[{"type": "fix"}],
            parent_task_id=parent_a.id,
        )
        db_session.add_all([ip, cand])
        await db_session.commit()
        db_session.expunge_all()

        # Active tasks: load EXACTLY as the dispatch loop does.
        active = (
            (
                await db_session.execute(
                    select(Task)
                    .where(
                        Task.status.in_(
                            [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
                        )
                    )
                    .options(load_only(*ACTIVE_TASKS_LIGHT_COLUMNS))
                )
            )
            .scalars()
            .all()
        )
        candidates = (
            (
                await db_session.execute(
                    select(Task).where(Task.status == TaskStatus.QUEUED)
                )
            )
            .scalars()
            .all()
        )

        # Reading active[*].parent_task_id inside the cross-parent branch must not
        # raise MissingGreenlet; cross-parent overlap is not deferred.
        result = await filter_file_conflicts(
            [c.id for c in candidates], candidates, active, db_session, AsyncMock()
        )
        assert result == [cand.id]
