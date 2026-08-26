"""Autonomous research spawning functions."""

import asyncio
import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import case, exists, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    TASK_ORIGINATION_RESEARCH,
    Task,
    Project,
    TaskHistory,
    TaskStatus,
    Alert,
    AlertSeverity,
)
from app.strategic_authority_locks import (
    LegacyEngineAuthorityExpired,
    commit_legacy_authorized,
    lock_and_validate_legacy_authority,
)
from app.database import AsyncSessionLocal, resolve_dialect_name
from app.knowledge_query import build_knowledge_index, build_compute_sweep_groups
from app.payload_schemas import (
    validate_internal_payload,
    ALL_TASK_TYPES,
    PayloadTooLargeError,
    _MAX_PAYLOAD_BYTES,
)
import app.shared_state as _shared_state
from app import strategic_cycle
from app.shared_state import (
    get_project_integration_branch as _get_project_integration_branch,
    runtime_config as _runtime_config,
)
from app.max_plan_pool import (
    _account_available_for_model,
    _max_plan_pools,
    _normalize_provider_name,
    _settings_bag_model,
    provider_selection_mode,
    resolve_task_model_preferences,
    resolve_task_provider_preference,
    resolve_task_provider_settings,
    resolve_task_quality_tier,
)
from app.providers.registry import get_provider
from app.findings_shelving import (
    RETIRED_DISPOSITION_KEY,
    build_retired_disposition_records,
)
from app.research_deferred_carry import (
    canonical_deferred_finding_hash,
    unacknowledged_deferred_findings,
)
from app.research_cycle_cadence import (
    STRATEGIC_LAST_ACCEPTED_RESEARCH_COUNT_KEY,
    record_strategic_cadence_fire,
    strategic_cadence_due,
)
from app.strategic_cycle_admission import automatic_strategic_cycles_enabled
from app.research_finding_policy import NON_SPAWNING_FINDING_TYPES
from app.task_capabilities import STRATEGIC_TASK_TYPES
from app.task_locking import (
    AUTOMATIC_TASK_CREATION_ALLOWED,
    acquire_automatic_task_creation_authority,
    acquire_direction_research_spawn_lock,
    with_vision_locked,
)
from app.time_utils import _utcnow
from app.workbench_state import (
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
)
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
    classify_paths_tristate,
    detect_absent_symbols,
    resolve_branch_verification_context,
    resolve_head_commit,
)
from app.capability_demand import (
    fetch_missing_demand_for_spawn,
    missing_demand_prompt_hint,
)
from app.capability_proof import commission_proof_earn_loop
from app.capability_registry import (
    annotate_capability_proof_status as _annotate_capability_proof_status,
    registry_capability_evidence_refs_map,
    registry_capability_names,
    registry_capability_vocabulary,
)
from app.direction_refractory import get_direction_refractory_info
from app.direction_priority import direction_task_priority
from app.resource_envelope import (
    get_project_recent_spend_usd,
    resolve_project_spend_ceiling,
    resolve_project_spend_warning_fraction,
    resolve_project_spend_window_days,
)
from app.autonomous_spawn_budget import (
    autonomous_spawn_task_clause,
    build_autonomous_spawn_budget_guard,
    classify_autonomous_spawn_budget_category,
    compute_autonomous_spawn_budget_snapshot,
    autonomous_spawn_budget_config_from_runtime,
    DEFAULT_AUTONOMOUS_SPAWN_BUDGET_MODE,
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


def _research_route_has_available_account(user_pool: list[dict]) -> bool:
    """Check the same unified provider/model route dispatch will use.

    This is a non-allocating preflight: it must never consult the deprecated
    ``task_type_models`` pin because Research roots intentionally leave model
    selection to ``task_type_routing`` plus the provider effort matrix.
    """

    probe = SimpleNamespace(payloads=[{"type": "research"}], context={})
    providers = resolve_task_provider_preference(probe, _runtime_config)
    model_preferences = resolve_task_model_preferences(probe, _runtime_config)
    if provider_selection_mode(_runtime_config) == "exclusive":
        providers = providers[:1]
        model_preferences = model_preferences[:1]

    candidates: list[tuple[str, str | None]] = [
        (preference["provider"], preference["model"])
        for preference in model_preferences
    ]
    quality_tier = resolve_task_quality_tier(probe, _runtime_config)
    for provider_name in providers:
        if any(candidate_provider == provider_name for candidate_provider, _ in candidates):
            continue
        try:
            settings = resolve_task_provider_settings(
                probe, _runtime_config, provider_name
            )
            model = _settings_bag_model(settings) or get_provider(
                provider_name
            ).tier_to_model(quality_tier, _runtime_config)
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "[AUTONOMOUS] Research route preflight could not resolve %s",
                provider_name,
                exc_info=True,
            )
            continue
        candidates.append((provider_name, model))

    for provider_name, model in candidates:
        if provider_name == "anthropic" and os.getenv("ANTHROPIC_API_KEY"):
            return True
        if any(
            (_normalize_provider_name(account.get("provider")) or "anthropic")
            == provider_name
            and _account_available_for_model(account, model)
            for account in user_pool
        ):
            return True
    return False


def project_parallelism_cap(
    project: Project, override_key: str, runtime_key: str, default: int
) -> int:
    """Resolve a per-project parallelism cap.

    A positive-int override in ``project.build_config[override_key]`` wins;
    otherwise fall back to the global runtime setting ``runtime_key``. The
    override is operator-set via the project build_config PATCH (validated
    1..50 by ``schemas.BuildConfig``); the isinstance guard keeps a
    hand-edited or legacy stored value from crashing the spawn loop. Used by
    the spawn guards here and mirrored by the spawn-diagnostics endpoint
    (``admin_debug``) so "why no spawn" reporting matches enforcement.
    """
    raw = (getattr(project, "build_config", None) or {}).get(override_key)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw
    return _runtime_config.get(runtime_key, default)


def _direction_in_refractory(d: dict, now: datetime) -> bool:
    """Return True if the direction's refractory_until window is still active."""
    refractory_info = get_direction_refractory_info(d, now=now)
    remaining = refractory_info["refractory_remaining_minutes"]
    return bool(remaining is not None and remaining > 0)


async def _project_spend_ceiling_spawn_defer_reason(
    project: Project,
    session: AsyncSession,
    *,
    expected_legacy_generation: int | None = None,
) -> str | None:
    """Return a spawn deferral reason when the project spend ceiling is reached.

    Also emits a spend-ceiling alert when the ceiling is hit so operators are
    notified regardless of which spawn gate triggers the check.
    """
    spend_ceiling = resolve_project_spend_ceiling(project)
    if spend_ceiling is None:
        return None
    spend_window_days = resolve_project_spend_window_days(project)
    spend_breakdown = await get_project_recent_spend_usd(
        session,
        project_id=project.id,
        window_days=spend_window_days,
    )
    recent_spend = spend_breakdown.get("ceiling_gate_total", spend_breakdown["total"])
    if recent_spend >= spend_ceiling:
        from app.alert_dispatch import _maybe_create_spend_ceiling_alert

        await _maybe_create_spend_ceiling_alert(
            project.id,
            session,
            window_spend_usd=recent_spend,
            ceiling_usd=spend_ceiling,
            spend_window_days=spend_window_days,
            expected_legacy_generation=expected_legacy_generation,
        )
        return (
            f"project spend ceiling reached: ${recent_spend:.2f}/${spend_ceiling:.2f} "
            f"in the last {spend_window_days} day(s)"
        )
    warning_fraction = resolve_project_spend_warning_fraction(project)
    if recent_spend >= spend_ceiling * warning_fraction:
        from app.alert_dispatch import _maybe_create_approaching_ceiling_alert

        await _maybe_create_approaching_ceiling_alert(
            project.id,
            session,
            window_spend_usd=recent_spend,
            ceiling_usd=spend_ceiling,
            warning_fraction=warning_fraction,
            spend_window_days=spend_window_days,
            expected_legacy_generation=expected_legacy_generation,
        )
    return None


# Sentinel segment for undirected (cross-cutting) research claims — undirected
# cycles target no direction id but must still be claimable one-at-a-time.
_CLAIM_UNDIRECTED = "cross-cutting"


def research_direction_claim_key(
    project_id, direction_id: str | None, *, pipelined: bool = False
) -> str:
    """Idempotency key that atomically claims a direction for ONE active research.

    Stamped on every top-level research spawn (the autonomous loop AND the
    manual ``POST /directions/{id}/research`` endpoint). The partial unique
    index ``ux_task_active_idempotency_key`` (non-NULL keys in active statuses)
    turns the claim into a database-level invariant: two concurrent ticks that
    both read "no active research on this direction" can never BOTH insert —
    the loser hits ``IntegrityError`` and skips. Same TOCTOU class and fix
    shape as the concurrent self-audit trigger (#8239); incident 2026-06-06:
    twins 6ca2f9cf / fcedb840 created 13.7s apart on one direction, each
    rolling up its own duplicate PR. The claim frees automatically when the
    holding task reaches a terminal status (the index only covers active
    rows), so completed/failed/cancelled research never blocks a new cycle.

    ``pipelined=True`` namespaces the ONE deliberately-pipelined cycle allowed
    alongside a genuinely-stuck EVALUATING parent (which still holds the
    primary claim while EVALUATING) — the pipelining exception keeps working,
    and is itself atomically capped at one active pipelined research per
    direction.
    """
    base = f"research-direction:{project_id}:{direction_id or _CLAIM_UNDIRECTED}"
    return f"{base}:pipelined" if pipelined else base


def research_idempotency_key(project_id, direction_id) -> str:
    """Compatibility wrapper for the canonical primary research claim key."""
    return research_direction_claim_key(project_id, direction_id)


def _is_research_claim_integrity_error(exc: IntegrityError) -> bool:
    """Recognize only the unique-index race this spawn path can safely absorb."""

    orig = exc.orig
    diag = getattr(orig, "diag", None)
    if getattr(diag, "constraint_name", None) == "ux_task_active_idempotency_key":
        return True
    message = str(orig).lower()
    return (
        "unique constraint failed: task.idempotency_key" in message
        or "ux_task_active_idempotency_key" in message
    )


def _research_payload_direction_id(payload: dict) -> str | None:
    """Return the direction id from a research payload.

    New rows stamp ``current_direction_id``; legacy single-direction payloads
    only carried ``directions[0].id``. Keep both forms so parked and in-flight
    legacy rows still retain direction ownership.
    """
    current_direction_id = payload.get("current_direction_id")
    if current_direction_id:
        return current_direction_id
    return next(
        (
            d.get("id")
            for d in (payload.get("directions") or [])
            if isinstance(d, dict) and d.get("id")
        ),
        None,
    )


def _research_spawn_lock_key(project_id) -> int:
    """Stable 31-bit advisory-lock key for a project's research spawn.

    Deliberately NOT Python ``hash()``: string hashing is salted per process
    (PYTHONHASHSEED), so two orchestrator processes computed DIFFERENT lock
    keys for the same project and the advisory lock never excluded anything
    across instances — one factor in the 2026-06-06 duplicate-research twins.
    """
    digest = hashlib.sha256(f"research_spawn:{project_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % (2**31)


async def _try_research_spawn_advisory_lock(session: AsyncSession, project_id) -> bool:
    """Try to take the per-project research-spawn advisory lock (Postgres).

    Transaction-scoped (``pg_try_advisory_xact_lock``) and therefore
    BEST-EFFORT: any intermediate commit on ``session`` releases it mid-flow,
    so this only reduces wasted duplicate work between concurrently-ticking
    orchestrator instances. The hard anti-duplicate guarantee is the
    per-direction idempotency-key claim (see ``research_direction_claim_key``).
    Non-Postgres dialects (tests) report no contention.
    """
    from sqlalchemy import text as _sql_text

    try:
        got = (
            await session.execute(
                _sql_text("SELECT pg_try_advisory_xact_lock(:k)"),
                {"k": _research_spawn_lock_key(project_id)},
            )
        ).scalar()
    except Exception:  # noqa: BLE001 — SQLite/test dialects raise NotSupportedError; treat as "no contention"
        return True
    return bool(got)


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


async def _cycle_blocked_direction_keys(
    session,
    project_id,
    directions: list[dict] | None = None,
) -> set[str]:
    """Direction keys whose live discovery cycle cannot admit a new research root.

    Mirrors the admission predicate in ``ensure_discovery_cycle_for_research``:
    a live cycle blocks a new root unless it is ``open`` with no source research
    task bound yet. Used by candidate selection so a blocked direction never
    consumes a ``max_parallel`` slot (head-of-line blocking every other
    eligible direction when the cap is low).
    """
    from app.discovery_admission import (
        direction_discovery_admission_decision,
        load_latest_direction_discovery_cycles,
    )

    cycles = await load_latest_direction_discovery_cycles(session, project_id)
    direction_by_key = {
        str(direction.get("id")): direction
        for direction in (directions or [])
        if direction.get("id")
    }
    return {
        key
        for key, cycle in cycles.items()
        if not direction_discovery_admission_decision(
            direction_by_key.get(key, {"id": key}), cycle
        ).can_admit_root
    }


async def _spawn_research_task(
    project: Project,
    session: AsyncSession,
    *,
    expected_legacy_generation: int | None = None,
    headroom_budget: int | None = None,
):
    """Spawn a research task for a project if conditions are met.

    Uses a per-project lock to prevent races between the post-merge hook
    and the idle-check loop, which run concurrently with separate sessions.

    ``headroom_budget`` (fleet headroom scaled by
    ``compute_headroom_spawn_budget``) additionally caps how many directions
    this call may admit; ``None`` means no fleet-headroom cap (manual spawns,
    post-merge hook without a budget). Returns the number of research tasks
    actually spawned (0 when nothing was admitted).
    """
    async with _get_spawn_lock(str(project.id)):
        return await _spawn_research_task_inner(
            project,
            session,
            expected_legacy_generation=expected_legacy_generation,
            headroom_budget=headroom_budget,
        )


async def _maybe_notify_operator_refresh_deferred(
    project: Project,
    session: AsyncSession,
    reason: str,
    *,
    expected_legacy_generation: int | None = None,
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
        await with_vision_locked(session, project.id, mark_strategic_refresh_notified)
        if expected_legacy_generation is None:
            await session.commit()
        else:
            await commit_legacy_authorized(
                session, project.id, expected_legacy_generation
            )
        logger.info(
            "[STRATEGIC] operator steering refresh deferred for project %s (%s) "
            "— posted one-time deferral notice",
            project.id,
            reason,
        )
    except Exception as exc:
        if isinstance(exc, LegacyEngineAuthorityExpired):
            raise
        logger.exception(
            "[STRATEGIC] failed to post operator steering deferral notice for "
            "project %s",
            project.id,
        )


async def _maybe_notify_operator_steering_acted(
    project: Project,
    session: AsyncSession,
    *,
    expected_legacy_generation: int | None = None,
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
        if expected_legacy_generation is None:
            await session.commit()
        else:
            await commit_legacy_authorized(
                session, project.id, expected_legacy_generation
            )
        logger.info(
            "[STRATEGIC] operator steering consumed on spawn for project %s "
            "— posted one-time confirmation notice",
            project.id,
        )
    except Exception as exc:
        if isinstance(exc, LegacyEngineAuthorityExpired):
            raise
        logger.exception(
            "[STRATEGIC] failed to post operator steering confirmation for project %s",
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


async def _check_half_open_test_task(
    project_id: uuid.UUID,
    session: AsyncSession,
    breaker: dict,
    *,
    expected_legacy_generation: int | None = None,
) -> bool:
    """Check a HALF_OPEN circuit breaker's test task for failure conditions.

    Handles two failure modes: stuck-for-30m+ and deleted/reaped.  In either
    case the breaker is mutated back to OPEN (with doubled cooldown) and an
    alert is created.

    Returns True when the breaker was re-opened — the caller must return
    immediately.  Returns False when the test task is still in-flight and the
    caller may proceed normally.

    Must only be called when breaker["state"] == "HALF_OPEN".
    """
    from app.alert_dispatch import _maybe_create_circuit_breaker_alert

    test_id = breaker.get("half_open_task_id")
    if not test_id:
        return False

    test_task = await session.get(Task, uuid.UUID(test_id))
    if (
        test_task
        and test_task.started_at
        and (_utcnow() - test_task.started_at).total_seconds() > 1800
    ):
        if expected_legacy_generation is not None:
            await lock_and_validate_legacy_authority(
                session,
                project_id,
                expected_generation=expected_legacy_generation,
            )
        logger.warning(
            f"[CIRCUIT BREAKER] HALF_OPEN test task {test_id} stuck for 30m+, treating as failure → OPEN"
        )
        breaker["state"] = "OPEN"
        breaker["opened_at"] = _utcnow()
        breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
        breaker["half_open_task_id"] = None
        if expected_legacy_generation is not None:
            await commit_legacy_authorized(
                session, project_id, expected_legacy_generation
            )
        await _maybe_create_circuit_breaker_alert(
            project_id,
            session,
            f"HALF_OPEN test task stuck for 30m+ — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)",
            expected_legacy_generation=expected_legacy_generation,
        )
        return True
    elif not test_task:
        if expected_legacy_generation is not None:
            await lock_and_validate_legacy_authority(
                session,
                project_id,
                expected_generation=expected_legacy_generation,
            )
        logger.warning(
            f"[CIRCUIT BREAKER] HALF_OPEN test task {test_id} no longer exists (deleted/reaped) → OPEN"
        )
        breaker["state"] = "OPEN"
        breaker["opened_at"] = _utcnow()
        breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
        breaker["half_open_task_id"] = None
        if expected_legacy_generation is not None:
            await commit_legacy_authorized(
                session, project_id, expected_legacy_generation
            )
        await _maybe_create_circuit_breaker_alert(
            project_id,
            session,
            f"HALF_OPEN test task deleted/reaped — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)",
            expected_legacy_generation=expected_legacy_generation,
        )
        return True

    return False


async def _advance_circuit_breaker_and_check(
    project: "Project",
    session: AsyncSession,
    breaker: dict,
    spawn_label: str,
    *,
    expected_legacy_generation: int | None = None,
) -> bool:
    """Advance OPEN→HALF_OPEN if cooldown elapsed; re-check HALF_OPEN test task.

    Returns True if the circuit breaker is blocking the spawn (caller should
    return immediately), False if spawn is allowed to proceed.
    """
    if breaker["state"] == "OPEN":
        if (
            breaker["opened_at"]
            and (_utcnow() - breaker["opened_at"]).total_seconds()
            >= breaker["cooldown_seconds"]
        ):
            if expected_legacy_generation is not None:
                await lock_and_validate_legacy_authority(
                    session,
                    project.id,
                    expected_generation=expected_legacy_generation,
                )
            breaker["state"] = "HALF_OPEN"
            if expected_legacy_generation is not None:
                await commit_legacy_authorized(
                    session, project.id, expected_legacy_generation
                )
            logger.warning(
                f"[CIRCUIT BREAKER] OPEN → HALF-OPEN for {project.id} (cooldown elapsed, allowing one test task)"
            )
        else:
            logger.warning(
                f"[CIRCUIT BREAKER] Skipping {spawn_label} for project={project.id} "
                f"— circuit breaker state=OPEN (still in cooldown)"
            )
            await _maybe_notify_operator_refresh_deferred(
                project,
                session,
                "paused while the research circuit breaker is open",
                expected_legacy_generation=expected_legacy_generation,
            )
            return True
    elif breaker["state"] == "HALF_OPEN":  # noqa: SIM102
        if await _check_half_open_test_task(
            project.id,
            session,
            breaker,
            expected_legacy_generation=expected_legacy_generation,
        ):
            await _maybe_notify_operator_refresh_deferred(
                project,
                session,
                "paused while the research circuit breaker is open",
                expected_legacy_generation=expected_legacy_generation,
            )
            return True
    return False


def _stage_anomaly_action_alert(
    session: AsyncSession,
    project_id: uuid.UUID,
    anomaly_action: dict,
) -> None:
    """Stage durable anomaly state while legacy authority is still locked."""
    action = anomaly_action.get("action")
    if action == "adjusted" and anomaly_action.get("is_new"):
        pre_vms = anomaly_action.get("pre_adjustment_max_vms") or "?"
        new_vms = anomaly_action["max_concurrent_vms"]
        gate = anomaly_action["failure_gate_threshold"]
        triggers = ", ".join(anomaly_action.get("trigger_anomalies") or [])
        session.add(
            Alert(
                project_id=project_id,
                severity=AlertSeverity.WARNING,
                event_type="research.anomaly_auto_adjusted",
                title="Pipeline auto-adjusted: anomaly threshold exceeded",
                message=(
                    f"max_concurrent_vms reduced from {pre_vms} to {new_vms}, "
                    f"failure_gate_threshold tightened to {gate}. "
                    f"Triggered by: {triggers}."
                ),
            )
        )
    elif action == "restored":
        session.add(
            Alert(
                project_id=project_id,
                severity=AlertSeverity.INFO,
                event_type="research.anomaly_auto_adjusted",
                title="Pipeline restored: anomaly conditions cleared",
                message=(
                    "max_concurrent_vms restored to "
                    f"{anomaly_action['max_concurrent_vms']}, "
                    "failure_gate_threshold restored to "
                    f"{anomaly_action['failure_gate_threshold']}."
                ),
            )
        )


async def _publish_anomaly_action(
    project_id: uuid.UUID,
    anomaly_action: dict,
) -> None:
    """Publish anomaly effects only after the authorized transaction commits."""
    action = anomaly_action.get("action")
    if action == "none":
        return
    from app.broadcast import broadcast_to_project
    from app.notification_dispatch import dispatch_external_notification

    await broadcast_to_project(str(project_id), "anomaly_adjustment", anomaly_action)
    if action == "adjusted" and anomaly_action.get("is_new"):
        asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                str(project_id),
                "ANOMALY_ADJUSTMENT",
                {
                    "max_concurrent_vms": anomaly_action["max_concurrent_vms"],
                    "failure_gate_threshold": anomaly_action[
                        "failure_gate_threshold"
                    ],
                    "trigger_anomalies": anomaly_action.get("trigger_anomalies")
                    or [],
                    "pre_adjustment_max_vms": anomaly_action.get(
                        "pre_adjustment_max_vms"
                    )
                    or "?",
                },
            )
        )
    elif action == "restored":
        asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                str(project_id),
                "ANOMALY_RESTORED",
                {
                    "max_concurrent_vms": anomaly_action["max_concurrent_vms"],
                    "failure_gate_threshold": anomaly_action[
                        "failure_gate_threshold"
                    ],
                },
            )
        )


def _strategic_backstop_due(
    last_strategic_at: datetime | None,
    now: datetime,
    *,
    max_interval_minutes: int | float,
) -> bool:
    """Return whether an automatic strategic backstop interval has elapsed.

    This safety net is independent of the strategic failure breaker. The
    breaker suppresses routine retries after repeated failures; it must not
    suppress an eligible automatic liveness check. This predicate only reports
    elapsed time; its caller must apply the existing automatic Strategic
    admission policy before claiming a backstop firing or creating a cycle.
    """
    if max_interval_minutes <= 0 or last_strategic_at is None:
        return False
    if last_strategic_at.tzinfo is not None:
        last_strategic_at = last_strategic_at.replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return (now - last_strategic_at).total_seconds() / 60.0 >= max_interval_minutes


# Starvation recovery bypasses BOTH the research circuit breaker and the
# ``_strategic_failures < 3`` park, so it needs its own ceiling: the starvation
# sweep re-arms its marker on EVERY reaper tick while the loop stays halted —
# i.e. continuously, precisely when spawned cycles keep failing. Each bypassed
# cycle that fails increments ``_strategic_failures``, so capping the bypass on
# that counter bounds recovery to a few extra attempts beyond the ordinary
# breaker and then parks until the 48h failure decay clears the latch.
_STARVATION_BYPASS_FAILURE_CAP = 6


async def _spawn_research_task_inner(
    project: Project,
    session: AsyncSession,
    *,
    expected_legacy_generation: int | None = None,
    headroom_budget: int | None = None,
):
    """Inner implementation — always called under the per-project spawn lock.

    ``headroom_budget``, when not None, additionally caps the number of
    directions admitted this call (fleet-headroom scaled admission); it also
    gates the EVALUATING-total relaxation behind the
    ``research_headroom_ignores_evaluating`` runtime flag. Returns the number
    of research tasks actually spawned (0 when nothing was admitted). A budget
    of 0 admits nothing at all — including the undirected ``[None]`` fallback —
    so a mid-sweep budget exhaustion cannot slip an extra task past the floor.
    """
    try:
        legacy_generation = (
            await lock_and_validate_legacy_authority(
                session,
                project.id,
                expected_generation=expected_legacy_generation,
            )
        )
        await session.commit()
    except LegacyEngineAuthorityExpired:
        await session.rollback()
        return

    # Failure-breaker decay must run before any availability, budget, bootstrap,
    # or circuit-breaker early return. Otherwise an unavailable model/account can
    # keep the strategic failure latch alive indefinitely, making the sparse
    # wall-clock backstop unreachable even after the 48h recovery window.
    vision_data = project.vision or {}
    strategic_failures = vision_data.get("_strategic_failures", 0)
    if strategic_failures >= 3:
        failures_at = vision_data.get("_strategic_failures_at")
        try:
            last_failure = datetime.fromisoformat(
                str(failures_at).replace("Z", "+00:00")
            )
            if last_failure.tzinfo is not None:
                last_failure = last_failure.astimezone(timezone.utc).replace(tzinfo=None)
            if (_utcnow() - last_failure).total_seconds() > 48 * 3600:

                def _clear_strategic_failures(vision: dict) -> None:
                    vision.pop("_strategic_failures", None)
                    vision.pop("_strategic_failures_at", None)

                await with_vision_locked(session, project.id, _clear_strategic_failures)
                await commit_legacy_authorized(session, project.id, legacy_generation)
                await session.refresh(project)
                strategic_failures = 0
                vision_data = project.vision or {}
                logger.info(
                    f"[STRATEGIC] Auto-decayed _strategic_failures for project {project.id} "
                    f"(last failure: {failures_at})"
                )
        except (ValueError, TypeError, OverflowError):
            pass  # malformed or missing timestamp, keep current value

    if headroom_budget is not None and headroom_budget <= 0:
        logger.info(
            f"[AUTONOMOUS] Fleet headroom budget exhausted ({headroom_budget}) — "
            f"no research admission for project {project.id}"
        )
        return 0

    from app.research_completion import (
        _compute_momentum_factor,
        _compute_productivity_score,
        _compute_acceptance_rate,
        effective_research_finding_headroom,
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
        logger.info(
            "[AUTONOMOUS] Skipping research spawn for project %s — %s",
            project.id,
            "autonomous_enabled is False" if not project.autonomous_enabled else "project is archived",
        )
        return

    # ``autonomous_enabled`` authorizes tactical direction research. Strategic
    # cadence/force work is a separate operator-owned lane so a bounded tactical
    # canary can run without also releasing the strategist. Manual/operator
    # Strategic surfaces never enter this function and remain unaffected.
    automatic_strategic_cycles_allowed = automatic_strategic_cycles_enabled(project)

    # Playbook auto-seed removed: the hardcoded seed entries were specific to the
    # Blueprint pipeline repo and polluted new projects with wrong build/test
    # instructions. Agents discover the correct playbook entries by exploring
    # the actual codebase and writing them via `./playbook set`.
    # Guard: don't spawn if user has no available accounts (avoids immediate failure)
    user_pool = _max_plan_pools.get(project.user_id, [])
    has_available = _research_route_has_available_account(user_pool)
    if not has_available:
        logger.info(
            f"[AUTONOMOUS] Skipping research for {project.name} — no available accounts"
        )
        # Operator steering must never be a silent swallow: if an operator-origin
        # refresh marker is pending, tell them their steering is queued and why
        # before bailing. The marker itself is left intact (we only return), so
        # the steering is still delivered on a later tick once accounts recover.
        await _maybe_notify_operator_refresh_deferred(
            project,
            session,
            "paused while no model accounts are available",
            expected_legacy_generation=legacy_generation,
        )
        return

    # Monthly budget gate: if fleet-wide month-to-date spend has reached 100% of
    # monthly_budget_target_usd, skip autonomous spawning entirely (not just for
    # this project — the fleet-wide budget is exhausted). This prevents autonomous
    # research from starting new work while the budget is maxed out.
    try:
        from app.monthly_budget_alerts import check_monthly_budget

        budget_exhausted, _usage_ratio = await check_monthly_budget(session)
    except Exception:
        budget_exhausted = False
        logger.exception(
            "[AUTONOMOUS] Monthly budget check failed — treating as not exhausted"
        )
    if budget_exhausted:
        logger.info(
            "[AUTONOMOUS] Skipping research spawn for project %s — monthly budget exhausted",
            project.id,
        )
        await _maybe_notify_operator_refresh_deferred(
            project,
            session,
            "paused while the monthly budget target is exhausted",
            expected_legacy_generation=legacy_generation,
        )
        return

    # Daily budget gate: same fail-closed semantics alongside the monthly gate.
    try:
        from app.monthly_budget_alerts import check_daily_budget  # noqa: PLC0415, RUF100

        daily_budget_exhausted, _daily_ratio = await check_daily_budget(session)
    except Exception:
        daily_budget_exhausted = False
        logger.exception(
            "[AUTONOMOUS] Daily budget check failed — treating as not exhausted"
        )
    if daily_budget_exhausted:
        logger.info(
            "[AUTONOMOUS] Skipping research spawn for project %s — daily budget exhausted",
            project.id,
        )
        await _maybe_notify_operator_refresh_deferred(
            project,
            session,
            "paused while the daily spend budget is exhausted",
            expected_legacy_generation=legacy_generation,
        )
        return

    spend_defer_reason = await _project_spend_ceiling_spawn_defer_reason(
        project,
        session,
        expected_legacy_generation=legacy_generation,
    )
    if spend_defer_reason:
        logger.info(
            "[AUTONOMOUS] Skipping research spawn for project %s — %s",
            project.id,
            spend_defer_reason,
        )
        await _maybe_notify_operator_refresh_deferred(
            project,
            session,
            "paused while the project spend ceiling is reached",
            expected_legacy_generation=legacy_generation,
        )
        return

    # The starvation sweep arms this exact qualification refresh as the
    # unattended recovery path for a halted Strategic loop. Unlike ordinary
    # qualification refreshes, it must be allowed through an OPEN breaker:
    # the breaker is open precisely because the loop has already failed, and
    # waiting for it to cool would leave the sweep's promised self-heal marker
    # unread. Keep the reason/origin narrow so unrelated qualification work
    # remains breaker-protected, and keep the arm itself bounded by the raised
    # ``_STARVATION_BYPASS_FAILURE_CAP`` failure ceiling: a loop whose recovery
    # cycles keep failing stops bypassing and parks instead of re-firing forever.
    from app.strategic_trigger import (
        ORIGIN_QUALIFICATION as _ORIGIN_QUALIFICATION,
        strategic_refresh_due,
        strategic_refresh_origin as _strategic_refresh_origin,
    )

    _starvation_refresh_marker = vision_data.get("_strategic_refresh")
    _starvation_refresh_due = (
        automatic_strategic_cycles_allowed
        and isinstance(_starvation_refresh_marker, dict)
        and _starvation_refresh_marker.get("reason") == "strategic_cycle_starvation"
        and _strategic_refresh_origin(vision_data) == _ORIGIN_QUALIFICATION
        and strategic_failures < _STARVATION_BYPASS_FAILURE_CAP
        and strategic_refresh_due(
            vision_data,
            _utcnow(),
            min_interval_seconds=_runtime_config.get(
                "strategic_refresh_min_interval_seconds", 120
            ),
        )
    )

    # Guard: circuit breaker must allow spawning. A struggling project (breaker
    # OPEN) is exactly when an operator is most likely to steer; never swallow
    # that silently. Generic refresh markers are preserved — only the spawn
    # returns — so steering still lands once the breaker clears. The explicit
    # starvation recovery arm above is the narrow exception: it must wake the
    # halted loop that caused the breaker to open.
    #
    # The check itself always runs (it owns the OPEN→HALF_OPEN cooldown advance
    # and the one-time operator deferral notice). Only the *return* is bypassed,
    # and only for the strategic recovery spawn: several branches below reset
    # ``is_strategic`` and fall through to ordinary tactical research, which must
    # stay breaker-protected. ``_breaker_bypassed_for_starvation`` carries that
    # obligation to the re-assertion immediately before the tactical path.
    breaker = _shared_state.get_circuit_breaker(str(project.id))
    _breaker_blocks = await _advance_circuit_breaker_and_check(
        project,
        session,
        breaker,
        "autonomous research spawn",
        expected_legacy_generation=legacy_generation,
    )
    if _breaker_blocks and not _starvation_refresh_due:
        return
    _breaker_bypassed_for_starvation = _breaker_blocks
    # Guard: defer autonomous research until repo_bootstrap completes for external-repo
    # projects. Strategic cycles already defer this way; keeping research consistent
    # prevents ungrounded findings against a half-onboarded repo. Terminal bootstraps
    # (FAILED/COMPLETED) proceed gracefully so a degraded bootstrap never deadlocks.
    from app.strategic_spawning import _bootstrap_pending

    if await _bootstrap_pending(project, session):
        await _maybe_notify_operator_refresh_deferred(
            project,
            session,
            "repo_bootstrap task is pending (bootstrap_completed_at not set)",
            expected_legacy_generation=legacy_generation,
        )
        logger.info(
            "[AUTONOMOUS] Deferring research spawn for project %s — "
            "repo_bootstrap task is pending (bootstrap_completed_at not set)",
            project.id,
        )
        return

    # Guard: must have active directions that aren't in cooldown or mid-cycle
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
    from sqlalchemy import and_
    from app.failure_classification import INFRA_CLASSES

    busy_direction_ids: set = set()
    # Directions whose spawn this cycle is the ONE pipelined cycle allowed
    # alongside a genuinely-stuck EVALUATING parent. Tracked so the spawn loop
    # claims them under the ``:pipelined`` idempotency key — the stuck parent
    # still holds the primary per-direction claim while EVALUATING.
    pipelined_direction_ids: set = set()
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
    active_research_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    ]
    active_research = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    or_(
                        Task.status.in_(active_research_statuses),
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
                                TaskStatus.BLOCKED,
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
            rdid = _research_payload_direction_id(rpayload)
            if rdid:
                recent_research_by_direction.setdefault(rdid, []).append(rt.id)

    for t in active_research:
        payload = t.first_payload
        if payload.get("type") == "research":
            # current_direction_id identifies the targeted direction; null means
            # undirected/cross-cutting analysis which doesn't block any direction.
            current_did = _research_payload_direction_id(payload)
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
                            for rid in recent_research_by_direction.get(current_did, [])
                            if rid != t.id
                        ]
                        if not recent_others:
                            # No prior pipelined spawn — allow one. Record the
                            # direction so the spawn loop claims it under the
                            # :pipelined idempotency key (the stuck EVALUATING
                            # parent still holds the primary claim).
                            pipelined_direction_ids.add(current_did)
                            continue
                        # Already pipelined within the window — treat as busy.
                    # else: progressing (children active or PR open) → busy
            busy_direction_ids.add(current_did)

    active_directions = await _process_direction_lifecycle(
        project,
        session,
        busy_direction_ids,
        now,
        expected_legacy_generation=legacy_generation,
    )
    # Allow strategic cycles to proceed without active directions so they can
    # assess the portfolio and any concrete uncovered problem-model gaps.
    # Stop forcing after 3 consecutive strategic failures to prevent infinite loops.
    vision_data = project.vision or {}
    strategic_failures = vision_data.get("_strategic_failures", 0)
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
    # Purpose resolves through problem_model.intent.purpose (vision.purpose is
    # the deprecated legacy fallback) — reading only vision here made every
    # problem-model project look purposeless, forcing a strategic cycle on
    # every tick (rate-limited only by the spawn cooldown).
    from app.problem_model import project_has_purpose as _project_has_purpose

    force_strategic = (
        automatic_strategic_cycles_allowed
        and strategic_failures < 3
        and (
            not _project_has_purpose(project)
            or not any_active_in_project
            or all_dirs_saturated
        )
    )
    # An operator steering refresh must schedule the strategist promptly even
    # when the failure breaker would otherwise suppress forcing (and the
    # `not active_directions` early-return below) — that is exactly the moment an
    # operator needs to intervene. The active-cycle / min-interval gates in the
    # spawn block still apply (and surface a one-time deferral notice); only the
    # _strategic_failures breaker is bypassed for operator intent.
    from app.strategic_trigger import (
        ORIGIN_OPERATOR as _ORIGIN_OPERATOR,
        refresh_breaker_applies,
    )

    if (
        automatic_strategic_cycles_allowed
        and _strategic_refresh_origin(vision_data) == _ORIGIN_OPERATOR
    ) or _starvation_refresh_due:
        force_strategic = True
    elif automatic_strategic_cycles_allowed and not force_strategic:
        _refresh_min_interval = _runtime_config.get(
            "strategic_refresh_min_interval_seconds", 120
        )
        # Operator steering bypasses the _strategic_failures breaker — an
        # explicit human request is exactly when an operator needs to
        # intervene (and exactly when the breaker is likely tripped).
        # Automated qualification refreshes still park under the breaker.
        _breaker_gate_ok = (
            not refresh_breaker_applies(project.vision) or strategic_failures < 3
        )
        if _breaker_gate_ok and strategic_refresh_due(
            project.vision, now, min_interval_seconds=_refresh_min_interval
        ):
            force_strategic = True
    # Wall-clock backstop: when the autonomous Strategic lane owns authorship,
    # drive force_strategic so it bypasses the `not active_directions` early
    # return below. Without this promotion, when every active direction has an
    # in-flight child (busy_direction_ids), _process_direction_lifecycle empties
    # active_directions and we return here — never reaching the strategic spawn
    # block, no matter how long since the last cycle. Resident-owned projects
    # deliberately skip this check: their autonomous loop cannot satisfy the
    # firing claim without racing the Resident lane.
    if automatic_strategic_cycles_allowed and not force_strategic:
        strategic_max_interval = _runtime_config.get(
            "strategic_max_interval_minutes", 1_440
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
            backstop_now = _utcnow()
            if _strategic_backstop_due(
                last_strategic_at,
                backstop_now,
                max_interval_minutes=strategic_max_interval,
            ):
                normalized_last_strategic_at = last_strategic_at
                if normalized_last_strategic_at.tzinfo is not None:
                    normalized_last_strategic_at = normalized_last_strategic_at.replace(
                        tzinfo=None
                    )
                age_minutes = (
                    backstop_now - normalized_last_strategic_at
                ).total_seconds() / 60.0
                logger.info(
                    f"[STRATEGIC] Wall-clock backstop firing for project {project.id}: "
                    f"last strategic task created {age_minutes:.1f}min ago "
                    f"(threshold {strategic_max_interval}min, "
                    f"active_directions={len(active_directions)}, busy={len(busy_direction_ids)})"
                )
                force_strategic = True
    if not active_directions and not force_strategic:
        # When every active direction is held in refractory (none eligible this
        # cycle), request a coalesced strategic coverage refresh so the
        # strategist can explicitly reconsider a held direction.
        # The strategist may clear refractory_until on a direction to bypass.
        # The configurable sparse wall-clock backstop above remains the safety net.
        _any_refractory = any(
            _direction_in_refractory(d, now)
            for d in (project.directions or [])
            if d.get("status") == "active"
        )
        if _any_refractory and automatic_strategic_cycles_allowed:
            from app.strategic_trigger import (
                ORIGIN_QUALIFICATION as _OQ,
                request_strategic_refresh as _req_strat,
            )

            await _req_strat(
                session,
                project.id,
                reason="all active directions in refractory — strategic coverage review needed",
                origin=_OQ,
            )
            force_strategic = True
            logger.info(
                "[AUTONOMOUS] All active directions in refractory for project %s; "
                "fired strategic refresh trigger",
                project.id,
            )
        else:
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
        # Best-effort only: the xact-scoped lock drops at the next commit (and
        # several paths below commit mid-flow), so the hard guarantee against
        # duplicate spawns is the per-direction idempotency-key claim stamped
        # on the Task rows in the spawn loop below.
        if not await _try_research_spawn_advisory_lock(session, project.id):
            logger.info(
                f"[AUTONOMOUS] Research spawn for project {project.id} skipped — "
                f"another instance holds the advisory lock"
            )
            return

        max_parallel = project_parallelism_cap(
            project, "max_parallel_research", "max_parallel_research_per_project", 6
        )
        directed_cooldown_minutes = _runtime_config.get(
            "directed_research_cooldown_minutes", 120
        )
        # Admission-aware selection: a direction whose live discovery cycle
        # cannot admit a new research root (mirrors the predicate in
        # ensure_discovery_cycle_for_research) would consume one of the
        # max_parallel candidate slots only to be skipped at spawn admission.
        # With a low cap the same blocked direction then head-of-line blocks
        # every other eligible direction on every sweep, so exclude them here.
        from app.discovery_admission import (
            load_direction_discovery_admission_decisions,
        )

        discovery_admission = await load_direction_discovery_admission_decisions(
            session,
            project.id,
            list(project.directions or []),
        )
        cycle_blocked_keys = {
            key
            for key, admission in discovery_admission.items()
            if not admission.can_admit_root
        }
        for d in sorted_eligible:
            did = d.get("id")
            if did in cycle_blocked_keys:
                admission = discovery_admission[did]
                logger.info(
                    "[AUTONOMOUS] skipped direction %s: %s; next action: %s",
                    did,
                    admission.reason,
                    admission.next_action,
                )
                continue
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
            if directed_cooldown_minutes > 0:
                last_researched = d.get("last_researched_at")
                if last_researched:
                    try:
                        last_dt = datetime.fromisoformat(last_researched).replace(
                            tzinfo=None
                        )
                        age_minutes = (now - last_dt).total_seconds() / 60.0
                        if age_minutes < directed_cooldown_minutes:
                            logger.info(
                                "[AUTONOMOUS] skipped direction %s: last research "
                                "%.1f min ago (cooldown %d min)",
                                did,
                                age_minutes,
                                directed_cooldown_minutes,
                            )
                            continue
                    except (ValueError, TypeError):
                        pass
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
    if automatic_strategic_cycles_allowed and pending_sr:
        _sr_attempt_count = pending_sr.get("attempt_count", 0)
        _sr_first_attempted_at = pending_sr.get("first_attempted_at")
        _sr_max_attempts = _runtime_config.get("pending_sr_max_attempts", 3)
        _sr_max_age_hours = _runtime_config.get("pending_sr_max_age_hours", 24)

        # Check if the marker has exceeded the retry cap or age window.
        _sr_age_exceeded = False
        if _sr_first_attempted_at:
            try:
                _first_dt = datetime.fromisoformat(_sr_first_attempted_at).replace(
                    tzinfo=timezone.utc
                )
                _sr_age_exceeded = (
                    _utcnow().replace(tzinfo=timezone.utc) - _first_dt
                ) > timedelta(hours=_sr_max_age_hours)
            except (ValueError, TypeError):
                pass

        if _sr_attempt_count >= _sr_max_attempts or _sr_age_exceeded:
            _reason = (
                f"attempt_count={_sr_attempt_count} >= max={_sr_max_attempts}"
                if _sr_attempt_count >= _sr_max_attempts
                else f"marker age exceeded {_sr_max_age_hours}h (first_attempted_at={_sr_first_attempted_at})"
            )
            logger.error(
                f"[STRATEGIC] Pending SR cap/age exceeded for project {project.id}: {_reason} — dropping marker and alerting"
            )

            def _drop_pending_sr(vision: dict) -> None:
                vision.pop("_pending_strategic_review", None)

            await with_vision_locked(session, project.id, _drop_pending_sr)
            await commit_legacy_authorized(session, project.id, legacy_generation)
            await session.refresh(project)

            from app.alert_dispatch import _create_strategic_alert

            await _create_strategic_alert(
                project.id,
                session,
                f"Pending strategic review retry abandoned after {_reason}. "
                f"Original failure: {pending_sr.get('error', 'unknown')}",
                expected_legacy_generation=legacy_generation,
            )
            # Fall through to tactical research below
        else:
            try:
                from app.strategic_spawning import _spawn_strategic_review

                _sr_suppression: dict[str, str] = {}
                if await _spawn_strategic_review(
                    project,
                    session,
                    apply_spawn_budget_guard=True,
                    expected_legacy_generation=legacy_generation,
                    cycle_id=await strategic_cycle.active_cycle_id_for_retry(
                        session, pending_sr.get("cycle_id")
                    ),
                    operator_steered=bool(pending_sr.get("operator_steered")),
                    steering_excerpt=pending_sr.get("steering_excerpt"),
                    preferred_runner_target_id=pending_sr.get(
                        "preferred_runner_target_id"
                    )
                    if isinstance(pending_sr.get("preferred_runner_target_id"), str)
                    else None,
                    local_runner_context={
                        STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
                        STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: str(
                            pending_sr.get(STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY)
                            or "workbench_continuous"
                        ),
                    }
                    if pending_sr.get(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY)
                    else None,
                    suppression_reason_out=_sr_suppression,
                ):

                    def _clear_pending_sr(vision: dict) -> None:
                        vision.pop("_pending_strategic_review", None)

                    await with_vision_locked(session, project.id, _clear_pending_sr)
                    await commit_legacy_authorized(
                        session, project.id, legacy_generation
                    )
                    await session.refresh(project)
                    logger.info(
                        f"[STRATEGIC] Retried pending strategic review for project {project.id} (originally failed at {pending_sr.get('failed_at')})"
                    )
                    return  # Don't spawn tactical research this cycle
                else:
                    from app.strategic_cycle_admission import (
                        AUTOMATIC_STRATEGIC_CYCLES_DISABLED,
                    )
                    from app.task_locking import (
                        AUTOMATIC_TASK_CREATION_PROJECT_ARCHIVED,
                        AUTOMATIC_TASK_CREATION_PROJECT_PAUSED,
                        AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE,
                    )

                    _authority_suppressions = {
                        AUTOMATIC_STRATEGIC_CYCLES_DISABLED,
                        AUTOMATIC_TASK_CREATION_PROJECT_ARCHIVED,
                        AUTOMATIC_TASK_CREATION_PROJECT_PAUSED,
                        AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE,
                    }
                    _suppression_reason = _sr_suppression.get("reason")
                    if _suppression_reason in _authority_suppressions:
                        logger.info(
                            "[STRATEGIC] Pending SR retry held without spending "
                            "attempt budget for project %s (%s)",
                            project.id,
                            _suppression_reason,
                        )
                    else:
                        logger.warning(
                            f"[STRATEGIC] Pending SR retry skipped (active strategic task) for project {project.id}"
                        )
                        _now_iso = _utcnow().isoformat()

                        def _bump_pending_sr_skip(vision: dict) -> None:
                            marker = vision.get("_pending_strategic_review")
                            if marker is None:
                                return
                            marker["attempt_count"] = (
                                marker.get("attempt_count", 0) + 1
                            )
                            if not marker.get("first_attempted_at"):
                                marker["first_attempted_at"] = _now_iso

                        await with_vision_locked(
                            session, project.id, _bump_pending_sr_skip
                        )
                        await commit_legacy_authorized(
                            session, project.id, legacy_generation
                        )
                        await session.refresh(project)
                    # Fall through to tactical research
            except LegacyEngineAuthorityExpired:
                raise
            except Exception:
                logger.exception(
                    f"[STRATEGIC] Retry of pending strategic review failed for project {project.id}"
                )
                _now_iso = _utcnow().isoformat()

                def _bump_pending_sr(vision: dict) -> None:
                    marker = vision.get("_pending_strategic_review")
                    if marker is None:
                        return
                    marker["attempt_count"] = marker.get("attempt_count", 0) + 1
                    if not marker.get("first_attempted_at"):
                        marker["first_attempted_at"] = _now_iso

                await with_vision_locked(session, project.id, _bump_pending_sr)
                await commit_legacy_authorized(
                    session, project.id, legacy_generation
                )
                await session.refresh(project)
                # Fall through to tactical research

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
    is_strategic = automatic_strategic_cycles_allowed and (
        strategic_cadence_due(
            research_spawn_count,
            (project.vision or {}).get(STRATEGIC_LAST_ACCEPTED_RESEARCH_COUNT_KEY),
            strategic_frequency,
        )
        or force_strategic
    )
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
    # Cross-instance double-spawn guard. Two orchestrator instances — or two idle
    # ticks racing — can both pass the unlocked has_active_strategic check below
    # before either's spawn commits, double-spawning a strategic cycle. That burns
    # a redundant agent run and races the two cycles through the phase state
    # machine into false failures. Take the per-project research-spawn advisory
    # lock for the check-and-spawn HEAD: the lock is transaction-scoped and the
    # spawn path DOES commit mid-build (``_build_recent_interventions`` releases
    # its read transaction before GitHub ancestry I/O, #8842), so this
    # acquisition only covers the checks below, not the full build→INSERT.
    # ``_spawn_strategic_review`` re-takes the lock and re-runs the active-task
    # check immediately before its INSERT to close the tail of the window
    # (2026-06-11 double-spawn). If a peer instance holds it, skip strategic this tick — the
    # peer is already spawning, and the operator-refresh marker survives an
    # un-spawned tick (consumed on the follow-up tick). Best-effort but no-op in
    # tests (the advisory lock reports no contention on non-Postgres dialects).
    if is_strategic and not await _try_research_spawn_advisory_lock(
        session, project.id
    ):
        logger.debug(
            f"[STRATEGIC] Skipping strategic spawn — another worker holds the "
            f"research-spawn lock for project {project.id}; retrying next tick"
        )
        is_strategic = False

    if is_strategic:
        from app.task_lifecycle import _has_active_strategic_task

        has_active_strategic = await _has_active_strategic_task(project.id, session)
        if has_active_strategic:
            logger.debug(
                f"[STRATEGIC] Skipping strategic spawn — active strategic task exists for "
                f"project {project.id}; falling through to tactical"
            )
            # An operator steering request is deferred behind the running cycle
            # (it will consume on the follow-up tick). Surface that once so the
            # operator knows their message is queued, not silently swallowed.
            await _maybe_notify_operator_refresh_deferred(
                project,
                session,
                "queued behind an active strategic cycle",
                expected_legacy_generation=legacy_generation,
            )
            is_strategic = (
                False  # skip the spawn block; tactical research below may still fire
            )
        else:
            from sqlalchemy import func as _strat_func

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
                # guards against. Gated by the _strategic_failures breaker
                # (< 3, or < _STARVATION_BYPASS_FAILURE_CAP for the starvation
                # recovery arm) and a min-interval floor, so a broken request
                # that keeps failing stops bypassing instead of looping.
                _refresh_min_interval = _runtime_config.get(
                    "strategic_refresh_min_interval_seconds", 120
                )
                _breaker_gate_ok = (
                    _starvation_refresh_due
                    or not refresh_breaker_applies(project.vision)
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
                        (project.vision or {})
                        .get("_strategic_refresh", {})
                        .get("reason"),
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
        # No-delta gate: before assembling the full strategic context (the
        # expensive ~137KB payload re-sent across ~17 turns), check whether
        # anything durable changed since the last completed cycle. A genuinely
        # quiet project skips the cycle entirely — recorded on vision (bounded
        # skip log + counter) and NEVER advancing the watermark, which stays
        # derived from the StrategicCycle table so a delta arriving mid-check
        # is caught on the next tick. The gate is conservative: any ambiguous
        # signal, or any must-run condition (bootstrap / pending refresh /
        # open operator message / no purpose / no active directions /
        # saturation / prior-cycle failure), RUNS the cycle.
        from app.strategic_cycle_gating import (
            record_no_delta_skip,
            should_skip_strategic_cycle,
        )

        gate_decision = await should_skip_strategic_cycle(
            session, project, force_strategic=force_strategic
        )
        if gate_decision.skip:
            # Log the gate's OWN reason. This branch also carries the
            # consecutive-stall cutoff, where "nothing meaningful changed" is
            # actively false and hid the one line an operator needed.
            logger.info(
                "[STRATEGIC] gate: skipping strategic cycle for project %s "
                "(watermark=%s, signals=%s) — %s",
                project.id,
                gate_decision.watermark,
                gate_decision.signals.as_dict(),
                gate_decision.reason,
            )
            await record_no_delta_skip(
                session,
                project.id,
                gate_decision,
                expected_legacy_generation=legacy_generation,
            )
            # The vision write committed and expired `project`; re-load so the
            # fall-through tactical path reads fresh state.
            await session.refresh(project)
            is_strategic = False
        else:
            logger.debug(
                "[STRATEGIC] no-delta gate: running cycle for project %s "
                "(must_run=%s, signals=%s)",
                project.id,
                gate_decision.must_run_reasons,
                gate_decision.signals.as_dict(),
            )

    if is_strategic:
        # Determine if this strategic cycle should also include architecture review.
        # Architecture stabilizes faster than strategy, so it runs less frequently.
        # Additionally, if the design_doc is empty (no architecture content), force
        # an architecture cycle to establish the baseline before strategy reasons
        # about an unfamiliar codebase.
        strategic_cycle_count = (project.vision or {}).get(
            "_strategic_cycle_count", 0
        ) + 1
        arch_frequency = _runtime_config.get("architecture_review_frequency", 3)
        # Architecture cycle triggers on the modulo counter OR when this is the first
        # strategic cycle and design_doc is empty (no systems), ensuring newly-onboarded
        # projects build architectural context on their first strategic pass rather than
        # spending cycles on undirected research. Design_doc stores systems as keys
        # like "systems/sys-id" (not under a "systems" key).
        design_doc = project.design_doc or {}
        has_design_doc_systems = any(
            k.startswith("systems/") for k in design_doc if isinstance(k, str)
        )
        is_first_cycle = strategic_cycle_count == 1
        is_architecture_cycle = (
            arch_frequency > 0 and strategic_cycle_count % arch_frequency == 0
        ) or (is_first_cycle and not has_design_doc_systems)

        spawned = False
        try:
            if is_architecture_cycle:
                # Full pipeline: Phase 1 (architecture) → Phase 2 (strategic) via completion handler
                from app.strategic_spawning import _spawn_design_doc_update

                spawned = await _spawn_design_doc_update(
                    project,
                    session,
                    apply_spawn_budget_guard=True,
                    expected_legacy_generation=legacy_generation,
                    cadence_accepted_count=research_spawn_count,
                )
                if spawned:
                    logger.info(
                        f"[AUTONOMOUS] Spawned design doc update (strategic cycle #{strategic_cycle_count}, "
                        f"vision rev {(project.vision or {}).get('revision', 0)})"
                    )
            else:
                # Strategy-only: skip architecture, go straight to strategic review
                from app.strategic_spawning import _spawn_strategic_review

                spawned = await _spawn_strategic_review(
                    project,
                    session,
                    apply_spawn_budget_guard=True,
                    expected_legacy_generation=legacy_generation,
                    cadence_accepted_count=research_spawn_count,
                )
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
                        event_type="self_improvement.payload_too_large",
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
            await commit_legacy_authorized(session, project.id, legacy_generation)
            # session.commit() expired `project`; the notify helper and the
            # fall-through `if spawned:` block below both read project.vision/.id.
            # Re-load it so those reads do not lazy-load from async context and
            # raise MissingGreenlet on asyncpg.
            await session.refresh(project)
            # A pending operator steering marker survives this skip (the spawn
            # never happened, so it is not consumed). Tell the operator their
            # steering is paused on payload bloat rather than leaving it silent.
            await _maybe_notify_operator_refresh_deferred(
                project,
                session,
                "strategic refresh paused — context payload exceeds the size limit",
                expected_legacy_generation=legacy_generation,
            )

        if spawned:
            # Capture, inside the lock, whether the consumed marker was
            # operator-origin — read BEFORE clear_strategic_refresh wipes it so
            # the symmetric success notice can fire only for operator steering.
            _consumed: dict = {}

            def _record_strategic_spawn(vision: dict) -> None:
                record_strategic_cadence_fire(
                    vision,
                    accepted_count=research_spawn_count,
                )
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
            # The spawner committed the cadence receipt atomically with its
            # Task/StrategicCycle. This second write is deliberately idempotent
            # and commits the remaining cycle counter, bloat cleanup, and
            # refresh-marker consume before any tactical early-return below.
            # If this outer commit faults, the born-task receipt still prevents
            # replaying the same accepted-count cadence boundary.
            await commit_legacy_authorized(session, project.id, legacy_generation)

            # Symmetric success counterpart to the deferral notice: when the
            # consumed marker was operator-origin AND carried preserved steering
            # text, confirm the strategist woke and is acting on the steering.
            # Without an excerpt the woken cycle has no directive to act on, so
            # suppress the confirmation rather than promise steering that the
            # strategist cannot actually see. Best-effort — never fatal to the
            # spawn that already succeeded and committed above.
            if _consumed.get("operator") and _consumed.get("excerpt"):
                await _maybe_notify_operator_steering_acted(
                    project,
                    session,
                    expected_legacy_generation=legacy_generation,
                )

    # A strategic-branch rollback above expires `project`; the tactical guards
    # below read its columns through the SYNC project_parallelism_cap(), whose
    # lazy-load would open a connection outside the greenlet on
    # PostgreSQL/asyncpg (MissingGreenlet). Re-load the row here so those reads
    # never lazy-load — mirrors the refreshes the strategic paths above already
    # perform after committing.
    await session.refresh(project)

    # Re-assert the circuit breaker before the tactical path. The starvation
    # bypass above is scoped to the strategic recovery spawn ONLY — it exists to
    # wake the halted loop that opened the breaker, not to admit ordinary
    # direction research. Every branch above that resets ``is_strategic``
    # (advisory-lock contention, an already-active strategic task, the spawn
    # cooldown, the no-delta gate) falls through to here, so without this the
    # bypass would silently spawn tactical research with the breaker OPEN.
    if _breaker_bypassed_for_starvation:
        logger.info(
            "[AUTONOMOUS] Skipping tactical research spawn for project %s — "
            "circuit breaker is open (the starvation bypass covers only the "
            "strategic recovery spawn)",
            project.id,
        )
        return

    # Guard: limit parallel research tasks per project.
    # EVALUATING parents are just waiting on children — they don't block new research.
    max_parallel = project_parallelism_cap(
        project, "max_parallel_research", "max_parallel_research_per_project", 6
    )
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
                Task.payloads[0]["type"].as_string() == "research",
            )
        )
    ).scalar() or 0
    if active_research_count >= max_parallel:
        logger.info(
            "[AUTONOMOUS] Parallel research cap reached: %s/%s — skipping spawn for project %s",
            active_research_count,
            max_parallel,
            project.id,
        )
        return
    # Second cap: total research in any non-terminal state. Pending rollups in
    # EVALUATING accumulate merge complexity — each new spawn produces another
    # rollup branch competing for the same base. This cap prevents the pile-up
    # that makes downstream merges incrementally more conflict-prone. BUT an
    # EVALUATING research parent parks atomically with its child-task INSERTs
    # (research_completion flips to EVALUATING in the same commit that spawns
    # children) and only leaves EVALUATING once those children resolve into a
    # rollup PR. Counting such parents against the admission cap throttles the
    # system harder the more work it ships: the more productive research is,
    # the less new research can start. Exclude EVALUATING parents that still
    # have active children (productive in-flight work, mirrored by the
    # parents_with_active_children set computed for the pipelining guard above);
    # genuinely-stuck parents — EVALUATING with all children terminal — still
    # count, so the merge-complexity rationale survives.
    max_total = project_parallelism_cap(
        project, "max_total_research", "max_total_research_per_project", 5
    )
    # The total cap normally includes EVALUATING parents (pending rollups pile
    # up merge complexity on the same base). When the operator opts into the
    # headroom relaxation AND the fleet has headroom above the admission floor
    # (headroom_budget > 0), EVALUATING parents — which wait on rollup merges,
    # not VM slots — stop counting so an idle fleet does not let a pile of
    # pending rollups starve new research.
    _headroom_above_floor = headroom_budget is not None and headroom_budget > 0
    _total_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
    ]
    if not (
        _runtime_config.get("research_headroom_ignores_evaluating", True)
        and _headroom_above_floor
    ):
        _total_statuses.append(TaskStatus.EVALUATING)
    total_research_count = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == project.id,
                Task.status.in_(_total_statuses),
                Task.payloads[0]["type"].as_string() == "research",
            )
        )
    ).scalar() or 0
    # The productive-EVALUATING subtraction only applies when EVALUATING was
    # counted at all — under the headroom relaxation the statuses above already
    # exclude EVALUATING entirely, and subtracting parents that were never
    # counted would under-count the remaining classes.
    if parents_with_active_children and TaskStatus.EVALUATING in _total_statuses:
        productive_evaluating = (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.project_id == project.id,
                    Task.status == TaskStatus.EVALUATING,
                    Task.payloads[0]["type"].as_string() == "research",
                    Task.id.in_(parents_with_active_children),
                )
            )
        ).scalar() or 0
        total_research_count = max(0, total_research_count - productive_evaluating)
    if total_research_count >= max_total:
        logger.info(
            f"[AUTONOMOUS] Total research cap reached: {total_research_count}/{max_total} "
            f"({'excluding all EVALUATING — headroom relaxation' if TaskStatus.EVALUATING not in _total_statuses else 'excluding EVALUATING parents with active children'}) "
            f"— skipping spawn for project {project.id}"
        )
        return
    # Reduce chosen_directions to the smaller of the two remaining budgets,
    # plus the fleet-headroom budget when one was supplied (scaled admission).
    remaining_slots = min(
        max_parallel - active_research_count, max_total - total_research_count
    )
    if headroom_budget is not None:
        remaining_slots = min(remaining_slots, headroom_budget)
    chosen_directions = chosen_directions[:remaining_slots]
    # Guard: global rate limit across ALL projects
    one_hour_ago = _utcnow() - timedelta(hours=1)
    budget_snapshot = None
    try:
        budget_snapshot = await compute_autonomous_spawn_budget_snapshot(
            session,
            runtime_config=_runtime_config,
            now=one_hour_ago + timedelta(hours=1),
        )
        logger.info(
            "[AUTONOMOUS] Spawn budget snapshot: %s",
            budget_snapshot.as_log_fields(),
        )
    except Exception as exc:  # snapshot read failed; fail posture is mode-dependent
        # A persistent snapshot/DB failure must NOT silently disable an
        # operator-enabled 'enforce' budget (that would be the exact opposite
        # of enforcement). The configured mode comes from runtime_config and
        # needs no DB, so we can still read it here. Fail CLOSED under enforce
        # (block the spawn) and preserve fail-open for the observational
        # off/observe modes where the guard is only advisory.
        try:
            mode = autonomous_spawn_budget_config_from_runtime(_runtime_config).mode
        except Exception:  # noqa: BLE001 - malformed config; stay observational
            mode = DEFAULT_AUTONOMOUS_SPAWN_BUDGET_MODE
        if mode == "enforce":
            logger.exception(
                "[AUTONOMOUS] Spawn budget snapshot failed in enforce mode; "
                "failing CLOSED (blocking research spawn)"
            )
            return
        logger.warning(
            "[AUTONOMOUS] Spawn budget calculation failed open (mode=%s): %s",
            mode,
            exc,
        )
    if budget_snapshot is not None:
        budget_guard = build_autonomous_spawn_budget_guard(
            snapshot=budget_snapshot,
            spawn_category="initiative",
            enforce_initiative=budget_snapshot.mode == "enforce",
        )
        if budget_guard is not None:
            logger.warning("[AUTONOMOUS] Spawn budget block: %s", budget_guard)
            return
    else:
        global_max = _runtime_config.get(
            "max_global_autonomous_tasks_per_hour",
            _runtime_config["max_autonomous_tasks_per_hour"] * 10,
        )
        global_recent = (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    autonomous_spawn_task_clause(),
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
                Task.payloads[0]["type"].as_string() == "research",
            )
        )
    ).scalar() or 0
    per_hour_cap = project_parallelism_cap(
        project, "max_autonomous_tasks_per_hour", "max_autonomous_tasks_per_hour", 12
    )
    if recent_research_count >= per_hour_cap:
        logger.info(
            "[AUTONOMOUS] Per-hour research rate limit reached: %s/%s — skipping spawn for project %s",
            recent_research_count,
            per_hour_cap,
            project.id,
        )
        return

    # Also cap by per-project rate limit
    rate_limit_remaining = per_hour_cap - recent_research_count
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

    # Model/provider/effort routing is resolved once, at dispatch, from the
    # unified task_type_routing/category_routing policy.  Do not copy the legacy
    # task_type_models/task_type_efforts maps into this task's payload: payload
    # fields outrank unified routing and can silently pin every autonomous root
    # to an obsolete model or lower effort.
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
    # Apply/publish only at the final fenced transaction below. A stale legacy
    # tick must not mutate fleet policy or emit operator-visible effects.
    anomaly_action = {"action": "none"}
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
                event_type="research.anomaly_auto_adjusted",
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
                event_type="research.anomaly_auto_adjusted",
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

    # All candidate/policy preparation above is read-only.  Linearize the
    # actual automatic admissions with operator pause now, before taking any
    # direction/discovery child lock and keep the Project lock through the
    # batched Task INSERT commit below.
    admission_project_id = project.id
    admission = await acquire_automatic_task_creation_authority(
        session, admission_project_id, skip_locked=True
    )
    if admission != AUTOMATIC_TASK_CREATION_ALLOWED:
        await session.rollback()
        logger.info(
            "[AUTONOMOUS] Research admission closed for project %s (%s)",
            admission_project_id,
            admission,
        )
        return 0

    spawned_tasks = []
    for chosen_direction in directions_to_spawn:
        _mode = normalized_recommended_mode(chosen_direction)
        # Build lightweight direction summaries — include compact history for
        # agent-side stats (aggregateDirectionStats, buildScopeCalibrationSignal).
        # Full histories are available via the API. This keeps the payload under
        # the OS E2BIG limit (~128KB).
        _HISTORY_SUMMARY_FIELDS = {
            "tasks_created",
            "prs_merged",
            "tasks_failed",
            "failed_task_details",
            "finding_titles",
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
                "status": d.get("status") or "active",
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
            if isinstance(ctx, str) and ctx:
                summary["research_context"] = ctx[:2000]
            # Include compact history for agent-side analysis functions
            raw_history = d.get("research_history") or []
            raw_agg = d.get("history_aggregate") or {}
            agg_compact = {k: raw_agg[k] for k in _AGGREGATE_FIELDS if k in raw_agg}
            if agg_compact:
                summary["history_aggregate"] = agg_compact
            if d.get("id") == current_dir_id:
                # The selected direction gets its bounded working envelope.
                # Do not copy these fields for every direction: notes and paths
                # are user-authored and can otherwise breach the ~128KB agent
                # launch limit this summary exists to protect.
                paths = d.get("paths")
                if isinstance(paths, list):
                    summary["paths"] = [
                        path[:300]
                        for path in paths[:30]
                        if isinstance(path, str) and path
                    ]
                notes = d.get("notes")
                if isinstance(notes, list):
                    summary["notes"] = [
                        {
                            "text": note["text"][:500],
                            **(
                                {"added_at": note["added_at"][:80]}
                                if isinstance(note.get("added_at"), str)
                                else {}
                            ),
                        }
                        for note in notes[-5:]
                        if isinstance(note, dict)
                        and isinstance(note.get("text"), str)
                    ]
                # Inject rejected finding titles from the prior dedup-deadlock window
                # so the agent does not re-propose them on recovery.
                _dft = d.get("deadlock_filtered_titles")
                if _dft:
                    summary["deadlock_filtered_titles"] = _dft
                # Current direction: last 3 cycles with numeric fields only
                compact_history = []
                for entry in raw_history[-3:]:
                    compact_entry = {
                        k: entry[k] for k in _HISTORY_SUMMARY_FIELDS if k in entry
                    }
                    finding_titles = compact_entry.get("finding_titles")
                    if isinstance(finding_titles, list):
                        compact_entry["finding_titles"] = [
                            title[:200]
                            for title in finding_titles[-20:]
                            if isinstance(title, str) and title
                        ]
                    compact_history.append(compact_entry)
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

        # Design doc summary — just keys and playbook keys index, not full content.
        # Agent can fetch specific sections via ./design-doc get <key>.
        doc = project.design_doc or {}
        design_doc_summary: dict = {
            "_keys": [
                k
                for k in doc
                if not k.startswith("_") and not k.startswith("playbook/")
            ],
        }
        # Include playbook entry names only — verbatim content already ships via
        # task.playbook (capped, relevance-ranked selection). Embedding every entry
        # in full here doubles the payload and is re-read on every agentic turn;
        # the agent can fetch on demand with ./playbook get <key>.
        # Skip pending_source entries — those are mid-flight writes whose
        # source task hasn't completed yet. See main.py update_playbook for
        # the anti-poisoning rationale.
        from app.playbook_storage import visible_playbook_entry

        playbook_keys = [
            k[len("playbook/"):]
            for k, v in doc.items()
            if k.startswith("playbook/") and visible_playbook_entry(v) is not None
        ]
        if playbook_keys:
            design_doc_summary["_playbook_keys"] = playbook_keys

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
            present, missing, indeterminate = await classify_paths_tristate(
                modules, **verification_kwargs
            )
            if not missing and not indeterminate:
                status = "verified"
            elif not missing:
                # Nothing is definitely absent but some paths were unknowable
                # (rate limit / quota reserve / API error) — no verdict either
                # way. Never "verified" (can't confirm) and never an absence.
                status = "unverified"
            elif present or indeterminate:
                status = "partial"
            else:
                # Every declared module is unambiguously absent from the
                # integration branch: the system is claimed but its backing
                # code does not exist.
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
            system_verification[dkey[len("systems/") :]] = {
                "status": status,
                "claimed_status": str(dval.get("status", "")),
                "present": present,
                "missing": missing,
                **({"indeterminate": indeterminate} if indeterminate else {}),
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
            present, missing, indeterminate = await classify_paths_tristate(
                cap_paths, **verification_kwargs
            )
            if not missing and not indeterminate:
                status = "verified"
            elif not missing:
                # Unknowable paths only (see systems loop): no verdict.
                status = "unverified"
            elif present or indeterminate:
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
            capability_verification[dkey[len("capabilities/") :]] = {
                "status": status,
                "claimed_status": str(dval.get("status", "")),
                "present": present,
                "missing": missing,
                **({"indeterminate": indeterminate} if indeterminate else {}),
                **({"absent_symbols": absent_symbols} if absent_symbols else {}),
            }

        # Stamp verification provenance — when and against which branch tip
        # the verdicts were computed.  The agent surfaces these so an old stamp
        # (from a task spawned hours or days ago and now being retried) is
        # visibly discountable.  Resolved once; shared by both maps.
        _v_computed_at = _utcnow().isoformat()
        _v_branch_sha = await resolve_head_commit(**verification_kwargs)
        if system_verification:
            system_verification["_computed_at"] = _v_computed_at
            system_verification["_branch_sha"] = _v_branch_sha
        if capability_verification:
            capability_verification["_computed_at"] = _v_computed_at
            capability_verification["_branch_sha"] = _v_branch_sha

        research_payload: dict = {
            "type": "research",
            "directions": direction_summaries,
            "current_direction_id": None
            if chosen_direction is None
            else chosen_direction.get("id"),
            "design_doc": design_doc_summary,
            "base_branch": base_branch,
            **({"prompt_suffix": research_suffix} if research_suffix else {}),
        }
        if system_verification:
            research_payload["system_verification"] = system_verification
        if capability_verification:
            research_payload["capability_verification"] = capability_verification
        # Surface the strategist-curated capability vocabulary (canonical names +
        # statuses) so the research agent declares prerequisite / provides_capability
        # refs from the SAME shared namespace consumers draw from. Additive and
        # optional: omitted on the empty path, older agents ignore it and still
        # validate.
        _available_capabilities = _annotate_capability_proof_status(
            registry_capability_vocabulary(project.problem_model),
            getattr(project, "capability_proof_records", None),
            registry_capability_evidence_refs_map(project.problem_model),
        )
        if _available_capabilities:
            research_payload["available_capabilities"] = _available_capabilities
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
            from app.project_telemetry import get_project_telemetry

            # Hydrate from telemetry — strip_migrated_doc_keys removes this
            # key from project.design_doc at write time.
            _telemetry_dfa = await get_project_telemetry(
                session, project.id, "direction_first_attempt_feedback",
                design_doc_fallback=project.design_doc,
            )
            _hydrated_doc = dict(project.design_doc or {})
            if _telemetry_dfa is not None:
                _hydrated_doc["direction_first_attempt_feedback"] = _telemetry_dfa

            _dir_id = chosen_direction.get("id")
            _coaching = (
                _get_failure_coaching_suffix(str(_dir_id), _hydrated_doc)
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
                relevant: dict = {"system_id": dir_system_id}
                sys_page = doc.get(f"systems/{dir_system_id}")
                if isinstance(sys_page, dict):
                    relevant["system"] = sys_page
                related_caps = {
                    k[len("capabilities/") :]: v
                    for k, v in doc.items()
                    if k.startswith("capabilities/")
                    and isinstance(v, dict)
                    and v.get("status") in ("missing", "partial")
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
        # Surface aggregated missing-foundation demand so the research agent can
        # propose/create the absent foundation capability or recommend retiring the
        # stranded demand. Advisory + additive: read-only, fail-open, injected only
        # when demand is non-empty, and it does NOT bypass the spawn dedup/cooldown/
        # circuit-breaker guards that already ran above.
        _missing_demand = await fetch_missing_demand_for_spawn(session, project.id)
        if _missing_demand.get("demands"):
            research_payload["missing_capability_demand"] = _missing_demand
            _demand_hint = missing_demand_prompt_hint(
                _missing_demand,
                registry_names=registry_capability_names(project.problem_model),
            )
            if _demand_hint:
                append_prompt_suffix(research_payload, _demand_hint)

            # On-demand earn: attempt to prove demanded unproven capabilities
            # that declare backing_paths by verifying each path at HEAD.  This
            # commissions verification work at demand-detection time (not just
            # on the reaper's hourly cadence) so proof can be earned before a
            # dependent needs it.  Fail-open: any error is logged and swallowed.
            try:
                _pm = (
                    project.problem_model
                    if isinstance(project.problem_model, dict)
                    else {}
                )
                _earn_result = await commission_proof_earn_loop(
                    project, _missing_demand, _pm, session=session
                )
                if _earn_result.get("earned", 0) > 0:
                    logger.info(
                        "[AUTONOMOUS] earned proof for %d demanded unproven "
                        "capability(ies) via on-demand earn loop",
                        _earn_result["earned"],
                    )
            except Exception:  # noqa: BLE001 — earn loop is advisory; must not crash spawn
                logger.warning(
                    "[AUTONOMOUS] on-demand earn loop failed for project %s:",
                    project.id,
                    exc_info=True,
                )
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
        _compute_sweep_groups = await build_compute_sweep_groups(
            session, project.id,
            **({"direction_id": current_dir_id} if current_dir_id else {}),
        )
        if _compute_sweep_groups:
            research_payload["compute_experiment_sweeps"] = _compute_sweep_groups
        research_payload = validate_internal_payload(
            research_payload, "autonomous_research:research"
        )

        # Recover deferred findings from recent completed research tasks.
        task_context: dict = {}
        if resolve_dialect_name(session) == "postgresql":
            deferred_findings_filters = [
                Task.context.has_key("deferred_findings"),
                case(
                    (
                        func.jsonb_typeof(Task.context["deferred_findings"]) == "array",
                        func.jsonb_array_length(Task.context["deferred_findings"]),
                    ),
                    else_=0,
                )
                > 0,
            ]
        else:
            deferred_findings_filters = [
                func.json_type(Task.context, "$.deferred_findings") == "array",
                func.json_array_length(Task.context, "$.deferred_findings") > 0,
            ]
        if current_dir_id:
            if resolve_dialect_name(session) == "postgresql":
                deferred_findings_scope_rows = func.jsonb_array_elements(
                    Task.context["deferred_findings"]
                ).table_valued("value")
                _explicit_scope_match = exists(
                    select(1)
                    .select_from(deferred_findings_scope_rows)
                    .where(
                        func.jsonb_extract_path_text(
                            deferred_findings_scope_rows.c.value,
                            "direction_id",
                        )
                        == current_dir_id
                    )
                )
                _legacy_blank_scope_match = exists(
                    select(1)
                    .select_from(deferred_findings_scope_rows)
                    .where(
                        func.coalesce(
                            func.jsonb_extract_path_text(
                                deferred_findings_scope_rows.c.value,
                                "direction_id",
                            ),
                            "",
                        )
                        == ""
                    )
                )
                _producer_scope = func.coalesce(
                    func.jsonb_extract_path_text(Task.context, "direction_id"),
                    "",
                )
            else:
                deferred_findings_scope_rows = func.json_each(
                    Task.context, "$.deferred_findings"
                ).table_valued("key", "value")
                _explicit_scope_match = exists(
                    select(1)
                    .select_from(deferred_findings_scope_rows)
                    .where(
                        func.coalesce(
                            func.json_extract(
                                deferred_findings_scope_rows.c.value,
                                "$.direction_id",
                            ),
                            "",
                        )
                        == current_dir_id
                    )
                )
                _legacy_blank_scope_match = exists(
                    select(1)
                    .select_from(deferred_findings_scope_rows)
                    .where(
                        func.coalesce(
                            func.json_extract(
                                deferred_findings_scope_rows.c.value,
                                "$.direction_id",
                            ),
                            "",
                        )
                        == ""
                    )
                )
                _producer_scope = func.coalesce(
                    func.json_extract(Task.context, "$.direction_id"), ""
                )
            # Legacy research parents stored the owning direction on the
            # producer context while leaving each deferred finding unscoped.
            # Admit those rows so the server-owned fallback in
            # unacknowledged_deferred_findings() can stamp the direction, but
            # never use that fallback to override an explicit other direction.
            deferred_findings_scope_filter = or_(
                _explicit_scope_match,
                and_(
                    _producer_scope == current_dir_id,
                    _legacy_blank_scope_match,
                ),
            )
        else:
            if resolve_dialect_name(session) == "postgresql":
                deferred_findings_scope_rows = func.jsonb_array_elements(
                    Task.context["deferred_findings"]
                ).table_valued("value")
                deferred_findings_scope_filter = exists(
                    select(1)
                    .select_from(deferred_findings_scope_rows)
                    .where(
                        func.coalesce(
                            func.jsonb_extract_path_text(
                                deferred_findings_scope_rows.c.value,
                                "direction_id",
                            ),
                            "",
                        )
                        == ""
                    )
                )
            else:
                deferred_findings_scope_rows = func.json_each(
                    Task.context, "$.deferred_findings"
                ).table_valued("key", "value")
                deferred_findings_scope_filter = exists(
                    select(1)
                    .select_from(deferred_findings_scope_rows)
                    .where(
                        func.coalesce(
                            func.json_extract(
                                deferred_findings_scope_rows.c.value,
                                "$.direction_id",
                            ),
                            "",
                        )
                        == ""
                    )
                )
        prev_research_rows = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.project_id == project.id,
                        Task.status == TaskStatus.COMPLETED,
                        Task.payloads[0]["type"].as_string() == "research",
                        *deferred_findings_filters,
                        deferred_findings_scope_filter,
                    )
                    .order_by(Task.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        _deferred_ttl_days = int(_runtime_config.get("deferred_finding_ttl_days", 7))
        _ttl_cutoff = _utcnow() - timedelta(days=_deferred_ttl_days)
        dir_deferred: list[dict] = []
        seen_hashes: set[str] = set()
        _expired_count = 0
        # Expired findings grouped by the producing COMPLETED task that shelved
        # them (``context.deferred_findings``). Each affected producer gets a
        # retired-disposition record so a TTL drop is never only a logged count.
        _expired_by_producer: dict[uuid.UUID, list[dict]] = {}
        for prev_row in prev_research_rows:
            for f in unacknowledged_deferred_findings(prev_row):
                f_dir_id = f.get("direction_id")
                if current_dir_id:
                    if f_dir_id != current_dir_id:
                        continue
                else:
                    if f_dir_id:
                        continue
                # Expire stale findings (originally researched too long ago)
                _orig_at = f.get("originally_researched_at")
                if _orig_at:
                    try:
                        _orig_dt = datetime.fromisoformat(_orig_at).replace(tzinfo=None)
                        if _orig_dt < _ttl_cutoff:
                            _expired_count += 1
                            _expired_by_producer.setdefault(prev_row.id, []).append(f)
                            continue
                    except (ValueError, TypeError):
                        pass
                # Stored ``finding_hash`` is routing lineage and is not a trusted
                # carry identity.  Older deferred rows may omit it entirely.
                fhash = canonical_deferred_finding_hash(f)
                if fhash not in seen_hashes:
                    dir_deferred.append(f)
                    seen_hashes.add(fhash)
        # Record a durable retired disposition on each affected producing task.
        # Fail-open: a recording error must never abort the research spawn.
        for prev_row in prev_research_rows:
            _expired = _expired_by_producer.get(prev_row.id)
            if not _expired:
                continue
            try:
                _prod_ctx = dict(prev_row.context or {})
                # Retirement identity is server-derived. ``finding_hash`` is
                # legacy routing lineage and may be missing or agent-authored,
                # so never let it alias an unrelated disposition.
                _expired_for_retirement = [
                    {
                        **finding,
                        "finding_hash": canonical_deferred_finding_hash(finding),
                    }
                    for finding in _expired
                ]
                _prod_ctx[RETIRED_DISPOSITION_KEY] = (
                    build_retired_disposition_records(
                        _expired_for_retirement,
                        existing_records=_prod_ctx.get(RETIRED_DISPOSITION_KEY),
                    )
                )
                # Once retirement is durable, remove the matching transport
                # entries from this immediate producer. Otherwise a producer
                # containing only expired work keeps matching the query above
                # forever and can occupy one of its 20 bounded slots on every
                # cycle, starving older producers that still hold live carry.
                _producer_direction_id = str(
                    _prod_ctx.get("direction_id") or ""
                ).strip()

                def _retirement_transport_identity(
                    finding: dict,
                    producer_direction_id: str = _producer_direction_id,
                ) -> tuple[str, str, str]:
                    return (
                        canonical_deferred_finding_hash(finding),
                        str(
                            finding.get("direction_id") or producer_direction_id
                        ).strip(),
                        str(finding.get("originally_researched_at") or "").strip(),
                    )

                _expired_identities = {
                    _retirement_transport_identity(finding)
                    for finding in _expired
                }
                _raw_producer_deferred = _prod_ctx.get("deferred_findings", [])
                if isinstance(_raw_producer_deferred, list):
                    _prod_ctx["deferred_findings"] = [
                        finding
                        for finding in _raw_producer_deferred
                        if not (
                            isinstance(finding, dict)
                            and _retirement_transport_identity(finding)
                            in _expired_identities
                        )
                    ]
                prev_row.context = _prod_ctx
                flag_modified(prev_row, "context")
                session.add(prev_row)
                logger.warning(
                    f"[AUTONOMOUS] Recorded {len(_expired)} retired disposition(s) "
                    f"on producer task {prev_row.id} (TTL {_deferred_ttl_days}d)"
                )
            except Exception:  # noqa: BLE001 — disposition logging is best-effort
                logger.warning(
                    "[AUTONOMOUS] failed to record retired disposition(s) on "
                    "producer task %s — disposition logging is best-effort",
                    prev_row.id,
                    exc_info=True,
                )
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

        await acquire_direction_research_spawn_lock(session, project.id, current_dir_id)
        if current_dir_id is not None:
            locked_direction_research = [
                task
                for task in (
                    (
                        await session.execute(
                            select(Task).where(
                                Task.project_id == project.id,
                                Task.status.in_(active_research_statuses),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if task.first_payload.get("type") == "research"
                and _research_payload_direction_id(task.first_payload) == current_dir_id
            ]
            is_pipelined_spawn = current_dir_id in pipelined_direction_ids
            if is_pipelined_spawn:
                if len(locked_direction_research) != 1:
                    logger.info(
                        "[AUTONOMOUS] Skipping pipelined research for project %s "
                        "direction %s under lock — expected one stuck parent, found %s active task(s)",
                        project.id,
                        current_dir_id,
                        len(locked_direction_research),
                    )
                    continue
                locked_parent = locked_direction_research[0]
                if locked_parent.status != TaskStatus.EVALUATING:
                    logger.info(
                        "[AUTONOMOUS] Skipping pipelined research for project %s "
                        "direction %s under lock — active holder is %s, not stale EVALUATING",
                        project.id,
                        current_dir_id,
                        locked_parent.status,
                    )
                    continue
                locked_updated = locked_parent.updated_at
                if locked_updated is not None and locked_updated.tzinfo is not None:
                    locked_updated = locked_updated.replace(tzinfo=None)
                if (
                    locked_updated is None
                    or ((now - locked_updated).total_seconds() / 60)
                    < evaluating_timeout_minutes
                ):
                    logger.info(
                        "[AUTONOMOUS] Skipping pipelined research for project %s "
                        "direction %s under lock — parent is no longer stale enough",
                        project.id,
                        current_dir_id,
                    )
                    continue
                locked_parent_has_active_children = (
                    await session.execute(
                        select(Task.id).where(
                            Task.parent_task_id == locked_parent.id,
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
                ).scalars().first() is not None
                locked_parent_has_open_pr = bool(
                    ((locked_parent.context or {}).get("review_loop") or {}).get(
                        "pr_number"
                    )
                )
                if locked_parent_has_active_children or locked_parent_has_open_pr:
                    logger.info(
                        "[AUTONOMOUS] Skipping pipelined research for project %s "
                        "direction %s under lock — parent resumed progress",
                        project.id,
                        current_dir_id,
                    )
                    continue
            elif locked_direction_research:
                logger.info(
                    "[AUTONOMOUS] Skipping research for project %s direction %s "
                    "under lock — a concurrent task already holds the direction",
                    project.id,
                    current_dir_id,
                )
                continue

        from app.discovery_admission import (
            DiscoveryCycleUnavailable,
            ensure_discovery_cycle_for_research,
        )

        _discovery_cycle = None
        if current_dir_id:
            try:
                _discovery_cycle = await ensure_discovery_cycle_for_research(
                    session,
                    project=project,
                    direction_key=current_dir_id,
                    direction=chosen_direction or {"id": current_dir_id},
                    runtime_config=_runtime_config,
                    candidate_sha=_v_branch_sha,
                )
            except DiscoveryCycleUnavailable as exc:
                logger.info(
                    "[AUTONOMOUS] Skipping discovery-root spawn for project %s "
                    "direction %s: %s",
                    project.id,
                    current_dir_id,
                    exc,
                )
                continue
            _discovery_cycle_id = (
                _discovery_cycle.id if _discovery_cycle is not None else None
            )
            if _discovery_cycle is not None:
                research_payload.update(
                    {
                        "discovery_generation": _discovery_cycle.generation,
                        "discovery_phase": _discovery_cycle.phase,
                        "discovery_intent_snapshot": dict(
                            _discovery_cycle.intent_snapshot or {}
                        ),
                        "discovery_bet_snapshot": dict(
                            _discovery_cycle.bet_snapshot or {}
                        ),
                    }
                )
                research_payload = validate_internal_payload(
                    research_payload,
                    "autonomous_research:discovery_cycle",
                )
        else:
            _discovery_cycle_id = None
        # Stamp the maximum implementation set this cycle can admit. This is a
        # capacity ceiling, not a finding-count target: the research parent still
        # owns the smallest coherent execution graph justified by the evidence,
        # but must not truncate that graph to a historical 1-3 expectation when
        # more slots are genuinely available. Deferred task-spawning findings
        # consume the same ceiling; carried notes/direction/design metadata do
        # not, because processing them creates no task and cannot consume an
        # admission slot. Additive and optional: validated via BasePayload
        # extra='allow' and ignored by older agents.
        _capacity_consuming_deferred = sum(
            1
            for finding in dir_deferred
            if str(finding.get("type") or "").strip().lower()
            not in NON_SPAWNING_FINDING_TYPES
        )
        research_payload["work_headroom"] = effective_research_finding_headroom(
            _capacity_consuming_deferred
        )
        new_task = Task(
            project_id=project.id,
            discovery_cycle_id=_discovery_cycle_id,
            title=f"Research: {direction_text}",
            description=f"Autonomous research for: {direction_text}",
            payloads=[research_payload],
            context=task_context,
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            origination=TASK_ORIGINATION_RESEARCH,
            priority=direction_task_priority(chosen_direction),
            # Atomic per-direction claim (ux_task_active_idempotency_key):
            # see research_direction_claim_key.
            idempotency_key=research_direction_claim_key(
                project.id,
                current_dir_id,
                pipelined=current_dir_id in pipelined_direction_ids,
            ),
        )
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
        # Insert under a SAVEPOINT so losing the per-direction claim race only
        # skips THIS direction (and its history row) instead of aborting the
        # whole batch/transaction. The flush before the savepoint pushes any
        # earlier staged state (e.g. anomaly Alerts) into the outer
        # transaction so a claim-race rollback can never drop it.
        await session.flush()
        try:
            async with session.begin_nested():
                session.add(new_task)
                session.add(history)
                await session.flush()
                if _discovery_cycle_id is not None:
                    if (
                        _discovery_cycle is not None
                        and _discovery_cycle.source_research_task_id is None
                    ):
                        _discovery_cycle.source_research_task_id = new_task.id
                        session.add(_discovery_cycle)
        except IntegrityError as exc:
            if not _is_research_claim_integrity_error(exc):
                raise
            # A sibling tick (another orchestrator instance, or the manual
            # research endpoint) committed a research task for this direction
            # between our no-active-research read and this insert — exactly
            # the TOCTOU window behind the 2026-06-06 duplicate twins
            # (6ca2f9cf / fcedb840, 13.7s apart). The committed sibling IS
            # the research cycle we wanted; skip, don't retry.
            logger.warning(
                "[AUTONOMOUS] Lost the research spawn race for project %s "
                "direction %s — a concurrent spawn already claimed it (%s)",
                project.id,
                current_dir_id or "undirected",
                exc.orig,
            )
            if (
                _discovery_cycle is not None
                and _discovery_cycle.source_research_task_id is None
            ):
                _discovery_cycle.state = "cancelled"
                session.add(_discovery_cycle)
            continue
        spawned_tasks.append(new_task)

    # Onboarding-proof: record the first non-bootstrap autonomous task spawn for
    # this project. Best-effort and idempotent — only the first spawn stamps —
    # and it must never gate the spawn commit, so it is wrapped defensively.
    if spawned_tasks:
        try:
            from app.onboarding_proof import stamp_first_task_spawned

            stamp_first_task_spawned(project, session, when=_utcnow())
        except Exception:  # noqa: BLE001 — observability stamp never blocks spawn
            logger.warning(
                "[AUTONOMOUS] onboarding first-task-spawned stamp failed",
                exc_info=True,
            )

    # Apply the in-memory policy change and stage its durable alert, then commit
    # the whole mutation together so no broadcast or external notification is
    # scheduled until the transaction has persisted.
    await lock_and_validate_legacy_authority(
        session,
        project.id,
        expected_generation=legacy_generation,
    )
    if breaker["state"] == "HALF_OPEN" and spawned_tasks:
        breaker["half_open_task_id"] = str(spawned_tasks[0].id)
    anomaly_action = await check_anomaly_actions()
    _stage_anomaly_action_alert(session, project.id, anomaly_action)
    await commit_legacy_authorized(session, project.id, legacy_generation)
    await _publish_anomaly_action(project.id, anomaly_action)

    for t in spawned_tasks:
        logger.info(
            f"[AUTONOMOUS] Spawned research task {t.id} for project {project.id}"
        )
    if len(spawned_tasks) > 1:
        logger.info(
            f"[AUTONOMOUS] Spawned {len(spawned_tasks)} parallel research tasks for project {project.id}"
        )
    return len(spawned_tasks)


async def _spawn_periodic_task(
    project: Project,
    task_type: str,
    session: AsyncSession,
    *,
    expected_legacy_generation: int | None = None,
):
    """Spawn a periodic non-research task (qa, security_audit, deps) if its schedule is due."""
    from sqlalchemy import func

    # Guard: task_type must be a known type
    if task_type not in ALL_TASK_TYPES:
        logger.warning(
            f"[AUTONOMOUS] Unknown task_type '{task_type}' in _spawn_periodic_task, skipping"
        )
        return
    spend_defer_reason = await _project_spend_ceiling_spawn_defer_reason(
        project,
        session,
        expected_legacy_generation=expected_legacy_generation,
    )
    if spend_defer_reason:
        logger.info(
            "[AUTONOMOUS] Skipping periodic task spawn (type=%s) for project %s — %s",
            task_type,
            project.id,
            spend_defer_reason,
        )
        return

    # Guard: circuit breaker must allow spawning
    breaker = _shared_state.get_circuit_breaker(str(project.id))
    if await _advance_circuit_breaker_and_check(
        project,
        session,
        breaker,
        f"periodic task spawn (type={task_type})",
        expected_legacy_generation=expected_legacy_generation,
    ):
        return

    # The circuit-breaker path above may commit policy transitions.  Acquire
    # the universal Project pause/archive authority after it and before the
    # first Task child query so the schedule check -> INSERT window is fenced.
    admission_project_id = project.id
    admission = await acquire_automatic_task_creation_authority(
        session, admission_project_id, skip_locked=True
    )
    if admission != AUTOMATIC_TASK_CREATION_ALLOWED:
        await session.rollback()
        logger.info(
            "[AUTONOMOUS] Periodic admission closed for project %s type=%s (%s)",
            admission_project_id,
            task_type,
            admission,
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
    budget_snapshot = None
    try:
        budget_snapshot = await compute_autonomous_spawn_budget_snapshot(
            session,
            runtime_config=_runtime_config,
            now=one_hour_ago + timedelta(hours=1),
        )
        logger.info(
            "[AUTONOMOUS] Spawn budget snapshot: %s",
            budget_snapshot.as_log_fields(),
        )
    except Exception as exc:
        # A persistent snapshot/DB failure must NOT silently disable an
        # operator-enabled 'enforce' budget (that would be the exact opposite
        # of enforcement). The configured mode comes from runtime_config and
        # needs no DB, so we can still read it here. Fail CLOSED under enforce
        # (block the spawn) and preserve fail-open for the observational
        # off/observe modes where the guard is only advisory.
        try:
            mode = autonomous_spawn_budget_config_from_runtime(_runtime_config).mode
        except Exception:  # noqa: BLE001 - malformed config; stay observational
            mode = DEFAULT_AUTONOMOUS_SPAWN_BUDGET_MODE
        if mode == "enforce":
            logger.exception(
                "[AUTONOMOUS] Spawn budget snapshot failed in enforce mode; "
                "failing CLOSED (blocking periodic task spawn type=%s)",
                task_type,
            )
            return
        logger.warning(
            "[AUTONOMOUS] Spawn budget calculation failed open (mode=%s): %s",
            mode,
            exc,
        )
    if budget_snapshot is not None:
        budget_config = autonomous_spawn_budget_config_from_runtime(_runtime_config)
        budget_guard = build_autonomous_spawn_budget_guard(
            snapshot=budget_snapshot,
            spawn_category=classify_autonomous_spawn_budget_category(
                task_type=task_type,
                config=budget_config,
            ),
            enforce_initiative=budget_snapshot.mode == "enforce",
        )
        if budget_guard is not None:
            logger.warning("[AUTONOMOUS] Spawn budget block: %s", budget_guard)
            return
    else:
        global_max = _runtime_config.get(
            "max_global_autonomous_tasks_per_hour",
            _runtime_config["max_autonomous_tasks_per_hour"] * 10,
        )
        global_recent = (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    autonomous_spawn_task_clause(),
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
    _periodic_per_hour_cap = project_parallelism_cap(
        project, "max_autonomous_tasks_per_hour", "max_autonomous_tasks_per_hour", 12
    )
    if recent_auto >= _periodic_per_hour_cap:
        logger.info(
            f"[AUTONOMOUS] Per-hour cap reached: {recent_auto}/{_periodic_per_hour_cap} "
            f"— skipping periodic spawn (type={task_type}) for project {project.id}"
        )
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
    if expected_legacy_generation is not None:
        await lock_and_validate_legacy_authority(
            session,
            project.id,
            expected_generation=expected_legacy_generation,
        )
    if breaker["state"] == "HALF_OPEN":
        breaker["half_open_task_id"] = str(new_task.id)
    if expected_legacy_generation is None:
        await session.commit()
    else:
        await commit_legacy_authorized(
            session, project.id, expected_legacy_generation
        )
    await session.refresh(new_task)

    logger.info(
        f"[AUTONOMOUS] Spawned periodic {task_type} task {new_task.id} for project {project.id}"
    )


async def _check_autonomous_research(owner: str, repo: str):
    """Post-merge hook: check if we should spawn a research task.

    Each matching project is spawned in its OWN short-lived session so a
    lock-timeout / deadlock / cancelled-mid-query error on one project rolls back
    only that project's transaction and the others still get a chance to spawn.
    The previous single shared session across the whole loop meant one DB error
    (deadlock, "connection is closed", or a PendingRollbackError from a
    rollback-expired lazy-load) poisoned the session and silently killed spawning
    for every project while churning the connection pool.
    """
    # Phase 1 — enumerate matching projects + the fleet-headroom budget in one
    # short read session, so no per-project spawn can hold this open. The
    # post-merge hook shares the same scaled-admission semantics as the idle
    # sweep: a budget of 0 (fleet below the min-free-slots floor) admits none.
    try:
        async with AsyncSessionLocal() as session:
            project_ids = (
                await session.execute(
                    select(Project.id).where(
                        Project.github_owner == owner,
                        Project.github_repo == repo,
                        Project.autonomous_enabled.is_(True),
                        # A paused project must not spawn new autonomous
                        # work even when autonomous_enabled is still True.
                        Project.paused.is_(False),
                        Project.not_archived(),
                    )
                )
            ).scalars().all()
            if not project_ids:
                return

            from app.fleet_utilization import (
                compute_headroom_spawn_budget,
                get_fleet_utilization_snapshot,
            )

            headroom_budget = 0
            try:
                snapshot = await get_fleet_utilization_snapshot(session)
                headroom = compute_headroom_spawn_budget(
                    snapshot,
                    min_free_slots=_runtime_config.get(
                        "research_headroom_min_free_slots", 2
                    ),
                    cap=_runtime_config.get("research_fleet_spawn_budget", 6),
                )
                headroom_budget = headroom["budget"]
                logger.info("[AUTONOMOUS] Post-merge research headroom: %s", headroom)
            except Exception as e:
                logger.warning(
                    f"[AUTONOMOUS] Fleet headroom snapshot failed open for "
                    f"post-merge research: {e}",
                    exc_info=True,
                )
                headroom_budget = _runtime_config.get(
                    "research_fleet_spawn_budget", 6
                )
    except Exception as e:
        logger.error(
            f"[AUTONOMOUS] Error enumerating projects for research trigger {owner}/{repo}: {e}",
            exc_info=True,
        )
        return

    if headroom_budget <= 0:
        return

    # Phase 2 — spawn each project in its own isolated session.  The Project
    # instance MUST be loaded by that same session: carrying ORM instances out
    # of a batch session leaves them detached, and persistence operations in the
    # spawn path (notably ``session.refresh(project)``) then raise
    # ``InvalidRequestError: Instance ... is not persistent within this
    # Session``.  Re-fetching by id also ensures pause/config changes made after
    # enumeration are observed before spawning.
    for project_id in project_ids:
        if headroom_budget <= 0:
            # Fleet headroom budget exhausted by earlier projects in this sweep —
            # stop admitting rather than hand a 0 budget to the spawner (which
            # would still take the legacy-authority lock for nothing).
            break
        try:
            async with AsyncSessionLocal() as session:
                project = await session.get(Project, project_id)
                if project is None:
                    continue
                spawned = await _spawn_research_task(
                    project,
                    session,
                    expected_legacy_generation=None,
                    headroom_budget=headroom_budget,
                )
                if isinstance(spawned, int) and spawned > 0:
                    headroom_budget -= spawned
        except Exception as e:
            logger.error(
                f"[AUTONOMOUS] Research trigger spawn failed for project {project_id} ({owner}/{repo}): {e}",
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Residual-fingerprint create-fix lane. Re-exported here so existing callers
# and tests that do ``from app.autonomous_research import ...`` keep working.
# The lane itself was re-homed to app.autonomous_research_residual when the
# self-repair apparatus (autonomous_research_investigations) was deleted; the
# residual create-fix lane is explicitly preserved.
# ---------------------------------------------------------------------------
from app.autonomous_research_residual import (
    _spawn_residual_fingerprint_remediation,
    _spawn_viewport_regression_remediation,
)


async def _run_idle_research_for_project(
    project: Project,
    session: AsyncSession,
    headroom_budget: int,
    *,
    expected_legacy_generation: int,
    fleet_has_capacity: bool = True,
) -> int:
    """Idle-research spawn work for a single project.

    ``headroom_budget`` is the *remaining* fleet-headroom-scaled RESEARCH
    admission budget for this sweep (0 = no research is admitted for this
    project — either the fleet has no headroom above the floor, or earlier
    projects in the sweep already consumed the budget). Returns the number of
    research tasks actually spawned so the sweep can decrement the shared
    budget across projects.

    ``fleet_has_capacity`` is the sweep-entry fleet-capacity verdict, computed
    ONCE per sweep and passed unchanged to every project. It gates the
    non-research periodic lane (security_audit / qa / deps). Keeping that lane
    off the *decrementing* research budget is deliberate: the budget scales
    research admission only, and folding the periodic lane into it would make
    project iteration order decide whether security audits run — later projects
    in the sweep would silently lose their periodic spawns once earlier ones
    consumed the research budget, even with the fleet still having capacity.
    Gating on the per-sweep verdict preserves the pre-headroom semantics, where
    a single fleet snapshot gated the periodic lane for every project alike.

    Runs inside a per-project session owned by ``_check_idle_research``; a
    failure here is contained to this project's transaction (see that function's
    note on why a shared session across the whole loop was structurally unsafe).
    """
    # Log pending proposals awaiting review
    pending_proposals = [
        d for d in (project.directions or []) if d.get("status") == "proposed"
    ]
    if pending_proposals:
        logger.info(
            f"[DIRECTIONS] Project {project.name} has {len(pending_proposals)} pending direction proposals awaiting review"
        )

    # There is deliberately NO direction-count gate here. The spawner owns
    # bootstrap, failure-breaker decay, the wall-clock backstop and bounded
    # starvation recovery, so every direction-less project must reach it; a
    # second failure ceiling at this level made those self-heals unreachable
    # for a halted, direction-less project. Do not reintroduce one — raise the
    # ceiling in `_spawn_research_task` instead, where the decision is bounded.

    # Monthly budget gate: skip idle-research spawning when the fleet-wide
    # monthly budget is exhausted. Since this runs for every project in the
    # idle-research sweep, avoid recomputing the budget per-project by
    # checking the shared-state flag the orchestrator sets on each cycle.
    if _shared_state.monthly_budget_gate_engaged:
        logger.info(
            "[AUTONOMOUS] Skipping idle research for project %s — monthly budget exhausted",
            project.id,
        )
        return 0

    # Daily budget gate: same shared-state fast path as the monthly gate.
    if _shared_state.daily_budget_gate_engaged:
        logger.info(
            "[AUTONOMOUS] Skipping idle research for project %s — daily budget exhausted",
            project.id,
        )
        return 0

    # Research admission is the only lane scaled by the decrementing budget.
    # A budget of 0 skips the spawner entirely rather than calling it with 0
    # (which would take the legacy-authority lock for nothing) — the same
    # early-out ``_check_autonomous_research`` gets from its ``break``.
    spawned = 0
    if headroom_budget > 0:
        spawned = await _spawn_research_task(
            project,
            session,
            expected_legacy_generation=expected_legacy_generation,
            headroom_budget=headroom_budget,
        )
        if not isinstance(spawned, int):
            spawned = 0

    # Periodic lane: gated on the per-sweep fleet-capacity verdict, NOT the
    # decrementing research budget, so a project's position in the sweep cannot
    # decide whether its security audit runs. Each `_spawn_periodic_task` still
    # applies its own cadence, dedup and spend-ceiling gates.
    if fleet_has_capacity:
        for task_type in ("security_audit", "qa", "deps"):
            await _spawn_periodic_task(
                project,
                task_type,
                session,
                expected_legacy_generation=expected_legacy_generation,
            )

    # Spawn the working residual-fingerprint create-fix lane for any
    # recurring failure pattern that warrants a targeted fix.
    # Guard with the same spend-ceiling check used by _spawn_research_task
    # and _spawn_periodic_task so an over-budget project cannot spawn new
    # work through this lane either.
    _residual_defer_reason = await _project_spend_ceiling_spawn_defer_reason(
        project,
        session,
        expected_legacy_generation=expected_legacy_generation,
    )
    if _residual_defer_reason:
        logger.info(
            "[AUTONOMOUS] Skipping residual remediation spawn for project %s — %s",
            project.id,
            _residual_defer_reason,
        )
    else:
        await _spawn_residual_fingerprint_remediation(
            project,
            session,
            expected_legacy_generation=expected_legacy_generation,
        )
        await _spawn_viewport_regression_remediation(
            project,
            session,
            expected_legacy_generation=expected_legacy_generation,
        )
    await commit_legacy_authorized(
        session, project.id, expected_legacy_generation
    )
    return spawned


async def _check_idle_research():
    """Periodic check: spawn research if projects are idle with active directions.

    Structure: enumerate candidate projects + the global VM-slot gate in one
    short read-only session, then spawn each project in its OWN session. A
    lock-timeout / deadlock / cancelled-mid-query error on one project rolls back
    only that project's transaction and the sweep continues. The previous single
    shared session across the whole loop meant one DB error (deadlock,
    "connection is closed", or a PendingRollbackError from a rollback-expired
    lazy-load) poisoned the session and silently killed spawning for EVERY
    project while churning the connection pool.
    """
    from app.anomaly_detection import _expire_stale_proposals

    now = _utcnow()
    interval_minutes = _runtime_config.get("autonomous_research_interval_minutes", 30)
    if _shared_state.last_idle_check and (
        now - _shared_state.last_idle_check
    ) < timedelta(minutes=interval_minutes):
        return
    _shared_state.last_idle_check = now

    # Phase 1 — enumerate candidates + the fleet-headroom budget in a short
    # session. Research admission is scaled to measured fleet headroom instead
    # of the old binary "any VM slot free?" gate: the budget is 0 when headroom
    # is below the min-free-slots floor, and scales up to the per-tick budget
    # as headroom grows.
    try:
        async with AsyncSessionLocal() as session:
            # Expire stale proposals before processing
            await _expire_stale_proposals(session)
            await session.commit()

            project_ids = (
                await session.execute(
                    select(Project.id).where(
                        Project.autonomous_enabled.is_(True),
                        # Skip paused projects: pause blocks ALL new work
                        # (research, periodic, and residual remediation)
                        # regardless of the autonomous_enabled toggle.
                        Project.paused.is_(False),
                        Project.not_archived(),
                    )
                )
            ).scalars().all()

            # Measure fleet headroom once per sweep. The snapshot's GCP/warm-pool
            # reads are internally exception-guarded; if the whole snapshot fails
            # we fail OPEN to the configured budget so a measurement outage does
            # not silently halt research admission.
            from app.fleet_utilization import (
                compute_headroom_spawn_budget,
                get_fleet_utilization_snapshot,
            )

            headroom_budget = 0
            try:
                snapshot = await get_fleet_utilization_snapshot(session)
                headroom = compute_headroom_spawn_budget(
                    snapshot,
                    min_free_slots=_runtime_config.get(
                        "research_headroom_min_free_slots", 2
                    ),
                    cap=_runtime_config.get("research_fleet_spawn_budget", 6),
                )
                headroom_budget = headroom["budget"]
                logger.info("[AUTONOMOUS] Idle research headroom: %s", headroom)
            except Exception as e:
                logger.warning(
                    f"[AUTONOMOUS] Fleet headroom snapshot failed open for idle "
                    f"research: {e}",
                    exc_info=True,
                )
                headroom_budget = _runtime_config.get(
                    "research_fleet_spawn_budget", 6
                )
    except Exception as e:
        logger.error(
            f"[AUTONOMOUS] Error enumerating projects for idle research: {e}",
            exc_info=True,
        )
        return

    # NOTE: deliberately NO sweep-level early return on `headroom_budget <= 0`.
    # A zero budget means only that *research* admission is closed for this
    # sweep; the per-project loop below also drives the residual-fingerprint and
    # viewport-regression remediation lanes (and the legacy-commit authorization
    # path), which are not fleet-budget-scaled and matter most precisely when
    # the fleet is busy with recurring failures. `_run_idle_research_for_project`
    # already handles `headroom_budget == 0` by skipping research admission.

    # Sweep-entry fleet-capacity verdict: computed ONCE and passed unchanged to
    # every project so the non-research periodic lane (security_audit / qa /
    # deps) is all-or-none across the sweep, exactly as it was when a single
    # `vm_consuming < max_concurrent_vms` snapshot gated it. Only research
    # admission consumes the decrementing budget below.
    fleet_has_capacity = headroom_budget > 0

    # Phase 2 — spawn each project in its own isolated session.  Load the
    # Project in the session that will run the spawn so it remains persistent
    # for every refresh/write in ``_run_idle_research_for_project``.  Passing a
    # Project loaded by a now-closed batch session is a detached-instance bug.
    # The fleet-headroom budget is shared across the sweep: each project
    # decrements it by the number of research tasks it actually spawned.  Note
    # this loop does NOT `break` when the budget hits 0 (unlike
    # `_check_autonomous_research`, whose only lane is research): later projects
    # still need their periodic and residual-remediation lanes, which are not
    # budget-scaled.  Research admission itself still early-outs per project on
    # `headroom_budget > 0`, so the two loops stop admitting research on the
    # same condition.
    for project_id in project_ids:
        try:
            async with AsyncSessionLocal() as session:
                project = await session.get(Project, project_id)
                if project is None:
                    continue
                spawned = await _run_idle_research_for_project(
                    project,
                    session,
                    headroom_budget,
                    expected_legacy_generation=None,
                    fleet_has_capacity=fleet_has_capacity,
                )
                if isinstance(spawned, int) and spawned > 0:
                    headroom_budget -= spawned
        except Exception as e:
            logger.error(
                f"[AUTONOMOUS] Idle research spawn failed for project {project_id}: {e}",
                exc_info=True,
            )


async def refresh_research_task_verification(
    task: "Task",
    project: "Project",
    session: "AsyncSession",
) -> None:
    """Re-run the verification loops and stamp fresh provenance onto the task's payload.

    Called by the reaper when retrying a research task so the agent never
    executes stale spawn-time verdicts.  Mutates ``task.payloads[0]`` in-place
    using ``flag_modified``; the caller's transaction owns the eventual commit.
    No-ops when the task has no research payload or when verification context
    cannot be resolved.
    """
    from sqlalchemy.orm.attributes import flag_modified
    from app.playbook_lifecycle import _extract_file_paths

    payloads = task.payloads
    if not payloads or not isinstance(payloads, list):
        return
    payload = payloads[0]
    if not isinstance(payload, dict) or payload.get("type") != "research":
        return

    doc = project.design_doc or {}
    verification_kwargs = await resolve_branch_verification_context(session, project)

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
        present, missing, indeterminate = await classify_paths_tristate(
            modules, **verification_kwargs
        )
        if not missing and not indeterminate:
            status = "verified"
        elif not missing:
            status = "unverified"
        elif present or indeterminate:
            status = "partial"
        else:
            status = "claimed_absent"
        absent_symbols = await detect_absent_symbols(
            dval.get("modules"),
            [dval.get("description"), dval.get("notes")],
            **verification_kwargs,
        )
        if absent_symbols and status != "claimed_absent":
            status = "claimed_absent"
        system_verification[dkey[len("systems/") :]] = {
            "status": status,
            "claimed_status": str(dval.get("status", "")),
            "present": present,
            "missing": missing,
            **({"indeterminate": indeterminate} if indeterminate else {}),
            **({"absent_symbols": absent_symbols} if absent_symbols else {}),
        }

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
            continue
        cap_paths = [m for m in (dval.get("modules") or []) if isinstance(m, str)]
        for _field in ("notes", "description"):
            _txt = dval.get(_field)
            if isinstance(_txt, str) and _txt:
                cap_paths.extend(_extract_file_paths(_txt))
        cap_paths = list(dict.fromkeys(cap_paths))
        if not cap_paths:
            continue
        present, missing, indeterminate = await classify_paths_tristate(
            cap_paths, **verification_kwargs
        )
        if not missing and not indeterminate:
            status = "verified"
        elif not missing:
            status = "unverified"
        elif present or indeterminate:
            status = "partial"
        else:
            status = "claimed_absent"
        absent_symbols = await detect_absent_symbols(
            dval.get("modules"),
            [dval.get("notes"), dval.get("description")],
            **verification_kwargs,
        )
        if absent_symbols and status != "claimed_absent":
            status = "claimed_absent"
        capability_verification[dkey[len("capabilities/") :]] = {
            "status": status,
            "claimed_status": str(dval.get("status", "")),
            "present": present,
            "missing": missing,
            **({"indeterminate": indeterminate} if indeterminate else {}),
            **({"absent_symbols": absent_symbols} if absent_symbols else {}),
        }

    _v_computed_at = _utcnow().isoformat()
    _v_branch_sha = await resolve_head_commit(**verification_kwargs)
    if system_verification:
        system_verification["_computed_at"] = _v_computed_at
        system_verification["_branch_sha"] = _v_branch_sha
        payload["system_verification"] = system_verification
    else:
        payload.pop("system_verification", None)

    if capability_verification:
        capability_verification["_computed_at"] = _v_computed_at
        capability_verification["_branch_sha"] = _v_branch_sha
        payload["capability_verification"] = capability_verification
    else:
        payload.pop("capability_verification", None)

    flag_modified(task, "payloads")
    logger.info(
        "[AUTONOMOUS] Refreshed verification provenance for task %s (branch_sha=%s)",
        task.id,
        _v_branch_sha,
    )
