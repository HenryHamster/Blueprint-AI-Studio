"""Problem Model — the strategic layer's durable understanding of a project.

This replaces the old free-form ``project.vision`` blob and scattered
strategic state (design-doc ``notes/strategic-context``, per-direction
``research_context`` as the primary narrative). It is a structured model
the strategic agent *maintains* across cycles rather than regenerating
from scratch.

The shape is deliberately universal: same schema holds for a novel
experimental project (Blueprint self-improvement) and a mature structured
codebase (a SaaS backend). The content differs; the form does not.

Top-level elements:
  - intent: the human's goal (purpose + current_focus)
  - context: environmental framing (project type, team, users, cadence)
  - constraints: user-stated rules and red lines
  - capabilities: what the codebase currently does (functional, not structural)
  - gaps: specific deltas between intent and capabilities — where memory lives
  - archived_gaps: closed/abandoned gaps, retained for reference
  - friction: recurring blockers observed across moves
  - pending_user_confirmation: external-blocker queue (credentials, access, major pivots)

Directions remain first-class entities in the DB. Each direction carries
a ``gap_id`` pointer linking it to the gap it attempts to close; moves
are a projected view over directions, not a separate store.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Intent & context
# ---------------------------------------------------------------------------

SourceType = Literal[
    "user_message",
    "friction_promotion",
    "agent_observation",
    "self_reflection",
    "shipped_change",
    "direction_outcome",
]


class Source(BaseModel):
    """Where a piece of the problem model came from.

    Citations are universal — the strategic agent should be able to
    say "this capability is ``working`` because PR #42 merged" or
    "this gap exists because user said ..." instead of asserting facts
    without provenance. Used on Gap, Intent, Capability, Friction.
    """

    type: SourceType
    message_id: str | None = None
    """ProjectMessage id when type=user_message."""

    excerpt: str | None = None
    """First ~120 chars of the originating message / observation."""

    friction_id: str | None = None
    """Friction id when type=friction_promotion."""

    direction_id: str | None = None
    """Direction id when type=direction_outcome."""

    pr_number: int | None = None
    """PR number when type=shipped_change."""


class Intent(BaseModel):
    """What the human is trying to achieve."""

    purpose: str = Field(default="")
    """Long-term stable statement — what the project is *for*."""

    current_focus: str = Field(default="")
    """What we're actively working toward right now. Updates as phases complete."""

    source: Source | None = None
    """Where the most recent intent change came from — cite the user message
    or reflection that drove it. Full change history lives in
    ``ProblemModel.revision_log``."""


class ProjectContext(BaseModel):
    """Environmental framing that shapes how work is approached.

    A startup and a research lab take very different risks for the same
    move. Capturing this up-front lets the strategic agent weigh decisions
    without re-deriving it from stats every cycle.
    """

    project_type: str | None = None
    """e.g., ``startup``, ``research``, ``product``, ``personal``, ``enterprise``, ``internal-tool``."""

    users: str | None = None
    """Who this is for — one-line description."""

    team: str | None = None
    """Team size and shape — ``solo``, ``3 engineers + 1 PM``, etc."""

    deploy_cadence: str | None = None
    """How changes reach users — ``continuous``, ``weekly``, ``quarterly release``, ``sporadic``."""

    notes: str | None = None
    """Anything else that shapes decisions — risk tolerance, compliance, etc."""


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

class Capability(BaseModel):
    """A functional thing the codebase currently does (or plans to)."""

    name: str
    status: Literal["working", "partial", "planned"]
    notes: str | None = None
    source: Source | None = None
    """Where the current status assertion came from — e.g., a shipped PR
    or a direction outcome that established the capability."""


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------

GapSize = Literal["small", "medium", "large"]
GapPriority = Literal["high", "medium", "low"]
GapStatus = Literal["active", "closed", "abandoned", "blocked"]


class GapHistoryEvent(BaseModel):
    """Single event on a gap's append-only history log."""

    ts: str
    """ISO-8601 timestamp."""

    event: str
    """Short event tag — ``opened``, ``priority_raised``, ``move_attached``, etc."""

    note: str | None = None


class Gap(BaseModel):
    """A specific delta between intent and capabilities.

    Gaps are where cross-cycle memory lives: each gap accumulates history
    across the cycles that touch it, so the strategic agent sees the
    full arc of attempts rather than re-deriving it from task stats.

    When a gap closes or is abandoned, it moves to ``ProblemModel.archived_gaps``
    instead of staying in the active list.
    """

    id: str
    name: str
    description: str = ""
    evidence: str = ""

    size: GapSize = "medium"
    priority: GapPriority = "medium"
    status: GapStatus = "active"

    opened: str = ""
    """ISO-8601 when the gap was first opened."""

    closed: str | None = None
    """ISO-8601 when the gap was closed/abandoned, if applicable."""

    tag: str | None = None
    """Optional tag (e.g., ``self_improvement``) for filtering and reporting."""

    source: Source | None = None
    """Where this gap came from — lets the agent cite user messages
    and report user-request status across cycles."""

    active_moves: list[str] = Field(default_factory=list)
    """direction_ids currently working on this gap."""

    past_moves: list[str] = Field(default_factory=list)
    """direction_ids that previously worked on this gap (merged or failed)."""

    history: list[GapHistoryEvent] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Friction
# ---------------------------------------------------------------------------

FrictionStatus = Literal["active", "mitigated", "resolved"]


class Friction(BaseModel):
    """A recurring blocker observed across multiple moves.

    Gaps are things we *want to build*; friction is things *in the way*.
    When friction grows large enough to warrant dedicated work, the agent
    promotes it to a gap autonomously (no user gate) and links the
    resulting gap here.
    """

    id: str
    name: str
    description: str = ""
    seen_in: list[str] = Field(default_factory=list)
    """direction_ids where this friction was observed."""

    status: FrictionStatus = "active"
    mitigations_tried: list[str] = Field(default_factory=list)

    source: Source | None = None
    """Where this friction was first observed — usually a direction_outcome
    or agent_observation."""

    promoted_to_gap_id: str | None = None
    """Set when the agent promoted this friction to a first-class gap."""


# ---------------------------------------------------------------------------
# Pending user confirmation
# ---------------------------------------------------------------------------

PendingConfirmationType = Literal[
    "credentials_needed",
    "access_needed",
    "intent_change",
    "major_decision",
]


class PendingConfirmation(BaseModel):
    """Something the agent cannot resolve on its own and needs the user for.

    Scoped deliberately: credentials/secrets, access grants, intent shifts,
    and major pivots only. *Not* for "should I do X next" coding questions —
    those the agent resolves by executing.
    """

    id: str
    type: PendingConfirmationType
    proposal: str
    rationale: str = ""
    created_at: str = ""


# ---------------------------------------------------------------------------
# Revision log
# ---------------------------------------------------------------------------

class RevisionLogEntry(BaseModel):
    """One entry describing what changed in a given revision.

    Not auto-injected into prompts — accessible via tool call when the
    strategic agent explicitly wants historical deltas.
    """

    revision: int
    ts: str
    summary: str = ""
    changes: dict[str, Any] = Field(default_factory=dict)
    """Structured diff: e.g., ``{"gaps_opened": ["g-x"], "gaps_closed": ["g-y"]}``."""


# ---------------------------------------------------------------------------
# Top-level model
# ---------------------------------------------------------------------------

class ProblemModel(BaseModel):
    """The strategic layer's durable model of a project.

    Stored as JSONB in ``project.problem_model``. An empty instance is a
    valid state for brand-new projects — the strategic agent bootstraps
    the model during the first cycle.
    """

    model_config = ConfigDict(extra="forbid")

    revision: int = 0
    last_updated: str | None = None
    last_updated_by: Literal["agent", "user"] | None = None

    intent: Intent = Field(default_factory=Intent)
    context: ProjectContext = Field(default_factory=ProjectContext)

    constraints: list[str] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)

    gaps: list[Gap] = Field(default_factory=list)
    archived_gaps: list[Gap] = Field(default_factory=list)

    friction: list[Friction] = Field(default_factory=list)

    pending_user_confirmation: list[PendingConfirmation] = Field(default_factory=list)

    revision_log: list[RevisionLogEntry] = Field(default_factory=list)
    """Last N revisions' deltas. Capped at REVISION_LOG_MAX."""


REVISION_LOG_MAX = 10
"""Cap on revision_log length — older entries are dropped."""


SELF_REFLECTION_LOG_MAX = 20
"""Cap on project.self_reflection_log length — older entries are dropped."""


class SelfReflectionEntry(BaseModel):
    """Agent-written reflection about what was hard this cycle.

    Appended to ``project.self_reflection_log`` (a separate JSONB column,
    not part of the problem model). Over time the log accumulates the
    agent's own complaints about its limitations — the raw material for
    self-improvement gaps on self-repo projects.

    Deliberately low-structure: this is a diary, not an analysis. Prose
    fields with soft caps so the agent can be honest without being verbose.
    """

    ts: str
    cycle_task_id: str | None = None
    """Task id of the strategic cycle that produced this entry, for audit."""

    what_was_hard: str = ""
    """What the agent struggled with this cycle — missing context, unclear
    instruction, schema friction, etc."""

    missing_context: str = ""
    """Specific data the agent wished it had access to."""

    proposed_self_improvement: str = ""
    """Optional — the agent's own suggestion for a gap/direction that would
    address the struggle. Not required; sometimes struggles aren't
    actionable yet."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_problem_model(raw: dict | None) -> ProblemModel:
    """Safely parse a JSONB blob into a ProblemModel.

    Returns an empty ProblemModel if the input is None or empty. Raises
    pydantic ValidationError on malformed non-empty input — callers that
    need fail-open behavior should wrap this themselves.
    """
    if not raw:
        return ProblemModel()
    return ProblemModel.model_validate(raw)


def archive_closed_gaps(model: ProblemModel) -> ProblemModel:
    """Move any gaps whose status is ``closed`` or ``abandoned`` from
    ``gaps`` into ``archived_gaps``. Returns the same instance for
    chaining; mutation is in place."""
    still_active: list[Gap] = []
    for gap in model.gaps:
        if gap.status in ("closed", "abandoned"):
            model.archived_gaps.append(gap)
        else:
            still_active.append(gap)
    model.gaps = still_active
    return model


def append_revision_log(
    model: ProblemModel,
    summary: str,
    changes: dict[str, Any] | None = None,
) -> None:
    """Append a revision log entry and enforce the cap.

    ``model.revision`` and ``model.last_updated`` should already reflect
    the new revision before calling this.
    """
    entry = RevisionLogEntry(
        revision=model.revision,
        ts=model.last_updated or "",
        summary=summary,
        changes=changes or {},
    )
    model.revision_log.append(entry)
    if len(model.revision_log) > REVISION_LOG_MAX:
        del model.revision_log[0 : len(model.revision_log) - REVISION_LOG_MAX]


def append_self_reflection(
    log_raw: list | None,
    entry: SelfReflectionEntry,
) -> list[dict]:
    """Append a reflection entry to the project's self-reflection log,
    enforcing :data:`SELF_REFLECTION_LOG_MAX`. Returns the new list ready
    for JSONB persistence."""
    log = list(log_raw or [])
    log.append(entry.model_dump())
    if len(log) > SELF_REFLECTION_LOG_MAX:
        log = log[-SELF_REFLECTION_LOG_MAX:]
    return log


def is_empty(model: ProblemModel) -> bool:
    """True when the model has no meaningful content — useful for deciding
    whether to run an initial bootstrap cycle."""
    return (
        not model.intent.purpose
        and not model.intent.current_focus
        and not model.capabilities
        and not model.gaps
        and not model.archived_gaps
        and not model.friction
        and not model.constraints
    )


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------

def _compute_diff(current: ProblemModel, proposed: ProblemModel) -> dict[str, Any]:
    """Compare two model states and return a compact change summary.

    The output is intended for revision_log entries, not full reconstruction.
    """
    cur_gap_ids = {g.id for g in current.gaps}
    prop_gap_ids = {g.id for g in proposed.gaps}
    prop_closed_ids = {
        g.id for g in proposed.gaps
        if g.status in ("closed", "abandoned")
    }

    diff: dict[str, Any] = {}
    opened = sorted(prop_gap_ids - cur_gap_ids)
    if opened:
        diff["gaps_opened"] = opened
    closed_now = sorted(prop_closed_ids)
    if closed_now:
        diff["gaps_closed"] = closed_now

    cur_friction_ids = {f.id for f in current.friction}
    prop_friction_ids = {f.id for f in proposed.friction}
    new_friction = sorted(prop_friction_ids - cur_friction_ids)
    if new_friction:
        diff["friction_observed"] = new_friction
    resolved_friction = sorted(
        f.id for f in proposed.friction if f.status == "resolved"
    )
    if resolved_friction:
        diff["friction_resolved"] = resolved_friction

    intent_changed = (
        current.intent.purpose != proposed.intent.purpose
        or current.intent.current_focus != proposed.intent.current_focus
    )
    if intent_changed:
        diff["intent_changed"] = True

    cur_cap_status = {c.name: c.status for c in current.capabilities}
    prop_cap_status = {c.name: c.status for c in proposed.capabilities}
    cap_changes = [
        name for name, status in prop_cap_status.items()
        if cur_cap_status.get(name) != status
    ]
    if cap_changes:
        diff["capabilities_changed"] = sorted(cap_changes)

    return diff


def _diff_summary(diff: dict[str, Any]) -> str:
    """Render a diff dict as a one-line human-readable summary."""
    parts: list[str] = []
    if opened := diff.get("gaps_opened"):
        parts.append(f"opened {len(opened)} gap(s)")
    if closed := diff.get("gaps_closed"):
        parts.append(f"closed {len(closed)} gap(s)")
    if friction := diff.get("friction_observed"):
        parts.append(f"observed {len(friction)} friction")
    if resolved := diff.get("friction_resolved"):
        parts.append(f"resolved {len(resolved)} friction")
    if diff.get("intent_changed"):
        parts.append("intent updated")
    if caps := diff.get("capabilities_changed"):
        parts.append(f"{len(caps)} capability status changes")
    return "; ".join(parts) if parts else "no structural changes"


# ---------------------------------------------------------------------------
# Agent update application
# ---------------------------------------------------------------------------

def apply_agent_update(
    current_raw: dict | None,
    proposed_raw: dict,
    now_iso: str,
) -> tuple[dict, dict]:
    """Apply an agent-proposed problem_model update to the current state.

    Validates ``proposed_raw`` through :class:`ProblemModel`, bumps the
    revision and timestamp, preserves existing ``archived_gaps`` and
    ``revision_log``, auto-archives any gaps that are now closed or
    abandoned, and appends a revision-log entry summarizing the change.

    Returns a tuple of ``(new_raw_dict, diff_summary)``:
      - ``new_raw_dict`` is ready for JSONB persistence on ``project.problem_model``
      - ``diff_summary`` is the structured change dict for downstream logging

    Raises :class:`pydantic.ValidationError` if ``proposed_raw`` is malformed.
    Callers should catch and keep the current state on validation failure.

    Note: concurrent user edits are not yet merged carefully. If the DB
    revision advanced while the cycle ran, the agent's write will overwrite
    those edits. Future work: field-level merge like the legacy vision
    update path does for purpose/success_criteria.
    """
    proposed = ProblemModel.model_validate(proposed_raw)
    current = parse_problem_model(current_raw)

    proposed.revision = current.revision + 1
    proposed.last_updated = now_iso
    proposed.last_updated_by = "agent"

    proposed.archived_gaps = list(current.archived_gaps) + list(proposed.archived_gaps)
    proposed.revision_log = list(current.revision_log)

    # Compute diff before archival so closed gaps are still visible on
    # proposed.gaps — otherwise _compute_diff can't see what got closed
    # this cycle.
    diff = _compute_diff(current, proposed)

    archive_closed_gaps(proposed)

    append_revision_log(proposed, summary=_diff_summary(diff), changes=diff)

    return proposed.model_dump(), diff
