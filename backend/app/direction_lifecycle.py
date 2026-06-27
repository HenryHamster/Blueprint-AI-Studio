"""Direction lifecycle management for autonomous research.

Handles dead direction pruning, auto-recovery from auto-pause, and active
direction filtering (cooldown enforcement, auto-pause, completion detection).
Extracted from autonomous_research.py to create headroom in that module.
"""

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Project, Task, TaskStatus
from app.payload_schemas import validate_internal_payload
from app.shared_state import (
    runtime_config as _runtime_config,
)
from app.direction_refractory import get_direction_refractory_info, resolve_direction_refractory_minutes
from app.strategic_completion import _is_code_failure_pause_reason
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

_MAX_AUTO_PAUSE_CYCLES = 3

# Zombie-direction auto-retire ladder (4th ladder, ref knowledge bf6d8788). A
# direction the health guardrail keeps churn/unproductive/saturated-pausing
# (Rules 1, 2, 5) accumulates churn_family_pause_count; once it has been paused
# this many times for a churn-family reason it is auto-retired, freeing the slot
# without strategist intervention. "Lifetime burn backstop:" is deliberately
# EXCLUDED — those pauses set needs_manual_intervention and opt out of auto-
# recovery, so the operator (not this ladder) must act on them.
_MAX_CHURN_PAUSE_CYCLES = 2
_ZOMBIE_FAMILY_PREFIXES = (
    "Unproductive:",
    "Churn guardrail:",
    "Saturated:",
)

# Remediation monitor window: if a merged remediation happened longer ago than this,
# a new failure is presumed unrelated and the stale window is cleared instead of
# triggering escalation.
_REMEDIATION_MONITOR_EXPIRY_DAYS = 7

# Max time an investigation task may hold diagnosis_in_progress before flags are considered stale.
# Investigations typically finish in minutes; 4h is a safety ceiling to recover from reaped/timed-out tasks.
_DIAGNOSIS_STALE_HOURS = 4

# Recent failed tasks to scan when computing the dominant error fingerprint
_FINGERPRINT_SAMPLE_SIZE = 10
# Min matching failures required for a fingerprint to count as "dominant" for the direction
_FINGERPRINT_MIN_RECURRENCE = 2
# Only consider failures from within this window so stale patterns don't dominate
# after the current failure mode has shifted. Falls back to the unfiltered query
# when fewer than _FINGERPRINT_MIN_RECURRENCE recent failures exist.
_FINGERPRINT_RECENCY_DAYS = 7

# Max retries for a BLOCKER escalation notification whose initial delivery failed.
# After this many failed retries, the pending flag is cleared and an ERROR is logged
# with structured fields so monitoring can catch a silently-stuck direction.
_MAX_ESCALATION_NOTIFICATION_RETRIES = 3

# Minimum fractional drop in CODE_ERROR failure rate (pre- vs post-remediation)
# that counts as partial success rather than a failed fix. 0.5 == 50% reduction.
_PARTIAL_SUCCESS_RATE_DROP = 0.5

# TTL fallback for clearing needs_manual_intervention when no remediation monitor
# ever started. The monitor only starts on a successful remediation merge; if
# remediation keeps failing, the flag (and its operator alert) would otherwise
# persist indefinitely.
_STUCK_INTERVENTION_TTL_HOURS = 72

_OUTCOME_DETECTION_KEYS = (
    "outcome_progress_history",
    "outcome_progress_ever_met",
    "evidence_progress",
    "possibly_complete",
    "completion_suggestion",
    "outcome_scaffolding_flagged",
    "outcome_scaffolding_flagged_at",
    "outcome_scaffolding_flagged_cycles",
)


def _reset_outcome_detection_state(d: dict) -> None:
    """Clear per-direction outcome-detection state after a contract field changes.

    Called whenever text, paths, completion_criteria, success_evidence, or
    retirement_condition is rewritten so the next research cycle measures
    against the new contract rather than poisoned snapshots from the old one.
    """
    d["outcome_progress_history"] = []
    for key in _OUTCOME_DETECTION_KEYS[1:]:
        d.pop(key, None)


def _detect_partial_remediation_success(direction: dict) -> bool | None:
    """Return True if post-remediation code-failure rate is materially lower than pre-remediation.

    Returns None when research_history lacks entries on either side of
    ``last_remediation_at`` — the data is insufficient to decide, which
    the caller must distinguish from a confirmed failure (False) so a
    newly-created direction or one whose cycles haven't completed yet is
    not prematurely escalated to manual intervention.
    """
    last_rem_at = direction.get("last_remediation_at")
    if not last_rem_at:
        # No remediation recorded — nothing to evaluate; proceed to escalation.
        return False
    try:
        boundary = datetime.fromisoformat(last_rem_at)
    except (ValueError, TypeError):
        return False
    pre: list[dict] = []
    post: list[dict] = []
    for entry in direction.get("research_history") or []:
        ts = entry.get("timestamp")
        if not ts:
            continue
        try:
            entry_at = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        (post if entry_at >= boundary else pre).append(entry)
    if not pre or not post:
        return None

    def _code_failure_rate(entries: list[dict]) -> float:
        created = sum(e.get("tasks_created", 0) for e in entries)
        if created <= 0:
            return 0.0
        failed = sum(e.get("tasks_failed", 0) for e in entries)
        infra = sum(e.get("tasks_failed_infra", 0) for e in entries)
        return max(0, failed - infra) / created

    pre_rate = _code_failure_rate(pre[-3:])
    post_rate = _code_failure_rate(post)
    if pre_rate <= 0:
        return None
    return post_rate <= pre_rate * (1.0 - _PARTIAL_SUCCESS_RATE_DROP)


def _build_escalation_args(direction: dict) -> dict:
    """Assemble kwargs for post_remediation_escalation_message from direction state.

    Shared by the initial auto-pause escalation path and the retry loop so both
    produce identical message bodies when re-derived from the direction.
    """
    from app.strategic_completion import _build_direction_failure_summary

    summary = _build_direction_failure_summary(direction)
    rem_summary = (
        f"cycles_analyzed={summary['cycles_analyzed']} "
        f"tasks_created={summary['tasks_created']} "
        f"code_failed={summary['tasks_failed_code']} "
        f"merged={summary['prs_merged']} "
        f"code_failure_rate={summary['code_failure_rate']}"
    )
    did = direction.get("id")
    return {
        "direction_text": direction.get("text") or (str(did) if did else ""),
        "failure_pattern": (
            direction.get("error_fingerprint")
            or direction.get("paused_reason")
            or "(unknown)"
        ),
        "attempts": int(direction.get("remediation_attempts", 0)),
        "remediation_summary": rem_summary,
    }


async def _compute_direction_error_fingerprint(
    project_id: uuid.UUID,
    direction_id: str,
    session: AsyncSession,
) -> str | None:
    """Return the dominant error fingerprint across this direction's recent failed tasks.

    Pulls the most recent ``_FINGERPRINT_SAMPLE_SIZE`` failures for ``direction_id``
    within the last ``_FINGERPRINT_RECENCY_DAYS`` days and uses
    ``count_matching_fingerprints`` to find the fingerprint that recurs most,
    returning it only if it matches at least ``_FINGERPRINT_MIN_RECURRENCE``
    failures. Falls back to the unfiltered query when too few failures exist
    inside the window so low-volume directions still get a fingerprint. The
    result is stored on the direction so operators see the specific error
    pattern and the escalation path can compare it against the last
    remediation's fingerprint.
    """
    from app.anomaly_detection import (
        _task_error_fingerprint,
        count_matching_fingerprints,
    )

    base_query = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.FAILED,
            Task.context["direction_id"].as_string() == direction_id,
        )
        .order_by(Task.created_at.desc())
        .limit(_FINGERPRINT_SAMPLE_SIZE)
    )
    window_start = _utcnow() - timedelta(days=_FINGERPRINT_RECENCY_DAYS)
    failed_tasks = (
        (await session.execute(base_query.where(Task.created_at >= window_start)))
        .scalars()
        .all()
    )
    if len(failed_tasks) < _FINGERPRINT_MIN_RECURRENCE:
        failed_tasks = (await session.execute(base_query)).scalars().all()
    best_fp: str | None = None
    best_count = 0
    seen: set[str] = set()
    for t in failed_tasks:
        fp = _task_error_fingerprint(t)
        if not fp or fp in seen:
            continue
        seen.add(fp)
        count = count_matching_fingerprints(fp, failed_tasks)
        if count > best_count:
            best_fp, best_count = fp, count
    return best_fp if best_count >= _FINGERPRINT_MIN_RECURRENCE else None


def _synthesize_outcome_scaffolding_memo(
    direction_id: str,
    direction_topic: str,
    scaffolding_ev: dict,
) -> dict:
    """Build a memo dict from scaffolding evidence available at spawn time.

    All fields come from structured evidence the backend already holds,
    so no model call is needed — the result is substantive and citable.
    """
    total_cycles = scaffolding_ev.get("total_cycles", 0)
    lifetime_merged = scaffolding_ev.get("lifetime_merged", 0)
    merge_rate = scaffolding_ev.get("lifetime_merge_rate", 0.0)
    stall_cycles = scaffolding_ev.get("stall_run_cycles", 0)

    summary = (
        f"Direction '{direction_topic[:120]}' has shipped {lifetime_merged} merged PRs "
        f"across {total_cycles} research cycles ({merge_rate:.0%} merge rate) but its "
        f"outcome signal has stalled for {stall_cycles} consecutive cycles."
    )
    parts = [
        f"Direction: {direction_topic}",
        (
            f"Scaffolding evidence: {total_cycles} total cycles, "
            f"{lifetime_merged} lifetime merged PRs, "
            f"{merge_rate:.1%} lifetime merge rate, "
            f"{stall_cycles} consecutive stall cycles without outcome advancement."
        ),
        (
            "Assessment: The direction is generating consistent engineering activity "
            "(merges) but not advancing its declared success evidence. "
            "This indicates outcome-less scaffolding — work that builds structure "
            "without demonstrating measurable progress toward the stated goal."
        ),
    ]
    return {
        "summary": summary[:500],
        "reasoning": "\n\n".join(parts),
        "citations": [f"direction:{direction_id}"],
        "tags": ["outcome-scaffolding"],
    }


async def _directed_research_recently_completed(
    project_id, direction_id: str, session, direction: dict | None = None
) -> bool:
    """True when a directed research task for this direction COMPLETED within the
    cooldown window.

    Crash guard: protects against re-commissioning when ``_handle_research_completion``
    dies before it can update ``refractory_until`` on the direction — the refractory
    gate then sees no hold and allows a re-spawn (incident 2026-06-10).

    Mirrors ``_undirected_research_recently_active`` for the directed case; the
    undirected guard already covers all statuses, so this companion only checks
    COMPLETED. Set ``completed_research_cooldown_minutes`` <= 0 to disable.

    Absent an explicit ``completed_research_cooldown_minutes`` value the window
    derives from the direction's effective refractory window (via
    ``resolve_direction_refractory_minutes``) when a direction dict is supplied,
    or falls back to ``autonomous_direction_cooldown_minutes`` (default 240) —
    matching the refractory gate's fallback so both gates agree on the cooldown window.
    """
    from app.routers.admin_analytics import _derive_completed_at

    explicit = _runtime_config.get("completed_research_cooldown_minutes")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        cooldown_minutes = float(explicit)
    elif direction is not None:
        cooldown_minutes = resolve_direction_refractory_minutes(direction)
    else:
        base = _runtime_config.get("autonomous_direction_cooldown_minutes", 240)
        if isinstance(base, (int, float)) and not isinstance(base, bool):
            cooldown_minutes = float(base)
        else:
            cooldown_minutes = 240.0
    if cooldown_minutes <= 0:
        return False
    cutoff = _utcnow() - timedelta(minutes=cooldown_minutes)
    completed = (
        await session.execute(
            select(Task).where(
                Task.project_id == project_id,
                Task.status == TaskStatus.COMPLETED,
                Task.payloads[0]["type"].as_string() == "research",
                Task.payloads[0]["current_direction_id"].as_string()
                == str(direction_id),
            )
        )
    ).scalars()
    return any(_derive_completed_at(task) >= cutoff for task in completed)


async def _process_direction_lifecycle(
    project: Project,
    session: AsyncSession,
    busy_direction_ids: set,
    now: datetime,
) -> list:
    """Acquire project-row lock, then delegate to the inner lifecycle worker.

    Uses skip_locked=True so a concurrent operator write causes this background
    tick to be skipped rather than queue behind a 30s lock timeout.  The next
    scheduled cycle will pick up any directions that were not processed.
    """
    from app.task_locking import project_lock  # noqa: PLC0415

    async with project_lock(session, project.id, skip_locked=True) as _locked:
        if _locked is None:
            logger.debug(
                "[LIFECYCLE] Skipping direction lifecycle for %s: project row held by concurrent writer",
                project.id,
            )
            return []
        # Reload directions inside the lock so we read the latest committed
        # state and cannot overwrite a concurrent write that committed between
        # the caller's outer project load and this lock acquisition.
        await session.refresh(project, attribute_names=["directions"])
        return await _process_direction_lifecycle_locked(
            project, session, busy_direction_ids, now
        )


async def _process_direction_lifecycle_locked(
    project: Project,
    session: AsyncSession,
    busy_direction_ids: set,
    now: datetime,
) -> list:
    """Prune dead directions, auto-recover paused ones, and return eligible active directions.

    Always called under the project-row lock (via _process_direction_lifecycle).
    Mutates project.directions in place for auto-pause and recovery state changes.
    Commits the session if any direction state was modified.

    Returns a list of active, non-cooling directions eligible for research.
    """
    from statistics import median

    from app.strategic_completion import (
        _check_direction_health,
        _check_direction_completion,
        _check_outcome_scaffolding,
        _compute_churn_metrics,
        _compute_lifetime_burn_metrics,
        _evidence_progress_suppresses_scaffolding,
        _outcome_progress_config,
        already_built_completion_suggestion,
    )

    directions_modified = False
    # Prune dead directions (expired/completed/rejected) to prevent JSON bloat.
    # "staged" directions are inert-by-design (begin-after-current-version-
    # promotes) and MUST survive the prune so they remain to be auto-activated
    # at the next version-open transition — they are not dead.
    live_dirs = [
        d
        for d in (project.directions or [])
        if d.get("status") in ("active", "proposed", "paused", "staged")
    ]
    if len(live_dirs) < len(project.directions or []):
        logger.info(
            f"[AUTONOMOUS] Pruned {len(project.directions or []) - len(live_dirs)} dead directions from {project.id}"
        )
        project.directions = live_dirs
        flag_modified(project, "directions")
        directions_modified = True
    # Retry BLOCKER escalation notifications whose initial delivery failed.
    # Runs before auto-recovery so a direction that recovers this cycle does
    # not silently lose its pending notification.
    for d in project.directions or []:
        if not d.get("escalation_notification_pending"):
            continue
        retries = int(d.get("escalation_notification_retries", 0))
        if retries >= _MAX_ESCALATION_NOTIFICATION_RETRIES:
            logger.error(
                "[NOTIFY] Escalation notification retries exhausted",
                extra={
                    "event": "escalation_retries_exhausted",
                    "project_id": str(project.id),
                    "direction_id": d.get("id"),
                    "retries": retries,
                    "remediation_attempts": int(d.get("remediation_attempts", 0)),
                },
            )
            from app.models import Alert, AlertSeverity

            session.add(
                Alert(
                    project_id=project.id,
                    severity=AlertSeverity.ERROR,
                    title="Escalation notification delivery failed",
                    message=(
                        f"Direction {d.get('id')} ('{(d.get('text') or '')[:80]}') needs manual "
                        f"intervention but the BLOCKER notification could not be delivered after "
                        f"{retries} retries. Fingerprint={d.get('error_fingerprint') or '(unknown)'}, "
                        f"remediation_attempts={int(d.get('remediation_attempts', 0))}."
                    ),
                )
            )
            d.pop("escalation_notification_pending", None)
            directions_modified = True
            continue
        try:
            from app.notification_dispatch import post_remediation_escalation_message

            await post_remediation_escalation_message(
                session,
                project.id,
                **_build_escalation_args(d),
            )
            d.pop("escalation_notification_pending", None)
            d.pop("escalation_notification_retries", None)
            directions_modified = True
            logger.info(
                f"[NOTIFY] Escalation retry succeeded for direction {d.get('id')} "
                f"after {retries} prior retries"
            )
        except Exception as _retry_exc:
            d["escalation_notification_retries"] = retries + 1
            directions_modified = True
            logger.warning(
                f"[NOTIFY] Escalation retry {retries + 1} failed for {d.get('id')}: {_retry_exc}"
            )
    # TTL fallback for stuck needs_manual_intervention. The flag is normally cleared
    # when the remediation monitor expires (line ~561) or when the fingerprint
    # changes on recovery, but both paths require remediation_monitor_started_at,
    # which only gets set on a successful remediation merge. If remediation keeps
    # failing, the flag (and operator alert) persists forever — clear it here
    # once enough time has passed without a monitor ever starting.
    #
    # EXCEPTION: lifetime-burn pauses set needs_manual_intervention but never set
    # a remediation monitor (they are not code-failure remediation cases). They require
    # manual operator action (retire or reset) and should persist until resolved,
    # not expire via this TTL safety net designed for remediation-stuck scenarios.
    for d in project.directions or []:
        if not d.get("needs_manual_intervention"):
            continue
        if d.get("remediation_monitor_started_at"):
            continue
        # Skip TTL clear for lifetime-burn pauses — they require manual operator
        # intervention and should not auto-expire.
        if (d.get("paused_reason") or "").startswith("Lifetime burn backstop:"):
            continue
        flagged_at = d.get("needs_manual_intervention_at")
        if not flagged_at:
            continue
        try:
            age_h = (
                _utcnow() - datetime.fromisoformat(flagged_at)
            ).total_seconds() / 3600
        except (ValueError, TypeError):
            continue
        if age_h < _STUCK_INTERVENTION_TTL_HOURS:
            continue
        d.pop("needs_manual_intervention", None)
        d.pop("needs_manual_intervention_at", None)
        directions_modified = True
        logger.info(
            f"[AUTONOMOUS] Cleared stuck needs_manual_intervention on {d.get('id')} "
            f"after {age_h:.1f}h with no remediation monitor started"
        )
    # Auto-recovery: reactivate auto-paused directions past cooldown
    default_recovery_hours = _runtime_config.get("direction_auto_recovery_hours", 48)
    for d in project.directions or []:
        if (
            d.get("status") != "paused"
            or not d.get("auto_paused")
            or not d.get("paused_at")
        ):
            continue
        recovery_hours = default_recovery_hours
        hours = (
            _utcnow() - datetime.fromisoformat(d["paused_at"])
        ).total_seconds() / 3600
        if hours < recovery_hours:
            continue
        if not d.get("needs_manual_intervention"):
            d["status"] = "active"
            for k in ("auto_paused", "paused_reason", "paused_at"):
                d.pop(k, None)
            # Grace period: stamp recovery time so _check_direction_health can skip
            # history-based health rules until at least one new cycle is appended.
            # Without this, the same frozen research_history that triggered the pause
            # immediately re-triggers it on the next lifecycle tick.
            d["auto_recovered_at"] = _utcnow().isoformat()
            # Start remediation-effectiveness monitoring when recovering a direction whose
            # remediation task merged. Subsequent failures with the same error fingerprint
            # during this window signal the fix did not hold; escalation is triggered below
            # when the direction is re-paused while monitoring is active.
            if d.get("last_remediation_result") in ("merged", "partial_success"):
                d["remediation_monitor_started_at"] = _utcnow().isoformat()
            logger.info(
                f"[AUTONOMOUS] Auto-recovered direction {d.get('id')} after {hours:.0f}h cooldown"
            )
            directions_modified = True
        else:
            # Lifetime-burn backstop pauses require manual operator action (retire or reset)
            # because the cumulative metric won't drop on its own. Keep the pause active
            # until the operator reactivates the direction or the TTL fallback clears the flag.
            d["paused_at"] = _utcnow().isoformat()
            logger.info(
                f"[AUTONOMOUS] Direction {d.get('id')} requires manual intervention, extending pause"
            )
            directions_modified = True
    active_directions = []
    auto_paused_any = False

    def _cost_per_merge(direction: dict) -> float | None:
        history = list(direction.get("research_history") or [])
        if cpm_window > 0:
            history = history[-cpm_window:]
        spend = 0.0
        autonomous = 0
        for entry in history:
            try:
                spend += max(0.0, float(entry.get("estimated_cost_usd") or 0.0))
                # Legacy fallback: pre-attribution entries lack autonomous_prs_merged;
                # treat all merges as autonomous so CPM is conservative for old data.
                autonomous += max(
                    0,
                    int(
                        entry.get("autonomous_prs_merged", entry.get("prs_merged")) or 0
                    ),
                )
            except (TypeError, ValueError):
                continue
        return (spend / autonomous) if autonomous > 0 else None

    def _cost_by_provider_model_window(
        direction: dict,
    ) -> tuple[dict[str, float], float]:
        """Sum per-(provider, model) cost across the same rolling window.

        Returns (totals_by_key, total_window_cost). Legacy entries lacking
        ``cost_by_provider_model`` contribute 0 to the breakdown — the caller
        falls back to omitting the split for those directions.
        """
        history = list(direction.get("research_history") or [])
        if cpm_window > 0:
            history = history[-cpm_window:]
        totals: dict[str, float] = {}
        for entry in history:
            for key, val in (entry.get("cost_by_provider_model") or {}).items():
                try:
                    totals[key] = totals.get(key, 0.0) + max(0.0, float(val))
                except (TypeError, ValueError):
                    continue
        return totals, sum(totals.values())

    try:
        cpm_window = int(_runtime_config.get("direction_cost_per_merge_window", 6) or 6)
    except (TypeError, ValueError):
        cpm_window = 6
    try:
        cpm_multiplier = float(
            _runtime_config.get("direction_cost_per_merge_multiplier", 2.0) or 2.0
        )
    except (TypeError, ValueError):
        cpm_multiplier = 2.0
    project_cpms = [
        cpm
        for cpm in (_cost_per_merge(x) for x in (project.directions or []))
        if cpm is not None and cpm > 0
    ]
    project_cpm_median = median(project_cpms) if project_cpms else None
    for d in project.directions or []:
        if d.get("status") != "active":
            continue
        did = d.get("id")
        # Skip directions with an unrolled research cycle still in progress
        if did and did in busy_direction_ids:
            logger.debug(
                f"[RESEARCH] Skipping direction {did}: research cycle already active"
            )
            continue
        # Clear stale diagnosis flags so a re-paused direction can be diagnosed again.
        # If the investigation task was reaped/timed out without reaching the completion handler,
        # diagnosis_in_progress stays True and permanently blocks future diagnosis spawning.
        if d.get("diagnosis_in_progress"):
            started_at = d.get("diagnosis_started_at")
            is_stale = True
            if started_at:
                try:
                    age_h = (
                        _utcnow() - datetime.fromisoformat(started_at)
                    ).total_seconds() / 3600
                    is_stale = age_h >= _DIAGNOSIS_STALE_HOURS
                except (ValueError, TypeError):
                    is_stale = True
            if is_stale:
                prior_task_id = d.get("diagnosis_task_id")
                logger.warning(
                    f"[DIAGNOSIS] Clearing stale diagnosis flags for direction {did} "
                    f"(started_at={started_at})"
                )
                d.pop("diagnosis_in_progress", None)
                d.pop("diagnosis_started_at", None)
                d.pop("diagnosis_task_id", None)
                d["diagnosis_completed_at"] = _utcnow().isoformat()
                directions_modified = True
                # The auto-diagnosis re-spawn lane was removed with the
                # detect->investigate->fix apparatus strip. Surface the stalled
                # diagnosis as a deterministic operator Alert instead so the
                # signal (a direction whose investigation never produced a
                # finding) is preserved for manual follow-up.
                from app.models import Alert, AlertSeverity

                session.add(
                    Alert(
                        project_id=project.id,
                        severity=AlertSeverity.WARNING,
                        title="Direction diagnosis stalled",
                        message=(
                            f"Direction '{(d.get('text') or '')[:80]}' ({did}) had a "
                            f"diagnosis investigation time out without producing a "
                            f"finding (prior task {prior_task_id}). Manual "
                            f"investigation required."
                        ),
                    )
                )
                logger.warning(
                    f"[DIAGNOSIS] Stalled diagnosis for {did}; "
                    f"escalating to operator alert"
                )
        # If a remediation monitor has expired while the direction stayed active,
        # the fix held — drop the monitor and clear any lingering manual-intervention
        # alert so the UI stops showing a permanent red banner after recovery.
        monitor_started_at = d.get("remediation_monitor_started_at")
        if monitor_started_at:
            expired = False
            age_desc = ""
            try:
                age_days = (
                    _utcnow() - datetime.fromisoformat(monitor_started_at)
                ).total_seconds() / 86400
                if age_days > _REMEDIATION_MONITOR_EXPIRY_DAYS:
                    expired = True
                    age_desc = f"age {age_days:.1f}d"
            except (TypeError, ValueError):
                # Unparseable timestamp — safer to treat the monitor as expired
                # than to keep it active indefinitely and risk a false escalation.
                expired = True
                age_desc = f"unparseable timestamp {monitor_started_at!r}"
            if expired:
                d.pop("remediation_monitor_started_at", None)
                cleared = d.pop("needs_manual_intervention", None)
                d.pop("needs_manual_intervention_at", None)
                # Stamp "held" on the remediation_history entry that started this monitor
                # so the digest funnel can count fixes that survived the full window.
                held_iso = _utcnow().isoformat()
                history = list(d.get("remediation_history") or [])
                for entry in reversed(history):
                    if entry.get("result") in ("merged", "partial_success"):
                        entry["result"] = "held"
                        entry["held_at"] = held_iso
                        d["remediation_history"] = history
                        break
                d["last_remediation_held_at"] = held_iso
                if d.get("last_remediation_result") in ("merged", "partial_success"):
                    d["last_remediation_result"] = "held"
                logger.info(
                    f"[AUTONOMOUS] Remediation monitor expired for {did} "
                    f"({age_desc}); fix held; "
                    f"{'cleared needs_manual_intervention' if cleared else 'no intervention flag to clear'}"
                )
                directions_modified = True
        # Refractory check: direction is held ineligible until refractory_until elapses.
        # Set by research completion (standard window) or no-findings saturation (longer
        # window). Strategist/operator can clear refractory_until to re-open the direction.
        refractory_info = get_direction_refractory_info(d, now=now)
        remaining_m = refractory_info["refractory_remaining_minutes"]
        if remaining_m is not None and remaining_m > 0:
            logger.debug(
                "[RESEARCH] Skipping direction %s: %s hold for %.1fm more",
                did,
                refractory_info["refractory_reason"],
                remaining_m,
            )
            continue
        # Cost efficiency escalation: flag expensive directions for retirement consideration.
        direction_cpm = _cost_per_merge(d)
        threshold = (
            (project_cpm_median * cpm_multiplier) if project_cpm_median else None
        )
        if direction_cpm and threshold and direction_cpm > threshold:
            d["retirement_consideration_needed"] = True
            d["retirement_consideration_at"] = _utcnow().isoformat()
            d["retirement_consideration_reason"] = (
                f"Recent cost/merge ${direction_cpm:.2f} exceeded threshold ${threshold:.2f}."
            )
            evidence: dict = {
                "window_cycles": cpm_window,
                "direction_cost_per_merge_usd": round(direction_cpm, 4),
                "threshold_usd": round(threshold, 4),
            }
            split_totals, split_total = _cost_by_provider_model_window(d)
            if split_totals and split_total > 0:
                evidence["cost_by_provider_model"] = {
                    key: {
                        "cost_usd": round(cost, 4),
                        "share_pct": round(cost / split_total, 4),
                    }
                    for key, cost in split_totals.items()
                }
            else:
                evidence["cost_by_provider_model"] = {}
            d["retirement_consideration_evidence"] = evidence
            directions_modified = True
            logger.warning(
                f"[AUTONOMOUS] Direction {did} flagged for retirement consideration: "
                f"cost/merge ${direction_cpm:.2f} > ${threshold:.2f}"
            )
        # Auto-pause check — must come after cooldown to only evaluate directions eligible for research
        pause_reason = _check_direction_health(d)
        cumulative_cost = 0.0
        for entry in d.get("research_history") or []:
            try:
                cumulative_cost += max(
                    0.0, float(entry.get("estimated_cost_usd") or 0.0)
                )
            except (TypeError, ValueError):
                continue
        budget = d.get("cost_budget_usd")
        try:
            budget_f = float(budget) if budget is not None else None
        except (TypeError, ValueError):
            budget_f = None
        if budget_f and cumulative_cost > budget_f:
            pause_reason = (
                f"Budget exceeded: cumulative estimated cost ${cumulative_cost:.2f} "
                f"> cost budget ${budget_f:.2f}"
            )
            d["cost_pause_evidence"] = {
                "cumulative_estimated_cost_usd": round(cumulative_cost, 4),
                "cost_budget_usd": round(budget_f, 4),
            }
        # Fix #3: route "already-built" directions to completion instead of a
        # churn / zero-merge auto-pause. When the pending pause is a cumulative
        # merge-rate signal (Rule 2 Unproductive, Rule 5 Churn, Rule 6 Lifetime burn)
        # AND the direction's recent cycles are dominated by correct no-op
        # "already-present" completions (no merges, no code failures), the goal is
        # effectively already met. Suppress the pause here; the possibly_complete
        # flag itself is set authoritatively by the positive-completion block below
        # (which also owns clearing it), so the two paths cannot fight over the flag.
        # Budget and code-failure pauses are deliberately excluded — those are not
        # "already built".
        _CHURN_FAMILY_PREFIXES = (
            "Unproductive:",
            "Churn guardrail:",
            "Lifetime burn backstop:",
        )
        if (
            pause_reason
            and pause_reason.startswith(_CHURN_FAMILY_PREFIXES)
            and already_built_completion_suggestion(d)
        ):
            logger.info(
                f"[AUTONOMOUS] Direction {did} suppressing churn-pause "
                f"({pause_reason!r}); routing to possibly_complete (already built)"
            )
            pause_reason = None
        if pause_reason:
            d["status"] = "paused"
            d["paused_reason"] = pause_reason
            d["paused_at"] = _utcnow().isoformat()
            d["auto_paused"] = True
            d["auto_pause_count"] = d.get("auto_pause_count", 0) + 1
            # Zombie ladder: count churn/unproductive/saturated re-pauses so a
            # direction that oscillates between paused and auto-recovered without
            # ever shipping work can be auto-retired below (ref bf6d8788).
            if pause_reason.startswith(_ZOMBIE_FAMILY_PREFIXES):
                d["churn_family_pause_count"] = (
                    d.get("churn_family_pause_count", 0) + 1
                )
            d.pop("auto_recovered_at", None)  # Grace period consumed on re-pause
            d.pop(
                "diagnosis_stall_retry_count", None
            )  # Reset retries for this pause event
            # Store structured evidence for churn guardrail pauses so the operator
            # can see the windowed counts (not just the reason string).
            if pause_reason.startswith("Churn guardrail:"):
                churn_ev = _compute_churn_metrics(d)
                if churn_ev:
                    d["churn_pause_evidence"] = churn_ev
            # Lifetime burn backstop: store evidence and flag for manual intervention.
            # The lifetime metric won't drop on its own after auto-recovery, so the
            # direction would thrash through repeated pause/extend cycles — surface it
            # for operator decision (retire vs. reset history_aggregate) instead.
            if pause_reason.startswith("Lifetime burn backstop:"):
                lb_ev = _compute_lifetime_burn_metrics(d)
                if lb_ev:
                    d["lifetime_burn_pause_evidence"] = lb_ev
                if not d.get("needs_manual_intervention"):
                    d["needs_manual_intervention"] = True
                    d["needs_manual_intervention_at"] = _utcnow().isoformat()
                    logger.warning(
                        f"[AUTONOMOUS] Direction {did} flagged needs_manual_intervention "
                        f"(lifetime burn backstop): operator must retire or reset."
                    )
            # Only code-failure pauses (Rules 3/4) count toward auto-retirement.
            # Infrastructure-driven pauses (saturation, unproductive) shouldn't
            # permanently retire an otherwise healthy direction.
            if _is_code_failure_pause_reason(pause_reason):
                d["code_failure_pause_count"] = d.get("code_failure_pause_count", 0) + 1
            # Stamp the dominant error fingerprint from the direction's recent failures
            # so the diagnostics API can surface it and escalation can verify that the
            # same root cause keeps recurring after remediation.
            if _is_code_failure_pause_reason(pause_reason):
                try:
                    fingerprint = await _compute_direction_error_fingerprint(
                        project.id, did, session
                    )
                    if fingerprint:
                        d["error_fingerprint"] = fingerprint
                except Exception as _fp_exc:
                    logger.warning(
                        f"[DIAGNOSIS] Failed to compute error fingerprint for {did}: {_fp_exc!r}"
                    )
            # Re-pause while remediation monitoring is active means the fix did not hold.
            # Flag for manual intervention once attempts reach the escalation threshold so
            # the diagnostics API and notification dispatcher can surface it to operators.
            monitor_started_at = d.get("remediation_monitor_started_at")
            monitor_expired = False
            if monitor_started_at:
                try:
                    age_days = (
                        _utcnow() - datetime.fromisoformat(monitor_started_at)
                    ).total_seconds() / 86400
                    if age_days > _REMEDIATION_MONITOR_EXPIRY_DAYS:
                        monitor_expired = True
                        d.pop("remediation_monitor_started_at", None)
                        logger.info(
                            f"[AUTONOMOUS] Cleared stale remediation monitor on {did} "
                            f"(age {age_days:.1f}d > {_REMEDIATION_MONITOR_EXPIRY_DAYS}d)"
                        )
                except (TypeError, ValueError):
                    # Unparseable timestamp — safe default is to treat the monitor
                    # as expired so escalation logic does not fire off a bad value.
                    monitor_expired = True
                    d.pop("remediation_monitor_started_at", None)
                    logger.warning(
                        f"[AUTONOMOUS] Cleared unparseable remediation monitor on {did}: "
                        f"{monitor_started_at!r}"
                    )
            # Only escalate when the monitor is active AND the recurring failure matches
            # the fingerprint we already remediated (or fingerprint data is unavailable —
            # conservative fallback so we don't silently drop escalations).
            current_fp = d.get("error_fingerprint")
            last_rem_fp = d.get("last_remediation_fingerprint")
            fingerprints_match = (
                not current_fp or not last_rem_fp or str(current_fp) == str(last_rem_fp)
            )
            if (
                monitor_started_at
                and not monitor_expired
                and d.get("remediation_attempts", 0) >= 3
                and fingerprints_match
            ):
                partial_result = _detect_partial_remediation_success(d)
                if partial_result is True:
                    # Fix materially reduced failure rate even though the
                    # dominant fingerprint is still present — treat as partial
                    # progress so the direction is not escalated to manual
                    # intervention on the next remediation cycle.
                    d["last_remediation_result"] = "partial_success"
                    # Update the most recent merged remediation_history entry so
                    # follow-up investigations and spawn calibration can read the
                    # nuanced outcome ("partial" rather than "fully merged").
                    partial_iso = _utcnow().isoformat()
                    history = list(d.get("remediation_history") or [])
                    for entry in reversed(history):
                        if entry.get("result") == "merged":
                            entry["result"] = "partial_success"
                            entry["partial_success_at"] = partial_iso
                            d["remediation_history"] = history
                            break
                    logger.info(
                        f"[AUTONOMOUS] Direction {did} showed partial remediation success "
                        f"(code-failure rate dropped >= {int(_PARTIAL_SUCCESS_RATE_DROP * 100)}%); "
                        f"skipping manual-intervention escalation"
                    )
                elif partial_result is None:
                    logger.info(
                        f"[AUTONOMOUS] Direction {did} has insufficient research_history "
                        f"to evaluate remediation success; deferring manual-intervention escalation"
                    )
                else:
                    d["needs_manual_intervention"] = True
                    d["needs_manual_intervention_at"] = _utcnow().isoformat()
                    logger.warning(
                        f"[AUTONOMOUS] Direction {did} flagged needs_manual_intervention "
                        f"after {d.get('remediation_attempts', 0)} remediation attempts"
                    )
                    try:
                        from app.notification_dispatch import (
                            post_remediation_escalation_message,
                        )

                        await post_remediation_escalation_message(
                            session,
                            project.id,
                            **_build_escalation_args(d),
                        )
                        # Clear any pending retry stamps from a prior failed escalation
                        d.pop("escalation_notification_pending", None)
                        d.pop("escalation_notification_retries", None)
                    except Exception as _esc_exc:
                        logger.warning(
                            f"[NOTIFY] Failed to post remediation escalation message: {_esc_exc}"
                        )
                        # Stamp for retry on the next lifecycle cycle. Retry count is
                        # preserved if an earlier escalation on this direction was also
                        # pending; the retry loop enforces _MAX_ESCALATION_NOTIFICATION_RETRIES.
                        d["escalation_notification_pending"] = True
                        d["escalation_notification_retries"] = int(
                            d.get("escalation_notification_retries", 0)
                        )
            logger.info(f"[AUTONOMOUS] Auto-paused direction {did}: {pause_reason}")
            auto_paused_any = True
            try:
                if pause_reason.startswith("Lifetime burn backstop:"):
                    from app.notification_dispatch import (
                        post_direction_burn_pause_message,
                    )  # noqa: PLC0415

                    await post_direction_burn_pause_message(
                        session,
                        project.id,
                        direction_text=d.get("text", ""),
                        reason=pause_reason,
                        direction_id=did,
                        evidence=d.get("lifetime_burn_pause_evidence"),
                    )
                elif pause_reason.startswith("Churn guardrail:"):
                    from app.notification_dispatch import (
                        post_direction_churn_pause_message,
                    )  # noqa: PLC0415

                    await post_direction_churn_pause_message(
                        session,
                        project.id,
                        direction_text=d.get("text", ""),
                        reason=pause_reason,
                        direction_id=did,
                        evidence=d.get("churn_pause_evidence"),
                    )
                else:
                    from app.notification_dispatch import (
                        post_direction_milestone_message,
                    )  # noqa: PLC0415

                    await post_direction_milestone_message(
                        session,
                        project.id,
                        direction_text=d.get("text", ""),
                        milestone="auto-paused",
                        reason=pause_reason,
                        direction_id=did,
                    )
            except Exception as _notify_exc:
                logger.warning(
                    f"[NOTIFY] Failed to post direction pause notification: {_notify_exc}"
                )
            # Zombie auto-retire ladder (4th ladder, ref bf6d8788): a direction
            # that has now been churn/unproductive/saturated-paused >= the
            # threshold has never escaped the recycling-zombie loop — no findings,
            # no merged PRs, no in-flight tasks (the churn-family rules imply low
            # throughput). Retire it terminally so the strategic agent can reuse
            # the slot. Fires on the SAME pause event, before the code-failure
            # ladder check below.
            churn_count = d.get("churn_family_pause_count", 0)
            if churn_count >= _MAX_CHURN_PAUSE_CYCLES:
                d["status"] = "completed"
                d["auto_completed"] = True
                d["auto_completed_reason"] = (
                    f"Auto-retired: zombie direction paused {churn_count} times for "
                    f"churn/unproductive/saturated with no findings or merged PRs. "
                    f"Slot freed for new directions."
                )
                d["completed_at"] = _utcnow().isoformat()
                logger.info(
                    f"[AUTONOMOUS] Auto-retired zombie direction {did} after "
                    f"{churn_count} churn-family auto-pause cycles: "
                    f"{d.get('text', '')[:60]}"
                )
                from app.models import Alert, AlertSeverity

                session.add(
                    Alert(
                        project_id=project.id,
                        severity=AlertSeverity.INFO,
                        title="Direction auto-retired (zombie)",
                        message=(
                            f"Direction '{d.get('text', '')[:80]}' was auto-retired after "
                            f"{churn_count} churn/unproductive/saturated auto-pause cycles. "
                            f"Slot freed for strategic agent to create a replacement."
                        ),
                    )
                )
                try:
                    from app.notification_dispatch import (
                        post_direction_milestone_message,
                    )  # noqa: PLC0415

                    await post_direction_milestone_message(
                        session,
                        project.id,
                        direction_text=d.get("text", ""),
                        milestone="auto-retired-zombie",
                        reason=d["auto_completed_reason"],
                        direction_id=did,
                    )
                except Exception as _notify_exc:
                    logger.warning(
                        f"[NOTIFY] Failed to post zombie retire milestone for {did}: "
                        f"{_notify_exc}"
                    )
                # Gap reconciliation (Direction C, ref 626ec3f5): free the gap this
                # direction was staffing so it can be re-staffed. Fail-open — a
                # malformed problem model returns None and the retire still lands.
                gap_id = d.get("gap_id")
                if gap_id:
                    from app.problem_model import reconcile_gap_staffing_raw

                    _reconciled_pm = reconcile_gap_staffing_raw(
                        project.problem_model, did, None, gap_id
                    )
                    if _reconciled_pm is not None:
                        project.problem_model = _reconciled_pm
                        flag_modified(project, "problem_model")
                directions_modified = True
                continue  # terminal — skip active_directions.append
            MAX_AUTO_PAUSE_CYCLES = _MAX_AUTO_PAUSE_CYCLES
            code_pause_count = d.get("code_failure_pause_count", 0)
            if code_pause_count >= MAX_AUTO_PAUSE_CYCLES:
                d["status"] = "completed"
                d["auto_completed"] = True
                d["auto_completed_reason"] = (
                    f"Auto-retired: direction hit {code_pause_count} code-failure "
                    f"auto-pauses without producing results. Slot freed for new directions."
                )
                d["completed_at"] = _utcnow().isoformat()
                logger.info(
                    f"[AUTONOMOUS] Auto-retired direction {did} after "
                    f"{code_pause_count} code-failure auto-pause cycles: {d.get('text', '')[:60]}"
                )
                from app.models import Alert, AlertSeverity

                alert = Alert(
                    project_id=project.id,
                    severity=AlertSeverity.INFO,
                    title="Direction auto-retired",
                    message=(
                        f"Direction '{d.get('text', '')[:80]}' was auto-retired after "
                        f"{code_pause_count} consecutive code-failure auto-pause cycles. "
                        f"Slot freed for strategic agent to create a replacement."
                    ),
                )
                session.add(alert)
                try:
                    from app.notification_dispatch import (
                        post_direction_milestone_message,
                    )  # noqa: PLC0415

                    await post_direction_milestone_message(
                        session,
                        project.id,
                        direction_text=d.get("text", ""),
                        milestone="auto-retired",
                        reason=d["auto_completed_reason"],
                        direction_id=did,
                    )
                except Exception as _notify_exc:
                    logger.warning(
                        f"[NOTIFY] Failed to post direction milestone message: {_notify_exc}"
                    )
                directions_modified = True
            # The auto-paused direction's diagnosis-research spawn was removed
            # with the detect->investigate->fix apparatus strip. The pause itself
            # (and its operator notification, posted above) is the preserved
            # signal; the strategic agent / operator decides any follow-up.
            continue
        # Positive completion detection — mark for user review but don't pause.
        # Two signals feed possibly_complete: the merge-based heuristic
        # (_check_direction_completion) and the Fix #3 "already-built" signature
        # (no-op-dominated completions). The already-built signal is the reason a
        # churn/zero-merge pause was just suppressed above, so it must set the flag
        # here; otherwise the else-branch below would immediately pop it.
        completion_suggestion = _check_direction_completion(
            d
        ) or already_built_completion_suggestion(d)
        if completion_suggestion:
            if not d.get("possibly_complete"):
                d["possibly_complete"] = True
                d["completion_suggestion"] = completion_suggestion
                logger.info(
                    f"[AUTONOMOUS] Direction {did} possibly complete: {completion_suggestion}"
                )
                directions_modified = True
        else:
            if d.get("possibly_complete"):
                d.pop("possibly_complete", None)
                d.pop("completion_suggestion", None)
                directions_modified = True
        # Outcome-progress (scaffolding) detection — persistence-pressure ladder.
        # Flags a long-running healthy-merge direction whose outcome signal has
        # stalled. The flag never triggers the 6 auto-PAUSE rules above; instead it
        # escalates over consecutive flagged cycles: notify on entry, escalation
        # notification at escalation_cycles, and auto-RETIRE at max_flagged_cycles
        # (an advancing/met evidence_progress verdict suppresses the counter). This
        # surfaces outcome-less scaffolding to the strategic agent / operator and,
        # if it persists, frees the slot. Notify once per transition into flagged.
        scaffolding_ev = _check_outcome_scaffolding(d)
        if scaffolding_ev:
            if not d.get("outcome_scaffolding_flagged"):
                d["outcome_scaffolding_flagged"] = True
                d["outcome_scaffolding_flagged_at"] = _utcnow().isoformat()
                d["outcome_scaffolding_evidence"] = scaffolding_ev
                d["outcome_scaffolding_last_cycle_count"] = scaffolding_ev[
                    "total_cycles"
                ]
                directions_modified = True
                logger.warning(
                    f"[AUTONOMOUS] Direction {did} flagged outcome-less scaffolding: "
                    f"{scaffolding_ev['total_cycles']} cycles, "
                    f"{scaffolding_ev['lifetime_merged']} merged "
                    f"(rate {scaffolding_ev['lifetime_merge_rate']:.0%}), "
                    f"progress stalled {scaffolding_ev['stall_run_cycles']} cycles"
                )
                try:
                    from app.notification_dispatch import (
                        post_direction_outcome_stalled_message,
                    )  # noqa: PLC0415

                    await post_direction_outcome_stalled_message(
                        session,
                        project.id,
                        direction_text=d.get("text", ""),
                        evidence=scaffolding_ev,
                        direction_id=did,
                    )
                except Exception as _notify_exc:
                    logger.warning(
                        f"[NOTIFY] Failed to post outcome-stalled message for {did}: "
                        f"{_notify_exc}"
                    )
                # Spawn one memo task to capture this signal as a citable artifact.
                # The flag already gates first-detection only so no dedup query needed.
                try:
                    from app.plan_completion import TASK_PRIORITY_MAP  # noqa: PLC0415
                    from app.task_completion import _apply_synthesized_memo  # noqa: PLC0415

                    _scaffolding_topic = (d.get("text") or "")[
                        :120
                    ] or f"Direction {did}"
                    _scaffolding_payload = validate_internal_payload(
                        {
                            "type": "memo",
                            "topic": f"Outcome-less scaffolding: {_scaffolding_topic}",
                            "reasoning_kind": "outcome_scaffolding",
                            "direction_id": did,
                            "scaffolding_evidence": scaffolding_ev,
                        },
                        "direction_lifecycle:memo:outcome_scaffolding",
                    )
                    _scaffolding_memo = Task(
                        project_id=project.id,
                        title=f"Memo: outcome-less scaffolding — {_scaffolding_topic[:80]}",
                        description="Reasoning memo for outcome-less scaffolding detection",
                        payloads=[_scaffolding_payload],
                        context={
                            "memo_reasoning_kind": "outcome_scaffolding",
                            "memo_source_direction_id": did,
                            "outcome_scaffolding_evidence": scaffolding_ev,
                        },
                        status=TaskStatus.QUEUED,
                        created_by="ORCHESTRATOR",
                        priority=TASK_PRIORITY_MAP.get("low", 0),
                    )
                    session.add(_scaffolding_memo)
                    _synth_memo = _synthesize_outcome_scaffolding_memo(
                        did, _scaffolding_topic, scaffolding_ev
                    )
                    _apply_synthesized_memo(_scaffolding_memo, _synth_memo, session)
                    logger.info(
                        "[AUTONOMOUS] Spawned outcome-scaffolding memo task %s for direction %s",
                        _scaffolding_memo.id,
                        did,
                    )
                except Exception:
                    try:
                        _scaffolding_memo.status = TaskStatus.FAILED
                    except Exception:
                        pass
                    logger.warning(
                        f"[AUTONOMOUS] Failed to spawn scaffolding memo for direction {did}",
                        exc_info=True,
                    )
            else:
                # Keep the evidence fresh while the flag remains set.
                d["outcome_scaffolding_evidence"] = scaffolding_ev
                # Persistence-pressure ladder: increment each cycle the flag holds.
                _scaffolding_cfg = _outcome_progress_config()
                _esc_threshold = _scaffolding_cfg["escalation_cycles"]
                _retire_threshold = _scaffolding_cfg["max_flagged_cycles"]
                # A RECENT advancing/met evidence_progress verdict suppresses the
                # counter increment — uses the same recency-aware helper as the
                # detector so stale verdicts grant no credit (consistent semantics).
                _ep_suppresses = _evidence_progress_suppresses_scaffolding(
                    d,
                    scaffolding_ev["total_cycles"],
                    scaffolding_ev["stall_run_cycles"],
                )
                if not _ep_suppresses:
                    _advanced_flag_cycle = False
                    _current_cycles = scaffolding_ev["total_cycles"]
                    _last_counted = d.get("outcome_scaffolding_last_cycle_count")
                    if _last_counted is None:
                        # Legacy flagged records predate the watermark field.
                        # Backfill from the current completed-cycle count so
                        # this tick does not miscount a stale cycle as new work.
                        d["outcome_scaffolding_last_cycle_count"] = _current_cycles
                        _last_counted = _current_cycles
                        directions_modified = True
                    if _current_cycles <= _last_counted:
                        # No new completed research cycle — skip increment.
                        pass
                    else:
                        _flag_cycles = (
                            d.get("outcome_scaffolding_flagged_cycles", 0) + 1
                        )
                        d["outcome_scaffolding_flagged_cycles"] = _flag_cycles
                        d["outcome_scaffolding_last_cycle_count"] = _current_cycles
                        _advanced_flag_cycle = True
                        directions_modified = True
                    _flag_cycles = d.get("outcome_scaffolding_flagged_cycles", 0)
                    if _flag_cycles >= _retire_threshold:
                        # Auto-retire — mirroring the code_failure_pause_count ladder.
                        d["status"] = "completed"
                        d["auto_completed"] = True
                        d["auto_completed_reason"] = (
                            f"Auto-retired: outcome-less scaffolding flag unresolved "
                            f"for {_flag_cycles} consecutive cycles. Direction kept "
                            f"shipping merges but never advanced its success_evidence."
                        )
                        d["completed_at"] = _utcnow().isoformat()
                        logger.info(
                            f"[AUTONOMOUS] Auto-retired direction {did} after "
                            f"{_flag_cycles} scaffolding-flagged cycles: {d.get('text', '')[:60]}"
                        )
                        from app.models import Alert, AlertSeverity  # noqa: PLC0415

                        _sc_alert = Alert(
                            project_id=project.id,
                            severity=AlertSeverity.INFO,
                            title="Direction auto-retired (outcome scaffolding)",
                            message=(
                                f"Direction '{d.get('text', '')[:80]}' was auto-retired after "
                                f"{_flag_cycles} cycles with outcome-less scaffolding flag unresolved. "
                                f"Slot freed for strategic agent to create a replacement."
                            ),
                        )
                        session.add(_sc_alert)
                        try:
                            from app.notification_dispatch import (  # noqa: PLC0415
                                post_direction_milestone_message,
                            )

                            await post_direction_milestone_message(
                                session,
                                project.id,
                                direction_text=d.get("text", ""),
                                milestone="auto-retired-scaffolding",
                                reason=d["auto_completed_reason"],
                                direction_id=did,
                            )
                        except Exception as _sc_notify_exc:
                            logger.warning(
                                f"[NOTIFY] Failed to post scaffolding auto-retire milestone: {_sc_notify_exc}"
                            )
                        # Retired direction is terminal — skip the append so the
                        # downstream greedy selection cannot spawn a cycle on it
                        # this tick (mirrors the auto-pause `continue` at L1202).
                        continue
                    elif _advanced_flag_cycle and _flag_cycles == _esc_threshold:
                        # Escalation notification at mid-threshold.
                        try:
                            from app.notification_dispatch import (  # noqa: PLC0415
                                post_direction_outcome_scaffolding_escalation_message,
                            )

                            await post_direction_outcome_scaffolding_escalation_message(
                                session,
                                project.id,
                                direction_text=d.get("text", ""),
                                flagged_cycles=_flag_cycles,
                                evidence=scaffolding_ev,
                                direction_id=did,
                            )
                        except Exception as _esc_notify_exc:
                            logger.warning(
                                f"[NOTIFY] Failed to post scaffolding escalation for {did}: {_esc_notify_exc}"
                            )
        elif d.get("outcome_scaffolding_flagged"):
            d.pop("outcome_scaffolding_flagged", None)
            d.pop("outcome_scaffolding_flagged_at", None)
            d.pop("outcome_scaffolding_evidence", None)
            d.pop("outcome_scaffolding_flagged_cycles", None)
            d.pop("outcome_scaffolding_last_cycle_count", None)
            directions_modified = True
            logger.info(
                f"[AUTONOMOUS] Cleared outcome-scaffolding flag on {did}: "
                f"condition no longer holds"
            )
        # Crash guard: skip if a research task for this direction completed within the
        # cooldown window — covers the gap when _handle_research_completion dies before
        # stamping refractory_until (incident 2026-06-10).
        if did and await _directed_research_recently_completed(
            project.id, did, session, direction=d
        ):
            logger.info(
                "[RESEARCH] Skipping direction %s: a research task completed within the "
                "cooldown window (crash guard)",
                did,
            )
            continue
        active_directions.append(d)
    if auto_paused_any or directions_modified:
        flag_modified(project, "directions")
        session.add(project)
        await session.commit()
    return active_directions
