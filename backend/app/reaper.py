"""Background reaper service: detects and recovers orphaned/stale tasks."""

import asyncio
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict

from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import Task, TaskStatus, Project, Artifact, ArtifactType
from app.compute import (
    get_provision_error,
    record_zone_failure,
    _capture_serial_for_vm,
    get_vm_zone,
    get_vm_status,
    INFRA_PROVISION_PATTERNS,
    any_zone_available,
    soonest_zone_recovery,
)
from app.shared_state import runtime_config as _runtime_config
from app.shared_state import (
    record_pipeline_event,
    external_health as _external_health,
    resolve_retry_budget,
)
from app.failure_classification import (
    classify_failure,
    persist_failure_category,
    INFRA_CLASSES,
)
from app.retry_budget import append_failure_class_history
from app.time_utils import _utcnow, set_retry_after, RETRY_AFTER_MAX_MINUTES

logger = logging.getLogger(__name__)


async def _relock_orphan_task(
    session,
    task_id,
    cached_task: Task | None,
    expected_status,
    expected_last_heartbeat,
) -> Task | None:
    """Re-acquire FOR UPDATE on an orphan task and validate the pre-IO snapshot.

    Mirrors ``rollup_management._relock_parent`` (canonical reference) and
    ``ci_completion._relock_merge_task``: expire-then-populate_existing-SELECT
    handles the cached-identity-map staleness under async + skip_locked.

    Returns the freshly-loaded Task if:
      - the row is still ours (not held by another writer), AND
      - status is unchanged (still PROVISIONING or IN_PROGRESS, not COMPLETED/FAILED), AND
      - last_heartbeat hasn't advanced (agent didn't revive during the unlocked window)

    Returns None on any of those conditions failing — caller should ``continue``
    to the next orphan task.
    """
    if cached_task is not None:
        try:
            session.expire(cached_task)
        except InvalidRequestError:
            session.expire_all()
    relock_stmt = (
        select(Task)
        .where(Task.id == task_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    fresh = (await session.execute(relock_stmt)).scalars().first()
    if fresh is None:
        logger.info(
            f"[REAPER] Orphan re-lock skipped for task {task_id} — held by peer "
            f"(deferring to next reaper cycle)"
        )
        return None
    if fresh.status not in (TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS):
        logger.info(
            f"[REAPER] Orphan re-lock found task {task_id} status transitioned "
            f"to {fresh.status} during external I/O — skipping reap"
        )
        return None
    if fresh.last_heartbeat != expected_last_heartbeat:
        logger.info(
            f"[REAPER] Orphan re-lock found task {task_id} heartbeat advanced "
            f"({expected_last_heartbeat} → {fresh.last_heartbeat}) during external I/O "
            f"— agent revived, skipping reap"
        )
        return None
    if fresh.status != expected_status:
        # Defensive: status changed within the (PROVISIONING, IN_PROGRESS) set
        # — e.g. claim_task flipped PROVISIONING → IN_PROGRESS. Not the same as
        # a terminal transition, but the reap reason classification was based on
        # the original status, so safer to defer.
        logger.info(
            f"[REAPER] Orphan re-lock found task {task_id} status changed "
            f"({expected_status} → {fresh.status}) during external I/O — skipping reap"
        )
        return None
    return fresh


# Reaper: track tasks skipped by skip_locked to force-reap after repeated skips
_reaper_skip_counts: Dict[uuid.UUID, int] = {}

# Reaper liveness telemetry. Incremented at the END of each fully-successful
# cycle (after the success path resets consecutive_errors). If the cycle body
# raises, the counter does NOT increment and the age gauge keeps growing —
# that's the signal a deploy gate / alert can fire on. Production 2026-05-04:
# the reaper crashed every cycle on a MissingGreenlet at reaper.py:724 for
# 80+ minutes before operator noticed; this counter is the broad backstop
# against any "reaper crash-loops silently" failure mode, not just that bug.
_reaper_cycles_completed_count: int = 0
_reaper_last_cycle_completed_at: datetime | None = None


def get_reaper_stats() -> dict:
    """Snapshot of reaper liveness for /metrics and admin observability.

    ``last_cycle_age_seconds`` is the wall-clock seconds since the last
    fully-successful cycle. -1 means the reaper has never completed a cycle
    (process just started, or every cycle has crashed). Alerts should fire
    if this exceeds the reaper sleep interval by a meaningful margin
    (e.g. >300s — five expected cycles missed).
    """
    age_seconds: float | int = -1
    if _reaper_last_cycle_completed_at is not None:
        age_seconds = max(
            0, (_utcnow() - _reaper_last_cycle_completed_at).total_seconds()
        )
    return {
        "cycles_completed_total": _reaper_cycles_completed_count,
        "last_cycle_age_seconds": age_seconds,
    }


# Per-(provider, model) regression scan cadence. The reaper itself fires every
# ~60s; the regression scan is gated to ~15min so we don't pay the per-account
# Task aggregation cost on every reaper tick. Aligned with the existing
# _QUARANTINE_COOLDOWN_SECONDS (15 min) referenced in the task spec.
_REGRESSION_SCAN_INTERVAL_SECONDS = 15 * 60
_last_regression_scan: datetime | None = None


_TRANSIENT_5XX_PATTERNS = frozenset(
    {
        "http 502",
        "http 503",
        "http 504",
        "502 bad gateway",
        "503 service",
        "504 gateway",
        "server error",
    }
)


# Audit R3-W2 (H7): per-tick fetch cap for unbounded reaper scans. A project
# with a flood of stuck tasks would otherwise pull megabytes per tick. The
# constant is intentionally generous (5000) — any tick that hits the cap
# emits a WARNING so operators can chunk-paginate as a follow-up.
_BATCH = 5000


_ANALYSIS_ARTIFACT_KEYS = (
    ("_pr_diff_summary", "_previous_diff_summary"),
    ("_test_failure_summary", "_previous_test_failure_summary"),
    ("_self_review_outcome", "_previous_self_review_outcome"),
    ("_analysis_plan", "_previous_analysis_plan"),
)


def _copy_analysis_artifacts_to_previous(ctx: dict) -> None:
    # Mirror failed-attempt artifacts into _previous_* so the agent's buildRetryContext
    # surfaces them — matches task_lifecycle._requeue_parent_task.
    for src, dst in _ANALYSIS_ARTIFACT_KEYS:
        if ctx.get(src):
            ctx[dst] = ctx[src]


_HEARTBEAT_STALE_DEFAULTS = {
    "small": 300,
    "medium": 420,
    "large": 900,
}


_HEARTBEAT_EFFORT_FLOORS = {
    "low": 300,
    "medium": 300,
    "high": 420,
    "max": 720,
}


_PRECLAIM_STALE_DEFAULTS = {
    "small": 360,
    "medium": 480,
    "large": 600,
}

# Progress-staleness thresholds: how long a fresh-heartbeating task may go
# without any tool/LLM event before being classified as agent_stalled.
# Longer than heartbeat-stale to avoid false positives on normal LLM think time.
_PROGRESS_STALE_DEFAULTS = {
    "small": 480,  # 8 min
    "medium": 720,  # 12 min
    "large": 1080,  # 18 min
}

# Task types where the agent legitimately spends 5-20 min reasoning before
# emitting a tool call (b2410541 incident: Claude Opus on strategic_review
# was reaped after 484s while in the middle of pass-1 thinking — actual
# attempt completed cleanly in 19 min once given a budget). These types
# get a flat longer cutoff regardless of nominal scope.
_THINKING_HEAVY_TASK_TYPES = frozenset(
    {
        "strategic_review",
        "design_doc_update",
        "research",
        "investigate",
        "plan",
    }
)
_THINKING_HEAVY_PROGRESS_STALE_SECONDS = 1500  # 25 min


def _progress_stale_cutoff(task, now: datetime) -> datetime:
    """Return cutoff time for meaningful-progress (last_tool_use_at) staleness.

    Only fires when last_heartbeat IS fresh (agent alive but wedged). Uses
    longer thresholds than heartbeat-stale to tolerate normal LLM latency.
    Thinking-heavy task types override scope-based defaults — reasoning
    models on strategic/research work spend long stretches between tool
    calls (8-min cutoff misclassifies normal Opus reasoning as stalled).
    """
    ctx = task.context or {}
    payload = task.first_payload or {}
    task_type = payload.get("type") or ""
    if task_type in _THINKING_HEAVY_TASK_TYPES:
        thinking_override = _runtime_config.get(
            "progress_stale_seconds_thinking_heavy",
            _THINKING_HEAVY_PROGRESS_STALE_SECONDS,
        )
        return now - timedelta(seconds=thinking_override)
    scope = ctx.get("scope") or payload.get("scope")
    thresholds = _runtime_config.get(
        "progress_stale_seconds_by_scope", _PROGRESS_STALE_DEFAULTS
    )
    seconds = thresholds.get(scope, thresholds.get("small", 480))
    return now - timedelta(seconds=seconds)


def _extract_tool_trace_tail(task) -> str:
    """Extract and format the tail of _tool_trace from task.context.

    Returns a compact formatted string of recent tool calls (timestamp + tool name
    + first ~80 chars of input preview), limited to ~400 chars total. If _tool_trace
    is missing or empty, returns empty string.
    """
    ctx = task.context or {}
    tool_trace = ctx.get("_tool_trace") or []
    if not tool_trace:
        return ""

    # Take the last 5 entries to stay within 400-char budget
    entries = tool_trace[-5:] if len(tool_trace) > 5 else tool_trace
    parts = []
    total_len = 0
    max_total = 400

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        t_str = entry.get("t", "").split("T")[-1][:8]  # HH:MM:SS from ISO-8601
        tool_name = entry.get("tool", "?")[:20]
        input_preview = entry.get("input_preview", "")[:80]
        # Compact format: timestamp + tool + input
        part = f"{t_str} {tool_name}: {input_preview}"
        if total_len + len(part) + 3 > max_total:  # +3 for " | "
            break
        parts.append(part)
        total_len += len(part) + 3

    if not parts:
        return ""
    return " | ".join(parts)


def _preclaim_stale_cutoff(task, now: datetime) -> datetime:
    ctx = task.context or {}
    payload = task.first_payload or {}
    scope = ctx.get("scope") or payload.get("scope")
    thresholds = _runtime_config.get(
        "preclaim_stale_seconds_by_scope", _PRECLAIM_STALE_DEFAULTS
    )
    seconds = thresholds.get(scope, thresholds.get("small", 360))
    return now - timedelta(seconds=seconds)


def _heartbeat_stale_cutoff(task, now: datetime, has_open_pr: bool = False) -> datetime:
    # When a task has an open PR, reduce the heartbeat-stale cutoff to 90s
    # to detect post-PR silent deaths faster and hand off to the review
    # pipeline sooner (normally 5-15 min delay; with PR cutoff ~90-150s).
    # The 90s gives one missed heartbeat (60s) plus one reaper cycle (~30s).
    if has_open_pr:
        return now - timedelta(seconds=90)

    ctx = task.context or {}
    payload = task.first_payload or {}
    scope = ctx.get("scope") or payload.get("scope")
    thresholds = _runtime_config.get(
        "heartbeat_stale_seconds_by_scope", _HEARTBEAT_STALE_DEFAULTS
    )
    seconds = thresholds.get(scope, thresholds.get("small", 300))

    effort = ctx.get("effort") or payload.get("effort")
    if not effort:
        task_type = ctx.get("type") or payload.get("type")
        if task_type:
            # Resolve effort from the provider settings bag: merge Anthropic
            # defaults with any per-task-type override. Reaper needs a scalar
            # effort to size the heartbeat-stale floor; other providers'
            # effort-like knobs (reasoning_effort, thinking_budget) aren't
            # normalized so we look at Anthropic only, which is the dispatch
            # default.
            defaults = _runtime_config.get("provider_settings_defaults", {}).get(
                "anthropic", {}
            )
            overrides = (
                _runtime_config.get("task_type_provider_overrides", {})
                .get(task_type, {})
                .get("anthropic", {})
            )
            effort = overrides.get("effort") or defaults.get("effort")
    floor = _HEARTBEAT_EFFORT_FLOORS.get(effort, 0)
    seconds = max(seconds, floor)
    return now - timedelta(seconds=seconds)


async def _task_has_open_pr(task, session) -> tuple[bool, int | None]:
    """Check whether a task has a PULL_REQUEST artifact (PR was published).

    NOTE: Despite the name, this only checks the Artifact table — it does
    NOT verify the PR is still open on GitHub. The PR may have been closed
    or merged out-of-band. Callers that act on "open" must re-confirm via
    get_pull_request before parking the task in EVALUATING.

    Used by the reaper: a task that pushed a branch and opened a PR before
    being reaped should hand off to the review pipeline (EVALUATING) rather
    than retry from scratch (QUEUED) — the retry path would collide with the
    existing branch and be refused by the agent's stale-branch guard
    (agent/src/index.ts:706), orphaning the PR.

    The Artifact table is written atomically with PR creation (see
    routers/agent.py:2035). Returns (True, pr_number) on match.
    """
    stmt = select(Artifact).where(
        Artifact.task_id == task.id,
        Artifact.artifact_type == ArtifactType.PULL_REQUEST,
    )
    artifact = (await session.execute(stmt)).scalars().first()
    if artifact is None or not artifact.pr_url:
        return False, None
    # Parse PR number from the URL (e.g. https://github.com/owner/repo/pull/2903)
    try:
        pr_num = int(artifact.pr_url.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return False, None
    return True, pr_num


async def _sweep_orphan_direction_holds(session) -> int:
    """Release direction holds whose direction_id no longer maps to a live direction.

    A direction is "live" if it exists in some project's directions JSONB with
    status in (active, paused, proposed). completed/expired/rejected directions
    shouldn't hold tasks — and archived directions that disappear entirely
    definitely shouldn't. Returns the number of holds cleared.
    """
    from app.shared_state import (
        direction_failure_streaks,
        clear_direction_hold,
    )

    live_dids: set[str] = set()
    projects = (await session.execute(select(Project))).scalars().all()
    for proj in projects:
        for d in proj.directions or []:
            if d.get("status") in ("active", "paused", "proposed"):
                did_val = d.get("id")
                if did_val:
                    live_dids.add(did_val)
    cleared = 0
    for did, streak in list(direction_failure_streaks.items()):
        if not streak.get("held"):
            continue
        if did not in live_dids:
            clear_direction_hold(did)
            cleared += 1
            logger.info(
                f"[REAPER] Cleared hold on orphan direction {did} "
                "(no longer live in any project's directions)"
            )
    if cleared:
        logger.info(f"[REAPER] Cleared {cleared} orphan direction hold(s)")
    return cleared


async def _sweep_stale_residual_cases(session) -> int:
    """Close stale residual cases and auto-resolve quiet REMEDIATING cases.

    ACTIVE rows transition to CLOSED_ORGANICALLY after the configured quiet
    window, unless the fingerprint has been seen recently. REMEDIATING rows
    transition to RESOLVED when the remediation has been stale for the
    configured window and no new failures carried that fingerprint in-window.
    """
    from app.models import ResidualRemediationCase, ResidualCaseStatus
    from app.failure_classification import _mark_residual_case_closed
    from app.notification_dispatch import post_event_message

    now = _utcnow()
    active_hours = int(_runtime_config.get("residual_active_auto_close_hours", 72))
    remediating_hours = int(
        _runtime_config.get("residual_remediating_auto_resolve_hours", active_hours)
    )
    recent_hit_grace_hours = int(
        _runtime_config.get("residual_recent_hit_grace_hours", 1)
    )
    active_cutoff = now - timedelta(hours=active_hours)
    remediating_cutoff = now - timedelta(hours=remediating_hours)
    recent_hit_cutoff = now - timedelta(hours=recent_hit_grace_hours)

    active_stale_cases = (
        (
            await session.execute(
                select(ResidualRemediationCase).where(
                    ResidualRemediationCase.status == ResidualCaseStatus.ACTIVE,
                    ResidualRemediationCase.last_seen_at < active_cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    remediating_stale_cases = (
        (
            await session.execute(
                select(ResidualRemediationCase).where(
                    ResidualRemediationCase.status == ResidualCaseStatus.REMEDIATING,
                    ResidualRemediationCase.last_spawned_at.is_not(None),
                    ResidualRemediationCase.last_spawned_at < remediating_cutoff,
                    ResidualRemediationCase.last_seen_at < remediating_cutoff,
                )
            )
        )
        .scalars()
        .all()
    )

    closed = 0
    for case in active_stale_cases:
        if case.last_seen_at >= recent_hit_cutoff:
            logger.info(
                "[REAPER] Deferring ACTIVE residual case close %s (fingerprint=%r, last_seen_at=%s)",
                case.id,
                case.fingerprint,
                case.last_seen_at.isoformat(),
            )
            continue
        case.status = ResidualCaseStatus.CLOSED_ORGANICALLY
        case.updated_at = now
        session.add(case)
        _mark_residual_case_closed(case.fingerprint)
        closed += 1
        logger.info(
            "[REAPER] Closed stale residual case %s (fingerprint=%r, last_seen_at=%s)",
            case.id,
            case.fingerprint,
            case.last_seen_at.isoformat(),
        )
    for case in remediating_stale_cases:
        case.status = ResidualCaseStatus.RESOLVED
        case.updated_at = now
        session.add(case)
        _mark_residual_case_closed(case.fingerprint)
        closed += 1
        await post_event_message(
            session,
            case.project_id,
            event_type="residual_fix_resolved_organically",
            summary=f"Residual auto-fix resolved organically for {case.fingerprint[:32]} after quiet period",
            context={"fingerprint": case.fingerprint, "result": "resolved_organically"},
            task_id=case.last_spawned_task_id,
        )
        logger.info(
            "[REAPER] Auto-resolved stale REMEDIATING residual case %s (fingerprint=%r, last_spawned_at=%s, last_seen_at=%s)",
            case.id,
            case.fingerprint,
            case.last_spawned_at.isoformat() if case.last_spawned_at else None,
            case.last_seen_at.isoformat(),
        )

    if closed:
        await session.commit()
        logger.info("[REAPER] Closed %d stale residual remediation case(s)", closed)
    return closed


async def _run_regression_scan_and_notify(session) -> int:
    """Run the (provider, model) regression scan and fan out alerts per fired event.

    Each newly-fired quarantine becomes (a) a ProjectMessage on every project
    the owning user is a member of and (b) one external webhook dispatch per
    project (matching the existing per-project notification routing). This
    matches how _emit_all_accounts_blocked_stall fans out — accounts are
    user-scoped, so any project the user owns is a valid landing spot for the
    operator-facing notification.

    Returns the number of newly-fired quarantine events.
    """
    from app.max_plan_pool import scan_account_model_regressions
    from app.notification_dispatch import (
        dispatch_external_notification,
        post_account_model_regression_message,
    )
    from app.models import ProjectMember

    fired = await scan_account_model_regressions(session)
    if not fired:
        return 0

    # Cache user_id → list[project_id] across this scan so multi-account users
    # don't re-query for every fired event.
    user_projects_cache: dict[str, list[uuid.UUID]] = {}
    for event in fired:
        user_id = event.get("user_id")
        if not user_id:
            logger.warning(
                f"[REAPER] regression-quarantine for {event['account_id']} has no user_id — "
                "skipping notification fan-out"
            )
            continue
        if user_id not in user_projects_cache:
            rows = (
                (
                    await session.execute(
                        select(ProjectMember.project_id).where(
                            ProjectMember.user_id == user_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            user_projects_cache[user_id] = list(rows)
        project_ids = user_projects_cache[user_id]
        if not project_ids:
            logger.info(
                f"[REAPER] regression-quarantine fired for user {user_id} with no projects — "
                "skipping notification fan-out (event recorded in pool state)"
            )
            continue
        payload = {
            "account_id": event["account_id"],
            "account_label": event["account_label"],
            "provider": event["provider"],
            "model": event["model"],
            "account_rate": event["account_rate"],
            "project_median": event["project_median"],
            "sample_size": event["sample_size"],
            "distinct_accounts": event["distinct_accounts"],
            "started_at": event["started_at"],
        }
        for pid in project_ids:
            try:
                await post_account_model_regression_message(
                    session,
                    pid,
                    account_id=event["account_id"],
                    account_label=event["account_label"],
                    provider=event["provider"],
                    model=event["model"],
                    account_rate=event["account_rate"],
                    project_median=event["project_median"],
                    sample_size=event["sample_size"],
                    distinct_accounts=event["distinct_accounts"],
                    started_at=event["started_at"],
                )
            except Exception:
                logger.exception(
                    f"[REAPER] post_account_model_regression_message failed for project {pid}"
                )
            asyncio.ensure_future(  # noqa: RUF006
                dispatch_external_notification(
                    pid,
                    "account_model_regression_quarantined",
                    payload,
                )
            )
    try:
        await session.commit()
    except Exception:
        logger.exception(
            "[REAPER] Failed to commit regression quarantine notifications"
        )
        try:
            await session.rollback()
        except Exception:
            logger.exception("[REAPER] Rollback after regression-notify commit failed")
    return len(fired)


async def reaper_service():
    """
    Background worker that scans for orphaned VMs.
    Marks them as FAILED and issues a delete command to GCP if they haven't sent a heartbeat in 5 minutes.
    """
    # Deferred imports to avoid circular dependency with app.main
    from app.max_plan_pool import (
        clear_task_llm_runtime_context,
        disable_account_model,
        get_task_llm_account_id,
        _extract_task_model,
        release_max_plan_account,
    )
    from app.main import (
        _ensure_vm_deleted,
        _maybe_create_failure_alert,
    )
    from app.routers.agent_status import record_task_result
    from app.broadcast import broadcast_to_project
    from app.task_lifecycle import _requeue_parent_task
    from app.rollup_management import _check_and_trigger_rollup

    consecutive_errors = 0
    while True:
        try:
            async with AsyncSessionLocal() as session:
                # Step 1: Read active tasks WITHOUT locks (avoids blocking heartbeat writes)
                # Phase B4 (H-P1): drop heavy ``plan_output`` JSONB from the projection.
                # ``status_log`` MUST stay loaded because the Task.status / Task.
                # latest_status_message event listeners (models.py ~360) read it on
                # every assignment; under async sessions an unloaded status_log
                # triggers MissingGreenlet. ``context`` and ``payloads`` stay loaded
                # because _heartbeat_stale_cutoff / _preclaim_stale_cutoff read both
                # via task.first_payload (L-P2). The reaper mutates status,
                # latest_status_message, vm_instance_id, last_heartbeat, started_at,
                # context, payloads, attempt_count downstream.
                # Audit R3-W2 (H7): cap per-tick fetch at _BATCH rows.
                stmt = (
                    select(Task)
                    .options(
                        load_only(
                            Task.id,
                            Task.status,
                            Task.last_heartbeat,
                            Task.last_tool_use_at,
                            Task.started_at,
                            Task.created_at,
                            Task.vm_instance_id,
                            Task.vm_provisioned_at,
                            Task.project_id,
                            Task.parent_task_id,
                            Task.latest_status_message,
                            Task.attempt_count,
                            Task.context,
                            Task.payloads,
                            Task.status_log,
                            Task.retry_after_until,
                            Task.previous_error_fingerprint,
                            Task.requeue_in_flight,
                            Task.description,
                        )
                    )
                    .where(
                        Task.status.in_(
                            [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
                        )
                    )
                    .limit(_BATCH)
                )
                active_tasks = (await session.execute(stmt)).scalars().all()
                if len(active_tasks) >= _BATCH:
                    logger.warning(
                        "[REAPER] active sweep hit cap=%d rows — result truncated; "
                        "follow-up: chunk-paginate",
                        _BATCH,
                    )

                now = _utcnow()
                ten_minutes_ago = now - timedelta(minutes=10)
                fifteen_minutes_ago = now - timedelta(minutes=15)

                # Batch query for PULL_REQUEST artifacts to support PR-based heartbeat
                # cutoff reduction. Tasks with open PRs use a 90s cutoff instead of
                # scope-aware defaults to detect post-PR silent deaths faster.
                task_ids_to_check = {t.id for t in active_tasks if t.vm_instance_id}
                pr_artifacts = (
                    (
                        await session.execute(
                            select(Artifact).where(
                                Artifact.task_id.in_(task_ids_to_check),
                                Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                            )
                        )
                    )
                    .scalars()
                    .all()
                    if task_ids_to_check
                    else []
                )
                tasks_with_pr = {a.task_id for a in pr_artifacts}

                # State-machine invariants after the clock-separation fix:
                #   - vm_provisioned_at: written by orchestrator when the VM is dispatched
                #     and when GCE confirms it's running. Never written by the agent.
                #   - last_heartbeat: written only by the agent (claim + heartbeat interval).
                #     None on a PROVISIONING task means the agent has not contacted us yet.
                #
                # Thresholds:
                #   - IN_PROGRESS + stale heartbeat → agent crashed mid-run (attempt budget).
                #     Stale cutoff is scope-aware: 5min (small) / 7min (medium) / 10min (large).
                #     Exception: tasks with open PRs use 90s cutoff to speed handoff to review.
                #   - PROVISIONING + vm_provisioned_at > 6min ago + no heartbeat → silent pre-claim
                #     death (provision budget). 6min gives margin over the ~90-150s nominal cold
                #     boot + auth validation + claim sequence.
                #   - PROVISIONING + started_at > 15min ago → absolute catch-all.
                orphaned_ids = []
                for task in active_tasks:
                    if not task.vm_instance_id:
                        continue
                    has_pr = task.id in tasks_with_pr
                    hb_cutoff = _heartbeat_stale_cutoff(task, now, has_open_pr=has_pr)
                    if task.status == TaskStatus.PROVISIONING:
                        if task.started_at and task.started_at < fifteen_minutes_ago:
                            orphaned_ids.append(
                                task.id
                            )  # absolute provisioning ceiling
                            logger.warning(
                                f"[REAPER] Task {task.id} stuck in PROVISIONING for >15min (started_at={task.started_at})"
                            )
                        elif task.last_heartbeat and task.last_heartbeat < hb_cutoff:
                            # Rare: agent claimed during PROVISIONING (shouldn't happen since
                            # claim_task also flips status to IN_PROGRESS), then went silent.
                            orphaned_ids.append(task.id)
                        elif (
                            task.vm_provisioned_at
                            and task.vm_provisioned_at
                            < _preclaim_stale_cutoff(task, now)
                            and task.last_heartbeat is None
                        ):
                            # Silent pre-claim death: VM was dispatched long enough ago
                            # (scope-aware: small 6min, medium 8min, large 10min) and the
                            # agent never claimed. Charge provision budget.
                            orphaned_ids.append(task.id)
                    elif task.status == TaskStatus.IN_PROGRESS:
                        if task.last_heartbeat and task.last_heartbeat < hb_cutoff:
                            orphaned_ids.append(task.id)
                        elif (
                            task.last_heartbeat is None
                            and task.started_at
                            and task.started_at < hb_cutoff
                        ):
                            # Defensive: IN_PROGRESS without any heartbeat shouldn't happen
                            # (claim_task sets last_heartbeat at agent.py:312) but if we hit
                            # it, treat as a post-claim crash. Use scope-aware cutoff so
                            # large/high-effort tasks aren't reaped prematurely.
                            orphaned_ids.append(task.id)
                        elif (
                            task.last_heartbeat is None
                            and task.started_at is None
                            and task.created_at < ten_minutes_ago
                        ):
                            # Fully-degenerate IN_PROGRESS row (no heartbeat, no started_at)
                            # that's been alive for >10 min — data corruption or a path that
                            # forgot to set started_at. Reap as a catch-all.
                            orphaned_ids.append(task.id)
                        elif (
                            task.last_heartbeat
                            and task.last_heartbeat >= hb_cutoff
                            and task.last_tool_use_at is not None
                            and task.last_tool_use_at
                            < _progress_stale_cutoff(task, now)
                            and (
                                task.started_at is None
                                or task.started_at
                                < _progress_stale_cutoff(task, now)
                            )
                        ):
                            # Stalled-but-heartbeating: agent process is alive (fresh
                            # heartbeat) but has made no tool/LLM calls for longer than
                            # the scope-aware progress threshold. Indicates a hung LLM
                            # call, a spinning retry loop, or a wedged tool invocation.
                            #
                            # Cross-attempt stale-tool guard (8c8cf973 incident):
                            # last_tool_use_at carries forward across retries — if
                            # only this field is checked, every retry is born stale
                            # and reaped within seconds (8c8cf973 attempt 3 reaped
                            # 22s after starting because tool_use_at was 11min old
                            # from attempt 1). The started_at check ensures the
                            # current attempt has actually had time to call tools.
                            orphaned_ids.append(task.id)
                        elif (
                            task.last_heartbeat
                            and task.last_heartbeat >= hb_cutoff
                            and task.last_tool_use_at is None
                            and task.started_at
                            and task.started_at
                            < _progress_stale_cutoff(task, now)
                            and (task.context or {}).get("llm_provider")
                            == "anthropic"
                        ):
                            # Stalled-but-heartbeating with zero tool calls ever.
                            # Provider-gated to anthropic only because the agent
                            # wrapper's outputMode='text' path (Codex/Gemini at
                            # agent/src/index.ts:3061,3119) never calls
                            # recordToolUse — only the claude-stream-json parser
                            # does. So last_tool_use_at IS NULL for every
                            # Codex/Gemini task by design, regardless of whether
                            # tools were called. Without this gate the watchdog
                            # reaps healthy non-anthropic work after 8 min.
                            # TODO: parse Codex --json events / Gemini events
                            # to populate tool_trace, then drop the gate.
                            orphaned_ids.append(task.id)
                    elif task.created_at < ten_minutes_ago:
                        orphaned_ids.append(task.id)

                # Step 2: Lock ONLY orphaned rows (heartbeats on healthy tasks are never blocked)
                # expire_all() nukes the identity map so step 2 MUST read fresh from DB.
                # populate_existing=True was tried but doesn't reliably refresh with
                # with_for_update(skip_locked=True) on async sessions.
                orphaned_tasks = []
                if orphaned_ids:
                    session.expire_all()
                    lock_stmt = (
                        select(Task)
                        .where(Task.id.in_(orphaned_ids))
                        .with_for_update(skip_locked=True)
                    )
                    orphaned_tasks = list(
                        (await session.execute(lock_stmt)).scalars().all()
                    )

                    # Track tasks skipped by skip_locked — attempt force-reap after 5 consecutive skips.
                    # The threshold is 5 (not 3) to give more time before force-lock attempts, since
                    # transient lock contention (e.g. a heartbeat write) often resolves within a few cycles.
                    locked_ids = set(orphaned_ids) - {t.id for t in orphaned_tasks}
                    for lid in locked_ids:
                        _reaper_skip_counts[lid] = _reaper_skip_counts.get(lid, 0) + 1
                    for t in orphaned_tasks:
                        _reaper_skip_counts.pop(t.id, None)

                    # Force-lock persistently skipped tasks using skip_locked=True (non-blocking).
                    # A task skipped here stays in _reaper_skip_counts and will be retried next cycle,
                    # so the reaper never stalls waiting on a contested row.
                    force_lock_ids = [
                        lid for lid, count in _reaper_skip_counts.items() if count >= 5
                    ]
                    if force_lock_ids:
                        try:
                            session.expire_all()
                            # Audit C4 (M-C3): the SET LOCAL lock_timeout=60s
                            # below is a defensive no-op as long as the
                            # acquisition uses skip_locked=True — Postgres'
                            # SKIP LOCKED returns immediately for any row
                            # held by another transaction, never blocking, so
                            # lock_timeout never fires on this path. The
                            # statement is kept (a) to document intent for
                            # operators reading logs, and (b) so a future
                            # change that replaces skip_locked with a NOWAIT
                            # or blocking variant inherits the bounded wait
                            # automatically. Reverts on commit/rollback.
                            from sqlalchemy import text as _text

                            await session.execute(
                                _text("SET LOCAL lock_timeout = '60000'")
                            )
                            force_stmt = (
                                select(Task)
                                .where(Task.id.in_(force_lock_ids))
                                .with_for_update(skip_locked=True)
                            )
                            force_locked = list(
                                (await session.execute(force_stmt)).scalars().all()
                            )
                            for t in force_locked:
                                if t.status not in (
                                    TaskStatus.PROVISIONING,
                                    TaskStatus.IN_PROGRESS,
                                ):
                                    continue
                                has_pr = t.id in tasks_with_pr
                                if (
                                    t.last_heartbeat
                                    and t.last_heartbeat
                                    >= _heartbeat_stale_cutoff(
                                        t, now, has_open_pr=has_pr
                                    )
                                ):
                                    continue
                                _reaper_skip_counts.pop(t.id, None)
                                orphaned_tasks.append(t)
                                logger.warning(
                                    f"[REAPER] Force-locked persistently skipped task {t.id}"
                                )
                        except Exception as e:  # noqa: BLE001
                            logger.warning(
                                f"[REAPER] Force-lock failed (lock timeout?): {e}"
                            )
                            # Reset skip counts so force-lock is retried next cycle
                            for lid in force_lock_ids:
                                _reaper_skip_counts.pop(lid, None)

                    # Clean stale entries from skip counts
                    orphaned_id_set = set(orphaned_ids)
                    for tid in list(_reaper_skip_counts.keys()):
                        if tid not in orphaned_id_set:
                            del _reaper_skip_counts[tid]

                    # Re-verify after acquiring lock (heartbeat may have arrived between step 1 and 2)
                    # expire_all() above ensures these objects have fresh DB values.
                    # Mirrors the state machine above — same thresholds, same branching.
                    now = _utcnow()  # Refresh now — time passed during lock acquisition
                    ten_minutes_ago = now - timedelta(minutes=10)
                    fifteen_minutes_ago = now - timedelta(minutes=15)
                    verified = []
                    for t in orphaned_tasks:
                        has_pr = t.id in tasks_with_pr
                        hb_cutoff = _heartbeat_stale_cutoff(t, now, has_open_pr=has_pr)
                        if t.status == TaskStatus.PROVISIONING:
                            if t.started_at and t.started_at < fifteen_minutes_ago:  # noqa: SIM114
                                verified.append(t)
                            elif t.last_heartbeat and t.last_heartbeat < hb_cutoff:  # noqa: SIM114
                                verified.append(t)
                            elif (
                                t.vm_provisioned_at
                                and t.vm_provisioned_at < _preclaim_stale_cutoff(t, now)
                                and t.last_heartbeat is None
                            ):
                                verified.append(t)
                        elif t.status == TaskStatus.IN_PROGRESS:
                            if t.last_heartbeat and t.last_heartbeat < hb_cutoff:  # noqa: SIM114
                                verified.append(t)
                            elif (  # noqa: SIM114
                                t.last_heartbeat is None
                                and t.started_at
                                and t.started_at < hb_cutoff
                            ):
                                verified.append(t)
                            elif (
                                t.last_heartbeat is None
                                and t.started_at is None
                                and t.created_at < ten_minutes_ago
                            ):
                                verified.append(t)
                            elif (
                                t.last_heartbeat
                                and t.last_heartbeat >= hb_cutoff
                                and t.last_tool_use_at is not None
                                and t.last_tool_use_at < _progress_stale_cutoff(t, now)
                                and (
                                    t.started_at is None
                                    or t.started_at < _progress_stale_cutoff(t, now)
                                )
                            ):
                                # Verification mirror; see step-1 cross-attempt
                                # stale-tool guard (8c8cf973 incident).
                                verified.append(t)
                            elif (
                                t.last_heartbeat
                                and t.last_heartbeat >= hb_cutoff
                                and t.last_tool_use_at is None
                                and t.started_at
                                and t.started_at < _progress_stale_cutoff(t, now)
                                and (t.context or {}).get("llm_provider")
                                == "anthropic"
                            ):
                                # Verification-loop mirror of step-1 guard.
                                # Provider-gated to anthropic only because the
                                # agent wrapper's outputMode='text' path
                                # (Codex/Gemini) never calls recordToolUse, so
                                # last_tool_use_at IS NULL by design. See
                                # step-1 branch for the full rationale.
                                verified.append(t)
                        elif t.created_at < ten_minutes_ago:
                            verified.append(t)
                    orphaned_tasks = verified

                # ── RELEASE MULTI-ROW FOR UPDATE BEFORE EXTERNAL I/O LOOP ──
                # Production 2026-05-04 cascade shape: the orphan-loop body
                # below makes 4 external API calls per task — get_vm_status,
                # disable_account_model (with retry+sleep), _capture_serial_for_vm,
                # get_pull_request — each up to 30s, with combined per-task
                # worst-case 30-120s. The multi-row FOR UPDATE acquired at
                # lock_stmt/force_stmt above was previously held until the
                # batch commit at the end of the for loop, blocking heartbeats
                # on EVERY orphan row across the entire batch and tripping
                # the 60s idle_in_transaction_session_timeout under load.
                # Release it now: classification was already done against the
                # ``verified`` list, and each per-task iteration below does its
                # own release-around-IO + skip_locked re-acquire dance for the
                # mutation phase. expire_all() ensures subsequent reads come
                # from fresh DB state.
                #
                # CRITICAL: snapshot IDs BEFORE expire_all. After expire_all,
                # every attribute on every instance is expired — including
                # ``task.id``. Accessing ``task.id`` then triggers a sync
                # attribute __get__ that drives a lazy refresh; on AsyncSession
                # the underlying pool ping uses ``await_()`` which raises
                # MissingGreenlet because the sync attribute access is not
                # inside the AsyncSession's greenlet bridge. Result: the
                # orphan loop crashes mid-iteration on the very first task.
                # Iterate by id instead so no expired-attribute access happens.
                _orphaned_task_ids: list[str] = []
                if orphaned_tasks:
                    _orphaned_task_ids = [t.id for t in orphaned_tasks]
                    await session.commit()
                    session.expire_all()

                # Collect old VM IDs for deletion after commit (can't set attrs on SQLModel)
                _reaper_old_vms: Dict[str, str] = {}
                # Audit 2026-04-25 A8: defer parent re-queue to AFTER session.commit()
                # so the orphan-row FOR UPDATE lock is released first. The previous
                # version called _requeue_parent_task (which clones children + remaps
                # deps + does its own queries) WHILE holding force-locked rows, which
                # starved agent heartbeats with 429s and triggered cascading false
                # orphan detection on the next reaper cycle.
                _deferred_parent_requeues: list[Task] = []

                for task_id in _orphaned_task_ids:
                    # Re-validate status before acting — task may have completed/been cancelled
                    # between the initial query and now
                    fresh_task = await session.get(Task, task_id)
                    if fresh_task is None or fresh_task.status not in (
                        TaskStatus.PROVISIONING,
                        TaskStatus.IN_PROGRESS,
                    ):
                        logger.info(
                            f"[REAPER] Skipping task {task_id} — status transitioned to {fresh_task.status if fresh_task else 'gone'} before reap"
                        )
                        continue
                    task = (
                        fresh_task  # Use fresh data for all subsequent reads and writes
                    )

                    # Determine why the reaper flagged this task. The classification slices
                    # into three cases based on status + which clock expired first.
                    if (
                        fresh_task.status == TaskStatus.PROVISIONING
                        and fresh_task.started_at
                        and fresh_task.started_at < fifteen_minutes_ago
                    ):
                        age = int((now - fresh_task.started_at).total_seconds())
                        reap_reason = (
                            f"Absolute provision timeout — {age}s since dispatch"
                        )
                        reap_reason_code = "absolute_timeout"
                    elif (
                        fresh_task.status == TaskStatus.PROVISIONING
                        and fresh_task.last_heartbeat is None
                    ):
                        # VM created, agent never contacted us. Use vm_provisioned_at if available,
                        # else fall back to started_at for the age display.
                        clock = fresh_task.vm_provisioned_at or fresh_task.started_at
                        age = int((now - clock).total_seconds()) if clock else 0
                        reap_reason = f"Silent pre-claim death — {age}s since VM provisioned, no heartbeat"
                        reap_reason_code = "provision_timeout_pre_claim"
                    elif (
                        fresh_task.last_heartbeat
                        and fresh_task.last_tool_use_at is not None
                        and fresh_task.last_tool_use_at
                        < _progress_stale_cutoff(fresh_task, now)
                        and (
                            fresh_task.started_at is None
                            or fresh_task.started_at
                            < _progress_stale_cutoff(fresh_task, now)
                        )
                        and fresh_task.last_heartbeat
                        >= _heartbeat_stale_cutoff(fresh_task, now)
                    ):
                        # Stalled-but-heartbeating: process alive, no tool progress.
                        # The started_at clause guards against cross-attempt
                        # stale-tool reaps — last_tool_use_at carries forward
                        # across retries; without this, a fresh attempt can be
                        # killed within seconds of starting (8c8cf973 incident).
                        age = int((now - fresh_task.last_tool_use_at).total_seconds())
                        reap_reason = (
                            f"Agent stalled — heartbeat fresh but no tool calls for {age}s "
                            f"(last_tool_use_at={fresh_task.last_tool_use_at.isoformat()})"
                        )
                        reap_reason_code = "agent_stalled"
                    elif (
                        fresh_task.last_heartbeat
                        and fresh_task.last_tool_use_at is None
                        and fresh_task.started_at
                        and fresh_task.started_at
                        < _progress_stale_cutoff(fresh_task, now)
                        and fresh_task.last_heartbeat
                        >= _heartbeat_stale_cutoff(fresh_task, now)
                    ):
                        # Stalled-but-heartbeating with zero tool calls ever
                        # (Codex text-mode pattern). Same classification as
                        # last_tool_use_at-stale; uses started_at as the clock.
                        age = int((now - fresh_task.started_at).total_seconds())
                        reap_reason = (
                            f"Agent stalled — heartbeat fresh but no tool call has occurred in {age}s "
                            f"since started ({fresh_task.started_at.isoformat()})"
                        )
                        reap_reason_code = "agent_stalled"
                    elif fresh_task.last_heartbeat:
                        age = int((now - fresh_task.last_heartbeat).total_seconds())
                        reap_reason = f"No heartbeat for {age}s (last: {fresh_task.last_heartbeat.isoformat()})"
                        # Enrich with tool trace tail for post-mortem analysis
                        tool_trace_tail = _extract_tool_trace_tail(fresh_task)
                        if tool_trace_tail:
                            reap_reason += f" | last tools: {tool_trace_tail}"
                        reap_reason_code = "heartbeat_timeout_post_claim"
                    elif fresh_task.started_at:
                        # IN_PROGRESS without a heartbeat (defensive branch — should be rare).
                        age = int((now - fresh_task.started_at).total_seconds())
                        reap_reason = (
                            f"IN_PROGRESS without heartbeat for {age}s since dispatch"
                        )
                        # Enrich with tool trace tail for post-mortem analysis
                        tool_trace_tail = _extract_tool_trace_tail(fresh_task)
                        if tool_trace_tail:
                            reap_reason += f" | last tools: {tool_trace_tail}"
                        reap_reason_code = "heartbeat_timeout_post_claim"
                    else:
                        age = int((now - fresh_task.created_at).total_seconds())
                        reap_reason = (
                            f"Never started — {age}s since creation, no heartbeat"
                        )
                        reap_reason_code = "never_started"

                    task_type = (fresh_task.first_payload).get("type", "unknown")
                    logger.info(
                        "reaper_task_reaped",
                        extra={
                            "task_id": str(task.id),
                            "task_type": task_type,
                            "reap_reason": reap_reason_code,
                            "vm_instance_id": fresh_task.vm_instance_id,
                        },
                    )

                    record_pipeline_event(
                        reap_reason_code,
                        task_id=str(task.id),
                        vm_id=fresh_task.vm_instance_id,
                        zone=(task.context or {}).get("provision_zone"),
                    )

                    # Capture old VM ID before any overwrite — deletion happens after commit
                    if fresh_task.vm_instance_id:
                        _reaper_old_vms[str(task.id)] = fresh_task.vm_instance_id

                    # Read-only inputs to the I/O block + branch decisions below.
                    # No mutations happen until AFTER the relock further down.
                    acct_id = get_task_llm_account_id(fresh_task)
                    _slot_already_released = bool(
                        (fresh_task.context or {}).get("_slot_released")
                    )

                    # Separate provision failures from agent failures for retry budgeting
                    is_provision_failure = fresh_task.status == TaskStatus.PROVISIONING
                    provision_failures = (task.context or {}).get(
                        "provision_failures", 0
                    )
                    max_provision_retries = _runtime_config["max_provision_retries"]
                    can_retry = False

                    # Rate-limited tasks: requeue without burning retry budget.
                    # Delegate to classify_failure so the reaper path applies the same
                    # Anthropic-529/GitHub-429 guards as the agent-status path, and
                    # matches real Claude rate-limit strings only (RATE_LIMIT_PATTERNS).
                    combined_logs = (
                        ((task.context or {}).get("last_error") or "")
                        + "\n"
                        + (
                            (task.error_logs or "")
                            if hasattr(task, "error_logs")
                            else ""
                        )
                    )
                    is_rate_limited = (
                        classify_failure(task, logs=combined_logs) == "RATE_LIMIT"
                    )

                    # ── EXTERNAL I/O BLOCK (no row lock held) ─────────────────
                    # Production 2026-05-04: this block previously ran with the
                    # multi-row FOR UPDATE on this task held, blocking heartbeats
                    # for 30-120s per task across get_vm_status (GCP) +
                    # disable_account_model (with retry+sleep) + _capture_serial_for_vm
                    # (GCP serial console) + get_pull_request (GitHub). The
                    # multi-row lock is released above, so this whole block runs
                    # unlocked. We re-acquire FOR UPDATE briefly after this block
                    # to apply the resulting mutations.
                    # ──────────────────────────────────────────────────────────

                    # Capture pre-IO snapshot so the relock below can detect peer
                    # mutation during the unlocked window. status + last_heartbeat
                    # are the two fields the heartbeat path writes; an advance in
                    # either signals the agent revived and the reap should be aborted.
                    pre_io_status = fresh_task.status
                    pre_io_last_hb = fresh_task.last_heartbeat

                    # 1. Spot preemption check (GCP API)
                    is_spot_preemption = False
                    prov_zone_for_status: str | None = None
                    io_vm_status: str | None = None
                    io_orch_delete_info: tuple[bool, str] = (False, "")
                    if (
                        getattr(fresh_task, "vm_spot", False)
                        and fresh_task.vm_instance_id
                        and reap_reason_code
                        in (
                            "heartbeat_timeout_post_claim",
                            "provision_timeout_pre_claim",
                            # absolute_timeout: a PROVISIONING task that aged past
                            # the 15-min absolute ceiling before earlier reaper
                            # codes had a chance to classify it (typically because
                            # the reaper itself was lagging — e.g., 2026-05-07
                            # MissingGreenlet crash on last_tool_use_at). Without
                            # this branch, the spot check is skipped and the
                            # account gets blacklisted for what's actually a GCE
                            # capacity issue.
                            "absolute_timeout",
                        )
                    ):
                        prov_zone_for_status = (task.context or {}).get(
                            "provision_zone"
                        ) or get_vm_zone(fresh_task.vm_instance_id)
                        if prov_zone_for_status:
                            try:
                                io_vm_status = await asyncio.to_thread(
                                    get_vm_status,
                                    fresh_task.vm_instance_id,
                                    prov_zone_for_status,
                                )
                                # TERMINATED = preempted by GCP. DELETED = already gone.
                                # DELETED is ambiguous: GCP delete-on-preempt OR orchestrator-
                                # initiated cleanup (agent_completion, terminal_transition,
                                # admin_requeue, etc). Production 2026-05-04: reaper misclassified
                                # orchestrator-deleted VMs as preemptions, inflating
                                # spot_preemption_count for tasks that were really completed
                                # but whose status POST 500'd during a cutover.
                                # Fix: consult the orchestrator-initiated-delete tracker. If the
                                # VM was deleted by us within the last 30min, it's NOT a preemption.
                                from app.main import is_orchestrator_initiated_delete

                                io_orch_delete_info = is_orchestrator_initiated_delete(
                                    fresh_task.vm_instance_id
                                )
                                if io_orch_delete_info[0]:
                                    logger.info(
                                        f"[REAPER] Task {task.id} on SPOT VM {fresh_task.vm_instance_id} "
                                        f"vm_status={io_vm_status} but orchestrator-initiated delete "
                                        f"(reason={io_orch_delete_info[1]}) — NOT classifying as preemption"
                                    )
                                elif io_vm_status in ("TERMINATED", "DELETED"):
                                    is_spot_preemption = True
                                    logger.warning(
                                        f"[REAPER] Task {task.id} on SPOT VM {fresh_task.vm_instance_id} "
                                        f"in {prov_zone_for_status} — vm_status={io_vm_status}, treating "
                                        f"as preemption (skipping attempt_count++)"
                                    )
                            except Exception as e:  # noqa: BLE001
                                logger.debug(
                                    f"[REAPER] get_vm_status failed for {fresh_task.vm_instance_id}: {e}"
                                )

                    # 2. Rate-limited account-disable I/O (called only when is_rate_limited
                    #    AND not is_spot_preemption; mirrors the original elif chain where
                    #    the rate-limited branch was skipped if spot preempt fired first).
                    #    Side-effect-only — disables the account/model in the in-memory pool
                    #    + persists to DB via its own internal session.
                    rl_acct_id: str | None = None
                    rl_model: str | None = None
                    if is_rate_limited and not is_spot_preemption:
                        rl_acct_id = get_task_llm_account_id(task)
                        logger.info(
                            f"[REAPER] Task {task.id} rate-limited (account={rl_acct_id}) — "
                            f"requeuing immediately with account blacklisted"
                        )
                        # Disable the exhausted model on the account so the orchestrator's
                        # blacklist pruning preserves this entry on the next dispatch cycle.
                        rl_model = _extract_task_model(task)
                        if not rl_model:
                            _rl_task_type = (task.first_payload or {}).get(
                                "type", "create"
                            )
                            rl_model = _runtime_config.get("task_type_models", {}).get(
                                _rl_task_type
                            )
                        if rl_acct_id and rl_model:
                            _reaper_ctx_io = task.context or {}
                            _reaper_kwargs_io = {
                                "retry_after_seconds": _reaper_ctx_io.get(
                                    "rate_limit_retry_after_seconds"
                                ),
                                "kind": _reaper_ctx_io.get("rate_limit_kind")
                                or "rate_limit",
                            }
                            try:
                                await disable_account_model(
                                    rl_acct_id, rl_model, **_reaper_kwargs_io
                                )
                            except Exception as _disable_err:  # noqa: BLE001
                                logger.warning(
                                    f"[REAPER] disable_account_model failed for {rl_acct_id}/{rl_model}: "
                                    f"{_disable_err}; retrying in 1s"
                                )
                                await asyncio.sleep(1)
                                try:
                                    await disable_account_model(
                                        rl_acct_id, rl_model, **_reaper_kwargs_io
                                    )
                                except Exception as _disable_err2:  # noqa: BLE001
                                    logger.error(  # noqa: TRY400
                                        f"[REAPER] disable_account_model retry also failed for "
                                        f"{rl_acct_id}/{rl_model}: {_disable_err2}. "
                                        "Account remains active in pool — tight re-selection loop risk."
                                    )

                    # 3. Provision-failure serial capture I/O (GCP serial console).
                    # Run unconditionally on provision failures: the spot-preempt
                    # branch falls through to the provision-failure mutation block
                    # below (which uses io_serial_log), so we need the capture
                    # whenever is_provision_failure regardless of spot/rate-limit
                    # branch. Tiny perf cost when spot/rate fast-paths skip the
                    # mutation block, but avoids re-introducing the held-lock-during-IO
                    # bug as a fallback path.
                    io_serial_log: str | None = None
                    io_prov_zone: str | None = None
                    io_provision_err: str | None = None
                    if is_provision_failure:
                        # Resolve zone (in-memory cache + persisted fallback). The two
                        # values prov_zone and provision_err are stable post-fail; reading
                        # them while unlocked is safe — no peer mutates them.
                        if fresh_task.vm_instance_id:
                            cached = get_vm_zone(fresh_task.vm_instance_id)
                            if cached and cached != "unknown":
                                io_prov_zone = cached
                        if not io_prov_zone:
                            io_prov_zone = (task.context or {}).get("provision_zone")
                        io_provision_err = get_provision_error(
                            fresh_task.vm_instance_id or ""
                        )
                        if io_prov_zone and fresh_task.vm_instance_id:
                            try:
                                io_serial_log = await asyncio.to_thread(
                                    _capture_serial_for_vm,
                                    fresh_task.vm_instance_id,
                                    io_prov_zone,
                                )
                                if io_serial_log:
                                    logger.warning(
                                        f"[REAPER] Serial log for dying agent {fresh_task.vm_instance_id} "
                                        f"(task {task.id}):\n{io_serial_log}"
                                    )
                            except Exception:
                                logger.exception(
                                    f"[REAPER] Failed to capture serial log for VM {fresh_task.vm_instance_id}"
                                )

                    # 4. PR-state lookup I/O (GitHub API)
                    _has_pr_artifact, _pr_num = await _task_has_open_pr(task, session)
                    io_gh_pr_state: str | None = None
                    io_gh_pr_merged: bool = False
                    if _has_pr_artifact and _pr_num:
                        try:
                            from app.routers.agent import get_github_token_for_task
                            from app.github import get_pull_request

                            (
                                _gh_token,
                                _gh_owner,
                                _gh_repo,
                            ) = await get_github_token_for_task(task.id, session)
                            _pr_data = await get_pull_request(
                                _gh_token, _gh_owner, _gh_repo, _pr_num
                            )
                            io_gh_pr_state = _pr_data.get("state")
                            io_gh_pr_merged = bool(_pr_data.get("merged"))
                        except Exception as _gh_exc:  # noqa: BLE001
                            # GH API unreachable: fall back to "open" — handing off
                            # to review pipeline is the safer default.
                            logger.warning(
                                f"[REAPER] PR-state lookup failed for task {task.id} PR #{_pr_num}: "
                                f"{type(_gh_exc).__name__}: {_gh_exc} — assuming open",
                                exc_info=True,
                            )
                            io_gh_pr_state = "open"

                    # ── RE-ACQUIRE FOR UPDATE FOR MUTATIONS ──────────────────
                    # All external I/O above is done. Re-acquire the FOR UPDATE
                    # lock briefly to apply state mutations atomically. If the
                    # row is held by a peer (e.g. concurrent reaper, finalize
                    # path) or status changed during the unlocked window,
                    # _relock_orphan_task returns None and we skip this iter.
                    relocked = await _relock_orphan_task(
                        session,
                        task.id,
                        fresh_task,
                        pre_io_status,
                        pre_io_last_hb,
                    )
                    if relocked is None:
                        # Peer is finalizing this task or agent revived; skip safely.
                        # Don't add to _reaper_old_vms-driven cleanup either — let
                        # the peer (or next reaper cycle) handle it.
                        _reaper_old_vms.pop(str(task.id), None)
                        continue
                    task = relocked
                    fresh_task = relocked

                    # Apply the in-memory account-slot release marker. Re-check
                    # the flag from the FRESH (post-relock) context to avoid a
                    # double-release on the rare case where a peer reaper iteration
                    # set the flag between our pre-IO capture and the relock (would
                    # require last_heartbeat unchanged, since the relock validates
                    # that). The release op itself is also idempotent inside the
                    # in-memory pool, so this is belt-and-suspenders.
                    _slot_now_released = bool(
                        (task.context or {}).get("_slot_released")
                    )
                    if (
                        acct_id
                        and not _slot_already_released
                        and not _slot_now_released
                    ):
                        await release_max_plan_account(acct_id)
                        task.context = {**(task.context or {}), "_slot_released": True}

                    if is_spot_preemption:
                        # Skip attempt_count++ and provision_failures++ — preemption
                        # isn't a code defect or account problem. Requeue cleanly.
                        logger.info(
                            f"[REAPER] Spot preemption — requeuing task {task.id} without burning retry budget"
                        )
                        new_ctx = dict(task.context or {})
                        spot_count = (new_ctx.get("spot_preemption_count") or 0) + 1
                        new_ctx["spot_preemption_count"] = spot_count
                        if is_provision_failure:
                            # Undo the provision_failures increment from the pre-claim path
                            provision_failures -= 1
                            new_ctx["provision_failures"] = provision_failures
                        task.context = new_ctx
                        can_retry = True
                        record_pipeline_event(
                            "spot_preemption",
                            task_id=str(task.id),
                            vm_id=fresh_task.vm_instance_id,
                            zone=prov_zone_for_status,
                        )
                    elif is_rate_limited:
                        # disable_account_model was already invoked (with retry+sleep)
                        # in the unlocked I/O block above; rl_acct_id and rl_model
                        # captured there. Apply the resulting state mutations here.
                        task.status = TaskStatus.QUEUED
                        task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                        task.last_heartbeat = None
                        task.started_at = None
                        task.latest_status_message = (
                            "Rate limited — requeued with exhausted account blacklisted"
                        )
                        # Blacklist the exhausted account; let the orchestrator pick another
                        # from the pool on the next cycle. If all accounts are rate-limited,
                        # the orchestrator handles that case by skipping the task and retrying
                        # next cycle — no need for a task-level retry_after delay.
                        rl_ctx = dict(task.context or {})
                        if rl_acct_id:
                            blacklisted = list(rl_ctx.get("blacklisted_accounts") or [])
                            if rl_acct_id not in blacklisted:
                                blacklisted.append(rl_acct_id)
                                record_pipeline_event(
                                    "account_blacklisted",
                                    task_id=str(task.id),
                                    vm_id=fresh_task.vm_instance_id,
                                    zone=(task.context or {}).get("provision_zone"),
                                    account_id=rl_acct_id,
                                )
                            rl_ctx["blacklisted_accounts"] = blacklisted
                        _copy_analysis_artifacts_to_previous(rl_ctx)
                        rl_ctx = clear_task_llm_runtime_context(rl_ctx)
                        rl_ctx.pop("_slot_released", None)
                        # Audit W1: retry_after_until is a column now.
                        task.retry_after_until = None
                        # Rate-limit is an account-level issue, not a provision failure —
                        # don't carry provision budget into the next dispatch.
                        rl_ctx.pop("provision_failures", None)
                        task.context = rl_ctx
                        session.add(task)
                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": TaskStatus.QUEUED},
                        )
                        continue

                    # Track per-class budget/failure-class for non-provision retries.
                    # Initialized here so both retry and fail paths can reference them.
                    reap_failure_class: str | None = None
                    class_budget: int = _runtime_config["max_retries"]

                    if is_provision_failure:
                        provision_failures += 1
                        # Reuse zone + provision_err + serial_log captured by the
                        # unlocked I/O block above. Read-only locals fall back to
                        # the same fallback chain when the I/O block didn't fire
                        # (e.g. is_provision_failure was True but the row was rate-
                        # limited or spot-preempted, in which case those branches
                        # already returned via `continue` and we never reach here).
                        prov_zone = io_prov_zone
                        if prov_zone is None and fresh_task.vm_instance_id:
                            cached = get_vm_zone(fresh_task.vm_instance_id)
                            if cached and cached != "unknown":
                                prov_zone = cached
                        if not prov_zone:
                            prov_zone = (task.context or {}).get("provision_zone")
                        failed_acct = get_task_llm_account_id(task)

                        # Classify: infra-wide (all zones/quota exhausted) vs task-specific
                        provision_err = (
                            io_provision_err
                            if io_provision_err is not None
                            else get_provision_error(fresh_task.vm_instance_id or "")
                        )
                        err_lower = (
                            (provision_err or "")
                            + " "
                            + (task.latest_status_message or "")
                        ).lower()
                        is_infra = any(
                            p in err_lower for p in INFRA_PROVISION_PATTERNS
                        ) or (not prov_zone and not any_zone_available())

                        # Serial log was captured by the unlocked I/O block above.
                        # We intentionally do NOT re-capture here on failure: doing so
                        # would call _capture_serial_for_vm (5-30s GCP API) while holding
                        # the FOR UPDATE lock, re-introducing the production-2026-05-04
                        # cascade. If the I/O block couldn't capture (zone unresolvable
                        # or transient GCP error), the next reaper cycle will retry from
                        # the unlocked I/O path.
                        serial_log = io_serial_log
                        if prov_zone:
                            # VM was created (GCP API succeeded) but agent never claimed.

                            # Blacklist the account so retry uses a different one.
                            # We maintain two lists:
                            #   - blacklisted_accounts: pruned when model cooldown expires
                            #     (correct for rate-limit blacklists)
                            #   - provision_failed_accounts: sticky for this task's lifetime
                            #     (silent pre-claim death isn't model-cooldown-driven, so
                            #     pruning by _account_available_for_model wrongly reselects
                            #     the broken account — observed on task 812fdbb9 retrying
                            #     account 017d51cb 3x in a row in us-central1-a).
                            blacklisted = list(
                                (task.context or {}).get("blacklisted_accounts") or []
                            )
                            sticky = list(
                                (task.context or {}).get("provision_failed_accounts")
                                or []
                            )
                            if failed_acct and failed_acct not in blacklisted:
                                blacklisted.append(failed_acct)
                                logger.warning(
                                    f"[REAPER] Agent startup failure on {prov_zone} acct={failed_acct[:8]} "
                                    f"— blacklisting account for task {task.id}"
                                )
                                record_pipeline_event(
                                    "account_blacklisted",
                                    task_id=str(task.id),
                                    vm_id=fresh_task.vm_instance_id,
                                    zone=prov_zone,
                                    account_id=failed_acct,
                                )
                            if failed_acct and failed_acct not in sticky:
                                sticky.append(failed_acct)
                            new_ctx = {
                                **(task.context or {}),
                                "blacklisted_accounts": blacklisted or None,
                                "provision_failed_accounts": sticky or None,
                            }
                            if serial_log:
                                new_ctx["last_serial_log"] = serial_log
                            record_zone_failure(
                                prov_zone, reason="AGENT_STARTUP_FAILURE"
                            )
                        else:
                            # No provision_zone — VM may never have been created, or the
                            # fallback chain couldn't resolve one.
                            logger.warning(
                                f"[REAPER] Skipping serial capture for task {task.id}: "
                                f"zone unknown (vm_id={fresh_task.vm_instance_id})"
                            )
                            new_ctx = dict(task.context or {})

                        if is_infra:
                            # Infrastructure-wide failure — don't burn task retry budget
                            provision_failures -= 1  # Undo the increment above
                            infra_count = new_ctx.get("provision_failures_infra", 0) + 1
                            new_ctx["provision_failures_infra"] = infra_count
                            new_ctx["provision_failures"] = provision_failures
                            new_ctx["_infra_wait"] = True
                            task.context = new_ctx
                            can_retry = True
                            logger.warning(
                                f"[REAPER] Task {task.id} infra-wide provision failure (#{infra_count}) — requeuing without burning retry budget"
                            )
                        else:
                            new_ctx["provision_failures"] = provision_failures
                            new_ctx.pop("_infra_wait", None)
                            task.context = new_ctx
                            can_retry = provision_failures < max_provision_retries
                            if not can_retry:
                                logger.error(
                                    f"[REAPER] Task {task.id} provision failed {provision_failures} times — giving up"
                                )
                    else:
                        # Classify failure to select per-class retry budget.
                        # Use agent-reported error logs as the primary signal. If the agent
                        # left no error context (pure heartbeat timeout), fall back to
                        # AGENT_TIMEOUT rather than the default CODE_ERROR — a silent death
                        # is not a code defect.
                        reap_failure_class = classify_failure(task, logs=combined_logs)
                        if (
                            reap_failure_class == "CODE_ERROR"
                            and not combined_logs.strip()
                        ):
                            reap_failure_class = "AGENT_TIMEOUT"
                        task_scope = (task.context or {}).get("scope")
                        class_budget = resolve_retry_budget(
                            reap_failure_class, task_scope
                        )
                        # Detect EXTERNAL_API fast-path: transient 5xx where the dependency
                        # health check shows recovery. These failures skip the attempt_count
                        # increment below (mirrors the RATE_LIMIT fast-path) since the retry
                        # is near-instant and not indicative of a code defect. Applies whether
                        # or not the task already has a PR — a transient 502 after pushing
                        # work should not burn a retry attempt if the dependency recovered.
                        _err_blob = (
                            combined_logs + " " + (task.latest_status_message or "")
                        ).lower()
                        _transient_5xx = any(
                            p in _err_blob for p in _TRANSIENT_5XX_PATTERNS
                        )
                        _gh_consec = _external_health.get(
                            "consecutive_failures", {}
                        ).get("github_api", 0)
                        ext_api_fast_path = (
                            reap_failure_class == "EXTERNAL_API"
                            and _transient_5xx
                            and _gh_consec == 0
                        )
                        can_retry = task.attempt_count < class_budget

                    # Open-PR handoff: if this attempt already pushed a branch
                    # and opened a PR, retrying from scratch would collide with
                    # the existing branch (see agent/src/index.ts:706 stale-
                    # branch guard) and orphan the PR. Hand off to the review
                    # pipeline instead — review/fix iteration + the existing
                    # 6h EVALUATING hard timeout (services.py:807) + the 16-iter
                    # review-fix cap guarantee termination.
                    #
                    # PR state was already fetched in the unlocked I/O block above
                    # (io_gh_pr_state, io_gh_pr_merged) so the GitHub round-trip
                    # doesn't run with FOR UPDATE held. Reuse those values here.
                    _gh_pr_state = io_gh_pr_state
                    _gh_pr_merged = io_gh_pr_merged
                    _pr_actually_open = (
                        _has_pr_artifact and _pr_num and _gh_pr_state == "open"
                    )

                    if (
                        _has_pr_artifact
                        and _pr_num
                        and _gh_pr_state == "closed"
                        and _gh_pr_merged
                    ):
                        # PR merged out-of-band: completion-processing didn't run
                        # (agent died before reporting). Mark the task COMPLETED
                        # so the rollup parent and downstream review/fix tasks
                        # see the merge.
                        logger.info(
                            f"[REAPER] Task {task.id} reaped mid-run; PR #{_pr_num} already merged — "
                            f"marking COMPLETED"
                        )
                        task.status = TaskStatus.COMPLETED
                        task.vm_instance_id = None
                        task.last_heartbeat = None
                        task.latest_status_message = (
                            f"Reaped mid-run; PR #{_pr_num} was merged out-of-band"
                        )
                        task.context = {**(task.context or {}), "pr_merged": True}
                        session.add(task)
                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": task.status},
                        )
                    elif (
                        _has_pr_artifact
                        and _pr_num
                        and _gh_pr_state == "closed"
                        and not _gh_pr_merged
                    ):
                        # PR closed without merging (operator rejection / empty
                        # rollup short-circuit / review-pipeline rejection).
                        # Don't hand off to review — review can't act on a closed
                        # PR. Mark FAILED with the closure marker so Step 5's
                        # resurrection sweep skips it.
                        logger.info(
                            f"[REAPER] Task {task.id} reaped mid-run; PR #{_pr_num} closed without merge — "
                            f"marking FAILED"
                        )
                        task.status = TaskStatus.FAILED
                        task.vm_instance_id = None
                        task.last_heartbeat = None
                        task.latest_status_message = (
                            f"Reaped mid-run; PR #{_pr_num} was closed without merging"
                        )
                        task.context = {
                            **(task.context or {}),
                            "pr_closed_unmerged": True,
                        }
                        session.add(task)
                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": task.status},
                        )
                    elif _pr_actually_open:
                        # Provision failures can't publish a PR (agent never ran),
                        # so class_budget/reap_failure_class are only bound when
                        # the agent-error branch above ran. Guard the format args.
                        _display_class = (
                            "PROVISION_FAILURE"
                            if is_provision_failure
                            else locals().get("reap_failure_class", "UNKNOWN")
                        )
                        _display_budget = locals().get("class_budget", "?")
                        logger.info(
                            f"[REAPER] Task {task.id} reaped mid-run with open PR #{_pr_num}; "
                            f"handing off to review pipeline (was attempt {task.attempt_count}/{_display_budget})"
                        )
                        task.status = TaskStatus.EVALUATING
                        task.vm_instance_id = None
                        task.last_heartbeat = None
                        task.latest_status_message = (
                            f"Reaped mid-run with open PR #{_pr_num}; "
                            f"handed off to review pipeline ({_display_class})"
                        )
                        session.add(task)
                        # Spawn the review task that _process_post_commit would
                        # have spawned if the agent had reported COMPLETED.
                        try:
                            from app.review_pipeline import _spawn_pipeline_task

                            _short_id = str(task.id).replace("-", "")[:8]
                            _task_type = (task.first_payload or {}).get(
                                "type", ""
                            ) or ""
                            _type_prefix_map = {
                                "docs": "docs",
                                "deps": "deps",
                                "migrate": "migrate",
                                "refactor": "refactor",
                                "perf": "task",
                                "qa": "task",
                                "security_audit": "task",
                                "debug": "task",
                                "smoketest": "task",
                            }
                            _branch_prefix = _type_prefix_map.get(_task_type, "task")
                            # Prefer the source task's existing branch (fix tasks
                            # carry the PR's HEAD ref; create/refactor tasks carry
                            # the branch the agent already pushed). Only fall back
                            # to constructing a fresh blueprint/* name when no
                            # branch is recorded — without this, reaped fix tasks
                            # spawn review tasks pointing at non-existent branches
                            # and the review fails on `git clone --branch`.
                            _branch = (task.first_payload or {}).get(
                                "branch"
                            ) or f"blueprint/{_branch_prefix}-{_short_id}"
                            _base_branch = (task.first_payload or {}).get(
                                "base_branch", "main"
                            )
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
                        except Exception as _spawn_exc:  # noqa: BLE001
                            # Review spawn is best-effort from the reaper — if it
                            # fails, reaper Step 3 (stale EVALUATING sweep) will
                            # retry within 15 min, and the 6h hard timeout still
                            # bounds the EVALUATING state. Don't let this crash
                            # the reaper cycle.
                            logger.warning(
                                f"[REAPER] Review spawn failed for reaped-with-PR task {task.id}: "
                                f"{type(_spawn_exc).__name__}: {_spawn_exc}",
                                exc_info=True,
                            )
                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": task.status},
                        )
                    elif can_retry:
                        # Check if this is a research/plan parent with work children —
                        # re-evaluate instead of re-run.
                        task_type = (task.first_payload).get("type", "")
                        is_parent_type = task_type in ("research", "plan")
                        children = []
                        if is_parent_type:
                            children = (
                                (
                                    await session.execute(
                                        select(Task).where(
                                            Task.parent_task_id == task.id
                                        )
                                    )
                                )
                                .scalars()
                                .all()
                            )
                        if children:
                            # Audit A8: queue for post-commit processing; don't
                            # block heartbeats on this task while we clone children.
                            # Mark requeue_in_flight so the next reaper cycle (and
                            # the orchestrator dispatch) skips this task while we work.
                            logger.info(
                                f"[REAPER] Deferring re-evaluation of parent task {task.id} until after lock release"
                            )
                            task.requeue_in_flight = True
                            session.add(task)
                            _deferred_parent_requeues.append(task)
                        else:
                            if not is_provision_failure and not ext_api_fast_path:
                                task.attempt_count += 1
                            if is_provision_failure:
                                retry_label = f"provision {provision_failures}/{max_provision_retries}"
                            else:
                                retry_label = f"attempt {task.attempt_count}/{class_budget} [{reap_failure_class}]"
                            logger.info(
                                f"[REAPER] Retrying task {task.id} ({retry_label})"
                            )
                            reap_ctx = task.context or {}
                            reap_zone = reap_ctx.get("provision_zone", "?")
                            reap_acct = (get_task_llm_account_id(reap_ctx) or "?")[:8]
                            task.latest_status_message = f"Reaped ({retry_label}): {reap_reason} [zone={reap_zone}, acct={reap_acct}]"
                            task.status = TaskStatus.QUEUED
                            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                            task.last_heartbeat = None
                            task.started_at = None
                            reaper_ctx = dict(task.context or {})
                            # Clear account so dispatcher does fresh selection (respects blacklist)
                            reaper_ctx = clear_task_llm_runtime_context(reaper_ctx)
                            reaper_ctx.pop("_slot_released", None)
                            # Enrich with failure diagnosis for retry guidance
                            try:
                                from app.anomaly_detection import (
                                    _error_fingerprint,
                                    _is_recurring_failure_class,
                                )
                                from app.task_lifecycle import _derive_failure_stage
                                import app.shared_state as _anomaly_state

                                _reap_failure_class = (
                                    "PROVISION_FAILURE"
                                    if is_provision_failure
                                    else classify_failure(task, logs=combined_logs)
                                )
                                _fingerprint = _error_fingerprint(reap_reason)
                                # Audit W1: previous_error_fingerprint is a column.
                                task.previous_error_fingerprint = _fingerprint
                                _stage = _derive_failure_stage(
                                    _reap_failure_class, reap_reason
                                )
                                if _stage:
                                    reaper_ctx["_previous_failure_stage"] = _stage
                                _proj_anomalies = (
                                    _anomaly_state.persistent_anomalies.get(
                                        str(task.project_id), {}
                                    )
                                )
                                if _is_recurring_failure_class(
                                    _proj_anomalies, _reap_failure_class
                                ):
                                    reaper_ctx["_previous_is_recurring"] = True
                                # Set base fields so buildRetryContext renders the retry section
                                # (only for agent failures — provision failures ran no agent code)
                                if not is_provision_failure:
                                    reaper_ctx["_attempt_number"] = task.attempt_count
                                    reaper_ctx["_max_attempts"] = class_budget
                                    reaper_ctx["_previous_error"] = (
                                        task.latest_status_message or ""
                                    )[:1500]
                                    reaper_ctx["_previous_failure_class"] = (
                                        _reap_failure_class
                                    )
                                    reaper_ctx["_failure_class_history"] = (
                                        append_failure_class_history(
                                            reaper_ctx.get("_failure_class_history"),
                                            _reap_failure_class,
                                        )
                                    )
                                    if _reap_failure_class == "CODE_ERROR":
                                        reaper_ctx["_previous_error_summary"] = (
                                            task.latest_status_message or ""
                                        )[:500]
                                    _copy_analysis_artifacts_to_previous(reaper_ctx)
                            except Exception:
                                logger.exception(
                                    "[REAPER] Failed to enrich retry context for task %s",
                                    task.id,
                                )
                            if not is_provision_failure:
                                try:
                                    from app.task_lifecycle import (
                                        _build_retry_prompt_suffix,
                                        merge_retry_prompt_suffix,
                                    )
                                    from sqlalchemy.orm.attributes import flag_modified

                                    _task_scope = (
                                        reaper_ctx.get("scope")
                                        or (task.first_payload or {}).get("scope")
                                        or "small"
                                    )
                                    _prior_error = (
                                        (task.context or {}).get("last_error")
                                        or reaper_ctx.get("_previous_error")
                                        or ""
                                    )
                                    _extra_guidance, _scope_override = (
                                        _build_retry_prompt_suffix(
                                            reap_failure_class,
                                            _prior_error,
                                            task.attempt_count,
                                            _task_scope,
                                            reaper_ctx,
                                        )
                                    )
                                    if _scope_override:
                                        reaper_ctx["scope"] = _scope_override
                                        reaper_ctx["_scope_reduced"] = True
                                    if _extra_guidance and task.payloads:
                                        _new_payloads = list(task.payloads)
                                        if isinstance(_new_payloads[0], dict):
                                            _p0 = dict(_new_payloads[0])
                                            _existing_suffix = (
                                                _p0.get("prompt_suffix", "") or ""
                                            )
                                            _p0["prompt_suffix"] = (
                                                merge_retry_prompt_suffix(
                                                    _extra_guidance,
                                                    _existing_suffix,
                                                    reaper_ctx,
                                                )
                                            )
                                            _new_payloads[0] = _p0
                                            task.payloads = _new_payloads
                                            flag_modified(task, "payloads")
                                except Exception:
                                    logger.exception(
                                        "[REAPER] Failed to inject retry guidance for task %s",
                                        task.id,
                                    )
                            if is_provision_failure and (task.context or {}).get(
                                "_infra_wait"
                            ):
                                # Infra-wide failure: align with zone recovery
                                recovery = soonest_zone_recovery()
                                if recovery:
                                    wait_sec = max(
                                        60,
                                        (
                                            recovery - datetime.now(timezone.utc)
                                        ).total_seconds()
                                        + 60,
                                    )
                                    delay_minutes = min(wait_sec / 60, 30)
                                else:
                                    delay_minutes = 10 * random.uniform(0.75, 1.25)
                            elif is_provision_failure:
                                delay_minutes = 5 * random.uniform(
                                    0.75, 1.25
                                )  # 3.75-6.25 minutes
                            elif reap_failure_class == "EXTERNAL_API":
                                # Adaptive delay: transient HTTP 5xx typically resolves in 1-3
                                # minutes, so don't wait 5+ minutes on first retry. Sustained
                                # outages fall through to [5,15,30,60]. Non-transient errors
                                # (DNS, TLS, context_length_exceeded) indicate deeper problems
                                # and always use the longer sequence.
                                _err_blob = (
                                    combined_logs
                                    + " "
                                    + (task.latest_status_message or "")
                                ).lower()
                                _transient_5xx = any(
                                    p in _err_blob for p in _TRANSIENT_5XX_PATTERNS
                                )
                                _gh_consec = _external_health.get(
                                    "consecutive_failures", {}
                                ).get("github_api", 0)
                                if _transient_5xx and _gh_consec == 0:
                                    # Dependency already recovered — requeue immediately.
                                    delay_minutes = 0
                                elif _transient_5xx and task.attempt_count == 1:
                                    # First-attempt short delay for transient 5xx.
                                    delay_minutes = 1.5 * random.uniform(0.75, 1.25)
                                elif _transient_5xx:
                                    # Subsequent retries for transient 5xx: [5, 15, 30].
                                    _t5xx_delays = [5, 15, 30]
                                    _idx = min(
                                        task.attempt_count - 2, len(_t5xx_delays) - 1
                                    )
                                    delay_minutes = _t5xx_delays[_idx] * random.uniform(
                                        0.75, 1.25
                                    )
                                else:
                                    # Non-transient (DNS, TLS, context_length_exceeded) — use
                                    # the longer sequence since these indicate deeper problems.
                                    _ext_api_delays = [5, 15, 30, 60]
                                    _attempt_idx = min(
                                        task.attempt_count - 1, len(_ext_api_delays) - 1
                                    )
                                    delay_minutes = _ext_api_delays[
                                        _attempt_idx
                                    ] * random.uniform(0.75, 1.25)
                            else:
                                # Exponential backoff for agent failures: 1, 2, 4, 8 min capped at 15
                                delay_minutes = min(
                                    2 ** (task.attempt_count - 1), 15
                                ) * random.uniform(0.75, 1.25)
                            task.context = reaper_ctx
                            set_retry_after(task, delay_minutes)
                            session.add(task)

                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {
                                "task_id": str(task.id),
                                "status": task.status,
                                "attempt_count": task.attempt_count,
                            },
                        )
                    else:
                        if is_provision_failure:
                            fail_reason = f"provision failed {provision_failures} times"
                        else:
                            fail_reason = f"failed after {class_budget} retries ({reap_failure_class})"
                        logger.error(f"[REAPER] Task {task.id} {fail_reason}.")
                        task.status = TaskStatus.FAILED
                        if is_provision_failure:
                            ctx = task.context or {}
                            provision_zone = ctx.get("provision_zone", "unknown zone")
                            blacklisted_accounts = (
                                ctx.get("blacklisted_accounts", []) or []
                            )
                            sticky_accounts = (
                                ctx.get("provision_failed_accounts", []) or []
                            )
                            all_blocked = list(
                                {*blacklisted_accounts, *sticky_accounts}
                            )
                            acct_id = get_task_llm_account_id(ctx) or "no account"
                            gcp_error = provision_err or "no GCP error captured"
                            gcp_error_short = gcp_error[:200]
                            # Distinguish: VM created but agent died vs. GCP API failure
                            if provision_zone and provision_zone != "unknown zone":
                                label = "Agent startup failed"
                                detail = f"VM created in {provision_zone} but agent never claimed task"
                                if all_blocked:
                                    detail += (
                                        f" (blacklisted {len(all_blocked)} account(s))"
                                    )
                            else:
                                label = "Provision failed"
                                detail = f"GCP error: {gcp_error_short}"
                            task.latest_status_message = (
                                f"{label} {provision_failures}x — "
                                f"zone: {provision_zone}, acct: {acct_id}. "
                                f"{detail}. "
                                f"Last reap: {reap_reason}"
                            )
                        else:
                            task.latest_status_message = (
                                f"{fail_reason.capitalize()} — last reap: {reap_reason}"
                            )
                        extra_log_parts = [
                            (task.context or {}).get("last_error", ""),
                            (task.context or {}).get("last_serial_log", ""),
                        ]
                        if is_provision_failure:
                            extra_log_parts.append(provision_err or "")
                        extra_logs = " ".join(filter(None, extra_log_parts))
                        from app.failure_classification import (
                            classify_failure_with_confidence,
                        )

                        classification = classify_failure_with_confidence(
                            task, logs=extra_logs or None
                        )
                        failure_class = classification.failure_class
                        if reap_reason_code == "agent_stalled":
                            failure_class = "agent_stalled"
                        persist_failure_category(
                            task,
                            failure_class,
                            logs=extra_logs or None,
                            confidence=classification.confidence,
                        )
                        record_task_result(
                            success=False,
                            failure_class=failure_class,
                            task_id=str(task.id),
                            task_type=task.first_payload.get("type", ""),
                        )
                        session.add(task)
                        await _maybe_create_failure_alert(task, session)

                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": TaskStatus.FAILED},
                        )

                if orphaned_tasks:
                    await session.commit()

                    # Audit A8: now that the orphan-row lock is released, do the
                    # slow parent-requeue work (child clone + dep remap). Heartbeats
                    # on the parent task will go through fine since we only set
                    # requeue_in_flight (a marker), not a row lock.
                    for parent in _deferred_parent_requeues:
                        try:
                            await _requeue_parent_task(parent, session)
                        except Exception as e:  # noqa: PERF203 — per-attempt try in deferred-requeue loop
                            logger.error(
                                f"[REAPER] Deferred parent requeue failed for {parent.id}: {e}",
                                exc_info=True,
                            )
                        finally:
                            # Always clear the in-flight marker so subsequent reaper
                            # cycles can re-evaluate if requeue itself failed.
                            parent.requeue_in_flight = False
                            session.add(parent)
                    if _deferred_parent_requeues:
                        await session.commit()

                    # Delete old VMs after commit (IDs captured before overwrite)
                    for old_vm in _reaper_old_vms.values():
                        await _ensure_vm_deleted(old_vm, reason="reaper")

                    # Trigger rollup checks for failed tasks with parents (after commit)
                    for task in orphaned_tasks:
                        if task.status == TaskStatus.FAILED and task.parent_task_id:
                            try:
                                await _check_and_trigger_rollup(task, session)
                            except Exception as e:
                                logger.error(
                                    f"[REAPER] Rollup check failed for task {task.id}: {e}",
                                    exc_info=True,
                                )

                    # Immediately unblock dependents of terminally failed tasks
                    for task in orphaned_tasks:
                        if task.status == TaskStatus.FAILED:
                            try:
                                async with AsyncSessionLocal() as dep_session:
                                    dep_stmt = select(Task).where(
                                        Task.project_id == task.project_id,
                                        Task.status == TaskStatus.QUEUED,
                                        Task.depends_on.contains([str(task.id)]),
                                    )
                                    dep_result = await dep_session.execute(dep_stmt)
                                    waiting_tasks = dep_result.scalars().all()
                                    count = 0
                                    for wt in waiting_tasks:
                                        wt.depends_on = [
                                            d
                                            for d in (wt.depends_on or [])
                                            if d != str(task.id)
                                        ]
                                        dep_session.add(wt)
                                        count += 1
                                    if count:
                                        await dep_session.commit()
                                        logger.info(
                                            f"[PIPELINE] Cleared terminal dependency {task.id} from {count} waiting tasks"
                                        )
                            except Exception as e:  # noqa: BLE001
                                logger.warning(
                                    f"[PIPELINE] Failed to unblock dependents of {task.id}: {e}",
                                    exc_info=True,
                                )

                    # Close open PRs owned by terminally-failed fix tasks. When a
                    # fix exhausts retries, its PR stays OPEN on GitHub, and
                    # sibling tasks whose rollup waits on open PRs targeting the
                    # same research branch block forever. Closing the dead PR
                    # removes it from the sibling rollup-wait gate.
                    for task in orphaned_tasks:
                        if task.status != TaskStatus.FAILED:
                            continue
                        payload = task.first_payload if task.payloads else {}
                        if payload.get("type") != "fix":
                            continue
                        pr_number = (task.context or {}).get("review_loop", {}).get(
                            "pr_number"
                        ) or payload.get("pr_number")
                        if not pr_number:
                            continue
                        # Skip closing rollup PRs (branch=research/*). A rollup PR
                        # represents many tasks' worth of merged work; closing it
                        # discards the whole body. Better to leave the rollup
                        # open for human follow-up than to throw away N children's
                        # successfully-merged contributions for a single fix task
                        # that exhausted retries.
                        branch = payload.get("branch", "")
                        if branch.startswith("research/"):
                            logger.warning(
                                f"[REAPER] Skip closing rollup PR #{pr_number} "
                                f"(branch={branch}, fix task {task.id}) — "
                                f"closing would discard the rollup body"
                            )
                            continue
                        try:
                            from app.routers.agent import get_github_token_for_task
                            from app.github import close_pull_request, create_pr_comment

                            gh_token, owner, repo = await get_github_token_for_task(
                                task.id, session
                            )
                            reason = (
                                task.latest_status_message
                                or "fix task reached terminal failure"
                            )
                            try:
                                await create_pr_comment(
                                    gh_token,
                                    owner,
                                    repo,
                                    int(pr_number),
                                    f":warning: Abandoning this PR — fix task `{str(task.id)[:8]}` reached terminal failure.\n\n"
                                    f"{reason[:400]}\n\n"
                                    f"Closing so sibling rollups aren't blocked waiting for this branch.",
                                )
                            except Exception as ce:  # noqa: BLE001
                                logger.warning(
                                    f"[PIPELINE] Failed to comment on dead PR #{pr_number}: {ce}"
                                )
                            closed = await close_pull_request(
                                gh_token,
                                owner,
                                repo,
                                int(pr_number),
                                project_id=str(task.project_id),
                            )
                            if closed:
                                logger.info(
                                    f"[PIPELINE] Closed PR #{pr_number} after fix task {task.id} terminal-failed"
                                )
                            else:
                                logger.warning(
                                    f"[PIPELINE] close_pull_request returned False for PR #{pr_number}"
                                )
                        except Exception as e:  # noqa: BLE001
                            logger.warning(
                                f"[PIPELINE] Failed to close PR for failed fix task {task.id}: {e}",
                                exc_info=True,
                            )

                # Step 3: Sweep EVALUATING parents with stale unmerged children.
                # When the initial review spawn after a child completes fails silently,
                # no event re-triggers _check_and_trigger_rollup.  This sweep catches
                # those stuck parents and lets the stale-PR recovery path respawn reviews.
                #
                # Phase B4 (H-P1): batch-fetch all children for ALL evaluating parents
                # in a single ``IN (...)`` query (mirrors the Step 4 pattern at the
                # batched fetch below). The previous code fired N queries — one per
                # parent — to find a single COMPLETED child for the rollup trigger.
                # ``load_only`` keeps each row narrow but MUST include ``status_log``
                # because the ``Task.latest_status_message``/``Task.status`` event
                # listeners (models.py ~360) read status_log on every set; under
                # async sessions a deferred-column fetch raises MissingGreenlet.
                _children_by_parent: Dict[uuid.UUID, list] = {}
                # Plain-tuple snapshot of (id, status, context) per child, captured
                # at SELECT time. Step 4 iterates this rather than touching live
                # ORM children: ``_check_and_trigger_rollup`` in Step 3 may commit
                # or trigger a session rollback (via inner DB error), which under
                # SQLAlchemy expires every loaded instance — a subsequent
                # ``c.status`` read fires a sync lazy-load that can't acquire a
                # connection if the underlying asyncpg socket is dead, crashing
                # the entire reaper cycle. Snapshotting decouples Step 4 from
                # session state. (Production 2026-05-08: 26x crashes at
                # reaper.py:2407 over a 45 min Cloud SQL instability window.)
                _children_snapshot_by_parent: Dict[uuid.UUID, list[tuple]] = {}
                evaluating_parents = []
                try:
                    stale_cutoff = now - timedelta(minutes=15)
                    evaluating_parents = (
                        (
                            await session.execute(
                                select(Task)
                                .options(
                                    load_only(
                                        Task.id,
                                        Task.status,
                                        Task.updated_at,
                                        Task.context,
                                        Task.payloads,
                                        Task.project_id,
                                        Task.parent_task_id,
                                        Task.latest_status_message,
                                        Task.status_log,
                                        Task.attempt_count,
                                        Task.created_at,
                                    )
                                )
                                .where(
                                    Task.status == TaskStatus.EVALUATING,
                                    Task.updated_at < stale_cutoff,
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if evaluating_parents:
                        _parent_ids = [p.id for p in evaluating_parents]
                        _all_children = (
                            (
                                await session.execute(
                                    select(Task)
                                    .options(
                                        load_only(
                                            Task.id,
                                            Task.status,
                                            Task.parent_task_id,
                                            Task.context,
                                            Task.status_log,
                                            Task.latest_status_message,
                                            Task.payloads,
                                        )
                                    )
                                    .where(Task.parent_task_id.in_(_parent_ids))
                                )
                            )
                            .scalars()
                            .all()
                        )
                        for _c in _all_children:
                            _children_by_parent.setdefault(
                                _c.parent_task_id, []
                            ).append(_c)
                            _children_snapshot_by_parent.setdefault(
                                _c.parent_task_id, []
                            ).append((_c.id, _c.status, _c.context or {}))
                    for parent in evaluating_parents:
                        # Find a completed child to use as the trigger for _check_and_trigger_rollup
                        child = next(
                            (
                                c
                                for c in _children_by_parent.get(parent.id, [])
                                if c.status == TaskStatus.COMPLETED
                            ),
                            None,
                        )
                        if child:
                            try:
                                await _check_and_trigger_rollup(child, session)
                            except Exception as e:  # noqa: BLE001
                                logger.warning(
                                    f"[REAPER] Evaluating sweep rollup failed for parent {parent.id}: {e}",
                                    exc_info=True,
                                )
                    if evaluating_parents:
                        logger.info(
                            f"[REAPER] Swept {len(evaluating_parents)} stale EVALUATING parents"
                        )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[REAPER] Evaluating parent sweep error: {e}", exc_info=True
                    )

                # Step 4: Rescue EVALUATING parents whose children ALL terminated without
                # any completing successfully.  These parents have no COMPLETED child to
                # trigger _check_and_trigger_rollup, so they stay EVALUATING forever.
                # Iterates the plain-tuple snapshot from Step 3 — see the comment
                # at ``_children_snapshot_by_parent`` for why we don't touch
                # live ORM children here.
                try:
                    _terminal = {
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                        TaskStatus.CANCELLED,
                    }
                    rescued = 0
                    for parent in evaluating_parents:
                        children_snap = _children_snapshot_by_parent.get(parent.id, [])
                        if not children_snap:
                            continue
                        if any(status not in _terminal for _, status, _ in children_snap):
                            # At least one child still running — not safe to rescue yet.
                            continue
                        if any(status == TaskStatus.COMPLETED for _, status, _ in children_snap):
                            # Step 3 handles this; skip.
                            continue
                        # All children are terminal and none completed — attempt re-evaluation
                        # via _requeue_parent_task which clones failed children with retry
                        # guidance (up to MAX_PARENT_REQUEUES attempts).  If attempts are
                        # exhausted it marks the parent FAILED and fires a failure alert.
                        failed_children = [
                            (cid, ctx) for cid, status, ctx in children_snap
                            if status == TaskStatus.FAILED
                        ]
                        cancelled_children = [
                            cid for cid, status, _ in children_snap
                            if status == TaskStatus.CANCELLED
                        ]
                        # Build a diagnostic message including child failure classes.
                        parts = []
                        if failed_children:
                            classes = [
                                (ctx or {}).get("failure_category") or "unknown"
                                for _, ctx in failed_children
                            ]
                            class_summary = ", ".join(f"{cls}" for cls in classes)
                            parts.append(
                                f"{len(failed_children)} failed ({class_summary})"
                            )
                        if cancelled_children:
                            parts.append(f"{len(cancelled_children)} cancelled")
                        diag = (
                            "; ".join(parts) if parts else f"{len(children_snap)} terminal"
                        )
                        parent.latest_status_message = (
                            f"All child tasks terminated without success: {diag}"
                        )
                        session.add(parent)
                        await _requeue_parent_task(parent, session)
                        rescued += 1
                        logger.warning(
                            f"[REAPER] Rescued stuck EVALUATING parent {parent.id}: {diag}"
                        )
                    if rescued:
                        await session.commit()
                        logger.info(
                            f"[REAPER] Rescued {rescued} all-children-failed EVALUATING parents"
                        )
                except Exception as e:
                    logger.error(
                        f"[REAPER] All-children-failed rescue error: {e}", exc_info=True
                    )

                # Step 5: Infrastructure recovery sweep — resurrect recently-failed tasks
                # that exhausted their retry budget due to transient infrastructure failures
                # once the specific root cause has resolved.  Conservative: max 1 resurrection
                # per task, only for confirmed infra failure classes, only when infra is healthy.
                try:
                    from app.max_plan_pool import (
                        _all_accounts,
                        _account_available_for_model,
                    )

                    _infra_lookback = now - timedelta(hours=2)
                    # Exclude just-reaped tasks so Step 2 failures aren't immediately
                    # resurrected in the same cycle, defeating the retry budget.
                    _just_reaped_ids = {t.id for t in orphaned_tasks}
                    _infra_query = select(Task).where(
                        Task.status == TaskStatus.FAILED,
                        Task.updated_at >= _infra_lookback,
                    )
                    if _just_reaped_ids:
                        _infra_query = _infra_query.where(
                            Task.id.notin_(_just_reaped_ids)
                        )
                    _infra_failed = (
                        (await session.execute(_infra_query)).scalars().all()
                    )

                    # Parent tasks (those with children) must not be resurrected as leaf
                    # dispatches — they only fail via _requeue_parent_task after exhausting
                    # the re-evaluation budget, and resurrecting them back to QUEUED would
                    # cause the orchestrator to dispatch a research/plan parent as if it
                    # were a work task. Parents recover through Step 4 (which re-clones
                    # failed children), not through Step 5. Batch-fetch parent IDs that
                    # have any children to skip them here.
                    _parent_ids_with_children: set[uuid.UUID] = set()
                    if _infra_failed:
                        _failed_ids = [_t.id for _t in _infra_failed]
                        _child_rows = (
                            (
                                await session.execute(
                                    select(Task.parent_task_id)
                                    .where(Task.parent_task_id.in_(_failed_ids))
                                    .distinct()
                                )
                            )
                            .scalars()
                            .all()
                        )
                        _parent_ids_with_children = {
                            pid for pid in _child_rows if pid is not None
                        }

                    _resurrected = 0
                    for _t in _infra_failed:
                        _ctx = _t.context or {}
                        # Only resurrect infra-class failures, and only once per task.
                        _fail_cat = _ctx.get("failure_category", "")
                        if _fail_cat not in INFRA_CLASSES:
                            continue
                        if _ctx.get("_resurrection_count", 0) >= 1:
                            continue
                        # Skip terminal PR-closure outcomes. classify_failure routes
                        # "closed without merging" / empty-rollup closures into
                        # EXTERNAL_API (it shares the merge-blocking GitHub bank),
                        # but operator rejection and empty-rollup short-circuit are
                        # not infrastructure failures — re-running produces the same
                        # rejected/empty PR. Mirrors the closure markers documented
                        # in services._pr_possibly_open.
                        if (
                            _ctx.get("pr_closed_unmerged") is True
                            or _ctx.get("pr_closed_empty") is True
                            or _ctx.get("pr_rejected") is True
                        ):
                            continue
                        # Skip parents — they must re-evaluate via _requeue_parent_task, not
                        # resurrect as leaf dispatches.
                        if _t.id in _parent_ids_with_children:
                            continue

                        # Check that the specific infrastructure issue has resolved.
                        _infra_ready = False
                        if _fail_cat == "PROVISION_FAILURE":
                            _infra_ready = any_zone_available()
                        elif _fail_cat == "RATE_LIMIT":
                            # Need at least one available account for this task's model
                            # that isn't already blacklisted by this task.
                            _task_model = _extract_task_model(_t)
                            _task_type = (_t.first_payload or {}).get("type", "create")
                            if not _task_model:
                                _task_model = _runtime_config.get(
                                    "task_type_models", {}
                                ).get(_task_type)
                            _blacklisted = set(_ctx.get("blacklisted_accounts") or [])
                            _accounts = _all_accounts()
                            _infra_ready = any(
                                acct.get("id") not in _blacklisted
                                and _account_available_for_model(acct, _task_model)
                                for acct in _accounts
                            )
                        elif _fail_cat == "EXTERNAL_API":
                            _infra_ready = _external_health.get(
                                "github_api", False
                            ) and _external_health.get("dispatch_api", False)
                        elif _fail_cat == "AGENT_TIMEOUT":
                            # Process crashes and heartbeat timeouts are transient per-VM
                            # issues. OOM kills are NOT transient — the task scope hasn't
                            # changed and it will OOM again on the same VM type. Skip
                            # resurrection for OOM; let scope reduction handle it.
                            if _ctx.get("timeout_subtype") == "oom":
                                continue
                            _infra_ready = any_zone_available()

                        if not _infra_ready:
                            continue

                        # Resurrect: reset to QUEUED with a clean slate.
                        logger.info(
                            f"[REAPER] Resurrecting infra-failed task {_t.id} "
                            f"(failure_category={_fail_cat}) — infrastructure recovered"
                        )
                        _new_ctx = dict(_ctx)
                        _new_ctx["_resurrection_count"] = (
                            _ctx.get("_resurrection_count", 0) + 1
                        )
                        _new_ctx["_resurrected_from"] = _fail_cat
                        # Clear all infra retry state so the task runs like a fresh dispatch.
                        _new_ctx.pop("provision_failures", None)
                        _new_ctx.pop("provision_failures_infra", None)
                        _new_ctx.pop("_infra_wait", None)
                        _new_ctx.pop("blacklisted_accounts", None)
                        _new_ctx.pop("provision_failed_accounts", None)
                        _new_ctx = clear_task_llm_runtime_context(_new_ctx)
                        _new_ctx.pop("_slot_released", None)
                        _t.retry_after_until = None  # audit W1: column source of truth
                        _new_ctx.pop("failure_category", None)
                        # Clear EXTERNAL_API retry counter: a resurrected task that hits another
                        # API error would otherwise see stale _api_failure_retries > 5 and
                        # bypass the EXTERNAL_API-specific backoff, falling through to the
                        # general retry path with a depleted budget.
                        _new_ctx.pop("_api_failure_retries", None)
                        _t.context = _new_ctx
                        _t.attempt_count = 0
                        _t.status = TaskStatus.QUEUED
                        _t.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                        _t.last_heartbeat = None
                        _t.started_at = None
                        _t.latest_status_message = (
                            f"Auto-resurrected after infrastructure recovery "
                            f"(was: {_fail_cat})"
                        )
                        session.add(_t)

                        # INFO alert so the user knows recovery happened.
                        try:
                            from app.models import Alert, AlertSeverity

                            _task_type_label = (_t.first_payload or {}).get(
                                "type", "unknown"
                            )
                            _res_alert = Alert(
                                project_id=_t.project_id,
                                severity=AlertSeverity.INFO,
                                title=f"Task auto-resurrected: {_t.title or str(_t.id)[:8]}",
                                message=(
                                    f"Task type: {_task_type_label}. "
                                    f"Infrastructure failure ({_fail_cat}) resolved — "
                                    f"task re-queued automatically."
                                ),
                                task_id=_t.id,
                            )
                            session.add(_res_alert)
                            await broadcast_to_project(
                                str(_t.project_id),
                                "alert_created",
                                {
                                    "alert_id": str(_res_alert.id),
                                    "severity": "INFO",
                                    "title": _res_alert.title,
                                },
                            )
                        except Exception as _alert_err:  # noqa: BLE001
                            logger.warning(
                                f"[REAPER] Resurrection alert failed for {_t.id}: {_alert_err}"
                            )

                        _resurrected += 1

                    if _resurrected:
                        await session.commit()
                        logger.info(
                            f"[REAPER] Resurrected {_resurrected} infra-failed task(s)"
                        )

                except Exception as e:
                    logger.error(
                        f"[REAPER] Infra recovery sweep error: {e}", exc_info=True
                    )

                # Step 5a: Clear infra holds on directions whose root-cause signal has recovered.
                # Mirrors the signal checks in Step 5 but applies to direction-level holds
                # in direction_failure_streaks rather than individual task resurrection.
                try:
                    from app.max_plan_pool import (
                        _all_accounts,
                        _account_available_for_model,
                    )
                    from app.shared_state import (
                        direction_failure_streaks,
                        clear_direction_hold,
                    )

                    _cleared_holds = 0
                    for _did, _streak in list(direction_failure_streaks.items()):
                        if (
                            not _streak.get("held")
                            or _streak.get("held_reason") != "infra"
                        ):
                            continue
                        _fc = (
                            _streak.get("held_failure_class")
                            or _streak.get("last_failure_class")
                            or ""
                        )
                        _ready = False
                        if (
                            _fc == "PROVISION_FAILURE"
                            or _fc == "AGENT_TIMEOUT"
                            or _fc == "SCOPE_OVERFLOW"
                        ):
                            _ready = any_zone_available()
                        elif _fc == "RATE_LIMIT":
                            _ready = any(
                                _account_available_for_model(acct, None)
                                for acct in _all_accounts()
                            )
                        elif _fc == "EXTERNAL_API":
                            _ready = _external_health.get(
                                "github_api", False
                            ) and _external_health.get("dispatch_api", False)
                        if _ready:
                            clear_direction_hold(_did)
                            _cleared_holds += 1
                            logger.info(
                                f"[REAPER] Cleared infra hold on direction {_did} "
                                f"(signal {_fc} recovered)"
                            )
                    if _cleared_holds:
                        logger.info(
                            f"[REAPER] Cleared {_cleared_holds} infra direction hold(s)"
                        )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Infra direction hold recovery error: {e}",
                        exc_info=True,
                    )

                # Step 5c: Clear holds on directions that are no longer live.
                # When a direction is auto-retired/completed/rejected/expired
                # while a task is held against it, the in-memory streak entry
                # keeps the hold set forever — the 4-hour timeout is the only
                # escape. That strands the task behind a direction that no
                # longer exists in any project's directions list.
                try:
                    await _sweep_orphan_direction_holds(session)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Orphan direction hold sweep error: {e}",
                        exc_info=True,
                    )

                # Step 5d: Prune idle entries from long-lived in-memory state
                # dicts. direction_failure_streaks / circuit_breakers /
                # merge_error_occurrences / autonomous_failure_counts /
                # user_debug_spawn_times all accumulate keys monotonically
                # over an instance's lifetime. On Cloud Run instances that
                # stay up for days this contributes to 4GB OOMs.
                try:
                    from app.shared_state import sweep_stale_in_memory_state

                    _mem_pruned = sweep_stale_in_memory_state()
                    if any(_mem_pruned.values()):
                        logger.info(
                            f"[REAPER] Pruned idle in-memory state: {_mem_pruned}"
                        )
                except Exception as e:
                    logger.error(
                        f"[REAPER] In-memory state sweep error: {e}", exc_info=True
                    )

                # Step 5b: Clear stale or malformed retry_after on QUEUED tasks.
                # A far-future or unparseable value permanently blocks dispatch
                # (orchestrator skips tasks where retry_after > now).
                try:
                    # Audit R3-W2 (H7): cap per-tick fetch at _BATCH rows.
                    # Audit R4-W2 (M2): filter ``retry_after_until IS NOT NULL``
                    # at SQL — without this, the LIMIT _BATCH budget was wasted
                    # iterating QUEUED rows that never had a retry_after set
                    # (the common case), starving the actual stale-far-future
                    # candidates the sweep was meant to catch.
                    _retry_cutoff = now + timedelta(minutes=RETRY_AFTER_MAX_MINUTES)
                    _queued_with_retry = (
                        (
                            await session.execute(
                                select(Task)
                                .where(
                                    Task.status == TaskStatus.QUEUED,
                                    Task.retry_after_until.is_not(None),
                                )
                                .limit(_BATCH)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if len(_queued_with_retry) >= _BATCH:
                        logger.warning(
                            "[REAPER] retry_after sweep hit cap=%d rows — result truncated; "
                            "follow-up: chunk-paginate",
                            _BATCH,
                        )
                    _cleared = 0
                    for _qt in _queued_with_retry:
                        # Audit W1: retry_after_until is a column now.
                        if _qt.retry_after_until is None:
                            continue
                        if _qt.retry_after_until > _retry_cutoff:
                            logger.warning(
                                f"[REAPER] Task {_qt.id}: clearing far-future retry_after={_qt.retry_after_until.isoformat()} (>{RETRY_AFTER_MAX_MINUTES}min)"
                            )
                        else:
                            continue
                        _qt.retry_after_until = None
                        _new_ctx = dict(_qt.context or {})
                        _new_ctx.pop("_retry_after_resets", None)
                        _qt.context = _new_ctx
                        session.add(_qt)
                        _cleared += 1
                    if _cleared:
                        await session.commit()
                        logger.info(
                            f"[REAPER] Cleared stale retry_after on {_cleared} task(s)"
                        )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Stale retry_after sweep error: {e}", exc_info=True
                    )

                # Step 5e: Close ACTIVE ResidualRemediationCase rows whose fingerprint
                # has not been seen in the last 72 h. Without this sweep the table
                # accumulates stale ACTIVE rows for fingerprints that stopped occurring
                # organically (infra recovered, unrelated PR fixed the root cause, etc.).
                try:
                    await _sweep_stale_residual_cases(session)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Stale residual case sweep error: {e}", exc_info=True
                    )

                # Step 6: QUEUED task age alerts — escalate through 3 tiers (INFO/WARNING/ERROR
                # at 1×/3×/6× the threshold) so long-stalled tasks re-alert instead of going silent.  # noqa: RUF003
                try:
                    from app.models import Alert, AlertSeverity
                    from app.broadcast import broadcast_to_project as _bcast
                    import app.shared_state as _shared_state

                    _queued_threshold_hours = _runtime_config.get(
                        "queued_age_alert_hours", 2
                    )
                    _queued_cutoff = now - timedelta(hours=_queued_threshold_hours)
                    # Audit R3-W2 (H7): cap per-tick fetch at _BATCH rows.
                    _stale_queued = (
                        (
                            await session.execute(
                                select(Task)
                                .where(
                                    Task.status == TaskStatus.QUEUED,
                                    Task.created_at <= _queued_cutoff,
                                )
                                .limit(_BATCH)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if len(_stale_queued) >= _BATCH:
                        logger.warning(
                            "[REAPER] stale-queued sweep hit cap=%d rows — result truncated; "
                            "follow-up: chunk-paginate",
                            _BATCH,
                        )

                    _tier_specs = [
                        (1, _queued_threshold_hours, AlertSeverity.INFO, "INFO"),
                        (
                            2,
                            _queued_threshold_hours * 3,
                            AlertSeverity.WARNING,
                            "WARNING",
                        ),
                        (3, _queued_threshold_hours * 6, AlertSeverity.ERROR, "ERROR"),
                    ]

                    _alerted = 0
                    for _qt in _stale_queued:
                        _qctx = _qt.context or {}
                        _prev_tier = _qctx.get("queued_age_alert_tier") or (
                            1 if _qctx.get("queued_age_alert_fired") else 0
                        )
                        _queue_hours = (now - _qt.created_at).total_seconds() / 3600
                        _target_tier, _target_severity, _target_label = (
                            0,
                            AlertSeverity.INFO,
                            "INFO",
                        )
                        for _tier, _hrs, _sev, _sev_label in _tier_specs:
                            if _queue_hours >= _hrs:
                                _target_tier, _target_severity, _target_label = (
                                    _tier,
                                    _sev,
                                    _sev_label,
                                )
                        if _target_tier <= _prev_tier:
                            continue

                        _qt_type = (_qt.first_payload or {}).get("type", "unknown")
                        # Audit W1: retry_after_until is a column.
                        _ra_dt = _qt.retry_after_until
                        _ra_raw = _ra_dt.isoformat() if _ra_dt else None
                        _ra_recent_cutoff = now - timedelta(minutes=5)
                        if _qt.requires_approval:
                            _likely_cause = "awaiting operator approval"
                        elif _ra_dt is not None and _ra_dt >= _ra_recent_cutoff:
                            _resets = int(_qctx.get("_retry_after_resets") or 0)
                            if _ra_dt > now:
                                _held_min = (now - _qt.created_at).total_seconds() / 60
                                _likely_cause = (
                                    f"rate-limit retry delay active (retry_after={_ra_raw}, "
                                    f"held {_held_min:.1f}m, reset {_resets} times)"
                                )
                            else:
                                _expired_min = (now - _ra_dt).total_seconds() / 60
                                _likely_cause = (
                                    f"rate-limit retry delay recently expired ({_expired_min:.1f}m ago, "
                                    f"reset {_resets} times)"
                                )
                        else:
                            _rate, _sample = _shared_state.get_task_type_failure_rate(
                                _qt_type, window_minutes=15
                            )
                            if _sample >= 3 and _rate >= 0.7:
                                _likely_cause = f"type failure gate active ({_qt_type}: {_rate:.0%} failure rate)"
                            elif (
                                _shared_state.tasks_active_gauge
                                >= _runtime_config.get("max_concurrent_vms", 8)
                            ):
                                _likely_cause = (
                                    "all accounts healthy but VM slot limit reached"
                                )
                            else:
                                from app.max_plan_pool import (
                                    describe_pool_blockage,
                                    _extract_task_model,
                                )

                                _likely_cause = describe_pool_blockage(
                                    _extract_task_model(_qt)
                                )

                        _alert = Alert(
                            project_id=_qt.project_id,
                            severity=_target_severity,
                            title=f"Task stalled in queue: {_qt.title or str(_qt.id)[:8]}",
                            message=(
                                f"Task type: {_qt_type}. "
                                f"Queued for {_queue_hours:.1f}h (threshold: {_queued_threshold_hours}h, tier {_target_tier}). "
                                f"Likely cause: {_likely_cause}."
                            ),
                            task_id=_qt.id,
                        )
                        session.add(_alert)

                        _new_qctx = dict(_qctx)
                        _new_qctx["queued_age_alert_tier"] = _target_tier
                        _new_qctx["queued_age_alert_fired"] = True
                        _qt.context = _new_qctx
                        session.add(_qt)

                        try:
                            await _bcast(
                                str(_qt.project_id),
                                "alert_created",
                                {
                                    "alert_id": str(_alert.id),
                                    "severity": _target_label,
                                    "title": _alert.title,
                                },
                            )
                        except Exception as _bcast_err:  # noqa: BLE001
                            logger.warning(
                                f"[REAPER] Queued age alert broadcast failed for {_qt.id}: {_bcast_err}"
                            )

                        _alerted += 1
                        logger.info(
                            f"[REAPER] Queued age alert tier {_target_tier} ({_target_label}) fired for task {_qt.id} "
                            f"(type={_qt_type}, queued={_queue_hours:.1f}h, cause={_likely_cause})"
                        )

                    if _alerted:
                        await session.commit()
                        logger.info(
                            f"[REAPER] Fired queued age alerts for {_alerted} stale task(s)"
                        )

                except Exception as e:
                    logger.error(
                        f"[REAPER] Queued age alert sweep error: {e}", exc_info=True
                    )

                # Step 7: Stalled strategic-cycle reaper.
                # Audit W2/D2/D12: cycles stuck in phase*_running state for >3h
                # mean the synchronous phase1→phase2 chain crashed and the
                # lifecycle backstop missed it (e.g. orchestrator restart between
                # task termination and post-commit). Mark them stalled so the
                # strategic agent can see the failure on the next run.
                try:
                    from app import strategic_cycle
                    from app.models import Alert, AlertSeverity
                    from app.broadcast import broadcast_to_project as _bcast

                    stalled = await strategic_cycle.find_stalled(session)
                    for c in stalled:
                        if c.outcome in (
                            strategic_cycle.OUTCOME_IN_PROGRESS,
                            strategic_cycle.OUTCOME_PHASE1_RUNNING,
                            strategic_cycle.OUTCOME_PHASE1_DONE,
                        ):
                            c.outcome = strategic_cycle.OUTCOME_STALLED_PHASE1
                            stalled_phase = "phase1"
                        else:
                            c.outcome = strategic_cycle.OUTCOME_STALLED_PHASE2
                            stalled_phase = "phase2"
                        if c.failure_reason is None:
                            c.failure_reason = f"reaper_stall: cycle in {stalled_phase} >3h with no completion"
                        session.add(c)
                        try:
                            alert = Alert(
                                project_id=c.project_id,
                                severity=AlertSeverity.WARNING,
                                title=f"Strategic cycle {c.id} stalled in {stalled_phase}",
                                message=(
                                    f"Cycle {c.id} has been in {stalled_phase} for >3h with no "
                                    f"completion handler update. Marking stalled — next strategic "
                                    f"cycle will spawn a fresh attempt. Phase1 task: {c.phase1_task_id}, "
                                    f"phase2 task: {c.phase2_task_id}."
                                ),
                            )
                            session.add(alert)
                            await _bcast(
                                str(c.project_id),
                                "alert_created",
                                {
                                    "alert_id": str(alert.id),
                                    "severity": "WARNING",
                                    "title": alert.title,
                                },
                            )
                        except Exception:
                            logger.exception(
                                f"[REAPER] Failed to create stalled-cycle alert for {c.id}"
                            )
                    if stalled:
                        await session.commit()
                        logger.info(
                            f"[REAPER] Marked {len(stalled)} strategic cycle(s) stalled"
                        )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Stalled-cycle sweep error: {e}", exc_info=True
                    )

                # Step 8: Per-(provider, model) regression scan.
                # Detects accounts whose (provider, model) success-rate has
                # drifted significantly below the project median and, on
                # transition, quarantines the model + dispatches notifications.
                # Gated to ~15min cadence — the underlying Task aggregation
                # query is non-trivial, and a faster cadence wouldn't change
                # quarantine outcomes (the regression criteria need 7 days of
                # samples and don't shift minute-to-minute).
                global _last_regression_scan
                now_scan = _utcnow()
                if (
                    _last_regression_scan is None
                    or (now_scan - _last_regression_scan).total_seconds()
                    >= _REGRESSION_SCAN_INTERVAL_SECONDS
                ):
                    _last_regression_scan = now_scan
                    try:
                        await _run_regression_scan_and_notify(session)
                    except Exception as e:
                        logger.error(
                            f"[REAPER] Regression scan error: {e}", exc_info=True
                        )

                # Step 9: Finalize release qualifications stuck "pending".
                # The reaper's own task-kill path (Step 5) can mark a
                # release_audit / qualifier-smoketest child FAILED directly,
                # without routing through the task-completion handler that
                # normally calls maybe_finalize_qualification. A reaper-killed
                # qualifier child therefore leaves its ReleaseVersion wedged at
                # last_qualification.status == "pending" forever. This sweep is
                # the defense-in-depth backstop: it finds every version stuck
                # pending (children all terminal, OR pending past the
                # qualification timeout) and finalizes each. Idempotent and
                # safe every cycle.
                #
                # Audit H5: sweep_stuck_qualifications finalizes EACH version in
                # its OWN dedicated DB session — a real promotion lands a GitHub
                # merge on `main`, and the per-version session makes that
                # status="promoted" + N+1 row durable in isolation, so a later
                # version raising can't roll it back. The `session` passed here
                # is used only for the read-only scan that lists candidate
                # versions; the sweep never writes through it. No end-of-step
                # commit is needed here; the surrounding try/except still
                # contains any unexpected sweep-level error.
                #
                # ``session_factory=AsyncSessionLocal`` is passed explicitly:
                # the per-version dedicated finalize session must open against
                # the SAME engine as this scan session. Without it the sweep
                # falls back to ``release_qualifier``'s module-level
                # ``app.database.AsyncSessionLocal`` — a different binding from
                # the reaper's own ``app.reaper.AsyncSessionLocal`` that the
                # ``reaper_db`` test fixture patches. In production both
                # resolve to the real DB so it happens to work; under the test
                # harness the unpatched factory points at the wrong engine and
                # every finalize is silently swallowed by the sweep's
                # per-version try/except. Passing the reaper's own factory
                # keeps scan and finalize on one engine in both environments.
                try:
                    from app import release_qualifier

                    _finalized = await release_qualifier.sweep_stuck_qualifications(
                        session, session_factory=AsyncSessionLocal
                    )
                    if _finalized:
                        logger.info(
                            f"[REAPER] Finalized {_finalized} stuck release "
                            f"qualification(s)"
                        )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Stuck qualification sweep error: {e}",
                        exc_info=True,
                    )
            consecutive_errors = 0
            # Liveness telemetry: only bump on the success path. If the
            # cycle body raised above, neither this counter nor the
            # last-completed timestamp advances — the /metrics gauge will
            # report a growing age and the monitoring alert fires.
            global _reaper_cycles_completed_count, _reaper_last_cycle_completed_at
            _reaper_cycles_completed_count += 1
            _reaper_last_cycle_completed_at = _utcnow()
        except asyncio.CancelledError:
            logger.info("[REAPER] Service shutting down.")
            break
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"[REAPER] Error: {e}", exc_info=True)
            try:
                await session.rollback()
            except Exception:
                logger.exception("[REAPER] Rollback after reaper error failed")

        # Sleep before next cycle; use exponential backoff on consecutive errors
        sleep_seconds = min(60 * (2**consecutive_errors), 300)
        try:
            await asyncio.sleep(sleep_seconds)
        except asyncio.CancelledError:
            logger.info("[REAPER] Service shutting down.")
            break
