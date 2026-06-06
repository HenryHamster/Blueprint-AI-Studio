"""Agent task status update endpoint.

Extracted from app.main to reduce main.py size.  Contains the POST /api/agent/status
endpoint (update_agent_status) and its supporting helpers: record_task_result and
_recorded_task_ids.

All deferred imports from app.main are intentional — they avoid circular imports
(agent_status is imported BY main) and preserve test-patchability.
"""

import asyncio
import contextlib
import logging
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.exc import IntegrityError, InterfaceError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select
from app.database import get_session, resolve_async_engine
from app.branch_resolution import classify_rollup_merge_outcome
from app.landed_signal import apply_merged_landed_signal
from app.models import (
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskStatus,
    bank_task_current_attempt,
    get_task_total_vm_cost_usd,
    with_task_attempt_banking,
)
from app.schemas import AgentStatusPayload
from app.shared_state import limiter
from app.shared_state import (
    get_project_integration_branch as _get_project_integration_branch,
)
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config
import app.orchestrator as _orch
from app.broadcast import broadcast_to_project
from app.routers.agent import verify_agent_token
from app.sr_blind_spots import filter_disallowed_blind_spots
from app.max_plan_pool import (
    _max_plan_pool_lock,
    apply_task_llm_runtime_context,
    release_max_plan_account,
    _account_available_for_model,
    _cooldown_seconds_for_info,
    _max_plan_pools,
    _extract_task_model,
    clear_rate_limit_state_on_success,
    clear_regression_on_success,
    db_account_was_rate_limited,
    db_pool_wide_rate_limit_cooldown,
    disable_account_for_auth,
    disable_account_model,
    get_task_llm_account_id,
    get_task_llm_auth_mode,
    get_task_llm_execution_mode,
    get_task_rate_limit_scope,
    get_task_llm_provider,
    record_completion_event,
    record_task_outcome,
)
from app.task_lifecycle import (
    _process_post_commit,
    PostCommitFinalCommitFailed,
    PostCommitLocked,
)
from app.rollup_management import _check_and_trigger_rollup
from app.payload_schemas import validate_internal_payload as _validate_internal_payload
from app.task_completion import _compute_first_attempt_succeeded
from app.landed_signal import merge_verification_status as _merge_verification_status
from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE
from app.time_utils import _utcnow, set_retry_after
from app.agent_status_spawn import (
    _recover_poisoned_session,
    maybe_spawn_smoketest_fix,
    maybe_spawn_debug_create,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_recorded_task_ids: set = set()  # Track which task IDs have already recorded results
_AGENT_STATUS_TERMINAL_STATUSES = (
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.BLOCKED,
)


def record_task_result(
    success: bool,
    failure_class: str = "",
    task_id: str = "",
    task_type: str = "",
    task_context: dict | None = None,
    auto_remediation: bool = False,
):
    """Record a task result for deployment monitoring. Deduplicates by task_id.

    When ``auto_remediation`` is True (or task_context legacy fallback says so),
    the recorded task_type is overridden to ``auto_remediation`` so these tasks
    don't pollute the per-type failure-rate gate on their underlying type
    (typically `create`). Auto-remediation tasks have a fundamentally
    different risk profile — they're spawned to fix known-broken code on
    main, so their failure rate is expected to run higher than fresh
    feature work. Bundling them into the generic `create` bucket
    cascades those failures into a gate that blocks normal create tasks.

    Audit W1: ``auto_remediation`` is now a Task column; callers should
    pass ``auto_remediation=task.auto_remediation`` directly. The
    ``task_context`` fallback is retained for callsites that haven't yet
    been threaded through with a Task object in scope.
    """
    from app.shared_state import deploy_monitoring

    # Deduplicate: only record one result per task
    if task_id:
        if task_id in _recorded_task_ids:
            return
        _recorded_task_ids.add(task_id)
        # Keep the set bounded
        if len(_recorded_task_ids) > 200:
            _recorded_task_ids.clear()

    # Override the bucket when the task was auto-spawned remediation.
    if auto_remediation or (task_context and task_context.get("auto_remediation")):
        task_type = "auto_remediation"

    entry = (
        _utcnow().isoformat(),
        "success" if success else "failure",
        failure_class,
        task_type,
    )
    deploy_monitoring["task_results"].append(
        entry
    )  # deque(maxlen=100) handles cap automatically

    if success:
        _shared_state.deploy_monitoring["last_completion_time"] = _utcnow()


async def _check_daily_cost_ceiling(task: Task, session: AsyncSession) -> None:
    """Create a WARNING alert if the daily VM cost total exceeds the configured ceiling.

    Runs at most once per task completion. The alert is deduplicated — only one
    undismissed 'Daily cost ceiling exceeded' alert per project is created.
    """
    ceiling = float(_runtime_config.get("daily_cost_ceiling_usd", 0))
    if ceiling <= 0:
        return

    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    # Exclude current task from DB query to avoid double-counting (its vm_stopped_at
    # was just set in-memory and may be flushed before the query runs).
    rows = (
        await session.execute(
            select(Task.vm_provisioned_at, Task.vm_stopped_at, Task.vm_spot).where(
                Task.project_id == task.project_id,
                Task.id != task.id,
                Task.vm_stopped_at >= today_start,
                Task.vm_provisioned_at.is_not(None),
            )
        )
    ).all()

    daily_total = 0.0
    for prov_at, stop_at, is_spot in rows:
        if prov_at and stop_at:
            secs = (stop_at - prov_at).total_seconds()
            rate = SPOT_RATE if is_spot else ON_DEMAND_RATE
            daily_total += secs * rate

    # Always include the current task's cost explicitly (it is in-memory, not yet committed).
    if (
        task.vm_provisioned_at
        and task.vm_stopped_at
        and task.vm_stopped_at >= today_start
    ):
        secs = (task.vm_stopped_at - task.vm_provisioned_at).total_seconds()
        daily_total += secs * (SPOT_RATE if task.vm_spot else ON_DEMAND_RATE)

    vm_total = daily_total

    # Off-VM compute jobs accrue their own durable spend ledger. Fold today's
    # settled (terminal, costed) compute-job spend into the daily total so the
    # ceiling reflects total project spend, not just agent-VM runtime.
    from app.compute_jobs import get_recent_terminal_compute_spend_usd

    compute_total = await get_recent_terminal_compute_spend_usd(
        session, project_id=task.project_id, since=today_start
    )
    daily_total = vm_total + compute_total

    if daily_total <= ceiling:
        return

    alert_title = "Daily cost ceiling exceeded"
    existing = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == task.project_id,
                    Alert.task_id.is_(None),
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.dismissed_at.is_(None),
                    Alert.title == alert_title,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return

    alert = Alert(
        project_id=task.project_id,
        severity=AlertSeverity.WARNING,
        title=alert_title,
        message=(
            f"Daily total spend ${daily_total:.2f} exceeds ceiling ${ceiling:.2f} "
            f"(VM ${vm_total:.2f} + compute ${compute_total:.2f})."
        ),
    )
    session.add(alert)
    await broadcast_to_project(
        str(task.project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "WARNING", "title": alert_title},
    )
    logger.warning(
        f"[COST] Daily cost ceiling exceeded for project {task.project_id}: "
        f"total ${daily_total:.2f} > ${ceiling:.2f} "
        f"(VM ${vm_total:.2f} + compute ${compute_total:.2f})"
    )
    try:
        from app.notification_dispatch import dispatch_external_notification
        from app.failure_classification import classify_failure

        ctx = task.context or {}
        first_payload = task.first_payload or {}
        direction_text = (
            ctx.get("direction_text") or first_payload.get("direction_text") or ""
        )
        # Aggregate today's cost by failure class across this project's tasks.
        # Exclude current task (added explicitly below) to avoid double-counting via identity map.
        cost_rows = (
            (
                await session.execute(
                    select(Task)
                    .options(
                        load_only(
                            Task.vm_provisioned_at,
                            Task.vm_stopped_at,
                            Task.vm_spot,
                            Task.status,
                            Task.id,
                            Task.context,
                            Task.latest_status_message,
                        )
                    )
                    .where(
                        Task.project_id == task.project_id,
                        Task.id != task.id,
                        Task.vm_stopped_at >= today_start,
                        Task.vm_provisioned_at.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        cost_by_failure_class: dict[str, float] = {}
        for t in [*list(cost_rows), task]:
            if not (t.vm_provisioned_at and t.vm_stopped_at):
                continue
            secs = (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
            rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
            status_val = getattr(t.status, "value", None) or (
                t.status if isinstance(t.status, str) else "UNKNOWN"
            )
            bucket = (
                classify_failure(t, at_time=t.last_heartbeat or t.started_at)
                if status_val == TaskStatus.FAILED.value
                else status_val
            )
            cost_by_failure_class[bucket] = round(
                cost_by_failure_class.get(bucket, 0.0) + secs * rate, 4
            )
        _budget_future = asyncio.ensure_future(
            dispatch_external_notification(
                task.project_id,
                "BUDGET_EXCEEDED",
                {
                    "task_id": str(task.id),
                    "daily_total_usd": round(daily_total, 2),
                    "daily_vm_cost_usd": round(vm_total, 2),
                    "daily_compute_cost_usd": round(compute_total, 2),
                    "ceiling_usd": round(ceiling, 2),
                    "direction_text": direction_text[:200],
                    "cost_by_failure_class": cost_by_failure_class,
                },
            )
        )
        del _budget_future  # fire-and-forget; stored to satisfy RUF006
    except Exception as _ne:
        logger.warning(f"[NOTIFY] BUDGET_EXCEEDED dispatch setup failed: {_ne}")


_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "was",
        "are",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "not",
        "no",
        "nor",
        "so",
        "yet",
        "both",
        "either",
        "neither",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "than",
        "too",
        "very",
        "just",
        "task",
        "error",
        "failed",
        "failure",
        "retries",
        "exhausted",
        "attempt",
        "status",
        "message",
        "timeout",
        "unknown",
        "none",
        "null",
        "true",
        "false",
        "code",
        "line",
        "file",
        "test",
        "run",
        "running",
        "agent",
        "vm",
        # Words that recur in *templated* failure messages (e.g.
        # "Lint/build checks failed after max fix attempts. See logs above
        # for details.") and surface as noise in keyword-frequency auto
        # playbook entries — they describe the template, not the failure.
        "above",
        "after",
        "before",
        "details",
        "logs",
        "log",
        "max",
        "fix",
        "attempts",
        "build",
        "checks",
        "check",
        "see",
        "lint",
        "report",
        "session",
        "session_id",
        "init",
        "complete",
        "completed",
        "children",
        "child",
        "evaluation",
        "evaluating",
        "hours",
        "minutes",
    ]
)

_AUTO_PLAYBOOK_CAP = 5


async def _check_recurring_failures(task: Task, session: AsyncSession) -> None:
    """Check for recurring failure patterns and auto-create playbook entries.

    Queries the last 30 FAILED tasks for this project within the past 7 days,
    groups by failure_class, and creates a playbook entry when 3+ tasks share
    common keywords in their status messages. Capped at 5 auto-generated entries
    per project.
    """
    import hashlib as _hashlib
    from collections import Counter
    from sqlalchemy.orm.attributes import flag_modified
    from app.failure_classification import classify_failure

    cutoff = _utcnow() - timedelta(days=7)

    recent_failed = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == task.project_id,
                    Task.id
                    != task.id,  # exclude current task (session-commit-exclusion-pattern)
                    Task.status == TaskStatus.FAILED,
                    Task.created_at >= cutoff,
                )
                .order_by(Task.created_at.desc())
                .limit(30)
            )
        )
        .scalars()
        .all()
    )

    # Add the current task's contribution manually (it was excluded above)
    all_failed = [*list(recent_failed), task]

    # Group by failure_class
    by_class: dict[str, list] = {}
    for t in all_failed:
        fc = classify_failure(t, at_time=t.last_heartbeat or t.started_at)
        by_class.setdefault(fc, []).append(t)

    for failure_class, tasks_in_class in by_class.items():
        if len(tasks_in_class) < 3:
            continue

        # Tokenize status messages and find keywords appearing in 3+ tasks
        task_keyword_sets: list[set[str]] = []
        for t in tasks_in_class:
            msg = (t.latest_status_message or "").lower()
            tokens = set()
            for word in msg.split():
                word = word.strip(".,;:!?\"'()[]{}|/\\-")
                if len(word) >= 4 and word not in _STOPWORDS and word.isalpha():
                    tokens.add(word)
            task_keyword_sets.append(tokens)

        keyword_counts = Counter(kw for kw_set in task_keyword_sets for kw in kw_set)
        common_keywords = sorted(
            kw for kw, count in keyword_counts.items() if count >= 3
        )[:10]

        if len(common_keywords) < 2:
            continue

        # Acquire project row lock before the design_doc read-modify-write.
        # skip_locked=True: best-effort, contention degrades gracefully.
        from app.task_locking import project_lock

        async with project_lock(session, task.project_id, skip_locked=True) as project:
            if project is None:
                continue

            doc = dict(project.design_doc or {})
            existing_auto_keys = [k for k in doc if k.startswith("playbook/auto-")]

            # Infer scope: direction-scoped only when ALL tasks have a direction_id
            # and they all share the same one.  Tasks without a direction_id must NOT
            # be silently dropped — their presence means the pattern is universal.
            all_have_direction = all(
                (t.context or {}).get("direction_id") for t in tasks_in_class
            )
            task_direction_ids = {
                (t.context or {}).get("direction_id")
                for t in tasks_in_class
                if (t.context or {}).get("direction_id")
            }
            if all_have_direction and len(task_direction_ids) == 1:
                inferred_scope = "direction"
                inferred_direction: str | None = next(iter(task_direction_ids))
            else:
                inferred_scope = "universal"
                inferred_direction = None

            # Check duplicate: look for any existing entry whose keywords overlap significantly
            already_covered = False
            keyword_set = set(common_keywords)
            for existing_key in existing_auto_keys:
                entry = doc.get(existing_key, {})
                entry_content = entry.get("content", "").lower()
                overlap = sum(1 for kw in keyword_set if kw in entry_content)
                if overlap >= max(2, len(keyword_set) // 2):
                    # Update the count in the existing entry
                    updated_entry = dict(entry)
                    updated_entry["content"] = (
                        f"WARNING: {failure_class} failures have occurred {len(tasks_in_class)} times recently. "
                        f"Common pattern: {', '.join(common_keywords)}. "
                        f"Recent examples: {_truncate_examples(tasks_in_class[-2:])}. "
                        f"Agents should check for this issue before starting work."
                    )
                    updated_entry["updated_at"] = _utcnow().isoformat()
                    updated_entry["applicable_types"] = _get_applicable_types(
                        tasks_in_class
                    )
                    updated_entry["scope"] = inferred_scope
                    updated_entry["learned_from_direction"] = inferred_direction
                    doc[existing_key] = updated_entry
                    project.design_doc = doc
                    flag_modified(project, "design_doc")
                    session.add(project)
                    logger.info(
                        f"[PLAYBOOK] Updated existing auto-entry {existing_key} for {failure_class} pattern"
                    )
                    already_covered = True
                    break

            if already_covered:
                continue

            # Cap at 5 auto-generated entries
            if len(existing_auto_keys) >= _AUTO_PLAYBOOK_CAP:
                logger.info(
                    f"[PLAYBOOK] Auto-playbook cap ({_AUTO_PLAYBOOK_CAP}) reached for project {task.project_id}, skipping"
                )
                continue

            # Create a new auto-entry
            short_hash = _hashlib.md5(
                ",".join(sorted(common_keywords)).encode()
            ).hexdigest()[:6]
            entry_key = f"playbook/auto-{failure_class.lower()}-{short_hash}"

            applicable_types = _get_applicable_types(tasks_in_class)
            content = (
                f"WARNING: {failure_class} failures have occurred {len(tasks_in_class)} times recently. "
                f"Common pattern: {', '.join(common_keywords)}. "
                f"Recent examples: {_truncate_examples(tasks_in_class[-2:])}. "
                f"Agents should check for this issue before starting work."
            )

            doc[entry_key] = {
                "content": content,
                "source_task_id": None,
                "updated_at": _utcnow().isoformat(),
                "confirmed_count": 0,
                "applicable_types": applicable_types,
                "scope": inferred_scope,
                "learned_from_direction": inferred_direction,
            }
            project.design_doc = doc
            flag_modified(project, "design_doc")
            session.add(project)
            logger.info(
                f"[PLAYBOOK] Auto-created entry {entry_key} for {failure_class} pattern "
                f"({len(tasks_in_class)} occurrences, keywords: {common_keywords})"
            )


def _truncate_examples(tasks: list) -> str:
    """Build a short string of recent failure messages for playbook content."""
    parts = []
    for t in tasks:
        msg = (t.latest_status_message or "")[:150].strip()
        if msg:
            parts.append(f'"{msg}"')
    return "; ".join(parts) if parts else "(no message)"


def _get_applicable_types(tasks: list) -> list[str]:
    """Extract unique task types from a list of tasks."""
    types = set()
    for t in tasks:
        fp = t.first_payload
        if fp:
            task_type = fp.get("type", "")
            if task_type:
                types.add(task_type)
    return sorted(types) if types else []


async def _persist_post_commit_pending(factory, task_id, reason: str, payload_dump):
    """Set the post_commit_pending column + retry metadata on a fresh session.

    The request's original session is often unusable here — either the
    transaction was poisoned by a LockNotAvailableError or the row is still
    held by the peer that triggered this failure. A new session with
    skip_locked lets the marker land as soon as the peer releases, without
    blocking on Postgres' lock_timeout.

    Audit W1 (A2/A5): the pending flag is now a real column
    (``Task.post_commit_pending``) so the idempotency guard at
    ``update_agent_status`` reads a DB-fresh value instead of a stale
    in-memory copy. The retry metadata (reason / payload / attempts) lives
    in context['_post_commit_pending_meta'] for diagnostics; nothing reads
    it for control flow.
    """
    last_exc = None
    for attempt in range(5):
        try:
            async with factory() as s:
                locked = (
                    (
                        await s.execute(
                            select(Task)
                            .where(Task.id == task_id)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .first()
                )
                if locked is None:
                    # Row exists but held by peer; short sleep and retry.
                    exists = (
                        await s.execute(select(Task.id).where(Task.id == task_id))
                    ).first()
                    if exists is None:
                        return
                    await asyncio.sleep(1 + attempt)
                    continue
                locked.post_commit_pending = True
                prior_meta = (locked.context or {}).get(
                    "_post_commit_pending_meta"
                ) or {}
                locked.context = {
                    **(locked.context or {}),
                    "_post_commit_pending_meta": {
                        "at": _utcnow().isoformat(),
                        "reason": reason,
                        "payload": payload_dump,
                        "attempts": int(prior_meta.get("attempts", 0)) + 1,
                    },
                }
                flag_modified(locked, "context")
                s.add(locked)
                await s.commit()
                return
        except Exception as e:
            last_exc = e
            await asyncio.sleep(1 + attempt)
    if last_exc is not None:
        raise last_exc


async def _clear_post_commit_pending(factory, task_id) -> None:
    """Clear the write-ahead post_commit_pending marker after a clean run.

    Paired with the write-ahead set in ``update_agent_status``: the marker is
    persisted *before* ``_process_post_commit`` runs, so a mid-flight instance
    kill (Cloud Run scale-in / "user disabled instance" SIGKILL) that bypasses
    the except handler still leaves a retry marker for
    ``_sweep_post_commit_pending``. On the happy path we clear it here.

    Best-effort: a clear failure is self-healing because the sweep age-gates
    ``reason="inline_running"`` markers and its ``_process_post_commit`` retry
    is idempotent — the worst case is one redundant post-commit pass.
    """
    for attempt in range(3):
        try:
            async with factory() as s:
                locked = (
                    (
                        await s.execute(
                            select(Task)
                            .where(Task.id == task_id)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .first()
                )
                if locked is None:
                    exists = (
                        await s.execute(select(Task.id).where(Task.id == task_id))
                    ).first()
                    if exists is None:
                        return
                    await asyncio.sleep(1 + attempt)
                    continue
                if (
                    not locked.post_commit_pending
                    and "_post_commit_pending_meta" not in (locked.context or {})
                ):
                    return  # already clear
                locked.post_commit_pending = False
                new_ctx = dict(locked.context or {})
                new_ctx.pop("_post_commit_pending_meta", None)
                locked.context = new_ctx
                flag_modified(locked, "context")
                s.add(locked)
                await s.commit()
                return
        except Exception as e:
            logger.warning(
                f"[PIPELINE] Failed to clear post_commit_pending for task "
                f"{task_id} (attempt {attempt + 1}/3): {e}"
            )
            await asyncio.sleep(1 + attempt)


async def _propagate_pipeline_failure_to_original(
    task: Task, session: AsyncSession
) -> None:
    """When a review/fix task fails permanently, cancel its original create task
    (so rollup excludes it) and, if the original was an auto-remediation task,
    propagate the failure to the branch's auto-fix escalation counter."""
    task_payload = task.first_payload
    task_type = task_payload.get("type", "")
    if task_type not in ("review", "fix"):
        return
    original_task_id = (task.context or {}).get("review_loop", {}).get(
        "original_task_id", ""
    ) or task_payload.get("original_task_id", "")
    if not original_task_id:
        return
    try:
        orig = await session.get(Task, uuid.UUID(original_task_id))
        if not (orig and orig.status == TaskStatus.COMPLETED):
            return
        orig.status = TaskStatus.CANCELLED
        orig.latest_status_message = (
            f"Pipeline {task_type} task failed — PR excluded from rollup"
        )
        # C6 rework (B): write-ahead a rollup-nudge recovery marker on the
        # original child BEFORE the terminal commit. The CANCELLED status is
        # persisted by the commit below regardless of whether the rollup nudge
        # that follows ever runs; a death (or session poison) between this
        # commit and ``_check_and_trigger_rollup`` would otherwise DROP the
        # rollup trigger and strand the rollup parent in EVALUATING (reaper
        # Step 3 only nudges on a COMPLETED child — a parent whose last
        # not-yet-rolled child is this CANCELLED one is invisible to it).
        # The reaper Step-3 sweep consumes ``_rollup_nudge_pending`` and
        # re-runs ``_check_and_trigger_rollup`` for the carrying child. Cleared
        # ONLY after the nudge below succeeds (mirrors the _pr_state_check_pending
        # producer/consumer discipline used elsewhere in this cluster).
        _orig_ctx = dict(orig.context or {})
        _orig_ctx["_rollup_nudge_pending"] = True
        orig.context = _orig_ctx
        flag_modified(orig, "context")
        session.add(orig)
        await session.commit()
        if orig.parent_task_id:
            await _check_and_trigger_rollup(orig, session)
            # Nudge succeeded — drop the write-ahead recovery marker. (A
            # leftover marker is self-healing: the reaper sweep would just
            # re-run an idempotent rollup check, but clearing it keeps the
            # candidate set small.)
            _clear_ctx = dict(orig.context or {})
            if _clear_ctx.pop("_rollup_nudge_pending", None) is not None:
                orig.context = _clear_ctx
                flag_modified(orig, "context")
                session.add(orig)
                await session.commit()
        logger.error(
            f"[PIPELINE] Cancelled original task {original_task_id} — {task_type} failed, skipping in rollup"
        )
        # Auto-remediation: review/fix cycle exhaustion means the remediation
        # attempt failed. The review task itself doesn't carry auto_remediation,
        # so propagate via the original create task. (Audit W1: column now.)
        if orig.auto_remediation:
            try:
                from app.notification_dispatch import post_remediation_result_message

                await post_remediation_result_message(
                    session,
                    orig,
                    result="failed",
                )
                await session.commit()
            except Exception as _ar_exc:
                logger.warning(
                    "[AUTO-REMEDIATION] Failed to post failure result after pipeline exhaustion: %s",
                    _ar_exc,
                )
                # WHY: post_remediation_result_message does session.add() and the
                # following session.commit() can raise on FK/serialization conflict,
                # leaving the session in pending-rollback state. Without this rollback
                # the subsequent early commit in update_agent_status (line ~1979)
                # surfaces as PendingRollbackError, the swallow point being invisible.
                with contextlib.suppress(Exception):
                    await session.rollback()
    except Exception as e:
        logger.warning(
            f"[PIPELINE] Failed to propagate failure to {original_task_id}: {e}",
            exc_info=True,
        )
        # WHY: session.commit() at line ~510 (and the rollup helper that may
        # call session.execute beneath it) can raise on serialization conflict
        # / FK violation / lock contention, leaving the session in
        # pending-rollback state. The handler then proceeds to the early commit
        # at line ~1979 of update_agent_status which raises PendingRollbackError
        # — production logs at 2026-05-04 19:46:43 UTC showed this exact pattern.
        with contextlib.suppress(Exception):
            await session.rollback()


@router.post("/api/agent/status")
@limiter.limit("120/minute")
async def update_agent_status(
    request: Request,
    payload: AgentStatusPayload,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """
    Endpoint for agents to report progress, save git artifacts, and update task status.
    """
    if str(payload.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    # 1. Fetch task — validate vm_instance_id to prevent stale agents from
    # overwriting status after a task was requeued with a new VM.
    # FOR UPDATE prevents race with reaper: without it, a stale agent's
    # COMPLETED can overwrite the reaper's QUEUED (requeue) between read and write.
    #
    # SKIP LOCKED design (audit E9): _process_post_commit re-acquires FOR UPDATE
    # on a separate session and holds it while making GitHub API calls (15-120s).
    # A duplicate status POST arriving in that window would block on a plain
    # FOR UPDATE, hit the 30s Postgres lock_timeout, and return 500 — Cloud Run
    # then classifies the 5xx as ContainerError and quarantines the instance.
    # skip_locked + 429 retry is the correct trade-off here: the agent's apiPost
    # already treats 429 as retry-able with backoff. The verification report
    # (2026-04-26) flagged this as PARTIAL because there's no blocking-lock
    # alternative — but that alternative is structurally worse on Cloud Run.
    # Mitigations in this design:
    #   - terminal-state short-circuit (no pointless retries on COMPLETED tasks)
    #   - structured 429 with Retry-After (5s) for contention
    #   - the lock holder (post-commit) commits before its long GitHub calls
    #     (early-commit pattern at line ~1635), so the contention window is
    #     bounded to the few-ms early-commit phase, not the full post-commit
    stmt = (
        select(Task)
        .where(
            Task.id == payload.task_id,
            Task.vm_instance_id == token.get("vm_instance_id"),
        )
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(stmt)
    task = result.scalars().first()

    if not task:
        # Distinguish "row locked" from "row missing / wrong VM" with a
        # non-locking read, mirroring the heartbeat endpoint (agent.py:1077).
        check_stmt = select(Task).where(Task.id == payload.task_id)
        check_task = (await session.execute(check_stmt)).scalars().first()
        if not check_task:
            raise HTTPException(status_code=404, detail="Task not found")
        if check_task.vm_instance_id != token.get("vm_instance_id"):
            raise HTTPException(status_code=404, detail="Task not found for this VM")
        # Row exists but is locked. If the task is already terminal, the
        # in-flight lock holder is post-commit processing for a prior POST —
        # this retry would just hit the idempotency guard, so short-circuit
        # without waiting for the lock.
        if check_task.status in _AGENT_STATUS_TERMINAL_STATUSES:
            return {"status": "already_terminal", "task_id": str(check_task.id)}
        if check_task.status == TaskStatus.EVALUATING:
            return {"status": "already_evaluating", "task_id": str(check_task.id)}
        # Non-terminal + locked: tell the agent to retry. We use 429 instead
        # of 503 so Cloud Run does not classify these temporary-contention
        # responses as ContainerError (Cloud Run flags any 5xx as a container
        # fault, quarantining the instance). The agent's apiPost treats 429
        # as retry-able the same way it handles 5xx.
        # Audit E9: log at INFO so observability can see contention spikes
        # without polluting WARN/ERROR with normal retry traffic.
        logger.info(
            f"[AGENT_STATUS] Row locked for task {check_task.id} (status={check_task.status}); "
            f"returning 429 to agent for retry"
        )
        raise HTTPException(
            status_code=429,
            detail="Task row locked, retry",
            headers={"Retry-After": "5"},
        )

    # F11: this handler holds a row lock until the early commit below, but it
    # also performs several SELECTs while preparing artifacts/retry metadata.
    # Production AsyncSession defaults autoflush=True, so those SELECTs can
    # push partial task mutations before the explicit commit boundary. Disable
    # request-session autoflush; commit still flushes all pending work.
    session.sync_session.autoflush = False

    # Capture task.id as a plain string up front so the broad except clauses
    # below (and the post-commit early-commit handler at line ~1854) can log
    # without re-accessing `task.id` on a session that may already be in
    # pending-rollback state. Mirrors the pattern e15af067 introduced for the
    # post-commit logger.
    _task_id_str = str(task.id)

    # Idempotency guard: if the task is already in a terminal state, ignore duplicate
    # callbacks (e.g. agent HTTP retries).  Still return 200 so the agent doesn't retry.
    # Exception: if post_commit_pending is set, the early-commit succeeded but
    # _process_post_commit raised before spawning review/fix tasks. Let the retry
    # re-run post-commit processing so the PR doesn't get orphaned.
    # Audit W1 (A2/A5): post_commit_pending is now a column. We re-fetch it
    # via a sub-row SELECT inside this branch so a marker that was just
    # written by `_persist_post_commit_pending` on a fresh session is visible
    # even though the in-memory `task` object was loaded earlier in the
    # request and has stale context.
    if task.status in _AGENT_STATUS_TERMINAL_STATUSES:
        pending_row = (
            await session.execute(
                select(Task.post_commit_pending).where(Task.id == task.id)
            )
        ).first()
        is_pending = bool(pending_row and pending_row[0])
        if is_pending:
            logger.info(
                f"[PIPELINE] Retrying post-commit for terminal task {task.id} (post_commit_pending set)"
            )
            # Release the outer FOR UPDATE SKIP LOCKED row lock before calling
            # _process_post_commit: it opens a separate session and takes its
            # own FOR UPDATE (task_lifecycle.py:559) on the same row. Holding
            # the outer lock deadlocks the inner acquire until lock_timeout
            # (30s) → LockNotAvailableError → 500 → Cloud Run ContainerError.
            await session.commit()
            _engine = resolve_async_engine(session)
            _factory = async_sessionmaker(
                _engine, class_=AsyncSession, expire_on_commit=False
            )
            try:
                result = await _process_post_commit(
                    task.id, payload, _session_factory=_factory
                )
                await session.refresh(task)
                task.post_commit_pending = False
                session.add(task)
                await session.commit()
                return result
            except PostCommitLocked:
                # Pure row contention: a peer (the post-commit sweep at
                # recovery_sweeps.py:2095, or another instance's retry) holds
                # the task row and is running this same post-commit. The
                # write-ahead marker is still set (that's why we entered this
                # branch), so that peer — or the sweep — owns the retry.
                # PostCommitLocked is documented (task_lifecycle.py:149) as a
                # "retryable pending, not an error": surfacing it as a 5xx makes
                # Cloud Run flag a ContainerError and quarantine the instance,
                # exactly the failure mode the row-lock guard above (line ~891)
                # converts to a 429 for. Leave post_commit_pending set and
                # return a clean retry-pending instead of re-raising → 500.
                logger.info(
                    f"[PIPELINE] Post-commit retry for terminal task "
                    f"{_task_id_str} hit row contention (held by peer); "
                    f"leaving post_commit_pending set for sweep",
                )
                return {
                    "status": "post_commit_retry_pending",
                    "task_id": _task_id_str,
                }
            except PostCommitFinalCommitFailed as e:
                # C1: the retry re-ran post-commit but its FINAL commit failed
                # again — the deferred side effects are still unpersisted. Do
                # NOT clear post_commit_pending (leave the marker set so the
                # sweep keeps retrying) and do NOT 500 the agent (the terminal
                # status is already committed; the marker owns the retry).
                logger.warning(
                    f"[PIPELINE] Post-commit retry final-commit failed for "
                    f"{task.id}: {e} — leaving post_commit_pending set for sweep",
                )
                with contextlib.suppress(Exception):
                    await session.rollback()
                return {
                    "status": "post_commit_retry_pending",
                    "task_id": str(task.id),
                }
            except Exception as e:
                # A disconnection-class DB error on the retry path is deploy-cutover
                # collateral, not a code fault: the terminal status is already
                # committed and post_commit_pending is still set (we only clear it on
                # the clean-return branch above), so the sweep retains ownership of
                # the deferred side effects. Returning a clean 200 avoids a 500 →
                # Cloud Run ContainerError → instance quarantine during a cutover
                # (prod 2026-06-03). Genuine errors still surface as 500.
                if bool(getattr(e, "connection_invalidated", False)) or isinstance(
                    e, InterfaceError
                ):
                    logger.warning(
                        "[PIPELINE] Post-commit retry for %s hit a disconnection-class "
                        "DB error (instance-shutdown collateral); leaving "
                        "post_commit_pending set for the sweep",
                        _task_id_str,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()
                    return {
                        "status": "post_commit_retry_pending",
                        "task_id": _task_id_str,
                    }
                logger.error(
                    f"[PIPELINE] Post-commit retry also failed for {task.id}: {e}",
                    exc_info=True,
                )
                raise
        return {"status": "already_terminal", "task_id": str(task.id)}
    if task.status == TaskStatus.EVALUATING:
        return {"status": "already_evaluating", "task_id": str(task.id)}

    # 2. Update task status
    task.status = payload.status
    if payload.provider or payload.auth_mode or payload.model or payload.execution_mode:
        task.context = apply_task_llm_runtime_context(
            task.context,
            account_id=get_task_llm_account_id(task),
            provider=payload.provider or get_task_llm_provider(task),
            auth_mode=payload.auth_mode or get_task_llm_auth_mode(task),
            model=payload.model or (task.context or {}).get("llm_model"),
            execution_mode=payload.execution_mode or get_task_llm_execution_mode(task),
        )
        flag_modified(task, "context")
    payloads = list(task.payloads or [])
    if payloads:
        payload_updated = False
        if payload.model and not payloads[0].get("model"):
            payloads[0]["model"] = payload.model
            payload_updated = True
        if payload.execution_mode and not payloads[0].get("execution_mode"):
            payloads[0]["execution_mode"] = payload.execution_mode
            payload_updated = True
        if payload_updated:
            task.payloads = payloads
            flag_modified(task, "payloads")
    # Preserve logs as status message when:
    # - no artifact is created (e.g., "no changes" completion), OR
    # - task is failing (artifact-bearing failures must not silently lose diagnostic text)
    # Keep the LAST 8000 chars — errors (stack traces, failed assertions,
    # the actual diagnostic line like "No 'foo' export is defined on the
    # '…' mock") almost always land at the tail of test/build output.
    # Head-slicing (previous 2000-char cap) was clipping off the useful
    # part and leaving boilerplate ("RUN v2.1.9", setup noise).
    if payload.logs and (
        not payload.artifact_type or payload.status == TaskStatus.FAILED
    ):
        task.latest_status_message = payload.logs[-8000:]
    if payload.failure_category:
        from app.failure_classification import normalize_agent_failure_category

        _fc_at_time = task.last_heartbeat or task.started_at
        _fc_acct_id = get_task_llm_account_id(task)
        _fc_scope = get_task_rate_limit_scope(task)
        _db_rl = (
            await db_account_was_rate_limited(_fc_acct_id, at_time=_fc_at_time)
            if _fc_acct_id
            else await db_pool_wide_rate_limit_cooldown(
                at_time=_fc_at_time,
                providers=_fc_scope.get("providers"),
                model=_fc_scope.get("model"),
                user_id=_fc_scope.get("user_id"),
            )
        )
        normalized_failure_category = normalize_agent_failure_category(
            payload.failure_category,
            task,
            at_time=_fc_at_time,
            db_rl_override=_db_rl,
        )
        ctx = dict(task.context or {})
        ctx["failure_category"] = normalized_failure_category
        # Preserve the raw lowercase agent label (e.g. "incomplete",
        # "claude_code_failed") alongside the normalized class. Normalization
        # collapses every CODE_ERROR sub-flavour into a single string, but
        # retry-guidance routing in task_lifecycle.py needs the sub-category
        # to distinguish "agent printed a diff but never shipped" (incomplete)
        # from real build/test failures.
        ctx["agent_failure_category_raw"] = payload.failure_category.strip().lower()
        # Persist authoritative verdict so analytics paths that call
        # classify_failure() hours later short-circuit to this value and
        # cannot flip RATE_LIMIT→CODE_ERROR once the cooldown lifts.
        ctx["failure_class"] = normalized_failure_category
        task.context = ctx
    if payload.scope_gate_triggered:
        ctx = dict(task.context or {})
        ctx["scope_gate_triggered"] = payload.scope_gate_triggered
        task.context = ctx
        flag_modified(task, "context")
    if payload.no_op_reason:
        # Affirmative no-op completion (`./done --already-done`): the agent
        # verified the deliverable already exists on the base branch, so there
        # is no PR/diff. Persist the reason so operators (and the task detail
        # view) can see WHY a COMPLETED task has no PR — and so a no-PR
        # completion is distinguishable from a dropped/abandoned one. The
        # `logs` field already carries the same reason and lands in
        # latest_status_message via the no-artifact branch below.
        ctx = dict(task.context or {})
        ctx["no_op_reason"] = payload.no_op_reason[:2000]
        task.context = ctx
        flag_modified(task, "context")
    if payload.file_change_details:
        ctx = dict(task.context or {})
        ctx["_file_change_details"] = payload.file_change_details[:50]
        task.context = ctx
        flag_modified(task, "context")
    if payload.fix_loop_events:
        # Fold optional fix-loop telemetry events into Task.fix_loop_history.
        # Same loose-validation pattern as the heartbeat path: invalid entries
        # log + drop instead of 422'ing the whole status update.
        from app.fix_loop_telemetry import FixLoopEventIn, apply_fix_loop_events

        validated: list[FixLoopEventIn] = []
        for raw in payload.fix_loop_events:
            try:
                validated.append(FixLoopEventIn(**raw))
            except Exception as exc:  # noqa: PERF203 — per-item validation; each item may individually fail
                logger.info(
                    "[FIX_LOOP] dropping invalid event for task %s: %s",
                    payload.task_id,
                    exc,
                )
        if validated:
            apply_fix_loop_events(task, validated)
    # Persist prompt fingerprint on the terminal status path. Mirrors the
    # identical block in the heartbeat handler (agent.py) so tasks that emit
    # their fingerprint only on the final status call still get it recorded.
    if payload.prompt_fingerprint:
        if task.prompt_fingerprint != payload.prompt_fingerprint:
            task.prompt_fingerprint = payload.prompt_fingerprint
        if (task.context or {}).get("prompt_fingerprint") != payload.prompt_fingerprint:
            ctx = dict(task.context or {})
            ctx["prompt_fingerprint"] = payload.prompt_fingerprint
            task.context = ctx
    session.add(task)

    # 3. Create Artifact if provided (with duplicate prevention for PR artifacts).
    # Audit 2026-04-25 A14: pre-check is non-locking — two concurrent status POSTs
    # can both pass and INSERT. Migration 0050 added a partial unique index
    # (ux_artifact_task_pull_request) that makes the second INSERT raise
    # IntegrityError; we catch it and treat as the dedup-skip path.
    if payload.artifact_type:
        if payload.artifact_type == ArtifactType.PULL_REQUEST:
            _PR_URL_RE = r"^https://github\.com/[^/\s]+/[^/\s]+/pull/\d+$"
            if payload.pr_url and not re.match(_PR_URL_RE, payload.pr_url):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "pr_url must match https://github.com/<owner>/<repo>/pull/<number>; "
                        f"got: {payload.pr_url!r}"
                    ),
                )
            existing = (
                (
                    await session.execute(
                        select(Artifact).where(
                            Artifact.task_id == task.id,
                            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing:
                logger.info(
                    f"[STATUS] Task {task.id} already has PR artifact, skipping duplicate"
                )
            else:
                artifact = Artifact(
                    task_id=task.id,
                    artifact_type=payload.artifact_type,
                    commit_hash=payload.commit_hash,
                    repo_url=payload.repo_url,
                    pr_url=payload.pr_url,
                    storage_uri=payload.storage_uri,
                    logs=payload.logs,
                )
                session.add(artifact)
                try:
                    await session.flush()  # surface IntegrityError now, not at commit
                except IntegrityError as ie:
                    # Concurrent peer beat us to it — partial UNIQUE caught the race.
                    await session.rollback()
                    logger.info(
                        f"[STATUS] Task {task.id} PR artifact race lost (peer inserted first); "
                        f"continuing with peer's row: {ie}"
                    )
                    return {"status": "accepted_dedup_race"}
                # Store changed files and enriched retry info in task context
                if (
                    payload.changed_files
                    or payload.diff_summary
                    or payload.self_review_outcome
                    or payload.fix_agent_responses
                ):
                    ctx = dict(task.context or {})
                    if payload.changed_files:
                        ctx["_pr_changed_files"] = payload.changed_files[:50]
                        related = (
                            ctx.get("related_files")
                            or task.first_payload.get("related_files")
                            or []
                        )
                        if related:
                            ctx["expansion_ratio"] = round(
                                len(payload.changed_files) / len(related), 2
                            )
                    ctx["_pr_url"] = payload.pr_url or ""
                    if payload.diff_summary:
                        ctx["_pr_diff_summary"] = payload.diff_summary[:50]
                    if payload.self_review_outcome:
                        ctx["_self_review_outcome"] = filter_disallowed_blind_spots(
                            dict(payload.self_review_outcome)
                        )
                    if payload.test_failure_summary:
                        ctx["_test_failure_summary"] = payload.test_failure_summary[
                            :5000
                        ]
                    if payload.fix_agent_responses:
                        # _spawn_pipeline_task carries this forward to the next
                        # reviewer's review_loop.fix_agent_responses for verify-mode.
                        ctx["_fix_agent_responses"] = payload.fix_agent_responses
                    task.context = ctx
                    flag_modified(task, "context")
        else:
            artifact = Artifact(
                task_id=task.id,
                artifact_type=payload.artifact_type,
                commit_hash=payload.commit_hash,
                repo_url=payload.repo_url,
                pr_url=payload.pr_url,
                storage_uri=payload.storage_uri,
                logs=payload.logs,
            )
            session.add(artifact)
            # Persist enriched retry info for non-PR failures (e.g. test/lint failures)
            if (
                payload.changed_files
                or payload.diff_summary
                or payload.test_failure_summary
                or payload.analysis_plan
                or payload.fix_agent_responses
            ):
                ctx = dict(task.context or {})
                if payload.changed_files:
                    ctx["_pr_changed_files"] = payload.changed_files[:50]
                    related = (
                        ctx.get("related_files")
                        or task.first_payload.get("related_files")
                        or []
                    )
                    if related:
                        ctx["expansion_ratio"] = round(
                            len(payload.changed_files) / len(related), 2
                        )
                if payload.diff_summary:
                    ctx["_pr_diff_summary"] = payload.diff_summary[:50]
                if payload.test_failure_summary:
                    ctx["_test_failure_summary"] = payload.test_failure_summary[:5000]
                if payload.analysis_plan:
                    ctx["_analysis_plan"] = payload.analysis_plan[:5000]
                if payload.fix_agent_responses:
                    ctx["_fix_agent_responses"] = payload.fix_agent_responses
                task.context = ctx
                flag_modified(task, "context")

        # Broadcast artifact creation (e.g., new logs or screenshot available)
        await broadcast_to_project(
            str(task.project_id),
            "artifact_created",
            {
                "task_id": str(task.id),
                "artifact_type": payload.artifact_type,
                "logs": payload.logs,
                "storage_uri": payload.storage_uri,
            },
        )

    # 3b+3c. Smoketest / debug spawn blocks — extracted to agent_status_spawn.py
    await maybe_spawn_smoketest_fix(task, payload, session, _task_id_str)

    await maybe_spawn_debug_create(task, payload, session, _task_id_str)

    # 4. External VM Termination
    # If the task is finished or failed, immediately kill the VM to save costs
    from app.failure_classification import classify_failure
    from app.vm_lifecycle import _ensure_vm_deleted
    from app.alert_dispatch import _maybe_create_circuit_breaker_alert

    if payload.status in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
        # Record result for deployment monitoring and update Prometheus counters
        # Wrapped in try-except so slot release (below) always runs even if
        # classify_failure() or record_task_result() throw.
        try:
            _direction_id = str((task.context or {}).get("direction_id") or "")
            _project_id = str(task.project_id) if task.project_id else ""
            if payload.status == TaskStatus.COMPLETED:
                record_task_result(
                    success=True,
                    task_id=str(task.id),
                    task_type=task.first_payload.get("type", ""),
                    task_context=dict(task.context or {}),
                    auto_remediation=task.auto_remediation,
                )
                _shared_state.tasks_completed_total += 1
                if _direction_id:
                    _shared_state.record_direction_result(
                        _direction_id, success=True, project_id=_project_id
                    )
                # Probe success: clear cascade-scoped disabled_models entries.
                # No-op if the account wasn't probing anything for this model.
                acct_id_clear = get_task_llm_account_id(task)
                model_clear = _extract_task_model(task)
                if acct_id_clear and model_clear:
                    try:
                        await clear_rate_limit_state_on_success(
                            acct_id_clear, model_clear
                        )
                    except Exception as e:
                        logger.warning(
                            f"[STATUS] clear_rate_limit_state_on_success failed for {acct_id_clear}: {e}",
                            exc_info=True,
                        )
                    try:
                        _rr = await clear_regression_on_success(
                            acct_id_clear, model_clear
                        )
                        if _rr and task.project_id:
                            from app.notification_dispatch import (
                                post_account_model_regression_recovered_message,
                                dispatch_external_notification,
                            )

                            await post_account_model_regression_recovered_message(
                                session,
                                task.project_id,
                                account_id=_rr["account_id"],
                                account_label=_rr["account_label"],
                                provider=_rr.get("provider") or "",
                                model=_rr["model"],
                                account_rate=_rr.get("account_rate"),
                                project_median=_rr.get("project_median"),
                                sample_size=_rr.get("sample_size"),
                            )
                            _regression_future = asyncio.ensure_future(
                                dispatch_external_notification(
                                    task.project_id,
                                    "account_model_regression_recovered",
                                    {
                                        k: _rr.get(k)
                                        for k in (
                                            "account_id",
                                            "account_label",
                                            "provider",
                                            "model",
                                        )
                                    },
                                )
                            )
                            del (
                                _regression_future
                            )  # fire-and-forget; stored to satisfy RUF006
                    except Exception as e:
                        logger.warning(
                            f"[STATUS] regression recovery failed for {acct_id_clear}: {e}",
                            exc_info=True,
                        )
                        # WHY: post_account_model_regression_recovered_message
                        # → post_event_message issues session.add(ProjectMessage).
                        # Any autoflush triggered along the way (e.g. by an
                        # implicit relationship load) can raise and poison the
                        # session, which then surfaces only at the early commit
                        # at ~1979 as an opaque PendingRollbackError.
                        with contextlib.suppress(Exception):
                            await session.rollback()
                if acct_id_clear and task.vm_provisioned_at:
                    try:
                        duration_secs = (
                            _utcnow() - task.vm_provisioned_at
                        ).total_seconds()
                        await record_completion_event(acct_id_clear, duration_secs)
                    except Exception as e:
                        logger.warning(
                            f"[STATUS] record_completion_event failed for {acct_id_clear}: {e}",
                            exc_info=True,
                        )
            else:
                from app.failure_classification import classify_failure_with_confidence

                classification = classify_failure_with_confidence(
                    task, payload.logs, at_time=task.last_heartbeat or task.started_at
                )
                failure_class = classification.failure_class

                # BLOCKED_PRECONDITION: the agent surfaced a "BLOCKER:" (via
                # ./blocked) — the work is wanted but a precondition is absent
                # (an upstream dependency/rollup, a base branch or referenced
                # file). Move the task into the first-class BLOCKED state instead
                # of re-dispatching it (which would burn the retry budget hitting
                # the same wall) or terminal-failing it (which would require a
                # manual requeue). Release the account slot and kill the VM, but
                # do NOT record a task/direction failure and do NOT increment
                # attempt_count — BLOCKED is not a failure. Recovery
                # (recheck/escalate -> QUEUED) is owned by the auto-unblock sweep.
                from app.failure_classification import BLOCKED_PRECONDITION

                if failure_class == BLOCKED_PRECONDITION:
                    from app.task_blocked import enter_blocked_state

                    _blk_acct_id = get_task_llm_account_id(task)
                    if _blk_acct_id and not (task.context or {}).get("_slot_released"):
                        await release_max_plan_account(_blk_acct_id)
                        task.context = {**(task.context or {}), "_slot_released": True}
                    enter_blocked_state(
                        task,
                        waiting_on=(task.context or {}).get("blocker_waiting_on")
                        or "precondition",
                        reason=task.latest_status_message or "Precondition not met",
                        source="agent",
                        need=(
                            payload.blocked_need.model_dump(exclude_none=True)
                            if payload.blocked_need
                            else None
                        ),
                    )
                    task.vm_stopped_at = _utcnow()
                    session.add(task)
                    await session.commit()
                    # Surface one deduplicated, operator-visible precondition
                    # blocker alert so a held task isn't silently parked.
                    try:
                        from app.alert_dispatch import (
                            _maybe_create_precondition_blocker_alert,
                        )

                        await _maybe_create_precondition_blocker_alert(
                            task,
                            session,
                            blocker_kind="precondition",
                            detail=task.latest_status_message or "Precondition not met",
                        )
                    except Exception as _blk_alert_err:
                        logger.warning(
                            f"[AGENT STATUS] precondition-blocker alert failed for "
                            f"{task.id}: {_blk_alert_err}"
                        )
                    await broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": TaskStatus.BLOCKED},
                    )
                    await _ensure_vm_deleted(
                        task.vm_instance_id, reason="blocked_precondition"
                    )
                    logger.info(
                        f"[AGENT STATUS] Task {task.id} BLOCKED — precondition not "
                        f"met; held without burning retry budget"
                    )
                    return {"status": "blocked", "task_id": str(task.id)}

                from app.failure_classification import (
                    EXTERNAL_API_SUBTYPE_PROVIDER_MODEL_CONFIG,
                    NO_PROGRESS_TIMEOUT,
                    persist_failure_category,
                )

                persist_failure_category(
                    task, failure_class, confidence=classification.confidence
                )
                record_task_result(
                    success=False,
                    failure_class=failure_class,
                    task_id=str(task.id),
                    task_type=task.first_payload.get("type", ""),
                    task_context=dict(task.context or {}),
                    auto_remediation=task.auto_remediation,
                )
                _shared_state.tasks_failed_total += 1
                if _direction_id:
                    _shared_state.record_direction_result(
                        _direction_id,
                        success=False,
                        failure_class=failure_class,
                        project_id=_project_id,
                    )

                # Rate-limited or transient API failures: requeue instead of permanently failing.
                # NO_PROGRESS_TIMEOUT is an inferred (not confirmed) transient provider
                # hang: requeue immediately and release the VM slot, but DO NOT block the
                # account's model (the disable_account_model / blacklist branches below are
                # gated on failure_class == "RATE_LIMIT", so a healthy account stays
                # available). Like RATE_LIMIT it does not burn the retry budget.
                if failure_class in ("RATE_LIMIT", "EXTERNAL_API", NO_PROGRESS_TIMEOUT):
                    acct_id = get_task_llm_account_id(task)
                    model = _extract_task_model(task)
                    if acct_id:
                        await release_max_plan_account(acct_id)
                        # Mark slot as released so the fallback release (line ~6232) doesn't double-release
                        task.context = {**(task.context or {}), "_slot_released": True}
                        if failure_class == "RATE_LIMIT" and model:
                            # Only disable a specific model when we know which one hit the limit.
                            # cascade_disabled_models() inside the helper handles tier cascading
                            # (Opus 429 → sonnet + haiku disabled too).
                            # classify_failure() wrote rate_limit_kind + rate_limit_retry_after_seconds
                            # into task.context; pass them through so quota-exhausted accounts get
                            # the flat 24h cooldown and server-provided retry hints are honored.
                            _rl_ctx = task.context or {}
                            await disable_account_model(
                                acct_id,
                                model,
                                retry_after_seconds=_rl_ctx.get(
                                    "rate_limit_retry_after_seconds"
                                ),
                                kind=_rl_ctx.get("rate_limit_kind") or "rate_limit",
                            )
                        # EXTERNAL_API: don't disable account (transient outage)

                    # Track API failure retries to prevent infinite loops
                    ctx = dict(task.context or {})
                    # Blacklist the account that just hit rate limit so retry picks a different one
                    acct_id_for_blacklist = get_task_llm_account_id(task)
                    if acct_id_for_blacklist and failure_class == "RATE_LIMIT":
                        blacklisted = list(ctx.get("blacklisted_accounts") or [])
                        if acct_id_for_blacklist not in blacklisted:
                            blacklisted.append(acct_id_for_blacklist)
                        ctx["blacklisted_accounts"] = blacklisted
                    should_requeue = True
                    if failure_class == "EXTERNAL_API":
                        external_api_subtype = ctx.get("external_api_subtype")
                        if (
                            external_api_subtype
                            == EXTERNAL_API_SUBTYPE_PROVIDER_MODEL_CONFIG
                        ):
                            provider_name = get_task_llm_provider(task)
                            current_model = model or _extract_task_model(task)
                            fallback_model = None
                            if provider_name and current_model:
                                try:
                                    from app.providers.registry import get_provider

                                    fallback_model = get_provider(
                                        provider_name
                                    ).fallback_model(current_model)
                                except Exception as e:
                                    logger.warning(
                                        "[AGENT STATUS] Could not resolve provider model fallback "
                                        "for task %s provider=%s model=%s: %s",
                                        task.id,
                                        provider_name,
                                        current_model,
                                        e,
                                        exc_info=True,
                                    )
                            if fallback_model and fallback_model != current_model:
                                ctx = apply_task_llm_runtime_context(
                                    ctx,
                                    account_id=None,
                                    provider=provider_name,
                                    auth_mode=get_task_llm_auth_mode(task),
                                    model=fallback_model,
                                    execution_mode=get_task_llm_execution_mode(task),
                                )
                                ctx["model_fallback"] = {
                                    "requested": current_model,
                                    "effective": fallback_model,
                                    "provider": provider_name,
                                    "reason": "provider_model_config_rejection",
                                    "at": _utcnow().isoformat(),
                                }
                                ctx.pop("_api_failure_retries", None)
                                task.retry_after_until = (
                                    None  # audit W1: column is source of truth
                                )
                                ctx.pop("blacklisted_accounts", None)
                                payloads = list(task.payloads or [])
                                if payloads and isinstance(payloads[0], dict):
                                    payloads[0] = dict(payloads[0])
                                    payloads[0]["model"] = fallback_model
                                    task.payloads = payloads
                                    flag_modified(task, "payloads")
                                logger.warning(
                                    "[AGENT STATUS] Task %s provider model/config rejection for "
                                    "%s/%s - requeuing with fallback model %s",
                                    task.id,
                                    provider_name,
                                    current_model,
                                    fallback_model,
                                )
                            else:
                                logger.warning(
                                    "[AGENT STATUS] Task %s provider model/config rejection for "
                                    "%s/%s - no fallback available; not requeuing same runtime",
                                    task.id,
                                    provider_name,
                                    current_model,
                                )
                                task.latest_status_message = "External API model/config failure - update provider model/settings and retry."
                                task.attempt_count = _runtime_config["max_retries"]
                                should_requeue = False
                        # Permanent auth failures (bad/expired credentials, org-disabled
                        # OAuth) should never be retried — they keep failing until the
                        # operator re-authenticates. Detect these by inspecting the failure
                        # message before burning retries.
                        elif any(
                            ind in (payload.logs or "").lower()
                            for ind in (
                                "token expired",
                                "token revoked",
                                "bad credentials",
                                "unauthorized",
                                "401",
                                "oauth_org_not_allowed",
                            )
                        ):
                            logger.warning(
                                f"[AGENT STATUS] Task {task.id} EXTERNAL_API permanent auth failure "
                                f"— not requeuing (operator must re-authenticate)"
                            )
                            task.latest_status_message = (
                                "External API auth failure — token expired or credentials invalid. "
                                "Re-authenticate and retry."
                            )
                            # Exhaust the general retry budget so the fallback retry path
                            # (attempt_count < max_retries) also skips this task.
                            task.attempt_count = _runtime_config["max_retries"]
                            should_requeue = False
                            # Hard-disable the offending account so NO further task selects
                            # it and burns VM hours on a credential that will only 4xx
                            # again. Non-recoverable: stays disabled until an operator
                            # re-authenticates and re-enables it. Uses its own DB session
                            # (won't poison this request's session).
                            auth_acct_id = get_task_llm_account_id(task)
                            if auth_acct_id:
                                try:
                                    newly_disabled = await disable_account_for_auth(
                                        auth_acct_id,
                                        "non-recoverable auth failure "
                                        "(oauth_org_not_allowed / invalid credentials)",
                                    )
                                except Exception as _e:
                                    newly_disabled = False
                                    logger.warning(
                                        f"[AGENT STATUS] Failed to auth-disable account "
                                        f"{auth_acct_id} for task {task.id}: {_e}"
                                    )
                                if newly_disabled:
                                    try:
                                        from app.notification_dispatch import (
                                            post_failure_alert_message,
                                        )

                                        await post_failure_alert_message(
                                            session,
                                            task.project_id,
                                            "LLM account auto-disabled after a "
                                            "non-recoverable auth failure "
                                            "(e.g. oauth_org_not_allowed). Re-authenticate / "
                                            "re-enable OAuth for Claude Code, then re-enable "
                                            "the account.",
                                            task.id,
                                        )
                                    except Exception as _e:
                                        logger.warning(
                                            f"[NOTIFY] Failed to post auth-disable alert: {_e}"
                                        )
                                        # post_event_message does session.add; an autoflush
                                        # could poison the session before the early commit.
                                        # Roll back defensively (same pattern as the circuit
                                        # breaker alert path).
                                        with contextlib.suppress(Exception):
                                            await session.rollback()
                        else:
                            # Transient infrastructure outage: retry with exponential backoff.
                            # Budget is 5 retries (up from 3) to survive longer outages.
                            api_retries = ctx.get("_api_failure_retries", 0) + 1
                            ctx["_api_failure_retries"] = api_retries
                            if api_retries > 5:
                                logger.warning(
                                    f"[AGENT STATUS] Task {task.id} hit {api_retries} EXTERNAL_API failures — not requeuing"
                                )
                                should_requeue = False
                            else:
                                delay = min(2**api_retries * 120, 1800)
                                set_retry_after(ctx, delay / 60)
                                logger.info(
                                    f"[AGENT STATUS] Task {task.id} EXTERNAL_API retry {api_retries} — backoff {delay}s"
                                )

                    if should_requeue:
                        old_vm_id_rl = task.vm_instance_id  # Save BEFORE reassignment
                        bank_task_current_attempt(task, stopped_at=_utcnow())
                        ctx = with_task_attempt_banking(task, ctx)
                        task.context = ctx
                        task.status = TaskStatus.QUEUED
                        # The earlier FAILED assignment fired the SQLAlchemy set event
                        # which added task.id to _terminated_task_ids, revoking the JWT.
                        # Since we're requeuing, drop the cache entry so the next
                        # provisioned VM can successfully claim.
                        from app.routers.agent import _terminated_task_ids

                        _terminated_task_ids.pop(str(task.id), None)
                        task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                        task.last_heartbeat = None
                        task.started_at = None
                        task.latest_status_message = (
                            f"{failure_class.replace('_', ' ').title()} — requeued"
                        )
                        # For rate-limited tasks: check if any account is still available.
                        # If all are exhausted, set retry_after based on earliest recovery
                        # to prevent rapid cycling through exhausted accounts.
                        if failure_class == "RATE_LIMIT":
                            model = _extract_task_model(task)
                            blacklisted = ctx.get("blacklisted_accounts") or []
                            # Check this user's accounts to see if any are available.
                            # Find which user pool the failed account belongs to.
                            any_available = False
                            earliest_recovery = None
                            async with _max_plan_pool_lock:
                                # Find user pool via the account that just failed
                                user_pool = []
                                if acct_id:
                                    for pool in _max_plan_pools.values():
                                        if any(a["id"] == acct_id for a in pool):
                                            user_pool = pool
                                            break
                                for a in user_pool:
                                    if a["id"] in blacklisted:
                                        continue
                                    if _account_available_for_model(a, model):
                                        any_available = True
                                        break
                                    # Track earliest model recovery across user's accounts
                                    dm = a.get("disabled_models", {})
                                    if model and model in dm:
                                        info = dm[model]
                                        disabled_at = info.get("at")
                                        if disabled_at:
                                            # Canonical cooldown — honors
                                            # quota_exhausted (flat 24h, e.g. a
                                            # weekly cap) and server retry hints,
                                            # instead of recomputing only the
                                            # 5-hour exponential. Without this a
                                            # weekly-capped task re-cycled every
                                            # ~4h against a still-exhausted account.
                                            cooldown_secs = _cooldown_seconds_for_info(
                                                info
                                            )
                                            recovery_dt = disabled_at + timedelta(
                                                seconds=cooldown_secs
                                            )
                                            if (
                                                earliest_recovery is None
                                                or recovery_dt < earliest_recovery
                                            ):
                                                earliest_recovery = recovery_dt
                            if any_available:
                                task.retry_after_until = (
                                    None  # audit W1: column source of truth
                                )
                            elif earliest_recovery:
                                minutes_until = (
                                    earliest_recovery - _utcnow()
                                ).total_seconds() / 60
                                set_retry_after(task, minutes_until)
                                ctx = dict(task.context or {})
                                task.latest_status_message = "Rate Limited — all accounts exhausted, waiting for recovery"
                            else:
                                set_retry_after(task, 5)
                                ctx = dict(task.context or {})
                                task.latest_status_message = "Rate Limited — all accounts exhausted, retrying in 5min"
                        elif failure_class != "EXTERNAL_API":
                            task.retry_after_until = (
                                None  # audit W1: column source of truth
                            )
                        # Preserve llm_* runtime identity keys (provider/model/auth_mode/
                        # execution_mode) for observability — debugging which provider failed
                        # and per-provider metrics need this. The blacklisted_accounts list
                        # already prevents the next attempt from re-picking the failed account.
                        ctx.pop("_slot_released", None)
                        # The task just ran on a VM successfully enough to POST a result —
                        # provisioning path worked. Don't carry a stale provision budget
                        # into the retry, or one more provision hiccup will fail it outright.
                        ctx.pop("provision_failures", None)
                        task.context = ctx
                        session.add(task)
                        await session.commit()
                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": TaskStatus.QUEUED},
                        )
                        logger.info(
                            f"[AGENT STATUS] Task {task.id} {failure_class} — requeued without burning retry"
                        )
                        await _ensure_vm_deleted(
                            old_vm_id_rl, reason="rate_limit_requeue"
                        )
                        return {"status": "accepted"}
        except Exception as e:
            logger.error(
                f"[STATUS] Exception during result recording for task {_task_id_str}: {e!r}",
                exc_info=True,
            )
            # If the swallowed exception left the session in pending-rollback
            # state — most likely the rate-limit-requeue commit at line ~1276
            # raising on a serialization conflict / deadlock — every subsequent
            # session.flush()/commit() in this handler raises PendingRollbackError.
            # 2026-05-03 production: 10x failures pointed at the FAILED-retry
            # commit at line ~1474 because of exactly this. Recover the session
            # here and re-apply the in-memory mutations from lines ~623-671 that
            # the retry path / terminal-failure annotation / early-commit all
            # depend on.
            if await _recover_poisoned_session(session, _task_id_str):
                task.status = payload.status
                if payload.logs and (
                    not payload.artifact_type or payload.status == TaskStatus.FAILED
                ):
                    task.latest_status_message = payload.logs[-8000:]
                if payload.failure_category:
                    from app.failure_classification import (
                        normalize_agent_failure_category,
                    )

                    _fc_at_time2 = task.last_heartbeat or task.started_at
                    _fc_acct_id2 = get_task_llm_account_id(task)
                    _fc_scope2 = get_task_rate_limit_scope(task)
                    _db_rl2 = (
                        await db_account_was_rate_limited(
                            _fc_acct_id2, at_time=_fc_at_time2
                        )
                        if _fc_acct_id2
                        else await db_pool_wide_rate_limit_cooldown(
                            at_time=_fc_at_time2,
                            providers=_fc_scope2.get("providers"),
                            model=_fc_scope2.get("model"),
                            user_id=_fc_scope2.get("user_id"),
                        )
                    )
                    ctx = dict(task.context or {})
                    ctx["failure_category"] = normalize_agent_failure_category(
                        payload.failure_category,
                        task,
                        at_time=_fc_at_time2,
                        db_rl_override=_db_rl2,
                    )
                    task.context = ctx
                    flag_modified(task, "context")
                session.add(task)

        # Release Max plan account slot (idempotent — skip if already released by reaper)
        # Runs regardless of whether result recording above succeeded or threw.
        # Flush immediately so _slot_released persists even if downstream processing fails.
        acct_id = get_task_llm_account_id(task)
        if acct_id and not (task.context or {}).get("_slot_released"):
            await release_max_plan_account(acct_id)
            task.context = {**(task.context or {}), "_slot_released": True}
            session.add(task)
            await session.flush()

        # Persist runtime LLM telemetry on Task for durable analytics.
        _ctx_terminal = dict(task.context or {})
        _provider = _ctx_terminal.get("llm_provider") or payload.provider
        _model = _ctx_terminal.get("llm_model") or payload.model
        task.provider = str(_provider) if _provider else None
        task.account_id = str(acct_id) if acct_id else None
        task.model = str(_model) if _model else None

        task.vm_stopped_at = _utcnow()
        _estimated_cost = get_task_total_vm_cost_usd(
            task, stopped_at=task.vm_stopped_at
        )
        if _estimated_cost > 0:
            ctx = dict(task.context or {})
            ctx["estimated_cost_usd"] = round(_estimated_cost, 4)
            task.context = ctx
            task.estimated_cost_usd = round(_estimated_cost, 4)
            await _check_daily_cost_ceiling(task, session)
        else:
            task.estimated_cost_usd = None
        old_vm_id = task.vm_instance_id
        # Fire-and-forget: awaiting _ensure_vm_deleted holds the FOR UPDATE
        # row lock through 60-90s of GCP I/O. Concurrent /api/agent/status
        # POSTs hit the held lock and 429, and Cloud Run instance termination
        # mid-block loses the COMPLETED commit (reaper then re-runs work that
        # already succeeded). Production incident 2026-05-05 task 04605f9d:
        # APPROVE verdict written, status update never committed, full
        # review re-executed on retry. The orchestrator-deleted marker is
        # recorded synchronously inside _ensure_vm_deleted before its first
        # await, so the reaper sees the orchestrator action immediately.
        from app.vm_lifecycle import fire_and_forget_vm_delete

        # Backlog reuse (efficiency direction d-91d8dede): a COMPLETED task's
        # agent re-enters its /next-role poll loop, so if backlog reuse is
        # enabled and the project has eligible QUEUED work this VM could run,
        # defer teardown for a bounded window so it can claim that work warm
        # instead of being torn down and cold-re-provisioned. FAILED agents
        # re-throw and exit without polling, so only COMPLETED is deferred (a
        # deferred FAILED VM would never poll and would just waste VM time).
        _teardown_deferred = False
        if (
            payload.status == TaskStatus.COMPLETED
            and old_vm_id
            and _shared_state.runtime_config.get("warm_backlog_reuse_enabled", False)
        ):
            try:
                from app.warm_backlog_reuse import has_eligible_backlog_task

                # Provider-aware teardown defer: a reused VM is locked to the
                # LLM provider/account/model frozen into its boot manifest (the
                # warm claim only mints a fresh JWT — it never re-materializes
                # credentials). The completing task ran on that boot identity, so
                # task.provider/account_id/model (set just above) ARE the VM's
                # boot identity. Pass it through so we only hold the VM for
                # backlog work it can actually run warm — a task pinned/recorded
                # to a different provider cold-dispatches instead. NO-OP for
                # single-provider projects (the dominant case): their queued
                # tasks carry no divergent constraint, so nothing is filtered.
                vm_boot = {
                    "provider": task.provider,
                    "account_id": task.account_id,
                    "model": task.model,
                }
                if await has_eligible_backlog_task(
                    session, task.project_id, old_vm_id, vm_boot=vm_boot
                ):
                    from app.vm_lifecycle import schedule_deferred_backlog_teardown
                    from app.warm_backlog_reuse import (
                        record_teardown_defer,
                        record_vm_with_eligible_backlog,
                    )

                    record_vm_with_eligible_backlog()
                    _shared_state.register_backlog_first_refusal(str(task.project_id))
                    schedule_deferred_backlog_teardown(old_vm_id)
                    record_teardown_defer()
                    _teardown_deferred = True
                    logger.info(
                        f"[BACKLOG-REUSE] Deferring teardown of VM {old_vm_id} "
                        f"(task {task.id} COMPLETED) — eligible backlog work in "
                        f"project {task.project_id}"
                    )
            except Exception:
                logger.exception(
                    "[BACKLOG-REUSE] Teardown-defer check failed — tearing down"
                )
                _teardown_deferred = False
        if not _teardown_deferred:
            fire_and_forget_vm_delete(old_vm_id, reason="agent_completion")

        # Backlog-reuse / warm-pool circuit breakers: if this FAILED task was
        # originally claimed via a warm path, record a failure event so the
        # matching circuit breaker can suppress that path if failures accumulate
        # (the classic "a bad image silently fails a stream of tasks" mode).
        if payload.status == TaskStatus.FAILED:
            from app.warm_pool import WARM_POOL_ORIGIN

            _timing_ctx = (task.context or {}).get("_timing", {})
            _claim_origin = _timing_ctx.get("warm_claim_origin")
            if _claim_origin == "backlog_reuse":
                _shared_state.record_backlog_reuse_failure("task_failed")
            elif _claim_origin == WARM_POOL_ORIGIN:
                # A warm-pool member served this task and it FAILED — feed the
                # pool-claim breaker so the maintainer stops replenishing (and
                # dispatch stops leasing) while a bad pool image is in rotation.
                # The pool dispatch path stamps WARM_POOL_ORIGIN ("warm_pool"),
                # NOT "pool" — compare against the canonical constant so this
                # feed cannot silently drift out of sync with the stamp site
                # (agent_warm_continuity._finalize_warm_claim, origin=WARM_POOL_ORIGIN).
                _shared_state.record_pool_claim_failure("task_failed")

        # Open-PR handoff: if a previous attempt already pushed a branch and
        # opened a PR, retrying from scratch will collide with the agent's
        # stale-branch guard (agent/src/index.ts setupWorkspace) which throws
        # on a same-branch open PR — leading to an infinite "Refusing to
        # delete stale branch" loop that exhausts the retry budget without
        # ever progressing. Mirror the reaper's open-PR handoff
        # (reaper.py:912-1048) so the agent-reported FAILED path also routes
        # the task to review/EVALUATING instead of looping retries.
        if payload.status == TaskStatus.FAILED:
            try:
                from app.reaper import _task_has_open_pr

                _has_pr_artifact, _pr_num = await _task_has_open_pr(task, session)
            except Exception as _pr_check_err:
                logger.warning(
                    f"[AGENT_STATUS] PR-artifact lookup failed for task {task.id}: {_pr_check_err}"
                )
                _has_pr_artifact, _pr_num = False, None
            if _has_pr_artifact and _pr_num:
                # ── RELEASE FOR UPDATE BEFORE SLOW GITHUB I/O ──
                # The handler holds the FOR UPDATE on this task row from
                # line ~619 (the early `select(...).with_for_update`).
                # `get_pull_request` below makes a synchronous GitHub API
                # call that can take 5-30s normally and 60-120s during
                # rate-limit / Cloud Run cutover. Holding the lock through
                # that window:
                #   - blocks heartbeats on the same task (they fast-fail with
                #     409 via skip_locked, but rapid 409 churn looks like a
                #     stuck VM to the reaper)
                #   - hits the 60s `idle_in_transaction_session_timeout` set
                #     in database.py, which kills the asyncpg connection
                #     mid-transaction and surfaces downstream as
                #     `InterfaceError("connection is closed")` +
                #     `PendingRollbackError` cascades on subsequent
                #     requests pulling that connection back out of the pool.
                # Production incident 2026-05-04 19:45:29 UTC: 70s
                # server-side latency on /api/agent/status during cutover,
                # caused by exactly this held-lock-around-GH path.
                # Pre-commit a tentative EVALUATING state so the lock
                # releases cleanly, then make the GitHub call with no row
                # lock held, then re-acquire FOR UPDATE on a fresh select
                # to finalize the status. flag_modified is imported at
                # module-level (line 20).
                #
                # R5 fix (2026-05-04): two correctness changes to the
                # tentative pre-commit:
                #   (a) Set status=EVALUATING explicitly. Without this the
                #       pre-commit lands status=FAILED (set at line ~711),
                #       which fires the SQLAlchemy set event registering
                #       the task in `_terminated_task_ids`. JWT validation
                #       for this task then 401s for the entire GH window
                #       even though we're going to upgrade to EVALUATING.
                #       Setting EVALUATING here keeps the JWT live and
                #       avoids the cache pollution.
                #   (b) Pop `_terminated_task_ids` defensively — the
                #       FAILED set event already ran upstream (line 711),
                #       so the cache entry exists. Mirrors lines 1291-1293
                #       and 1624-1625 (other FAILED→non-terminal paths).
                #   No `_recover_poisoned_session` call here: the recovery
                #   would discard the upstream attribute mutations
                #   (attempt_count, payload, retry context, etc.) and
                #   `session.add(task)` after rollback re-attaches an
                #   instance whose change-tracker is empty — so the
                #   pre-commit would land a fragmentary state. If the
                #   session is poisoned upstream, let the commit raise;
                #   the request retries cleanly with an idempotency guard.
                task.status = TaskStatus.EVALUATING
                from app.routers.agent import _terminated_task_ids

                _terminated_task_ids.pop(str(task.id), None)
                task.last_heartbeat = None
                task.latest_status_message = (
                    f"Reported FAILED with open PR #{_pr_num}; checking PR state"
                )
                task.context = {**(task.context or {}), "_pr_state_check_pending": True}
                flag_modified(task, "context")
                session.add(task)
                # Capture pre-GH status for the staleness check after the
                # GitHub call. Used to detect concurrent writers (reaper,
                # admin endpoints, parallel /api/agent/status from a peer
                # role) so we don't clobber their decisions.
                _pre_gh_status = TaskStatus.EVALUATING
                await session.commit()

                _gh_pr_state: str | None = None
                _gh_pr_merged: bool = False
                _gh_pr_base_branch: str | None = None
                try:
                    from app.routers.agent import get_github_token_for_task
                    from app.github import get_pull_request

                    _gh_token, _gh_owner, _gh_repo = await get_github_token_for_task(
                        task.id, session
                    )
                    _pr_data = await get_pull_request(
                        _gh_token, _gh_owner, _gh_repo, _pr_num
                    )
                    _gh_pr_state = _pr_data.get("state")
                    _gh_pr_merged = bool(_pr_data.get("merged"))
                    _gh_pr_base_branch = (_pr_data.get("base") or {}).get("ref")
                except Exception as _gh_exc:
                    # Mirror reaper behavior: assume open on transient GH failure
                    # so a brief outage doesn't strand the task as FAILED-forever.
                    logger.warning(
                        f"[AGENT_STATUS] PR-state lookup failed for task {task.id} PR #{_pr_num}: "
                        f"{type(_gh_exc).__name__}: {_gh_exc} — assuming open"
                    )
                    _gh_pr_state = "open"

                # Re-acquire FOR UPDATE on the same row for the final state
                # transition. populate_existing=True forces SQLA to refresh
                # the cached identity-map instance from the SELECT result.
                # session.expire(task) before the re-SELECT is belt-and-
                # suspenders: populate_existing alone has been observed to
                # be unreliable under async + with_for_update(skip_locked=True)
                # — see rollup_management._relock_parent (~line 186) and
                # task_completion.py:1671 for the canonical pattern.
                try:
                    session.expire(task)
                except InvalidRequestError:
                    # Object detached (rare); fall through to the SELECT,
                    # which will repopulate the row via the identity map.
                    session.expire_all()
                relock_stmt = (
                    select(Task)
                    .where(Task.id == task.id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
                fresh = (await session.execute(relock_stmt)).scalars().first()
                if fresh is None:
                    # Row currently locked by another writer (rare — most
                    # likely a concurrent reaper sweep finalizing this task).
                    # The tentative commit above persisted the
                    # `_pr_state_check_pending` flag as a forensic marker —
                    # no consumer is plumbed yet, so on contention the task
                    # stays in the tentative EVALUATING state. The reaper's
                    # stale-EVALUATING sweep (services._check_evaluating_timeouts,
                    # via the 48h backstop) is the safety net; if a consumer
                    # is added later it can grep for `_pr_state_check_pending`
                    # to find these tasks.
                    logger.info(
                        f"[AGENT_STATUS] Task {task.id} re-lock skipped (locked elsewhere) — "
                        f"tentative state persisted, returning"
                    )
                    return {"status": "accepted_pr_check_deferred"}
                # Staleness check (R5 H2): a concurrent writer (reaper,
                # admin endpoint, parallel /api/agent/status) could have
                # mutated the row to a terminal state during the GH window.
                # Don't overwrite their decision. Compare against the
                # tentative status we committed pre-GH; anything else means
                # someone else finalized.
                if fresh.status != _pre_gh_status:
                    logger.info(
                        f"[AGENT_STATUS] Task {task.id} status changed from "
                        f"{_pre_gh_status} to {fresh.status} during GH window "
                        f"— accepting peer's decision, skipping finalize"
                    )
                    return {
                        "status": "accepted_pr_check_raced",
                        "final_status": fresh.status.value
                        if hasattr(fresh.status, "value")
                        else str(fresh.status),
                    }
                task = fresh
                # C6 fix: do NOT clear `_pr_state_check_pending` up front.
                # It is the write-ahead recovery marker for THIS path: if the
                # process dies (or the review spawn below fails) while the task
                # sits EVALUATING, the services._check_evaluating_timeouts
                # consumer (the `_pr_state_check_pending` branch) re-checks the
                # PR and re-spawns the review. Clearing it before the spawn is
                # confirmed leaves a childless EVALUATING parent invisible to
                # that consumer. The TERMINAL branches (merged / closed-unmerged)
                # clear it explicitly since they reach a final state with no
                # pending spawn; only the still-open branch keeps it set until
                # the spawn succeeds.
                _ctx = dict(task.context or {})
                if _gh_pr_state == "closed" and _gh_pr_merged:
                    _proj = await session.get(Project, task.project_id)
                    logger.info(
                        f"[AGENT_STATUS] Task {task.id} reported FAILED but PR #{_pr_num} was merged "
                        f"out-of-band — classifying landing"
                    )
                    (
                        _record_pr_merged,
                        _terminal_rollup_merge,
                        _landed_branch,
                    ) = await classify_rollup_merge_outcome(
                        task,
                        _proj,
                        session,
                        pr_number=_pr_num,
                        merged_base_branch=_gh_pr_base_branch,
                        source="agent_status.failed_pr_recovery",
                    )
                    _ctx.pop("_pr_state_check_pending", None)
                    task.context = _ctx
                    flag_modified(task, "context")
                    task.status = (
                        TaskStatus.COMPLETED
                        if _terminal_rollup_merge
                        else TaskStatus.EVALUATING
                    )
                    task.vm_instance_id = None
                    task.last_heartbeat = None
                    if _terminal_rollup_merge:
                        task.latest_status_message = (
                            f"Reported FAILED; PR #{_pr_num} was merged out-of-band"
                        )
                    else:
                        task.latest_status_message = (
                            f"Reported FAILED; PR #{_pr_num} merged into "
                            f"{_landed_branch}; awaiting downstream landing"
                        )
                    if _record_pr_merged:
                        apply_merged_landed_signal(
                            task,
                            _proj,
                            base_branch=_landed_branch,
                            hotfix_lane=bool(
                                (task.first_payload or {}).get("hotfix")
                                or (task.context or {}).get("hotfix")
                            ),
                            non_merge_reason="merged_pr_non_integration_branch",
                        )
                    session.add(task)
                    await session.commit()
                    # Heal the direction's research-history merge counts: this PR
                    # merged out-of-band (agent reported FAILED after the merge),
                    # so the COMPLETED-time recompute never saw the landed signal.
                    # Recompute now that it is committed. No-op unless the task
                    # carries direction/research lineage; best-effort.
                    if _record_pr_merged:
                        from app.direction_outcomes import (
                            refresh_direction_outcomes_after_merge,
                        )

                        await refresh_direction_outcomes_after_merge(task, session)
                    await broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": task.status},
                    )
                    return {"status": "accepted_pr_merged"}
                if _gh_pr_state == "closed" and not _gh_pr_merged:
                    logger.info(
                        f"[AGENT_STATUS] Task {task.id} reported FAILED and PR #{_pr_num} closed "
                        f"without merge — marking FAILED (no retry; PR was rejected)"
                    )
                    task.status = TaskStatus.FAILED
                    task.vm_instance_id = None
                    task.last_heartbeat = None
                    task.latest_status_message = (
                        f"Reported FAILED; PR #{_pr_num} was closed without merging"
                    )
                    _ctx.pop("_pr_state_check_pending", None)
                    task.context = {**_ctx, "pr_closed_unmerged": True}
                    flag_modified(task, "context")
                    # Exhaust retries so the orphan-resurrection sweep skips it.
                    task.attempt_count = _runtime_config["max_retries"]
                    session.add(task)
                    await session.commit()
                    await broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": task.status},
                    )
                    return {"status": "accepted_pr_closed"}
                # PR is still open — hand off to review pipeline.
                logger.info(
                    f"[AGENT_STATUS] Task {task.id} reported FAILED with open PR #{_pr_num}; "
                    f"handing off to review pipeline (was attempt {task.attempt_count}/"
                    f"{_runtime_config['max_retries']})"
                )
                task.status = TaskStatus.EVALUATING
                task.vm_instance_id = None
                task.last_heartbeat = None
                task.latest_status_message = f"Reported FAILED with open PR #{_pr_num}; handed off to review pipeline ({failure_class})"
                # C6 fix: `_pr_state_check_pending` is still in `_ctx` (we did
                # NOT pop it up front). Keep it set as a write-ahead recovery
                # marker through the spawn so a death (or a spawn failure)
                # leaves the parent recoverable by the services
                # `_pr_state_check_pending` consumer. It is cleared ONLY on
                # confirmed spawn success below.
                task.context = _ctx
                flag_modified(task, "context")
                session.add(task)
                _spawn_ok = False
                try:
                    from app.review_pipeline import _spawn_pipeline_task
                    from app.shared_state import (
                        derive_task_branch as _derive_task_branch,
                    )

                    _task_type = (task.first_payload or {}).get("type", "") or ""
                    _proj = await session.get(Project, task.project_id)
                    _branch = _derive_task_branch(
                        task.id,
                        _task_type,
                        _shared_state.get_project_task_branch_namespace(_proj),
                    )
                    _base_branch = (task.first_payload or {}).get(
                        "base_branch"
                    ) or _get_project_integration_branch(_proj)
                    _original_prompt = (task.first_payload or {}).get(
                        "prompt", task.description or ""
                    )
                    await _spawn_pipeline_task(
                        task,
                        "review",
                        session,
                        _pr_num,
                        _branch,
                        _base_branch,
                        original_prompt=_original_prompt,
                        original_task_id=str(task.id),
                    )
                    _spawn_ok = True
                except Exception as _spawn_exc:
                    # C6 fix: review-spawn failure no longer strands the task. We
                    # leave `_pr_state_check_pending` set (NOT cleared on the
                    # success path below), so the services
                    # `_check_evaluating_timeouts` consumer re-checks the PR and
                    # re-spawns the review within its grace window — instead of
                    # the parent sitting childless+EVALUATING until the 6h/48h
                    # backstop, invisible to reaper Steps 3 (needs COMPLETED
                    # child) and 4 (skips childless parents).
                    logger.warning(
                        f"[AGENT_STATUS] Review spawn failed for FAILED-with-open-PR task {_task_id_str}: "
                        f"{type(_spawn_exc).__name__}: {_spawn_exc} — leaving "
                        f"_pr_state_check_pending set for the evaluating-sweep consumer",
                        exc_info=True,
                    )
                    # _spawn_pipeline_task does session.flush()/commit() internally —
                    # if it raised on a flush/commit, the session is now in
                    # pending-rollback state and the commit on the next line
                    # would surface as PendingRollbackError. Recover and re-stage
                    # the EVALUATING handoff WITH the recovery marker so the task
                    # isn't stranded RUNNING and stays recoverable.
                    if await _recover_poisoned_session(session, _task_id_str):
                        task.status = TaskStatus.EVALUATING
                        task.vm_instance_id = None
                        task.last_heartbeat = None
                        task.latest_status_message = (
                            f"Reported FAILED with open PR #{_pr_num}; handed off to review pipeline "
                            f"({failure_class}) — review spawn failed, evaluating-sweep will retry"
                        )
                        # The rollback discarded our context mutation; re-stage
                        # the recovery marker so the consumer can find this task.
                        _recover_ctx = dict(task.context or {})
                        _recover_ctx["_pr_state_check_pending"] = True
                        task.context = _recover_ctx
                        flag_modified(task, "context")
                        session.add(task)
                if _spawn_ok:
                    # Spawn confirmed — the review child exists, so drop the
                    # write-ahead recovery marker. (A childed EVALUATING parent
                    # is a normal parked parent; leaving the flag set would make
                    # the consumer try to spawn a SECOND review.)
                    _ok_ctx = dict(task.context or {})
                    _ok_ctx.pop("_pr_state_check_pending", None)
                    task.context = _ok_ctx
                    flag_modified(task, "context")
                    session.add(task)
                await session.commit()
                await broadcast_to_project(
                    str(task.project_id),
                    "task_updated",
                    {"task_id": str(task.id), "status": task.status},
                )
                return {"status": "accepted_pr_handoff"}

        # SCOPE_EMPTY is a deterministic "work already done" exit — retrying
        # just burns another VM for the identical result, so it is terminal
        # on first detection (finalized as CANCELLED in the else-branch).
        # REASSIGNED is the same shape of non-genuine outcome: the agent
        # gracefully self-terminated because the task was superseded /
        # reassigned / deauthed out from under its VM. Re-provisioning a fresh
        # VM for a task that is no longer "owned" is wasted work, so it too is
        # terminal-on-first-detection and lands CANCELLED (no alert, no
        # account/direction ding) — mirroring the SCOPE_EMPTY precedent. A
        # genuine code failure is NEVER classified REASSIGNED, so this never
        # masks a real defect.
        from app.failure_classification import (
            REASSIGNED,
            SCOPE_EMPTY,
            persist_failure_category,
        )

        _NON_GENUINE_TERMINAL_CLASSES = (SCOPE_EMPTY, REASSIGNED)

        # Retry logic for failed tasks
        if (
            payload.status == TaskStatus.FAILED
            and task.attempt_count < _runtime_config["max_retries"]
            and failure_class not in _NON_GENUINE_TERMINAL_CLASSES
        ):
            task.attempt_count += 1
            bank_task_current_attempt(task, stopped_at=task.vm_stopped_at)
            task.status = TaskStatus.QUEUED
            # The earlier FAILED assignment fired the SQLAlchemy set event
            # which added task.id to _terminated_task_ids, revoking the JWT.
            # Since we're retrying, drop the cache entry so the next
            # provisioned VM can successfully claim.
            from app.routers.agent import _terminated_task_ids

            _terminated_task_ids.pop(str(task.id), None)
            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
            task.last_heartbeat = None
            task.started_at = None  # Fresh timeout window for the new VM
            # Add retry delay for transient failures to avoid hammering exhausted resources
            ctx = dict(task.context or {})
            if failure_class == "RATE_LIMIT":
                # Track per-account rate-limit hit count for exponential backoff
                rl_counts = dict(ctx.get("rate_limit_counts") or {})
                if acct_id:
                    rl_counts[str(acct_id)] = rl_counts.get(str(acct_id), 0) + 1
                ctx["rate_limit_counts"] = rl_counts

                rl_hit_count = rl_counts.get(str(acct_id), 1) if acct_id else 1
                # Exponential backoff: 30min * 2^(n-1), capped at 4 hours, ±20% jitter
                base_delay_minutes = min(30 * (2 ** (rl_hit_count - 1)), 240)
                delay_minutes = base_delay_minutes * random.uniform(0.8, 1.2)
                # Audit W1: pass task — set_retry_after now writes the column
                # in addition to the context counter.
                task.context = ctx
                retry_after = set_retry_after(task, delay_minutes)
                ctx = dict(task.context or {})
                logger.info(
                    f"[ORCHESTRATOR] Task {task.id} rate-limited (hit #{rl_hit_count}) — holding back {delay_minutes:.0f}min (retry_after={retry_after})"
                )
                # Blacklist rate-limited account so the retry picks a different one
                if acct_id:
                    blacklisted = list(ctx.get("blacklisted_accounts") or [])
                    if acct_id not in blacklisted:
                        blacklisted.append(acct_id)
                    ctx["blacklisted_accounts"] = blacklisted
            elif failure_class == "PROVISION_FAILURE":
                delay_minutes = 5 * random.uniform(0.75, 1.25)  # 3.75-6.25 minutes
                task.context = ctx
                retry_after = set_retry_after(task, delay_minutes)
                ctx = dict(task.context or {})
                logger.info(
                    f"[ORCHESTRATOR] Task {task.id} provision failure — holding back {delay_minutes:.1f}min (retry_after={retry_after})"
                )
            else:
                task.retry_after_until = None  # audit W1: column source of truth
                # Agent reached the point of POSTing a result, so provisioning worked —
                # don't carry a stale provision budget into the retry.
                ctx.pop("provision_failures", None)
            task.context = ctx
            session.add(task)

            # Same defensive pattern as the early commit below: production
            # 2026-05-04 logged 2x PendingRollbackError at this commit when
            # the upstream result-recording broad-except (line ~1303) or the
            # rate-limit-requeue commit (line ~1297) had already poisoned the
            # session. Recover here so the FAILED→QUEUED retry transition
            # actually persists instead of returning 500 and stranding the
            # task RUNNING.
            if await _recover_poisoned_session(session, _task_id_str):
                logger.warning(
                    f"[PIPELINE] Recovered poisoned session for {_task_id_str} "
                    f"before FAILED-retry commit — upstream swallow-point leaked"
                )
                session.add(task)

            await session.commit()

            await broadcast_to_project(
                str(task.project_id),
                "task_updated",
                {
                    "task_id": str(task.id),
                    "status": TaskStatus.QUEUED,
                    "attempt_count": task.attempt_count,
                },
            )
            return {
                "status": "retrying",
                "message": f"Task {task.id} failed, retrying (Attempt {task.attempt_count}/3)",
            }

        else:
            # Retries exhausted, or a no-retry terminal class (SCOPE_EMPTY).
            # COMPLETED results also reach this else-branch, and
            # ``failure_class`` is only bound on the FAILED path — so gate
            # on payload.status before reading it.
            if (
                payload.status == TaskStatus.FAILED
                and failure_class in _NON_GENUINE_TERMINAL_CLASSES
            ):
                # Non-genuine terminal outcome — NOT a code/review failure:
                #   • SCOPE_EMPTY  — agent found the work already done (clean
                #     no-op, sibling cluster already merged the change).
                #   • REASSIGNED   — task was superseded / reassigned / deauthed
                #     out from under the VM; the agent gracefully bowed out.
                # Land it CANCELLED (not FAILED) and skip the retry annotation,
                # account success-rate ding, and failure alert. FIX H: REASSIGNED
                # parity with the SCOPE_EMPTY->CANCELLED precedent so a handoff
                # neither false-fails the task nor pages the operator.
                task.status = TaskStatus.CANCELLED
                persist_failure_category(task, failure_class)
                session.add(task)
            elif payload.status == TaskStatus.FAILED:
                max_r = _runtime_config["max_retries"]
                existing_msg = task.latest_status_message or ""
                if "retries exhausted" not in existing_msg:
                    task.latest_status_message = (
                        f"[{failure_class}] Retries exhausted ({task.attempt_count}/{max_r}). "
                        + existing_msg[:300]
                    ).strip()
                    session.add(task)
                # Record CODE_ERROR-class failures to 7-day account success-rate tracking.
                # RATE_LIMIT is tracked via disable_account_model; COMPLETED via record_completion_event.
                if failure_class not in (
                    "RATE_LIMIT",
                    "EXTERNAL_API",
                    "PROVISION_FAILURE",
                ):
                    _acct_id_outcome = get_task_llm_account_id(task)
                    if _acct_id_outcome:
                        try:
                            await record_task_outcome(_acct_id_outcome, success=False)
                        except Exception as _outcome_err:
                            logger.warning(
                                f"[AGENT_STATUS] record_task_outcome failed for {_acct_id_outcome}: {_outcome_err}"
                            )
                try:
                    from app.alert_dispatch import _maybe_create_failure_alert

                    await _maybe_create_failure_alert(
                        task,
                        session,
                        task.latest_status_message
                        or f"[{failure_class}] Retries exhausted ({task.attempt_count}/{max_r})",
                    )
                except Exception as _alert_err:
                    # Audit 2026-04-25 A15: alert-creation failure was logged-and-continued.
                    # Operator never saw the alert AND never saw that it failed to create.
                    # Now we (a) mark the task so admin queries can find these silent failures,
                    # (b) raise a project-level WARNING alert about the alert-system failure
                    # itself so observability isn't blind to alert-system breakage.
                    logger.error(
                        f"[AGENT_STATUS] Failed to create failure alert for task {task.id}: {_alert_err}",
                        exc_info=True,
                    )
                    _alert_err_ctx = dict(task.context or {})
                    _alert_err_ctx["alert_creation_failed"] = True
                    _alert_err_ctx["alert_creation_failure"] = (
                        f"{type(_alert_err).__name__}: {str(_alert_err)[:300]}"
                    )
                    task.context = _alert_err_ctx
                    flag_modified(task, "context")
                    session.add(task)
                    try:
                        meta_alert = Alert(
                            project_id=task.project_id,
                            severity=AlertSeverity.WARNING,
                            title=f"Alert system failure on task {str(task.id)[:8]}",
                            message=(
                                f"Failed to create the failure alert for task {task.id} "
                                f"(type={task.first_payload.get('type', '')}, "
                                f"failure_class={failure_class}). "
                                f"Operator must inspect logs — task failure may be invisible "
                                f"in the alerts panel. Error: "
                                f"{type(_alert_err).__name__}: {str(_alert_err)[:200]}"
                            ),
                            task_id=None,  # project-level, not bound to the failed task
                        )
                        session.add(meta_alert)
                        await broadcast_to_project(
                            str(task.project_id),
                            "alert_created",
                            {
                                "alert_id": str(meta_alert.id),
                                "severity": "WARNING",
                                "title": meta_alert.title,
                            },
                        )
                    except Exception as _meta_err:
                        # Last-resort: if even the meta-alert fails, give up gracefully
                        # rather than tearing down the status POST. Logged at ERROR so
                        # CloudLogging picks it up.
                        logger.error(
                            f"[AGENT_STATUS] Meta-alert creation also failed for task {task.id}: {_meta_err}",
                            exc_info=True,
                        )

        # Capture string forms of risky-to-lazy-refresh attributes BEFORE the
        # metrics + recurring-failures blocks below. Production 2026-05-04:
        # those blocks' inner ``await session.rollback()`` calls (lines
        # ~1839, 1919, 1927, 1944, 1955) expire all attached object
        # attributes; subsequent ``f"...{task.id}..."`` log lines then
        # trigger sync lazy refresh → asyncpg pool_pre_ping →
        # MissingGreenlet, which 500s the heartbeat endpoint and starves
        # task dispatch. Captured at parent scope so both the metrics
        # block and the recurring-failures block can name the task in
        # error logs without touching ORM attribute getters.
        _task_id_str = str(task.id)
        _task_project_id_str = str(task.project_id)
        _task_type_str = (task.first_payload or {}).get("type", "unknown")
        # Production 2026-05-05: the f"...{task.id}..." snapshot above
        # protected log lines, but downstream business logic at
        # line ~1973 (``failed_task_payload = task.first_payload``) and
        # the auto-debug branch below also access ORM attributes
        # (task.first_payload, task.context, task.title, task.created_by,
        # task.parent_task_id, task.description) and crashed with the
        # same MissingGreenlet at agent_status.py:1973 → models.py:470.
        # Track whether any rollback below required us to refresh
        # before the auto-debug branch reads. Set True at every
        # rollback site in the metrics + playbook blocks.
        _attrs_may_be_expired = False

        # Record first-attempt success metrics for playbook effectiveness tracking
        if payload.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            try:
                _ctx = dict(task.context or {})
                _ctx["_first_attempt_succeeded"] = _compute_first_attempt_succeeded(
                    task.attempt_count,
                    payload.status,
                    merge_verification=_merge_verification_status(task),
                )
                _proj = None
                try:
                    _proj = await session.get(Project, task.project_id)
                    _playbook_count = (
                        sum(
                            1
                            for k in (_proj.design_doc or {})
                            if k.startswith("playbook/")
                        )
                        if _proj
                        else 0
                    )
                except Exception:
                    _playbook_count = 0
                    # WHY: session.get(Project) can raise OperationalError /
                    # SerializationFailure / connection-lost, marking the
                    # transaction rollback-only. Without this rollback the next
                    # session op (session.add(task) on line ~1674 below, then
                    # the early commit at ~1979) raises PendingRollbackError.
                    with contextlib.suppress(Exception):
                        await session.rollback()
                    _attrs_may_be_expired = True
                _ctx["_had_playbook"] = bool(_playbook_count)
                _ctx["_had_arch_context"] = bool(_ctx.get("_had_arch_context"))
                task.context = _ctx
                session.add(task)
                if payload.status == TaskStatus.COMPLETED and not bool(_playbook_count):
                    logger.info(
                        f"[METRICS] first_task_no_playbook project={_task_project_id_str} task={_task_id_str} "
                        f"type={_task_type_str}"
                    )
                # Increment confirmed_count and update effectiveness scoring for successful first attempts
                if _ctx["_first_attempt_succeeded"] and _playbook_count > 0:
                    try:
                        from app.task_locking import project_lock as _proj_lock

                        _task_type = task.first_payload.get("type", "create")
                        _PLAYBOOK_DEFAULT_TYPES = {
                            "create",
                            "fix",
                            "refactor",
                            "review",
                            "qa",
                            "smoketest",
                            "deps",
                            "migrate",
                            "debug",
                            "docs",
                        }
                        # Acquire project row lock before design_doc read-modify-write.
                        # skip_locked=True: best-effort, contention degrades gracefully.
                        async with _proj_lock(
                            session, task.project_id, skip_locked=True
                        ) as _proj_locked:
                            if _proj_locked is not None:
                                _doc = dict(_proj_locked.design_doc or {})
                                _confirmed_changed = False
                                _now_iso = _utcnow().isoformat()
                                # Keys shown to this task (stored at claim time for accurate outcome tracking)
                                _shown_keys = {
                                    f"playbook/{k}"
                                    for k in (_ctx.get("_shown_playbook_keys") or [])
                                }
                                for _pk, _pv in list(_doc.items()):
                                    if _pk.startswith("playbook/") and isinstance(
                                        _pv, dict
                                    ):
                                        _applicable = _pv.get("applicable_types")
                                        if _applicable is not None:
                                            if _task_type not in _applicable:
                                                continue
                                        elif _task_type not in _PLAYBOOK_DEFAULT_TYPES:
                                            continue
                                        _entry = dict(_pv)
                                        _entry["confirmed_count"] = (
                                            _entry.get("confirmed_count", 0) + 1
                                        )
                                        _entry["last_confirmed_at"] = _now_iso
                                        # Update rolling recent_outcomes only for entries shown to this task
                                        if _pk in _shown_keys:
                                            _recent = list(
                                                _entry.get("recent_outcomes", [])
                                            )
                                            # Flip the most recent unconfirmed (False) outcome to True
                                            _flipped = False
                                            for _i in range(len(_recent) - 1, -1, -1):
                                                if _recent[_i] is False:
                                                    _recent[_i] = True
                                                    _flipped = True
                                                    break
                                            if not _flipped:
                                                # No pending False; append True directly
                                                _recent.append(True)
                                                if len(_recent) > 10:
                                                    _recent = _recent[-10:]
                                            _entry["recent_outcomes"] = _recent
                                            _ref = _entry.get("reference_count", 0)
                                            _conf = _entry.get("confirmed_count", 0)
                                            if _conf > 0 or _ref > 0:
                                                _entry["effectiveness_score"] = round(
                                                    _conf / max(_ref, 1), 4
                                                )
                                        _doc[_pk] = _entry
                                        _confirmed_changed = True
                                if _confirmed_changed:
                                    _proj_locked.design_doc = _doc
                                    flag_modified(_proj_locked, "design_doc")
                                    session.add(_proj_locked)
                    except Exception as _pce:
                        logger.warning(
                            f"[PLAYBOOK] Failed to increment confirmed_count for task {_task_id_str}: {_pce}"
                        )
                # Capture positive pattern for sampling-eligible first-attempt successes
                if _ctx["_first_attempt_succeeded"] and _proj:
                    try:
                        from app.playbook_learning import capture_success_pattern

                        await capture_success_pattern(task, session)
                    except Exception as _sce:
                        logger.warning(
                            f"[PLAYBOOK] Failed to capture success pattern for task {_task_id_str}: {_sce}"
                        )
                        # WHY: capture_success_pattern issues a COUNT query
                        # (session.execute) and session.get(Project, ...) — both
                        # may raise on serialization / lock-timeout / connection
                        # loss, leaving the session pending-rollback and
                        # crashing the early commit at line ~1979. This was one
                        # of the leak categories explicitly named in the
                        # comment block at lines ~1951-1962 ("playbook
                        # capture/learning").
                        with contextlib.suppress(Exception):
                            await session.rollback()
                        _attrs_may_be_expired = True
            except Exception as _e:
                logger.warning(
                    f"[METRICS] Failed to record first-attempt metrics for task {_task_id_str}: {_e}"
                )
                # WHY: the metrics block also touches session.get and session.add;
                # any inner op raising past the inner try/excepts (e.g. on a
                # poisoned session that we already failed to recover) lands here.
                # Roll back so the early commit at ~1979 has a clean session.
                with contextlib.suppress(Exception):
                    await session.rollback()
                _attrs_may_be_expired = True

        # Auto-create playbook entries from recurring failure patterns
        if payload.status == TaskStatus.FAILED:
            try:
                await _check_recurring_failures(task, session)
            except Exception as _pbe:
                logger.warning(
                    f"[PLAYBOOK] Failed to check recurring failures for task {_task_id_str}: {_pbe}"
                )
                # WHY: _check_recurring_failures issues session.execute() (SELECT),
                # session.get(Project, ...), and session.add(project) without an
                # internal flush. A serialization/lock-timeout/connection error
                # raised from session.execute leaves the transaction in
                # pending-rollback state — every later session op (including the
                # early commit at line ~1979) raises PendingRollbackError. Roll
                # back here so downstream commits land on a clean session.
                with contextlib.suppress(Exception):
                    await session.rollback()
                _attrs_may_be_expired = True

        # Production 2026-05-05 fix: any rollback in the metrics block above
        # (4 inner-except sites at lines ~1877, ~1957, ~1965; or this
        # playbook block's except at ~1983) expires SQLAlchemy attribute
        # state on the `task` object. The auto-debug branch below reads
        # ``task.first_payload``, ``task.context``, ``task.title``,
        # ``task.created_by``, ``task.parent_task_id``, ``task.description``,
        # all of which would trigger sync lazy-load → asyncpg
        # pool_pre_ping → MissingGreenlet → 500 (observed at
        # agent_status.py:1973 → models.py:470 in production logs).
        # Refresh once so downstream reads use freshly-loaded attrs.
        # Best-effort: if refresh itself raises on a truly poisoned
        # connection, the suppress preserves the prior behavior — the
        # downstream attribute access will surface a clean error.
        if _attrs_may_be_expired:
            with contextlib.suppress(Exception):
                await session.refresh(task)

        # Spawn a debug task for permanently failed tasks (retries exhausted)
        failed_task_payload = task.first_payload
        failed_task_type = failed_task_payload.get("type", "")
        _is_user_submitted = task.created_by in ("USER", "ADMIN")
        _user_debug_allowed = (
            not _is_user_submitted
            or _shared_state.count_recent_user_debug_spawns(
                str(task.project_id), _utcnow()
            )
            < _shared_state.USER_DEBUG_MAX_PER_HOUR
        )
        should_auto_debug = (
            payload.status
            == TaskStatus.FAILED  # Only for failed tasks (failure_class is unset for COMPLETED)
            and failed_task_type != "debug"  # Don't recursively debug debug tasks
            and (task.context or {}).get("debug_task_id")
            is None  # Don't re-debug fix tasks spawned by debug
            and payload.logs
            and failure_class
            == "CODE_ERROR"  # Only debug actual code errors, not infra/timeout/rate-limit
            and _user_debug_allowed
            and (
                (task.context or {}).get("auto_debug") is True
                or task.parent_task_id
                is not None  # Part of a pipeline (plan/research subtask)
                or task.created_by
                in ("ORCHESTRATOR", "USER", "ADMIN")  # Include user-submitted tasks
            )
        )
        if should_auto_debug:
            _auto_debug_project = await session.get(Project, task.project_id)
            base_branch = failed_task_payload.get(
                "base_branch"
            ) or _get_project_integration_branch(_auto_debug_project)
            debug_task = Task(
                project_id=task.project_id,
                title=f"Debug: {task.title}",
                description=f"Automated root cause analysis for failed task: {task.title}",
                parent_task_id=task.id,
                payloads=[
                    _validate_internal_payload(
                        {
                            "type": "debug",
                            "failed_task_id": str(task.id),
                            "failed_task_title": task.title,
                            "error_logs": (payload.logs or "")[:20000],
                            "base_branch": base_branch,
                            "failed_task_prompt": (
                                failed_task_payload.get("prompt")
                                or task.description
                                or ""
                            )[:2000],
                            "failed_task_type": failed_task_type,
                        },
                        "main:auto_debug",
                    )
                ],
                context={
                    "failed_task_id": str(task.id),
                    "parent_base": base_branch,
                    "related_files": (task.context or {}).get("related_files", []),
                    "scope": (task.context or {}).get("scope"),
                    "failure_class": failure_class,
                },
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                status=TaskStatus.QUEUED,
                created_by="ORCHESTRATOR",
            )
            session.add(debug_task)
            await session.flush()
            if _is_user_submitted:
                _shared_state.record_user_debug_spawn(str(task.project_id), _utcnow())
            await broadcast_to_project(
                str(task.project_id),
                "task_updated",
                {
                    "task_id": str(debug_task.id),
                    "status": TaskStatus.QUEUED,
                    "title": debug_task.title,
                },
            )
            logger.error(
                f"[DEBUG] Spawned debug task {debug_task.id} for permanently failed task {task.id}"
            )

        # Circuit breaker for autonomous tasks (CLOSED/OPEN/HALF-OPEN)
        # Tracks two separate failure classes:
        #  - failure_count: code-quality failures (CODE_ERROR, AGENT_TIMEOUT) → opens at 3
        #  - infra_failure_count: infra wall (PROVISION_FAILURE, RATE_LIMIT, EXTERNAL_API) → opens at 5
        # Both walls block autonomous spawning, but infra opens use a shorter cooldown
        # because infra cascades typically self-heal faster than code regressions.
        _cb_failure_class = classify_failure(
            task, payload.logs, at_time=task.last_heartbeat or task.started_at
        )
        if payload.status == TaskStatus.FAILED and task.created_by == "ORCHESTRATOR":
            project_key = str(task.project_id)
            breaker = _shared_state.get_circuit_breaker(project_key)
            is_code_failure = _cb_failure_class in ("CODE_ERROR", "AGENT_TIMEOUT")
            is_infra_failure = _cb_failure_class in (
                "PROVISION_FAILURE",
                "RATE_LIMIT",
                "EXTERNAL_API",
            )
            if is_code_failure:
                breaker["failure_count"] += 1
                breaker["success_count"] = 0
            elif is_infra_failure:
                breaker["infra_failure_count"] = (
                    breaker.get("infra_failure_count", 0) + 1
                )
                breaker["success_count"] = 0

            # HALF-OPEN test task failed → back to OPEN with doubled cooldown
            # (any failure on the test task re-opens, including infra failures)
            if breaker["state"] == "HALF_OPEN" and str(task.id) == breaker.get(
                "half_open_task_id"
            ):
                breaker["state"] = "OPEN"
                breaker["cooldown_seconds"] = min(
                    breaker["cooldown_seconds"] * 2, 7200
                )  # cap at 2 hours
                breaker["opened_at"] = _utcnow()
                breaker["half_open_task_id"] = None
                logger.warning(
                    f"[CIRCUIT BREAKER] HALF-OPEN → OPEN for {project_key} (cooldown doubled to {breaker['cooldown_seconds']}s)"
                )
                await _maybe_create_circuit_breaker_alert(
                    task.project_id,
                    session,
                    f"HALF-OPEN test task failed — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)",
                )

            # CLOSED → OPEN after 3 consecutive code failures
            elif breaker["state"] == "CLOSED" and breaker["failure_count"] >= 3:
                breaker["state"] = "OPEN"
                breaker["open_reason"] = "code"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = breaker["base_cooldown"]
                await broadcast_to_project(
                    project_key,
                    "autonomous_paused",
                    {
                        "reason": f"Circuit breaker OPEN after {breaker['failure_count']} consecutive code failures"
                    },
                )
                logger.warning(
                    f"[CIRCUIT BREAKER] CLOSED → OPEN (code) for {project_key} after {breaker['failure_count']} failures"
                )
                _cb_alert_text = f"Circuit breaker opened after {breaker['failure_count']} consecutive code failures (cooldown: {breaker['cooldown_seconds']}s)"
                await _maybe_create_circuit_breaker_alert(
                    task.project_id,
                    session,
                    _cb_alert_text,
                )
                try:
                    from app.notification_dispatch import post_failure_alert_message

                    await post_failure_alert_message(
                        session, task.project_id, _cb_alert_text, task.id
                    )
                except Exception as _e:
                    logger.warning(
                        f"[NOTIFY] Failed to post circuit breaker message: {_e}"
                    )
                    # WHY: post_failure_alert_message → post_event_message does
                    # session.add(ProjectMessage). Most failures here are pre-add
                    # (import / no-flush), but autoflush triggered by any
                    # implicit attribute load on `task` (relationship lazy-load)
                    # could surface a flush error and poison the session before
                    # the early commit at ~1979. Roll back defensively — this
                    # was named in the leak categories at lines ~1951-1962.
                    with contextlib.suppress(Exception):
                        await session.rollback()

            # CLOSED → OPEN after 5 consecutive infra failures (wall of provisioning/rate-limit)
            elif (
                breaker["state"] == "CLOSED"
                and breaker.get("infra_failure_count", 0) >= 5
            ):
                breaker["state"] = "OPEN"
                breaker["open_reason"] = "infra"
                breaker["opened_at"] = _utcnow()
                # Shorter cooldown than code breaker — infra outages usually self-heal faster
                breaker["cooldown_seconds"] = 300  # 5 min
                await broadcast_to_project(
                    project_key,
                    "autonomous_paused",
                    {
                        "reason": f"Circuit breaker OPEN after {breaker['infra_failure_count']} consecutive infra failures ({_cb_failure_class})"
                    },
                )
                logger.warning(
                    f"[CIRCUIT BREAKER] CLOSED → OPEN (infra) for {project_key} after {breaker['infra_failure_count']} infra failures"
                )
                _cb_alert_text = (
                    f"Circuit breaker opened: {breaker['infra_failure_count']} consecutive infra failures "
                    f"({_cb_failure_class}). Cooldown: {breaker['cooldown_seconds']}s."
                )
                await _maybe_create_circuit_breaker_alert(
                    task.project_id,
                    session,
                    _cb_alert_text,
                )
                try:
                    from app.notification_dispatch import post_failure_alert_message

                    await post_failure_alert_message(
                        session, task.project_id, _cb_alert_text, task.id
                    )
                except Exception as _e:
                    logger.warning(
                        f"[NOTIFY] Failed to post circuit breaker message: {_e}"
                    )
                    # WHY: same poison risk as the CODE-class branch above —
                    # post_event_message does session.add and an autoflush could
                    # raise. Roll back so the early commit at ~1979 doesn't
                    # surface the upstream poisoning as PendingRollbackError.
                    with contextlib.suppress(Exception):
                        await session.rollback()
            # Persist breaker state on any transition
            if (
                breaker["state"] != "CLOSED"
                or breaker["failure_count"] > 0
                or breaker.get("infra_failure_count", 0) > 0
            ):
                await _orch._persist_circuit_breakers()
        # When review/fix fails permanently, cancel the original create task so rollup
        # skips it (via cancelled-child logic) instead of hanging forever.
        # Other children's work is preserved — only this PR is excluded from the rollup.
        if payload.status == TaskStatus.FAILED:
            await _propagate_pipeline_failure_to_original(task, session)

        if payload.status == TaskStatus.COMPLETED and task.created_by == "ORCHESTRATOR":
            project_key = str(task.project_id)
            breaker = _shared_state.get_circuit_breaker(project_key)
            breaker["failure_count"] = 0
            breaker["infra_failure_count"] = 0
            breaker["success_count"] += 1

            # HALF-OPEN test task succeeded → back to CLOSED
            if breaker["state"] == "HALF_OPEN" and str(task.id) == breaker.get(
                "half_open_task_id"
            ):
                breaker["state"] = "CLOSED"
                breaker["open_reason"] = None
                breaker["cooldown_seconds"] = breaker["base_cooldown"]
                breaker["half_open_task_id"] = None
                logger.warning(
                    f"[CIRCUIT BREAKER] HALF-OPEN → CLOSED for {project_key} (test task succeeded)"
                )
            elif breaker["state"] == "OPEN":
                # Shouldn't happen (OPEN blocks spawning), but reset if it does
                breaker["state"] = "CLOSED"
                breaker["open_reason"] = None
            # Persist on state transition back to CLOSED
            if (
                breaker["state"] == "CLOSED"
                and breaker["failure_count"] == 0
                and breaker["infra_failure_count"] == 0
            ):
                await _orch._persist_circuit_breakers()

        if payload.status == TaskStatus.COMPLETED:
            try:
                from app.notification_dispatch import dispatch_external_notification

                ctx = task.context or {}
                first_payload = task.first_payload or {}
                changed_files = ctx.get("_pr_changed_files") or []
                direction_text = (
                    ctx.get("direction_text")
                    or first_payload.get("direction_text")
                    or ""
                )
                _completed_future = asyncio.ensure_future(
                    dispatch_external_notification(
                        task.project_id,
                        "TASK_COMPLETED",
                        {
                            "task_id": str(task.id),
                            "task_title": task.title or str(task.id)[:8],
                            "task_type": task.first_payload.get("type", ""),
                            "pr_url": ctx.get("_pr_url") or ctx.get("pr_url") or "",
                            "changed_files_count": len(changed_files),
                            "direction_id": ctx.get("direction_id") or "",
                            "direction_text": direction_text[:200],
                            "scope": ctx.get("scope")
                            or first_payload.get("scope")
                            or "",
                            "review_iterations": int(ctx.get("review_iterations") or 0),
                        },
                    )
                )
                del _completed_future  # fire-and-forget; stored to satisfy RUF006
            except Exception as _ne:
                logger.warning(f"[NOTIFY] TASK_COMPLETED dispatch setup failed: {_ne}")

        # ── EARLY COMMIT ──
        # Release the FOR UPDATE lock NOW, before downstream processing.
        # Everything below (review pipeline, completion handlers, rollup) makes
        # GitHub API calls that can hang 15-120+ seconds during connectivity issues.
        # Holding the lock during those calls starves the heartbeat endpoint
        # (which also needs FOR UPDATE on this row), causing last_heartbeat to go
        # stale and the reaper to kill the task.
        # At this point the status change, artifact, and retry logic are all
        # persisted.  expire_on_commit=False keeps the task object usable.
        # Capture task.id as a plain string BEFORE the commit attempt. If the
        # commit raises (e.g. PendingRollbackError from an upstream session-state
        # corruption), accessing `task.id` inside the except handler will trigger
        # a lazy-load on the poisoned session, raise a SECOND exception, and mask
        # the original commit failure. 2026-05-03 incident: the post-PR persist
        # path on /api/agent/status returned 500s with `PendingRollbackError`
        # tracebacks pointing AT THE LOGGER LINE rather than the actual upstream
        # bug; trace-mining confirmed it was the `task.id` access in the f-string
        # raising on a session that was already dead. Capturing as a local makes
        # the original `early_exc` survive into the log so we can actually see
        # which upstream operation poisoned the session.
        _task_id_str = str(task.id)
        # Defensive catch-all: many broad except clauses upstream
        # (playbook capture/learning, _check_recurring_failures,
        # _propagate_pipeline_failure_to_original, circuit-breaker
        # post_failure_alert_message, etc.) log-and-continue on flush
        # failures without rolling back, leaving the session in
        # pending-rollback state. The named recovery sites only cover
        # the largest known swallow-points; any leak surfaces here as
        # PendingRollbackError on the early commit (12x in production
        # 2026-05-04 logs at this line). Rolling back a poisoned session
        # before commit returns the request to a clean state and re-stages
        # the in-memory task mutations so the COMPLETED/FAILED transition
        # the agent reported is still persisted.
        if await _recover_poisoned_session(session, _task_id_str):
            logger.warning(
                f"[PIPELINE] Recovered poisoned session for {_task_id_str} "
                f"before early commit — an upstream swallow-point leaked"
            )
            session.add(task)

        # Write-ahead the post-commit retry marker. _process_post_commit can be
        # killed mid-flight by an instance shutdown — a Cloud Run scale-in or
        # "user disabled instance" SIGKILL that bypasses BOTH the 240s asyncio
        # timeout AND the except handler below, so the failure-path
        # _persist_post_commit_pending never runs and the task orphans with no
        # retry marker (incident 2026-05-21: cluster PR #4775 completed but its
        # research-branch auto-merge was killed ~90s in, stranding the dep
        # chain — eefbed05 sat QUEUED indefinitely). Persisting the marker
        # BEFORE post-commit runs — committed atomically with the terminal
        # status by the early commit below — means any death short of a clean
        # return leaves a marker for _sweep_post_commit_pending to recover.
        # The happy path clears it via _clear_post_commit_pending once
        # _process_post_commit returns; the sweep age-gates reason=
        # "inline_running" markers so a still-running inline pass is never
        # double-processed. Scoped to COMPLETED/FAILED — the only statuses the
        # sweep retries.
        try:
            _payload_dump = payload.model_dump(mode="json")
        except Exception:
            _payload_dump = None
        if payload.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            task.post_commit_pending = True
            task.context = {
                **(task.context or {}),
                "_post_commit_pending_meta": {
                    "at": _utcnow().isoformat(),
                    "reason": "inline_running",
                    "payload": _payload_dump,
                    "attempts": 0,
                },
            }
            flag_modified(task, "context")
            session.add(task)

        try:
            # Commit unconditionally if a transaction is open. The earlier
            # `if session.dirty or session.new` was unsafe: autoflush from any
            # prior SELECT in this handler already wrote pending UPDATEs to the
            # DB and cleared session.dirty, so the check would skip — and then
            # session.close() would roll back the autoflushed changes. Manifests
            # as task.status appearing to update in logs but reverting in DB
            # (most visibly in the EXTERNAL_API permanent-auth-failure path,
            # which has no explicit commit upstream of this point).
            if session.in_transaction():
                await session.commit()
        except Exception as early_exc:
            logger.error(
                f"[PIPELINE] Early commit failed for {_task_id_str}: {early_exc}",
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await session.rollback()
            raise

        # Pass a session factory bound to the SAME engine the request used.
        # In production this is AsyncSessionLocal; in tests it's the test engine.
        # This ensures the helper's fresh session doesn't bypass test DB overrides.
        _engine = resolve_async_engine(session)
        _factory = async_sessionmaker(
            _engine, class_=AsyncSession, expire_on_commit=False
        )
        # Audit Phase C4 (M-C6): release the request-session connection back to
        # the pool now. _process_post_commit and _persist_post_commit_pending
        # both use `_factory` (a fresh session), and the early-commit above
        # already persisted the row state. Holding the request session open
        # during the 240s post-commit timeout below pins one pool slot per
        # in-flight status update — at 1k users that's pool exhaustion.
        # Note: subsequent code paths (success and exception branches) MUST
        # use `_factory`, not `session`. Verified at audit time: the only
        # remaining `session.*` reference would be on the FastAPI generator
        # exit path (get_session's `async with`), which tolerates an
        # already-closed session.
        try:
            await session.close()
        except Exception as _close_exc:
            logger.debug(
                f"[PIPELINE] Early session close failed (non-fatal): {_close_exc}"
            )
        # Cloud Run's default request timeout is 300s. Post-commit makes
        # multiple GitHub calls (review posting, check-run update, PR state
        # check, merge attempts) that each retry with up to 3x120s backoff on
        # 403 rate-limit. A single stalled retry chain can exceed 300s and
        # Cloud Run kills the request mid-execution, bypassing the except
        # handler below. The 240s budget catches an in-Python timeout before
        # that 300s ceiling and routes through the except handler; the
        # write-ahead marker set above is what survives a *hard* kill
        # (instance shutdown) the except handler can never reach.
        try:
            _pc_result = await asyncio.wait_for(
                _process_post_commit(task.id, payload, _session_factory=_factory),
                timeout=240,
            )
        except (Exception, asyncio.TimeoutError) as e:
            # Early-commit already persisted the task status. If post-commit
            # (review pipeline, rollup, etc.) fails or times out, the
            # idempotency guard would block agent retries — permanently
            # orphaning any PR that needed a review/fix spawn. Set a flag so
            # the guard lets the retry re-run post-commit processing.
            is_timeout = isinstance(e, asyncio.TimeoutError)
            is_locked = isinstance(e, PostCommitLocked)
            # C1: the final post-commit commit failed. The task's terminal
            # status is already safely committed (early commit above); only the
            # best-effort deferred side effects (spawned follow-up / remediation
            # tasks) were dropped. We MUST preserve the write-ahead marker so the
            # sweep retries those side effects — but the agent has nothing to do,
            # so do NOT re-raise a 500. Persist the marker (below), then return a
            # clean 200 ("retry pending"). Without this, _process_post_commit
            # swallowing the failure → clean return → marker cleared → sweep
            # never retries (incident eefbed05).
            is_final_commit = isinstance(e, PostCommitFinalCommitFailed)
            # A disconnection-class DB error (connection severed mid-request by a
            # Cloud Run deploy cutover / instance drain) is infra collateral, not
            # a code fault. ``connection_invalidated`` is SQLAlchemy's canonical
            # disconnect flag (set when the pool's handle_error listener invalidated
            # the connection — see database._install_disconnect_invalidation);
            # InterfaceError ("connection is closed") is the asyncpg shape that
            # surfaces directly. The terminal status is already committed and the
            # write-ahead marker (below) hands the deferred side effects to
            # _sweep_post_commit_pending, so re-raising would only turn benign
            # cutover collateral into a 500 → Cloud Run ContainerError → instance
            # quarantine (prod 2026-06-03 07:38 cutover burst).
            is_disconnect = bool(getattr(e, "connection_invalidated", False)) or isinstance(
                e, InterfaceError
            )
            if is_timeout:
                reason = "post_commit_timeout"
            elif is_locked:
                reason = "post_commit_locked"
            elif is_final_commit:
                reason = "post_commit_final_commit_failed"
            elif is_disconnect:
                reason = "post_commit_disconnect"
            else:
                reason = "post_commit_error"
            logger.error(  # noqa: TRY400 — conditional exc_info: suppress for expected timeout/lock, show for real errors
                f"[PIPELINE] Post-commit {'timed out' if is_timeout else ('locked' if is_locked else ('final-commit failed' if is_final_commit else 'failed'))} for task {task.id}: {e}",
                exc_info=not (is_timeout or is_locked),
            )
            # The request session may be poisoned (LockNotAvailableError tears
            # down the transaction) and the task row may still be held by a
            # peer. Use a fresh session with skip_locked + retry so the pending
            # marker lands even when the request session can't commit.
            # ``_payload_dump`` was already computed for the write-ahead marker;
            # _persist_post_commit_pending re-stamps the marker with the
            # specific failure reason and bumps its attempt counter.
            _persist_err: Exception | None = None
            try:
                await _persist_post_commit_pending(
                    _factory, task.id, reason, _payload_dump
                )
            except Exception as ctx_err:
                _persist_err = ctx_err
                logger.exception(
                    f"[PIPELINE] Failed to set _post_commit_pending for task {task.id}: {ctx_err}"
                )
            # C1: a final-commit failure is non-fatal to the agent — the marker
            # is now persisted, so the sweep owns the retry. Return a clean 200
            # ("retry pending") rather than 500ing the agent, UNLESS the marker
            # persist itself also failed (then there is no retry record and we
            # must surface the failure so the agent's retry re-runs post-commit
            # via the idempotency guard, which re-arms the marker).
            #
            # A row-contention lock (PostCommitLocked) is the same shape: the
            # marker is now persisted so the sweep owns the retry, and
            # PostCommitLocked is documented (task_lifecycle.py:149) as a
            # "retryable pending, not an error". Re-raising it would surface a
            # 5xx → Cloud Run ContainerError for what is benign contention with a
            # peer post-commit driver, exactly the case the row-lock guard above
            # converts to a 429. So treat it like the final-commit case.
            if (
                is_final_commit or is_locked or is_disconnect
            ) and _persist_err is None:
                if is_disconnect:
                    logger.warning(
                        "[PIPELINE] Post-commit hit a disconnection-class DB error "
                        "for task %s (instance-shutdown collateral) — terminal status "
                        "already committed, retry marker persisted; returning 200 so "
                        "the sweep owns the deferred side effects instead of 500ing the "
                        "agent.",
                        _task_id_str,
                    )
                return {
                    "status": "post_commit_retry_pending",
                    "task_id": _task_id_str,
                }
            # Audit 2026-04-25 A9: explicitly chain the persist failure so
            # operators see both the original post-commit error AND the
            # secondary persist failure in one traceback. The bare `raise`
            # already used implicit __context__ chaining, but `raise … from`
            # makes the intent unambiguous and survives future refactors.
            if _persist_err is not None:
                raise e from _persist_err
            raise

        # _process_post_commit returned cleanly. Clear the write-ahead marker
        # so _sweep_post_commit_pending doesn't re-run an already-finished
        # post-commit. Best-effort — see _clear_post_commit_pending.
        await _clear_post_commit_pending(_factory, task.id)
        return _pc_result
