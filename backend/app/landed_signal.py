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
from typing import Literal

from sqlalchemy import and_, cast, func, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.ci_resolution import is_degraded_unverified_reason
from app.database import resolve_dialect_name
from app.models import Task, TaskStatus

logger = logging.getLogger(__name__)

# The context key both meanings currently share. Centralised here so the field
# name lives in exactly one place.
LANDED_SIGNAL_KEY = "pr_merged"
LANDED_ON_INTEGRATION_KEY = "landed_on_integration"
NON_MERGE_UNBLOCK_REASON_KEY = "_non_merge_unblock_reason"

# Where the no-CI merge gate stamps WHY a PR merged without real test
# verification. Lives under ``task.context["review_loop"]``; absent on a
# verified/normal merge. The reason KEYS are minted in ``ci_resolution`` and
# classified benign-vs-degraded there (the single source of truth).
REVIEW_LOOP_KEY = "review_loop"
UNVERIFIED_MERGE_REASON_KEY = "unverified_merge_reason"

# Task-level verified-merge classification consumed by success-scoring analytics
# (task_outcomes / direction_outcomes / pipeline_health).
MergeVerificationStatus = Literal[
    "verified", "unverified_benign", "unverified_degraded"
]


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


def _unverified_merge_reason(task: Task) -> str | None:
    """Read ``review_loop["unverified_merge_reason"]`` defensively.

    Returns the stable reason KEY a no-CI fall-open stamped, or ``None`` when the
    merge was verified (no reason) or the context shape is missing/malformed.
    """
    review_loop = (task.context or {}).get(REVIEW_LOOP_KEY)
    if not isinstance(review_loop, dict):
        return None
    reason = review_loop.get(UNVERIFIED_MERGE_REASON_KEY)
    return reason if isinstance(reason, str) and reason else None


def merge_verification_status(task: Task) -> "MergeVerificationStatus | None":
    """Classify HOW a merged completion's PR was verified, for success scoring.

    A PR merging onto the integration branch is necessary but NOT sufficient to
    call a done/fixed claim *proven*: the no-CI merge gate legitimately "falls
    open" and merges some PRs WITHOUT running real tests, recording why in
    ``review_loop["unverified_merge_reason"]``. This predicate reads that reason
    and returns one of:

      - ``"verified"`` — merged completion with NO fall-open reason: a real CI /
        test verdict gated the merge.
      - ``"unverified_benign"`` — merged via a fall-open whose reason is benign
        (the project has no runnable suite, or only positive execution signals
        were obtained). A benign project must NOT be penalized for having no tests.
      - ``"unverified_degraded"`` — merged via a fall-open whose reason is
        *degraded* (verification was expected but an environment/setup failure
        blocked it) per :func:`ci_resolution.is_degraded_unverified_reason`.

    Returns ``None`` for any task that is not a merged completion (the question
    does not apply), so callers can cheaply skip non-shipped tasks. Reuses
    :func:`is_merged_completion` so the "shipped" definition stays identical to
    the rest of analytics; an unknown / unclassified reason key defaults to
    ``"unverified_benign"`` (mirroring the WARNING severity default).
    """
    if not is_merged_completion(task):
        return None
    reason = _unverified_merge_reason(task)
    if reason is None:
        return "verified"
    if is_degraded_unverified_reason(reason):
        return "unverified_degraded"
    return "unverified_benign"


def is_verified_merge(task: Task) -> bool:
    """True iff *task* is a merged completion whose PR was really verified.

    Convenience wrapper over :func:`merge_verification_status`: only the
    ``"verified"`` status counts. Both unverified buckets and non-merged tasks
    return False, so success-scoring can gate a "proven fix" credit on this.
    """
    return merge_verification_status(task) == "verified"


def landed_signal_predicate(session: AsyncSession) -> ColumnElement[bool]:
    """SQL predicate for rows that truly landed on the integration branch.

    Mirrors :func:`has_landed_signal`, including the legacy fallback for rows
    that predate ``landed_on_integration`` and the explicit opt-out marker for
    merge-equivalent non-landings.
    """
    # Async sessions created from a sessionmaker can expose a None ``bind``
    # attribute even though the session still resolves an engine. Routing through
    # the centralised resolver keeps the None-bind-safe resolution (``.bind`` →
    # ``get_bind()`` → ``"postgresql"`` default) in exactly one place so the
    # SQLite branch is chosen correctly on a SQLite-bound session.
    dialect_name = resolve_dialect_name(session)
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
            # ``has_key`` here is SQLAlchemy's JSONB ``?`` operator, not the
            # removed dict method.
            ~Task.context.has_key(LANDED_ON_INTEGRATION_KEY),
            ~Task.context.has_key(NON_MERGE_UNBLOCK_REASON_KEY),
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
        # Augment the merge with the code-task acceptance gate: when the task
        # DECLARED acceptance evidence, evaluate it now (the changed files and
        # merge-verification reason are both on the task) and stamp the verdict
        # durably so success scoring can consume it. Fail-safe: no declaration =>
        # no key written, scoring unchanged. Lazy import avoids a module cycle
        # (acceptance_gate imports this module for its constants).
        try:
            from app.acceptance_gate import record_code_acceptance_verdict

            record_code_acceptance_verdict(task)
        except Exception:  # noqa: BLE001 — gating must never undo a real merge
            logger.warning(
                "[LANDED] code-acceptance verdict recording failed for task %s",
                getattr(task, "id", None),
                exc_info=True,
            )
    elif non_merge_reason:
        mark_unblocked_non_merge(task, reason=non_merge_reason)
    return landed_on_integration
