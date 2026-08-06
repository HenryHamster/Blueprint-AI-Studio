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

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.failure_classification import (
    AGENT_TIMEOUT,
    CODE_ERROR,
    INFRA_ANALYTICS_CLASSES,
    SCOPE_OVERFLOW,
    classify_failure,
)
from app.fix_loop_telemetry import has_terminal_divergence
from app.models import Task, TaskStatus
from app.time_utils import _utcnow


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

FAILURE_CLASS_HISTORY_TTL = timedelta(hours=48)
FAILURE_CLASS_HISTORY_MAX = 5


def _coerce_history_datetime(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=timezone.utc)
    if value.tzinfo is not None and reference.tzinfo is None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _parse_history_seen_at(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_failure_class_history(
    raw_history: object,
    *,
    now: datetime | None = None,
    ttl: timedelta = FAILURE_CLASS_HISTORY_TTL,
) -> list[dict[str, str]]:
    """Return timestamped, unexpired failure-class history entries.

    Legacy entries were bare strings and never expired. Keep them readable, but
    convert them to timestamped entries on the next write so future retry-budget
    decisions are bounded by ``ttl`` instead of all historical attempts.
    """
    if not isinstance(raw_history, list):
        return []
    now = now or _utcnow()
    cutoff = now - ttl
    normalized: list[dict[str, str]] = []
    for entry in raw_history:
        if isinstance(entry, dict):
            cls = str(entry.get("class") or entry.get("failure_class") or "").strip()
            seen_at = _parse_history_seen_at(entry.get("at") or entry.get("seen_at"))
        elif isinstance(entry, str):
            cls = entry.strip()
            seen_at = now
        else:
            continue
        if not cls:
            continue
        seen_at = seen_at or now
        comparable_seen_at = _coerce_history_datetime(seen_at, now)
        if comparable_seen_at < cutoff:
            continue
        normalized.append({"class": cls, "at": comparable_seen_at.isoformat()})
    return normalized[-FAILURE_CLASS_HISTORY_MAX:]


def append_failure_class_history(
    raw_history: object,
    failure_class: str,
    *,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    now = now or _utcnow()
    history = normalize_failure_class_history(raw_history, now=now)
    if failure_class:
        history.append({"class": str(failure_class), "at": now.isoformat()})
    return history[-FAILURE_CLASS_HISTORY_MAX:]


def failure_class_history_classes(
    raw_history: object, *, now: datetime | None = None
) -> list[str]:
    return [
        entry["class"]
        for entry in normalize_failure_class_history(raw_history, now=now)
    ]


async def compute_effective_max_requeues(
    parent: Task, session: AsyncSession, max_requeues: int
) -> int:
    """Return the max requeue count, reduced when failed children show recurring
    failure patterns:

    - Same non-infra class repeats (current == previous): reduce by 1.  "Infra"
      uses the analytics superset INFRA_ANALYTICS_CLASSES, so a repeated
      UNPLACEABLE_EXECUTION_SITE (a platform routing contract, not a code issue)
      never reduces the parent's budget.
    - A non-infra class appears 2+ times in ``_failure_class_history``: reduce
      by 1; 3+ times: reduce by 2 (root cause clearly not being addressed).
    - SCOPE_OVERFLOW appears 2+ times in history: reduce by 1, despite being
      in INFRA_CLASSES, since repeat scope mismatches signal that scope
      escalation is not working.
    - AGENT_TIMEOUT appears 2+ times in history: reduce by 1, despite being
      in INFRA_CLASSES, since repeat timeouts signal scope/complexity exceeds
      the runtime budget rather than transient infrastructure.
    - The same specific tests failed across attempts (child's
      ``_previous_failing_tests`` overlaps with failing tests extracted from
      its own ``latest_status_message``): reduce by 1. Retry guidance cannot
      fix deterministic test failures, so fail the parent earlier instead of
      exhausting the full CODE_ERROR budget.
    """
    from app.task_lifecycle import _extract_failing_tests

    children = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id,
                    Task.status == TaskStatus.FAILED,
                )
            )
        )
        .scalars()
        .all()
    )
    reduction = 0
    recurring_tests = False
    for child in children:
        ctx = child.context or {}
        current_class = classify_failure(
            child,
            logs=ctx.get("last_error") or None,
            at_time=child.vm_stopped_at or child.last_heartbeat,
        )
        prev_class = ctx.get("_previous_failure_class")
        if (
            prev_class
            and current_class == prev_class
            and current_class not in INFRA_ANALYTICS_CLASSES
        ):
            reduction = max(reduction, 1)
        history = failure_class_history_classes(
            ctx.get("_failure_class_history"), now=_utcnow()
        )
        if history:
            counts: dict[str, int] = {}
            for cls in history:
                counts[cls] = counts.get(cls, 0) + 1
            for cls, count in counts.items():
                if cls == SCOPE_OVERFLOW and count >= 2:
                    reduction = max(reduction, 1)
                elif cls == AGENT_TIMEOUT and count >= 2:
                    reduction = max(reduction, 1)
                elif cls not in INFRA_ANALYTICS_CLASSES and count >= 3:
                    reduction = max(reduction, 2)
                elif cls not in INFRA_ANALYTICS_CLASSES and count >= 2:
                    reduction = max(reduction, 1)
        prev_failing = ctx.get("_previous_failing_tests") or []
        if isinstance(prev_failing, list) and prev_failing:
            current_failing = _extract_failing_tests(child.latest_status_message or "")
            if any(t in set(prev_failing) for t in current_failing):
                recurring_tests = True
    if recurring_tests:
        reduction += 1

    # Divergence gate: if any failed child's fix loop concluded it isn't
    # converging, pin the effective budget to 0 — no further parent retries.
    # The abandonment marker is written here so the admin digest can count it.
    for child in children:
        diverged = has_terminal_divergence(child.fix_loop_history)
        if diverged:
            loop_name, exit_reason = diverged
            child_ctx = dict(child.context or {})
            if not child_ctx.get("fix_loop_abandoned"):
                from sqlalchemy.orm.attributes import flag_modified as _flag_modified

                child_ctx["fix_loop_abandoned"] = {
                    "loop": loop_name,
                    "exit_reason": exit_reason,
                    "at": _utcnow().isoformat(),
                }
                child_ctx["failure_detail"] = "fix_loop_diverged"
                child.context = child_ctx
                _flag_modified(child, "context")
                session.add(child)
                await session.flush()
            return 0

    return max_requeues - reduction


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
