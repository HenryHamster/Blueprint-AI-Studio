"""Canonical integration-landing and rollup-unblock signals.

Historically a single ``task.context["pr_merged"]`` flag did two distinct jobs:

  1. mark a PR that truly merged onto the project's *integration* branch, and
  2. mark merge-*equivalent* successes that did NOT land on the integration
     branch — staging-only rollup children (whose PR merges into the rollup
     staging branch), no-PR / no-op completions, and deliberate success-closes
     where the work landed elsewhere.

Conflating the two forced shipped / merge-rate / direction-outcome analytics to
read a flag that overcounts, and forced strategic code to reconstruct the
"really on the integration branch" answer with GitHub ancestry heuristics.

This module gives those two jobs *named, separate* entry points that every
writer funnels through, plus canonical readers for each meaning. The legacy
``pr_merged`` field remains the rollup-unblock / merge-equivalent bit, while
``landed_on_integration`` is the explicit "this work actually landed on the
integration branch" signal used by analytics.
"""

import logging

from sqlalchemy import and_, cast, func, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.models import Task, TaskStatus

logger = logging.getLogger(__name__)

# The context key both meanings currently share. Centralised here so the field
# name lives in exactly one place.
LANDED_SIGNAL_KEY = "pr_merged"
LANDED_ON_INTEGRATION_KEY = "landed_on_integration"
NON_MERGE_UNBLOCK_REASON_KEY = "_non_merge_unblock_reason"


def has_merge_unblock_signal(task: Task) -> bool:
    """Canonical read of the rollup-unblock / merge-equivalent signal."""
    return (task.context or {}).get(LANDED_SIGNAL_KEY) is True


def has_landed_signal(task: Task) -> bool:
    """Canonical read of the integration-branch landing signal.

    New writes stamp ``landed_on_integration`` explicitly. Legacy rows that
    predate the split fall back to ``pr_merged is True`` unless they carry the
    explicit non-merge marker introduced by :func:`mark_unblocked_non_merge`.
    """
    ctx = task.context or {}
    landed = ctx.get(LANDED_ON_INTEGRATION_KEY)
    if landed is True:
        return True
    if landed is False:
        return False
    return (
        ctx.get(LANDED_SIGNAL_KEY) is True
        and NON_MERGE_UNBLOCK_REASON_KEY not in ctx
    )


def is_merged_completion(task: Task) -> bool:
    """The canonical "shipped" predicate for analytics.

    True when *task* is COMPLETED and carries the landed signal — the
    ``status == COMPLETED and (ctx or {}).get("pr_merged")`` idiom that
    shipped / merge-rate / direction-outcome logic repeats throughout the
    codebase.
    """
    return task.status == TaskStatus.COMPLETED and has_landed_signal(task)


def is_unblocked_completion(task: Task) -> bool:
    """True when a completed task should no longer block rollup convergence."""
    return task.status == TaskStatus.COMPLETED and has_merge_unblock_signal(task)


def landed_signal_predicate(session: AsyncSession) -> ColumnElement[bool]:
    """SQL predicate for rows that truly landed on the integration branch.

    Mirrors :func:`has_landed_signal`, including the legacy fallback for rows
    that predate ``landed_on_integration`` and the explicit opt-out marker for
    merge-equivalent non-landings.
    """
    dialect_name = session.bind.dialect.name if session.bind else "postgresql"
    if dialect_name == "sqlite":
        landed = func.json_extract(Task.context, f"$.{LANDED_ON_INTEGRATION_KEY}")
        pr_merged = func.json_extract(Task.context, f"$.{LANDED_SIGNAL_KEY}")
        non_merge_reason = func.json_extract(
            Task.context, f"$.{NON_MERGE_UNBLOCK_REASON_KEY}"
        )
        return or_(
            landed == True,  # noqa: E712
            and_(
                landed.is_(None),
                pr_merged == True,  # noqa: E712
                non_merge_reason.is_(None),
            ),
        )

    landed_true = Task.context.op("@>")(cast({LANDED_ON_INTEGRATION_KEY: True}, JSONB))
    legacy_pr_merged = Task.context.op("@>")(cast({LANDED_SIGNAL_KEY: True}, JSONB))
    return or_(
        landed_true,
        and_(
            legacy_pr_merged,
            ~Task.context.has_key(LANDED_ON_INTEGRATION_KEY),  # noqa: W601
            ~Task.context.has_key(NON_MERGE_UNBLOCK_REASON_KEY),  # noqa: W601
        ),
    )


def mark_landed_on_integration(task: Task) -> None:
    """Stamp the landed signal for a PR that merged onto the integration branch.

    This is the canonical write for a *real* integration-branch merge. Callers
    that need the integration-vs-promotion-target guard should use
    :func:`apply_merged_landed_signal` instead, which routes through here.
    """
    ctx = {
        **(task.context or {}),
        LANDED_SIGNAL_KEY: True,
        LANDED_ON_INTEGRATION_KEY: True,
    }
    ctx.pop(NON_MERGE_UNBLOCK_REASON_KEY, None)
    task.context = ctx


def mark_unblocked_non_merge(task: Task, *, reason: str) -> None:
    """Stamp the unblock marker for a merge-*equivalent* success.

    Used for staging-only rollup children, no-PR / no-op completions, and
    deliberate success-closes where the work landed elsewhere. These never
    landed on the integration branch but must still unblock dependents and
    rollup convergence, so they keep ``pr_merged=True`` while explicitly
    recording ``landed_on_integration=False``.
    """
    logger.debug(
        "[LANDED] non-merge unblock for task %s: %s",
        getattr(task, "id", None),
        reason,
    )
    task.context = {
        **(task.context or {}),
        LANDED_SIGNAL_KEY: True,
        LANDED_ON_INTEGRATION_KEY: False,
        NON_MERGE_UNBLOCK_REASON_KEY: reason,
    }


def apply_merged_landed_signal(
    task: Task,
    project,
    *,
    base_branch: str,
    hotfix_lane: bool,
    non_merge_reason: str | None = None,
) -> bool:
    """Guarded write path for a merged PR.

    Stamps the landed signal only when the merged PR landed on the project's
    integration branch. When the base branch resolves to a distinct promotion
    target, the signal is withheld and the same branch-safety alert the rollup
    boundary raises is staged.

    Returns True iff the merged PR landed on the integration branch.

    When *non_merge_reason* is provided, merged PRs that landed somewhere other
    than the integration branch are still marked as merge-equivalent rollup
    unblocks via :func:`mark_unblocked_non_merge`.
    """
    from app.branch_resolution import _signal_blocking_project_branch_configuration
    from app.shared_state import (
        get_project_integration_branch,
        get_project_promotion_target,
    )

    integration_branch = get_project_integration_branch(project)
    promotion_target = get_project_promotion_target(project)
    landed_on_integration = bool(
        integration_branch and base_branch == integration_branch
    )
    if (
        promotion_target
        and base_branch == promotion_target
        and base_branch != integration_branch
        and not hotfix_lane
        and project is not None
    ):
        _signal_blocking_project_branch_configuration(
            project,
            "rollup_target_matches_promotion_target",
            (
                f"Rollup blocked: target branch '{base_branch}' is this "
                "project's promotion target. Configure a distinct "
                "integration branch before landing fleet work."
            ),
        )
    if landed_on_integration:
        mark_landed_on_integration(task)
    elif non_merge_reason:
        mark_unblocked_non_merge(task, reason=non_merge_reason)
    return landed_on_integration
