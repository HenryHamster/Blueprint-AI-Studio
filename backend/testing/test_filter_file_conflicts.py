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


def _queued(project_id, files, scope="small", ttype="fix"):
    task = make_task(
        project_id=project_id,
        status=TaskStatus.QUEUED,
        context={"related_files": files, "scope": scope},
        payloads=[{"type": ttype}],
    )
    return task


def _in_progress(project_id, files):
    return make_task(
        project_id=project_id,
        status=TaskStatus.IN_PROGRESS,
        context={"related_files": files},
        payloads=[{"type": "fix"}],
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
    async def test_plan_type_deferred_on_single_file_overlap(self):
        pid = uuid.uuid4()
        c = _queued(pid, ["a.py", "b.py"], scope="small", ttype="plan")
        ip = _in_progress(pid, ["a.py"])
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
    async def test_shared_test_files_not_deferred(self):
        pid = uuid.uuid4()
        c = _queued(pid, ["backend/testing/test_foo.py", "backend/testing/test_bar.py"])
        ip = _in_progress(pid, ["backend/testing/test_foo.py", "backend/testing/test_bar.py"])
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
