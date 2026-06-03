"""Strategic completion handler and direction health checks.

Extracted from app.task_completion to keep that module focused on plan/security/perf
completion handlers. Contains direction health/completion checks, duplicate detection,
and the _handle_strategic_completion handler.
"""

import hashlib
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
    TaskStatus,
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
            exists = (
                await session.execute(
                    select(Project.id).where(Project.id == task.project_id)
                )
            ).first()
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
        failure_category = task.first_payload.get("failure_category", "")
        outcome_override = (
            strategic_cycle.OUTCOME_THINKER_ARTIFACTS_MISSING
            if failure_category == "strategic_thinker_artifacts_missing"
            else None
        )
        await strategic_cycle.mark_failure(
            session,
            cycle_id,
            phase=failed_phase,
            reason=(task.latest_status_message or "task failed")[:200],
            outcome_override=outcome_override,
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


_VALID_VERSION_PHASES = frozenset({"build", "stabilize"})

# Cap on the persisted reasoning string carried with a version_phase_decision.
# Matches release_version._PHASE_REASONING_MAX so the value the endpoint stores
# in task.context and the value release_version persists agree on bounds.
_VPD_REASONING_MAX = 500

# The transient task.context key that carries an agent-emitted
# version_phase_decision from the set_problem_model endpoint to the
# strategic-completion handler's API path.  Single source of truth so the
# producer (main.strategic_operation) and consumer (_handle_strategic_completion)
# never drift.
VERSION_PHASE_DECISION_CONTEXT_KEY = "_strategic_version_phase_decision"

# Cap on the qualify_request reason string the endpoint stows in task.context.
# Advisory only (logged, not stored on ReleaseVersion), so a generous cap is fine.
_QUALIFY_REQUEST_REASON_MAX = 500

# Transient task.context key carrying an agent-emitted qualify_request from the
# set_problem_model endpoint to _handle_strategic_completion's API path. Mirrors
# VERSION_PHASE_DECISION_CONTEXT_KEY — one source of truth so producer + consumer
# never drift.
QUALIFY_REQUEST_CONTEXT_KEY = "_strategic_qualify_request"

# OPTIONAL agent verdict that refines the deterministic outcome-progress
# (scaffolding) detector. For each long-running ACTIVE direction the strategic
# agent may record one verdict — advancing | stalled | met — that raises the
# detector's precision: advancing/met SUPPRESS the advisory scaffolding flag
# (the agent confirms genuine progress toward success_evidence), stalled
# reinforces it. An OMITTED verdict changes nothing, so the deterministic
# detector remains the guaranteed backstop. main.strategic_operation stashes the
# cleaned list under EVIDENCE_PROGRESS_CONTEXT_KEY; _apply_evidence_progress_verdicts
# persists it onto each matching direction here.
_VALID_EVIDENCE_PROGRESS = frozenset({"advancing", "stalled", "met"})
_EVIDENCE_PROGRESS_SUPPRESSING = frozenset({"advancing", "met"})
_EVIDENCE_PROGRESS_RATIONALE_MAX = 300
EVIDENCE_PROGRESS_CONTEXT_KEY = "_strategic_evidence_progress"


def _normalize_version_phase(value: object) -> str | None:
    """Return a canonical version phase string or None.

    Lower-cases and trims input; rejects values outside _VALID_VERSION_PHASES
    so storage stays bounded to the documented set (build | stabilize).
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    if cleaned in _VALID_VERSION_PHASES:
        return cleaned
    return None


def extract_version_phase_decision(raw: object) -> dict | None:
    """Validate + normalize a raw ``version_phase_decision`` value.

    Accepts the value the strategic agent embeds as a top-level sibling in the
    ``set-problem-model`` payload — shape ``{phase, reasoning}`` — and returns a
    clean ``{"phase": <build|stabilize>, "reasoning": <str>}`` dict, or ``None``
    when the input is absent or unusable.

    Returning a *sanitized* dict (canonical phase, trimmed/bounded reasoning)
    rather than the raw value means the transient blob stored in ``task.context``
    is already validated — ``_apply_version_phase_decision`` re-validates anyway,
    so a bad value is defensively no-op'd, never raised.

    Used by the ``set_problem_model`` endpoint to lift the field out of the
    problem_model JSON *before* ``ProblemModel`` validation (which is
    ``extra="forbid"`` and would otherwise reject the whole update).
    """
    if not isinstance(raw, dict):
        return None
    phase = _normalize_version_phase(raw.get("phase"))
    if not phase:
        return None
    reasoning_raw = raw.get("reasoning")
    reasoning = (
        reasoning_raw.strip()[:_VPD_REASONING_MAX]
        if isinstance(reasoning_raw, str)
        else ""
    )
    return {"phase": phase, "reasoning": reasoning}


def extract_qualify_request(raw: object) -> dict | None:
    """Validate + normalize a raw ``qualify_request`` value.

    Accepts the {reason} value the strategic agent embeds as a top-level sibling
    in the ``set-problem-model`` payload and returns a clean ``{"reason": str}``
    dict, or ``None`` when the input is absent or unusable. A qualify_request is
    the agent's transient signal that an ``open`` version in ``stabilize`` is
    release-ready and the heavy qualifier should run (automain-lts-plan-v1.md
    Phase 3) — never durable model state.

    Used by the ``set_problem_model`` endpoint to lift the field out of the
    problem_model JSON *before* ``ProblemModel`` validation (``extra="forbid"``,
    which would otherwise reject the whole update). ``_apply_qualify_request``
    re-checks the phase gate, so a wrong-phase request is no-op'd, never raised.
    """
    if not isinstance(raw, dict):
        return None
    reason_raw = raw.get("reason")
    reason = (
        reason_raw.strip()[:_QUALIFY_REQUEST_REASON_MAX]
        if isinstance(reason_raw, str)
        else ""
    )
    return {"reason": reason}


async def _apply_version_phase_decision(
    decision: object,
    task: Task,
    project: Project,
    session: AsyncSession,
    *,
    path: str,
) -> None:
    """Parse and apply an optional ``version_phase_decision`` from the strategic agent.

    ``decision`` is the raw value from either:
      - the fallback parse path: ``parsed.get("version_phase_decision")``
      - the API path: ``task.context.get("_strategic_version_phase_decision")``

    Expected shape: ``{phase: "build"|"stabilize", reasoning: str}``.

    When a valid decision is present:
      1. Looks up the project's current open ``ReleaseVersion``.
      2. Calls ``release_version.set_version_phase`` which stamps ``phase``,
         ``phase_changed_at``, and ``phase_reasoning`` on the row.
      3. ``session.add``s the row — caller's surrounding ``await session.commit()``
         persists it.

    Silently no-ops when:
      - ``decision`` is None / not a dict
      - ``phase`` is missing or not in ``{build, stabilize}``
      - The project has no open ``ReleaseVersion`` (pre-Phase-2a, or no version opened yet)

    All errors are logged and swallowed — a bad or absent phase decision must never
    block vision/direction writes.
    """
    if not decision or not isinstance(decision, dict):
        return
    phase = _normalize_version_phase(decision.get("phase"))
    if not phase:
        raw = decision.get("phase")
        logger.warning(
            "[STRATEGIC] Task %s (%s path): version_phase_decision has invalid/missing "
            "phase=%r — skipping",
            task.id,
            path,
            raw,
        )
        return
    reasoning_raw = decision.get("reasoning")
    reasoning: str | None = (
        reasoning_raw.strip() or None if isinstance(reasoning_raw, str) else None
    )
    try:
        from app import release_version as _rv

        open_ver = await _rv.current_open_version(session, project.id)
        if open_ver is None:
            logger.debug(
                "[STRATEGIC] Task %s (%s path): no open ReleaseVersion for project %s "
                "— skipping version_phase_decision",
                task.id,
                path,
                project.id,
            )
            return
        _rv.set_version_phase(open_ver, phase, reasoning=reasoning)
        session.add(open_ver)
        logger.info(
            "[STRATEGIC] Task %s (%s path): applied version_phase_decision "
            "phase=%s for project %s version v%d",
            task.id,
            path,
            phase,
            project.id,
            open_ver.version_number,
        )
    except Exception:
        logger.exception(
            "[STRATEGIC] Task %s (%s path): failed to apply version_phase_decision "
            "(non-fatal)",
            task.id,
            path,
        )


async def _apply_qualify_request(
    request: object,
    task: Task,
    project: Project,
    session: AsyncSession,
    *,
    path: str,
) -> None:
    """Parse and apply an optional ``qualify_request`` from the strategic agent.

    ``request`` is the raw {reason: str} value from either:
      - the API path: ``task.context[QUALIFY_REQUEST_CONTEXT_KEY]``
      - the fallback / log-parse path: ``parsed.get("qualify_request")``

    It is the agent's signal that an ``open`` version in ``stabilize`` is
    release-ready (automain-lts-plan-v1.md Phase 3). When valid, looks up the
    open ``ReleaseVersion``, gates on
    ``phase == "stabilize"``, and calls ``release_qualifier.run_qualification``
    (which snapshots the candidate SHA + spawns the audit/smoketest children;
    the caller's surrounding commit persists its version-row writes).

    Silently no-ops when ``request`` is None/not a dict, there is no open
    ``ReleaseVersion``, or that version is not in ``stabilize``. All errors are
    logged and swallowed — a bad or absent request must never block the write.
    """
    if not request or not isinstance(request, dict):
        return
    reason_raw = request.get("reason")
    reason = reason_raw.strip() or None if isinstance(reason_raw, str) else None
    try:
        from app import release_qualifier as _rq
        from app import release_version as _rv

        open_ver = await _rv.current_open_version(session, project.id)
        if open_ver is None:
            logger.debug(
                "[STRATEGIC] Task %s (%s path): no open ReleaseVersion for project "
                "%s — skipping qualify_request",
                task.id,
                path,
                project.id,
            )
            return
        if open_ver.phase != "stabilize":
            logger.info(
                "[STRATEGIC] Task %s (%s path): qualify_request for project %s "
                "version v%d ignored — version is in phase=%s, not stabilize",
                task.id,
                path,
                project.id,
                open_ver.version_number,
                open_ver.phase,
            )
            return
        result = await _rq.run_qualification(session, project.id, open_ver)
        logger.info(
            "[STRATEGIC] Task %s (%s path): qualify_request ran qualification "
            "for project %s version v%d — status=%s (reason=%r)",
            task.id,
            path,
            project.id,
            open_ver.version_number,
            (result or {}).get("status"),
            reason,
        )
    except Exception:
        logger.exception(
            "[STRATEGIC] Task %s (%s path): failed to apply qualify_request "
            "(non-fatal)",
            task.id,
            path,
        )


def _extract_rationale_from_notes(direction: dict | None, prefix: str) -> str | None:
    """Return the latest note rationale matching prefix, or None."""
    if not direction:
        return None
    notes = direction.get("notes") or []
    for note in reversed(notes):
        text = (note.get("text") or "") if isinstance(note, dict) else ""
        if text.startswith(prefix):
            rationale = text[len(prefix) :].strip()
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
    "the",
    "a",
    "an",
    "to",
    "for",
    "and",
    "or",
    "in",
    "of",
    "is",
    "it",
    "this",
    "that",
    "with",
    "by",
    "we",
    "should",
    "be",
    "are",
    "on",
    "as",
    "feat",
    "fix",
    "refactor",
    "add",
    "update",
    "improve",
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
        1
        for s in systems
        if isinstance(s, dict) and s.get("status") in ("functional", "mature")
    )
    words = set(text.lower().split()) - _PRED_STOPWORDS
    similar_past_merges_raw = 0
    similar_count = 0
    own_id = direction.get("id")
    for d in project.directions or []:
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
    from app.strategic_analysis import _compute_prediction_calibration_factor

    calibration_factor = _compute_prediction_calibration_factor(project)
    by_id = {d.get("id"): d for d in (project.directions or []) if d.get("id")}
    result: Dict[str, Dict[str, Any]] = {}
    for did in direction_ids:
        d = by_id.get(did)
        if d is not None:
            result[did] = _build_creation_prediction(
                d, project, calibration_factor=calibration_factor
            )
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


async def _spawn_retirement_memos(
    session: AsyncSession,
    project: Project,
    task: Task,
    retired_ids: list,
    rationales: dict,
) -> int:
    """Spawn one memo task per newly-retired direction as a durable citable artifact.

    The memo preserves the retirement rationale so strategic-cycle citation
    queries can reference it.  Dedup guard: skips any direction that already
    has a retirement memo in the last 90 days.
    """
    if not retired_ids:
        return 0
    from app.payload_schemas import validate_internal_payload
    from app.plan_completion import TASK_PRIORITY_MAP
    from app.task_completion import _apply_synthesized_memo

    ninety_days_ago = _utcnow() - timedelta(days=90)
    existing_rows = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.created_at >= ninety_days_ago,
                    Task.created_by == "ORCHESTRATOR",
                )
            )
        )
        .scalars()
        .all()
    )
    already_memed: set[str] = {
        str(t.context.get("memo_source_direction_id"))
        for t in existing_rows
        if isinstance(t.context, dict)
        and t.context.get("memo_reasoning_kind") == "direction_retirement"
        and t.context.get("memo_source_direction_id")
    }

    by_id = {d.get("id"): d for d in (project.directions or []) if d.get("id")}
    spawned = 0
    for did in retired_ids:
        if did in already_memed:
            logger.debug(
                "[STRATEGIC] Skipping duplicate retirement memo for direction %s", did
            )
            continue
        direction = by_id.get(did)
        direction_text = (direction.get("text") or "") if direction else ""
        rationale = rationales.get(did) or ""
        topic = direction_text[:120] or f"Direction {did}"
        memo_payload = validate_internal_payload(
            {
                "type": "memo",
                "topic": f"Direction retired: {topic}",
                "reasoning_kind": "direction_retirement",
                "direction_id": did,
                **({"retirement_rationale": rationale} if rationale else {}),
            },
            "strategic_completion:memo:direction_retirement",
        )
        memo_task = Task(
            project_id=project.id,
            title=f"Memo: direction retired — {topic[:100]}",
            description="Reasoning memo for retired direction",
            parent_task_id=task.id,
            payloads=[memo_payload],
            context={
                "memo_reasoning_kind": "direction_retirement",
                "memo_source_direction_id": did,
                "memo_source_task_id": str(task.id),
                **({"retirement_rationale": rationale} if rationale else {}),
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            priority=TASK_PRIORITY_MAP.get("low", 0),
        )
        session.add(memo_task)
        # Synthesize the memo artifact at spawn time when rationale is available
        # so the task is immediately COMPLETED and citable without provisioning a VM.
        synthesized = False
        if rationale:
            parts = [f"Retirement rationale: {rationale}"]
            if direction_text:
                parts.insert(0, f"Direction: {direction_text}")
            synth_memo = {
                "summary": rationale[:200],
                "reasoning": "\n\n".join(parts),
                "citations": [f"direction:{did}", f"task:{task.id}"],
                "tags": ["direction-retirement"],
            }
            _apply_synthesized_memo(memo_task, synth_memo, session)
            synthesized = True
        spawned += 1
        logger.info(
            "[STRATEGIC] Spawned retirement memo task %s for direction %s (synthesized=%s)",
            memo_task.id,
            did,
            synthesized,
        )
    return spawned


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


_FINDINGS_PROMPT_MAX_ROWS = 10
_FINDINGS_DESCRIPTION_MAX = 120
_FINDINGS_CLAIM_MAX = 3
_FINDINGS_SOURCE_MAX = 2


def format_findings_for_prompt(
    rows: list, max_rows: int = _FINDINGS_PROMPT_MAX_ROWS
) -> str:
    """Format query_findings_index rows as a compact prompt block.

    Returns a newline-separated string with one finding per line: grade, title,
    truncated description, top claims, and source refs.  Empty string when rows
    is empty.  Suitable for embedding in a strategic synthesis prompt.
    """
    if not rows:
        return ""
    lines: list[str] = []
    for r in rows[:max_rows]:
        grade = r.get("actionability_grade") or "MEDIUM"
        title = r.get("finding_title") or ""
        desc = (r.get("finding_description") or "")[:_FINDINGS_DESCRIPTION_MAX]
        claims = [str(c) for c in (r.get("claims") or [])[:_FINDINGS_CLAIM_MAX]]
        sources = r.get("sources") or []
        refs = [
            s.get("ref", "")
            for s in sources[:_FINDINGS_SOURCE_MAX]
            if isinstance(s, dict) and s.get("ref")
        ]
        line = f"[{grade}] {title}: {desc}"
        if claims:
            line += f" | claims: {'; '.join(claims)}"
        if refs:
            line += f" | sources: {', '.join(refs)}"
        lines.append(line)
    return "\n".join(lines)


def _compute_churn_metrics(direction: dict) -> dict | None:
    """Return windowed churn metrics for a direction, or None if history is insufficient.

    Counts finished tasks that did not result in a merged PR over the rolling
    window: completed-but-unmerged tasks plus code failures. Infra failures are
    excluded because they are uncorrelated with direction quality. The count is
    operator-facing (pause reason string and churn badge), so it reports the real
    non-merging finished-task volume with no baseline adjustment. research/
    investigate/memo children that legitimately finish without a PR are absorbed
    conservatively by the pause threshold instead.
    """
    history = direction.get("research_history") or []
    churn_window = int(_runtime_config.get("direction_churn_window_cycles", 5) or 5)
    if len(history) < 3 or churn_window <= 0:
        return None
    window = history[-churn_window:]
    merged = sum(e.get("prs_merged", 0) for e in window)
    tasks_complete = sum(e.get("tasks_completed", 0) for e in window)
    # Children that COMPLETED without opening a PR because the deliverable already
    # existed ("no changes needed") are correct no-ops, not churn. Subtract them so
    # a direction whose features are already built does not look like it is burning
    # tasks. Field absent on pre-existing entries → defaults to 0 (unchanged behavior).
    no_pr_completions = sum(e.get("tasks_completed_no_pr", 0) for e in window)
    code_failed = sum(
        max(0, e.get("tasks_failed", 0) - e.get("tasks_failed_infra", 0))
        for e in window
    )
    non_merge_finished = max(
        0, (tasks_complete - no_pr_completions) + code_failed - merged
    )
    total_finished = merged + non_merge_finished
    merge_rate = merged / total_finished if total_finished > 0 else 0.0
    return {
        "window_cycles": len(window),
        "merged": merged,
        "non_merge_finished": non_merge_finished,
        "merge_rate": round(merge_rate, 3),
    }


def _compute_lifetime_burn_metrics(direction: dict) -> dict | None:
    """Return lifetime/cumulative non-merge burn metrics combining research_history and history_aggregate.

    Uses the same non-merge definition as _compute_churn_metrics (tasks_completed +
    code_failed - prs_merged) but across the direction's entire lifetime. Returns
    None when there is no meaningful lifetime data.
    """
    history = direction.get("research_history") or []
    agg = direction.get("history_aggregate") or {}

    window_merged = sum(e.get("prs_merged", 0) for e in history)
    window_completed = sum(e.get("tasks_completed", 0) for e in history)
    # Correct no-op completions ("deliverable already exists", no PR) are not burn.
    # Subtract them across both the rolling window and the lifetime aggregate. Both
    # accessors default to 0 so old entries / un-folded aggregates are unchanged.
    window_no_pr = sum(e.get("tasks_completed_no_pr", 0) for e in history)
    window_code_failed = sum(
        max(0, e.get("tasks_failed", 0) - e.get("tasks_failed_infra", 0))
        for e in history
    )
    agg_merged = agg.get("total_prs_merged", 0)
    agg_completed = agg.get("total_tasks_completed", 0)
    agg_no_pr = agg.get("total_tasks_completed_no_pr", 0)
    agg_code_failed = max(
        0, agg.get("total_tasks_failed", 0) - agg.get("total_tasks_failed_infra", 0)
    )

    lifetime_merged = window_merged + agg_merged
    lifetime_completed_real = max(
        0, (window_completed - window_no_pr) + (agg_completed - agg_no_pr)
    )
    lifetime_non_merge_finished = max(
        0,
        (lifetime_completed_real + window_code_failed + agg_code_failed)
        - lifetime_merged,
    )
    total_finished = lifetime_merged + lifetime_non_merge_finished
    if total_finished == 0:
        return None
    lifetime_merge_rate = lifetime_merged / total_finished
    return {
        "lifetime_merged": lifetime_merged,
        "lifetime_non_merge_finished": lifetime_non_merge_finished,
        "lifetime_merge_rate": round(lifetime_merge_rate, 3),
    }


def _direction_still_productive(direction: dict) -> bool:
    """True when a direction is demonstrably still producing useful work.

    Gate for the cumulative merge-rate family of pause rules (Rule 2 zero-merge,
    Rule 5 churn, Rule 6 lifetime burn). Those rules count "finished but not merged"
    volume and can wrongly pause a healthy direction whose implementation tasks
    correctly complete as no-ops ("deliverable already present") — no PR, no merge,
    yet genuinely productive.

    We require BOTH signals rather than either alone (the task allows OR, but AND is
    strictly more robust here):
      * ``last_cycle_productive`` is True — the system's own per-cycle verdict that
        the most recent cycle spawned actionable work. This is the authoritative,
        already-computed contradiction to a "this direction is dead" pause.
      * ``consecutive_unproductive_cycles < N`` — guards against a single stale True
        flag papering over a direction that has since gone quiet for several cycles.
        N defaults to 2 and is configurable to match the direction_* config style.

    Using AND keeps existing behavior unchanged for directions that never set the
    productivity flag (the common case in fixtures and legacy data): ``.get`` returns
    falsy → not productive → the rules fire exactly as before. CODE-FAILURE rules
    (3, 4) are intentionally NOT gated by this — a crashing direction must still
    pause regardless of how productive its last cycle looked.
    """
    if not direction.get("last_cycle_productive"):
        return False
    skip_max = int(
        _runtime_config.get("direction_productive_pause_skip_max_unproductive", 2) or 2
    )
    consecutive = int(direction.get("consecutive_unproductive_cycles", 0) or 0)
    return consecutive < skip_max


def _already_built_signature(direction: dict) -> dict | None:
    """Detect a direction whose recent cycles are dominated by correct no-op
    "already-built" completions (PRs not opened because the deliverable already
    exists) — i.e. the goal is effectively already met.

    Returns a metrics dict (for completion-suggestion wording / evidence) when the
    signature holds, else None. Conservative by design: it only fires when, over a
    rolling window, the no-op completions clearly dominate finished work, there are
    essentially no merges, and there are NO code failures (a code-failing direction
    is broken, not done).

    Signature (all must hold over the last ``direction_churn_window_cycles`` cycles,
    needing the same >=3-cycle history floor the churn metric uses):
      * total no-op "already-built" completions >= ``min_no_pr`` (default 3), and
      * no-op completions are >= ``dominance`` (default 0.8) of all completed tasks,
      * merged PRs <= ``max_merged`` (default 1) — not an actively shipping direction,
      * zero code failures in the window — nothing is broken.
    """
    history = direction.get("research_history") or []
    window_cycles = int(_runtime_config.get("direction_churn_window_cycles", 5) or 5)
    if len(history) < 3 or window_cycles <= 0:
        return None
    window = history[-window_cycles:]
    no_pr = sum(e.get("tasks_completed_no_pr", 0) for e in window)
    completed = sum(e.get("tasks_completed", 0) for e in window)
    merged = sum(e.get("prs_merged", 0) for e in window)
    code_failed = sum(
        max(0, e.get("tasks_failed", 0) - e.get("tasks_failed_infra", 0))
        for e in window
    )
    min_no_pr = int(
        _runtime_config.get("direction_already_built_min_no_pr", 3) or 3
    )
    dominance = float(
        _runtime_config.get("direction_already_built_dominance", 0.8) or 0.8
    )
    max_merged = int(_runtime_config.get("direction_already_built_max_merged", 1) or 1)
    if (
        no_pr >= min_no_pr
        and completed > 0
        and (no_pr / completed) >= dominance
        and merged <= max_merged
        and code_failed == 0
    ):
        return {
            "window_cycles": len(window),
            "no_pr_completions": no_pr,
            "completed": completed,
            "merged": merged,
            "no_pr_ratio": round(no_pr / completed, 3),
        }
    return None


def _check_direction_health(direction: dict) -> str | None:
    """Return a pause reason if this direction should be auto-paused, else None."""
    history = direction.get("research_history") or []

    # Grace period: a just-recovered direction skips history-based rules (1, 2, 5, 6)
    # until at least one new research cycle is appended after recovery. This prevents
    # the frozen research_history that originally triggered the pause from immediately
    # re-triggering it on the very next lifecycle tick. Code-failure rules (3, 4) are
    # always evaluated so auto-retirement is never weakened by the grace period.
    recovered_at = direction.get("auto_recovered_at")
    in_grace = recovered_at is not None and not any(
        (e.get("timestamp") or "") > recovered_at for e in history
    )

    # Fix #1 gate for the cumulative merge-rate family (Rules 2, 5, 6 only): a
    # direction that is still demonstrably productive must not be pause-retired by a
    # zero-merge / churn / lifetime-burn signal. Code-failure rules (3, 4) below are
    # deliberately left UNGATED — a crashing direction pauses no matter how productive
    # its last cycle looked.
    still_productive = _direction_still_productive(direction)

    if not in_grace:
        # Rule 1: Persistent saturation — possibly_saturated AND last 5+ cycles all produced ≤1 task
        if direction.get("possibly_saturated") and len(history) >= 5:
            recent5 = history[-5:]
            if all(entry.get("tasks_created", 0) <= 1 for entry in recent5):
                return (
                    "Saturated: last 5 research cycles produced no actionable findings"
                )

        # Rule 2: Zero merge rate — 5+ cycles with 0 PRs merged total, and the most
        # recent cycle has no tasks still in-flight (tasks_created == tasks_completed + tasks_failed).
        # Skipped when the direction is still productive (Fix #1). Also skipped when the
        # only thing that "finished" was correct no-op "already-built" completions
        # (Fix #2 inline math): subtract tasks_completed_no_pr (and infra failures) from
        # the lifetime non-merge volume — if nothing real finished unmerged, 0 PRs merged
        # is "nothing to merge", not unproductiveness.
        if len(history) >= 5 and not still_productive:
            total_merged = sum(entry.get("prs_merged", 0) for entry in history)
            if total_merged == 0:
                non_merge_real = sum(
                    max(0, entry.get("tasks_completed", 0))
                    - entry.get("tasks_completed_no_pr", 0)
                    + max(
                        0,
                        entry.get("tasks_failed", 0)
                        - entry.get("tasks_failed_infra", 0),
                    )
                    for entry in history
                )
                last = history[-1]
                created = last.get("tasks_created", 0)
                finished = last.get("tasks_completed", 0) + last.get("tasks_failed", 0)
                if (created == 0 or finished >= created) and non_merge_real > 0:
                    total_created = sum(
                        entry.get("tasks_created", 0) for entry in history
                    )
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
            return (
                "Recent stall: last 3 research cycles all failed or produced no merges"
            )

    if not in_grace and not still_productive:
        # Rules 5 & 6 are gated on the same Fix #1 productivity check as Rule 2: a
        # still-productive direction is never churn/burn-paused.
        #
        # Rule 5: High non-merge churn — rolling window of finished tasks that did not merge.
        # Only fires when non-merging volume is both absolutely high AND merge rate is very low,
        # to avoid pausing productive high-throughput directions that simply have many tasks.
        # research/investigate children are excluded conservatively by the default threshold.
        churn_threshold = int(
            _runtime_config.get("direction_churn_non_merge_threshold", 15) or 15
        )
        churn_min_merge_rate = float(
            _runtime_config.get("direction_churn_min_merge_rate", 0.10) or 0.10
        )
        if churn_threshold > 0:
            metrics = _compute_churn_metrics(direction)
            if (
                metrics
                and metrics["non_merge_finished"] >= churn_threshold
                and metrics["merge_rate"] < churn_min_merge_rate
            ):
                return (
                    f"Churn guardrail: {metrics['non_merge_finished']} tasks finished "
                    f"without merging, {metrics['merged']} merged in last "
                    f"{metrics['window_cycles']} cycles "
                    f"(merge rate {metrics['merge_rate']:.0%})"
                )

        # Rule 6: Lifetime cumulative burn backstop — catches slow-burn directions that stay
        # just under the rolling-window threshold every window but accumulate hundreds of
        # non-merging tasks over their lifetime. High threshold prevents pausing productive
        # high-throughput directions. This pause requires manual operator intervention (retire
        # vs. reset) because the lifetime metric will not drop on its own after auto-recovery.
        lifetime_threshold = int(
            _runtime_config.get("direction_lifetime_non_merge_threshold", 150) or 150
        )
        lifetime_min_merge_rate = float(
            _runtime_config.get("direction_lifetime_min_merge_rate", 0.10) or 0.10
        )
        if lifetime_threshold > 0:
            lm = _compute_lifetime_burn_metrics(direction)
            if (
                lm
                and lm["lifetime_non_merge_finished"] >= lifetime_threshold
                and lm["lifetime_merge_rate"] < lifetime_min_merge_rate
            ):
                return (
                    f"Lifetime burn backstop: {lm['lifetime_non_merge_finished']} tasks "
                    f"finished without merging lifetime, {lm['lifetime_merged']} merged "
                    f"(lifetime merge rate {lm['lifetime_merge_rate']:.0%})"
                )

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


def already_built_completion_suggestion(direction: dict) -> str | None:
    """Return a completion-suggestion string when the direction's recent cycles are
    dominated by correct no-op "already-built" completions, else None (Fix #3).

    A direction whose implementation tasks keep completing as "no changes needed —
    deliverable already present" has effectively reached its goal: the feature exists
    on the integration branch already. That is COMPLETE/saturated, not churn — the
    operator should see it surfaced as possibly_complete, not silently auto-paused.

    Delegates the conservative signature to ``_already_built_signature`` (high no-op
    ratio, ~no merges, no code failures). Returns None when the signature does not
    hold so callers fall through to the normal pause / completion paths.
    """
    sig = _already_built_signature(direction)
    if not sig:
        return None
    return (
        f"Direction appears already built: {sig['no_pr_completions']} of "
        f"{sig['completed']} completed tasks needed no changes (deliverable already "
        f"present) across the last {sig['window_cycles']} cycles, "
        f"{sig['merged']} PRs merged. Goal likely already met — review for completion."
    )


# ── Outcome-progress (scaffolding) detector ──────────────────────────────────
# Advisory axis catching the INVERSE of every existing health rule: a direction
# with a HEALTHY merge rate that keeps shipping refactor/scaffolding PRs while
# never advancing its stated success_evidence. Such a direction scores high on
# quality_score and looks 'stable' on merge-rate trend, so it can burn dozens of
# cycles unnoticed. The flag drives a persistence-pressure ladder (in
# direction_lifecycle): it never adds a 7th auto-PAUSE rule, but escalates over
# consecutive flagged cycles — surfacing to the strategic agent / operator and,
# if the flag persists to max_flagged_cycles, auto-RETIRING the direction.

_OUTCOME_PROGRESS_HISTORY_HARD_CAP = 20


def _outcome_progress_config() -> dict:
    """Read outcome-progress thresholds from runtime_config with hardcoded fallbacks.

    Mirrors the direction_churn_* / direction_lifetime_* convention: defaults live
    in shared_state.runtime_config and are read via .get(key, fallback) so a missing
    key never raises.
    """

    def _int(key: str, fallback: int) -> int:
        try:
            return int(_runtime_config.get(key, fallback) or fallback)
        except (TypeError, ValueError):
            return fallback

    def _float(key: str, fallback: float) -> float:
        try:
            return float(_runtime_config.get(key, fallback) or fallback)
        except (TypeError, ValueError):
            return fallback

    return {
        "min_cycles": _int("direction_outcome_progress_min_cycles", 12),
        "merge_floor": _int("direction_outcome_progress_merge_floor", 8),
        "min_merge_rate": _float("direction_outcome_progress_min_merge_rate", 0.5),
        "stall_run": _int("direction_outcome_progress_stall_run", 6),
        "history_max": _int(
            "direction_outcome_progress_history_max",
            _OUTCOME_PROGRESS_HISTORY_HARD_CAP,
        ),
        "escalation_cycles": _int("direction_outcome_progress_escalation_cycles", 3),
        "max_flagged_cycles": _int("direction_outcome_progress_max_flagged_cycles", 6),
    }


def _compute_outcome_progress_signal(direction: dict) -> int:
    """Deterministic outcome-progress proxy: cumulative confirmed established findings.

    Counts confirmed (non-proposed, non-failed) entries currently visible in the
    direction's research_context PLUS confirmed findings that were evicted from
    the document once it reached its character cap. Both halves apply the SAME
    confirmed-only filter: the visible count excludes ``(proposed)`` /
    ``(failed:)`` entries, and the evicted half reads ``established_confirmed`` —
    the eviction summary's sub-count of durable evictions — NOT the raw
    ``established`` total (which also tallies evicted proposed/failed entries).
    This keeps the two halves consistent, so a saturated long-running direction
    that evicts only churn (proposed/failed) entries does NOT appear to advance,
    while genuine confirmed findings still increment the signal whether they sit
    below the cap (visible) or were trimmed at the cap (evicted_confirmed). The
    combined value is monotonically non-decreasing for confirmed progress.
    """
    from app.research_context import _parse_eviction_counts, _parse_research_context

    ctx = direction.get("research_context") or ""
    if not ctx:
        return 0
    parsed = _parse_research_context(ctx)
    # Count only durably-landed findings: exclude proposed (not yet shipped) and
    # failed (retagged after an unsuccessful fix attempt) so churn-only directions
    # don't appear to advance.
    visible = sum(
        1
        for item in parsed.get("established", [])
        if "(proposed)" not in item and "(failed:" not in item
    )
    evicted_confirmed = _parse_eviction_counts(parsed.get("eviction_summary")).get(
        "established_confirmed", 0
    )
    return visible + evicted_confirmed


def outcome_progress_summary_fields(direction: dict) -> dict:
    """Cumulative confirmed-finding count + trend from outcome_progress_history.

    Single source of truth for the eviction-robust signal surfaced to the
    strategic cycle. Shared by both producers — strategic_analysis
    (_build_outcome_evaluation) and strategic_spawning (_spawn_strategic_review)
    — so the count selection and trend thresholds cannot drift between them.
    Returns {} when no snapshots exist; otherwise {"confirmed_finding_count": int}
    plus "confirmed_finding_trend" ("improving"/"declining"/"stable") once ≥2
    snapshots are available. Trend compares the mean of the first half of
    snapshots against the second half (±30% band).
    """
    snaps = direction.get("outcome_progress_history") or []
    if not snaps:
        return {}
    progress_vals = [int(s.get("progress", 0) or 0) for s in snaps]
    fields: dict = {"confirmed_finding_count": progress_vals[-1]}
    if len(progress_vals) >= 2:
        mid = len(progress_vals) // 2
        first_avg = sum(progress_vals[:mid]) / max(mid, 1)
        second_avg = sum(progress_vals[mid:]) / max(len(progress_vals) - mid, 1)
        if second_avg > first_avg * 1.3:
            fields["confirmed_finding_trend"] = "improving"
        elif second_avg < first_avg * 0.7:
            fields["confirmed_finding_trend"] = "declining"
        else:
            fields["confirmed_finding_trend"] = "stable"
    return fields


def record_outcome_progress_snapshot(direction: dict) -> None:
    """Append a per-cycle outcome-progress snapshot to the direction (bounded).

    Records the current progress signal alongside the cumulative cycle count and
    lifetime merged volume, capping the list the same way recent_findings_evidence
    and drift-snapshot retention are capped (sliding window, no new table). Also
    latches ``outcome_progress_ever_met`` when the positive-completion detector
    (``_check_direction_completion``) trips or the direction is flagged
    possibly_complete, so the scaffolding detector can require a direction to have
    NEVER met its outcome.

    Called once per research cycle from research_direction_state.apply_post_spawn_state
    after research_context has been rebuilt for the cycle.
    """
    cfg = _outcome_progress_config()
    history = direction.get("research_history") or []
    agg = direction.get("history_aggregate") or {}
    total_cycles = len(history) + (agg.get("total_cycles") or 0)
    total_merged = sum(e.get("prs_merged", 0) for e in history) + (
        agg.get("total_prs_merged") or 0
    )
    se_text = ((direction.get("context") or {}).get("success_evidence") or "").encode()
    success_evidence_fp = hashlib.sha256(se_text).hexdigest()[:16]
    snapshot = {
        "timestamp": _utcnow().isoformat(),
        "cycle_index": total_cycles,
        "progress": _compute_outcome_progress_signal(direction),
        "merged": total_merged,
        "success_evidence_fp": success_evidence_fp,
    }
    snaps = list(direction.get("outcome_progress_history") or [])
    snaps.append(snapshot)
    cap = max(1, cfg["history_max"])
    if len(snaps) > cap:
        snaps = snaps[-cap:]
    direction["outcome_progress_history"] = snaps
    # Latch ``outcome_progress_ever_met`` only when the completion heuristic
    # trips with evidence accumulated UNDER the current outcome. Both
    # `_check_direction_completion` and the lifecycle-derived
    # `possibly_complete` flag are independent of success_evidence /
    # retirement_condition — they fire purely on cumulative cycle counts and
    # PR-merge totals. Without a post-reset gate, a direction that was 'complete'
    # under an old goal would have this flag re-latched on the very next
    # snapshot after the outcome is redefined, permanently exempting it from
    # future scaffolding detection. We require post-reset cycle / merge deltas
    # that meet the heuristic's own gates (>=4 cycles, >=3 merges) before
    # treating completion as meaningful under the new outcome.
    reset_cycle = direction.get("outcome_progress_reset_at_cycle")
    reset_merged = direction.get("outcome_progress_reset_at_merged")
    if reset_cycle is None:
        latch_eligible = True
    else:
        post_cycles = total_cycles - int(reset_cycle)
        post_merged = total_merged - int(reset_merged or 0)
        latch_eligible = post_cycles >= 4 and post_merged >= 3
    if latch_eligible and (
        _check_direction_completion(direction) is not None
        or direction.get("possibly_complete")
    ):
        direction["outcome_progress_ever_met"] = True


# Fields that encode either past outcome progress / completion state or the
# advisory scaffolding-flag ladder. ALL of these must be cleared when the
# direction's success_evidence or retirement_condition is redefined — they
# were judged against the OLD outcome and would otherwise surface stale
# evidence into strategic_analysis / strategic_spawning until the next
# lifecycle sweep clears them, or permanently exempt the direction from
# future scaffolding detection.
_OUTCOME_RESET_FIELDS: tuple[str, ...] = (
    "outcome_progress_ever_met",
    "outcome_progress_history",
    "evidence_progress",
    "outcome_scaffolding_flagged",
    "outcome_scaffolding_flagged_at",
    "outcome_scaffolding_flagged_cycles",
    "outcome_scaffolding_evidence",
    "possibly_complete",
    "completion_suggestion",
)


def _reset_scaffolding_state_on_outcome_change(direction: dict) -> bool:
    """Clear outcome-progress + scaffolding-flag state on outcome redefinition.

    Pops every field listed in ``_OUTCOME_RESET_FIELDS`` so future cycles judge
    the direction against the NEW goal rather than stale progress, stale
    completion state, or stale scaffolding-flag evidence. Also stamps a
    post-reset baseline (``outcome_progress_reset_at_cycle`` /
    ``outcome_progress_reset_at_merged``) that ``record_outcome_progress_snapshot``
    consults to keep ``outcome_progress_ever_met`` from re-latching on the very
    next snapshot — the completion heuristic is independent of the redefined
    outcome, so we require fresh post-reset cycles and merges before treating
    its verdict as meaningful under the new goal. Returns True if any tracked
    field was actually removed (caller may persist on truthy result).
    """
    cleared = False
    for _field in _OUTCOME_RESET_FIELDS:
        if _field in direction:
            direction.pop(_field)
            cleared = True
    history = direction.get("research_history") or []
    agg = direction.get("history_aggregate") or {}
    total_cycles = len(history) + (agg.get("total_cycles") or 0)
    total_merged = sum(e.get("prs_merged", 0) for e in history) + (
        agg.get("total_prs_merged") or 0
    )
    direction["outcome_progress_reset_at_cycle"] = total_cycles
    direction["outcome_progress_reset_at_merged"] = total_merged
    return cleared


def _evidence_progress_suppresses_scaffolding(
    direction: dict, total_cycles: int, stall_run: int
) -> bool:
    """True if a current agent evidence_progress verdict confirms advancement.

    An ``advancing`` or ``met`` verdict recorded within the last ``stall_run``
    cycles suppresses the advisory scaffolding flag — the agent has confirmed
    genuine progress, so the deterministic detector defers to it. A ``stalled``
    verdict (which reinforces the deterministic signal) or NO verdict at all
    never suppresses, so the deterministic detector stays the guaranteed
    backstop whenever the agent omits the optional assessment. The verdict
    expires after a full stall window so a stale "advancing" cannot mask a
    direction that later genuinely stalls.
    """
    ep = direction.get("evidence_progress")
    if not isinstance(ep, dict):
        return False
    if ep.get("status") not in _EVIDENCE_PROGRESS_SUPPRESSING:
        return False
    recorded_cycle = ep.get("cycle_index")
    if not isinstance(recorded_cycle, int):
        # Undated verdict — trust the agent's confirmation (treat as current).
        return True
    return (total_cycles - recorded_cycle) < max(1, stall_run)


def _check_outcome_scaffolding(direction: dict) -> dict | None:
    """Return advisory evidence dict if a direction looks like outcome-less scaffolding.

    ADVISORY ONLY — callers must NOT auto-pause on this. Returns None unless ALL hold:
      - long-running: cumulative cycle count >= min_cycles (default ~12);
      - keeps shipping merges: lifetime merged >= merge_floor AND lifetime merge
        rate >= min_merge_rate, so churn/burn directions (low merge rate / high
        non-merge volume) are explicitly EXCLUDED — those are handled by the churn
        guardrail and lifetime-burn backstop in _check_direction_health;
      - sustained no progress: the cumulative confirmed-finding count did not grow
        across the last stall_run snapshots AND the direction has never reached a
        'met' outcome.
    Guards hard against false positives: requires a FULL stall_run window of
    snapshots, so a single stalled cycle cannot trip the flag, and any growth in
    the confirmed-finding count anywhere in the window clears it.
    """
    if direction.get("outcome_progress_ever_met"):
        return None
    cfg = _outcome_progress_config()
    history = direction.get("research_history") or []
    agg = direction.get("history_aggregate") or {}
    total_cycles = len(history) + (agg.get("total_cycles") or 0)
    if total_cycles < cfg["min_cycles"]:
        return None
    # Keeps shipping merges at a healthy rate (excludes churn/burn).
    lm = _compute_lifetime_burn_metrics(direction)
    if not lm:
        return None
    lifetime_merged = lm["lifetime_merged"]
    merge_rate = lm["lifetime_merge_rate"]
    if lifetime_merged < cfg["merge_floor"]:
        return None
    if merge_rate < cfg["min_merge_rate"]:
        return None
    # Sustained no progress across a full run of recorded snapshots.
    run = cfg["stall_run"]
    snaps = direction.get("outcome_progress_history") or []
    if run < 2 or len(snaps) < run:
        return None
    # Filter snapshots to the current contract fingerprint so that contract rewrites
    # (success_evidence, retirement_condition, etc.) cannot mix old-contract measurements
    # into the stall-run window. If fewer than run snapshots exist under the current
    # contract, the window is insufficient and we return None — a direction whose contract
    # was just rewritten needs time to build up new-contract evidence before the detector
    # can fire. This is a defense-in-depth layer; callers should also call
    # _reset_outcome_detection_state on contract changes to clear the history outright.
    se_text = ((direction.get("context") or {}).get("success_evidence") or "").encode()
    current_fp = hashlib.sha256(se_text).hexdigest()[:16]
    contract_snaps = [s for s in snaps if s.get("success_evidence_fp") == current_fp]
    if len(contract_snaps) < run:
        return None
    window = contract_snaps[-run:]
    progress_vals = [int(s.get("progress", 0) or 0) for s in window]
    # Gate on whether the cumulative confirmed-finding count grew during the stall window.
    # The signal from _compute_outcome_progress_signal is visible + evicted confirmed
    # findings — monotonically non-decreasing regardless of research_context cap saturation.
    if max(progress_vals) > progress_vals[0]:
        return None  # finding count grew -> direction is advancing
    # Never met its outcome (current completion detector must also be negative).
    if _check_direction_completion(direction) is not None:
        return None
    # OPTIONAL agent refinement: a current advancing/met verdict suppresses the
    # advisory flag (the agent confirms genuine progress). A stalled verdict or
    # an omitted verdict never suppresses — the deterministic detector fires.
    if _evidence_progress_suppresses_scaffolding(direction, total_cycles, run):
        return None
    return {
        "total_cycles": total_cycles,
        "lifetime_merged": lifetime_merged,
        "lifetime_merge_rate": round(merge_rate, 3),
        "stall_run_cycles": run,
        "progress_signal": progress_vals[-1],
        "outcome_progress_window": progress_vals,
        "progress_stagnant_cycles": run,
    }


_DIRECTION_DEDUP_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "to",
        "for",
        "and",
        "or",
        "in",
        "of",
        "is",
        "it",
        "this",
        "that",
        "with",
        "by",
        "we",
        "should",
        "be",
        "are",
        "on",
        "as",
        "feat",
        "fix",
        "refactor",
        "add",
        "update",
        "improve",
    }
)


def _is_direction_duplicate(
    proposal_text: str,
    existing_directions: list,
    rejected_proposals: list,
) -> bool:
    """Return True if proposal_text overlaps with an existing or recently rejected direction.

    A near-duplicate is a 4+ shared non-stopword token overlap against any existing
    direction (any status) or a proposal rejected within the last 30 days.

    This is now an **anti-loop guard for the automated/programmatic direction
    creators only** — the starvation fallback, anomaly auto-proposals, and
    research-finding extraction, which can otherwise re-emit near-identical
    directions every cycle. The *strategic agent's own* proposals are no longer
    hard-blocked: those call sites treat overlap as advisory (the agent owns its
    direction set and can retire a true duplicate next cycle). That is why the old
    distinction-override / terminal-status escape hatch was removed — it existed
    only to let the agent argue past this guard, and the agent now bypasses it
    entirely rather than fighting it (and edge-casing on which statuses the
    override covered was itself a recurring silent-block bug).
    """
    words = set(proposal_text.lower().split()) - _DIRECTION_DEDUP_STOPWORDS
    if len(words) < 2:
        return False
    for d in existing_directions:
        existing_text = (d.get("text") or "").lower()
        existing_words = set(existing_text.split()) - _DIRECTION_DEDUP_STOPWORDS
        if len(words & existing_words) >= 4:
            return True
    cutoff = (_utcnow() - timedelta(days=30)).isoformat()
    for entry in rejected_proposals:
        if entry.get("rejected_at", "") < cutoff:
            continue
        rejected_words = (
            set((entry.get("text") or "").lower().split()) - _DIRECTION_DEDUP_STOPWORDS
        )
        if len(words & rejected_words) >= 4:
            return True
    return False


def _find_matching_direction(
    proposal_text: str, existing_directions: list
) -> dict | None:
    """Return the direction that triggered the dup-match, using the same
    tokenization/threshold as `_is_direction_duplicate`.

    Used purely for diagnostic logging when a proposal is rejected. Falls back
    to None if no direction passes the threshold (e.g. when the rejection came
    from a recently-rejected proposal rather than an existing direction).
    """
    text_lower = proposal_text.lower().strip()
    words = set(proposal_text.lower().split()) - _DIRECTION_DEDUP_STOPWORDS
    for d in existing_directions:
        existing_text = (d.get("text") or "").lower().strip()
        if existing_text == text_lower:
            return d
        existing_words = set(existing_text.split()) - _DIRECTION_DEDUP_STOPWORDS
        if len(words & existing_words) >= 4:
            return d
    return None


def _find_recent_rejected_proposal(
    proposal_text: str, rejected_proposals: list
) -> dict | None:
    """Return a recent rejected proposal that overlaps with proposal_text.

    Uses the same tokenization/threshold as `_is_direction_duplicate`, but only
    scans rejections from the last 30 days so strategic-agent create paths can
    hard-block recent operator/user rejections while keeping older overlaps
    advisory-only.
    """
    words = set(proposal_text.lower().split()) - _DIRECTION_DEDUP_STOPWORDS
    if len(words) < 2:
        return None
    text_lower = proposal_text.lower().strip()
    cutoff = (_utcnow() - timedelta(days=30)).isoformat()
    for entry in rejected_proposals:
        if entry.get("rejected_at", "") < cutoff:
            continue
        rejected_text = (entry.get("text") or "").lower().strip()
        if rejected_text == text_lower:
            return entry
        rejected_words = set(rejected_text.split()) - _DIRECTION_DEDUP_STOPWORDS
        if len(words & rejected_words) >= 4:
            return entry
    return None


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
    from app.alert_dispatch import _create_strategic_alert

    directions = project.directions or []
    active_count = sum(1 for d in directions if d.get("status") == "active")
    product_count = sum(
        1
        for d in directions
        if d.get("status") == "active" and d.get("category") == "product"
    )
    all_saturated = active_count > 0 and all(
        d.get("possibly_saturated") for d in directions if d.get("status") == "active"
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
                    fallback_text = (
                        "Expand system capabilities beyond current directions"
                    )
                    fallback_category = None
                else:
                    gap_type = "ALL_SATURATED"
                    fallback_text = (
                        "Explore and improve underserved areas of the system"
                    )
                    fallback_category = None

                # Audit W2/D8: dedup the fallback against existing directions
                # AND recently-rejected proposals before creating. The previous
                # version skipped dedup, so a saturated project would loop
                # creating near-identical fallbacks every 5 cycles.
                rejected_proposals = list(
                    (vision or {}).get("rejected_proposals") or []
                )
                is_dup = _is_direction_duplicate(
                    fallback_text,
                    directions,
                    rejected_proposals,
                )
                if is_dup:
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
                        project.id,
                        session,
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
                    "notes": [
                        {
                            "text": (
                                f"Auto-created by starvation handler after {starvation_count} "
                                f"consecutive cycles with no direction creation"
                            ),
                            "added_at": _utcnow().isoformat(),
                        }
                    ],
                    "research_history": [],
                }
                if fallback_category:
                    fallback_dir["category"] = fallback_category

                from app.task_locking import with_directions_locked

                _fd = fallback_dir
                await with_directions_locked(
                    session, project.id, lambda dirs: dirs.append(_fd)
                )

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
                    project.id,
                    session,
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
                gap_parts.append(
                    f"only {active_count} active directions (min 3 needed)"
                )
            if product_count == 0:
                gap_parts.append("no product-category directions")
            if all_saturated:
                gap_parts.append("all active directions are saturated")
            gap_desc = "; ".join(gap_parts) or "unspecified gap"
            vision["_strategic_frequency_override"] = 2
            project.vision = vision
            flag_modified(project, "vision")
            await _create_strategic_alert(
                project.id,
                session,
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


def _apply_evidence_progress_verdicts(task: Task, project: Project) -> int:
    """Persist agent evidence_progress verdicts onto matching active directions.

    Reads the OPTIONAL list stashed by main.strategic_operation under
    EVIDENCE_PROGRESS_CONTEXT_KEY and writes ``direction["evidence_progress"]``
    = {status, rationale, recorded_at, cycle_index, cycle_task_id} onto each
    matching ACTIVE direction. The verdict lives alongside the deterministic
    ``outcome_progress_history`` and is read back by ``_check_outcome_scaffolding``
    to refine (suppress/reinforce) the advisory flag. Returns the count of
    directions updated; a no-op returning 0 when the agent omitted the optional
    assessment, so the deterministic backstop is unaffected.
    """
    verdicts = (task.context or {}).get(EVIDENCE_PROGRESS_CONTEXT_KEY)
    if not isinstance(verdicts, list) or not verdicts:
        return 0
    by_id = {
        v["direction_id"]: v
        for v in verdicts
        if isinstance(v, dict) and v.get("direction_id")
    }
    if not by_id:
        return 0
    now_iso = _utcnow().isoformat()
    applied = 0
    for d in project.directions or []:
        if d.get("status") != "active":
            continue
        verdict = by_id.get(d.get("id"))
        if not verdict:
            continue
        status = str(verdict.get("status", "")).strip().lower()
        if status not in _VALID_EVIDENCE_PROGRESS:
            continue
        history = d.get("research_history") or []
        agg = d.get("history_aggregate") or {}
        cycle_index = len(history) + (agg.get("total_cycles") or 0)
        d["evidence_progress"] = {
            "status": status,
            "rationale": str(verdict.get("rationale", ""))[
                :_EVIDENCE_PROGRESS_RATIONALE_MAX
            ],
            "recorded_at": now_iso,
            "cycle_index": cycle_index,
            "cycle_task_id": str(task.id),
        }
        applied += 1
    if applied:
        flag_modified(project, "directions")
    return applied


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
    from app.alert_dispatch import _create_strategic_alert

    # Phase C3: track whether the caller pre-acquired the project lock so
    # the legacy fallback path below can reuse it instead of re-acquiring.
    caller_holds_lock = project is not None
    if project is None:
        project = await session.get(Project, task.project_id)
    if not project:
        logger.error(f"[STRATEGIC] Task {task.id}: project {task.project_id} not found")
        return

    vision_rev = (project.vision or {}).get("revision", 0)
    active_dirs = sum(
        1 for d in (project.directions or []) if d.get("status") == "active"
    )

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
        # Collect rationales regardless of cycle_id so synthesized memos always
        # have the retirement reason available at spawn time.
        retired_rationales: Dict[str, str] = _collect_rationales(
            project, retired_ids, _RETIRED_NOTE_PREFIX
        )
        if cycle_id:
            _vision_updated = (task.context or {}).get(
                "_strategic_vision_set", False
            ) or vision_rev > spawned_vision_rev
            _spawn_suffix = f", {tasks_spawned} tasks spawned" if tasks_spawned else ""
            _summary = f"rev {vision_rev}, {active_dirs} active, {dirs_created} created, {dirs_retired} retired{_spawn_suffix}"
            created_rationales = _collect_rationales(
                project, created_ids, _CREATED_NOTE_PREFIX
            )
            # retired_rationales already populated above (outside cycle_id guard)
            created_predictions = _collect_creation_predictions(project, created_ids)
            retired_final_metrics = _collect_retirement_metrics(project, retired_ids)
            # Audit W2: cycle outcome lives on the StrategicCycle row;
            # per-direction maps land in cycle_metadata.
            _retirement_critiques = (task.context or {}).get(
                "_strategic_retirement_critiques"
            )
            _extra: dict = {
                "created_direction_ids": created_ids,
                "retired_direction_ids": retired_ids,
                "created_rationales": created_rationales,
                "retired_rationales": retired_rationales,
                "created_predictions": created_predictions,
                "retired_final_metrics": retired_final_metrics,
            }
            if _retirement_critiques is not None:
                _extra["retirement_critiques"] = _retirement_critiques
            try:
                from app.research_completion import query_findings_index

                _finding_rows = await query_findings_index(
                    session, project.id, limit=20, days=30
                )
                if _finding_rows:
                    _extra["recent_research_findings"] = _finding_rows
                    _extra["recent_research_findings_prompt"] = (
                        format_findings_for_prompt(_finding_rows)
                    )
            except Exception:  # noqa: BLE001
                logger.warning(
                    f"[STRATEGIC] Task {task.id}: failed to fetch findings index (non-fatal)",
                    exc_info=True,
                )
            _memo_citations: list = []
            try:
                _memo_citations = await strategic_cycle.query_recent_memo_citations(
                    session, project.id
                )
                if _memo_citations:
                    _extra["memo_citations"] = _memo_citations
            except Exception:  # noqa: BLE001
                logger.warning(
                    f"[STRATEGIC] Task {task.id}: failed to fetch memo citations (non-fatal)",
                    exc_info=True,
                )
            _cycle_reasoning = _extract_cycle_reasoning(task)
            _memos_available = len(_memo_citations)
            # Prefer the structured cited-ID set written by the agent via the
            # cite_memos API (task.context["_strategic_cited_memo_ids"]).  This is
            # reliable because the thinker analysis text is scanned at emit-time,
            # right after the thinker phase, before the status message is set.
            # Fall back to text-scanning _cycle_reasoning for tasks that predate
            # the cite_memos API (no key in context).
            _ctx_cited_ids = (task.context or {}).get("_strategic_cited_memo_ids")
            if _ctx_cited_ids is not None:
                _available_ids = {
                    m.get("task_id") for m in _memo_citations if m.get("task_id")
                }
                _memos_cited = len(set(_ctx_cited_ids) & _available_ids)
            else:
                _memos_cited = strategic_cycle.count_cited_memos(
                    _memo_citations, _cycle_reasoning
                )
            await strategic_cycle.mark_phase2_completed(
                session,
                cycle_id,
                summary_text=_summary,
                cycle_reasoning=_cycle_reasoning,
                directions_created=dirs_created,
                directions_retired=dirs_retired,
                tasks_spawned=tasks_spawned,
                vision_updated=_vision_updated,
                memos_available=_memos_available,
                memos_cited=_memos_cited,
                extra_metadata=_extra,
            )
        # OPTIONAL refinement: persist per-direction evidence_progress verdicts
        # (advancing|stalled|met) onto active directions. Independent of cycle_id
        # — a no-op when the agent omitted the assessment, leaving the
        # deterministic scaffolding detector as the guaranteed backstop.
        _ep_applied = _apply_evidence_progress_verdicts(task, project)
        if _ep_applied:
            logger.info(
                f"[STRATEGIC] Task {task.id}: applied {_ep_applied} "
                f"evidence_progress verdict(s) to directions"
            )
        await _check_direction_starvation(project, dirs_created, session)
        session.add(project)
        _spawn_suffix_msg = f", {tasks_spawned} tasks spawned" if tasks_spawned else ""
        task.latest_status_message = f"Strategic review complete — vision rev {vision_rev}, {active_dirs} active directions, {dirs_created} created, {dirs_retired} retired{_spawn_suffix_msg}"
        session.add(task)
        # Audit: check for unaddressed FEEDBACK messages
        remaining_feedback_result = await session.execute(
            select(func.count())
            .select_from(ProjectMessage)
            .where(
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
        # Apply version_phase_decision if the agent supplied one via the API.
        # The ``set_problem_model`` endpoint lifts the field out of the
        # problem_model JSON (ProblemModel is extra="forbid") and stows the
        # sanitized {phase, reasoning} dict under
        # ``VERSION_PHASE_DECISION_CONTEXT_KEY`` in task.context.
        _vpd_ctx = (task.context or {}).get(VERSION_PHASE_DECISION_CONTEXT_KEY)
        await _apply_version_phase_decision(
            _vpd_ctx, task, project, session, path="api"
        )
        # Apply qualify_request if the agent supplied one via the API. Same
        # transport as version_phase_decision: the ``set_problem_model``
        # endpoint lifts the field out of the problem_model JSON and stows the
        # sanitized {reason} dict under ``QUALIFY_REQUEST_CONTEXT_KEY``. When the
        # open ReleaseVersion is in ``stabilize`` this kicks off the release
        # qualifier.
        _qr_ctx = (task.context or {}).get(QUALIFY_REQUEST_CONTEXT_KEY)
        await _apply_qualify_request(_qr_ctx, task, project, session, path="api")

        # Per-cycle message post removed: the strategic agent now maintains
        # project.running_update as a living markdown document it rewrites
        # each cycle. Replaces the previous one-message-per-cycle noise.
        await _spawn_retirement_memos(
            session, project, task, retired_ids, retired_rationales
        )
        await session.commit()
        # Post-commit: the live strategic ops already emitted per-write
        # `strategic_updated` events during execution; here we close the cycle.
        from app.strategic_events import emit_strategic_cycle_completed

        await emit_strategic_cycle_completed(task.project_id, task.id, 2)
        logger.info(
            f"[STRATEGIC] Task {task.id} complete via API: vision rev {vision_rev}, {active_dirs} active directions"
        )
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
            cycle_id = task.strategic_cycle_id or (task.context or {}).get(
                "strategic_cycle_id"
            )
            if cycle_id:
                # Audit W2: failure recorded on the StrategicCycle row.
                await strategic_cycle.mark_failure(
                    session,
                    cycle_id,
                    phase="phase2",
                    reason="strategic_review unparseable output",
                )
            session.add(project)
        task.latest_status_message = "Strategic output could not be parsed"
        session.add(task)
        await session.commit()
        # Post-commit: the cycle ended (in failure) — close it for the UI.
        from app.strategic_events import emit_strategic_cycle_completed

        await emit_strategic_cycle_completed(task.project_id, task.id, 2)
        failure_num = current_vision.get("_strategic_failures", "?") if project else "?"
        logger.warning(
            f"[STRATEGIC] Task {task.id}: unparseable output (failure #{failure_num})"
        )
        if project:
            await _create_strategic_alert(
                task.project_id,
                session,
                f"Strategic review could not parse agent output (failure #{failure_num}, task {str(task.id)[:8]})",
            )
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
            exists = (
                await session.execute(
                    select(Project.id).where(Project.id == task.project_id)
                )
            ).first()
            if exists is None:
                logger.error(
                    f"[STRATEGIC] Task {task.id}: project {task.project_id} not found"
                )
                return
            logger.warning(
                f"[STRATEGIC] Task {task.id}: project {task.project_id} held under contention "
                f"after retries; deferring mutation to next cycle"
            )
            await _create_strategic_alert(
                task.project_id,
                session,
                f"Strategic completion: project row contended (task {str(task.id)[:8]}) — mutation deferred",
            )
            return

    # Store raw output for admin visibility
    task.plan_output = parsed
    session.add(task)

    # Update vision — merge with current DB state to avoid overwriting concurrent user edits.
    # Audit W2/D7: full 3-way merge (user-bias, keep-both via _rejected_agent_edits ledger)
    # extends to systems/priorities/context lists, not just purpose/success_criteria.
    if new_vision and isinstance(new_vision, dict):
        from app.vision_merge import append_rejected_edits, merge_vision

        current_vision = dict(project.vision or {})
        payload_vision_rev = (task.first_payload.get("vision") or {}).get("revision", 0)
        db_vision_rev = current_vision.get("revision", 0)
        # Baseline: prefer the snapshot the cycle row captured at spawn time
        # (StrategicCycle.cycle_metadata.baseline_vision). Falls back to the
        # payload vision if the cycle row is unavailable (legacy task).
        cycle_id = task.strategic_cycle_id or (task.context or {}).get(
            "strategic_cycle_id"
        )
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
                mapped_dirs = [
                    d for d in directions_list if d.get("system_id") == sys_id
                ]
                total_merged = 0
                for d in mapped_dirs:
                    for h in d.get("research_history", []):
                        total_merged += h.get("prs_merged", 0)
                    agg = d.get("history_aggregate", {})
                    total_merged += agg.get("total_prs_merged", 0)
                # Only upgrade status, never downgrade what the agent set
                agent_status = sys.get("status", "planned")
                STATUS_RANK = {
                    "planned": 0,
                    "in_progress": 1,
                    "functional": 2,
                    "mature": 3,
                }
                agent_rank = STATUS_RANK.get(agent_status, 0)
                if total_merged >= 5 and agent_rank < 2:
                    sys["status"] = "functional"
                elif (total_merged > 0 or len(mapped_dirs) > 0) and agent_rank < 1:
                    sys["status"] = "in_progress"
        project.vision = new_vision
        flag_modified(project, "vision")
        logger.info(
            f"[STRATEGIC] Updated vision for project {project.id} to revision {new_vision['revision']}"
        )

    # Apply problem_model update — new strategic source of truth running
    # alongside vision during the migration. Failures here are logged and
    # swallowed so a malformed problem_model doesn't block vision / direction
    # ops writes.
    if new_problem_model and isinstance(new_problem_model, dict):
        try:
            from pydantic import ValidationError as _PmValidationError

            from app.problem_model import apply_agent_update

            # Resolve the cycle's baseline problem model (snapshot at spawn) for
            # the 3-way operator-wins merge, mirroring the vision baseline
            # resolution above. Degrade to None (blank-preservation only) when
            # no cycle/baseline is resolvable (legacy task).
            pm_baseline: dict | None = None
            pm_cycle_id = task.strategic_cycle_id or (task.context or {}).get(
                "strategic_cycle_id"
            )
            if pm_cycle_id:
                pm_cycle_row = await strategic_cycle.get(session, pm_cycle_id)
                if pm_cycle_row is not None:
                    snap = strategic_cycle.baseline_problem_model(pm_cycle_row)
                    if snap:
                        pm_baseline = snap

            try:
                new_pm_raw, pm_diff = apply_agent_update(
                    current_raw=project.problem_model,
                    proposed_raw=new_problem_model,
                    now_iso=_utcnow().isoformat(),
                    baseline_raw=pm_baseline,
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
            logger.exception(
                "[STRATEGIC] Failed to apply problem_model update (non-fatal)"
            )

    # Reconcile project.running_update operator-wins via the same shared helper
    # the live set_running_update op uses, so the two write paths cannot drift.
    # The document is a living narrative the agent rewrites each cycle; a
    # concurrent operator body edit is preserved (operator wins, agent's
    # document recorded for next cycle) and a mid-cycle routine-event digest
    # append is re-integrated rather than discarded. Degrades to blind-assign
    # when no cycle/baseline is resolvable (legacy task).
    if isinstance(new_running_update, str):
        try:
            from app.running_update_merge import apply_reconciled_running_update

            ru_baseline: str | None = None
            ru_cycle_id = task.strategic_cycle_id or (task.context or {}).get(
                "strategic_cycle_id"
            )
            if ru_cycle_id:
                ru_cycle_row = await strategic_cycle.get(session, ru_cycle_id)
                if ru_cycle_row is not None:
                    ru_baseline = strategic_cycle.baseline_running_update(
                        ru_cycle_row
                    )
            ru_rejected = apply_reconciled_running_update(
                project, baseline=ru_baseline, agent=new_running_update
            )
            if ru_rejected is not None:
                logger.info(
                    f"[STRATEGIC] running_update: concurrent operator edit "
                    f"preserved for project {project.id}; agent's document "
                    f"({len(new_running_update)} chars) recorded as a rejected edit"
                )
            else:
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
                entry = SelfReflectionEntry.model_validate(
                    {
                        "ts": _utcnow().isoformat(),
                        "cycle_task_id": str(task.id),
                        **new_reflection,
                    }
                )
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
        logger.warning(
            f"[STRATEGIC] Task {task.id}: no vision produced (failure #{failures})"
        )
        if failures >= 2:
            await _create_strategic_alert(
                task.project_id,
                session,
                f"Strategic review produced no vision ({failures} consecutive failures, task {str(task.id)[:8]})",
            )

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
            logger.warning(
                f"[STRATEGIC] Task {task.id}: truncating {len(direction_ops)} direction ops to {MAX_DIRECTION_OPS}"
            )
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
                logger.warning(
                    f"[STRATEGIC] Task {task.id}: truncating rationale {len(rationale)}→{STRATEGIC_RATIONALE_MAX} chars"
                )
                rationale = rationale[:STRATEGIC_RATIONALE_MAX]

            if op == "create":
                if creates_count >= MAX_DIRECTION_CREATES:
                    continue
                text = op_entry.get("text", "").strip()
                if not text:
                    continue
                if len(text) > STRATEGIC_DIRECTION_TEXT_MAX:
                    logger.warning(
                        f"[STRATEGIC] Task {task.id}: truncating direction text {len(text)}→{STRATEGIC_DIRECTION_TEXT_MAX} chars"
                    )
                text = text[:STRATEGIC_DIRECTION_TEXT_MAX]
                raw_dedup_reasoning = op_entry.get("dedup_reasoning")
                # Advisory dedup: the strategic agent owns its direction set, so a
                # proposal that resembles an existing direction is no longer
                # hard-blocked — we surface the resemblance (log + a note on the
                # created direction) and trust the agent's judgment (it can retire
                # a true duplicate next cycle). Hard dedup is retained only on the
                # automated/loop-prone creators.
                matched = _find_matching_direction(text, directions)
                if matched:
                    logger.info(
                        "[STRATEGIC] Direction '%s' resembles existing '%s' (status=%s) — "
                        "creating anyway (advisory; agent-owned set); project=%s",
                        text[:60],
                        (matched.get("text") or "")[:60],
                        matched.get("status"),
                        project.id,
                    )
                success_evidence = str(op_entry.get("success_evidence", "")).strip()
                retirement_condition = str(
                    op_entry.get("retirement_condition", "")
                ).strip()
                if not success_evidence or not retirement_condition:
                    logger.warning(
                        f"[STRATEGIC] Task {task.id}: direction create '{text[:60]}' rejected — "
                        f"missing success_evidence or retirement_condition; strategic agent should include both fields"
                    )
                    continue
                raw_priority = op_entry.get("priority", "medium")
                priority = (
                    raw_priority
                    if raw_priority in ("high", "medium", "low")
                    else "medium"
                )
                _DIR_FIELD_MAX = 500
                new_dir = {
                    "id": f"d-{uuid.uuid4().hex[:8]}",
                    "text": text,
                    "status": "active",
                    "priority": priority,
                    "notes": (
                        [
                            {
                                "text": f"Created by strategic review: {rationale}",
                                "added_at": _utcnow().isoformat(),
                            }
                        ]
                        if rationale
                        else []
                    )
                    + (
                        [
                            {
                                "text": (
                                    "Advisory: resembles existing direction "
                                    f"'{(matched.get('text') or '')[:80]}' "
                                    f"(status={matched.get('status')}) — created on the "
                                    "agent's judgment; retire one if redundant."
                                ),
                                "added_at": _utcnow().isoformat(),
                            }
                        ]
                        if matched
                        else []
                    ),
                    "research_history": [],
                    "context": {
                        "success_evidence": success_evidence[:_DIR_FIELD_MAX],
                        "retirement_condition": retirement_condition[:_DIR_FIELD_MAX],
                    },
                }
                impl_constraints = str(
                    op_entry.get("implementation_constraints", "")
                ).strip()
                if impl_constraints:
                    new_dir["context"]["implementation_constraints"] = impl_constraints[
                        :_DIR_FIELD_MAX
                    ]
                if isinstance(raw_dedup_reasoning, dict):
                    dr: dict = {}
                    closest = raw_dedup_reasoning.get("closest_directions")
                    if isinstance(closest, list):
                        dr["closest_directions"] = closest[:3]
                    distinction = str(
                        raw_dedup_reasoning.get("distinction") or ""
                    ).strip()
                    if distinction:
                        dr["distinction"] = distinction[:_DIR_FIELD_MAX]
                    if dr:
                        new_dir["context"]["dedup_reasoning"] = dr
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
                        logger.warning(
                            f"[STRATEGIC] Task {task.id}: truncating system_id {len(sid)}→{STRATEGIC_SYSTEM_ID_MAX} chars"
                        )
                    new_dir["system_id"] = sid[:STRATEGIC_SYSTEM_ID_MAX]
                if op_entry.get("category") and op_entry["category"] in (
                    "product",
                    "structural",
                ):
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
                        "strategic_cycle_id": str(
                            task.strategic_cycle_id
                            or (task.context or {}).get("strategic_cycle_id")
                            or ""
                        ),
                    }
                current_active = sum(
                    1 for d in directions if d.get("status") == "active"
                )
                if current_active >= _runtime_config.get("max_active_directions", 5):
                    logger.warning(
                        f"[STRATEGIC] Skipping direction create: active count {current_active} >= cap"
                    )
                    continue
                # Translate recommended_mode='pause' to paused status so the
                # existing spawn-skip (status != 'active') mechanism handles it.
                if rec_mode == "pause":
                    new_dir["status"] = "paused"
                    new_dir["auto_paused"] = True
                    new_dir["paused_reason"] = (
                        "Strategic review: recommended_mode=pause"
                    )
                    new_dir["paused_at"] = _utcnow().isoformat()
                directions.append(new_dir)
                ops_applied += 1
                creates_count += 1
                created_ids_fb.append(new_dir["id"])
                if rationale:
                    created_rationales_fb[new_dir["id"]] = rationale[
                        :_RATIONALE_PERSIST_MAX
                    ]
                logger.info(
                    f"[STRATEGIC] Created direction '{text[:60]}' for project {project.id}"
                )

            elif op == "update":
                dir_id = op_entry.get("direction_id")
                changes = op_entry.get("changes", {})
                if not dir_id or not changes:
                    continue
                for d in directions:
                    if d.get("id") == dir_id:
                        # Snapshot contract fields before mutation to detect real changes.
                        _pre_text = d.get("text")
                        _pre_paths = list(d.get("paths") or [])
                        _pre_ctx = d.get("context") or {}
                        _pre_se = _pre_ctx.get("success_evidence")
                        _pre_rc = _pre_ctx.get("retirement_condition")
                        if "text" in changes:
                            new_text = str(changes["text"])
                            if len(new_text) > STRATEGIC_DIRECTION_TEXT_MAX:
                                logger.warning(
                                    f"[STRATEGIC] Task {task.id}: truncating direction text {len(new_text)}→{STRATEGIC_DIRECTION_TEXT_MAX} chars"
                                )
                            d["text"] = new_text[:STRATEGIC_DIRECTION_TEXT_MAX]
                        if "priority" in changes and changes["priority"] in (
                            "high",
                            "medium",
                            "low",
                        ):
                            d["priority"] = changes["priority"]
                        if "paths" in changes:
                            d["paths"] = changes["paths"]
                        if "system_id" in changes:
                            if changes["system_id"]:
                                sid_new = str(changes["system_id"])
                                if len(sid_new) > STRATEGIC_SYSTEM_ID_MAX:
                                    logger.warning(
                                        f"[STRATEGIC] Task {task.id}: truncating system_id {len(sid_new)}→{STRATEGIC_SYSTEM_ID_MAX} chars"
                                    )
                                d["system_id"] = sid_new[:STRATEGIC_SYSTEM_ID_MAX]
                            else:
                                d["system_id"] = None
                        if "status" in changes and changes["status"] in (
                            "active",
                            "paused",
                            "expired",
                            "superseded",
                        ):
                            # Block reactivating a direction the user manually paused
                            if (
                                changes["status"] == "active"
                                and d.get("status") == "paused"
                                and not d.get("auto_paused")
                            ):
                                logger.warning(
                                    f"[STRATEGIC] Blocked reactivation of user-paused direction {dir_id}"
                                )
                            else:
                                d["status"] = changes["status"]
                                if changes["status"] == "superseded":
                                    d["completed_at"] = _utcnow().isoformat()
                                    superseded_by = changes.get("superseded_by")
                                    if not superseded_by:
                                        succ_id = (
                                            changes.get("successor_id")
                                            or changes.get("superseded_by_id")
                                            or op_entry.get("successor_id")
                                            or op_entry.get("superseded_by_id")
                                        )
                                        if succ_id:
                                            superseded_by = {
                                                "direction_id": str(succ_id),
                                                "set_at": _utcnow().isoformat(),
                                            }
                                    if superseded_by:
                                        d["superseded_by"] = superseded_by
                        if "category" in changes:
                            if changes["category"] in (
                                "product",
                                "structural",
                                "mixed",
                            ):
                                d["category"] = changes["category"]
                            elif changes["category"] is None:
                                d.pop("category", None)
                        if "recommended_mode" in changes:
                            new_mode = _normalize_recommended_mode(
                                changes["recommended_mode"]
                            )
                            if new_mode:
                                d["recommended_mode"] = new_mode
                                d[RECOMMENDED_MODE_META_KEY] = {
                                    "mode": new_mode,
                                    "source": RECOMMENDED_MODE_SOURCE_STRATEGIC,
                                    "source_task_id": str(task.id),
                                    "set_at": _utcnow().isoformat(),
                                    "strategic_cycle_id": str(
                                        task.strategic_cycle_id
                                        or (task.context or {}).get(
                                            "strategic_cycle_id"
                                        )
                                        or ""
                                    ),
                                }
                            elif changes["recommended_mode"] is None:
                                d.pop("recommended_mode", None)
                                d.pop(RECOMMENDED_MODE_META_KEY, None)
                        # Also accept top-level recommended_mode on the
                        # operation so the agent can emit it without
                        # nesting it inside a changes block.
                        top_level_mode = _normalize_recommended_mode(
                            op_entry.get("recommended_mode")
                        )
                        if top_level_mode:
                            d["recommended_mode"] = top_level_mode
                            d[RECOMMENDED_MODE_META_KEY] = {
                                "mode": top_level_mode,
                                "source": RECOMMENDED_MODE_SOURCE_STRATEGIC,
                                "source_task_id": str(task.id),
                                "set_at": _utcnow().isoformat(),
                                "strategic_cycle_id": str(
                                    task.strategic_cycle_id
                                    or (task.context or {}).get("strategic_cycle_id")
                                    or ""
                                ),
                            }
                        # Translate recommended_mode='pause' to paused direction
                        # status so existing spawn-skip logic handles it.
                        if (
                            d.get("recommended_mode") == "pause"
                            and d.get("status") == "active"
                        ):
                            d["status"] = "paused"
                            d["auto_paused"] = True
                            d["paused_reason"] = (
                                "Strategic review: recommended_mode=pause"
                            )
                            d["paused_at"] = _utcnow().isoformat()
                        # Persist context-structured fields (same cap as CREATE path)
                        _DIR_FIELD_MAX = 500
                        ctx = d.get("context") or {}
                        _old_se = ctx.get("success_evidence")
                        _old_rc = ctx.get("retirement_condition")
                        for _ctx_field in (
                            "success_evidence",
                            "retirement_condition",
                            "implementation_constraints",
                        ):
                            if _ctx_field in changes:
                                val = str(changes[_ctx_field] or "").strip()
                                if val:
                                    ctx[_ctx_field] = val[:_DIR_FIELD_MAX]
                                else:
                                    ctx.pop(_ctx_field, None)
                        if (
                            ctx.get("success_evidence") != _old_se
                            or ctx.get("retirement_condition") != _old_rc
                        ):
                            _reset_scaffolding_state_on_outcome_change(d)
                        d["context"] = ctx
                        # Reset outcome-detection state when any contract field changed.
                        _new_ctx = d.get("context") or {}
                        if (
                            d.get("text") != _pre_text
                            or list(d.get("paths") or []) != _pre_paths
                            or _new_ctx.get("success_evidence") != _pre_se
                            or _new_ctx.get("retirement_condition") != _pre_rc
                        ):
                            from app.direction_lifecycle import (
                                _reset_outcome_detection_state,
                            )

                            _reset_outcome_detection_state(d)
                        # Record rationale as a note
                        if rationale:
                            notes = d.get("notes", [])
                            notes.append(
                                {
                                    "text": f"Strategic update: {rationale}",
                                    "added_at": _utcnow().isoformat(),
                                }
                            )
                            if len(notes) > STRATEGIC_NOTES_HISTORY_MAX:
                                logger.warning(
                                    f"[STRATEGIC] Task {task.id}: dropping {len(notes) - STRATEGIC_NOTES_HISTORY_MAX} oldest notes on direction {dir_id}"
                                )
                            d["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                        ops_applied += 1
                        logger.info(
                            f"[STRATEGIC] Updated direction {dir_id} for project {project.id}: {list(changes.keys())}"
                        )
                        break

            elif op == "retire":
                dir_id = op_entry.get("direction_id")
                if not dir_id:
                    continue
                for d in directions:
                    if d.get("id") == dir_id:
                        d["status"] = "retired"
                        d["retired_at"] = _utcnow().isoformat()
                        if rationale:
                            notes = d.get("notes", [])
                            notes.append(
                                {
                                    "text": f"Retired by strategic review: {rationale}",
                                    "added_at": _utcnow().isoformat(),
                                }
                            )
                            if len(notes) > STRATEGIC_NOTES_HISTORY_MAX:
                                logger.warning(
                                    f"[STRATEGIC] Task {task.id}: dropping {len(notes) - STRATEGIC_NOTES_HISTORY_MAX} oldest notes on direction {dir_id}"
                                )
                            d["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                        ops_applied += 1
                        retires_applied += 1
                        retired_ids_fb.append(dir_id)
                        if rationale:
                            retired_rationales_fb[dir_id] = rationale[
                                :_RATIONALE_PERSIST_MAX
                            ]
                        logger.info(
                            f"[STRATEGIC] Retired direction {dir_id} for project {project.id}"
                        )
                        break

        if ops_applied > 0:
            from app.task_locking import with_directions_locked

            _dirs = list(directions)
            await with_directions_locked(session, task.project_id, lambda _: _dirs)

    # Apply version_phase_decision + qualify_request from the parsed output
    # (fallback / log-parse path). In practice the agent emits both fields
    # only through the set_problem_model API (which forces the API path
    # above), so this fallback is effectively unreached today — but the two
    # paths are kept symmetric so a future fallback-only emission path can't
    # silently drop one signal. Shapes: version_phase_decision = {phase:
    # "build"|"stabilize", reasoning: str}; qualify_request = {reason: str}.
    _vpd_parsed = parsed.get("version_phase_decision")
    await _apply_version_phase_decision(
        _vpd_parsed, task, project, session, path="fallback"
    )
    _qr_parsed = parsed.get("qualify_request")
    await _apply_qualify_request(_qr_parsed, task, project, session, path="fallback")

    # Update cycle history with final outcome
    # Audit W2: cycle outcome lives on the StrategicCycle row.
    cycle_id = task.strategic_cycle_id or (task.context or {}).get("strategic_cycle_id")
    dirs_created = (
        creates_count if direction_ops and isinstance(direction_ops, list) else 0
    )
    if cycle_id:
        dirs_retired = retires_applied
        vision_produced = bool(new_vision and isinstance(new_vision, dict))
        _active_dirs_fb = sum(
            1 for d in (project.directions or []) if d.get("status") == "active"
        )
        _vision_rev_fb = (project.vision or {}).get("revision", 0)
        _summary_fb = f"rev {_vision_rev_fb}, {_active_dirs_fb} active, {dirs_created} created, {dirs_retired} retired"
        if vision_produced:
            _fb_extra: dict = {
                "created_direction_ids": list(created_ids_fb),
                "retired_direction_ids": list(retired_ids_fb),
                "created_rationales": dict(created_rationales_fb),
                "retired_rationales": dict(retired_rationales_fb),
                "created_predictions": _collect_creation_predictions(
                    project, created_ids_fb
                ),
                "retired_final_metrics": _collect_retirement_metrics(
                    project, retired_ids_fb
                ),
            }
            _fb_critiques = parsed.get("retired_direction_critiques")
            if _fb_critiques is not None:
                _fb_extra["retirement_critiques"] = _fb_critiques
            await strategic_cycle.mark_phase2_completed(
                session,
                cycle_id,
                summary_text=_summary_fb,
                cycle_reasoning=_extract_cycle_reasoning(task),
                directions_created=dirs_created,
                directions_retired=dirs_retired,
                vision_updated=True,
                extra_metadata=_fb_extra,
            )
        else:
            await strategic_cycle.mark_failure(
                session,
                cycle_id,
                phase="phase2",
                reason="strategic_review fallback path produced no vision",
            )

    await _check_direction_starvation(project, dirs_created, session)
    session.add(project)

    # Deferred import of message/resolution caps — same STRATEGIC_* namespace
    from app.strategic_constants import (
        STRATEGIC_MESSAGE_CONTENT_MAX,
        STRATEGIC_RESOLUTION_MAX,
    )

    # Process messages from agent (questions, blockers, notes)
    msgs_created = 0
    for msg_entry in (parsed.get("messages") or [])[:5]:
        if not isinstance(msg_entry, dict):
            continue
        content = (msg_entry.get("content") or "").strip()
        if not content:
            continue
        if len(content) > STRATEGIC_MESSAGE_CONTENT_MAX:
            logger.warning(
                f"[STRATEGIC] Task {task.id}: truncating message content {len(content)}→{STRATEGIC_MESSAGE_CONTENT_MAX} chars"
            )
            content = content[:STRATEGIC_MESSAGE_CONTENT_MAX]
        try:
            msg_type = MessageType(msg_entry.get("type", "NOTE").upper())
        except ValueError:
            msg_type = MessageType.NOTE
        _msg_ctx = msg_entry.get("context") or {}
        new_msg = ProjectMessage(
            project_id=project.id,
            message_type=msg_type,
            from_type="strategic",
            from_task_id=task.id,
            content=content,
            context=_msg_ctx,
        )
        session.add(new_msg)
        _event_type = _msg_ctx.get("event_type") if isinstance(_msg_ctx, dict) else None
        if _event_type:
            from app.routers.admin_notifications import (
                ROUTINE_EVENT_TYPES as _ROUTINE_EVENT_TYPES,
                append_routine_digest as _append_routine_digest,
            )

            if _event_type in _ROUTINE_EVENT_TYPES:
                _append_routine_digest(project, _event_type, content)
        msgs_created += 1

    # Resolve messages the agent addressed
    msgs_resolved = 0
    # Support both formats: resolved_messages (with resolution text) and resolved_message_ids (legacy)
    resolved_entries = parsed.get("resolved_messages") or []
    if not resolved_entries:
        # Fall back to legacy format (plain ID list)
        resolved_entries = [
            {"id": mid} for mid in (parsed.get("resolved_message_ids") or [])
        ]
    for entry in resolved_entries[:10]:
        try:
            msg_id = entry.get("id") if isinstance(entry, dict) else str(entry)
            resolution_text = (
                entry.get("resolution", "Addressed by strategic review")
                if isinstance(entry, dict)
                else "Addressed by strategic review"
            )
            msg = await session.get(ProjectMessage, uuid.UUID(str(msg_id)))
            if msg and msg.project_id == project.id and msg.status == "open":
                msg.status = "resolved"
                if resolution_text:
                    rt = str(resolution_text)
                    if len(rt) > STRATEGIC_RESOLUTION_MAX:
                        logger.warning(
                            f"[STRATEGIC] Task {task.id}: truncating resolution {len(rt)}→{STRATEGIC_RESOLUTION_MAX} chars"
                        )
                    msg.resolution = rt[:STRATEGIC_RESOLUTION_MAX]
                else:
                    msg.resolution = "Addressed by strategic review"
                msg.resolved_at = _utcnow()
                session.add(msg)
                msgs_resolved += 1
                if msg.message_type == MessageType.FEEDBACK:
                    try:
                        from app.notification_dispatch import (
                            post_feedback_acknowledgment,
                        )

                        await post_feedback_acknowledgment(
                            session,
                            project.id,
                            msg.id,
                            msg.resolution or "Addressed by strategic review",
                            task.id,
                        )
                    except Exception as _e:  # noqa: BLE001
                        logger.warning(
                            f"[NOTIFY] Failed to post feedback acknowledgment: {_e}"
                        )
        except (ValueError, TypeError):  # noqa: PERF203
            pass

    # Audit: check for unaddressed FEEDBACK messages
    remaining_feedback_result = await session.execute(
        select(func.count())
        .select_from(ProjectMessage)
        .where(
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
    await _spawn_retirement_memos(
        session, project, task, retired_ids_fb, retired_rationales_fb
    )
    await (
        session.commit()
    )  # Single commit: vision + directions + messages + task status
    # Post-commit: notify the admin UI of every strategist output this
    # legacy-parse path wrote, then close the cycle. Tiny refetch signals.
    from app.strategic_events import (
        STRATEGIC_KIND_DIRECTIONS,
        STRATEGIC_KIND_PROBLEM_MODEL,
        STRATEGIC_KIND_RUNNING_UPDATE,
        emit_strategic_cycle_completed,
        emit_strategic_updated,
    )

    if isinstance(new_running_update, str):
        await emit_strategic_updated(task.project_id, STRATEGIC_KIND_RUNNING_UPDATE)
    if new_problem_model and isinstance(new_problem_model, dict):
        await emit_strategic_updated(task.project_id, STRATEGIC_KIND_PROBLEM_MODEL)
    if ops_applied or retires_applied:
        await emit_strategic_updated(task.project_id, STRATEGIC_KIND_DIRECTIONS)
    await emit_strategic_cycle_completed(task.project_id, task.id, 2)
    logger.info(
        f"[STRATEGIC] Task {task.id} complete: vision rev {vision_rev}, {ops_applied} direction ops, {msgs_created} msgs created, {msgs_resolved} resolved"
    )
