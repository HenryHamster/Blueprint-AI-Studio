#!/usr/bin/env python3
"""Realised strategic-review effort-tier evidence (criterion c-a6b328b9).

Reads the pre-spawn effort tier stamped per strategic_review cycle
(``StrategicCycle.cycle_metadata['effort_tier']``, written by
``strategic_cycle.record_effort_tier`` at spawn time, before the LLM call) and
reports, from those persisted rows, the two halves of the criterion:

  1. The REALISED routine/consequential split -- actual counts and proportions
     of completed cycles at each tier over the recorded window, NOT the
     designed or intended split. ``strategic_review_effort.py`` classifies a
     cycle as ROUTINE (cheap tier, effort 'medium') or CONSEQUENTIAL (stays at
     'max'); ``strategic_spawning.py`` stamps that verdict on the cycle row.
     This module reads the stamps back and reports what actually happened.

  2. That routine-tier cycles still produced COURSE-CHANGING findings. A
     completed cycle is course-changing when its persisted outcome accounting
     (filled on phase-2 completion, ``strategic_cycle.mark_phase2_completed``)
     shows it changed the project's course:

       * ``directions_created > 0`` -- opened one or more new directions
         (a problem-model gap staffed / a new avenue opened);
       * ``directions_retired > 0`` -- retired one or more directions
         (a gap closed / a course retired);
       * ``vision_updated``         -- changed the project vision (the
         persisted version-phase / steering record).

     ``tasks_spawned`` is reported alongside but is NOT part of the
     course-changing definition: spawning follow-up work is a consequence of a
     finding, not the finding itself. The definition is deliberately the union
     of the three decision signals and is stated here so a reader can audit
     the number rather than trust it.

  3. Rows whose tier is MISSING are reported honestly rather than filtered
     silently: a completed cycle without a stamped tier is itself a finding
     (it predates ``record_effort_tier`` or bypassed the stamping path).

WINDOW AND ASSUMPTIONS
  * Window: ``--days N`` restricts to cycles whose cycle row was created within
    the last N days. The default is ALL recorded rows. The first/last row
    timestamps actually observed are printed so the window is auditable.
  * Denominator: the realised split is computed over COMPLETED cycles with a
    stamped tier -- the cycles whose outcomes are fully realised. Completed
    cycles missing a tier and non-completed cycles with a tier are reported as
    separate honest counts, never silently dropped.
  * Read-only: this script never writes to the database. The optional
    ``--write-json PATH`` writes the realised split as a durable JSON payload --
    the checked-in address the cost projection reads routine share R from
    (``backend/scripts/measured_effort_tier_split.json``). It writes a
    measurement record, not the DB.

REPLAY PATH (``--replay``): the cost projection's routine share R is UNMEASURED
when no completed cycle carries a stamped tier, and stamps cannot exist until
the effort-tier classifier ships (which the criterion itself blocks). To break
that deadlock, ``--replay`` re-runs the canonical classifier
(``app.strategic_review_effort.classify_strategic_review_effort``, lazy-imported
here so the module stays stdlib-only at import time) on signals reconstructed
from PERSISTED StrategicCycle data:

  * completed cycles with a stamped ``cycle_metadata['effort_tier']`` use the
    stamp as-is;
  * completed cycles WITHOUT a stamp are re-classified by reconstructing the
    pre-spawn signals -- ``integrity_conflict_counts`` from the cycle's own
    stamp, ``prior_cycle_dirty`` from the immediately-preceding completed
    cycle's counts, ``recent_cycle_failures`` from any failed/stalled outcome
    among the 3 prior cycles, ``open_message_count`` from the phase2 task
    payload, ``version_phase_block`` from the phase2 payload's ``version_phase``
    key (the block the cadence spawn packs there), and ``operator_steered``
    from the phase1 design-doc task context (the ONLY place the spawn path
    persists it) -- with every signal a ROUTINE verdict needs that is genuinely
    unavailable (most load-bearing: ``open_message_count``; ``operator_steered``
    for strategy-only cycles with no phase1 task) resolving to CONSEQUENTIAL.

FAIL-CLOSED LOWER BOUND: the replayed routine share is deliberately a LOWER
BOUND on what the classifier would have produced with full signals, never a
measured realized split. The emitted payload is labelled
``measurement_status: 'replayed'`` / ``bound_kind: 'fail_closed_lower_bound'``
and carries a ``signal_resolution`` map so a reader can audit which signals
were reconstructed-from-payload, derived-from-history, defaulted-fail-closed,
or defaulted-not-a-signal. A signal a ROUTINE verdict needs that is genuinely
unavailable resolves to CONSEQUENTIAL -- never to the ROUTINE-friendly
default -- so the replayed share is a strict lower bound. The one deliberate
``defaulted-not-a-signal`` exception is ``version_phase_block`` ABSENT: the
strategic payload schema gives ``version_phase`` a floor callback, so a
non-empty block always ships in the packed payload; a missing ``version_phase``
key means the classifier saw ``version_phase=None`` at spawn, which it
explicitly treats as not-a-consequential-signal.

Convention: this module is stdlib-only at import time (no ``app.*`` import at
module level) so the aggregation functions load and can be tested without a
database -- the same convention as the accepted evidence precedent
``backend/scripts/strategic_cycle_cost_projection.py``. The DB read lives in
``main()`` behind a lazy import.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

# Mirrors app/models.py CYCLE_META_EFFORT_TIER / CYCLE_META_EFFORT_REASON
# (kept local so the module loads without the app package).
CYCLE_META_EFFORT_TIER = "effort_tier"
CYCLE_META_EFFORT_REASON = "effort_tier_reason"

# Mirrors app/strategic_review_effort.py tier vocabulary plus the "pinned"
# tier strategic_spawning.py records when task_type_efforts explicitly pins
# strategic_review to a configured effort.
ROUTINE_TIER = "routine"
CONSEQUENTIAL_TIER = "consequential"
PINNED_TIER = "pinned"
_KNOWN_TIERS = (ROUTINE_TIER, CONSEQUENTIAL_TIER, PINNED_TIER)

# Mirrors app/strategic_cycle.py OUTCOME_COMPLETED.
OUTCOME_COMPLETED = "completed"

# Mirrors app/models.py CYCLE_META_INTEGRITY_CONFLICT_COUNTS (kept local so the
# module loads without the app package).
CYCLE_META_INTEGRITY_CONFLICT_COUNTS = "integrity_conflict_counts"

# Mirrors app/strategic_cycle.py integrity-conflict classes and the failed /
# stalled outcome vocabulary used to reconstruct the pre-spawn signals the
# effort classifier needs.
_INTEGRITY_CONFLICT_KEYS = (
    "orphaned_gaps",
    "ungrounded_directions",
    "stale_active_moves",
    "staffed_but_deferred",
)
_FAILED_CYCLE_OUTCOMES = frozenset(
    {
        "failed_phase1",
        "failed_phase2",
        "stalled_phase1",
        "stalled_phase2",
        "failed_phase2_spawn",
        "thinker_artifacts_missing",
    }
)


def row_tier(row: Mapping[str, Any]) -> str | None:
    """The stamped effort tier from a cycle row dict, or None when absent."""
    meta = row.get("cycle_metadata")
    if not isinstance(meta, Mapping):
        return None
    tier = meta.get(CYCLE_META_EFFORT_TIER)
    return tier if isinstance(tier, str) and tier else None


def tier_bucket(tier: str) -> str:
    """Map a tier string to its aggregation bucket.

    Known tiers map to themselves; any unrecognised string lands in ``other``
    so an unexpected stamp value is surfaced rather than silently dropped.
    """
    return tier if tier in _KNOWN_TIERS else "other"


def is_course_changing(row: Mapping[str, Any]) -> bool:
    """Whether a completed cycle's persisted outcome accounting changed course.

    Course-changing = any of the three decision signals: a direction opened
    (``directions_created``), a direction retired (``directions_retired``), or
    the vision changed (``vision_updated``). See the module docstring for the
    full definition. ``tasks_spawned`` is deliberately excluded.
    """
    return (
        int(row.get("directions_created") or 0) > 0
        or int(row.get("directions_retired") or 0) > 0
        or bool(row.get("vision_updated"))
    )


def realised_tier_split(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Realised routine/consequential split over completed cycles with a tier.

    Counts and proportions per tier over the completed cycles carrying a
    stamped tier, plus the honest accounting of rows excluded from the
    denominator:

      * ``completed_missing_tier``      -- completed cycles with NO stamp;
      * ``non_completed_with_tier``     -- rows with a stamp but not completed
        (in-flight or failed cycles -- their outcomes are not yet realised).

    A defensive ``other`` bucket counts any unrecognised tier string so an
    unexpected stamp value is surfaced, never silently dropped.
    """
    rows = list(rows)
    completed = [r for r in rows if r.get("outcome") == OUTCOME_COMPLETED]
    completed_with_tier = [r for r in completed if row_tier(r) is not None]
    completed_missing_tier = [r for r in completed if row_tier(r) is None]
    non_completed_with_tier = [
        r for r in rows if r.get("outcome") != OUTCOME_COMPLETED and row_tier(r) is not None
    ]

    counts: dict[str, int] = {}
    for r in completed_with_tier:
        bucket = tier_bucket(row_tier(r) or "")
        counts[bucket] = counts.get(bucket, 0) + 1

    total = len(completed_with_tier)
    proportions = {t: (counts[t] / total if total else 0.0) for t in counts}
    return {
        "total_completed_with_tier": total,
        "counts": counts,
        "proportions": proportions,
        "completed_missing_tier_ids": [str(r.get("id")) for r in completed_missing_tier],
        "completed_missing_tier_count": len(completed_missing_tier),
        "non_completed_with_tier_count": len(non_completed_with_tier),
    }


def course_changing_breakdown(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Course-changing findings by tier over completed cycles with a tier.

    For each tier reports how many completed cycles were course-changing and
    the rate. The claim tested is that ROUTINE cycles -- the cheapened tier --
    still produced course-changing findings at a non-zero rate.
    """
    rows = list(rows)
    completed_with_tier = [
        r for r in rows if r.get("outcome") == OUTCOME_COMPLETED and row_tier(r) is not None
    ]

    by_tier: dict[str, dict[str, Any]] = {}
    for tier in _KNOWN_TIERS + ("other",):
        tier_rows = [r for r in completed_with_tier if tier_bucket(row_tier(r) or "") == tier]
        cc = [r for r in tier_rows if is_course_changing(r)]
        by_tier[tier] = {
            "cycles": len(tier_rows),
            "course_changing": len(cc),
            "course_changing_rate": (len(cc) / len(tier_rows)) if tier_rows else 0.0,
        }

    routine = by_tier[ROUTINE_TIER]
    return {
        "by_tier": by_tier,
        "routine_course_changing_cycles": routine["course_changing"],
        "routine_course_changing_rate": routine["course_changing_rate"],
    }


def aggregate_effort_tier_evidence(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Combined realised split + course-changing findings for a row set."""
    rows = list(rows)
    return {
        "rows_considered": len(rows),
        "split": realised_tier_split(rows),
        "course_changing": course_changing_breakdown(rows),
    }


def row_to_dict(cycle: Any) -> dict[str, Any]:
    """Reduce an ORM ``StrategicCycle`` to the plain dict the aggregation reads.

    Only persisted fields are copied; nothing is derived from the LLM payload.
    Naive timestamps are passed through as ISO strings (the DB stores naive UTC
    via ``NaiveUTCDateTime``).
    """
    completed_at = getattr(cycle, "completed_at", None)
    return {
        "id": str(cycle.id),
        "project_id": str(cycle.project_id),
        "outcome": str(cycle.outcome or ""),
        "directions_created": int(getattr(cycle, "directions_created", 0) or 0),
        "directions_retired": int(getattr(cycle, "directions_retired", 0) or 0),
        "tasks_spawned": int(getattr(cycle, "tasks_spawned", 0) or 0),
        "vision_updated": bool(getattr(cycle, "vision_updated", False)),
        "cycle_metadata": dict(getattr(cycle, "cycle_metadata", None) or {}),
        "phase1_started_at": _iso(getattr(cycle, "phase1_started_at", None)),
        "phase2_started_at": _iso(getattr(cycle, "phase2_started_at", None)),
        "phase2_completed_at": _iso(getattr(cycle, "phase2_completed_at", None)),
        "created_at": _iso(getattr(cycle, "created_at", None)),
        "completed_at": _iso(completed_at),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _as_utc(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _row_timestamp(row: Mapping[str, Any]) -> datetime | None:
    for key in ("created_at", "phase1_started_at"):
        value = row.get(key)
        if isinstance(value, str) and value:
            dt = _as_utc(value)
            if dt is not None:
                return dt
    return None


def _within_window(row: Mapping[str, Any], start: datetime | None, end: datetime | None) -> bool:
    ts = _row_timestamp(row)
    if ts is None:
        return True  # untimestamped row -- keep it, and surface the boundary honestly
    if start is not None and ts < start:
        return False
    if end is not None and ts > end:
        return False
    return True


def filter_rows_by_window(
    rows: Iterable[Mapping[str, Any]],
    days: int | None,
    as_of: datetime,
) -> tuple[list[Mapping[str, Any]], datetime | None, datetime | None]:
    """Restrict rows to the last ``days`` days (None = all) and derive the
    observed window boundaries (earliest/latest row timestamp) from the kept
    rows. Returns ``(kept_rows, window_start, window_end)``.
    """
    rows = list(rows)
    start = as_of - timedelta(days=days) if days else None
    kept = [r for r in rows if _within_window(r, start, as_of)]

    observed_start: datetime | None = None
    observed_end: datetime | None = None
    for r in kept:
        ts = _row_timestamp(r)
        if ts is None:
            continue
        observed_start = ts if observed_start is None else min(observed_start, ts)
        observed_end = ts if observed_end is None else max(observed_end, ts)
    return kept, observed_start, observed_end


# ---------------------------------------------------------------------------
# Replay path (fail-closed lower bound)
# ---------------------------------------------------------------------------
# The cost projection's routine share R is UNMEASURED when no completed cycle
# carries a stamped effort tier, and stamps cannot exist until the v28
# classifier ships. The replay path re-runs the canonical classifier on signals
# reconstructed from persisted StrategicCycle data: ``open_message_count`` and
# ``version_phase_block`` from the phase2 task payload, ``operator_steered``
# from the phase1 design-doc task context, the integrity/history signals from
# cycle stamps and the prior-cycle log. A signal a ROUTINE verdict needs that
# is genuinely unavailable resolves to CONSEQUENTIAL -- the classifier's own
# fail-closed contract for ``open_message_count``, a conservative treatment of
# an unverifiable prior-cycle clean record, and operator_steered for
# strategy-only cycles with no phase1 task -- so the replayed routine share is a
# strict LOWER BOUND on the full-signals share.


def _integrity_counts_from_stamp(row: Mapping[str, Any]) -> dict[str, int] | None:
    """Reconstruct ``integrity_conflict_counts`` from the cycle's own stamp.

    Returns the four-class counts dict when the cycle carries
    ``cycle_metadata['integrity_conflict_counts']`` (written by
    ``strategic_cycle.record_integrity_conflict_counts``); None when the stamp
    is absent (a legacy cycle that predates the feature or bypassed the
    reconciliation path).
    """
    meta = row.get("cycle_metadata")
    if not isinstance(meta, Mapping):
        return None
    raw = meta.get(CYCLE_META_INTEGRITY_CONFLICT_COUNTS)
    if not isinstance(raw, Mapping):
        return None
    return {key: int(raw.get(key) or 0) for key in _INTEGRITY_CONFLICT_KEYS}


def _integrity_counts_dirty(counts: Mapping[str, Any]) -> bool:
    """Any of the four integrity-conflict classes is non-zero."""
    return any(int(counts.get(key) or 0) > 0 for key in _INTEGRITY_CONFLICT_KEYS)


def _extract_pre_spawn_signals(task_payload: Any) -> dict[str, Any]:
    """Reconstructible pre-spawn signals from a phase2 task's first payload.

    The strategic_review spawn payload packs ``open_message_count`` as a
    critical key and the version-phase block under ``version_phase``; the
    payload does NOT pack ``operator_steered`` (the spawn path passes that only
    to the classifier and persists it on the phase1 task context, recovered by
    ``_extract_operator_steered_from_phase1_context``). The remaining classifier
    signals are not persisted and are deliberately left for their
    fail-closed / not-a-signal defaults.
    """
    if not isinstance(task_payload, Mapping):
        return {}
    out: dict[str, Any] = {}
    if "open_message_count" in task_payload:
        out["open_message_count"] = task_payload.get("open_message_count")
    version_phase = task_payload.get("version_phase")
    if isinstance(version_phase, Mapping):
        out["version_phase_block"] = version_phase
    return out


def _extract_operator_steered_from_phase1_context(context: Any) -> bool | None:
    """Reconstruct ``operator_steered`` from the phase1 design-doc task context.

    ``_spawn_design_doc_update`` stamps ``context['operator_steered'] = True``
    when the wake is operator-origin (strategic_spawning.py) and leaves the key
    ABSENT otherwise, so a present context yields a definite value (absent key =
    False). Returns None when there is no context to read (a strategy-only cycle
    has no phase1 task, so the signal is genuinely unavailable and the replay
    path must fail closed rather than assume a clean value).
    """
    if not isinstance(context, Mapping):
        return None
    return bool(context.get("operator_steered", False))


def _sorted_chronological(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Order rows oldest-first per project (stable; id breaks timestamp ties)."""

    def _ts_key(row: Mapping[str, Any]) -> str:
        ts = _row_timestamp(row)
        return ts.isoformat() if ts is not None else ""

    return sorted(
        rows,
        key=lambda r: (
            str(r.get("project_id") or ""),
            _ts_key(r),
            str(r.get("id") or ""),
        ),
    )


def _prior_completed_cycle(
    row: Mapping[str, Any], prior_rows: list[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """The immediately-preceding completed cycle (chronologically before row)."""
    for prior in reversed(prior_rows):
        if prior.get("outcome") == OUTCOME_COMPLETED:
            return prior
    return None


def _replay_signals_for_cycle(
    row: Mapping[str, Any],
    prior_rows: list[Mapping[str, Any]],
    pre_spawn_signals: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Reconstruct the pre-spawn signals an unstamped completed cycle needs.

    Returns ``(signals, resolution)`` where ``signals`` is fed verbatim to
    ``classify_strategic_review_effort`` and ``resolution`` records how each
    signal was obtained -- reconstructed-from-payload /
    derived-from-history / defaulted-fail-closed / defaulted-not-a-signal --
    so a reader can audit which signals were hard.
    """
    signals: dict[str, Any] = {}
    resolution: dict[str, str] = {}

    # open_message_count -- from the enriched task payload when present;
    # otherwise left ABSENT so the classifier itself fails closed on
    # "open_message_count unknown" (the most load-bearing unavailable signal).
    if pre_spawn_signals.get("open_message_count") is not None:
        signals["open_message_count"] = pre_spawn_signals.get("open_message_count")
        resolution["open_message_count"] = "reconstructed-from-payload"
    else:
        resolution["open_message_count"] = "defaulted-fail-closed"

    # operator_steered -- reconstructed from the phase1 design-doc task context
    # (the phase2 payload never packs it; _extract_operator_steered_from_phase1_context
    # recovers a definite True/False when a phase1 task exists). ABSENT means no
    # phase1 task existed (a strategy-only cycle), so the signal is genuinely
    # unavailable -- fail closed by treating the cycle as operator-steered, never
    # assuming the ROUTINE-friendly falsy default.
    if "operator_steered" in pre_spawn_signals:
        signals["operator_steered"] = bool(pre_spawn_signals.get("operator_steered"))
        resolution["operator_steered"] = "reconstructed-from-payload"
    else:
        signals["operator_steered"] = True
        resolution["operator_steered"] = "defaulted-fail-closed"

    # version_phase_block -- the phase2 payload carries the block under the
    # ``version_phase`` key (mapped to ``version_phase_block`` by
    # ``_extract_pre_spawn_signals``). ABSENT is a genuine not-a-signal: the
    # strategic payload schema gives ``version_phase`` a floor callback, so a
    # non-empty block always ships in the packed payload -- a missing key means
    # the classifier saw version_phase=None at spawn, which it explicitly treats
    # as not-a-consequential-signal (so treating it as such cannot inflate the
    # replayed routine share above the full-signals share).
    vpb = pre_spawn_signals.get("version_phase_block")
    if isinstance(vpb, Mapping):
        signals["version_phase_block"] = vpb
        resolution["version_phase_block"] = "reconstructed-from-payload"
    else:
        resolution["version_phase_block"] = "defaulted-not-a-signal"

    # integrity_conflict_counts -- from the cycle's own stamp when present;
    # a missing stamp is treated as not-a-signal by the classifier (pinned by
    # test_strategic_completion.py::test_strategic_review_effort_classifier).
    own_counts = _integrity_counts_from_stamp(row)
    if own_counts is not None:
        signals["integrity_conflict_counts"] = own_counts
        resolution["integrity_conflict_counts"] = "reconstructed-from-payload"
    else:
        resolution["integrity_conflict_counts"] = "defaulted-not-a-signal"

    # prior_cycle_dirty -- the immediately-preceding completed cycle's counts.
    prior_completed = _prior_completed_cycle(row, prior_rows)
    if prior_completed is None:
        signals["prior_cycle_dirty"] = False
        resolution["prior_cycle_dirty"] = "derived-from-history"
    else:
        prior_counts = _integrity_counts_from_stamp(prior_completed)
        if prior_counts is None:
            # The prior completed cycle exists but its clean record is
            # unverifiable -- treat the recovery condition as present (fail
            # closed) so the replayed share can never be MORE optimistic than
            # the full-signals share.
            signals["prior_cycle_dirty"] = True
            resolution["prior_cycle_dirty"] = "defaulted-fail-closed"
        else:
            signals["prior_cycle_dirty"] = _integrity_counts_dirty(prior_counts)
            resolution["prior_cycle_dirty"] = "derived-from-history"

    # recent_cycle_failures -- any of the 3 immediately-prior cycles failed/stalled.
    recent_prior = prior_rows[-3:]
    signals["recent_cycle_failures"] = any(
        p.get("outcome") in _FAILED_CYCLE_OUTCOMES for p in recent_prior
    )
    resolution["recent_cycle_failures"] = "derived-from-history"

    return signals, resolution


def _classify_replay(signals: Mapping[str, Any]) -> tuple[str, str, str]:
    """Run the canonical effort classifier (lazy import keeps the module
    stdlib-only at import time, the same convention as ``_load_cycle_rows``)."""
    backend_root = Path(__file__).resolve().parents[1]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    from app.strategic_review_effort import classify_strategic_review_effort  # noqa: PLC0415

    return classify_strategic_review_effort(signals)


def replay_effort_tier_split(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Replayed routine/consequential split over ALL completed cycles.

    Stamp-preferred: a completed cycle carrying a stamped
    ``cycle_metadata['effort_tier']`` is honored as-is. A completed cycle
    WITHOUT a stamp is classified by re-running the canonical classifier on
    signals reconstructed from persisted data, fail-closed (an unavailable
    signal a ROUTINE verdict needs resolves to CONSEQUENTIAL). The resulting
    ``routine_share`` is therefore a LOWER BOUND on the share the classifier
    would have produced with full signals -- never a measured realized split.
    """
    ordered = _sorted_chronological(rows)
    by_project: dict[str, list[Mapping[str, Any]]] = {}
    for row in ordered:
        by_project.setdefault(str(row.get("project_id") or ""), []).append(row)

    counts: dict[str, int] = {}
    source_counts = {"stamp": 0, "replay": 0, "unresolved": 0}
    signal_resolution: dict[str, dict[str, int]] = {}

    for project_rows in by_project.values():
        prior: list[Mapping[str, Any]] = []
        for row in project_rows:
            if row.get("outcome") != OUTCOME_COMPLETED:
                prior.append(row)
                continue
            tier = row_tier(row)
            if tier is not None:
                bucket = tier_bucket(tier)
                counts[bucket] = counts.get(bucket, 0) + 1
                source_counts["stamp"] += 1
            else:
                pre = row.get("pre_spawn_signals")
                pre = pre if isinstance(pre, Mapping) else {}
                signals, resolution = _replay_signals_for_cycle(row, prior, pre)
                _effort, replay_tier, _reason = _classify_replay(signals)
                bucket = tier_bucket(replay_tier)
                counts[bucket] = counts.get(bucket, 0) + 1
                source_counts["replay"] += 1
                for signal, label in resolution.items():
                    by_label = signal_resolution.setdefault(signal, {})
                    by_label[label] = by_label.get(label, 0) + 1
            prior.append(row)

    total = sum(counts.values())
    return {
        "denominator_cycles": total,
        "counts": counts,
        "proportions": {
            tier: (counts[tier] / total if total else 0.0) for tier in counts
        },
        "routine_count": counts.get(ROUTINE_TIER, 0),
        "routine_share": round(counts.get(ROUTINE_TIER, 0) / total, 6) if total else 0.0,
        "source_counts": source_counts,
        "signal_resolution": signal_resolution,
    }


def split_to_json_payload(
    agg: Mapping[str, Any],
    window_start: datetime | None,
    window_end: datetime | None,
    days: int | None,
    as_of: datetime,
    provenance: str,
    replay: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Durable JSON payload for the effort-tier split.

    ``measurement_status`` is ``"measured"`` when at least one completed cycle
    carries a stamped tier (``routine_share`` = the realised routine proportion
    over completed tiered cycles); otherwise ``"unmeasured"`` with
    ``routine_share: null`` -- the honest state when the window has no
    completed stamped cycles. ``backend/scripts/strategic_cycle_cost_projection.py``
    reads this exact schema, so this payload IS the durable address of R.

    When ``replay`` (the result of ``replay_effort_tier_split``) is supplied,
    the top-level ``measurement_status`` becomes ``"replayed"`` -- distinct from
    ``"measured"`` -- and ``routine_share``/``counts``/``denominator_cycles`` are
    the fail-closed replayed lower bound, labelled ``bound_kind:
    'fail_closed_lower_bound'`` with a ``signal_resolution`` map. The realised
    ``split`` sub-object is preserved as the (possibly empty) measured view so a
    reader can tell the measured and replayed paths apart, and
    ``measured_denominator_cycles`` preserves the STAMP-ONLY denominator that
    view is computed over -- it is NOT overwritten by the replay denominator, so
    a reader that quotes ``split['routine']`` can always quote the basis that
    share is actually a proportion of.
    """
    split = agg["split"]
    total = int(split["total_completed_with_tier"])
    measured = total > 0
    proportions = split["proportions"]
    tier_order = (ROUTINE_TIER, CONSEQUENTIAL_TIER, PINNED_TIER, "other")

    def _round_proportion(value: Any) -> float | None:
        if value is None:
            return None
        return round(float(value), 6)

    payload = {
        "schema_version": 1,
        "measurement_status": "measured" if measured else "unmeasured",
        "routine_share": (
            _round_proportion(proportions.get(ROUTINE_TIER, 0.0)) if measured else None
        ),
        "split": {
            tier: (_round_proportion(proportions.get(tier, 0.0)) if measured else None)
            for tier in tier_order
        },
        "counts": {tier: int(count) for tier, count in (split["counts"] or {}).items()},
        "denominator_cycles": total,
        # The stamp-only basis of the measured ``split`` view. Kept separate from
        # ``denominator_cycles`` because the replay branch below overwrites that
        # key with the (larger) replayed denominator.
        "measured_denominator_cycles": total,
        "completed_missing_tier_count": int(split["completed_missing_tier_count"]),
        "non_completed_with_tier_count": int(split["non_completed_with_tier_count"]),
        "rows_considered": int(agg["rows_considered"]),
        "window_days": days,
        "window_start": window_start.isoformat() if window_start else None,
        "window_end": window_end.isoformat() if window_end else None,
        "measured_at": as_of.isoformat(),
        "provenance": provenance,
    }
    if replay is not None:
        payload["measurement_status"] = "replayed"
        payload["bound_kind"] = "fail_closed_lower_bound"
        payload["routine_share"] = _round_proportion(replay.get("routine_share"))
        payload["counts"] = {
            tier: int(count) for tier, count in (replay.get("counts") or {}).items()
        }
        payload["denominator_cycles"] = int(replay.get("denominator_cycles") or 0)
        payload["signal_resolution"] = dict(replay.get("signal_resolution") or {})
        payload["replay_sources"] = dict(replay.get("source_counts") or {})
    return payload


async def _load_cycle_rows() -> list[dict[str, Any]]:
    """Read every StrategicCycle row (lazy DB import; app package required).

    Each cycle's phase2 task first payload contributes a ``pre_spawn_signals``
    dict (``open_message_count`` / ``version_phase_block``) when present, and
    the phase1 design-doc task context contributes ``operator_steered`` (the
    only persisted source of that signal), so the replay path can reconstruct
    the classifier's pre-spawn signals from persisted data instead of waiting
    for an effort-tier stamp.
    """
    backend_root = Path(__file__).resolve().parents[1]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    from app.database import AsyncSessionLocal  # noqa: PLC0415
    from app.models import StrategicCycle, Task  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    async with AsyncSessionLocal() as session:
        cycles = (await session.execute(select(StrategicCycle))).scalars().all()
        phase2_ids = [c.phase2_task_id for c in cycles if c.phase2_task_id is not None]
        tasks: dict = {}
        if phase2_ids:
            task_rows = (
                await session.execute(select(Task).where(Task.id.in_(phase2_ids)))
            ).scalars().all()
            tasks = {t.id: t for t in task_rows}
        phase1_ids = [c.phase1_task_id for c in cycles if c.phase1_task_id is not None]
        phase1_tasks: dict = {}
        if phase1_ids:
            task_rows = (
                await session.execute(select(Task).where(Task.id.in_(phase1_ids)))
            ).scalars().all()
            phase1_tasks = {t.id: t for t in task_rows}

    rows: list[dict[str, Any]] = []
    for cycle in cycles:
        row = row_to_dict(cycle)
        pre: dict[str, Any] = {}
        task = tasks.get(getattr(cycle, "phase2_task_id", None))
        if task is not None:
            pre.update(_extract_pre_spawn_signals(task.first_payload))
        phase1_task = phase1_tasks.get(getattr(cycle, "phase1_task_id", None))
        if phase1_task is not None:
            operator_steered = _extract_operator_steered_from_phase1_context(
                getattr(phase1_task, "context", None)
            )
            if operator_steered is not None:
                pre["operator_steered"] = operator_steered
        if pre:
            row["pre_spawn_signals"] = pre
        rows.append(row)
    return rows


def _print_inputs_and_results(
    rows: list[Mapping[str, Any]],
    window_start: datetime | None,
    window_end: datetime | None,
    days: int | None,
    as_of: datetime,
    agg: Mapping[str, Any],
) -> None:
    split = agg["split"]
    cc = agg["course_changing"]

    print("=" * 78)
    print("Realised strategic-review effort-tier evidence (criterion c-a6b328b9)")
    print("=" * 78)

    print("\n[1] Inputs and window boundaries (audit the number, don't trust it)\n")
    print(f"  Window mode      : last {days} days" if days else "  Window mode      : ALL recorded rows (no --days)")
    print(f"  As-of timestamp  : {as_of.isoformat()}")
    print(f"  Rows read        : {agg['rows_considered']} StrategicCycle rows")
    print(f"  Window start     : {window_start.isoformat() if window_start else 'unbounded'}")
    print(f"  Window end       : {window_end.isoformat() if window_end else 'unbounded'}")
    print("  Denominator      : COMPLETED cycles with a stamped effort_tier")
    print("  Tier source      : cycle_metadata['effort_tier'] (record_effort_tier, spawn time)")
    print("  Course-changing  : directions_created>0 OR directions_retired>0 OR vision_updated")
    print("                    (tasks_spawned reported separately; NOT course-changing)")

    print("\n[2] Honest row accounting\n")
    print(f"  Completed cycles with a tier   : {split['total_completed_with_tier']}")
    print(f"  Completed cycles MISSING tier  : {split['completed_missing_tier_count']}")
    if split["completed_missing_tier_ids"]:
        for cid in split["completed_missing_tier_ids"]:
            print(f"      - {cid}")
    print(f"  Non-completed rows with a tier : {split['non_completed_with_tier_count']}")
    print("    (in-flight/failed cycles -- outcomes not yet realised; excluded from")
    print("     the split, reported here so nothing is silently dropped)")

    print("\n[3] REALISED routine/consequential split (counts + proportions)\n")
    total = split["total_completed_with_tier"]
    for tier in ("routine", "consequential", "pinned", "other"):
        count = split["counts"].get(tier, 0)
        prop = split["proportions"].get(tier, 0.0)
        print(f"  {tier:<14} {count:>5}  {prop:>7.1%} of {total} completed tiered cycles")
    if total == 0:
        print("  (no completed cycles with a stamped tier -- the recorded window is empty)")

    print("\n[4] Course-changing findings by tier\n")
    print("  Definition: opened a direction, retired a direction, or changed the vision.")
    for tier in ("routine", "consequential", "pinned", "other"):
        info = cc["by_tier"].get(tier, {"cycles": 0, "course_changing": 0, "course_changing_rate": 0.0})
        print(f"  {tier:<14} {info['course_changing']:>4} course-changing / {info['cycles']:>4} cycles "
              f"({info['course_changing_rate']:.1%})")

    print("\n[5] Verdict: did the cheaper tier silence the findings that matter?\n")
    if cc["by_tier"]["routine"]["cycles"] == 0:
        print("  No routine-tier completed cycles in the window -- the split is entirely")
        print("  consequential/pinned, so the cheapening claim has no realised support yet.")
    elif cc["routine_course_changing_cycles"] > 0:
        print(f"  ROUTINE cycles produced {cc['routine_course_changing_cycles']} "
              f"course-changing finding(s) across {cc['by_tier']['routine']['cycles']} "
              f"routine cycles ({cc['routine_course_changing_rate']:.1%}).")
        print("  Dropping to the cheaper tier did NOT silence all course-changing findings.")
    else:
        print(f"  ROUTINE cycles produced ZERO course-changing findings across "
              f"{cc['by_tier']['routine']['cycles']} routine cycles.")
        print("  The cheaper tier produced no persisted course-changing output in this window.")


def _print_replay_results(
    rows: list[Mapping[str, Any]],
    window_start: datetime | None,
    window_end: datetime | None,
    days: int | None,
    as_of: datetime,
    agg: Mapping[str, Any],
    replay: Mapping[str, Any],
) -> None:
    print("=" * 78)
    print("Replayed routine-share lower bound (fail-closed classifier replay)")
    print("=" * 78)

    print("\n[1] Replay inputs (audit the number, don't trust it)\n")
    print(f"  Window mode      : last {days} days" if days else "  Window mode      : ALL recorded rows (no --days)")
    print(f"  As-of timestamp  : {as_of.isoformat()}")
    print(f"  Rows read        : {agg['rows_considered']} StrategicCycle rows")
    print(f"  Window start     : {window_start.isoformat() if window_start else 'unbounded'}")
    print(f"  Window end       : {window_end.isoformat() if window_end else 'unbounded'}")
    print("  Denominator      : ALL COMPLETED cycles (stamped + replayed)")
    print("  Tier source      : stamp preferred; unstamped re-classified by")
    print("                     classify_strategic_review_effort on reconstructed signals")

    print("\n[2] Replayed split (fail-closed lower bound)\n")
    total = int(replay["denominator_cycles"])
    for tier in ("routine", "consequential", "other"):
        count = int(replay["counts"].get(tier, 0))
        prop = replay["proportions"].get(tier, 0.0)
        print(f"  {tier:<14} {count:>5}  {prop:>7.1%} of {total} completed cycles")
    if total == 0:
        print("  (no completed cycles in the window)")
    print(f"\n  Replayed routine share (LOWER BOUND) : {replay['routine_share']:.6f}")

    print("\n[3] Tier provenance\n")
    src = replay["source_counts"]
    print(f"  From stamps  : {src['stamp']}")
    print(f"  From replay  : {src['replay']}")

    print("\n[4] Signal resolution (how each reconstructed signal was obtained)\n")
    for signal in sorted(replay["signal_resolution"]):
        by_label = replay["signal_resolution"][signal]
        detail = ", ".join(f"{label}={count}" for label, count in sorted(by_label.items()))
        print(f"  {signal:<28} {detail}")

    print("\n[5] Meaning\n")
    print("  This share is a FAIL-CLOSED LOWER BOUND, NOT a measured realized")
    print("  split: signals a ROUTINE verdict needs that were genuinely unavailable")
    print("  (most load-bearing: open_message_count) resolved to CONSEQUENTIAL.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Restrict to cycles created within the last N days (default: all rows)",
    )
    parser.add_argument(
        "--write-json",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Write the effort-tier split as a durable JSON payload to PATH (the "
            "checked-in address backend/scripts/strategic_cycle_cost_projection.py "
            "reads routine share R from); does not touch the database"
        ),
    )
    parser.add_argument(
        "--replay",
        action="store_true",
        default=False,
        help=(
            "Compute the fail-closed replayed routine-share LOWER BOUND over ALL "
            "completed cycles: cycles with a stamped effort_tier use the stamp; "
            "unstamped completed cycles are re-classified by re-running "
            "classify_strategic_review_effort on signals reconstructed from "
            "persisted StrategicCycle data, with unavailable signals resolving to "
            "CONSEQUENTIAL. With --write-json the emitted payload is labelled "
            "measurement_status 'replayed' / bound_kind 'fail_closed_lower_bound'."
        ),
    )
    args = parser.parse_args()

    import asyncio  # noqa: PLC0415

    rows = asyncio.run(_load_cycle_rows())
    as_of = datetime.now(timezone.utc)
    kept, window_start, window_end = filter_rows_by_window(rows, args.days, as_of)
    agg = aggregate_effort_tier_evidence(kept)

    replay = None
    if args.replay:
        replay = replay_effort_tier_split(kept)
        _print_replay_results(kept, window_start, window_end, args.days, as_of, agg, replay)
    else:
        _print_inputs_and_results(kept, window_start, window_end, args.days, as_of, agg)

    if args.write_json:
        if args.replay:
            provenance = (
                "Replayed fail-closed lower bound regenerated by "
                "backend/scripts/strategic_effort_tier_evidence.py "
                f"--replay --days {args.days if args.days is not None else 'all'} "
                f"--write-json {args.write_json}. Routine share R is a "
                "FAIL-CLOSED LOWER BOUND (measurement_status 'replayed' / "
                "bound_kind 'fail_closed_lower_bound'), NOT a measured realized "
                "split: completed cycles with a stamped cycle_metadata['effort_tier'] "
                "use the stamp; unstamped completed cycles are re-classified by "
                "re-running app.strategic_review_effort.classify_strategic_review_effort "
                "on signals reconstructed from persisted StrategicCycle data, with "
                "any signal a ROUTINE verdict needs that is genuinely unavailable "
                "(most load-bearing: open_message_count) resolving to CONSEQUENTIAL."
            )
        else:
            total = int(agg["split"]["total_completed_with_tier"])
            provenance = (
                "Regenerated by backend/scripts/strategic_effort_tier_evidence.py "
                f"--days {args.days if args.days is not None else 'all'} "
                f"--write-json {args.write_json}. Routine share R is "
                f"{'MEASURED' if total > 0 else 'UNMEASURED'} from "
                f"{agg['rows_considered']} StrategicCycle row(s): the realised split "
                "over COMPLETED cycles with a stamped cycle_metadata['effort_tier'] "
                "(written at spawn time by strategic_cycle.record_effort_tier), "
                "excluding completed cycles missing a tier and non-completed cycles "
                "with a tier (reported separately, never silently dropped). "
                "A fail-closed replayed lower bound (no stamps required) is produced "
                "by adding --replay to this command."
            )
        payload = split_to_json_payload(
            agg, window_start, window_end, args.days, as_of, provenance, replay=replay
        )
        out_path = Path(args.write_json)
        out_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote {'replayed ' if args.replay else ''}effort-tier split to {out_path}")


if __name__ == "__main__":
    main()
