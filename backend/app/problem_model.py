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

import json
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

    absorbs: list[str] = Field(default_factory=list)
    """Old capability names this (surviving or newly-merged) capability absorbed
    during a strategist consolidation. Optional and default-empty so every
    stored model and older agent payload validates unchanged.

    Consolidating overlapping capabilities is destructive today: a capability
    name is an exact-match key for prerequisite resolution
    (``working_capability_names``), the off-registry advisory known-set
    (``registry_capability_names``), and durable proof/verdict records. Deleting
    the old names (via ``_deletions.capabilities``) while declaring them here on
    the survivor keeps those old names *resolving* — ``working_capability_names``
    and ``registry_capability_names`` fold an absorbed name into their output so
    a prerequisite that referenced it stays satisfied — WITHOUT re-rendering the
    absorbed names into the agent-facing vocabulary
    (``registry_capability_vocabulary`` reports survivor names only), which is
    how the consolidation actually shrinks the payload. The write path strips
    this key when empty so a model with no consolidation serializes
    byte-identically to before the field existed."""


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


class GapDeferral(BaseModel):
    """An acknowledged, intentional decision to NOT staff a gap this cycle.

    Lets the strategist record "this gap is real and still open, but we are
    deliberately not working on it right now" without fabricating a direction
    just to keep the gap from looking orphaned in the reconciliation view. An
    active gap carrying a ``deferral`` is *intentionally* without an active
    direction — distinct from a gap that drifted unstaffed by neglect.

    Default-empty and optional so every stored JSONB model and older agent
    payload validates unchanged. The agent clears it (sets ``deferral`` back to
    ``None`` via a model write) when it picks the gap back up.
    """

    rationale: str = ""
    """Why the gap is intentionally unstaffed this cycle."""

    hold_until: str | None = None
    """Optional ISO-8601 (or free-form milestone) marking when to revisit —
    e.g. a date, a version tag, or a condition the agent will re-evaluate."""

    created_at: str = ""
    """ISO-8601 when the deferral was recorded."""


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

    deferral: GapDeferral | None = None
    """Set when the gap is intentionally left unstaffed this cycle (see
    :class:`GapDeferral`). ``None`` means the gap is either being actively
    worked (has ``active_moves``) or is a genuine orphan — the reconciliation
    view distinguishes a deferred gap from a drifted one by this field. As a
    plain proposed-wins field it threads through :func:`_merge_gap` and
    ``apply_agent_update`` with no extra merge logic: the agent sets it to
    record a deferral and clears it (``None``) when it resumes the gap."""

    history_compacted_at: str | None = Field(default=None)
    """ISO-8601 watermark for history compaction.  Entries with ``ts`` at or
    before this timestamp are excluded from future union passes so a trimmed
    echo cannot resurrect compacted history.  Set via the ``_compactions``
    directive in :func:`apply_agent_update`.  ``None`` means no compaction
    has occurred; existing models validate unchanged."""

    past_moves_compacted_at: str | None = Field(default=None)
    """ISO-8601 watermark for past_moves compaction.  When a ``_compactions``
    directive sets this for a gap, ``_merge_gap`` uses replacement semantics
    for that write (``proposed.past_moves`` replaces the durable log rather
    than unioning).  The watermark survives subsequent writes and is monotonic
    (cannot be regressed by a stale directive)."""


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


GAP_HISTORY_DURABLE_MAX = 200
"""Cap on a surviving gap's durable ``history`` after write-back restore.

The strategic payload trims each active gap's ``history`` to only its most
recent few entries before the agent ever sees it (see
``strategic_spawning._GAP_HISTORY_KEEP``); :func:`_merge_gap`
unions the durable head back in on write-back so a round-tripped trimmed view
cannot truncate the log. This cap keeps the restored, append-only log bounded
in the JSONB column — generous (far above the payload trim) so it never loses
recent memory, but finite so it cannot grow without limit."""


GAP_PAST_MOVES_DURABLE_MAX = 200
"""Cap on a surviving gap's durable ``past_moves`` after write-back restore —
see :data:`GAP_HISTORY_DURABLE_MAX`."""

GAP_APPEND_ONLY_GC_FLOOR = 12
"""Per-active-gap floor kept by the byte GC for ``history`` / ``past_moves``."""


ARCHIVED_GAPS_DURABLE_MAX = 200
"""Hard cap on the durable ``archived_gaps`` collection — the only durable
collection that grows without limit forever.

Closed/abandoned gaps are moved into ``archived_gaps`` every cycle and the
prior archive is unioned forward on every agent write (see
:func:`apply_agent_update`), so without a backstop the JSONB column grows
unboundedly and eventually the model can no longer round-trip in a single
payload pass. This cap mirrors the append-log caps
(:data:`GAP_HISTORY_DURABLE_MAX` / :data:`GAP_PAST_MOVES_DURABLE_MAX`): it is
generous but finite, and eviction is oldest-first by ``closed``/``opened``
timestamp. ``archived_gaps`` is pure history — closed/abandoned, reference-only,
and individually re-fetchable by id — so rolling the oldest off is safe in a
way that evicting active gaps/capabilities/friction would not be.

This is a STRUCTURAL backstop applied by the backend on every write-back,
independent of whether the agent voluntarily prunes. Active, semantically
meaningful state (active gaps, capabilities, pending confirmations) is never
evicted here — only history/archive."""


PROBLEM_MODEL_BYTE_BUDGET = 90_000
"""Serialized-byte budget for the durable problem model, set safely under the
strategic write cap.

The strategic op endpoint enforces a 100KB limit on the INCOMING agent payload
(``admin_agent_ops`` ``set_problem_model``), but the persisted post-merge model
— after ``archived_gaps`` are unioned forward — is never byte-checked, so the
durable model can silently exceed the size that could ever be written back
through the cap. :func:`gc_problem_model` measures the serialized model after
the count caps run and, while it exceeds this budget, evicts oldest archived
gaps and trims the rejected-agent-edits ledger (both history/reference-only) so
the durable model stays round-trippable. Set ~10KB under the 100KB cap to leave
headroom for the revision-log entry appended in the same write."""


CAPABILITIES_DURABLE_MAX = 200
"""Hard cap on the durable ``capabilities`` collection.

Capabilities are merged non-destructively every cycle (see
:func:`_merge_capabilities`), so on a mature project they accumulate without an
intrinsic bound and — once read-side skeletonization is removed — a
capability-heavy model can exceed the strategic payload budget and be dropped
whole. This cap is the write-side backstop: generous (far above the advisory
:data:`MODEL_SIZE_HEALTHY_CAPABILITIES`) but finite. Eviction is restricted to
``planned`` capabilities (terminal/low-value — described but not yet built);
live ``working``/``partial`` capabilities are never evicted, so an all-live
collection may legitimately remain above this cap."""


FRICTION_DURABLE_MAX = 200
"""Hard cap on the durable ``friction`` collection — see
:data:`CAPABILITIES_DURABLE_MAX`. Eviction is restricted to terminal
``resolved`` then ``mitigated`` friction (reference-only, like archived gaps);
``active`` friction is live high-value state and is never evicted, so an
all-active collection may legitimately remain above this cap."""


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


def _archived_gap_recency_key(gap: Gap) -> str:
    """Sort key for archived-gap eviction — most recent first when sorted
    descending. Prefers ``closed`` (when it was archived), falling back to
    ``opened``; a gap with neither timestamp sorts oldest and is evicted
    first. Empty string sorts below any ISO-8601 timestamp."""
    return gap.closed or gap.opened or ""


def _active_gap_recency_key(gap: Gap) -> str:
    """Sort key for active-gap tail trimming — oldest active gaps first."""
    return gap.opened or (gap.history[0].ts if gap.history else "")


def _trim_gap_append_only_tail(gap: Gap) -> dict[str, Any] | None:
    """Trim one active gap to the durable append-only floor."""
    trimmed: dict[str, Any] = {"gap_id": gap.id}
    if len(gap.history) > GAP_APPEND_ONLY_GC_FLOOR:
        trimmed["history_trimmed"] = len(gap.history) - GAP_APPEND_ONLY_GC_FLOOR
        gap.history = gap.history[-GAP_APPEND_ONLY_GC_FLOOR:]
    if len(gap.past_moves) > GAP_APPEND_ONLY_GC_FLOOR:
        trimmed["past_moves_trimmed"] = (
            len(gap.past_moves) - GAP_APPEND_ONLY_GC_FLOOR
        )
        gap.past_moves = gap.past_moves[-GAP_APPEND_ONLY_GC_FLOOR:]
    return trimmed if len(trimmed) > 1 else None


def _serialized_model_bytes(model: ProblemModel) -> int:
    """Serialized byte length of the durable model, matching how the strategic
    op endpoint measures an incoming payload (``json.dumps(..., default=str)``)."""
    return len(json.dumps(model.model_dump(), default=str))


def _is_evictable_capability(cap: Capability) -> bool:
    """A capability the byte GC may evict: ``planned`` only — described but not
    yet built, so terminal/low-value. ``working``/``partial`` are live
    high-value state the agent owns and are never evicted."""
    return cap.status == "planned"


def _is_evictable_friction(fric: Friction) -> bool:
    """Friction the byte GC may evict: ``resolved`` or ``mitigated`` —
    terminal/reference-only, like an archived gap. ``active`` friction is live
    high-value state and is never evicted."""
    return fric.status in ("resolved", "mitigated")


def _evict_one_terminal_capfriction(
    model: ProblemModel,
) -> tuple[str, str] | None:
    """Evict the single most-disposable terminal capability/friction entry,
    mutating ``model`` in place.

    Priority (most disposable first): ``resolved`` friction, then ``mitigated``
    friction, then ``planned`` capabilities — friction before any capability,
    mirroring how archived gaps are the first reference-only state to roll off.
    Within a status, oldest-first (list order, the merge-insertion order). Live
    state (``working``/``partial`` capabilities, ``active`` friction) is never
    touched. Returns ``(kind, identifier)`` of what was evicted —
    ``("friction", id)`` or ``("capability", name)`` — or ``None`` when nothing
    is evictable."""
    for i, fric in enumerate(model.friction):
        if fric.status == "resolved":
            model.friction.pop(i)
            return ("friction", fric.id)
    for i, fric in enumerate(model.friction):
        if fric.status == "mitigated":
            model.friction.pop(i)
            return ("friction", fric.id)
    for i, cap in enumerate(model.capabilities):
        if cap.status == "planned":
            model.capabilities.pop(i)
            return ("capability", cap.name)
    return None


def _cap_count_cap(model: ProblemModel) -> list[str]:
    """Evict terminal (``planned``) capabilities oldest-first when the
    collection exceeds :data:`CAPABILITIES_DURABLE_MAX`. Live working/partial
    capabilities are never evicted, so the collection may remain above the cap
    when it is all-live. Returns the evicted capability names."""
    overage = len(model.capabilities) - CAPABILITIES_DURABLE_MAX
    if overage <= 0:
        return []
    evicted: list[str] = []
    kept: list[Capability] = []
    for cap in model.capabilities:
        if len(evicted) < overage and _is_evictable_capability(cap):
            evicted.append(cap.name)
        else:
            kept.append(cap)
    model.capabilities = kept
    return evicted


def _friction_count_cap(model: ProblemModel) -> list[str]:
    """Evict terminal friction when the collection exceeds
    :data:`FRICTION_DURABLE_MAX`, ``resolved`` before ``mitigated`` and
    oldest-first within each. Active friction is never evicted, so the
    collection may remain above the cap when it is all-active. Returns the
    evicted friction ids (in surviving-list order)."""
    overage = len(model.friction) - FRICTION_DURABLE_MAX
    if overage <= 0:
        return []
    resolved = [i for i, f in enumerate(model.friction) if f.status == "resolved"]
    mitigated = [i for i, f in enumerate(model.friction) if f.status == "mitigated"]
    evict_idx = set((resolved + mitigated)[:overage])
    evicted: list[str] = []
    kept: list[Friction] = []
    for i, fric in enumerate(model.friction):
        if i in evict_idx:
            evicted.append(fric.id)
        else:
            kept.append(fric)
    model.friction = kept
    return evicted


def _byte_evictable(model: ProblemModel) -> bool:
    """Whether the byte pass can actually bring ``model`` under the budget.

    The byte pass touches only reference-only / terminal state — ``archived_gaps``,
    the ``rejected_agent_edits`` ledger, the oldest active-gap append-only tails,
    and terminal capabilities/friction (``planned`` capabilities, ``resolved``/
    ``mitigated`` friction). If the live/active state alone (everything that
    survives once those are reduced to their durable floor) already exceeds the
    budget, evicting that history cannot get the model under the cap; doing so
    would be futile destruction of re-fetchable reference state on every mature
    project (a healthy within-caps model carries enough active gaps/history to
    exceed the budget on its own). So gate eviction on it actually being
    achievable: measure the floor with the evictable collections cleared and
    active tails trimmed, restore the model, and only let the caller evict when
    that floor fits. The legitimate target — a model whose archived/ledger/
    terminal cap/friction bulk is large enough to blow the cap despite a small
    live core — still has a sub-budget floor and is GC'd normally.

    Crucially, the floor must drop terminal capabilities/friction too: without
    that, a model whose bulk IS its ``planned`` capabilities or ``resolved``/
    ``mitigated`` friction would measure an over-budget floor and the gate would
    wrongly skip — leaving it oversized in the durable store (the precise gap
    this backstop closes)."""
    floor_model = model.model_copy(deep=True)
    floor_model.archived_gaps = []
    floor_model.rejected_agent_edits = []
    floor_model.capabilities = [
        c for c in floor_model.capabilities if not _is_evictable_capability(c)
    ]
    floor_model.friction = [
        f for f in floor_model.friction if not _is_evictable_friction(f)
    ]
    for gap in sorted(floor_model.gaps, key=_active_gap_recency_key):
        _trim_gap_append_only_tail(gap)
    floor_bytes = _serialized_model_bytes(floor_model)
    return floor_bytes <= PROBLEM_MODEL_BYTE_BUDGET


def gc_problem_model(model: ProblemModel) -> dict[str, Any]:
    """Structurally bound the durable model so it round-trips in a single pass.

    Pure backend backstop — mutates ``model`` in place and returns a small
    summary of what was evicted (empty dict when nothing was dropped, so the
    caller can keep the diff/output byte-identical on the no-op path).

    Two passes, both touching ONLY history/reference-only/terminal state:

    1. **Count cap** — bounds the durable collections that grow without limit:
       ``archived_gaps`` at :data:`ARCHIVED_GAPS_DURABLE_MAX` (evicting the oldest
       by ``closed``/``opened`` timestamp), and ``capabilities`` / ``friction``
       at :data:`CAPABILITIES_DURABLE_MAX` / :data:`FRICTION_DURABLE_MAX`. The
       cap/friction count caps evict ONLY terminal entries — ``planned``
       capabilities and ``resolved``/``mitigated`` friction — oldest-first, so an
       all-live collection may legitimately remain above the cap (live state is
       never dropped to satisfy a count).
    2. **Byte cap** — after the count caps, measures the serialized model and,
       while it exceeds :data:`PROBLEM_MODEL_BYTE_BUDGET` (set safely under the
       100KB write cap), evicts the oldest archived gaps, then trims the oldest
       rejected-agent-edits ledger entries, then trims the oldest active gaps'
       append-only tails, and finally — as a last resort — evicts terminal
       capabilities/friction (``resolved``/``mitigated`` friction first, then
       ``planned`` capabilities) until the model fits. This guards models whose
       bulk is large individual entries or accumulated terminal cap/friction
       despite being under the count caps, so the durable model stays writable
       back through the strategic op endpoint. The byte pass is gated on
       :func:`_byte_evictable`: it runs ONLY when emptying the evictable
       reference-only/terminal collections and reducing active tails to the
       durable floor could actually bring the model under budget. A healthy
       within-caps model can carry enough live state to exceed the budget on its
       own, and the byte pass cannot touch that live state — so evicting
       reference history there is futile destruction and is skipped.

    It deliberately does NOT touch active gaps, live capabilities
    (``working``/``partial``), active friction, or pending confirmations — those
    are live, semantically meaningful state that the agent owns and merges
    non-destructively; dropping them could lose entries the agent never saw.
    ``archived_gaps`` is pure history (closed/abandoned, reference-only,
    re-fetchable by id), ``rejected_agent_edits`` is agent-readable bookkeeping,
    and terminal capabilities/friction are reference-only (the work is done or
    abandoned) — so oldest-first eviction of any of these is safe.

    Returns a dict like ``{"archived_gaps_evicted": ["g-x"],
    "rejected_agent_edits_trimmed": 2, "friction_evicted": ["f-y"]}`` recording
    what rolled off, suitable for folding into the revision-log diff.
    """
    summary: dict[str, Any] = {}
    evicted_ids: list[str] = []
    active_gap_tails_trimmed: list[dict[str, Any]] = []
    capabilities_evicted: list[str] = []
    friction_evicted: list[str] = []

    if len(model.archived_gaps) > ARCHIVED_GAPS_DURABLE_MAX:
        # Stable sort newest-last by recency, then keep the most-recent N.
        ordered = sorted(model.archived_gaps, key=_archived_gap_recency_key)
        evicted = ordered[:-ARCHIVED_GAPS_DURABLE_MAX]
        kept = ordered[-ARCHIVED_GAPS_DURABLE_MAX:]
        model.archived_gaps = kept
        evicted_ids.extend(g.id for g in evicted)

    # Terminal-only count caps for the two non-destructively-merged collections.
    capabilities_evicted.extend(_cap_count_cap(model))
    friction_evicted.extend(_friction_count_cap(model))

    # Byte cap: only re-order/evict when we are genuinely over budget so the
    # no-op path leaves the model byte-identical (no spurious archived re-sort).
    ledger_trimmed = 0
    if _serialized_model_bytes(model) > PROBLEM_MODEL_BYTE_BUDGET and _byte_evictable(
        model
    ):
        # Oldest-first so list.pop(0) drops the oldest archived gap.
        model.archived_gaps = sorted(
            model.archived_gaps, key=_archived_gap_recency_key
        )
        while _serialized_model_bytes(model) > PROBLEM_MODEL_BYTE_BUDGET and (
            model.archived_gaps or model.rejected_agent_edits
        ):
            if model.archived_gaps:
                evicted_ids.append(model.archived_gaps.pop(0).id)
            else:
                model.rejected_agent_edits.pop(0)
                ledger_trimmed += 1
        for gap in sorted(model.gaps, key=_active_gap_recency_key):
            if _serialized_model_bytes(model) <= PROBLEM_MODEL_BYTE_BUDGET:
                break
            if trimmed := _trim_gap_append_only_tail(gap):
                active_gap_tails_trimmed.append(trimmed)
        # Last resort: evict terminal capabilities/friction. Live state
        # (working/partial caps, active friction) is never reached because
        # _evict_one_terminal_capfriction only ever returns terminal entries.
        while _serialized_model_bytes(model) > PROBLEM_MODEL_BYTE_BUDGET:
            ev = _evict_one_terminal_capfriction(model)
            if ev is None:
                break
            kind, ident = ev
            if kind == "capability":
                capabilities_evicted.append(ident)
            else:
                friction_evicted.append(ident)

    if evicted_ids:
        summary["archived_gaps_evicted"] = evicted_ids
    if ledger_trimmed:
        summary["rejected_agent_edits_trimmed"] = ledger_trimmed
    if active_gap_tails_trimmed:
        summary["active_gap_tails_trimmed"] = active_gap_tails_trimmed
    if capabilities_evicted:
        summary["capabilities_evicted"] = capabilities_evicted
    if friction_evicted:
        summary["friction_evicted"] = friction_evicted

    return summary


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


# ---------------------------------------------------------------------------
# Model size / health signal
# ---------------------------------------------------------------------------

MODEL_SIZE_HEALTHY_GAPS = 12
"""At or below this many ACTIVE gaps (archived excluded) the gap dimension is
``healthy``. Chosen so the thinker can audit every active gap in one pass."""

MODEL_SIZE_HEALTHY_CAPABILITIES = 20
"""At or below this many capabilities the capability dimension is ``healthy``."""

MODEL_SIZE_HEALTHY_FRICTION = 8
"""At or below this many ACTIVE friction items (status=='active') the friction
dimension is ``healthy``."""

MODEL_SIZE_HEALTHY_BYTES = 40_000
"""At or below this many serialized bytes the byte dimension is ``healthy``.
Set well under the 90KB PROBLEM_MODEL_BYTE_BUDGET to signal when the model
is approaching the structural limit even if entry counts look healthy."""

MODEL_SIZE_GROWING_MULTIPLIER = 1.5
"""A dimension at >= 1.5x its healthy threshold is at least ``growing``."""

MODEL_SIZE_LARGE_MULTIPLIER = 2.0
"""A dimension at >= 2x its healthy threshold is at least ``large``."""

MODEL_SIZE_OVERSIZED_MULTIPLIER = 3.0
"""A dimension at >= 3x its healthy threshold is ``oversized``."""

ModelSizeStatus = Literal["healthy", "growing", "large", "oversized"]

# Per-entry token weights for the APPROXIMATE footprint estimate. These are
# deliberately round order-of-magnitude guesses, NOT measured token counts —
# the estimate exists only to give the thinker a rough sense of how heavy the
# model is to read, never as a precise budget. A gap costs a base (name +
# description + evidence) plus a per-history-event increment; capabilities and
# friction items each cost a flat approximate amount. Text content in free-form
# fields (descriptions, evidence, notes) is approximated by dividing byte count
# by TEXT_BYTES_PER_TOKEN to avoid models with verbose entries masquerading as
# healthy.
_TOKENS_PER_GAP_BASE = 60
_TOKENS_PER_GAP_HISTORY_ENTRY = 20
_TOKENS_PER_CAPABILITY = 25
_TOKENS_PER_FRICTION = 30
_TEXT_BYTES_PER_TOKEN = 4  # Rough estimate: 1 token ≈ 4 characters


def _dimension_status(count: int, healthy: int) -> ModelSizeStatus:
    """Classify one dimension against its healthy threshold and the shared
    growing/large/oversized multipliers. Worst-case ordering is
    healthy < growing < large < oversized."""
    if count >= healthy * MODEL_SIZE_OVERSIZED_MULTIPLIER:
        return "oversized"
    if count >= healthy * MODEL_SIZE_LARGE_MULTIPLIER:
        return "large"
    if count >= healthy * MODEL_SIZE_GROWING_MULTIPLIER:
        return "growing"
    return "healthy"


def compute_model_size_signal(model: ProblemModel) -> dict[str, Any]:
    """Compute a SOFT size/health signal for a problem model.

    Pure and side-effect-free — takes a parsed :class:`ProblemModel` (the
    DURABLE model, not the trimmed agent view, so counts are honest) and
    returns a small dict the strategic payload injects under ``model_health``:

      - ``active_gap_count`` — number of active gaps (status=='active', archived_gaps excluded)
      - ``capability_count`` — number of capabilities
      - ``active_friction_count`` — friction items with ``status == 'active'``
      - ``estimated_token_footprint`` — an EXPLICITLY APPROXIMATE estimate of how
        many tokens the model costs to read (see the ``_TOKENS_PER_*`` weights);
        do not treat it as precise
      - ``serialized_bytes`` — the durable model's actual serialized byte length,
        which is what determines whether it fits the strategic-cycle payload
      - ``model_size_status`` — worst-dimension-wins classification, one of
        ``healthy`` / ``growing`` / ``large`` / ``oversized``. Now byte-aware:
        a model that is heavy by bytes but light by count will be classified as
        ``large`` or ``oversized`` to signal the byte pressure

    The status is purely advisory: it tells the thinker the model is getting
    expensive to audit, never a hard cap that blocks opening new gaps. The
    healthy thresholds and multipliers are exported as module constants so the
    agent prompt can cite the same numbers.
    """
    active_gaps = [g for g in model.gaps if g.status == "active"]
    active_gap_count = len(active_gaps)
    capability_count = len(model.capabilities)
    active_friction_count = sum(1 for f in model.friction if f.status == "active")

    # Approximate token footprint — includes both entry count and text content.
    # Text content is approximated via byte length divided by TEXT_BYTES_PER_TOKEN
    # so a model with verbose entries scores higher than one with terse entries.
    estimated_token_footprint = (
        sum(
            _TOKENS_PER_GAP_BASE
            + len(g.history) * _TOKENS_PER_GAP_HISTORY_ENTRY
            + (len(g.description) + len(g.evidence)) // _TEXT_BYTES_PER_TOKEN
            for g in active_gaps
        )
        + sum(
            _TOKENS_PER_CAPABILITY + (len(c.notes or "") // _TEXT_BYTES_PER_TOKEN)
            for c in model.capabilities
        )
        + sum(
            _TOKENS_PER_FRICTION + (len(f.description) // _TEXT_BYTES_PER_TOKEN)
            for f in model.friction
            if f.status == "active"
        )
    )

    # Byte-aware dimension: the serialized model size relative to the byte budget.
    serialized_bytes = _serialized_model_bytes(model)
    byte_status = _dimension_status(serialized_bytes, MODEL_SIZE_HEALTHY_BYTES)

    statuses = (
        _dimension_status(active_gap_count, MODEL_SIZE_HEALTHY_GAPS),
        _dimension_status(capability_count, MODEL_SIZE_HEALTHY_CAPABILITIES),
        _dimension_status(active_friction_count, MODEL_SIZE_HEALTHY_FRICTION),
        byte_status,
    )
    order: list[ModelSizeStatus] = ["healthy", "growing", "large", "oversized"]
    model_size_status = max(statuses, key=order.index)

    return {
        "active_gap_count": active_gap_count,
        "capability_count": capability_count,
        "active_friction_count": active_friction_count,
        "estimated_token_footprint": estimated_token_footprint,
        "serialized_bytes": serialized_bytes,
        "model_size_status": model_size_status,
    }


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
    if consolidated := diff.get("capabilities_consolidated"):
        parts.append(f"consolidated {len(consolidated)} capability group(s)")
    if evicted := diff.get("archived_gaps_evicted"):
        parts.append(f"GC evicted {len(evicted)} archived gap(s)")
    if trimmed := diff.get("rejected_agent_edits_trimmed"):
        parts.append(f"GC trimmed {trimmed} rejected-edit ledger entry(ies)")
    if tails := diff.get("active_gap_tails_trimmed"):
        parts.append(f"GC trimmed {len(tails)} active gap tail(s)")
    if caps_evicted := diff.get("capabilities_evicted"):
        parts.append(f"GC evicted {len(caps_evicted)} terminal capability(ies)")
    if friction_evicted := diff.get("friction_evicted"):
        parts.append(f"GC evicted {len(friction_evicted)} terminal friction")
    return "; ".join(parts) if parts else "no structural changes"


# ---------------------------------------------------------------------------
# Agent update application
# ---------------------------------------------------------------------------


# Note: the former ``ColdStartWipeError`` / ``_is_bootstrap_over_mature`` guard
# (which hard-rejected a bootstrap-over-mature write) was removed when the
# agent-owned collections moved to a non-destructive merge (main #7449). Under
# merge semantics a trimmed or dropped payload can no longer wipe by omission —
# omitted/empty collections are simply preserved — so the reject path is
# redundant. Intentional removal is now explicit via the ``_deletions`` directive
# in :func:`apply_agent_update`.


def project_has_purpose(project) -> bool:
    """True when the project has an established purpose.

    Canonical source is ``problem_model.intent.purpose`` (seeded at onboarding
    by ``admin_onboard._seed_strategic_intent``); the deprecated
    ``vision.purpose`` is honored as a legacy fallback for projects that
    pre-date the problem model. Both the strategic force-trigger
    (``autonomous_research``) and the autonomy-debug endpoint resolve purpose
    through this helper so trigger behavior and "why" reporting cannot drift.
    Reading only ``vision.purpose`` made every problem-model project look
    purposeless — ``force_strategic`` fired on every tick and strategic
    cycles spawned back-to-back at the cooldown rate (stsbot2, 2026-06-07:
    6 cycles in ~3h against a frequency of 6 spawns/cycle).
    """
    pm = getattr(project, "problem_model", None) or {}
    intent = pm.get("intent") or {}
    if (intent.get("purpose") or "").strip():
        return True
    vision = getattr(project, "vision", None) or {}
    return bool((vision.get("purpose") or "").strip())


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
    # When the patch carries ``intent``, the shallow merge replaced the whole
    # intent object — dropping whatever ``source`` was previously stamped (e.g.
    # the onboarding ``user_message`` provenance). Re-stamp operator authorship
    # so the operator-wins protection (_preserve_operator_intent /
    # _merge_operator_owned_fields, which gate on
    # ``intent.source.type == 'user_message'``) keeps guarding the edited
    # purpose against the next strategic cycle. A blanked purpose clears the
    # source so an empty operator intent is not spuriously protected. Patches
    # that omit ``intent`` (e.g. onboarding's context/constraints-only steering,
    # which sets its own message_id-bearing source directly) are left untouched.
    if "intent" in patch:
        purpose = (validated.intent.purpose or "").strip()
        validated.intent.source = (
            Source(type="user_message", excerpt=purpose[:120]) if purpose else None
        )
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


def operator_owned_subset(raw: dict | None) -> dict[str, Any]:
    """Extract the operator-owned subset (:data:`USER_EDITABLE_FIELDS`) from a
    raw problem-model dict as a merge *ancestor*.

    Returns ``{"intent": {"purpose": ...}, "context": {...}, "constraints": [...]}``
    — the only three fields :func:`_merge_operator_owned_fields` reads off its
    ``baseline`` argument. Tolerates extra/agent-owned keys (e.g. a ``_deletions``
    directive or the full agent collections) so it can be handed the agent's raw
    *pre-merge* proposal, and round-trips through :func:`parse_problem_model`.

    Used by the ``set_problem_model`` write paths to remember the agent's own
    last proposed operator-owned values per cycle, so a *second* same-cycle agent
    write reconciles against the agent's prior proposal — not the cycle-start
    snapshot — and is no longer misread as a mid-cycle operator edit. A genuine
    operator edit (which moves ``current`` away from the agent's stored proposal)
    still wins, because the merge keys on ``current != baseline``.
    """
    raw = raw or {}
    intent = raw.get("intent") if isinstance(raw.get("intent"), dict) else {}
    context = raw.get("context") if isinstance(raw.get("context"), dict) else {}
    constraints = raw.get("constraints")
    return {
        "intent": {"purpose": str((intent or {}).get("purpose") or "")},
        "context": dict(context or {}),
        "constraints": list(constraints) if isinstance(constraints, list) else [],
    }


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
    if purpose_was_operator:
        # Always-protect: when purpose is operator-owned, an agent-proposed
        # change is accepted ONLY when the agent supplies user_message
        # provenance (indicating the user directly updated the purpose
        # through the conversation thread). Without that, the agent must
        # route intent changes through pending_user_confirmation instead of
        # silently overwriting. This closes the repair-path clobber where a
        # fresh repair session with no prior analysis context echoes
        # schema-template placeholder text as the purpose
        # (BUG_REPORT 8154c2d3 / PR #7435).
        agent_has_user_message_source = (
            proposed.intent.source is not None
            and proposed.intent.source.type == "user_message"
        )
        if current.intent.purpose != baseline.intent.purpose:
            # Mid-cycle operator edit: operator always wins regardless of
            # what the agent proposed.
            if proposed.intent.purpose not in (
                baseline.intent.purpose,
                current.intent.purpose,
            ):
                _record(
                    "intent.purpose",
                    current.intent.purpose,
                    proposed.intent.purpose,
                )
            proposed.intent.purpose = current.intent.purpose
            proposed.intent.source = current.intent.source
        elif (
            proposed.intent.purpose != current.intent.purpose
            and not agent_has_user_message_source
        ):
            # No mid-cycle operator edit, but agent is trying to change an
            # operator-owned purpose without user_message provenance — reject.
            if proposed.intent.purpose != baseline.intent.purpose:
                _record(
                    "intent.purpose",
                    current.intent.purpose,
                    proposed.intent.purpose,
                )
            proposed.intent.purpose = current.intent.purpose
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


# ---------------------------------------------------------------------------
# Agent-owned collection merge — non-destructive write
#
# The agent-owned collections (capabilities, gaps, friction,
# pending_user_confirmation) are NOT operator-protected: they advance freely
# each cycle. But the strategic-spawn payload packs problem_model under a byte
# budget (pack_under_budget, 120 KB) and trims it (per-gap history/past_moves
# capped, archived_gaps dropped) or drops it entirely under pressure. A
# full-replace write would then silently wipe entries the agent never received
# (the "capability map and friction I can't see" trap). So these collections
# MERGE: proposed entries upsert by stable key, entries the agent omitted are
# preserved, and per-gap append-only logs union so a trimmed tail echoed back
# can't truncate the full record. Intentional removal is explicit
# (``_deletions``), never by omission. Operator-owned fields (intent.purpose,
# context, constraints) are untouched here — they keep their 3-way merge above.


def _as_str_list(deletions: Any, key: str) -> list[str]:
    """Pull ``deletions[key]`` as a list of strings, tolerating junk."""
    if not isinstance(deletions, dict):
        return []
    val = deletions.get(key)
    return [str(x) for x in val] if isinstance(val, list) else []


def _entry_key(x: Any) -> str:
    """Canonical JSON key for an append-only-log entry (str or model/dict)."""
    if isinstance(x, BaseModel):
        x = x.model_dump()
    return json.dumps(x, sort_keys=True, default=str)


def _merge_append_log(current_list: list, proposed_list: list) -> list:
    """Union two append-only logs: keep the full *current* list (the agent may
    have only seen a trimmed tail) and append any *proposed* entries not already
    present. Order-stable: current first, then genuinely-new proposed."""
    out = list(current_list)
    seen = {_entry_key(x) for x in out}
    for x in proposed_list:
        k = _entry_key(x)
        if k not in seen:
            out.append(x)
            seen.add(k)
    return out


def _upsert_by_key(
    current: list,
    proposed: list,
    key_fn,
    deleted: set[str],
    merge_fn=None,
) -> list:
    """Upsert *proposed* into *current* keyed by ``key_fn``.

    - present in both: replaced by proposed (or ``merge_fn(cur, prop)`` for
      deep merges)
    - only in current: preserved (omission never deletes)
    - only in proposed: appended (new)
    - keys in *deleted*: dropped

    Order-stable: current order preserved (updated in place), new proposed
    entries appended in their own order.
    """
    prop_by_key = {key_fn(p): p for p in proposed}
    used: set[str] = set()
    result: list = []
    for c in current:
        k = key_fn(c)
        if k in deleted:
            continue
        if k in prop_by_key:
            p = prop_by_key[k]
            result.append(merge_fn(c, p) if merge_fn else p)
            used.add(k)
        else:
            result.append(c)
    for p in proposed:
        k = key_fn(p)
        if k in used or k in deleted:
            continue
        result.append(p)
    return result


# Floor-strippable detail that the read-time agent-view floor reduces away under
# byte pressure (mirrors strategic_spawning's _FLOOR_SHRINK_LEVELS gap fields,
# _CAP_SKELETON_KEEP, and _FRICTION_SKELETON_KEEP — these are the fields NOT kept
# in each skeleton). When the agent edits and resubmits an entry it only ever saw
# in floored/skeleton form, a blank value for one of these fields means "I never
# received it", NOT "clear it" — so the merge restores the durable value instead
# of letting the skeleton silently overwrite it. This is keyed off the floor's
# own ``_agent_view_floor_applied`` marker (echoed back in the proposal) so a
# genuine clear of a field the agent actually SAW (no floor, or this field not
# stripped) still applies — empty is not blindly treated as keep.
_GAP_FLOOR_FIELDS = ("description", "evidence", "active_moves")
_CAP_FLOOR_FIELDS = ("notes", "backing_paths", "source", "absorbs")
_FRICTION_FLOOR_FIELDS = (
    "description",
    "seen_in",
    "mitigations_tried",
    "source",
    "promoted_to_gap_id",
)


def _is_blank_floor_field(value: Any) -> bool:
    """A floor-strippable field value the agent may never have received: ``None``
    or an empty string/list/dict. A populated ``Source`` model is non-blank."""
    if value is None:
        return True
    return isinstance(value, (str, list, dict)) and len(value) == 0


def _restore_floored_fields(
    current: BaseModel, proposed: BaseModel, fields: tuple[str, ...]
) -> None:
    """Copy each of *fields* from *current* onto *proposed* when proposed's value
    is blank but current's is not, so a resubmitted floor skeleton cannot wipe
    durable detail the agent could not have seen. Mutates *proposed* in place."""
    for name in fields:
        if _is_blank_floor_field(getattr(proposed, name, None)) and not (
            _is_blank_floor_field(getattr(current, name, None))
        ):
            setattr(proposed, name, getattr(current, name))


def _merge_gap(current: Gap, proposed: Gap, past_moves_directive: bool = False) -> Gap:
    """Upsert one gap: proposed wins for every field EXCEPT the append-only
    logs (``history``, ``past_moves``), which union current+proposed so a
    trimmed tail echoed back by the agent cannot truncate the full record. The
    unioned logs are bounded by :data:`GAP_HISTORY_DURABLE_MAX` /
    :data:`GAP_PAST_MOVES_DURABLE_MAX` (oldest entries roll off) so the JSONB
    column cannot grow without limit across cycles.

    History compaction watermark contract (durable, two-sided, monotonic):
    - The effective watermark is ``max(current.history_compacted_at,
      proposed.history_compacted_at)`` so a write that omits the field cannot
      regress a durable watermark (Defect 1 fix).
    - BOTH ``current.history`` and ``proposed.history`` are filtered before the
      union, so a stale echo carrying pre-compaction entries on the proposed side
      cannot resurrect them (Defect 2 fix).

    ``past_moves_directive`` signals that a ``_compactions`` directive with
    ``past_moves_watermark`` was present for this gap in the current write.
    When ``True``, replacement semantics apply: ``proposed.past_moves`` is used
    directly rather than unioned with ``current.past_moves``.  Without the
    directive, union semantics preserve the omission-never-deletes invariant."""
    merged = proposed.model_copy(deep=True)

    # History watermark: monotonic max of current and proposed.  A write that
    # omits the field cannot regress a durable watermark.
    effective_h_wm = max(
        (wm for wm in (current.history_compacted_at, merged.history_compacted_at) if wm),
        default=None,
    )
    merged.history_compacted_at = effective_h_wm

    # Filter BOTH sides of the history union: a stale echo carrying
    # pre-compaction entries on the proposed side cannot resurrect them.
    if effective_h_wm:
        current_history = [e for e in current.history if e.ts > effective_h_wm]
        proposed_history = [e for e in proposed.history if e.ts > effective_h_wm]
    else:
        current_history = current.history
        proposed_history = proposed.history
    merged.history = _merge_append_log(current_history, proposed_history)[
        -GAP_HISTORY_DURABLE_MAX:
    ]

    # past_moves watermark: monotonic max for observability/durability.
    effective_pm_wm = max(
        (wm for wm in (current.past_moves_compacted_at, merged.past_moves_compacted_at) if wm),
        default=None,
    )
    merged.past_moves_compacted_at = effective_pm_wm

    # past_moves compaction: when a _compactions directive named this gap with
    # past_moves_watermark, use replacement semantics so the agent's explicit
    # trimmed list becomes the new durable baseline.  Without the directive,
    # union as before (omission-never-deletes invariant is preserved).
    if past_moves_directive:
        merged.past_moves = list(proposed.past_moves)[-GAP_PAST_MOVES_DURABLE_MAX:]
    else:
        merged.past_moves = _merge_append_log(current.past_moves, proposed.past_moves)[
            -GAP_PAST_MOVES_DURABLE_MAX:
        ]
    return merged


def _merge_agent_owned_collections(
    current: ProblemModel,
    proposed: ProblemModel,
    deletions: Any,
    floor: Any = None,
    pm_compacted_gaps: set[str] | None = None,
) -> None:
    """Replace ``proposed``'s agent-owned collections with a non-destructive
    merge over ``current`` (mutates ``proposed`` in place). Removal is explicit
    via ``deletions`` — a dict of ``{capabilities|gaps|friction|
    pending_user_confirmation: [key, ...]}``. ``constraints`` is NOT here: it is
    operator-owned and handled by the 3-way merge / blank backstop.

    ``floor`` is the read-time ``_agent_view_floor_applied`` marker the agent
    echoes back (or ``None``). When it records that a field/section was
    floor-stripped before the agent saw it, a blank value for that field in a
    resubmitted entry preserves the durable detail instead of wiping it (see
    :func:`_restore_floored_fields`). Gap preservation is per-field-precise
    (``stripped_gap_fields`` is uniform across gaps); capability/friction
    preservation gates on whether that section was skeletonized this cycle
    (``summarized_*`` > 0) — the floor records a count, not per-entry keys, and
    over-preserving a field the agent meant to clear is the safe direction (no
    durable loss, recoverable next cycle) versus silently wiping detail."""
    del_caps = set(_as_str_list(deletions, "capabilities"))
    del_gaps = set(_as_str_list(deletions, "gaps"))
    del_friction = set(_as_str_list(deletions, "friction"))
    del_pending = set(_as_str_list(deletions, "pending_user_confirmation"))

    floor = floor if isinstance(floor, dict) else {}
    stripped_gap = set(floor.get("stripped_gap_fields") or [])
    gap_floor_fields = tuple(f for f in _GAP_FLOOR_FIELDS if f in stripped_gap)
    caps_floored = bool(floor.get("summarized_capabilities"))
    friction_floored = bool(floor.get("summarized_friction"))
    _pm_compacted = pm_compacted_gaps or set()

    def _merge_gap_preserving(c: Gap, p: Gap) -> Gap:
        merged = _merge_gap(c, p, p.id in _pm_compacted)
        if gap_floor_fields:
            _restore_floored_fields(c, merged, gap_floor_fields)
        return merged

    def _merge_cap_preserving(c: Capability, p: Capability) -> Capability:
        if caps_floored:
            _restore_floored_fields(c, p, _CAP_FLOOR_FIELDS)
        return p

    def _merge_friction_preserving(c: Friction, p: Friction) -> Friction:
        if friction_floored:
            _restore_floored_fields(c, p, _FRICTION_FLOOR_FIELDS)
        return p

    proposed.capabilities = _upsert_by_key(
        current.capabilities,
        proposed.capabilities,
        lambda c: c.name,
        del_caps,
        merge_fn=_merge_cap_preserving,
    )
    proposed.gaps = _upsert_by_key(
        current.gaps,
        proposed.gaps,
        lambda g: g.id,
        del_gaps,
        merge_fn=_merge_gap_preserving,
    )
    proposed.friction = _upsert_by_key(
        current.friction,
        proposed.friction,
        lambda f: f.id,
        del_friction,
        merge_fn=_merge_friction_preserving,
    )
    proposed.pending_user_confirmation = _upsert_by_key(
        current.pending_user_confirmation,
        proposed.pending_user_confirmation,
        lambda p: p.id,
        del_pending,
    )
    # archived_gaps was already set to current+proposed archived; honor an
    # explicit gap deletion against it too.
    if del_gaps:
        proposed.archived_gaps = [
            g for g in proposed.archived_gaps if g.id not in del_gaps
        ]


def _apply_compactions(
    proposed: ProblemModel,
    compactions_raw: Any,
) -> list[dict[str, Any]]:
    """Apply ``_compactions`` directive to *proposed* by setting per-gap
    watermark fields.  Returns a list of event dicts for revision-log
    observability.  No-op (returns ``[]``) when *compactions_raw* is absent,
    not a list, or names no matching gaps."""
    if not isinstance(compactions_raw, list):
        return []
    events: list[dict[str, Any]] = []
    gap_by_id = {g.id: g for g in proposed.gaps}
    for entry in compactions_raw:
        if not isinstance(entry, dict):
            continue
        gap_id = entry.get("gap_id")
        if not isinstance(gap_id, str) or gap_id not in gap_by_id:
            continue
        gap = gap_by_id[gap_id]
        event: dict[str, Any] = {"gap_id": gap_id}
        h_wm = entry.get("history_watermark")
        if isinstance(h_wm, str) and h_wm:
            # Monotonic guard: a stale directive cannot regress an already-set watermark.
            existing_h = gap.history_compacted_at or ""
            gap.history_compacted_at = max(h_wm, existing_h) or None
            event["history_watermark"] = gap.history_compacted_at
        pm_wm = entry.get("past_moves_watermark")
        if isinstance(pm_wm, str) and pm_wm:
            existing_pm = gap.past_moves_compacted_at or ""
            gap.past_moves_compacted_at = max(pm_wm, existing_pm) or None
            event["past_moves_watermark"] = gap.past_moves_compacted_at
        if len(event) > 1:  # at least one watermark was set
            events.append(event)
    return events


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
    restores any append-only per-gap ``history``/``past_moves`` the payload
    trimmed before the agent saw it (see :func:`_merge_agent_owned_collections`
    / :func:`_merge_gap`), auto-archives any gaps that are now closed or
    abandoned, and appends a
    revision-log entry summarizing the change.

    Returns a tuple of ``(new_raw_dict, diff_summary)``:
      - ``new_raw_dict`` is ready for JSONB persistence on ``project.problem_model``
      - ``diff_summary`` is the structured change dict for downstream logging

    Raises :class:`pydantic.ValidationError` if ``proposed_raw`` is malformed.
    Callers should catch and keep the current state on validation failure.

    A trimmed or dropped payload can no longer wipe the durable model: the
    agent-owned collections merge non-destructively (omitted/empty entries are
    preserved — see below), so there is no separate bootstrap-over-mature reject
    path. Intentional removal is explicit via ``_deletions``.

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

    Agent-owned collections (capabilities, gaps, friction,
    pending_user_confirmation) MERGE non-destructively rather than replace: the
    agent often acts on a trimmed or dropped payload view, so an omitted entry
    is preserved and per-gap append-only logs union (see
    :func:`_merge_agent_owned_collections`). Intentional removal is explicit via
    an optional top-level ``_deletions`` key on ``proposed_raw`` —
    ``{"capabilities": [name], "gaps": [id], "friction": [id],
    "pending_user_confirmation": [id]}`` — lifted out before validation
    (``ProblemModel`` is ``extra="forbid"``). Removal by omission is impossible,
    so an agent on a partial view cannot wipe what it could not see.

    Per-gap history compaction is driven by an optional ``_compactions`` key
    — a list of ``{"gap_id": str, "history_watermark"?: ISO, "past_moves_watermark"?: ISO}``
    entries — that sets watermark fields on the matched gaps before the merge
    pass.  ``_merge_gap`` then excludes current history entries at or before
    the watermark, preventing stale echoes from resurrecting compacted entries.
    Compaction events are recorded in the revision-log diff.
    """
    # Lift the explicit-deletion directive out before validation — it is not
    # part of the durable model (extra="forbid" would reject it).
    proposed_raw = dict(proposed_raw)
    deletions = proposed_raw.pop("_deletions", None)
    # Per-gap history compaction directive: ``{gap_id: {history?: ISO,
    # past_moves?: ISO}}``.  Lifted out like _deletions so ProblemModel
    # (extra="forbid") does not reject it.
    compactions_raw = proposed_raw.pop("_compactions", None)
    # Read-time agent-view markers the packer injects into the floored model the
    # agent sees. They are not part of the durable model (extra="forbid" would
    # reject them), so lift them out before validation. ``_agent_view_floor_applied``
    # drives floor-aware field preservation below; ``_gap_memory_trimmed`` is
    # purely informational and discarded.
    floor_applied = proposed_raw.pop("_agent_view_floor_applied", None)
    proposed_raw.pop("_gap_memory_trimmed", None)

    proposed = ProblemModel.model_validate(proposed_raw)
    current = parse_problem_model(current_raw)

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

    # Apply per-gap history compaction watermarks BEFORE the merge so
    # _merge_gap respects them when filtering current entries.
    compaction_events = _apply_compactions(proposed, compactions_raw)

    # Derive the set of gaps that had a past_moves_watermark directive in THIS
    # write — used by _merge_gap to trigger replacement semantics for past_moves.
    pm_compacted_gaps = {
        e["gap_id"] for e in compaction_events if "past_moves_watermark" in e
    }

    # Non-destructive merge of agent-owned collections: an agent acting on a
    # trimmed/dropped payload view must not wipe entries it never saw. Runs
    # before the diff so the diff reflects the true merged state.
    _merge_agent_owned_collections(current, proposed, deletions, floor_applied, pm_compacted_gaps)

    # Compute diff before archival so closed gaps are still visible on
    # proposed.gaps — otherwise _compute_diff can't see what got closed
    # this cycle.
    diff = _compute_diff(current, proposed)
    # Surface explicit deletions in the structured change log (otherwise they
    # are invisible to _compute_diff, which only sees current vs merged state).
    for diff_key, del_key in (
        ("gaps_deleted", "gaps"),
        ("capabilities_deleted", "capabilities"),
        ("friction_deleted", "friction"),
    ):
        removed = sorted(_as_str_list(deletions, del_key))
        if removed:
            diff[diff_key] = removed

    # Record compaction events in the diff for observability.
    if compaction_events:
        diff["gaps_history_compacted"] = compaction_events

    # Surface a capability consolidation: a surviving capability whose ``absorbs``
    # lists names deleted in THIS write (via ``_deletions.capabilities``) merged
    # those old capabilities into itself. Recorded only when an actual
    # consolidation occurred (a survivor absorbs at least one just-deleted name),
    # so the no-consolidation path leaves the diff untouched.
    deleted_cap_names = set(_as_str_list(deletions, "capabilities"))
    if deleted_cap_names:
        consolidated: list[dict[str, Any]] = []
        for cap in proposed.capabilities:
            absorbed = [name for name in cap.absorbs if name in deleted_cap_names]
            if absorbed:
                consolidated.append(
                    {"survivor": cap.name, "absorbed": sorted(absorbed)}
                )
        if consolidated:
            diff["capabilities_consolidated"] = consolidated

    archive_closed_gaps(proposed)

    # Carry the durable rejected-agent-edits ledger forward and append any new
    # overrides, capped — mirrors archived_gaps/revision_log preservation. The
    # agent's own submission of this field is ignored (agent-readable, not
    # agent-writable bookkeeping). Set BEFORE the GC so the byte-aware pass can
    # see the full durable model (the ledger stores sizable operator+agent
    # blobs) and trim this reference-only state if the model is over budget.
    ledger = list(current.rejected_agent_edits) + rejected_edits
    proposed.rejected_agent_edits = ledger[-REJECTED_AGENT_EDITS_MAX:]

    # Structural GC backstop: bound the durable history/archive collections so
    # the model round-trips in a single pass regardless of whether the agent
    # voluntarily pruned. Runs AFTER archival (so this cycle's just-closed gaps
    # are eligible) and BEFORE the revision-log append so the eviction is
    # recorded in the same revision that performed it. Folds what was dropped
    # into the diff so the GC is observable in the revision log.
    gc_summary = gc_problem_model(proposed)
    if gc_summary:
        diff.update(gc_summary)

    append_revision_log(proposed, summary=_diff_summary(diff), changes=diff)

    result = proposed.model_dump()
    # Keep output byte-identical to the pre-merge behavior on the no-conflict /
    # no-baseline path: only surface the ledger key when it carries entries.
    if not result.get(REJECTED_AGENT_EDITS_KEY):
        result.pop(REJECTED_AGENT_EDITS_KEY, None)
    # ``absorbs`` is a default-empty list, so model_dump() emits ``"absorbs": []``
    # on EVERY capability — which would change the stored bytes of every existing
    # model. Strip it from any capability that absorbed nothing so a write with no
    # consolidation stays byte-identical to before the field existed; only a
    # genuine consolidation (a survivor with a non-empty ``absorbs``) carries it.
    for cap in result.get("capabilities") or []:
        if isinstance(cap, dict) and not cap.get("absorbs"):
            cap.pop("absorbs", None)
    # Watermark fields default to None; strip them from gaps that have never
    # been compacted so the stored bytes of clean models stay identical to
    # before the fields existed.
    for gap in result.get("gaps") or []:
        if isinstance(gap, dict):
            if gap.get("history_compacted_at") is None:
                gap.pop("history_compacted_at", None)
            if gap.get("past_moves_compacted_at") is None:
                gap.pop("past_moves_compacted_at", None)
    for gap in result.get("archived_gaps") or []:
        if isinstance(gap, dict):
            if gap.get("history_compacted_at") is None:
                gap.pop("history_compacted_at", None)
            if gap.get("past_moves_compacted_at") is None:
                gap.pop("past_moves_compacted_at", None)

    return result, diff
