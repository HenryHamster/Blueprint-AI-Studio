"""Lane-baseline snapshot persistence.

When a ReleaseVersion reaches a terminal status (promoted or abandoned), this
module captures the version's lane-diagnostic aggregates and persists them
durably as one row per version in the dedicated ``project_lane_baseline``
table. Historical ``project.design_doc["lane_baselines"]`` values remain as a
read fallback until a future backfill-and-strip migration explicitly retires
them.

The stored blob is a subset of the lane-diag response plus per-PR relay medians
for the v10 lane-pilot comparison.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy.dialects.postgresql import insert as _pg_insert
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.digest_metrics import (
    compute_lane_waste_diagnostics,
    compute_outer_loop_convergence,
)
from app.models import (
    ImprovementLane,
    Project,
    ProjectLaneBaseline,
    Task,
    TaskStatus,
    _utcnow,
)
from app.project_telemetry import (
    read_all_lane_baselines,
    upsert_lane_baseline,
)
from app.project_telemetry import _prune_lane_baselines
from app.project_telemetry import (
    read_version_lane_baseline as _read_table_lane_baseline,
)
from app.review_fix_lane_pilot import LANE_RECOVERY_MANIFEST_KEY, LANE_RECOVERY_REASON_KEY
from app.routers.admin_analytics import (
    _compute_lane_diag,
    _review_loop_pr_number,
)
from app.routers.admin_pipeline_observability import (
    _load_escalation_alerts,
    _load_outer_loop_original_tasks,
)

logger = logging.getLogger(__name__)

DESIGN_DOC_KEY = "lane_baselines"
BASELINE_DEF_VERSION = "v5"
_SNAPSHOT_PR_BREAKDOWN_LIMIT = 100
_MAX_TASKS_PER_SNAPSHOT = 5000
_MAX_BASELINES_STORED = 20
# Per-entry byte budget: 20 entries x 10 KB = 200 KB max, well under the 500 KB design-doc ceiling.
_MAX_BASELINE_ENTRY_BYTES = 10_000

# Fields dropped before persisting: large unread sub-blobs that inflate the entry.
_SLIM_DROP_TOP_LEVEL = frozenset({"per_pr_phase_breakdown", "relay_counts_by_lane"})
# Fallback fields to shed one-by-one if the entry still exceeds the byte budget after
# the primary drops.  Lane waste is deliberately not in this list: its compact
# grading counters are the durable evidence for the lane-retarget experiment.
_SLIM_OVERFLOW_DROPPABLE = (
    "lane_iteration_distribution",
    "checkpoint_recovery_waste",
)

_LANE_WASTE_GRADING_FIELDS = frozenset(
    {"retarget_review_scope_counts", "stale_head_discard_count"}
)
_LANE_WASTE_KNOWN_FIELDS = frozenset(
    {
        "waste_taxonomy",
        "waste_task_count",
        "cap_counted_task_count",
        "cap_excluded_task_count",
        "chain_count",
        "cap_exhausted_chain_count",
        "cap_exhausted_direction_count",
        "cap_exhausted_directions",
        "supersede_affected_chain_count",
        "supersede_affected_reviews_to_converge",
        "rollup_chain_count",
        "rollup_supersede_affected_chain_count",
        "rollup_supersede_unaffected_chain_count",
        "rollup_supersede_affected_reviews_to_converge",
        "rollup_supersede_unaffected_reviews_to_converge",
        "reviews_to_converge",
        "retarget_review_scope_counts",
        "stale_head_discard_count",
        "chain_limit",
        "chains_truncated",
        "chains",
        "converged_review_counts",
    }
)


def _baseline_definition_number(version: str | None) -> int:
    """Return the numeric revision for a versioned snapshot definition."""
    if isinstance(version, str) and version.startswith("v"):
        try:
            return int(version[1:])
        except ValueError:
            pass
    return 0


def _baseline_needs_refresh(row: ProjectLaneBaseline) -> bool:
    """Whether *row* predates the current persisted snapshot definition."""
    return _baseline_definition_number(row.baseline_def_version) < _baseline_definition_number(
        BASELINE_DEF_VERSION
    )


def _slim_baseline(baseline: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *baseline* with large unread sub-blobs removed.

    Primary drops (no downstream readers):
    - ``per_pr_phase_breakdown`` — per-PR phase rows (the live UI recomputes from DB)
    - ``relay_counts_by_lane`` — relay-count breakdown by lane
    - ``lane_waste["chains"]`` — per-chain/per-task drilldown rows

    After the primary drops an entry should comfortably fit inside
    ``_MAX_BASELINE_ENTRY_BYTES``.  If it still exceeds the budget (e.g. because a
    future heavy field was added), fields are shed in
    ``_SLIM_OVERFLOW_DROPPABLE`` order until the entry fits.  Overflow-only
    removals are recorded in ``dropped_fields``; the primary drops above are
    intentionally not reported as evictions.
    """
    entry = {k: v for k, v in baseline.items() if k not in _SLIM_DROP_TOP_LEVEL}
    existing_dropped = {
        field
        for field in (baseline.get("dropped_fields") or [])
        if isinstance(field, str) and field
    }
    if existing_dropped:
        entry["dropped_fields"] = sorted(existing_dropped)
    if "lane_waste" in entry and isinstance(entry["lane_waste"], dict):
        lw = dict(entry["lane_waste"])
        lw.pop("chains", None)
        # ``converged_review_counts`` was an unused unbounded legacy sample.
        # Remove it before overflow shedding so it cannot evict durable metrics.
        lw.pop("converged_review_counts", None)
        entry["lane_waste"] = lw

    def _size() -> int:
        return len(json.dumps(entry, default=str).encode())

    def _record_drop(field: str) -> None:
        existing_dropped.add(field)
        entry["dropped_fields"] = sorted(existing_dropped)
        logger.warning(
            "[LANE_BASELINE] entry for v%s still over %d B after slim; dropping %s",
            entry.get("version_number"),
            _MAX_BASELINE_ENTRY_BYTES,
            field,
        )

    for field in _SLIM_OVERFLOW_DROPPABLE:
        if _size() <= _MAX_BASELINE_ENTRY_BYTES:
            break
        if field in entry:
            entry.pop(field)
            _record_drop(field)

    # The lane-waste drilldown is the only large block that contains the
    # direction-grading counters.  If it is the remaining source of overflow,
    # remove nonessential nested additions first and retain all known scalar
    # counters plus both retarget scope counts and stale-head discards.
    while _size() > _MAX_BASELINE_ENTRY_BYTES:
        lane_waste = entry.get("lane_waste")
        if not isinstance(lane_waste, dict):
            break
        candidates = sorted(
            key
            for key in lane_waste
            if key not in _LANE_WASTE_GRADING_FIELDS
            and key not in {"dropped_fields"}
            and (
                key not in _LANE_WASTE_KNOWN_FIELDS
                or isinstance(lane_waste[key], (dict, list, tuple))
            )
        )
        if not candidates:
            break
        field = candidates[0]
        lane_waste.pop(field)
        _record_drop(f"lane_waste.{field}")

    # Keep the byte bound future-proof for an unexpectedly large top-level
    # addition.  Core baseline identity, convergence, and lane-waste grading
    # evidence are retained; only non-core additions are eligible here.
    mandatory = {
        "captured_at",
        "baseline_def_version",
        "version_number",
        "terminal_status",
        "opened_at",
        "closed_at",
        "outer_loop_convergence",
        "lane_waste",
        "dropped_fields",
    }
    while _size() > _MAX_BASELINE_ENTRY_BYTES:
        candidates = [field for field in entry if field not in mandatory]
        if not candidates:
            break
        field = max(
            candidates,
            key=lambda key: (len(json.dumps(entry[key], default=str)), key),
        )
        entry.pop(field)
        _record_drop(field)

    return entry


_TASK_SNAPSHOT_COLUMNS = (
    Task.id,
    Task.project_id,
    Task.status,
    Task.created_at,
    Task.started_at,
    Task.completed_at,
    Task.updated_at,
    Task.status_log,
    Task.payloads,
    Task.context,
    Task.fix_loop_history,
    Task.archived,
    Task.vm_dispatched_at,
    Task.vm_provisioned_at,
    Task.vm_started_at,
    Task.vm_stopped_at,
)


def _compute_checkpoint_recovery_waste(tasks: List[Task], lanes: List[Any] | None = None) -> Dict[str, Any]:
    """Count checkpoint-recovery tasks by reason from a version task window.

    Recovery tasks carry ``LANE_RECOVERY_REASON_KEY`` in their context, stamped
    by the review-spawn path when a checkpoint miss is classified as RECOVER.
    Groups by the reason string and returns a total count. Exhausted-budget
    kills are counted from ImprovementLane rows in KILLED status with an
    exhausted recovery marker in the manifest.
    """
    by_reason: Dict[str, int] = {}
    for task in tasks:
        ctx = task.context or {}
        reason = ctx.get(LANE_RECOVERY_REASON_KEY)
        if reason is not None:
            key = str(reason)
            by_reason[key] = by_reason.get(key, 0) + 1

    exhausted_count = 0
    if lanes:
        for lane in lanes:
            if lane.status == "KILLED":
                manifest = lane.manifest or {}
                recovery = manifest.get(LANE_RECOVERY_MANIFEST_KEY)
                if isinstance(recovery, dict) and recovery.get("exhausted") is True:
                    exhausted_count += 1

    return {
        "by_reason": by_reason,
        "total": sum(by_reason.values()),
        "exhausted_count": exhausted_count,
    }


def _compute_relay_medians(per_pr_breakdown: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Derive per-PR relay-baseline medians from lane-diag phase breakdown rows."""
    if not per_pr_breakdown:
        return {
            "wall_clock_seconds": None,
            "active_seconds": None,
            "gap_seconds": None,
            "task_count": None,
            "pr_count": 0,
            "fix_active_seconds": None,
            "review_active_seconds": None,
        }

    def _median(vals: List[float]) -> float | None:
        if not vals:
            return None
        s = sorted(vals)
        n = len(s)
        mid = n // 2
        return s[mid] if n % 2 == 1 else (s[mid - 1] + s[mid]) / 2.0

    return {
        "wall_clock_seconds": _median([r["total_seconds"] for r in per_pr_breakdown]),
        "active_seconds": _median(
            [r["review_seconds"] + r["fix_seconds"] for r in per_pr_breakdown]
        ),
        "gap_seconds": _median([r["gap_seconds"] for r in per_pr_breakdown]),
        "task_count": _median([float(r["task_count"]) for r in per_pr_breakdown]),
        "pr_count": len(per_pr_breakdown),
        "fix_active_seconds": _median([r["fix_seconds"] for r in per_pr_breakdown]),
        "review_active_seconds": _median(
            [r["review_seconds"] for r in per_pr_breakdown]
        ),
    }


async def persist_version_lane_baseline(
    session: Any,
    *,
    project_id: uuid.UUID,
    version_number: int,
    opened_at: datetime,
    closed_at: datetime,
    terminal_status: str,
) -> Dict[str, Any] | None:
    """Compute and store the lane baseline for a just-closed ReleaseVersion.

    Queries the version's task window, computes full lane-diag aggregates plus
    per-PR relay medians, and persists the result as a ``project_lane_baseline``
    row keyed by ``version_number``.

    First-write-wins: a second call for the same version is a no-op.  Always
    called from a separate ``AsyncSessionLocal`` so a failure here never aborts
    promotion or abandonment.  Caller must ``await session.commit()``.

    Returns the baseline dict that was (or was already) persisted, or None on
    error.
    """

    def _strip_tz(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt

    t_opened = _strip_tz(opened_at)
    t_closed = _strip_tz(closed_at)

    # Lock an obsolete row for the duration of the recomputation.  Terminal
    # hooks and the periodic sweep can overlap, but only the first writer for
    # the current definition may win; a current or recognized newer row is
    # returned before doing the expensive diagnostic queries.
    existing_row = (
        (
            await session.execute(
                select(ProjectLaneBaseline)
                .where(
                    ProjectLaneBaseline.project_id == project_id,
                    ProjectLaneBaseline.version_number == version_number,
                )
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if existing_row is not None and not _baseline_needs_refresh(existing_row):
        logger.info(
            "[LANE_BASELINE] v%d baseline already stored for project=%s "
            "(definition=%s, first-write-wins, skip)",
            version_number,
            project_id,
            existing_row.baseline_def_version,
        )
        return dict(existing_row.blob)

    _opts = load_only(*_TASK_SNAPSHOT_COLUMNS, raiseload=True)
    _proj_filter = [Task.project_id == project_id]

    completed_tasks = list(
        (
            await session.execute(
                select(Task)
                .options(_opts)
                .where(
                    Task.status == TaskStatus.COMPLETED,
                    Task.created_at >= t_opened,
                    Task.created_at < t_closed,
                    *_proj_filter,
                )
                .order_by(Task.created_at.desc())
                .limit(_MAX_TASKS_PER_SNAPSHOT)
            )
        )
        .scalars()
        .all()
    )

    all_terminal_tasks = list(
        (
            await session.execute(
                select(Task)
                .options(_opts)
                .where(
                    Task.status.in_(
                        [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
                    ),
                    Task.created_at >= t_opened,
                    Task.created_at < t_closed,
                    *_proj_filter,
                )
                .order_by(Task.created_at.desc())
                .limit(_MAX_TASKS_PER_SNAPSHOT)
            )
        )
        .scalars()
        .all()
    )

    version_lanes = list(
        (
            await session.execute(
                select(ImprovementLane).where(
                    ImprovementLane.project_id == project_id,
                    ImprovementLane.created_at >= t_opened,
                    ImprovementLane.created_at < t_closed,
                )
            )
        )
        .scalars()
        .all()
    )

    scoped = [t for t in completed_tasks if _review_loop_pr_number(t)]
    diag = _compute_lane_diag(scoped, _SNAPSHOT_PR_BREAKDOWN_LIMIT)

    original_tasks = await _load_outer_loop_original_tasks(
        session, all_terminal_tasks, project_id=project_id
    )
    all_tasks_with_originals = [
        *all_terminal_tasks,
        *[
            task
            for oid, task in original_tasks.items()
            if oid not in {str(t.id) for t in all_terminal_tasks}
        ],
    ]
    alerts = await _load_escalation_alerts(
        session, cutoff=t_opened, project_id=project_id
    )
    diag["lane_waste"] = compute_lane_waste_diagnostics(
        all_tasks_with_originals,
        t_opened,
        chain_limit=_SNAPSHOT_PR_BREAKDOWN_LIMIT,
        escalation_alerts=alerts,
    )
    convergence = compute_outer_loop_convergence(
        all_tasks=all_tasks_with_originals,
        escalation_alerts=alerts,
        window_start=t_opened,
    )
    # Keep only scalar convergence counters and the small terminal histogram.
    # The reducer's per-chain rows are deliberately not part of a durable
    # version snapshot; lane_waste owns the bounded drilldown for the live
    # window, while these counters remain available after the version closes.
    diag["outer_loop_convergence"] = {
        "iteration_median": convergence.get("iteration_median"),
        "iteration_p95": convergence.get("iteration_p95"),
        "convergence_rate": convergence.get("convergence_rate"),
        "cap_exhausted_chain_count": (
            (convergence.get("outcomes") or {}).get("cap_exhausted", 0)
        ),
        "cap_exhausted_direction_count": convergence.get(
            "cap_exhausted_direction_count", 0
        ),
        "cap_exhausted_directions": convergence.get(
            "cap_exhausted_directions", []
        ),
        "supersede_affected_chain_count": convergence.get(
            "supersede_affected_chain_count", 0
        ),
        "supersede_affected_merged_chain_count": convergence.get(
            "supersede_affected_merged_chain_count", 0
        ),
        "supersede_affected_reviews_to_converge_count": (
            convergence.get("supersede_affected_reviews_to_converge") or {}
        ).get("count", 0),
        "supersede_affected_reviews_to_converge_sample_count": (
            convergence.get("supersede_affected_reviews_to_converge") or {}
        ).get("sample_count", 0),
        "supersede_affected_reviews_to_converge_median": (
            convergence.get("supersede_affected_reviews_to_converge") or {}
        ).get("median"),
        "supersede_affected_reviews_to_converge_p95": (
            convergence.get("supersede_affected_reviews_to_converge") or {}
        ).get("p95"),
        "rollup_chain_count": convergence.get("rollup_chain_count", 0),
        "rollup_supersede_affected_chain_count": convergence.get(
            "rollup_supersede_affected_chain_count", 0
        ),
        "rollup_supersede_unaffected_chain_count": convergence.get(
            "rollup_supersede_unaffected_chain_count", 0
        ),
        "rollup_supersede_affected_reviews_to_converge_count": (
            convergence.get("rollup_supersede_affected_reviews_to_converge") or {}
        ).get("count", 0),
        "rollup_supersede_affected_reviews_to_converge_sample_count": (
            convergence.get("rollup_supersede_affected_reviews_to_converge") or {}
        ).get("sample_count", 0),
        "rollup_supersede_affected_reviews_to_converge_median": (
            convergence.get("rollup_supersede_affected_reviews_to_converge") or {}
        ).get("median"),
        "rollup_supersede_affected_reviews_to_converge_p95": (
            convergence.get("rollup_supersede_affected_reviews_to_converge") or {}
        ).get("p95"),
        "rollup_supersede_unaffected_reviews_to_converge_count": (
            convergence.get("rollup_supersede_unaffected_reviews_to_converge") or {}
        ).get("count", 0),
        "rollup_supersede_unaffected_reviews_to_converge_sample_count": (
            convergence.get("rollup_supersede_unaffected_reviews_to_converge") or {}
        ).get("sample_count", 0),
        "rollup_supersede_unaffected_reviews_to_converge_median": (
            convergence.get("rollup_supersede_unaffected_reviews_to_converge") or {}
        ).get("median"),
        "rollup_supersede_unaffected_reviews_to_converge_p95": (
            convergence.get("rollup_supersede_unaffected_reviews_to_converge") or {}
        ).get("p95"),
        "terminal_outcome_counts": convergence.get("terminal_outcome_histogram") or {},
    }
    diag["relay_medians"] = _compute_relay_medians(diag["per_pr_phase_breakdown"])
    diag["checkpoint_recovery_waste"] = _compute_checkpoint_recovery_waste(
        all_terminal_tasks, lanes=version_lanes
    )

    now = _utcnow()
    baseline: Dict[str, Any] = {
        "captured_at": now.isoformat(),
        "baseline_def_version": BASELINE_DEF_VERSION,
        "version_number": version_number,
        "terminal_status": terminal_status,
        "opened_at": t_opened.isoformat(),
        "closed_at": t_closed.isoformat(),
        **diag,
    }

    slimmed = _slim_baseline(baseline)

    # Project-existence guard (non-locking): a missing project cannot own a
    # baseline row (FK), and the legacy path returned None in that case.
    _exists = (
        await session.execute(select(Project.id).where(Project.id == project_id))
    ).first()
    if _exists is None:
        logger.warning(
            "[LANE_BASELINE] project %s not found; v%d baseline not persisted",
            project_id,
            version_number,
        )
        return None

    if existing_row is not None:
        # The row is locked above, so this overwrite cannot downgrade a newer
        # definition or race another stale refresh.
        await upsert_lane_baseline(
            session,
            project_id=project_id,
            version_number=version_number,
            captured_at=now,
            baseline_def_version=BASELINE_DEF_VERSION,
            blob=slimmed,
        )
    else:
        # A terminal hook and a backfill can both observe an absent row.  Use
        # DO NOTHING so the losing writer cannot replace the winner's current
        # definition, then retain the normal table retention behavior.
        stmt = _pg_insert(ProjectLaneBaseline).values(
            id=uuid.uuid4(),
            project_id=project_id,
            version_number=version_number,
            captured_at=now,
            baseline_def_version=BASELINE_DEF_VERSION,
            blob=slimmed,
        )
        await session.execute(
            stmt.on_conflict_do_nothing(
                index_elements=[
                    ProjectLaneBaseline.project_id,
                    ProjectLaneBaseline.version_number,
                ]
            )
        )
        await _prune_lane_baselines(session, project_id)

        # If another writer won the insert race, return its blob and preserve
        # its first-write-wins timestamp/content contract.
        stored = await _read_table_lane_baseline(session, project_id, version_number)
        if stored is not None:
            slimmed = stored

    logger.info(
        "[LANE_BASELINE] persisted v%d lane baseline for project=%s "
        "(status=%s, pr_count=%d, task_count=%d)",
        version_number,
        project_id,
        terminal_status,
        diag.get("pr_count", 0),
        diag.get("task_count", 0),
    )
    return slimmed


async def backfill_missing_lane_baselines(
    *,
    project_id: uuid.UUID,
    max_versions: int = _MAX_BASELINES_STORED,
) -> List[int]:
    """Backfill lane baselines for recently-closed versions that lack a stored entry.

    Queries up to ``max_versions`` closed (promoted or abandoned) ReleaseVersions
    for the project, skips only table rows at the current or a newer definition,
    and calls ``persist_version_lane_baseline`` for each missing or obsolete
    snapshot. Legacy ``design_doc["lane_baselines"]`` entries remain intact and
    do not suppress a table refresh.

    Uses its own ``AsyncSessionLocal`` sessions per version so each persist
    commits independently and the populate_existing lock does not overwrite
    an earlier write in the same transaction.  Safe to run repeatedly:
    ``persist_version_lane_baseline`` is first-write-wins idempotent.

    Returns the list of version_numbers for which a baseline was written.
    Failures on individual versions are logged and skipped (non-fatal).
    """
    from app.database import AsyncSessionLocal
    from app.models import Project, ReleaseVersion

    # Read the list of closed versions and existing lane baseline keys.
    async with AsyncSessionLocal() as session:
        project_row = (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project_row is None:
            return []

        # Legacy design_doc rows are intentionally not included: they are the
        # fallback being repaired, and the refreshed table row takes precedence
        # without mutating the retained document.
        current_table_versions = {
            row.version_number
            for row in await read_all_lane_baselines(session, project_id)
            if not _baseline_needs_refresh(row)
        }

        versions = list(
            (
                await session.execute(
                    select(ReleaseVersion)
                    .where(
                        ReleaseVersion.project_id == project_id,
                        ReleaseVersion.status.in_(["promoted", "abandoned"]),
                    )
                    .order_by(ReleaseVersion.version_number.desc())
                    .limit(max_versions)
                )
            )
            .scalars()
            .all()
        )

    # Persist each missing baseline in its own session so commits are atomic
    # and independent (mirrors the terminal-hook pattern in release_promotion /
    # release_version).
    backfilled: List[int] = []
    for version in versions:
        if version.version_number in current_table_versions:
            continue

        if version.status == "promoted":
            closed_at = version.promoted_at or _utcnow()
        else:
            abandoned_at_str = (version.last_qualification or {}).get("abandoned_at")
            if abandoned_at_str:
                try:
                    closed_at = datetime.fromisoformat(abandoned_at_str)
                except (ValueError, TypeError):
                    closed_at = _utcnow()
            else:
                closed_at = _utcnow()

        try:
            async with AsyncSessionLocal() as _snap_session:
                result = await persist_version_lane_baseline(
                    _snap_session,
                    project_id=project_id,
                    version_number=version.version_number,
                    opened_at=version.opened_at,
                    closed_at=closed_at,
                    terminal_status=version.status,
                )
                await _snap_session.commit()
            if result is not None:
                backfilled.append(version.version_number)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[LANE_BASELINE] backfill: persist failed for project=%s v%d (non-fatal)",
                project_id,
                version.version_number,
                exc_info=True,
            )

    return backfilled


def read_version_lane_baseline(
    project: Project,
    version_number: int,
    *,
    lane_baseline_data: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    """Return the persisted lane baseline for the given version, or None.

    When ``lane_baseline_data`` is provided (pre-loaded from the
    ``project_lane_baseline`` table), it takes precedence. Falls back to
    ``project.design_doc["lane_baselines"]`` for data written before
    migration 0681.  Zero additional DB queries in either case — the caller
    must pre-load the appropriate row.
    """
    if lane_baseline_data is not None:
        return lane_baseline_data.get(str(version_number))
    # Backward compat: check design_doc for old data.
    if not project.design_doc:
        return None
    baselines = project.design_doc.get(DESIGN_DOC_KEY)
    if not baselines:
        return None
    return baselines.get(str(version_number))


async def read_version_lane_baseline_preferring_table(
    session: Any,
    project: Project,
    version_number: int,
) -> Dict[str, Any] | None:
    """Return the lane baseline for a version, preferring the dedicated table.

    Prefers the ``project_lane_baseline`` row; falls back to the design_doc blob
    on the already-loaded ``project`` row when the table has no row for that
    version (before backfill or after a rollback that reverted the dual-write).
    """
    row = await _read_table_lane_baseline(session, project.id, version_number)
    if row is not None:
        return row
    return read_version_lane_baseline(project, version_number)


async def read_all_lane_baselines_preferring_table(
    session: Any,
    project_id: uuid.UUID,
    design_doc: Dict[str, Any] | None,
) -> Dict[str, Dict[str, Any]]:
    """Return ``{version_key: blob}`` for every stored version, preferring the table.

    Rows from ``project_lane_baseline`` override the design_doc
    ``lane_baselines`` map for the same version; versions present only in
    design_doc (pre-backfill or post-rollback) are still included so no stored
    baseline is dropped during the dual-write window. Keys are the stringified
    ``version_number`` so callers keep the same per-version keying they had when
    reading straight from ``design_doc['lane_baselines']``.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    if design_doc:
        for key, value in (design_doc.get(DESIGN_DOC_KEY) or {}).items():
            if isinstance(value, dict):
                merged[str(key)] = value
    for row in await read_all_lane_baselines(session, project_id):
        merged[str(row.version_number)] = row.blob
    return merged
