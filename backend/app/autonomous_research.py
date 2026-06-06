"""Autonomous research spawning functions."""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    Task,
    Project,
    TaskHistory,
    TaskStatus,
    Alert,
    AlertSeverity,
)
from app.database import AsyncSessionLocal, resolve_dialect_name
from app.knowledge_query import build_knowledge_index
from app.payload_schemas import (
    validate_internal_payload,
    ALL_TASK_TYPES,
    PayloadTooLargeError,
    _MAX_PAYLOAD_BYTES,
)
import app.shared_state as _shared_state
from app.shared_state import (
    get_project_integration_branch as _get_project_integration_branch,
    runtime_config as _runtime_config,
)
from app.max_plan_pool import _max_plan_pools, _account_available_for_model
from app.task_capabilities import STRATEGIC_TASK_TYPES
from app.task_locking import with_vision_locked
from app.time_utils import _utcnow
from app.direction_mode import (
    append_prompt_suffix,
    is_pause_mode,
    mode_prompt_hint,
    normalized_recommended_mode,
)
from app.playbook_learning import (
    RECENT_SUCCESSES_TASK_TYPES,
    summarize_recent_successes,
)
from app.source_ref_validation import (
    classify_existing_paths,
    detect_absent_symbols,
    resolve_branch_verification_context,
)

logger = logging.getLogger(__name__)
_spawn_locks: dict[str, asyncio.Lock] = {}

# Title/text for undirected (cross-cutting) research. Centralised so the spawn
# site and the dedup guard below can never drift apart.
CROSS_CUTTING_DIRECTION_TEXT = "Cross-cutting codebase analysis"
CROSS_CUTTING_RESEARCH_TITLE = f"Research: {CROSS_CUTTING_DIRECTION_TEXT}"


def _get_spawn_lock(project_id: str) -> asyncio.Lock:
    if project_id not in _spawn_locks:
        _spawn_locks[project_id] = asyncio.Lock()
    return _spawn_locks[project_id]


async def _undirected_research_recently_active(project_id, session) -> bool:
    """True when a cross-cutting (undirected) research task is already active or
    was spawned within the cooldown window.

    The undirected spawn fires from two re-triggering paths: the completion-based
    modulo counter (``research_spawn_count % undirected_research_frequency``,
    which stays on the trigger until a research COMPLETES) and the
    ``chosen_directions``-empty fallback (which re-fires every idle cycle when the
    few active directions are busy). Without a guard these spawn back-to-back
    near-identical cross-cutting analyses whose findings then dedupe to ~0 tasks
    (incident 2026-06-02: d551b034 / b8f1eb8c / a473833c — 3 in ~13 min, the
    middle cancelled, d551b034's 4 actionable findings all deduped to zero).

    Mirrors the strategic-spawn active+cooldown guard: (a) the active-state check
    blocks piling a second analysis on top of a running one; (b) the cooldown
    blocks the cancel/complete-then-instantly-respawn loop regardless of the
    prior task's terminal state. Set ``undirected_research_cooldown_minutes`` <= 0
    to disable the cooldown leg.
    """
    active = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == project_id,
                Task.title == CROSS_CUTTING_RESEARCH_TITLE,
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
    ).scalar() or 0
    if active > 0:
        return True
    cooldown_minutes = _runtime_config.get("undirected_research_cooldown_minutes", 60)
    if cooldown_minutes <= 0:
        return False
    cutoff = _utcnow() - timedelta(minutes=cooldown_minutes)
    recent = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == project_id,
                Task.title == CROSS_CUTTING_RESEARCH_TITLE,
                Task.created_at >= cutoff,
            )
        )
    ).scalar() or 0
    return recent > 0


async def _spawn_research_task(project: Project, session: AsyncSession):
    """Spawn a research task for a project if conditions are met.

    Uses a per-project lock to prevent races between the post-merge hook
    and the idle-check loop, which run concurrently with separate sessions.
    """
    async with _get_spawn_lock(str(project.id)):
        return await _spawn_research_task_inner(project, session)


async def _maybe_notify_operator_refresh_deferred(
    project: Project, session: AsyncSession, reason: str
) -> None:
    """Surface, exactly once, that an operator steering refresh was deferred.

    An operator steering request routes through the coalesced strategic-refresh
    marker. When it cannot schedule the strategist immediately (a cycle is
    already running, or a residual gate holds it), the operator must be told
    *why* rather than left with silence — never a silent swallow. Only fires for
    an operator-origin pending marker that has not already been notified; sets
    the ``notified`` flag so subsequent ticks stay quiet until the request
    re-arms or is consumed. Best-effort and non-fatal.
    """
    from app.strategic_trigger import (
        ORIGIN_OPERATOR,
        mark_strategic_refresh_notified,
        strategic_refresh_notified,
        strategic_refresh_origin,
    )

    vision = project.vision or {}
    if strategic_refresh_origin(vision) != ORIGIN_OPERATOR:
        return
    if strategic_refresh_notified(vision):
        return
    try:
        from app.notification_dispatch import (
            post_strategic_steering_deferred_message,
        )

        await post_strategic_steering_deferred_message(session, project.id, reason)
        await with_vision_locked(
            session, project.id, mark_strategic_refresh_notified
        )
        await session.commit()
        logger.info(
            "[STRATEGIC] operator steering refresh deferred for project %s (%s) "
            "— posted one-time deferral notice",
            project.id,
            reason,
        )
    except Exception:
        logger.exception(
            "[STRATEGIC] failed to post operator steering deferral notice for "
            "project %s",
            project.id,
        )


async def _maybe_notify_operator_steering_acted(
    project: Project, session: AsyncSession
) -> None:
    """Confirm, exactly once, that operator steering reached the strategist.

    Symmetric success counterpart to ``_maybe_notify_operator_refresh_deferred``.
    Called only after a strategic spawn has *consumed* an operator-origin refresh
    marker — the caller reads the marker origin BEFORE ``clear_strategic_refresh``
    wipes it, so this fires only for operator intent (a qualification-origin
    consume stays silent). The marker is gone after consume, so this single
    consume site is inherently once-per-pending-request — no notified flag is
    needed. Best-effort and non-fatal: a failure here must never break the spawn
    that already succeeded and committed.
    """
    try:
        from app.notification_dispatch import (
            post_strategic_steering_acted_message,
        )

        await post_strategic_steering_acted_message(session, project.id)
        await session.commit()
        logger.info(
            "[STRATEGIC] operator steering consumed on spawn for project %s "
            "— posted one-time confirmation notice",
            project.id,
        )
    except Exception:
        logger.exception(
            "[STRATEGIC] failed to post operator steering confirmation for "
            "project %s",
            project.id,
        )


async def _fetch_direction_reasoning_memos(
    project_id,
    direction_id: str,
    session: AsyncSession,
    *,
    cap: int = 5,
    days: int = 90,
) -> list[dict]:
    """Fetch recent COMPLETED memo tasks for a specific direction.

    Returns a compact list so research agents can avoid re-proposing work
    already documented as unactionable (investigation_unactionable,
    direction_retirement, outcome_scaffolding, etc.).
    """
    from sqlalchemy import or_

    cutoff = _utcnow() - timedelta(days=days)
    rows = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.COMPLETED,
                    Task.payloads[0]["type"].as_string() == "memo",
                    Task.created_at >= cutoff,
                    or_(
                        Task.context["direction_id"].as_string() == direction_id,
                        Task.context["memo_source_direction_id"].as_string()
                        == direction_id,
                    ),
                )
                .order_by(Task.created_at.desc())
                .limit(cap)
            )
        )
        .scalars()
        .all()
    )

    result = []
    for t in rows:
        ctx = t.context or {}
        plan = t.plan_output if isinstance(t.plan_output, dict) else {}
        memo = plan.get("memo") if isinstance(plan.get("memo"), dict) else {}
        summary = str((memo or {}).get("summary") or "")
        if not summary:
            summary = (
                ctx.get("investigation_summary")
                or ctx.get("retirement_rationale")
                or (t.first_payload or {}).get("topic")
                or ""
            )
        result.append(
            {
                "task_id": str(t.id),
                "kind": ctx.get("memo_reasoning_kind", ""),
                "outcome": ctx.get("investigation_outcome", ""),
                "summary": str(summary)[:400],
            }
        )
    return result


async def _spawn_research_task_inner(project: Project, session: AsyncSession):
    """Inner implementation — always called under the per-project spawn lock."""
    from app.alert_dispatch import _maybe_create_circuit_breaker_alert
    from app.research_completion import (
        _compute_momentum_factor,
        _compute_productivity_score,
        _compute_acceptance_rate,
    )
    from app.direction_metrics import (
        _compute_direction_quality_score,
        _compute_difficulty_index,
    )
    from app.anomaly_detection import _compute_research_anomalies, check_anomaly_actions
    from app.direction_lifecycle import _process_direction_lifecycle

    # Guard: autonomous must be enabled and the project must not be archived.
    # Archiving halts all of a project's work, including autonomous spawning.
    # We deliberately do NOT post an operator deferral notice here: an operator
    # steering a project whose autonomy is intentionally off (or which is
    # archived) is steering an intentionally-paused project, so a "your steering
    # is queued" notice would be misleading — the work is paused by design, not
    # transiently deferred. The marker persists and is delivered if autonomy is
    # re-enabled.
    if not project.autonomous_enabled or project.archived:
        return

    # Playbook auto-seed removed: the hardcoded seed entries were specific to the
    # Blueprint pipeline repo and polluted new projects with wrong build/test
    # instructions. Agents discover the correct playbook entries by exploring
    # the actual codebase and writing them via `./playbook set`.
    # Guard: don't spawn if user has no available accounts (avoids immediate failure)
    user_pool = _max_plan_pools.get(project.user_id, [])
    research_model = _runtime_config.get("task_type_models", {}).get("research")
    has_available = any(
        _account_available_for_model(a, research_model) for a in user_pool
    ) or bool(os.getenv("ANTHROPIC_API_KEY"))
    if not has_available:
        logger.info(
            f"[AUTONOMOUS] Skipping research for {project.name} — no available accounts"
        )
        # Operator steering must never be a silent swallow: if an operator-origin
        # refresh marker is pending, tell them their steering is queued and why
        # before bailing. The marker itself is left intact (we only return), so
        # the steering is still delivered on a later tick once accounts recover.
        await _maybe_notify_operator_refresh_deferred(
            project, session, "paused while no model accounts are available"
        )
        return
    # Guard: circuit breaker must allow spawning
    breaker = _shared_state.get_circuit_breaker(str(project.id))
    if breaker["state"] == "OPEN":
        # Check if cooldown has elapsed → transition to HALF-OPEN
        if (
            breaker["opened_at"]
            and (_utcnow() - breaker["opened_at"]).total_seconds()
            >= breaker["cooldown_seconds"]
        ):
            breaker["state"] = "HALF_OPEN"
            logger.warning(
                f"[CIRCUIT BREAKER] OPEN → HALF-OPEN for {project.id} (cooldown elapsed, allowing one test task)"
            )
        else:
            logger.warning(
                f"[CIRCUIT BREAKER] Skipping autonomous research spawn for project={project.id} "
                f"— circuit breaker state=OPEN (still in cooldown)"
            )
            # A struggling project (breaker OPEN) is exactly when an operator is
            # most likely to steer; never swallow that silently. Notify (once)
            # that steering is queued while the breaker cools down. The marker is
            # preserved — only the spawn returns — so steering still lands once
            # the breaker clears.
            await _maybe_notify_operator_refresh_deferred(
                project, session, "paused while the research circuit breaker is open"
            )
            return  # Still in cooldown
    elif breaker["state"] == "HALF_OPEN":
        # Check if existing test task is stuck — treat as failure after 30 min
        test_id = breaker.get("half_open_task_id")
        if test_id:
            test_task = await session.get(Task, uuid.UUID(test_id))
            if (
                test_task
                and test_task.started_at
                and (_utcnow() - test_task.started_at).total_seconds() > 1800
            ):
                logger.warning(
                    f"[CIRCUIT BREAKER] HALF_OPEN test task {test_id} stuck for 30m+, treating as failure → OPEN"
                )
                breaker["state"] = "OPEN"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
                breaker["half_open_task_id"] = None
                await _maybe_create_circuit_breaker_alert(
                    project.id,
                    session,
                    f"HALF_OPEN test task stuck for 30m+ — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)",
                )
                # Re-opened breaker is still a circuit-breaker halt: surface the
                # deferral to any pending operator steering (once). Marker is
                # preserved. Placed after the breaker mutation/alert so the
                # helper's commit also flushes the alert.
                await _maybe_notify_operator_refresh_deferred(
                    project,
                    session,
                    "paused while the research circuit breaker is open",
                )
                return  # Back to cooldown
            elif not test_task:
                logger.warning(
                    f"[CIRCUIT BREAKER] HALF_OPEN test task {test_id} no longer exists (deleted/reaped) → OPEN"
                )
                breaker["state"] = "OPEN"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
                breaker["half_open_task_id"] = None
                await _maybe_create_circuit_breaker_alert(
                    project.id,
                    session,
                    f"HALF_OPEN test task deleted/reaped — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)",
                )
                # Re-opened breaker is still a circuit-breaker halt: surface the
                # deferral to any pending operator steering (once). Marker is
                # preserved. Placed after the breaker mutation/alert so the
                # helper's commit also flushes the alert.
                await _maybe_notify_operator_refresh_deferred(
                    project,
                    session,
                    "paused while the research circuit breaker is open",
                )
                return  # Back to cooldown
    # Guard: must have active directions that aren't in cooldown or mid-cycle
    cooldown_minutes = _runtime_config.get("autonomous_direction_cooldown_minutes", 240)
    now = _utcnow()

    # Build set of direction IDs that already have an active research cycle.
    # Include EVALUATING — the agent is done but children are still merging,
    # so the direction is still being worked on and shouldn't be re-spawned.
    #
    # Also include FAILED-but-resurrection-eligible tasks. The reaper's
    # Step 5 sweep at reaper.py:~1782 resurrects infra-class FAILED tasks
    # within a 2h lookback once infrastructure recovers. Without this
    # branch, a brief gap opens between "reaper marks FAILED" and "reaper
    # resurrects" — typically ~60s — during which active_research returns
    # empty and the autonomous-spawn loop slips a duplicate research onto
    # the same direction. Production 2026-05-05 (3d4bc05a + 88c7d5c8 on
    # d-3878da01): 5s after reaper marked 3d4bc05a FAILED, the spawn loop
    # fired 88c7d5c8 on the same direction; reaper resurrected 3d4bc05a
    # 66s later, leaving two parallel research cycles. Match the reaper's
    # own resurrection criteria here so the windows align exactly:
    #   - Task.updated_at within 2h (matches _infra_lookback)
    #   - failure_category in INFRA_CLASSES (resurrection-eligible class)
    #   - _resurrection_count not yet set (can still be resurrected once)
    # Genuinely-dead tasks (CODE_ERROR / OOM / pr_closed_unmerged /
    # already-resurrected) won't match and won't block.
    from sqlalchemy import or_, and_
    from app.failure_classification import INFRA_CLASSES

    busy_direction_ids: set = set()
    resurrection_lookback = now - timedelta(hours=2)
    # ``~has_key`` compiles to the Postgres-only JSONB ``?`` (key-existence)
    # operator, which raises ``sqlite3.OperationalError`` on the SQLite smoke
    # gate. Branch on the resolved dialect and fall back to the portable
    # ``json_extract`` IS NULL form for the key-absence check on SQLite.
    if resolve_dialect_name(session) == "postgresql":
        _no_resurrection_count = ~Task.context.has_key("_resurrection_count")
    else:
        _no_resurrection_count = func.json_extract(
            Task.context, "$._resurrection_count"
        ).is_(None)
    active_research = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    or_(
                        Task.status.in_(
                            [
                                TaskStatus.QUEUED,
                                TaskStatus.PROVISIONING,
                                TaskStatus.IN_PROGRESS,
                                TaskStatus.EVALUATING,
                                # BLOCKED research parents still structurally
                                # occupy their direction: a lane parked on an
                                # unresolved upstream prerequisite is wanted-but-
                                # not-runnable, not done. Treating it as busy
                                # keeps the prerequisite-gating model consistent
                                # — the direction stays held until the lane
                                # resumes (foundation lands / recheck) or is
                                # explicitly resolved, instead of drifting back
                                # into autonomous selection after the cooldown.
                                TaskStatus.BLOCKED,
                            ]
                        ),
                        and_(
                            Task.status == TaskStatus.FAILED,
                            Task.updated_at >= resurrection_lookback,
                            Task.context["failure_category"]
                            .as_string()
                            .in_(list(INFRA_CLASSES)),
                            _no_resurrection_count,
                        ),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    evaluating_timeout_minutes = _runtime_config.get(
        "evaluating_pipeline_timeout_minutes", 240
    )

    # Parents that still have non-terminal children are actively progressing
    # (their findings are mid-flight on their way to a rollup), not stuck. Used
    # to suppress the EVALUATING-pipelining exception below so we don't spawn a
    # duplicate research cycle on a direction that's still being worked.
    active_research_ids = [t.id for t in active_research]
    parents_with_active_children: set = set()
    if active_research_ids:
        child_parent_rows = (
            (
                await session.execute(
                    select(Task.parent_task_id).where(
                        Task.parent_task_id.in_(active_research_ids),
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
        parents_with_active_children = {pid for pid in child_parent_rows if pid}

    # Pipelining cap: when a long-EVALUATING parent triggers the staleness
    # skip below, we still want to allow at most ONE additional pipelined
    # research per direction within the configured window. Without this,
    # every spawn cycle (~6 min) saw the same EVALUATING-stale parent and
    # spawned a fresh research — the parent's updated_at never bumps, so
    # the skip stays armed and pipelining is uncapped (incident
    # 2026-05-04: research 779af581 stuck EVALUATING for 17.8h triggered
    # three duplicate research spawns at 22:50/22:56/23:07 on direction
    # d-3878da01, all manually cancelled). The window default of 240 min
    # accounts for legitimately long-running research/merge cycles where
    # a second concurrent spawn would risk big merge conflicts.
    pipelining_window_minutes = _runtime_config.get(
        "research_pipelining_window_minutes",
        240,
    )
    pipelining_cutoff = now - timedelta(minutes=pipelining_window_minutes)
    recently_spawned = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.created_at >= pipelining_cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    recent_research_by_direction: dict[str, list] = {}
    for rt in recently_spawned:
        rpayload = rt.first_payload
        if rpayload.get("type") == "research":
            rdid = rpayload.get("current_direction_id")
            if rdid:
                recent_research_by_direction.setdefault(rdid, []).append(rt.id)

    for t in active_research:
        payload = t.first_payload
        if payload.get("type") == "research":
            # current_direction_id identifies the targeted direction; null means
            # undirected/cross-cutting analysis which doesn't block any direction.
            current_did = payload.get("current_direction_id")
            if not current_did:
                continue
            # Pipeline new research alongside long-EVALUATING parents: once children
            # have been merging past the timeout, allow a fresh cycle on the same
            # direction. Capped at 1 additional spawn per pipelining window so
            # truly stuck parents (e.g., 17h EVALUATING) don't get a fresh spawn
            # every cycle. Dedup in research_completion handles overlapping
            # findings on the one allowed pipelined spawn.
            if t.status == TaskStatus.EVALUATING and t.updated_at:
                updated = t.updated_at
                if updated.tzinfo is not None:
                    updated = updated.replace(tzinfo=None)
                age_minutes = (now - updated).total_seconds() / 60
                if age_minutes >= evaluating_timeout_minutes:
                    # Only pipeline when the parent is GENUINELY stuck, not
                    # merely waiting on in-flight work. A research parent whose
                    # children are still running, or whose rollup PR is still
                    # open, is progressing normally and legitimately sits in
                    # EVALUATING for hours — the EVALUATING age alone can't tell
                    # "stuck" from "children working". Pipelining there spawns a
                    # duplicate rollup on the SAME direction that is guaranteed
                    # to conflict with the first once one of them merges
                    # (incident 2026-06-03: d2415495/#7427 vs e783a7ad/#7433 on
                    # d-5d841cea, spawned 2h20m apart — the second fired ~4 min
                    # after this parent crossed the timeout while its 5 children
                    # were still working). Genuinely-stuck = all children
                    # terminal AND no open rollup PR. (A still-EVALUATING parent
                    # with a pr_number means the rollup PR is still in flight —
                    # it only leaves EVALUATING on merge.)
                    has_active_children = t.id in parents_with_active_children
                    has_open_pr = bool(
                        ((t.context or {}).get("review_loop") or {}).get("pr_number")
                    )
                    if not has_active_children and not has_open_pr:
                        # Count OTHER recent research on this direction (any
                        # status, including CANCELLED) — exclude self.
                        recent_others = [
                            rid
                            for rid in recent_research_by_direction.get(
                                current_did, []
                            )
                            if rid != t.id
                        ]
                        if not recent_others:
                            continue  # No prior pipelined spawn — allow one
                        # Already pipelined within the window — treat as busy.
                    # else: progressing (children active or PR open) → busy
            busy_direction_ids.add(current_did)

    active_directions = await _process_direction_lifecycle(
        project,
        session,
        cooldown_minutes,
        busy_direction_ids,
        now,
    )
    # Allow strategic cycles to proceed even without active directions (they create directions).
    # Stop forcing after 3 consecutive strategic failures to prevent infinite loops.
    vision_data = project.vision or {}
    strategic_failures = vision_data.get("_strategic_failures", 0)
    # Auto-decay: if last failure was >48h ago, reset counter for recovery purposes
    if strategic_failures >= 3:
        failures_at = vision_data.get("_strategic_failures_at")
        if failures_at:
            try:
                last_failure = datetime.fromisoformat(
                    failures_at.replace("Z", "+00:00")
                )
                if (_utcnow() - last_failure).total_seconds() > 48 * 3600:
                    strategic_failures = 0
                    logger.info(
                        f"[STRATEGIC] Auto-decayed _strategic_failures for project {project.id} (last failure: {failures_at})"
                    )
            except (ValueError, TypeError):
                pass  # malformed timestamp, keep current value
    # Check if ANY direction is active in the project (ignoring cooldown filter).
    # active_directions only contains non-cooldown directions, so a project with
    # active-but-cooling directions should not trigger a strategic cycle.
    any_active_in_project = any(
        d.get("status") == "active" for d in (project.directions or [])
    )
    all_dirs_saturated = any_active_in_project and all(
        d.get("possibly_saturated")
        for d in (project.directions or [])
        if d.get("status") == "active"
    )
    force_strategic = strategic_failures < 3 and (
        not vision_data.get("purpose")
        or not any_active_in_project
        or all_dirs_saturated
    )
    # An operator steering refresh must schedule the strategist promptly even
    # when the failure breaker would otherwise suppress forcing (and the
    # `not active_directions` early-return below) — that is exactly the moment an
    # operator needs to intervene. The active-cycle / min-interval gates in the
    # spawn block still apply (and surface a one-time deferral notice); only the
    # _strategic_failures breaker is bypassed for operator intent.
    from app.strategic_trigger import (
        ORIGIN_OPERATOR as _ORIGIN_OPERATOR,
        strategic_refresh_origin as _strategic_refresh_origin,
    )

    if _strategic_refresh_origin(vision_data) == _ORIGIN_OPERATOR:
        force_strategic = True
    # Wall-clock backstop: must drive force_strategic so it bypasses the
    # `not active_directions` early return below. Without this promotion, when
    # every active direction has an in-flight child (busy_direction_ids),
    # _process_direction_lifecycle empties active_directions and we return here
    # — never reaching the strategic spawn block, no matter how long since the
    # last cycle. Hit on 2026-05-26 (deploy 00195): backstop never fired despite
    # last strategic being 2h+ old, because both directions were busy.
    if not force_strategic and strategic_failures < 3:
        strategic_max_interval = _runtime_config.get(
            "strategic_max_interval_minutes", 180
        )
        if strategic_max_interval > 0:
            from sqlalchemy import func as _backstop_func

            last_strategic_at = (
                await session.execute(
                    select(_backstop_func.max(Task.created_at)).where(
                        Task.project_id == project.id,
                        Task.payloads[0]["type"]
                        .as_string()
                        .in_(sorted(STRATEGIC_TASK_TYPES)),
                    )
                )
            ).scalar()
            if last_strategic_at is not None:
                # _utcnow() returns naive UTC (project convention).
                if last_strategic_at.tzinfo is not None:
                    last_strategic_at = last_strategic_at.replace(tzinfo=None)
                age_minutes = (_utcnow() - last_strategic_at).total_seconds() / 60.0
                if age_minutes >= strategic_max_interval:
                    logger.info(
                        f"[STRATEGIC] Wall-clock backstop firing for project {project.id}: "
                        f"last strategic task created {age_minutes:.1f}min ago "
                        f"(threshold {strategic_max_interval}min, "
                        f"active_directions={len(active_directions)}, busy={len(busy_direction_ids)})"
                    )
                    force_strategic = True
    if not active_directions and not force_strategic:
        return
    # Pick up to N non-conflicting directions for parallel research.
    # active_directions preserves UI drag-order (index 0 = highest priority).
    # Build vision priority lookup: directions tagged with a system_id that appears
    # earlier in vision.priorities get a lower (better) rank.
    vision_priorities = (vision_data.get("priorities") or []) if vision_data else []
    vision_priority_index: dict = {
        sys_id: idx
        for idx, sys_id in enumerate(vision_priorities)
        if isinstance(sys_id, str)
    }

    def _vision_rank(d: dict) -> int:
        """0 = top vision priority, len = unlisted. Directions without system_id get neutral rank."""
        sid = d.get("system_id")
        if not sid:
            return len(vision_priorities)  # neutral — don't penalize untagged
        return vision_priority_index.get(
            sid, len(vision_priorities) + 1
        )  # unlisted slightly below neutral

    # Sort all eligible directions by priority for greedy selection
    chosen_directions: list[dict] = []
    if active_directions:
        priority_rank = {"high": 0, "medium": 1, "low": 2}

        def _direction_score(d: dict) -> tuple:
            productivity_score = _compute_productivity_score(d)
            momentum = _compute_momentum_factor(d)
            acceptance_rate = _compute_acceptance_rate(d)
            quality_score = _compute_direction_quality_score(d)
            difficulty = _compute_difficulty_index(d)
            priority = d.get("priority", "medium")
            # Easy directions get a slight boost; hard directions are deprioritized
            # unless flagged high-priority (operator override).
            if difficulty < 5:
                difficulty_bonus = -1
            elif difficulty > 20 and priority != "high":
                difficulty_bonus = 1
            else:
                difficulty_bonus = 0
            is_new = 0 if d.get("last_researched_at") else -1  # small novelty bonus
            category_bonus = (
                -1 if d.get("category") == "product" else 0
            )  # product directions preferred
            # Vision rank matters: top vision priority = 0, unlisted = N.
            # Placed after priority+category but BEFORE productivity so the
            # strategic agent's vision.priorities actually steer selection
            # instead of being a dead tiebreaker.
            v_rank = _vision_rank(d)
            return (
                priority_rank.get(d.get("priority", "medium"), 1),
                category_bonus,  # product category within same priority
                is_new,  # novelty bonus within same priority
                v_rank,  # vision priority drives selection
                difficulty_bonus,  # easy boost / hard deprioritization
                -(
                    productivity_score * momentum
                ),  # productivity within same vision rank
                -quality_score,  # taxonomy-based quality; higher = earlier
                -(
                    acceptance_rate if acceptance_rate is not None else 0.5
                ),  # low acceptance = deprioritized
                1 if d.get("possibly_saturated") else 0,  # saturated last
                d.get("last_researched_at") or "",  # staleness
            )

        active_directions.sort(key=_direction_score)
        sorted_eligible = active_directions

        # Greedy selection: pick non-conflicting directions up to the parallel cap.
        # conflicts_with is a mutual exclusion list — if A lists B, they can't run
        # at the same time regardless of which was picked first.
        blocked_ids: set = set(busy_direction_ids)
        # Expand blocked set from already-running directions' conflicts
        all_dir_map = {
            d.get("id"): d for d in (project.directions or []) if d.get("id")
        }
        for did in busy_direction_ids:
            d = all_dir_map.get(did)
            if d:
                blocked_ids.update(d.get("conflicts_with") or [])
        for d in project.directions or []:
            if any(
                rid in (d.get("conflicts_with") or []) for rid in busy_direction_ids
            ):
                blocked_ids.add(d.get("id"))

        # Cross-instance serialization for concurrent orchestrator instances.
        from sqlalchemy import text as _sql_text

        _lock_key = abs(hash(f"research_spawn:{project.id}")) % (2**31)
        try:
            _got_lock = (
                await session.execute(
                    _sql_text("SELECT pg_try_advisory_xact_lock(:k)"),
                    {"k": _lock_key},
                )
            ).scalar()
        except Exception:  # noqa: BLE001 — SQLite/test dialects raise NotSupportedError; treat as "no contention"
            _got_lock = True  # non-Postgres dialect (tests) — skip serialization
        if not _got_lock:
            logger.info(
                f"[AUTONOMOUS] Research spawn for project {project.id} skipped — "
                f"another instance holds the advisory lock"
            )
            return

        max_parallel = _runtime_config.get("max_parallel_research_per_project", 3)
        for d in sorted_eligible:
            did = d.get("id")
            if is_pause_mode(d):
                logger.info(
                    "[AUTONOMOUS] skipped direction due to recommended_mode",
                    extra={
                        "direction_id": did,
                        "recommended_mode": "pause",
                        "reason": "mode_pause",
                    },
                )
                continue
            if did in blocked_ids:
                continue
            if d.get("recommended_mode") == "pause":
                continue  # strategic agent flagged this system as externally blocked
            chosen_directions.append(d)
            # Block this direction and its conflicts
            blocked_ids.add(did)
            blocked_ids.update(d.get("conflicts_with") or [])
            for other in sorted_eligible:
                if did in (other.get("conflicts_with") or []):
                    blocked_ids.add(other.get("id"))
            if len(chosen_directions) >= max_parallel:
                break

        if chosen_directions:
            for d in chosen_directions:
                did = d.get("id", "?")
                productivity_score = _compute_productivity_score(d)
                momentum = _compute_momentum_factor(d)
                category = d.get("category", "unset")
                logger.debug(
                    f"[RESEARCH] Selected direction {did} category={category} productivity_score={productivity_score:.2f} momentum={momentum:.1f} vision_rank={_vision_rank(d)}"
                )
    elif not force_strategic:
        return  # No directions and not a strategic cycle — nothing to do

    if (
        not chosen_directions
        and active_directions
        and all(is_pause_mode(d) for d in active_directions)
        and not force_strategic
    ):
        # Skip tactical research when every active direction is paused — UNLESS a
        # strategic cycle is forced (e.g. the wall-clock backstop above fired).
        # Without the `not force_strategic` guard this return short-circuits the
        # strategic-spawn block below, so a project whose only directions are all
        # paused can never get a strategic cycle: no research spawns → the modulo
        # counter never advances → directions never re-evaluated → permanent
        # starvation deadlock. Mirrors the `not active_directions and not
        # force_strategic` guard above so the backstop reaches the paused case too.
        #
        # Log clarity: `active_directions` here is the post-lifecycle list, which
        # has ALREADY excluded directions filtered by cooldown / in-flight work /
        # hold. So "all of these are paused" does NOT mean the project is wedged —
        # a non-paused direction that is merely cooling down (e.g. just had a
        # productive research cycle) is invisible to this check. Distinguish the
        # two so an operator (or this system's own diagnosis) doesn't read a
        # normal cooldown gap as a starvation wedge.
        _eligible_ids = {d.get("id") for d in active_directions}
        _filtered_non_paused = [
            d
            for d in (project.directions or [])
            if d.get("status") == "active"
            and not is_pause_mode(d)
            and d.get("id") not in _eligible_ids
        ]
        if _filtered_non_paused:
            logger.info(
                "[AUTONOMOUS] skipping tactical research: the %d active direction(s) "
                "eligible this cycle are all recommended_mode=pause, but %d non-paused "
                "direction(s) are temporarily filtered (cooldown / in-flight / hold) — "
                "NOT a wedge, they resume when their cooldown elapses: %s",
                len(active_directions),
                len(_filtered_non_paused),
                ", ".join(str(d.get("id")) for d in _filtered_non_paused[:5]),
            )
        else:
            logger.info(
                "[AUTONOMOUS] skipping tactical research: all active directions recommended_mode=pause"
            )
        return

    # When all active directions are saturated, skip tactical research — a strategic
    # cycle will create fresh directions; spawning more saturated research is wasteful.
    if all_dirs_saturated:
        chosen_directions = []
        active_directions = []

    # Check for a pending strategic review from a previously failed chain.
    # If the design_doc → strategic_review spawn failed last cycle, retry it now
    # rather than waiting up to 6 more research spawns (~3+ hours).
    pending_sr = (project.vision or {}).get("_pending_strategic_review")
    if pending_sr:
        try:
            from app.strategic_spawning import _spawn_strategic_review

            if await _spawn_strategic_review(
                project,
                session,
                cycle_id=pending_sr.get("cycle_id"),
                operator_steered=bool(pending_sr.get("operator_steered")),
                steering_excerpt=pending_sr.get("steering_excerpt"),
            ):
                def _clear_pending_sr(vision: dict) -> None:
                    vision.pop("_pending_strategic_review", None)

                await with_vision_locked(session, project.id, _clear_pending_sr)
                logger.info(
                    f"[STRATEGIC] Retried pending strategic review for project {project.id} (originally failed at {pending_sr.get('failed_at')})"
                )
            else:
                logger.warning(
                    f"[STRATEGIC] Pending SR retry skipped (active strategic task) for project {project.id}"
                )
        except Exception:
            logger.exception(
                f"[STRATEGIC] Retry of pending strategic review failed for project {project.id}"
            )
        return  # Don't spawn tactical research this cycle

    # Strategic cycle check — runs BEFORE the research blocking guard so it's
    # never blocked by an active tactical research task.  The design_doc_update
    # and strategic_review use their own task types and run in parallel.
    all_active_directions = [
        d for d in (project.directions or []) if d.get("status") == "active"
    ]
    # Use a stored spawn counter instead of derived research_history count.
    # The history-based count doesn't increment when cycles produce no novel
    # findings, causing undirected/strategic cycles to repeat indefinitely.
    # Seed from research_history on first access so existing projects don't reset.
    research_spawn_count = (project.vision or {}).get("_research_spawn_count")
    if research_spawn_count is None:
        research_spawn_count = sum(
            len(d.get("research_history", []))
            + d.get("history_aggregate", {}).get("total_cycles", 0)
            for d in all_active_directions
        )
        def _seed_research_spawn_count(vision: dict) -> None:
            vision["_research_spawn_count"] = research_spawn_count

        await with_vision_locked(session, project.id, _seed_research_spawn_count)
        logger.info(
            f"[RESEARCH] Seeded _research_spawn_count={research_spawn_count} from research_history"
        )
    strategic_frequency = (project.vision or {}).get(
        "_strategic_frequency_override"
    ) or _runtime_config.get("strategic_research_frequency", 6)
    is_strategic = (
        strategic_frequency > 0
        and research_spawn_count > 0
        and research_spawn_count % strategic_frequency == 0
    ) or force_strategic
    # 2026-05-03: gate strategic spawn on (a) no-active-strategic AND
    # (b) no recent strategic spawn (regardless of terminal state).
    #
    # (a) covers normal dedup: the spawner internally returns False when an
    #     active task exists, but the counter stays at the modulo trigger so
    #     the next tick re-enters the spawn branch and loops indefinitely.
    #
    # (b) covers the cancellation/failure loop: when force_strategic=True
    #     (no purpose / no active dirs / all saturated), the modulo check is
    #     bypassed and EVERY tick wants to spawn. A user cancelling the
    #     freshly-spawned strategic transitions it to CANCELLED (terminal),
    #     so the active-only check at (a) returns 0 — and the next tick
    #     spawns a new strategic instantly. Same for FAILED outcomes.
    #     Cooldown gates this regardless of force_strategic.
    if is_strategic:
        from sqlalchemy import func as _strat_func

        has_active_strategic = (
            await session.execute(
                select(_strat_func.count())
                .select_from(Task)
                .where(
                    Task.project_id == project.id,
                    Task.payloads[0]["type"]
                    .as_string()
                    .in_(sorted(STRATEGIC_TASK_TYPES)),
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
        ).scalar() or 0
        if has_active_strategic > 0:
            logger.debug(
                f"[STRATEGIC] Skipping strategic spawn — {has_active_strategic} active "
                f"strategic task(s) exist for project {project.id}; falling through to tactical"
            )
            # An operator steering request is deferred behind the running cycle
            # (it will consume on the follow-up tick). Surface that once so the
            # operator knows their message is queued, not silently swallowed.
            await _maybe_notify_operator_refresh_deferred(
                project,
                session,
                "queued behind an active strategic cycle",
            )
            is_strategic = (
                False  # skip the spawn block; tactical research below may still fire
            )
        else:
            cooldown_minutes = _runtime_config.get(
                "strategic_spawn_cooldown_minutes", 30
            )
            cooldown_cutoff = _utcnow() - timedelta(minutes=cooldown_minutes)
            recent_strategic = (
                await session.execute(
                    select(_strat_func.count())
                    .select_from(Task)
                    .where(
                        Task.project_id == project.id,
                        Task.payloads[0]["type"]
                        .as_string()
                        .in_(sorted(STRATEGIC_TASK_TYPES)),
                        Task.created_at >= cooldown_cutoff,
                    )
                )
            ).scalar() or 0
            if recent_strategic > 0:
                # A pending strategic-refresh marker (set by a resolved
                # qualification verdict that couldn't spawn while a cycle was
                # active) is a LEGITIMATE reason to bypass this anti-spam
                # cooldown — distinct from the cancel/fail loop the cooldown
                # guards against. Gated by the _strategic_failures breaker (< 3)
                # and a min-interval floor, so a broken request that keeps
                # failing stops bypassing instead of looping.
                from app.strategic_trigger import (
                    refresh_breaker_applies,
                    strategic_refresh_due,
                )

                _refresh_min_interval = _runtime_config.get(
                    "strategic_refresh_min_interval_seconds", 120
                )
                # Operator steering bypasses the _strategic_failures breaker — an
                # explicit human request is exactly when an operator needs to
                # intervene (and exactly when the breaker is likely tripped).
                # Automated qualification refreshes still park under the breaker.
                _breaker_gate_ok = (
                    not refresh_breaker_applies(project.vision)
                    or strategic_failures < 3
                )
                _refresh_due = _breaker_gate_ok and strategic_refresh_due(
                    project.vision, now, min_interval_seconds=_refresh_min_interval
                )
                if _refresh_due:
                    logger.info(
                        "[STRATEGIC] bypassing %dmin spawn-cooldown for project %s "
                        "— a strategic-refresh is pending (%s)",
                        cooldown_minutes,
                        project.id,
                        (project.vision or {}).get("_strategic_refresh", {}).get(
                            "reason"
                        ),
                    )
                else:
                    logger.info(
                        f"[STRATEGIC] Skipping strategic spawn — {recent_strategic} strategic "
                        f"task(s) created in last {cooldown_minutes}min for project {project.id} "
                        f"(force_strategic={force_strategic}); falling through to tactical"
                    )
                    # An operator steering request that is pending but not yet due
                    # (min-interval / spawn-cooldown floor) is held, not dropped —
                    # surface that once so it isn't a silent swallow.
                    await _maybe_notify_operator_refresh_deferred(
                        project,
                        session,
                        "queued behind a recent strategic cycle (spawn cooldown not yet elapsed)",
                    )
                    is_strategic = False

    if is_strategic:
        # Determine if this strategic cycle should also include architecture review.
        # Architecture stabilizes faster than strategy, so it runs less frequently.
        strategic_cycle_count = (project.vision or {}).get(
            "_strategic_cycle_count", 0
        ) + 1
        arch_frequency = _runtime_config.get("architecture_review_frequency", 3)
        is_architecture_cycle = (
            arch_frequency > 0 and strategic_cycle_count % arch_frequency == 0
        )

        spawned = False
        try:
            if is_architecture_cycle:
                # Full pipeline: Phase 1 (architecture) → Phase 2 (strategic) via completion handler
                from app.strategic_spawning import _spawn_design_doc_update

                spawned = await _spawn_design_doc_update(project, session)
                if spawned:
                    logger.info(
                        f"[AUTONOMOUS] Spawned design doc update (strategic cycle #{strategic_cycle_count}, "
                        f"vision rev {(project.vision or {}).get('revision', 0)})"
                    )
            else:
                # Strategy-only: skip architecture, go straight to strategic review
                from app.strategic_spawning import _spawn_strategic_review

                spawned = await _spawn_strategic_review(project, session)
                if spawned:
                    logger.info(
                        f"[AUTONOMOUS] Spawned strategic review without architecture "
                        f"(cycle #{strategic_cycle_count}, arch runs every {arch_frequency})"
                    )
        except PayloadTooLargeError as e:
            # The strategic payload outgrew the 256 KB cap (problem_model + design_doc
            # carry most of the weight). Without this catch, the exception bubbles up
            # to _check_idle_research's outer except and silently kills *all* autonomy
            # (including tactical research) for as long as the strategic context stays
            # bloated. Fall through so tactical research continues, and surface a
            # dedup'd alert so the operator knows strategic refresh is paused.
            logger.warning(
                f"[STRATEGIC] Skipping strategic spawn for project {project.id}: {e}. "
                f"Tactical research will continue."
            )
            existing_alert = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == project.id,
                            Alert.title
                            == "Strategic review skipped: payload too large",
                            Alert.dismissed_at.is_(None),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if not existing_alert:
                max_kb = _MAX_PAYLOAD_BYTES // 1024
                session.add(
                    Alert(
                        project_id=project.id,
                        severity=AlertSeverity.WARNING,
                        title="Strategic review skipped: payload too large",
                        message=(
                            f"Strategic spawn payload exceeds the {max_kb} KB limit "
                            f"({e.size} bytes, top contributors: "
                            f"{', '.join(f'{k}={n}B' for k, n in e.top_keys[:3])}). "
                            "Tactical research continues, but strategic refresh is paused "
                            "until the design_doc/problem_model/conversation_thread shrink."
                        ),
                    )
                )
            # Write a machine-readable marker so the strategic-stall signal
            # can distinguish a payload-bloat stall from a scheduling stall
            # and redirect diagnosis toward compaction rather than stuck tasks.
            bloat_size = e.size
            bloat_top = [{"key": k, "size": n} for k, n in e.top_keys[:5]]

            def _mark_payload_bloat_skip(vision: dict) -> None:
                vision["_strategic_payload_bloat_skip"] = {
                    "skipped_at": _utcnow().isoformat(),
                    "size": bloat_size,
                    "top_contributors": bloat_top,
                }

            await with_vision_locked(session, project.id, _mark_payload_bloat_skip)
            await session.commit()
            # A pending operator steering marker survives this skip (the spawn
            # never happened, so it is not consumed). Tell the operator their
            # steering is paused on payload bloat rather than leaving it silent.
            await _maybe_notify_operator_refresh_deferred(
                project,
                session,
                "strategic refresh paused — context payload exceeds the size limit",
            )

        if spawned:
            research_spawn_count += 1

            # Capture, inside the lock, whether the consumed marker was
            # operator-origin — read BEFORE clear_strategic_refresh wipes it so
            # the symmetric success notice can fire only for operator steering.
            _consumed: dict = {}

            def _record_strategic_spawn(vision: dict) -> None:
                vision["_research_spawn_count"] = research_spawn_count
                vision["_strategic_cycle_count"] = strategic_cycle_count
                # Clear any stale payload-bloat skip marker now that a strategic
                # spawn succeeded (payload shrank back under the cap). Otherwise
                # the marker persists in vision and the strategic-health panel +
                # stall investigation keep flagging a bloat condition that has
                # already recovered.
                vision.pop("_strategic_payload_bloat_skip", None)
                # Consume any pending strategic-refresh marker — this spawn
                # satisfies the owed refresh (and stamps the fire time so the
                # min-interval floor measures refresh-driven fires).
                from app.strategic_trigger import (
                    ORIGIN_OPERATOR,
                    clear_strategic_refresh,
                    strategic_refresh_excerpt,
                    strategic_refresh_origin,
                )

                _consumed["operator"] = (
                    strategic_refresh_origin(vision) == ORIGIN_OPERATOR
                )
                # Capture the persisted steering excerpt BEFORE the marker is
                # wiped: the success confirmation must fire only when the
                # operator's message text was actually preserved (otherwise the
                # woken cycle has no directive to act on).
                _consumed["excerpt"] = strategic_refresh_excerpt(vision)
                clear_strategic_refresh(vision, now)

            await with_vision_locked(session, project.id, _record_strategic_spawn)
            # 2026-05-03: commit BEFORE the tactical-research early-returns
            # below. Without this, when a tactical-research cap (max_parallel
            # / max_total / global rate-limit) fires, the function returns
            # without committing the strategic-counter increment. Next tick
            # reads the OLD count, sees count % strategic_frequency == 0
            # again, fires another strategic spawn. Even with the
            # has_active_strategic guard above, eventually that strategic
            # task completes and the next tick re-fires immediately.
            await session.commit()

            # Symmetric success counterpart to the deferral notice: when the
            # consumed marker was operator-origin AND carried preserved steering
            # text, confirm the strategist woke and is acting on the steering.
            # Without an excerpt the woken cycle has no directive to act on, so
            # suppress the confirmation rather than promise steering that the
            # strategist cannot actually see. Best-effort — never fatal to the
            # spawn that already succeeded and committed above.
            if _consumed.get("operator") and _consumed.get("excerpt"):
                await _maybe_notify_operator_steering_acted(project, session)

    # Guard: limit parallel research tasks per project.
    # EVALUATING parents are just waiting on children — they don't block new research.
    max_parallel = _runtime_config.get("max_parallel_research_per_project", 3)
    active_research_count = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == project.id,
                Task.status.in_(
                    [
                        TaskStatus.QUEUED,
                        TaskStatus.PROVISIONING,
                        TaskStatus.IN_PROGRESS,
                    ]
                ),
                Task.payloads[0]["type"].astext == "research",
            )
        )
    ).scalar() or 0
    if active_research_count >= max_parallel:
        return
    # Second cap: total research in any non-terminal state (including
    # EVALUATING). Pending rollups in EVALUATING accumulate merge complexity
    # — each new spawn produces another rollup branch competing for the same
    # base. This cap prevents the pile-up that makes downstream merges
    # incrementally more conflict-prone.
    max_total = _runtime_config.get("max_total_research_per_project", 5)
    total_research_count = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == project.id,
                Task.status.in_(
                    [
                        TaskStatus.QUEUED,
                        TaskStatus.PROVISIONING,
                        TaskStatus.IN_PROGRESS,
                        TaskStatus.EVALUATING,
                    ]
                ),
                Task.payloads[0]["type"].astext == "research",
            )
        )
    ).scalar() or 0
    if total_research_count >= max_total:
        logger.info(
            f"[AUTONOMOUS] Total research cap reached: {total_research_count}/{max_total} "
            f"(includes EVALUATING) — skipping spawn for project {project.id}"
        )
        return
    # Reduce chosen_directions to the smaller of the two remaining budgets
    remaining_slots = min(
        max_parallel - active_research_count, max_total - total_research_count
    )
    chosen_directions = chosen_directions[:remaining_slots]
    # Guard: global rate limit across ALL projects
    one_hour_ago = _utcnow() - timedelta(hours=1)
    global_max = _runtime_config.get(
        "max_global_autonomous_tasks_per_hour",
        _runtime_config["max_autonomous_tasks_per_hour"] * 10,
    )
    global_recent = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.created_by == "ORCHESTRATOR",
                Task.created_at >= one_hour_ago,
            )
        )
    ).scalar() or 0
    if global_recent >= global_max:
        logger.warning(
            f"[AUTONOMOUS] Global rate limit reached: {global_recent}/{global_max} tasks/hour"
        )
        return
    # Guard: per-project rate limit for research spawns
    recent_research_count = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == project.id,
                Task.created_by == "ORCHESTRATOR",
                Task.created_at >= one_hour_ago,
                Task.payloads[0]["type"].astext == "research",
            )
        )
    ).scalar() or 0
    if recent_research_count >= _runtime_config["max_autonomous_tasks_per_hour"]:
        return

    # Also cap by per-project rate limit
    rate_limit_remaining = (
        _runtime_config["max_autonomous_tasks_per_hour"] - recent_research_count
    )
    if rate_limit_remaining < len(chosen_directions):
        chosen_directions = chosen_directions[: max(0, rate_limit_remaining)]

    # If no active directions exist (e.g., all paused), only strategic runs (already spawned above).
    if not chosen_directions and not active_directions:
        return

    # Autonomous research creates independent tasks (no parent_task_id), so they
    # must always branch from the integration trunk.  Staging branches
    # (research/*, plan/*) are ephemeral — they get deleted when their parent's
    # rollup merges.  An independent task using someone else's staging branch
    # will break when that rollup cleans up.  Actual subresearch (spawned by
    # _handle_research_completion with parent_task_id set) correctly inherits
    # its parent's staging branch.
    base_branch = _get_project_integration_branch(project)

    # Use per-type defaults
    research_model = _runtime_config.get("task_type_models", {}).get("research") or None
    research_effort = (
        _runtime_config.get("task_type_efforts", {}).get("research") or None
    )
    research_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("research")
    # Every Nth cycle is "undirected" for cross-cutting analysis.
    undirected_frequency = _runtime_config.get("undirected_research_frequency", 12)
    is_undirected = (
        undirected_frequency > 0
        and research_spawn_count > 0
        and research_spawn_count % undirected_frequency == 0
    )
    # Don't run undirected on strategic cycles
    if is_strategic:
        is_undirected = False

    # Handle undirected cycle (only first slot, rest are directed)
    anomalies = None
    if is_undirected:
        anomalies = await _compute_research_anomalies(project.id, session)

    # Check anomaly-driven pipeline adjustments (apply or restore) after anomaly tracking
    anomaly_action = await check_anomaly_actions()
    if anomaly_action["action"] != "none":
        from app.broadcast import broadcast_to_project
        from app.notification_dispatch import dispatch_external_notification

        await broadcast_to_project(
            str(project.id),
            "anomaly_adjustment",
            anomaly_action,
        )
        if anomaly_action["action"] == "adjusted" and anomaly_action.get("is_new"):
            pre_vms = anomaly_action.get("pre_adjustment_max_vms") or "?"
            new_vms = anomaly_action["max_concurrent_vms"]
            gate = anomaly_action["failure_gate_threshold"]
            triggers = ", ".join(anomaly_action.get("trigger_anomalies") or [])
            alert = Alert(
                project_id=project.id,
                severity=AlertSeverity.WARNING,
                title="Pipeline auto-adjusted: anomaly threshold exceeded",
                message=(
                    f"max_concurrent_vms reduced from {pre_vms} to {new_vms}, "
                    f"failure_gate_threshold tightened to {gate}. "
                    f"Triggered by: {triggers}."
                ),
            )
            session.add(alert)
            asyncio.ensure_future(  # noqa: RUF006
                dispatch_external_notification(
                    str(project.id),
                    "ANOMALY_ADJUSTMENT",
                    {
                        "max_concurrent_vms": new_vms,
                        "failure_gate_threshold": gate,
                        "trigger_anomalies": anomaly_action.get("trigger_anomalies")
                        or [],
                        "pre_adjustment_max_vms": pre_vms,
                    },
                )
            )
        elif anomaly_action["action"] == "restored":
            new_vms = anomaly_action["max_concurrent_vms"]
            gate = anomaly_action["failure_gate_threshold"]
            alert = Alert(
                project_id=project.id,
                severity=AlertSeverity.INFO,
                title="Pipeline restored: anomaly conditions cleared",
                message=(
                    f"max_concurrent_vms restored to {new_vms}, "
                    f"failure_gate_threshold restored to {gate}."
                ),
            )
            session.add(alert)
            asyncio.ensure_future(  # noqa: RUF006
                dispatch_external_notification(
                    str(project.id),
                    "ANOMALY_RESTORED",
                    {
                        "max_concurrent_vms": new_vms,
                        "failure_gate_threshold": gate,
                    },
                )
            )

    # Build and spawn research tasks — one per chosen direction (or one undirected)
    directions_to_spawn = (
        chosen_directions if chosen_directions else [None]
    )  # None = undirected
    if is_undirected:
        directions_to_spawn = [None]  # Undirected replaces directed for this cycle

    # Guard the cross-cutting (undirected) spawn against the re-trigger loop.
    # directions_to_spawn collapses to [None] both from the modulo `is_undirected`
    # trigger and from the chosen_directions-empty fallback (which fires nearly
    # every idle cycle when the few active directions are busy). If a cross-cutting
    # research is already active or was spawned within the cooldown, skip this
    # cycle entirely rather than pile on a near-identical analysis whose findings
    # would only dedupe to ~0 tasks. (See _undirected_research_recently_active.)
    if directions_to_spawn == [None] and await _undirected_research_recently_active(
        project.id, session
    ):
        logger.info(
            f"[AUTONOMOUS] Skipping cross-cutting research spawn for project "
            f"{project.id} — an undirected research task is already active or was "
            f"spawned within the cooldown window."
        )
        # Spawn nothing this cycle (rather than pile on a redundant analysis).
        # Fall through to the normal commit so any pending session state from
        # this cycle (e.g. an anomaly-adjustment Alert added above) still lands.
        directions_to_spawn = []

    spawned_tasks = []
    for chosen_direction in directions_to_spawn:
        _mode = normalized_recommended_mode(chosen_direction)
        # Build lightweight direction summaries — include compact history for
        # agent-side stats (aggregateDirectionStats, buildScopeCalibrationSignal).
        # Full histories are available via the API. This keeps the payload under
        # the OS E2BIG limit (~128KB).
        _HISTORY_NUMERIC_FIELDS = {
            "tasks_created",
            "prs_merged",
            "tasks_failed",
            "failed_task_details",
            "scope_breakdown",
            "avg_time_to_merge_hours",
            "median_review_iterations",
            "avg_review_iterations",
        }
        _AGGREGATE_FIELDS = {
            "total_cycles",
            "total_tasks_created",
            "total_prs_merged",
            "total_tasks_failed",
            "total_prs_rejected",
        }
        current_dir_id = (
            None if chosen_direction is None else chosen_direction.get("id")
        )
        direction_summaries = []
        for d in all_active_directions:
            summary: dict = {
                "id": d.get("id"),
                "text": d.get("text"),
                "priority": d.get("priority"),
                "system_id": d.get("system_id"),
                "possibly_saturated": d.get("possibly_saturated", False),
                "possibly_complete": d.get("possibly_complete", False),
            }
            if d.get("conflicts_with"):
                summary["conflicts_with"] = d["conflicts_with"]
            if d.get("completion_criteria"):
                summary["completion_criteria"] = d["completion_criteria"]
            if d.get("category"):
                summary["category"] = d["category"]
            # Include compact research context (truncated) — not full history
            ctx = d.get("research_context")
            if ctx:
                summary["research_context"] = ctx[:2000]
            # Include structured findings evidence for the strategic agent
            rfe = d.get("recent_findings_evidence")
            if rfe:
                summary["recent_findings_evidence"] = rfe
            # Include compact history for agent-side analysis functions
            raw_history = d.get("research_history") or []
            raw_agg = d.get("history_aggregate") or {}
            agg_compact = {k: raw_agg[k] for k in _AGGREGATE_FIELDS if k in raw_agg}
            if agg_compact:
                summary["history_aggregate"] = agg_compact
            if d.get("id") == current_dir_id:
                # Current direction: last 3 cycles with numeric fields only
                compact_history = [
                    {k: entry[k] for k in _HISTORY_NUMERIC_FIELDS if k in entry}
                    for entry in raw_history[-3:]
                ]
                if compact_history:
                    summary["research_history"] = compact_history
                # Rolling median of per-cycle median_review_iterations across last 3 cycles
                recent_medians = [
                    entry["median_review_iterations"]
                    for entry in raw_history[-3:]
                    if isinstance(entry.get("median_review_iterations"), (int, float))
                ]
                if recent_medians:
                    sorted_m = sorted(recent_medians)
                    n = len(sorted_m)
                    summary["rolling_median_review_iterations"] = (
                        sorted_m[n // 2]
                        if n % 2
                        else (sorted_m[n // 2 - 1] + sorted_m[n // 2]) / 2
                    )
            direction_summaries.append(summary)

        # Design doc summary — just keys and playbook, not full content.
        # Agent can fetch specific sections via ./design-doc get <key>.
        doc = project.design_doc or {}
        design_doc_summary: dict = {
            "_keys": [
                k
                for k in doc
                if not k.startswith("_") and not k.startswith("playbook/")
            ],
        }
        # Include playbook entries (small, operationally critical).
        # Skip pending_source entries — those are mid-flight writes whose
        # source task hasn't completed yet. See main.py update_playbook for
        # the anti-poisoning rationale.
        for k, v in doc.items():
            if k.startswith("playbook/") and isinstance(v, dict):
                if v.get("pending_source"):
                    continue
                design_doc_summary[k] = v.get("content", "")

        # Verify design-doc system module paths against the integration-branch
        # file tree before trusting their status claims. Read-only/advisory: the
        # stored design_doc is NOT mutated — we only annotate the payload so the
        # research agent reasons about what actually exists instead of
        # confabulating phantom code behind a status it trusts. Flat storage
        # shape: each ``systems/<id>`` key is a dict carrying a ``modules`` list.
        # Sorted iteration keeps the annotation deterministic for identical input.
        verification_kwargs = await resolve_branch_verification_context(
            session, project
        )
        system_verification: dict = {}
        for dkey in sorted(
            k for k in doc if isinstance(k, str) and k.startswith("systems/")
        ):
            dval = doc[dkey]
            if not isinstance(dval, dict):
                continue
            modules = [m for m in (dval.get("modules") or []) if isinstance(m, str)]
            if not modules:
                continue
            present, missing = await classify_existing_paths(
                modules, **verification_kwargs
            )
            if not missing:
                status = "verified"
            elif present:
                status = "partial"
            else:
                # Every declared module is absent from the integration branch:
                # the system is claimed but its backing code does not exist.
                status = "claimed_absent"
            # Symbol-level depth: a present file can still have lost the cited
            # symbol. An unambiguously-absent symbol gets the same treat-as-missing
            # verdict a fully-absent file already receives.
            absent_symbols = await detect_absent_symbols(
                dval.get("modules"),
                [dval.get("description"), dval.get("notes")],
                **verification_kwargs,
            )
            if absent_symbols and status != "claimed_absent":
                status = "claimed_absent"
            system_verification[dkey[len("systems/"):]] = {
                "status": status,
                "claimed_status": str(dval.get("status", "")),
                "present": present,
                "missing": missing,
                **({"absent_symbols": absent_symbols} if absent_symbols else {}),
            }

        # Verify design-doc CAPABILITIES the same way as systems above. The
        # research agent reasons about — and proposes improvements to —
        # capability behavior; a capability claimed working/partial whose every
        # backing path is absent on the integration branch describes code that
        # does not exist. The backend's research-side loop historically covered
        # only systems/, leaving this gap. Mirror the strategic-side
        # reconciliation: only PRESENT-status capabilities (working/partial) are
        # checked, only when the entry carries at least one path pointer (in
        # ``modules`` or embedded in ``notes``/``description`` free text), and an
        # all-absent entry is flagged ``claimed_absent`` while a partial hit is
        # left ``partial`` (conservative). Read-only/advisory: design_doc is
        # never mutated; the agent renders this as a prominent prompt section
        # (buildCapabilityVerificationSection).
        from app.playbook_lifecycle import _extract_file_paths

        _present_cap_statuses = {"working", "partial"}
        capability_verification: dict = {}
        for dkey in sorted(
            k for k in doc if isinstance(k, str) and k.startswith("capabilities/")
        ):
            dval = doc[dkey]
            if not isinstance(dval, dict):
                continue
            claimed = str(dval.get("status", "")).lower()
            if claimed not in _present_cap_statuses:
                continue  # planned/missing/stub already tells the agent to build
            cap_paths = [m for m in (dval.get("modules") or []) if isinstance(m, str)]
            for _field in ("notes", "description"):
                _txt = dval.get(_field)
                if isinstance(_txt, str) and _txt:
                    cap_paths.extend(_extract_file_paths(_txt))
            cap_paths = list(dict.fromkeys(cap_paths))
            if not cap_paths:
                continue  # no path pointer → absence is not evidence of absent code
            present, missing = await classify_existing_paths(
                cap_paths, **verification_kwargs
            )
            if not missing:
                status = "verified"
            elif present:
                status = "partial"
            else:
                status = "claimed_absent"
            # Symbol-level depth (see systems loop): flag a cited symbol that is
            # unambiguously gone even though its file is present.
            absent_symbols = await detect_absent_symbols(
                dval.get("modules"),
                [dval.get("notes"), dval.get("description")],
                **verification_kwargs,
            )
            if absent_symbols and status != "claimed_absent":
                status = "claimed_absent"
            capability_verification[dkey[len("capabilities/"):]] = {
                "status": status,
                "claimed_status": str(dval.get("status", "")),
                "present": present,
                "missing": missing,
                **({"absent_symbols": absent_symbols} if absent_symbols else {}),
            }

        research_payload: dict = {
            "type": "research",
            "directions": direction_summaries,
            "current_direction_id": None
            if chosen_direction is None
            else chosen_direction.get("id"),
            "design_doc": design_doc_summary,
            "base_branch": base_branch,
            **({"model": research_model} if research_model else {}),
            **({"effort": research_effort} if research_effort else {}),
            **({"prompt_suffix": research_suffix} if research_suffix else {}),
        }
        if system_verification:
            research_payload["system_verification"] = system_verification
        if capability_verification:
            research_payload["capability_verification"] = capability_verification
        if chosen_direction and chosen_direction.get("category") == "product":
            append_prompt_suffix(
                research_payload,
                "This direction targets a product feature. Prioritize user-visible changes, "
                "end-user experience improvements, and direct product value. Structural work "
                "is acceptable only when it directly enables the product feature.",
            )
        _mode_hint = mode_prompt_hint(_mode)
        if chosen_direction and _mode_hint:
            append_prompt_suffix(research_payload, _mode_hint)
        # Inject failure-mode coaching derived from direction's first-attempt rejection history
        if chosen_direction:
            from app.spawn_calibration import _get_failure_coaching_suffix

            _dir_id = chosen_direction.get("id")
            _coaching = (
                _get_failure_coaching_suffix(str(_dir_id), project.design_doc or {})
                if _dir_id
                else None
            )
            if _coaching:
                append_prompt_suffix(research_payload, _coaching)
        # Surface 'what has compounded here' from prior playbook/success-research-* entries.
        if "research" in RECENT_SUCCESSES_TASK_TYPES:
            _recent_dir_id = chosen_direction.get("id") if chosen_direction else None
            _recent = summarize_recent_successes(
                project.design_doc or {},
                "research",
                _recent_dir_id,
                None,
            )
            if _recent:
                append_prompt_suffix(research_payload, _recent)
        # Pre-extract design doc context for the current direction's system_id
        if chosen_direction:
            dir_system_id = chosen_direction.get("system_id")
            if dir_system_id and project.design_doc:
                doc = project.design_doc
                doc_systems = doc.get("systems", [])
                doc_capabilities = doc.get("capabilities", {})
                relevant: dict = {"system_id": dir_system_id}
                for sys in doc_systems if isinstance(doc_systems, list) else []:
                    if isinstance(sys, dict) and sys.get("id") == dir_system_id:
                        relevant["system"] = sys
                        break
                if isinstance(doc_capabilities, dict):
                    related_caps = {
                        cap_name: cap_val
                        for cap_name, cap_val in doc_capabilities.items()
                        if isinstance(cap_val, dict)
                        and cap_val.get("status") in ("missing", "partial")
                    }
                    if related_caps:
                        relevant["missing_capabilities"] = related_caps
                # Surface the current direction's system verification verdict
                # inline so the agent sees claimed-but-absent / partial code
                # right next to the architecture context it would otherwise trust.
                if dir_system_id in system_verification:
                    relevant["verification"] = system_verification[dir_system_id]
                research_payload["direction_design_context"] = relevant
            if chosen_direction.get("possibly_complete"):
                research_payload["direction_possibly_complete"] = True
                research_payload["completion_suggestion"] = chosen_direction.get(
                    "completion_suggestion", ""
                )
            if current_dir_id:
                _dir_memos = await _fetch_direction_reasoning_memos(
                    project.id, current_dir_id, session
                )
                if _dir_memos:
                    research_payload["direction_reasoning_memos"] = _dir_memos
        if chosen_direction is None:
            if anomalies:
                research_payload["anomaly_signals"] = anomalies
            direction_text = CROSS_CUTTING_DIRECTION_TEXT
            logger.info(
                f"[AUTONOMOUS] Spawning UNDIRECTED research cycle (cycle #{research_spawn_count})"
            )
        else:
            direction_text = (chosen_direction.get("text") or "general improvement")[
                :200
            ]

        if project.build_config:
            research_payload["build_config"] = project.build_config
        # Compact, body-omitted index of existing knowledge documents
        # (web_research / design output), scoped to this project and — when the
        # cycle is directed — the current direction first. Lets the research
        # agent ground findings in knowledge that already exists and fetch full
        # bodies on demand. Included only when documents exist.
        _knowledge_index = await build_knowledge_index(
            session, project.id, direction_id=current_dir_id
        )
        if _knowledge_index:
            research_payload["knowledge_index"] = _knowledge_index
        research_payload = validate_internal_payload(
            research_payload, "autonomous_research:research"
        )

        # Recover deferred findings from recent completed research tasks.
        task_context: dict = {}
        prev_research_rows = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.project_id == project.id,
                        Task.status == TaskStatus.COMPLETED,
                    )
                    .order_by(Task.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        prev_research_rows = [
            r for r in prev_research_rows if (r.context or {}).get("deferred_findings")
        ]
        from app.finding_deduplication import finding_hash as _finding_hash

        _deferred_ttl_days = int(_runtime_config.get("deferred_finding_ttl_days", 7))
        _ttl_cutoff = _utcnow() - timedelta(days=_deferred_ttl_days)
        dir_deferred: list[dict] = []
        seen_hashes: set[str] = set()
        _expired_count = 0
        for prev_row in prev_research_rows:
            for f in (prev_row.context or {}).get("deferred_findings", []):
                if not isinstance(f, dict):
                    continue
                f_dir_id = f.get("direction_id")
                if current_dir_id:
                    if f_dir_id and f_dir_id != current_dir_id:
                        continue
                else:
                    if f_dir_id:
                        continue
                # Expire stale findings (originally researched too long ago)
                _orig_at = f.get("originally_researched_at")
                if _orig_at:
                    try:
                        _orig_dt = datetime.fromisoformat(_orig_at)
                        if _orig_dt.tzinfo is None:
                            _orig_dt = _orig_dt.replace(tzinfo=timezone.utc)
                        if _orig_dt < _ttl_cutoff:
                            _expired_count += 1
                            continue
                    except (ValueError, TypeError):
                        pass
                # Prefer finding_hash for dedup; fall back to computed hash
                fhash = f.get("finding_hash") or _finding_hash(
                    f.get("title", ""), f.get("description", "")
                )
                if fhash not in seen_hashes:
                    dir_deferred.append(f)
                    seen_hashes.add(fhash)
        # Prioritize findings that have been deferred the most times — they keep
        # getting bumped and should execute first when capacity returns.
        dir_deferred.sort(key=lambda f: int(f.get("deferred_count", 0)), reverse=True)
        if _expired_count:
            logger.info(
                f"[AUTONOMOUS] Expired {_expired_count} stale deferred finding(s) "
                f"(older than {_deferred_ttl_days}d)"
            )
        if dir_deferred:
            task_context["prior_deferred_findings"] = dir_deferred
            logger.info(
                f"[AUTONOMOUS] Injecting {len(dir_deferred)} deferred finding(s) from previous "
                f"research cycle(s) into new research task for direction {current_dir_id or 'undirected'}"
            )

        new_task = Task(
            project_id=project.id,
            title=f"Research: {direction_text}",
            description=f"Autonomous research for: {direction_text}",
            payloads=[research_payload],
            context=task_context,
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        session.add(new_task)
        # NOTE: counter is NOT incremented here. _research_spawn_count is
        # actually a research-COMPLETION counter despite the name — the
        # strategic_research_frequency gate at autonomous_research.py:384 is
        # meant to fire after N actual research findings have landed, not
        # after N tasks were queued. Incrementing on spawn caused 3
        # back-to-back strategic reviews on the 2026-05-03 dogfood run because
        # the very first strategic_review's Phase 2 spawned ~4 research tasks
        # AT ONCE, the spawn loop bumped the counter past the modulo trigger,
        # and the next iteration of _check_idle_research immediately spawned
        # strategic review #2 with zero findings to consume. The increment
        # now lives in _handle_research_completion (research_completion.py).
        history = TaskHistory(
            task_id=new_task.id,
            action="SPAWNED",
            details={"spawned_by": "AUTONOMOUS_LOOP", "trigger": "post_merge_or_idle"},
        )
        session.add(history)
        spawned_tasks.append(new_task)

    await session.commit()

    # Track first task for HALF_OPEN → CLOSED transition
    if breaker["state"] == "HALF_OPEN" and spawned_tasks:
        breaker["half_open_task_id"] = str(spawned_tasks[0].id)

    for t in spawned_tasks:
        logger.info(
            f"[AUTONOMOUS] Spawned research task {t.id} for project {project.id}"
        )
    if len(spawned_tasks) > 1:
        logger.info(
            f"[AUTONOMOUS] Spawned {len(spawned_tasks)} parallel research tasks for project {project.id}"
        )


async def _spawn_periodic_task(project: Project, task_type: str, session: AsyncSession):
    """Spawn a periodic non-research task (qa, security_audit, deps) if its schedule is due."""
    from app.alert_dispatch import _maybe_create_circuit_breaker_alert
    from sqlalchemy import func

    # Guard: task_type must be a known type
    if task_type not in ALL_TASK_TYPES:
        logger.warning(
            f"[AUTONOMOUS] Unknown task_type '{task_type}' in _spawn_periodic_task, skipping"
        )
        return

    # Guard: circuit breaker must allow spawning
    breaker = _shared_state.get_circuit_breaker(str(project.id))
    if breaker["state"] == "OPEN":
        if (
            breaker["opened_at"]
            and (_utcnow() - breaker["opened_at"]).total_seconds()
            >= breaker["cooldown_seconds"]
        ):
            breaker["state"] = "HALF_OPEN"
            logger.warning(
                f"[CIRCUIT BREAKER] OPEN → HALF-OPEN for {project.id} (cooldown elapsed, allowing one test task)"
            )
        else:
            logger.warning(
                f"[CIRCUIT BREAKER] Skipping periodic task spawn (type={task_type}) for project={project.id} "
                f"— circuit breaker state=OPEN (still in cooldown)"
            )
            return  # Still in cooldown
    elif breaker["state"] == "HALF_OPEN":
        test_id = breaker.get("half_open_task_id")
        if test_id:
            test_task = await session.get(Task, uuid.UUID(test_id))
            if (
                test_task
                and test_task.started_at
                and (_utcnow() - test_task.started_at).total_seconds() > 1800
            ):
                logger.warning(
                    f"[CIRCUIT BREAKER] HALF_OPEN test task {test_id} stuck for 30m+, treating as failure → OPEN"
                )
                breaker["state"] = "OPEN"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
                breaker["half_open_task_id"] = None
                await _maybe_create_circuit_breaker_alert(
                    project.id,
                    session,
                    f"HALF_OPEN test task stuck for 30m+ — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)",
                )
                return

    periodic_cfg = _runtime_config.get("autonomous_periodic_tasks", {}).get(
        task_type, {}
    )
    if not periodic_cfg.get("enabled", False):
        return

    interval_hours = periodic_cfg.get("interval_hours", 168)

    # Guard: no task of this type already QUEUED/PROVISIONING/IN_PROGRESS
    active_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for t in active_tasks:
        if (t.first_payload).get("type") == task_type:
            return

    # Guard: global rate limit across ALL projects
    one_hour_ago = _utcnow() - timedelta(hours=1)
    global_max = _runtime_config.get(
        "max_global_autonomous_tasks_per_hour",
        _runtime_config["max_autonomous_tasks_per_hour"] * 10,
    )
    global_recent = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.created_by == "ORCHESTRATOR",
                Task.created_at >= one_hour_ago,
            )
        )
    ).scalar() or 0
    if global_recent >= global_max:
        logger.warning(
            f"[AUTONOMOUS] Global rate limit reached: {global_recent}/{global_max} tasks/hour"
        )
        return
    # Guard: per-project rate limit
    recent_auto = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == project.id,
                Task.created_by == "ORCHESTRATOR",
                Task.created_at >= one_hour_ago,
            )
        )
    ).scalar() or 0
    if recent_auto >= _runtime_config["max_autonomous_tasks_per_hour"]:
        return

    # Check when this type last ran (most recent completed task of this type)
    completed_tasks = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.status == TaskStatus.COMPLETED,
                )
                .order_by(Task.created_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    last_run = None
    for t in completed_tasks:
        if (t.first_payload).get("type") == task_type:
            last_run = t.created_at
            break

    now = _utcnow()
    if last_run is not None:
        elapsed = (now - last_run).total_seconds() / 3600
        if elapsed < interval_hours:
            return

    # Infer base_branch from the most recent task in this project
    recent_task = (
        (
            await session.execute(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    _integration_branch = _get_project_integration_branch(project)
    base_branch = (
        (recent_task.first_payload).get("base_branch", _integration_branch)
        if recent_task
        else _integration_branch
    )

    task_model = _runtime_config.get("task_type_models", {}).get(task_type) or None
    task_effort = _runtime_config.get("task_type_efforts", {}).get(task_type) or None
    # agent_prompt_suffixes: supported config key — dict of task_type → prompt suffix string,
    # appended to the agent prompt for matching task types (validated in admin PATCH endpoint).
    task_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get(task_type)
    payload: dict = {
        "type": task_type,
        "base_branch": base_branch,
        **({"model": task_model} if task_model else {}),
        **({"effort": task_effort} if task_effort else {}),
        **({"prompt_suffix": task_suffix} if task_suffix else {}),
    }
    payload = validate_internal_payload(
        payload, "autonomous_research:periodic:" + task_type
    )
    new_task = Task(
        project_id=project.id,
        title=f"{task_type.upper()} scan: autonomous periodic",
        description=f"Automated {task_type} task triggered by autonomous periodic scheduler",
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    history = TaskHistory(
        task_id=new_task.id,
        action="SPAWNED",
        details={
            "spawned_by": "AUTONOMOUS_LOOP",
            "trigger": "periodic_schedule",
            "task_type": task_type,
        },
    )
    session.add(history)
    await session.commit()
    await session.refresh(new_task)

    if breaker["state"] == "HALF_OPEN":
        breaker["half_open_task_id"] = str(new_task.id)

    logger.info(
        f"[AUTONOMOUS] Spawned periodic {task_type} task {new_task.id} for project {project.id}"
    )


async def _check_autonomous_research(owner: str, repo: str):
    """Post-merge hook: check if we should spawn a research task."""
    try:
        async with AsyncSessionLocal() as session:
            projects = (
                (
                    await session.execute(
                        select(Project).where(
                            Project.github_owner == owner,
                            Project.github_repo == repo,
                            Project.autonomous_enabled.is_(True),
                            # A paused project must not spawn new autonomous
                            # work even when autonomous_enabled is still True.
                            Project.paused.is_(False),
                            Project.not_archived(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not projects:
                return

            # Spawn if VM slots are available (PROVISIONING + IN_PROGRESS, not blocked QUEUED)
            from sqlalchemy import func

            vm_consuming = (
                await session.execute(
                    select(func.count())
                    .select_from(Task)
                    .where(
                        Task.status.in_(
                            [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
                        ),
                    )
                )
            ).scalar() or 0

            if vm_consuming < _runtime_config["max_concurrent_vms"]:
                for project in projects:
                    await _spawn_research_task(project, session)
    except Exception as e:
        logger.error(
            f"[AUTONOMOUS] Error checking research trigger for {owner}/{repo}: {e}",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Residual-fingerprint create-fix lane. Re-exported here so existing callers
# and tests that do ``from app.autonomous_research import ...`` keep working.
# The lane itself was re-homed to app.autonomous_research_residual when the
# self-repair apparatus (autonomous_research_investigations) was deleted; the
# residual create-fix lane is explicitly preserved.
# ---------------------------------------------------------------------------
from app.autonomous_research_residual import (  # noqa: E402
    _spawn_residual_fingerprint_remediation,
)



async def _check_idle_research():
    """Periodic check: spawn research if projects are idle with active directions."""
    from app.anomaly_detection import _expire_stale_proposals

    now = _utcnow()
    interval_minutes = _runtime_config.get("autonomous_research_interval_minutes", 30)
    if _shared_state.last_idle_check and (
        now - _shared_state.last_idle_check
    ) < timedelta(minutes=interval_minutes):
        return
    _shared_state.last_idle_check = now

    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import func

            # Expire stale proposals before processing
            await _expire_stale_proposals(session)

            projects = (
                (
                    await session.execute(
                        select(Project).where(
                            Project.autonomous_enabled.is_(True),
                            # Skip paused projects: pause blocks ALL new work
                            # (research, periodic, and residual remediation)
                            # regardless of the autonomous_enabled toggle.
                            Project.paused.is_(False),
                            Project.not_archived(),
                        )
                    )
                )
                .scalars()
                .all()
            )

            for project in projects:
                # Log pending proposals awaiting review
                pending_proposals = [
                    d
                    for d in (project.directions or [])
                    if d.get("status") == "proposed"
                ]
                if pending_proposals:
                    logger.info(
                        f"[DIRECTIONS] Project {project.name} has {len(pending_proposals)} pending direction proposals awaiting review"
                    )

                active_directions = [
                    d for d in (project.directions or []) if d.get("status") == "active"
                ]
                # Allow projects with no directions through if vision needs bootstrapping
                vision_needs_bootstrap = not (project.vision or {}).get("purpose")
                strategic_recovery_needed = not active_directions and (
                    (project.vision or {}).get("_strategic_failures", 0) < 3
                )
                if (
                    not active_directions
                    and not vision_needs_bootstrap
                    and not strategic_recovery_needed
                ):
                    continue

                # Spawn if VM slots are available — count only tasks consuming VMs,
                # not QUEUED tasks blocked on dependencies
                vm_consuming = (
                    await session.execute(
                        select(func.count())
                        .select_from(Task)
                        .where(
                            Task.status.in_(
                                [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
                            ),
                        )
                    )
                ).scalar() or 0

                if vm_consuming < _runtime_config["max_concurrent_vms"]:
                    await _spawn_research_task(project, session)

                    for task_type in ("security_audit", "qa", "deps"):
                        await _spawn_periodic_task(project, task_type, session)

                # Spawn the working residual-fingerprint create-fix lane for any
                # recurring failure pattern that warrants a targeted fix.
                await _spawn_residual_fingerprint_remediation(project, session)
                await session.commit()
    except Exception as e:
        logger.error(f"[AUTONOMOUS] Error in idle research check: {e}", exc_info=True)
