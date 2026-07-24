"""Tests for capture_success_pattern, summarize_recent_successes, and file-ref validation."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, Task, TaskStatus
from app.routers.agent_status import _utcnow


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
    t_small = await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )
    t_medium = await _make_completed_task(
        db_session, project.id, task_type="create", scope="medium"
    )

    await capture_success_pattern(t_small, db_session)
    await capture_success_pattern(t_medium, db_session)
    await db_session.flush()

    doc = project.design_doc or {}
    assert "playbook/success-create-small" in doc
    assert "playbook/success-create-medium" in doc
    # Entries must be independent
    assert doc["playbook/success-create-small"]["scope_distribution"] == {"small": 1}
    assert doc["playbook/success-create-medium"]["scope_distribution"] == {"medium": 1}


@pytest.mark.asyncio
async def test_capture_success_scope_distribution_accumulates(db_session: AsyncSession):
    """scope_distribution tallies how many times each scope has succeeded."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    for _ in range(4):
        await _make_completed_task(
            db_session, project.id, task_type="create", scope="small"
        )
    t1 = await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )
    t2 = await _make_completed_task(
        db_session, project.id, task_type="create", scope="small"
    )

    await capture_success_pattern(t1, db_session)
    await capture_success_pattern(t2, db_session)
    await db_session.flush()

    entry = (project.design_doc or {})["playbook/success-create-small"]
    assert entry["scope_distribution"] == {"small": 2}


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
# Tests for retired task-type guard and orphan purge
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_success_skips_retired_task_type(db_session: AsyncSession):
    """capture_success_pattern returns None and creates no playbook entry for a
    task type that is no longer in the live task-type registry (ALL_TASK_TYPES)."""
    from app.success_patterns import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    # Satisfy noise filter: 2 prior completions of the retired type
    await _make_completed_task(
        db_session, project.id, task_type="panel_review", scope="small"
    )
    await _make_completed_task(
        db_session, project.id, task_type="panel_review", scope="small"
    )
    task = await _make_completed_task(
        db_session, project.id, task_type="panel_review", scope="small"
    )

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key is None, "Should return None for a retired task type"
    doc = project.design_doc or {}
    assert not any(k.startswith("playbook/success-panel_review") for k in doc), (
        "No playbook entry should be created for a retired task type"
    )


@pytest.mark.asyncio
async def test_capture_success_purges_orphan_entries_for_retired_types(
    db_session: AsyncSession,
):
    """When a success pattern is captured for a live task type, any pre-existing
    playbook/success-* entries for retired task types are purged from the design_doc."""
    from app.success_patterns import capture_success_pattern

    orphan_entry = {
        "content": "First-attempt success pattern for panel_review tasks (5 observations).",
        "source": "success-pattern",
        "success_count": 5,
        "confirmed_count": 0,
        "applicable_types": ["panel_review"],
        "scope": "small",
        "scope_distribution": {"small": 5},
        "num_files_changed": 2,
        "tests_added": False,
        "first_attempt_success": True,
        "updated_at": _utcnow().isoformat(),
    }
    project = await _make_project(
        db_session,
        design_doc={"playbook/success-panel_review-small": orphan_entry},
    )
    # 2 prior completions so the live type crosses the noise threshold
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
    assert "playbook/success-panel_review-small" not in doc, (
        "Orphan entry for retired task type should be purged"
    )
    assert "playbook/success-create-small" in doc, (
        "Live task type entry should still be created"
    )


def test_purge_retired_success_entries_removes_unregistered():
    """_purge_retired_success_entries removes entries whose task type is absent from
    ALL_TASK_TYPES and leaves registered-type entries untouched."""
    from app.success_patterns import _purge_retired_success_entries

    doc = {
        "playbook/success-panel_review-small": {"content": "orphan"},
        "playbook/success-create-small": {"content": "keep"},
        "playbook/other-key": {"content": "untouched"},
    }
    removed = _purge_retired_success_entries(doc)

    assert removed == 1
    assert "playbook/success-panel_review-small" not in doc
    assert "playbook/success-create-small" in doc
    assert "playbook/other-key" in doc


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
