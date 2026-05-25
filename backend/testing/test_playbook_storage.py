from datetime import timedelta

from app.models import _utcnow


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
