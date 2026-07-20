"""Short-window claim-vs-shipped drift detection.

A direction's strategic agent may *claim* shipped change (via plan_output,
TaskHistory, strategic-cycle history) without those claims actually
materializing as merged PRs. The lifetime calibration factor in
``direction_metrics`` captures merge prediction accuracy across many cycles
but doesn't surface short-window divergence between what the pipeline says
it shipped and what actually shipped.

This module computes a short rolling-window drift signal per direction and
rolls it up per system_id. The strategic agent and autonomous_research
consume this signal to switch the pipeline into audit/fundamentals mode
when drift is high — independent of lifetime merge calibration.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Index, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field, SQLModel, select

from app.landed_signal import has_landed_signal, has_merge_unblock_signal
from app.models import Artifact, ArtifactType, Project, Task, TaskStatus
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


_OVER_CLAIM_MIN_CLAIMS_DEFAULT = 3
_OVER_CLAIM_CONSISTENCY_THRESHOLD_DEFAULT = 0.4
_STALL_MIN_CLAIMS_DEFAULT = 5
# Maximum snapshots retained per (project_id, direction_id) pair.
_DRIFT_SNAPSHOT_RETENTION = 50


def _drift_thresholds() -> tuple[int, float, int]:
    """Return (over_claim_min_claims, over_claim_consistency_threshold, stall_min_claims)
    from runtime_config, falling back to defaults when absent.
    """
    from app.shared_state import runtime_config as _rc  # lazy import avoids circular

    return (
        int(_rc.get("drift_over_claim_min_claims", _OVER_CLAIM_MIN_CLAIMS_DEFAULT)),
        float(
            _rc.get(
                "drift_over_claim_consistency_threshold",
                _OVER_CLAIM_CONSISTENCY_THRESHOLD_DEFAULT,
            )
        ),
        int(_rc.get("drift_stall_min_claims", _STALL_MIN_CLAIMS_DEFAULT)),
    )


class DirectionDriftSnapshot(SQLModel, table=True):
    """One drift-signal observation for a single direction at a point in time.

    Written by persist_drift_snapshot() each autonomous cycle so consumers
    can look back N cycles and distinguish a one-off spike from a sustained
    breakdown.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(index=True)
    direction_id: str = Field(index=True)
    snapshot_at: datetime = Field(default_factory=_utcnow)
    claimed_changes_count: int = Field(default=0)
    actually_merged_prs: int = Field(default=0)
    claim_shipped_consistency: float = Field(default=1.0)
    over_claim_flag: bool = Field(default=False)
    stalled_despite_churn_flag: bool = Field(default=False)

    __table_args__ = (
        Index(
            "ix_dds_project_direction_at", "project_id", "direction_id", "snapshot_at"
        ),
    )


def _neutral_signal() -> dict[str, Any]:
    """Default signal returned when a direction has no data in the window.

    Neutral means "no evidence" — over_claim_flag and stalled_despite_churn_flag
    are False so consumers don't trigger an audit dispatch on empty data.
    """
    return {
        "claimed_changes_count": 0,
        "actually_merged_prs": 0,
        "claim_shipped_consistency": 1.0,
        "over_claim_flag": False,
        "stalled_despite_churn_flag": False,
    }


def _direction_claimed_changes(task: Task) -> int:
    """How many claimed shipped changes this task represents for its direction.

    A claim is recorded when a task that targets a direction reports a shipped
    change — captured most reliably by status COMPLETED with a plan_output
    that has results, or by ``context.shipped`` / ``context.pr_url``. This
    is intentionally permissive: any indication the task believed it shipped
    something counts. Drift is defined as the gap between these claims and
    the actually-merged PR count.

    Returns 1 for a single claim, 0 for none. We do not multiply by output
    item count — a task either claimed a shipped change or it didn't.
    """
    ctx = task.context or {}
    if task.status != TaskStatus.COMPLETED:
        return 0
    if ctx.get("pr_url") or ctx.get("shipped"):
        return 1
    plan_out = task.plan_output or {}
    if isinstance(plan_out, dict):
        if (
            plan_out.get("results")
            or plan_out.get("findings")
            or plan_out.get("changes")
        ):
            return 1
        if plan_out.get("summary"):
            return 1
    return 0


def _consistency(merged: int, claimed: int) -> float:
    """Fraction of claims that materialized as merged PRs, clipped to [0, 1].

    Returns 1.0 when claimed is 0 (no evidence of drift — neutral).
    """
    if claimed <= 0:
        return 1.0
    return max(0.0, min(1.0, merged / claimed))


def _build_direction_signal(
    claimed: int,
    merged: int,
    *,
    over_claim_min: int | None = None,
    consistency_threshold: float | None = None,
    stall_min: int | None = None,
) -> dict[str, Any]:
    if over_claim_min is None or consistency_threshold is None or stall_min is None:
        _oc_min, _ct, _st_min = _drift_thresholds()
        over_claim_min = over_claim_min if over_claim_min is not None else _oc_min
        consistency_threshold = (
            consistency_threshold if consistency_threshold is not None else _ct
        )
        stall_min = stall_min if stall_min is not None else _st_min
    consistency = _consistency(merged, claimed)
    over_claim = claimed >= over_claim_min and consistency < consistency_threshold
    stalled = claimed >= stall_min and merged == 0
    return {
        "claimed_changes_count": claimed,
        "actually_merged_prs": merged,
        "claim_shipped_consistency": round(consistency, 3),
        "over_claim_flag": bool(over_claim),
        "stalled_despite_churn_flag": bool(stalled),
    }


async def _maybe_create_drift_alerts(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_id: str,
    signal: dict[str, Any],
) -> None:
    """Emit a WARNING Alert for each drift flag; skip if undismissed alert exists."""
    from app.models import Alert, AlertSeverity  # lazy: avoids circular import

    claimed = signal.get("claimed_changes_count", 0)
    merged = signal.get("actually_merged_prs", 0)
    candidates: list[tuple[str, str]] = []
    if signal.get("over_claim_flag"):
        candidates.append(
            (
                f"over-claim drift: direction {direction_id}",
                f"Direction {direction_id} claims {claimed} changes but only {merged} PRs merged in 7d",
            )
        )
    if signal.get("stalled_despite_churn_flag"):
        candidates.append(
            (
                f"stalled-despite-churn drift: direction {direction_id}",
                f"Direction {direction_id} has {claimed} claimed changes but 0 PRs merged in 7d",
            )
        )
    if not candidates:
        return
    # Batch the undismissed-alert existence check into one query (was one query
    # per candidate title) and test membership in-memory; create-only-if-absent
    # semantics are preserved exactly.
    existing_alerts = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.dismissed_at.is_(None),
                    Alert.title.in_([t for t, _ in candidates]),
                )
            )
        )
        .scalars()
        .all()
    )
    existing_titles = {a.title for a in existing_alerts}
    for title, message in candidates:
        if title not in existing_titles:
            session.add(
                Alert(
                    project_id=project_id,
                    severity=AlertSeverity.WARNING,
                    title=title,
                    message=message,
                    event_type="research.direction_drift",
                )
            )


async def compute_direction_drift(
    session: AsyncSession,
    project: Project,
    window_days: int = 7,
) -> dict[str, Any]:
    """Compute short-window claim-vs-shipped drift per direction and per system.

    Args:
      session: an active AsyncSession.
      project: the project to scan; reads project.directions for direction list.
      window_days: rolling lookback window in days.

    Returns:
      {
        "by_direction": {direction_id: signal_dict, ...},
        "by_system":    {system_id:    signal_dict, ...},
        "generated_at": ISO timestamp,
        "window_days":  int,
      }

    The function is best-effort. Missing rows / DB errors yield neutral
    signals so callers can rely on a stable shape.
    """
    generated_at = _utcnow().isoformat()
    over_claim_min, consistency_threshold, stall_min = _drift_thresholds()
    directions = list(project.directions or [])
    by_direction: dict[str, dict[str, Any]] = {}
    by_system_accum: dict[str, dict[str, int]] = {}

    if not directions:
        return {
            "by_direction": {},
            "by_system": {},
            "generated_at": generated_at,
            "window_days": window_days,
        }

    cutoff = _utcnow() - timedelta(days=window_days)

    # Single fetch of recent tasks for this project, joined with their
    # PR artifacts so we can compute both claimed and merged counts in one
    # pass. PR artifacts are queried via Artifact rows; merged-ness is
    # tracked on Task.context.pr_merged (the rollup_management writer
    # stamps that on rollup).
    tasks_in_window = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.created_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )

    # Pre-fetch PR artifacts grouped by task_id for the in-window tasks.
    in_window_task_ids = [t.id for t in tasks_in_window]
    pr_artifact_task_ids: set = set()
    if in_window_task_ids:
        pr_rows = (
            await session.execute(
                select(Artifact.task_id).where(
                    Artifact.task_id.in_(in_window_task_ids),
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        ).all()
        for row in pr_rows:
            pr_artifact_task_ids.add(row[0])

    # Index tasks by direction_id for the window. direction_id can live on
    # task.context (the standard place for autonomous-spawned research) or
    # on the first payload (current_direction_id for research tasks).
    per_direction_claims: dict[str, int] = {}
    per_direction_merged: dict[str, int] = {}
    for t in tasks_in_window:
        ctx = t.context or {}
        dir_id = ctx.get("direction_id")
        if not dir_id:
            payload = t.first_payload or {}
            dir_id = payload.get("current_direction_id")
        if not dir_id:
            continue
        # Gate claims on PR-artifact presence so claims and merges measure the
        # same population: only tasks that opened a PR (have a PULL_REQUEST
        # artifact) can claim a shipped change.  Without this gate, review/
        # research/fix child tasks that share a direction_id but never open a
        # PR of their own inflate claimed_changes_count while never contributing
        # to actually_merged_prs, producing a structurally low consistency ratio.
        if t.id in pr_artifact_task_ids:
            per_direction_claims[dir_id] = per_direction_claims.get(
                dir_id, 0
            ) + _direction_claimed_changes(t)
            # Count a shipped change when the PR actually merged — onto the
            # integration branch (``has_landed_signal``) OR a rollup staging branch
            # (``has_merge_unblock_signal``, the merge-equivalent rollup-unblock
            # bit). Counting integration-only landings was credit-blind: a healthy
            # direction whose children merge to staging (before the rollup lands and
            # re-stamps ``landed_on_integration``) read as ``0 merged`` against a
            # positive ``claimed`` count, producing false ``over_claim`` / ``stalled``
            # drift flags — spending audit budget and poisoning strategist context.
            # ``has_merge_unblock_signal`` subsumes integration landings (they also
            # set the unblock bit) but the OR keeps a legacy row that carries only the
            # explicit ``landed_on_integration=True`` marker counted too. Confirmed
            # non-landings (stranded rollups clear ``pr_merged``) are excluded by both.
            if has_merge_unblock_signal(t) or has_landed_signal(t):
                per_direction_merged[dir_id] = per_direction_merged.get(dir_id, 0) + 1

    for d in directions:
        did = d.get("id")
        if not did:
            continue
        claimed = per_direction_claims.get(did, 0)
        merged = per_direction_merged.get(did, 0)
        signal = _build_direction_signal(
            claimed,
            merged,
            over_claim_min=over_claim_min,
            consistency_threshold=consistency_threshold,
            stall_min=stall_min,
        )
        by_direction[did] = signal
        sid = d.get("system_id")
        if sid:
            sys_acc = by_system_accum.setdefault(sid, {"claimed": 0, "merged": 0})
            sys_acc["claimed"] += claimed
            sys_acc["merged"] += merged

    # Roll up per-system signals from accumulated counts so per-direction
    # neutrals don't dominate the system summary. Empty systems get a
    # neutral signal so consumers can rely on the by_system shape.
    by_system: dict[str, dict[str, Any]] = {}
    for sid, acc in by_system_accum.items():
        by_system[sid] = _build_direction_signal(
            acc["claimed"],
            acc["merged"],
            over_claim_min=over_claim_min,
            consistency_threshold=consistency_threshold,
            stall_min=stall_min,
        )

    # Surface systems that exist on directions but had no in-window data —
    # neutral signal lets the strategic prompt still see the system_id.
    seen_system_ids = set(by_system.keys())
    for d in directions:
        sid = d.get("system_id")
        if sid and sid not in seen_system_ids:
            by_system[sid] = _neutral_signal()
            seen_system_ids.add(sid)

    return {
        "by_direction": by_direction,
        "by_system": by_system,
        "generated_at": generated_at,
        "window_days": window_days,
    }


async def persist_drift_snapshot(
    session: AsyncSession,
    project: Project,
    drift_result: dict[str, Any],
) -> None:
    """Write one DirectionDriftSnapshot row per direction and prune excess history.

    Callers (strategic_spawning, self_audit_dispatch) call this after
    compute_direction_drift() to build the time-series. The session is
    flushed but NOT committed — the caller owns the transaction boundary.

    Prunes rows beyond _DRIFT_SNAPSHOT_RETENTION newest for each direction.
    """
    by_direction = drift_result.get("by_direction", {})
    if not by_direction:
        return

    snapshot_at = _utcnow()
    for direction_id, signal in by_direction.items():
        session.add(
            DirectionDriftSnapshot(
                project_id=project.id,
                direction_id=direction_id,
                snapshot_at=snapshot_at,
                claimed_changes_count=signal.get("claimed_changes_count", 0),
                actually_merged_prs=signal.get("actually_merged_prs", 0),
                claim_shipped_consistency=signal.get("claim_shipped_consistency", 1.0),
                over_claim_flag=bool(signal.get("over_claim_flag", False)),
                stalled_despite_churn_flag=bool(
                    signal.get("stalled_despite_churn_flag", False)
                ),
            )
        )

    await session.flush()

    for direction_id, signal in by_direction.items():
        await _maybe_create_drift_alerts(session, project.id, direction_id, signal)

    # Single pass: use ROW_NUMBER() PARTITION BY direction_id to identify all
    # excess snapshots across all directions in one query, replacing the
    # previous per-direction SELECT+DELETE loop (which was 2N queries for N
    # directions).
    inner = (
        select(
            DirectionDriftSnapshot.id,
            func.row_number()
            .over(
                partition_by=DirectionDriftSnapshot.direction_id,
                order_by=DirectionDriftSnapshot.snapshot_at.desc(),
            )
            .label("rn"),
        )
        .where(DirectionDriftSnapshot.project_id == project.id)
        .subquery()
    )
    excess_ids = (
        (await session.execute(select(inner.c.id).where(inner.c.rn > _DRIFT_SNAPSHOT_RETENTION)))
        .scalars()
        .all()
    )
    if excess_ids:
        await session.execute(
            delete(DirectionDriftSnapshot).where(DirectionDriftSnapshot.id.in_(excess_ids))
        )


async def last_n_drift_snapshots(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_id: str,
    n: int = 10,
) -> list[DirectionDriftSnapshot]:
    """Return the n most-recent drift snapshots for a direction, newest first."""
    return list(
        (
            await session.execute(
                select(DirectionDriftSnapshot)
                .where(
                    DirectionDriftSnapshot.project_id == project_id,
                    DirectionDriftSnapshot.direction_id == direction_id,
                )
                .order_by(DirectionDriftSnapshot.snapshot_at.desc())
                .limit(n)
            )
        )
        .scalars()
        .all()
    )


# Number of snapshots returned by drift_history_series — aligns with the default
# window_days so the sparkline covers one full rolling window.
_DRIFT_HISTORY_SERIES_N = 7


async def drift_history_series(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_id: str,
    n: int = _DRIFT_HISTORY_SERIES_N,
) -> list[dict[str, Any]]:
    """Return serialised drift history for a direction, newest first.

    Each element contains the fields needed to render a sparkline:
        snapshot_at (ISO string), claim_shipped_consistency (float 0-1),
        over_claim_flag (bool), stalled_despite_churn_flag (bool).

    Returns an empty list when no history exists yet.
    """
    snapshots = await last_n_drift_snapshots(session, project_id, direction_id, n=n)
    return [
        {
            "snapshot_at": s.snapshot_at.isoformat(),
            "claim_shipped_consistency": s.claim_shipped_consistency,
            "over_claim_flag": s.over_claim_flag,
            "stalled_despite_churn_flag": s.stalled_despite_churn_flag,
        }
        for s in snapshots
    ]
