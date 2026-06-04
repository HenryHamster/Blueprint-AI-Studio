"""Tests for backend/app/payload_schemas.py:pack_under_budget.

The packer is the long-term fix for strategic-spawn payload bloat. It hard-
caps a payload at *budget* bytes by tier-priority, summarizing or dropping
items as needed and surfacing what changed via ``_packer_meta`` so the
agent prompt can ask the operator (or fetch endpoints) for the dropped state.
"""

import json
import logging

import pytest

from app.payload_schemas import (
    PayloadTooLargeError,
    pack_under_budget,
    validate_internal_payload,
)


def _size(value) -> int:
    return len(json.dumps(value, default=str).encode())


def test_packs_all_parts_under_budget():
    """Small payload, generous budget — every key included, no trims/drops."""
    payload = {
        "type": "strategic_review",
        "vision": {"purpose": "demo"},
        "directions": ["d1", "d2"],
    }
    schema = {
        "type": {"priority": "critical"},
        "vision": {"priority": "high"},
        "directions": {"priority": "medium"},
    }
    out = pack_under_budget(payload, schema, budget=10_000)
    assert out["type"] == "strategic_review"
    assert out["vision"] == {"purpose": "demo"}
    assert out["directions"] == ["d1", "d2"]
    assert out["_packer_meta"]["trimmed"] == []
    assert out["_packer_meta"]["dropped"] == []
    assert out["_packer_meta"]["budget_total"] == 10_000
    assert out["_packer_meta"]["budget_used"] > 0


def test_drops_lowest_priority_first():
    """Tight budget with one high + one low. Low gets dropped, high survives."""
    payload = {
        "must_keep": "x" * 200,
        "nice_to_have": "y" * 200,
    }
    schema = {
        "must_keep": {"priority": "high"},
        "nice_to_have": {"priority": "low"},
    }
    # ~204 bytes for must_keep alone; 250-byte budget squeezes out nice_to_have
    out = pack_under_budget(payload, schema, budget=250)
    assert "must_keep" in out
    assert "nice_to_have" not in out
    assert out["_packer_meta"]["dropped"] == [
        {"key": "nice_to_have", "reason": "exceeds remaining budget (no summarize_fn)"},
    ]


def test_summarize_invoked_when_full_overflows():
    """Over-budget value gets trimmed via summarize; trimmed value is what's stored."""
    payload = {
        "items": list(range(1000)),  # ~3.9KB
    }
    schema = {
        "items": {"priority": "medium", "summarize": lambda v: v[:10]},
    }
    out = pack_under_budget(payload, schema, budget=200)
    assert out["items"] == list(range(10))
    assert out["_packer_meta"]["trimmed"] == [
        {"key": "items", "kind": "list_trimmed", "shown": 10, "total": 1000},
    ]
    assert out["_packer_meta"]["dropped"] == []


def test_summarize_still_overflows_drops_part():
    """Trimmed value still too large — part absent, recorded in dropped."""
    payload = {
        "huge": "x" * 5000,
    }
    schema = {
        # summarize trims to 4000 chars — still too big for a 100-byte budget
        "huge": {"priority": "medium", "summarize": lambda v: v[:4000]},
    }
    out = pack_under_budget(payload, schema, budget=100)
    assert "huge" not in out
    assert out["_packer_meta"]["dropped"] == [
        {"key": "huge", "reason": "exceeds remaining budget even after summarize"},
    ]


def test_critical_overflow_raises():
    """Critical-tier sum exceeds budget — raise rather than silently drop."""
    payload = {
        "must_have_a": "x" * 5000,
        "must_have_b": "y" * 5000,
    }
    schema = {
        "must_have_a": {"priority": "critical"},
        "must_have_b": {"priority": "critical"},
    }
    with pytest.raises(PayloadTooLargeError):
        pack_under_budget(payload, schema, budget=6_000)


def test_smaller_within_tier_packs_first():
    """Tiebreak: within same tier, smaller items pack first to maximize count.

    Without (size asc) tiebreak, insertion order would decide which fits.
    With it, the small item always fits when only one slot remains.
    """
    payload = {
        "big": "x" * 200,
        "small": "y" * 30,
    }
    schema = {
        "big": {"priority": "high"},
        "small": {"priority": "high"},
    }
    # 100 byte budget: only the small fits cleanly; big gets dropped.
    out = pack_under_budget(payload, schema, budget=100)
    assert "small" in out
    assert "big" not in out
    assert any(d["key"] == "big" for d in out["_packer_meta"]["dropped"])


def test_unknown_priority_logged_and_treated_as_low(caplog):
    """Schema with bogus priority falls back to 'low' with a warning."""
    payload = {"x": "data", "y": "data"}
    schema = {
        "x": {"priority": "high"},
        "y": {"priority": "potatoes"},  # bogus
    }
    with caplog.at_level(logging.WARNING):
        out = pack_under_budget(payload, schema, budget=10_000)
    assert "x" in out and "y" in out
    assert any("unknown priority" in rec.message.lower() for rec in caplog.records)


def test_missing_schema_key_logged_and_defaults_low(caplog):
    """Payload key absent from schema gets default low priority + warning."""
    payload = {"known": "a", "mystery": "b"}
    schema = {"known": {"priority": "high"}}
    with caplog.at_level(logging.WARNING):
        out = pack_under_budget(payload, schema, budget=10_000)
    assert "known" in out and "mystery" in out
    assert any("no schema entry" in rec.message.lower() for rec in caplog.records)


def test_non_serializable_value_dropped_not_raised():
    """Circular/weird values land in dropped, packer doesn't crash."""

    class WeirdObject:
        def __repr__(self):
            return "weird"

    # Make it actually unserializable — json.dumps with default=str handles
    # most things, but a recursive list will trip ValueError.
    recursive: list = []
    recursive.append(recursive)

    payload = {
        "fine": "ok",
        "broken": recursive,
    }
    schema = {
        "fine": {"priority": "high"},
        "broken": {"priority": "low"},
    }
    out = pack_under_budget(payload, schema, budget=10_000)
    assert out["fine"] == "ok"
    assert "broken" not in out
    assert any(d["key"] == "broken" for d in out["_packer_meta"]["dropped"])


def test_zero_budget_raises():
    """Budget=0 is API misuse, raise ValueError early."""
    with pytest.raises(ValueError, match="budget must be positive"):
        pack_under_budget({"x": "y"}, {"x": {"priority": "high"}}, budget=0)


def test_packer_meta_preserved_through_validate_internal_payload():
    """Round-trip through the validator keeps _packer_meta intact.

    This pins Pydantic's extra='allow' behavior — without it, _packer_meta
    would silently disappear and the agent would never see what was trimmed.
    """
    # ResearchPayload.directions is list[dict] (each item describes a direction);
    # the fail-closed validator (3eeb693) rejects bare strings here. The test
    # only cares that _packer_meta survives validation, so any well-typed
    # directions value works.
    payload = pack_under_budget(
        {
            "type": "research",
            "research_mode": "tactical",
            "base_branch": "main",
            "directions": [{"id": "d1"}],
        },
        {
            "type": {"priority": "critical"},
            "research_mode": {"priority": "critical"},
            "base_branch": {"priority": "critical"},
            "directions": {"priority": "high"},
        },
        budget=10_000,
    )
    assert "_packer_meta" in payload
    validated = validate_internal_payload(payload, "test:packer_meta_roundtrip")
    assert "_packer_meta" in validated
    assert validated["_packer_meta"]["budget_total"] == 10_000


def test_pre_trim_marker_surfaced_into_packer_meta():
    """A section pre-trimmed before the packer ran is reported in _packer_meta.

    The strategic problem_model has its per-gap append-only memory trimmed in
    strategic_spawning._agent_view_problem_model — BEFORE pack_under_budget
    runs. That trim leaves a ``_gap_memory_trimmed`` marker on the section.
    The packer must reflect it in ``_packer_meta.gap_memory_trimmed`` so the
    agent's existing fetch-decision channel reports it even though the packer
    itself shipped the section in full (no summarize/drop happened).
    """
    marker = [{"gap_id": "g-auth", "fields": {"history": {"shown": 5, "total": 31}}}]
    payload = {
        "problem_model": {"gaps": [{"id": "g-auth"}], "_gap_memory_trimmed": marker},
    }
    schema = {"problem_model": {"priority": "high"}}
    out = pack_under_budget(payload, schema, budget=10_000)
    # Section shipped in full — not trimmed or dropped by the packer itself.
    assert "problem_model" in out
    assert out["_packer_meta"]["trimmed"] == []
    assert out["_packer_meta"]["dropped"] == []
    # ...but the pre-trim IS surfaced in the metadata channel.
    assert out["_packer_meta"]["gap_memory_trimmed"] == [
        {"key": "problem_model", "gaps": marker},
    ]


def test_no_pre_trim_marker_yields_empty_list():
    """When no section carries a pre-trim marker, the field is an empty list."""
    payload = {"problem_model": {"gaps": [{"id": "g-auth"}]}}
    schema = {"problem_model": {"priority": "high"}}
    out = pack_under_budget(payload, schema, budget=10_000)
    assert out["_packer_meta"]["gap_memory_trimmed"] == []


def test_empty_pre_trim_marker_not_surfaced():
    """An empty/false marker means nothing was actually trimmed — don't report it."""
    payload = {"problem_model": {"gaps": [], "_gap_memory_trimmed": []}}
    schema = {"problem_model": {"priority": "high"}}
    out = pack_under_budget(payload, schema, budget=10_000)
    assert out["_packer_meta"]["gap_memory_trimmed"] == []


def test_summarize_returning_larger_value_falls_back_to_original(caplog):
    """If summarize_fn returns something larger (caller bug), use original.

    The packer should never grow the payload by 'trimming'. The summarize
    is only called when the original overflows, so the test forces that
    overflow first, then verifies the defensive fallback path warns and
    proceeds with the original-size check (which here still drops it).
    """
    payload = {"items": [1, 2, 3, 4, 5]}  # ~16B serialized
    schema = {
        "items": {
            "priority": "medium",
            "summarize": lambda v: v + ["extra"] * 1000,  # returns LARGER
        },
    }
    # Budget too small for original (forces summarize call); summarize
    # returns larger; defensive falls back to original; original still
    # doesn't fit → drop, with "grew" warning logged.
    with caplog.at_level(logging.WARNING):
        out = pack_under_budget(payload, schema, budget=10)
    assert "items" not in out
    assert any("grew" in rec.message.lower() for rec in caplog.records)
