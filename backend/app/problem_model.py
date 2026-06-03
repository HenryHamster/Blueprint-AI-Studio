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

    backing_paths: list[str] = Field(default_factory=list)
    """Integration-branch file paths that back this capability. Optional and
    default-empty so every stored model and older agent payload validates
    unchanged. Used by strategic payload assembly to verify (advisory, never
    mutating the durable model) whether the code a status claims actually
    exists — a capability whose backing files are all missing is framed as
    absent/unverified, mixed presence as partial. Mirrors the design-doc
    ``systems/<id>.modules`` verification."""


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

    rejected_agent_edits: list[dict[str, Any]] = Field(default_factory=list)
    """Operator-wins 3-way-merge ledger.

    When a strategic cycle's agent write lands on an operator-owned field
    (``intent.purpose`` / ``context`` / ``constraints``) the operator edited
    mid-cycle, the operator's value is kept and the overridden agent value is
    appended here (see :func:`_merge_operator_owned_fields`). The ledger is
    agent-owned bookkeeping carried in the strategic payload so the next cycle
    can reconcile what it tried to change. Capped at
    :data:`REJECTED_AGENT_EDITS_MAX`; older entries roll off.

    Absent from :func:`apply_agent_update` output until at least one edit is
    actually overridden, so the no-conflict / no-baseline path stays
    byte-identical to the pre-merge behavior.
    """


REVISION_LOG_MAX = 10
"""Cap on revision_log length — older entries are dropped."""

REJECTED_AGENT_EDITS_KEY = "rejected_agent_edits"
"""Field/key name for the operator-wins merge ledger on the problem model."""

REJECTED_AGENT_EDITS_MAX = 30
"""Cap on the rejected-agent-edits ledger — older entries roll off. Mirrors
``vision_merge.REJECTED_EDITS_MAX`` so the two ledgers behave identically."""


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
        g.id for g in proposed.gaps if g.status in ("closed", "abandoned")
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
        name
        for name, status in prop_cap_status.items()
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


class ColdStartWipeError(ValueError):
    """Raised when an agent update would silently wipe a non-empty problem_model.

    The strategic payload is assembled under a byte budget; under pressure the
    ``problem_model`` section can be dropped or heavily trimmed. The agent then
    believes it is the first cycle and writes a fresh cold-start ("bootstrap")
    model from scratch — empty ``capabilities``/``gaps``/``friction`` — which a
    wholesale replacement would use to erase the mature durable model.

    :func:`apply_agent_update` refuses that destructive write loudly by raising
    this error instead of overwriting. Callers surface it (the API path returns
    a 409 with the actionable message; the strategic-completion fallback logs a
    warning and keeps the prior state) so the wipe is never silent. The agent
    should fetch the current model and edit it incrementally rather than replace
    it from scratch.
    """


def _is_bootstrap_over_mature(current: ProblemModel, proposed: ProblemModel) -> bool:
    """True for the catastrophic bootstrap-over-mature signature.

    Fires only when the proposed update reduces ALL THREE agent-owned
    collections (``capabilities``, ``gaps``, ``friction``) to empty while the
    current durable model already holds real content in at least one of them.

    Keyed deliberately on the all-collections-to-empty case, not per-item
    omission: an agent retiring a gap marks it ``closed``/``abandoned`` (so the
    gap is still present in the submission and gets archived), and may prune
    individual capabilities — those leave at least one collection non-empty and
    do NOT trip this guard. Only a wholesale cold-start replacement of an
    already-mature model matches.

    Operator-owned fields (``intent``/``context``/``constraints``) are protected
    separately by :func:`_preserve_operator_intent` /
    :func:`_merge_operator_owned_fields`; ``archived_gaps`` and ``revision_log``
    are carried forward unconditionally — none of them count toward "mature"
    here, so a model holding only operator intent or only archived history is
    not treated as a wipe target.
    """
    proposed_collections_empty = (
        not proposed.capabilities and not proposed.gaps and not proposed.friction
    )
    current_has_content = bool(
        current.capabilities or current.gaps or current.friction
    )
    return proposed_collections_empty and current_has_content


# ---------------------------------------------------------------------------
# User-editable intent updates
# ---------------------------------------------------------------------------

USER_EDITABLE_FIELDS = ("intent", "context", "constraints")
"""Problem-model fields a human may write directly.

Everything else (gaps, capabilities, friction, archived_gaps,
pending_user_confirmation, revision_log, revision metadata) is agent-owned and
must not be overwritten by user-facing write paths. The onboarding
goal/steering capture seeds the user's strategic intent through this same
subset (see ``admin_onboard._seed_strategic_intent``); future user-facing
write paths should reuse it so the editable subset stays consistent.
"""


def apply_user_intent_patch(
    current_raw: dict | None,
    patch: dict[str, Any],
    now_iso: str,
) -> ProblemModel:
    """Merge a user-supplied subset into the current problem model.

    Only :data:`USER_EDITABLE_FIELDS` are accepted; any other key raises
    :class:`ValueError` so callers can surface a 400 instead of silently
    letting a user overwrite agent-owned state. Bumps the revision, stamps
    ``last_updated``/``last_updated_by='user'``, and returns the validated
    model (callers ``model_dump()`` it for JSONB persistence).

    Used by ``admin_onboard._seed_strategic_intent`` to merge optional
    onboarding steering; callers pre-filter to the editable subset when they
    want unknown keys ignored rather than rejected.

    An empty ``patch`` is a valid no-op edit (still a user-attributed
    revision bump), preserving today's behavior for users who supply no
    steering. Raises :class:`pydantic.ValidationError` if the merged result
    is malformed.
    """
    invalid = sorted(set(patch) - set(USER_EDITABLE_FIELDS))
    if invalid:
        raise ValueError(
            f"Cannot edit agent-owned problem_model fields: {invalid}. "
            f"Editable fields: {sorted(USER_EDITABLE_FIELDS)}"
        )
    current = parse_problem_model(current_raw)
    merged = current.model_dump()
    merged.update(patch)
    validated = ProblemModel.model_validate(merged)
    validated.revision = current.revision + 1
    validated.last_updated = now_iso
    validated.last_updated_by = "user"
    return validated


def _context_is_blank(context: ProjectContext) -> bool:
    """True when every ``ProjectContext`` field is empty/unset.

    Used to detect when an agent's proposed model carries a default-blank
    context that would silently erase operator-seeded environmental framing.
    """
    return context.model_dump(exclude_none=True) == {}


def _preserve_operator_intent(current: ProblemModel, proposed: ProblemModel) -> None:
    """Carry operator-seeded strategic intent forward when the agent's
    proposed model omits or blanks it.

    The operator seeds ``intent.purpose`` (with a ``user_message`` source),
    ``context``, and ``constraints`` at onboarding via
    ``admin_onboard._seed_strategic_intent`` (the same user-editable subset
    :func:`apply_user_intent_patch` accepts). A strategic agent that submits a
    full ``set_problem_model`` replacement can accidentally drop these fields,
    silently erasing the operator's words from the next worker/plan prompt's
    Strategic Intent section.

    This fills operator-owned fields back from ``current`` whenever ``proposed``
    leaves them empty, mutating ``proposed`` in place. It does NOT block a
    genuine, non-empty agent change to an operator-owned field — that still
    applies and still bumps the revision. The agent-owned ``current_focus``
    advances freely and is never preserved here.

    The escape hatch for an agent that genuinely intends to *change*
    operator-owned intent is the ``pending_user_confirmation`` channel
    (``type='intent_change'``), not a silent overwrite — see
    :class:`PendingConfirmation`.
    """
    # ``intent.purpose`` is operator-owned only when the prior change was
    # attributed to a user message; agent-authored purposes may be retired.
    purpose_was_operator = (
        current.intent.source is not None
        and current.intent.source.type == "user_message"
    )
    if (
        purpose_was_operator
        and current.intent.purpose.strip()
        and not proposed.intent.purpose.strip()
    ):
        proposed.intent.purpose = current.intent.purpose
        # Restore the operator provenance unless the agent supplied its own.
        if proposed.intent.source is None:
            proposed.intent.source = current.intent.source

    # ``context`` / ``constraints`` are operator-owned when the prior model was
    # last written by the user (the onboarding seed stamps last_updated_by).
    operator_owned = purpose_was_operator or current.last_updated_by == "user"
    if (
        operator_owned
        and not _context_is_blank(current.context)
        and _context_is_blank(proposed.context)
    ):
        proposed.context = current.context.model_copy(deep=True)
    if operator_owned and current.constraints and not proposed.constraints:
        proposed.constraints = list(current.constraints)


def _merge_operator_owned_fields(
    baseline: ProblemModel,
    current: ProblemModel,
    proposed: ProblemModel,
    now_iso: str,
) -> list[dict[str, Any]]:
    """3-way, operator-wins field-level merge for the operator-owned subset.

    Generalizes the vision 3-way merge (:mod:`app.vision_merge`) to the three
    operator-owned problem-model fields — ``intent.purpose``, ``context``,
    ``constraints`` (:data:`USER_EDITABLE_FIELDS`). This protects a *non-blank*
    operator edit made WHILE a strategic cycle is in flight: the agent computes
    its proposed model from a snapshot taken at cycle spawn and may write a
    different non-blank value for the same field, which the blank-only
    :func:`_preserve_operator_intent` backstop would let through silently.

    Three inputs per field:
      - ``baseline`` — the value the agent saw at cycle spawn (snapshot).
      - ``proposed`` (agent) — the value the agent returned at cycle end.
      - ``current`` — the value in the DB now (may carry a mid-cycle operator
        edit).

    Rule per field:
      - ``current == baseline`` (operator didn't touch it): take the agent's
        value (no-op here — ``proposed`` already holds it).
      - ``current != baseline`` (operator edited mid-cycle) AND
        ``proposed != baseline`` (agent also tried to change it): keep the
        operator's value and record the overridden agent value to the returned
        ledger so the next cycle can reconcile.
      - only the operator changed it: keep the operator's value.
      - only the agent changed it: take the agent's value.

    Mutates ``proposed`` in place to the resolved values; returns the (possibly
    empty) list of rejected agent edits for the caller to fold into the durable
    ledger. ``intent.purpose`` is treated as operator-owned only when the prior
    intent was attributed to a ``user_message`` source, matching
    :func:`_preserve_operator_intent`; otherwise the purpose is agent-authored
    and the agent overwrites it freely.
    """
    rejected: list[dict[str, Any]] = []

    def _record(field: str, operator_value: Any, agent_value: Any) -> None:
        rejected.append(
            {
                "at": now_iso,
                "field": field,
                "kind": "value_conflict",
                "operator_value": operator_value,
                "agent_value": agent_value,
            }
        )

    # intent.purpose — operator-owned only when the prior change came from a
    # user message; agent-authored purposes are not protected here.
    purpose_was_operator = (
        current.intent.source is not None
        and current.intent.source.type == "user_message"
    )
    if purpose_was_operator and current.intent.purpose != baseline.intent.purpose:
        if proposed.intent.purpose not in (
            baseline.intent.purpose,
            current.intent.purpose,
        ):
            _record(
                "intent.purpose", current.intent.purpose, proposed.intent.purpose
            )
        proposed.intent.purpose = current.intent.purpose
        # Keep operator provenance on the surviving purpose.
        proposed.intent.source = current.intent.source

    # context — a mid-cycle change of current vs baseline can only be an
    # operator edit (a single strategic cycle runs at a time), so operator wins.
    if current.context != baseline.context:
        if proposed.context not in (baseline.context, current.context):
            _record(
                "context",
                current.context.model_dump(exclude_none=True),
                proposed.context.model_dump(exclude_none=True),
            )
        proposed.context = current.context.model_copy(deep=True)

    # constraints — same operator-wins rule.
    if current.constraints != baseline.constraints:
        if proposed.constraints not in (baseline.constraints, current.constraints):
            _record(
                "constraints", list(current.constraints), list(proposed.constraints)
            )
        proposed.constraints = list(current.constraints)

    return rejected


def apply_agent_update(
    current_raw: dict | None,
    proposed_raw: dict,
    now_iso: str,
    *,
    baseline_raw: dict | None = None,
) -> tuple[dict, dict]:
    """Apply an agent-proposed problem_model update to the current state.

    Validates ``proposed_raw`` through :class:`ProblemModel`, bumps the
    revision and timestamp, preserves existing ``archived_gaps`` and
    ``revision_log``, carries forward operator-seeded strategic intent the
    agent omitted or blanked (see :func:`_preserve_operator_intent`),
    auto-archives any gaps that are now closed or abandoned, and appends a
    revision-log entry summarizing the change.

    Returns a tuple of ``(new_raw_dict, diff_summary)``:
      - ``new_raw_dict`` is ready for JSONB persistence on ``project.problem_model``
      - ``diff_summary`` is the structured change dict for downstream logging

    Raises :class:`pydantic.ValidationError` if ``proposed_raw`` is malformed.
    Callers should catch and keep the current state on validation failure.

    Raises :class:`ColdStartWipeError` when the current durable model is
    non-empty but the proposed update reduces all agent-owned collections
    (``capabilities``/``gaps``/``friction``) to empty — the bootstrap-over-mature
    signature (see :func:`_is_bootstrap_over_mature`). The destructive write is
    refused loudly rather than silently overwriting the mature model; callers
    surface the error and keep the current state.

    Operator intent is protected, not frozen: a genuine, explicit, non-empty
    agent change to ``intent.purpose`` / ``context`` / ``constraints`` still
    applies and still bumps the revision when the operator did NOT also touch
    that field. An agent that genuinely intends to *change* operator-owned
    intent should route that through the ``pending_user_confirmation`` channel
    (``type='intent_change'``) rather than overwriting it silently.

    Concurrent operator edits ARE merged when ``baseline_raw`` is supplied (the
    problem-model snapshot the cycle captured at spawn — see
    ``strategic_cycle.baseline_problem_model``). For the operator-owned subset,
    a field-level operator-wins 3-way merge runs (see
    :func:`_merge_operator_owned_fields`): if the operator edited a field
    mid-cycle to a new non-blank value while the agent also changed it, the
    operator's value wins and the agent's overridden value is recorded to the
    :data:`REJECTED_AGENT_EDITS_KEY` ledger for next-cycle reconciliation.
    Agent-owned fields (``current_focus``, gaps, capabilities, friction, …) are
    unaffected and still advance + bump the revision. When ``baseline_raw`` is
    None (legacy task with no cycle row, or no baseline resolvable), the merge
    is skipped and behavior is byte-identical to the blank-preservation-only
    path. The blank-preservation backstop runs in both cases.
    """
    proposed = ProblemModel.model_validate(proposed_raw)
    current = parse_problem_model(current_raw)

    # Refuse a cold-start wipe loudly: a non-empty durable model whose
    # agent-owned collections would all be reduced to empty is the
    # bootstrap-over-mature signature (the payload likely dropped the existing
    # model). Reject before any mutation so the prior state survives intact; the
    # agent must fetch and edit the model rather than replace it from scratch.
    if _is_bootstrap_over_mature(current, proposed):
        raise ColdStartWipeError(
            "Refusing to overwrite a non-empty problem_model with an empty "
            f"cold-start bootstrap: the current model holds "
            f"{len(current.capabilities)} capabilities, {len(current.gaps)} "
            f"gaps, and {len(current.friction)} friction entries, but the "
            "proposed update reduces all three agent-owned collections to "
            "empty. This is the bootstrap-over-mature signature — the payload "
            "likely dropped the existing model under byte pressure. Fetch the "
            "current problem_model (GET /api/agent/projects/{project_id}/"
            "problem-model or `./fetch problem-model`) and edit it "
            "incrementally instead of replacing it from scratch."
        )

    proposed.revision = current.revision + 1
    proposed.last_updated = now_iso
    proposed.last_updated_by = "agent"

    proposed.archived_gaps = list(current.archived_gaps) + list(proposed.archived_gaps)
    proposed.revision_log = list(current.revision_log)

    # 3-way operator-wins merge for operator-owned fields when a cycle baseline
    # is available. Runs BEFORE blank-preservation and the diff so a concurrent
    # operator edit is honored and reported faithfully.
    rejected_edits: list[dict[str, Any]] = []
    if baseline_raw is not None:
        baseline = parse_problem_model(baseline_raw)
        rejected_edits = _merge_operator_owned_fields(
            baseline, current, proposed, now_iso
        )

    # Carry operator-seeded intent forward before computing the diff so a
    # merely-preserved purpose is not falsely reported as intent_changed. This
    # is a complementary backstop to the 3-way merge above: the merge handles
    # non-blank concurrent edits; this fills agent omissions/blanks.
    _preserve_operator_intent(current, proposed)

    # Compute diff before archival so closed gaps are still visible on
    # proposed.gaps — otherwise _compute_diff can't see what got closed
    # this cycle.
    diff = _compute_diff(current, proposed)

    archive_closed_gaps(proposed)

    append_revision_log(proposed, summary=_diff_summary(diff), changes=diff)

    # Carry the durable rejected-agent-edits ledger forward and append any new
    # overrides, capped — mirrors archived_gaps/revision_log preservation. The
    # agent's own submission of this field is ignored (agent-readable, not
    # agent-writable bookkeeping).
    ledger = list(current.rejected_agent_edits) + rejected_edits
    proposed.rejected_agent_edits = ledger[-REJECTED_AGENT_EDITS_MAX:]

    result = proposed.model_dump()
    # Keep output byte-identical to the pre-merge behavior on the no-conflict /
    # no-baseline path: only surface the ledger key when it carries entries.
    if not result.get(REJECTED_AGENT_EDITS_KEY):
        result.pop(REJECTED_AGENT_EDITS_KEY, None)

    return result, diff
