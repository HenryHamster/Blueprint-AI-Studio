"""Tests for CI auto-remediation (auto_remediation.py + webhook extension in github.py)."""
import hashlib
import hmac
import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Project, Task, TaskStatus
from app.auto_remediation import (
    _handle_main_branch_ci_failure_inner,
    _extract_ci_context,
    _build_remediation_prompt,
    _find_matching_prior_investigation,
    _utcnow,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _create_project(session: AsyncSession, **overrides) -> Project:
    """Create a minimal project for testing."""
    defaults = {
        "name": "Test Repo",
        "user_id": "test-user-123",
        "github_owner": "acme",
        "github_repo": "webapp",
        "design_doc": {"_auto_remediation": {"enabled": True}},
    }
    defaults.update(overrides)
    project = Project(**defaults)
    session.add(project)
    await session.flush()
    return project


async def _create_auto_remediation_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    status: TaskStatus = TaskStatus.IN_PROGRESS,
    sha: str = "abc123",
    created_at_offset: timedelta | None = None,
) -> Task:
    """Create an auto-remediation task for guard-rail testing."""
    task = Task(
        project_id=project_id,
        title="Auto-fix: CI failure on main",
        payloads=[{"type": "create", "prompt": "fix it"}],
        context={
            "auto_remediation": True,
            "ci_failure_sha": sha,
            "ci_failure_branch": "main",
        },
        status=status,
        created_by="SYSTEM_AUTO_REMEDIATION",
    )
    if created_at_offset is not None:
        task.created_at = _utcnow() + created_at_offset
    session.add(task)
    await session.flush()
    return task


def _default_config(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "branch_whitelist": ["main", "master"],
        "max_per_hour": 2,
        "max_concurrent": 2,
    }
    cfg.update(overrides)
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Webhook-level test helpers
# ─────────────────────────────────────────────────────────────────────────────

_TEST_WEBHOOK_SECRET = "test-webhook-secret-for-pytest"


def _build_check_run_webhook_payload(
    owner: str = "acme",
    repo: str = "webapp",
    head_sha: str = "deadbeef1234567890",
    head_branch: str = "main",
    conclusion: str = "failure",
) -> dict:
    """Build a minimal GitHub check_run webhook payload."""
    return {
        "action": "completed",
        "check_run": {
            "head_sha": head_sha,
            "conclusion": conclusion,
            "check_suite": {"head_branch": head_branch},
        },
        "repository": {"full_name": f"{owner}/{repo}"},
    }


def _sign_webhook_payload(payload_bytes: bytes) -> str:
    """Generate the x-hub-signature-256 header value."""
    return "sha256=" + hmac.new(
        _TEST_WEBHOOK_SECRET.encode(), payload_bytes, hashlib.sha256
    ).hexdigest()


async def _count_auto_tasks(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Count tasks with auto_remediation=True in the project."""
    all_tasks = (await session.execute(
        select(Task).where(Task.project_id == project_id)
    )).scalars().all()
    return sum(1 for t in all_tasks if (t.context or {}).get("auto_remediation") is True)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _handle_main_branch_ci_failure_inner
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_spawns_task(db_session: AsyncSession):
    """Basic success: a CI failure on main spawns a fix task."""
    project = await _create_project(db_session)

    with patch("app.auto_remediation._extract_ci_context", new_callable=AsyncMock) as mock_ctx, \
         patch("app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock) as mock_msg, \
         patch("app.main._send_webhook_notification", new_callable=AsyncMock):
        mock_ctx.return_value = (["CI / tests"], "AssertionError: 1 != 2", "test")

        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    assert await _count_auto_tasks(db_session, project.id) == 1

    # Verify the created task
    tasks = (await session_tasks(db_session, project.id))
    task = tasks[0]
    assert task.status == TaskStatus.QUEUED
    assert task.created_by == "SYSTEM_AUTO_REMEDIATION"
    assert task.context["auto_remediation"] is True
    assert task.context["ci_failure_sha"] == "deadbeef"
    assert task.context["ci_failure_branch"] == "main"
    assert task.context["ci_failure_type"] == "test"
    assert task.context["trigger_failure_class"] == "CODE_ERROR"
    assert task.first_payload["type"] == "create"
    assert task.first_payload["base_branch"] == "main"
    assert "deadbeef" in task.first_payload["prompt"]

    # Verify notification was posted with correct branch and checks
    mock_msg.assert_called_once()
    call_kwargs = mock_msg.call_args
    assert call_kwargs[1]["branch"] == "main"
    assert call_kwargs[1]["failed_checks"] == ["CI / tests"]


@pytest.mark.asyncio
async def test_task_context_includes_error_fingerprint(db_session: AsyncSession):
    """Auto-remediation task context should carry _previous_error_fingerprint for cross-direction dedup."""
    from app.anomaly_detection import _error_fingerprint

    project = await _create_project(db_session)
    error_summary = "AssertionError: expected 1 but got 2 in test_foo"

    with patch("app.auto_remediation._extract_ci_context", new_callable=AsyncMock) as mock_ctx, \
         patch("app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock), \
         patch("app.main._send_webhook_notification", new_callable=AsyncMock):
        mock_ctx.return_value = (["CI / tests"], error_summary, "test")

        await _handle_main_branch_ci_failure_inner(
            owner="acme", repo="webapp", head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    tasks = await session_tasks(db_session, project.id)
    assert len(tasks) == 1
    assert tasks[0].context["_previous_error_fingerprint"] == _error_fingerprint(error_summary)


async def session_tasks(session: AsyncSession, project_id: uuid.UUID) -> list[Task]:
    result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
        )
    )
    return [t for t in result.scalars().all() if (t.context or {}).get("auto_remediation")]


@pytest.mark.asyncio
async def test_rate_limit_rejects_third_spawn(db_session: AsyncSession):
    """With max_per_hour=2, a third spawn within the hour is rejected."""
    project = await _create_project(db_session)

    # Create 2 recent auto-remediation tasks (within the last hour)
    await _create_auto_remediation_task(db_session, project.id, sha="sha1", status=TaskStatus.COMPLETED)
    await _create_auto_remediation_task(db_session, project.id, sha="sha2", status=TaskStatus.COMPLETED)
    await db_session.flush()

    with patch("app.auto_remediation._extract_ci_context", new_callable=AsyncMock) as mock_ctx:
        mock_ctx.return_value = (["CI"], "error", "test")
        await _handle_main_branch_ci_failure_inner(
            owner="acme", repo="webapp", head_sha="sha3",
            head_branch="main",
            project_id=project.id,
            config=_default_config(max_per_hour=2),
            session=db_session,
        )

    # Should NOT have spawned a 3rd task
    assert await _count_auto_tasks(db_session, project.id) == 2


@pytest.mark.asyncio
async def test_duplicate_sha_rejected(db_session: AsyncSession):
    """An active task with the same SHA blocks a duplicate spawn."""
    project = await _create_project(db_session)

    # Existing active task for this SHA
    await _create_auto_remediation_task(
        db_session, project.id, sha="deadbeef", status=TaskStatus.IN_PROGRESS,
    )
    await db_session.flush()

    with patch("app.auto_remediation._extract_ci_context", new_callable=AsyncMock) as mock_ctx:
        mock_ctx.return_value = (["CI"], "error", "test")
        await _handle_main_branch_ci_failure_inner(
            owner="acme", repo="webapp", head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    # Only the original task should exist
    assert await _count_auto_tasks(db_session, project.id) == 1


@pytest.mark.asyncio
async def test_concurrent_limit_blocks_spawn(db_session: AsyncSession):
    """With max_concurrent=2, a 3rd concurrent auto-remediation is blocked."""
    project = await _create_project(db_session)

    # Two active auto-remediation tasks
    await _create_auto_remediation_task(
        db_session, project.id, sha="sha1", status=TaskStatus.IN_PROGRESS,
    )
    await _create_auto_remediation_task(
        db_session, project.id, sha="sha2", status=TaskStatus.QUEUED,
    )
    await db_session.flush()

    with patch("app.auto_remediation._extract_ci_context", new_callable=AsyncMock) as mock_ctx:
        mock_ctx.return_value = (["CI"], "error", "test")
        await _handle_main_branch_ci_failure_inner(
            owner="acme", repo="webapp", head_sha="sha3",
            head_branch="main",
            project_id=project.id,
            config=_default_config(max_concurrent=2),
            session=db_session,
        )

    # No new task should have been spawned
    assert await _count_auto_tasks(db_session, project.id) == 2


@pytest.mark.asyncio
async def test_different_sha_same_branch_blocked(db_session: AsyncSession):
    """Dedup is branch-based: a different SHA on the same active branch is blocked."""
    project = await _create_project(db_session)

    await _create_auto_remediation_task(
        db_session, project.id, sha="sha_old", status=TaskStatus.IN_PROGRESS,
    )
    await db_session.flush()

    with patch("app.auto_remediation._extract_ci_context", new_callable=AsyncMock) as mock_ctx:
        mock_ctx.return_value = (["CI / build"], "SyntaxError", "build")
        await _handle_main_branch_ci_failure_inner(
            owner="acme", repo="webapp", head_sha="sha_new",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    # Branch-based dedup: same branch already has an active task — no new spawn
    assert await _count_auto_tasks(db_session, project.id) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Tests: webhook-level filtering (via the check_run handler in github.py)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_protected_branch_ignored(client: AsyncClient, db_session: AsyncSession):
    """CI failure on a non-whitelisted branch is ignored at the webhook level."""
    project = await _create_project(db_session)
    await db_session.commit()

    payload_dict = _build_check_run_webhook_payload(head_branch="feature/xyz", conclusion="failure")
    payload_bytes = json.dumps(payload_dict).encode()
    signature = _sign_webhook_payload(payload_bytes)

    with patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET):
        response = await client.post(
            "/api/github/webhook",
            content=payload_bytes,
            headers={
                "x-hub-signature-256": signature,
                "x-github-event": "check_run",
                "x-github-delivery": f"test-{uuid.uuid4()}",
                "content-type": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored_auto_remediation_disabled"
    # No auto-remediation task should have been created
    assert await _count_auto_tasks(db_session, project.id) == 0


@pytest.mark.asyncio
async def test_check_run_success_ignored(client: AsyncClient, db_session: AsyncSession):
    """check_run with conclusion='success' returns no_match at the webhook level."""
    await _create_project(db_session)
    await db_session.commit()

    payload_dict = _build_check_run_webhook_payload(head_branch="main", conclusion="success")
    payload_bytes = json.dumps(payload_dict).encode()
    signature = _sign_webhook_payload(payload_bytes)

    with patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET):
        response = await client.post(
            "/api/github/webhook",
            content=payload_bytes,
            headers={
                "x-hub-signature-256": signature,
                "x-github-event": "check_run",
                "x-github-delivery": f"test-{uuid.uuid4()}",
                "content-type": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "no_match"


@pytest.mark.asyncio
async def test_completed_tasks_dont_count_concurrent(db_session: AsyncSession):
    """COMPLETED auto-remediation tasks don't count toward concurrent limit."""
    project = await _create_project(db_session)

    # Create 2 completed tasks — these should NOT block new spawns
    await _create_auto_remediation_task(
        db_session, project.id, sha="sha1", status=TaskStatus.COMPLETED,
    )
    await _create_auto_remediation_task(
        db_session, project.id, sha="sha2", status=TaskStatus.FAILED,
    )
    await db_session.flush()

    with patch("app.auto_remediation._extract_ci_context", new_callable=AsyncMock) as mock_ctx, \
         patch("app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock), \
         patch("app.main._send_webhook_notification", new_callable=AsyncMock):
        mock_ctx.return_value = (["CI"], "error", "test")
        await _handle_main_branch_ci_failure_inner(
            owner="acme", repo="webapp", head_sha="sha3",
            head_branch="main",
            project_id=project.id,
            config=_default_config(max_concurrent=2, max_per_hour=10),
            session=db_session,
        )

    # New task should have been spawned (completed tasks don't count as concurrent)
    assert await _count_auto_tasks(db_session, project.id) == 3


# ─────────────────────────────────────────────────────────────────────────────
# Tests: CI context extraction
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_ci_context_test_failure():
    """Logs containing 'FAILED' should be classified as 'test'."""
    mock_ci_result = {
        "status": "completed",
        "conclusion": "failure",
        "details": [
            {"name": "CI / tests", "status": "completed", "conclusion": "failure"},
            {"name": "CI / lint", "status": "completed", "conclusion": "success"},
        ],
        "failed_logs": "FAILED tests/test_main.py::test_foo - AssertionError: 1 != 2\n2 failed",
    }

    with patch("app.auto_remediation.get_check_runs_cached", new_callable=AsyncMock) as mock_runs, \
         patch("app.auto_remediation.get_repo_installation_token", new_callable=AsyncMock) as mock_token:
        mock_token.return_value = "ghp_test_token"
        mock_runs.return_value = mock_ci_result

        failed_checks, error_summary, failure_type = await _extract_ci_context(
            "acme", "webapp", "deadbeef", uuid.uuid4(), AsyncMock(),
        )

    assert failed_checks == ["CI / tests"]
    assert "AssertionError" in error_summary
    assert failure_type == "test"


@pytest.mark.asyncio
async def test_extract_ci_context_build_failure():
    """Logs containing 'ImportError' should be classified as 'build'."""
    mock_ci_result = {
        "status": "completed",
        "conclusion": "failure",
        "details": [
            {"name": "CI / build", "status": "completed", "conclusion": "failure"},
        ],
        "failed_logs": "ImportError: No module named 'missing_dep'\nBuild failed.",
    }

    with patch("app.auto_remediation.get_check_runs_cached", new_callable=AsyncMock) as mock_runs, \
         patch("app.auto_remediation.get_repo_installation_token", new_callable=AsyncMock) as mock_token:
        mock_token.return_value = "ghp_test_token"
        mock_runs.return_value = mock_ci_result

        failed_checks, _error_summary, failure_type = await _extract_ci_context(
            "acme", "webapp", "deadbeef", uuid.uuid4(), AsyncMock(),
        )

    assert failed_checks == ["CI / build"]
    assert failure_type == "build"


@pytest.mark.asyncio
async def test_extract_ci_context_no_token():
    """When no GitHub token is available, returns empty defaults."""
    with patch("app.auto_remediation.get_repo_installation_token", new_callable=AsyncMock) as mock_token:
        mock_token.return_value = None

        failed_checks, error_summary, failure_type = await _extract_ci_context(
            "acme", "webapp", "deadbeef", uuid.uuid4(), AsyncMock(),
        )

    assert failed_checks == []
    assert error_summary == ""
    assert failure_type == "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: prompt building
# ─────────────────────────────────────────────────────────────────────────────


def test_build_remediation_prompt_includes_context():
    """The remediation prompt should include branch, SHA, checks, and error logs."""
    prompt = _build_remediation_prompt(
        head_branch="main",
        head_sha="abc123def456",
        failed_checks=["CI / tests", "CI / lint"],
        error_summary="AssertionError: expected True",
        failure_type="test",
    )
    assert "main" in prompt
    assert "abc123def456" in prompt
    assert "CI / tests" in prompt
    assert "CI / lint" in prompt
    assert "AssertionError" in prompt
    assert "fix branch" in prompt.lower() or "Fix" in prompt
    assert "test" in prompt
    assert "Failure classification" in prompt


def test_build_remediation_prompt_empty_checks():
    """Prompt handles missing check names gracefully."""
    prompt = _build_remediation_prompt(
        head_branch="master",
        head_sha="deadbeef",
        failed_checks=[],
        error_summary="",
        failure_type="unknown",
    )
    assert "master" in prompt
    assert "not available" in prompt.lower() or "no error" in prompt.lower()
    assert "unknown" in prompt


# ─────────────────────────────────────────────────────────────────────────────
# Tests: suspension guard
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_suspended_branch_is_blocked(db_session: AsyncSession):
    """A branch in suspended_branches is rejected before any other guard."""
    project = await _create_project(db_session)

    with patch("app.auto_remediation._extract_ci_context", new_callable=AsyncMock) as mock_ctx:
        mock_ctx.return_value = (["CI"], "error", "test")
        await _handle_main_branch_ci_failure_inner(
            owner="acme", repo="webapp", head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=_default_config(suspended_branches=["main"]),
            session=db_session,
        )

    # Suspended branch: no task should have been spawned
    assert await _count_auto_tasks(db_session, project.id) == 0
    # CI context extraction should not even be called
    mock_ctx.assert_not_called()


@pytest.mark.asyncio
async def test_skip_when_no_ci_context(db_session: AsyncSession):
    """No task is spawned when CI context extraction returns no useful information."""
    project = await _create_project(db_session)

    with patch("app.auto_remediation._extract_ci_context", new_callable=AsyncMock) as mock_ctx, \
         patch("app.main._send_webhook_notification", new_callable=AsyncMock):
        # Return the "no useful context" sentinel values
        mock_ctx.return_value = ([], "", "unknown")
        await _handle_main_branch_ci_failure_inner(
            owner="acme", repo="webapp", head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    # No task should have been spawned
    assert await _count_auto_tasks(db_session, project.id) == 0


@pytest.mark.asyncio
async def test_non_suspended_branch_spawns_normally(db_session: AsyncSession):
    """A branch not in suspended_branches proceeds normally."""
    project = await _create_project(db_session)

    with patch("app.auto_remediation._extract_ci_context", new_callable=AsyncMock) as mock_ctx, \
         patch("app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock), \
         patch("app.main._send_webhook_notification", new_callable=AsyncMock):
        mock_ctx.return_value = (["CI / tests"], "error", "test")
        await _handle_main_branch_ci_failure_inner(
            owner="acme", repo="webapp", head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=_default_config(suspended_branches=["develop"]),
            session=db_session,
        )

    assert await _count_auto_tasks(db_session, project.id) == 1


@pytest.mark.asyncio
async def test_full_ci_remediation_cycle_spawn_to_trackable_outcome(db_session: AsyncSession):
    """End-to-end: CI failure spawns task → task completes → outcome is trackable by trigger class."""
    project = await _create_project(db_session)

    with patch("app.auto_remediation._extract_ci_context", new_callable=AsyncMock) as mock_ctx, \
         patch("app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock), \
         patch("app.main._send_webhook_notification", new_callable=AsyncMock):
        mock_ctx.return_value = (["CI / tests"], "AssertionError: boom", "test")
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="cafed00d",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    spawned = (await session_tasks(db_session, project.id))
    assert len(spawned) == 1
    task = spawned[0]

    # (2) Verify context fields required for downstream tracking
    assert task.context["auto_remediation"] is True
    assert task.context["trigger_failure_class"] == "CODE_ERROR"
    assert task.context["ci_failure_type"] == "test"
    assert task.context["ci_failure_sha"] == "cafed00d"
    assert task.status == TaskStatus.QUEUED

    # (3) Simulate successful merge
    task.status = TaskStatus.COMPLETED
    await db_session.flush()

    # (4) Verify the task is queryable as a completed CI auto-remediation and
    # aggregatable by trigger_failure_class — the shape the digest funnel relies on.
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status == TaskStatus.COMPLETED,
        )
    )
    completed = [t for t in result.scalars().all() if (t.context or {}).get("auto_remediation") is True]
    assert len(completed) == 1

    by_trigger: dict[str, int] = {}
    for t in completed:
        cls = (t.context or {}).get("trigger_failure_class") or "unknown"
        by_trigger[cls] = by_trigger.get(cls, 0) + 1
    assert by_trigger == {"CODE_ERROR": 1}


@pytest.mark.asyncio
async def test_remediation_spawn_to_feedback_clears_escalation_state(db_session: AsyncSession):
    """End-to-end: spawn → merged feedback clears per-branch failure counts and suspension."""
    from sqlalchemy.orm.attributes import flag_modified
    from app.notification_dispatch import post_auto_fix_result_message

    project = await _create_project(
        db_session,
        design_doc={
            "_auto_remediation": {
                "enabled": True,
                "branch_failure_counts": {"main": 2},
                "suspended_branches": ["main"],
            }
        },
    )

    # Unsuspend 'main' so spawn can proceed; prior failure count persists.
    ar = dict(project.design_doc["_auto_remediation"])
    ar["suspended_branches"] = []
    project.design_doc = {**project.design_doc, "_auto_remediation": ar}
    flag_modified(project, "design_doc")
    await db_session.flush()

    with patch("app.auto_remediation._extract_ci_context", new_callable=AsyncMock) as mock_ctx, \
         patch("app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock), \
         patch("app.main._send_webhook_notification", new_callable=AsyncMock):
        mock_ctx.return_value = (["CI / tests"], "AssertionError: boom", "test")
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    spawned = await session_tasks(db_session, project.id)
    assert len(spawned) == 1
    task = spawned[0]

    # Re-suspend and restore the prior-failure count to simulate state at merge time.
    ar = dict(project.design_doc["_auto_remediation"])
    ar["branch_failure_counts"] = {"main": 2}
    ar["suspended_branches"] = ["main"]
    project.design_doc = {**project.design_doc, "_auto_remediation": ar}
    flag_modified(project, "design_doc")
    await db_session.flush()

    await post_auto_fix_result_message(
        db_session,
        project.id,
        branch="main",
        result="merged",
        pr_number=999,
        task_id=task.id,
    )
    ar_after = (project.design_doc or {}).get("_auto_remediation", {})
    assert "main" not in ar_after.get("branch_failure_counts", {})
    assert "main" not in ar_after.get("suspended_branches", [])


@pytest.mark.asyncio
async def test_pipeline_exhaustion_propagates_to_auto_remediation_branch(db_session: AsyncSession):
    """When a review/fix task fails permanently on an auto-remediation PR, the
    original create task's auto_remediation context drives post_auto_fix_result_message
    with result='failed' so the branch's failure counter advances toward suspension."""
    from app.routers.agent_status import _propagate_pipeline_failure_to_original

    project = await _create_project(db_session)
    await db_session.flush()

    original = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "auto-fix CI", "base_branch": "main"}],
        context={
            "auto_remediation": True,
            "ci_failure_sha": "abc123",
            "ci_failure_branch": "main",
        },
        status=TaskStatus.COMPLETED,
        created_by="SYSTEM_AUTO_REMEDIATION",
    )
    db_session.add(original)
    await db_session.flush()

    review = Task(
        project_id=project.id,
        payloads=[{
            "type": "review",
            "pr_number": 9999,
            "branch": "blueprint/auto-fix",
            "original_task_id": str(original.id),
        }],
        context={"review_loop": {"original_task_id": str(original.id), "pr_number": 9999}},
        status=TaskStatus.FAILED,
    )
    db_session.add(review)
    await db_session.flush()

    with patch(
        "app.notification_dispatch.post_auto_fix_result_message",
        new_callable=AsyncMock,
    ) as mock_post, patch(
        "app.routers.agent_status._check_and_trigger_rollup",
        new_callable=AsyncMock,
    ):
        await _propagate_pipeline_failure_to_original(review, db_session)
        assert mock_post.await_count == 1
        kwargs = mock_post.await_args.kwargs
        assert kwargs["branch"] == "main"
        assert kwargs["result"] == "failed"
        assert kwargs["task_id"] == original.id

    await db_session.refresh(original)
    assert original.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_pipeline_exhaustion_skips_non_remediation_tasks(db_session: AsyncSession):
    """Non-auto-remediation originals still get cancelled, but no auto-fix failure message is posted."""
    from app.routers.agent_status import _propagate_pipeline_failure_to_original

    project = await _create_project(db_session)
    await db_session.flush()

    original = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "regular work"}],
        context={},
        status=TaskStatus.COMPLETED,
    )
    db_session.add(original)
    await db_session.flush()

    review = Task(
        project_id=project.id,
        payloads=[{
            "type": "review",
            "pr_number": 42,
            "branch": "blueprint/task-x",
            "original_task_id": str(original.id),
        }],
        context={"review_loop": {"original_task_id": str(original.id), "pr_number": 42}},
        status=TaskStatus.FAILED,
    )
    db_session.add(review)
    await db_session.flush()

    with patch(
        "app.notification_dispatch.post_auto_fix_result_message",
        new_callable=AsyncMock,
    ) as mock_post, patch(
        "app.routers.agent_status._check_and_trigger_rollup",
        new_callable=AsyncMock,
    ):
        await _propagate_pipeline_failure_to_original(review, db_session)
        mock_post.assert_not_awaited()

    await db_session.refresh(original)
    assert original.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_find_matching_prior_investigation_matches_on_file_path(db_session: AsyncSession):
    """A recent completed investigate task whose affected file appears in the error summary matches."""
    project = await _create_project(db_session)
    inv = Task(
        project_id=project.id,
        title="Investigate CI",
        payloads=[{"type": "investigate"}],
        status=TaskStatus.COMPLETED,
        plan_output={
            "summary": "flaky auth middleware test",
            "root_cause": "session token mismatch",
            "affected_files": [{"path": "backend/app/auth.py"}],
            "recommended_fix": "reset session cache",
        },
    )
    db_session.add(inv)
    await db_session.flush()

    match = await _find_matching_prior_investigation(
        project_id=project.id,
        error_summary="FAILED backend/app/auth.py::test_session something",
        session=db_session,
    )
    assert match is not None and match.id == inv.id


@pytest.mark.asyncio
async def test_find_matching_prior_investigation_no_match_returns_none(db_session: AsyncSession):
    """Empty error summary or no matching investigation returns None."""
    project = await _create_project(db_session)
    assert await _find_matching_prior_investigation(
        project_id=project.id, error_summary="", session=db_session,
    ) is None
    assert await _find_matching_prior_investigation(
        project_id=project.id, error_summary="unrelated failure xyz",
        session=db_session,
    ) is None
