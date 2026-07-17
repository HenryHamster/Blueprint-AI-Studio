"""Review-fix lane pilot wiring (flag-gated, self-repo only).

Wires the persistent improvement-lane substrate into the live
``review -> fix -> re-review`` loop, lane-izing ONLY the fix side.

Gate-integrity invariant (kill-class, never tunable)
----------------------------------------------------
The reviewer / auditor must NEVER carry lane context
(``improvement_lane_id`` / ``is_lane_phase`` / ``lane_checkpoint_sha``) and
must NEVER be placed on a lane-reserved warm-pool member. Warm-sharing a
reviewer onto the implementer's workspace would destroy the independence the
review provides — that is the single strongest quality signal the system has.
Only the FIX task resumes warm onto the lane workspace.

Flag states
-----------
Off (``improvement_lanes_enabled`` False): every chain runs the classic path
byte-for-byte unchanged. Shadow (master True, pilot False): eligible self-repo
chains create armless lanes (``pilot_arm=None``) and exercise the real
SHA-verified checkpoint/CAS contract, but write no ITT assignment, take no warm
reservation, attach no fix prompt/dispatch context, force no cold reviewer, and
spawn no recovery tasks. Full pilot (both flags True): today's lane/control
intent-to-treat trial with fix-side warm resume and bounded recovery.

Intent-to-treat
---------------
Arm membership ('lane' vs 'control') is a pure function of the immutable
original create-task id (stable djb2 hash parity — the same deterministic
technique the repo-map holdback arm uses). Because the create task runs
identically in both arms and the only lane treatment is fix-side warm-resume,
deciding the arm at the create -> review boundary (before any fix-side
treatment) is genuine intent-to-treat. The decision is recorded ONCE as a
durable context key (``LANE_PILOT_ASSIGNMENT_KEY``), separate from the
use-receipt keys which are written only when lane mechanics actually engage.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.time_utils import _utcnow

# Canonical keys/constants are OWNED by the measurement subtask — import them
# verbatim, never re-type the literals (a drifted key silently de-populates the
# trial's arm/receipt subpopulations).
from app.capability_trial_verdict import (
    LANE_PILOT_ASSIGNMENT_KEY,
    LANE_PILOT_CAP_NAME,
    LANE_PILOT_CHECKPOINT_RECEIPT_KEY,
    LANE_PILOT_WARM_RESUME_RECEIPT_KEY,
)

logger = logging.getLogger(__name__)

# Chain-private lane identity. Unlike ``improvement_lane_id`` (which the
# warm-pool dispatch hook honors by leasing the lane-reserved member), this key
# is inert to dispatch: it only lets a later fix spawn re-discover the chain's
# lane. It rides the chain on EVERY task (including the cold review) so the
# binding survives, while the active dispatch directive does not.
LANE_CHAIN_ID_KEY = "_review_fix_lane_id"
WORKSPACE_PREP_CONTEXT_KEY = "_workspace_prep"

# Transient lane-resume block attached ONLY to a fix-task context. Carries the
# accumulated chain knowledge (prior phase summaries, changed/critical files,
# open issues, known risks) derived from the lane manifest + last checkpoint so
# the fix agent starts warm on chain context. It is a transient dispatch
# directive: it must NEVER ride a review/audit spawn (a reviewer reading stale
# fix context would erode the independence the review provides), so it is listed
# in TRANSIENT_LANE_CONTEXT_KEYS and re-attached freshly per fix phase.
LANE_RESUME_CONTEXT_KEY = "lane_resume"

# Per-phase dispatch directives that must NEVER be inherited from a source task.
# They are (re)attached freshly and only ever for a fix task on a lane arm.
TRANSIENT_LANE_CONTEXT_KEYS = (
    "improvement_lane_id",
    "is_lane_phase",
    "lane_checkpoint_sha",
    LANE_RESUME_CONTEXT_KEY,
)

# Bounds for the derived manifest patch and the fix-resume block. Kept well
# under the 8KB ImprovementLane.manifest contract (_LANE_MANIFEST_MAX_BYTES) so
# accumulation across phases never breaches it, and so the LaneCheckpointPayload
# 8KB manifest_patch validator never rejects a single phase's patch.
_MANIFEST_LIST_MAX_ITEMS = 6
_MANIFEST_STR_MAX_CHARS = 200
_MANIFEST_PATCH_MAX_BYTES = 6000
_RESUME_LIST_MAX_ITEMS = 6
_RESUME_BLOCK_MAX_BYTES = 4000

# Underscore-prefixed context keys that the review pipeline must propagate
# across iterations (the spawn-time copy filters underscore keys by default).
PILOT_PROPAGATE_KEYS = frozenset({LANE_PILOT_ASSIGNMENT_KEY, LANE_CHAIN_ID_KEY})

# Lane workspace lifecycle (mirrors ImprovementLane.workspace_state literals).
WORKSPACE_COLD = "COLD"
WORKSPACE_WARM_RESERVED = "WARM_RESERVED"
WORKSPACE_LEASED = "LEASED"

_VALID_ARMS = ("lane", "control")
_SHADOW_ARM = "shadow"

# ── Checkpoint-phase recovery classification ──────────────────────────────────
# Returned by ``checkpoint_or_recover_phase`` so the review-spawn path can act on
# a missing/unverifiable checkpoint instead of silently letting a cold review
# proceed (the old behavior, where a miss only showed up as ``phase_requeues``).
#   OK        → checkpoint written, or a benign no-op (lane None / already
#               PARKED). The caller spawns the cold review normally.
#   RECOVER   → checkpoint MISSING and the requeue budget is intact. The caller
#               MUST suppress the review and spawn a lane-aware retry of the
#               failed phase from durable context.
#   EXHAUSTED → checkpoint MISSING and the requeue budget is exhausted. The lane
#               has been closed (KILLED) inline and its reservation released; the
#               caller falls back to a classic cold review (the lane treatment is
#               over) — the visible exhaustion reason lives on the lane row.
CHECKPOINT_PHASE_OK = "ok"
CHECKPOINT_PHASE_RECOVER = "recover"
CHECKPOINT_PHASE_EXHAUSTED = "exhausted"

# Manifest key carrying the operator-visible checkpoint-recovery state (reason,
# attempt, exhausted). Surfaced on the admin improvement-lanes row so a recovered
# or killed lane explains *why* rather than only incrementing a counter.
LANE_RECOVERY_MANIFEST_KEY = "recovery"

# Durable context keys placed on a recovery task so the rebound lane chain can
# explain itself. They are inert to warm dispatch (carry no improvement_lane_id /
# checkpoint SHA / warm-handoff pin — the whole point of recovery is that the
# warm workspace could not be trusted).
LANE_RECOVERY_REASON_KEY = "_lane_recovery_reason"
LANE_RECOVERY_ATTEMPT_KEY = "_lane_recovery_attempt"


# ── Flag gating ───────────────────────────────────────────────────────────────
def is_review_fix_lane_pilot_enabled() -> bool:
    """True when the review-fix lane pilot is active.

    Requires BOTH the substrate master switch (``improvement_lanes_enabled``)
    AND the pilot flag (``review_fix_lane_pilot_enabled``). A KILL verdict is a
    flip of the pilot flag to False, after which this returns False and every
    chain degrades to classic spawning.
    """
    from app.runtime_config_state import is_review_fix_lane_pilot_enabled as _enabled

    return _enabled()


def is_review_fix_lane_shadow_enabled() -> bool:
    """True when review-fix lanes are in checkpoint-only shadow mode."""
    from app.runtime_config_state import is_review_fix_lane_shadow_enabled as _enabled

    return _enabled()


def is_review_fix_lane_pilot_shadow_mode_enabled() -> bool:
    """True when the three-value pilot shadow mode is active (mode='shadow')."""
    from app.runtime_config_state import (
        is_review_fix_lane_pilot_shadow_mode_enabled as _enabled,
    )

    return _enabled()


def is_pilot_eligible_project(project: Any) -> bool:
    """True when *project* is in scope for the pilot (Blueprint self-repo)."""
    from app.branch_resolution import _is_blueprint_self_repo

    return bool(project is not None and _is_blueprint_self_repo(project))


# ── Intent-to-treat assignment ────────────────────────────────────────────────
def assign_lane_arm(seed: str) -> str:
    """Deterministically map a stable seed (task id) to 'lane' or 'control'.

    Reuses the repo-map holdback arm's djb2 hash technique, then splits on hash
    parity for a stable ~50/50 partition. Pure and side-effect-free: the same
    seed always yields the same arm, independent of any treatment applied.
    """
    h = 5381
    for ch in seed:
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
    return "lane" if (h & 1) == 0 else "control"


def lane_arm_of(ctx: dict | None) -> str | None:
    """Return the recorded arm ('lane'/'control') for a chain, or None."""
    if not isinstance(ctx, dict):
        return None
    arm = ctx.get(LANE_PILOT_ASSIGNMENT_KEY)
    return arm if arm in _VALID_ARMS else None


def strip_transient_lane_context(ctx: dict) -> None:
    """Remove per-phase lane dispatch directives from a spawn context.

    Called unconditionally for every spawn so a reviewer can never inherit a
    stale ``improvement_lane_id`` (which would land it on the lane-reserved VM)
    or a checkpoint SHA. A no-op on classic chains.
    """
    for key in TRANSIENT_LANE_CONTEXT_KEYS:
        ctx.pop(key, None)


def record_lane_arm_assignment(
    ctx: dict, seed_task_id: str, project: Any
) -> str | None:
    """Record the intent-to-treat arm ONCE on *ctx*; return the arm or None.

    No-op (returns None, writes nothing) when the pilot is disabled, the project
    is out of scope, or a durable KILL verdict is recorded — this is the flag-off
    and KILL-durability purity guarantee. When an assignment is already present it
    is returned unchanged (recorded once). When a durable ADOPT verdict is
    recorded, all newly-assigned chains are forced to the 'lane' arm (holdback
    drops to 0, matching get_effective_holdback_fraction adopt->0.0 semantics).
    """
    if (
        not is_review_fix_lane_pilot_enabled()
        and not is_review_fix_lane_pilot_shadow_mode_enabled()
    ) or not is_pilot_eligible_project(project):
        return None
    # KILL short-circuit: a durable KILL verdict suppresses all new assignments
    # regardless of whether the runtime flag is still True (reload survival).
    if is_review_fix_lane_killed(project):
        return None
    existing = lane_arm_of(ctx)
    if existing is not None:
        return existing
    # ADOPT override: route every new eligible chain to the lane arm so no chain
    # stays in the control arm permanently after the trial decision.
    if is_review_fix_lane_adopted(project):
        ctx[LANE_PILOT_ASSIGNMENT_KEY] = "lane"
        return "lane"
    arm = assign_lane_arm(str(seed_task_id))
    ctx[LANE_PILOT_ASSIGNMENT_KEY] = arm
    return arm


# ── Lane lookup / lazy creation ───────────────────────────────────────────────
async def get_chain_lane(session: AsyncSession, ctx: dict | None) -> Any | None:
    """Return the ImprovementLane bound to a chain via LANE_CHAIN_ID_KEY, or None.

    Returns None for lanes not in ACTIVE or PARKED status — a PROPOSED lane
    that failed activation must never be used as a valid chain lane.
    """
    from app.models import ImprovementLane, ImprovementLaneStatus

    if not isinstance(ctx, dict):
        return None
    raw = ctx.get(LANE_CHAIN_ID_KEY)
    if not raw:
        return None
    try:
        lane_id = uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None
    lane = await session.get(ImprovementLane, lane_id)
    _usable = {ImprovementLaneStatus.ACTIVE.value, ImprovementLaneStatus.PARKED.value}
    if lane is not None and lane.status not in _usable:
        return None
    return lane


async def ensure_chain_lane(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    ctx: dict,
    title: str,
    branch_name: str,
    base_branch: str,
    direction_id: str | None,
    current_task_id: uuid.UUID | None,
    pilot_arm: str | None = "lane",
) -> Any | None:
    """Look up (or lazily create+activate) the lane for a lane chain.

    The lane is created in PROPOSED then advanced to ACTIVE and bound to the
    running phase task, so the next checkpoint can CAS ACTIVE -> PARKED. The
    chain remembers its lane via ``LANE_CHAIN_ID_KEY`` in *ctx*. Full pilot uses
    ``pilot_arm="lane"``; shadow uses ``pilot_arm=None`` so trial aggregates stay
    untouched.
    """
    from app.improvement_lane_ops import cas_transition, create_lane
    from app.models import ImprovementLaneStatus

    lane = await get_chain_lane(session, ctx)
    if lane is not None and lane.pilot_arm == pilot_arm:
        return lane
    if lane is not None:
        logger.info(
            "[LANE-PILOT] chain lane %s has pilot_arm=%s but requested %s; "
            "creating a fresh lane for the new mode",
            lane.id,
            lane.pilot_arm,
            pilot_arm,
        )

    lane = await create_lane(
        session,
        project_id=project_id,
        direction_id=direction_id,
        title=title,
        goal=title,
        branch_name=branch_name,
        base_branch=base_branch,
        pilot_arm=pilot_arm,
    )
    await session.flush()  # assign lane.id
    try:
        await cas_transition(
            session,
            lane.id,
            ImprovementLaneStatus.PROPOSED,
            ImprovementLaneStatus.ACTIVE,
            current_task_id=current_task_id,
        )
    except Exception as exc:  # noqa: BLE001 - lane creation is best-effort
        logger.warning("[LANE-PILOT] could not activate lane %s: %s", lane.id, exc)
        # Best-effort cleanup: retire the orphaned PROPOSED lane so it doesn't
        # linger until the stranded-lane sweep ages it out.
        with contextlib.suppress(Exception):
            await cas_transition(
                session,
                lane.id,
                ImprovementLaneStatus.PROPOSED,
                ImprovementLaneStatus.RETIRED,
            )
        # Do NOT bind the chain to an unactivated lane — the chain falls through
        # to classic relay behavior for this phase.
        return None
    ctx[LANE_CHAIN_ID_KEY] = str(lane.id)
    return lane


# ── Bounded manifest-patch / resume-block derivation ──────────────────────────
def _clip(value: Any, limit: int = _MANIFEST_STR_MAX_CHARS) -> str:
    """Coerce *value* to a length-bounded string."""
    return str(value)[:limit]


def _bound_json_size(payload: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Trim list fields of *payload* until its JSON encoding fits *max_bytes*.

    Drops the trailing element of the currently-longest list field on each pass.
    Mutates and returns *payload*. Non-list fields are never dropped (they are
    small scalars). A defence-in-depth bound: the per-field caps usually keep the
    payload small, but a pathological set of long strings could still overrun.
    """
    while len(json.dumps(payload, default=str).encode()) > max_bytes:
        longest: str | None = None
        for key, val in payload.items():
            if (
                isinstance(val, list)
                and len(val) > 1
                and (longest is None or len(val) > len(payload[longest]))
            ):
                longest = key
        if longest is None:
            break
        payload[longest] = payload[longest][:-1]
    return payload


def build_lane_manifest_patch(source_task: Any, *, summary: str) -> dict[str, Any]:
    """Derive a compact manifest patch from a completed implement/fix phase.

    Read-only over ``source_task`` durable state the backend already holds. The
    returned patch uses ONLY the list-field keys ``_merge_manifest_patch`` knows
    how to bound (``progress``/``remaining``/``critical_files``/``failing_tests``/
    ``risks``/``phase_log``) so the existing bounded merge keeps the accumulated
    ImprovementLane.manifest under the 8KB contract. Empty-source phases yield a
    minimal single-entry ``phase_log`` patch. The result is itself size-bounded so
    the LaneCheckpointPayload 8KB validator never rejects it.
    """
    ctx = getattr(source_task, "context", None) or {}
    try:
        payload = source_task.first_payload or {}
    except Exception:  # noqa: BLE001 - payload access is best-effort
        payload = {}
    phase = str(payload.get("type") or "phase")
    review_loop = ctx.get("review_loop") or {}
    iteration = review_loop.get("iteration")
    pr_number = payload.get("pr_number") or review_loop.get("pr_number")

    patch: dict[str, Any] = {}

    # phase_log: one rolling entry describing this phase + its checkpoint summary.
    label = phase
    if pr_number:
        label += f" PR#{pr_number}"
    if iteration is not None:
        label += f" iter{iteration}"
    patch["phase_log"] = [_clip(f"{label}: {summary}")]

    # critical_files: files this phase changed, plus reviewer/investigated paths.
    files: list[str] = []
    for source in (ctx.get("_pr_changed_files"), ctx.get("investigated_files")):
        if isinstance(source, list):
            files.extend(str(f) for f in source if f)
    deduped: list[str] = []
    for f in files:
        if f not in deduped:
            deduped.append(f)
    if deduped:
        patch["critical_files"] = [
            _clip(f, 200) for f in deduped[:_MANIFEST_LIST_MAX_ITEMS]
        ]

    # failing_tests: a bounded test-failure / CI-failure summary string, if any.
    tf_summary = ctx.get("_test_failure_summary") or ctx.get("_ci_failure_logs")
    if isinstance(tf_summary, str) and tf_summary.strip():
        patch["failing_tests"] = [_clip(tf_summary)]

    # remaining: the open review issues this phase was addressing (latest entry).
    history = review_loop.get("review_history")
    latest_issues: list[Any] = []
    if isinstance(history, list) and history and isinstance(history[-1], dict):
        latest_issues = history[-1].get("issues") or []
    remaining: list[str] = []
    for issue in latest_issues:
        if isinstance(issue, dict):
            sev = str(issue.get("severity") or "").upper()
            loc = f" [{issue.get('file')}]" if issue.get("file") else ""
            remaining.append(_clip(f"[{sev}]{loc} {issue.get('description') or ''}"))
    if remaining:
        patch["remaining"] = remaining[:_MANIFEST_LIST_MAX_ITEMS]

    # risks: fix-agent disputed/deferred responses (claims + known risks).
    responses = review_loop.get("fix_agent_responses") or []
    risks: list[str] = []
    for r in responses:
        if isinstance(r, dict):
            claim = str(r.get("claim") or r.get("issue_summary") or "")
            if claim:
                rtype = str(r.get("response_type") or "")
                risks.append(_clip(f"{rtype}: {claim}" if rtype else claim))
    if risks:
        patch["risks"] = risks[:_MANIFEST_LIST_MAX_ITEMS]

    return _bound_json_size(patch, _MANIFEST_PATCH_MAX_BYTES)


def build_fix_resume_block(lane: Any) -> dict[str, Any] | None:
    """Build a bounded transient lane-resume block for a FIX spawn.

    Derived from the lane's accumulated manifest + last checkpoint pointers.
    Returns None when there is nothing useful to resume from (no manifest content
    and no checkpoint SHA) so callers attach the key only when it carries signal.
    The block is size-bounded so it never bloats the fix task context.
    """
    if lane is None:
        return None
    manifest = getattr(lane, "manifest", None) or {}
    last_sha = getattr(lane, "last_checkpoint_sha", None)
    last_phase = getattr(lane, "last_checkpoint_phase", None)
    seq = getattr(lane, "checkpoint_seq", 0) or 0

    block: dict[str, Any] = {}
    for field in (
        "phase_log",
        "remaining",
        "critical_files",
        "failing_tests",
        "risks",
        "progress",
    ):
        value = manifest.get(field) if isinstance(manifest, dict) else None
        if isinstance(value, list) and value:
            block[field] = [_clip(x) for x in value[-_RESUME_LIST_MAX_ITEMS:]]

    # Nothing to resume from: no accumulated manifest content and no checkpoint.
    if not block and not last_sha:
        return None

    block["checkpoint_seq"] = int(seq)
    if last_sha:
        block["last_checkpoint_sha"] = _clip(last_sha, 64)
    if last_phase:
        block["last_phase"] = _clip(last_phase, 40)
    return _bound_json_size(block, _RESUME_BLOCK_MAX_BYTES)


# ── Phase-completion checkpoint + warm reservation (implement/fix -> review) ───
async def checkpoint_and_reserve_on_phase_complete(
    session: AsyncSession,
    source_task: Any,
    lane: Any,
    *,
    sha: str | None,
    summary: str,
    reserve_workspace: bool = True,
    record_miss_marker: bool = False,
) -> bool:
    """Checkpoint the lane and optionally move its workspace to WARM_RESERVED.

    Called when a lane-arm implement/fix phase completes and a COLD review is
    about to spawn. Appends a verified checkpoint (CAS ACTIVE -> PARKED) and
    in full-pilot mode reserves the just-freed warm-pool member to the lane so
    the next fix phase can resume warm. Shadow passes ``reserve_workspace=False``
    to exercise checkpointing while leaving workspace_state COLD. Returns True
    when a checkpoint was written.

    A missing/invalid SHA degrades to ``mark_checkpoint_missing`` rather than
    fabricating a checkpoint. The bounded recovery (suppress the review, requeue
    the failed phase, or close the lane when exhausted) is driven inline by the
    review-spawn path via ``checkpoint_or_recover_phase`` — not deferred to a
    sweep. This function still returns a bare ``bool`` (True iff a checkpoint was
    written) so existing callers/tests are unchanged; the recovery classifier
    wraps it.
    """
    from app.improvement_lane_ops import (
        LaneCheckpointError,
        LaneOwnerMismatchError,
        checkpoint_lane,
        mark_checkpoint_missing,
    )
    from app.models import (
        ImprovementLaneStatus,
        get_task_total_llm_totals,
        get_task_total_vm_cost_usd,
    )
    from app.payload_schemas import LaneCheckpointPayload

    if lane is None:
        return False

    # Only the ACTIVE phase checkpoints. A lane already PARKED for this phase
    # (e.g. a re-review spawned twice) is a no-op, not a loss — do not requeue.
    if lane.status != ImprovementLaneStatus.ACTIVE.value:
        return False

    current_task_id = getattr(lane, "current_task_id", None)
    if current_task_id != source_task.id:
        logger.info(
            "[LANE-PILOT] stale checkpoint skipped for lane %s by non-owner task %s",
            lane.id,
            source_task.id,
        )
        raise LaneOwnerMismatchError(
            f"Lane {lane.id} is owned by task {current_task_id}, "
            f"but checkpoint was attempted by task {source_task.id}"
        )

    if not sha or len(str(sha)) < 7:
        with contextlib.suppress(LaneCheckpointError):
            missed_lane, _ = await mark_checkpoint_missing(
                session, lane.id, reason="no_phase_sha"
            )
            if record_miss_marker:
                _record_lane_recovery_state(
                    missed_lane,
                    reason="no_phase_sha",
                    attempt=missed_lane.phase_requeues or 0,
                    exhausted=False,
                )
        return False

    # Verify the SHA is actually reachable on the lane branch before recording
    # it as the durable checkpoint. An unverified SHA causes the agent's warm-
    # resume hard-reset to fail and silently degrades to a cold clone —
    # defeating the measurement. Policy: confirmed False (SHA not reachable)
    # calls mark_checkpoint_missing so the sweep can requeue. Transient/
    # indeterminate outcomes (exception or None) return False WITHOUT calling
    # mark_checkpoint_missing — a transient hiccup must not burn the
    # phase_requeues budget; the review spawn path is unaffected either way.
    try:
        from app.github_refs import is_ancestor_of
        from app.github_token_resolver import get_github_token_for_task

        gh_token, gh_owner, gh_repo = await get_github_token_for_task(
            source_task.id, session
        )
        reachable = await is_ancestor_of(
            gh_token,
            gh_owner,
            gh_repo,
            str(sha),
            lane.branch_name,
            project_id=str(source_task.project_id),
        )
    except Exception as exc:  # noqa: BLE001 - verification is best-effort
        logger.warning(
            "[LANE-PILOT] SHA verification unavailable for lane %s sha=%s: %s — "
            "skipping checkpoint without burning requeue budget",
            lane.id,
            str(sha)[:8],
            exc,
        )
        return False

    if reachable is False:
        logger.warning(
            "[LANE-PILOT] SHA %s not reachable on branch %s for lane %s — "
            "skipping checkpoint",
            str(sha)[:8],
            lane.branch_name,
            lane.id,
        )
        with contextlib.suppress(LaneCheckpointError):
            missed_lane, _ = await mark_checkpoint_missing(
                session, lane.id, reason="sha_not_reachable"
            )
            if record_miss_marker:
                _record_lane_recovery_state(
                    missed_lane,
                    reason="sha_not_reachable",
                    attempt=missed_lane.phase_requeues or 0,
                    exhausted=False,
                )
        return False

    if reachable is None:
        # Transient GitHub API failure (rate limit, network, credentials) — do
        # NOT call mark_checkpoint_missing, which would burn a phase_requeues
        # budget slot.  Three transient failures must not kill a lane that has
        # no genuine SHA-reachability problem.  The checkpoint is simply skipped
        # for this phase; the lane stays ACTIVE and can be retried.
        logger.warning(
            "[LANE-PILOT] SHA reachability indeterminate (transient) for lane %s "
            "sha=%s — skipping checkpoint without consuming requeue budget",
            lane.id,
            str(sha)[:8],
        )
        return False

    _tokens_in, _tokens_out, _ = get_task_total_llm_totals(source_task)
    _vm_cost_usd = get_task_total_vm_cost_usd(source_task)
    payload = LaneCheckpointPayload(
        sha=str(sha),
        summary=summary[:1024],
        warm_resume=bool(
            (getattr(source_task, "context", None) or {}).get(
                LANE_PILOT_WARM_RESUME_RECEIPT_KEY
            )
        ),
        # Derive a compact manifest patch from the just-completed phase's durable
        # state so ImprovementLane.manifest accumulates chain knowledge across
        # phases via the existing bounded merge (never exceeding the 8KB cap).
        manifest_patch=build_lane_manifest_patch(source_task, summary=summary),
        tokens_in=_tokens_in,
        tokens_out=_tokens_out,
        vm_cost_usd=_vm_cost_usd,
    )
    try:
        await checkpoint_lane(
            session,
            lane.id,
            phase=str(source_task.first_payload.get("type", "")) or "phase",
            task_id=source_task.id,
            checkpoint=payload,
        )
    except LaneOwnerMismatchError:
        logger.info(
            "[LANE-PILOT] stale checkpoint skipped for lane %s by non-owner task %s",
            lane.id,
            source_task.id,
        )
        raise
    except LaneCheckpointError as exc:
        logger.warning("[LANE-PILOT] checkpoint failed for lane %s: %s", lane.id, exc)
        with contextlib.suppress(LaneCheckpointError):
            missed_lane, _ = await mark_checkpoint_missing(
                session, lane.id, reason="checkpoint_error"
            )
            if record_miss_marker:
                _record_lane_recovery_state(
                    missed_lane,
                    reason="checkpoint_error",
                    attempt=missed_lane.phase_requeues or 0,
                    exhausted=False,
                )
        return False

    # Reserve the freed warm-pool member to the lane (best-effort: only engages
    # when the source ran on a pool member). The lane's workspace_state mirrors
    # the reservation so operators see WARM_RESERVED on the lane row.
    # Shadow-arm lanes never take warm reservations — they are observational only.
    if getattr(lane, "pilot_arm", None) == _SHADOW_ARM or not reserve_workspace:
        return True
    vm_id = getattr(source_task, "vm_instance_id", None)
    if vm_id:
        try:
            from app.warm_pool import release_pool_vm

            member = await release_pool_vm(
                session,
                vm_id,
                owning_task_id=source_task.id,
                lane_id=str(lane.id),
            )
            if member is not None:
                lane.workspace_state = WORKSPACE_WARM_RESERVED
                lane.workspace_vm_instance_id = vm_id
                session.add(lane)
        except Exception as exc:  # noqa: BLE001 - reservation is best-effort
            logger.warning(
                "[LANE-PILOT] warm reservation failed for lane %s vm=%s: %s",
                lane.id,
                vm_id,
                exc,
            )
    return True


# ── Checkpoint-miss bounded recovery ──────────────────────────────────────────
def _record_lane_recovery_state(
    lane: Any, *, reason: str, attempt: int, exhausted: bool
) -> None:
    """Stamp an operator-visible recovery marker into the lane manifest.

    Written on BOTH the under-budget requeue and the budget-exhausted close so
    the admin lane view shows *why* a checkpoint-missing lane requeued or was
    killed, not just an incremented ``phase_requeues`` counter. Reassigns
    ``lane.manifest`` (rather than mutating in place) so SQLAlchemy persists the
    JSONB change. The marker is a small scalar field — the bounded list-field
    merge/resume logic never touches it.
    """
    manifest = dict(lane.manifest or {})
    manifest[LANE_RECOVERY_MANIFEST_KEY] = {
        "reason": str(reason)[:200],
        "attempt": int(attempt),
        "exhausted": bool(exhausted),
        "at": _utcnow().isoformat(),
    }
    lane.manifest = manifest


async def close_lane_checkpoint_exhausted(
    session: AsyncSession, lane: Any, *, reason: str
) -> bool:
    """CAS a checkpoint-recovery-exhausted lane to KILLED and free its reservation.

    Called when ``phase_requeues`` has crossed the budget so the lane reaches a
    terminal state inline (rather than idling ACTIVE until the exhausted-budget
    sweep) and any warm-pool member still reserved for it rejoins the general
    pool immediately. Records a visible exhaustion reason in the manifest.
    Best-effort: a non-ACTIVE lane is left unchanged. Returns True when closed.
    """
    from app.improvement_lane_ops import LaneCASError, cas_transition
    from app.models import ImprovementLaneStatus, PoolVm

    if lane is None or lane.status != ImprovementLaneStatus.ACTIVE.value:
        return False
    try:
        await cas_transition(
            session,
            lane.id,
            ImprovementLaneStatus.ACTIVE,
            ImprovementLaneStatus.KILLED,
        )
    except LaneCASError as exc:
        logger.warning(
            "[LANE-PILOT] could not KILL checkpoint-exhausted lane %s: %s",
            lane.id,
            exc,
        )
        return False
    _record_lane_recovery_state(
        lane, reason=reason, attempt=lane.phase_requeues or 0, exhausted=True
    )
    lane.workspace_state = WORKSPACE_COLD
    lane.workspace_vm_instance_id = None
    reserved = (
        (
            await session.execute(
                select(PoolVm).where(PoolVm.reserved_lane_id == lane.id)
            )
        )
        .scalars()
        .all()
    )
    from app.warm_pool import record_lane_reservation_cause

    for member in reserved:
        member.reserved_lane_id = None
        member.reservation_expires_at = None
        member.reservation_hard_expires_at = None
        session.add(member)
        await record_lane_reservation_cause(session, lane.id, "force_expired")
    session.add(lane)
    logger.warning(
        "[LANE-PILOT] lane %s KILLED — checkpoint recovery exhausted "
        "(phase_requeues=%d) reason=%s",
        lane.id,
        lane.phase_requeues or 0,
        reason,
    )
    lane_title = getattr(lane, "title", None) or str(lane.id)
    await emit_lane_event_message(
        session,
        lane.project_id,
        lane.id,
        "lane_killed_exhausted",
        f"lane '{lane_title}' KILLED — checkpoint recovery exhausted "
        f"(phase_requeues={lane.phase_requeues or 0}): {reason}",
    )
    return True


async def checkpoint_or_recover_phase(
    session: AsyncSession,
    source_task: Any,
    lane: Any,
    *,
    sha: str | None,
    summary: str,
    reserve_workspace: bool = True,
    enable_recovery: bool = True,
    record_miss_marker: bool = False,
) -> str:
    """Checkpoint a completed lane phase, classifying a MISS into bounded recovery.

    Wraps :func:`checkpoint_and_reserve_on_phase_complete` (whose bare-``bool``
    return is preserved for existing callers/tests) and inspects the lane to
    decide what the review-spawn path should do — replacing the old behavior
    where a missing/unverifiable checkpoint silently let a cold review proceed
    and only showed up as a ``phase_requeues`` increment.

    Returns one of ``CHECKPOINT_PHASE_OK`` / ``CHECKPOINT_PHASE_RECOVER`` /
    ``CHECKPOINT_PHASE_EXHAUSTED``:

    * checkpoint written, or a benign no-op (lane None / already PARKED) → OK.
    * checkpoint MISSING (lane still ACTIVE, ``phase_requeues`` bumped by
      ``mark_checkpoint_missing``) and budget intact → RECOVER.
    * checkpoint MISSING and budget exhausted → the lane is closed inline via
      :func:`close_lane_checkpoint_exhausted` and EXHAUSTED is returned.
    """
    from app.improvement_lane_ops import LaneOwnerMismatchError
    from app.models import ImprovementLaneStatus, _LANE_MAX_PHASE_REQUEUES

    try:
        written = await checkpoint_and_reserve_on_phase_complete(
            session,
            source_task,
            lane,
            sha=sha,
            summary=summary,
            reserve_workspace=reserve_workspace,
            record_miss_marker=record_miss_marker,
        )
    except LaneOwnerMismatchError:
        if record_miss_marker and lane is not None:
            _record_lane_recovery_state(
                lane,
                reason="owner_mismatch",
                attempt=lane.phase_requeues or 0,
                exhausted=False,
            )
            session.add(lane)
        return CHECKPOINT_PHASE_OK
    if written or lane is None:
        return CHECKPOINT_PHASE_OK
    # ``checkpoint_lane`` advances a written checkpoint to PARKED; a lane still
    # ACTIVE here means ``mark_checkpoint_missing`` recorded a miss. The other
    # False paths (lane None handled above, or an already-PARKED no-op) leave a
    # non-ACTIVE lane and must NOT trigger recovery.
    if lane.status != ImprovementLaneStatus.ACTIVE.value:
        return CHECKPOINT_PHASE_OK
    if not enable_recovery:
        return CHECKPOINT_PHASE_OK
    if (lane.phase_requeues or 0) > _LANE_MAX_PHASE_REQUEUES:
        await close_lane_checkpoint_exhausted(
            session, lane, reason="checkpoint_recovery_exhausted"
        )
        return CHECKPOINT_PHASE_EXHAUSTED
    lane_title = getattr(lane, "title", None) or str(lane.id)
    await emit_lane_event_message(
        session,
        lane.project_id,
        lane.id,
        "lane_checkpoint_recover",
        f"lane '{lane_title}' checkpoint miss — recovery queued "
        f"(phase_requeues={lane.phase_requeues or 0})",
    )
    if (lane.phase_requeues or 0) == _LANE_MAX_PHASE_REQUEUES - 1:
        await emit_lane_event_message(
            session,
            lane.project_id,
            lane.id,
            "lane_budget_near_exhausted",
            f"lane '{lane_title}' budget near exhausted — next checkpoint miss will kill this lane "
            f"(phase_requeues={lane.phase_requeues or 0}/{_LANE_MAX_PHASE_REQUEUES})",
        )
    return CHECKPOINT_PHASE_RECOVER


async def rebind_lane_to_recovery(
    session: AsyncSession, lane: Any, *, task_id: uuid.UUID, reason: str
) -> None:
    """Rebind an ACTIVE lane to a freshly-spawned checkpoint-recovery task.

    The lane stays ACTIVE (the failed phase is being retried, not parked) and its
    ``current_task_id`` points at the recovery task so chain continuity and the
    operator lane view follow the retry. The workspace drops to COLD — the
    recovery runs from a fresh clone because the warm checkpoint could not be
    trusted. Records a visible recovery marker in the manifest. Best-effort: a
    non-ACTIVE lane is left unchanged.
    """
    from app.models import ImprovementLaneStatus

    from app.models import PoolVm

    if lane is None or lane.status != ImprovementLaneStatus.ACTIVE.value:
        return
    lane.current_task_id = task_id
    lane.workspace_state = WORKSPACE_COLD
    _record_lane_recovery_state(
        lane, reason=reason, attempt=lane.phase_requeues or 0, exhausted=False
    )
    session.add(lane)
    reserved = (
        (
            await session.execute(
                select(PoolVm).where(PoolVm.reserved_lane_id == lane.id)
            )
        )
        .scalars()
        .all()
    )
    from app.warm_pool import record_lane_reservation_cause

    for member in reserved:
        member.reserved_lane_id = None
        member.reservation_expires_at = None
        member.reservation_hard_expires_at = None
        session.add(member)
        await record_lane_reservation_cause(session, lane.id, "force_expired")


# ── Fix-side warm resume wiring ───────────────────────────────────────────────
async def activate_lane_for_fix(
    session: AsyncSession,
    lane: Any,
    *,
    task_id: uuid.UUID | None = None,
    lease_workspace: bool = True,
) -> None:
    """Resume a PARKED lane to ACTIVE for the fix phase (best-effort).

    Binds the fix to the lane and, in full-pilot mode, flips the workspace to
    LEASED so the next re-review boundary can checkpoint ACTIVE -> PARKED again.
    Shadow passes ``lease_workspace=False`` to exercise CAS without changing
    dispatch/workspace state. A lane already ACTIVE (or in a terminal state) is
    left unchanged.
    """
    from app.improvement_lane_ops import LaneCASError, cas_transition
    from app.models import ImprovementLaneStatus, PoolVm
    from app.shared_state import runtime_config as _runtime_config

    if lane is None or lane.status != ImprovementLaneStatus.PARKED.value:
        return
    try:
        await cas_transition(
            session,
            lane.id,
            ImprovementLaneStatus.PARKED,
            ImprovementLaneStatus.ACTIVE,
            current_task_id=task_id,
        )
        if lease_workspace:
            lane.workspace_state = WORKSPACE_LEASED
            session.add(lane)
    except LaneCASError as exc:
        logger.warning(
            "[LANE-PILOT] could not resume lane %s for fix: %s", lane.id, exc
        )
        return

    # Re-stamp the soft TTL on the reserved PoolVm so the fix task has a full
    # TTL window to claim it. The Pass 1 sweep only extends while the lane is
    # PARKED; once we flip to ACTIVE here the sweep stops. Without re-stamping,
    # a lane that waited a long time in PARKED may have an expired soft TTL,
    # causing the fix task's warm-claim to fail silently.
    member = (
        (
            await session.execute(
                select(PoolVm).where(PoolVm.reserved_lane_id == lane.id).limit(1)
            )
        )
        .scalars()
        .first()
    )
    if member is not None and member.reservation_hard_expires_at is not None:
        now = _utcnow()
        hard_anchor = member.reservation_hard_expires_at
        if hard_anchor > now:
            soft_ttl = int(
                _runtime_config.get("warm_pool_lane_reservation_ttl_seconds", 1800)
                or 1800
            )
            member.reservation_expires_at = min(
                now + timedelta(seconds=soft_ttl), hard_anchor
            )
            session.add(member)
            logger.debug(
                "[LANE-PILOT] re-stamped reservation soft TTL vm=%s lane=%s -> %s "
                "(hard ceiling %s)",
                member.vm_instance_id,
                lane.id,
                member.reservation_expires_at,
                hard_anchor,
            )


def wire_fix_lane_context(ctx: dict, lane: Any) -> None:
    """Attach lane warm-resume directives to a FIX spawn context.

    Sets ``improvement_lane_id`` (so the warm-pool dispatch hook leases the
    lane-reserved member), ``is_lane_phase`` + ``lane_checkpoint_sha`` (so the
    agent fetch+resets to the checkpoint, skipping clone/install), and the
    bounded ``lane_resume`` block (accumulated chain knowledge the agent renders
    into its fix prompt). Receipts are intentionally NOT stamped here — they
    record actual warm engagement and are written only by
    ``confirm_lane_warm_resume`` at the warm-claim site.
    """
    if lane is None:
        return
    ctx["improvement_lane_id"] = str(lane.id)
    ctx["is_lane_phase"] = True
    if lane.last_checkpoint_sha:
        ctx["lane_checkpoint_sha"] = str(lane.last_checkpoint_sha)
    # Attach the bounded transient lane-resume block (accumulated chain knowledge)
    # so the fix agent starts from the lane's manifest/checkpoint instead of cold.
    # Only ever set here (fix-side) and stripped from every review/audit spawn via
    # TRANSIENT_LANE_CONTEXT_KEYS — gate integrity. Omitted when there is no
    # accumulated signal to resume from.
    resume_block = build_fix_resume_block(lane)
    if resume_block is not None:
        ctx[LANE_RESUME_CONTEXT_KEY] = resume_block
    else:
        ctx.pop(LANE_RESUME_CONTEXT_KEY, None)


def confirm_lane_warm_resume(ctx: dict, *, has_checkpoint: bool) -> None:
    """Stamp warm-resume and checkpoint USE receipts on a task context.

    Called ONLY at the warm-claim site (``_finalize_warm_claim``) when a
    lane-phase task is actually claimed warm — never at spawn time. Receipts
    feed the purity gate and the lane checkpoint ``warm_resume`` field; they
    must reflect what actually happened, not what was intended at spawn.
    ``has_checkpoint`` should be True when the task's context had a
    ``lane_checkpoint_sha`` (i.e. the checkpoint was available for the agent
    to reset to).
    """
    ctx[LANE_PILOT_WARM_RESUME_RECEIPT_KEY] = True
    ctx[LANE_PILOT_CHECKPOINT_RECEIPT_KEY] = has_checkpoint


def record_workspace_prep_outcome(
    ctx: dict,
    workspace_prep: dict[str, Any],
    *,
    task_type: str | None = None,
) -> None:
    """Persist the agent's actual workspace-prep outcome and reconcile receipts."""
    ctx[WORKSPACE_PREP_CONTEXT_KEY] = workspace_prep
    if not (bool(ctx.get("is_lane_phase")) and task_type in {"fix", "implement"}):
        return

    warm_reuse_succeeded = workspace_prep.get("path") == "warm" and bool(
        workspace_prep.get("warm_reuse_succeeded")
    )
    if not warm_reuse_succeeded:
        ctx.pop(LANE_PILOT_WARM_RESUME_RECEIPT_KEY, None)
        ctx.pop(LANE_PILOT_CHECKPOINT_RECEIPT_KEY, None)
        return

    ctx[LANE_PILOT_WARM_RESUME_RECEIPT_KEY] = True
    if bool(workspace_prep.get("checkpoint_reset_applied")):
        ctx[LANE_PILOT_CHECKPOINT_RECEIPT_KEY] = True
    else:
        ctx.pop(LANE_PILOT_CHECKPOINT_RECEIPT_KEY, None)


# ── KILL mechanics ────────────────────────────────────────────────────────────
async def force_expire_lane_reservations(
    session: AsyncSession, project_id: uuid.UUID
) -> int:
    """Force-expire every warm-pool reservation held for one project's lanes.

    Invoked on a KILL flag-flip so reserved members rejoin the general pool
    immediately instead of idling until TTL. Returns the count cleared.
    """
    from app.models import ImprovementLane, PoolVm

    lane_ids = (
        (
            await session.execute(
                select(ImprovementLane.id).where(
                    ImprovementLane.project_id == project_id
                )
            )
        )
        .scalars()
        .all()
    )
    if not lane_ids:
        return 0

    members = (
        (
            await session.execute(
                select(PoolVm).where(PoolVm.reserved_lane_id.in_(lane_ids))
            )
        )
        .scalars()
        .all()
    )
    from app.warm_pool import record_lane_reservation_cause

    cleared = 0
    for member in members:
        cause_lane_id = member.reserved_lane_id
        member.reserved_lane_id = None
        member.reservation_expires_at = None
        member.reservation_hard_expires_at = None
        session.add(member)
        if cause_lane_id is not None:
            await record_lane_reservation_cause(session, cause_lane_id, "force_expired")
        cleared += 1
    if cleared:
        logger.info(
            "[LANE-PILOT] KILL force-expired %d lane reservation(s) for project %s",
            cleared,
            project_id,
        )
    return cleared


async def retire_chain_lane(
    session: AsyncSession,
    ctx: dict | None,
) -> bool:
    """CAS the chain's lane to RETIRED if it is not already terminal.

    Transitions ACTIVE, PARKED, or PROPOSED lanes to RETIRED.  PROPOSED->RETIRED
    is now a valid transition (added alongside the stranded-lane reconciliation
    sweep), so a chain whose PR merges/closes before the lane ever activated is
    closed cleanly instead of the CAS being swallowed.

    Called when a chain's PR merges or closes so the lane row reaches a
    terminal state promptly.  Best-effort: LaneCASError (concurrent retirement
    by another worker) is suppressed.  Returns True when a transition was made.
    """
    from app.improvement_lane_ops import (
        LaneCASError,
        _TERMINAL_STATUSES,
        cas_transition,
    )
    from app.models import ImprovementLaneStatus, PoolVm

    lane = await get_chain_lane(session, ctx)
    if lane is None:
        return False
    if lane.status in _TERMINAL_STATUSES:
        return False
    try:
        current = ImprovementLaneStatus(lane.status)
        await cas_transition(session, lane.id, current, ImprovementLaneStatus.RETIRED)
        logger.info("[LANE-PILOT] lane %s -> RETIRED (chain terminal)", lane.id)
        # Release any warm-pool member still reserved for this lane so it
        # rejoins the general pool immediately rather than waiting for TTL.
        reserved_members = (
            (
                await session.execute(
                    select(PoolVm).where(PoolVm.reserved_lane_id == lane.id)
                )
            )
            .scalars()
            .all()
        )
        from app.warm_pool import record_lane_reservation_cause

        for member in reserved_members:
            member.reserved_lane_id = None
            member.reservation_expires_at = None
            member.reservation_hard_expires_at = None
            session.add(member)
            await record_lane_reservation_cause(session, lane.id, "force_expired")
        return True
    except LaneCASError as exc:
        logger.debug(
            "[LANE-PILOT] retire_chain_lane CAS miss lane=%s: %s", lane.id, exc
        )
        return False


async def kill_project_lanes(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """CAS non-terminal ImprovementLane rows for *project_id* to KILLED.

    Transitions ACTIVE, PARKED, and PROPOSED lanes to KILLED.  PROPOSED->KILLED
    is now a valid transition (added alongside the stranded-lane reconciliation
    sweep), so a PROPOSED row whose activation never completed is closed by the
    KILL operation instead of being silently skipped on a CAS miss.

    Returns the count of lanes successfully transitioned.  Called from
    apply_lane_pilot_kill so the KILL operation closes all lane rows rather
    than leaving them ACTIVE/PARKED after the pilot flag is disabled.
    """
    from app.improvement_lane_ops import (
        LaneCASError,
        _TERMINAL_STATUSES,
        cas_transition,
    )
    from app.models import ImprovementLane, ImprovementLaneStatus

    rows = (
        (
            await session.execute(
                select(ImprovementLane).where(
                    ImprovementLane.project_id == project_id,
                    ImprovementLane.status.not_in(list(_TERMINAL_STATUSES)),
                )
            )
        )
        .scalars()
        .all()
    )

    killed = 0
    for lane in rows:
        try:
            current = ImprovementLaneStatus(lane.status)
            await cas_transition(
                session, lane.id, current, ImprovementLaneStatus.KILLED
            )
            killed += 1
        except LaneCASError as exc:  # noqa: PERF203 — CAS miss per iteration is intentional
            logger.debug(
                "[LANE-PILOT] kill_project_lanes CAS miss lane=%s: %s", lane.id, exc
            )
    if killed:
        logger.info(
            "[LANE-PILOT] KILL closed %d lane(s) for project %s", killed, project_id
        )
    return killed


async def kill_single_lane(
    session: AsyncSession,
    lane_id: uuid.UUID,
) -> dict:
    """Kill a single stuck lane and release its warm-pool reservation.

    Transitions the lane from ACTIVE, PARKED, or PROPOSED to KILLED, and
    force-expires any PoolVm reservation held for this lane.  Best-effort:
    LaneCASError (concurrent kill by another worker) is logged and ignored.
    Idempotent: returns zero counts if the lane is already terminal.

    Returns a dict with lanes_killed and reservations_cleared counts.
    """
    from app.improvement_lane_ops import (
        LaneCASError,
        _TERMINAL_STATUSES,
        cas_transition,
    )
    from app.models import ImprovementLane, ImprovementLaneStatus, PoolVm

    lane = await session.get(ImprovementLane, lane_id)
    if lane is None or lane.status in _TERMINAL_STATUSES:
        return {"lanes_killed": 0, "reservations_cleared": 0}

    try:
        current = ImprovementLaneStatus(lane.status)
        await cas_transition(session, lane.id, current, ImprovementLaneStatus.KILLED)
    except LaneCASError:
        logger.debug("[LANE-PILOT] kill_single_lane CAS miss lane=%s", lane_id)
        return {"lanes_killed": 0, "reservations_cleared": 0}

    # Release any warm-pool member reserved for this lane.
    reserved_members = (
        (
            await session.execute(
                select(PoolVm).where(PoolVm.reserved_lane_id == lane.id)
            )
        )
        .scalars()
        .all()
    )
    from app.warm_pool import record_lane_reservation_cause

    cleared = 0
    for member in reserved_members:
        member.reserved_lane_id = None
        member.reservation_expires_at = None
        member.reservation_hard_expires_at = None
        session.add(member)
        await record_lane_reservation_cause(session, lane_id, "force_expired")
        cleared += 1

    logger.info(
        "[LANE-PILOT] KILL lane %s (operator) killed=1 reservations=%d",
        lane_id,
        cleared,
    )
    return {"lanes_killed": 1, "reservations_cleared": cleared}


async def apply_lane_pilot_kill(
    session: AsyncSession, project_id: uuid.UUID, *, reason: str
) -> dict:
    """Degrade the pilot to classic spawning: flip the flag off + free members.

    The flag flip makes ``is_review_fix_lane_pilot_enabled`` return False so all
    subsequent spawns run classic; force-expiring reservations releases members
    held for in-flight lanes; kill_project_lanes closes the lane rows so they
    reach KILLED rather than leaking as ACTIVE/PARKED. Idempotent.
    """
    from app.shared_state import runtime_config

    runtime_config["review_fix_lane_pilot_enabled"] = False
    cleared = await force_expire_lane_reservations(session, project_id)
    lanes_killed = await kill_project_lanes(session, project_id)
    logger.warning(
        "[LANE-PILOT] KILL applied for project %s reason=%s "
        "reservations_cleared=%d lanes_killed=%d",
        project_id,
        reason,
        cleared,
        lanes_killed,
    )
    await emit_lane_event_message(
        session,
        project_id,
        None,
        "pilot_auto_disabled",
        f"lane-pilot auto-disabled for project {project_id}: {reason}",
    )
    return {
        "flag_disabled": True,
        "reservations_cleared": cleared,
        "lanes_killed": lanes_killed,
        "reason": reason,
    }


async def apply_lane_pilot_kill_in_transaction(
    source_session: AsyncSession,
    project_id: uuid.UUID,
    *,
    verdict_entry: dict[str, Any],
    reason: str,
    alert_detail: str | None = None,
) -> dict:
    """Persist a lane-pilot KILL in an explicit write-owned transaction.

    Digest computation runs on read/request sessions that may be rolled back by
    callers after the response is computed.  KILL is a safety control, so it
    must not depend on that outer session committing.
    """
    from app.capability_trial_verdict import record_trial_verdict
    from app.database import resolve_async_engine
    from app.models import Project

    factory = async_sessionmaker(
        resolve_async_engine(source_session),
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with factory() as session:
        try:
            project = (
                await session.execute(
                    select(Project).where(Project.id == project_id).with_for_update()
                )
            ).scalars().first()
            if project is None:
                raise ValueError(f"Project {project_id} not found for lane-pilot KILL")

            prior = (
                project.capability_adoption_verdicts.get(LANE_PILOT_CAP_NAME)
                if isinstance(project.capability_adoption_verdicts, dict)
                else None
            )
            if not (isinstance(prior, dict) and prior.get("verdict") == "KILL"):
                record_trial_verdict(project, LANE_PILOT_CAP_NAME, verdict_entry)

            result = await apply_lane_pilot_kill(session, project_id, reason=reason)
            if alert_detail is not None:
                await raise_gate_integrity_alert(
                    session, project_id, detail=alert_detail
                )
            await session.commit()
            return result
        except Exception:
            await session.rollback()
            raise


async def emit_lane_event_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    lane_id: uuid.UUID | None,
    event_type: str,
    detail: str,
) -> None:
    """Post a deduplicated WARNING operator message for a lane terminal/degraded event.

    Dedupe key is ``lane_<event_type>_<lane_id>`` for per-lane events and
    ``pilot_<event_type>_<project_id>`` for project-level pilot events, so each
    lane emits at most one open message per event type and the board never floods.
    """
    from app.message_audience import stamp_message_audience
    from app.models import MessageType, ProjectMessage
    from app.project_message_creation import build_project_message

    if lane_id is not None:
        dedupe_key = f"lane_{event_type}_{lane_id}"
    else:
        dedupe_key = f"pilot_{event_type}_{project_id}"

    result = await session.execute(
        select(ProjectMessage.id)
        .where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.status == "open",
            ProjectMessage.context["dedupe_key"].as_string() == dedupe_key,
        )
        .limit(1)
    )
    already = result.scalar() is not None
    if already:
        return
    session.add(
        await build_project_message(
            session,
            project_id=project_id,
            message_type=MessageType.NOTE,
            from_type="system",
            content=f"[LANE-PILOT] {detail}",
            context=stamp_message_audience(
                {
                    "lane_id": str(lane_id) if lane_id is not None else None,
                    "dedupe_key": dedupe_key,
                },
                event_type,
            ),
        )
    )


async def raise_gate_integrity_alert(
    session: AsyncSession, project_id: uuid.UUID, detail: str
) -> None:
    """Raise an immediate operator alert for a gate-integrity violation.

    Posts a BLOCKER ProjectMessage regardless of trial sample size (gate
    integrity is non-negotiable). Deduplicated on the violation event type so
    repeated detections in a sweep don't spam the board.
    """
    from app.message_audience import stamp_message_audience
    from app.models import MessageType, ProjectMessage
    from app.project_message_creation import build_project_message

    dedupe_key = "review_fix_lane_gate_integrity_violation"
    result = await session.execute(
        select(ProjectMessage.id)
        .where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.status == "open",
            ProjectMessage.context["dedupe_key"].as_string() == dedupe_key,
        )
        .limit(1)
    )
    already = result.scalar() is not None
    if already:
        return
    session.add(
        await build_project_message(
            session,
            project_id=project_id,
            message_type=MessageType.BLOCKER,
            from_type="system",
            content=f"[LANE-PILOT] gate-integrity violation: {detail}",
            context=stamp_message_audience(
                {"dedupe_key": dedupe_key},
                "review_fix_lane_gate_integrity_violation",
            ),
        )
    )


def is_review_fix_lane_killed(project: Any) -> bool:
    """True when a durable KILL verdict is recorded for the pilot capability."""
    verdicts = getattr(project, "capability_adoption_verdicts", None)
    if not isinstance(verdicts, dict):
        return False
    entry = verdicts.get(LANE_PILOT_CAP_NAME)
    return isinstance(entry, dict) and entry.get("verdict") == "KILL"


def is_review_fix_lane_adopted(project: Any) -> bool:
    """True when a durable ADOPT verdict is recorded for the pilot capability."""
    verdicts = getattr(project, "capability_adoption_verdicts", None)
    if not isinstance(verdicts, dict):
        return False
    entry = verdicts.get(LANE_PILOT_CAP_NAME)
    return isinstance(entry, dict) and entry.get("verdict") == "ADOPT"


def is_review_fix_lane_pilot_active_for_project(project: Any) -> bool:
    """True when the pilot is active for *project*: enabled, eligible, and not killed.

    This is the spawn-time gate that consults BOTH the runtime flag AND the durable
    persisted verdict — so a KILL that survived a runtime_config reload is still
    honored even when ``review_fix_lane_pilot_enabled`` is still True.
    """
    return (
        is_review_fix_lane_pilot_enabled()
        and is_pilot_eligible_project(project)
        and not is_review_fix_lane_killed(project)
    )


def is_review_fix_lane_shadow_active_for_project(project: Any) -> bool:
    """True when checkpoint-only shadow lanes should attach for *project*."""
    return (
        is_review_fix_lane_shadow_enabled()
        and is_pilot_eligible_project(project)
        and not is_review_fix_lane_killed(project)
    )


def is_review_fix_lane_pilot_shadow_mode_active_for_project(project: Any) -> bool:
    """True when the three-value shadow pilot mode is active for *project*.

    Shadow pilot: arm assignment + checkpoint writing run, but no warm-pool
    reservations are made and fix tasks are not routed to lane workspaces.
    Distinct from the classic armless shadow (improvement_lanes_enabled=True,
    pilot_enabled=False) which creates lanes with pilot_arm=None.
    """
    return (
        is_review_fix_lane_pilot_shadow_mode_enabled()
        and is_pilot_eligible_project(project)
        and not is_review_fix_lane_killed(project)
    )


# ── Lane-arm time medians (feeds lane_relay_comparison in digest) ──────────────
async def compute_lane_arm_time_medians(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, Any]:
    """Compute per-PR wall-clock, active, and gap medians for lane-arm tasks.

    Queries terminal lane-arm tasks (COMPLETED/FAILED/CANCELLED), groups them
    by PR number from context["review_loop"]["pr_number"], and returns per-PR
    time medians using the same field names and definitions as
    ``_compute_relay_medians`` in lane_baseline_snapshot so the two sides can
    be compared directly.

    Returns a dict with keys: wall_clock_seconds, active_seconds, gap_seconds,
    task_count, pr_count — matching the relay_medians schema.
    """
    from sqlalchemy import func

    from app.database import resolve_dialect_name
    from app.models import Task, TaskStatus

    is_sqlite = resolve_dialect_name(session) == "sqlite"
    if is_sqlite:
        _arm_expr = func.json_extract(Task.context, f"$.{LANE_PILOT_ASSIGNMENT_KEY}")
    else:
        _arm_expr = Task.context[LANE_PILOT_ASSIGNMENT_KEY].astext

    rows = (
        await session.execute(
            select(
                Task.created_at,
                Task.started_at,
                Task.updated_at,
                Task.context,
                Task.payloads,
            ).where(
                Task.project_id == project_id,
                Task.status.in_(
                    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
                ),
                _arm_expr == "lane",
            )
        )
    ).all()

    _FIX_TASK_TYPES = frozenset({"fix", "implement"})
    _REVIEW_TASK_TYPES = frozenset({"review", "audit", "code-review"})

    def _strip_tz(dt: Any) -> Any:
        if dt is None:
            return None
        return (
            dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) is not None else dt
        )

    pr_buckets: dict[str, list[tuple]] = {}
    for created_at, started_at, updated_at, ctx, payloads in rows:
        if not isinstance(ctx, dict):
            continue
        review_loop = ctx.get("review_loop")
        if not isinstance(review_loop, dict):
            continue
        pr_number = review_loop.get("pr_number")
        if pr_number is None:
            continue
        payload_type = ""
        if isinstance(payloads, list) and payloads and isinstance(payloads[0], dict):
            payload_type = str(payloads[0].get("type") or "")
        key = str(pr_number)
        if key not in pr_buckets:
            pr_buckets[key] = []
        pr_buckets[key].append(
            (
                _strip_tz(created_at),
                _strip_tz(started_at),
                _strip_tz(updated_at),
                payload_type,
            )
        )

    if not pr_buckets:
        return {
            "wall_clock_seconds": None,
            "active_seconds": None,
            "gap_seconds": None,
            "task_count": None,
            "pr_count": 0,
            "fix_active_seconds": None,
            "review_active_seconds": None,
        }

    def _median(vals: list) -> float | None:
        if not vals:
            return None
        s = sorted(vals)
        n = len(s)
        mid = n // 2
        return s[mid] if n % 2 == 1 else (s[mid - 1] + s[mid]) / 2.0

    wc_vals: list[float] = []
    active_vals: list[float] = []
    gap_vals: list[float] = []
    count_vals: list[float] = []
    fix_active_vals: list[float] = []
    review_active_vals: list[float] = []

    for _, tasks in pr_buckets.items():
        created_times = [t[0] for t in tasks if t[0] is not None]
        updated_times = [t[2] for t in tasks if t[2] is not None]
        if not created_times or not updated_times:
            continue
        wall_clock = (max(updated_times) - min(created_times)).total_seconds()
        active = sum(
            max((t[2] - t[1]).total_seconds(), 0.0)
            for t in tasks
            if t[1] is not None and t[2] is not None
        )
        fix_active = sum(
            max((t[2] - t[1]).total_seconds(), 0.0)
            for t in tasks
            if t[1] is not None and t[2] is not None and t[3] in _FIX_TASK_TYPES
        )
        review_active = sum(
            max((t[2] - t[1]).total_seconds(), 0.0)
            for t in tasks
            if t[1] is not None and t[2] is not None and t[3] in _REVIEW_TASK_TYPES
        )
        wc_vals.append(wall_clock)
        active_vals.append(active)
        gap_vals.append(max(wall_clock - active, 0.0))
        count_vals.append(float(len(tasks)))
        fix_active_vals.append(fix_active)
        review_active_vals.append(review_active)

    return {
        "wall_clock_seconds": _median(wc_vals),
        "active_seconds": _median(active_vals),
        "gap_seconds": _median(gap_vals),
        "task_count": _median(count_vals),
        "pr_count": len(pr_buckets),
        "fix_active_seconds": _median(fix_active_vals),
        "review_active_seconds": _median(review_active_vals),
    }


# ── TrialDefinition aggregate wiring (feeds evaluate_lane_pilot_verdict) ───────
async def compute_lane_pilot_aggregates(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[str, Any]:
    """Build per-arm aggregates for ``evaluate_lane_pilot_verdict``.

    Sample counts and merge/runtime cost come from durable Task rows carrying the
    intent-to-treat arm assignment. Reserved-idle warm-pool spend is read from the
    lane row bound to each chain and folded into the lane arm's total treatment
    cost; control chains do not hold reservations, so their reserved-idle
    component is zero by construction. The returned snapshot also exposes the
    runtime vs reserved-idle split.
    Checkpoint-loss rate comes from ImprovementLane rows (phase_requeues).
    Gate-integrity violations are detected by querying for review/audit tasks that
    ACTUALLY carry transient lane context keys — a non-circular durable signal
    that is 0 under correct operation and non-zero only when the strip-transient
    guard was bypassed.
    """
    from sqlalchemy import func

    from app.database import resolve_dialect_name
    from app.models import ImprovementLane, Task, TaskStatus

    # ── Durable per-arm sample counts + merge/cost from Task rows ─────────────
    # The trial unit is the CHAIN (original PR / create task), not the individual
    # iteration task. A multi-phase chain (create → review → fix → re-review →
    # re-fix …) must contribute exactly ONE sample regardless of how many terminal
    # tasks it spawns.  Chain identity: payloads[0].original_task_id when present
    # (set on every spawned iteration task); the task's own id for the root create
    # task (which has no original_task_id).
    #
    # Per-chain aggregation rules:
    #   arm      — taken from the task row (consistent across a chain via ITT key)
    #   merged   — True if ANY task in the chain has pr_merged=True
    #   cost     — SUM across all tasks in the chain
    is_sqlite = resolve_dialect_name(session) == "sqlite"
    if is_sqlite:
        _arm_expr = func.json_extract(Task.context, f"$.{LANE_PILOT_ASSIGNMENT_KEY}")
        _merged_expr = func.json_extract(Task.context, "$.pr_merged")
        _orig_id_expr = func.json_extract(Task.payloads, "$[0].original_task_id")
        _chain_lane_id_expr = func.json_extract(Task.context, f"$.{LANE_CHAIN_ID_KEY}")
    else:
        _arm_expr = Task.context[LANE_PILOT_ASSIGNMENT_KEY].astext
        _merged_expr = Task.context["pr_merged"].astext
        _orig_id_expr = Task.payloads[0]["original_task_id"].as_string()
        _chain_lane_id_expr = Task.context[LANE_CHAIN_ID_KEY].astext

    task_rows = (
        await session.execute(
            select(
                Task.id,
                Task.estimated_cost_usd,
                _arm_expr,
                _merged_expr,
                _orig_id_expr,
                _chain_lane_id_expr,
            ).where(
                Task.project_id == project_id,
                Task.status.in_(
                    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
                ),
                _arm_expr.in_(["lane", "control"]),
            )
        )
    ).all()

    # Count distinct shadow-pilot chains separately (never included in the live
    # lane/control counts — shadow arms are observational only).
    shadow_rows = (
        await session.execute(
            select(Task.id, _orig_id_expr).where(
                Task.project_id == project_id,
                Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                _arm_expr == _SHADOW_ARM,
            )
        )
    ).all()
    _shadow_chain_ids: set[str] = set()
    for task_id_val, orig_id_val in shadow_rows:
        chain_id = str(orig_id_val).strip() if orig_id_val else str(task_id_val)
        _shadow_chain_ids.add(chain_id)
    n_shadow = len(_shadow_chain_ids)

    # Group rows by chain, then compute per-arm counts.
    _chains: dict[str, dict[str, Any]] = {}
    for task_id_val, cost, arm_val, merged_val, orig_id_val, lane_id_val in task_rows:
        arm = str(arm_val) if arm_val is not None else None
        if arm not in ("lane", "control"):
            continue
        chain_id = str(orig_id_val).strip() if orig_id_val else str(task_id_val)
        if chain_id not in _chains:
            _chains[chain_id] = {
                "arm": arm,
                "merged": False,
                "runtime_cost": 0.0,
                "reserved_idle_cost": 0.0,
                "lane_id": None,
            }
        _chains[chain_id]["runtime_cost"] += float(cost or 0.0)
        if lane_id_val:
            _chains[chain_id]["lane_id"] = str(lane_id_val).strip()
        if merged_val in (True, 1, "true", "True"):
            _chains[chain_id]["merged"] = True

    lane_ids = {
        chain["lane_id"]
        for chain in _chains.values()
        if chain["arm"] == "lane" and chain.get("lane_id")
    }
    lane_reserved_idle_cost_by_id: dict[str, float] = {}
    if lane_ids:
        lane_rows = (
            await session.execute(
                select(
                    ImprovementLane.id, ImprovementLane.reserved_idle_cost_usd_total
                ).where(
                    ImprovementLane.project_id == project_id,
                    ImprovementLane.pilot_arm == "lane",
                    ImprovementLane.id.in_(lane_ids),
                )
            )
        ).all()
        lane_reserved_idle_cost_by_id = {
            str(lane_id): float(reserved_idle_cost or 0.0)
            for lane_id, reserved_idle_cost in lane_rows
        }

    for chain in _chains.values():
        if chain["arm"] == "lane" and chain.get("lane_id"):
            chain["reserved_idle_cost"] = lane_reserved_idle_cost_by_id.get(
                str(chain["lane_id"]), 0.0
            )

    arm_merged_count: dict[str, int] = {"lane": 0, "control": 0}
    arm_runtime_cost_merged: dict[str, float] = {"lane": 0.0, "control": 0.0}
    arm_reserved_idle_cost_merged: dict[str, float] = {"lane": 0.0, "control": 0.0}
    arm_total_cost_merged: dict[str, float] = {"lane": 0.0, "control": 0.0}
    arm_chain_total: dict[str, int] = {"lane": 0, "control": 0}

    for chain in _chains.values():
        arm = chain["arm"]
        if arm not in ("lane", "control"):
            continue
        arm_chain_total[arm] += 1
        if chain["merged"]:
            arm_merged_count[arm] += 1
            arm_runtime_cost_merged[arm] += chain["runtime_cost"]
            arm_reserved_idle_cost_merged[arm] += chain["reserved_idle_cost"]
            arm_total_cost_merged[arm] += (
                chain["runtime_cost"] + chain["reserved_idle_cost"]
            )

    n_lane = arm_chain_total["lane"]
    n_control = arm_chain_total["control"]

    lane_merge_rate: float | None = (
        arm_merged_count["lane"] / arm_chain_total["lane"]
        if arm_chain_total["lane"] > 0
        else None
    )
    control_merge_rate: float | None = (
        arm_merged_count["control"] / arm_chain_total["control"]
        if arm_chain_total["control"] > 0
        else None
    )
    lane_cost_per_merged: float | None = (
        arm_total_cost_merged["lane"] / arm_merged_count["lane"]
        if arm_merged_count["lane"] > 0
        else None
    )
    control_cost_per_merged: float | None = (
        arm_total_cost_merged["control"] / arm_merged_count["control"]
        if arm_merged_count["control"] > 0
        else None
    )
    lane_runtime_cost_per_merged: float | None = (
        arm_runtime_cost_merged["lane"] / arm_merged_count["lane"]
        if arm_merged_count["lane"] > 0
        else None
    )
    control_runtime_cost_per_merged: float | None = (
        arm_runtime_cost_merged["control"] / arm_merged_count["control"]
        if arm_merged_count["control"] > 0
        else None
    )
    lane_reserved_idle_cost_per_merged: float | None = (
        arm_reserved_idle_cost_merged["lane"] / arm_merged_count["lane"]
        if arm_merged_count["lane"] > 0
        else None
    )
    control_reserved_idle_cost_per_merged: float | None = (
        arm_reserved_idle_cost_merged["control"] / arm_merged_count["control"]
        if arm_merged_count["control"] > 0
        else None
    )

    # ── Checkpoint-loss rate from ImprovementLane rows ────────────────────────
    lane_rows = (
        (
            await session.execute(
                select(ImprovementLane).where(
                    ImprovementLane.project_id == project_id,
                    ImprovementLane.pilot_arm.in_(_VALID_ARMS),
                )
            )
        )
        .scalars()
        .all()
    )
    lane_rows_only = [ln for ln in lane_rows if ln.pilot_arm == "lane"]
    checkpoint_loss_rate: float | None = None
    checkpoint_attempts_total = 0
    checkpoint_requeues_total = 0
    if lane_rows_only:
        total_seq = sum(int(ln.checkpoint_seq or 0) for ln in lane_rows_only)
        total_requeues = sum(int(ln.phase_requeues or 0) for ln in lane_rows_only)
        checkpoint_attempts_total = total_seq + total_requeues
        checkpoint_requeues_total = total_requeues
        if checkpoint_attempts_total:
            checkpoint_loss_rate = total_requeues / checkpoint_attempts_total

    # ── Gate-integrity violation count (non-circular, direct Task scan) ─────────
    # Count review/audit tasks in the lane arm that ACTUALLY carry transient lane
    # dispatch context (improvement_lane_id).  Under correct operation (strip guard
    # fires on every review spawn) this is always 0.  It becomes non-zero only if a
    # bug bypasses the strip, making it a reliable, non-circular durable signal.
    # Reading ProjectMessages written by raise_gate_integrity_alert is circular:
    # the alert writer requires the violation count to already exceed the threshold.
    if is_sqlite:
        _gate_stmt = select(func.count(Task.id)).where(
            Task.project_id == project_id,
            func.json_extract(Task.context, f"$.{LANE_PILOT_ASSIGNMENT_KEY}") == "lane",
            func.json_extract(Task.context, "$.improvement_lane_id").isnot(None),
            func.json_extract(Task.payloads, "$[0].type").in_(["review", "audit"]),
        )
    else:
        _gate_stmt = select(func.count(Task.id)).where(
            Task.project_id == project_id,
            Task.context[LANE_PILOT_ASSIGNMENT_KEY].astext == "lane",
            Task.context["improvement_lane_id"].isnot(None),
            Task.payloads[0]["type"].as_string().in_(["review", "audit"]),
        )
    gate_integrity_violations = int((await session.execute(_gate_stmt)).scalar() or 0)

    # ── Control-arm contamination: warm-resume receipts on control fix tasks ──
    # Under correct operation control-arm fix tasks NEVER carry a warm-resume
    # receipt — they always run cold.  Any non-zero count means the gate that
    # guards confirm_lane_warm_resume was bypassed and the control baseline is
    # contaminated.  Even one such task invalidates the comparison.
    if is_sqlite:
        _ctrl_warm_stmt = select(func.count(Task.id)).where(
            Task.project_id == project_id,
            func.json_extract(Task.context, f"$.{LANE_PILOT_ASSIGNMENT_KEY}")
            == "control",
            func.json_extract(
                Task.context, f"$.{LANE_PILOT_WARM_RESUME_RECEIPT_KEY}"
            ).isnot(None),
            func.json_extract(Task.payloads, "$[0].type").in_(["fix", "implement"]),
        )
    else:
        _ctrl_warm_stmt = select(func.count(Task.id)).where(
            Task.project_id == project_id,
            Task.context[LANE_PILOT_ASSIGNMENT_KEY].astext == "control",
            Task.context[LANE_PILOT_WARM_RESUME_RECEIPT_KEY].isnot(None),
            Task.payloads[0]["type"].as_string().in_(["fix", "implement"]),
        )
    control_warm_resume_count = int(
        (await session.execute(_ctrl_warm_stmt)).scalar() or 0
    )

    # ── Treatment-engagement receipts (warm-resume + checkpoint) ─────────────
    # Among terminal lane-arm fix-phase tasks, count chains where at least one
    # fix task in the chain carried each durable receipt.  Grouping by chain
    # (via original_task_id) keeps engagement fractions consistent with the
    # chain-level n_lane count computed above.
    if is_sqlite:
        _warm_receipt_expr = func.json_extract(
            Task.context, f"$.{LANE_PILOT_WARM_RESUME_RECEIPT_KEY}"
        )
        _ckpt_receipt_expr = func.json_extract(
            Task.context, f"$.{LANE_PILOT_CHECKPOINT_RECEIPT_KEY}"
        )
        _fix_payload_type_expr = func.json_extract(Task.payloads, "$[0].type")
        _engagement_stmt = select(
            Task.id, _orig_id_expr, _warm_receipt_expr, _ckpt_receipt_expr
        ).where(
            Task.project_id == project_id,
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            func.json_extract(Task.context, f"$.{LANE_PILOT_ASSIGNMENT_KEY}") == "lane",
            _fix_payload_type_expr.in_(["fix", "implement"]),
        )
    else:
        _engagement_stmt = select(
            Task.id,
            _orig_id_expr,
            Task.context[LANE_PILOT_WARM_RESUME_RECEIPT_KEY].astext,
            Task.context[LANE_PILOT_CHECKPOINT_RECEIPT_KEY].astext,
        ).where(
            Task.project_id == project_id,
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            Task.context[LANE_PILOT_ASSIGNMENT_KEY].astext == "lane",
            Task.payloads[0]["type"].as_string().in_(["fix", "implement"]),
        )

    engagement_rows = (await session.execute(_engagement_stmt)).all()
    _TRUTHY = (True, 1, "true", "True", "1")
    # Group by chain: chain is warm/checkpoint-engaged if ANY fix task in it was.
    _engagement_chains: dict[str, dict[str, bool]] = {}
    for task_id_val, orig_id_val, warm_val, ckpt_val in engagement_rows:
        chain_id = str(orig_id_val).strip() if orig_id_val else str(task_id_val)
        if chain_id not in _engagement_chains:
            _engagement_chains[chain_id] = {"warm": False, "ckpt": False}
        if warm_val in _TRUTHY:
            _engagement_chains[chain_id]["warm"] = True
        if ckpt_val in _TRUTHY:
            _engagement_chains[chain_id]["ckpt"] = True

    lane_fix_tasks_sampled = len(_engagement_chains)
    warm_resume_count = sum(1 for c in _engagement_chains.values() if c["warm"])
    checkpoint_count = sum(1 for c in _engagement_chains.values() if c["ckpt"])

    warm_resume_fraction: float | None = (
        warm_resume_count / lane_fix_tasks_sampled
        if lane_fix_tasks_sampled > 0
        else None
    )
    checkpoint_fraction: float | None = (
        checkpoint_count / lane_fix_tasks_sampled
        if lane_fix_tasks_sampled > 0
        else None
    )

    return {
        "n_lane": n_lane,
        "n_control": n_control,
        "n_shadow": n_shadow,
        "lane_merge_rate": lane_merge_rate,
        "control_merge_rate": control_merge_rate,
        "lane_cost_per_merged": lane_cost_per_merged,
        "control_cost_per_merged": control_cost_per_merged,
        "lane_runtime_cost_per_merged": lane_runtime_cost_per_merged,
        "control_runtime_cost_per_merged": control_runtime_cost_per_merged,
        "lane_reserved_idle_cost_per_merged": lane_reserved_idle_cost_per_merged,
        "control_reserved_idle_cost_per_merged": control_reserved_idle_cost_per_merged,
        "gate_integrity_violations": gate_integrity_violations,
        "control_warm_resume_count": control_warm_resume_count,
        "checkpoint_loss_rate": checkpoint_loss_rate,
        "checkpoint_attempts_total": checkpoint_attempts_total,
        "checkpoint_requeues_total": checkpoint_requeues_total,
        "warm_resume_fraction": warm_resume_fraction,
        "checkpoint_fraction": checkpoint_fraction,
        "lane_fix_tasks_sampled": lane_fix_tasks_sampled,
    }


async def sweep_lane_pilot_kill_verdict(session: AsyncSession) -> int:
    """Evaluate KILL conditions for all projects with active lane work.

    Runs on every reaper cycle, independently of digest computation, so the
    safety KILL (flag flip + force-expire reservations + kill lanes + alert)
    is not gated on a leader-only precompute timer or an operator GET.

    Returns the count of projects where KILL was applied this cycle.
    """
    if not is_review_fix_lane_pilot_enabled():
        return 0

    from app.capability_trial_verdict import evaluate_lane_pilot_verdict
    from app.improvement_lane_ops import _TERMINAL_STATUSES
    from app.models import ImprovementLane, Project

    # Enumerate distinct project IDs that have non-terminal lane rows.
    rows = (
        await session.execute(
            select(ImprovementLane.project_id).where(
                ImprovementLane.status.not_in(list(_TERMINAL_STATUSES))
            )
        )
    ).all()
    project_ids = list({row[0] for row in rows})

    # Batch-fetch all projects and snapshot capability_adoption_verdicts as plain
    # dicts to avoid N+1 session.get calls inside the loop. After the first
    # apply_lane_pilot_kill_in_transaction (which commits), ORM instances expire,
    # so we snapshot the field we need rather than holding ORM references.
    if project_ids:
        _proj_rows = (
            await session.execute(
                select(Project).where(Project.id.in_(project_ids))
            )
        ).scalars().all()
        _verdicts_by_id: dict = {
            str(p.id): dict(p.capability_adoption_verdicts)
            if isinstance(p.capability_adoption_verdicts, dict)
            else None
            for p in _proj_rows
        }
        _found_ids = {str(p.id) for p in _proj_rows}
    else:
        _verdicts_by_id = {}
        _found_ids = set()

    kills_applied = 0
    for project_id in project_ids:
        try:
            # Skip projects that already have a durable KILL recorded.
            if str(project_id) not in _found_ids:
                continue
            verdicts = _verdicts_by_id.get(str(project_id))
            if isinstance(verdicts, dict):
                entry = verdicts.get(LANE_PILOT_CAP_NAME)
                if isinstance(entry, dict) and entry.get("verdict") == "KILL":
                    continue

            aggregates = await compute_lane_pilot_aggregates(session, project_id)
            verdict = evaluate_lane_pilot_verdict(aggregates)
            if verdict.get("verdict") != "KILL":
                continue

            alert_detail: str | None = None
            if verdict.get("immediate_alert"):
                alert_detail = (
                    verdict.get("verdict_reason") or "gate_integrity_violation"
                )

            await apply_lane_pilot_kill_in_transaction(
                session,
                project_id,
                verdict_entry=verdict,
                reason="reaper_sweep",
                alert_detail=alert_detail,
            )
            kills_applied += 1
            logger.info(
                "[LANE-PILOT] reaper sweep applied KILL to project %s reason=%s",
                project_id,
                verdict.get("verdict_reason"),
            )
        except Exception as exc:
            logger.error(
                "[LANE-PILOT] reaper sweep error for project %s: %s",
                project_id,
                exc,
                exc_info=True,
            )

    return kills_applied
