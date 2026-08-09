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
import re
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.evidence_primitive import (
    EvidenceRef,
    EvidenceStatus,
    classify_evidence,
)


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
    """Concise, forward-looking mission statement for the current version — one or two
    sentences stating what this version is *for* in user-visible terms. Write as an
    objective (e.g. "Add design-system token layer to admin-ui"), NOT as a status or
    progress log (not "v18 re-qualifying... deliverables confirmed complete...").
    version.goal is seeded from this field at version-open and frozen for the version
    lifetime; keep it meaningful for grading and operator communication."""

    user_purpose: str = Field(default="")
    """Durable operator-owned copy of the project purpose.

    Set only via :func:`apply_user_intent_patch` (the operator PATCH path).
    Agent writes can NEVER modify this field — :func:`apply_agent_update`
    unconditionally strips any proposed value and restores it from the durable
    current model before any merge pass. This makes it the immutable backstop
    that survives strategic-cycle rewrites, thinker JSON-repair clobbers, and
    3-way merge resolutions. Rendered in the agent context prefix as a separate
    clearly-labeled operator block so agents see the durable steering even when
    ``intent.purpose`` has been overwritten by a prior strategic cycle.
    """

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

    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    """Structured, machine-checkable pointers at the evidence backing this
    capability, mirroring :attr:`Gap.evidence_refs` exactly — same type, same
    default, same proposed-wins merge contract (``_merge_cap_preserving`` returns
    the proposed value wholesale). Optional and default-empty so every stored
    JSONB model and older agent payload validates unchanged under
    ``extra='forbid'``. The reconciliation sweep
    (``metrics_integrity_sweep.check_capability_claim_reconciliation``) overrides
    the derived status to ``contradicted`` when a proven ref is disproved."""

    proof_exempt: bool = Field(default=False)
    """When True, this working capability is declared exempt from proof
    requirements. An exempt capability:
    - classifies as ``"proven"`` in :func:`working_capability_proof_status`
      (satisfies the proof gate without machine-verified evidence),
    - is skipped by the proof earn loop,
    - does not count as unproven in the release-qualifier gate,
    - is reported separately (not in delta) by the metrics sweep.
    Defaults to False; existing capabilities without this field are
    unaffected (backward-compatible)."""


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

    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    """Structured, machine-checkable pointers at the evidence backing this gap,
    alongside the unchanged free-text ``evidence`` narrative. Each ref names a
    proof :class:`~app.evidence_primitive.ProofKind` and a ``ref`` (a
    criterion_key / proof_ref / artifact_id / file-or-test path). Optional and
    default-empty so every stored JSONB model and older agent payload validates
    unchanged — ``ProblemModel`` uses ``extra="forbid"`` so the field must be
    declared, but the empty default keeps back-compat round-trips byte-identical.
    A plain proposed-wins field: it threads through :func:`_merge_gap` and
    ``apply_agent_update`` with no extra merge logic. Kept small and bounded;
    the standing evidence-reconciliation sweep re-verifies path-backed refs
    against HEAD (see ``metrics_integrity_sweep.check_evidence_claim_reconciliation``)."""

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


class GapStaffingReconciliation(BaseModel):
    """Structured result for direction/gap staffing reconciliation."""

    changed: bool = False
    direction_id: str = ""
    new_gap_id: str | None = None
    prior_gap_id: str | None = None
    new_status: str | None = None
    prior_status: str | None = None
    staffed_gap_id: str | None = None
    pruned_gap_ids: list[str] = Field(default_factory=list)
    newly_deferred_gap_ids: list[str] = Field(default_factory=list)
    unresolved_gap_ids: dict[str, str] = Field(default_factory=dict)
    malformed_model: bool = False


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


PENDING_USER_CONFIRMATION_MAX = 25
"""Hard cap on the durable ``pending_user_confirmation`` list.

The ``pending_confirm`` strategic op appends to this list; without a cap a
retried op or a runaway strategist can grow the list without bound and bloat
the ``problem_model`` JSONB column. 25 is generous for real use (the list
represents external blockers waiting for human attention, not a log), and
mirrors the finite-but-generous caps on other append-only collections. Active
confirmations are never evicted here — the list is capped at the WRITE side
by the op endpoint, not by GC."""


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


def gap_evidence_status(gap: Gap) -> EvidenceStatus:
    """Derived evidence status for a gap, for prompt rendering and the sweep.

    Thin wrapper over :func:`app.evidence_primitive.classify_evidence` on the
    gap's ``evidence_refs``. Pure and stored-flag-only — it never re-verifies a
    ref against ground truth; a caller that does so (the reconciliation sweep)
    overrides the result to ``contradicted`` when a proven ref is disproved."""
    return classify_evidence(gap.evidence_refs)


def capability_evidence_status(cap: Capability) -> EvidenceStatus:
    """Derived evidence status for a capability, mirroring :func:`gap_evidence_status`.

    Thin wrapper over :func:`app.evidence_primitive.classify_evidence` on the
    capability's ``evidence_refs``. Pure and stored-flag-only — it never
    re-verifies a ref against ground truth; the reconciliation sweep
    (``metrics_integrity_sweep.check_capability_claim_reconciliation``) overrides
    the result to ``contradicted`` when a proven ref is definitively disproved."""
    return classify_evidence(cap.evidence_refs)



PACKER_MARKER_KEYS: frozenset[str] = frozenset(
    {
        "truncated",
        "_agent_view_floor_applied",
        "_agent_view_lean",
        "_gap_memory_trimmed",
        "_agent_view_compacted",
    }
)


def sanitize_problem_model_raw(raw: dict) -> dict:
    """Strip packer markers and unknown keys from a raw problem_model dict.

    Removes any top-level key that is not a declared ``ProblemModel`` field —
    including packer-injected markers (see :data:`PACKER_MARKER_KEYS`) and any
    future unknowns. Safe to call on the dict before persisting it so a
    packer-truncated view can never be written back with its poison markers
    intact.

    This is the canonical pre-write sanitizer for all non-agent write paths
    (``with_problem_model_locked``, ``_reconcile_retired_gap``, etc.).
    ``apply_agent_update`` does equivalent stripping inline on both current and
    proposed; call this function for every other write path instead.
    """
    known = set(ProblemModel.model_fields)
    return {k: v for k, v in raw.items() if k in known}


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
    ``gaps`` into ``archived_gaps``. Skips gaps whose id is already present
    in ``archived_gaps`` so repeated calls or stale strategist payloads that
    re-submit an already-archived gap cannot create duplicate entries.
    Returns the same instance for chaining; mutation is in place."""
    already_archived_ids = {g.id for g in model.archived_gaps}
    still_active: list[Gap] = []
    for gap in model.gaps:
        if gap.status in ("closed", "abandoned"):
            if gap.id not in already_archived_ids:
                model.archived_gaps.append(gap)
                already_archived_ids.add(gap.id)
            # Either way, remove from active gaps — don't keep a closed gap active.
        else:
            still_active.append(gap)
    model.gaps = still_active
    return model


def reconcile_gaps_for_retired_directions(
    raw_model: dict | None,
    retired_direction_ids: Iterable[str],
    now_iso: str,
    rationale: str | None = None,
) -> tuple[dict, list[str]]:
    """Reconcile problem-model gaps when their staffing direction(s) retire.

    Pure, side-effecting-on-its-argument helper invoked under the project row
    lock by both retire paths (the live ``retire_direction`` strategic op and
    the batch/log-parse strategic-completion pass). It operates directly on the
    *raw* JSONB dict (gaps as raw dicts, straight from ``project.problem_model``)
    so it never has to round-trip through the pydantic model.

    For every retired direction id, across every gap whose ``active_moves`` list
    contains that id (``active_moves`` is the authoritative staffing list — we
    match on the direction id, not the direction's ``gap_id`` hint):

    - prune the retired id out of ``gap.active_moves`` and union it into
      ``gap.past_moves`` (no duplicate if it is already there);
    - if that prune empties ``active_moves`` (it was the gap's LAST active move)
      AND the gap has no existing ``deferral``, record a :class:`GapDeferral`
      naming the retired direction(s) as the trigger — acknowledging the now
      unstaffed gap instead of silently orphaning it.

    It deliberately does NOT change ``gap.status`` (never auto-closes a gap) and
    NEVER overwrites an existing ``deferral``. It is idempotent: re-running with
    an id that has already been pruned is a no-op (the id is no longer in
    ``active_moves`` to match), so it cannot duplicate a ``past_moves`` entry or
    clobber a recorded deferral.

    Backward-compatible by construction: gaps missing any of
    ``active_moves`` / ``past_moves`` / ``deferral``, a model missing ``gaps``,
    and retired ids matching no gap are all no-ops (never raise).

    Args:
        raw_model: the raw ``project.problem_model`` dict (may be ``None``/empty).
        retired_direction_ids: the direction ids that just retired.
        now_iso: ISO-8601 timestamp stamped onto any newly recorded deferral.
        rationale: optional override for the deferral rationale; when omitted a
            default naming the retired direction(s) is used.

    Returns:
        ``(raw_model, newly_deferred_gap_ids)`` — the same (mutated) dict and
        the ids of gaps that gained a fresh deferral on this call.
    """
    model = raw_model if isinstance(raw_model, dict) else {}
    retired = {str(d) for d in (retired_direction_ids or []) if d}
    newly_deferred: list[str] = []
    if not retired:
        return model, newly_deferred

    gaps = model.get("gaps")
    if not isinstance(gaps, list):
        return model, newly_deferred

    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        active = gap.get("active_moves")
        if not isinstance(active, list):
            continue
        matched = [m for m in active if str(m) in retired]
        if not matched:
            continue

        gap["active_moves"] = [m for m in active if str(m) not in retired]

        past = gap.get("past_moves")
        if not isinstance(past, list):
            past = []
        past_seen = {str(p) for p in past}
        for m in matched:
            if str(m) not in past_seen:
                past.append(m)
                past_seen.add(str(m))
        gap["past_moves"] = past

        # Last active move removed AND no deferral yet -> acknowledge the now
        # unstaffed gap. An existing deferral is left exactly as-is.
        if not gap["active_moves"] and not gap.get("deferral"):
            trigger = ", ".join(str(m) for m in matched)
            deferral_rationale = rationale or (
                f"auto-deferred: last staffing direction {trigger} retired"
            )
            gap["deferral"] = GapDeferral(
                rationale=deferral_rationale,
                created_at=now_iso,
            ).model_dump()
            gid = gap.get("id")
            if gid is not None:
                newly_deferred.append(str(gid))

    return model, newly_deferred


def _record_gap_staffing_prune(
    gap: Gap,
    direction_id: str,
    now_iso: str | None,
    rationale: str | None,
) -> tuple[bool, bool]:
    """Prune a direction from one gap, archive it, and defer if emptied."""
    changed = False
    newly_deferred = False
    if direction_id in gap.active_moves:
        gap.active_moves = [m for m in gap.active_moves if m != direction_id]
        changed = True
    if direction_id not in gap.past_moves:
        gap.past_moves.append(direction_id)
        changed = True
    if not gap.active_moves and gap.deferral is None:
        gap.deferral = GapDeferral(
            rationale=(
                rationale
                or f"auto-deferred: staffing direction {direction_id} no longer active"
            ),
            created_at=now_iso or "",
        )
        changed = True
        newly_deferred = True
    return changed, newly_deferred


def reconcile_gap_staffing_status(
    model: ProblemModel,
    direction_id: str,
    new_gap_id: str | None,
    prior_gap_id: str | None = None,
) -> GapStaffingReconciliation:
    """Reconcile a gap's ``active_moves`` / ``deferral`` when a direction is
    created on, re-grounded onto, or status-flipped against a gap.

    The staffing-side mirror of the retire-side reconciliation: where retiring a
    gap-linked direction prunes it from the gap's ``active_moves`` (and resolves
    or re-defers the now-unstaffed gap), *staffing* a gap with a direction must
    record that the gap is being actively worked again.

    Mutates ``model`` in place and returns a structured result:

    - active directions with a valid ``new_gap_id`` staff that gap and clear any
      stale deferral;
    - non-active directions (paused/staged/expired/superseded/completed/etc.)
      do not staff a gap, and any prior active staffing is pruned into
      ``past_moves``;
    - ``prior_gap_id`` set, matching an existing gap, and different from
      ``new_gap_id`` — the direction re-grounded away from (or cleared) its old
      gap: prune ``direction_id`` from that gap's ``active_moves`` and record it
      in ``past_moves``. If the prune empties the gap, record a
      :class:`GapDeferral`;
    - unresolved new/prior gap ids are reported in ``unresolved_gap_ids`` so
      callers can log or surface drift instead of treating it as a clean no-op.

    Proposed-wins compatible and non-destructive: it only adjusts
    ``active_moves`` / ``past_moves`` / ``deferral`` on gaps that already exist in
    the model — it never creates a gap or touches any other field.
    """
    return reconcile_gap_staffing_for_status(
        model,
        direction_id,
        new_gap_id,
        prior_gap_id,
        new_status="active",
    )


def reconcile_gap_staffing_for_status(
    model: ProblemModel,
    direction_id: str,
    new_gap_id: str | None,
    prior_gap_id: str | None = None,
    *,
    new_status: str | None = "active",
    prior_status: str | None = None,
    now_iso: str | None = None,
    rationale: str | None = None,
) -> GapStaffingReconciliation:
    """Status-aware direction/gap staffing reconciliation."""
    result = GapStaffingReconciliation(
        direction_id=str(direction_id or ""),
        new_gap_id=str(new_gap_id) if new_gap_id else None,
        prior_gap_id=str(prior_gap_id) if prior_gap_id else None,
        new_status=str(new_status) if new_status is not None else None,
        prior_status=str(prior_status) if prior_status is not None else None,
    )
    if not direction_id:
        return result

    gap_by_id = {gap.id: gap for gap in model.gaps}
    # Legacy direction dicts sometimes omit status; existing write paths treat
    # missing status as active, so preserve that compatibility here.
    is_active_status = new_status is None or str(new_status) == "active"
    active_new_gap_id = result.new_gap_id if is_active_status else None

    # Re-ground away from (or clear) the OLD gap: prune the move, archive it in
    # past_moves. For status transitions to non-active, active_new_gap_id is
    # None, so the existing gap is pruned even when the direction's gap_id field
    # itself did not change.
    if result.prior_gap_id and result.prior_gap_id != active_new_gap_id:
        old_gap = gap_by_id.get(result.prior_gap_id)
        if old_gap is not None:
            changed, newly_deferred = _record_gap_staffing_prune(
                old_gap,
                str(direction_id),
                now_iso,
                rationale,
            )
            if changed:
                result.changed = True
                result.pruned_gap_ids.append(old_gap.id)
            if newly_deferred:
                result.newly_deferred_gap_ids.append(old_gap.id)
        else:
            result.unresolved_gap_ids["prior_gap_id"] = result.prior_gap_id

    # Staff the NEW gap: record the active move and drop a now-false deferral.
    if active_new_gap_id:
        new_gap = gap_by_id.get(active_new_gap_id)
        if new_gap is not None:
            if direction_id not in new_gap.active_moves:
                new_gap.active_moves.append(direction_id)
                result.changed = True
            if new_gap.deferral is not None:
                new_gap.deferral = None
                result.changed = True
            result.staffed_gap_id = new_gap.id
        else:
            result.unresolved_gap_ids["new_gap_id"] = active_new_gap_id

    return result


def reconcile_gap_staffing(
    model: ProblemModel,
    direction_id: str,
    new_gap_id: str | None,
    prior_gap_id: str | None = None,
) -> bool:
    """Boolean compatibility wrapper for active create/re-ground paths."""
    return reconcile_gap_staffing_status(
        model, direction_id, new_gap_id, prior_gap_id
    ).changed


def reconcile_gap_staffing_raw(
    problem_model_raw: dict | None,
    direction_id: str,
    new_gap_id: str | None,
    prior_gap_id: str | None = None,
) -> dict | None:
    """Raw-JSONB adapter for :func:`reconcile_gap_staffing`.

    The direction write paths hold ``project.problem_model`` as a raw JSONB dict
    on a row already locked alongside ``directions`` (same project row). This
    parses that dict, reconciles, and returns a fresh ``model_dump()`` dict when
    something changed — or ``None`` when nothing changed (so the caller skips the
    write and leaves the column byte-identical) or the model is empty.

    Fail-open: a malformed model returns ``None`` rather than raising, so a bad
    durable problem model can never block a direction create/update — mirroring
    how the retire/respawn paths tolerate a model they cannot reconcile.
    """
    result = reconcile_gap_staffing_raw_result(
        problem_model_raw,
        direction_id,
        new_gap_id,
        prior_gap_id,
        new_status="active",
    )
    return result.get("problem_model") if result.get("changed") else None


def reconcile_gap_staffing_raw_result(
    problem_model_raw: dict | None,
    direction_id: str,
    new_gap_id: str | None,
    prior_gap_id: str | None = None,
    *,
    new_status: str | None = "active",
    prior_status: str | None = None,
    now_iso: str | None = None,
    rationale: str | None = None,
) -> dict[str, Any]:
    """Raw-JSONB adapter returning structured reconciliation diagnostics.

    ``problem_model`` is present only when a write-back is needed. Malformed or
    empty models fail open but are classified so callers can distinguish them
    from a clean no-op.
    """
    empty = GapStaffingReconciliation(
        direction_id=str(direction_id or ""),
        new_gap_id=str(new_gap_id) if new_gap_id else None,
        prior_gap_id=str(prior_gap_id) if prior_gap_id else None,
        new_status=str(new_status) if new_status is not None else None,
        prior_status=str(prior_status) if prior_status is not None else None,
    )
    if not problem_model_raw:
        return {"changed": False, "problem_model": None, "result": empty.model_dump()}
    try:
        model = parse_problem_model(problem_model_raw)
    except ValidationError:
        empty.malformed_model = True
        return {"changed": False, "problem_model": None, "result": empty.model_dump()}
    result = reconcile_gap_staffing_for_status(
        model,
        direction_id,
        new_gap_id,
        prior_gap_id,
        new_status=new_status,
        prior_status=prior_status,
        now_iso=now_iso,
        rationale=rationale,
    )
    return {
        "changed": result.changed,
        "problem_model": model.model_dump() if result.changed else None,
        "result": result.model_dump(),
    }


def _deduplicate_archived_gaps(gaps: list[Gap]) -> tuple[list[Gap], int]:
    """Collapse duplicate gap IDs in the archive, keeping the newest version.

    Returns (deduplicated_list, collapsed_count). Keeps the version with the
    most recent closed or opened timestamp for each gap id.
    """
    seen: dict[str, Gap] = {}
    for gap in gaps:
        gap_id = gap.id
        if gap_id not in seen:
            seen[gap_id] = gap
        else:
            # Keep the gap with the more recent timestamp.
            existing_time = seen[gap_id].closed or seen[gap_id].opened or ""
            new_time = gap.closed or gap.opened or ""
            if new_time > existing_time:
                seen[gap_id] = gap
    deduplicated = list(seen.values())
    collapsed_count = len(gaps) - len(deduplicated)
    return deduplicated, collapsed_count


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
        trimmed["past_moves_trimmed"] = len(gap.past_moves) - GAP_APPEND_ONLY_GC_FLOOR
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



def _diagnose_live_core_oversize(model: ProblemModel) -> str:
    """Name the largest live section dominating the byte oversize.

    Called after full eviction when the model is still over budget. The only
    remaining state is the unevictable live core (active gaps, live capabilities,
    active friction). Returns a human-readable description of the dominant section
    so the caller's error message is actionable.
    """
    sections = [
        (
            "active_gaps",
            len(json.dumps([g.model_dump() for g in model.gaps], default=str)),
            len(model.gaps),
        ),
        (
            "live_capabilities",
            len(json.dumps(
                [c.model_dump() for c in model.capabilities if not _is_evictable_capability(c)],
                default=str,
            )),
            sum(1 for c in model.capabilities if not _is_evictable_capability(c)),
        ),
        (
            "active_friction",
            len(json.dumps(
                [f.model_dump() for f in model.friction if not _is_evictable_friction(f)],
                default=str,
            )),
            sum(1 for f in model.friction if not _is_evictable_friction(f)),
        ),
    ]
    name, size, count = max(sections, key=lambda x: x[1])
    return f"{name} ({count} items, ~{size} bytes)"


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
       back through the strategic op endpoint. If the model is STILL over budget
       after all eviction (the live core alone exceeds the budget), raises
       ``ValueError`` naming the dominant oversize section — ``active_gaps``,
       ``live_capabilities``, or ``active_friction`` — so the caller surfaces an
       actionable error instead of silently committing an oversized model.

    It deliberately does NOT touch active gaps, live capabilities
    (``working``/``partial``), active friction, or pending confirmations — those
    are live, semantically meaningful state that the agent owns and merges
    non-destructively; dropping them could lose entries the agent never saw.
    ``archived_gaps`` is pure history (closed/abandoned, reference-only,
    re-fetchable by id), ``rejected_agent_edits`` is agent-readable bookkeeping,
    and terminal capabilities/friction are reference-only (the work is done or
    abandoned) — so oldest-first eviction of any of these is safe.

    Returns a dict like ``{"archived_gaps_evicted": ["g-x"],
    "rejected_agent_edits_trimmed": 2, "friction_evicted": ["f-y"],
    "archived_gaps_deduplicated": 5}`` recording what rolled off, suitable for
    folding into the revision-log diff.
    """
    summary: dict[str, Any] = {}
    evicted_ids: list[str] = []
    active_gap_tails_trimmed: list[dict[str, Any]] = []
    capabilities_evicted: list[str] = []
    friction_evicted: list[str] = []

    # Deduplicate archived gaps before eviction — collapse duplicates, keep newest.
    dedup_gaps, dedup_count = _deduplicate_archived_gaps(model.archived_gaps)
    if dedup_count > 0:
        model.archived_gaps = dedup_gaps
        summary["archived_gaps_deduplicated"] = dedup_count

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

    # Byte cap: evict reference-only / terminal state when over budget, then
    # raise loudly if the live core alone is still too large.
    ledger_trimmed = 0
    if _serialized_model_bytes(model) > PROBLEM_MODEL_BYTE_BUDGET:
        # Oldest-first so list.pop(0) drops the oldest archived gap.
        model.archived_gaps = sorted(model.archived_gaps, key=_archived_gap_recency_key)
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
        # After all eviction, if still over budget the live core itself is too
        # large.  Raise loudly so the caller surfaces an actionable error instead
        # of silently committing an oversized model to the durable store.
        if _serialized_model_bytes(model) > PROBLEM_MODEL_BYTE_BUDGET:
            section = _diagnose_live_core_oversize(model)
            raise ValueError(
                f"gc_problem_model: live core exceeds PROBLEM_MODEL_BYTE_BUDGET "
                f"({PROBLEM_MODEL_BYTE_BUDGET} bytes) after full eviction — "
                f"oversize section: {section}. Reduce the size of active state "
                f"(active gaps, live capabilities, or active friction) to bring "
                f"the model under the byte budget."
            )

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


def enforce_problem_model_size(
    model: ProblemModel,
) -> tuple[dict[str, Any], str | None]:
    """Run gc_problem_model and return (gc_summary, oversize_section_or_None).

    Mutates *model* in place (same as :func:`gc_problem_model`).  Returns:

    * ``(summary, None)``  — eviction brought model under
      :data:`PROBLEM_MODEL_BYTE_BUDGET`.
    * ``(partial_summary, section_str)`` — live core alone still exceeds the
      budget after full eviction; *section_str* names the dominant section.

    Never raises.  Callers decide whether to reject (GROW paths) or log-and-
    persist (SHRINK / background paths).
    """
    try:
        summary = gc_problem_model(model)
        return summary, None
    except ValueError as exc:
        msg = str(exc)
        section: str = msg
        prefix = "oversize section: "
        idx = msg.find(prefix)
        if idx >= 0:
            # Extract up to the first period so we get only the section name.
            section = msg[idx + len(prefix) :].split(".")[0].strip()
        return {}, section


def _parse_iso_to_naive_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string into a naive-UTC datetime, or ``None``.

    Returns ``None`` for empty, non-string, or unparseable input (a deferral's
    ``hold_until`` may legitimately be a free-form milestone/version tag rather
    than a date). Tz-aware inputs are converted to UTC and stripped so they
    compare correctly against the naive-UTC ``now`` the callers pass."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _deferral_is_stale(deferral: GapDeferral, now: datetime | None) -> bool:
    """Whether a gap's deferral has aged past its revisit point.

    Two triggers, in order:

    1. ``hold_until`` is a parseable timestamp in the past — the revisit the
       strategist scheduled is overdue.
    2. ``hold_until`` is absent/unparseable (a free-form milestone) AND
       ``created_at`` is a parseable timestamp older than
       :data:`DEFERRAL_STALE_GRACE_DAYS` — the deferral has simply lapsed.

    Conservative on missing data: when ``now`` is unparseable, or neither
    ``hold_until`` nor ``created_at`` yields a usable date, the deferral is NOT
    considered stale (returns ``False``) so a gap is never archived on an
    ambiguous signal."""
    if now is None:
        return False
    hold_until = _parse_iso_to_naive_utc(deferral.hold_until)
    if hold_until is not None:
        return hold_until < now
    created_at = _parse_iso_to_naive_utc(deferral.created_at)
    if created_at is not None:
        return created_at < now - timedelta(days=DEFERRAL_STALE_GRACE_DAYS)
    return False


def auto_archive_stale_problem_model_items(
    model: ProblemModel,
    now_iso: str,
    live_direction_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Count-threshold-gated TTL auto-archival for gaps, friction, and capabilities.

    Decays only items that are PROVABLY inert — no live direction staffing via
    ``active_moves``, any deferral expired, age past a TTL, not recently
    touched — and ONLY when counts exceed the healthy thresholds
    (:data:`MODEL_SIZE_HEALTHY_GAPS`, :data:`MODEL_SIZE_HEALTHY_FRICTION`,
    :data:`MODEL_SIZE_HEALTHY_CAPABILITIES`).  Err toward NOT archiving something
    a strategist might still need; leave :func:`gc_problem_model` as the
    backstop for byte-budget overflow, unchanged.

    Mutates ``model`` in place and returns a small summary dict (empty on no-op).
    Runs BEFORE the :func:`gc_problem_model` 200-item/byte backstop in
    :func:`apply_agent_update` and composes cleanly with it.

    Three actions, each touching ONLY terminal-ish / reference-only state:

    1. **Aged-deferred, unstaffed gaps → archive.** Each ACTIVE gap that carries a
       ``deferral``, has no live direction staffing (``active_moves`` has no
       overlap with ``live_direction_ids``), and whose deferral has aged past
       its revisit point is marked ``abandoned`` and moved into ``archived_gaps``.
       A gap with active live staffing, a fresh/held deferral, or no deferral at
       all is NEVER touched.  When ``live_direction_ids`` is empty the check falls
       back to ``active_moves`` being empty (backward-compatible).
    2. **Terminal friction surplus → drop.** Bounds total friction toward
       :data:`MODEL_SIZE_HEALTHY_FRICTION` by dropping terminal
       (``resolved`` then ``mitigated``) friction oldest-first, only as far as
       needed.
    3. **Stale planned capabilities surplus → drop.** Bounds capabilities toward
       :data:`MODEL_SIZE_HEALTHY_CAPABILITIES` by dropping ``planned``
       capabilities oldest-first.

    Returns a dict like ``{"gaps_lifecycle_archived": ["g-x"],
    "friction_lifecycle_evicted": ["f-y"],
    "capabilities_lifecycle_evicted": ["cap-z"]}`` — only the keys that had
    activity are present; an all-live model returns ``{}``.
    """
    summary: dict[str, Any] = {}
    now = _parse_iso_to_naive_utc(now_iso)

    # 1. Aged-deferred, unstaffed gaps → mark abandoned so archive_closed_gaps
    #    moves them into archived_gaps (reversible, re-fetchable by id).
    #    Gated on active gap count exceeding the healthy threshold — a model
    #    at or under the threshold is never touched even if some gaps are inert.
    #    A gap is inert only when NO member of its active_moves is a live
    #    direction (provably no ongoing work).  When live_direction_ids is
    #    unavailable the check conservatively falls back to active_moves being
    #    empty — a gap with any move entries is skipped.
    active_count = sum(1 for g in model.gaps if g.status == "active")
    archived_gap_ids: list[str] = []
    if active_count > MODEL_SIZE_HEALTHY_GAPS:
        for gap in model.gaps:
            if gap.status != "active" or gap.deferral is None:
                continue
            has_live_staff = (
                bool(set(gap.active_moves) & live_direction_ids)
                if live_direction_ids
                else bool(gap.active_moves)
            )
            if has_live_staff:
                continue
            if not _deferral_is_stale(gap.deferral, now):
                continue
            gap.status = "abandoned"
            gap.closed = now_iso
            gap.history.append(
                GapHistoryEvent(
                    ts=now_iso,
                    event="lifecycle_auto_archived",
                    note=(
                        "auto-archived: deferral aged past revisit point "
                        "(lifecycle-bound pass)"
                    ),
                )
            )
            archived_gap_ids.append(gap.id)
    if archived_gap_ids:
        archive_closed_gaps(model)
        summary["gaps_lifecycle_archived"] = archived_gap_ids

    # 2. Terminal friction surplus → drop toward the healthy friction threshold,
    #    resolved before mitigated, oldest-first. Active friction is never
    #    dropped, so an all-active list stays untouched.
    friction_overage = len(model.friction) - MODEL_SIZE_HEALTHY_FRICTION
    if friction_overage > 0:
        resolved = [i for i, f in enumerate(model.friction) if f.status == "resolved"]
        mitigated = [i for i, f in enumerate(model.friction) if f.status == "mitigated"]
        evict_idx = set((resolved + mitigated)[:friction_overage])
        if evict_idx:
            evicted_friction: list[str] = []
            kept_friction: list[Friction] = []
            for i, fric in enumerate(model.friction):
                if i in evict_idx:
                    evicted_friction.append(fric.id)
                else:
                    kept_friction.append(fric)
            model.friction = kept_friction
            summary["friction_lifecycle_evicted"] = evicted_friction

    # 3. Stale planned capabilities surplus → drop toward the healthy capability
    #    threshold, oldest-first. working/partial capabilities are never dropped.
    cap_overage = len(model.capabilities) - MODEL_SIZE_HEALTHY_CAPABILITIES
    if cap_overage > 0:
        evicted_caps: list[str] = []
        kept_caps: list[Capability] = []
        for cap in model.capabilities:
            if len(evicted_caps) < cap_overage and _is_evictable_capability(cap):
                evicted_caps.append(cap.name)
            else:
                kept_caps.append(cap)
        if evicted_caps:
            model.capabilities = kept_caps
            summary["capabilities_lifecycle_evicted"] = evicted_caps

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


# ---------------------------------------------------------------------------
# Auto-clear moot pending confirmations
# ---------------------------------------------------------------------------


class ConfirmationResolutionSignals(BaseModel):
    """Positively-detected runtime availability signals used to auto-clear moot
    pending confirmations.

    Every flag is fail-closed (defaults ``False``): an empty/unpopulated signal
    clears nothing, so :func:`auto_clear_resolved_confirmations` only ever
    removes an item when the moot condition is POSITIVELY proven by the runtime.
    The reaper sweep populates these from the same credential/access resolution
    the dispatcher already uses (owner LLM-account readiness, cached project
    repo token); keeping this model pure makes the detection logic unit-testable
    without a DB or the in-memory pool.
    """

    model_config = ConfigDict(extra="forbid")

    llm_credentials_ready: bool = False
    """Owner has >=1 enabled, runtime-ready LLM account (OAuth token / Secret
    Manager credential present) — the account-credential the runtime resolves at
    dispatch. Mirrors ``max_plan_pool._db_account_runtime_ready``."""

    repo_access_ready: bool = False
    """Project has a currently-live cached GitHub installation token, proving
    repo access is granted right now. Mirrors
    ``github_token_resolver.get_cached_project_token``."""


# Keyword gates: a pending item is only bound to a resolution signal when its
# free text clearly references that resource class. PendingConfirmation carries
# no structured credential id, so this textual gate is what keeps the clear
# conservative — a ``credentials_needed`` item about some OTHER secret (a
# dataset key, a third-party API token) is left in place even when LLM
# credentials are ready.
_LLM_CREDENTIAL_TERMS = (
    "llm",
    "anthropic",
    "claude",
    "oauth",
    "api key",
    "api-key",
    "apikey",
    "max plan",
    "max-plan",
    "model credential",
    "model access",
)
_REPO_ACCESS_TERMS = (
    "repo",
    "repository",
    "github",
    "git access",
    "clone",
    "push access",
)


def _confirmation_is_moot(
    item: PendingConfirmation, signals: ConfirmationResolutionSignals
) -> bool:
    """Whether a single pending confirmation is POSITIVELY resolved.

    Strictly conservative and fail-closed:

    - only ``credentials_needed`` / ``access_needed`` are ever eligible;
      ``intent_change`` / ``major_decision`` require a human decision and are
      NEVER auto-removed;
    - the relevant runtime signal must be positively available AND the item's
      free text must reference that resource class. Anything ambiguous returns
      ``False`` (leave it for the operator to dismiss).
    """
    text = f"{item.proposal or ''}\n{item.rationale or ''}".lower()
    if item.type == "credentials_needed":
        return signals.llm_credentials_ready and any(
            t in text for t in _LLM_CREDENTIAL_TERMS
        )
    if item.type == "access_needed":
        return signals.repo_access_ready and any(
            t in text for t in _REPO_ACCESS_TERMS
        )
    return False


def auto_clear_resolved_confirmations(
    model: ProblemModel,
    signals: ConfirmationResolutionSignals,
    now_iso: str,
) -> list[str]:
    """Remove pending confirmations whose moot condition is positively detected.

    Pure, DB-free counterpart to the operator dismiss/accept endpoints
    (``admin_projects.dismiss_pending_confirmation``): it round-trips the
    ``ProblemModel`` identically — removes the item(s), bumps ``revision``,
    stamps ``last_updated`` / ``last_updated_by='user'`` (operator-surface
    consistency), and appends one ``revision_log`` entry. Mutates ``model`` in
    place and returns the ids cleared (empty list => no change, so the caller
    keeps the durable column byte-identical on the no-op path).

    Only ``credentials_needed`` / ``access_needed`` items are eligible, and only
    when :func:`_confirmation_is_moot` positively proves resolution — see that
    helper for the fail-closed contract. Idempotent and safe to run every sweep:
    a second call with the same signals is a no-op because the cleared items are
    already gone.
    """
    if not model.pending_user_confirmation:
        return []
    cleared: list[str] = []
    kept: list[PendingConfirmation] = []
    for item in model.pending_user_confirmation:
        if _confirmation_is_moot(item, signals):
            cleared.append(item.id)
        else:
            kept.append(item)
    if not cleared:
        return []
    model.pending_user_confirmation = kept
    model.revision += 1
    model.last_updated = now_iso
    model.last_updated_by = "user"
    append_revision_log(
        model,
        (
            f"auto-cleared {len(cleared)} moot pending confirmation(s): "
            "credential/access now available"
        ),
        {"auto_cleared_pending_confirmations": cleared},
    )
    return cleared


PENDING_CONFIRMATION_STALE_DECAY_DAYS = 30
"""Age (in days) after which a ``pending_user_confirmation`` item is considered stale
and eligible for automatic removal by the reaper decay sweep, regardless of type.

Applies to ALL four types: ``intent_change``, ``major_decision``,
``credentials_needed``, and ``access_needed``.  The moot-confirmation sweep
(:func:`auto_clear_resolved_confirmations`) remains the fast-path that clears
credential/access items when the system can positively prove resolution; this
constant governs the age-based fallback for items the moot sweep can never remove
(``intent_change`` / ``major_decision``) and for credential/access items whose moot
signal never fires."""


def decay_stale_pending_confirmations(
    model: ProblemModel,
    now_iso: str,
) -> list[str]:
    """Remove ``pending_user_confirmation`` items older than
    :data:`PENDING_CONFIRMATION_STALE_DECAY_DAYS`.

    Pure, DB-free counterpart to :func:`auto_clear_resolved_confirmations`: it
    round-trips the ``ProblemModel`` identically — removes the item(s), bumps
    ``revision``, stamps ``last_updated`` / ``last_updated_by='user'``, and appends
    one ``revision_log`` entry.  Mutates ``model`` in place and returns the ids
    decayed (empty list => no change).

    Applies to ALL four confirmation types.  Items with a missing or unparseable
    ``created_at`` are conservatively left in place (fail-closed on ambiguous data).
    Idempotent: removed items are already gone on a second call.
    """
    if not model.pending_user_confirmation:
        return []
    now = _parse_iso_to_naive_utc(now_iso)
    if now is None:
        return []
    cutoff = now - timedelta(days=PENDING_CONFIRMATION_STALE_DECAY_DAYS)
    decayed: list[str] = []
    kept: list[PendingConfirmation] = []
    for item in model.pending_user_confirmation:
        created = _parse_iso_to_naive_utc(item.created_at)
        if created is not None and created < cutoff:
            decayed.append(item.id)
        else:
            kept.append(item)
    if not decayed:
        return []
    model.pending_user_confirmation = kept
    model.revision += 1
    model.last_updated = now_iso
    model.last_updated_by = "user"
    append_revision_log(
        model,
        f"age-decayed {len(decayed)} stale pending confirmation(s) "
        f"(>{PENDING_CONFIRMATION_STALE_DECAY_DAYS}d old)",
        {"decayed_pending_confirmations": decayed},
    )
    return decayed


def append_self_reflection(
    log_raw: list | None,
    entry: SelfReflectionEntry,
) -> list[dict]:
    """Append a reflection entry to the project's self-reflection log,
    enforcing :data:`SELF_REFLECTION_LOG_MAX`. Returns the new list ready
    for JSONB persistence.

    Deduplicates by cycle_task_id to prevent re-appending identical entries
    when the strategic-completion handler is replayed from a saved payload."""
    log = list(log_raw or [])
    # Dedup: skip if an entry with the same cycle_task_id already exists
    if entry.cycle_task_id and any(
        e.get("cycle_task_id") == entry.cycle_task_id for e in log
    ):
        return log
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

DEFERRAL_STALE_GRACE_DAYS = 30
"""Grace window (days) before a deferred, unstaffed gap is auto-archived by
:func:`auto_archive_stale_problem_model_items` when its deferral carries no usable
``hold_until``.

A gap's ``deferral`` is an explicit "real but intentionally not staffed this
cycle" marker (see :class:`GapDeferral`). When the deferral names a
``hold_until`` that has passed, the revisit is overdue and the gap is archived.
When ``hold_until`` is absent or unparseable (a free-form milestone/version
tag rather than a date), the fallback trigger is age: a deferral whose
``created_at`` is older than this window has sat un-revisited long enough that
the strategist has effectively let it lapse. Generous by design — 30 days is
far longer than a strategic cycle, so a freshly-deferred gap is never touched;
only a genuinely stale one is. Archival is reversible: the gap moves to
``archived_gaps`` and stays re-fetchable by id, so a false positive costs a
lookup, not data."""

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
    if pending_deleted := diff.get("pending_user_confirmation_deleted"):
        parts.append(
            f"cleared {len(pending_deleted)} pending confirmation(s)"
        )
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
    if lc_gaps := diff.get("gaps_lifecycle_archived"):
        parts.append(f"lifecycle-archived {len(lc_gaps)} aged-deferred gap(s)")
    if lc_friction := diff.get("friction_lifecycle_evicted"):
        parts.append(f"lifecycle-dropped {len(lc_friction)} terminal friction")
    if lc_caps := diff.get("capabilities_lifecycle_evicted"):
        parts.append(
            f"lifecycle-dropped {len(lc_caps)} planned capability(ies)"
        )
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


def resolve_project_purpose(project) -> str:
    """Return one normalized, non-placeholder project purpose.

    Operator-owned ``intent.user_purpose`` wins, followed by the current
    problem-model purpose and the two legacy vision fields.
    """
    pm = getattr(project, "problem_model", None) or {}
    intent = pm.get("intent") or {}
    vision = getattr(project, "vision", None) or {}

    for candidate in (
        intent.get("user_purpose"),
        intent.get("purpose"),
        vision.get("user_purpose"),
        vision.get("purpose"),
    ):
        if not isinstance(candidate, str):
            continue
        normalized = candidate.strip()
        if not _is_placeholder_purpose(normalized):
            return normalized
    return ""


def project_has_purpose(project) -> bool:
    """True when the canonical resolver finds an established purpose."""
    return bool(resolve_project_purpose(project))


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
        # Durable operator copy: update user_purpose to mirror the new operator-set
        # purpose so the field survives any future agent strategic-cycle rewrites.
        validated.intent.user_purpose = purpose
    else:
        # No intent patch — the shallow merged.update(patch) replaced the intent
        # dict entirely, dropping user_purpose from the merged object. Carry it
        # forward explicitly so an unrelated patch (context-only, constraints-only)
        # never silently blanks the durable operator purpose.
        validated.intent.user_purpose = current.intent.user_purpose
    validated.revision = current.revision + 1
    validated.last_updated = now_iso
    validated.last_updated_by = "user"
    return validated


_PLACEHOLDER_PURPOSE_RE = re.compile(r"^<[^>]+>$")
"""Matches XML-tag-style schema placeholders like ``<from analysis>``."""


def _is_placeholder_purpose(purpose: str) -> bool:
    """True when purpose is blank or a known schema-placeholder pattern.

    Mirrors the agent-side ``isSchemaPlaceholder`` check in
    ``agent/src/prompts/thinker.ts`` so the backend can detect and heal the
    same broken states the agent rejects.
    """
    stripped = purpose.strip()
    return not stripped or stripped == "..." or bool(_PLACEHOLDER_PURPOSE_RE.match(stripped))


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

    # Fallback: when user_purpose (the durable operator-set copy) is non-empty
    # and proposed purpose is blank or a schema-placeholder, restore it
    # regardless of intent.source.  This heals the state where source drifted
    # to null while user_purpose still holds the operator's real text — a case
    # the source-gated block above cannot reach (purpose_was_operator=False).
    if _is_placeholder_purpose(proposed.intent.purpose) and current.intent.user_purpose.strip():
        proposed.intent.purpose = current.intent.user_purpose
        if proposed.intent.source is None:
            proposed.intent.source = Source(
                type="user_message",
                excerpt=current.intent.user_purpose[:120],
            )

    # ``context`` / ``constraints`` are operator-owned when the prior model was
    # last written by the user (the onboarding seed stamps last_updated_by).
    operator_owned = purpose_was_operator or current.last_updated_by == "user"
    if (
        operator_owned
        and not _context_is_blank(current.context)
        and _context_is_blank(proposed.context)
    ):
        proposed.context = current.context.model_copy(deep=True)
    if current.constraints and not proposed.constraints:
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

# Fields ALWAYS compacted out of the agent-view problem_model (see
# ``strategic_problem_view._COMPACT_*_FIELDS`` — a test keeps the two modules'
# lists in sync). The strategist never sees them, so a blank in a write-back
# means "never received", NOT "clear" — restore them unconditionally on merge.
# This is distinct from the floor path above: the floor only fires under byte
# pressure and is marker-gated; the compaction runs every cycle and the agent
# cannot express a deliberate clear for a field it never saw, so preserving is
# the only safe direction (over-preserving is the project's accepted trade-off).
_COMPACT_CAPABILITY_FIELDS = ("source", "evidence_refs")
_COMPACT_GAP_FIELDS = ("source", "evidence_refs")
_COMPACT_FRICTION_FIELDS = ("source", "mitigations_tried")


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
        (
            wm
            for wm in (current.history_compacted_at, merged.history_compacted_at)
            if wm
        ),
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
        (
            wm
            for wm in (current.past_moves_compacted_at, merged.past_moves_compacted_at)
            if wm
        ),
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
        # Always-compacted provenance fields are restored unconditionally — the
        # agent never saw them, so omission is not a clear.
        _restore_floored_fields(c, merged, _COMPACT_GAP_FIELDS)
        return merged

    def _merge_cap_preserving(c: Capability, p: Capability) -> Capability:
        if caps_floored:
            _restore_floored_fields(c, p, _CAP_FLOOR_FIELDS)
        _restore_floored_fields(c, p, _COMPACT_CAPABILITY_FIELDS)
        return p

    def _merge_friction_preserving(c: Friction, p: Friction) -> Friction:
        merged = p.model_copy(deep=True)
        # Make friction status monotonic: once a friction entry is terminal
        # (resolved/mitigated), a stale concurrent write must NOT regress it
        # back to active.
        if _is_evictable_friction(c) and merged.status == "active":
            merged.status = c.status
        if friction_floored:
            _restore_floored_fields(c, merged, _FRICTION_FLOOR_FIELDS)
        _restore_floored_fields(c, merged, _COMPACT_FRICTION_FIELDS)
        return merged

    archived_gap_ids = {g.id for g in proposed.archived_gaps}
    current_live_gaps = [g for g in current.gaps if g.id not in archived_gap_ids]
    proposed_live_gaps = [
        g for g in proposed.gaps if g.id not in archived_gap_ids
    ]

    proposed.capabilities = _upsert_by_key(
        current.capabilities,
        proposed.capabilities,
        lambda c: c.name,
        del_caps,
        merge_fn=_merge_cap_preserving,
    )
    proposed.gaps = _upsert_by_key(
        current_live_gaps,
        proposed_live_gaps,
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


def active_direction_gap_ids(directions: list[dict] | None) -> set[str]:
    """Return the set of gap_ids referenced by active directions.

    Used to compute the protected set before calling
    :func:`apply_agent_update` so a gap deletion that would orphan an active
    direction can be surfaced rather than silently applied.
    """
    result: set[str] = set()
    for d in directions or []:
        if d.get("status") == "active":
            gid = d.get("gap_id")
            if gid:
                result.add(str(gid))
    return result


def normalize_active_moves_writeback(
    new_pm_raw: dict,
    active_dir_ids: set[str],
    now_iso: str,
) -> tuple[dict, list[str]]:
    """Normalize gap active_moves and record corrections in the revision log.

    Shared helper invoked by both the op-endpoint (``set_problem_model``) and
    the log-parse fallback in ``strategic_completion`` so the two write paths
    cannot diverge on active-moves normalization.

    Returns ``(updated_raw, corrections)`` where ``corrections`` is the list of
    change messages from :func:`normalize_gap_active_moves`.  When empty the
    raw dict is returned unchanged and no revision-log entry is written.
    """
    pm_obj = parse_problem_model(new_pm_raw)
    corrections = normalize_gap_active_moves(pm_obj, active_dir_ids, now_iso)
    if corrections:
        append_revision_log(
            pm_obj,
            "auto-normalized stale active_moves",
            {"active_moves_normalized": corrections},
        )
        new_pm_raw = pm_obj.model_dump()
    return new_pm_raw, corrections


_WIPE_RISK_SENTINEL = object()  # detect_wipe_risk: sentinel for absent-key check


def detect_wipe_risk(
    current: ProblemModel,
    proposed: ProblemModel,
    deletions: Any,
    *,
    proposed_raw: dict | None = None,
) -> dict[str, bool]:
    """Detect a proposed full-collection wipe without explicit _deletions coverage.

    Called BEFORE :func:`_merge_agent_owned_collections` so ``proposed``
    still reflects what the agent actually submitted (pre-merge). The merge
    itself prevents the wipe; this function surfaces the intent so callers
    can fire an alert.

    ``proposed_raw`` is the raw dict the agent submitted BEFORE pydantic
    validation (after directives like ``_deletions`` are lifted out). When
    supplied, a collection is only flagged when the raw dict EXPLICITLY
    submitted an empty list for that key.  An absent key is a partial update
    — the merge's omission=keep semantics handle it, and no alarm is needed.
    When ``proposed_raw`` is not supplied (legacy / test callers), the
    function falls back to checking the parsed model's empty-collection field.

    Returns a dict with ``gaps_wipe_risk`` and/or ``capabilities_wipe_risk``
    set to ``True`` when the condition fires:
      - durable model has at least one entry
      - proposed payload explicitly submitted an empty list for that collection
      - ``_deletions`` carries no explicit removals for that collection
        (a full explicit deletion is intentional and not flagged)
    """
    result: dict[str, bool] = {}

    def _explicitly_empty(field: str) -> bool:
        if proposed_raw is None:
            # Fallback when raw dict is unavailable: use the parsed model.
            return not getattr(proposed, field)
        raw_val = proposed_raw.get(field, _WIPE_RISK_SENTINEL)
        if raw_val is _WIPE_RISK_SENTINEL:
            return False  # absent key is a partial update, not a wipe
        return isinstance(raw_val, list) and len(raw_val) == 0

    del_gap_ids = set(_as_str_list(deletions, "gaps"))
    if current.gaps and _explicitly_empty("gaps") and not del_gap_ids:
        result["gaps_wipe_risk"] = True
    del_cap_names = set(_as_str_list(deletions, "capabilities"))
    if current.capabilities and _explicitly_empty("capabilities") and not del_cap_names:
        result["capabilities_wipe_risk"] = True
    return result


def apply_agent_update(
    current_raw: dict | None,
    proposed_raw: dict,
    now_iso: str,
    *,
    baseline_raw: dict | None = None,
    protected_gap_ids: set[str] | None = None,
    live_direction_ids: frozenset[str] = frozenset(),
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
    # Filter protected gap_ids from deletions BEFORE the merge pass so gaps
    # still referenced by active directions are preserved, not silently wiped.
    # Non-destructive: blocked ids are surfaced in the diff; the write proceeds.
    _blocked_gap_deletions: list[str] = []
    if protected_gap_ids and isinstance(deletions, dict):
        _raw_gap_dels = _as_str_list(deletions, "gaps")
        if _raw_gap_dels:
            _blocked_gap_deletions = [g for g in _raw_gap_dels if g in protected_gap_ids]
            if _blocked_gap_deletions:
                deletions = {
                    **deletions,
                    "gaps": [g for g in _raw_gap_dels if g not in protected_gap_ids],
                }
    # Per-gap history compaction directive: ``{gap_id: {history?: ISO,
    # past_moves?: ISO}}``.  Lifted out like _deletions so ProblemModel
    # (extra="forbid") does not reject it.
    compactions_raw = proposed_raw.pop("_compactions", None)
    # Read-time agent-view markers the packer injects into the floored model the
    # agent sees. They are not part of the durable model (extra="forbid" would
    # reject them), so lift them out before validation. ``_agent_view_floor_applied``
    # drives floor-aware field preservation below; ``_gap_memory_trimmed`` is
    # purely informational and discarded. ``truncated`` is injected by the
    # byte-budget packer when a value is truncated — strip it here so an agent
    # that echoes back a truncated view does not produce a permanent 400.
    floor_applied = proposed_raw.pop("_agent_view_floor_applied", None)
    # The lean digest (the DEFAULT problem_model shape shipped each cycle — see
    # strategic_problem_view._agent_view_problem_model_lean) records in
    # ``_agent_view_lean`` exactly which fields/sections it reduced before the
    # agent saw them. Merge that record into the floor-applied restoration set
    # so the same floor-aware preservation machinery restores the stripped
    # detail on write-back: gap ``description``/``evidence``/``active_moves``,
    # capability ``notes``/``backing_paths``/``source``/``absorbs``, and
    # friction ``description``/``seen_in``/``mitigations_tried``/``source``/
    # ``promoted_to_gap_id``. An agent that edits the digest and echoes it back
    # (marker included) cannot wipe durable detail it never saw. Like the floor
    # marker, the echo is the agent's signal of what it actually saw — a full
    # model submitted after ``./fetch problem-model`` omits it, so a blank there
    # remains a genuine clear (see test
    # ``test_apply_agent_update_blank_gap_field_clears_without_floor_marker``).
    lean_view = proposed_raw.pop("_agent_view_lean", None)
    if isinstance(lean_view, dict):
        _lean_floor = dict(floor_applied) if isinstance(floor_applied, dict) else {}
        _stripped = list(_lean_floor.get("stripped_gap_fields") or [])
        for _field in ("description", "evidence", "active_moves"):
            if _field not in _stripped:
                _stripped.append(_field)
        _lean_floor["stripped_gap_fields"] = _stripped
        for _count_key in ("summarized_capabilities", "summarized_friction"):
            if lean_view.get(_count_key):
                _lean_floor[_count_key] = max(
                    int(_lean_floor.get(_count_key) or 0),
                    int(lean_view[_count_key]),
                )
        floor_applied = _lean_floor
    proposed_raw.pop("_gap_memory_trimmed", None)
    # The compaction marker is informational at write time — restore of the
    # compacted fields is unconditional (see _COMPACT_*_FIELDS), so the echoed
    # marker is only lifted to satisfy ProblemModel's extra="forbid".
    proposed_raw.pop("_agent_view_compacted", None)
    proposed_raw.pop("truncated", None)

    proposed = ProblemModel.model_validate(proposed_raw)
    # Sanitize current_raw before validation: strip any extra top-level key that
    # ProblemModel (extra="forbid") would reject. A poisoned stored row — e.g.
    # one that carries a stale "truncated": true packer marker — otherwise wedges
    # every subsequent agent write permanently with 400. Stripping unknown keys
    # is safe: model_dump() never emits them, so they were never durable state.
    # Known underscore-prefixed directives (_deletions, _compactions, …) are
    # already lifted above from proposed_raw; current_raw should never carry
    # them, but strip them here too as a belt-and-suspenders guard.
    _known_model_keys = set(ProblemModel.model_fields)
    _current_sanitized: dict = {}
    _stripped_keys: list[str] = []
    for _k, _v in (current_raw or {}).items():
        if _k in _known_model_keys:
            _current_sanitized[_k] = _v
        else:
            _stripped_keys.append(_k)
    if _stripped_keys:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "apply_agent_update: stripped unknown top-level keys from stored "
            "problem_model before parse (self-heal): %s",
            sorted(_stripped_keys),
        )
    current = parse_problem_model(_current_sanitized if _stripped_keys else current_raw)

    # user_purpose is a durable operator-owned copy of the purpose; agents must
    # never change it (it is set exclusively by apply_user_intent_patch). Restore
    # it unconditionally before any merge pass so the diff and 3-way merge see the
    # correct value. Constraints are NOT unconditionally restored here — agents CAN
    # update constraints when no operator edit occurred (3-way merge: current ==
    # baseline → agent wins). The blank/drop case is handled by
    # _preserve_operator_intent below (which now guards regardless of operator_owned
    # to close the legacy-path clobber where dropped source makes operator_owned=False).
    proposed.intent.user_purpose = current.intent.user_purpose

    proposed.revision = current.revision + 1
    proposed.last_updated = now_iso
    proposed.last_updated_by = "agent"

    # Deduplicate archived_gaps by id: keep current, add only proposed entries not already present.
    archived_by_id = {g.id: g for g in current.archived_gaps}
    for g in proposed.archived_gaps:
        if g.id not in archived_by_id:
            archived_by_id[g.id] = g
    proposed.archived_gaps = list(archived_by_id.values())
    proposed.revision_log = list(current.revision_log)

    # 3-way operator-wins merge for operator-owned fields when a cycle baseline
    # is available. Runs BEFORE blank-preservation and the diff so a concurrent
    # operator edit is honored and reported faithfully.
    rejected_edits: list[dict[str, Any]] = []
    if baseline_raw is not None:
        baseline = parse_problem_model(sanitize_problem_model_raw(baseline_raw))
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

    # Detect wipe-without-deletions BEFORE the merge restores omitted entries.
    # The merge below is non-destructive so the actual wipe is prevented, but
    # a proposed payload with zero gaps/capabilities and no _deletions signals
    # that the agent either truncated its view or is attempting a silent wipe.
    # Pass proposed_raw so detect_wipe_risk can distinguish an explicit empty
    # list (wipe-without-deletions) from an absent key (partial update).
    _wipe_risk = detect_wipe_risk(current, proposed, deletions, proposed_raw=proposed_raw)

    # Non-destructive merge of agent-owned collections: an agent acting on a
    # trimmed/dropped payload view must not wipe entries it never saw. Runs
    # before the diff so the diff reflects the true merged state.
    _merge_agent_owned_collections(
        current, proposed, deletions, floor_applied, pm_compacted_gaps
    )

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
        ("pending_user_confirmation_deleted", "pending_user_confirmation"),
    ):
        removed = sorted(_as_str_list(deletions, del_key))
        if removed:
            diff[diff_key] = removed
    # Surface blocked gap deletions (already filtered before merge above).
    if _blocked_gap_deletions:
        diff["gaps_deletion_blocked"] = sorted(_blocked_gap_deletions)
    # Surface wipe-without-deletions signals detected before the merge.
    if _wipe_risk.get("gaps_wipe_risk"):
        diff["gaps_wipe_risk"] = True
    if _wipe_risk.get("capabilities_wipe_risk"):
        diff["capabilities_wipe_risk"] = True

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

    # Lifecycle-bound pass: eagerly archive/drop terminal-ish state (aged-deferred
    # unstaffed gaps, terminal friction surplus, stale planned-capability surplus)
    # toward the healthy thresholds, so the durable model self-bounds instead of
    # relying on manual per-cycle compaction. Runs AFTER archival (so this cycle's
    # closures are already settled) and BEFORE the gc_problem_model backstop, and
    # touches ONLY the same reference-only/terminal class gc treats as evictable —
    # just applied at the healthy threshold instead of the 200-item cap. Its
    # summary folds into the diff exactly like gc's, so the actions are observable
    # in the revision log; an all-live model returns {} and leaves the diff (and
    # the serialized model) byte-identical.
    lifecycle_summary = auto_archive_stale_problem_model_items(
        proposed, now_iso, live_direction_ids
    )
    if lifecycle_summary:
        diff.update(lifecycle_summary)

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
    try:
        gc_summary = gc_problem_model(proposed)
    except ValueError:
        # Live core alone exceeds PROBLEM_MODEL_BYTE_BUDGET after full eviction.
        # Surface as a structured diff flag so callers can reject loudly (HTTP 400
        # for the op endpoint) rather than propagating an opaque uncaught exception.
        diff["live_core_exceeds_budget"] = _diagnose_live_core_oversize(proposed)
        gc_summary = {}
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


# ---------------------------------------------------------------------------
# Gap staffing normalization
# ---------------------------------------------------------------------------


def normalize_gap_active_moves(
    model: ProblemModel,
    active_direction_ids: set[str],
    now_iso: str,
) -> list[str]:
    """Normalize gap.active_moves against the currently-active direction set.

    For each gap, any direction ID in active_moves that is NOT in
    active_direction_ids (paused, expired, staged, etc.) is pruned into
    past_moves. A gap that becomes unstaffed as a result and has no existing
    deferral is auto-deferred. Returns correction messages describing each
    change. Mutates model in place. Pure (no I/O).
    """
    corrections: list[str] = []
    for gap in model.gaps:
        stale = [m for m in gap.active_moves if m not in active_direction_ids]
        if not stale:
            continue
        gap.active_moves = [m for m in gap.active_moves if m in active_direction_ids]
        past_seen = set(gap.past_moves)
        for m in stale:
            if m not in past_seen:
                gap.past_moves.append(m)
                past_seen.add(m)
        corrections.append(f"gap {gap.id}: moved {stale} from active_moves to past_moves")
        if not gap.active_moves and not gap.deferral:
            trigger = ", ".join(stale)
            gap.deferral = GapDeferral(
                rationale=f"auto-deferred: staffing direction(s) {trigger} not active",
                created_at=now_iso,
            )
            corrections.append(f"gap {gap.id}: auto-deferred (active_moves emptied)")
    return corrections


# ---------------------------------------------------------------------------
# Direction–gap referential-integrity guard
# ---------------------------------------------------------------------------

GapLinkClassification = Literal["none", "valid", "closed", "dangling"]
"""Classification of a direction's gap_id against the current problem model.

``none``     — no gap_id set (ungrounded direction).
``valid``    — gap_id resolves to an active or blocked gap.
``closed``   — gap_id resolves to a closed/abandoned gap (still in ``gaps``
               with a terminal status, or already in ``archived_gaps``).
``dangling`` — gap_id matches no gap at all.
"""


def classify_gap_link(
    gap_id: str | None,
    model: ProblemModel,
) -> GapLinkClassification:
    """Classify a direction's gap_id link against the current problem model.

    Pure and side-effect-free.  Used as the foundation for write-time
    enforcement at every direction/gap write path.
    """
    if not gap_id:
        return "none"
    for gap in model.gaps:
        if gap.id == gap_id:
            return "valid" if gap.status in ("active", "blocked") else "closed"
    for gap in model.archived_gaps:
        if gap.id == gap_id:
            return "closed"
    return "dangling"


def check_gap_id_write(
    gap_id: str | None,
    problem_model_raw: dict | None,
) -> tuple[str | None, list[str]]:
    """Return (error_reason, open_gap_ids) for a proposed gap_id write.

    Returns ``(None, [])`` when the gap_id is absent, the problem model cannot
    be parsed, or the gap resolves to an open (active/blocked) gap.
    Returns ``(classification, open_ids)`` when the gap is closed or dangling,
    where ``open_ids`` lists all currently-active or -blocked gap IDs so the
    caller can surface them in an error response.
    """
    if not gap_id or not problem_model_raw:
        return None, []
    try:
        pm = ProblemModel.model_validate(problem_model_raw)
    except Exception:  # noqa: BLE001
        return None, []
    cls = classify_gap_link(gap_id, pm)
    if cls in ("valid", "none"):
        return None, []
    return cls, [g.id for g in pm.gaps if g.status in ("active", "blocked")]
