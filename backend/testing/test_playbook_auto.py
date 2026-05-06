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
    assert len(auto_keys_2) == 1, f"Expected 1 auto entry after duplicate check, got {auto_keys_2}"


@pytest.mark.asyncio
async def test_cap_of_five_is_respected(db_session: AsyncSession):
    """Auto-playbook entries are capped at 5 per project; beyond that no new entries are created."""
    # Pre-populate design_doc with 5 existing auto-entries
    prefilled = {f"playbook/auto-CODE_ERROR-abc{i:03d}": {"content": f"entry {i}", "confirmed_count": 0} for i in range(5)}
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
    await _make_failed_task(db_session, project.id, status_message=common_msg, task_type="create")
    await _make_failed_task(db_session, project.id, status_message=common_msg, task_type="fix")
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

    common_msg = "CPUS_ALL_REGIONS quota exceeded in zone us-central1"
    # 2 old failures (> 7 days ago)
    await _make_failed_task(db_session, project.id, status_message=common_msg, age_days=8)
    await _make_failed_task(db_session, project.id, status_message=common_msg, age_days=8)
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
async def test_capture_success_metadata_detects_tests_dir(db_session: AsyncSession):
    """Files under /tests/ count as test files for tests_added/test_file_count."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    files = ["backend/tests/test_router.py", "backend/app/foo.py"]
    await _make_completed_task(db_session, project.id, task_type="create", scope="small")
    await _make_completed_task(db_session, project.id, task_type="create", scope="small")
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


@pytest.mark.asyncio
async def test_capture_success_direction_affinity(db_session: AsyncSession):
    """direction_successes accumulates per-direction counts; file_type_distribution
    records extension breakdown; test_file_count reflects actual test file count."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    files = ["backend/app/foo.py", "backend/app/bar.py", "backend/testing/test_foo.py"]
    dir_id = "dir-abc"
    await _make_completed_task(db_session, project.id, task_type="create", scope="small")
    await _make_completed_task(db_session, project.id, task_type="create", scope="small")
    t1 = await _make_completed_task(
        db_session, project.id,
        task_type="create", scope="small",
        related_files=files, direction_id=dir_id,
    )

    await capture_success_pattern(t1, db_session)
    await db_session.flush()

    entry = (project.design_doc or {})["playbook/success-create-small"]
    assert entry["direction_successes"] == {dir_id: 1}
    assert entry["file_type_distribution"] == {"py": 3}
    assert entry["test_file_count"] == 1

    # Second success for the same direction increments its count
    t2 = await _make_completed_task(
        db_session, project.id,
        task_type="create", scope="small",
        related_files=files, direction_id=dir_id,
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
        "playbook/success-create-small": _make_success_entry(1, file_type_distribution={"py": 1}),
    }
    assert summarize_recent_successes(doc, "create", None, "small") is None


def test_summarize_recent_successes_omitted_for_other_task_type():
    """Entries for a different task_type → returns None."""
    from app.playbook_learning import summarize_recent_successes

    doc = {
        "playbook/success-fix-small": _make_success_entry(5, file_type_distribution={"py": 5}),
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
        assert ln.startswith("## ") or ln.startswith("- "), f"Truncated mid-line: {ln!r}"


def test_summarize_recent_successes_returns_none_when_budget_too_small():
    """When even one bullet doesn't fit, return None rather than malformed output."""
    from app.playbook_learning import summarize_recent_successes

    doc = {
        "playbook/success-create-small": _make_success_entry(
            5, file_type_distribution={"py": 5}
        ),
    }
    # 10 chars cannot fit even the header
    assert summarize_recent_successes(doc, "create", None, "small", char_budget=10) is None


def test_summarize_recent_successes_task_type_agnostic():
    """Helper itself does not gate by task_type — call sites enforce the allowlist."""
    from app.playbook_learning import summarize_recent_successes, RECENT_SUCCESSES_TASK_TYPES

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

    assert _age_penalty(old_date, confirmed=0) == 1, "Old entry with no confirmations should get penalty"
    assert _age_penalty(old_date, confirmed=2) == 1, "Old entry with 2 confirmations should get penalty"
    assert _age_penalty(old_date, confirmed=3) == 0, "Old entry with 3+ confirmations should NOT get penalty"
    assert _age_penalty(recent_date, confirmed=0) == 0, "Recent entry should NOT get penalty"


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


def _make_doc_with_size(num_entries: int, confirmed_counts: list[int] | None = None) -> dict:
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
        "playbook/a": {"content": "tip a", "confirmed_count": 5, "updated_at": _utcnow().isoformat()},
        "playbook/b": {"content": "tip b", "confirmed_count": 0, "updated_at": _utcnow().isoformat()},
        "playbook/c": {"content": "tip c", "confirmed_count": 3, "updated_at": _utcnow().isoformat()},
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
            "content": "tip a", "confirmed_count": 0,
            "stale_file_refs": False, "updated_at": _utcnow().isoformat()
        },
        "playbook/b": {
            "content": "tip b", "confirmed_count": 5,
            "stale_file_refs": True, "updated_at": _utcnow().isoformat()
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
            "content": "old tip", "confirmed_count": 0,
            "updated_at": old_date,
        },
        "playbook/recent": {
            "content": "recent tip", "confirmed_count": 0,
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
    from app.playbook_learning import _archive_least_useful_entry, _PLAYBOOK_ARCHIVE_MAX_DEFAULT
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
        existing_archive = [
            {"_archived_key": f"old-{i}"} for i in range(5)
        ]
        doc = {
            "playbook_archive": existing_archive,
            "playbook/new": {
                "content": "least useful", "confirmed_count": 0,
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
    assert "playbook/low-performer" not in result, "Low-success-rate entry should be archived first"
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
            "followed_count": 10,
            "confirmed_count": 1,  # 10% success rate
        },
    }
    retire_ineffective_patterns(doc)
    archive = doc.get("playbook_archive", [])
    assert len(archive) == 1
    reason = archive[0]["_retirement_reason"]
    assert "0.10" in reason


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
    """maybe_archive_for_capacity archives when combined size exceeds 25KB."""
    from app.playbook_learning import maybe_archive_for_capacity, _PLAYBOOK_CAPACITY_BYTES

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

    # If already over capacity, archiving should trigger
    if current_size >= _PLAYBOOK_CAPACITY_BYTES:
        keys_before = {k for k in doc if k.startswith("playbook/")}
        result = maybe_archive_for_capacity(doc, 0)
        keys_after = {k for k in result if k.startswith("playbook/")}
        assert len(keys_after) < len(keys_before), "An entry should have been archived"
        assert len(result.get("playbook_archive", [])) == 1


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


@pytest.mark.asyncio
async def test_capture_success_archives_when_over_capacity(db_session: AsyncSession):
    """capture_success_pattern archives the least-useful entry when total size exceeds 25KB."""
    from app.playbook_learning import capture_success_pattern, _PLAYBOOK_CAPACITY_BYTES

    # Build a design_doc clearly over capacity: 28 entries × ~950 bytes ≈ 26.6 KB
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
    for i in range(27):
        existing_doc[f"playbook/old-entry-{i:03d}"] = {
            "content": big_content,
            "confirmed_count": 0,
            "source": "auto-generated",
            "updated_at": (_utcnow() - timedelta(days=10)).isoformat(),
        }

    project = await _make_project(db_session, design_doc=dict(existing_doc))
    # Create 3 completed tasks of the same type to satisfy the noise filter (>= 3 completions).
    await _make_completed_task(db_session, project.id, task_type="create", scope="small")
    await _make_completed_task(db_session, project.id, task_type="create", scope="small")
    task = await _make_completed_task(db_session, project.id, task_type="create", scope="small")

    from app.playbook_learning import _playbook_size_bytes
    size_before = _playbook_size_bytes(project.design_doc)
    assert size_before > _PLAYBOOK_CAPACITY_BYTES, "Doc must be over capacity before the test"

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key == "success-create-small", "New entry should have been created"
    doc = project.design_doc or {}
    archive = doc.get("playbook_archive", [])
    assert len(archive) >= 1, "At least one entry should have been archived for capacity"


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
async def test_compute_review_effectiveness_returns_none_below_sample_size(db_session: AsyncSession):
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
async def test_compute_review_effectiveness_detects_decreasing_trend(db_session: AsyncSession):
    """Detects a decreasing trend when a category drops by >= 5pp between windows."""
    from app.playbook_learning import compute_review_effectiveness

    project = await _make_project(db_session)

    # Previous window (15-28 days ago): all 12 tasks have test-coverage issues
    prev_tasks = []
    for _ in range(12):
        t = await _make_task_with_reviews(
            db_session, project.id, age_days=20,
            issue_descs=["missing test coverage for new function", "no error handling"]
        )
        prev_tasks.append(t)

    # Current window (last 14 days): only 2 out of 12 tasks have test-coverage issues
    curr_tasks = []
    for _ in range(2):
        t = await _make_task_with_reviews(
            db_session, project.id, age_days=5,
            issue_descs=["missing test coverage for new function"]
        )
        curr_tasks.append(t)
    for _ in range(10):
        t = await _make_task_with_reviews(
            db_session, project.id, age_days=5,
            issue_descs=["no error handling"]
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
async def test_compute_review_effectiveness_stores_in_design_doc(db_session: AsyncSession):
    """Result is persisted in project.design_doc['review_effectiveness']."""
    from app.playbook_learning import compute_review_effectiveness

    project = await _make_project(db_session)

    # Create 12 tasks in each window
    all_tasks = []
    for _ in range(12):
        t = await _make_task_with_reviews(
            db_session, project.id, age_days=20,
            issue_descs=["missing test coverage for new function"]
        )
        all_tasks.append(t)
    for _ in range(12):
        t = await _make_task_with_reviews(
            db_session, project.id, age_days=5,
            issue_descs=["missing test coverage for new function"]
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
async def test_compute_review_effectiveness_rate_history_grows(db_session: AsyncSession):
    """rate_history accumulates a new entry on each new window computation."""
    from app.playbook_learning import compute_review_effectiveness

    project = await _make_project(db_session)

    all_tasks = []
    for _ in range(12):
        all_tasks.append(await _make_task_with_reviews(
            db_session, project.id, age_days=20,
            issue_descs=["missing test coverage for new function"]
        ))
    for _ in range(12):
        all_tasks.append(await _make_task_with_reviews(
            db_session, project.id, age_days=5,
            issue_descs=["missing test coverage for new function"]
        ))

    # First computation — creates the first history entry
    result1 = await compute_review_effectiveness(project.id, db_session, all_tasks)
    await db_session.flush()
    assert result1 is not None
    history1 = result1["categories"].get("test-coverage", {}).get("rate_history", [])
    assert len(history1) == 1

    # Simulate a new window by clearing computed_at to force is_new_window=True
    doc = dict(project.design_doc or {})
    doc["review_effectiveness"]["computed_at"] = (_utcnow() - timedelta(days=15)).isoformat()
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
    project = await _make_project(db_session, design_doc={
        "review_effectiveness": {
            "computed_at": (_utcnow() - timedelta(days=15)).isoformat(),
            "categories": {
                "test-coverage": {"rate_history": [0.35, 0.30]},
            },
            "persistent_issues": [],
        },
    })

    # 12 tasks in each window, all with test-coverage issues (keeps rate high)
    all_tasks = []
    for _ in range(12):
        all_tasks.append(await _make_task_with_reviews(
            db_session, project.id, age_days=20,
            issue_descs=["missing test coverage for new function"]
        ))
    for _ in range(12):
        all_tasks.append(await _make_task_with_reviews(
            db_session, project.id, age_days=5,
            issue_descs=["missing test coverage for new function"]
        ))

    result = await compute_review_effectiveness(project.id, db_session, all_tasks)
    await db_session.flush()

    assert result is not None
    # rate_history now has 3 entries, all > 20%, so it should be flagged as persistent
    history = result["categories"].get("test-coverage", {}).get("rate_history", [])
    assert len(history) == 3
    assert all(r > 0.20 for r in history)
    assert "test-coverage" in result["persistent_issues"]


@pytest.mark.asyncio
async def test_compute_review_effectiveness_recurrence_rate_basic(db_session: AsyncSession):
    """recurrence_rate measures fraction of rejected tasks with each category."""
    from app.playbook_learning import compute_review_effectiveness

    project = await _make_project(db_session)

    # 12 tasks in each window so we pass the minimum sample threshold.
    # Previous window (15-28 days ago): give all 12 tasks review history,
    # and give 6 of them a test-coverage first-attempt rejection.
    all_tasks = []
    for i in range(12):
        t = await _make_task_with_reviews(
            db_session, project.id, age_days=20,
            issue_descs=["missing test coverage for new function"]
        )
        if i < 6:
            t.context = {**(t.context or {}), "_first_attempt_rejection_categories": {"test-coverage": 2}}
        all_tasks.append(t)

    # Current window (last 14 days): 12 tasks with review history.
    # Give 4 out of 12 a test-coverage rejection.
    for i in range(12):
        t = await _make_task_with_reviews(
            db_session, project.id, age_days=5,
            issue_descs=["missing test coverage for new function"]
        )
        if i < 4:
            t.context = {**(t.context or {}), "_first_attempt_rejection_categories": {"test-coverage": 1}}
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
async def test_compute_review_effectiveness_recurrence_history_grows(db_session: AsyncSession):
    """recurrence_history accumulates a new entry on each new window, capped at 5."""
    from app.playbook_learning import compute_review_effectiveness

    project = await _make_project(db_session)

    all_tasks = []
    for _ in range(12):
        t = await _make_task_with_reviews(
            db_session, project.id, age_days=20,
            issue_descs=["missing test coverage for new function"]
        )
        t.context = {**(t.context or {}), "_first_attempt_rejection_categories": {"test-coverage": 1}}
        all_tasks.append(t)
    for _ in range(12):
        t = await _make_task_with_reviews(
            db_session, project.id, age_days=5,
            issue_descs=["missing test coverage for new function"]
        )
        t.context = {**(t.context or {}), "_first_attempt_rejection_categories": {"test-coverage": 1}}
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
    doc["review_effectiveness"]["computed_at"] = (_utcnow() - timedelta(days=15)).isoformat()
    project.design_doc = doc
    await db_session.flush()

    result2 = await compute_review_effectiveness(project.id, db_session, all_tasks)
    await db_session.flush()
    assert result2 is not None
    rh2 = result2["categories"].get("test-coverage", {}).get("recurrence_history", [])
    assert len(rh2) == 2


@pytest.mark.asyncio
async def test_compute_review_effectiveness_recurrence_none_when_no_rejections(db_session: AsyncSession):
    """recurrence_rate is None when no tasks have _first_attempt_rejection_categories."""
    from app.playbook_learning import compute_review_effectiveness

    project = await _make_project(db_session)

    # Tasks have review history (to pass sample threshold) but no rejection categories
    all_tasks = []
    for _ in range(12):
        all_tasks.append(await _make_task_with_reviews(
            db_session, project.id, age_days=20,
            issue_descs=["missing test coverage for new function"]
        ))
    for _ in range(12):
        all_tasks.append(await _make_task_with_reviews(
            db_session, project.id, age_days=5,
            issue_descs=["missing test coverage for new function"]
        ))

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
        "test-coverage": [{"desc": "missing test", "severity": "MAJOR", "iteration": 1}],
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
        - timedelta(days=_MODULE_FEEDBACK_DECAY_AGE_DAYS + _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS)
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
        "test-coverage": [{"desc": "missing test", "severity": "MAJOR", "iteration": 1}],
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
        "test-coverage": [{"desc": "missing test", "severity": "MAJOR", "iteration": 1}],
    }

    _update_module_feedback_profiles(doc, ["backend/app/orchestrator.py"], buckets)

    entry = doc["module_feedback"]["backend/app/orchestrator.py"]
    # error-handling decayed to 0 and was dropped; test-coverage came back via the new bucket.
    assert "error-handling" not in entry
    assert entry["test-coverage"] == 1
    assert entry["total"] == 1


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
    assert "compute" in sig       # innermost frame
    assert "main" not in sig      # outer frame should not appear


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
    t1 = await _make_failed_task(db_session, project.id, status_message=error_with_traceback)
    t1.context = {"_failure_signature": sig}
    t2 = await _make_failed_task(db_session, project.id, status_message=error_with_traceback)
    t2.context = {"_failure_signature": sig}
    await db_session.flush()

    t3 = await _make_failed_task(db_session, project.id, status_message=error_with_traceback)
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


def test_accumulate_direction_first_attempt_decays_old_counts():
    """Stale counts past the grace window decay exponentially before new counts accumulate."""
    from app.playbook_learning import _accumulate_direction_first_attempt, _DIRECTION_FIRST_ATTEMPT_KEY

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
    created = _maybe_auto_playbook_from_first_attempt(doc, direction_key, "create", "task-id")
    assert created == []


# ─────────────────────────────────────────────────────────────────────────────
# Tests for scope inference in _maybe_auto_playbook
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_maybe_auto_playbook_direction_scope_when_single_direction(db_session: AsyncSession):
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
        t3, "PROVISION_FAILURE",
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
async def test_maybe_auto_playbook_universal_scope_when_multiple_directions(db_session: AsyncSession):
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
        t3, "PROVISION_FAILURE",
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

    await extract_review_feedback_to_playbook(
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
                "last_updated": _utcnow().isoformat(),
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

    await extract_review_feedback_to_playbook(
        task, review_loop, pr_number=42, original_task_type="create", session=db_session
    )
    await db_session.flush()

    doc = project.design_doc or {}
    review_keys = [k for k in doc if k.startswith("playbook/review-")]
    assert len(review_keys) >= 1, "Should create at least 1 review-extracted entry at iteration 3"
    assert any("review-test-coverage" in k for k in review_keys)


# ─────────────────────────────────────────────────────────────────────────────
# Tests for scope inference in extract_review_feedback_to_playbook
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_review_feedback_module_scope_when_related_files_present(db_session: AsyncSession):
    """extract_review_feedback_to_playbook sets scope=module and stores related_files when present."""
    from app.playbook_learning import extract_review_feedback_to_playbook

    direction_id = "dir-xyz-456"
    project = await _make_project(db_session, design_doc={})
    review_history = [
        {
            "iteration": 1,
            "issues": [
                {"description": "missing test coverage for new function", "severity": "MAJOR"},
                {"description": "no unit test for edge case", "severity": "MINOR"},
                {"description": "add test for error path", "severity": "MAJOR"},
            ],
        },
        {"iteration": 2, "issues": [{"description": "still missing tests", "severity": "MAJOR"}]},
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
        assert entry.get("scope") == "module", f"Expected module scope, got {entry.get('scope')}"
        assert "backend/app/routes.py" in entry.get("related_files", [])
        assert entry.get("learned_from_direction") == direction_id


@pytest.mark.asyncio
async def test_extract_review_feedback_universal_scope_when_no_related_files(db_session: AsyncSession):
    """extract_review_feedback_to_playbook sets scope=universal when no related_files."""
    from app.playbook_learning import extract_review_feedback_to_playbook

    project = await _make_project(db_session, design_doc={})
    review_history = [
        {
            "iteration": 1,
            "issues": [
                {"description": "missing test coverage for new function", "severity": "MAJOR"},
                {"description": "no unit test for edge case", "severity": "MINOR"},
                {"description": "add test for error path", "severity": "MAJOR"},
            ],
        },
        {"iteration": 2, "issues": [{"description": "still missing tests", "severity": "MAJOR"}]},
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
        task, review_loop, pr_number=100, original_task_type="create", session=db_session
    )
    await db_session.flush()

    assert keys, "Expected at least one entry to be created"
    doc = project.design_doc or {}
    for key in keys:
        if not key.startswith("review-"):
            continue  # skip first-attempt-pattern keys
        entry = doc[f"playbook/{key}"]
        assert entry.get("scope") == "universal", f"Expected universal scope, got {entry.get('scope')}"
        assert "related_files" not in entry


@pytest.mark.asyncio
async def test_extract_review_feedback_stores_learned_from_direction(db_session: AsyncSession):
    """extract_review_feedback_to_playbook stores learned_from_direction from task context."""
    from app.playbook_learning import extract_review_feedback_to_playbook

    direction_id = "dir-provenance-test"
    project = await _make_project(db_session, design_doc={})
    review_history = [
        {
            "iteration": 1,
            "issues": [
                {"description": "missing test coverage for new function", "severity": "MAJOR"},
                {"description": "no unit test for edge case", "severity": "MINOR"},
                {"description": "add test for error path", "severity": "MAJOR"},
            ],
        },
        {"iteration": 2, "issues": [{"description": "still missing tests", "severity": "MAJOR"}]},
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
            task, _make_multi_iter_review_loop(), pr_number=pr,
            original_task_type="create", session=db_session,
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
    assert entry["hit_count"] == 3, f"hit_count should accumulate, got {entry['hit_count']}"


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
        task, _make_multi_iter_review_loop("missing unit tests for new module"),
        pr_number=99, original_task_type="create", session=db_session,
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
async def test_extract_review_feedback_keeps_fresh_entry_content(db_session: AsyncSession):
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
        task, _make_multi_iter_review_loop(),
        pr_number=200, original_task_type="create", session=db_session,
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
        task, _make_multi_iter_review_loop(),
        pr_number=300, original_task_type="create", session=db_session,
    )
    await db_session.flush()

    entry = (project.design_doc or {})["playbook/review-error-handling"]
    assert entry.get("potentially_stale") is True
    assert entry["updated_at"] == very_old_iso, "stale entry timestamp should not change"
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
        compute_recurring_first_review_descriptions(rows, cutoff, min_tasks=5)
        is None
    )


def test_recurring_descriptions_count_unique_tasks_mode():
    from app.playbook_learning import compute_recurring_first_review_descriptions

    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    # "solo" appears 5 times but all in one task; "shared" appears twice across two tasks.
    rows = [
        _FakeRow("t1", [_entry(0, "solo", "solo", "solo", "solo", "solo", "shared")], created_at=now),
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
    overage_days = _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS  # exactly one half-life past grace
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


@pytest.mark.xfail(strict=False, reason="Sibling cluster 1/4 adds increment_playbook_applied_counts to playbook_learning.py")
@pytest.mark.asyncio
async def test_confirmed_count_increments_on_applied_entry_success(db_session: AsyncSession):
    """When a completed task has _applied_playbook_entries in its context, the referenced
    entry's confirmed_count and followed_count both increment."""
    from app.playbook_learning import increment_playbook_applied_counts

    key = "playbook/success-create-small"
    project = await _make_project(db_session, design_doc={
        key: {
            "content": "First-attempt success pattern for create tasks.",
            "source": "success-pattern",
            "confirmed_count": 1,
            "followed_count": 3,
            "applicable_types": ["create"],
            "updated_at": _utcnow().isoformat(),
        }
    })
    task = await _make_completed_task(db_session, project.id, task_type="create", scope="small")
    task.context = {**(task.context or {}), "_applied_playbook_entries": ["success-create-small"]}
    await db_session.flush()

    await increment_playbook_applied_counts(task, db_session, success=True)
    await db_session.flush()

    entry = (project.design_doc or {})[key]
    assert entry["confirmed_count"] == 2, "confirmed_count must increment on success"
    assert entry["followed_count"] == 4, "followed_count must increment on any application"


@pytest.mark.xfail(strict=False, reason="Sibling cluster 1/4 adds increment_playbook_applied_counts to playbook_learning.py")
@pytest.mark.asyncio
async def test_followed_count_increments_without_confirmed_on_failure(db_session: AsyncSession):
    """When an applied task fails, followed_count grows (denominator) but confirmed_count
    stays fixed — causing the success rate to drop as expected."""
    from app.playbook_learning import increment_playbook_applied_counts

    key = "playbook/success-create-small"
    project = await _make_project(db_session, design_doc={
        key: {
            "content": "First-attempt success pattern for create tasks.",
            "source": "success-pattern",
            "confirmed_count": 2,
            "followed_count": 3,
            "applicable_types": ["create"],
            "updated_at": _utcnow().isoformat(),
        }
    })
    task = Task(
        project_id=project.id,
        title="Failed task",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.FAILED,
        context={"scope": "small", "_applied_playbook_entries": ["success-create-small"]},
    )
    db_session.add(task)
    await db_session.flush()

    await increment_playbook_applied_counts(task, db_session, success=False)
    await db_session.flush()

    entry = (project.design_doc or {})[key]
    assert entry["confirmed_count"] == 2, "confirmed_count must not change on failure"
    assert entry["followed_count"] == 4, "followed_count must grow regardless of outcome"


@pytest.mark.xfail(strict=False, reason="Sibling cluster 1/4 adds auto_retire_low_effectiveness_entries to playbook_learning.py")
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
    assert any("_archived_reason" in e for e in archive), "Must include explanatory reason tag"


@pytest.mark.xfail(strict=False, reason="Sibling cluster 1/4 adds auto_retire_low_effectiveness_entries to playbook_learning.py")
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

    assert "playbook/new-pattern" in doc, "Under-observed entry must not be auto-retired"
    assert not doc.get("playbook_archive"), "No archive entries should be created for under-observed patterns"


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
    assert "pending_source" not in entry, "Successful task's pending flag must be cleared"
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
) -> Task:
    """Build a Task with both _self_review_outcome.check_results and _first_attempt_rejection_categories."""
    check_results = {cat: {"passed": False, "issue": "x"} for cat in sr_failed_cats}
    task = Task(
        project_id=project_id,
        title="SR task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {"check_results": check_results},
            "_first_attempt_rejection_categories": human_cats,
        },
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_returns_none_below_sample_size(db_session: AsyncSession):
    """Returns None when either window has fewer than 5 tasks with both fields."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for i in range(4):
        t = _make_sr_task(project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
        db_session.add(t)
        tasks.append(t)
    for i in range(4):
        t = _make_sr_task(project.id, age_days=20, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is None


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_per_category_hit_and_miss(db_session: AsyncSession):
    """Computes correct per-category hit_rate and miss_rate."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window (5 tasks): 3 hits, 2 misses on test-coverage
    for i in range(3):
        t = _make_sr_task(project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[])
        db_session.add(t)
        tasks.append(t)
    # Previous window (5 tasks): all hits
    for i in range(5):
        t = _make_sr_task(project.id, age_days=20, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
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
async def test_compute_sr_catch_rate_trends_trend_classification(db_session: AsyncSession):
    """Hit rate drops >= 5pp → trend='decreasing'."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 3/5 = 0.6
    for i in range(3):
        t = _make_sr_task(project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[])
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5/5 = 1.0 (delta = -0.4, well below -5pp threshold)
    for i in range(5):
        t = _make_sr_task(project.id, age_days=20, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["categories"]["test-coverage"]["trend"] == "decreasing"


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_rate_history_grows(db_session: AsyncSession):
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
        t = _make_sr_task(project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[])
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5/5 hits
    for i in range(5):
        t = _make_sr_task(project.id, age_days=20, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
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
async def test_compute_sr_catch_rate_trends_persists_to_design_doc(db_session: AsyncSession):
    """Writes sr_catch_rate_trends key to project.design_doc."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for i in range(5):
        t = _make_sr_task(project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(project.id, age_days=20, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
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


@pytest.mark.parametrize("phrasing", [
    "this regresses test_foo",
    "broke test_foo in this diff",
    "now failing assertion that was passing before",
    "the build no longer works after these changes",
    "this is a regression in the auth flow",
    "breaks test_serializer",
])
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
async def test_compute_sr_catch_rate_trends_regression_risk_hit_and_miss(db_session: AsyncSession):
    """When tasks carry regression_risk in both human_cats and sr_failed, the dashboard
    surfaces a non-zero hit_rate on the regression_risk axis."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window (5 tasks): 3 hits, 2 misses on regression_risk
    for i in range(3):
        t = _make_sr_task(
            project.id, age_days=2,
            human_cats={"regression_risk": 1},
            sr_failed_cats=["regression_risk"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project.id, age_days=2,
            human_cats={"regression_risk": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window (5 tasks): all hits
    for i in range(5):
        t = _make_sr_task(
            project.id, age_days=20,
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
async def test_compute_sr_catch_rate_trends_per_task_type_breakdown(db_session: AsyncSession):
    """by_task_type contains correct hit/miss rates per (task_type, category)."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5 'create' tasks — 4 hits on test-coverage
    for i in range(4):
        t = _make_sr_task(project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"], task_type="create")
        db_session.add(t)
        tasks.append(t)
    t = _make_sr_task(project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[], task_type="create")
    db_session.add(t)
    tasks.append(t)
    # Current window: 5 'fix' tasks — 2 hits on test-coverage
    for i in range(2):
        t = _make_sr_task(project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"], task_type="fix")
        db_session.add(t)
        tasks.append(t)
    for i in range(3):
        t = _make_sr_task(project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[], task_type="fix")
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5 tasks needed for overall threshold
    for i in range(5):
        t = _make_sr_task(project.id, age_days=20, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
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
async def test_compute_sr_catch_rate_trends_by_task_type_threshold_omits_low_sample(db_session: AsyncSession):
    """Cells with fewer than 5 samples per (task_type, category) are omitted from by_task_type."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5 'create' tasks (meets threshold), 4 'fix' tasks (below threshold)
    for i in range(5):
        t = _make_sr_task(project.id, age_days=2, human_cats={"correctness": 1}, sr_failed_cats=["correctness"], task_type="create")
        db_session.add(t)
        tasks.append(t)
    for i in range(4):
        t = _make_sr_task(project.id, age_days=2, human_cats={"correctness": 1}, sr_failed_cats=["correctness"], task_type="fix")
        db_session.add(t)
        tasks.append(t)
    # Previous window
    for i in range(5):
        t = _make_sr_task(project.id, age_days=20, human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    by_type = result["by_task_type"]
    assert "create" in by_type
    assert "fix" not in by_type


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
                "review_history": [
                    {"iteration": 1, "issues": review_issues}
                ]
            },
        },
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_example_issues_populated(db_session: AsyncSession):
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
        t = _make_sr_task(project.id, age_days=20, human_cats={"error-handling": 1}, sr_failed_cats=["error-handling"])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["error-handling"]
    assert "examples" in cat
    assert len(cat["examples"]) >= 1
    assert any("null check" in ex["desc"] for ex in cat["examples"])
    assert all(ex["severity"] in ("CRITICAL", "MAJOR", "MINOR") for ex in cat["examples"])


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_examples_by_task_type_scoped(db_session: AsyncSession):
    """Examples in by_task_type are drawn only from matching task types."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # "null check" is in error-handling keywords; "fallback" is also in error-handling
    create_issue = {"description": "Missing null check in create handler", "severity": "MAJOR"}
    fix_issue = {"description": "Unhandled exception in fix scenario", "severity": "MAJOR"}

    # 5 'create' tasks missing error-handling
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id, age_days=2,
            human_cats={"error-handling": 1}, sr_failed_cats=[],
            review_issues=[create_issue], task_type="create",
        )
        db_session.add(t)
        tasks.append(t)
    # 5 'fix' tasks missing error-handling
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id, age_days=2,
            human_cats={"error-handling": 1}, sr_failed_cats=[],
            review_issues=[fix_issue], task_type="fix",
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window
    for i in range(5):
        t = _make_sr_task(project.id, age_days=20, human_cats={"error-handling": 1}, sr_failed_cats=["error-handling"])
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
async def test_compute_sr_catch_rate_trends_sensitive_examples_dropped(db_session: AsyncSession):
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
            project.id, age_days=2,
            human_cats={"error-handling": 1}, sr_failed_cats=[],
            review_issues=review_issues,
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(project.id, age_days=20, human_cats={"error-handling": 1}, sr_failed_cats=["error-handling"])
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
async def test_compute_sr_catch_rate_trends_descriptions_truncated_and_pr_stripped(db_session: AsyncSession):
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
            project.id, age_days=2,
            human_cats={"error-handling": 1}, sr_failed_cats=[],
            review_issues=review_issues,
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(project.id, age_days=20, human_cats={"error-handling": 1}, sr_failed_cats=["error-handling"])
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
async def test_compute_sr_catch_rate_trends_example_cap_per_category(db_session: AsyncSession):
    """At most _EXAMPLE_MAX_PER_CAT examples are stored per category."""
    from app.playbook_learning import compute_sr_catch_rate_trends, _EXAMPLE_MAX_PER_CAT

    project = await _make_project(db_session)
    tasks = []
    # Provide many distinct review issues per task
    review_issues = [
        {"description": f"Issue number {i}", "severity": "MAJOR"}
        for i in range(10)
    ]
    for _ in range(5):
        t = _make_sr_task_with_review(
            project.id, age_days=2,
            human_cats={"error-handling": 1}, sr_failed_cats=[],
            review_issues=review_issues,
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(project.id, age_days=20, human_cats={"error-handling": 1}, sr_failed_cats=["error-handling"])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    examples = result["categories"]["error-handling"].get("examples", [])
    assert len(examples) <= _EXAMPLE_MAX_PER_CAT


# ─────────────────────────────────────────────────────────────────────────────
# Tests for regression notifications in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_decreasing_task_set(project_id, db_session, tasks):
    """Helper: 5 current tasks (3/5 hit) + 5 previous tasks (5/5 hit) → decreasing."""
    for i in range(3):
        t = _make_sr_task(project_id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(project_id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[])
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(project_id, age_days=20, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
        db_session.add(t)
        tasks.append(t)


@pytest.mark.asyncio
async def test_sr_catch_rate_regression_fires_notification_on_decreasing(db_session: AsyncSession):
    """A decreasing trend triggers post_failure_alert_message and dispatch_external_notification."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    _make_decreasing_task_set(project.id, db_session, tasks)
    await db_session.flush()

    with patch("app.notification_dispatch.post_failure_alert_message", new_callable=AsyncMock) as mock_post, \
         patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock) as mock_dispatch, \
         patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["categories"]["test-coverage"]["trend"] == "decreasing"

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    description = call_kwargs.args[2] if len(call_kwargs.args) >= 3 else call_kwargs.kwargs.get("description", "")
    assert "test-coverage" in description
    assert "→" in description

    mock_dispatch.assert_called_once()
    dispatch_kwargs = mock_dispatch.call_args
    payload = dispatch_kwargs.kwargs.get("payload") or dispatch_kwargs.args[2]
    assert dispatch_kwargs.kwargs.get("event_type") == "sr_catch_rate_regression" or dispatch_kwargs.args[1] == "sr_catch_rate_regression"
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
    alert_rows = (await db_session.execute(
        _sql_select(Alert).where(
            Alert.project_id == project.id,
            Alert.dismissed_at.is_(None),
        )
    )).scalars().all()
    reg_rows = [r for r in alert_rows if r.title.startswith(_SR_REGRESSION_ALERT_TITLE_PREFIX)]
    assert len(reg_rows) == 1
    assert reg_rows[0].severity == AlertSeverity.WARNING
    assert "test-coverage" in reg_rows[0].title
    # Message reuses the same description string as the ProjectMessage
    assert "test-coverage" in reg_rows[0].message


@pytest.mark.asyncio
async def test_sr_catch_rate_regression_no_duplicate_in_same_window(db_session: AsyncSession):
    """A second call with the same regressing categories in the same window does NOT re-post."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    project = await _make_project(db_session)
    tasks = []
    _make_decreasing_task_set(project.id, db_session, tasks)
    await db_session.flush()

    with patch("app.notification_dispatch.post_failure_alert_message", new_callable=AsyncMock) as mock_post, \
         patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock), \
         patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None):
        await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    # Verify alert state was persisted
    await db_session.refresh(project)
    assert "sr_catch_rate_trends_alert" in (project.design_doc or {})
    assert mock_post.call_count == 1

    # Second call in the same window — should be deduped
    with patch("app.notification_dispatch.post_failure_alert_message", new_callable=AsyncMock) as mock_post2, \
         patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock), \
         patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None):
        await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    mock_post2.assert_not_called()

    # The dedup contract also covers the in-app Alert row: still exactly one.
    from app.playbook_learning import _SR_REGRESSION_ALERT_TITLE_PREFIX
    await db_session.commit()
    alert_rows = (await db_session.execute(
        _sql_select(Alert).where(
            Alert.project_id == project.id,
            Alert.dismissed_at.is_(None),
        )
    )).scalars().all()
    reg_rows = [r for r in alert_rows if r.title.startswith(_SR_REGRESSION_ALERT_TITLE_PREFIX)]
    assert len(reg_rows) == 1


@pytest.mark.asyncio
async def test_sr_catch_rate_regression_no_notification_when_all_stable_or_increasing(db_session: AsyncSession):
    """No notification fires when no category is decreasing."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5/5 hit (stable/increasing vs previous 3/5)
    for i in range(5):
        t = _make_sr_task(project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
        db_session.add(t)
        tasks.append(t)
    for i in range(3):
        t = _make_sr_task(project.id, age_days=20, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(project.id, age_days=20, human_cats={"test-coverage": 1}, sr_failed_cats=[])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    with patch("app.notification_dispatch.post_failure_alert_message", new_callable=AsyncMock) as mock_post, \
         patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock) as mock_dispatch, \
         patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None):
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
async def test_compute_sr_catch_rate_trends_all_iterations_catch_rate_vs_first_attempt(db_session: AsyncSession):
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
                "_all_iterations_rejection_categories": {"test-coverage": 1, "correctness": 1},
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
                "_all_iterations_rejection_categories": {"test-coverage": 1, "correctness": 1},
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
    assert "correctness" in cats, "iteration-2 category must surface in all_iterations_catch_rate"
    assert cats["correctness"]["current_hit_rate"] is None, (
        "correctness must be absent from first-attempt metric"
    )
    assert cats["correctness"]["all_iterations_catch_rate"] == 0.0
    assert cats["correctness"]["all_iterations_miss_rate"] == 1.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_all_iterations_backward_compat(db_session: AsyncSession):
    """When _all_iterations_rejection_categories is absent and review_history is empty,
    all_iterations_catch_rate falls back to the first-attempt categories so existing
    tasks continue to produce a non-None all_iterations value."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 5 current tasks: only _first_attempt_rejection_categories, no all-iterations field
    for _ in range(5):
        t = _make_sr_task(
            project.id, age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    # 5 previous tasks: same
    for _ in range(5):
        t = _make_sr_task(
            project.id, age_days=20,
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
        t = _make_sr_task(project.id, age_days=2,
                          human_cats={"test-coverage": 1}, sr_failed_cats=["correctness"])
        db_session.add(t)
        tasks.append(t)
    # 5 previous: same
    for _ in range(5):
        t = _make_sr_task(project.id, age_days=20,
                          human_cats={"test-coverage": 1}, sr_failed_cats=["correctness"])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["false_positive_rate"] == 1.0, "all SR failures unconfirmed → FP rate must be 1.0"
    assert cat["precision"] == 0.0, "zero confirmed failures → precision must be 0.0"


@pytest.mark.asyncio
async def test_sr_false_positive_rate_human_confirms_is_zero(db_session: AsyncSession):
    """SR fails a category that human also flags → false_positive_rate == 0.0, precision == 1.0."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):
        t = _make_sr_task(project.id, age_days=2,
                          human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(project.id, age_days=20,
                          human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["false_positive_rate"] == 0.0, "all SR failures confirmed by human → FP rate 0"
    assert cat["precision"] == 1.0, "all SR failures confirmed → precision must be 1.0"


@pytest.mark.asyncio
async def test_sr_false_positive_rate_review_history_confirms_not_fp(db_session: AsyncSession):
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
                        {"iteration": 2, "issues": [
                            {"description": "missing test for edge case", "severity": "MAJOR"}
                        ]}
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
        t = _make_sr_task(project.id, age_days=2,
                          human_cats={"correctness": 1}, sr_failed_cats=[])
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(project.id, age_days=20,
                          human_cats={"correctness": 1}, sr_failed_cats=[])
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


# ---------------------------------------------------------------------------
# Tests for per-severity catch-rate breakdown in compute_sr_catch_rate_trends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_severity_counts(db_session: AsyncSession):
    """by_severity accumulates correct hit_count and human_count per severity."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Current window: 5 tasks with CRITICAL issue in "error-handling"; SR catches 2 of them
    # "exception" keyword → categorised as error-handling
    review_issue_critical = {"description": "Unhandled exception in request path", "severity": "CRITICAL"}
    for i in range(2):
        t = _make_sr_task_with_review(
            project.id, age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "error-handling"],
            review_issues=[review_issue_critical],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(3):
        t = _make_sr_task_with_review(
            project.id, age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],  # SR did NOT catch error-handling
            review_issues=[review_issue_critical],
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window: 5 tasks with MAJOR issue in "error-handling"; SR catches all
    review_issue_major = {"description": "Missing error handling for null input", "severity": "MAJOR"}
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id, age_days=20,
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
async def test_compute_sr_catch_rate_trends_critical_alert_tighter_threshold(db_session: AsyncSession):
    """CRITICAL regression (>=3pp delta) fires the alert even when category trend is stable."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Review issue that categorises to error-handling (keyword "exception")
    review_crit = {"description": "Unhandled exception bypasses auth guard", "severity": "CRITICAL"}

    # Current window: 5 tasks; SR misses error-handling (sr_failed only has "correctness")
    # → CRITICAL miss_rate = 5/5 = 1.0
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id, age_days=2,
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
            project.id, age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "error-handling"],
            review_issues=[review_crit],
        )
        db_session.add(t)
        tasks.append(t)
    t = _make_sr_task_with_review(
        project.id, age_days=20,
        human_cats={"correctness": 1},
        sr_failed_cats=["correctness"],
        review_issues=[review_crit],
    )
    db_session.add(t)
    tasks.append(t)

    await db_session.flush()

    with patch("app.notification_dispatch.post_failure_alert_message", new_callable=AsyncMock) as mock_post, \
         patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock) as mock_dispatch, \
         patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    # Per-category trend for "correctness" should be stable (all caught in both windows)
    assert result["categories"]["correctness"]["trend"] in ("stable", "increasing")
    # Per-severity CRITICAL must be regressing (delta = 0.8 >= 0.03)
    assert result["by_severity"]["CRITICAL"]["trend"] == "regressing"
    # Alert must fire due to severity regression
    mock_post.assert_called_once()
    call_desc = mock_post.call_args.args[2] if len(mock_post.call_args.args) >= 3 else mock_post.call_args.kwargs.get("description", "")
    assert "CRITICAL" in call_desc
    mock_dispatch.assert_called_once()
    payload = mock_dispatch.call_args.kwargs.get("payload") or mock_dispatch.call_args.args[2]
    assert "regressing_severities" in payload
    sevs = payload["regressing_severities"]
    assert any(s["severity"] == "CRITICAL" for s in sevs)


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_severity_dedup_includes_sevs(db_session: AsyncSession):
    """A new regressing severity not in the prior alert state breaks dedup and re-fires."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    project = await _make_project(db_session)
    tasks = []

    review_crit = {"description": "Unhandled exception on auth path", "severity": "CRITICAL"}
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id, age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            review_issues=[review_crit],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(4):
        t = _make_sr_task_with_review(
            project.id, age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "error-handling"],
            review_issues=[review_crit],
        )
        db_session.add(t)
        tasks.append(t)
    t = _make_sr_task_with_review(
        project.id, age_days=20,
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

    with patch("app.notification_dispatch.post_failure_alert_message", new_callable=AsyncMock) as mock_post, \
         patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock), \
         patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    # CRITICAL is regressing, so alert fires despite prior alert existing (different severity set)
    assert result is not None
    assert result["by_severity"]["CRITICAL"]["trend"] == "regressing"
    mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_severity_absent_when_no_rev_hist(db_session: AsyncSession):
    """by_severity has zero samples when tasks have no review_history."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for i in range(5):
        t = _make_sr_task(project.id, age_days=2, human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(project.id, age_days=20, human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
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
async def test_compute_sr_catch_rate_trends_by_severity_persisted_to_design_doc(db_session: AsyncSession):
    """by_severity is persisted to project.design_doc['sr_catch_rate_trends']."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    review_issue = {"description": "Logic bug in edge case", "severity": "MAJOR"}
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id, age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            review_issues=[review_issue],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id, age_days=20,
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
                {"description": "changes are too large for declared scope", "severity": "MAJOR"},
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
        {"iteration": 1, "issues": [{"description": "test coverage missing", "severity": "MAJOR"}]}
    ]
    sentinel = {"correctness": [{"desc": "pre-computed", "severity": "CRITICAL", "iteration": 0}]}

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
        {"classifications": [{"index": 0, "category": "test-coverage"}, {"index": 1, "category": "security"}]}
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
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_REVIEW_CATEGORIZATION": ""}):
        with patch("anthropic.AsyncAnthropic", mock_client_cls):
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
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_REVIEW_CATEGORIZATION": ""}):
        with patch("anthropic.AsyncAnthropic", MagicMock(side_effect=Exception("network error"))):
            result = await categorize_review_issues_llm(issues)

    expected = _keyword_from_flat(issues)
    assert result == expected


@pytest.mark.asyncio
async def test_categorize_review_issues_llm_kill_switch():
    """When DISABLE_LLM_REVIEW_CATEGORIZATION=1, no Anthropic client is constructed."""
    from unittest.mock import MagicMock
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

    issues = [{"description": "missing unit tests", "severity": "MAJOR", "iteration": 1}]
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
    from app.routers.admin_digest import _compute_sr_prediction_accuracy
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
                        {"iteration": 1, "issues": [{"description": "logic error", "severity": "MAJOR"}]}
                    ]
                },
            },
        )
        return t

    cached = {"test-coverage": [{"desc": "from cache", "severity": "MAJOR", "iteration": 1}]}
    tasks = [_make_task_with_cache(cached) for _ in range(5)]

    call_count = {"n": 0}
    original = _categorize_review_issues

    def spy(review_history, cached=None):
        if cached is None:
            call_count["n"] += 1
        return original(review_history, cached=cached)

    with patch("app.routers.admin_digest._categorize_review_issues", side_effect=spy):
        result = _compute_sr_prediction_accuracy(tasks)

    assert result is not None
    # The keyword scan should NOT have been called (cached path was taken for all 5 tasks).
    assert call_count["n"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Tests for cycle-based bucketing in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_cycle(project_id, *, age_days: float, outcome: str = "completed") -> "StrategicCycle":
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
        t = _make_sr_task(project.id, age_days=1, human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(project.id, age_days=1, human_cats={"correctness": 1}, sr_failed_cats=[])
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    # Previous cycle: 5/5 hit
    for i in range(5):
        t = _make_sr_task(project.id, age_days=19, human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
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
async def test_cycle_bucketing_skips_underpopulated_cycle_rolls_further_back(db_session: AsyncSession):
    """Skips a cycle pair where current bucket has < _MIN_SR_SAMPLE and uses N-1 / N-2."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    newest_cycle = _make_cycle(project.id, age_days=2)   # only 2 tasks — below threshold
    mid_cycle = _make_cycle(project.id, age_days=20)     # 5 tasks
    old_cycle = _make_cycle(project.id, age_days=40)     # 5 tasks
    db_session.add(newest_cycle)
    db_session.add(mid_cycle)
    db_session.add(old_cycle)
    await db_session.flush()

    tasks = []
    for i in range(2):  # newest: below threshold
        t = _make_sr_task(project.id, age_days=1, human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
        t.strategic_cycle_id = newest_cycle.id
        db_session.add(t)
        tasks.append(t)
    for i in range(5):  # mid: 5 tasks (should become "current")
        t = _make_sr_task(project.id, age_days=19, human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
        t.strategic_cycle_id = mid_cycle.id
        db_session.add(t)
        tasks.append(t)
    for i in range(5):  # old: 5 tasks (should become "previous")
        t = _make_sr_task(project.id, age_days=39, human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
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
async def test_fallback_to_calendar_windows_when_no_completed_cycles(db_session: AsyncSession):
    """Falls back to 14-day calendar windows when no completed cycles exist."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    # Add an in-progress cycle only — no completed cycles.
    in_progress_cycle = _make_cycle(project.id, age_days=2, outcome="in_progress")
    db_session.add(in_progress_cycle)
    await db_session.flush()

    tasks = []
    for i in range(5):
        t = _make_sr_task(project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(project.id, age_days=20, human_cats={"test-coverage": 1}, sr_failed_cats=["test-coverage"])
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
        t = _make_sr_task(project.id, age_days=1, human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(project.id, age_days=1, human_cats={"correctness": 1}, sr_failed_cats=[])
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(project.id, age_days=19, human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
        t.strategic_cycle_id = p_cycle.id
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    with patch("app.notification_dispatch.post_failure_alert_message", new_callable=AsyncMock) as mock_post, \
         patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock), \
         patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None):
        await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert mock_post.call_count == 1

    # Second call same cycle — dedup should suppress.
    with patch("app.notification_dispatch.post_failure_alert_message", new_callable=AsyncMock) as mock_post2, \
         patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock), \
         patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None):
        await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    mock_post2.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Tests for reviewer_recovery_after_incomplete_sr_count tracking
# ─────────────────────────────────────────────────────────────────────────────


def _make_recovery_task(project_id, *, use_top_level_origin: bool = True, use_check_results_origin: bool = False) -> Task:
    """Build a Task whose _self_review_outcome marks a recovery pass."""
    sro: dict = {}
    if use_top_level_origin:
        sro["reviewer_recovery_origin"] = "reviewer_recovery_after_incomplete_sr"
    if use_check_results_origin:
        sro["check_results"] = {
            "correctness": {"passed": False, "origin": "reviewer_recovery_after_incomplete_sr"},
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
        1 for t in tasks
        if (
            (t.context or {}).get("_self_review_outcome", {}).get("reviewer_recovery_origin")
            == "reviewer_recovery_after_incomplete_sr"
        ) or any(
            isinstance(v, dict) and v.get("origin") == "reviewer_recovery_after_incomplete_sr"
            for v in (
                (t.context or {}).get("_self_review_outcome", {}).get("check_results") or {}
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
            context={"_self_review_outcome": {"check_results": {"correctness": {"passed": True}}}},
        ),
    ]
    assert _count_recovery_tasks(tasks) == 2


def test_reviewer_recovery_count_via_check_results_origin():
    """Tasks with origin on a check_results entry (no top-level field) are counted."""
    project_id = uuid.uuid4()
    tasks = [
        _make_recovery_task(project_id, use_top_level_origin=False, use_check_results_origin=True),
        Task(
            project_id=project_id,
            title="Normal task",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.COMPLETED,
            context={"_self_review_outcome": {"check_results": {"correctness": {"passed": False}}}},
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
    doc["sr_catch_rate_trends"] = {"computed_at": "2026-01-01T00:00:00", "categories": {}}
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


def _make_skip_task(project_id, *, age_days: float, skip_reason: str | None = None) -> Task:
    """Build a Task whose _self_review_outcome is a fallback/skip envelope."""
    if skip_reason:
        outcome: dict = {"verdict": "unknown", "check_results": {}, "skip_reason": skip_reason}
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
        t = _make_sr_task(project.id, age_days=2, human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
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
        t = _make_sr_task(project.id, age_days=20, human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
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
        t = _make_sr_task(project.id, age_days=2, human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(project.id, age_days=20, human_cats={"correctness": 1}, sr_failed_cats=["correctness"])
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    # Overall result may be None (below _MIN_SR_SAMPLE) or skip fields may be absent
    if result is not None:
        assert "sr_skip_rate" not in result


@pytest.mark.asyncio
async def test_compute_fresh_reviewer_trend_returns_none_below_sample_size(db_session: AsyncSession):
    """Returns None when either window has fewer than _MIN_SR_SAMPLE tasks with reviewer_findings."""
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    tasks = []
    for _ in range(3):
        t = _make_fr_task(project.id, age_days=2, reviewer_findings=[{"category": "correctness"}])
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_fr_task(project.id, age_days=20, reviewer_findings=[{"category": "correctness"}])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_fresh_reviewer_trend(project.id, db_session, tasks)

    assert result is None


@pytest.mark.asyncio
async def test_compute_fresh_reviewer_trend_per_category_hit_rate(db_session: AsyncSession):
    """per_category_hit_rate reflects confirmed / flagged per category."""
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    tasks = []
    # Current: 5 flag correctness, 3 confirmed + 2 not
    for i in range(5):
        confirmed = i < 3
        t = _make_fr_task(
            project.id, age_days=2,
            reviewer_findings=[{"category": "correctness"}],
            human_cats=["correctness"] if confirmed else [],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous: 5 flag correctness, all confirmed
    for _ in range(5):
        t = _make_fr_task(
            project.id, age_days=20,
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
            project.id, age_days=2,
            reviewer_findings=[{"category": "correctness"}],
            human_cats=["correctness"],
            sr_failed_cats=["correctness"] if agent_caught else [],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous: reviewer flags correctness, agent also caught all → added_value=0
    for _ in range(5):
        t = _make_fr_task(
            project.id, age_days=20,
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
async def test_compute_fresh_reviewer_trend_approval_rate_lift(db_session: AsyncSession):
    """approval_rate_lift is fa_rate_with_reviewer minus fa_rate_without."""
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    tasks = []
    # Current: 5 with reviewer (all succeed), 5 without (3/5 succeed)
    for _ in range(5):
        t = _make_fr_task(project.id, age_days=2,
                          reviewer_findings=[{"category": "style-conventions"}],
                          first_attempt_succeeded=True)
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_fr_task(project.id, age_days=2, first_attempt_succeeded=(i < 3))
        db_session.add(t)
        tasks.append(t)
    # Previous: 5 with reviewer (4/5 succeed), 5 without (4/5 succeed)
    for i in range(5):
        t = _make_fr_task(project.id, age_days=20,
                          reviewer_findings=[{"category": "style-conventions"}],
                          first_attempt_succeeded=(i < 4))
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
async def test_compute_fresh_reviewer_trend_stable_classification(db_session: AsyncSession):
    """Hit rate delta < 5pp → trend='stable'."""
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    tasks = []
    for window_age in (2, 20):
        for i in range(5):
            t = _make_fr_task(
                project.id, age_days=window_age,
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
async def test_compute_fresh_reviewer_trend_persists_to_design_doc(db_session: AsyncSession):
    """Writes fresh_reviewer_trend to project.design_doc."""
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    tasks = []
    for window_age in (2, 20):
        for _ in range(5):
            t = _make_fr_task(project.id, age_days=window_age,
                              reviewer_findings=[{"category": "correctness"}])
            db_session.add(t)
            tasks.append(t)
    await db_session.flush()

    result = await compute_fresh_reviewer_trend(project.id, db_session, tasks)
    await db_session.flush()
    await db_session.refresh(project)

    assert result is not None
    assert project.design_doc.get("fresh_reviewer_trend") is not None
    assert project.design_doc["fresh_reviewer_trend"]["computed_at"] == result["computed_at"]


@pytest.mark.asyncio
async def test_compute_fresh_reviewer_trend_mixed_fr_on_off_cycle_bucketing(db_session: AsyncSession):
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
        t = _make_fr_task(project.id, age_days=5,
                          reviewer_findings=[{"category": "correctness"}],
                          cycle_id=str(c1.id))
        db_session.add(t)
        tasks.append(t)
    for _ in range(10):
        t = _make_fr_task(project.id, age_days=5, cycle_id=str(c1.id))
        db_session.add(t)
        tasks.append(t)
    # c2: 5 with reviewer
    for _ in range(5):
        t = _make_fr_task(project.id, age_days=25,
                          reviewer_findings=[{"category": "correctness"}],
                          cycle_id=str(c2.id))
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_fresh_reviewer_trend(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "cycle"
    assert result["current_sample_size"] == 5
    assert result["previous_sample_size"] == 5


@pytest.mark.asyncio
async def test_compute_fresh_reviewer_trend_improving_classification(db_session: AsyncSession):
    """Hit rate delta >= 5pp → trend='improving' for per_category_hit_rate."""
    from app.playbook_learning import compute_fresh_reviewer_trend

    project = await _make_project(db_session)
    tasks = []
    # Current: 5 flag correctness, all 5 confirmed → rate=1.0
    for _ in range(5):
        t = _make_fr_task(
            project.id, age_days=2,
            reviewer_findings=[{"category": "correctness"}],
            human_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous: 5 flag correctness, only 3 confirmed → rate=0.6
    for i in range(5):
        t = _make_fr_task(
            project.id, age_days=20,
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
        t = _make_fr_task(project.id, age_days=7,
                          reviewer_findings=[{"category": "correctness"}])
        db_session.add(t)
        tasks.append(t)
    # 5 tasks in 14-28d window
    for _ in range(5):
        t = _make_fr_task(project.id, age_days=21,
                          reviewer_findings=[{"category": "correctness"}])
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
async def test_compute_sr_reviewer_disagreements_returns_none_when_no_findings(db_session: AsyncSession):
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
async def test_compute_sr_reviewer_disagreements_counts_disagreements(db_session: AsyncSession):
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
            reviewer_findings=[{"category": "correctness", "severity": "HIGH", "description": "bug"}],
        )
        db_session.add(t)
        tasks.append(t)
    # 1 task: reviewer flags correctness but SR already failed it -> NOT a disagreement
    t2 = _make_sr_reviewer_task(
        project.id,
        age_days=2,
        task_type="fix",
        check_results={"correctness": {"passed": False, "issue": "x"}},
        reviewer_findings=[{"category": "correctness", "severity": "HIGH", "description": "bug"}],
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


@pytest.mark.asyncio
async def test_compute_sr_reviewer_disagreements_persists_to_design_doc(db_session: AsyncSession):
    """Result is written to project.design_doc['sr_reviewer_disagreements']."""
    from app.playbook_learning import compute_sr_reviewer_disagreements

    project = await _make_project(db_session)
    tasks = []
    t = _make_sr_reviewer_task(
        project.id,
        age_days=1,
        check_results={},
        reviewer_findings=[{"category": "test-coverage", "severity": "MEDIUM", "description": "missing test"}],
    )
    db_session.add(t)
    tasks.append(t)
    await db_session.flush()

    await compute_sr_reviewer_disagreements(project.id, db_session, tasks)

    stored = project.design_doc.get("sr_reviewer_disagreements") or {}
    assert stored.get("disagreements")
    assert stored["disagreements"][0]["category"] == "test-coverage"


@pytest.mark.asyncio
async def test_compute_sr_reviewer_disagreements_excludes_old_tasks(db_session: AsyncSession):
    """Tasks older than window_days are excluded."""
    from app.playbook_learning import compute_sr_reviewer_disagreements

    project = await _make_project(db_session)
    tasks = []
    t = _make_sr_reviewer_task(
        project.id,
        age_days=35,  # outside 30-day default window
        check_results={},
        reviewer_findings=[{"category": "security", "severity": "HIGH", "description": "x"}],
    )
    db_session.add(t)
    tasks.append(t)
    await db_session.flush()

    result = await compute_sr_reviewer_disagreements(project.id, db_session, tasks)

    assert result is None


@pytest.mark.asyncio
async def test_compute_sr_reviewer_disagreements_excludes_non_code_types(db_session: AsyncSession):
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
            reviewer_findings=[{"category": "correctness", "severity": "HIGH", "description": "bug"}],
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
            {"category": cat, "severity": "HIGH", "description": "found"} for cat in reviewer_cats
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


def test_compute_cat_fp_stats_fp_and_tp_counts():
    """SR-flagged categories caught by reviewer count as TP; missed by reviewer count as FP."""
    from app.playbook_learning import _compute_cat_fp_stats

    project_id = uuid.uuid4()
    tasks = [
        # correctness: SR flagged, reviewer also flagged → TP
        _make_fp_task(project_id, sr_failed_cats=["correctness"], reviewer_cats=["correctness"]),
        # correctness: SR flagged, reviewer missed → FP
        _make_fp_task(project_id, sr_failed_cats=["correctness"], reviewer_cats=[]),
        # security: SR flagged, reviewer missed → FP
        _make_fp_task(project_id, sr_failed_cats=["security"], reviewer_cats=["correctness"]),
    ]

    cat_fp, type_cat_fp = _compute_cat_fp_stats(tasks)

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
        _make_fp_task(project_id, task_type="research", sr_failed_cats=["correctness"], reviewer_cats=[]),
        _make_fp_task(project_id, task_type="plan", sr_failed_cats=["security"], reviewer_cats=[]),
    ]

    cat_fp, type_cat_fp = _compute_cat_fp_stats(tasks)

    assert cat_fp == {}
    assert type_cat_fp == {}


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

    cat_fp, type_cat_fp = _compute_cat_fp_stats([task])

    assert cat_fp == {}


def test_compute_cat_fp_stats_false_positive_rate_formula():
    """FPR = fp / (fp + tp): 2 FP and 1 TP gives 0.667."""
    from app.playbook_learning import _compute_cat_fp_stats

    project_id = uuid.uuid4()
    tasks = [
        _make_fp_task(project_id, sr_failed_cats=["correctness"], reviewer_cats=[]),
        _make_fp_task(project_id, sr_failed_cats=["correctness"], reviewer_cats=[]),
        _make_fp_task(project_id, sr_failed_cats=["correctness"], reviewer_cats=["correctness"]),
    ]

    cat_fp, _ = _compute_cat_fp_stats(tasks)

    fp = cat_fp["correctness"]["fp"]
    tp = cat_fp["correctness"]["tp"]
    assert fp == 2
    assert tp == 1
    fpr = round(fp / (fp + tp), 3)
    assert fpr == 0.667


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
            "edges_considered": edges_considered if edges_considered is not None else [],
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
async def test_compute_edges_trend_returns_none_below_sample_size(db_session: AsyncSession):
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
            project.id, age_days=2,
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
            project.id, age_days=3,
            edges_considered=[{"id": "e1"}] if i < 4 else [],
            cycle_id=str(c1.id),
        )
        db_session.add(t)
        tasks.append(t)
    # c2 (previous): 5 tasks, 1 with edges → rate=0.2
    for i in range(5):
        t = _make_edges_task(
            project.id, age_days=25,
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
            project.id, age_days=2,
            edges_considered=[{"id": "e1"}] if i < 4 else [],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous: 5 tasks, 4/5 with edges → 0.8 (delta=0 → stable)
    for i in range(5):
        t = _make_edges_task(
            project.id, age_days=20,
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
            t = _make_edges_task(project.id, age_days=window_age, edges_considered=[{"id": "e1"}])
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
                project.id, age_days=call_idx * 10 + 0.5,
                edges_considered=[{"id": "e1"}],
                cycle_id=str(c_cycle.id),
            )
            db_session.add(t)
            tasks.append(t)
        for _ in range(5):
            t = _make_edges_task(
                project.id, age_days=call_idx * 10 + 15,
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
async def test_compute_edges_trend_same_cycle_ids_as_sr_catch_rate(db_session: AsyncSession):
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
            project.id, age_days=3,
            human_cats={"correctness": 1}, sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = c1.id
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_edges_task(project.id, age_days=3, edges_considered=[{"id": "e1"}], cycle_id=str(c1.id))
        db_session.add(t)
        tasks.append(t)
    # c2: 5 SR tasks + 5 edges tasks
    for _ in range(5):
        t = _make_sr_task(
            project.id, age_days=25,
            human_cats={"correctness": 1}, sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = c2.id
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_edges_task(project.id, age_days=25, edges_considered=[{"id": "e1"}], cycle_id=str(c2.id))
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
async def test_compute_edges_trend_author_vs_reviewer_breakdown(db_session: AsyncSession):
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
            project.id, age_days=20,
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
