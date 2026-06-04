"""Tests for the perf-audit task completion handler.

A perf task analyzes the codebase for performance bottlenecks, spawns a
``create`` child task per high-severity finding, and surfaces medium/low
findings as a PR comment. It opens NO PR of its own.

Regression focus: when a perf audit finds nothing high-severity it spawns
ZERO children. A perf task with no children and no PR has no lifecycle path
that would ever promote it out of EVALUATING (no COMPLETED child triggers a
rollup; the reaper's EVALUATING sweep skips childless parents). It must be
marked COMPLETED directly — otherwise it hangs forever and pins its
research/strategic parent in EVALUATING too.
"""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from app.models import Task, TaskStatus
from app.task_completion import _handle_perf_completion
from testing.factories import make_project, make_task


@pytest.fixture
def _quiet_perf_io(mocker):
    """Silence broadcasts and stub the GitHub PR-comment path so the handler
    runs without network access."""
    mocker.patch(
        "app.task_completion.broadcast_to_project", new=AsyncMock(return_value=None)
    )
    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.task_completion.get_open_pull_requests",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.task_completion.create_pr_comment", new=AsyncMock(return_value=None)
    )


async def _make_perf_task(db_session):
    project = make_project(name="PerfProj")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="perf: audit something",
        status=TaskStatus.COMPLETED,  # agent reported done; handler decides final state
        payloads=[{"type": "perf", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


async def _children_of(db_session, task):
    return (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_perf_completion_no_high_severity_marks_completed(
    db_session, _quiet_perf_io
):
    """Findings exist but none are high-severity → ZERO children spawned →
    task is marked COMPLETED directly (not left in EVALUATING)."""
    task = await _make_perf_task(db_session)

    findings = [
        {
            "severity": "medium",
            "area": "db",
            "title": "N+1 query in dashboard",
            "description": "Loads each row in a loop.",
            "fix_prompt": "Batch the query.",
        },
        {
            "severity": "low",
            "area": "ui",
            "title": "Avoidable re-render",
            "description": "Memoize the list.",
            "fix_prompt": "Wrap in useMemo.",
        },
    ]

    await _handle_perf_completion(task, json.dumps(findings), db_session)

    children = await _children_of(db_session, task)
    assert children == []

    await db_session.refresh(task)
    # The regression: a childless, PR-less perf task MUST be terminal.
    assert task.status == TaskStatus.COMPLETED
    assert "no high-severity findings" in (task.latest_status_message or "")


@pytest.mark.asyncio
async def test_perf_completion_high_severity_spawns_children_and_evaluates(
    db_session, _quiet_perf_io
):
    """At least one high-severity finding → spawns a child fix task and parks
    the parent in EVALUATING to await the child's rollup."""
    task = await _make_perf_task(db_session)

    findings = [
        {
            "severity": "high",
            "area": "provisioning",
            "title": "VM cold-start blocks claim",
            "description": "Synchronous provision on the hot path.",
            "fix_prompt": "Provision asynchronously and reuse warm VMs.",
        },
        {
            "severity": "low",
            "area": "logging",
            "title": "Chatty debug logs",
            "description": "Excess log volume.",
            "fix_prompt": "Drop to debug level.",
        },
    ]

    await _handle_perf_completion(task, json.dumps(findings), db_session)

    children = await _children_of(db_session, task)
    assert len(children) == 1
    assert children[0].parent_task_id == task.id

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert "1 high-severity fix task spawned" in (task.latest_status_message or "")


@pytest.mark.asyncio
async def test_perf_completion_broadcasts_children_after_commit(
    db_session, _quiet_perf_io, mocker
):
    """Child task updates must broadcast only after the final perf commit."""
    task = await _make_perf_task(db_session)

    findings = [
        {
            "severity": "high",
            "area": "provisioning",
            "title": "VM cold-start blocks claim",
            "description": "Synchronous provision on the hot path.",
            "fix_prompt": "Provision asynchronously and reuse warm VMs.",
        }
    ]

    events: list = []
    commit_count = 0
    real_commit = db_session.commit

    async def _tracking_commit(*a, **k):
        nonlocal commit_count
        commit_count += 1
        events.append(f"commit-{commit_count}")
        return await real_commit(*a, **k)

    mocker.patch.object(db_session, "commit", side_effect=_tracking_commit)

    async def _tracking_broadcast(project_id, event_name, payload):
        events.append(("broadcast", event_name, payload.get("task_id")))

    mocker.patch(
        "app.task_completion.broadcast_to_project", side_effect=_tracking_broadcast
    )

    await _handle_perf_completion(task, json.dumps(findings), db_session)

    children = await _children_of(db_session, task)
    assert len(children) == 1
    assert events.index("commit-2") < events.index(
        ("broadcast", "task_updated", str(children[0].id))
    )


@pytest.mark.asyncio
async def test_perf_completion_does_not_broadcast_children_on_commit_failure(
    db_session, _quiet_perf_io, mocker
):
    """If the final perf commit fails, queued child broadcasts must not fire."""
    task = await _make_perf_task(db_session)

    findings = [
        {
            "severity": "high",
            "area": "provisioning",
            "title": "VM cold-start blocks claim",
            "description": "Synchronous provision on the hot path.",
            "fix_prompt": "Provision asynchronously and reuse warm VMs.",
        }
    ]

    broadcast = mocker.patch(
        "app.task_completion.broadcast_to_project", new_callable=AsyncMock
    )
    commit_count = 0
    real_commit = db_session.commit

    async def _failing_commit(*a, **k):
        nonlocal commit_count
        commit_count += 1
        if commit_count == 2:
            raise RuntimeError("perf commit boom")
        return await real_commit(*a, **k)

    mocker.patch.object(db_session, "commit", side_effect=_failing_commit)

    with pytest.raises(RuntimeError, match="perf commit boom"):
        await _handle_perf_completion(task, json.dumps(findings), db_session)

    broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_perf_completion_no_measurement_fails_metric_gate(
    db_session, _quiet_perf_io
):
    """Metric gate: unparseable / empty output means the perf report produced no
    structured measurement, so it must NOT complete clean. No children spawn and
    the task is marked FAILED (a perf task opens no PR and spawns no children, so
    EVALUATING would hang forever)."""
    task = await _make_perf_task(db_session)

    await _handle_perf_completion(task, "not a findings array at all", db_session)

    children = await _children_of(db_session, task)
    assert children == []

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert "gate" in (task.latest_status_message or "").lower()
    assert task.context["evaluation_gate"]["passed"] is False
    assert task.context["evaluation_gate"]["strategy"] == "metric"


@pytest.mark.asyncio
async def test_perf_completion_multiple_high_severity_bundles_into_one_child(
    db_session, _quiet_perf_io
):
    """Multiple high-severity findings → bundled into a SINGLE child task that
    enumerates all findings so the agent addresses them together."""
    task = await _make_perf_task(db_session)

    findings = [
        {
            "severity": "high",
            "area": "provisioning",
            "title": "VM cold-start blocks claim",
            "description": "Synchronous provision on the hot path.",
            "fix_prompt": "Provision asynchronously and reuse warm VMs.",
        },
        {
            "severity": "high",
            "area": "caching",
            "title": "Missing result cache",
            "description": "Repeated computation in hot loop.",
            "fix_prompt": "Add memoization layer.",
        },
        {
            "severity": "low",
            "area": "logging",
            "title": "Chatty debug logs",
            "description": "Excess log volume.",
            "fix_prompt": "Drop to debug level.",
        },
    ]

    await _handle_perf_completion(task, json.dumps(findings), db_session)

    children = await _children_of(db_session, task)
    # Key assertion: multiple high-severity findings → ONE bundled child
    assert len(children) == 1
    assert children[0].parent_task_id == task.id

    # Verify the bundled task enumerates both findings in its prompt
    child_payload = children[0].first_payload or {}
    child_prompt = child_payload.get("prompt", "")
    assert "2 high-severity findings" in child_prompt or "identified 2" in child_prompt
    assert "VM cold-start" in child_prompt
    assert "Missing result cache" in child_prompt

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    # Verify message indicates bundled task + medium/low findings
    msg = task.latest_status_message or ""
    assert "1 high-severity fix task spawned" in msg
    assert "2 findings" in msg  # The bundled task covers 2 high-sev findings
    assert "1 medium/low" in msg
