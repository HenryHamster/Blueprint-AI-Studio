"""Autonomous research spawning functions.

This module handles all aspects of autonomous research task spawning:
- Research task selection and rate limiting
- Strategic cycle management (design doc update → strategic review)
- Periodic task scheduling (security_audit, qa, deps)
- Post-merge and idle research triggers
- Direction health, auto-pause, and auto-recovery
"""
import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    Task, Project, TaskHistory, TaskStatus, Alert, AlertSeverity,
)
from app.database import AsyncSessionLocal
from app.payload_schemas import validate_internal_payload, ALL_TASK_TYPES
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config
from app.max_plan_pool import _max_plan_pools, _account_available_for_model
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Per-project lock to prevent concurrent _spawn_research_task calls
# (post-merge hook and idle check can race on the same project)
_spawn_locks: dict[str, asyncio.Lock] = {}


def _get_spawn_lock(project_id: str) -> asyncio.Lock:
    if project_id not in _spawn_locks:
        _spawn_locks[project_id] = asyncio.Lock()
    return _spawn_locks[project_id]


async def _spawn_research_task(project: Project, session: AsyncSession):
    """Spawn a research task for a project if conditions are met.

    Uses a per-project lock to prevent races between the post-merge hook
    and the idle-check loop, which run concurrently with separate sessions.
    """
    async with _get_spawn_lock(str(project.id)):
        return await _spawn_research_task_inner(project, session)


async def _spawn_research_task_inner(project: Project, session: AsyncSession):
    """Inner implementation — always called under the per-project spawn lock."""
    from app.main import _maybe_create_circuit_breaker_alert  # noqa: PLC0415
    from app.task_lifecycle import _increment_research_spawn_count  # noqa: PLC0415
    from app.research_completion import _compute_momentum_factor, _compute_productivity_score, _compute_acceptance_rate  # noqa: PLC0415
    from app.anomaly_detection import _compute_research_anomalies, check_anomaly_actions  # noqa: PLC0415
    from app.direction_lifecycle import _process_direction_lifecycle  # noqa: PLC0415
    # Guard: autonomous must be enabled
    if not project.autonomous_enabled:
        return

    # Playbook auto-seed removed: the hardcoded seed entries were specific to the
    # Blueprint pipeline repo and polluted new projects with wrong build/test
    # instructions. Agents discover the correct playbook entries by exploring
    # the actual codebase and writing them via `./playbook set`.
    # Guard: don't spawn if user has no available accounts (avoids immediate failure)
    user_pool = _max_plan_pools.get(project.user_id, [])
    research_model = _runtime_config.get("task_type_models", {}).get("research")
    has_available = any(_account_available_for_model(a, research_model) for a in user_pool) or bool(os.getenv("ANTHROPIC_API_KEY"))
    if not has_available:
        logger.info(f"[AUTONOMOUS] Skipping research for {project.name} — no available accounts")
        return
    # Guard: circuit breaker must allow spawning
    breaker = _shared_state.get_circuit_breaker(str(project.id))
    if breaker["state"] == "OPEN":
        # Check if cooldown has elapsed → transition to HALF-OPEN
        if breaker["opened_at"] and (_utcnow() - breaker["opened_at"]).total_seconds() >= breaker["cooldown_seconds"]:
            breaker["state"] = "HALF_OPEN"
            logger.warning(f"[CIRCUIT BREAKER] OPEN → HALF-OPEN for {project.id} (cooldown elapsed, allowing one test task)")
        else:
            return  # Still in cooldown
    elif breaker["state"] == "HALF_OPEN":
        # Check if existing test task is stuck — treat as failure after 30 min
        test_id = breaker.get("half_open_task_id")
        if test_id:
            test_task = await session.get(Task, uuid.UUID(test_id))
            if test_task and test_task.started_at and (_utcnow() - test_task.started_at).total_seconds() > 1800:
                logger.warning(f"[CIRCUIT BREAKER] HALF_OPEN test task {test_id} stuck for 30m+, treating as failure → OPEN")
                breaker["state"] = "OPEN"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
                breaker["half_open_task_id"] = None
                await _maybe_create_circuit_breaker_alert(
                    project.id, session,
                    f"HALF_OPEN test task stuck for 30m+ — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)"
                )
                return  # Back to cooldown
            elif not test_task:
                logger.warning(f"[CIRCUIT BREAKER] HALF_OPEN test task {test_id} no longer exists (deleted/reaped) → OPEN")
                breaker["state"] = "OPEN"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
                breaker["half_open_task_id"] = None
                await _maybe_create_circuit_breaker_alert(
                    project.id, session,
                    f"HALF_OPEN test task deleted/reaped — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)"
                )
                return  # Back to cooldown
    # Guard: must have active directions that aren't in cooldown or mid-cycle
    cooldown_minutes = _runtime_config.get("autonomous_direction_cooldown_minutes", 240)
    now = _utcnow()

    # Build set of direction IDs that already have an active research cycle.
    # Include EVALUATING — the agent is done but children are still merging,
    # so the direction is still being worked on and shouldn't be re-spawned.
    busy_direction_ids: set = set()
    active_research = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([
                TaskStatus.QUEUED, TaskStatus.PROVISIONING,
                TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING,
            ]),
        )
    )).scalars().all()
    for t in active_research:
        payload = t.first_payload
        if payload.get("type") == "research":
            # current_direction_id identifies the targeted direction; null means
            # undirected/cross-cutting analysis which doesn't block any direction.
            current_did = payload.get("current_direction_id")
            if current_did:
                busy_direction_ids.add(current_did)

    active_directions = await _process_direction_lifecycle(
        project, session, cooldown_minutes, busy_direction_ids, now
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
                last_failure = datetime.fromisoformat(failures_at.replace('Z', '+00:00'))
                if (_utcnow() - last_failure).total_seconds() > 48 * 3600:
                    strategic_failures = 0
                    logger.info(f"[STRATEGIC] Auto-decayed _strategic_failures for project {project.id} (last failure: {failures_at})")
            except (ValueError, TypeError):
                pass  # malformed timestamp, keep current value
    # Check if ANY direction is active in the project (ignoring cooldown filter).
    # active_directions only contains non-cooldown directions, so a project with
    # active-but-cooling directions should not trigger a strategic cycle.
    any_active_in_project = any(d.get("status") == "active" for d in (project.directions or []))
    all_dirs_saturated = any_active_in_project and all(
        d.get("possibly_saturated") for d in (project.directions or []) if d.get("status") == "active"
    )
    force_strategic = strategic_failures < 3 and (
        not vision_data.get("purpose") or not any_active_in_project or all_dirs_saturated
    )
    if not active_directions and not force_strategic:
        return
    # Pick up to N non-conflicting directions for parallel research.
    # active_directions preserves UI drag-order (index 0 = highest priority).
    # Build vision priority lookup: directions tagged with a system_id that appears
    # earlier in vision.priorities get a lower (better) rank.
    vision_priorities = (vision_data.get("priorities") or []) if vision_data else []
    vision_priority_index: dict = {}
    for idx, sys_id in enumerate(vision_priorities):
        if isinstance(sys_id, str):
            vision_priority_index[sys_id] = idx

    def _vision_rank(d: dict) -> int:
        """0 = top vision priority, len = unlisted. Directions without system_id get neutral rank."""
        sid = d.get("system_id")
        if not sid:
            return len(vision_priorities)  # neutral — don't penalize untagged
        return vision_priority_index.get(sid, len(vision_priorities) + 1)  # unlisted slightly below neutral

    # Sort all eligible directions by priority for greedy selection
    chosen_directions: list[dict] = []
    if active_directions:
        import random
        priority_rank = {"high": 0, "medium": 1, "low": 2}

        def _direction_score(d: dict) -> tuple:
            productivity_score = _compute_productivity_score(d)
            momentum = _compute_momentum_factor(d)
            acceptance_rate = _compute_acceptance_rate(d)
            is_new = 0 if d.get("last_researched_at") else -1  # small novelty bonus
            category_bonus = -1 if d.get("category") == "product" else 0  # product directions preferred
            # Vision rank matters: top vision priority = 0, unlisted = N.
            # Placed after priority+category but BEFORE productivity so the
            # strategic agent's vision.priorities actually steer selection
            # instead of being a dead tiebreaker.
            v_rank = _vision_rank(d)
            return (
                priority_rank.get(d.get("priority", "medium"), 1),
                category_bonus,                                  # product category within same priority
                is_new,                                          # novelty bonus within same priority
                v_rank,                                          # vision priority drives selection
                -(productivity_score * momentum),                # productivity within same vision rank
                acceptance_rate if acceptance_rate is not None else 0.5,  # low acceptance = deprioritized
                1 if d.get("possibly_saturated") else 0,         # saturated last
                d.get("last_researched_at") or "",               # staleness
            )

        active_directions.sort(key=_direction_score)
        sorted_eligible = active_directions

        # Greedy selection: pick non-conflicting directions up to the parallel cap.
        # conflicts_with is a mutual exclusion list — if A lists B, they can't run
        # at the same time regardless of which was picked first.
        blocked_ids: set = set(busy_direction_ids)
        # Expand blocked set from already-running directions' conflicts
        all_dir_map = {d.get("id"): d for d in (project.directions or []) if d.get("id")}
        for did in busy_direction_ids:
            d = all_dir_map.get(did)
            if d:
                blocked_ids.update(d.get("conflicts_with") or [])
        for d in (project.directions or []):
            if any(rid in (d.get("conflicts_with") or []) for rid in busy_direction_ids):
                blocked_ids.add(d.get("id"))

        # Cross-instance serialization: Cloud Run runs up to N orchestrator
        # instances (maxScale=3 currently), each firing its own idle-research
        # cycle.  Without this lock, instances race — each reads "0 active
        # research" before any of them commits, then each spawns a task.  The
        # user sets max_parallel=1 expecting 1 task; without serialization
        # they get up to N (one per instance, each picking a different
        # direction because the first instance's just-QUEUED task shows up
        # as "busy" to the next instance's read).
        #
        # pg_try_advisory_xact_lock is non-blocking; losers return for this
        # cycle and pick up on the next interval tick.  The lock is scoped
        # to the transaction — releases on commit/rollback automatically.
        # Tests use SQLite which has no advisory locks; the try/except keeps
        # the fallback equivalent to "assume we hold the lock" (safe because
        # there's only one process in tests).
        from sqlalchemy import text as _sql_text  # noqa: PLC0415
        _lock_key = abs(hash(f"research_spawn:{project.id}")) % (2**31)
        try:
            _got_lock = (await session.execute(
                _sql_text("SELECT pg_try_advisory_xact_lock(:k)"),
                {"k": _lock_key},
            )).scalar()
        except Exception:
            _got_lock = True  # non-Postgres dialect (tests) — skip serialization
        if not _got_lock:
            logger.info(
                f"[AUTONOMOUS] Research spawn for project {project.id} skipped — "
                f"another instance holds the advisory lock"
            )
            return

        max_parallel = _runtime_config.get("max_parallel_research_per_project", 2)
        for d in sorted_eligible:
            did = d.get("id")
            if did in blocked_ids:
                continue
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
                logger.debug(f"[RESEARCH] Selected direction {did} category={category} productivity_score={productivity_score:.2f} momentum={momentum:.1f} vision_rank={_vision_rank(d)}")
    elif not force_strategic:
        return  # No directions and not a strategic cycle — nothing to do

    # When all active directions are saturated, skip tactical research — a strategic
    # cycle will create fresh directions; spawning more saturated research is wasteful.
    if all_dirs_saturated:
        chosen_directions = []
        active_directions = []

    # Check for a pending strategic review from a previously failed chain.
    # If the design_doc → strategic_review spawn failed last cycle, retry it now
    # rather than waiting up to 6 more research spawns (~3+ hours).
    pending_sr = (project.vision or {}).get('_pending_strategic_review')
    if pending_sr:
        try:
            from app.strategic_spawning import _spawn_strategic_review  # noqa: PLC0415
            if await _spawn_strategic_review(project, session, cycle_id=pending_sr.get('cycle_id')):
                project.vision = {**project.vision}
                project.vision.pop('_pending_strategic_review', None)
                flag_modified(project, 'vision')
                session.add(project)
                logger.info(f'[STRATEGIC] Retried pending strategic review for project {project.id} (originally failed at {pending_sr.get("failed_at")})')
            else:
                logger.warning(f'[STRATEGIC] Pending SR retry skipped (active strategic task) for project {project.id}')
        except Exception:
            logger.exception(f'[STRATEGIC] Retry of pending strategic review failed for project {project.id}')
        return  # Don't spawn tactical research this cycle

    # Strategic cycle check — runs BEFORE the research blocking guard so it's
    # never blocked by an active tactical research task.  The design_doc_update
    # and strategic_review use their own task types and run in parallel.
    all_active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]
    # Use a stored spawn counter instead of derived research_history count.
    # The history-based count doesn't increment when cycles produce no novel
    # findings, causing undirected/strategic cycles to repeat indefinitely.
    # Seed from research_history on first access so existing projects don't reset.
    research_spawn_count = (project.vision or {}).get("_research_spawn_count")
    if research_spawn_count is None:
        research_spawn_count = sum(
            len(d.get("research_history", [])) + d.get("history_aggregate", {}).get("total_cycles", 0)
            for d in all_active_directions
        )
        project.vision = {**(project.vision or {}), "_research_spawn_count": research_spawn_count}
        flag_modified(project, "vision")
        session.add(project)
        logger.info(f"[RESEARCH] Seeded _research_spawn_count={research_spawn_count} from research_history")
    strategic_frequency = (project.vision or {}).get("_strategic_frequency_override") or _runtime_config.get("strategic_research_frequency", 6)
    is_strategic = (
        (strategic_frequency > 0 and research_spawn_count > 0 and research_spawn_count % strategic_frequency == 0)
        or force_strategic
    )
    if is_strategic:
        # Determine if this strategic cycle should also include architecture review.
        # Architecture stabilizes faster than strategy, so it runs less frequently.
        strategic_cycle_count = (project.vision or {}).get("_strategic_cycle_count", 0) + 1
        arch_frequency = _runtime_config.get("architecture_review_frequency", 3)
        is_architecture_cycle = arch_frequency > 0 and strategic_cycle_count % arch_frequency == 0

        spawned = False
        if is_architecture_cycle:
            # Full pipeline: Phase 1 (architecture) → Phase 2 (strategic) via completion handler
            from app.strategic_spawning import _spawn_design_doc_update
            spawned = await _spawn_design_doc_update(project, session)
            if spawned:
                logger.info(f"[AUTONOMOUS] Spawned design doc update (strategic cycle #{strategic_cycle_count}, "
                            f"vision rev {(project.vision or {}).get('revision', 0)})")
        else:
            # Strategy-only: skip architecture, go straight to strategic review
            from app.strategic_spawning import _spawn_strategic_review
            spawned = await _spawn_strategic_review(project, session)
            if spawned:
                logger.info(f"[AUTONOMOUS] Spawned strategic review without architecture "
                            f"(cycle #{strategic_cycle_count}, arch runs every {arch_frequency})")

        if spawned:
            research_spawn_count += 1
            project.vision = {**(project.vision or {}),
                              "_research_spawn_count": research_spawn_count,
                              "_strategic_cycle_count": strategic_cycle_count}
            flag_modified(project, "vision")
            session.add(project)

    # Guard: limit parallel research tasks per project.
    # EVALUATING parents are just waiting on children — they don't block new research.
    max_parallel = _runtime_config.get("max_parallel_research_per_project", 2)
    active_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        )
    )).scalars().all()
    active_research_count = sum(1 for t in active_tasks if (t.first_payload).get("type") == "research")
    if active_research_count >= max_parallel:
        return
    # Reduce chosen_directions to the remaining slots
    remaining_slots = max_parallel - active_research_count
    chosen_directions = chosen_directions[:remaining_slots]
    # Guard: global rate limit across ALL projects
    from sqlalchemy import func
    one_hour_ago = _utcnow() - timedelta(hours=1)
    global_max = _runtime_config.get("max_global_autonomous_tasks_per_hour",
                                      _runtime_config["max_autonomous_tasks_per_hour"] * 10)
    global_recent = (await session.execute(
        select(func.count()).select_from(Task).where(
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )).scalar() or 0
    if global_recent >= global_max:
        logger.warning(f"[AUTONOMOUS] Global rate limit reached: {global_recent}/{global_max} tasks/hour")
        return
    # Guard: per-project rate limit for research spawns
    recent_research = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )).scalars().all()
    recent_research_count = sum(1 for t in recent_research if (t.first_payload).get("type") == "research")
    if recent_research_count >= _runtime_config["max_autonomous_tasks_per_hour"]:
        return

    # Also cap by per-project rate limit
    rate_limit_remaining = _runtime_config["max_autonomous_tasks_per_hour"] - recent_research_count
    if rate_limit_remaining < len(chosen_directions):
        chosen_directions = chosen_directions[:max(0, rate_limit_remaining)]

    # If no active directions exist (e.g., all paused), only strategic runs (already spawned above).
    if not chosen_directions and not active_directions:
        return

    # Autonomous research creates independent tasks (no parent_task_id), so they
    # must always branch from main.  Staging branches (research/*, plan/*) are
    # ephemeral — they get deleted when their parent's rollup merges.  An
    # independent task using someone else's staging branch will break when that
    # rollup cleans up.  Actual subresearch (spawned by _handle_research_completion
    # with parent_task_id set) correctly inherits its parent's staging branch.
    base_branch = "main"

    # Use per-type defaults
    research_model = _runtime_config.get("task_type_models", {}).get("research") or None
    research_effort = _runtime_config.get("task_type_efforts", {}).get("research") or None
    research_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("research")
    # Every Nth cycle is "undirected" — the agent explores the whole codebase
    # without a specific direction lens, finding cross-cutting improvements.
    # Default raised from 4 → 12 on 2026-04-17: cross-cutting research was
    # producing diminishing returns relative to directed cycles, and it burns
    # just as much GitHub/account budget per run.  Still configurable via
    # runtime_config for repos that need more exploratory cycles.
    undirected_frequency = _runtime_config.get("undirected_research_frequency", 12)
    is_undirected = undirected_frequency > 0 and research_spawn_count > 0 and research_spawn_count % undirected_frequency == 0
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
        from app.broadcast import broadcast_to_project  # noqa: PLC0415
        from app.notification_dispatch import dispatch_external_notification  # noqa: PLC0415
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
            asyncio.ensure_future(dispatch_external_notification(
                str(project.id),
                "ANOMALY_ADJUSTMENT",
                {
                    "max_concurrent_vms": new_vms,
                    "failure_gate_threshold": gate,
                    "trigger_anomalies": anomaly_action.get("trigger_anomalies") or [],
                    "pre_adjustment_max_vms": pre_vms,
                },
            ))
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
            asyncio.ensure_future(dispatch_external_notification(
                str(project.id),
                "ANOMALY_RESTORED",
                {
                    "max_concurrent_vms": new_vms,
                    "failure_gate_threshold": gate,
                },
            ))

    # Build and spawn research tasks — one per chosen direction (or one undirected)
    directions_to_spawn = chosen_directions if chosen_directions else [None]  # None = undirected
    if is_undirected:
        directions_to_spawn = [None]  # Undirected replaces directed for this cycle

    spawned_tasks = []
    for chosen_direction in directions_to_spawn:
        # Build lightweight direction summaries — include compact history for
        # agent-side stats (aggregateDirectionStats, buildScopeCalibrationSignal).
        # Full histories are available via the API. This keeps the payload under
        # the OS E2BIG limit (~128KB).
        _HISTORY_NUMERIC_FIELDS = {
            "tasks_created", "prs_merged", "tasks_failed",
            "failed_task_details", "scope_breakdown", "avg_time_to_merge_hours",
        }
        _AGGREGATE_FIELDS = {
            "total_cycles", "total_tasks_created", "total_prs_merged",
            "total_tasks_failed", "total_prs_rejected",
        }
        current_dir_id = None if chosen_direction is None else chosen_direction.get("id")
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
            direction_summaries.append(summary)

        # Design doc summary — just keys and playbook, not full content.
        # Agent can fetch specific sections via ./design-doc get <key>.
        doc = project.design_doc or {}
        design_doc_summary: dict = {
            "_keys": [k for k in doc if not k.startswith("_") and not k.startswith("playbook/")],
        }
        # Include playbook entries (small, operationally critical)
        for k, v in doc.items():
            if k.startswith("playbook/") and isinstance(v, dict):
                design_doc_summary[k] = v.get("content", "")

        research_payload: dict = {
            "type": "research",
            "directions": direction_summaries,
            "current_direction_id": None if chosen_direction is None else chosen_direction.get("id"),
            "design_doc": design_doc_summary,
            "base_branch": base_branch,
            **({"model": research_model} if research_model else {}),
            **({"effort": research_effort} if research_effort else {}),
            **({"prompt_suffix": research_suffix} if research_suffix else {}),
        }
        if chosen_direction and chosen_direction.get("category") == "product":
            product_guidance = (
                "This direction targets a product feature. Prioritize user-visible changes, "
                "end-user experience improvements, and direct product value. Structural work "
                "is acceptable only when it directly enables the product feature."
            )
            existing = research_payload.get("prompt_suffix", "")
            research_payload["prompt_suffix"] = f"{existing}\n{product_guidance}".strip() if existing else product_guidance
        # Pre-extract design doc context for the current direction's system_id
        if chosen_direction:
            dir_system_id = chosen_direction.get("system_id")
            if dir_system_id and project.design_doc:
                doc = project.design_doc
                doc_systems = doc.get("systems", [])
                doc_capabilities = doc.get("capabilities", {})
                relevant: dict = {"system_id": dir_system_id}
                for sys in (doc_systems if isinstance(doc_systems, list) else []):
                    if isinstance(sys, dict) and sys.get("id") == dir_system_id:
                        relevant["system"] = sys
                        break
                if isinstance(doc_capabilities, dict):
                    related_caps = {
                        cap_name: cap_val for cap_name, cap_val in doc_capabilities.items()
                        if isinstance(cap_val, dict) and cap_val.get("status") in ("missing", "partial")
                    }
                    if related_caps:
                        relevant["missing_capabilities"] = related_caps
                research_payload["direction_design_context"] = relevant
            if chosen_direction.get("possibly_complete"):
                research_payload["direction_possibly_complete"] = True
                research_payload["completion_suggestion"] = chosen_direction.get("completion_suggestion", "")
        if chosen_direction is None:
            if anomalies:
                research_payload["anomaly_signals"] = anomalies
            direction_text = "Cross-cutting codebase analysis"
            logger.info(f"[AUTONOMOUS] Spawning UNDIRECTED research cycle (cycle #{research_spawn_count})")
        else:
            direction_text = (chosen_direction.get("text") or "general improvement")[:200]

        if project.build_config:
            research_payload["build_config"] = project.build_config
        research_payload = validate_internal_payload(research_payload, "autonomous_research:research")

        # Recover deferred findings from recent completed research tasks.
        # When the pipeline was degraded, _handle_research_completion caps spawns at
        # _DEGRADED_SPAWN_CAP and stores the remaining findings in the completed task's
        # context['deferred_findings']. Inject them here so the new cycle processes them
        # alongside its own discoveries (they go through full dedup + health gate again).
        # Directed cycles claim findings tagged with their direction_id plus unscoped
        # findings; undirected cycles claim only unscoped (direction_id-less) findings
        # so directed leftovers stay reserved for their direction's next cycle.
        task_context: dict = {}
        prev_research_rows = (await session.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.status == TaskStatus.COMPLETED,
            ).order_by(Task.created_at.desc()).limit(20)
        )).scalars().all()
        prev_research_rows = [r for r in prev_research_rows if (r.context or {}).get("deferred_findings")]
        from app.finding_deduplication import finding_hash as _finding_hash  # noqa: PLC0415
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
        await _increment_research_spawn_count(project, session)
        history = TaskHistory(
            task_id=new_task.id, action="SPAWNED",
            details={"spawned_by": "AUTONOMOUS_LOOP", "trigger": "post_merge_or_idle"}
        )
        session.add(history)
        spawned_tasks.append(new_task)

    await session.commit()

    # Track first task for HALF_OPEN → CLOSED transition
    if breaker["state"] == "HALF_OPEN" and spawned_tasks:
        breaker["half_open_task_id"] = str(spawned_tasks[0].id)

    for t in spawned_tasks:
        logger.info(f"[AUTONOMOUS] Spawned research task {t.id} for project {project.id}")
    if len(spawned_tasks) > 1:
        logger.info(f"[AUTONOMOUS] Spawned {len(spawned_tasks)} parallel research tasks for project {project.id}")


async def _spawn_periodic_task(project: Project, task_type: str, session: AsyncSession):
    """Spawn a periodic non-research task (qa, security_audit, deps) if its schedule is due."""
    from app.main import _maybe_create_circuit_breaker_alert  # noqa: PLC0415
    from sqlalchemy import func

    # Guard: task_type must be a known type
    if task_type not in ALL_TASK_TYPES:
        logger.warning(f"[AUTONOMOUS] Unknown task_type '{task_type}' in _spawn_periodic_task, skipping")
        return

    # Guard: circuit breaker must allow spawning
    breaker = _shared_state.get_circuit_breaker(str(project.id))
    if breaker["state"] == "OPEN":
        if breaker["opened_at"] and (_utcnow() - breaker["opened_at"]).total_seconds() >= breaker["cooldown_seconds"]:
            breaker["state"] = "HALF_OPEN"
            logger.warning(f"[CIRCUIT BREAKER] OPEN → HALF-OPEN for {project.id} (cooldown elapsed, allowing one test task)")
        else:
            return  # Still in cooldown
    elif breaker["state"] == "HALF_OPEN":
        test_id = breaker.get("half_open_task_id")
        if test_id:
            test_task = await session.get(Task, uuid.UUID(test_id))
            if test_task and test_task.started_at and (_utcnow() - test_task.started_at).total_seconds() > 1800:
                logger.warning(f"[CIRCUIT BREAKER] HALF_OPEN test task {test_id} stuck for 30m+, treating as failure → OPEN")
                breaker["state"] = "OPEN"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
                breaker["half_open_task_id"] = None
                await _maybe_create_circuit_breaker_alert(
                    project.id, session,
                    f"HALF_OPEN test task stuck for 30m+ — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)"
                )
                return

    periodic_cfg = _runtime_config.get("autonomous_periodic_tasks", {}).get(task_type, {})
    if not periodic_cfg.get("enabled", False):
        return

    interval_hours = periodic_cfg.get("interval_hours", 168)

    # Guard: no task of this type already QUEUED/PROVISIONING/IN_PROGRESS
    active_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        )
    )).scalars().all()
    for t in active_tasks:
        if (t.first_payload).get("type") == task_type:
            return

    # Guard: global rate limit across ALL projects
    one_hour_ago = _utcnow() - timedelta(hours=1)
    global_max = _runtime_config.get("max_global_autonomous_tasks_per_hour",
                                      _runtime_config["max_autonomous_tasks_per_hour"] * 10)
    global_recent = (await session.execute(
        select(func.count()).select_from(Task).where(
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )).scalar() or 0
    if global_recent >= global_max:
        logger.warning(f"[AUTONOMOUS] Global rate limit reached: {global_recent}/{global_max} tasks/hour")
        return
    # Guard: per-project rate limit
    recent_auto = (await session.execute(
        select(func.count()).select_from(Task).where(
            Task.project_id == project.id,
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )).scalar() or 0
    if recent_auto >= _runtime_config["max_autonomous_tasks_per_hour"]:
        return

    # Check when this type last ran (most recent completed task of this type)
    completed_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status == TaskStatus.COMPLETED,
        ).order_by(Task.created_at.desc()).limit(100)
    )).scalars().all()
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
    recent_task = (await session.execute(
        select(Task).where(Task.project_id == project.id)
        .order_by(Task.created_at.desc()).limit(1)
    )).scalars().first()
    base_branch = (recent_task.first_payload).get("base_branch", "main") if recent_task else "main"

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
    payload = validate_internal_payload(payload, "autonomous_research:periodic:" + task_type)
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
        task_id=new_task.id, action="SPAWNED",
        details={"spawned_by": "AUTONOMOUS_LOOP", "trigger": "periodic_schedule", "task_type": task_type}
    )
    session.add(history)
    await session.commit()
    await session.refresh(new_task)

    if breaker["state"] == "HALF_OPEN":
        breaker["half_open_task_id"] = str(new_task.id)

    logger.info(f"[AUTONOMOUS] Spawned periodic {task_type} task {new_task.id} for project {project.id}")


async def _check_autonomous_research(owner: str, repo: str):
    """Post-merge hook: check if we should spawn a research task."""
    try:
        async with AsyncSessionLocal() as session:
            projects = (await session.execute(
                select(Project).where(
                    Project.github_owner == owner,
                    Project.github_repo == repo,
                    Project.autonomous_enabled.is_(True),
                )
            )).scalars().all()
            if not projects:
                return

            # Spawn if VM slots are available (PROVISIONING + IN_PROGRESS, not blocked QUEUED)
            from sqlalchemy import func
            vm_consuming = (await session.execute(
                select(func.count()).select_from(Task).where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                )
            )).scalar() or 0

            if vm_consuming < _runtime_config["max_concurrent_vms"]:
                for project in projects:
                    await _spawn_research_task(project, session)
    except Exception as e:
        logger.error(f"[AUTONOMOUS] Error checking research trigger for {owner}/{repo}: {e}", exc_info=True)


async def _spawn_direction_hold_investigation(project: Project, session: AsyncSession) -> int:
    """Auto-spawn an investigate-type task for each held direction in the project.

    A direction enters the held state after 3 consecutive CODE_ERROR failures (see
    shared_state.record_direction_result). Without this spawn, the hold silently
    expires after DIRECTION_HOLD_TIMEOUT_HOURS and the direction resumes blindly
    retrying. Investigation reports are consumed by _handle_investigation_completion,
    which can then drive targeted remediation.
    """
    from app.anomaly_detection import _error_fingerprint  # noqa: PLC0415
    held = _shared_state.get_held_directions()
    if not held:
        return 0

    project_id_str = str(project.id)
    direction_map = {d.get("id"): d for d in (project.directions or []) if d.get("id")}
    spawned = 0

    in_flight = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([
                TaskStatus.QUEUED, TaskStatus.PROVISIONING,
                TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING,
            ]),
        )
    )).scalars().all()
    in_flight_dir_ids = {
        (t.context or {}).get("direction_id")
        for t in in_flight
        if (t.context or {}).get("spawned_by") == "direction_hold_investigation"
    }

    for direction_id, hold_info in held.items():
        if hold_info.get("project_id") != project_id_str:
            continue
        direction = direction_map.get(direction_id)
        if not direction:
            continue
        if direction_id in in_flight_dir_ids:
            continue

        recent_failed = (await session.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.status == TaskStatus.FAILED,
            ).order_by(Task.created_at.desc()).limit(30)
        )).scalars().all()
        dir_failures = [
            t for t in recent_failed if (t.context or {}).get("direction_id") == direction_id
        ][:5]

        failure_lines = []
        fingerprints: list[str] = []
        for t in dir_failures:
            err = (t.latest_status_message or "(no message)")[:240]
            fp = _error_fingerprint(err)
            fingerprints.append(fp)
            failure_lines.append(f"- [{str(t.id)[:8]}] fp={fp} | {err}")
        failure_block = "\n".join(failure_lines) if failure_lines else "(no recent failure details available)"

        direction_text = (direction.get("text") or "?")[:300]
        fc = hold_info.get("last_failure_class") or "CODE_ERROR"
        streak_n = hold_info.get("consecutive_failures", "?")
        prompt = (
            f"Direction '{direction_text}' (id: {direction_id}) has been placed on hold "
            f"after {streak_n} consecutive {fc} failures. Diagnose the underlying root cause "
            f"so the direction can resume.\n\n"
            f"Recent failed tasks for this direction:\n{failure_block}\n\n"
            f"Produce the structured diagnosis JSON report. Focus on shared failure patterns "
            f"across the tasks above (same fingerprint = same root cause)."
        )

        payload = validate_internal_payload(
            {"type": "investigate", "prompt": prompt, "base_branch": "main"},
            "autonomous_research:direction_hold_investigation",
        )
        new_task = Task(
            project_id=project.id,
            title=f"Investigate direction hold: {direction_text[:80]}",
            description=f"Auto-spawned on direction {direction_id[:8]} hold ({streak_n} {fc} failures)",
            payloads=[payload],
            context={
                "direction_id": direction_id,
                "spawned_by": "direction_hold_investigation",
                "hold_reason": hold_info.get("reason", ""),
                "last_failure_class": fc,
                "failure_fingerprints": fingerprints,
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        session.add(new_task)
        session.add(TaskHistory(
            task_id=new_task.id, action="SPAWNED",
            details={"spawned_by": "DIRECTION_HOLD_INVESTIGATION", "direction_id": direction_id},
        ))
        logger.info(
            f"[DIRECTION HEALTH] Spawned investigation task {new_task.id} for held direction {direction_id}"
        )
        spawned += 1

    if spawned:
        await session.commit()
    return spawned


async def _check_idle_research():
    """Periodic check: spawn research if projects are idle with active directions."""
    from app.anomaly_detection import _expire_stale_proposals, _check_anomaly_proposals  # noqa: PLC0415
    now = _utcnow()
    interval_minutes = _runtime_config.get("autonomous_research_interval_minutes", 30)
    if _shared_state.last_idle_check and (now - _shared_state.last_idle_check) < timedelta(minutes=interval_minutes):
        return
    _shared_state.last_idle_check = now

    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import func

            # Expire stale proposals before processing
            await _expire_stale_proposals(session)

            projects = (await session.execute(
                select(Project).where(Project.autonomous_enabled.is_(True))
            )).scalars().all()

            for project in projects:
                # Log pending proposals awaiting review
                pending_proposals = [d for d in (project.directions or []) if d.get("status") == "proposed"]
                if pending_proposals:
                    logger.info(f"[DIRECTIONS] Project {project.name} has {len(pending_proposals)} pending direction proposals awaiting review")

                active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]
                # Allow projects with no directions through if vision needs bootstrapping
                vision_needs_bootstrap = not (project.vision or {}).get("purpose")
                strategic_recovery_needed = not active_directions and (
                    (project.vision or {}).get("_strategic_failures", 0) < 3
                )
                if not active_directions and not vision_needs_bootstrap and not strategic_recovery_needed:
                    continue

                # Spawn if VM slots are available — count only tasks consuming VMs,
                # not QUEUED tasks blocked on dependencies
                vm_consuming = (await session.execute(
                    select(func.count()).select_from(Task).where(
                        Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                    )
                )).scalar() or 0

                if vm_consuming < _runtime_config["max_concurrent_vms"]:
                    await _spawn_research_task(project, session)

                    for task_type in ("security_audit", "qa", "deps"):
                        await _spawn_periodic_task(project, task_type, session)

                # Check persistent anomalies for direction proposals (regardless of VM slots)
                await _check_anomaly_proposals(project, session)
                # Auto-spawn investigation tasks for any directions placed on hold
                await _spawn_direction_hold_investigation(project, session)
                await session.commit()
    except Exception as e:
        logger.error(f"[AUTONOMOUS] Error in idle research check: {e}", exc_info=True)


async def _spawn_direction_diagnosis(
    project: Project,
    direction: dict,
    pause_reason: str,
    session: AsyncSession,
) -> bool:
    """Spawn a diagnosis research task for a freshly auto-paused direction.

    Diagnosis research investigates WHY the direction keeps failing — it looks at
    the direction's recent error signatures, failure classifications, and playbook
    entries to produce targeted fix findings that address the root cause before the
    direction's 48h cooldown elapses.

    Returns True if a new diagnosis task was created, False if skipped (already an
    active diagnosis task for this direction, or the spawn couldn't be completed).
    """
    did = direction.get("id")
    if not did:
        return False

    # Skip if a diagnosis research is already active for this direction —
    # one diagnosis per pause event is plenty.
    existing = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([
                TaskStatus.QUEUED, TaskStatus.PROVISIONING,
                TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING,
            ]),
            Task.context["diagnosis_for_direction"].as_string() == did,
        )
    )).scalars().first()
    if existing is not None:
        logger.info(
            f"[DIAGNOSIS] Skipping diagnosis spawn for direction {did}: "
            f"task {existing.id} already active"
        )
        return False

    # Gather recent failed-task signatures for this direction to inform the agent.
    recent_failed_rows = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status == TaskStatus.FAILED,
            Task.context["direction_id"].as_string() == did,
        ).order_by(Task.created_at.desc()).limit(10)
    )).scalars().all()
    failed_signatures: list[dict] = []
    taxonomy_counts: dict[str, int] = {}
    for t in recent_failed_rows:
        ctx = t.context or {}
        payload = t.first_payload or {}
        category = ctx.get("failure_category") or ""
        taxonomy = ctx.get("failure_taxonomy") or ""
        signature = {
            "task_id": str(t.id),
            "title": (t.title or "")[:200],
            "task_type": payload.get("type", ""),
            "failure_category": category,
            "failure_taxonomy": taxonomy,
            "status_message": (t.latest_status_message or "")[:400],
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        fingerprint = ctx.get("failure_fingerprint") or ctx.get("failure_stage")
        if fingerprint:
            signature["failure_fingerprint"] = str(fingerprint)[:200]
        failed_signatures.append(signature)
        if taxonomy:
            taxonomy_counts[taxonomy] = taxonomy_counts.get(taxonomy, 0) + 1
        elif category:
            taxonomy_counts[category] = taxonomy_counts.get(category, 0) + 1

    # Pull playbook entries (operational knowledge) for the agent to reference.
    doc = project.design_doc or {}
    playbook_entries: dict[str, str] = {}
    for k, v in doc.items():
        if k.startswith("playbook/") and isinstance(v, dict):
            content = v.get("content") or ""
            if content:
                playbook_entries[k] = content[:2000]

    # Compact direction summary — just this direction, with recent history attached.
    raw_history = direction.get("research_history") or []
    _HISTORY_NUMERIC_FIELDS = {
        "tasks_created", "prs_merged", "tasks_failed",
        "tasks_failed_infra", "scope_breakdown",
    }
    compact_history = [
        {k: entry[k] for k in _HISTORY_NUMERIC_FIELDS if k in entry}
        for entry in raw_history[-5:]
    ]
    direction_summary: dict = {
        "id": did,
        "text": direction.get("text"),
        "priority": direction.get("priority"),
        "system_id": direction.get("system_id"),
        "paths": direction.get("paths") or [],
        "auto_pause_count": direction.get("auto_pause_count", 0),
    }
    if direction.get("category"):
        direction_summary["category"] = direction["category"]
    if compact_history:
        direction_summary["research_history"] = compact_history

    # Design doc summary — keys + playbook entries (same shape as standard research).
    design_doc_summary: dict = {
        "_keys": [k for k in doc if not k.startswith("_") and not k.startswith("playbook/")],
    }
    design_doc_summary.update(playbook_entries)

    research_model = _runtime_config.get("task_type_models", {}).get("research") or None
    research_effort = _runtime_config.get("task_type_efforts", {}).get("research") or None
    research_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("research")

    research_payload: dict = {
        "type": "research",
        "research_mode": "diagnosis",
        "directions": [direction_summary],
        "current_direction_id": did,
        "design_doc": design_doc_summary,
        "base_branch": "main",
        "pause_reason": pause_reason,
        "failed_task_signatures": failed_signatures,
        "failure_taxonomy_counts": taxonomy_counts,
        **({"model": research_model} if research_model else {}),
        **({"effort": research_effort} if research_effort else {}),
        **({"prompt_suffix": research_suffix} if research_suffix else {}),
    }
    if project.build_config:
        research_payload["build_config"] = project.build_config
    research_payload = validate_internal_payload(research_payload, "autonomous_research:diagnosis")

    direction_text = (direction.get("text") or "direction")[:160]
    new_task = Task(
        project_id=project.id,
        title=f"Diagnosis: {direction_text}",
        description=f"Auto-paused diagnosis for direction: {direction_text}",
        payloads=[research_payload],
        context={
            "diagnosis_for_direction": did,
            "direction_id": did,
            "pause_reason": pause_reason,
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    history = TaskHistory(
        task_id=new_task.id, action="SPAWNED",
        details={
            "spawned_by": "AUTONOMOUS_LOOP",
            "trigger": "direction_auto_pause_diagnosis",
            "pause_reason": pause_reason,
        },
    )
    session.add(history)
    logger.info(
        f"[DIAGNOSIS] Spawned diagnosis research {new_task.id} for direction {did}: "
        f"{pause_reason} ({len(failed_signatures)} failed task signatures)"
    )
    return True
