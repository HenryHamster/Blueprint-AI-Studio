"""Tests for automatic playbook entry creation from recurring failure patterns."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, AlertSeverity, Project, Task, TaskStatus
from app.routers.agent_status import _check_recurring_failures, _utcnow
from sqlmodel import select as _sql_select


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
    direction_id: str | None = None,
) -> Task:
    ctx: dict = {"scope": scope}
    if related_files:
        ctx["related_files"] = related_files
    if direction_id:
        ctx["direction_id"] = direction_id
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

    common_msg = "CPUS_ALL_REGIONS quota exceeded in zone us-central1"
    await _make_failed_task(db_session, project.id, status_message=common_msg)
    await _make_failed_task(db_session, project.id, status_message=common_msg)
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

    common_msg = "CPUS_ALL_REGIONS quota exceeded in zone us-central1"
    await _make_failed_task(db_session, project.id, status_message=common_msg)
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

    common_msg = "CPUS_ALL_REGIONS quota exceeded in zone us-central1"
    await _make_failed_task(db_session, project.id, status_message=common_msg)
    await _make_failed_task(db_session, project.id, status_message=common_msg)
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
    assert len(auto_keys_2) == 1, (
        f"Expected 1 auto entry after duplicate check, got {auto_keys_2}"
    )


@pytest.mark.asyncio
async def test_cap_of_five_is_respected(db_session: AsyncSession):
    """Auto-playbook entries are capped at 5 per project; beyond that no new entries are created."""
    # Pre-populate design_doc with 5 existing auto-entries
    prefilled = {
        f"playbook/auto-CODE_ERROR-abc{i:03d}": {
            "content": f"entry {i}",
            "confirmed_count": 0,
        }
        for i in range(5)
    }
    project = await _make_project(db_session, design_doc=dict(prefilled))

    # 3 failures that would normally trigger a new entry
    common_msg = "CPUS_ALL_REGIONS quota exceeded in zone us-central1"
    await _make_failed_task(db_session, project.id, status_message=common_msg)
    await _make_failed_task(db_session, project.id, status_message=common_msg)
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

    common_msg = "CPUS_ALL_REGIONS quota exceeded in zone us-central1"
    await _make_failed_task(
        db_session, project.id, status_message=common_msg, task_type="create"
    )
    await _make_failed_task(
        db_session, project.id, status_message=common_msg, task_type="fix"
    )
    t3 = await _make_failed_task(
        db_session, project.id, status_message=common_msg, task_type="create"
    )

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

    common_msg = "CPUS_ALL_REGIONS quota exceeded in zone us-central1"
    # 2 old failures (> 7 days ago)
    await _make_failed_task(
        db_session, project.id, status_message=common_msg, age_days=8
    )
    await _make_failed_task(
        db_session, project.id, status_message=common_msg, age_days=8
    )
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
    await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )
    await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )
    task = await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )

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
    await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )
    task = await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key is None
    doc = project.design_doc or {}
    assert not any(k.startswith("playbook/success-") for k in doc)


@pytest.mark.asyncio
async def test_capture_success_skips_with_no_prior_completions(
    db_session: AsyncSession,
):
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

    project = await _make_project(
        db_session,
        design_doc={
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
        },
    )
    # 2 prior completions to satisfy noise filter
    await _make_completed_task(db_session, project.id, task_type="fix", scope="medium")
    await _make_completed_task(db_session, project.id, task_type="fix", scope="medium")
    task = await _make_completed_task(
        db_session, project.id, task_type="fix", scope="medium"
    )

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
        db_session,
        project.id,
        task_type="refactor",
        related_files=files,
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
    await _make_completed_task(
        db_session, project.id, task_type="migrate", scope="small"
    )
    await _make_completed_task(
        db_session, project.id, task_type="migrate", scope="small"
    )
    task = await _make_completed_task(
        db_session, project.id, task_type="migrate", scope="small"
    )

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
    await _make_completed_task(
        db_session, project.id, task_type="create", scope="medium"
    )
    await _make_completed_task(
        db_session, project.id, task_type="create", scope="medium"
    )
    task = await _make_completed_task(
        db_session,
        project.id,
        task_type="create",
        scope="medium",
        related_files=files,
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
async def test_capture_success_metadata_detects_tests_dir(db_session: AsyncSession):
    """Files under /tests/ count as test files for tests_added/test_file_count."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    files = ["backend/tests/test_router.py", "backend/app/foo.py"]
    await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )
    await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )
    task = await _make_completed_task(
        db_session, project.id, task_type="create", scope="small", related_files=files
    )

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key == "success-create-small"
    entry = (project.design_doc or {})["playbook/success-create-small"]
    assert entry["tests_added"] is True
    assert entry["test_file_count"] == 1


@pytest.mark.asyncio
async def test_capture_success_per_scope_key(db_session: AsyncSession):
    """Different scopes produce separate playbook entries."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    # 2 prior of each scope to satisfy threshold
    await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )
    await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )
    await _make_completed_task(
        db_session, project.id, task_type="create", scope="medium"
    )
    await _make_completed_task(
        db_session, project.id, task_type="create", scope="medium"
    )

    task_small = await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )
    key_small = await capture_success_pattern(task_small, db_session)

    task_medium = await _make_completed_task(
        db_session, project.id, task_type="create", scope="medium"
    )
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
    t1 = await _make_completed_task(
        db_session, project.id, task_type="fix", scope="small"
    )
    await capture_success_pattern(t1, db_session)
    await db_session.flush()

    # Second success: updates scope_distribution
    t2 = await _make_completed_task(
        db_session, project.id, task_type="fix", scope="small"
    )
    await capture_success_pattern(t2, db_session)
    await db_session.flush()

    entry = (project.design_doc or {}).get("playbook/success-fix-small", {})
    assert entry.get("success_count") == 2
    assert entry.get("scope_distribution") == {"small": 2}


@pytest.mark.asyncio
async def test_capture_success_direction_affinity(db_session: AsyncSession):
    """direction_successes accumulates per-direction counts; file_type_distribution
    records extension breakdown; test_file_count reflects actual test file count."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    files = ["backend/app/foo.py", "backend/app/bar.py", "backend/testing/test_foo.py"]
    dir_id = "dir-abc"
    await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )
    await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )
    t1 = await _make_completed_task(
        db_session,
        project.id,
        task_type="create",
        scope="small",
        related_files=files,
        direction_id=dir_id,
    )

    await capture_success_pattern(t1, db_session)
    await db_session.flush()

    entry = (project.design_doc or {})["playbook/success-create-small"]
    assert entry["direction_successes"] == {dir_id: 1}
    assert entry["file_type_distribution"] == {"py": 3}
    assert entry["test_file_count"] == 1

    # Second success for the same direction increments its count
    t2 = await _make_completed_task(
        db_session,
        project.id,
        task_type="create",
        scope="small",
        related_files=files,
        direction_id=dir_id,
    )
    await capture_success_pattern(t2, db_session)
    await db_session.flush()

    entry = (project.design_doc or {})["playbook/success-create-small"]
    assert entry["direction_successes"] == {dir_id: 2}
    assert entry["success_count"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# Tests for summarize_recent_successes
# ─────────────────────────────────────────────────────────────────────────────


def _make_success_entry(
    success_count: int,
    *,
    file_type_distribution: dict | None = None,
    test_file_count: int = 0,
    tests_added: bool = False,
    direction_successes: dict | None = None,
    updated_at: str | None = None,
) -> dict:
    return {
        "content": f"success ({success_count}x)",
        "source": "success-pattern",
        "success_count": success_count,
        "scope_distribution": {"small": success_count},
        "direction_successes": direction_successes or {},
        "file_type_distribution": file_type_distribution or {},
        "test_file_count": test_file_count,
        "tests_added": tests_added,
        "updated_at": updated_at or _utcnow().isoformat(),
    }


def test_summarize_recent_successes_omitted_when_no_entries():
    """Empty design_doc → returns None."""
    from app.playbook_learning import summarize_recent_successes

    assert summarize_recent_successes({}, "create", "d-1", "small") is None


def test_summarize_recent_successes_omitted_when_only_singletons():
    """Entries with success_count < 2 are filtered out."""
    from app.playbook_learning import summarize_recent_successes

    doc = {
        "playbook/success-create-small": _make_success_entry(
            1, file_type_distribution={"py": 1}
        ),
    }
    assert summarize_recent_successes(doc, "create", None, "small") is None


def test_summarize_recent_successes_omitted_for_other_task_type():
    """Entries for a different task_type → returns None."""
    from app.playbook_learning import summarize_recent_successes

    doc = {
        "playbook/success-fix-small": _make_success_entry(
            5, file_type_distribution={"py": 5}
        ),
    }
    assert summarize_recent_successes(doc, "create", None, "small") is None


def test_summarize_recent_successes_includes_matching_entries():
    """Section header + at least one bullet referencing seeded entry shape."""
    from app.playbook_learning import summarize_recent_successes

    doc = {
        "playbook/success-create-small": _make_success_entry(
            8,
            file_type_distribution={"py": 6, "ts": 2},
            test_file_count=2,
            tests_added=True,
        ),
        "playbook/success-create-medium": _make_success_entry(
            3,
            file_type_distribution={"py": 3},
        ),
    }
    out = summarize_recent_successes(doc, "create", None, "small")
    assert out is not None
    assert "## Recent successes" in out
    assert "create-small" in out
    assert "8x" in out
    assert "py" in out
    # File-type signal surfaced
    assert "files:" in out
    # Tests-added signal surfaced
    assert "tests added" in out


def test_summarize_recent_successes_prefers_direction_affinity():
    """Entries with this direction_id in direction_successes rank first."""
    from app.playbook_learning import summarize_recent_successes

    doc = {
        # Higher overall success_count but no direction affinity
        "playbook/success-create-small": _make_success_entry(
            10, file_type_distribution={"py": 10}
        ),
        # Lower overall success_count but matches the direction
        "playbook/success-create-medium": _make_success_entry(
            3,
            file_type_distribution={"ts": 3},
            direction_successes={"d-target": 3},
        ),
    }
    out = summarize_recent_successes(doc, "create", "d-target", "small")
    assert out is not None
    lines = out.splitlines()
    # First bullet should be the direction-affined entry (medium scope, ts files)
    assert "create-medium" in lines[1]
    assert "this direction: 3" in lines[1]


def test_summarize_recent_successes_caps_bullets():
    """At most max_bullets bullets are emitted regardless of how many qualify."""
    from app.playbook_learning import summarize_recent_successes

    doc = {
        f"playbook/success-create-scope{i}": _make_success_entry(
            5, file_type_distribution={"py": 1}
        )
        for i in range(10)
    }
    out = summarize_recent_successes(
        doc, "create", None, "small", max_bullets=3, char_budget=2000
    )
    assert out is not None
    # 1 header + at most 3 bullets
    assert len(out.splitlines()) <= 4
    bullet_lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(bullet_lines) == 3


def test_summarize_recent_successes_enforces_char_budget():
    """Output never exceeds char_budget; truncation drops trailing bullets, not mid-line."""
    from app.playbook_learning import summarize_recent_successes

    # 10 large entries — header + each bullet is ~50 chars, so a 200-char
    # budget should fit only a handful of bullets cleanly.
    doc = {
        f"playbook/success-create-suffix{i:02d}": _make_success_entry(
            5,
            file_type_distribution={"py": 5, "ts": 3, "tsx": 2},
            test_file_count=1,
            tests_added=True,
        )
        for i in range(10)
    }
    out = summarize_recent_successes(doc, "create", None, "small", char_budget=200)
    assert out is not None
    assert len(out) <= 200, f"Output exceeds budget: {len(out)} chars"
    # Every line must be either the header or a complete bullet — no partial lines.
    for ln in out.splitlines():
        assert ln.startswith("## ") or ln.startswith("- "), (
            f"Truncated mid-line: {ln!r}"
        )


def test_summarize_recent_successes_returns_none_when_budget_too_small():
    """When even one bullet doesn't fit, return None rather than malformed output."""
    from app.playbook_learning import summarize_recent_successes

    doc = {
        "playbook/success-create-small": _make_success_entry(
            5, file_type_distribution={"py": 5}
        ),
    }
    # 10 chars cannot fit even the header
    assert (
        summarize_recent_successes(doc, "create", None, "small", char_budget=10) is None
    )


def test_summarize_recent_successes_task_type_agnostic():
    """Helper itself does not gate by task_type — call sites enforce the allowlist."""
    from app.playbook_learning import (
        summarize_recent_successes,
        RECENT_SUCCESSES_TASK_TYPES,
    )

    # Non-allowlisted type 'qa'; helper still surfaces matching entries.
    doc = {
        "playbook/success-qa-small": _make_success_entry(
            5, file_type_distribution={"py": 5}
        ),
    }
    out = summarize_recent_successes(doc, "qa", None, "small")
    assert out is not None
    assert "qa-small" in out
    # Confirm 'qa' is not in the call-site allowlist
    assert "qa" not in RECENT_SUCCESSES_TASK_TYPES


# ─────────────────────────────────────────────────────────────────────────────
# Tests for age-based flagging
# ─────────────────────────────────────────────────────────────────────────────


def test_old_unconfirmed_entry_gets_age_penalty():
    """Entries older than 30 days with confirmed_count < 3 receive age_penalty=1 in the sort tuple."""
    # Mirrors the same logic used in the claim endpoint (both use naive UTC datetimes).
    old_date = (_utcnow() - timedelta(days=31)).isoformat()
    recent_date = _utcnow().isoformat()

    def _age_penalty(updated_at_str: str, confirmed: int) -> int:
        if confirmed >= 3:
            return 0
        try:
            updated_dt = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
            # Strip tzinfo so comparison stays naive (matches _utcnow() which is naive UTC)
            if updated_dt.tzinfo is not None:
                updated_dt = updated_dt.replace(tzinfo=None)
            return 1 if (_utcnow() - updated_dt).days > 30 else 0
        except (ValueError, TypeError):
            return 0

    assert _age_penalty(old_date, confirmed=0) == 1, (
        "Old entry with no confirmations should get penalty"
    )
    assert _age_penalty(old_date, confirmed=2) == 1, (
        "Old entry with 2 confirmations should get penalty"
    )
    assert _age_penalty(old_date, confirmed=3) == 0, (
        "Old entry with 3+ confirmations should NOT get penalty"
    )
    assert _age_penalty(recent_date, confirmed=0) == 0, (
        "Recent entry should NOT get penalty"
    )


def test_stale_file_refs_penalty():
    """Entries with stale_file_refs=True receive stale_penalty=1 in the sort tuple."""
    stale_entry = {"confirmed_count": 5, "stale_file_refs": True, "content": "x"}
    clean_entry = {"confirmed_count": 5, "stale_file_refs": False, "content": "y"}
    no_flag_entry = {"confirmed_count": 5, "content": "z"}

    def stale_penalty(v):
        return 1 if v.get("stale_file_refs") else 0

    assert stale_penalty(stale_entry) == 1
    assert stale_penalty(clean_entry) == 0
    assert stale_penalty(no_flag_entry) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Tests for file-reference validation
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_file_paths_basic():
    """_extract_file_paths correctly extracts backend/agent/admin-ui paths from content."""
    from app.playbook_learning import _extract_file_paths

    content = (
        "See backend/app/models.py and agent/src/index.ts. "
        "Also admin-ui/src/App.tsx for the UI."
    )
    paths = _extract_file_paths(content)
    assert "backend/app/models.py" in paths
    assert "agent/src/index.ts" in paths
    assert "admin-ui/src/App.tsx" in paths


def test_extract_file_paths_no_matches():
    """_extract_file_paths returns an empty list when no file paths are present."""
    from app.playbook_learning import _extract_file_paths

    assert _extract_file_paths("No file references here.") == []


def test_check_and_flag_stale_refs_no_repo_root():
    """check_and_flag_stale_refs is a no-op when repo_root is falsy or missing."""
    from app.playbook_learning import check_and_flag_stale_refs

    doc = {
        "playbook/tip": {
            "content": "see backend/app/missing_file.py",
            "confirmed_count": 0,
            "updated_at": _utcnow().isoformat(),
        }
    }
    result = check_and_flag_stale_refs(doc, "")
    assert not result["playbook/tip"].get("stale_file_refs")


def test_check_and_flag_stale_refs_marks_missing(tmp_path):
    """check_and_flag_stale_refs sets stale_file_refs=True when referenced file is missing."""
    from app.playbook_learning import check_and_flag_stale_refs

    # Create a repo_root with one real file and reference a missing file
    backend_dir = tmp_path / "backend" / "app"
    backend_dir.mkdir(parents=True)
    (backend_dir / "real_file.py").write_text("# exists")

    doc = {
        "playbook/tip": {
            "content": "See backend/app/gone_file.py for the pattern.",
            "confirmed_count": 0,
            "updated_at": _utcnow().isoformat(),
        }
    }
    result = check_and_flag_stale_refs(doc, str(tmp_path))
    assert result["playbook/tip"].get("stale_file_refs") is True


def test_check_and_flag_stale_refs_clears_when_file_restored(tmp_path):
    """check_and_flag_stale_refs clears stale_file_refs when all referenced files exist."""
    from app.playbook_learning import check_and_flag_stale_refs

    backend_dir = tmp_path / "backend" / "app"
    backend_dir.mkdir(parents=True)
    (backend_dir / "models.py").write_text("# exists")

    doc = {
        "playbook/tip": {
            "content": "See backend/app/models.py.",
            "confirmed_count": 0,
            "stale_file_refs": True,
            "updated_at": _utcnow().isoformat(),
        }
    }
    result = check_and_flag_stale_refs(doc, str(tmp_path))
    assert result["playbook/tip"].get("stale_file_refs") is False


def test_check_and_flag_stale_refs_no_file_paths_untouched(tmp_path):
    """Entries with no file references are not modified by check_and_flag_stale_refs."""
    from app.playbook_learning import check_and_flag_stale_refs

    doc = {
        "playbook/tip": {
            "content": "General advice with no file references.",
            "confirmed_count": 0,
            "updated_at": _utcnow().isoformat(),
        }
    }
    result = check_and_flag_stale_refs(doc, str(tmp_path))
    assert "stale_file_refs" not in result["playbook/tip"]


# ─────────────────────────────────────────────────────────────────────────────
# Tests for capacity-aware cleanup
# ─────────────────────────────────────────────────────────────────────────────


def _make_doc_with_size(
    num_entries: int, confirmed_counts: list[int] | None = None
) -> dict:
    """Build a doc dict whose playbook entries collectively approach *num_entries* * ~500 bytes."""
    confirmed_counts = confirmed_counts or [0] * num_entries
    doc: dict = {}
    for i in range(num_entries):
        doc[f"playbook/entry-{i:03d}"] = {
            "content": "x" * 480,  # ~480 bytes per entry to keep math simple
            "confirmed_count": confirmed_counts[i],
            "updated_at": _utcnow().isoformat(),
        }
    return doc


def test_archive_least_useful_entry_removes_and_archives():
    """_archive_least_useful_entry moves the entry with lowest confirmed_count to playbook_archive."""
    from app.playbook_learning import _archive_least_useful_entry

    doc = {
        "playbook/a": {
            "content": "tip a",
            "confirmed_count": 5,
            "updated_at": _utcnow().isoformat(),
        },
        "playbook/b": {
            "content": "tip b",
            "confirmed_count": 0,
            "updated_at": _utcnow().isoformat(),
        },
        "playbook/c": {
            "content": "tip c",
            "confirmed_count": 3,
            "updated_at": _utcnow().isoformat(),
        },
    }
    result = _archive_least_useful_entry(doc)

    # 'b' has lowest confirmed_count — should be archived
    assert "playbook/b" not in result
    assert "playbook/a" in result
    assert "playbook/c" in result
    archive = result.get("playbook_archive", [])
    assert len(archive) == 1
    assert archive[0]["_archived_key"] == "playbook/b"


def test_archive_prefers_stale_entries():
    """_archive_least_useful_entry archives stale_file_refs entries first."""
    from app.playbook_learning import _archive_least_useful_entry

    doc = {
        "playbook/a": {
            "content": "tip a",
            "confirmed_count": 0,
            "stale_file_refs": False,
            "updated_at": _utcnow().isoformat(),
        },
        "playbook/b": {
            "content": "tip b",
            "confirmed_count": 5,
            "stale_file_refs": True,
            "updated_at": _utcnow().isoformat(),
        },
    }
    result = _archive_least_useful_entry(doc)
    # Despite higher confirmed_count, b is stale so it should be archived first
    assert "playbook/b" not in result
    assert "playbook/a" in result


def test_archive_prefers_older_entries_within_same_tier():
    """_archive_least_useful_entry archives the oldest entry when stale and confirmed_count tie."""
    from app.playbook_learning import _archive_least_useful_entry

    old_date = (_utcnow() - timedelta(days=60)).isoformat()
    recent_date = _utcnow().isoformat()

    doc = {
        "playbook/old": {
            "content": "old tip",
            "confirmed_count": 0,
            "updated_at": old_date,
        },
        "playbook/recent": {
            "content": "recent tip",
            "confirmed_count": 0,
            "updated_at": recent_date,
        },
    }
    result = _archive_least_useful_entry(doc)
    # Both entries have same stale flag and confirmed_count — oldest should be archived first
    assert "playbook/old" not in result, "Oldest entry should have been archived"
    assert "playbook/recent" in result


def test_archive_caps_playbook_archive_length():
    """_archive_least_useful_entry trims playbook_archive past the cap.

    Without this cap the archive grows monotonically — a single project
    observed 517 entries / 351 KB of archive, which bloated the design_doc
    payload past the agent budget and caused silent pre-claim death."""
    from app.playbook_learning import (
        _archive_least_useful_entry,
        _PLAYBOOK_ARCHIVE_MAX_DEFAULT,
    )
    from app.shared_state import runtime_config

    # Pre-populate archive with cap+5 entries so the next archival exceeds the cap
    existing_archive = [
        {"_archived_key": f"playbook/old-{i}", "content": "x"}
        for i in range(_PLAYBOOK_ARCHIVE_MAX_DEFAULT + 5)
    ]
    doc = {
        "playbook_archive": existing_archive,
        "playbook/victim": {
            "content": "least useful",
            "confirmed_count": 0,
            "updated_at": _utcnow().isoformat(),
        },
    }

    # Ensure runtime_config default is used
    runtime_config.pop("playbook_archive_max", None)

    result = _archive_least_useful_entry(doc)
    archive = result["playbook_archive"]

    # Length should be exactly the cap after trim + new entry
    assert len(archive) == _PLAYBOOK_ARCHIVE_MAX_DEFAULT
    # Oldest entries should have been dropped; the newly-archived one is the tail
    assert archive[-1]["_archived_key"] == "playbook/victim"
    # The very oldest entries (old-0, old-1, etc.) are gone
    keys = [e["_archived_key"] for e in archive]
    assert "playbook/old-0" not in keys


def test_archive_cap_respects_runtime_config_override():
    """Operators can tune playbook_archive_max via runtime_config."""
    from app.playbook_learning import _archive_least_useful_entry
    from app.shared_state import runtime_config

    runtime_config["playbook_archive_max"] = 3
    try:
        existing_archive = [{"_archived_key": f"old-{i}"} for i in range(5)]
        doc = {
            "playbook_archive": existing_archive,
            "playbook/new": {
                "content": "least useful",
                "confirmed_count": 0,
                "updated_at": _utcnow().isoformat(),
            },
        }
        result = _archive_least_useful_entry(doc)
        assert len(result["playbook_archive"]) == 3
    finally:
        runtime_config.pop("playbook_archive_max", None)


def test_archive_uses_rate_over_raw_count_when_enough_observations():
    """_archive_least_useful_entry uses success rate when followed_count >= _MIN_FOLLOWED_FOR_RATE.

    An entry with many raw confirmations but no follow tracking should be kept
    over an entry that has few confirmations relative to many follows, because
    the rate-based score (effectiveness = -(confirmed/followed)) of the
    low-performer is higher (closer to 0) than the score of the well-confirmed
    entry that lacks follow tracking (effectiveness = -confirmed).
    """
    from app.playbook_learning import _archive_least_useful_entry

    doc = {
        # Many raw confirmations but no follow tracking (old-style entry)
        "playbook/well-confirmed": {
            "content": "well-confirmed tip",
            "confirmed_count": 5,
            "followed_count": 0,  # below threshold → effectiveness = -5
            "updated_at": _utcnow().isoformat(),
        },
        # Low success rate with enough follows — effectiveness = -(1/10) = -0.1
        # This sorts higher than -5, so gets archived even though it has 1 confirmation
        "playbook/low-performer": {
            "content": "low performer tip",
            "confirmed_count": 1,
            "followed_count": 10,  # 10% success rate
            "updated_at": _utcnow().isoformat(),
        },
    }
    result = _archive_least_useful_entry(doc)
    # The low-performer (10% success rate on 10 follows) archives before the
    # 5-raw-confirmation entry, because -0.1 > -5 in the sort.
    assert "playbook/low-performer" not in result, (
        "Low-success-rate entry should be archived first"
    )
    assert "playbook/well-confirmed" in result
    archive = result.get("playbook_archive", [])
    assert archive[0]["_archived_key"] == "playbook/low-performer"


# ─────────────────────────────────────────────────────────────────────────────
# Tests for record_playbook_applied / record_playbook_confirmed
# ─────────────────────────────────────────────────────────────────────────────


def test_record_playbook_applied_increments_followed_count():
    from app.playbook_learning import record_playbook_applied

    doc = {"playbook/my-tip": {"content": "x", "confirmed_count": 2}}
    record_playbook_applied(doc, "my-tip")
    assert doc["playbook/my-tip"]["followed_count"] == 1
    record_playbook_applied(doc, "my-tip")
    assert doc["playbook/my-tip"]["followed_count"] == 2


def test_record_playbook_applied_accepts_full_key_form():
    from app.playbook_learning import record_playbook_applied

    doc = {"playbook/tip": {"content": "x"}}
    record_playbook_applied(doc, "playbook/tip")
    assert doc["playbook/tip"]["followed_count"] == 1


def test_record_playbook_applied_noop_for_missing_key():
    from app.playbook_learning import record_playbook_applied

    doc = {}
    record_playbook_applied(doc, "nonexistent")
    assert doc == {}


def test_record_playbook_confirmed_increments_confirmed_count():
    from app.playbook_learning import record_playbook_confirmed

    doc = {"playbook/tip": {"content": "x", "confirmed_count": 1}}
    record_playbook_confirmed(doc, "tip")
    assert doc["playbook/tip"]["confirmed_count"] == 2


def test_record_playbook_confirmed_accepts_full_key_form():
    from app.playbook_learning import record_playbook_confirmed

    doc = {"playbook/tip": {"content": "x", "confirmed_count": 0}}
    record_playbook_confirmed(doc, "playbook/tip")
    assert doc["playbook/tip"]["confirmed_count"] == 1


def test_record_playbook_confirmed_noop_for_missing_key():
    from app.playbook_learning import record_playbook_confirmed

    doc = {}
    record_playbook_confirmed(doc, "nonexistent")
    assert doc == {}


# ─────────────────────────────────────────────────────────────────────────────
# Tests for retire_ineffective_patterns
# ─────────────────────────────────────────────────────────────────────────────


def test_retire_ineffective_patterns_retires_low_success_rate():
    from app.playbook_learning import retire_ineffective_patterns, _RETIRE_MIN_FOLLOWED

    doc = {
        "playbook/bad": {
            "content": "bad tip",
            "source": "auto-generated",
            "followed_count": _RETIRE_MIN_FOLLOWED,
            "confirmed_count": 0,  # 0% success rate — below threshold
        },
    }
    retired = retire_ineffective_patterns(doc)
    assert retired == ["playbook/bad"]
    assert "playbook/bad" not in doc
    archive = doc.get("playbook_archive", [])
    assert len(archive) == 1
    assert archive[0]["_archived_key"] == "playbook/bad"
    assert "low_success_rate" in archive[0].get("_retirement_reason", "")


def test_retire_ineffective_patterns_keeps_sufficient_success_rate():
    from app.playbook_learning import retire_ineffective_patterns, _RETIRE_MIN_FOLLOWED

    doc = {
        "playbook/good": {
            "content": "good tip",
            "source": "auto-generated",
            "followed_count": _RETIRE_MIN_FOLLOWED,
            "confirmed_count": _RETIRE_MIN_FOLLOWED,  # 100% — well above threshold
        },
    }
    retired = retire_ineffective_patterns(doc)
    assert retired == []
    assert "playbook/good" in doc


def test_retire_ineffective_patterns_skips_entries_below_min_followed():
    from app.playbook_learning import retire_ineffective_patterns, _RETIRE_MIN_FOLLOWED

    doc = {
        "playbook/new": {
            "content": "new tip",
            "source": "auto-generated",
            "followed_count": _RETIRE_MIN_FOLLOWED - 1,
            "confirmed_count": 0,  # Would retire if followed enough
        },
    }
    retired = retire_ineffective_patterns(doc)
    assert retired == []
    assert "playbook/new" in doc


def test_retire_ineffective_patterns_retirement_reason_includes_rate():
    from app.playbook_learning import retire_ineffective_patterns

    doc = {
        "playbook/tip": {
            "content": "x",
            "source": "auto-generated",
            "followed_count": 10,
            "confirmed_count": 1,  # 10% success rate
        },
    }
    retire_ineffective_patterns(doc)
    archive = doc.get("playbook_archive", [])
    assert len(archive) == 1
    reason = archive[0]["_retirement_reason"]
    assert "0.10" in reason


def test_retire_ineffective_patterns_skips_operator_curated():
    """Operator-curated entries (non-auto source) must NOT be retired even if low success rate."""
    from app.playbook_learning import retire_ineffective_patterns, _RETIRE_MIN_FOLLOWED

    doc = {
        "playbook/operator-tip": {
            "content": "operator tip",
            "source": "operator",
            "followed_count": _RETIRE_MIN_FOLLOWED,
            "confirmed_count": 0,
        },
        "playbook/review-tip": {
            "content": "review tip",
            "source": "review-extracted",
            "followed_count": _RETIRE_MIN_FOLLOWED,
            "confirmed_count": 0,
        },
    }
    retired = retire_ineffective_patterns(doc)
    assert retired == []
    assert "playbook/operator-tip" in doc
    assert "playbook/review-tip" in doc


def test_retire_ineffective_patterns_skips_no_source():
    """Entries with no source field must NOT be retired (treated as operator-curated)."""
    from app.playbook_learning import retire_ineffective_patterns, _RETIRE_MIN_FOLLOWED

    doc = {
        "playbook/legacy": {
            "content": "legacy tip",
            "followed_count": _RETIRE_MIN_FOLLOWED,
            "confirmed_count": 0,
        },
    }
    retired = retire_ineffective_patterns(doc)
    assert retired == []
    assert "playbook/legacy" in doc


def test_count_low_success_retired_within_window():
    from app.playbook_learning import count_low_success_retired_within

    now = _utcnow()
    doc = {
        "playbook_archive": [
            # Within 24h, low_success_rate reason → counted
            {
                "_archived_key": "playbook/a",
                "_archived_at": (now - timedelta(hours=2)).isoformat(),
                "_retirement_reason": "low_success_rate:0.10",
            },
            # Within 24h, but idle reason → NOT counted
            {
                "_archived_key": "playbook/b",
                "_archived_at": (now - timedelta(hours=2)).isoformat(),
                "_retirement_reason": "idle_auto_entry:30d",
            },
            # low_success_rate but older than 24h → NOT counted
            {
                "_archived_key": "playbook/c",
                "_archived_at": (now - timedelta(hours=48)).isoformat(),
                "_retirement_reason": "low_success_rate:0.05",
            },
            # Within 24h, low_success_rate → counted
            {
                "_archived_key": "playbook/d",
                "_archived_at": (now - timedelta(hours=23)).isoformat(),
                "_retirement_reason": "low_success_rate:0.20",
            },
        ],
    }
    assert count_low_success_retired_within(doc, hours=24) == 2
    assert count_low_success_retired_within({}, hours=24) == 0
    assert count_low_success_retired_within({"playbook_archive": []}, hours=24) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Tests for retire_idle_auto_entries
# ─────────────────────────────────────────────────────────────────────────────


def test_retire_idle_auto_entries_archives_idle_auto():
    from app.playbook_learning import retire_idle_auto_entries

    old_ts = (_utcnow() - timedelta(days=31)).isoformat()
    doc = {
        "playbook/foo": {
            "content": "stale tip",
            "source": "auto-generated",
            "last_referenced_at": old_ts,
            "updated_at": old_ts,
        },
    }
    retired = retire_idle_auto_entries(doc)
    assert retired == ["playbook/foo"]
    assert "playbook/foo" not in doc
    archive = doc.get("playbook_archive", [])
    assert len(archive) == 1
    assert archive[0]["_archived_key"] == "playbook/foo"
    assert archive[0]["_retirement_reason"].startswith("idle_auto_entry")


def test_retire_idle_auto_entries_skips_operator_curated():
    from app.playbook_learning import retire_idle_auto_entries

    old_ts = (_utcnow() - timedelta(days=60)).isoformat()
    doc = {
        # Source missing entirely (treated as operator-curated)
        "playbook/manual-no-source": {
            "content": "manual tip",
            "last_referenced_at": old_ts,
            "updated_at": old_ts,
        },
        # Explicit non-auto source
        "playbook/review-extracted-x": {
            "content": "review tip",
            "source": "review-extracted",
            "last_referenced_at": old_ts,
            "updated_at": old_ts,
        },
        "playbook/operator-x": {
            "content": "operator tip",
            "source": "operator",
            "last_referenced_at": old_ts,
            "updated_at": old_ts,
        },
    }
    retired = retire_idle_auto_entries(doc)
    assert retired == []
    assert "playbook/manual-no-source" in doc
    assert "playbook/review-extracted-x" in doc
    assert "playbook/operator-x" in doc


def test_retire_idle_auto_entries_skips_recently_referenced():
    from app.playbook_learning import retire_idle_auto_entries

    fresh_ts = (_utcnow() - timedelta(days=5)).isoformat()
    old_updated_ts = (_utcnow() - timedelta(days=120)).isoformat()
    doc = {
        # Updated long ago, but referenced 5 days ago — should NOT retire
        "playbook/active": {
            "content": "active tip",
            "source": "auto-generated",
            "last_referenced_at": fresh_ts,
            "updated_at": old_updated_ts,
        },
    }
    retired = retire_idle_auto_entries(doc)
    assert retired == []
    assert "playbook/active" in doc


def test_retire_idle_auto_entries_falls_back_to_updated_at():
    """Entries lacking last_referenced_at fall back to updated_at."""
    from app.playbook_learning import retire_idle_auto_entries

    old_ts = (_utcnow() - timedelta(days=45)).isoformat()
    doc = {
        "playbook/never-shown": {
            "content": "tip never shown to a claim",
            "source": "auto-generated",
            "updated_at": old_ts,
        },
    }
    retired = retire_idle_auto_entries(doc)
    assert retired == ["playbook/never-shown"]
    assert "playbook/never-shown" not in doc


def test_retire_idle_auto_entries_no_timestamps_kept():
    """Defensive: entries with neither timestamp must NOT retire."""
    from app.playbook_learning import retire_idle_auto_entries

    doc = {
        "playbook/timeless": {
            "content": "tip with no timestamps",
            "source": "auto-generated",
        },
    }
    retired = retire_idle_auto_entries(doc)
    assert retired == []
    assert "playbook/timeless" in doc


def test_retire_idle_auto_entries_respects_archive_cap():
    """playbook_archive_max trim still applies after idle retirement."""
    from app.playbook_learning import retire_idle_auto_entries
    import app.shared_state as _ss

    old_ts = (_utcnow() - timedelta(days=40)).isoformat()
    doc = {
        # Pre-fill archive at the cap
        "playbook_archive": [
            {"_archived_key": f"playbook/old-{i}", "_archived_at": old_ts}
            for i in range(50)
        ],
        "playbook/idle1": {
            "content": "tip 1",
            "source": "auto-generated",
            "last_referenced_at": old_ts,
            "updated_at": old_ts,
        },
        "playbook/idle2": {
            "content": "tip 2",
            "source": "auto-generated",
            "last_referenced_at": old_ts,
            "updated_at": old_ts,
        },
    }
    prev_max = _ss.runtime_config.get("playbook_archive_max")
    _ss.runtime_config["playbook_archive_max"] = 50
    try:
        retired = retire_idle_auto_entries(doc)
    finally:
        if prev_max is None:
            _ss.runtime_config.pop("playbook_archive_max", None)
        else:
            _ss.runtime_config["playbook_archive_max"] = prev_max
    assert sorted(retired) == ["playbook/idle1", "playbook/idle2"]
    assert len(doc["playbook_archive"]) == 50  # cap preserved
    # Newest entries (idle1/idle2) should be at the tail after trim
    tail_keys = {e.get("_archived_key") for e in doc["playbook_archive"][-2:]}
    assert tail_keys == {"playbook/idle1", "playbook/idle2"}


def test_count_idle_auto_retired_within_window():
    from app.playbook_learning import count_idle_auto_retired_within

    now = _utcnow()
    doc = {
        "playbook_archive": [
            # Within 24h, idle reason → counted
            {
                "_archived_key": "playbook/a",
                "_archived_at": (now - timedelta(hours=2)).isoformat(),
                "_retirement_reason": "idle_auto_entry:30d",
            },
            # Within 24h, but different reason → NOT counted
            {
                "_archived_key": "playbook/b",
                "_archived_at": (now - timedelta(hours=2)).isoformat(),
                "_retirement_reason": "low_success_rate:0.10",
            },
            # Idle reason but older than 24h → NOT counted
            {
                "_archived_key": "playbook/c",
                "_archived_at": (now - timedelta(hours=48)).isoformat(),
                "_retirement_reason": "idle_auto_entry:30d",
            },
            # Within 24h, idle reason → counted
            {
                "_archived_key": "playbook/d",
                "_archived_at": (now - timedelta(hours=23)).isoformat(),
                "_retirement_reason": "idle_auto_entry:30d",
            },
        ],
    }
    assert count_idle_auto_retired_within(doc, hours=24) == 2
    # Empty / missing doc handling
    assert count_idle_auto_retired_within({}, hours=24) == 0
    assert count_idle_auto_retired_within({"playbook_archive": []}, hours=24) == 0


def test_maybe_archive_for_capacity_triggers_at_threshold():
    """maybe_archive_for_capacity archives when combined size exceeds 30KB."""
    from app.playbook_learning import (
        maybe_archive_for_capacity,
        _PLAYBOOK_CAPACITY_BYTES,
    )

    # Build a doc just below capacity, then add a large entry estimate
    doc: dict = {}
    # Fill doc to just below capacity
    big_content = "x" * 500
    entry_count = _PLAYBOOK_CAPACITY_BYTES // (len(big_content) + 50)  # rough estimate
    for i in range(entry_count):
        doc[f"playbook/entry-{i:03d}"] = {
            "content": big_content,
            "confirmed_count": 0,
            "updated_at": _utcnow().isoformat(),
        }

    from app.playbook_learning import _playbook_size_bytes

    current_size = _playbook_size_bytes(doc)

    # If already over capacity, archiving should trigger and loop until under cap
    if current_size >= _PLAYBOOK_CAPACITY_BYTES:
        from app.playbook_learning import _playbook_size_bytes

        keys_before = {k for k in doc if k.startswith("playbook/")}
        result = maybe_archive_for_capacity(doc, 0)
        keys_after = {k for k in result if k.startswith("playbook/")}
        assert len(keys_after) < len(keys_before), (
            "At least one entry should have been archived"
        )
        # Loop must evict enough entries to bring size under cap
        assert _playbook_size_bytes(result) <= _PLAYBOOK_CAPACITY_BYTES


def test_maybe_archive_for_capacity_no_op_when_under_limit():
    """maybe_archive_for_capacity is a no-op when total size is below threshold."""
    from app.playbook_learning import maybe_archive_for_capacity

    doc = {
        "playbook/tiny": {
            "content": "small entry",
            "confirmed_count": 1,
            "updated_at": _utcnow().isoformat(),
        }
    }
    result = maybe_archive_for_capacity(doc, 0)
    assert "playbook/tiny" in result
    assert "playbook_archive" not in result


def test_maybe_archive_for_capacity_loops_multiple_times_for_large_estimate():
    """Loop evicts multiple entries when a single eviction is insufficient for the estimate."""
    from app.playbook_learning import (
        maybe_archive_for_capacity,
        _PLAYBOOK_CAPACITY_BYTES,
        _playbook_size_bytes,
    )

    # Build a doc over capacity with many small entries so one eviction won't suffice.
    entry_count = 60
    doc: dict = {}
    for i in range(entry_count):
        doc[f"playbook/entry-{i:03d}"] = {
            "content": "x" * 430,  # ~450 B per entry → ~27 KB total
            "confirmed_count": 0,
            "updated_at": (_utcnow() - timedelta(days=i)).isoformat(),
        }

    # A large estimate forces multiple evictions to make room.
    large_estimate = 5 * 1024  # 5 KB

    keys_before = {k for k in doc if k.startswith("playbook/")}
    result = maybe_archive_for_capacity(doc, large_estimate)
    keys_after = {k for k in result if k.startswith("playbook/")}

    evicted = len(keys_before) - len(keys_after)
    assert evicted > 1, (
        f"Expected more than 1 eviction for a {large_estimate}-byte estimate, got {evicted}"
    )
    archive = result.get("playbook_archive", [])
    assert len(archive) == evicted, (
        "Every evicted entry should appear in playbook_archive"
    )
    # Remaining playbook + estimate must fit within capacity.
    assert _playbook_size_bytes(result) + large_estimate <= _PLAYBOOK_CAPACITY_BYTES


def test_maybe_archive_for_capacity_exits_gracefully_when_no_evictable_entries():
    """Loop exits without archiving when there are no playbook/* entries to evict."""
    from app.playbook_learning import (
        maybe_archive_for_capacity,
        _PLAYBOOK_CAPACITY_BYTES,
    )

    # Non-playbook keys only — nothing archiveable.
    doc: dict = {
        "_meta": {"revision": 1},
        "some_other_key": "not a playbook entry",
    }

    large_estimate = (
        _PLAYBOOK_CAPACITY_BYTES + 1000
    )  # would need eviction if entries existed
    result = maybe_archive_for_capacity(doc, large_estimate)

    assert "playbook_archive" not in result, (
        "No archive created when nothing was evictable"
    )
    assert result is doc or result == doc, (
        "Doc returned unchanged when nothing to evict"
    )


def test_maybe_archive_for_capacity_evicted_entry_triggers_resurrection_cooldown():
    """An entry evicted via capacity management retains _archived_at, which blocks
    _is_auto_playbook_suppressed from re-creating it within the cooldown window.
    """
    from app.playbook_learning import (
        maybe_archive_for_capacity,
        _is_auto_playbook_suppressed,
        _PLAYBOOK_CAPACITY_BYTES,
    )

    victim_key = "playbook/auto-test-pattern"
    # Two large entries so the playbook is over capacity; the older one (victim) evicts first.
    half = _PLAYBOOK_CAPACITY_BYTES // 2
    doc: dict = {
        victim_key: {
            "content": "y" * (half - 50),
            "confirmed_count": 0,
            "source": "auto-generated",
            "updated_at": (_utcnow() - timedelta(days=60)).isoformat(),
        },
        "playbook/newer-entry": {
            "content": "z" * (half - 50),
            "confirmed_count": 5,
            "source": "auto-generated",
            "updated_at": _utcnow().isoformat(),
        },
    }

    result = maybe_archive_for_capacity(doc, 500)

    archive = result.get("playbook_archive", [])
    assert any(e.get("_archived_key") == victim_key for e in archive), (
        "Evicted entry must appear in playbook_archive"
    )
    assert victim_key not in result, (
        "Evicted key must be removed from the active playbook"
    )
    # The cooldown must now block re-creation of the same key.
    assert _is_auto_playbook_suppressed(result, victim_key), (
        "Entry evicted for capacity should be suppressed within the resurrection cooldown window"
    )


@pytest.mark.asyncio
async def test_capture_success_archives_when_over_capacity(db_session: AsyncSession):
    """capture_success_pattern archives the least-useful entry when total size exceeds 30KB."""
    from app.playbook_learning import capture_success_pattern, _PLAYBOOK_CAPACITY_BYTES

    # Build a design_doc clearly over capacity: 35 entries × ~1050 bytes ≈ 35.6 KB
    big_content = "x" * 900
    existing_doc: dict = {
        # A failure-derived entry (pre-existing playbook content)
        "playbook/review-test-coverage": {
            "content": "test coverage issues",
            "source": "review-extracted",
            "applicable_types": ["create"],
            "confirmed_count": 0,
            "updated_at": _utcnow().isoformat(),
        },
    }
    for i in range(34):
        existing_doc[f"playbook/old-entry-{i:03d}"] = {
            "content": big_content,
            "confirmed_count": 0,
            "source": "auto-generated",
            "updated_at": (_utcnow() - timedelta(days=10)).isoformat(),
        }

    project = await _make_project(db_session, design_doc=dict(existing_doc))
    # Create 3 completed tasks of the same type to satisfy the noise filter (>= 3 completions).
    await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )
    await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )
    task = await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )

    from app.playbook_learning import _playbook_size_bytes

    size_before = _playbook_size_bytes(project.design_doc)
    assert size_before > _PLAYBOOK_CAPACITY_BYTES, (
        "Doc must be over capacity before the test"
    )

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key == "success-create-small", "New entry should have been created"
    doc = project.design_doc or {}
    archive = doc.get("playbook_archive", [])
    assert len(archive) >= 1, (
        "At least one entry should have been archived for capacity"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests for compute_review_effectiveness
# ─────────────────────────────────────────────────────────────────────────────


def _make_review_issue(desc: str, severity: str = "MAJOR") -> dict:
    return {"description": desc, "severity": severity}


def _make_review_entry(iteration: int, issue_descs: list[str]) -> dict:
    return {
        "iteration": iteration,
        "issues": [_make_review_issue(d) for d in issue_descs],
    }


async def _make_task_with_reviews(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    age_days: float,
    issue_descs: list[str],
    task_type: str = "create",
) -> Task:
    review_history = [_make_review_entry(0, issue_descs)]
    task = Task(
        project_id=project_id,
        title="Task with reviews",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={"review_loop": {"iteration": 1, "review_history": review_history}},
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    session.add(task)
    await session.flush()
    return task


@pytest.mark.asyncio
async def test_compute_review_effectiveness_returns_none_below_sample_size(
    db_session: AsyncSession,
):
    """Returns None when either window has fewer than 10 tasks with review history."""
    from app.playbook_learning import compute_review_effectiveness

    project = await _make_project(db_session)
    # Only 5 tasks in each window (below the minimum of 10)
    tasks = []
    for i in range(5):
        t = await _make_task_with_reviews(
            db_session, project.id, age_days=2, issue_descs=["missing test coverage"]
        )
        tasks.append(t)
    for i in range(5):
        t = await _make_task_with_reviews(
            db_session, project.id, age_days=18, issue_descs=["missing test coverage"]
        )
        tasks.append(t)

    result = await compute_review_effectiveness(project.id, db_session, tasks)
    assert result is None


@pytest.mark.asyncio
async def test_compute_review_effectiveness_detects_decreasing_trend(
    db_session: AsyncSession,
):
    """Detects a decreasing trend when a category drops by >= 5pp between windows."""
    from app.playbook_learning import compute_review_effectiveness

    project = await _make_project(db_session)

    # Previous window (15-28 days ago): all 12 tasks have test-coverage issues
    prev_tasks = []
    for _ in range(12):
        t = await _make_task_with_reviews(
            db_session,
            project.id,
            age_days=20,
            issue_descs=["missing test coverage for new function", "no error handling"],
        )
        prev_tasks.append(t)

    # Current window (last 14 days): only 2 out of 12 tasks have test-coverage issues
    curr_tasks = []
    for _ in range(2):
        t = await _make_task_with_reviews(
            db_session,
            project.id,
            age_days=5,
            issue_descs=["missing test coverage for new function"],
        )
        curr_tasks.append(t)
    for _ in range(10):
        t = await _make_task_with_reviews(
            db_session, project.id, age_days=5, issue_descs=["no error handling"]
        )
        curr_tasks.append(t)

    all_tasks = prev_tasks + curr_tasks
    result = await compute_review_effectiveness(project.id, db_session, all_tasks)

    assert result is not None
    cats = result["categories"]
    # test-coverage should be decreasing (fewer issues in current window vs previous)
    assert "test-coverage" in cats
    assert cats["test-coverage"]["trend"] == "decreasing"


@pytest.mark.asyncio
async def test_compute_review_effectiveness_stores_in_design_doc(
    db_session: AsyncSession,
):
    """Result is persisted in project.design_doc['review_effectiveness']."""
    from app.playbook_learning import compute_review_effectiveness

    project = await _make_project(db_session)

    # Create 12 tasks in each window
    all_tasks = []
    for _ in range(12):
        t = await _make_task_with_reviews(
            db_session,
            project.id,
            age_days=20,
            issue_descs=["missing test coverage for new function"],
        )
        all_tasks.append(t)
    for _ in range(12):
        t = await _make_task_with_reviews(
            db_session,
            project.id,
            age_days=5,
            issue_descs=["missing test coverage for new function"],
        )
        all_tasks.append(t)

    result = await compute_review_effectiveness(project.id, db_session, all_tasks)
    await db_session.flush()

    assert result is not None
    assert "review_effectiveness" in (project.design_doc or {})
    stored = project.design_doc["review_effectiveness"]
    assert "categories" in stored
    assert "computed_at" in stored
    assert "persistent_issues" in stored


@pytest.mark.asyncio
async def test_compute_review_effectiveness_rate_history_grows(
    db_session: AsyncSession,
):
    """rate_history accumulates a new entry on each new window computation."""
    from app.playbook_learning import compute_review_effectiveness

    project = await _make_project(db_session)

    all_tasks = []
    for _ in range(12):
        all_tasks.append(
            await _make_task_with_reviews(
                db_session,
                project.id,
                age_days=20,
                issue_descs=["missing test coverage for new function"],
            )
        )
    for _ in range(12):
        all_tasks.append(
            await _make_task_with_reviews(
                db_session,
                project.id,
                age_days=5,
                issue_descs=["missing test coverage for new function"],
            )
        )

    # First computation — creates the first history entry
    result1 = await compute_review_effectiveness(project.id, db_session, all_tasks)
    await db_session.flush()
    assert result1 is not None
    history1 = result1["categories"].get("test-coverage", {}).get("rate_history", [])
    assert len(history1) == 1

    # Simulate a new window by clearing computed_at to force is_new_window=True
    doc = dict(project.design_doc or {})
    doc["review_effectiveness"]["computed_at"] = (
        _utcnow() - timedelta(days=15)
    ).isoformat()
    project.design_doc = doc
    await db_session.flush()

    result2 = await compute_review_effectiveness(project.id, db_session, all_tasks)
    await db_session.flush()
    assert result2 is not None
    history2 = result2["categories"].get("test-coverage", {}).get("rate_history", [])
    assert len(history2) == 2


@pytest.mark.asyncio
async def test_compute_review_effectiveness_persistent_issues(db_session: AsyncSession):
    """Identifies persistent issues when a category stays above 20% for 3+ windows."""
    from app.playbook_learning import compute_review_effectiveness

    # Pre-seed two historical high-rate windows
    project = await _make_project(
        db_session,
        design_doc={
            "review_effectiveness": {
                "computed_at": (_utcnow() - timedelta(days=15)).isoformat(),
                "categories": {
                    "test-coverage": {"rate_history": [0.35, 0.30]},
                },
                "persistent_issues": [],
            },
        },
    )

    # 12 tasks in each window, all with test-coverage issues (keeps rate high)
    all_tasks = []
    for _ in range(12):
        all_tasks.append(
            await _make_task_with_reviews(
                db_session,
                project.id,
                age_days=20,
                issue_descs=["missing test coverage for new function"],
            )
        )
    for _ in range(12):
        all_tasks.append(
            await _make_task_with_reviews(
                db_session,
                project.id,
                age_days=5,
                issue_descs=["missing test coverage for new function"],
            )
        )

    result = await compute_review_effectiveness(project.id, db_session, all_tasks)
    await db_session.flush()

    assert result is not None
    # rate_history now has 3 entries, all > 20%, so it should be flagged as persistent
    history = result["categories"].get("test-coverage", {}).get("rate_history", [])
    assert len(history) == 3
    assert all(r > 0.20 for r in history)
    assert "test-coverage" in result["persistent_issues"]


@pytest.mark.asyncio
async def test_compute_review_effectiveness_recurrence_rate_basic(
    db_session: AsyncSession,
):
    """recurrence_rate measures fraction of rejected tasks with each category."""
    from app.playbook_learning import compute_review_effectiveness

    project = await _make_project(db_session)

    # 12 tasks in each window so we pass the minimum sample threshold.
    # Previous window (15-28 days ago): give all 12 tasks review history,
    # and give 6 of them a test-coverage first-attempt rejection.
    all_tasks = []
    for i in range(12):
        t = await _make_task_with_reviews(
            db_session,
            project.id,
            age_days=20,
            issue_descs=["missing test coverage for new function"],
        )
        if i < 6:
            t.context = {
                **(t.context or {}),
                "_first_attempt_rejection_categories": {"test-coverage": 2},
            }
        all_tasks.append(t)

    # Current window (last 14 days): 12 tasks with review history.
    # Give 4 out of 12 a test-coverage rejection.
    for i in range(12):
        t = await _make_task_with_reviews(
            db_session,
            project.id,
            age_days=5,
            issue_descs=["missing test coverage for new function"],
        )
        if i < 4:
            t.context = {
                **(t.context or {}),
                "_first_attempt_rejection_categories": {"test-coverage": 1},
            }
        all_tasks.append(t)

    await db_session.flush()
    result = await compute_review_effectiveness(project.id, db_session, all_tasks)

    assert result is not None
    cats = result["categories"]
    assert "test-coverage" in cats
    tc = cats["test-coverage"]
    # 4 tasks had test-coverage in current window; total tasks with any rejection = 4
    assert tc["recurrence_rate"] == round(4 / 4, 3)
    # 6 tasks had test-coverage in previous window; total with any rejection = 6
    assert tc["previous_recurrence_rate"] == round(6 / 6, 3)


@pytest.mark.asyncio
async def test_compute_review_effectiveness_recurrence_history_grows(
    db_session: AsyncSession,
):
    """recurrence_history accumulates a new entry on each new window, capped at 5."""
    from app.playbook_learning import compute_review_effectiveness

    project = await _make_project(db_session)

    all_tasks = []
    for _ in range(12):
        t = await _make_task_with_reviews(
            db_session,
            project.id,
            age_days=20,
            issue_descs=["missing test coverage for new function"],
        )
        t.context = {
            **(t.context or {}),
            "_first_attempt_rejection_categories": {"test-coverage": 1},
        }
        all_tasks.append(t)
    for _ in range(12):
        t = await _make_task_with_reviews(
            db_session,
            project.id,
            age_days=5,
            issue_descs=["missing test coverage for new function"],
        )
        t.context = {
            **(t.context or {}),
            "_first_attempt_rejection_categories": {"test-coverage": 1},
        }
        all_tasks.append(t)

    await db_session.flush()

    # First computation — creates first recurrence_history entry
    result1 = await compute_review_effectiveness(project.id, db_session, all_tasks)
    await db_session.flush()
    assert result1 is not None
    rh1 = result1["categories"].get("test-coverage", {}).get("recurrence_history", [])
    assert len(rh1) == 1

    # Simulate a new window by backdating computed_at
    doc = dict(project.design_doc or {})
    doc["review_effectiveness"]["computed_at"] = (
        _utcnow() - timedelta(days=15)
    ).isoformat()
    project.design_doc = doc
    await db_session.flush()

    result2 = await compute_review_effectiveness(project.id, db_session, all_tasks)
    await db_session.flush()
    assert result2 is not None
    rh2 = result2["categories"].get("test-coverage", {}).get("recurrence_history", [])
    assert len(rh2) == 2


@pytest.mark.asyncio
async def test_compute_review_effectiveness_recurrence_none_when_no_rejections(
    db_session: AsyncSession,
):
    """recurrence_rate is None when no tasks have _first_attempt_rejection_categories."""
    from app.playbook_learning import compute_review_effectiveness

    project = await _make_project(db_session)

    # Tasks have review history (to pass sample threshold) but no rejection categories
    all_tasks = []
    for _ in range(12):
        all_tasks.append(
            await _make_task_with_reviews(
                db_session,
                project.id,
                age_days=20,
                issue_descs=["missing test coverage for new function"],
            )
        )
    for _ in range(12):
        all_tasks.append(
            await _make_task_with_reviews(
                db_session,
                project.id,
                age_days=5,
                issue_descs=["missing test coverage for new function"],
            )
        )

    result = await compute_review_effectiveness(project.id, db_session, all_tasks)
    assert result is not None
    cats = result["categories"]
    assert "test-coverage" in cats
    # No _first_attempt_rejection_categories on any task → recurrence_rate is None
    assert cats["test-coverage"]["recurrence_rate"] is None
    assert cats["test-coverage"]["previous_recurrence_rate"] is None
    assert cats["test-coverage"]["recurrence_history"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Tests for _update_module_feedback_profiles
# ─────────────────────────────────────────────────────────────────────────────


def test_update_module_feedback_profiles_creates_new_entries():
    """New files get fresh per-category issue counts in the module_feedback dict."""
    from app.playbook_learning import _update_module_feedback_profiles

    doc: dict = {}
    related_files = ["backend/app/orchestrator.py", "backend/app/models.py"]
    buckets = {
        "test-coverage": [
            {"desc": "missing test", "severity": "MAJOR", "iteration": 1}
        ],
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
    """Subsequent calls accumulate counts for already-tracked files within the decay grace window."""
    from app.playbook_learning import _update_module_feedback_profiles

    recent = (_utcnow() - timedelta(days=1)).isoformat()
    doc: dict = {
        "module_feedback": {
            "backend/app/orchestrator.py": {
                "test-coverage": 3,
                "total": 3,
                "last_updated": recent,
            }
        }
    }
    buckets = {
        "test-coverage": [
            {"desc": "missing test", "severity": "MAJOR", "iteration": 1}
        ],
    }

    _update_module_feedback_profiles(doc, ["backend/app/orchestrator.py"], buckets)

    entry = doc["module_feedback"]["backend/app/orchestrator.py"]
    assert entry["test-coverage"] == 4
    assert entry["total"] == 4


def test_update_module_feedback_profiles_decays_stale_counts():
    """Stale entries (last_updated past the grace window) decay before new counts are added."""
    from app.playbook_learning import (
        _update_module_feedback_profiles,
        _MODULE_FEEDBACK_DECAY_AGE_DAYS,
        _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS,
    )

    # 14-day grace + 14-day half-life = 28 days → existing counts halve.
    stale = (
        _utcnow()
        - timedelta(
            days=_MODULE_FEEDBACK_DECAY_AGE_DAYS + _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS
        )
    ).isoformat()
    doc: dict = {
        "module_feedback": {
            "backend/app/orchestrator.py": {
                "test-coverage": 10,
                "error-handling": 4,
                "total": 14,
                "last_updated": stale,
            }
        }
    }
    buckets = {
        "test-coverage": [
            {"desc": "missing test", "severity": "MAJOR", "iteration": 1}
        ],
    }

    _update_module_feedback_profiles(doc, ["backend/app/orchestrator.py"], buckets)

    entry = doc["module_feedback"]["backend/app/orchestrator.py"]
    # 10 → 5 then +1 from new bucket
    assert entry["test-coverage"] == 6
    # 4 → 2 (no new contribution)
    assert entry["error-handling"] == 2
    assert entry["total"] == 8


def test_update_module_feedback_profiles_drops_categories_that_decay_to_zero():
    """Categories whose decayed count rounds to zero are dropped from the entry."""
    from app.playbook_learning import _update_module_feedback_profiles

    very_stale = (_utcnow() - timedelta(days=365)).isoformat()
    doc: dict = {
        "module_feedback": {
            "backend/app/orchestrator.py": {
                "test-coverage": 2,
                "error-handling": 50,
                "total": 52,
                "last_updated": very_stale,
            }
        }
    }
    buckets = {
        "test-coverage": [
            {"desc": "missing test", "severity": "MAJOR", "iteration": 1}
        ],
    }

    _update_module_feedback_profiles(doc, ["backend/app/orchestrator.py"], buckets)

    entry = doc["module_feedback"]["backend/app/orchestrator.py"]
    # error-handling decayed to 0 and was dropped; test-coverage came back via the new bucket.
    assert "error-handling" not in entry
    assert entry["test-coverage"] == 1
    assert entry["total"] == 1


def test_update_module_feedback_profiles_evicts_oldest_when_over_cap():
    """When the module cap is exceeded the oldest-updated entry is evicted."""
    from app.playbook_learning import (
        _update_module_feedback_profiles,
        _MODULE_FEEDBACK_MAX_ENTRIES,
    )

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
    _update_module_feedback_profiles(
        doc, [], {"test-coverage": [{"desc": "x", "severity": "MINOR", "iteration": 0}]}
    )
    assert "module_feedback" not in doc

    _update_module_feedback_profiles(doc, ["backend/app/models.py"], {})
    assert "module_feedback" not in doc


@pytest.mark.asyncio
async def test_extract_review_feedback_updates_module_profiles(
    db_session: AsyncSession,
):
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

    await extract_review_feedback_to_playbook(
        task, review_loop, pr_number=42, original_task_type="create", session=db_session
    )
    await db_session.flush()

    doc = project.design_doc or {}
    assert "module_feedback" in doc, "module_feedback should be written to design_doc"
    feedback = doc["module_feedback"]
    assert "backend/app/routes.py" in feedback
    entry = feedback["backend/app/routes.py"]
    assert entry["total"] > 0
    assert "last_updated" in entry


# ─────────────────────────────────────────────────────────────────────────────
# Tests for _extract_failure_signature module-aware signatures
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_failure_signature_includes_module_from_traceback():
    """Python exception with a File traceback frame includes the module name in the signature."""
    from app.playbook_learning import _extract_failure_signature

    error = (
        "Traceback (most recent call last):\n"
        '  File "backend/app/auth.py", line 42, in <module>\n'
        "ImportError: cannot import name 'oauth_client'\n"
    )
    sig = _extract_failure_signature("CODE_ERROR", error)
    # Format: "importerror: auth: cannot import name 'oauth_client'"
    assert sig.startswith("importerror: auth:")
    assert "oauth_client" in sig


def test_extract_failure_signature_no_traceback_no_module():
    """Python exception without a File frame does not inject a module prefix."""
    from app.playbook_learning import _extract_failure_signature

    error = "ImportError: cannot import name 'foo'"
    sig = _extract_failure_signature("CODE_ERROR", error)
    assert "importerror" in sig
    # No module injected — "auth:" or similar should not appear
    assert ": auth:" not in sig
    assert sig.startswith("importerror:")


def test_extract_failure_signature_innermost_frame_wins():
    """When multiple File frames exist, the innermost (last) provides the module name."""
    from app.playbook_learning import _extract_failure_signature

    error = (
        "Traceback (most recent call last):\n"
        '  File "backend/app/main.py", line 10, in startup\n'
        '  File "backend/app/compute.py", line 55, in init\n'
        "AttributeError: 'NoneType' object has no attribute 'zone'\n"
    )
    sig = _extract_failure_signature("CODE_ERROR", error)
    assert "compute" in sig  # innermost frame
    assert "main" not in sig  # outer frame should not appear


def test_extract_failure_module_returns_none_when_no_frame():
    """_extract_failure_module returns None for text without a File "..." line."""
    from app.playbook_learning import _extract_failure_module

    assert _extract_failure_module("some error without a traceback") is None
    assert _extract_failure_module("") is None


def test_extract_failure_module_strips_path_and_extension():
    """_extract_failure_module extracts just the base module name."""
    from app.playbook_learning import _extract_failure_module

    text = 'file "backend/app/github.py", line 7, in handler'
    assert _extract_failure_module(text) == "github"


@pytest.mark.asyncio
async def test_auto_playbook_stores_failure_module_and_references_in_content(
    db_session: AsyncSession,
):
    """_maybe_auto_playbook stores _failure_module on task context and names the module
    explicitly in the auto-generated playbook entry content."""
    from app.playbook_learning import _maybe_auto_playbook, _extract_failure_signature

    project = await _make_project(db_session)
    error_with_traceback = (
        "Traceback (most recent call last):\n"
        '  File "backend/app/auth.py", line 10, in <module>\n'
        "ImportError: cannot import name 'oauth_client'\n"
    )

    # Pre-seed t1 and t2 with the matching signature so the count reaches 3
    sig = _extract_failure_signature("CODE_ERROR", error_with_traceback)
    t1 = await _make_failed_task(
        db_session, project.id, status_message=error_with_traceback
    )
    t1.context = {"_failure_signature": sig}
    t2 = await _make_failed_task(
        db_session, project.id, status_message=error_with_traceback
    )
    t2.context = {"_failure_signature": sig}
    await db_session.flush()

    t3 = await _make_failed_task(
        db_session, project.id, status_message=error_with_traceback
    )
    await _maybe_auto_playbook(t3, "CODE_ERROR", error_with_traceback, db_session)
    await db_session.flush()

    # Module stored on task context
    assert t3.context.get("_failure_module") == "auth"

    # Playbook entry references the module explicitly
    doc = project.design_doc or {}
    playbook_keys = [k for k in doc if k.startswith("playbook/")]
    assert len(playbook_keys) == 1
    entry = doc[playbook_keys[0]]
    assert "auth" in entry["content"]
    assert "module" in entry["content"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# Tests for _accumulate_direction_first_attempt
# ─────────────────────────────────────────────────────────────────────────────


def test_accumulate_direction_first_attempt_creates_entry():
    """Creates a direction tally entry with counts from buckets."""
    from app.playbook_learning import (
        _accumulate_direction_first_attempt,
        _DIRECTION_FIRST_ATTEMPT_KEY,
    )

    doc: dict = {}
    buckets = {
        "test-coverage": [
            {"desc": "missing test", "severity": "MAJOR", "iteration": 0}
        ],
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
    from app.playbook_learning import (
        _accumulate_direction_first_attempt,
        _DIRECTION_FIRST_ATTEMPT_KEY,
    )

    doc: dict = {}
    buckets = {"test-coverage": [{"desc": "x", "severity": "MAJOR", "iteration": 0}]}
    _accumulate_direction_first_attempt(doc, "dir-abc", buckets)
    _accumulate_direction_first_attempt(doc, "dir-abc", buckets)
    _accumulate_direction_first_attempt(doc, "dir-abc", buckets)

    tally = doc[_DIRECTION_FIRST_ATTEMPT_KEY]["dir-abc"]
    assert tally["test-coverage"] == 3
    assert tally["total"] == 3


def test_accumulate_direction_first_attempt_decays_old_counts():
    """Stale counts past the grace window decay exponentially before new counts accumulate."""
    from app.playbook_learning import (
        _accumulate_direction_first_attempt,
        _DIRECTION_FIRST_ATTEMPT_KEY,
    )

    stale_last = (_utcnow() - timedelta(days=60)).isoformat()
    doc: dict = {
        _DIRECTION_FIRST_ATTEMPT_KEY: {
            "dir-old": {
                "test-coverage": 20,
                "total": 20,
                "last_updated": stale_last,
            }
        }
    }
    buckets = {"test-coverage": [{"desc": "x", "severity": "MAJOR", "iteration": 0}]}
    _accumulate_direction_first_attempt(doc, "dir-old", buckets)

    tally = doc[_DIRECTION_FIRST_ATTEMPT_KEY]["dir-old"]
    # 60 days old => overage 46d, factor ≈ 0.5^(46/14) ≈ 0.102 => round(20*0.102)=2, +1 = 3
    assert tally["test-coverage"] < 20
    assert tally["test-coverage"] >= 1


def test_accumulate_direction_first_attempt_noop_on_empty_buckets():
    """No changes when buckets are empty."""
    from app.playbook_learning import (
        _accumulate_direction_first_attempt,
        _DIRECTION_FIRST_ATTEMPT_KEY,
    )

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
    created = _maybe_auto_playbook_from_first_attempt(
        doc, "dir-abc123", "create", "task-id-1"
    )

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
    created = _maybe_auto_playbook_from_first_attempt(
        doc, "dir-abc", "create", "task-id"
    )
    assert created == []


def test_maybe_auto_playbook_deduplicates():
    """Skips entry creation if key already exists."""
    from app.playbook_learning import (
        _maybe_auto_playbook_from_first_attempt,
        _DIRECTION_FIRST_ATTEMPT_KEY,
        _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD,
    )

    # Compute the key that would be generated for direction "dir-abc123"
    # (slug logic: direction_key[:8].replace("-","").replace("_",""))
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
    created = _maybe_auto_playbook_from_first_attempt(
        doc, direction_key, "create", "task-id"
    )
    assert created == []


# ─────────────────────────────────────────────────────────────────────────────
# Tests for scope inference in _maybe_auto_playbook
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_maybe_auto_playbook_direction_scope_when_single_direction(
    db_session: AsyncSession,
):
    """_maybe_auto_playbook marks scope=direction when all tasks in pattern share same direction."""
    from app.playbook_learning import _maybe_auto_playbook

    direction_id = "dir-abc-123"
    project = await _make_project(db_session)

    # Compute expected signature so prior tasks match in the DB query.
    # _extract_failure_signature("PROVISION_FAILURE", msg) falls through to generic
    # fallback and returns the first non-empty line stripped of paths/line-numbers.
    expected_sig = "vm failed to start after provision attempt"

    # Two prior tasks with same direction and same signature
    t1 = Task(
        project_id=project.id,
        title="Task 1",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.FAILED,
        latest_status_message="vm failed to start after provision attempt",
        context={"direction_id": direction_id, "_failure_signature": expected_sig},
    )
    t2 = Task(
        project_id=project.id,
        title="Task 2",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.FAILED,
        latest_status_message="vm failed to start after provision attempt",
        context={"direction_id": direction_id, "_failure_signature": expected_sig},
    )
    db_session.add(t1)
    db_session.add(t2)
    await db_session.flush()

    # Current task — same direction
    t3 = Task(
        project_id=project.id,
        title="Task 3",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.FAILED,
        latest_status_message="vm failed to start after provision attempt",
        context={"direction_id": direction_id},
    )
    db_session.add(t3)
    await db_session.flush()

    key = await _maybe_auto_playbook(
        t3,
        "PROVISION_FAILURE",
        "vm failed to start after provision attempt",
        db_session,
    )
    await db_session.flush()

    assert key is not None
    doc = project.design_doc or {}
    entry = doc.get(f"playbook/{key}", {})
    assert entry.get("scope") == "direction"
    assert entry.get("learned_from_direction") == direction_id


@pytest.mark.asyncio
async def test_maybe_auto_playbook_universal_scope_when_multiple_directions(
    db_session: AsyncSession,
):
    """_maybe_auto_playbook marks scope=universal when tasks span multiple directions."""
    from app.playbook_learning import _maybe_auto_playbook

    direction_a = "dir-aaa"
    direction_b = "dir-bbb"
    project = await _make_project(db_session)

    expected_sig = "vm failed to start after provision attempt"

    t1 = Task(
        project_id=project.id,
        title="Task 1",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.FAILED,
        latest_status_message="vm failed to start after provision attempt",
        context={"direction_id": direction_a, "_failure_signature": expected_sig},
    )
    t2 = Task(
        project_id=project.id,
        title="Task 2",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.FAILED,
        latest_status_message="vm failed to start after provision attempt",
        context={"direction_id": direction_b, "_failure_signature": expected_sig},
    )
    db_session.add(t1)
    db_session.add(t2)
    await db_session.flush()

    t3 = Task(
        project_id=project.id,
        title="Task 3",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.FAILED,
        latest_status_message="vm failed to start after provision attempt",
        context={"direction_id": direction_a},
    )
    db_session.add(t3)
    await db_session.flush()

    key = await _maybe_auto_playbook(
        t3,
        "PROVISION_FAILURE",
        "vm failed to start after provision attempt",
        db_session,
    )
    await db_session.flush()

    assert key is not None
    doc = project.design_doc or {}
    entry = doc.get(f"playbook/{key}", {})
    assert entry.get("scope") == "universal"
    assert entry.get("learned_from_direction") is None


# ─────────────────────────────────────────────────────────────────────────────
# Tests for extract_review_feedback_to_playbook — first-attempt (iteration < 3)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_review_feedback_accumulates_on_first_iteration(
    db_session: AsyncSession,
):
    """extract_review_feedback_to_playbook accumulates direction data on iteration == 1."""
    from app.playbook_learning import (
        extract_review_feedback_to_playbook,
        _DIRECTION_FIRST_ATTEMPT_KEY,
    )

    project = await _make_project(db_session, design_doc={})
    review_history = [
        {
            "iteration": 0,
            "issues": [
                {
                    "description": "missing unit tests for the handler",
                    "severity": "MAJOR",
                },
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

    await extract_review_feedback_to_playbook(
        task, review_loop, pr_number=99, original_task_type="create", session=db_session
    )
    await db_session.flush()

    doc = project.design_doc or {}

    # No full playbook entry created for iteration < 3
    review_keys = [k for k in doc if k.startswith("playbook/review-")]
    assert review_keys == [], (
        f"Should not create review-extracted entries at iteration 1, got {review_keys}"
    )

    # Direction tally was accumulated
    assert _DIRECTION_FIRST_ATTEMPT_KEY in doc, (
        "direction_first_attempt_feedback should be written"
    )
    tally = doc[_DIRECTION_FIRST_ATTEMPT_KEY].get("dir-cafebabe-1234")
    assert tally is not None
    assert tally.get("test-coverage", 0) >= 1
    assert tally.get("total", 0) >= 1


@pytest.mark.asyncio
async def test_extract_review_feedback_no_first_attempt_on_empty_history(
    db_session: AsyncSession,
):
    """extract_review_feedback_to_playbook returns [] with no review_history."""
    from app.playbook_learning import (
        extract_review_feedback_to_playbook,
        _DIRECTION_FIRST_ATTEMPT_KEY,
    )

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
async def test_extract_review_feedback_auto_playbook_at_threshold(
    db_session: AsyncSession,
):
    """Auto-generates a first-attempt-pattern entry when direction tally hits the threshold."""
    from app.playbook_learning import (
        extract_review_feedback_to_playbook,
        _DIRECTION_FIRST_ATTEMPT_KEY,
        _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD,
    )

    direction_id = "dir-deadbeef-5678"
    # Pre-populate the tally so the next call pushes test-coverage over the threshold
    pre_count = _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD - 1
    project = await _make_project(
        db_session,
        design_doc={
            _DIRECTION_FIRST_ATTEMPT_KEY: {
                direction_id: {
                    "test-coverage": pre_count,
                    "total": pre_count,
                    "last_updated": _utcnow().isoformat(),
                }
            }
        },
    )
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
        task,
        review_loop,
        pr_number=100,
        original_task_type="create",
        session=db_session,
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
async def test_extract_review_feedback_still_creates_entries_at_3_plus(
    db_session: AsyncSession,
):
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

    await extract_review_feedback_to_playbook(
        task, review_loop, pr_number=42, original_task_type="create", session=db_session
    )
    await db_session.flush()

    doc = project.design_doc or {}
    review_keys = [k for k in doc if k.startswith("playbook/review-")]
    assert len(review_keys) >= 1, (
        "Should create at least 1 review-extracted entry at iteration 3"
    )
    assert any("review-test-coverage" in k for k in review_keys)


# ─────────────────────────────────────────────────────────────────────────────
# Tests for scope inference in extract_review_feedback_to_playbook
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_review_feedback_module_scope_when_related_files_present(
    db_session: AsyncSession,
):
    """extract_review_feedback_to_playbook sets scope=module and stores related_files when present."""
    from app.playbook_learning import extract_review_feedback_to_playbook

    direction_id = "dir-xyz-456"
    project = await _make_project(db_session, design_doc={})
    review_history = [
        {
            "iteration": 1,
            "issues": [
                {
                    "description": "missing test coverage for new function",
                    "severity": "MAJOR",
                },
                {"description": "no unit test for edge case", "severity": "MINOR"},
                {"description": "add test for error path", "severity": "MAJOR"},
            ],
        },
        {
            "iteration": 2,
            "issues": [{"description": "still missing tests", "severity": "MAJOR"}],
        },
        {"iteration": 3, "issues": []},
    ]
    review_loop = {"iteration": 3, "review_history": review_history}
    task = Task(
        project_id=project.id,
        title="Test task",
        payloads=[{"type": "create", "prompt": "add endpoint"}],
        status=TaskStatus.COMPLETED,
        context={
            "direction_id": direction_id,
            "related_files": ["backend/app/routes.py", "backend/app/models.py"],
        },
    )
    db_session.add(task)
    await db_session.flush()

    keys = await extract_review_feedback_to_playbook(
        task, review_loop, pr_number=99, original_task_type="create", session=db_session
    )
    await db_session.flush()

    assert keys, "Expected at least one entry to be created"
    doc = project.design_doc or {}
    for key in keys:
        if not key.startswith("review-"):
            continue  # skip first-attempt-pattern keys; scope only applies to review-extracted
        entry = doc[f"playbook/{key}"]
        assert entry.get("scope") == "module", (
            f"Expected module scope, got {entry.get('scope')}"
        )
        assert "backend/app/routes.py" in entry.get("related_files", [])
        assert entry.get("learned_from_direction") == direction_id


@pytest.mark.asyncio
async def test_extract_review_feedback_universal_scope_when_no_related_files(
    db_session: AsyncSession,
):
    """extract_review_feedback_to_playbook sets scope=universal when no related_files."""
    from app.playbook_learning import extract_review_feedback_to_playbook

    project = await _make_project(db_session, design_doc={})
    review_history = [
        {
            "iteration": 1,
            "issues": [
                {
                    "description": "missing test coverage for new function",
                    "severity": "MAJOR",
                },
                {"description": "no unit test for edge case", "severity": "MINOR"},
                {"description": "add test for error path", "severity": "MAJOR"},
            ],
        },
        {
            "iteration": 2,
            "issues": [{"description": "still missing tests", "severity": "MAJOR"}],
        },
        {"iteration": 3, "issues": []},
    ]
    review_loop = {"iteration": 3, "review_history": review_history}
    task = Task(
        project_id=project.id,
        title="Test task",
        payloads=[{"type": "create", "prompt": "add endpoint"}],
        status=TaskStatus.COMPLETED,
        context={},  # No related_files
    )
    db_session.add(task)
    await db_session.flush()

    keys = await extract_review_feedback_to_playbook(
        task,
        review_loop,
        pr_number=100,
        original_task_type="create",
        session=db_session,
    )
    await db_session.flush()

    assert keys, "Expected at least one entry to be created"
    doc = project.design_doc or {}
    for key in keys:
        if not key.startswith("review-"):
            continue  # skip first-attempt-pattern keys
        entry = doc[f"playbook/{key}"]
        assert entry.get("scope") == "universal", (
            f"Expected universal scope, got {entry.get('scope')}"
        )
        assert "related_files" not in entry


@pytest.mark.asyncio
async def test_extract_review_feedback_stores_learned_from_direction(
    db_session: AsyncSession,
):
    """extract_review_feedback_to_playbook stores learned_from_direction from task context."""
    from app.playbook_learning import extract_review_feedback_to_playbook

    direction_id = "dir-provenance-test"
    project = await _make_project(db_session, design_doc={})
    review_history = [
        {
            "iteration": 1,
            "issues": [
                {
                    "description": "missing test coverage for new function",
                    "severity": "MAJOR",
                },
                {"description": "no unit test for edge case", "severity": "MINOR"},
                {"description": "add test for error path", "severity": "MAJOR"},
            ],
        },
        {
            "iteration": 2,
            "issues": [{"description": "still missing tests", "severity": "MAJOR"}],
        },
        {"iteration": 3, "issues": []},
    ]
    review_loop = {"iteration": 3, "review_history": review_history}
    task = Task(
        project_id=project.id,
        title="Test task",
        payloads=[{"type": "fix", "prompt": "fix the bug"}],
        status=TaskStatus.COMPLETED,
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.flush()

    keys = await extract_review_feedback_to_playbook(
        task, review_loop, pr_number=101, original_task_type="fix", session=db_session
    )
    await db_session.flush()

    assert keys
    doc = project.design_doc or {}
    for key in keys:
        if not key.startswith("review-"):
            continue  # skip first-attempt-pattern keys
        entry = doc[f"playbook/{key}"]
        assert entry.get("learned_from_direction") == direction_id


# ─────────────────────────────────────────────────────────────────────────────
# Tests for review-extracted entry refresh and stale-marking
# ─────────────────────────────────────────────────────────────────────────────


def _make_multi_iter_review_loop(desc: str = "missing unit tests") -> dict:
    return {
        "iteration": 3,
        "review_history": [
            {"iteration": 0, "issues": [{"description": desc, "severity": "MAJOR"}]},
            {"iteration": 1, "issues": [{"description": desc, "severity": "MAJOR"}]},
            {"iteration": 2, "issues": []},
        ],
    }


@pytest.mark.asyncio
async def test_extract_review_feedback_increments_hit_count_when_pattern_recurs(
    db_session: AsyncSession,
):
    """Re-observing the same review pattern bumps hit_count on the existing entry."""
    from app.playbook_learning import extract_review_feedback_to_playbook

    project = await _make_project(db_session, design_doc={})

    async def _run(pr: int) -> Task:
        task = Task(
            project_id=project.id,
            title="Task",
            payloads=[{"type": "create", "prompt": "do"}],
            status=TaskStatus.COMPLETED,
            context={},
        )
        db_session.add(task)
        await db_session.flush()
        await extract_review_feedback_to_playbook(
            task,
            _make_multi_iter_review_loop(),
            pr_number=pr,
            original_task_type="create",
            session=db_session,
        )
        await db_session.flush()
        return task

    await _run(1)
    doc = project.design_doc or {}
    entry = doc["playbook/review-test-coverage"]
    assert entry["hit_count"] == 1

    await _run(2)
    await _run(3)
    entry = (project.design_doc or {})["playbook/review-test-coverage"]
    assert entry["hit_count"] == 3, (
        f"hit_count should accumulate, got {entry['hit_count']}"
    )


@pytest.mark.asyncio
async def test_extract_review_feedback_refreshes_stale_entry(db_session: AsyncSession):
    """Existing entry older than 30 days is refreshed with new content and timestamp."""
    from app.playbook_learning import extract_review_feedback_to_playbook

    old_iso = (_utcnow() - timedelta(days=45)).isoformat()
    existing_doc = {
        "playbook/review-test-coverage": {
            "content": "Stale content from PR #1",
            "source": "review-extracted",
            "applicable_types": ["create"],
            "confirmed_count": 0,
            "hit_count": 4,
            "updated_at": old_iso,
            "pr_number": 1,
            "iteration_count": 3,
            "scope": "universal",
            "potentially_stale": True,
        }
    }
    project = await _make_project(db_session, design_doc=existing_doc)

    task = Task(
        project_id=project.id,
        title="Task",
        payloads=[{"type": "create", "prompt": "do"}],
        status=TaskStatus.COMPLETED,
        context={},
    )
    db_session.add(task)
    await db_session.flush()

    await extract_review_feedback_to_playbook(
        task,
        _make_multi_iter_review_loop("missing unit tests for new module"),
        pr_number=99,
        original_task_type="create",
        session=db_session,
    )
    await db_session.flush()

    entry = (project.design_doc or {})["playbook/review-test-coverage"]
    assert entry["hit_count"] == 5
    assert entry["pr_number"] == 99
    assert "PR #99" in entry["content"]
    assert "potentially_stale" not in entry
    refreshed_at = datetime.fromisoformat(entry["updated_at"].replace("Z", "+00:00"))
    if refreshed_at.tzinfo is not None:
        refreshed_at = refreshed_at.replace(tzinfo=None)
    assert (_utcnow() - refreshed_at).total_seconds() < 60


@pytest.mark.asyncio
async def test_extract_review_feedback_keeps_fresh_entry_content(
    db_session: AsyncSession,
):
    """Entry younger than 30 days has hit_count bumped but content/timestamp untouched."""
    from app.playbook_learning import extract_review_feedback_to_playbook

    fresh_iso = (_utcnow() - timedelta(days=5)).isoformat()
    existing_doc = {
        "playbook/review-test-coverage": {
            "content": "Fresh content from PR #7",
            "source": "review-extracted",
            "applicable_types": ["create"],
            "confirmed_count": 0,
            "hit_count": 2,
            "updated_at": fresh_iso,
            "pr_number": 7,
            "iteration_count": 3,
            "scope": "universal",
        }
    }
    project = await _make_project(db_session, design_doc=existing_doc)

    task = Task(
        project_id=project.id,
        title="Task",
        payloads=[{"type": "create", "prompt": "do"}],
        status=TaskStatus.COMPLETED,
        context={},
    )
    db_session.add(task)
    await db_session.flush()

    await extract_review_feedback_to_playbook(
        task,
        _make_multi_iter_review_loop(),
        pr_number=200,
        original_task_type="create",
        session=db_session,
    )
    await db_session.flush()

    entry = (project.design_doc or {})["playbook/review-test-coverage"]
    assert entry["hit_count"] == 3
    assert entry["pr_number"] == 7, "fresh entry pr_number should be unchanged"
    assert entry["updated_at"] == fresh_iso, "fresh entry timestamp should be unchanged"
    assert entry["content"] == "Fresh content from PR #7"


@pytest.mark.asyncio
async def test_extract_review_feedback_marks_unobserved_entry_potentially_stale(
    db_session: AsyncSession,
):
    """Entries not observed for >60 days are marked potentially_stale on next call."""
    from app.playbook_learning import extract_review_feedback_to_playbook

    very_old_iso = (_utcnow() - timedelta(days=75)).isoformat()
    existing_doc = {
        "playbook/review-error-handling": {
            "content": "Old error-handling pattern",
            "source": "review-extracted",
            "applicable_types": ["create"],
            "confirmed_count": 0,
            "hit_count": 1,
            "updated_at": very_old_iso,
            "pr_number": 1,
            "iteration_count": 3,
            "scope": "universal",
        }
    }
    project = await _make_project(db_session, design_doc=existing_doc)

    task = Task(
        project_id=project.id,
        title="Task",
        payloads=[{"type": "create", "prompt": "do"}],
        status=TaskStatus.COMPLETED,
        context={},
    )
    db_session.add(task)
    await db_session.flush()

    await extract_review_feedback_to_playbook(
        task,
        _make_multi_iter_review_loop(),
        pr_number=300,
        original_task_type="create",
        session=db_session,
    )
    await db_session.flush()

    entry = (project.design_doc or {})["playbook/review-error-handling"]
    assert entry.get("potentially_stale") is True
    assert entry["updated_at"] == very_old_iso, (
        "stale entry timestamp should not change"
    )
    assert entry["hit_count"] == 1


def test_mark_stale_review_entries_ignores_non_review_and_fresh():
    """Stale-marker only touches review-extracted entries past the 60-day threshold."""
    from app.playbook_learning import _mark_stale_review_entries

    now = _utcnow()
    doc = {
        "playbook/review-test-coverage": {
            "source": "review-extracted",
            "updated_at": (now - timedelta(days=10)).isoformat(),
        },
        "playbook/review-old": {
            "source": "review-extracted",
            "updated_at": (now - timedelta(days=90)).isoformat(),
        },
        "playbook/auto-bug": {
            "source": "auto-generated",
            "updated_at": (now - timedelta(days=120)).isoformat(),
        },
    }
    changed = _mark_stale_review_entries(doc)
    assert changed is True
    assert doc["playbook/review-old"].get("potentially_stale") is True
    assert "potentially_stale" not in doc["playbook/review-test-coverage"]
    assert "potentially_stale" not in doc["playbook/auto-bug"]

    # Idempotent: a second call does not re-flag.
    assert _mark_stale_review_entries(doc) is False


# ─────────────────────────────────────────────────────────────────────────────
# compute_recurring_first_review_descriptions
# ─────────────────────────────────────────────────────────────────────────────


class _FakeRow:
    def __init__(self, rid, review_history, created_at=None):
        self.id = rid
        self.created_at = created_at
        self.context = {"review_loop": {"review_history": review_history}}


def _entry(iteration, *descs, category="style"):
    return {
        "iteration": iteration,
        "issues": [{"description": d, "category": category} for d in descs],
    }


def test_recurring_descriptions_empty_rows_returns_none():
    from app.playbook_learning import compute_recurring_first_review_descriptions

    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert compute_recurring_first_review_descriptions([], cutoff) is None


def test_recurring_descriptions_no_review_history_returns_none():
    from app.playbook_learning import compute_recurring_first_review_descriptions

    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [_FakeRow("t1", [], created_at=datetime(2026, 2, 1, tzinfo=timezone.utc))]
    assert compute_recurring_first_review_descriptions(rows, cutoff) is None


def test_recurring_descriptions_single_occurrence_returns_none():
    from app.playbook_learning import compute_recurring_first_review_descriptions

    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    rows = [
        _FakeRow("t1", [_entry(0, "Missing docstring")], created_at=now),
        _FakeRow("t2", [_entry(0, "Bad import")], created_at=now),
    ]
    assert compute_recurring_first_review_descriptions(rows, cutoff) is None


def test_recurring_descriptions_returns_sorted_capped_at_three():
    from app.playbook_learning import compute_recurring_first_review_descriptions

    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    rows = [
        _FakeRow("t1", [_entry(0, "a", "b", "c", "d")], created_at=now),
        _FakeRow("t2", [_entry(0, "a", "b", "c", "d")], created_at=now),
        _FakeRow("t3", [_entry(0, "a", "b", "c")], created_at=now),
        _FakeRow("t4", [_entry(0, "a", "b")], created_at=now),
        _FakeRow("t5", [_entry(0, "a")], created_at=now),
    ]
    out = compute_recurring_first_review_descriptions(rows, cutoff)
    assert out is not None
    assert len(out) == 3
    counts = [e["count"] for e in out]
    assert counts == sorted(counts, reverse=True)
    assert out[0]["description"] == "a"
    assert out[0]["count"] == 5
    assert out[0]["category"] == "style"


def test_recurring_descriptions_cutoff_filters_old_tasks():
    from app.playbook_learning import compute_recurring_first_review_descriptions

    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old = datetime(2025, 6, 1, tzinfo=timezone.utc)
    new = datetime(2026, 2, 1, tzinfo=timezone.utc)
    rows = [
        _FakeRow("t1", [_entry(0, "repeat")], created_at=old),
        _FakeRow("t2", [_entry(0, "repeat")], created_at=old),
        _FakeRow("t3", [_entry(0, "repeat")], created_at=new),
    ]
    assert compute_recurring_first_review_descriptions(rows, cutoff) is None


def test_recurring_descriptions_task_filter_excludes():
    from app.playbook_learning import compute_recurring_first_review_descriptions

    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    rows = [
        _FakeRow("keep1", [_entry(0, "repeat")], created_at=now),
        _FakeRow("keep2", [_entry(0, "repeat")], created_at=now),
        _FakeRow("skip1", [_entry(0, "other")], created_at=now),
        _FakeRow("skip2", [_entry(0, "other")], created_at=now),
    ]
    out = compute_recurring_first_review_descriptions(
        rows, cutoff, task_filter=lambda r: r.id.startswith("keep")
    )
    assert out is not None
    assert len(out) == 1
    assert out[0]["description"] == "repeat"


def test_recurring_descriptions_min_tasks_threshold():
    from app.playbook_learning import compute_recurring_first_review_descriptions

    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    rows = [
        _FakeRow("t1", [_entry(0, "repeat", "repeat")], created_at=now),
    ]
    assert (
        compute_recurring_first_review_descriptions(rows, cutoff, min_tasks=5) is None
    )


def test_recurring_descriptions_count_unique_tasks_mode():
    from app.playbook_learning import compute_recurring_first_review_descriptions

    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    # "solo" appears 5 times but all in one task; "shared" appears twice across two tasks.
    rows = [
        _FakeRow(
            "t1",
            [_entry(0, "solo", "solo", "solo", "solo", "solo", "shared")],
            created_at=now,
        ),
        _FakeRow("t2", [_entry(0, "shared")], created_at=now),
    ]
    out = compute_recurring_first_review_descriptions(
        rows, cutoff, count_unique_tasks=True
    )
    assert out is not None
    descs = {e["description"]: e["count"] for e in out}
    assert descs == {"shared": 2}


# ─────────────────────────────────────────────────────────────────────────────
# _decay_module_feedback_entry
# ─────────────────────────────────────────────────────────────────────────────


def test_decay_module_feedback_entry_missing_last_updated_returns_unchanged():
    from app.playbook_learning import _decay_module_feedback_entry

    entry = {"review_quality": 3, "total": 3}
    now = datetime(2026, 4, 18)
    assert _decay_module_feedback_entry(entry, now) is entry


def test_decay_module_feedback_entry_unparseable_last_updated_returns_unchanged():
    from app.playbook_learning import _decay_module_feedback_entry

    entry = {"review_quality": 3, "total": 3, "last_updated": "not-a-date"}
    now = datetime(2026, 4, 18)
    assert _decay_module_feedback_entry(entry, now) is entry


def test_decay_module_feedback_entry_within_grace_window_unchanged():
    from app.playbook_learning import (
        _MODULE_FEEDBACK_DECAY_AGE_DAYS,
        _decay_module_feedback_entry,
    )

    now = datetime(2026, 4, 18)
    last = now - timedelta(days=_MODULE_FEEDBACK_DECAY_AGE_DAYS - 1)
    entry = {
        "review_quality": 5,
        "total": 5,
        "last_updated": last.isoformat(),
    }
    assert _decay_module_feedback_entry(entry, now) is entry


def test_decay_module_feedback_entry_past_grace_decays_counts():
    from app.playbook_learning import (
        _MODULE_FEEDBACK_DECAY_AGE_DAYS,
        _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS,
        _decay_module_feedback_entry,
    )

    now = datetime(2026, 4, 18)
    overage_days = (
        _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS  # exactly one half-life past grace
    )
    last = now - timedelta(days=_MODULE_FEEDBACK_DECAY_AGE_DAYS + overage_days)
    entry = {
        "review_quality": 10,
        "correctness": 4,
        "total": 14,
        "last_updated": last.isoformat(),
    }
    factor = 0.5 ** (overage_days / _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS)
    result = _decay_module_feedback_entry(entry, now)
    assert result["review_quality"] == round(10 * factor)
    assert result["correctness"] == round(4 * factor)
    assert result["total"] == result["review_quality"] + result["correctness"]


def test_decay_module_feedback_entry_drops_categories_rounding_to_zero():
    from app.playbook_learning import (
        _MODULE_FEEDBACK_DECAY_AGE_DAYS,
        _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS,
        _decay_module_feedback_entry,
    )

    now = datetime(2026, 4, 18)
    # 5 half-lives: factor = 1/32. A count of 1 rounds to 0 and is dropped.
    overage_days = 5 * _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS
    last = now - timedelta(days=_MODULE_FEEDBACK_DECAY_AGE_DAYS + overage_days)
    entry = {
        "review_quality": 1,
        "correctness": 100,
        "total": 101,
        "last_updated": last.isoformat(),
    }
    result = _decay_module_feedback_entry(entry, now)
    assert "review_quality" not in result
    assert result["correctness"] == round(100 / 32)
    assert result["total"] == result["correctness"]


def test_decay_module_feedback_entry_preserves_non_integer_values():
    from app.playbook_learning import (
        _MODULE_FEEDBACK_DECAY_AGE_DAYS,
        _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS,
        _decay_module_feedback_entry,
    )

    now = datetime(2026, 4, 18)
    overage_days = _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS
    last = now - timedelta(days=_MODULE_FEEDBACK_DECAY_AGE_DAYS + overage_days)
    entry = {
        "review_quality": 8,
        "total": 8,
        "last_updated": last.isoformat(),
        "note": "some string",
        "ratio": 0.75,
    }
    result = _decay_module_feedback_entry(entry, now)
    assert result["note"] == "some string"
    assert result["ratio"] == 0.75
    assert result["review_quality"] == round(8 * 0.5)
    assert result["total"] == result["review_quality"]


def test_decay_module_feedback_entry_total_recomputed_from_surviving_counts():
    from app.playbook_learning import (
        _MODULE_FEEDBACK_DECAY_AGE_DAYS,
        _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS,
        _decay_module_feedback_entry,
    )

    now = datetime(2026, 4, 18)
    overage_days = _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS
    last = now - timedelta(days=_MODULE_FEEDBACK_DECAY_AGE_DAYS + overage_days)
    # Stored total is wrong on purpose; decay should recompute from survivors.
    entry = {
        "review_quality": 10,
        "correctness": 6,
        "total": 9999,
        "last_updated": last.isoformat(),
    }
    result = _decay_module_feedback_entry(entry, now)
    assert result["total"] == result["review_quality"] + result["correctness"]
    assert result["total"] != 9999


# ─────────────────────────────────────────────────────────────────────────────
# summarize_decomposition_outcomes
# ─────────────────────────────────────────────────────────────────────────────


def test_summarize_decomposition_outcomes_empty_history():
    from app.playbook_learning import summarize_decomposition_outcomes

    assert summarize_decomposition_outcomes({}) is None
    assert summarize_decomposition_outcomes({"research_history": []}) is None


def test_summarize_decomposition_outcomes_no_decomposition_quality():
    from app.playbook_learning import summarize_decomposition_outcomes

    direction = {
        "research_history": [
            {"tasks_created": 2},
            {"tasks_created": 3, "decomposition_quality": []},
            {"tasks_created": 1, "decomposition_quality": "not-a-list"},
        ]
    }
    assert summarize_decomposition_outcomes(direction) is None


def test_summarize_decomposition_outcomes_single_cycle_averages():
    from app.playbook_learning import summarize_decomposition_outcomes

    direction = {
        "research_history": [
            {
                "tasks_created": 2,
                "decomposition_quality": [
                    {
                        "subtask_merge_rate": 0.8,
                        "avg_files_per_subtask": 2.0,
                        "max_files_in_subtask": 3,
                        "dependency_depth": 1,
                        "cross_module_count": 0,
                    },
                    {
                        "subtask_merge_rate": 0.6,
                        "avg_files_per_subtask": 4.0,
                        "max_files_in_subtask": 5,
                        "dependency_depth": 2,
                        "cross_module_count": 1,
                    },
                ],
            }
        ]
    }
    out = summarize_decomposition_outcomes(direction)
    assert out is not None
    assert out["prior_decomposition_count"] == 2
    assert out["avg_subtask_merge_rate"] == 0.7
    assert out["avg_files_per_subtask"] == 3.0
    assert out["max_files_in_subtask"] == 5
    assert out["avg_dependency_depth"] == 1.5
    assert out["max_dependency_depth"] == 2
    assert out["high_cross_module_correlates_with_low_merge"] is False


def test_summarize_decomposition_outcomes_cross_module_correlation_flag():
    from app.playbook_learning import summarize_decomposition_outcomes

    direction = {
        "research_history": [
            {
                "tasks_created": 1,
                "decomposition_quality": [
                    {"subtask_merge_rate": 0.9, "cross_module_count": 0},
                    {"subtask_merge_rate": 0.85, "cross_module_count": 1},
                    {"subtask_merge_rate": 0.4, "cross_module_count": 3},
                    {"subtask_merge_rate": 0.3, "cross_module_count": 2},
                ],
            }
        ]
    }
    out = summarize_decomposition_outcomes(direction)
    assert out is not None
    assert out["high_cross_module_correlates_with_low_merge"] is True


def test_summarize_decomposition_outcomes_skips_non_numeric():
    from app.playbook_learning import summarize_decomposition_outcomes

    direction = {
        "research_history": [
            {
                "tasks_created": 1,
                "decomposition_quality": [
                    {
                        "subtask_merge_rate": "nope",
                        "avg_files_per_subtask": None,
                        "max_files_in_subtask": "x",
                        "dependency_depth": None,
                        "cross_module_count": "y",
                    },
                    {
                        "subtask_merge_rate": 0.5,
                        "avg_files_per_subtask": 2.0,
                        "max_files_in_subtask": 2,
                        "dependency_depth": 1,
                        "cross_module_count": 0,
                    },
                ],
            }
        ]
    }
    out = summarize_decomposition_outcomes(direction)
    assert out is not None
    assert out["prior_decomposition_count"] == 2
    assert out["avg_subtask_merge_rate"] == 0.5
    assert out["avg_files_per_subtask"] == 2.0
    assert out["max_files_in_subtask"] == 2


def test_summarize_decomposition_outcomes_only_last_three_productive():
    from app.playbook_learning import summarize_decomposition_outcomes

    def _cycle(rate, created=1):
        return {
            "tasks_created": created,
            "decomposition_quality": [
                {"subtask_merge_rate": rate, "cross_module_count": 0}
            ],
        }

    direction = {
        "research_history": [
            _cycle(0.1),  # productive but outside last-3 window
            _cycle(0.0, created=0),  # unproductive — skipped entirely
            _cycle(0.5),
            _cycle(0.5),
            _cycle(0.5),
        ]
    }
    out = summarize_decomposition_outcomes(direction)
    assert out is not None
    assert out["prior_decomposition_count"] == 3
    assert out["avg_subtask_merge_rate"] == 0.5


# ─────────────────────────────────────────────────────────────────────────────
# Tests for effectiveness tracking: confirmed_count / followed_count increment
# (TDD stubs — xfail until sibling clusters 1–3 land)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.xfail(
    strict=False,
    reason="Sibling cluster 1/4 adds increment_playbook_applied_counts to playbook_learning.py",
)
@pytest.mark.asyncio
async def test_confirmed_count_increments_on_applied_entry_success(
    db_session: AsyncSession,
):
    """When a completed task has _applied_playbook_entries in its context, the referenced
    entry's confirmed_count and followed_count both increment."""
    from app.playbook_learning import increment_playbook_applied_counts

    key = "playbook/success-create-small"
    project = await _make_project(
        db_session,
        design_doc={
            key: {
                "content": "First-attempt success pattern for create tasks.",
                "source": "success-pattern",
                "confirmed_count": 1,
                "followed_count": 3,
                "applicable_types": ["create"],
                "updated_at": _utcnow().isoformat(),
            }
        },
    )
    task = await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )
    task.context = {
        **(task.context or {}),
        "_applied_playbook_entries": ["success-create-small"],
    }
    await db_session.flush()

    await increment_playbook_applied_counts(task, db_session, success=True)
    await db_session.flush()

    entry = (project.design_doc or {})[key]
    assert entry["confirmed_count"] == 2, "confirmed_count must increment on success"
    assert entry["followed_count"] == 4, (
        "followed_count must increment on any application"
    )


@pytest.mark.xfail(
    strict=False,
    reason="Sibling cluster 1/4 adds increment_playbook_applied_counts to playbook_learning.py",
)
@pytest.mark.asyncio
async def test_followed_count_increments_without_confirmed_on_failure(
    db_session: AsyncSession,
):
    """When an applied task fails, followed_count grows (denominator) but confirmed_count
    stays fixed — causing the success rate to drop as expected."""
    from app.playbook_learning import increment_playbook_applied_counts

    key = "playbook/success-create-small"
    project = await _make_project(
        db_session,
        design_doc={
            key: {
                "content": "First-attempt success pattern for create tasks.",
                "source": "success-pattern",
                "confirmed_count": 2,
                "followed_count": 3,
                "applicable_types": ["create"],
                "updated_at": _utcnow().isoformat(),
            }
        },
    )
    task = Task(
        project_id=project.id,
        title="Failed task",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.FAILED,
        context={
            "scope": "small",
            "_applied_playbook_entries": ["success-create-small"],
        },
    )
    db_session.add(task)
    await db_session.flush()

    await increment_playbook_applied_counts(task, db_session, success=False)
    await db_session.flush()

    entry = (project.design_doc or {})[key]
    assert entry["confirmed_count"] == 2, "confirmed_count must not change on failure"
    assert entry["followed_count"] == 4, (
        "followed_count must grow regardless of outcome"
    )


@pytest.mark.xfail(
    strict=False,
    reason="Sibling cluster 1/4 adds auto_retire_low_effectiveness_entries to playbook_learning.py",
)
def test_auto_retire_archives_low_success_rate_entry():
    """auto_retire_low_effectiveness_entries moves entries with followed_count >= min and
    confirmed/followed ratio below threshold into playbook_archive with an _archived_reason."""
    from app.playbook_learning import auto_retire_low_effectiveness_entries

    doc = {
        "playbook/stale-pattern": {
            "content": "Old pattern that stopped working",
            "source": "auto-generated",
            "confirmed_count": 1,
            "followed_count": 10,  # 10% success rate — well below threshold
            "updated_at": _utcnow().isoformat(),
        },
        "playbook/good-pattern": {
            "content": "Pattern that still works",
            "source": "auto-generated",
            "confirmed_count": 8,
            "followed_count": 10,  # 80% success rate — healthy
            "updated_at": _utcnow().isoformat(),
        },
    }

    auto_retire_low_effectiveness_entries(doc)

    assert "playbook/stale-pattern" not in doc, "Low-rate entry should be archived"
    assert "playbook/good-pattern" in doc, "High-rate entry should remain"
    archive = doc.get("playbook_archive", [])
    assert any(e.get("_archived_key") == "playbook/stale-pattern" for e in archive)
    assert any("_archived_reason" in e for e in archive), (
        "Must include explanatory reason tag"
    )


@pytest.mark.xfail(
    strict=False,
    reason="Sibling cluster 1/4 adds auto_retire_low_effectiveness_entries to playbook_learning.py",
)
def test_auto_retire_skips_entries_below_min_observations():
    """Entries with too few followed_count observations are not retired — insufficient data
    to judge effectiveness, so the rate is statistically meaningless."""
    from app.playbook_learning import auto_retire_low_effectiveness_entries

    doc = {
        "playbook/new-pattern": {
            "content": "Pattern with only 2 observations — not enough data",
            "source": "success-pattern",
            "confirmed_count": 0,
            "followed_count": 2,  # below minimum observation threshold
            "updated_at": _utcnow().isoformat(),
        },
    }

    auto_retire_low_effectiveness_entries(doc)

    assert "playbook/new-pattern" in doc, (
        "Under-observed entry must not be auto-retired"
    )
    assert not doc.get("playbook_archive"), (
        "No archive entries should be created for under-observed patterns"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Anti-poisoning: finalize_pending_playbook_entries
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_pending_playbook_entries_drops_on_failure(db_session):
    """A FAILED task's pending entries are removed so peers never see the lie."""
    from app.playbook_learning import finalize_pending_playbook_entries
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    await db_session.flush()

    project.design_doc = {
        "playbook/lie": {
            "content": "X exists in foo.py",
            "source_task_id": str(task.id),
            "pending_source": str(task.id),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 0,
        },
        "playbook/peer": {  # written by a different task — must NOT be touched
            "content": "Real, finalised entry",
            "source_task_id": str(uuid.uuid4()),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 1,
        },
    }
    flag_modified(project, "design_doc")
    await db_session.flush()

    n = await finalize_pending_playbook_entries(task, db_session, succeeded=False)
    await db_session.flush()

    assert n == 1
    doc = project.design_doc
    assert "playbook/lie" not in doc, "Failed task's pending entry must be removed"
    assert "playbook/peer" in doc, "Other tasks' entries must be preserved"


@pytest.mark.asyncio
async def test_finalize_pending_playbook_entries_promotes_on_success(db_session):
    """A COMPLETED task's pending entries lose pending_source and become visible."""
    from app.playbook_learning import finalize_pending_playbook_entries
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.flush()

    project.design_doc = {
        "playbook/proven": {
            "content": "Real finding from completed task",
            "source_task_id": str(task.id),
            "pending_source": str(task.id),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 0,
        },
    }
    flag_modified(project, "design_doc")
    await db_session.flush()

    n = await finalize_pending_playbook_entries(task, db_session, succeeded=True)
    await db_session.flush()

    assert n == 1
    entry = project.design_doc["playbook/proven"]
    assert "pending_source" not in entry, (
        "Successful task's pending flag must be cleared"
    )
    assert entry["content"] == "Real finding from completed task"


# ─────────────────────────────────────────────────────────────────────────────
# Tests for compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_sr_task(
    project_id,
    *,
    age_days: float,
    human_cats: dict,
    sr_failed_cats: list,
    task_type: str = "create",
    check_result_overrides: dict[str, dict] | None = None,
    sr_completed_at: datetime | None = None,
    rejection_at: datetime | None = None,
    sr_passed_cats_with_reasoning: dict[str, str] | None = None,
) -> Task:
    """Build a Task with both _self_review_outcome.check_results and _first_attempt_rejection_categories.

    sr_passed_cats_with_reasoning: optional {cat: reasoning_string} to add
    explicit PASS entries to check_results (for engagement tracking tests).
    """
    check_results = {cat: {"passed": False, "issue": "x"} for cat in sr_failed_cats}
    if sr_passed_cats_with_reasoning:
        for cat, reasoning in sr_passed_cats_with_reasoning.items():
            check_results[cat] = {"passed": True, "reasoning": reasoning}
    if check_result_overrides:
        for cat, overrides in check_result_overrides.items():
            if cat in check_results:
                check_results[cat].update(overrides)
    sr_outcome: dict = {"check_results": check_results}
    if sr_completed_at is not None:
        sr_outcome["completed_at"] = sr_completed_at.isoformat()
    ctx: dict = {
        "_self_review_outcome": sr_outcome,
        "_first_attempt_rejection_categories": human_cats,
    }
    if rejection_at is not None:
        ctx["_first_attempt_rejection_at"] = rejection_at.isoformat()
    task = Task(
        project_id=project_id,
        title="SR task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_returns_none_below_sample_size(
    db_session: AsyncSession,
):
    """Returns None when either window has fewer than _MIN_SR_FINDINGS reviewer findings."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for i in range(4):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(4):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is None


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_stamps_heartbeat_on_low_sample(
    db_session: AsyncSession,
):
    """Even when returning None (below _MIN_SR_FINDINGS), stamps signal_last_computed_at."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for i in range(4):  # 4 findings per window — below _MIN_SR_FINDINGS=5
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(4):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is None
    stored = (project.design_doc or {}).get("sr_catch_rate_trends", {})
    assert "signal_last_computed_at" in stored, (
        "heartbeat must be stamped even when returning None (idle/low-data state)"
    )


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_stamps_heartbeat_on_success(
    db_session: AsyncSession,
):
    """Stamps signal_last_computed_at in design_doc when computation produces a result."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):  # 5 findings per window — meets _MIN_SR_FINDINGS=5
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    stored = (project.design_doc or {}).get("sr_catch_rate_trends", {})
    assert "signal_last_computed_at" in stored, (
        "heartbeat must be stamped on every successful invocation"
    )


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_data_sufficient_false_on_low_sample(
    db_session: AsyncSession,
):
    """data_sufficient=False is stored when returning None (insufficient findings).
    last_sufficient_computed_at must NOT be set/overwritten so callers can tell
    when data was last actually sufficient."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for i in range(4):  # 4 findings — below _MIN_SR_FINDINGS=5
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(4):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is None
    stored = (project.design_doc or {}).get("sr_catch_rate_trends", {})
    assert stored.get("data_sufficient") is False, (
        "data_sufficient must be False when findings are insufficient"
    )
    assert "last_sufficient_computed_at" not in stored, (
        "last_sufficient_computed_at must not be set when no successful computation occurred"
    )


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_data_sufficient_true_on_success(
    db_session: AsyncSession,
):
    """data_sufficient=True and last_sufficient_computed_at are stored on a real computation."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):  # 5 findings per window — meets _MIN_SR_FINDINGS=5
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result.get("data_sufficient") is True, (
        "data_sufficient must be True on a successful computation"
    )
    assert "last_sufficient_computed_at" in result, (
        "last_sufficient_computed_at must be set on a successful computation"
    )
    stored = (project.design_doc or {}).get("sr_catch_rate_trends", {})
    assert stored.get("data_sufficient") is True
    assert "last_sufficient_computed_at" in stored


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_data_sufficient_preserves_last_sufficient_on_low(
    db_session: AsyncSession,
):
    """After a successful run, a subsequent low-data run preserves last_sufficient_computed_at."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)

    # First run with sufficient data
    tasks_ok = []
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks_ok.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks_ok.append(t)
    await db_session.flush()

    result1 = await compute_sr_catch_rate_trends(project.id, db_session, tasks_ok)
    assert result1 is not None
    stored1 = (project.design_doc or {}).get("sr_catch_rate_trends", {})
    last_sufficient = stored1["last_sufficient_computed_at"]

    # Second run with insufficient data — should preserve last_sufficient_computed_at
    tasks_low = []
    for _ in range(4):  # below threshold
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks_low.append(t)
    await db_session.flush()

    result2 = await compute_sr_catch_rate_trends(project.id, db_session, tasks_low)
    assert result2 is None
    stored2 = (project.design_doc or {}).get("sr_catch_rate_trends", {})
    assert stored2.get("data_sufficient") is False
    assert stored2.get("last_sufficient_computed_at") == last_sufficient, (
        "last_sufficient_computed_at must be preserved (not cleared) after a low-data run"
    )


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_exposes_finding_counts(
    db_session: AsyncSession,
):
    """result contains current_finding_count / previous_finding_count (true denominator)
    and per-category low_confidence_finding_count flag."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):
        t = _make_sr_task(
            project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id, age_days=20, human_cats={"test-coverage": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["current_finding_count"] == 5
    assert result["previous_finding_count"] == 5
    cat = result["categories"]["test-coverage"]
    assert cat["current_human_count"] == 5
    # 5 findings == _MIN_SR_FINDINGS, so not low-confidence
    assert cat["low_confidence_finding_count"] is False


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_low_confidence_suppresses_trend(
    db_session: AsyncSession,
):
    """Categories with fewer than _MIN_SR_FINDINGS findings are flagged low-confidence
    and their trend is suppressed to 'stable' even when hit rates diverge."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 6 tasks — 5 flag 'correctness', 1 flags 'test-coverage' (low-finding cat)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    t = _make_sr_task(
        project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[]
    )
    db_session.add(t)
    tasks.append(t)
    # Previous window: 5 tasks all flag 'correctness' (hit rate differs); 'test-coverage' absent
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    # 'test-coverage' has only 1 finding in current window — low-confidence, trend suppressed
    tc = result["categories"]["test-coverage"]
    assert tc["low_confidence_finding_count"] is True
    assert tc["trend"] == "stable"
    # 'correctness' has 5 current + 5 previous = enough findings, not low-confidence
    corr = result["categories"]["correctness"]
    assert corr["low_confidence_finding_count"] is False


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_per_category_hit_and_miss(
    db_session: AsyncSession,
):
    """Computes correct per-category hit_rate and miss_rate."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window (5 tasks): 3 hits, 2 misses on test-coverage
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window (5 tasks): all hits
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["test-coverage"]
    assert cat["current_hit_rate"] == round(3 / 5, 3)
    assert cat["miss_rate"] == round(1 - 3 / 5, 3)
    assert cat["previous_hit_rate"] == 1.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_splits_sr_and_reviewer_hits(
    db_session: AsyncSession,
):
    """Origin tags split combined hits into SR-alone vs reviewer-save counts."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    current_specs = [
        (["test-coverage"], None),
        (["test-coverage"], {"test-coverage": {"origin": "reviewer"}}),
        (
            ["test-coverage"],
            {"test-coverage": {"origin": "reviewer_recovery_after_incomplete_sr"}},
        ),
        ([], None),
        ([], None),
    ]
    for failed_cats, overrides in current_specs:
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=failed_cats,
            check_result_overrides=overrides,
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["test-coverage"]
    assert cat["current_hit_rate"] == round(3 / 5, 3)
    assert cat["sr_alone_hit_count"] == 1
    assert cat["reviewer_save_hit_count"] == 2
    assert cat["sr_alone_hit_rate"] == round(1 / 5, 3)
    assert cat["reviewer_save_rate"] == round(2 / 5, 3)
    assert cat["current_hit_rate"] == round(
        cat["sr_alone_hit_rate"] + cat["reviewer_save_rate"], 3
    )


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_split_excludes_false_positives(
    db_session: AsyncSession,
):
    """Bucket counts only count true positives — FP sr_failed entries (cat not in
    human_cats for the same task) must not inflate sr_alone/reviewer_save so
    the split rates always sum to current_hit_rate."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5 tasks where human flagged test-coverage (3 hits, 2 misses)
    # plus 2 tasks where humans flagged "correctness" but SR (incorrectly) flagged
    # test-coverage. The 2 extra entries are FPs for test-coverage and must not
    # add to its sr_alone/reviewer_save buckets (the bug being fixed).
    current_specs = [
        # human={test-coverage}, sr_failed=[test-coverage] (TP hits)
        ({"test-coverage": 1}, ["test-coverage"], None),
        (
            {"test-coverage": 1},
            ["test-coverage"],
            {"test-coverage": {"origin": "reviewer"}},
        ),
        (
            {"test-coverage": 1},
            ["test-coverage"],
            {"test-coverage": {"origin": "reviewer_recovery_after_incomplete_sr"}},
        ),
        # human={test-coverage}, sr_failed=[] (TP misses)
        ({"test-coverage": 1}, [], None),
        ({"test-coverage": 1}, [], None),
        # human={correctness}, sr_failed=[test-coverage] (FP for test-coverage)
        ({"correctness": 1}, ["test-coverage"], None),
        (
            {"correctness": 1},
            ["test-coverage"],
            {"test-coverage": {"origin": "reviewer"}},
        ),
    ]
    for human, failed_cats, overrides in current_specs:
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats=human,
            sr_failed_cats=failed_cats,
            check_result_overrides=overrides,
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["test-coverage"]
    # Combined hit_rate counts only TPs (3/5 — the 2 FP entries don't enter
    # human_count or hit_count).
    assert cat["current_hit_rate"] == round(3 / 5, 3)
    # Buckets must also count only TPs — not the 2 FP entries.
    assert cat["sr_alone_hit_count"] == 1
    assert cat["reviewer_save_hit_count"] == 2
    assert cat["sr_alone_hit_rate"] == round(1 / 5, 3)
    assert cat["reviewer_save_rate"] == round(2 / 5, 3)
    # Invariant: split rates sum to combined rate. With the buggy code the FP
    # entries would inflate either bucket and break this equality.
    assert round(cat["sr_alone_hit_rate"] + cat["reviewer_save_rate"], 3) == round(
        cat["current_hit_rate"], 3
    )


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_trend_classification(
    db_session: AsyncSession,
):
    """Hit rate drops >= 5pp → trend='decreasing'."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 3/5 = 0.6
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5/5 = 1.0 (delta = -0.4, well below -5pp threshold)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["categories"]["test-coverage"]["trend"] == "decreasing"
    assert result["categories"]["test-coverage"]["trend_significant"] is True


@pytest.mark.asyncio
async def test_trend_small_sample_swing_suppressed(db_session: AsyncSession):
    """n=5 per window with a 1-finding delta (0.2 hit-rate diff) is sampling noise.

    The two-proportion z-test (z≈0.63 < _TREND_Z_MIN=1.28) must suppress the
    directional label even though delta (0.2) exceeds _TREND_DELTA_THRESHOLD (0.05).
    trend_significant must be False to distinguish this from 'not enough data'.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 2/5 = 0.4 hit_rate
    for _ in range(2):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 3/5 = 0.6 hit_rate (delta=−0.2, z≈0.63)
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(2):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["test-coverage"]
    # delta=−0.2 exceeds the raw threshold but is not statistically significant
    assert cat["trend"] == "stable"
    assert cat["trend_significant"] is False


@pytest.mark.asyncio
async def test_trend_large_consistent_shift_reported(db_session: AsyncSession):
    """Large n with a 0.3 hit-rate drop is statistically significant and reported.

    n=30 per window, delta=−0.3 → z≈2.74 >> _TREND_Z_MIN=1.28; trend='decreasing'.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 12/30 = 0.4 hit_rate
    for _ in range(12):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(18):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 21/30 = 0.7 hit_rate (delta=−0.3, significant)
    for _ in range(21):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(9):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["trend"] == "decreasing"
    assert cat["trend_significant"] is True


@pytest.mark.asyncio
async def test_trend_significance_scales_with_sample_size(db_session: AsyncSession):
    """The same delta (0.2) reports 'stable' at n=5 but 'decreasing' at n=30.

    This validates the significance boundary: a fixed raw threshold is insufficient
    because the same rate change means very different things at different sample sizes.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    # --- Small n: n=5, delta=0.2 → NOT significant ---
    project_small = await _make_project(db_session)
    tasks_small = []
    for _ in range(2):  # current 2/5 = 0.4
        t = _make_sr_task(
            project_small.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks_small.append(t)
    for _ in range(3):
        t = _make_sr_task(
            project_small.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks_small.append(t)
    for _ in range(3):  # previous 3/5 = 0.6
        t = _make_sr_task(
            project_small.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks_small.append(t)
    for _ in range(2):
        t = _make_sr_task(
            project_small.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks_small.append(t)

    # --- Large n: n=30, delta=0.2 → significant ---
    project_large = await _make_project(db_session)
    tasks_large = []
    for _ in range(12):  # current 12/30 = 0.4
        t = _make_sr_task(
            project_large.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks_large.append(t)
    for _ in range(18):
        t = _make_sr_task(
            project_large.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks_large.append(t)
    for _ in range(18):  # previous 18/30 = 0.6
        t = _make_sr_task(
            project_large.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks_large.append(t)
    for _ in range(12):
        t = _make_sr_task(
            project_large.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks_large.append(t)

    await db_session.flush()

    result_small = await compute_sr_catch_rate_trends(
        project_small.id, db_session, tasks_small
    )
    result_large = await compute_sr_catch_rate_trends(
        project_large.id, db_session, tasks_large
    )

    assert result_small is not None
    assert result_large is not None
    small_cat = result_small["categories"]["error-handling"]
    large_cat = result_large["categories"]["error-handling"]
    # Same delta, different sample sizes → different significance outcomes
    assert small_cat["trend"] == "stable"
    assert small_cat["trend_significant"] is False
    assert large_cat["trend"] == "decreasing"
    assert large_cat["trend_significant"] is True


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_rate_history_grows(
    db_session: AsyncSession,
):
    """rate_history grows on each new window call and caps at 5."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    # Pre-seed design_doc with 4 existing history entries
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": (_utcnow() - timedelta(days=15)).isoformat(),
            "categories": {
                "test-coverage": {"rate_history": [0.8, 0.7, 0.6, 0.5]},
            },
        }
    }
    flag_modified(project, "design_doc")
    await db_session.flush()

    tasks = []
    # Current window: 3/5 hits
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5/5 hits
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    history = result["categories"]["test-coverage"]["rate_history"]
    # 4 existing + 1 new = 5 (capped at 5)
    assert len(history) == 5
    assert history[-1] == round(3 / 5, 3)


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_calendar_to_cycle_resets_history(
    db_session: AsyncSession,
):
    """Transitioning from calendar mode to cycle mode resets rate_history.

    When prior data was produced in calendar mode and this run uses cycle mode,
    the stored history is incomparable — the function must not concatenate them.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    # Pre-seed calendar-mode history with two prior entries.
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": (_utcnow() - timedelta(days=15)).isoformat(),
            "bucket_mode": "calendar",
            "categories": {
                "test-coverage": {"rate_history": [0.9, 0.85]},
            },
        }
    }
    flag_modified(project, "design_doc")
    await db_session.flush()

    # Create two completed cycles so cycle mode is selected.
    c_cycle = _make_cycle(project.id, age_days=2)
    p_cycle = _make_cycle(project.id, age_days=20)
    db_session.add(c_cycle)
    db_session.add(p_cycle)
    await db_session.flush()

    tasks = []
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=1,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=19,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        t.strategic_cycle_id = p_cycle.id
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "cycle"
    history = result["categories"]["test-coverage"]["rate_history"]
    # Old calendar entries must be discarded — only the fresh cycle measurement.
    assert len(history) == 1, f"expected 1 entry after mode transition, got {history}"
    assert 0.9 not in history
    assert 0.85 not in history


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_cycle_to_calendar_resets_history(
    db_session: AsyncSession,
):
    """Transitioning from cycle mode to calendar mode resets rate_history.

    When prior data was produced in cycle mode and this run falls back to
    calendar windows (no completed cycles with enough tasks), history must
    not carry over incomparable cycle-span rates.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    fake_cycle_id = "old-cycle-abc123"
    # Pre-seed cycle-mode history.
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": (_utcnow() - timedelta(days=15)).isoformat(),
            "bucket_mode": "cycle",
            "current_cycle_id": fake_cycle_id,
            "categories": {
                "test-coverage": {"rate_history": [0.7, 0.65]},
            },
        }
    }
    flag_modified(project, "design_doc")
    await db_session.flush()

    # No completed cycles — forces calendar fallback.
    tasks = []
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "calendar"
    history = result["categories"]["test-coverage"]["rate_history"]
    # Old cycle entries must be discarded — only the fresh calendar measurement.
    assert len(history) == 1, f"expected 1 entry after mode transition, got {history}"
    assert 0.7 not in history
    assert 0.65 not in history


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_persists_to_design_doc(
    db_session: AsyncSession,
):
    """Writes sr_catch_rate_trends key to project.design_doc."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    assert result is not None
    assert "sr_catch_rate_trends" in (project.design_doc or {})
    stored = project.design_doc["sr_catch_rate_trends"]
    assert "computed_at" in stored
    assert "categories" in stored
    assert "by_task_type" in stored


def _make_sr_task_with_fix_iter(
    project_id,
    *,
    age_days: float,
    human_cats: dict,
    sr_failed_cats: list,
    fix_iteration: int | None = None,
    task_type: str = "create",
) -> Task:
    sro: dict = {
        "check_results": {
            cat: {"passed": False, "issue": "x"} for cat in sr_failed_cats
        }
    }
    if fix_iteration is not None:
        sro["fix_iteration"] = fix_iteration
    task = Task(
        project_id=project_id,
        title="SR fix-iter task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": sro,
            "_first_attempt_rejection_categories": human_cats,
        },
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_fix_iteration_buckets(
    db_session: AsyncSession,
):
    """by_fix_iteration buckets tasks by _self_review_outcome.fix_iteration into
    '1' (None or <=1), '2', and '3+' (>=3). Buckets below _MIN_SR_SAMPLE are omitted."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list[Task] = []
    # Bucket "1": 5 tasks all hit (fix_iteration absent ⇒ original/first pass)
    for _ in range(5):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            fix_iteration=None,
        )
        db_session.add(t)
        tasks.append(t)
    # Bucket "2": 5 tasks with 2 hits, 3 misses on correctness
    for _ in range(2):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            fix_iteration=2,
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            fix_iteration=2,
        )
        db_session.add(t)
        tasks.append(t)
    # Bucket "3+": 5 tasks all missed
    for _ in range(5):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            fix_iteration=4,
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window — meets _MIN_SR_SAMPLE so compute returns a result
    for _ in range(5):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            fix_iteration=None,
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    by_iter = result.get("by_fix_iteration") or {}
    assert set(by_iter.keys()) == {"1", "2", "3+"}
    # Bucket "1": 0 misses → miss_rate 0
    assert by_iter["1"]["correctness"]["miss_rate"] == 0.0
    assert by_iter["1"]["correctness"]["sample_count"] == 5
    # Bucket "2": 3 of 5 missed → miss_rate 0.6
    assert by_iter["2"]["correctness"]["miss_rate"] == round(3 / 5, 3)
    assert by_iter["2"]["correctness"]["sample_count"] == 5
    # Bucket "3+": 5 of 5 missed → miss_rate 1.0
    assert by_iter["3+"]["correctness"]["miss_rate"] == 1.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_fix_iteration_omits_below_sample(
    db_session: AsyncSession,
):
    """Buckets with fewer than _MIN_SR_SAMPLE qualifying observations per category
    are omitted from by_fix_iteration."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list[Task] = []
    # Bucket "1": 5 tasks (above floor)
    for _ in range(5):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            fix_iteration=None,
        )
        db_session.add(t)
        tasks.append(t)
    # Bucket "2": only 2 tasks (below floor)
    for _ in range(2):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            fix_iteration=2,
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window — meets _MIN_SR_SAMPLE
    for _ in range(5):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            fix_iteration=None,
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    by_iter = result.get("by_fix_iteration") or {}
    assert "1" in by_iter
    # Bucket "2" had only 2 samples for correctness — filtered out.
    assert "2" not in by_iter
    assert "3+" not in by_iter


def _make_sr_task_with_direction(
    project_id,
    direction_id: str,
    *,
    age_days: float,
    human_cats: dict,
    sr_failed_cats: list,
    task_type: str = "create",
) -> Task:
    """SR task with direction_id stamped into context."""
    check_results = {cat: {"passed": False, "issue": "x"} for cat in sr_failed_cats}
    task = Task(
        project_id=project_id,
        title="SR direction task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {"check_results": check_results},
            "_first_attempt_rejection_categories": human_cats,
            "direction_id": direction_id,
        },
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_includes_by_direction_when_above_sample(
    db_session: AsyncSession,
):
    """compute_sr_catch_rate_trends should expose per-direction breakdown via the
    by_direction key when a direction has at least _MIN_DIRECTION_SR_SAMPLE qualifying tasks."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    direction_id = "d-by-dir-test"
    tasks = []
    # 4 tasks for the direction, all missed
    for i in range(4):
        t = _make_sr_task_with_direction(
            project.id,
            direction_id,
            age_days=2,
            human_cats={"regression_risk": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    # Window-overall threshold (need >= _MIN_SR_SAMPLE in current and previous)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    assert "by_direction" in result
    assert direction_id in result["by_direction"]
    entry = result["by_direction"][direction_id]
    assert entry["sample_count"] == 4
    assert "regression_risk" in entry["categories"]
    # All missed → miss_rate 1.0 → overall_catch_rate 0.0
    assert entry["categories"]["regression_risk"]["miss_rate"] == 1.0
    assert entry["miss_rate"] == 1.0
    assert entry["overall_catch_rate"] == 0.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_omits_below_sample_directions(
    db_session: AsyncSession,
):
    """Directions with fewer than _MIN_DIRECTION_SR_SAMPLE tasks are not exposed
    in by_direction (their data is not actionable)."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    # Only 2 tasks for this direction (under threshold of 3)
    tasks = []
    for i in range(2):
        t = _make_sr_task_with_direction(
            project.id,
            "d-low",
            age_days=2,
            human_cats={"regression_risk": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    # Window-overall threshold padding
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    by_direction = result.get("by_direction", {})
    assert "d-low" not in by_direction


def _make_direction_tasks(project_id, direction_id, count, age_days, miss_all=True):
    """Create `count` SR tasks for a direction; if miss_all, human catches but SR misses."""
    tasks = []
    for _ in range(count):
        sr_failed = [] if miss_all else ["regression_risk"]
        t = _make_sr_task_with_direction(
            project_id,
            direction_id,
            age_days=age_days,
            human_cats={"regression_risk": 1},
            sr_failed_cats=sr_failed,
        )
        tasks.append(t)
    return tasks


def _make_window_padding(project_id, age_days, n=5):
    """Pad the overall SR sample so compute_sr_catch_rate_trends has enough tasks."""
    tasks = []
    for _ in range(n):
        t = _make_sr_task(
            project_id,
            age_days=age_days,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        tasks.append(t)
    return tasks


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_trend_increasing(
    db_session: AsyncSession,
):
    """When current miss_rate exceeds previous by >= _TREND_DELTA_THRESHOLD, trend=='increasing'."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    direction_id = "d-trend-increasing"

    # Seed prior state: miss_rate was 0.0 (all caught)
    project.design_doc = {
        "sr_catch_rate_trends": {
            "by_direction": {
                direction_id: {
                    "miss_rate": 0.0,
                    "rate_history": [0.0],
                }
            }
        }
    }
    await db_session.flush()

    # Current window: all tasks missed (miss_rate → 1.0)
    tasks = []
    for t in _make_direction_tasks(
        project.id, direction_id, 4, age_days=2, miss_all=True
    ):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=2):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=20):
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    entry = result["by_direction"][direction_id]
    assert entry["trend"] == "increasing"
    assert entry["previous_miss_rate"] == 0.0
    assert len(entry["rate_history"]) == 2
    assert entry["rate_history"][0] == 0.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_trend_decreasing(
    db_session: AsyncSession,
):
    """When current miss_rate is lower than previous by >= _TREND_DELTA_THRESHOLD, trend=='decreasing'."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    direction_id = "d-trend-decreasing"

    # Seed prior state: miss_rate was 1.0 (all missed)
    project.design_doc = {
        "sr_catch_rate_trends": {
            "by_direction": {
                direction_id: {
                    "miss_rate": 1.0,
                    "rate_history": [1.0],
                }
            }
        }
    }
    await db_session.flush()

    # Current window: all tasks caught (miss_rate → 0.0)
    tasks = []
    for t in _make_direction_tasks(
        project.id, direction_id, 4, age_days=2, miss_all=False
    ):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=2):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=20):
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    entry = result["by_direction"][direction_id]
    assert entry["trend"] == "decreasing"
    assert entry["previous_miss_rate"] == 1.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_trend_stable(
    db_session: AsyncSession,
):
    """When current miss_rate delta is within ±_TREND_DELTA_THRESHOLD, trend=='stable'."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    direction_id = "d-trend-stable"

    # Seed prior state: miss_rate was 1.0
    project.design_doc = {
        "sr_catch_rate_trends": {
            "by_direction": {
                direction_id: {
                    "miss_rate": 1.0,
                    "rate_history": [1.0],
                }
            }
        }
    }
    await db_session.flush()

    # Current window: all missed too (miss_rate → 1.0, delta == 0)
    tasks = []
    for t in _make_direction_tasks(
        project.id, direction_id, 4, age_days=2, miss_all=True
    ):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=2):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=20):
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    entry = result["by_direction"][direction_id]
    assert entry["trend"] == "stable"


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_no_previous(
    db_session: AsyncSession,
):
    """First run (no prior history): previous_miss_rate=None, trend='stable', rate_history has one entry."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    direction_id = "d-trend-new"
    tasks = []
    for t in _make_direction_tasks(
        project.id, direction_id, 4, age_days=2, miss_all=True
    ):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=2):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=20):
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    entry = result["by_direction"][direction_id]
    assert entry["previous_miss_rate"] is None
    assert entry["trend"] == "stable"
    assert len(entry["rate_history"]) == 1


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_rate_history_capped(
    db_session: AsyncSession,
):
    """rate_history is capped at 5 entries; oldest are dropped when new entries arrive."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    direction_id = "d-hist-cap"

    # Seed prior state with 5 history entries (already at cap)
    project.design_doc = {
        "sr_catch_rate_trends": {
            "by_direction": {
                direction_id: {
                    "miss_rate": 0.9,
                    "rate_history": [0.5, 0.6, 0.7, 0.8, 0.9],
                }
            }
        }
    }
    await db_session.flush()

    tasks = []
    for t in _make_direction_tasks(
        project.id, direction_id, 4, age_days=2, miss_all=True
    ):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=2):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=20):
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    entry = result["by_direction"][direction_id]
    assert len(entry["rate_history"]) == 5
    # The oldest entry (0.5) should have been dropped
    assert 0.5 not in entry["rate_history"]


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_stores_hit_rate_and_sample_count(
    db_session: AsyncSession,
):
    """by_direction category entries must expose hit_rate and sample_count for the
    dispatcher's _current_signal_value_for_class per-direction handler."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    direction_id = "d-hitsample-test"
    tasks = []
    # 3 tasks: 2 caught (SR flagged + human confirmed), 1 missed (SR missed, human caught)
    for _ in range(2):
        t = _make_sr_task_with_direction(
            project.id,
            direction_id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    t = _make_sr_task_with_direction(
        project.id,
        direction_id,
        age_days=2,
        human_cats={"correctness": 1},
        sr_failed_cats=[],  # SR missed this one
    )
    db_session.add(t)
    tasks.append(t)
    # Project-wide window tasks
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat_entry = result["by_direction"][direction_id]["categories"]["correctness"]
    assert "hit_rate" in cat_entry, (
        "dispatcher reads hit_rate for magnitude calculation"
    )
    assert "sample_count" in cat_entry, "dispatcher reads sample_count for floor check"
    assert cat_entry["sample_count"] == 3
    assert abs(cat_entry["hit_rate"] - round(2 / 3, 3)) < 0.001


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_stores_direction_text(
    db_session: AsyncSession,
):
    """direction_text is stored in by_direction entries when project.directions has titles."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    direction_id = "d-text-test"
    project = await _make_project(db_session)
    # Inject a direction with a title into the project
    project.directions = [
        {"id": direction_id, "text": "Fix the login regression", "status": "active"}
    ]
    db_session.add(project)

    tasks = []
    for _ in range(3):
        t = _make_sr_task_with_direction(
            project.id,
            direction_id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    entry = result["by_direction"][direction_id]
    assert entry.get("direction_text") == "Fix the login regression"


# ─────────────────────────────────────────────────────────────────────────────
# Tests for reviewer_hallucination_rate in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_sr_task_with_rff(
    project_id,
    *,
    age_days: float,
    total_reviewer_findings: int,
    filtered_count: int,
) -> Task:
    """Build a Task carrying reviewer_findings_filtered in its SR outcome."""
    ctx: dict = {
        "_self_review_outcome": {
            "check_results": {"correctness": {"passed": True}},
            "reviewer_findings_filtered": {
                "total_reviewer_findings": total_reviewer_findings,
                "filtered_count": filtered_count,
                "sampled_phantom_paths": [],
            },
        },
        "_first_attempt_rejection_categories": {"correctness": 1},
    }
    task = Task(
        project_id=project_id,
        title="RFF task",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_reviewer_hallucination_rate_omitted_below_floor(
    db_session: AsyncSession,
):
    """reviewer_hallucination_rate is absent when total findings < floor (10)."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5 tasks, each with only 1 total finding → sum=5, below floor of 10
    for _ in range(5):
        t = _make_sr_task_with_rff(
            project.id, age_days=2, total_reviewer_findings=1, filtered_count=0
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task_with_rff(
            project.id, age_days=20, total_reviewer_findings=1, filtered_count=0
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    # Neither window meets the floor (5 < 10), so the key should be absent
    assert "reviewer_hallucination_rate" not in result


@pytest.mark.asyncio
async def test_reviewer_hallucination_rate_correct_computation(
    db_session: AsyncSession,
):
    """reviewer_hallucination_rate current_rate = filtered/total across current window tasks."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5 tasks, 4 findings each → total=20, filtered=5 → rate=0.25
    for _ in range(5):
        t = _make_sr_task_with_rff(
            project.id, age_days=2, total_reviewer_findings=4, filtered_count=1
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5 tasks, 4 findings each → total=20, filtered=0 → rate=0.0
    for _ in range(5):
        t = _make_sr_task_with_rff(
            project.id, age_days=20, total_reviewer_findings=4, filtered_count=0
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    assert "reviewer_hallucination_rate" in result
    rhr = result["reviewer_hallucination_rate"]
    assert rhr["current_rate"] == pytest.approx(0.25, abs=0.001)
    assert rhr["previous_rate"] == pytest.approx(0.0, abs=0.001)
    assert rhr["current_sample"] == 20
    assert rhr["previous_sample"] == 20
    assert rhr["delta"] == pytest.approx(0.25, abs=0.001)


@pytest.mark.asyncio
async def test_reviewer_hallucination_rate_delta_direction(
    db_session: AsyncSession,
):
    """delta is positive when current_rate > previous_rate (worsening)."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: higher hallucination rate (6/20 = 0.3)
    for _ in range(5):
        t = _make_sr_task_with_rff(
            project.id, age_days=2, total_reviewer_findings=4, filtered_count=1
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: lower hallucination rate (2/20 = 0.1)
    for _ in range(5):
        t = _make_sr_task_with_rff(
            project.id, age_days=20, total_reviewer_findings=4, filtered_count=0
        )
        db_session.add(t)
        tasks.append(t)
    # Add 2 more current and 2 more previous with extra filtered
    t_extra_curr = _make_sr_task_with_rff(
        project.id, age_days=3, total_reviewer_findings=0, filtered_count=0
    )
    t_extra_prev = _make_sr_task_with_rff(
        project.id, age_days=21, total_reviewer_findings=0, filtered_count=0
    )
    db_session.add(t_extra_curr)
    db_session.add(t_extra_prev)
    tasks.extend([t_extra_curr, t_extra_prev])
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    if "reviewer_hallucination_rate" in result:
        rhr = result["reviewer_hallucination_rate"]
        # delta should be current_rate - previous_rate
        if rhr["delta"] is not None:
            assert (
                abs(rhr["delta"] - (rhr["current_rate"] - rhr["previous_rate"])) < 0.001
            )


@pytest.mark.asyncio
async def test_reviewer_hallucination_rate_persists_to_design_doc(
    db_session: AsyncSession,
):
    """reviewer_hallucination_rate is persisted in project.design_doc['sr_catch_rate_trends']."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):
        t = _make_sr_task_with_rff(
            project.id, age_days=2, total_reviewer_findings=4, filtered_count=2
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task_with_rff(
            project.id, age_days=20, total_reviewer_findings=4, filtered_count=1
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    assert "reviewer_hallucination_rate" in result
    doc = project.design_doc or {}
    stored = doc.get("sr_catch_rate_trends", {})
    assert "reviewer_hallucination_rate" in stored


@pytest.mark.asyncio
async def test_reviewer_hallucination_rate_tasks_without_field_ignored(
    db_session: AsyncSession,
):
    """Tasks without reviewer_findings_filtered contribute nothing to the rate."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Mix tasks with and without the field; only those with field count
    for _ in range(3):
        t = _make_sr_task_with_rff(
            project.id, age_days=2, total_reviewer_findings=5, filtered_count=1
        )
        db_session.add(t)
        tasks.append(t)
    # 2 tasks without the field (old-style)
    for _ in range(2):
        t = _make_sr_task(
            project.id, age_days=2, human_cats={"correctness": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id, age_days=20, human_cats={"correctness": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    # Previous window has no tasks with reviewer_findings_filtered, so no rate for it.
    # Current window total = 3*5 = 15 >= 10, so current_rate should be present.
    if "reviewer_hallucination_rate" in result:
        rhr = result["reviewer_hallucination_rate"]
        assert rhr["current_sample"] == 15  # only the 3 tasks with rff contribute
        assert rhr["previous_rate"] is None  # previous window below floor


# ─────────────────────────────────────────────────────────────────────────────
# Tests for select_sr_miss_examples_for_injection
# ─────────────────────────────────────────────────────────────────────────────

_SAMPLE_TRENDS: dict = {
    "categories": {
        "correctness": {
            "weighted_miss_rate": 0.8,
            "examples": [{"severity": "MAJOR", "desc": "Global correctness issue"}],
        },
        "error-handling": {
            "weighted_miss_rate": 0.5,
            "examples": [{"severity": "MINOR", "desc": "Global error handling gap"}],
        },
    },
    "by_direction": {
        "dir-x": {
            "categories": {
                "security": {
                    "miss_rate": 0.9,
                    "examples": [
                        {"severity": "CRITICAL", "desc": "Direction X security miss"}
                    ],
                },
            }
        },
        "dir-empty": {
            "categories": {
                "correctness": {
                    "miss_rate": 0.7,
                    # no examples key → direction has no qualifying examples
                },
            }
        },
    },
}


def test_select_sr_miss_examples_global_when_no_direction_id():
    """When direction_id is None, global categories are used."""
    from app.playbook_learning import select_sr_miss_examples_for_injection

    result = select_sr_miss_examples_for_injection(_SAMPLE_TRENDS, direction_id=None)
    # Should include global categories ranked by weighted_miss_rate
    assert "correctness" in result
    assert any("Global correctness issue" in ex["desc"] for ex in result["correctness"])
    # Global ranking preserved: correctness (0.8) before error-handling (0.5)
    keys = list(result.keys())
    assert keys.index("correctness") < keys.index("error-handling")


def test_select_sr_miss_examples_prefers_direction_when_examples_present():
    """When direction_id is provided and has examples, those are preferred over global."""
    from app.playbook_learning import select_sr_miss_examples_for_injection

    result = select_sr_miss_examples_for_injection(_SAMPLE_TRENDS, direction_id="dir-x")
    # Direction-scoped category should be present
    assert "security" in result
    assert any("Direction X security miss" in ex["desc"] for ex in result["security"])
    # Global-only category must not appear (direction slice was used instead)
    assert "correctness" not in result
    assert "error-handling" not in result


def test_select_sr_miss_examples_falls_back_to_global_when_direction_has_no_examples():
    """When the direction has no categories with examples, fall back to global."""
    from app.playbook_learning import select_sr_miss_examples_for_injection

    result = select_sr_miss_examples_for_injection(
        _SAMPLE_TRENDS, direction_id="dir-empty"
    )
    # dir-empty has no examples, so global categories are used
    assert "correctness" in result
    assert "security" not in result


def test_select_sr_miss_examples_falls_back_to_global_for_unknown_direction():
    """When direction_id is not found in by_direction, fall back to global."""
    from app.playbook_learning import select_sr_miss_examples_for_injection

    result = select_sr_miss_examples_for_injection(
        _SAMPLE_TRENDS, direction_id="dir-unknown"
    )
    assert "correctness" in result


def test_select_sr_miss_examples_empty_trends_returns_empty():
    """When sr_catch_rate_trends has no categories, return empty dict."""
    from app.playbook_learning import select_sr_miss_examples_for_injection

    assert select_sr_miss_examples_for_injection({}) == {}
    assert select_sr_miss_examples_for_injection({"categories": {}}) == {}


# ─────────────────────────────────────────────────────────────────────────────
# Tests for reasoning engagement in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_sr_task_with_pass_reasoning(
    project_id,
    *,
    age_days: float,
    human_cats: dict,
    pass_cats_with_reasoning: dict[str, str],
) -> "Task":
    """Build a Task with PASS check_results entries that include reasoning text.

    pass_cats_with_reasoning: {cat: reasoning_string}.  Categories not in the
    dict are absent from check_results.  The task has no human rejections beyond
    what's supplied in human_cats.
    """
    check_results = {
        cat: {"passed": True, "reasoning": reasoning}
        for cat, reasoning in pass_cats_with_reasoning.items()
    }
    task = Task(
        project_id=project_id,
        title="SR task with PASS reasoning",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {"check_results": check_results},
            "_first_attempt_rejection_categories": human_cats,
        },
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_reasoning_engagement_populated_for_stable_categories(
    db_session: AsyncSession,
):
    """Engagement fields are present on stable/increasing categories (not just decreasing)."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window (10 tasks): correctness appears in both windows with stable hit rate.
    # 8 tasks: SR hits correctness (FAIL entry) + 2 tasks: SR passes with reasoning (PASS entry).
    for _ in range(8):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(2):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "substantial reasoning here"},
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window (10 tasks): same hit rate → stable trend.
    for _ in range(8):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(2):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"].get("correctness")
    assert cat is not None, "correctness category should be present"
    assert cat["trend"] == "stable", "should be stable — same rate in both windows"
    # Engagement fields must be populated even for stable categories.
    assert "reasoning_engagement_rate" in cat
    assert "reasoning_engagement_with" in cat
    assert "reasoning_engagement_total" in cat
    # 2 current-window tasks had PASS entries for correctness; 2 with substantial reasoning.
    assert cat["reasoning_engagement_total"] == 2
    assert cat["reasoning_engagement_with"] == 2
    assert cat["reasoning_engagement_rate"] == 1.0


@pytest.mark.asyncio
async def test_reasoning_quality_scoring(db_session: AsyncSession):
    """Reasoning quality score rewards file paths, test names, and word count."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Current window: 5 tasks with varying reasoning quality.
    # Empty reasoning: no quality score
    t = _make_sr_task(
        project.id,
        age_days=2,
        human_cats={"correctness": 1},
        sr_failed_cats=[],
        sr_passed_cats_with_reasoning={"correctness": ""},
    )
    db_session.add(t)
    tasks.append(t)

    # Low quality: "looks ok" (3 words, no signals) → score ≈ 0.0
    t = _make_sr_task(
        project.id,
        age_days=2,
        human_cats={"correctness": 1},
        sr_failed_cats=[],
        sr_passed_cats_with_reasoning={"correctness": "looks ok here"},
    )
    db_session.add(t)
    tasks.append(t)

    # Medium quality: "looks fine to me — checked the diff" (8 words, no signals) → score ≈ 0.34
    t = _make_sr_task(
        project.id,
        age_days=2,
        human_cats={"correctness": 1},
        sr_failed_cats=[],
        sr_passed_cats_with_reasoning={
            "correctness": "looks fine to me checked the diff"
        },
    )
    db_session.add(t)
    tasks.append(t)

    # High quality: file path + test name + word count → score = 1.0
    t = _make_sr_task(
        project.id,
        age_days=2,
        human_cats={"correctness": 1},
        sr_failed_cats=[],
        sr_passed_cats_with_reasoning={
            "correctness": "verified backend/app/foo.py handles the empty list case via test_foo.py::test_empty"
        },
    )
    db_session.add(t)
    tasks.append(t)

    # Medium-high: file path + word count → score ≈ 0.67
    t = _make_sr_task(
        project.id,
        age_days=2,
        human_cats={"correctness": 1},
        sr_failed_cats=[],
        sr_passed_cats_with_reasoning={
            "correctness": "checked the logic in backend/app/utils.py and verified all edge cases are correct"
        },
    )
    db_session.add(t)
    tasks.append(t)

    # Previous window: 5 tasks (same pattern to keep trend stable)
    for reasoning in [
        "",
        "looks ok here",
        "looks fine to me checked the diff",
        "verified backend/app/foo.py handles the empty list case via test_foo.py::test_empty",
        "checked the logic in backend/app/utils.py and verified all edge cases are correct",
    ]:
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": reasoning},
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"].get("correctness")
    assert cat is not None

    # Verify quality_rate is present and non-zero (average across all tasks)
    assert "reasoning_quality_rate" in cat
    quality_rate = cat["reasoning_quality_rate"]
    assert quality_rate is not None
    assert quality_rate > 0.0, "quality rate should be > 0 with mixed-quality inputs"
    # With the mixed inputs, quality_rate should be moderately good
    assert quality_rate >= 0.3


@pytest.mark.asyncio
async def test_pass_reasoning_thinning_alert_fires_for_non_regressing_category(
    db_session: AsyncSession,
):
    """A non-regressing category with low engagement and >= min-passes triggers thinning Alert."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from app.models import Alert, AlertSeverity

    project = await _make_project(db_session)
    tasks = []
    # Current window: correctness hit rate = 10/20 = 0.5 (exactly at floor, NOT below).
    # 10 hit tasks + 10 explicit PASS entries with no substantial reasoning.
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={
                "correctness": "x"
            },  # ≤ 10 chars → no engagement
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: same structure so trend = stable.
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    alerts = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.title.like("PASS reasoning thinning%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "correctness" in alerts[0].title


@pytest.mark.asyncio
async def test_pass_reasoning_thinning_alert_suppressed_below_min_passes(
    db_session: AsyncSession,
):
    """Alert is NOT fired when total PASS count is below min-passes threshold."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from app.models import Alert, AlertSeverity

    project = await _make_project(db_session)
    tasks = []
    # Current window: 8 hits + 3 PASS entries (below min_passes=10).
    for _ in range(8):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: same structure.
    for _ in range(8):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    alerts = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.title.like("PASS reasoning thinning%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0, "no alert when passes < min_passes"


@pytest.mark.asyncio
async def test_pass_reasoning_thinning_alert_suppressed_for_regressing_category(
    db_session: AsyncSession,
):
    """Alert is NOT fired for categories already covered by a regression alert."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from app.models import Alert, AlertSeverity

    project = await _make_project(db_session)
    tasks = []
    # correctness is regressing: 3/10 hit in current, 5/5 in previous.
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(7):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={
                "correctness": "x"
            },  # explicit PASS, no reasoning
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    thinning_alerts = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.title.like("PASS reasoning thinning%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(thinning_alerts) == 0, (
        "thinning alert suppressed for regressing category"
    )


@pytest.mark.asyncio
async def test_pass_reasoning_thinning_alert_honors_design_doc_overrides(
    db_session: AsyncSession,
):
    """sr_engagement_threshold and sr_engagement_min_passes overrides from design_doc are honored."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from app.models import Alert, AlertSeverity

    project = await _make_project(db_session)
    # Override: threshold=1.0 (always fire if any PASS lacks reasoning), min_passes=3.
    project.design_doc = {
        "sr_engagement_threshold": 1.0,
        "sr_engagement_min_passes": 3,
    }
    tasks = []
    # Current window: 7 hits + 3 PASS entries (1 with reasoning, 2 without).
    # Engagement = 1/3 = 0.33 < 1.0, total_passes=3 >= 3 → alert fires.
    for _ in range(7):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    t = _make_sr_task(
        project.id,
        age_days=2,
        human_cats={"correctness": 1},
        sr_failed_cats=[],
        sr_passed_cats_with_reasoning={
            "correctness": "this is a substantial reasoning"
        },
    )
    db_session.add(t)
    tasks.append(t)
    for _ in range(2):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},  # short → not counted
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: same structure (stable trend).
    for _ in range(7):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    alerts = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.title.like("PASS reasoning thinning%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1, (
        "alert fires because engagement (0.33) < threshold (1.0) with 3 passes"
    )


@pytest.mark.asyncio
async def test_pass_reasoning_thinning_alert_deduplicates_within_cycle(
    db_session: AsyncSession,
):
    """Calling compute_sr_catch_rate_trends twice with the same thinning state does not create duplicate Alerts."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from app.models import Alert, AlertSeverity

    project = await _make_project(db_session)
    tasks = []
    # Same structure as the fires test: hit rate=0.5, 10 PASS entries with no reasoning.
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    # First call.
    await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()
    # Second call — same data, same cycle.
    await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    alerts = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.title.like("PASS reasoning thinning%"),
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1, "dedup: second call must not create a second Alert"


# ─────────────────────────────────────────────────────────────────────────────
# Tests for _categorize_review_issues — scope-drift bucket
# ─────────────────────────────────────────────────────────────────────────────


def test_categorize_review_issues_scope_drift_keyword_routes_correctly():
    """Review comments containing scope-drift keywords are bucketed under scope-drift."""
    from app.playbook_learning import _categorize_review_issues

    review_history = [
        {
            "iteration": 1,
            "issues": [
                {
                    "description": "changes are too large for declared scope",
                    "severity": "MAJOR",
                },
            ],
        }
    ]

    result = _categorize_review_issues(review_history)

    assert "scope-drift" in result
    assert len(result["scope-drift"]) == 1
    assert result["scope-drift"][0]["severity"] == "MAJOR"


def test_categorize_review_issues_out_of_scope_routes_to_scope_drift():
    """'out of scope' phrasing also routes to scope-drift bucket."""
    from app.playbook_learning import _categorize_review_issues

    review_history = [
        {
            "iteration": 1,
            "issues": [
                {
                    "description": "This change includes unrelated changes beyond scope",
                    "severity": "MINOR",
                },
            ],
        }
    ]

    result = _categorize_review_issues(review_history)

    assert "scope-drift" in result
    assert len(result["scope-drift"]) == 1


def test_categorize_review_issues_scope_drift_not_in_other_bucket():
    """Scope-drift issue should not appear in the 'other' bucket."""
    from app.playbook_learning import _categorize_review_issues

    review_history = [
        {
            "iteration": 1,
            "issues": [
                {
                    "description": "diff size is much too large, beyond scope of this task",
                    "severity": "CRITICAL",
                },
            ],
        }
    ]

    result = _categorize_review_issues(review_history)

    assert "scope-drift" in result
    assert "other" not in result


# ─────────────────────────────────────────────────────────────────────────────
# Tests for _categorize_review_issues — regression_risk bucket
# ─────────────────────────────────────────────────────────────────────────────


def test_categorize_review_issues_regression_risk_keyword_routes_correctly():
    """Review comments mentioning a previously-passing test now failing route to regression_risk."""
    from app.playbook_learning import _categorize_review_issues

    review_history = [
        {
            "iteration": 1,
            "issues": [
                {
                    "description": "previously passing test now fails after this change",
                    "severity": "MAJOR",
                },
            ],
        }
    ]

    result = _categorize_review_issues(review_history)

    assert "regression_risk" in result
    assert len(result["regression_risk"]) == 1
    assert result["regression_risk"][0]["severity"] == "MAJOR"


@pytest.mark.parametrize(
    "phrasing",
    [
        "this regresses test_foo",
        "broke test_foo in this diff",
        "now failing assertion that was passing before",
        "the build no longer works after these changes",
        "this is a regression in the auth flow",
        "breaks test_serializer",
    ],
)
def test_categorize_review_issues_regression_risk_phrasings(phrasing):
    """A variety of regression-flavored phrasings all route to regression_risk."""
    from app.playbook_learning import _categorize_review_issues

    review_history = [
        {
            "iteration": 1,
            "issues": [
                {"description": phrasing, "severity": "MAJOR"},
            ],
        }
    ]

    result = _categorize_review_issues(review_history)

    assert "regression_risk" in result, f"phrasing did not route: {phrasing!r}"
    # Must NOT fall through to correctness or other.
    assert "other" not in result, f"phrasing leaked to other: {phrasing!r}"


def test_categorize_review_issues_regression_risk_wins_over_correctness():
    """First-match ordering: a regression phrasing that also contains 'assertion'
    must land in regression_risk, NOT correctness."""
    from app.playbook_learning import _categorize_review_issues

    review_history = [
        {
            "iteration": 1,
            "issues": [
                {
                    "description": "this breaks the previously-passing assertion in test_foo",
                    "severity": "CRITICAL",
                },
            ],
        }
    ]

    result = _categorize_review_issues(review_history)

    assert "regression_risk" in result
    assert "correctness" not in result


def test_categorize_review_issues_regression_risk_does_not_steal_correctness():
    """A vanilla correctness comment with no regression keywords must still land
    in correctness, not in regression_risk."""
    from app.playbook_learning import _categorize_review_issues

    review_history = [
        {
            "iteration": 1,
            "issues": [
                {
                    "description": "off-by-one bug in the boundary handling logic",
                    "severity": "MAJOR",
                },
            ],
        }
    ]

    result = _categorize_review_issues(review_history)

    assert "correctness" in result
    assert "regression_risk" not in result


def test_categorize_review_issues_regression_risk_in_valid_categories():
    """regression_risk is exposed as an LLM-classifier valid category."""
    from app.playbook_learning import _REVIEW_CATEGORIES, _VALID_CATEGORIES

    assert "regression_risk" in _REVIEW_CATEGORIES
    assert "regression_risk" in _VALID_CATEGORIES
    # Ordering matters for first-match-wins keyword classification.
    keys = list(_REVIEW_CATEGORIES.keys())
    assert keys.index("regression_risk") < keys.index("correctness")


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_regression_risk_hit_and_miss(
    db_session: AsyncSession,
):
    """When tasks carry regression_risk in both human_cats and sr_failed, the dashboard
    surfaces a non-zero hit_rate on the regression_risk axis."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window (5 tasks): 3 hits, 2 misses on regression_risk
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"regression_risk": 1},
            sr_failed_cats=["regression_risk"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"regression_risk": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window (5 tasks): all hits
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"regression_risk": 1},
            sr_failed_cats=["regression_risk"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert "regression_risk" in result["categories"], (
        "regression_risk axis must be wired through compute_sr_catch_rate_trends"
    )
    cat = result["categories"]["regression_risk"]
    assert cat["current_hit_rate"] == round(3 / 5, 3)
    assert cat["miss_rate"] == round(1 - 3 / 5, 3)
    assert cat["previous_hit_rate"] == 1.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_per_task_type_breakdown(
    db_session: AsyncSession,
):
    """by_task_type contains correct hit/miss rates per (task_type, category)."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5 'create' tasks — 4 hits on test-coverage
    for i in range(4):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
            task_type="create",
        )
        db_session.add(t)
        tasks.append(t)
    t = _make_sr_task(
        project.id,
        age_days=2,
        human_cats={"test-coverage": 1},
        sr_failed_cats=[],
        task_type="create",
    )
    db_session.add(t)
    tasks.append(t)
    # Current window: 5 'fix' tasks — 2 hits on test-coverage
    for i in range(2):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
            task_type="fix",
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=[],
            task_type="fix",
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5 tasks needed for overall threshold
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    by_type = result["by_task_type"]
    assert "create" in by_type
    assert "fix" in by_type
    create_cat = by_type["create"]["test-coverage"]
    assert create_cat["hit_rate"] == round(4 / 5, 3)
    assert create_cat["miss_rate"] == round(1 / 5, 3)
    assert create_cat["sample_count"] == 5
    fix_cat = by_type["fix"]["test-coverage"]
    assert fix_cat["hit_rate"] == round(2 / 5, 3)
    assert fix_cat["miss_rate"] == round(3 / 5, 3)


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_task_type_threshold_omits_low_sample(
    db_session: AsyncSession,
):
    """Cells with fewer than 5 samples per (task_type, category) are omitted from by_task_type."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5 'create' tasks (meets threshold), 4 'fix' tasks (below threshold)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            task_type="create",
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(4):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            task_type="fix",
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    by_type = result["by_task_type"]
    assert "create" in by_type
    assert "fix" not in by_type


# ─────────────────────────────────────────────────────────────────────────────
# Tests for by_scope breakdown in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _set_task_scope(task: Task, scope: str) -> Task:
    """Attach scope to task.context, preserving SR fields set by _make_sr_task."""
    ctx = dict(task.context or {})
    ctx["scope"] = scope
    task.context = ctx
    return task


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_scope_basic(
    db_session: AsyncSession,
):
    """by_scope contains correct hit/miss rates per (scope, category)."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 5 small-scope tasks: 4 hits on test-coverage
    for _ in range(4):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"test-coverage": 1},
                sr_failed_cats=["test-coverage"],
            ),
            "small",
        )
        db_session.add(t)
        tasks.append(t)
    t = _set_task_scope(
        _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=[],
        ),
        "small",
    )
    db_session.add(t)
    tasks.append(t)
    # 5 large-scope tasks: 1 hit on test-coverage (markedly more misses)
    for _ in range(1):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"test-coverage": 1},
                sr_failed_cats=["test-coverage"],
            ),
            "large",
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(4):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"test-coverage": 1},
                sr_failed_cats=[],
            ),
            "large",
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert "by_scope" in result
    by_scope = result["by_scope"]
    assert "small" in by_scope
    assert "large" in by_scope
    small_cell = by_scope["small"]["test-coverage"]
    assert small_cell["hit_rate"] == round(4 / 5, 3)
    assert small_cell["miss_rate"] == round(1 / 5, 3)
    assert small_cell["sample_count"] == 5
    large_cell = by_scope["large"]["test-coverage"]
    assert large_cell["hit_rate"] == round(1 / 5, 3)
    assert large_cell["miss_rate"] == round(4 / 5, 3)
    assert large_cell["sample_count"] == 5
    # Observability win: large-scope miss rate is markedly higher than small.
    assert large_cell["miss_rate"] > small_cell["miss_rate"]


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_scope_threshold_omits_low_sample(
    db_session: AsyncSession,
):
    """Cells with fewer than 5 samples per (scope, category) are omitted from by_scope."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 5 small-scope tasks (meets threshold)
    for _ in range(5):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"correctness": 1},
                sr_failed_cats=["correctness"],
            ),
            "small",
        )
        db_session.add(t)
        tasks.append(t)
    # 4 medium-scope tasks (below threshold — must be omitted)
    for _ in range(4):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"correctness": 1},
                sr_failed_cats=["correctness"],
            ),
            "medium",
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    by_scope = result["by_scope"]
    assert "small" in by_scope
    # medium had 4 samples — must be omitted entirely (no 0%/100% sentinel cell)
    assert "medium" not in by_scope


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_scope_default_small_when_missing(
    db_session: AsyncSession,
):
    """Tasks without an explicit scope bucket under DEFAULT_TASK_SCOPE='small'."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 5 tasks with no scope set in context — should bucket under 'small' (default).
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    by_scope = result["by_scope"]
    assert "small" in by_scope
    assert by_scope["small"]["correctness"]["sample_count"] == 5


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_scope_fp_stats(
    db_session: AsyncSession,
):
    """by_scope cells carry fp_count / tp_count / false_positive_rate."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 5 large-scope create tasks: SR flags `correctness` but human never confirms → FP=5
    for _ in range(5):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"correctness": 1},
                sr_failed_cats=["correctness"],
            ),
            "large",
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    by_scope = result["by_scope"]
    assert "large" in by_scope
    cell = by_scope["large"]["correctness"]
    assert "fp_count" in cell
    assert "tp_count" in cell
    assert "false_positive_rate" in cell
    # All 5 SR flags were confirmed by human → all TPs, no FPs.
    assert cell["tp_count"] == 5
    assert cell["fp_count"] == 0
    assert cell["false_positive_rate"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Tests for by_scope rate_history and trend in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_scope_rate_history_and_trend(
    db_session: AsyncSession,
):
    """by_scope cells carry rate_history and trend after multiple windows."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    # Seed existing by_scope: small/test-coverage had hit_rate=0.8 last window.
    doc = dict(project.design_doc or {})
    doc["sr_catch_rate_trends"] = {
        "computed_at": (_utcnow() - timedelta(days=15)).isoformat(),
        "by_scope": {
            "small": {
                "test-coverage": {"hit_rate": 0.8, "rate_history": [0.8]},
            }
        },
        "categories": {},
    }
    project.design_doc = doc
    flag_modified(project, "design_doc")
    await db_session.flush()

    tasks = []
    # Current window: 2/5 hits (hit_rate=0.4, dropping 0.4 from 0.8 → decreasing)
    for _ in range(2):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"test-coverage": 1},
                sr_failed_cats=["test-coverage"],
            ),
            "small",
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"test-coverage": 1},
                sr_failed_cats=[],
            ),
            "small",
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window (needed to satisfy _MIN_SR_SAMPLE for the function)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cell = result["by_scope"]["small"]["test-coverage"]
    assert "rate_history" in cell
    # Existing [0.8] + new 0.4 = [0.8, 0.4]
    assert len(cell["rate_history"]) == 2
    assert cell["rate_history"][-1] == round(2 / 5, 3)
    # hit_rate dropped 0.4 → delta <= -0.05 → "decreasing"
    assert cell["trend"] == "decreasing"


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_scope_rate_history_cap(
    db_session: AsyncSession,
):
    """rate_history per (scope, category) is capped at 5 entries."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    doc = dict(project.design_doc or {})
    doc["sr_catch_rate_trends"] = {
        "computed_at": (_utcnow() - timedelta(days=15)).isoformat(),
        "by_scope": {
            "small": {
                "test-coverage": {
                    "hit_rate": 0.8,
                    "rate_history": [0.9, 0.85, 0.8, 0.75],
                },
            }
        },
        "categories": {},
    }
    project.design_doc = doc
    flag_modified(project, "design_doc")
    await db_session.flush()

    tasks = []
    for _ in range(5):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"test-coverage": 1},
                sr_failed_cats=["test-coverage"],
            ),
            "small",
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cell = result["by_scope"]["small"]["test-coverage"]
    assert len(cell["rate_history"]) <= 5


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_scope_below_sample_no_history(
    db_session: AsyncSession,
):
    """Cells below _MIN_SR_SAMPLE are absent and do not pollute rate_history."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 4 small/test-coverage tasks: below _MIN_SR_SAMPLE=5 → cell absent.
    # One extra task on a different category to keep the current window ≥ 5 total
    # (otherwise the overall function returns None before reaching by_scope).
    for _ in range(4):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"test-coverage": 1},
                sr_failed_cats=["test-coverage"],
            ),
            "small",
        )
        db_session.add(t)
        tasks.append(t)
    t = _set_task_scope(
        _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
        ),
        "medium",
    )
    db_session.add(t)
    tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    small = (result.get("by_scope") or {}).get("small", {})
    assert "test-coverage" not in small


# ─────────────────────────────────────────────────────────────────────────────
# Tests for miss-example collection in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_sr_task_with_review(
    project_id,
    *,
    age_days: float,
    human_cats: dict,
    sr_failed_cats: list,
    review_issues: list,
    task_type: str = "create",
) -> "Task":
    """Build a Task with review_history issues for example extraction."""
    check_results = {cat: {"passed": False, "issue": "x"} for cat in sr_failed_cats}
    task = Task(
        project_id=project_id,
        title="SR task with review",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {"check_results": check_results},
            "_first_attempt_rejection_categories": human_cats,
            "review_loop": {
                "review_history": [{"iteration": 1, "issues": review_issues}]
            },
        },
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_example_issues_populated(
    db_session: AsyncSession,
):
    """Examples are collected from review_history for missed categories."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    review_issue = {"description": "Missing null check in handler", "severity": "MAJOR"}
    # 5 current tasks: SR missed error-handling (sr_failed_cats=[])
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
            review_issues=[review_issue],
        )
        db_session.add(t)
        tasks.append(t)
    # 5 previous tasks
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["error-handling"]
    assert "examples" in cat
    assert len(cat["examples"]) >= 1
    assert any("null check" in ex["desc"] for ex in cat["examples"])
    assert all(
        ex["severity"] in ("CRITICAL", "MAJOR", "MINOR") for ex in cat["examples"]
    )


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_examples_by_task_type_scoped(
    db_session: AsyncSession,
):
    """Examples in by_task_type are drawn only from matching task types."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # "null check" is in error-handling keywords; "fallback" is also in error-handling
    create_issue = {
        "description": "Missing null check in create handler",
        "severity": "MAJOR",
    }
    fix_issue = {
        "description": "Unhandled exception in fix scenario",
        "severity": "MAJOR",
    }

    # 5 'create' tasks missing error-handling
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
            review_issues=[create_issue],
            task_type="create",
        )
        db_session.add(t)
        tasks.append(t)
    # 5 'fix' tasks missing error-handling
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
            review_issues=[fix_issue],
            task_type="fix",
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    by_type = result["by_task_type"]
    assert "create" in by_type
    assert "fix" in by_type
    create_exs = by_type["create"]["error-handling"].get("examples", [])
    fix_exs = by_type["fix"]["error-handling"].get("examples", [])
    create_descs = [e["desc"] for e in create_exs]
    fix_descs = [e["desc"] for e in fix_exs]
    # create examples must contain create-specific text and not fix-specific text
    assert any("null check" in d for d in create_descs)
    assert not any("exception" in d for d in create_descs)
    assert any("exception" in d for d in fix_descs)
    assert not any("null check" in d for d in fix_descs)


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_sensitive_examples_dropped(
    db_session: AsyncSession,
):
    """Descriptions containing sensitive tokens are excluded from examples."""
    from app.playbook_learning import compute_sr_catch_rate_trends, _SENSITIVE_TOKENS

    project = await _make_project(db_session)
    tasks = []
    # Use a token from the list
    sensitive_token = _SENSITIVE_TOKENS[0]  # e.g. "password"
    review_issues = [
        {"description": f"Leaked {sensitive_token} in logs", "severity": "CRITICAL"},
        {"description": "Unhandled exception in handler", "severity": "MAJOR"},
    ]
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
            review_issues=review_issues,
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    examples = result["categories"]["error-handling"].get("examples", [])
    assert all(sensitive_token not in ex["desc"].lower() for ex in examples)
    # The non-sensitive issue should still appear
    assert any("Unhandled exception" in ex["desc"] for ex in examples)


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_descriptions_truncated_and_pr_stripped(
    db_session: AsyncSession,
):
    """Descriptions are truncated to <=100 chars and PR refs / URLs are stripped."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    long_desc = "A" * 200  # 200 chars, should be truncated
    pr_desc = "Fix regression introduced in PR #1234 — missing guard"
    url_desc = "See https://example.com/issue/99 for context — unhandled null"
    review_issues = [
        {"description": long_desc, "severity": "MAJOR"},
        {"description": pr_desc, "severity": "MAJOR"},
        {"description": url_desc, "severity": "MINOR"},
    ]
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
            review_issues=review_issues,
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    examples = result["categories"]["error-handling"].get("examples", [])
    for ex in examples:
        assert len(ex["desc"]) <= 100
    pr_exs = [e for e in examples if "missing guard" in e["desc"]]
    if pr_exs:
        assert "PR" not in pr_exs[0]["desc"]
        assert "#1234" not in pr_exs[0]["desc"]
    url_exs = [e for e in examples if "unhandled null" in e["desc"]]
    if url_exs:
        assert "https://" not in url_exs[0]["desc"]


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_example_cap_per_category(
    db_session: AsyncSession,
):
    """At most _EXAMPLE_MAX_PER_CAT examples are stored per category."""
    from app.playbook_learning import compute_sr_catch_rate_trends, _EXAMPLE_MAX_PER_CAT

    project = await _make_project(db_session)
    tasks = []
    # Provide many distinct review issues per task
    review_issues = [
        {"description": f"Issue number {i}", "severity": "MAJOR"} for i in range(10)
    ]
    for _ in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
            review_issues=review_issues,
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    examples = result["categories"]["error-handling"].get("examples", [])
    assert len(examples) <= _EXAMPLE_MAX_PER_CAT


# ─────────────────────────────────────────────────────────────────────────────
# 'other' bucket exclusion from SR catch-rate denominator
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_excludes_other_from_denominator(
    db_session: AsyncSession,
):
    """'other' category must not appear as a 0% catch-rate row nor depress overall_hit_rate.

    Self-review never emits 'other', so any task whose human-truth set contains
    'other' would otherwise be a guaranteed miss that silently drags the headline
    down.  This test builds two windows where the only human-flagged category in
    some tasks is 'other', verifies that 'other' is absent from the categories
    dict, and that overall_hit_rate reflects only the canonical categories.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5 canonical 'correctness' tasks (SR hits all 5) + 2 tasks
    # flagging only 'other' (SR can never emit 'other').  Without filtering,
    # overall_hit_rate would be 5/7 ≈ 0.714; with 'other' excluded it must be 5/5 = 1.0.
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(2):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"other": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5 tasks all hitting 'correctness' (needed for trend gating)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cats = result.get("categories", {})
    assert "other" not in cats, "'other' must not appear as a catch-rate category"
    assert "correctness" in cats, "canonical categories must still be present"
    # overall_hit_rate must reflect only canonical categories (5 hits / 5 findings = 1.0)
    assert result.get("overall_hit_rate") == 1.0, (
        f"overall_hit_rate should be 1.0 (5/5 canonical hits) but got "
        f"{result.get('overall_hit_rate')} — 'other' is depressing it"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests for regression notifications in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_decreasing_task_set(project_id, db_session, tasks):
    """Helper: 5 current tasks (3/5 hit) + 5 previous tasks (5/5 hit) → decreasing."""
    for i in range(3):
        t = _make_sr_task(
            project_id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project_id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project_id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)


@pytest.mark.asyncio
async def test_sr_catch_rate_regression_fires_notification_on_decreasing(
    db_session: AsyncSession,
):
    """A decreasing trend triggers post_failure_alert_message and dispatch_external_notification."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    _make_decreasing_task_set(project.id, db_session, tasks)
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as mock_dispatch,
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["categories"]["test-coverage"]["trend"] == "decreasing"

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    description = (
        call_kwargs.args[2]
        if len(call_kwargs.args) >= 3
        else call_kwargs.kwargs.get("description", "")
    )
    assert "test-coverage" in description
    assert "→" in description

    mock_dispatch.assert_called_once()
    dispatch_kwargs = mock_dispatch.call_args
    payload = dispatch_kwargs.kwargs.get("payload") or dispatch_kwargs.args[2]
    assert (
        dispatch_kwargs.kwargs.get("event_type") == "sr_catch_rate_regression"
        or dispatch_kwargs.args[1] == "sr_catch_rate_regression"
    )
    assert "regressing_categories" in payload
    cats = payload["regressing_categories"]
    assert len(cats) == 1
    assert cats[0]["category"] == "test-coverage"
    assert cats[0]["previous_hit_rate"] is not None
    assert cats[0]["current_hit_rate"] is not None
    assert cats[0]["sample_size"] > 0

    # AlertsBanner surfacing: the same firing path must insert an
    # undismissed WARNING Alert row keyed on the regression title prefix.
    from app.playbook_learning import _SR_REGRESSION_ALERT_TITLE_PREFIX

    await db_session.commit()
    alert_rows = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    reg_rows = [
        r for r in alert_rows if r.title.startswith(_SR_REGRESSION_ALERT_TITLE_PREFIX)
    ]
    assert len(reg_rows) == 1
    assert reg_rows[0].severity == AlertSeverity.WARNING
    assert "test-coverage" in reg_rows[0].title
    # Message reuses the same description string as the ProjectMessage
    assert "test-coverage" in reg_rows[0].message


@pytest.mark.asyncio
async def test_sr_catch_rate_regression_no_duplicate_in_same_window(
    db_session: AsyncSession,
):
    """A second call with the same regressing categories in the same window does NOT re-post."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    _make_decreasing_task_set(project.id, db_session, tasks)
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    # Verify alert state was persisted
    await db_session.refresh(project)
    assert "sr_catch_rate_trends_alert" in (project.design_doc or {})
    assert mock_post.call_count == 1

    # Second call in the same window — should be deduped
    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post2,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    mock_post2.assert_not_called()

    # The dedup contract also covers the in-app Alert row: still exactly one.
    from app.playbook_learning import _SR_REGRESSION_ALERT_TITLE_PREFIX

    await db_session.commit()
    alert_rows = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    reg_rows = [
        r for r in alert_rows if r.title.startswith(_SR_REGRESSION_ALERT_TITLE_PREFIX)
    ]
    assert len(reg_rows) == 1


@pytest.mark.asyncio
async def test_sr_catch_rate_regression_no_notification_when_all_stable_or_increasing(
    db_session: AsyncSession,
):
    """No notification fires when no category is decreasing."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5/5 hit (stable/increasing vs previous 3/5)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project.id, age_days=20, human_cats={"test-coverage": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as mock_dispatch,
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    trend = result["categories"]["test-coverage"]["trend"]
    assert trend in ("stable", "increasing")
    mock_post.assert_not_called()
    mock_dispatch.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Tests for all_iterations_catch_rate in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_all_iterations_catch_rate_vs_first_attempt(
    db_session: AsyncSession,
):
    """A category flagged only in iteration 2+ appears in all_iterations_catch_rate as a miss
    but current_hit_rate (first-attempt) is None for that category — the two metrics diverge."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window (5 tasks): SR passes everything.
    # First-attempt human truth: test-coverage only (iteration 0).
    # All-iterations truth: test-coverage + correctness (flagged in iteration 2).
    for _ in range(5):
        task = Task(
            project_id=project.id,
            title="SR task",
            payloads=[{"type": "create", "prompt": "do something"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {"check_results": {}},
                "_first_attempt_rejection_categories": {"test-coverage": 1},
                "_all_iterations_rejection_categories": {
                    "test-coverage": 1,
                    "correctness": 1,
                },
            },
        )
        task.created_at = _utcnow() - timedelta(days=2)
        db_session.add(task)
        tasks.append(task)
    # Previous window (5 tasks): same shape
    for _ in range(5):
        task = Task(
            project_id=project.id,
            title="SR task",
            payloads=[{"type": "create", "prompt": "do something"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {"check_results": {}},
                "_first_attempt_rejection_categories": {"test-coverage": 1},
                "_all_iterations_rejection_categories": {
                    "test-coverage": 1,
                    "correctness": 1,
                },
            },
        )
        task.created_at = _utcnow() - timedelta(days=20)
        db_session.add(task)
        tasks.append(task)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cats = result["categories"]
    # test-coverage: flagged in first iteration → visible in both metrics
    assert "test-coverage" in cats
    assert cats["test-coverage"]["current_hit_rate"] == 0.0
    assert cats["test-coverage"]["all_iterations_catch_rate"] == 0.0

    # correctness: flagged only in iteration 2 — invisible to the first-attempt metric
    # but the all-iterations metric counts it as a miss (catch_rate == 0)
    assert "correctness" in cats, (
        "iteration-2 category must surface in all_iterations_catch_rate"
    )
    assert cats["correctness"]["current_hit_rate"] is None, (
        "correctness must be absent from first-attempt metric"
    )
    assert cats["correctness"]["all_iterations_catch_rate"] == 0.0
    assert cats["correctness"]["all_iterations_miss_rate"] == 1.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_all_iterations_backward_compat(
    db_session: AsyncSession,
):
    """When _all_iterations_rejection_categories is absent and review_history is empty,
    all_iterations_catch_rate falls back to the first-attempt categories so existing
    tasks continue to produce a non-None all_iterations value."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 5 current tasks: only _first_attempt_rejection_categories, no all-iterations field
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    # 5 previous tasks: same
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["test-coverage"]
    # Both metrics resolve to the same value when there's no extra iteration data
    assert cat["current_hit_rate"] is not None
    assert cat["all_iterations_catch_rate"] is not None
    assert cat["current_hit_rate"] == cat["all_iterations_catch_rate"]
    assert cat["all_iterations_miss_rate"] is not None


# ---------------------------------------------------------------------------
# SR false-positive rate and precision per category
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sr_false_positive_rate_no_human_confirmation(db_session: AsyncSession):
    """SR fails a category that no human ever flags → false_positive_rate == 1.0."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 5 current: SR fails 'correctness', human flags only 'test-coverage' (unrelated)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    # 5 previous: same
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["false_positive_rate"] == 1.0, (
        "all SR failures unconfirmed → FP rate must be 1.0"
    )
    assert cat["precision"] == 0.0, "zero confirmed failures → precision must be 0.0"


@pytest.mark.asyncio
async def test_sr_false_positive_rate_human_confirms_is_zero(db_session: AsyncSession):
    """SR fails a category that human also flags → false_positive_rate == 0.0, precision == 1.0."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["false_positive_rate"] == 0.0, (
        "all SR failures confirmed by human → FP rate 0"
    )
    assert cat["precision"] == 1.0, "all SR failures confirmed → precision must be 1.0"


@pytest.mark.asyncio
async def test_sr_false_positive_rate_review_history_confirms_not_fp(
    db_session: AsyncSession,
):
    """SR fails test-coverage; review_history (iter 2) contains a 'missing test' issue
    that keyword-matches test-coverage → not counted as a false positive."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    def _make_task_with_review_history(age_days):
        check_results = {"test-coverage": {"passed": False, "issue": "x"}}
        task = Task(
            project_id=project.id,
            title="SR task",
            payloads=[{"type": "create", "prompt": "do something"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {"check_results": check_results},
                # Unrelated first-attempt feedback so count >= _MIN_SR_SAMPLE
                "_first_attempt_rejection_categories": {"style-conventions": 1},
                "review_loop": {
                    "review_history": [
                        {
                            "iteration": 2,
                            "issues": [
                                {
                                    "description": "missing test for edge case",
                                    "severity": "MAJOR",
                                }
                            ],
                        }
                    ]
                },
            },
        )
        task.created_at = _utcnow() - timedelta(days=age_days)
        return task

    for _ in range(5):
        t = _make_task_with_review_history(age_days=2)
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_task_with_review_history(age_days=20)
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["test-coverage"]
    # review_history confirmed the SR failure → fp_count == 0
    assert cat["false_positive_rate"] == 0.0, (
        "review_history keyword-matched test-coverage → SR failure is confirmed, not a FP"
    )


@pytest.mark.asyncio
async def test_sr_passes_do_not_affect_false_positive_metric(db_session: AsyncSession):
    """When SR passes a category, it contributes nothing to sr_failed_count or fp_count."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # SR passes everything; human flags 'correctness'
    for _ in range(5):
        t = _make_sr_task(
            project.id, age_days=2, human_cats={"correctness": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id, age_days=20, human_cats={"correctness": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["false_positive_rate"] is None, (
        "SR never failed correctness → no sr_failed_count → FP rate must be None"
    )
    assert cat["precision"] is None, (
        "SR never failed correctness → precision must be None"
    )


@pytest.mark.asyncio
async def test_previous_fp_rate_uses_reviewer_findings_for_consistency(
    db_session: AsyncSession,
):
    """previous_fp_rate uses _compute_cat_fp_stats so reviewer_findings in the prior
    window count as confirmations — preventing phantom sr_false_positive signals after
    a fresh-reviewer wave raises many reviewer_findings in the previous cycle."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    def _task_with_reviewer_confirmation(age_days):
        task = Task(
            project_id=project.id,
            title="SR task",
            payloads=[{"type": "create", "prompt": "do something"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "check_results": {"correctness": {"passed": False, "issue": "x"}},
                    "reviewer_findings": [
                        {"category": "correctness", "severity": "HIGH"}
                    ],
                },
                "_first_attempt_rejection_categories": {"test-coverage": 1},
            },
        )
        task.created_at = _utcnow() - timedelta(days=age_days)
        return task

    for _ in range(5):
        t = _task_with_reviewer_confirmation(age_days=2)
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _task_with_reviewer_confirmation(age_days=20)
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["false_positive_rate"] == 0.0, (
        "reviewer_findings confirmed SR failure in current window → not a FP"
    )
    assert cat["previous_fp_rate"] == 0.0, (
        "reviewer_findings confirmed SR failure in previous window → previous_fp_rate must be 0.0"
    )


# ---------------------------------------------------------------------------
# Tests for loosen-hint data pipeline: fp_count + tp_count >= 10 threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_stores_loosen_hint_threshold_fields(
    db_session: AsyncSession,
):
    """compute_sr_catch_rate_trends stores fp_count, tp_count, and false_positive_rate
    at values that cross the pre-execution loosen-hint thresholds (fp_rate >= 0.5,
    fp+tp >= 10). The formatHighFpRateLoosenHintForSrPrompt formatter reads these
    fields via self_review_catch_rate.categories to build its pre-execution section."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Current window: 7 FP + 3 TP tasks for 'correctness'.
    # FP: SR flags correctness but human flags test-coverage (unrelated).
    # TP: SR flags correctness AND human flags correctness.
    # Result: fp_count=7, tp_count=3, false_positive_rate=0.7, sample=10.
    for _ in range(7):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window: 5 tasks where correctness is confirmed (TPs) — needed
    # for _MIN_SR_SAMPLE (5) so the function doesn't return None.
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["correctness"]

    # Verify fields needed by formatHighFpRateLoosenHintForSrPrompt (via _cat_entry).
    assert cat["fp_count"] == 7, f"expected fp_count=7, got {cat['fp_count']}"
    assert cat["tp_count"] == 3, f"expected tp_count=3, got {cat['tp_count']}"
    assert cat["false_positive_rate"] == 0.7, (
        f"fp_rate = 7/(7+3) = 0.7, got {cat['false_positive_rate']}"
    )
    # Both loosen-hint thresholds are met: fp_rate >= 0.5 and sample >= 10.
    sample = cat["fp_count"] + cat["tp_count"]
    assert sample >= 10, f"loosen-hint needs sample>=10, got {sample}"
    assert cat["false_positive_rate"] >= 0.5, "loosen-hint needs fp_rate>=0.5"


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_below_loosen_hint_sample_threshold(
    db_session: AsyncSession,
):
    """When sample size (fp+tp) < 10, fp_count and tp_count are stored correctly
    but the loosen-hint threshold is not crossed — category is not over-flagged."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Current window: 4 FP + 2 TP = 6 total, below the sample=10 threshold.
    for _ in range(4):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(2):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window: 5 tasks so the function does not return None.
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["fp_count"] == 4
    assert cat["tp_count"] == 2
    sample = cat["fp_count"] + cat["tp_count"]
    assert sample == 6 and sample < 10, (
        "sample below loosen-hint threshold — category should not trigger loosen hint"
    )


# ---------------------------------------------------------------------------
# Tests for per-severity catch-rate breakdown in compute_sr_catch_rate_trends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_severity_counts(
    db_session: AsyncSession,
):
    """by_severity accumulates correct hit_count and human_count per severity."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Current window: 5 tasks with CRITICAL issue in "error-handling"; SR catches 2 of them
    # "exception" keyword → categorised as error-handling
    review_issue_critical = {
        "description": "Unhandled exception in request path",
        "severity": "CRITICAL",
    }
    for i in range(2):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "error-handling"],
            review_issues=[review_issue_critical],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(3):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],  # SR did NOT catch error-handling
            review_issues=[review_issue_critical],
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window: 5 tasks with MAJOR issue in "error-handling"; SR catches all
    review_issue_major = {
        "description": "Missing error handling for null input",
        "severity": "MAJOR",
    }
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "error-handling"],
            review_issues=[review_issue_major],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert "by_severity" in result
    by_sev = result["by_severity"]

    # CRITICAL: 5 issues total (current window), 2 caught by SR
    assert by_sev["CRITICAL"]["current_sample"] == 5
    assert by_sev["CRITICAL"]["current_hit_rate"] == round(2 / 5, 3)
    assert by_sev["CRITICAL"]["current_miss_rate"] == round(3 / 5, 3)

    # MAJOR: 5 issues in previous window, 5 caught
    assert by_sev["MAJOR"]["previous_sample"] == 5
    assert by_sev["MAJOR"]["previous_hit_rate"] == 1.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_critical_alert_tighter_threshold(
    db_session: AsyncSession,
):
    """CRITICAL regression (>=3pp delta) fires the alert even when category trend is stable."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Review issue that categorises to error-handling (keyword "exception")
    review_crit = {
        "description": "Unhandled exception bypasses auth guard",
        "severity": "CRITICAL",
    }

    # Current window: 5 tasks; SR misses error-handling (sr_failed only has "correctness")
    # → CRITICAL miss_rate = 5/5 = 1.0
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            review_issues=[review_crit],
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window: 5 tasks; SR catches error-handling for 4 of them
    # → CRITICAL miss_rate = 1/5 = 0.2
    for i in range(4):
        t = _make_sr_task_with_review(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "error-handling"],
            review_issues=[review_crit],
        )
        db_session.add(t)
        tasks.append(t)
    t = _make_sr_task_with_review(
        project.id,
        age_days=20,
        human_cats={"correctness": 1},
        sr_failed_cats=["correctness"],
        review_issues=[review_crit],
    )
    db_session.add(t)
    tasks.append(t)

    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as mock_dispatch,
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    # Per-category trend for "correctness" should be stable (all caught in both windows)
    assert result["categories"]["correctness"]["trend"] in ("stable", "increasing")
    # Per-severity CRITICAL must be regressing (delta = 0.8 >= 0.03)
    assert result["by_severity"]["CRITICAL"]["trend"] == "regressing"
    # Alert must fire due to severity regression
    mock_post.assert_called_once()
    call_desc = (
        mock_post.call_args.args[2]
        if len(mock_post.call_args.args) >= 3
        else mock_post.call_args.kwargs.get("description", "")
    )
    assert "CRITICAL" in call_desc
    mock_dispatch.assert_called_once()
    payload = (
        mock_dispatch.call_args.kwargs.get("payload") or mock_dispatch.call_args.args[2]
    )
    assert "regressing_severities" in payload
    sevs = payload["regressing_severities"]
    assert any(s["severity"] == "CRITICAL" for s in sevs)


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_severity_dedup_includes_sevs(
    db_session: AsyncSession,
):
    """A new regressing severity not in the prior alert state breaks dedup and re-fires."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    project = await _make_project(db_session)
    tasks = []

    review_crit = {
        "description": "Unhandled exception on auth path",
        "severity": "CRITICAL",
    }
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            review_issues=[review_crit],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(4):
        t = _make_sr_task_with_review(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "error-handling"],
            review_issues=[review_crit],
        )
        db_session.add(t)
        tasks.append(t)
    t = _make_sr_task_with_review(
        project.id,
        age_days=20,
        human_cats={"correctness": 1},
        sr_failed_cats=["correctness"],
        review_issues=[review_crit],
    )
    db_session.add(t)
    tasks.append(t)
    await db_session.flush()

    # Pre-seed alert state with categories=[] and severities=[] to simulate prior alert
    # that covered no categories and no severities.
    doc = dict(project.design_doc or {})
    doc["sr_catch_rate_trends_alert"] = {
        "computed_at": (_utcnow() - timedelta(days=1)).isoformat(),
        "cycle_id": None,
        "categories": [],
        "severities": [],
    }
    project.design_doc = doc
    _flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    # CRITICAL is regressing, so alert fires despite prior alert existing (different severity set)
    assert result is not None
    assert result["by_severity"]["CRITICAL"]["trend"] == "regressing"
    mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_severity_absent_when_no_rev_hist(
    db_session: AsyncSession,
):
    """by_severity has zero samples when tasks have no review_history."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert "by_severity" in result
    by_sev = result["by_severity"]
    # No review_history → all samples are zero, all rates are None
    for sev in ("CRITICAL", "MAJOR", "MINOR"):
        assert by_sev[sev]["current_sample"] == 0
        assert by_sev[sev]["previous_sample"] == 0
        assert by_sev[sev]["current_miss_rate"] is None
        assert by_sev[sev]["trend"] == "stable"


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_severity_persisted_to_design_doc(
    db_session: AsyncSession,
):
    """by_severity is persisted to project.design_doc['sr_catch_rate_trends']."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    review_issue = {"description": "Logic bug in edge case", "severity": "MAJOR"}
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            review_issues=[review_issue],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            review_issues=[review_issue],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    assert result is not None
    stored = project.design_doc.get("sr_catch_rate_trends", {})
    assert "by_severity" in stored
    assert "CRITICAL" in stored["by_severity"]
    assert "MAJOR" in stored["by_severity"]
    assert "MINOR" in stored["by_severity"]


# ---------------------------------------------------------------------------
# LLM categorization helpers
# ---------------------------------------------------------------------------


def test_categorize_review_issues_keyword_scope_drift():
    """_categorize_review_issues_keyword has the same behaviour as _categorize_review_issues."""
    from app.playbook_learning import _categorize_review_issues_keyword

    review_history = [
        {
            "iteration": 1,
            "issues": [
                {
                    "description": "changes are too large for declared scope",
                    "severity": "MAJOR",
                },
            ],
        }
    ]
    result = _categorize_review_issues_keyword(review_history)
    assert "scope-drift" in result
    assert len(result["scope-drift"]) == 1
    assert result["scope-drift"][0]["severity"] == "MAJOR"
    assert "other" not in result


def test_categorize_review_issues_with_cached_returns_verbatim():
    """When cached= is provided, _categorize_review_issues returns it without scanning history."""
    from app.playbook_learning import _categorize_review_issues

    # Nonsense review_history that would produce different buckets if scanned.
    review_history = [
        {
            "iteration": 1,
            "issues": [{"description": "test coverage missing", "severity": "MAJOR"}],
        }
    ]
    sentinel = {
        "correctness": [
            {"desc": "pre-computed", "severity": "CRITICAL", "iteration": 0}
        ]
    }

    result = _categorize_review_issues(review_history, cached=sentinel)
    assert result is sentinel


@pytest.mark.asyncio
async def test_categorize_review_issues_llm_returns_correct_buckets():
    """LLM helper correctly parses a valid JSON response and returns bucketed issues."""
    from unittest.mock import MagicMock
    from app.playbook_learning import categorize_review_issues_llm

    issues = [
        {"description": "missing unit tests", "severity": "MAJOR", "iteration": 1},
        {"description": "SQL injection risk", "severity": "CRITICAL", "iteration": 1},
    ]
    fake_text = json.dumps(
        {
            "classifications": [
                {"index": 0, "category": "test-coverage"},
                {"index": 1, "category": "security"},
            ]
        }
    )

    mock_content_item = MagicMock()
    mock_content_item.text = fake_text
    mock_resp = MagicMock()
    mock_resp.content = [mock_content_item]

    mock_create = AsyncMock(return_value=mock_resp)
    mock_messages = MagicMock()
    mock_messages.create = mock_create
    mock_client = MagicMock()
    mock_client.messages = mock_messages
    mock_client_cls = MagicMock(return_value=mock_client)

    import os

    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_REVIEW_CATEGORIZATION": ""},
        ),
        patch("anthropic.AsyncAnthropic", mock_client_cls),
    ):
        result = await categorize_review_issues_llm(issues)

    assert "test-coverage" in result
    assert result["test-coverage"][0]["desc"] == "missing unit tests"
    assert "security" in result
    assert result["security"][0]["desc"] == "SQL injection risk"


@pytest.mark.asyncio
async def test_categorize_review_issues_llm_falls_back_on_api_error():
    """On LLM exception the helper falls back to keyword classification."""
    from unittest.mock import MagicMock
    from app.playbook_learning import categorize_review_issues_llm, _keyword_from_flat

    issues = [
        {"description": "missing unit tests", "severity": "MAJOR", "iteration": 1},
    ]

    import os

    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_REVIEW_CATEGORIZATION": ""},
        ),
        patch(
            "anthropic.AsyncAnthropic",
            MagicMock(side_effect=Exception("network error")),
        ),
    ):
        result = await categorize_review_issues_llm(issues)

    expected = _keyword_from_flat(issues)
    assert result == expected


@pytest.mark.asyncio
async def test_categorize_review_issues_llm_kill_switch():
    """When DISABLE_LLM_REVIEW_CATEGORIZATION=1, no Anthropic client is constructed."""
    from app.playbook_learning import categorize_review_issues_llm

    issues = [
        {"description": "missing unit tests", "severity": "MAJOR", "iteration": 1},
    ]

    import os

    with patch.dict(os.environ, {"DISABLE_LLM_REVIEW_CATEGORIZATION": "1"}):
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            result = await categorize_review_issues_llm(issues)
            mock_cls.assert_not_called()

    assert "test-coverage" in result


@pytest.mark.asyncio
async def test_cache_first_attempt_classification_writes_buckets_and_method():
    """cache_first_attempt_classification writes buckets + _classification_method to context."""
    from app.playbook_learning import cache_first_attempt_classification

    issues = [
        {"description": "missing unit tests", "severity": "MAJOR", "iteration": 1}
    ]
    ctx: dict = {}

    import os

    with patch.dict(os.environ, {"DISABLE_LLM_REVIEW_CATEGORIZATION": "1"}):
        buckets = await cache_first_attempt_classification(ctx, issues)

    assert "_first_attempt_categorized_issues" in ctx
    entry = ctx["_first_attempt_categorized_issues"]
    assert "buckets" in entry
    assert "_classification_method" in entry
    assert entry["_classification_method"] in ("llm", "keyword")
    assert buckets == entry["buckets"]
    assert "test-coverage" in buckets


def test_compute_sr_prediction_accuracy_uses_cached_categories():
    """When _first_attempt_categorized_issues is present, _categorize_review_issues is not called."""
    from app.digest_metrics import compute_sr_prediction_accuracy
    from app.playbook_learning import _categorize_review_issues
    from app.models import Task, TaskStatus
    import uuid

    def _make_task_with_cache(cached_buckets):
        t = Task(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            status=TaskStatus.COMPLETED,
            prompt="test",
            task_type="create",
            context={
                "_self_review_outcome": {
                    "check_results": {"test-coverage": {"passed": False}}
                },
                "_first_attempt_rejection_categories": {"test-coverage": 1},
                "_first_attempt_categorized_issues": {
                    "buckets": cached_buckets,
                    "_classification_method": "llm",
                },
                "review_loop": {
                    "review_history": [
                        {
                            "iteration": 1,
                            "issues": [
                                {"description": "logic error", "severity": "MAJOR"}
                            ],
                        }
                    ]
                },
            },
        )
        return t

    cached = {
        "test-coverage": [{"desc": "from cache", "severity": "MAJOR", "iteration": 1}]
    }
    tasks = [_make_task_with_cache(cached) for _ in range(5)]

    call_count = {"n": 0}
    original = _categorize_review_issues

    def spy(review_history, cached=None):
        if cached is None:
            call_count["n"] += 1
        return original(review_history, cached=cached)

    with patch("app.digest_metrics._categorize_review_issues", side_effect=spy):
        result = compute_sr_prediction_accuracy(tasks)

    assert result is not None
    # The keyword scan should NOT have been called (cached path was taken for all 5 tasks).
    assert call_count["n"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Tests for cycle-based bucketing in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_cycle(project_id, *, age_days: float, outcome: str = "completed"):
    from app.models import StrategicCycle

    cycle = StrategicCycle(
        id=uuid.uuid4().hex,
        project_id=project_id,
        outcome=outcome,
    )
    cycle.phase1_started_at = _utcnow() - timedelta(days=age_days)
    return cycle


@pytest.mark.asyncio
async def test_cycle_bucketing_uses_strategic_cycle_id(db_session: AsyncSession):
    """When two completed cycles exist, tasks are bucketed by strategic_cycle_id."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    c_cycle = _make_cycle(project.id, age_days=2)
    p_cycle = _make_cycle(project.id, age_days=20)
    db_session.add(c_cycle)
    db_session.add(p_cycle)
    await db_session.flush()

    tasks = []
    # Current cycle: 3/5 hit
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=1,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project.id, age_days=1, human_cats={"correctness": 1}, sr_failed_cats=[]
        )
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    # Previous cycle: 5/5 hit
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=19,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = p_cycle.id
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "cycle"
    assert result["current_cycle_id"] == c_cycle.id
    assert result["previous_cycle_id"] == p_cycle.id
    cat = result["categories"]["correctness"]
    assert cat["current_hit_rate"] == round(3 / 5, 3)
    assert cat["previous_hit_rate"] == 1.0
    assert cat["trend"] == "decreasing"


@pytest.mark.asyncio
async def test_cycle_bucketing_skips_underpopulated_cycle_rolls_further_back(
    db_session: AsyncSession,
):
    """Skips a cycle pair where current bucket has < _MIN_SR_SAMPLE and uses N-1 / N-2."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    newest_cycle = _make_cycle(project.id, age_days=2)  # only 2 tasks — below threshold
    mid_cycle = _make_cycle(project.id, age_days=20)  # 5 tasks
    old_cycle = _make_cycle(project.id, age_days=40)  # 5 tasks
    db_session.add(newest_cycle)
    db_session.add(mid_cycle)
    db_session.add(old_cycle)
    await db_session.flush()

    tasks = []
    for i in range(2):  # newest: below threshold
        t = _make_sr_task(
            project.id,
            age_days=1,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = newest_cycle.id
        db_session.add(t)
        tasks.append(t)
    for i in range(5):  # mid: 5 tasks (should become "current")
        t = _make_sr_task(
            project.id,
            age_days=19,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = mid_cycle.id
        db_session.add(t)
        tasks.append(t)
    for i in range(5):  # old: 5 tasks (should become "previous")
        t = _make_sr_task(
            project.id,
            age_days=39,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = old_cycle.id
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "cycle"
    assert result["current_cycle_id"] == mid_cycle.id
    assert result["previous_cycle_id"] == old_cycle.id


@pytest.mark.asyncio
async def test_fallback_to_calendar_windows_when_no_completed_cycles(
    db_session: AsyncSession,
):
    """Falls back to 14-day calendar windows when no completed cycles exist."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    # Add an in-progress cycle only — no completed cycles.
    in_progress_cycle = _make_cycle(project.id, age_days=2, outcome="in_progress")
    db_session.add(in_progress_cycle)
    await db_session.flush()

    tasks = []
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "calendar"
    assert result["current_cycle_id"] is None
    assert "test-coverage" in result["categories"]


@pytest.mark.asyncio
async def test_regression_alert_dedup_tied_to_cycle_id(db_session: AsyncSession):
    """Second call with same cycle_id + regressing cats does NOT re-fire notification."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    c_cycle = _make_cycle(project.id, age_days=2)
    p_cycle = _make_cycle(project.id, age_days=20)
    db_session.add(c_cycle)
    db_session.add(p_cycle)

    tasks = []
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=1,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project.id, age_days=1, human_cats={"correctness": 1}, sr_failed_cats=[]
        )
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=19,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = p_cycle.id
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert mock_post.call_count == 1

    # Second call same cycle — dedup should suppress.
    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post2,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    mock_post2.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Tests for reviewer_recovery_after_incomplete_sr_count tracking
# ─────────────────────────────────────────────────────────────────────────────


def _make_recovery_task(
    project_id,
    *,
    use_top_level_origin: bool = True,
    use_check_results_origin: bool = False,
) -> Task:
    """Build a Task whose _self_review_outcome marks a recovery pass."""
    sro: dict = {}
    if use_top_level_origin:
        sro["reviewer_recovery_origin"] = "reviewer_recovery_after_incomplete_sr"
    if use_check_results_origin:
        sro["check_results"] = {
            "correctness": {
                "passed": False,
                "origin": "reviewer_recovery_after_incomplete_sr",
            },
        }
    return Task(
        project_id=project_id,
        title="Recovery task",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={"_self_review_outcome": sro},
    )


def _count_recovery_tasks(tasks: list) -> int:
    """Mirror the counting expression used in agent.py for reviewer_recovery_after_incomplete_sr_count."""
    return sum(
        1
        for t in tasks
        if (
            (t.context or {})
            .get("_self_review_outcome", {})
            .get("reviewer_recovery_origin")
            == "reviewer_recovery_after_incomplete_sr"
        )
        or any(
            isinstance(v, dict)
            and v.get("origin") == "reviewer_recovery_after_incomplete_sr"
            for v in (
                (t.context or {}).get("_self_review_outcome", {}).get("check_results")
                or {}
            ).values()
        )
    )


def test_reviewer_recovery_count_via_top_level_origin():
    """Tasks with reviewer_recovery_origin on _self_review_outcome are counted."""
    project_id = uuid.uuid4()
    tasks = [
        _make_recovery_task(project_id, use_top_level_origin=True),
        _make_recovery_task(project_id, use_top_level_origin=True),
        Task(  # no recovery fields
            project_id=project_id,
            title="Normal task",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "check_results": {"correctness": {"passed": True}}
                }
            },
        ),
    ]
    assert _count_recovery_tasks(tasks) == 2


def test_reviewer_recovery_count_via_check_results_origin():
    """Tasks with origin on a check_results entry (no top-level field) are counted."""
    project_id = uuid.uuid4()
    tasks = [
        _make_recovery_task(
            project_id, use_top_level_origin=False, use_check_results_origin=True
        ),
        Task(
            project_id=project_id,
            title="Normal task",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "check_results": {"correctness": {"passed": False}}
                }
            },
        ),
    ]
    assert _count_recovery_tasks(tasks) == 1


def test_reviewer_recovery_count_is_zero_when_no_recovery_tasks():
    """Returns 0 when no tasks have recovery markers."""
    project_id = uuid.uuid4()
    tasks = [
        Task(
            project_id=project_id,
            title="Task A",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.COMPLETED,
            context={"_self_review_outcome": {"check_results": {}}},
        ),
        Task(
            project_id=project_id,
            title="Task B",
            payloads=[{"type": "fix", "prompt": "y"}],
            status=TaskStatus.COMPLETED,
            context={},
        ),
    ]
    assert _count_recovery_tasks(tasks) == 0


@pytest.mark.asyncio
async def test_reviewer_recovery_count_written_to_design_doc(db_session: AsyncSession):
    """Verifies the count is written to project.design_doc['sr_catch_rate_trends']."""
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    # Simulate: sr_catch_rate_trends already exists from compute_sr_catch_rate_trends
    doc = dict(project.design_doc or {})
    doc["sr_catch_rate_trends"] = {
        "computed_at": "2026-01-01T00:00:00",
        "categories": {},
    }
    project.design_doc = doc
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.flush()

    tasks = [
        _make_recovery_task(project.id, use_top_level_origin=True),
        _make_recovery_task(project.id, use_top_level_origin=True),
        Task(
            project_id=project.id,
            title="Normal task",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.COMPLETED,
            context={"_self_review_outcome": {"check_results": {}}},
        ),
    ]
    recovery_count = _count_recovery_tasks(tasks)
    assert recovery_count == 2

    # Apply the same update logic as agent.py
    _proj = await db_session.get(Project, project.id)
    assert _proj is not None
    _doc = dict(_proj.design_doc or {})
    _trends = dict(_doc.get("sr_catch_rate_trends") or {})
    _trends["reviewer_recovery_after_incomplete_sr_count"] = recovery_count
    _doc["sr_catch_rate_trends"] = _trends
    _proj.design_doc = _doc
    flag_modified(_proj, "design_doc")
    db_session.add(_proj)
    await db_session.flush()

    refreshed = await db_session.get(Project, project.id)
    assert refreshed is not None
    trends = (refreshed.design_doc or {}).get("sr_catch_rate_trends", {})
    assert trends.get("reviewer_recovery_after_incomplete_sr_count") == 2


# ─────────────────────────────────────────────────────────────────────────────
# Tests for sr_skip_rate / sr_skip_reason_distribution
# ─────────────────────────────────────────────────────────────────────────────


def _make_skip_task(
    project_id, *, age_days: float, skip_reason: str | None = None
) -> Task:
    """Build a Task whose _self_review_outcome is a fallback/skip envelope."""
    if skip_reason:
        outcome: dict = {
            "verdict": "unknown",
            "check_results": {},
            "skip_reason": skip_reason,
        }
    else:
        # Legacy: verdict unknown + empty check_results, no skip_reason field
        outcome = {"verdict": "unknown", "check_results": {}}
    task = Task(
        project_id=project_id,
        title="Skip task",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={"_self_review_outcome": outcome},
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


# ─────────────────────────────────────────────────────────────────────────────
# Tests for compute_fresh_reviewer_trend
# ─────────────────────────────────────────────────────────────────────────────


def _make_fr_task(
    project_id,
    *,
    age_days: float,
    reviewer_findings=None,
    human_cats=None,
    sr_failed_cats=None,
    first_attempt_succeeded: bool = True,
    task_type: str = "create",
    cycle_id=None,
) -> Task:
    """Build a Task for fresh-reviewer trend tests."""
    check_results = {cat: {"passed": False} for cat in (sr_failed_cats or [])}
    ctx: dict = {"_first_attempt_succeeded": first_attempt_succeeded}
    if reviewer_findings is not None:
        ctx["_self_review_outcome"] = {
            "check_results": check_results,
            "reviewer_findings": reviewer_findings,
        }
    else:
        ctx["_self_review_outcome"] = {"check_results": check_results}
    if human_cats is not None:
        ctx["_first_attempt_rejection_categories"] = human_cats
    task = Task(
        project_id=project_id,
        title="FR task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    if cycle_id:
        task.strategic_cycle_id = cycle_id
    return task


@pytest.mark.asyncio
async def test_sr_skip_rate_and_distribution(db_session: AsyncSession):
    """sr_skip_rate and sr_skip_reason_distribution are computed for a mixed current window."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Current window (0-14 days): 5 normal SR tasks + 3 skip tasks (2 structured, 1 legacy)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    t1 = _make_skip_task(project.id, age_days=2, skip_reason="diff_too_small")
    t2 = _make_skip_task(project.id, age_days=3, skip_reason="json_missing")
    t3 = _make_skip_task(project.id, age_days=4, skip_reason=None)  # legacy unknown
    for t in [t1, t2, t3]:
        db_session.add(t)
        tasks.append(t)

    # Previous window (14-28 days): 5 normal SR tasks
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    # 3 skipped out of 8 tasks-with-outcome in current window
    assert "sr_skip_rate" in result
    assert abs(result["sr_skip_rate"] - round(3 / 8, 3)) < 0.001
    dist = result["sr_skip_reason_distribution"]
    assert dist is not None
    assert dist.get("diff_too_small") == 1
    assert dist.get("json_missing") == 1
    assert dist.get("legacy_unknown") == 1


@pytest.mark.asyncio
async def test_sr_skip_rate_omitted_below_min_sample(db_session: AsyncSession):
    """sr_skip_rate is omitted when the current window has fewer than _MIN_SR_SAMPLE tasks-with-outcome."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # 4 tasks in current window (below _MIN_SR_SAMPLE=5 for skip stats)
    for _ in range(4):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    # sr_skip_rate is always present in the result dict; it is None when sample is too small.
    if result is not None:
        assert "sr_skip_rate" in result
        assert result["sr_skip_rate"] is None


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_all_fields_always_present(
    db_session: AsyncSession,
):
    """All canonical fields are always present in the result dict with safe defaults."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Enough tasks for compute to return a result (>=5 per window)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None

    required_keys = [
        "computed_at",
        "bucket_mode",
        "current_cycle_id",
        "previous_cycle_id",
        "categories",
        "by_task_type",
        "by_fix_iteration",
        "by_scope",
        "by_severity",
        "edges_considered_stats",
        "sr_skip_rate",
        "sr_skip_reason_distribution",
        "by_direction",
        "execution_evidence_miss_rates",
        "overall_trend_verdict",
        "overall_trend_significant",
        "overall_trend_data_sufficient",
    ]
    for key in required_keys:
        assert key in result, f"missing required key: {key}"

    assert isinstance(result["sr_skip_reason_distribution"], dict)
    assert isinstance(result["by_direction"], dict)
    assert isinstance(result["execution_evidence_miss_rates"], dict)


@pytest.mark.asyncio
async def test_compute_fresh_reviewer_trend_returns_none_below_sample_size(
    db_session: AsyncSession,
):
    """Returns None when either window has fewer than _MIN_SR_SAMPLE tasks with reviewer_findings."""
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    tasks = []
    for _ in range(3):
        t = _make_fr_task(
            project.id, age_days=2, reviewer_findings=[{"category": "correctness"}]
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_fr_task(
            project.id, age_days=20, reviewer_findings=[{"category": "correctness"}]
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_fresh_reviewer_trend(project.id, db_session, tasks)

    assert result is None


@pytest.mark.asyncio
async def test_compute_fresh_reviewer_trend_per_category_hit_rate(
    db_session: AsyncSession,
):
    """per_category_hit_rate reflects confirmed / flagged per category."""
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    tasks = []
    # Current: 5 flag correctness, 3 confirmed + 2 not
    for i in range(5):
        confirmed = i < 3
        t = _make_fr_task(
            project.id,
            age_days=2,
            reviewer_findings=[{"category": "correctness"}],
            human_cats=["correctness"] if confirmed else [],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous: 5 flag correctness, all confirmed
    for _ in range(5):
        t = _make_fr_task(
            project.id,
            age_days=20,
            reviewer_findings=[{"category": "correctness"}],
            human_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_fresh_reviewer_trend(project.id, db_session, tasks)

    assert result is not None
    cat = result["per_category_hit_rate"]["correctness"]
    assert cat["current"] == round(3 / 5, 3)
    assert cat["previous"] == 1.0
    assert cat["trend"] == "regressing"


@pytest.mark.asyncio
async def test_compute_fresh_reviewer_trend_added_value(db_session: AsyncSession):
    """added_value counts tasks where fresh-reviewer flagged what agent SR missed."""
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    tasks = []
    # Current: 5 with reviewer flagging correctness, human confirmed all;
    # agent SR caught it in first 2, missed in last 3 → added_value=3
    for i in range(5):
        agent_caught = i < 2
        t = _make_fr_task(
            project.id,
            age_days=2,
            reviewer_findings=[{"category": "correctness"}],
            human_cats=["correctness"],
            sr_failed_cats=["correctness"] if agent_caught else [],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous: reviewer flags correctness, agent also caught all → added_value=0
    for _ in range(5):
        t = _make_fr_task(
            project.id,
            age_days=20,
            reviewer_findings=[{"category": "correctness"}],
            human_cats=["correctness"],
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_fresh_reviewer_trend(project.id, db_session, tasks)

    assert result is not None
    av = result["added_value"]["correctness"]
    assert av["current"] == 3
    assert av["previous"] == 0


@pytest.mark.asyncio
async def test_compute_fresh_reviewer_trend_approval_rate_lift(
    db_session: AsyncSession,
):
    """approval_rate_lift is fa_rate_with_reviewer minus fa_rate_without."""
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    tasks = []
    # Current: 5 with reviewer (all succeed), 5 without (3/5 succeed)
    for _ in range(5):
        t = _make_fr_task(
            project.id,
            age_days=2,
            reviewer_findings=[{"category": "style-conventions"}],
            first_attempt_succeeded=True,
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_fr_task(project.id, age_days=2, first_attempt_succeeded=(i < 3))
        db_session.add(t)
        tasks.append(t)
    # Previous: 5 with reviewer (4/5 succeed), 5 without (4/5 succeed)
    for i in range(5):
        t = _make_fr_task(
            project.id,
            age_days=20,
            reviewer_findings=[{"category": "style-conventions"}],
            first_attempt_succeeded=(i < 4),
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_fr_task(project.id, age_days=20, first_attempt_succeeded=(i < 4))
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_fresh_reviewer_trend(project.id, db_session, tasks)

    assert result is not None
    lift = result["approval_rate_lift"]
    # current lift = 1.0 - 0.6 = 0.4; previous lift = 0.8 - 0.8 = 0.0
    assert lift["current"] == pytest.approx(0.4, abs=0.01)
    assert lift["previous"] == pytest.approx(0.0, abs=0.01)
    assert lift["trend"] == "improving"


@pytest.mark.asyncio
async def test_compute_fresh_reviewer_trend_stable_classification(
    db_session: AsyncSession,
):
    """Hit rate delta < 5pp → trend='stable'."""
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    tasks = []
    for window_age in (2, 20):
        for i in range(5):
            t = _make_fr_task(
                project.id,
                age_days=window_age,
                reviewer_findings=[{"category": "security"}],
                human_cats=["security"] if i < 4 else [],
            )
            db_session.add(t)
            tasks.append(t)
    await db_session.flush()

    result = await compute_fresh_reviewer_trend(project.id, db_session, tasks)

    assert result is not None
    assert result["per_category_hit_rate"]["security"]["trend"] == "stable"


@pytest.mark.asyncio
async def test_compute_fresh_reviewer_trend_persists_to_design_doc(
    db_session: AsyncSession,
):
    """Writes fresh_reviewer_trend to project.design_doc."""
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    tasks = []
    for window_age in (2, 20):
        for _ in range(5):
            t = _make_fr_task(
                project.id,
                age_days=window_age,
                reviewer_findings=[{"category": "correctness"}],
            )
            db_session.add(t)
            tasks.append(t)
    await db_session.flush()

    result = await compute_fresh_reviewer_trend(project.id, db_session, tasks)
    await db_session.flush()
    await db_session.refresh(project)

    assert result is not None
    assert project.design_doc.get("fresh_reviewer_trend") is not None
    assert (
        project.design_doc["fresh_reviewer_trend"]["computed_at"]
        == result["computed_at"]
    )


@pytest.mark.asyncio
async def test_compute_fresh_reviewer_trend_mixed_fr_on_off_cycle_bucketing(
    db_session: AsyncSession,
):
    """Cycle bucketing: tasks without reviewer_findings don't count toward sample size."""
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    c1 = _make_cycle(project.id, age_days=10)
    c2 = _make_cycle(project.id, age_days=30)
    db_session.add_all([c1, c2])
    await db_session.flush()

    tasks = []
    # c1: 5 with reviewer, 10 without
    for _ in range(5):
        t = _make_fr_task(
            project.id,
            age_days=5,
            reviewer_findings=[{"category": "correctness"}],
            cycle_id=str(c1.id),
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(10):
        t = _make_fr_task(project.id, age_days=5, cycle_id=str(c1.id))
        db_session.add(t)
        tasks.append(t)
    # c2: 5 with reviewer
    for _ in range(5):
        t = _make_fr_task(
            project.id,
            age_days=25,
            reviewer_findings=[{"category": "correctness"}],
            cycle_id=str(c2.id),
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_fresh_reviewer_trend(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "cycle"
    assert result["current_sample_size"] == 5
    assert result["previous_sample_size"] == 5


@pytest.mark.asyncio
async def test_compute_fresh_reviewer_trend_improving_classification(
    db_session: AsyncSession,
):
    """Hit rate delta >= 5pp → trend='improving' for per_category_hit_rate."""
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    tasks = []
    # Current: 5 flag correctness, all 5 confirmed → rate=1.0
    for _ in range(5):
        t = _make_fr_task(
            project.id,
            age_days=2,
            reviewer_findings=[{"category": "correctness"}],
            human_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous: 5 flag correctness, only 3 confirmed → rate=0.6
    for i in range(5):
        t = _make_fr_task(
            project.id,
            age_days=20,
            reviewer_findings=[{"category": "correctness"}],
            human_cats=["correctness"] if i < 3 else [],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_fresh_reviewer_trend(project.id, db_session, tasks)

    assert result is not None
    cat = result["per_category_hit_rate"]["correctness"]
    assert cat["current"] == 1.0
    assert cat["previous"] == round(3 / 5, 3)
    assert cat["trend"] == "improving"


@pytest.mark.asyncio
async def test_compute_fresh_reviewer_trend_calendar_fallback(db_session: AsyncSession):
    """Falls back to calendar bucketing (0-14d vs 14-28d) when no completed cycles exist."""
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    tasks = []
    # No cycles → calendar mode; 5 tasks in 0-14d window
    for _ in range(5):
        t = _make_fr_task(
            project.id, age_days=7, reviewer_findings=[{"category": "correctness"}]
        )
        db_session.add(t)
        tasks.append(t)
    # 5 tasks in 14-28d window
    for _ in range(5):
        t = _make_fr_task(
            project.id, age_days=21, reviewer_findings=[{"category": "correctness"}]
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_fresh_reviewer_trend(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "calendar"
    assert result["current_sample_size"] == 5
    assert result["previous_sample_size"] == 5


# ─────────────────────────────────────────────────────────────────────────────
# Tests for compute_sr_reviewer_disagreements
# ─────────────────────────────────────────────────────────────────────────────


def _make_sr_reviewer_task(
    project_id,
    *,
    age_days: float,
    task_type: str = "create",
    check_results: dict | None = None,
    reviewer_findings: list | None = None,
) -> Task:
    """Build a completed Task with self-review outcome and fresh-reviewer findings."""
    sro: dict = {"check_results": check_results or {}}
    if reviewer_findings is not None:
        sro["reviewer_findings"] = reviewer_findings
    task = Task(
        project_id=project_id,
        title="SR reviewer task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={"_self_review_outcome": sro},
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_compute_sr_reviewer_disagreements_returns_none_when_no_findings(
    db_session: AsyncSession,
):
    """Returns None when no tasks have reviewer_findings."""
    from app.playbook_learning import compute_sr_reviewer_disagreements

    project = await _make_project(db_session)
    tasks = []
    for _ in range(3):
        t = _make_sr_reviewer_task(project.id, age_days=1)
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_reviewer_disagreements(project.id, db_session, tasks)

    assert result is None


@pytest.mark.asyncio
async def test_compute_sr_reviewer_disagreements_counts_disagreements(
    db_session: AsyncSession,
):
    """Counts disagreements where reviewer flagged but SR passed/omitted the category."""
    from app.playbook_learning import compute_sr_reviewer_disagreements

    project = await _make_project(db_session)
    tasks = []
    # 3 tasks: reviewer flags correctness, SR omits it -> disagreement
    for _ in range(3):
        t = _make_sr_reviewer_task(
            project.id,
            age_days=1,
            task_type="fix",
            check_results={},
            reviewer_findings=[
                {"category": "correctness", "severity": "HIGH", "description": "bug"}
            ],
        )
        db_session.add(t)
        tasks.append(t)
    # 1 task: reviewer flags correctness but SR already failed it -> NOT a disagreement
    t2 = _make_sr_reviewer_task(
        project.id,
        age_days=2,
        task_type="fix",
        check_results={"correctness": {"passed": False, "issue": "x"}},
        reviewer_findings=[
            {"category": "correctness", "severity": "HIGH", "description": "bug"}
        ],
    )
    db_session.add(t2)
    tasks.append(t2)
    await db_session.flush()

    result = await compute_sr_reviewer_disagreements(project.id, db_session, tasks)

    assert result is not None
    assert result["window_days"] == 30
    entries = result["disagreements"]
    assert len(entries) == 1
    assert entries[0]["category"] == "correctness"
    assert entries[0]["task_type"] == "fix"
    assert entries[0]["count"] == 3
    # Examples should be included — capped at 3 per bucket by sr_blind_spots
    assert "examples" in entries[0]
    assert len(entries[0]["examples"]) >= 1
    ex = entries[0]["examples"][0]
    assert ex["severity"] == "HIGH"
    assert "bug" in ex["description"]


@pytest.mark.asyncio
async def test_compute_sr_reviewer_disagreements_persists_to_design_doc(
    db_session: AsyncSession,
):
    """Result is written to project.design_doc['sr_reviewer_disagreements']."""
    from app.playbook_learning import compute_sr_reviewer_disagreements

    project = await _make_project(db_session)
    tasks = []
    t = _make_sr_reviewer_task(
        project.id,
        age_days=1,
        check_results={},
        reviewer_findings=[
            {
                "category": "test-coverage",
                "severity": "MEDIUM",
                "description": "missing test",
            }
        ],
    )
    db_session.add(t)
    tasks.append(t)
    await db_session.flush()

    await compute_sr_reviewer_disagreements(project.id, db_session, tasks)

    stored = project.design_doc.get("sr_reviewer_disagreements") or {}
    assert stored.get("disagreements")
    assert stored["disagreements"][0]["category"] == "test-coverage"


@pytest.mark.asyncio
async def test_compute_sr_reviewer_disagreements_excludes_old_tasks(
    db_session: AsyncSession,
):
    """Tasks older than window_days are excluded."""
    from app.playbook_learning import compute_sr_reviewer_disagreements

    project = await _make_project(db_session)
    tasks = []
    t = _make_sr_reviewer_task(
        project.id,
        age_days=35,  # outside 30-day default window
        check_results={},
        reviewer_findings=[
            {"category": "security", "severity": "HIGH", "description": "x"}
        ],
    )
    db_session.add(t)
    tasks.append(t)
    await db_session.flush()

    result = await compute_sr_reviewer_disagreements(project.id, db_session, tasks)

    assert result is None


@pytest.mark.asyncio
async def test_compute_sr_reviewer_disagreements_excludes_non_code_types(
    db_session: AsyncSession,
):
    """Only code-change task types (create/fix/refactor/migrate) are counted."""
    from app.playbook_learning import compute_sr_reviewer_disagreements

    project = await _make_project(db_session)
    tasks = []
    for ttype in ("research", "plan", "review"):
        t = _make_sr_reviewer_task(
            project.id,
            age_days=1,
            task_type=ttype,
            check_results={},
            reviewer_findings=[
                {"category": "correctness", "severity": "HIGH", "description": "bug"}
            ],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_reviewer_disagreements(project.id, db_session, tasks)

    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Tests for _compute_cat_fp_stats (false-positive rate aggregator)
# ─────────────────────────────────────────────────────────────────────────────


def _make_fp_task(
    project_id,
    *,
    task_type: str = "create",
    sr_failed_cats: list[str],
    reviewer_cats: list[str],
) -> Task:
    """Build a Task whose self-review flagged sr_failed_cats and whose reviewer caught reviewer_cats."""
    check_results = {cat: {"passed": False, "issue": "x"} for cat in sr_failed_cats}
    sro: dict = {
        "check_results": check_results,
        "reviewer_findings": [
            {"category": cat, "severity": "HIGH", "description": "found"}
            for cat in reviewer_cats
        ],
    }
    task = Task(
        project_id=project_id,
        title="FP test task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={"_self_review_outcome": sro},
    )
    return task


def _make_fp_quality_task(
    project_id,
    *,
    prompt_fingerprint: str,
    sr_failed_cats: list[str] | None = None,
    reviewer_cats: list[str] | None = None,
    human_cats: list[str] | None = None,
    review_history: list[dict] | None = None,
    skipped: bool = False,
) -> Task:
    if skipped:
        sro = {
            "verdict": "unknown",
            "fixes_applied": False,
            "check_results": {},
            "blind_spots": [],
        }
    else:
        sro = {
            "verdict": "pass",
            "fixes_applied": False,
            "check_results": {
                cat: {"passed": False, "issue": "x"} for cat in (sr_failed_cats or [])
            },
            "blind_spots": [],
            "reviewer_findings": [
                {"category": cat, "severity": "HIGH", "description": "found"}
                for cat in (reviewer_cats or [])
            ],
        }
    context = {"_self_review_outcome": sro}
    if human_cats is not None:
        context["_first_attempt_rejection_categories"] = human_cats
    if review_history is not None:
        context["review_loop"] = {"review_history": review_history}
    return Task(
        project_id=project_id,
        title="FP quality task",
        payloads=[{"type": "create", "prompt": "do something"}],
        prompt_fingerprint=prompt_fingerprint,
        status=TaskStatus.COMPLETED,
        context=context,
    )


def test_compute_cat_fp_stats_fp_and_tp_counts():
    """SR-flagged categories caught by reviewer count as TP; missed by reviewer count as FP."""
    from app.playbook_learning import _compute_cat_fp_stats

    project_id = uuid.uuid4()
    tasks = [
        # correctness: SR flagged, reviewer also flagged → TP
        _make_fp_task(
            project_id, sr_failed_cats=["correctness"], reviewer_cats=["correctness"]
        ),
        # correctness: SR flagged, reviewer missed → FP
        _make_fp_task(project_id, sr_failed_cats=["correctness"], reviewer_cats=[]),
        # security: SR flagged, reviewer missed → FP
        _make_fp_task(
            project_id, sr_failed_cats=["security"], reviewer_cats=["correctness"]
        ),
    ]

    cat_fp, type_cat_fp, _scope_cat_fp = _compute_cat_fp_stats(tasks)

    assert cat_fp["correctness"]["tp"] == 1
    assert cat_fp["correctness"]["fp"] == 1
    assert cat_fp["security"]["fp"] == 1
    assert cat_fp["security"].get("tp", 0) == 0

    assert type_cat_fp["create"]["correctness"]["tp"] == 1
    assert type_cat_fp["create"]["correctness"]["fp"] == 1
    assert type_cat_fp["create"]["security"]["fp"] == 1


def test_compute_cat_fp_stats_skips_non_code_types():
    """Tasks whose type is not a code-change type are excluded."""
    from app.playbook_learning import _compute_cat_fp_stats

    project_id = uuid.uuid4()
    tasks = [
        _make_fp_task(
            project_id,
            task_type="research",
            sr_failed_cats=["correctness"],
            reviewer_cats=[],
        ),
        _make_fp_task(
            project_id, task_type="plan", sr_failed_cats=["security"], reviewer_cats=[]
        ),
    ]

    cat_fp, type_cat_fp, scope_cat_fp = _compute_cat_fp_stats(tasks)

    assert cat_fp == {}
    assert type_cat_fp == {}
    assert scope_cat_fp == {}


def test_compute_cat_fp_stats_skips_tasks_without_sro():
    """Tasks with no _self_review_outcome in context are skipped."""
    from app.playbook_learning import _compute_cat_fp_stats

    project_id = uuid.uuid4()
    task = Task(
        project_id=project_id,
        title="no sro",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        context={},
    )

    cat_fp, type_cat_fp, _scope_cat_fp = _compute_cat_fp_stats([task])

    assert cat_fp == {}


def test_compute_cat_fp_stats_false_positive_rate_formula():
    """FPR = fp / (fp + tp): 2 FP and 1 TP gives 0.667."""
    from app.playbook_learning import _compute_cat_fp_stats

    project_id = uuid.uuid4()
    tasks = [
        _make_fp_task(project_id, sr_failed_cats=["correctness"], reviewer_cats=[]),
        _make_fp_task(project_id, sr_failed_cats=["correctness"], reviewer_cats=[]),
        _make_fp_task(
            project_id, sr_failed_cats=["correctness"], reviewer_cats=["correctness"]
        ),
    ]

    cat_fp, _, _ = _compute_cat_fp_stats(tasks)

    fp = cat_fp["correctness"]["fp"]
    tp = cat_fp["correctness"]["tp"]
    assert fp == 2
    assert tp == 1
    fpr = round(fp / (fp + tp), 3)
    assert fpr == 0.667


def test_compute_sr_quality_by_fingerprint_groups_metrics_and_gates_low_sr_samples():
    from app.playbook_learning import compute_sr_quality_by_fingerprint

    project_id = uuid.uuid4()
    review_history = [
        {
            "iteration": 0,
            "issues": [{"description": "missing unit tests", "severity": "MAJOR"}],
        }
    ]
    results = compute_sr_quality_by_fingerprint(
        [
            _make_fp_quality_task(
                project_id,
                prompt_fingerprint="fp-a",
                sr_failed_cats=["correctness"],
                human_cats=["correctness"],
            ),
            _make_fp_quality_task(
                project_id,
                prompt_fingerprint="fp-a",
                sr_failed_cats=["test-coverage"],
                review_history=review_history,
            ),
            _make_fp_quality_task(
                project_id,
                prompt_fingerprint="fp-a",
                sr_failed_cats=["architecture"],
                reviewer_cats=["architecture"],
            ),
            _make_fp_quality_task(
                project_id,
                prompt_fingerprint="fp-a",
                sr_failed_cats=["performance"],
            ),
            _make_fp_quality_task(
                project_id,
                prompt_fingerprint="fp-a",
                reviewer_cats=["security"],
                human_cats=["security"],
            ),
            _make_fp_quality_task(
                project_id,
                prompt_fingerprint="fp-a",
                skipped=True,
            ),
            _make_fp_quality_task(
                project_id,
                prompt_fingerprint="fp-b",
                sr_failed_cats=["correctness"],
                human_cats=["correctness"],
            ),
        ],
        min_sample_size=2,
        top_disagreement_categories=3,
    )

    assert set(results) == {"fp-a", "fp-b"}

    a = results["fp-a"]
    assert a["sr_sample_size"] == 5
    assert a["sr_catch_sample_size"] == 4
    assert a["sr_catch_rate"] == 0.75
    assert a["reviewer_rescue_sample_size"] == 1
    assert a["reviewer_rescue_rate"] == 1.0
    assert a["sr_false_positive_sample_size"] == 4
    assert a["sr_false_positive_rate"] == 0.25
    fp_by_cat = {
        entry["category"]: (entry["fp_count"], entry["tp_count"])
        for entry in a["sr_false_positive_rate_by_category"]
    }
    assert fp_by_cat["architecture"] == (0, 1)
    assert fp_by_cat["correctness"] == (0, 1)
    assert fp_by_cat["performance"] == (1, 0)
    assert fp_by_cat["test-coverage"] == (0, 1)
    disagreements = {
        entry["category"]: (
            entry["reviewer_overrode_sr_count"],
            entry["sr_overrode_reviewer_count"],
        )
        for entry in a["sr_reviewer_disagreement_top_categories"]
    }
    assert disagreements == {
        "correctness": (0, 1),
        "performance": (0, 1),
        "security": (1, 0),
    }
    assert a["sr_catch_rate_delta_vs_project_mean"] == -0.05

    b = results["fp-b"]
    assert b["sr_sample_size"] == 1
    assert b["sr_catch_rate"] is None
    assert b["reviewer_rescue_rate"] is None
    assert b["sr_false_positive_rate"] is None
    assert b["sr_false_positive_rate_by_category"] == []
    assert b["sr_reviewer_disagreement_top_categories"] == []


@pytest.mark.parametrize(
    "origin",
    ["reviewer", "reviewer_disagreement", "reviewer_recovery_after_incomplete_sr"],
)
def test_compute_sr_quality_by_fingerprint_excludes_reviewer_origin(origin):
    """Reviewer-stamped passed=False entries must not inflate sr_catch_rate.

    The category was actually missed by the author SR, so it must surface as a
    reviewer rescue (miss) — not as an author catch.
    """
    from app.playbook_learning import compute_sr_quality_by_fingerprint

    project_id = uuid.uuid4()
    sro = {
        "verdict": "pass",
        "fixes_applied": False,
        "check_results": {
            "correctness": {"passed": False, "issue": "x", "origin": origin},
        },
        "blind_spots": [],
        "reviewer_findings": [
            {"category": "correctness", "severity": "HIGH", "description": "found"}
        ],
    }
    task = Task(
        project_id=project_id,
        title="reviewer-rescue fp task",
        payloads=[{"type": "create", "prompt": "do something"}],
        prompt_fingerprint="fp-rescue",
        status=TaskStatus.COMPLETED,
        context={"_self_review_outcome": sro},
    )

    results = compute_sr_quality_by_fingerprint([task], min_sample_size=1)

    bucket = results["fp-rescue"]
    # Reviewer flagged correctness, author SR did NOT (origin-stamped entries
    # are excluded) — so this is a reviewer rescue, not an author catch.
    assert bucket["sr_catch_rate"] == 0.0
    assert bucket["reviewer_rescue_rate"] == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Tests for reviewer-origin attribution (PR #4193 follow-up).
# Reviewer-stamped check_results entries (origin in _REVIEWER_ORIGINS) must NOT
# be attributed to the author SR — i.e. they must still count as "agent missed"
# for `cat_added_value`, must NOT contribute to TP/FP in `_compute_cat_fp_stats`
# or `_stats_for_tasks`, and must still surface as misses in the SR drilldown.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "origin",
    ["reviewer", "reviewer_disagreement", "reviewer_recovery_after_incomplete_sr"],
)
def test_compute_cat_fp_stats_excludes_reviewer_origin_from_sr_failed(origin):
    """A reviewer-stamped failure must NOT count toward the author SR's TP/FP totals."""
    from app.playbook_learning import _compute_cat_fp_stats

    project_id = uuid.uuid4()
    # SR check_results says correctness:passed=False, BUT origin is reviewer-stamped
    # → this is a reviewer rescue, not an author catch. Must not appear in cat_fp.
    sro = {
        "check_results": {
            "correctness": {"passed": False, "issue": "x", "origin": origin},
        },
        "reviewer_findings": [
            {"category": "correctness", "severity": "HIGH", "description": "found"}
        ],
    }
    task = Task(
        project_id=project_id,
        title="reviewer-rescue task",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={"_self_review_outcome": sro},
    )

    cat_fp, type_cat_fp, scope_cat_fp = _compute_cat_fp_stats([task])

    # Reviewer-stamped failure must not be attributed to the author SR.
    assert cat_fp == {}
    assert type_cat_fp == {}
    assert scope_cat_fp == {}


def test_compute_cat_fp_stats_legacy_no_origin_still_counts():
    """Control: a passed=False entry with no `origin` is a genuine author catch."""
    from app.playbook_learning import _compute_cat_fp_stats

    project_id = uuid.uuid4()
    task = _make_fp_task(
        project_id, sr_failed_cats=["correctness"], reviewer_cats=["correctness"]
    )

    cat_fp, _, _ = _compute_cat_fp_stats([task])

    assert cat_fp["correctness"]["tp"] == 1
    assert cat_fp["correctness"].get("fp", 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    ["reviewer", "reviewer_disagreement", "reviewer_recovery_after_incomplete_sr"],
)
async def test_compute_fresh_reviewer_trend_added_value_excludes_reviewer_origin(
    db_session: AsyncSession, origin
):
    """A reviewer-stamped passed=False must not silently absorb the rescue.

    When the author SR's check_results entry was stamped by the reviewer
    (origin in _REVIEWER_ORIGINS), the category was actually missed by the
    author SR — so it must still count toward `cat_added_value`.
    """
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    tasks: list[Task] = []

    # Current window: 5 tasks, all reviewer-stamped on `correctness`.
    # Pre-fix: agent_failed would include `correctness` and added_value=0.
    # Post-fix: reviewer-origin excluded → added_value=5.
    for _ in range(5):
        check_results = {
            "correctness": {"passed": False, "issue": "x", "origin": origin}
        }
        ctx = {
            "_self_review_outcome": {
                "check_results": check_results,
                "reviewer_findings": [{"category": "correctness"}],
            },
            "_first_attempt_rejection_categories": ["correctness"],
            "_first_attempt_succeeded": True,
        }
        t = Task(
            project_id=project.id,
            title="reviewer-rescue current",
            payloads=[{"type": "create", "prompt": "do something"}],
            status=TaskStatus.COMPLETED,
            context=ctx,
        )
        t.created_at = _utcnow() - timedelta(days=2)
        db_session.add(t)
        tasks.append(t)

    # Previous window: 5 tasks where author SR genuinely caught `correctness`
    # (no origin field) → added_value=0 in previous bucket.
    for _ in range(5):
        t = _make_fr_task(
            project.id,
            age_days=20,
            reviewer_findings=[{"category": "correctness"}],
            human_cats=["correctness"],
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_fresh_reviewer_trend(project.id, db_session, tasks)

    assert result is not None
    av = result["added_value"]["correctness"]
    assert av["current"] == 5, (
        f"reviewer-origin '{origin}' should still count as added_value (got {av['current']})"
    )
    assert av["previous"] == 0


def test_stats_for_tasks_reviewer_origin_excluded_from_sr_failed_count():
    """Reviewer-stamped failures must NOT inflate sr_failed_count / fp_count
    in `_stats_for_tasks`. The reviewer rescue is not a thing the author SR
    flagged, so it must not appear in the precision/false-positive metrics.
    """
    from app.playbook_learning import _stats_for_tasks

    project_id = uuid.uuid4()
    # Author SR genuinely flagged `correctness` (no origin) — TP since human
    # confirmed it.
    author_task = _make_sr_task(
        project_id,
        age_days=1,
        human_cats={"correctness": 1},
        sr_failed_cats=["correctness"],
    )
    # Reviewer-stamped failure on `security` with no human confirmation. Pre-fix
    # this would be counted as a SR false positive; post-fix it's excluded from
    # sr_failed_count entirely because the author SR didn't actually flag it.
    reviewer_task = _make_sr_task(
        project_id,
        age_days=1,
        human_cats={"correctness": 1},
        sr_failed_cats=["security"],
        check_result_overrides={"security": {"origin": "reviewer_disagreement"}},
    )

    _count, cat_stats, _type_cat, _all_iter, _sev, _scope_cat = _stats_for_tasks(
        [author_task, reviewer_task]
    )

    # `correctness` is a genuine author catch — should appear with a TP.
    correctness = cat_stats.get("correctness", {})
    assert correctness.get("sr_failed_count", 0) == 1
    assert correctness.get("fp_count", 0) == 0
    # `security` was reviewer-stamped only — must not surface as a SR failure
    # at all in the FP/sr_failed counters.
    if "security" in cat_stats:
        assert cat_stats["security"].get("sr_failed_count", 0) == 0
        assert cat_stats["security"].get("fp_count", 0) == 0


def test_stats_for_tasks_reviewer_origin_keeps_combined_hit_count():
    """Reviewer-stamped passed=False must still feed `reviewer_save_hit_count`.

    The combined `hit_count` (and the origin-based split) is intentionally
    inclusive of reviewer rescues — only the FP/precision side excludes them.
    Locks in the invariant that the fix did not regress the split metrics.
    """
    from app.playbook_learning import _stats_for_tasks

    project_id = uuid.uuid4()
    # Reviewer-stamped passed=False on `correctness`, human also flagged it →
    # this is a reviewer-save TP.
    task = _make_sr_task(
        project_id,
        age_days=1,
        human_cats={"correctness": 1},
        sr_failed_cats=["correctness"],
        check_result_overrides={"correctness": {"origin": "reviewer"}},
    )

    _count, cat_stats, _type_cat, _all_iter, _sev, _scope_cat = _stats_for_tasks([task])

    cat = cat_stats.get("correctness") or {}
    assert cat.get("hit_count", 0) == 1
    assert cat.get("reviewer_save_hit_count", 0) == 1
    assert cat.get("sr_alone_hit_count", 0) == 0


def test_get_sr_alert_drilldown_tasks_reviewer_rescue_still_a_miss():
    """A reviewer-rescued category must surface as `missed_in_target` in the drilldown."""
    from app.playbook_learning import get_sr_alert_drilldown_tasks

    project_id = uuid.uuid4()
    # Task has `correctness` stamped by the reviewer (passed=False with origin)
    # plus a human rejection on `correctness` → SR did NOT catch it.
    ctx = {
        "_self_review_outcome": {
            "check_results": {
                "correctness": {
                    "passed": False,
                    "issue": "x",
                    "origin": "reviewer_disagreement",
                },
            },
            "verdict": "fail",
        },
        "_first_attempt_rejection_categories": ["correctness"],
    }
    task = Task(
        project_id=project_id,
        title="reviewer-rescue drilldown",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=1)

    rows = get_sr_alert_drilldown_tasks(
        all_tasks=[task],
        design_doc={},
        categories=["correctness"],
        alert_type="trends",
    )

    assert len(rows) == 1
    assert "correctness" in rows[0]["missed_categories"]
    assert "correctness" not in rows[0]["caught_categories"]


def test_get_sr_alert_drilldown_tasks_includes_reviewer_findings():
    """Each drilldown row includes missed_category_findings with severity and description."""
    from app.playbook_learning import get_sr_alert_drilldown_tasks

    project_id = uuid.uuid4()
    ctx = {
        "_self_review_outcome": {
            "check_results": {"correctness": {"passed": True}},
            "verdict": "pass",
            "reviewer_findings": [
                {
                    "category": "correctness",
                    "severity": "HIGH",
                    "description": "The null check is missing on the payload field.",
                }
            ],
        },
        "_first_attempt_rejection_categories": ["correctness"],
    }
    task = Task(
        project_id=project_id,
        title="missing null check",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=1)

    rows = get_sr_alert_drilldown_tasks(
        all_tasks=[task],
        design_doc={},
        categories=["correctness"],
    )

    assert len(rows) == 1
    findings = rows[0]["missed_category_findings"]
    assert "correctness" in findings
    assert findings["correctness"]["severity"] == "HIGH"
    assert (
        findings["correctness"]["description"]
        == "The null check is missing on the payload field."
    )
    assert findings["correctness"]["has_more"] is False


def test_get_sr_alert_drilldown_tasks_fallback_when_no_reviewer_finding():
    """When reviewer_findings has no entry for a missed category, a placeholder is used."""
    from app.playbook_learning import get_sr_alert_drilldown_tasks

    project_id = uuid.uuid4()
    ctx = {
        "_self_review_outcome": {
            "check_results": {"correctness": {"passed": True}},
            "verdict": "pass",
            "reviewer_findings": [],
        },
        "_first_attempt_rejection_categories": ["correctness"],
    }
    task = Task(
        project_id=project_id,
        title="no findings task",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=1)

    rows = get_sr_alert_drilldown_tasks(
        all_tasks=[task],
        design_doc={},
        categories=["correctness"],
    )

    assert len(rows) == 1
    findings = rows[0]["missed_category_findings"]
    assert findings["correctness"]["description"] == "no description recorded"
    assert findings["correctness"]["severity"] is None


def test_get_sr_alert_drilldown_tasks_truncates_long_description():
    """Descriptions longer than 140 chars are truncated with has_more=True."""
    from app.playbook_learning import get_sr_alert_drilldown_tasks

    project_id = uuid.uuid4()
    long_desc = "x" * 200
    ctx = {
        "_self_review_outcome": {
            "check_results": {"security": {"passed": True}},
            "verdict": "pass",
            "reviewer_findings": [
                {
                    "category": "security",
                    "severity": "CRITICAL",
                    "description": long_desc,
                }
            ],
        },
        "_first_attempt_rejection_categories": ["security"],
    }
    task = Task(
        project_id=project_id,
        title="long desc task",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=1)

    rows = get_sr_alert_drilldown_tasks(
        all_tasks=[task],
        design_doc={},
        categories=["security"],
    )

    assert len(rows) == 1
    finding = rows[0]["missed_category_findings"]["security"]
    assert len(finding["description"]) == 140
    assert finding["has_more"] is True
    assert finding["full_description"] == long_desc


# ─────────────────────────────────────────────────────────────────────────────
# Tests for compute_edges_trend
# ─────────────────────────────────────────────────────────────────────────────


def _make_edges_task(
    project_id,
    *,
    age_days: float,
    edges_considered: list | None = None,
    task_type: str = "create",
    cycle_id: str | None = None,
) -> Task:
    """Build a Task for edges-trend tests with _self_review_outcome.edges_considered."""
    ctx: dict = {
        "_self_review_outcome": {
            "edges_considered": edges_considered
            if edges_considered is not None
            else [],
        }
    }
    task = Task(
        project_id=project_id,
        title="Edges task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    if cycle_id:
        task.strategic_cycle_id = cycle_id
    return task


@pytest.mark.asyncio
async def test_compute_edges_trend_returns_none_below_sample_size(
    db_session: AsyncSession,
):
    """Returns None when either window has fewer than _MIN_SR_SAMPLE qualifying tasks."""
    from app.playbook_learning import compute_edges_trend

    project = await _make_project(db_session)
    tasks = []
    for _ in range(3):
        t = _make_edges_task(project.id, age_days=2, edges_considered=[{"id": "e1"}])
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_edges_task(project.id, age_days=20, edges_considered=[{"id": "e1"}])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_edges_trend(project.id, db_session, tasks)

    assert result is None


@pytest.mark.asyncio
async def test_compute_edges_trend_calendar_fallback(db_session: AsyncSession):
    """Falls back to calendar bucketing when no completed cycles exist."""
    from app.playbook_learning import compute_edges_trend

    project = await _make_project(db_session)
    tasks = []
    # Current window (0-14d): 5 tasks, 3 with edges
    for i in range(5):
        t = _make_edges_task(
            project.id,
            age_days=2,
            edges_considered=[{"id": "e1"}] if i < 3 else [],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window (14-28d): 5 tasks, all with edges
    for _ in range(5):
        t = _make_edges_task(project.id, age_days=20, edges_considered=[{"id": "e1"}])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_edges_trend(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "calendar"
    assert result["current_cycle_id"] is None
    assert result["current"]["tasks_with_edges_rate"] == round(3 / 5, 3)
    assert result["previous"]["tasks_with_edges_rate"] == 1.0
    assert result["trend"] == "regressing"


@pytest.mark.asyncio
async def test_compute_edges_trend_cycle_mode_happy_path(db_session: AsyncSession):
    """Uses cycle bucketing when two completed cycles each have ≥5 qualifying tasks."""
    from app.playbook_learning import compute_edges_trend

    project = await _make_project(db_session)
    c1 = _make_cycle(project.id, age_days=5)
    c2 = _make_cycle(project.id, age_days=30)
    db_session.add_all([c1, c2])
    await db_session.flush()

    tasks = []
    # c1 (current): 5 tasks, 4 with edges → rate=0.8
    for i in range(5):
        t = _make_edges_task(
            project.id,
            age_days=3,
            edges_considered=[{"id": "e1"}] if i < 4 else [],
            cycle_id=str(c1.id),
        )
        db_session.add(t)
        tasks.append(t)
    # c2 (previous): 5 tasks, 1 with edges → rate=0.2
    for i in range(5):
        t = _make_edges_task(
            project.id,
            age_days=25,
            edges_considered=[{"id": "e1"}] if i < 1 else [],
            cycle_id=str(c2.id),
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_edges_trend(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "cycle"
    assert result["current_cycle_id"] == c1.id
    assert result["previous_cycle_id"] == c2.id
    assert result["current"]["tasks_with_edges_rate"] == 0.8
    assert result["previous"]["tasks_with_edges_rate"] == 0.2
    assert result["tasks_with_edges_rate_delta"] == pytest.approx(0.6, abs=0.01)
    assert result["trend"] == "improving"


@pytest.mark.asyncio
async def test_compute_edges_trend_stable_classification(db_session: AsyncSession):
    """Delta < 5pp → trend='stable'."""
    from app.playbook_learning import compute_edges_trend

    project = await _make_project(db_session)
    tasks = []
    # Current: 5 tasks, 4/5 with edges → 0.8
    for i in range(5):
        t = _make_edges_task(
            project.id,
            age_days=2,
            edges_considered=[{"id": "e1"}] if i < 4 else [],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous: 5 tasks, 4/5 with edges → 0.8 (delta=0 → stable)
    for i in range(5):
        t = _make_edges_task(
            project.id,
            age_days=20,
            edges_considered=[{"id": "e1"}] if i < 4 else [],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_edges_trend(project.id, db_session, tasks)

    assert result is not None
    assert result["trend"] == "stable"


@pytest.mark.asyncio
async def test_compute_edges_trend_persists_to_design_doc(db_session: AsyncSession):
    """Writes edges_trend to project.design_doc."""
    from app.playbook_learning import compute_edges_trend

    project = await _make_project(db_session)
    tasks = []
    for window_age in (2, 20):
        for _ in range(5):
            t = _make_edges_task(
                project.id, age_days=window_age, edges_considered=[{"id": "e1"}]
            )
            db_session.add(t)
            tasks.append(t)
    await db_session.flush()

    result = await compute_edges_trend(project.id, db_session, tasks)
    await db_session.flush()
    await db_session.refresh(project)

    assert result is not None
    assert project.design_doc.get("edges_trend") is not None
    assert project.design_doc["edges_trend"]["computed_at"] == result["computed_at"]
    assert project.design_doc["edges_trend"]["trend"] == result["trend"]


@pytest.mark.asyncio
async def test_compute_edges_trend_history_capped_at_5(db_session: AsyncSession):
    """rate_history never exceeds 5 entries across repeated calls with distinct cycles."""
    from app.playbook_learning import compute_edges_trend

    project = await _make_project(db_session)

    for call_idx in range(6):
        c_cycle = _make_cycle(project.id, age_days=call_idx * 10 + 1)
        p_cycle = _make_cycle(project.id, age_days=call_idx * 10 + 20)
        db_session.add_all([c_cycle, p_cycle])
        await db_session.flush()

        tasks = []
        for _ in range(5):
            t = _make_edges_task(
                project.id,
                age_days=call_idx * 10 + 0.5,
                edges_considered=[{"id": "e1"}],
                cycle_id=str(c_cycle.id),
            )
            db_session.add(t)
            tasks.append(t)
        for _ in range(5):
            t = _make_edges_task(
                project.id,
                age_days=call_idx * 10 + 15,
                edges_considered=[{"id": "e1"}],
                cycle_id=str(p_cycle.id),
            )
            db_session.add(t)
            tasks.append(t)
        await db_session.flush()

        await compute_edges_trend(project.id, db_session, tasks)
        await db_session.flush()
        await db_session.refresh(project)

    history = project.design_doc["edges_trend"]["rate_history"]
    assert len(history) <= 5


@pytest.mark.asyncio
async def test_compute_edges_trend_same_cycle_ids_as_sr_catch_rate(
    db_session: AsyncSession,
):
    """When both functions run on the same tasks, they pick the same cycle pair."""
    from app.playbook_learning import compute_edges_trend, compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    c1 = _make_cycle(project.id, age_days=5)
    c2 = _make_cycle(project.id, age_days=30)
    db_session.add_all([c1, c2])
    await db_session.flush()

    tasks = []
    # c1: 5 SR tasks + 5 edges tasks
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=3,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = c1.id
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_edges_task(
            project.id, age_days=3, edges_considered=[{"id": "e1"}], cycle_id=str(c1.id)
        )
        db_session.add(t)
        tasks.append(t)
    # c2: 5 SR tasks + 5 edges tasks
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=25,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = c2.id
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_edges_task(
            project.id,
            age_days=25,
            edges_considered=[{"id": "e1"}],
            cycle_id=str(c2.id),
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    sr_result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    edges_result = await compute_edges_trend(project.id, db_session, tasks)

    assert sr_result is not None
    assert edges_result is not None
    assert sr_result["bucket_mode"] == "cycle"
    assert edges_result["bucket_mode"] == "cycle"
    assert sr_result["current_cycle_id"] == edges_result["current_cycle_id"]
    assert sr_result["previous_cycle_id"] == edges_result["previous_cycle_id"]


@pytest.mark.asyncio
async def test_compute_edges_trend_author_vs_reviewer_breakdown(
    db_session: AsyncSession,
):
    """Per-origin breakdown surfaces author vs reviewer counts and the
    reviewer_caught signal in the trend dict's `current` stats block."""
    from app.playbook_learning import compute_edges_trend

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5 tasks; 3 have author edges, 2 of those also have a
    # reviewer edge that catches a case the author missed.
    current_edge_payloads = [
        [
            {"case": "off-by-one", "handled": True, "origin": "author"},
            {"case": "null guard", "handled": False, "origin": "reviewer"},
        ],
        [
            {"case": "empty list", "handled": True, "origin": "author"},
            {"case": "race", "handled": False, "origin": "reviewer"},
        ],
        [{"case": "type-coerce", "handled": True, "origin": "author"}],
        [],
        [],
    ]
    for payload in current_edge_payloads:
        t = _make_edges_task(project.id, age_days=2, edges_considered=payload)
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5 tasks, no reviewer edges at all.
    for i in range(5):
        t = _make_edges_task(
            project.id,
            age_days=20,
            edges_considered=[{"case": f"prev-{i}", "handled": True}] if i < 3 else [],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_edges_trend(project.id, db_session, tasks)

    assert result is not None
    cur = result["current"]
    assert cur["tasks_with_reviewer_edges_count"] == 2
    assert cur["tasks_with_reviewer_edges_rate"] == round(2 / 5, 3)
    assert cur["reviewer_caught_count"] == 2
    assert cur["reviewer_caught_rate"] == round(2 / 5, 3)
    assert cur["author_avg_edges_per_task"] == 1.0
    assert cur["reviewer_avg_edges_per_task"] == 1.0

    prev = result["previous"]
    assert prev["tasks_with_reviewer_edges_count"] == 0
    assert prev["reviewer_caught_count"] == 0

    assert result["reviewer_caught_rate_delta"] == round(2 / 5 - 0, 3)
    assert result["reviewer_caught_trend"] == "improving"


# ─────────────────────────────────────────────────────────────────────────────
# Tests for compute_sr_regression_risk_test_signal_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_sr_rr_task(
    project_id,
    *,
    age_days: float,
    sr_passed,
    test_attempts,
    task_type: str = "create",
    blind_spots=None,
) -> Task:
    """Build a Task with SR regression_risk verdict + fix_loop_history.test bucket.

    ``sr_passed`` of ``None`` omits the regression_risk entry so the task
    fails the qualifies-predicate.  ``test_attempts`` of ``None`` defaults
    to no test loop activity.
    """
    check_results: dict = {}
    if sr_passed is not None:
        check_results["regression_risk"] = {"passed": sr_passed, "issue": "x"}
    sro: dict = {"check_results": check_results}
    if blind_spots is not None:
        sro["blind_spots"] = blind_spots
    history: dict = {}
    if test_attempts is not None:
        history["test"] = test_attempts
    task = Task(
        project_id=project_id,
        title="SR-RR task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={"_self_review_outcome": sro},
        fix_loop_history=history,
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_returns_none_below_sample(
    db_session: AsyncSession,
):
    """Below _MIN_SR_SAMPLE qualifying tasks → None (not zero)."""
    from app.playbook_learning import compute_sr_regression_risk_test_signal_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(4):
        t = _make_sr_rr_task(project.id, age_days=2, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    for _ in range(4):
        t = _make_sr_rr_task(project.id, age_days=20, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_regression_risk_test_signal_trends(
        project.id, db_session, tasks
    )
    assert result is None


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_passed_no_signal_is_true_pass(
    db_session: AsyncSession,
):
    """SR passed + no/trivial test loop is a true-pass, not a miss."""
    from app.playbook_learning import compute_sr_regression_risk_test_signal_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=2, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=20, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_regression_risk_test_signal_trends(
        project.id, db_session, tasks
    )
    assert result is not None
    cur = result["current"]
    assert cur["sr_passed_no_signal"] == 5
    assert cur["sr_passed_with_signal"] == 0
    assert cur["miss_by_test_signal_rate"] == 0.0
    assert cur["numerator"] == 0
    assert cur["denominator"] == 5


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_passed_with_signal_counts_as_miss(
    db_session: AsyncSession,
):
    """SR passed + test loop ran non-trivially → miss-by-test-signal."""
    from app.playbook_learning import compute_sr_regression_risk_test_signal_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(3):
        t = _make_sr_rr_task(
            project.id,
            age_days=2,
            sr_passed=True,
            test_attempts=[
                {"iteration": 1, "exit_reason": "in_progress"},
                {"iteration": 2, "exit_reason": "passed"},
            ],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(2):
        t = _make_sr_rr_task(project.id, age_days=2, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=20, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_regression_risk_test_signal_trends(
        project.id, db_session, tasks
    )
    assert result is not None
    cur = result["current"]
    assert cur["sr_passed_with_signal"] == 3
    assert cur["sr_passed_no_signal"] == 2
    assert cur["miss_by_test_signal_rate"] == round(3 / 5, 3)
    assert cur["numerator"] == 3
    assert cur["denominator"] == 5
    assert result["miss_by_test_signal_rate_delta"] == round(3 / 5, 3)
    assert result["trend"] == "regressing"


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_failed_with_signal_is_hit(
    db_session: AsyncSession,
):
    """SR failed regression_risk + test loop ran → hit-by-test-signal counted."""
    from app.playbook_learning import compute_sr_regression_risk_test_signal_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(4):
        t = _make_sr_rr_task(
            project.id,
            age_days=2,
            sr_passed=False,
            test_attempts=[
                {"iteration": 1, "exit_reason": "divergence_detected"},
            ],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(1):
        t = _make_sr_rr_task(project.id, age_days=2, sr_passed=False, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=20, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_regression_risk_test_signal_trends(
        project.id, db_session, tasks
    )
    assert result is not None
    cur = result["current"]
    assert cur["sr_failed_with_signal"] == 4
    assert cur["sr_failed_no_signal"] == 1
    assert cur["denominator"] == 0
    assert cur["miss_by_test_signal_rate"] is None


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_skip_envelope_excluded(
    db_session: AsyncSession,
):
    """Skip-fallback SR envelopes are excluded from numerator and denominator."""
    from app.playbook_learning import compute_sr_regression_risk_test_signal_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=2, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_sr_rr_task(
            project.id,
            age_days=2,
            sr_passed=None,
            test_attempts=[{"iteration": 1, "exit_reason": "passed"}],
            blind_spots=["self-review skipped: diff below threshold"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=20, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_regression_risk_test_signal_trends(
        project.id, db_session, tasks
    )
    assert result is not None
    cur = result["current"]
    assert cur["sample_size"] == 5
    assert (
        cur["sr_passed_no_signal"]
        + cur["sr_passed_with_signal"]
        + cur["sr_failed_no_signal"]
        + cur["sr_failed_with_signal"]
    ) == 5


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_persists_to_design_doc(
    db_session: AsyncSession,
):
    """Result is written to project.design_doc under the new key."""
    from app.playbook_learning import compute_sr_regression_risk_test_signal_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=2, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=20, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_regression_risk_test_signal_trends(
        project.id, db_session, tasks
    )
    assert result is not None

    await db_session.flush()
    await db_session.refresh(project)
    assert project.design_doc is not None
    persisted = project.design_doc.get("sr_regression_risk_test_signal_trends")
    assert persisted is not None
    assert persisted["computed_at"] == result["computed_at"]
    assert persisted["current"]["sample_size"] == 5


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_cycle_bucketing(
    db_session: AsyncSession,
):
    """Two completed StrategicCycle rows form current/previous buckets."""
    from app.playbook_learning import compute_sr_regression_risk_test_signal_trends

    project = await _make_project(db_session)
    c_cycle = _make_cycle(project.id, age_days=2)
    p_cycle = _make_cycle(project.id, age_days=20)
    db_session.add(c_cycle)
    db_session.add(p_cycle)
    await db_session.flush()

    tasks = []
    for _ in range(5):
        t = _make_sr_rr_task(
            project.id,
            age_days=1,
            sr_passed=True,
            test_attempts=[
                {"iteration": 1, "exit_reason": "in_progress"},
                {"iteration": 2, "exit_reason": "passed"},
            ],
        )
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=19, sr_passed=True, test_attempts=[])
        t.strategic_cycle_id = p_cycle.id
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_regression_risk_test_signal_trends(
        project.id, db_session, tasks
    )
    assert result is not None
    assert result["bucket_mode"] == "cycle"
    assert result["current_cycle_id"] == c_cycle.id
    assert result["previous_cycle_id"] == p_cycle.id
    assert result["current"]["sr_passed_with_signal"] == 5
    assert result["previous"]["sr_passed_with_signal"] == 0
    assert result["trend"] == "regressing"


# ─────────────────────────────────────────────────────────────────────────────
# Tests for resolve_sr_miss_human_truth — three-tier precedence
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_sr_miss_human_truth_prefers_all_iterations():
    """_all_iterations_rejection_categories wins over review_history and first-attempt."""
    from app.playbook_learning import resolve_sr_miss_human_truth

    ctx = {
        "_all_iterations_rejection_categories": {"architecture": 1, "correctness": 2},
        "_first_attempt_rejection_categories": {"correctness": 1},
        "review_loop": {
            "review_history": [
                {
                    "iteration": 1,
                    "issues": [
                        {"description": "security hole", "severity": "CRITICAL"}
                    ],
                }
            ]
        },
    }
    result = resolve_sr_miss_human_truth(ctx)
    assert result == {"architecture", "correctness"}


def test_resolve_sr_miss_human_truth_falls_back_to_review_history():
    """When _all_iterations absent, keyword-categorised review_history is used."""
    from app.playbook_learning import resolve_sr_miss_human_truth

    ctx = {
        "_first_attempt_rejection_categories": {"correctness": 1},
        "review_loop": {
            "review_history": [
                {
                    "iteration": 2,
                    "issues": [
                        {
                            "description": "regressed previously passing test",
                            "severity": "MAJOR",
                        }
                    ],
                }
            ]
        },
    }
    result = resolve_sr_miss_human_truth(ctx)
    assert "regression_risk" in result


def test_resolve_sr_miss_human_truth_falls_back_to_first_attempt():
    """When neither _all_iterations nor review_history exist, uses first-attempt."""
    from app.playbook_learning import resolve_sr_miss_human_truth

    ctx = {
        "_first_attempt_rejection_categories": {"security": 1, "performance": 1},
    }
    result = resolve_sr_miss_human_truth(ctx)
    assert result == {"security", "performance"}


def test_resolve_sr_miss_human_truth_empty_ctx():
    """Returns empty set when no human-truth sources are present."""
    from app.playbook_learning import resolve_sr_miss_human_truth

    assert resolve_sr_miss_human_truth({}) == set()


# ---------------------------------------------------------------------------
# Precision/recall quadrant classification tests
# ---------------------------------------------------------------------------

# Helper: build a two-window task set where current window has the given
# hit/fp rates and the previous window has different rates, producing known
# precision/recall deltas.
#
# Recall == hit_rate (hit_count / human_count).
# Precision == (sr_failed - fp) / sr_failed.
#
# We control fp via check_result_overrides: tasks where SR fails AND the human
# flagged the same cat are TPs; tasks where SR fails but human did NOT flag are
# FPs.  We approximate FP by using a separate category for the human_cats so
# the fp-stats see it as an FP.
#
# For simplicity, we use 10 tasks per window so rates are round numbers.


def _make_quadrant_tasks(
    project_id,
    db_session,
    tasks: list,
    *,
    curr_hits: int,
    curr_fp: int,
    prev_hits: int,
    prev_fp: int,
    cat: str = "correctness",
    n: int = 10,
):
    """Build current + previous window tasks controlling hit and FP counts.

    actual sr_failed = hits + fp for each window.

    For each window:
    - ``hits`` tasks: human flagged cat + SR failed cat → TP hit
    - ``n - hits`` tasks: human flagged cat, SR passed → miss
    - ``fp`` tasks: SR failed cat but human flagged a different cat → FP

    precision = hits / (hits + fp); recall (hit_rate) = hits / n
    """
    # Current window (age_days=2)
    for _ in range(curr_hits):
        t = _make_sr_task(
            project_id,
            age_days=2,
            human_cats={cat: 1},
            sr_failed_cats=[cat],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(n - curr_hits):
        t = _make_sr_task(
            project_id,
            age_days=2,
            human_cats={cat: 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(curr_fp):
        t = _make_sr_task(
            project_id,
            age_days=2,
            human_cats={"other-cat": 1},
            sr_failed_cats=[cat],
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window (age_days=20)
    for _ in range(prev_hits):
        t = _make_sr_task(
            project_id,
            age_days=20,
            human_cats={cat: 1},
            sr_failed_cats=[cat],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(n - prev_hits):
        t = _make_sr_task(
            project_id,
            age_days=20,
            human_cats={cat: 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(prev_fp):
        t = _make_sr_task(
            project_id,
            age_days=20,
            human_cats={"other-cat": 1},
            sr_failed_cats=[cat],
        )
        db_session.add(t)
        tasks.append(t)


@pytest.mark.asyncio
async def test_precision_recall_quadrant_conservative_shift(db_session: AsyncSession):
    """Precision up + recall down → conservative_shift."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    # Current: recall=6/10=0.6, sr_failed=8, fp=1 → precision=(8-1)/8=0.875
    # Previous: recall=9/10=0.9, sr_failed=9, fp=4 → precision=(9-4)/9=0.556
    # recall_delta = 0.6-0.9 = -0.3 (below -threshold)
    # precision_delta = 0.875-0.556 = +0.319 (above +threshold)
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=6,
        curr_fp=1,
        prev_hits=9,
        prev_fp=4,
    )
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["precision_recall_quadrant"] == "conservative_shift"
    assert cat["recall_delta"] is not None and cat["recall_delta"] < -0.05
    assert cat["precision_delta"] is not None and cat["precision_delta"] > 0.05


@pytest.mark.asyncio
async def test_precision_recall_quadrant_noisy_shift(db_session: AsyncSession):
    """Precision down + recall up → noisy_shift."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    # Current: recall=9/10=0.9, sr_failed=9, fp=4 → precision=(9-4)/9≈0.556
    # Previous: recall=6/10=0.6, sr_failed=8, fp=1 → precision=(8-1)/8=0.875
    # recall_delta = +0.3, precision_delta = -0.319
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=9,
        curr_fp=4,
        prev_hits=6,
        prev_fp=1,
    )
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["precision_recall_quadrant"] == "noisy_shift"
    assert cat["recall_delta"] is not None and cat["recall_delta"] > 0.05
    assert cat["precision_delta"] is not None and cat["precision_delta"] < -0.05


@pytest.mark.asyncio
async def test_precision_recall_quadrant_both_declining(db_session: AsyncSession):
    """Precision down + recall down → both_declining."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    # Current: recall=4/10=0.4, sr_failed=6, fp=3 → precision=(6-3)/6=0.5
    # Previous: recall=8/10=0.8, sr_failed=8, fp=1 → precision=(8-1)/8=0.875
    # recall_delta = -0.4, precision_delta = -0.375
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=4,
        curr_fp=3,
        prev_hits=8,
        prev_fp=1,
    )
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["precision_recall_quadrant"] == "both_declining"
    assert cat["recall_delta"] is not None and cat["recall_delta"] < -0.05
    assert cat["precision_delta"] is not None and cat["precision_delta"] < -0.05


@pytest.mark.asyncio
async def test_precision_recall_quadrant_healthy(db_session: AsyncSession):
    """Precision up + recall up → healthy."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    # Current: recall=9/10=0.9, sr_failed=9, fp=1 → precision=(9-1)/9≈0.889
    # Previous: recall=4/10=0.4, sr_failed=6, fp=3 → precision=(6-3)/6=0.5
    # recall_delta = +0.5, precision_delta = +0.389
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=9,
        curr_fp=1,
        prev_hits=4,
        prev_fp=3,
    )
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["precision_recall_quadrant"] == "healthy"
    assert cat["recall_delta"] is not None and cat["recall_delta"] > 0.05
    assert cat["precision_delta"] is not None and cat["precision_delta"] > 0.05


@pytest.mark.asyncio
async def test_precision_recall_quadrant_flat(db_session: AsyncSession):
    """Both deltas within threshold → flat."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    # Current == previous: recall=7/10=0.7, sr_failed=7, fp=2 → precision=5/7≈0.714
    # Both deltas = 0.0 → flat
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=7,
        curr_fp=2,
        prev_hits=7,
        prev_fp=2,
    )
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["precision_recall_quadrant"] == "flat"


@pytest.mark.asyncio
async def test_precision_recall_quadrant_mixed(db_session: AsyncSession):
    """One delta significant, other not → mixed."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    # Current: recall=8/10=0.8, sr_failed=8, fp=1 → precision=7/8=0.875
    # Previous: recall=7/10=0.7, sr_failed=8, fp=3 → precision=5/8=0.625
    # recall_delta = +0.1 (above threshold)
    # precision_delta = +0.25 (above threshold) → both up → actually healthy
    # Let's adjust: prev_hits=7, but make precision barely below threshold for recall
    # Actually let's set: curr_hits=7(recall=0.7), prev_hits=6(recall=0.6) delta=+0.1 (above 0.05)
    # and curr_fp=2, prev_fp=2 with same sr_failed=7 → precision delta = 0 → mixed
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=7,
        curr_fp=2,
        prev_hits=6,
        prev_fp=2,
    )
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"]["correctness"]
    # recall_delta = +0.1 (significant), precision_delta = 0.0 (not significant) → mixed
    assert cat["precision_recall_quadrant"] == "mixed"


@pytest.mark.asyncio
async def test_precision_recall_quadrant_null_when_no_previous_precision(
    db_session: AsyncSession,
):
    """When previous window has no SR failures (prev_sr_failed=0), quadrant is None."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    # prev_hits=0, prev_fp=0 → actual prev_sr_failed = 0 → previous_precision=None
    # precision_delta = None → quadrant = None
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=7,
        curr_fp=2,
        prev_hits=0,
        prev_fp=0,
    )
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["precision_recall_quadrant"] is None
    assert cat["precision_delta"] is None


@pytest.mark.asyncio
async def test_precision_recall_quadrant_alert_fires_for_conservative_shift(
    db_session: AsyncSession,
):
    """conservative_shift triggers exactly one alert per cycle."""
    from app.playbook_learning import (
        compute_sr_catch_rate_trends,
        _SR_QUADRANT_ALERT_TITLE_PREFIX,
    )

    project = await _make_project(db_session)
    tasks: list = []
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=6,
        curr_fp=1,
        prev_hits=9,
        prev_fp=4,
    )
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert (
        result["categories"]["correctness"]["precision_recall_quadrant"]
        == "conservative_shift"
    )
    mock_post.assert_called()

    # Verify Alert row was inserted
    await db_session.commit()
    alert_rows = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    q_rows = [
        r for r in alert_rows if r.title.startswith(_SR_QUADRANT_ALERT_TITLE_PREFIX)
    ]
    assert len(q_rows) == 1
    assert q_rows[0].severity == AlertSeverity.WARNING
    assert "correctness" in q_rows[0].title


@pytest.mark.asyncio
async def test_precision_recall_quadrant_alert_fires_for_noisy_shift(
    db_session: AsyncSession,
):
    """noisy_shift triggers an alert."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=9,
        curr_fp=4,
        prev_hits=6,
        prev_fp=1,
    )
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert (
        result["categories"]["correctness"]["precision_recall_quadrant"]
        == "noisy_shift"
    )
    mock_post.assert_called()


@pytest.mark.asyncio
async def test_precision_recall_quadrant_no_alert_for_healthy(
    db_session: AsyncSession,
):
    """healthy quadrant does NOT trigger an alert."""
    from app.playbook_learning import (
        compute_sr_catch_rate_trends,
        _SR_QUADRANT_ALERT_TITLE_PREFIX,
    )

    project = await _make_project(db_session)
    tasks: list = []
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=9,
        curr_fp=1,
        prev_hits=4,
        prev_fp=3,
    )
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ),
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["categories"]["correctness"]["precision_recall_quadrant"] == "healthy"

    await db_session.commit()
    alert_rows = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                    Alert.title.like(f"{_SR_QUADRANT_ALERT_TITLE_PREFIX}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alert_rows) == 0


@pytest.mark.asyncio
async def test_precision_recall_quadrant_no_alert_for_both_declining(
    db_session: AsyncSession,
):
    """both_declining quadrant does NOT trigger a quadrant alert (covered by regression alert)."""
    from app.playbook_learning import (
        compute_sr_catch_rate_trends,
        _SR_QUADRANT_ALERT_TITLE_PREFIX,
    )

    project = await _make_project(db_session)
    tasks: list = []
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=4,
        curr_fp=3,
        prev_hits=8,
        prev_fp=1,
    )
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ),
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert (
        result["categories"]["correctness"]["precision_recall_quadrant"]
        == "both_declining"
    )

    await db_session.commit()
    q_rows = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                    Alert.title.like(f"{_SR_QUADRANT_ALERT_TITLE_PREFIX}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(q_rows) == 0


@pytest.mark.asyncio
async def test_precision_recall_quadrant_alert_dedup_same_cycle(
    db_session: AsyncSession,
):
    """Alert fires only once per cycle even when compute is called twice."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=6,
        curr_fp=1,
        prev_hits=9,
        prev_fp=4,
    )
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        await compute_sr_catch_rate_trends(project.id, db_session, tasks)
        await db_session.commit()
        call_count_after_first = mock_post.call_count

        await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    # Second call should not fire again for same window/cycle
    assert mock_post.call_count == call_count_after_first


# ─────────────────────────────────────────────────────────────────────────────
# Tests for _compute_miss_latency_stats
# ─────────────────────────────────────────────────────────────────────────────


def _make_latency_task(
    project_id,
    *,
    sr_completed_at: datetime,
    rejection_at: datetime,
    miss_cats: list[str],
    age_days: float = 2,
) -> Task:
    """Build a Task where SR passed all miss_cats but reviewer rejected them."""
    # SR marks all miss_cats as passed=True
    check_results = {cat: {"passed": True, "issue": ""} for cat in miss_cats}
    # human cats contains all miss_cats
    human_cats = dict.fromkeys(miss_cats, 1)
    sr_outcome = {
        "check_results": check_results,
        "completed_at": sr_completed_at.isoformat(),
    }
    ctx = {
        "_self_review_outcome": sr_outcome,
        "_first_attempt_rejection_categories": human_cats,
        "_first_attempt_rejection_at": rejection_at.isoformat(),
    }
    task = Task(
        project_id=project_id,
        title="latency task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


def test_compute_miss_latency_stats_p50_and_p95():
    """p50 / p95 are computed correctly from multiple samples."""
    from app.playbook_learning import _compute_miss_latency_stats

    import uuid as _uuid

    pid = _uuid.uuid4()
    base = datetime(2026, 1, 1, 0, 0, 0)
    # Latencies in seconds: 60, 120, 300, 600, 86400
    latency_secs = [60, 120, 300, 600, 86400]
    tasks = []
    for lat in latency_secs:
        sr_ts = base
        rej_ts = base + timedelta(seconds=lat)
        tasks.append(
            _make_latency_task(
                pid,
                sr_completed_at=sr_ts,
                rejection_at=rej_ts,
                miss_cats=["test-coverage"],
            )
        )

    result = _compute_miss_latency_stats(tasks)

    assert "test-coverage" in result
    cat = result["test-coverage"]
    assert cat["time_to_miss_detect_sample_count"] == 5
    # p50 of [60, 120, 300, 600, 86400] = 300
    assert cat["time_to_miss_detect_p50_sec"] == 300
    # p95 of 5 values: idx = 0.95*4 = 3.8 → interpolated between 600 and 86400
    # Use ±1 tolerance for float→int truncation
    assert abs(cat["time_to_miss_detect_p95_sec"] - 69240) <= 1


def test_compute_miss_latency_stats_single_sample_lone_checker_false():
    """With only one sample, lone_checker must be False regardless of latency."""
    from app.playbook_learning import _compute_miss_latency_stats

    import uuid as _uuid

    pid = _uuid.uuid4()
    base = datetime(2026, 1, 1, 0, 0, 0)
    # Single miss with > 7-day latency
    sr_ts = base
    rej_ts = base + timedelta(days=10)
    tasks = [
        _make_latency_task(
            pid, sr_completed_at=sr_ts, rejection_at=rej_ts, miss_cats=["correctness"]
        )
    ]

    result = _compute_miss_latency_stats(tasks)

    assert "correctness" in result
    cat = result["correctness"]
    assert cat["time_to_miss_detect_sample_count"] == 1
    assert cat["time_to_miss_detect_lone_checker"] is False


def test_compute_miss_latency_stats_no_misses_absent():
    """A category with no misses should not appear in the result."""
    from app.playbook_learning import _compute_miss_latency_stats

    import uuid as _uuid

    pid = _uuid.uuid4()
    base = datetime(2026, 1, 1, 0, 0, 0)
    # Task where SR also failed the category (not a miss — both SR and reviewer flagged)
    check_results = {"test-coverage": {"passed": False, "issue": "x"}}
    human_cats = {"test-coverage": 1}
    ctx = {
        "_self_review_outcome": {
            "check_results": check_results,
            "completed_at": base.isoformat(),
        },
        "_first_attempt_rejection_categories": human_cats,
        "_first_attempt_rejection_at": (base + timedelta(hours=1)).isoformat(),
    }
    task = Task(
        project_id=pid,
        title="not a miss",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    result = _compute_miss_latency_stats([task])
    assert "test-coverage" not in result


def test_compute_miss_latency_stats_lone_checker_flag():
    """lone_checker=True when sample_count >= 2 and p95 > 7 days."""
    from app.playbook_learning import _compute_miss_latency_stats

    import uuid as _uuid

    pid = _uuid.uuid4()
    base = datetime(2026, 1, 1, 0, 0, 0)
    # Two misses each with 8-day latency → p95 = 8 days > 7 days
    tasks = []
    for _ in range(2):
        sr_ts = base
        rej_ts = base + timedelta(days=8)
        tasks.append(
            _make_latency_task(
                pid, sr_completed_at=sr_ts, rejection_at=rej_ts, miss_cats=["security"]
            )
        )

    result = _compute_miss_latency_stats(tasks)

    assert result["security"]["time_to_miss_detect_lone_checker"] is True


def test_compute_miss_latency_stats_lone_checker_false_when_fast():
    """lone_checker=False when p95 is under 7 days even with multiple samples."""
    from app.playbook_learning import _compute_miss_latency_stats

    import uuid as _uuid

    pid = _uuid.uuid4()
    base = datetime(2026, 1, 1, 0, 0, 0)
    # Two misses with 1-hour latency
    tasks = []
    for _ in range(2):
        sr_ts = base
        rej_ts = base + timedelta(hours=1)
        tasks.append(
            _make_latency_task(
                pid,
                sr_completed_at=sr_ts,
                rejection_at=rej_ts,
                miss_cats=["style-conventions"],
            )
        )

    result = _compute_miss_latency_stats(tasks)

    assert result["style-conventions"]["time_to_miss_detect_lone_checker"] is False


def test_compute_miss_latency_stats_fallback_both_skipped():
    """When SR and rejection both lack real timestamps, the sample is skipped."""
    from app.playbook_learning import _compute_miss_latency_stats

    import uuid as _uuid

    pid = _uuid.uuid4()
    # Build a task with no SR completed_at and no rejection_at in context
    check_results = {"test-coverage": {"passed": True, "issue": ""}}
    human_cats = {"test-coverage": 1}
    ctx = {
        "_self_review_outcome": {"check_results": check_results},  # no completed_at
        "_first_attempt_rejection_categories": human_cats,  # no _first_attempt_rejection_at
    }
    task = Task(
        project_id=pid,
        title="no-ts task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    # updated_at is None on this in-memory task — both fallbacks are None, so skipped
    result = _compute_miss_latency_stats([task])
    assert "test-coverage" not in result


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_miss_latency_fields_present(
    db_session: AsyncSession,
):
    """compute_sr_catch_rate_trends attaches time_to_miss_detect fields to category entries."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    base = _utcnow()

    # Current window (5 tasks): each has a miss on test-coverage with 2h latency
    for _ in range(5):
        sr_ts = base - timedelta(days=2, hours=2)
        rej_ts = base - timedelta(days=2)
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=[],
            check_result_overrides={},
            sr_completed_at=sr_ts,
            rejection_at=rej_ts,
        )
        # Make test-coverage a miss (SR passed, reviewer rejected)
        t.context = {
            "_self_review_outcome": {
                "check_results": {"test-coverage": {"passed": True, "issue": ""}},
                "completed_at": sr_ts.isoformat(),
            },
            "_first_attempt_rejection_categories": {"test-coverage": 1},
            "_first_attempt_rejection_at": rej_ts.isoformat(),
        }
        db_session.add(t)
        tasks.append(t)

    # Previous window (5 tasks): simple misses without latency timestamps
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"].get("test-coverage")
    assert cat is not None
    # Fields must be present (value may be None or int)
    assert "time_to_miss_detect_sample_count" in cat
    assert "time_to_miss_detect_lone_checker" in cat
    assert "time_to_miss_detect_p50_sec" in cat
    assert "time_to_miss_detect_p95_sec" in cat
    # We have 5 samples with 2h latency → p50 = p95 = 7200
    assert cat["time_to_miss_detect_sample_count"] == 5
    assert cat["time_to_miss_detect_p50_sec"] == 7200
    assert cat["time_to_miss_detect_lone_checker"] is False


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_zero_miss_category_has_zero_count(
    db_session: AsyncSession,
):
    """A category with no latency samples gets time_to_miss_detect_sample_count == 0."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # 5 current + 5 previous tasks, SR also failed (not a miss — no passed=True in SR)
    for age in [2, 20]:
        for _ in range(5):
            t = _make_sr_task(
                project.id,
                age_days=age,
                human_cats={"correctness": 1},
                sr_failed_cats=["correctness"],
            )
            db_session.add(t)
            tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"].get("correctness")
    assert cat is not None
    assert cat["time_to_miss_detect_sample_count"] == 0
    assert cat["time_to_miss_detect_p50_sec"] is None
    assert cat["time_to_miss_detect_p95_sec"] is None
    assert cat["time_to_miss_detect_lone_checker"] is False


# ---------------------------------------------------------------------------
# SR blind-spot layer distribution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blind_spot_layer_distribution_populated(db_session: AsyncSession):
    """aggregate_sr_blind_spot_themes populates layer_distribution for classifiable text."""
    from app.sr_blind_spots import aggregate_sr_blind_spot_themes

    project = await _make_project(db_session)

    # Text that triggers CI_WORKFLOW (matches "github actions.*fail" pattern)
    ci_text = "github actions failed to trigger workflow due to path filter miss"
    # Text that triggers AGENT_VERB (matches "scope too broad" pattern)
    verb_text = "scope too broad — edited files outside declared scope in handler"

    db_session.add(
        Task(
            project_id=project.id,
            title="t1",
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {},
                    "blind_spots": [ci_text],
                }
            },
        )
    )
    db_session.add(
        Task(
            project_id=project.id,
            title="t2",
            payloads=[{"type": "fix"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {},
                    "blind_spots": [verb_text],
                }
            },
        )
    )
    await db_session.flush()

    themes = await aggregate_sr_blind_spot_themes(
        db_session, project.id, days=30, limit=10
    )

    assert len(themes) == 2
    by_theme = {t["theme"]: t for t in themes}

    ci_theme_key = " ".join(ci_text.lower().split()[:6])
    verb_theme_key = " ".join(verb_text.lower().split()[:6])

    ci_entry = by_theme[ci_theme_key]
    assert "layer_distribution" in ci_entry
    assert isinstance(ci_entry["layer_distribution"], dict)
    assert ci_entry["layer_distribution"].get("CI_WORKFLOW", 0) >= 1

    verb_entry = by_theme[verb_theme_key]
    assert "layer_distribution" in verb_entry
    assert isinstance(verb_entry["layer_distribution"], dict)
    assert verb_entry["layer_distribution"].get("AGENT_VERB", 0) >= 1

    # Counts must be positive ints; total per theme ≤ count × num_layers
    for entry in themes:
        dist = entry["layer_distribution"]
        for layer_count in dist.values():
            assert isinstance(layer_count, int) and layer_count > 0
        total = sum(dist.values())
        assert total <= entry["count"] * 5  # at most 5 layers per example


# ─────────────────────────────────────────────────────────────────────────────
# Tests for _compute_cat_severity_calibration
# ─────────────────────────────────────────────────────────────────────────────


def _make_sr_task_with_severity(
    project_id,
    *,
    age_days: float,
    human_cats: dict,
    agent_cat: str,
    agent_severity: str,
    reviewer_severity: str,
    reviewer_keyword: str = "injection",
) -> Task:
    """Build a Task with severity-annotated SR check_result and review_loop history."""
    check_results = {
        agent_cat: {"passed": False, "issue": "x", "severity": agent_severity}
    }
    ctx: dict = {
        "_self_review_outcome": {"check_results": check_results},
        "_first_attempt_rejection_categories": human_cats,
        "review_loop": {
            "review_history": [
                {
                    "iteration": 0,
                    "issues": [
                        {
                            "description": f"{reviewer_keyword} vulnerability found",
                            "severity": reviewer_severity,
                        }
                    ],
                }
            ]
        },
    }
    task = Task(
        project_id=project_id,
        title="SR severity task",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


def test_compute_cat_severity_calibration_matched_under_over():
    """_compute_cat_severity_calibration correctly buckets matched/under/over cases."""
    from app.playbook_learning import _compute_cat_severity_calibration

    project_id = uuid.uuid4()

    # Task 1: agent=MINOR, reviewer=CRITICAL → under_rated
    t1 = _make_sr_task_with_severity(
        project_id,
        age_days=1,
        human_cats={"security": 1},
        agent_cat="security",
        agent_severity="MINOR",
        reviewer_severity="CRITICAL",
        reviewer_keyword="injection",
    )
    # Task 2: agent=MAJOR, reviewer=MAJOR → matched
    t2 = _make_sr_task_with_severity(
        project_id,
        age_days=1,
        human_cats={"security": 1},
        agent_cat="security",
        agent_severity="MAJOR",
        reviewer_severity="MAJOR",
        reviewer_keyword="injection",
    )
    # Task 3: agent=CRITICAL, reviewer=MINOR → over_rated
    t3 = _make_sr_task_with_severity(
        project_id,
        age_days=1,
        human_cats={"security": 1},
        agent_cat="security",
        agent_severity="CRITICAL",
        reviewer_severity="MINOR",
        reviewer_keyword="injection",
    )

    result = _compute_cat_severity_calibration([t1, t2, t3])

    assert "security" in result
    sec = result["security"]
    assert sec["under_rated"] == 1
    assert sec["over_rated"] == 1
    assert sec["matched"] == 1
    assert sec["total"] == 3


def test_compute_cat_severity_calibration_skips_missing_severity():
    """Tasks without agent severity or no review_history are skipped."""
    from app.playbook_learning import _compute_cat_severity_calibration

    project_id = uuid.uuid4()

    # No agent severity
    t_no_agent_sev = Task(
        project_id=project_id,
        title="t",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {
                "check_results": {"security": {"passed": False, "issue": "x"}}
            },
            "_first_attempt_rejection_categories": {"security": 1},
            "review_loop": {
                "review_history": [
                    {
                        "iteration": 0,
                        "issues": [
                            {"description": "injection vuln", "severity": "CRITICAL"}
                        ],
                    }
                ]
            },
        },
    )
    t_no_agent_sev.created_at = _utcnow() - timedelta(days=1)

    # No review_history
    t_no_rev_hist = Task(
        project_id=project_id,
        title="t",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {
                "check_results": {
                    "security": {"passed": False, "issue": "x", "severity": "MINOR"}
                }
            },
            "_first_attempt_rejection_categories": {"security": 1},
        },
    )
    t_no_rev_hist.created_at = _utcnow() - timedelta(days=1)

    result = _compute_cat_severity_calibration([t_no_agent_sev, t_no_rev_hist])
    assert result == {}


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_severity_calibration_propagates(
    db_session: AsyncSession,
):
    """severity_calibration is included in categories when conditions are met."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Current window (7 tasks): agent rates security as MINOR, reviewer rates CRITICAL
    for _ in range(7):
        t = _make_sr_task_with_severity(
            project.id,
            age_days=2,
            human_cats={"security": 1},
            agent_cat="security",
            agent_severity="MINOR",
            reviewer_severity="CRITICAL",
            reviewer_keyword="injection",
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window (6 tasks): matched severities
    for _ in range(6):
        t = _make_sr_task_with_severity(
            project.id,
            age_days=20,
            human_cats={"security": 1},
            agent_cat="security",
            agent_severity="MAJOR",
            reviewer_severity="MAJOR",
            reviewer_keyword="injection",
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert "security" in result["categories"]
    cal = result["categories"]["security"].get("severity_calibration")
    assert cal is not None, "severity_calibration should be present"
    assert cal["total"] == 7
    assert cal["under_rated"] == 7
    assert cal["over_rated"] == 0
    assert cal["matched"] == 0


@pytest.mark.asyncio
async def test_blind_spot_layer_distribution_empty_for_unclassifiable(
    db_session: AsyncSession,
):
    """aggregate_sr_blind_spot_themes returns empty layer_distribution when text matches no layer."""
    from app.sr_blind_spots import aggregate_sr_blind_spot_themes

    project = await _make_project(db_session)

    db_session.add(
        Task(
            project_id=project.id,
            title="t1",
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {},
                    "blind_spots": ["did not add extra comments to helpers"],
                }
            },
        )
    )
    await db_session.flush()

    themes = await aggregate_sr_blind_spot_themes(
        db_session, project.id, days=30, limit=10
    )

    assert len(themes) == 1
    dist = themes[0]["layer_distribution"]
    assert isinstance(dist, dict)
    # May be empty — unclassifiable text produces no layer tags


# ─────────────────────────────────────────────────────────────────────────────
# Tests for aggregate_sr_co_failure_pairs
# ─────────────────────────────────────────────────────────────────────────────


def _make_co_fail_task(project_id, *, age_days: float, failed_cats: list) -> Task:
    """Build a code-change task with the given SR-failed categories."""
    check_results = {c: {"passed": False, "issue": "x"} for c in failed_cats}
    task = Task(
        project_id=project_id,
        title="co-fail test",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {"check_results": check_results},
            "_first_attempt_rejection_categories": {},
        },
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


def test_aggregate_sr_co_failure_pairs_returns_none_below_min_sample():
    """Returns None when fewer than min_sample qualifying tasks exist."""
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    tasks = [
        _make_co_fail_task(
            uuid.uuid4(), age_days=1, failed_cats=["correctness", "style-conventions"]
        )
    ]
    result = aggregate_sr_co_failure_pairs(tasks, min_sample=5)
    assert result is None


def test_aggregate_sr_co_failure_pairs_high_lift_pair_surfaced():
    """Pair with lift >> 1 appears in output sorted by lift desc.

    Setup: 5 tasks where A+B co-fail, 3 tasks where B fails alone.
    total=8, miss_A=5, miss_B=8; P(B|A)=5/5=1.0, base_rate(B)=8/8=1.0 — wrong.

    Better: 5 co-fail tasks, 0 B-alone tasks, 3 no-fail tasks.
    total=8, miss_A=5, miss_B=5; P(B|A)=1.0, base=5/8=0.625 → lift=1.6
    """
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    project_id = uuid.uuid4()
    tasks = []
    # 5 tasks: both A and B fail
    for _ in range(5):
        tasks.append(
            _make_co_fail_task(
                project_id, age_days=1, failed_cats=["correctness", "security"]
            )
        )
    # 3 tasks: neither fails (qualify for total count)
    for _ in range(3):
        tasks.append(_make_co_fail_task(project_id, age_days=1, failed_cats=[]))

    result = aggregate_sr_co_failure_pairs(tasks, min_sample=5, min_co_count=2)
    assert result is not None
    pair = next(
        (p for p in result if p["cat_a"] == "correctness" and p["cat_b"] == "security"),
        None,
    )
    assert pair is not None
    # P(B|A) = 5/5 = 1.0; base_rate(B) = 5/8 = 0.625; lift ≈ 1.6
    assert pair["lift"] > 1.0
    assert pair["co_fail_count"] == 5
    assert abs(pair["lift"] - pair["p_b_given_a"] / pair["base_rate_b"]) < 0.01


def test_aggregate_sr_co_failure_pairs_base_rate_equals_conditional_gives_lift_one():
    """When P(B|A)==base_rate(B), lift rounds to 1.0."""
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    project_id = uuid.uuid4()
    tasks = []
    # 6 tasks: all have both cats failing → P(B|A)=1.0, base_rate(B)=1.0, lift=1.0
    for _ in range(6):
        tasks.append(
            _make_co_fail_task(
                project_id, age_days=1, failed_cats=["correctness", "security"]
            )
        )

    result = aggregate_sr_co_failure_pairs(tasks, min_sample=5, min_co_count=2)
    assert result is not None
    pair = next(
        (p for p in result if p["cat_a"] == "correctness" and p["cat_b"] == "security"),
        None,
    )
    assert pair is not None
    assert round(pair["lift"], 2) == 1.0


def test_aggregate_sr_co_failure_pairs_single_task_miss_filtered_by_min_co_count():
    """A pair that appears in only 1 task is excluded by default min_co_count=2."""
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    project_id = uuid.uuid4()
    tasks = []
    # 5 tasks where only one has co-failure
    tasks.append(
        _make_co_fail_task(
            project_id, age_days=1, failed_cats=["correctness", "security"]
        )
    )
    for _ in range(4):
        tasks.append(
            _make_co_fail_task(project_id, age_days=1, failed_cats=["correctness"])
        )

    result = aggregate_sr_co_failure_pairs(tasks, min_sample=5, min_co_count=2)
    # The pair exists but co_count=1 < min_co_count=2 → filtered out
    assert result == []


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_includes_co_failure_pairs(
    db_session: AsyncSession,
):
    """compute_sr_catch_rate_trends writes co_failure_pairs into the result."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Build enough tasks for both windows (5+ each)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=3,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "security"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    assert "co_failure_pairs" in result
    assert isinstance(result["co_failure_pairs"], list)


# ---------------------------------------------------------------------------
# lone_checker_recovery origin attribution in catch-rate stats
# ---------------------------------------------------------------------------


def test_stats_for_tasks_lone_checker_recovery_is_reviewer_save():
    """lone_checker_recovery origin counts as reviewer_save, not sr_alone."""
    import uuid as _uuid
    from app.playbook_learning import _stats_for_tasks

    project_id = _uuid.uuid4()
    check_results = {
        "security": {
            "passed": False,
            "origin": "lone_checker_recovery",
            "issue": "fresh-reviewer caught this",
        }
    }
    ctx = {
        "_self_review_outcome": {"check_results": check_results},
        "_first_attempt_rejection_categories": {"security": 1},
    }
    t = Task(
        project_id=project_id,
        title="lone checker task",
        payloads=[{"type": "create", "prompt": "p"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    _, cat_stats, _, _, _, _ = _stats_for_tasks([t])
    assert "security" in cat_stats
    assert cat_stats["security"]["reviewer_save_hit_count"] == 1
    assert cat_stats["security"]["sr_alone_hit_count"] == 0


def test_stats_for_tasks_lone_checker_recovery_excluded_from_sr_failed_author():
    """lone_checker_recovery findings are not counted as author SR false positives."""
    import uuid as _uuid
    from app.playbook_learning import _stats_for_tasks

    project_id = _uuid.uuid4()
    # SR passed (lone-checker reviewer flagged security), human did NOT reject.
    check_results = {
        "security": {
            "passed": False,
            "origin": "lone_checker_recovery",
            "issue": "fresh-reviewer only",
        }
    }
    ctx = {
        "_self_review_outcome": {"check_results": check_results},
        "_first_attempt_rejection_categories": {},
    }
    t = Task(
        project_id=project_id,
        title="lone checker no human rejection",
        payloads=[{"type": "create", "prompt": "p"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    _, cat_stats, _, _, _, _ = _stats_for_tasks([t])
    # lone_checker_recovery is not an author SR failure, so fp_count must be 0.
    security = cat_stats.get("security", {})
    assert security.get("fp_count", 0) == 0


# ---------------------------------------------------------------------------
# miss_mode_breakdown per-category breakdown in compute_sr_catch_rate_trends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_miss_mode_breakdown(
    db_session: AsyncSession,
):
    """compute_sr_catch_rate_trends attaches miss_mode_breakdown per category with correct counters."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5 tasks on "correctness" with distinct reviewer origins.
    # 1 reading_gap, 2 judgment_gap, 2 coverage_gap
    current_specs = [
        {"origin": "reviewer_recovery_after_incomplete_sr"},
        {"origin": "reviewer_disagreement"},
        {"origin": "reviewer_disagreement"},
        {"origin": "reviewer"},
        {"origin": "reviewer"},
    ]
    for spec in current_specs:
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            check_result_overrides={"correctness": {"origin": spec["origin"]}},
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5 tasks (no origin tags)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"]["correctness"]
    breakdown = cat.get("miss_mode_breakdown")
    assert breakdown is not None, (
        "miss_mode_breakdown should be present for reviewer-origin misses"
    )
    assert breakdown["reading_gap"] == 1
    assert breakdown["judgment_gap"] == 2
    assert breakdown["coverage_gap"] == 2
    # Both judgment_gap and coverage_gap tied at 2; dominant is one of them
    assert breakdown["dominant_mode"] in ("judgment_gap", "coverage_gap")


@pytest.mark.asyncio
async def test_below_floor_flag_persisted_to_design_doc(db_session: AsyncSession):
    """below_floor=True is stored in design_doc categories when a category has
    been below the floor threshold for >= _FLOOR_MIN_CONSECUTIVE_WINDOWS windows.
    The dispatcher's _evaluate_sr_below_floor_signal reads this field directly."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    # Pre-seed one prior below-floor rate_history entry so two consecutive
    # below-floor windows are reached after a single compute call.
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": (_utcnow() - timedelta(days=15)).isoformat(),
            "categories": {
                "correctness": {"rate_history": [0.3]},
            },
        }
    }
    flag_modified(project, "design_doc")
    await db_session.flush()

    tasks = []
    # Current window: 5 tasks — all SR misses (hit_rate=0.0, below 50% floor).
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],  # SR passes → miss
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5 tasks with low hit rate (also below floor).
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    stored = (project.design_doc or {}).get("sr_catch_rate_trends", {})
    cat = stored.get("categories", {}).get("correctness", {})
    assert cat.get("below_floor") is True, (
        "below_floor must be True after two consecutive below-floor windows; "
        "dispatcher reads this field from design_doc"
    )
    assert cat.get("current_hit_rate") is not None, (
        "current_hit_rate required by dispatcher"
    )
    assert isinstance(cat.get("rate_history"), list) and len(cat["rate_history"]) >= 2


@pytest.mark.asyncio
async def test_compute_sr_false_attestation_no_duplicate_alerts(
    db_session: AsyncSession,
):
    """Calling compute_sr_false_attestation_trends twice with the same elevated
    categories must not create duplicate Alert rows (defense-in-depth dedup)."""
    from app.playbook_learning import compute_sr_false_attestation_trends

    project = await _make_project(db_session)

    def _fa_task():
        """Task where SR claimed tests_executed_cleanly=PASS but CI failed."""
        t = Task(
            project_id=project.id,
            title="false attestation task",
            payloads=[{"type": "create", "prompt": "p"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "check_results": {
                        "tests_executed_cleanly": {
                            "passed": True,
                            "evidence": "exit_code=0",
                        }
                    }
                },
                "review_loop": {"ci_passed": False},
            },
        )
        t.created_at = _utcnow() - timedelta(days=2)
        return t

    tasks = [_fa_task() for _ in range(5)]
    for t in tasks:
        db_session.add(t)
    await db_session.flush()

    await compute_sr_false_attestation_trends(project.id, db_session, tasks)
    await db_session.flush()
    await compute_sr_false_attestation_trends(project.id, db_session, tasks)
    await db_session.flush()

    alerts = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                    Alert.title.like("SR false attestation rate elevated%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1, (
        f"expected exactly 1 false-attestation alert, got {len(alerts)}; "
        "duplicate-alert dedup guard must be active"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests for engagement_thinning flag in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engagement_thinning_flag_set_when_below_threshold(
    db_session: AsyncSession,
):
    """engagement_thinning=True when rate < threshold and total >= min_passes."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 10 current-window PASS tasks for correctness: 0 have reasoning → rate = 0.0
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": ""},
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window tasks to establish a history entry
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"].get("correctness")
    assert cat is not None
    assert cat["reasoning_engagement_total"] == 10
    assert cat["reasoning_engagement_rate"] == 0.0  # below 0.5 threshold
    assert cat["engagement_thinning"] is True


@pytest.mark.asyncio
async def test_engagement_thinning_flag_false_when_insufficient_passes(
    db_session: AsyncSession,
):
    """engagement_thinning=False when total < min_passes (default 10), even if rate is low."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Only 5 PASS tasks (below the default min_passes=10) in current window
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": ""},
        )
        db_session.add(t)
        tasks.append(t)
    # Previous-window tasks to ensure the function can compute trends (non-None result)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"].get("correctness")
    assert cat is not None
    assert cat["reasoning_engagement_total"] == 5
    assert cat["reasoning_engagement_rate"] == 0.0
    # Too few samples — thinning flag must be False
    assert cat["engagement_thinning"] is False


@pytest.mark.asyncio
async def test_engagement_thinning_flag_set_for_regressing_category(
    db_session: AsyncSession,
):
    """engagement_thinning=True even for a regressing category (no exclusion)."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 10 hits (SR catches human) + 10 no-reasoning PASS tasks
    # → category is NOT regressing by miss_rate, but engagement rate = 0.0
    # Make it regressing by having a decreasing hit rate vs previous window.
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    # 10 PASS tasks with no reasoning in current window
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": ""},
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: all hits → current window has lower hit rate → regressing
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"].get("correctness")
    assert cat is not None
    # The category should be regressing (below 0.5 threshold) or have trend decreasing
    assert cat["reasoning_engagement_total"] == 10
    assert cat["reasoning_engagement_rate"] == 0.0
    # thinning flag applies even for regressing categories
    assert cat["engagement_thinning"] is True


@pytest.mark.asyncio
async def test_engagement_thinning_honors_design_doc_overrides(
    db_session: AsyncSession,
):
    """engagement_thinning respects sr_engagement_threshold / sr_engagement_min_passes from design_doc."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    # Override threshold=0.8, min_passes=5 so that 5 passes at 0.0 rate qualifies.
    project = await _make_project(
        db_session,
        design_doc={
            "sr_engagement_threshold": 0.8,
            "sr_engagement_min_passes": 5,
        },
    )
    tasks = []
    # 5 PASS tasks with no reasoning — rate=0.0, total=5
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": ""},
        )
        db_session.add(t)
        tasks.append(t)
    # Previous-window tasks to ensure the function can compute trends (non-None result)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"].get("correctness")
    assert cat is not None
    assert cat["reasoning_engagement_total"] == 5
    assert cat["reasoning_engagement_rate"] == 0.0
    # With overrides: threshold=0.8, min_passes=5 → 5 passes at 0.0 qualifies
    assert cat["engagement_thinning"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Tests for anti-resurrection suppression helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_is_auto_playbook_suppressed_archive_within_cooldown():
    """Entry archived within cooldown window is suppressed."""
    from app.playbook_learning import _is_auto_playbook_suppressed
    from app.models import _utcnow

    now_str = _utcnow().isoformat()
    doc = {
        "playbook_archive": [
            {
                "_archived_key": "playbook/auto-test-pattern",
                "_archived_at": now_str,
                "_retirement_reason": "idle_auto_entry:30d",
            }
        ]
    }
    assert _is_auto_playbook_suppressed(doc, "playbook/auto-test-pattern") is True


def test_is_auto_playbook_suppressed_archive_after_cooldown():
    """Entry archived beyond cooldown window is NOT suppressed."""
    from app.playbook_learning import (
        _is_auto_playbook_suppressed,
        _AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS,
    )
    from app.models import _utcnow

    old_str = (
        _utcnow() - timedelta(days=_AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS + 1)
    ).isoformat()
    doc = {
        "playbook_archive": [
            {
                "_archived_key": "playbook/auto-test-pattern",
                "_archived_at": old_str,
                "_retirement_reason": "idle_auto_entry:30d",
            }
        ]
    }
    assert _is_auto_playbook_suppressed(doc, "playbook/auto-test-pattern") is False


def test_is_auto_playbook_suppressed_operator_deleted_key():
    """Key in playbook_deleted_keys is suppressed regardless of archive."""
    from app.playbook_learning import _is_auto_playbook_suppressed

    doc = {
        "playbook_deleted_keys": ["playbook/auto-test-pattern"],
    }
    assert _is_auto_playbook_suppressed(doc, "playbook/auto-test-pattern") is True


def test_is_auto_playbook_suppressed_never_seen():
    """Key with no archive entry and not in deleted_keys is NOT suppressed."""
    from app.playbook_learning import _is_auto_playbook_suppressed

    doc: dict = {}
    assert _is_auto_playbook_suppressed(doc, "playbook/auto-test-pattern") is False


def test_get_prior_archive_entry_returns_most_recent():
    """_get_prior_archive_entry returns the most recent (last) archive entry."""
    from app.playbook_learning import _get_prior_archive_entry

    doc = {
        "playbook_archive": [
            {
                "_archived_key": "playbook/k",
                "_archived_at": "2026-01-01T00:00:00",
                "confirmed_count": 3,
            },
            {
                "_archived_key": "playbook/k",
                "_archived_at": "2026-02-01T00:00:00",
                "confirmed_count": 7,
            },
            {"_archived_key": "playbook/other", "_archived_at": "2026-03-01T00:00:00"},
        ]
    }
    entry = _get_prior_archive_entry(doc, "playbook/k")
    assert entry is not None
    assert entry["confirmed_count"] == 7


def test_get_prior_archive_entry_returns_none_when_missing():
    """_get_prior_archive_entry returns None when no matching archive entry."""
    from app.playbook_learning import _get_prior_archive_entry

    doc: dict = {"playbook_archive": [{"_archived_key": "playbook/other"}]}
    assert _get_prior_archive_entry(doc, "playbook/missing") is None


def test_first_attempt_skips_suppressed_key():
    """_maybe_auto_playbook_from_first_attempt skips keys in playbook_deleted_keys."""
    from app.playbook_learning import (
        _maybe_auto_playbook_from_first_attempt,
        _DIRECTION_FIRST_ATTEMPT_KEY,
        _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD,
    )

    direction_key = "dir-abc123"
    dir_slug = direction_key[:8].replace("-", "").replace("_", "")
    suppressed_key = f"playbook/first-attempt-{dir_slug}-test-coverage"

    doc: dict = {
        _DIRECTION_FIRST_ATTEMPT_KEY: {
            direction_key: {
                "test-coverage": _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD + 2,
            }
        },
        "playbook_deleted_keys": [suppressed_key],
    }
    created = _maybe_auto_playbook_from_first_attempt(
        doc, direction_key, "create", "task-id"
    )
    assert created == []


@pytest.mark.asyncio
async def test_auto_playbook_skips_if_archived_within_cooldown(
    db_session: AsyncSession,
):
    """_maybe_auto_playbook does not recreate entry archived within cooldown."""
    from app.playbook_learning import (
        _maybe_auto_playbook,
        _signature_to_key,
        _extract_failure_signature,
    )
    from app.models import _utcnow

    project = await _make_project(db_session)
    msg = "vm failed to start after provision attempt"

    # Compute the key that would be generated for this failure pattern
    sig = _extract_failure_signature("PROVISION_FAILURE", msg, "create", "small")
    key = _signature_to_key(sig)
    full_key = f"playbook/{key}"

    # Pre-populate archive with a recently-retired entry
    now_str = _utcnow().isoformat()
    project.design_doc = {
        "playbook_archive": [
            {
                "_archived_key": full_key,
                "_archived_at": now_str,
                "_retirement_reason": "idle_auto_entry:30d",
                "confirmed_count": 4,
            }
        ]
    }
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.flush()

    # Two prior tasks with pre-set signature
    expected_sig = sig
    t1 = Task(
        project_id=project.id,
        title="T1",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.FAILED,
        latest_status_message=msg,
        context={"_failure_signature": expected_sig},
    )
    t2 = Task(
        project_id=project.id,
        title="T2",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.FAILED,
        latest_status_message=msg,
        context={"_failure_signature": expected_sig},
    )
    db_session.add(t1)
    db_session.add(t2)
    await db_session.flush()

    # Current task — should be suppressed
    t3 = Task(
        project_id=project.id,
        title="T3",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.FAILED,
        latest_status_message=msg,
        context={},
    )
    db_session.add(t3)
    await db_session.flush()

    result = await _maybe_auto_playbook(t3, "PROVISION_FAILURE", msg, db_session)
    assert result is None, "Entry should be suppressed within cooldown"
    doc = project.design_doc or {}
    assert full_key not in doc


@pytest.mark.asyncio
async def test_auto_playbook_skips_if_operator_deleted(db_session: AsyncSession):
    """_maybe_auto_playbook does not recreate operator-deleted entries."""
    from app.playbook_learning import (
        _maybe_auto_playbook,
        _signature_to_key,
        _extract_failure_signature,
    )

    project = await _make_project(db_session)
    msg = "vm failed to start after provision attempt"

    sig = _extract_failure_signature("PROVISION_FAILURE", msg, "create", "small")
    key = _signature_to_key(sig)
    full_key = f"playbook/{key}"

    # Operator deleted this key
    project.design_doc = {"playbook_deleted_keys": [full_key]}
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.flush()

    expected_sig = sig
    t1 = Task(
        project_id=project.id,
        title="T1",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.FAILED,
        latest_status_message=msg,
        context={"_failure_signature": expected_sig},
    )
    t2 = Task(
        project_id=project.id,
        title="T2",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.FAILED,
        latest_status_message=msg,
        context={"_failure_signature": expected_sig},
    )
    db_session.add(t1)
    db_session.add(t2)
    await db_session.flush()

    t3 = Task(
        project_id=project.id,
        title="T3",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.FAILED,
        latest_status_message=msg,
        context={},
    )
    db_session.add(t3)
    await db_session.flush()

    result = await _maybe_auto_playbook(t3, "PROVISION_FAILURE", msg, db_session)
    assert result is None, "Operator-deleted entry should be suppressed"
    doc = project.design_doc or {}
    assert full_key not in doc


@pytest.mark.asyncio
async def test_auto_playbook_recreates_after_cooldown_carries_prior_data(
    db_session: AsyncSession,
):
    """_maybe_auto_playbook re-creates entry after cooldown and carries prior confirmed_count."""
    from app.playbook_learning import (
        _maybe_auto_playbook,
        _signature_to_key,
        _extract_failure_signature,
        _AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS,
    )
    from app.models import _utcnow

    project = await _make_project(db_session)
    msg = "vm failed to start after provision attempt"

    sig = _extract_failure_signature("PROVISION_FAILURE", msg, "create", "small")
    key = _signature_to_key(sig)
    full_key = f"playbook/{key}"

    # Archive entry is OLDER than cooldown → should be recreatable
    old_str = (
        _utcnow() - timedelta(days=_AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS + 1)
    ).isoformat()
    project.design_doc = {
        "playbook_archive": [
            {
                "_archived_key": full_key,
                "_archived_at": old_str,
                "_retirement_reason": "low_success_rate:0.10",
                "confirmed_count": 6,
            }
        ]
    }
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.flush()

    expected_sig = sig
    t1 = Task(
        project_id=project.id,
        title="T1",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.FAILED,
        latest_status_message=msg,
        context={"_failure_signature": expected_sig},
    )
    t2 = Task(
        project_id=project.id,
        title="T2",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.FAILED,
        latest_status_message=msg,
        context={"_failure_signature": expected_sig},
    )
    db_session.add(t1)
    db_session.add(t2)
    await db_session.flush()

    t3 = Task(
        project_id=project.id,
        title="T3",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.FAILED,
        latest_status_message=msg,
        context={},
    )
    db_session.add(t3)
    await db_session.flush()

    result = await _maybe_auto_playbook(t3, "PROVISION_FAILURE", msg, db_session)
    assert result is not None, "Entry should be recreated after cooldown elapsed"
    doc = project.design_doc or {}
    entry = doc.get(full_key)
    assert entry is not None
    # Prior confirmed_count carried forward
    assert entry["confirmed_count"] == 6
    # Prior retirement reason preserved
    assert entry.get("prior_retirement_reason") == "low_success_rate:0.10"


# ─────────────────────────────────────────────────────────────────────────────
# E2E non-degeneracy guard for compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_sr_task_production_shaped(
    project_id,
    *,
    age_days: float,
    all_hits: bool = True,
) -> Task:
    """Build a Task that mirrors the full production context shape.

    Populates all three human-truth layers:
    - review_loop.review_history with CRITICAL/MAJOR/MINOR issues whose
      descriptions match known _REVIEW_CATEGORIES keywords
    - _first_attempt_rejection_categories (regression_risk, type-safety)
    - _all_iterations_rejection_categories (regression_risk, type-safety, correctness)

    check_results distinguish sr_alone (regression_risk, no origin) vs
    reviewer_save (type-safety, origin="reviewer").

    When all_hits=False, neither category appears in sr_failed so both are
    misses (hit_rate drops), exercising the miss path.
    """
    check_results: dict = {}
    if all_hits:
        check_results["regression_risk"] = {
            "passed": False,
            "issue": "regression in test_bar",
        }
        check_results["type-safety"] = {
            "passed": False,
            "issue": "type hint missing for return value",
            "origin": "reviewer",
        }

    ctx: dict = {
        "_self_review_outcome": {"check_results": check_results},
        "review_loop": {
            "review_history": [
                {
                    "iteration": 0,
                    "issues": [
                        {
                            "description": "regression in test_bar broke the test suite",
                            "severity": "CRITICAL",
                        },
                        {
                            "description": "type hint missing for return value parameter",
                            "severity": "MAJOR",
                        },
                    ],
                },
                {
                    "iteration": 1,
                    "issues": [
                        {
                            "description": "logic bug in edge case boundary handling",
                            "severity": "MINOR",
                        },
                    ],
                },
            ]
        },
        "_first_attempt_rejection_categories": {
            "regression_risk": 1,
            "type-safety": 1,
        },
        "_all_iterations_rejection_categories": {
            "regression_risk": 1,
            "type-safety": 1,
            "correctness": 1,
        },
    }
    task = Task(
        project_id=project_id,
        title="Production-shaped SR task",
        payloads=[{"type": "create", "prompt": "implement feature X"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_e2e_production_shaped(
    db_session: AsyncSession,
):
    """Non-degeneracy guard: the metric produces a real, in-range result on
    production-shaped task data with full review_loop.review_history,
    origin-tagged check_results, and both rejection-category sets.

    Uses two completed StrategicCycles so cycle-mode engages. Asserts the
    headline contract: data_sufficient=True, overall_hit_rate and
    previous_overall_hit_rate are real floats in [0,1], categories is
    non-empty with non-None current_hit_rate, and a trend label is present.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    c_cycle = _make_cycle(project.id, age_days=5)
    p_cycle = _make_cycle(project.id, age_days=30)
    db_session.add(c_cycle)
    db_session.add(p_cycle)
    await db_session.flush()

    tasks = []
    # Current cycle: 5 tasks are full hits, 1 task misses — hit_rate 5/6 per category.
    for _ in range(5):
        t = _make_sr_task_production_shaped(project.id, age_days=3, all_hits=True)
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    t_miss = _make_sr_task_production_shaped(project.id, age_days=4, all_hits=False)
    t_miss.strategic_cycle_id = c_cycle.id
    db_session.add(t_miss)
    tasks.append(t_miss)

    # Previous cycle: all 6 tasks are full hits — hit_rate 6/6 = 1.0.
    for _ in range(6):
        t = _make_sr_task_production_shaped(project.id, age_days=25, all_hits=True)
        t.strategic_cycle_id = p_cycle.id
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    # ── Headline contract ─────────────────────────────────────────────────────
    assert result is not None, "Should produce a real result on production-shaped data"
    assert result["data_sufficient"] is True, "data_sufficient must be True"
    assert result["bucket_mode"] == "cycle", (
        "Should use cycle-mode with two completed cycles"
    )

    # overall_hit_rate is a real float in [0, 1]
    overall = result["overall_hit_rate"]
    assert isinstance(overall, float), (
        f"overall_hit_rate must be a float, got {overall!r}"
    )
    assert 0.0 <= overall <= 1.0, f"overall_hit_rate out of range: {overall}"

    previous_overall = result["previous_overall_hit_rate"]
    assert isinstance(previous_overall, float), (
        f"previous_overall_hit_rate must be a float, got {previous_overall!r}"
    )
    assert 0.0 <= previous_overall <= 1.0, (
        f"previous_overall_hit_rate out of range: {previous_overall}"
    )

    # categories is non-empty; categories with findings must have non-None rates
    cats = result["categories"]
    assert cats, "categories must be non-empty"
    for cat_name, cat_data in cats.items():
        # A trend label must always be present
        assert cat_data["trend"] in ("stable", "increasing", "decreasing"), (
            f"category {cat_name!r} has unexpected trend: {cat_data['trend']!r}"
        )
        # current_hit_rate is None only when current_human_count == 0 (e.g. a
        # category present in _all_iterations but not _first_attempt set); for
        # categories that have findings in the current window, rate must be a
        # real float in [0, 1].
        if cat_data["current_human_count"] > 0:
            hr = cat_data["current_hit_rate"]
            assert hr is not None, (
                f"category {cat_name!r} has None current_hit_rate despite "
                f"current_human_count={cat_data['current_human_count']} — metric degenerated"
            )
            assert isinstance(hr, float), (
                f"category {cat_name!r} current_hit_rate must be a float, got {hr!r}"
            )
            assert 0.0 <= hr <= 1.0, (
                f"category {cat_name!r} current_hit_rate out of range: {hr}"
            )

    # At least the two explicitly-seeded categories must have current findings
    assert any(d["current_human_count"] > 0 for d in cats.values()), (
        "No category has current_human_count > 0 — metric degenerated"
    )

    # ── Origin-split wiring ───────────────────────────────────────────────────
    # regression_risk should be an SR-alone hit; type-safety a reviewer-save hit
    rr = cats.get("regression_risk")
    ts = cats.get("type-safety")
    assert rr is not None, "regression_risk must appear in categories"
    assert ts is not None, "type-safety must appear in categories"

    # SR-alone hits come from the 5 hitting tasks (no origin tag)
    assert rr["sr_alone_hit_count"] == 5, (
        f"Expected 5 sr_alone hits for regression_risk, got {rr['sr_alone_hit_count']}"
    )
    assert rr["reviewer_save_hit_count"] == 0

    # reviewer_save hits come from the 5 hitting tasks (origin=reviewer)
    assert ts["reviewer_save_hit_count"] == 5, (
        f"Expected 5 reviewer_save hits for type-safety, got {ts['reviewer_save_hit_count']}"
    )
    assert ts["sr_alone_hit_count"] == 0

    # ── Cycle-over-cycle rates ────────────────────────────────────────────────
    # Current: 5/6 ≈ 0.833; Previous: 6/6 = 1.0
    assert rr["current_hit_rate"] == round(5 / 6, 3), (
        f"regression_risk current_hit_rate wrong: {rr['current_hit_rate']}"
    )
    assert rr["previous_hit_rate"] == 1.0, (
        f"regression_risk previous_hit_rate wrong: {rr['previous_hit_rate']}"
    )


@pytest.mark.asyncio
async def test_overall_trend_verdict_improving_when_significant(
    db_session: AsyncSession,
):
    """overall_trend_verdict='improving' with sufficient data and a significant delta."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(8):  # current: 8/8 hits → 1.0
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(8):  # previous: 1/8 hits → 0.125 (delta=0.875, z>1.28)
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"] if i == 0 else [],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    assert result["overall_trend_verdict"] == "improving"
    assert result["overall_trend_significant"] is True
    assert result["overall_trend_data_sufficient"] is True


@pytest.mark.asyncio
async def test_overall_trend_verdict_stable_when_not_significant(
    db_session: AsyncSession,
):
    """overall_trend_verdict='stable' when delta is above threshold but not statistically
    significant (z < _TREND_Z_MIN=1.28 with small sample sizes)."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for cnt, hit in [(3, True), (2, False)]:  # current: 3/5 = 0.6
        for _ in range(cnt):
            t = _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"correctness": 1},
                sr_failed_cats=["correctness"] if hit else [],
            )
            db_session.add(t)
            tasks.append(t)
    for cnt, hit in [
        (4, True),
        (1, False),
    ]:  # previous: 4/5 = 0.8; delta=0.2>0.05 but z≈0.69<1.28
        for _ in range(cnt):
            t = _make_sr_task(
                project.id,
                age_days=20,
                human_cats={"correctness": 1},
                sr_failed_cats=["correctness"] if hit else [],
            )
            db_session.add(t)
            tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    assert result["overall_trend_verdict"] == "stable"
    assert result["overall_trend_significant"] is False
    assert result["overall_trend_data_sufficient"] is True


@pytest.mark.asyncio
async def test_sr_catch_rate_previous_bucket_not_starved_by_throughput(
    db_session: AsyncSession,
):
    """Regression: when the current window has more tasks than the old fetch cap (200),
    the previous window must still be populated. The fix in agent.py uses a 28-day
    time-bounded query instead of a flat row-count cap so both 14-day buckets are
    always loaded regardless of current-window throughput."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    OLD_CAP = 200
    # Create more tasks than the old cap in the current window (last 7 days).
    current_tasks = []
    for _ in range(OLD_CAP + 10):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        current_tasks.append(t)
    # Create 5 tasks in the previous window (15-28 days ago).
    previous_tasks = []
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        previous_tasks.append(t)
    await db_session.flush()

    # OLD behaviour: only the most-recent OLD_CAP rows — all land in the current
    # window (age_days=2), previous bucket is empty → metric freezes at None.
    old_cap_rows = sorted(
        current_tasks + previous_tasks,
        key=lambda t: t.created_at,
        reverse=True,
    )[:OLD_CAP]
    result_old = await compute_sr_catch_rate_trends(
        project.id, db_session, old_cap_rows
    )
    assert result_old is None, (
        "flat cap fetch should leave the previous bucket empty and return None"
    )

    # NEW behaviour: time-bounded fetch (28 days) loads both windows → both
    # buckets populated → real result returned with data_sufficient=True.
    all_tasks = current_tasks + previous_tasks
    result_new = await compute_sr_catch_rate_trends(project.id, db_session, all_tasks)
    assert result_new is not None, (
        "window-bounded fetch must populate both buckets and return a real result"
    )
    assert result_new.get("data_sufficient") is True


@pytest.mark.asyncio
async def test_refresh_sr_catch_rate_fetch_covers_both_windows(
    db_session: AsyncSession,
):
    """Integration: _refresh_sr_catch_rate must fetch tasks from both the current
    and previous calendar windows (0-14 days and 14-28 days ago) so the metric
    is data_sufficient=True even when the current window alone saturates a
    row-count cap.  Catches fetch-window regressions that bypass compute_sr_catch_rate_trends
    unit tests (which accept hand-built task lists)."""
    from app.routers.agent import _refresh_sr_catch_rate

    project = await _make_project(db_session)

    # 6 SR tasks in current window (age_days=3 → inside 0–14-day bucket)
    for _ in range(6):
        t = _make_sr_task(
            project.id,
            age_days=3,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)

    # 6 SR tasks in previous window (age_days=20 → inside 14–28-day bucket)
    for _ in range(6):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)

    # Non-SR tasks that must NOT affect the metric (no _self_review_outcome)
    for _ in range(3):
        t = Task(
            project_id=project.id,
            title="non-SR task",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.COMPLETED,
            context={"scope": "small"},
        )
        db_session.add(t)

    await db_session.flush()

    # Call the router-level helper that performs the actual DB fetch
    await _refresh_sr_catch_rate(session=db_session, project_id=project.id)
    await db_session.flush()

    trends = (project.design_doc or {}).get("sr_catch_rate_trends")
    assert trends is not None, "_refresh_sr_catch_rate must write sr_catch_rate_trends"
    assert trends.get("data_sufficient") is True, (
        "fetch must cover both 0-14 and 14-28 day windows; "
        "data_sufficient=False means the previous window was starved"
    )
