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


def split_to_json_payload(
    agg: Mapping[str, Any],
    window_start: datetime | None,
    window_end: datetime | None,
    days: int | None,
    as_of: datetime,
    provenance: str,
) -> dict[str, Any]:
    """Durable JSON payload for the realised effort-tier split.

    ``measurement_status`` is ``"measured"`` when at least one completed cycle
    carries a stamped tier (``routine_share`` = the realised routine proportion
    over completed tiered cycles); otherwise ``"unmeasured"`` with
    ``routine_share: null`` -- the honest state when the window has no
    completed stamped cycles. ``backend/scripts/strategic_cycle_cost_projection.py``
    reads this exact schema, so this payload IS the durable address of R.
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

    return {
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
        "completed_missing_tier_count": int(split["completed_missing_tier_count"]),
        "non_completed_with_tier_count": int(split["non_completed_with_tier_count"]),
        "rows_considered": int(agg["rows_considered"]),
        "window_days": days,
        "window_start": window_start.isoformat() if window_start else None,
        "window_end": window_end.isoformat() if window_end else None,
        "measured_at": as_of.isoformat(),
        "provenance": provenance,
    }


async def _load_cycle_rows() -> list[dict[str, Any]]:
    """Read every StrategicCycle row (lazy DB import; app package required)."""
    backend_root = Path(__file__).resolve().parents[1]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    from app.database import AsyncSessionLocal  # noqa: PLC0415
    from app.models import StrategicCycle  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    async with AsyncSessionLocal() as session:
        cycles = (await session.execute(select(StrategicCycle))).scalars().all()
    return [row_to_dict(c) for c in cycles]


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
            "Write the realised effort-tier split as a durable JSON payload to "
            "PATH (the checked-in address backend/scripts/"
            "strategic_cycle_cost_projection.py reads routine share R from); "
            "does not touch the database"
        ),
    )
    args = parser.parse_args()

    import asyncio  # noqa: PLC0415

    rows = asyncio.run(_load_cycle_rows())
    as_of = datetime.now(timezone.utc)
    kept, window_start, window_end = filter_rows_by_window(rows, args.days, as_of)
    agg = aggregate_effort_tier_evidence(kept)
    _print_inputs_and_results(kept, window_start, window_end, args.days, as_of, agg)

    if args.write_json:
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
            "with a tier (reported separately, never silently dropped)."
        )
        payload = split_to_json_payload(
            agg, window_start, window_end, args.days, as_of, provenance
        )
        out_path = Path(args.write_json)
        out_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote realised effort-tier split to {out_path}")


if __name__ == "__main__":
    main()
