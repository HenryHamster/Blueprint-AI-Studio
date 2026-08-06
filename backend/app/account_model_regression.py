"""Per-(provider, model) success-rate regression detection helpers.

Extracted from admin_pool_accounts.py so the quarantine wiring in
max_plan_pool.py can call find_account_model_regressions without importing
the full router module.

Infra failures (PROVISION_FAILURE, RATE_LIMIT, AGENT_TIMEOUT, EXTERNAL_API,
SCOPE_OVERFLOW) are excluded from both numerator and denominator — they reflect
infrastructure issues, not account/model quality.
"""

from __future__ import annotations

import contextlib
import math
import uuid as _uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.failure_classification import INFRA_ANALYTICS_CLASSES
from app.models import MaxPlanAccount, Task, TaskStatus, _utcnow

# Minimum total tasks for a (provider, model) group to appear in the breakdown.
_PROVIDER_MODEL_MIN_SAMPLE = 5
# Minimum per-(account, provider, model) tasks to be eligible for regression detection.
# Raised from 3 to 10 to ensure statistical significance before quarantining.
_ACCOUNT_MIN_SAMPLE = 10
# Minimum distinct accounts in a (provider, model) group before regression can fire.
# Requires >= 3 so the pool comparator is never the degenerate average-of-two.
_DISTINCT_ACCOUNTS_MIN = 3
# Confidence level for the one-sided Wilson score interval (95% → z=1.645).
# Regression fires only when the account's Wilson upper bound is strictly below
# the pool's Wilson lower bound, i.e. the gap is statistically significant.
_WILSON_Z = 1.645
# Regression triggers when account_rate < project_median * this threshold.
# Kept for the display fields on RegressionCandidate; the statistical gate is
# the primary firing condition now.
REGRESSION_RELATIVE_THRESHOLD = 0.8
# Regression also requires this absolute gap: project_median - account_rate >= threshold.
REGRESSION_ABSOLUTE_THRESHOLD = 0.15


def _wilson_lower(successes: int, n: int, z: float = _WILSON_Z) -> float:
    """Wilson score confidence interval lower bound for a proportion.

    Returns the lower bound of the one-sided (1-alpha) CI for the true success
    probability p given ``successes`` out of ``n`` trials.

    When n == 0 returns 0.0.
    """
    if n == 0:
        return 0.0
    p_hat = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p_hat + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p_hat * (1 - p_hat) / n + z2 / (4 * n * n))
    return max(0.0, centre - margin)


def _wilson_upper(successes: int, n: int, z: float = _WILSON_Z) -> float:
    """Wilson score confidence interval upper bound for a proportion."""
    if n == 0:
        return 1.0
    p_hat = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p_hat + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p_hat * (1 - p_hat) / n + z2 / (4 * n * n))
    return min(1.0, centre + margin)


@dataclass
class RegressionCandidate:
    account_id: str
    provider: str
    model: str
    account_rate: float
    project_median: float
    sample_size: int
    distinct_accounts: int


async def compute_provider_model_breakdown(
    session: AsyncSession,
    account_ids: list[str],
    *,
    expand_accounts: bool = False,
    project_id: _uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Compute rolling 7-day per-(provider, model) success-rate breakdown.

    Infra failures are excluded from both numerator and denominator.
    When expand_accounts=True, each row includes per-account drill-in with
    below_median flags and quarantine_status (read from MaxPlanAccount.rate_limit_state).
    When project_id is supplied, only tasks belonging to that project are included.
    """
    if not account_ids:
        return []

    cutoff = _utcnow() - timedelta(days=7)
    terminal = [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
    where_clauses = [
        Task.created_at >= cutoff,
        Task.status.in_(terminal),
        Task.provider.isnot(None),
        Task.model.isnot(None),
        Task.account_id.in_(account_ids),
    ]
    if project_id is not None:
        where_clauses.append(Task.project_id == project_id)
    stmt = select(
        Task.provider, Task.model, Task.account_id, Task.status, Task.context
    ).where(*where_clauses)
    task_rows = (await session.execute(stmt)).all()

    # Aggregate per (provider, model, account_id), skipping infra failures.
    # UNPLACEABLE_EXECUTION_SITE is included in the skip set via
    # INFRA_ANALYTICS_CLASSES: the task never ran, so it must not depress a
    # provider/model/account's success rate.
    raw: dict[tuple[str, str], dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"total": 0, "successes": 0})
    )
    for provider, model, account_id, status, ctx in task_rows:
        fc = (ctx if isinstance(ctx, dict) else {}).get("failure_category", "")
        if fc in INFRA_ANALYTICS_CLASSES:
            continue
        raw[(provider, model)][account_id or ""]["total"] += 1
        if status == TaskStatus.COMPLETED:
            raw[(provider, model)][account_id or ""]["successes"] += 1

    # Fetch quarantine state for expand_accounts mode.
    rl_state_by_account: dict[str, dict[str, Any]] = {}
    if expand_accounts and account_ids:
        valid_uuids = []
        for aid in account_ids:
            with contextlib.suppress(ValueError):
                valid_uuids.append(_uuid.UUID(aid))
        if valid_uuids:
            rl_rows = (
                await session.execute(
                    select(MaxPlanAccount.id, MaxPlanAccount.rate_limit_state).where(
                        MaxPlanAccount.id.in_(valid_uuids)
                    )
                )
            ).all()
            for acct_id, rl_state in rl_rows:
                rl_state_by_account[str(acct_id)] = rl_state or {}

    result: list[dict[str, Any]] = []
    for (provider, model), acct_dict in raw.items():
        accounts = [
            {"account_id": aid, "total": d["total"], "successes": d["successes"]}
            for aid, d in acct_dict.items()
        ]
        total_tasks = sum(a["total"] for a in accounts)
        if total_tasks < _PROVIDER_MODEL_MIN_SAMPLE:
            continue
        success_count = sum(a["successes"] for a in accounts)
        per_acct_rates = sorted(
            a["successes"] / a["total"] for a in accounts if a["total"] >= 3
        )
        if per_acct_rates:
            n = len(per_acct_rates)
            median = (
                per_acct_rates[n // 2]
                if n % 2
                else (per_acct_rates[n // 2 - 1] + per_acct_rates[n // 2]) / 2
            )
        else:
            median = success_count / total_tasks

        entry: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "total_tasks": total_tasks,
            "success_count": success_count,
            "success_rate": round(success_count / total_tasks, 4),
            "distinct_accounts": len(
                {a["account_id"] for a in accounts if a["account_id"]}
            ),
            "project_median_success_rate": round(median, 4),
        }
        if expand_accounts:
            entry["accounts"] = []
            for a in sorted(accounts, key=lambda x: x["total"], reverse=True):
                acct_rate = round(a["successes"] / a["total"], 4) if a["total"] else 0.0
                below_median = (
                    a["total"] >= 3 and (a["successes"] / a["total"]) < median * 0.8
                )
                quarantine_status = _quarantine_status(
                    rl_state_by_account, a["account_id"], model
                )
                entry["accounts"].append(
                    {
                        "account_id": a["account_id"],
                        "total_tasks": a["total"],
                        "success_count": a["successes"],
                        "success_rate": acct_rate,
                        "below_median": below_median,
                        "quarantine_status": quarantine_status,
                    }
                )
        result.append(entry)

    result.sort(key=lambda x: x["total_tasks"], reverse=True)
    return result


def _quarantine_status(
    rl_state_by_account: dict[str, dict[str, Any]],
    account_id: str,
    model: str,
) -> dict[str, Any] | None:
    """Return the regression quarantine entry for (account, model), or None."""
    rl_state = rl_state_by_account.get(account_id, {})
    dm = rl_state.get("disabled_models", {})
    entry = dm.get(model)
    if entry and entry.get("kind") == "regression":
        return {
            "kind": "regression",
            "started_at": entry.get("at"),
            "account_rate": entry.get("account_rate"),
            "project_median": entry.get("project_median"),
            "sample_size": entry.get("sample_size"),
        }
    return None


async def find_account_model_regressions(
    session: AsyncSession,
    account_ids: list[str],
) -> list[RegressionCandidate]:
    """Return (account, provider, model) triples that meet regression criteria.

    A triple is a candidate iff ALL hold:
    - (provider, model) group has distinct_accounts >= _DISTINCT_ACCOUNTS_MIN (3),
      so the pool comparator is never the degenerate average-of-two
    - (provider, model) group has total_tasks >= _PROVIDER_MODEL_MIN_SAMPLE (5)
    - (account, provider, model) cell has total_tasks >= _ACCOUNT_MIN_SAMPLE (10)
    - Statistical significance gate: the account's Wilson upper bound (95%) is
      strictly below the pool's Wilson lower bound, i.e. the performance gap is
      unlikely to be sampling noise. The pool baseline is built from the OTHER
      evaluated accounts only (each with >= _ACCOUNT_MIN_SAMPLE tasks); micro-
      accounts below the floor never contaminate the comparator, and the pool
      itself must clear _ACCOUNT_MIN_SAMPLE tasks before any gap can fire.
    - Legacy absolute/relative thresholds are also checked to preserve the
      existing behaviour for clearly bad accounts and avoid false positives at
      the margin of the confidence interval

    The statistical gate replaces the old raw-ratio/median-of-two comparison
    that could quarantine a healthy account after just 3 tasks against a single
    comparator account (the documented HenryHamster false-positive class).
    """
    rows = await compute_provider_model_breakdown(
        session, account_ids, expand_accounts=True
    )
    candidates: list[RegressionCandidate] = []
    for row in rows:
        if row["distinct_accounts"] < _DISTINCT_ACCOUNTS_MIN:
            continue
        median = row["project_median_success_rate"]

        accounts_in_row = row.get("accounts", [])

        # Build the pool baseline from the EVALUATED accounts only — accounts
        # with fewer than _ACCOUNT_MIN_SAMPLE tasks are skipped in the detection
        # loop, so their (noisy) tasks must not contaminate the comparator
        # either. row["success_count"]/row["total_tasks"] include every account
        # regardless of sample size, so we cannot reuse them here.
        eligible = [
            a for a in accounts_in_row if a["total_tasks"] >= _ACCOUNT_MIN_SAMPLE
        ]
        eligible_successes = sum(a["success_count"] for a in eligible)
        eligible_total = sum(a["total_tasks"] for a in eligible)

        for acct in eligible:
            total = acct["total_tasks"]
            successes = acct["success_count"]
            rate = acct["success_rate"]

            # Legacy threshold check: must still fail both the relative and
            # absolute gaps so clearly-bad accounts are always caught.
            if not (
                rate < median * REGRESSION_RELATIVE_THRESHOLD
                and (median - rate) >= REGRESSION_ABSOLUTE_THRESHOLD
            ):
                continue

            # Statistical significance gate: compare this account's Wilson
            # upper bound against the pool (all OTHER evaluated accounts) Wilson
            # lower bound. If the account's upper bound is still below the
            # pool's lower bound the gap is statistically significant at ~95%.
            pool_successes = eligible_successes - successes
            pool_total = eligible_total - total

            # Min-sample guard on the pool: with too few comparator tasks the
            # Wilson lower bound is meaningless (it can be driven near zero by a
            # single comparator account), so no gap is ever "significant".
            if pool_total < _ACCOUNT_MIN_SAMPLE:
                continue

            acct_upper = _wilson_upper(successes, total)
            pool_lower = _wilson_lower(pool_successes, pool_total)
            if acct_upper >= pool_lower:
                # Gap is not statistically significant — skip.
                continue

            candidates.append(
                RegressionCandidate(
                    account_id=acct["account_id"],
                    provider=row["provider"],
                    model=row["model"],
                    account_rate=rate,
                    project_median=median,
                    sample_size=total,
                    distinct_accounts=row["distinct_accounts"],
                )
            )
    return candidates
