"""Tests for the structured problem-model schema.

These tests are pure schema validation — they don't touch the DB or
the orchestrator. Integration with claim_task / strategic cycles lives
in later tests.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.acceptance_criteria import criterion_coverage_to_evidence_status
from app.problem_model import (
    ARCHIVED_GAPS_DURABLE_MAX,
    sanitize_problem_model_raw,
    CAPABILITIES_DURABLE_MAX,
    DEFERRAL_STALE_GRACE_DAYS,
    FRICTION_DURABLE_MAX,
    PROBLEM_MODEL_BYTE_BUDGET,
    GAP_HISTORY_DURABLE_MAX,
    GAP_PAST_MOVES_DURABLE_MAX,
    GAP_APPEND_ONLY_GC_FLOOR,
    MODEL_SIZE_HEALTHY_BYTES,
    MODEL_SIZE_HEALTHY_CAPABILITIES,
    MODEL_SIZE_HEALTHY_FRICTION,
    MODEL_SIZE_HEALTHY_GAPS,
    REVISION_LOG_MAX,
    SELF_REFLECTION_LOG_MAX,
    Capability,
    Friction,
    Gap,
    GapDeferral,
    Intent,
    ProblemModel,
    ProjectContext,
    SelfReflectionEntry,
    Source,
    active_direction_gap_ids,
    append_revision_log,
    append_self_reflection,
    apply_agent_update,
    apply_user_intent_patch,
    archive_closed_gaps,
    auto_archive_stale_problem_model_items,
    lifecycle_bound_problem_model,
    check_gap_id_write,
    classify_gap_link,
    compute_model_size_signal,
    capability_evidence_status,
    gap_evidence_status,
    gc_problem_model,
    is_empty,
    normalize_active_moves_writeback,
    normalize_gap_active_moves,
    parse_problem_model,
    reconcile_gap_staffing,
    reconcile_gap_staffing_raw,
    reconcile_gap_staffing_raw_result,
    reconcile_gaps_for_retired_directions,
    ConfirmationResolutionSignals,
    PendingConfirmation,
    auto_clear_resolved_confirmations,
)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_empty_problem_model_parses():
    model = parse_problem_model(None)
    assert isinstance(model, ProblemModel)
    assert model.revision == 0
    assert model.intent.purpose == ""
    assert model.gaps == []


def test_empty_dict_parses():
    model = parse_problem_model({})
    assert isinstance(model, ProblemModel)
    assert is_empty(model)


def test_full_round_trip_preserves_structure():
    payload = {
        "revision": 4,
        "last_updated": "2026-04-21T10:00:00",
        "last_updated_by": "agent",
        "intent": {
            "purpose": "Autonomous dev studio.",
            "current_focus": "First-attempt merge rate to 70%.",
        },
        "context": {
            "project_type": "personal",
            "users": "Solo dev + eventual early adopters.",
            "team": "solo",
            "deploy_cadence": "continuous",
            "notes": "High risk tolerance.",
        },
        "constraints": ["Don't modify CI workflows."],
        "capabilities": [
            {"name": "Task dispatch", "status": "working", "notes": None},
            {"name": "Strategic cycles", "status": "partial"},
        ],
        "gaps": [
            {
                "id": "g-mem",
                "name": "No cross-cycle memory",
                "description": "Strategic agent starts fresh each cycle.",
                "evidence": "User feedback 2026-04-21.",
                "size": "large",
                "priority": "high",
                "status": "active",
                "opened": "2026-04-21",
                "active_moves": [],
                "past_moves": [],
                "history": [
                    {"ts": "2026-04-21", "event": "opened", "note": None},
                ],
            }
        ],
        "friction": [
            {
                "id": "f-rate",
                "name": "LLM rate limits",
                "seen_in": ["dir-1", "dir-2"],
                "status": "active",
            }
        ],
        "pending_user_confirmation": [
            {
                "id": "p-creds",
                "type": "credentials_needed",
                "proposal": "Need OpenAI key with higher rate limit.",
                "rationale": "Strategic cycles hitting 429s.",
                "created_at": "2026-04-21",
            }
        ],
    }
    model = parse_problem_model(payload)
    # Round-trip should produce equivalent dict (minus model-level defaults
    # that were filled in on parse).
    dumped = model.model_dump()
    assert dumped["intent"]["purpose"] == "Autonomous dev studio."
    assert dumped["context"]["project_type"] == "personal"
    assert len(dumped["gaps"]) == 1
    assert dumped["gaps"][0]["history"][0]["event"] == "opened"
    assert dumped["friction"][0]["seen_in"] == ["dir-1", "dir-2"]
    assert dumped["pending_user_confirmation"][0]["type"] == "credentials_needed"


def test_extra_fields_rejected():
    """Schema uses extra='forbid' so typos surface loudly instead of being
    silently dropped."""
    with pytest.raises(ValidationError):
        ProblemModel.model_validate({"goose": "honk"})


def test_gap_enum_validation():
    with pytest.raises(ValidationError):
        Gap(id="g-x", name="x", size="enormous")  # invalid size


def test_capability_enum_validation():
    with pytest.raises(ValidationError):
        Capability(name="x", status="maybe")  # invalid status


def test_capability_backing_paths_defaults_empty():
    """Old-shape capability (no backing_paths) parses with an empty list, so
    every stored model and older agent payload remains valid."""
    cap = Capability(name="auth", status="working")
    assert cap.backing_paths == []


def test_capability_backing_paths_round_trips():
    cap = Capability(
        name="auth",
        status="working",
        backing_paths=["backend/app/auth.py", "backend/app/models.py"],
    )
    dumped = cap.model_dump()
    assert dumped["backing_paths"] == [
        "backend/app/auth.py",
        "backend/app/models.py",
    ]
    restored = Capability.model_validate(dumped)
    assert restored.backing_paths == cap.backing_paths


def test_problem_model_round_trip_preserves_backing_paths():
    """Full ProblemModel round-trip keeps capability backing_paths intact."""
    model = ProblemModel(
        capabilities=[
            Capability(
                name="auth",
                status="working",
                backing_paths=["backend/app/auth.py"],
            ),
            Capability(name="legacy", status="partial"),  # old shape, no paths
        ]
    )
    raw = model.model_dump()
    restored = parse_problem_model(raw)
    assert restored.capabilities[0].backing_paths == ["backend/app/auth.py"]
    assert restored.capabilities[1].backing_paths == []


def test_old_shape_problem_model_with_capabilities_parses():
    """A stored problem_model whose capabilities predate backing_paths still
    validates cleanly (backward compatibility)."""
    raw = {
        "revision": 3,
        "capabilities": [
            {"name": "CapA", "status": "working", "notes": "stable"},
            {"name": "CapB", "status": "planned"},
        ],
    }
    model = parse_problem_model(raw)
    assert all(c.backing_paths == [] for c in model.capabilities)


def test_gap_evidence_refs_defaults_empty():
    """Old-shape gap (no evidence_refs) parses with an empty list, so every
    stored model and older agent payload remains valid."""
    gap = Gap(id="g-1", name="a gap")
    assert gap.evidence_refs == []
    assert gap.evidence == ""  # narrative field unchanged, still present


def test_gap_evidence_refs_round_trip():
    gap = Gap(
        id="g-1",
        name="a gap",
        evidence="narrative still here",
        evidence_refs=[
            {
                "kind": "named_test",
                "ref": "backend/testing/test_foo.py",
                "proven": True,
            },
            {"kind": "backing_path", "ref": "backend/app/foo.py", "proven": False},
        ],
    )
    dumped = gap.model_dump()
    assert dumped["evidence_refs"][0]["kind"] == "named_test"
    assert dumped["evidence_refs"][0]["proven"] is True
    assert dumped["evidence"] == "narrative still here"
    restored = Gap.model_validate(dumped)
    assert restored.evidence_refs == gap.evidence_refs


def test_gap_missing_evidence_refs_back_compat_validates():
    """A stored gap payload lacking evidence_refs validates (extra='forbid' on
    the parent, but the field's empty default keeps old payloads valid)."""
    raw = {
        "revision": 2,
        "gaps": [
            {"id": "g-1", "name": "legacy gap", "status": "active", "evidence": "x"},
        ],
    }
    model = parse_problem_model(raw)
    assert model.gaps[0].evidence_refs == []
    # Full round-trip preserves the empty default.
    restored = parse_problem_model(model.model_dump())
    assert restored.gaps[0].evidence_refs == []


def test_gap_evidence_status_helper():
    proven = Gap(
        id="g1",
        name="p",
        evidence_refs=[{"kind": "named_test", "ref": "a.py", "proven": True}],
    )
    partial = Gap(
        id="g2",
        name="q",
        evidence_refs=[{"kind": "named_test", "ref": "a.py", "proven": False}],
    )
    empty = Gap(id="g3", name="r")
    assert gap_evidence_status(proven) == "proven"
    assert gap_evidence_status(partial) == "partial"
    assert gap_evidence_status(empty) == "unproven"


def test_apply_agent_update_gap_evidence_refs_proposed_wins():
    """evidence_refs is a plain proposed-wins field through _merge_gap /
    apply_agent_update — the agent's list replaces the durable one."""
    current = {
        "revision": 1,
        "gaps": [
            {
                "id": "g-1",
                "name": "gap",
                "status": "active",
                "evidence_refs": [
                    {"kind": "named_test", "ref": "old.py", "proven": False}
                ],
            }
        ],
    }
    proposed = {
        "gaps": [
            {
                "id": "g-1",
                "name": "gap",
                "status": "active",
                "evidence_refs": [
                    {"kind": "backing_path", "ref": "new.py", "proven": True}
                ],
            }
        ],
    }
    new_raw, _diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    merged_gap = new_raw["gaps"][0]
    assert merged_gap["evidence_refs"] == [
        {"kind": "backing_path", "ref": "new.py", "proven": True}
    ]


def test_apply_agent_update_unaffected_by_backing_paths_only_change():
    """A capability that only adds/changes backing_paths (status unchanged)
    is not reported as a capability status change by _compute_diff."""
    current = {
        "revision": 1,
        "capabilities": [{"name": "CapA", "status": "working"}],
    }
    proposed = {
        "capabilities": [
            {
                "name": "CapA",
                "status": "working",  # unchanged
                "backing_paths": ["backend/app/foo.py"],  # newly linked
            }
        ],
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert "capabilities_changed" not in diff
    assert new_raw["capabilities"][0]["backing_paths"] == ["backend/app/foo.py"]


def test_is_empty_detects_meaningful_content():
    empty = ProblemModel()
    assert is_empty(empty)

    with_intent = ProblemModel(intent=Intent(purpose="Do a thing."))
    assert not is_empty(with_intent)

    with_gap = ProblemModel(gaps=[Gap(id="g-1", name="x")])
    assert not is_empty(with_gap)


# ---------------------------------------------------------------------------
# Gap archiving
# ---------------------------------------------------------------------------


def test_archive_closed_gaps_moves_closed_and_abandoned():
    model = ProblemModel(
        gaps=[
            Gap(id="g-active", name="still open", status="active"),
            Gap(id="g-closed", name="done", status="closed"),
            Gap(id="g-abandoned", name="gave up", status="abandoned"),
            Gap(id="g-blocked", name="waiting", status="blocked"),
        ]
    )
    archive_closed_gaps(model)
    active_ids = {g.id for g in model.gaps}
    archived_ids = {g.id for g in model.archived_gaps}
    assert active_ids == {"g-active", "g-blocked"}
    assert archived_ids == {"g-closed", "g-abandoned"}


def test_archive_is_idempotent_and_preserves_prior_archive():
    model = ProblemModel(
        gaps=[Gap(id="g-closed", name="done", status="closed")],
        archived_gaps=[Gap(id="g-old", name="prior archive", status="closed")],
    )
    archive_closed_gaps(model)
    assert {g.id for g in model.archived_gaps} == {"g-old", "g-closed"}
    # Running again is a no-op
    archive_closed_gaps(model)
    assert {g.id for g in model.archived_gaps} == {"g-old", "g-closed"}


def test_archive_closed_gaps_skips_already_archived_id():
    """A gap already in archived_gaps must not be duplicated when it also
    appears in gaps with status=closed -- the upstream trigger for the
    production duplicate bug (stale strategist payload re-submits a gap that
    was archived in a prior cycle)."""
    model = ProblemModel(
        gaps=[Gap(id="g-already-archived", name="done", status="closed")],
        archived_gaps=[Gap(id="g-already-archived", name="done", status="closed")],
    )
    archive_closed_gaps(model)
    # Still exactly one entry, not two.
    assert len(model.archived_gaps) == 1
    assert model.archived_gaps[0].id == "g-already-archived"
    # The closed gap is removed from the active gaps list regardless.
    assert model.gaps == []


def test_stale_echo_of_archived_gap_does_not_duplicate_or_resurrect():
    """A stale strategist echo must not repopulate ``gaps`` or duplicate
    ``archived_gaps`` when it re-sends a gap id that is already archived."""
    current = {
        "revision": 3,
        "archived_gaps": [
            {"id": "g-self-qualify-ci-wedge", "name": "ci wedge", "status": "closed"},
        ],
    }
    # Stale strategist payload re-submits the same gap in the active gaps list
    # and also echoes the historical archived entry.
    proposed = {
        "gaps": [
            {"id": "g-self-qualify-ci-wedge", "name": "ci wedge", "status": "active"},
        ],
        "archived_gaps": [
            {"id": "g-self-qualify-ci-wedge", "name": "ci wedge", "status": "closed"},
        ],
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-07-05T00:00:00")
    archived_ids = [g["id"] for g in new_raw["archived_gaps"]]
    assert archived_ids.count("g-self-qualify-ci-wedge") == 1, (
        f"duplicate archived_gaps entry: {archived_ids}"
    )
    assert len([g for g in new_raw["gaps"] if g["id"] == "g-self-qualify-ci-wedge"]) == 0


# ---------------------------------------------------------------------------
# Revision log
# ---------------------------------------------------------------------------


def test_append_revision_log_caps_to_max():
    model = ProblemModel(revision=1, last_updated="2026-04-21T00:00:00")
    for i in range(REVISION_LOG_MAX + 5):
        model.revision = i
        append_revision_log(model, summary=f"cycle {i}")
    assert len(model.revision_log) == REVISION_LOG_MAX
    # Oldest entries dropped; newest retained
    revisions = [entry.revision for entry in model.revision_log]
    assert revisions == list(
        range(REVISION_LOG_MAX + 5 - REVISION_LOG_MAX, REVISION_LOG_MAX + 5)
    )


def test_append_revision_log_records_changes():
    model = ProblemModel(revision=3, last_updated="2026-04-21T00:00:00")
    append_revision_log(
        model,
        summary="opened strategic-memory gap",
        changes={"gaps_opened": ["g-mem"], "friction_observed": ["f-rate"]},
    )
    assert len(model.revision_log) == 1
    entry = model.revision_log[0]
    assert entry.revision == 3
    assert entry.summary == "opened strategic-memory gap"
    assert entry.changes == {"gaps_opened": ["g-mem"], "friction_observed": ["f-rate"]}


# ---------------------------------------------------------------------------
# Friction promotion
# ---------------------------------------------------------------------------


def test_friction_tracks_promotion_to_gap():
    """When the agent decides friction is worth building a solution for,
    it promotes by opening a gap and pointing friction.promoted_to_gap_id
    at it — autonomous, no user gate."""
    friction = Friction(
        id="f-flaky-tests",
        name="Integration tests flake",
        seen_in=["d-1", "d-2", "d-3", "d-4"],
    )
    # Agent promotes autonomously
    gap = Gap(
        id="g-ci-reliability",
        name="CI reliability",
        description="Tests flake blocks the merge pipeline.",
    )
    friction.promoted_to_gap_id = gap.id
    friction.status = "mitigated"

    assert friction.promoted_to_gap_id == "g-ci-reliability"
    assert friction.status == "mitigated"


# ---------------------------------------------------------------------------
# Optional context fields
# ---------------------------------------------------------------------------


def test_context_fields_all_optional():
    ctx = ProjectContext()
    assert ctx.project_type is None
    assert ctx.users is None
    assert ctx.team is None
    assert ctx.deploy_cadence is None
    assert ctx.notes is None


def test_context_supports_free_form_project_type():
    """project_type is free-form string — not an enum — so users can
    describe unusual setups (``academic-spinoff``, ``open-source-contrib``)."""
    ctx = ProjectContext(project_type="open-source-contrib")
    assert ctx.project_type == "open-source-contrib"


# ---------------------------------------------------------------------------
# apply_agent_update — the strategic-completion write-back helper
# ---------------------------------------------------------------------------


def test_apply_agent_update_bumps_revision_and_timestamps():
    current = {
        "revision": 3,
        "intent": {"purpose": "Old purpose.", "current_focus": ""},
    }
    proposed = {
        "intent": {"purpose": "New purpose.", "current_focus": "New focus."},
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert new_raw["revision"] == 4
    assert new_raw["last_updated"] == "2026-04-21T12:00:00"
    assert new_raw["last_updated_by"] == "agent"
    assert new_raw["intent"]["purpose"] == "New purpose."
    assert diff.get("intent_changed") is True


def test_apply_agent_update_archives_closed_gaps():
    current = {
        "revision": 1,
        "gaps": [
            {"id": "g-open", "name": "still open", "status": "active"},
            {"id": "g-to-close", "name": "done next cycle", "status": "active"},
        ],
    }
    proposed = {
        "gaps": [
            {"id": "g-open", "name": "still open", "status": "active"},
            {"id": "g-to-close", "name": "done next cycle", "status": "closed"},
        ],
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    active_ids = {g["id"] for g in new_raw["gaps"]}
    archived_ids = {g["id"] for g in new_raw["archived_gaps"]}
    assert active_ids == {"g-open"}
    assert archived_ids == {"g-to-close"}
    assert diff["gaps_closed"] == ["g-to-close"]


def test_apply_agent_update_preserves_prior_archive_and_log():
    """Agent typically only sends the current working state; the helper
    preserves archived_gaps and revision_log from the DB."""
    current = {
        "revision": 2,
        "archived_gaps": [
            {"id": "g-ancient", "name": "old", "status": "closed"},
        ],
        "revision_log": [
            {
                "revision": 1,
                "ts": "2026-04-01T00:00:00",
                "summary": "prior",
                "changes": {},
            },
        ],
    }
    proposed: dict = {"gaps": []}
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    archived_ids = {g["id"] for g in new_raw["archived_gaps"]}
    assert "g-ancient" in archived_ids
    # Revision log preserved + new entry appended
    assert len(new_raw["revision_log"]) == 2
    assert new_raw["revision_log"][0]["summary"] == "prior"
    assert new_raw["revision_log"][1]["revision"] == 3


def test_apply_agent_update_dedupes_archived_gaps():
    """Agent that echoes its payload view re-appends already-archived gaps;
    deduplication by id prevents accumulating duplicates every cycle."""
    current = {
        "revision": 5,
        "archived_gaps": [
            {"id": "g-1", "name": "gap 1", "status": "closed"},
            {"id": "g-2", "name": "gap 2", "status": "closed"},
            {"id": "g-3", "name": "gap 3", "status": "closed"},
        ],
    }
    # Agent echoes back all three archived gaps in its payload view (common
    # when the agent reads/edits archived gaps via strategic context).
    proposed = {
        "archived_gaps": [
            {"id": "g-1", "name": "gap 1", "status": "closed"},
            {"id": "g-2", "name": "gap 2", "status": "closed"},
            {"id": "g-3", "name": "gap 3", "status": "closed"},
        ],
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    # Deduplication by id: the three archived gaps appear exactly once.
    archived_ids = [g["id"] for g in new_raw["archived_gaps"]]
    assert archived_ids == ["g-1", "g-2", "g-3"]
    assert len(new_raw["archived_gaps"]) == 3


def test_apply_agent_update_archived_gaps_new_entries_added():
    """When proposed includes a gap not in current, it is added to the archive."""
    current = {
        "revision": 5,
        "archived_gaps": [
            {"id": "g-1", "name": "gap 1", "status": "closed"},
        ],
    }
    # Agent echoes back the existing gap and adds a new one to the archive
    # (e.g., manually archived or archiving after closing).
    proposed = {
        "archived_gaps": [
            {"id": "g-1", "name": "gap 1", "status": "closed"},
            {"id": "g-4", "name": "gap 4", "status": "closed"},
        ],
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    archived_ids = {g["id"] for g in new_raw["archived_gaps"]}
    assert archived_ids == {"g-1", "g-4"}
    assert len(new_raw["archived_gaps"]) == 2


def test_gc_problem_model_keeps_most_recent_archived():
    """Direct unit test of eviction ordering: oldest archived gaps (by
    closed/opened timestamp) roll off, the most-recent ARCHIVED_GAPS_DURABLE_MAX
    are kept."""
    # Build cap+5 archived gaps with strictly increasing closed timestamps so
    # ordering is unambiguous.
    total = ARCHIVED_GAPS_DURABLE_MAX + 5
    archived = [
        Gap(
            id=f"g-{i:04d}",
            name=f"archived {i}",
            status="closed",
            closed=f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}",
        )
        for i in range(total)
    ]
    model = ProblemModel(archived_gaps=archived)

    summary = gc_problem_model(model)

    assert len(model.archived_gaps) == ARCHIVED_GAPS_DURABLE_MAX
    # The 5 oldest (lowest timestamps == lowest indices) were evicted.
    assert summary["archived_gaps_evicted"] == [f"g-{i:04d}" for i in range(5)]
    kept_ids = {g.id for g in model.archived_gaps}
    assert kept_ids == {f"g-{i:04d}" for i in range(5, total)}


def test_gc_problem_model_noop_under_cap():
    """Below the cap, gc_problem_model evicts nothing and returns an empty
    summary so callers can keep output byte-identical on the no-op path."""
    archived = [
        Gap(id=f"g-{i}", name=f"x{i}", status="closed", closed=f"2026-01-0{i}")
        for i in range(1, 4)
    ]
    model = ProblemModel(archived_gaps=archived)
    before = model.model_dump()

    summary = gc_problem_model(model)

    assert summary == {}
    assert model.model_dump() == before


def test_gc_problem_model_deduplicates_archived_gaps():
    """When archived_gaps holds duplicates (same id, persisted by append-without-
    dedup), gc_problem_model collapses them, keeping the newest by timestamp,
    and records the heal in the summary."""
    archived = [
        Gap(
            id="g-dup",
            name="old copy",
            status="closed",
            closed="2026-01-01T00:00:00",
        ),
        Gap(
            id="g-dup",
            name="newer copy",
            status="closed",
            closed="2026-01-02T00:00:00",
        ),
        Gap(
            id="g-dup",
            name="oldest copy",
            status="closed",
            closed="2025-12-31T00:00:00",
        ),
        Gap(id="g-unique", name="solo", status="closed", closed="2026-01-03T00:00:00"),
    ]
    model = ProblemModel(archived_gaps=archived)

    summary = gc_problem_model(model)

    # 2 duplicates collapsed, 1 healed
    assert summary["archived_gaps_deduplicated"] == 2
    # Kept only 2 unique ids
    assert len(model.archived_gaps) == 2
    kept_ids = {g.id for g in model.archived_gaps}
    assert kept_ids == {"g-dup", "g-unique"}
    # The kept duplicate is the newest one
    kept_dup = next(g for g in model.archived_gaps if g.id == "g-dup")
    assert kept_dup.closed == "2026-01-02T00:00:00"
    assert kept_dup.name == "newer copy"


# ---------------------------------------------------------------------------
# lifecycle_bound_problem_model — direct unit
# ---------------------------------------------------------------------------

_LB_NOW = "2026-07-07T12:00:00"


def _deferred_gap(gap_id: str, deferral: GapDeferral, **kw) -> Gap:
    return Gap(
        id=gap_id,
        name=f"gap {gap_id}",
        status="active",
        opened="2026-01-01T00:00:00",
        deferral=deferral,
        **kw,
    )


def test_lifecycle_archives_gap_with_aged_hold_until():
    """An active, unstaffed gap whose deferral hold_until is in the past is
    marked abandoned, stamped closed, gets a lifecycle history event, and moves
    into archived_gaps (reversible — still re-fetchable by id)."""
    gap = _deferred_gap(
        "g-aged",
        GapDeferral(rationale="later", hold_until="2026-01-01T00:00:00"),
    )
    model = ProblemModel(gaps=[gap])

    summary = lifecycle_bound_problem_model(model, _LB_NOW)

    assert summary == {"gaps_lifecycle_archived": ["g-aged"]}
    assert model.gaps == []
    archived = {g.id: g for g in model.archived_gaps}
    assert "g-aged" in archived
    moved = archived["g-aged"]
    assert moved.status == "abandoned"
    assert moved.closed == _LB_NOW
    assert moved.history[-1].event == "lifecycle_auto_archived"


def test_lifecycle_preserves_gap_with_future_hold_until():
    """A fresh/held deferral (hold_until in the future) is never touched."""
    gap = _deferred_gap(
        "g-held",
        GapDeferral(rationale="pinned", hold_until="2027-01-01T00:00:00"),
    )
    model = ProblemModel(gaps=[gap])
    before = model.model_dump()

    summary = lifecycle_bound_problem_model(model, _LB_NOW)

    assert summary == {}
    assert model.model_dump() == before


def test_lifecycle_created_at_grace_boundary():
    """When hold_until is absent/unparseable, created_at drives staleness against
    a generous grace window: within the window is preserved, past it is
    archived."""
    fresh = _deferred_gap(
        "g-fresh",
        GapDeferral(rationale="milestone", hold_until="v22", created_at=_LB_NOW),
    )
    aged = _deferred_gap(
        "g-lapsed",
        GapDeferral(
            rationale="milestone",
            hold_until="v22",  # unparseable → fall back to created_at
            created_at="2026-01-01T00:00:00",  # far past the grace window
        ),
    )
    model = ProblemModel(gaps=[fresh, aged])

    summary = lifecycle_bound_problem_model(model, _LB_NOW)

    assert summary == {"gaps_lifecycle_archived": ["g-lapsed"]}
    assert {g.id for g in model.gaps} == {"g-fresh"}
    assert {g.id for g in model.archived_gaps} == {"g-lapsed"}
    # Grace window is generous — a full month — so a recently-deferred gap is safe.
    assert DEFERRAL_STALE_GRACE_DAYS >= 14


def test_lifecycle_never_touches_staffed_or_undeferred_gaps():
    """A gap with active_moves, or one carrying no deferral at all, is never
    archived even when the deferral (if any) is aged."""
    staffed = _deferred_gap(
        "g-staffed",
        GapDeferral(rationale="x", hold_until="2020-01-01T00:00:00"),
        active_moves=["d-live"],
    )
    undeferred = Gap(
        id="g-plain", name="plain", status="active", opened="2026-01-01T00:00:00"
    )
    model = ProblemModel(gaps=[staffed, undeferred])
    before = model.model_dump()

    summary = lifecycle_bound_problem_model(model, _LB_NOW)

    assert summary == {}
    assert model.model_dump() == before


def test_lifecycle_ignores_deferral_without_usable_dates():
    """A deferral with an unparseable hold_until and no created_at yields no
    usable age signal — the gap is left alone (conservative)."""
    gap = _deferred_gap(
        "g-nodate",
        GapDeferral(rationale="someday", hold_until="when-ready"),
    )
    model = ProblemModel(gaps=[gap])
    before = model.model_dump()

    assert lifecycle_bound_problem_model(model, _LB_NOW) == {}
    assert model.model_dump() == before


def test_lifecycle_drops_terminal_friction_toward_healthy_floor():
    """Terminal (resolved/mitigated) friction is dropped oldest-first, resolved
    before mitigated, only as far as needed to reach the healthy threshold."""
    # 2 over the healthy floor, all terminal.
    friction = [
        Friction(id=f"f-res-{i}", name=f"r{i}", status="resolved")
        for i in range(MODEL_SIZE_HEALTHY_FRICTION)
    ] + [
        Friction(id="f-mit-0", name="m0", status="mitigated"),
        Friction(id="f-mit-1", name="m1", status="mitigated"),
    ]
    model = ProblemModel(friction=friction)

    summary = lifecycle_bound_problem_model(model, _LB_NOW)

    assert len(model.friction) == MODEL_SIZE_HEALTHY_FRICTION
    # Resolved are most disposable → the two oldest resolved go first.
    assert summary["friction_lifecycle_evicted"] == ["f-res-0", "f-res-1"]


def test_lifecycle_never_drops_active_friction():
    """An all-active friction list above the healthy floor is left untouched —
    live state is never dropped to satisfy a bound."""
    friction = [
        Friction(id=f"f-{i}", name=f"a{i}", status="active")
        for i in range(MODEL_SIZE_HEALTHY_FRICTION + 5)
    ]
    model = ProblemModel(friction=friction)
    before = model.model_dump()

    assert lifecycle_bound_problem_model(model, _LB_NOW) == {}
    assert model.model_dump() == before


def test_lifecycle_drops_planned_capabilities_toward_healthy_floor():
    """Surplus planned (never-built) capabilities are dropped oldest-first;
    working/partial are never dropped."""
    caps = [
        Capability(name=f"working-{i}", status="working")
        for i in range(MODEL_SIZE_HEALTHY_CAPABILITIES)
    ] + [
        Capability(name="planned-0", status="planned"),
        Capability(name="planned-1", status="planned"),
        Capability(name="partial-live", status="partial"),
    ]
    model = ProblemModel(capabilities=caps)

    summary = lifecycle_bound_problem_model(model, _LB_NOW)

    # 3 over the floor, but only 2 are planned (evictable); partial is kept.
    assert summary["capabilities_lifecycle_evicted"] == ["planned-0", "planned-1"]
    kept = {c.name for c in model.capabilities}
    assert "partial-live" in kept
    assert "planned-0" not in kept and "planned-1" not in kept


def test_lifecycle_all_live_capabilities_untouched():
    """An all-working/partial capability list above the floor is left untouched."""
    caps = [
        Capability(name=f"c-{i}", status="working" if i % 2 else "partial")
        for i in range(MODEL_SIZE_HEALTHY_CAPABILITIES + 6)
    ]
    model = ProblemModel(capabilities=caps)
    before = model.model_dump()

    assert lifecycle_bound_problem_model(model, _LB_NOW) == {}
    assert model.model_dump() == before


def test_lifecycle_empty_model_is_noop_byte_identical():
    """The no-op path returns {} and leaves the model byte-identical."""
    model = ProblemModel(
        gaps=[Gap(id="g", name="live", status="active")],
        friction=[Friction(id="f", name="a", status="active")],
        capabilities=[Capability(name="c", status="working")],
    )
    before = model.model_dump()

    assert lifecycle_bound_problem_model(model, _LB_NOW) == {}
    assert model.model_dump() == before


# auto_archive_stale_problem_model_items — enhanced lifecycle with live_direction_ids
# ---------------------------------------------------------------------------


def test_auto_archive_gap_under_threshold_never_touched():
    """A gap under the healthy threshold (≤12 active) is never archived even
    if it is inert — the function is count-gated and errs conservative."""
    # 3 active gaps (well under MODEL_SIZE_HEALTHY_GAPS=12), all deferred and aged.
    gaps = [
        _deferred_gap(
            f"g-stale-{i}",
            GapDeferral(rationale="later", hold_until="2020-01-01T00:00:00"),
            active_moves=[],
        )
        for i in range(3)
    ]
    model = ProblemModel(gaps=gaps)
    before = model.model_dump()

    summary = auto_archive_stale_problem_model_items(model, _LB_NOW)

    assert summary == {}
    assert model.model_dump() == before
    assert all(g.status == "active" for g in model.gaps)


def test_auto_archive_gap_over_threshold_actively_staffed_never_archived():
    """A gap over the healthy threshold (>12 active) that is still actively
    staffed by a live direction is NEVER archived — the live_direction_ids
    gate protects it."""
    # Create 13 active gaps (1 over the healthy threshold), all aged-deferred.
    # The first one is staffed by a live direction.
    live_staffed = _deferred_gap(
        "g-staffed",
        GapDeferral(
            rationale="active work",
            hold_until="2020-01-01T00:00:00",  # aged past
        ),
        active_moves=["d-live-1"],
    )
    inert_gaps = [
        _deferred_gap(
            f"g-inert-{i}",
            GapDeferral(rationale="later", hold_until="2020-01-01T00:00:00"),
            active_moves=[],
        )
        for i in range(MODEL_SIZE_HEALTHY_GAPS)
    ]
    model = ProblemModel(gaps=[live_staffed] + inert_gaps)
    live_ids = frozenset({"d-live-1"})

    summary = auto_archive_stale_problem_model_items(model, _LB_NOW, live_ids)

    # The staffed gap must survive.
    staffed_gap = next(g for g in model.gaps if g.id == "g-staffed")
    assert staffed_gap.status == "active"
    # The inert ones should be archived.
    assert "gaps_lifecycle_archived" in summary


def test_auto_archive_gap_over_threshold_inert_ttl_expired_archived():
    """A gap over the healthy threshold, inert (no live staffing), with an
    expired deferral TTL, gets correctly auto-archived."""
    # Create 13 active gaps (1 over the healthy threshold), one is aged-deferred
    # and unstaffed — that one should be archived.
    aged = _deferred_gap(
        "g-aged",
        GapDeferral(
            rationale="forgotten",
            hold_until="2020-01-01T00:00:00",  # well in the past
        ),
        active_moves=[],
    )
    healthy_active = [
        Gap(
            id=f"g-active-{i}",
            name=f"active {i}",
            status="active",
            opened="2026-06-01T00:00:00",
            active_moves=[],
        )
        for i in range(MODEL_SIZE_HEALTHY_GAPS)
    ]
    model = ProblemModel(gaps=[aged] + healthy_active)

    summary = auto_archive_stale_problem_model_items(model, _LB_NOW)

    assert summary == {"gaps_lifecycle_archived": ["g-aged"]}
    assert aged.id not in {g.id for g in model.gaps}
    assert aged.id in {g.id for g in model.archived_gaps}
    moved = next(g for g in model.archived_gaps if g.id == aged.id)
    assert moved.status == "abandoned"
    assert moved.closed == _LB_NOW
    assert moved.history[-1].event == "lifecycle_auto_archived"


def test_auto_archive_gap_with_live_direction_ids_stale_moves_can_be_archived():
    """A gap whose active_moves contains ONLY stale (non-live) direction IDs
    can be archived — the live_direction_ids cross-check proves inertness
    even when active_moves is non-empty."""
    gap = _deferred_gap(
        "g-stale-moves",
        GapDeferral(rationale="later", hold_until="2020-01-01T00:00:00"),
        active_moves=["d-retired-1", "d-expired-2"],
    )
    # 12 healthy active gaps to push past threshold
    healthy_gaps = [
        Gap(
            id=f"g-h-{i}",
            name=f"healthy {i}",
            status="active",
            opened="2026-06-01T00:00:00",
            active_moves=[],
        )
        for i in range(MODEL_SIZE_HEALTHY_GAPS)
    ]
    model = ProblemModel(gaps=[gap] + healthy_gaps)
    # Only "d-live-1" is live — the gap's moves are all stale, so it IS inert.
    live_ids = frozenset({"d-live-1", "d-live-2"})

    summary = auto_archive_stale_problem_model_items(model, _LB_NOW, live_ids)

    assert "gaps_lifecycle_archived" in summary
    assert "g-stale-moves" in summary["gaps_lifecycle_archived"]


def test_auto_archive_gap_undeferred_active_never_archived():
    """A gap with no deferral is never archived even when over threshold and
    unstaffed — the deferral guard is mandatory."""
    gaps = [
        Gap(
            id=f"g-no-def-{i}",
            name=f"no deferral {i}",
            status="active",
            opened="2026-01-01T00:00:00",
            active_moves=[],
        )
        for i in range(MODEL_SIZE_HEALTHY_GAPS + 2)
    ]
    model = ProblemModel(gaps=gaps)
    before = model.model_dump()

    summary = auto_archive_stale_problem_model_items(model, _LB_NOW)

    assert summary == {}
    assert model.model_dump() == before
    assert all(g.status == "active" for g in model.gaps)


def test_auto_archive_delegates_from_lifecycle_wrapper():
    """lifecycle_bound_problem_model delegates to
    auto_archive_stale_problem_model_items — same behavior with empty
    live_direction_ids."""
    gap = _deferred_gap(
        "g-aged",
        GapDeferral(rationale="later", hold_until="2020-01-01T00:00:00"),
    )
    healthy_gaps = [
        Gap(
            id=f"g-h-{i}",
            name=f"healthy {i}",
            status="active",
            opened="2026-06-01T00:00:00",
            active_moves=[],
        )
        for i in range(MODEL_SIZE_HEALTHY_GAPS)
    ]
    model1 = ProblemModel(gaps=[gap] + healthy_gaps)
    model2 = model1.model_copy(deep=True)

    s1 = lifecycle_bound_problem_model(model1, _LB_NOW)
    s2 = auto_archive_stale_problem_model_items(model2, _LB_NOW)

    assert s1 == s2
    assert model1.model_dump() == model2.model_dump()


def test_apply_agent_update_caps_archived_gaps():
    """A model accumulating archived gaps past the cap is structurally bounded
    on write-back, regardless of whether the agent pruned — the oldest roll
    off and active state is untouched."""
    current = {
        "revision": 1,
        "archived_gaps": [
            {
                "id": f"g-{i:04d}",
                "name": f"old {i}",
                "status": "closed",
                "closed": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}",
            }
            for i in range(ARCHIVED_GAPS_DURABLE_MAX + 10)
        ],
        "gaps": [{"id": "g-live", "name": "active gap", "status": "active"}],
    }
    proposed = {"gaps": [{"id": "g-live", "name": "active gap", "status": "active"}]}

    new_raw, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")

    assert len(new_raw["archived_gaps"]) == ARCHIVED_GAPS_DURABLE_MAX
    # Oldest 10 evicted; the active gap is never touched by GC.
    assert diff["archived_gaps_evicted"] == [f"g-{i:04d}" for i in range(10)]
    assert {g["id"] for g in new_raw["gaps"]} == {"g-live"}
    kept = {g["id"] for g in new_raw["archived_gaps"]}
    assert "g-0000" not in kept
    assert f"g-{ARCHIVED_GAPS_DURABLE_MAX + 9:04d}" in kept


def test_apply_agent_update_archived_gc_recorded_in_revision_log():
    """The GC eviction is observable: evicted ids land in the diff AND the
    revision-log entry recorded for the revision that performed the eviction."""
    current = {
        "revision": 5,
        "archived_gaps": [
            {
                "id": f"g-{i:04d}",
                "name": f"old {i}",
                "status": "closed",
                "closed": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}",
            }
            for i in range(ARCHIVED_GAPS_DURABLE_MAX + 3)
        ],
    }
    new_raw, diff = apply_agent_update(current, {"gaps": []}, "2026-04-21T12:00:00")

    assert diff["archived_gaps_evicted"] == [f"g-{i:04d}" for i in range(3)]
    last_entry = new_raw["revision_log"][-1]
    assert last_entry["changes"]["archived_gaps_evicted"] == [
        f"g-{i:04d}" for i in range(3)
    ]
    assert "GC evicted 3 archived gap(s)" in last_entry["summary"]


def test_apply_agent_update_under_cap_no_gc_mutation():
    """Below the cap, no archived gaps are evicted and the diff carries no GC
    key — the GC backstop is invisible when the model is small."""
    current = {
        "revision": 1,
        "archived_gaps": [
            {"id": "g-a", "name": "a", "status": "closed", "closed": "2026-01-01"},
            {"id": "g-b", "name": "b", "status": "closed", "closed": "2026-01-02"},
        ],
    }
    new_raw, diff = apply_agent_update(current, {"gaps": []}, "2026-04-21T12:00:00")

    assert {g["id"] for g in new_raw["archived_gaps"]} == {"g-a", "g-b"}
    assert "archived_gaps_evicted" not in diff


def test_apply_agent_update_gc_never_drops_active_state():
    """GC bounds history/archive only — active gaps, capabilities, and pending
    confirmations survive even when the archive is over the cap and evicting."""
    current = {
        "revision": 1,
        "archived_gaps": [
            {
                "id": f"g-arch-{i:04d}",
                "name": f"old {i}",
                "status": "closed",
                "closed": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}",
            }
            for i in range(ARCHIVED_GAPS_DURABLE_MAX + 25)
        ],
        "gaps": [{"id": "g-live", "name": "live", "status": "active"}],
        "capabilities": [{"name": "cap-x", "status": "working"}],
        "pending_user_confirmation": [
            {"id": "p-1", "type": "access_needed", "proposal": "grant access"}
        ],
    }
    proposed = {
        "gaps": [{"id": "g-live", "name": "live", "status": "active"}],
        "capabilities": [{"name": "cap-x", "status": "working"}],
        "pending_user_confirmation": [
            {"id": "p-1", "type": "access_needed", "proposal": "grant access"}
        ],
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")

    assert len(new_raw["archived_gaps"]) == ARCHIVED_GAPS_DURABLE_MAX
    assert {g["id"] for g in new_raw["gaps"]} == {"g-live"}
    assert {c["name"] for c in new_raw["capabilities"]} == {"cap-x"}
    assert {p["id"] for p in new_raw["pending_user_confirmation"]} == {"p-1"}


# ---------------------------------------------------------------------------
# Byte-aware GC — bound the serialized model under the write cap even when
# individual entries are large (count caps alone don't catch this).
# ---------------------------------------------------------------------------


def _serialized_bytes(model: ProblemModel) -> int:
    import json

    return len(json.dumps(model.model_dump(), default=str))


def test_gc_problem_model_byte_aware_evicts_large_archived_gaps():
    """A model UNDER the archived-gap count cap but whose large entries push the
    serialized size past the byte budget is GC'd down to fit, evicting the
    oldest archived gaps first."""
    big = "x" * 5_000
    # ~20 archived gaps * ~5KB each ≈ 100KB > 90KB budget, but well under the
    # 200-entry count cap so only the byte pass can catch it.
    archived = [
        Gap(
            id=f"g-{i:04d}",
            name=f"archived {i}",
            status="closed",
            closed=f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}",
            description=big,
        )
        for i in range(20)
    ]
    model = ProblemModel(archived_gaps=archived)
    assert len(model.archived_gaps) < ARCHIVED_GAPS_DURABLE_MAX  # count cap inert
    assert _serialized_bytes(model) > PROBLEM_MODEL_BYTE_BUDGET

    summary = gc_problem_model(model)

    assert _serialized_bytes(model) <= PROBLEM_MODEL_BYTE_BUDGET
    # Oldest evicted first; the most-recent gaps survive.
    evicted = summary["archived_gaps_evicted"]
    assert evicted == [f"g-{i:04d}" for i in range(len(evicted))]
    kept_ids = {g.id for g in model.archived_gaps}
    assert f"g-{19:04d}" in kept_ids
    assert "g-0000" not in kept_ids


def test_gc_problem_model_byte_aware_trims_ledger_when_archive_exhausted():
    """When archived gaps are exhausted (or absent) and the model is still over
    budget, the byte pass trims the oldest rejected-agent-edits ledger entries —
    pure reference-only bookkeeping — until it fits."""
    big = "x" * 6_000
    ledger = [
        {
            "at": f"2026-01-0{i}",
            "field": "context",
            "kind": "value_conflict",
            "operator_value": big,
            "agent_value": big,
        }
        for i in range(10)
    ]
    model = ProblemModel(rejected_agent_edits=ledger)
    assert _serialized_bytes(model) > PROBLEM_MODEL_BYTE_BUDGET

    summary = gc_problem_model(model)

    assert _serialized_bytes(model) <= PROBLEM_MODEL_BYTE_BUDGET
    assert summary["rejected_agent_edits_trimmed"] > 0
    # Oldest entries dropped; the newest survive (trim is from the front).
    assert model.rejected_agent_edits[-1]["at"] == "2026-01-09"
    assert "archived_gaps_evicted" not in summary


def test_gc_problem_model_byte_aware_noop_under_budget():
    """A model already under the byte budget is left byte-identical — the byte
    pass never re-orders archived gaps or trims the ledger on the no-op path."""
    model = ProblemModel(
        archived_gaps=[
            Gap(id="g-b", name="b", status="closed", closed="2026-01-02"),
            Gap(id="g-a", name="a", status="closed", closed="2026-01-01"),
        ],
        rejected_agent_edits=[{"at": "t", "field": "context"}],
    )
    before = model.model_dump()
    assert _serialized_bytes(model) <= PROBLEM_MODEL_BYTE_BUDGET

    summary = gc_problem_model(model)

    assert summary == {}
    # Archived-gap order is preserved (no spurious recency re-sort).
    assert model.model_dump() == before


def test_apply_agent_update_byte_gc_keeps_model_roundtrippable():
    """On write-back, a durable model stuffed with large archived gaps AND a
    large rejected-edits ledger is GC'd under the byte budget — and the eviction
    is observable in the diff + revision log — while live state is untouched."""
    big = "y" * 5_000
    history = [f"e{i}-{('x' * 1000)}" for i in range(60)]
    past_moves = [f"d-{i}-{('z' * 1000)}" for i in range(60)]
    current = {
        "revision": 3,
        "archived_gaps": [
            {
                "id": f"g-arch-{i:04d}",
                "name": f"old {i}",
                "status": "closed",
                "closed": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}",
                "description": big,
            }
            for i in range(20)
        ],
        "rejected_agent_edits": [
            {
                "at": f"2026-02-0{i}",
                "field": "constraints",
                "kind": "value_conflict",
                "operator_value": big,
                "agent_value": big,
            }
            for i in range(5)
        ],
        "gaps": [_gap_with_memory(history, past_moves)],
        "capabilities": [{"name": "cap-x", "status": "working"}],
        "pending_user_confirmation": [
            {"id": "p-1", "type": "access_needed", "proposal": "grant access"}
        ],
    }
    proposed = {"gaps": [_gap_with_memory(history[-5:], past_moves[-5:])]}

    new_raw, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")

    persisted = ProblemModel.model_validate(new_raw)
    assert _serialized_bytes(persisted) <= PROBLEM_MODEL_BYTE_BUDGET
    assert list(diff) == [
        "archived_gaps_evicted",
        "rejected_agent_edits_trimmed",
        "active_gap_tails_trimmed",
    ]
    gap = new_raw["gaps"][0]
    assert gap["id"] == "g-mem"
    assert len(gap["history"]) == GAP_APPEND_ONLY_GC_FLOOR
    assert gap["history"][0]["event"] == history[-GAP_APPEND_ONLY_GC_FLOOR]
    assert len(gap["past_moves"]) == GAP_APPEND_ONLY_GC_FLOOR
    assert gap["past_moves"][0] == past_moves[-GAP_APPEND_ONLY_GC_FLOOR]

    round_tripped, _ = apply_agent_update(
        new_raw,
        {"gaps": [_gap_with_memory(history[-5:], past_moves[-5:])]},
        "2026-04-21T12:05:00",
    )
    assert round_tripped["gaps"][0]["history"] == gap["history"]
    assert round_tripped["gaps"][0]["past_moves"] == gap["past_moves"]

    last_entry = new_raw["revision_log"][-1]
    assert "active_gap_tails_trimmed" in last_entry["changes"]
    assert "GC evicted" in last_entry["summary"]
    # Live, semantically meaningful state survives untouched.
    assert {c["name"] for c in new_raw["capabilities"]} == {"cap-x"}
    assert {p["id"] for p in new_raw["pending_user_confirmation"]} == {"p-1"}


def test_apply_agent_update_under_budget_no_byte_gc():
    """A normally-sized model write-back is not touched by the byte pass — no
    archived_gaps_evicted / rejected_agent_edits_trimmed keys appear."""
    current = {
        "revision": 1,
        "archived_gaps": [
            {"id": "g-a", "name": "a", "status": "closed", "closed": "2026-01-01"},
        ],
        "gaps": [{"id": "g-live", "name": "live", "status": "active"}],
    }
    proposed = {"gaps": [{"id": "g-live", "name": "live", "status": "active"}]}

    new_raw, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")

    assert "archived_gaps_evicted" not in diff
    assert "rejected_agent_edits_trimmed" not in diff
    assert {g["id"] for g in new_raw["archived_gaps"]} == {"g-a"}


# ---------------------------------------------------------------------------
# Byte-aware GC — bound capabilities/friction (non-destructively merged, so
# unbounded on mature projects) without ever dropping live high-value state.
# ---------------------------------------------------------------------------


def test_gc_problem_model_byte_aware_evicts_excess_capabilities():
    """A capabilities-heavy model over the byte budget is GC'd to fit by evicting
    terminal (``planned``) capabilities; live working/partial capabilities within
    budget survive untouched."""
    big = "x" * 4_000
    planned = [
        Capability(name=f"planned-{i}", status="planned", notes=big)
        for i in range(30)
    ]
    live = [
        Capability(name="cap-working", status="working"),
        Capability(name="cap-partial", status="partial"),
    ]
    model = ProblemModel(capabilities=planned + live)
    assert _serialized_bytes(model) > PROBLEM_MODEL_BYTE_BUDGET

    summary = gc_problem_model(model)

    assert _serialized_bytes(model) <= PROBLEM_MODEL_BYTE_BUDGET
    assert summary["capabilities_evicted"]  # at least one planned cap evicted
    surviving = {c.name for c in model.capabilities}
    # Live high-value capabilities are never evicted.
    assert {"cap-working", "cap-partial"} <= surviving
    # Everything evicted was a terminal planned capability.
    assert all(name.startswith("planned-") for name in summary["capabilities_evicted"])


def test_gc_problem_model_byte_aware_evicts_excess_friction_resolved_first():
    """When friction must be evicted to fit, resolved/mitigated (terminal) entries
    go first and active friction is preserved."""
    big = "y" * 4_000
    resolved = [
        Friction(id=f"f-res-{i}", name=f"res {i}", status="resolved", description=big)
        for i in range(15)
    ]
    mitigated = [
        Friction(id=f"f-mit-{i}", name=f"mit {i}", status="mitigated", description=big)
        for i in range(15)
    ]
    active = [Friction(id="f-active", name="live blocker", status="active")]
    model = ProblemModel(friction=resolved + mitigated + active)
    assert _serialized_bytes(model) > PROBLEM_MODEL_BYTE_BUDGET

    summary = gc_problem_model(model)

    assert _serialized_bytes(model) <= PROBLEM_MODEL_BYTE_BUDGET
    surviving = {f.id for f in model.friction}
    # Active friction always survives.
    assert "f-active" in surviving
    # Resolved friction is evicted before any mitigated entry: if any mitigated
    # was evicted, every resolved must already be gone.
    evicted = set(summary["friction_evicted"])
    if any(fid.startswith("f-mit-") for fid in evicted):
        assert all(f"f-res-{i}" in evicted for i in range(15))
    assert all(fid != "f-active" for fid in evicted)


def test_gc_problem_model_byte_aware_runs_when_capfriction_are_bulk():
    """Regression: an oversized model whose BULK is terminal capabilities/friction
    is GC'd to fit — the byte pass no longer skips because the live floor (after
    clearing terminal cap/friction) now fits under budget."""
    big = "z" * 3_000
    model = ProblemModel(
        gaps=[Gap(id="g-live", name="live", status="active")],
        capabilities=[
            Capability(name=f"planned-{i}", status="planned", notes=big)
            for i in range(20)
        ],
        friction=[
            Friction(id=f"f-{i}", name=f"res {i}", status="resolved", description=big)
            for i in range(20)
        ],
    )
    assert _serialized_bytes(model) > PROBLEM_MODEL_BYTE_BUDGET

    summary = gc_problem_model(model)

    assert _serialized_bytes(model) <= PROBLEM_MODEL_BYTE_BUDGET
    # The byte pass actually fired (rather than being futilely gated off).
    assert summary.get("capabilities_evicted") or summary.get("friction_evicted")
    # The live gap is untouched.
    assert {g.id for g in model.gaps} == {"g-live"}


def test_gc_problem_model_caps_capabilities_and_friction_count():
    """Count caps evict terminal entries down to the durable max even when the
    model is under the byte budget; live entries are never dropped to satisfy a
    count, so an all-live collection may remain above the cap."""
    # Small entries so the byte budget is not the trigger — exercise the count cap.
    planned = [
        Capability(name=f"planned-{i}", status="planned")
        for i in range(CAPABILITIES_DURABLE_MAX + 5)
    ]
    working = [Capability(name="cap-working", status="working")]
    resolved = [
        Friction(id=f"f-{i}", name=f"res {i}", status="resolved")
        for i in range(FRICTION_DURABLE_MAX + 7)
    ]
    active = [Friction(id="f-active", name="live", status="active")]
    model = ProblemModel(
        capabilities=planned + working, friction=resolved + active
    )
    assert _serialized_bytes(model) <= PROBLEM_MODEL_BYTE_BUDGET  # count, not byte

    summary = gc_problem_model(model)

    # Capability count brought down to the cap; the working cap survives.
    assert len(model.capabilities) == CAPABILITIES_DURABLE_MAX
    assert "cap-working" in {c.name for c in model.capabilities}
    assert len(summary["capabilities_evicted"]) == 6  # 5 overage + working kept
    # Friction count brought down to the cap; active friction survives.
    assert len(model.friction) == FRICTION_DURABLE_MAX
    assert "f-active" in {f.id for f in model.friction}
    assert len(summary["friction_evicted"]) == 8


def test_gc_problem_model_count_cap_never_evicts_all_live():
    """An all-live collection above the durable max is left intact — the count
    cap only evicts terminal entries, never live ones, to honor the invariant."""
    working = [
        Capability(name=f"cap-{i}", status="working")
        for i in range(CAPABILITIES_DURABLE_MAX + 10)
    ]
    active = [
        Friction(id=f"f-{i}", name=f"live {i}", status="active")
        for i in range(FRICTION_DURABLE_MAX + 10)
    ]
    model = ProblemModel(capabilities=working, friction=active)

    summary = gc_problem_model(model)

    # No live entry evicted even though both collections exceed their caps.
    assert summary == {}
    assert len(model.capabilities) == CAPABILITIES_DURABLE_MAX + 10
    assert len(model.friction) == FRICTION_DURABLE_MAX + 10


def test_apply_agent_update_byte_gc_bounds_capfriction_roundtrippable():
    """End-to-end: an oversized capability/friction-heavy model write-back lands
    under the durable byte budget, records the eviction in the diff + revision
    log, and round-trips; live gaps/pending state is untouched."""
    big = "w" * 3_500
    current = {
        "revision": 4,
        "capabilities": (
            [
                {"name": f"planned-{i}", "status": "planned", "notes": big}
                for i in range(20)
            ]
            + [{"name": "cap-working", "status": "working"}]
        ),
        "friction": (
            [
                {
                    "id": f"f-res-{i}",
                    "name": f"res {i}",
                    "status": "resolved",
                    "description": big,
                }
                for i in range(20)
            ]
            + [{"id": "f-active", "name": "live blocker", "status": "active"}]
        ),
        "gaps": [{"id": "g-live", "name": "live", "status": "active"}],
        "pending_user_confirmation": [
            {"id": "p-1", "type": "access_needed", "proposal": "grant access"}
        ],
    }
    proposed = {"gaps": [{"id": "g-live", "name": "live", "status": "active"}]}

    new_raw, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")

    persisted = ProblemModel.model_validate(new_raw)
    assert _serialized_bytes(persisted) <= PROBLEM_MODEL_BYTE_BUDGET
    assert diff.get("capabilities_evicted") or diff.get("friction_evicted")
    # Live high-value state survives.
    assert "cap-working" in {c["name"] for c in new_raw["capabilities"]}
    assert "f-active" in {f["id"] for f in new_raw["friction"]}
    assert {g["id"] for g in new_raw["gaps"]} == {"g-live"}
    assert {p["id"] for p in new_raw["pending_user_confirmation"]} == {"p-1"}
    # Observable in the revision log.
    last_entry = new_raw["revision_log"][-1]
    assert "GC evicted" in last_entry["summary"]

    # Round-trips cleanly back through another write-back.
    round_tripped, _ = apply_agent_update(
        new_raw, proposed, "2026-04-21T12:05:00"
    )
    assert _serialized_bytes(ProblemModel.model_validate(round_tripped)) <= (
        PROBLEM_MODEL_BYTE_BUDGET
    )


def test_apply_agent_update_detects_new_gaps():
    current = {
        "revision": 1,
        "gaps": [{"id": "g-existing", "name": "x", "status": "active"}],
    }
    proposed = {
        "gaps": [
            {"id": "g-existing", "name": "x", "status": "active"},
            {"id": "g-new", "name": "new thing", "status": "active"},
        ],
    }
    _, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert diff["gaps_opened"] == ["g-new"]
    assert "gaps_closed" not in diff


def test_apply_agent_update_detects_friction_changes():
    current = {
        "revision": 1,
        "friction": [{"id": "f-old", "name": "old", "status": "active"}],
    }
    proposed = {
        "friction": [
            {"id": "f-old", "name": "old", "status": "resolved"},
            {"id": "f-new", "name": "just observed", "status": "active"},
        ],
    }
    _, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert diff["friction_observed"] == ["f-new"]
    assert diff["friction_resolved"] == ["f-old"]


def test_apply_agent_update_detects_capability_status_changes():
    current = {
        "revision": 1,
        "capabilities": [
            {"name": "CapA", "status": "partial"},
            {"name": "CapB", "status": "working"},
        ],
    }
    proposed = {
        "capabilities": [
            {"name": "CapA", "status": "working"},  # upgraded
            {"name": "CapB", "status": "working"},  # unchanged
            {"name": "CapC", "status": "planned"},  # new
        ],
    }
    _, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert diff["capabilities_changed"] == ["CapA", "CapC"]


def test_apply_agent_update_rejects_malformed():
    """Validation errors raise ValidationError — callers catch and skip."""
    current: dict = {}
    proposed = {"gaps": [{"id": "g-bad", "name": "x", "size": "galactic"}]}
    with pytest.raises(ValidationError):
        apply_agent_update(current, proposed, "2026-04-21T12:00:00")


def test_apply_agent_update_handles_empty_current():
    """First-cycle scenario: current is None or {}."""
    new_raw, diff = apply_agent_update(
        None,
        {"intent": {"purpose": "Bootstrap the project."}},
        "2026-04-21T12:00:00",
    )
    assert new_raw["revision"] == 1
    assert new_raw["intent"]["purpose"] == "Bootstrap the project."
    assert diff.get("intent_changed") is True


def test_apply_agent_update_no_changes_still_logs():
    """Even an identical proposed model produces a revision entry so cycle
    cadence is observable in the log."""
    current = {
        "revision": 5,
        "intent": {"purpose": "Stable.", "current_focus": "Stable focus."},
    }
    proposed = {
        "intent": {"purpose": "Stable.", "current_focus": "Stable focus."},
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert new_raw["revision"] == 6
    assert diff == {}
    assert len(new_raw["revision_log"]) == 1
    assert new_raw["revision_log"][0]["summary"] == "no structural changes"


# ---------------------------------------------------------------------------
# apply_agent_update — mature model is never wiped (non-destructive merge)
#
# The former ColdStartWipeError guard hard-rejected a bootstrap-over-mature
# write. With agent-owned collections now merging non-destructively (main
# #7449), a trimmed/dropped payload can no longer wipe by omission — the prior
# entries are simply preserved — so these cases now assert preservation via
# merge rather than a raised error.
# ---------------------------------------------------------------------------


def _mature_current() -> dict:
    """A non-empty durable model with content in all three agent-owned
    collections — the kind a blind cold-start bootstrap must NOT erase."""
    return {
        "revision": 7,
        "capabilities": [
            {"name": "Billing", "status": "working"},
            {"name": "Metering", "status": "partial"},
        ],
        "gaps": [{"id": "g-1", "name": "invoicing", "status": "active"}],
        "friction": [{"id": "f-1", "name": "flaky CI", "status": "active"}],
    }


def test_apply_agent_update_genuine_empty_current_first_cycle_passes():
    """A genuine first cycle (current is empty) bootstrapping an empty model
    must succeed — nothing to preserve, so the collections stay empty."""
    new_raw, _ = apply_agent_update(
        None,
        {"intent": {"purpose": "Bootstrap the project."}},
        "2026-04-21T12:00:00",
    )
    assert new_raw["revision"] == 1
    assert new_raw["capabilities"] == []
    assert new_raw["gaps"] == []
    assert new_raw["friction"] == []


def test_apply_agent_update_bootstrap_over_mature_preserves_collections():
    """A cold-start proposed model (only intent, no agent-owned collections)
    over a mature model must NOT wipe it: merge preserves every existing
    capability, gap, and friction entry the agent never echoed back."""
    current = _mature_current()
    proposed = {"intent": {"purpose": "Fresh start.", "current_focus": "begin"}}
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert {c["name"] for c in new_raw["capabilities"]} == {"Billing", "Metering"}
    assert {g["id"] for g in new_raw["gaps"]} == {"g-1"}
    assert {f["id"] for f in new_raw["friction"]} == {"f-1"}


def test_apply_agent_update_explicit_empty_collections_preserve_via_merge():
    """Explicitly submitting empty arrays for all three collections over a
    mature model still preserves them — an empty list is no upserts, not a
    wipe. Removal is only via the explicit ``_deletions`` directive."""
    current = _mature_current()
    proposed = {"capabilities": [], "gaps": [], "friction": []}
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert {c["name"] for c in new_raw["capabilities"]} == {"Billing", "Metering"}
    assert {g["id"] for g in new_raw["gaps"]} == {"g-1"}
    assert {f["id"] for f in new_raw["friction"]} == {"f-1"}


def test_apply_agent_update_close_by_status_edit_passes():
    """Retiring a gap by marking it closed keeps the gap in the submission, so
    not all collections are empty — a normal edit that must still apply."""
    current = _mature_current()
    proposed = {
        "capabilities": [{"name": "Billing", "status": "working"}],
        # The retired gap is still present (status=closed) → gaps non-empty.
        "gaps": [{"id": "g-1", "name": "invoicing", "status": "closed"}],
        "friction": [{"id": "f-1", "name": "flaky CI", "status": "active"}],
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert new_raw["revision"] == 8
    # Closed gap was archived, not wiped.
    assert {g["id"] for g in new_raw["archived_gaps"]} == {"g-1"}
    assert diff["gaps_closed"] == ["g-1"]


def test_apply_agent_update_partial_proposed_merges_not_replaces():
    """A partial proposal touching only one collection merges into the durable
    model: the named capability upserts and the unmentioned capability, gaps,
    and friction are preserved (omission never deletes)."""
    current = _mature_current()
    proposed = {"capabilities": [{"name": "Billing", "status": "working"}]}
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert {c["name"] for c in new_raw["capabilities"]} == {"Billing", "Metering"}
    assert {g["id"] for g in new_raw["gaps"]} == {"g-1"}
    assert {f["id"] for f in new_raw["friction"]} == {"f-1"}


def test_apply_agent_update_archived_only_current_not_treated_as_mature():
    """A model holding only archived_gaps (all gaps already closed) is not a
    wipe target — archived history is carried forward regardless."""
    current = {
        "revision": 4,
        "archived_gaps": [{"id": "g-old", "name": "done", "status": "closed"}],
    }
    proposed: dict = {"intent": {"purpose": "Next phase."}}
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert new_raw["revision"] == 5
    assert {g["id"] for g in new_raw["archived_gaps"]} == {"g-old"}


# ---------------------------------------------------------------------------
# apply_agent_update — floor-skeleton field preservation
#
# Under byte pressure the read-time floor delivers a SKELETON of an entry (gap
# description/evidence stripped; capabilities/friction reduced to name+status).
# Editing and resubmitting such an entry must NOT blank the durable fields the
# agent never received. Preservation keys off the floor's own
# ``_agent_view_floor_applied`` marker (echoed back) so a genuine clear of a
# field the agent actually saw still applies.
# ---------------------------------------------------------------------------


def test_apply_agent_update_floored_gap_skeleton_preserves_detail():
    """Bumping a floored gap's priority and resubmitting it with blank
    description/evidence preserves the durable detail the agent never saw."""
    current = {
        "revision": 3,
        "gaps": [
            {
                "id": "g-1",
                "name": "invoicing",
                "status": "active",
                "priority": "medium",
                "description": "Durable description the agent never received.",
                "evidence": "PR #42 establishes the gap.",
                "active_moves": ["t-staffed-99"],
            }
        ],
    }
    proposed = {
        "gaps": [
            {
                "id": "g-1",
                "name": "invoicing",
                "status": "active",
                "priority": "high",
                "description": "",
                "evidence": "",
                "active_moves": [],
            }
        ],
        "_agent_view_floor_applied": {
            "floor_bytes": 32768,
            "stripped_gap_fields": [
                "history",
                "past_moves",
                "active_moves",
                "evidence",
                "description",
            ],
            "dropped_gaps": 0,
        },
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    gap = new_raw["gaps"][0]
    assert gap["priority"] == "high"  # the real edit still applies
    assert gap["description"] == "Durable description the agent never received."
    assert gap["evidence"] == "PR #42 establishes the gap."
    # Durable staffing state (read by reconciliation) survives the floored
    # resubmit instead of being silently orphaned.
    assert gap["active_moves"] == ["t-staffed-99"]


def test_apply_agent_update_blank_gap_field_clears_without_floor_marker():
    """No floor marker means the agent saw the full entry, so a blank field is a
    deliberate clear and must apply — preservation is not blind."""
    current = {
        "revision": 3,
        "gaps": [
            {"id": "g-1", "name": "x", "status": "active", "description": "old"}
        ],
    }
    proposed = {
        "gaps": [{"id": "g-1", "name": "x", "status": "active", "description": ""}]
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert new_raw["gaps"][0]["description"] == ""


def test_apply_agent_update_floor_marker_only_preserves_stripped_gap_fields():
    """A field NOT in ``stripped_gap_fields`` was seen by the agent, so blanking
    it is a genuine clear even when the marker is present; only the stripped
    field is preserved."""
    current = {
        "revision": 2,
        "gaps": [
            {
                "id": "g-1",
                "name": "x",
                "status": "active",
                "description": "seen",
                "evidence": "durable",
            }
        ],
    }
    proposed = {
        "gaps": [
            {
                "id": "g-1",
                "name": "x",
                "status": "active",
                "description": "",
                "evidence": "",
            }
        ],
        "_agent_view_floor_applied": {
            "stripped_gap_fields": ["history", "past_moves", "evidence"],
            "dropped_gaps": 0,
        },
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    gap = new_raw["gaps"][0]
    assert gap["evidence"] == "durable"  # stripped → preserved
    assert gap["description"] == ""  # seen by agent → genuine clear applies


def test_apply_agent_update_floored_capability_skeleton_preserves_detail():
    """Bumping a skeletonized capability's status preserves its durable notes,
    backing_paths, and source provenance."""
    current = {
        "revision": 5,
        "capabilities": [
            {
                "name": "Billing",
                "status": "partial",
                "notes": "durable notes",
                "backing_paths": ["a.py", "b.py"],
                "source": {"type": "shipped_change", "pr_number": 42},
            }
        ],
    }
    proposed = {
        "capabilities": [{"name": "Billing", "status": "working"}],
        "_agent_view_floor_applied": {
            "summarized_capabilities": 1,
            "summarized_friction": 0,
        },
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    cap = new_raw["capabilities"][0]
    assert cap["status"] == "working"  # edit applied
    assert cap["notes"] == "durable notes"
    assert cap["backing_paths"] == ["a.py", "b.py"]
    assert cap["source"]["pr_number"] == 42


def test_apply_agent_update_floored_capability_nonblank_value_overwrites():
    """A skeletonized capability whose notes the agent actually rewrote (non-
    blank) still takes the new value — preservation only fires on blanks."""
    current = {
        "revision": 5,
        "capabilities": [
            {"name": "Billing", "status": "partial", "notes": "old notes"}
        ],
    }
    proposed = {
        "capabilities": [
            {"name": "Billing", "status": "working", "notes": "rewritten"}
        ],
        "_agent_view_floor_applied": {"summarized_capabilities": 1},
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert new_raw["capabilities"][0]["notes"] == "rewritten"


def test_apply_agent_update_floored_friction_skeleton_preserves_detail():
    """Bumping a skeletonized friction's status preserves its durable
    description, seen_in, mitigations_tried, source, and the promoted_to_gap_id
    link (the skeleton keeps only id/name/status, so the link is stripped before
    the agent sees it and a blank resubmit must NOT sever it)."""
    current = {
        "revision": 5,
        "friction": [
            {
                "id": "f-1",
                "name": "flaky CI",
                "status": "active",
                "description": "durable desc",
                "seen_in": ["d-1"],
                "mitigations_tried": ["retry"],
                "source": {"type": "agent_observation"},
                "promoted_to_gap_id": "g-flaky-ci",
            }
        ],
    }
    proposed = {
        "friction": [{"id": "f-1", "name": "flaky CI", "status": "mitigated"}],
        "_agent_view_floor_applied": {
            "summarized_friction": 1,
            "summarized_capabilities": 0,
        },
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    fr = new_raw["friction"][0]
    assert fr["status"] == "mitigated"  # edit applied
    assert fr["description"] == "durable desc"
    assert fr["seen_in"] == ["d-1"]
    assert fr["mitigations_tried"] == ["retry"]
    assert fr["source"]["type"] == "agent_observation"
    assert fr["promoted_to_gap_id"] == "g-flaky-ci"


def test_apply_agent_update_caps_friction_cleared_without_floor_marker():
    """Without a floor marker (or with the section not skeletonized) a blank
    capability/friction detail field is a genuine clear and applies."""
    current = {
        "revision": 5,
        "capabilities": [
            {"name": "Billing", "status": "partial", "notes": "old"}
        ],
        "friction": [
            {"id": "f-1", "name": "flaky", "status": "active", "description": "d"}
        ],
    }
    proposed = {
        "capabilities": [{"name": "Billing", "status": "working", "notes": ""}],
        "friction": [{"id": "f-1", "name": "flaky", "status": "mitigated"}],
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert new_raw["capabilities"][0]["notes"] == ""
    assert new_raw["friction"][0]["description"] == ""


# ---------------------------------------------------------------------------
# apply_agent_update — operator-seeded intent preservation
# ---------------------------------------------------------------------------


def _operator_seeded_current() -> dict:
    """A problem-model as the onboarding seed leaves it: operator purpose with
    a user_message source, operator context/constraints, last_updated_by=user."""
    return {
        "revision": 1,
        "last_updated_by": "user",
        "intent": {
            "purpose": "Ship a reliable billing system.",
            "current_focus": "Ship a reliable billing system.",
            "source": {"type": "user_message", "message_id": "m-1", "excerpt": "Ship..."},
        },
        "context": {"project_type": "startup", "users": "SMBs"},
        "constraints": ["No PII in logs", "GDPR compliant"],
    }


def test_apply_agent_update_preserves_operator_intent_when_omitted():
    """Agent omits intent/context/constraints entirely → operator values kept."""
    current = _operator_seeded_current()
    proposed: dict = {"gaps": [{"id": "g1", "name": "x", "status": "active"}]}
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert new_raw["intent"]["purpose"] == "Ship a reliable billing system."
    assert new_raw["intent"]["source"]["type"] == "user_message"
    assert new_raw["context"]["project_type"] == "startup"
    assert new_raw["constraints"] == ["No PII in logs", "GDPR compliant"]


def test_apply_agent_update_preserves_operator_intent_when_blanked():
    """Agent sends empty purpose / blank context / empty constraints → kept."""
    current = _operator_seeded_current()
    proposed = {
        "intent": {"purpose": "", "current_focus": "Wire up Stripe webhooks."},
        "context": {},
        "constraints": [],
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert new_raw["intent"]["purpose"] == "Ship a reliable billing system."
    assert new_raw["intent"]["source"]["type"] == "user_message"
    assert new_raw["context"]["project_type"] == "startup"
    assert new_raw["constraints"] == ["No PII in logs", "GDPR compliant"]
    # Agent-owned current_focus advances freely.
    assert new_raw["intent"]["current_focus"] == "Wire up Stripe webhooks."


def test_apply_agent_update_preserved_purpose_not_reported_as_intent_changed():
    """Honest diff: when only the purpose is preserved (current_focus
    unchanged), the carry-forward must NOT register as intent_changed."""
    current = _operator_seeded_current()
    proposed = {
        # Same current_focus the seed wrote; only purpose is blanked.
        "intent": {"purpose": "", "current_focus": "Ship a reliable billing system."},
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert new_raw["intent"]["purpose"] == "Ship a reliable billing system."
    assert "intent_changed" not in diff


def test_apply_agent_update_allows_explicit_operator_intent_change():
    """A genuine non-empty agent change to purpose still applies + bumps rev."""
    current = _operator_seeded_current()
    proposed = {
        "intent": {"purpose": "Pivot to a usage-metering platform.", "current_focus": ""},
        "context": {"project_type": "product"},
        "constraints": ["SOC2"],
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert new_raw["intent"]["purpose"] == "Pivot to a usage-metering platform."
    assert new_raw["context"]["project_type"] == "product"
    assert new_raw["constraints"] == ["SOC2"]
    assert new_raw["revision"] == 2
    assert diff.get("intent_changed") is True


def test_apply_agent_update_advances_agent_owned_fields_with_intent_preserved():
    """Agent advances current_focus / gaps while purpose is preserved."""
    current = _operator_seeded_current()
    proposed = {
        "intent": {"purpose": "", "current_focus": "Phase 2: invoicing."},
        "gaps": [{"id": "g-new", "name": "invoicing", "status": "active"}],
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert new_raw["intent"]["purpose"] == "Ship a reliable billing system."
    assert new_raw["intent"]["current_focus"] == "Phase 2: invoicing."
    assert {g["id"] for g in new_raw["gaps"]} == {"g-new"}
    assert diff["gaps_opened"] == ["g-new"]


def test_apply_agent_update_does_not_preserve_agent_authored_intent():
    """When prior purpose was agent-authored (no user source), a blank proposed
    purpose is NOT resurrected — only operator-owned words are protected."""
    current = {
        "revision": 2,
        "last_updated_by": "agent",
        "intent": {
            "purpose": "Agent-derived purpose.",
            "current_focus": "focus",
            "source": {"type": "agent_observation"},
        },
    }
    proposed = {"intent": {"purpose": "", "current_focus": "next"}}
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert new_raw["intent"]["purpose"] == ""


def test_apply_agent_update_heals_blank_purpose_from_user_purpose_when_source_null():
    """Regression: purpose="" + source=null + user_purpose populated → self-heal.

    This is the exact broken state that stalled strategic_review tasks: the
    durable model had intent.purpose="" with source=null while user_purpose held
    the operator's real text.  The source-gated guard (purpose_was_operator=False)
    could not restore it; the new user_purpose fallback must.
    """
    current = {
        "revision": 3,
        "last_updated_by": "agent",
        "intent": {
            "purpose": "",
            "current_focus": "some focus",
            "source": None,
            "user_purpose": "Make Blueprint handle any project well.",
        },
    }
    proposed = {"intent": {"purpose": "", "current_focus": "next focus"}}
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    # purpose must be restored from user_purpose
    assert new_raw["intent"]["purpose"] == "Make Blueprint handle any project well."
    # source must be re-stamped as user_message so future cycles protect it
    assert new_raw["intent"]["source"]["type"] == "user_message"
    # agent-owned current_focus advances freely
    assert new_raw["intent"]["current_focus"] == "next focus"


def test_apply_agent_update_heals_schema_placeholder_from_user_purpose():
    """Purpose matches a schema-placeholder pattern → healed from user_purpose."""
    for placeholder in ("...", "<from analysis>", "<placeholder>", ""):
        current = {
            "revision": 1,
            "intent": {
                "purpose": placeholder,
                "source": None,
                "user_purpose": "Real operator purpose.",
            },
        }
        proposed = {"intent": {"purpose": placeholder}}
        new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
        assert new_raw["intent"]["purpose"] == "Real operator purpose.", (
            f"Failed to heal placeholder: {placeholder!r}"
        )


def test_apply_agent_update_no_heal_when_user_purpose_empty():
    """If user_purpose is also empty there is nothing to restore; stay blank."""
    current = {
        "revision": 1,
        "intent": {"purpose": "", "source": None, "user_purpose": ""},
    }
    proposed = {"intent": {"purpose": ""}}
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert new_raw["intent"]["purpose"] == ""


# ---------------------------------------------------------------------------
# apply_agent_update — append-only gap-memory restore after payload trim
# ---------------------------------------------------------------------------


def _gap_with_memory(
    history_events: list[str],
    past_moves: list[str],
    *,
    status: str = "active",
) -> dict:
    """A single durable gap carrying append-only history + past_moves memory."""
    # ``ts`` is stable per event tag so a trimmed view (the same events the
    # agent saw, re-submitted) compares equal to the durable entries — exactly
    # how the real payload trim preserves each event's original timestamp.
    return {
        "id": "g-mem",
        "name": "Cross-cycle memory gap",
        "status": status,
        "history": [
            {"ts": f"2026-04-21T00:00:00-{ev}", "event": ev}
            for ev in history_events
        ],
        "past_moves": list(past_moves),
    }


def test_apply_agent_update_restores_trimmed_gap_history_on_round_trip():
    """The bug this guards: the payload trims each gap's history to the most
    recent few entries before the agent sees it, the agent round-trips the
    trimmed view back as a full replacement, and without restore the older
    durable entries are silently lost. The durable older events must survive."""
    current = {
        "revision": 5,
        "gaps": [
            _gap_with_memory(
                ["opened", "priority_raised", "move_attached", "move_failed", "retried"],
                ["d-1", "d-2", "d-3"],
            )
        ],
    }
    # Simulate the upstream payload trim: agent only ever saw the last 2 entries
    # and submits exactly that trimmed view back, unchanged.
    proposed = {
        "gaps": [
            _gap_with_memory(["move_failed", "retried"], ["d-3"]),
        ]
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    gap = new_raw["gaps"][0]
    events = [e["event"] for e in gap["history"]]
    # All five durable events survive in order — the trim cannot truncate them.
    assert events == [
        "opened",
        "priority_raised",
        "move_attached",
        "move_failed",
        "retried",
    ]
    # past_moves likewise restored to the full durable union.
    assert gap["past_moves"] == ["d-1", "d-2", "d-3"]


def test_apply_agent_update_records_genuine_gap_history_append():
    """The agent can still extend the log: a genuinely new event it adds to the
    trimmed view is appended after the restored durable head."""
    current = {
        "revision": 5,
        "gaps": [
            _gap_with_memory(
                ["opened", "priority_raised", "move_attached", "move_failed"],
                ["d-1", "d-2"],
            )
        ],
    }
    # Agent saw the trimmed tail and appended one new event + one new past move.
    proposed = {
        "gaps": [_gap_with_memory(["move_failed", "move_succeeded"], ["d-2", "d-3"])],
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    gap = new_raw["gaps"][0]
    events = [e["event"] for e in gap["history"]]
    # Restored durable head + the genuinely new event, no duplication.
    assert events == [
        "opened",
        "priority_raised",
        "move_attached",
        "move_failed",
        "move_succeeded",
    ]
    assert gap["past_moves"] == ["d-1", "d-2", "d-3"]


def test_apply_agent_update_new_gap_memory_taken_as_is():
    """A brand-new gap (no prior durable record) is taken verbatim — there is
    nothing to restore, and the restore must not invent history for it."""
    current = {
        "revision": 5,
        "gaps": [_gap_with_memory(["opened"], ["d-1"])],
    }
    proposed = {
        "gaps": [
            _gap_with_memory(["opened"], ["d-1"]),  # surviving gap
            {
                "id": "g-fresh",
                "name": "Brand new gap",
                "status": "active",
                "history": [{"ts": "2026-04-21T00:00:00", "event": "opened"}],
                "past_moves": [],
            },
        ],
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    fresh = next(g for g in new_raw["gaps"] if g["id"] == "g-fresh")
    assert [e["event"] for e in fresh["history"]] == ["opened"]
    assert fresh["past_moves"] == []


def test_apply_agent_update_closed_gap_keeps_full_memory_into_archive():
    """A gap the agent legitimately closes keeps its FULL durable memory as it
    moves to the archive — the trimmed round-trip must not truncate the record
    that gets preserved for posterity."""
    current = {
        "revision": 5,
        "gaps": [
            _gap_with_memory(
                ["opened", "priority_raised", "move_attached", "shipped"],
                ["d-1", "d-2"],
            )
        ],
    }
    # Agent closes the gap, having only seen the trimmed tail.
    proposed = {
        "gaps": [_gap_with_memory(["shipped"], ["d-2"], status="closed")],
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    assert new_raw["gaps"] == []  # archived, not active
    archived = next(g for g in new_raw["archived_gaps"] if g["id"] == "g-mem")
    assert [e["event"] for e in archived["history"]] == [
        "opened",
        "priority_raised",
        "move_attached",
        "shipped",
    ]
    assert archived["past_moves"] == ["d-1", "d-2"]


def test_apply_agent_update_restored_gap_memory_is_capped():
    """The restored append-only logs stay bounded by the durable caps so the
    JSONB column cannot grow without limit — oldest entries roll off."""
    over = GAP_HISTORY_DURABLE_MAX + 25
    current = {
        "revision": 5,
        "gaps": [
            _gap_with_memory(
                [f"e{i}" for i in range(over)],
                [f"d-{i}" for i in range(GAP_PAST_MOVES_DURABLE_MAX + 25)],
            )
        ],
    }
    proposed = {"gaps": [_gap_with_memory(["e-tail"], ["d-tail"])]}
    new_raw, _ = apply_agent_update(current, proposed, "2026-04-21T12:00:00")
    gap = new_raw["gaps"][0]
    assert len(gap["history"]) == GAP_HISTORY_DURABLE_MAX
    assert len(gap["past_moves"]) == GAP_PAST_MOVES_DURABLE_MAX
    # Newest entry (the agent's appended tail) is retained at the end.
    assert gap["history"][-1]["event"] == "e-tail"
    assert gap["past_moves"][-1] == "d-tail"
    # Oldest durable entries rolled off the front: 225 durable + 1 appended
    # tail = 226, capped to 200 drops the first 26 (indices 0..25).
    assert gap["history"][0]["event"] == "e26"
    assert gap["past_moves"][0] == "d-26"


# ---------------------------------------------------------------------------
# apply_agent_update — 3-way operator-wins merge (concurrent mid-cycle edit)
# ---------------------------------------------------------------------------


def test_apply_agent_update_operator_purpose_edit_mid_cycle_wins():
    """The race this PR closes: operator edits intent.purpose to a NEW non-blank
    value mid-cycle; the agent (computing from the baseline snapshot) proposes a
    DIFFERENT non-blank value. With a baseline supplied, the operator's value
    wins, the agent's value is recorded to the ledger, and agent-owned advances
    (a new gap, advanced current_focus) still apply + bump the revision."""
    baseline = _operator_seeded_current()  # purpose = "Ship a reliable billing system."
    # Operator edits the purpose mid-cycle (current diverges from baseline).
    current = _operator_seeded_current()
    current["intent"]["purpose"] = "Ship reliable billing AND metering."
    current["revision"] = 2
    # Agent, working from the baseline, proposes its own different purpose plus
    # genuine agent-owned advances.
    proposed = {
        "intent": {
            "purpose": "Pivot fully to a metering platform.",
            "current_focus": "Phase 2: invoicing.",
        },
        "gaps": [{"id": "g-new", "name": "invoicing", "status": "active"}],
    }
    new_raw, diff = apply_agent_update(
        current, proposed, "2026-04-21T12:00:00", baseline_raw=baseline
    )
    # Operator's mid-cycle purpose survives; agent's is rejected.
    assert new_raw["intent"]["purpose"] == "Ship reliable billing AND metering."
    assert new_raw["intent"]["source"]["type"] == "user_message"
    # Agent-owned advances still land + revision bumps.
    assert new_raw["intent"]["current_focus"] == "Phase 2: invoicing."
    assert {g["id"] for g in new_raw["gaps"]} == {"g-new"}
    assert new_raw["revision"] == 3
    assert diff["gaps_opened"] == ["g-new"]
    # The overridden agent value is recorded to the ledger for reconciliation.
    ledger = new_raw["rejected_agent_edits"]
    assert len(ledger) == 1
    assert ledger[0]["field"] == "intent.purpose"
    assert ledger[0]["operator_value"] == "Ship reliable billing AND metering."
    assert ledger[0]["agent_value"] == "Pivot fully to a metering platform."


def test_apply_agent_update_operator_context_and_constraints_edit_wins():
    """A mid-cycle operator edit to context / constraints wins over a divergent
    agent write, and each override is recorded."""
    baseline = _operator_seeded_current()
    current = _operator_seeded_current()
    current["context"] = {"project_type": "enterprise", "users": "Fortune 500"}
    current["constraints"] = ["SOC2", "No PII in logs"]
    proposed = {
        "intent": {"purpose": "Ship a reliable billing system.", "current_focus": "x"},
        "context": {"project_type": "startup", "users": "indie hackers"},
        "constraints": ["Move fast"],
    }
    new_raw, _ = apply_agent_update(
        current, proposed, "2026-04-21T12:00:00", baseline_raw=baseline
    )
    assert new_raw["context"]["project_type"] == "enterprise"
    assert new_raw["constraints"] == ["SOC2", "No PII in logs"]
    fields = {e["field"] for e in new_raw["rejected_agent_edits"]}
    assert fields == {"context", "constraints"}


def test_apply_agent_update_operator_purpose_rejected_without_user_message_provenance():
    """Always-protect: when purpose is operator-owned (user_message source) and
    current == baseline, an agent change without user_message provenance is
    rejected to the ledger and the operator value is preserved."""
    baseline = _operator_seeded_current()
    current = _operator_seeded_current()  # identical to baseline → untouched
    proposed = {
        "intent": {
            "purpose": "Refined: reliable billing, fast onboarding.",
            "current_focus": "Phase 2: invoicing.",
        },
    }
    new_raw, diff = apply_agent_update(
        current, proposed, "2026-04-21T12:00:00", baseline_raw=baseline
    )
    # Purpose preserved — no user_message provenance on the proposed change.
    assert new_raw["intent"]["purpose"] == "Ship a reliable billing system."
    assert new_raw["intent"]["source"]["type"] == "user_message"
    # Agent-owned current_focus still advances freely.
    assert new_raw["intent"]["current_focus"] == "Phase 2: invoicing."
    assert diff.get("intent_changed") is True  # current_focus changed
    # Rejected purpose change recorded to the ledger.
    ledger = new_raw["rejected_agent_edits"]
    assert len(ledger) == 1
    assert ledger[0]["field"] == "intent.purpose"
    assert ledger[0]["agent_value"] == "Refined: reliable billing, fast onboarding."
    assert ledger[0]["operator_value"] == "Ship a reliable billing system."


def test_apply_agent_update_operator_purpose_allowed_with_user_message_provenance():
    """Escape hatch: when the agent supplies user_message provenance on the
    proposed intent, the purpose change is accepted even though it is
    operator-owned (current == baseline, no mid-cycle edit)."""
    baseline = _operator_seeded_current()
    current = _operator_seeded_current()
    proposed = {
        "intent": {
            "purpose": "Refined: reliable billing, fast onboarding.",
            "current_focus": "Phase 2: invoicing.",
            "source": {"type": "user_message", "message_id": "m-42", "excerpt": "Please refine the billing goal"},
        },
    }
    new_raw, diff = apply_agent_update(
        current, proposed, "2026-04-21T12:00:00", baseline_raw=baseline
    )
    # Purpose accepted — agent supplied user_message provenance.
    assert new_raw["intent"]["purpose"] == "Refined: reliable billing, fast onboarding."
    assert new_raw["intent"]["source"]["type"] == "user_message"
    assert new_raw["intent"]["source"]["message_id"] == "m-42"
    assert diff.get("intent_changed") is True
    assert "rejected_agent_edits" not in new_raw


def test_apply_agent_update_agent_context_and_constraints_change_applies():
    """When the operator did NOT touch a field (current == baseline), the agent's
    new value applies even with a baseline supplied — no ledger entry."""
    baseline = _operator_seeded_current()
    current = _operator_seeded_current()  # identical to baseline → untouched
    proposed = {
        "intent": {"purpose": "Ship a reliable billing system.", "current_focus": "x"},
        "context": {"project_type": "scale-up", "users": "SMBs"},
        "constraints": ["No PII in logs", "GDPR compliant", "SOC2"],
    }
    new_raw, _ = apply_agent_update(
        current, proposed, "2026-04-21T12:00:00", baseline_raw=baseline
    )
    # Context: agent's change applies (not always-protected).
    assert new_raw["context"]["project_type"] == "scale-up"
    # Constraints: no mid-cycle operator edit — agent's new value applies.
    assert new_raw["constraints"] == ["No PII in logs", "GDPR compliant", "SOC2"]
    assert "rejected_agent_edits" not in new_raw


def test_apply_agent_update_operator_edit_agent_kept_field_no_conflict():
    """Operator edited a field mid-cycle but the agent did NOT change it (agent
    value == baseline): operator wins silently with NO ledger entry."""
    baseline = _operator_seeded_current()
    current = _operator_seeded_current()
    current["constraints"] = ["No PII in logs", "GDPR compliant", "PCI-DSS"]
    # Agent echoes the baseline constraints (didn't touch them).
    proposed = {
        "intent": {"purpose": "Ship a reliable billing system.", "current_focus": "x"},
        "constraints": ["No PII in logs", "GDPR compliant"],
    }
    new_raw, _ = apply_agent_update(
        current, proposed, "2026-04-21T12:00:00", baseline_raw=baseline
    )
    # Operator's value wins.
    assert new_raw["constraints"] == ["No PII in logs", "GDPR compliant", "PCI-DSS"]
    # Agent didn't try to change it → no conflict recorded.
    assert "rejected_agent_edits" not in new_raw


def test_apply_agent_update_no_baseline_is_unchanged_from_today():
    """Degrade safely: with baseline_raw=None the merge is skipped and the
    output is byte-identical to the blank-preservation-only path."""
    current = _operator_seeded_current()
    current["intent"]["purpose"] = "Operator changed this mid-cycle."
    proposed = {
        "intent": {"purpose": "Agent's divergent purpose.", "current_focus": "x"},
    }
    without_baseline, _ = apply_agent_update(
        current, dict(proposed), "2026-04-21T12:00:00"
    )
    # Without a baseline, today's behavior stands: the agent's non-blank purpose
    # overwrites (the race is NOT caught — that's the legacy contract).
    assert without_baseline["intent"]["purpose"] == "Agent's divergent purpose."
    assert "rejected_agent_edits" not in without_baseline


def test_apply_agent_update_agent_authored_purpose_not_merged():
    """When the prior purpose was agent-authored (source != user_message), the
    purpose is agent-owned: the agent overwrites it even if current != baseline,
    and nothing is recorded."""
    baseline = {
        "revision": 1,
        "intent": {
            "purpose": "Agent purpose v1.",
            "current_focus": "f",
            "source": {"type": "agent_observation"},
        },
    }
    current = {
        "revision": 2,
        "intent": {
            "purpose": "Agent purpose v2.",  # differs from baseline
            "current_focus": "f",
            "source": {"type": "agent_observation"},
        },
    }
    proposed = {
        "intent": {"purpose": "Agent purpose v3.", "current_focus": "f"},
    }
    new_raw, _ = apply_agent_update(
        current, proposed, "2026-04-21T12:00:00", baseline_raw=baseline
    )
    assert new_raw["intent"]["purpose"] == "Agent purpose v3."
    assert "rejected_agent_edits" not in new_raw


def test_apply_agent_update_ledger_carried_forward_and_capped():
    """The ledger persists across cycles (carried from current) and is capped at
    REJECTED_AGENT_EDITS_MAX, oldest rolled off."""
    from app.problem_model import REJECTED_AGENT_EDITS_MAX

    prior_ledger = [
        {"at": "t", "field": "context", "kind": "value_conflict", "i": i}
        for i in range(REJECTED_AGENT_EDITS_MAX)
    ]
    baseline = _operator_seeded_current()
    current = _operator_seeded_current()
    current["intent"]["purpose"] = "Operator edit."
    current["rejected_agent_edits"] = prior_ledger
    proposed = {
        "intent": {"purpose": "Agent divergent.", "current_focus": "x"},
    }
    new_raw, _ = apply_agent_update(
        current, proposed, "2026-04-21T12:00:00", baseline_raw=baseline
    )
    ledger = new_raw["rejected_agent_edits"]
    assert len(ledger) == REJECTED_AGENT_EDITS_MAX  # capped
    # Newest (this cycle's purpose conflict) retained at the end; oldest dropped.
    assert ledger[-1]["field"] == "intent.purpose"
    assert ledger[0]["i"] == 1  # entry 0 rolled off


# ---------------------------------------------------------------------------
# always-protect: repair-path clobber scenario (BUG_REPORT 8154c2d3)
# ---------------------------------------------------------------------------


def test_always_protect_repair_placeholder_purpose_rejected():
    """Concrete repair-path clobber scenario: the JSON repair reopen runs as a
    fresh session with no prior analysis context and echoes schema-template text
    as intent.purpose. Because current.intent.source.type == 'user_message' and
    the proposed intent carries no user_message provenance, the placeholder is
    rejected and the operator's real purpose is preserved.
    Note: constraints have no always-protect; the thinker-side fix (embedding
    prior analysis in the repair prompt) prevents garbage constraints."""
    baseline = _operator_seeded_current()
    current = _operator_seeded_current()
    placeholder = (
        "Provide a parseable problem_model JSON object with all required "
        "top-level keys for /tmp/problem_model_abc123.json"
    )
    proposed = {
        "intent": {"purpose": placeholder, "current_focus": "x"},
        "constraints": ["Provide all required top-level keys"],
    }
    new_raw, _ = apply_agent_update(
        current, proposed, "2026-04-21T12:00:00", baseline_raw=baseline
    )
    # Operator purpose is preserved; placeholder is rejected.
    assert new_raw["intent"]["purpose"] == "Ship a reliable billing system."
    assert new_raw["intent"]["source"]["type"] == "user_message"
    # Purpose override recorded.
    ledger = new_raw["rejected_agent_edits"]
    fields = {e["field"] for e in ledger}
    assert "intent.purpose" in fields
    purpose_entry = next(e for e in ledger if e["field"] == "intent.purpose")
    assert purpose_entry["agent_value"] == placeholder


def test_always_protect_purpose_baseline_equal_no_ledger_when_agent_unchanged():
    """When the agent does NOT attempt to change an operator-owned purpose
    (proposed == current), no ledger entry is emitted."""
    baseline = _operator_seeded_current()
    current = _operator_seeded_current()
    proposed = {
        "intent": {
            "purpose": "Ship a reliable billing system.",  # same as current
            "current_focus": "Phase 2.",
        },
    }
    new_raw, _ = apply_agent_update(
        current, proposed, "2026-04-21T12:00:00", baseline_raw=baseline
    )
    assert new_raw["intent"]["purpose"] == "Ship a reliable billing system."
    assert "rejected_agent_edits" not in new_raw


def test_always_protect_empty_constraints_not_flagged():
    """When current constraints are empty, an agent that also sends empty
    constraints generates no ledger entry (nothing to protect)."""
    baseline = _operator_seeded_current()
    current = _operator_seeded_current()
    # Operator cleared their constraints.
    current["constraints"] = []
    baseline_empty = dict(baseline)
    baseline_empty["constraints"] = []
    proposed = {
        "intent": {"purpose": "Ship a reliable billing system.", "current_focus": "x"},
        "constraints": [],
    }
    new_raw, _ = apply_agent_update(
        current, proposed, "2026-04-21T12:00:00", baseline_raw=baseline_empty
    )
    assert new_raw["constraints"] == []
    assert "rejected_agent_edits" not in new_raw


# ---------------------------------------------------------------------------
# Second same-cycle agent write — ancestor is the agent's own prior proposal
# (the bug this PR fixes: the callers now pass the agent's last proposal, not
# the cycle-start snapshot, as baseline on writes after the first).
# ---------------------------------------------------------------------------


def test_operator_owned_subset_extracts_only_editable_fields():
    """operator_owned_subset pulls intent.purpose / context / constraints and
    tolerates extra agent-owned keys (so it accepts a raw pre-merge proposal)."""
    from app.problem_model import operator_owned_subset

    subset = operator_owned_subset(
        {
            "intent": {"purpose": "P", "current_focus": "ignored"},
            "context": {"project_type": "startup"},
            "constraints": ["c1"],
            "gaps": [{"id": "g", "name": "n", "status": "active"}],
            "_deletions": {"gaps": ["g"]},
        }
    )
    assert subset == {
        "intent": {"purpose": "P"},
        "context": {"project_type": "startup"},
        "constraints": ["c1"],
    }
    # Tolerates None / missing.
    assert operator_owned_subset(None) == {
        "intent": {"purpose": ""},
        "context": {},
        "constraints": [],
    }


def test_apply_agent_update_second_write_against_own_prior_proposal_no_false_conflict():
    """The core fix contract: when the merge ancestor is the agent's OWN prior
    proposal (what the callers now store + pass on a 2nd same-cycle write), a
    different 2nd context/constraints applies cleanly with NO fabricated
    operator-conflict — even though current != cycle-start baseline."""
    from app.problem_model import operator_owned_subset

    # Write 1: agent advances context/constraints from the cycle-start snapshot.
    snapshot = _operator_seeded_current()
    proposed1 = {
        "intent": {"purpose": "Ship a reliable billing system.", "current_focus": "x"},
        "context": {"project_type": "scale-up", "users": "SMBs"},
        "constraints": ["No PII in logs", "GDPR compliant", "SOC2"],
    }
    new_raw1, _ = apply_agent_update(
        _operator_seeded_current(),
        dict(proposed1),
        "2026-04-21T12:00:00",
        baseline_raw=snapshot,
    )
    assert new_raw1["context"]["project_type"] == "scale-up"
    assert "rejected_agent_edits" not in new_raw1

    # Write 2: ancestor is the agent's prior proposal (not the snapshot), current
    # is the agent's own first write. A genuinely different 2nd value persists.
    proposed2 = {
        "intent": {"purpose": "Ship a reliable billing system.", "current_focus": "y"},
        "context": {"project_type": "enterprise", "users": "Fortune 500"},
        "constraints": ["No PII in logs", "GDPR compliant", "SOC2", "HIPAA"],
    }
    new_raw2, _ = apply_agent_update(
        new_raw1,
        dict(proposed2),
        "2026-04-21T12:05:00",
        baseline_raw=operator_owned_subset(proposed1),
    )
    # The 2nd write's distinct context/constraints persist — NOT dropped.
    assert new_raw2["context"]["project_type"] == "enterprise"
    assert new_raw2["constraints"] == [
        "No PII in logs",
        "GDPR compliant",
        "SOC2",
        "HIPAA",
    ]
    # And NO false operator-conflict was fabricated.
    assert "rejected_agent_edits" not in new_raw2


def test_apply_agent_update_operator_edit_between_two_writes_still_wins():
    """Case 3: an operator edits between the agent's two writes. With the
    ancestor = the agent's prior proposal, the operator's mid-cycle value still
    wins on the 2nd write (current moved away from the agent's proposal)."""
    from app.problem_model import operator_owned_subset

    snapshot = _operator_seeded_current()
    proposed1 = {
        "intent": {"purpose": "Ship a reliable billing system.", "current_focus": "x"},
        "context": {"project_type": "scale-up", "users": "SMBs"},
        "constraints": ["No PII in logs"],
    }
    new_raw1, _ = apply_agent_update(
        _operator_seeded_current(),
        dict(proposed1),
        "2026-04-21T12:00:00",
        baseline_raw=snapshot,
    )
    # Operator edits problem_model mid-cycle (admin patch stamps user + new vals).
    after_operator = dict(new_raw1)
    after_operator["context"] = {"project_type": "regulated-fintech"}
    after_operator["constraints"] = ["No PII in logs", "PCI-DSS"]
    after_operator["last_updated_by"] = "user"

    # Write 2: agent proposes its own divergent values against its prior proposal.
    proposed2 = {
        "intent": {"purpose": "Ship a reliable billing system.", "current_focus": "y"},
        "context": {"project_type": "enterprise"},
        "constraints": ["Move fast"],
    }
    new_raw2, _ = apply_agent_update(
        after_operator,
        dict(proposed2),
        "2026-04-21T12:05:00",
        baseline_raw=operator_owned_subset(proposed1),
    )
    # Operator's mid-cycle edit survives; the agent's divergent values are
    # rejected and recorded.
    assert new_raw2["context"]["project_type"] == "regulated-fintech"
    assert new_raw2["constraints"] == ["No PII in logs", "PCI-DSS"]
    fields = {e["field"] for e in new_raw2["rejected_agent_edits"]}
    assert fields == {"context", "constraints"}


# ---------------------------------------------------------------------------
# apply_user_intent_patch — the user-facing write path (onboarding + PATCH)
# ---------------------------------------------------------------------------


def test_apply_user_intent_patch_seeds_goal_into_intent():
    """Onboarding goal capture: an empty model gains the user's intent and
    is attributed to the user."""
    model = apply_user_intent_patch(
        None,
        {"intent": {"purpose": "Ship a CLI", "current_focus": "Ship a CLI"}},
        "2026-06-02T12:00:00",
    )
    assert model.intent.purpose == "Ship a CLI"
    assert model.intent.current_focus == "Ship a CLI"
    assert model.revision == 1
    assert model.last_updated == "2026-06-02T12:00:00"
    assert model.last_updated_by == "user"


def test_apply_user_intent_patch_merges_steering_subset():
    """Optional steering reaches the context / constraints subset."""
    model = apply_user_intent_patch(
        {"revision": 2, "intent": {"purpose": "x"}},
        {
            "context": {"project_type": "startup", "users": "early adopters"},
            "constraints": ["No breaking changes"],
        },
        "2026-06-02T12:00:00",
    )
    assert model.context.project_type == "startup"
    assert model.constraints == ["No breaking changes"]
    # Existing intent is preserved when not in the patch.
    assert model.intent.purpose == "x"
    assert model.revision == 3


def test_apply_user_intent_patch_empty_patch_is_valid_no_op_bump():
    """An empty steering payload preserves content but still records a
    user-attributed revision — today's skip-the-goal behavior."""
    model = apply_user_intent_patch(
        {"revision": 4, "intent": {"purpose": "kept"}},
        {},
        "2026-06-02T12:00:00",
    )
    assert model.intent.purpose == "kept"
    assert model.revision == 5
    assert model.last_updated_by == "user"


def test_apply_user_intent_patch_rejects_agent_owned_fields():
    """Users cannot overwrite gaps / capabilities / other agent-owned state."""
    with pytest.raises(ValueError, match="agent-owned"):
        apply_user_intent_patch(
            None,
            {"gaps": [{"id": "g-x", "name": "x"}]},
            "2026-06-02T12:00:00",
        )


def test_apply_user_intent_patch_rejects_malformed_subset():
    """A malformed value inside an allowed field still surfaces as a
    ValidationError, not a silent persist."""
    with pytest.raises(ValidationError):
        apply_user_intent_patch(
            None,
            {"intent": {"purpose": 123}},  # purpose must be a str
            "2026-06-02T12:00:00",
        )


def test_apply_user_intent_patch_intent_edit_stamps_operator_source():
    """Editing intent.purpose through the user-facing path re-stamps the
    operator (user_message) provenance the shallow merge would otherwise drop —
    so the operator-wins protection keeps guarding the edited purpose."""
    model = apply_user_intent_patch(
        {"revision": 3, "intent": {"purpose": "old", "current_focus": "f"}},
        {"intent": {"purpose": "Operator-edited purpose", "current_focus": "f"}},
        "2026-06-02T12:00:00",
    )
    assert model.intent.purpose == "Operator-edited purpose"
    assert model.intent.source is not None
    assert model.intent.source.type == "user_message"
    assert model.intent.source.excerpt == "Operator-edited purpose"


def test_apply_user_intent_patch_blanking_purpose_clears_source():
    """Blanking the purpose through the editor clears the source so an empty
    operator intent is not spuriously treated as protected."""
    model = apply_user_intent_patch(
        {
            "revision": 1,
            "intent": {
                "purpose": "had a purpose",
                "source": {"type": "user_message", "message_id": "m-1"},
            },
        },
        {"intent": {"purpose": "   "}},
        "2026-06-02T12:00:00",
    )
    assert model.intent.purpose.strip() == ""
    assert model.intent.source is None


def test_apply_user_intent_patch_without_intent_preserves_existing_source():
    """A patch that omits intent (onboarding's context/constraints-only
    steering) leaves the existing message_id-bearing source untouched."""
    model = apply_user_intent_patch(
        {
            "revision": 1,
            "intent": {
                "purpose": "seeded goal",
                "source": {
                    "type": "user_message",
                    "message_id": "m-42",
                    "excerpt": "seeded goal",
                },
            },
        },
        {"context": {"project_type": "startup"}},
        "2026-06-02T12:00:00",
    )
    assert model.intent.purpose == "seeded goal"
    assert model.intent.source is not None
    assert model.intent.source.type == "user_message"
    assert model.intent.source.message_id == "m-42"
    assert model.context.project_type == "startup"


def test_editor_purpose_edit_survives_divergent_agent_cycle():
    """End-to-end protection: an operator edits the purpose via the user-facing
    patch path, then a strategic cycle proposes a different purpose. The
    operator's value must win and the agent's discarded value must be recorded
    in the rejected-edits ledger — exactly as an onboarded purpose is guarded."""
    # The model the agent saw at cycle spawn (onboarded purpose).
    baseline_raw = {
        "revision": 5,
        "intent": {
            "purpose": "Onboarded purpose",
            "current_focus": "phase 1",
            "source": {"type": "user_message", "message_id": "m-seed"},
        },
    }
    # Operator edits the purpose mid-cycle through the official editor path.
    edited = apply_user_intent_patch(
        baseline_raw,
        {"intent": {"purpose": "Operator-edited purpose", "current_focus": "phase 1"}},
        "2026-06-02T12:00:00",
    )
    assert edited.intent.source is not None
    assert edited.intent.source.type == "user_message"

    # Agent's next cycle proposes a divergent purpose.
    proposed = {
        "revision": 5,
        "intent": {"purpose": "Agent-proposed purpose", "current_focus": "phase 2"},
    }
    new_raw, _diff = apply_agent_update(
        edited.model_dump(),
        proposed,
        "2026-06-02T13:00:00",
        baseline_raw=baseline_raw,
    )

    # Operator's purpose wins; agent's discarded value is in the ledger.
    assert new_raw["intent"]["purpose"] == "Operator-edited purpose"
    ledger = new_raw["rejected_agent_edits"]
    assert any(
        entry["field"] == "intent.purpose"
        and entry["operator_value"] == "Operator-edited purpose"
        and entry["agent_value"] == "Agent-proposed purpose"
        for entry in ledger
    )


# ---------------------------------------------------------------------------
# user_purpose — durable operator-owned intent field
# ---------------------------------------------------------------------------


def test_apply_user_intent_patch_sets_user_purpose():
    """Patching intent.purpose through the operator path also sets user_purpose."""
    model = apply_user_intent_patch(
        None,
        {"intent": {"purpose": "Ship a CLI tool", "current_focus": ""}},
        "2026-06-11T00:00:00",
    )
    assert model.intent.purpose == "Ship a CLI tool"
    assert model.intent.user_purpose == "Ship a CLI tool", (
        "user_purpose must mirror the operator-set purpose"
    )


def test_apply_user_intent_patch_blanking_purpose_clears_user_purpose():
    """Blanking the purpose via operator patch also clears user_purpose."""
    model = apply_user_intent_patch(
        {"revision": 1, "intent": {"purpose": "Had a goal", "user_purpose": "Had a goal"}},
        {"intent": {"purpose": ""}},
        "2026-06-11T00:00:00",
    )
    assert model.intent.purpose.strip() == ""
    assert model.intent.user_purpose == "", (
        "user_purpose must be cleared when purpose is blanked by operator"
    )


def test_apply_user_intent_patch_non_intent_patch_preserves_user_purpose():
    """A patch that omits intent (context-only / constraints-only) carries
    the existing user_purpose forward — the shallow merge would otherwise drop it."""
    model = apply_user_intent_patch(
        {
            "revision": 1,
            "intent": {
                "purpose": "Ship a CLI",
                "user_purpose": "Ship a CLI",
                "source": {"type": "user_message", "message_id": "m-1"},
            },
        },
        {"context": {"project_type": "startup"}},
        "2026-06-11T00:00:00",
    )
    assert model.intent.user_purpose == "Ship a CLI", (
        "user_purpose must survive a context-only patch (shallow merge replaces intent)"
    )
    assert model.context.project_type == "startup"


def test_apply_agent_update_strips_user_purpose_from_proposal():
    """Agents can NEVER overwrite user_purpose — any proposed value is silently
    stripped and replaced with the durable current value."""
    current = {
        "revision": 10,
        "intent": {
            "purpose": "Real purpose",
            "user_purpose": "Durable operator purpose",
            "source": {"type": "user_message", "message_id": "m-1"},
        },
    }
    # Agent attempts to clobber user_purpose (e.g. after a JSON-repair session).
    proposed = {
        "intent": {
            "purpose": "Agent-rewritten purpose",
            "user_purpose": "CLOBBERED by repair session",
        },
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-06-11T00:00:00")
    assert new_raw["intent"]["user_purpose"] == "Durable operator purpose", (
        "user_purpose must be restored from current regardless of agent proposal"
    )


def test_apply_agent_update_strips_blank_user_purpose_from_proposal():
    """An agent proposal that blanks user_purpose (e.g. schema-template repair)
    must be silently corrected — user_purpose is unconditionally restored from current."""
    current = {
        "revision": 5,
        "intent": {
            "purpose": "Build a dashboard",
            "user_purpose": "Build a dashboard",
        },
    }
    # Agent (or a repair session) submits a blank user_purpose.
    proposed = {
        "intent": {
            "purpose": "Build a dashboard",
            "user_purpose": "",
        },
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-06-11T00:00:00")
    assert new_raw["intent"]["user_purpose"] == "Build a dashboard", (
        "blank user_purpose in agent proposal must be replaced with current durable value"
    )


def test_user_purpose_survives_full_agent_cycle_rewrite():
    """End-to-end: user_purpose set at onboarding survives a complete strategic
    cycle that rewrites intent.purpose and blanks user_purpose.

    The defect scenario (BUG_REPORT 2026-06-11): when baseline_raw is None
    (legacy path), _preserve_operator_intent only protects BLANK agent proposals
    — a non-blank agent rewrite of intent.purpose drops the user_message source,
    which breaks the 3-way merge protection on the NEXT cycle (purpose_was_operator
    becomes False, so constraints are no longer guarded). user_purpose is the
    durable backstop that survives regardless of provenance.
    """
    # Onboarding seeds purpose and user_purpose.
    seeded = apply_user_intent_patch(
        None,
        {
            "intent": {"purpose": "Operator intent", "current_focus": ""},
            "constraints": ["No breaking changes"],
        },
        "2026-06-11T00:00:00",
    )
    assert seeded.intent.user_purpose == "Operator intent"

    # Strategic agent proposes a full rewrite that blanks user_purpose.
    # baseline_raw=None exercises the legacy path where _preserve_operator_intent
    # only blocks blank proposals — a non-blank rewrite goes through and drops source.
    proposed = {
        "intent": {
            "purpose": "Agent-rewritten purpose",
            "current_focus": "Working on X",
            "user_purpose": "",  # blanked — this is the clobber scenario
        },
        "constraints": [],  # blanked
    }
    new_raw, _ = apply_agent_update(
        seeded.model_dump(),
        proposed,
        "2026-06-11T01:00:00",
        baseline_raw=None,  # legacy path: no 3-way merge, only blank-preservation
    )
    # user_purpose must be the durable operator value, regardless of agent proposal —
    # even on the legacy path where intent.purpose could be rewritten.
    assert new_raw["intent"]["user_purpose"] == "Operator intent", (
        "user_purpose must survive a full agent cycle rewrite that blanks it"
    )
    # Without the 3-way merge (baseline_raw=None), the agent's non-blank purpose
    # rewrites intent.purpose — this is the known limitation user_purpose backstops.
    assert new_raw["intent"]["purpose"] == "Agent-rewritten purpose", (
        "intent.purpose is agent-volatile on the legacy path (no 3-way merge) — "
        "user_purpose is the durable backstop that survives"
    )


def test_constraints_survive_full_agent_cycle_wipe_legacy_path():
    """Operator-seeded constraints survive a legacy-path agent cycle that proposes
    constraints: [].

    The observed incident (2026-06-11, rev 345→365): an agent rewrote intent.purpose
    with non-empty text, dropping the user_message source.  On the NEXT cycle
    (baseline_raw=None), purpose_was_operator=False and last_updated_by='agent', so
    _preserve_operator_intent's operator_owned gate evaluated to False and a
    subsequent agent proposal with constraints:[] silently wiped the seeded
    constraints.

    Constraints must now be unconditionally operator-owned, regardless of provenance.
    """
    # Step 1: Operator seeds purpose and constraints at onboarding.
    seeded = apply_user_intent_patch(
        None,
        {
            "intent": {"purpose": "Operator intent", "current_focus": ""},
            "constraints": ["No breaking changes", "PCI-DSS compliance"],
        },
        "2026-06-11T00:00:00",
    )
    assert seeded.constraints == ["No breaking changes", "PCI-DSS compliance"]
    assert seeded.intent.user_purpose == "Operator intent"

    # Step 2: Agent rewrites intent.purpose with non-empty text, dropping provenance.
    # This is the first cycle — it rewrites purpose so intent.source is cleared,
    # making purpose_was_operator=False going into the next cycle.
    first_cycle, _ = apply_agent_update(
        seeded.model_dump(),
        {
            "intent": {
                "purpose": "Agent-rewritten purpose",
                "current_focus": "Working on something",
            },
            "constraints": ["No breaking changes", "PCI-DSS compliance"],
        },
        "2026-06-11T01:00:00",
        baseline_raw=None,
    )
    # Verify provenance was dropped by the agent rewrite.
    assert first_cycle["intent"].get("source") is None, (
        "after agent purpose rewrite, source should be absent"
    )
    assert first_cycle["last_updated_by"] == "agent"

    # Step 3: Second agent cycle — proposes constraints:[] with dropped provenance.
    # This is the exact observed defect path: operator_owned=False so the old
    # _preserve_operator_intent guard was bypassed and constraints were wiped.
    second_cycle, _ = apply_agent_update(
        first_cycle,
        {
            "intent": {
                "purpose": "Agent-rewritten purpose v2",
                "current_focus": "Different focus",
            },
            "constraints": [],  # agent drops all constraints
        },
        "2026-06-11T02:00:00",
        baseline_raw=None,  # legacy path: no 3-way merge
    )
    assert second_cycle["constraints"] == ["No breaking changes", "PCI-DSS compliance"], (
        "operator-seeded constraints must survive an agent proposal with constraints:[] "
        "even on the legacy path where provenance is lost"
    )


def test_intent_purpose_restores_when_blank_regardless_of_source():
    """Regression: intent.purpose auto-heals from user_purpose when source is None.

    Observed incident (2026-07-04, direction d-d3cb208c): intent.purpose was
    found blank with intent.source=null and intent.user_purpose holding the
    correct operator text.  _preserve_operator_intent previously gated on
    source.type=="user_message", so source=None caused purpose_was_operator=False
    and the blank purpose propagated through.  The thinker-gate then rejected the
    problem model (schema-placeholder check: empty string) and every spawned
    strategic_review sub-task failed 3/3 retries, stalling direction retirement
    for 5 cycles.
    """
    # Onboarding seeds purpose, user_purpose, and source=user_message.
    seeded = apply_user_intent_patch(
        None,
        {"intent": {"purpose": "Operator real purpose", "current_focus": ""}},
        "2026-07-04T00:00:00",
    )
    assert seeded.intent.user_purpose == "Operator real purpose"
    assert seeded.intent.source is not None and seeded.intent.source.type == "user_message"

    # Agent cycle 1: rewrites purpose with non-blank text, dropping provenance.
    # This is the known legacy-path behaviour: source becomes None.
    cycle1, _ = apply_agent_update(
        seeded.model_dump(),
        {
            "intent": {
                "purpose": "Agent rewritten purpose",
                "current_focus": "Something",
                # source intentionally absent — models that don't carry it end up null
            }
        },
        "2026-07-04T01:00:00",
        baseline_raw=None,
    )
    assert cycle1["intent"].get("source") is None, (
        "agent rewrite drops provenance — precondition for the regression"
    )

    # Corrupted state: simulate the exact live incident — purpose blanked,
    # source=null, user_purpose has the real text.
    corrupted = dict(cycle1)
    corrupted["intent"] = dict(cycle1["intent"])
    corrupted["intent"]["purpose"] = ""
    corrupted["intent"]["source"] = None
    # user_purpose is the durable backstop — must still have the operator value.
    assert corrupted["intent"]["user_purpose"] == "Operator real purpose"

    # Agent cycle 2: proposes anything; the auto-heal must fire regardless.
    cycle2, _ = apply_agent_update(
        corrupted,
        {
            "intent": {
                "purpose": "",  # blank — the thinker-gate would reject this
                "current_focus": "New focus",
            }
        },
        "2026-07-04T02:00:00",
        baseline_raw=None,
    )
    assert cycle2["intent"]["purpose"] == "Operator real purpose", (
        "intent.purpose must be restored from user_purpose when blank and source=null"
    )


# ---------------------------------------------------------------------------
# apply_agent_update — non-destructive merge of AGENT-OWNED collections
#
# capabilities/gaps/friction/pending advance freely each cycle, but the agent
# often acts on a trimmed or dropped payload view. These tests pin that a write
# can only ADD or MODIFY those collections — never silently wipe an entry the
# agent didn't echo back. Removal is explicit via _deletions. (Operator-owned
# fields — purpose/context/constraints — have their own merge above.)
# ---------------------------------------------------------------------------


def test_merge_preserves_omitted_capabilities():
    """An agent that omits a capability (didn't see it) must not delete it."""
    current = {
        "revision": 1,
        "capabilities": [
            {"name": "CapA", "status": "working"},
            {"name": "CapB", "status": "working"},
        ],
    }
    proposed = {"capabilities": [{"name": "CapA", "status": "partial"}]}
    new_raw, _ = apply_agent_update(current, proposed, "2026-06-03T00:00:00")
    by_name = {c["name"]: c["status"] for c in new_raw["capabilities"]}
    assert by_name == {"CapA": "partial", "CapB": "working"}


def test_merge_preserves_omitted_gaps_and_friction():
    current = {
        "revision": 1,
        "gaps": [
            {"id": "g-seen", "name": "seen", "status": "active"},
            {"id": "g-unseen", "name": "trimmed away", "status": "active"},
        ],
        "friction": [
            {"id": "f-seen", "name": "seen"},
            {"id": "f-unseen", "name": "trimmed away"},
        ],
    }
    # Agent only echoes back the gap/friction it saw.
    proposed = {
        "gaps": [{"id": "g-seen", "name": "seen", "status": "active"}],
        "friction": [{"id": "f-seen", "name": "seen", "status": "active"}],
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-06-03T00:00:00")
    assert {g["id"] for g in new_raw["gaps"]} == {"g-seen", "g-unseen"}
    assert {f["id"] for f in new_raw["friction"]} == {"f-seen", "f-unseen"}


def test_merge_preserves_omitted_pending_confirmation():
    current = {
        "revision": 1,
        "pending_user_confirmation": [
            {"id": "p-creds", "type": "credentials_needed", "proposal": "need a key"}
        ],
    }
    proposed: dict = {"gaps": []}  # agent wrote without the pending queue
    new_raw, _ = apply_agent_update(current, proposed, "2026-06-03T00:00:00")
    assert {p["id"] for p in new_raw["pending_user_confirmation"]} == {"p-creds"}


def test_merge_unions_gap_history_so_trimmed_tail_does_not_truncate():
    """The payload trims per-gap history to the last N. If the agent echoes
    that tail back, the full history must survive (union), and any new event
    the agent added is appended."""
    full_history = [
        {"ts": f"2026-05-{d:02d}T00:00:00", "event": "opened"} for d in range(1, 9)
    ]
    current = {
        "revision": 1,
        "gaps": [
            {"id": "g1", "name": "x", "status": "active", "history": full_history}
        ],
    }
    # Agent saw only the last 3 entries (trimmed) and adds one new event.
    proposed_history = [
        *full_history[-3:],
        {"ts": "2026-05-09T00:00:00", "event": "priority_raised"},
    ]
    proposed = {
        "gaps": [
            {"id": "g1", "name": "x", "status": "active", "history": proposed_history}
        ]
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-06-03T00:00:00")
    merged_history = new_raw["gaps"][0]["history"]
    # All 8 original + 1 new, none duplicated.
    assert len(merged_history) == 9
    assert merged_history[-1]["event"] == "priority_raised"
    assert [h["ts"] for h in merged_history[:8]] == [h["ts"] for h in full_history]


def test_merge_unions_gap_past_moves():
    current = {
        "revision": 1,
        "gaps": [
            {
                "id": "g1",
                "name": "x",
                "status": "active",
                "past_moves": ["d-1", "d-2", "d-3"],
            }
        ],
    }
    # Agent saw only the recent tail and attached a new move.
    proposed = {
        "gaps": [
            {"id": "g1", "name": "x", "status": "active", "past_moves": ["d-3", "d-4"]}
        ]
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-06-03T00:00:00")
    assert new_raw["gaps"][0]["past_moves"] == ["d-1", "d-2", "d-3", "d-4"]


def test_explicit_deletions_remove_named_agent_owned_entries():
    current = {
        "revision": 1,
        "capabilities": [
            {"name": "CapA", "status": "working"},
            {"name": "CapStale", "status": "planned"},
        ],
        "gaps": [
            {"id": "g-keep", "name": "k", "status": "active"},
            {"id": "g-wrong", "name": "w", "status": "active"},
        ],
        "friction": [{"id": "f-gone", "name": "obsolete"}],
    }
    proposed = {
        "_deletions": {
            "capabilities": ["CapStale"],
            "gaps": ["g-wrong"],
            "friction": ["f-gone"],
        }
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-06-03T00:00:00")
    assert {c["name"] for c in new_raw["capabilities"]} == {"CapA"}
    assert {g["id"] for g in new_raw["gaps"]} == {"g-keep"}
    assert new_raw["friction"] == []
    # Deletions surface in the structured change log.
    assert diff.get("gaps_deleted") == ["g-wrong"]
    assert diff.get("capabilities_deleted") == ["CapStale"]
    assert diff.get("friction_deleted") == ["f-gone"]


def test_pending_user_confirmation_deletion_is_auditable_in_diff():
    """A strategist clearing a pending confirmation via _deletions must be
    auditable — the deletion surfaces in the structured revision-log diff so it
    is not an invisible mutation (Stage 2 lifecycle, design d-7c851b3f)."""
    current = {
        "revision": 1,
        "pending_user_confirmation": [
            {
                "id": "p-creds",
                "type": "credentials_needed",
                "proposal": "need a token",
                "created_at": "2026-06-01T00:00:00",
            },
            {
                "id": "p-keep",
                "type": "access_needed",
                "proposal": "keep me",
                "created_at": "2026-06-01T00:00:00",
            },
        ],
    }
    proposed = {"_deletions": {"pending_user_confirmation": ["p-creds"]}}
    new_raw, diff = apply_agent_update(current, proposed, "2026-06-03T00:00:00")
    assert {p["id"] for p in new_raw["pending_user_confirmation"]} == {"p-keep"}
    assert diff.get("pending_user_confirmation_deleted") == ["p-creds"]


def test_deletions_removes_archived_gap_by_id():
    current = {
        "revision": 1,
        "archived_gaps": [{"id": "g-old", "name": "old", "status": "closed"}],
    }
    proposed = {"_deletions": {"gaps": ["g-old"]}}
    new_raw, _ = apply_agent_update(current, proposed, "2026-06-03T00:00:00")
    assert new_raw["archived_gaps"] == []


def test_deletions_tolerates_junk_shape():
    """A malformed _deletions must not crash the update — it's just ignored."""
    current = {"revision": 1, "gaps": [{"id": "g1", "name": "x", "status": "active"}]}
    proposed = {"_deletions": "not-a-dict"}
    new_raw, _ = apply_agent_update(current, proposed, "2026-06-03T00:00:00")
    assert {g["id"] for g in new_raw["gaps"]} == {"g1"}


def test_deletions_field_not_persisted_in_model():
    """_deletions is a transient directive — it must never land in the saved
    model (which is extra='forbid')."""
    current = {"revision": 1}
    proposed = {
        "intent": {"purpose": "p"},
        "_deletions": {"gaps": ["nonexistent"]},
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-06-03T00:00:00")
    assert "_deletions" not in new_raw
    # Round-trips cleanly through the strict schema.
    parse_problem_model(new_raw)


def test_merge_close_by_status_still_archives():
    """Status-based close (the normal lifecycle) still auto-archives under the
    merge path — closing is NOT a deletion."""
    current = {
        "revision": 1,
        "gaps": [{"id": "g-done", "name": "done", "status": "active"}],
    }
    proposed = {"gaps": [{"id": "g-done", "name": "done", "status": "closed"}]}
    new_raw, diff = apply_agent_update(current, proposed, "2026-06-03T00:00:00")
    assert new_raw["gaps"] == []
    assert {g["id"] for g in new_raw["archived_gaps"]} == {"g-done"}
    assert diff["gaps_closed"] == ["g-done"]


def test_merge_empty_proposed_preserves_agent_owned_collections():
    """The pathological case the fix exists for: the model was dropped from the
    payload entirely and the agent wrote a near-empty model. The agent-owned
    collections survive intact."""
    current = {
        "revision": 4,
        "capabilities": [{"name": "CapA", "status": "working"}],
        "gaps": [{"id": "g1", "name": "x", "status": "active"}],
        "friction": [{"id": "f1", "name": "y"}],
        "pending_user_confirmation": [
            {"id": "p1", "type": "access_needed", "proposal": "grant access"}
        ],
    }
    proposed: dict = {}
    new_raw, _ = apply_agent_update(current, proposed, "2026-06-03T00:00:00")
    assert new_raw["revision"] == 5
    assert {c["name"] for c in new_raw["capabilities"]} == {"CapA"}
    assert {g["id"] for g in new_raw["gaps"]} == {"g1"}
    assert {f["id"] for f in new_raw["friction"]} == {"f1"}
    assert {p["id"] for p in new_raw["pending_user_confirmation"]} == {"p1"}


def test_friction_status_monotonic_stale_echo_does_not_regress():
    """Regression: a stale concurrent write echoing a friction entry with status
    'active' must NOT regress a terminal status (resolved or mitigated) back to
    'active'.  Reproduces the revs-1332-1341 stale-echo sequence: resolved
    tracker regressed to active by a concurrent write, then re-resolved.

    Three sub-scenarios:
      1. Resolved → stale active → still resolved (monotonic).
      2. Mitigated → stale active → still mitigated (monotonic).
      3. Active → resolved → works (forward progress not blocked)."""
    # --- Sub-scenario 1: resolved is not regressed ---
    current = {
        "revision": 10,
        "friction": [
            {
                "id": "f-1",
                "name": "flaky CI",
                "status": "resolved",
                "description": "Fixed by retry logic.",
                "mitigations_tried": ["added retry", "increased timeout"],
            }
        ],
    }
    proposed = {
        "friction": [
            {
                "id": "f-1",
                "name": "flaky CI",
                "status": "active",
            }
        ],
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-07-17T12:00:00")
    fr = new_raw["friction"][0]
    assert fr["status"] == "resolved", (
        f"Expected 'resolved' (monotonic), got '{fr['status']}'"
    )
    # Durable fields the stale echo omitted (description, mitigations_tried)
    # must still be preserved when the floor is NOT applied — a stale echo
    # just omits the field, which is NOT the same as a blank under floor marker.
    assert fr["name"] == "flaky CI"
    assert fr["id"] == "f-1"

    # --- Sub-scenario 2: mitigated is not regressed ---
    current2 = {
        "revision": 11,
        "friction": [
            {
                "id": "f-2",
                "name": "slow builds",
                "status": "mitigated",
                "description": "Build cache reduced time by 40%.",
            }
        ],
    }
    proposed2 = {
        "friction": [
            {
                "id": "f-2",
                "name": "slow builds",
                "status": "active",
            }
        ],
    }
    new_raw2, _ = apply_agent_update(current2, proposed2, "2026-07-17T12:00:00")
    fr2 = new_raw2["friction"][0]
    assert fr2["status"] == "mitigated", (
        f"Expected 'mitigated' (monotonic), got '{fr2['status']}'"
    )

    # --- Sub-scenario 3: forward progress from active still works ---
    current3 = {
        "revision": 12,
        "friction": [
            {
                "id": "f-3",
                "name": "flakey test",
                "status": "active",
            }
        ],
    }
    proposed3 = {
        "friction": [
            {
                "id": "f-3",
                "name": "flakey test",
                "status": "resolved",
            }
        ],
    }
    new_raw3, _ = apply_agent_update(current3, proposed3, "2026-07-17T12:00:00")
    fr3 = new_raw3["friction"][0]
    assert fr3["status"] == "resolved", (
        f"Expected 'resolved' (forward progress), got '{fr3['status']}'"
    )


# ---------------------------------------------------------------------------
# Strategic-payload integration — smoke tests
# ---------------------------------------------------------------------------


def test_strategic_review_payload_accepts_problem_model():
    """Schema allows problem_model on the strategic_review payload."""
    from app.payload_schemas import StrategicReviewPayload

    payload = StrategicReviewPayload.model_validate(
        {
            "vision": {"purpose": "x"},
            "problem_model": {"intent": {"purpose": "x"}},
        }
    )
    assert payload.problem_model == {"intent": {"purpose": "x"}}


def test_design_doc_update_payload_accepts_problem_model():
    from app.payload_schemas import DesignDocUpdatePayload

    payload = DesignDocUpdatePayload.model_validate(
        {
            "problem_model": {"intent": {"purpose": "x"}},
        }
    )
    assert payload.problem_model == {"intent": {"purpose": "x"}}


def test_strategic_payload_accepts_running_update_fields():
    """Payload must transport running_update (markdown doc) + acknowledgement
    + last-user timestamps from spawn to the agent."""
    from app.payload_schemas import StrategicReviewPayload

    payload = StrategicReviewPayload.model_validate(
        {
            "running_update": "## What's going on\nShipping problem_model v1.",
            "running_update_acknowledged_at": "2026-04-20T00:00:00",
            "user_last_active_at": "2026-04-20T12:00:00",
            "self_reflection_log": [
                {"ts": "2026-04-19T00:00:00", "what_was_hard": "y"}
            ],
        }
    )
    assert payload.running_update.startswith("## What's going on")
    assert payload.running_update_acknowledged_at == "2026-04-20T00:00:00"
    assert payload.user_last_active_at == "2026-04-20T12:00:00"
    assert len(payload.self_reflection_log) == 1


# ---------------------------------------------------------------------------
# Gap source binding — cross-cycle continuity for user asks
# ---------------------------------------------------------------------------


def test_gap_accepts_user_message_source():
    """When a user message drives a gap, the binding persists so subsequent
    cycles can report status on the original ask."""
    gap = Gap(
        id="g-feature-x",
        name="Feature X",
        source=Source(
            type="user_message",
            message_id="msg-42",
            excerpt="Can we add feature X? It would help with...",
        ),
    )
    assert gap.source is not None
    assert gap.source.type == "user_message"
    assert gap.source.message_id == "msg-42"


def test_gap_source_is_optional():
    gap = Gap(id="g-x", name="x")
    assert gap.source is None


def test_gap_source_type_validated():
    with pytest.raises(ValidationError):
        Source(type="mystery")  # invalid type


def test_gap_accepts_friction_promotion_source():
    gap = Gap(
        id="g-ci-reliability",
        name="CI reliability",
        source=Source(type="friction_promotion", friction_id="f-flaky"),
    )
    assert gap.source.type == "friction_promotion"
    assert gap.source.friction_id == "f-flaky"


def test_gap_accepts_self_improvement_tag():
    """Self-improvement gaps are tagged for filtering/reporting. No behavior
    change; the tag is just metadata."""
    gap = Gap(
        id="g-better-recall",
        name="Strategic agent needs cross-cycle memory",
        tag="self_improvement",
        source=Source(
            type="self_reflection",
            excerpt="Couldn't distinguish two near-duplicate directions.",
        ),
    )
    assert gap.tag == "self_improvement"
    assert gap.source.type == "self_reflection"


def test_gap_round_trip_with_source_preserves_binding():
    model = ProblemModel(
        gaps=[
            Gap(
                id="g-x",
                name="x",
                source=Source(type="user_message", message_id="m-1", excerpt="hi"),
            )
        ]
    )
    dumped = model.model_dump()
    roundtrip = ProblemModel.model_validate(dumped)
    assert roundtrip.gaps[0].source.message_id == "m-1"
    assert roundtrip.gaps[0].source.type == "user_message"


def test_self_reflection_entry_parses_with_empty_fields():
    """Self-reflection is a diary; fields can be empty strings."""
    entry = SelfReflectionEntry(ts="2026-04-21T12:00:00")
    assert entry.what_was_hard == ""
    assert entry.missing_context == ""
    assert entry.proposed_self_improvement == ""
    assert entry.cycle_task_id is None


def test_append_self_reflection_enforces_cap():
    log_raw: list = []
    for i in range(SELF_REFLECTION_LOG_MAX + 5):
        log_raw = append_self_reflection(
            log_raw,
            SelfReflectionEntry(
                ts=f"2026-04-{i:02d}T00:00:00",
                what_was_hard=f"struggle {i}",
            ),
        )
    assert len(log_raw) == SELF_REFLECTION_LOG_MAX
    # Oldest dropped, newest retained
    kept_struggles = [e["what_was_hard"] for e in log_raw]
    assert kept_struggles[0] == f"struggle {5}"
    assert kept_struggles[-1] == f"struggle {SELF_REFLECTION_LOG_MAX + 4}"


def test_append_self_reflection_handles_none_input():
    entry = SelfReflectionEntry(ts="2026-04-21T00:00:00", what_was_hard="x")
    log = append_self_reflection(None, entry)
    assert len(log) == 1
    assert log[0]["what_was_hard"] == "x"


# Removed: RunningUpdateEntry struct, append_running_update(), RUNNING_UPDATE_MAX.
# running_update is now a single markdown document (Optional[str]) the strategic
# agent rewrites each cycle — no per-cycle entry schema, no append semantics,
# no cap to enforce. See test_strategic_payload_accepts_running_update_fields
# for the current payload shape.


def test_project_append_routine_digest_creates_entry():
    """Project.append_routine_digest writes a formatted digest line to running_update."""
    from app.models import Project

    p = Project(name="test", user_id="u1")
    assert p.running_update is None
    p.append_routine_digest("pr_merged", "PR #42 merged into main")
    assert p.running_update is not None
    assert "pr_merged" in p.running_update
    assert "PR #42 merged into main" in p.running_update


def test_project_append_routine_digest_appends_additional_lines():
    """Each call appends a new line; existing running_update content is preserved."""
    from app.models import Project

    p = Project(name="test", user_id="u1")
    p.append_routine_digest("pr_merged", "PR #1 merged")
    p.append_routine_digest("auto_fix_result", "Fix passed CI")
    lines = p.running_update.splitlines()
    assert len(lines) == 2
    assert "pr_merged" in lines[0]
    assert "auto_fix_result" in lines[1]


def test_append_routine_digest_collapses_multiline_summary():
    """A multi-line summary collapses to a single physical line matching the
    canonical digest shape, so the reconciliation classifier never misreads the
    append as an operator body edit."""
    from app.models import Project
    from app.running_update_merge import _DIGEST_LINE_RE

    p = Project(name="test", user_id="u1")
    multiline = "Line one\r\nLine two\nLine three\rLine four\n\n  extra  spaces"
    p.append_routine_digest("operator_message", multiline)

    lines = p.running_update.splitlines()
    assert len(lines) == 1
    assert _DIGEST_LINE_RE.match(lines[0])
    # All newline-separated fragments survive on the single collapsed line.
    assert "Line one Line two Line three Line four extra spaces" in lines[0]
    assert "\n" not in p.running_update
    assert "\r" not in p.running_update


def test_append_routine_digest_truncates_after_collapse():
    """Truncation to 200 chars applies after whitespace collapse, and the result
    remains a single line."""
    from app.models import Project

    p = Project(name="test", user_id="u1")
    summary = "word\n" * 200  # 1000 chars of newline-separated tokens
    p.append_routine_digest("auto_fix_result", summary)

    lines = p.running_update.splitlines()
    assert len(lines) == 1
    # The summary portion after the "**: " prefix is capped at 200 chars.
    body = lines[0].split("**: ", 1)[1]
    assert len(body) <= 200


def test_source_is_reusable_across_model_elements():
    """Source is a generic citation — the same type applies on Gap,
    Intent, Capability, Friction."""
    intent = Intent(
        purpose="Build X.",
        source=Source(type="user_message", message_id="m-1"),
    )
    cap = Capability(
        name="Feature X",
        status="working",
        source=Source(type="shipped_change", pr_number=42),
    )
    friction = Friction(
        id="f-y",
        name="Something flaky",
        source=Source(type="direction_outcome", direction_id="d-7"),
    )
    gap = Gap(
        id="g-z",
        name="Gap",
        source=Source(type="self_reflection", excerpt="noticed recurring pattern"),
    )
    assert intent.source.type == "user_message"
    assert cap.source.pr_number == 42
    assert friction.source.direction_id == "d-7"
    assert gap.source.type == "self_reflection"


# Gap.deferral — acknowledged intentional non-staffing
# ---------------------------------------------------------------------------


def test_gap_deferral_defaults_none_and_absent_in_dump():
    """A gap without a deferral parses fine and serializes with deferral=None —
    every stored JSONB model and older agent payload validates unchanged."""
    gap = Gap(id="g-1", name="invoicing")
    assert gap.deferral is None
    assert gap.model_dump()["deferral"] is None


def test_gap_deferral_validates_and_round_trips():
    """An explicit deferral validates and survives a model_dump round-trip."""
    gap = Gap(
        id="g-1",
        name="invoicing",
        deferral=GapDeferral(
            rationale="Not staffed this cycle — waiting on v6 to open.",
            hold_until="v6",
            created_at="2026-06-04T00:00:00",
        ),
    )
    dumped = gap.model_dump()
    assert dumped["deferral"]["rationale"].startswith("Not staffed")
    assert dumped["deferral"]["hold_until"] == "v6"
    reparsed = Gap.model_validate(dumped)
    assert reparsed.deferral is not None
    assert reparsed.deferral.hold_until == "v6"


def test_gap_deferral_defaults_within_model():
    """GapDeferral fields are all optional/defaulted so a minimal deferral
    (rationale only) is valid."""
    deferral = GapDeferral(rationale="deferred")
    assert deferral.hold_until is None
    assert deferral.created_at == ""


def test_gap_deferral_survives_apply_agent_update_merge():
    """A deferral set by the agent threads through apply_agent_update's
    non-destructive merge (proposed-wins for non-log gap fields)."""
    current = {
        "revision": 3,
        "gaps": [{"id": "g-1", "name": "invoicing", "status": "active"}],
    }
    proposed = {
        "gaps": [
            {
                "id": "g-1",
                "name": "invoicing",
                "status": "active",
                "deferral": {
                    "rationale": "intentionally parked this cycle",
                    "hold_until": "next-version",
                },
            }
        ]
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-06-04T12:00:00")
    g = next(g for g in new_raw["gaps"] if g["id"] == "g-1")
    assert g["deferral"]["rationale"] == "intentionally parked this cycle"
    assert g["deferral"]["hold_until"] == "next-version"


def test_gap_deferral_cleared_by_agent_via_merge():
    """Clearing the deferral (proposed deferral=None) wins over a prior
    deferral — the agent picks the gap back up by writing deferral: null."""
    current = {
        "revision": 4,
        "gaps": [
            {
                "id": "g-1",
                "name": "invoicing",
                "status": "active",
                "deferral": {"rationale": "parked", "hold_until": "later"},
            }
        ],
    }
    proposed = {
        "gaps": [
            {"id": "g-1", "name": "invoicing", "status": "active", "deferral": None}
        ]
    }
    new_raw, _ = apply_agent_update(current, proposed, "2026-06-04T12:00:00")
    g = next(g for g in new_raw["gaps"] if g["id"] == "g-1")
    assert g["deferral"] is None


# ---------------------------------------------------------------------------
# Model size / health signal
# ---------------------------------------------------------------------------


def _gaps(n: int) -> list[Gap]:
    return [Gap(id=f"g-{i}", name=f"gap {i}") for i in range(n)]


def _caps(n: int) -> list[Capability]:
    return [Capability(name=f"cap-{i}", status="working") for i in range(n)]


def _friction(n_active: int, n_other: int = 0) -> list[Friction]:
    out = [Friction(id=f"f-a{i}", name=f"fa{i}", status="active") for i in range(n_active)]
    out += [Friction(id=f"f-r{i}", name=f"fr{i}", status="resolved") for i in range(n_other)]
    return out


def test_model_size_signal_empty_is_healthy():
    sig = compute_model_size_signal(ProblemModel())
    assert sig["active_gap_count"] == 0
    assert sig["capability_count"] == 0
    assert sig["active_friction_count"] == 0
    assert sig["estimated_token_footprint"] == 0
    assert sig["model_size_status"] == "healthy"


def test_model_size_signal_excludes_archived_gaps():
    model = ProblemModel(
        gaps=_gaps(3),
        archived_gaps=_gaps(50),
    )
    sig = compute_model_size_signal(model)
    # archived_gaps must NOT inflate the active gap count or push the status up.
    assert sig["active_gap_count"] == 3
    assert sig["model_size_status"] == "healthy"


def test_model_size_signal_excludes_blocked_gaps():
    """Blocked gaps are intentionally parked work, not active load — they must not
    inflate the active_gap_count or drive the status toward large/oversized."""
    active = [Gap(id=f"g-active-{i}", name=f"active {i}", status="active") for i in range(3)]
    blocked = [Gap(id=f"g-blocked-{i}", name=f"blocked {i}", status="blocked") for i in range(10)]
    model = ProblemModel(gaps=active + blocked)
    sig = compute_model_size_signal(model)
    # Only the 3 active gaps count; the 10 blocked are excluded.
    assert sig["active_gap_count"] == 3
    # Status should be healthy based on 3 active gaps, not large based on 13 total.
    assert sig["model_size_status"] == "healthy"


def test_model_size_signal_counts_only_active_friction():
    model = ProblemModel(friction=_friction(n_active=2, n_other=40))
    sig = compute_model_size_signal(model)
    assert sig["active_friction_count"] == 2
    assert sig["model_size_status"] == "healthy"


def test_model_size_signal_gap_dimension_transitions():
    h = MODEL_SIZE_HEALTHY_GAPS  # 12
    # at threshold -> still healthy
    assert compute_model_size_signal(ProblemModel(gaps=_gaps(h)))["model_size_status"] == "healthy"
    # 1.5x -> growing
    assert (
        compute_model_size_signal(ProblemModel(gaps=_gaps(int(h * 1.5))))["model_size_status"]
        == "growing"
    )
    # 2x -> large
    assert (
        compute_model_size_signal(ProblemModel(gaps=_gaps(h * 2)))["model_size_status"] == "large"
    )
    # 3x -> oversized
    assert (
        compute_model_size_signal(ProblemModel(gaps=_gaps(h * 3)))["model_size_status"]
        == "oversized"
    )


def test_model_size_signal_capability_dimension_transitions():
    h = MODEL_SIZE_HEALTHY_CAPABILITIES  # 20
    assert compute_model_size_signal(ProblemModel(capabilities=_caps(h)))["model_size_status"] == "healthy"
    assert compute_model_size_signal(ProblemModel(capabilities=_caps(int(h * 1.5))))["model_size_status"] == "growing"
    assert compute_model_size_signal(ProblemModel(capabilities=_caps(h * 2)))["model_size_status"] == "large"
    assert compute_model_size_signal(ProblemModel(capabilities=_caps(h * 3)))["model_size_status"] == "oversized"


def test_model_size_signal_friction_dimension_transitions():
    h = MODEL_SIZE_HEALTHY_FRICTION  # 8
    assert compute_model_size_signal(ProblemModel(friction=_friction(h)))["model_size_status"] == "healthy"
    assert compute_model_size_signal(ProblemModel(friction=_friction(int(h * 1.5))))["model_size_status"] == "growing"
    assert compute_model_size_signal(ProblemModel(friction=_friction(h * 2)))["model_size_status"] == "large"
    assert compute_model_size_signal(ProblemModel(friction=_friction(h * 3)))["model_size_status"] == "oversized"


def test_model_size_signal_worst_dimension_wins():
    # gaps healthy, capabilities healthy, friction oversized -> overall oversized
    model = ProblemModel(
        gaps=_gaps(2),
        capabilities=_caps(2),
        friction=_friction(MODEL_SIZE_HEALTHY_FRICTION * 3),
    )
    assert compute_model_size_signal(model)["model_size_status"] == "oversized"


def test_model_size_signal_footprint_positive_and_monotonic():
    small = compute_model_size_signal(ProblemModel(gaps=_gaps(2)))["estimated_token_footprint"]
    big = compute_model_size_signal(ProblemModel(gaps=_gaps(10)))["estimated_token_footprint"]
    assert small > 0
    assert big > small


def test_model_size_signal_footprint_includes_text_content():
    terse_gap = Gap(id="g-terse", name="x", description="brief", evidence="")
    verbose_gap = Gap(
        id="g-verbose",
        name="x",
        description="This is a detailed description with lots of context about the gap. " * 5,
        evidence="Additional evidence and supporting details that provide substantive information. " * 5,
    )
    terse_model = ProblemModel(gaps=[terse_gap])
    verbose_model = ProblemModel(gaps=[verbose_gap])

    terse_footprint = compute_model_size_signal(terse_model)["estimated_token_footprint"]
    verbose_footprint = compute_model_size_signal(verbose_model)["estimated_token_footprint"]
    # Same gap count (1), but verbose has much more text — footprint must reflect it.
    assert verbose_footprint > terse_footprint


def test_model_size_signal_footprint_includes_capability_notes():
    terse_cap = Capability(name="cap-terse", status="working", notes="brief")
    verbose_cap = Capability(
        name="cap-verbose",
        status="working",
        notes="Detailed notes about this capability including implementation details "
        "and architectural considerations. " * 5,
    )
    terse_model = ProblemModel(capabilities=[terse_cap])
    verbose_model = ProblemModel(capabilities=[verbose_cap])

    terse_footprint = compute_model_size_signal(terse_model)["estimated_token_footprint"]
    verbose_footprint = compute_model_size_signal(verbose_model)["estimated_token_footprint"]
    assert verbose_footprint > terse_footprint


def test_model_size_signal_footprint_includes_friction_description():
    terse_friction = Friction(id="f-terse", name="x", description="brief")
    verbose_friction = Friction(
        id="f-verbose",
        name="x",
        description="Detailed friction description explaining the blocker and its impact "
        "on the project. " * 5,
    )
    terse_model = ProblemModel(friction=[terse_friction])
    verbose_model = ProblemModel(friction=[verbose_friction])

    terse_footprint = compute_model_size_signal(terse_model)["estimated_token_footprint"]
    verbose_footprint = compute_model_size_signal(verbose_model)["estimated_token_footprint"]
    assert verbose_footprint > terse_footprint


def test_model_size_signal_includes_serialized_bytes():
    """The signal now includes the actual serialized byte size of the model."""
    model = ProblemModel(gaps=_gaps(2))
    sig = compute_model_size_signal(model)
    assert "serialized_bytes" in sig
    assert isinstance(sig["serialized_bytes"], int)
    assert sig["serialized_bytes"] > 0


def test_model_size_signal_byte_aware_small_model_healthy():
    """A small model with few bytes and few entries is healthy."""
    model = ProblemModel(gaps=_gaps(2))
    sig = compute_model_size_signal(model)
    assert sig["serialized_bytes"] < MODEL_SIZE_HEALTHY_BYTES
    assert sig["model_size_status"] == "healthy"


def test_model_size_signal_byte_aware_large_verbose_model_triggers_status():
    """A model that is byte-heavy but count-light (verbose entries) is classified
    as large/oversized by the byte dimension, triggering worst-dimension-wins even
    when entry counts look healthy."""
    # Create gaps with verbose descriptions that exceed the byte threshold
    # while keeping the count well under the healthy threshold.
    # Use larger descriptions to reliably push past the healthy byte threshold.
    big_description = "x" * 20_000
    gaps = [
        Gap(
            id=f"g-{i}",
            name=f"gap {i}",
            description=big_description,
            evidence=big_description,
            status="active",
        )
        for i in range(3)  # Only 3 gaps (well under healthy count of 12)
    ]
    model = ProblemModel(gaps=gaps)
    sig = compute_model_size_signal(model)

    # Entry counts are healthy, but bytes push it to large/oversized.
    assert sig["active_gap_count"] == 3  # Under healthy threshold of 12
    assert sig["serialized_bytes"] > MODEL_SIZE_HEALTHY_BYTES
    # Worst-dimension-wins: byte dimension is growing/large/oversized,
    # so overall status reflects the byte pressure
    assert sig["model_size_status"] in ("growing", "large", "oversized")


def test_model_size_signal_byte_dimension_is_part_of_worst_wins():
    """The byte dimension is included in worst-dimension-wins status calculation."""
    # Verify a model where all dimensions are healthy would stay healthy
    small = ProblemModel(gaps=_gaps(2), capabilities=_caps(2), friction=_friction(2))
    sig_small = compute_model_size_signal(small)
    assert sig_small["active_gap_count"] <= MODEL_SIZE_HEALTHY_GAPS
    assert sig_small["capability_count"] <= MODEL_SIZE_HEALTHY_CAPABILITIES
    assert sig_small["active_friction_count"] <= MODEL_SIZE_HEALTHY_FRICTION
    assert sig_small["serialized_bytes"] < MODEL_SIZE_HEALTHY_BYTES
    assert sig_small["model_size_status"] == "healthy"

    # Verify a model with many gaps triggers worst-dimension-wins
    many_gaps = ProblemModel(gaps=_gaps(MODEL_SIZE_HEALTHY_GAPS * 3))
    sig_many = compute_model_size_signal(many_gaps)
    # Gap count dominates: 36 gaps >> 12 healthy threshold
    assert sig_many["model_size_status"] == "oversized"
    # Byte information is always present
    assert "serialized_bytes" in sig_many
    assert sig_many["serialized_bytes"] > 0


# ---------------------------------------------------------------------------
# Alias-aware capability consolidation
# ---------------------------------------------------------------------------


def test_capability_absorbs_defaults_empty_and_old_payload_validates():
    """A capability with no ``absorbs`` parses with an empty list, so every
    stored model and older agent payload remains valid."""
    cap = Capability(name="ci-pipeline", status="working")
    assert cap.absorbs == []
    # Old-shape payload (no absorbs key) validates unchanged.
    restored = Capability.model_validate(
        {"name": "ci-pipeline", "status": "working"}
    )
    assert restored.absorbs == []


def test_capability_consolidation_resolves_absorbed_prereq():
    """End-to-end (the exact pair the blocked-resume matcher uses): a
    consolidation write — ``_deletions.capabilities`` removing several old names
    plus a working survivor that declares them in ``absorbs`` — makes each old
    name resolve through ``working_capability_names`` +
    ``registry_satisfies_capability_need`` ONLY when the alias relationship is
    PLATFORM-RECORDED via forwarding markers in ``proof_records``. An agent-declared
    ``absorbs`` list alone (without corresponding forwarding markers) does NOT
    cause absorbed names to resolve — closing the alias-forgery bypass."""
    from app.capability_registry import (
        CAPABILITY_ABSORPTION_FORWARD_PROOF_KIND,
        PLATFORM_WORKING_CAPABILITIES,
        registry_satisfies_capability_need,
        working_capability_names,
    )

    current = {
        "revision": 1,
        "capabilities": [
            {"name": "ci-poll", "status": "working"},
            {"name": "ci-resolve", "status": "working"},
            {"name": "ci-fix-spawn", "status": "working"},
        ],
    }
    # Strategist consolidates the three CI capabilities into one survivor that
    # absorbs them, deleting the old names in the same write.
    proposed = {
        "capabilities": [
            {
                "name": "ci-pipeline",
                "status": "working",
                "absorbs": ["ci-poll", "ci-resolve", "ci-fix-spawn"],
            },
        ],
        "_deletions": {"capabilities": ["ci-poll", "ci-resolve", "ci-fix-spawn"]},
    }
    new_raw, _diff = apply_agent_update(current, proposed, "2026-06-10T12:00:00")

    # The durable model now holds only the survivor.
    names = [c["name"] for c in new_raw["capabilities"]]
    assert names == ["ci-pipeline"]

    # Simulate the platform forwarding markers that a real consolidation would
    # leave via carry_proof_records_for_consolidation. Without these markers,
    # the absorbed names must NOT resolve through working_capability_names.
    proof_records: dict = {}
    for _absorbed in ("ci-poll", "ci-resolve", "ci-fix-spawn"):
        proof_records[_absorbed] = [
            {
                "proof_kind": CAPABILITY_ABSORPTION_FORWARD_PROOF_KIND,
                "survivor": "ci-pipeline",
                "task_id": f"consolidation:{_absorbed}",
                "proved_at": "2026-06-10T12:00:00",
            }
        ]

    # working_capability_names folds the PLATFORM-RECORDED absorbed aliases in
    # alongside the survivor's own name. It also ALWAYS includes the always-working
    # PLATFORM_WORKING_CAPABILITIES (foundation capabilities unrelated to this
    # consolidation), so the survivor + its three absorbed aliases are exactly
    # the project-model contribution on top of the platform set.
    working = working_capability_names(new_raw, proof_records=proof_records)
    assert working == {
        "ci-pipeline",
        "ci-poll",
        "ci-resolve",
        "ci-fix-spawn",
    } | PLATFORM_WORKING_CAPABILITIES

    # Each absorbed old name resolves as satisfied-by-the-codebase through the
    # exact helper pair the blocked-resume matcher uses.
    for old_name in ("ci-poll", "ci-resolve", "ci-fix-spawn"):
        assert registry_satisfies_capability_need(
            {"kind": "capability", "ref": old_name}, working
        )
    # The survivor's own name resolves too.
    assert registry_satisfies_capability_need(
        {"kind": "capability", "ref": "ci-pipeline"}, working
    )


def test_consolidation_shrinks_vocabulary_but_not_known_set():
    """The rendered ``registry_capability_vocabulary`` SHRINKS (absorbed names
    absent) while ``registry_capability_names`` (advisory known-set) still
    contains them — the two readers deliberately diverge."""
    from app.capability_registry import (
        registry_capability_names,
        registry_capability_vocabulary,
    )

    model = ProblemModel(
        capabilities=[
            Capability(
                name="ci-pipeline",
                status="working",
                absorbs=["ci-poll", "ci-resolve"],
            ),
            Capability(name="auth", status="working"),
        ]
    )

    vocab_names = [entry["name"] for entry in registry_capability_vocabulary(model)]
    # Vocabulary is survivor-only: absorbed names do NOT re-appear.
    assert vocab_names == ["ci-pipeline", "auth"]
    assert "ci-poll" not in vocab_names
    assert "ci-resolve" not in vocab_names

    # Advisory known-set folds the absorbed aliases back in.
    known = registry_capability_names(model)
    assert "ci-poll" in known
    assert "ci-resolve" in known
    assert "ci-pipeline" in known
    assert "auth" in known
    # The known-set is strictly larger than the vocabulary by exactly the
    # absorbed aliases.
    assert set(known) - set(vocab_names) == {"ci-poll", "ci-resolve"}


def test_consolidation_recorded_in_revision_log_diff():
    """A consolidation is recorded as ``capabilities_consolidated`` in the diff
    and surfaced in the revision-log summary."""
    current = {
        "revision": 4,
        "capabilities": [
            {"name": "ci-poll", "status": "working"},
            {"name": "ci-resolve", "status": "working"},
        ],
    }
    proposed = {
        "capabilities": [
            {
                "name": "ci-pipeline",
                "status": "working",
                "absorbs": ["ci-poll", "ci-resolve"],
            },
        ],
        "_deletions": {"capabilities": ["ci-poll", "ci-resolve"]},
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-06-10T12:00:00")

    assert diff["capabilities_consolidated"] == [
        {"survivor": "ci-pipeline", "absorbed": ["ci-poll", "ci-resolve"]}
    ]
    # And it is folded into the appended revision-log entry.
    last_entry = new_raw["revision_log"][-1]
    assert last_entry["changes"]["capabilities_consolidated"] == [
        {"survivor": "ci-pipeline", "absorbed": ["ci-poll", "ci-resolve"]}
    ]
    assert "consolidated 1 capability group(s)" in last_entry["summary"]


def test_no_consolidation_write_is_byte_identical():
    """A no-consolidation write is BYTE-IDENTICAL to today: the round-trip
    model_dump emits no ``absorbs`` key when every capability absorbed
    nothing."""
    current = {
        "revision": 2,
        "capabilities": [
            {"name": "auth", "status": "working", "notes": "stable"},
            {"name": "billing", "status": "partial"},
        ],
    }
    # A plain status-bump write that performs no consolidation.
    proposed = {
        "capabilities": [
            {"name": "auth", "status": "working", "notes": "stable"},
            {"name": "billing", "status": "working"},
        ],
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-06-10T12:00:00")

    # No capability dict carries an ``absorbs`` key — output is byte-identical
    # to the pre-field behavior.
    for cap in new_raw["capabilities"]:
        assert "absorbs" not in cap
    assert "capabilities_consolidated" not in diff
    # The whole serialized blob carries no ``absorbs`` token at all.
    import json as _json

    assert "absorbs" not in _json.dumps(new_raw)


def test_absorbed_name_not_flagged_off_registry():
    """An absorbed name is NOT flagged by ``off_registry_capability_refs`` when
    passed the alias-aware known-set."""
    from app.capability_registry import (
        off_registry_capability_refs,
        registry_capability_names,
    )

    model = ProblemModel(
        capabilities=[
            Capability(
                name="ci-pipeline",
                status="working",
                absorbs=["ci-poll", "ci-resolve"],
            ),
        ]
    )
    known = registry_capability_names(model)

    # A prerequisite still naming the absorbed old capability is treated as
    # known (not off-registry), exactly as before consolidation.
    flagged = off_registry_capability_refs(
        ["ci-poll", "Ci Resolve", "never-declared"], known
    )
    assert "ci-poll" not in flagged
    # Normalization still applies on the ref side.
    assert "Ci Resolve" not in flagged
    # A genuinely unknown ref is still flagged.
    assert flagged == ["never-declared"]


def test_absorbs_floor_field_not_wiped_by_skeleton_resubmit():
    """``absorbs`` is in _CAP_FLOOR_FIELDS, so a resubmitted skeleton capability
    (seen floored) does not silently wipe a durable ``absorbs`` list."""
    current = {
        "revision": 5,
        "capabilities": [
            {
                "name": "ci-pipeline",
                "status": "working",
                "absorbs": ["ci-poll", "ci-resolve"],
            },
        ],
    }
    # Agent saw a skeletonized capabilities section and echoes the floor marker
    # back; it resubmits only name+status (absorbs blank because it never saw it).
    proposed = {
        "capabilities": [{"name": "ci-pipeline", "status": "working"}],
        "_agent_view_floor_applied": {"summarized_capabilities": 1},
    }
    new_raw, _diff = apply_agent_update(current, proposed, "2026-06-10T12:00:00")
    assert new_raw["capabilities"][0]["absorbs"] == ["ci-poll", "ci-resolve"]


# ---------------------------------------------------------------------------
# Gap-history compaction watermark
# ---------------------------------------------------------------------------


def _gap_with_history(gap_id: str, events: list[dict]) -> dict:
    return {
        "id": gap_id,
        "name": f"gap {gap_id}",
        "history": events,
    }


def _evt(ts: str, event: str = "note") -> dict:
    return {"ts": ts, "event": event}


def test_watermark_prevents_stale_echo_resurrection():
    """Core invariant: entries at or before history_compacted_at are excluded
    from the write-back union — a trimmed echo cannot resurrect them."""
    old_evt = _evt("2026-04-01T00:00:00Z", "old-entry")
    new_evt = _evt("2026-06-01T00:00:00Z", "new-entry")

    # Durable store: gap has both old and new history entries
    current = {
        "revision": 1,
        "gaps": [_gap_with_history("g-1", [old_evt, new_evt])],
    }
    # Agent sets a compaction watermark that covers old_evt
    proposed = {
        "gaps": [_gap_with_history("g-1", [new_evt])],
        "_compactions": [
            {"gap_id": "g-1", "history_watermark": "2026-05-01T00:00:00Z"}
        ],
    }
    new_raw, _diff = apply_agent_update(current, proposed, "2026-06-10T12:00:00")
    gap_result = next(g for g in new_raw["gaps"] if g["id"] == "g-1")

    # The old entry is compacted out; only the new entry survives
    history_ts = [e["ts"] for e in gap_result["history"]]
    assert old_evt["ts"] not in history_ts
    assert new_evt["ts"] in history_ts
    # Watermark is persisted on the gap
    assert gap_result["history_compacted_at"] == "2026-05-01T00:00:00Z"


def test_watermark_prevents_resurrection_on_subsequent_write():
    """A second write without _compactions still excludes entries below the
    persistent watermark — the watermark survives the round-trip."""
    old_evt = _evt("2026-04-01T00:00:00Z", "old-entry")
    new_evt = _evt("2026-06-01T00:00:00Z", "new-entry")

    # First write: compact away old_evt
    current = {
        "revision": 1,
        "gaps": [_gap_with_history("g-1", [old_evt, new_evt])],
    }
    proposed_first = {
        "gaps": [_gap_with_history("g-1", [new_evt])],
        "_compactions": [
            {"gap_id": "g-1", "history_watermark": "2026-05-01T00:00:00Z"}
        ],
    }
    after_first, _ = apply_agent_update(current, proposed_first, "2026-06-10T10:00:00")

    # Second write: agent sends only new_evt (trimmed view); no _compactions
    proposed_second = {
        "revision": after_first["revision"],
        "gaps": [_gap_with_history("g-1", [new_evt])],
    }
    after_second, _ = apply_agent_update(after_first, proposed_second, "2026-06-10T11:00:00")
    gap_result = next(g for g in after_second["gaps"] if g["id"] == "g-1")

    # old_evt is still excluded because the persisted watermark gates it out
    history_ts = [e["ts"] for e in gap_result["history"]]
    assert old_evt["ts"] not in history_ts
    assert new_evt["ts"] in history_ts
    # Watermark must survive the write that omits it (durable / Defect 1)
    assert gap_result.get("history_compacted_at") == "2026-05-01T00:00:00Z"


def test_compaction_observable_in_revision_log():
    """Compaction events are recorded in the diff and the revision-log entry."""
    current = {
        "revision": 3,
        "gaps": [_gap_with_history("g-x", [_evt("2026-01-01T00:00:00Z", "old")])],
    }
    proposed = {
        "gaps": [_gap_with_history("g-x", [])],
        "_compactions": [
            {"gap_id": "g-x", "history_watermark": "2026-05-01T00:00:00Z"}
        ],
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-06-10T12:00:00")

    # Diff carries the event
    assert "gaps_history_compacted" in diff
    compacted_events = diff["gaps_history_compacted"]
    assert any(e["gap_id"] == "g-x" for e in compacted_events)

    # Revision log records it too
    last_log = new_raw["revision_log"][-1]
    assert "gaps_history_compacted" in last_log["changes"]


def test_no_op_without_compaction_directive():
    """No _compactions → output is byte-identical to pre-watermark behavior:
    no watermark keys on gaps, merge is unchanged."""
    evt = _evt("2026-04-01T00:00:00Z", "entry")
    current = {
        "revision": 1,
        "gaps": [_gap_with_history("g-1", [evt])],
    }
    proposed = {
        "gaps": [_gap_with_history("g-1", [evt])],
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-06-10T12:00:00")
    gap_result = next(g for g in new_raw["gaps"] if g["id"] == "g-1")

    # No watermark fields on output
    assert "history_compacted_at" not in gap_result
    assert "past_moves_compacted_at" not in gap_result
    assert "gaps_history_compacted" not in diff
    # History is unchanged (union of current + proposed)
    assert any(e["ts"] == evt["ts"] for e in gap_result["history"])


def test_watermark_on_trimmed_view():
    """Agent receives a 5-entry trimmed view and can compact it: entries below
    the watermark that ARE in the durable log are excluded, entries above are
    preserved."""
    # Durable: 10 history entries
    all_events = [_evt(f"2026-0{i + 1}-01T00:00:00Z", f"evt-{i}") for i in range(9)]
    all_events.append(_evt("2026-10-01T00:00:00Z", "evt-9"))
    current = {"revision": 1, "gaps": [_gap_with_history("g-trim", all_events)]}

    # Agent only sees last 5 (the trim); decides to compact everything before June
    watermark = "2026-05-31T23:59:59Z"
    agent_view = all_events[-5:]  # entries 5-9
    proposed = {
        "gaps": [_gap_with_history("g-trim", agent_view)],
        "_compactions": [
            {"gap_id": "g-trim", "history_watermark": watermark}
        ],
    }
    new_raw, _diff = apply_agent_update(current, proposed, "2026-06-10T12:00:00")
    gap_result = next(g for g in new_raw["gaps"] if g["id"] == "g-trim")
    result_ts = {e["ts"] for e in gap_result["history"]}

    # Entries with ts <= watermark are excluded
    for evt in all_events:
        if evt["ts"] <= watermark:
            assert evt["ts"] not in result_ts, f"{evt['ts']} should be compacted"
        else:
            assert evt["ts"] in result_ts, f"{evt['ts']} should survive"


def test_past_moves_compacted_at_persisted():
    """past_moves_compacted_at is stored on the gap for observability."""
    current = {
        "revision": 1,
        "gaps": [{"id": "g-1", "name": "g", "past_moves": ["d-old", "d-new"]}],
    }
    proposed = {
        "gaps": [{"id": "g-1", "name": "g", "past_moves": ["d-old", "d-new"]}],
        "_compactions": [
            {"gap_id": "g-1", "past_moves_watermark": "2026-05-01T00:00:00Z"}
        ],
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-06-10T12:00:00")
    gap_result = next(g for g in new_raw["gaps"] if g["id"] == "g-1")
    assert gap_result.get("past_moves_compacted_at") == "2026-05-01T00:00:00Z"
    assert any(e.get("past_moves_watermark") for e in diff.get("gaps_history_compacted", []))


def test_compaction_directive_unknown_gap_is_noop():
    """_compactions referencing a nonexistent gap_id is silently ignored."""
    current = {"revision": 1, "gaps": [{"id": "g-real", "name": "real"}]}
    proposed = {
        "gaps": [{"id": "g-real", "name": "real"}],
        "_compactions": [
            {"gap_id": "g-nonexistent", "history_watermark": "2026-05-01T00:00:00Z"}
        ],
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-06-10T12:00:00")
    # No events recorded, no crash
    assert "gaps_history_compacted" not in diff
    gap = next(g for g in new_raw["gaps"] if g["id"] == "g-real")
    assert "history_compacted_at" not in gap


def test_gap_watermark_fields_default_none_byte_identical():
    """apply_agent_update output strips None watermark fields from gaps so the
    stored bytes of clean models are identical to before the fields existed."""
    import json as _json

    model = Gap(id="g-1", name="test")
    assert model.history_compacted_at is None
    assert model.past_moves_compacted_at is None

    # Via apply_agent_update, the output should not carry the watermark keys
    current = {"revision": 1, "gaps": [{"id": "g-1", "name": "test"}]}
    proposed = {"gaps": [{"id": "g-1", "name": "test"}]}
    new_raw, _ = apply_agent_update(current, proposed, "2026-06-10T12:00:00")
    gap_out = next(g for g in new_raw["gaps"] if g["id"] == "g-1")
    dumped = _json.dumps(gap_out)
    assert "history_compacted_at" not in dumped
    assert "past_moves_compacted_at" not in dumped


# ---------------------------------------------------------------------------
# Defect 1: watermark wipe — durable watermark must survive writes that omit it
# ---------------------------------------------------------------------------


def test_watermark_durable_across_write_without_compaction_directive():
    """Watermark survives a write that omits _compactions entirely — the
    durable current watermark is carried forward monotonically (Defect 1)."""
    old_evt = _evt("2026-04-01T00:00:00Z", "old-entry")
    new_evt = _evt("2026-06-01T00:00:00Z", "new-entry")

    # First write: set watermark
    current = {"revision": 1, "gaps": [_gap_with_history("g-1", [old_evt, new_evt])]}
    proposed_first = {
        "gaps": [_gap_with_history("g-1", [new_evt])],
        "_compactions": [{"gap_id": "g-1", "history_watermark": "2026-05-01T00:00:00Z"}],
    }
    after_first, _ = apply_agent_update(current, proposed_first, "2026-06-10T10:00:00")
    assert after_first["gaps"][0]["history_compacted_at"] == "2026-05-01T00:00:00Z"

    # Second write: no _compactions, no watermark field echoed
    proposed_second = {"gaps": [_gap_with_history("g-1", [new_evt])]}
    after_second, _ = apply_agent_update(after_first, proposed_second, "2026-06-10T11:00:00")
    gap_result = next(g for g in after_second["gaps"] if g["id"] == "g-1")

    # Watermark must survive
    assert gap_result.get("history_compacted_at") == "2026-05-01T00:00:00Z"
    # old_evt still excluded (watermark is effective)
    assert old_evt["ts"] not in {e["ts"] for e in gap_result["history"]}


# ---------------------------------------------------------------------------
# Defect 2: stale-echo resurrection — proposed side must also be filtered
# ---------------------------------------------------------------------------


def test_watermark_two_sided_filters_proposed_history_echoes():
    """A stale echo that includes pre-compaction entries on the proposed side
    cannot resurrect them — BOTH current and proposed history are filtered
    before the union (Defect 2)."""
    old_evt = _evt("2026-04-01T00:00:00Z", "old-entry")
    new_evt = _evt("2026-06-01T00:00:00Z", "new-entry")

    # Compact old_evt
    current = {"revision": 1, "gaps": [_gap_with_history("g-1", [old_evt, new_evt])]}
    proposed_first = {
        "gaps": [_gap_with_history("g-1", [new_evt])],
        "_compactions": [{"gap_id": "g-1", "history_watermark": "2026-05-01T00:00:00Z"}],
    }
    after_first, _ = apply_agent_update(current, proposed_first, "2026-06-10T10:00:00")

    # Second write: agent echoes BOTH entries (stale view); no _compactions
    proposed_second = {
        "gaps": [_gap_with_history("g-1", [old_evt, new_evt])],  # old_evt echoed back!
    }
    after_second, _ = apply_agent_update(after_first, proposed_second, "2026-06-10T11:00:00")
    gap_result = next(g for g in after_second["gaps"] if g["id"] == "g-1")

    # old_evt must NOT be resurrected even though it was on the proposed side
    assert old_evt["ts"] not in {e["ts"] for e in gap_result["history"]}
    assert new_evt["ts"] in {e["ts"] for e in gap_result["history"]}


# ---------------------------------------------------------------------------
# Defect 3: monotonic guard — stale directive cannot regress watermark
# ---------------------------------------------------------------------------


def test_watermark_monotonic_stale_compaction_directive_cannot_regress():
    """A _compactions directive with an older watermark cannot regress a
    durable newer watermark (Defect 3 — monotonic guard in _apply_compactions)."""
    old_evt = _evt("2026-04-01T00:00:00Z", "old")
    new_evt = _evt("2026-06-01T00:00:00Z", "new")

    # First write: set watermark to 2026-05-01
    current = {"revision": 1, "gaps": [_gap_with_history("g-1", [old_evt, new_evt])]}
    proposed_first = {
        "gaps": [_gap_with_history("g-1", [new_evt])],
        "_compactions": [{"gap_id": "g-1", "history_watermark": "2026-05-01T00:00:00Z"}],
    }
    after_first, _ = apply_agent_update(current, proposed_first, "2026-06-10T10:00:00")

    # Second write: directive with OLDER watermark (stale replay)
    proposed_second = {
        "gaps": [_gap_with_history("g-1", [new_evt])],
        "_compactions": [{"gap_id": "g-1", "history_watermark": "2026-04-01T00:00:00Z"}],
    }
    after_second, _ = apply_agent_update(after_first, proposed_second, "2026-06-10T11:00:00")
    gap_result = next(g for g in after_second["gaps"] if g["id"] == "g-1")

    # Watermark must NOT have regressed
    assert gap_result["history_compacted_at"] == "2026-05-01T00:00:00Z"
    # old_evt still excluded
    assert old_evt["ts"] not in {e["ts"] for e in gap_result["history"]}


# ---------------------------------------------------------------------------
# past_moves: replacement semantics and durable watermark
# ---------------------------------------------------------------------------


def test_past_moves_replacement_semantics_on_directive_write():
    """When _compactions sets past_moves_watermark, proposed.past_moves replaces
    the durable log (not unioned) — allowing actual compaction of past_moves."""
    current = {
        "revision": 1,
        "gaps": [{"id": "g-1", "name": "g", "past_moves": ["d-old-1", "d-old-2", "d-new"]}],
    }
    proposed = {
        "gaps": [{"id": "g-1", "name": "g", "past_moves": ["d-new"]}],
        "_compactions": [{"gap_id": "g-1", "past_moves_watermark": "2026-05-01T00:00:00Z"}],
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-06-10T12:00:00")
    gap_result = next(g for g in new_raw["gaps"] if g["id"] == "g-1")

    # Replacement semantics: only the agent's trimmed list survives, not the union
    assert gap_result["past_moves"] == ["d-new"]
    assert "d-old-1" not in gap_result["past_moves"]
    assert "d-old-2" not in gap_result["past_moves"]
    assert gap_result.get("past_moves_compacted_at") == "2026-05-01T00:00:00Z"
    assert any(e.get("past_moves_watermark") for e in diff.get("gaps_history_compacted", []))


def test_past_moves_compacted_at_durable_across_write_without_directive():
    """past_moves_compacted_at survives a subsequent write that omits the
    directive — the watermark is monotonically carried forward."""
    current = {
        "revision": 1,
        "gaps": [{"id": "g-1", "name": "g", "past_moves": ["d-old", "d-new"]}],
    }
    proposed_first = {
        "gaps": [{"id": "g-1", "name": "g", "past_moves": ["d-new"]}],
        "_compactions": [{"gap_id": "g-1", "past_moves_watermark": "2026-05-01T00:00:00Z"}],
    }
    after_first, _ = apply_agent_update(current, proposed_first, "2026-06-10T10:00:00")
    assert after_first["gaps"][0].get("past_moves_compacted_at") == "2026-05-01T00:00:00Z"

    # Second write: no _compactions directive
    proposed_second = {
        "gaps": [{"id": "g-1", "name": "g", "past_moves": ["d-new", "d-newer"]}],
    }
    after_second, _ = apply_agent_update(after_first, proposed_second, "2026-06-10T11:00:00")
    gap_result = next(g for g in after_second["gaps"] if g["id"] == "g-1")

    # Watermark must survive the write that omits it
    assert gap_result.get("past_moves_compacted_at") == "2026-05-01T00:00:00Z"
    # d-newer is added (union semantics without directive), d-new stays
    assert "d-new" in gap_result["past_moves"]
    assert "d-newer" in gap_result["past_moves"]


# ---------------------------------------------------------------------------
# Acceptance evidence: combined Defect 1 + Defect 2 scenario
# ---------------------------------------------------------------------------


def test_watermark_survives_echo_omitting_field_and_filters_proposed_side():
    """Combined acceptance test: watermark survives a write that omits it AND
    filters the proposed side so a stale echo cannot resurrect compacted entries.

    This verifies both Defect 1 (watermark wipe) and Defect 2 (stale-echo
    resurrection via proposed side) in a single scenario matching the real
    multi-write-per-cycle pattern."""
    old_evt = _evt("2026-04-01T00:00:00Z", "old-entry")
    new_evt = _evt("2026-06-01T00:00:00Z", "new-entry")

    # First write: compact old_evt with watermark
    current = {"revision": 1, "gaps": [_gap_with_history("g-1", [old_evt, new_evt])]}
    proposed_first = {
        "gaps": [_gap_with_history("g-1", [new_evt])],
        "_compactions": [{"gap_id": "g-1", "history_watermark": "2026-05-01T00:00:00Z"}],
    }
    after_first, _ = apply_agent_update(current, proposed_first, "2026-06-10T10:00:00")
    gap_after_first = next(g for g in after_first["gaps"] if g["id"] == "g-1")
    assert gap_after_first["history_compacted_at"] == "2026-05-01T00:00:00Z"

    # Second write: agent sends the full pre-compaction view (stale echo); no directive,
    # no watermark field echoed — simulates an agent whose context was built before or
    # during the compaction write (common for multi-write cycles).
    proposed_second = {
        "gaps": [_gap_with_history("g-1", [old_evt, new_evt])],  # stale echo with old_evt
        # deliberately omits history_compacted_at (not echoed by agent)
    }
    after_second, _ = apply_agent_update(after_first, proposed_second, "2026-06-10T11:00:00")
    gap_result = next(g for g in after_second["gaps"] if g["id"] == "g-1")

    # Defect 1: watermark must survive even though the agent didn't echo it
    assert gap_result.get("history_compacted_at") == "2026-05-01T00:00:00Z"

    # Defect 2: old_evt must NOT be resurrected even though it was on proposed side
    history_ts = {e["ts"] for e in gap_result["history"]}
    assert old_evt["ts"] not in history_ts
    assert new_evt["ts"] in history_ts


# ---------------------------------------------------------------------------
# reconcile_gaps_for_retired_directions — gap staffing reconciliation on retire
# ---------------------------------------------------------------------------

_NOW = "2026-06-27T12:00:00"


def _staffed_gap(gap_id: str, active, past=None, **extra):
    """Raw gap dict (as stored in the JSONB column) with staffing moves."""
    gap = {
        "id": gap_id,
        "name": gap_id,
        "status": "active",
        "active_moves": list(active),
        "past_moves": list(past or []),
    }
    gap.update(extra)
    return gap


def test_retire_direction_prunes_active_moves_and_defers_last_move():
    """Acceptance: retiring a gap's LAST active move prunes it into past_moves
    AND records a GapDeferral naming the retired direction, without touching
    status."""
    model = {"gaps": [_staffed_gap("g-1", ["d-1"])]}

    out, deferred = reconcile_gaps_for_retired_directions(model, ["d-1"], _NOW)

    gap = out["gaps"][0]
    assert gap["active_moves"] == []
    assert gap["past_moves"] == ["d-1"]
    assert deferred == ["g-1"]
    # Deferral recorded with the trigger direction named + created_at stamp.
    assert gap["deferral"]["created_at"] == _NOW
    assert "d-1" in gap["deferral"]["rationale"]
    # Status is NEVER changed — gap is not auto-closed.
    assert gap["status"] == "active"


def test_reconcile_non_last_move_prunes_without_deferral():
    """A gap that still has another active move after the prune is reconciled
    but NOT deferred (it remains staffed)."""
    model = {"gaps": [_staffed_gap("g-1", ["d-1", "d-2"])]}

    out, deferred = reconcile_gaps_for_retired_directions(model, ["d-1"], _NOW)

    gap = out["gaps"][0]
    assert gap["active_moves"] == ["d-2"]
    assert gap["past_moves"] == ["d-1"]
    assert deferred == []
    assert "deferral" not in gap or gap.get("deferral") is None
    assert gap["status"] == "active"


def test_reconcile_does_not_duplicate_existing_past_move():
    """Union semantics: an id already in past_moves is not duplicated."""
    model = {"gaps": [_staffed_gap("g-1", ["d-1", "d-2"], past=["d-1"])]}

    out, _ = reconcile_gaps_for_retired_directions(model, ["d-1"], _NOW)

    gap = out["gaps"][0]
    assert gap["past_moves"] == ["d-1"]  # not ["d-1", "d-1"]
    assert gap["active_moves"] == ["d-2"]


def test_reconcile_preserves_existing_deferral():
    """An existing deferral is left exactly as-is even when the retire empties
    active_moves."""
    existing = {"rationale": "operator paused", "created_at": "2026-01-01T00:00:00"}
    model = {"gaps": [_staffed_gap("g-1", ["d-1"], deferral=dict(existing))]}

    out, deferred = reconcile_gaps_for_retired_directions(model, ["d-1"], _NOW)

    gap = out["gaps"][0]
    assert gap["active_moves"] == []
    assert gap["past_moves"] == ["d-1"]
    # Deferral untouched; gap id NOT reported as newly deferred.
    assert gap["deferral"] == existing
    assert deferred == []


def test_reconcile_already_pruned_id_is_noop():
    """Idempotent: re-running with an id already pruned does not duplicate
    past_moves or clobber the recorded deferral."""
    model = {"gaps": [_staffed_gap("g-1", ["d-1"])]}

    first, _ = reconcile_gaps_for_retired_directions(model, ["d-1"], _NOW)
    gap_after_first = dict(first["gaps"][0])

    # Second pass with the same (already-pruned) id.
    second, deferred = reconcile_gaps_for_retired_directions(
        first, ["d-1"], "2026-12-31T00:00:00"
    )
    gap = second["gaps"][0]
    assert gap["active_moves"] == []
    assert gap["past_moves"] == ["d-1"]
    assert deferred == []  # nothing newly deferred on the no-op pass
    # Deferral created_at retains the FIRST timestamp (not clobbered).
    assert gap["deferral"]["created_at"] == gap_after_first["deferral"]["created_at"]


def test_reconcile_matches_on_direction_id_not_gap_hint():
    """Matching is on active_moves membership, not the direction's gap_id hint:
    a gap whose active_moves contains the id is reconciled even with no hint,
    and a gap NOT staffing the id is left untouched."""
    model = {
        "gaps": [
            _staffed_gap("g-1", ["d-1"]),
            _staffed_gap("g-2", ["d-9"]),
        ]
    }

    out, deferred = reconcile_gaps_for_retired_directions(model, ["d-1"], _NOW)

    g1 = next(g for g in out["gaps"] if g["id"] == "g-1")
    g2 = next(g for g in out["gaps"] if g["id"] == "g-2")
    assert g1["active_moves"] == [] and g1["past_moves"] == ["d-1"]
    assert deferred == ["g-1"]
    # g-2 never staffed d-1 -> fully untouched.
    assert g2["active_moves"] == ["d-9"]
    assert g2["past_moves"] == []
    assert "deferral" not in g2


def test_reconcile_tolerates_missing_keys_and_unknown_ids():
    """Backward-compat: gaps without active_moves/past_moves keys, a model
    without gaps, and ids matching no gap are all no-ops (never raise)."""
    # Gap missing active_moves entirely.
    model = {"gaps": [{"id": "g-1", "name": "g-1", "status": "active"}]}
    out, deferred = reconcile_gaps_for_retired_directions(model, ["d-1"], _NOW)
    assert deferred == []
    assert "active_moves" not in out["gaps"][0]

    # Model without gaps key.
    out2, deferred2 = reconcile_gaps_for_retired_directions({}, ["d-1"], _NOW)
    assert deferred2 == []

    # None model + empty id iterable.
    out3, deferred3 = reconcile_gaps_for_retired_directions(None, [], _NOW)
    assert out3 == {} and deferred3 == []


def test_reconcile_result_validates_as_problem_model():
    """The mutated raw dict still round-trips through the pydantic model — the
    recorded deferral matches the GapDeferral shape (no invented fields)."""
    model = {
        "revision": 3,
        "gaps": [_staffed_gap("g-1", ["d-1"], description="a gap")],
    }
    out, _ = reconcile_gaps_for_retired_directions(model, ["d-1"], _NOW)

    parsed = ProblemModel.model_validate(out)
    gap = parsed.gaps[0]
    assert isinstance(gap.deferral, GapDeferral)
    assert gap.deferral.created_at == _NOW
    assert gap.active_moves == []
    assert gap.past_moves == ["d-1"]


# ---------------------------------------------------------------------------
# Gap-staffing reconciliation (create / re-ground side)
# ---------------------------------------------------------------------------


def test_stage_gaps_for_created_directions():
    """Creating a direction on a gap stages it: the direction id joins
    active_moves and a stale deferral is cleared. The symmetric staffing-side
    mirror of the retire-side reconciliation."""
    model = ProblemModel(
        gaps=[
            Gap(
                id="g-1",
                name="metering",
                deferral=GapDeferral(rationale="not this cycle"),
            )
        ]
    )

    changed = reconcile_gap_staffing(model, "d-new", "g-1", None)

    assert changed is True
    gap = model.gaps[0]
    assert gap.active_moves == ["d-new"]
    # The deferral is now false — the gap is actively staffed.
    assert gap.deferral is None


def test_stage_gap_without_deferral_only_adds_move():
    """Staffing a gap that carries no deferral just records the active move."""
    model = ProblemModel(gaps=[Gap(id="g-1", name="metering")])

    changed = reconcile_gap_staffing(model, "d-new", "g-1", None)

    assert changed is True
    assert model.gaps[0].active_moves == ["d-new"]
    assert model.gaps[0].deferral is None


def test_stage_gap_is_idempotent():
    """Re-running with the same linkage neither duplicates active_moves nor
    re-clears an already-cleared deferral — the second call reports no change."""
    model = ProblemModel(
        gaps=[
            Gap(
                id="g-1",
                name="metering",
                deferral=GapDeferral(rationale="not this cycle"),
            )
        ]
    )

    first = reconcile_gap_staffing(model, "d-new", "g-1", None)
    second = reconcile_gap_staffing(model, "d-new", "g-1", None)

    assert first is True
    assert second is False
    assert model.gaps[0].active_moves == ["d-new"]
    assert model.gaps[0].deferral is None


def test_reground_prunes_old_gap_and_stages_new():
    """Re-grounding a direction onto a different gap prunes it from the old
    gap's active_moves (archiving it in past_moves) and stages the new gap."""
    model = ProblemModel(
        gaps=[
            Gap(id="g-old", name="old", active_moves=["d-1", "d-other"]),
            Gap(
                id="g-new",
                name="new",
                deferral=GapDeferral(rationale="parked"),
            ),
        ]
    )

    changed = reconcile_gap_staffing(model, "d-1", "g-new", "g-old")

    assert changed is True
    old_gap = next(g for g in model.gaps if g.id == "g-old")
    new_gap = next(g for g in model.gaps if g.id == "g-new")
    # Old gap: d-1 pruned from active, moved to past; d-other untouched.
    assert old_gap.active_moves == ["d-other"]
    assert old_gap.past_moves == ["d-1"]
    # New gap: staffed and deferral cleared.
    assert new_gap.active_moves == ["d-1"]
    assert new_gap.deferral is None


def test_clear_linkage_prunes_old_gap_only():
    """Clearing a direction's gap linkage (new_gap_id None) prunes the old gap
    with no new staffing."""
    model = ProblemModel(
        gaps=[Gap(id="g-old", name="old", active_moves=["d-1"])]
    )

    changed = reconcile_gap_staffing(model, "d-1", None, "g-old")

    assert changed is True
    assert model.gaps[0].active_moves == []
    assert model.gaps[0].past_moves == ["d-1"]


def test_reground_is_idempotent():
    """Re-applying the same re-ground does not duplicate past_moves or move."""
    model = ProblemModel(
        gaps=[
            Gap(id="g-old", name="old", active_moves=["d-1"]),
            Gap(id="g-new", name="new"),
        ]
    )

    first = reconcile_gap_staffing(model, "d-1", "g-new", "g-old")
    second = reconcile_gap_staffing(model, "d-1", "g-new", "g-old")

    assert first is True
    assert second is False
    old_gap = next(g for g in model.gaps if g.id == "g-old")
    new_gap = next(g for g in model.gaps if g.id == "g-new")
    assert old_gap.active_moves == []
    assert old_gap.past_moves == ["d-1"]
    assert new_gap.active_moves == ["d-1"]


def test_unknown_gap_id_is_safe_no_op():
    """A gap_id with no matching gap (dangling linkage) is a no-op, not a raise.

    Mirrors how the retire/respawn paths tolerate dangling linkages."""
    model = ProblemModel(gaps=[Gap(id="g-1", name="real")])

    changed = reconcile_gap_staffing(model, "d-new", "g-missing", "g-also-missing")

    assert changed is False
    assert model.gaps[0].active_moves == []


def test_raw_gap_staffing_classifies_unresolved_gap_id():
    """Dangling links remain fail-open but are classified for caller logging."""
    raw = ProblemModel(gaps=[Gap(id="g-1", name="real")]).model_dump()

    result = reconcile_gap_staffing_raw_result(
        raw,
        "d-new",
        "g-missing",
        None,
        new_status="active",
    )

    assert result["changed"] is False
    assert result["problem_model"] is None
    details = result["result"]
    assert details["unresolved_gap_ids"] == {"new_gap_id": "g-missing"}
    assert details["malformed_model"] is False


def test_reground_to_same_gap_is_no_op():
    """gap_id 'change' to the same value does not prune-then-restage — the old
    gap is left alone and staffing stays idempotent."""
    model = ProblemModel(gaps=[Gap(id="g-1", name="x", active_moves=["d-1"])])

    changed = reconcile_gap_staffing(model, "d-1", "g-1", "g-1")

    assert changed is False
    assert model.gaps[0].active_moves == ["d-1"]
    assert model.gaps[0].past_moves == []


def test_empty_direction_id_is_no_op():
    """A falsy direction id reconciles nothing."""
    model = ProblemModel(gaps=[Gap(id="g-1", name="x")])
    assert reconcile_gap_staffing(model, "", "g-1", None) is False
    assert model.gaps[0].active_moves == []


def test_reconcile_does_not_create_gaps_proposed_wins():
    """Reconciliation only adjusts existing gaps — it never creates one, so it
    cannot fight the agent's own problem-model writes."""
    model = ProblemModel(gaps=[Gap(id="g-1", name="x")])
    before_ids = {g.id for g in model.gaps}

    reconcile_gap_staffing(model, "d-1", "g-unknown", None)

    assert {g.id for g in model.gaps} == before_ids


def test_reconcile_raw_returns_changed_dump():
    """The raw adapter returns a model_dump dict when something changed, with
    the staffing applied."""
    raw = ProblemModel(
        gaps=[
            Gap(
                id="g-1",
                name="x",
                deferral=GapDeferral(rationale="parked"),
            )
        ]
    ).model_dump()

    result = reconcile_gap_staffing_raw(raw, "d-1", "g-1", None)

    assert result is not None
    gap = result["gaps"][0]
    assert gap["active_moves"] == ["d-1"]
    assert gap["deferral"] is None


def test_reconcile_raw_returns_none_on_no_change():
    """The raw adapter returns None when nothing changed (so the caller skips
    the write and keeps the column byte-identical)."""
    raw = ProblemModel(
        gaps=[Gap(id="g-1", name="x", active_moves=["d-1"])]
    ).model_dump()

    # d-1 already staffs g-1, no deferral → no change.
    assert reconcile_gap_staffing_raw(raw, "d-1", "g-1", None) is None


def test_reconcile_raw_empty_model_is_none():
    """An empty/None raw model is a no-op None (fail-open)."""
    assert reconcile_gap_staffing_raw(None, "d-1", "g-1", None) is None
    assert reconcile_gap_staffing_raw({}, "d-1", "g-1", None) is None


def test_reconcile_raw_malformed_model_fails_open():
    """A malformed durable model returns None rather than raising — a bad model
    can never block a direction write."""
    # `gaps` must be a list of gap objects; a string is malformed.
    assert reconcile_gap_staffing_raw({"gaps": "nope"}, "d-1", "g-1", None) is None


# ---------------------------------------------------------------------------
# classify_gap_link — direction–gap referential-integrity guard
# ---------------------------------------------------------------------------


def test_classify_gap_link_none_when_no_gap_id():
    """A direction with no gap_id (ungrounded) classifies as 'none'."""
    model = ProblemModel(gaps=[Gap(id="g-1", name="x")])
    assert classify_gap_link(None, model) == "none"
    assert classify_gap_link("", model) == "none"


def test_classify_gap_link_valid_for_active_gap():
    """A gap_id pointing to an active gap classifies as 'valid'."""
    model = ProblemModel(gaps=[Gap(id="g-active", name="x", status="active")])
    assert classify_gap_link("g-active", model) == "valid"


def test_classify_gap_link_valid_for_blocked_gap():
    """A gap_id pointing to a blocked gap is still workable — 'valid'."""
    model = ProblemModel(gaps=[Gap(id="g-blocked", name="x", status="blocked")])
    assert classify_gap_link("g-blocked", model) == "valid"


def test_classify_gap_link_closed_for_terminal_active_list_gap():
    """A gap that is still in model.gaps but has a terminal status (closed or
    abandoned) classifies as 'closed', not 'valid' or 'dangling'."""
    model = ProblemModel(
        gaps=[
            Gap(id="g-closed", name="done", status="closed"),
            Gap(id="g-abandoned", name="gave up", status="abandoned"),
        ]
    )
    assert classify_gap_link("g-closed", model) == "closed"
    assert classify_gap_link("g-abandoned", model) == "closed"


def test_classify_gap_link_closed_for_archived_gap():
    """A gap_id matching an archived gap (fully moved to archived_gaps)
    classifies as 'closed'."""
    model = ProblemModel(
        archived_gaps=[Gap(id="g-old", name="historical", status="closed")]
    )
    assert classify_gap_link("g-old", model) == "closed"


def test_classify_gap_link_dangling_when_no_match():
    """A gap_id that matches nothing — neither active gaps nor archived — is
    'dangling'. This is the orphaned-gap conflict class."""
    model = ProblemModel(gaps=[Gap(id="g-real", name="x")])
    assert classify_gap_link("g-nonexistent", model) == "dangling"


def test_classify_gap_link_dangling_on_empty_model():
    """On a blank problem model every gap_id is dangling."""
    assert classify_gap_link("g-anything", ProblemModel()) == "dangling"


def test_classify_gap_link_pure_no_mutation():
    """classify_gap_link must not mutate the model (pure read)."""
    model = ProblemModel(gaps=[Gap(id="g-1", name="x", status="active")])
    before = model.model_dump()
    classify_gap_link("g-1", model)
    assert model.model_dump() == before


# ---------------------------------------------------------------------------
# check_gap_id_write — write-time guard for direction gap_id linkage
# ---------------------------------------------------------------------------


def test_check_gap_id_write_no_error_for_valid_gap():
    pm = ProblemModel(gaps=[Gap(id="g-open", name="x", status="active")]).model_dump()
    err, open_ids = check_gap_id_write("g-open", pm)
    assert err is None
    assert open_ids == []


def test_check_gap_id_write_no_error_for_blocked_gap():
    pm = ProblemModel(gaps=[Gap(id="g-blk", name="x", status="blocked")]).model_dump()
    err, open_ids = check_gap_id_write("g-blk", pm)
    assert err is None


def test_check_gap_id_write_error_for_closed_gap():
    pm = ProblemModel(gaps=[Gap(id="g-done", name="x", status="closed")]).model_dump()
    err, open_ids = check_gap_id_write("g-done", pm)
    assert err == "closed"
    assert open_ids == []  # no open gaps in this model


def test_check_gap_id_write_error_for_dangling_gap():
    pm = ProblemModel(gaps=[Gap(id="g-real", name="x", status="active")]).model_dump()
    err, open_ids = check_gap_id_write("g-ghost", pm)
    assert err == "dangling"
    assert open_ids == ["g-real"]  # the one open gap is surfaced


def test_check_gap_id_write_open_ids_exclude_closed_gaps():
    pm = ProblemModel(gaps=[
        Gap(id="g-open1", name="a", status="active"),
        Gap(id="g-closed", name="b", status="closed"),
        Gap(id="g-open2", name="c", status="blocked"),
    ]).model_dump()
    err, open_ids = check_gap_id_write("g-ghost", pm)
    assert err == "dangling"
    assert sorted(open_ids) == ["g-open1", "g-open2"]


def test_check_gap_id_write_no_gap_id_is_ok():
    pm = ProblemModel(gaps=[Gap(id="g-x", name="x", status="active")]).model_dump()
    err, open_ids = check_gap_id_write(None, pm)
    assert err is None
    err2, _ = check_gap_id_write("", pm)
    assert err2 is None


def test_check_gap_id_write_no_problem_model_skips_validation():
    """When the project has no problem model, validation is skipped (fail-open)."""
    err, open_ids = check_gap_id_write("g-anything", None)
    assert err is None
    assert open_ids == []


def test_check_gap_id_write_malformed_model_skips_validation():
    """A malformed problem model dict skips validation rather than raising."""
    err, open_ids = check_gap_id_write("g-x", {"gaps": "not-a-list"})
    assert err is None


# ---------------------------------------------------------------------------
# normalize_gap_active_moves
# ---------------------------------------------------------------------------

_NORM_NOW = "2026-07-04T00:00:00"


def test_normalize_gap_active_moves_prunes_non_active():
    """A paused/expired direction ID in active_moves is moved to past_moves."""
    gap = Gap(id="g-1", name="x", active_moves=["d-active", "d-paused"])
    model = ProblemModel(gaps=[gap])
    corrections = normalize_gap_active_moves(model, {"d-active"}, _NORM_NOW)
    assert model.gaps[0].active_moves == ["d-active"]
    assert "d-paused" in model.gaps[0].past_moves
    assert any("d-paused" in c for c in corrections)


def test_normalize_gap_active_moves_auto_defers_emptied_gap():
    """A gap whose active_moves becomes empty is auto-deferred."""
    gap = Gap(id="g-1", name="x", active_moves=["d-old"])
    model = ProblemModel(gaps=[gap])
    corrections = normalize_gap_active_moves(model, set(), _NORM_NOW)
    assert model.gaps[0].active_moves == []
    assert model.gaps[0].deferral is not None
    assert model.gaps[0].deferral.created_at == _NORM_NOW
    assert any("auto-deferred" in c for c in corrections)


def test_normalize_gap_active_moves_preserves_existing_deferral():
    """An existing deferral is not overwritten when active_moves empties."""
    existing = GapDeferral(rationale="operator hold", created_at="2026-01-01T00:00:00")
    gap = Gap(id="g-1", name="x", active_moves=["d-1"], deferral=existing)
    model = ProblemModel(gaps=[gap])
    normalize_gap_active_moves(model, set(), _NORM_NOW)
    assert model.gaps[0].deferral.rationale == "operator hold"


def test_normalize_gap_active_moves_no_change_when_all_active():
    """No corrections when all active_moves IDs are in active_direction_ids."""
    gap = Gap(id="g-1", name="x", active_moves=["d-1", "d-2"])
    model = ProblemModel(gaps=[gap])
    corrections = normalize_gap_active_moves(model, {"d-1", "d-2"}, _NORM_NOW)
    assert corrections == []
    assert model.gaps[0].active_moves == ["d-1", "d-2"]


def test_normalize_gap_active_moves_no_duplicate_past_moves():
    """A stale ID already in past_moves is not duplicated."""
    gap = Gap(id="g-1", name="x", active_moves=["d-1"], past_moves=["d-1"])
    model = ProblemModel(gaps=[gap])
    normalize_gap_active_moves(model, set(), _NORM_NOW)
    assert model.gaps[0].past_moves.count("d-1") == 1


# ---------------------------------------------------------------------------
# normalize_active_moves_writeback (shared helper)
# ---------------------------------------------------------------------------


def test_normalize_active_moves_writeback_no_corrections_unchanged():
    """Returns raw dict unchanged and empty corrections when all moves are active."""
    gap = Gap(id="g-1", name="x", active_moves=["d-active"])
    pm = ProblemModel(gaps=[gap])
    raw = pm.model_dump()
    updated_raw, corrections = normalize_active_moves_writeback(raw, {"d-active"}, _NORM_NOW)
    assert corrections == []
    assert updated_raw == raw


def test_normalize_active_moves_writeback_records_revision_log():
    """When corrections occur, a revision-log entry is written into the returned raw dict."""
    gap = Gap(id="g-1", name="x", active_moves=["d-paused"])
    pm = ProblemModel(gaps=[gap])
    raw = pm.model_dump()
    updated_raw, corrections = normalize_active_moves_writeback(raw, set(), _NORM_NOW)
    assert len(corrections) >= 1
    log = updated_raw.get("revision_log") or []
    assert any(
        isinstance(e, dict) and "active_moves_normalized" in e.get("changes", {})
        for e in log
    ), "expected revision_log entry for active_moves normalization"


def test_normalize_active_moves_writeback_prunes_stale():
    """Stale active_moves ID ends up in past_moves in the returned raw dict."""
    gap = Gap(id="g-1", name="x", active_moves=["d-paused", "d-active"])
    pm = ProblemModel(gaps=[gap])
    raw = pm.model_dump()
    updated_raw, _ = normalize_active_moves_writeback(raw, {"d-active"}, _NORM_NOW)
    g = updated_raw["gaps"][0]
    assert "d-paused" not in g["active_moves"]
    assert "d-paused" in g["past_moves"]


# ---------------------------------------------------------------------------
# active_direction_gap_ids
# ---------------------------------------------------------------------------


def test_active_direction_gap_ids_returns_active_only():
    dirs = [
        {"id": "d-1", "status": "active", "gap_id": "g-1"},
        {"id": "d-2", "status": "paused", "gap_id": "g-2"},
        {"id": "d-3", "status": "active", "gap_id": "g-3"},
    ]
    assert active_direction_gap_ids(dirs) == {"g-1", "g-3"}


def test_active_direction_gap_ids_skips_missing_gap_id():
    dirs = [{"id": "d-1", "status": "active"}]
    assert active_direction_gap_ids(dirs) == set()


def test_active_direction_gap_ids_empty_input():
    assert active_direction_gap_ids([]) == set()
    assert active_direction_gap_ids(None) == set()


# ---------------------------------------------------------------------------
# apply_agent_update with protected_gap_ids (Residual 2 guard)
# ---------------------------------------------------------------------------


def test_apply_agent_update_gap_deletion_orphan_guard():
    """A _deletions.gaps entry that orphans an active direction is blocked.

    The gap must survive (not be deleted), and diff['gaps_deletion_blocked']
    must list the blocked id.  This is the named acceptance-criterion test.
    """
    current = {
        "revision": 1,
        "gaps": [
            {"id": "g-protected", "name": "live gap", "status": "active"},
            {"id": "g-safe", "name": "safe gap", "status": "active"},
        ],
    }
    proposed = {
        "gaps": [
            {"id": "g-protected", "name": "live gap", "status": "active"},
            {"id": "g-safe", "name": "safe gap", "status": "active"},
        ],
        "_deletions": {"gaps": ["g-protected", "g-safe"]},
    }
    # g-protected is still the gap_id of an active direction.
    protected = {"g-protected"}
    new_raw, diff = apply_agent_update(
        current, proposed, _NORM_NOW, protected_gap_ids=protected
    )
    assert "g-protected" in diff.get("gaps_deletion_blocked", [])
    assert "g-safe" in diff.get("gaps_deleted", [])
    surviving_ids = [g["id"] for g in new_raw.get("gaps", [])]
    assert "g-protected" in surviving_ids, "protected gap must not be deleted"
    assert "g-safe" not in surviving_ids, "unprotected gap should be deleted"


def test_apply_agent_update_gap_deletion_no_protected_ids_deletes_all():
    """Without protected_gap_ids, all requested deletions proceed as before."""
    current = {
        "revision": 1,
        "gaps": [{"id": "g-1", "name": "x", "status": "active"}],
    }
    proposed = {
        "gaps": [{"id": "g-1", "name": "x", "status": "active"}],
        "_deletions": {"gaps": ["g-1"]},
    }
    new_raw, diff = apply_agent_update(current, proposed, _NORM_NOW)
    assert "g-1" in diff.get("gaps_deleted", [])
    surviving_ids = [g["id"] for g in new_raw.get("gaps", [])]
    assert "g-1" not in surviving_ids


def test_apply_agent_update_gap_deletion_protected_empty_set_deletes_all():
    """An empty protected set allows all deletions (no false blocks)."""
    current = {
        "revision": 1,
        "gaps": [{"id": "g-1", "name": "x", "status": "active"}],
    }
    proposed = {
        "gaps": [{"id": "g-1", "name": "x", "status": "active"}],
        "_deletions": {"gaps": ["g-1"]},
    }
    new_raw, diff = apply_agent_update(
        current, proposed, _NORM_NOW, protected_gap_ids=set()
    )
    assert "g-1" in diff.get("gaps_deleted", [])
    surviving_ids = [g["id"] for g in new_raw.get("gaps", [])]
    assert "g-1" not in surviving_ids


def test_apply_agent_update_gap_deletion_blocked_does_not_block_write():
    """The write succeeds even when a gap deletion is blocked (non-fatal)."""
    current = {
        "revision": 1,
        "gaps": [{"id": "g-protected", "name": "y", "status": "active"}],
        "intent": {"purpose": "test", "current_focus": ""},
    }
    proposed = {
        "gaps": [{"id": "g-protected", "name": "y updated", "status": "active"}],
        "_deletions": {"gaps": ["g-protected"]},
        "intent": {"purpose": "test updated", "current_focus": ""},
    }
    new_raw, diff = apply_agent_update(
        current, proposed, _NORM_NOW, protected_gap_ids={"g-protected"}
    )
    assert new_raw["revision"] == 2
    assert "g-protected" in diff["gaps_deletion_blocked"]
    surviving = [g["id"] for g in new_raw["gaps"]]
    assert "g-protected" in surviving


# ---------------------------------------------------------------------------
# Round-trip: operator intent.purpose preserved on both write paths
#
# Criterion c-8cdb5049: source=null corrupted shape full round-trip
# Criterion c-543d4959: placeholder auto-restore from operator-owned user_purpose
#
# Both apply_agent_update (agent write path) and apply_user_intent_patch (user
# write path) must handle corrupted shapes correctly. The existing tests for
# apply_agent_update without baseline_raw are at lines 1575-1630; the tests
# below extend coverage to (a) the 3-way-merge code path (baseline_raw
# provided) and (b) the apply_user_intent_patch write path.
# ---------------------------------------------------------------------------


# --- apply_user_intent_patch path ---


def test_apply_user_intent_patch_source_null_shape_restamps_source_and_user_purpose():
    """c-8cdb5049 / apply_user_intent_patch path: when the current model holds the
    corrupted shape (purpose="", source=null, user_purpose populated) and the
    operator re-patches with a real intent, the write path correctly stamps
    source=user_message and user_purpose — healing the corruption."""
    corrupted = {
        "revision": 5,
        "intent": {
            "purpose": "",
            "current_focus": "some focus",
            "source": None,
            "user_purpose": "Make Blueprint handle any project well.",
        },
    }
    model = apply_user_intent_patch(
        corrupted,
        {"intent": {"purpose": "Make Blueprint handle any project well.", "current_focus": "v22"}},
        "2026-07-05T00:00:00",
    )
    assert model.intent.purpose == "Make Blueprint handle any project well."
    assert model.intent.source is not None
    assert model.intent.source.type == "user_message"
    assert model.intent.user_purpose == "Make Blueprint handle any project well.", (
        "user_purpose must mirror the operator-set purpose after intent patch"
    )
    assert model.revision == 6
    assert model.last_updated_by == "user"


def test_apply_user_intent_patch_placeholder_shape_stamps_user_purpose():
    """c-543d4959 / apply_user_intent_patch path: when the current model holds a
    placeholder purpose and the operator re-patches, the user_purpose is set to
    the real purpose so future agent cycles can self-heal from it."""
    for placeholder in ("...", "<from analysis>", "<placeholder>", ""):
        corrupted = {
            "revision": 2,
            "intent": {
                "purpose": placeholder,
                "source": None,
                "user_purpose": "Real operator purpose.",
            },
        }
        model = apply_user_intent_patch(
            corrupted,
            {"intent": {"purpose": "Real operator purpose.", "current_focus": ""}},
            "2026-07-05T00:00:00",
        )
        assert model.intent.purpose == "Real operator purpose.", (
            f"placeholder {placeholder!r} not replaced"
        )
        assert model.intent.source is not None
        assert model.intent.source.type == "user_message"
        assert model.intent.user_purpose == "Real operator purpose.", (
            f"user_purpose not set after replacing placeholder {placeholder!r}"
        )


def test_apply_user_intent_patch_context_only_preserves_user_purpose_on_corrupted_model():
    """c-8cdb5049 / apply_user_intent_patch path: a context-only patch on a
    source=null, blank-purpose corrupted model carries user_purpose forward so a
    subsequent apply_agent_update can heal from it — full round-trip."""
    corrupted = {
        "revision": 5,
        "intent": {
            "purpose": "",
            "source": None,
            "user_purpose": "Operator real purpose.",
        },
    }
    # User only patches context (no intent in patch).
    patched = apply_user_intent_patch(
        corrupted,
        {"context": {"project_type": "startup"}},
        "2026-07-05T00:00:00",
    )
    # user_purpose must survive the context-only patch.
    assert patched.intent.user_purpose == "Operator real purpose.", (
        "user_purpose must be preserved on a non-intent patch over a corrupted model"
    )
    assert patched.context.project_type == "startup"
    assert patched.intent.purpose == ""  # still blank — user did not fix it

    # apply_agent_update then heals from user_purpose regardless of source=null.
    new_raw, _ = apply_agent_update(
        patched.model_dump(),
        {"intent": {"purpose": "", "current_focus": "new focus"}},
        "2026-07-05T01:00:00",
    )
    assert new_raw["intent"]["purpose"] == "Operator real purpose.", (
        "intent.purpose must be restored from user_purpose after context-only "
        "apply_user_intent_patch preserves user_purpose across the corrupted model"
    )
    assert new_raw["intent"]["source"]["type"] == "user_message"
    assert new_raw["intent"]["current_focus"] == "new focus"


# --- apply_agent_update path WITH baseline_raw (3-way merge code path) ---


def test_apply_agent_update_heals_source_null_on_3way_merge_path():
    """c-8cdb5049 / apply_agent_update with baseline_raw: source=null + blank
    purpose + user_purpose populated — the user_purpose fallback in
    _preserve_operator_intent fires on the 3-way-merge code path just as it does
    on the legacy path (baseline_raw=None)."""
    current = {
        "revision": 3,
        "last_updated_by": "agent",
        "intent": {
            "purpose": "",
            "current_focus": "some focus",
            "source": None,
            "user_purpose": "Make Blueprint handle any project well.",
        },
    }
    baseline = {
        "revision": 2,
        "intent": {
            "purpose": "Make Blueprint handle any project well.",
            "source": {"type": "user_message", "message_id": "m-seed"},
        },
    }
    proposed = {"intent": {"purpose": "", "current_focus": "next focus"}}
    new_raw, _ = apply_agent_update(
        current, proposed, "2026-07-05T00:00:00", baseline_raw=baseline
    )
    assert new_raw["intent"]["purpose"] == "Make Blueprint handle any project well.", (
        "intent.purpose must be restored from user_purpose even on the 3-way-merge path"
    )
    assert new_raw["intent"]["source"]["type"] == "user_message"
    # agent-owned current_focus still advances
    assert new_raw["intent"]["current_focus"] == "next focus"
    assert new_raw["intent"]["user_purpose"] == "Make Blueprint handle any project well."


def test_apply_agent_update_heals_placeholder_on_3way_merge_path():
    """c-543d4959 / apply_agent_update with baseline_raw: placeholder purpose +
    user_purpose — the user_purpose fallback fires on the 3-way-merge path."""
    for placeholder in ("...", "<from analysis>", ""):
        current = {
            "revision": 1,
            "intent": {
                "purpose": placeholder,
                "source": None,
                "user_purpose": "Real operator purpose.",
            },
        }
        baseline = {
            "revision": 0,
            "intent": {
                "purpose": "Real operator purpose.",
                "source": {"type": "user_message"},
            },
        }
        proposed = {"intent": {"purpose": placeholder, "current_focus": "next"}}
        new_raw, _ = apply_agent_update(
            current, proposed, "2026-07-05T00:00:00", baseline_raw=baseline
        )
        assert new_raw["intent"]["purpose"] == "Real operator purpose.", (
            f"placeholder {placeholder!r} not healed from user_purpose on 3-way-merge path"
        )
        assert new_raw["intent"]["source"]["type"] == "user_message"
        assert new_raw["intent"]["user_purpose"] == "Real operator purpose."


def test_full_round_trip_both_write_paths_source_null_and_placeholder():
    """c-8cdb5049 + c-543d4959: full round-trip exercising both write paths.

    1. apply_user_intent_patch seeds purpose + user_purpose (user write path).
    2. Agent cycle corrupts: source=null, purpose blanked to placeholder.
    3. apply_user_intent_patch context-only patch preserves user_purpose
       (user write path — no intent in patch).
    4. apply_agent_update heals from user_purpose (agent write path, with
       baseline_raw to exercise the 3-way-merge code path).
    """
    # Step 1: operator seeds purpose via the user write path.
    seeded = apply_user_intent_patch(
        None,
        {"intent": {"purpose": "Operator steering purpose.", "current_focus": ""}},
        "2026-07-05T00:00:00",
    )
    assert seeded.intent.purpose == "Operator steering purpose."
    assert seeded.intent.user_purpose == "Operator steering purpose."
    assert seeded.intent.source is not None
    assert seeded.intent.source.type == "user_message"

    # Step 2: agent cycle drops provenance and then something blanks purpose.
    cycle1, _ = apply_agent_update(
        seeded.model_dump(),
        {"intent": {"purpose": "Agent derived purpose.", "current_focus": "phase 1"}},
        "2026-07-05T01:00:00",
        baseline_raw=None,  # legacy path drops source
    )
    # Corrupt: simulate purpose being overwritten with a placeholder.
    corrupted = dict(cycle1)
    corrupted["intent"] = dict(cycle1["intent"])
    corrupted["intent"]["purpose"] = "..."
    corrupted["intent"]["source"] = None
    assert corrupted["intent"]["user_purpose"] == "Operator steering purpose."

    # Step 3: operator issues a context-only patch (user write path, no intent).
    after_context_patch = apply_user_intent_patch(
        corrupted,
        {"context": {"project_type": "product"}},
        "2026-07-05T02:00:00",
    )
    # user_purpose is preserved across the non-intent patch.
    assert after_context_patch.intent.user_purpose == "Operator steering purpose.", (
        "user_purpose must survive a context-only apply_user_intent_patch call"
    )
    assert after_context_patch.intent.purpose == "...", "placeholder still present"

    # Step 4: agent cycle heals from user_purpose (3-way-merge path).
    baseline = {"revision": cycle1["revision"], "intent": {"purpose": "Agent derived purpose."}}
    final, _ = apply_agent_update(
        after_context_patch.model_dump(),
        {"intent": {"purpose": "...", "current_focus": "phase 2"}},
        "2026-07-05T03:00:00",
        baseline_raw=baseline,
    )
    assert final["intent"]["purpose"] == "Operator steering purpose.", (
        "intent.purpose must be healed from user_purpose after the full round-trip "
        "through both write paths"
    )
    assert final["intent"]["source"]["type"] == "user_message"
    assert final["intent"]["user_purpose"] == "Operator steering purpose."
    assert final["intent"]["current_focus"] == "phase 2"


# ---------------------------------------------------------------------------
# Capability.evidence_refs and capability_evidence_status
# ---------------------------------------------------------------------------


def test_capability_evidence_refs_defaults_empty():
    """A Capability with no evidence_refs parses and the field defaults to []."""
    cap = Capability(name="Task dispatch", status="working")
    assert cap.evidence_refs == []


def test_capability_evidence_refs_round_trip():
    """Round-trip a Capability with evidence_refs through model_dump/model_validate."""
    cap = Capability(
        name="Strategic cycles",
        status="partial",
        evidence_refs=[
            {"kind": "named_test", "ref": "backend/testing/test_foo.py", "proven": True},
            {"kind": "backing_path", "ref": "backend/app/foo.py", "proven": False},
        ],
    )
    dumped = cap.model_dump()
    assert dumped["evidence_refs"][0]["kind"] == "named_test"
    assert dumped["evidence_refs"][0]["proven"] is True
    restored = Capability.model_validate(dumped)
    assert restored.evidence_refs == cap.evidence_refs


def test_capability_missing_evidence_refs_back_compat_validates():
    """A stored capability payload lacking evidence_refs validates under extra='forbid'."""
    raw = {
        "revision": 2,
        "capabilities": [
            {"name": "Task dispatch", "status": "working"},
        ],
    }
    model = parse_problem_model(raw)
    assert model.capabilities[0].evidence_refs == []
    restored = parse_problem_model(model.model_dump())
    assert restored.capabilities[0].evidence_refs == []


def test_capability_evidence_status_proven():
    """capability_evidence_status returns 'proven' when evidence_refs has a proven=True entry."""
    cap = Capability(
        name="Task dispatch",
        status="working",
        evidence_refs=[{"kind": "named_test", "ref": "test_foo.py", "proven": True}],
    )
    assert capability_evidence_status(cap) == "proven"


def test_capability_evidence_status_partial():
    """capability_evidence_status returns 'partial' with refs that exist but none proven."""
    cap = Capability(
        name="Task dispatch",
        status="working",
        evidence_refs=[{"kind": "named_test", "ref": "test_foo.py", "proven": False}],
    )
    assert capability_evidence_status(cap) == "partial"


def test_capability_evidence_status_unproven():
    """capability_evidence_status returns 'unproven' with empty evidence_refs."""
    cap = Capability(name="Task dispatch", status="working")
    assert capability_evidence_status(cap) == "unproven"


def test_capability_evidence_status_unverified():
    """capability_evidence_status returns 'unverified' with structurally uncheckable refs.

    Bypass Pydantic validation via model_construct to inject non-dict entries
    (the only way to produce the 'unverified' branch in classify_evidence —
    a TypedDict would always be a dict and therefore 'partial' or 'proven').
    """
    cap = Capability.model_construct(
        name="Task dispatch",
        status="working",
        evidence_refs=["not a dict"],  # non-dict entries → classify_evidence → "unverified"
    )
    assert capability_evidence_status(cap) == "unverified"


def test_apply_agent_update_capability_evidence_refs_proposed_wins():
    """evidence_refs follows proposed-wins through _upsert_by_key/_merge_cap_preserving."""
    current = {
        "revision": 1,
        "capabilities": [
            {
                "name": "Task dispatch",
                "status": "working",
                "evidence_refs": [
                    {"kind": "named_test", "ref": "old_test.py", "proven": False}
                ],
            }
        ],
    }
    proposed = {
        "capabilities": [
            {
                "name": "Task dispatch",
                "status": "working",
                "evidence_refs": [
                    {"kind": "backing_path", "ref": "new_path.py", "proven": True}
                ],
            }
        ],
    }
    new_raw, _diff = apply_agent_update(current, proposed, "2026-07-10T12:00:00")
    merged_cap = new_raw["capabilities"][0]
    assert merged_cap["evidence_refs"] == [
        {"kind": "backing_path", "ref": "new_path.py", "proven": True}
    ]


# ---------------------------------------------------------------------------
# criterion_coverage_to_evidence_status
# ---------------------------------------------------------------------------


def test_criterion_coverage_to_evidence_status_covered():
    assert criterion_coverage_to_evidence_status("covered") == "proven"


def test_criterion_coverage_to_evidence_status_partial():
    assert criterion_coverage_to_evidence_status("partial") == "partial"


def test_criterion_coverage_to_evidence_status_uncovered():
    assert criterion_coverage_to_evidence_status("uncovered") == "unproven"


def test_criterion_coverage_to_evidence_status_unknown():
    assert criterion_coverage_to_evidence_status("bogus") == "unverified"
    assert criterion_coverage_to_evidence_status("") == "unverified"


# ---------------------------------------------------------------------------
# Acceptance evidence: evidence-primitive phase-1 integration
# Named tests proving that Gap.evidence_refs and Capability.evidence_refs have
# downstream consumers and that status is validated against evidence.
# ---------------------------------------------------------------------------


def test_evidence_primitive_phase1_capability_status_validated_against_evidence():
    """Capability.status is validated against its evidence_refs via
    claim_contradicts_evidence — a 'working' capability with zero evidence_refs
    is a contradiction, and one with a proven named_test is not.

    Acceptance evidence for evidence-primitive design phase 1: the evidence
    classification and contradiction check are wired into the problem model, not
    advisory-only; capability_evidence_status delegates to classify_evidence and
    claim_contradicts_evidence operates on the result.
    """
    from app.evidence_primitive import claim_contradicts_evidence

    # A 'working' capability with no refs → evidence is 'unproven' → contradicts.
    cap_no_evidence = Capability(name="dispatch", status="working")
    ev_status = capability_evidence_status(cap_no_evidence)
    assert ev_status == "unproven"
    assert claim_contradicts_evidence("working", ev_status) is True

    # A 'working' capability with a proven named_test → evidence is 'proven' → ok.
    cap_proven = Capability(
        name="dispatch",
        status="working",
        evidence_refs=[{"kind": "named_test", "ref": "test_dispatch.py", "proven": True}],
    )
    ev_status_proven = capability_evidence_status(cap_proven)
    assert ev_status_proven == "proven"
    assert claim_contradicts_evidence("working", ev_status_proven) is False


def test_evidence_primitive_phase1_gap_evidence_refs_downstream_consumer():
    """Gap.evidence_refs feeds gap_evidence_status which feeds claim_contradicts_evidence.

    Acceptance evidence for evidence-primitive design phase 1: structured
    evidence pointers on Gap are not inert storage — they flow through
    gap_evidence_status into claim_contradicts_evidence, producing actionable
    contradiction signals that the metrics_integrity_sweep reconciliation step
    consumes. A 'closed' gap with no proven evidence is flagged; a 'closed'
    gap with a proven named_test is clean.
    """
    from app.evidence_primitive import claim_contradicts_evidence

    # Closed gap with no proven evidence → unproven → contradiction.
    closed_gap_no_proof = Gap(id="g1", name="g1", status="closed")
    ev = gap_evidence_status(closed_gap_no_proof)
    assert ev == "unproven"
    assert claim_contradicts_evidence("closed", ev) is True

    # Closed gap with proven named_test → proven → no contradiction.
    closed_gap_proven = Gap(
        id="g2",
        name="g2",
        status="closed",
        evidence_refs=[{"kind": "named_test", "ref": "test_g2.py", "proven": True}],
    )
    ev_proven = gap_evidence_status(closed_gap_proven)
    assert ev_proven == "proven"
    assert claim_contradicts_evidence("closed", ev_proven) is False


# ---------------------------------------------------------------------------
# Auto-clear moot pending confirmations
# ---------------------------------------------------------------------------


def _pending(pid: str, ptype: str, proposal: str = "", rationale: str = ""):
    return PendingConfirmation(
        id=pid, type=ptype, proposal=proposal, rationale=rationale
    )


def test_auto_clear_credentials_needed_when_llm_ready():
    """A credentials_needed item referencing LLM creds clears when the owner's
    LLM account is runtime-ready — and revision/log are stamped like the
    operator dismiss endpoint."""
    model = ProblemModel(
        revision=4,
        pending_user_confirmation=[
            _pending(
                "p-cred",
                "credentials_needed",
                proposal="Add an Anthropic Claude OAuth token so the pool can run.",
            )
        ],
    )
    signals = ConfirmationResolutionSignals(llm_credentials_ready=True)
    cleared = auto_clear_resolved_confirmations(model, signals, "2026-07-14T00:00:00")
    assert cleared == ["p-cred"]
    assert model.pending_user_confirmation == []
    assert model.revision == 5
    assert model.last_updated == "2026-07-14T00:00:00"
    assert model.last_updated_by == "user"
    assert len(model.revision_log) == 1
    entry = model.revision_log[0]
    assert entry.revision == 5
    assert entry.changes["auto_cleared_pending_confirmations"] == ["p-cred"]


def test_credentials_needed_left_when_llm_not_ready():
    """Fail-closed: no positive credential signal -> item stays, byte-identical."""
    model = ProblemModel(
        pending_user_confirmation=[
            _pending("p-cred", "credentials_needed", proposal="Need a Claude API key.")
        ],
    )
    before = model.model_dump()
    cleared = auto_clear_resolved_confirmations(
        model, ConfirmationResolutionSignals(llm_credentials_ready=False), "2026-07-14T00:00:00"
    )
    assert cleared == []
    assert model.model_dump() == before  # no revision bump, no log entry


def test_credentials_needed_left_when_text_unrelated():
    """Positive LLM signal but the ask is about a DIFFERENT secret -> left in place."""
    model = ProblemModel(
        pending_user_confirmation=[
            _pending(
                "p-hf",
                "credentials_needed",
                proposal="Need a HuggingFace dataset download token for the experiment.",
            )
        ],
    )
    cleared = auto_clear_resolved_confirmations(
        model, ConfirmationResolutionSignals(llm_credentials_ready=True), "2026-07-14T00:00:00"
    )
    assert cleared == []
    assert [p.id for p in model.pending_user_confirmation] == ["p-hf"]


def test_auto_clear_access_needed_when_repo_ready():
    """An access_needed item referencing repo/github access clears when a live
    repo token proves access is granted."""
    model = ProblemModel(
        pending_user_confirmation=[
            _pending(
                "p-access",
                "access_needed",
                proposal="Grant the GitHub app access to the repository.",
            )
        ],
    )
    cleared = auto_clear_resolved_confirmations(
        model, ConfirmationResolutionSignals(repo_access_ready=True), "2026-07-14T00:00:00"
    )
    assert cleared == ["p-access"]
    assert model.pending_user_confirmation == []


def test_access_needed_left_when_repo_not_ready():
    model = ProblemModel(
        pending_user_confirmation=[
            _pending("p-access", "access_needed", proposal="Need github repo access.")
        ],
    )
    cleared = auto_clear_resolved_confirmations(
        model, ConfirmationResolutionSignals(repo_access_ready=False), "2026-07-14T00:00:00"
    )
    assert cleared == []
    assert [p.id for p in model.pending_user_confirmation] == ["p-access"]


def test_intent_change_and_major_decision_never_auto_cleared():
    """Human-decision items are NEVER auto-removed, even with every signal set."""
    model = ProblemModel(
        pending_user_confirmation=[
            _pending("p-intent", "intent_change", proposal="Pivot to a Claude API product."),
            _pending("p-major", "major_decision", proposal="Migrate the github repo."),
        ],
    )
    cleared = auto_clear_resolved_confirmations(
        model,
        ConfirmationResolutionSignals(
            llm_credentials_ready=True, repo_access_ready=True
        ),
        "2026-07-14T00:00:00",
    )
    assert cleared == []
    assert {p.id for p in model.pending_user_confirmation} == {"p-intent", "p-major"}


def test_auto_clear_is_idempotent_and_mixed():
    """Clears only the eligible/resolved item; second run is a no-op."""
    model = ProblemModel(
        pending_user_confirmation=[
            _pending("p-cred", "credentials_needed", proposal="Anthropic OAuth token needed."),
            _pending("p-intent", "intent_change", proposal="Change the focus."),
        ],
    )
    signals = ConfirmationResolutionSignals(llm_credentials_ready=True)
    first = auto_clear_resolved_confirmations(model, signals, "2026-07-14T00:00:00")
    assert first == ["p-cred"]
    rev_after_first = model.revision
    second = auto_clear_resolved_confirmations(model, signals, "2026-07-14T01:00:00")
    assert second == []
    assert model.revision == rev_after_first  # no spurious bump on the no-op
    assert [p.id for p in model.pending_user_confirmation] == ["p-intent"]


def test_auto_clear_empty_model_is_noop():
    model = ProblemModel()
    before = model.model_dump()
    cleared = auto_clear_resolved_confirmations(
        model,
        ConfirmationResolutionSignals(llm_credentials_ready=True, repo_access_ready=True),
        "2026-07-14T00:00:00",
    )
    assert cleared == []
    assert model.model_dump() == before


def test_alias_forgery_rejected_at_existence_check():
    """A forged alias in the agent-writable ``absorbs`` list — without a
    corresponding platform forwarding marker in ``proof_records`` — does NOT
    resolve through ``working_capability_names`` at ANY of the three consumer
    seams.

    This pinning test closes the alias-forgery bypass (existence/authority alias
    divergence): formerly, ``working_capability_names`` read ``absorbs`` from the
    agent-writable problem_model, so a forged entry like:
      ``{"name": "bogus", "status": "working", "absorbs": ["real-proven-cap"]}``
    would include ``"real-proven-cap"`` in the working set, letting a need for
    that capability satisfy the existence check without any platform-recorded
    proof relationship. Now only platform-ledger forwarding markers
    (``absorbed_into``) contribute alias names.
    """
    from app.capability_registry import (
        PLATFORM_WORKING_CAPABILITIES,
        registry_satisfies_capability_need,
        working_capability_names,
    )

    # An agent writes a new capability entry with a forged absorbs that names a
    # (possibly real) capability — but there is NO platform forwarding marker
    # for this relationship.
    forged_model = {
        "capabilities": [
            {
                "name": "my-forged-capability",
                "status": "working",
                "absorbs": ["real-proven-capability"],
            },
        ],
    }

    # SCENARIO 1: No proof_records at all → no alias resolution.
    working = working_capability_names(forged_model)
    # The forged capability's own name IS in the working set (it's declared working).
    assert "my-forged-capability" in working
    # But the forged alias name is NOT included — no platform markers exist.
    assert "real-proven-capability" not in working
    # Therefore the existence check for the forged alias fails.
    assert not registry_satisfies_capability_need(
        {"kind": "capability", "ref": "real-proven-capability"}, working
    )

    # SCENARIO 2: proof_records is provided but contains NO forwarding markers
    # for the forged alias → still not resolved.
    working_with_ledger = working_capability_names(
        forged_model,
        proof_records={
            # Some unrelated entries exist in the ledger
            "unrelated-cap": [
                {"proof_kind": "named_test", "proven": True, "task_id": "t-1"}
            ],
        },
    )
    assert "my-forged-capability" in working_with_ledger
    assert "real-proven-capability" not in working_with_ledger
    assert not registry_satisfies_capability_need(
        {"kind": "capability", "ref": "real-proven-capability"}, working_with_ledger
    )

    # SCENARIO 3: Even when some of the working set is PLATFORM capabilities,
    # the forged alias still does not resolve.
    assert "real-proven-capability" not in PLATFORM_WORKING_CAPABILITIES
    working_platform = working_capability_names(forged_model)
    # Platform capabilities are unaffected.
    for plat in PLATFORM_WORKING_CAPABILITIES:
        assert plat in working_platform
    # Forged alias is still absent.
    assert "real-proven-capability" not in working_platform


# ---------------------------------------------------------------------------
# apply_agent_update — packer/view marker sanitization (regression for
# poisoned stored problem_model containing "truncated": true)
# ---------------------------------------------------------------------------


def test_apply_agent_update_poisoned_current_raw_with_truncated_self_heals():
    """A stored problem_model containing 'truncated': true at the top level
    (a stale packer marker that was somehow persisted) must not block every
    subsequent agent write with 400.  apply_agent_update strips unknown extra
    top-level keys from current_raw before parsing so the write succeeds and
    the persisted output contains no marker keys."""
    current = {
        "revision": 5,
        "truncated": True,  # poisoning marker
        "intent": {"purpose": "Some purpose.", "current_focus": ""},
    }
    proposed = {
        "intent": {"purpose": "Updated purpose.", "current_focus": "Focus."},
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-07-21T10:00:00")
    # Write succeeds; revision incremented from 5
    assert new_raw["revision"] == 6
    assert new_raw["intent"]["purpose"] == "Updated purpose."
    # Marker key must not appear in the persisted output
    assert "truncated" not in new_raw


def test_apply_agent_update_proposed_with_truncated_stripped_not_rejected():
    """An agent that echoes back a packer-truncated view (carrying 'truncated':
    true in proposed_raw) must have the key silently stripped rather than
    receiving a permanent 400."""
    current = {
        "revision": 2,
        "intent": {"purpose": "Stable purpose.", "current_focus": ""},
    }
    proposed = {
        "truncated": True,  # agent echoed back packer view marker
        "intent": {"purpose": "Stable purpose.", "current_focus": "New focus."},
    }
    new_raw, _diff = apply_agent_update(current, proposed, "2026-07-21T10:00:00")
    assert new_raw["revision"] == 3
    assert "truncated" not in new_raw


def test_apply_agent_update_both_current_and_proposed_poisoned_succeeds():
    """When both the stored model AND the proposed payload carry 'truncated': true
    (worst-case: poisoned DB row + agent echoing the truncated view), both are
    sanitized and the write succeeds with a clean output model."""
    current = {
        "revision": 10,
        "truncated": True,
        "intent": {"purpose": "Old purpose.", "current_focus": ""},
    }
    proposed = {
        "truncated": True,
        "intent": {"purpose": "Fresh purpose.", "current_focus": ""},
    }
    new_raw, diff = apply_agent_update(current, proposed, "2026-07-21T11:00:00")
    assert new_raw["revision"] == 11
    assert new_raw["intent"]["purpose"] == "Fresh purpose."
    assert "truncated" not in new_raw


# ---------------------------------------------------------------------------
# sanitize_problem_model_raw
# ---------------------------------------------------------------------------


def test_sanitize_problem_model_raw_strips_packer_markers():
    """All three packer markers are removed by sanitize_problem_model_raw."""
    raw = {
        "revision": 5,
        "intent": {"purpose": "p", "current_focus": ""},
        "truncated": True,
        "_agent_view_floor_applied": "gap-abc",
        "_gap_memory_trimmed": True,
    }
    result = sanitize_problem_model_raw(raw)
    assert "truncated" not in result
    assert "_agent_view_floor_applied" not in result
    assert "_gap_memory_trimmed" not in result
    # Known fields are preserved
    assert result["revision"] == 5
    assert result["intent"] == {"purpose": "p", "current_focus": ""}


def test_sanitize_problem_model_raw_strips_unknown_keys():
    """Unknown top-level keys (beyond packer markers) are also removed."""
    raw = {
        "revision": 1,
        "some_unknown_field": "should be gone",
        "_another_directive": [1, 2, 3],
    }
    result = sanitize_problem_model_raw(raw)
    assert "some_unknown_field" not in result
    assert "_another_directive" not in result
    assert result["revision"] == 1


def test_sanitize_problem_model_raw_preserves_all_known_keys():
    """All ProblemModel fields pass through sanitize_problem_model_raw unchanged."""
    from app.problem_model import ProblemModel
    known_fields = set(ProblemModel.model_fields)
    raw = {k: None for k in known_fields}
    raw["revision"] = 3
    result = sanitize_problem_model_raw(raw)
    # Every known field is present in the output
    for field in known_fields:
        assert field in result
    # No unexpected extras
    assert set(result.keys()) == known_fields


def test_sanitize_problem_model_raw_empty_dict():
    """Empty dict is returned unchanged (no crash, no phantom keys)."""
    result = sanitize_problem_model_raw({})
    assert result == {}


def test_sanitize_problem_model_raw_output_is_pydantic_parseable():
    """sanitize_problem_model_raw output round-trips through ProblemModel cleanly."""
    raw = {
        "revision": 2,
        "truncated": True,
        "_agent_view_floor_applied": "g-1",
        "intent": {"purpose": "test purpose", "current_focus": "current"},
        "gaps": [],
        "capabilities": [],
    }
    sanitized = sanitize_problem_model_raw(raw)
    # Must not raise ValidationError
    model = parse_problem_model(sanitized)
    assert model.revision == 2
    assert model.intent.purpose == "test purpose"
