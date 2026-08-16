"""Unit tests for filter_rollup_conflicts in orchestrator_filters.

Covers the rollup-deferral filter, focusing on the sibling-exemption rule:
tasks that are direct children of the EVALUATING rollup parent (e.g., review
or fix tasks spawned for sibling PRs) must NOT be deferred against that
rollup, because they ARE the rollup pipeline — blocking them deadlocks the
rollup it depends on.
"""

import uuid
import pytest
from unittest.mock import AsyncMock

from app.models import Project, Task, TaskStatus
from app.orchestrator_filters import filter_rollup_conflicts
from testing.factories import make_alert


async def _mk_project(session, name="Rollup Filter Test"):
    project = Project(name=name, user_id="test-user")
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _mk_task(
    session,
    project_id,
    *,
    status,
    payloads,
    context=None,
    parent_task_id=None,
    title=None,
):
    task = Task(
        project_id=project_id,
        title=title or f"task-{uuid.uuid4().hex[:8]}",
        payloads=payloads,
        status=status,
        parent_task_id=parent_task_id,
        context=context or {},
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


# ══════════════════════════════════════════════════════════════════════════════
# Regression: sibling review/fix tasks must NOT be deferred by their own rollup
# ══════════════════════════════════════════════════════════════════════════════


class TestRollupFilterSiblingExemption:
    """The dispatch filter must not defer a review/fix task against the very
    rollup it belongs to. Observed incident: a research task parked in
    EVALUATING with a pending rollup had a COMPLETED refactor child whose
    files (``admin_analytics.py``, ``test_admin_analytics.py``) matched the
    review task spawned to review that same refactor's PR. The filter saw
    100% file overlap and deferred the review with "overlaps pending rollup"
    — which then blocked the rollup because the rollup can't finalize until
    its refactor child is reviewed.
    """

    @pytest.mark.asyncio
    async def test_review_for_sibling_pr_not_deferred(self, db_session):
        """A review task whose parent is the EVALUATING rollup parent must pass
        through the filter, even when its ``related_files`` fully overlap the
        pending-rollup file set (which they will, since the review targets a
        sibling's PR touching the same files)."""
        broadcast_fn = AsyncMock()
        project = await _mk_project(db_session, "Review Exemption")

        rollup_parent = await _mk_task(
            db_session,
            project.id,
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "research"}],
            title="Research: admin_analytics refactor",
        )
        overlapping_files = [
            "backend/app/routers/admin_analytics.py",
            "backend/testing/test_admin_analytics.py",
        ]
        # Sibling refactor child that fed the rollup.
        await _mk_task(
            db_session,
            project.id,
            status=TaskStatus.COMPLETED,
            parent_task_id=rollup_parent.id,
            payloads=[{"type": "refactor"}],
            context={"scope": "medium", "related_files": overlapping_files},
            title="refactor: extract orchestrator error routes",
        )
        # Review task for that refactor's PR — sibling under the same rollup parent.
        review_task = await _mk_task(
            db_session,
            project.id,
            status=TaskStatus.QUEUED,
            parent_task_id=rollup_parent.id,
            payloads=[
                {
                    "type": "review",
                    "pr_number": 2063,
                    "base_branch": f"research/{str(rollup_parent.id)[:8]}",
                }
            ],
            context={"related_files": overlapping_files},
            title="Review PR #2063",
        )

        result = await filter_rollup_conflicts(
            [review_task.id], [review_task], db_session, broadcast_fn
        )

        assert result == [review_task.id], (
            "Review task whose parent is the EVALUATING rollup parent must NOT "
            "be deferred by that rollup's own pending-files set — it's part of "
            "the rollup pipeline, not external work blocked by it. Deferring it "
            "deadlocks the rollup, since the rollup can't advance until this "
            "review signs off on the sibling's PR."
        )
        assert "overlaps pending rollup" not in (
            review_task.latest_status_message or ""
        ), (
            f"Sibling review must not be marked with the rollup-deferral status "
            f"message, got {review_task.latest_status_message!r}"
        )
        broadcast_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_external_task_still_deferred(self, db_session):
        """Counterpart: a QUEUED task that is NOT a child of the rollup parent
        and overlaps the pending-rollup files is still deferred. Protects
        against the sibling-exemption guard being widened too far."""
        broadcast_fn = AsyncMock()
        project = await _mk_project(db_session, "External Defer")

        rollup_parent = await _mk_task(
            db_session,
            project.id,
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "research"}],
            title="Research: module decomposition",
        )
        files = ["backend/app/main.py", "backend/app/models.py", "backend/app/admin.py"]
        await _mk_task(
            db_session,
            project.id,
            status=TaskStatus.COMPLETED,
            parent_task_id=rollup_parent.id,
            payloads=[{"type": "refactor"}],
            context={"scope": "large", "related_files": files},
            title="Refactor: decompose module",
        )
        # External task — NO parent link to rollup_parent, files fully overlap.
        external = await _mk_task(
            db_session,
            project.id,
            status=TaskStatus.QUEUED,
            parent_task_id=None,
            payloads=[{"type": "create"}],
            context={"related_files": files},
            title="Unrelated feature that touches the same files",
        )

        result = await filter_rollup_conflicts(
            [external.id], [external], db_session, broadcast_fn
        )

        assert result == [], (
            "External task overlapping 3/3 files with the pending rollup must "
            "still be deferred — the sibling-exemption guard only applies to "
            "direct children of the EVALUATING rollup parent."
        )
        # Inspect the in-memory candidate — the filter mutates it and adds it
        # to the session but does not commit (the caller is responsible for
        # that), so a refresh would pull the pre-mutation row.
        assert "overlaps pending rollup" in (external.latest_status_message or ""), (
            f"External task deferral must set the rollup-overlap status message, "
            f"got {external.latest_status_message!r}"
        )

    @pytest.mark.asyncio
    async def test_sibling_with_no_overlap_still_passes(self, db_session):
        """Sanity: a sibling with disjoint files passes through — not because
        of the exemption, but because the base filter doesn't fire."""
        broadcast_fn = AsyncMock()
        project = await _mk_project(db_session, "Sibling No Overlap")

        rollup_parent = await _mk_task(
            db_session,
            project.id,
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "research"}],
        )
        await _mk_task(
            db_session,
            project.id,
            status=TaskStatus.COMPLETED,
            parent_task_id=rollup_parent.id,
            payloads=[{"type": "refactor"}],
            context={
                "scope": "large",
                "related_files": ["backend/app/main.py", "backend/app/admin.py"],
            },
        )
        sibling = await _mk_task(
            db_session,
            project.id,
            status=TaskStatus.QUEUED,
            parent_task_id=rollup_parent.id,
            payloads=[{"type": "fix", "pr_number": 9000}],
            context={"related_files": ["agent/src/runner.ts"]},
        )

        result = await filter_rollup_conflicts(
            [sibling.id], [sibling], db_session, broadcast_fn
        )
        assert result == [sibling.id]

    @pytest.mark.asyncio
    async def test_foreign_child_link_cannot_manufacture_rollup_reservation(
        self, db_session
    ):
        """A child/project mismatch is not rollup authority in either project."""

        broadcast_fn = AsyncMock()
        local = await _mk_project(db_session, "Local Rollup Parent")
        foreign = await _mk_project(db_session, "Foreign Linked Child")
        local_parent = await _mk_task(
            db_session,
            local.id,
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "research"}],
        )
        files = ["backend/app/a.py", "backend/app/b.py"]
        await _mk_task(
            db_session,
            foreign.id,
            status=TaskStatus.COMPLETED,
            parent_task_id=local_parent.id,
            payloads=[{"type": "refactor"}],
            context={"scope": "large", "related_files": files},
            title="inconsistent foreign child",
        )
        candidate = await _mk_task(
            db_session,
            foreign.id,
            status=TaskStatus.QUEUED,
            payloads=[{"type": "create"}],
            context={"related_files": files},
        )

        result = await filter_rollup_conflicts(
            [candidate.id], [candidate], db_session, broadcast_fn
        )

        assert result == [candidate.id]
        broadcast_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_foreign_parent_link_does_not_grant_sibling_exemption(
        self, db_session
    ):
        """Sibling exemption requires the candidate and parent project to match."""

        broadcast_fn = AsyncMock()
        local = await _mk_project(db_session, "Foreign Parent Project")
        foreign = await _mk_project(db_session, "Real Rollup Project")
        foreign_linked_parent = await _mk_task(
            db_session,
            local.id,
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "research"}],
        )
        real_parent = await _mk_task(
            db_session,
            foreign.id,
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "research"}],
        )
        files = ["backend/app/a.py", "backend/app/b.py"]
        await _mk_task(
            db_session,
            foreign.id,
            status=TaskStatus.COMPLETED,
            parent_task_id=real_parent.id,
            payloads=[{"type": "refactor"}],
            context={"scope": "large", "related_files": files},
            title="real foreign rollup child",
        )
        candidate = await _mk_task(
            db_session,
            foreign.id,
            status=TaskStatus.QUEUED,
            parent_task_id=foreign_linked_parent.id,
            payloads=[{"type": "create"}],
            context={"related_files": files},
        )

        result = await filter_rollup_conflicts(
            [candidate.id], [candidate], db_session, broadcast_fn
        )

        assert result == []
        assert "overlaps pending rollup" in (
            candidate.latest_status_message or ""
        )


# ══════════════════════════════════════════════════════════════════════════════
# BLOCKED deferral durability — pending-rollup caller family
# ══════════════════════════════════════════════════════════════════════════════


class TestRollupDeferralDurability:
    """The BLOCKED deferral durability contract for the pending-rollup caller
    family, mirroring the file-conflict family (test_filter_file_conflicts):
    at the escalation threshold the shared helper commits the BLOCKED parking
    BEFORE alerting/broadcasting, and a task_updated BLOCKED event is emitted
    iff that transition persisted (never on an aborted/rolled-back commit).
    """

    async def _setup(self, db_session, *, title="Refactor: decompose module"):
        """Build a pending-rollup conflict: an EVALUATING rollup parent with a
        COMPLETED large-scope refactor child, and an EXTERNAL QUEUED candidate
        whose files significantly overlap the pending-rollup set (so the base
        rollup filter fires every cycle)."""
        project = await _mk_project(db_session, "Rollup Durability")
        rollup_parent = await _mk_task(
            db_session,
            project.id,
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "research"}],
            title="Research: decomposition",
        )
        files = [
            "backend/app/a.py",
            "backend/app/b.py",
            "backend/app/c.py",
            "backend/app/d.py",
        ]
        await _mk_task(
            db_session,
            project.id,
            status=TaskStatus.COMPLETED,
            parent_task_id=rollup_parent.id,
            payloads=[{"type": "refactor"}],
            context={"scope": "large", "related_files": files},
            title=title,
        )
        candidate = await _mk_task(
            db_session,
            project.id,
            status=TaskStatus.QUEUED,
            parent_task_id=None,
            payloads=[{"type": "create"}],
            context={"related_files": files[:2]},
            title="External feature on the same files",
        )
        return project, candidate, title

    @pytest.mark.asyncio
    async def test_persistent_rollup_conflict_parks_task_in_blocked(
        self, db_session, mocker
    ):
        """At the escalation threshold the candidate is parked in BLOCKED with
        ``block_code='pending_rollup'`` and a BLOCKED broadcast is emitted."""
        mocker.patch(
            "app.alert_dispatch._maybe_create_precondition_blocker_alert",
            new_callable=AsyncMock,
        )
        _project, candidate, _title = await self._setup(db_session)
        broadcast_fn = AsyncMock()

        # Below the threshold the task stays QUEUED (deferred this cycle only).
        await filter_rollup_conflicts(
            [candidate.id], [candidate], db_session, broadcast_fn
        )
        assert candidate.status == TaskStatus.QUEUED
        assert candidate.blocker_kind is None

        # Cross the threshold (default 3) → parked BLOCKED with durable metadata.
        await filter_rollup_conflicts(
            [candidate.id], [candidate], db_session, broadcast_fn
        )
        await filter_rollup_conflicts(
            [candidate.id], [candidate], db_session, broadcast_fn
        )
        assert candidate.status == TaskStatus.BLOCKED
        assert candidate.blocker_kind == "pending_rollup"
        assert candidate.blocked_reason and "pending rollup" in candidate.blocked_reason
        assert candidate.blocked_at is not None
        assert (
            candidate.recheck_after is not None
            and candidate.recheck_after > candidate.blocked_at
        )
        # The transition persisted (real db_session.commit), so the BLOCKED
        # broadcast fired exactly once with the parked state.
        blocked_calls = [
            cc
            for cc in broadcast_fn.call_args_list
            if cc.args[2].get("status") == "BLOCKED"
        ]
        assert len(blocked_calls) == 1
        assert blocked_calls[0].args[0] == str(candidate.project_id)
        assert blocked_calls[0].args[1] == "task_updated"
        assert blocked_calls[0].args[2]["task_id"] == str(candidate.id)
        assert "Blocked:" in blocked_calls[0].args[2]["message"]

    @pytest.mark.asyncio
    async def test_reblock_with_existing_alert_broadcasts_after_commit(
        self, db_session, mocker
    ):
        """Existing-alert path: when the operator alert for this blocker already
        exists, the helper returns False (no new alert/commit). The caller has
        already committed the parked task itself, so BLOCKED is still broadcast
        — durability no longer depends on the alert helper committing."""
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project, candidate, title = await self._setup(db_session)
        existing = make_alert(
            project_id=project.id,
            task_id=candidate.id,
            severity="WARNING",
            title=f"Precondition blocker: pending rollup '{title[:60]}'",
            message="Waiting: overlaps pending rollup",
        )
        db_session.add(existing)
        await db_session.commit()
        broadcast_fn = AsyncMock()

        for _ in range(3):
            await filter_rollup_conflicts(
                [candidate.id], [candidate], db_session, broadcast_fn
            )

        await db_session.refresh(candidate)
        assert candidate.status == TaskStatus.BLOCKED
        blocked_calls = [
            cc
            for cc in broadcast_fn.call_args_list
            if cc.args[2].get("status") == "BLOCKED"
        ]
        assert len(blocked_calls) == 1
        assert "Blocked:" in blocked_calls[0].args[2]["message"]

    @pytest.mark.asyncio
    async def test_failed_commit_suppresses_blocked_broadcast(
        self, db_session, mocker
    ):
        """Failed-commit path: when the BLOCKED parking commit raises, the
        transition did not persist, so NO BLOCKED task_updated event may escape
        the aborted transaction, and the operator alert (which sits after the
        commit) never fires."""
        esc = mocker.patch(
            "app.alert_dispatch._maybe_create_precondition_blocker_alert",
            new_callable=AsyncMock,
        )
        _project, candidate, _title = await self._setup(db_session)
        broadcast_fn = AsyncMock()

        # First two cycles stay QUEUED (count < threshold → no commit attempted).
        await filter_rollup_conflicts(
            [candidate.id], [candidate], db_session, broadcast_fn
        )
        await filter_rollup_conflicts(
            [candidate.id], [candidate], db_session, broadcast_fn
        )
        assert candidate.status == TaskStatus.QUEUED
        broadcast_fn.reset_mock()

        # Patch the parking commit to raise, then cross the threshold. The filter
        # reads via session.execute (autoflush, not commit), so only the helper's
        # explicit parking commit is affected.
        mocker.patch.object(
            db_session, "commit", new=AsyncMock(side_effect=RuntimeError("db down"))
        )
        with pytest.raises(RuntimeError):
            await filter_rollup_conflicts(
                [candidate.id], [candidate], db_session, broadcast_fn
            )

        assert candidate.status == TaskStatus.BLOCKED  # mutated in memory only
        assert all(
            cc.args[2].get("status") != "BLOCKED"
            for cc in broadcast_fn.call_args_list
        )
        esc.assert_not_called()
