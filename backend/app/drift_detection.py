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
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Artifact, ArtifactType, Project, Task, TaskStatus
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


_OVER_CLAIM_MIN_CLAIMS = 3
_OVER_CLAIM_CONSISTENCY_THRESHOLD = 0.4
_STALL_MIN_CLAIMS = 5


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
        if plan_out.get("results") or plan_out.get("findings") or plan_out.get("changes"):
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


def _build_direction_signal(claimed: int, merged: int) -> dict[str, Any]:
    consistency = _consistency(merged, claimed)
    over_claim = (
        claimed >= _OVER_CLAIM_MIN_CLAIMS
        and consistency < _OVER_CLAIM_CONSISTENCY_THRESHOLD
    )
    stalled = claimed >= _STALL_MIN_CLAIMS and merged == 0
    return {
        "claimed_changes_count": claimed,
        "actually_merged_prs": merged,
        "claim_shipped_consistency": round(consistency, 3),
        "over_claim_flag": bool(over_claim),
        "stalled_despite_churn_flag": bool(stalled),
    }


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
        await session.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.created_at >= cutoff,
            )
        )
    ).scalars().all()

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
        per_direction_claims[dir_id] = per_direction_claims.get(dir_id, 0) + _direction_claimed_changes(t)
        # Merged PR: must have a PULL_REQUEST artifact AND context.pr_merged
        # truthy. The latter is set by the rollup verification step, so it's
        # the authoritative "shipped" signal — pure artifact existence
        # would over-count rejected PRs.
        if t.id in pr_artifact_task_ids and ctx.get("pr_merged"):
            per_direction_merged[dir_id] = per_direction_merged.get(dir_id, 0) + 1

    for d in directions:
        did = d.get("id")
        if not did:
            continue
        claimed = per_direction_claims.get(did, 0)
        merged = per_direction_merged.get(did, 0)
        signal = _build_direction_signal(claimed, merged)
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
        by_system[sid] = _build_direction_signal(acc["claimed"], acc["merged"])

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
