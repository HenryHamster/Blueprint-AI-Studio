"""Orphan-classification helpers extracted from reaper.py.

Pure (or near-pure) functions for orphan priority bucketing, selection,
VM-contact predicates, and the _classify_orphan() state machine. No
dependency on the reaper service loop — extracting these makes
_classify_orphan() unit-testable in isolation.
"""

from datetime import datetime, timedelta

from sqlmodel import select

from app.failure_classification import RATE_LIMIT
from app.models import Artifact, ArtifactType, TaskStatus
from app.schemas import redact_command_log_text
from app.shared_state import runtime_config as _runtime_config

# Orphan-priority buckets (lower = processed first). Time-sensitive orphans are
# reaped before the cap can truncate the tail.
_ORPHAN_PRIORITY_RATE_LIMITED = 0  # rate-limited / quota — reroute ASAP
_ORPHAN_PRIORITY_SPOT = 1  # spot VM — likelier to be preempted again
_ORPHAN_PRIORITY_PROVISION = 2  # stuck PROVISIONING — never got a heartbeat
_ORPHAN_PRIORITY_DEFAULT = 3  # everything else


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


def _is_shared_runner_vm_id(vm_id) -> bool:
    """True for the shared self-hosted-runner vm id (``runner:<id>``).

    Every task pinned to the same operator runner carries this IDENTICAL
    ``vm_instance_id``, so the per-VM ``vmcontact`` row is a RUNNER-level liveness
    signal, not a per-TASK one — it cannot say which of the runner's tasks is
    alive.
    """
    return isinstance(vm_id, str) and vm_id.startswith("runner:")


def _runner_contact_within_cutoff(
    task, cutoff: datetime, vm_contact_lookup: dict | None
) -> bool:
    """Runner-LEVEL liveness override: True iff the runner behind this task's
    ``vm_instance_id`` was heard from within ``cutoff``.

    Intended ONLY for the resident-session carve-out, whose reap question really
    is "has the RUNNER gone silent" (a resident session is a 1:1 pin to a
    dedicated runner). Unlike :func:`_vm_contact_within_cutoff` this DOES honour a
    shared ``runner:<id>`` contact, because runner liveness is exactly the signal
    that path wants.
    """
    if not vm_contact_lookup:
        return False
    vm_id = getattr(task, "vm_instance_id", None)
    if not vm_id:
        return False
    last_contact = vm_contact_lookup.get(vm_id)
    return last_contact is not None and last_contact >= cutoff


def _vm_contact_within_cutoff(
    task, hb_cutoff: datetime, vm_contact_lookup: dict | None
) -> bool:
    """True iff the orchestrator heard from this task's VM within ``hb_cutoff``.

    Heartbeat false-reap fix (option B): the heartbeat handler upserts an
    uncontended ``vmcontact`` row keyed on ``vm_instance_id`` BEFORE it touches
    the contended Task row, so a VM that is alive-but-lock-starved (its
    ``Task.last_heartbeat`` write was skipped while the rollup held the task row
    FOR UPDATE) still leaves a fresh ``last_contact_at``. This override is gated
    on the SAME heartbeat-stale cutoff used for ``last_heartbeat``: a genuinely
    dead agent (crashed / preempted / OOM — sends no requests) has a stale or
    absent contact row and is reaped exactly as before. Old / absent rows fall
    back to the existing ``last_heartbeat`` behaviour (no regression).

    Per-task-death fix: a SHARED ``runner:<id>`` vm id keys the ``vmcontact`` row
    on the RUNNER, not the task, so a live SIBLING task's heartbeat would mask a
    dead task pinned to the same runner (false-negative → zombie that never gets
    reaped). The shared runner contact therefore does NOT prove THIS task is
    alive and must not override the per-task heartbeat-stale reap: for such ids we
    fall back to the task's own on-row ``last_heartbeat`` (already evaluated by the
    caller before this override is consulted). Resident sessions, whose reap
    question genuinely IS runner liveness, use :func:`_runner_contact_within_cutoff`
    instead. Cloud/GCE tasks (unique per-VM ids) are unaffected.
    """
    if not vm_contact_lookup:
        return False
    vm_id = getattr(task, "vm_instance_id", None)
    if not vm_id:
        return False
    if _is_shared_runner_vm_id(vm_id):
        # Shared runner contact cannot attribute liveness to THIS specific task.
        return False
    last_contact = vm_contact_lookup.get(vm_id)
    return last_contact is not None and last_contact >= hb_cutoff


def _vm_tool_use_within_cutoff(
    task, progress_cutoff: datetime, vm_tool_use_lookup: dict | None
) -> bool:
    """True iff the orchestrator heard a tool-use from this VM after ``progress_cutoff``.

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
    """
    if not vm_tool_use_lookup:
        return False
    vm_id = getattr(task, "vm_instance_id", None)
    if not vm_id:
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


def _classify_orphan(
    task,
    now: datetime,
    has_open_pr: bool,
    vm_contact_lookup: dict | None = None,
    vm_tool_use_lookup: dict | None = None,
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

    # Resident strategic session carve-out: an IN_PROGRESS task pinned for the
    # life of an operator session (context["_resident_session"] == True). It is
    # EXEMPT from the progress-staleness (thinking-heavy 1500s) budget — being
    # warm-but-idle between operator turns / autonomous chunks means long
    # tool-call gaps are normal, not a stall. It is reaped only when the runner
    # itself has gone silent beyond the resident heartbeat window (see
    # _classify_resident_session). A resident session still PROVISIONING is a
    # runner pin awaiting pull (started_at IS NULL), already protected by
    # _is_runner_pinned_awaiting_pull, so this carve-out is scoped to IN_PROGRESS.
    if task.status == TaskStatus.IN_PROGRESS and _is_resident_session(task):
        return _classify_resident_session(task, now, vm_contact_lookup)

    if task.status == TaskStatus.PROVISIONING:
        if task.started_at and task.started_at < fifteen_minutes_ago:
            return True  # absolute provisioning ceiling
        if task.last_heartbeat and task.last_heartbeat < hb_cutoff:
            # Rare: agent claimed during PROVISIONING (shouldn't happen since
            # claim_task also flips status to IN_PROGRESS), then went silent.
            # vm_contact override: a fresh contact within hb_cutoff means the VM
            # is alive (lock-starved last_heartbeat write) — don't reap.
            return not _vm_contact_within_cutoff(task, hb_cutoff, vm_contact_lookup)
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
            return not _vm_contact_within_cutoff(task, hb_cutoff, vm_contact_lookup)
        return False

    if task.status == TaskStatus.IN_PROGRESS:
        if task.last_heartbeat and task.last_heartbeat < hb_cutoff:
            # Agent crashed mid-run — UNLESS we heard from the VM within the
            # same cutoff (lock-starved last_heartbeat write); then it's alive.
            return not _vm_contact_within_cutoff(task, hb_cutoff, vm_contact_lookup)
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
            return not _vm_contact_within_cutoff(task, hb_cutoff, vm_contact_lookup)
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
            return not _vm_contact_within_cutoff(task, hb_cutoff, vm_contact_lookup)
        if (
            task.last_heartbeat
            and task.last_heartbeat >= hb_cutoff
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
                task, _progress_stale_cutoff(task, now), vm_tool_use_lookup
            )
        if (
            task.last_heartbeat
            and task.last_heartbeat >= hb_cutoff
            and task.last_tool_use_at is None
            and task.started_at
            and task.started_at < _progress_stale_cutoff(task, now)
            and (task.context or {}).get("llm_provider") == "anthropic"
        ):
            # Stalled-but-heartbeating with zero tool calls ever.
            # Provider-gated to anthropic only because the agent
            # wrapper's outputMode='text' path (Codex/Gemini at
            # agent/src/index.ts:3061,3119) never calls
            # recordToolUse — only the claude-stream-json parser
            # does. So last_tool_use_at IS NULL for every
            # Codex/Gemini task by design, regardless of whether
            # tools were called. Without this gate the watchdog
            # reaps healthy non-anthropic work after 8 min.
            # TODO: parse Codex --json events / Gemini events
            # to populate tool_trace, then drop the gate.
            #
            # Fix 3b: even with on-row last_tool_use_at NULL, an off-row
            # ``vmcontact.last_tool_use_at`` within the progress cutoff means
            # the agent DID record a tool use that the lock-starved heartbeat
            # failed to land on the task row — it is progressing, don't reap.
            return not _vm_tool_use_within_cutoff(
                task, _progress_stale_cutoff(task, now), vm_tool_use_lookup
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

# Resident strategic session (context["_resident_session"] == True): a
# long-lived strategic_review task that stays IN_PROGRESS for the whole operator
# session, pinned to one runner. It is warm-but-idle between operator turns and
# autonomous chunks, so it legitimately makes NO tool calls for long stretches —
# the progress-staleness (thinking-heavy 1500s) budget would wrongly reap it.
# It is EXEMPT from progress-staleness and reaped only when the runner itself has
# gone silent (last_heartbeat / started_at) beyond this resident heartbeat window.
# Longer than the normal scope/effort heartbeat cutoffs so a briefly-asleep laptop
# or a transient network gap does not tear down the warm session; a genuinely dead
# runner is still reclaimed. Overridable via runtime_config.
_RESIDENT_SESSION_HEARTBEAT_STALE_SECONDS = 1500  # 25 min


def _is_resident_session(task) -> bool:
    """True iff *task* is a resident strategic session (opts into the carve-out).

    Keyed on the ``context["_resident_session"] == True`` flag the session
    start path stamps (shared identifier per the resident-agent build contract).
    """
    return (getattr(task, "context", None) or {}).get("_resident_session") is True


def _resident_session_heartbeat_stale_cutoff(task, now: datetime) -> datetime:
    """Return the runner-silence cutoff for a resident strategic session.

    Independent of the scope/effort heartbeat cutoffs: a resident session is
    warm-but-idle between turns, so it gets a single generous window keyed only
    on runner liveness. Reads ``resident_session_heartbeat_stale_seconds`` from
    runtime_config, defaulting to ``_RESIDENT_SESSION_HEARTBEAT_STALE_SECONDS``.
    """
    seconds = _runtime_config.get(
        "resident_session_heartbeat_stale_seconds",
        _RESIDENT_SESSION_HEARTBEAT_STALE_SECONDS,
    )
    return now - timedelta(seconds=seconds)


def _classify_resident_session(
    task,
    now: datetime,
    vm_contact_lookup: dict | None,
) -> bool:
    """Reap decision for an IN_PROGRESS resident strategic session.

    EXEMPT from progress-staleness (no reap on stale ``last_tool_use_at`` —
    long idle-but-warm stretches are expected). Reaped ONLY when the runner has
    gone silent beyond the resident heartbeat window: a ``last_heartbeat`` older
    than the resident cutoff (runner dead / laptop closed), or, if no heartbeat
    was ever recorded, a ``started_at`` older than the same window. If BOTH
    ``last_heartbeat`` and ``started_at`` are absent (a degenerate / corrupt
    row), fall back to ``created_at`` against the same window so the carve-out
    can never pin such a row forever. In every case a fresh off-row VM contact
    within the window (lock-starved heartbeat write) overrides the reap,
    mirroring the other heartbeat-stale branches. The window itself comes from
    ``runtime_config['resident_session_heartbeat_stale_seconds']`` (see
    ``_resident_session_heartbeat_stale_cutoff``), not a hardcoded constant.
    """
    # Resident sessions use the RUNNER-level contact override: the reap question
    # here is "has the runner gone silent", and a resident session is a 1:1 pin to
    # a dedicated runner, so honouring the shared ``runner:<id>`` contact is both
    # safe and exactly the signal wanted (unlike the per-task orphan branches,
    # which must NOT trust a shared runner contact — see _vm_contact_within_cutoff).
    resident_cutoff = _resident_session_heartbeat_stale_cutoff(task, now)
    if task.last_heartbeat is not None:
        if task.last_heartbeat < resident_cutoff:
            return not _runner_contact_within_cutoff(
                task, resident_cutoff, vm_contact_lookup
            )
        return False
    if task.started_at is not None:
        if task.started_at < resident_cutoff:
            return not _runner_contact_within_cutoff(
                task, resident_cutoff, vm_contact_lookup
            )
        return False
    # Degenerate row: neither a heartbeat nor a started_at was ever recorded.
    # Without this branch the carve-out returns False unconditionally and never
    # reaps a corrupt resident row. Reap it on the same created_at-based window
    # (fresh off-row runner contact still overrides, like every other branch).
    created_at = getattr(task, "created_at", None)
    if created_at is not None and created_at < resident_cutoff:
        return not _runner_contact_within_cutoff(
            task, resident_cutoff, vm_contact_lookup
        )
    return False


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
