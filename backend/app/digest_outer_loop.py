"""Outer-loop convergence helpers extracted from ``app.digest_metrics``.

Pure functions over already-loaded Task / Alert data that group review<->fix
PR-cycle iteration-tasks into PR-chains, parse review-fix escalation Alerts, and
resolve each chain's single terminal outcome. They are shared by the convergence
aggregate (``compute_outer_loop_convergence``) and the chain drill-down
(``_resolve_outer_loop_chains``) plus the lane-waste diagnostics, so every
consumer reads chains and outcomes identically.

``app.digest_metrics`` re-exports these symbols so existing import paths (and
``mock.patch`` targets) are unchanged.
"""

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Dict, Any, List, Optional

from app.landed_signal import has_landed_signal
from app.models import Alert, Task

# Reverse of alert_dispatch._ESCALATION_REASON_LABELS — maps the human-readable
# label embedded in a "Review-fix escalation: <label> on PR #N" Alert title back
# to its canonical reason key. Kept in lockstep with alert_dispatch; the label
# strings are part of the deduplicated alert title, so they are stable.
_ESCALATION_TITLE_PREFIX = "Review-fix escalation: "
_ESCALATION_LABEL_TO_REASON = {
    "stuck cycle": "stuck",
    "dispute standoff": "standoff",
    "repeat-fingerprint halt": "repeat-fingerprint",
}
_TERMINAL_OUTCOME_KEY = "review_loop_terminal_outcome"
_TERMINAL_OUTCOME_KEYS = frozenset(
    {
        "merged",
        "rejected",
        "cap_exhausted",
        "stuck",
        "standoff",
        "repeat_fingerprint_halt",
    }
)
_ESCALATION_OUTCOME_KEYS = frozenset({"stuck", "standoff", "repeat_fingerprint_halt"})
_CAP_EXHAUSTED_DIRECTION_LIMIT = 20


def _outer_loop_pct(values: List[int], p: int) -> Optional[int]:
    """Nearest-rank percentile over a list of ints (None when empty)."""
    if not values:
        return None
    s = sorted(values)
    return s[min(int(len(s) * p / 100), len(s) - 1)]


def _outer_loop_chain_key(t: "Task") -> str:
    """Stable PR-chain identifier for a review-loop task.

    A single PR that takes k review<->fix cycles spawns k Task rows, each
    carrying ``review_loop`` with the SAME ``original_task_id`` (and
    ``pr_number``) but an incremented ``iteration``. Grouping on this key
    collapses those k iteration-tasks back into one PR-chain. Falls back to
    ``pr_number`` and then the task's own id so a lone iteration with neither
    key is still its own chain.
    """
    rl = (t.context or {}).get("review_loop") or {}
    oid = rl.get("original_task_id")
    if oid:
        return f"orig:{oid}"
    pr = rl.get("pr_number")
    if pr is not None:
        return f"pr:{pr}"
    return f"task:{t.id}"


def _outer_loop_terminal_outcome(t: "Task") -> Optional[str]:
    """Return the structured or legacy terminal outcome for a task."""
    ctx = t.context or {}
    marker = ctx.get(_TERMINAL_OUTCOME_KEY)
    if isinstance(marker, dict):
        kind = marker.get("kind") or marker.get("outcome")
        if kind == "repeat-fingerprint":
            kind = "repeat_fingerprint_halt"
        if kind in _TERMINAL_OUTCOME_KEYS:
            return kind
    if has_landed_signal(t):
        return "merged"
    if (ctx or {}).get("failure_detail") == "review_cap_exhausted":
        return "cap_exhausted"
    if (ctx or {}).get("blocked_repeat_fingerprint"):
        return "repeat_fingerprint_halt"
    return None


def _group_outer_loop_chains(all_tasks: List[Task], window_start: "datetime") -> tuple:
    """Group in-window review-loop tasks into PR-chains.

    Returns ``(tasks_by_id, chains, task_id_to_chain)`` where ``chains`` maps a
    chain key (see ``_outer_loop_chain_key``) to its iteration-tasks and
    ``task_id_to_chain`` maps every iteration-task id AND each chain's
    ``original_task_id`` (both str and UUID forms) back to the chain key, so an
    escalation Alert raised against the original resolves to the right chain.
    Shared by ``compute_outer_loop_convergence`` and ``_resolve_outer_loop_chains``.
    """
    tasks_by_id = {str(t.id): t for t in all_tasks}
    loop_tasks = [
        t
        for t in all_tasks
        if isinstance((t.context or {}).get("review_loop"), dict)
        and (t.created_at is None or t.created_at >= window_start)
    ]
    chains: Dict[str, List[Task]] = defaultdict(list)
    task_id_to_chain: Dict[Any, str] = {}
    for t in loop_tasks:
        key = _outer_loop_chain_key(t)
        chains[key].append(t)
        task_id_to_chain[t.id] = key
        rl = (t.context or {}).get("review_loop") or {}
        oid = rl.get("original_task_id")
        if oid:
            task_id_to_chain[oid] = key
            try:
                task_id_to_chain[uuid.UUID(str(oid))] = key
            except Exception:
                pass
    return tasks_by_id, chains, task_id_to_chain


def _scan_escalation_alerts(
    escalation_alerts: List[Alert], task_id_to_chain: Dict[Any, str]
) -> tuple:
    """Parse review-fix escalation Alerts into chain-level reason data.

    Returns ``(alert_reasons_by_chain, escalation_by_reason, escalated_task_ids)``.
    A given ``(task_id, reason)`` pair is counted once in ``escalation_by_reason``
    even when the same alert title recurs, so the histogram never over-counts
    relative to the distinct-task escalation total. Shared by the convergence
    aggregate and the chain drill-down so both read escalations identically.
    """
    escalation_by_reason: Dict[str, int] = {
        "stuck": 0,
        "standoff": 0,
        "repeat-fingerprint": 0,
    }
    alert_reasons_by_chain: Dict[str, set] = defaultdict(set)
    seen_task_reason: set = set()
    escalated_task_ids: set = set()
    for alert in escalation_alerts:
        title = alert.title or ""
        if not title.startswith(_ESCALATION_TITLE_PREFIX):
            continue
        if alert.task_id is not None:
            escalated_task_ids.add(alert.task_id)
        for label, reason in _ESCALATION_LABEL_TO_REASON.items():
            if f"{_ESCALATION_TITLE_PREFIX}{label} " in title:
                pair = (alert.task_id, reason)
                if pair not in seen_task_reason:
                    seen_task_reason.add(pair)
                    escalation_by_reason[reason] += 1
                    if alert.task_id is not None:
                        alert_reasons_by_chain[
                            task_id_to_chain.get(alert.task_id, f"task:{alert.task_id}")
                        ].add(reason)
                break
    return alert_reasons_by_chain, escalation_by_reason, escalated_task_ids


def _resolve_chain_outcome(
    outcome_tasks: List[Task], chain_reasons: set
) -> Optional[str]:
    """Resolve a PR-chain's single terminal outcome.

    Definitive terminal states (merged > cap-exhausted > rejected) win over a
    possibly stale structured ``review_loop_terminal_outcome`` marker; the
    alert-derived escalation reasons (standoff > stuck > repeat-fingerprint) are
    the last-resort fallback. This priority is the SAME one
    ``compute_outer_loop_convergence`` used inline — extracted so the drill-down
    endpoint reports outcomes identical to the aggregate. Critically, because
    ``merged`` is checked first, an escalated-then-merged chain resolves to
    ``merged`` and is never attributed to its prior escalation reason.
    """
    if any(has_landed_signal(t) for t in outcome_tasks):
        return "merged"
    if any(
        (t.context or {}).get("failure_detail") == "review_cap_exhausted"
        for t in outcome_tasks
    ):
        return "cap_exhausted"
    if any((t.context or {}).get("pr_rejected") is True for t in outcome_tasks):
        return "rejected"
    for t in outcome_tasks:
        structured_outcome = _outer_loop_terminal_outcome(t)
        if structured_outcome is not None:
            return structured_outcome
    if "standoff" in chain_reasons:
        return "standoff"
    if "stuck" in chain_reasons:
        return "stuck"
    if "repeat-fingerprint" in chain_reasons:
        return "repeat_fingerprint_halt"
    return None


def _outer_loop_is_rollup_chain(
    chain_tasks: List[Task], outcome_tasks: List[Task]
) -> bool:
    """Return whether a chain carries the canonical rollup lineage marker."""
    # Keep the predicate in review_pipeline authoritative.  The local import
    # avoids making this pure digest helper part of review-pipeline imports.
    from app.review_pipeline import _is_rollup_chain_source

    return any(
        _is_rollup_chain_source(task)
        for task in [*chain_tasks, *outcome_tasks]
    )


def _outer_loop_direction_text(
    direction_id: Optional[str], chain_tasks: List[Task], outcome_tasks: List[Task]
) -> Optional[str]:
    """Find the bounded human-readable label attached to a direction."""
    if not direction_id:
        return None
    for task in [*chain_tasks, *outcome_tasks]:
        context = task.context or {}
        payload = task.first_payload or {}
        candidate = (
            context.get("direction_text")
            or context.get("direction_name")
            or payload.get("direction_text")
            or payload.get("direction_name")
        )
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:200]
    return None


def _bounded_direction_records(
    direction_records: Dict[str, Optional[str]], limit: int = _CAP_EXHAUSTED_DIRECTION_LIMIT
) -> List[Dict[str, Optional[str]]]:
    """Return deterministic, bounded cap-loss direction records."""
    return [
        {"direction_id": direction_id, "direction_text": direction_text}
        for direction_id, direction_text in sorted(direction_records.items())[:limit]
    ]


def _outer_loop_review_stats(values: List[int]) -> Dict[str, Any]:
    """Summarize a reviews-to-converge population without truncating it."""
    return {
        "count": len(values),
        "sample_count": len(values),
        "median": _outer_loop_pct(values, 50),
        "p95": _outer_loop_pct(values, 95),
    }


def _outer_loop_chain_observation(
    key: str,
    chain_tasks: List[Task],
    tasks_by_id: Dict[str, Task],
    chain_reasons: set,
) -> Dict[str, Any]:
    """Reduce one PR-chain's iteration-tasks to a single observation row.

    Folds the chain-original task (which carries terminal flags like
    ``pr_merged`` but usually no ``review_loop``) into the outcome scan, then
    computes cycles-to-merge (``max(iteration)``), churn (``max`` of the running
    ``churn_detections`` total), drift presence, the resolved terminal outcome,
    and identifying context (``pr_number``, ``task_ids``, ``project_id``). Both
    the convergence aggregate and the drill-down endpoint build from this row.
    """
    outcome_tasks = list(chain_tasks)
    rl0 = (chain_tasks[0].context or {}).get("review_loop") or {}
    oid = rl0.get("original_task_id")
    if oid and str(oid) in tasks_by_id:
        orig = tasks_by_id[str(oid)]
        if orig not in outcome_tasks:
            outcome_tasks.append(orig)

    chain_iters: List[int] = []
    chain_churn = 0
    chain_has_drift = False
    pr_number = rl0.get("pr_number")
    direction_id = None
    review_count = 0
    review_loop_pr_numbers: set[str] = set()
    superseded_pr_numbers: set[str] = set()
    for t in chain_tasks:
        context = t.context or {}
        if direction_id is None and context.get("direction_id"):
            direction_id = str(context["direction_id"])
        rl = (t.context or {}).get("review_loop") or {}
        pr = rl.get("pr_number")
        if pr is not None and str(pr).strip():
            review_loop_pr_numbers.add(str(pr).strip())
        prior_prs = rl.get("superseded_pr_numbers")
        if isinstance(prior_prs, list):
            superseded_pr_numbers.update(
                str(prior_pr).strip()
                for prior_pr in prior_prs
                if prior_pr is not None and str(prior_pr).strip()
            )
        # Legacy retarget receipts predate the durable lineage field.  Reading
        # them keeps a currently-retargeted task correctly classified while
        # successor tasks use ``superseded_pr_numbers`` above.
        legacy_retarget = rl.get("head_drift_retargeted_from")
        if isinstance(legacy_retarget, dict):
            prior_pr = legacy_retarget.get("pr_number")
            if prior_pr is not None and str(prior_pr).strip():
                superseded_pr_numbers.add(str(prior_pr).strip())
        if pr_number is None and rl.get("pr_number") is not None:
            pr_number = rl.get("pr_number")
        it = rl.get("iteration")
        if isinstance(it, (int, float)) and it >= 1:
            chain_iters.append(int(it))
        # churn_detections is a running total carried forward across iterations,
        # so the chain's churn is the MAX seen (summing would double-count).
        chain_churn = max(chain_churn, int(rl.get("churn_detections") or 0))
        drift = rl.get("drift_signal")
        if isinstance(drift, dict) and (
            drift.get("drift_in") or drift.get("drift_out")
        ):
            chain_has_drift = True
        if (t.first_payload or {}).get("type") == "review":
            review_count += 1

    if direction_id is None:
        for t in outcome_tasks:
            candidate = (t.context or {}).get("direction_id")
            if candidate:
                direction_id = str(candidate)
                break

    outcome = _resolve_chain_outcome(outcome_tasks, chain_reasons)
    chain_pr_numbers = review_loop_pr_numbers | superseded_pr_numbers
    supersede_affected = len(chain_pr_numbers) > 1
    is_rollup = _outer_loop_is_rollup_chain(chain_tasks, outcome_tasks)
    direction_text = _outer_loop_direction_text(
        direction_id, chain_tasks, outcome_tasks
    )

    project_id = None
    for t in outcome_tasks:
        if getattr(t, "project_id", None) is not None:
            project_id = str(t.project_id)
            break

    return {
        "chain_key": key,
        "outcome": outcome,
        "max_iteration": max(chain_iters) if chain_iters else None,
        "churn_count": chain_churn,
        "has_drift": chain_has_drift,
        "direction_id": direction_id,
        "direction_text": direction_text,
        "is_rollup": is_rollup,
        "review_count": review_count,
        "reviews_to_converge": review_count if outcome == "merged" else None,
        # A recreated PR remains one chain because the original-task identity
        # is stable. Retained prior PRs make supersession observable after the
        # original iteration tasks have aged out of the reporting window.
        "review_loop_pr_numbers": sorted(chain_pr_numbers),
        "supersede_affected": supersede_affected,
        "original_task_id": str(oid) if oid else None,
        "pr_number": pr_number,
        "task_ids": [str(t.id) for t in chain_tasks],
        "project_id": project_id,
    }


def _resolve_outer_loop_chains(
    all_tasks: List[Task],
    escalation_alerts: List[Alert],
    window_start: "datetime",
) -> List[Dict[str, Any]]:
    """Reduce in-window review-loop tasks to one observation row per PR-chain.

    Pure function — no database calls. Reuses the same grouping
    (``_outer_loop_chain_key``), escalation-alert parsing, and per-chain outcome
    resolution as ``compute_outer_loop_convergence``, so the chain drill-down
    endpoint reports outcomes identical to the aggregate summary. Rows are
    newest-first by the chain's most recent review_loop task creation time.
    """
    tasks_by_id, chains, task_id_to_chain = _group_outer_loop_chains(
        all_tasks, window_start
    )
    alert_reasons_by_chain, _by_reason, _escalated_ids = _scan_escalation_alerts(
        escalation_alerts, task_id_to_chain
    )

    rows: List[Dict[str, Any]] = []
    for key, chain_tasks in chains.items():
        obs = _outer_loop_chain_observation(
            key, chain_tasks, tasks_by_id, alert_reasons_by_chain.get(key, set())
        )
        latest_created = None
        for t in chain_tasks:
            created = t.created_at
            if created is not None and (
                latest_created is None or created > latest_created
            ):
                latest_created = created
        obs["_latest_created"] = latest_created
        rows.append(obs)

    rows.sort(key=lambda r: r["_latest_created"] or datetime.min, reverse=True)
    for r in rows:
        r.pop("_latest_created", None)
    return rows
