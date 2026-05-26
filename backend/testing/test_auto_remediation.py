"""Tests for CI auto-remediation (auto_remediation.py + webhook extension in github.py)."""

import hashlib
import hmac
import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Project, Task, TaskStatus
from app.auto_remediation import (
    _handle_main_branch_ci_failure_inner,
    _extract_ci_context,
    _extract_structured_signals,
    _build_remediation_prompt,
    _find_direction_by_fingerprint,
    _find_matching_prior_investigation,
    _find_recent_merged_remediation,
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
    """Create an auto-remediation task for guard-rail testing.

    Audit W1: auto_remediation is a column now.
    """
    task = Task(
        project_id=project_id,
        title="Auto-fix: CI failure on main",
        payloads=[{"type": "create", "prompt": "fix it"}],
        auto_remediation=True,
        context={
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
    return (
        "sha256="
        + hmac.new(
            _TEST_WEBHOOK_SECRET.encode(), payload_bytes, hashlib.sha256
        ).hexdigest()
    )


async def _count_auto_tasks(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Count tasks with auto_remediation=True in the project.

    Audit W1: auto_remediation is now a column (was JSONB context key).
    """
    return (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == project_id,
                Task.auto_remediation.is_(True),
            )
        )
    ).scalar() or 0


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _handle_main_branch_ci_failure_inner
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_spawns_task(db_session: AsyncSession):
    """Basic success: a CI failure on main spawns a fix task."""
    project = await _create_project(db_session)

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch(
            "app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock
        ) as mock_msg,
        patch("app.main._send_webhook_notification", new_callable=AsyncMock),
    ):
        mock_ctx.return_value = (
            ["CI / tests"],
            "AssertionError: 1 != 2",
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )

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
    tasks = await session_tasks(db_session, project.id)
    task = tasks[0]
    assert task.status == TaskStatus.QUEUED
    assert task.created_by == "SYSTEM_AUTO_REMEDIATION"
    assert task.auto_remediation is True  # audit W1: column source of truth
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

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch(
            "app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock
        ),
        patch("app.main._send_webhook_notification", new_callable=AsyncMock),
    ):
        mock_ctx.return_value = (
            ["CI / tests"],
            error_summary,
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )

        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    tasks = await session_tasks(db_session, project.id)
    assert len(tasks) == 1
    # Audit W1: previous_error_fingerprint is now a column (was JSONB key).
    # Fingerprint is stage-aware: "AssertionError ... test_foo" → stage="test".
    assert tasks[0].previous_error_fingerprint == _error_fingerprint(
        error_summary, "test"
    )


async def session_tasks(session: AsyncSession, project_id: uuid.UUID) -> list[Task]:
    # Audit W1: filter on the column directly.
    result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.auto_remediation.is_(True),
        )
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_rate_limit_rejects_third_spawn(db_session: AsyncSession):
    """With max_per_hour=2, a third spawn within the hour is rejected."""
    project = await _create_project(db_session)

    # Create 2 recent auto-remediation tasks (within the last hour)
    await _create_auto_remediation_task(
        db_session, project.id, sha="sha1", status=TaskStatus.COMPLETED
    )
    await _create_auto_remediation_task(
        db_session, project.id, sha="sha2", status=TaskStatus.COMPLETED
    )
    await db_session.flush()

    with patch(
        "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
    ) as mock_ctx:
        mock_ctx.return_value = (
            ["CI"],
            "error",
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="sha3",
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
        db_session,
        project.id,
        sha="deadbeef",
        status=TaskStatus.IN_PROGRESS,
    )
    await db_session.flush()

    with patch(
        "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
    ) as mock_ctx:
        mock_ctx.return_value = (
            ["CI"],
            "error",
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="deadbeef",
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
        db_session,
        project.id,
        sha="sha1",
        status=TaskStatus.IN_PROGRESS,
    )
    await _create_auto_remediation_task(
        db_session,
        project.id,
        sha="sha2",
        status=TaskStatus.QUEUED,
    )
    await db_session.flush()

    with patch(
        "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
    ) as mock_ctx:
        mock_ctx.return_value = (
            ["CI"],
            "error",
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="sha3",
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
        db_session,
        project.id,
        sha="sha_old",
        status=TaskStatus.IN_PROGRESS,
    )
    await db_session.flush()

    with patch(
        "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
    ) as mock_ctx:
        mock_ctx.return_value = (
            ["CI / build"],
            "SyntaxError",
            "build",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="sha_new",
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
async def test_non_protected_branch_ignored(
    client: AsyncClient, db_session: AsyncSession
):
    """CI failure on a non-whitelisted branch is ignored at the webhook level."""
    project = await _create_project(db_session)
    await db_session.commit()

    payload_dict = _build_check_run_webhook_payload(
        head_branch="feature/xyz", conclusion="failure"
    )
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

    payload_dict = _build_check_run_webhook_payload(
        head_branch="main", conclusion="success"
    )
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
async def test_integration_branch_triggers_auto_remediation(
    client: AsyncClient, db_session: AsyncSession
):
    """CI failure on the configured integration branch triggers auto-remediation
    even when that branch is NOT in the stored branch_whitelist."""
    await _create_project(db_session, integration_branch="automain")
    await db_session.commit()

    payload_dict = _build_check_run_webhook_payload(
        head_branch="automain", conclusion="failure"
    )
    payload_bytes = json.dumps(payload_dict).encode()
    signature = _sign_webhook_payload(payload_bytes)

    with (
        patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET),
        patch("app.auto_remediation._handle_main_branch_ci_failure_inner", AsyncMock()),
    ):
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
    assert response.json()["status"] == "auto_remediation_triggered"


@pytest.mark.asyncio
async def test_feature_branch_not_in_integration_branch_ignored(
    client: AsyncClient, db_session: AsyncSession
):
    """CI failure on an unrelated feature branch (not in whitelist, not the
    integration branch) still returns ignored_auto_remediation_disabled."""
    await _create_project(db_session, integration_branch="automain")
    await db_session.commit()

    payload_dict = _build_check_run_webhook_payload(
        head_branch="feature/unrelated", conclusion="failure"
    )
    payload_bytes = json.dumps(payload_dict).encode()
    signature = _sign_webhook_payload(payload_bytes)

    with (
        patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET),
    ):
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


@pytest.mark.asyncio
async def test_completed_tasks_dont_count_concurrent(db_session: AsyncSession):
    """COMPLETED auto-remediation tasks don't count toward concurrent limit."""
    project = await _create_project(db_session)

    # Create 2 completed tasks — these should NOT block new spawns
    await _create_auto_remediation_task(
        db_session,
        project.id,
        sha="sha1",
        status=TaskStatus.COMPLETED,
    )
    await _create_auto_remediation_task(
        db_session,
        project.id,
        sha="sha2",
        status=TaskStatus.FAILED,
    )
    await db_session.flush()

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch(
            "app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock
        ),
        patch("app.main._send_webhook_notification", new_callable=AsyncMock),
    ):
        mock_ctx.return_value = (
            ["CI"],
            "error",
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="sha3",
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

    with (
        patch(
            "app.auto_remediation.get_check_runs_cached", new_callable=AsyncMock
        ) as mock_runs,
        patch(
            "app.auto_remediation.get_repo_installation_token", new_callable=AsyncMock
        ) as mock_token,
    ):
        mock_token.return_value = "ghp_test_token"
        mock_runs.return_value = mock_ci_result

        failed_checks, error_summary, failure_type, signals = await _extract_ci_context(
            "acme",
            "webapp",
            "deadbeef",
            uuid.uuid4(),
            AsyncMock(),
        )

    assert failed_checks == ["CI / tests"]
    assert "AssertionError" in error_summary
    assert failure_type == "test"
    assert "tests/test_main.py::test_foo" in signals["failing_tests"]


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

    with (
        patch(
            "app.auto_remediation.get_check_runs_cached", new_callable=AsyncMock
        ) as mock_runs,
        patch(
            "app.auto_remediation.get_repo_installation_token", new_callable=AsyncMock
        ) as mock_token,
    ):
        mock_token.return_value = "ghp_test_token"
        mock_runs.return_value = mock_ci_result

        (
            failed_checks,
            _error_summary,
            failure_type,
            _signals,
        ) = await _extract_ci_context(
            "acme",
            "webapp",
            "deadbeef",
            uuid.uuid4(),
            AsyncMock(),
        )

    assert failed_checks == ["CI / build"]
    assert failure_type == "build"


@pytest.mark.asyncio
async def test_extract_ci_context_migration_failure():
    """Logs containing alembic/schema patterns should be classified as 'migration'."""
    mock_ci_result = {
        "status": "completed",
        "conclusion": "failure",
        "details": [
            {"name": "CI / tests", "status": "completed", "conclusion": "failure"},
        ],
        "failed_logs": "alembic.util.exc.CommandError: Can't locate revision identified by 'abc123'\nFAILED",
    }

    with (
        patch(
            "app.auto_remediation.get_check_runs_cached", new_callable=AsyncMock
        ) as mock_runs,
        patch(
            "app.auto_remediation.get_repo_installation_token", new_callable=AsyncMock
        ) as mock_token,
    ):
        mock_token.return_value = "ghp_test_token"
        mock_runs.return_value = mock_ci_result

        _failed, _summary, failure_type, _signals = await _extract_ci_context(
            "acme",
            "webapp",
            "deadbeef",
            uuid.uuid4(),
            AsyncMock(),
        )

    assert failure_type == "migration"


@pytest.mark.asyncio
async def test_extract_ci_context_migration_undefined_column():
    """UndefinedColumn errors should classify as 'migration', not 'build'."""
    mock_ci_result = {
        "status": "completed",
        "conclusion": "failure",
        "details": [
            {"name": "CI / tests", "status": "completed", "conclusion": "failure"}
        ],
        "failed_logs": 'psycopg2.errors.UndefinedColumn: column "foo" does not exist\nImportError ignored',
    }

    with (
        patch(
            "app.auto_remediation.get_check_runs_cached", new_callable=AsyncMock
        ) as mock_runs,
        patch(
            "app.auto_remediation.get_repo_installation_token", new_callable=AsyncMock
        ) as mock_token,
    ):
        mock_token.return_value = "ghp_test_token"
        mock_runs.return_value = mock_ci_result

        _failed, _summary, failure_type, _signals = await _extract_ci_context(
            "acme",
            "webapp",
            "deadbeef",
            uuid.uuid4(),
            AsyncMock(),
        )

    assert failure_type == "migration"


def test_build_remediation_prompt_migration_guidance():
    """Migration failure_type should emit alembic/EXPECTED_SCHEMA_REVISION guidance."""
    prompt = _build_remediation_prompt(
        head_branch="main",
        head_sha="abc",
        failed_checks=["CI / tests"],
        error_summary="alembic revision error",
        failure_type="migration",
    )
    assert "alembic" in prompt.lower()
    assert "EXPECTED_SCHEMA_REVISION" in prompt
    assert "down_revision" in prompt


def test_build_remediation_prompt_migration_extracts_schema_mismatch():
    """Schema version mismatch error yields concrete revision IDs in guidance."""
    prompt = _build_remediation_prompt(
        head_branch="main",
        head_sha="abc",
        failed_checks=["CI / tests"],
        error_summary="Schema version mismatch: database at '0037', code expects '0038'",
        failure_type="migration",
    )
    assert "0037" in prompt
    assert "0038" in prompt
    assert "EXPECTED_SCHEMA_REVISION" in prompt


def test_build_remediation_prompt_migration_extracts_missing_revision():
    """can't locate revision errors surface the missing revision ID."""
    prompt = _build_remediation_prompt(
        head_branch="main",
        head_sha="abc",
        failed_checks=["CI / tests"],
        error_summary="alembic.util.exc.CommandError: Can't locate revision identified by '0042'",
        failure_type="migration",
    )
    assert "0042" in prompt
    assert "Missing revision" in prompt or "missing revision" in prompt.lower()


def test_build_remediation_prompt_migration_extracts_multiple_heads():
    """Multiple head revisions error notes the multi-head condition."""
    prompt = _build_remediation_prompt(
        head_branch="main",
        head_sha="abc",
        failed_checks=["CI / tests"],
        error_summary="Multiple head revisions are present for given argument 'head'; revisions 0038 and 0039 both head",
        failure_type="migration",
    )
    assert "Multiple alembic heads" in prompt
    assert "0038" in prompt and "0039" in prompt


@pytest.mark.asyncio
async def test_extract_ci_context_no_token():
    """When no GitHub token is available, returns empty defaults."""
    with patch(
        "app.auto_remediation.get_repo_installation_token", new_callable=AsyncMock
    ) as mock_token:
        mock_token.return_value = None

        failed_checks, error_summary, failure_type, signals = await _extract_ci_context(
            "acme",
            "webapp",
            "deadbeef",
            uuid.uuid4(),
            AsyncMock(),
        )

    assert failed_checks == []
    assert error_summary == ""
    assert failure_type == "unknown"
    assert signals == {"failing_tests": [], "error_locations": [], "error_messages": []}


@pytest.mark.asyncio
async def test_extract_ci_context_skips_failed_check_without_name():
    """Malformed CI details should not collapse the whole extraction result."""
    mock_ci_result = {
        "status": "completed",
        "conclusion": "failure",
        "details": [
            {"status": "completed", "conclusion": "failure"},
            {"name": "CI / lint", "status": "completed", "conclusion": "failure"},
        ],
        "failed_logs": "src/App.tsx:12:5 error: lint failed",
    }

    with (
        patch(
            "app.auto_remediation.get_check_runs_cached", new_callable=AsyncMock
        ) as mock_runs,
        patch(
            "app.auto_remediation.get_repo_installation_token", new_callable=AsyncMock
        ) as mock_token,
    ):
        mock_token.return_value = "ghp_test_token"
        mock_runs.return_value = mock_ci_result

        failed_checks, error_summary, failure_type, signals = await _extract_ci_context(
            "acme",
            "webapp",
            "deadbeef",
            uuid.uuid4(),
            AsyncMock(),
        )

    assert failed_checks == ["CI / lint"]
    assert error_summary == "src/App.tsx:12:5 error: lint failed"
    assert failure_type == "lint"
    assert "src/App.tsx:12" in signals["error_locations"]


def test_extract_structured_signals_pytest():
    """Pytest FAILED lines and Python tracebacks are extracted even past truncation."""
    logs = (
        "collected 5 items\n"
        "FAILED tests/test_auth.py::test_login - AssertionError: 401 != 200\n"
        "FAILED tests/test_auth.py::test_logout - KeyError: 'session'\n"
        "Traceback (most recent call last):\n"
        '  File "backend/app/auth.py", line 142, in login\n'
        '    raise AssertionError("bad creds")\n'
        "AssertionError: bad creds\n"
    )
    signals = _extract_structured_signals(logs)
    assert "tests/test_auth.py::test_login" in signals["failing_tests"]
    assert "tests/test_auth.py::test_logout" in signals["failing_tests"]
    assert "backend/app/auth.py:142" in signals["error_locations"]
    assert any("AssertionError" in m for m in signals["error_messages"])
    assert any("KeyError" in m for m in signals["error_messages"])


def test_extract_structured_signals_tsc_eslint():
    """TypeScript / ESLint-style file:line output is extracted."""
    logs = (
        "admin-ui/src/App.tsx:45:12 - error TS2304: Cannot find name 'Foo'.\n"
        "admin-ui/src/components/Bar.tsx:10:5  error  'x' is not defined  no-undef\n"
    )
    signals = _extract_structured_signals(logs)
    assert "admin-ui/src/App.tsx:45" in signals["error_locations"]
    assert "admin-ui/src/components/Bar.tsx:10" in signals["error_locations"]
    assert any("TS2304" in m for m in signals["error_messages"])


def test_extract_structured_signals_survives_truncation():
    """Signals from past the 3000-char cutoff still appear in the structured result."""
    pad = "noise line\n" * 400  # ~4400 chars of noise
    logs = pad + "FAILED tests/late.py::test_tail - AssertionError: boom\n"
    signals = _extract_structured_signals(logs)
    assert "tests/late.py::test_tail" in signals["failing_tests"]


def test_build_remediation_prompt_includes_structured_signals():
    """Structured signals render as a dedicated section separate from raw logs."""
    prompt = _build_remediation_prompt(
        head_branch="main",
        head_sha="abc",
        failed_checks=["CI / tests"],
        error_summary="truncated raw output",
        failure_type="test",
        structured_signals={
            "failing_tests": ["tests/test_x.py::test_y"],
            "error_locations": ["backend/app/foo.py:42"],
            "error_messages": ["AssertionError: nope"],
        },
    )
    assert "## Structured Failure Signals" in prompt
    assert "tests/test_x.py::test_y" in prompt
    assert "backend/app/foo.py:42" in prompt
    assert "AssertionError: nope" in prompt


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

    with patch(
        "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
    ) as mock_ctx:
        mock_ctx.return_value = (
            ["CI"],
            "error",
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="deadbeef",
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

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch("app.main._send_webhook_notification", new_callable=AsyncMock),
    ):
        # Return the "no useful context" sentinel values
        mock_ctx.return_value = (
            [],
            "",
            "unknown",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    # No task should have been spawned
    assert await _count_auto_tasks(db_session, project.id) == 0


@pytest.mark.asyncio
async def test_suspended_branch_auto_recovers_after_cooldown(db_session: AsyncSession):
    """Branch is auto-recovered when suspension cooldown has elapsed."""
    suspended_at = (_utcnow() - timedelta(hours=25)).isoformat()
    project = await _create_project(
        db_session,
        design_doc={
            "_auto_remediation": {
                "enabled": True,
                "branch_failure_counts": {"main": 3},
                "suspended_branches": ["main"],
                "suspension_metadata": {
                    "main": {"suspended_at": suspended_at, "recovery_attempts": 0},
                },
            }
        },
    )

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch(
            "app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock
        ),
        patch("app.main._send_webhook_notification", new_callable=AsyncMock),
    ):
        mock_ctx.return_value = (
            ["CI / tests"],
            "error",
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=(project.design_doc or {}).get("_auto_remediation", {}),
            session=db_session,
        )

    # Task should spawn and suspension should be lifted with attempts bumped
    assert await _count_auto_tasks(db_session, project.id) == 1
    ar = (project.design_doc or {}).get("_auto_remediation", {})
    assert "main" not in ar.get("suspended_branches", [])
    assert (
        ar.get("suspension_metadata", {}).get("main", {}).get("recovery_attempts") == 1
    )


@pytest.mark.asyncio
async def test_suspended_branch_within_cooldown_stays_blocked(db_session: AsyncSession):
    """Suspension is enforced while cooldown has not elapsed."""
    suspended_at = (_utcnow() - timedelta(hours=1)).isoformat()
    project = await _create_project(
        db_session,
        design_doc={
            "_auto_remediation": {
                "enabled": True,
                "suspended_branches": ["main"],
                "suspension_metadata": {
                    "main": {"suspended_at": suspended_at, "recovery_attempts": 0},
                },
            }
        },
    )

    with patch(
        "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
    ) as mock_ctx:
        mock_ctx.return_value = (
            ["CI"],
            "error",
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=(project.design_doc or {}).get("_auto_remediation", {}),
            session=db_session,
        )

    assert await _count_auto_tasks(db_session, project.id) == 0
    mock_ctx.assert_not_called()


@pytest.mark.asyncio
async def test_suspended_branch_permanently_blocked_after_max_attempts(
    db_session: AsyncSession,
):
    """After MAX_AUTO_RECOVERY_ATTEMPTS, branch stays suspended even past cooldown."""
    from app.auto_remediation import MAX_AUTO_RECOVERY_ATTEMPTS

    suspended_at = (_utcnow() - timedelta(days=365)).isoformat()
    project = await _create_project(
        db_session,
        design_doc={
            "_auto_remediation": {
                "enabled": True,
                "suspended_branches": ["main"],
                "suspension_metadata": {
                    "main": {
                        "suspended_at": suspended_at,
                        "recovery_attempts": MAX_AUTO_RECOVERY_ATTEMPTS,
                    },
                },
            }
        },
    )

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch(
            "app.main._send_webhook_notification", new_callable=AsyncMock
        ) as mock_webhook,
    ):
        mock_ctx.return_value = (
            ["CI"],
            "error",
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=(project.design_doc or {}).get("_auto_remediation", {}),
            session=db_session,
        )

    assert await _count_auto_tasks(db_session, project.id) == 0
    mock_ctx.assert_not_called()
    # Permanent-escalation webhook should fire
    assert any(
        "PERMANENT" in (call.args[0] if call.args else "")
        for call in mock_webhook.call_args_list
    )


@pytest.mark.asyncio
async def test_non_suspended_branch_spawns_normally(db_session: AsyncSession):
    """A branch not in suspended_branches proceeds normally."""
    project = await _create_project(db_session)

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch(
            "app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock
        ),
        patch("app.main._send_webhook_notification", new_callable=AsyncMock),
    ):
        mock_ctx.return_value = (
            ["CI / tests"],
            "error",
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=_default_config(suspended_branches=["develop"]),
            session=db_session,
        )

    assert await _count_auto_tasks(db_session, project.id) == 1


@pytest.mark.asyncio
async def test_per_fingerprint_cap_blocks_repeat_spawn(db_session: AsyncSession):
    """Three failed auto-remediations sharing a fingerprint block a fourth spawn."""
    from app.anomaly_detection import _error_fingerprint
    from app.failure_stage import _derive_failure_stage

    project = await _create_project(db_session)
    err = "AssertionError: boom"
    # Production stamps the stage-aware fingerprint at auto_remediation.py
    # (stage = _derive_failure_stage("CODE_ERROR", err)). The cap probe must
    # mirror that exactly — otherwise the test would only exercise a mode
    # production never produces.
    stage = _derive_failure_stage("CODE_ERROR", err)
    fp = _error_fingerprint(err, stage)
    for _ in range(3):
        # Audit W1: auto_remediation + previous_error_fingerprint are columns.
        t = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "x"}],
            auto_remediation=True,
            previous_error_fingerprint=fp,
            context={
                "ci_failure_branch": "main",
            },
            status=TaskStatus.FAILED,
            created_by="SYSTEM_AUTO_REMEDIATION",
        )
        db_session.add(t)
    await db_session.flush()

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch(
            "app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock
        ),
        patch("app.main._send_webhook_notification", new_callable=AsyncMock),
    ):
        mock_ctx.return_value = (
            ["CI / tests"],
            err,
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    # No new auto-remediation task spawned (only the 3 pre-existing FAILED tasks).
    spawned = await session_tasks(db_session, project.id)
    assert all(t.status == TaskStatus.FAILED for t in spawned)
    assert len(spawned) == 3


@pytest.mark.asyncio
async def test_full_ci_remediation_cycle_spawn_to_trackable_outcome(
    db_session: AsyncSession,
):
    """End-to-end: CI failure spawns task → task completes → outcome is trackable by trigger class."""
    project = await _create_project(db_session)

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch(
            "app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock
        ),
        patch("app.main._send_webhook_notification", new_callable=AsyncMock),
    ):
        mock_ctx.return_value = (
            ["CI / tests"],
            "AssertionError: boom",
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="cafed00d",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    spawned = await session_tasks(db_session, project.id)
    assert len(spawned) == 1
    task = spawned[0]

    # (2) Verify context fields required for downstream tracking
    assert task.auto_remediation is True  # audit W1: column source of truth
    assert task.context["trigger_failure_class"] == "CODE_ERROR"
    assert task.context["ci_failure_type"] == "test"
    assert task.context["ci_failure_sha"] == "cafed00d"
    assert task.status == TaskStatus.QUEUED

    # (3) Simulate successful merge
    task.status = TaskStatus.COMPLETED
    await db_session.flush()

    # (4) Verify the task is queryable as a completed CI auto-remediation and
    # aggregatable by trigger_failure_class — the shape the digest funnel relies on.
    # Audit W1: filter on the column directly.
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status == TaskStatus.COMPLETED,
            Task.auto_remediation.is_(True),
        )
    )
    completed = list(result.scalars().all())
    assert len(completed) == 1

    by_trigger: dict[str, int] = {}
    for t in completed:
        cls = (t.context or {}).get("trigger_failure_class") or "unknown"
        by_trigger[cls] = by_trigger.get(cls, 0) + 1
    assert by_trigger == {"CODE_ERROR": 1}


@pytest.mark.asyncio
async def test_remediation_spawn_to_feedback_clears_escalation_state(
    db_session: AsyncSession,
):
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

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch(
            "app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock
        ),
        patch("app.main._send_webhook_notification", new_callable=AsyncMock),
    ):
        mock_ctx.return_value = (
            ["CI / tests"],
            "AssertionError: boom",
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
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
async def test_merged_preserves_recovery_attempts_for_future_backoff(
    db_session: AsyncSession,
):
    """Merging an auto-fix must not reset recovery_attempts; the next suspension
    cycle needs the prior attempt count so exponential backoff escalates."""
    from app.notification_dispatch import post_auto_fix_result_message

    # Branch was suspended, auto-recovered once (recovery_attempts=1), fix just merged.
    project = await _create_project(
        db_session,
        design_doc={
            "_auto_remediation": {
                "enabled": True,
                "branch_failure_counts": {"main": 0},
                "suspended_branches": [],
                "suspension_metadata": {"main": {"recovery_attempts": 1}},
            }
        },
    )

    await post_auto_fix_result_message(
        db_session,
        project.id,
        branch="main",
        result="merged",
        pr_number=77,
    )
    # Each auto-fix result is a separate task completion that commits before the
    # next one runs (see agent_status.py post_remediation_result_message ->
    # session.commit()). Commit here so the canonical expire-before-lock read in
    # post_auto_fix_result_message picks up this write rather than reloading the
    # pre-call row.
    await db_session.commit()
    ar = (project.design_doc or {}).get("_auto_remediation", {})
    assert "main" not in ar.get("branch_failure_counts", {})
    assert "main" not in ar.get("suspended_branches", [])
    # recovery_attempts must persist across the merge.
    assert (
        ar.get("suspension_metadata", {}).get("main", {}).get("recovery_attempts") == 1
    )

    # Same error recurs — three failures re-suspend the branch.
    with (
        patch(
            "app.notification_dispatch.post_auto_fix_escalation_message",
            new_callable=AsyncMock,
        ),
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
    ):
        for _ in range(3):
            await post_auto_fix_result_message(
                db_session,
                project.id,
                branch="main",
                result="failed",
            )
            # Commit between completions so each failure increment persists and
            # the next call's expire-before-lock read accumulates it.
            await db_session.commit()

    ar = (project.design_doc or {}).get("_auto_remediation", {})
    assert "main" in ar.get("suspended_branches", [])
    # Re-suspension carries the prior recovery_attempts forward (not reset to 0).
    assert (
        ar.get("suspension_metadata", {}).get("main", {}).get("recovery_attempts") == 1
    )


@pytest.mark.asyncio
async def test_pipeline_exhaustion_propagates_to_auto_remediation_branch(
    db_session: AsyncSession,
):
    """When a review/fix task fails permanently on an auto-remediation PR, the
    original create task's auto_remediation context drives post_auto_fix_result_message
    with result='failed' so the branch's failure counter advances toward suspension."""
    from app.routers.agent_status import _propagate_pipeline_failure_to_original

    project = await _create_project(db_session)
    await db_session.flush()

    # Audit W1: auto_remediation is a column.
    original = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "auto-fix CI", "base_branch": "main"}],
        auto_remediation=True,
        context={
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
        payloads=[
            {
                "type": "review",
                "pr_number": 9999,
                "branch": "blueprint/auto-fix",
                "original_task_id": str(original.id),
            }
        ],
        context={
            "review_loop": {"original_task_id": str(original.id), "pr_number": 9999}
        },
        status=TaskStatus.FAILED,
    )
    db_session.add(review)
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_auto_fix_result_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.routers.agent_status._check_and_trigger_rollup",
            new_callable=AsyncMock,
        ),
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
async def test_pipeline_exhaustion_skips_non_remediation_tasks(
    db_session: AsyncSession,
):
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
        payloads=[
            {
                "type": "review",
                "pr_number": 42,
                "branch": "blueprint/task-x",
                "original_task_id": str(original.id),
            }
        ],
        context={
            "review_loop": {"original_task_id": str(original.id), "pr_number": 42}
        },
        status=TaskStatus.FAILED,
    )
    db_session.add(review)
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_auto_fix_result_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.routers.agent_status._check_and_trigger_rollup",
            new_callable=AsyncMock,
        ),
    ):
        await _propagate_pipeline_failure_to_original(review, db_session)
        mock_post.assert_not_awaited()

    await db_session.refresh(original)
    assert original.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_find_matching_prior_investigation_matches_on_file_path(
    db_session: AsyncSession,
):
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
async def test_find_matching_prior_investigation_no_match_returns_none(
    db_session: AsyncSession,
):
    """Empty error summary or no matching investigation returns None."""
    project = await _create_project(db_session)
    assert (
        await _find_matching_prior_investigation(
            project_id=project.id,
            error_summary="",
            session=db_session,
        )
        is None
    )
    assert (
        await _find_matching_prior_investigation(
            project_id=project.id,
            error_summary="unrelated failure xyz",
            session=db_session,
        )
        is None
    )


@pytest.mark.asyncio
async def test_prior_investigation_propagates_diagnosis_context(
    db_session: AsyncSession,
):
    """When a prior investigation matches, the spawned task carries diagnosis_task_id+direction_id."""
    direction_id = str(uuid.uuid4())
    project = await _create_project(
        db_session,
        directions=[{"id": direction_id, "title": "fix auth", "status": "active"}],
    )
    inv = Task(
        project_id=project.id,
        title="Investigate CI",
        payloads=[{"type": "investigate"}],
        status=TaskStatus.COMPLETED,
        context={"direction_id": direction_id, "diagnosis_fingerprint": "fp-abc"},
        plan_output={
            "summary": "flaky auth middleware test",
            "root_cause": "session token mismatch",
            "affected_files": [{"path": "backend/app/auth.py"}],
            "recommended_fix": "reset session cache",
        },
    )
    db_session.add(inv)
    await db_session.flush()

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch(
            "app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock
        ),
        patch("app.main._send_webhook_notification", new_callable=AsyncMock),
    ):
        mock_ctx.return_value = (
            ["CI"],
            "FAILED backend/app/auth.py::test_session token mismatch",
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    tasks = await session_tasks(db_session, project.id)
    assert len(tasks) == 1
    ctx = tasks[0].context
    assert ctx["prior_diagnosis_task_id"] == str(inv.id)
    assert ctx["diagnosis_task_id"] == str(inv.id)
    assert ctx["direction_id"] == direction_id
    assert ctx["diagnosis_fingerprint"] == "fp-abc"


@pytest.mark.asyncio
async def test_remediation_outcome_recorded_when_auto_remediation_merges(
    db_session: AsyncSession,
):
    """An auto-remediation task carrying diagnosis context updates direction state on merge."""
    from app.task_completion import _record_remediation_outcome

    direction_id = str(uuid.uuid4())
    project = await _create_project(
        db_session,
        directions=[{"id": direction_id, "title": "fix auth", "status": "active"}],
    )
    # Audit W1: auto_remediation is a column.
    task = Task(
        project_id=project.id,
        title="Auto-fix",
        payloads=[{"type": "create", "prompt": "fix"}],
        status=TaskStatus.COMPLETED,
        auto_remediation=True,
        context={
            "diagnosis_task_id": str(uuid.uuid4()),
            "direction_id": direction_id,
            "diagnosis_fingerprint": "fp-abc",
        },
    )
    db_session.add(task)
    await db_session.flush()

    recorded = await _record_remediation_outcome(task, merged=True, session=db_session)
    assert recorded is True
    target = next(d for d in project.directions if d.get("id") == direction_id)
    assert target["last_remediation_result"] == "merged"
    assert target["remediation_attempts"] == 1
    assert target["last_remediation_fingerprint"] == "fp-abc"
    assert len(target["remediation_history"]) == 1


@pytest.mark.asyncio
async def test_remediation_outcome_recorded_for_fingerprint_matched_merge(
    db_session: AsyncSession,
):
    """Fingerprint-matched auto-remediation (direction_id set, no diagnosis_task_id) stamps merge outcome."""
    from app.task_completion import _record_remediation_outcome

    direction_id = str(uuid.uuid4())
    project = await _create_project(
        db_session,
        directions=[{"id": direction_id, "title": "fix auth", "status": "active"}],
    )
    # Audit W1: auto_remediation is now a column.
    task = Task(
        project_id=project.id,
        title="Auto-fix",
        payloads=[{"type": "create", "prompt": "fix"}],
        status=TaskStatus.COMPLETED,
        auto_remediation=True,
        context={
            "direction_id": direction_id,
            "diagnosis_fingerprint": "fp-fm",
        },
    )
    db_session.add(task)
    await db_session.flush()

    # Simulate the merge-path guard in task_lifecycle.py: it must fire when
    # auto_remediation is True and direction_id is present, even without
    # diagnosis_task_id (fingerprint-matched fallback path).
    ctx = task.context or {}
    guard_fires = bool(
        ctx.get("diagnosis_task_id")
        or (task.auto_remediation and ctx.get("direction_id"))
    )
    assert guard_fires

    recorded = await _record_remediation_outcome(task, merged=True, session=db_session)
    assert recorded is True
    target = next(d for d in project.directions if d.get("id") == direction_id)
    assert target["last_remediation_result"] == "merged"
    assert target["last_remediation_fingerprint"] == "fp-fm"
    assert "last_remediation_diagnosis_task_id" not in target


@pytest.mark.asyncio
async def test_find_recent_merged_remediation_matches_branch_and_fingerprint(
    db_session: AsyncSession,
):
    """A recent completed auto-remediation task with matching branch+fingerprint is found."""
    project = await _create_project(db_session)
    # Audit W1: auto_remediation + previous_error_fingerprint are columns.
    prior = Task(
        project_id=project.id,
        title="Auto-fix: CI failure on main",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        auto_remediation=True,
        previous_error_fingerprint="fp-xyz",
        context={
            "ci_failure_branch": "main",
        },
    )
    db_session.add(prior)
    await db_session.flush()

    match = await _find_recent_merged_remediation(
        project_id=project.id,
        branch="main",
        fingerprint="fp-xyz",
        session=db_session,
    )
    assert match is not None and match.id == prior.id

    # Different branch → no match
    assert (
        await _find_recent_merged_remediation(
            project_id=project.id,
            branch="other",
            fingerprint="fp-xyz",
            session=db_session,
        )
        is None
    )
    # Different fingerprint → no match
    assert (
        await _find_recent_merged_remediation(
            project_id=project.id,
            branch="main",
            fingerprint="fp-other",
            session=db_session,
        )
        is None
    )


@pytest.mark.asyncio
async def test_regression_detection_injects_context_and_prompt(
    db_session: AsyncSession,
):
    """When a prior remediation merged for same branch+fingerprint, spawned task gets regression context."""
    project = await _create_project(db_session)
    from app.anomaly_detection import _error_fingerprint
    from app.failure_stage import _derive_failure_stage

    err = "AssertionError: boom specific pattern"
    # Mirror production: prior remediation tasks persist the stage-aware
    # fingerprint, so the regression probe must match that exact form.
    fp = _error_fingerprint(err, _derive_failure_stage("CODE_ERROR", err))
    # Audit W1: auto_remediation + previous_error_fingerprint are columns.
    prior = Task(
        project_id=project.id,
        title="Auto-fix: CI failure on main",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        auto_remediation=True,
        previous_error_fingerprint=fp,
        context={
            "ci_failure_branch": "main",
            "merged_pr_number": 4242,
        },
    )
    db_session.add(prior)
    await db_session.flush()

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch(
            "app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock
        ),
        patch("app.main._send_webhook_notification", new_callable=AsyncMock),
        patch(
            "app.auto_remediation.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="fake-token",
        ),
        patch(
            "app.auto_remediation.get_pull_request",
            new_callable=AsyncMock,
            return_value={"merge_commit_sha": "abc123merge"},
        ),
        patch(
            "app.auto_remediation.is_ancestor_of",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        mock_ctx.return_value = (
            ["CI / tests"],
            err,
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="feedface",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    spawned = await session_tasks(db_session, project.id)
    new_task = next(t for t in spawned if t.id != prior.id)
    assert new_task.context.get("regression_of_task_id") == str(prior.id)
    assert new_task.context.get("regression_prior_pr_number") == 4242
    assert "REGRESSION DETECTED" in new_task.payloads[0]["prompt"]
    assert "#4242" in new_task.payloads[0]["prompt"]


@pytest.mark.asyncio
async def test_regression_of_confirmed_when_fix_is_ancestor(
    db_session: AsyncSession,
):
    """When is_ancestor_of returns True, prior remediation is treated as a landed fix (regression)."""
    project = await _create_project(db_session)
    from app.anomaly_detection import _error_fingerprint
    from app.failure_stage import _derive_failure_stage

    err = "AssertionError: confirmed ancestor pattern"
    fp = _error_fingerprint(err, _derive_failure_stage("CODE_ERROR", err))
    prior = Task(
        project_id=project.id,
        title="Auto-fix: prior fix",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        auto_remediation=True,
        previous_error_fingerprint=fp,
        context={"ci_failure_branch": "main", "merged_pr_number": 9901},
    )
    db_session.add(prior)
    await db_session.flush()

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch(
            "app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock
        ),
        patch("app.main._send_webhook_notification", new_callable=AsyncMock),
        patch(
            "app.auto_remediation.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="fake-token",
        ),
        patch(
            "app.auto_remediation.get_pull_request",
            new_callable=AsyncMock,
            return_value={"merge_commit_sha": "deadbeefmerge"},
        ),
        patch(
            "app.auto_remediation.is_ancestor_of",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        mock_ctx.return_value = (
            ["CI / tests"],
            err,
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="feedface",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    spawned = await session_tasks(db_session, project.id)
    new_task = next(t for t in spawned if t.id != prior.id)
    # ancestry confirmed → regression path taken
    assert new_task.context.get("regression_of_task_id") == str(prior.id)
    assert "REGRESSION DETECTED" in new_task.payloads[0]["prompt"]


@pytest.mark.asyncio
async def test_regression_of_cleared_when_fix_not_ancestor(
    db_session: AsyncSession,
):
    """When is_ancestor_of returns False, prior COMPLETED remediation is NOT treated as landed."""
    project = await _create_project(db_session)
    from app.anomaly_detection import _error_fingerprint
    from app.failure_stage import _derive_failure_stage

    err = "AssertionError: stranded fix pattern"
    fp = _error_fingerprint(err, _derive_failure_stage("CODE_ERROR", err))
    prior = Task(
        project_id=project.id,
        title="Auto-fix: stranded fix",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        auto_remediation=True,
        previous_error_fingerprint=fp,
        context={"ci_failure_branch": "main", "merged_pr_number": 9902},
    )
    db_session.add(prior)
    await db_session.flush()

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch(
            "app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock
        ),
        patch("app.main._send_webhook_notification", new_callable=AsyncMock),
        patch(
            "app.auto_remediation.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="fake-token",
        ),
        patch(
            "app.auto_remediation.get_pull_request",
            new_callable=AsyncMock,
            return_value={"merge_commit_sha": "strandedshanotlanded"},
        ),
        patch(
            "app.auto_remediation.is_ancestor_of",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        mock_ctx.return_value = (
            ["CI / tests"],
            err,
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="feedface",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    spawned = await session_tasks(db_session, project.id)
    new_task = next(t for t in spawned if t.id != prior.id)
    # fix commit not on branch → not a regression, no regression_of_task_id
    assert new_task.context.get("regression_of_task_id") is None
    assert "REGRESSION DETECTED" not in new_task.payloads[0]["prompt"]


@pytest.mark.asyncio
async def test_regression_of_cleared_when_ancestry_indeterminate(
    db_session: AsyncSession,
):
    """When is_ancestor_of returns None (GitHub error), safe-degrade: not treated as landed."""
    project = await _create_project(db_session)
    from app.anomaly_detection import _error_fingerprint
    from app.failure_stage import _derive_failure_stage

    err = "AssertionError: indeterminate ancestry pattern"
    fp = _error_fingerprint(err, _derive_failure_stage("CODE_ERROR", err))
    prior = Task(
        project_id=project.id,
        title="Auto-fix: indeterminate prior",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        auto_remediation=True,
        previous_error_fingerprint=fp,
        context={"ci_failure_branch": "main", "merged_pr_number": 9903},
    )
    db_session.add(prior)
    await db_session.flush()

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch(
            "app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock
        ),
        patch("app.main._send_webhook_notification", new_callable=AsyncMock),
        patch(
            "app.auto_remediation.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="fake-token",
        ),
        patch(
            "app.auto_remediation.get_pull_request",
            new_callable=AsyncMock,
            return_value={"merge_commit_sha": "someshagithubcantcheck"},
        ),
        patch(
            "app.auto_remediation.is_ancestor_of",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        mock_ctx.return_value = (
            ["CI / tests"],
            err,
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="feedface",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    spawned = await session_tasks(db_session, project.id)
    new_task = next(t for t in spawned if t.id != prior.id)
    # indeterminate → safe-degrade to not-landed, no regression context
    assert new_task.context.get("regression_of_task_id") is None
    assert "REGRESSION DETECTED" not in new_task.payloads[0]["prompt"]


@pytest.mark.asyncio
async def test_regression_task_failure_suspends_at_threshold_one(
    db_session: AsyncSession,
):
    """A failing task flagged as regression triggers suspension after a single failure."""
    from app.notification_dispatch import post_auto_fix_result_message

    project = await _create_project(db_session)
    # Audit W1: auto_remediation is a column.
    task = Task(
        project_id=project.id,
        title="Auto-fix regression",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        auto_remediation=True,
        context={"regression_of_task_id": str(uuid.uuid4())},
    )
    db_session.add(task)
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_auto_fix_escalation_message",
            new_callable=AsyncMock,
        ) as mock_esc,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
    ):
        await post_auto_fix_result_message(
            db_session,
            project.id,
            branch="main",
            result="failed",
            task_id=task.id,
        )

    mock_esc.assert_awaited_once()
    ar = (project.design_doc or {}).get("_auto_remediation", {})
    assert "main" in ar.get("suspended_branches", [])


@pytest.mark.asyncio
async def test_find_direction_by_fingerprint_prefers_active(db_session: AsyncSession):
    """When multiple directions share a fingerprint, return the active one."""
    fp = "fingerprint-xyz"
    paused_id = str(uuid.uuid4())
    active_id = str(uuid.uuid4())
    project = await _create_project(
        db_session,
        directions=[
            {
                "id": paused_id,
                "status": "auto_paused",
                "auto_paused": True,
                "last_remediation_fingerprint": fp,
                "last_remediation_at": "2026-04-20T00:00:00+00:00",
            },
            {
                "id": active_id,
                "status": "active",
                "last_remediation_fingerprint": fp,
                "last_remediation_at": "2026-04-19T00:00:00+00:00",
            },
        ],
    )

    result = await _find_direction_by_fingerprint(
        project_id=project.id,
        fingerprint=fp,
        session=db_session,
    )
    assert result == active_id


@pytest.mark.asyncio
async def test_find_direction_by_fingerprint_prefers_most_recent_active(
    db_session: AsyncSession,
):
    """Among active matches, prefer the one with the most recent last_remediation_at."""
    fp = "fp-abc"
    older_id = str(uuid.uuid4())
    newer_id = str(uuid.uuid4())
    project = await _create_project(
        db_session,
        directions=[
            {
                "id": older_id,
                "status": "active",
                "error_fingerprint": fp,
                "last_remediation_at": "2026-04-10T00:00:00+00:00",
            },
            {
                "id": newer_id,
                "status": "active",
                "error_fingerprint": fp,
                "last_remediation_at": "2026-04-19T00:00:00+00:00",
            },
        ],
    )
    result = await _find_direction_by_fingerprint(
        project_id=project.id,
        fingerprint=fp,
        session=db_session,
    )
    assert result == newer_id


@pytest.mark.asyncio
async def test_spawn_direction_stamp_uses_directions_lock(db_session: AsyncSession):
    """Remediation spawn stamps the direction via with_directions_locked, not bare session.get.

    Verify that after _handle_main_branch_ci_failure_inner spawns a task whose
    context carries direction_id, the matched direction has last_remediation_task_id
    set — proving the stamp went through the lock helper and the mutation was
    committed to the DB row.
    """
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.ext.asyncio import AsyncSession as _AS

    direction_id = str(uuid.uuid4())
    project = await _create_project(
        db_session,
        directions=[{"id": direction_id, "title": "fix auth", "status": "active"}],
    )
    await db_session.commit()

    inv = Task(
        project_id=project.id,
        title="Investigate CI",
        payloads=[{"type": "investigate"}],
        status=TaskStatus.COMPLETED,
        context={"direction_id": direction_id, "diagnosis_fingerprint": "fp-lock-test"},
        plan_output={
            "summary": "token mismatch",
            "root_cause": "session cache",
            "affected_files": [{"path": "backend/app/auth.py"}],
            "recommended_fix": "reset cache",
        },
    )
    db_session.add(inv)
    await db_session.commit()

    with (
        patch(
            "app.auto_remediation._extract_ci_context", new_callable=AsyncMock
        ) as mock_ctx,
        patch(
            "app.auto_remediation.post_auto_fix_spawned_message", new_callable=AsyncMock
        ),
        patch("app.main._send_webhook_notification", new_callable=AsyncMock),
    ):
        mock_ctx.return_value = (
            ["CI"],
            "FAILED backend/app/auth.py::test_session token mismatch",
            "test",
            {"failing_tests": [], "error_locations": [], "error_messages": []},
        )
        await _handle_main_branch_ci_failure_inner(
            owner="acme",
            repo="webapp",
            head_sha="deadbeef",
            head_branch="main",
            project_id=project.id,
            config=_default_config(),
            session=db_session,
        )

    await db_session.commit()

    # Read fresh to bypass identity-map cache
    _Session = sessionmaker(
        bind=db_session.bind,
        class_=_AS,
        expire_on_commit=False,
    )
    async with _Session() as verify:
        result = await verify.execute(select(Project).where(Project.id == project.id))
        refreshed = result.scalar_one()

    target = next(d for d in refreshed.directions if d.get("id") == direction_id)
    assert "last_remediation_task_id" in target, (
        "direction was not stamped — with_directions_locked bypass may have silently dropped the mutation"
    )
