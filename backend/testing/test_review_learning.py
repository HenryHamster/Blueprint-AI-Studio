"""Playbook test suite (split).

Tests exercising the app.review_learning surface, split out of the former
testing/test_playbook_auto.py monolith. Behavior-preserving: test bodies
are copied verbatim; only file boundaries and shared-helper imports change.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Task, TaskStatus
from app.routers.agent_status import _utcnow

from testing._playbook_test_helpers import (
    _make_project,
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
    """extract_review_feedback_to_playbook persists per-module profiles to project_telemetry."""
    from app.playbook_learning import extract_review_feedback_to_playbook
    from app.project_telemetry import read_telemetry

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

    # module_feedback is a telemetry-migrated key — stripped from design_doc at
    # write time and persisted only to the project_telemetry KV table.
    feedback = await read_telemetry(db_session, project.id, "module_feedback")
    assert feedback is not None, "module_feedback should be written to project_telemetry"
    assert "backend/app/routes.py" in feedback
    entry = feedback["backend/app/routes.py"]
    assert entry["total"] > 0
    assert "last_updated" in entry


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

    # Direction tally accumulated — persisted to project_telemetry now (the
    # design_doc write leg for this migrated key is cut).
    from app.project_telemetry import read_telemetry

    dfa = await read_telemetry(db_session, project.id, _DIRECTION_FIRST_ATTEMPT_KEY)
    assert dfa is not None, "direction_first_attempt_feedback should be written"
    tally = dfa.get("dir-cafebabe-1234")
    assert tally is not None
    assert tally.get("test-coverage", 0) >= 1
    assert tally.get("total", 0) >= 1


@pytest.mark.asyncio
async def test_extract_review_feedback_dual_writes_direction_feedback_to_telemetry(
    db_session: AsyncSession,
):
    """The direction first-attempt tally is persisted to project_telemetry.

    The design_doc write leg for this migrated key is CUT: extract_review_feedback_to_playbook
    upserts the tally only into the dedicated project_telemetry KV row (key
    'direction_first_attempt_feedback'), and the telemetry-preferring reader
    read_direction_first_attempt_feedback returns it.
    """
    from app.playbook_learning import (
        extract_review_feedback_to_playbook,
        _DIRECTION_FIRST_ATTEMPT_KEY,
    )
    from app.project_telemetry import read_telemetry
    from app.review_learning import read_direction_first_attempt_feedback

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

    # design_doc write leg is CUT — the tally lives only in project_telemetry.
    assert _DIRECTION_FIRST_ATTEMPT_KEY not in (project.design_doc or {}), (
        "design_doc write leg for direction_first_attempt_feedback is cut"
    )

    # Telemetry row holds the tally.
    tel_tally = await read_telemetry(
        db_session, project.id, _DIRECTION_FIRST_ATTEMPT_KEY
    )
    assert tel_tally is not None
    assert tel_tally.get("dir-cafebabe-1234", {}).get("test-coverage", 0) >= 1

    # Reader prefers telemetry and returns the same tally.
    read_back = await read_direction_first_attempt_feedback(db_session, project)
    assert read_back == tel_tally


@pytest.mark.asyncio
async def test_read_direction_first_attempt_feedback_prefers_telemetry_only_value(
    db_session: AsyncSession,
):
    """A value present ONLY in project_telemetry is read back (telemetry wins)."""
    from app.review_learning import (
        _DIRECTION_FIRST_ATTEMPT_KEY,
        read_direction_first_attempt_feedback,
    )
    from app.project_telemetry import upsert_telemetry

    # design_doc holds a STALE value; telemetry holds the fresh one.
    project = await _make_project(
        db_session,
        design_doc={_DIRECTION_FIRST_ATTEMPT_KEY: {"dir-old": {"total": 1}}},
    )
    fresh = {"dir-new": {"total": 7, "test-coverage": 7}}
    await upsert_telemetry(
        db_session, project.id, _DIRECTION_FIRST_ATTEMPT_KEY, fresh
    )
    await db_session.flush()

    read_back = await read_direction_first_attempt_feedback(db_session, project)
    assert read_back == fresh, "telemetry row must take precedence over design_doc"


@pytest.mark.asyncio
async def test_read_direction_first_attempt_feedback_falls_back_to_design_doc(
    db_session: AsyncSession,
):
    """With no telemetry row, the reader falls back to design_doc (instant rollback)."""
    from app.review_learning import (
        _DIRECTION_FIRST_ATTEMPT_KEY,
        read_direction_first_attempt_feedback,
    )

    legacy = {"dir-legacy": {"total": 3, "correctness": 3}}
    project = await _make_project(
        db_session, design_doc={_DIRECTION_FIRST_ATTEMPT_KEY: legacy}
    )

    read_back = await read_direction_first_attempt_feedback(db_session, project)
    assert read_back == legacy


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
    from app.playbook_capacity import _mark_stale_review_entries

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
@pytest.mark.parametrize(
    "payload",
    [
        {"classifications": [{"index": 0, "category": "bogus"}]},
        {"classifications": [{"index": 0}]},
        {"classifications": {"index": 0, "category": "security"}},
        {"classifications": [{"index": "zero", "category": "security"}]},
    ],
)
async def test_categorize_review_issues_llm_falls_back_on_malformed_llm_output(payload):
    """Malformed classification payloads should still use keyword fallback."""
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
        patch("anthropic.AsyncAnthropic") as mock_cls,
        patch("app.review_learning.call_inline_llm_json", AsyncMock(return_value=payload)),
    ):
        result = await categorize_review_issues_llm(issues)
        mock_cls.assert_not_called()

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

    with patch("app.sr_review_quality_metrics._categorize_review_issues", side_effect=spy):
        result = compute_sr_prediction_accuracy(tasks)

    assert result is not None
    # The keyword scan should NOT have been called (cached path was taken for all 5 tasks).
    assert call_count["n"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Tests for reviewer_confirmed_categories — canonical precedence
# ─────────────────────────────────────────────────────────────────────────────


def test_reviewer_confirmed_categories_prefers_all_iterations():
    """_all_iterations_rejection_categories wins over first-attempt and review history."""
    from app.sr_blind_spots import reviewer_confirmed_categories

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
    result = reviewer_confirmed_categories(ctx)
    assert result == {"architecture", "correctness"}


def test_reviewer_confirmed_categories_excludes_other_from_all_iterations():
    """Persisted ``other`` must not leak into canonical SR ground truth."""
    from app.sr_blind_spots import reviewer_confirmed_categories

    ctx = {
        "_all_iterations_rejection_categories": {
            "architecture": 1,
            "other": 3,
            "correctness": 2,
        },
        "_first_attempt_rejection_categories": {"other": 1},
    }

    assert reviewer_confirmed_categories(ctx) == {"architecture", "correctness"}


def test_reviewer_confirmed_categories_ignores_review_history_only():
    """Review-history-only data is not canonical truth."""
    from app.sr_blind_spots import reviewer_confirmed_categories

    ctx = {
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
    assert reviewer_confirmed_categories(ctx) == set()


def test_reviewer_confirmed_categories_falls_back_to_first_attempt():
    """When _all_iterations is absent, first-attempt categories are used."""
    from app.sr_blind_spots import reviewer_confirmed_categories

    ctx = {
        "_first_attempt_rejection_categories": {"security": 1, "performance": 1},
    }
    result = reviewer_confirmed_categories(ctx)
    assert result == {"security", "performance"}


def test_reviewer_confirmed_categories_excludes_other_from_first_attempt_fallback():
    """First-attempt fallback keeps the same author-emittable category filter."""
    from app.sr_blind_spots import reviewer_confirmed_categories

    ctx = {
        "_first_attempt_rejection_categories": {
            "security": 1,
            "other": 2,
            "performance": 1,
        },
    }

    assert reviewer_confirmed_categories(ctx) == {"security", "performance"}


def test_reviewer_confirmed_categories_empty_ctx():
    """Returns empty set when no reviewer-confirmed sources are present."""
    from app.sr_blind_spots import reviewer_confirmed_categories

    assert reviewer_confirmed_categories({}) == set()


# ─────────────────────────────────────────────────────────────────────────────
# Tests for fresh_reviewer_effectiveness baseline
# ─────────────────────────────────────────────────────────────────────────────


def test_baseline_excludes_clean_reviewed():
    """Baseline approval rate excludes clean-reviewed tasks (reviewer pass with no findings).

    Clean-reviewed tasks are those where a reviewer pass ran but found no issues
    (has _self_review_outcome but empty reviewer_findings). These should be excluded
    from the baseline to avoid inflating it with disproportionately high approval rates.
    """
    from app.sr_review_quality_metrics import _compute_fresh_reviewer_effectiveness
    from app.models import Task, TaskStatus
    import uuid

    project_id = uuid.uuid4()

    # Task 1: No reviewer pass at all (should be in baseline)
    task_no_review = Task(
        id=uuid.uuid4(),
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        prompt="test",
        payloads=[{"type": "create", "prompt": "do something"}],
        context={
            "_first_attempt_succeeded": True,
        },
    )

    # Task 2: Reviewer pass ran but found nothing (clean-reviewed, should be EXCLUDED)
    task_clean_reviewed = Task(
        id=uuid.uuid4(),
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        prompt="test",
        payloads=[{"type": "create", "prompt": "do something"}],
        context={
            "_first_attempt_succeeded": True,
            "_self_review_outcome": {
                "reviewer_findings": [],  # Empty: pass ran but found nothing
                "check_results": {"test-coverage": {"passed": False}},
            },
        },
    )

    # Task 3: Reviewer pass ran and found issues (should be in tasks_with_reviewer)
    task_with_findings = Task(
        id=uuid.uuid4(),
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        prompt="test",
        payloads=[{"type": "create", "prompt": "do something"}],
        context={
            "_first_attempt_succeeded": False,
            "_self_review_outcome": {
                "reviewer_findings": [{"category": "test-coverage", "severity": "MAJOR"}],
                "check_results": {"test-coverage": {"passed": False}},
            },
            "_first_attempt_rejection_categories": {"test-coverage": 1},
        },
    )

    result = _compute_fresh_reviewer_effectiveness([
        task_no_review,
        task_clean_reviewed,
        task_with_findings,
    ])

    # Verify the split:
    # - baseline should only count task_no_review (1 task at 100% approval)
    # - tasks_with_reviewer should only count task_with_findings (1 task at 0% approval)
    # - task_clean_reviewed should be excluded from both calculations
    assert result["fresh_reviewer_sample_size"] == 1
    assert result["fresh_reviewer_first_attempt_approval_rate"] == 0.0  # task_with_findings: approved=False
    assert result["baseline_first_attempt_approval_rate"] == 1.0  # task_no_review: approved=True
