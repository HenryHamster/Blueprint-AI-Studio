"""Shared capability-proof execution-authority fence — closes the TOCTOU gap.

Capability-proof prerequisites are resolved once, at admission, when a research-
or plan-spawned child is created. Between admission and the moment the child
actually gains execution authority the world can move underneath it: the
``capability_proof_gate_mode`` can graduate ``off``/``observe`` -> ``enforce``,
a capability's platform proof can expire past the freshness TTL, be revoked
after a definitive RED trial, or be contradicted/uncorroborated. A task admitted
while the gate was permissive would then dispatch on a foundation the platform
can no longer machine-verify — the classic time-of-check / time-of-use gap.

This module is the single shared fence that both the cold orchestrator dispatch
routes and the warm ``/next-role`` claim finalizer call IMMEDIATELY BEFORE a
locked QUEUED task gains execution authority. It re-grades the task's capability
requirements against the LIVE gate mode and the project's current platform-only
proof ledger, reusing the exact same BOTH consumer seams the spawn-time resolver
and the blocked-resume sweep apply — the working-registry seam
(:func:`~app.capability_registry.consumer_gate_proof_status` +
:func:`~app.capability_registry.capability_need_blocked_by_proof_gate`) AND the
task-backed seam
(:func:`~app.capability_registry.task_backed_capability_need_blocked_by_proof_gate`)
— so this authority boundary can never drift from them (freshness, alias,
corroboration and operator-exemption semantics are all inherited).

Durability: the canonical capability-requirement record
(:data:`CAPABILITY_EXECUTION_REQUIREMENTS_KEY`) is deliberately NOT listed in
``task_blocked.FRESH_ATTEMPT_CONTEXT_STRIP_KEYS``, so it SURVIVES manual and bulk
generic retry even though fresh-attempt cleanup strips ordinary blocker metadata.
Retry therefore cannot bypass the fence — the requirement is re-read on every
dispatch/claim, and the normal blocked-resume sweep releases the task only once
platform proof is current again.

The fence is a no-op in ``off``/``observe`` mode and for tasks that carry no
capability requirement, so existing execution behavior and endpoint response
compatibility are preserved.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Mapping

from sqlalchemy.orm.attributes import flag_modified

from app.models import Task
from app.task_blocked import build_need, enter_blocked_state, get_blocker_needs

logger = logging.getLogger(__name__)

# Canonical, durable record of EVERY capability prerequisite a child depends on,
# stamped at admission regardless of whether the capability was proven, observed,
# or the gate was off. A list of normalized ``{kind: "capability", ref[, label]}``
# needs. Kept OUT of ``FRESH_ATTEMPT_CONTEXT_STRIP_KEYS`` so it survives generic
# retry; the fence re-reads it on every execution-authority boundary.
CAPABILITY_EXECUTION_REQUIREMENTS_KEY = "_capability_execution_requirements"

# Backward-compatible admission-time surfacing shape written by the spawn-time
# resolver (research_completion / plan_completion) in observe/off mode. Duplicated
# as a literal here (rather than imported) to keep this a leaf module free of the
# heavy completion-handler import graph.
_SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY = "_spawn_capability_proof_unverified"

_GATE_MODES = ("off", "observe", "enforce")

# Warm-dispatch pin/lease context markers cleared when a warm claim is denied,
# so a now-BLOCKED task never keeps a pool VM (or chain/backlog VM) pinned to it.
_WARM_PIN_CONTEXT_KEYS = (
    "warm_target_vm_id",
    "warm_pool_pin",
    "warm_target_pr",
    "warm_handoff_deadline",
)


def _capability_needs_from(entries: object) -> List[dict]:
    """Extract normalized capability needs from a heterogeneous entry list.

    Accepts the several backward-compatible shapes that carry a capability
    reference (blocker needs, ``unresolved_prerequisite``,
    ``_spawn_capability_proof_unverified``, ``prerequisite``, and the canonical
    durable record). Only ``kind == "capability"`` entries survive; each is
    re-validated through :func:`~app.task_blocked.build_need`, so a malformed
    dict is dropped rather than mis-graded.
    """
    out: List[dict] = []
    if isinstance(entries, Mapping):
        entries = [entries]
    if not isinstance(entries, (list, tuple)):
        return out
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("kind") != "capability":
            continue
        need = build_need("capability", entry.get("ref"), entry.get("label"))
        if need is not None:
            out.append(need)
    return out


def collect_capability_requirements(task: Task) -> List[dict]:
    """Reconstruct every capability requirement recorded on ``task``.

    Unions the canonical durable record with every backward-compatible shape a
    spawned child may carry, so a task admitted before the durable record existed
    (or under a shape written by another seam) is still fenced. Deduplicated by
    normalized capability ref. Returns ``[]`` for a task with no capability
    requirement — the fence is then a pure no-op.
    """
    ctx = task.context if isinstance(task.context, Mapping) else {}
    collected: List[dict] = []
    collected += _capability_needs_from(ctx.get(CAPABILITY_EXECUTION_REQUIREMENTS_KEY))
    collected += _capability_needs_from(get_blocker_needs(task))
    collected += _capability_needs_from(ctx.get(_SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY))
    collected += _capability_needs_from(ctx.get("unresolved_prerequisite"))
    collected += _capability_needs_from(ctx.get("prerequisite"))
    seen: set = set()
    deduped: List[dict] = []
    for need in collected:
        ref = need["ref"]
        if ref in seen:
            continue
        seen.add(ref)
        deduped.append(need)
    return deduped


def stamp_capability_requirements(task: Task, needs: object) -> List[dict]:
    """Persist ``needs`` into the task's canonical durable requirement record.

    Idempotently merges (dedup by ref) with any existing record and marks the
    JSONB column dirty. Callers own ``session.add`` / commit. Returns the full
    merged requirement list (``[]`` when nothing capability-shaped was supplied).
    """
    caps = _capability_needs_from(needs)
    ctx = dict(task.context or {})
    merged: dict = {}
    for need in _capability_needs_from(ctx.get(CAPABILITY_EXECUTION_REQUIREMENTS_KEY)):
        merged[need["ref"]] = need
    for need in caps:
        merged[need["ref"]] = need
    if not merged:
        return []
    ctx[CAPABILITY_EXECUTION_REQUIREMENTS_KEY] = list(merged.values())
    task.context = ctx
    flag_modified(task, "context")
    return list(merged.values())


def resolve_gate_mode(override: str | None = None) -> str:
    """Return the live ``capability_proof_gate_mode`` (``off``/``observe``/``enforce``).

    ``override`` (when a valid mode) wins — mirroring
    :func:`~app.capability_registry.capability_proof_gate_mode` so tests can inject
    a mode without touching global runtime config. Otherwise reads the canonical
    ``runtime_config`` the spawn resolver and blocked-resume sweep both read,
    falling back to the safest ``"off"`` for an unset/invalid value.
    """
    if override in _GATE_MODES:
        return override  # type: ignore[return-value]
    from app.shared_state import runtime_config

    raw = str(runtime_config.get("capability_proof_gate_mode", "off") or "off")
    return raw if raw in _GATE_MODES else "off"


def grade_blocking_capability_needs(
    project: object,
    needs: List[dict],
    *,
    mode: str,
    now: datetime | None = None,
) -> List[dict]:
    """Return the subset of ``needs`` that the proof gate BLOCKS under ``mode``.

    Grades each capability requirement against the project's platform-only proof
    ledger via BOTH shared consumer seams the spawn resolver and the blocked-resume
    sweep apply, unioning their blocking verdicts so the fence inherits the exact
    same coverage:

    * :func:`~app.capability_registry.capability_need_blocked_by_proof_gate` — the
      ``working`` registry seam: blocks an unproven capability that IS in the
      project's registry ``working`` set; returns not-blocked for a name that is
      not in that set.
    * :func:`~app.capability_registry.task_backed_capability_need_blocked_by_proof_gate`
      — the task-backed seam: blocks a capability foundation provided by a sibling
      task on its platform-ledger proof for the aliased name-cluster, EVEN when
      that capability is not in the registry ``working`` set. Without this a child
      admitted under off/observe against a task-backed foundation would
      under-grade to "not blocked" here and slip the fence after the gate
      graduates to enforce.

    Absent / expired (past freshness TTL) / revoked / contradicted / uncorroborated
    proof all fail identically. Returns ``[]`` for ``off``/``observe`` (no gating)
    and for a capability that either seam grades proven / exempt / corroborated.
    """
    if mode != "enforce" or not needs:
        return []
    from app.capability_registry import (
        capability_need_blocked_by_proof_gate,
        consumer_gate_proof_status,
        task_backed_capability_need_blocked_by_proof_gate,
    )
    from app.time_utils import _utcnow

    now = now or _utcnow()
    proof_records = getattr(project, "capability_proof_records", None)
    problem_model = getattr(project, "problem_model", None)
    proof_status_by_name = consumer_gate_proof_status(
        problem_model,
        proof_records,
        now=now,
    )
    return [
        need
        for need in needs
        if capability_need_blocked_by_proof_gate(need, proof_status_by_name, mode=mode)
        or task_backed_capability_need_blocked_by_proof_gate(
            need, proof_records, problem_model, mode=mode
        )
    ]


async def evaluate_execution_authority_block(
    session,
    task: Task,
    *,
    mode: str | None = None,
    now: datetime | None = None,
) -> List[dict]:
    """Cheap-first grade of ``task`` at an execution-authority boundary.

    Returns the capability needs that must BLOCK the task (empty => authority is
    granted). Does NOT mutate ``task`` — the caller owns the BLOCKED transition
    and the transaction so cold and warm paths can each unwind their own
    resources. The mode/requirement checks run before the project fetch, so the
    common off/observe/no-requirement path costs no extra IO.
    """
    resolved = resolve_gate_mode(mode)
    if resolved != "enforce":
        return []
    needs = collect_capability_requirements(task)
    if not needs:
        return []
    from app.models import Project

    project = await session.get(Project, task.project_id)
    if project is None:
        return []
    return grade_blocking_capability_needs(project, needs, mode=resolved, now=now)


def block_task_for_capability_proof(
    task: Task,
    blocking_needs: List[dict],
    *,
    source: str,
    now: datetime | None = None,
) -> dict:
    """Atomically move ``task`` to first-class BLOCKED with capability needs.

    Re-stamps the durable requirement record (so a subsequent generic retry that
    strips ordinary blocker metadata still cannot bypass the fence) and enters the
    BLOCKED state with the machine-resolvable capability needs the blocked-resume
    sweep re-grades. Consumes no dispatch slot — ``enter_blocked_state`` neither
    increments ``attempt_count`` nor records a failure. Caller owns
    ``session.add`` / commit and any resource unwind. Returns the blocker metadata.
    """
    stamp_capability_requirements(task, blocking_needs)
    refs = ", ".join(sorted({n["ref"] for n in blocking_needs}))
    return enter_blocked_state(
        task,
        waiting_on=f"capability proof: {refs}",
        reason=(
            f"Execution authority withheld at {source}: capability proof gate is "
            f"'enforce' and required platform-verified proof for [{refs}] is "
            "absent, expired, revoked, contradicted, or uncorroborated. Holds "
            "until the project's platform proof ledger records current proof."
        ),
        source="capability_proof_fence",
        blocker_kind="precondition",
        needs=list(blocking_needs),
        now=now,
    )


def clear_warm_dispatch_pin(task: Task) -> None:
    """Drop transient warm pin/lease markers so a denied task pins no warm VM.

    Called on the warm-denial path after :func:`block_task_for_capability_proof`:
    the task is now BLOCKED, so the pool/chain/backlog VM that had pinned it must
    not keep waiting on it. The pool VM lease itself is released separately by the
    caller (``warm_pool.release_pool_vm``).
    """
    ctx = task.context if isinstance(task.context, Mapping) else None
    if not ctx or not any(key in ctx for key in _WARM_PIN_CONTEXT_KEYS):
        return
    new_ctx = dict(ctx)
    for key in _WARM_PIN_CONTEXT_KEYS:
        new_ctx.pop(key, None)
    task.context = new_ctx
    flag_modified(task, "context")
