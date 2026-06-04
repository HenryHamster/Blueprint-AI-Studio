"""Tests for the structured problem-model schema.

These tests are pure schema validation — they don't touch the DB or
the orchestrator. Integration with claim_task / strategic cycles lives
in later tests.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.problem_model import (
    GAP_HISTORY_DURABLE_MAX,
    GAP_PAST_MOVES_DURABLE_MAX,
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
    append_revision_log,
    append_self_reflection,
    apply_agent_update,
    apply_user_intent_patch,
    archive_closed_gaps,
    compute_model_size_signal,
    is_empty,
    parse_problem_model,
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


def test_apply_agent_update_operator_untouched_field_takes_agent_value():
    """When the operator did NOT touch a field (current == baseline), the agent's
    new value applies even with a baseline supplied — no ledger entry."""
    baseline = _operator_seeded_current()
    current = _operator_seeded_current()  # identical to baseline → untouched
    proposed = {
        "intent": {
            "purpose": "Refined: reliable billing, fast onboarding.",
            "current_focus": "x",
        },
    }
    new_raw, diff = apply_agent_update(
        current, proposed, "2026-04-21T12:00:00", baseline_raw=baseline
    )
    assert new_raw["intent"]["purpose"] == "Refined: reliable billing, fast onboarding."
    assert diff.get("intent_changed") is True
    # No conflict → no ledger key surfaced (byte-identical to pre-merge output).
    assert "rejected_agent_edits" not in new_raw


def test_apply_agent_update_agent_only_change_to_operator_field_applies():
    """Operator did not edit (current == baseline) but the agent changed an
    operator-owned field: the agent's value applies, no rejection."""
    baseline = _operator_seeded_current()
    current = _operator_seeded_current()
    proposed = {
        "intent": {"purpose": "Ship a reliable billing system.", "current_focus": "x"},
        "context": {"project_type": "scale-up", "users": "SMBs"},
        "constraints": ["No PII in logs", "GDPR compliant", "SOC2"],
    }
    new_raw, _ = apply_agent_update(
        current, proposed, "2026-04-21T12:00:00", baseline_raw=baseline
    )
    assert new_raw["context"]["project_type"] == "scale-up"
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
