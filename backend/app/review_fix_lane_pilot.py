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

Flag-off purity
---------------
With the pilot flag off (or the project not the Blueprint self-repo) every
chain runs the classic path byte-for-byte unchanged: no intent-to-treat
assignment is written, no lane context is attached, and no reservation is
taken. The only unconditional action is stripping transient per-phase lane
directives that must never be inherited — a no-op on a classic chain that
never carried them.

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
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    if not is_review_fix_lane_pilot_enabled() or not is_pilot_eligible_project(project):
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
async def get_chain_lane(
    session: AsyncSession, ctx: dict | None
) -> Any | None:
    """Return the ImprovementLane bound to a chain via LANE_CHAIN_ID_KEY, or None."""
    from app.models import ImprovementLane

    if not isinstance(ctx, dict):
        return None
    raw = ctx.get(LANE_CHAIN_ID_KEY)
    if not raw:
        return None
    try:
        lane_id = uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None
    return await session.get(ImprovementLane, lane_id)


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
) -> Any | None:
    """Look up (or lazily create+activate) the lane for a lane-arm chain.

    The lane is created in PROPOSED then advanced to ACTIVE and bound to the
    running phase task, so the next checkpoint can CAS ACTIVE -> PARKED. The
    chain remembers its lane via ``LANE_CHAIN_ID_KEY`` in *ctx*.
    """
    from app.improvement_lane_ops import cas_transition, create_lane
    from app.models import ImprovementLaneStatus

    lane = await get_chain_lane(session, ctx)
    if lane is not None:
        return lane

    lane = await create_lane(
        session,
        project_id=project_id,
        direction_id=direction_id,
        title=title,
        goal=title,
        branch_name=branch_name,
        base_branch=base_branch,
        pilot_arm="lane",
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
            block[field] = [
                _clip(x) for x in value[-_RESUME_LIST_MAX_ITEMS:]
            ]

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
) -> bool:
    """Checkpoint the lane and move its workspace to WARM_RESERVED.

    Called when a lane-arm implement/fix phase completes and a COLD review is
    about to spawn. Appends a verified checkpoint (CAS ACTIVE -> PARKED) and
    reserves the just-freed warm-pool member to the lane so the next fix phase
    can resume warm. Returns True when a checkpoint was written.

    A missing/invalid SHA degrades to ``mark_checkpoint_missing`` (the lane is
    left for the sweep to requeue) rather than fabricating a checkpoint.
    """
    from app.improvement_lane_ops import (
        LaneCheckpointError,
        checkpoint_lane,
        mark_checkpoint_missing,
    )
    from app.models import ImprovementLaneStatus
    from app.payload_schemas import LaneCheckpointPayload

    if lane is None:
        return False

    # Only the ACTIVE phase checkpoints. A lane already PARKED for this phase
    # (e.g. a re-review spawned twice) is a no-op, not a loss — do not requeue.
    if lane.status != ImprovementLaneStatus.ACTIVE.value:
        return False

    if not sha or len(str(sha)) < 7:
        with contextlib.suppress(LaneCheckpointError):
            await mark_checkpoint_missing(session, lane.id, reason="no_phase_sha")
        return False

    # Verify the SHA is actually reachable on the lane branch before recording
    # it as the durable checkpoint. An unverified SHA causes the agent's warm-
    # resume hard-reset to fail and silently degrades to a cold clone —
    # defeating the measurement. Policy: verification failure (both False and
    # None) degrades to mark_checkpoint_missing so the sweep can requeue;
    # verification unavailability (credentials missing, transient API failure)
    # is treated identically to not-reachable to avoid recording a
    # potentially-stale SHA. Neither case blocks the review spawn path.
    try:
        from app.github_refs import is_ancestor_of
        from app.github_token_resolver import get_github_token_for_task

        gh_token, gh_owner, gh_repo = await get_github_token_for_task(
            source_task.id, session
        )
        reachable = await is_ancestor_of(
            gh_token, gh_owner, gh_repo, str(sha), lane.branch_name,
            project_id=str(source_task.project_id),
        )
    except Exception as exc:  # noqa: BLE001 - verification is best-effort
        logger.warning(
            "[LANE-PILOT] SHA verification unavailable for lane %s sha=%s: %s",
            lane.id,
            str(sha)[:8],
            exc,
        )
        with contextlib.suppress(LaneCheckpointError):
            await mark_checkpoint_missing(
                session, lane.id, reason="sha_verification_unavailable"
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
            await mark_checkpoint_missing(
                session, lane.id, reason="sha_not_reachable"
            )
        return False

    if reachable is None:
        logger.warning(
            "[LANE-PILOT] SHA reachability indeterminate for lane %s sha=%s — "
            "skipping checkpoint",
            lane.id,
            str(sha)[:8],
        )
        with contextlib.suppress(LaneCheckpointError):
            await mark_checkpoint_missing(
                session, lane.id, reason="sha_verification_unavailable"
            )
        return False

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
    )
    try:
        await checkpoint_lane(
            session,
            lane.id,
            phase=str(source_task.first_payload.get("type", "")) or "phase",
            task_id=source_task.id,
            checkpoint=payload,
        )
    except LaneCheckpointError as exc:
        logger.warning("[LANE-PILOT] checkpoint failed for lane %s: %s", lane.id, exc)
        with contextlib.suppress(LaneCheckpointError):
            await mark_checkpoint_missing(session, lane.id, reason="checkpoint_error")
        return False

    # Reserve the freed warm-pool member to the lane (best-effort: only engages
    # when the source ran on a pool member). The lane's workspace_state mirrors
    # the reservation so operators see WARM_RESERVED on the lane row.
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


# ── Fix-side warm resume wiring ───────────────────────────────────────────────
async def activate_lane_for_fix(
    session: AsyncSession, lane: Any, *, task_id: uuid.UUID | None = None
) -> None:
    """Resume a PARKED lane to ACTIVE for the fix phase (best-effort).

    Binds the fix to the lane and flips the workspace to LEASED so the next
    re-review boundary can checkpoint ACTIVE -> PARKED again. A lane already
    ACTIVE (or in a terminal state) is left unchanged.
    """
    from app.improvement_lane_ops import LaneCASError, cas_transition
    from app.models import ImprovementLaneStatus

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
        lane.workspace_state = WORKSPACE_LEASED
        session.add(lane)
    except LaneCASError as exc:
        logger.warning("[LANE-PILOT] could not resume lane %s for fix: %s", lane.id, exc)


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
    cleared = 0
    for member in members:
        member.reserved_lane_id = None
        member.reservation_expires_at = None
        session.add(member)
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

    Transitions ACTIVE or PARKED lanes to RETIRED.  If the lane happens to be
    PROPOSED (non-terminal but PROPOSED->RETIRED is not a valid transition),
    the CAS raises LaneCASError and is silently swallowed — PROPOSED is
    transient and is advanced to ACTIVE immediately on creation.

    Called when a chain's PR merges or closes so the lane row reaches a
    terminal state promptly.  Best-effort: LaneCASError (concurrent retirement
    by another worker) is suppressed.  Returns True when a transition was made.
    """
    from app.improvement_lane_ops import LaneCASError, _TERMINAL_STATUSES, cas_transition
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
            await session.execute(
                select(PoolVm).where(PoolVm.reserved_lane_id == lane.id)
            )
        ).scalars().all()
        for member in reserved_members:
            member.reserved_lane_id = None
            member.reservation_expires_at = None
            session.add(member)
        return True
    except LaneCASError as exc:
        logger.debug("[LANE-PILOT] retire_chain_lane CAS miss lane=%s: %s", lane.id, exc)
        return False


async def kill_project_lanes(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """CAS non-terminal ImprovementLane rows for *project_id* to KILLED.

    Transitions ACTIVE and PARKED lanes to KILLED.  PROPOSED lanes are
    technically non-terminal but PROPOSED->KILLED is not a valid transition;
    any PROPOSED row encountered raises LaneCASError and is silently skipped
    (PROPOSED is transient and is advanced to ACTIVE immediately on creation).

    Returns the count of lanes successfully transitioned.  Called from
    apply_lane_pilot_kill so the KILL operation closes all lane rows rather
    than leaving them ACTIVE/PARKED after the pilot flag is disabled.
    """
    from app.improvement_lane_ops import LaneCASError, _TERMINAL_STATUSES, cas_transition
    from app.models import ImprovementLane, ImprovementLaneStatus

    rows = (
        await session.execute(
            select(ImprovementLane).where(
                ImprovementLane.project_id == project_id,
                ImprovementLane.status.not_in(list(_TERMINAL_STATUSES)),
            )
        )
    ).scalars().all()

    killed = 0
    for lane in rows:
        try:
            current = ImprovementLaneStatus(lane.status)
            await cas_transition(session, lane.id, current, ImprovementLaneStatus.KILLED)
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
            project = await session.get(Project, project_id)
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
                await raise_gate_integrity_alert(session, project_id, detail=alert_detail)
            await session.commit()
            return result
        except Exception:
            await session.rollback()
            raise


async def raise_gate_integrity_alert(
    session: AsyncSession, project_id: uuid.UUID, detail: str
) -> None:
    """Raise an immediate operator alert for a gate-integrity violation.

    Posts a BLOCKER ProjectMessage regardless of trial sample size (gate
    integrity is non-negotiable). Deduplicated on the violation event type so
    repeated detections in a sweep don't spam the board.
    """
    from app.models import MessageType, ProjectMessage

    dedupe_key = "review_fix_lane_gate_integrity_violation"
    existing = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.status == "open",
            )
        )
    ).scalars().all()
    already = any(
        (m.context or {}).get("dedupe_key") == dedupe_key for m in existing
    )
    if already:
        return
    session.add(
        ProjectMessage(
            project_id=project_id,
            message_type=MessageType.BLOCKER,
            from_type="system",
            content=f"[LANE-PILOT] gate-integrity violation: {detail}",
            context={
                "event_type": "review_fix_lane_gate_integrity_violation",
                "dedupe_key": dedupe_key,
            },
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
            select(Task.created_at, Task.started_at, Task.updated_at, Task.context)
            .where(
                Task.project_id == project_id,
                Task.status.in_(
                    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
                ),
                _arm_expr == "lane",
            )
        )
    ).all()

    def _strip_tz(dt: Any) -> Any:
        if dt is None:
            return None
        return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) is not None else dt

    pr_buckets: dict[str, list[tuple]] = {}
    for created_at, started_at, updated_at, ctx in rows:
        if not isinstance(ctx, dict):
            continue
        review_loop = ctx.get("review_loop")
        if not isinstance(review_loop, dict):
            continue
        pr_number = review_loop.get("pr_number")
        if pr_number is None:
            continue
        key = str(pr_number)
        if key not in pr_buckets:
            pr_buckets[key] = []
        pr_buckets[key].append((_strip_tz(created_at), _strip_tz(started_at), _strip_tz(updated_at)))

    if not pr_buckets:
        return {
            "wall_clock_seconds": None,
            "active_seconds": None,
            "gap_seconds": None,
            "task_count": None,
            "pr_count": 0,
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
        wc_vals.append(wall_clock)
        active_vals.append(active)
        gap_vals.append(max(wall_clock - active, 0.0))
        count_vals.append(float(len(tasks)))

    return {
        "wall_clock_seconds": _median(wc_vals),
        "active_seconds": _median(active_vals),
        "gap_seconds": _median(gap_vals),
        "task_count": _median(count_vals),
        "pr_count": len(pr_buckets),
    }


# ── TrialDefinition aggregate wiring (feeds evaluate_lane_pilot_verdict) ───────
async def compute_lane_pilot_aggregates(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[str, Any]:
    """Build per-arm aggregates for ``evaluate_lane_pilot_verdict``.

    Sample counts, merge rate, and cost-per-merged come from durable Task rows
    carrying the intent-to-treat arm assignment. Checkpoint-loss rate comes from
    ImprovementLane rows (phase_requeues). Gate-integrity violations are detected
    by querying for review/audit tasks that ACTUALLY carry transient lane context
    keys — a non-circular durable signal that is 0 under correct operation and
    non-zero only when the strip-transient guard was bypassed.
    """
    from sqlalchemy import func

    from app.database import resolve_dialect_name
    from app.models import ImprovementLane, Task, TaskStatus

    # ── Durable per-arm sample counts + merge/cost from Task rows ─────────────
    # Use intent-to-treat assignment, not lane rows: control chains never create
    # an ImprovementLane row, so lane-row counts would permanently undercount it.
    is_sqlite = resolve_dialect_name(session) == "sqlite"
    if is_sqlite:
        _arm_expr = func.json_extract(Task.context, f"$.{LANE_PILOT_ASSIGNMENT_KEY}")
        _merged_expr = func.json_extract(Task.context, "$.pr_merged")
    else:
        _arm_expr = Task.context[LANE_PILOT_ASSIGNMENT_KEY].astext
        _merged_expr = Task.context["pr_merged"].astext

    task_rows = (
        await session.execute(
            select(Task.estimated_cost_usd, _arm_expr, _merged_expr)
            .where(
                Task.project_id == project_id,
                Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                _arm_expr.in_(["lane", "control"]),
            )
        )
    ).all()

    arm_merged_count: dict[str, int] = {"lane": 0, "control": 0}
    arm_cost_merged: dict[str, float] = {"lane": 0.0, "control": 0.0}
    arm_task_total: dict[str, int] = {"lane": 0, "control": 0}

    for cost, arm_val, merged_val in task_rows:
        arm = str(arm_val) if arm_val is not None else None
        if arm not in ("lane", "control"):
            continue
        arm_task_total[arm] += 1
        is_merged = merged_val in (True, 1, "true", "True") if merged_val is not None else False
        if is_merged:
            arm_merged_count[arm] += 1
            arm_cost_merged[arm] += float(cost or 0.0)

    n_lane = arm_task_total["lane"]
    n_control = arm_task_total["control"]

    lane_merge_rate: float | None = (
        arm_merged_count["lane"] / arm_task_total["lane"]
        if arm_task_total["lane"] > 0 else None
    )
    control_merge_rate: float | None = (
        arm_merged_count["control"] / arm_task_total["control"]
        if arm_task_total["control"] > 0 else None
    )
    lane_cost_per_merged: float | None = (
        arm_cost_merged["lane"] / arm_merged_count["lane"]
        if arm_merged_count["lane"] > 0 else None
    )
    control_cost_per_merged: float | None = (
        arm_cost_merged["control"] / arm_merged_count["control"]
        if arm_merged_count["control"] > 0 else None
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
    if lane_rows_only:
        lost = sum(1 for ln in lane_rows_only if (ln.phase_requeues or 0) > 0)
        checkpoint_loss_rate = lost / len(lane_rows_only)

    # ── Gate-integrity violation count (non-circular, direct Task scan) ─────────
    # Count review/audit tasks in the lane arm that ACTUALLY carry transient lane
    # dispatch context (improvement_lane_id).  Under correct operation (strip guard
    # fires on every review spawn) this is always 0.  It becomes non-zero only if a
    # bug bypasses the strip, making it a reliable, non-circular durable signal.
    # Reading ProjectMessages written by raise_gate_integrity_alert is circular:
    # the alert writer requires the violation count to already exceed the threshold.
    if is_sqlite:
        _gate_stmt = (
            select(func.count(Task.id))
            .where(
                Task.project_id == project_id,
                func.json_extract(Task.context, f"$.{LANE_PILOT_ASSIGNMENT_KEY}") == "lane",
                func.json_extract(Task.context, "$.improvement_lane_id").isnot(None),
                func.json_extract(Task.payloads, "$[0].type").in_(["review", "audit"]),
            )
        )
    else:
        _gate_stmt = (
            select(func.count(Task.id))
            .where(
                Task.project_id == project_id,
                Task.context[LANE_PILOT_ASSIGNMENT_KEY].astext == "lane",
                Task.context["improvement_lane_id"].isnot(None),
                Task.payloads[0]["type"].as_string().in_(["review", "audit"]),
            )
        )
    gate_integrity_violations = int(
        (await session.execute(_gate_stmt)).scalar() or 0
    )

    # ── Treatment-engagement receipts (warm-resume + checkpoint) ─────────────
    # Among terminal lane-arm fix-phase tasks, count how many carried each
    # durable receipt.  Receipt keys are stamped only at the actual warm-claim
    # or checkpoint site, so absence means the treatment was not delivered.
    if is_sqlite:
        _warm_receipt_expr = func.json_extract(
            Task.context, f"$.{LANE_PILOT_WARM_RESUME_RECEIPT_KEY}"
        )
        _ckpt_receipt_expr = func.json_extract(
            Task.context, f"$.{LANE_PILOT_CHECKPOINT_RECEIPT_KEY}"
        )
        _fix_payload_type_expr = func.json_extract(Task.payloads, "$[0].type")
        _engagement_stmt = (
            select(_warm_receipt_expr, _ckpt_receipt_expr)
            .where(
                Task.project_id == project_id,
                Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                func.json_extract(Task.context, f"$.{LANE_PILOT_ASSIGNMENT_KEY}") == "lane",
                _fix_payload_type_expr.in_(["fix", "implement"]),
            )
        )
    else:
        _engagement_stmt = (
            select(
                Task.context[LANE_PILOT_WARM_RESUME_RECEIPT_KEY].astext,
                Task.context[LANE_PILOT_CHECKPOINT_RECEIPT_KEY].astext,
            )
            .where(
                Task.project_id == project_id,
                Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                Task.context[LANE_PILOT_ASSIGNMENT_KEY].astext == "lane",
                Task.payloads[0]["type"].as_string().in_(["fix", "implement"]),
            )
        )

    engagement_rows = (await session.execute(_engagement_stmt)).all()
    lane_fix_tasks_sampled = len(engagement_rows)
    warm_resume_count = 0
    checkpoint_count = 0
    _TRUTHY = (True, 1, "true", "True", "1")
    for warm_val, ckpt_val in engagement_rows:
        if warm_val in _TRUTHY:
            warm_resume_count += 1
        if ckpt_val in _TRUTHY:
            checkpoint_count += 1

    warm_resume_fraction: float | None = (
        warm_resume_count / lane_fix_tasks_sampled if lane_fix_tasks_sampled > 0 else None
    )
    checkpoint_fraction: float | None = (
        checkpoint_count / lane_fix_tasks_sampled if lane_fix_tasks_sampled > 0 else None
    )

    return {
        "n_lane": n_lane,
        "n_control": n_control,
        "lane_merge_rate": lane_merge_rate,
        "control_merge_rate": control_merge_rate,
        "lane_cost_per_merged": lane_cost_per_merged,
        "control_cost_per_merged": control_cost_per_merged,
        "gate_integrity_violations": gate_integrity_violations,
        "checkpoint_loss_rate": checkpoint_loss_rate,
        "warm_resume_fraction": warm_resume_fraction,
        "checkpoint_fraction": checkpoint_fraction,
        "lane_fix_tasks_sampled": lane_fix_tasks_sampled,
    }
