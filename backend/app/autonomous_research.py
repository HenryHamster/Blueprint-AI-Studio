"""Autonomous research spawning functions."""

import asyncio
import contextlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    Task,
    Project,
    TaskHistory,
    TaskStatus,
    Alert,
    AlertSeverity,
)
from app.database import AsyncSessionLocal
from app.payload_schemas import (
    validate_internal_payload,
    ALL_TASK_TYPES,
    PayloadTooLargeError,
)
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config
from app.max_plan_pool import _max_plan_pools, _account_available_for_model
from app.time_utils import _utcnow
from app.autonomous_research_diagnosis import _spawn_direction_diagnosis  # noqa: F401 — re-export for backwards compat
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

logger = logging.getLogger(__name__)
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


# Re-export the self_audit helpers for backwards compat with callers /
# tests that patch them under ``app.autonomous_research``.
from app.self_audit_dispatch import (  # noqa: E402 — re-export block follows conditional imports above
    _record_residual_audit_deferral,
    _should_dispatch_self_audit,
    _spawn_self_audit_task,
)


async def _spawn_research_task_inner(project: Project, session: AsyncSession):
    """Inner implementation — always called under the per-project spawn lock."""
    from app.main import _maybe_create_circuit_breaker_alert
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
    has_available = any(
        _account_available_for_model(a, research_model) for a in user_pool
    ) or bool(os.getenv("ANTHROPIC_API_KEY"))
    if not has_available:
        logger.info(
            f"[AUTONOMOUS] Skipping research for {project.name} — no available accounts"
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
                            ]
                        ),
                        and_(
                            Task.status == TaskStatus.FAILED,
                            Task.updated_at >= resurrection_lookback,
                            Task.context["failure_category"]
                            .as_string()
                            .in_(list(INFRA_CLASSES)),
                            ~Task.context.has_key("_resurrection_count"),
                        ),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    evaluating_timeout_minutes = _runtime_config.get(
        "evaluating_pipeline_timeout_minutes", 30
    )

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
                    # Count OTHER recent research on this direction (any
                    # status, including CANCELLED) — exclude self.
                    recent_others = [
                        rid
                        for rid in recent_research_by_direction.get(current_did, [])
                        if rid != t.id
                    ]
                    if not recent_others:
                        continue  # No prior pipelined spawn — allow one
                    # Already pipelined within the window — treat as busy.
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
    ):
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
                project, session, cycle_id=pending_sr.get("cycle_id")
            ):
                project.vision = {**project.vision}
                project.vision.pop("_pending_strategic_review", None)
                flag_modified(project, "vision")
                session.add(project)
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

    # Mode-aware self_audit dispatch. Runs BEFORE strategic dispatch so a stale
    # or high-drift project audits before the strategic agent reads stale state.
    # Force_strategic preempts: when force_strategic is True (no purpose / no
    # active dirs / all saturated) the project needs strategic spawn, not an
    # audit, so we skip the audit decision entirely.
    if not force_strategic:
        try:
            audit_decision = await _should_dispatch_self_audit(project, session)
        except Exception:
            logger.exception(
                f"[SELF_AUDIT] decision failed for project {project.id}; "
                "falling through to regular dispatch"
            )
            audit_decision = None
        if audit_decision is not None:
            try:
                if await _spawn_self_audit_task(project, session, audit_decision):
                    return  # Audit dispatched; skip tactical/strategic this tick.
            except Exception:
                logger.exception(
                    f"[SELF_AUDIT] spawn failed for project {project.id}; "
                    "falling through to regular dispatch"
                )

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
        project.vision = {
            **(project.vision or {}),
            "_research_spawn_count": research_spawn_count,
        }
        flag_modified(project, "vision")
        session.add(project)
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
                    .in_(["strategic_review", "design_doc_update"]),
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
                        .in_(["strategic_review", "design_doc_update"]),
                        Task.created_at >= cooldown_cutoff,
                    )
                )
            ).scalar() or 0
            if recent_strategic > 0:
                logger.info(
                    f"[STRATEGIC] Skipping strategic spawn — {recent_strategic} strategic "
                    f"task(s) created in last {cooldown_minutes}min for project {project.id} "
                    f"(force_strategic={force_strategic}); falling through to tactical"
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
                session.add(
                    Alert(
                        project_id=project.id,
                        severity=AlertSeverity.WARNING,
                        title="Strategic review skipped: payload too large",
                        message=(
                            f"Strategic spawn payload exceeds the 256 KB limit "
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
            project.vision = {
                **(project.vision or {}),
                "_strategic_payload_bloat_skip": {
                    "skipped_at": _utcnow().isoformat(),
                    "size": e.size,
                    "top_contributors": [
                        {"key": k, "size": n} for k, n in e.top_keys[:5]
                    ],
                },
            }
            flag_modified(project, "vision")
            session.add(project)
            await session.commit()

        if spawned:
            research_spawn_count += 1
            project.vision = {
                **(project.vision or {}),
                "_research_spawn_count": research_spawn_count,
                "_strategic_cycle_count": strategic_cycle_count,
            }
            flag_modified(project, "vision")
            session.add(project)
            # 2026-05-03: commit BEFORE the tactical-research early-returns
            # below. Without this, when a tactical-research cap (max_parallel
            # / max_total / global rate-limit) fires, the function returns
            # without committing the strategic-counter increment. Next tick
            # reads the OLD count, sees count % strategic_frequency == 0
            # again, fires another strategic spawn. Even with the
            # has_active_strategic guard above, eventually that strategic
            # task completes and the next tick re-fires immediately.
            await session.commit()

    # Guard: limit parallel research tasks per project.
    # EVALUATING parents are just waiting on children — they don't block new research.
    max_parallel = _runtime_config.get("max_parallel_research_per_project", 3)
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
    active_research_count = sum(
        1 for t in active_tasks if (t.first_payload).get("type") == "research"
    )
    if active_research_count >= max_parallel:
        return
    # Second cap: total research in any non-terminal state (including
    # EVALUATING). Pending rollups in EVALUATING accumulate merge complexity
    # — each new spawn produces another rollup branch competing for the same
    # base. This cap prevents the pile-up that makes downstream merges
    # incrementally more conflict-prone.
    max_total = _runtime_config.get("max_total_research_per_project", 5)
    total_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project.id,
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
    total_research_count = sum(
        1 for t in total_tasks if (t.first_payload).get("type") == "research"
    )
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
    from sqlalchemy import func

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
    recent_research = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.created_by == "ORCHESTRATOR",
                    Task.created_at >= one_hour_ago,
                )
            )
        )
        .scalars()
        .all()
    )
    recent_research_count = sum(
        1 for t in recent_research if (t.first_payload).get("type") == "research"
    )
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
    base_branch = _runtime_config.get("integration_branch", "main")

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

    spawned_tasks = []
    for chosen_direction in directions_to_spawn:
        _mode = normalized_recommended_mode(chosen_direction)
        if (
            chosen_direction
            and _mode == "audit"
            and chosen_direction.get("system_id")
            and await _spawn_self_audit_task(
                project,
                session,
                {
                    "focus_systems": [chosen_direction.get("system_id")],
                    "max_systems": 1,
                },
            )
        ):
            continue

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
                research_payload["direction_design_context"] = relevant
            if chosen_direction.get("possibly_complete"):
                research_payload["direction_possibly_complete"] = True
                research_payload["completion_suggestion"] = chosen_direction.get(
                    "completion_suggestion", ""
                )
        if chosen_direction is None:
            if anomalies:
                research_payload["anomaly_signals"] = anomalies
            direction_text = "Cross-cutting codebase analysis"
            logger.info(
                f"[AUTONOMOUS] Spawning UNDIRECTED research cycle (cycle #{research_spawn_count})"
            )
        else:
            direction_text = (chosen_direction.get("text") or "general improvement")[
                :200
            ]

        if project.build_config:
            research_payload["build_config"] = project.build_config
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
    from app.main import _maybe_create_circuit_breaker_alert
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
    _integration_branch = _runtime_config.get("integration_branch", "main")
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


_MAX_ACTIVE_INVESTIGATIONS_PER_DIRECTION = 3


async def _recent_investigation_exists(
    project_id: uuid.UUID,
    direction_id: str,
    session: AsyncSession,
    error_fingerprint: str | None = None,
) -> bool:
    """Return True if an investigate-type task already targets this direction.

    Unified dedup gate shared by ``_spawn_direction_diagnosis`` (auto-pause path)
    and ``_spawn_direction_hold_investigation`` (direction-hold path). Matches
    any investigate task for ``direction_id`` that is either in-flight
    (QUEUED/PROVISIONING/IN_PROGRESS/EVALUATING) or was created in the last 24h.

    When ``error_fingerprint`` is supplied, dedup is narrowed to investigations
    whose ``failure_fingerprints`` context array contains that fingerprint —
    distinct failure patterns within the same direction can each be investigated.
    Regardless of fingerprint, a per-direction concurrent cap of
    ``_MAX_ACTIVE_INVESTIGATIONS_PER_DIRECTION`` always applies to prevent
    resource exhaustion.
    """
    from sqlalchemy import or_

    active_statuses = (
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    )
    cutoff = _utcnow() - timedelta(hours=24)
    rows = (
        await session.execute(
            select(Task.id, Task.context, Task.status).where(
                Task.project_id == project_id,
                Task.payloads[0]["type"].as_string() == "investigate",
                Task.context["direction_id"].as_string() == str(direction_id),
                or_(
                    Task.status.in_(active_statuses),
                    Task.created_at >= cutoff,
                ),
            )
        )
    ).all()

    active_count = sum(1 for _id, _ctx, status in rows if status in active_statuses)
    if active_count >= _MAX_ACTIVE_INVESTIGATIONS_PER_DIRECTION:
        return True

    if error_fingerprint is None:
        return len(rows) > 0

    for _id, ctx, _status in rows:
        fps = (ctx or {}).get("failure_fingerprints") or []
        if error_fingerprint in fps:
            return True
    return False


async def _spawn_direction_hold_investigation(
    project: Project, session: AsyncSession
) -> int:
    """Auto-spawn an investigate-type task for each held direction in the project.

    A direction enters the held state after 3 consecutive CODE_ERROR failures (see
    shared_state.record_direction_result). Without this spawn, the hold silently
    expires after DIRECTION_HOLD_TIMEOUT_HOURS and the direction resumes blindly
    retrying. Investigation reports are consumed by _handle_investigation_completion,
    which can then drive targeted remediation.
    """
    from app.anomaly_detection import _error_fingerprint

    held = _shared_state.get_held_directions()
    if not held:
        return 0

    project_id_str = str(project.id)
    direction_map = {d.get("id"): d for d in (project.directions or []) if d.get("id")}
    spawned = 0

    for direction_id, hold_info in held.items():
        if hold_info.get("project_id") != project_id_str:
            continue
        direction = direction_map.get(direction_id)
        if not direction:
            continue
        recent_failed = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.project_id == project.id,
                        Task.status == TaskStatus.FAILED,
                    )
                    .order_by(Task.created_at.desc())
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )
        dir_failures = [
            t
            for t in recent_failed
            if (t.context or {}).get("direction_id") == direction_id
        ][:5]

        failure_lines = []
        fingerprints: list[str] = []
        for t in dir_failures:
            err = (t.latest_status_message or "(no message)")[:240]
            fp = _error_fingerprint(err)
            fingerprints.append(fp)
            failure_lines.append(f"- [{str(t.id)[:8]}] fp={fp} | {err}")
        failure_block = (
            "\n".join(failure_lines)
            if failure_lines
            else "(no recent failure details available)"
        )

        dominant_fp = fingerprints[0] if fingerprints else None
        if await _recent_investigation_exists(
            project.id,
            direction_id,
            session,
            error_fingerprint=dominant_fp,
        ):
            continue

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
            {
                "type": "investigate",
                "prompt": prompt,
                "base_branch": _runtime_config.get("integration_branch", "main"),
            },
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
                **({"error_fingerprint": fingerprints[0]} if fingerprints else {}),
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        session.add(new_task)
        session.add(
            TaskHistory(
                task_id=new_task.id,
                action="SPAWNED",
                details={
                    "spawned_by": "DIRECTION_HOLD_INVESTIGATION",
                    "direction_id": direction_id,
                },
            )
        )
        logger.info(
            f"[DIRECTION HEALTH] Spawned investigation task {new_task.id} for held direction {direction_id}"
        )
        spawned += 1

    if spawned:
        await session.commit()
    return spawned


async def _spawn_task_failure_investigation(
    task: Task,
    session: AsyncSession,
) -> bool:
    """Spawn an investigate task when a directed task exhausts retries with
    a CODE_ERROR / code_defect failure.

    Returns True when a new investigation task is created, False otherwise.
    Canonical implementation — called by task_lifecycle post-failure processing
    once a task has been marked FAILED and its retry budget is exhausted.
    Gating is delegated to ``should_trigger_code_defect_investigation``; this
    function adds 24h dedup per direction and builds the investigation prompt.
    """
    from app.failure_classification import (
        AGENT_TIMEOUT,
        CODE_ERROR,
        REVIEW_QUALITY_STREAK_THRESHOLD,
        TAXONOMY_CODE_DEFECT,
        TAXONOMY_INFRA_RELATED,
        TAXONOMY_REVIEW_QUALITY,
        classify_failure_taxonomy,
        should_trigger_code_defect_investigation,
        should_trigger_review_quality_investigation,
        should_trigger_timeout_investigation,
    )

    is_code_defect = should_trigger_code_defect_investigation(task)
    is_timeout = (not is_code_defect) and should_trigger_timeout_investigation(task)
    is_review_quality = (
        (not is_code_defect)
        and (not is_timeout)
        and should_trigger_review_quality_investigation(task)
    )
    if not (is_code_defect or is_timeout or is_review_quality):
        return False

    ctx = task.context or {}
    direction_id = ctx.get("direction_id")

    # Investigation-recursion depth cap. The 2026-05-03 dogfood run showed
    # multi-level investigation chains burning compute without converging:
    #   original_task fails (depth 0) →
    #     investigation_1 (depth 1) → remediation_1 (depth 1) →
    #       investigation_2 (depth 2) → remediation_2 (depth 2) →
    #         investigation_3 (depth 3) → ...
    # Each investigation costs an agent VM (~$0.50-1.00 + tokens) and the
    # marginal value drops fast — by depth 3 the diagnoses tend to converge
    # on operator-judgment-required outcomes anyway. Cap at depth 2: failures
    # of deeper-level remediations skip the spawn and surface to operator
    # review via project message instead.
    MAX_INVESTIGATION_DEPTH = 2
    source_depth = int(ctx.get("investigation_depth") or 0)
    next_depth = source_depth + 1
    if next_depth > MAX_INVESTIGATION_DEPTH:
        logger.warning(
            f"[TASK FAILURE INVESTIGATION] Skip spawn for task {task.id} "
            f"(direction {direction_id}): investigation_depth would be "
            f"{next_depth} > MAX={MAX_INVESTIGATION_DEPTH}. Marking for "
            f"operator review."
        )
        try:
            from app.models import ProjectMessage, MessageType

            session.add(
                ProjectMessage(
                    project_id=task.project_id,
                    message_type=MessageType.NOTE,
                    from_type="orchestrator",
                    content=(
                        f"Investigation chain depth limit ({MAX_INVESTIGATION_DEPTH}) "
                        f"reached for direction {direction_id or 'undirected'}. "
                        f"Failed task: {task.id} ({(task.title or '?')[:80]}). "
                        f"Auto-remediation has tried multiple times without converging — "
                        f"operator review is needed before spawning further investigations."
                    ),
                )
            )
        except Exception as _msg_exc:  # noqa: BLE001
            logger.warning(
                f"[TASK FAILURE INVESTIGATION] Failed to post depth-cap message: {_msg_exc}"
            )
        return False

    if is_review_quality:
        # Frequency gate: fire once this direction shows a review-quality
        # pattern in its recent failures. Use a sliding window over the
        # most recent FAILED tasks on the direction (excluding the current
        # one) and require at least (THRESHOLD - 1) of the last WINDOW_SIZE
        # to be review_quality. This tolerates occasional non-review-quality
        # failures interspersed in an otherwise-clear review-quality pattern.
        WINDOW_SIZE = 5
        required_count = REVIEW_QUALITY_STREAK_THRESHOLD - 1
        # If a convergence investigation already exists for this direction
        # within the last 7 days, that's strong independent evidence of
        # chronic review-quality problems — lower the count requirement by
        # 1 so we trigger faster on repeat offenders.
        convergence_cutoff = _utcnow() - timedelta(days=7)
        convergence_recent = (
            (
                await session.execute(
                    select(Task).where(
                        Task.project_id == task.project_id,
                        Task.created_at >= convergence_cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        has_convergence = any(
            (t.context or {}).get("direction_id") == direction_id
            and (t.context or {}).get("spawned_by")
            == "review_convergence_investigation"
            for t in convergence_recent
        )
        if has_convergence:
            required_count = max(1, required_count - 1)

        recent = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.project_id == task.project_id,
                        Task.status == TaskStatus.FAILED,
                        Task.id != task.id,
                    )
                    .order_by(Task.created_at.desc())
                    .limit(60)
                )
            )
            .scalars()
            .all()
        )
        dir_recent = [
            t for t in recent if (t.context or {}).get("direction_id") == direction_id
        ][:WINDOW_SIZE]
        if len(dir_recent) < required_count:
            return False
        review_quality_count = sum(
            1
            for t in dir_recent
            if classify_failure_taxonomy(t) == TAXONOMY_REVIEW_QUALITY
        )
        if review_quality_count < required_count:
            return False

    last_failure_class = (
        CODE_ERROR if (is_code_defect or is_review_quality) else AGENT_TIMEOUT
    )
    if is_code_defect:
        failure_taxonomy = TAXONOMY_CODE_DEFECT
    elif is_review_quality:
        failure_taxonomy = TAXONOMY_REVIEW_QUALITY
    else:
        failure_taxonomy = TAXONOMY_INFRA_RELATED
    # Audit W1: previous_error_fingerprint is now a column.
    prev_fp = task.previous_error_fingerprint or ""
    if await _recent_investigation_exists(
        task.project_id,
        direction_id,
        session,
        error_fingerprint=prev_fp or None,
    ):
        return False

    if prev_fp:
        from app.task_completion import _find_sibling_fingerprint_investigation

        sibling_cutoff = _utcnow() - timedelta(hours=24)
        shared_id = await _find_sibling_fingerprint_investigation(
            task.project_id,
            prev_fp,
            sibling_cutoff,
            session,
        )
        if shared_id is not None:
            logger.info(
                f"[TASK FAILURE INVESTIGATION] Skip spawn for task {task.id} "
                f"(direction {direction_id}): shares fingerprint {prev_fp[:40]} "
                f"with existing investigate task {shared_id}"
            )
            return False

    direction: dict = {}
    if direction_id:
        project = await session.get(Project, task.project_id)
        if project is not None:
            direction = (
                next(
                    (
                        d
                        for d in (project.directions or [])
                        if d.get("id") == direction_id
                    ),
                    {},
                )
                or {}
            )

    payload = task.first_payload or {}
    ttype = payload.get("type") or ""
    status_msg = (task.latest_status_message or "(no message)")[:400]
    tool_trace = ctx.get("_tool_trace") or ""
    if isinstance(tool_trace, list):
        tool_trace = "\n".join(str(x) for x in tool_trace)
    tool_trace = str(tool_trace)[:1500]
    failing_tests = ctx.get("failing_tests") or ctx.get("failed_tests") or []
    if isinstance(failing_tests, (list, tuple)):
        failing_tests_str = ", ".join(str(x) for x in failing_tests)[:400]
    else:
        failing_tests_str = str(failing_tests)[:400]

    prompt_lines = [
        f"Task {str(task.id)[:8]} ({ttype}) failed with {last_failure_class} / {failure_taxonomy} "
        f"after exhausting its retry budget. Diagnose the root cause.",
        "",
        f"Direction: {direction_id}",
        f"Status message: {status_msg}",
    ]
    if prev_fp:
        prompt_lines.append(f"Previous error fingerprint: {prev_fp}")
    if failing_tests_str:
        prompt_lines.append(f"Failing tests: {failing_tests_str}")
    if tool_trace:
        prompt_lines.append(f"\nRecent tool trace:\n{tool_trace}")
    if is_review_quality:
        prompt_lines.append(
            f"\nThis is a review-quality pattern investigation: this direction has "
            f"{REVIEW_QUALITY_STREAK_THRESHOLD}+ review-quality failures in its recent "
            f"task window (lint/type-check/test-suite). Look for systemic causes — a module that "
            f"consistently produces lint-failing code, a test configuration mismatch, "
            f"or a code pattern that always triggers type errors — rather than "
            f"diagnosing this one task's failure in isolation."
        )

    prior_diag = (direction.get("diagnosis_summary") or "").strip()
    if prior_diag:
        prompt_lines.append(f"\nPrior diagnosis summary:\n{prior_diag[:800]}")
    partial_summary = (direction.get("partial_diagnosis_summary") or "").strip()
    partial_root = (direction.get("partial_diagnosis_root_cause") or "").strip()
    partial_files = direction.get("partial_diagnosis_files") or []
    if partial_summary or partial_root or partial_files:
        partial_lines = ["\nPartial diagnosis from prior failed investigation:"]
        if partial_summary:
            partial_lines.append(f"- summary: {partial_summary[:400]}")
        if partial_root:
            partial_lines.append(f"- root cause: {partial_root[:400]}")
        if partial_files:
            files_str = ", ".join(str(f) for f in partial_files[:10])[:300]
            partial_lines.append(f"- affected files: {files_str}")
        prompt_lines.append("\n".join(partial_lines))
    rem_attempts = int(direction.get("remediation_attempts") or 0)
    last_rem_result = direction.get("last_remediation_result")
    last_rem_err = (direction.get("last_remediation_error_summary") or "")[:300]
    last_rem_fc = direction.get("last_remediation_failure_class")
    if rem_attempts or last_rem_result or last_rem_err:
        rem_lines = [f"\nRemediation history ({rem_attempts} prior attempt(s)):"]
        if last_rem_result:
            rem_lines.append(f"- last result: {last_rem_result}")
        if last_rem_fc:
            rem_lines.append(f"- last failure class: {last_rem_fc}")
        if last_rem_err:
            rem_lines.append(f"- last error: {last_rem_err}")
        prompt_lines.append("\n".join(rem_lines))

    prompt_lines.append(
        "\nProduce the structured diagnosis JSON report so remediation can be spawned. "
        "Build on prior findings above rather than re-diagnosing what is already known."
    )
    prompt = "\n".join(prompt_lines)

    inv_payload = validate_internal_payload(
        {
            "type": "investigate",
            "prompt": prompt,
            "base_branch": _runtime_config.get("integration_branch", "main"),
        },
        "autonomous_research:task_failure_investigation",
    )
    new_task = Task(
        project_id=task.project_id,
        title=f"Investigate task failure: {(task.title or '?')[:80]}",
        description=f"Auto-spawned after {ttype} task {str(task.id)[:8]} exhausted retries (CODE_ERROR/code_defect)",
        payloads=[inv_payload],
        context={
            "direction_id": direction_id,
            "spawned_by": "task_failure_investigation",
            "source_task_id": str(task.id),
            "last_failure_class": last_failure_class,
            "failure_taxonomy": failure_taxonomy,
            "failure_fingerprints": [prev_fp] if prev_fp else [],
            "investigation_depth": next_depth,
            **(
                {"diagnosis_fingerprint": prev_fp, "error_fingerprint": prev_fp}
                if prev_fp
                else {}
            ),
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    session.add(
        TaskHistory(
            task_id=new_task.id,
            action="SPAWNED",
            details={
                "spawned_by": "TASK_FAILURE_INVESTIGATION",
                "direction_id": direction_id,
                "source_task_id": str(task.id),
            },
        )
    )
    logger.info(
        f"[TASK FAILURE INVESTIGATION] Spawned investigation {new_task.id} for "
        f"failed task {task.id} (direction {direction_id})"
    )
    return True


async def _spawn_review_convergence_investigation(
    orig_task: Task,
    session: AsyncSession,
    *,
    pr_number: int,
    iterations: int,
    detail: str,
    trigger: str,
    review_history: list | None = None,
) -> uuid.UUID | None:
    """Spawn an investigate task for review convergence failures.

    Fired from ``review_pipeline._handle_cap_exhaustion`` (trigger=
    ``review_cap_exhausted``) and the stuck-cycle path in
    ``_spawn_pipeline_task`` (trigger=``review_stuck_cycle``).

    Guards (mirroring _spawn_task_failure_investigation):
    - Skip when orig_task has no direction_id (no direction to diagnose).
    - Skip investigate/smoketest types to avoid self-referential loops.
    - Respect the shared 24h per-direction investigation dedup.
    """
    ttype = (orig_task.first_payload or {}).get("type") or ""
    if ttype in ("investigate", "smoketest"):
        return None

    direction_id = (orig_task.context or {}).get("direction_id")
    if not direction_id:
        return None

    if await _recent_investigation_exists(orig_task.project_id, direction_id, session):
        return None

    from app.anomaly_detection import _error_fingerprint
    from app.failure_classification import CODE_ERROR, TAXONOMY_REVIEW_QUALITY
    from app.task_completion import _find_sibling_fingerprint_investigation

    fp_source = (orig_task.latest_status_message or "").strip()
    if not fp_source:
        history_for_fp = (
            review_history
            or (orig_task.context or {}).get("review_loop", {}).get("review_history")
            or []
        )
        for entry in reversed(history_for_fp):
            for iss in entry.get("issues") or []:
                desc = (iss.get("description") or "").strip()
                if desc:
                    fp_source = desc
                    break
            if fp_source:
                break
            fb = entry.get("feedback")
            if fb:
                fp_source = str(fb).strip()
                break
    diagnosis_fingerprint = _error_fingerprint(fp_source) if fp_source else ""

    if diagnosis_fingerprint:
        sibling_cutoff = _utcnow() - timedelta(hours=24)
        shared_id = await _find_sibling_fingerprint_investigation(
            orig_task.project_id,
            diagnosis_fingerprint,
            sibling_cutoff,
            session,
        )
        if shared_id is not None:
            logger.info(
                f"[REVIEW CONVERGENCE] Skip spawn for PR #{pr_number} "
                f"(direction {direction_id}): shares fingerprint "
                f"{diagnosis_fingerprint[:40]} with existing investigate task {shared_id}"
            )
            return None

    history = (
        review_history
        or (orig_task.context or {}).get("review_loop", {}).get("review_history")
        or []
    )
    issues_lines: list[str] = []
    for entry in history[-3:]:
        it = entry.get("iteration", "?")
        issues = entry.get("issues") or []
        for iss in issues[:5]:
            sev = (iss.get("severity") or "").upper()
            desc = (iss.get("description") or "").strip()[:200]
            if desc:
                issues_lines.append(f"  - [iter {it}] [{sev}] {desc}")
        if not issues and entry.get("feedback"):
            issues_lines.append(f"  - [iter {it}] {str(entry.get('feedback'))[:200]}")
    issues_block = (
        "\n".join(issues_lines)
        if issues_lines
        else "(no structured review issues captured)"
    )

    trigger_label = (
        "cap exhausted" if trigger == "review_cap_exhausted" else "stuck cycle"
    )
    prompt = (
        f"Review-fix convergence failure on PR #{pr_number} ({trigger_label}) after "
        f"{iterations} iterations. Diagnose why the review loop could not converge.\n\n"
        f"Direction: {direction_id}\n"
        f"Source task: {str(orig_task.id)[:8]} ({ttype})\n"
        f"Detail: {detail}\n\n"
        f"Repeating review issues:\n{issues_block}\n\n"
        f"Consider: scope too large for one PR, architectural mismatch, wrong approach, "
        f"direction misalignment, or test-coverage/correctness gaps the fix agent cannot "
        f"close in scope. Produce the structured diagnosis JSON report."
    )

    inv_payload = validate_internal_payload(
        {
            "type": "investigate",
            "prompt": prompt,
            "base_branch": _runtime_config.get("integration_branch", "main"),
        },
        "autonomous_research:review_convergence_investigation",
    )
    new_task = Task(
        project_id=orig_task.project_id,
        title=f"Investigate review convergence: PR #{pr_number}",
        description=f"Auto-spawned after review {trigger_label} on PR #{pr_number} ({iterations} iterations)",
        payloads=[inv_payload],
        context={
            "direction_id": direction_id,
            "spawned_by": "review_convergence_investigation",
            "source_task_id": str(orig_task.id),
            "pr_number": pr_number,
            "review_iterations": iterations,
            "convergence_trigger": trigger,
            "convergence_detail": detail,
            "last_failure_class": CODE_ERROR,
            "failure_taxonomy": TAXONOMY_REVIEW_QUALITY,
            "failure_fingerprints": [diagnosis_fingerprint]
            if diagnosis_fingerprint
            else [],
            **(
                {"diagnosis_fingerprint": diagnosis_fingerprint}
                if diagnosis_fingerprint
                else {}
            ),
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    session.add(
        TaskHistory(
            task_id=new_task.id,
            action="SPAWNED",
            details={
                "spawned_by": "REVIEW_CONVERGENCE_INVESTIGATION",
                "direction_id": direction_id,
                "source_task_id": str(orig_task.id),
                "pr_number": pr_number,
                "trigger": trigger,
            },
        )
    )
    logger.info(
        f"[REVIEW CONVERGENCE] Spawned investigation {new_task.id} for "
        f"PR #{pr_number} ({trigger}, direction {direction_id})"
    )
    return new_task.id


async def _infer_fingerprint_audit_system(
    project: Project,
    session: AsyncSession,
    task_ids: list,
) -> str | None:
    """Return system_id if fingerprint tasks all map to a single system in audit/fundamentals mode.

    Reuses the direction→system_id mapping from project.directions (same as self_audit_dispatch).
    Returns None when the mapping is ambiguous (no direction link, multiple systems, or unset mode).
    """
    from app.models import SystemMaturity

    dir_to_system: dict[str, str] = {
        d["id"]: d["system_id"]
        for d in (project.directions or [])
        if d.get("id") and d.get("system_id")
    }
    if not dir_to_system or not task_ids:
        return None
    uuids = []
    for tid in task_ids[:5]:
        with contextlib.suppress(ValueError, AttributeError):
            uuids.append(uuid.UUID(str(tid)))
    if not uuids:
        return None
    rows = (await session.execute(select(Task.context).where(Task.id.in_(uuids)))).all()
    system_ids: set[str] = set()
    for (ctx,) in rows:
        did = (ctx or {}).get("direction_id")
        if did and did in dir_to_system:
            system_ids.add(dir_to_system[did])
    if len(system_ids) != 1:
        return None
    sid = next(iter(system_ids))
    mat = (
        await session.execute(
            select(SystemMaturity).where(
                SystemMaturity.project_id == project.id,
                SystemMaturity.system_id == sid,
            )
        )
    ).scalar_one_or_none()
    return sid if mat and mat.recommended_mode in ("audit", "fundamentals") else None


async def _spawn_residual_fingerprint_remediation(
    project: Project, session: AsyncSession
) -> int:
    """Spawn a create-type remediation task for the top uncooled residual CODE_ERROR fingerprint.

    Scans get_top_residual_fingerprints() and spawns at most one create task per
    call — the highest-count fingerprint whose per-project cooldown has expired.
    The cooldown (default 24 h, configurable via residual_remediation_cooldown_hours)
    prevents re-spawning while a prior remediation is still live or recently merged.

    Global concurrency cap: if there are already `max_concurrent_auto_remediations`
    (default 2) auto-remediation tasks active or queued, skip spawning. Prevents
    the queue from ballooning when many distinct fingerprints accumulate, e.g.
    when an ambient bug on main causes every task to fail in a slightly
    different place and each surface-level error gets its own fingerprint.

    Returns the number of tasks spawned (0 or 1).
    """
    from app.failure_classification import get_top_residual_fingerprints
    from app.models import ResidualCaseStatus, ResidualRemediationCase
    from sqlalchemy import select

    fingerprints = get_top_residual_fingerprints(window_seconds=86400, top_n=10)
    if not fingerprints:
        return 0

    # Load case rows we need for spawn-side filters in one query:
    # IGNORED + SUSPENDED block re-spawn; REMEDIATING + last_spawned_at
    # drive the per-fingerprint cooldown. Row count for REMEDIATING
    # also drives the project-wide concurrency cap (see below).
    case_rows_result = await session.execute(
        select(
            ResidualRemediationCase.fingerprint,
            ResidualRemediationCase.status,
            ResidualRemediationCase.last_spawned_at,
        ).where(ResidualRemediationCase.project_id == project.id)
    )
    case_rows = case_rows_result.all()
    blocked_fingerprints: set[str] = {
        fp
        for fp, status, _ in case_rows
        if status in (ResidualCaseStatus.IGNORED, ResidualCaseStatus.SUSPENDED)
    }
    last_spawned_by_fp: dict[str, datetime] = {
        fp: ts for fp, _, ts in case_rows if ts is not None
    }

    # Project-wide concurrency cap. Count case rows currently in the
    # in-flight REMEDIATING state — independent of the CI auto-fix
    # quota since that lives on project.design_doc._auto_remediation.
    # The DB row count is authoritative; old in-memory dedup is gone.
    max_concurrent = int(_runtime_config.get("max_concurrent_auto_remediations", 2))
    active_count = sum(
        1 for _, status, _ in case_rows if status == ResidualCaseStatus.REMEDIATING
    )
    if active_count >= max_concurrent:
        logger.info(
            "[AUTO REMEDIATION] %d residual case(s) already REMEDIATING, "
            "at or above cap (%d); skipping spawn (project %s).",
            active_count,
            max_concurrent,
            project.id,
        )
        return 0

    cooldown_hours = _runtime_config.get("residual_remediation_cooldown_hours", 24)
    cooldown_seconds = cooldown_hours * 3600
    # Higher count threshold for fingerprints whose source isn't error-shaped
    # (latest_status_message was a routine heartbeat — Cloning, Using tool,
    # PROVISION_PROGRESS, etc. — and no error line surfaced from the logs).
    # A single one-off task dying with such a status is noise; many tasks
    # dying with the same heartbeat is a real signal worth investigating.
    no_error_min_count = int(
        _runtime_config.get("residual_remediation_no_error_min_count", 20)
    )
    now_dt = _utcnow()
    cooldown_delta = timedelta(seconds=cooldown_seconds)

    for fp_entry in fingerprints:
        fp = fp_entry["fingerprint"]
        if fp in blocked_fingerprints:
            logger.debug(
                "[AUTO REMEDIATION] Skipping IGNORED/SUSPENDED fingerprint %s (project %s).",
                fp[:32],
                project.id,
            )
            continue
        last_spawned = last_spawned_by_fp.get(fp)
        if last_spawned is not None and (now_dt - last_spawned) < cooldown_delta:
            continue

        # Defer if all sampled tasks map to a single system in audit/fundamentals mode.
        _audit_sid = await _infer_fingerprint_audit_system(
            project, session, fp_entry.get("task_ids") or []
        )
        if _audit_sid:
            logger.info(
                "[AUTO REMEDIATION] Deferring fingerprint %s: system %r is in "
                "audit/fundamentals mode (project %s).",
                fp[:32],
                _audit_sid,
                project.id,
            )
            await _record_residual_audit_deferral(session, project.id, fp, _audit_sid)
            continue

        # Require a high count before remediating non-error-shaped clusters.
        if (
            not fp_entry.get("is_error_shaped", True)
            and fp_entry.get("count", 0) < no_error_min_count
        ):
            logger.info(
                "[AUTO REMEDIATION] Skipping fingerprint %s: heartbeat-only cluster "
                "(no error-shaped contribution) at count=%d, threshold=%d. "
                "Sample: %.80s",
                fp[:32],
                fp_entry.get("count", 0),
                no_error_min_count,
                (fp_entry.get("sample_message") or "")[:80],
            )
            continue

        # Prefer the list-of-samples when present (added so fingerprints
        # accumulate distinct error-line variations across tasks); fall
        # back to the single legacy fields.
        sample_messages = fp_entry.get("sample_messages") or (
            [fp_entry["sample_message"]] if fp_entry.get("sample_message") else []
        )

        # Re-classify the primary sample against the current classifier.
        # Fingerprints can linger in the in-memory store for up to 24 h after
        # the underlying classify_failure fix is deployed (the store is
        # process-local; it doesn't clear on code push). If the sample now
        # routes to AGENT_TIMEOUT (or any non-CODE_ERROR class), the issue
        # has already been addressed and spawning another remediation task
        # would waste capacity and trigger an identical cycle 24 h later.
        if sample_messages:
            from app.failure_classification import (
                classify_failure as _rfc,
                CODE_ERROR as _CE,
            )
            import types as _types

            _probe = _types.SimpleNamespace(
                context={},
                latest_status_message=sample_messages[0],
                id=None,
            )
            if _rfc(_probe) != _CE:
                logger.info(
                    "[AUTO REMEDIATION] Skipping fingerprint %s: primary sample "
                    "no longer classifies as CODE_ERROR (underlying fix already deployed). "
                    "Sample: %.80s",
                    fp[:32],
                    sample_messages[0],
                )
                continue

        sample_statuses = fp_entry.get("sample_statuses") or (
            [fp_entry["sample_status"]] if fp_entry.get("sample_status") else []
        )
        task_ids = fp_entry.get("task_ids") or []
        task_ids_str = ", ".join(str(t) for t in task_ids[:5])

        # Render each bucket as a numbered list when we have >1 sample, or
        # a single block when we have one. Capped list already from the
        # residual store; just join.
        def _render_samples(label: str, samples: list[str]) -> str:
            samples = [s for s in samples if s]
            if not samples:
                return ""
            if len(samples) == 1:
                return f"\n\n{label}:\n{samples[0]}"
            bodies = "\n\n".join(f"[{i + 1}] {s}" for i, s in enumerate(samples))
            return f"\n\n{label} ({len(samples)} variations across affected tasks):\n{bodies}"

        samples_block = _render_samples("Sample error message(s)", sample_messages)
        # Status context — show only those that differ from the error lines.
        distinct_statuses = [
            s
            for s in sample_statuses
            if s and not any(s.strip() == m.strip() for m in sample_messages)
        ]
        status_block = _render_samples(
            "Agent status when these failures hit (context, not the error)",
            distinct_statuses,
        )

        prompt = (
            f"You are spawned as an auto-remediation task. A CODE_ERROR "
            f"fingerprint has been recurring on main for {fp_entry.get('count', '?')} "
            f"task(s) in the last 24 hours and nothing in the pipeline has "
            f"fixed it. Your job is to investigate and open a fix PR.\n\n"
            f"IMPORTANT — read this carefully:\n"
            f"- This bug exists on main TODAY. It is not caused by your "
            f"changes. Your job is to fix it.\n"
            f"- The default 'don't touch pre-existing failures' instinct "
            f"DOES NOT APPLY here. You are EXPLICITLY authorized (and "
            f"expected) to edit unrelated-looking code, test mocks, "
            f"fixtures, or imports when the error points there.\n"
            f"- Common causes: missing exports on test mocks, stale "
            f"fixtures, recent refactors that broke unrelated tests, "
            f"missing migration, imports pointing at a moved file.\n"
            f"- First, reproduce the error locally — run the relevant test "
            f"suite and confirm you see the failure before editing.\n"
            f"- Do NOT fix by silencing, skipping, weakening assertions, "
            f"or try/except-wrapping errors. Find and fix the real cause.\n\n"
            f"Fingerprint: '{fp}'"
            f"{samples_block}"
            f"{status_block}\n\n"
            f"Affected task IDs (up to 5): {task_ids_str or '(none recorded)'}\n\n"
            f"Open a fix PR that resolves the underlying code defect."
        )
        payload = validate_internal_payload(
            {
                "type": "create",
                "prompt": prompt,
                "base_branch": _runtime_config.get("integration_branch", "main"),
            },
            "autonomous_research:residual_fingerprint_remediation",
        )
        primary_sample = sample_messages[0] if sample_messages else ""
        # Compute investigation_depth for the remediation. We look up the
        # max depth among task_ids (the failed tasks that contributed to
        # this fingerprint) and add 1. If the deepest source was at depth 1
        # (already a remediation that failed), the new remediation lands at
        # depth 2 — and a subsequent failure won't trigger investigation #3
        # (gated by MAX_INVESTIGATION_DEPTH=2 in _spawn_task_failure_investigation).
        source_max_depth = 0
        if task_ids:
            try:
                rows = (
                    await session.execute(
                        select(Task.context).where(
                            Task.id.in_(
                                [
                                    uuid.UUID(t) if isinstance(t, str) else t
                                    for t in task_ids[:5]
                                ]
                            )
                        )
                    )
                ).all()
                for (rc,) in rows:
                    d = int((rc or {}).get("investigation_depth") or 0)
                    if d > source_max_depth:
                        source_max_depth = d
            except Exception as _depth_exc:  # noqa: BLE001
                logger.debug(
                    f"[AUTO REMEDIATION] depth lookup failed for {fp[:32]}: {_depth_exc}"
                )
        remediation_context = {
            "auto_remediation": True,
            "spawned_by": "residual_fingerprint_remediation",
            "error_fingerprint": fp,
            "sample_message": primary_sample[:500],
            "task_ids": task_ids,
            "investigation_depth": source_max_depth + 1,
        }
        new_task = Task(
            project_id=project.id,
            title=f"Remediate residual fingerprint: {fp[:60]}",
            description=f"Auto-spawned remediation for residual CODE_ERROR fingerprint {fp[:40]}",
            payloads=[payload],
            context=remediation_context,
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        session.add(new_task)
        session.add(
            TaskHistory(
                task_id=new_task.id,
                action="SPAWNED",
                details={
                    "spawned_by": "RESIDUAL_FINGERPRINT_REMEDIATION",
                    "error_fingerprint": fp,
                },
            )
        )
        # Stamp REMEDIATING + last_spawned_* on the case row and emit the
        # spawn message. Subsequent ticks read the same row to enforce the
        # per-fingerprint cooldown — no in-memory dedup dict.
        from app.notification_dispatch import post_residual_fix_spawned_message

        try:
            await post_residual_fix_spawned_message(
                session,
                project.id,
                fingerprint=fp,
                sample_message=primary_sample,
                task_id=new_task.id,
            )
        except Exception as _spawn_msg_exc:  # noqa: BLE001
            logger.warning(
                "[AUTO REMEDIATION] Failed to post spawned message for %s: %s",
                fp[:32],
                _spawn_msg_exc,
            )
        logger.info(
            f"[AUTO REMEDIATION] Spawned create task {new_task.id} for residual "
            f"fingerprint {fp[:40]} (project {project.id})"
        )
        await session.commit()
        return 1

    return 0


async def _check_idle_research():
    """Periodic check: spawn research if projects are idle with active directions."""
    from app.anomaly_detection import _expire_stale_proposals, _check_anomaly_proposals

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
                        select(Project).where(Project.autonomous_enabled.is_(True))
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

                # Check persistent anomalies for direction proposals (regardless of VM slots)
                await _check_anomaly_proposals(project, session)
                # Auto-spawn investigation tasks for any directions placed on hold
                await _spawn_direction_hold_investigation(project, session)
                await _spawn_residual_fingerprint_remediation(project, session)
                await session.commit()
    except Exception as e:
        logger.error(f"[AUTONOMOUS] Error in idle research check: {e}", exc_info=True)


# _spawn_direction_diagnosis lives in app.autonomous_research_diagnosis and is
# re-exported at the top of this module for backwards compatibility.
