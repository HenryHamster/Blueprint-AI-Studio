"""Playbook test suite (split).

Tests exercising the app.playbook_lifecycle surface, split out of the former
testing/test_playbook_auto.py monolith. Behavior-preserving: test bodies
are copied verbatim; only file boundaries and shared-helper imports change.
"""

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task, TaskStatus
from testing._playbook_test_helpers import _make_project


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


