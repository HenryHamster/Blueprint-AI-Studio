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

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Task, TaskHistory, TaskStatus
from app.payload_schemas import validate_internal_payload
from app.shared_state import runtime_config as _runtime_config
from app.broadcast import broadcast_to_project
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


MAX_REVIEW_FIX_ITERATIONS = 16

STUCK_CYCLE_THRESHOLD_DEFAULT = 0.5
STUCK_CYCLE_THRESHOLD_REFACTOR = 0.65
# Medium-scope tasks juggle more complexity and are more likely to produce
# repeating CRITICAL/MAJOR issues across iterations. Detect the stuck cycle
# 1-2 iterations earlier than the default to save the remaining budget.
STUCK_CYCLE_THRESHOLD_MEDIUM = 0.45

# Token-level Jaccard thresholds used by the feedback-text fallback when one or
# both review_history entries lack a structured `issues` list (e.g. verdict
# parsing fell back to log/keyword scraping). Set higher than the structured
# thresholds because text comparison is noisier.
STUCK_CYCLE_TEXT_THRESHOLD_DEFAULT = 0.7
STUCK_CYCLE_TEXT_THRESHOLD_REFACTOR = 0.75
STUCK_CYCLE_TEXT_THRESHOLD_MEDIUM = 0.65

# Secondary Jaccard thresholds for the rephrased-issue fallback: both sides
# have structured CRITICAL/MAJOR issues but the exact-match proportion falls
# below the primary structured threshold. Stays above 0.6 so that two issues
# sharing boilerplate tokens but targeting different symbols (e.g. "Missing
# error handling in processUser" vs "…processOrder" → Jaccard 0.6) do NOT
# trigger a false positive.
STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_DEFAULT = 0.65
STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_REFACTOR = 0.7
STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_MEDIUM = 0.6


def _stuck_thresholds(scope: str, is_refactor: bool) -> tuple[float, float, float]:
    """Return (exact_match, issue_similarity, text_similarity) thresholds.

    Refactor takes priority over scope because refactor feedback structurally
    repeats. Medium scope lowers all three thresholds so higher-complexity
    tasks bail out 1-2 iterations earlier than the default.
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
ISSUE_CHURN_WINDOW = 3


def _critical_major_count(entry: dict) -> int:
    return sum(
        1 for i in (entry.get("issues") or [])
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
        for issue in (entry.get("issues") or []):
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


_CHURN_NARROW_FOCUS_PREFIX = (
    "⚠️ ISSUE CHURN DETECTED: the prior {window} fix iterations resolved some "
    "issues but introduced new ones (CRITICAL+MAJOR counts: {counts}). "
    "Address ONLY the explicitly listed issues below. Do NOT modify files "
    "that are not directly referenced by those issues. Do NOT refactor, "
    "reformat, or clean up unrelated code in this iteration.\n\n"
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

# Utilization at/above this fraction of the scope file budget signals the fix
# agent to prefer minimal fixes over expansive refactoring — gives headroom
# before a fix iteration would tip the PR into scope-overflow territory.
SCOPE_PRESSURE_WARN_THRESHOLD = 0.7


def _is_test_file(path: str) -> bool:
    p = path.lower()
    if "/testing/" in p or p.startswith("testing/") or "/tests/" in p or p.startswith("tests/"):
        return True
    name = p.rsplit("/", 1)[-1]
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return ".test." in name or ".spec." in name


def _compute_scope_budget_utilization(
    changed_files: list[str] | None, scope: str
) -> dict | None:
    """Summarize how close a PR's file count is to its scope budget.

    Returns None when no signal is available (no files known, or unknown scope).
    ``percentage`` is a whole number (0-100+); callers surface guidance when it
    crosses ``SCOPE_PRESSURE_WARN_THRESHOLD``.
    """
    from app.research_completion import SCOPE_FILE_BUDGETS  # noqa: PLC0415
    if not changed_files:
        return None
    budget_info = SCOPE_FILE_BUDGETS.get(scope) or {}
    budget = budget_info.get("production_files_max") or budget_info.get("total_files_max")
    if not budget:
        return None
    non_test_count = sum(1 for f in changed_files if not _is_test_file(f))
    return {
        "scope": scope,
        "file_count": len(changed_files),
        "non_test_file_count": non_test_count,
        "budget": budget,
        "percentage": round(non_test_count / budget * 100),
    }


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
    scope = ctx.get('scope', 'medium')
    fix_reason = ctx.get('review_loop', {}).get('fix_reason', 'code_review')

    # CI-only fixes should converge fast (lint/type/test errors)
    if fix_reason == 'ci_failure':
        limit = 8
    # Small scope tasks: less code, fewer things to fix
    elif scope == 'small':
        limit = 10
    # Medium scope: moderate iteration budget
    elif scope == 'medium':
        limit = 12
    # Large scope or unknown: full budget
    else:
        limit = 16

    # Direction-aware reduction: cut cap by ~30% for consistently-rejected directions.
    # Only applies when direction_data is available and we're not already at the ci_failure cap.
    if direction_data is not None and fix_reason != 'ci_failure':
        from app.research_completion import _compute_acceptance_rate
        acceptance_rate = _compute_acceptance_rate(direction_data)
        # Check recent rejection ratio from last few history entries
        recent_history = (direction_data.get("research_history") or [])[-3:]
        recent_merged = sum(e.get("prs_merged", 0) for e in recent_history)
        recent_rejected = sum(e.get("prs_rejected", 0) for e in recent_history)
        high_rejection_ratio = (
            recent_merged > 0 and (recent_rejected / recent_merged) > 2.0
        ) or (
            recent_merged == 0 and recent_rejected >= 3
        )
        if (acceptance_rate is not None and acceptance_rate < 0.3) or high_rejection_ratio:
            limit = max(1, round(limit * 0.7))

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
    from app.failure_classification import AGENT_TIMEOUT, persist_failure_category  # noqa: PLC0415
    from app.main import _maybe_create_failure_alert  # noqa: PLC0415
    from app.github import close_pull_request, create_pr_comment  # noqa: PLC0415
    from app.routers.agent import get_github_token_for_task  # noqa: PLC0415
    from app.routers.agent_status import record_task_result  # noqa: PLC0415
    from app.shared_state import record_direction_result  # noqa: PLC0415
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    cap_label = "global" if scope == "global" else "per-chain"
    source_task.latest_status_message = (
        f"Review-fix cap reached for PR #{pr_number} ({iterations} iterations, {cap_label})"
    )
    session.add(source_task)

    try:
        orig = await session.get(Task, uuid.UUID(resolved_original_id))
    except Exception as e:
        logger.warning(f"[PIPELINE] Cap-exhaustion: failed to load orig {resolved_original_id[:8]}: {e}")
        return
    if orig is None:
        logger.warning(f"[PIPELINE] Cap-exhaustion: orig {resolved_original_id[:8]} not found")
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
    persist_failure_category(orig, AGENT_TIMEOUT)
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
        logger.warning(f"[PIPELINE] Cap-exhaustion: failed to commit orig {resolved_original_id[:8]}: {e}", exc_info=True)
        return

    # Surface cap-exhaustion to deploy_monitoring + direction health tracking.
    # Without this, cap failures bypass per-type failure gates and anomaly detection.
    record_task_result(
        success=False, failure_class=AGENT_TIMEOUT,
        task_id=str(orig.id), task_type=(orig.first_payload or {}).get("type", ""),
    )
    _direction_id = str((orig.context or {}).get("direction_id") or "")
    if _direction_id:
        record_direction_result(
            _direction_id, success=False, failure_class=AGENT_TIMEOUT,
            project_id=str(orig.project_id) if orig.project_id else "",
        )

    try:
        gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
        try:
            await create_pr_comment(
                gh_token, owner, repo, pr_number,
                f"Auto-closed: review-fix cap reached ({iterations} iterations, {cap_label}). "
                f"Manual review required. Reopen this PR to resume work."
            )
        except Exception as e:
            logger.warning(f"[PIPELINE] Cap-exhaustion: failed to comment on PR #{pr_number}: {e}")
        closed = await close_pull_request(
            gh_token, owner, repo, pr_number, project_id=str(orig.project_id)
        )
        if closed:
            logger.info(f"[PIPELINE] Closed PR #{pr_number} after cap exhaustion ({iterations} iterations)")
        else:
            logger.warning(f"[PIPELINE] close_pull_request returned False for PR #{pr_number}")
    except Exception as e:
        logger.warning(f"[PIPELINE] Cap-exhaustion: failed to close PR #{pr_number}: {e}", exc_info=True)

    try:
        await _maybe_create_failure_alert(
            orig, session,
            f"PR #{pr_number} hit review-fix cap ({iterations} iterations, {cap_label}) — "
            f"auto-closed, manual review required"
        )
    except Exception as e:
        logger.warning(f"[PIPELINE] Cap-exhaustion: failed to create alert for PR #{pr_number}: {e}")


async def _spawn_pipeline_task(
    source_task: Task, task_type: str, session: AsyncSession,
    pr_number: int, branch: str, base_branch: str,
    original_prompt: str = "", fix_prompt: str = "", original_task_id: str = "",
    current_feedback: str = "", fix_reason: str = "", current_issues: list | None = None
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
    # Resolve the chain identifier once, so the "already merged" guard, the
    # dedup guard, and the spawn path all agree on which chain this is.
    # A chain is identified by the *create* task that opened the PR; review/fix
    # tasks in the chain carry that id in payload.original_task_id. For a source
    # task that has no explicit chain id, fall back to its own id (the source IS
    # the create task in that case).
    resolved_original_id = (
        original_task_id
        or (source_task.context or {}).get("review_loop", {}).get("original_task_id", "")
        or (source_task.first_payload).get("original_task_id", "")
        or str(source_task.id)
    )

    # Guard: don't spawn if PR is already merged (check both original and source).
    try:
        orig_task = await session.get(Task, uuid.UUID(resolved_original_id))
        if orig_task and (orig_task.context or {}).get("pr_merged"):
            logger.info(f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — PR already merged (original task {resolved_original_id[:8]})")
            source_task.latest_status_message = f"PR #{pr_number} already merged — skipping {task_type}"
            session.add(source_task)
            return (None, False)
    except Exception:
        logger.debug(f"[PIPELINE] Could not verify pr_merged for original task {resolved_original_id[:8]}: skipping check")
    if (source_task.context or {}).get("pr_merged"):
        logger.info(f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — PR already merged (source task)")
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
    active_pipeline = (await session.execute(
        select(Task).where(
            Task.project_id == source_task.project_id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING]),
        )
    )).scalars().all()
    for t in active_pipeline:
        if t.id == source_task.id:
            continue
        t_payload = t.first_payload
        t_type = t_payload.get("type")
        t_orig = t_payload.get("original_task_id") or ""
        if (t_payload.get("pr_number") != pr_number
                or t_type not in ("review", "fix")
                or t_orig != resolved_original_id):
            continue

        if _is_pipeline_task_stale(t):
            from app.failure_classification import AGENT_TIMEOUT, persist_failure_category  # noqa: PLC0415
            t.status = TaskStatus.CANCELLED
            t.latest_status_message = (
                f"Superseded by new {task_type} for PR #{pr_number}: "
                f"no heartbeat for >{PIPELINE_TASK_STALE_MINUTES}min"
            )
            persist_failure_category(t, AGENT_TIMEOUT)
            session.add(t)
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
            session.add(source_task)
            return (t, False)

        if task_type == "fix" and t_type == "review":
            from app.failure_classification import AGENT_TIMEOUT, persist_failure_category  # noqa: PLC0415
            t.status = TaskStatus.CANCELLED
            t.latest_status_message = (
                f"Superseded by fix on PR #{pr_number}: review was scoped to "
                f"a pre-conflict / pre-CI-failure snapshot"
            )
            persist_failure_category(t, AGENT_TIMEOUT)
            session.add(t)
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
        session.add(source_task)
        return (None, False)

    # Per-PR iteration cap: count completed review/fix tasks for this PR across all chains
    # Filter to tasks matching this PR number (check payloads JSONB).
    # Excludes archived tasks (manual iteration-cap reset path) and infra-class
    # failures: a task that died in VM provisioning / rate limit / agent timeout
    # / external API / stale-info push never actually ran a review, so counting
    # it toward the per-PR iteration cap would trip the cap with zero real
    # reviews on the PR.
    from app.failure_classification import INFRA_CLASSES  # noqa: PLC0415
    pr_pipeline_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == source_task.project_id,
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            Task.archived.is_(False),
        )
    )).scalars().all()

    def _counts_toward_cap(t: Task) -> bool:
        if t.first_payload.get("pr_number") != pr_number:
            return False
        if t.first_payload.get("type") not in ("review", "fix"):
            return False
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
            for d in (project.directions or []):
                if d.get("id") == direction_id:
                    direction_data = d
                    break

    global_cap = _max_review_iterations(source_task, direction_data)
    # Rollup reviews accumulate iterations across retry attempts — always use the max cap
    # so transient review failures don't permanently block rollup merges. Detection uses
    # context flags set authoritatively by rollup_management when spawning review/fix tasks.
    is_rollup = (
        (source_task.context or {}).get("_is_rollup_review") is True
        or (source_task.context or {}).get("_rollup_retries") is not None
    )
    if is_rollup:
        global_cap = MAX_REVIEW_FIX_ITERATIONS
    if pr_iterations >= global_cap:
        logger.warning(f"[PIPELINE] PR #{pr_number} hit global review-fix cap ({pr_iterations} iterations across all chains)")
        await _handle_cap_exhaustion(
            source_task, resolved_original_id, pr_number, pr_iterations, session,
            scope="global",
        )
        return (None, False)

    # Track review-fix iteration count to prevent infinite loops (per-chain)
    source_loop = (source_task.context or {}).get("review_loop", {})
    iteration = source_loop.get("iteration", 0) + 1
    per_chain_cap = _max_review_iterations(source_task, direction_data)

    if iteration > per_chain_cap:
        logger.warning(f"[PIPELINE] Review-fix cycle limit reached for PR #{pr_number} after {iteration - 1} iterations")
        await _handle_cap_exhaustion(
            source_task, resolved_original_id, pr_number, per_chain_cap, session,
            scope="per-chain",
        )
        return (None, False)

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
        logger.debug(f"[PIPELINE] Could not inherit properties from original task {resolved_original_id[:8] if resolved_original_id else '?'}")

    # Apply per-task-type model/effort defaults from settings
    type_model = _runtime_config.get("task_type_models", {}).get(task_type) or None
    type_effort = _runtime_config.get("task_type_efforts", {}).get(task_type) or None

    if task_type == "review":
        title = f"Review PR #{pr_number}"
        payload_data = {"type": "review", "pr_number": pr_number, "branch": branch,
                        "base_branch": base_branch, "original_prompt": original_prompt,
                        "original_task_id": resolved_original_id,
                        "original_task_type": source_task.first_payload.get("type", ""),
                        **({"model": type_model} if type_model else {}),
                        **({"effort": type_effort} if type_effort else {})}
    else:
        title = f"Fix PR #{pr_number}"
        # Prior review feedback is carried via review_loop.review_history in the
        # task context, which the fix agent formats with severity tags and file/line
        # locations. Avoid duplicating it here as unstructured text.
        payload_data = {"type": "fix", "pr_number": pr_number, "branch": branch,
                        "base_branch": base_branch, "prompt": fix_prompt,
                        "original_prompt": original_prompt,
                        "original_task_id": resolved_original_id,
                        **({"model": type_model} if type_model else {}),
                        **({"effort": type_effort} if type_effort else {})}

    # Build review_history: carry forward from source task, appending current feedback if spawning a fix
    prev_review_history = source_loop.get("review_history", [])
    new_review_entry = {"iteration": iteration - 1, "feedback": current_feedback}
    if current_issues:
        new_review_entry["issues"] = current_issues
    new_review_history = (
        [*prev_review_history, new_review_entry]
        if task_type == "fix" and current_feedback
        else prev_review_history
    )

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
                1 for desc in curr_critical_major
                if desc in prev_critical_major
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
                    stuck_detail = f"feedback similarity {similarity:.0%} ≥ {text_threshold:.0%}"

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
                        from app.notification_dispatch import dispatch_external_notification
                        _asyncio.ensure_future(dispatch_external_notification(
                            stuck_orig.project_id,
                            "APPROVAL_NEEDED",
                            {
                                "task_id": str(stuck_orig.id),
                                "task_title": stuck_orig.title or str(stuck_orig.id)[:8],
                                "pr_number": pr_number,
                                "iteration": iteration,
                                "reason": "Review-fix cycle stuck — manual review required",
                            },
                        ))
                    except Exception as _ne:
                        logger.warning(f"[NOTIFY] APPROVAL_NEEDED dispatch setup failed: {_ne}")
            except Exception as e:
                logger.warning(
                    f"[PIPELINE] Failed to mark stuck cycle on {resolved_original_id}: {e}",
                    exc_info=True,
                )
            return (None, False)

        # Issue-churn detection: complements stuck-cycle above. If total
        # CRITICAL+MAJOR count fails to decrease across the last
        # ISSUE_CHURN_WINDOW iterations (even though the specific issues are
        # rotating and so evading Jaccard stuck detection), reduce the
        # remaining iteration cap by ~1/3 and instruct the fix agent to
        # narrow focus. The stuck-cycle detector handles harsher intervention.
        is_churn, churn_counts = _detect_issue_churn(new_review_history)
        if is_churn and task_type == "fix":
            remaining = max(per_chain_cap - iteration, 0)
            reduced_cap = iteration + (remaining * 2 // 3)
            if reduced_cap < per_chain_cap:
                logger.warning(
                    f"[PIPELINE] Issue churn detected for PR #{pr_number}: "
                    f"CRITICAL+MAJOR counts {churn_counts} non-decreasing over "
                    f"{ISSUE_CHURN_WINDOW} iterations. Reducing cap "
                    f"{per_chain_cap} → {reduced_cap}; injecting narrow-focus directive."
                )
                per_chain_cap = reduced_cap
            else:
                logger.warning(
                    f"[PIPELINE] Issue churn detected for PR #{pr_number}: "
                    f"counts {churn_counts}; injecting narrow-focus directive."
                )
            narrow_prefix = _CHURN_NARROW_FOCUS_PREFIX.format(
                window=ISSUE_CHURN_WINDOW, counts=churn_counts
            )
            narrow_prefix += _format_repeating_issues_section(new_review_history)
            payload_data["prompt"] = narrow_prefix + (payload_data.get("prompt") or "")
            fix_prompt = narrow_prefix + (fix_prompt or "")
        elif task_type == "fix" and per_chain_cap > 0 and (
            iteration >= MID_BUDGET_NARROW_FOCUS_THRESHOLD * per_chain_cap
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

    payload_data = validate_internal_payload(payload_data, f"review_pipeline:{task_type}:pr#{pr_number}")
    source_ctx = source_task.context or {}
    propagated_scope = source_ctx.get('scope', source_loop.get('scope', 'medium'))
    # Carry strategic context (direction_id, related_files, parent_context, scope, …)
    # from source task so fix/review agents see the same direction/intent the create
    # task had. Skip underscore-prefixed internal flags and override review_loop below.
    propagated_ctx = {
        k: v for k, v in source_ctx.items()
        if not k.startswith('_') and k != 'review_loop'
    }
    propagated_ctx['scope'] = propagated_scope
    # Carry forward the create task's self-review outcome so the review agent
    # can see which files were verified (truncated_files_read) and whether the
    # self-review was marked incomplete, and focus attention on blind spots
    # rather than re-reviewing files the self-review already checked.
    prev_self_review_outcome = source_ctx.get('_self_review_outcome')
    if prev_self_review_outcome:
        propagated_ctx['_self_review_outcome'] = prev_self_review_outcome
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
        propagated_ctx['scope_budget_utilization'] = utilization
    # Propagate the original create task's planned file list so the reviewer can
    # objectively detect scope creep (files changed but not in the original plan).
    planned_files = (orig.context or {}).get("related_files") if orig else None
    if not planned_files:
        planned_files = source_ctx.get("related_files")
    if planned_files:
        propagated_ctx['_planned_files'] = list(planned_files)
    propagated_ctx['review_loop'] = {
        "pr_number": pr_number, "branch": branch,
        "base_branch": base_branch,
        "original_task_id": resolved_original_id,
        "iteration": iteration,
        "max_iterations": per_chain_cap,
        **({"review_history": new_review_history} if new_review_history else {}),
        **({"fix_reason": fix_reason} if fix_reason else {}),
    }
    new_task = Task(
        project_id=source_task.project_id,
        title=title,
        description=fix_prompt or f"Automated {task_type} for PR #{pr_number}",
        parent_task_id=parent_id,
        priority=inherited_priority,
        payloads=[payload_data],
        context=propagated_ctx,
        vm_instance_id=vm_name,
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    await session.commit()
    await session.refresh(new_task)

    history = TaskHistory(task_id=new_task.id, action="SPAWNED",
                          details={"spawned_by": "REVIEW_PIPELINE", "type": task_type, "pr_number": pr_number})
    session.add(history)
    await session.commit()

    await broadcast_to_project(str(source_task.project_id), "task_updated",
                               {"task_id": str(new_task.id), "status": TaskStatus.QUEUED})
    return (new_task, True)
