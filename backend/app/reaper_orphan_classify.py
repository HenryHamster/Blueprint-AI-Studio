"""Orphan-classification helpers extracted from reaper.py.

Pure (or near-pure) functions for orphan priority bucketing, selection,
VM-contact predicates, and the _classify_orphan() state machine. No
dependency on the reaper service loop — extracting these makes
_classify_orphan() unit-testable in isolation.
"""

import uuid
from datetime import datetime, timedelta

from sqlmodel import select

from app.failure_classification import RATE_LIMIT
from app.models import Artifact, ArtifactType, LocalRunner, TaskStatus
from app.schemas import redact_command_log_text
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _coerce_naive_utc

# Orphan-priority buckets (lower = processed first). Time-sensitive orphans are
# reaped before the cap can truncate the tail.
_ORPHAN_PRIORITY_RATE_LIMITED = 0  # rate-limited / quota — reroute ASAP
_ORPHAN_PRIORITY_SPOT = 1  # spot VM — likelier to be preempted again
_ORPHAN_PRIORITY_PROVISION = 2  # stuck PROVISIONING — never got a heartbeat
_ORPHAN_PRIORITY_DEFAULT = 3  # everything else

# Bespoke handlers that do not invoke an LLM and therefore cannot emit the
# tool-use events consumed by the progress-stale watchdog. Their direct work is
# observable through the ordinary agent heartbeat only:
#
# * verify_tests runs detected build/test subprocesses directly; a healthy suite
#   can exceed the small-scope 480s progress cutoff.
# * compute_experiment launches a backend-managed job and parks on its durable
#   terminal wake; it likewise has no LLM/tool-event stream while launch is in
#   flight.
#
# This is deliberately a progress-only exemption. Heartbeat-stale, missing-
# heartbeat, provisioning, and absolute-ceiling branches run before the guarded
# progress checks below and remain fully effective.
_HEARTBEAT_ONLY_PROGRESS_TASK_TYPES = frozenset(
    {
        "verify_tests",
        "compute_experiment",
    }
)


def _uses_heartbeat_only_progress(task) -> bool:
    """True for direct handlers that cannot produce LLM tool-use events."""
    payload = task.first_payload or {}
    return payload.get("type") in _HEARTBEAT_ONLY_PROGRESS_TASK_TYPES


def _orphan_priority_key(task) -> int:
    """Cheap priority bucket for one orphan, computed ONLY from already-loaded
    columns (status, latest_status_message, context, vm_spot) — NO extra
    queries. Lower number = processed first so time-sensitive orphans survive a
    truncating cap. Pure + side-effect-free for unit testing.

    Order: rate-limited (0) < spot (1) < provision-failure (2) < other (3).
    """
    # 0: rate-limited / quota — reroute to a fresh pool account ASAP.
    msg = (getattr(task, "latest_status_message", None) or "").lower()
    ctx = getattr(task, "context", None) or {}
    failure_category = ctx.get("failure_category")
    if (
        "rate" in msg
        or "429" in msg
        or "quota" in msg
        or failure_category == RATE_LIMIT
    ):
        return _ORPHAN_PRIORITY_RATE_LIMITED
    # 1: spot VMs — cheaper but preemptible; recover before they churn again.
    if getattr(task, "vm_spot", None) is True:
        return _ORPHAN_PRIORITY_SPOT
    # 2: provision-failures — VM died before first heartbeat; no agent state lost.
    if getattr(task, "status", None) == TaskStatus.PROVISIONING:
        return _ORPHAN_PRIORITY_PROVISION
    # 3: everything else.
    return _ORPHAN_PRIORITY_DEFAULT


def _select_orphans_for_cycle(tasks: list, cap: int) -> tuple[list, int]:
    """Sort ``tasks`` by ascending priority bucket and return the top ``cap``
    plus the number deferred. Sort is stable (Python's sort), so within a bucket
    the original orphan-detection order is preserved. Pure helper so the
    cap+priority logic is unit-testable without the full reaper loop.

    Returns ``(selected, deferred_count)``. A non-positive cap is treated as
    "no cap" (process everything) to fail safe if misconfigured.
    """
    ordered = sorted(tasks, key=_orphan_priority_key)
    if cap is None or cap <= 0 or len(ordered) <= cap:
        return ordered, 0
    return ordered[:cap], len(ordered) - cap


def _is_multiplexed_vm_instance_id(vm_id) -> bool:
    """True iff *vm_id* is a fleet-runner id, i.e. SHARED by many concurrent tasks.

    The single definition of "is this vm id multiplexed", shared by the WRITER
    (``routers/agent.py::_record_vm_contact``, which decides whether to write a
    ``taskcontact`` row) and the READERS (``_is_multiplexed_vm_id`` below, the
    reaper's candidate filters, and ``reaper_relock._relock_contact_at``).

    They MUST agree. If the writer ever stopped writing a row that a reader still
    expects, that reader would see an ABSENT row and fall back to the SHARED
    ``vmcontact`` row — silently restoring the exact sibling-masking bug this
    table was added to kill, with no error anywhere. Keeping one predicate makes
    that divergence impossible to introduce by editing one side.
    """
    return isinstance(vm_id, str) and vm_id.startswith("runner:")


def _is_multiplexed_vm_id(task) -> bool:
    """True iff this task's vm id is a fleet runner, i.e. SHARED with sibling tasks.

    ``vmcontact`` is primary-keyed on ``vm_instance_id`` — "one row per VM". For a
    GCE agent VM that identity is also "one row per TASK" (one task per VM), which
    is what the off-row contact/progress overrides below silently assume. For a
    fleet runner it is FALSE: ``scripts/local_runner.py`` multiplexes many
    concurrent tasks onto one ``vm_instance_id='runner:<id>'``, so ALL of them
    share one ``vmcontact`` row and ANY agent's heartbeat advances it for
    everybody. Reading that shared row as if it were a per-task signal is what
    let a hung agent hide behind a working sibling forever (2026-07-12 zombie
    class); for these vm ids the overrides must consult the TASK-scoped
    ``taskcontact`` row instead.
    """
    return _is_multiplexed_vm_instance_id(getattr(task, "vm_instance_id", None))


def _has_task_contact_row(task, task_contact_lookup: dict | None) -> bool:
    """True iff a TASK-scoped ``taskcontact`` row exists for this task.

    This is the rollout-safety oracle, and the reason the caller passes a
    ``{task_id: last_contact_at}`` map rather than only the nullable tool-use map:
    ``taskcontact.last_contact_at`` is NOT NULL, so presence in that map is exactly
    "a row exists". The distinction matters enormously —

      * row ABSENT  → we have no task-scoped truth (task predates this fix, or has
        not heartbeated since deploy). Absence is NEVER evidence of death: fall
        back to the shared ``vmcontact`` row, i.e. today's behaviour.
      * row PRESENT → it is authoritative for this task. Do NOT fall back to the
        shared row even when the task-scoped stamp is NULL or stale — falling back
        there is precisely the sibling-masking bug being fixed (a hung agent that
        never called a tool has a row whose ``last_tool_use_at`` is NULL, while the
        shared row is kept fresh by its working sibling).
    """
    if not task_contact_lookup:
        return False
    task_id = getattr(task, "id", None)
    if task_id is None:
        return False
    return str(task_id) in task_contact_lookup


def _vm_contact_within_cutoff(
    task,
    hb_cutoff: datetime,
    vm_contact_lookup: dict | None,
    task_contact_lookup: dict | None = None,
) -> bool:
    """True iff the orchestrator heard from this TASK's agent within ``hb_cutoff``.

    Heartbeat false-reap fix (option B): the heartbeat handler upserts an
    uncontended ``vmcontact`` row keyed on ``vm_instance_id`` BEFORE it touches
    the contended Task row, so a VM that is alive-but-lock-starved (its
    ``Task.last_heartbeat`` write was skipped while the rollup held the task row
    FOR UPDATE) still leaves a fresh ``last_contact_at``. This override is gated
    on the SAME heartbeat-stale cutoff used for ``last_heartbeat``: a genuinely
    dead agent (crashed / preempted / OOM — sends no requests) has a stale or
    absent contact row and is reaped exactly as before. Old / absent rows fall
    back to the existing ``last_heartbeat`` behaviour (no regression).

    Multiplexed-runner fix: on a fleet runner the ``vmcontact`` row is shared by
    every task on that runner, so a live sibling's contact would prove "alive" for
    a task whose own agent process is hung or dead. When ``task_contact_lookup``
    carries a TASK-scoped row for a ``runner:`` task we read THAT row and never the
    shared one. GCE (1 task per VM) is untouched: its ``vmcontact`` row already IS
    the task's row, so the genuine lock-starvation protection this override exists
    for keeps working exactly as before.

    ``task_contact_lookup`` defaults to ``None`` so callers that have not been
    migrated keep the previous behaviour.
    """
    vm_id = getattr(task, "vm_instance_id", None)
    if not vm_id:
        return False
    if _is_multiplexed_vm_id(task) and _has_task_contact_row(task, task_contact_lookup):
        last_contact = task_contact_lookup.get(str(task.id))
        return last_contact is not None and last_contact >= hb_cutoff
    if not vm_contact_lookup:
        return False
    last_contact = vm_contact_lookup.get(vm_id)
    return last_contact is not None and last_contact >= hb_cutoff


def _vm_tool_use_within_cutoff(
    task,
    progress_cutoff: datetime,
    vm_tool_use_lookup: dict | None,
    task_tool_use_lookup: dict | None = None,
    task_contact_lookup: dict | None = None,
) -> bool:
    """True iff the orchestrator heard a tool-use from THIS TASK after ``progress_cutoff``.

    Fix 3b: the PROGRESS-stall branch reaps a fresh-heartbeating agent whose
    on-row ``Task.last_tool_use_at`` is stale. But that on-row write shares the
    contended task row with ``last_heartbeat`` — a lock-starved heartbeat (the
    rollup holding the task row FOR UPDATE) drops BOTH, so an agent that IS
    making tool calls can read as stalled. The heartbeat handler upserts the
    most-recent tool-use OFF the task row onto ``vmcontact.last_tool_use_at``
    (keyed on ``vm_instance_id``), so a tool-use within the SAME progress-stale
    cutoff proves the agent is progressing (its on-row stamp was merely
    lock-dropped) and the stall reap is suppressed. A genuinely stalled agent
    (no tool calls anywhere) leaves this stale/absent and is still reaped.
    Unlike the heartbeat-stale ``_vm_contact_within_cutoff`` override, this does
    NOT key on the mere fact of contact (a hung agent still heartbeats) — it
    keys on actual TOOL-USE recency, so genuine stall detection is preserved.

    Multiplexed-runner fix (the P0 this function was the root of): keying the
    PROGRESS signal on ``vm_instance_id`` is only sound when one task owns the VM.
    A fleet runner multiplexes many tasks onto one ``runner:<id>`` vm id, and the
    heartbeat handler advances the shared ``vmcontact.last_tool_use_at`` with
    ``GREATEST(...)`` from ANY of them — so a hung-but-heartbeating agent (wedged
    LLM call / spin loop) co-located with ANY actively working sibling read as
    "progressing" forever and was NEVER stall-reaped. It could not be rescued by
    the runner self-report (#14413) either: a hung-but-alive process is still in
    the runner's ``active_task_ids``. With no absolute IN_PROGRESS ceiling
    anywhere, nothing else ever fired — a permanent zombie holding a runner slot,
    an account slot, and pinning its parent lineage.

    So for ``runner:`` vm ids we read the TASK-scoped ``taskcontact`` row: the
    sibling's tool calls are no longer this task's progress. GCE tasks keep
    reading ``vmcontact`` (where the row is already task-scoped), preserving the
    lock-starvation protection. A ``runner:`` task with NO task-scoped row yet
    (pre-rollout / not yet heartbeated) falls back to the shared row — absence is
    never evidence of death. See ``_has_task_contact_row``.
    """
    vm_id = getattr(task, "vm_instance_id", None)
    if not vm_id:
        return False
    if _is_multiplexed_vm_id(task) and _has_task_contact_row(task, task_contact_lookup):
        # Authoritative for this task. A NULL / stale task-scoped stamp means THIS
        # agent is not progressing, whatever its siblings are doing — no fallback
        # to the shared row (that fallback IS the bug).
        last_tool_use = (task_tool_use_lookup or {}).get(str(task.id))
        return last_tool_use is not None and last_tool_use >= progress_cutoff
    if not vm_tool_use_lookup:
        return False
    last_tool_use = vm_tool_use_lookup.get(vm_id)
    return last_tool_use is not None and last_tool_use >= progress_cutoff


def _is_runner_pinned_awaiting_pull(task) -> bool:
    """True iff this task is a self-hosted-runner pin still awaiting its pull.

    The orchestrator pins a QUEUED task to a registered local runner by setting
    ``vm_instance_id = 'runner:{id}'`` and ``status = PROVISIONING`` with
    ``vm_provisioned_at`` stamped (orchestrator.py ``_try_runner_dispatch``). Such
    a task is NOT a dispatched-then-dead GCE VM: no VM was ever provisioned. It
    sits PROVISIONING until the operator's runner polls
    ``POST /api/runner/next-task`` — which sets ``started_at`` (runner.py) — so a
    pin still awaiting that pull always has ``started_at IS NULL``.

    The silent pre-claim VM-death reap keys on ``vm_provisioned_at < cutoff and
    last_heartbeat is None``; left ungated it would reap a runner pin the moment
    the (6-10 min) timer elapses, even though there is no VM to have died and the
    operator's runner may simply be briefly offline (laptop asleep / rebooting)
    and will pull the pin when it returns. Excluding it here lets the work wait
    for the runner instead of churning requeue→repin→reap. (Genuinely-stuck
    require-local work is separately bounded by the 6h hold sweep,
    orchestrator.py ``_fail_stale_local_runner_holds``.)

    Scoped narrowly: only the ``runner:`` vm-id prefix + ``started_at IS NULL``
    match here, so cloud/GCE PROVISIONING tasks (numeric/instance-name vm ids, or
    runner tasks the runner has already pulled and stamped ``started_at``) are
    reaped exactly as before.
    """
    vm_id = getattr(task, "vm_instance_id", None)
    return (
        isinstance(vm_id, str)
        and vm_id.startswith("runner:")
        and getattr(task, "started_at", None) is None
    )


async def _build_runner_report_lookup(
    session, candidate_vm_ids: set
) -> dict[str, tuple[datetime, frozenset]]:
    """Batch-fetch fleet-runner self-reports for ``runner:<uuid>`` vm ids.

    Returns ``{vm_instance_id: (reported_at, frozenset(task_id_strs))}`` for
    every candidate runner whose heartbeat has stamped
    ``capabilities.active_work_reported_at`` (runner.py heartbeat handler).
    ``_classify_orphan`` consults it (via ``_runner_report_excludes_task``) so
    a runner's own fresh report that OMITS a task defeats the shared-VmContact
    reap suppression — all tasks on one runner share the ``runner:<id>`` vm
    id, so a live sibling agent's contact otherwise masks a per-task agent
    death forever (2026-07-12 zombie incident). Runners without the stamp
    (older versions) or with malformed values are skipped — those tasks keep
    the previous suppress behaviour.
    """
    runner_uuids: dict[uuid.UUID, str] = {}
    for vm_id in candidate_vm_ids:
        if not isinstance(vm_id, str) or not vm_id.startswith("runner:"):
            continue
        try:
            runner_uuids[uuid.UUID(vm_id.split(":", 1)[1])] = vm_id
        except ValueError:
            continue
    if not runner_uuids:
        return {}
    rows = (
        await session.execute(
            select(LocalRunner.id, LocalRunner.capabilities).where(
                LocalRunner.id.in_(runner_uuids.keys())
            )
        )
    ).all()
    lookup: dict[str, tuple[datetime, frozenset]] = {}
    for runner_id, capabilities in rows:
        caps = capabilities or {}
        reported_raw = caps.get("active_work_reported_at")
        active_ids = caps.get("active_task_ids")
        if not isinstance(reported_raw, str) or not isinstance(active_ids, list):
            continue
        try:
            reported_at = _coerce_naive_utc(datetime.fromisoformat(reported_raw))
        except ValueError:
            continue
        lookup[runner_uuids[runner_id]] = (
            reported_at,
            frozenset(str(i) for i in active_ids),
        )
    return lookup


# Grace after a runner pulls a task (started_at stamped) before its
# self-reported active-work list is trusted to EXCLUDE that task: the
# pull → agent-spawn → next-heartbeat cycle needs time to include a fresh
# claim, and distrusting a report inside that window would reap-churn a
# just-claimed task. Generous vs the ~30-60s runner heartbeat interval.
_RUNNER_REPORT_CLAIM_GRACE_SECONDS = 180


def _runner_report_excludes_task(
    task, hb_cutoff: datetime, runner_report_lookup: dict | None
) -> bool:
    """True iff this task's own runner freshly self-reported an active-work
    list that does NOT contain the task — proof its agent process is gone.

    Closes the shared-VmContact zombie gap: every task on a fleet runner
    shares ``vm_instance_id='runner:<id>'``, so ANY live sibling agent keeps
    the shared ``vmcontact`` row fresh and the heartbeat-stale reap override
    suppresses forever — a per-task agent death on a busy runner is invisible
    (2026-07-12 incident: FATAL'd tasks sat IN_PROGRESS for hours, pinning
    their parent lineages in EVALUATING). The runner already reports its
    ground-truth ``active_task_ids`` on every heartbeat, stamped
    ``active_work_reported_at``; a report that is fresh (within the same
    ``hb_cutoff`` used for the heartbeat-stale check), postdates this task's
    claim by a grace period, and omits the task means the runner is no longer
    running it — the shared-contact override must not save it.

    ``runner_report_lookup`` maps ``vm_instance_id`` →
    ``(reported_at, frozenset(task_id_strs))``, batch-fetched per cycle by the
    caller. Fail-safe: any missing piece — non-runner vm id, no lookup row
    (older runner version without the stamp), report stale or not yet past
    claim+grace, pin not yet pulled — returns False, preserving the previous
    suppress behaviour.
    """
    if not runner_report_lookup:
        return False
    vm_id = getattr(task, "vm_instance_id", None)
    if not isinstance(vm_id, str) or not vm_id.startswith("runner:"):
        return False
    report = runner_report_lookup.get(vm_id)
    if report is None:
        return False
    reported_at, active_ids = report
    # A pin the runner hasn't pulled yet (started_at IS NULL) can't be in its
    # list; it is protected by _is_runner_pinned_awaiting_pull, never distrusted.
    started_at = getattr(task, "started_at", None)
    if started_at is None:
        return False
    if reported_at < hb_cutoff:
        # Runner itself has gone quiet — the list is stale, not evidence.
        return False
    if reported_at < started_at + timedelta(
        seconds=_RUNNER_REPORT_CLAIM_GRACE_SECONDS
    ):
        # Report may predate the runner learning about this task's claim.
        return False
    return str(task.id) not in active_ids


def _classify_orphan(
    task,
    now: datetime,
    has_open_pr: bool,
    vm_contact_lookup: dict | None = None,
    vm_tool_use_lookup: dict | None = None,
    runner_report_lookup: dict | None = None,
    task_contact_lookup: dict | None = None,
    task_tool_use_lookup: dict | None = None,
) -> bool:
    """Single source of truth for the orphan-detection state machine.

    Returns ``True`` iff ``task`` (a PROVISIONING / IN_PROGRESS row that already
    owns a vm_instance_id, or a degenerate other-status catch-all) is an orphan
    that should be reaped, given the current clock and whether it has an open PR.

    Previously this predicate was DUPLICATED verbatim between the Step-1 lock-free
    scan (which collected ``task.id``) and the Step-2 post-lock re-verification
    (which collected the locked ORM object). Two copies drifting out of sync would
    silently change recovery outcomes, so both call sites now route through here.
    Pure + side-effect-free (no logging, no queries) so it is unit-testable and so
    the caller owns any per-site logging.

    ``vm_contact_lookup`` is an optional ``{vm_instance_id: last_contact_at}`` map
    (batch-fetched ONCE per cycle by the caller, never one query per task). On any
    heartbeat-stale branch that would reap, a contact within the SAME
    ``hb_cutoff`` overrides the reap (the agent is alive; its ``last_heartbeat``
    write was lock-starved). Absent / stale contact → existing behaviour.

    ``vm_tool_use_lookup`` is an optional ``{vm_instance_id: last_tool_use_at}``
    map (batch-fetched from the SAME query, Fix 3b). On the PROGRESS-stall
    branches that would reap (``agent_stalled``), a tool-use within the
    ``_progress_stale_cutoff`` overrides the reap — the agent IS making tool
    calls; its on-row ``last_tool_use_at`` write was lock-starved together with
    the heartbeat. Unlike ``vm_contact_lookup`` it keys on tool-use recency, NOT
    mere contact (a hung agent still heartbeats), so genuine stall detection is
    preserved. Absent / stale tool-use → existing behaviour (reap).

    ``runner_report_lookup`` is an optional ``{vm_instance_id: (reported_at,
    frozenset(task_id_strs))}`` map of fleet-runner self-reports (see
    ``_runner_report_excludes_task``). On the heartbeat-stale branches it
    OVERRIDES the ``vm_contact_lookup`` suppression for runner-hosted tasks:
    all tasks on one runner share a ``runner:<id>`` vm id, so a live sibling's
    contact must not shield a task the runner's own fresh report says it is no
    longer running. Absent / stale / pre-grace report → the vm_contact
    override applies exactly as before.

    ``task_contact_lookup`` (``{task_id_str: last_contact_at}``) and
    ``task_tool_use_lookup`` (``{task_id_str: last_tool_use_at}``) are the
    TASK-scoped twins of the two vm-keyed maps, batch-fetched from ``taskcontact``
    in the same sweep. They exist because ``vmcontact``'s "one row per VM" key is
    only "one row per TASK" on GCE — a fleet runner multiplexes many tasks onto one
    ``runner:<id>`` vm id, so the shared row's stamps are advanced by ANY agent on
    that runner. For ``runner:`` vm ids the overrides therefore read these
    task-scoped maps instead of the vm-keyed ones, which is what stops a working
    sibling from masking a hung agent's stall (and, on the heartbeat-stale
    branches, from masking a hung agent's death that ``_runner_report_excludes_task``
    cannot see because the hung process is still in ``active_task_ids``). GCE tasks
    are unaffected — they keep reading the vm-keyed maps, so their genuine
    lock-starvation false-reap protection is preserved. A ``runner:`` task with no
    ``taskcontact`` row yet falls back to the vm-keyed maps (absence is not
    evidence of death). Both default to ``None`` → previous behaviour.

    State machine (unchanged):
      PROVISIONING:
        - started_at > 15min ago                        → absolute provision ceiling
        - last_heartbeat older than hb_cutoff            → claimed-then-silent (rare)
        - vm_provisioned_at past preclaim cutoff + no hb → silent pre-claim death
      IN_PROGRESS:
        - last_heartbeat older than hb_cutoff            → agent crashed mid-run
        - no hb, started_at older than hb_cutoff         → post-claim crash (defensive)
        - no hb, no started_at, created >10min ago       → degenerate catch-all
        - fresh hb but last_tool_use_at stale (+started)  → agent stalled (tool clock)
        - fresh hb, no tool use ever, started stale, anthropic → agent stalled (Codex gate)
      any other status: created >10min ago               → catch-all
    """
    ten_minutes_ago = now - timedelta(minutes=10)
    fifteen_minutes_ago = now - timedelta(minutes=15)
    hb_cutoff = _heartbeat_stale_cutoff(task, now, has_open_pr=has_open_pr)
    # Runner-hosted zombie override: when the task's own runner freshly reports
    # it is NOT running this task, the shared-vm_id contact freshness below is a
    # sibling's liveness, not this task's — the reap must proceed. Computed once
    # so every heartbeat-stale branch applies the identical predicate.
    runner_excluded = _runner_report_excludes_task(
        task, hb_cutoff, runner_report_lookup
    )

    if task.status == TaskStatus.PROVISIONING:
        if task.started_at and task.started_at < fifteen_minutes_ago:
            return True  # absolute provisioning ceiling
        if task.last_heartbeat and task.last_heartbeat < hb_cutoff:
            # Rare: agent claimed during PROVISIONING (shouldn't happen since
            # claim_task also flips status to IN_PROGRESS), then went silent.
            # vm_contact override: a fresh contact within hb_cutoff means the VM
            # is alive (lock-starved last_heartbeat write) — don't reap.
            return runner_excluded or not _vm_contact_within_cutoff(
                task, hb_cutoff, vm_contact_lookup, task_contact_lookup
            )
        if (
            task.vm_provisioned_at
            and task.vm_provisioned_at < _preclaim_stale_cutoff(task, now)
            and task.last_heartbeat is None
            and not _is_runner_pinned_awaiting_pull(task)
        ):
            # Silent pre-claim death: VM was dispatched long enough ago
            # (scope-aware: small 6min, medium 8min, large 10min) and the
            # agent never claimed. Charge provision budget. vm_contact override:
            # if the agent has actually been calling in within hb_cutoff it is
            # NOT a silent death — suppress the reap.
            #
            # Runner-pin exclusion: a task pinned to a self-hosted runner
            # (vm_instance_id='runner:{id}', started_at IS NULL) was never a GCE
            # VM — it is waiting for the operator's runner to poll. The
            # vm_provisioned_at timer must NOT reap it as a pre-claim death; that
            # caused surplus runner-pinned compute_experiments to churn
            # (requeue→repin→reap). It is bounded instead by the 6h local-runner
            # hold sweep (orchestrator.py _fail_stale_local_runner_holds).
            return runner_excluded or not _vm_contact_within_cutoff(
                task, hb_cutoff, vm_contact_lookup, task_contact_lookup
            )
        return False

    if task.status == TaskStatus.IN_PROGRESS:
        if task.last_heartbeat and task.last_heartbeat < hb_cutoff:
            # Agent crashed mid-run — UNLESS we heard from the VM within the
            # same cutoff (lock-starved last_heartbeat write); then it's alive.
            return runner_excluded or not _vm_contact_within_cutoff(
                task, hb_cutoff, vm_contact_lookup, task_contact_lookup
            )
        if (
            task.last_heartbeat is None
            and task.started_at
            and task.started_at < hb_cutoff
        ):
            # Defensive: IN_PROGRESS without any heartbeat shouldn't happen
            # (claim_task sets last_heartbeat at agent.py:312) but if we hit
            # it, treat as a post-claim crash. Use scope-aware cutoff so
            # large/high-effort tasks aren't reaped prematurely. vm_contact
            # override: a fresh contact within hb_cutoff proves the VM is alive
            # (its last_heartbeat write was lock-starved), so don't reap.
            return runner_excluded or not _vm_contact_within_cutoff(
                task, hb_cutoff, vm_contact_lookup, task_contact_lookup
            )
        if (
            task.last_heartbeat is None
            and task.started_at is None
            and task.created_at < ten_minutes_ago
        ):
            # Fully-degenerate IN_PROGRESS row (no heartbeat, no started_at)
            # that's been alive for >10 min — data corruption or a path that
            # forgot to set started_at. Reap as a catch-all. vm_contact override:
            # if the VM is demonstrably alive (fresh contact within hb_cutoff)
            # this is not a dead-VM orphan — suppress the reap.
            return runner_excluded or not _vm_contact_within_cutoff(
                task, hb_cutoff, vm_contact_lookup, task_contact_lookup
            )
        if (
            task.last_heartbeat
            and task.last_heartbeat >= hb_cutoff
            and not _uses_heartbeat_only_progress(task)
            and task.last_tool_use_at is not None
            and task.last_tool_use_at < _progress_stale_cutoff(task, now)
            and (
                task.started_at is None
                or task.started_at < _progress_stale_cutoff(task, now)
            )
        ):
            # Stalled-but-heartbeating: agent process is alive (fresh
            # heartbeat) but has made no tool/LLM calls for longer than
            # the scope-aware progress threshold. Indicates a hung LLM
            # call, a spinning retry loop, or a wedged tool invocation.
            #
            # NO vm_contact (last_contact_at) override here: this is a
            # PROGRESS-stale reap, not a heartbeat-stale one — the heartbeat IS
            # fresh (last_heartbeat >= hb_cutoff), so a mere contact would
            # likewise be fresh and suppressing on it would defeat genuine stall
            # detection. The contact override only covers the last_heartbeat
            # false-reap (lock-starved write).
            #
            # Fix 3b: a TOOL-USE override IS valid here. The on-row
            # ``last_tool_use_at`` write shares the contended task row with
            # ``last_heartbeat``, so a lock-starved heartbeat drops it too — an
            # agent that IS calling tools can read as stalled. The off-row
            # ``vmcontact.last_tool_use_at`` carries the real progress signal:
            # if it's within the progress-stale cutoff the agent is progressing,
            # so don't reap. Keyed on tool-use recency (not mere contact), so a
            # genuinely stalled-but-heartbeating agent (no recent tool use
            # anywhere) is still reaped.
            #
            # Cross-attempt stale-tool guard (8c8cf973 incident):
            # last_tool_use_at carries forward across retries — if
            # only this field is checked, every retry is born stale
            # and reaped within seconds (8c8cf973 attempt 3 reaped
            # 22s after starting because tool_use_at was 11min old
            # from attempt 1). The started_at check ensures the
            # current attempt has actually had time to call tools.
            return not _vm_tool_use_within_cutoff(
                task,
                _progress_stale_cutoff(task, now),
                vm_tool_use_lookup,
                task_tool_use_lookup,
                task_contact_lookup,
            )
        if (
            task.last_heartbeat
            and task.last_heartbeat >= hb_cutoff
            and not _uses_heartbeat_only_progress(task)
            and task.last_tool_use_at is None
            and task.started_at
            and task.started_at < _progress_stale_cutoff(task, now)
        ):
            # Stalled-but-heartbeating with zero tool calls ever.
            # All providers now populate tool_trace via recordToolUse:
            # claude-stream-json fires on tool_use blocks; codex-jsonl
            # fires on item events; text-mode (Gemini) fires on every
            # non-empty stdout chunk as 'model_output'. So
            # last_tool_use_at is NULL only for genuinely zero-progress
            # agents, regardless of provider.
            #
            # Fix 3b: even with on-row last_tool_use_at NULL, an off-row
            # ``vmcontact.last_tool_use_at`` within the progress cutoff means
            # the agent DID record a tool use that the lock-starved heartbeat
            # failed to land on the task row — it is progressing, don't reap.
            return not _vm_tool_use_within_cutoff(
                task,
                _progress_stale_cutoff(task, now),
                vm_tool_use_lookup,
                task_tool_use_lookup,
                task_contact_lookup,
            )
        return False

    # Any other status (degenerate row left in an active-scan status): catch-all.
    return task.created_at < ten_minutes_ago


_HEARTBEAT_STALE_DEFAULTS = {
    "small": 300,
    "medium": 420,
    "large": 900,
}


_HEARTBEAT_EFFORT_FLOORS = {
    "low": 300,
    "medium": 300,
    "high": 420,
    "max": 720,
}


_PRECLAIM_STALE_DEFAULTS = {
    "small": 360,
    "medium": 480,
    "large": 600,
}

# Progress-staleness thresholds: how long a fresh-heartbeating task may go
# without any tool/LLM event before being classified as agent_stalled.
# Longer than heartbeat-stale to avoid false positives on normal LLM think time.
_PROGRESS_STALE_DEFAULTS = {
    "small": 480,  # 8 min
    "medium": 720,  # 12 min
    "large": 1080,  # 18 min
}

# Task types where the agent legitimately spends 5-20 min reasoning before
# emitting a tool call (b2410541 incident: Claude Opus on strategic_review
# was reaped after 484s while in the middle of pass-1 thinking — actual
# attempt completed cleanly in 19 min once given a budget). These types
# get a flat longer cutoff regardless of nominal scope.
# Memo (pure-reasoning documentation tasks with no repo) may run 5-10+ min
# producing a reasoning artifact without any tool calls — not a sign of staleness.
_THINKING_HEAVY_TASK_TYPES = frozenset(
    {
        "strategic_review",
        "design_doc_update",
        "research",
        "investigate",
        "plan",
        "memo",
    }
)
_THINKING_HEAVY_PROGRESS_STALE_SECONDS = 1500  # 25 min


def _progress_stale_cutoff(task, now: datetime) -> datetime:
    """Return cutoff time for meaningful-progress (last_tool_use_at) staleness.

    Only fires when last_heartbeat IS fresh (agent alive but wedged). Uses
    longer thresholds than heartbeat-stale to tolerate normal LLM latency.
    Thinking-heavy task types override scope-based defaults — reasoning
    models on strategic/research work spend long stretches between tool
    calls (8-min cutoff misclassifies normal Opus reasoning as stalled).
    """
    ctx = task.context or {}
    payload = task.first_payload or {}
    task_type = payload.get("type") or ""
    if task_type in _THINKING_HEAVY_TASK_TYPES:
        thinking_override = _runtime_config.get(
            "progress_stale_seconds_thinking_heavy",
            _THINKING_HEAVY_PROGRESS_STALE_SECONDS,
        )
        return now - timedelta(seconds=thinking_override)
    scope = ctx.get("scope") or payload.get("scope")
    thresholds = _runtime_config.get(
        "progress_stale_seconds_by_scope", _PROGRESS_STALE_DEFAULTS
    )
    seconds = thresholds.get(scope, thresholds.get("small", 480))
    return now - timedelta(seconds=seconds)


def _preclaim_stale_cutoff(task, now: datetime) -> datetime:
    ctx = task.context or {}
    payload = task.first_payload or {}
    scope = ctx.get("scope") or payload.get("scope")
    thresholds = _runtime_config.get(
        "preclaim_stale_seconds_by_scope", _PRECLAIM_STALE_DEFAULTS
    )
    seconds = thresholds.get(scope, thresholds.get("small", 360))
    return now - timedelta(seconds=seconds)


def _heartbeat_stale_cutoff(task, now: datetime, has_open_pr: bool = False) -> datetime:
    # When a task has an open PR, reduce the heartbeat-stale cutoff to 90s
    # to detect post-PR silent deaths faster and hand off to the review
    # pipeline sooner (normally 5-15 min delay; with PR cutoff ~90-150s).
    # The 90s gives one missed heartbeat (60s) plus one reaper cycle (~30s).
    if has_open_pr:
        return now - timedelta(seconds=90)

    ctx = task.context or {}
    payload = task.first_payload or {}
    scope = ctx.get("scope") or payload.get("scope")
    thresholds = _runtime_config.get(
        "heartbeat_stale_seconds_by_scope", _HEARTBEAT_STALE_DEFAULTS
    )
    seconds = thresholds.get(scope, thresholds.get("small", 300))

    effort = ctx.get("effort") or payload.get("effort")
    if not effort:
        task_type = ctx.get("type") or payload.get("type")
        if task_type:
            # Resolve effort from the provider settings bag: merge Anthropic
            # defaults with any per-task-type override. Reaper needs a scalar
            # effort to size the heartbeat-stale floor; other providers'
            # effort-like knobs (reasoning_effort, thinking_budget) aren't
            # normalized so we look at Anthropic only, which is the dispatch
            # default.
            defaults = _runtime_config.get("provider_settings_defaults", {}).get(
                "anthropic", {}
            )
            overrides = (
                _runtime_config.get("task_type_provider_overrides", {})
                .get(task_type, {})
                .get("anthropic", {})
            )
            effort = overrides.get("effort") or defaults.get("effort")
    floor = _HEARTBEAT_EFFORT_FLOORS.get(effort, 0)
    seconds = max(seconds, floor)
    return now - timedelta(seconds=seconds)


def _extract_tool_trace_tail(task) -> str:
    """Extract and format the tail of _tool_trace from task.context.

    Returns a compact formatted string of recent tool calls (timestamp + tool name
    + first ~80 chars of input preview), limited to ~400 chars total. If _tool_trace
    is missing or empty, returns empty string.
    """
    ctx = task.context or {}
    tool_trace = ctx.get("_tool_trace") or []
    if not tool_trace:
        return ""

    # Take the last 5 entries to stay within 400-char budget
    entries = tool_trace[-5:] if len(tool_trace) > 5 else tool_trace
    parts = []
    total_len = 0
    max_total = 400

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        t_str = entry.get("t", "").split("T")[-1][:8]  # HH:MM:SS from ISO-8601
        tool_name = entry.get("tool", "?")[:20]
        input_preview = redact_command_log_text(entry.get("input_preview", ""))[:80]
        # Compact format: timestamp + tool + input
        part = f"{t_str} {tool_name}: {input_preview}"
        if total_len + len(part) + 3 > max_total:  # +3 for " | "
            break
        parts.append(part)
        total_len += len(part) + 3

    if not parts:
        return ""
    return " | ".join(parts)


async def _task_has_open_pr(task, session) -> tuple[bool, int | None]:
    """Check whether a task has a PULL_REQUEST artifact (PR was published).

    NOTE: Despite the name, this only checks the Artifact table — it does
    NOT verify the PR is still open on GitHub. The PR may have been closed
    or merged out-of-band. Callers that act on "open" must re-confirm via
    get_pull_request before parking the task in EVALUATING.

    Used by the reaper: a task that pushed a branch and opened a PR before
    being reaped should hand off to the review pipeline (EVALUATING) rather
    than retry from scratch (QUEUED) — the retry path would collide with the
    existing branch and be refused by the agent's stale-branch guard
    (agent/src/index.ts:706), orphaning the PR.

    The Artifact table is written atomically with PR creation (see
    routers/agent.py:2035). Returns (True, pr_number) on match.
    """
    stmt = select(Artifact).where(
        Artifact.task_id == task.id,
        Artifact.artifact_type == ArtifactType.PULL_REQUEST,
    )
    artifact = (await session.execute(stmt)).scalars().first()
    if artifact is None or not artifact.pr_url:
        return False, None
    # Parse PR number from the URL (e.g. https://github.com/owner/repo/pull/2903)
    try:
        pr_num = int(artifact.pr_url.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return False, None
    return True, pr_num
