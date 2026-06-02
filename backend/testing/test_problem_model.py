"""Tests for the structured problem-model schema.

These tests are pure schema validation — they don't touch the DB or
the orchestrator. Integration with claim_task / strategic cycles lives
in later tests.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.problem_model import (
    REVISION_LOG_MAX,
    SELF_REFLECTION_LOG_MAX,
    Capability,
    Friction,
    Gap,
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
