"""Playbook test suite (split).

Tests exercising the app.playbook_auto surface, split out of the former
testing/test_playbook_auto.py monolith. Behavior-preserving: test bodies
are copied verbatim; only file boundaries and shared-helper imports change.
"""

import logging
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from app.routers.agent_status import _check_recurring_failures
from app.task_locking import DesignDocSizeLimitExceeded

from testing._playbook_test_helpers import (
    _make_failed_task,
    _make_project,
)


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_review_learning_symbols_reexported_from_playbook_learning():
    import app.playbook_learning as playbook_learning
    import app.review_learning as review_learning

    assert (
        playbook_learning.cache_first_attempt_classification
        is review_learning.cache_first_attempt_classification
    )
    assert (
        playbook_learning.compute_review_effectiveness
        is review_learning.compute_review_effectiveness
    )
    assert (
        playbook_learning.extract_review_feedback_to_playbook
        is review_learning.extract_review_feedback_to_playbook
    )


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
    # The single entry reflects the second call's data: 4 occurrences now
    updated_content = doc2[auto_keys_2[0]]["content"]
    assert "4 times" in updated_content, (
        f"Expected updated content to mention '4 times', got: {updated_content!r}"
    )
    # The key is unchanged (updated in place, not a new key)
    assert auto_keys_2[0] == auto_keys_1[0], (
        f"Expected same key after update, key changed from {auto_keys_1[0]!r} to {auto_keys_2[0]!r}"
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


@pytest.mark.asyncio
async def test_design_doc_size_limit_swallow_logs_warning(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
):
    """When with_design_doc_locked raises DesignDocSizeLimitExceeded,
    _maybe_auto_playbook logs a warning and returns None (preserving the
    degrade-gracefully behavior)."""
    from app.playbook_learning import _maybe_auto_playbook, _extract_failure_signature

    error_with_traceback = (
        "Traceback (most recent call last):\n"
        '  File "backend/app/auth.py", line 10, in <module>\n'
        "ImportError: cannot import name 'oauth_client'\n"
    )

    project = await _make_project(db_session)
    sig = _extract_failure_signature("CODE_ERROR", error_with_traceback)

    # Create 2 prior FAILED tasks so the threshold (3) is met
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

    project_id = project.id

    with patch(
        "app.playbook_auto.with_design_doc_locked",
        side_effect=DesignDocSizeLimitExceeded(
            project_id=project_id, old_size=400_000, new_size=500_100, ceiling=500_000
        ),
    ):
        with caplog.at_level(logging.WARNING, logger="app.playbook_auto"):
            result = await _maybe_auto_playbook(
                t3, "CODE_ERROR", error_with_traceback, db_session
            )

    # Return value unchanged (None = no entry created)
    assert result is None, "Should return None on size cap hit"

    # Warning was emitted with attributable context
    assert any(
        "design_doc size limit exceeded" in rec.message
        and str(project_id) in rec.message
        for rec in caplog.records
    ), (
        "Expected a logger.warning with project_id and 'design_doc size limit exceeded' "
        f"in caplog records: {[r.message for r in caplog.records]}"
    )
