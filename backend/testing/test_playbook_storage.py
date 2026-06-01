import uuid
from datetime import timedelta

import pytest

from app.models import Task, TaskStatus, _utcnow
from testing._playbook_test_helpers import _make_project


def test_archive_playbook_entry_moves_entry_and_trims_archive(monkeypatch):
    from app import playbook_storage as storage
    from app.shared_state import runtime_config

    now_iso = _utcnow().isoformat()
    doc = {
        "playbook_archive": [
            {"_archived_key": "playbook/old-1", "_archived_at": now_iso},
            {"_archived_key": "playbook/old-2", "_archived_at": now_iso},
        ],
        "playbook/tip": {
            "content": "tip",
            "confirmed_count": 1,
            "updated_at": now_iso,
        },
    }

    prev_max = runtime_config.get("playbook_archive_max")
    runtime_config["playbook_archive_max"] = 2
    try:
        archived = storage.archive_playbook_entry(
            doc,
            "playbook/tip",
            archived_at=now_iso,
            retirement_reason="manual_archive",
        )
    finally:
        if prev_max is None:
            runtime_config.pop("playbook_archive_max", None)
        else:
            runtime_config["playbook_archive_max"] = prev_max

    assert archived is True
    assert "playbook/tip" not in doc
    archive = doc["playbook_archive"]
    assert len(archive) == 2
    assert archive[-1]["_archived_key"] == "playbook/tip"
    assert archive[-1]["_retirement_reason"] == "manual_archive"
    assert archive[0]["_archived_key"] == "playbook/old-2"


def test_record_playbook_tombstone_adds_deleted_key_once():
    from app.playbook_storage import record_playbook_tombstone

    now_iso = _utcnow().isoformat()
    doc = {}

    record_playbook_tombstone(doc, "playbook/tip", archived_at=now_iso)
    record_playbook_tombstone(doc, "playbook/tip", archived_at=now_iso)

    deleted_keys = doc["playbook_deleted_keys"]
    assert deleted_keys == ["playbook/tip"]
    archive = doc["playbook_archive"]
    assert len(archive) == 2
    assert all(entry["_archived_key"] == "playbook/tip" for entry in archive)
    assert all(entry["_retirement_reason"] == "operator_deleted" for entry in archive)


def test_playbook_learning_reexports_storage_helpers():
    import app.playbook_learning as learning
    import app.playbook_storage as storage

    assert learning.maybe_archive_for_capacity is storage.maybe_archive_for_capacity
    assert learning.finalize_pending_playbook_entries is (
        storage.finalize_pending_playbook_entries
    )
    assert learning.retire_idle_auto_entries is storage.retire_idle_auto_entries


def test_is_auto_playbook_suppressed_respects_recent_tombstone():
    from app.playbook_storage import (
        _is_auto_playbook_suppressed,
        record_playbook_tombstone,
    )

    doc = {}
    record_playbook_tombstone(doc, "playbook/tip", archived_at=_utcnow().isoformat())

    assert _is_auto_playbook_suppressed(doc, "playbook/tip") is True
    assert (
        _is_auto_playbook_suppressed(
            {
                "playbook_archive": [
                    {
                        "_archived_key": "playbook/other",
                        "_archived_at": (_utcnow() - timedelta(days=90)).isoformat(),
                    }
                ]
            },
            "playbook/tip",
        )
        is False
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
