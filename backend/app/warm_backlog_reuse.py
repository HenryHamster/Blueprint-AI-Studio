"""Warm-VM backlog reuse (efficiency direction d-91d8dede).

Extends the existing warm-VM continuity mechanism so an idle agent VM can
claim the next eligible QUEUED task from its project's GENERAL backlog --
not only a PR-chain follow-on pinned to its VM. Gated by the runtime config
flag ``warm_backlog_reuse_enabled`` (default ON, guarded by the
is_warm_backlog_reuse_suppressed() circuit breaker), independent from
``warm_continuity_enabled``.

This module owns the *eligibility* and *atomic selection* for a backlog
claim. The actual QUEUED->IN_PROGRESS flip, telemetry stamping, and JWT mint
live in the /api/agent/next-role handler so the chain and backlog paths share
one claim-finalize routine.
"""

import logging
import uuid
from collections import Counter, deque
from datetime import timedelta

from sqlalchemy import or_, select

from app.models import Project, Task, TaskStatus
from app.orchestrator_filters import (
    HOTSPOT_FILE_THRESHOLD,
    _prod_files,
    file_conflict_defers,
)
from app.shared_state import runtime_config as _runtime_config
from app.task_capabilities import COMPUTE_SUBSTRATE_TASK_TYPES, READ_ONLY_TASK_TYPES
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")


def _norm(value) -> str | None:
    """Lowercased, stripped string, or None for empty/non-string values."""
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return None


def _is_read_only_payloads(payloads) -> bool:
    """True if a task's payloads describe a read-only task type.

    Read-only types (review/plan/research/...) push no branch and open no PR, so
    they cannot collide at merge time. They must be excluded from BOTH halves of
    the warm-path file-conflict check — never counted in the IN_PROGRESS claim
    set and never deferred as a candidate — to match
    ``filter_file_conflicts``'s isolation guarantee on the cold dispatch path.
    Mirrors ``Task.first_payload``: the first payload's ``type`` field.
    """
    first = (payloads or [{}])[0]
    return (first or {}).get("type", "") in READ_ONLY_TASK_TYPES


def _is_compute_substrate_payloads(payloads) -> bool:
    """True if a task's payloads describe a compute-substrate task type.

    Compute-substrate types (e.g. compute_experiment) have a dedicated
    runner-aware direct-dispatch path (_dispatch_compute_experiment_directly)
    that selects an eligible runner and pins runner_target_id before launch.
    Warm backlog reuse has no runner-selection logic, so it must never claim
    these tasks — doing so would default the GPU job to cloud dispatch and
    bypass the enrolled local runner (or raise ComputeLaunchPreconditionFailed
    when require_local_runner=true).
    """
    first = (payloads or [{}])[0]
    return (first or {}).get("type", "") in COMPUTE_SUBSTRATE_TASK_TYPES


def _task_pinned_providers(task_provider, ctx) -> set[str]:
    """Provider set a backlog task is constrained to (empty == unconstrained).

    Combines the recorded ``Task.provider`` column (what a re-queued task last
    ran on) with an explicit per-task ``context['provider_preference']`` pin.
    An empty set means the task is provider-fungible, so any boot account may
    run it warm.
    """
    constrained: set[str] = set()
    recorded = _norm(task_provider)
    if recorded:
        constrained.add(recorded)
    pref = ctx.get("provider_preference")
    if isinstance(pref, str):
        p = _norm(pref)
        if p:
            constrained.add(p)
    elif isinstance(pref, list):
        for item in pref:
            p = _norm(item)
            if p:
                constrained.add(p)
    return constrained


def _retarget_enabled() -> bool:
    """True iff warm-VM re-targeting is enabled (default-off ``warm_retarget_enabled``).

    When on, the warm-claim finalizer re-materializes the resolved account's
    credentials onto the VM, so boot-identity divergence no longer needs to
    disqualify a candidate (see ``_vm_boot_incompatible``).
    """
    return bool(_runtime_config.get("warm_retarget_enabled", False))


def _vm_boot_incompatible(
    vm_boot, task_provider, task_account_id, task_model, ctx
) -> bool:
    """True iff a backlog task is pinned/recorded to provider/account/model
    material incompatible with the reusing VM's frozen boot manifest.

    A reused VM runs every role on the LLM provider/account/model baked into
    its boot manifest — the warm claim only mints a fresh JWT and never
    re-materializes credentials. So a task whose resolved/pinned provider,
    account, or model differs from the boot identity would silently run on the
    wrong account; such tasks must cold-dispatch instead of being warm-claimed.

    EXCEPTION — warm re-targeting (``warm_retarget_enabled``): when on, the
    warm-claim finalizer re-resolves the task's LLM runtime and re-materializes
    the resolved account's secret manifest onto the VM, so provider/account/
    model divergence is fully re-targetable and no longer disqualifies. The
    only remaining isolation boundary is same-project scoping, which the caller
    already enforces — so this gate becomes a pure no-op. With the flag OFF the
    original boot-compat behavior below is byte-for-byte unchanged.

    Deliberately a NO-OP (returns False) when ``vm_boot`` is absent or its
    provider is unknown, and for single-provider/single-account projects where
    tasks carry no divergent constraint — so those (dominant) projects keep
    warm-reusing exactly as before. This is the cheap pre-check the poll path
    needs: it never runs full per-candidate account selection.
    """
    if not vm_boot:
        return False
    if _retarget_enabled():
        return False
    vm_provider = _norm(vm_boot.get("provider"))
    if vm_provider is None:
        return False
    constrained = _task_pinned_providers(task_provider, ctx)
    if constrained and vm_provider not in constrained:
        return True
    vm_account = _norm(vm_boot.get("account_id"))
    task_account = _norm(task_account_id)
    if vm_account and task_account and task_account != vm_account:
        return True
    vm_model = _norm(vm_boot.get("model"))
    task_model_n = _norm(task_model)
    return bool(vm_model and task_model_n and task_model_n != vm_model)

# Bounded window (seconds) for which a COMPLETED task's VM teardown is held so
# the agent can poll /api/agent/next-role and claim backlog work before the VM
# is torn down. Sized to comfortably exceed the agent's poll cadence (5s) plus
# one round-trip. If nothing is claimed within the window, teardown proceeds
# exactly as before.


def _runtime_int(key: str, fallback: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(_runtime_config.get(key, fallback) or fallback)
    except (TypeError, ValueError):
        return fallback
    if value < min_value or value > max_value:
        return fallback
    return value


def _backlog_scan_limit() -> int:
    # How many QUEUED candidates to scan per poll. A small cap keeps the
    # endpoint cheap (it can be hit by many idle VMs at up to 600/min) while
    # still giving the file-conflict / skip-locked filters enough rows to find
    # a claimable one.
    return _runtime_int(
        "warm_backlog_scan_limit",
        10,
        min_value=1,
        max_value=100,
    )


def _teardown_defer_secs() -> int:
    # Unconditional minimum hold (seconds) before activity-gating begins. Covers
    # the first /api/agent/next-role poll round-trip, which records the VM
    # heartbeat the grace loop's activity gate then reads — so the gate never
    # evicts a VM before its agent has had a chance to poll even once.
    return _runtime_int(
        "warm_backlog_teardown_defer_secs",
        20,
        min_value=5,
        max_value=120,
    )


def _grace_window_secs() -> int:
    # Bounded ceiling (seconds) for the activity-gated teardown grace window:
    # the longest a just-idle COMPLETED VM with eligible backlog is held while
    # its agent keeps polling /api/agent/next-role. Sized to the agent's poll
    # ceiling (next_role_poll_timeout_secs, default 180) so the held VM lives no
    # longer than the agent actually polls. The activity gate (below) releases
    # sooner if the agent stops heartbeating, so this is a worst-case cap, not a
    # fixed hold. The teardown loop floors this at _teardown_defer_secs() so the
    # ceiling can never undercut the unconditional minimum hold.
    return _runtime_int(
        "warm_backlog_grace_window_secs",
        180,
        min_value=5,
        max_value=600,
    )


def _grace_idle_cutoff_secs() -> int:
    # Activity gate threshold (seconds). If the held VM has not heartbeated (via
    # its /next-role poll or vm-heartbeat) within this many seconds, the agent is
    # treated as gone (gave up / process exited / VM preempted) and teardown is
    # released early rather than holding an idle VM to the ceiling. Comfortably
    # exceeds the agent's ~5s poll cadence so a couple of missed polls do not
    # evict a still-live agent.
    return _runtime_int(
        "warm_backlog_grace_idle_secs",
        30,
        min_value=10,
        max_value=120,
    )


def _grace_poll_interval_secs() -> int:
    # How often the teardown loop re-checks (claimed-work + liveness) while
    # holding the VM. Kept at the agent's poll cadence so the loop reacts within
    # one agent round-trip; clamped so a misconfigured value cannot busy-spin or
    # overshoot the gate.
    return _runtime_int(
        "warm_backlog_grace_poll_interval_secs",
        5,
        min_value=1,
        max_value=30,
    )


async def _project_in_progress_files(
    session, project_id
) -> list[tuple[frozenset, uuid.UUID | None]]:
    """Per-task production-file sets for IN_PROGRESS write tasks in a project.

    Returns a list of (files, parent_task_id) tuples, one per IN_PROGRESS
    write task. The richer per-task attribution enables the cross-parent
    hotspot exemption in _eligible_backlog_ids — callers that need only the
    flat file set can union the first elements.

    Read-only IN_PROGRESS tasks (reviews, plans, etc.) push no diff and are
    excluded, mirroring filter_file_conflicts' isolation guarantee.
    """
    rows = (
        await session.execute(
            select(Task.context, Task.payloads, Task.parent_task_id).where(
                Task.project_id == project_id,
                Task.status == TaskStatus.IN_PROGRESS,
            )
        )
    ).all()
    result: list[tuple[frozenset, uuid.UUID | None]] = []
    for ctx, payloads, parent_task_id in rows:
        if _is_read_only_payloads(payloads):
            continue
        files = _prod_files((ctx or {}).get("related_files", []))
        if files:
            result.append((frozenset(files), parent_task_id))
    return result


async def _eligible_backlog_ids(
    session,
    project_id,
    vm_instance_id: str,
    *,
    in_progress_files: "list[tuple[frozenset, uuid.UUID | None]] | None" = None,
    vm_boot: dict | None = None,
) -> list[uuid.UUID]:
    """QUEUED task ids in *project_id* an idle VM may claim, priority-ordered.

    Conservative subset of the orchestrator's dispatch gate (kept simple
    rather than replicating the full async filter stack):
      - status == QUEUED, same project_id
      - not requires_approval (orchestrator skips these too)
      - retry_after_until null or already elapsed
      - either no ``depends_on`` OR the orchestrator has already resolved
        every dependency and durably stamped ``context["_deps_satisfied"]``
        (avoids re-implementing dependency / PR-merge resolution on the hot
        path — the marker is the orchestrator's authoritative, GitHub-aware
        readiness signal). A dependency-bearing task whose deps are still
        unresolved carries no marker and continues to cold-dispatch as before.
      - not pinned to a *different* VM via ``warm_target_vm_id`` (a pin to
        this VM is the chain path's job; a pin elsewhere must not be stolen)
      - provider/account/model compatible with the reusing VM's boot manifest
        when ``vm_boot`` is supplied (see ``_vm_boot_incompatible``); a task
        pinned/recorded to a different provider cold-dispatches instead of
        running on the wrong frozen account. Omitting ``vm_boot`` disables the
        guard, and it is a no-op for single-provider projects either way.
      - no related_files overlap with an IN_PROGRESS task in the same project

    Any task type is runnable by any agent VM (the workspace is re-cloned per
    role via resetForNextRole), so no per-VM task-type capability gate is
    needed — same-project scoping is the capability boundary.

    ``in_progress_files`` lets a caller that already computed the project's
    IN_PROGRESS file set (e.g. an eligibility gate immediately followed by a
    claim) pass it in so the file-conflict scan runs once instead of once per
    call. When omitted it is computed lazily — and only if the QUEUED scan
    returned rows, so an empty backlog never pays for the IN_PROGRESS scan.
    """
    now = _utcnow()
    # A paused project's backlog is never warm-claimable. Pause is a
    # first-class persisted switch (distinct from terminal status and from
    # autonomous_enabled) that blocks ALL new dispatch — the orchestrator's
    # cold path filters it in its candidate query, and this idle-VM next-role
    # reuse path must filter it identically so a paused project can't keep
    # claiming warm VMs. The join is safe: project_id is a non-nullable FK.
    rows = (
        await session.execute(
            select(
                Task.id,
                Task.depends_on,
                Task.context,
                Task.provider,
                Task.account_id,
                Task.model,
                Task.payloads,
                Task.parent_task_id,
            )
            .join(Project, Task.project_id == Project.id)
            .where(
                Task.status == TaskStatus.QUEUED,
                Task.project_id == project_id,
                Task.requires_approval.is_(False),
                Project.paused.is_(False),
                or_(
                    Task.retry_after_until.is_(None),
                    Task.retry_after_until <= now,
                ),
            )
            .order_by(Task.priority.desc().nullslast(), Task.created_at.asc())
            .limit(_backlog_scan_limit())
        )
    ).all()

    rows_fetched = len(rows)
    saturated = rows_fetched >= _backlog_scan_limit()

    if not rows:
        logger.debug(
            "[BACKLOG-REUSE] scan project_id=%s vm_instance_id=%s "
            "rows_fetched=%d rows_eligible=0 saturated=%s",
            project_id,
            vm_instance_id,
            rows_fetched,
            saturated,
        )
        return []

    if in_progress_files is None:
        in_progress_files = await _project_in_progress_files(session, project_id)

    # Compute hotspot files from the union of QUEUED candidates + IN_PROGRESS tasks.
    # Files referenced by HOTSPOT_FILE_THRESHOLD+ tasks are exempt from cross-parent
    # deferral (see file_conflict_defers). Count occurrences per task (not per file
    # in the flat union) so a file in 3 separate tasks scores 3.
    ip_file_set: set[str] = set()
    file_counts: Counter = Counter()
    for ip_files, _ in in_progress_files:
        for f in ip_files:
            file_counts[f] += 1
        ip_file_set.update(ip_files)
    for task_id, depends_on, ctx, t_provider, t_account_id, t_model, payloads, _ in rows:  # noqa: B007
        if not _is_read_only_payloads(payloads):
            for f in _prod_files((ctx or {}).get("related_files", [])):
                file_counts[f] += 1
    hotspot_files = frozenset(f for f, n in file_counts.items() if n >= HOTSPOT_FILE_THRESHOLD)

    eligible: list[uuid.UUID] = []
    skipped_depends_on = 0
    skipped_pinned_vm = 0
    skipped_file_conflict = 0
    skipped_provider_mismatch = 0
    skipped_direct_dispatch = 0

    for task_id, depends_on, ctx, t_provider, t_account_id, t_model, payloads, t_parent_id in rows:
        ctx = ctx or {}
        # Dependency-bearing tasks are claimable warm ONLY once the
        # orchestrator's authoritative dep resolution has marked them ready
        # (``_deps_satisfied``). Without the marker the deps are still
        # unresolved, so the task stays ineligible and cold-dispatches — the
        # warm check is thus never looser than the cold dispatcher's gate.
        if depends_on and not ctx.get("_deps_satisfied"):
            skipped_depends_on += 1
            continue
        pinned_vm = ctx.get("warm_target_vm_id")
        if pinned_vm and pinned_vm != vm_instance_id:
            skipped_pinned_vm += 1
            continue
        if _vm_boot_incompatible(vm_boot, t_provider, t_account_id, t_model, ctx):
            skipped_provider_mismatch += 1
            continue
        # A warm-pool pin is owned exclusively by the warm-pool dispatch path
        # (claimed via the /next-role pool branch, origin "warm_pool"). Backlog
        # reuse must never claim it — that would mis-attribute the lift and
        # bypass the pool's lease accounting — even when it is pinned to THIS VM.
        if ctx.get("warm_pool_pin"):
            skipped_pinned_vm += 1
            continue
        # Compute-substrate types (e.g. compute_experiment) must flow through
        # the runner-aware direct-dispatch path (_dispatch_compute_experiment_directly)
        # which selects an eligible GPU runner and stamps runner_target_id.  Warm
        # backlog reuse never consults runner eligibility, so a claimed
        # compute_experiment would silently route to the cloud backend (or raise
        # ComputeLaunchPreconditionFailed when require_local_runner=true),
        # bypassing the enrolled local GPU runner.
        if _is_compute_substrate_payloads(payloads):
            skipped_direct_dispatch += 1
            continue
        # Read-only candidates (review/plan/research/...) produce no diff, so a
        # file overlap with in-flight work is never a real conflict — exempt them
        # from the overlap defer entirely, matching the cold path's candidate-side
        # exemption in filter_file_conflicts (the warm check was stricter, gating
        # on ANY single-file overlap with no read-only carve-out at all).
        if in_progress_files and not _is_read_only_payloads(payloads):
            cfiles = _prod_files(ctx.get("related_files", []))
            if cfiles & ip_file_set:
                # Check cross-parent hotspot exemption (mirrors filter_file_conflicts).
                overlapping_ip_files: set[str] = set()
                overlapping_ip_parents: set = set()
                for ip_files, ip_parent in in_progress_files:
                    if cfiles & ip_files:
                        overlapping_ip_files.update(ip_files)
                        overlapping_ip_parents.add(ip_parent)
                scope = ctx.get("scope", "small")
                if file_conflict_defers(
                    cfiles,
                    overlapping_ip_files,
                    scope=scope,
                    cand_parent=t_parent_id,
                    blocker_parents=overlapping_ip_parents,
                    hotspot_files=hotspot_files,
                ):
                    skipped_file_conflict += 1
                    continue
        eligible.append(task_id)

    # Per-poll funnel aggregation: each reason fires AT MOST ONCE per scan,
    # so one giant scan can't dominate the chart over many small ones.
    if skipped_depends_on > 0:
        record_scan_skipped_depends_on()
    if skipped_pinned_vm > 0:
        record_scan_skipped_pinned_vm()
    if skipped_file_conflict > 0:
        record_scan_skipped_file_conflict()
    if skipped_provider_mismatch > 0:
        record_scan_skipped_provider_mismatch()
    if saturated:
        record_scan_saturated()

    logger.debug(
        "[BACKLOG-REUSE] scan project_id=%s vm_instance_id=%s "
        "rows_fetched=%d rows_eligible=%d saturated=%s "
        "skipped_depends_on=%d skipped_pinned_vm=%d skipped_file_conflict=%d "
        "skipped_provider_mismatch=%d skipped_direct_dispatch=%d",
        project_id,
        vm_instance_id,
        rows_fetched,
        len(eligible),
        saturated,
        skipped_depends_on,
        skipped_pinned_vm,
        skipped_file_conflict,
        skipped_provider_mismatch,
        skipped_direct_dispatch,
    )

    return eligible


async def has_eligible_backlog_task(
    session,
    project_id,
    vm_instance_id: str,
    *,
    in_progress_files: "list[tuple[frozenset, uuid.UUID | None]] | None" = None,
    vm_boot: dict | None = None,
) -> bool:
    """True iff *project_id* has at least one backlog task this VM could claim.

    Used by the teardown-deferral decision: only worth keeping an idle VM
    alive if there is genuine pending work it can run warm.

    Pass ``in_progress_files`` to reuse an already-computed IN_PROGRESS file
    set (see ``_eligible_backlog_ids``) when this gate is paired with a claim.
    Pass ``vm_boot`` to enforce provider/account/model compatibility with the
    reusing VM's boot manifest (see ``_vm_boot_incompatible``).
    """
    return bool(
        await _eligible_backlog_ids(
            session,
            project_id,
            vm_instance_id,
            in_progress_files=in_progress_files,
            vm_boot=vm_boot,
        )
    )


async def select_backlog_reuse_candidate(
    session,
    project_id,
    vm_instance_id: str,
    *,
    in_progress_files: "list[tuple[frozenset, uuid.UUID | None]] | None" = None,
    vm_boot: dict | None = None,
) -> Task | None:
    """Atomically lock the next claimable backlog task for an idle VM, or None.

    Walks the eligible ids in priority order and locks the first one still
    QUEUED with ``SELECT ... FOR UPDATE SKIP LOCKED`` — so a concurrent
    orchestrator cold-dispatch or a peer VM's next-role poll can never
    double-claim the same row (whoever wins the lock flips it; the other
    path's lock attempt skips it and tries the next candidate).

    The returned Task is locked and still QUEUED; the caller finalizes the
    claim (status flip, telemetry, commit).

    Pass ``in_progress_files`` to reuse an already-computed IN_PROGRESS file
    set (see ``_eligible_backlog_ids``) when this claim follows an eligibility
    gate in the same flow, so the file-conflict scan is not run twice. Pass
    ``vm_boot`` to enforce provider/account/model compatibility with the
    reusing VM's boot manifest (see ``_vm_boot_incompatible``); incompatible
    tasks are excluded from the candidate set and cold-dispatch as before.

    Records the funnel skip reason for the two None-returning cases, which the
    caller cannot distinguish on its own: ``no_eligible`` when no backlog task
    qualifies, ``lost_claim_race`` when one or more qualified but a concurrent
    claimer won the SKIP-LOCKED row.
    """
    eligible = await _eligible_backlog_ids(
        session,
        project_id,
        vm_instance_id,
        in_progress_files=in_progress_files,
        vm_boot=vm_boot,
    )
    if not eligible:
        record_claim_skipped("no_eligible")
        return None
    result = await session.execute(
        select(Task)
        .join(Project, Task.project_id == Project.id)
        .where(
            Task.id.in_(eligible),
            Task.status == TaskStatus.QUEUED,
            Project.paused.is_(False),
        )
        .order_by(Task.priority.desc().nullslast(), Task.created_at.asc())
        # of=Task locks ONLY the candidate Task row, not the joined Project
        # row. All backlog candidates belong to one project, so locking the
        # shared Project row would let skip_locked silently drop a claimable
        # task whenever a sibling claim in the same project held it (mirrors
        # agent_warm_continuity.py:750/:926).
        .with_for_update(skip_locked=True, of=Task)
        .limit(1)
    )
    claimed = result.scalars().first()
    logger.debug(
        "[BACKLOG-REUSE] claim project_id=%s vm_instance_id=%s "
        "task_id=%s rows_eligible=%d",
        project_id,
        vm_instance_id,
        claimed.id if claimed is not None else None,
        len(eligible),
    )
    if claimed is None:
        record_claim_skipped("lost_claim_race")
    return claimed


# ---------------------------------------------------------------------------
# Funnel observability counters
# ---------------------------------------------------------------------------
# Rolling deques for each funnel stage. Each entry is a UTC datetime (or a
# (datetime, reason) tuple for claim_skipped). Bounded by maxlen so they
# never grow without bound on a long-lived instance.

_vms_with_eligible_backlog_events: deque = deque(maxlen=500)
_teardown_defers_events: deque = deque(maxlen=500)
_next_role_polls_during_defer_events: deque = deque(maxlen=500)
_warm_claims_succeeded_events: deque = deque(maxlen=500)
_claim_skipped_events: deque = deque(maxlen=500)  # (datetime, reason)
# Completion-side trigger stage: each entry is a (datetime, count) tuple, one
# per COMPLETION event that proactively stamped ``_deps_satisfied`` on >=1
# just-unblocked dependent (so the just-completed VM can warm-claim that
# follow-on work inside its teardown-defer grace window rather than tearing
# down cold while the dependent waits for a later dispatch scan). count is the
# number of dependents marked by that event. Recorded by the completion-side
# trigger that owns the marking (orchestrator/lifecycle/reaper termination
# paths); surfaced here so the trigger's lift is visible alongside the
# unresolved-dependency scan skips it is designed to drive down.
_completion_dependents_marked_events: deque = deque(maxlen=500)  # (datetime, count)

# Eligibility-scan skip-reason events. Each entry is a single _utcnow() per
# poll where that reason fired (one or more rows skipped). This per-poll
# aggregation matches the semantics of _claim_skipped_events: one big scan
# that skipped 8 rows for the same reason counts as one event, not eight.
_scan_skipped_depends_on_events: deque = deque(maxlen=500)
_scan_skipped_pinned_vm_events: deque = deque(maxlen=500)
_scan_skipped_file_conflict_events: deque = deque(maxlen=500)
_scan_skipped_provider_mismatch_events: deque = deque(maxlen=500)
_scan_saturated_events: deque = deque(maxlen=500)

# Grace-window (activity-gated teardown hold) stage events. Each entry is a
# single _utcnow() per occurrence. Extensions count how many poll intervals a
# held VM survived because its agent was still polling; idle-releases and
# ceiling-releases count the two bounded exits when no claim happened.
_grace_window_extension_events: deque = deque(maxlen=500)
_grace_idle_release_events: deque = deque(maxlen=500)
_grace_ceiling_release_events: deque = deque(maxlen=500)


def record_scan_skipped_depends_on() -> None:
    """Stage A: an eligibility scan skipped >=1 candidate for unresolved depends_on."""
    _scan_skipped_depends_on_events.append(_utcnow())


def record_scan_skipped_pinned_vm() -> None:
    """Stage A: an eligibility scan skipped >=1 candidate pinned to a different VM."""
    _scan_skipped_pinned_vm_events.append(_utcnow())


def record_scan_skipped_file_conflict() -> None:
    """Stage A: an eligibility scan skipped >=1 candidate via related_files conflict."""
    _scan_skipped_file_conflict_events.append(_utcnow())


def record_scan_skipped_provider_mismatch() -> None:
    """Stage A: an eligibility scan skipped >=1 candidate whose pinned/recorded
    provider/account/model is incompatible with the reusing VM's boot manifest."""
    _scan_skipped_provider_mismatch_events.append(_utcnow())


def record_scan_saturated() -> None:
    """Stage A: an eligibility scan hit the per-poll _backlog_scan_limit()
    (runtime key ``warm_backlog_scan_limit``)."""
    _scan_saturated_events.append(_utcnow())


def record_grace_window_extension() -> None:
    """Grace stage: a held VM survived another poll interval because its agent
    was still actively polling (activity gate held the VM open)."""
    _grace_window_extension_events.append(_utcnow())


def record_grace_idle_release() -> None:
    """Grace stage: a held VM was torn down early because its agent stopped
    heartbeating before the bounded ceiling (activity gate fired)."""
    _grace_idle_release_events.append(_utcnow())


def record_grace_ceiling_release() -> None:
    """Grace stage: a held VM reached the bounded grace ceiling without claiming
    work and was torn down."""
    _grace_ceiling_release_events.append(_utcnow())


def record_vm_with_eligible_backlog() -> None:
    """Stage 1: a COMPLETION event found ≥1 eligible QUEUED backlog task."""
    _vms_with_eligible_backlog_events.append(_utcnow())


def record_teardown_defer() -> None:
    """Stage 2: VM teardown was deferred for the warm-backlog reuse window."""
    _teardown_defers_events.append(_utcnow())


def record_next_role_poll_during_defer() -> None:
    """Stage 3: a /next-role poll arrived inside the teardown-defer window."""
    _next_role_polls_during_defer_events.append(_utcnow())


def record_warm_claim_succeeded() -> None:
    """Stage 4: a /next-role poll claimed a backlog task (vm_warm_start=True)."""
    _warm_claims_succeeded_events.append(_utcnow())


def record_claim_skipped(reason: str) -> None:
    """Stage 4 miss: poll arrived but did not claim.

    reason should be one of: "no_eligible", "lost_claim_race",
    "circuit_breaker_tripped".
    """
    _claim_skipped_events.append((_utcnow(), reason))


def record_completion_dependents_marked(count: int) -> None:
    """Completion-side trigger: a finished task proactively stamped the sticky
    ``_deps_satisfied`` readiness marker on *count* just-unblocked QUEUED
    dependents, making them warm-claimable within the just-completed VM's
    teardown-defer grace window instead of waiting for a later dispatch scan.

    No-ops on a non-positive count so callers can record unconditionally.
    """
    if count and count > 0:
        _completion_dependents_marked_events.append((_utcnow(), count))


def get_reuse_funnel_snapshot(window_hours: int = 24) -> dict:
    """Return rolling funnel counts for the warm-reuse claim pipeline.

    All counts cover the trailing ``window_hours`` (default 24h).
    """
    cutoff = _utcnow() - timedelta(hours=window_hours)
    skipped_by_reason: dict = {}
    for ts, reason in _claim_skipped_events:
        if ts >= cutoff:
            skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
    return {
        "window_hours": window_hours,
        "completed_vms_with_eligible_backlog": sum(
            1 for ts in _vms_with_eligible_backlog_events if ts >= cutoff
        ),
        "teardown_defers_scheduled": sum(
            1 for ts in _teardown_defers_events if ts >= cutoff
        ),
        "next_role_polls_during_defer": sum(
            1 for ts in _next_role_polls_during_defer_events if ts >= cutoff
        ),
        "warm_claims_succeeded": sum(
            1 for ts in _warm_claims_succeeded_events if ts >= cutoff
        ),
        "completion_dependents_marked": sum(
            count for ts, count in _completion_dependents_marked_events if ts >= cutoff
        ),
        "claim_skipped_by_reason": skipped_by_reason,
        "claim_skipped_total": sum(skipped_by_reason.values()),
        "scan_polls_with_depends_on_skip": sum(
            1 for ts in _scan_skipped_depends_on_events if ts >= cutoff
        ),
        "scan_polls_with_pinned_vm_skip": sum(
            1 for ts in _scan_skipped_pinned_vm_events if ts >= cutoff
        ),
        "scan_polls_with_file_conflict_skip": sum(
            1 for ts in _scan_skipped_file_conflict_events if ts >= cutoff
        ),
        "scan_polls_with_provider_mismatch_skip": sum(
            1 for ts in _scan_skipped_provider_mismatch_events if ts >= cutoff
        ),
        "scan_polls_saturated": sum(
            1 for ts in _scan_saturated_events if ts >= cutoff
        ),
        "grace_window_extensions": sum(
            1 for ts in _grace_window_extension_events if ts >= cutoff
        ),
        "grace_idle_releases": sum(
            1 for ts in _grace_idle_release_events if ts >= cutoff
        ),
        "grace_ceiling_releases": sum(
            1 for ts in _grace_ceiling_release_events if ts >= cutoff
        ),
    }


# Gate for naming a single dominant reuse drop-out. Mirrors the
# dominant-blocker gating in digest_metrics: a reason is named the leader only
# when it is present at meaningful volume (>=2 events), covers at least half the
# non-converting population, AND strictly outranks the runner-up (an even split
# names no leader). Below the gate, dominant_dropout is None.
_DOMINANT_DROPOUT_MIN_COUNT = 2
_DOMINANT_DROPOUT_MIN_SHARE = 0.5

# Human-readable funnel keys that represent a completed-VM-with-eligible-backlog
# failing to convert to a warm claim, mapped to a ranking reason label. Each is
# an independent rolling event stream (per-poll or per-occurrence), NOT a per-VM
# record, so the ranking reflects event magnitude over the window rather than
# exact per-VM causation (stated in attribution_note below).
_DROPOUT_REASON_KEYS = (
    ("grace_idle_releases", "grace_idle_release"),
    ("grace_ceiling_releases", "grace_ceiling_release"),
    ("scan_polls_with_depends_on_skip", "scan_depends_on"),
    ("scan_polls_with_pinned_vm_skip", "scan_pinned_vm"),
    ("scan_polls_with_file_conflict_skip", "scan_file_conflict"),
    ("scan_polls_saturated", "scan_saturated"),
)


def compute_dominant_reuse_dropout(window_hours: int = 24) -> dict:
    """Rank where warm-reuse leaks and name the single dominant drop-out.

    Derives a per-reason drop-out breakdown from the rolling reuse funnel
    (``get_reuse_funnel_snapshot``): every per-claim skip reason plus the
    grace-window releases and eligibility-scan skips that prevented a completed
    VM with eligible backlog from converting to a warm claim. The dominant
    drop-out is named only when one reason clearly dominates the non-converting
    population (see the gating constants); otherwise ``dominant_dropout`` is
    None. Suitable for daily persistence (efficiency snapshot) and admin
    surfacing so a widening change can be confirmed to reduce the leader.
    """
    snap = get_reuse_funnel_snapshot(window_hours)
    dropout_by_reason: dict = {}
    for reason, count in (snap.get("claim_skipped_by_reason") or {}).items():
        if count:
            dropout_by_reason[f"claim_{reason}"] = count
    for key, reason in _DROPOUT_REASON_KEYS:
        count = snap.get(key) or 0
        if count:
            dropout_by_reason[reason] = count

    total = sum(dropout_by_reason.values())
    dominant = None
    if total > 0:
        ranked = sorted(dropout_by_reason.items(), key=lambda kv: (-kv[1], kv[0]))
        top_reason, top_count = ranked[0]
        second_count = ranked[1][1] if len(ranked) > 1 else 0
        if (
            top_count >= _DOMINANT_DROPOUT_MIN_COUNT
            and top_count / total >= _DOMINANT_DROPOUT_MIN_SHARE
            and top_count > second_count
        ):
            dominant = {
                "reason": top_reason,
                "count": top_count,
                "total": total,
                "share": round(top_count / total, 3),
            }
    return {
        "window_hours": window_hours,
        "dropout_by_reason": dropout_by_reason,
        "dropout_total": total,
        "dominant_dropout": dominant,
        "attribution_note": (
            "Drop-out reasons are independent rolling event-stream counts, not "
            "per-VM-linked records; the ranking reflects event magnitude over "
            "the window rather than exact per-VM causation."
        ),
    }


# ---------------------------------------------------------------------------
# Warm-POOL dispatch funnel observability counters (direction d-5fd69fe1)
# ---------------------------------------------------------------------------
# Pool-hit-vs-cold-dispatch funnel for the pre-provisioned VM pool. Mirrors the
# backlog-reuse funnel above: bounded rolling deques of UTC timestamps (plus a
# (datetime, reason) tuple for the multi-reason lease-miss stage), maxlen=500 to
# bound memory on a long-lived process. The dispatch hook (sibling task) calls
# these as it decides between leasing an IDLE pool member (a "hit") and
# cold-provisioning a fresh VM (a "cold dispatch"); get_pool_funnel_snapshot
# rolls them up for the operator panel.
#
# Per-account-mix dimension (direction d-5fd69fe1, telemetry cluster): each
# hit / cold / miss event carries the resolved account label of the dispatch
# so an operator can answer, PER ACCOUNT, how many dispatches the pool served
# warm vs cold and the per-account hit rate -- the judgment a controlled
# enable's 'retire on success or pivot' decision depends on. The ``account``
# argument is optional and defaults to None so existing call sites keep working
# unchanged; un-attributed events bucket under ``_UNATTRIBUTED_ACCOUNT`` in the
# per-account breakdown while still counting toward the fleet-wide totals.

_pool_lease_hit_events: deque = deque(maxlen=500)  # (datetime, account, project_id)
_pool_cold_dispatch_events: deque = deque(maxlen=500)  # (datetime, account, project_id)
_pool_lease_miss_events: deque = deque(maxlen=500)  # (datetime, reason, account, project_id)

# Bucket label for dispatches recorded without a resolved account (e.g. legacy
# call sites that have not yet been wired to pass identity).
_UNATTRIBUTED_ACCOUNT = "unattributed"

# Lease-miss reasons that represent a dispatch the pool was genuinely ELIGIBLE to
# serve but couldn't — the only misses that belong in the hit-rate denominator
# the cost guard acts on. Everything else is structural non-eligibility, not a
# pool capability failure, and must be excluded so it never drags the rate down:
#   - "out_of_scope": the task's project is not in the warm-pool allowlist — the
#     pool was never eligible (different tenant scope), so it can never "hit".
#   - "suppressed": the claim-failure circuit breaker paused the pool itself.
#   - "cost_guard": the cost guard paused leasing — counting its OWN suppression
#     toward the rate it gates on makes the guard unable to ever disengage
#     (a self-reinforcing drain spiral).
# Tasks that never CONSULT the pool (a pinned warm target, dependencies, or no
# resolved runtime account) record neither a hit nor a miss, so they fall out of
# this denominator automatically — only ``record_pool_cold_dispatch`` fires for
# them, which keeps the raw fleet-wide ``pool_hit_rate`` (visibility only).
POOL_ELIGIBLE_MISS_REASONS = frozenset(
    {"no_idle_member", "no_compatible_member", "doomed_vm", "lease_error"}
)


def _account_label(account: str | None) -> str:
    return account or _UNATTRIBUTED_ACCOUNT


def _eligible_miss_count(miss_by_reason: dict) -> int:
    """Sum the misses that count toward the eligible (pool-was-able-to-serve) rate."""
    return sum(
        n for reason, n in miss_by_reason.items()
        if reason in POOL_ELIGIBLE_MISS_REASONS
    )


def record_pool_lease_hit(account: str | None = None, *, project_id=None) -> None:
    """A queued task was served warm by leasing an IDLE pool member.

    ``account`` is the resolved account id the dispatch was served for; pass it
    to attribute the warm claim in the per-account breakdown. ``project_id``
    attributes the event to a specific project for the per-project funnel.
    """
    _pool_lease_hit_events.append((_utcnow(), account, project_id))


def record_pool_cold_dispatch(account: str | None = None, *, project_id=None) -> None:
    """A queued task cold-provisioned a VM (pool disabled, empty, or no match).

    ``account`` is the resolved account id the dispatch was served for; pass it
    to attribute the cold dispatch in the per-account breakdown. ``project_id``
    attributes the event to a specific project for the per-project funnel.
    """
    _pool_cold_dispatch_events.append((_utcnow(), account, project_id))


def record_pool_lease_miss(
    reason: str, account: str | None = None, *, project_id=None
) -> None:
    """The pool was consulted but no member could be leased.

    reason should be one of: "no_idle_member" (pool empty / all leased),
    "no_compatible_member" (idle members exist but identity/tenant mismatch),
    "disabled" (warm_pool_enabled off), "out_of_scope" (task project not in the
    warm-pool allowlist — dispatch honors the same per-project scope the
    maintainer enforces), "lost_lease_race" (a peer dispatcher won the
    SKIP-LOCKED row). ``account`` is the resolved account id the lease was
    attempted for; pass it to attribute the miss per account. ``project_id``
    attributes the event to a specific project for the per-project funnel.
    """
    _pool_lease_miss_events.append((_utcnow(), reason, account, project_id))


def get_pool_funnel_snapshot(window_hours: int = 24) -> dict:
    """Return rolling funnel counts for the warm-pool dispatch path.

    All counts cover the trailing ``window_hours`` (default 24h). ``pool_hit_rate``
    is hits / (hits + cold dispatches), or None when no dispatch was recorded —
    a fleet-wide, visibility-only figure that includes dispatches the pool was
    never eligible to serve (other projects, never-consulted cold provisions).

    ``eligible_hit_rate`` is hits / (hits + eligible misses), where eligible
    misses are only those in :data:`POOL_ELIGIBLE_MISS_REASONS` (the pool was
    genuinely able to serve the task but couldn't). It excludes out-of-scope,
    breaker-suppressed, and cost-guard misses, plus never-consulted cold
    dispatches — so a pool scoped to one project is judged on the demand it could
    actually serve. ``eligible_dispatches`` is that denominator. This is the rate
    the cost guard acts on; see :func:`app.warm_pool_maintainer.evaluate_pool_cost_guard`.

    ``by_account`` breaks the same funnel down by resolved account (raw and
    eligible) so an operator can read, per account, the warm-claim / cold-dispatch
    counts, hit rates, and lease-miss reasons during a controlled enable. Events
    recorded without an account bucket under ``_UNATTRIBUTED_ACCOUNT``.
    """
    cutoff = _utcnow() - timedelta(hours=window_hours)

    def _new_bucket() -> dict:
        return {
            "pool_lease_hits": 0,
            "cold_dispatches": 0,
            "lease_miss_by_reason": {},
        }

    by_account: dict = {}
    miss_by_reason: dict = {}
    hits = 0
    cold = 0
    for ts, account, _pid in _pool_lease_hit_events:
        if ts >= cutoff:
            hits += 1
            by_account.setdefault(_account_label(account), _new_bucket())[
                "pool_lease_hits"
            ] += 1
    for ts, account, _pid in _pool_cold_dispatch_events:
        if ts >= cutoff:
            cold += 1
            by_account.setdefault(_account_label(account), _new_bucket())[
                "cold_dispatches"
            ] += 1
    for ts, reason, account, _pid in _pool_lease_miss_events:
        if ts >= cutoff:
            miss_by_reason[reason] = miss_by_reason.get(reason, 0) + 1
            bucket = by_account.setdefault(_account_label(account), _new_bucket())
            bucket["lease_miss_by_reason"][reason] = (
                bucket["lease_miss_by_reason"].get(reason, 0) + 1
            )

    # Finalize each account bucket with its own totals + hit rate. The eligible
    # rate counts only hits + pool-eligible misses (see POOL_ELIGIBLE_MISS_REASONS)
    # — the raw rate keeps cold dispatches in the denominator for visibility.
    for bucket in by_account.values():
        acct_total = bucket["pool_lease_hits"] + bucket["cold_dispatches"]
        bucket["total_dispatches"] = acct_total
        bucket["pool_hit_rate"] = (
            round(bucket["pool_lease_hits"] / acct_total, 4) if acct_total else None
        )
        bucket["lease_miss_total"] = sum(bucket["lease_miss_by_reason"].values())
        acct_elig_miss = _eligible_miss_count(bucket["lease_miss_by_reason"])
        acct_elig_total = bucket["pool_lease_hits"] + acct_elig_miss
        bucket["eligible_miss_total"] = acct_elig_miss
        bucket["eligible_dispatches"] = acct_elig_total
        bucket["eligible_hit_rate"] = (
            round(bucket["pool_lease_hits"] / acct_elig_total, 4)
            if acct_elig_total
            else None
        )

    total = hits + cold
    # Eligible funnel: hits + only the misses where the pool was genuinely able to
    # serve the task. This is the rate the cost guard gates on, so structural
    # non-eligibility (other projects, the pool's own breaker/cost-guard pauses,
    # and never-consulted cold dispatches) can't crush a scoped pool's measured
    # hit rate and trigger a self-reinforcing drain.
    eligible_misses = _eligible_miss_count(miss_by_reason)
    eligible_total = hits + eligible_misses
    return {
        "window_hours": window_hours,
        "pool_lease_hits": hits,
        "cold_dispatches": cold,
        "total_dispatches": total,
        "pool_hit_rate": round(hits / total, 4) if total else None,
        "eligible_miss_total": eligible_misses,
        "eligible_dispatches": eligible_total,
        "eligible_hit_rate": (
            round(hits / eligible_total, 4) if eligible_total else None
        ),
        "lease_miss_by_reason": miss_by_reason,
        "lease_miss_total": sum(miss_by_reason.values()),
        "by_account": by_account,
    }


def get_project_pool_funnel_snapshot(project_id, window_hours: int = 24) -> dict:
    """Per-project rolling funnel snapshot for the warm-pool dispatch path.

    Identical semantics to :func:`get_pool_funnel_snapshot` but filtered to
    events attributed to ``project_id``.  Only events recorded with a matching
    ``project_id`` keyword argument are counted — events recorded without a
    project_id are excluded.

    ``eligible_hit_rate`` and ``eligible_dispatches`` use the same
    :data:`POOL_ELIGIBLE_MISS_REASONS` exclusion set as the fleet guard, so
    out-of-scope, suppressed, and cost_guard misses never enter a project's
    denominator.  This is the rate :func:`app.warm_pool_maintainer.\
evaluate_project_pool_cost_guard` acts on.
    """
    cutoff = _utcnow() - timedelta(hours=window_hours)
    pid = str(project_id) if project_id is not None else None
    miss_by_reason: dict = {}
    hits = 0
    for ts, _account, event_pid in _pool_lease_hit_events:
        if ts >= cutoff and str(event_pid) == pid:
            hits += 1
    for ts, reason, _account, event_pid in _pool_lease_miss_events:
        if ts >= cutoff and str(event_pid) == pid:
            miss_by_reason[reason] = miss_by_reason.get(reason, 0) + 1

    eligible_misses = _eligible_miss_count(miss_by_reason)
    eligible_total = hits + eligible_misses
    return {
        "window_hours": window_hours,
        "pool_lease_hits": hits,
        "eligible_miss_total": eligible_misses,
        "eligible_dispatches": eligible_total,
        "eligible_hit_rate": (
            round(hits / eligible_total, 4) if eligible_total else None
        ),
        "lease_miss_by_reason": miss_by_reason,
    }


def compute_dominant_pool_miss(window_hours: int = 24) -> dict:
    """Rank pool lease-miss reasons and name the single dominant miss.

    Mirrors :func:`compute_dominant_reuse_dropout` for the POOL dispatch funnel
    (a DIFFERENT funnel from the backlog reuse-dropout one): derives the
    lease-miss-by-reason breakdown from :func:`get_pool_funnel_snapshot` and
    names a ``dominant_miss`` reason only when one clearly dominates the miss
    population — present at meaningful volume (>=2 events), covering at least
    half the misses, AND strictly outranking the runner-up (an even split names
    no leader). The full funnel aggregates (hits, cold dispatches, overall and
    per-account hit rate, the miss breakdown) ride along so the whole block can
    be folded into the daily efficiency snapshot and survive the restarts that
    wipe the in-memory funnel counters.

    Why the miss breakdown matters: when the pool is missing, WHY drives the
    next action. A funnel dominated by ``no_compatible_member`` (idle VM on the
    wrong account/provider/model) says enable account-retarget or rebalance
    provisioning; one dominated by ``no_idle_member`` says grow the pool or the
    demand is too bursty for the current target size. Losing that breakdown on
    every restart makes the enable-and-tune loop blind.
    """
    snap = get_pool_funnel_snapshot(window_hours)
    miss_by_reason = dict(snap.get("lease_miss_by_reason") or {})
    total = sum(miss_by_reason.values())
    dominant = None
    if total > 0:
        ranked = sorted(miss_by_reason.items(), key=lambda kv: (-kv[1], kv[0]))
        top_reason, top_count = ranked[0]
        second_count = ranked[1][1] if len(ranked) > 1 else 0
        if (
            top_count >= _DOMINANT_DROPOUT_MIN_COUNT
            and top_count / total >= _DOMINANT_DROPOUT_MIN_SHARE
            and top_count > second_count
        ):
            dominant = {
                "reason": top_reason,
                "count": top_count,
                "total": total,
                "share": round(top_count / total, 3),
            }
    return {
        "window_hours": snap.get("window_hours"),
        "pool_lease_hits": snap.get("pool_lease_hits"),
        "cold_dispatches": snap.get("cold_dispatches"),
        "total_dispatches": snap.get("total_dispatches"),
        "pool_hit_rate": snap.get("pool_hit_rate"),
        "lease_miss_by_reason": miss_by_reason,
        "lease_miss_total": snap.get("lease_miss_total"),
        "dominant_miss": dominant,
        "by_account": snap.get("by_account"),
    }


# ---------------------------------------------------------------------------
# Chain-continuity funnel observability counters
# ---------------------------------------------------------------------------
# Mirrors the backlog-reuse funnel above for the chain-continuity branch of
# /next-role. Each entry is a UTC datetime (or (datetime, reason) tuple for
# the multi-reason skipped stage). Bounded by maxlen=500 to prevent
# unbounded growth on a long-lived process.

_chain_poll_events: deque = deque(maxlen=500)
_chain_claimed_events: deque = deque(maxlen=500)
_chain_no_pinned_events: deque = deque(maxlen=500)
_chain_other_vm_pr_events: deque = deque(maxlen=500)
_chain_doomed_events: deque = deque(maxlen=500)
_chain_skipped_events: deque = deque(maxlen=500)  # (datetime, reason)


def record_chain_poll() -> None:
    """Total poll arrivals: every /next-role call that enters the chain branch."""
    _chain_poll_events.append(_utcnow())


def record_chain_claimed() -> None:
    """Poll successfully claimed a warm-pinned chain task via _finalize_warm_claim."""
    _chain_claimed_events.append(_utcnow())


def record_chain_no_pinned() -> None:
    """Poll found no pinned task (chain was active) and returned 204."""
    _chain_no_pinned_events.append(_utcnow())


def record_chain_other_vm_pr() -> None:
    """Poll exited because another VM owns an IN_PROGRESS task for this PR."""
    _chain_other_vm_pr_events.append(_utcnow())


def record_chain_doomed() -> None:
    """Poll returned 204 because the VM is scheduled for orchestrator-initiated deletion."""
    _chain_doomed_events.append(_utcnow())


def record_chain_skipped(reason: str) -> None:
    """Chain lookup was skipped; reason is 'disabled' or 'breaker_tripped'."""
    _chain_skipped_events.append((_utcnow(), reason))


def get_chain_continuity_funnel_snapshot(window_hours: int = 24) -> dict:
    """Return rolling funnel counts for the chain-continuity claim path.

    All counts cover the trailing ``window_hours`` (default 24h).
    """
    cutoff = _utcnow() - timedelta(hours=window_hours)
    skipped_by_reason: dict = {}
    for ts, reason in _chain_skipped_events:
        if ts >= cutoff:
            skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
    return {
        "window_hours": window_hours,
        "poll_arrivals": sum(1 for ts in _chain_poll_events if ts >= cutoff),
        "chain_claimed": sum(1 for ts in _chain_claimed_events if ts >= cutoff),
        "no_pinned_task": sum(1 for ts in _chain_no_pinned_events if ts >= cutoff),
        "another_vm_owns_pr": sum(
            1 for ts in _chain_other_vm_pr_events if ts >= cutoff
        ),
        "doomed_vm": sum(1 for ts in _chain_doomed_events if ts >= cutoff),
        "skipped_by_reason": skipped_by_reason,
        "skipped_total": sum(skipped_by_reason.values()),
    }


# ---------------------------------------------------------------------------
# Spawn-time warm-pin funnel observability counters
# ---------------------------------------------------------------------------
# Mirrors the chain-continuity funnel above, but for the *spawn-side* warm-pin
# decision in review_pipeline._spawn_pipeline_task: when a review/fix/audit/perf
# task is created, did it get pinned to the source task's warm VM or not, and
# if not, why? Each entry is a UTC datetime (or (datetime, reason) tuple for
# the multi-reason skipped stage). Bounded by maxlen=500 to prevent unbounded
# growth on a long-lived process.

_spawn_attempt_events: deque = deque(maxlen=500)
_spawn_pin_set_events: deque = deque(maxlen=500)
_spawn_pin_skipped_events: deque = deque(maxlen=500)  # (datetime, reason)


def record_spawn_attempt() -> None:
    """Stage 1: a pipeline spawn reached the warm-pin decision block."""
    _spawn_attempt_events.append(_utcnow())


def record_spawn_pin_set() -> None:
    """Stage 2 (success): the spawned task was pinned to the source VM."""
    _spawn_pin_set_events.append(_utcnow())


def record_spawn_pin_skipped(reason: str) -> None:
    """Stage 2 (miss): warm-pin skipped.

    reason should be one of: "disabled" (warm_continuity_enabled flag off),
    "breaker_tripped" (is_warm_continuity_suppressed() true),
    "caller_suppressed" (suppress_warm_handoff=True),
    "source_cold" (source task has no vm_instance_id),
    "stale_source" (source heartbeat older than WARM_HANDOFF_WINDOW_SECS),
    "project_paused" (owning project is paused — no new dispatch on any path),
    "sibling_already_pinned" (an unclaimed QUEUED task is already pinned to
    the same source VM for this PR — WI-5 anti-re-pin guard).
    """
    _spawn_pin_skipped_events.append((_utcnow(), reason))


def get_spawn_pin_funnel_snapshot(window_hours: int = 24) -> dict:
    """Return rolling funnel counts for the spawn-time warm-pin decision.

    All counts cover the trailing ``window_hours`` (default 24h).
    """
    cutoff = _utcnow() - timedelta(hours=window_hours)
    skipped_by_reason: dict = {}
    for ts, reason in _spawn_pin_skipped_events:
        if ts >= cutoff:
            skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
    return {
        "window_hours": window_hours,
        "spawn_attempts": sum(1 for ts in _spawn_attempt_events if ts >= cutoff),
        "pin_set": sum(1 for ts in _spawn_pin_set_events if ts >= cutoff),
        "skipped_by_reason": skipped_by_reason,
        "skipped_total": sum(skipped_by_reason.values()),
    }
