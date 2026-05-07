"""Review pipeline: spawn and manage automated review/fix task cycles.

Extracted from task_lifecycle.py to reduce its size. Contains the per-PR
review-fix loop, iteration cap, and stuck-cycle detection logic.

All imports are module-level — no circular dependency risk with app.main
or app.task_lifecycle.
"""

import logging
import re
import uuid
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.github import post_pipeline_merge_summary_comment as post_merge_summary_comment  # noqa: F401
from app.models import Task, TaskHistory, TaskStatus
from app.payload_schemas import validate_internal_payload
from app.shared_state import runtime_config as _runtime_config
from app.broadcast import broadcast_to_project
from app.time_utils import _utcnow
from app.spawn_calibration import (
    _compute_scope_budget_utilization,
    _compute_scope_overflow_warning,
)  # noqa: F401

logger = logging.getLogger(__name__)


MAX_REVIEW_FIX_ITERATIONS = 16

# Phase 2c (warm-VM continuity): how long after a source task's last heartbeat
# (or its completion) the warm-VM pin is honored on the spawned task. After
# this window the orchestrator's provisioning loop clears the pin and
# cold-provisions normally. Tuned to cover the typical role-handoff gap
# (agent finishing prior role + a few VM-heartbeat ticks during the next-role
# poll) without keeping a stale pin alive long enough to mask a dead VM.
WARM_HANDOFF_WINDOW_SECS = 240

STUCK_CYCLE_THRESHOLD_DEFAULT = 0.55
STUCK_CYCLE_THRESHOLD_REFACTOR = 0.65
# Medium-scope tasks share the default threshold: cross-module PRs legitimately
# need a few iterations to resolve cross-file consistency issues, and bailing
# out earlier prevented them from converging.
STUCK_CYCLE_THRESHOLD_MEDIUM = 0.55

# Token-level Jaccard thresholds used by the feedback-text fallback when one or
# both review_history entries lack a structured `issues` list (e.g. verdict
# parsing fell back to log/keyword scraping). Set higher than the structured
# thresholds because text comparison is noisier.
STUCK_CYCLE_TEXT_THRESHOLD_DEFAULT = 0.7
STUCK_CYCLE_TEXT_THRESHOLD_REFACTOR = 0.75
STUCK_CYCLE_TEXT_THRESHOLD_MEDIUM = 0.7

# Secondary Jaccard thresholds for the rephrased-issue fallback: both sides
# have structured CRITICAL/MAJOR issues but the exact-match proportion falls
# below the primary structured threshold. Stays above 0.6 so that two issues
# sharing boilerplate tokens but targeting different symbols (e.g. "Missing
# error handling in processUser" vs "…processOrder" → Jaccard 0.6) do NOT
# trigger a false positive.
STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_DEFAULT = 0.68
STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_REFACTOR = 0.7
STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_MEDIUM = 0.68


def _stuck_thresholds(scope: str, is_refactor: bool) -> tuple[float, float, float]:
    """Return (exact_match, issue_similarity, text_similarity) thresholds.

    Refactor takes priority over scope because refactor feedback structurally
    repeats. Medium scope currently matches the default; the constants are kept
    distinct so future tuning can diverge again without churning call sites.
    """
    if is_refactor:
        return (
            STUCK_CYCLE_THRESHOLD_REFACTOR,
            STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_REFACTOR,
            STUCK_CYCLE_TEXT_THRESHOLD_REFACTOR,
        )
    if scope == "medium":
        return (
            STUCK_CYCLE_THRESHOLD_MEDIUM,
            STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_MEDIUM,
            STUCK_CYCLE_TEXT_THRESHOLD_MEDIUM,
        )
    return (
        STUCK_CYCLE_THRESHOLD_DEFAULT,
        STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_DEFAULT,
        STUCK_CYCLE_TEXT_THRESHOLD_DEFAULT,
    )


_FEEDBACK_TOKEN_RE = re.compile(r"[a-z0-9_]+")

# Issue-churn: fix agent keeps rotating CRITICAL+MAJOR issues without the total
# count decreasing — evades Jaccard stuck-cycle detection but still unproductive.
ISSUE_CHURN_WINDOW = 2


def _critical_major_count(entry: dict) -> int:
    return sum(
        1
        for i in (entry.get("issues") or [])
        if i.get("severity", "").upper() in ("CRITICAL", "MAJOR")
        and (i.get("description") or "").strip()
    )


def _detect_issue_churn(history: list) -> tuple[bool, list[int]]:
    if len(history) < ISSUE_CHURN_WINDOW:
        return (False, [])
    counts = [_critical_major_count(e) for e in history[-ISSUE_CHURN_WINDOW:]]
    if any(c == 0 for c in counts):
        return (False, counts)
    non_decreasing = all(counts[i + 1] >= counts[i] for i in range(len(counts) - 1))
    # Oscillating patterns (e.g. [5, 3, 5], [4, 2, 4]) also represent zero net
    # progress: the fix agent removes some issues but reintroduces others.
    no_net_progress = counts[-1] >= counts[0]
    return (non_decreasing or no_net_progress, counts)


def _repeating_issue_descriptions(
    history: list, window: int = ISSUE_CHURN_WINDOW, min_occurrences: int = 2
) -> list[str]:
    """Descriptions appearing in >= min_occurrences of the last `window` entries."""
    if len(history) < min_occurrences:
        return []
    recent = history[-window:]
    desc_by_norm: dict[str, str] = {}
    per_entry: list[set[str]] = []
    for entry in recent:
        seen: set[str] = set()
        for issue in entry.get("issues") or []:
            if issue.get("severity", "").upper() not in ("CRITICAL", "MAJOR"):
                continue
            desc = (issue.get("description") or "").strip()
            if not desc:
                continue
            norm = " ".join(_FEEDBACK_TOKEN_RE.findall(desc.lower()))
            if not norm:
                continue
            seen.add(norm)
            desc_by_norm.setdefault(norm, desc)
        per_entry.append(seen)
    counts: dict[str, int] = {}
    for s in per_entry:
        for n in s:
            counts[n] = counts.get(n, 0) + 1
    repeating = [desc_by_norm[n] for n, c in counts.items() if c >= min_occurrences]
    return repeating[:5]


def _format_repeating_issues_section(history: list) -> str:
    repeating = _repeating_issue_descriptions(history)
    if not repeating:
        return ""
    bullets = "\n".join(f"- {d}" for d in repeating)
    return (
        "Repeating issues from prior iterations (prioritize breaking the cycle "
        "on these exact issues):\n" + bullets + "\n\n"
    )


def compute_drift_signal(
    review_history: list,
    planned_files: list,
    current_files: list,
) -> dict:
    """Compute drift_in / drift_out for the trajectory section in fix prompts.

    drift_in = files in the current diff that have NOT appeared in the planned
    list and have NOT been part of any APPROVE iteration's diff. These are the
    candidates for "leftover from a rebase or earlier fix that nobody reverted"
    — the failure mode behind PR #3435 / PR #3408.

    drift_out = files that WERE planned or part of an approved iteration's diff
    but are missing from the current diff. These are the candidates for "lost
    during a rebase fix" (e.g. PR #3435's iter-1-approved test changes that
    iter-2's rebase clobbered).

    Each drift_in entry is annotated with first_appeared_iteration (when the
    file first showed up in any review_history entry's changed_files) and
    flag_count (how many REQUEST_CHANGES iterations cited the file in issues).
    """
    approved_files: set[str] = set()
    file_first_seen: dict[str, int] = {}
    file_flag_count: dict[str, int] = {}
    for entry in review_history or []:
        if not isinstance(entry, dict):
            continue
        cf = entry.get("changed_files") or []
        verdict = entry.get("verdict")
        iteration_num = entry.get("iteration")
        for f in cf:
            if not isinstance(f, str) or not f:
                continue
            if f not in file_first_seen and isinstance(iteration_num, int):
                file_first_seen[f] = iteration_num
            if verdict == "APPROVE":
                approved_files.add(f)
        # Count REQUEST_CHANGES iterations that flagged a specific file via
        # an issue's `file` field — distinguishes "merely changed" from
        # "actively rejected by a reviewer."
        if verdict == "REQUEST_CHANGES":
            for issue in entry.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                fname = issue.get("file")
                if isinstance(fname, str) and fname:
                    file_flag_count[fname] = file_flag_count.get(fname, 0) + 1

    planned_set = {f for f in (planned_files or []) if isinstance(f, str) and f}
    current_set = {f for f in (current_files or []) if isinstance(f, str) and f}

    drift_in_files = current_set - (planned_set | approved_files)
    drift_in = [
        {
            "file": f,
            "first_appeared_iteration": file_first_seen.get(f),
            "flag_count": file_flag_count.get(f, 0),
        }
        for f in sorted(drift_in_files)
    ]
    drift_out = sorted((planned_set | approved_files) - current_set)
    return {"drift_in": drift_in, "drift_out": drift_out}


_CHURN_NARROW_FOCUS_PREFIX = (
    "⚠️ ISSUE CHURN DETECTED: the prior {window} fix iterations resolved some "
    "issues but introduced new ones (CRITICAL+MAJOR counts: {counts}). "
    "Prefer addressing only the listed issues; avoid incidental refactoring "
    "or cleanup. If the same issue keeps recurring, the fix you've been "
    "applying is probably wrong — don't apply it harder, investigate. Two "
    "common patterns: (1) the planned file set was wrong and the fix "
    "genuinely needs files outside scope (a sibling cluster's dep) — expand "
    "scope, explain why in the PR description, stop rewriting the same file "
    "differently each iteration; (2) review asks for a literal revert — run "
    "`git checkout origin/<base_branch> -- <file>` rather than rewriting "
    "from memory, which drifts each iteration and never converges.\n\n"
)

# Softer proactive directive injected when a fix iteration has consumed at
# least half the per-chain iteration cap but churn has not (yet) been
# detected. Unlike the churn prefix, this does NOT reduce the cap — it is
# guidance only, aimed at preventing churn before it begins.
MID_BUDGET_NARROW_FOCUS_THRESHOLD = 0.5
_MID_BUDGET_NARROW_FOCUS_PREFIX = (
    "⚠️ You have used over half of the fix iteration budget "
    "({iteration}/{cap}). Focus on the specific issues listed in the review "
    "feedback. Avoid modifying files not referenced by the issues. Defer "
    "cleanups and refactors to follow-up tasks.\n\n"
)

# Light-touch informational prefix injected from iteration 2 onward whenever
# repeating CRITICAL/MAJOR issues are observed across prior review iterations.
# No cap reduction, no narrow-focus urgency — just surfaces the pattern so fix
# agents have early visibility before churn or mid-budget thresholds trigger.
_EARLY_REPEATING_ISSUES_PREFIX = (
    "Note: some issues have appeared in multiple prior review iterations. "
    "Consider prioritizing a durable fix for these recurring patterns.\n\n"
)

# Utilization at/above this fraction of the scope file budget signals the fix
# agent to prefer minimal fixes over expansive refactoring — gives headroom
# before a fix iteration would tip the PR into scope-overflow territory.
SCOPE_PRESSURE_WARN_THRESHOLD = 0.7

# Graduated iteration cap: once a medium-scope PR crosses
# SCOPE_PRESSURE_WARN_THRESHOLD (70%) of its production-file budget, the
# remaining review-fix iterations are scaled down by how close the PR is to
# full budget. At 70% utilization the fix agent keeps ~30% of its remaining
# budget; at 90% only ~10% (floor of 2); at 95%+ the floor of 2 dominates.
# The no-new-files prompt stays gated on the stricter
# SCOPE_PRESSURE_HARD_CAP_PERCENTAGE (90%) level.
SCOPE_PRESSURE_HARD_CAP_PERCENTAGE = 90
SCOPE_PRESSURE_HARD_CAP_REMAINING = 2

_SCOPE_PRESSURE_HARD_CAP_PREFIX = (
    "🚫 SCOPE BUDGET EXHAUSTED: this PR has consumed {count}/{budget} "
    "({pct}%) of its medium-scope production file budget. You MUST NOT add "
    "any new production files — only modify the existing changed files. "
    "Defer any new-file work to follow-up tasks.\n\n"
)


def _is_test_file(path: str) -> bool:
    p = path.lower()
    if (
        "/testing/" in p
        or p.startswith("testing/")
        or "/tests/" in p
        or p.startswith("tests/")
    ):
        return True
    name = p.rsplit("/", 1)[-1]
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return ".test." in name or ".spec." in name


# Minimum direction acceptance rate required to auto-approve a small diff.
# Directions with poor merge history still get a full review regardless of
# diff size — the acceptance gate is independent of the line/file thresholds.
AUTO_APPROVE_MIN_ACCEPTANCE_RATE = 0.5


def _diff_lines_changed(diff_summary: list | None) -> int | None:
    """Sum added+removed across a diff summary list.

    Returns None when the summary is missing or malformed, so callers can
    fall through to the full review path when diff size is unknown.
    """
    if not diff_summary or not isinstance(diff_summary, list):
        return None
    total = 0
    for entry in diff_summary:
        if not isinstance(entry, dict):
            return None
        total += int(entry.get("added") or 0) + int(entry.get("removed") or 0)
    return total


def should_auto_approve_diff(
    source_task: Task, direction_data: dict | None
) -> tuple[bool, str]:
    """Decide whether to skip the review task for a trivially small/medium diff.

    Returns (skip, reason). Reason is always populated — a short human-
    readable string suitable for task status messages and operator logs.

    Gates (all must pass):
    - source task type is create, refactor, or fix
    - scope is 'small' or 'medium' (large always gets full review). Medium
      uses tighter line/file thresholds than small and can be disabled
      independently via auto_approve_medium_max_lines=0.
    - diff size known and <= per-scope max_lines
    - non-test file count <= per-scope max_files
    - direction acceptance_rate > AUTO_APPROVE_MIN_ACCEPTANCE_RATE when
      direction history is available (new directions pass by default)
    """
    source_type = (source_task.first_payload or {}).get("type", "")
    if source_type not in ("create", "refactor", "fix", "migrate"):
        return (False, f"source type {source_type!r} not eligible")

    ctx = source_task.context or {}
    scope = ctx.get("scope", "medium")
    if scope == "small":
        max_lines = _runtime_config.get("auto_approve_max_lines", 50)
        max_files = _runtime_config.get("auto_approve_max_files", 4)
        if not max_lines or max_lines <= 0:
            return (False, "auto-approve disabled (auto_approve_max_lines=0)")
    elif scope == "medium":
        max_lines = _runtime_config.get("auto_approve_medium_max_lines", 30)
        max_files = _runtime_config.get("auto_approve_medium_max_files", 2)
        if not max_lines or max_lines <= 0:
            return (
                False,
                "medium auto-approve disabled (auto_approve_medium_max_lines=0)",
            )
    else:
        return (False, f"scope {scope!r} not eligible (small/medium only)")

    changed_files = ctx.get("_pr_changed_files") or []
    if not changed_files:
        return (False, "changed files unknown")
    overflow_warning = _compute_scope_overflow_warning(changed_files, scope)
    if overflow_warning:
        return (
            False,
            "scope overflow warning present: "
            f"{overflow_warning['non_test_file_count']} non-test files > "
            f"{overflow_warning['threshold']} threshold",
        )
    non_test_count = sum(1 for f in changed_files if not _is_test_file(f))
    if non_test_count > max_files:
        return (False, f"{non_test_count} non-test files > {max_files}")

    lines_changed = _diff_lines_changed(ctx.get("_pr_diff_summary"))
    if lines_changed is None:
        return (False, "diff size unknown")
    if lines_changed > max_lines:
        return (False, f"{lines_changed} lines > {max_lines}")

    if direction_data is not None:
        from app.research_completion import _compute_acceptance_rate  # noqa: PLC0415

        acceptance_rate = _compute_acceptance_rate(direction_data)
        if (
            acceptance_rate is not None
            and acceptance_rate <= AUTO_APPROVE_MIN_ACCEPTANCE_RATE
        ):
            return (
                False,
                f"direction acceptance_rate {acceptance_rate:.0%} "
                f"≤ {AUTO_APPROVE_MIN_ACCEPTANCE_RATE:.0%}",
            )

    return (
        True,
        f"auto-approve: {lines_changed} lines across {non_test_count} non-test "
        f"file(s) (≤ {max_lines}/{max_files})",
    )


should_auto_approve_small_diff = should_auto_approve_diff


def _feedback_token_set(text: str) -> set[str]:
    return {t for t in _FEEDBACK_TOKEN_RE.findall(text.lower()) if len(t) >= 3}


def _feedback_jaccard(a: str, b: str) -> float:
    tokens_a = _feedback_token_set(a)
    tokens_b = _feedback_token_set(b)
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


# A pipeline task is considered 'stale' (effectively dead, safe to supersede)
# when it has not heartbeated for this long. Matches the orchestrator's own
# reap threshold (see orchestrator.py stale-PROVISIONING recovery) so we don't
# fight with it — whichever sweep reaches the task first cancels it.
PIPELINE_TASK_STALE_MINUTES = 15


def _is_pipeline_task_stale(task: Task) -> bool:
    """True if a review/fix task has gone silent and can be superseded.

    Only applies to tasks that have actually started running. A task still
    waiting in QUEUED (never claimed) is NOT considered stale — long queue
    waits typically mean rate-limit backoff, not a dead task, and the
    orchestrator's reap loop owns that state. Once a task has either
    heartbeated or reached ``started_at``, we measure silence from the
    freshest of those timestamps.
    """
    threshold = _utcnow() - timedelta(minutes=PIPELINE_TASK_STALE_MINUTES)
    if task.last_heartbeat is not None:
        return task.last_heartbeat < threshold
    if task.started_at is None:
        return False
    return task.started_at < threshold


def _max_review_iterations(task: Task, direction_data: dict | None = None) -> int:
    """Scope-aware review-fix iteration limit, optionally reduced for low-quality directions.

    If direction_data is provided and the direction has a poor operator acceptance rate
    (acceptance_rate < 0.3) or a high rejection ratio (prs_rejected / prs_merged > 2.0
    in recent history), the scope-based cap is reduced by ~30% to avoid wasting compute
    on directions the operator consistently rejects.
    """
    ctx = task.context or {}
    scope = ctx.get("scope", "medium")
    fix_reason = ctx.get("review_loop", {}).get("fix_reason", "code_review")

    # CI-only fixes should converge fast (lint/type/test errors)
    if fix_reason == "ci_failure":
        limit = 8
    # Small scope tasks: less code, fewer things to fix
    elif scope == "small":
        limit = 10
    # Medium scope: moderate iteration budget
    elif scope == "medium":
        limit = 12
    # Large scope or unknown: full budget
    else:
        limit = 16

    # Direction-aware reduction: cut cap by ~30% for consistently-rejected directions.
    # Only applies when direction_data is available and we're not already at the ci_failure cap.
    if direction_data is not None and fix_reason != "ci_failure":
        from app.research_completion import _compute_acceptance_rate

        acceptance_rate = _compute_acceptance_rate(direction_data)
        # Check recent rejection ratio from last few history entries
        recent_history = (direction_data.get("research_history") or [])[-3:]
        recent_merged = sum(e.get("prs_merged", 0) for e in recent_history)
        recent_rejected = sum(e.get("prs_rejected", 0) for e in recent_history)
        high_rejection_ratio = (
            recent_merged > 0 and (recent_rejected / recent_merged) > 2.0
        ) or (recent_merged == 0 and recent_rejected >= 3)
        if (
            acceptance_rate is not None and acceptance_rate < 0.3
        ) or high_rejection_ratio:
            limit = max(1, round(limit * 0.7))

    # Scope-budget hard cap: once a medium-scope PR crosses 90% of its
    # file budget, a prior spawn persists an absolute iteration ceiling via
    # _scope_budget_hard_cap. Honor it regardless of scope/direction-aware
    # logic so the reduction survives across subsequent review/fix spawns.
    hard_cap = ctx.get("_scope_budget_hard_cap")
    if isinstance(hard_cap, int) and hard_cap > 0:
        limit = min(limit, hard_cap)

    # Retry carry-forward: if a prior attempt of this task (before parent requeue
    # cloned it) already burned review iterations, subtract half of that usage
    # from the fresh cap so retries don't each get a full budget. Floor at 3 so
    # a heavily-iterated prior attempt doesn't leave the retry dead on arrival.
    prior_consumed = ctx.get("_prior_review_iterations_consumed")
    if isinstance(prior_consumed, int) and prior_consumed > 0:
        limit = max(limit - prior_consumed // 2, 3)

    return min(limit, MAX_REVIEW_FIX_ITERATIONS)


async def _handle_cap_exhaustion(
    source_task: Task,
    resolved_original_id: str,
    pr_number: int,
    iterations: int,
    session: AsyncSession,
    *,
    scope: str,
) -> None:
    """Terminal handler when the review-fix iteration cap is hit.

    Transitions the create task (``orig``) to FAILED with
    failure_detail=review_cap_exhausted, closes the GitHub PR, and fires an
    approval alert. Idempotent — safe to re-enter on the same orig without
    duplicating side effects.

    Why FAILED instead of COMPLETED+requires_approval:
    The prior behaviour left the create task COMPLETED and relied on
    requires_approval=True as a terminal signal, but rollup_management's
    stale-unmerged respawn loop doesn't check that flag — it kept re-entering
    the cap path every 10 min indefinitely. Marking FAILED routes the task
    through the standard failure → partial-rollup path.

    ``scope`` is either "global" (cross-chain) or "per-chain" — used only for
    human-readable messages.
    """
    from app.failure_classification import CODE_ERROR, persist_failure_category  # noqa: PLC0415
    from app.main import _maybe_create_failure_alert  # noqa: PLC0415
    from app.github import close_pull_request, create_pr_comment  # noqa: PLC0415
    from app.routers.agent import get_github_token_for_task  # noqa: PLC0415
    from app.routers.agent_status import record_task_result  # noqa: PLC0415
    from app.shared_state import record_direction_result  # noqa: PLC0415
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    cap_label = "global" if scope == "global" else "per-chain"
    source_task.latest_status_message = f"Review-fix cap reached for PR #{pr_number} ({iterations} iterations, {cap_label})"
    session.add(source_task)

    try:
        orig = await session.get(Task, uuid.UUID(resolved_original_id))
    except Exception as e:
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: failed to load orig {resolved_original_id[:8]}: {e}"
        )
        return
    if orig is None:
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: orig {resolved_original_id[:8]} not found"
        )
        return

    already_handled = (
        orig.status == TaskStatus.FAILED
        and (orig.context or {}).get("failure_detail") == "review_cap_exhausted"
    )
    if already_handled:
        logger.info(
            f"[PIPELINE] Cap re-triggered for PR #{pr_number} — orig {resolved_original_id[:8]} "
            f"already FAILED with review_cap_exhausted, skipping side effects"
        )
        await session.commit()
        return

    orig.status = TaskStatus.FAILED
    orig.requires_approval = True
    orig.latest_status_message = (
        f"Review-fix cycle reached limit ({iterations} iterations, {cap_label}) — "
        f"PR #{pr_number} auto-closed, manual review required"
    )
    persist_failure_category(orig, CODE_ERROR)
    ctx = dict(orig.context or {})
    ctx["failure_detail"] = "review_cap_exhausted"
    ctx["review_cap_iterations"] = iterations
    ctx["pr_rejected"] = True
    ctx["pr_rejection_note"] = (
        f"Auto-closed by Blueprint: review-fix cap exhausted after {iterations} iterations"
    )
    orig.context = ctx
    flag_modified(orig, "context")
    session.add(orig)
    try:
        await session.commit()
    except Exception as e:
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: failed to commit orig {resolved_original_id[:8]}: {e}",
            exc_info=True,
        )
        return

    # Surface cap-exhaustion to deploy_monitoring + direction health tracking.
    # Without this, cap failures bypass per-type failure gates and anomaly detection.
    record_task_result(
        success=False,
        failure_class=CODE_ERROR,
        task_id=str(orig.id),
        task_type=(orig.first_payload or {}).get("type", ""),
    )
    _direction_id = str((orig.context or {}).get("direction_id") or "")
    if _direction_id:
        record_direction_result(
            _direction_id,
            success=False,
            failure_class=CODE_ERROR,
            project_id=str(orig.project_id) if orig.project_id else "",
        )

    # Cap exhaustion bypasses the normal status-update handler; call
    # _record_remediation_if_terminal directly to keep direction state consistent.
    try:
        from app.task_completion import _record_remediation_if_terminal  # noqa: PLC0415

        await _record_remediation_if_terminal(orig, TaskStatus.FAILED, session)
        await session.commit()
    except Exception as e:
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: failed to record remediation outcome for PR #{pr_number}: {e}"
        )
    investigation_id: str | None = None
    try:
        from app.autonomous_research import _spawn_review_convergence_investigation  # noqa: PLC0415

        _inv = await _spawn_review_convergence_investigation(
            orig,
            session,
            pr_number=pr_number,
            iterations=iterations,
            detail=cap_label,
            trigger="review_cap_exhausted",
        )
        if _inv is not None:
            investigation_id = str(_inv)
            await session.commit()
    except Exception as e:
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: convergence investigation spawn failed for PR #{pr_number}: {e}",
            exc_info=True,
        )

    try:
        gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
        try:
            fingerprint = ((orig.context or {}).get("failure_fingerprints") or [None])[
                0
            ]
            investigation_note = (
                f"A convergence investigation has been queued (task `{investigation_id[:8]}`) "
                f"to diagnose why this PR could not converge. Reopen after the investigation completes."
                if investigation_id
                else "No investigation was spawned (no direction attached or a recent one already covers this). Manual review required."
            )
            fingerprint_note = (
                f"\n\nFailure fingerprint: `{fingerprint}`" if fingerprint else ""
            )
            await create_pr_comment(
                gh_token,
                owner,
                repo,
                pr_number,
                f"Auto-closed: review-fix cap reached ({iterations} iterations, {cap_label}). "
                f"{investigation_note}{fingerprint_note}",
            )
        except Exception as e:
            logger.warning(
                f"[PIPELINE] Cap-exhaustion: failed to comment on PR #{pr_number}: {e}"
            )
        closed = await close_pull_request(
            gh_token, owner, repo, pr_number, project_id=str(orig.project_id)
        )
        if closed:
            logger.info(
                f"[PIPELINE] Closed PR #{pr_number} after cap exhaustion ({iterations} iterations)"
            )
        else:
            logger.warning(
                f"[PIPELINE] close_pull_request returned False for PR #{pr_number}"
            )
    except Exception as e:
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: failed to close PR #{pr_number}: {e}",
            exc_info=True,
        )

    try:
        await _maybe_create_failure_alert(
            orig,
            session,
            f"PR #{pr_number} hit review-fix cap ({iterations} iterations, {cap_label}) — "
            f"auto-closed, manual review required",
        )
    except Exception as e:
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: failed to create alert for PR #{pr_number}: {e}"
        )


SPAWN_REASON_KEY = "_last_pipeline_spawn_reason"
# Reason codes written to ``source_task.context[SPAWN_REASON_KEY]`` by
# ``_spawn_pipeline_task``. Rollup callers read this to distinguish
# transient deferrals (retry next sweep) from terminal failures.
SPAWN_REASON_SPAWNED = "spawned"
SPAWN_REASON_DEDUP = "dedup_same_type"
SPAWN_REASON_ALREADY_MERGED = "already_merged"
SPAWN_REASON_DEFERRED = "deferred_cross_type"
SPAWN_REASON_CAP = "cap_exhausted"
SPAWN_REASON_STUCK = "stuck_cycle"
# Two consecutive same-type pipeline tasks failed with an identical failure
# fingerprint — the agent's code edits aren't moving the failure shape, which
# is the signature of a precondition gap (missing system lib, baseline lint
# debt, toolchain layer) rather than a code defect. Halt the chain and let
# the strategic agent (or operator) decide whether to spawn an infra fix.
SPAWN_REASON_BLOCKED_REPEAT = "blocked_repeat_fingerprint"

_PIPELINE_ACTIVE_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
)

SCOPE_OVERFLOW_PR_SIGNAL_EMITTED_KEY = "scope_overflow_pr_signal_emitted"


def _pipeline_task_idempotency_key(
    *,
    project_id,
    task_type: str,
    pr_number: int,
    original_task_id: str,
    iteration: int,
    is_rollup: bool,
) -> str:
    chain_id = f"rollup-pr-{pr_number}" if is_rollup else original_task_id
    return (
        f"review_pipeline:{project_id}:{chain_id}:{pr_number}:{task_type}:{iteration}"
    )


async def _get_active_pipeline_task_by_key(
    session: AsyncSession,
    idempotency_key: str,
) -> Task | None:
    return (
        (
            await session.execute(
                select(Task).where(
                    Task.idempotency_key == idempotency_key,
                    Task.status.in_(_PIPELINE_ACTIVE_STATUSES),
                )
            )
        )
        .scalars()
        .first()
    )


def _set_spawn_reason(task: Task, reason: str) -> None:
    ctx = dict(task.context or {})
    ctx[SPAWN_REASON_KEY] = reason
    task.context = ctx


_FINGERPRINT_NOISE_RE = (
    # Canonical UUID with dashes (8-4-4-4-12) — matched before the generic
    # hex regex so the whole UUID collapses into one token instead of leaving
    # the 4-char middle groups behind.
    re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    ),
    # SHAs / hex tokens of 8+ hex chars (no dashes)
    re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE),
    # Timestamps "2026-05-06T09:31" / "2026-05-06 09:31"
    re.compile(
        r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(z|[+-]\d{2}:?\d{2})?",
        re.IGNORECASE,
    ),
    # /tmp/<noise> paths
    re.compile(r"/tmp/\S+"),
    # iter / iteration markers ("iter=3", "iteration 3", "iter:3")
    re.compile(r"\biter(ation)?[\s=:]+\d+", re.IGNORECASE),
    # attempt N / attempt: N
    re.compile(r"\battempt[\s=:]+\d+", re.IGNORECASE),
    # task-XXXXXX / agent-vm-XXXXX style identifiers
    re.compile(r"\b(?:task|agent-vm|vm)-[a-z0-9-]+", re.IGNORECASE),
    # Standalone integers ≥ 4 digits (line numbers, counts, etc. that vary by run)
    re.compile(r"\b\d{4,}\b"),
)


def _failure_fingerprint_msg(msg: str | None) -> str:
    """Stable 12-char hash of a failure status message for cross-iteration comparison.

    Mirrors the agent-side ``errorFingerprint`` (in ``agent/src/index.ts``) but
    additionally strips iteration-specific noise — UUIDs, timestamps, /tmp paths,
    iter/attempt counters, agent-vm identifiers — so that two iterations hitting
    the same underlying blocker produce identical fingerprints despite cosmetic
    differences in their status messages.

    Returns ``""`` for empty input. Returned hashes are 12 hex chars (SHA-256
    truncated) to match the ``errorFingerprint`` format used elsewhere.
    """
    from hashlib import sha256

    if not msg:
        return ""
    s = msg.strip().lower()
    if not s:
        return ""
    for pat in _FINGERPRINT_NOISE_RE:
        s = pat.sub("X", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    return sha256(s.encode("utf-8")).hexdigest()[:12]


async def _post_repeat_fingerprint_blocker_message(
    session: AsyncSession,
    *,
    source_task: Task,
    pr_number: int,
    fingerprint: str,
    prior_task_id,
) -> None:
    """Surface a repeat-fingerprint halt to the strategic agent via ProjectMessage.

    The strategic agent reads ``open`` ProjectMessages on its cycle (see
    ``directions.py`` open_messages loading), so this becomes the trigger for
    "decide whether to spawn a precondition-fix direction or defer." Posted
    with ``from_type="system"`` and ``message_type=BLOCKER`` so it sorts
    correctly in the strategic agent's context block.
    """
    from app.models import MessageType, ProjectMessage  # noqa: PLC0415

    body = (
        f"Pipeline halt — repeat-fingerprint detected on PR #{pr_number}.\n\n"
        f"The fix-agent's code edits aren't moving the failure shape: this "
        f"task ({str(source_task.id)[:8]}) failed with the same fingerprint "
        f"({fingerprint}) as the prior attempt ({str(prior_task_id)[:8]}). "
        f"Identical failures across iterations is the signature of a "
        f"*precondition gap* (missing system library, baseline lint/test "
        f"debt, toolchain mismatch) rather than a code defect — another "
        f"review-fix iteration won't help.\n\n"
        f"Latest failure text:\n"
        f"  {(source_task.latest_status_message or '')[:600]}\n\n"
        f"Decision points (yours):\n"
        f"  - Spawn a direction to fix the precondition (update the packer "
        f"image, scope the gate to changed files, add a missing dep, etc.) "
        f"if the unblock is worth the investment for this PR + future ones.\n"
        f"  - Defer and route around it — leave this PR halted, work on "
        f"unrelated directions.\n"
        f"  - Cancel the chain (operator) if the work is no longer worth "
        f"pursuing.\n\n"
        f"This task is now classified as BLOCKED_PRECONDITION (INFRA class — "
        f"does not count toward the per-PR review-fix iteration cap, will "
        f"not be auto-resurrected by the reaper)."
    )
    pm = ProjectMessage(
        project_id=source_task.project_id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        from_task_id=source_task.id,
        content=body,
        context={
            "pr_number": pr_number,
            "fingerprint": fingerprint,
            "source_task_id": str(source_task.id),
            "prior_task_id": str(prior_task_id),
            "trigger": "repeat_fingerprint_halt",
        },
    )
    session.add(pm)


# ---------------------------------------------------------------------------
# Cross-cutting safeguard gate
# ---------------------------------------------------------------------------

TOP_LEVEL_MODULES = frozenset({"backend", "agent", "admin-ui"})


def _is_cross_cutting_pr(source_task: Task) -> tuple[bool, str]:
    """Return (True, reason) when the PR qualifies for a cross-cutting safeguard gate.

    Three conditions, any one of which triggers the gate:
    (a) scope=large — large PRs always get a safeguard review.
    (b) scope=medium AND changed files span >= cross_cutting_gate_medium_module_threshold
        distinct top-level modules (backend / agent / admin-ui).
    (c) scope_overflow_warned=True AND diff lines exceed
        cross_cutting_gate_overflow_line_threshold.
    """
    if not _runtime_config.get("cross_cutting_gate_enabled", True):
        return (False, "gate disabled")
    ctx = source_task.context or {}
    scope = ctx.get("scope", "medium")
    if scope == "large":
        return (True, "scope=large")
    if scope == "medium":
        changed = ctx.get("_pr_changed_files") or []
        mods = len(
            {f.split("/")[0] for f in changed if f.split("/")[0] in TOP_LEVEL_MODULES}
        )
        threshold = int(
            _runtime_config.get("cross_cutting_gate_medium_module_threshold", 3)
        )
        if mods >= threshold:
            return (True, f"scope=medium,{mods}_modules")
    if (ctx.get("review_loop") or {}).get("scope_overflow_warned"):
        lines = _diff_lines_changed(ctx.get("_pr_diff_summary"))
        overflow_threshold = int(
            _runtime_config.get("cross_cutting_gate_overflow_line_threshold", 100)
        )
        if lines and lines > overflow_threshold:
            return (True, f"overflow+{lines}_lines")
    return (False, "not cross-cutting")


async def maybe_spawn_cross_cutting_safeguard(
    orig: Task, pr_number: int, branch: str, base_branch: str, session: AsyncSession
) -> bool:
    """Spawn a security_audit safeguard task if the PR is cross-cutting.

    Sets orig.requires_approval=True and stores the safeguard task ID in
    orig.context so the orchestrator and completion handlers can coordinate.
    Returns True if a safeguard was spawned, False otherwise.
    Idempotent: no-ops if orig.context already has a safeguard_task_id.
    """
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    ctx = orig.context or {}
    if ctx.get("safeguard_task_id"):
        return False
    is_cross, reason = _is_cross_cutting_pr(orig)
    if not is_cross:
        return False

    safeguard_id = uuid.uuid4()
    changed = ctx.get("_pr_changed_files") or []
    files_str = ", ".join(changed[:15]) or "all changed files"
    payload_data = validate_internal_payload(
        {
            "type": "security_audit",
            "prompt": (
                f"Cross-cutting safeguard review for PR #{pr_number} ({reason}). "
                "Assess: (1) security vulnerabilities introduced by cross-module changes, "
                "(2) interface breakage across module boundaries, "
                "(3) test coverage for cross-module interactions. "
                f"Changed files: {files_str}. Return findings as a JSON array."
            ),
            "base_branch": base_branch,
        },
        "review_pipeline:cross_cutting_safeguard",
    )
    safeguard = Task(
        id=safeguard_id,
        project_id=orig.project_id,
        title=f"Cross-cutting safeguard: PR #{pr_number}",
        description=f"Security/QA safeguard ({reason})",
        parent_task_id=orig.id,
        payloads=[payload_data],
        context={
            "safeguard_for_task_id": str(orig.id),
            "safeguard_pr_number": pr_number,
            "safeguard_base_branch": base_branch,
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(safeguard)
    orig.requires_approval = True
    orig.context = {
        **ctx,
        "safeguard_task_id": str(safeguard_id),
        "safeguard_reason": reason,
        "safeguard_pr_number": pr_number,
    }
    flag_modified(orig, "context")
    orig.latest_status_message = (
        f"Merge gated — awaiting cross-cutting safeguard for PR #{pr_number} ({reason})"
    )
    session.add(orig)
    session.add(
        TaskHistory(
            task_id=safeguard_id,
            action="SPAWNED",
            details={
                "spawned_by": "CROSS_CUTTING_GATE",
                "pr_number": pr_number,
                "reason": reason,
            },
        )
    )
    await session.commit()
    logger.info(
        f"[PIPELINE] Cross-cutting safeguard {safeguard_id} spawned "
        f"for PR #{pr_number} ({reason}) — merge gated on orig {orig.id}"
    )
    await broadcast_to_project(
        str(orig.project_id),
        "task_updated",
        {"task_id": str(safeguard_id), "status": TaskStatus.QUEUED},
    )
    return True


async def _spawn_pipeline_task(
    source_task: Task,
    task_type: str,
    session: AsyncSession,
    pr_number: int,
    branch: str,
    base_branch: str,
    original_prompt: str = "",
    fix_prompt: str = "",
    original_task_id: str = "",
    current_feedback: str = "",
    fix_reason: str = "",
    current_issues: list | None = None,
    ci_failure_logs: str = "",
    investigated_files: list[str] | None = None,
    suppress_warm_handoff: bool = False,
) -> tuple[Task | None, bool]:
    """Spawn a review or fix task as part of the automated review pipeline.

    Returns (task, was_created):
    - (None, False)          → spawn suppressed. Reasons:
                                 * PR already merged
                                 * iteration cap reached (per-chain or global)
                                 * stuck-cycle detected
                                 * a DIFFERENT-type task is in flight on the
                                   same chain and must finish first (e.g. a fix
                                   is running and the caller asked for a review)
                                Caller must decide follow-up (retry later, mark
                                for manual approval, etc.).
                                The specific reason is written to
                                ``source_task.context[SPAWN_REASON_KEY]`` so
                                callers can distinguish transient defers from
                                terminal caps without string-matching messages.
    - (existing_task, False) → same-type dedup hit: a review/fix of the SAME
                                task_type is already running on the same chain
                                and is still alive (has heartbeated recently).
                                Caller can trust it to finish the work.
    - (new_task, True)       → a fresh review/fix task was created. This path
                                also fires when we superseded a stale same-
                                type task, or cancelled an in-flight review
                                whose snapshot is now stale because the caller
                                requested a fix.
    """
    source_task_id = source_task.id
    # Serialize concurrent spawn attempts for the same PR. Without this, two
    # callers can both pass the SELECT in the dedup guard below before either
    # commits — each spawn has a distinct idempotency_key so the active-key
    # partial UNIQUE doesn't catch the race, and we end up with a double
    # review/fix on the same PR/chain. Lock is held until commit/rollback.
    from app.task_locking import (
        acquire_pr_spawn_lock,
    )  # local import to keep top of file lean

    await acquire_pr_spawn_lock(session, pr_number)

    # Resolve the chain identifier once, so the "already merged" guard, the
    # dedup guard, and the spawn path all agree on which chain this is.
    # A chain is identified by the *create* task that opened the PR; review/fix
    # tasks in the chain carry that id in payload.original_task_id. For a source
    # task that has no explicit chain id, fall back to its own id (the source IS
    # the create task in that case).
    resolved_original_id = (
        original_task_id
        or (source_task.context or {})
        .get("review_loop", {})
        .get("original_task_id", "")
        or (source_task.first_payload).get("original_task_id", "")
        or str(source_task.id)
    )

    # Guard: don't spawn if PR is already merged (check both original and source).
    try:
        orig_task = await session.get(Task, uuid.UUID(resolved_original_id))
        if orig_task and (orig_task.context or {}).get("pr_merged"):
            logger.info(
                f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — PR already merged (original task {resolved_original_id[:8]})"
            )
            source_task.latest_status_message = (
                f"PR #{pr_number} already merged — skipping {task_type}"
            )
            _set_spawn_reason(source_task, SPAWN_REASON_ALREADY_MERGED)
            session.add(source_task)
            return (None, False)
    except Exception:
        logger.debug(
            f"[PIPELINE] Could not verify pr_merged for original task {resolved_original_id[:8]}: skipping check"
        )
    if (source_task.context or {}).get("pr_merged"):
        logger.info(
            f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — PR already merged (source task)"
        )
        _set_spawn_reason(source_task, SPAWN_REASON_ALREADY_MERGED)
        session.add(source_task)
        return (None, False)

    # Dedup guard for active pipeline tasks on this PR/chain. The match axes are
    # (pr_number, chain, task_type, liveness):
    #
    # - Different chain (original_task_id) ⇒ unrelated; ignore.
    # - Same chain, same type, alive ⇒ real dedup hit; return the existing task.
    # - Same chain, any type, stale (no heartbeat for PIPELINE_TASK_STALE_MINUTES)
    #   ⇒ supersede: cancel the corpse and continue to a fresh spawn. This closes
    #   the "dying VM never posts status, caller blocked forever" path.
    # - Same chain, different type, alive:
    #     * fix requested while review in flight ⇒ cancel the review; its verdict
    #       would be against a pre-conflict / pre-CI-failure snapshot. A fresh
    #       review auto-spawns once the fix pushes.
    #     * any other cross-type combo ⇒ return (None, False) and let the caller
    #       retry on the next tick; no silent substitution.
    #
    # Rollup exception: rollup PRs aggregate subtasks whose parents each run
    # their own rollup logic. When two rollup parents target the same PR, they
    # each pass ``original_task_id=str(parent.id)`` — different chains, but the
    # SAME rollup PR. Gate dedup on PR-number only in that case so we don't
    # double-spawn review/fix for the shared PR.
    is_rollup_source = bool((source_task.context or {}).get("_is_rollup_review"))
    superseded_stale_task = False
    active_pipeline = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == source_task.project_id,
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.EVALUATING,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for t in active_pipeline:
        if t.id == source_task.id:
            continue
        t_payload = t.first_payload
        t_type = t_payload.get("type")
        t_orig = t_payload.get("original_task_id") or ""
        if t_payload.get("pr_number") != pr_number or t_type not in ("review", "fix"):
            continue
        if not is_rollup_source and t_orig != resolved_original_id:
            continue

        if _is_pipeline_task_stale(t):
            from app.failure_classification import (
                AGENT_TIMEOUT,
                persist_failure_category,
            )  # noqa: PLC0415

            t.status = TaskStatus.CANCELLED
            t.latest_status_message = (
                f"Superseded by new {task_type} for PR #{pr_number}: "
                f"no heartbeat for >{PIPELINE_TASK_STALE_MINUTES}min"
            )
            persist_failure_category(t, AGENT_TIMEOUT)
            session.add(t)
            superseded_stale_task = True
            logger.warning(
                f"[PIPELINE] Superseding stale {t_type} task {t.id} "
                f"(last_heartbeat={t.last_heartbeat}, started_at={t.started_at}) "
                f"for PR #{pr_number}"
            )
            continue

        if t_type == task_type:
            logger.warning(
                f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — "
                f"same-chain {t_type} already active: {t.id}"
            )
            source_task.latest_status_message = (
                f"{task_type.title()} skipped for PR #{pr_number} — "
                f"{t_type} active → {str(t.id)[:8]}"
            )
            _set_spawn_reason(source_task, SPAWN_REASON_DEDUP)
            session.add(source_task)
            return (t, False)

        if task_type == "fix" and t_type == "review":
            from app.failure_classification import (
                AGENT_TIMEOUT,
                persist_failure_category,
            )  # noqa: PLC0415

            t.status = TaskStatus.CANCELLED
            t.latest_status_message = (
                f"Superseded by fix on PR #{pr_number}: review was scoped to "
                f"a pre-conflict / pre-CI-failure snapshot"
            )
            persist_failure_category(t, AGENT_TIMEOUT)
            session.add(t)
            superseded_stale_task = True
            logger.warning(
                f"[PIPELINE] Cancelling in-flight review {t.id} on PR #{pr_number} "
                f"so fix can proceed (review snapshot is stale)"
            )
            continue

        logger.info(
            f"[PIPELINE] Deferring {task_type} spawn for PR #{pr_number} — "
            f"in-flight {t_type} task {t.id} must finish first"
        )
        source_task.latest_status_message = (
            f"{task_type.title()} deferred for PR #{pr_number} — "
            f"waiting on active {t_type} task {str(t.id)[:8]}"
        )
        _set_spawn_reason(source_task, SPAWN_REASON_DEFERRED)
        session.add(source_task)
        return (None, False)

    if superseded_stale_task:
        # Release the active-idempotency partial index before inserting the
        # replacement task in the same transaction.
        await session.flush()

    # Per-PR iteration cap: count completed review/fix tasks for this PR across all chains.
    # Excludes archived tasks (manual iteration-cap reset path) and infra-class
    # failures: a task that died in VM provisioning / rate limit / agent timeout
    # / external API / stale-info push never actually ran a review, so counting
    # it toward the per-PR iteration cap would trip the cap with zero real
    # reviews on the PR.
    #
    # Push pr_number + type filters into SQL via the partial expression index
    # ``ix_task_payloads_pr_number`` (alembic 0057). The infra-class failure
    # filter remains Python-side because INFRA_CLASSES is composed dynamically.
    # ``load_only`` keeps the row narrow — we only need status/context/payloads
    # for the post-filter; status_log and other JSONB columns stay deferred.
    from app.failure_classification import INFRA_CLASSES  # noqa: PLC0415

    # Dialect-conditional WHERE for the per-PR cap query (audit 2026-04-29
    # round 2 / finding C2). Postgres requires byte-exact expression match
    # for ``ix_task_payloads_pr_number`` (built on
    # ``(payloads -> 0 ->> 'pr_number')`` per alembic 0057). The
    # ``.as_string() == str(pr_number)`` form compiles to
    # ``CAST(((payloads -> 0) ->> 'pr_number') AS VARCHAR) = '55'``; the
    # planner is smart enough to drop the redundant CAST AS VARCHAR (text
    # already) and matches the index — verified via EXPLAIN.
    #
    # SQLite test path: ``.as_string()`` compiles to
    # ``JSON_EXTRACT(JSON_QUOTE(JSON_EXTRACT(payloads, '$[0]')), ...)``
    # which double-quotes the int and never matches. ``func.json_extract``
    # reads the value as a primitive (typeof = 'integer' for an int
    # payload) — that's the form that works on SQLite. SQLite has no
    # expression index here (the migration creates a regular B-tree on
    # ``payloads`` for the SQLite branch), so seq-scan is acceptable for
    # tests. Postgres production path is the source of truth and gets the
    # index acceleration.
    dialect_name = session.bind.dialect.name if session.bind else "postgresql"
    if dialect_name == "sqlite":
        pr_number_match = (
            func.json_extract(Task.payloads, "$[0].pr_number") == pr_number
        )
        type_match = func.json_extract(Task.payloads, "$[0].type").in_(
            ("review", "fix")
        )
    else:
        pr_number_match = Task.payloads[0]["pr_number"].as_string() == str(pr_number)
        type_match = Task.payloads[0]["type"].as_string().in_(("review", "fix"))
    pr_pipeline_tasks = (
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.status,
                        Task.context,
                        Task.payloads,
                        Task.archived,
                        Task.project_id,
                        Task.created_at,
                        Task.latest_status_message,
                        # status_log must be loaded eagerly: the event listener on
                        # latest_status_message setter (models.py:_on_status_message_set)
                        # reads target.status_log to append. Under async SQLAlchemy, a
                        # lazy-load of a deferred attribute during a setter event raises
                        # MissingGreenlet — see memory note "load_only Task event-listener
                        # gotcha". The repeat-fingerprint halt below mutates
                        # latest_status_message on the most-recent failed task; without
                        # this column the mutation crashes mid-cycle.
                        Task.status_log,
                    )
                )
                .where(
                    Task.project_id == source_task.project_id,
                    Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                    Task.archived.is_(False),
                    pr_number_match,
                    type_match,
                )
            )
        )
        .scalars()
        .all()
    )

    # ── Repeat-fingerprint halt ─────────────────────────────────────────
    # If the last two same-type FAILED tasks in this chain share an identical
    # failure fingerprint, the agent's code edits aren't changing the failure
    # shape — signature of a precondition gap (missing system lib, baseline
    # debt, toolchain mismatch). Halt: reclassify the most recent FAILED task
    # to BLOCKED_PRECONDITION (INFRA-class — won't burn cap or get auto-
    # resurrected), set requires_approval=True on the chain's original task
    # (mirrors stuck-cycle), and post a ProjectMessage so the strategic agent
    # (or operator) can decide whether to spawn a precondition-fix direction.
    #
    # This runs regardless of source_task.status (FAILED, EVALUATING-after-
    # FAILED-with-open-PR, etc.) because the FAILED → EVALUATING transition
    # in agent_status.py happens before this function is called for the
    # handoff path. The signal lives in chain history (pr_pipeline_tasks),
    # not in source_task's current status. Skip on COMPLETED source — a
    # successful task means the chain IS making progress, even if prior
    # iterations failed identically.
    if source_task.status != TaskStatus.COMPLETED:
        src_payload_type = source_task.first_payload.get("type", "")
        if src_payload_type:
            same_type_failures: list[Task] = []
            for _t in pr_pipeline_tasks:
                if _t.status != TaskStatus.FAILED:
                    continue
                if _t.first_payload.get("type", "") != src_payload_type:
                    continue
                _t_chain = (
                    (_t.context or {}).get("review_loop", {}).get("original_task_id")
                    or _t.first_payload.get("original_task_id")
                    or ""
                )
                if _t_chain != resolved_original_id:
                    continue
                same_type_failures.append(_t)
            if len(same_type_failures) >= 2:
                same_type_failures.sort(key=lambda x: x.created_at)
                a, b = same_type_failures[-2], same_type_failures[-1]
                fp_a = _failure_fingerprint_msg(a.latest_status_message)
                fp_b = _failure_fingerprint_msg(b.latest_status_message)
                if fp_a and fp_a == fp_b:
                    # Idempotency: if this halt already fired for `b` (we
                    # stamped blocked_repeat_fingerprint in a prior cycle),
                    # don't reclassify or re-post the BLOCKER message — just
                    # halt the spawn and exit. Without this guard, repeated
                    # _spawn_pipeline_task calls for the same chain (e.g.
                    # rollup retries) would create duplicate ProjectMessages.
                    already_halted = bool(
                        (b.context or {}).get("blocked_repeat_fingerprint")
                    )
                    logger.warning(
                        f"[PIPELINE] Repeat-fingerprint halt on PR #{pr_number} "
                        f"chain={resolved_original_id[:8]} type={src_payload_type}: "
                        f"prior={str(a.id)[:8]} and most-recent={str(b.id)[:8]} "
                        f"both failed with fp={fp_b} — halting spawn"
                        f"{' (already classified, skipping reclassify)' if already_halted else ', reclassifying most-recent as BLOCKED_PRECONDITION'}."
                    )
                    if not already_halted:
                        # Reclassify b (the most recent FAILED) so its row stops
                        # contributing to CODE_ERROR cap pressure and so any
                        # downstream consumer (digest, alerts, direction-health)
                        # sees the same reason as the BLOCKER message.
                        _b_ctx = dict(b.context or {})
                        _b_ctx["failure_category"] = "BLOCKED_PRECONDITION"
                        _b_ctx["blocked_repeat_fingerprint"] = fp_b
                        _b_ctx["blocked_repeat_prior_task_id"] = str(a.id)
                        b.context = _b_ctx
                        _b_msg = b.latest_status_message or ""
                        if not _b_msg.lstrip().lower().startswith("blocker:"):
                            b.latest_status_message = (
                                f"BLOCKER: identical failure to prior iteration "
                                f"({str(a.id)[:8]}, fp={fp_b}) — code-shaped fix has "
                                f"no effect, precondition gap suspected. Original: "
                                f"{_b_msg[:200]}"
                            )
                        session.add(b)
                        # Surface to operator/strategic via requires_approval
                        # on the chain's original task — mirrors stuck-cycle.
                        try:
                            _orig = await session.get(
                                Task, uuid.UUID(resolved_original_id)
                            )
                            if _orig:
                                _orig.requires_approval = True
                                _orig.latest_status_message = (
                                    f"Repeat-fingerprint halt on PR #{pr_number}: last "
                                    f"two {src_payload_type} attempts failed identically "
                                    f"(fp={fp_b}). Likely a precondition gap — manual "
                                    f"review or strategic-agent decision required."
                                )
                                session.add(_orig)
                        except Exception as _orig_err:
                            logger.warning(
                                f"[PIPELINE] Failed to mark requires_approval on chain "
                                f"original {resolved_original_id[:8]}: {_orig_err}",
                                exc_info=True,
                            )
                        try:
                            await _post_repeat_fingerprint_blocker_message(
                                session,
                                source_task=b,
                                pr_number=pr_number,
                                fingerprint=fp_b,
                                prior_task_id=a.id,
                            )
                        except Exception as _msg_err:
                            logger.warning(
                                f"[PIPELINE] Failed to post repeat-fingerprint BLOCKER "
                                f"message for PR #{pr_number}: {_msg_err}",
                                exc_info=True,
                            )
                    _set_spawn_reason(source_task, SPAWN_REASON_BLOCKED_REPEAT)
                    session.add(source_task)
                    return (None, False)

    def _counts_toward_cap(t: Task) -> bool:
        if t.status == TaskStatus.FAILED:
            fc = (t.context or {}).get("failure_category")
            if fc in INFRA_CLASSES:
                return False
        return True

    pr_iterations = sum(1 for t in pr_pipeline_tasks if _counts_toward_cap(t))
    # Load direction data for direction-aware cap adjustment.
    direction_data: dict | None = None
    direction_id = (source_task.context or {}).get("direction_id")
    if direction_id:
        from app.models import Project

        project = await session.get(Project, source_task.project_id)
        if project:
            for d in project.directions or []:
                if d.get("id") == direction_id:
                    direction_data = d
                    break

    global_cap = _max_review_iterations(source_task, direction_data)
    # Rollup reviews accumulate iterations across retry attempts — always use the max cap
    # so transient review failures don't permanently block rollup merges. Detection uses
    # context flags set authoritatively by rollup_management when spawning review/fix tasks.
    is_rollup = (source_task.context or {}).get("_is_rollup_review") is True or (
        source_task.context or {}
    ).get("_rollup_retries") is not None
    if is_rollup:
        global_cap = MAX_REVIEW_FIX_ITERATIONS
    if pr_iterations >= global_cap:
        logger.warning(
            f"[PIPELINE] PR #{pr_number} hit global review-fix cap ({pr_iterations} iterations across all chains)"
        )
        await _handle_cap_exhaustion(
            source_task,
            resolved_original_id,
            pr_number,
            pr_iterations,
            session,
            scope="global",
        )
        _set_spawn_reason(source_task, SPAWN_REASON_CAP)
        session.add(source_task)
        return (None, False)

    # Track review-fix iteration count to prevent infinite loops (per-chain)
    source_loop = (source_task.context or {}).get("review_loop", {})
    iteration = source_loop.get("iteration", 0) + 1
    per_chain_cap = _max_review_iterations(source_task, direction_data)

    if iteration > per_chain_cap:
        logger.warning(
            f"[PIPELINE] Review-fix cycle limit reached for PR #{pr_number} after {iteration - 1} iterations"
        )
        await _handle_cap_exhaustion(
            source_task,
            resolved_original_id,
            pr_number,
            per_chain_cap,
            session,
            scope="per-chain",
        )
        _set_spawn_reason(source_task, SPAWN_REASON_CAP)
        session.add(source_task)
        return (None, False)

    idempotency_key = _pipeline_task_idempotency_key(
        project_id=source_task.project_id,
        task_type=task_type,
        pr_number=pr_number,
        original_task_id=resolved_original_id,
        iteration=iteration,
        is_rollup=is_rollup,
    )
    existing_by_key = await _get_active_pipeline_task_by_key(session, idempotency_key)
    if existing_by_key is not None:
        logger.warning(
            f"[PIPELINE] Reusing idempotent {task_type} spawn for PR #{pr_number}: "
            f"{existing_by_key.id}"
        )
        source_task.latest_status_message = (
            f"{task_type.title()} skipped for PR #{pr_number} — "
            f"idempotent spawn already active → {str(existing_by_key.id)[:8]}"
        )
        _set_spawn_reason(source_task, SPAWN_REASON_DEDUP)
        session.add(source_task)
        return (existing_by_key, False)

    vm_name = f"agent-vm-{uuid.uuid4().hex}"

    # Determine parent: use the original task's parent (so review/fix are siblings of the
    # create task under the plan/research parent), falling back to original task itself.
    # resolved_original_id was computed at the top of this function.
    parent_id = None
    inherited_priority = source_task.priority
    orig: Task | None = None
    try:
        orig = await session.get(Task, uuid.UUID(resolved_original_id))
        if orig and orig.parent_task_id:
            parent_id = orig.parent_task_id
        elif orig:
            parent_id = orig.id
        if orig:
            inherited_priority = orig.priority
    except Exception:
        logger.debug(
            f"[PIPELINE] Could not inherit properties from original task {resolved_original_id[:8] if resolved_original_id else '?'}"
        )

    # Cross-cutting safeguard gate: on the first review spawn for a PR that spans
    # multiple top-level modules, is large-scope, or has an overflow warning on a
    # large diff, spawn a security_audit safeguard task and block auto-merge until
    # it completes. Skip for rollup reviews — those are already aggregated.
    if task_type == "review" and iteration == 1 and orig is not None and not is_rollup:
        try:
            await maybe_spawn_cross_cutting_safeguard(
                orig, pr_number, branch, base_branch, session
            )
        except Exception as _sg_err:
            logger.warning(
                f"[PIPELINE] Cross-cutting safeguard spawn failed for PR #{pr_number}: {_sg_err}",
                exc_info=True,
            )

    # Apply per-task-type model/effort defaults from settings
    type_model = _runtime_config.get("task_type_models", {}).get(task_type) or None
    type_effort = _runtime_config.get("task_type_efforts", {}).get(task_type) or None

    if task_type == "review":
        title = f"Review PR #{pr_number}"
        payload_data = {
            "type": "review",
            "pr_number": pr_number,
            "branch": branch,
            "base_branch": base_branch,
            "original_prompt": original_prompt,
            "original_task_id": resolved_original_id,
            "original_task_type": source_task.first_payload.get("type", ""),
            **({"model": type_model} if type_model else {}),
            **({"effort": type_effort} if type_effort else {}),
        }
    else:
        title = f"Fix PR #{pr_number}"
        # Prior review feedback is carried via review_loop.review_history in the
        # task context, which the fix agent formats with severity tags and file/line
        # locations. Avoid duplicating it here as unstructured text.
        payload_data = {
            "type": "fix",
            "pr_number": pr_number,
            "branch": branch,
            "base_branch": base_branch,
            "prompt": fix_prompt,
            "original_prompt": original_prompt,
            "original_task_id": resolved_original_id,
            **({"model": type_model} if type_model else {}),
            **({"effort": type_effort} if type_effort else {}),
        }

    # Build review_history: carry forward from source task, appending current feedback if spawning a fix
    prev_review_history = source_loop.get("review_history", [])
    # The PR's actual changed files at this iteration boundary, used by drift
    # detection. The review task itself doesn't push commits and so won't have
    # _pr_changed_files in its own context — fall back to the original task
    # (which is the create/fix that pushed the commits being reviewed).
    _source_changed_files = (
        (source_task.context or {}).get("_pr_changed_files")
        or ((orig.context or {}).get("_pr_changed_files") if orig else None)
        or []
    )
    new_review_entry: dict[str, object] = {
        "iteration": iteration - 1,
        "feedback": current_feedback,
        # Gate: this branch fires when spawning a fix from REQUEST_CHANGES.
        # APPROVE iterations are written separately at the CI-wait transition
        # in task_lifecycle.py so the trajectory shows them too.
        "verdict": "REQUEST_CHANGES",
        "changed_files": list(_source_changed_files)[:30],
    }
    if current_issues:
        new_review_entry["issues"] = current_issues
    new_review_history = (
        [*prev_review_history, new_review_entry]
        if task_type == "fix" and current_feedback
        else prev_review_history
    )

    churn_detections = int(source_loop.get("churn_detections", 0) or 0)
    # Stuck-cycle detection: if the same CRITICAL/MAJOR issues repeat across consecutive
    # iterations, escalate for manual review instead of consuming the full iteration budget.
    if task_type == "fix" and iteration >= 2 and len(new_review_history) >= 2:
        prev_entry = new_review_history[-2]
        curr_entry = new_review_history[-1]
        prev_issues_list = prev_entry.get("issues") or []
        curr_issues_list = curr_entry.get("issues") or []
        prev_critical_major = {
            i.get("description", "").lower().strip()
            for i in prev_issues_list
            if i.get("severity", "").upper() in ("CRITICAL", "MAJOR")
            and i.get("description", "").strip()
        }
        curr_critical_major = [
            i.get("description", "").lower().strip()
            for i in curr_issues_list
            if i.get("severity", "").upper() in ("CRITICAL", "MAJOR")
            and i.get("description", "").strip()
        ]
        # Refactor tasks get a higher threshold — structural review feedback often repeats
        # because the fix agent cannot fully resolve architectural concerns in scope
        source_type = (source_task.first_payload or {}).get("type", "")
        is_refactor = source_type == "refactor"
        source_scope = (source_task.context or {}).get("scope", "medium")
        stuck_threshold, issue_threshold, text_threshold = _stuck_thresholds(
            source_scope, is_refactor
        )

        is_stuck = False
        stuck_detail = ""

        if curr_critical_major and prev_critical_major:
            matched_count = sum(
                1 for desc in curr_critical_major if desc in prev_critical_major
            )
            total_count = len(curr_critical_major)
            if matched_count / total_count >= stuck_threshold:
                is_stuck = True
                stuck_detail = f"{matched_count}/{total_count} issues repeated"
            else:
                # Primary uses normalized exact equality, so it misses pure
                # rephrasings ("Missing null check in handler" ↔ "Handler lacks
                # null check"). Fall back to a Jaccard on the *concatenated*
                # CRITICAL/MAJOR descriptions: token overlap aggregated across
                # all issues distinguishes genuine rephrasings (high overall
                # overlap) from issues that only share boilerplate but target
                # different symbols (processUser vs processOrder — distinct
                # target tokens dilute aggregate overlap below the 0.65 floor).
                similarity = _feedback_jaccard(
                    " ".join(prev_critical_major), " ".join(curr_critical_major)
                )
                if similarity >= issue_threshold:
                    is_stuck = True
                    stuck_detail = (
                        f"rephrased-issue similarity {similarity:.0%} "
                        f"≥ {issue_threshold:.0%} "
                        f"(exact match {matched_count}/{total_count})"
                    )
        elif not prev_issues_list or not curr_issues_list:
            # Fallback when at least one side lacks structured issues (verdict parsing
            # fell back to log/keyword scraping). Compare normalized feedback text.
            prev_feedback = (prev_entry.get("feedback") or "").strip()
            curr_feedback = (curr_entry.get("feedback") or "").strip()
            if prev_feedback and curr_feedback:
                similarity = _feedback_jaccard(prev_feedback, curr_feedback)
                if similarity >= text_threshold:
                    is_stuck = True
                    stuck_detail = (
                        f"feedback similarity {similarity:.0%} ≥ {text_threshold:.0%}"
                    )

        # Standoff detection: distinct from generic stuck. When the prior fix
        # iteration submitted structured rationale for an issue AND the current
        # reviewer re-raised the same issue anyway, that's a genuine
        # disagreement — operator should adjudicate rather than burn another
        # cycle on a topic two agents already disagree about.  Counts only
        # responses whose verify steps are mechanical (verify_steps_ok at the
        # agent side; backend trusts what landed but applies the same check).
        is_standoff = False
        standoff_detail = ""
        if curr_critical_major and prev_critical_major:
            prior_responses = list(source_loop.get("fix_agent_responses") or [])
            # Match by issue_id (sha1 of normalized description) when present;
            # fall back to substring match on issue_summary so rationale
            # written before issue_id became standard still counts.
            standoff_issue_ids: set[str] = set()
            for r in prior_responses:
                if not isinstance(r, dict):
                    continue
                rid = r.get("issue_id")
                if isinstance(rid, str) and rid:
                    standoff_issue_ids.add(rid)
            # Re-raised description is in curr_critical_major (already lowered+stripped).
            # If any prior response targets one of those issues, the agent disputed
            # but the reviewer rejected — standoff.
            re_raised_with_response = 0
            for desc in curr_critical_major:
                # issue_id sha1 matching deferred until backend hashes
                # consistently; for now any rationale present + same issue
                # description re-appearing is treated as a standoff signal.
                if standoff_issue_ids and desc in prev_critical_major:
                    re_raised_with_response += 1
            if re_raised_with_response > 0 and standoff_issue_ids:
                is_standoff = True
                standoff_detail = (
                    f"{re_raised_with_response} issue(s) re-raised after fix-agent rationale; "
                    f"reviewer rejected the rationale and re-flagged"
                )

        if is_standoff:
            logger.warning(
                f"[PIPELINE] Standoff detected for PR #{pr_number}: {standoff_detail}"
            )
            # Treat the same as is_stuck — escalate to operator. Differs from
            # stuck only in WHY (genuine disagreement vs. agent not trying).
            is_stuck = True
            stuck_detail = standoff_detail or stuck_detail

        if is_stuck:
            logger.warning(
                f"[PIPELINE] Stuck cycle detected for PR #{pr_number}: {stuck_detail}"
            )
            # resolved_original_id is always non-empty (falls back to str(source_task.id))
            try:
                stuck_orig = await session.get(Task, uuid.UUID(resolved_original_id))
                if stuck_orig:
                    stuck_orig.requires_approval = True
                    stuck_orig.latest_status_message = (
                        f"Review-fix cycle appears stuck — same issues repeated across "
                        f"{iteration} iterations. Manual review required."
                    )
                    session.add(stuck_orig)
                    await session.commit()
                    try:
                        import asyncio as _asyncio
                        from app.notification_dispatch import (
                            dispatch_external_notification,
                        )

                        _asyncio.ensure_future(
                            dispatch_external_notification(
                                stuck_orig.project_id,
                                "APPROVAL_NEEDED",
                                {
                                    "task_id": str(stuck_orig.id),
                                    "task_title": stuck_orig.title
                                    or str(stuck_orig.id)[:8],
                                    "pr_number": pr_number,
                                    "iteration": iteration,
                                    "reason": "Review-fix cycle stuck — manual review required",
                                },
                            )
                        )
                    except Exception as _ne:
                        logger.warning(
                            f"[NOTIFY] APPROVAL_NEEDED dispatch setup failed: {_ne}"
                        )
                    try:
                        from app.autonomous_research import (
                            _spawn_review_convergence_investigation,
                        )  # noqa: PLC0415

                        if await _spawn_review_convergence_investigation(
                            stuck_orig,
                            session,
                            pr_number=pr_number,
                            iterations=iteration,
                            detail=stuck_detail,
                            trigger="review_stuck_cycle",
                            review_history=new_review_history,
                        ):
                            await session.commit()
                    except Exception as _ie:
                        logger.warning(
                            f"[PIPELINE] Stuck-cycle convergence investigation spawn failed for PR #{pr_number}: {_ie}",
                            exc_info=True,
                        )
            except Exception as e:
                logger.warning(
                    f"[PIPELINE] Failed to mark stuck cycle on {resolved_original_id}: {e}",
                    exc_info=True,
                )
            _set_spawn_reason(source_task, SPAWN_REASON_STUCK)
            session.add(source_task)
            return (None, False)

        # Issue-churn detection: complements stuck-cycle above. If total
        # CRITICAL+MAJOR count fails to decrease across the last
        # ISSUE_CHURN_WINDOW iterations (even though the specific issues are
        # rotating and so evading Jaccard stuck detection), reduce the
        # remaining iteration cap by ~1/3 and instruct the fix agent to
        # narrow focus. The stuck-cycle detector handles harsher intervention.
        is_churn, churn_counts = _detect_issue_churn(new_review_history)
        if is_churn and task_type == "fix":
            churn_detections += 1
            remaining = max(per_chain_cap - iteration, 0)
            reduced_cap = iteration + (remaining // 2)
            if reduced_cap < per_chain_cap:
                logger.warning(
                    f"[PIPELINE] Issue churn detected for PR #{pr_number} "
                    f"(detection #{churn_detections}): "
                    f"CRITICAL+MAJOR counts {churn_counts} non-decreasing over "
                    f"{ISSUE_CHURN_WINDOW} iterations. Reducing cap "
                    f"{per_chain_cap} → {reduced_cap}; injecting narrow-focus directive."
                )
                per_chain_cap = reduced_cap
            else:
                logger.warning(
                    f"[PIPELINE] Issue churn detected for PR #{pr_number} "
                    f"(detection #{churn_detections}): "
                    f"counts {churn_counts}; injecting narrow-focus directive."
                )
            narrow_prefix = _CHURN_NARROW_FOCUS_PREFIX.format(
                window=ISSUE_CHURN_WINDOW, counts=churn_counts
            )
            narrow_prefix += _format_repeating_issues_section(new_review_history)
            payload_data["prompt"] = narrow_prefix + (payload_data.get("prompt") or "")
            fix_prompt = narrow_prefix + (fix_prompt or "")
        elif (
            task_type == "fix"
            and per_chain_cap > 0
            and (iteration >= MID_BUDGET_NARROW_FOCUS_THRESHOLD * per_chain_cap)
        ):
            # Proactive mid-budget directive: softer wording, no cap reduction.
            mid_prefix = _MID_BUDGET_NARROW_FOCUS_PREFIX.format(
                iteration=iteration, cap=per_chain_cap
            )
            mid_prefix += _format_repeating_issues_section(new_review_history)
            logger.info(
                f"[PIPELINE] Mid-budget narrow-focus directive for PR #{pr_number} "
                f"(iteration {iteration}/{per_chain_cap})."
            )
            payload_data["prompt"] = mid_prefix + (payload_data.get("prompt") or "")
            fix_prompt = mid_prefix + (fix_prompt or "")
        elif task_type == "fix" and len(new_review_history) >= 2:
            # Early visibility: from iteration 2 onward, surface any repeating
            # CRITICAL/MAJOR issues without the churn/mid-budget urgency signals.
            repeating_section = _format_repeating_issues_section(new_review_history)
            if repeating_section:
                early_prefix = _EARLY_REPEATING_ISSUES_PREFIX + repeating_section
                payload_data["prompt"] = early_prefix + (
                    payload_data.get("prompt") or ""
                )
                fix_prompt = early_prefix + (fix_prompt or "")

    # Graduated scope-budget pressure cap: a medium-scope fix cycle's
    # remaining iterations are scaled down by how close the PR is to its
    # production file budget. Activation is at SCOPE_PRESSURE_WARN_THRESHOLD
    # (70%); the no-new-files prompt still only fires at
    # SCOPE_PRESSURE_HARD_CAP_PERCENTAGE (90%). The derived cap persists via
    # the spawned task's context so subsequent spawns respect the ceiling.
    scope_budget_hard_cap: int | None = None
    if task_type == "fix":
        _scope_for_pressure = (source_task.context or {}).get(
            "scope", source_loop.get("scope", "medium")
        )
        _changed_for_pressure = (
            (source_task.context or {}).get("_pr_changed_files")
            or ((orig.context or {}).get("_pr_changed_files") if orig else None)
            or []
        )
        _util_for_pressure = _compute_scope_budget_utilization(
            _changed_for_pressure, _scope_for_pressure
        )
        if (
            _util_for_pressure
            and _scope_for_pressure == "medium"
            and _util_for_pressure.get("percentage", 0)
            >= SCOPE_PRESSURE_WARN_THRESHOLD * 100
        ):
            util_pct = _util_for_pressure["percentage"]
            base_remaining = per_chain_cap - iteration
            if base_remaining > 0:
                graduated_remaining = max(
                    SCOPE_PRESSURE_HARD_CAP_REMAINING,
                    round(base_remaining * (1.0 - util_pct / 100)),
                )
                hard_cap_target = iteration + graduated_remaining
                if hard_cap_target < per_chain_cap:
                    logger.warning(
                        f"[PIPELINE] Scope-budget pressure for PR #{pr_number}: "
                        f"utilization {util_pct}% ≥ "
                        f"{round(SCOPE_PRESSURE_WARN_THRESHOLD * 100)}% (non-test "
                        f"{_util_for_pressure['non_test_file_count']}/"
                        f"{_util_for_pressure['budget']}). Reducing cap "
                        f"{per_chain_cap} → {hard_cap_target} "
                        f"(graduated remaining={graduated_remaining})."
                    )
                    per_chain_cap = hard_cap_target
                scope_budget_hard_cap = hard_cap_target
            if util_pct >= SCOPE_PRESSURE_HARD_CAP_PERCENTAGE:
                hard_prefix = _SCOPE_PRESSURE_HARD_CAP_PREFIX.format(
                    count=_util_for_pressure["non_test_file_count"],
                    budget=_util_for_pressure["budget"],
                    pct=util_pct,
                )
                payload_data["prompt"] = hard_prefix + (
                    payload_data.get("prompt") or ""
                )
                fix_prompt = hard_prefix + (fix_prompt or "")

    payload_data = validate_internal_payload(
        payload_data, f"review_pipeline:{task_type}:pr#{pr_number}"
    )
    source_ctx = source_task.context or {}
    propagated_scope = source_ctx.get("scope", source_loop.get("scope", "medium"))
    # Carry strategic context (direction_id, related_files, parent_context, scope, …)
    # from source task so fix/review agents see the same direction/intent the create
    # task had. Skip underscore-prefixed internal flags and override review_loop below.
    propagated_ctx = {
        k: v
        for k, v in source_ctx.items()
        if not k.startswith("_") and k != "review_loop"
    }
    propagated_ctx["scope"] = propagated_scope
    # Propagate direction title (text snippet) so agent PR-body footers can
    # show a human-readable label alongside the direction ID.
    if direction_data and not propagated_ctx.get("direction_title"):
        _dir_text = (direction_data.get("text") or "").strip()
        if _dir_text:
            propagated_ctx["direction_title"] = _dir_text[:80]
    # Carry forward the create task's self-review outcome so the review agent
    # can see which files were verified (truncated_files_read) and whether the
    # self-review was marked incomplete, and focus attention on blind spots
    # rather than re-reviewing files the self-review already checked.
    prev_self_review_outcome = source_ctx.get("_self_review_outcome")
    if prev_self_review_outcome:
        propagated_ctx["_self_review_outcome"] = prev_self_review_outcome
    # Scope-pressure awareness: expose file-budget utilization to both fix and
    # review agents. Fix agents use it to prefer minimal edits; reviewers use it
    # to weigh whether requesting changes would push the PR over its budget.
    changed_files = (
        source_ctx.get("_pr_changed_files")
        or ((orig.context or {}).get("_pr_changed_files") if orig else None)
        or []
    )
    utilization = _compute_scope_budget_utilization(changed_files, propagated_scope)
    if utilization:
        propagated_ctx["scope_budget_utilization"] = utilization
    overflow_warning = _compute_scope_overflow_warning(changed_files, propagated_scope)
    if overflow_warning:
        propagated_ctx["scope_overflow_warned"] = True
        propagated_ctx["scope_overflow_warning"] = overflow_warning
        source_ctx_updated = dict(source_ctx)
        source_ctx_updated["scope_overflow_warned"] = True
        source_ctx_updated["scope_overflow_warning"] = overflow_warning
        source_task.context = source_ctx_updated
        source_ctx = source_ctx_updated
        signal_already_emitted = bool(
            source_ctx.get(SCOPE_OVERFLOW_PR_SIGNAL_EMITTED_KEY)
            or (
                (orig.context or {}).get(SCOPE_OVERFLOW_PR_SIGNAL_EMITTED_KEY)
                if orig
                else False
            )
        )
        if not signal_already_emitted:
            try:
                from app.github import add_issue_label, create_pr_comment  # noqa: PLC0415
                from app.routers.agent import get_github_token_for_task  # noqa: PLC0415

                token_task_id = orig.id if orig is not None else source_task.id
                gh_token, owner, repo = await get_github_token_for_task(
                    token_task_id, session
                )
                changed_paths = overflow_warning.get("changed_files") or []
                file_lines = "\n".join(f"- `{path}`" for path in changed_paths)
                scope_label = overflow_warning.get("scope") or propagated_scope
                comment_body = (
                    "⚠️ Scope overflow detected for this PR.\n\n"
                    f"- Declared scope: `{scope_label}`\n"
                    f"- Non-test files changed: `{overflow_warning['non_test_file_count']}`\n"
                    f"- Scope budget: `{overflow_warning['threshold']}`\n\n"
                    "Affected files (capped):\n"
                    f"{file_lines}"
                )
                await add_issue_label(
                    gh_token, owner, repo, pr_number, "scope-overflow"
                )
                await create_pr_comment(gh_token, owner, repo, pr_number, comment_body)

                source_ctx_updated[SCOPE_OVERFLOW_PR_SIGNAL_EMITTED_KEY] = True
                source_task.context = source_ctx_updated
                source_ctx = source_ctx_updated
                if orig is not None:
                    orig_ctx = dict(orig.context or {})
                    orig_ctx[SCOPE_OVERFLOW_PR_SIGNAL_EMITTED_KEY] = True
                    orig.context = orig_ctx
                    session.add(orig)
            except Exception as e:
                logger.warning(
                    f"[PIPELINE] Failed to emit scope-overflow PR signal for PR #{pr_number}: {e}",
                    exc_info=True,
                )
        if task_type == "review":
            overflow_instruction = (
                "Scope-overflow context: this PR exceeded the declared scope file "
                f"budget ({overflow_warning['non_test_file_count']} non-test files "
                f"vs threshold {overflow_warning['threshold']}). During review, "
                "explicitly assess whether the additional files form a coherent, "
                "necessary unit or indicate scope sprawl that should be deferred."
            )
            payload_data["original_prompt"] = (
                (payload_data.get("original_prompt") or "").strip()
                + "\n\n"
                + overflow_instruction
            ).strip()
    # Persist the scope-budget hard cap (either newly triggered this spawn or
    # carried forward from a prior spawn) so _max_review_iterations enforces
    # it on subsequent review/fix iterations for this chain.
    prior_hard_cap = source_ctx.get("_scope_budget_hard_cap")
    effective_hard_cap = scope_budget_hard_cap
    # Only propagate a prior hard cap while the PR is still under scope pressure.
    # If utilization has dropped below the warn threshold (or is unknown), the
    # fix chain has successfully narrowed its footprint and the iteration
    # restriction is no longer warranted — let the cap relax back to per_chain_cap.
    _prior_cap_still_relevant = (
        utilization is not None
        and utilization.get("percentage", 0) >= SCOPE_PRESSURE_WARN_THRESHOLD * 100
    )
    if (
        isinstance(prior_hard_cap, int)
        and prior_hard_cap > 0
        and _prior_cap_still_relevant
    ):
        effective_hard_cap = (
            min(effective_hard_cap, prior_hard_cap)
            if effective_hard_cap is not None
            else prior_hard_cap
        )
    if effective_hard_cap is not None:
        propagated_ctx["_scope_budget_hard_cap"] = effective_hard_cap
    # Propagate diagnosis context from the original create task when the PR was
    # spawned by autonomous remediation. The reviewer uses these to verify the
    # fix targets the diagnosed root cause rather than doing a generic review.
    orig_ctx_src = (orig.context or {}) if orig else {}
    if orig_ctx_src.get("diagnosis_task_id"):
        for _dk in (
            "diagnosis_task_id",
            "diagnosis_root_cause",
            "diagnosis_recommended_fix",
            "severity",
            "diagnosis_confidence",
            "diagnosis_fingerprint",
            "error_fingerprint",
            "trigger_failure_class",
            "trigger_failure_taxonomy",
        ):
            if orig_ctx_src.get(_dk) is not None:
                propagated_ctx[_dk] = orig_ctx_src[_dk]

    if task_type == "fix" and fix_reason == "ci_failure" and ci_failure_logs:
        # Truncate from the start, not the end — pytest's FAILED summary lines
        # (the ones the agent extracts selectors from) sit at the tail of each
        # job's log section.
        propagated_ctx["ci_failure_logs"] = str(ci_failure_logs)[-5000:]

    if task_type == "fix" and investigated_files:
        # Reviewer's working set forwarded so the fix agent can start at the
        # paths the reviewer already opened, instead of rediscovering them.
        # Capped to keep per-task context bounded; the cap mirrors the agent
        # side's 25-path slice when it builds verdict_json.
        propagated_ctx["investigated_files"] = [
            str(p) for p in investigated_files if isinstance(p, str) and p
        ][:25]

    # Propagate the original create task's planned file list so the reviewer can
    # objectively detect scope creep (files changed but not in the original plan).
    planned_files = (orig.context or {}).get("related_files") if orig else None
    if not planned_files:
        planned_files = source_ctx.get("related_files")
    if planned_files:
        propagated_ctx["_planned_files"] = list(planned_files)
    # Drift signal: surface files that drifted in (no plan/approval link) or
    # drifted out (lost from approved iteration). Computed at spawn time so the
    # fix-agent prompt builder can render the section without re-deriving from
    # scratch. Only computed when spawning a fix — reviewers don't need it.
    drift_signal: dict | None = None
    if task_type == "fix":
        _planned_for_drift = list(planned_files) if planned_files else []
        # As above: review tasks don't push commits, fall back to the original
        # task's _pr_changed_files (the create/fix that produced the PR state
        # the review verdict was made against).
        _current_files = list(
            (source_task.context or {}).get("_pr_changed_files")
            or ((orig.context or {}).get("_pr_changed_files") if orig else None)
            or []
        )
        drift_signal = compute_drift_signal(
            new_review_history or [],
            _planned_for_drift,
            _current_files,
        )

    # Fix-agent responses carry-forward: if the prior fix iteration wrote
    # structured rationale for disputed issues, accumulate it onto the next
    # reviewer's context so the verify-mode prompt block can render. Each
    # entry persists across iterations until the issue is resolved.
    prior_responses = list(source_loop.get("fix_agent_responses") or [])
    new_responses = list((source_task.context or {}).get("_fix_agent_responses") or [])
    # Cap aggregate at ~30 (recent-first) so context doesn't bloat over long
    # cycles. Per-iteration cap of 5 is enforced at write time elsewhere.
    accumulated_responses = (prior_responses + new_responses)[-30:]

    propagated_ctx["review_loop"] = {
        "pr_number": pr_number,
        "branch": branch,
        "base_branch": base_branch,
        "original_task_id": resolved_original_id,
        "iteration": iteration,
        "max_iterations": per_chain_cap,
        **({"review_history": new_review_history} if new_review_history else {}),
        **({"fix_reason": fix_reason} if fix_reason else {}),
        **({"churn_detections": churn_detections} if churn_detections else {}),
        **({"scope_overflow_warned": True} if overflow_warning else {}),
        **({"drift_signal": drift_signal} if drift_signal else {}),
        **(
            {"fix_agent_responses": accumulated_responses}
            if accumulated_responses
            else {}
        ),
    }

    # Phase 2c: warm-VM continuity pin. When the runtime flag is on AND the
    # source task's VM is still alive (IN_PROGRESS or completed within the
    # warm-handoff window), pin the spawned task to that VM and leave
    # vm_instance_id=NULL so the orchestrator's provisioning loop knows to
    # check the heartbeat map before provisioning. The agent on the live
    # VM picks the task up via /api/agent/next-role. If the warm window
    # expires or the VM dies before pickup, the orchestrator clears the
    # warm fields and provisions normally.
    spawn_vm_instance_id: str | None = vm_name
    warm_continuity_enabled = bool(
        _runtime_config.get("warm_continuity_enabled", False)
    )
    if (
        warm_continuity_enabled
        and not suppress_warm_handoff
        and source_task.vm_instance_id
    ):
        # Source task is eligible if it's currently running OR completed
        # recently enough to keep the warm chain warm. Use the most recent
        # heartbeat as the "completion" reference. Note: routers/agent_status.py
        # does NOT set last_heartbeat=_utcnow() on the terminal write, so this
        # measures time since the LAST 30s task-heartbeat tick — giving up to
        # 30s less margin than the WARM_HANDOFF_WINDOW_SECS budget suggests.
        # The 240s window absorbs that gap with room to spare.
        is_in_progress = source_task.status == TaskStatus.IN_PROGRESS
        is_recent_terminal = False
        if not is_in_progress and source_task.last_heartbeat is not None:
            elapsed = (_utcnow() - source_task.last_heartbeat).total_seconds()
            is_recent_terminal = elapsed <= WARM_HANDOFF_WINDOW_SECS
        if is_in_progress or is_recent_terminal:
            propagated_ctx["warm_target_vm_id"] = source_task.vm_instance_id
            # Stored as int — query side uses .as_string() which on Postgres reads
            # ->> (text), so int → "<n>" conversion is automatic. SQLite test
            # backend behavior is not load-bearing here, but keeping the type
            # consistent makes the test fixtures reflect production.
            propagated_ctx["warm_target_pr"] = (
                int(pr_number) if pr_number is not None else None
            )
            propagated_ctx["warm_handoff_deadline"] = (
                _utcnow() + timedelta(seconds=WARM_HANDOFF_WINDOW_SECS)
            ).isoformat()
            spawn_vm_instance_id = None
            logger.info(
                f"[PIPELINE] Warm-VM pin set for {task_type} PR #{pr_number}: "
                f"target_vm={source_task.vm_instance_id} "
                f"deadline={propagated_ctx['warm_handoff_deadline']}"
            )

    new_task = Task(
        project_id=source_task.project_id,
        title=title,
        description=fix_prompt or f"Automated {task_type} for PR #{pr_number}",
        parent_task_id=parent_id,
        priority=inherited_priority,
        payloads=[payload_data],
        context=propagated_ctx,
        idempotency_key=idempotency_key,
        vm_instance_id=spawn_vm_instance_id,
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await _get_active_pipeline_task_by_key(session, idempotency_key)
        if existing is None:
            raise
        logger.warning(
            f"[PIPELINE] Concurrent {task_type} spawn for PR #{pr_number} "
            f"hit idempotency key; using existing task {existing.id}"
        )
        reloaded_source = await session.get(Task, source_task_id)
        if reloaded_source is not None:
            reloaded_source.latest_status_message = (
                f"{task_type.title()} skipped for PR #{pr_number} — "
                f"idempotent spawn already active → {str(existing.id)[:8]}"
            )
            _set_spawn_reason(reloaded_source, SPAWN_REASON_DEDUP)
            session.add(reloaded_source)
            await session.commit()
        return (existing, False)
    await session.refresh(new_task)

    history = TaskHistory(
        task_id=new_task.id,
        action="SPAWNED",
        details={
            "spawned_by": "REVIEW_PIPELINE",
            "type": task_type,
            "pr_number": pr_number,
        },
    )
    session.add(history)
    await session.commit()

    await broadcast_to_project(
        str(source_task.project_id),
        "task_updated",
        {"task_id": str(new_task.id), "status": TaskStatus.QUEUED},
    )
    _set_spawn_reason(source_task, SPAWN_REASON_SPAWNED)
    session.add(source_task)
    return (new_task, True)
