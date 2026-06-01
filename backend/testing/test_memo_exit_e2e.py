"""End-to-end integration tests for the memo-exit pathway and strategic citation criterion.

Covers the full chain from spawning COMPLETED memo tasks through to the
ever_cited_memo signal flipping to True, without provisioning an agent VM.

Success criterion for the parent direction:
  - ≥5 tasks exit via the memo pathway (count_memo_exits ≥ 5)
  - ≥1 of those is cited in a completed strategic cycle (ever_cited_memo returns True)
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app import strategic_cycle
from app.models import Project, Task, TaskStatus
from app.task_completion import _apply_synthesized_memo


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _make_project(session) -> Project:
    p = Project(name="Memo E2E Project", user_id="test-memo-e2e")
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return p


def _make_memo_task(project_id) -> Task:
    return Task(
        project_id=project_id,
        payloads=[
            {
                "type": "memo",
                "topic": "investigation",
                "reasoning_kind": "investigation",
            }
        ],
        status=TaskStatus.QUEUED,
    )


def _synth_memo(summary: str = "Finding summary") -> dict:
    return {
        "summary": summary,
        "reasoning": "Full reasoning text for the investigation.",
        "citations": ["task:abc-123"],
        "tags": ["investigation"],
    }


# ---------------------------------------------------------------------------
# Baseline: count_memo_exits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_memo_exits_zero_when_no_memo_tasks(db_session):
    """Fresh project with no tasks returns 0."""
    project = await _make_project(db_session)
    count = await strategic_cycle.count_memo_exits(db_session, project.id)
    assert count == 0


@pytest.mark.asyncio
async def test_count_memo_exits_ignores_non_memo_tasks(db_session):
    """COMPLETED tasks whose type is not 'memo' must not be counted."""
    project = await _make_project(db_session)
    for task_type in ("create", "research", "fix", "strategic_review"):
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": task_type}],
                status=TaskStatus.COMPLETED,
            )
        )
    await db_session.commit()

    count = await strategic_cycle.count_memo_exits(db_session, project.id)
    assert count == 0, f"Expected 0 memo exits, got {count} (non-memo tasks counted)"


# ---------------------------------------------------------------------------
# _apply_synthesized_memo: marks task COMPLETED at spawn time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_synthesized_memo_marks_completed_and_counted(db_session):
    """_apply_synthesized_memo sets task status to COMPLETED immediately.

    After commit, count_memo_exits must return 1 for the project.
    """
    project = await _make_project(db_session)
    memo_task = _make_memo_task(project.id)
    db_session.add(memo_task)
    await db_session.flush()

    _apply_synthesized_memo(memo_task, _synth_memo(), db_session)
    assert memo_task.status == TaskStatus.COMPLETED, (
        "_apply_synthesized_memo must set status=COMPLETED before caller commits"
    )

    await db_session.commit()

    count = await strategic_cycle.count_memo_exits(db_session, project.id)
    assert count == 1, f"Expected 1 memo exit after synthesized memo, got {count}"


# ---------------------------------------------------------------------------
# _handle_memo_completion: async completion handler marks task COMPLETED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_memo_completion_exit_counted(db_session):
    """_handle_memo_completion marks the task COMPLETED; count_memo_exits returns 1."""
    from app.task_completion import _handle_memo_completion

    project = await _make_project(db_session)
    memo_task = _make_memo_task(project.id)
    db_session.add(memo_task)
    await db_session.commit()
    await db_session.refresh(memo_task)

    logs = json.dumps(
        {
            "summary": "Rate-limit investigation concluded.",
            "reasoning": "No actionable fix found; classified as known rate-limit behaviour.",
            "citations": ["task:aaa-111"],
            "tags": ["investigation-unactionable"],
        }
    )

    with patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock):
        await _handle_memo_completion(memo_task, logs, db_session)

    assert memo_task.status == TaskStatus.COMPLETED
    count = await strategic_cycle.count_memo_exits(db_session, project.id)
    assert count == 1, (
        f"Expected 1 memo exit after _handle_memo_completion, got {count}"
    )


# ---------------------------------------------------------------------------
# _spawn_retirement_memos: retirement path produces COMPLETED memo tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_retirement_memos_completed_memo_counted(db_session):
    """_spawn_retirement_memos with a rationale synthesizes a COMPLETED memo.

    count_memo_exits must return 1 after the parent task commits.
    """
    from app.strategic_completion import _spawn_retirement_memos

    direction_id = str(uuid.uuid4())
    project = Project(
        name="Retirement Test",
        user_id="test-memo-e2e",
        directions=[
            {
                "id": direction_id,
                "text": "Improve CI cache hit rate",
                "status": "retired",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent_task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(parent_task)
    await db_session.commit()
    await db_session.refresh(parent_task)

    spawned = await _spawn_retirement_memos(
        db_session,
        project,
        parent_task,
        retired_ids=[direction_id],
        rationales={
            direction_id: "Direction retired: goal fully achieved, CI cache hit rate >80%."
        },
    )
    assert spawned == 1, "Expected 1 retirement memo to be spawned"
    await db_session.commit()

    count = await strategic_cycle.count_memo_exits(db_session, project.id)
    assert count == 1, (
        f"Retirement memo must be COMPLETED at spawn time and counted; got {count}"
    )


# ---------------------------------------------------------------------------
# Five memo exits reach the direction success threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_five_synthesized_memos_reach_direction_threshold(db_session):
    """Synthesizing 5 memos via _apply_synthesized_memo reaches count_memo_exits ≥ 5.

    This is the numeric target from the direction's success criterion.
    """
    project = await _make_project(db_session)

    for i in range(5):
        t = _make_memo_task(project.id)
        db_session.add(t)
        await db_session.flush()
        _apply_synthesized_memo(t, _synth_memo(summary=f"Finding {i}"), db_session)

    await db_session.commit()

    count = await strategic_cycle.count_memo_exits(db_session, project.id)
    assert count >= 5, (
        f"Direction requires ≥5 memo exits; count_memo_exits returned {count}"
    )


# ---------------------------------------------------------------------------
# Full citation chain: _handle_strategic_completion → ever_cited_memo=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_strategic_completion_cited_memo_sets_ever_cited(db_session):
    """Full e2e: COMPLETED memo tasks + strategic cycle that cites one → ever_cited=True.

    Steps:
      1. Create 3 synthesized COMPLETED memo tasks.
      2. Create a StrategicCycle in PHASE2_RUNNING state.
      3. Create a strategic_review task with _strategic_cited_memo_ids pointing
         at one of the memo task IDs.
      4. Call _handle_strategic_completion (the api_was_used path).
      5. Assert ever_cited_memo returns ever_cited=True.
    """
    from app.strategic_completion import _handle_strategic_completion

    project = Project(
        name="Citation E2E",
        user_id="test-memo-e2e",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 1. Create 3 synthesized COMPLETED memo tasks
    memo_task_ids: list[str] = []
    for i in range(3):
        t = _make_memo_task(project.id)
        db_session.add(t)
        await db_session.flush()
        _apply_synthesized_memo(t, _synth_memo(summary=f"Memo {i}"), db_session)
        await db_session.flush()
        memo_task_ids.append(str(t.id))

    await db_session.commit()

    # Confirm all 3 are countable before the cycle runs
    pre_count = await strategic_cycle.count_memo_exits(db_session, project.id)
    assert pre_count == 3

    # 2. Create a StrategicCycle that is ready for phase-2 completion
    phase1_task = Task(
        project_id=project.id,
        payloads=[{"type": "design_doc_update"}],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(phase1_task)
    await db_session.flush()

    cycle = await strategic_cycle.create(
        db_session,
        project_id=project.id,
        phase1_task_id=phase1_task.id,
    )
    await db_session.commit()

    # Advance cycle to PHASE2_RUNNING so mark_phase2_completed can complete it
    phase2_stub = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        status=TaskStatus.QUEUED,
    )
    db_session.add(phase2_stub)
    await db_session.flush()
    await strategic_cycle.mark_phase1_completed(
        db_session, cycle.id, phase2_task_id=phase2_stub.id
    )
    await db_session.commit()

    # 3. Build the strategic_review task with citation context
    cited_id = memo_task_ids[0]
    strategic_task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={
            "_strategic_api_called": True,
            "strategic_cycle_id": cycle.id,
            "_strategic_cited_memo_ids": [cited_id],
            "_strategic_dirs_created": 0,
            "_strategic_dirs_retired": 0,
            "_strategic_tasks_spawned": 0,
        },
        status=TaskStatus.COMPLETED,
    )
    db_session.add(strategic_task)
    await db_session.commit()
    await db_session.refresh(strategic_task)

    # 4. Call the completion handler (mock side-effect helpers)
    with (
        patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock),
        patch(
            "app.research_completion.query_findings_index",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        await _handle_strategic_completion(
            strategic_task, "", db_session, project=project
        )

    # 5. Verify the cycle recorded a citation and ever_cited_memo flips True
    await db_session.refresh(cycle)
    cited_count = (cycle.cycle_metadata or {}).get("memos_cited", 0)
    assert cited_count >= 1, (
        f"Expected memos_cited ≥ 1 in cycle_metadata, got {cited_count}"
    )

    result = await strategic_cycle.ever_cited_memo(db_session, project.id)
    assert result["ever_cited"] is True, (
        f"ever_cited_memo must return True after a cycle cites a memo; got {result}"
    )
    assert result["first_cycle_id"] == cycle.id
