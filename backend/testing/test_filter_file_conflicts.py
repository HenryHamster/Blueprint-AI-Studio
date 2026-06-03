"""Unit tests for filter_file_conflicts in orchestrator_filters.

Mirrors the test pattern in test_direction_health.TestFilterHeldDirections
for the sibling filter function in the same module.
"""

import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.models import TaskStatus
from app.orchestrator_filters import filter_file_conflicts
from testing.factories import make_task


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
        broadcast_fn = AsyncMock()

        # Three deferrals cross the threshold and re-block the task.
        for _ in range(3):
            await filter_file_conflicts([c.id], [c], [ip], session, broadcast_fn)

        assert c.status == TaskStatus.BLOCKED
        # blocked_at reflects the preserved onset (~7h ago), not a fresh now,
        # so now - blocked_at exceeds the escalation deadline.
        assert c.blocked_at == onset

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
