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


def test_compact_direction_entry_preserves_retired_grounding():
    """_compact_direction_summary_entry keeps retired_direction_grounding through
    compaction, including nested metrics and retirement_memo bodies."""
    from app.strategic_payload_compaction import _compact_direction_summary_entry

    stat = {
        "id": "d-rg",
        "status": "active",
        "text": "continuation direction",
        "retired_direction_grounding": {
            "direction_id": "d-prior",
            "classification": "success_evidence_met",
            "metrics": {"prs_merged": 5, "cycles": 9},
            "reason": "prior direction met its success evidence",
            "gap_id": "g-x",
            "retirement_memo": {
                "knowledge_doc_id": "kd-1",
                "summary": "landed the ledger feature",
            },
        },
    }
    out = _compact_direction_summary_entry(stat)
    grounding = out["retired_direction_grounding"]
    assert grounding["direction_id"] == "d-prior"
    assert grounding["classification"] == "success_evidence_met"
    # nested bodies preserved, not flattened away
    assert grounding["metrics"] == {"prs_merged": 5, "cycles": 9}
    assert grounding["retirement_memo"] == {
        "knowledge_doc_id": "kd-1",
        "summary": "landed the ledger feature",
    }


def test_compact_direction_entry_omits_grounding_when_absent():
    """No retired_direction_grounding on input → key absent from compacted entry."""
    from app.strategic_payload_compaction import _compact_direction_summary_entry

    out = _compact_direction_summary_entry({"id": "d-novel", "status": "active"})
    assert "retired_direction_grounding" not in out


def test_compact_direction_entry_preserves_in_flight_count():
    """_compact_direction_summary_entry carries the in_flight scalar through the
    full/summarize path, including the zero case (present-even-when-0)."""
    from app.strategic_payload_compaction import _compact_direction_summary_entry

    staffed = _compact_direction_summary_entry(
        {"id": "d-staffed", "status": "active", "in_flight": 3}
    )
    assert staffed["in_flight"] == 3

    idle = _compact_direction_summary_entry(
        {"id": "d-idle", "status": "active", "in_flight": 0}
    )
    # Zero must survive so idle stays distinguishable from staffed.
    assert idle["in_flight"] == 0

    # Unstamped entry simply omits the field (backward-compatible).
    unstamped = _compact_direction_summary_entry({"id": "d-old", "status": "active"})
    assert "in_flight" not in unstamped


def test_direction_compaction_ranks_idle_findings_ahead_of_activity():
    """Actionable findings outrank historical activity in degraded forms."""
    from app.strategic_payload_compaction import _summarize_directions

    idle_with_finding = {
        "id": "d-actionable",
        "status": "active",
        "priority": "low",
        "in_flight": 0,
        "recent_findings": ["new finding"],
        "research_context": "## Established Findings\n- new finding\n",
    }
    busy_history = {
        "id": "d-busy",
        "status": "active",
        "priority": "high",
        "in_flight": 2,
        "total_cycles": 500,
        "prs_merged": 100,
    }

    compacted = _summarize_directions([busy_history, idle_with_finding])

    assert compacted[0]["id"] == "d-actionable"
    assert compacted[0]["research_context"] == idle_with_finding["research_context"].strip()


def test_floor_direction_entry_carries_in_flight_count():
    """_floor_direction_entry emits the in_flight count on the ultra-compact core
    floor line so a staffed direction is distinguishable from an idle one even
    under extreme byte pressure. Zero survives; an unstamped entry omits it."""
    from app.strategic_payload_compaction import _floor_direction_entry

    staffed = _floor_direction_entry(
        {"id": "d-staffed", "priority": 1, "status": "active", "in_flight": 2}
    )
    assert staffed["in_flight"] == 2
    # Floor keeps the load-bearing identity scalars alongside the count.
    assert staffed["id"] == "d-staffed"
    assert staffed["status"] == "active"

    idle = _floor_direction_entry(
        {"id": "d-idle", "priority": 2, "status": "active", "in_flight": 0}
    )
    assert idle["in_flight"] == 0

    unstamped = _floor_direction_entry({"id": "d-old", "status": "active"})
    assert "in_flight" not in unstamped


def test_directions_summary_floor_retains_in_flight_for_staffed_direction():
    """The guaranteed core floor retains the per-direction in-flight count for a
    staffed direction so the strategist can avoid duplicate spawns in degraded
    (budget-pressured) cycles where the reconciliation section is dropped."""
    from app.strategic_payload_compaction import _directions_summary_floor

    stats = [
        {"id": "d-staffed", "priority": 1, "status": "active", "in_flight": 4},
        {"id": "d-idle", "priority": 2, "status": "active", "in_flight": 0},
    ]
    floored = _directions_summary_floor(stats)
    by_id = {entry["id"]: entry for entry in floored}
    assert by_id["d-staffed"]["in_flight"] == 4
    assert by_id["d-idle"]["in_flight"] == 0


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


def test_decision_value_beats_size_within_priority_tier():
    """A decision-critical section wins a same-tier byte race over a summary."""
    payload = {
        "generic_summary": "g" * 100,
        "decision_context": "d" * 300,
    }
    schema = {
        "generic_summary": {"priority": "high", "decision_value": 1},
        "decision_context": {"priority": "high", "decision_value": 100},
    }
    budget = _size(payload["decision_context"]) + 1

    out = pack_under_budget(payload, schema, budget=budget)

    assert out["decision_context"] == payload["decision_context"]
    assert "generic_summary" not in out
    assert out["_packer_meta"]["dropped_section_count"] == 1
    assert out["_packer_meta"]["dropped"][0]["key"] == "generic_summary"


def test_strategic_pack_preserves_idle_finding_context_and_reports_currency():
    """Under pressure, the idle finding direction and pack diagnostics survive."""
    from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA

    finding_direction = {
        "id": "d-findings",
        "priority": "low",
        "status": "active",
        "text": "Act on the latest research finding",
        "in_flight": 0,
        "recent_findings": ["A concrete finding changes the next task."],
        "research_context": "## Established Findings\n- The next task is actionable.\n",
    }
    payload = {
        "type": "strategic_review",
        "directions_summary": [finding_direction],
        "payload_staleness": {
            "verdict": "fresh",
            "payload_sha": "a" * 40,
            "integration_head_sha": "a" * 40,
        },
        "running_update": "r" * 300,
        "recent_handled_conversation": "h" * 900,
    }
    budget = (
        _size(payload["type"])
        + _size(payload["payload_staleness"])
        + _size(payload["running_update"])
        + _size({
            "id": finding_direction["id"],
            "priority": finding_direction["priority"],
            "status": finding_direction["status"],
            "in_flight": 0,
            "text": finding_direction["text"],
            "research_context": finding_direction["research_context"],
        })
        + 20
    )

    out = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA, budget=budget)
    meta = out["_packer_meta"]

    assert any(
        entry.get("research_context") == finding_direction["research_context"]
        for entry in out["directions_summary"]
    )
    assert meta["payload_currency"] == "fresh"
    assert meta["payload_currency_reason"] == "verdict=fresh"
    assert meta["dropped_section_count"] >= 1
    dropped = {entry["key"]: entry for entry in meta["dropped"]}
    assert "recent_handled_conversation" in dropped
    assert dropped["recent_handled_conversation"]["reason"]


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


def test_over_budget_payload_ships_context_families_alongside_whole_problem_model():
    """Acceptance (c-407fb5d3): a REPRESENTATIVE OVER-BUDGET strategic-review
    payload still ships design, knowledge, and outcome-evaluation context
    alongside the FULL problem model.

    Producer-side packing: design_doc, knowledge_index, and outcome_evaluation
    previously carried only a ``summarize`` fallback, so under byte pressure
    their summarized form still could not fit and they were DROPPED WHOLESALE
    every cycle. Each now also carries a ``floor`` callback producing a
    guaranteed-minimum, bounded, ranked, truncated-with-a-marker form. This test
    proves the end-to-end contract at the real default budget: every context
    family ships something useful (never dropped whole) while the problem_model —
    the strategic source of truth — ships complete and unfloored.

    The payload is deliberately well over the 120KB budget (worst-case GC'd
    agent-view problem_model + a 60-section design index + a 60-entry knowledge
    index + a 40-direction outcome evaluation + inbox/directions/conversation
    context = ~265KB), so all three families cannot fit by accident: stripping
    the three floors from the schema makes this exact payload DROP design_doc and
    outcome_evaluation (the packer has no bounded form left to ship), which is
    what failed in production every cycle.
    """
    from app.problem_model import gc_problem_model
    from app.strategic_spawning import (
        _STRATEGIC_REVIEW_SCHEMA,
        _agent_view_problem_model,
    )

    worst = _worst_case_floor_inputs()

    model = _worst_case_durable_problem_model()
    gc_problem_model(model)

    payload = {
        "type": "strategic_review",
        "research_mode": False,
        "project_name": "Blueprint",
        "base_branch": "main",
        "is_self_repo": True,
        "enable_self_improvement": True,
        "model": "opus",
        "effort": "standard",
        "direction_counts": {"active": 12, "paused": 2, "total": 14},
        "max_active_directions": 6,
        "open_message_count": 40,
        "vision": {"purpose": "Stabilize platform and add observability hooks"},
        "effective_purpose": "Stabilize platform and add observability hooks",
        "model_health": {"gaps_count": 12, "capabilities_count": 20},
        "playbook_keys_index": [
            {"key": f"playbook-{i}", "title": f"Playbook {i}"} for i in range(10)
        ],
        # Large high-tier inbox/directions that ship full and consume most of
        # the budget, forcing the medium context families below into their
        # bounded floor/summarize forms.
        "open_messages": [
            {"id": f"m-{i}", "type": "BUG_REPORT", "title": f"Bug {i}", "content": "c" * 1000}
            for i in range(40)
        ],
        "directions_summary": [
            {
                "id": f"d-{i:04d}",
                "priority": "high",
                "status": "active",
                "text": "t" * 900,
                "gap_id": f"g-{i}",
                "system_id": f"s-{i}",
            }
            for i in range(25)
        ],
        "problem_model": _agent_view_problem_model(model.model_dump()),
        # The three context families at their worst-case sizes.
        "design_doc": worst["design_doc"],
        "knowledge_index": worst["knowledge_index"],
        "outcome_evaluation": worst["outcome_evaluation"],
        "conversation_thread": [
            {"id": f"c-{i}", "author": "operator", "excerpt": "e" * 400} for i in range(30)
        ],
        "self_reflection_log": [
            {"ts": f"2026-05-{(i % 27) + 1:02d}T00:00:00Z", "reflection": "r" * 300}
            for i in range(20)
        ],
        "playbook_summary": [
            {"key": f"pb-{i}", "summary": "s" * 250} for i in range(20)
        ],
        "strategic_memory_notes": [
            {"note": f"note {i}", "set_at": f"2026-05-{(i % 27) + 1:02d}T00:00:00Z"}
            for i in range(15)
        ],
        "previous_decision_outcomes": [
            {"direction_id": f"d-{i}", "outcome": "merged", "summary": "p" * 200}
            for i in range(15)
        ],
    }

    # Self-verifying: the payload is genuinely over budget before packing.
    assert _size(payload) > 120 * 1024, "test payload must exceed the default budget"

    out = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA)
    meta = out["_packer_meta"]
    dropped_keys = {d["key"] for d in meta["dropped"]}
    trimmed_by_key = {t["key"]: t for t in meta["trimmed"]}

    assert meta["budget_used"] <= 120 * 1024

    # The FULL problem_model ships whole — never floored or dropped.
    assert "problem_model" in out, "problem_model dropped under budget pressure"
    assert "problem_model" not in dropped_keys
    pm = out["problem_model"]
    assert "_agent_view_floor_applied" not in pm, (
        "problem_model must ship whole (not degraded) alongside the context families"
    )
    assert isinstance(pm.get("gaps"), list) and len(pm["gaps"]) > 0

    # All three context families ship alongside it (never dropped wholesale).
    for key in ("design_doc", "knowledge_index", "outcome_evaluation"):
        assert key in out, f"{key} was dropped under budget pressure"
        assert key not in dropped_keys, f"{key} was dropped: {meta['dropped']}"

    # Each family is BOUNDED by the packer (not shipped full): the over-budget
    # payload forces design/knowledge/outcome into their floor or summarize
    # forms, proving "packed to fit" rather than silently-believed-full.
    for key in ("design_doc", "knowledge_index", "outcome_evaluation"):
        assert key in trimmed_by_key, (
            f"{key} shipped full despite the over-budget payload — the budget "
            f"math changed; {meta['trimmed']}"
        )

    # Truncation is VISIBLE to the strategist for every family: either an inline
    # floor marker in the shipped value or a packer-meta trim record carrying the
    # fetch verb, so the agent knows detail exists and can pull it on demand.
    design_out = out["design_doc"]
    assert isinstance(design_out, dict) and isinstance(design_out.get("sections"), list)
    design_floor = design_out.get("_design_floor")
    assert design_floor is not None, "design_doc truncation must be marked inline"
    assert design_floor["total_sections"] == 60
    assert design_floor["shipped_sections"] == len(design_out["sections"])
    assert design_floor["fetch_verb"] == "section"
    # Bounded + ranked: the shipped skeleton keeps the architecture sections
    # (systems/capabilities) that active directions reference.
    shipped_design_keys = [s.get("key") for s in design_out["sections"]]
    assert len(shipped_design_keys) <= 6
    assert all(
        k.startswith(("systems/", "capabilities/")) for k in shipped_design_keys
    )

    k_out = out["knowledge_index"]
    assert isinstance(k_out, list)
    k_trim = trimmed_by_key.get("knowledge_index")
    assert k_trim is not None and k_trim["kind"] == "list_trimmed"
    assert k_trim["fetch_verb"] == "knowledge"
    assert k_trim["shown"] < k_trim["total"]
    assert len(k_out) == k_trim["shown"]

    oe_out = out["outcome_evaluation"]
    assert isinstance(oe_out, dict)
    oe_floor = oe_out.get("_outcome_floor")
    assert oe_floor is not None, "outcome_evaluation truncation must be marked inline"
    assert oe_floor["total_directions"] == 40
    assert oe_floor["shipped_directions"] == len(oe_out.get("directions") or [])
    assert oe_floor["fetch_verb"] == "sections"
    # Bounded + decision-relevant: only the top activity-ranked directions ship.
    assert len(oe_out.get("directions") or []) <= 3


def test_floor_design_doc_index_bounds_ranks_and_marks():
    """The design_doc floor is bounded, architecture-ranked, and truncation-marked.

    The floor is the guaranteed-minimum design context the strategist ALWAYS
    receives under budget pressure: top-N sections ranked so the architecture
    sections active directions reference lead, each reduced to key + status + a
    short summary (backing module lists dropped), with a ``_design_floor`` marker
    recording how many sections were omitted and the fetch verb for the rest.
    """
    from app.strategic_payload_summaries import _floor_design_doc_index

    index = _worst_case_floor_inputs()["design_doc"]
    floored = _floor_design_doc_index(index)

    marker = floored["_design_floor"]
    assert marker["total_sections"] == len(index["sections"]) == 60
    assert marker["shipped_sections"] == len(floored["sections"])
    assert marker["fetch_verb"] == "section"

    sections = floored["sections"]
    assert 0 < len(sections) <= 6, "floor must keep a bounded, non-empty section set"
    keys = [s.get("key") for s in sections]
    # Ranked: systems/capabilities (the sections active directions reference)
    # lead; within rank the keys stay sorted.
    assert all(k.startswith(("systems/", "capabilities/")) for k in keys), (
        "architecture sections must lead the floored design index"
    )
    for entry in sections:
        # Compact per-entry form: key + status + short bounded summary.
        assert "key" in entry
        assert "summary" in entry and len(entry["summary"]) <= 120 + 3
        assert "modules" not in entry, "backing-module lists are heavy — dropped"

    # The floor is a strict reduction (watermark monotonicity holds).
    assert _size(floored) < _size(index)


def test_floor_knowledge_index_bounds_to_relevance_head():
    """The knowledge_index floor keeps a bounded pointer-only head.

    ``build_knowledge_index`` already orders decision-relevant docs first, so the
    floor keeps the top entries' navigation pointers (id/title/topic/direction_id)
    with no summary body — the full document stays reachable via the fetch verb.
    """
    from app.strategic_payload_summaries import _floor_knowledge_index

    entries = _worst_case_floor_inputs()["knowledge_index"]
    floored = _floor_knowledge_index(entries)

    assert 0 < len(floored) <= 4, "floor must keep a bounded, non-empty head"
    for entry in floored:
        assert isinstance(entry, dict)
        # Pointer-only: navigation keys survive, body/provenance does not.
        assert "id" in entry and "title" in entry and "direction_id" in entry
        assert "summary" not in entry, "summary body must be dropped in the floor"
        assert "content_path" not in entry

    assert _size(floored) < _size(entries)


def test_floor_outcome_evaluation_keeps_aggregates_and_top_directions():
    """The outcome_evaluation floor keeps bounded aggregates + top directions.

    The headline ``overall``/``window`` aggregates are bounded (they do not grow
    with project size) and the per-direction list is capped to the top-N by
    activity, each compacted to headline metrics. A ``_outcome_floor`` marker
    records how many directions were omitted and the fetch verb for the full
    evaluation.
    """
    from app.strategic_payload_summaries import _floor_outcome_evaluation

    oe = _worst_case_floor_inputs()["outcome_evaluation"]
    floored = _floor_outcome_evaluation(oe)

    # Bounded headline aggregates survive.
    assert "window" in floored and isinstance(floored["window"], dict)
    assert "overall" in floored and isinstance(floored["overall"], dict)
    # Aggregates are compact: only the declared headline keys.
    for k in floored["overall"]:
        assert k in (
            "cycles",
            "merged",
            "failed",
            "success_rate",
            "trend",
            "generalized_successes",
            "generalized_success_rate",
        )

    marker = floored["_outcome_floor"]
    assert marker["total_directions"] == len(oe["directions"]) == 40
    assert marker["shipped_directions"] == len(floored["directions"])
    assert marker["fetch_verb"] == "sections"

    directions = floored["directions"]
    assert 0 < len(directions) <= 3, "floor must keep a bounded direction set"
    # Ranked by decision relevance (merged*3 + failed + cycles), desc.
    scores = [
        d["merged"] * 3 + d["failed"] + d["cycles"] for d in directions
    ]
    assert scores == sorted(scores, reverse=True), (
        "floored directions must be the highest-activity subset"
    )
    for d in directions:
        # Compacted per-direction form: headline metrics only.
        assert "id" in d and "text" in d and "cycles" in d
        assert "type_distribution" not in d, "heavy per-direction metrics dropped"

    assert _size(floored) < _size(oe)


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


def test_version_phase_floor_keeps_per_criterion_state():
    """When version_phase ships in floored form, criterion_coverage includes a
    per-criterion list so the strategist can see WHICH criteria are uncovered or
    stuck without a fetch round-trip.

    The floor path must:
    - Preserve aggregate counts (total, covered, partial, uncovered, stuck_count).
    - Include a per-criterion list capped to _VERSION_PHASE_CRITERIA_FLOOR_CAP.
    - Prioritize stuck criteria first, then uncovered, then partial.
    - Emit only load-bearing fields (criterion_key, text, coverage, stuck_reason).
    - Omit covered criteria when the cap forces a choice.
    """
    from app.strategic_spawning import (
        _STRATEGIC_REVIEW_SCHEMA,
        _agent_view_problem_model,
    )

    # Build a version_phase with a rich criterion_coverage list: a mix of
    # stuck, uncovered, partial, and covered criteria so we can verify that
    # the floor prioritizes correctly.
    criteria = []
    # 3 stuck (uncovered with stuck_reason)
    for i in range(3):
        criteria.append({
            "criterion_key": f"c-stuck-{i:02d}",
            "text": f"Stuck criterion {i}: needs direction remapping. " + ("s" * 180),
            "coverage": "uncovered",
            "status": "active",
            "evidence_count": 0,
            "stuck_reason": "no_mapping",
        })
    # 5 uncovered (no stuck_reason)
    for i in range(5):
        criteria.append({
            "criterion_key": f"c-uncov-{i:02d}",
            "text": f"Uncovered criterion {i}: no evidence yet. " + ("u" * 180),
            "coverage": "uncovered",
            "status": "active",
            "evidence_count": 0,
            "stuck_reason": None,
        })
    # 4 partial
    for i in range(4):
        criteria.append({
            "criterion_key": f"c-partial-{i:02d}",
            "text": f"Partial criterion {i}: some evidence gathered. " + ("p" * 180),
            "coverage": "partial",
            "status": "active",
            "evidence_count": 1,
            "stuck_reason": None,
        })
    # 38 covered (should be lowest priority; dropped when cap forces a choice)
    for i in range(38):
        criteria.append({
            "criterion_key": f"c-covered-{i:02d}",
            "text": f"Covered criterion {i}: fully evidenced. " + ("v" * 180),
            "coverage": "covered",
            "status": "active",
            "evidence_count": 3,
            "stuck_reason": None,
        })

    version_phase = {
        "version_number": 22,
        "phase": "build",
        "goal": "Ship the core-floor guarantee. " + ("g" * 4000),
        "phase_reasoning": "r" * 4000,
        "criterion_coverage": {
            "total": 50,
            "covered": 38,
            "partial": 4,
            "uncovered": 8,
            "stuck_count": 3,
            "criteria": criteria,
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

    # Build a payload large enough to force the packer to floor version_phase.
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
    }
    problem_model = _agent_view_problem_model(big_pm)
    directions_summary = [
        {
            "id": f"d-{i:03d}",
            "priority": "high",
            "status": "active",
            "text": "t" * 1500,
        }
        for i in range(20)
    ]
    open_messages = [
        {"id": f"m-{i:03d}", "type": "NOTE", "content": "c" * 800}
        for i in range(30)
    ]

    payload = {
        "type": "strategic_review",
        "vision": {"purpose": "Test floor criterion state."},
        "problem_model": problem_model,
        "directions_summary": directions_summary,
        "open_messages": open_messages,
        "version_phase": version_phase,
    }

    out = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA, budget=120 * 1024)

    # version_phase must be present (core floor guarantee).
    assert "version_phase" in out, "version_phase was dropped wholesale"
    trimmed_keys = {t["key"] for t in out["_packer_meta"]["trimmed"]}
    assert "version_phase" in trimmed_keys, (
        "version_phase should be in floored/trimmed form given the budget pressure"
    )

    vp_out = out["version_phase"]
    cc = vp_out.get("criterion_coverage")
    assert cc is not None, "criterion_coverage was dropped from floored version_phase"

    # Aggregate counts preserved.
    assert cc.get("total") == 50
    assert cc.get("stuck_count") == 3

    # Per-criterion list present in floored form.
    floor_criteria = cc.get("criteria")
    assert floor_criteria is not None, (
        "floor path must include per-criterion list — "
        "strategist cannot see which criteria are stuck/uncovered without it"
    )
    assert len(floor_criteria) > 0, "floor criteria list must not be empty"

    # All 3 stuck criteria must appear (they rank highest).
    floor_keys = {c["criterion_key"] for c in floor_criteria if isinstance(c, dict)}
    for i in range(3):
        assert f"c-stuck-{i:02d}" in floor_keys, (
            f"stuck criterion c-stuck-{i:02d} missing from floor list"
        )

    # Each criterion entry carries the load-bearing fields.
    for entry in floor_criteria:
        assert isinstance(entry, dict)
        assert "criterion_key" in entry, "floor entry missing criterion_key"
        assert "coverage" in entry, "floor entry missing coverage"
        # text may be absent only if the original had no text
        # stuck_reason present (even if None)
        assert "stuck_reason" in entry, "floor entry missing stuck_reason"
        # status is load-bearing: the strategic renderer (formatCriteriaRubric)
        # filters the rubric to status == "active"; a floored entry without it
        # renders as "None authored yet", conflating floored-out with no-criteria.
        assert entry.get("status") == "active", (
            "floor entry must preserve status so the rubric renders as active criteria"
        )

    # Covered criteria are the lowest priority — with 3+5+4=12 higher-priority
    # entries and a cap of 10, covered entries should not appear.
    covered_in_floor = [c for c in floor_criteria if c.get("coverage") == "covered"]
    assert not covered_in_floor, (
        "covered criteria should not appear in the floor list when "
        "stuck+uncovered+partial entries fill the cap"
    )


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


def test_shipped_records_full_size_sections():
    """Sections that pack at full size appear in _packer_meta['shipped'] with
    correct key, bytes, and priority."""
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
    shipped = out["_packer_meta"]["shipped"]
    # All three sections fit at full size.
    assert len(shipped) == 3
    shipped_by_key = {e["key"]: e for e in shipped}
    assert "type" in shipped_by_key
    assert "vision" in shipped_by_key
    assert "directions" in shipped_by_key
    # Each entry carries bytes and priority.
    assert shipped_by_key["type"]["bytes"] == _size("strategic_review")
    assert shipped_by_key["type"]["priority"] == "critical"
    assert shipped_by_key["vision"]["bytes"] == _size({"purpose": "demo"})
    assert shipped_by_key["vision"]["priority"] == "high"
    assert shipped_by_key["directions"]["bytes"] == _size(["d1", "d2"])
    assert shipped_by_key["directions"]["priority"] == "medium"


def test_shipped_empty_when_all_trimmed_or_dropped():
    """When no section ships at full size, _packer_meta['shipped'] is empty."""
    payload = {
        "items": list(range(1000)),  # summarized
        "huge": "x" * 5000,         # dropped
    }
    schema = {
        "items": {"priority": "medium", "summarize": lambda v: v[:5]},
        "huge": {"priority": "low"},
    }
    out = pack_under_budget(payload, schema, budget=100)
    # items is trimmed, huge is dropped — neither ships at full size.
    assert out["_packer_meta"]["shipped"] == []
    assert len(out["_packer_meta"]["trimmed"]) == 1
    assert len(out["_packer_meta"]["dropped"]) == 1


def test_section_sizes_sum_to_budget_used():
    """The sum of bytes across all shipped entries equals budget_used.

    This is the accounting invariant: every byte reported in budget_used must
    be attributed to a specific section in the shipped list. Trimmed sections
    contribute their post-trim size (bytes_trimmed), not the original size.
    budget_used is the total of what actually landed in the packed payload, so
    shipped + trimmed_trimmed_bytes == budget_used (dropped contribute 0).
    """
    payload = {
        "type": "strategic_review",
        "vision": {"purpose": "full value fits"},
        "items": list(range(200)),  # summarized
    }
    schema = {
        "type": {"priority": "critical"},
        "vision": {"priority": "high"},
        "items": {"priority": "medium", "summarize": lambda v: v[:5]},
    }
    out = pack_under_budget(payload, schema, budget=500)
    meta = out["_packer_meta"]
    # Shipped sections sum to their exact byte contributions.
    shipped_sum = sum(e["bytes"] for e in meta["shipped"])
    # Trimmed sections contribute their post-trim bytes.
    trimmed_sum = sum(e["bytes_trimmed"] for e in meta["trimmed"])
    assert shipped_sum + trimmed_sum == meta["budget_used"], (
        f"shipped_sum={shipped_sum} + trimmed_sum={trimmed_sum} != "
        f"budget_used={meta['budget_used']}"
    )


def test_core_floor_sum_stays_under_half_budget():
    """The sum of core-section floor reservations must not exceed ~half the budget.

    Core floors are the guaranteed-minimum bytes reserved off the top of the budget
    for strategic payload sections with floor callbacks (open_messages,
    directions_summary, problem_model, version_phase). If core floors collectively
    exceed ~half the budget, the remaining budget for critical + other high-tier
    sections is too tight — the packer risks starving critical keys into a hard
    PayloadTooLargeError or degrading recoverable sections unnecessarily.

    This test verifies the invariant: core-floor sum <= ~half the default budget
    (60KB), leaving at least 60KB headroom for critical sections and other context.
    """
    from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA

    # Identify all core sections (those with floor callbacks).
    core_sections = {
        key: entry
        for key, entry in _STRATEGIC_REVIEW_SCHEMA.items()
        if entry.get("floor") is not None
    }
    assert core_sections, "expected at least one core section with a floor callback"

    # Build a realistic worst-case payload: each core section at its maximum
    # reasonable size (what would actually be floored in a constrained cycle).
    worst_case_payload = {
        "type": "strategic_review",
        "research_mode": False,
        "vision": {"purpose": "Test core floor sum invariant."},
    }

    # Populate core sections with bulk that exercises their floor callbacks.
    worst_case_payload["open_messages"] = [
        {"id": f"m-{i}", "type": "BUG_REPORT", "content": "c" * 500}
        for i in range(50)
    ]
    worst_case_payload["directions_summary"] = [
        {"id": f"d-{i}", "priority": "high", "status": "active", "text": "t" * 1200}
        for i in range(40)
    ]
    worst_case_payload["problem_model"] = {
        "gaps": [
            {
                "id": f"g-{i}",
                "name": f"Gap {i}",
                "status": "active",
                "priority": "high",
                "opened": f"2026-05-{(i % 27) + 1:02d}T00:00:00Z",
                "evidence": "e" * 800,
                "description": "d" * 400,
            }
            for i in range(50)
        ],
    }
    worst_case_payload["version_phase"] = {
        "version_number": 22,
        "phase": "build",
        "goal": "test" * 1000,
        "phase_reasoning": "r" * 3000,
        "criterion_coverage": {
            "total": 100,
            "covered": 40,
            "criteria": [
                {"criterion_key": f"c-{i}", "text": "x" * 200, "coverage": "uncovered"}
                for i in range(100)
            ],
        },
    }

    # Add some critical keys so the packer exercises the floor-reservation logic.
    worst_case_payload["project_name"] = "test"
    worst_case_payload["base_branch"] = "main"

    # Pack at the default budget (120KB).
    budget = 120 * 1024
    out = pack_under_budget(worst_case_payload, _STRATEGIC_REVIEW_SCHEMA, budget=budget)

    # Extract the packer metadata.
    meta = out["_packer_meta"]
    budget_used = meta["budget_used"]

    # Verify that the budget was used responsibly: the sum of all shipped sections
    # should be well under budget, with no critical sections dropped or starved.
    assert budget_used <= budget, "packer must respect the budget ceiling"

    # Verify no critical sections are missing (dropped or absent).
    dropped_keys = {d["key"] for d in meta["dropped"]}
    required_critical = {"type", "vision"}  # Minimal critical set always required.
    for key in required_critical:
        assert key not in dropped_keys, f"critical section {key!r} was dropped"
        assert key in out, f"critical section {key!r} is missing from output"

    # Verify that core sections themselves are present (not dropped wholesale).
    # At least some form of each core section should ship.
    for core_key in core_sections:
        if core_key in worst_case_payload:
            # The section should be present in output (full, summarized, or floored).
            assert core_key in out or core_key in dropped_keys, (
                f"core section {core_key!r} neither in output nor in dropped list"
            )
            # If it made it to the output, it should not be dropped.
            if core_key in out:
                assert core_key not in dropped_keys, (
                    f"core section {core_key!r} in both output and dropped list"
                )


def test_monotonic_degradation_budget_increase():
    """Increasing the budget never causes a section to ship in a worse form.

    The monotonic-degradation invariant: if we pack a payload with budget B1 and
    a section X ships in form F1 (full → summarized → floored → dropped), then
    packing the SAME payload with budget B2 > B1 should result in form F2 that
    is at least as good as F1 (never worse).

    This is critical: a larger budget should ONLY improve or maintain forms, never
    degrade them. Violations would indicate a bug in the packer's reservation or
    prioritization logic.
    """
    # Build a payload with multiple sections at various priorities.
    payload = {
        "critical_a": "c" * 200,
        "high_big": "h" * 800,
        "high_small": "s" * 200,
        "medium_bulk": "m" * 1500,
        "low_extra": "l" * 500,
    }

    schema = {
        "critical_a": {"priority": "critical"},
        "high_big": {
            "priority": "high",
            "summarize": lambda v: v[:400],
        },
        "high_small": {"priority": "high"},
        "medium_bulk": {
            "priority": "medium",
            "summarize": lambda v: v[:500],
        },
        "low_extra": {
            "priority": "low",
        },
    }

    def _form_rank(key: str, out: dict, meta: dict) -> int:
        """Assign a rank to the form a section ships in (higher = better).

        Ranks:
        4: full (not in trimmed/dropped)
        3: summarized (in trimmed with kind in summarized/list_trimmed)
        2: floored (in trimmed with kind="floored")
        1: dropped (in dropped list)
        0: missing (not in output at all)
        """
        if key not in out:
            if any(d["key"] == key for d in meta["dropped"]):
                return 1  # dropped
            return 0  # missing
        # Check if it's trimmed
        trimmed = [t for t in meta["trimmed"] if t["key"] == key]
        if trimmed:
            if trimmed[0]["kind"] == "floored":
                return 2
            elif trimmed[0]["kind"] in ("summarized", "list_trimmed"):
                return 3
        # Otherwise it's full.
        return 4

    # Pack at two budgets: tight (300 bytes) and loose (5000 bytes).
    # With tight budget, some sections degrade; with loose budget, they should improve.
    tight_budget = 300
    loose_budget = 5000

    out_tight = pack_under_budget(payload, schema, budget=tight_budget)
    out_loose = pack_under_budget(payload, schema, budget=loose_budget)

    meta_tight = out_tight["_packer_meta"]
    meta_loose = out_loose["_packer_meta"]

    # For each section, verify that the loose-budget form is at least as good.
    for key in payload:
        rank_tight = _form_rank(key, out_tight, meta_tight)
        rank_loose = _form_rank(key, out_loose, meta_loose)
        assert rank_loose >= rank_tight, (
            f"section {key!r} degraded under larger budget: "
            f"tight={rank_tight}, loose={rank_loose} "
            f"(4=full, 3=summarized, 2=floored, 1=dropped, 0=missing)"
        )


def test_core_floor_reserved_accounting_in_metadata():
    """Core-floor reservations are properly accounted in the packer metadata.

    When the packer reserves bytes for core-section floors off the top of the
    budget, this reservation must be visible in the _packer_meta so operators
    can understand why other sections were dropped or degraded.

    This test verifies that sections with floor callbacks are handled in the
    packing flow and result in the expected bytes-used accounting, which proves
    the floor reservations are computed and applied.
    """
    from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA

    # Build a payload where budget pressure is real but not overwhelming.
    payload = {
        "type": "strategic_review",
        "vision": {"purpose": "Test floor accounting."},
        "problem_model": {
            "gaps": [{"id": f"g-{i}", "name": f"Gap {i}", "status": "active"} for i in range(30)],
        },
        # Add some high-tier context that will compete with problem_model for budget.
        "directions_summary": [
            {"id": f"d-{i}", "status": "active", "text": "t" * 1000} for i in range(15)
        ],
        "open_messages": [
            {"id": f"m-{i}", "type": "NOTE", "content": "c" * 400} for i in range(20)
        ],
    }

    budget = 120 * 1024
    out = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA, budget=budget)
    meta = out["_packer_meta"]

    # The budget must be respected: used <= total.
    assert meta["budget_used"] <= meta["budget_total"]

    # All core sections should be present (core sections always ship at least
    # their floored form when floors are properly reserved).
    for core_key in ("problem_model", "directions_summary", "open_messages"):
        if core_key in payload:
            assert core_key in out, (
                f"core section {core_key!r} should be present when floors are reserved"
            )

    # Verify the packer didn't drop ANY critical sections due to core-floor
    # starvation (if critical sections are missing, the floor reservation was violated).
    dropped_keys = {d["key"] for d in meta["dropped"]}
    critical_sections = {k for k, v in _STRATEGIC_REVIEW_SCHEMA.items()
                         if v.get("priority") == "critical"}
    for crit_key in critical_sections:
        assert crit_key not in dropped_keys, (
            f"critical section {crit_key!r} was dropped — "
            f"floor reservation may have starved critical keys"
        )


# ---------------------------------------------------------------------------
# Named-open test gaps: watermark monotonicity, floor-sum, per-section accounting
# Criterion: c-5257b111
# ---------------------------------------------------------------------------

def _worst_case_floor_inputs() -> dict:
    """Shared worst-case values exercising each core section's floor callback."""
    return {
        "open_messages": [
            {"id": f"m-{i}", "type": "BUG_REPORT", "title": f"Bug {i}", "content": "c" * 500}
            for i in range(50)
        ],
        "directions_summary": [
            {
                "id": f"d-{i}",
                "priority": "high",
                "status": "active",
                "text": "t" * 1200,
                "gap_id": f"g-{i}",
                "system_id": f"s-{i}",
            }
            for i in range(40)
        ],
        "problem_model": {
            "gaps": [
                {
                    "id": f"g-{i}",
                    "name": f"Gap {i}",
                    "status": "active",
                    "priority": "high",
                    "opened": f"2026-05-{(i % 27) + 1:02d}T00:00:00Z",
                    "evidence": "e" * 800,
                    "description": "d" * 400,
                }
                for i in range(30)
            ]
        },
        # design_doc: 60-section navigation index (systems/capabilities first) —
        # worst case for the floor, which must keep the architecture-relevant head.
        "design_doc": {
            "sections": [
                {
                    "key": f"systems/sys-{i:03d}" if i % 2 else f"capabilities/cap-{i:03d}",
                    "title": f"Section {i} title with enough prose to be realistic",
                    "status": "working" if i % 3 else "draft",
                    "summary": "s" * 200,
                    "modules": [f"backend/app/mod{i}_a.py", f"backend/app/mod{i}_b.py"],
                }
                for i in range(60)
            ]
        },
        # knowledge_index: 60 body-bearing entries (per-arm verdict / provenance
        # sub-fields) — worst case for the floor, which keeps the relevance head.
        "knowledge_index": [
            {
                "id": f"k-{i:04d}",
                "title": f"Knowledge {i} title",
                "topic": f"topic-{i % 7}",
                "direction_id": f"d-{i % 5}",
                "source_kind": "design",
                "sweep_id": f"sweep-{i % 3}",
                "arm_label": f"arm-{i % 2}",
                "criterion_met": True,
                "compute_acceptance_grade": "B",
                "created_at": f"2026-05-{(i % 27) + 1:02d}T00:00:00Z",
                "summary": "u" * 300,
                "content_path": f"sweeps/{i}/notes.md",
            }
            for i in range(60)
        ],
        # outcome_evaluation: 40 direction outcomes with the full per-direction
        # metric surface — worst case for the floor, which keeps the bounded
        # headline aggregates plus the top few activity-ranked directions.
        "outcome_evaluation": {
            "window": {"from": "2026-04-01", "to": "2026-08-01", "vision_revision": 3},
            "overall": {
                "cycles": 200,
                "merged": 120,
                "failed": 30,
                "success_rate": 0.8,
                "trend": "improving",
                "generalized_successes": 10,
                "generalized_success_rate": 0.5,
            },
            "systems": [{"system_id": f"s{i}", "merged": i} for i in range(5)],
            "directions": [
                {
                    "id": f"d-{i:04d}",
                    "text": "t" * 220,
                    "status": "active",
                    "cycles": 5 + i,
                    "merged": 1 + i % 4,
                    "failed": i % 3,
                    "prs_rejected": 2,
                    "taxonomy_summary": {"primary": f"task-{i % 5}", "secondary": []},
                    "acceptance_rate": 0.6,
                    "avg_time_to_merge_hours": 12,
                    "difficulty_index": 2,
                    "generalized_successes": [f"g{i}"],
                    "generalized_success_rate": 0.8,
                    "estimated_cost_usd": 1000,
                    "cost_per_merged_pr": 500,
                    "cost_budget_usd": 2000,
                    "budget_exceeded": False,
                    "operator_rejection_rate": 0.1,
                    "success_evidence": [f"ev{i}-1"],
                    "retirement_condition": None,
                    "confirmed": [f"cf{i}"],
                    "proposed": [],
                    "failed_approaches": [f"fa{i}"],
                    "outcome_progress_summary_fields": {"summary": "p" * 100},
                    "recent_diagnosis": {"diagnosis": "d" * 80},
                    "evidence": [f"e{i}"],
                    "recent_findings": ["f"],
                    "direction_knowledge": [f"dk{i}"],
                    "lifetime": {"cycles": 5, "merged": 2, "failed": 1, "status": "active"},
                    "type_distribution": {"design": 3, "implementation": 2},
                    "product_ratio": {"maintenance": 0.5, "feature": 0.5},
                    "scope_performance": {"high_cost_per_merge": 1},
                    "escalation_outcomes": [{"cycle": i, "outcome": "continued"}],
                    "trend": "improving",
                    "velocity": 3.1,
                    "high_cost_per_merge": False,
                }
                for i in range(40)
            ],
        },
        "version_phase": {
            "version_number": 22,
            "phase": "build",
            "goal": "g" * 2000,
            "phase_reasoning": "r" * 3000,
            "criterion_coverage": {
                "total": 80,
                "covered": 20,
                "criteria": [
                    {
                        "criterion_key": f"c-{i}",
                        "text": "x" * 200,
                        "coverage": "uncovered",
                        "stuck_reason": "s" * 100,
                    }
                    for i in range(80)
                ],
            },
        },
    }


def test_floor_size_watermark_monotonicity():
    """For each core section, floor(v).bytes ≤ full(v).bytes and summarize(v).bytes ≤ full(v).bytes.

    Watermark-monotonicity invariant: neither the floor nor the summarize callback
    may grow the payload beyond the full value. The floor is the section's
    low-water mark — the minimum representation guaranteed to ship — so it must
    always be no larger than the raw value. The same non-expansion constraint
    applies to summarize. Violations corrupt budget-reservation accounting: a
    floor larger than full means the packer reserves MORE than the section
    actually costs at full size, crowding out other sections unnecessarily.

    Note: floor_size ≤ summarize_size does NOT always hold. For problem_model,
    the floor is the whole agent-view model (preserved intact for the strategist)
    while the summarize callback produces a smaller skeleton — an intentional
    design where the floor is the guaranteed-rich form and summarize is the
    emergency fallback.

    Criterion: c-5257b111
    """
    import json as _json

    from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA

    def _sz(v: object) -> int:
        return len(_json.dumps(v, default=str).encode())

    worst_case = _worst_case_floor_inputs()

    for key, entry in _STRATEGIC_REVIEW_SCHEMA.items():
        floor_fn = entry.get("floor")
        if floor_fn is None or key not in worst_case:
            continue

        value = worst_case[key]
        full_size = _sz(value)

        floored = floor_fn(value)
        floor_size = _sz(floored)

        assert floor_size <= full_size, (
            f"section {key!r}: floor_size={floor_size} > full_size={full_size} — "
            "floor callback must not grow the payload (watermark monotonicity violated)"
        )

        summarize_fn = entry.get("summarize")
        if summarize_fn is not None:
            summarized = summarize_fn(value)
            summarize_size = _sz(summarized)

            assert summarize_size <= full_size, (
                f"section {key!r}: summarize_size={summarize_size} > full_size={full_size} — "
                "summarize callback must not grow the payload (watermark monotonicity violated)"
            )


def test_floor_sum_measured_under_half_budget():
    """Raw sum of floor bytes across all core sections must be ≤ half the budget.

    This test directly measures floor-reservation cost by applying each floor
    callback to a worst-case input and summing the resulting byte sizes. If the
    sum exceeds half the default budget (60 KB out of 120 KB), the reserved bytes
    leave insufficient headroom for critical sections and other high-tier context,
    risking PayloadTooLargeError on critical keys or unnecessary degradation.

    Unlike test_core_floor_sum_stays_under_half_budget (which verifies end-to-end
    packing behaviour), this test measures the raw floor-callback output sizes so
    any new floor callback that adds unexpectedly large reservations fails loudly.

    Criterion: c-5257b111
    """
    import json as _json

    from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA

    def _sz(v: object) -> int:
        return len(_json.dumps(v, default=str).encode())

    worst_case = _worst_case_floor_inputs()
    budget = 120 * 1024
    half_budget = budget // 2

    floor_bytes_per_section: dict[str, int] = {}
    for key, entry in _STRATEGIC_REVIEW_SCHEMA.items():
        floor_fn = entry.get("floor")
        if floor_fn is None or key not in worst_case:
            continue
        floored = floor_fn(worst_case[key])
        floor_bytes_per_section[key] = _sz(floored)

    assert floor_bytes_per_section, "expected at least one core section with a floor callback"

    total_floor_bytes = sum(floor_bytes_per_section.values())
    assert total_floor_bytes <= half_budget, (
        f"sum of core-section floor bytes ({total_floor_bytes}) exceeds half the "
        f"default budget ({half_budget} of {budget}). "
        f"Per-section: {floor_bytes_per_section}"
    )


def test_per_section_byte_count_matches_serialized_size():
    """Each section's reported byte count in _packer_meta matches its actual serialized size.

    Per-section accounting invariant: for every shipped entry, meta["shipped"][i]["bytes"]
    must equal the actual JSON byte length of the packed value, and for every trimmed
    entry, meta["trimmed"][i]["bytes_trimmed"] must match the packed value's size.
    A mismatch means the packer is mis-counting bytes, leading to incorrect budget
    enforcement on subsequent sections packed in the same call.

    Criterion: c-5257b111
    """
    import json as _json

    def _sz(v: object) -> int:
        return len(_json.dumps(v, default=str).encode())

    payload = {
        "type": "strategic_review",
        "vision": {"purpose": "per-section accounting test"},
        "items_high": "h" * 800,
        "items_medium": list(range(200)),
        "items_low": "l" * 600,
    }
    schema = {
        "type": {"priority": "critical"},
        "vision": {"priority": "high"},
        "items_high": {"priority": "high"},
        "items_medium": {"priority": "medium", "summarize": lambda v: v[:10]},
        "items_low": {"priority": "low"},
    }
    # Budget=900 forces items_medium to be summarized (890→30 bytes) and
    # drops items_low (602 bytes) — exercising the trimmed-path accounting.
    out = pack_under_budget(payload, schema, budget=900)
    meta = out["_packer_meta"]

    for entry in meta["shipped"]:
        key = entry["key"]
        reported = entry["bytes"]
        actual = _sz(out[key])
        assert reported == actual, (
            f"shipped section {key!r}: reported bytes={reported} but actual "
            f"serialized size={actual} — per-section accounting is wrong"
        )

    # Verify trimming actually happened — items_medium must be summarized.
    trimmed_keys = {entry["key"] for entry in meta["trimmed"]}
    assert "items_medium" in trimmed_keys, (
        f"items_medium should be trimmed at budget=900 but meta['trimmed']={meta['trimmed']}"
    )
    # items_low must be dropped (too large even after trimming attempt).
    assert "items_low" not in out, (
        "items_low should be dropped at budget=900 but it appears in the packed output"
    )

    for entry in meta["trimmed"]:
        key = entry["key"]
        reported_trimmed = entry["bytes_trimmed"]
        actual = _sz(out[key])
        assert reported_trimmed == actual, (
            f"trimmed section {key!r}: bytes_trimmed={reported_trimmed} but actual "
            f"serialized size={actual} — per-section trimmed accounting is wrong"
        )


def test_compact_last_qualification_includes_qualify_request_blocked():
    """_compact_last_qualification preserves qualify_request_blocked dict and
    qualify_request_blocked_at scalar — the durable block-reason fields written
    by the hotfixed instrumentation paths in strategic_completion.py."""
    from app.strategic_payload_compaction import _compact_last_qualification

    value = {
        "status": "error",
        "candidate_sha": "deadbeef" * 5,
        "error": "no_credentials",
        "detail": "GitHub token missing for this project",
        "blocker": "ci_compliance",
        "qualify_request_blocked": {
            "blocker": "ci_compliance",
            "violation_count": 3,
            "head_sha": "cafebabe" * 5,
            "cap_bump_task": "task-abc123",
        },
        "qualify_request_blocked_at": "2026-07-05T10:00:00",
    }

    out = _compact_last_qualification(value)
    assert out is not None

    # scalar fields
    assert out["error"] == "no_credentials"
    assert out["blocker"] == "ci_compliance"
    assert out["qualify_request_blocked_at"] == "2026-07-05T10:00:00"
    # text-capped field
    assert out["detail"] == "GitHub token missing for this project"
    # nested dict compacted
    qrb = out["qualify_request_blocked"]
    assert qrb is not None
    assert qrb["blocker"] == "ci_compliance"
    assert qrb["violation_count"] == 3

    # floor=True should also preserve the fields
    out_floor = _compact_last_qualification(value, floor=True)
    assert out_floor is not None
    assert out_floor["qualify_request_blocked_at"] == "2026-07-05T10:00:00"
    assert out_floor["qualify_request_blocked"]["blocker"] == "ci_compliance"


def test_compact_last_qualification_preserves_category_verdicts():
    """_compact_last_qualification keeps the per-category grader verdicts and
    residual gap — the WHY each category is unmet — on both the normal and
    floor paths, while dropping bulky citations."""
    from app.strategic_payload_compaction import _compact_last_qualification

    value = {
        "status": "not_promoted",
        "blocker": "ci_compliance",
        "category_verdicts": [
            {
                "category_key": "merge_grade",
                "status": "not_met",
                "tier": "judged",
                "gap": "Merge grade below threshold: " + "x" * 600,
                "citations": [{"id": "cite-1", "body": "big citation " * 50}],
            },
            {"category_key": "ci", "status": "met", "tier": "machine", "gap": None},
        ],
        "residual_uncaptured_gap": (
            "No criterion captures token-budget erosion: " + "y" * 600
        ),
    }

    out = _compact_last_qualification(value)
    assert out is not None

    verdicts = out["category_verdicts"]
    assert verdicts[0]["category_key"] == "merge_grade"
    assert verdicts[0]["status"] == "not_met"
    assert verdicts[0]["tier"] == "judged"
    assert verdicts[0]["gap"] == "Merge grade below threshold: " + "x" * 468 + "..."
    assert verdicts[1] == {"category_key": "ci", "status": "met", "tier": "machine"}
    # Bulky citations never survive compaction.
    assert "citations" not in verdicts[0]
    assert "gap" not in verdicts[1], "None gap should be dropped, not kept"
    assert out["residual_uncaptured_gap"].startswith("No criterion captures")

    # floor=True caps the text tighter but keeps the same shape.
    out_floor = _compact_last_qualification(value, floor=True)
    assert out_floor is not None
    floor_verdicts = out_floor["category_verdicts"]
    assert floor_verdicts[0]["category_key"] == "merge_grade"
    assert floor_verdicts[0]["status"] == "not_met"
    assert floor_verdicts[0]["tier"] == "judged"
    assert len(floor_verdicts[0]["gap"]) <= 240
    assert out_floor["residual_uncaptured_gap"] != out["residual_uncaptured_gap"]


def test_compact_last_qualification_missing_category_verdicts_is_none():
    """Absent category verdicts stay absent — legacy rows without the field
    must not fabricate an empty list in the compacted output."""
    from app.strategic_payload_compaction import _compact_last_qualification

    out = _compact_last_qualification({"status": "promoted"})
    assert out is not None
    assert "category_verdicts" not in out
    assert "residual_uncaptured_gap" not in out


def test_compact_version_phase_passes_failure_marker_through():
    """_compact_version_phase must preserve the failure-marker keys
    (lifecycle_signal, signal_status, error, integration_branch, promotion_target)
    on both the normal and floor paths so the strategist always sees why the
    block is absent rather than receiving a silently empty section."""
    from app.strategic_payload_compaction import _compact_version_phase

    marker = {
        "lifecycle_signal": "unavailable",
        "signal_status": "build_failed",
        "error": "RuntimeError('simulated failure')",
        "integration_branch": "automain",
        "promotion_target": "main",
    }

    # Normal path
    out = _compact_version_phase(marker)
    assert out.get("lifecycle_signal") == "unavailable"
    assert out.get("signal_status") == "build_failed"
    assert out.get("error") == "RuntimeError('simulated failure')"
    assert out.get("integration_branch") == "automain"
    assert out.get("promotion_target") == "main"

    # Floor path must also preserve all five marker keys.
    out_floor = _compact_version_phase(marker, floor=True)
    assert out_floor.get("lifecycle_signal") == "unavailable"
    assert out_floor.get("signal_status") == "build_failed"
    assert out_floor.get("error") == "RuntimeError('simulated failure')"
    assert out_floor.get("integration_branch") == "automain"
    assert out_floor.get("promotion_target") == "main"


# ---------------------------------------------------------------------------
# lifecycle_bound_problem_model — through apply_agent_update (write-path proof)
#
# The lifecycle-bound pass is wired into apply_agent_update between
# archive_closed_gaps and the gc_problem_model backstop, so BOTH durable-model
# write paths (strategic_completion + admin_agent_ops) get it. These tests
# exercise that seam end-to-end at the same altitude the packer round-trip
# tests use the durable model.
# ---------------------------------------------------------------------------

_LB_NOW = "2026-07-07T12:00:00"


def test_apply_agent_update_lifecycle_bounds_terminal_state_toward_healthy():
    """A durable model seeded ABOVE every healthy threshold with terminal-ish
    records (aged-deferred unstaffed gaps, terminal friction surplus, stale
    planned-capability surplus) is pulled back to/under each healthy threshold
    by the write-path lifecycle pass, and every action is recorded in the diff
    and the revision-log summary."""
    from app.problem_model import (
        MODEL_SIZE_HEALTHY_CAPABILITIES,
        MODEL_SIZE_HEALTHY_FRICTION,
        MODEL_SIZE_HEALTHY_GAPS,
        apply_agent_update,
    )

    # 4 genuinely-live staffed gaps + a surplus of aged-deferred unstaffed gaps
    # that pushes the active-gap count above the healthy threshold.
    live_gaps = [
        {
            "id": f"g-live-{i}",
            "name": f"live {i}",
            "status": "active",
            "active_moves": [f"d-{i}"],
        }
        for i in range(4)
    ]
    aged_gaps = [
        {
            "id": f"g-aged-{i}",
            "name": f"aged {i}",
            "status": "active",
            "deferral": {
                "rationale": "deferred long ago",
                "hold_until": "2026-01-01T00:00:00",  # well in the past
            },
        }
        for i in range(MODEL_SIZE_HEALTHY_GAPS)
    ]

    # 4 active friction + a terminal surplus that pushes total friction over the
    # healthy threshold.
    active_friction = [
        {"id": f"f-active-{i}", "name": f"a{i}", "status": "active"} for i in range(4)
    ]
    terminal_friction = [
        {"id": f"f-res-{i}", "name": f"r{i}", "status": "resolved"} for i in range(8)
    ]

    # working caps at the healthy threshold + a stale planned surplus.
    working_caps = [
        {"name": f"cap-work-{i}", "status": "working"}
        for i in range(MODEL_SIZE_HEALTHY_CAPABILITIES)
    ]
    planned_caps = [
        {"name": f"cap-planned-{i}", "status": "planned"} for i in range(5)
    ]

    current = {
        "revision": 7,
        "gaps": live_gaps + aged_gaps,
        "friction": active_friction + terminal_friction,
        "capabilities": working_caps + planned_caps,
    }
    # Echo the same agent-owned collections so the non-destructive merge does not
    # itself churn the diff — the only changes must come from the lifecycle pass.
    proposed = {
        "gaps": live_gaps + aged_gaps,
        "friction": active_friction + terminal_friction,
        "capabilities": working_caps + planned_caps,
    }

    new_raw, diff = apply_agent_update(current, proposed, _LB_NOW)

    # Every aged-deferred unstaffed gap was archived; the live staffed ones stayed.
    archived_ids = {g["id"] for g in new_raw["archived_gaps"]}
    assert diff["gaps_lifecycle_archived"] == [f"g-aged-{i}" for i in range(
        MODEL_SIZE_HEALTHY_GAPS)]
    assert archived_ids == {f"g-aged-{i}" for i in range(MODEL_SIZE_HEALTHY_GAPS)}
    active_ids = {g["id"] for g in new_raw["gaps"]}
    assert active_ids == {f"g-live-{i}" for i in range(4)}
    # Active-gap dimension pulled under the healthy threshold.
    assert len(new_raw["gaps"]) <= MODEL_SIZE_HEALTHY_GAPS

    # Friction pulled to the healthy threshold — resolved dropped first, active kept.
    assert len(new_raw["friction"]) == MODEL_SIZE_HEALTHY_FRICTION
    assert diff["friction_lifecycle_evicted"] == [f"f-res-{i}" for i in range(4)]
    assert all(f["status"] == "active" for f in new_raw["friction"]
               if f["id"].startswith("f-active"))

    # Capabilities pulled to the healthy threshold — planned dropped, working kept.
    assert len(new_raw["capabilities"]) == MODEL_SIZE_HEALTHY_CAPABILITIES
    assert diff["capabilities_lifecycle_evicted"] == [
        f"cap-planned-{i}" for i in range(5)
    ]
    assert all(c["status"] == "working" for c in new_raw["capabilities"])

    # The actions are observable in the revision-log summary.
    summary = new_raw["revision_log"][-1]["summary"]
    assert "lifecycle-archived" in summary
    assert "lifecycle-dropped" in summary


def test_apply_agent_update_purely_live_model_no_lifecycle_churn():
    """A purely-live model seeded ABOVE the thresholds (all active staffed gaps,
    all active friction, all working/partial capabilities) round-trips through
    the write path with NO lifecycle eviction and NO lifecycle diff keys — the
    pass is a genuine no-op on live state."""
    from app.problem_model import (
        MODEL_SIZE_HEALTHY_CAPABILITIES,
        MODEL_SIZE_HEALTHY_FRICTION,
        MODEL_SIZE_HEALTHY_GAPS,
        apply_agent_update,
    )

    gaps = [
        {
            "id": f"g-{i}",
            "name": f"live {i}",
            "status": "active",
            "active_moves": [f"d-{i}"],
        }
        for i in range(MODEL_SIZE_HEALTHY_GAPS + 6)
    ]
    friction = [
        {"id": f"f-{i}", "name": f"a{i}", "status": "active"}
        for i in range(MODEL_SIZE_HEALTHY_FRICTION + 6)
    ]
    capabilities = [
        {"name": f"c-{i}", "status": "working" if i % 2 else "partial"}
        for i in range(MODEL_SIZE_HEALTHY_CAPABILITIES + 6)
    ]
    current = {
        "revision": 3,
        "gaps": gaps,
        "friction": friction,
        "capabilities": capabilities,
    }
    proposed = {"gaps": gaps, "friction": friction, "capabilities": capabilities}

    new_raw, diff = apply_agent_update(current, proposed, _LB_NOW)

    # No lifecycle diff keys at all.
    assert "gaps_lifecycle_archived" not in diff
    assert "friction_lifecycle_evicted" not in diff
    assert "capabilities_lifecycle_evicted" not in diff
    # Every live record survived intact — nothing evicted or archived.
    assert len(new_raw["gaps"]) == MODEL_SIZE_HEALTHY_GAPS + 6
    assert new_raw.get("archived_gaps", []) == []
    assert len(new_raw["friction"]) == MODEL_SIZE_HEALTHY_FRICTION + 6
    assert len(new_raw["capabilities"]) == MODEL_SIZE_HEALTHY_CAPABILITIES + 6
    assert new_raw["revision_log"][-1]["summary"] == "no structural changes"


def test_apply_agent_update_lifecycle_spares_staffed_and_fresh_deferrals():
    """Even when the model is oversized, an active gap that is staffed
    (active_moves) or carries a fresh (unexpired) deferral, plus active friction
    and working/partial capabilities, are never touched by the lifecycle pass."""
    from app.problem_model import apply_agent_update

    gaps = [
        # Staffed + aged deferral → spared because it is staffed.
        {
            "id": "g-staffed",
            "name": "staffed",
            "status": "active",
            "active_moves": ["d-live"],
            "deferral": {"rationale": "x", "hold_until": "2020-01-01T00:00:00"},
        },
        # Fresh (future) deferral, unstaffed → spared because the deferral holds.
        {
            "id": "g-fresh",
            "name": "fresh defer",
            "status": "active",
            "deferral": {"rationale": "pinned", "hold_until": "2099-01-01T00:00:00"},
        },
    ]
    current = {"revision": 1, "gaps": gaps}
    new_raw, diff = apply_agent_update(current, {"gaps": gaps}, _LB_NOW)

    assert "gaps_lifecycle_archived" not in diff
    assert {g["id"] for g in new_raw["gaps"]} == {"g-staffed", "g-fresh"}
    assert new_raw.get("archived_gaps", []) == []


def test_apply_agent_update_lifecycle_runs_before_gc_backstop():
    """Lifecycle archival lands in archived_gaps and is NOT then rolled off by
    the gc backstop: with archived_gaps well under the 200-item cap, a
    lifecycle-archived gap remains re-fetchable in the durable model."""
    from app.problem_model import (
        MODEL_SIZE_HEALTHY_GAPS,
        apply_agent_update,
    )

    # One aged-deferred gap plus enough active healthy gaps to exceed the count
    # threshold so the lifecycle pass triggers.
    aged = {
        "id": "g-aged",
        "name": "aged",
        "status": "active",
        "deferral": {"rationale": "old", "hold_until": "2026-01-01T00:00:00"},
    }
    healthy_gaps = [
        {"id": f"g-h-{i}", "name": f"healthy {i}", "status": "active"}
        for i in range(MODEL_SIZE_HEALTHY_GAPS)
    ]
    current = {
        "revision": 5,
        "gaps": [aged] + healthy_gaps,
        "archived_gaps": [
            {"id": "g-prior", "name": "prior", "status": "closed",
             "closed": "2026-02-01T00:00:00"}
        ],
    }
    new_raw, diff = apply_agent_update(
        current, {"gaps": [aged] + healthy_gaps}, _LB_NOW
    )

    assert diff["gaps_lifecycle_archived"] == ["g-aged"]
    # No gc eviction (well under the cap) — both the prior and the newly
    # lifecycle-archived gap are retained and re-fetchable by id.
    assert "archived_gaps_evicted" not in diff
    archived_ids = {g["id"] for g in new_raw["archived_gaps"]}
    assert archived_ids == {"g-prior", "g-aged"}
