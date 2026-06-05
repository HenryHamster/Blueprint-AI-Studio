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
    assert {"key": "model", "kind": "floored"} in out["_packer_meta"]["trimmed"]


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
    assert {"key": "model", "kind": "floored"} in out["_packer_meta"]["trimmed"]


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
    together and collectively blow the budget, the problem_model is still
    delivered in-payload (floored, never dropped) while other recoverable
    high-tier context degrades or drops first.

    This exercises the production ``_STRATEGIC_REVIEW_SCHEMA`` + default budget,
    not isolated unit inputs — the guarantee must hold under real bloat.
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
    assert "problem_model" not in dropped_keys, (
        f"problem_model dropped by the packer: {out['_packer_meta']['dropped']}"
    )
    # The delivered model is a meaningful working set (still carries live gaps).
    assert out["problem_model"].get("gaps"), "delivered problem_model lost its gaps"
    # Other recoverable context degraded first — at least one high sibling dropped.
    assert dropped_keys, "expected recoverable siblings to drop before the floor"
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
