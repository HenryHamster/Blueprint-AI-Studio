"""Tests for the fix-loop telemetry foundation and convergence aggregate endpoint.

Covers:
  - the ingest helper (apply_fix_loop_events): append, replace-by-key,
    per-loop cap.
  - heartbeat-driven persistence end-to-end: three iterations of a lint
    loop arrive via /api/agent/heartbeat and land on Task.fix_loop_history.
  - the admin read endpoint returns the stored history + summary.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from app.auth import create_agent_token
from app.fix_loop_telemetry import (
    FixLoopEventIn,
    MAX_ATTEMPTS_PER_LOOP,
    apply_fix_loop_events,
    summarize_fix_loop_history,
)
from app.models import Project, ProjectMember, Task, TaskStatus


def _ev(loop: str, iteration: int, **kw):
    base = {
        "loop": loop,
        "iteration": iteration,
        "fingerprint_hash": kw.get("fingerprint_hash", "abc1234567890def"),
        "error_tail": kw.get("error_tail", "TypeError: foo"),
        "change_summary": kw.get("change_summary", "1 file, +2 -1"),
        "exit_reason": kw.get("exit_reason", "in_progress"),
        "source": kw.get("source", "agent_emitted"),
        "started_at": kw.get("started_at", "2026-05-04T00:00:00"),
        "ended_at": kw.get("ended_at", "2026-05-04T00:00:30"),
    }
    return base


# --- ingest helper unit tests ---------------------------------------------

def test_apply_fix_loop_events_appends_new_iterations():
    task = Task(project_id=uuid.uuid4(), payloads=[{"cmd": "x"}])
    events = [
        FixLoopEventIn(**_ev("lint", 1)),
        FixLoopEventIn(**_ev("lint", 2)),
    ]
    apply_fix_loop_events(task, events)
    assert len(task.fix_loop_history["lint"]) == 2
    assert [e["iteration"] for e in task.fix_loop_history["lint"]] == [1, 2]


def test_apply_fix_loop_events_replaces_by_loop_iteration():
    task = Task(project_id=uuid.uuid4(), payloads=[{"cmd": "x"}])
    apply_fix_loop_events(task, [FixLoopEventIn(**_ev("lint", 1, exit_reason="in_progress"))])
    apply_fix_loop_events(task, [FixLoopEventIn(**_ev("lint", 1, exit_reason="passed"))])
    assert len(task.fix_loop_history["lint"]) == 1
    assert task.fix_loop_history["lint"][0]["exit_reason"] == "passed"


def test_apply_fix_loop_events_caps_per_loop_list():
    task = Task(project_id=uuid.uuid4(), payloads=[{"cmd": "x"}])
    events = [FixLoopEventIn(**_ev("test", i)) for i in range(1, MAX_ATTEMPTS_PER_LOOP + 11)]
    apply_fix_loop_events(task, events)
    # Capped to MAX_ATTEMPTS_PER_LOOP, oldest dropped — newest must remain.
    bucket = task.fix_loop_history["test"]
    assert len(bucket) == MAX_ATTEMPTS_PER_LOOP
    assert bucket[-1]["iteration"] == MAX_ATTEMPTS_PER_LOOP + 10
    assert bucket[0]["iteration"] == 11


def test_apply_fix_loop_events_isolates_loops():
    task = Task(project_id=uuid.uuid4(), payloads=[{"cmd": "x"}])
    apply_fix_loop_events(task, [
        FixLoopEventIn(**_ev("lint", 1)),
        FixLoopEventIn(**_ev("test", 1)),
        FixLoopEventIn(**_ev("check", 1)),
    ])
    assert len(task.fix_loop_history["lint"]) == 1
    assert len(task.fix_loop_history["test"]) == 1
    assert len(task.fix_loop_history["check"]) == 1


def test_summarize_fix_loop_history_handles_empty():
    summary = summarize_fix_loop_history({})
    for loop in ("lint", "test", "check"):
        assert summary[loop] == {"attempts": 0, "last_exit_reason": None}


def test_summarize_fix_loop_history_reports_terminal_reason():
    history = {
        "lint": [
            {"iteration": 1, "exit_reason": "in_progress"},
            {"iteration": 2, "exit_reason": "passed"},
        ],
    }
    summary = summarize_fix_loop_history(history)
    assert summary["lint"]["attempts"] == 2
    assert summary["lint"]["last_exit_reason"] == "passed"


# --- end-to-end via heartbeat + admin read endpoint ----------------------

@pytest.mark.asyncio
async def test_heartbeat_persists_fix_loop_events_and_admin_read(
    client: AsyncClient, db_session
):
    """Three heartbeats simulate a 3-iteration lint loop. Final iteration
    is terminal (exit_reason="passed"). Both the ORM column and the
    admin GET endpoint must reflect the stored events.
    """
    project = Project(name="Fix loop telemetry test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(ProjectMember(project_id=project.id, user_id="test-user-123", role="owner"))
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-fixloop",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-fixloop")
    headers = {"x-agent-token": token}

    for i in (1, 2, 3):
        exit_reason = "passed" if i == 3 else "in_progress"
        payload = {
            "task_id": str(task.id),
            "fix_loop_events": [
                {
                    "loop": "lint",
                    "iteration": i,
                    "fingerprint_hash": f"deadbeef{i:04x}aaaa",
                    "error_tail": f"iter {i}: import error",
                    "change_summary": f"+{i} -0",
                    "exit_reason": exit_reason,
                    "source": "agent_emitted",
                    "started_at": "2026-05-04T00:00:00",
                    "ended_at": "2026-05-04T00:00:30",
                }
            ],
        }
        resp = await client.post("/api/agent/heartbeat", json=payload, headers=headers)
        assert resp.status_code == 200, resp.text

    await db_session.refresh(task)
    bucket = task.fix_loop_history["lint"]
    assert len(bucket) == 3
    assert [e["iteration"] for e in bucket] == [1, 2, 3]
    for e in bucket:
        assert e["fingerprint_hash"]  # hex present
        assert all(c in "0123456789abcdef" for c in e["fingerprint_hash"])
    assert bucket[-1]["exit_reason"] == "passed"

    # Admin read endpoint
    resp = await client.get(f"/api/admin/tasks/{task.id}/fix-loop-history")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == str(task.id)
    assert len(body["fix_loop_history"]["lint"]) == 3
    assert body["summary"]["lint"]["attempts"] == 3
    assert body["summary"]["lint"]["last_exit_reason"] == "passed"
    assert body["summary"]["test"] == {"attempts": 0, "last_exit_reason": None}


@pytest.mark.asyncio
async def test_heartbeat_without_fix_loop_events_unchanged(
    client: AsyncClient, db_session
):
    """Backward compat: a heartbeat with no fix_loop_events must keep
    fix_loop_history empty (older agents that don't yet emit telemetry).
    """
    project = Project(name="Fix loop bc test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-fixloop-bc",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-fixloop-bc")
    headers = {"x-agent-token": token}
    resp = await client.post(
        "/api/agent/heartbeat",
        json={"task_id": str(task.id), "status_message": "still running"},
        headers=headers,
    )
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.fix_loop_history == {}


@pytest.mark.asyncio
async def test_heartbeat_drops_invalid_fix_loop_events(client: AsyncClient, db_session):
    """A malformed event in the batch must not 422 the heartbeat — invalid
    entries are dropped silently so a forward-rolled agent doesn't break
    the heartbeat hot path.
    """
    project = Project(name="Fix loop dropbad test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-fixloop-bad",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-fixloop-bad")
    headers = {"x-agent-token": token}
    resp = await client.post(
        "/api/agent/heartbeat",
        json={
            "task_id": str(task.id),
            "fix_loop_events": [
                {"loop": "not_a_loop", "iteration": 1},  # invalid loop
                {"loop": "lint", "iteration": 1, "exit_reason": "passed"},  # valid
            ],
        },
        headers=headers,
    )
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert len(task.fix_loop_history["lint"]) == 1
    assert task.fix_loop_history["lint"][0]["exit_reason"] == "passed"


# --- fix-loop convergence aggregate endpoint ---------------------------------

@pytest.mark.asyncio
async def test_convergence_endpoint_empty(client: AsyncClient):
    """No tasks with fix-loop history → tasks_with_fix_loops == 0, all loops have zero count."""
    resp = await client.get("/api/admin/fix-loop-convergence?hours=24")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_hours"] == 24
    assert body["tasks_with_fix_loops"] == 0
    for loop in ("lint", "test", "check"):
        assert body[loop]["task_count"] == 0
        assert body[loop]["avg_iterations"] is None
        assert body[loop]["convergence_rate"] is None


@pytest.mark.asyncio
async def test_convergence_endpoint_mixed_loops(client: AsyncClient, db_session):
    """Tasks with mixed loop outcomes are aggregated correctly."""
    project = Project(name="Convergence test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(ProjectMember(project_id=project.id, user_id="test-user-123", role="owner"))
    await db_session.commit()

    # Task 1: lint passed in 2 iterations, test passed in 1
    task1 = Task(
        project_id=project.id,
        payloads=[{"cmd": "x"}],
        status=TaskStatus.COMPLETED,
        fix_loop_history={
            "lint": [
                {"loop": "lint", "iteration": 1, "exit_reason": "in_progress"},
                {"loop": "lint", "iteration": 2, "exit_reason": "passed"},
            ],
            "test": [
                {"loop": "test", "iteration": 1, "exit_reason": "passed"},
            ],
        },
    )
    # Task 2: lint exhausted in 4 iterations
    task2 = Task(
        project_id=project.id,
        payloads=[{"cmd": "x"}],
        status=TaskStatus.FAILED,
        fix_loop_history={
            "lint": [
                {"loop": "lint", "iteration": i, "exit_reason": "in_progress" if i < 4 else "max_attempts"}
                for i in range(1, 5)
            ],
        },
    )
    db_session.add_all([task1, task2])
    await db_session.commit()

    resp = await client.get("/api/admin/fix-loop-convergence?hours=24")
    assert resp.status_code == 200
    body = resp.json()

    assert body["tasks_with_fix_loops"] == 2

    lint = body["lint"]
    assert lint["task_count"] == 2
    # task1 had 2 lint iters, task2 had 4 → avg = 3.0
    assert lint["avg_iterations"] == 3.0
    # only task1 converged → 1/2 = 0.5
    assert lint["convergence_rate"] == 0.5
    assert lint["exit_reason_counts"].get("passed") == 1
    assert lint["exit_reason_counts"].get("max_attempts") == 1

    test = body["test"]
    assert test["task_count"] == 1
    assert test["avg_iterations"] == 1.0
    assert test["convergence_rate"] == 1.0


@pytest.mark.asyncio
async def test_convergence_endpoint_iteration_distribution(client: AsyncClient, db_session):
    """Iteration distribution buckets (1, 2, 3, 4+) are counted correctly."""
    project = Project(name="Iter dist test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(ProjectMember(project_id=project.id, user_id="test-user-123", role="owner"))
    await db_session.commit()

    buckets = [1, 1, 2, 3, 5]  # two 1-iter, one 2-iter, one 3-iter, one 4+ (5 iters)
    for n in buckets:
        task = Task(
            project_id=project.id,
            payloads=[{"cmd": "x"}],
            status=TaskStatus.COMPLETED,
            fix_loop_history={
                "lint": [
                    {"loop": "lint", "iteration": i, "exit_reason": "passed" if i == n else "in_progress"}
                    for i in range(1, n + 1)
                ]
            },
        )
        db_session.add(task)
    await db_session.commit()

    resp = await client.get("/api/admin/fix-loop-convergence?hours=24")
    assert resp.status_code == 200
    body = resp.json()
    dist = body["lint"]["iteration_distribution"]
    assert dist["1"] == 2
    assert dist["2"] == 1
    assert dist["3"] == 1
    assert dist["4+"] == 1
