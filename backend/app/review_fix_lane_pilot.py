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
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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

# Per-phase dispatch directives that must NEVER be inherited from a source task.
# They are (re)attached freshly and only ever for a fix task on a lane arm.
TRANSIENT_LANE_CONTEXT_KEYS = (
    "improvement_lane_id",
    "is_lane_phase",
    "lane_checkpoint_sha",
)

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

    No-op (returns None, writes nothing) when the pilot is disabled or the
    project is out of scope — this is the flag-off purity guarantee. When an
    assignment is already present it is returned unchanged (recorded once).
    """
    if not is_review_fix_lane_pilot_enabled() or not is_pilot_eligible_project(project):
        return None
    existing = lane_arm_of(ctx)
    if existing is not None:
        return existing
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

    payload = LaneCheckpointPayload(
        sha=str(sha),
        summary=summary[:1024],
        warm_resume=bool(
            (getattr(source_task, "context", None) or {}).get(
                LANE_PILOT_WARM_RESUME_RECEIPT_KEY
            )
        ),
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

            member = await release_pool_vm(session, vm_id, lane_id=str(lane.id))
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
    """Attach lane warm-resume directives + use receipts to a FIX spawn context.

    Sets ``improvement_lane_id`` (so the warm-pool dispatch hook leases the
    lane-reserved member), ``is_lane_phase`` + ``lane_checkpoint_sha`` (so the
    agent fetch+resets to the checkpoint, skipping clone/install), and the
    warm-resume + checkpoint USE receipts. Receipts mark that lane mechanics
    actually engaged on this task — they feed the purity gate ONLY and never
    determine arm membership (the assignment key does that).
    """
    if lane is None:
        return
    ctx["improvement_lane_id"] = str(lane.id)
    ctx["is_lane_phase"] = True
    if lane.last_checkpoint_sha:
        ctx["lane_checkpoint_sha"] = str(lane.last_checkpoint_sha)
    ctx[LANE_PILOT_WARM_RESUME_RECEIPT_KEY] = True
    ctx[LANE_PILOT_CHECKPOINT_RECEIPT_KEY] = True


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


async def apply_lane_pilot_kill(
    session: AsyncSession, project_id: uuid.UUID, *, reason: str
) -> dict:
    """Degrade the pilot to classic spawning: flip the flag off + free members.

    The flag flip makes ``is_review_fix_lane_pilot_enabled`` return False so all
    subsequent spawns run classic; force-expiring reservations releases members
    held for in-flight lanes. Idempotent.
    """
    from app.shared_state import runtime_config

    runtime_config["review_fix_lane_pilot_enabled"] = False
    cleared = await force_expire_lane_reservations(session, project_id)
    logger.warning(
        "[LANE-PILOT] KILL applied for project %s reason=%s reservations_cleared=%d",
        project_id,
        reason,
        cleared,
    )
    return {"flag_disabled": True, "reservations_cleared": cleared, "reason": reason}


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

    return {
        "n_lane": n_lane,
        "n_control": n_control,
        "lane_merge_rate": lane_merge_rate,
        "control_merge_rate": control_merge_rate,
        "lane_cost_per_merged": lane_cost_per_merged,
        "control_cost_per_merged": control_cost_per_merged,
        "gate_integrity_violations": gate_integrity_violations,
        "checkpoint_loss_rate": checkpoint_loss_rate,
    }
