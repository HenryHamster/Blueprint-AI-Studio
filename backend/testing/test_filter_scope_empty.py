"""Unit tests for filter_scope_empty in orchestrator_filters."""

import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.models import TaskStatus
from app.orchestrator_filters import filter_scope_empty
from testing.factories import make_task


def _task(
    project_id,
    parent_id,
    status,
    files=None,
    auto_decomposed=True,
    ptype="create",
    finding_index=0,
):
    ctx = {"related_files": files or []}
    if auto_decomposed:
        ctx["auto_decomposed"] = True
    if finding_index is not None:
        ctx["finding_index"] = finding_index
    return make_task(
        project_id=project_id,
        parent_task_id=parent_id,
        status=status,
        context=ctx,
        payloads=[{"type": ptype}],
    )


def _mock_session(completed_siblings):
    session = MagicMock()
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = completed_siblings
    result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=result)
    return session


class TestFilterScopeEmpty:
    @pytest.mark.asyncio
    async def test_pre_empts_when_all_files_covered(self):
        pid = uuid.uuid4()
        parent_id = uuid.uuid4()
        files = ["backend/app/foo.py"]
        sibling = _task(pid, parent_id, TaskStatus.COMPLETED, files=files)
        candidate = _task(pid, parent_id, TaskStatus.QUEUED, files=files)

        session = _mock_session([sibling])
        broadcast = AsyncMock()

        result = await filter_scope_empty(
            [candidate.id], [candidate], session, broadcast
        )

        assert result == []
        assert candidate.status == TaskStatus.CANCELLED
        assert "SCOPE_EMPTY" in (candidate.latest_status_message or "")
        assert (candidate.context or {}).get("failure_category") == "SCOPE_EMPTY"
        broadcast.assert_awaited_once()

    @pytest.mark.parametrize("ptype", ["review", "fix", "plan"])
    @pytest.mark.asyncio
    async def test_non_cluster_task_types_not_pre_empted(self, ptype):
        """Regression (2026-05-09 SCOPE_EMPTY loop): review/fix/plan tasks
        inherit ``auto_decomposed`` from the create-task they descend from, but
        a sibling cluster covering the same files does not make their work
        redundant. Pre-empting a review stranded PR #4717 and spawned 795
        respawned review tasks in 9.5h — only the implementation task is
        pre-emptible."""
        pid = uuid.uuid4()
        parent_id = uuid.uuid4()
        files = ["backend/app/foo.py"]
        sibling = _task(pid, parent_id, TaskStatus.COMPLETED, files=files)
        candidate = _task(
            pid, parent_id, TaskStatus.QUEUED, files=files, ptype=ptype
        )

        session = _mock_session([sibling])
        broadcast = AsyncMock()

        result = await filter_scope_empty(
            [candidate.id], [candidate], session, broadcast
        )

        assert result == [candidate.id], f"{ptype} task must not be pre-empted"
        assert candidate.status == TaskStatus.QUEUED
        broadcast.assert_not_called()

    @pytest.mark.asyncio
    async def test_passes_through_when_not_fully_covered(self):
        pid = uuid.uuid4()
        parent_id = uuid.uuid4()
        sibling = _task(
            pid, parent_id, TaskStatus.COMPLETED, files=["backend/app/foo.py"]
        )
        candidate = _task(
            pid,
            parent_id,
            TaskStatus.QUEUED,
            files=["backend/app/foo.py", "backend/app/bar.py"],
        )

        session = _mock_session([sibling])
        broadcast = AsyncMock()

        result = await filter_scope_empty(
            [candidate.id], [candidate], session, broadcast
        )

        assert result == [candidate.id]
        assert candidate.status == TaskStatus.QUEUED
        broadcast.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_completed_siblings_passes_through(self):
        pid = uuid.uuid4()
        parent_id = uuid.uuid4()
        candidate = _task(
            pid, parent_id, TaskStatus.QUEUED, files=["backend/app/foo.py"]
        )

        session = _mock_session([])
        broadcast = AsyncMock()

        result = await filter_scope_empty(
            [candidate.id], [candidate], session, broadcast
        )

        assert result == [candidate.id]

    @pytest.mark.asyncio
    async def test_non_auto_decomposed_not_checked(self):
        pid = uuid.uuid4()
        parent_id = uuid.uuid4()
        sibling = _task(
            pid, parent_id, TaskStatus.COMPLETED, files=["backend/app/foo.py"]
        )
        candidate = _task(
            pid,
            parent_id,
            TaskStatus.QUEUED,
            files=["backend/app/foo.py"],
            auto_decomposed=False,
        )

        session = _mock_session([sibling])
        broadcast = AsyncMock()

        result = await filter_scope_empty(
            [candidate.id], [candidate], session, broadcast
        )

        # No DB query should happen for non-auto-decomposed tasks
        assert result == [candidate.id]
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_task_without_parent_not_checked(self):
        pid = uuid.uuid4()
        candidate = make_task(
            project_id=pid,
            status=TaskStatus.QUEUED,
            context={"auto_decomposed": True, "related_files": ["backend/app/foo.py"]},
            payloads=[{"type": "create"}],
        )
        # parent_task_id is None (not set)

        session = _mock_session([])
        broadcast = AsyncMock()

        result = await filter_scope_empty(
            [candidate.id], [candidate], session, broadcast
        )

        assert result == [candidate.id]
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_test_files_excluded_from_overlap_check(self):
        pid = uuid.uuid4()
        parent_id = uuid.uuid4()
        # Sibling covers only a test file; prod file not covered
        sibling = _task(
            pid,
            parent_id,
            TaskStatus.COMPLETED,
            files=["backend/testing/test_foo.py"],
        )
        candidate = _task(
            pid,
            parent_id,
            TaskStatus.QUEUED,
            files=["backend/app/foo.py"],
        )

        session = _mock_session([sibling])
        broadcast = AsyncMock()

        result = await filter_scope_empty(
            [candidate.id], [candidate], session, broadcast
        )

        # test files excluded → candidate's prod file not covered → passes through
        assert result == [candidate.id]
        assert candidate.status == TaskStatus.QUEUED

    @pytest.mark.asyncio
    async def test_empty_eligible_ids_returns_empty(self):
        session = _mock_session([])
        broadcast = AsyncMock()

        result = await filter_scope_empty([], [], session, broadcast)

        assert result == []
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_pre_empted_by_sibling_from_different_finding(self):
        """Regression (2026-05-21): SCOPE_EMPTY pre-emption must scope
        'sibling' to the same decomposition cluster — (parent_task_id,
        finding_index) — not the whole research parent. A completed task
        from a *different finding* under the same research parent that
        happens to touch an overlapping file does NOT make this cluster's
        work redundant (finding-#1 'fix(digest)' touching admin_digest.py
        wrongly pre-empted a finding-#2 cluster declaring the same file)."""
        pid = uuid.uuid4()
        parent_id = uuid.uuid4()
        files = ["backend/app/foo.py"]
        sibling = _task(
            pid, parent_id, TaskStatus.COMPLETED, files=files, finding_index=1
        )
        candidate = _task(
            pid, parent_id, TaskStatus.QUEUED, files=files, finding_index=2
        )

        session = _mock_session([sibling])
        broadcast = AsyncMock()

        result = await filter_scope_empty(
            [candidate.id], [candidate], session, broadcast
        )

        assert result == [candidate.id], (
            "cross-finding file overlap must not pre-empt"
        )
        assert candidate.status == TaskStatus.QUEUED
        broadcast.assert_not_called()

    @pytest.mark.asyncio
    async def test_pre_empted_by_sibling_from_same_finding(self):
        """Within one finding's decomposition (same finding_index), a
        completed sibling cluster covering all the candidate's files still
        pre-empts it — the fix narrows the match, it does not disable it."""
        pid = uuid.uuid4()
        parent_id = uuid.uuid4()
        files = ["backend/app/foo.py"]
        sibling = _task(
            pid, parent_id, TaskStatus.COMPLETED, files=files, finding_index=2
        )
        candidate = _task(
            pid, parent_id, TaskStatus.QUEUED, files=files, finding_index=2
        )

        session = _mock_session([sibling])
        broadcast = AsyncMock()

        result = await filter_scope_empty(
            [candidate.id], [candidate], session, broadcast
        )

        assert result == []
        assert candidate.status == TaskStatus.CANCELLED
        assert (candidate.context or {}).get("failure_category") == "SCOPE_EMPTY"

    @pytest.mark.asyncio
    async def test_not_pre_empted_when_finding_index_missing(self):
        """A candidate with no finding_index cannot be placed in a cluster
        group, so it must not be pre-empted — coincidental file overlap is
        not proof of redundant work."""
        pid = uuid.uuid4()
        parent_id = uuid.uuid4()
        files = ["backend/app/foo.py"]
        sibling = _task(
            pid, parent_id, TaskStatus.COMPLETED, files=files, finding_index=1
        )
        candidate = _task(
            pid,
            parent_id,
            TaskStatus.QUEUED,
            files=files,
            finding_index=None,
        )

        session = _mock_session([sibling])
        broadcast = AsyncMock()

        result = await filter_scope_empty(
            [candidate.id], [candidate], session, broadcast
        )

        assert result == [candidate.id]
        assert candidate.status == TaskStatus.QUEUED
