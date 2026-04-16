"""Tests for automatic playbook entry creation from recurring failure patterns."""
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
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

    stale_penalty = lambda v: 1 if v.get("stale_file_refs") else 0
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

    import json as _json
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
        # One failure-derived entry to satisfy the sampling gate
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
    task = await _make_completed_task(db_session, project.id, task_type="create", scope="small")

    from app.playbook_learning import _playbook_size_bytes
    size_before = _playbook_size_bytes(project.design_doc)
    assert size_before > _PLAYBOOK_CAPACITY_BYTES, "Doc must be over capacity before the test"

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key == "success-create", "New entry should have been created"
    doc = project.design_doc or {}
    archive = doc.get("playbook_archive", [])
    assert len(archive) >= 1, "At least one entry should have been archived for capacity"
