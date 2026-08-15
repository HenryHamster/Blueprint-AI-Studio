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
from app.direction_metrics import (
    _agg_proof_weighted_merged,
    _compute_direction_quality_score,
    proof_weighted_merged,
)
from app.direction_outcomes import credit_gate_merged_count, verified_merged_count
from app.direction_referential_guard import (
    build_retired_direction_grounding,
    classify_retired_direction_outcome,
)
from app.direction_grounding import validate_create_direction_grounding
from app.direction_contract import (
    DIRECTION_CONTRACT_FIELD_MAX,
    normalize_direction_contract_changes,
    normalize_direction_contract_field,
)

# Stopword set reused so the spawn-title redundancy check matches the project's
# established word-overlap semantics (same tokens ignored: articles,
# conjunctions, and verb prefixes like "feat"/"fix").
from app.finding_deduplication import _STOPWORDS as _SPAWN_STOPWORDS
from app.models import (
    KnowledgeDocument,
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    RECOMMENDED_MODE_META_KEY,
    RECOMMENDED_MODE_SOURCE_STRATEGIC,
    SourceKind,
    Task,
    TaskStatus,
)
from app.broadcast import broadcast_to_project
from app.message_audience import stamp_message_audience
from app.project_message_creation import build_project_message
from app.project_message_projection import message_event_payload
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# --- Gap-respawn observe-mode observability ------------------------------------

#: Lane discriminator for AuditLaneDecisionLog rows recording observe-mode
#: would-have-blocked events from the direction_gap_respawn_block_mode check.
_GAP_RESPAWN_LANE = "gap_respawn"

#: Bounded in-memory ring of recent observe-mode would-have-blocked events
#: (mirrors the pattern in evergreen_backlog / structural_health_audit).
from collections import deque as _deque  # noqa: E402
_GAP_RESPAWN_OBSERVE_RING: _deque = _deque(maxlen=50)  # type: ignore[type-arg]


async def _record_gap_respawn_observe_event(
    session: Any,
    *,
    ts: Any,
    direction_text: str,
    gap_id: str | None,
    matched_retired_id: str,
    match_kind: str,
    project_id: Any,
) -> None:
    """Record an observe-mode would-have-blocked event durably and in-memory.

    Writes one row to ``AuditLaneDecisionLog`` (lane='gap_respawn') so the
    mode-knob graduation harness can read a durable multi-day window, and
    appends to the bounded in-memory ring for live operator observability.
    Best-effort: a DB error is logged and swallowed.
    """
    from app.models import AuditLaneDecisionLog

    payload = {
        "direction_text": direction_text[:200],
        "gap_id": gap_id,
        "matched_retired_id": matched_retired_id,
        "match_kind": match_kind,
        "project_id": str(project_id) if project_id else None,
    }

    # In-memory ring (bounded, process-local).
    _GAP_RESPAWN_OBSERVE_RING.append({
        "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "direction_text": direction_text[:200],
        "gap_id": gap_id,
        "matched_retired_id": matched_retired_id,
        "match_kind": match_kind,
    })

    # Durable DB row.
    try:
        row = AuditLaneDecisionLog(
            ts=ts,
            lane=_GAP_RESPAWN_LANE,
            mode="observe",
            tripped=True,
            enrolled=0,
            payload=payload,
        )
        session.add(row)
        await session.flush()
    except Exception:
        logger.warning(
            "[STRATEGIC] _record_gap_respawn_observe_event failed to persist",
            exc_info=True,
        )


def get_gap_respawn_observability() -> dict:
    """Return the current in-memory gap-respawn observability snapshot."""
    return {
        "would_have_blocked_recent": list(_GAP_RESPAWN_OBSERVE_RING),
        "would_have_blocked_count": len(_GAP_RESPAWN_OBSERVE_RING),
    }


def _reset_gap_respawn_observability_for_tests() -> None:
    """Reset the in-memory ring (test isolation only)."""
    _GAP_RESPAWN_OBSERVE_RING.clear()


def spawn_budget_status(
    per_review_count: int,
    window_count: int,
    *,
    per_review_cap: int,
    window_cap: int,
) -> tuple[bool, str | None, str | None]:
    """Decide whether one more strategist spawn fits within budget.

    Pure policy helper (no DB) so the bound is independently unit-testable. The
    handler counts prior spawns and passes them in; this returns whether the
    next spawn is allowed.

    ``per_review_count`` — strategic_direct spawns already created by THIS review.
    ``window_count`` — project-wide strategic_direct spawns in the rolling window.

    Returns ``(ok, scope, reason)``:
      * ``ok`` is False when either count has reached (>=) its cap.
      * ``scope`` is ``"per_review"`` or ``"window"`` identifying which cap was
        hit (the per-review cap takes precedence when both are exhausted), else
        ``None``.
      * ``reason`` is a short human-readable explanation, or ``None`` when ok.

    A non-positive cap disables that dimension (never blocks).
    """
    if per_review_cap > 0 and per_review_count >= per_review_cap:
        return (
            False,
            "per_review",
            f"Per-review spawn cap reached: {per_review_count}/{per_review_cap}",
        )
    if window_cap > 0 and window_count >= window_cap:
        return (
            False,
            "window",
            f"Rolling-window spawn cap reached: {window_count}/{window_cap}",
        )
    return True, None, None


def _ctx_list(ctx: dict, key: str) -> list[str]:
    values = ctx.get(key)
    if not isinstance(values, list):
        return []
    return [str(v) for v in values if v]


def _strategic_event_completion_summary(
    task: Task,
    project: Project | None,
    *,
    cycle=None,
    status: str = "completed",
    status_message: str | None = None,
    failure_reason: str | None = None,
    created_ids: list | None = None,
    retired_ids: list | None = None,
    staged_ids: list | None = None,
    updated_ids: list | None = None,
    problem_model_changed: bool | None = None,
    running_update_changed: bool | None = None,
) -> dict[str, Any]:
    ctx = task.context or {}
    meta = getattr(cycle, "cycle_metadata", None) or {}
    created_source = created_ids or _ctx_list(ctx, "_strategic_created_ids")
    retired_source = retired_ids or _ctx_list(ctx, "_strategic_retired_ids")
    approved_count = int(ctx.get("_strategic_proposals_approved") or 0)
    rejected_count = int(ctx.get("_strategic_proposals_rejected") or 0)
    created = [
        str(v) for v in (created_source or meta.get("created_direction_ids", [])) if v
    ]
    retired = [
        str(v) for v in (retired_source or meta.get("retired_direction_ids", [])) if v
    ]
    staged = [
        str(v) for v in (staged_ids or _ctx_list(ctx, "_strategic_staged_ids")) if v
    ]
    updated = [
        str(v) for v in (updated_ids or _ctx_list(ctx, "_strategic_updated_ids")) if v
    ]
    created_count = int(
        ctx.get("_strategic_dirs_created")
        or getattr(cycle, "directions_created", 0)
        or len(created)
    )
    retired_count = int(
        ctx.get("_strategic_dirs_retired")
        or getattr(cycle, "directions_retired", 0)
        or len(retired)
        or rejected_count
    )
    staged_count = int(ctx.get("_strategic_dirs_staged") or len(staged))
    updated_count = int(
        ctx.get("_strategic_dirs_updated") or len(updated) or approved_count
    )
    tasks_spawned = int(
        ctx.get("_strategic_tasks_spawned") or getattr(cycle, "tasks_spawned", 0) or 0
    )
    adoption_declared = int(ctx.get("_strategic_adoption_declared") or 0)
    pm_changed = (
        bool(ctx.get("_strategic_problem_model_set"))
        if problem_model_changed is None
        else bool(problem_model_changed)
    )
    ru_changed = (
        bool(ctx.get("_strategic_running_update_set"))
        if running_update_changed is None
        else bool(running_update_changed)
    )
    summary: dict[str, Any] = {
        "status": status,
        "status_message": status_message or task.latest_status_message,
        "failure_reason": failure_reason or getattr(cycle, "failure_reason", None),
        "created_direction_ids": created,
        "retired_direction_ids": retired,
        "staged_direction_ids": staged,
        "updated_direction_ids": updated,
        "created_count": created_count,
        "retired_count": retired_count,
        "staged_count": staged_count,
        "updated_count": updated_count,
        "tasks_spawned": tasks_spawned,
        "directions_changed": bool(
            created_count or retired_count or staged_count or updated_count
        ),
        "problem_model_changed": pm_changed,
        "running_update_changed": ru_changed,
        "active_direction_count": sum(
            1
            for d in ((project.directions if project else []) or [])
            if d.get("status") == "active"
        ),
    }
    if adoption_declared > 0:
        summary["adoption_declared_count"] = adoption_declared
    # Compact strategic-judgment-challenge summary: rides the completion dict into
    # the websocket payload and digest. The full bounded record stays in
    # cycle_metadata for drill-down; only {mode, verdict, warned/blocked counts}
    # travel here. Prefer the durable cycle_metadata copy, fall back to the
    # in-flight task.context record; omit the key entirely for legacy cycles with
    # no challenge so absence never reads as a failure.
    _challenge_record = meta.get("strategic_judgment_challenge") or ctx.get(
        STRATEGIC_CHALLENGE_CONTEXT_KEY
    )
    _challenge_summary = strategic_challenge_summary(_challenge_record)
    if _challenge_summary is not None:
        summary["strategic_judgment_challenge"] = _challenge_summary
    _retirement_deferrals = meta.get("retirement_deferrals")
    if isinstance(_retirement_deferrals, list) and _retirement_deferrals:
        summary["retirement_deferrals"] = _retirement_deferrals
    # Direction ops dropped for invalid criterion_keys — surfaced so the
    # rejection reaches the operator (websocket payload + digest) instead of
    # vanishing as a log line. Bounded at record time; capped again here.
    _rejected_ops = ctx.get(REJECTED_DIRECTION_OPS_CONTEXT_KEY)
    if isinstance(_rejected_ops, list) and _rejected_ops:
        summary["rejected_direction_ops"] = _rejected_ops[-_REJECTED_DIRECTION_OPS_MAX:]
    return summary


def spawn_title_is_redundant(
    title: str,
    existing_titles,
    *,
    threshold: int,
) -> str | None:
    """Return the first existing title that semantically duplicates ``title``.

    Broadens the exact-title idempotency guard to collapse near-duplicate
    investigations that differ only in wording. Uses the same word-overlap /
    stopword semantics as ``finding_deduplication.is_duplicate`` (overlap of
    non-stopword tokens at or above ``threshold`` is a match). Returns the
    matched existing title, or ``None`` when nothing overlaps enough.

    ``threshold`` must stay >=4 so genuinely distinct single-word-differing
    titles (e.g. "Investigate A" vs "Investigate B", overlap 1) are not
    collapsed.
    """
    words = set(title.lower().split()) - _SPAWN_STOPWORDS
    if not words:
        return None
    for existing in existing_titles:
        overlap = words & (set(existing.lower().split()) - _SPAWN_STOPWORDS)
        if len(overlap) >= threshold:
            return existing
    return None


async def _record_strategic_task_failure(
    task: Task,
    session: AsyncSession,
    *,
    project: Project | None = None,
) -> None:
    """Record a FAILED strategic task in the circuit breaker.

    Called from _process_post_commit when a design_doc_update or strategic_review
    task terminates with FAILED status (VM crash, timeout, etc.).

    Commit semantics (R5-W5 audit MED-1):
    -----------------------------------
    This function does NOT commit. It mutates ``project.vision`` and then
    ``session.add(project)``.
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

# Expensive product probes are selected by the strategist at the qualification
# decision, not by a fleet-wide timer/flag.  Keep this closed and deliberately
# small: each supported value names a real, decision-bearing qualifier child.
_QUALIFY_REQUEST_PROBE_KINDS = frozenset({"walkthrough_e2e"})

# Transient task.context key carrying an agent-emitted qualify_request from the
# set_problem_model endpoint to _handle_strategic_completion's API path. Mirrors
# VERSION_PHASE_DECISION_CONTEXT_KEY — one source of truth so producer + consumer
# never drift.
QUALIFY_REQUEST_CONTEXT_KEY = "_strategic_qualify_request"

# Transient task.context key carrying an agent-emitted new_version_goal from the
# set_problem_model endpoint to _handle_strategic_completion's API path. Mirrors
# QUALIFY_REQUEST_CONTEXT_KEY — one source of truth so producer + consumer never drift.
NEW_VERSION_GOAL_CONTEXT_KEY = "_strategic_new_version_goal"

# Cap on the new_version_goal string stored in task.context (and eventually on
# ReleaseVersion.goal).  Matches the display budget for version goal UI surfaces.
_NEW_VERSION_GOAL_MAX = 500

# Transient task.context key carrying agent-emitted new_acceptance_criteria from the
# set_problem_model endpoint to _handle_strategic_completion's API path. Mirrors
# NEW_VERSION_GOAL_CONTEXT_KEY — one source of truth so producer + consumer never drift.
NEW_ACCEPTANCE_CRITERIA_CONTEXT_KEY = "_strategic_new_acceptance_criteria"

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

# Transient task.context key carrying direction create/update ops that were
# REJECTED because their criterion_keys reference unknown (dangling) or
# retracted/superseded (inactive) acceptance criteria. The fallback parse path
# used to drop these ops with only a log line, so the strategist never learned
# the link was refused. The bounded list is recorded on task.context, surfaced
# in the completion summary and cycle_metadata, and re-surfaced in the next
# cycle's payload so the strategist can remap to an active criterion.
REJECTED_DIRECTION_OPS_CONTEXT_KEY = "_strategic_rejected_direction_ops"
_REJECTED_DIRECTION_OPS_MAX = 10
# Per-entry field caps so the recorded list stays bounded regardless of the
# agent's proposed text/keys (mirrors the API validation error surface).
_REJECTED_OP_TEXT_MAX = 120
_REJECTED_OP_KEYS_MAX = 20


# ── Strategic judgment challenge record ──────────────────────────────────────
# An independent adversarial SECOND OPINION over the strategist's OWN
# self-produced judgments — evidence_progress ratings, retirement critiques, and
# portfolio-shape decisions — recorded BEFORE the executor commits strategic
# state. The record is bounded + versioned, stored on task.context under
# STRATEGIC_CHALLENGE_CONTEXT_KEY by the set_strategic_challenge endpoint (which
# deliberately does NOT flip _strategic_api_called, so a challenge-only cycle
# cannot masquerade as a successful executor mutation), copied verbatim into
# StrategicCycle.cycle_metadata at completion, and consulted by the first hard
# gate: an advancing/met evidence_progress verdict may only SUPPRESS the
# deterministic outcome-scaffolding flag when the challenge APPROVED the same
# direction_id + status judgment. The same fail-closed shape gates agent-authored
# retirements when the runtime rollout mode reaches enforce. A rejected /
# uncertain / missing / malformed challenge approval leaves the deterministic
# backstop active and never approves a retirement.
STRATEGIC_CHALLENGE_CONTEXT_KEY = "_strategic_judgment_challenge"
STRATEGIC_CHALLENGE_RECORD_VERSION = 1
_VALID_CHALLENGE_VERDICTS = frozenset({"approved", "rejected", "uncertain"})
_CHALLENGE_RATIONALE_MAX = 300
# Section-block verdict vocabulary emitted by the challenger prompt:
#   retirement_critique_quality: solid (clean), shallow (warn), not_assessed (neutral)
#   portfolio_shape_risk:        low (clean),   elevated (warn), high (block)
# Standard evidence-progress vocab (rejected/uncertain) is also accepted for
# backwards compatibility and for tests that seed sections directly.
_SECTION_BLOCK_VERDICTS = frozenset({"rejected", "high"})
_SECTION_WARN_VERDICTS = frozenset({"uncertain", "shallow", "elevated"})
_CHALLENGE_SUBJECT_MAX = 120
# Per-section cap on challenge items so the JSON blob stored on task.context and
# copied into cycle_metadata stays bounded regardless of agent output size.
_CHALLENGE_MAX_ITEMS = 50
_RETIREMENT_APPROVALS_KEY = "retirement_approvals"
_PORTFOLIO_OP_ALIASES = {
    "create": "create_direction",
    "update": "update_direction",
    "retire": "retire_direction",
    "stage": "stage_direction",
    "approve": "approve_direction",
    "reject": "reject_direction",
    "activate": "activate_direction",
}
_VALID_PORTFOLIO_OPS = frozenset(
    {
        "create_direction",
        "update_direction",
        "retire_direction",
        "stage_direction",
        "approve_direction",
        "reject_direction",
        "activate_direction",
    }
)
_VALID_PORTFOLIO_VERDICTS = frozenset({"approved", "rejected", "warned", "uncertain"})
_PORTFOLIO_BLOCK_VERDICTS = frozenset({"rejected"})
_PORTFOLIO_WARN_VERDICTS = frozenset({"warned", "uncertain"})
_HARD_BLOCK_PORTFOLIO_OPS = frozenset(
    {
        "create_direction",
        "update_direction",
        "retire_direction",
        "stage_direction",
        "approve_direction",
        "reject_direction",
        "activate_direction",
    }
)
def _sanitize_challenge_verdict_block(
    raw: object,
    *,
    default_verdict: str,
    default_rationale: str = "",
) -> dict:
    if not isinstance(raw, dict):
        return {
            "verdict": default_verdict,
            "rationale": default_rationale[:_CHALLENGE_RATIONALE_MAX],
        }
    verdict = (
        str(raw.get("verdict", "")).strip().lower()
        or str(raw.get("assessment", "")).strip().lower()
    )
    rationale = (
        str(raw.get("rationale", "")).strip() or str(raw.get("reason", "")).strip()
    )
    return {
        "verdict": (verdict or default_verdict)[:64],
        "rationale": rationale[:_CHALLENGE_RATIONALE_MAX],
    }


def sanitize_strategic_challenge_record(raw: object) -> dict | None:
    """Validate + bound a raw strategic-judgment-challenge record.

    Accepts the agent's challenge payload and returns a sanitized, versioned
    dict::

        {
          "challenge_version": STRATEGIC_CHALLENGE_RECORD_VERSION,
          "version": STRATEGIC_CHALLENGE_RECORD_VERSION,
          "status": "ok|blocked|invalid",
          "evidence_progress": [
              {"direction_id", "status", "verdict", "rationale"}, ...
          ],
          "retirement_approvals": [
              {"direction_id", "verdict", "rationale"}, ...
          ],
          "retirement_critique_quality": {"verdict", "rationale"},
          "portfolio_shape_risk": {"verdict", "rationale"},
          "notes": "..."
        }

    Returns ``None`` only when ``raw`` is not a dict at all (the caller maps
    that to a 400). A dict with no usable judgments in any section is preserved
    as a valid EMPTY record — the strategist may legitimately have found nothing
    to challenge, and recording the empty pass still proves a challenge ran.

    Per-item sanitation is lenient: an item with an unknown verdict / status, a
    missing identifier, or a non-dict shape is dropped (not raised) so a single
    malformed entry never discards the rest of the record. Every section is
    capped at ``_CHALLENGE_MAX_ITEMS`` and every string is length-bounded.
    """
    if not isinstance(raw, dict):
        return None

    version_raw = raw.get("challenge_version", raw.get("version"))
    version = (
        version_raw
        if isinstance(version_raw, int) and version_raw > 0
        else STRATEGIC_CHALLENGE_RECORD_VERSION
    )
    status_raw = str(raw.get("status", "")).strip().lower()
    status = status_raw if status_raw in {"ok", "blocked", "invalid"} else "invalid"
    record: dict[str, Any] = {
        "challenge_version": version,
        "version": version,
        "status": status,
    }

    ep_in = raw.get("evidence_progress")
    ep_items: list[dict] = []
    if isinstance(ep_in, list):
        for item in ep_in[:_CHALLENGE_MAX_ITEMS]:
            if not isinstance(item, dict):
                continue
            did = str(item.get("direction_id", "")).strip()
            item_status = str(item.get("status", "")).strip().lower()
            verdict = str(item.get("verdict", "")).strip().lower()
            if (
                not did
                or item_status not in _VALID_EVIDENCE_PROGRESS
                or verdict not in _VALID_CHALLENGE_VERDICTS
            ):
                continue
            ep_items.append(
                {
                    "direction_id": did[:64],
                    "status": item_status,
                    "verdict": verdict,
                    "rationale": str(item.get("rationale", "")).strip()[
                        :_CHALLENGE_RATIONALE_MAX
                    ],
                }
            )
    record["evidence_progress"] = ep_items
    retirement_in = raw.get(_RETIREMENT_APPROVALS_KEY)
    retirement_items: list[dict] = []
    if isinstance(retirement_in, list):
        for item in retirement_in[:_CHALLENGE_MAX_ITEMS]:
            if not isinstance(item, dict):
                continue
            did = str(item.get("direction_id", "")).strip()
            verdict = str(item.get("verdict", "")).strip().lower()
            if not did or verdict not in _VALID_CHALLENGE_VERDICTS:
                continue
            retirement_items.append(
                {
                    "direction_id": did[:64],
                    "verdict": verdict,
                    "rationale": str(item.get("rationale", "")).strip()[
                        :_CHALLENGE_RATIONALE_MAX
                    ],
                }
            )
    record[_RETIREMENT_APPROVALS_KEY] = retirement_items
    record["retirement_critique_quality"] = _sanitize_challenge_verdict_block(
        raw.get("retirement_critique_quality"),
        default_verdict="not_assessed",
    )
    record["portfolio_shape_risk"] = _sanitize_challenge_verdict_block(
        raw.get("portfolio_shape_risk"),
        default_verdict="not_assessed",
    )
    record["portfolio_operations"] = (
        _sanitize_portfolio_operations(raw.get("portfolio_operations"))
        if status == "ok"
        else []
    )
    notes = raw.get("notes")
    if isinstance(notes, str):
        cleaned_notes = notes.strip()[:_CHALLENGE_RATIONALE_MAX]
        if cleaned_notes:
            record["notes"] = cleaned_notes

    return record


def _canonical_portfolio_op(op: object) -> str | None:
    """Return a canonical portfolio op name or None for unknown ops."""
    name = str(op or "").strip().lower()
    if name in _VALID_PORTFOLIO_OPS:
        return name
    return _PORTFOLIO_OP_ALIASES.get(name)


def portfolio_operation_fingerprint(
    op: object,
    *,
    direction_id: object = None,
    text: object = None,
) -> str:
    """Deterministic fingerprint for a portfolio mutation."""
    canon = _canonical_portfolio_op(op) or str(op or "").strip().lower()
    did = str(direction_id or "").strip()
    if did:
        return f"{canon}:{did}"
    norm = " ".join(str(text or "").strip().lower().split())
    if norm:
        digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
        return f"{canon}:text:{digest}"
    return f"{canon}:"


def _sanitize_portfolio_operations(raw: object) -> list[dict]:
    """Validate + bound the per-operation portfolio challenge verdict list."""
    if not isinstance(raw, list):
        return []
    items: list[dict] = []
    seen: set[str] = set()
    for entry in raw[:_CHALLENGE_MAX_ITEMS]:
        if not isinstance(entry, dict):
            continue
        canon = _canonical_portfolio_op(entry.get("op"))
        if canon is None:
            continue
        verdict = str(entry.get("verdict", "")).strip().lower()
        if verdict not in _VALID_PORTFOLIO_VERDICTS:
            continue
        did = str(entry.get("direction_id", "")).strip()
        txt = str(entry.get("text", "")).strip()
        # A "stage_direction" verdict that carries text but no direction_id is really a
        # vote to STAGE A NEW direction — which executes as create_direction(staged=true),
        # since the stage path cannot create. Canonicalize so the stored verdict
        # fingerprint matches the create_direction op the strategist actually runs;
        # otherwise an APPROVED staged-create is hard-blocked as "no matching verdict".
        if canon == "stage_direction" and txt and not did:
            canon = "create_direction"
        fp = portfolio_operation_fingerprint(
            canon, direction_id=did or None, text=txt or None
        )
        if fp in seen:
            continue
        seen.add(fp)
        items.append(
            {
                "op": canon,
                "direction_id": did[:64],
                "fingerprint": fp,
                "verdict": verdict,
                "rationale": str(entry.get("rationale", "")).strip()[
                    :_CHALLENGE_RATIONALE_MAX
                ],
            }
        )
    return items


def portfolio_op_challenge_verdict(
    record: object,
    op: object,
    *,
    direction_id: object = None,
    text: object = None,
) -> dict | None:
    """Return the stored per-operation challenge item matching this mutation."""
    if not isinstance(record, dict):
        return None
    target_fp = portfolio_operation_fingerprint(
        op, direction_id=direction_id, text=text
    )
    for item in record.get("portfolio_operations") or []:
        if isinstance(item, dict) and item.get("fingerprint") == target_fp:
            return item
    return None


def challenge_rejects_portfolio_op(
    record: object,
    op: object,
    *,
    direction_id: object = None,
    text: object = None,
) -> tuple[bool, str]:
    """Whether a stored challenge HARD-BLOCKS this portfolio mutation.

    Returns False only when the op is benign, the challenge is absent, or an
    explicitly ok challenge approved/warned the matching mutation.
    """
    canon = _canonical_portfolio_op(op)
    if canon is None or canon not in _HARD_BLOCK_PORTFOLIO_OPS:
        return (False, "")
    if record is None:
        return (False, "")
    if not isinstance(record, dict):
        return (True, "challenge record was malformed")
    if str(record.get("status", "")).strip().lower() != "ok":
        return (
            True,
            f"challenge status was {str(record.get('status', 'missing')).strip().lower()}",
        )
    item = portfolio_op_challenge_verdict(
        record, canon, direction_id=direction_id, text=text
    )
    if item is None:
        return (True, "no matching portfolio_operations verdict")
    verdict = str(item.get("verdict", "")).strip().lower()
    if verdict in _PORTFOLIO_BLOCK_VERDICTS:
        return (True, str(item.get("rationale", "")).strip())
    return (False, "")


def challenge_approves_evidence_progress(
    record: object, direction_id: str, status: str
) -> bool:
    """True iff the challenge record APPROVED this exact (direction_id, status).

    The first hard gate: an advancing/met evidence_progress self-rating may only
    suppress the deterministic scaffolding backstop when an independent challenge
    explicitly endorsed it. Any other outcome — a ``rejected`` or ``uncertain``
    verdict, a verdict for a different status, a missing direction entry, or a
    missing / malformed record — returns False, so the deterministic detector
    stays active.
    """
    if not isinstance(record, dict):
        return False
    # Only ok-status records can approve anything; blocked/invalid records
    # must not suppress the deterministic scaffolding backstop.
    if record.get("status") != "ok":
        return False
    target_status = (status or "").strip().lower()
    target_id = (direction_id or "").strip()
    if not target_id or not target_status:
        return False
    for item in record.get("evidence_progress") or []:
        if not isinstance(item, dict):
            continue
        if (
            str(item.get("direction_id", "")).strip() == target_id
            and str(item.get("status", "")).strip().lower() == target_status
            and str(item.get("verdict", "")).strip().lower() == "approved"
        ):
            return True
    return False


def strategic_challenge_summary(record: object) -> dict | None:
    """Derive a compact, websocket-safe summary of a strategic-judgment challenge.

    Consumes a sanitized challenge record — the bounded shape produced by
    :func:`sanitize_strategic_challenge_record` and persisted verbatim into
    ``StrategicCycle.cycle_metadata['strategic_judgment_challenge']`` — and
    returns four scalar fields suitable for completion receipts, recent-cycle
    summaries, and digest cards WITHOUT shipping the full bounded item list::

        {
          "mode": "ok" | "blocked" | "invalid",         # the record's own status
          "verdict": "approved" | "warned" | "blocked",  # rolled-up overall verdict
          "warned_count": int,    # challenged judgments the challenger could not confirm
          "blocked_count": int,   # challenged judgments the challenger disputed
        }

    ``blocked_count`` / ``warned_count`` tally every challenged judgment across the
    ``evidence_progress`` items, ``retirement_approvals`` items, the per-operation
    ``portfolio_operations`` verdicts, AND the two section verdict blocks
    (``retirement_critique_quality``, ``portfolio_shape_risk``).

    For ``evidence_progress`` items the standard vocabulary applies: ``rejected``
    → blocked, ``uncertain`` → warned, everything else → clean.

    For the two section blocks the challenger prompt uses a different vocabulary:
    ``retirement_critique_quality`` emits ``solid`` (clean) / ``shallow`` (warned) /
    ``not_assessed`` (clean); ``portfolio_shape_risk`` emits ``low`` (clean) /
    ``elevated`` (warned) / ``high`` (blocked). The standard ``rejected`` /
    ``uncertain`` values are also accepted for backward compatibility.

    The rolled-up ``verdict`` is ``blocked`` when anything was blocked, else
    ``warned`` when anything was warned, else ``approved``.

    Returns ``None`` when ``record`` is not a dict (legacy cycles carrying no
    challenge record), so callers can omit the field entirely rather than emit a
    misleading zeroed summary that would read as a failure.
    """
    if not isinstance(record, dict):
        return None

    warned = 0
    blocked = 0

    def _tally(verdict: object) -> None:
        # Standard evidence-progress vocabulary: approved / rejected / uncertain.
        nonlocal warned, blocked
        v = str(verdict or "").strip().lower()
        if v == "rejected":
            blocked += 1
        elif v == "uncertain":
            warned += 1

    def _tally_section(verdict: object) -> None:
        # Section-block vocabulary from the challenger prompt, plus standard
        # rejected/uncertain for backward compatibility.
        nonlocal warned, blocked
        v = str(verdict or "").strip().lower()
        if v in _SECTION_BLOCK_VERDICTS:
            blocked += 1
        elif v in _SECTION_WARN_VERDICTS:
            warned += 1

    def _tally_portfolio(verdict: object) -> None:
        # Per-operation portfolio vocabulary: rejected -> blocked, warned/
        # uncertain -> warned, approved -> clean.
        nonlocal warned, blocked
        v = str(verdict or "").strip().lower()
        if v in _PORTFOLIO_BLOCK_VERDICTS:
            blocked += 1
        elif v in _PORTFOLIO_WARN_VERDICTS:
            warned += 1

    for item in record.get("evidence_progress") or []:
        if isinstance(item, dict):
            _tally(item.get("verdict"))
    for item in record.get(_RETIREMENT_APPROVALS_KEY) or []:
        if isinstance(item, dict):
            _tally(item.get("verdict"))
    for section in ("retirement_critique_quality", "portfolio_shape_risk"):
        block = record.get(section)
        if isinstance(block, dict):
            _tally_section(block.get("verdict"))
    for item in record.get("portfolio_operations") or []:
        if isinstance(item, dict):
            _tally_portfolio(item.get("verdict"))

    mode_raw = str(record.get("status", "")).strip().lower()
    mode = mode_raw if mode_raw in {"ok", "blocked", "invalid"} else "invalid"

    if mode != "ok" or blocked:
        verdict = "blocked"
    elif warned:
        verdict = "warned"
    else:
        verdict = "approved"

    gate_mode_raw = (
        str(_runtime_config.get("strategic_challenge_gate_mode", "enforce"))
        .strip()
        .lower()
    )
    gate_mode_val = (
        gate_mode_raw
        if gate_mode_raw in {"observe", "advisory", "enforce"}
        else "enforce"
    )

    return {
        "mode": mode,
        "verdict": verdict,
        "warned_count": warned,
        "blocked_count": blocked,
        "gate_mode": gate_mode_val,
    }


async def _link_clustered_bug_messages(
    session: AsyncSession,
    project: Project,
    anchor_msg: ProjectMessage,
    *,
    kind: str,
    identifier: str,
    resolution_text: str,
) -> list[ProjectMessage]:
    """Link other open bug-class messages that share the anchor's structured signature."""
    from app.message_convergence import (
        bug_message_cluster_signature as _bug_message_cluster_signature,
        bug_messages_share_cluster as _bug_messages_share_cluster,
        link_message_to_outcome as _link_message_to_outcome,
    )

    if _bug_message_cluster_signature(anchor_msg) is None:
        return []

    # Limit to 500 candidates to bound O(N) comparisons on large projects.
    result = await session.execute(
        select(ProjectMessage)
        .where(
            ProjectMessage.project_id == project.id,
            ProjectMessage.status == "open",
        )
        .order_by(ProjectMessage.created_at.asc())
        .limit(500)
    )
    linked: list[ProjectMessage] = []
    for candidate in result.scalars().all():
        if candidate.id == anchor_msg.id:
            continue
        if not _bug_messages_share_cluster(anchor_msg, candidate):
            continue
        _link_message_to_outcome(candidate, kind, identifier)
        candidate.resolution = resolution_text
        candidate.handling_state = MessageHandlingState.RESPONDED
        candidate.handled_by_task_id = anchor_msg.handled_by_task_id
        candidate.updated_at = _utcnow()
        session.add(candidate)
        linked.append(candidate)
    return linked


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

    Accepts the ``{reason, probes?}`` value the strategic agent embeds as a
    top-level sibling in the ``set-problem-model`` payload and returns a clean
    request. ``probes`` is a bounded list of explicitly requested, supported
    product probes. A qualify_request is
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
    probes_raw = raw.get("probes") if "probes" in raw else []
    probes: list[str] = []
    if not isinstance(probes_raw, list):
        raise ValueError("qualify_request.probes must be a list")
    if len(probes_raw) > len(_QUALIFY_REQUEST_PROBE_KINDS):
        raise ValueError("qualify_request.probes contains too many entries")
    for item in probes_raw:
        if not isinstance(item, str):
            raise ValueError("qualify_request.probes entries must be strings")
        probe = item.strip().lower()
        if not probe:
            raise ValueError("qualify_request.probes entries must not be blank")
        if probe not in _QUALIFY_REQUEST_PROBE_KINDS:
            raise ValueError(f"unsupported qualification probe: {probe[:80]}")
        if probe in probes:
            raise ValueError(f"duplicate qualification probe: {probe}")
        probes.append(probe)
    return {"reason": reason, "probes": probes}


def extract_new_version_goal(raw: object) -> str | None:
    """Validate + normalize a raw ``new_version_goal`` value.

    Accepts the string the strategic agent embeds as a top-level sibling in the
    ``set-problem-model`` payload and returns a clean, bounded string, or ``None``
    when the input is absent, contentless, or unusable.

    Used by the ``set_problem_model`` endpoint to lift the field out of the
    problem_model JSON *before* ``ProblemModel`` validation (``extra="forbid"``,
    which would otherwise reject the whole update). ``_apply_new_version_goal``
    re-checks the open-version / goal-already-set guards, so a no-op is safe.
    """
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()[:_NEW_VERSION_GOAL_MAX]
    if not cleaned:
        return None
    from app.release_scoring import is_contentless_goal

    if is_contentless_goal(cleaned):
        return None
    return cleaned


async def _apply_new_version_goal(
    goal: object,
    task: Task,
    project: Project,
    session: AsyncSession,
    *,
    path: str,
) -> bool:
    """Apply an optional ``new_version_goal`` from the strategic agent.

    ``goal`` is the raw value from either:
      - the API path: ``task.context.get(NEW_VERSION_GOAL_CONTEXT_KEY)``
      - the fallback parse path: ``parsed.get("new_version_goal")``

    When valid, sets the goal with ``source='strategist'`` and ``session.add``s
    the row — the caller's surrounding ``await session.commit()`` persists it.
    Strategic may correct any system-written version goal when evidence shows
    it is infeasible or badly scoped; ``goal_editor_info`` retains provenance.

    Silently no-ops when:
      - ``goal`` is None / not a non-empty string
      - ``is_contentless_goal`` returns True
      - The project has no open ``ReleaseVersion``

    All errors are logged and swallowed — a bad or absent goal must never
    block vision/direction writes.
    """
    if not goal or not isinstance(goal, str):
        return
    _nvg_text = goal.strip()
    if not _nvg_text:
        return
    try:
        from app.release_scoring import is_contentless_goal
        from app.release_version import (
            current_open_version as _cur_open_version,
            set_version_goal as _set_version_goal,
        )

        if is_contentless_goal(_nvg_text):
            return
        _open_ver = await _cur_open_version(session, project.id)
        if _open_ver is None:
            logger.info(
                "[STRATEGIC] Task %s (%s path): no open ReleaseVersion for project %s "
                "— skipping new_version_goal",
                task.id,
                path,
                project.id,
            )
            return
        if _open_ver.goal is not None:
            logger.info(
                "[STRATEGIC] Task %s (%s path): correcting open version v%d goal "
                "(prior provenance=%s)",
                task.id,
                path,
                _open_ver.version_number,
                _open_ver.goal_editor_info,
            )
        _set_version_goal(
            _open_ver,
            _nvg_text,
            editor_id=str(task.id)[:8],
            source="strategist",
        )
        session.add(_open_ver)
        logger.info(
            "[STRATEGIC] Task %s (%s path): authored version goal for v%d project=%s: %.80s",
            task.id,
            path,
            _open_ver.version_number,
            project.id,
            _nvg_text,
        )
    except Exception:
        logger.exception(
            "[STRATEGIC] Task %s (%s path): failed to apply new_version_goal (non-fatal)",
            task.id,
            path,
        )


async def _apply_new_acceptance_criteria(
    criteria_texts: object,
    task: Task,
    project: Project,
    session: AsyncSession,
    *,
    path: str,
) -> None:
    """Apply optional ``new_acceptance_criteria`` from the strategic agent.

    ``criteria_texts`` is the raw value from
    ``task.context[NEW_ACCEPTANCE_CRITERIA_CONTEXT_KEY]`` — a list of strings.
    For each text, creates a new VersionAcceptanceCriterion on the open version,
    skipping duplicates (same text already exists). Errors are swallowed so a
    bad criteria list never blocks vision/direction writes.
    """
    if not isinstance(criteria_texts, list) or not criteria_texts:
        return
    try:
        from app import acceptance_criteria as _ac
        from app.release_version import current_open_version as _cur_open_version

        _open_ver = await _cur_open_version(session, project.id)
        if _open_ver is None:
            logger.info(
                "[STRATEGIC] Task %s (%s path): no open ReleaseVersion for project %s "
                "— skipping new_acceptance_criteria",
                task.id,
                path,
                project.id,
            )
            return
        existing = await _ac.get_criteria_for_version(session, _open_ver.id)
        existing_texts = {c.text for c in existing}
        active_count = sum(1 for c in existing if c.status == "active")
        headroom = _ac.MAX_CRITERIA_PER_VERSION - active_count
        created_count = 0
        skipped_over_cap = 0
        for text in criteria_texts:
            if not isinstance(text, str):
                continue
            cleaned = text.strip()
            if not cleaned:
                continue
            if cleaned in existing_texts:
                logger.debug(
                    "[STRATEGIC] Task %s (%s path): skipping duplicate criterion text for "
                    "project %s version v%d: %.80s",
                    task.id,
                    path,
                    project.id,
                    _open_ver.version_number,
                    cleaned,
                )
                continue
            if created_count >= headroom:
                skipped_over_cap += 1
                continue
            await _ac.stage_criterion(
                session,
                project_id=project.id,
                release_version_id=_open_ver.id,
                text=cleaned,
                created_by="strategist",
            )
            existing_texts.add(cleaned)
            created_count += 1
        if created_count > 0:
            logger.info(
                "[STRATEGIC] Task %s (%s path): authored %d acceptance criterion(a) "
                "for project %s version v%d",
                task.id,
                path,
                created_count,
                project.id,
                _open_ver.version_number,
            )
        if skipped_over_cap > 0:
            logger.info(
                "[STRATEGIC] Task %s (%s path): dropped %d criterion(a) — "
                "cap of %d reached for project %s version v%d",
                task.id,
                path,
                skipped_over_cap,
                _ac.MAX_CRITERIA_PER_VERSION,
                project.id,
                _open_ver.version_number,
            )
    except Exception:
        logger.exception(
            "[STRATEGIC] Task %s (%s path): failed to apply new_acceptance_criteria (non-fatal)",
            task.id,
            path,
        )


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

    ``request`` is the raw ``{reason: str, probes?: list[str]}`` value from either:
      - the API path: ``task.context[QUALIFY_REQUEST_CONTEXT_KEY]``
      - the fallback / log-parse path: ``parsed.get("qualify_request")``

    It is the agent's signal that an ``open`` version in ``stabilize`` is
    release-ready (automain-lts-plan-v1.md Phase 3). When valid, looks up the
    open ``ReleaseVersion``, gates on
    ``phase == "stabilize"``, verifies the current automain HEAD is not blocked
    by CI-compliance violations, and calls ``release_qualifier.run_qualification``
    (which snapshots the candidate SHA + spawns the active qualifier children;
    the caller's surrounding commit persists its version-row writes). When
    CI-compliance blocks qualification — either an observed violation or
    evidence that is unavailable/degraded (GitHub credentials, HEAD, limits
    file, or repo tree) — durable blocker evidence is recorded and ``True`` is
    returned. The gate is fail-CLOSED: a qualify_request never proceeds on the
    assumption that compliance is clean when it could not be verified — an
    exception raised BY the gate itself (network error, unexpected shape) is
    the same evidence-unavailable class and is recorded and blocked too. The
    post-commit wrapper then arms a coalesced Strategic refresh in a separate
    transaction, after blocker durability is established. Strategic uses its
    ordinary task authority to choose any repair; this path never hard-codes
    one.

    Silently no-ops when ``request`` is None/not a dict, there is no open
    ``ReleaseVersion``, or that version is not in ``stabilize``. All errors are
    logged and swallowed — a bad or absent request must never block the write.
    """
    if not request or not isinstance(request, dict):
        return False
    reason_raw = request.get("reason")
    reason = reason_raw.strip() or None if isinstance(reason_raw, str) else None
    try:
        normalized_request = extract_qualify_request(request)
    except ValueError as exc:
        logger.warning(
            "[STRATEGIC] Task %s (%s path): invalid qualify_request ignored: %s",
            task.id,
            path,
            exc,
        )
        return False
    if normalized_request is None:
        return False
    reason = normalized_request["reason"] or None
    requested_probes = normalized_request["probes"]
    # Initialised outside the try so the except clause can record a durable
    # failure on the version row even if run_qualification throws.
    open_ver = None
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
            return False
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
            return False
        try:
            from app import ci_compliance as _ci_compliance
            from app.strategic_spawning import (
                _maybe_spawn_ci_compliance_cap_bump_fix,
                ci_compliance_qualify_gate,
            )

            status = await _ci_compliance.ensure_current_ci_compliance(
                session, project
            )
            if status.get("status") == "success":
                gate = ci_compliance_qualify_gate(project, status.get("head_sha"))
                if gate.get("blocked"):
                    logger.info(
                        "[STRATEGIC] Task %s (%s path): qualify_request for "
                        "project %s version v%d blocked by CI-compliance "
                        "violations=%s head=%s; returning evidence to Strategic "
                        "(reason=%r)",
                        task.id,
                        path,
                        project.id,
                        open_ver.version_number,
                        gate.get("violation_count", 0),
                        gate.get("head_sha"),
                        reason,
                    )
                    # Spawn the cap-bump repair HERE so the block path is
                    # self-sufficient. The strategic-review payload build is the
                    # only other spawner; if it never runs (no strategic cycle)
                    # the repair would otherwise never be queued. The spawn is
                    # deduped per project+HEAD, so an already-queued repair is
                    # returned rather than duplicated.
                    try:
                        repair_task = await _maybe_spawn_ci_compliance_cap_bump_fix(
                            session, project, gate
                        )
                    except Exception:
                        logger.exception(
                            "[STRATEGIC] Task %s (%s path): failed to spawn "
                            "CI-compliance cap-bump repair for project %s",
                            task.id,
                            path,
                            project.id,
                        )
                        repair_task = None
                    # Record the CI-compliance block durably on the version row
                    # so the resident strategist can see WHY a requested
                    # qualification never started (and which repair task is
                    # addressing it). The previous path only spawned a cap-bump
                    # task from the payload build and logged, leaving
                    # last_qualification null and therefore indistinguishable
                    # from "never requested" without log access.
                    # Merged under a dedicated key (like the qualify_request_error
                    # trace on the failure path below) so it never overwrites a
                    # prior verdict's "status" and cannot perturb
                    # run_qualification's cached-verdict guard.
                    try:
                        open_ver.last_qualification = {
                            **(open_ver.last_qualification or {}),
                            "qualify_request_blocked": {
                                "blocker": "ci_compliance",
                                "blocker_class": "actionable",
                                "violation_count": gate.get("violation_count", 0),
                                "head_sha": gate.get("head_sha"),
                                "repair_task": (
                                    str(repair_task.id)
                                    if repair_task is not None
                                    else None
                                ),
                            },
                            "qualify_request_blocked_at": _utcnow().isoformat(),
                        }
                        flag_modified(open_ver, "last_qualification")
                        session.add(open_ver)
                    except Exception:
                        logger.exception(
                            "[STRATEGIC] Task %s (%s path): failed to record "
                            "qualify_request CI-compliance block",
                            task.id,
                            path,
                        )
                        return False
                    # Notify the operator (deduped per project + HEAD) so the
                    # block is not silent. The strategist payload also surfaces
                    # the evidence, but a durable Alert is the operator-facing
                    # signal. Policy fail-closed is fine: if the event type is
                    # not operator-visible the alert is a no-op, never a crash.
                    try:
                        from app.alert_policy import (
                            create_project_alert_if_allowed,
                        )
                        from app.models import AlertSeverity

                        await create_project_alert_if_allowed(
                            session,
                            project_id=project.id,
                            event_type="release.qualification_ci_compliance_block",
                            severity=AlertSeverity.WARNING,
                            task_id=(
                                repair_task.id
                                if repair_task is not None
                                else None
                            ),
                            title=(
                                f"Release v{open_ver.version_number} blocked by "
                                "CI-compliance violations"
                            ),
                            message=(
                                f"qualify_request for release "
                                f"v{open_ver.version_number} was blocked by "
                                f"{gate.get('violation_count', 0)} CI-compliance "
                                f"violation(s) at HEAD "
                                f"{str(gate.get('head_sha') or '')[:12]}. "
                                f"Repair task "
                                f"{repair_task.id if repair_task is not None else 'spawn failed'} "
                                "queued; qualification resumes once the guardrail "
                                "is clean."
                            ),
                            dedupe_key=(
                                f"ci_compliance_block:{project.id}:"
                                f"{str(gate.get('head_sha') or 'unknown')[:12]}"
                            ),
                        )
                    except Exception:
                        logger.exception(
                            "[STRATEGIC] Task %s (%s path): failed to raise "
                            "CI-compliance block alert for project %s",
                            task.id,
                            path,
                            project.id,
                        )
                    return True
            else:
                # CI-compliance evidence is UNAVAILABLE (GitHub credentials or
                # HEAD unresolvable). Fail CLOSED: a qualify_request must not
                # proceed on the assumption that compliance is clean when it
                # could not be checked. Record the block durably so Strategic
                # can act on the root cause (e.g. re-authorize GitHub) instead
                # of silently qualifying on unverified evidence.
                logger.info(
                    "[STRATEGIC] Task %s (%s path): CI-compliance status "
                    "unavailable for project %s before qualify_request "
                    "(reason=%s); blocking qualification",
                    task.id,
                    path,
                    project.id,
                    status.get("reason"),
                )
                try:
                    open_ver.last_qualification = {
                        **(open_ver.last_qualification or {}),
                        "qualify_request_blocked": {
                            "blocker": "ci_compliance",
                            "blocker_class": "unavailable_evidence",
                            "violation_count": 0,
                            "head_sha": None,
                            "repair_task": None,
                        },
                        "qualify_request_blocked_at": _utcnow().isoformat(),
                    }
                    flag_modified(open_ver, "last_qualification")
                    session.add(open_ver)
                except Exception:
                    logger.exception(
                        "[STRATEGIC] Task %s (%s path): failed to record "
                        "qualify_request CI-compliance unavailability block",
                        task.id,
                        path,
                    )
                    return False
                return True
        except Exception:
            # The gate itself raised (network error from the compliance fetch,
            # unexpected payload shape, import failure). That is the SAME
            # evidence-unavailable class as the `status != success` branch
            # above: we do not know whether compliance is clean. Fail CLOSED —
            # record the blocker durably and return without qualifying, rather
            # than proceeding on unverified evidence.
            logger.exception(
                "[STRATEGIC] Task %s (%s path): CI-compliance qualify gate "
                "raised for project %s; blocking qualification (fail-closed)",
                task.id,
                path,
                project.id,
            )
            try:
                open_ver.last_qualification = {
                    **(open_ver.last_qualification or {}),
                    "qualify_request_blocked": {
                        "blocker": "ci_compliance",
                        "blocker_class": "unavailable_evidence",
                        "violation_count": 0,
                        "head_sha": None,
                        "repair_task": None,
                    },
                    "qualify_request_blocked_at": _utcnow().isoformat(),
                }
                flag_modified(open_ver, "last_qualification")
                session.add(open_ver)
            except Exception:
                logger.exception(
                    "[STRATEGIC] Task %s (%s path): failed to record "
                    "qualify_request CI-compliance gate-error block",
                    task.id,
                    path,
                )
                return False
            return True
        result = await _rq.run_qualification(
            session,
            project.id,
            open_ver,
            requested_probes=requested_probes,
            explicit_request=True,
        )
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
        import traceback as _tb

        logger.exception(
            "[STRATEGIC] Task %s (%s path): failed to apply qualify_request "
            "(non-fatal)",
            task.id,
            path,
        )
        # Record the failure durably on the ReleaseVersion row so it is
        # queryable via the strategic payload / admin version-phase fetch.
        # A log line alone is not fetchable by the agent or operator without
        # direct log access.  Merges under a separate key so a partial write
        # by run_qualification (e.g. "status": "error") is preserved.
        if open_ver is not None:
            try:
                open_ver.last_qualification = {
                    **(open_ver.last_qualification or {}),
                    "qualify_request_error": _tb.format_exc(limit=5)[:500],
                    "qualify_request_error_at": _utcnow().isoformat(),
                }
                flag_modified(open_ver, "last_qualification")
                session.add(open_ver)
            except Exception:
                logger.exception(
                    "[STRATEGIC] Task %s (%s path): also failed to record "
                    "qualify_request error on ReleaseVersion — failure lost",
                    task.id,
                    path,
                )
    return False


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
            # Count VERIFIED merges so the creation-difficulty prediction is
            # primed on work that actually shipped, not degraded/unverified
            # merges. Legacy fallback handled by verified_merged_count / the
            # aggregate .get chain.
            for h in d.get("research_history") or []:
                similar_past_merges_raw += verified_merged_count(h)
            agg = d.get("history_aggregate") or {}
            similar_past_merges_raw += (
                agg.get("total_verified_prs_merged", agg.get("total_prs_merged", 0))
                or 0
            )
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


_SNAPSHOT_MAX_RECENT_CYCLES = 3
_SNAPSHOT_FIELD_MAX = 300


def _build_retirement_snapshot(direction: dict, rationale: str) -> Dict[str, Any]:
    """Build a compact snapshot of a retired direction for memo context.

    Reads success_evidence and retirement_condition from direction["context"]
    (read-only — never writes back). Handles absent context dict gracefully.
    Bounds string lengths and recent history size to keep payloads small.
    """
    ctx = direction.get("context") or {}
    success_evidence = (ctx.get("success_evidence") or "")[:_SNAPSHOT_FIELD_MAX]
    retirement_condition = (ctx.get("retirement_condition") or "")[:_SNAPSHOT_FIELD_MAX]
    direction_text = (direction.get("text") or "")[:_SNAPSHOT_FIELD_MAX]

    history = direction.get("research_history") or []
    recent = history[-_SNAPSHOT_MAX_RECENT_CYCLES:] if history else []
    recent_history_summary = [
        {
            "cycles": h.get("cycles", 1),
            "prs_merged": h.get("prs_merged", 0),
            "tasks_completed": h.get("tasks_completed", 0),
            "tasks_failed": h.get("tasks_failed", 0),
        }
        for h in recent
    ]

    return {
        "direction_text": direction_text,
        "success_evidence": success_evidence,
        "retirement_condition": retirement_condition,
        "rationale": rationale[:_SNAPSHOT_FIELD_MAX],
        "recent_history_summary": recent_history_summary,
        "retirement_metrics": _build_retirement_metrics(direction),
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


def _snapshot_has_substantive_context(snapshot: dict) -> bool:
    """Return True when the snapshot carries enough context to synthesize a meaningful memo.

    retirement_metrics is checked for non-zero activity (final_prs_merged or
    final_cycles_active) because _build_retirement_metrics always returns a dict.
    """
    metrics = snapshot.get("retirement_metrics") or {}
    metrics_substantive = bool(
        metrics.get("final_prs_merged") or metrics.get("final_cycles_active")
    )
    return bool(
        snapshot.get("direction_text")
        or snapshot.get("success_evidence")
        or snapshot.get("retirement_condition")
        or metrics_substantive
        or snapshot.get("recent_history_summary")
    )


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
        # A retirement that was previously DEFERRED by the challenge gate left an
        # open ``retirement_residual:{project}:{direction}:deferred`` debt row.
        # The retire has now succeeded, so resolve that residual. Runs BEFORE the
        # memo-dedup ``continue`` so a duplicate-memo direction still gets its
        # deferred debt cleared. Best-effort/non-fatal; does not touch the
        # separate memo handoff debt written below.
        try:
            from app.completion_debt import aresolve_deferred_retirement_debt

            await aresolve_deferred_retirement_debt(session, project.id, did)
        except Exception:
            logger.exception(
                "[STRATEGIC] deferred-retirement debt resolve failed for "
                "direction %s (non-fatal)",
                did,
            )
        if did in already_memed:
            logger.debug(
                "[STRATEGIC] Skipping duplicate retirement memo for direction %s", did
            )
            continue
        direction = by_id.get(did)
        direction_text = (direction.get("text") or "") if direction else ""
        rationale = rationales.get(did) or ""
        topic = direction_text[:120] or f"Direction {did}"
        snapshot = _build_retirement_snapshot(direction or {}, rationale)
        snapshot["source_task_id"] = str(task.id)
        success_evidence = snapshot.get("success_evidence") or ""
        retirement_condition = snapshot.get("retirement_condition") or ""
        memo_payload = validate_internal_payload(
            {
                "type": "memo",
                "topic": f"Direction retired: {topic}",
                "reasoning_kind": "direction_retirement",
                "direction_id": did,
                "source_finding": snapshot,
                **({"retirement_rationale": rationale} if rationale else {}),
            },
            "strategic_completion:memo:direction_retirement",
        )
        context: Dict[str, Any] = {
            "memo_reasoning_kind": "direction_retirement",
            "memo_source_direction_id": did,
            "memo_source_task_id": str(task.id),
            **({"retirement_rationale": rationale} if rationale else {}),
            **({"success_evidence": success_evidence} if success_evidence else {}),
            **(
                {"retirement_condition": retirement_condition}
                if retirement_condition
                else {}
            ),
        }
        memo_task = Task(
            project_id=project.id,
            title=f"Memo: direction retired — {topic[:100]}",
            description="Reasoning memo for retired direction",
            parent_task_id=task.id,
            payloads=[memo_payload],
            context=context,
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            priority=TASK_PRIORITY_MAP.get("low", 0),
        )
        session.add(memo_task)
        # Flush so memo_task.id is visible in the DB before KnowledgeDocument
        # rows reference it via source_task_id (no ORM relationship to order the
        # flush automatically, unlike Artifact which has a back_populates).
        await session.flush()
        # Synthesize the memo artifact at spawn time when source_finding is
        # available — works for both rationale-present and rationale-absent paths.
        # The source_finding snapshot carries all decision context so synthesis
        # is substantive even on auto-retirement paths with no rationale note.
        synthesized = False
        try:
            if rationale:
                parts = []
                if direction_text:
                    parts.append(f"Direction: {direction_text}")
                parts.append(f"Retirement rationale: {rationale}")
                if success_evidence:
                    parts.append(f"Success evidence: {success_evidence}")
                if retirement_condition:
                    parts.append(f"Retirement condition: {retirement_condition}")
                metrics = snapshot.get("retirement_metrics") or {}
                if metrics:
                    metrics_str = ", ".join(f"{k}={v}" for k, v in metrics.items())
                    parts.append(f"Final metrics: {metrics_str}")
                history = snapshot.get("recent_history_summary") or []
                if history:
                    history_lines = [
                        f"- Cycle {i + 1}: {h['prs_merged']} PRs merged, "
                        f"{h['tasks_completed']} completed, {h['tasks_failed']} failed"
                        for i, h in enumerate(history)
                    ]
                    parts.append("Recent history:\n" + "\n".join(history_lines))
                tags = ["direction-retirement"]
                if success_evidence:
                    tags.append("success-evidence")
                if retirement_condition:
                    tags.append("retirement-condition")
                citations = [f"direction:{did}", f"task:{task.id}"]
                synth_memo = {
                    "summary": rationale[:200],
                    "reasoning": "\n\n".join(parts),
                    "citations": citations,
                    "tags": tags,
                }
                _apply_synthesized_memo(memo_task, synth_memo, session)
                session.add(
                    KnowledgeDocument(
                        project_id=project.id,
                        source_task_id=memo_task.id,
                        source_kind=SourceKind.DIRECTION_RETIREMENT.value,
                        title=memo_task.title,
                        summary=synth_memo["summary"],
                        body=synth_memo["reasoning"],
                        topic="direction-retirement",
                        direction_id=did,
                        tags=synth_memo["tags"],
                        source_citations=synth_memo["citations"],
                    )
                )
                synthesized = True
            elif _snapshot_has_substantive_context(snapshot):
                parts = []
                if direction_text:
                    parts.append(f"Direction: {direction_text}")
                if success_evidence:
                    parts.append(f"Success evidence: {success_evidence}")
                if retirement_condition:
                    parts.append(f"Retirement condition: {retirement_condition}")
                metrics = snapshot.get("retirement_metrics") or {}
                if metrics:
                    metrics_str = ", ".join(f"{k}={v}" for k, v in metrics.items())
                    parts.append(f"Final metrics: {metrics_str}")
                history = snapshot.get("recent_history_summary") or []
                if history:
                    history_lines = [
                        f"- Cycle {i + 1}: {h['prs_merged']} PRs merged, "
                        f"{h['tasks_completed']} completed, {h['tasks_failed']} failed"
                        for i, h in enumerate(history)
                    ]
                    parts.append("Recent history:\n" + "\n".join(history_lines))
                tags = ["direction-retirement"]
                if success_evidence:
                    tags.append("success-evidence")
                if retirement_condition:
                    tags.append("retirement-condition")
                citations = [f"direction:{did}", f"task:{task.id}"]
                summary = direction_text[:200] or f"Direction {did} retired"
                synth_memo = {
                    "summary": summary,
                    "reasoning": "\n\n".join(parts),
                    "citations": citations,
                    "tags": tags,
                }
                _apply_synthesized_memo(memo_task, synth_memo, session)
                session.add(
                    KnowledgeDocument(
                        project_id=project.id,
                        source_task_id=memo_task.id,
                        source_kind=SourceKind.DIRECTION_RETIREMENT.value,
                        title=memo_task.title,
                        summary=synth_memo["summary"],
                        body=synth_memo["reasoning"],
                        topic="direction-retirement",
                        direction_id=did,
                        tags=synth_memo["tags"],
                        source_citations=synth_memo["citations"],
                    )
                )
                synthesized = True
            else:
                # No rationale and no substantive snapshot context. Synthesize a
                # minimal memo so the task never provisions a VM for an empty payload.
                minimal_memo = {
                    "summary": f"Direction {did} retired",
                    "reasoning": "Retirement recorded with no additional context.",
                    "citations": [f"direction:{did}", f"task:{task.id}"],
                    "tags": ["direction-retirement"],
                }
                _apply_synthesized_memo(memo_task, minimal_memo, session)
                session.add(
                    KnowledgeDocument(
                        project_id=project.id,
                        source_task_id=memo_task.id,
                        source_kind=SourceKind.DIRECTION_RETIREMENT.value,
                        title=memo_task.title,
                        summary=minimal_memo["summary"],
                        body=minimal_memo["reasoning"],
                        topic="direction-retirement",
                        direction_id=did,
                        tags=minimal_memo["tags"],
                        source_citations=minimal_memo["citations"],
                    )
                )
                synthesized = True
        except Exception:
            memo_task.status = TaskStatus.FAILED
            raise
        spawned += 1
        # Completion-debt ledger: a retirement leaves a residual handoff (the
        # memo captures rationale, but downstream follow-through is owed).
        # Best-effort, deduped per (project, direction) so the row is unique.
        try:
            from app.completion_debt import (
                arecord_completion_debt,
                KIND_RETIREMENT_RESIDUAL,
            )

            await arecord_completion_debt(
                session,
                dedupe_key=f"retirement_residual:{project.id}:{did}",
                kind=KIND_RETIREMENT_RESIDUAL,
                title=f"Retirement residual handoff for direction {did}: {topic[:120]}",
                project_id=project.id,
                source="strategic_completion",
                detail={
                    "direction_id": str(did),
                    "memo_task_id": str(memo_task.id),
                    "kind": "retired",
                },
            )
        except Exception:
            logger.exception(
                "[STRATEGIC] completion-debt record failed for retired "
                "direction %s (non-fatal)",
                did,
            )
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

    Returns a newline-separated string with one finding per line: grade,
    verification basis, title, truncated description, top claims, and source
    refs.  Empty string when rows is empty.  Suitable for embedding in a
    strategic synthesis prompt.

    The verification_basis (e.g. code_verified vs pattern_inferred) is rendered
    alongside the actionability grade so the strategist can weight a finding
    verified against HEAD differently from one merely asserted.  It is a
    weighting signal only — rows are never filtered out by basis.
    """
    if not rows:
        return ""
    lines: list[str] = []
    for r in rows[:max_rows]:
        grade = r.get("actionability_grade") or "MEDIUM"
        basis = r.get("verification_basis") or "pattern_inferred"
        downgraded_from = r.get("verification_basis_downgraded_from")
        downgrade_reason = r.get("verification_basis_downgrade_reason")
        title = r.get("finding_title") or ""
        desc = (r.get("finding_description") or "")[:_FINDINGS_DESCRIPTION_MAX]
        claims = [str(c) for c in (r.get("claims") or [])[:_FINDINGS_CLAIM_MAX]]
        sources = r.get("sources") or []
        refs = [
            s.get("ref", "")
            for s in sources[:_FINDINGS_SOURCE_MAX]
            if isinstance(s, dict) and s.get("ref")
        ]
        prefix = f"[{grade}/{basis}]"
        if downgraded_from:
            prefix = f"[{grade}/{basis} downgraded-from={downgraded_from}]"
        line = f"{prefix} {title}: {desc}"
        if downgrade_reason:
            line += f" | downgrade: {downgrade_reason}"
        if claims:
            line += f" | claims: {'; '.join(claims)}"
        if refs:
            line += f" | sources: {', '.join(refs)}"
        challenge_summary = r.get("challenge_summary")
        if isinstance(challenge_summary, dict):
            agreement = challenge_summary.get("agreement") or "indeterminate"
            second_verdict = challenge_summary.get("second_opinion_verdict") or ""
            token = f"{agreement}/{second_verdict}" if second_verdict else agreement
            line += f" | challenge: {token}"
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
    # Credit-gate aware: confirmed-stranded merges are excluded; landed and
    # ancestry-indeterminate merges both count (flag-trust preserved). Also
    # excludes degraded-unverified merges via verified_merged_count base.
    merged = sum(credit_gate_merged_count(e) for e in window)
    # Proof-weighted merge volume drives the RATE gate so a direction whose merges
    # are mostly undeclared/unproven (the "merely delivered" proxy) reads as a lower
    # merge rate than one shipping proven work, and trips the churn guardrail where
    # the proven direction does not. The displayed ``merged`` count is the
    # credit-gate basis (verified minus confirmed-stranded). Legacy/read-only cycles
    # degrade to the verified value.
    # Subtract confirmed-stranded merges from the proof-weighted total so the rate
    # numerator stays consistent with the credit_gate_merged_count denominator.
    proof_merged = sum(
        max(0.0, proof_weighted_merged(e) - float(e.get("stranded_prs_merged") or 0))
        for e in window
    )
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
    merge_rate = proof_merged / total_finished if total_finished > 0 else 0.0
    # Raw merge volume: children whose PR actually MERGED — onto the integration
    # branch OR a rollup staging branch — over the window. Unlike ``merged``
    # (credit-gate, integration-landed only), this stays non-zero the moment work
    # lands on a staging branch, before the rollup lands and re-stamps
    # landed_on_integration. It is a defense-in-depth backstop for the pause gate:
    # a direction whose work is demonstrably landing must never be paused as
    # "0 real merges" merely because rollup-landing credit propagation has not yet
    # recomputed prs_merged. Legacy entries lacking the field contribute 0.
    real_merges = sum(int(e.get("merged_prs_any_base") or 0) for e in window)
    return {
        "window_cycles": len(window),
        "merged": merged,
        "non_merge_finished": non_merge_finished,
        "merge_rate": round(merge_rate, 3),
        "real_merges": real_merges,
    }


def _compute_lifetime_burn_metrics(direction: dict) -> dict | None:
    """Return lifetime/cumulative non-merge burn metrics combining research_history and history_aggregate.

    Uses the same non-merge definition as _compute_churn_metrics (tasks_completed +
    code_failed - prs_merged) but across the direction's entire lifetime. Returns
    None when there is no meaningful lifetime data.
    """
    history = direction.get("research_history") or []
    agg = direction.get("history_aggregate") or {}

    # Credit-gate aware: excludes confirmed-stranded AND degraded-unverified
    # merges so neither suppresses lifetime non-merge burn.
    window_merged = sum(credit_gate_merged_count(e) for e in history)
    window_completed = sum(e.get("tasks_completed", 0) for e in history)
    # Correct no-op completions ("deliverable already exists", no PR) are not burn.
    # Subtract them across both the rolling window and the lifetime aggregate. Both
    # accessors default to 0 so old entries / un-folded aggregates are unchanged.
    window_no_pr = sum(e.get("tasks_completed_no_pr", 0) for e in history)
    window_code_failed = sum(
        max(0, e.get("tasks_failed", 0) - e.get("tasks_failed_infra", 0))
        for e in history
    )
    # Legacy aggregates predate total_verified_prs_merged; fall back to
    # total_prs_merged so old folded data is unchanged. Subtract stranded merges
    # so the credit-gate discount survives cycle eviction into the aggregate.
    agg_merged = max(
        0,
        int(agg.get("total_verified_prs_merged", agg.get("total_prs_merged", 0)) or 0)
        - int(agg.get("total_stranded_prs_merged", 0) or 0),
    )
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
    # Proof-weighted lifetime merge volume drives the RATE gate (same rationale as
    # _compute_churn_metrics): undeclared/unproven lifetime merges count for less, so
    # a slow-burn direction shipping only unprovable work trips the backstop where a
    # proven one does not. Displayed lifetime_merged is the credit-gate basis
    # (verified minus confirmed-stranded, same as window_merged).
    # Subtract confirmed-stranded merges so the rate numerator stays consistent with
    # the credit_gate_merged_count basis used for lifetime_merged/window_merged.
    agg_stranded = float(agg.get("total_stranded_prs_merged", 0) or 0)
    proof_lifetime_merged = sum(
        max(0.0, proof_weighted_merged(e) - float(e.get("stranded_prs_merged") or 0))
        for e in history
    ) + max(0.0, _agg_proof_weighted_merged(agg) - agg_stranded)
    lifetime_merge_rate = proof_lifetime_merged / total_finished
    return {
        "lifetime_merged": lifetime_merged,
        "lifetime_non_merge_finished": lifetime_non_merge_finished,
        "lifetime_merge_rate": round(lifetime_merge_rate, 3),
    }


def _cycle_landed_or_noop(entry: dict) -> bool:
    """True when a single research_history cycle shows a genuine LANDING or a
    genuine NO-OP completion — the only per-cycle signals that qualify a cycle to
    hold the still-productive pause shield.

    * **Landing** — a PR that actually merged, integration OR staging:
      ``merged_prs_any_base`` (raw merge volume, staging-inclusive and safe
      against rollup-landing credit-propagation lag), ``verified_merged_count``
      (verified integration merge), or the legacy ``prs_merged`` count (a PR that
      merged even if verification was later classified degraded — it still landed).
    * **No-op completion** — an implementation task that correctly completed with
      NO PR because the deliverable already existed (``tasks_completed_no_pr``).
      Genuinely productive, and the shield's legitimate purpose to protect.

    A cycle that merely SPAWNED children or emitted direction notes (no merge, no
    no-op) is deliberately NOT shield-qualifying: that spawn-only-never-land shape
    is exactly the churn the merge-rate pause family exists to catch.
    """
    if not isinstance(entry, dict):
        return False
    return (
        int(entry.get("merged_prs_any_base") or 0) > 0
        or verified_merged_count(entry) > 0
        or int(entry.get("prs_merged") or 0) > 0
        or int(entry.get("tasks_completed_no_pr") or 0) > 0
    )


def _direction_consecutive_zero_landed_cycles(direction: dict) -> int:
    """Count the most-recent consecutive research cycles that neither landed a
    merge nor completed a genuine no-op (see :func:`_cycle_landed_or_noop`).

    Trailing count from the newest history entry backward, stopping at the first
    cycle that shows a landing/no-op. Drives the still-productive shield strip: a
    direction that has been spawn-/notes-only for this many cycles running has
    stopped shipping and must lose its shield so the merge-rate pause family can
    finally evaluate it.
    """
    history = direction.get("research_history") or []
    count = 0
    for entry in reversed(history):
        if _cycle_landed_or_noop(entry):
            break
        count += 1
    return count


def _direction_still_productive(direction: dict) -> bool:
    """True when a direction is demonstrably still producing useful work.

    Gate for the cumulative merge-rate family of pause rules (Rule 2 zero-merge,
    Rule 5 churn, Rule 6 lifetime burn). Those rules count "finished but not merged"
    volume and can wrongly pause a healthy direction whose implementation tasks
    correctly complete as no-ops ("deliverable already present") — no PR, no merge,
    yet genuinely productive.

    Three conditions must ALL hold:
      * ``last_cycle_productive`` is True — the system's own per-cycle verdict that
        the most recent cycle produced actionable work.
      * ``consecutive_unproductive_cycles < N`` — guards against a single stale True
        flag papering over a direction that has since gone quiet for several cycles.
        N defaults to 2 and is configurable to match the direction_* config style.
      * The direction has NOT gone ``direction_productive_shield_max_zero_landed_cycles``
        (default 3) consecutive most-recent cycles with neither a landed merge
        (integration OR staging) nor a genuine no-op completion.

    Signal-that-lies fix (3rd condition): ``last_cycle_productive`` is set True by a
    cycle that merely SPAWNED a child or emitted notes. That is the right signal for
    the cooldown cadence (``direction_refractory``) but a FALSE contradiction to a
    merge-rate pause — a direction that spawns every cycle yet never lands anything
    would otherwise keep this shield forever and never reach the churn / zero-merge /
    lifetime-burn family (and would earn the fast productive cooldown on top). Mere
    spawn/notes no longer qualify: once the direction stops landing merges AND stops
    completing genuine no-ops for N cycles running, the shield is stripped. A
    direction that is really shipping — or legitimately completing already-built
    no-ops — resets that counter and keeps its shield, preserving the shield's
    legitimate purpose.

    Keeps existing behavior for directions that never set the productivity flag (the
    common case in fixtures and legacy data): ``.get`` returns falsy → not productive
    → the rules fire exactly as before. CODE-FAILURE rules (3, 4) are intentionally
    NOT gated by this — a crashing direction must still pause regardless of how
    productive its last cycle looked.
    """
    if not direction.get("last_cycle_productive"):
        return False
    skip_max = int(
        _runtime_config.get("direction_productive_pause_skip_max_unproductive", 2) or 2
    )
    consecutive = int(direction.get("consecutive_unproductive_cycles", 0) or 0)
    if consecutive >= skip_max:
        return False
    strip_max = int(
        _runtime_config.get("direction_productive_shield_max_zero_landed_cycles", 3) or 3
    )
    # Shield holds unless the direction has gone strip_max consecutive most-recent
    # cycles with neither a landed merge nor a genuine no-op completion.
    return not (
        strip_max > 0
        and _direction_consecutive_zero_landed_cycles(direction) >= strip_max
    )


def _recent_merges_mostly_unproven(direction: dict) -> bool:
    """True when a direction's recent VERIFIED merges are essentially all
    undeclared/unproven — the "merely delivered" proxy.

    Used to STRIP the still-productive pause shield (Rules 2/5/6) from a direction
    that keeps shipping merges no acceptance evidence backs, so a pile of
    unprovable merges no longer out-protects a direction shipping proven work.
    A proven (or even merely declared) direction keeps its shield: the proof-
    weighted fraction stays at or above ``direction_proof_shield_min_fraction``.

    Fail-safe: returns False when the rolling window has NO acceptance-aware merges
    (legacy / read-only / research directions), so their shield is untouched and
    behavior is unchanged for pre-acceptance data.
    """
    history = direction.get("research_history") or []
    window_cycles = int(_runtime_config.get("direction_churn_window_cycles", 5) or 5)
    if window_cycles <= 0:
        return False
    window = history[-window_cycles:]
    acc = [e for e in window if "acceptance_merged_write_total" in e]
    verified = sum(verified_merged_count(e) for e in acc)
    if verified <= 0:
        return False
    weighted = sum(proof_weighted_merged(e) for e in acc)
    threshold = float(
        _runtime_config.get("direction_proof_shield_min_fraction", 0.6) or 0.6
    )
    return (weighted / verified) < threshold


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
    merged = sum(verified_merged_count(e) for e in window)
    code_failed = sum(
        max(0, e.get("tasks_failed", 0) - e.get("tasks_failed_infra", 0))
        for e in window
    )
    min_no_pr = int(_runtime_config.get("direction_already_built_min_no_pr", 3) or 3)
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


def _direction_non_merge_pause(direction: dict) -> str | None:
    """Single consolidated non-merge-rate pause proxy — the mechanical trigger.

    Churn-guardrail redesign: the three former independent merge-rate rules
    (zero-merge, churn guardrail, lifetime burn backstop) are collapsed here into
    ONE proxy driven by a single signal — finished tasks that did not merge, versus
    the proof-weighted merge rate — evaluated at two horizons that share that same
    signal shape:

      1. Rolling window (via ``_compute_churn_metrics``) — the primary, responsive
         trigger. Two folded-in cases of the one signal:
           * FOLDED zero-merge: ``0`` proof-weighted merges landed over >=5 settled
             cycles with real (non-no-op, non-infra) unmerged work — the degenerate
             rate==0 tail of the proxy. Reason kept as ``Unproductive:`` for the
             downstream zombie-ladder / already-built classification.
           * FOLDED churn: unmerged volume is absolutely high AND the proof-weighted
             merge rate is very low. Reason ``Churn guardrail:``.
      2. Lifetime (via ``_compute_lifetime_burn_metrics``) — a slow-burn backstop for
         directions that stay under the window volume every window yet accumulate.
         Reason ``Lifetime burn backstop:``.

    Every branch reads the same non-merge definition (completed-minus-no-op plus
    non-infra code failures, minus proof-weighted merges) so infra-failure exclusion
    and already-built no-op suppression are preserved. Reason prefixes are unchanged
    so the lifecycle's evidence capture, zombie/lifetime ladders and already-built
    routing keep working. No terminal retirement happens here — only a reversible
    pause reason is returned. Callers gate this on ``not in_grace and not
    still_productive`` (grace + productivity/proof shields).
    """
    history = direction.get("research_history") or []

    # --- Folded zero-merge tail (was Rule 2): proof-weighted merge rate == 0 ---
    # 5+ cycles, no verified merges anywhere, most recent cycle settled (no tasks
    # in flight), and real unmerged work finished. This is the low-volume end of the
    # same non-merge proxy: a direction shipping literally nothing over five cycles.
    if len(history) >= 5:
        total_merged = sum(verified_merged_count(entry) for entry in history)
        # Raw merge volume across the direction's whole life (integration + staging
        # merges). A direction that merged real PRs — even ones whose rollup-landing
        # credit has not yet propagated back into verified_prs_merged — is NOT
        # "shipping literally nothing"; this zero-merge pause must not fire on the
        # merge-credit-propagation lag. Legacy entries lacking the field contribute 0,
        # preserving the pause for genuinely unproductive directions.
        real_merges_any = sum(
            int(entry.get("merged_prs_any_base") or 0) for entry in history
        )
        if total_merged == 0 and real_merges_any == 0:
            non_merge_real = sum(
                max(0, entry.get("tasks_completed", 0))
                - entry.get("tasks_completed_no_pr", 0)
                + max(
                    0,
                    entry.get("tasks_failed", 0) - entry.get("tasks_failed_infra", 0),
                )
                for entry in history
            )
            last = history[-1]
            created = last.get("tasks_created", 0)
            finished = last.get("tasks_completed", 0) + last.get("tasks_failed", 0)
            if (created == 0 or finished >= created) and non_merge_real > 0:
                total_created = sum(entry.get("tasks_created", 0) for entry in history)
                return (
                    f"Unproductive: 0 PRs merged from {len(history)} research cycles "
                    f"({total_created} tasks created)"
                )

    # --- Rolling-window churn (was Rule 5): high unmerged volume at a low rate ---
    churn_threshold = int(
        _runtime_config.get("direction_churn_non_merge_threshold", 15) or 15
    )
    churn_min_merge_rate_config = _runtime_config.get(
        "direction_churn_min_merge_rate", 0.10
    )
    churn_min_merge_rate = float(
        0.10 if churn_min_merge_rate_config is None else churn_min_merge_rate_config
    )
    if churn_threshold > 0:
        metrics = _compute_churn_metrics(direction)
        if (
            metrics
            and metrics["non_merge_finished"] >= churn_threshold
            and metrics["merge_rate"] < churn_min_merge_rate
            # Defense-in-depth: never pause a direction whose work is demonstrably
            # landing. ``real_merges`` counts PRs that actually merged (integration
            # OR staging) over the window; it stays non-zero even when a rollup
            # landing's merge-credit has not yet propagated into the credit-gate
            # ``merge_rate``. Genuine churn (nothing merged anywhere) keeps
            # ``real_merges == 0`` and still trips the guardrail.
            and metrics.get("real_merges", 0) == 0
        ):
            return (
                f"Churn guardrail: {metrics['non_merge_finished']} tasks finished "
                f"without merging, {metrics['merged']} merged in last "
                f"{metrics['window_cycles']} cycles "
                f"(merge rate {metrics['merge_rate']:.0%})"
            )

    # --- Lifetime cumulative burn backstop (was Rule 6): same signal, whole life ---
    lifetime_threshold = int(
        _runtime_config.get("direction_lifetime_non_merge_threshold", 150) or 150
    )
    lifetime_min_merge_rate_config = _runtime_config.get(
        "direction_lifetime_min_merge_rate", 0.10
    )
    lifetime_min_merge_rate = float(
        0.10
        if lifetime_min_merge_rate_config is None
        else lifetime_min_merge_rate_config
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


def _check_direction_health(direction: dict) -> str | None:
    """Return a pause reason if this direction should be auto-paused, else None.

    Consolidation (churn-guardrail redesign). The former six independent
    history-based pause rules are reduced to ONE mechanical non-merge-rate proxy
    (:func:`_direction_non_merge_pause`) plus two guards that the code shows are a
    genuinely different — and safer to keep separate — kind of signal:

      * FOLDED into the single proxy: zero-merge (Rule 2), churn (Rule 5) and
        lifetime burn (Rule 6). All three are one signal — unmerged finished work
        versus the proof-weighted merge rate — at the rate==0 tail, the rolling
        window, and the lifetime horizon respectively.
      * RETAINED as a separate mechanical guard — code-failure (Rules 3 & 4). This
        is a CRASH signal, not a merge-RATE one: it must stay classifiable
        (``_is_code_failure_pause_reason`` routes it to an investigate task and its
        own retire ladder) and it is deliberately UNGATED — a crashing direction
        pauses no matter how productive its last cycle looked and even inside the
        grace period.
      * DROPPED as a standalone rule but preserved in spirit — saturation (Rule 1).
        A saturated direction produces no actionable work at all (it may finish
        nothing), so a non-merge-RATE proxy that needs finished work cannot see it;
        it is kept as a small research-productivity guard rather than force-fitted
        into the proxy.

    Preserved semantics: grace period (the proxy + saturation skip until a new
    post-recovery cycle appends; code-failure always evaluated), the still-productive
    shield with proof-aware forfeit (:func:`_direction_still_productive` /
    :func:`_recent_merges_mostly_unproven`), and infra-failure exclusion. No terminal
    retirement happens here.
    """
    history = direction.get("research_history") or []

    # Grace period: a just-recovered direction skips the history-based non-merge
    # proxy and the saturation guard until at least one new research cycle is
    # appended after recovery, so the frozen research_history that triggered the
    # original pause cannot immediately re-trigger it. The code-failure guard below
    # is always evaluated so auto-retirement is never weakened by the grace period.
    recovered_at = direction.get("auto_recovered_at")
    in_grace = recovered_at is not None and not any(
        (e.get("timestamp") or "") > recovered_at for e in history
    )

    # Productivity/proof shield for the non-merge proxy only: a direction that is
    # still demonstrably productive must not be pause-retired by a merge-rate signal.
    # The code-failure guard is deliberately left UNGATED. The shield is FORFEIT when
    # the direction's recent merges are essentially all undeclared/unproven
    # (_recent_merges_mostly_unproven): "productive" must mean proven work.
    still_productive = _direction_still_productive(
        direction
    ) and not _recent_merges_mostly_unproven(direction)

    # Saturation guard (retained): research produced no actionable findings.
    if not in_grace and direction.get("possibly_saturated") and len(history) >= 5:
        recent5 = history[-5:]
        if all(entry.get("tasks_created", 0) <= 1 for entry in recent5):
            return "Saturated: last 5 research cycles produced no actionable findings"

    # Single consolidated non-merge-rate proxy (folds zero-merge + churn + lifetime).
    # Evaluated BEFORE the code-failure guards so that a zero-merge, non-productive
    # direction is always classified as 'Unproductive:' (reversible, non-terminal)
    # rather than 'Recent stall:' (code_failure_pause_count, terminal at 3 strikes).
    # The code-failure guards below then only fire on directions that passed the
    # non-merge proxy (in_grace, still_productive, or no non-merge trigger).
    if not in_grace and not still_productive:
        proxy_reason = _direction_non_merge_pause(direction)
        if proxy_reason:
            return proxy_reason

    # Code-failure guard (retained, UNGATED): a crash signal that routes to an
    # investigate task. Rule 3 — >70% of spawned tasks failed for CODE reasons over
    # the last 3 cycles (infra failures excluded, uncorrelated with direction quality).
    if len(history) >= 3:
        recent3 = history[-3:]
        total_created_3 = sum(entry.get("tasks_created", 0) for entry in recent3)
        total_failed_3 = sum(entry.get("tasks_failed", 0) for entry in recent3)
        infra_failed_3 = sum(entry.get("tasks_failed_infra", 0) for entry in recent3)
        code_failed_3 = total_failed_3 - infra_failed_3
        if total_created_3 > 0 and code_failed_3 / total_created_3 > 0.7:
            return f"High code failure rate: >70% of spawned tasks failed for code reasons in last 3 cycles ({code_failed_3}/{total_created_3}, {infra_failed_3} infra failures excluded)"

    # Rule 4 — recent stall: last 3 cycles all either failed as research tasks
    # (crash/timeout/provision) or produced children with >=1 CODE failure and zero
    # merges. Infra failures are excluded for the same reason as Rule 3.
    if len(history) >= 3:
        recent3 = history[-3:]

        def _cycle_failed(e: dict) -> bool:
            if e.get("research_task_failed"):
                return True
            total_failed = e.get("tasks_failed", 0)
            infra_failed = e.get("tasks_failed_infra", 0)
            code_failed = max(0, total_failed - infra_failed)
            return code_failed > 0 and verified_merged_count(e) == 0

        if all(_cycle_failed(e) for e in recent3):
            return (
                "Recent stall: last 3 research cycles all failed or produced no merges"
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


def _direction_has_recent_merges(direction: dict) -> bool:
    """True when a direction has merged PRs recently (rolling window) or over its
    aggregated lifetime — i.e. it is still shipping output, even if unproven.

    Used to keep a still-shipping direction OUT of the zero-output exhaustion
    class: a direction that keeps merging PRs (verified or not) has work, so it is
    not "genuinely exhausted". The proven >=3-verified-merge route owns retiring
    productive saturated directions; the exhaustion route here is for directions
    producing nothing. ``prs_merged`` (raw) is used deliberately — any merge, even
    an unproven one, signals the direction is not output-empty.
    """
    history = direction.get("research_history") or []
    window = history[-5:]
    if sum(int(e.get("prs_merged", 0) or 0) for e in window) > 0:
        return True
    agg = direction.get("history_aggregate") or {}
    try:
        return int(agg.get("total_prs_merged", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def _direction_genuinely_exhausted(direction: dict) -> bool:
    """True when a direction has no new work left — the shared exhaustion predicate.

    A direction is genuinely exhausted when it is dedup-deadlocked, already-fixed
    -deadlocked, OR saturated while no longer shipping any output. All three mean
    "there is no new work in this direction":

      * dedup-deadlocked — research cycles keep producing findings immediately
        rejected as already-known (``_detect_dedup_deadlock``);
      * already-fixed-deadlocked — spawned tasks all terminate SCOPE_EMPTY because
        the work is already on HEAD (``_detect_already_fixed_deadlock``);
      * saturated (``possibly_saturated`` / ``_compute_saturation``) AND not still
        shipping output (``_direction_has_recent_merges`` is False). The output
        guard keeps a saturated-but-still-merging direction — which is a proof
        /quality concern, not exhaustion — out of this class so the proven-merge
        route remains the only path that retires it.

    Detection helpers live in ``research_direction_state`` and are imported lazily
    to avoid a module-level import cycle (that module lazily imports this one).
    """
    from app.research_direction_state import (
        _compute_saturation,
        _detect_already_fixed_deadlock,
        _detect_dedup_deadlock,
    )

    history = direction.get("research_history") or []
    if _detect_dedup_deadlock(history)[0]:
        return True
    if _detect_already_fixed_deadlock(history)[0]:
        return True
    saturated = bool(direction.get("possibly_saturated")) or _compute_saturation(
        history
    )
    return saturated and not _direction_has_recent_merges(direction)


def _direction_code_failing(direction: dict) -> bool:
    """Guardrail 1: a code-failing direction is BROKEN, not exhausted.

    True when ``_check_direction_health`` returns a code-failure pause reason
    (Rules 3/4 — high code-failure rate or recent stall). Such a direction must
    NOT be auto-completed as exhausted: the existing pause / diagnosis /
    auto-retire-after-N path owns it. A crashing direction is broken, not done.
    """
    return _is_code_failure_pause_reason(_check_direction_health(direction))


def _direction_externally_frozen(direction: dict) -> bool:
    """Guardrail 2: a direction blocked by factors external to its own exhaustion.

    Such a direction may still have real work once the external block clears, so
    it must not be retired as exhausted. Derived entirely from EXISTING fields:

      * ``needs_manual_intervention`` — operator action pending;
      * ``cost_pause_evidence`` present, or cumulative estimated cost exceeds the
        direction's ``cost_budget_usd`` (a budget/cost pause);
      * a recorded budget / deploy-health / external-dependency pause reason.
    """
    if direction.get("needs_manual_intervention"):
        return True
    if direction.get("cost_pause_evidence"):
        return True
    budget = direction.get("cost_budget_usd")
    try:
        budget_f = float(budget) if budget is not None else None
    except (TypeError, ValueError):
        budget_f = None
    if budget_f and budget_f > 0:
        cumulative = 0.0
        for entry in direction.get("research_history") or []:
            raw = entry.get("estimated_cost_usd") or 0.0
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                cumulative += max(0.0, float(raw))
        if cumulative > budget_f:
            return True
    paused_reason = direction.get("paused_reason")
    return isinstance(paused_reason, str) and (
        paused_reason.startswith("Budget exceeded:")
        or "external_dependency" in paused_reason
        or "deploy" in paused_reason.lower()
    )


def _exhaustion_complete_reason(direction: dict) -> str:
    """Build the reason for an exhaustion-retired direction.

    Exhaustion is explicitly non-successful.  The verified merge count remains
    useful context, but can never promote this outcome to success.
    """
    history = direction.get("research_history") or []
    agg = direction.get("history_aggregate") or {}
    total_merged = sum(verified_merged_count(e) for e in history) + int(
        agg.get("total_verified_prs_merged", agg.get("total_prs_merged", 0)) or 0
    )
    return (
        f"Auto-retired as exhausted: direction saturated/deadlocked with "
        f"{total_merged} verified PRs merged (no new work remaining)"
    )


def _should_autocomplete_exhausted(
    direction: dict, *, exhausted: bool | None = None
) -> bool:
    """Shared decision: should this direction retire as exhausted?

    ``exhausted`` lets a caller that has ALREADY detected a specific deadlock
    signal (e.g. the already-fixed site detects on ``history[:-1]`` because the
    just-appended cycle's children have not resolved yet) pass that verdict in;
    otherwise the shared ``_direction_genuinely_exhausted`` predicate is used. The
    two guardrails — code-failing and externally-frozen — are applied identically
    regardless of how exhaustion was determined.
    """
    is_exhausted = (
        exhausted
        if exhausted is not None
        else _direction_genuinely_exhausted(direction)
    )
    if not is_exhausted:
        return False
    if _direction_code_failing(direction):
        return False
    return not _direction_externally_frozen(direction)


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
    total_merged = sum(verified_merged_count(entry) for entry in history)
    if total_merged < 3:
        return None
    recent2 = history[-2:]
    recent_created = sum(e.get("tasks_created", 0) for e in recent2)
    recent_merged = sum(verified_merged_count(e) for e in recent2)
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


# ── Direction objective completion (campaign path) ────────────────────────────
# A direction (multi-task campaign) no longer 'completes' merely when its child
# PRs merge: once its integrated merged diff has been graded against its
# DECLARED OBJECTIVE, the graded verdict is authoritative for the completion
# signal.  The trigger lives in
# ``rollup_management._maybe_spawn_direction_merge_grade`` (spawn) and the
# lifecycle consumes the completed grade via
# :func:`consume_direction_merge_grade_if_ready`; these helpers read the
# DIRECTION-scoped category-verdict ledger so the lifecycle gate and the digest
# surface reflect the OBJECTIVE, not child-merge counts.  Directions with no
# declared rubric or no graded verdict return ``None`` and keep today's
# merge-count heuristic behaviour (backward compatible).


async def _direction_active_category_keys(
    session: AsyncSession,
    project: Project | None,
    direction: dict,
    direction_id: str,
) -> set[str]:
    """Resolve a direction's ACTIVE acceptance-criterion keys.

    Reads the in-memory direction dict's ``criterion_keys`` first; when the
    dict does not carry them (legacy/synthetic directions) it falls back to the
    first-class ``Direction.criterion_keys`` column via ``get_direction_row``
    (the in-memory dicts do not reliably carry the promoted column — see
    ``acceptance_criteria._direction_owns_blocking_uncovered_criteria``).
    """
    keys = direction.get("criterion_keys")
    if not isinstance(keys, list):
        keys = None
    if not keys and project is not None:
        from app.directions_repo import get_direction_row  # noqa: PLC0415

        try:
            row = await get_direction_row(session, project.id, direction_id)
        except Exception:  # noqa: BLE001 — best-effort fallback
            row = None
        if row is not None:
            keys = list(getattr(row, "criterion_keys", None) or [])
    if not isinstance(keys, list):
        return set()
    return {str(k).strip() for k in keys if str(k).strip()}


async def _direction_active_categories(
    session: AsyncSession,
    project: Project | None,
    direction: dict,
    active_keys: set[str],
) -> list[dict[str, str]]:
    """Resolve active criterion keys to grader-facing rubric categories.

    The blind merge-grade rubric must carry the human-readable criterion TEXT:
    the version path passes ``VersionAcceptanceCriterion.text`` and a key-only
    rubric (``c-1a2b3c4d: c-1a2b3c4d``) is ungradeable.  A key whose row cannot
    be resolved falls back to the key itself so the category degrades rather
    than silently dropping out of the rubric.
    """
    text_by_key: dict[str, str] = {}
    if project is not None and active_keys:
        from app.acceptance_criteria import (  # noqa: PLC0415
            direction_criterion_release_version_id,
            get_criteria_by_keys,
        )

        try:
            rows = await get_criteria_by_keys(
                session,
                project.id,
                sorted(active_keys),
                release_version_id=direction_criterion_release_version_id(direction),
            )
        except Exception:  # noqa: BLE001 — best-effort resolution
            rows = []
        for row in rows:
            text = str(getattr(row, "text", "") or "").strip()
            if text:
                text_by_key[row.criterion_key] = text
    return [
        {"category_key": k, "text": text_by_key.get(k, k)}
        for k in sorted(active_keys)
    ]


async def direction_objective_completion_verdict(
    session: AsyncSession,
    project: Project | None,
    direction: dict,
) -> bool | None:
    """Return the direction's graded objective-completion verdict, or None.

    ``None`` means there is no graded verdict to consult (no declared rubric,
    or no grade has landed yet) — the caller keeps today's heuristic behavior.
    ``True``/``False`` are authoritative: a graded fully-met objective marks the
    direction complete; a graded not-met objective suppresses completion no
    matter what the merge-count heuristic says.

    Mirrors the version path's gate: the rubric-gated ledger oracle
    (:func:`app.category_verdict_ledger.is_candidate_fully_met`) is consulted
    only when the direction declares acceptance criteria (``criterion_keys``).
    """
    if project is None:
        return None
    did = str(direction.get("id") or direction.get("direction_id") or "").strip()
    if not did:
        return None
    active_keys = await _direction_active_category_keys(
        session, project, direction, did
    )
    if not active_keys:
        # No rubric to grade against — the ledger oracle fails closed on an
        # EMPTY acceptance set, and an unrubricated direction has nothing to
        # certify.  Mirror the version path (which only gates when active
        # categories exist) and defer to the heuristic.
        return None
    from app import category_verdict_ledger as _ledger  # noqa: PLC0415

    candidate_sha = await _ledger.get_latest_direction_candidate_sha(session, did)
    if not candidate_sha:
        return None
    try:
        return await _ledger.is_candidate_fully_met(
            session,
            direction_id=did,
            candidate_sha=candidate_sha,
            active_category_keys=active_keys,
        )
    except Exception:  # noqa: BLE001 — a ledger hiccup must not break lifecycle
        logger.warning(
            "[STRATEGIC] failed to read direction objective verdict for %s",
            did,
            exc_info=True,
        )
        return None


async def consume_direction_merge_grade_if_ready(
    session: AsyncSession,
    project: Project | None,
    direction: dict,
) -> bool:
    """Record a completed direction-scoped merge grade into the ledger, if any.

    Finds the direction's most recent COMPLETED ``release_merge_grade`` task;
    when its candidate has not been recorded yet, reads the
    ``RELEASE_MERGE_GRADE`` artifact and persists the verdicts + objective_gap
    debt via ``record_direction_merge_grade`` (the direction-scoped RESULT side
    of the blind objective grade).  Returns True when a grade was newly
    recorded, False otherwise.  Best-effort: never raises — a ledger hiccup
    must not break the lifecycle tick.
    """
    if project is None:
        return False
    did = str(direction.get("id") or direction.get("direction_id") or "").strip()
    if not did:
        return False
    from app import category_verdict_ledger as _ledger  # noqa: PLC0415
    from app.database import resolve_dialect_name  # noqa: PLC0415
    from app.models import ArtifactType  # noqa: PLC0415
    from app.release_qualifier_audit import (  # noqa: PLC0415
        _latest_artifact_logs,
        _resolve_direction_objective,
        record_direction_merge_grade,
    )
    from sqlalchemy import cast as _cast  # noqa: PLC0415
    from sqlalchemy import func as _func  # noqa: PLC0415
    from sqlalchemy.dialects.postgresql import JSONB as _JSONB  # noqa: PLC0415

    try:
        dialect_name = resolve_dialect_name(session)
        if dialect_name == "sqlite":
            direction_filter = _func.json_extract(
                Task.context, "$.direction_id"
            ) == did
            type_filter = (
                _func.json_extract(Task.payloads, "$[0].type")
                == "release_merge_grade"
            )
        else:
            direction_filter = Task.context.op("@>")(
                _cast({"direction_id": did}, _JSONB)
            )
            type_filter = (
                Task.payloads[0]["type"].as_string() == "release_merge_grade"
            )
        task = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.project_id == project.id,
                        Task.status == TaskStatus.COMPLETED,
                        type_filter,
                        direction_filter,
                    )
                    .order_by(Task.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
    except Exception:  # noqa: BLE001 — best-effort consumption
        logger.warning(
            "[STRATEGIC] failed to look up direction merge grade for %s",
            did,
            exc_info=True,
        )
        return False
    if task is None:
        return False
    ctx = task.context or {}
    candidate_sha = str(ctx.get("candidate_sha") or "").strip() or None
    if not candidate_sha:
        return False
    if await _ledger.get_category_verdicts(
        session, None, candidate_sha, direction_id=did
    ):
        return False  # already recorded
    artifact = await _latest_artifact_logs(
        session, task.id, ArtifactType.RELEASE_MERGE_GRADE
    )
    if not artifact:
        return False
    objective = _resolve_direction_objective(direction)
    if not objective:
        return False
    active_keys = await _direction_active_category_keys(
        session, project, direction, did
    )
    active_categories = await _direction_active_categories(
        session, project, direction, active_keys
    )
    _attempt_id = ctx.get("qualification_attempt_id")
    try:
        _attempt_uuid = uuid.UUID(str(_attempt_id)) if _attempt_id else None
    except (ValueError, TypeError):
        _attempt_uuid = None
    try:
        _result = await record_direction_merge_grade(
            session,
            direction_id=did,
            project_id=project.id,
            goal=objective,
            artifact=artifact,
            active_categories=active_categories or None,
            qualification_attempt_id=_attempt_uuid,
            grader_run_id=str(task.id),
        )
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning(
            "[STRATEGIC] failed to record direction merge grade for %s",
            did,
            exc_info=True,
        )
        return False
    # True ONLY when at least one verdict/debt row was actually persisted — an
    # artifact with no gradable content (missing graded_candidate_sha, no
    # per-category verdicts) records nothing, and re-stamping the direction row
    # every cycle just because the grade task is COMPLETED is write amplification.
    return bool(_result and _result.get("ledger_writes"))


# ── Outcome-progress (scaffolding) detector ──────────────────────────────────
# Advisory axis catching the INVERSE of every existing health rule: a direction
# with a HEALTHY merge rate that keeps shipping refactor/scaffolding PRs while
# never advancing its stated success_evidence. Such a direction scores high on
# quality_score and looks 'stable' on merge-rate trend, so it can burn dozens of
# cycles unnoticed. The flag is advisory evidence surfaced to the strategic
# agent/operator; it never pauses or retires a direction by itself.

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


def _outcome_progress_merged_total(history: list[dict], agg: dict) -> int:
    return sum(credit_gate_merged_count(e) for e in history) + max(
        0,
        int(agg.get("total_verified_prs_merged", agg.get("total_prs_merged", 0)) or 0)
        - int(agg.get("total_stranded_prs_merged", 0) or 0),
    )


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
    # Reachable credit basis; reset MUST use the same helper so deltas align.
    total_merged = _outcome_progress_merged_total(history, agg)
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
    "stall_advisory",
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
    # Reachable credit basis; MUST match ``record_outcome_progress_snapshot``.
    total_merged = _outcome_progress_merged_total(history, agg)
    direction["outcome_progress_reset_at_cycle"] = total_cycles
    direction["outcome_progress_reset_at_merged"] = total_merged
    return cleared


def _evidence_progress_suppresses_scaffolding(
    direction: dict, total_cycles: int, stall_run: int
) -> bool:
    """True if a CHALLENGE-APPROVED, current agent evidence_progress verdict confirms advancement.

    An ``advancing`` or ``met`` verdict recorded within the last ``stall_run``
    cycles suppresses the advisory scaffolding flag — the agent has confirmed
    genuine progress, so the deterministic detector defers to it. A ``stalled``
    verdict (which reinforces the deterministic signal) or NO verdict at all
    never suppresses, so the deterministic detector stays the guaranteed
    backstop whenever the agent omits the optional assessment. The verdict
    expires after a full stall window so a stale "advancing" cannot mask a
    direction that later genuinely stalls.

    FIRST HARD GATE: a self-produced advancing/met verdict only earns suppression
    when an INDEPENDENT challenge approved the same direction_id + status — i.e.
    ``_apply_evidence_progress_verdicts`` stamped ``challenge_approved: True`` onto
    the persisted verdict from the stored challenge record. An unchallenged,
    rejected, uncertain, or malformed approval leaves ``challenge_approved`` falsy,
    so the deterministic scaffolding backstop stays active.
    """
    ep = direction.get("evidence_progress")
    if not isinstance(ep, dict):
        return False
    if ep.get("status") not in _EVIDENCE_PROGRESS_SUPPRESSING:
        return False
    if not ep.get("challenge_approved"):
        gate_mode = str(ep.get("gate_mode", "enforce")).strip().lower()
        if gate_mode not in {"observe", "advisory", "enforce"}:
            gate_mode = "enforce"
        if gate_mode == "enforce":
            return False
        if gate_mode == "advisory" and ep.get("gate_would_block"):
            logger.warning(
                "strategic challenge gate [advisory]: evidence_progress suppression "
                "allowed but gate would have blocked (direction cycle_task_id=%s)",
                ep.get("cycle_task_id", "?"),
            )
        # observe or advisory: allow suppression; fall through to staleness check
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


_RETIRED_MEMORY_CAP = 30
_RETIRED_TEXT_TOKEN_CAP = 20
# Bounded char cap for the optional per-entry outcome blurb ("why it was
# retired / what landed"). The list is shipped in the strategist payload, so
# the outcome must stay compact — same spirit as the token cap above.
_RETIRED_OUTCOME_CHAR_CAP = 200


def _retired_direction_outcome_metrics(direction: dict) -> dict[str, int]:
    history = direction.get("research_history") or []
    agg = direction.get("history_aggregate") or {}
    total_cycles = len(history) + int(agg.get("total_cycles") or 0)
    total_merged = sum(int(h.get("prs_merged", 0) or 0) for h in history) + int(
        agg.get("total_prs_merged") or 0
    )
    return {"cycles": total_cycles, "prs_merged": total_merged}


def record_retired_direction_memory(
    vision: dict,
    direction: dict,
    *,
    reason: str,
    outcome: str | None = None,
) -> None:
    """Add a compact entry to vision['_recently_retired_directions'].

    Called at every terminal-status write so the create_direction dedup check
    can block same-gap or text-overlap re-spawns even after direction pruning.
    Mutates vision in place; caller is responsible for flag_modified + session.add.

    ``outcome`` is an OPTIONAL compact blurb capturing *why* the direction was
    retired / *what* it shipped (e.g. the retirement rationale, or a
    ``superseded by <id>`` successor pointer). It is bounded to
    ``_RETIRED_OUTCOME_CHAR_CAP`` chars and only stored when non-empty, so
    existing callers that omit it keep the previous entry shape unchanged.
    """
    text = direction.get("text") or ""
    tokens = [
        w for w in text.lower().split() if w not in _DIRECTION_DEDUP_STOPWORDS
    ][:_RETIRED_TEXT_TOKEN_CAP]

    metrics = _retired_direction_outcome_metrics(direction)
    classification = classify_retired_direction_outcome(
        reason,
        prs_merged=metrics["prs_merged"],
        retirement_outcome=direction.get("retirement_outcome"),
    )

    entry: dict = {
        "id": direction.get("id", ""),
        "text": text[:_RETIRED_OUTCOME_CHAR_CAP],
        "text_tokens": tokens,
        "retired_at": _utcnow().isoformat(),
        "reason": reason,
        # Preserve legacy top-level metrics for older readers while placing the
        # classified outcome in one compact object for new grounding consumers.
        "cycles": metrics["cycles"],
        "prs_merged": metrics["prs_merged"],
        "retired_outcome": {
            "classification": classification,
            "metrics": metrics,
        },
    }
    if direction.get("retirement_outcome"):
        entry["retirement_outcome"] = str(direction["retirement_outcome"])[
            :_RETIRED_OUTCOME_CHAR_CAP
        ]
    gap_id = direction.get("gap_id")
    if gap_id:
        entry["gap_id"] = str(gap_id)
    if outcome:
        _bounded = str(outcome).strip()[:_RETIRED_OUTCOME_CHAR_CAP]
        if _bounded:
            entry["outcome"] = _bounded
    existing = [
        e for e in (vision.get("_recently_retired_directions") or [])
        if not entry["id"] or e.get("id") != entry["id"]
    ]
    existing.append(entry)
    vision["_recently_retired_directions"] = existing[-_RETIRED_MEMORY_CAP:]


def _check_gap_respawn_block(
    text: str,
    new_gap_id: str | None,
    retired_memory: list,
    cooldown_days: float,
) -> str | None:
    """Return block reason if text/gap matches a recently-retired direction, else None.

    Gate 1: exact gap_id match against any retired memory entry within cooldown window.
    Gate 2: text-token overlap >=4 (same threshold as _is_direction_duplicate).
    Returns None when no block applies.
    """
    match = find_gap_respawn_match(
        text,
        new_gap_id,
        retired_memory,
        cooldown_days,
    )
    if match is None:
        return None
    mem_entry, match_kind = match
    annotation = build_retired_direction_grounding(mem_entry)
    return _format_retired_grounding_reason(
        mem_entry,
        match_kind=match_kind,
        new_gap_id=new_gap_id,
        grounding=annotation,
    )


def find_gap_respawn_match(
    text: str,
    new_gap_id: str | None,
    retired_memory: list,
    cooldown_days: float,
) -> tuple[dict, str] | None:
    cooldown_cutoff = (_utcnow() - timedelta(days=cooldown_days)).isoformat()
    new_text_tokens = set(text.lower().split()) - _DIRECTION_DEDUP_STOPWORDS
    for mem_entry in retired_memory:
        if not isinstance(mem_entry, dict):
            continue
        if mem_entry.get("retired_at", "") < cooldown_cutoff:
            continue
        if new_gap_id and mem_entry.get("gap_id") == new_gap_id:
            return mem_entry, "gap"
        mem_tokens = set(mem_entry.get("text_tokens") or [])
        if len(new_text_tokens & mem_tokens) >= 4:
            return mem_entry, "text"
    return None


def _format_retired_grounding_reason(
    mem_entry: dict,
    *,
    match_kind: str,
    new_gap_id: str | None,
    grounding: dict | None,
) -> str:
    direction_id = mem_entry.get("id")
    reason = mem_entry.get("reason")
    if match_kind == "gap":
        prefix = (
            f"gap {new_gap_id} was recently staffed by retired direction "
            f"{direction_id}"
        )
    else:
        prefix = f"text overlaps recently-retired direction {direction_id}"

    classification = (grounding or {}).get("classification")
    metrics = (grounding or {}).get("metrics") or {}
    if classification in {"landed", "success_evidence_met"}:
        outcome_label = (
            "landed work"
            if classification == "landed"
            else "met success evidence"
        )
        return (
            f"{prefix} whose prior outcome was {outcome_label} "
            f"(prs_merged={metrics.get('prs_merged', 0)}, "
            f"cycles={metrics.get('cycles', 0)}; {reason})"
        )
    return f"{prefix} ({reason})"


def _retired_grounding_note(grounding: dict, block_reason: str) -> str:
    classification = grounding.get("classification") or "unknown"
    metrics = grounding.get("metrics") or {}
    direction_id = grounding.get("direction_id") or "unknown"
    note = (
        "Grounding: matches retired direction "
        f"{direction_id}; prior_outcome={classification}; "
        f"prs_merged={metrics.get('prs_merged', 0)}, "
        f"cycles={metrics.get('cycles', 0)}. {block_reason}"
    )
    memo = grounding.get("retirement_memo")
    if isinstance(memo, dict) and memo.get("knowledge_doc_id"):
        note += (
            f" Retirement memo {memo.get('knowledge_doc_id')}: "
            f"{str(memo.get('summary') or '')[:160]}"
        )
    return note


async def _check_direction_starvation(
    project: Any, dirs_created: int, session: AsyncSession, task: Any = None
) -> None:
    """Detect repeated cycles that leave concrete problem-model gaps uncovered.

    Direction count, category mix, saturation, and fleet capacity are not gaps.
    Only an active problem-model gap with no staffing move and no explicit
    deferral contributes to this signal. Repeated misses raise an alert and
    increase strategic review frequency; this guard never creates directions.
    """
    from app.alert_dispatch import _create_strategic_alert

    raw_model = project.problem_model if isinstance(project.problem_model, dict) else {}
    raw_gaps = raw_model.get("gaps")
    uncovered_gaps: list[dict[str, Any]] = []
    if isinstance(raw_gaps, list):
        for gap in raw_gaps:
            if not isinstance(gap, dict) or gap.get("status", "active") != "active":
                continue
            # Require a concrete durable object, not a malformed placeholder.
            if not str(gap.get("id") or "").strip():
                continue
            if not str(gap.get("name") or gap.get("description") or "").strip():
                continue
            active_moves = gap.get("active_moves")
            if isinstance(active_moves, list) and active_moves:
                continue
            if gap.get("deferral"):
                continue
            uncovered_gaps.append(gap)

    vision = dict(project.vision or {})
    if uncovered_gaps:
        task_id_str = str(task.id) if task is not None else None
        if task_id_str and vision.get("_direction_starvation_last_task_id") == task_id_str:
            # Already processed this cycle on a prior run — skip to avoid double-counting.
            return
        starvation_count = vision.get("_direction_starvation_count", 0) + 1
        vision["_direction_starvation_count"] = starvation_count
        if task_id_str:
            vision["_direction_starvation_last_task_id"] = task_id_str

        if starvation_count >= 2:
            gap_ids = [str(gap["id"]) for gap in uncovered_gaps]
            gap_desc = ", ".join(gap_ids[:5])
            if len(gap_ids) > 5:
                gap_desc += f" (+{len(gap_ids) - 5} more)"
            vision["_strategic_frequency_override"] = 2
            project.vision = vision
            flag_modified(project, "vision")
            session.add(project)
            await _create_strategic_alert(
                project.id,
                session,
                f"Uncovered strategic gaps: {starvation_count} consecutive cycles left "
                f"active, non-deferred problem-model gap(s) unstaffed ({gap_desc}). "
                f"Boosting strategic frequency; no direction was auto-created.",
            )
            logger.warning(
                f"[STRATEGIC] Uncovered gaps for project {project.id}: "
                f"{starvation_count} cycles, gaps: {gap_desc}. Frequency override → 2."
            )
        else:
            project.vision = vision
            flag_modified(project, "vision")
            session.add(project)
            logger.info(
                f"[STRATEGIC] Uncovered-gap cycle {starvation_count}/2 "
                f"for project {project.id}"
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
                f"[STRATEGIC] Uncovered-gap signal resolved for project {project.id}: "
                f"no concrete unstaffed gap remains. Counter and override cleared."
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
    # First hard gate substrate: the stored independent challenge record. An
    # advancing/met verdict only earns ``challenge_approved`` when the challenge
    # endorsed the same direction_id + status; a stalled verdict is never a
    # suppressor so it never needs (or gets) an approval stamp.
    challenge_record = (task.context or {}).get(STRATEGIC_CHALLENGE_CONTEXT_KEY)
    gate_mode_raw = (
        str(_runtime_config.get("strategic_challenge_gate_mode", "enforce"))
        .strip()
        .lower()
    )
    gate_mode = (
        gate_mode_raw
        if gate_mode_raw in {"observe", "advisory", "enforce"}
        else "enforce"
    )
    now_iso = _utcnow().isoformat()
    from app.stall_advisory import apply_stall_advisory, clear_current_stall_advisory

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
        challenge_approved = status in _EVIDENCE_PROGRESS_SUPPRESSING and (
            challenge_approves_evidence_progress(challenge_record, d.get("id"), status)
        )
        gate_would_block = (
            status in _EVIDENCE_PROGRESS_SUPPRESSING and not challenge_approved
        )
        d["evidence_progress"] = {
            "status": status,
            "rationale": str(verdict.get("rationale", ""))[
                :_EVIDENCE_PROGRESS_RATIONALE_MAX
            ],
            "recorded_at": now_iso,
            "cycle_index": cycle_index,
            "cycle_task_id": str(task.id),
            "challenge_approved": challenge_approved,
            "gate_mode": gate_mode,
            "gate_would_block": gate_would_block,
        }
        if status == "stalled" and isinstance(verdict.get("stall_advisory"), dict):
            apply_stall_advisory(
                d,
                verdict["stall_advisory"],
                recorded_at=now_iso,
                cycle_index=cycle_index,
                cycle_task_id=str(task.id),
            )
        elif status in _EVIDENCE_PROGRESS_SUPPRESSING and (
            challenge_approved or gate_mode in {"observe", "advisory"}
        ):
            # Progress clears only the current warning/advisory. Historical
            # observations remain available for later policy evaluation.
            clear_current_stall_advisory(d)
        applied += 1
    if applied:
        flag_modified(project, "directions")
    return applied


def _compute_model_gc_telemetry(project: Project, task: Task) -> dict:
    """Compute per-cycle model-size and payload-truncation telemetry for cycle_metadata.

    Reads from project.problem_model (durable, not the trimmed agent view) and
    from the assembled payload's _packer_meta so the retirement monitor can verify
    size thresholds and payload coverage without querying the task payload directly.
    """
    from app.problem_model import ProblemModel, compute_model_size_signal

    raw_pm = project.problem_model or {}
    try:
        pm = ProblemModel.model_validate(raw_pm)
        size_signal = compute_model_size_signal(pm)
        model_bytes: int = size_signal["serialized_bytes"]
        capability_count: int = size_signal["capability_count"]
        gap_count: int = size_signal["active_gap_count"]
        model_size_status: str = size_signal["model_size_status"]
    except Exception:  # noqa: BLE001
        model_bytes = len(json.dumps(raw_pm, default=str).encode())
        capability_count = len(raw_pm.get("capabilities") or [])
        gap_count = len(
            [
                g
                for g in (raw_pm.get("gaps") or [])
                if isinstance(g, dict) and g.get("status") == "active"
            ]
        )
        model_size_status = "unknown"

    packer_meta: dict = {}
    try:
        first_payload = task.first_payload or {}
        packer_meta = first_payload.get("_packer_meta") or {}
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed to read packer metadata from task.first_payload",
            exc_info=True,
        )

    dropped: list = packer_meta.get("dropped") or []
    trimmed: list = packer_meta.get("trimmed") or []
    shipped: list = packer_meta.get("shipped") or []

    directions_dropped_count: int = sum(
        1
        for e in dropped
        if isinstance(e, dict) and "direction" in str(e.get("key", "")).lower()
    )
    problem_model_truncated: bool = any(
        isinstance(e, dict) and "problem_model" in str(e.get("key", "")).lower()
        for e in (dropped + trimmed)
    )
    open_messages_dropped: bool = any(
        isinstance(e, dict) and str(e.get("key", "")) == "open_messages"
        for e in dropped
    )
    version_phase_dropped: bool = any(
        isinstance(e, dict) and str(e.get("key", "")) == "version_phase"
        for e in dropped
    )
    # High/critical-tier sections that dropped wholesale under budget pressure —
    # the packer stamps each drop entry with its schema tier. A non-empty list
    # means the strategist ran on a degraded payload; empty means clean.
    high_tier_dropped: list[str] = [
        str(e.get("key"))
        for e in dropped
        if isinstance(e, dict) and e.get("priority") in ("high", "critical")
    ]

    gap_memory_trimmed: list = packer_meta.get("gap_memory_trimmed") or []
    gap_memory_trimmed_count: int = sum(
        len(entry.get("gaps") or [])
        for entry in gap_memory_trimmed
        if isinstance(entry, dict)
    )
    gap_memory_hidden_entries: int = 0
    for entry in gap_memory_trimmed:
        if not isinstance(entry, dict):
            continue
        for gap in entry.get("gaps") or []:
            if not isinstance(gap, dict):
                continue
            fields = gap.get("fields") or {}
            if not isinstance(fields, dict):
                continue
            for field_val in fields.values():
                if not isinstance(field_val, dict):
                    continue
                total = field_val.get("total") or 0
                shown = field_val.get("shown") or 0
                if isinstance(total, int) and isinstance(shown, int):
                    gap_memory_hidden_entries += max(0, total - shown)

    budget_total: int = packer_meta.get("budget_total") or 0
    budget_used: int = packer_meta.get("budget_used") or 0
    budget_utilization_pct: float | None = (
        round(budget_used / budget_total * 100, 1) if budget_total > 0 else None
    )

    # Merge byte sizes from all three accounting paths: shipped (full-size),
    # trimmed (bytes_trimmed = shipped size), dropped (bytes = original size).
    section_bytes: dict[str, int] = {}
    for entry in shipped:
        if isinstance(entry, dict) and entry.get("key"):
            section_bytes[str(entry["key"])] = int(entry.get("bytes") or 0)
    for entry in trimmed:
        if isinstance(entry, dict) and entry.get("key"):
            section_bytes[str(entry["key"])] = int(entry.get("bytes_trimmed") or 0)
    for entry in dropped:
        if isinstance(entry, dict) and entry.get("key"):
            # dropped sections contributed 0 bytes to the packed payload
            section_bytes.setdefault(str(entry["key"]), 0)
    top_budget_consumers: list[dict] = sorted(
        [{"key": k, "bytes": v} for k, v in section_bytes.items()],
        key=lambda x: x["bytes"],
        reverse=True,
    )[:5]

    return {
        "model_bytes": model_bytes,
        "capability_count": capability_count,
        "gap_count": gap_count,
        "model_size_status": model_size_status,
        "directions_dropped_count": directions_dropped_count,
        "problem_model_truncated": problem_model_truncated,
        "open_messages_dropped": open_messages_dropped,
        "version_phase_dropped": version_phase_dropped,
        "high_tier_dropped": high_tier_dropped,
        "gap_memory_trimmed_count": gap_memory_trimmed_count,
        "gap_memory_hidden_entries": gap_memory_hidden_entries,
        "budget_utilization_pct": budget_utilization_pct,
        "top_budget_consumers": top_budget_consumers,
    }


async def _apply_qualify_request_post_commit(
    request: object,
    *,
    task_id: "uuid.UUID",
    project_id: "uuid.UUID",
    path: str,
    session_factory=None,
) -> None:
    """Post-commit: run the best-effort ``qualify_request`` side-effect on a fresh session.

    STRUCTURAL INVARIANT (docs/concurrency.md; ``_process_post_commit`` — "release
    the lock before slow GitHub calls"): the ``qualify_request`` handler does
    minutes of external GitHub I/O — the CI-compliance scan in
    ``ci_compliance.ensure_current_ci_compliance`` (which reads the capped module
    set at the integration-branch HEAD) plus the candidate-SHA resolution in
    ``release_qualifier.run_qualification``. That MUST NOT run while the strategic
    completion holds the Project ``FOR UPDATE`` row lock (acquired up front by
    ``_process_post_commit`` via ``lock_project_then_task``) and its DB connection.

    Running it under the lock idled the completion's connection across the
    multi-minute scan until Cloud SQL closed it mid-flush (``UPDATE project SET
    ci_compliance`` → asyncpg ``InterfaceError: connection is closed``), poisoning
    the completion session so its commit raised ``PendingRollbackError``. The
    completion handler classified that as a transient error and requeued the WHOLE
    agent job — re-running a 5–24 min architecture review up to ``max_retries``
    times before giving up as FAILED, even though the agent's design-doc write had
    already landed each time.

    ``run_qualification`` is self-defending — it takes its own ``ReleaseVersion``
    ``FOR UPDATE`` lock and resolves its GitHub I/O outside that lock — so it
    needs no lock held by this caller. So the qualify side-effect runs here, AFTER
    the completion has durably committed and released its Project lock, on a
    dedicated fresh session. It is best-effort: any failure (connection death,
    GitHub error) is logged and swallowed — the durable completion already
    committed, so a failure here can never requeue the task or discard the
    agent's work.
    """  # noqa: RUF002
    if not request or not isinstance(request, dict):
        return
    from app.database import AsyncSessionLocal

    factory = session_factory or AsyncSessionLocal
    refresh_required = False
    try:
        async with factory() as fresh:
            project = await fresh.get(Project, project_id)
            task = await fresh.get(Task, task_id)
            if project is None or task is None:
                return
            refresh_required = await _apply_qualify_request(
                request, task, project, fresh, path=path
            )
            await fresh.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[STRATEGIC_COMPLETION] _apply_qualify_request_post_commit: qualify_request "
            "side-effect failed for task %s project %s (%s path) — non-fatal; the "
            "strategic completion already committed durably, task NOT requeued",
            task_id,
            project_id,
            path,
            exc_info=True,
        )
        return

    if refresh_required:
        # Keep refresh arming isolated from blocker persistence. The trigger's
        # bounded lock-retry path may roll its transaction back on contention or
        # DB failure; running it in a fresh session means that can never erase
        # the already-committed ReleaseVersion evidence above.
        try:
            from app.strategic_trigger import request_strategic_refresh

            async with factory() as refresh_session:
                armed = await request_strategic_refresh(
                    refresh_session,
                    project_id,
                    "qualification blocked by actionable CI compliance evidence",
                    skip_locked=False,
                )
                if armed:
                    await refresh_session.commit()
                else:
                    await refresh_session.rollback()
            if not armed:
                logger.warning(
                    "[STRATEGIC_COMPLETION] CI-compliance blocker committed but "
                    "Strategic refresh could not be armed for project %s; the "
                    "cadence backstop remains available",
                    project_id,
                )
        except Exception:  # noqa: BLE001
            logger.warning(
                "[STRATEGIC_COMPLETION] CI-compliance blocker committed but "
                "isolated Strategic refresh failed for project %s",
                project_id,
                exc_info=True,
            )


async def _maybe_retry_workday_auto_start(
    project_id: "uuid.UUID",
    *,
    session_factory=None,
) -> None:
    """Post-commit: retry Workday auto-start when a blocking strategic cycle finishes.

    When the project is in Workday (continuous) mode, has no live resident session,
    and its last recorded blocked reason is ``cycle_in_flight`` — meaning a batch
    strategic cycle was in flight when the operator entered Workday — re-run the
    Workday auto-start now that the blocking cycle has completed. Runs on a
    dedicated fresh session; any failure is logged and swallowed so strategic
    completion is never broken.
    """
    import app.strategic_resident_session as _srs
    from app.database import AsyncSessionLocal
    from app.workbench_state import get_strategic_workbench_state

    factory = session_factory or AsyncSessionLocal
    # Read current project state on a fresh session.
    try:
        async with factory() as fresh:
            project = await fresh.get(Project, project_id)
            if project is None:
                return
            # Only retry when no live resident session is running.
            if _srs.resident_session_is_active(
                _srs.read_resident_session(project.vision)
            ):
                return
            # Only retry when the blocked reason is specifically cycle_in_flight.
            raw_reason = (project.vision or {}).get(
                _srs.RESIDENT_SESSION_REASON_KEY
            ) or {}
            if raw_reason.get("reason") != _srs.RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT:
                return
            op_user_id = raw_reason.get("operator_user_id") or "system"
            # Only retry when Workday mode is still active — read from the
            # per-user prefs store (MessagesNoisePrefs) where workbench state
            # is actually persisted, NOT from project.vision.
            workbench = await get_strategic_workbench_state(fresh, op_user_id, project_id)
            if workbench.get("interaction_mode") != "continuous":
                return
    except Exception:
        logger.warning(
            "[STRATEGIC_COMPLETION] _maybe_retry_workday_auto_start: state read failed "
            "for project %s",
            project_id,
            exc_info=True,
        )
        return
    # Conditions met — re-run the auto-start on a separate fresh session.
    try:
        await _srs._auto_start_on_workday_entry(
            project_id,
            operator_user_id=op_user_id,
            session_factory=factory,
        )
        logger.info(
            "[STRATEGIC_COMPLETION] Workday auto-start retry triggered for project %s "
            "(cycle_in_flight unblocked by strategic completion)",
            project_id,
        )
    except Exception:
        logger.warning(
            "[STRATEGIC_COMPLETION] _maybe_retry_workday_auto_start: auto-start retry "
            "failed for project %s",
            project_id,
            exc_info=True,
        )


def _merge_rejected_proposals(vision: dict, batch: list) -> None:
    """Append batch entries to vision['rejected_proposals'], deduplicating by text.

    Safe to call multiple times with the same batch (replay-idempotent): an entry
    whose 'text' already exists in the list is silently skipped.  The list is
    capped at 30 entries after merging.
    """
    _rp = list(vision.get("rejected_proposals") or [])
    existing_texts = {e.get("text") for e in _rp}
    for entry in batch:
        if entry.get("text") not in existing_texts:
            _rp.append(entry)
            existing_texts.add(entry.get("text"))
    vision["rejected_proposals"] = _rp[-30:]


def _record_rejected_direction_op(
    task: Task,
    *,
    op: str,
    direction_id: str | None,
    text: str,
    invalid_criterion_keys: list[str],
    active_criterion_keys=None,
) -> None:
    """Record a create/update_direction op dropped for invalid criterion_keys.

    The fallback parse path previously dropped these ops with only a log line,
    so the strategist never saw the rejection and the proposed criterion link
    was lost silently. This appends a bounded entry to
    ``task.context[REJECTED_DIRECTION_OPS_CONTEXT_KEY]`` — newest last, capped
    at ``_REJECTED_DIRECTION_OPS_MAX`` — which then flows into the completion
    summary, the cycle's ``cycle_metadata``, and the next cycle's payload so the
    strategist can remap to an active criterion.

    ``active_criterion_keys`` carries the current valid keys for the open
    version so the record is actionable, not just a refusal.
    """
    ctx = dict(task.context or {})
    rejected = list(ctx.get(REJECTED_DIRECTION_OPS_CONTEXT_KEY) or [])
    rejected.append(
        {
            "op": str(op)[:32],
            "direction_id": str(direction_id)[:64] if direction_id else None,
            "text": str(text or "")[: _REJECTED_OP_TEXT_MAX],
            "reason": "invalid_criterion_keys",
            "invalid_criterion_keys": [
                str(k)[:80] for k in (invalid_criterion_keys or [])
            ][:_REJECTED_OP_KEYS_MAX],
            "active_criterion_keys": sorted(
                str(k) for k in (active_criterion_keys or [])
            )[:_REJECTED_OP_KEYS_MAX],
            "rejected_at": _utcnow().isoformat(),
        }
    )
    ctx[REJECTED_DIRECTION_OPS_CONTEXT_KEY] = rejected[-_REJECTED_DIRECTION_OPS_MAX:]
    task.context = ctx
    flag_modified(task, "context")


def strategic_completion_message_count(logs: str) -> int:
    """Return a bounded reservation covering every fallback message emission."""

    try:
        parsed = json.loads(logs[:500_000]) if logs else None
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(parsed, dict):
        return 0
    entries = parsed.get("messages")
    direct_count = sum(
        1
        for entry in (entries[:5] if isinstance(entries, list) else [])
        if isinstance(entry, dict) and str(entry.get("content") or "").strip()
    )
    resolved_entries = parsed.get("resolved_messages")
    if not isinstance(resolved_entries, list) or not resolved_entries:
        resolved_entries = parsed.get("resolved_message_ids")
    reply_capacity = (
        min(len(resolved_entries), 10)
        if isinstance(resolved_entries, list)
        else 0
    )
    return direct_count + reply_capacity


async def _handle_strategic_completion(
    task: Task,
    logs: str,
    session: AsyncSession,
    *,
    project: Project | None = None,
    message_sequences: list[int] | None = None,
    preserve_terminal_cycle_disposition: bool = False,
):
    """Handle strategic review completion.

    Vision and direction updates were already applied incrementally via POST /api/agent/strategic
    during agent execution. This handler just records final status and checks for issues.
    Falls back to log parsing for backward compatibility if the agent used the old approach.

    ``preserve_terminal_cycle_disposition`` is used only by the resident-stop
    bridge after it reconciles a cancelled/failed linked task.  API side effects
    (version decisions, release requests, and retirement memos) still need
    processing, but that already-emitted terminal receipt is authoritative:
    this handler must not overwrite the failed cycle or broadcast a success.

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
    # The legacy vision body is no longer shipped in the strategic payload, so
    # the spawn-time vision baseline can no longer be read from
    # task.first_payload["vision"]. Resolve it from the cycle's durable
    # baseline_vision snapshot instead (equivalent: the payload used to carry
    # _agent_visible_vision(project.vision), whose revision equals the snapshot
    # taken at spawn). Legacy tasks that still carry the payload key keep the
    # old read; tasks with no cycle row fall back to revision 0.
    spawned_vision_rev = (task_payload.get("vision") or {}).get("revision", 0)
    _baseline_cycle_id = task.strategic_cycle_id or (task.context or {}).get(
        "strategic_cycle_id"
    )
    if _baseline_cycle_id:
        _baseline_cycle = await strategic_cycle.get(session, _baseline_cycle_id)
        if _baseline_cycle is not None:
            _baseline_vision_snap = strategic_cycle.baseline_vision(_baseline_cycle)
            if _baseline_vision_snap:
                spawned_vision_rev = _baseline_vision_snap.get("revision", 0)
    tasks_spawned = (task.context or {}).get("_strategic_tasks_spawned", 0)
    adoption_declared = int((task.context or {}).get("_strategic_adoption_declared", 0))
    api_was_used = (
        (task.context or {}).get("_strategic_vision_set", False)
        or vision_rev > spawned_vision_rev
        or (task.context or {}).get("_strategic_dirs_created", 0) > 0
        or (task.context or {}).get("_strategic_dirs_retired", 0) > 0
        or (task.context or {}).get("_strategic_dirs_staged", 0) > 0
        or (task.context or {}).get("_strategic_dirs_updated", 0) > 0
        or (task.context or {}).get("_strategic_proposals_approved", 0) > 0
        or (task.context or {}).get("_strategic_proposals_rejected", 0) > 0
        or tasks_spawned > 0
        or (task.context or {}).get("_strategic_api_called", False)
    )

    resident_infrastructure = bool(
        (task.context or {}).get("_resident_session") is True
        or (task.context or {}).get("_resident_infrastructure") is True
    )
    if resident_infrastructure and not api_was_used:
        # A Continuous Workday generation is process infrastructure, not a
        # one-shot strategic result. Its normal lifecycle terminal carries no
        # JSON output to parse. Keep the raw Task as audit provenance, but never
        # turn that expected absence into a false effectiveness failure/alert.
        # An adopted cadence task can still own an active StrategicCycle. Close
        # that cycle with a cancelled infrastructure disposition; otherwise a
        # normally-returned Resident generation (Task COMPLETED) would leave the
        # cycle stuck in phase2_running because the generic lifecycle backstop
        # only reconciles FAILED/CANCELLED tasks.
        reconciled_cycle = await strategic_cycle.reconcile_terminal_task(
            session,
            task,
            source="resident_generation_terminal",
            allow_completed_resident_infrastructure=True,
        )
        completion_event = (
            strategic_cycle.terminal_completion_event(
                reconciled_cycle,
                task,
                source="resident_generation_terminal",
            )
            if reconciled_cycle is not None
            else None
        )
        context = {
            **(task.context or {}),
            "_resident_infrastructure": True,
        }
        context.setdefault(
            "_resident_lifecycle_disposition", "resident_generation_terminal"
        )
        task.context = context
        if not task.latest_status_message or task.latest_status_message == (
            "Strategic output could not be parsed"
        ):
            task.latest_status_message = "Resident generation ended (infrastructure)"
        session.add(task)
        await session.commit()
        if completion_event is not None:
            await strategic_cycle.emit_terminal_completion_events([completion_event])
        logger.info(
            "[STRATEGIC] Resident infrastructure task %s ended without result parsing",
            task.id,
        )
        return

    if api_was_used:
        cycle_id = (task.context or {}).get("strategic_cycle_id")
        dirs_created = (task.context or {}).get("_strategic_dirs_created", 0)
        dirs_retired = (task.context or {}).get("_strategic_dirs_retired", 0)
        adoption_declared = int(adoption_declared or 0)
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
            _adoption_suffix = (
                f", {adoption_declared} capability adoption decision(s) declared"
                if adoption_declared
                else ""
            )
            _spawn_suffix = f", {tasks_spawned} tasks spawned" if tasks_spawned else ""
            _summary = (
                f"rev {vision_rev}, {active_dirs} active, {dirs_created} created, "
                f"{dirs_retired} retired{_adoption_suffix}{_spawn_suffix}"
            )
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
            # Direction ops dropped for invalid criterion_keys — copied into
            # cycle_metadata so the next cycle's payload producer can re-surface
            # them to the strategist (durable across the transaction boundary).
            _api_rejected_ops = (task.context or {}).get(
                REJECTED_DIRECTION_OPS_CONTEXT_KEY
            )
            if _api_rejected_ops:
                _extra["rejected_direction_ops"] = _api_rejected_ops[
                    -_REJECTED_DIRECTION_OPS_MAX:
                ]
            try:
                _extra["model_gc_telemetry"] = _compute_model_gc_telemetry(
                    project, task
                )
                # A high/critical-tier section dropping wholesale means the
                # strategist ran on a degraded payload — raise a loud, dedup'd
                # operator alert. Medium/low drops are expected degradation and
                # do NOT alert.
                _high_tier_dropped = _extra["model_gc_telemetry"].get(
                    "high_tier_dropped"
                ) or []
                if _high_tier_dropped:
                    logger.error(
                        "[STRATEGIC] Task %s: strategist ran on degraded payload — "
                        "high-tier sections dropped under budget: %s",
                        task.id,
                        _high_tier_dropped,
                    )
                    try:
                        await _create_strategic_alert(
                            project.id,
                            session,
                            "Strategist ran on a degraded payload: high-tier "
                            "section(s) dropped under the byte budget — "
                            f"{', '.join(_high_tier_dropped)}. Context was "
                            "truncated; verify the cycle's decisions.",
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            f"[STRATEGIC] Task {task.id}: failed to raise high-tier-drop alert (non-fatal)",
                            exc_info=True,
                        )
            except Exception:  # noqa: BLE001
                logger.warning(
                    f"[STRATEGIC] Task {task.id}: failed to compute model_gc_telemetry (non-fatal)",
                    exc_info=True,
                )
            if _retirement_critiques is not None:
                _extra["retirement_critiques"] = _retirement_critiques
            # Copy the bounded, versioned strategic judgment challenge record
            # (evidence_progress / retirement / portfolio-shape second opinion)
            # verbatim into cycle_metadata so the challenge pass is durably
            # attached to the cycle. Stored under STRATEGIC_CHALLENGE_CONTEXT_KEY
            # by the set_strategic_challenge endpoint; a no-op when absent.
            _challenge_record = (task.context or {}).get(
                STRATEGIC_CHALLENGE_CONTEXT_KEY
            )
            if _challenge_record is not None:
                _extra["strategic_judgment_challenge"] = _challenge_record
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
            # Knowledge document consumption tracking — mirrors the memo pattern.
            # knowledge_available = count of docs in the index fed to the agent;
            # knowledge_cited = count from the cite_knowledge API op stored on
            # task.context["_strategic_cited_knowledge_ids"].
            _knowledge_available = 0
            _knowledge_cited = 0
            try:
                from app.knowledge_query import build_knowledge_index

                _knowledge_index = await build_knowledge_index(session, project.id)
                _knowledge_available = len(_knowledge_index)
                _ctx_cited_knowledge_ids = (task.context or {}).get(
                    "_strategic_cited_knowledge_ids"
                )
                if _ctx_cited_knowledge_ids is not None:
                    _available_knowledge_ids = {
                        str(e.get("id")) for e in _knowledge_index if e.get("id")
                    }
                    _knowledge_cited = len(
                        set(_ctx_cited_knowledge_ids) & _available_knowledge_ids
                    )
            except Exception:  # noqa: BLE001
                logger.warning(
                    f"[STRATEGIC] Task {task.id}: failed to compute knowledge counts (non-fatal)",
                    exc_info=True,
                )
            if preserve_terminal_cycle_disposition:
                completed_cycle = None
            else:
                completed_cycle = await strategic_cycle.mark_phase2_completed(
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
                    knowledge_available=_knowledge_available,
                    knowledge_cited=_knowledge_cited,
                    extra_metadata=_extra,
                )
        else:
            completed_cycle = None
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
        await _check_direction_starvation(project, dirs_created, session, task=task)
        # Reset the consecutive-failure breaker on a successful cycle. The
        # counter is otherwise only cleared on a vision *write* (the
        # set-problem-model API path at admin_agent_ops and the legacy
        # log-parse merge below), but a healthy cycle now commonly completes by
        # updating directions / problem_model / running_update WITHOUT rewriting
        # the vision. Without this reset, _strategic_failures only ever ratchets
        # UP (failures increment it) and can be cleared solely by the 48h
        # time-decay — so 3 failures in any 48h window leave the breaker falsely
        # tripped, gating forced/backstop strategic refresh for up to two days
        # even while every subsequent cycle succeeds. "Consecutive" failures
        # means: reset to zero on any success.
        # Mutate the vision dict in place (then flag_modified) rather than
        # reassigning ``project.vision``: this write runs under the project lock
        # the caller pre-acquired (lock_project_then_task), but the vision-lock
        # ratchet (test_invariants.TestVisionLockInvariant) is decrease-only, so
        # a fresh ``project.vision = …`` assignment would trip it. A ``.pop()``
        # is a call, not an assignment node, so it adds no counted write.
        _cur_vision = project.vision
        if not preserve_terminal_cycle_disposition:
            if _cur_vision and (
                "_strategic_failures" in _cur_vision
                or "_strategic_failures_at" in _cur_vision
            ):
                _cur_vision.pop("_strategic_failures", None)
                _cur_vision.pop("_strategic_failures_at", None)
                flag_modified(project, "vision")
                logger.info(
                    f"[STRATEGIC] Task {task.id}: reset _strategic_failures breaker "
                    f"on successful cycle (project {project.id})"
                )
        session.add(project)
        _adoption_suffix_msg = (
            f", {adoption_declared} capability adoption decision(s) declared"
            if adoption_declared
            else ""
        )
        _spawn_suffix_msg = f", {tasks_spawned} tasks spawned" if tasks_spawned else ""
        if not preserve_terminal_cycle_disposition:
            task.latest_status_message = (
                f"Strategic review complete — vision rev {vision_rev}, {active_dirs} "
                f"active directions, {dirs_created} created, {dirs_retired} retired"
                f"{_adoption_suffix_msg}{_spawn_suffix_msg}"
            )
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
        # Audit: check for unanswered QUESTION messages
        remaining_questions_result = await session.execute(
            select(func.count())
            .select_from(ProjectMessage)
            .where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.status == "open",
                ProjectMessage.message_type == MessageType.QUESTION,
            )
        )
        open_question_count = remaining_questions_result.scalar() or 0
        if open_question_count > 0:
            logger.warning(
                f"[STRATEGIC] Task {task.id}: {open_question_count} QUESTION(s) left unanswered after strategic review"
            )
            await _create_strategic_alert(
                task.project_id,
                session,
                f"{open_question_count} QUESTION(s) left unanswered after strategic review — agent may be ignoring operator questions",
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
        # qualify_request is a best-effort side-effect that does minutes of GitHub
        # I/O (CI-compliance scan + run_qualification). Per the "release the lock
        # before slow GitHub calls" invariant it must NOT run under the Project
        # FOR UPDATE lock _process_post_commit holds — doing so idled this
        # session's connection across the scan until Cloud SQL closed it,
        # poisoning the commit below (PendingRollbackError) and requeuing the
        # whole agent job. Captured here; applied post-commit on a fresh session.
        _qr_ctx = (task.context or {}).get(QUALIFY_REQUEST_CONTEXT_KEY)
        _qr_task_id = task.id
        _qr_project_id = project.id
        # Apply new_version_goal if the agent supplied one via the API. Same
        # transport as qualify_request: the ``set_problem_model`` endpoint lifts
        # the field out of the problem_model JSON (ProblemModel extra="forbid")
        # and stows the sanitized string under ``NEW_VERSION_GOAL_CONTEXT_KEY``.
        # Sets the open version's goal with source="strategist" when null.
        _nvg_ctx = (task.context or {}).get(NEW_VERSION_GOAL_CONTEXT_KEY)
        await _apply_new_version_goal(_nvg_ctx, task, project, session, path="api")
        # Apply new_acceptance_criteria if the agent supplied them via the API.
        # Same transport: the ``set_problem_model`` endpoint lifts the field out of
        # the problem_model JSON and stows the list under
        # ``NEW_ACCEPTANCE_CRITERIA_CONTEXT_KEY``. Creates one criterion per text,
        # skipping duplicates.
        _nac_ctx = (task.context or {}).get(NEW_ACCEPTANCE_CRITERIA_CONTEXT_KEY)
        await _apply_new_acceptance_criteria(_nac_ctx, task, project, session, path="api")

        # Per-cycle message post removed: the strategic agent now maintains
        # project.running_update as a living markdown document it rewrites
        # each cycle. Replaces the previous one-message-per-cycle noise.
        try:
            await _spawn_retirement_memos(
                session, project, task, retired_ids, retired_rationales
            )
        except Exception:
            logger.error(
                "[STRATEGIC] Task %s: _spawn_retirement_memos failed; retirement memos not created "
                "for direction IDs %s — commit will still proceed",
                task.id,
                retired_ids,
                exc_info=True,
            )
        await session.commit()
        # qualify_request runs AFTER the lock-release commit, on its own fresh
        # session, so its slow GitHub I/O never holds this completion's Project
        # lock / connection and a failure here can never requeue the durably-
        # completed task (see _apply_qualify_request_post_commit).
        await _apply_qualify_request_post_commit(
            _qr_ctx, task_id=_qr_task_id, project_id=_qr_project_id, path="api"
        )
        if not preserve_terminal_cycle_disposition:
            # Post-commit: the live strategic ops already emitted per-write
            # `strategic_updated` events during execution; here we close the cycle.
            from app.strategic_events import emit_strategic_cycle_completed

            await emit_strategic_cycle_completed(
                task.project_id,
                task.id,
                2,
                cycle_id=cycle_id,
                completion=_strategic_event_completion_summary(
                    task,
                    project,
                    cycle=completed_cycle,
                    status="completed",
                ),
            )
        logger.info(
            f"[STRATEGIC] Task {task.id} complete via API: vision rev {vision_rev}, {active_dirs} active directions"
            f"{_adoption_suffix_msg}{_spawn_suffix_msg}"
        )
        if not preserve_terminal_cycle_disposition:
            await _maybe_retry_workday_auto_start(task.project_id)
        return

    # Fallback: try log-based parsing for backward compatibility
    try:
        parsed = json.loads(logs[:500_000]) if logs else None
    except (json.JSONDecodeError, ValueError):
        parsed = None

    if not parsed or not isinstance(parsed, dict):
        # No API calls and no parseable output — record failure under a row lock.
        # skip_locked=False: wait for the lock rather than silently drop a count.
        from app.task_locking import with_vision_locked

        cycle_id = task.strategic_cycle_id or (task.context or {}).get(
            "strategic_cycle_id"
        )
        _now_iso = _utcnow().isoformat()
        _failure_num: list[int] = []

        def _bump_failures(vision: dict) -> None:
            failures = vision.get("_strategic_failures", 0) + 1
            vision["_strategic_failures"] = failures
            vision["_strategic_failures_at"] = _now_iso
            _failure_num.append(failures)

        project_found = await with_vision_locked(
            session, task.project_id, _bump_failures
        )
        if project_found:
            project = await session.get(Project, task.project_id)
            if cycle_id:
                # Audit W2: failure recorded on the StrategicCycle row.
                failed_cycle = await strategic_cycle.mark_failure(
                    session,
                    cycle_id,
                    phase="phase2",
                    reason="strategic_review unparseable output",
                )
            else:
                failed_cycle = None
        else:
            project = None
            failed_cycle = None
        task.latest_status_message = "Strategic output could not be parsed"
        session.add(task)
        await session.commit()
        # Post-commit: the cycle ended (in failure) — close it for the UI.
        from app.strategic_events import emit_strategic_cycle_completed

        await emit_strategic_cycle_completed(
            task.project_id,
            task.id,
            2,
            cycle_id=cycle_id,
            phase_state="failed",
            completion=_strategic_event_completion_summary(
                task,
                project,
                cycle=failed_cycle,
                status="failed",
                failure_reason="strategic_review unparseable output",
            ),
        )
        failure_num = _failure_num[0] if _failure_num else "?"
        logger.warning(
            f"[STRATEGIC] Task {task.id}: unparseable output (failure #{failure_num})"
        )
        if project:
            await _create_strategic_alert(
                task.project_id,
                session,
                f"Strategic review could not parse agent output (failure #{failure_num}, task {str(task.id)[:8]})",
            )
        await _maybe_retry_workday_auto_start(task.project_id)
        return

    new_vision = parsed.get("vision")
    new_problem_model = parsed.get("problem_model")
    new_running_update = parsed.get("running_update")
    new_version_goal = parsed.get("new_version_goal")
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
        # The legacy vision body is no longer shipped in the strategic payload
        # (see the spawned_vision_rev resolution above), so the payload fallback
        # is empty for every modern task. Bind the merge baseline to the
        # cycle's durable baseline_vision snapshot; legacy tasks that still
        # carry the payload key keep the old read; tasks with no cycle row fall
        # back to the CURRENT agent-visible vision so the 3-way merge never runs
        # against an empty baseline (which makes every agent-supplied field read
        # as a fresh edit and logs spurious rejections for legit updates).
        payload_vision_rev = spawned_vision_rev
        db_vision_rev = current_vision.get("revision", 0)
        # Baseline: prefer the snapshot the cycle row captured at spawn time
        # (StrategicCycle.cycle_metadata.baseline_vision). Falls back to the
        # payload vision (legacy task) and then the current agent-visible vision.
        cycle_id = task.strategic_cycle_id or (task.context or {}).get(
            "strategic_cycle_id"
        )
        baseline_vision = task.first_payload.get("vision") or {}
        if not baseline_vision:
            from app.strategic_local_runner import _agent_visible_vision

            baseline_vision = _agent_visible_vision(project.vision)
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
                # Verified basis: only merges that reproduced on HEAD upgrade a
                # system's status, consistent with the completion detector and the
                # outcome-progress latch. Legacy entries/folds fall back to raw.
                total_merged = 0
                for d in mapped_dirs:
                    for h in d.get("research_history", []):
                        total_merged += verified_merged_count(h)
                    agg = d.get("history_aggregate", {})
                    total_merged += agg.get(
                        "total_verified_prs_merged", agg.get("total_prs_merged", 0)
                    )
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

            from app.problem_model import (
                active_direction_gap_ids as _active_direction_gap_ids,
                apply_agent_update,
                normalize_active_moves_writeback as _normalize_active_moves_writeback,
            )
            from app.research_direction_state import RETIRED_DIRECTION_STATUSES

            # Resolve the cycle's baseline problem model (snapshot at spawn) for
            # the 3-way operator-wins merge, mirroring the vision baseline
            # resolution above. Degrade to None (blank-preservation only) when
            # no cycle/baseline is resolvable (legacy task).
            # Prefer the agent's own prior same-cycle proposal as the merge
            # ancestor (mirrors the live set_problem_model op) so a second write
            # is not misread as an operator edit; fall back to the cycle-start
            # snapshot on the first/only write. Degrade to None (blank-
            # preservation only) when no cycle/baseline is resolvable.
            pm_baseline: dict | None = None
            pm_cycle_id = task.strategic_cycle_id or (task.context or {}).get(
                "strategic_cycle_id"
            )
            if pm_cycle_id:
                pm_cycle_row = await strategic_cycle.get(session, pm_cycle_id)
                if pm_cycle_row is not None:
                    prior = strategic_cycle.agent_problem_model_proposal(pm_cycle_row)
                    if prior is not None:
                        pm_baseline = prior
                    else:
                        snap = strategic_cycle.baseline_problem_model(pm_cycle_row)
                        if snap:
                            pm_baseline = snap

            _protected_gap_ids = _active_direction_gap_ids(project.directions)
            _live_dir_ids = frozenset(
                str(d.get("id"))
                for d in (project.directions or [])
                if d.get("status") not in RETIRED_DIRECTION_STATUSES
            )
            # Strip agent-owned collections from the proposed problem model when
            # they are unchanged from the cycle baseline.  The strategic completion
            # path does not independently edit friction, gaps, capabilities, or
            # pending_user_confirmation — the agent may echo stale copies that
            # would overwrite newer concurrent state.  Unchanged collections are
            # safely preserved through the merge's omission=keep logic inside
            # apply_agent_update, which also handles the non-trivial gap history
            # union and per-gap compaction watermarks.
            if pm_baseline is not None:
                for _ckey in ("friction", "gaps", "capabilities", "pending_user_confirmation"):
                    pv = new_problem_model.get(_ckey)
                    bv = pm_baseline.get(_ckey)
                    if pv is not None and bv is not None and pv == bv:
                        new_problem_model.pop(_ckey)
            try:
                new_pm_raw, pm_diff = apply_agent_update(
                    current_raw=project.problem_model,
                    proposed_raw=new_problem_model,
                    now_iso=_utcnow().isoformat(),
                    baseline_raw=pm_baseline,
                    protected_gap_ids=_protected_gap_ids,
                    live_direction_ids=_live_dir_ids,
                )
            except _PmValidationError as pm_err:
                logger.warning(
                    f"[STRATEGIC] Task {task.id}: problem_model validation failed — "
                    f"keeping prior state. Error: {pm_err}"
                )
            else:
                if pm_diff.get("live_core_exceeds_budget"):
                    logger.error(
                        f"[STRATEGIC] Task {task.id}: problem_model live core exceeds "
                        f"byte budget — rejecting write to prevent oversized durable state "
                        f"(rev-1367 wipe precondition avoided); section: "
                        f"{pm_diff['live_core_exceeds_budget']}"
                    )
                    from app.alert_dispatch import (
                        _maybe_create_problem_model_live_core_alert,
                    )
                    await _maybe_create_problem_model_live_core_alert(
                        project.id, session
                    )
                else:
                    project.problem_model = new_pm_raw
                    flag_modified(project, "problem_model")
                    # Surface blocked gap deletions (active direction still references).
                    _blocked_dels = pm_diff.get("gaps_deletion_blocked") or []
                    if _blocked_dels:
                        logger.warning(
                            f"[STRATEGIC] Task {task.id}: gap deletion(s) blocked — "
                            f"still referenced by active direction(s): {_blocked_dels}"
                        )
                    for _wipe_field in ("gaps", "capabilities"):
                        if pm_diff.get(f"{_wipe_field}_wipe_risk"):
                            logger.warning(
                                f"[STRATEGIC] Task {task.id}: problem_model {_wipe_field} "
                                f"wipe-without-deletions detected — agent proposed empty "
                                f"{_wipe_field} with no _deletions directive; existing "
                                f"{_wipe_field} preserved via non-destructive merge"
                            )
                            from app.alert_dispatch import (
                                _maybe_create_problem_model_wipe_alert,
                            )
                            await _maybe_create_problem_model_wipe_alert(
                                project.id, session, field=_wipe_field
                            )
                    # Normalize stale active_moves via shared helper so both write
                    # paths (op-endpoint + log-parse fallback) stay converged.
                    _active_dir_ids = {
                        str(d.get("id"))
                        for d in (project.directions or [])
                        if d.get("status") == "active"
                    }
                    new_pm_raw, _norm = _normalize_active_moves_writeback(
                        new_pm_raw, _active_dir_ids, _utcnow().isoformat()
                    )
                    if _norm:
                        logger.info(
                            f"[STRATEGIC] Task {task.id}: auto-normalized active_moves: {_norm}"
                        )
                        project.problem_model = new_pm_raw
                        flag_modified(project, "problem_model")
                    logger.info(
                        f"[STRATEGIC] Updated problem_model for project {project.id} "
                        f"to revision {new_pm_raw.get('revision')} ({pm_diff or 'no diff'})"
                    )
                    # Carry proof records and adoption verdicts onto survivor keys
                    # when a capability consolidation occurred in this write.
                    _consolidated = (
                        pm_diff.get("capabilities_consolidated") if pm_diff else None
                    )
                    if _consolidated:
                        from app.capability_proof import (
                            carry_proof_records_for_consolidation,
                        )
                        from app.capability_trial_verdict import (
                            carry_adoption_verdicts_for_consolidation,
                        )

                        if not isinstance(
                            getattr(project, "capability_proof_records", None), dict
                        ):
                            project.capability_proof_records = {}
                        project.capability_proof_records = (
                            carry_proof_records_for_consolidation(
                                _consolidated, project.capability_proof_records
                            )
                        )
                        flag_modified(project, "capability_proof_records")
                        if not isinstance(
                            getattr(project, "capability_adoption_verdicts", None), dict
                        ):
                            project.capability_adoption_verdicts = {}
                        project.capability_adoption_verdicts = (
                            carry_adoption_verdicts_for_consolidation(
                                _consolidated, project.capability_adoption_verdicts
                            )
                        )
                        flag_modified(project, "capability_adoption_verdicts")
        except Exception:
            logger.exception(
                "[STRATEGIC] Failed to apply problem_model update (non-fatal)"
            )

    # Apply new_version_goal when the strategist explicitly authors the version
    # objective (new_version_goal field in agent output).  Delegates to the
    # shared helper used by the API path so both paths behave identically.
    await _apply_new_version_goal(
        new_version_goal, task, project, session, path="fallback"
    )

    # Apply new_acceptance_criteria when the strategist authors version-scoped
    # criteria in the fallback output. Mirrors the API path above so fallback
    # completions cannot silently drop the canonical acceptance contract.
    await _apply_new_acceptance_criteria(
        parsed.get("new_acceptance_criteria"), task, project, session, path="fallback"
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
            ru_cycle_row = None
            ru_cycle_id = task.strategic_cycle_id or (task.context or {}).get(
                "strategic_cycle_id"
            )
            if ru_cycle_id:
                ru_cycle_row = await strategic_cycle.get(session, ru_cycle_id)
                if ru_cycle_row is not None:
                    ru_baseline = strategic_cycle.baseline_running_update(ru_cycle_row)
            ru_rejected = apply_reconciled_running_update(
                project,
                baseline=ru_baseline,
                agent=new_running_update,
                cycle=ru_cycle_row,
            )
            if ru_cycle_row is not None:
                # Persist the advanced baseline so a later same-cycle write
                # reconciles against this document, not the cycle-start snapshot.
                session.add(ru_cycle_row)
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
        # No vision produced — increment failure counter to prevent infinite strategic loops.
        # `project` is already locked by lock_project_for_strategic_transition (or held by
        # the caller via caller_holds_lock), so the RMW below is safe without re-acquiring
        # with_vision_locked — a double-acquire would deadlock.
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
    created_retired_grounding_fb: dict[str, dict] = {}
    _pending_retire_memory: list = []
    # Rejected-proposal tombstones to persist into vision["rejected_proposals"]
    # after the op loop, via with_vision_locked() — same deferred-batch +
    # canonical-helper pattern as _pending_retire_memory (keeps the direct
    # project.vision assignment out of this file's ratchet count).
    _pending_rejected_proposals: list = []
    retired_rationales_fb: Dict[str, str] = {}
    # Gap-staffing reconciliations to apply to project.problem_model after the
    # op loop. Each entry is
    # ``(direction_id, new_gap_id, prior_gap_id, new_status, prior_status)``.
    # Applied once after the loop, in the same locked transaction as the
    # directions write.
    _pending_gap_reconciles: list = []
    # Delta tracking for the post-loop directions write.  Each op mutates the
    # in-memory ``directions`` snapshot; capturing those mutations here lets the
    # locked write apply only what changed against the FRESH DB list, so a
    # direction added concurrently between snapshot and write is preserved rather
    # than silently overwritten.
    _delta_mutated: dict = {}   # str(id) -> final dict for any modified existing dir
    _delta_removed: set = set() # str(ids) removed with no tombstone (reject, no text)
    _delta_created: list = []   # new direction dicts appended this batch
    if direction_ops and isinstance(direction_ops, list):
        if len(direction_ops) > MAX_DIRECTION_OPS:
            logger.warning(
                f"[STRATEGIC] Task {task.id}: truncating {len(direction_ops)} direction ops to {MAX_DIRECTION_OPS}"
            )
            direction_ops = direction_ops[:MAX_DIRECTION_OPS]
        directions = list(project.directions or [])
        fallback_challenge_record = (task.context or {}).get(
            STRATEGIC_CHALLENGE_CONTEXT_KEY
        )
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
            op = _canonical_portfolio_op(op_entry.get("op"))
            if op is None:
                continue
            rationale = str(op_entry.get("rationale", ""))
            if len(rationale) > STRATEGIC_RATIONALE_MAX:
                logger.warning(
                    f"[STRATEGIC] Task {task.id}: truncating rationale {len(rationale)}→{STRATEGIC_RATIONALE_MAX} chars"
                )
                rationale = rationale[:STRATEGIC_RATIONALE_MAX]

            if op == "create_direction":
                if creates_count >= MAX_DIRECTION_CREATES:
                    continue
                text = op_entry.get("text", "").strip()
                if not text:
                    continue
                blocked, why = challenge_rejects_portfolio_op(
                    fallback_challenge_record,
                    op,
                    text=text,
                )
                if blocked:
                    logger.info(
                        "[STRATEGIC] Task %s: fallback portfolio challenge blocked create_direction: %s",
                        task.id,
                        why or "challenge rejected this create_direction",
                    )
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
                # Gap-scoped recently-retired block: hard-stops same-gap re-spawns
                # even after the original direction has been pruned from the live list.
                _gap_block_mode = str(
                    _runtime_config.get("direction_gap_respawn_block_mode", "observe")
                ).strip().lower()
                if _gap_block_mode not in {"observe", "enforce"}:
                    _gap_block_mode = "observe"
                _gap_respawn_cooldown_days = float(
                    _runtime_config.get("direction_gap_respawn_cooldown_days", 14) or 14
                )
                _retired_memory = list(
                    (project.vision or {}).get("_recently_retired_directions") or []
                )
                _retired_grounding: dict | None = None
                _gap_block_reason = None
                _gap_match = find_gap_respawn_match(
                    text,
                    op_entry.get("gap_id"),
                    _retired_memory,
                    _gap_respawn_cooldown_days,
                )
                if _gap_match is not None:
                    _matched_retired_entry, _gap_match_kind = _gap_match
                    _retirement_memo = None
                    _retired_direction_id = str(
                        _matched_retired_entry.get("id") or ""
                    )
                    if _retired_direction_id:
                        try:
                            from app.knowledge_query import fetch_retirement_memo

                            _retirement_memo = await fetch_retirement_memo(
                                session,
                                project.id,
                                _retired_direction_id,
                            )
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "[STRATEGIC] Task %s: failed to fetch retirement memo for %s",
                                task.id,
                                _retired_direction_id,
                                exc_info=True,
                            )
                    _retired_grounding = build_retired_direction_grounding(
                        _matched_retired_entry,
                        _retirement_memo,
                    )
                    _gap_block_reason = _format_retired_grounding_reason(
                        _matched_retired_entry,
                        match_kind=_gap_match_kind,
                        new_gap_id=op_entry.get("gap_id"),
                        grounding=_retired_grounding,
                    )
                if _gap_block_reason:
                    logger.warning(
                        "[STRATEGIC] Task %s: create_direction '%s' blocked by recently-retired "
                        "memory (%s) [mode=%s]",
                        task.id,
                        text[:60],
                        _gap_block_reason,
                        _gap_block_mode,
                    )
                    # In observe mode, record the would-have-blocked event durably
                    # so the mode-knob graduation harness can measure lift over a
                    # multi-day window (direction_gap_respawn_block_mode).
                    if _gap_block_mode == "observe":
                        await _record_gap_respawn_observe_event(
                            session,
                            ts=_utcnow(),
                            direction_text=text,
                            gap_id=op_entry.get("gap_id"),
                            matched_retired_id=_retired_direction_id,
                            match_kind=_gap_match_kind,
                            project_id=project.id,
                        )
                    if _gap_block_mode == "enforce":
                        continue
                try:
                    success_evidence = normalize_direction_contract_field(
                        op_entry.get("success_evidence", ""), "success_evidence"
                    )
                    retirement_condition = normalize_direction_contract_field(
                        op_entry.get("retirement_condition", ""),
                        "retirement_condition",
                    )
                    implementation_constraints = (
                        normalize_direction_contract_field(
                            op_entry.get("implementation_constraints", ""),
                            "implementation_constraints",
                        )
                    )
                except ValueError as exc:
                    logger.warning(
                        "[STRATEGIC] Task %s: direction create '%s' rejected — %s",
                        task.id,
                        text[:60],
                        exc,
                    )
                    continue
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
                raw_staged = op_entry.get("staged", False)
                if not isinstance(raw_staged, bool):
                    logger.warning(
                        "[STRATEGIC] Task %s: direction create '%s' rejected — "
                        "staged must be a boolean",
                        task.id,
                        text[:60],
                    )
                    continue
                staged_create = raw_staged
                staged_for_version: int | None = None
                if staged_create:
                    from app.release_version import current_open_version

                    open_version = await current_open_version(session, project.id)
                    if open_version is None:
                        logger.warning(
                            "[STRATEGIC] Task %s: fallback staged create '%s' skipped "
                            "because no open version exists",
                            task.id,
                            text[:60],
                        )
                        continue
                    staged_for_version = open_version.version_number

                criterion_keys_supplied = "criterion_keys" in op_entry
                criterion_keys: list[str] = []
                criterion_release_version_id = None
                if criterion_keys_supplied:
                    raw_criterion_keys = op_entry.get("criterion_keys")
                    if not isinstance(raw_criterion_keys, list) or not all(
                        isinstance(key, str) and key.strip()
                        for key in raw_criterion_keys
                    ):
                        logger.warning(
                            "[STRATEGIC] Task %s: direction create '%s' rejected — "
                            "criterion_keys must be a list of non-empty strings",
                            task.id,
                            text[:60],
                        )
                        continue
                    criterion_keys = list(
                        dict.fromkeys(key.strip() for key in raw_criterion_keys)
                    )
                    if criterion_keys:
                        from app.direction_referential_guard import (
                            check_criterion_key_write,
                            load_version_criterion_key_sets,
                        )
                        from app.release_version import current_open_version

                        criterion_version = (
                            open_version
                            if staged_create
                            else await current_open_version(session, project.id)
                        )
                        if criterion_version is None:
                            logger.warning(
                                "[STRATEGIC] Task %s: direction create '%s' rejected — "
                                "criterion_keys require an open version",
                                task.id,
                                text[:60],
                            )
                            continue
                        active_keys, inactive_keys = (
                            await load_version_criterion_key_sets(
                                session, project.id, criterion_version.id
                            )
                        )
                        invalid_criterion_keys = [
                            key
                            for key in criterion_keys
                            if check_criterion_key_write(
                                key, active_keys, inactive_keys
                            )[0]
                            is not None
                        ]
                        if invalid_criterion_keys:
                            logger.warning(
                                "[STRATEGIC] Task %s: direction create '%s' rejected — "
                                "invalid criterion_keys: %s",
                                task.id,
                                text[:60],
                                invalid_criterion_keys,
                            )
                            _record_rejected_direction_op(
                                task,
                                op=op,
                                direction_id=None,
                                text=text,
                                invalid_criterion_keys=invalid_criterion_keys,
                                active_criterion_keys=active_keys,
                            )
                            continue
                        criterion_release_version_id = criterion_version.id

                raw_conflicts_with = op_entry.get("conflicts_with")
                conflicts_with: list[str] | None = None
                if raw_conflicts_with is not None:
                    if not isinstance(raw_conflicts_with, list) or not all(
                        isinstance(direction_id, str)
                        for direction_id in raw_conflicts_with
                    ):
                        logger.warning(
                            "[STRATEGIC] Task %s: direction create '%s' rejected — "
                            "conflicts_with must be a list of direction IDs",
                            task.id,
                            text[:60],
                        )
                        continue
                    existing_direction_ids = {
                        str(direction.get("id"))
                        for direction in directions
                        if direction.get("id")
                    }
                    invalid_conflicts = [
                        direction_id
                        for direction_id in raw_conflicts_with
                        if direction_id not in existing_direction_ids
                    ]
                    if invalid_conflicts:
                        logger.warning(
                            "[STRATEGIC] Task %s: direction create '%s' rejected — "
                            "unknown conflicts_with IDs: %s",
                            task.id,
                            text[:60],
                            invalid_conflicts,
                        )
                        continue
                    conflicts_with = list(raw_conflicts_with)
                # Deterministic ID: same task + same text always produces the same
                # direction ID, so a post-commit replay that re-parses direction_ops
                # from agent stdout will find the already-committed direction and skip
                # rather than appending a duplicate with a fresh random UUID.
                _dir_id = f"d-{hashlib.sha256(f'{task.id}:{text}'.encode()).hexdigest()[:8]}"
                if any(d.get("id") == _dir_id for d in directions):
                    logger.info(
                        "[STRATEGIC] Task %s: skipping create_direction replay — id %s already exists",
                        task.id,
                        _dir_id,
                    )
                    continue
                new_dir = {
                    "id": _dir_id,
                    "text": text,
                    "status": "staged" if staged_create else "active",
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
                    )
                    + (
                        [
                            {
                                "text": _retired_grounding_note(
                                    _retired_grounding,
                                    _gap_block_reason or "",
                                ),
                                "added_at": _utcnow().isoformat(),
                            }
                        ]
                        if _retired_grounding
                        else []
                    ),
                    "research_history": [],
                    "context": {
                        "success_evidence": success_evidence,
                        "retirement_condition": retirement_condition,
                    },
                }
                if criterion_keys_supplied:
                    new_dir["criterion_keys"] = criterion_keys
                    if criterion_release_version_id is not None:
                        new_dir["criterion_release_version_id"] = str(
                            criterion_release_version_id
                        )
                if conflicts_with is not None:
                    new_dir["conflicts_with"] = conflicts_with
                if staged_create:
                    new_dir["staged_for_version"] = staged_for_version
                    new_dir["staged_at"] = _utcnow().isoformat()
                if _retired_grounding:
                    new_dir["context"]["retired_direction_grounding"] = (
                        _retired_grounding
                    )
                if implementation_constraints:
                    new_dir["context"]["implementation_constraints"] = (
                        implementation_constraints
                    )
                if isinstance(raw_dedup_reasoning, dict):
                    dr: dict = {}
                    closest = raw_dedup_reasoning.get("closest_directions")
                    if isinstance(closest, list):
                        dr["closest_directions"] = closest[:3]
                    distinction = str(
                        raw_dedup_reasoning.get("distinction") or ""
                    ).strip()
                    if distinction:
                        dr["distinction"] = distinction[
                            :DIRECTION_CONTRACT_FIELD_MAX
                        ]
                    if dr:
                        new_dir["context"]["dedup_reasoning"] = dr
                        new_dir["context"]["grounding_verdict"] = validate_create_direction_grounding(
                            dr,
                            directions,
                            _retired_memory,
                        )
                if op_entry.get("source"):
                    new_dir["source"] = str(op_entry["source"])[:64]
                # Persist the direction→gap linkage (mirrors the API write path
                # in admin_agent_ops.create_direction) so reconciliation can map
                # an active direction back to its problem-model gap. Skip and warn
                # when the gap_id resolves to a closed/nonexistent gap.
                if op_entry.get("gap_id"):
                    _gid_fb = str(op_entry["gap_id"])
                    from app.problem_model import check_gap_id_write
                    _gap_err_fb, _ = check_gap_id_write(_gid_fb, project.problem_model)
                    if _gap_err_fb:
                        logger.warning(
                            "[STRATEGIC] Task %s: skipping gap_id link for %s: gap %s is %s",
                            task.id, new_dir["id"], _gid_fb, _gap_err_fb,
                        )
                    else:
                        new_dir["gap_id"] = _gid_fb
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
                if not staged_create:
                    current_active = sum(
                        1 for d in directions if d.get("status") == "active"
                    )
                    if current_active >= _runtime_config.get(
                        "max_active_directions", 10
                    ):
                        logger.warning(
                            f"[STRATEGIC] Skipping direction create: active count {current_active} >= cap"
                        )
                        continue
                # Translate recommended_mode='pause' to paused status so the
                # existing spawn-skip (status != 'active') mechanism handles it.
                if rec_mode == "pause" and not staged_create:
                    new_dir["status"] = "paused"
                    new_dir["auto_paused"] = True
                    new_dir["paused_reason"] = (
                        "Strategic review: recommended_mode=pause"
                    )
                    new_dir["paused_at"] = _utcnow().isoformat()
                directions.append(new_dir)
                _delta_created.append(new_dir)
                # Stage the gap this fresh active direction staffs (active_moves
                # + clear deferral). Mirror the HTTP create_direction path: only a
                # live active direction staffs a gap (a pause-translated direction
                # is not yet working it). Applied to problem_model after the loop.
                if new_dir.get("gap_id") and new_dir.get("status") == "active":
                    _pending_gap_reconciles.append(
                        (new_dir["id"], new_dir["gap_id"], None, "active", None)
                    )
                ops_applied += 1
                creates_count += 1
                created_ids_fb.append(new_dir["id"])
                if rationale:
                    created_rationales_fb[new_dir["id"]] = rationale[
                        :_RATIONALE_PERSIST_MAX
                    ]
                if _retired_grounding:
                    created_retired_grounding_fb[new_dir["id"]] = _retired_grounding
                logger.info(
                    f"[STRATEGIC] Created direction '{text[:60]}' for project {project.id}"
                )

            elif op == "update_direction":
                dir_id = op_entry.get("direction_id")
                changes = op_entry.get("changes", {})
                raw_dedup_reasoning = op_entry.get("dedup_reasoning")
                if not dir_id or not changes:
                    continue
                try:
                    changes = normalize_direction_contract_changes(
                        changes, field_prefix="changes."
                    )
                except ValueError as exc:
                    logger.warning(
                        "[STRATEGIC] Task %s: skipped update_direction for %s — %s",
                        task.id,
                        dir_id,
                        exc,
                    )
                    continue
                blocked, why = challenge_rejects_portfolio_op(
                    fallback_challenge_record,
                    op,
                    direction_id=str(dir_id),
                )
                if blocked:
                    logger.info(
                        "[STRATEGIC] Task %s: fallback portfolio challenge blocked update_direction for %s: %s",
                        task.id,
                        dir_id,
                        why or "challenge rejected this update_direction",
                    )
                    continue
                # changes.criterion_keys — mirrors the API update_direction path in
                # admin_agent_ops: validate the keys (list of non-empty strings),
                # require an open version for non-empty keys, and reject dangling or
                # inactive keys. Empty []/None clears the direction's criterion link.
                criterion_keys_supplied = "criterion_keys" in changes
                criterion_keys: list[str] = []
                update_criterion_release_version_id = None
                if criterion_keys_supplied:
                    raw_criterion_keys = changes.get("criterion_keys")
                    if not isinstance(raw_criterion_keys, list) or not all(
                        isinstance(key, str) and key.strip()
                        for key in raw_criterion_keys
                    ):
                        logger.warning(
                            "[STRATEGIC] Task %s: skipped update_direction for %s — "
                            "changes.criterion_keys must be a list of non-empty strings",
                            task.id,
                            dir_id,
                        )
                        continue
                    criterion_keys = list(
                        dict.fromkeys(key.strip() for key in raw_criterion_keys)
                    )
                    if criterion_keys:
                        from app.direction_referential_guard import (
                            check_criterion_key_write,
                            load_version_criterion_key_sets,
                        )
                        from app.release_version import current_open_version

                        criterion_version = await current_open_version(
                            session, project.id
                        )
                        if criterion_version is None:
                            logger.warning(
                                "[STRATEGIC] Task %s: skipped update_direction for %s — "
                                "criterion_keys require an open version",
                                task.id,
                                dir_id,
                            )
                            continue
                        active_keys, inactive_keys = (
                            await load_version_criterion_key_sets(
                                session, project.id, criterion_version.id
                            )
                        )
                        invalid_criterion_keys = [
                            key
                            for key in criterion_keys
                            if check_criterion_key_write(
                                key, active_keys, inactive_keys
                            )[0]
                            is not None
                        ]
                        if invalid_criterion_keys:
                            logger.warning(
                                "[STRATEGIC] Task %s: skipped update_direction for %s — "
                                "invalid criterion_keys: %s",
                                task.id,
                                dir_id,
                                invalid_criterion_keys,
                            )
                            _record_rejected_direction_op(
                                task,
                                op=op,
                                direction_id=str(dir_id),
                                text=str(changes.get("text") or ""),
                                invalid_criterion_keys=invalid_criterion_keys,
                                active_criterion_keys=active_keys,
                            )
                            continue
                        update_criterion_release_version_id = criterion_version.id
                for d in directions:
                    if d.get("id") == dir_id:
                        requested_status = changes.get("status")
                        terminal_reason_fb = op_entry.get(
                            "terminal_reason"
                        ) or changes.get("terminal_reason")
                        if requested_status in {"expired", "superseded"} and d.get(
                            "status"
                        ) not in {"expired", "superseded"}:
                            from app.direction_terminal_guard import (
                                validate_direction_terminal_transition,
                            )

                            rationale_fb = op_entry.get("rationale") or changes.get(
                                "rationale"
                            )
                            terminal_ok, terminal_error = (
                                validate_direction_terminal_transition(
                                    d,
                                    rationale=rationale_fb,
                                    terminal_reason=terminal_reason_fb,
                                )
                            )
                            if not terminal_ok:
                                logger.warning(
                                    "[STRATEGIC] Task %s: skipped terminal "
                                    "update_direction for %s: %s",
                                    task.id,
                                    dir_id,
                                    terminal_error,
                                )
                                continue
                        # Snapshot contract fields before mutation to detect real changes.
                        _pre_text = d.get("text")
                        _pre_paths = list(d.get("paths") or [])
                        _pre_ctx = d.get("context") or {}
                        _pre_se = _pre_ctx.get("success_evidence")
                        _pre_rc = _pre_ctx.get("retirement_condition")
                        _pre_constraints = _pre_ctx.get(
                            "implementation_constraints"
                        )
                        # Prior gap_id captured before the change, for re-ground
                        # reconciliation (prune old gap, stage the new one).
                        _pre_gap_id_fb = d.get("gap_id")
                        # Prior status, so a flip INTO a terminal state
                        # (expired/superseded) records retired-direction memory
                        # exactly once (and not on updates that were already
                        # terminal).
                        _pre_status_fb = d.get("status")
                        _gap_changed_fb = False
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
                        # Update (or clear) the direction→gap linkage (mirrors the
                        # API update_direction path in admin_agent_ops). Skip and
                        # warn when the new gap_id resolves to a closed/nonexistent gap.
                        if "gap_id" in changes:
                            gid = changes["gap_id"]
                            if gid:
                                _gid_fb = str(gid)
                                from app.problem_model import check_gap_id_write
                                _gap_err_fb, _ = check_gap_id_write(_gid_fb, project.problem_model)
                                if _gap_err_fb:
                                    logger.warning(
                                        "[STRATEGIC] Task %s: skipping gap_id link for %s: gap %s is %s",
                                        task.id, dir_id, _gid_fb, _gap_err_fb,
                                    )
                                else:
                                    d["gap_id"] = _gid_fb
                                    _gap_changed_fb = True
                            else:
                                d.pop("gap_id", None)
                                _gap_changed_fb = True
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
                        retired_memory = list(
                            (project.vision or {}).get("_recently_retired_directions") or []
                        )
                        if "status" in changes and changes["status"] in (
                            "active",
                            "paused",
                            "expired",
                            "superseded",
                        ):
                            # Reactivation goes through the shared seam so it clears
                            # pause residue AND stamps the auto_recovered_at grace
                            # marker when resuming from paused — preventing frozen
                            # research_history from immediately re-pausing or
                            # zombie-retiring the direction. The strategist may NOT
                            # resume a user-paused direction (allow_user_paused=False):
                            # the helper returns False and we keep the existing
                            # blocked-reactivation warning, mutating nothing.
                            if changes["status"] == "active":
                                from app.direction_lifecycle import (
                                    reactivate_paused_direction,
                                )

                                if not reactivate_paused_direction(
                                    d, allow_user_paused=False
                                ):
                                    logger.warning(
                                        f"[STRATEGIC] Blocked reactivation of user-paused direction {dir_id}"
                                    )
                            else:
                                d["status"] = changes["status"]
                                if requested_status in {
                                    "expired",
                                    "superseded",
                                } and terminal_reason_fb:
                                    d["terminal_reason"] = (
                                        str(terminal_reason_fb).strip().lower()
                                    )
                                superseded_by = None
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
                            # Record retired-direction memory when this update
                            # flips the direction INTO a terminal state. Queue
                            # into the deferred batch (flushed under the single
                            # post-loop with_vision_locked pass) - do NOT write
                            # vision inside the op loop. Guard on the prior
                            # status so an update that was already terminal, or
                            # that sets a non-terminal status, records nothing.
                            if (
                                changes["status"] in ("expired", "superseded")
                                and _pre_status_fb
                                not in ("expired", "superseded")
                            ):
                                if changes["status"] == "superseded":
                                    _fb_term_reason = "strategic_superseded"
                                    _fb_sb = d.get("superseded_by")
                                    _fb_succ = (
                                        _fb_sb.get("direction_id")
                                        if isinstance(_fb_sb, dict)
                                        else None
                                    )
                                    _fb_term_outcome = (
                                        f"superseded by {_fb_succ}"
                                        if _fb_succ
                                        else None
                                    )
                                else:
                                    _fb_term_reason = "strategic_expired"
                                    _fb_term_outcome = rationale or None
                                _pending_retire_memory.append(
                                    {
                                        "snapshot": dict(d),
                                        "reason": _fb_term_reason,
                                        "outcome": _fb_term_outcome,
                                    }
                                )
                        if isinstance(raw_dedup_reasoning, dict):
                            dr: dict = {}
                            closest = raw_dedup_reasoning.get("closest_directions")
                            if isinstance(closest, list):
                                dr["closest_directions"] = closest[:3]
                            distinction = str(
                                raw_dedup_reasoning.get("distinction") or ""
                            ).strip()
                            if distinction:
                                dr["distinction"] = distinction[
                                    :DIRECTION_CONTRACT_FIELD_MAX
                                ]
                            if dr:
                                ctx = dict(d.get("context") or {})
                                ctx["dedup_reasoning"] = dr
                                ctx["grounding_verdict"] = validate_create_direction_grounding(
                                    dr,
                                    directions,
                                    retired_memory,
                                )
                                d["context"] = ctx
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
                        if criterion_keys_supplied:
                            d["criterion_keys"] = criterion_keys
                            if update_criterion_release_version_id is not None:
                                d["criterion_release_version_id"] = str(
                                    update_criterion_release_version_id
                                )
                            else:
                                d.pop("criterion_release_version_id", None)
                        # Persist context-structured fields (same cap as CREATE path)
                        ctx = d.get("context") or {}
                        _old_se = ctx.get("success_evidence")
                        _old_rc = ctx.get("retirement_condition")
                        _old_constraints = ctx.get("implementation_constraints")
                        for _ctx_field in (
                            "success_evidence",
                            "retirement_condition",
                            "implementation_constraints",
                        ):
                            if _ctx_field in changes:
                                val = changes[_ctx_field]
                                if val:
                                    ctx[_ctx_field] = val
                                else:
                                    ctx.pop(_ctx_field, None)
                        if (
                            ctx.get("success_evidence") != _old_se
                            or ctx.get("retirement_condition") != _old_rc
                            or ctx.get("implementation_constraints")
                            != _old_constraints
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
                            or _new_ctx.get("implementation_constraints")
                            != _pre_constraints
                        ):
                            from app.direction_lifecycle import (
                                _reset_outcome_detection_state,
                            )

                            _reset_outcome_detection_state(d)
                        # Record rationale as a note (dedup: skip if same text already present)
                        if rationale:
                            notes = d.get("notes", [])
                            note_text = f"Strategic update: {rationale}"
                            if not any(n.get("text") == note_text for n in notes):
                                notes.append(
                                    {
                                        "text": note_text,
                                        "added_at": _utcnow().isoformat(),
                                    }
                                )
                                if len(notes) > STRATEGIC_NOTES_HISTORY_MAX:
                                    logger.warning(
                                        f"[STRATEGIC] Task {task.id}: dropping {len(notes) - STRATEGIC_NOTES_HISTORY_MAX} oldest notes on direction {dir_id}"
                                    )
                                d["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                        if _gap_changed_fb or d.get("status") != _pre_status_fb:
                            _pending_gap_reconciles.append(
                                (
                                    str(dir_id),
                                    d.get("gap_id"),
                                    _pre_gap_id_fb,
                                    d.get("status"),
                                    _pre_status_fb,
                                )
                            )
                        _delta_mutated[str(dir_id)] = d
                        ops_applied += 1
                        logger.info(
                            f"[STRATEGIC] Updated direction {dir_id} for project {project.id}: {list(changes.keys())}"
                        )
                        break

            elif op == "retire_direction":
                dir_id = op_entry.get("direction_id")
                if not dir_id:
                    continue
                rationale = str(op_entry.get("rationale") or "").strip()
                blocked, why = challenge_rejects_portfolio_op(
                    fallback_challenge_record,
                    op,
                    direction_id=str(dir_id),
                )
                if blocked:
                    logger.info(
                        "[STRATEGIC] Task %s: fallback portfolio challenge blocked retire_direction for %s: %s",
                        task.id,
                        dir_id,
                        why or "challenge rejected this retire_direction",
                    )
                    continue
                from app.direction_terminal_guard import (
                    validate_direction_terminal_transition,
                )

                terminal_reason = op_entry.get("terminal_reason")
                # Direction retirement is the strategist's judgment call. The
                # acceptance contract is enforced only at qualify (the ledger
                # gate in compute_composite), so retiring a direction while a
                # criterion is unmet still cannot promote an unmet version —
                # there is no pre-qualification coverage precondition here.
                for d in directions:
                    if d.get("id") == dir_id:
                        terminal_ok, terminal_error = (
                            validate_direction_terminal_transition(
                                d,
                                rationale=rationale,
                                terminal_reason=terminal_reason,
                            )
                        )
                        if not terminal_ok:
                            logger.warning(
                                "[STRATEGIC] Task %s: skipped retire_direction for %s: %s",
                                task.id,
                                dir_id,
                                terminal_error,
                            )
                            break
                        d["status"] = "retired"
                        d["retired_at"] = _utcnow().isoformat()
                        if terminal_reason:
                            d["terminal_reason"] = str(terminal_reason).strip().lower()
                        _pending_retire_memory.append(
                            {
                                "snapshot": dict(d),
                                "reason": "strategic_retire",
                                "outcome": rationale or None,
                            }
                        )
                        if rationale:
                            notes = d.get("notes", [])
                            note_text = f"Retired by strategic review: {rationale}"
                            if not any(n.get("text") == note_text for n in notes):
                                notes.append(
                                    {
                                        "text": note_text,
                                        "added_at": _utcnow().isoformat(),
                                    }
                                )
                                if len(notes) > STRATEGIC_NOTES_HISTORY_MAX:
                                    logger.warning(
                                        f"[STRATEGIC] Task {task.id}: dropping {len(notes) - STRATEGIC_NOTES_HISTORY_MAX} oldest notes on direction {dir_id}"
                                    )
                                d["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                        _delta_mutated[str(dir_id)] = d
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

            elif op == "stage_direction":
                dir_id = op_entry.get("direction_id")
                if not dir_id:
                    continue
                blocked, why = challenge_rejects_portfolio_op(
                    fallback_challenge_record,
                    op,
                    direction_id=str(dir_id),
                )
                if blocked:
                    logger.info(
                        "[STRATEGIC] Task %s: fallback portfolio challenge blocked stage_direction for %s: %s",
                        task.id,
                        dir_id,
                        why or "challenge rejected this stage_direction",
                    )
                    continue
                from app.release_version import current_open_version

                open_version = await current_open_version(session, project.id)
                if open_version is None:
                    logger.info(
                        "[STRATEGIC] Task %s: fallback stage_direction skipped for %s because no open version exists",
                        task.id,
                        dir_id,
                    )
                    continue
                for d in directions:
                    if d.get("id") == dir_id:
                        staged_for_version = open_version.version_number
                        _pre_status_fb = d.get("status")
                        _gap_id_fb = d.get("gap_id")
                        d["status"] = "staged"
                        d["staged_for_version"] = staged_for_version
                        d["staged_at"] = _utcnow().isoformat()
                        notes = d.get("notes", [])
                        note_text = (
                            f"Staged to begin after v{staged_for_version} promotes"
                            + (f": {rationale}" if rationale else "")
                        )
                        if not any(n.get("text") == note_text for n in notes):
                            notes.append(
                                {"text": note_text, "added_at": _utcnow().isoformat()}
                            )
                            d["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                        ctx = task.context or {}
                        task.context = {
                            **ctx,
                            "_strategic_dirs_staged": ctx.get(
                                "_strategic_dirs_staged", 0
                            )
                            + 1,
                            "_strategic_staged_ids": list(  # noqa: RUF005
                                ctx.get("_strategic_staged_ids", [])
                            )
                            + [dir_id],
                        }
                        flag_modified(task, "context")
                        session.add(task)
                        if _gap_id_fb:
                            _pending_gap_reconciles.append(
                                (
                                    str(dir_id),
                                    _gap_id_fb,
                                    _gap_id_fb,
                                    "staged",
                                    _pre_status_fb,
                                )
                            )
                        _delta_mutated[str(dir_id)] = d
                        ops_applied += 1
                        logger.info(
                            f"[STRATEGIC] Staged direction {dir_id} for project {project.id}"
                        )
                        break

            elif op == "approve_direction":
                dir_id = op_entry.get("direction_id")
                if not dir_id:
                    continue
                target = next((d for d in directions if d.get("id") == dir_id), None)
                if not target:
                    continue
                if target.get("status") != "proposed":
                    logger.info(
                        "[STRATEGIC] Task %s: fallback approve_direction skipped for %s because status=%s",
                        task.id,
                        dir_id,
                        target.get("status"),
                    )
                    continue
                blocked, why = challenge_rejects_portfolio_op(
                    fallback_challenge_record,
                    op,
                    direction_id=str(dir_id),
                )
                if blocked:
                    logger.info(
                        "[STRATEGIC] Task %s: fallback portfolio challenge blocked approve_direction for %s: %s",
                        task.id,
                        dir_id,
                        why or "challenge rejected this approve_direction",
                    )
                    continue
                active_count = sum(1 for d in directions if d.get("status") == "active")
                max_active = _runtime_config.get("max_active_directions", 10)
                if active_count >= max_active:
                    logger.info(
                        "[STRATEGIC] Task %s: fallback approve_direction skipped for %s because active count %s/%s reached cap",
                        task.id,
                        dir_id,
                        active_count,
                        max_active,
                    )
                    continue
                target["status"] = "active"
                target["activated_at"] = _utcnow().isoformat()
                if target.get("gap_id"):
                    _pending_gap_reconciles.append(
                        (
                            str(dir_id),
                            target.get("gap_id"),
                            target.get("gap_id"),
                            "active",
                            "proposed",
                        )
                    )
                notes = target.get("notes", [])
                note_text = (
                    f"Approved by strategic review: {rationale}"
                    if rationale
                    else "Approved by strategic review"
                )
                if not any(n.get("text") == note_text for n in notes):
                    notes.append(
                        {"text": note_text, "added_at": _utcnow().isoformat()}
                    )
                    target["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                ctx = task.context or {}
                task.context = {
                    **ctx,
                    "_strategic_proposals_approved": ctx.get(
                        "_strategic_proposals_approved", 0
                    )
                    + 1,
                    "_strategic_dirs_updated": ctx.get("_strategic_dirs_updated", 0)
                    + 1,
                    "_strategic_updated_ids": list(  # noqa: RUF005
                        ctx.get("_strategic_updated_ids", [])
                    )
                    + [dir_id],
                }
                flag_modified(task, "context")
                session.add(task)
                _delta_mutated[str(dir_id)] = target
                ops_applied += 1
                logger.info(
                    f"[STRATEGIC] Approved direction {dir_id} for project {project.id}"
                )

            elif op == "reject_direction":
                dir_id = op_entry.get("direction_id")
                if not dir_id:
                    continue
                target = next((d for d in directions if d.get("id") == dir_id), None)
                if not target:
                    continue
                if target.get("status") != "proposed":
                    logger.info(
                        "[STRATEGIC] Task %s: fallback reject_direction skipped for %s because status=%s",
                        task.id,
                        dir_id,
                        target.get("status"),
                    )
                    continue
                blocked, why = challenge_rejects_portfolio_op(
                    fallback_challenge_record,
                    op,
                    direction_id=str(dir_id),
                )
                if blocked:
                    logger.info(
                        "[STRATEGIC] Task %s: fallback portfolio challenge blocked reject_direction for %s: %s",
                        task.id,
                        dir_id,
                        why or "challenge rejected this reject_direction",
                    )
                    continue
                rejected_text = (target.get("text") or "")[:200].strip()
                remaining = [d for d in directions if d.get("id") != dir_id]
                if rejected_text:
                    now_iso = _utcnow().isoformat()
                    tombstone = {
                        "id": target["id"],
                        "text": rejected_text,
                        "status": "rejected",
                        "rejected_at": now_iso,
                        "rejected_by": "strategic",
                        **({"rejection_rationale": rationale} if rationale else {}),
                    }
                    rejected = [d for d in remaining if d.get("status") == "rejected"]
                    non_rejected = [
                        d for d in remaining if d.get("status") != "rejected"
                    ]
                    rejected.append(tombstone)
                    rejected = rejected[-20:]
                    remaining = non_rejected + rejected
                    # Persist to vision["rejected_proposals"] so the starvation
                    # fallback dedup survives direction-list pruning. Defer the
                    # actual write to a single with_vision_locked() pass after the
                    # loop (mirrors _pending_retire_memory) so the read-modify-write
                    # is serialized under the project row lock.
                    _pending_rejected_proposals.append(
                        {"text": rejected_text, "rejected_at": now_iso}
                    )
                cancelled_result = await session.execute(
                    select(Task).where(
                        Task.project_id == project.id,
                        Task.status == TaskStatus.QUEUED,
                        Task.context["direction_id"].as_string() == dir_id,
                    )
                )
                cancelled_tasks = cancelled_result.scalars().all()
                vm_ids_to_delete: list[str] = []
                for t in cancelled_tasks:
                    t.status = TaskStatus.CANCELLED
                    t.latest_status_message = (
                        f"Cancelled: proposal {dir_id} was rejected by strategic review"
                    )
                    session.add(t)
                    if t.vm_instance_id:
                        vm_ids_to_delete.append(t.vm_instance_id)
                directions = remaining
                if rejected_text:
                    _delta_mutated[str(dir_id)] = tombstone
                else:
                    _delta_removed.add(str(dir_id))
                ctx = task.context or {}
                task.context = {
                    **ctx,
                    "_strategic_proposals_rejected": ctx.get(
                        "_strategic_proposals_rejected", 0
                    )
                    + 1,
                    "_strategic_dirs_retired": ctx.get("_strategic_dirs_retired", 0)
                    + 1,
                    "_strategic_retired_ids": list(  # noqa: RUF005
                        ctx.get("_strategic_retired_ids", [])
                    )
                    + [dir_id],
                }
                flag_modified(task, "context")
                session.add(task)
                for vm_id in vm_ids_to_delete:
                    from app.vm_lifecycle import (
                        _ensure_vm_deleted as _main_ensure_vm_deleted,
                    )

                    await _main_ensure_vm_deleted(vm_id, reason="proposal_rejected")
                ops_applied += 1
                logger.info(
                    f"[STRATEGIC] Rejected direction {dir_id} for project {project.id}"
                )

            elif op == "activate_direction":
                dir_id = op_entry.get("direction_id")
                if not dir_id:
                    continue
                target = next((d for d in directions if d.get("id") == dir_id), None)
                if not target:
                    continue
                cur_status = target.get("status")
                _TERMINAL_STATUSES_FB = frozenset(
                    {"retired", "expired", "superseded", "completed", "rejected"}
                )
                if cur_status == "active":
                    logger.info(
                        "[STRATEGIC] Task %s: fallback activate_direction skipped for %s because already active",
                        task.id,
                        dir_id,
                    )
                    continue
                if cur_status in _TERMINAL_STATUSES_FB:
                    logger.info(
                        "[STRATEGIC] Task %s: fallback activate_direction skipped for %s because terminal status=%s",
                        task.id,
                        dir_id,
                        cur_status,
                    )
                    continue
                blocked, why = challenge_rejects_portfolio_op(
                    fallback_challenge_record,
                    op,
                    direction_id=str(dir_id),
                )
                if blocked:
                    logger.info(
                        "[STRATEGIC] Task %s: fallback portfolio challenge blocked activate_direction for %s: %s",
                        task.id,
                        dir_id,
                        why or "challenge rejected this activate_direction",
                    )
                    continue
                active_count = sum(1 for d in directions if d.get("status") == "active")
                max_active = _runtime_config.get("max_active_directions", 10)
                if active_count >= max_active:
                    logger.info(
                        "[STRATEGIC] Task %s: fallback activate_direction skipped for %s because active count %s/%s reached cap",
                        task.id,
                        dir_id,
                        active_count,
                        max_active,
                    )
                    continue
                from app.direction_lifecycle import reactivate_paused_direction as _reactivate_fb
                _reactivate_fb(target, allow_user_paused=True)
                target.pop("staged_at", None)
                if rationale:
                    notes = target.get("notes", [])
                    note_text = f"Reactivated by strategic op: {rationale}"
                    if not any(n.get("text") == note_text for n in notes):
                        notes.append(
                            {"text": note_text, "added_at": _utcnow().isoformat()}
                        )
                        target["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                _gap_id_activate_fb = target.get("gap_id")
                if _gap_id_activate_fb:
                    _pending_gap_reconciles.append(
                        (
                            str(dir_id),
                            _gap_id_activate_fb,
                            _gap_id_activate_fb,
                            "active",
                            cur_status,
                        )
                    )
                _delta_mutated[str(dir_id)] = target
                ctx = task.context or {}
                task.context = {
                    **ctx,
                    "_strategic_dirs_updated": ctx.get("_strategic_dirs_updated", 0) + 1,
                    "_strategic_updated_ids": list(  # noqa: RUF005
                        ctx.get("_strategic_updated_ids", [])
                    ) + [dir_id],
                }
                flag_modified(task, "context")
                session.add(task)
                ops_applied += 1
                logger.info(
                    f"[STRATEGIC] Reactivated direction {dir_id} for project {project.id}"
                )

        if ops_applied > 0:
            from app.task_locking import with_directions_locked

            _fb_mutated = dict(_delta_mutated)
            _fb_removed = set(_delta_removed)
            _fb_created = list(_delta_created)

            def _apply_fb_delta(fresh: list) -> list:
                result = []
                for d in fresh:
                    did = str(d.get("id") or "")
                    if did in _fb_removed:
                        continue
                    if did in _fb_mutated:
                        result.append(_fb_mutated[did])
                    else:
                        result.append(d)
                fresh_ids = {str(d.get("id") or "") for d in fresh}
                for new_dir in _fb_created:
                    if str(new_dir.get("id") or "") not in fresh_ids:
                        result.append(new_dir)  # noqa: PERF401
                # Enforce the 20-tombstone cap on "rejected" entries so the
                # fallback path stays consistent with the HTTP reject path
                # (admin_agent_ops.py) and rejected entries never accumulate
                # unboundedly via repeated fallback writes.
                rejected_entries = [
                    d for d in result if d.get("status") == "rejected"
                ]
                if len(rejected_entries) > 20:
                    non_rejected = [
                        d for d in result if d.get("status") != "rejected"
                    ]
                    result = non_rejected + rejected_entries[-20:]
                return result

            await with_directions_locked(session, task.project_id, _apply_fb_delta)

        # Reconcile problem-model gaps for any create/re-ground that carried a
        # gap_id, in the same locked transaction as the directions write (the
        # project row is held for this whole fallback). Parse once, apply the
        # whole batch, write back only when something changed — mirrors the HTTP
        # create/update_direction reconciliation. Fail-open on a malformed model.
        if _pending_gap_reconciles:
            from app.problem_model import (
                parse_problem_model,
                reconcile_gap_staffing_for_status,
            )
            from pydantic import ValidationError as _PMValidationError

            try:
                _pm_model = parse_problem_model(project.problem_model)
            except _PMValidationError:
                _pm_model = None
            if _pm_model is not None:
                _pm_changed = False
                for _entry in _pending_gap_reconciles:
                    if len(_entry) == 3:
                        _did, _new_g, _prior_g = _entry
                        _new_status = "active"
                        _prior_status = None
                    else:
                        _did, _new_g, _prior_g, _new_status, _prior_status = _entry
                    _result = reconcile_gap_staffing_for_status(
                        _pm_model,
                        _did,
                        _new_g,
                        _prior_g,
                        new_status=_new_status,
                        prior_status=_prior_status,
                        now_iso=_utcnow().isoformat(),
                    )
                    if _result.changed:
                        _pm_changed = True
                    if _result.unresolved_gap_ids:
                        logger.warning(
                            "[STRATEGIC] Task %s: fallback gap staffing "
                            "reconciliation unresolved for %s: %s",
                            task.id,
                            _did,
                            _result.model_dump(),
                        )
                if _pm_changed:
                    from app.problem_model import (
                        enforce_problem_model_size as _enforce_pm_size,
                    )

                    _gc_summary, _oversize_section = _enforce_pm_size(_pm_model)
                    if _oversize_section:
                        from app.alert_dispatch import (
                            _maybe_create_problem_model_live_core_alert,
                        )

                        logger.error(
                            "[STRATEGIC] Task %s: problem_model live core exceeds "
                            "byte budget after gap-staffing reconciliation: %s",
                            task.id,
                            _oversize_section,
                        )
                        await _maybe_create_problem_model_live_core_alert(
                            task.project_id, session
                        )
                    project.problem_model = _pm_model.model_dump()
                    flag_modified(project, "problem_model")

        if _pending_retire_memory:
            from app.task_locking import with_vision_locked

            _retire_batch = list(_pending_retire_memory)

            def _apply_retire_memory(vision: dict) -> None:
                for _rd in _retire_batch:
                    record_retired_direction_memory(
                        vision,
                        _rd["snapshot"],
                        reason=_rd["reason"],
                        outcome=_rd.get("outcome"),
                    )

            await with_vision_locked(session, task.project_id, _apply_retire_memory)

        # Reconcile problem-model gaps for the batch's retired directions in a
        # single deferred locked pass (mirrors the _pending_retire_memory flush
        # above) — NOT inside the per-op loop. Prunes each retired id from its
        # gap's active_moves (-> past_moves) and records a GapDeferral when the
        # retire emptied a gap's active_moves, so a gap whose only staffing
        # direction just retired is acknowledged instead of flagged as a stale
        # active-move / orphaned gap by the next cycle's reconciliation view.
        if retires_applied and retired_ids_fb:
            from app.problem_model import reconcile_gaps_for_retired_directions
            from app.task_locking import with_problem_model_locked

            _reconcile_ids = list(retired_ids_fb)
            _reconcile_now = _utcnow().isoformat()

            def _apply_gap_reconcile(pm: dict) -> None:
                reconcile_gaps_for_retired_directions(
                    pm, _reconcile_ids, _reconcile_now
                )

            await with_problem_model_locked(
                session, task.project_id, _apply_gap_reconcile
            )

        if _pending_rejected_proposals:
            from app.task_locking import with_vision_locked

            _rejected_batch = list(_pending_rejected_proposals)

            def _apply_rejected_proposals(vision: dict) -> None:
                _merge_rejected_proposals(vision, _rejected_batch)

            await with_vision_locked(
                session, task.project_id, _apply_rejected_proposals
            )

    # Apply version_phase_decision + qualify_request from the parsed output
    # (fallback / log-parse path). In practice the agent emits both fields
    # only through the set_problem_model API (which forces the API path
    # above), so this fallback is effectively unreached today — but the two
    # paths are kept symmetric so a future fallback-only emission path can't
    # silently drop one signal. Shapes: version_phase_decision = {phase:
    # "build"|"stabilize", reasoning: str}; qualify_request = {reason: str,
    # probes?: ["walkthrough_e2e"]}.
    _vpd_parsed = parsed.get("version_phase_decision")
    await _apply_version_phase_decision(
        _vpd_parsed, task, project, session, path="fallback"
    )
    # See _apply_qualify_request_post_commit: the qualify_request side-effect does
    # slow GitHub I/O and must run AFTER this path's lock-release commit, on a
    # fresh session — never under the completion's Project lock. Captured here.
    _qr_parsed = parsed.get("qualify_request")
    _qr_task_id_fb = task.id
    _qr_project_id_fb = task.project_id

    # Update cycle history with final outcome
    # Audit W2: cycle outcome lives on the StrategicCycle row.
    cycle_id = task.strategic_cycle_id or (task.context or {}).get("strategic_cycle_id")
    if not cycle_id:
        # Legacy fallback task that never carried the cycle linkage (spawned
        # before the strategic_cycle_id column/context, or a context-stripped
        # replay): resolve the owning cycle from the task linkage so this
        # completion still finalizes the row and broadcasts the real cycle id.
        _linked_cycle = await strategic_cycle.get_by_task(session, task.id)
        if _linked_cycle is not None:
            cycle_id = _linked_cycle.id
    dirs_created = (
        creates_count if direction_ops and isinstance(direction_ops, list) else 0
    )
    fallback_cycle = None
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
            if created_retired_grounding_fb:
                _fb_extra["created_retired_grounding"] = dict(
                    created_retired_grounding_fb
                )
            _fb_critiques = parsed.get("retired_direction_critiques")
            if _fb_critiques is not None:
                _fb_extra["retirement_critiques"] = _fb_critiques
            # A challenge record is always stored on task.context by the
            # set_strategic_challenge endpoint (never in the parsed log), so the
            # fallback path reads it from context too. No-op when absent.
            _fb_challenge = (task.context or {}).get(STRATEGIC_CHALLENGE_CONTEXT_KEY)
            if _fb_challenge is not None:
                _fb_extra["strategic_judgment_challenge"] = _fb_challenge
            # Direction ops dropped for invalid criterion_keys — copied into
            # cycle_metadata so the next cycle's payload producer can re-surface
            # them to the strategist (durable across the transaction boundary).
            _fb_rejected_ops = (task.context or {}).get(
                REJECTED_DIRECTION_OPS_CONTEXT_KEY
            )
            if _fb_rejected_ops:
                _fb_extra["rejected_direction_ops"] = _fb_rejected_ops[
                    -_REJECTED_DIRECTION_OPS_MAX:
                ]
            fallback_cycle = await strategic_cycle.mark_phase2_completed(
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
            fallback_cycle = await strategic_cycle.mark_failure(
                session,
                cycle_id,
                phase="phase2",
                reason="strategic_review fallback path produced no vision",
            )

    await _check_direction_starvation(project, dirs_created, session, task=task)
    session.add(project)

    # Deferred import of message/resolution caps — same STRATEGIC_* namespace
    from app.strategic_constants import (
        STRATEGIC_MESSAGE_CONTENT_MAX,
        STRATEGIC_RESOLUTION_MAX,
    )

    # Process messages from agent (questions, blockers, notes)
    msgs_created = 0
    from app.routers.admin_notifications import (
        ROUTINE_EVENT_TYPES as _ROUTINE_EVENT_TYPES,
        append_routine_digest as _append_routine_digest,
    )

    reserved_message_sequences = iter(message_sequences or [])
    message_events: dict[
        uuid.UUID, tuple[ProjectMessage, str | None]
    ] = {}

    def _collect_message_event(
        message: ProjectMessage,
        *,
        status: str | None = None,
    ) -> None:
        existing = message_events.get(message.id)
        # A row created during this completion stays a create event even if the
        # same bounded pass subsequently mutates it.
        if existing is not None and existing[1] is None:
            return
        message_events[message.id] = (message, status)
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
        _raw_msg_ctx = msg_entry.get("context")
        _msg_ctx = dict(_raw_msg_ctx) if isinstance(_raw_msg_ctx, dict) else {}
        # Reproduce-on-HEAD citation grounding for strategist-authored bug-class
        # messages: a BUG_REPORT/BLOCKER parsed out of the strategic agent's
        # output that cites code already fixed/refactored away would re-enter the
        # strategist inbox fully trusted. Run the SAME conservative grounding the
        # user/annotation paths use and stash an ``_provenance_warnings`` signal
        # when a citation is unambiguously absent. Best-effort — indeterminate
        # never flags, never fails message emission.
        if msg_type in (MessageType.BUG_REPORT, MessageType.BLOCKER):
            from app.source_ref_validation import compute_message_citation_warning

            warning = await compute_message_citation_warning(session, project, content)
            if warning:
                _msg_ctx = {**_msg_ctx, "_provenance_warnings": warning}
        _event_type = _msg_ctx.get("event_type")
        # When replying to a specific message (reply_to present) and no explicit
        # event_type was supplied, default to "question_answer" (OPERATOR_FYI)
        # instead of "strategic_message" (STRATEGIST_INTERNAL).  Without this,
        # the operator never receives a notification that a reply arrived.
        _default_event_type = (
            "question_answer"
            if _event_type is None and _msg_ctx.get("reply_to") is not None
            else "strategic_message"
        )
        _msg_ctx = stamp_message_audience(
            _msg_ctx, _event_type or _default_event_type, message_type=msg_type.value
        )
        _conversation_reply = _event_type not in _ROUTINE_EVENT_TYPES
        _reply_to_id: uuid.UUID | None = None
        _raw_reply_to = _msg_ctx.get("reply_to")
        if _raw_reply_to:
            try:
                _candidate_reply_id = uuid.UUID(str(_raw_reply_to))
                _reply_target = await session.get(ProjectMessage, _candidate_reply_id)
                if _reply_target is not None and _reply_target.project_id == project.id:
                    _reply_to_id = _candidate_reply_id
                    _reply_target.handling_state = MessageHandlingState.RESPONDED
                    _reply_target.handled_by_task_id = task.id
                    _reply_target.updated_at = _utcnow()
                    session.add(_reply_target)
                    _collect_message_event(
                        _reply_target, status=str(_reply_target.status)
                    )
                else:
                    _msg_ctx.pop("reply_to", None)
            except (TypeError, ValueError):
                _msg_ctx.pop("reply_to", None)
        if _reply_to_id is not None:
            _msg_ctx["message_role"] = "operator_reply"

        reserved_sequence = next(reserved_message_sequences, None)
        new_msg = await build_project_message(
            session,
            project_id=project.id,
            sequence=reserved_sequence,
            surface=(
                MessageSurface.CONVERSATION
                if _conversation_reply
                else MessageSurface.ACTIVITY
            ),
            handling_state=(
                MessageHandlingState.RESPONDED
                if _conversation_reply
                else MessageHandlingState.PENDING
            ),
            handled_by_task_id=task.id if _conversation_reply else None,
            reply_to_message_id=_reply_to_id,
            message_type=msg_type,
            from_type="strategic",
            from_task_id=task.id,
            content=content,
            context=_msg_ctx,
        )
        session.add(new_msg)
        _collect_message_event(new_msg)
        if _event_type:  # noqa: SIM102
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
    from app.message_convergence import (
        LINKED_STATUS as _LINKED_STATUS,
        is_bug_class as _is_bug_class,
        link_message_to_outcome as _link_message_to_outcome,
    )

    # Batch-fetch all messages upfront to avoid N+1 SELECTs in the loop below.
    _resolve_batch = resolved_entries[:10]
    _msg_ids: list[uuid.UUID] = []
    for _entry in _resolve_batch:
        try:
            _raw_id = _entry.get("id") if isinstance(_entry, dict) else str(_entry)
            _msg_ids.append(uuid.UUID(str(_raw_id)))
        except (ValueError, TypeError):  # noqa: PERF203
            pass
    if _msg_ids:
        _batch_result = await session.execute(
            select(ProjectMessage).where(ProjectMessage.id.in_(_msg_ids))
        )
        _messages_by_id: dict[uuid.UUID, ProjectMessage] = {
            m.id: m for m in _batch_result.scalars().all()
        }
    else:
        _messages_by_id = {}

    for entry in _resolve_batch:
        try:
            msg_id = entry.get("id") if isinstance(entry, dict) else str(entry)
            resolution_text = (
                entry.get("resolution", "Addressed by strategic review")
                if isinstance(entry, dict)
                else "Addressed by strategic review"
            )
            # Ownership fields signal that the fix is in-flight rather than already landed.
            owned_by_direction = (
                entry.get("owned_by_direction") if isinstance(entry, dict) else None
            )
            owned_by_task = (
                entry.get("owned_by_task") if isinstance(entry, dict) else None
            )
            msg = _messages_by_id.get(uuid.UUID(str(msg_id)))
            # Accept both 'open' and 'linked' so an explicit terminal resolve can
            # always move a linked message to 'resolved' (the operator's escape hatch).
            if (
                msg
                and msg.project_id == project.id
                and msg.status
                in {
                    "open",
                    _LINKED_STATUS,
                }
            ):
                if _is_bug_class(msg) and (owned_by_direction or owned_by_task):
                    # Bug-class message with ownership info: record the forward
                    # link and move to 'linked' state. The actual convergence to
                    # 'resolved' happens when the owning outcome lands (reaper sweep).
                    kind = "direction" if owned_by_direction else "task"
                    identifier = owned_by_direction or owned_by_task
                    rt = str(resolution_text or f"Fix owned by {kind}: {identifier}")
                    if len(rt) > STRATEGIC_RESOLUTION_MAX:
                        rt = rt[:STRATEGIC_RESOLUTION_MAX]
                    _link_message_to_outcome(msg, kind, str(identifier))
                    msg.resolution = rt
                    msg.handling_state = MessageHandlingState.RESPONDED
                    msg.handled_by_task_id = task.id
                    msg.updated_at = _utcnow()
                    session.add(msg)
                    clustered = await _link_clustered_bug_messages(
                        session,
                        project,
                        msg,
                        kind=kind,
                        identifier=str(identifier),
                        resolution_text=rt,
                    )
                    _collect_message_event(msg, status=str(msg.status))
                    for clustered_msg in clustered:
                        _collect_message_event(
                            clustered_msg, status=str(clustered_msg.status)
                        )
                    msgs_resolved += 1 + len(clustered)
                    logger.info(
                        f"[STRATEGIC] Task {task.id}: linked {msg.message_type} "
                        f"message {msg.id} to {kind}={identifier} "
                        f"(clustered={len(clustered)})"
                    )
                else:
                    # Explicit terminal resolve: operator/strategist dismisses
                    # the message (no ownership info, or not a bug-class type).
                    msg.status = "resolved"
                    msg.handling_state = MessageHandlingState.RESPONDED
                    msg.handled_by_task_id = task.id
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
                    msg.updated_at = msg.resolved_at
                    session.add(msg)
                    _collect_message_event(msg, status=str(msg.status))
                    msgs_resolved += 1
                    if msg.message_type == MessageType.FEEDBACK:
                        try:
                            from app.notification_dispatch import (
                                post_feedback_acknowledgment,
                            )

                            reply = await post_feedback_acknowledgment(
                                session,
                                project.id,
                                msg.id,
                                msg.resolution or "Addressed by strategic review",
                                task.id,
                                sequence=next(reserved_message_sequences, None),
                            )
                            _collect_message_event(reply)
                        except Exception as _e:  # noqa: BLE001
                            logger.warning(
                                f"[NOTIFY] Failed to post feedback acknowledgment: {_e}"
                            )
                    elif msg.message_type == MessageType.QUESTION:
                        try:
                            from app.notification_dispatch import (
                                post_question_answer,
                            )

                            from app.strategic_resident_session import (
                                read_resident_session,
                                resident_session_is_active,
                            )

                            resident_blob = read_resident_session(project.vision)
                            reply = await post_question_answer(
                                session,
                                project.id,
                                msg.id,
                                msg.resolution or "Addressed by strategic review",
                                task.id,
                                resident_session_id=(
                                    str(resident_blob.get("session_id"))
                                    if resident_session_is_active(resident_blob)
                                    and resident_blob.get("session_id")
                                    else None
                                ),
                                sequence=next(reserved_message_sequences, None),
                            )
                            _collect_message_event(reply)
                        except Exception as _e:  # noqa: BLE001
                            logger.warning(
                                f"[NOTIFY] Failed to post question answer: {_e}"
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
    # Audit: check for unanswered QUESTION messages
    remaining_questions_result = await session.execute(
        select(func.count())
        .select_from(ProjectMessage)
        .where(
            ProjectMessage.project_id == project.id,
            ProjectMessage.status == "open",
            ProjectMessage.message_type == MessageType.QUESTION,
        )
    )
    open_question_count = remaining_questions_result.scalar() or 0
    if open_question_count > 0:
        logger.warning(
            f"[STRATEGIC] Task {task.id}: {open_question_count} QUESTION(s) left unanswered after strategic review"
        )
        await _create_strategic_alert(
            task.project_id,
            session,
            f"{open_question_count} QUESTION(s) left unanswered after strategic review — agent may be ignoring operator questions",
        )

    vision_rev = (project.vision or {}).get("revision", "?")
    # Per-cycle message post removed: the strategic agent now maintains
    # project.running_update as a living markdown document it rewrites each
    # cycle; the user acknowledges to stamp a read-marker.
    task.latest_status_message = f"Strategic review complete — vision rev {vision_rev}, {ops_applied} direction ops, {msgs_created} messages, {msgs_resolved} resolved"
    session.add(task)
    try:
        await _spawn_retirement_memos(
            session, project, task, retired_ids_fb, retired_rationales_fb
        )
    except Exception:
        logger.error(
            "[STRATEGIC] Task %s: _spawn_retirement_memos failed (fallback path); retirement memos "
            "not created for direction IDs %s — commit will still proceed",
            task.id,
            retired_ids_fb,
            exc_info=True,
        )
    await (
        session.commit()
    )  # Single commit: vision + directions + messages + task status
    # Canonical message events are emitted only after the transaction is
    # durable. Sequence order makes concurrent/reconnecting clients observe the
    # same conversation/activity projection as cursor pagination.
    ordered_message_events = sorted(
        message_events.values(),
        key=lambda item: (
            item[0].sequence is None,
            item[0].sequence if item[0].sequence is not None else 0,
            str(item[0].id),
        ),
    )
    for message, status in ordered_message_events:
        try:
            await broadcast_to_project(
                str(project.id),
                "message_created" if status is None else "message_updated",
                message_event_payload(message, status=status),
            )
        except Exception:  # noqa: BLE001, PERF203
            # The durable sequence lets reconnect/cursor fetch repair a missed
            # realtime notification. Never requeue an already-committed
            # strategic completion because a websocket fanout failed.
            logger.warning(
                "[STRATEGIC] Failed to broadcast committed message %s",
                message.id,
                exc_info=True,
            )
    # qualify_request runs post-commit on its own fresh session (see
    # _apply_qualify_request_post_commit) so its slow GitHub I/O never holds this
    # completion's Project lock / connection.
    await _apply_qualify_request_post_commit(
        _qr_parsed, task_id=_qr_task_id_fb, project_id=_qr_project_id_fb, path="fallback"
    )
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
        await emit_strategic_updated(
            task.project_id,
            STRATEGIC_KIND_RUNNING_UPDATE,
            task_id=task.id,
            cycle_id=cycle_id,
            output_kind=STRATEGIC_KIND_RUNNING_UPDATE,
            op="fallback_parse",
            running_update_changed=True,
        )
    if new_problem_model and isinstance(new_problem_model, dict):
        await emit_strategic_updated(
            task.project_id,
            STRATEGIC_KIND_PROBLEM_MODEL,
            task_id=task.id,
            cycle_id=cycle_id,
            output_kind=STRATEGIC_KIND_PROBLEM_MODEL,
            op="fallback_parse",
            problem_model_changed=True,
        )
    if ops_applied or retires_applied:
        await emit_strategic_updated(
            task.project_id,
            STRATEGIC_KIND_DIRECTIONS,
            task_id=task.id,
            cycle_id=cycle_id,
            output_kind=STRATEGIC_KIND_DIRECTIONS,
            op="fallback_parse",
            changed_direction_ids=list(created_ids_fb) + list(retired_ids_fb),
            changed_count=ops_applied + retires_applied,
        )
    fallback_succeeded = fallback_cycle is None or (
        getattr(fallback_cycle, "outcome", None) == "completed"
    )
    await emit_strategic_cycle_completed(
        task.project_id,
        task.id,
        2,
        cycle_id=cycle_id,
        phase_state="cycle_done" if fallback_succeeded else "failed",
        completion=_strategic_event_completion_summary(
            task,
            project,
            cycle=fallback_cycle,
            status="completed" if fallback_succeeded else "failed",
            created_ids=created_ids_fb,
            retired_ids=retired_ids_fb,
            problem_model_changed=bool(
                new_problem_model and isinstance(new_problem_model, dict)
            ),
            running_update_changed=isinstance(new_running_update, str),
        ),
    )
    logger.info(
        f"[STRATEGIC] Task {task.id} complete: vision rev {vision_rev}, {ops_applied} direction ops, {msgs_created} msgs created, {msgs_resolved} resolved"
    )
    await _maybe_retry_workday_auto_start(task.project_id)
