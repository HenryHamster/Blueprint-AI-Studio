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
    assert len(out["_packer_meta"]["dropped"]) == 1
    d = out["_packer_meta"]["dropped"][0]
    assert d["key"] == "nice_to_have"
    assert d["reason"] == "exceeds remaining budget (no summarize_fn)"
    assert d["priority"] == "low"
    assert d["bytes"] == _size("y" * 200)


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
    assert len(out["_packer_meta"]["trimmed"]) == 1
    t = out["_packer_meta"]["trimmed"][0]
    assert t["key"] == "items"
    assert t["kind"] == "list_trimmed"
    assert t["shown"] == 10
    assert t["total"] == 1000
    assert t["bytes_original"] > t["bytes_trimmed"] > 0
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
    assert len(out["_packer_meta"]["dropped"]) == 1
    d = out["_packer_meta"]["dropped"][0]
    assert d["key"] == "huge"
    assert d["reason"] == "exceeds remaining budget even after summarize"
    assert d["priority"] == "medium"
    assert d["bytes"] == _size("x" * 5000)


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


def test_floor_key_survives_when_higher_volume_siblings_exhaust_budget():
    """A key with a ``floor`` callback always ships at least its floored value,
    even when same-tier siblings that sort ahead would otherwise exhaust the
    budget. Other recoverable context degrades/drops before the floored key.

    This is the in-payload delivery guarantee: without the floor reservation,
    the big high-tier siblings pack first and ``model`` (whose full size sorts
    it last in the high tier) would be dropped entirely.
    """
    payload = {
        "model": {"gaps": ["g"] * 400},  # big full value, sorts last in high tier
        "sibling_a": "x" * 600,
        "sibling_b": "y" * 600,
    }
    schema = {
        # floor strips the model down to a tiny (~32B) guaranteed working set
        "model": {
            "priority": "high",
            "summarize": lambda v: {"gaps": v["gaps"][:1], "floored": True},
            "floor": lambda v: {"gaps": v["gaps"][:1], "floored": True},
        },
        "sibling_a": {"priority": "high"},
        "sibling_b": {"priority": "high"},
    }
    # Budget is tight: the two ~602B siblings fit with only ~6B to spare — too
    # little for the ~32B floored model. WITHOUT the reservation the siblings
    # win the race and the model is dropped; WITH it a sibling drops instead.
    out = pack_under_budget(payload, schema, budget=1210)
    assert "model" in out, "floored key was starved out by higher-volume siblings"
    assert out["model"] == {"gaps": ["g"], "floored": True}
    dropped_keys = {d["key"] for d in out["_packer_meta"]["dropped"]}
    assert "model" not in dropped_keys
    # A recoverable sibling degraded (dropped) before the floored model vanished.
    assert "sibling_a" not in out or "sibling_b" not in out


def test_floor_used_when_summarize_absent_or_raises():
    """When a floor key has no summarize_fn (or it raises), the floor still
    ships rather than the key being dropped."""
    payload = {"model": {"big": "z" * 5000}}
    schema = {
        "model": {
            "priority": "high",
            "floor": lambda v: {"floored": True},
        },
    }
    out = pack_under_budget(payload, schema, budget=200)
    assert out["model"] == {"floored": True}
    assert any(t["key"] == "model" and t["kind"] == "floored" for t in out["_packer_meta"]["trimmed"])


def test_floor_full_value_ships_when_budget_allows():
    """The floor is a guarantee, not a cap — when the budget fits the full
    value, the full (un-floored) value ships."""
    payload = {"model": {"gaps": ["g1", "g2", "g3"]}}
    schema = {
        "model": {
            "priority": "high",
            "floor": lambda v: {"gaps": v["gaps"][:1]},
        },
    }
    out = pack_under_budget(payload, schema, budget=10_000)
    assert out["model"] == {"gaps": ["g1", "g2", "g3"]}
    assert out["_packer_meta"]["trimmed"] == []


def test_floor_equal_to_full_value_reserves_whole_value_off_the_top():
    """When the ``floor`` callback returns the FULL value (identity floor), the
    packer reserves the whole value's bytes off the top of the budget — so the
    full value ships intact (un-summarized) while higher-volume siblings that
    sort ahead must yield first.

    This is the reworked strategic ``problem_model`` guarantee: the floor equals
    the full agent-view model, so the model ordinarily ships WHOLE and recoverable
    siblings degrade/drop before it is ever reduced. The summarize callback (which
    would shrink the model) must NOT fire on this path.
    """
    full_model = {"gaps": ["g"] * 300}  # big, sorts last in the high tier
    summarize_calls = []

    def summarize(v):
        summarize_calls.append(v)
        return {"gaps": v["gaps"][:1], "floored": True}

    payload = {
        "model": full_model,
        "sibling_a": "a" * 1500,
        "sibling_b": "b" * 1500,
    }
    schema = {
        # Identity floor: reserve the WHOLE value (mirrors the production
        # _agent_view_problem_model_floor returning the full agent-view model).
        "model": {
            "priority": "high",
            "summarize": summarize,
            "floor": lambda v: v,
        },
        "sibling_a": {"priority": "high"},
        "sibling_b": {"priority": "high"},
    }
    model_bytes = len(json.dumps(full_model, default=str).encode())
    # Budget fits the whole model plus only ONE ~1.5KB sibling — the other must
    # yield because the model's full bytes are reserved off the top.
    out = pack_under_budget(payload, schema, budget=model_bytes + 1800)

    # The WHOLE value shipped (not the floored/summarized 1-gap form).
    assert out["model"] == full_model, "the full value must ship, un-summarized"
    assert summarize_calls == [], "summarize must NOT fire when the full value fits"
    assert out["_packer_meta"]["trimmed"] == []
    # A higher-volume sibling yielded first.
    dropped_keys = {d["key"] for d in out["_packer_meta"]["dropped"]}
    assert "model" not in dropped_keys
    assert "sibling_a" in dropped_keys or "sibling_b" in dropped_keys, (
        "a higher-volume sibling must drop before the reserved full model"
    )


def test_full_model_floor_does_not_starve_critical_keys(caplog):
    """When the full-model floor reservation fits the budget on its own but the
    model PLUS the always-critical identity/vision keys would not, the packer
    must drop the floor guarantee (so the model's summarize fires) rather than
    starve a critical key into a hard PayloadTooLargeError.

    Reproduces the near-budget band: model ~fits-alone, model+vision overflows.
    The floor reservation is ``reserved_remaining <= budget`` (guarantee kept by
    the old condition), yet ``vision`` (critical, no summarize/floor) sorts first
    and would see ``remaining = budget - reserved`` too small to fit. The fix
    accounts for non-floored critical keys' full bytes in the drop condition, so
    the model degrades loudly via summarize and the critical key still ships.
    """
    big_model = {"gaps": ["g"] * 4000}
    model_bytes = len(json.dumps(big_model, default=str).encode())
    vision = "v" * 4000
    vision_bytes = len(json.dumps(vision, default=str).encode())
    summarize_calls = []

    def summarize(v):
        summarize_calls.append(v)
        return {"gaps": v["gaps"][:1], "_agent_view_floor_applied": {"floored": True}}

    payload = {"vision": vision, "model": big_model}
    schema = {
        "vision": {"priority": "critical"},
        "model": {
            "priority": "high",
            "summarize": summarize,
            "floor": lambda v: v,  # identity floor: reserves the WHOLE model
        },
    }
    # Budget fits the model alone (reservation <= budget) but NOT model+vision.
    budget = model_bytes + vision_bytes - 50
    with caplog.at_level(logging.WARNING):
        out = pack_under_budget(payload, schema, budget=budget)

    # Critical key shipped — no PayloadTooLargeError.
    assert out["vision"] == vision
    # Floor guarantee was dropped, so the model degraded via summarize (loudly).
    assert summarize_calls, "summarize must fire when the floor guarantee is dropped"
    assert out["model"].get("_agent_view_floor_applied"), (
        "the degraded model must carry the loud floor-applied marker"
    )
    assert any(t["key"] == "model" and t["kind"] == "summarized" for t in out["_packer_meta"]["trimmed"])
    assert any(
        "exceed budget" in rec.message and "critical" in rec.message
        for rec in caplog.records
    ), "the floor-drop must log that critical headroom forced the drop"


def test_degraded_floor_reservation_survives_mixed_high_contention():
    """If the full-floor reservation must be dropped to preserve critical keys,
    a smaller summarized reservation still protects the key from same-tier
    siblings consuming the rest of the budget first.

    Regression for the mixed critical/high band: the old fallback cleared
    ``floors`` entirely, so two smaller high-priority siblings packed ahead of
    ``problem_model`` and left too little room for even its summarized form,
    silently dropping it. The packer must instead reserve the degraded form so
    the model ships loudly summarized and at least one sibling yields first.
    """
    problem_model = {"gaps": ["g" * 200 for _ in range(20)]}

    def summarize(v):
        return {
            "gaps": v["gaps"][:1],
            "_agent_view_floor_applied": {"floored": True},
        }

    summarized_model = summarize(problem_model)
    sibling_a = {"body": "a" * 220}
    sibling_b = {"body": "b" * 220}
    vision = {"purpose": "v" * 600}

    summarized_size = _size(summarized_model)
    sibling_a_size = _size(sibling_a)
    sibling_b_size = _size(sibling_b)
    critical_size = _size("strategic_review") + _size(vision)

    budget = critical_size + summarized_size + sibling_a_size + sibling_b_size - 1
    assert budget < critical_size + _size(problem_model), (
        "fixture must force the full-floor reservation to overflow with critical keys"
    )

    payload = {
        "type": "strategic_review",
        "vision": vision,
        "sibling_a": sibling_a,
        "sibling_b": sibling_b,
        "problem_model": problem_model,
    }
    schema = {
        "type": {"priority": "critical"},
        "vision": {"priority": "critical"},
        "sibling_a": {"priority": "high"},
        "sibling_b": {"priority": "high"},
        "problem_model": {
            "priority": "high",
            "summarize": summarize,
            "floor": lambda v: v,
        },
    }

    out = pack_under_budget(payload, schema, budget=budget)

    assert "problem_model" in out, "degraded reservation must keep problem_model delivered"
    assert out["problem_model"] == summarized_model
    assert any(t["key"] == "problem_model" and t["kind"] == "summarized" for t in out["_packer_meta"]["trimmed"])
    dropped_keys = {d["key"] for d in out["_packer_meta"]["dropped"]}
    assert "problem_model" not in dropped_keys, f"problem_model dropped: {out['_packer_meta']['dropped']}"
    assert {"sibling_a", "sibling_b"} & dropped_keys, (
        "at least one same-tier sibling must yield before the degraded reservation"
    )


def test_degraded_reservation_preserves_floor_without_summarize(caplog):
    """When the full-floor reservation must be dropped to preserve critical keys,
    a floored key with NO usable summarized candidate still keeps a reservation
    for its actual floor value rather than being cleared and silently dropped.

    Regression for the degraded-reservation branch: it previously only reserved
    ``_compute_summarized_candidate(p)``. A key with a valid floor but no usable
    summarized form (no summarize_fn here) produced no degraded candidate, so
    with both floored keys lacking summarizers ``degraded_floors`` stayed empty
    and ``floors`` was cleared entirely — dropping a floor that fits within
    ``budget - critical_full``. The fallback must reserve the floor value itself
    when that is the only viable degraded form.
    """
    model_a = {"gaps": ["a" * 200 for _ in range(20)]}
    model_b = {"gaps": ["b" * 200 for _ in range(20)]}
    floor_a = {"floored": "a"}
    floor_b = {"floored": "b"}
    vision = "v" * 600

    fa_size = _size(floor_a)
    fb_size = _size(floor_b)
    critical_size = _size("strategic_review") + _size(vision)

    # Branch fires: full-floor reservations (fa + fb) plus the critical keys
    # exceed budget by 1. degraded_budget = budget - critical_size = fa + fb - 1,
    # so model_a's floor fits but model_b's does not (greedy boundary).
    budget = critical_size + fa_size + fb_size - 1
    assert fa_size + fb_size + critical_size > budget, (
        "fixture must force the full-floor reservation to overflow with critical keys"
    )

    payload = {
        "type": "strategic_review",
        "vision": vision,
        "model_a": model_a,
        "model_b": model_b,
    }
    schema = {
        "type": {"priority": "critical"},
        "vision": {"priority": "critical"},
        # Both floored keys lack a summarize_fn: the floor value is the ONLY
        # viable degraded reservation form.
        "model_a": {"priority": "high", "floor": lambda v: floor_a},
        "model_b": {"priority": "high", "floor": lambda v: floor_b},
    }

    with caplog.at_level(logging.WARNING):
        out = pack_under_budget(payload, schema, budget=budget)

    # Critical keys shipped — no PayloadTooLargeError.
    assert out["vision"] == vision
    # model_a kept its floor reservation and shipped the floor value (loudly),
    # instead of being cleared and dropped for lack of a summarized candidate.
    assert out["model_a"] == floor_a, "floored key must ship its floor value"
    assert any(t["key"] == "model_a" and t["kind"] == "floored" for t in out["_packer_meta"]["trimmed"])
    dropped_keys = {d["key"] for d in out["_packer_meta"]["dropped"]}
    assert "model_a" not in dropped_keys, f"model_a dropped: {out['_packer_meta']['dropped']}"
    assert any(
        "preserving degraded reservations" in rec.message for rec in caplog.records
    ), "the degraded pass must log that it preserved a floor reservation"


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


def test_floor_survives_when_summarize_raises(caplog):
    """Floor callback preserves a key even when summarize callback fails.

    This is the critical hardening case: when the summarize callback raises
    an exception, the packer catches it and abandons the summarize path. But
    the floor callback is independent, so it still computes and reserves bytes
    for the floored representation. The floored value ships to the agent with
    a marker indicating it was floored, so the agent can fetch fuller detail.

    This guarantees the problem_model is NEVER silently dropped due to a
    failure in the elaborate summarize logic.
    """

    def broken_summarize(v):
        """Simulate a failure in the elaborate summarize callback."""
        raise ValueError("Elaborate summarize logic failed: X happened")

    def working_floor(v):
        """Simple, independent, can't-fail floor."""
        return {"floored": True, "minimal": "skeleton"}

    payload = {"model": {"big": "data" * 1000}}  # ~4KB
    schema = {
        "model": {
            "priority": "high",
            "summarize": broken_summarize,
            "floor": working_floor,
        },
    }

    # Budget is tight, forcing the summarize path. The summarize callback
    # raises, but the floor callback succeeds and the floored value ships.
    with caplog.at_level(logging.WARNING):
        out = pack_under_budget(payload, schema, budget=100)

    # The key is present (not dropped) and holds the floored value.
    assert "model" in out
    assert out["model"] == {"floored": True, "minimal": "skeleton"}
    # The packer logged a warning about the summarize failure.
    assert any("summarize" in rec.message.lower() and "raised" in rec.message.lower()
               for rec in caplog.records)
    # The floored entry is marked as floored in the metadata.
    assert any(t["key"] == "model" and t["kind"] == "floored" for t in out["_packer_meta"]["trimmed"])


def test_floor_survives_when_both_summarize_and_floor_raise():
    """When both summarize and floor raise, the section is silently dropped.

    This is a catastrophic failure case: the floor function is expected to be
    simple and can't-fail. If it does raise, the packer logs a warning and
    the key is dropped entirely (there is no last-resort value to ship). This
    test documents the boundary condition: the floor MUST be defensively coded
    to never raise, or the inbound guarantee (model always shipped) is broken.
    """

    def broken_summarize(v):
        raise ValueError("Summarize failed")

    def broken_floor(v):
        raise RuntimeError("Floor failed too")

    payload = {"model": {"data": "x" * 1000}}
    schema = {
        "model": {
            "priority": "high",
            "summarize": broken_summarize,
            "floor": broken_floor,
        },
    }

    # Both callbacks fail. The section is dropped with warnings about both
    # failures (first floor, then summarize as a fallback).
    out = pack_under_budget(payload, schema, budget=200)
    assert "model" not in out
    # The packer logs warnings for both the floor and summarize failures.
    dropped = [d for d in out["_packer_meta"]["dropped"] if d["key"] == "model"]
    assert len(dropped) == 1
    # When the summarize callback also fails, that message is recorded.
    assert "exceeds remaining budget even after summarize" in dropped[0]["reason"]


# ---------------------------------------------------------------------------
# Invariant: a GC'd, worst-case-but-within-caps durable problem_model survives
# the real strategic single-pass path (GC -> agent-view trim -> pack_under_budget).
#
# gc_problem_model's stated purpose is to keep the durable model "small enough
# to round-trip in a single pass". That round-trip is:
#   durable model -> _agent_view_problem_model (trim) -> pack_under_budget
# at the real default budget under the production _STRATEGIC_REVIEW_SCHEMA.
# The pieces are tested in isolation elsewhere; these tests prove the whole
# path holds when the model is populated to the documented GC/health caps.
#
# All sizes are pulled from the actual cap constants (never magic numbers), so
# if a cap is raised without the budget still holding, these tests fail and
# surface the regression instead of CI staying green while the packer silently
# drops the entire problem_model section.
# ---------------------------------------------------------------------------


def _worst_case_durable_problem_model():
    """Build a maximal-within-caps durable ProblemModel for the round-trip proof.

    - ``archived_gaps`` filled PAST :data:`ARCHIVED_GAPS_DURABLE_MAX` so GC has
      to evict down to the cap (exercises the real eviction path).
    - :data:`MODEL_SIZE_HEALTHY_GAPS` active gaps, each with ``history`` /
      ``past_moves`` at their durable caps and a substantial ``evidence`` body.
    - :data:`MODEL_SIZE_HEALTHY_CAPABILITIES` capabilities and
      :data:`MODEL_SIZE_HEALTHY_FRICTION` active friction at the healthy thresholds.

    Field text is realistic (non-empty descriptions / evidence / notes) so the
    byte footprint is honest rather than artificially small.
    """
    from app.problem_model import (
        ARCHIVED_GAPS_DURABLE_MAX,
        GAP_HISTORY_DURABLE_MAX,
        GAP_PAST_MOVES_DURABLE_MAX,
        MODEL_SIZE_HEALTHY_CAPABILITIES,
        MODEL_SIZE_HEALTHY_FRICTION,
        MODEL_SIZE_HEALTHY_GAPS,
        Capability,
        Friction,
        Gap,
        GapHistoryEvent,
        ProblemModel,
        Source,
    )

    def _ts(i: int) -> str:
        # Monotonic ISO-8601 timestamps so archived-gap eviction has a stable
        # recency order to sort by.
        return f"2026-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}T12:00:00Z"

    def _history(n: int) -> list:
        return [
            GapHistoryEvent(
                ts=_ts(j),
                event="move_attached" if j % 2 else "priority_raised",
                note=(
                    f"cycle {j}: attempted direction d-{j:04d}; "
                    "outcome recorded against HEAD with full provenance and "
                    "a short human-readable rationale for the next strategist."
                ),
            )
            for j in range(n)
        ]

    def _evidence(gid: str) -> str:
        # Gap.evidence is a free-text string (not a list) — give it a realistic
        # multi-sentence body so the per-gap footprint is honest.
        return (
            f"Gap {gid} is backed by repeated direction outcomes and operator "
            "observations across several cycles: the strategic loop kept "
            "re-deriving cycle state by hand, the problem_model failed to "
            "round-trip its own setter, and findings carried no verified-vs-HEAD "
            "provenance. This evidence body is intentionally substantial so the "
            "worst-case byte footprint reflects a mature project, not a stub."
        )

    active_gaps = [
        Gap(
            id=f"g-active-{i:04d}",
            name=f"Active gap {i}: durable problem_model round-trip subsystem {i}",
            description=(
                f"Active gap {i} describes a concrete delta between strategic "
                "intent and current capabilities, retained with full append-only "
                "history so the strategist sees the whole arc across cycles."
            ),
            evidence=_evidence(f"g-active-{i:04d}"),
            priority="high",
            status="active",
            opened=_ts(i),
            source=Source(type="direction_outcome", direction_id=f"d-{i:04d}"),
            active_moves=[f"d-active-{i:04d}"],
            past_moves=[f"d-past-{i:04d}-{k:03d}" for k in range(GAP_PAST_MOVES_DURABLE_MAX)],
            history=_history(GAP_HISTORY_DURABLE_MAX),
        )
        for i in range(MODEL_SIZE_HEALTHY_GAPS)
    ]

    archived_gaps = [
        Gap(
            id=f"g-archived-{i:04d}",
            name=f"Archived gap {i}",
            description=f"Closed/abandoned gap {i} retained as reference-only history.",
            evidence=_evidence(f"g-archived-{i:04d}"),
            status="closed",
            opened=_ts(i),
            closed=_ts(i + 1),
        )
        # Overfill past the cap so gc_problem_model has to evict.
        for i in range(ARCHIVED_GAPS_DURABLE_MAX + 25)
    ]

    capabilities = [
        Capability(
            name=f"Capability {i}: registry-derived task-type subsystem {i}",
            status="working" if i % 2 else "partial",
            notes=(
                f"Capability {i} is backed by integration-branch code and a "
                "shipped PR; notes describe the current honest status and the "
                "evidence that established it."
            ),
            source=Source(type="shipped_change", pr_number=7000 + i),
            backing_paths=[
                f"backend/app/subsystem_{i}.py",
                f"backend/testing/test_subsystem_{i}.py",
            ],
        )
        for i in range(MODEL_SIZE_HEALTHY_CAPABILITIES)
    ]

    friction = [
        Friction(
            id=f"f-{i:04d}",
            name=f"Friction {i}: recurring blocker observed across moves",
            description=(
                f"Friction {i} is a recurring blocker seen across multiple "
                "directions; described in enough detail to be actionable."
            ),
            seen_in=[f"d-{i:04d}", f"d-{i + 1:04d}"],
            status="active",
            mitigations_tried=[f"tried approach {k}" for k in range(3)],
        )
        for i in range(MODEL_SIZE_HEALTHY_FRICTION)
    ]

    return ProblemModel(
        revision=42,
        gaps=active_gaps,
        archived_gaps=archived_gaps,
        capabilities=capabilities,
        friction=friction,
    )


def test_gcd_problem_model_round_trips_under_single_pass_budget():
    """A worst-case-but-within-caps durable problem_model survives the real
    GC + agent-view trim + pack_under_budget path at the default budget.

    This is the invariant gc_problem_model exists to guarantee: after GC the
    model is "small enough to round-trip in a single pass". We run the exact
    production wiring (the real ``_STRATEGIC_REVIEW_SCHEMA`` and the default
    ``pack_under_budget`` budget) and assert the ``problem_model`` section is
    present in the output and NOT in ``_packer_meta["dropped"]``.
    """
    from app.problem_model import (
        ARCHIVED_GAPS_DURABLE_MAX,
        PROBLEM_MODEL_BYTE_BUDGET,
        _serialized_model_bytes,
        gc_problem_model,
    )
    from app.strategic_spawning import (
        _STRATEGIC_REVIEW_SCHEMA,
        _agent_view_problem_model,
    )

    model = _worst_case_durable_problem_model()

    # Real backend GC: the count pass evicts archived_gaps down to the cap, then
    # the byte pass evicts further (archived below the cap + active-gap
    # append-only tails) to bring the serialized model under the byte budget.
    summary = gc_problem_model(model)
    assert summary.get("archived_gaps_evicted"), (
        "fixture should overfill archived_gaps so GC actually evicts"
    )
    # The byte pass can drive archived_gaps below the count cap (even to empty)
    # when that is what it takes to fit the budget, so assert the cap is an upper
    # bound and that the durable model actually fits — the real round-trip
    # invariant this test guards.
    assert len(model.archived_gaps) <= ARCHIVED_GAPS_DURABLE_MAX
    assert _serialized_model_bytes(model) <= PROBLEM_MODEL_BYTE_BUDGET

    # Reproduce the production payload wiring: the durable model is dumped,
    # trimmed by the agent view, and packed under the SAME schema object the
    # strategic spawn path uses, at the real default budget.
    payload = {
        "type": "strategic_review",
        "research_mode": False,
        "problem_model": _agent_view_problem_model(model.model_dump()),
    }
    # Default budget (120 * 1024) — pass none so the production default applies.
    out = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA)

    assert out["_packer_meta"]["budget_total"] == 120 * 1024
    assert "problem_model" in out, (
        "GC'd worst-case problem_model was dropped under the single-pass budget"
    )
    dropped_keys = {d["key"] for d in out["_packer_meta"]["dropped"]}
    assert "problem_model" not in dropped_keys, (
        f"problem_model section was dropped by the packer: {out['_packer_meta']['dropped']}"
    )
    # The shipped section must still carry the real gaps (not summarized away).
    assert out["problem_model"].get("gaps"), "packed problem_model lost its active gaps"
    # archived_gaps is intentionally stripped by the agent view.
    assert "archived_gaps" not in out["problem_model"]


def test_problem_model_survives_bloated_real_payload_with_many_high_sections():
    """The real strategic payload path: when MANY high-tier sections are present
    together and collectively blow the budget, the WHOLE problem_model is still
    delivered in-payload (intact, never floored, never dropped) while other
    recoverable high-tier context degrades or drops first.

    The floor reservation equals the whole agent-view model, so a healthy mature
    model (well under the 120KB budget) ships in full and its recoverable siblings
    yield first — the skeletonizing fallback never fires. This exercises the
    production ``_STRATEGIC_REVIEW_SCHEMA`` + default budget, not isolated unit
    inputs — the guarantee must hold under real bloat.
    """
    from app.problem_model import gc_problem_model
    from app.strategic_spawning import (
        _STRATEGIC_REVIEW_SCHEMA,
        _agent_view_problem_model,
    )

    model = _worst_case_durable_problem_model()
    gc_problem_model(model)

    # A realistic-but-oversized cycle payload: the problem_model alongside
    # several other high-tier sections each padded with enough bulk that the
    # sum far exceeds the 120 KB budget, forcing real eviction pressure.
    bulk = "x" * 45_000
    payload = {
        "type": "strategic_review",
        "research_mode": False,
        "project_name": "Blueprint",
        "base_branch": "main",
        "vision": {"purpose": "demo"},
        "problem_model": _agent_view_problem_model(model.model_dump()),
        # High-tier siblings without summarize_fns that would otherwise win the
        # budget race and starve the problem_model out of the payload.
        "running_update": bulk,
        "cycle_start_reconciliation": {"conflicts": [bulk]},
        "deployment_state": {"deployed": bulk},
    }
    out = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA)

    assert "problem_model" in out, (
        "problem_model was dropped under real multi-high-section budget pressure"
    )
    dropped_keys = {d["key"] for d in out["_packer_meta"]["dropped"]}
    trimmed_keys = {t["key"] for t in out["_packer_meta"]["trimmed"]}
    assert "problem_model" not in dropped_keys, (
        f"problem_model dropped by the packer: {out['_packer_meta']['dropped']}"
    )
    # The WHOLE model shipped — not the floored/skeletonized fallback.
    assert "problem_model" not in trimmed_keys, (
        f"problem_model was degraded instead of shipping whole: {out['_packer_meta']['trimmed']}"
    )
    assert "_agent_view_floor_applied" not in out["problem_model"], (
        "a healthy mature model must ship whole, without the floored marker"
    )
    # The delivered model is a meaningful working set (still carries live gaps).
    assert out["problem_model"].get("gaps"), "delivered problem_model lost its gaps"
    # Other recoverable context degraded first — at least one high sibling dropped.
    assert dropped_keys, "expected recoverable siblings to drop before the reserved model"
    assert out["_packer_meta"]["budget_used"] <= 120 * 1024


def test_gap_memory_trimmed_surfaced_for_capped_model():
    """The agent-view per-gap trim (history/past_moves) is surfaced via
    ``_packer_meta['gap_memory_trimmed']`` for the worst-case model, so the
    strategist knows a gap's append-only memory was shortened to a window."""
    from app.problem_model import (
        GAP_APPEND_ONLY_GC_FLOOR,
        gc_problem_model,
    )
    from app.strategic_spawning import (
        _STRATEGIC_REVIEW_SCHEMA,
        _GAP_HISTORY_KEEP,
        _agent_view_problem_model,
    )

    model = _worst_case_durable_problem_model()
    gc_problem_model(model)

    payload = {
        "type": "strategic_review",
        "research_mode": False,
        "problem_model": _agent_view_problem_model(model.model_dump()),
    }
    out = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA)

    gmt = out["_packer_meta"]["gap_memory_trimmed"]
    assert gmt, "expected per-gap pre-trim markers to be surfaced"
    pm_entry = next((e for e in gmt if e["key"] == "problem_model"), None)
    assert pm_entry is not None
    # The byte pass first trims every active gap's append-only history/past_moves
    # down to the durable GC floor; the agent view then trims that floor down to
    # its keep window and surfaces the (post-GC) pre-trim total, which is the GC
    # floor rather than the durable cap.
    a_gap = pm_entry["gaps"][0]
    assert a_gap["fields"]["history"]["shown"] == _GAP_HISTORY_KEEP
    assert a_gap["fields"]["history"]["total"] == GAP_APPEND_ONLY_GC_FLOOR
    assert a_gap["fields"]["past_moves"]["total"] == GAP_APPEND_ONLY_GC_FLOOR


def test_design_doc_index_and_whole_problem_model_coexist_in_realistic_payload():
    """Regression: a realistic strategic-review payload ships both the design_doc
    as a compact index AND the whole agent-view problem_model (not floored) at the
    real default budget, proving the compact design_doc index successfully reserves
    headroom for the complete problem_model to fit intact.

    The design_doc was switched from full-content to a bounded navigation index
    (key + one-line summary + reconciled status/modules) specifically to keep the
    problem_model from being crowded out of the budget. This test proves that
    tradeoff works in a realistic end-to-end payload: both the design_doc index
    and the whole problem_model ship in full, neither is floored or dropped.
    """
    from app.problem_model import gc_problem_model
    from app.strategic_spawning import (
        _STRATEGIC_REVIEW_SCHEMA,
        _agent_view_problem_model,
        _design_doc_index,
    )

    model = _worst_case_durable_problem_model()
    gc_problem_model(model)

    # Build a realistic design_doc with multiple sections (systems, capabilities,
    # notes). Each section carries content so the full doc (before indexing) is
    # realistic in size.
    design_doc = {
        "systems/auth": {
            "title": "Authentication System",
            "description": (
                "OAuth2 integration with external provider, multi-factor "
                "authentication support, session management, and token refresh logic."
            ),
            "status": "working",
            "modules": ["backend/app/auth.py", "backend/app/auth_handlers.py"],
        },
        "systems/api": {
            "title": "API Layer",
            "description": "REST API endpoints, OpenAPI schema generation, and request routing.",
            "status": "working",
            "modules": ["backend/app/routers/", "backend/app/schemas.py"],
        },
        "capabilities/database": {
            "title": "Database Access Layer",
            "notes": "SQLAlchemy ORM with async support and connection pooling.",
            "status": "working",
        },
        "notes": "Core platform systems are stable; focus areas for next version.",
    }

    # Assemble a realistic strategic-review payload with multiple high-tier
    # sections alongside the problem_model: directions_summary, open_messages,
    # cycle_start_reconciliation, and model_health. This represents a typical
    # cycle payload with meaningful context, not a minimal test case.
    payload = {
        "type": "strategic_review",
        "research_mode": False,
        "project_name": "Blueprint",
        "base_branch": "main",
        "model": "opus",
        "effort": "standard",
        "enable_self_improvement": False,
        "vision": {"purpose": "Stabilize platform and add observability hooks"},
        "problem_model": _agent_view_problem_model(model.model_dump()),
        # The compact design_doc index — the whole doc is replaced with a bounded
        # index (one entry per section: key + summary + reconciled status/modules).
        "design_doc": _design_doc_index(design_doc),
        # Representative open messages (operator feedback / issues).
        "open_messages": [
            {"id": "m1", "type": "NOTE", "title": "Observability gap", "body": "x" * 500},
            {
                "id": "m2",
                "type": "BUG_REPORT",
                "title": "Session token leak",
                "body": "y" * 800,
            },
        ],
        # Cycle-start reconciliation summarizing live state.
        "cycle_start_reconciliation": {
            "conflicts": ["Direction d-1 conflicts with staged d-2"],
            "summary": "State aligned; model up-to-date.",
        },
        # Soft health signal (advisory).
        "model_health": {"gaps_count": 5, "capabilities_count": 3},
    }

    # Pack under the real default budget with the production schema.
    out = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA)

    # Prove the design_doc index is present.
    assert "design_doc" in out, "design_doc index was dropped or absent"
    design_doc_out = out["design_doc"]
    assert isinstance(design_doc_out, dict), "design_doc should be a dict with sections"
    assert "sections" in design_doc_out
    sections = design_doc_out["sections"]
    assert isinstance(sections, list)
    assert len(sections) > 0, "design_doc index should have section entries"
    # Verify the index is compact (bounded summaries, not full prose).
    for entry in sections:
        if "summary" in entry:
            assert len(entry["summary"]) <= 200 + 1, (
                "design_doc index summaries should be bounded to ~200 chars"
            )

    # Prove the problem_model ships in full (whole, not floored or dropped).
    assert "problem_model" in out, (
        "problem_model was dropped under real multi-section budget pressure"
    )
    dropped_keys = {d["key"] for d in out["_packer_meta"]["dropped"]}
    assert "problem_model" not in dropped_keys, (
        f"problem_model was dropped by the packer: {out['_packer_meta']['dropped']}"
    )

    # The delivered model must be whole (not floored). The floor marker is only
    # present when the packer's summarize callback shrinks the model to fit.
    pm = out["problem_model"]
    assert "_agent_view_floor_applied" not in pm, (
        "problem_model should not be floored when design_doc index reserves "
        "enough headroom — the floor marker indicates degradation."
    )

    # Verify the model retains its full structure: gaps, capabilities, friction
    # are all present (not summarized away or dropped).
    assert pm.get("gaps"), (
        "whole problem_model should retain its active gaps list"
    )
    assert isinstance(pm["gaps"], list) and len(pm["gaps"]) > 0
    # Capabilities and friction are present and substantial (not skeletonized).
    # A skeletonized section would have only name + status; a full one has notes,
    # descriptions, evidence, backing_paths, etc. Check a sample entry.
    if pm.get("capabilities"):
        cap = pm["capabilities"][0]
        assert isinstance(cap, dict)
        assert "name" in cap
        # The whole model has notes/descriptions; floored/skeletonized would not.
        # If the model is truly whole, at least one capability should have detail
        # beyond just name+status.
        has_detail = any(
            pm["capabilities"][i].get(k)
            for i in range(len(pm["capabilities"]))
            for k in ("notes", "source", "backing_paths")
        )
        assert has_detail, (
            "capabilities should retain full detail (notes, source, paths) "
            "when not floored"
        )

    # The payload stayed under budget.
    assert out["_packer_meta"]["budget_used"] <= 120 * 1024


def test_droppable_sections_declare_summarize_or_exemption():
    """Every non-critical schema entry must declare a summarize fallback OR an
    explicit ``exempt`` marker.  Without one, a section can silently vanish from
    the payload with no trim-log record and no degraded form for the agent to
    observe.  ``exempt`` is the opt-out: sections already bounded+capped at build
    time don't need a summarize_fn, but they must say so explicitly so this
    invariant can distinguish deliberate design from an accidental omission.

    Both _STRATEGIC_REVIEW_SCHEMA and _DESIGN_DOC_UPDATE_SCHEMA are checked so
    the two sibling schemas stay consistent under the same contract.
    """
    from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA, _DESIGN_DOC_UPDATE_SCHEMA

    schemas = {
        "_STRATEGIC_REVIEW_SCHEMA": _STRATEGIC_REVIEW_SCHEMA,
        "_DESIGN_DOC_UPDATE_SCHEMA": _DESIGN_DOC_UPDATE_SCHEMA,
    }
    all_failures = []
    for schema_name, schema in schemas.items():
        for key, entry in schema.items():
            if entry.get("priority") == "critical":
                continue  # critical keys always ship; no summarize/exempt needed
            if entry.get("summarize") or entry.get("floor"):
                continue  # has a degradation path
            if entry.get("exempt"):
                continue  # explicitly exempted (bounded/capped at build time)
            all_failures.append(f"{schema_name}.{key}")

    assert not all_failures, (
        f"Schema keys lack both 'summarize' and 'exempt': {all_failures}. "
        "Add a summarize_fn for sections that can grow under budget pressure, "
        "or add 'exempt': True for sections already bounded at build time."
    )


def test_dropped_sections_logged_with_byte_accounting(caplog):
    """Dropped sections log with byte count; core (floored) drops log at ERROR.

    Part 1: a regular (non-core) drop emits a WARNING that includes the
    serialized byte size of the dropped section.
    Part 2: a core section (one with a ``floor`` callback in schema) that
    gets dropped when floor guarantees are cleared emits an ERROR log with
    byte accounting so operators can diagnose budget violations immediately
    from the backend log stream without needing to inspect _packer_meta.
    """
    # --- Part 1: regular (non-core) drop → WARNING with byte count ---
    payload_reg = {"big": "x" * 200}
    schema_reg = {"big": {"priority": "medium"}}
    with caplog.at_level(logging.INFO, logger="app.payload_schemas"):
        out_reg = pack_under_budget(payload_reg, schema_reg, budget=10)
    assert "big" not in out_reg
    big_bytes = _size("x" * 200)
    drop_logs = [
        r for r in caplog.records
        if "dropped" in r.message.lower() and "big" in r.message
    ]
    assert drop_logs, "expected a log record for the dropped 'big' section"
    assert drop_logs[0].levelno == logging.WARNING, (
        "non-core drop must log at WARNING, not ERROR"
    )
    assert str(big_bytes) in drop_logs[0].message, (
        f"expected byte count {big_bytes} in drop log: {drop_logs[0].message}"
    )

    # --- Part 2: core section (has floor) dropped → ERROR with byte count ---
    # Build a payload where reserved_remaining + critical_full > budget so the
    # packer clears all floor guarantees. With degraded_budget too small to fit
    # even the floor value, the floored key ends up unprotected and is dropped.
    caplog.clear()
    critical_data = "c" * 400
    core_data = "f" * 200
    critical_bytes = _size(critical_data)
    floor_bytes = _size({"minimal": True})  # the floor callback's output size
    core_bytes = _size(core_data)
    # Budget: fits critical alone; adding the floor pushes reserved+critical over.
    budget = critical_bytes + floor_bytes - 1
    payload_core = {"identity": critical_data, "core": core_data}
    schema_core = {
        "identity": {"priority": "critical"},
        "core": {"priority": "high", "floor": lambda v: {"minimal": True}},
    }
    with caplog.at_level(logging.INFO, logger="app.payload_schemas"):
        out_core = pack_under_budget(payload_core, schema_core, budget=budget)
    assert "core" not in out_core, (
        "core section should be dropped when floor guarantee is lost"
    )
    error_logs = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "core" in r.message
    ]
    assert error_logs, "expected an ERROR log when a core (floored) section is dropped"
    assert str(core_bytes) in error_logs[0].message, (
        f"expected byte count {core_bytes} in ERROR log: {error_logs[0].message}"
    )


def test_high_tier_drop_logged_loud(caplog):
    """A high-tier (non-floor) drop logs at ERROR naming key, tier, bytes, reason;
    a medium/low drop stays at WARNING.

    Regression guard for the 2026-07-04 incident where 14 sections — including
    the whole open-message inbox — dropped under the 120KB budget with no loud
    backend signal. A high-tier wholesale drop must now be loud (ERROR) so an
    operator sees the strategist ran on a degraded payload.
    """
    # --- high-tier drop → ERROR naming key, tier, bytes, reason ---
    payload_hi = {"inbox": "x" * 400}
    schema_hi = {"inbox": {"priority": "high"}}
    inbox_bytes = _size("x" * 400)
    with caplog.at_level(logging.INFO, logger="app.payload_schemas"):
        out_hi = pack_under_budget(payload_hi, schema_hi, budget=10)
    assert "inbox" not in out_hi
    hi_logs = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "inbox" in r.message
    ]
    assert hi_logs, "expected an ERROR log when a high-tier section is dropped"
    msg = hi_logs[0].message
    assert "inbox" in msg, f"ERROR log must name the key: {msg}"
    assert "high" in msg, f"ERROR log must name the tier: {msg}"
    assert str(inbox_bytes) in msg, f"ERROR log must include byte size {inbox_bytes}: {msg}"
    assert "budget" in msg.lower(), f"ERROR log must include the drop reason: {msg}"

    # --- medium-tier drop → WARNING, NOT ERROR ---
    caplog.clear()
    payload_med = {"extra": "y" * 400}
    schema_med = {"extra": {"priority": "medium"}}
    with caplog.at_level(logging.INFO, logger="app.payload_schemas"):
        out_med = pack_under_budget(payload_med, schema_med, budget=10)
    assert "extra" not in out_med
    med_drop_logs = [
        r for r in caplog.records
        if "dropped" in r.message.lower() and "extra" in r.message
    ]
    assert med_drop_logs, "expected a log record for the dropped 'extra' section"
    assert all(r.levelno == logging.WARNING for r in med_drop_logs), (
        "medium/low drop must log at WARNING, not ERROR"
    )


def test_dropped_entry_carries_priority():
    """Each _packer_meta drop entry records the section's schema tier so
    downstream telemetry can filter high/critical drops without a schema
    re-lookup."""
    payload = {
        "top": "a" * 400,
        "mid": "b" * 400,
        "bottom": "c" * 400,
    }
    schema = {
        "top": {"priority": "high"},
        "mid": {"priority": "medium"},
        "bottom": {"priority": "low"},
    }
    out = pack_under_budget(payload, schema, budget=10)
    by_key = {d["key"]: d for d in out["_packer_meta"]["dropped"]}
    assert by_key["top"]["priority"] == "high"
    assert by_key["mid"]["priority"] == "medium"
    assert by_key["bottom"]["priority"] == "low"


def test_core_sections_survive_realistic_worst_case():
    """Worst case: an oversized problem_model dominates the budget, yet the three
    strategic CORE sections — active-direction summaries, the open-message inbox,
    and version/phase state — ALWAYS ship at least a compact floored/summarized
    form. None is dropped wholesale.

    This is the core-section floor guarantee: the 2026-07-04 cycle shipped with
    the entire open-message inbox and all per-direction detail dropped while a
    large problem_model consumed the byte budget. Each core key now declares a
    ``floor`` callback whose bytes are reserved off the top, so a big
    problem_model degrades the recoverable long-tail — not the core.
    """
    from app.strategic_spawning import (
        _STRATEGIC_REVIEW_SCHEMA,
        _agent_view_problem_model,
    )

    # An oversized problem_model: many active gaps with substantial evidence so
    # the whole agent-view model dominates (but still floor-reserves) the budget.
    big_pm = {
        "gaps": [
            {
                "id": f"g-{i:03d}",
                "name": f"Gap {i}",
                "priority": "high",
                "status": "active",
                "opened": f"2026-05-{(i % 27) + 1:02d}T00:00:00Z",
                "evidence": "e" * 900,
                "description": "d" * 600,
            }
            for i in range(60)
        ],
        "capabilities": [
            {"name": f"cap {i}", "status": "working", "notes": "n" * 300}
            for i in range(20)
        ],
    }
    problem_model = _agent_view_problem_model(big_pm)

    # Core section 1: many directions, each with a bulky text body so the full
    # directions_summary far exceeds any leftover budget.
    directions_summary = [
        {
            "id": f"d-{i:03d}",
            "priority": "high" if i % 2 else "medium",
            "status": "active",
            "total_cycles": i,
            "prs_merged": i % 4,
            "text": f"Direction {i}: " + ("t" * 1500),
        }
        for i in range(30)
    ]

    # Core section 2: a full open-message inbox with long per-message content.
    open_messages = [
        {
            "id": f"m-{i:03d}",
            "type": "BUG_REPORT" if i < 3 else "NOTE",
            "created_at": f"2026-06-{(i % 27) + 1:02d}T00:00:00Z",
            "content": f"message {i}: " + ("c" * 800),
        }
        for i in range(40)
    ]

    # Core section 3: version/phase state with a bulky raw block (big goal text,
    # a long per-criterion coverage list, and several recent versions).
    version_phase = {
        "version_number": 22,
        "phase": "build",
        "goal": "Ship the core-floor guarantee. " + ("g" * 4000),
        "phase_reasoning": "r" * 4000,
        "criterion_coverage": {
            "total": 200,
            "covered": 40,
            "criteria": [
                {"id": f"c-{i:03d}", "text": "criterion " + ("x" * 200)}
                for i in range(200)
            ],
        },
        "recent_versions": [
            {
                "version_number": 22 - i,
                "status": "promoted",
                "last_qualification": {"status": "not_promoted", "detail": "z" * 1000},
            }
            for i in range(10)
        ],
    }

    payload = {
        "type": "strategic_review",
        "vision": {"purpose": "Deliver true user-visible improvements."},
        "problem_model": problem_model,
        "directions_summary": directions_summary,
        "open_messages": open_messages,
        "version_phase": version_phase,
    }

    # The three core sections are each individually larger than the whole budget
    # would allow once the oversized problem_model reserves its bytes — proving
    # the floors, not luck, are what keeps them present.
    budget = 120 * 1024
    assert _size(problem_model) + _size(directions_summary) > budget

    out = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA, budget=budget)

    dropped_keys = {d["key"] for d in out["_packer_meta"]["dropped"]}
    trimmed_keys = {t["key"] for t in out["_packer_meta"]["trimmed"]}

    for core in ("directions_summary", "open_messages", "version_phase"):
        assert core in out, f"core section {core!r} was dropped wholesale"
        assert core not in dropped_keys, f"core section {core!r} in dropped: {out['_packer_meta']['dropped']}"
        # Present in COMPACT form — it could not fit at full size, so the packer
        # summarized or floored it (recorded in _packer_meta.trimmed).
        assert core in trimmed_keys, (
            f"core section {core!r} expected to ship compacted, not full-size"
        )

    # The core sections shipped in genuinely reduced form (smaller than raw).
    assert _size(out["directions_summary"]) < _size(directions_summary)
    assert _size(out["open_messages"]) < _size(open_messages)
    assert _size(out["version_phase"]) < _size(version_phase)

    # The open-message inbox floor keeps the reproducing BUG_REPORT items — the
    # highest survival-priority rows — not an arbitrary slice.
    shipped_types = {m.get("type") for m in out["open_messages"] if isinstance(m, dict)}
    assert "BUG_REPORT" in shipped_types, "core inbox floor dropped the BUG_REPORT items"

    # The problem_model is still delivered (never silently dropped either).
    assert "problem_model" in out
    assert "problem_model" not in dropped_keys
    assert out["_packer_meta"]["budget_used"] <= budget


def test_trim_and_drop_records_include_byte_counts_and_fetch_verb():
    """Trimmed and dropped entries carry byte counts and fetch_verb where declared.

    This is the acceptance test for the sized trim-log feature: every trim/drop
    record must carry the original byte size, trimmed entries must also carry the
    post-trim byte count, and entries for schema keys that declare a ``fetch_verb``
    must include it so the agent knows exactly which recovery verb to run.
    """
    items_full = list(range(500))
    huge_str = "x" * 5000
    payload = {
        "items": items_full,  # summarized → trimmed record
        "huge": huge_str,     # dropped (exceeds budget even after summarize)
    }
    schema = {
        "items": {
            "priority": "medium",
            "summarize": lambda v: v[:5],
            "fetch_verb": "direction",
        },
        "huge": {
            "priority": "low",
            "fetch_verb": "message",
        },
    }
    out = pack_under_budget(payload, schema, budget=500)

    # --- trimmed entry ---
    trimmed = out["_packer_meta"]["trimmed"]
    assert len(trimmed) == 1, f"expected 1 trimmed entry, got {trimmed}"
    t = trimmed[0]
    assert t["key"] == "items"
    assert t["kind"] == "list_trimmed"
    assert t["shown"] == 5
    assert t["total"] == 500
    # Byte counts must be present and correctly ordered
    assert "bytes_original" in t, "trimmed entry must carry bytes_original"
    assert "bytes_trimmed" in t, "trimmed entry must carry bytes_trimmed"
    assert t["bytes_original"] == _size(items_full), "bytes_original must equal full serialized size"
    assert t["bytes_trimmed"] == _size(list(range(5))), "bytes_trimmed must equal trimmed serialized size"
    assert t["bytes_original"] > t["bytes_trimmed"], "trimmed form must be smaller than original"
    # Recovery hint from schema
    assert t.get("fetch_verb") == "direction", "trimmed entry must carry fetch_verb from schema"

    # --- dropped entry ---
    dropped = out["_packer_meta"]["dropped"]
    assert len(dropped) == 1, f"expected 1 dropped entry, got {dropped}"
    d = dropped[0]
    assert d["key"] == "huge"
    assert "bytes" in d, "dropped entry must carry bytes"
    assert d["bytes"] == _size(huge_str), "bytes must equal original serialized size"
    # Recovery hint from schema
    assert d.get("fetch_verb") == "message", "dropped entry must carry fetch_verb from schema"


def test_trim_and_drop_records_omit_fetch_verb_when_not_declared():
    """When a schema entry does not declare fetch_verb, the records do not carry it.

    Fetch verbs are only surfaced for sections that have a documented recovery
    path. Sections without one should not emit a fetch_verb field so the agent
    does not try to run a non-existent recovery verb.
    """
    payload = {
        "section_a": list(range(100)),  # summarized
        "section_b": "y" * 5000,        # dropped
    }
    schema = {
        "section_a": {"priority": "medium", "summarize": lambda v: v[:3]},
        "section_b": {"priority": "low"},
    }
    out = pack_under_budget(payload, schema, budget=200)

    trimmed = out["_packer_meta"]["trimmed"]
    assert len(trimmed) == 1
    assert "fetch_verb" not in trimmed[0], "fetch_verb must not appear when not declared in schema"

    dropped = out["_packer_meta"]["dropped"]
    assert len(dropped) == 1
    assert "fetch_verb" not in dropped[0], "fetch_verb must not appear when not declared in schema"


def test_first_autonomous_improvement_at_registered_in_schema(caplog):
    """The first_autonomous_improvement_at timestamp field is properly registered
    in the schema, so it does not trigger a missing-schema warning and is treated
    as high-priority exempt (not demoted to low).
    """
    from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA

    # Verify the field is registered in the schema with correct priority.
    assert "first_autonomous_improvement_at" in _STRATEGIC_REVIEW_SCHEMA
    entry = _STRATEGIC_REVIEW_SCHEMA["first_autonomous_improvement_at"]
    assert entry.get("priority") == "high"
    assert entry.get("exempt") is True

    # Pack a payload with the field — no missing-schema warning should appear.
    payload = {
        "type": "strategic_review",
        "vision": {"purpose": "demo"},
        "first_autonomous_improvement_at": "2026-07-05T12:00:00Z",
    }
    schema = {
        "type": {"priority": "critical"},
        "vision": {"priority": "critical"},
        "first_autonomous_improvement_at": _STRATEGIC_REVIEW_SCHEMA[
            "first_autonomous_improvement_at"
        ],
    }
    with caplog.at_level(logging.WARNING):
        out = pack_under_budget(payload, schema, budget=10_000)

    # Field is present in the output.
    assert out["first_autonomous_improvement_at"] == "2026-07-05T12:00:00Z"
    # No missing-schema warning logged.
    assert not any(
        "no schema entry" in rec.message.lower() for rec in caplog.records
    )
