"""Strategic completion handler and direction health checks.

Extracted from app.task_completion to keep that module focused on plan/security/perf
completion handlers. Contains direction health/completion checks, duplicate detection,
and the _handle_strategic_completion handler.
"""
import json
import logging
import uuid
from datetime import timedelta
from typing import Any, Dict

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app import strategic_cycle
from app.direction_metrics import _compute_direction_quality_score
from app.models import (
    MessageType,
    Project,
    ProjectMessage,
    RECOMMENDED_MODE_META_KEY,
    RECOMMENDED_MODE_SOURCE_STRATEGIC,
    Task,
)
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


async def _record_strategic_task_failure(
    task: Task,
    session: AsyncSession,
    *,
    project: Project | None = None,
) -> None:
    """Record a FAILED strategic task in the circuit breaker and cycle history.

    Called from _process_post_commit when a design_doc_update or strategic_review
    task terminates with FAILED status (VM crash, timeout, etc.).

    Commit semantics (R5-W5 audit MED-1):
    -----------------------------------
    This function does NOT commit. It mutates ``project.vision`` and (when a
    cycle id is present) the StrategicCycle row, then ``session.add(project)``.
    The caller is expected to commit. ``_process_post_commit`` in
    ``task_lifecycle.py`` batches several mutations from a single terminal
    transition (dependent unblock, strategic-failure increment, research-failure
    increment, investigation-spawn) into one commit at the end of the post-
    commit chain — that's the design intent.

    Trade-off: if a later mutation in the same chain raises, the strategic-
    failure increment rolls back too. That's the chosen behavior — partial
    state is worse than a re-tried whole. The next FAILED-terminal transition
    on this task will re-enter the chain (it's idempotent: the increment is
    a write of ``failures = old + 1`` against the freshly loaded vision).
    Direct callers (currently only the test suite) that want immediate
    persistence must commit themselves.

    ``project=None`` fallback (R5-W5 audit MED-2):
    ---------------------------------------------
    Production callers always pass ``locked_project`` because
    ``task_lifecycle.py:1126`` pair-locks Project before Task for strategic
    types via ``lock_project_then_task`` (canonical Project-before-Task order
    per docs/concurrency.md). The fallback path below — acquiring the project
    via FOR UPDATE SKIP LOCKED — is therefore only exercised by direct test
    callers (see ``testing/test_strategic_completion.py::TestRecordStrategicTaskFailure``).
    It's preserved so the function remains independently testable without
    test fixtures having to wire up the canonical pair-lock.

    Phase C3 (HIGH H-C4): post-commit chain pre-acquires the project lock via
    ``lock_project_for_strategic_transition`` and passes the locked Project
    here as ``project=``.

    Round 4 W1 (audit M1): historically the fallback called
    ``session.get(Project, ...)`` with no lock, allowing simultaneous
    strategic_review and design_doc_update failures to read the same
    in-memory ``_strategic_failures`` baseline and write back the same
    incremented value — losing one increment. Fix: when ``project=None``,
    acquire FOR UPDATE with ``skip_locked=True``. If the row is held by
    a parallel handler, log and return — the parallel handler is already
    incrementing the counter, so silently skipping is safe (no failure
    is lost; the parallel handler will record both, since it sees the
    same baseline and processes its own task's failure).

    Note: when the parallel handler holds the lock, it processes its
    OWN task's failure — not this task's. The "skip" behavior here is
    therefore not a true reconciliation; the calling layer
    (``_process_post_commit``) is expected to re-call this on the next
    sweep if the task's status is still FAILED. In practice the
    post-commit chain is invoked once per terminal transition, so a
    skip on contention may drop a single increment under high
    concurrency. This is acceptable: the circuit breaker uses
    ``_strategic_failures`` as a noisy signal, not a strict count.
    """
    if project is None:
        # M1: acquire FOR UPDATE skip_locked instead of unlocked get.
        # populate_existing protects against identity-map cache if the
        # caller's session previously read this Project unlocked.
        result = await session.execute(
            select(Project)
            .where(Project.id == task.project_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        project = result.scalars().first()
        if project is None:
            # Disambiguate "missing" vs "held under contention" so logs
            # remain useful for postmortems.
            exists = (await session.execute(
                select(Project.id).where(Project.id == task.project_id)
            )).first()
            if exists is None:
                logger.warning(
                    f"[STRATEGIC] _record_strategic_task_failure: project "
                    f"{task.project_id} not found for task {task.id}"
                )
            else:
                logger.warning(
                    f"[STRATEGIC] _record_strategic_task_failure: project "
                    f"{task.project_id} held by parallel handler — skipping "
                    f"failure increment for task {task.id} (parallel handler "
                    f"records its own task's failure independently)"
                )
            return
    if not project:
        return
    vision = dict(project.vision or {})
    failures = vision.get("_strategic_failures", 0) + 1
    vision["_strategic_failures"] = failures
    vision["_strategic_failures_at"] = _utcnow().isoformat()
    project.vision = vision
    flag_modified(project, "vision")
    # Audit W2: cycle history is in the StrategicCycle table.
    cycle_id = task.strategic_cycle_id or (task.context or {}).get("strategic_cycle_id")
    if cycle_id:
        task_type = task.first_payload.get("type", "")
        failed_phase = "phase1" if task_type == "design_doc_update" else "phase2"
        await strategic_cycle.mark_failure(
            session, cycle_id,
            phase=failed_phase,
            reason=(task.latest_status_message or "task failed")[:200],
        )
    session.add(project)
    logger.warning(
        f"[STRATEGIC] {task.first_payload.get('type')} task {task.id} FAILED "
        f"(failure #{failures}): {(task.latest_status_message or '')[:120]}"
    )


_CREATED_NOTE_PREFIX = "Created by strategic review: "
_RETIRED_NOTE_PREFIX = "Retired by strategic review: "
_RATIONALE_PERSIST_MAX = 200
_CYCLE_REASONING_MAX = 500

# Canonical values the strategic agent is allowed to emit for recommended_mode
# on a direction. Anything else is dropped on persist — the field is optional
# so unknown values never block direction-op application.
_VALID_RECOMMENDED_MODES = frozenset({"audit", "fundamentals", "ship", "pause"})


def _normalize_recommended_mode(value: object) -> str | None:
    """Return a canonical recommended_mode string or None.

    Lower-cases and trims input; rejects values outside _VALID_RECOMMENDED_MODES
    so storage stays bounded to the documented set.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    if cleaned in _VALID_RECOMMENDED_MODES:
        return cleaned
    return None


def _extract_rationale_from_notes(direction: dict | None, prefix: str) -> str | None:
    """Return the latest note rationale matching prefix, or None."""
    if not direction:
        return None
    notes = direction.get("notes") or []
    for note in reversed(notes):
        text = (note.get("text") or "") if isinstance(note, dict) else ""
        if text.startswith(prefix):
            rationale = text[len(prefix):].strip()
            if rationale:
                return rationale[:_RATIONALE_PERSIST_MAX]
    return None


def _collect_rationales(
    project: Project, direction_ids: list, prefix: str
) -> Dict[str, str]:
    """Build a {direction_id: rationale} map by looking up each id's latest note."""
    by_id = {d.get("id"): d for d in (project.directions or []) if d.get("id")}
    result: Dict[str, str] = {}
    for did in direction_ids:
        rationale = _extract_rationale_from_notes(by_id.get(did), prefix)
        if rationale:
            result[did] = rationale
    return result


_PRED_STOPWORDS = {
    "the", "a", "an", "to", "for", "and", "or", "in", "of", "is", "it", "this",
    "that", "with", "by", "we", "should", "be", "are", "on", "as", "feat", "fix",
    "refactor", "add", "update", "improve",
}


def _build_creation_prediction(
    direction: dict, project: Project, calibration_factor: float = 1.0
) -> Dict[str, Any]:
    """Record initial signals available at direction creation time.

    Captures text-length-based difficulty proxy, project system maturity, and
    merge history from lexically similar past directions. Used later by
    _build_previous_decision_outcomes to compute calibration error.

    ``calibration_factor`` scales similar_past_merges to correct for historical
    over/under-prediction bias; computed by _compute_prediction_calibration_factor.
    """
    text = direction.get("text") or ""
    text_len = len(text)
    if text_len < 50:
        estimated_difficulty = "high"
    elif text_len < 150:
        estimated_difficulty = "medium"
    else:
        estimated_difficulty = "low"
    systems = (project.vision or {}).get("systems") or []
    mature = sum(
        1 for s in systems
        if isinstance(s, dict) and s.get("status") in ("functional", "mature")
    )
    words = set(text.lower().split()) - _PRED_STOPWORDS
    similar_past_merges_raw = 0
    similar_count = 0
    own_id = direction.get("id")
    for d in (project.directions or []):
        if d.get("id") == own_id:
            continue
        other_words = set((d.get("text") or "").lower().split()) - _PRED_STOPWORDS
        if len(words) >= 2 and len(words & other_words) >= 3:
            similar_count += 1
            for h in d.get("research_history") or []:
                similar_past_merges_raw += h.get("prs_merged", 0)
            agg = d.get("history_aggregate") or {}
            similar_past_merges_raw += agg.get("total_prs_merged") or 0
    similar_past_merges = round(similar_past_merges_raw * calibration_factor)
    result: Dict[str, Any] = {
        "estimated_difficulty": estimated_difficulty,
        "text_length": text_len,
        "mature_system_count": mature,
        "similar_direction_count": similar_count,
        "similar_past_merges": similar_past_merges,
    }
    if calibration_factor != 1.0:
        result["calibration_factor_applied"] = calibration_factor
    return result


def _build_retirement_metrics(direction: dict) -> Dict[str, Any]:
    """Snapshot final merge/quality metrics at retirement time."""
    history = direction.get("research_history") or []
    agg = direction.get("history_aggregate") or {}
    return {
        "final_quality_score": round(_compute_direction_quality_score(direction), 3),
        "final_prs_merged": sum(h.get("prs_merged", 0) for h in history)
        + (agg.get("total_prs_merged") or 0),
        "final_cycles_active": len(history) + (agg.get("total_cycles") or 0),
    }


def _collect_creation_predictions(
    project: Project, direction_ids: list
) -> Dict[str, Dict[str, Any]]:
    from app.strategic_analysis import _compute_prediction_calibration_factor  # noqa: PLC0415
    calibration_factor = _compute_prediction_calibration_factor(project)
    by_id = {d.get("id"): d for d in (project.directions or []) if d.get("id")}
    result: Dict[str, Dict[str, Any]] = {}
    for did in direction_ids:
        d = by_id.get(did)
        if d is not None:
            result[did] = _build_creation_prediction(d, project, calibration_factor=calibration_factor)
    return result


def _collect_retirement_metrics(
    project: Project, direction_ids: list
) -> Dict[str, Dict[str, Any]]:
    by_id = {d.get("id"): d for d in (project.directions or []) if d.get("id")}
    result: Dict[str, Dict[str, Any]] = {}
    for did in direction_ids:
        d = by_id.get(did)
        if d is not None:
            result[did] = _build_retirement_metrics(d)
    return result


def _extract_cycle_reasoning(task: Task) -> str:
    """Extract a condensed reasoning summary for the completed cycle (≤500 chars).

    Prefers task.plan_output's 'reasoning' or 'summary' field, falling back to
    the task's latest_status_message. Returns empty string if nothing available.
    """
    raw = task.plan_output if isinstance(task.plan_output, dict) else None
    if raw:
        for key in ("reasoning", "summary", "rationale"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:_CYCLE_REASONING_MAX]
    msg = task.latest_status_message or ""
    return msg.strip()[:_CYCLE_REASONING_MAX]


def _check_direction_health(direction: dict) -> str | None:
    """Return a pause reason if this direction should be auto-paused, else None."""
    history = direction.get("research_history") or []

    # Rule 1: Persistent saturation — possibly_saturated AND last 5+ cycles all produced ≤1 task
    if direction.get("possibly_saturated") and len(history) >= 5:
        recent5 = history[-5:]
        if all(entry.get("tasks_created", 0) <= 1 for entry in recent5):
            return "Saturated: last 5 research cycles produced no actionable findings"

    # Rule 2: Zero merge rate — 5+ cycles with 0 PRs merged total, and the most
    # recent cycle has no tasks still in-flight (tasks_created == tasks_completed + tasks_failed)
    if len(history) >= 5:
        total_merged = sum(entry.get("prs_merged", 0) for entry in history)
        if total_merged == 0:
            last = history[-1]
            created = last.get("tasks_created", 0)
            finished = last.get("tasks_completed", 0) + last.get("tasks_failed", 0)
            if created == 0 or finished >= created:
                total_created = sum(entry.get("tasks_created", 0) for entry in history)
                return f"Unproductive: 0 PRs merged from {len(history)} research cycles ({total_created} tasks created)"

    # Rule 3: High CODE failure rate — last 3 cycles have >70% tasks failed
    # for direction-attributable reasons (not infra failures like rate limits,
    # provision failures, or timeouts which are uncorrelated with direction quality).
    if len(history) >= 3:
        recent3 = history[-3:]
        total_created_3 = sum(entry.get("tasks_created", 0) for entry in recent3)
        total_failed_3 = sum(entry.get("tasks_failed", 0) for entry in recent3)
        infra_failed_3 = sum(entry.get("tasks_failed_infra", 0) for entry in recent3)
        code_failed_3 = total_failed_3 - infra_failed_3
        if total_created_3 > 0 and code_failed_3 / total_created_3 > 0.7:
            return f"High code failure rate: >70% of spawned tasks failed for code reasons in last 3 cycles ({code_failed_3}/{total_created_3}, {infra_failed_3} infra failures excluded)"

    # Rule 4: Recent cycle stall — last 3 cycles all either failed as research
    # tasks (crash/timeout/provision) or produced children with ≥1 CODE failed
    # and zero merges. Pauses faster than Rule 2's 5-cycle threshold. Infra
    # failures are excluded from the child-failed check for the same reason
    # as Rule 3: they're uncorrelated with direction quality.
    if len(history) >= 3:
        recent3 = history[-3:]
        def _cycle_failed(e: dict) -> bool:
            if e.get("research_task_failed"):
                return True
            total_failed = e.get("tasks_failed", 0)
            infra_failed = e.get("tasks_failed_infra", 0)
            code_failed = max(0, total_failed - infra_failed)
            return code_failed > 0 and e.get("prs_merged", 0) == 0
        if all(_cycle_failed(e) for e in recent3):
            return "Recent stall: last 3 research cycles all failed or produced no merges"

    return None


# Pause reasons from _check_direction_health whose prefixes indicate
# code-level failure patterns (Rules 3 and 4) — as opposed to Rule 1
# ("Saturated: …") and Rule 2 ("Unproductive: …"), which are direction
# exhaustion and won't benefit from a root-cause investigation.
_CODE_FAILURE_PAUSE_PREFIXES = ("High code failure rate:", "Recent stall:")


def _is_code_failure_pause_reason(pause_reason: str | None) -> bool:
    """True when pause_reason was produced by Rules 3 or 4 in _check_direction_health.

    Used by the direction_lifecycle auto-pause path to decide whether to spawn
    an investigate task: code-failure pauses warrant diagnosis, but saturation
    or zero-merge pauses indicate the direction is exhausted, not broken.
    """
    if not pause_reason:
        return False
    return pause_reason.startswith(_CODE_FAILURE_PAUSE_PREFIXES)


def _build_direction_failure_summary(direction: dict) -> Dict[str, Any]:
    """Aggregate recent failure metrics from a direction's research_history.

    Returns counts over the last 3 cycles — the same window Rules 3 and 4
    inspect — suitable for embedding in an investigate task's context so the
    diagnosis agent sees the failure pattern that triggered the pause.
    """
    history = direction.get("research_history") or []
    recent = history[-3:]
    tasks_created = sum(e.get("tasks_created", 0) for e in recent)
    tasks_failed = sum(e.get("tasks_failed", 0) for e in recent)
    tasks_failed_infra = sum(e.get("tasks_failed_infra", 0) for e in recent)
    tasks_completed = sum(e.get("tasks_completed", 0) for e in recent)
    prs_merged = sum(e.get("prs_merged", 0) for e in recent)
    code_failed = max(0, tasks_failed - tasks_failed_infra)
    code_failure_rate = (code_failed / tasks_created) if tasks_created > 0 else 0.0
    return {
        "cycles_analyzed": len(recent),
        "tasks_created": tasks_created,
        "tasks_completed": tasks_completed,
        "tasks_failed_total": tasks_failed,
        "tasks_failed_code": code_failed,
        "tasks_failed_infra": tasks_failed_infra,
        "prs_merged": prs_merged,
        "code_failure_rate": round(code_failure_rate, 3),
    }


def _check_direction_completion(direction: dict) -> str | None:
    """Return a completion suggestion string if this direction appears successfully complete, else None."""
    history = direction.get("research_history") or []
    if len(history) < 4:
        return None
    total_merged = sum(entry.get("prs_merged", 0) for entry in history)
    if total_merged < 3:
        return None
    recent2 = history[-2:]
    recent_created = sum(e.get("tasks_created", 0) for e in recent2)
    recent_merged = sum(e.get("prs_merged", 0) for e in recent2)
    if recent_created <= 2 and recent_merged <= 1:
        return (
            f"Direction appears complete: {total_merged} PRs merged across "
            f"{len(history)} cycles, last 2 cycles low-yield"
        )
    return None


def _is_direction_duplicate(proposal_text: str, existing_directions: list, rejected_proposals: list) -> bool:
    """Return True if proposal_text overlaps with an existing or recently rejected direction."""
    STOPWORDS = {'the', 'a', 'an', 'to', 'for', 'and', 'or', 'in', 'of', 'is', 'it',
                 'this', 'that', 'with', 'by', 'we', 'should', 'be', 'are', 'on', 'as',
                 'feat', 'fix', 'refactor', 'add', 'update', 'improve'}
    words = set(proposal_text.lower().split()) - STOPWORDS
    if len(words) < 2:
        return False
    for d in existing_directions:
        existing_text = (d.get("text") or "").lower()
        existing_words = set(existing_text.split()) - STOPWORDS
        if len(words & existing_words) >= 4:
            return True
    cutoff = (_utcnow() - timedelta(days=30)).isoformat()
    for entry in rejected_proposals:
        if entry.get("rejected_at", "") < cutoff:
            continue
        rejected_words = set((entry.get("text") or "").lower().split()) - STOPWORDS
        if len(words & rejected_words) >= 4:
            return True
    return False


async def _check_direction_starvation(
    project: Any, dirs_created: int, session: AsyncSession
) -> None:
    """Detect consecutive strategic cycles with no direction creation despite active gaps.

    Increments vision['_direction_starvation_count'] each cycle where dirs_created == 0
    and at least one gap exists (active_count < 3, no product directions, or all saturated).

    When starvation_count >= 2:
      - Creates a strategic alert describing the gap.
      - Sets vision['_strategic_frequency_override'] = 2 to boost cycle frequency.

    When starvation_count >= 5:
      - Auto-creates a fallback direction targeting the detected gap.
      - Resets the starvation counter and clears frequency override.

    Resets both counter and override when dirs_created > 0 (starvation resolved).
    """
    from app.main import _create_strategic_alert

    directions = project.directions or []
    active_count = sum(1 for d in directions if d.get("status") == "active")
    product_count = sum(
        1 for d in directions
        if d.get("status") == "active" and d.get("category") == "product"
    )
    all_saturated = (
        active_count > 0
        and all(d.get("possibly_saturated") for d in directions if d.get("status") == "active")
    )
    has_gaps = active_count < 3 or product_count == 0 or all_saturated

    vision = dict(project.vision or {})
    if dirs_created == 0 and has_gaps:
        starvation_count = vision.get("_direction_starvation_count", 0) + 1
        vision["_direction_starvation_count"] = starvation_count

        if starvation_count >= 5:
            # Auto-create a fallback direction as last resort
            max_active = _runtime_config.get("max_active_directions", 10)
            if active_count < max_active:
                if product_count == 0:
                    gap_type = "NO_PRODUCT_DIRECTIONS"
                    fallback_text = "Deliver user-visible product improvements"
                    fallback_category = "product"
                elif active_count < 3:
                    gap_type = "BELOW_MINIMUM"
                    fallback_text = "Expand system capabilities beyond current directions"
                    fallback_category = None
                else:
                    gap_type = "ALL_SATURATED"
                    fallback_text = "Explore and improve underserved areas of the system"
                    fallback_category = None

                # Audit W2/D8: dedup the fallback against existing directions
                # AND recently-rejected proposals before creating. The previous
                # version skipped dedup, so a saturated project would loop
                # creating near-identical fallbacks every 5 cycles.
                rejected_proposals = list((vision or {}).get("rejected_proposals") or [])
                if _is_direction_duplicate(fallback_text, directions, rejected_proposals):
                    logger.warning(
                        f"[STRATEGIC] Starvation fallback dedup'd for project {project.id}: "
                        f"text '{fallback_text}' overlaps with existing/rejected direction. "
                        f"Resetting starvation counter and alerting operator instead of auto-creating duplicate."
                    )
                    vision["_direction_starvation_count"] = 0
                    project.vision = vision
                    flag_modified(project, "vision")
                    session.add(project)
                    await _create_strategic_alert(
                        project.id, session,
                        f"Direction starvation reached {starvation_count} cycles but the fallback "
                        f"direction '{fallback_text}' would duplicate an existing or recently-rejected "
                        f"direction (gap: {gap_type}). Manual intervention required — review "
                        f"existing directions and propose distinct work, or expand "
                        f"max_active_directions.",
                    )
                    return

                fallback_dir: Dict[str, Any] = {
                    "id": f"d-{uuid.uuid4().hex[:8]}",
                    "text": fallback_text,
                    "status": "active",
                    "priority": "medium",
                    "notes": [{
                        "text": (
                            f"Auto-created by starvation handler after {starvation_count} "
                            f"consecutive cycles with no direction creation"
                        ),
                        "added_at": _utcnow().isoformat(),
                    }],
                    "research_history": [],
                }
                if fallback_category:
                    fallback_dir["category"] = fallback_category

                updated_directions = list(directions) + [fallback_dir]
                project.directions = updated_directions
                flag_modified(project, "directions")

                vision["_direction_starvation_count"] = 0
                vision.pop("_strategic_frequency_override", None)
                project.vision = vision
                flag_modified(project, "vision")
                session.add(project)

                logger.warning(
                    f"[STRATEGIC] Starvation fallback: auto-created direction for project "
                    f"{project.id} after {starvation_count} consecutive empty cycles. "
                    f"gap_type={gap_type}, dir_id={fallback_dir['id']}"
                )
                await _create_strategic_alert(
                    project.id, session,
                    f"Direction starvation fallback: auto-created direction '{fallback_text}' "
                    f"after {starvation_count} consecutive cycles with 0 new directions "
                    f"(gap: {gap_type}).",
                )
            else:
                # Cap reached — cannot auto-create, just log and keep alerting
                project.vision = vision
                flag_modified(project, "vision")
                session.add(project)
                logger.warning(
                    f"[STRATEGIC] Direction starvation for project {project.id}: "
                    f"{starvation_count} cycles, but direction cap ({max_active}) reached — "
                    f"cannot auto-create fallback."
                )
        elif starvation_count >= 2:
            gap_parts = []
            if active_count < 3:
                gap_parts.append(f"only {active_count} active directions (min 3 needed)")
            if product_count == 0:
                gap_parts.append("no product-category directions")
            if all_saturated:
                gap_parts.append("all active directions are saturated")
            gap_desc = "; ".join(gap_parts) or "unspecified gap"
            vision["_strategic_frequency_override"] = 2
            project.vision = vision
            flag_modified(project, "vision")
            await _create_strategic_alert(
                project.id, session,
                f"Direction starvation: {starvation_count} consecutive cycles with 0 new "
                f"directions ({gap_desc}). Boosting strategic frequency.",
            )
            logger.warning(
                f"[STRATEGIC] Direction starvation for project {project.id}: "
                f"{starvation_count} cycles, gap: {gap_desc}. Frequency override → 2."
            )
        else:
            project.vision = vision
            flag_modified(project, "vision")
            logger.info(
                f"[STRATEGIC] Direction starvation cycle {starvation_count}/2 "
                f"for project {project.id} (has_gaps=True, dirs_created=0)"
            )
    else:
        starvation_count = vision.get("_direction_starvation_count", 0)
        has_override = "_strategic_frequency_override" in vision
        if starvation_count > 0 or has_override:
            vision["_direction_starvation_count"] = 0
            vision.pop("_strategic_frequency_override", None)
            project.vision = vision
            flag_modified(project, "vision")
            logger.info(
                f"[STRATEGIC] Direction starvation resolved for project {project.id}: "
                f"{dirs_created} direction(s) created. Counter and override cleared."
            )


async def _handle_strategic_completion(
    task: Task,
    logs: str,
    session: AsyncSession,
    *,
    project: Project | None = None,
):
    """Handle strategic review completion.

    Vision and direction updates were already applied incrementally via POST /api/agent/strategic
    during agent execution. This handler just records final status and checks for issues.
    Falls back to log parsing for backward compatibility if the agent used the old approach.

    Phase C3 / Round 3 C3 + C4: callers in the post-commit chain pre-acquire
    the project lock once at the top of ``_process_post_commit`` via the
    canonical pair-locker ``lock_project_then_task`` (Project before Task)
    and pass the locked Project here as ``project=``. The legacy fallback
    path below (parseable-JSON branch) historically also acquired its own
    lock — that inner acquisition is preserved as a defensive no-op when
    the caller already holds the row (FOR UPDATE SKIP LOCKED returns the
    row to the holding transaction, so the same-session re-lock is a
    cheap re-read).
    """
    # Deferred imports to avoid circular dependency
    from app.main import _create_strategic_alert

    # Phase C3: track whether the caller pre-acquired the project lock so
    # the legacy fallback path below can reuse it instead of re-acquiring.
    caller_holds_lock = project is not None
    if project is None:
        project = await session.get(Project, task.project_id)
    if not project:
        logger.error(f"[STRATEGIC] Task {task.id}: project {task.project_id} not found")
        return

    vision_rev = (project.vision or {}).get("revision", 0)
    active_dirs = sum(1 for d in (project.directions or []) if d.get("status") == "active")

    # Check if the agent used the new incremental API during execution
    task_payload = task.first_payload
    spawned_vision_rev = (task_payload.get("vision") or {}).get("revision", 0)
    tasks_spawned = (task.context or {}).get("_strategic_tasks_spawned", 0)
    api_was_used = (
        (task.context or {}).get("_strategic_vision_set", False)
        or vision_rev > spawned_vision_rev
        or (task.context or {}).get("_strategic_dirs_created", 0) > 0
        or (task.context or {}).get("_strategic_dirs_retired", 0) > 0
        or tasks_spawned > 0
        or (task.context or {}).get("_strategic_api_called", False)
    )

    if api_was_used:
        cycle_id = (task.context or {}).get("strategic_cycle_id")
        dirs_created = (task.context or {}).get("_strategic_dirs_created", 0)
        dirs_retired = (task.context or {}).get("_strategic_dirs_retired", 0)
        created_ids = list((task.context or {}).get("_strategic_created_ids", []))
        retired_ids = list((task.context or {}).get("_strategic_retired_ids", []))
        if cycle_id:
            _vision_updated = (task.context or {}).get("_strategic_vision_set", False) or vision_rev > spawned_vision_rev
            _spawn_suffix = f", {tasks_spawned} tasks spawned" if tasks_spawned else ""
            _summary = f"rev {vision_rev}, {active_dirs} active, {dirs_created} created, {dirs_retired} retired{_spawn_suffix}"
            created_rationales = _collect_rationales(project, created_ids, _CREATED_NOTE_PREFIX)
            retired_rationales = _collect_rationales(project, retired_ids, _RETIRED_NOTE_PREFIX)
            created_predictions = _collect_creation_predictions(project, created_ids)
            retired_final_metrics = _collect_retirement_metrics(project, retired_ids)
            # Audit W2: cycle outcome lives on the StrategicCycle row;
            # per-direction maps land in cycle_metadata.
            await strategic_cycle.mark_phase2_completed(
                session, cycle_id,
                summary_text=_summary,
                cycle_reasoning=_extract_cycle_reasoning(task),
                directions_created=dirs_created,
                directions_retired=dirs_retired,
                tasks_spawned=tasks_spawned,
                vision_updated=_vision_updated,
                extra_metadata={
                    "created_direction_ids": created_ids,
                    "retired_direction_ids": retired_ids,
                    "created_rationales": created_rationales,
                    "retired_rationales": retired_rationales,
                    "created_predictions": created_predictions,
                    "retired_final_metrics": retired_final_metrics,
                },
            )
        await _check_direction_starvation(project, dirs_created, session)
        session.add(project)
        _spawn_suffix_msg = f", {tasks_spawned} tasks spawned" if tasks_spawned else ""
        task.latest_status_message = f"Strategic review complete — vision rev {vision_rev}, {active_dirs} active directions, {dirs_created} created, {dirs_retired} retired{_spawn_suffix_msg}"
        session.add(task)
        # Audit: check for unaddressed FEEDBACK messages
        remaining_feedback_result = await session.execute(
            select(func.count()).select_from(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.status == "open",
                ProjectMessage.message_type == MessageType.FEEDBACK,
            )
        )
        open_feedback_count = remaining_feedback_result.scalar() or 0
        if open_feedback_count > 0:
            logger.warning(
                f"[STRATEGIC] Task {task.id}: {open_feedback_count} FEEDBACK message(s) remain unaddressed after strategic review"
            )
            await _create_strategic_alert(
                task.project_id,
                session,
                f"{open_feedback_count} FEEDBACK message(s) remain unaddressed after strategic review — agent may be ignoring user requests",
            )
        # Per-cycle message post removed: the strategic agent now maintains
        # project.running_update as a living markdown document it rewrites
        # each cycle. Replaces the previous one-message-per-cycle noise.
        await session.commit()
        logger.info(f"[STRATEGIC] Task {task.id} complete via API: vision rev {vision_rev}, {active_dirs} active directions")
        return

    # Fallback: try log-based parsing for backward compatibility
    try:
        parsed = json.loads(logs[:500_000]) if logs else None
    except (json.JSONDecodeError, ValueError):
        parsed = None

    if not parsed or not isinstance(parsed, dict):
        # No API calls and no parseable output — record failure
        project = await session.get(Project, task.project_id)
        if project:
            current_vision = project.vision or {}
            failures = current_vision.get("_strategic_failures", 0) + 1
            current_vision["_strategic_failures"] = failures
            current_vision["_strategic_failures_at"] = _utcnow().isoformat()
            project.vision = current_vision
            flag_modified(project, "vision")
            cycle_id = task.strategic_cycle_id or (task.context or {}).get("strategic_cycle_id")
            if cycle_id:
                # Audit W2: failure recorded on the StrategicCycle row.
                await strategic_cycle.mark_failure(
                    session, cycle_id,
                    phase="phase2",
                    reason="strategic_review unparseable output",
                )
            session.add(project)
        task.latest_status_message = "Strategic output could not be parsed"
        session.add(task)
        await session.commit()
        failure_num = current_vision.get("_strategic_failures", "?") if project else "?"
        logger.warning(f"[STRATEGIC] Task {task.id}: unparseable output (failure #{failure_num})")
        if project:
            await _create_strategic_alert(task.project_id, session,
                f"Strategic review could not parse agent output (failure #{failure_num}, task {str(task.id)[:8]})")
        return

    new_vision = parsed.get("vision")
    new_problem_model = parsed.get("problem_model")
    new_running_update = parsed.get("running_update")
    direction_ops = parsed.get("direction_operations", [])

    # Lock project row to prevent concurrent direction/vision mutations.
    # Phase C3: when the caller pre-acquired the lock (post-commit chain),
    # reuse it directly. Otherwise, acquire via the centralized
    # ``lock_project_for_strategic_transition`` helper.
    if not caller_holds_lock:
        from app.task_locking import lock_project_for_strategic_transition
        project = await lock_project_for_strategic_transition(session, task.project_id)
        if project is None:
            # Helper returns None for both "missing" and "held past retries";
            # disambiguate so we surface the right error.
            exists = (await session.execute(
                select(Project.id).where(Project.id == task.project_id)
            )).first()
            if exists is None:
                logger.error(f"[STRATEGIC] Task {task.id}: project {task.project_id} not found")
                return
            logger.warning(
                f"[STRATEGIC] Task {task.id}: project {task.project_id} held under contention "
                f"after retries; deferring mutation to next cycle"
            )
            await _create_strategic_alert(task.project_id, session,
                f"Strategic completion: project row contended (task {str(task.id)[:8]}) — mutation deferred")
            return

    # Store raw output for admin visibility
    task.plan_output = parsed
    session.add(task)

    # Update vision — merge with current DB state to avoid overwriting concurrent user edits.
    # Audit W2/D7: full 3-way merge (user-bias, keep-both via _rejected_agent_edits ledger)
    # extends to systems/priorities/context lists, not just purpose/success_criteria.
    if new_vision and isinstance(new_vision, dict):
        from app.vision_merge import append_rejected_edits, merge_vision  # noqa: PLC0415
        current_vision = dict(project.vision or {})
        payload_vision_rev = (task.first_payload.get("vision") or {}).get("revision", 0)
        db_vision_rev = current_vision.get("revision", 0)
        # Baseline: prefer the snapshot the cycle row captured at spawn time
        # (StrategicCycle.cycle_metadata.baseline_vision). Falls back to the
        # payload vision if the cycle row is unavailable (legacy task).
        cycle_id = task.strategic_cycle_id or (task.context or {}).get("strategic_cycle_id")
        baseline_vision = task.first_payload.get("vision") or {}
        if cycle_id:
            cycle_row = await strategic_cycle.get(session, cycle_id)
            if cycle_row is not None:
                snap = strategic_cycle.baseline_vision(cycle_row)
                if snap:
                    baseline_vision = snap
        # Always run the 3-way merge — even when no concurrent user edit,
        # it costs nothing and produces a deterministic empty rejected_edits list.
        merged, rejected_edits = merge_vision(
            baseline=baseline_vision,
            agent=new_vision,
            current=current_vision,
        )
        if rejected_edits:
            logger.info(
                f"[STRATEGIC] Vision merge rejected {len(rejected_edits)} agent edit(s) "
                f"in favor of user changes (payload rev {payload_vision_rev}, DB rev {db_vision_rev})"
            )
            append_rejected_edits(merged, rejected_edits)
        new_vision = merged
        new_vision["revision"] = (db_vision_rev or 0) + 1
        new_vision["last_updated"] = _utcnow().isoformat()
        new_vision.pop("_strategic_failures", None)  # Reset failure counter on success
        # Audit W2: cycle history is in the StrategicCycle table; the
        # ``_strategic_cycle_history`` JSONB key is dropped by migration 0047
        # and never re-introduced — the agent's vision payload doesn't carry it.
        # Auto-derive system statuses from direction outcomes
        # Agents often leave everything as "planned" — infer from actual merge/direction data
        vision_systems = new_vision.get("systems")
        if isinstance(vision_systems, list):
            directions_list = project.directions or []
            for sys in vision_systems:
                if not isinstance(sys, dict) or not sys.get("id"):
                    continue
                sys_id = sys["id"]
                # Count directions mapped to this system and their merged PRs
                mapped_dirs = [d for d in directions_list if d.get("system_id") == sys_id]
                total_merged = 0
                for d in mapped_dirs:
                    for h in d.get("research_history", []):
                        total_merged += h.get("prs_merged", 0)
                    agg = d.get("history_aggregate", {})
                    total_merged += agg.get("total_prs_merged", 0)
                # Only upgrade status, never downgrade what the agent set
                agent_status = sys.get("status", "planned")
                STATUS_RANK = {"planned": 0, "in_progress": 1, "functional": 2, "mature": 3}
                agent_rank = STATUS_RANK.get(agent_status, 0)
                if total_merged >= 5 and agent_rank < 2:
                    sys["status"] = "functional"
                elif (total_merged > 0 or len(mapped_dirs) > 0) and agent_rank < 1:
                    sys["status"] = "in_progress"
        project.vision = new_vision
        flag_modified(project, "vision")
        logger.info(f"[STRATEGIC] Updated vision for project {project.id} to revision {new_vision['revision']}")

    # Apply problem_model update — new strategic source of truth running
    # alongside vision during the migration. Failures here are logged and
    # swallowed so a malformed problem_model doesn't block vision / direction
    # ops writes.
    if new_problem_model and isinstance(new_problem_model, dict):
        try:
            from pydantic import ValidationError as _PmValidationError

            from app.problem_model import apply_agent_update
            try:
                new_pm_raw, pm_diff = apply_agent_update(
                    current_raw=project.problem_model,
                    proposed_raw=new_problem_model,
                    now_iso=_utcnow().isoformat(),
                )
            except _PmValidationError as pm_err:
                logger.warning(
                    f"[STRATEGIC] Task {task.id}: problem_model validation failed — "
                    f"keeping prior state. Error: {pm_err}"
                )
            else:
                project.problem_model = new_pm_raw
                flag_modified(project, "problem_model")
                logger.info(
                    f"[STRATEGIC] Updated problem_model for project {project.id} "
                    f"to revision {new_pm_raw.get('revision')} ({pm_diff or 'no diff'})"
                )
        except Exception:
            logger.exception("[STRATEGIC] Failed to apply problem_model update (non-fatal)")

    # Replace project.running_update with the agent's refined markdown
    # document. Unlike the old per-cycle append, the document is a living
    # narrative the agent rewrites each cycle — the user sees a single
    # clean view of current state, not a log of summaries.
    if isinstance(new_running_update, str):
        try:
            project.running_update = new_running_update
            logger.info(
                f"[STRATEGIC] Set running_update for project {project.id} "
                f"(length: {len(new_running_update)} chars)"
            )
        except Exception:
            logger.exception("[STRATEGIC] Failed to set running_update (non-fatal)")

    # Append the agent's self_reflection entry (if any) to the project's
    # self_reflection log. The log drives self-improvement on self-repo
    # projects: recurring complaints about agent limitations become input
    # to future strategic cycles which open self_improvement gaps.
    new_reflection = parsed.get("self_reflection")
    if new_reflection and isinstance(new_reflection, dict):
        try:
            from pydantic import ValidationError as _SrValidationError

            from app.problem_model import SelfReflectionEntry, append_self_reflection
            try:
                entry = SelfReflectionEntry.model_validate({
                    "ts": _utcnow().isoformat(),
                    "cycle_task_id": str(task.id),
                    **new_reflection,
                })
            except _SrValidationError as sr_err:
                logger.warning(
                    f"[STRATEGIC] Task {task.id}: self_reflection validation failed — "
                    f"skipping. Error: {sr_err}"
                )
            else:
                project.self_reflection_log = append_self_reflection(
                    project.self_reflection_log, entry
                )
                flag_modified(project, "self_reflection_log")
                logger.info(
                    f"[STRATEGIC] Appended self_reflection entry for project {project.id} "
                    f"(log size: {len(project.self_reflection_log)})"
                )
        except Exception:
            logger.exception("[STRATEGIC] Failed to append self_reflection (non-fatal)")

    if not new_vision or not isinstance(new_vision, dict):
        # No vision produced — increment failure counter to prevent infinite strategic loops
        current_vision = dict(project.vision or {})
        failures = current_vision.get("_strategic_failures", 0) + 1
        current_vision["_strategic_failures"] = failures
        current_vision["_strategic_failures_at"] = _utcnow().isoformat()
        project.vision = current_vision
        flag_modified(project, "vision")
        logger.warning(f"[STRATEGIC] Task {task.id}: no vision produced (failure #{failures})")
        if failures >= 2:
            await _create_strategic_alert(task.project_id, session,
                f"Strategic review produced no vision ({failures} consecutive failures, task {str(task.id)[:8]})")

    # Process direction operations (capped to prevent flooding)
    MAX_DIRECTION_OPS = 10
    MAX_DIRECTION_CREATES = 5
    ops_applied = 0
    retires_applied = 0
    created_ids_fb: list = []
    retired_ids_fb: list = []
    created_rationales_fb: Dict[str, str] = {}
    retired_rationales_fb: Dict[str, str] = {}
    if direction_ops and isinstance(direction_ops, list):
        if len(direction_ops) > MAX_DIRECTION_OPS:
            logger.warning(f"[STRATEGIC] Task {task.id}: truncating {len(direction_ops)} direction ops to {MAX_DIRECTION_OPS}")
            direction_ops = direction_ops[:MAX_DIRECTION_OPS]
        directions = list(project.directions or [])
        creates_count = 0

        # Deferred import to avoid circular dependency with app.main.
        from app.strategic_constants import (
            STRATEGIC_DIRECTION_TEXT_MAX,
            STRATEGIC_NOTES_HISTORY_MAX,
            STRATEGIC_RATIONALE_MAX,
            STRATEGIC_SYSTEM_ID_MAX,
        )

        for op_entry in direction_ops:
            if not isinstance(op_entry, dict):
                continue
            op = op_entry.get("op")
            rationale = str(op_entry.get("rationale", ""))
            if len(rationale) > STRATEGIC_RATIONALE_MAX:
                logger.warning(f"[STRATEGIC] Task {task.id}: truncating rationale {len(rationale)}→{STRATEGIC_RATIONALE_MAX} chars")
                rationale = rationale[:STRATEGIC_RATIONALE_MAX]

            if op == "create":
                if creates_count >= MAX_DIRECTION_CREATES:
                    continue
                text = op_entry.get("text", "").strip()
                if not text:
                    continue
                if len(text) > STRATEGIC_DIRECTION_TEXT_MAX:
                    logger.warning(f"[STRATEGIC] Task {task.id}: truncating direction text {len(text)}→{STRATEGIC_DIRECTION_TEXT_MAX} chars")
                text = text[:STRATEGIC_DIRECTION_TEXT_MAX]
                # Dedup: skip if an active/proposed direction has very similar text
                text_lower = text.lower()
                if any(d.get("status") in ("active", "proposed") and
                       d.get("text", "").lower().strip() == text_lower
                       for d in directions):
                    logger.info(f"[STRATEGIC] Skipping duplicate direction '{text[:60]}' for project {project.id}")
                    continue
                success_evidence = str(op_entry.get("success_evidence", "")).strip()
                retirement_condition = str(op_entry.get("retirement_condition", "")).strip()
                if not success_evidence or not retirement_condition:
                    logger.warning(
                        f"[STRATEGIC] Task {task.id}: direction create '{text[:60]}' rejected — "
                        f"missing success_evidence or retirement_condition; strategic agent should include both fields"
                    )
                    continue
                raw_priority = op_entry.get("priority", "medium")
                priority = raw_priority if raw_priority in ("high", "medium", "low") else "medium"
                _DIR_FIELD_MAX = 500
                new_dir = {
                    "id": f"d-{uuid.uuid4().hex[:8]}",
                    "text": text,
                    "status": "active",
                    "priority": priority,
                    "notes": [{"text": f"Created by strategic review: {rationale}", "added_at": _utcnow().isoformat()}] if rationale else [],
                    "research_history": [],
                    "context": {
                        "success_evidence": success_evidence[:_DIR_FIELD_MAX],
                        "retirement_condition": retirement_condition[:_DIR_FIELD_MAX],
                    },
                }
                impl_constraints = str(op_entry.get("implementation_constraints", "")).strip()
                if impl_constraints:
                    new_dir["context"]["implementation_constraints"] = impl_constraints[:_DIR_FIELD_MAX]
                if op_entry.get("source"):
                    new_dir["source"] = str(op_entry["source"])[:64]
                if op_entry.get("paths"):
                    new_dir["paths"] = op_entry["paths"]
                raw_category = op_entry.get("category")
                if raw_category in ("product", "structural", "mixed"):
                    new_dir["category"] = raw_category
                if op_entry.get("system_id"):
                    sid = str(op_entry["system_id"])
                    if len(sid) > STRATEGIC_SYSTEM_ID_MAX:
                        logger.warning(f"[STRATEGIC] Task {task.id}: truncating system_id {len(sid)}→{STRATEGIC_SYSTEM_ID_MAX} chars")
                    new_dir["system_id"] = sid[:STRATEGIC_SYSTEM_ID_MAX]
                if op_entry.get("category") and op_entry["category"] in ("product", "structural"):
                    new_dir["category"] = op_entry["category"]
                # Optional mode hint from the strategic agent — persisted only
                # when canonical (audit | fundamentals | ship | pause).
                rec_mode = _normalize_recommended_mode(op_entry.get("recommended_mode"))
                if rec_mode:
                    new_dir["recommended_mode"] = rec_mode
                    new_dir[RECOMMENDED_MODE_META_KEY] = {
                        "mode": rec_mode,
                        "source": RECOMMENDED_MODE_SOURCE_STRATEGIC,
                        "source_task_id": str(task.id),
                        "set_at": _utcnow().isoformat(),
                        "strategic_cycle_id": str(task.strategic_cycle_id or (task.context or {}).get("strategic_cycle_id") or ""),
                    }
                current_active = sum(1 for d in directions if d.get('status') == 'active')
                if current_active >= _runtime_config.get('max_active_directions', 5):
                    logger.warning(f'[STRATEGIC] Skipping direction create: active count {current_active} >= cap')
                    continue
                # Translate recommended_mode='pause' to paused status so the
                # existing spawn-skip (status != 'active') mechanism handles it.
                if rec_mode == "pause":
                    new_dir["status"] = "paused"
                    new_dir["auto_paused"] = True
                    new_dir["paused_reason"] = "Strategic review: recommended_mode=pause"
                    new_dir["paused_at"] = _utcnow().isoformat()
                directions.append(new_dir)
                ops_applied += 1
                creates_count += 1
                created_ids_fb.append(new_dir["id"])
                if rationale:
                    created_rationales_fb[new_dir["id"]] = rationale[:_RATIONALE_PERSIST_MAX]
                logger.info(f"[STRATEGIC] Created direction '{text[:60]}' for project {project.id}")

            elif op == "update":
                dir_id = op_entry.get("direction_id")
                changes = op_entry.get("changes", {})
                if not dir_id or not changes:
                    continue
                for d in directions:
                    if d.get("id") == dir_id:
                        if "text" in changes:
                            new_text = str(changes["text"])
                            if len(new_text) > STRATEGIC_DIRECTION_TEXT_MAX:
                                logger.warning(f"[STRATEGIC] Task {task.id}: truncating direction text {len(new_text)}→{STRATEGIC_DIRECTION_TEXT_MAX} chars")
                            d["text"] = new_text[:STRATEGIC_DIRECTION_TEXT_MAX]
                        if "priority" in changes and changes["priority"] in ("high", "medium", "low"):
                            d["priority"] = changes["priority"]
                        if "paths" in changes:
                            d["paths"] = changes["paths"]
                        if "system_id" in changes:
                            if changes["system_id"]:
                                sid_new = str(changes["system_id"])
                                if len(sid_new) > STRATEGIC_SYSTEM_ID_MAX:
                                    logger.warning(f"[STRATEGIC] Task {task.id}: truncating system_id {len(sid_new)}→{STRATEGIC_SYSTEM_ID_MAX} chars")
                                d["system_id"] = sid_new[:STRATEGIC_SYSTEM_ID_MAX]
                            else:
                                d["system_id"] = None
                        if "status" in changes and changes["status"] in ("active", "paused", "expired"):
                            # Block reactivating a direction the user manually paused
                            if changes["status"] == "active" and d.get("status") == "paused" and not d.get("auto_paused"):
                                logger.warning(f"[STRATEGIC] Blocked reactivation of user-paused direction {dir_id}")
                            else:
                                d["status"] = changes["status"]
                        if "category" in changes:
                            if changes["category"] in ("product", "structural", "mixed"):
                                d["category"] = changes["category"]
                            elif changes["category"] is None:
                                d.pop("category", None)
                        if "recommended_mode" in changes:
                            new_mode = _normalize_recommended_mode(changes["recommended_mode"])
                            if new_mode:
                                d["recommended_mode"] = new_mode
                                d[RECOMMENDED_MODE_META_KEY] = {
                                    "mode": new_mode,
                                    "source": RECOMMENDED_MODE_SOURCE_STRATEGIC,
                                    "source_task_id": str(task.id),
                                    "set_at": _utcnow().isoformat(),
                                    "strategic_cycle_id": str(task.strategic_cycle_id or (task.context or {}).get("strategic_cycle_id") or ""),
                                }
                            elif changes["recommended_mode"] is None:
                                d.pop("recommended_mode", None)
                                d.pop(RECOMMENDED_MODE_META_KEY, None)
                        # Also accept top-level recommended_mode on the
                        # operation so the agent can emit it without
                        # nesting it inside a changes block.
                        top_level_mode = _normalize_recommended_mode(op_entry.get("recommended_mode"))
                        if top_level_mode:
                            d["recommended_mode"] = top_level_mode
                            d[RECOMMENDED_MODE_META_KEY] = {
                                "mode": top_level_mode,
                                "source": RECOMMENDED_MODE_SOURCE_STRATEGIC,
                                "source_task_id": str(task.id),
                                "set_at": _utcnow().isoformat(),
                                "strategic_cycle_id": str(task.strategic_cycle_id or (task.context or {}).get("strategic_cycle_id") or ""),
                            }
                        # Translate recommended_mode='pause' to paused direction
                        # status so existing spawn-skip logic handles it.
                        if d.get("recommended_mode") == "pause" and d.get("status") == "active":
                            d["status"] = "paused"
                            d["auto_paused"] = True
                            d["paused_reason"] = "Strategic review: recommended_mode=pause"
                            d["paused_at"] = _utcnow().isoformat()
                        # Record rationale as a note
                        if rationale:
                            notes = d.get("notes", [])
                            notes.append({"text": f"Strategic update: {rationale}", "added_at": _utcnow().isoformat()})
                            if len(notes) > STRATEGIC_NOTES_HISTORY_MAX:
                                logger.warning(f"[STRATEGIC] Task {task.id}: dropping {len(notes) - STRATEGIC_NOTES_HISTORY_MAX} oldest notes on direction {dir_id}")
                            d["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                        ops_applied += 1
                        logger.info(f"[STRATEGIC] Updated direction {dir_id} for project {project.id}: {list(changes.keys())}")
                        break

            elif op == "retire":
                dir_id = op_entry.get("direction_id")
                if not dir_id:
                    continue
                for d in directions:
                    if d.get("id") == dir_id:
                        d["status"] = "expired"
                        if rationale:
                            notes = d.get("notes", [])
                            notes.append({"text": f"Retired by strategic review: {rationale}", "added_at": _utcnow().isoformat()})
                            if len(notes) > STRATEGIC_NOTES_HISTORY_MAX:
                                logger.warning(f"[STRATEGIC] Task {task.id}: dropping {len(notes) - STRATEGIC_NOTES_HISTORY_MAX} oldest notes on direction {dir_id}")
                            d["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                        ops_applied += 1
                        retires_applied += 1
                        retired_ids_fb.append(dir_id)
                        if rationale:
                            retired_rationales_fb[dir_id] = rationale[:_RATIONALE_PERSIST_MAX]
                        logger.info(f"[STRATEGIC] Retired direction {dir_id} for project {project.id}")
                        break

        if ops_applied > 0:
            project.directions = directions
            flag_modified(project, "directions")

    # Update cycle history with final outcome
    # Audit W2: cycle outcome lives on the StrategicCycle row.
    cycle_id = task.strategic_cycle_id or (task.context or {}).get("strategic_cycle_id")
    dirs_created = creates_count if direction_ops and isinstance(direction_ops, list) else 0
    if cycle_id:
        dirs_retired = retires_applied
        vision_produced = bool(new_vision and isinstance(new_vision, dict))
        _active_dirs_fb = sum(1 for d in (project.directions or []) if d.get("status") == "active")
        _vision_rev_fb = (project.vision or {}).get("revision", 0)
        _summary_fb = f"rev {_vision_rev_fb}, {_active_dirs_fb} active, {dirs_created} created, {dirs_retired} retired"
        if vision_produced:
            await strategic_cycle.mark_phase2_completed(
                session, cycle_id,
                summary_text=_summary_fb,
                cycle_reasoning=_extract_cycle_reasoning(task),
                directions_created=dirs_created,
                directions_retired=dirs_retired,
                vision_updated=True,
                extra_metadata={
                    "created_direction_ids": list(created_ids_fb),
                    "retired_direction_ids": list(retired_ids_fb),
                    "created_rationales": dict(created_rationales_fb),
                    "retired_rationales": dict(retired_rationales_fb),
                    "created_predictions": _collect_creation_predictions(project, created_ids_fb),
                    "retired_final_metrics": _collect_retirement_metrics(project, retired_ids_fb),
                },
            )
        else:
            await strategic_cycle.mark_failure(
                session, cycle_id, phase="phase2",
                reason="strategic_review fallback path produced no vision",
            )

    await _check_direction_starvation(project, dirs_created, session)
    session.add(project)

    # Deferred import of message/resolution caps — same STRATEGIC_* namespace
    from app.strategic_constants import STRATEGIC_MESSAGE_CONTENT_MAX, STRATEGIC_RESOLUTION_MAX

    # Process messages from agent (questions, blockers, notes)
    msgs_created = 0
    for msg_entry in (parsed.get("messages") or [])[:5]:
        if not isinstance(msg_entry, dict):
            continue
        content = (msg_entry.get("content") or "").strip()
        if not content:
            continue
        if len(content) > STRATEGIC_MESSAGE_CONTENT_MAX:
            logger.warning(f"[STRATEGIC] Task {task.id}: truncating message content {len(content)}→{STRATEGIC_MESSAGE_CONTENT_MAX} chars")
            content = content[:STRATEGIC_MESSAGE_CONTENT_MAX]
        try:
            msg_type = MessageType(msg_entry.get("type", "NOTE").upper())
        except ValueError:
            msg_type = MessageType.NOTE
        new_msg = ProjectMessage(
            project_id=project.id,
            message_type=msg_type,
            from_type="strategic",
            from_task_id=task.id,
            content=content,
            context=msg_entry.get("context") or {},
        )
        session.add(new_msg)
        msgs_created += 1

    # Resolve messages the agent addressed
    msgs_resolved = 0
    # Support both formats: resolved_messages (with resolution text) and resolved_message_ids (legacy)
    resolved_entries = parsed.get("resolved_messages") or []
    if not resolved_entries:
        # Fall back to legacy format (plain ID list)
        resolved_entries = [{"id": mid} for mid in (parsed.get("resolved_message_ids") or [])]
    for entry in resolved_entries[:10]:
        try:
            msg_id = entry.get("id") if isinstance(entry, dict) else str(entry)
            resolution_text = entry.get("resolution", "Addressed by strategic review") if isinstance(entry, dict) else "Addressed by strategic review"
            msg = await session.get(ProjectMessage, uuid.UUID(str(msg_id)))
            if msg and msg.project_id == project.id and msg.status == "open":
                msg.status = "resolved"
                if resolution_text:
                    rt = str(resolution_text)
                    if len(rt) > STRATEGIC_RESOLUTION_MAX:
                        logger.warning(f"[STRATEGIC] Task {task.id}: truncating resolution {len(rt)}→{STRATEGIC_RESOLUTION_MAX} chars")
                    msg.resolution = rt[:STRATEGIC_RESOLUTION_MAX]
                else:
                    msg.resolution = "Addressed by strategic review"
                msg.resolved_at = _utcnow()
                session.add(msg)
                msgs_resolved += 1
                if msg.message_type == MessageType.FEEDBACK:
                    try:
                        from app.notification_dispatch import post_feedback_acknowledgment
                        await post_feedback_acknowledgment(
                            session, project.id, msg.id,
                            msg.resolution or "Addressed by strategic review",
                            task.id,
                        )
                    except Exception as _e:
                        logger.warning(f"[NOTIFY] Failed to post feedback acknowledgment: {_e}")
        except (ValueError, TypeError):
            pass

    # Audit: check for unaddressed FEEDBACK messages
    remaining_feedback_result = await session.execute(
        select(func.count()).select_from(ProjectMessage).where(
            ProjectMessage.project_id == project.id,
            ProjectMessage.status == "open",
            ProjectMessage.message_type == MessageType.FEEDBACK,
        )
    )
    open_feedback_count = remaining_feedback_result.scalar() or 0
    if open_feedback_count > 0:
        logger.warning(
            f"[STRATEGIC] Task {task.id}: {open_feedback_count} FEEDBACK message(s) remain unaddressed after strategic review"
        )
        await _create_strategic_alert(
            task.project_id,
            session,
            f"{open_feedback_count} FEEDBACK message(s) remain unaddressed after strategic review — agent may be ignoring user requests",
        )

    vision_rev = (project.vision or {}).get("revision", "?")
    # Per-cycle message post removed: the strategic agent now maintains
    # project.running_update as a living markdown document it rewrites each
    # cycle; the user acknowledges to stamp a read-marker.
    task.latest_status_message = f"Strategic review complete — vision rev {vision_rev}, {ops_applied} direction ops, {msgs_created} messages, {msgs_resolved} resolved"
    session.add(task)
    await session.commit()  # Single commit: vision + directions + messages + task status
    logger.info(f"[STRATEGIC] Task {task.id} complete: vision rev {vision_rev}, {ops_applied} direction ops, {msgs_created} msgs created, {msgs_resolved} resolved")
