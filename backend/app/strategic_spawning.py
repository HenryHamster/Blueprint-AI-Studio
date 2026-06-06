"""Strategic cycle spawning functions.

Handles Phase 1 (design_doc_update) and Phase 2 (strategic_review) of the
strategic review cycle. Extracted from autonomous_research.py to free up
function slots in that module.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app import strategic_cycle
from app.direction_metrics import _compute_direction_quality_score
from app.direction_outcomes import verified_merged_count
from app.direction_mode import mode_prompt_hint, normalized_recommended_mode
from app.knowledge_query import build_knowledge_index
from app.models import (
    Task,
    Project,
    TaskHistory,
    TaskStatus,
    MessageType,
    ProjectMessage,
    SystemMaturity,
)
from app.payload_schemas import pack_under_budget, validate_internal_payload
from app.problem_model import REJECTED_AGENT_EDITS_KEY as PM_REJECTED_EDITS_KEY
from app.problem_model import compute_model_size_signal, parse_problem_model
from app.running_update_merge import RUNNING_UPDATE_REJECTED_KIND
from app.source_ref_validation import (
    classify_existing_paths,
    ground_design_doc,
    resolve_branch_verification_context,
)
from app.strategic_trigger import (
    ARMING_STEERING,
    ORIGIN_OPERATOR,
    classify_operator_arming,
    strategic_refresh_excerpt,
    strategic_refresh_origin,
)
from app.task_capabilities import STRATEGIC_TASK_TYPES
import app.shared_state as _shared_state
from app.shared_state import (
    get_project_integration_branch,
    get_project_promotion_target,
    runtime_config as _runtime_config,
)
from app.time_utils import _utcnow, _coerce_naive_utc
from app.vision_merge import REJECTED_EDITS_KEY

logger = logging.getLogger(__name__)


def _build_parallelism_hint(
    active_count: int, max_concurrent_vms: int, max_active_directions: int
) -> str:
    """Advisory prompt hint nudging the strategist to keep enough INDEPENDENT
    direction lanes open to use the fleet.

    Each active direction is one parallel work-lane (research/plan dispatch fans
    out per-direction), so the active-direction count — not the VM cap — is the
    real ceiling on how much of the fleet works at once. The target is derived
    from existing config, never hardcoded: aim for the operator's own
    ``max_active_directions`` ceiling, clamped by ``max_concurrent_vms`` so a
    small fleet is never told to open lanes it cannot run. Returns "" once the
    lane count reaches that target. Quality-framed: it asks the agent to
    decompose the version goal into independent, non-overlapping workstreams —
    so the added lanes are *more parallelism, not more conflict* — rather than to
    hit a number.
    """
    target = max_active_directions
    if max_concurrent_vms:
        target = min(target, max_concurrent_vms)
    if active_count >= target:
        return ""
    cap_clause = (
        f" the fleet can run ~{max_concurrent_vms} VMs at once and"
        if max_concurrent_vms
        else ""
    )
    return (
        f"PARALLELISM: You have {active_count} active direction(s);{cap_clause} "
        f"each active direction is an independent work-lane, so most of that "
        f"capacity is idle right now. Decompose the version goal into as many "
        f"INDEPENDENT, non-overlapping workstreams as genuinely exist — each "
        f"touching different subsystems/files so their work lands without "
        f"colliding — and open a direction for each. Lean toward opening more "
        f"lanes than fewer. Keep them disjoint: if two directions would touch the "
        f"same code, mark them conflicts_with (to sequence) or merge them; retire "
        f"stale ones. Only stay narrow if there is genuinely no further "
        f"independent valuable work, or the rest depends on a load-bearing "
        f"direction landing first (room for up to {max_active_directions} active "
        f"directions)."
    )


# Keys kept in Project.design_doc for operator/UI consumption but never
# consumed by the agent. Stripping them before the payload goes out prevents
# unbounded state (e.g. playbook_archive) from bloating claim responses —
# see the silent pre-claim death investigation (task 595c235d).
_AGENT_UNUSED_DESIGN_DOC_KEYS = frozenset({"playbook_archive"})


def _agent_view_design_doc(doc: dict | None) -> dict:
    """Shallow copy of *doc* with agent-unused + internal (``_``-prefixed) keys
    dropped. The agent's design-doc handler only renders 200-char previews of
    each key, so operator-only state like ``playbook_archive`` has no
    functional purpose downstream and just inflates the payload."""
    if not doc:
        return {}
    return {
        k: v
        for k, v in doc.items()
        if k not in _AGENT_UNUSED_DESIGN_DOC_KEYS and not k.startswith("_")
    }


# Maximum length of the one-line per-entry summary carried in the design_doc
# index. Keeps each entry navigation-level — enough to recognize a section,
# never its full prose/specs — so the index stays bounded no matter how large
# the underlying design doc grows. The agent pulls full content on demand via
# ``POST /api/agent/design-doc`` (op:get).
_DESIGN_DOC_INDEX_SUMMARY_CHARS = 200
# Cap on backing module paths carried per systems/capabilities entry so a
# pathological entry with hundreds of modules cannot unbound the index. The
# declared paths are kept (capped) so the agent-side reconciliation over the
# cloned workspace remains possible as an optional second pass.
_DESIGN_DOC_INDEX_MODULES_KEEP = 16
# Non-section bookkeeping keys that live inside project.design_doc but are NOT
# architecture sections and so must NOT appear as index entries. ``proposed_changes``
# is the operator/agent reconciliation queue — it is shipped as its OWN top-level
# payload field (see _spawn_design_doc_update), so indexing it here would be
# redundant noise ('Other sections' entry with no summary).
_DESIGN_DOC_INDEX_NON_SECTION_KEYS = frozenset({"proposed_changes"})


def _design_doc_entry_summary(value: Any) -> str | None:
    """Derive a bounded, single-line summary for one design-doc section.

    Prefers an explicit ``title``/``summary`` field, else the first prose body
    (``description`` for systems, ``notes`` for capabilities, or any string).
    Collapses whitespace and truncates to :data:`_DESIGN_DOC_INDEX_SUMMARY_CHARS`
    so the index entry never carries full prose."""
    text: str | None = None
    if isinstance(value, str):
        text = value
    elif isinstance(value, dict):
        for field in ("title", "summary", "description", "notes", "purpose"):
            candidate = value.get(field)
            if isinstance(candidate, str) and candidate.strip():
                text = candidate
                break
    if not isinstance(text, str) or not text.strip():
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) > _DESIGN_DOC_INDEX_SUMMARY_CHARS:
        return collapsed[: _DESIGN_DOC_INDEX_SUMMARY_CHARS - 1].rstrip() + "…"
    return collapsed


def _design_doc_index(doc: dict | None) -> dict:
    """Build a compact, bounded navigation index over an already-grounded
    agent-view design doc.

    Replaces the full-content design_doc field in the strategic payload: instead
    of shipping every section's full prose/specs inline (unbounded as the doc
    grows), ships ONE entry per non-internal section carrying only navigation-
    and architecture-level metadata:

      * ``key``    — the section key (e.g. ``systems/auth``)
      * ``summary``— a single, whitespace-collapsed, char-bounded line (when the
        section has any prose to summarize)
      * for ``systems/*`` and ``capabilities/*`` additionally the reconciled
        ``status`` (presence — computed upstream by :func:`ground_design_doc`),
        the declared ``modules`` backing paths (capped), and any
        ``presence_warning`` the grounding pass stamped on.

    The full, server-side-reconciled content of any section remains available on
    demand via ``POST /api/agent/design-doc`` (op:get). The index size does NOT
    scale with section prose: every text field is bounded and module lists are
    capped, so the index stays small however large the design doc becomes.
    """
    if not doc:
        return {"sections": []}
    entries: list[dict] = []
    for key in sorted(k for k in doc if isinstance(k, str)):
        if (
            key in _AGENT_UNUSED_DESIGN_DOC_KEYS
            or key in _DESIGN_DOC_INDEX_NON_SECTION_KEYS
            or key.startswith("_")
        ):
            continue
        value = doc[key]
        entry: dict[str, Any] = {"key": key}
        summary = _design_doc_entry_summary(value)
        if summary is not None:
            entry["summary"] = summary
        if isinstance(value, dict) and (
            key.startswith("systems/") or key.startswith("capabilities/")
        ):
            status = value.get("status")
            if isinstance(status, str) and status.strip():
                entry["status"] = status
            modules = value.get("modules")
            if isinstance(modules, list):
                module_paths = [m for m in modules if isinstance(m, str) and m.strip()][
                    :_DESIGN_DOC_INDEX_MODULES_KEEP
                ]
                if module_paths:
                    entry["modules"] = module_paths
            warning = value.get("presence_warning")
            if isinstance(warning, str) and warning.strip():
                entry["presence_warning"] = warning
        entries.append(entry)
    return {"sections": entries}


def _summarize_design_doc_index(index: Any) -> Any:
    """Budget-pressure fallback for the design_doc index: drop the per-entry
    one-line summaries, keeping only the navigation skeleton (key + reconciled
    status + backing modules + presence_warning). The full content of any
    section — including its prose/summary — remains fetchable on demand via the
    design-doc op:get endpoint, so dropping summaries here is non-destructive.

    The index is already compact, so this almost never fires; it exists so the
    field degrades gracefully (skeleton) rather than vanishing wholesale under
    genuine budget exhaustion."""
    if not isinstance(index, dict):
        return index
    sections = index.get("sections")
    if not isinstance(sections, list):
        return index
    skeleton = [
        {k: v for k, v in entry.items() if k != "summary"}
        if isinstance(entry, dict)
        else entry
        for entry in sections
    ]
    return {**index, "sections": skeleton}


# Maximum history/past_moves entries to keep per active gap when shipping the
# problem model to the strategic agent. Active gaps accumulate move records and
# evidence over time; shipping the full lineage every cycle pushes the strategic
# payload past the 256 KB hard cap (see _MAX_PAYLOAD_BYTES rationale). Keep
# enough to reason about recent direction trajectory without dragging months
# of stale context.
_GAP_HISTORY_KEEP = 5
_GAP_PAST_MOVES_KEEP = 5
_GAP_EVIDENCE_KEEP = 8


def _compute_model_health(pm: dict | None) -> dict | None:
    """Fail-open wrapper around :func:`compute_model_size_signal`.

    Computes the soft model size/health signal from the DURABLE problem_model
    (honest pre-trim counts). Returns ``None`` when the durable blob fails strict
    schema validation (``parse_problem_model`` raises ``ValidationError`` on a
    malformed model) so a legacy/loose stored model never crashes strategic
    spawning — the signal is advisory, so omitting it on parse failure is the
    safe degradation. Callers conditionally include the key only when non-None.
    """
    if not pm:
        # Empty/absent durable model — report an honest 'healthy' zero signal.
        return compute_model_size_signal(parse_problem_model(None))
    try:
        return compute_model_size_signal(parse_problem_model(pm))
    except Exception:
        logger.warning(
            "[STRATEGIC] could not compute model_health (durable problem_model "
            "failed strict validation); omitting the soft signal this cycle",
            exc_info=True,
        )
        return None


def _agent_view_problem_model(pm: dict | None) -> dict:
    """Trim Project.problem_model for inclusion in strategic-agent payloads.

    Two sources of unbounded growth get capped here:

    1. ``archived_gaps`` — closed gaps retained for operator/UI reference.
       The strategic agent does not act on them; if it needs historical
       context it can pull a specific gap by id from the full record.
       This was 24 KB / 31 entries on the Blueprint project at the time
       this was added.
    2. Per-active-gap ``history`` / ``past_moves`` / ``evidence`` arrays
       grow with every cycle. Trim to the most recent N entries each so
       the per-gap footprint stays bounded.

    Together these two trims drop the problem_model from ~78 KB to ~50 KB
    on a mature project — enough headroom under the 256 KB payload cap
    for the rest of the strategic context to keep working.
    """
    if not pm:
        return {}
    trimmed = dict(pm)
    trimmed.pop("archived_gaps", None)
    # The rejected-agent-edits ledger is surfaced to the agent through the
    # dedicated, consolidated ``operator_overrides`` payload key (see
    # :func:`_build_operator_overrides_summary`) — strip it here so the raw,
    # unbounded ledger is not double-shipped inside problem_model.
    trimmed.pop("rejected_agent_edits", None)
    gaps = trimmed.get("gaps") or []
    if gaps:
        new_gaps = []
        gap_memory_trimmed: list[dict] = []
        for g in gaps:
            if not isinstance(g, dict):
                new_gaps.append(g)
                continue
            g2 = dict(g)
            shortened: dict[str, dict] = {}
            for key, keep in (
                ("history", _GAP_HISTORY_KEEP),
                ("past_moves", _GAP_PAST_MOVES_KEEP),
                ("evidence", _GAP_EVIDENCE_KEEP),
            ):
                arr = g2.get(key)
                if isinstance(arr, list) and len(arr) > keep:
                    shortened[key] = {"shown": keep, "total": len(arr)}
                    g2[key] = arr[-keep:]
            if shortened:
                gap_memory_trimmed.append(
                    {"gap_id": g2.get("id"), "fields": shortened}
                )
            new_gaps.append(g2)
        trimmed["gaps"] = new_gaps
        # Surface the pre-trim so the strategic agent can tell a gap's
        # append-only memory was shortened to a recent window for payload
        # size. This trim runs BEFORE the byte-budget packer, so it would
        # otherwise never appear in the payload's packer metadata and the
        # agent would mistake the shortened view for the gap's full arc.
        # The marker rides in the agent-facing problem_model (the durable
        # project.problem_model is never mutated — this operates on a copy)
        # and steers the agent to ./fetch full gap history before deciding
        # whether to push, refine, or retire a long-lived gap.
        if gap_memory_trimmed:
            trimmed["_gap_memory_trimmed"] = gap_memory_trimmed
    return trimmed


# Guaranteed-small floor for the agent-view problem_model when it is packed
# under genuine byte-budget pressure. The byte-budget packer registers
# ``_agent_view_problem_model_packed`` as problem_model's summarize_fn at
# "high" priority — NOT "critical" — so a count-trimmed-but-still-oversized
# model would otherwise be DROPPED entirely and the strategist would run a
# cycle with NO problem_model at all (recorded only in packer metadata).
# Active gaps have no count cap (they are live state), so the count-based
# trims above cannot guarantee the result fits. The floor makes the summarize
# path budget-aware: it progressively strips the lowest-value per-gap detail,
# then drops the lowest-value gaps, until the serialized model is under a small
# floor that comfortably fits the remaining budget — so the model is ALWAYS
# delivered. What was shortened is marked in ``_agent_view_floor_applied`` so
# the agent can ``./fetch`` fuller detail (and so an unfittable model is a
# LOUD, visible signal rather than a quiet drop).
_AGENT_VIEW_FLOOR_BYTES = 32 * 1024
# Headroom reserved below the floor for the ``_agent_view_floor_applied`` marker
# (added AFTER the size checks), so the final serialized model — marker included
# — still lands under the floor.
_FLOOR_MARKER_RESERVE = 1024
# Never shrink below this many gaps — the strategist needs at least the
# highest-value live gaps to reason at all.
_FLOOR_MIN_GAPS = 12
# Per-gap detail stripped at each escalating floor level, lowest-value first
# (append-only memory and free-text evidence before the human-readable body).
_FLOOR_SHRINK_LEVELS: tuple[tuple[str, ...], ...] = (
    ("history", "past_moves", "active_moves", "evidence"),
    ("description",),
)
_GAP_PRIORITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}

# When gap-shrinking alone cannot reach the floor (a model whose bulk lives in
# capabilities/friction rather than gaps), these sections are reduced to a
# minimal name+status skeleton, lowest-value first. The dropped detail (notes,
# backing paths, descriptions, seen-in, mitigations-tried, source provenance)
# is recoverable via the strategic problem_model fetch endpoint, so the shrink
# is non-destructive to the durable model.
_CAP_SKELETON_KEEP = ("name", "status")
_FRICTION_SKELETON_KEEP = ("id", "name", "status")
# Higher rank keeps its detail longer (reduced last); lowest-value first.
_CAP_KEEP_RANK = {"working": 2, "partial": 1, "planned": 0}
_FRICTION_KEEP_RANK = {"active": 2, "mitigated": 1, "resolved": 0}


def _serialized_bytes(value: Any) -> int:
    """Serialized JSON byte length using the project-standard ``default=str``
    (matches the packer's own ``_serialized_size`` measurement)."""
    return len(json.dumps(value, default=str).encode())


def _gap_floor_keep_rank(g: dict) -> tuple:
    """Sort key for floor eviction — HIGHER sorts first (kept). Active beats
    closed/abandoned, higher priority beats lower, newer-opened beats older."""
    closed = bool(g.get("closed")) or g.get("status") in ("closed", "abandoned")
    return (
        0 if closed else 1,
        _GAP_PRIORITY_RANK.get(g.get("priority"), 1),
        g.get("opened") or "",
    )


def _skeletonize_caps_friction(trimmed: dict, target: int) -> dict:
    """Reduce ``capabilities`` and ``friction`` to a minimal name+status
    skeleton, lowest-value first, until *trimmed* serializes under *target*
    (or both sections are exhausted). The full detail (notes, backing paths,
    descriptions, seen-in, mitigations-tried, source provenance) is recoverable
    via the strategic problem_model fetch endpoint, so this is non-destructive.

    Returns the count of entries reduced per section. Copies the section lists
    and entries before mutating so the durable model (which *trimmed* only
    shallow-copies) is never touched."""
    counts = {"capabilities": 0, "friction": 0}
    worklist: list[tuple[int, str, int]] = []
    sections: dict[str, tuple[list, tuple[str, ...]]] = {}
    for section_key, keep_fields, rank_map in (
        ("capabilities", _CAP_SKELETON_KEEP, _CAP_KEEP_RANK),
        ("friction", _FRICTION_SKELETON_KEEP, _FRICTION_KEEP_RANK),
    ):
        items = trimmed.get(section_key)
        if not isinstance(items, list) or not items:
            continue
        copied = [dict(it) if isinstance(it, dict) else it for it in items]
        trimmed[section_key] = copied
        sections[section_key] = (copied, keep_fields)
        for idx, item in enumerate(copied):
            if isinstance(item, dict):
                worklist.append((rank_map.get(item.get("status"), 1), section_key, idx))
    worklist.sort(key=lambda t: t[0])
    for _rank, section_key, idx in worklist:
        if _serialized_bytes(trimmed) <= target:
            break
        copied, keep_fields = sections[section_key]
        item = copied[idx]
        skeleton = {k: item[k] for k in keep_fields if k in item}
        if skeleton != item:
            copied[idx] = skeleton
            counts[section_key] += 1
    return counts


def _shrink_pm_to_floor(trimmed: dict) -> dict:
    """Progressively shrink *trimmed* (an already ``_agent_view_problem_model``-
    trimmed model) until it serializes under :data:`_AGENT_VIEW_FLOOR_BYTES`,
    so it always fits the packer's remaining budget and is never silently
    dropped. Mutates and returns *trimmed* (caller passes a fresh copy)."""
    if _serialized_bytes(trimmed) <= _AGENT_VIEW_FLOOR_BYTES:
        return trimmed
    target = _AGENT_VIEW_FLOOR_BYTES - _FLOOR_MARKER_RESERVE
    stripped: list[str] = []
    gaps = trimmed.get("gaps")
    if isinstance(gaps, list) and gaps:
        for level in _FLOOR_SHRINK_LEVELS:
            gaps = [
                {k: v for k, v in g.items() if k not in level}
                if isinstance(g, dict)
                else g
                for g in gaps
            ]
            trimmed["gaps"] = gaps
            stripped.extend(level)
            if _serialized_bytes(trimmed) <= target:
                trimmed["_agent_view_floor_applied"] = {
                    "floor_bytes": _AGENT_VIEW_FLOOR_BYTES,
                    "stripped_gap_fields": stripped,
                    "dropped_gaps": 0,
                }
                return trimmed
    # Gap-shrinking alone did not reach the floor (a capability/friction-heavy
    # model, or a pathological active-gap count). Reduce capabilities and
    # friction to a name+status skeleton, lowest-value first, before resorting
    # to dropping live gaps — their detail is the recoverable bulk.
    reduced = _skeletonize_caps_friction(trimmed, target)
    if _serialized_bytes(trimmed) <= target:
        trimmed["_agent_view_floor_applied"] = {
            "floor_bytes": _AGENT_VIEW_FLOOR_BYTES,
            "stripped_gap_fields": stripped,
            "dropped_gaps": 0,
            "summarized_capabilities": reduced["capabilities"],
            "summarized_friction": reduced["friction"],
        }
        return trimmed
    # Still over floor. Drop the lowest-value gaps (keeping the highest-value),
    # summarizing the rest as a count, so the model is delivered not dropped.
    gaps = trimmed.get("gaps")
    if not isinstance(gaps, list) or not gaps:
        # No gaps to drop (a caps/friction-only model already skeletonized).
        still_over = _serialized_bytes(trimmed) > _AGENT_VIEW_FLOOR_BYTES
        if still_over:
            logger.warning(
                "[STRATEGIC] agent-view problem_model still exceeds the %d-byte "
                "floor after skeletonizing capabilities/friction — shipping anyway "
                "with a loud marker so the strategist is never silently deprived",
                _AGENT_VIEW_FLOOR_BYTES,
            )
        trimmed["_agent_view_floor_applied"] = {
            "floor_bytes": _AGENT_VIEW_FLOOR_BYTES,
            "stripped_gap_fields": stripped,
            "dropped_gaps": 0,
            "summarized_capabilities": reduced["capabilities"],
            "summarized_friction": reduced["friction"],
            "still_over_floor": still_over,
        }
        return trimmed
    base = dict(trimmed)
    base["gaps"] = []
    running = _serialized_bytes(base)
    kept: list = []
    dropped = 0
    for g in sorted(
        (g for g in gaps if isinstance(g, dict)),
        key=_gap_floor_keep_rank,
        reverse=True,
    ):
        gsize = _serialized_bytes(g) + 1  # +1 ~ list separator
        if running + gsize <= target or len(kept) < _FLOOR_MIN_GAPS:
            kept.append(g)
            running += gsize
        else:
            dropped += 1
    trimmed["gaps"] = kept
    still_over = _serialized_bytes(trimmed) > _AGENT_VIEW_FLOOR_BYTES
    if still_over:
        logger.warning(
            "[STRATEGIC] agent-view problem_model still exceeds the %d-byte floor "
            "after shrinking to %d gaps — shipping anyway with a loud marker so "
            "the strategist is never silently deprived of its model",
            _AGENT_VIEW_FLOOR_BYTES,
            len(kept),
        )
    trimmed["_agent_view_floor_applied"] = {
        "floor_bytes": _AGENT_VIEW_FLOOR_BYTES,
        "stripped_gap_fields": stripped,
        "dropped_gaps": dropped,
        "summarized_capabilities": reduced["capabilities"],
        "summarized_friction": reduced["friction"],
        "still_over_floor": still_over,
    }
    return trimmed


def _agent_view_problem_model_packed(pm: dict | None) -> dict:
    """Budget-pressure ``summarize`` callback for the ``problem_model`` payload
    key. Runs ONLY when the (already agent-view-trimmed) model still exceeds the
    packer's remaining byte budget: it re-applies the agent-view trim, then
    shrinks to a guaranteed-small floor so the model ALWAYS fits and is never
    silently dropped from the strategist's cycle payload."""
    return _shrink_pm_to_floor(_agent_view_problem_model(pm))


def _agent_view_problem_model_floor(pm: dict | None) -> dict:
    """``floor`` callback for the ``problem_model`` payload key.

    Reserves and returns the WHOLE agent-view model handed to it, so the packer
    reserves the full model's bytes off the top of the budget. Every other
    recoverable sibling (design_doc index, playbook indices, outcome_evaluation,
    conversation_thread, directions_summary overflow, …) must therefore degrade
    or drop BEFORE the problem_model is ever reduced. Under normal cycle sizes
    the whole model ships intact via the packer's direct fit-path and the
    skeletonizing ``summarize`` callback never fires.

    Only genuinely exceptional overflow degrades the model: when the full model
    cannot fit at all, this floor's reservation alone exceeds the budget, the
    packer drops the floor guarantee wholesale, and the ``summarize`` callback
    (:func:`_agent_view_problem_model_packed` → :func:`_shrink_pm_to_floor`)
    takes over — stamping the loud ``_agent_view_floor_applied`` marker, shipping
    a bounded floored skeleton, and recording the trim in ``_packer_meta`` so the
    agent's ``problemModelRecovery`` resolves to ``'fetch'``. The degraded path
    is the visibly exceptional case, never the normal operating mode.

    Deliberately INDEPENDENT of the summarize callback and defensively coded: a
    failure in the summarize path never abandons this reservation, and the floor
    itself never crashes the packer (which already wraps it in try/except). It
    only ever returns the value handed to it — already an
    ``_agent_view_problem_model``-trimmed copy whose durable source is never
    mutated — or a minimal empty skeleton for a missing/garbage model.
    """
    if not pm or not isinstance(pm, dict):
        return {"gaps": [], "capabilities": [], "friction": []}
    # Reserve the WHOLE agent-view model. The value handed to this callback is
    # the same trimmed agent-view dict already placed in the payload, so the
    # reservation equals the full shipped value's byte size — no other section
    # can claim those bytes ahead of the problem_model.
    return pm


# Maximum operator-override entries surfaced to the next strategic cycle, and
# the per-value character cap. The override summary closes the operator-wins
# reconciliation loop: the reconciliation writers record each overridden agent
# value into rolling, capped ledgers (vision ``_rejected_agent_edits`` and
# problem_model ``rejected_agent_edits``) "so next cycle's agent can see what
# was overridden and reconcile" — this helper reads those ledgers (read-only;
# never clears or mutates them) into one bounded list. Kept small so it can
# never bloat the prompt; the payload packer drops it whole under budget.
_OPERATOR_OVERRIDES_MAX = 8
_OVERRIDE_VALUE_MAXLEN = 240


def _truncate_override_value(value: Any) -> Any:
    """Char-bound a single ledger value so the override summary stays compact.

    Strings are truncated with an ellipsis marker; non-strings are coerced to
    their ``repr`` and truncated the same way so a stray dict/list cannot blow
    the per-entry budget.
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else repr(value)
    if len(text) > _OVERRIDE_VALUE_MAXLEN:
        return text[:_OVERRIDE_VALUE_MAXLEN] + "…"
    return text


def _build_operator_overrides_summary(project: Project) -> list[dict]:
    """Consolidate the operator-wins rejected-edit ledgers into one bounded list.

    Reads both reconciliation ledgers — ``project.vision[_rejected_agent_edits]``
    (vision scalar conflicts + ``running_update_superseded`` entries) and
    ``project.problem_model[rejected_agent_edits]`` (operator-owned intent /
    context / constraints conflicts) — and normalizes each entry to a compact,
    char-bounded shape::

        {document, field, kind, agent_value, operator_value, at}

    ``document`` is ``running_update`` | ``vision`` | ``intent`` so the agent
    can tell which document it overrode. Running-update entries have no operator
    value (the operator's winning value is the live running_update body itself),
    so ``operator_value`` is ``None`` there. Sorted newest-first and capped at
    :data:`_OPERATOR_OVERRIDES_MAX`. Read-only: neither ledger is cleared or
    mutated. Returns ``[]`` when both ledgers are empty.
    """
    entries: list[dict] = []

    vision = project.vision if isinstance(project.vision, dict) else {}
    for e in vision.get(REJECTED_EDITS_KEY) or []:
        if not isinstance(e, dict):
            continue
        field = e.get("field") or ""
        if field == "running_update":
            document = "running_update"
        else:
            document = "vision"
        entries.append(
            {
                "document": document,
                "field": field,
                "kind": e.get("kind"),
                "agent_value": _truncate_override_value(e.get("agent_value")),
                # Vision conflicts log the operator's winning value as
                # ``user_value``; running_update entries carry none.
                "operator_value": _truncate_override_value(e.get("user_value")),
                "at": e.get("at"),
            }
        )

    pm = project.problem_model if isinstance(project.problem_model, dict) else {}
    for e in pm.get(PM_REJECTED_EDITS_KEY) or []:
        if not isinstance(e, dict):
            continue
        field = e.get("field") or ""
        document = "intent" if field.startswith("intent") else "problem_model"
        entries.append(
            {
                "document": document,
                "field": field,
                "kind": e.get("kind"),
                "agent_value": _truncate_override_value(e.get("agent_value")),
                "operator_value": _truncate_override_value(e.get("operator_value")),
                "at": e.get("at"),
            }
        )

    # Newest-first by ISO timestamp; missing/empty ``at`` sorts last.
    entries.sort(key=lambda x: x.get("at") or "", reverse=True)
    return entries[:_OPERATOR_OVERRIDES_MAX]


async def _verify_capability_backing_paths(
    pm: dict | None,
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Annotate each capability in *pm* with advisory backing-path verification.

    Mirrors the design-doc ``systems/<id>.modules`` verification in
    ``autonomous_research.py``: for every capability that declares
    ``backing_paths``, classify those paths against the integration-branch
    file tree (prefer the project's real GitHub repo/branch when available,
    else local git HEAD via :func:`classify_existing_paths`) and attach a
    per-capability ``verification`` block::

        {"present": [...], "missing": [...], "effective_status": "..."}

    ``effective_status`` framing so downstream prompts reason about what the
    code actually shows rather than trusting the stored status verbatim:
      - ``backing_paths`` empty → ``"unverified"`` (no linkage to check)
      - all paths present → the capability's own stored status (verified)
      - mixed presence → ``"partial"``
      - all paths missing → ``"absent"`` (claimed but no backing code exists)

    Operates on a copy: the input *pm* (and therefore the durable
    ``project.problem_model``) is never mutated. *pm* is expected to already
    be ``_agent_view_problem_model``-trimmed; this helper only adds keys.
    """
    if not pm:
        return {}
    annotated = dict(pm)
    caps = annotated.get("capabilities") or []
    if not isinstance(caps, list):
        return annotated
    new_caps: list = []
    for cap in caps:
        if not isinstance(cap, dict):
            new_caps.append(cap)
            continue
        cap2 = dict(cap)
        paths = [p for p in (cap2.get("backing_paths") or []) if isinstance(p, str)]
        if not paths:
            cap2["verification"] = {
                "present": [],
                "missing": [],
                "effective_status": "unverified",
            }
            new_caps.append(cap2)
            continue
        present, missing = await classify_existing_paths(
            paths,
            token=token,
            owner=owner,
            repo=repo,
            branch=branch,
            project_id=project_id,
        )
        if not missing:
            effective_status = str(cap2.get("status", "")) or "unverified"
        elif present:
            effective_status = "partial"
        else:
            # Every declared backing path is absent from the integration
            # branch: the capability is claimed but its code does not exist.
            effective_status = "absent"
        cap2["verification"] = {
            "present": present,
            "missing": missing,
            "effective_status": effective_status,
        }
        new_caps.append(cap2)
    annotated["capabilities"] = new_caps
    return annotated


# Per-system maturity rows can be heavy if `top_issues` and `evidence` carry
# bulky payloads — cap the count per project to keep the strategic payload
# under the 256KB ceiling. SystemMaturity is per (project_id, system_id), so
# the natural ceiling is the count of distinct system_ids on the project.
_SYSTEM_MATURITY_PAYLOAD_CAP = 25

# Per-row issue and evidence caps so a single noisy system can't dominate the
# payload. Operators can still see the full record via the API.
_SYSTEM_MATURITY_TOP_ISSUES_CAP = 5


async def _build_system_maturity_payload(
    project: Project,
    session: AsyncSession,
) -> list[dict]:
    """Fetch SystemMaturity rows for *project* and shape them for the agent.

    Returns at most _SYSTEM_MATURITY_PAYLOAD_CAP entries, ordered by
    last_audited_at descending (oldest audits get trimmed first when over
    the cap, so the agent always sees the freshest snapshots). Returns an
    empty list when no rows exist — strategic_spawning's payload includes
    the field only when non-empty so cold-start projects don't carry an
    empty field through the cap.
    """
    result = await session.execute(
        select(SystemMaturity)
        .where(SystemMaturity.project_id == project.id)
        .order_by(SystemMaturity.last_audited_at.desc())
        .limit(_SYSTEM_MATURITY_PAYLOAD_CAP)
    )
    rows = result.scalars().all()
    out: list[dict] = []
    for row in rows:
        top_issues = (row.top_issues or [])[:_SYSTEM_MATURITY_TOP_ISSUES_CAP]
        out.append(
            {
                "system_id": row.system_id,
                "score": row.score,
                "components": dict(row.components or {}),
                "recommended_mode": row.recommended_mode,
                "last_audited_at": (
                    row.last_audited_at.isoformat() if row.last_audited_at else None
                ),
                "top_issues": top_issues,
            }
        )
    return out


_EFFICIENCY_SNAPSHOT_HISTORY_TAIL = 3


def _summarize_efficiency_snapshot(snapshot: dict) -> dict:
    """Trim efficiency_snapshot for inclusion in strategic-agent payloads.

    Strips full history arrays down to a 3-point tail per axis so the agent
    can read a trend without dragging months of weekly history through the
    payload budget.
    """
    if not snapshot:
        return {}
    axes = snapshot.get("axes") or {}
    trimmed_axes: dict = {}
    for axis, data in axes.items():
        if not isinstance(data, dict):
            trimmed_axes[axis] = data
            continue
        history = data.get("history") or []
        trimmed_axes[axis] = {
            "current": data.get("current"),
            "baseline": data.get("baseline"),
            "drift_pct": data.get("drift_pct"),
            "history_tail": history[-_EFFICIENCY_SNAPSHOT_HISTORY_TAIL:],
        }
    return {
        "axes": trimmed_axes,
        "baseline_captured_at": snapshot.get("baseline_captured_at"),
        "last_snapshot_at": snapshot.get("last_snapshot_at"),
    }


def _format_fleet_utilization_axis(fleet: dict | None) -> str | None:
    """Render a fleet-utilization snapshot as a compact axis ``current`` string.

    Returns None when capacity is unknown (no enabled, capped accounts) so the
    axis is omitted rather than shipping a meaningless row. The cooled-account
    breakdown is appended only when accounts are actually cooled, keeping the
    common (healthy) case terse — the cause of low utilization (idle headroom
    vs rate-limit cooldown) is what would otherwise mislead the strategist into
    manufacturing work.
    """
    if not fleet:
        return None
    cap = fleet.get("nominal_capacity")
    if not cap:
        return None
    active = fleet.get("active_now", 0)
    util_now = fleet.get("util_now")
    util_avg = fleet.get("util_avg")
    window = fleet.get("window_hours", 24)
    now_pct = f"{round(util_now * 100)}%" if util_now is not None else "n/a"
    line = f"now {active}/{cap} ({now_pct})"
    if util_avg is not None:
        line += f", {window}h-avg {round(util_avg * 100)}%"
    cooled = fleet.get("cooled_accounts") or 0
    usable = fleet.get("usable_capacity")
    if cooled and usable is not None and usable != cap:
        line += f" — {cooled} acct cooled, {usable} usable"
    return line


_VERSION_PHASE_RECENT_VERSIONS_CAP = 3


def _summarize_version_phase(block: dict) -> dict:
    """Trim version_phase for inclusion under budget pressure.

    Drops ``thresholds`` from ``promotion_readiness`` (verbose config), trims
    ``confidence`` to {mean, stdev, stable}, and caps ``recent_versions`` to
    the 3 most recent entries so the agent sees the trend without dragging full
    verdict history through the payload.
    """
    if not block:
        return {}
    out = dict(block)
    pr = block.get("promotion_readiness")
    if isinstance(pr, dict):
        conf = pr.get("confidence") or {}
        out["promotion_readiness"] = {
            k: v
            for k, v in pr.items()
            if k not in {"thresholds", "blocker_counts", "confidence"}
        }
        if isinstance(conf, dict):
            out["promotion_readiness"]["confidence"] = {
                "mean": conf.get("mean"),
                "stdev": conf.get("stdev"),
                "stable": conf.get("stable"),
            }
    rv = block.get("recent_versions")
    if isinstance(rv, list):
        out["recent_versions"] = rv[:_VERSION_PHASE_RECENT_VERSIONS_CAP]
    return out


def _summarize_drift_signals(drift: dict) -> dict:
    """Return a payload-friendly subset of drift_detection.compute_direction_drift.

    Strips per-direction signals when the project has many directions and
    keeps only flagged or non-neutral entries, so the strategic payload
    doesn't carry zero-information rows for every direction.
    """
    if not drift:
        return {}
    by_direction = drift.get("by_direction") or {}
    by_system = drift.get("by_system") or {}
    # Keep flagged directions in full; for others summarize only if the
    # consistency dropped below 0.8.
    interesting_directions = {
        did: sig
        for did, sig in by_direction.items()
        if (
            sig.get("over_claim_flag")
            or sig.get("stalled_despite_churn_flag")
            or (
                sig.get("claim_shipped_consistency", 1.0) < 0.8
                and sig.get("claimed_changes_count", 0) > 0
            )
        )
    }
    return {
        "by_direction": interesting_directions,
        "by_system": by_system,
        "generated_at": drift.get("generated_at"),
        "window_days": drift.get("window_days"),
    }


# How many stored snapshots to look back when computing consecutive_flagged_cycles.
_DRIFT_TREND_LOOKBACK = 5


async def _enrich_drift_with_trend(
    session: AsyncSession,
    project_id: uuid.UUID,
    drift_summary: dict,
) -> dict:
    """Add consecutive_flagged_cycles to each direction already in drift_summary.

    For each interesting direction, reads the last _DRIFT_TREND_LOOKBACK stored
    snapshots (newest first) and counts how many consecutive leading entries had
    at least one flag set. This lets the strategic agent distinguish a one-off
    spike from a sustained breakdown without reading the full history.

    Only enriches directions already filtered into drift_summary by
    _summarize_drift_signals, so the extra queries are bounded by the
    flagged-direction count (typically 0-3 per project).
    """
    from app.drift_detection import last_n_drift_snapshots

    by_direction = (drift_summary or {}).get("by_direction") or {}
    if not by_direction:
        return drift_summary
    enriched_directions = {}
    for direction_id, sig in by_direction.items():
        snapshots = await last_n_drift_snapshots(
            session, project_id, direction_id, n=_DRIFT_TREND_LOOKBACK
        )
        consecutive = 0
        for snap in snapshots:
            if snap.over_claim_flag or snap.stalled_despite_churn_flag:
                consecutive += 1
            else:
                break
        enriched_sig = dict(sig)
        if consecutive > 0:
            enriched_sig["consecutive_flagged_cycles"] = consecutive
        enriched_directions[direction_id] = enriched_sig
    return {**drift_summary, "by_direction": enriched_directions}


# Per-payload caps used by the strategic-spawn schema's summarize_fns. These are
# what the agent sees by default; for full content the agent uses ./fetch.
_DIRECTIONS_SUMMARY_KEEP = 12
_OUTCOME_DIRECTION_OUTCOMES_KEEP = 10
_CONVERSATION_THREAD_KEEP_WHEN_TRIMMED = 8


def _direction_activity_score(stat: dict) -> int:
    """Sort key for choosing which direction stats stay in the strategic payload.

    Higher = more activity. Combines work shipped (merged), work pending (failed),
    and recency (cycles). High-priority directions get a small boost so a quiet
    high-priority direction doesn't get evicted by a chatty low-priority one.
    """
    if not isinstance(stat, dict):
        return 0
    merged = int(stat.get("prs_merged") or 0)
    failed = int(stat.get("tasks_failed") or 0)
    cycles = int(stat.get("total_cycles") or 0)
    priority = stat.get("priority")
    priority_bonus = {"high": 5, "medium": 0, "low": -2}.get(priority, 0)
    proposed_bonus = 3 if stat.get("status") == "proposed" else 0
    return merged * 3 + failed + cycles + priority_bonus + proposed_bonus


def _summarize_directions(stats: list) -> list:
    """Keep the top-N direction_summary entries by activity score.

    Dropped directions are still discoverable by id via the agent's
    ``./fetch direction <id>`` helper, so the agent can pull the full
    record when it needs to reason about a quiet direction.
    """
    if not isinstance(stats, list) or len(stats) <= _DIRECTIONS_SUMMARY_KEEP:
        return stats
    sorted_stats = sorted(stats, key=_direction_activity_score, reverse=True)
    return sorted_stats[:_DIRECTIONS_SUMMARY_KEEP]


# Cap on staged-direction entries surfaced to the strategist. Staged directions
# are inert next-version work; the strategist needs to see what is already queued
# so it does not re-propose the same work, but the list must stay bounded under
# the payload budget.
_STAGED_DIRECTIONS_KEEP = 12


def _build_staged_directions_summary(project: Project) -> list[dict]:
    """Bounded list of the project's currently-staged directions.

    A *staged* direction (``status == "staged"``) is inert work the strategist
    or an operator marked to begin only after the version that was open when it
    was staged has promoted — the per-direction ``staged_for_version`` marker
    records that version number. Staged directions are deliberately EXCLUDED from
    ``directions_summary`` (which ships only active/paused/proposed), so without
    this block the strategist has no view of what is already queued for the next
    version and would re-propose the same work.

    Each entry is ``{id, text, priority, staged_for_version, staged_at,
    staged_age_days}``. ``staged_at`` (the staging timestamp) and the derived
    ``staged_age_days`` let the strategist see HOW LONG each item has waited so it
    can self-correct stale staged work (retarget / cancel) instead of leaving it
    to rot — the same stall the reaper raises an operator alert for. Both are
    additive: ``staged_at`` is ``None`` and ``staged_age_days`` is ``None`` when
    the timestamp is absent or unparseable, keeping the schema backward-compatible.
    Returns ``[]`` when no directions are staged so the caller can omit the field
    on the common (nothing-staged) path.
    """
    staged = [
        d
        for d in (project.directions or [])
        if isinstance(d, dict) and d.get("status") == "staged"
    ]
    now = _utcnow()
    out: list[dict] = []
    for d in staged[:_STAGED_DIRECTIONS_KEEP]:
        staged_at_raw = d.get("staged_at")
        staged_age_days: int | None = None
        if staged_at_raw:
            try:
                staged_at = _coerce_naive_utc(
                    datetime.fromisoformat(str(staged_at_raw))
                )
                staged_age_days = max(0, (now - staged_at).days)
            except (TypeError, ValueError):
                staged_age_days = None
        out.append(
            {
                "id": d.get("id"),
                "text": d.get("text"),
                "priority": d.get("priority"),
                "staged_for_version": d.get("staged_for_version"),
                "staged_at": staged_at_raw,
                "staged_age_days": staged_age_days,
            }
        )
    return out


def _summarize_outcome_evaluation(oe: dict) -> dict:
    """Trim an outcome_evaluation payload to the strategic agent's working set.

    The biggest growth driver is ``direction_outcomes`` (one entry per active
    direction, each carrying scope_performance, type_distribution, lifetime
    aggregates, etc). Cap to top-N by activity. Top-level summaries
    (``overall_summary``, ``systems``, trend) are kept in full — they're
    aggregates that don't grow with project size.
    """
    if not isinstance(oe, dict):
        return oe
    trimmed = dict(oe)
    direction_outcomes = trimmed.get("direction_outcomes")
    if (
        isinstance(direction_outcomes, list)
        and len(direction_outcomes) > _OUTCOME_DIRECTION_OUTCOMES_KEEP
    ):
        sorted_outcomes = sorted(
            direction_outcomes,
            key=lambda d: (
                int((d or {}).get("merged") or 0) * 3
                + int((d or {}).get("failed") or 0)
                + int((d or {}).get("cycles") or 0)
            ),
            reverse=True,
        )
        trimmed["direction_outcomes"] = sorted_outcomes[
            :_OUTCOME_DIRECTION_OUTCOMES_KEEP
        ]
    return trimmed


# Per-list cap for the cycle-start reconciliation block. The block is meant to
# be a conflict-focused starting-state digest, NOT a restatement of the full
# problem model or direction list, so each conflict list is capped and the true
# total carried in ``counts`` (the renderer shows "+N more"). Twelve is enough
# to surface the conflicts that matter without ever bloating the payload.
_RECON_LIST_CAP = 12
# Prompt-facing sample cap for per-direction recent merges. Keep this bounded so
# the recent-work section stays compact; exact verdict counts are carried
# separately for reconciliation.
_RECENT_MERGED_TITLES_CAP = 10
# Char cap for any direction/gap/capability label echoed into the block.
_RECON_LABEL_MAXLEN = 120


def _build_cycle_start_reconciliation(
    direction_stats: list[dict],
    problem_model: dict | None,
    active_by_direction: dict[str, int] | None,
    per_direction_delta: list[dict] | None = None,
) -> dict | None:
    """Compute one authoritative cycle-start reconciliation of the strategic surfaces.

    The strategic cycle reasons over four surfaces that can silently disagree:
    problem-model capabilities (agent-asserted), grounded design-doc/file-tree
    verification, active directions (with their durable ``gap_id`` linkage and
    deferral metadata), and each direction's research pipeline. Rather than make
    the agent hand-reconcile these every cycle, this computes the *conflicts*
    between them up front:

      * ``capability_contradictions`` — capabilities whose stored ``status``
        disagrees with the grounded file-tree check (``verification``): a
        ``working`` capability whose backing paths are all missing (``absent``)
        or only partly present (``partial``). The grounding is advisory (see
        :func:`_verify_capability_backing_paths`); a contradiction means the
        stored status is not backed by code that currently exists.
      * ``orphaned_gaps`` — open (``active`` or ``blocked``) gaps with no active
        direction pointing back via ``gap_id`` AND no intentional
        :class:`~app.problem_model.GapDeferral`. Each entry carries the gap's
        ``status`` so a drifted active gap is distinguishable from an orphaned
        blocked one.
        A gap carrying a ``deferral`` is *deliberately* unstaffed and is NOT an
        orphan — it is excluded and counted separately so the agent can tell a
        drifted gap from a parked one.
      * ``ungrounded_directions`` — ``active`` directions whose ``gap_id`` no
        longer resolves to an active gap (closed/archived/never-existed), or that
        carry no gap linkage at all. These advance no tracked gap.
      * ``direction_pipeline`` — per active direction, whether its research is
        ``research_waiting_on_action`` (findings surfaced but no task in flight),
        ``tasks_in_progress`` (work already moving), or ``idle``.
      * ``merge_accounting`` — per active direction, where the merge BADGE
        (which asserts a PR shipped) disagrees with ancestry verification over
        the same recent merged-title window used for recent-work. Built from the
        tri-state ``merge_verdict`` already stamped on each
        ``per_direction_delta[*].recent_merged_titles`` entry, plus the exact
        verdict counts/sample emitted alongside that visible list (no second
        GitHub/ancestry pass): ``verified_landed`` (on the integration branch),
        ``verified_not_landed`` (badge-asserted but ancestry says NOT reachable —
        the real accounting conflict), and ``unverifiable`` (ancestry could not
        be confirmed — reported as unknown, NEVER manufactured into a
        contradiction). Only directions with at least one ``verified_not_landed``
        title are surfaced as conflicts; the landed/unverifiable tallies ride in
        ``counts`` so nothing is silently truncated.

    Returns ``None`` when there is nothing to reconcile (no active directions and
    no conflicts), so the caller omits the field for cold-start projects. Pure
    and side-effect free — operates only on the data handed in — so the backend
    computation is directly unit-testable.
    """
    pm = problem_model if isinstance(problem_model, dict) else {}
    active_by_direction = active_by_direction or {}

    def _label(text: Any) -> str:
        return str(text or "")[:_RECON_LABEL_MAXLEN]

    def _count_field(container: dict[str, Any], key: str) -> int | None:
        value = container.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return max(0, value)

    # Active directions and the set of gap_ids they reference (forward linkage).
    active_dirs = [
        d for d in direction_stats if isinstance(d, dict) and d.get("status") == "active"
    ]
    referenced_gap_ids: set[str] = {
        str(d["gap_id"]) for d in active_dirs if d.get("gap_id")
    }

    gaps = pm.get("gaps") if isinstance(pm.get("gaps"), list) else []
    # Active gaps only resolve here — closed/abandoned gaps move to
    # archived_gaps (stripped from the agent view), so an unresolved gap_id is a
    # genuine dangling reference.
    active_gap_ids: set[str] = {
        str(g.get("id"))
        for g in gaps
        if isinstance(g, dict) and g.get("id") and g.get("status") == "active"
    }

    # 1. Capability ↔ grounded-verification contradictions.
    capability_contradictions: list[dict] = []
    for cap in pm.get("capabilities") or []:
        if not isinstance(cap, dict):
            continue
        verification = cap.get("verification")
        if not isinstance(verification, dict):
            continue
        effective = verification.get("effective_status")
        stored = cap.get("status")
        # Only a contradiction when the grounded check DISAGREES with the stored
        # status. ``unverified`` (no backing_paths) and an effective status that
        # matches the stored one are not conflicts.
        if effective in ("absent", "partial") and effective != stored:
            missing = [
                m for m in (verification.get("missing") or []) if isinstance(m, str)
            ]
            capability_contradictions.append(
                {
                    "name": _label(cap.get("name")),
                    "stored_status": stored,
                    "effective_status": effective,
                    "missing": missing[:6],
                    "missing_total": len(missing),
                }
            )

    # 2. Orphaned open gaps (active or blocked, excluding deferred ones).
    # Blocked gaps are still open problems with no direction working them, so
    # they are orphan-eligible too — just flagged with their status.
    orphaned_gaps: list[dict] = []
    deferred_gap_count = 0
    for g in gaps:
        if not isinstance(g, dict) or g.get("status") not in ("active", "blocked"):
            continue
        gid = str(g.get("id") or "")
        staffed = bool(gid and gid in referenced_gap_ids)
        if staffed:
            continue
        if g.get("deferral"):
            deferred_gap_count += 1
            continue
        orphaned_gaps.append(
            {
                "gap_id": gid,
                "name": _label(g.get("name")),
                "priority": g.get("priority"),
                "status": g.get("status"),
            }
        )

    # 3. Ungrounded active directions.
    ungrounded_directions: list[dict] = []
    for d in active_dirs:
        gap_id = d.get("gap_id")
        if not gap_id:
            reason = "no_gap"
        elif str(gap_id) not in active_gap_ids:
            reason = "gap_missing"
        else:
            continue
        ungrounded_directions.append(
            {
                "direction_id": d.get("id"),
                "text": _label(d.get("text")),
                "reason": reason,
            }
        )

    # 4. Per-direction research pipeline state.
    direction_pipeline: list[dict] = []
    pipeline_counts = {
        "research_waiting_on_action": 0,
        "tasks_in_progress": 0,
        "idle": 0,
    }
    for d in active_dirs:
        did = str(d.get("id") or "")
        in_flight = int(active_by_direction.get(did, 0) or 0)
        findings = len(d.get("recent_findings") or [])
        if in_flight > 0:
            state = "tasks_in_progress"
        elif findings > 0:
            state = "research_waiting_on_action"
        else:
            state = "idle"
        pipeline_counts[state] += 1
        direction_pipeline.append(
            {
                "direction_id": did,
                "text": _label(d.get("text")),
                "state": state,
                "in_flight": in_flight,
                "findings": findings,
            }
        )
    direction_pipeline.sort(
        key=lambda item: (
            {"research_waiting_on_action": 0, "tasks_in_progress": 1, "idle": 2}.get(
                str(item.get("state")), 3
            ),
            -int(item.get("findings") or 0),
            -int(item.get("in_flight") or 0),
            str(item.get("text") or ""),
        )
    )

    # 5. Merge-accounting drift: badge-asserted-shipped vs ancestry-verified.
    # Reuses the tri-state ``merge_verdict`` already stamped on each
    # ``recent_merged_titles`` entry, plus the exact per-direction verdict
    # counts/sample carried alongside that visible list (no second GitHub/
    # ancestry pass). Only a ``verified_not_landed`` title is a real
    # accounting conflict; ``unverifiable`` is tallied as unknown, never
    # manufactured into a contradiction.
    merge_accounting: list[dict] = []
    merge_landed_total = 0
    merge_not_landed_total = 0
    merge_unverifiable_total = 0
    for delta in per_direction_delta or []:
        if not isinstance(delta, dict):
            continue
        titles = delta.get("recent_merged_titles")
        titles_list = titles if isinstance(titles, list) else []
        count_block = (
            delta.get("recent_merge_verdict_counts")
            if isinstance(delta.get("recent_merge_verdict_counts"), dict)
            else None
        )
        sampled_titles = delta.get("recent_not_landed_titles")
        not_landed = (
            [_label(title) for title in sampled_titles if title is not None]
            if isinstance(sampled_titles, list)
            else []
        )
        landed_n = (
            _count_field(count_block, "verified_landed") if count_block else None
        )
        not_landed_n = (
            _count_field(count_block, "verified_not_landed") if count_block else None
        )
        unverifiable_n = (
            _count_field(count_block, "unverifiable") if count_block else None
        )
        if landed_n is None or not_landed_n is None or unverifiable_n is None:
            not_landed = []
            landed_n = 0
            not_landed_n = 0
            unverifiable_n = 0
            for entry in titles_list:
                if not isinstance(entry, dict):
                    continue
                verdict = entry.get("merge_verdict")
                if verdict == "verified_not_landed":
                    not_landed.append(_label(entry.get("title")))
                    not_landed_n += 1
                elif verdict == "verified_landed":
                    landed_n += 1
                else:
                    # Absent/unknown verdict → unverifiable, NOT a conflict.
                    unverifiable_n += 1
        elif not not_landed:
            for entry in titles_list:
                if not isinstance(entry, dict):
                    continue
                if entry.get("merge_verdict") == "verified_not_landed":
                    not_landed.append(_label(entry.get("title")))
        merge_landed_total += landed_n
        merge_not_landed_total += not_landed_n
        merge_unverifiable_total += unverifiable_n
        # Surface only directions with a genuine verified-not-landed conflict.
        if not_landed_n <= 0:
            continue
        merge_accounting.append(
            {
                "direction_id": delta.get("direction_id"),
                "direction_text": _label(delta.get("direction_text")),
                "not_landed_titles": not_landed,
                "landed": landed_n,
                "not_landed": not_landed_n,
                "unverifiable": unverifiable_n,
            }
        )
    merge_accounting.sort(
        key=lambda m: (-int(m.get("not_landed") or 0), str(m.get("direction_text") or ""))
    )

    # Nothing to reconcile (e.g. a cold-start project): let the caller omit it.
    if (
        not active_dirs
        and not capability_contradictions
        and not orphaned_gaps
        and not deferred_gap_count
        and not merge_accounting
    ):
        return None

    def _cap(items: list) -> tuple[list, int]:
        if len(items) <= _RECON_LIST_CAP:
            return items, 0
        return items[:_RECON_LIST_CAP], len(items) - _RECON_LIST_CAP

    capped_caps, caps_total = _cap(capability_contradictions)
    capped_orphans, orphans_total = _cap(orphaned_gaps)
    capped_ungrounded, ungrounded_total = _cap(ungrounded_directions)
    capped_pipeline, pipeline_dropped = _cap(direction_pipeline)
    capped_merge, merge_dropped = _cap(merge_accounting)
    # Cap each surfaced direction's not-landed title list too, preserving the
    # true count (``not_landed``) so the renderer can show "+N more".
    for m in capped_merge:
        full_titles = m["not_landed_titles"]
        if len(full_titles) > _RECON_LIST_CAP:
            m["not_landed_titles"] = full_titles[:_RECON_LIST_CAP]
        shown_titles = m["not_landed_titles"]
        m["not_landed_titles_overflow"] = max(
            0, int(m.get("not_landed") or 0) - len(shown_titles)
        )

    return {
        "capability_contradictions": capped_caps,
        "orphaned_gaps": capped_orphans,
        "ungrounded_directions": capped_ungrounded,
        "direction_pipeline": capped_pipeline,
        "merge_accounting": capped_merge,
        "counts": {
            # True totals (shown + overflow) so the renderer can say "+N more".
            "capability_contradictions": len(capped_caps) + caps_total,
            "orphaned_gaps": len(capped_orphans) + orphans_total,
            "ungrounded_directions": len(capped_ungrounded) + ungrounded_total,
            "deferred_gaps": deferred_gap_count,
            "active_directions": len(active_dirs),
            "direction_pipeline": len(direction_pipeline),
            "direction_pipeline_overflow": pipeline_dropped,
            "pipeline_waiting_on_action": pipeline_counts[
                "research_waiting_on_action"
            ],
            "pipeline_tasks_in_progress": pipeline_counts["tasks_in_progress"],
            "pipeline_idle": pipeline_counts["idle"],
            # Merge-accounting drift: true direction-with-conflict total (shown +
            # overflow) plus aggregate landed/not-landed/unverifiable tallies.
            "merge_accounting": len(capped_merge) + merge_dropped,
            "merge_landed": merge_landed_total,
            "merge_not_landed": merge_not_landed_total,
            "merge_unverifiable": merge_unverifiable_total,
        },
    }


# Direction statuses that mean the direction is no longer live work, so an active
# gap still pointing at it (via ``active_moves``) is resolved-in-fact-but-open.
# Mirrors the retired set used in research_direction_state / strategic_analysis.
_PRUNABLE_DIRECTION_STATUSES = frozenset(
    {"retired", "completed", "superseded", "expired"}
)


def _build_prune_candidates(
    problem_model: dict | None,
    directions: list | None,
    model_health: dict | None,
    now_iso: str,
) -> dict | None:
    """Concrete, named cleanup candidates — surfaced ONLY when the model is heavy.

    The problem model only grows unless the strategist explicitly closes entries;
    the size signal is advisory and there is no structural backstop. When the
    model reaches ``large`` or ``oversized`` the strategist is told to prune, but
    is given no concrete list of WHICH entries are prunable. This reuses signals
    the backend already computes to name specific entries that are resolved in
    fact but still open:

      * ``resolved_gaps`` — ``active`` gaps whose ``active_moves`` resolve to a
        direction that is retired/completed/superseded/expired (the work is done
        or abandoned) or to a direction with merged PRs (shipped). The gap was
        worked and the work landed/stopped, yet the gap is still open.
      * ``absent_capabilities`` — capabilities the grounded file-tree check marks
        ``absent`` (claimed-but-absent): the backing code does not exist, so the
        capability is a stale assertion safe to consolidate/drop.
      * ``expired_holds`` — ``active`` gaps carrying a deferral whose
        ``hold_until`` has already passed (parsed against ``now_iso``): the hold
        the strategist set has elapsed, so the gap is due for a decision.

    Returns ``None`` unless ``model_health["model_size_status"]`` is ``large`` or
    ``oversized`` (the nudge is only actionable when the model is actually heavy),
    or when no candidates surface. Strictly advisory — a suggestion list, never an
    auto-deletion. Pure and side-effect free, so it is directly unit-testable.
    Complements :func:`_build_cycle_start_reconciliation`, which surfaces
    orphaned/ungrounded items but not resolved-yet-still-open ones.
    """
    status = (model_health or {}).get("model_size_status")
    if status not in ("large", "oversized"):
        return None

    pm = problem_model if isinstance(problem_model, dict) else {}
    directions = directions or []

    def _label(text: Any) -> str:
        return str(text or "")[:_RECON_LABEL_MAXLEN]

    def _hold_expired(hold_until: Any) -> bool:
        # Tolerate missing/malformed values: only an ISO timestamp strictly
        # earlier than now counts as expired; anything unparseable is ignored.
        if not isinstance(hold_until, str) or not hold_until:
            return False
        try:
            held = datetime.fromisoformat(hold_until.replace("Z", "+00:00"))
            now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return False
        # Compare in a tz-consistent frame (treat naive as UTC).
        if held.tzinfo is None:
            held = held.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return held < now

    # direction_id -> status, and whether it has any merged PRs (shipped work).
    dir_status: dict[str, str] = {}
    dir_shipped: dict[str, bool] = {}
    for d in directions:
        if not isinstance(d, dict):
            continue
        did = str(d.get("id") or "")
        if not did:
            continue
        dir_status[did] = str(d.get("status") or "active")
        history = d.get("research_history") or []
        # Retiring a capability gap is a ground-truth "shipped" decision, so it
        # must use the verification-aware merged count — a degraded/unverified
        # merge (test/CI verdict blocked by an environment failure) is NOT proof
        # the work landed. Mirror the rest of the system: verified_merged_count
        # per entry (with its legacy prs_merged fallback) plus the verified
        # aggregate key (falling back to total_prs_merged for pre-attribution
        # folded history).
        merged = sum(
            verified_merged_count(h) for h in history if isinstance(h, dict)
        )
        agg = d.get("history_aggregate") or {}
        merged += int(
            agg.get("total_verified_prs_merged", agg.get("total_prs_merged", 0)) or 0
        )
        dir_shipped[did] = merged > 0

    gaps = pm.get("gaps") if isinstance(pm.get("gaps"), list) else []

    # 1. Resolved-yet-open gaps + 3. expired-hold gaps (single active-gap pass).
    resolved_gaps: list[dict] = []
    expired_holds: list[dict] = []
    for g in gaps:
        if not isinstance(g, dict) or g.get("status") != "active":
            continue
        moves = [str(m) for m in (g.get("active_moves") or []) if m]
        retired_moves = [
            m for m in moves if dir_status.get(m) in _PRUNABLE_DIRECTION_STATUSES
        ]
        shipped_moves = [m for m in moves if dir_shipped.get(m)]
        if retired_moves or shipped_moves:
            resolved_gaps.append(
                {
                    "gap_id": str(g.get("id") or ""),
                    "name": _label(g.get("name")),
                    "reason": (
                        "direction_retired" if retired_moves else "direction_shipped"
                    ),
                    "directions": (retired_moves or shipped_moves)[:6],
                }
            )
        deferral = g.get("deferral")
        if isinstance(deferral, dict) and _hold_expired(deferral.get("hold_until")):
            expired_holds.append(
                {
                    "gap_id": str(g.get("id") or ""),
                    "name": _label(g.get("name")),
                    "hold_until": deferral.get("hold_until"),
                }
            )

    # 2. Claimed-but-absent capabilities (grounded check says the code is gone).
    absent_capabilities: list[dict] = []
    for cap in pm.get("capabilities") or []:
        if not isinstance(cap, dict):
            continue
        verification = cap.get("verification")
        if not isinstance(verification, dict):
            continue
        if verification.get("effective_status") == "absent":
            absent_capabilities.append(
                {
                    "name": _label(cap.get("name")),
                    "stored_status": cap.get("status"),
                }
            )

    if not resolved_gaps and not absent_capabilities and not expired_holds:
        return None

    def _cap(items: list) -> tuple[list, int]:
        if len(items) <= _RECON_LIST_CAP:
            return items, 0
        return items[:_RECON_LIST_CAP], len(items) - _RECON_LIST_CAP

    capped_resolved, _ = _cap(resolved_gaps)
    capped_absent, _ = _cap(absent_capabilities)
    capped_holds, _ = _cap(expired_holds)

    return {
        "model_size_status": status,
        "resolved_gaps": capped_resolved,
        "absent_capabilities": capped_absent,
        "expired_holds": capped_holds,
        "counts": {
            # True totals (uncapped) so the renderer can say "+N more".
            "resolved_gaps": len(resolved_gaps),
            "absent_capabilities": len(absent_capabilities),
            "expired_holds": len(expired_holds),
        },
    }


_OPERATOR_STEERING_QUOTE_MAX_CHARS = 600
_OPERATOR_STEERING_QUOTE_COUNT = 2


def _build_operator_steering_directive(
    conversation_thread: list, *, fallback_excerpt: str | None = None
) -> str | None:
    """High-priority steering directive for an operator-woken strategic cycle.

    When a strategic cycle is spawned *because* an operator-origin
    strategic-refresh marker was armed, the operator's steering message would
    otherwise appear only as one undifferentiated entry in the (capped)
    ``conversation_thread`` snapshot. This selects the most recent operator
    steering message(s) from that same thread — ``from == 'user'``, excluding
    passive ``NOTE``s and system/routine digest events via the shared
    ``classify_operator_arming`` gate so "steering" means exactly what arming
    meant — and frames them as the reason this cycle exists, so the strategist
    treats them as a priority directive rather than one of N messages.

    ``fallback_excerpt`` is the operator message text persisted on the refresh
    marker at arm time. When the live thread snapshot carries no qualifying
    operator steering message (e.g. it aged out of the recent-messages window
    before the deferred cycle reached its review phase), the directive is
    synthesized from this durable excerpt instead — so an operator wake never
    silently loses its steering. Returns ``None`` only when neither the thread
    nor the fallback yields any steering text, in which case the caller appends
    no steering framing.
    """
    steering_msgs = []
    for m in conversation_thread:
        if m.get("from") != "user":
            continue
        try:
            mtype = m.get("type")
            if not isinstance(mtype, MessageType):
                mtype = MessageType(mtype)
            event_type = (m.get("context") or {}).get("event_type")
            if (
                classify_operator_arming(mtype, event_type=event_type)
                != ARMING_STEERING
            ):
                continue
        except Exception:
            # Unknown/malformed type — treat as steering rather than silently
            # dropping a potential directive.
            pass
        steering_msgs.append(m)
    # conversation_thread is chronological (oldest→newest); the steering
    # message(s) that woke this cycle are the most recent, so quote the tail.
    quoted = []
    for m in steering_msgs[-_OPERATOR_STEERING_QUOTE_COUNT:]:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if len(content) > _OPERATOR_STEERING_QUOTE_MAX_CHARS:
            content = content[:_OPERATOR_STEERING_QUOTE_MAX_CHARS] + "…"
        quoted.append(f"  > {content}")
    # Thread-aging fallback: the live snapshot yielded no operator message, but
    # the excerpt was durably persisted on the refresh marker at arm time. Build
    # the directive from it using the same truncation/framing as a found message.
    if not quoted and fallback_excerpt:
        content = fallback_excerpt.strip()
        if content:
            if len(content) > _OPERATOR_STEERING_QUOTE_MAX_CHARS:
                content = content[:_OPERATOR_STEERING_QUOTE_MAX_CHARS] + "…"
            quoted.append(f"  > {content}")
    if not quoted:
        return None
    return (
        "OPERATOR STEERING — THIS CYCLE WAS TRIGGERED BY OPERATOR STEERING.\n"
        "An operator sent a live-chat steering message that woke this strategic "
        "cycle; acting on that steering is the reason this cycle exists. Treat "
        "the operator message(s) below as a priority directive, NOT as one of "
        "the undifferentiated conversation_thread entries:\n" + "\n".join(quoted)
    )


def _summarize_conversation_thread(thread: list) -> list:
    """Keep the most recent N conversation messages."""
    if (
        not isinstance(thread, list)
        or len(thread) <= _CONVERSATION_THREAD_KEEP_WHEN_TRIMMED
    ):
        return thread
    return thread[-_CONVERSATION_THREAD_KEEP_WHEN_TRIMMED:]


_MEMO_CITATIONS_KEEP = 5
_MEMO_EXCERPT_MAX_CHARS = 400


def _summarize_memo_citations(memos: list) -> list:
    """Trim memo citations for inclusion in strategic-agent payloads.

    Keeps the most recent N memos (by task_id) and caps excerpt length
    so the digest survives under budget pressure rather than vanishing
    entirely. The strategic reviewer can cite memos by task_id; shorter
    excerpts still preserve citability.
    """
    if not isinstance(memos, list):
        return memos
    if len(memos) <= _MEMO_CITATIONS_KEEP:
        # No trimming needed for list size, but still cap excerpts
        return [
            {
                **m,
                "summary_excerpt": (m.get("summary_excerpt") or "")[
                    :_MEMO_EXCERPT_MAX_CHARS
                ]
                if isinstance(m, dict)
                else m,
            }
            for m in memos
        ]
    # Cap to most recent memos and trim excerpts
    trimmed = memos[:_MEMO_CITATIONS_KEEP]
    return [
        {
            **m,
            "summary_excerpt": (m.get("summary_excerpt") or "")[
                :_MEMO_EXCERPT_MAX_CHARS
            ]
            if isinstance(m, dict)
            else m,
        }
        for m in trimmed
    ]


# ── Open-message inbox (the strategist's authoritative feed) ──────────────────
# The strategist's open-message inbox is its OWN status-filtered query — NOT a
# slice of the capped conversation_thread. Without this, an OPEN BUG_REPORT or
# BLOCKER older than the 15 newest messages ages out of the thread window and
# the strategist never sees it, so it can never be acted on or resolved and
# stays open forever. The feed is size-bounded two ways (a hard message count +
# a per-message excerpt cap) and priority-ordered so unresolved
# BUG_REPORT/BLOCKER items (then the oldest-open items) are the LAST to be
# dropped under budget pressure, never the first.
_OPEN_MESSAGES_FETCH_LIMIT = 200       # DB window over open-status rows
_OPEN_MESSAGES_KEEP = 40               # count bound for the packed feed
_OPEN_MESSAGES_KEEP_WHEN_TRIMMED = 20  # tighter bound for the summarize fallback
_OPEN_MESSAGE_EXCERPT_MAX_CHARS = 800  # per-message content excerpt cap
_CRITICAL_OPEN_MESSAGE_TYPES = (
    MessageType.BUG_REPORT.value,
    MessageType.BLOCKER.value,
)


def _open_message_survival_key(msg: dict) -> tuple:
    """Sort key ranking an open message's survival priority HIGH→LOW.

    Critical types (BUG_REPORT/BLOCKER) rank first, with reproducing (non-stale)
    ones ahead of stale (citation_state == "absent") ones; within each tier,
    oldest-open items rank first (ascending created_at). The HEAD of the sorted
    list survives when the feed must be bounded, so reproducing criticals are
    last to be dropped, followed by stale criticals, then non-critical messages.
    """
    is_critical = msg.get("type") in _CRITICAL_OPEN_MESSAGE_TYPES
    citation_state = msg.get("citation_state")
    is_stale = citation_state == "absent"

    # Tier 0: reproducing critical messages (highest priority)
    # Tier 1: stale critical messages (lower than reproducing)
    # Tier 2: non-critical messages
    if is_critical and not is_stale:
        tier = 0
    elif is_critical and is_stale:
        tier = 1
    else:
        tier = 2

    return (tier, msg.get("created_at") or "")


def _bound_open_messages(messages: list, keep: int) -> list:
    """Bound *messages* to *keep* items and cap each message's excerpt length.

    Lowest-priority survivors are dropped first: BUG_REPORT/BLOCKER (then the
    oldest-open) items are the LAST to go. Survivors are returned in oldest-open
    chronological order so the strategist reads a stable inbox regardless of
    which items were trimmed.
    """
    if not isinstance(messages, list):
        return messages
    capped: list = []
    for m in messages:
        if isinstance(m, dict) and isinstance(m.get("content"), str):
            content = m["content"]
            if len(content) > _OPEN_MESSAGE_EXCERPT_MAX_CHARS:
                m = {**m, "content": content[:_OPEN_MESSAGE_EXCERPT_MAX_CHARS]}
        capped.append(m)
    if len(capped) <= keep:
        return capped
    survivors = sorted(
        (m for m in capped if isinstance(m, dict)),
        key=_open_message_survival_key,
    )[:keep]
    # Re-sort survivors chronologically (oldest-open first) for a stable read.
    survivors.sort(key=lambda m: m.get("created_at") or "")
    return survivors


def _summarize_open_messages(messages: list) -> list:
    """Budget-pressure fallback for the open-message inbox.

    Rather than dropping the entire inbox wholesale when the strategic payload
    is over budget, trim it to a tighter bound that PRESERVES unresolved
    BUG_REPORT/BLOCKER (and the oldest-open) items and drops the rest — so the
    critical inbox items survive budget pressure instead of vanishing.
    """
    return _bound_open_messages(messages, _OPEN_MESSAGES_KEEP_WHEN_TRIMMED)


def _open_message_citation_summary(context: Any) -> dict:
    """Promote a message's stored citation-grounding signal to a compact summary.

    BUG_REPORT/BLOCKER ingestion stores a reproduce-on-HEAD citation signal under
    ``context['_provenance_warnings']`` (the same key research findings use). This
    surfaces it at the top level of the packed open-message dict so the strategist
    sees a stale/broken-citation report as discounted instead of trusting it. The
    signal is only stored when a citation is unambiguously absent, so a present
    summary always means "cited code is missing on HEAD". Returns an empty dict
    (no key added) when there is no recorded warning.
    """
    if not isinstance(context, dict):
        return {}
    warnings = context.get("_provenance_warnings")
    if not isinstance(warnings, dict):
        return {}
    state = warnings.get("citation_state")
    if state != "absent":
        return {}
    summary: dict = {"citation_state": state}
    broken = warnings.get("broken_refs")
    if isinstance(broken, list) and broken:
        summary["broken_refs"] = broken
    ungrounded = warnings.get("ungrounded_refs")
    if isinstance(ungrounded, list) and ungrounded:
        summary["ungrounded_refs"] = ungrounded
    return summary


async def _fetch_open_messages(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    limit: int = _OPEN_MESSAGES_KEEP,
) -> list[dict]:
    """Authoritative open-message feed for a strategic spawn payload.

    Queries messages whose status is open DIRECTLY (backed by
    ``ix_projectmessage_project_status``), independent of the capped
    conversation_thread, so an OPEN BUG_REPORT/BLOCKER stays visible to the
    strategist until it is fixed or explicitly resolved no matter how many newer
    messages have accumulated. The result is size-bounded: at most *limit*
    messages, priority-ordered so BUG_REPORT/BLOCKER and oldest-open items
    survive, with per-message excerpts capped.

    Shared by every strategic spawn path (strategic_review + design_doc_update)
    so the open-message access pattern stops drifting apart. Each row carries
    its full ``id`` (UUID string) so the agent can resolve it by reference.
    """
    # status == "open" is the ONLY actionable status. Messages in "linked"
    # state (bug-class messages whose fix is in-flight, owned by a direction
    # or task) are intentionally excluded here — they already have an owner
    # and the strategist should not act on them again. Messages with status
    # "resolved" are terminal and also excluded. This filter is sufficient:
    # no extra clause is needed to exclude linked messages.
    result = await session.execute(
        select(ProjectMessage)
        .where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.status == "open",
        )
        .order_by(ProjectMessage.created_at.asc())
        .limit(_OPEN_MESSAGES_FETCH_LIMIT)
    )
    messages = [
        {
            "id": str(m.id),
            "type": m.message_type,
            "content": m.content,
            "context": m.context,
            "from": m.from_type,
            "status": m.status,
            "resolution": m.resolution,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "resolved_at": m.resolved_at.isoformat() if m.resolved_at else None,
            # Promote the reproduce-on-HEAD citation signal (stored on the
            # message context at ingestion) to the top level so a stale or
            # broken-citation bug report is VISIBLY discounted to the strategist
            # rather than silently trusted. Absent when no warning was recorded.
            **_open_message_citation_summary(m.context),
        }
        for m in result.scalars().all()
    ]
    return _bound_open_messages(messages, limit)


# Schemas for pack_under_budget. Each key declares its priority tier and an
# optional summarize_fn. Critical = always shipped (raises if can't fit).
# High = shipped unless budget exhausted by summarized criticals + earlier
# high items. Medium and low get progressively more aggressive trimming.
#
# When new fields are added to the strategic payload, register them here or
# the packer will warn (and treat them as low). A registered key with no
# summarize_fn is dropped wholesale rather than trimmed when over budget.
_STRATEGIC_REVIEW_SCHEMA: dict[str, dict] = {
    # Identity / routing — must always reach the agent
    "type": {"priority": "critical"},
    "research_mode": {"priority": "critical"},
    "project_name": {"priority": "critical"},
    "base_branch": {"priority": "critical"},
    "enable_self_improvement": {"priority": "critical"},
    "model": {"priority": "critical"},
    "effort": {"priority": "critical"},
    # Trim-proof authoritative direction counts (true active/paused/total computed
    # before any trimming) and the operator's real max-active ceiling. Critical
    # tier with no summarize_fn: tiny scalars the packer ships verbatim, so the
    # agent's direction-health/portfolio/list views can anchor their headline
    # count, slots line, and gap flags on the true active set even when the
    # heavier ``directions_summary`` detail list is trimmed or dropped for budget.
    "direction_counts": {"priority": "critical"},
    "max_active_directions": {"priority": "critical"},
    "prompt_suffix": {"priority": "high"},
    # Vision + intent — small but load-bearing for every decision
    "vision": {"priority": "critical"},
    # User-facing state the agent must respond to. The open-message inbox is
    # packed high but with a summarize fallback so BUG_REPORT/BLOCKER (and the
    # oldest-open) items survive budget pressure instead of the whole inbox
    # being dropped wholesale.
    "open_messages": {"priority": "high", "summarize": _summarize_open_messages},
    "running_update": {"priority": "high"},
    "running_update_acknowledged_at": {"priority": "high"},
    "user_last_active_at": {"priority": "high"},
    # Bounded summary of the operator's recent reconciliation overrides (what the
    # agent proposed vs. what the operator kept, per document). Medium priority
    # with no summarize_fn: it is already capped + char-bounded at build time, so
    # the packer drops it whole under budget rather than trimming. Closes the
    # operator-wins reconciliation loop so the agent reconciles instead of
    # re-proposing overridden values.
    "operator_overrides": {"priority": "medium"},
    # Computed cycle-start reconciliation of the four strategic surfaces
    # (capabilities vs grounding, gaps vs directions, per-direction research
    # pipeline). Conflict-focused and bounded at build time, so it ships whole
    # or not at all. High priority: it is the authoritative starting state the
    # agent reasons from, and it is small.
    "cycle_start_reconciliation": {"priority": "high"},
    # Core working set (the big stuff) — summarize when over budget
    "directions_summary": {"priority": "high", "summarize": _summarize_directions},
    # Directions already queued for the NEXT version (status=staged). Medium
    # priority with no summarize_fn: it is already capped at build time, so the
    # packer drops it whole under budget rather than trimming. Surfaced so the
    # strategist knows what is already staged and does NOT re-propose it.
    "staged_directions": {"priority": "medium"},
    "problem_model": {
        "priority": "high",
        "summarize": _agent_view_problem_model_packed,
        # Floor reservation = the WHOLE agent-view model. The packer reserves the
        # full model's bytes off the top of the budget so every other recoverable
        # sibling degrades or drops BEFORE the problem_model is ever reduced —
        # under normal cycle sizes the whole model ships intact and the
        # skeletonizing summarize path never fires. Only when the full model
        # cannot fit at all does the reservation exceed budget; the packer then
        # drops the floor guarantee and the summarize callback degrades the model
        # loudly (marker + _packer_meta record). The floor callback is a simple,
        # independent function that cannot fail, so the reservation is honored
        # even if the summarize callback raises an exception.
        "floor": _agent_view_problem_model_floor,
    },
    # Tiny, load-bearing soft size/health signal computed from the DURABLE
    # problem_model (honest counts, not the trimmed agent view). High priority,
    # no summarize_fn — it should only drop under genuine budget exhaustion.
    "model_health": {"priority": "high"},
    # Concrete cleanup candidates surfaced only when the model is large/oversized.
    # Medium priority with no summarize_fn: already capped at build time, so the
    # packer drops it whole under budget rather than trimming. Advisory.
    "prune_candidates": {"priority": "medium"},
    # Compact index of all playbook keys — survives budget pressure so the
    # strategist always knows what operational knowledge exists and can fetch
    # entries on demand via `./fetch playbook <key>`. High priority to persist
    # under budget pressure, even if the heavier playbook_summary is dropped.
    "playbook_keys_index": {"priority": "high"},
    # Compact, bounded navigation index over the grounded agent-view design doc
    # (one entry per non-internal section: key + bounded summary + reconciled
    # presence status/modules/warning for systems/capabilities). The full
    # per-section content is fetched on demand via POST /api/agent/design-doc
    # (op:get) — see _design_doc_index. The summarize fallback drops per-entry
    # summaries to a navigation skeleton under genuine budget exhaustion.
    "design_doc": {"priority": "medium", "summarize": _summarize_design_doc_index},
    # Compact, body-omitted index of existing knowledge documents (web_research /
    # design output). Medium priority with no summarize_fn: it is already capped
    # (count) and char-bounded (per-entry summary) at build time, so the packer
    # drops it whole under budget rather than trimming — mirroring
    # staged_directions / operator_overrides. Full bodies fetched on demand.
    "knowledge_index": {"priority": "medium"},
    "outcome_evaluation": {
        "priority": "medium",
        "summarize": _summarize_outcome_evaluation,
    },
    "recent_interventions": {"priority": "medium"},
    "conversation_thread": {
        "priority": "medium",
        "summarize": _summarize_conversation_thread,
    },
    # Long-tail context — first to be dropped when budget is tight
    "self_reflection_log": {"priority": "low"},
    "previous_decision_outcomes": {"priority": "low"},
    "anomaly_summary": {"priority": "low"},
    "playbook_summary": {"priority": "low"},
    "rejected_proposals": {"priority": "low"},
    "running_update_superseded": {"priority": "low"},
    "prediction_calibration_factor": {"priority": "low"},
    "prediction_calibration_note": {"priority": "low"},
    # Mode-aware additions: only present when non-empty
    "system_maturity": {"priority": "low"},
    # drift_signals is actionable mode-switch input (audit/pause triggers), so it
    # must survive budget pressure — promoted to medium alongside conversation_thread.
    "drift_signals": {"priority": "medium"},
    # Lessons from retired directions — helps the agent avoid repeating patterns.
    "past_retirement_critiques": {"priority": "low"},
    # Raw retired-direction context (metrics + failed_approaches). Lower priority
    # than past_retirement_critiques (curated) — dropped first when budget is tight.
    "retired_directions_summary": {"priority": "low"},
    # LTS version/phase context — present only on self-repo projects. High priority
    # so it survives budget pressure (the agent needs it for phase-transition decisions).
    "version_phase": {"priority": "high", "summarize": _summarize_version_phase},
    # What is actually deployed (running live on the promotion target) vs what
    # has merged to the integration branch but is not yet promoted. High
    # priority: it is load-bearing for every runtime-metric judgment, and small.
    "deployment_state": {"priority": "high"},
    # Citable memo digest — terminal reasoning artifacts spawned from dropped research
    # findings. Medium priority so it survives typical payload pressure; the summarize_fn
    # trims memos and excerpts so it degrades gracefully (fewer memos / shorter text)
    # rather than vanishing entirely. The strategic reviewer must see memos to cite them.
    "memo_citations": {"priority": "medium", "summarize": _summarize_memo_citations},
    # Per-axis efficiency axes with drift vs baseline. Medium priority so it
    # survives typical payload pressure; the summarize_fn trims history tails
    # so it degrades gracefully before being dropped entirely.
    "efficiency_snapshot": {
        "priority": "medium",
        "summarize": _summarize_efficiency_snapshot,
    },
    # Held-directions block: directions that have hit the consecutive-failure
    # hold threshold. Low priority — purely observational planning context the
    # strategic agent reads to decide whether to refine/retire a stuck direction.
    "held_directions": {"priority": "low"},
}


_DESIGN_DOC_UPDATE_SCHEMA: dict[str, dict] = {
    "type": {"priority": "critical"},
    "project_name": {"priority": "critical"},
    "base_branch": {"priority": "critical"},
    "model": {"priority": "critical"},
    "effort": {"priority": "critical"},
    "vision": {"priority": "critical"},
    "open_messages": {"priority": "high", "summarize": _summarize_open_messages},
    # Compact, bounded navigation index over the grounded agent-view design doc
    # — see _STRATEGIC_REVIEW_SCHEMA["design_doc"] and _design_doc_index. Full
    # per-section content is fetched on demand via POST /api/agent/design-doc.
    "design_doc": {"priority": "high", "summarize": _summarize_design_doc_index},
    "problem_model": {
        "priority": "high",
        "summarize": _agent_view_problem_model_packed,
        # Floor reservation = the WHOLE agent-view model. The packer reserves the
        # full model's bytes off the top of the budget so every other recoverable
        # sibling degrades or drops BEFORE the problem_model is ever reduced —
        # under normal cycle sizes the whole model ships intact and the
        # skeletonizing summarize path never fires. Only when the full model
        # cannot fit at all does the reservation exceed budget; the packer then
        # drops the floor guarantee and the summarize callback degrades the model
        # loudly (marker + _packer_meta record). The floor callback is a simple,
        # independent function that cannot fail, so the reservation is honored
        # even if the summarize callback raises an exception.
        "floor": _agent_view_problem_model_floor,
    },
    # See _STRATEGIC_REVIEW_SCHEMA: tiny, load-bearing soft size/health signal.
    "model_health": {"priority": "high"},
    "proposed_changes": {"priority": "high"},
    "shipped_changes_manifest": {"priority": "medium"},
    # Compact, body-omitted index of existing knowledge documents — see
    # _STRATEGIC_REVIEW_SCHEMA["knowledge_index"]. Capped at build time, dropped
    # whole under budget; full bodies fetched on demand.
    "knowledge_index": {"priority": "medium"},
}


async def _build_shipped_changes_manifest(
    project: Project, session: AsyncSession
) -> list[dict]:
    """Build a manifest of merged tasks since the last architecture review.

    Uses design_doc._meta.last_updated as the cutoff, falling back to the
    most recent phase1_completed_at from vision._strategic_cycle_history.
    Returns a list of direction-grouped dicts, capped at 50 merged tasks.
    """

    # Determine cutoff timestamp.
    # Task.updated_at is TIMESTAMP WITHOUT TIME ZONE — the cutoff must be naive
    # UTC or asyncpg raises "can't subtract offset-naive and offset-aware
    # datetimes" when binding the parameter.
    def _parse_naive_utc(ts: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except (ValueError, TypeError):
            return None

    cutoff: datetime | None = None
    last_updated = (project.design_doc or {}).get("_meta", {}).get("last_updated")
    if last_updated:
        cutoff = _parse_naive_utc(last_updated)

    if cutoff is None:
        # Audit W2: cycle history is the StrategicCycle table now. Walk
        # newest-first; the helper returns rows in that order.
        recent_cycles = await strategic_cycle.all_for_project(
            session, project.id, limit=20
        )
        for c in recent_cycles:
            if c.phase1_completed_at:
                cutoff = c.phase1_completed_at
                break

    # Query completed tasks updated after cutoff (filter pr_merged in Python)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project.id,
            Task.status == TaskStatus.COMPLETED,
        )
        .order_by(Task.updated_at.desc())
        .limit(200)
    )
    if cutoff is not None:
        stmt = stmt.where(Task.updated_at > cutoff)

    result = await session.execute(stmt)
    completed_tasks = result.scalars().all()

    # Filter to merged tasks and collect manifest entries (cap at 50).
    # Only include root tasks (parent_task_id is None): rollup children merge to a
    # rollup staging branch, not the integration branch, so they have not "shipped"
    # until the rollup parent itself merges. Including children would tell the design
    # doc agent that work shipped when it only reached staging.
    manifest_entries: list[dict] = []
    for t in completed_tasks:
        ctx = t.context or {}
        if not ctx.get("pr_merged") or t.parent_task_id is not None:
            continue
        entry: dict = {
            "title": t.title,
            "direction_id": ctx.get("direction_id"),
            "changed_files": ctx.get("_pr_changed_files") or [],
            "scope": ctx.get("scope"),
        }
        manifest_entries.append(entry)
        if len(manifest_entries) >= 50:
            break

    if not manifest_entries:
        return []

    # Group by direction_id
    by_direction: dict[str, dict] = {}
    for entry in manifest_entries:
        dir_id = entry["direction_id"] or "unassigned"
        if dir_id not in by_direction:
            by_direction[dir_id] = {
                "direction_id": entry["direction_id"],
                "tasks": [],
                "all_changed_files": [],
            }
        by_direction[dir_id]["tasks"].append(
            {
                "title": entry["title"],
                "scope": entry["scope"],
                "changed_files": entry["changed_files"],
            }
        )
        for f in entry["changed_files"]:
            if f not in by_direction[dir_id]["all_changed_files"]:
                by_direction[dir_id]["all_changed_files"].append(f)

    return list(by_direction.values())


_TRIVIAL_ROLLUP_RE = None  # lazy: re module imported inline


def _rollup_body_is_trivial(body: str | None) -> bool:
    """Rollup PR bodies usually start with 'Rollup of N subtasks...' — that
    string carries no information about what actually shipped. Flag so callers
    can substitute per-subtask detail instead."""
    if not body:
        return True
    import re

    return bool(re.match(r"^\s*Rollup of \d+ subtasks?\b", body))


async def _build_recent_interventions(
    project: Project,
    session: AsyncSession,
    limit: int = 15,
    lookback_days: int = 21,
    last_strategic_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a rolling-window view of recent merged work.

    Replaces the prior weekly_rates view because weekly buckets are too coarse
    for this pipeline's velocity (20-40 tasks/day → 150-300/week). Weekly
    signal arrives 7 days late and noise dominates small samples. The rolling
    windows let the strategic agent reason about what's happening NOW vs the
    recent trend without waiting for week-end.

    Returns:
      - merges: last `limit` merged tasks with pr_title, pr_number, body
        (rollup body if non-trivial; else a synthesized summary from
        subtask titles), direction_id, direction_text, merged_at, hours_ago
      - rolling_rates: create/merge/rate for multiple windows:
        last_24h, last_3d, last_7d, since_last_strategic
      - per_direction_delta: for each direction, what shipped in each window
      - lookback_days: the outer data window
    """
    now = _utcnow()
    cutoff = now - timedelta(days=lookback_days)

    # Phase F3 (H3): bound the per-project lookback scan. With a 14-day default
    # window and 20-40 tasks/day, a busy project hits 280-560 rows; the cap at
    # 500 keeps the working set bounded for projects that burst higher. Order
    # newest-first so a truncated result still reflects the most recent rolling
    # windows (last_24h, last_3d, last_7d, since_last_strategic) — those rolling
    # buckets are the load-bearing outputs of this function. Drop heavy JSONBs
    # we don't read here (plan_output) via load_only, but keep status_log so
    # the Task event listeners (models.py ~360) don't trip MissingGreenlet under
    # async sessions even on read-only access.
    tasks_result = await session.execute(
        select(Task)
        .where(
            Task.project_id == project.id,
            Task.created_at >= cutoff,
        )
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.created_at,
                Task.updated_at,
                Task.context,
                Task.payloads,
                Task.parent_task_id,
                Task.status_log,
                # title + description are read by _build_recent_interventions
                # (lines ~613, 629, 638) when constructing the merge summary the
                # strategic agent consumes. Excluding them caused MissingGreenlet
                # at lazy-load time when this function ran from
                # task_completion._handle_design_doc_completion → spawn fails
                # silently and the strategic cycle stalls.
                Task.title,
                Task.description,
            )
        )
        .order_by(Task.created_at.desc())
        .limit(500)
    )
    tasks_in_window = list(tasks_result.scalars().all())
    if len(tasks_in_window) >= 500:
        logger.warning(
            "[STRATEGIC] creation_velocity truncated to 500 rows for project=%s "
            "(lookback=%dd) — extend lookback OR add LIMIT-aware logic if the "
            "rolling rates need to span a busier window",
            project.id,
            lookback_days,
        )
    active_tasks_result = await session.execute(
        select(Task)
        .where(
            Task.project_id == project.id,
            Task.status.in_(_IN_FLIGHT_TASK_STATUSES),
        )
        .options(load_only(Task.id, Task.status, Task.context, Task.payloads))
    )
    active_tasks = list(active_tasks_result.scalars().all())

    # Build direction lookup for text enrichment
    directions_by_id: dict[str, str] = {
        d.get("id", ""): d.get("text", "")[:200]
        for d in (project.directions or [])
        if d.get("id")
    }

    # Rolling windows: tighter than weekly, self-scaling to throughput.
    # "since_last_strategic" anchors to the actual review cadence so the
    # agent sees what's shipped since it last looked, not since Monday.
    _24h_ago = now - timedelta(hours=24)
    _3d_ago = now - timedelta(days=3)
    _7d_ago = now - timedelta(days=7)
    _strategic_cutoff = last_strategic_at  # may be None for first-ever cycle

    _TERMINAL = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)

    def _window_counts(tasks: list[Task], since: datetime) -> dict[str, Any]:
        """Count *terminal* tasks that reached their final state within the window.

        Filtering by terminal-updated-in-window instead of created-in-window
        avoids the in-flight bias that would otherwise make short windows
        look artificially bad: e.g. 16 medium tasks spawned in the last 24h
        of which 12 are still QUEUED/IN_PROGRESS/EVALUATING would read as
        "1/16 merged, 6% rate" even though the measured rate is 40%+ once
        those tasks finish. Slower-scope tasks (medium, large) were the
        biggest victims. "merged/finished" answers the operator question
        "of the work that landed or failed in this window, what fraction
        succeeded?" — the number they actually want to read.
        """
        from app.time_utils import _to_naive_utc

        since_naive = _to_naive_utc(since)
        merged = 0
        finished = 0
        spawned = 0  # retained for throughput visibility
        for t in tasks:
            # Task.created_at and updated_at are TIMESTAMPTZ since 0061; since is naive.
            created_at = _to_naive_utc(t.created_at)
            if created_at and since_naive and created_at >= since_naive:
                spawned += 1
            if t.status not in _TERMINAL:
                continue
            updated_at = _to_naive_utc(t.updated_at) or created_at
            if not updated_at or (since_naive and updated_at < since_naive):
                continue
            finished += 1
            # Count only integration-landed merges: root tasks (parent_task_id is None)
            # whose PR merged directly to the integration branch. Child tasks (rollup
            # members) have pr_merged=True when their PR lands on the rollup staging
            # branch — not on the integration branch — so they must not be counted here.
            if (
                t.status == TaskStatus.COMPLETED
                and (t.context or {}).get("pr_merged")
                and t.parent_task_id is None
            ):
                merged += 1
        rate = round(merged / finished, 3) if finished > 0 else None
        return {
            "finished": finished,
            "merged": merged,
            "spawned": spawned,
            "merge_rate": rate,
            # Legacy alias so existing consumers that read .created keep
            # working. Points at finished (not spawned) because "denominator"
            # for the merge_rate is finished-in-window.
            "created": finished,
        }

    rolling_rates = {
        "last_24h": _window_counts(tasks_in_window, _24h_ago),
        "last_3d": _window_counts(tasks_in_window, _3d_ago),
        "last_7d": _window_counts(tasks_in_window, _7d_ago),
    }
    if _strategic_cutoff:
        rolling_rates["since_last_strategic"] = _window_counts(
            tasks_in_window, _strategic_cutoff
        )

    # Extract the latest N merged tasks, newest first
    merged_tasks = [
        t
        for t in tasks_in_window
        if t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged")
    ]
    merged_tasks.sort(
        key=lambda t: (
            t.updated_at or t.created_at or datetime.min.replace(tzinfo=timezone.utc)
        ),
        reverse=True,
    )

    # Rollup-aware automain landing check.
    # A task with pr_merged=True may not be on automain yet when it was merged
    # into a rollup staging branch but the rollup PR itself hasn't been merged.
    # Root tasks (no parent): on automain only when git-ancestry confirms it; unverifiable
    # cases report False (fail-closed).
    # Child tasks → on automain iff their rollup parent's pr_merged is True.
    # We fetch parent contexts in one query so the per-direction loop below
    # can reuse the same map without extra round-trips.
    _all_parent_ids: set[uuid.UUID] = {
        t.parent_task_id for t in merged_tasks if t.parent_task_id is not None
    }
    _parent_pr_merged: dict[uuid.UUID, bool] = {}
    _parent_rollup_pr: dict[uuid.UUID, int | str | None] = {}
    if _all_parent_ids:
        _parents_result = await session.execute(
            select(Task)
            .where(Task.id.in_(_all_parent_ids))
            .options(load_only(Task.id, Task.context, Task.status_log))
        )
        for _p in _parents_result.scalars().all():
            _p_ctx = _p.context or {}
            _parent_pr_merged[_p.id] = bool(_p_ctx.get("pr_merged", False))
            _parent_rollup_pr[_p.id] = _p_ctx.get("rollup_pr_number") or _p_ctx.get(
                "pr_number"
            )

    # GitHub-ancestry verification for the on_automain signal.
    # The pr_merged flag is set when a PR is observed merged, but does NOT
    # confirm the commit is reachable from the integration branch (e.g. the
    # PR may have landed on a rollup staging branch that itself never merged).
    # We attempt a best-effort ancestry check using the existing github
    # primitives.  When credentials are unavailable or an API call fails the
    # fallback is fail-closed (False), NOT fail-open — see _on_automain below.
    _gh_token_ss: str | None = None
    _gh_owner_ss: str | None = None
    _gh_repo_ss: str | None = None
    _integration_branch_ss = get_project_integration_branch(project)
    _promotion_target_ss = get_project_promotion_target(project)
    _automain_verified: dict[uuid.UUID, bool] = {}
    _on_main_verified: dict[uuid.UUID, bool] = {}

    try:
        from app import github as _github_ss
        from app.release_qualifier import _resolve_github_with_session as _resolve_gh

        _gh_creds = await _resolve_gh(session, project)
        if _gh_creds:
            _gh_token_ss, _gh_owner_ss, _gh_repo_ss = _gh_creds
    except Exception as _exc_ss:  # noqa: BLE001 — credential failure → flag fallback
        logger.debug("[STRATEGIC] GitHub credential resolution skipped: %s", _exc_ss)

    if _gh_token_ss and _gh_owner_ss and _gh_repo_ss:
        # Collect the bounded set of tasks that will actually appear in the feed:
        # top-limit merges_out entries + tasks merged since the last strategic
        # cycle (the per_direction_delta recent window).
        _recent_cutoff_verify = _strategic_cutoff or _24h_ago
        _to_verify_ids: set[uuid.UUID] = {t.id for t in merged_tasks[:limit]}
        for _t_v in merged_tasks:
            _ts_v = _t_v.updated_at or _t_v.created_at
            if _ts_v and _ts_v >= _recent_cutoff_verify:
                _to_verify_ids.add(_t_v.id)
        _to_verify = [t for t in merged_tasks if t.id in _to_verify_ids]

        _pr_data_cache: dict[int, dict] = {}

        for _t in _to_verify:
            _ctx_t = _t.context or {}
            # Resolve which PR's merge commit to verify for ancestry.
            # A rollup child task merges into the rollup staging branch, so the
            # child PR's own merge commit lives on that branch — once the rollup
            # PR squash-merges, the child commit is NOT an ancestor of the
            # integration branch. The child is on automain iff the *parent's*
            # rollup PR landed, so verify that PR instead of the child's own.
            if _t.parent_task_id is not None:
                _pr_num_raw = _parent_rollup_pr.get(_t.parent_task_id)
            else:
                _pr_num_raw = _ctx_t.get("rollup_pr_number") or _ctx_t.get("pr_number")
            if not _pr_num_raw:
                # No PR number available — cannot verify, use flag fallback later
                continue
            try:
                _pr_num = int(_pr_num_raw)
                if _pr_num not in _pr_data_cache:
                    _pr_data_cache[_pr_num] = await _github_ss.get_pull_request(
                        _gh_token_ss,
                        _gh_owner_ss,
                        _gh_repo_ss,
                        _pr_num,
                        project_id=str(project.id),
                    )
                _pr_data = _pr_data_cache[_pr_num]
                _merge_sha = _pr_data.get("merge_commit_sha")
                if not _merge_sha:
                    # PR has no merge SHA (not yet merged or API gap) → not landed
                    _automain_verified[_t.id] = False
                    continue

                _is_anc = await _github_ss.is_ancestor_of(
                    _gh_token_ss,
                    _gh_owner_ss,
                    _gh_repo_ss,
                    _merge_sha,
                    _integration_branch_ss,
                    project_id=str(project.id),
                )

                # is_ancestor_of returns True (merge commit reachable from the
                # integration branch), False (genuinely not reachable), or None
                # (compare API indeterminate). Only True confirms the change
                # landed; False and None are both degrade-safe NOT-landed.
                _automain_verified[_t.id] = _is_anc is True

                # Deployed signal: also test reachability from the promotion
                # target. on_automain=True & on_main=False is exactly "landed on
                # the integration branch but NOT yet promoted" — i.e. not running
                # live. Reuses the already-fetched merge SHA, so it adds only one
                # compare call per already-verified PR, and only when there is an
                # LTS split (otherwise the two branches are identical and
                # on_main == on_automain).
                if _promotion_target_ss != _integration_branch_ss:
                    _is_anc_main = await _github_ss.is_ancestor_of(
                        _gh_token_ss,
                        _gh_owner_ss,
                        _gh_repo_ss,
                        _merge_sha,
                        _promotion_target_ss,
                        project_id=str(project.id),
                    )
                    _on_main_verified[_t.id] = _is_anc_main is True
            except Exception as _exc_t:  # noqa: BLE001 — API failure → flag fallback
                logger.debug(
                    "[STRATEGIC] ancestry check failed for task %s (pr=%s): %s",
                    _t.id,
                    _pr_num_raw,
                    _exc_t,
                )
                # Leave _t.id absent from _automain_verified; flag fallback used below

    def _on_automain(task: Task) -> bool:
        # Prefer the GitHub-verified result when available.
        if task.id in _automain_verified:
            return _automain_verified[task.id]
        # Fallback: flag heuristic (when credentials unavailable or PR has no number).
        # Root tasks with unverifiable ancestry report False (fail-closed) — we cannot
        # confirm the commit is reachable from the integration branch, so we must not
        # assume it landed.  Child tasks depend on whether their rollup parent's PR was
        # merged (DB-recorded flag), which already defaults to False.
        if task.parent_task_id is None:
            return False
        return _parent_pr_merged.get(task.parent_task_id, False)

    def _on_main(task: Task) -> bool:
        # No LTS split: the integration branch IS the promotion target, so
        # landing on one is landing on the other.
        if _promotion_target_ss == _integration_branch_ss:
            return _on_automain(task)
        # Verified reachable from the promotion target = promoted (deploying / on
        # the live branch). Fail-closed to False when unverified — un-promoted
        # work must never be reported as deployed.
        return _on_main_verified.get(task.id, False)

    def _merge_verdict(task: Task) -> str:
        """Tri-state ancestry verdict for the recent merged-title window.

        ``_on_automain`` is fail-closed: it collapses "ancestry-verified NOT
        landed" and "ancestry could not be confirmed" into a single ``False``.
        Cycle-start merge-accounting reconciliation needs to keep these apart —
        only a verified-not-landed merge is a real accounting conflict, while an
        unverifiable one must NOT manufacture a contradiction. This reads only
        the already-populated ``_automain_verified`` map (present True = landed,
        present False = verified-not-landed, absent = unverifiable), so it adds
        no second GitHub/ancestry pass.
        """
        if task.id in _automain_verified:
            return "verified_landed" if _automain_verified[task.id] else "verified_not_landed"
        return "unverifiable"

    # For rollups: look up child titles so the agent sees what actually
    # shipped instead of "Rollup of 4 subtasks" + a title.
    merged_parent_ids = {str(t.id) for t in merged_tasks[:limit]}
    child_titles_by_parent: dict[str, list[str]] = {}
    if merged_parent_ids:
        children_result = await session.execute(
            select(Task).where(
                Task.parent_task_id.in_([uuid.UUID(p) for p in merged_parent_ids]),
                Task.status == TaskStatus.COMPLETED,
            )
        )
        for c in children_result.scalars().all():
            # Only include real product-work children — skip review/fix pipeline tasks
            c_type = c.first_payload.get("type") if c.first_payload else ""
            if c_type in ("review", "fix"):
                continue
            child_titles_by_parent.setdefault(str(c.parent_task_id), []).append(
                (c.title or "")[:150]
            )

    merges_out: list[dict[str, Any]] = []
    for t in merged_tasks[:limit]:
        ctx = t.context or {}
        merged_at = t.updated_at or t.created_at
        direction_id = ctx.get("direction_id")
        hours_ago: float | None = None
        if merged_at:
            hours_ago = round((now - merged_at).total_seconds() / 3600.0, 1)

        # Body resolution: use t.description when available and informative.
        # If it's the trivial "Rollup of N subtasks..." boilerplate, replace
        # with a synthesized summary from child titles — the agent needs to
        # know WHAT shipped, not just how many things.
        body = (t.description or "")[:1200]
        if _rollup_body_is_trivial(body):
            children = child_titles_by_parent.get(str(t.id), [])
            if children:
                body = "Rollup contents:\n- " + "\n- ".join(children[:8])
            else:
                body = ""

        merges_out.append(
            {
                "pr_title": (t.title or "")[:200],
                "pr_number": ctx.get("rollup_pr_number") or ctx.get("pr_number"),
                "direction_id": direction_id,
                "direction_text": directions_by_id.get(direction_id, "")
                if direction_id
                else "",
                "merged_at": merged_at.isoformat() if merged_at else None,
                "hours_ago": hours_ago,
                "body": body,
                "on_automain": _on_automain(t),
                "on_main": _on_main(t),
            }
        )

    # Per-direction delta: for each active direction, counts + titles per
    # window. Lets the agent see "direction X shipped N things since my
    # last cycle" without having to cross-correlate merges[] by direction_id.
    per_direction_delta: list[dict[str, Any]] = []
    for d in project.directions or []:
        d_id = d.get("id")
        if not d_id or d.get("status") != "active":
            continue
        d_tasks = [
            t for t in tasks_in_window if (t.context or {}).get("direction_id") == d_id
        ]
        entry: dict[str, Any] = {
            "direction_id": d_id,
            "direction_text": (d.get("text") or "")[:200],
            "last_24h": _window_counts(d_tasks, _24h_ago),
            "last_3d": _window_counts(d_tasks, _3d_ago),
        }
        if _strategic_cutoff:
            entry["since_last_strategic"] = _window_counts(d_tasks, _strategic_cutoff)
        # Titles of things that shipped since last strategic (or last 24h as fallback).
        # Each entry is a dict so the agent can distinguish automain-landed from
        # staging-only merges (on_automain=False means rollup not yet merged).
        recent_cutoff = _strategic_cutoff or _24h_ago
        recent_merges_dicts: list[dict[str, Any]] = []
        recent_not_landed_titles: list[str] = []
        recent_merge_verdict_counts = {
            "verified_landed": 0,
            "verified_not_landed": 0,
            "unverifiable": 0,
        }
        for t in d_tasks:
            if (
                t.status != TaskStatus.COMPLETED
                or not (t.context or {}).get("pr_merged")
                or (
                    t.updated_at
                    or t.created_at
                    or datetime.min.replace(tzinfo=timezone.utc)
                )
                < recent_cutoff
            ):
                continue
            merge_verdict = _merge_verdict(t)
            title = (t.title or "")[:160]
            recent_merges_dicts.append(
                {
                    "title": title,
                    "on_automain": _on_automain(t),
                    "merge_verdict": merge_verdict,
                }
            )
            if merge_verdict not in recent_merge_verdict_counts:
                merge_verdict = "unverifiable"
            recent_merge_verdict_counts[merge_verdict] += 1
            if (
                merge_verdict == "verified_not_landed"
                and len(recent_not_landed_titles) < _RECON_LIST_CAP
            ):
                recent_not_landed_titles.append(title)
        if recent_merges_dicts:
            entry["recent_merged_titles"] = recent_merges_dicts[
                :_RECENT_MERGED_TITLES_CAP
            ]
            entry["recent_merge_verdict_counts"] = recent_merge_verdict_counts
            if recent_not_landed_titles:
                entry["recent_not_landed_titles"] = recent_not_landed_titles
        per_direction_delta.append(entry)

    # Active-by-scope: tasks currently in flight per scope. Surfaced so the
    # strategic agent doesn't lose sight of in-flight work now that the
    # merge_rate denominators are "finished-in-window" only. Without this,
    # pulling in-flight out of the rate would hide slow-moving large-scope
    # tasks entirely — the agent would see "no large merges in 24h" with
    # no signal that 3 large tasks are actively running.
    active_by_scope: dict[str, int] = {
        "small": 0,
        "medium": 0,
        "large": 0,
        "unknown": 0,
    }
    # In-flight task count per direction — the "is work already moving on this
    # direction" signal the cycle-start reconciliation reads to distinguish a
    # direction whose research is piling up unactioned from one with tasks
    # already in progress. Counts ANY in-flight task (research/plan/create/fix/
    # review) attributed to the direction, since each means the lane is active.
    active_by_direction: dict[str, int] = {}
    for t in active_tasks:
        did = (t.context or {}).get("direction_id")
        if did:
            active_by_direction[did] = active_by_direction.get(did, 0) + 1
        # Only count ROLLUP_TYPES work — pipeline tasks (review/fix) aren't
        # scope-classified and would inflate the counts.
        t_type = (t.first_payload or {}).get("type", "")
        if t_type in ("review", "fix"):
            continue
        scope = (t.context or {}).get("scope") or "unknown"
        if scope not in active_by_scope:
            active_by_scope[scope] = 0
        active_by_scope[scope] += 1

    # Directions whose shipped work has landed on the integration branch but is
    # NOT yet on the promotion target (promoted) — the capabilities whose
    # live/runtime metrics cannot move until the version promotes. The concrete
    # companion to the deploy-model framing. Only populated when the promotion
    # target was actually verifiable (creds present + an LTS split), so a
    # credential gap never manufactures false "undeployed" noise.
    undeployed_directions: list[dict[str, Any]] = []
    if (
        _gh_token_ss
        and _gh_owner_ss
        and _gh_repo_ss
        and _promotion_target_ss != _integration_branch_ss
    ):
        _undeployed_by_dir: dict[str, dict[str, Any]] = {}
        for _m in merges_out:
            if _m.get("on_automain") and not _m.get("on_main"):
                _did = _m.get("direction_id") or "_unattributed"
                _slot = _undeployed_by_dir.setdefault(
                    _did,
                    {
                        "direction_id": _m.get("direction_id"),
                        "direction_text": _m.get("direction_text") or "",
                        "merge_titles": [],
                    },
                )
                if len(_slot["merge_titles"]) < 5:
                    _slot["merge_titles"].append(_m.get("pr_title") or "")
        undeployed_directions = list(_undeployed_by_dir.values())

    result: dict[str, Any] = {
        "merges": merges_out,
        "rolling_rates": rolling_rates,
        "per_direction_delta": per_direction_delta,
        "active_by_scope": active_by_scope,
        "active_by_direction": active_by_direction,
        "lookback_days": lookback_days,
        "undeployed_directions": undeployed_directions,
    }
    if _strategic_cutoff:
        result["hours_since_last_strategic"] = round(
            (now - _strategic_cutoff).total_seconds() / 3600.0, 1
        )
    return result


def _build_previous_decision_outcomes(
    project: Project, cycle_history: list
) -> dict[str, Any] | None:
    """Summarize outcomes of the most recent completed strategic cycle's
    direction create/retire decisions so the agent can learn from them.

    Walks ``cycle_history`` newest-first for an entry with outcome
    "completed" that also recorded created_direction_ids or
    retired_direction_ids. For each direction id it looks up the current
    direction in project.directions and reports cycles_active, prs_merged,
    quality_score, and paused flags. Retired directions missing from the
    current list are reported with status="missing".

    Audit W2: ``cycle_history`` is now passed in (was JSONB); the async
    caller pre-fetches via ``strategic_cycle.recent_for_agent_context``.
    """
    cycle_history = list(cycle_history or [])
    if not cycle_history:
        return None
    prev_entry = None
    for entry in reversed(cycle_history):
        if entry.get("outcome") != "completed":
            continue
        if entry.get("created_direction_ids") or entry.get("retired_direction_ids"):
            prev_entry = entry
            break
    if prev_entry is None:
        return None

    directions_by_id: dict[str, dict] = {
        d.get("id"): d for d in (project.directions or []) if d.get("id")
    }

    created_rationales = prev_entry.get("created_rationales") or {}
    retired_rationales = prev_entry.get("retired_rationales") or {}
    created_predictions = prev_entry.get("created_predictions") or {}
    retired_final_metrics = prev_entry.get("retired_final_metrics") or {}

    def _summarize(
        direction_id: str, role: str, rationale: str | None
    ) -> dict[str, Any]:
        d = directions_by_id.get(direction_id)
        if d is None:
            entry: dict[str, Any] = {
                "direction_id": direction_id,
                "role": role,
                "status": "missing",
            }
            if rationale:
                entry["rationale"] = rationale
            if role == "retired" and direction_id in retired_final_metrics:
                entry["final_metrics"] = retired_final_metrics[direction_id]
            return entry
        history = d.get("research_history") or []
        agg = d.get("history_aggregate") or {}
        cycles_active = len(history) + (agg.get("total_cycles") or 0)
        prs_merged = sum(h.get("prs_merged", 0) for h in history) + (
            agg.get("total_prs_merged") or 0
        )
        is_paused = d.get("status") == "paused"
        auto_paused = bool(d.get("auto_paused"))
        entry = {
            "direction_id": direction_id,
            "role": role,
            "status": d.get("status"),
            "text": (d.get("text") or "")[:200],
            "cycles_active": cycles_active,
            "prs_merged": prs_merged,
            "quality_score": round(_compute_direction_quality_score(d), 3),
            "auto_paused": auto_paused,
            "user_paused": is_paused and not auto_paused,
        }
        if rationale:
            entry["rationale"] = rationale
        if role == "created" and direction_id in created_predictions:
            prediction = created_predictions[direction_id]
            entry["prediction"] = prediction
            expected = prediction.get("similar_past_merges", 0)
            entry["calibration_delta"] = entry["prs_merged"] - expected
        if role == "retired" and direction_id in retired_final_metrics:
            entry["final_metrics"] = retired_final_metrics[direction_id]
        return entry

    created = [
        _summarize(did, "created", created_rationales.get(did))
        for did in (prev_entry.get("created_direction_ids") or [])
    ]
    retired = [
        _summarize(did, "retired", retired_rationales.get(did))
        for did in (prev_entry.get("retired_direction_ids") or [])
    ]
    result: dict[str, Any] = {
        "cycle_id": prev_entry.get("cycle_id"),
        "phase2_completed_at": prev_entry.get("phase2_completed_at"),
        "summary_text": prev_entry.get("summary_text"),
        "created": created,
        "retired": retired,
    }
    cycle_reasoning = prev_entry.get("cycle_reasoning")
    if cycle_reasoning:
        result["cycle_reasoning"] = cycle_reasoning
    return result


def _build_past_retirement_critiques(
    cycle_history: list, n: int = 5
) -> list[dict[str, Any]]:
    """Collect the most recent N retirement critiques from cycle history.

    Each element is a critique object persisted by ``set_retirement_critiques``
    (fields: direction_id, what_it_addressed, pattern_that_produced_it, lesson)
    or the sentinel ``{"no_eligible_retirements": true}``. Returns [] when no
    cycle has recorded retirement_critiques yet.
    """
    results: list[dict[str, Any]] = []
    for entry in cycle_history:
        if entry.get("outcome") != "completed":
            continue
        critiques = entry.get("retirement_critiques")
        if critiques is None:
            continue
        if isinstance(critiques, dict) and critiques.get("no_eligible_retirements"):
            results.append({"no_eligible_retirements": True})
        elif isinstance(critiques, list):
            results.extend(critiques)
        if len(results) >= n:
            break
    return results[:n]


def _build_anomaly_summary(project: Project) -> dict[str, Any] | None:
    """Summarize high-occurrence persistent anomalies for the strategic agent.

    Surfaces entries with occurrence_count >= 3, splitting cross-direction
    fingerprints (which span multiple directions) from other recurring
    anomalies. Returns None when nothing qualifies so the key is omitted.
    """
    proj_anomalies = _shared_state.persistent_anomalies.get(str(project.id)) or {}
    if not proj_anomalies:
        return None

    cross_direction: list[dict[str, Any]] = []
    recurring: list[dict[str, Any]] = []
    for key, entry in proj_anomalies.items():
        # Skip non-dict sibling values (e.g. the "signal_last_computed_at"
        # ISO-string sentinel written by anomaly_detection._compute_research_anomalies
        # for the staleness watchdog). Every other consumer of persistent_anomalies
        # guards the same way; this one was the lone exception and crashed here.
        if not isinstance(entry, dict):
            continue
        occurrences = entry.get("occurrence_count") or 0
        if occurrences < 3:
            continue
        item: dict[str, Any] = {
            "key": key,
            "occurrence_count": occurrences,
            "severity": entry.get("severity"),
            "first_seen": entry.get("first_seen"),
            "last_seen": entry.get("last_seen"),
        }
        if key.startswith("CrossDirectionFingerprint_"):
            item["fingerprint"] = entry.get("fingerprint")
            item["affected_directions"] = list(entry.get("affected_directions") or [])
            if entry.get("failure_count"):
                item["failure_count"] = entry["failure_count"]
            cross_direction.append(item)
        else:
            if entry.get("affected_directions"):
                item["affected_directions"] = list(entry["affected_directions"])
            if entry.get("direction_id"):
                item["direction_id"] = entry["direction_id"]
            recurring.append(item)

    if not cross_direction and not recurring:
        return None

    cross_direction.sort(key=lambda e: e.get("occurrence_count") or 0, reverse=True)
    recurring.sort(key=lambda e: e.get("occurrence_count") or 0, reverse=True)
    return {
        "cross_direction_fingerprints": cross_direction[:10],
        "recurring_anomalies": recurring[:10],
    }


_STRATEGIC_PLAYBOOK_SOURCES = {
    "auto-generated",  # recurring failure patterns
    "review-extracted",  # review feedback patterns
    "first-attempt-pattern",  # direction-level rejection patterns
}
_STRATEGIC_PLAYBOOK_KEYWORDS = (
    "direction",
    "auto-pause",
    "auto_pause",
    "auto paused",
    "recurring",
    "repeated failure",
    "architecture",
)


def _build_playbook_summary(project: Project) -> dict[str, Any] | None:
    """Summarize playbook entries most relevant to strategic decisions.

    Pulls entries from project.design_doc whose key starts with ``playbook/``
    and either come from a strategy-relevant source (failure patterns, review
    patterns, first-attempt rejection patterns) or whose content mentions
    directions, auto-pause, or recurring failures. Returns the 10 most recently
    updated, with content truncated for prompt budget. Returns None when
    nothing qualifies so the key can be omitted from the payload.
    """
    doc = project.design_doc or {}
    candidates: list[dict[str, Any]] = []
    for key, entry in doc.items():
        if not key.startswith("playbook/") or not isinstance(entry, dict):
            continue
        # Skip pending entries (mid-flight writes from non-completed tasks).
        if entry.get("pending_source"):
            continue
        source = entry.get("source") or ""
        content = entry.get("content") or ""
        content_lower = content.lower()
        relevant = source in _STRATEGIC_PLAYBOOK_SOURCES or any(
            kw in content_lower for kw in _STRATEGIC_PLAYBOOK_KEYWORDS
        )
        if not relevant:
            continue
        item: dict[str, Any] = {
            "key": key[len("playbook/") :],
            "content": content[:400],
            "source": source or None,
            "updated_at": entry.get("updated_at"),
        }
        for opt in (
            "hit_count",
            "confirmed_count",
            "scope",
            "learned_from_direction",
            "rejection_count",
        ):
            if entry.get(opt) is not None:
                item[opt] = entry[opt]
        candidates.append(item)
    if not candidates:
        return None
    candidates.sort(key=lambda e: e.get("updated_at") or "", reverse=True)
    return {"entries": candidates[:10]}


def _build_playbook_keys_index(project: Project) -> dict[str, Any] | None:
    """Complete index of all playbook entry keys with minimal metadata.

    Lists ALL playbook entries (not just the top-N strategically relevant ones),
    with just their keys and minimal metadata (source, updated_at). This compact
    index survives budget pressure better than the full playbook_summary and lets
    the strategist discover what knowledge exists in the playbook, then fetch
    individual entries on demand via `./fetch playbook <key>`.

    Returns None when no playbook entries exist so the key can be omitted.
    """
    doc = project.design_doc or {}
    entries: list[dict[str, Any]] = []
    for key, entry in doc.items():
        if not key.startswith("playbook/") or not isinstance(entry, dict):
            continue
        # Skip pending entries (mid-flight writes from non-completed tasks).
        if entry.get("pending_source"):
            continue
        entries.append(
            {
                "key": key[len("playbook/") :],
                "source": entry.get("source"),
                "updated_at": entry.get("updated_at"),
            }
        )
    if not entries:
        return None
    # Sorted newest-first so the strategist sees recent changes first
    entries.sort(key=lambda e: e.get("updated_at") or "", reverse=True)
    return {"entries": entries}


_IN_FLIGHT_TASK_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
)
"""Statuses treated as live in-flight work for strategic pipeline summaries."""


async def _find_last_strategic_cycle_time(
    project: Project, session: AsyncSession
) -> datetime | None:
    """Return the updated_at of the most recent completed strategic task
    (strategic_review or design_doc_update) for this project, or None.

    Both task types constitute strategic-engine activity: design_doc_update is
    Phase 1 and strategic_review is Phase 2. Including both makes the
    since_last_strategic window accurate when Phase 1 just completed but
    Phase 2 hasn't run yet — otherwise the window would anchor to the prior
    Phase 2 completion and make recent_interventions look artificially stale.
    This also keeps the definition consistent with the stall detector in
    pipeline_regression_dispatcher, which treats both types as cycle activity.
    """
    result = await session.execute(
        select(Task)
        .where(
            Task.project_id == project.id,
            Task.status == TaskStatus.COMPLETED,
            Task.payloads[0]["type"].as_string().in_(sorted(STRATEGIC_TASK_TYPES)),
        )
        .order_by(Task.updated_at.desc())
        .limit(1)
    )
    task = result.scalars().first()
    if not task:
        return None
    return task.updated_at or task.created_at


async def _spawn_design_doc_update(project: Project, session: AsyncSession) -> bool:
    """Spawn a design doc update task (Phase 1 of strategic cycle).

    This runs as a separate task type so it doesn't block tactical research.
    On completion, it chains to _spawn_strategic_review (Phase 2).
    Returns True if a task was created, False if skipped.
    """
    from app.task_lifecycle import _has_active_strategic_task

    # Guard: no design_doc_update or strategic_review already running
    if await _has_active_strategic_task(project.id, session):
        logger.warning(
            f"[STRATEGIC] Skipping design doc update — strategic task already active for {project.id}"
        )
        return False

    doc_model = (
        _runtime_config.get("task_type_models", {}).get("design_doc_update")
        or _runtime_config.get("task_type_models", {}).get("research")
        or None
    )
    doc_effort = (
        _runtime_config.get("task_type_efforts", {}).get("design_doc_update")
        or _runtime_config.get("task_type_efforts", {}).get("research")
        or None
    )

    # Fetch open messages for the design doc agent via the shared authoritative
    # feed (status-filtered query, size-bounded, BUG_REPORT/BLOCKER-prioritized)
    # so this path and the strategic_review path stay unified.
    open_msgs = await _fetch_open_messages(session, project.id)

    # Build shipped_changes_manifest: tasks merged since the last arch review
    shipped_changes_manifest = await _build_shipped_changes_manifest(project, session)

    verification_kwargs = await resolve_branch_verification_context(session, project)

    # Ground the design doc against the real integration branch before shipping
    # it: present-but-absent systems/capabilities are downgraded so the agent
    # never sees a status the backing code can't support. Read-only/advisory —
    # the stored design_doc is untouched; reconcile operates on the agent view.
    # The grounded doc is then reduced to a compact, bounded navigation index
    # (key + bounded summary + reconciled presence status/modules/warning); the
    # full per-section content stays fetchable on demand via the design-doc
    # op:get endpoint so the field can no longer dominate the payload budget.
    reconciled_design_doc = await ground_design_doc(
        _agent_view_design_doc(project.design_doc), **verification_kwargs
    )

    # Compact, body-omitted index of existing knowledge documents (web_research /
    # design output) so the strategist knows what knowledge already exists and
    # can fetch full bodies on demand — included only when documents exist.
    _knowledge_index = await build_knowledge_index(session, project.id)

    payload: dict = pack_under_budget(
        {
            "type": "design_doc_update",
            "design_doc": _design_doc_index(reconciled_design_doc),
            **({"knowledge_index": _knowledge_index} if _knowledge_index else {}),
            "proposed_changes": (project.design_doc or {}).get("proposed_changes", []),
            "project_name": project.name,
            "vision": project.vision or {},
            "problem_model": await _verify_capability_backing_paths(
                _agent_view_problem_model(project.problem_model),
                **verification_kwargs,
            ),
            # Soft size/health signal from the DURABLE pre-trim model so counts
            # are honest (the agent view above is trimmed). Fail-open: omitted
            # when the durable model fails strict validation.
            **(
                {"model_health": _dd_model_health}
                if (_dd_model_health := _compute_model_health(project.problem_model))
                else {}
            ),
            "open_messages": open_msgs,
            "shipped_changes_manifest": shipped_changes_manifest,
            "base_branch": get_project_integration_branch(project),
            **({"model": doc_model} if doc_model else {}),
            **({"effort": doc_effort} if doc_effort else {}),
        },
        _DESIGN_DOC_UPDATE_SCHEMA,
    )
    payload = validate_internal_payload(
        payload, "autonomous_research:design_doc_update"
    )
    # Audit W2: cycle history is a real table now (StrategicCycle); the
    # 10-entry sliding-window cap in vision JSONB is gone. Snapshot the
    # vision at cycle start so the 3-way merge in strategic_completion can
    # compare user edits against the same baseline the agent was given.
    cycle_id = strategic_cycle.new_cycle_id()
    # Capture the operator-origin steering signal at spawn time: the pending
    # refresh marker is still armed here (the autonomous loop's spawn-record
    # consumes it AFTER this returns), but it will be gone by the time Phase 2
    # (_spawn_strategic_review) chains off this task's completion. Persist the
    # flag on the task context so the Phase 2 chain can re-surface the operator
    # steering directive rather than silently dropping it on architecture cycles.
    operator_steered = strategic_refresh_origin(project.vision) == ORIGIN_OPERATOR
    task_context: dict = {"strategic_cycle_id": cycle_id}
    if operator_steered:
        task_context["operator_steered"] = True
        # Persist the operator steering excerpt alongside the flag: the marker is
        # consumed when this Phase 1 spawn is recorded, so Phase 2 must carry the
        # text forward (the live thread snapshot may age it out before then).
        steering_excerpt = strategic_refresh_excerpt(project.vision)
        if steering_excerpt:
            task_context["steering_excerpt"] = steering_excerpt
    new_task = Task(
        project_id=project.id,
        title=f"Architecture review: {project.name}",
        description="Update project design document",
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context=task_context,
        strategic_cycle_id=cycle_id,
    )
    session.add(new_task)
    await session.flush()  # assign new_task.id for the FK below
    history = TaskHistory(
        task_id=new_task.id,
        action="SPAWNED",
        details={"spawned_by": "STRATEGIC_CYCLE", "phase": "design_doc_update"},
    )
    session.add(history)
    await strategic_cycle.create(
        session,
        project_id=project.id,
        cycle_id=cycle_id,
        phase1_task_id=new_task.id,
        baseline_vision=project.vision or {},
        baseline_problem_model=project.problem_model or {},
        baseline_running_update=project.running_update,
    )
    await session.commit()
    # Post-commit: signal the admin UI that a strategic cycle (Phase 1) started.
    from app.strategic_events import emit_strategic_cycle_started

    await emit_strategic_cycle_started(project.id, new_task.id, 1)
    logger.info(
        f"[STRATEGIC] Spawned design_doc_update task {new_task.id} for project {project.id} (cycle {cycle_id})"
    )
    return True


async def _resolve_candidate_currency(
    session: AsyncSession,
    project: Project,
    last_qual: dict,
    integration_branch: str,
) -> dict | None:
    """Best-effort check: is a qualification's candidate commit still HEAD?

    The qualifier scores its audit findings against a pinned ``candidate_sha``
    snapshot, but the integration branch advances continuously and fixes for
    those very findings often land afterward.  Resolve the live integration
    branch HEAD and compare it to the qualification's candidate commit.

    Returns ``{"current": bool, "candidate_sha": str, "head_sha": str}`` when
    the comparison resolves, or ``None`` when it is indeterminate (no candidate
    SHA on the record, GitHub unreachable, or the branch lookup failed).  Never
    raises — a failure to resolve must never block the strategic payload build.
    """
    candidate_sha = last_qual.get("candidate_sha")
    if not candidate_sha or not isinstance(candidate_sha, str):
        return None
    head_sha: str | None = None
    try:
        from app import github as _github
        from app.release_qualifier import _resolve_github_with_session

        _resolved = await _resolve_github_with_session(session, project)
        if _resolved is None:
            return None
        _gh_token, _gh_owner, _gh_repo = _resolved
        head_sha = await _github.get_branch_sha(
            _gh_token, _gh_owner, _gh_repo, integration_branch
        )
    except Exception:
        logger.exception(
            "[STRATEGIC] could not resolve integration branch HEAD for "
            "candidate-currency check"
        )
        return None
    if not head_sha or not isinstance(head_sha, str):
        return None
    return {
        "current": candidate_sha == head_sha,
        "candidate_sha": candidate_sha,
        "head_sha": head_sha,
    }


async def _spawn_strategic_review(
    project: Project,
    session: AsyncSession,
    cycle_id: str | None = None,
    operator_steered: bool = False,
    steering_excerpt: str | None = None,
) -> bool:
    """Spawn Phase 2 of the strategic cycle: vision + direction review.

    Called by _handle_design_doc_completion after the design doc is updated.
    Uses type 'strategic_review' so it doesn't block tactical research.
    Returns True if a task was created, False if skipped.

    ``operator_steered`` carries the operator-origin signal forward when this
    spawn is the Phase 2 tail of an architecture cycle. On that path the
    pending refresh marker has already been consumed by the autonomous loop's
    spawn-record (it cleared once for the Phase 1 design_doc spawn), so
    ``strategic_refresh_origin(project.vision)`` reads ``None`` here and the
    steering directive would otherwise be dropped (~1/3 of operator wakes land
    on an architecture cycle). The caller threads the flag through the
    design_doc task context so the directive still reaches the strategist.

    ``steering_excerpt`` is the operator's message text, threaded forward on the
    architecture-cycle Phase 2 path (where the marker — and its persisted
    excerpt — was already consumed at Phase 1 spawn). On the strategy-only path
    it is ``None`` and the excerpt is read directly from the still-armed marker.
    Either way it feeds ``_build_operator_steering_directive`` as the durable
    fallback so the directive survives the operator message aging out of the
    volatile conversation-thread snapshot.
    """
    from app.task_lifecycle import _has_active_strategic_task

    # Guard: no design_doc_update or strategic_review already running
    if await _has_active_strategic_task(project.id, session):
        logger.warning(
            f"[STRATEGIC] Skipping strategic review — strategic task already active for {project.id}"
        )
        return False

    # Build direction stats with rich data. Include `active` + `paused` +
    # `proposed`: paused entries are a strong user signal the strategic agent
    # should respect rather than re-propose; proposed entries exist so the
    # strategic agent can approve/reject them in the same cycle.
    included_directions = [
        d
        for d in (project.directions or [])
        if d.get("status") in ("active", "paused", "proposed")
    ]
    direction_stats = []
    for d in included_directions:
        history = d.get("research_history", [])
        agg = d.get("history_aggregate", {})
        total_cycles = len(history) + (agg.get("total_cycles") or 0)
        total_merged = sum(h.get("prs_merged", 0) for h in history) + (
            agg.get("total_prs_merged") or 0
        )
        total_rejected = sum(h.get("prs_rejected", 0) for h in history) + (
            agg.get("total_prs_rejected") or 0
        )
        total_failed = sum(h.get("tasks_failed", 0) for h in history) + (
            agg.get("total_tasks_failed") or 0
        )
        total_failed_infra = sum(h.get("tasks_failed_infra", 0) for h in history) + (
            agg.get("total_tasks_failed_infra") or 0
        )
        # Autonomy lifetime totals — legacy fallback treats prs_merged as autonomous
        # (same interpretation as admin_digest.py and direction_metrics.py).
        lifetime_autonomous = sum(
            h.get("autonomous_prs_merged", h.get("prs_merged", 0)) for h in history
        ) + (agg.get("total_autonomous_prs_merged") or 0)
        lifetime_op_actions = sum(
            h.get("operator_action_count", 0) for h in history
        ) + (agg.get("total_operator_actions") or 0)
        retirement_eligible = lifetime_autonomous >= 10 and lifetime_op_actions == 0
        taxonomy_totals: dict[str, int] = {}
        for h in history:
            for tkey, tval in (h.get("taxonomy_summary") or {}).items():
                if tval:
                    taxonomy_totals[tkey] = taxonomy_totals.get(tkey, 0) + tval
        recent_history = history[-5:]
        recent_findings = []
        recent_failures = []
        _agg_grade: dict = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        _agg_basis: dict = {
            "code_verified": 0,
            "path_verified": 0,
            "pattern_inferred": 0,
            "deduced": 0,
        }
        for h in recent_history:
            for desc in h.get("key_finding_descriptions") or []:
                if desc and desc not in recent_findings:
                    recent_findings.append(desc)
            recent_failures.extend(
                fd for fd in (h.get("failed_task_details") or []) if fd
            )
            for _gk, _gv in (h.get("grade_distribution") or {}).items():
                if _gk in _agg_grade:
                    _agg_grade[_gk] = _agg_grade.get(_gk, 0) + int(_gv or 0)
            for _bk, _bv in (h.get("verification_basis_distribution") or {}).items():
                if _bk in _agg_basis:
                    _agg_basis[_bk] = _agg_basis.get(_bk, 0) + int(_bv or 0)
        total_prs = total_merged + total_rejected
        operator_rejection_rate = (
            round(total_rejected / total_prs, 2) if total_prs > 0 else None
        )
        stat_entry: dict = {
            "id": d.get("id"),
            "text": d.get("text"),
            "priority": d.get("priority"),
            "status": d.get("status"),
            "total_cycles": total_cycles,
            "prs_merged": total_merged,
            "tasks_failed": total_failed,
            "possibly_saturated": d.get("possibly_saturated", False),
        }
        # Surface the direction→gap linkage so the strategic agent can reconcile
        # active directions against problem-model gaps (which gaps have a working
        # direction, which directions trace to no gap) without cross-referencing.
        if d.get("gap_id"):
            stat_entry["gap_id"] = str(d["gap_id"])
        if total_failed_infra > 0:
            stat_entry["tasks_failed_infra"] = total_failed_infra
        if taxonomy_totals:
            stat_entry["taxonomy_summary"] = taxonomy_totals
        stat_entry["quality_score"] = round(_compute_direction_quality_score(d), 3)
        stat_entry["lifetime_autonomous_prs_merged"] = lifetime_autonomous
        stat_entry["lifetime_operator_actions"] = lifetime_op_actions
        stat_entry["retirement_eligible"] = retirement_eligible
        if total_rejected > 0:
            stat_entry["prs_rejected"] = total_rejected
        if operator_rejection_rate is not None and total_rejected > 0:
            stat_entry["operator_rejection_rate"] = operator_rejection_rate
        from app.routers.admin_analytics import ON_DEMAND_RATE
        from app.strategic_analysis import _entry_cost_usd

        estimated_cost_usd = round(
            sum(_entry_cost_usd(h, ON_DEMAND_RATE) for h in history), 2
        )
        if estimated_cost_usd > 0:
            stat_entry["estimated_cost_usd"] = estimated_cost_usd
            if total_merged > 0:
                stat_entry["cost_per_merged_pr"] = round(
                    estimated_cost_usd / total_merged, 2
                )
        ctx = d.get("research_context")
        if ctx:
            stat_entry["research_context"] = ctx[:3000]
        # Surface the direction's stated outcome goal so the strategic agent's
        # per-cycle review is grounded in success_evidence/retirement_condition,
        # not just merge/cost metrics. Mirror the write-side cap from main.py.
        _DIR_FIELD_MAX = 500
        dir_context = d.get("context") or {}
        _success_evidence = str(dir_context.get("success_evidence") or "").strip()
        if _success_evidence:
            stat_entry["success_evidence"] = _success_evidence[:_DIR_FIELD_MAX]
        _retirement_condition = str(
            dir_context.get("retirement_condition") or ""
        ).strip()
        if _retirement_condition:
            stat_entry["retirement_condition"] = _retirement_condition[:_DIR_FIELD_MAX]
        # Advisory outcome-less-scaffolding flag: surfaces a long-running direction
        # that keeps merging PRs but never advances its success_evidence, so the
        # strategic agent sees it as a first-class signal (not just merge/cost metrics).
        if d.get("outcome_scaffolding_flagged"):
            stat_entry["outcome_scaffolding_flagged"] = True
            _osc_ev = d.get("outcome_scaffolding_evidence")
            if _osc_ev:
                stat_entry["outcome_scaffolding_evidence"] = _osc_ev
        _rfe = d.get("recent_findings_evidence") or []
        if _rfe:
            from app.research_context import filter_direction_evidence

            _filtered_ev = filter_direction_evidence(ctx, _rfe)
            if _filtered_ev:
                stat_entry["recent_findings_evidence"] = _filtered_ev
        if recent_findings:
            stat_entry["recent_findings"] = recent_findings[:10]
        if any(_agg_grade.values()):
            stat_entry["recent_findings_grade_distribution"] = _agg_grade
        if any(_agg_basis.values()):
            stat_entry["recent_findings_verification_basis"] = _agg_basis
        if recent_failures:
            stat_entry["recent_failures"] = recent_failures[:5]
        # Surface cumulative confirmed-finding count and trend from
        # outcome_progress_history (eviction-robust signal used by scaffolding
        # detector). Shared helper keeps thresholds in sync with strategic_analysis.
        from app.strategic_completion import outcome_progress_summary_fields

        stat_entry.update(outcome_progress_summary_fields(d))
        if d.get("system_id"):
            stat_entry["system_id"] = d["system_id"]
        if d.get("status") == "paused":
            stat_entry["auto_paused"] = bool(d.get("auto_paused"))
            if d.get("paused_reason"):
                stat_entry["paused_reason"] = d.get("paused_reason")
        elif d.get("status") == "proposed":
            if d.get("proposed_by"):
                stat_entry["proposed_by"] = str(d["proposed_by"])
            if d.get("proposal_rationale"):
                stat_entry["proposal_rationale"] = d["proposal_rationale"]
            if d.get("proposed_at"):
                stat_entry["proposed_at"] = d["proposed_at"]
        # Surface non-default recommended_mode so the strategic agent can bias
        # its direction decisions (audit/fundamentals → prefer non-feature work;
        # pause → tactical spawning should be skipped by autonomous_research).
        # 'ship' and unset are equivalent (baseline behavior) so they're omitted.
        _rec_mode = d.get("recommended_mode")
        if _rec_mode and _rec_mode != "ship":
            stat_entry["recommended_mode"] = _rec_mode
        direction_stats.append(stat_entry)

    mode_hint_lines = []
    for d in included_directions:
        mode = normalized_recommended_mode(d)
        hint = mode_prompt_hint(mode)
        if not hint and mode != "pause":
            continue
        direction_label = f"{d.get('id')}: {(d.get('text') or '')[:120]}".strip()
        if mode == "pause":
            hint = (
                "Mode hint: this direction is in pause mode. do not propose new feature "
                "work for this direction in this cycle; defer new findings against it."
            )
        mode_hint_lines.append(f"- {direction_label} — {hint}")
    prompt_suffix = (
        "\n".join(["Mode hints for this cycle:", *mode_hint_lines])
        if mode_hint_lines
        else None
    )

    # Recently rejected proposals (last 30 days) — surfaced so the strategic
    # agent avoids re-proposing similar work the user already declined.
    cutoff_iso = (_utcnow() - timedelta(days=30)).isoformat()
    rejected_proposals = [
        {
            "text": (d.get("text") or "")[:300],
            "rejected_at": d.get("rejected_at"),
            "rationale": (d.get("rationale") or "")[:300]
            if d.get("rationale")
            else None,
        }
        for d in (project.directions or [])
        if d.get("status") == "rejected" and (d.get("rejected_at") or "") >= cutoff_iso
    ]
    rejected_proposals.sort(key=lambda r: r.get("rejected_at") or "", reverse=True)
    rejected_proposals = rejected_proposals[:10]

    # running_update operator-override signal (last 30 days). When the
    # operator-wins reconciliation preserved an operator mid-cycle edit over the
    # agent's running_update rewrite, ``apply_reconciled_running_update`` records a
    # ``running_update_superseded`` entry in the vision rejected-edits ledger.
    # Surface a small derived signal so next cycle's thinker treats the document
    # as operator-authoritative rather than a blank slate to rewrite. Derived
    # server-side (count + most-recent ``at``) so it survives even if the full
    # vision blob is summarized away under the payload byte budget.
    _superseded_entries = [
        e
        for e in ((project.vision or {}).get(REJECTED_EDITS_KEY) or [])
        if isinstance(e, dict)
        and e.get("kind") == RUNNING_UPDATE_REJECTED_KIND
        and (e.get("at") or "") >= cutoff_iso
    ]
    running_update_superseded = (
        {
            "count": len(_superseded_entries),
            "most_recent_at": max(
                (e.get("at") or "") for e in _superseded_entries
            ),
        }
        if _superseded_entries
        else None
    )

    # Fetch unified conversation thread — recent messages chronologically,
    # regardless of status.  Gives the strategic agent a coherent back-and-forth
    # instead of flat, separated open/resolved lists. Capped at 15 because the
    # full strategic payload sits dangerously close to the 256 KB hard limit
    # (design_doc + problem_model alone are ~160 KB), and conversation_thread
    # is the most aggressively trimmable component without losing critical state.
    thread_result = await session.execute(
        select(ProjectMessage)
        .where(
            ProjectMessage.project_id == project.id,
        )
        .order_by(ProjectMessage.created_at.desc())
        .limit(15)
    )
    all_thread_msgs = list(reversed(thread_result.scalars().all()))  # chronological
    conversation_thread = [
        {
            "id": str(m.id),
            "type": m.message_type,
            "content": m.content,
            "context": m.context,
            "from": m.from_type,
            "status": m.status,
            "resolution": m.resolution,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "resolved_at": m.resolved_at.isoformat() if m.resolved_at else None,
        }
        for m in all_thread_msgs
    ]
    # The strategist's open-message inbox is its OWN authoritative status query
    # — NOT a slice of the conversation_thread above (capped at 15 by recency).
    # Deriving it from that slice meant any OPEN BUG_REPORT/BLOCKER older than
    # the 15 newest messages aged out of the strategist's view and could never
    # be resolved. _fetch_open_messages keeps them visible until fixed/resolved.
    open_msgs = await _fetch_open_messages(session, project.id)

    # When did the user last engage? Last ProjectMessage from_type='user'.
    # The strategic agent uses this to frame its running_update "since last
    # check-in" narrative at the right granularity.
    _last_user_msg = next(
        (m for m in reversed(all_thread_msgs) if m.from_type == "user"),
        None,
    )
    user_last_active_at = (
        _last_user_msg.created_at.isoformat()
        if _last_user_msg and _last_user_msg.created_at
        else None
    )

    review_model = (
        _runtime_config.get("task_type_models", {}).get("strategic_review")
        or _runtime_config.get("task_type_models", {}).get("research")
        or None
    )
    review_effort = (
        _runtime_config.get("task_type_efforts", {}).get("strategic_review")
        or _runtime_config.get("task_type_efforts", {}).get("research")
        or None
    )

    from app.strategic_analysis import (
        _build_outcome_evaluation,
        _build_retired_directions_summary,
        _compute_prediction_calibration,
        _build_retired_approaches_block,
    )

    # Audit W2: cycle history is in the StrategicCycle table; pre-fetch once
    # and pass into all the sync analysis helpers so they don't need the session.
    cycle_history = await strategic_cycle.recent_for_agent_context(
        session,
        project.id,
        limit=20,
    )
    last_strategic_at = await _find_last_strategic_cycle_time(project, session)
    _cal = _compute_prediction_calibration(project, cycle_history)
    calibration_factor = _cal["factor"]
    calibration_note = _cal["note"]
    recent_interventions = await _build_recent_interventions(
        project, session, last_strategic_at=last_strategic_at
    )
    verification_kwargs = await resolve_branch_verification_context(session, project)
    # Mode-aware additions: per-system maturity snapshots + short-window
    # claim-vs-shipped drift. Both are best-effort: empty/None when the
    # project has no SystemMaturity rows or no in-window task data.
    system_maturity_payload = await _build_system_maturity_payload(project, session)
    try:
        from app.drift_detection import compute_direction_drift, persist_drift_snapshot

        drift_raw = await compute_direction_drift(session, project)
        await persist_drift_snapshot(session, project, drift_raw)
        drift_summary = _summarize_drift_signals(drift_raw)
        if drift_summary.get("by_direction"):
            drift_summary = await _enrich_drift_with_trend(
                session, project.id, drift_summary
            )
    except Exception:
        logger.exception("[STRATEGIC] drift signal computation failed; continuing")
        drift_summary = {}
    # LTS version/phase context — active whenever the integration branch and
    # promotion target differ (i.e. the LTS split is on). Project-generic:
    # gated on the per-project branch pair, not project.is_self_repo. Best-effort.
    _open_version = None
    _version_phase_block: dict | None = None
    _integration_branch = get_project_integration_branch(project)
    _promotion_target = get_project_promotion_target(project)
    if _integration_branch != _promotion_target:
        # Deploy-model framing — present ONLY when the integration branch and the
        # promotion target differ (the LTS split). In that topology the strategic
        # model grounds on the integration branch, but only the promotion target is
        # deployed and runs live, so code merged to the integration branch is NOT
        # running until it is promoted. Without this the agent conflates "landed on
        # the integration branch" with "live" and adopts plans that are structurally
        # invalid for un-promoted code — enable-a-flag-and-measure-the-live-funnel,
        # or hard-pivot-away-on-a-live-metric-miss. Incident 2026-06-02: the entire
        # warm-pool was on automain only (0 hits on origin/main) yet g-vm-efficiency
        # planned to enable account-retarget + MEASURE the LIVE pool funnel and HARD
        # PIVOT on a miss — impossible until the version promotes. fr-staging-rollup-
        # lag already teaches "a shipped badge is not an integration-branch landing";
        # this is its sibling: "an integration-branch landing is not a deployment."
        # When the two branches are equal (no split) merges deploy directly, so the
        # framing is correctly omitted. Branch names are project-generic (never
        # hardcoded) — resolved from the per-project integration/promotion columns.
        _deploy_framing = (
            "Deployment model — read before reasoning about runtime behavior or "
            f"metrics. `{_integration_branch}` is the integration branch you ground "
            f"on; only `{_promotion_target}` is deployed and runs live. Work merged "
            f"to `{_integration_branch}` but not yet PROMOTED to "
            f"`{_promotion_target}` is NOT running yet. Every live/runtime signal — "
            "pool and VM-reuse funnels, hit rates, throughput, cost-per-merged-PR, "
            "any 'is this capability moving its metric' read — reflects ONLY "
            f"promoted+deployed `{_promotion_target}` code, never "
            f"`{_integration_branch}`-only work. Consequences for a capability "
            f"landed on `{_integration_branch}` but not promoted: (1) do NOT plan to "
            "'enable a runtime flag and measure the live funnel' — there is nothing "
            "live to measure; (2) do NOT pivot away from an approach because live "
            "metrics look flat — flat is the expected reading for un-promoted code, "
            f"not evidence the approach failed; (3) 'landed on `{_integration_branch}`' "
            "means the code EXISTS to reason about and modify, NOT that it is in "
            "effect. To make integration-branch runtime work actually testable and "
            "measurable, promote it first."
        )
        prompt_suffix = (
            f"{_deploy_framing}\n{prompt_suffix}"
            if prompt_suffix
            else _deploy_framing
        )
        try:
            from app.release_version import (
                count_merged_since,
                current_open_version,
                ensure_initial_release_version,
                recent_closed_versions,
                summarize_qualification_state,
                to_phase_block,
            )

            _open_version = await current_open_version(session, project.id)
            if _open_version is None:
                # No open version: bootstrap v1.  Resolve the integration-branch
                # HEAD SHA so the qualifier's diff is meaningful; fall back to
                # None on any error.
                _bootstrap_sha: str | None = None
                try:
                    from app import github as _github
                    from app.release_qualifier import _resolve_github_with_session

                    _resolved = await _resolve_github_with_session(session, project)
                    if _resolved is not None:
                        _gh_token, _gh_owner, _gh_repo = _resolved
                        _bootstrap_sha = await _github.get_branch_sha(
                            _gh_token, _gh_owner, _gh_repo, _integration_branch
                        )
                except Exception:
                    logger.exception(
                        "[STRATEGIC] could not resolve integration branch SHA for bootstrap"
                    )
                # Snapshot the project's current focus as the version's goal.
                _bootstrap_goal: str | None = None
                try:
                    _bootstrap_goal = (
                        parse_problem_model(project.problem_model).intent.current_focus
                        or None
                    )
                except Exception:
                    logger.exception(
                        "[STRATEGIC] could not read current_focus for version goal"
                    )
                _open_version = await ensure_initial_release_version(
                    session,
                    project.id,
                    integration_branch=_integration_branch,
                    base_sha=_bootstrap_sha,
                    goal=_bootstrap_goal,
                    promotion_target=_promotion_target,
                )
            if _open_version is not None:
                _tasks_merged = await count_merged_since(
                    session, project.id, _open_version.opened_at
                )
                _version_phase_block = {
                    **to_phase_block(_open_version),
                    "tasks_merged_this_version": _tasks_merged,
                }
            # Enrich with cross-version promotion-readiness trend.  Best-effort:
            # absent when no QualificationVerdict rows exist yet.
            try:
                from app.release_qualifier import compute_promotion_readiness

                _readiness = await compute_promotion_readiness(session, project.id)
                if _readiness.get("verdicts_evaluated", 0) > 0:
                    if _version_phase_block is None:
                        _version_phase_block = {}
                    _version_phase_block["promotion_readiness"] = _readiness
            except Exception:
                logger.exception(
                    "[STRATEGIC] promotion_readiness fetch failed; continuing"
                )
            try:
                _closed = await recent_closed_versions(
                    session, project.id, limit=_VERSION_PHASE_RECENT_VERSIONS_CAP
                )
                if _closed:
                    if _version_phase_block is None:
                        _version_phase_block = {}
                    _version_phase_block["recent_versions"] = [
                        {
                            "version_number": v.version_number,
                            "status": v.status,
                            "phase": v.phase,
                            "last_qualification": v.last_qualification,
                            "qualification_state": summarize_qualification_state(
                                v.last_qualification,
                                v.serving_status,
                                v.deploy_failure_reason,
                            ),
                            "serving_status": v.serving_status,
                            "served_sha": (v.served_sha or "")[:12] or None,
                            "deploy_failure_reason": v.deploy_failure_reason,
                        }
                        for v in _closed
                    ]
            except Exception:
                logger.exception(
                    "[STRATEGIC] recent_closed_versions fetch failed; continuing"
                )
        except Exception:
            logger.exception("[STRATEGIC] version_phase block build failed; continuing")
    # Deployment-state block — only under an LTS split (integration branch !=
    # promotion target). Grounds the deploy-model framing in concrete numbers:
    # what is actually DEPLOYED (running live on the promotion target) vs what
    # has merged to the integration branch but is NOT yet promoted, so the agent
    # never reads a flat live/runtime metric for un-promoted code as evidence an
    # approach failed (incident 2026-06-02: the warm-pool was on automain only,
    # 0 on main, yet g-vm-efficiency planned to measure its LIVE funnel). The
    # deployed-vs-promoted SHAs are surfaced separately so a promote→rollout lag
    # (or a ROLLED_BACK/HALTED status) is visible rather than conflated.
    # Best-effort — never block the payload build.
    _deployment_state_block: dict | None = None
    if _integration_branch != _promotion_target:
        try:
            from app.models import DeploymentState
            from app.release_version import (
                recent_closed_versions as _recent_closed_for_deploy,
            )

            _tasks_merged_unpromoted = (
                (_version_phase_block or {}).get("tasks_merged_this_version") or 0
            )
            # Latest promoted version (rows are newest-first; the first promoted
            # one is the most recent promotion = highest version_number).
            _promoted_v = None
            try:
                for _cv in await _recent_closed_for_deploy(
                    session, project.id, limit=_VERSION_PHASE_RECENT_VERSIONS_CAP
                ):
                    if _cv.status == "promoted":
                        _promoted_v = _cv
                        break
            except Exception:
                logger.exception(
                    "[STRATEGIC] promoted-version lookup failed; continuing"
                )
            # Deployed SHA: the orchestrator's own DeploymentState tracks the live
            # self-repo container. For a non-self-repo project it is meaningless,
            # so fall back to the latest promoted SHA (their deploy follows
            # promotion, not this orchestrator's container).
            _deployed_sha: str | None = None
            _deployed_revision: str | None = None
            _deploy_status: str | None = None
            if getattr(project, "is_self_repo", False):
                try:
                    _ds = await session.get(DeploymentState, 1)
                    if _ds is not None:
                        _deployed_sha = _ds.current_sha or None
                        _deployed_revision = _ds.current_revision or None
                        _deploy_status = _ds.status
                except Exception:
                    logger.exception(
                        "[STRATEGIC] DeploymentState read failed; continuing"
                    )
            # Real serving SHA only — NEVER a promoted_sha stand-in.  A version's
            # served_sha is set solely once deploy_health confirms the promotion
            # is actually serving (serving_status="serving_confirmed"); an
            # unconfirmed or deploy_failed promotion reports null here, so the
            # strategist never reads a merged-but-unshipped SHA as "deployed".
            if not _deployed_sha and _promoted_v is not None:
                _deployed_sha = _promoted_v.served_sha
            _undeployed_dirs = (
                (recent_interventions or {}).get("undeployed_directions") or []
            )
            # automain_ahead_of_main: reliable from DB alone (open, un-promoted
            # version carrying merged work), refined by the GitHub-verified
            # per-direction list when available.
            _open_unpromoted = (
                _open_version is not None and _open_version.status == "open"
            )
            _automain_ahead = bool(_undeployed_dirs) or (
                _open_unpromoted and _tasks_merged_unpromoted > 0
            )
            _deployment_state_block = {
                "integration_branch": _integration_branch,
                "promotion_target": _promotion_target,
                "deployed_sha": (_deployed_sha or "")[:12] or None,
                "deployed_revision": _deployed_revision,
                "deploy_status": _deploy_status,
                "promoted_version": (
                    _promoted_v.version_number if _promoted_v else None
                ),
                "promoted_sha": (
                    (_promoted_v.promoted_sha or "")[:12] or None
                    if _promoted_v
                    else None
                ),
                # Post-promotion serving outcome of the latest promotion: lets
                # the strategist see whether "promoted" actually shipped vs is
                # still unconfirmed or failed to deploy.
                "promoted_serving_status": (
                    _promoted_v.serving_status if _promoted_v else None
                ),
                "promoted_deploy_failure_reason": (
                    _promoted_v.deploy_failure_reason if _promoted_v else None
                ),
                "open_version": (
                    _open_version.version_number if _open_version else None
                ),
                "open_phase": _open_version.phase if _open_version else None,
                "open_status": _open_version.status if _open_version else None,
                "tasks_merged_unpromoted": _tasks_merged_unpromoted,
                "automain_ahead_of_main": _automain_ahead,
                **(
                    {"undeployed_directions": _undeployed_dirs}
                    if _undeployed_dirs
                    else {}
                ),
            }
            # Grounded deploy line — only when there is genuinely un-promoted
            # work. Self-contained (does not depend on the general deploy-model
            # framing) so it lands value independently: it points the agent at
            # the deployment_state block and names the un-promoted directions, so
            # a flat live metric for them is not read as failure.
            if _automain_ahead:
                _open_n = _open_version.version_number if _open_version else "?"
                _deploy_line = (
                    f"Deployment status (see `deployment_state`): v{_open_n} is open "
                    f"and NOT promoted to `{_promotion_target}`; "
                    f"{_tasks_merged_unpromoted} merged change(s) sit on "
                    f"`{_integration_branch}` only and are not running live yet."
                )
                if _undeployed_dirs:
                    _ids = ", ".join(
                        str(d.get("direction_id") or "?") for d in _undeployed_dirs[:6]
                    )
                    _deploy_line += (
                        f" Directions with undeployed runtime work: {_ids}. Their "
                        f"live/runtime metrics cannot move until v{_open_n} promotes "
                        f"— do not read a flat metric for them as evidence the "
                        f"approach failed."
                    )
                prompt_suffix = (
                    f"{prompt_suffix}\n{_deploy_line}"
                    if prompt_suffix
                    else _deploy_line
                )
        except Exception:
            logger.exception(
                "[STRATEGIC] deployment_state block build failed; continuing"
            )
    # Citable memo digest: terminal reasoning artifacts spawned from dropped
    # research findings since the previous cycle. Surfaced so the reviewer can
    # cite memos by task_id. Low priority in the pack schema — dropped first
    # under budget pressure.
    try:
        memo_citations = await strategic_cycle.query_recent_memo_citations(
            session, project.id
        )
    except Exception:
        logger.exception("[STRATEGIC] memo citation fetch failed; continuing")
        memo_citations = []
    # Efficiency axes + drift vs baseline. Best-effort: absent when no snapshot
    # data has been written yet (cold-start projects). The summarize_fn trims
    # history tails before the packer drops it entirely under budget pressure.
    _efficiency_snapshot: dict | None = None
    try:
        from app.efficiency_snapshot import read_baseline_with_drift

        _raw_snapshot = await read_baseline_with_drift(session, project.id)
        axes = (_raw_snapshot or {}).get("axes") or {}
        if any(
            v.get("current") is not None for v in axes.values() if isinstance(v, dict)
        ):
            _efficiency_snapshot = _summarize_efficiency_snapshot(_raw_snapshot)
    except Exception:
        logger.exception("[STRATEGIC] efficiency snapshot fetch failed; continuing")

    # Fleet slot utilization — a GLOBAL, real-time signal injected as an extra
    # axis into the efficiency table the strategist already renders. Unlike the
    # per-project daily axes above, it is computed fresh each cycle from the
    # shared max-plan account pool, so it ships even before any per-project
    # efficiency baseline exists. The active count is DB-derived (not the in-
    # memory active_tasks counter, which can leak and read high), so genuine
    # idle headroom is never masked.
    try:
        from app.fleet_utilization import get_fleet_utilization_snapshot

        _fleet_line = _format_fleet_utilization_axis(
            await get_fleet_utilization_snapshot(session)
        )
        if _fleet_line is not None:
            if _efficiency_snapshot is None:
                _efficiency_snapshot = {"axes": {}}
            _efficiency_snapshot.setdefault("axes", {})["fleet_utilization"] = {
                "current": _fleet_line,
                "baseline": None,
                "drift_pct": None,
            }
    except Exception:
        logger.exception("[STRATEGIC] fleet utilization snapshot failed; continuing")
    # Held-directions block — directions that hit the consecutive-failure hold
    # threshold (3 consecutive CODE_ERROR failures; see
    # shared_state.record_direction_result). Surfaces the held flag, the
    # dominant error fingerprint, and a few RAW recent failure messages so the
    # strategic agent can decide whether to refine or retire a stuck direction.
    # READ-ONLY planning context: no spawner, no enforcement, no hard block.
    # The failed-count / acceptance-rate / recent_diagnosis already reach the
    # agent via direction_outcomes, so this block adds ONLY the held flag +
    # dominant fingerprint + raw failure messages on top of those.
    _held_directions_block: list[dict] = []
    try:
        _held = _shared_state.get_held_directions()
        if _held:
            from app.anomaly_detection import _error_fingerprint

            _project_id_str = str(project.id)
            _dir_text_map = {
                d.get("id"): d
                for d in (project.directions or [])
                if d.get("id")
            }
            # One bounded query for the project's recent FAILED tasks, then
            # partition by direction_id in memory (mirrors the gathering logic
            # in _spawn_direction_hold_investigation, copied here intentionally
            # since that spawner is slated for deletion).
            _recent_failed = (
                (
                    await session.execute(
                        select(Task)
                        .where(
                            Task.project_id == project.id,
                            Task.status == TaskStatus.FAILED,
                        )
                        .order_by(Task.created_at.desc())
                        .limit(60)
                    )
                )
                .scalars()
                .all()
            )
            for _did, _hold_info in _held.items():
                if _hold_info.get("project_id") != _project_id_str:
                    continue
                _direction = _dir_text_map.get(_did)
                if not _direction:
                    continue
                _dir_failures = [
                    t
                    for t in _recent_failed
                    if (t.context or {}).get("direction_id") == _did
                ][:5]
                _failure_messages: list[str] = []
                _fingerprints: list[str] = []
                for t in _dir_failures:
                    _err = (t.latest_status_message or "(no message)")[:240]
                    _fingerprints.append(_error_fingerprint(_err))
                    _failure_messages.append(_err)
                _dominant_fp = _fingerprints[0] if _fingerprints else None
                _held_directions_block.append(
                    {
                        "direction_id": _did,
                        "direction_text": (_direction.get("text") or "?")[:300],
                        "consecutive_failures": _hold_info.get(
                            "consecutive_failures"
                        ),
                        "last_failure_class": _hold_info.get("last_failure_class"),
                        "dominant_error_fingerprint": _dominant_fp,
                        "recent_failure_messages": _failure_messages,
                    }
                )
    except Exception:
        logger.exception("[STRATEGIC] held-directions block build failed; continuing")
    # Post-promotion handoff — when a fresh version has just opened, it was
    # opened by the PROMOTION of the prior version: open_version() is only ever
    # called from _record_promotion_success and the v1 bootstrap, so a version
    # with number > 1 that has never been qualified ⇒ v{N-1} shipped to the
    # promotion target. Two things then mislead the agent into re-doing finished
    # work: (1) the `goal` field is a frozen snapshot of `current_focus` taken at
    # the promotion instant, which reliably still reads as v{N-1}'s own "get me
    # promoted" mission; and (2) v{N}'s null verdict reads as "not promoted yet".
    # The agent then plans to re-promote/re-qualify an already-live version
    # (incident 2026-06-03: a clean re-audit + research + multiple strategic
    # reviews all burned on a version already merged to main). So this hint
    # asserts the promotion POSITIVELY and forbids re-promotion, rather than only
    # flagging the carried text as stale. Fires only on the first cycles after
    # promotion (never qualified); self-clears once the first verdict lands.
    if (
        _open_version is not None
        and _open_version.version_number > 1
        and _open_version.last_qualification is None
        and (_open_version.qualification_attempts or 0) == 0
    ):
        _prior_n = _open_version.version_number - 1
        _cur_n = _open_version.version_number
        _target = _promotion_target or "main"
        # Ground the assertion in the prior version's actual promoted record
        # (promoted_at + final confidence) when readable — best-effort; the
        # positive "it promoted" framing holds regardless because a version with
        # number > 1 is only reachable via a promotion. Local import keeps this
        # resilient to back-merge refactors of release_version.
        _promoted_when = ""
        _verdict_txt = ""
        _prior_serving_status: str | None = None
        _prior_deploy_reason: str | None = None
        try:
            from app.release_version import recent_closed_versions as _rcv

            for _v in await _rcv(
                session, project.id, limit=_VERSION_PHASE_RECENT_VERSIONS_CAP
            ):
                if _v.version_number == _prior_n and _v.status == "promoted":
                    if _v.promoted_at is not None:
                        _promoted_when = f" at {_v.promoted_at.isoformat()}"
                    _conf = (_v.last_qualification or {}).get("confidence")
                    if _conf is not None:
                        _verdict_txt = f" (final qualification confidence {_conf})"
                    _prior_serving_status = _v.serving_status
                    _prior_deploy_reason = _v.deploy_failure_reason
                    break
        except Exception:
            logger.exception(
                "[STRATEGIC] post-promote prior-version lookup failed; continuing"
            )
        if _prior_serving_status == "deploy_failed":
            # The prior promotion MERGED but never shipped — do NOT advance focus
            # as if it succeeded.  This is the focus-advancement gate honoring the
            # serving signal: the strategist must treat the milestone as NOT
            # delivered and prioritize recovering the deploy.
            _reason_txt = (
                f" (reason={_prior_deploy_reason})" if _prior_deploy_reason else ""
            )
            _post_promote_hint = (
                f"Version handoff — READ THIS FIRST. v{_prior_n} MERGED to "
                f"{_target}{_promoted_when} but its deploy DID NOT confirm "
                f"serving{_reason_txt}: the promotion did NOT ship and the code "
                f"is NOT live (see `deployment_state.promoted_serving_status` = "
                f"'deploy_failed' and `version_phase.recent_versions`). Do NOT "
                f"treat v{_prior_n}'s mission as complete and do NOT advance "
                f"`intent.current_focus` to the next milestone as if it shipped. "
                f"Your TOP priority this cycle is recovering the failed deploy "
                f"(roll forward a fix / investigate why the promoted SHA never "
                f"served); only after it is serving should you move focus on."
            )
        elif _prior_serving_status == "unconfirmed":
            _post_promote_hint = (
                f"Version handoff — READ THIS FIRST. v{_prior_n} was promoted "
                f"(merged) to {_target}{_promoted_when}{_verdict_txt}, but its "
                f"deploy is NOT yet confirmed serving (see "
                f"`deployment_state.promoted_serving_status` = 'unconfirmed') — "
                f"it is still inside the post-promotion confirmation window. Do "
                f"NOT yet treat the milestone as fully shipped; the sweep will "
                f"resolve it to serving_confirmed or deploy_failed shortly. You "
                f"may begin setting v{_cur_n}'s mission, but do not declare "
                f"v{_prior_n} live until serving is confirmed, and do NOT "
                f"re-promote/re-qualify v{_prior_n}."
            )
        else:
            _post_promote_hint = (
                f"Version handoff — READ THIS FIRST. v{_prior_n} was SUCCESSFULLY "
                f"PROMOTED to {_target}{_promoted_when}{_verdict_txt} and is now "
                f"live; v{_cur_n} opened from that promotion. v{_prior_n}'s "
                f"mission is COMPLETE — do NOT plan, research, audit, or spawn "
                f"work to promote, qualify, re-audit, or re-qualify v{_prior_n}; "
                f"that is already done and merged to {_target} (cross-check "
                f"`version_phase.recent_versions` and `deployment_state` — "
                f"v{_prior_n} is recorded there with status='promoted'). The "
                f"`goal` field on v{_cur_n} is a frozen version-open snapshot "
                f"that still reads as v{_prior_n}'s promotion mission — treat it "
                f"as STALE CARRY-OVER, not a live directive, and disregard its "
                f"promote/qualify framing. A null "
                f"`version_phase.last_qualification` on v{_cur_n} means this "
                f"fresh version has not yet begun its OWN qualification (expected "
                f"for a just-opened version) — it does NOT mean v{_prior_n}'s "
                f"promotion is pending or failed. As your first action this "
                f"cycle, set v{_cur_n}'s real mission: rewrite "
                f"`intent.current_focus` to state what v{_cur_n} is for (not what "
                f"v{_prior_n} achieved), retire directions v{_prior_n} already "
                f"satisfied, and propose the directions the next milestone needs."
            )
        prompt_suffix = (
            f"{prompt_suffix}\n{_post_promote_hint}"
            if prompt_suffix
            else _post_promote_hint
        )
    # Failed-deploy recovery directive — even if the next version has started
    # qualification, keep the recovery priority visible while the latest promoted
    # version remains in deploy_failed state. This directive self-clears only when
    # a subsequent version is promoted AND confirmed serving. Fires independently
    # of the post-promotion handoff (which self-clears once the fresh version starts
    # qualifying) so failed deploys continue to drive strategic attention until
    # actually recovered.
    if (
        _open_version is not None
        and _integration_branch != _promotion_target
    ):
        _latest_promoted_version: Any | None = None
        try:
            from app.release_version import recent_closed_versions as _rcv

            for _v in await _rcv(
                session, project.id, limit=1
            ):
                if _v.status == "promoted":
                    _latest_promoted_version = _v
                    break
        except Exception:
            logger.exception(
                "[STRATEGIC] failed-deploy directive: latest promoted lookup failed; continuing"
            )
        if _latest_promoted_version is not None and _latest_promoted_version.serving_status == "deploy_failed":
            _reason_txt = (
                f" (reason={_latest_promoted_version.deploy_failure_reason})"
                if _latest_promoted_version.deploy_failure_reason
                else ""
            )
            _failed_deploy_directive = (
                f"Active deployment crisis — v{_latest_promoted_version.version_number} "
                f"MERGED to {_promotion_target} but DID NOT confirm serving{_reason_txt} "
                f"(see `deployment_state.promoted_serving_status` = 'deploy_failed'). "
                f"The promotion did NOT ship and code is NOT live. Your TOP priority "
                f"this cycle is RECOVERING the failed deploy (roll forward a fix / "
                f"investigate why the promoted SHA never served). Only after it is "
                f"serving should you move focus on. This directive remains active until "
                f"a subsequent version is promoted AND confirmed serving."
            )
            prompt_suffix = (
                f"{prompt_suffix}\n{_failed_deploy_directive}"
                if prompt_suffix
                else _failed_deploy_directive
            )
    # Qualification sub-state directive — last_qualification.status is overloaded
    # ("pending" = audit-scoring OR promotion-PR-open-and-auto-merging), which
    # made the agent conflate "wait for the audit" with "the promotion is
    # failing" and burn whole cycles waiting (incident 2026-06-02). Surface the
    # disambiguated sub-state as an explicit directive for the two states the
    # agent misreads as "still auditing, wait". Local import keeps this resilient
    # to integration-branch refactors of release_version on back-merge.
    if _open_version is not None:
        from app.release_version import (
            summarize_qualification_state as _summarize_qual_state,
        )

        _qual_state = _summarize_qual_state(_open_version.last_qualification)
        if _qual_state and _qual_state.get("state") in {
            "promotion_in_flight",
            "promotion_failed",
        }:
            _qual_hint = (
                f"Qualification sub-state: {_qual_state['state'].upper()} — "
                f"{_qual_state['summary']} Do NOT read "
                "version_phase.last_qualification.status='pending' as 'the audit "
                "is still running'; version_phase.qualification_state is the "
                "disambiguated source of truth."
            )
            prompt_suffix = (
                f"{prompt_suffix}\n{_qual_hint}" if prompt_suffix else _qual_hint
            )
    # Stabilize phase directive — when the open version is in stabilize, bias the
    # strategic agent's work-mix toward audit/cleanup/hardening so the version
    # converges toward qualification rather than accumulating net-new feature work.
    if _open_version is not None and _open_version.phase == "stabilize":
        _stabilize_hint = (
            "Version phase: STABILIZE. Bias the work-mix toward audit, cleanup, and "
            "hardening this cycle. Prefer recommended_mode='audit' or 'fundamentals' "
            "for active directions. Defer new feature directions — do not propose "
            "net-new feature work while the version is stabilizing."
        )
        # When the last qualification carried audit findings, point the agent at
        # the specific findings so it creates a TARGETED direction
        # (recommended_mode='audit') rather than doing blind generic cleanup that
        # may never touch the actual blockers.  Audit is NOT automatically the
        # binding constraint just because it flagged findings — frame it as such
        # only when audit is genuinely the limiting dimension; otherwise name the
        # real constraint (a hard blocker, or a lower sub-score) so hardening is
        # scoped at what actually blocks promotion.
        _last_qual = _open_version.last_qualification or {}
        _carried_findings = _last_qual.get("audit_findings")
        if isinstance(_carried_findings, list) and _carried_findings:
            _top = "; ".join(
                f"[{f.get('severity', '?')}] {f.get('summary', '')}"
                for f in _carried_findings[:5]
                if isinstance(f, dict)
            )
            # Determine the dimension that actually limits promotion.  A hard
            # blocker forces confidence to zero regardless of sub-scores; with no
            # blocker the binding dimension is the lowest active sub-score (the
            # smoketest dimension is excluded when the subsystem was disabled —
            # its 0.0 is a meaningless placeholder, not a real low score).
            _blocker = _last_qual.get("blocker")
            _sub_scores = _last_qual.get("sub_scores")
            _smoke_enabled = bool(_last_qual.get("smoketest_enabled", True))
            _binding_desc = "the audit sub-score is the binding constraint on promotion"
            if _blocker:
                _binding_desc = (
                    f"a hard blocker ({_blocker}) is the binding constraint on "
                    f"promotion — it forces confidence to zero regardless of the "
                    f"sub-scores, so resolving it is the priority"
                )
            elif isinstance(_sub_scores, dict):
                _active = {
                    _k: _v
                    for _k, _v in (
                        ("audit", _sub_scores.get("audit")),
                        ("test_erosion", _sub_scores.get("test_erosion")),
                        *(
                            (("smoketest", _sub_scores.get("smoketest")),)
                            if _smoke_enabled
                            else ()
                        ),
                    )
                    if isinstance(_v, (int, float)) and not isinstance(_v, bool)
                }
                if _active:
                    _binding_key = min(_active, key=lambda _k: _active[_k])
                    if _binding_key == "audit":
                        _binding_desc = (
                            "the audit sub-score is the binding constraint on "
                            "promotion"
                        )
                    else:
                        _binding_desc = (
                            f"the {_binding_key} sub-score "
                            f"({_active[_binding_key]:.2f}) is the binding "
                            f"constraint on promotion — it is lower than audit "
                            f"({_active.get('audit', 0.0):.2f}), so audit is not "
                            f"the limiting dimension even though it flagged "
                            f"findings; address {_binding_key} alongside the "
                            f"audit findings"
                        )
            # Establish whether the carried findings are still current before
            # presenting them as live promotion blockers.  The findings were
            # scored against a pinned candidate commit; if the integration
            # branch has advanced past it, later commits may already have fixed
            # them, so re-targeting research/plan/fix work directly at them
            # churns at 0% conversion.  Best-effort — None means indeterminate.
            _currency = await _resolve_candidate_currency(
                session, project, _last_qual, _integration_branch
            )
            _candidate_behind = _currency is not None and not _currency["current"]
            if _version_phase_block is not None and _currency is not None:
                _version_phase_block["last_qualification_candidate_currency"] = (
                    _currency
                )
            if _candidate_behind:
                _cand_short = _currency["candidate_sha"][:8]
                _head_short = _currency["head_sha"][:8]
                _stabilize_hint += (
                    f" The last qualification flagged {len(_carried_findings)} "
                    f"audit finding(s), but it scored them against candidate "
                    f"commit {_cand_short}, which is now BEHIND the live "
                    f"{_integration_branch} HEAD ({_head_short}). These findings "
                    f"are POSSIBLY-STALE: they were scored against a superseded "
                    f"snapshot and may already be resolved by commits that landed "
                    f"afterward — do NOT treat them as confirmed current promotion "
                    f"blockers. Before scoping brand-new hardening work directly at "
                    f"them, PREFER emitting a fresh qualify_request to re-qualify "
                    f"the current HEAD; a new audit against the current snapshot "
                    f"will report which finding(s), if any, genuinely remain. Prior "
                    f"list (against {_cand_short}) in "
                    f"version_phase.last_qualification.audit_findings. Top prior "
                    f"findings: {_top}."
                )
            else:
                _stabilize_hint += (
                    f" The last qualification's audit flagged "
                    f"{len(_carried_findings)} finding(s) — full list in "
                    f"version_phase.last_qualification.audit_findings — and "
                    f"{_binding_desc}. Top findings: {_top}."
                )
                if _currency is None:
                    _stabilize_hint += (
                        " (Could not confirm the candidate commit these findings "
                        "were scored against is still the integration-branch HEAD, "
                        "so treat their currency as provisional — if a fix may have "
                        "already landed, a fresh qualify_request will confirm what "
                        "genuinely remains.)"
                    )
            # When the carried findings are POSSIBLY-STALE, the highest-leverage
            # action is a fresh re-qualification, NOT scoping new remediation
            # directly at a superseded snapshot — so skip the create-a-direction
            # escalation below (which would churn fix work at already-resolved
            # findings).  The escalation only applies on the live/indeterminate
            # path where the findings may genuinely still block promotion.
            if not _candidate_behind:
                # Only audit/fundamentals-mode directions do remediation work; a
                # ship/pause direction that merely mentions the audit topic does
                # not dispatch fixes.  When no remediation-mode direction is
                # active, the agent must CREATE one — and we say so unambiguously.
                # The prior "create OR update a direction" phrasing let the agent
                # satisfy the instruction by appending a status note to an
                # unrelated ship-mode direction (observed 2026-05-27: v2 audit
                # dropped to 0.47 with 10 real findings, the agent added a status
                # note to a ship-mode direction and opened nothing, so the
                # findings were never dispatched and the score stayed below bar).
                _remediation_modes = {"audit", "fundamentals"}
                _has_remediation_direction = any(
                    d.get("status") == "active"
                    and d.get("recommended_mode") in _remediation_modes
                    for d in (project.directions or [])
                )
                if _has_remediation_direction:
                    _stabilize_hint += (
                        " You already have an active audit/fundamentals-mode "
                        "direction; confirm it explicitly targets THESE findings "
                        "(update its text/scope if not) so the remediation work "
                        "is actually dispatched this cycle."
                    )
                else:
                    _stabilize_hint += (
                        " There is currently NO active direction in audit or "
                        "fundamentals mode, so nothing is set up to FIX these "
                        "findings. You MUST create a new direction "
                        "(recommended_mode='audit') whose stated work is resolving "
                        "them — adding a status note to an existing ship/pause "
                        "direction does NOT count and will not move the audit "
                        "score. Creating this direction is the highest-leverage "
                        "action available this cycle."
                    )
        prompt_suffix = (
            f"{prompt_suffix}\n{_stabilize_hint}" if prompt_suffix else _stabilize_hint
        )
        # Stabilize-stall directive — when the version has been in stabilize too
        # long without progress toward qualification (zero attempts or only soft
        # near-misses), explicitly prompt the agent to emit qualify_request or
        # resolve whatever is blocking readiness.  Skips a dry-run hold (passing
        # candidate waiting for promotion_enabled) and hard-blocker stalls (covered
        # by the calcification alert).  Best-effort — never block the payload build.
        try:
            from app.release_qualifier import (
                _STABILIZE_STALL_AGE_DAYS,
                _STABILIZE_STALL_MERGED_TASKS,
            )
            from app.release_version import count_merged_since

            _stab_qual = _open_version.last_qualification or {}
            _stab_qual_status = _stab_qual.get("status")
            _stab_is_healthy = _stab_qual_status == "dry_run" or (
                _stab_qual_status == "not_promoted" and _stab_qual.get("blocker")
            )
            if not _stab_is_healthy:
                _stab_ref = _open_version.phase_changed_at or _open_version.opened_at
                _stab_age_days = (
                    max(0, (_utcnow() - _stab_ref).days) if _stab_ref else 0
                )
                # Stabilize-phase-scoped merged-task count, mirroring the
                # qualifier backstop _check_version_stabilize_stall (which counts
                # count_merged_since(..., phase_changed_at or opened_at)).  Using
                # the version-lifetime count from _version_phase_block here would
                # include build-phase merges and misreport the stall in prompt
                # text on the common age-triggered path.
                _stab_merged = (
                    await count_merged_since(session, project.id, _stab_ref)
                    if _stab_ref
                    else 0
                )
                if (
                    _stab_age_days >= _STABILIZE_STALL_AGE_DAYS
                    or _stab_merged >= _STABILIZE_STALL_MERGED_TASKS
                ):
                    _stab_stall_hint = (
                        f"Version phase: STABILIZE — STALLED. "
                        f"v{_open_version.version_number} has been in the stabilize "
                        f"phase for {_stab_age_days} day(s) with {_stab_merged} merged "
                        f"task(s) without making progress toward qualification. "
                        f"stabilize→qualify is agent-driven — consider emitting a "
                        f"qualify_request now, or identifying and resolving whatever "
                        f"is preventing qualification readiness."
                    )
                    prompt_suffix = (
                        f"{prompt_suffix}\n{_stab_stall_hint}"
                        if prompt_suffix
                        else _stab_stall_hint
                    )
                    if _version_phase_block is not None:
                        _version_phase_block["stabilize_stall"] = {
                            "age_days": _stab_age_days,
                            "tasks_merged": _stab_merged,
                        }
        except Exception:
            logger.exception(
                "[STRATEGIC] stabilize-stall hint build failed; continuing"
            )
    # Build-stall directive — when the open version is in build and has stalled
    # past the same threshold the qualifier backstop uses, explicitly tell the
    # strategic agent the version has been building for N days / M merged tasks
    # without advancing and should consider stabilizing it.  build→stabilize is
    # agent-driven with no cadence, so this nudge is the only thing prompting
    # the agent to move a parked version forward.  Best-effort — never block the
    # payload build.
    elif _open_version is not None and _open_version.phase == "build":
        try:
            from app.release_qualifier import _is_build_stalled
            from app.release_version import count_merged_since

            _build_ref = _open_version.phase_changed_at or _open_version.opened_at
            _build_age_days = max(0, (_utcnow() - _build_ref).days) if _build_ref else 0
            # Build-phase-scoped merged-task count, mirroring the qualifier
            # backstop _check_version_build_stall (which counts
            # count_merged_since(..., phase_changed_at or opened_at)).  Using
            # the version-lifetime count from _version_phase_block here would
            # include earlier build-phase and stabilize-phase merges and
            # misreport the stall for a version re-entering build from stabilize.
            _build_merged = (
                await count_merged_since(session, project.id, _build_ref)
                if _build_ref
                else 0
            )
            if _is_build_stalled(_build_age_days, _build_merged):
                _build_stall_hint = (
                    f"Version phase: BUILD — STALLED. v{_open_version.version_number} "
                    f"has been in the build phase for {_build_age_days} day(s) with "
                    f"{_build_merged} merged task(s) without advancing to stabilize. "
                    f"build→stabilize is agent-driven and has no automatic cadence — "
                    f"consider emitting a version_phase_decision to move it to "
                    f"'stabilize' so it can progress toward qualification and "
                    f"promotion."
                )
                prompt_suffix = (
                    f"{prompt_suffix}\n{_build_stall_hint}"
                    if prompt_suffix
                    else _build_stall_hint
                )
                if _version_phase_block is not None:
                    _version_phase_block["build_stall"] = {
                        "age_days": _build_age_days,
                        "tasks_merged": _build_merged,
                    }
        except Exception:
            logger.exception("[STRATEGIC] build-stall hint build failed; continuing")
    # Inject explicit list of failed approaches from retired directions so the
    # agent has a clear "do not re-propose" block in the prompt.
    _retired_summary = _build_retired_directions_summary(project)
    if _retired_summary:
        _approaches_block = _build_retired_approaches_block(_retired_summary)
        if _approaches_block:
            prompt_suffix = (
                f"{prompt_suffix}\n{_approaches_block}"
                if prompt_suffix
                else _approaches_block
            )
    # Authoritative, trim-proof direction counts computed from the FULL untrimmed
    # ``project.directions`` (NOT the budget-droppable ``directions_summary``).
    # These anchor the agent-side Direction-Health / Portfolio-Composition /
    # Current-research-directions views so their headline count, slots line, and
    # gap flags can never be starved out by the same byte-budget pressure that
    # trims or drops the per-direction detail list. Shipped as a ``critical``
    # payload key (see _STRATEGIC_REVIEW_SCHEMA) so the packer never reduces them.
    _all_directions = project.directions or []
    _active_directions_count = sum(
        1 for d in _all_directions if d.get("status") == "active"
    )
    _max_active_directions = _runtime_config.get("max_active_directions", 10)
    _direction_counts = {
        "active": _active_directions_count,
        "paused": sum(1 for d in _all_directions if d.get("status") == "paused"),
        "total": sum(
            1
            for d in _all_directions
            if d.get("status") in ("active", "paused", "proposed")
        ),
        "max_active": _max_active_directions,
    }
    # Advisory: when too few directions are active the fleet's per-direction
    # fan-out leaves most VM capacity idle. Surface that cost so the agent can
    # weigh opening more lanes for worthwhile parallel work.
    _parallelism_hint = _build_parallelism_hint(
        _active_directions_count,
        _runtime_config.get("max_concurrent_vms", 0),
        _max_active_directions,
    )
    if _parallelism_hint:
        prompt_suffix = (
            f"{prompt_suffix}\n{_parallelism_hint}"
            if prompt_suffix
            else _parallelism_hint
        )
    # If this cycle was woken by operator steering, lead the prompt_suffix with
    # a high-priority steering directive so the operator's message reaches and
    # steers the strategist instead of being diluted into conversation_thread.
    # Two ways an operator wake reaches here:
    #   - Strategy-only cycle: the operator-origin pending refresh marker is
    #     still armed (read here BEFORE autonomous_research.clear_strategic_refresh
    #     consumes it on the loop's spawn-record path).
    #   - Architecture cycle Phase 2: the marker was already consumed when the
    #     Phase 1 design_doc spawn was recorded, so it reads None now —
    #     ``operator_steered`` is threaded in from the design_doc task context
    #     to keep the directive alive across the Phase 1 → Phase 2 chain.
    # Read-only on the marker; scheduled/qualification cycles never reach this.
    if operator_steered or strategic_refresh_origin(project.vision) == ORIGIN_OPERATOR:
        # Durable fallback excerpt: the threaded Phase 2 value (architecture
        # cycle, marker already consumed) takes precedence; otherwise read it
        # from the still-armed marker (strategy-only cycle). This keeps the
        # directive alive when the operator message has aged out of the thread.
        _fallback_excerpt = steering_excerpt or strategic_refresh_excerpt(
            project.vision
        )
        _steering_directive = _build_operator_steering_directive(
            conversation_thread, fallback_excerpt=_fallback_excerpt
        )
        if _steering_directive:
            prompt_suffix = (
                f"{_steering_directive}\n{prompt_suffix}"
                if prompt_suffix
                else _steering_directive
            )
    # Compute the grounded problem_model once so it backs BOTH the payload field
    # and the cycle-start reconciliation (capabilities carry their advisory
    # ``verification`` block here). Reusing the single computed view keeps the
    # reconciliation consistent with what the agent sees in problem_model.
    _verified_problem_model = await _verify_capability_backing_paths(
        _agent_view_problem_model(project.problem_model),
        **verification_kwargs,
    )
    _cycle_start_reconciliation = _build_cycle_start_reconciliation(
        direction_stats,
        _verified_problem_model,
        (recent_interventions or {}).get("active_by_direction"),
        (recent_interventions or {}).get("per_direction_delta"),
    )
    # Soft size/health signal from the DURABLE pre-trim model so counts are honest
    # (the agent view above is trimmed). Fail-open: None when the durable model
    # fails strict validation. Hoisted above the payload so the prune-candidate
    # helper can reuse it (it only fires when the model is large/oversized).
    _sr_model_health = _compute_model_health(project.problem_model)
    # Concrete cleanup candidates, surfaced ONLY when the model is heavy — reuses
    # the grounded verification + direction linkage already computed. Advisory.
    _prune_candidates = _build_prune_candidates(
        _verified_problem_model,
        project.directions,
        _sr_model_health,
        _utcnow().isoformat(),
    )
    # Compact, body-omitted index of existing knowledge documents (web_research /
    # design output) so the strategist knows what knowledge already exists and
    # can fetch full bodies on demand — included only when documents exist.
    _knowledge_index = await build_knowledge_index(session, project.id)
    payload: dict = pack_under_budget(
        {
            "type": "strategic_review",
            "research_mode": "strategic",
            # Compact, bounded navigation index over the grounded agent-view
            # design doc (full per-section content fetched on demand via the
            # design-doc op:get endpoint) — keeps the design doc from dominating
            # the payload budget as it grows. See _design_doc_index.
            "design_doc": _design_doc_index(
                await ground_design_doc(
                    _agent_view_design_doc(project.design_doc), **verification_kwargs
                )
            ),
            "vision": project.vision or {},
            "problem_model": _verified_problem_model,
            **(
                {"cycle_start_reconciliation": _cycle_start_reconciliation}
                if _cycle_start_reconciliation
                else {}
            ),
            # Soft size/health signal from the DURABLE pre-trim model so counts
            # are honest (the agent view above is trimmed). Fail-open: omitted
            # when the durable model fails strict validation (computed above).
            **({"model_health": _sr_model_health} if _sr_model_health else {}),
            # Concrete prune candidates — present only when the model is
            # large/oversized AND something is actually prunable. Advisory list.
            **({"prune_candidates": _prune_candidates} if _prune_candidates else {}),
            **({"knowledge_index": _knowledge_index} if _knowledge_index else {}),
            "running_update": project.running_update or "",
            "running_update_acknowledged_at": (
                project.running_update_acknowledged_at.isoformat()
                if project.running_update_acknowledged_at
                else None
            ),
            "user_last_active_at": user_last_active_at,
            "self_reflection_log": (project.self_reflection_log or [])[-5:],
            "enable_self_improvement": project.enable_self_improvement,
            # Trim-proof authoritative direction counts + operator ceiling. Both
            # are ``critical`` so the agent's direction views always anchor on the
            # true active set even when ``directions_summary`` is trimmed/dropped.
            "direction_counts": _direction_counts,
            "max_active_directions": _max_active_directions,
            "directions_summary": direction_stats,
            **(
                {"staged_directions": _staged_directions}
                if (_staged_directions := _build_staged_directions_summary(project))
                else {}
            ),
            "outcome_evaluation": _build_outcome_evaluation(project, cycle_history),
            "recent_interventions": recent_interventions,
            "prediction_calibration_factor": calibration_factor,
            "prediction_calibration_note": calibration_note,
            **(
                {"previous_decision_outcomes": _prev_outcomes}
                if (
                    _prev_outcomes := _build_previous_decision_outcomes(
                        project, cycle_history
                    )
                )
                else {}
            ),
            **(
                {"anomaly_summary": _anomaly_summary}
                if (_anomaly_summary := _build_anomaly_summary(project))
                else {}
            ),
            **(
                {"playbook_summary": _playbook_summary}
                if (_playbook_summary := _build_playbook_summary(project))
                else {}
            ),
            **(
                {"playbook_keys_index": _playbook_keys_index}
                if (_playbook_keys_index := _build_playbook_keys_index(project))
                else {}
            ),
            "project_name": project.name,
            "open_messages": open_msgs,
            "conversation_thread": conversation_thread,
            **(
                {"operator_overrides": _operator_overrides}
                if (_operator_overrides := _build_operator_overrides_summary(project))
                else {}
            ),
            **(
                {"rejected_proposals": rejected_proposals} if rejected_proposals else {}
            ),
            **(
                {"running_update_superseded": running_update_superseded}
                if running_update_superseded
                else {}
            ),
            **({"prompt_suffix": prompt_suffix} if prompt_suffix else {}),
            # Mode-aware: include only when non-empty so cold-start projects
            # don't carry empty fields through the strategic payload size cap.
            **(
                {"system_maturity": system_maturity_payload}
                if system_maturity_payload
                else {}
            ),
            **({"drift_signals": drift_summary} if drift_summary else {}),
            **({"version_phase": _version_phase_block} if _version_phase_block else {}),
            **(
                {"deployment_state": _deployment_state_block}
                if _deployment_state_block
                else {}
            ),
            **({"memo_citations": memo_citations} if memo_citations else {}),
            **(
                {"past_retirement_critiques": _past_critiques}
                if (_past_critiques := _build_past_retirement_critiques(cycle_history))
                else {}
            ),
            **(
                {"retired_directions_summary": _retired_summary}
                if _retired_summary
                else {}
            ),
            **(
                {"efficiency_snapshot": _efficiency_snapshot}
                if _efficiency_snapshot
                else {}
            ),
            **(
                {"held_directions": _held_directions_block}
                if _held_directions_block
                else {}
            ),
            "base_branch": get_project_integration_branch(project),
            **({"model": review_model} if review_model else {}),
            **({"effort": review_effort} if review_effort else {}),
        },
        _STRATEGIC_REVIEW_SCHEMA,
    )
    payload = validate_internal_payload(payload, "autonomous_research:strategic_review")
    new_task = Task(
        project_id=project.id,
        title=f"Strategic review: {project.name}",
        description="Vision and direction review",
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context={"strategic_cycle_id": cycle_id} if cycle_id else {},
        strategic_cycle_id=cycle_id,
    )
    session.add(new_task)
    await session.flush()  # assign new_task.id for the cycle update below
    history = TaskHistory(
        task_id=new_task.id,
        action="SPAWNED",
        details={"spawned_by": "STRATEGIC_CYCLE", "phase": "strategic_review"},
    )
    session.add(history)
    if cycle_id:
        # Audit W2: record phase1 completion + phase2 task_id on the cycle
        # row instead of patching vision._strategic_cycle_history JSONB.
        await strategic_cycle.mark_phase1_completed(
            session,
            cycle_id,
            phase2_task_id=new_task.id,
        )
    await session.commit()
    # Post-commit: signal the admin UI that a strategic cycle (Phase 2) started.
    from app.strategic_events import emit_strategic_cycle_started

    await emit_strategic_cycle_started(project.id, new_task.id, 2)
    logger.info(
        f"[STRATEGIC] Spawned strategic_review task {new_task.id} for project {project.id}"
    )
    return True


# ---------------------------------------------------------------------------
# Spawn-time failure-feedback mitigation helper
# ---------------------------------------------------------------------------

# Minimum first-attempt rejections in a single category to trigger downscoping.
_FAILURE_DOWNSCOPE_THRESHOLD = 3
# Minimum success-pattern observations to bias scope selection toward a smaller scope.
_SUCCESS_BIAS_THRESHOLD = 3

_SCOPE_DOWNGRADE: dict[str, str] = {"large": "medium", "medium": "small"}
_SCOPE_ORDER_LIST = ["small", "medium", "large"]


def _build_failure_feedback_mitigation(
    direction: dict,
    proposed_scope: str,
    task_type: str,
    design_doc: dict | None,
) -> tuple[str, str | None, dict]:
    """Return (final_scope, coaching_note, evidence) for spawn-time mitigation.

    Reads direction_first_attempt_feedback for per-category rejection counts and
    playbook/success-* entries for successful scope shapes.

    - Downscopes by one level when any category reaches _FAILURE_DOWNSCOPE_THRESHOLD
      first-attempt rejections, and sets a coaching note naming the failure mode.
    - Biases toward the smallest scope that has _SUCCESS_BIAS_THRESHOLD+ successes
      in playbook/success-{task_type}-{scope}, applied after failure downscoping.

    Pure — no DB queries. Returns proposed_scope unchanged when no signal fires.
    """
    doc = design_doc or {}
    direction_id = str(direction.get("id") or "")
    final_scope = proposed_scope
    coaching_note: str | None = None
    evidence: dict = {"proposed_scope": proposed_scope}

    # --- Failure-concentration downscoping ---
    dfa = doc.get("direction_first_attempt_feedback") or {}
    fa_entry = dfa.get(direction_id) or {}
    cat_counts = {
        k: v
        for k, v in fa_entry.items()
        if k not in ("total", "last_updated") and isinstance(v, int) and v > 0
    }
    if cat_counts:
        top_cat = max(cat_counts, key=lambda k: cat_counts[k])
        top_count = cat_counts[top_cat]
        if (
            top_count >= _FAILURE_DOWNSCOPE_THRESHOLD
            and final_scope in _SCOPE_DOWNGRADE
        ):
            downscoped = _SCOPE_DOWNGRADE[final_scope]
            evidence["failure_downscope"] = {
                "category": top_cat,
                "count": top_count,
                "from": final_scope,
                "to": downscoped,
            }
            logger.info(
                "[SPAWN] Downscoping %s→%s for direction '%s' (first-attempt %s=%d)",
                final_scope,
                downscoped,
                direction_id,
                top_cat,
                top_count,
            )
            final_scope = downscoped
            coaching_note = (
                f"Historical pattern: {top_count} first-attempt rejections in "
                f"'{top_cat.replace('_', ' ')}' for this direction. "
                f"Pay extra attention to {top_cat.replace('_', ' ')} before submitting."
            )

    # --- Success-pattern scope bias ---
    proposed_idx = (
        _SCOPE_ORDER_LIST.index(proposed_scope)
        if proposed_scope in _SCOPE_ORDER_LIST
        else -1
    )
    if proposed_idx > 0:
        for candidate in _SCOPE_ORDER_LIST[:proposed_idx]:
            key = f"playbook/success-{task_type}-{candidate}"
            entry = doc.get(key)
            success_count = (
                entry.get("success_count") if isinstance(entry, dict) else None
            )
            if (
                isinstance(success_count, int)
                and success_count >= _SUCCESS_BIAS_THRESHOLD
            ):
                evidence["success_bias"] = {
                    "key": key,
                    "success_count": success_count,
                    "from": final_scope,
                    "to": candidate,
                }
                logger.info(
                    "[SPAWN] Success bias %s→%s for task_type='%s' ('%s': %d successes)",
                    final_scope,
                    candidate,
                    task_type,
                    key,
                    success_count,
                )
                final_scope = candidate
                break  # use smallest qualifying scope

    evidence["final_scope"] = final_scope
    return final_scope, coaching_note, evidence
