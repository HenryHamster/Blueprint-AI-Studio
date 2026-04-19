"""Retry-budget helpers for parent task re-evaluation.

Extracted from ``task_lifecycle.py`` to keep that module under its size
guardrail. Three pieces live here:

- :func:`compute_effective_max_requeues` shrinks the parent retry budget when a
  child has already failed twice in a row with the same non-infra failure
  class — that pattern usually means the retry is addressing the symptom, not
  the root cause. INFRA classes are exempt because transient failures can
  legitimately repeat.
- :func:`classify_override_reason` maps a failure-class + clone-context pair
  to a provenance label for scope overrides applied during retry. Used by
  ``direction_outcomes`` to distinguish tasks that were originally spawned at
  a given scope from those that reached it via retry escalation/downgrade.
- :data:`CODE_ERROR_STAGE_GUIDANCE` provides per-pipeline-stage retry hints
  for CODE_ERROR failures, used by ``_build_retry_prompt_suffix``.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.failure_classification import (
    AGENT_TIMEOUT,
    CODE_ERROR,
    INFRA_CLASSES,
    SCOPE_OVERFLOW,
    classify_failure,
)
from app.models import Task, TaskStatus


CODE_ERROR_STAGE_GUIDANCE: dict[str, str] = {
    "test": (
        "The previous attempt failed at the test stage. Focus on the specific failing tests "
        "shown in the error log and consider a fundamentally different approach if the same "
        "tests keep failing."
    ),
    "lint": (
        "The previous attempt failed at the lint stage. Run the linter locally before committing "
        "and check for type errors (ruff, eslint, tsc --noEmit)."
    ),
    "build": (
        "The previous attempt failed at the build stage. Verify import paths and module resolution, "
        "and ensure any new files are wired into the build correctly."
    ),
    "merge": (
        "The previous attempt failed at the merge stage. Check for merge conflicts with the target "
        "branch and rebase onto the latest base before retrying."
    ),
    "review": (
        "The previous attempt failed during code review. Focus on addressing the specific review "
        "feedback without introducing new issues or unrelated changes."
    ),
    "clone": (
        "The previous attempt failed during git clone/checkout. This is likely a transient "
        "infrastructure issue or a branch reference problem — verify the branch exists and is "
        "accessible before retrying."
    ),
    "pr_creation": (
        "The previous attempt failed while creating the pull request. Check for duplicate PRs, "
        "verify the branch has commits ahead of the base, and ensure the base branch is correct."
    ),
}


async def compute_effective_max_requeues(
    parent: Task, session: AsyncSession, max_requeues: int
) -> int:
    """Return the max requeue count, reduced by 1 if any failed child repeats
    its prior non-infra failure class."""
    children = (await session.execute(
        select(Task).where(
            Task.parent_task_id == parent.id,
            Task.status == TaskStatus.FAILED,
        )
    )).scalars().all()
    for child in children:
        ctx = child.context or {}
        current_class = classify_failure(child, logs=ctx.get("last_error") or None)
        prev_class = ctx.get("_previous_failure_class")
        if prev_class and current_class == prev_class and current_class not in INFRA_CLASSES:
            return max_requeues - 1
        history = ctx.get("_failure_class_history") or []
        if isinstance(history, list):
            counts: dict[str, int] = {}
            for cls in history:
                if isinstance(cls, str) and cls and cls not in INFRA_CLASSES:
                    counts[cls] = counts.get(cls, 0) + 1
                    if counts[cls] >= 2:
                        return max_requeues - 1
    return max_requeues


def classify_override_reason(failure_class: str, clone_ctx: dict) -> str:
    """Return a provenance label for a scope override applied during retry."""
    if failure_class == AGENT_TIMEOUT:
        return "timeout_downgrade"
    if failure_class == SCOPE_OVERFLOW or (
        (clone_ctx.get("failure_category") or "").lower() == "scope_overflow"
    ):
        return "scope_overflow_escalation"
    if failure_class == CODE_ERROR:
        return "code_error_downgrade"
    return "retry_override"
