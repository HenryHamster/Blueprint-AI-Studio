"""Tests for playbook archive, capacity management, and entry retirement."""

import uuid
from datetime import timedelta

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


# ─────────────────────────────────────────────────────────────────────────────
# Tests for capacity-aware cleanup
# ─────────────────────────────────────────────────────────────────────────────


def test_archive_least_useful_entry_removes_and_archives():
    """_archive_least_useful_entry moves the entry with lowest confirmed_count to playbook_archive."""
    from app.playbook_capacity import _archive_least_useful_entry

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
    from app.playbook_capacity import _archive_least_useful_entry

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
    from app.playbook_capacity import _archive_least_useful_entry

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
    from app.playbook_capacity import (
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
    result = _archive_least_useful_entry(doc)

    cap = runtime_config.get("playbook_archive_max", _PLAYBOOK_ARCHIVE_MAX_DEFAULT)
    assert len(result.get("playbook_archive", [])) <= cap


def test_archive_cap_respects_runtime_config_override():
    """playbook_archive_max runtime override replaces the built-in default."""
    from app.playbook_capacity import _archive_least_useful_entry
    import app.shared_state as _ss

    custom_cap = 5
    existing_archive = [
        {"_archived_key": f"playbook/old-{i}", "content": "x"}
        for i in range(custom_cap + 3)
    ]
    doc = {
        "playbook_archive": existing_archive,
        "playbook/victim": {
            "content": "to be archived",
            "confirmed_count": 0,
            "updated_at": _utcnow().isoformat(),
        },
    }

    prev = _ss.runtime_config.get("playbook_archive_max")
    _ss.runtime_config["playbook_archive_max"] = custom_cap
    try:
        result = _archive_least_useful_entry(doc)
    finally:
        if prev is None:
            _ss.runtime_config.pop("playbook_archive_max", None)
        else:
            _ss.runtime_config["playbook_archive_max"] = prev

    assert len(result.get("playbook_archive", [])) <= custom_cap


def test_archive_uses_rate_over_raw_count_when_enough_observations():
    """With enough observations, a low-rate entry is archived over a zero-count entry
    when its raw confirmed_count is still high."""
    from app.playbook_capacity import _archive_least_useful_entry

    doc = {
        # High raw confirmed_count but very high followed_count — rate = 1/100
        "playbook/low-performer": {
            "content": "rarely useful",
            "confirmed_count": 1,
            "followed_count": 100,
            "updated_at": _utcnow().isoformat(),
        },
        # Zero raw confirmed_count but also zero followed_count — never tried
        "playbook/untried": {
            "content": "never tried",
            "confirmed_count": 0,
            "followed_count": 0,
            "updated_at": _utcnow().isoformat(),
        },
    }
    result = _archive_least_useful_entry(doc)
    assert "playbook/low-performer" not in result
    archive = result.get("playbook_archive", [])
    assert len(archive) == 1
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

    doc = {"playbook/tip": {"content": "x", "confirmed_count": 0}}
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


# ─────────────────────────────────────────────────────────────────────────────
# Tests for maybe_archive_for_capacity
# ─────────────────────────────────────────────────────────────────────────────


def test_maybe_archive_for_capacity_triggers_at_threshold():
    """maybe_archive_for_capacity archives when combined size exceeds 30KB."""
    from app.playbook_capacity import (
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

    from app.playbook_capacity import _playbook_size_bytes

    current_size = _playbook_size_bytes(doc)

    # If already over capacity, archiving should trigger and loop until under cap
    if current_size >= _PLAYBOOK_CAPACITY_BYTES:
        from app.playbook_capacity import _playbook_size_bytes

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
    from app.playbook_capacity import maybe_archive_for_capacity

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
    from app.playbook_capacity import (
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
    from app.playbook_capacity import (
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
    from app.playbook_capacity import (
        maybe_archive_for_capacity,
        _PLAYBOOK_CAPACITY_BYTES,
    )
    from app.playbook_learning import _is_auto_playbook_suppressed

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
    from app.playbook_learning import capture_success_pattern
    from app.playbook_capacity import _PLAYBOOK_CAPACITY_BYTES

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

    from app.playbook_capacity import _playbook_size_bytes

    size_before = _playbook_size_bytes(project.design_doc)
    assert size_before > _PLAYBOOK_CAPACITY_BYTES, (
        "Doc must be over capacity before the test"
    )

    key = await capture_success_pattern(task, db_session)
    await db_session.flush()

    assert key == "success-create-small", "New entry should have been created"
    doc = project.design_doc or {}
    # playbook_archive is now stored in project_telemetry, not design_doc.
    assert "playbook_archive" not in doc, (
        "design_doc must not contain playbook_archive after migration"
    )
    from app.project_telemetry import get_project_telemetry
    archive = await get_project_telemetry(db_session, project.id, "playbook_archive") or []
    assert len(archive) >= 1, (
        "At least one entry should have been archived for capacity (in telemetry)"
    )


@pytest.mark.asyncio
async def test_capture_success_archive_design_doc_clean(db_session: AsyncSession):
    """capture_success_pattern must not persist playbook_archive in design_doc."""
    from app.playbook_learning import capture_success_pattern

    project = await _make_project(db_session, design_doc={})
    await _make_completed_task(db_session, project.id, task_type="create", scope="small")
    await _make_completed_task(db_session, project.id, task_type="create", scope="small")
    task = await _make_completed_task(db_session, project.id, task_type="create", scope="small")

    await capture_success_pattern(task, db_session)
    await db_session.flush()

    doc = project.design_doc or {}
    assert "playbook_archive" not in doc, (
        "design_doc must be free of playbook_archive after capture_success_pattern"
    )


@pytest.mark.asyncio
async def test_auto_playbook_suppressed_via_telemetry_archive(db_session: AsyncSession):
    """_is_auto_playbook_suppressed must read archive from project_telemetry.

    Proof: place a recently-archived entry ONLY in project_telemetry (not in
    design_doc); then verify that the auto-playbook path still suppresses
    re-creation of that entry.

    The signature "test-pattern" maps to key "auto-test-pattern" via
    _signature_to_key (fallback path: no traceback, no exception, returns the
    raw string; _signature_to_key prefixes "auto-").
    """
    from app.playbook_auto import _maybe_auto_playbook, _signature_to_key
    from app.project_telemetry import get_project_telemetry
    from app.models import ProjectTelemetry, Task, TaskStatus

    # Use a short, deterministic error_summary so the generated key is predictable.
    error_summary = "test-pattern"
    generated_key = _signature_to_key(error_summary)  # "auto-test-pattern"
    full_key = f"playbook/{generated_key}"

    project = await _make_project(db_session, design_doc={})

    # Seed archive ONLY in project_telemetry (design_doc has none).
    # Use ORM directly to avoid PostgreSQL-specific pg_insert dialect issues.
    from app.models import _utcnow as _m_utcnow
    recent_archived_at = (_m_utcnow() - timedelta(days=1)).isoformat()
    seed_archive = [
        {
            "_archived_key": full_key,
            "_archived_at": recent_archived_at,
            "_retirement_reason": "capacity",
        }
    ]
    telemetry_row = ProjectTelemetry(
        project_id=project.id,
        key="playbook_archive",
        value=seed_archive,
        updated_at=_m_utcnow(),
    )
    db_session.add(telemetry_row)
    await db_session.flush()

    # Create enough failure tasks to exceed the auto-playbook threshold (3).
    # The tasks must have _failure_signature matching what _maybe_auto_playbook computes.
    for _ in range(3):
        t = Task(
            project_id=project.id,
            title="Failed task",
            payloads=[{"type": "create", "prompt": "do something"}],
            status=TaskStatus.FAILED,
            context={"scope": "small", "_failure_signature": error_summary},
        )
        db_session.add(t)
    await db_session.flush()

    task = Task(
        project_id=project.id,
        title="Current failing task",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.FAILED,
        context={"scope": "small"},
    )
    db_session.add(task)
    await db_session.flush()

    result = await _maybe_auto_playbook(task, "CODE_ERROR", error_summary, db_session)

    # Entry must be suppressed because it was archived within the cooldown window.
    assert result is None, (
        f"_maybe_auto_playbook must not create key '{generated_key}' suppressed "
        f"by telemetry archive; got result={result!r}"
    )
    # Confirm the archive in telemetry is still intact.
    stored = await get_project_telemetry(db_session, project.id, "playbook_archive")
    assert stored is not None
    assert any(e.get("_archived_key") == full_key for e in stored)


@pytest.mark.asyncio
async def test_playbook_delete_tombstone_in_telemetry_not_design_doc(db_session: AsyncSession):
    """admin_playbook_delete must write the tombstone to project_telemetry, not design_doc."""
    from app.project_telemetry import (
        get_project_telemetry,
        set_project_telemetry,
        strip_migrated_doc_keys,
    )
    from app.playbook_storage import record_playbook_tombstone
    from sqlalchemy.orm.attributes import flag_modified

    full_key = "playbook/my-tip"
    project = await _make_project(
        db_session,
        design_doc={
            full_key: {"content": "operator tip", "confirmed_count": 0},
        },
    )
    await db_session.flush()

    # Simulate what admin_playbook_delete does.
    doc = dict(project.design_doc or {})
    existing_archive = await get_project_telemetry(
        db_session, project.id, "playbook_archive", design_doc_fallback=doc
    )
    doc["playbook_archive"] = list(existing_archive or [])
    del doc[full_key]
    record_playbook_tombstone(doc, full_key, archived_at=_utcnow().isoformat())
    updated_archive = list(doc.get("playbook_archive") or [])
    strip_migrated_doc_keys(doc)
    project.design_doc = doc
    flag_modified(project, "design_doc")
    db_session.add(project)
    await set_project_telemetry(db_session, project.id, "playbook_archive", updated_archive)
    await db_session.flush()

    # design_doc must be free of playbook_archive.
    assert "playbook_archive" not in (project.design_doc or {}), (
        "design_doc must not contain playbook_archive after delete"
    )
    # Tombstone must be readable from telemetry.
    archive = await get_project_telemetry(db_session, project.id, "playbook_archive") or []
    tombstones = [
        e for e in archive
        if e.get("_archived_key") == full_key
        and e.get("_retirement_reason") == "operator_deleted"
    ]
    assert len(tombstones) == 1, (
        "Tombstone must be present in project_telemetry.playbook_archive"
    )
