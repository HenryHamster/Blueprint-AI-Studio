"""Tests for automatic playbook entry creation from recurring failure patterns."""
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, Task, TaskStatus
from app.routers.agent_status import _check_recurring_failures, _utcnow


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _make_project(session: AsyncSession, **overrides) -> Project:
    defaults = {
        "name": "Test Project",
        "user_id": "test-user-123",
        "design_doc": {},
    }
    defaults.update(overrides)
    project = Project(**defaults)
    session.add(project)
    await session.flush()
    return project


async def _make_failed_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    status_message: str = "vm failed to start after provision attempt",
    task_type: str = "create",
    age_days: float = 0,
) -> Task:
    task = Task(
        project_id=project_id,
        title="Some task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.FAILED,
        latest_status_message=status_message,
    )
    if age_days:
        task.created_at = _utcnow() - timedelta(days=age_days)
    session.add(task)
    await session.flush()
    return task


async def _make_completed_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    task_type: str = "create",
    scope: str = "small",
    related_files: list | None = None,
) -> Task:
    ctx: dict = {"scope": scope}
    if related_files:
        ctx["related_files"] = related_files
    task = Task(
        project_id=project_id,
        title="Successful task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    session.add(task)
    await session.flush()
    return task


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_three_failures_trigger_auto_entry(db_session: AsyncSession):
    """3 FAILED tasks with the same failure_class and keyword overlap creates an auto-entry."""
    project = await _make_project(db_session)

    common_msg = "provision quota exceeded cpus zone failure"
    t1 = await _make_failed_task(db_session, project.id, status_message=common_msg)
    t2 = await _make_failed_task(db_session, project.id, status_message=common_msg)
    # t3 is the "current" task passed to _check_recurring_failures
    t3 = await _make_failed_task(db_session, project.id, status_message=common_msg)

    await _check_recurring_failures(t3, db_session)
    # Flush so in-memory changes are readable; don't refresh (refresh reloads from DB,
    # discarding in-memory changes not yet committed — filter-test-session-refresh-pattern)
    await db_session.flush()
    doc = project.design_doc or {}
    auto_keys = [k for k in doc if k.startswith("playbook/auto-")]
    assert len(auto_keys) == 1, f"Expected 1 auto entry, got {auto_keys}"
    entry = doc[auto_keys[0]]
    assert "PROVISION_FAILURE" in entry["content"]
    assert entry["confirmed_count"] == 0
    assert entry["source_task_id"] is None


@pytest.mark.asyncio
async def test_two_failures_do_not_trigger_entry(db_session: AsyncSession):
    """Only 2 failures with the same class — below the threshold of 3 — should not create entry."""
    project = await _make_project(db_session)

    common_msg = "provision quota exceeded cpus zone failure"
    t1 = await _make_failed_task(db_session, project.id, status_message=common_msg)
    # t2 is the "current" task
    t2 = await _make_failed_task(db_session, project.id, status_message=common_msg)

    await _check_recurring_failures(t2, db_session)
    await db_session.flush()
    doc = project.design_doc or {}
    auto_keys = [k for k in doc if k.startswith("playbook/auto-")]
    assert len(auto_keys) == 0


@pytest.mark.asyncio
async def test_duplicate_detection_prevents_recreation(db_session: AsyncSession):
    """When a matching auto-entry already exists, a second run updates it rather than adding a new key."""
    project = await _make_project(db_session)

    common_msg = "provision quota exceeded cpus zone failure"
    t1 = await _make_failed_task(db_session, project.id, status_message=common_msg)
    t2 = await _make_failed_task(db_session, project.id, status_message=common_msg)
    t3 = await _make_failed_task(db_session, project.id, status_message=common_msg)

    # First call creates the entry
    await _check_recurring_failures(t3, db_session)
    await db_session.flush()
    doc1 = dict(project.design_doc or {})
    auto_keys_1 = [k for k in doc1 if k.startswith("playbook/auto-")]
    assert len(auto_keys_1) == 1

    # Second call with a new "current" task whose keywords overlap significantly
    t4 = await _make_failed_task(db_session, project.id, status_message=common_msg)
    await _check_recurring_failures(t4, db_session)
    await db_session.flush()
    doc2 = dict(project.design_doc or {})
    auto_keys_2 = [k for k in doc2 if k.startswith("playbook/auto-")]
    # Still only 1 entry (updated in place, not a new key created)
    assert len(auto_keys_2) == 1, f"Expected 1 auto entry after duplicate check, got {auto_keys_2}"


@pytest.mark.asyncio
async def test_cap_of_five_is_respected(db_session: AsyncSession):
    """Auto-playbook entries are capped at 5 per project; beyond that no new entries are created."""
    # Pre-populate design_doc with 5 existing auto-entries
    prefilled = {f"playbook/auto-CODE_ERROR-abc{i:03d}": {"content": f"entry {i}", "confirmed_count": 0} for i in range(5)}
    project = await _make_project(db_session, design_doc=dict(prefilled))

    # 3 failures that would normally trigger a new entry
    common_msg = "provision quota exceeded cpus zone failure"
    t1 = await _make_failed_task(db_session, project.id, status_message=common_msg)
    t2 = await _make_failed_task(db_session, project.id, status_message=common_msg)
    t3 = await _make_failed_task(db_session, project.id, status_message=common_msg)

    await _check_recurring_failures(t3, db_session)
    await db_session.flush()
    doc = project.design_doc or {}
    auto_keys = [k for k in doc if k.startswith("playbook/auto-")]
    assert len(auto_keys) == 5, f"Cap of 5 should be respected, got {len(auto_keys)}"


@pytest.mark.asyncio
async def test_applicable_types_reflect_task_types(db_session: AsyncSession):
    """applicable_types on the auto-entry contains the types of the failing tasks."""
    project = await _make_project(db_session)

    common_msg = "provision quota exceeded cpus zone failure"
    t1 = await _make_failed_task(db_session, project.id, status_message=common_msg, task_type="create")
    t2 = await _make_failed_task(db_session, project.id, status_message=common_msg, task_type="fix")
    t3 = await _make_failed_task(db_session, project.id, status_message=common_msg, task_type="create")

    await _check_recurring_failures(t3, db_session)
    await db_session.flush()
    doc = project.design_doc or {}
    auto_keys = [k for k in doc if k.startswith("playbook/auto-")]
    assert len(auto_keys) == 1
    entry = doc[auto_keys[0]]
    applicable = entry.get("applicable_types", [])
    assert "create" in applicable
    # fix is in the prior tasks; t3 is the current task (type=create) added via session-exclusion
    # All 3 tasks share the same type in this test except t2
    assert isinstance(applicable, list)
    assert len(applicable) >= 1


@pytest.mark.asyncio
async def test_old_failures_outside_7_day_window_ignored(db_session: AsyncSession):
    """Failures older than 7 days are not counted toward the pattern threshold."""
    project = await _make_project(db_session)

    common_msg = "provision quota exceeded cpus zone failure"
    # 2 old failures (> 7 days ago)
    t1 = await _make_failed_task(db_session, project.id, status_message=common_msg, age_days=8)
    t2 = await _make_failed_task(db_session, project.id, status_message=common_msg, age_days=8)
    # 1 recent failure (the current task)
    t3 = await _make_failed_task(db_session, project.id, status_message=common_msg)

    await _check_recurring_failures(t3, db_session)
    await db_session.flush()
    doc = project.design_doc or {}
    auto_keys = [k for k in doc if k.startswith("playbook/auto-")]
    assert len(auto_keys) == 0, "Old failures outside 7-day window should not count"


# ─────────────────────────────────────────────────────────────────────────────
# Tests for capture_success_pattern
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_success_creates_entry_when_negative_exists(db_session: AsyncSession):
    """capture_success_pattern creates a success-pattern entry when a failure-derived
    entry exists for the same task type."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={
        "playbook/review-test-coverage": {
            "content": "test coverage issues flagged",
            "source": "review-extracted",
            "applicable_types": ["create"],
            "confirmed_count": 0,
            "updated_at": _utcnow().isoformat(),
        },
    })
    task = await _make_completed_task(db_session, project.id, task_type="create", scope="small")

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key == "success-create"
    doc = project.design_doc or {}
    assert "playbook/success-create" in doc
    entry = doc["playbook/success-create"]
    assert entry["source"] == "success-pattern"
    assert entry["success_count"] == 1
    assert entry["confirmed_count"] == 0
    assert "create" in entry["applicable_types"]
    assert "small" in entry["content"]
    assert "_meta" in doc


@pytest.mark.asyncio
async def test_capture_success_skips_when_no_negative_entries(db_session: AsyncSession):
    """capture_success_pattern returns None when there are no failure-derived entries
    for the task type — success on an easy task type is not noteworthy."""
    from app.playbook_learning import capture_success_pattern

    # Only a manually written entry exists — not a failure-derived one
    project = await _make_project(db_session, design_doc={
        "playbook/manual-tip": {
            "content": "a manually written tip",
            "source": "manual",
            "confirmed_count": 0,
            "updated_at": _utcnow().isoformat(),
        },
    })
    task = await _make_completed_task(db_session, project.id, task_type="create")

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key is None
    doc = project.design_doc or {}
    assert "playbook/success-create" not in doc


@pytest.mark.asyncio
async def test_capture_success_skips_when_project_has_no_playbook(db_session: AsyncSession):
    """capture_success_pattern returns None when the project has no playbook entries at all."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    task = await _make_completed_task(db_session, project.id, task_type="fix")

    key = await capture_success_pattern(task, db_session)
    assert key is None


@pytest.mark.asyncio
async def test_capture_success_updates_existing_entry(db_session: AsyncSession):
    """capture_success_pattern increments success_count and refreshes content on
    a second qualifying success of the same task type."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={
        "playbook/auto-code-error-abc123": {
            "content": "WARNING: CODE_ERROR failures have occurred 3 times",
            "confirmed_count": 0,
            "updated_at": _utcnow().isoformat(),
        },
        "playbook/success-fix": {
            "content": "First-attempt success pattern for fix tasks (1 observation, scope: small).",
            "source": "success-pattern",
            "success_count": 1,
            "confirmed_count": 2,
            "applicable_types": ["fix"],
            "updated_at": _utcnow().isoformat(),
        },
    })
    task = await _make_completed_task(db_session, project.id, task_type="fix", scope="medium")

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key == "success-fix"
    entry = (project.design_doc or {})["playbook/success-fix"]
    assert entry["success_count"] == 2
    # confirmed_count is left untouched by capture_success_pattern
    assert entry["confirmed_count"] == 2
    assert "medium" in entry["content"]
    assert "2 observations" in entry["content"]


@pytest.mark.asyncio
async def test_capture_success_includes_related_files(db_session: AsyncSession):
    """capture_success_pattern includes the files touched in the entry content."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={
        "playbook/review-error-handling": {
            "content": "error handling issues flagged",
            "source": "review-extracted",
            "applicable_types": ["refactor"],
            "confirmed_count": 0,
            "updated_at": _utcnow().isoformat(),
        },
    })
    files = ["backend/app/foo.py", "backend/app/bar.py"]
    task = await _make_completed_task(
        db_session, project.id,
        task_type="refactor", related_files=files,
    )

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key == "success-refactor"
    entry = (project.design_doc or {})["playbook/success-refactor"]
    assert "foo.py" in entry["content"]
    assert "bar.py" in entry["content"]


@pytest.mark.asyncio
async def test_capture_success_auto_key_counts_as_negative(db_session: AsyncSession):
    """playbook/auto-* entries created by _check_recurring_failures (no 'source' field)
    are treated as failure-derived entries and satisfy the sampling gate."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={
        # _check_recurring_failures does not set "source" — only key prefix "auto-"
        "playbook/auto-code-error-deadbeef": {
            "content": "WARNING: CODE_ERROR failures occurred 3 times",
            "confirmed_count": 0,
            "applicable_types": ["create"],
            "updated_at": _utcnow().isoformat(),
        },
    })
    task = await _make_completed_task(db_session, project.id, task_type="create")

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key == "success-create"
    assert "playbook/success-create" in (project.design_doc or {})


# ─────────────────────────────────────────────────────────────────────────────
# Tests for _update_module_feedback_profiles
# ─────────────────────────────────────────────────────────────────────────────


def test_update_module_feedback_profiles_creates_new_entries():
    """New files get fresh per-category issue counts in the module_feedback dict."""
    from app.playbook_learning import _update_module_feedback_profiles

    doc: dict = {}
    related_files = ["backend/app/orchestrator.py", "backend/app/models.py"]
    buckets = {
        "test-coverage": [{"desc": "missing test", "severity": "MAJOR", "iteration": 1}],
        "error-handling": [
            {"desc": "unhandled exception", "severity": "MINOR", "iteration": 2},
            {"desc": "no guard clause", "severity": "MINOR", "iteration": 2},
        ],
    }

    _update_module_feedback_profiles(doc, related_files, buckets)

    assert "module_feedback" in doc
    feedback = doc["module_feedback"]
    assert "backend/app/orchestrator.py" in feedback
    assert "backend/app/models.py" in feedback

    for entry in feedback.values():
        assert entry["test-coverage"] == 1
        assert entry["error-handling"] == 2
        assert entry["total"] == 3
        assert "last_updated" in entry


def test_update_module_feedback_profiles_accumulates_counts():
    """Subsequent calls accumulate counts for already-tracked files."""
    from app.playbook_learning import _update_module_feedback_profiles

    doc: dict = {
        "module_feedback": {
            "backend/app/orchestrator.py": {
                "test-coverage": 3,
                "total": 3,
                "last_updated": "2026-01-01T00:00:00",
            }
        }
    }
    buckets = {
        "test-coverage": [{"desc": "missing test", "severity": "MAJOR", "iteration": 1}],
    }

    _update_module_feedback_profiles(doc, ["backend/app/orchestrator.py"], buckets)

    entry = doc["module_feedback"]["backend/app/orchestrator.py"]
    assert entry["test-coverage"] == 4
    assert entry["total"] == 4


def test_update_module_feedback_profiles_evicts_oldest_when_over_cap():
    """When the module cap is exceeded the oldest-updated entry is evicted."""
    from app.playbook_learning import _update_module_feedback_profiles, _MODULE_FEEDBACK_MAX_ENTRIES

    # Fill up to the cap with stale entries
    existing = {
        f"file_{i}.py": {"total": 1, "last_updated": f"2025-01-{i + 1:02d}T00:00:00"}
        for i in range(_MODULE_FEEDBACK_MAX_ENTRIES)
    }
    doc: dict = {"module_feedback": existing}
    buckets = {"test-coverage": [{"desc": "x", "severity": "MINOR", "iteration": 0}]}

    _update_module_feedback_profiles(doc, ["new_file.py"], buckets)

    feedback = doc["module_feedback"]
    assert len(feedback) == _MODULE_FEEDBACK_MAX_ENTRIES
    assert "new_file.py" in feedback
    # The oldest entry (file_0.py with 2025-01-01) should have been evicted
    assert "file_0.py" not in feedback


def test_update_module_feedback_profiles_noop_on_empty_inputs():
    """No changes to doc when related_files or buckets are empty."""
    from app.playbook_learning import _update_module_feedback_profiles

    doc: dict = {}
    _update_module_feedback_profiles(doc, [], {"test-coverage": [{"desc": "x", "severity": "MINOR", "iteration": 0}]})
    assert "module_feedback" not in doc

    _update_module_feedback_profiles(doc, ["backend/app/models.py"], {})
    assert "module_feedback" not in doc


@pytest.mark.asyncio
async def test_extract_review_feedback_updates_module_profiles(db_session: AsyncSession):
    """extract_review_feedback_to_playbook also persists per-module profiles in design_doc."""
    from app.playbook_learning import extract_review_feedback_to_playbook

    project = await _make_project(db_session, design_doc={})
    review_history = [
        {
            "iteration": 1,
            "issues": [
                {"description": "missing test for new endpoint", "severity": "MAJOR"},
                {"description": "no unit test coverage", "severity": "MINOR"},
                {"description": "unhandled exception path", "severity": "MAJOR"},
            ],
        },
        {
            "iteration": 2,
            "issues": [
                {"description": "still missing tests", "severity": "MAJOR"},
            ],
        },
        {
            "iteration": 3,
            "issues": [],
        },
    ]
    review_loop = {"iteration": 3, "review_history": review_history}
    task = Task(
        project_id=project.id,
        title="Test task",
        payloads=[{"type": "create", "prompt": "add endpoint"}],
        status=TaskStatus.COMPLETED,
        context={"related_files": ["backend/app/routes.py"]},
    )
    db_session.add(task)
    await db_session.flush()

    await extract_review_feedback_to_playbook(task, review_loop, pr_number=42, original_task_type="create", session=db_session)
    await db_session.flush()

    doc = project.design_doc or {}
    assert "module_feedback" in doc, "module_feedback should be written to design_doc"
    feedback = doc["module_feedback"]
    assert "backend/app/routes.py" in feedback
    entry = feedback["backend/app/routes.py"]
    assert entry["total"] > 0
    assert "last_updated" in entry
