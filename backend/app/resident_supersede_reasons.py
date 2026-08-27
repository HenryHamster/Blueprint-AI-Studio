"""Machine-readable causes recorded on a superseded resident worker task.

A resident session supersedes its predecessor for several genuinely different
reasons, and until this module existed every one of them was written to the
cancelled task row as the same fixed sentence: "Resident worker superseded
after its resident heartbeat became stale".

That sentence was factually wrong for the dominant production case. A task the
pinned runner has never pulled has no heartbeat to go stale and never had one —
it was condemned by the *start-claim* clock, not by any worker liveness signal.
An operator reading 240 identically-worded cancellations could not tell the
never-pulled class from a genuinely dead worker, which is precisely why the
supersession livelock stayed invisible for 27 hours.

The health verdict returns exactly one of these codes (or ``None`` for a healthy
worker), and the cancelled row records both the code and the winning generation,
so a superseded task states WHY it lost and to whom.

Naming note: deliberately NOT ``strategic_resident_supersede_reasons.py``. The
``strategic_resident_*.py`` prefix under ``backend/app/`` is reserved — migration
0657 deleted ~70 modules of the retired autonomous-resident subsystem and
``test_conversational_workday_protection.py`` fails on any new file matching that
glob, so a reappearing one reads as a partial restore. This module belongs to the
kept conversational Workday session path, so it stays off that prefix rather than
being added to the guard's allowlist.
"""

from __future__ import annotations

#: The recorded task row backing the session identity no longer exists.
RESIDENT_SUPERSEDE_WORKER_TASK_MISSING = "worker_task_missing"
#: The backing task already reached a terminal status on its own.
RESIDENT_SUPERSEDE_WORKER_TASK_TERMINAL = "worker_task_terminal"
#: A worker attached and heartbeated, then stopped heartbeating.
RESIDENT_SUPERSEDE_WORKER_HEARTBEAT_STALE = "worker_heartbeat_stale"
#: No worker ever attached within the start-claim window.
RESIDENT_SUPERSEDE_START_CLAIM_STALE = "start_claim_stale"
#: The task was pinned to a local runner that went offline before pulling it.
RESIDENT_SUPERSEDE_PINNED_RUNNER_OFFLINE = "pinned_runner_offline"
#: The task was left behind by a predecessor identity that was itself superseded.
RESIDENT_SUPERSEDE_ORPHANED_IDENTITY = "orphaned_predecessor_identity"

#: Operator-facing sentence per cause, used for ``latest_status_message``. Each
#: reads as the tail of "Resident worker superseded by <winner> because ...".
RESIDENT_SUPERSEDE_REASON_TEXT = {
    RESIDENT_SUPERSEDE_WORKER_TASK_MISSING: "its backing task row was missing",
    RESIDENT_SUPERSEDE_WORKER_TASK_TERMINAL: (
        "its backing task had already reached a terminal status"
    ),
    RESIDENT_SUPERSEDE_WORKER_HEARTBEAT_STALE: (
        "its resident worker heartbeat went stale"
    ),
    RESIDENT_SUPERSEDE_START_CLAIM_STALE: (
        "its start claim went stale before a worker attached"
    ),
    RESIDENT_SUPERSEDE_PINNED_RUNNER_OFFLINE: (
        "the device it was pinned to went offline before pulling it"
    ),
    RESIDENT_SUPERSEDE_ORPHANED_IDENTITY: (
        "it was orphaned by a superseded predecessor identity"
    ),
}
