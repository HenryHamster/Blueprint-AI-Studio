"""Tests for the security-audit task completion handler.

A security_audit task spawns a ``create`` child fix-task per critical/high
finding and surfaces medium/low findings as a PR comment. It opens NO PR of
its own.

Regression focus (same class as test_perf_completion): a security_audit with
zero spawned children must NOT be left in EVALUATING — it opens no PR, so a
childless EVALUATING task has no lifecycle path to COMPLETED and would hang
forever, pinning any parent. The common "no critical/high" case is guarded by
an early return; the narrow hole is critical/high findings that all lack a
usable fix_prompt — that path now also resolves to COMPLETED.
"""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from app.models import Task, TaskStatus
from app.task_completion import _handle_security_audit_completion
from app.task_evaluators import (
    QUALITY_GRADER_SOURCE_KEY,
    QUALITY_GRADER_VERDICT_KEY,
)
from testing.factories import make_project, make_task


def _enable_audit_grader(mocker):
    """Flip the dark-shipped security-audit forward-quality grader flag ON."""
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"security_audit_quality_grader_enabled": True},
    )


@pytest.fixture
def _quiet_audit_io(mocker):
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


async def _make_audit_task(db_session):
    project = make_project(name="SecAuditProj")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="security_audit: scan repo",
        status=TaskStatus.COMPLETED,  # agent reported done; handler decides final state
        payloads=[{"type": "security_audit", "base_branch": "main"}],
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
async def test_audit_no_critical_high_stays_completed(db_session, _quiet_audit_io):
    """Findings exist but none critical/high → early return, no children,
    incoming terminal status preserved (not flipped to EVALUATING)."""
    task = await _make_audit_task(db_session)
    findings = [
        {
            "severity": "medium",
            "title": "Verbose error in response",
            "description": "Leaks stack info.",
            "fix_prompt": "Return a generic error.",
            "file_path": "app/x.py",
            "line_number": 10,
        },
    ]

    await _handle_security_audit_completion(task, json.dumps(findings), db_session)

    assert await _children_of(db_session, task) == []
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_audit_actionable_without_fix_prompt_marks_completed(
    db_session, _quiet_audit_io
):
    """Critical/high findings exist but all lack a usable fix_prompt/description
    → zero children spawned → must resolve to COMPLETED, not hang EVALUATING."""
    task = await _make_audit_task(db_session)
    findings = [
        {
            "severity": "critical",
            "title": "SQL injection",
            "description": "",  # no description
            "fix_prompt": "",  # no fix prompt → child is skipped
            "file_path": "app/db.py",
            "line_number": 42,
        },
    ]

    await _handle_security_audit_completion(task, json.dumps(findings), db_session)

    assert await _children_of(db_session, task) == []
    await db_session.refresh(task)
    # The regression: childless, PR-less audit MUST be terminal.
    assert task.status == TaskStatus.COMPLETED
    assert "no critical/high findings" in (task.latest_status_message or "")


@pytest.mark.asyncio
async def test_audit_critical_finding_spawns_child_and_evaluates(
    db_session, _quiet_audit_io
):
    """A fixable critical finding → spawns a child fix task and parks the
    parent in EVALUATING to await rollup."""
    task = await _make_audit_task(db_session)
    findings = [
        {
            "severity": "high",
            "title": "Missing auth check on admin route",
            "description": "Endpoint lacks @require_role.",
            "fix_prompt": "Add @require_role('admin') to the endpoint.",
            "file_path": "app/routers/admin.py",
            "line_number": 7,
        },
    ]

    await _handle_security_audit_completion(task, json.dumps(findings), db_session)

    children = await _children_of(db_session, task)
    assert len(children) == 1
    assert children[0].parent_task_id == task.id
    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_audit_critical_finding_escalates_to_grader_when_enabled(
    db_session, _quiet_audit_io, mocker
):
    """Grader enabled + critical/high findings → no fix children spawned yet;
    the audit parks EVALUATING and an INDEPENDENT grader task is stood up."""
    _enable_audit_grader(mocker)
    task = await _make_audit_task(db_session)
    findings = [
        {
            "severity": "critical",
            "title": "SQL injection",
            "description": "Raw string interpolation in query.",
            "fix_prompt": "Use parameterized queries.",
            "file_path": "app/db.py",
            "line_number": 42,
        },
    ]

    await _handle_security_audit_completion(task, json.dumps(findings), db_session)

    # No fix children before the grade — the audit is parked awaiting the verdict.
    assert await _children_of(db_session, task) == []
    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert task.context.get("awaiting_quality_grader")

    # Exactly one independent grader task, linked back to the audit.
    others = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == task.project_id, Task.id != task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(others) == 1
    grader = others[0]
    assert (grader.context or {}).get(QUALITY_GRADER_SOURCE_KEY) == str(task.id)
    assert (grader.first_payload or {}).get("type") == "investigate"


@pytest.mark.asyncio
async def test_audit_no_actionable_skips_grader_even_when_enabled(
    db_session, _quiet_audit_io, mocker
):
    """Proportional gate: an audit with no critical/high findings never escalates
    to a grader, even with the flag on — it ends terminal COMPLETED."""
    _enable_audit_grader(mocker)
    task = await _make_audit_task(db_session)
    findings = [
        {
            "severity": "low",
            "title": "Minor info leak",
            "description": "Verbose error.",
            "fix_prompt": "Generic error.",
            "file_path": "app/x.py",
            "line_number": 5,
        },
    ]

    await _handle_security_audit_completion(task, json.dumps(findings), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    # No grader spawned anywhere in the project.
    all_tasks = (
        (
            await db_session.execute(
                select(Task).where(Task.project_id == task.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert all(
        not (t.context or {}).get(QUALITY_GRADER_SOURCE_KEY) for t in all_tasks
    )


@pytest.mark.asyncio
async def test_audit_failed_grade_blocks_spawn_and_fails(
    db_session, _quiet_audit_io
):
    """A FAILED grader verdict on the re-entrant pass spawns NO fix children and
    marks the audit FAILED (fail closed — a garbage audit launches nothing)."""
    task = await _make_audit_task(db_session)
    # Simulate the verdict the grader-completion resolver writes before re-dispatch.
    task.context = {
        **(task.context or {}),
        QUALITY_GRADER_VERDICT_KEY: {
            "passed": False,
            "reason": "audit is ungrounded; cited vulnerability absent at HEAD",
        },
    }
    db_session.add(task)
    await db_session.commit()

    findings = [
        {
            "severity": "critical",
            "title": "SQL injection",
            "description": "Raw string interpolation in query.",
            "fix_prompt": "Use parameterized queries.",
            "file_path": "app/db.py",
            "line_number": 42,
        },
    ]
    await _handle_security_audit_completion(task, json.dumps(findings), db_session)

    assert await _children_of(db_session, task) == []
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert (task.context or {}).get("evaluation_gate", {}).get("passed") is False


@pytest.mark.asyncio
async def test_audit_passing_grade_resumes_and_spawns(
    db_session, _quiet_audit_io
):
    """A PASSING grader verdict on the re-entrant pass lets the findings spawn
    their fix children, as the legacy path would."""
    task = await _make_audit_task(db_session)
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        QUALITY_GRADER_VERDICT_KEY: {"passed": True, "reason": "well-grounded"},
    }
    db_session.add(task)
    await db_session.commit()

    findings = [
        {
            "severity": "high",
            "title": "Missing auth check on admin route",
            "description": "Endpoint lacks @require_role.",
            "fix_prompt": "Add @require_role('admin') to the endpoint.",
            "file_path": "app/routers/admin.py",
            "line_number": 7,
        },
    ]
    await _handle_security_audit_completion(task, json.dumps(findings), db_session)

    children = await _children_of(db_session, task)
    assert len(children) == 1
    # No grader re-spawned on the resume pass.
    assert not any(
        (c.context or {}).get(QUALITY_GRADER_SOURCE_KEY) for c in children
    )
    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_audit_with_non_dict_findings_skips_bad_elements(
    db_session, _quiet_audit_io
):
    """Malformed findings array with non-dict elements → handler completes
    gracefully, skipping bad elements instead of raising AttributeError."""
    task = await _make_audit_task(db_session)
    # Mix valid dicts with non-dict elements that would crash .get() if unfiltered
    malformed_findings = [
        {
            "severity": "high",
            "title": "Missing auth check",
            "description": "Endpoint lacks @require_role.",
            "fix_prompt": "Add @require_role('admin').",
            "file_path": "app/routers/admin.py",
            "line_number": 7,
        },
        "stray string",  # non-dict element
        42,  # non-dict element
        {
            "severity": "medium",
            "title": "Verbose error",
            "description": "Leaks stack info.",
            "file_path": "app/x.py",
            "line_number": 10,
        },
        ["nested", "array"],  # non-dict element
    ]

    await _handle_security_audit_completion(
        task, json.dumps(malformed_findings), db_session
    )

    # One child spawned from the high-severity valid finding
    children = await _children_of(db_session, task)
    assert len(children) == 1
    assert children[0].title == "[HIGH] Missing auth check"

    await db_session.refresh(task)
    # Handler completes successfully despite malformed elements
    assert task.status == TaskStatus.EVALUATING
    # Findings stored should only contain valid dicts (4 valid out of 5 total)
    assert len(task.plan_output.get("findings", [])) == 2
