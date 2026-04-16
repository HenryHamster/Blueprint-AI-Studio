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
async def test_capture_success_creates_entry_after_threshold(db_session: AsyncSession):
    """capture_success_pattern creates a success-pattern entry once the task type
    has at least 3 total completions (success or failure) in the project."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    # 2 prior completions of the same type (makes total = 3 with current task)
    await _make_completed_task(db_session, project.id, task_type="create", scope="small")
    await _make_completed_task(db_session, project.id, task_type="create", scope="small")
    task = await _make_completed_task(db_session, project.id, task_type="create", scope="small")

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key == "success-create-small"
    doc = project.design_doc or {}
    assert "playbook/success-create-small" in doc
    entry = doc["playbook/success-create-small"]
    assert entry["source"] == "success-pattern"
    assert entry["success_count"] == 1
    assert entry["confirmed_count"] == 0
    assert "create" in entry["applicable_types"]
    assert "small" in entry["content"]
    assert "_meta" in doc


@pytest.mark.asyncio
async def test_capture_success_skips_below_threshold(db_session: AsyncSession):
    """capture_success_pattern returns None when the task type has fewer than 3
    total completions — the single task is not yet statistically meaningful."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    # Only 1 prior completion + current = 2 total (below threshold of 3)
    await _make_completed_task(db_session, project.id, task_type="create", scope="small")
    task = await _make_completed_task(db_session, project.id, task_type="create", scope="small")

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key is None
    doc = project.design_doc or {}
    assert not any(k.startswith("playbook/success-") for k in doc)


@pytest.mark.asyncio
async def test_capture_success_skips_with_no_prior_completions(db_session: AsyncSession):
    """capture_success_pattern returns None when this is the very first task of the type."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    task = await _make_completed_task(db_session, project.id, task_type="fix")

    key = await capture_success_pattern(task, db_session)
    assert key is None


@pytest.mark.asyncio
async def test_capture_success_updates_existing_entry(db_session: AsyncSession):
    """capture_success_pattern increments success_count, updates scope_distribution,
    and refreshes content on a second qualifying success of the same (type, scope)."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={
        "playbook/success-fix-medium": {
            "content": "First-attempt success pattern for fix tasks (1 observation, scope: medium).",
            "source": "success-pattern",
            "success_count": 1,
            "confirmed_count": 2,
            "applicable_types": ["fix"],
            "scope": "medium",
            "scope_distribution": {"medium": 1},
            "num_files_changed": 3,
            "tests_added": False,
            "first_attempt_success": True,
            "updated_at": _utcnow().isoformat(),
        },
    })
    # 2 prior completions to satisfy noise filter
    await _make_completed_task(db_session, project.id, task_type="fix", scope="medium")
    await _make_completed_task(db_session, project.id, task_type="fix", scope="medium")
    task = await _make_completed_task(db_session, project.id, task_type="fix", scope="medium")

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key == "success-fix-medium"
    entry = (project.design_doc or {})["playbook/success-fix-medium"]
    assert entry["success_count"] == 2
    # confirmed_count is left untouched by capture_success_pattern
    assert entry["confirmed_count"] == 2
    assert "medium" in entry["content"]
    assert "2 observations" in entry["content"]
    assert entry["scope_distribution"] == {"medium": 2}


@pytest.mark.asyncio
async def test_capture_success_includes_related_files(db_session: AsyncSession):
    """capture_success_pattern includes the files touched in the entry content."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    files = ["backend/app/foo.py", "backend/app/bar.py"]
    # 2 prior completions to satisfy noise filter
    await _make_completed_task(db_session, project.id, task_type="refactor")
    await _make_completed_task(db_session, project.id, task_type="refactor")
    task = await _make_completed_task(
        db_session, project.id,
        task_type="refactor", related_files=files,
    )

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key == "success-refactor-small"
    entry = (project.design_doc or {})["playbook/success-refactor-small"]
    assert "foo.py" in entry["content"]
    assert "bar.py" in entry["content"]


@pytest.mark.asyncio
async def test_capture_success_new_type_without_failures(db_session: AsyncSession):
    """capture_success_pattern works for task types that have never failed
    (cold-start fix: no failure-gate required)."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    # Simulate a brand-new task type ('migrate') with 3 completions, no failures
    await _make_completed_task(db_session, project.id, task_type="migrate", scope="small")
    await _make_completed_task(db_session, project.id, task_type="migrate", scope="small")
    task = await _make_completed_task(db_session, project.id, task_type="migrate", scope="small")

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key == "success-migrate-small"
    assert "playbook/success-migrate-small" in (project.design_doc or {})


@pytest.mark.asyncio
async def test_capture_success_metadata_fields(db_session: AsyncSession):
    """New entries include num_files_changed, tests_added, first_attempt_success,
    scope, and scope_distribution fields."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    files = ["backend/app/foo.py", "backend/testing/test_foo.py"]
    await _make_completed_task(db_session, project.id, task_type="create", scope="medium")
    await _make_completed_task(db_session, project.id, task_type="create", scope="medium")
    task = await _make_completed_task(
        db_session, project.id,
        task_type="create", scope="medium", related_files=files,
    )

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key == "success-create-medium"
    entry = (project.design_doc or {})["playbook/success-create-medium"]
    assert entry["num_files_changed"] == 2
    assert entry["tests_added"] is True  # test_foo.py matches /testing/ pattern
    assert entry["first_attempt_success"] is True
    assert entry["scope"] == "medium"
    assert entry["scope_distribution"] == {"medium": 1}


@pytest.mark.asyncio
async def test_capture_success_per_scope_key(db_session: AsyncSession):
    """Different scopes produce separate playbook entries."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    # 2 prior of each scope to satisfy threshold
    await _make_completed_task(db_session, project.id, task_type="create", scope="small")
    await _make_completed_task(db_session, project.id, task_type="create", scope="small")
    await _make_completed_task(db_session, project.id, task_type="create", scope="medium")
    await _make_completed_task(db_session, project.id, task_type="create", scope="medium")

    task_small = await _make_completed_task(db_session, project.id, task_type="create", scope="small")
    key_small = await capture_success_pattern(task_small, db_session)

    task_medium = await _make_completed_task(db_session, project.id, task_type="create", scope="medium")
    key_medium = await capture_success_pattern(task_medium, db_session)

    await db_session.flush()

    assert key_small == "success-create-small"
    assert key_medium == "success-create-medium"
    doc = project.design_doc or {}
    assert "playbook/success-create-small" in doc
    assert "playbook/success-create-medium" in doc


@pytest.mark.asyncio
async def test_capture_success_scope_distribution_accumulates(db_session: AsyncSession):
    """scope_distribution tracks the count of successes at each scope level."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    # Pre-seed 2 prior completions for the threshold
    await _make_completed_task(db_session, project.id, task_type="fix", scope="small")
    await _make_completed_task(db_session, project.id, task_type="fix", scope="small")

    # First success: creates the entry
    t1 = await _make_completed_task(db_session, project.id, task_type="fix", scope="small")
    await capture_success_pattern(t1, db_session)
    await db_session.flush()

    # Second success: updates scope_distribution
    t2 = await _make_completed_task(db_session, project.id, task_type="fix", scope="small")
    await capture_success_pattern(t2, db_session)
    await db_session.flush()

    entry = (project.design_doc or {}).get("playbook/success-fix-small", {})
    assert entry.get("success_count") == 2
    assert entry.get("scope_distribution") == {"small": 2}


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


# ─────────────────────────────────────────────────────────────────────────────
# Tests for _accumulate_direction_first_attempt
# ─────────────────────────────────────────────────────────────────────────────


def test_accumulate_direction_first_attempt_creates_entry():
    """Creates a direction tally entry with counts from buckets."""
    from app.playbook_learning import _accumulate_direction_first_attempt, _DIRECTION_FIRST_ATTEMPT_KEY

    doc: dict = {}
    buckets = {
        "test-coverage": [{"desc": "missing test", "severity": "MAJOR", "iteration": 0}],
        "style-conventions": [
            {"desc": "wrong naming", "severity": "MINOR", "iteration": 0},
            {"desc": "bad indent", "severity": "MINOR", "iteration": 0},
        ],
    }
    _accumulate_direction_first_attempt(doc, "dir-abc123", buckets)

    assert _DIRECTION_FIRST_ATTEMPT_KEY in doc
    tally = doc[_DIRECTION_FIRST_ATTEMPT_KEY]["dir-abc123"]
    assert tally["test-coverage"] == 1
    assert tally["style-conventions"] == 2
    assert tally["total"] == 3
    assert "last_updated" in tally


def test_accumulate_direction_first_attempt_accumulates_across_calls():
    """Repeated calls for the same direction accumulate counts."""
    from app.playbook_learning import _accumulate_direction_first_attempt, _DIRECTION_FIRST_ATTEMPT_KEY

    doc: dict = {}
    buckets = {"test-coverage": [{"desc": "x", "severity": "MAJOR", "iteration": 0}]}
    _accumulate_direction_first_attempt(doc, "dir-abc", buckets)
    _accumulate_direction_first_attempt(doc, "dir-abc", buckets)
    _accumulate_direction_first_attempt(doc, "dir-abc", buckets)

    tally = doc[_DIRECTION_FIRST_ATTEMPT_KEY]["dir-abc"]
    assert tally["test-coverage"] == 3
    assert tally["total"] == 3


def test_accumulate_direction_first_attempt_noop_on_empty_buckets():
    """No changes when buckets are empty."""
    from app.playbook_learning import _accumulate_direction_first_attempt, _DIRECTION_FIRST_ATTEMPT_KEY

    doc: dict = {}
    _accumulate_direction_first_attempt(doc, "dir-xyz", {})
    assert _DIRECTION_FIRST_ATTEMPT_KEY not in doc


# ─────────────────────────────────────────────────────────────────────────────
# Tests for _maybe_auto_playbook_from_first_attempt
# ─────────────────────────────────────────────────────────────────────────────


def test_maybe_auto_playbook_creates_entry_at_threshold():
    """Creates a first-attempt-pattern playbook entry when a category reaches the threshold."""
    from app.playbook_learning import (
        _maybe_auto_playbook_from_first_attempt,
        _DIRECTION_FIRST_ATTEMPT_KEY,
        _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD,
    )

    doc: dict = {
        _DIRECTION_FIRST_ATTEMPT_KEY: {
            "dir-abc123": {
                "test-coverage": _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD,
                "style-conventions": 2,
                "total": _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD + 2,
                "last_updated": "2026-01-01T00:00:00",
            }
        }
    }
    created = _maybe_auto_playbook_from_first_attempt(doc, "dir-abc123", "create", "task-id-1")

    assert len(created) == 1
    key = created[0]
    assert "test-coverage" in key
    full_key = f"playbook/{key}"
    assert full_key in doc
    entry = doc[full_key]
    assert entry["source"] == "first-attempt-pattern"
    assert entry["rejection_count"] == _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD
    assert "create" in entry["applicable_types"]
    assert "test" in entry["content"]


def test_maybe_auto_playbook_skips_below_threshold():
    """No entry created when count is below threshold."""
    from app.playbook_learning import (
        _maybe_auto_playbook_from_first_attempt,
        _DIRECTION_FIRST_ATTEMPT_KEY,
        _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD,
    )

    doc: dict = {
        _DIRECTION_FIRST_ATTEMPT_KEY: {
            "dir-abc": {
                "test-coverage": _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD - 1,
                "total": _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD - 1,
            }
        }
    }
    created = _maybe_auto_playbook_from_first_attempt(doc, "dir-abc", "create", "task-id")
    assert created == []


def test_maybe_auto_playbook_deduplicates():
    """Skips entry creation if key already exists."""
    from app.playbook_learning import (
        _maybe_auto_playbook_from_first_attempt,
        _DIRECTION_FIRST_ATTEMPT_KEY,
        _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD,
    )

    dir_slug = "dirabc123"[:8]  # matches the slug logic: direction_key[:8].replace("-","").replace("_","")
    # Compute the key that would be generated for direction "dir-abc123"
    direction_key = "dir-abc123"
    expected_slug = direction_key[:8].replace("-", "").replace("_", "")
    existing_key = f"playbook/first-attempt-{expected_slug}-test-coverage"

    doc: dict = {
        _DIRECTION_FIRST_ATTEMPT_KEY: {
            direction_key: {
                "test-coverage": _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD + 2,
                "total": _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD + 2,
            }
        },
        existing_key: {"content": "already exists", "source": "first-attempt-pattern"},
    }
    created = _maybe_auto_playbook_from_first_attempt(doc, direction_key, "create", "task-id")
    assert created == []


# ─────────────────────────────────────────────────────────────────────────────
# Tests for extract_review_feedback_to_playbook — first-attempt (iteration < 3)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_review_feedback_accumulates_on_first_iteration(db_session: AsyncSession):
    """extract_review_feedback_to_playbook accumulates direction data on iteration == 1."""
    from app.playbook_learning import extract_review_feedback_to_playbook, _DIRECTION_FIRST_ATTEMPT_KEY

    project = await _make_project(db_session, design_doc={})
    review_history = [
        {
            "iteration": 0,
            "issues": [
                {"description": "missing unit tests for the handler", "severity": "MAJOR"},
                {"description": "no test coverage for edge cases", "severity": "MINOR"},
            ],
        },
    ]
    review_loop = {"iteration": 1, "review_history": review_history}
    task = Task(
        project_id=project.id,
        title="First-iter task",
        payloads=[{"type": "create", "prompt": "add handler"}],
        status=TaskStatus.COMPLETED,
        context={
            "direction_summary": {"id": "dir-cafebabe-1234", "title": "Test direction"},
        },
    )
    db_session.add(task)
    await db_session.flush()

    keys = await extract_review_feedback_to_playbook(
        task, review_loop, pr_number=99, original_task_type="create", session=db_session
    )
    await db_session.flush()

    doc = project.design_doc or {}

    # No full playbook entry created for iteration < 3
    review_keys = [k for k in doc if k.startswith("playbook/review-")]
    assert review_keys == [], f"Should not create review-extracted entries at iteration 1, got {review_keys}"

    # Direction tally was accumulated
    assert _DIRECTION_FIRST_ATTEMPT_KEY in doc, "direction_first_attempt_feedback should be written"
    tally = doc[_DIRECTION_FIRST_ATTEMPT_KEY].get("dir-cafebabe-1234")
    assert tally is not None
    assert tally.get("test-coverage", 0) >= 1
    assert tally.get("total", 0) >= 1


@pytest.mark.asyncio
async def test_extract_review_feedback_no_first_attempt_on_empty_history(db_session: AsyncSession):
    """extract_review_feedback_to_playbook returns [] with no review_history."""
    from app.playbook_learning import extract_review_feedback_to_playbook, _DIRECTION_FIRST_ATTEMPT_KEY

    project = await _make_project(db_session, design_doc={})
    review_loop = {"iteration": 1, "review_history": []}
    task = Task(
        project_id=project.id,
        title="Empty history task",
        payloads=[{"type": "create", "prompt": "add handler"}],
        status=TaskStatus.COMPLETED,
        context={},
    )
    db_session.add(task)
    await db_session.flush()

    keys = await extract_review_feedback_to_playbook(
        task, review_loop, pr_number=1, original_task_type="create", session=db_session
    )
    assert keys == []
    doc = project.design_doc or {}
    assert _DIRECTION_FIRST_ATTEMPT_KEY not in doc


@pytest.mark.asyncio
async def test_extract_review_feedback_auto_playbook_at_threshold(db_session: AsyncSession):
    """Auto-generates a first-attempt-pattern entry when direction tally hits the threshold."""
    from app.playbook_learning import (
        extract_review_feedback_to_playbook,
        _DIRECTION_FIRST_ATTEMPT_KEY,
        _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD,
    )

    direction_id = "dir-deadbeef-5678"
    # Pre-populate the tally so the next call pushes test-coverage over the threshold
    pre_count = _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD - 1
    project = await _make_project(db_session, design_doc={
        _DIRECTION_FIRST_ATTEMPT_KEY: {
            direction_id: {
                "test-coverage": pre_count,
                "total": pre_count,
                "last_updated": "2026-01-01T00:00:00",
            }
        }
    })
    review_history = [
        {
            "iteration": 0,
            "issues": [
                {"description": "missing unit tests", "severity": "MAJOR"},
            ],
        },
    ]
    review_loop = {"iteration": 1, "review_history": review_history}
    task = Task(
        project_id=project.id,
        title="Threshold task",
        payloads=[{"type": "create", "prompt": "add something"}],
        status=TaskStatus.COMPLETED,
        context={"direction_summary": {"id": direction_id, "title": "Test direction"}},
    )
    db_session.add(task)
    await db_session.flush()

    keys = await extract_review_feedback_to_playbook(
        task, review_loop, pr_number=100, original_task_type="create", session=db_session
    )
    await db_session.flush()

    doc = project.design_doc or {}
    # A first-attempt-pattern entry should have been auto-created
    fa_keys = [k for k in doc if k.startswith("playbook/first-attempt-")]
    assert len(fa_keys) == 1, f"Expected 1 first-attempt entry, got {fa_keys}"
    entry = doc[fa_keys[0]]
    assert entry["source"] == "first-attempt-pattern"
    assert entry["rejection_count"] == _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD
    # The auto-created key is in the returned list
    assert any("first-attempt" in k for k in keys)


@pytest.mark.asyncio
async def test_extract_review_feedback_still_creates_entries_at_3_plus(db_session: AsyncSession):
    """extract_review_feedback_to_playbook still creates review-extracted entries at iteration >= 3."""
    from app.playbook_learning import extract_review_feedback_to_playbook

    project = await _make_project(db_session, design_doc={})
    review_history = [
        {
            "iteration": 0,
            "issues": [
                {"description": "missing unit tests", "severity": "MAJOR"},
            ],
        },
        {
            "iteration": 1,
            "issues": [
                {"description": "still missing tests", "severity": "MAJOR"},
            ],
        },
        {
            "iteration": 2,
            "issues": [],
        },
    ]
    review_loop = {"iteration": 3, "review_history": review_history}
    task = Task(
        project_id=project.id,
        title="Multi-iter task",
        payloads=[{"type": "create", "prompt": "add endpoint"}],
        status=TaskStatus.COMPLETED,
        context={},
    )
    db_session.add(task)
    await db_session.flush()

    keys = await extract_review_feedback_to_playbook(
        task, review_loop, pr_number=42, original_task_type="create", session=db_session
    )
    await db_session.flush()

    doc = project.design_doc or {}
    review_keys = [k for k in doc if k.startswith("playbook/review-")]
    assert len(review_keys) >= 1, "Should create at least 1 review-extracted entry at iteration 3"
    assert any("review-test-coverage" in k for k in review_keys)
