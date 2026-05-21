"""Tests for constants and helper methods defined in app.models."""

import uuid

from app.models import (
    CYCLE_META_MEMOS_AVAILABLE,
    CYCLE_META_MEMOS_CITED,
    StrategicCycle,
)

_PROJECT_ID = uuid.uuid4()


def _make_cycle(cycle_metadata=None):
    return StrategicCycle(
        id=str(uuid.uuid4()),
        project_id=_PROJECT_ID,
        cycle_metadata=cycle_metadata if cycle_metadata is not None else {},
    )


def test_cycle_meta_constants_have_expected_values():
    assert CYCLE_META_MEMOS_AVAILABLE == "memos_available"
    assert CYCLE_META_MEMOS_CITED == "memos_cited"


def test_memo_stats_returns_zeros_when_metadata_empty():
    cycle = _make_cycle({})
    stats = cycle.memo_stats()
    assert stats[CYCLE_META_MEMOS_AVAILABLE] == 0
    assert stats[CYCLE_META_MEMOS_CITED] == 0


def test_memo_stats_returns_zeros_when_metadata_none():
    cycle = _make_cycle(None)
    stats = cycle.memo_stats()
    assert stats[CYCLE_META_MEMOS_AVAILABLE] == 0
    assert stats[CYCLE_META_MEMOS_CITED] == 0


def test_memo_stats_reads_values_from_metadata():
    cycle = _make_cycle(
        {
            CYCLE_META_MEMOS_AVAILABLE: 7,
            CYCLE_META_MEMOS_CITED: 3,
        }
    )
    stats = cycle.memo_stats()
    assert stats[CYCLE_META_MEMOS_AVAILABLE] == 7
    assert stats[CYCLE_META_MEMOS_CITED] == 3


def test_memo_stats_coerces_to_int():
    cycle = _make_cycle(
        {
            CYCLE_META_MEMOS_AVAILABLE: "4",
            CYCLE_META_MEMOS_CITED: "1",
        }
    )
    stats = cycle.memo_stats()
    assert stats[CYCLE_META_MEMOS_AVAILABLE] == 4
    assert stats[CYCLE_META_MEMOS_CITED] == 1
