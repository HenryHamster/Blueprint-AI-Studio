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
