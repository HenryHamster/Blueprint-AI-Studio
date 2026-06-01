"""Canonical "landed on the integration branch" signal.

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
writer funnels through, plus one canonical reader and one canonical
"completed-and-merged" predicate. Both writers still persist the same
underlying ``pr_merged`` field, so behavior is unchanged — but the call sites
now declare which meaning they intend, which is the seam a later change can use
to diverge the two signals without re-touching every site.
"""

import logging

from app.models import Task, TaskStatus

logger = logging.getLogger(__name__)

# The context key both meanings currently share. Centralised here so the field
# name lives in exactly one place.
LANDED_SIGNAL_KEY = "pr_merged"


def has_landed_signal(task: Task) -> bool:
    """Canonical read of the landed signal.

    Mirrors the historical truthiness check ``(task.context or {}).get("pr_merged")``
    exactly — tolerant of a missing/None context.
    """
    return bool((task.context or {}).get(LANDED_SIGNAL_KEY))


def is_merged_completion(task: Task) -> bool:
    """The canonical "shipped" predicate for analytics.

    True when *task* is COMPLETED and carries the landed signal — the
    ``status == COMPLETED and (ctx or {}).get("pr_merged")`` idiom that
    shipped / merge-rate / direction-outcome logic repeats throughout the
    codebase.
    """
    return task.status == TaskStatus.COMPLETED and has_landed_signal(task)


def mark_landed_on_integration(task: Task) -> None:
    """Stamp the landed signal for a PR that merged onto the integration branch.

    This is the canonical write for a *real* integration-branch merge. Callers
    that need the integration-vs-promotion-target guard should use
    :func:`apply_merged_landed_signal` instead, which routes through here.
    """
    task.context = {**(task.context or {}), LANDED_SIGNAL_KEY: True}


def mark_unblocked_non_merge(task: Task, *, reason: str) -> None:
    """Stamp the unblock marker for a merge-*equivalent* success.

    Used for staging-only rollup children, no-PR / no-op completions, and
    deliberate success-closes where the work landed elsewhere. These never
    landed on the integration branch but must still unblock dependents and
    rollup convergence. Shares the ``pr_merged`` field today (behavior is
    unchanged); the explicit, named call site is the seam for separating it.
    """
    logger.debug(
        "[LANDED] non-merge unblock for task %s: %s",
        getattr(task, "id", None),
        reason,
    )
    task.context = {**(task.context or {}), LANDED_SIGNAL_KEY: True}


def apply_merged_landed_signal(
    task: Task,
    project,
    *,
    base_branch: str,
    hotfix_lane: bool,
) -> bool:
    """Guarded write path for a merged PR.

    Stamps the landed signal only when the merged PR landed on the project's
    integration branch. When the base branch resolves to a distinct promotion
    target, the signal is withheld and the same branch-safety alert the rollup
    boundary raises is staged.

    Returns True iff the merged PR landed on the integration branch.

    This is the single guarded write path the CI-merge sites converge on
    (``orchestrator._apply_ci_merged_landed_signal`` and
    ``ci_completion`` rollup-original propagation).
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
    return landed_on_integration
