"""Agent-view problem_model builder — payload-section cluster peeled from
:mod:`app.strategic_spawning`.

Trims and byte-budget-shrinks ``Project.problem_model`` for inclusion in the
strategic-agent cycle payload: the count-based agent-view trim
(:func:`_agent_view_problem_model`), the guaranteed-small floor shrink
(:func:`_shrink_pm_to_floor`) and the packer ``summarize`` / ``floor``
callbacks. Kept a pure, dependency-light sibling so ``strategic_spawning`` can
import the whole cluster back into its namespace — every existing
``from app.strategic_spawning import ...`` call site and test keeps working.
"""

import json
import logging
import re
from typing import Any

from app.problem_model import compute_model_size_signal, parse_problem_model

logger = logging.getLogger(__name__)


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
    except Exception:  # noqa: BLE001 — advisory soft signal must degrade, never raise
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
                gap_memory_trimmed.append({"gap_id": g2.get("id"), "fields": shortened})
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

    # Auto-heal: when the persisted model has a blank/placeholder intent.purpose
    # but a populated user_purpose (the operator's durable backstop), restore
    # purpose from user_purpose before the agent sees it. This prevents the
    # strategic agent from deadlocking on a legally-persisted-but-blank purpose:
    # thinker.ts rejects placeholder purposes, so the agent would fail before
    # it could propose a fix. Healing here (at model-build time) means the agent
    # always sees a valid purpose, and the next agent write-back re-persists it
    # through _preserve_operator_intent's user_purpose fallback.
    intent = trimmed.get("intent")
    if isinstance(intent, dict):
        purpose = (intent.get("purpose") or "").strip()
        user_purpose = (intent.get("user_purpose") or "").strip()
        if (not purpose or purpose == "..." or bool(re.match(r"^<[^>]+>$", purpose))) and user_purpose:
            intent["purpose"] = user_purpose
            logger.info(
                "[STRATEGIC] Auto-healed blank intent.purpose from user_purpose "
                "in agent-view problem model"
            )

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
