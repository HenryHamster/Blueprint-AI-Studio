# Task Lifecycle & State Machine

This guide explains how Blueprint tasks progress through their lifecycle, from creation to completion.

---

## Task Status States

A task can be in one of 7 states:

```
QUEUED → PROVISIONING → IN_PROGRESS → [EVALUATING] → COMPLETED
           ↓
         FAILED → (retry)
         
CANCELLED (terminal, at any state)
```

| Status | Meaning |
|--------|---------|
| **QUEUED** | Task created, waiting for the orchestrator to provision a VM |
| **PROVISIONING** | VM is being created on GCP (or local subprocess in LOCAL_MODE) |
| **IN_PROGRESS** | Agent is running on the VM, executing the task |
| **EVALUATING** | Task completed on agent, orchestrator is post-processing (optional, only for certain task types) |
| **COMPLETED** | Task succeeded; artifacts stored, cleanup complete |
| **FAILED** | Task failed; error logs stored; may retry if attempts remain |
| **CANCELLED** | Task was manually cancelled; VM cleaned up |

---

## State Transitions

### 1. QUEUED → PROVISIONING

**Trigger:** `orchestrator_worker()` picks up the task (polls every 15 seconds, configurable via `ORCHESTRATOR_HARD_WATCHDOG_POLL_SECONDS`)

**What happens:**
- Task status updated to PROVISIONING
- GCP VM provisioned (or local subprocess spawned in LOCAL_MODE)
- VM name/ID stored on task
- Agent secret manifest prepared (includes LLM credentials, task ID, etc.)
- Task is locked with `FOR UPDATE` to prevent concurrent provisioning

**Code:** `backend/app/orchestrator.py` → `_provision_and_handle()`

---

### 2. PROVISIONING → IN_PROGRESS

**Trigger:** Agent boots and claims the task

**What happens:**
- Agent calls `POST /api/agent/claim` with JWT
- Orchestrator validates JWT (task ID matches, not expired)
- Task status updated to IN_PROGRESS
- Orchestrator returns full task details + GitHub token + prompt
- Agent begins work (clone repo, run Claude Code, etc.)

**Code:** `backend/app/routers/agent.py` → `claim_task()`

---

### 3. IN_PROGRESS → EVALUATING (optional)

**Trigger:** Agent reports completion via `POST /api/agent/status`

**When it happens:**
- Only for certain task types that require post-processing:
  - `review` — needs PR comment submission
  - `fix` — multiple fix loop iterations
  - `security_audit`, `perf`, `debug`, etc.

**What happens:**
- Task status set to EVALUATING
- Orchestrator asynchronously processes the agent's terminal report
- May spawn follow-on tasks (e.g., review findings from a research task)

**Code:** `backend/app/routers/agent_status.py` → `report_task_status()`

---

### 4. EVALUATING → COMPLETED or FAILED

**Trigger:** Post-processing complete (async)

**What happens:**
- Verdict recorded (success/failure category)
- Artifacts stored (PR URL, commit hash, logs, findings)
- Task history entry created (for audit trail)
- VM deleted (or reused if warm continuity enabled)
- If task succeeded: project status updated if all tasks done
- If task failed & retries remain: new task spawned with next attempt number

**Code:** `backend/app/task_completion.py` → `_process_completion()`

---

### 5. IN_PROGRESS or EVALUATING → FAILED (timeout)

**Trigger:** Reaper detects heartbeat timeout (5-minute window)

**What happens:**
- Agent hasn't sent a heartbeat in 5+ minutes
- Reaper marks task as FAILED with category `timeout`
- VM terminated
- Task not retried (timeout is considered terminal)

**Code:** `backend/app/reaper.py` → `reaper_service()`

---

### 6. Any state → CANCELLED

**Trigger:** User explicitly cancels via `POST /api/projects/{id}/cancel`

**What happens:**
- Task status set to CANCELLED
- If VM is running, it's terminated immediately
- Parent task (if this is a child) may be rolled back
- No artifacts are stored; no retries

**Code:** `backend/app/routers/projects.py` → `cancel_project()`

---

## Task Attempt Banking

When a task fails and needs to retry:

```python
# Each task has a retry budget (default 3 attempts total)
Task.attempt_number: 1, 2, 3  # Current attempt
Task.max_attempts: 3          # Total allowed

# On failure, orchestrator checks:
if attempt_number < max_attempts:
    # Spawn a new task with attempt_number += 1
    new_task = Task(
        parent_id=failed_task.id,
        attempt_number=2,
        ...
    )
else:
    # All retries exhausted; mark as terminal FAILED
```

**Banking:** Cost metrics (VM runtime, token usage) are accumulated via `Task.context[_TASK_BANKED_*]` keys so retry costs roll up to the parent for billing.

---

## Lifecycle Callbacks

Blueprint fires callbacks at key lifecycle events. Modules can register handlers:

```python
from app.models import _on_task_terminal_callbacks

# Module wants to act on task completion
def my_handler(task: Task):
    # Called whenever task reaches COMPLETED or FAILED

_on_task_terminal_callbacks.append(my_handler)
```

**Built-in callbacks:**
- Circuit breaker checks (halt project if failure rate too high)
- VM cleanup scheduling
- Direction completion checks (autonomous research)
- Strategic cycle triggers (spawn next directions)

**Code:** `backend/app/models.py` → `_on_task_terminal_callbacks`

---

## Parent-Child Task Relationships

Some task types spawn child tasks during execution:

```
Parent Task (e.g., research)
    ├─ Child 1 (e.g., spawned from findings)
    ├─ Child 2
    └─ Child 3
```

**Rules:**
- Child inherits parent's project_id, github_owner/repo
- Child has different task_type and payload
- Parent remains IN_PROGRESS until all children reach terminal state
- If parent is CANCELLED, children are also cancelled (cascade)
- Retry of parent does NOT retry children (they're independent)

**Spawning:** Agent calls `POST /api/agent/spawn` to create children mid-task.

**Code:** `backend/app/routers/agent.py` → `spawn_task()`

---

## Task Locking

The orchestrator uses **row-level locking** (`SELECT ... FOR UPDATE`) to prevent concurrent mutations:

```python
# Acquire exclusive lock on task row
async with task_lock(session, task_id):
    task = await session.get(Task, task_id)
    # Mutate task
    session.add(task)
    await session.commit()  # Lock released here
```

**Why:** Multiple background workers (orchestrator, reaper, post-commit sweep) may try to transition the same task simultaneously. The lock ensures only one wins.

**Timeout:** If a worker holds the lock for >30 seconds without progress, it logs a warning. If >60 seconds, a watchdog detects it.

**Code:** `backend/app/task_locking.py`

---

## Heartbeat Tracking

While IN_PROGRESS, the agent sends periodic heartbeats to signal it's alive:

```
POST /api/agent/heartbeat
{
  "task_id": "...",
  "status": "IN_PROGRESS",
  "metrics": { "tokens_used": 1234, ... }
}
```

**Heartbeat window:** 5 minutes (300 seconds)

- If agent sends heartbeat within 5 min: task remains IN_PROGRESS
- If >5 min of silence: reaper marks task FAILED with `timeout` category
- Heartbeat carries optional metrics (tokens, tool invocations, errors)

**Code:** `backend/app/reaper.py` and `backend/app/routers/agent.py`

---

## Warm VM Reuse

Tasks on the same VM can reuse it for efficiency (warm continuation):

```
VM boots → Task 1 executes → Task 1 completes
          → Task 2 claims VM via warm handoff
          → Task 2 executes on same VM
          → (repeat)
```

**Mechanism:**
- After Task 1 completes, agent polls `POST /api/agent/next-role` to ask for next task
- If eligible task exists in backlog, orchestrator assigns it to the same VM
- Agent reuses the VM process and cached dependencies

**Benefits:** Faster startup, lower cost (no VM boot overhead)

**When it's disabled:**
- Task type doesn't support warm reuse (e.g., some research tasks)
- Backlog is empty
- VM health check failed

**Code:** `backend/app/routers/agent_warm_continuity.py`

---

## VM Lifecycle

VMs are created on-demand and cleaned up automatically:

```
PROVISIONING: GCP creates VM from Packer image
IN_PROGRESS:  Agent runs; heartbeat kept alive
COMPLETED:    Cleanup scheduled (delete VM)
             OR: Reused (warm continuity)
```

**Cleanup triggers:**
1. **Task completion** — VM deleted within ~5 minutes
2. **Heartbeat timeout** — Reaper deletes immediately
3. **Hard TTL exceeded** — 1-hour limit; cleanup forced
4. **Warm reuse window closed** — Idle VM deleted after ~5 min (no new tasks available)

**GCP spot instances:**
- VMs prefer spot pricing (cheaper, preemptible)
- If preempted, task marked FAILED with `preemption` category
- Orchestrator retries automatically

**Code:** `backend/app/vm_lifecycle.py`, `backend/app/orchestrator.py`, `backend/app/reaper.py`

---

## Task History & Audit Trail

Every status transition is logged to `TaskHistory`:

```sql
task_id       | status       | changed_at
------------- | ------------ | --------
task-123      | QUEUED       | 2024-01-15 10:00:00
task-123      | PROVISIONING | 2024-01-15 10:01:00
task-123      | IN_PROGRESS  | 2024-01-15 10:02:00
task-123      | COMPLETED    | 2024-01-15 10:15:00
```

**Uses:**
- Operator visibility (when did this transition happen?)
- Metrics (duration per status)
- Debugging (replay task history to diagnose issues)

**Code:** `backend/app/task_lifecycle.py` → `_record_history()`

---

## Circuit Breaker

If a project's task failure rate exceeds a threshold, the orchestrator **halts new tasks**:

```python
# Default thresholds
FAILURE_RATE_THRESHOLD = 0.5  # 50% failures
WINDOW_SIZE = 10              # Last 10 tasks

if failed_tasks / total_tasks > threshold:
    # Circuit breaker OPEN — no new tasks
    project.circuit_breaker_open = True
```

**Recovery:**
- Operator fixes root cause (e.g., fixes failing tests)
- Manually resets circuit breaker in admin UI
- After reset, new tasks can be queued

**Code:** `backend/app/task_completion.py` → `_check_circuit_breaker()`

---

## Example: Full Lifecycle of a "create" Task

```
1. User clicks "Launch Task" in admin UI
   └─ POST /api/deploy with repo, branch, prompt

2. Orchestrator creates:
   ├─ Project (repo container)
   └─ Task (work unit) status=QUEUED

3. Next orchestrator cycle:
   └─ Task transitioned to PROVISIONING
   └─ GCP VM created (e2-standard-2, spot if available)

4. Agent boots in VM (takes ~1 minute):
   └─ Calls POST /api/agent/claim with JWT
   └─ Receives task payload, GitHub token, prompt
   └─ Task status → IN_PROGRESS

5. Agent works for N minutes:
   ├─ Clones repo
   ├─ Runs Claude Code (streaming output)
   ├─ Sends periodic heartbeats (every 30s)
   ├─ Fixes lint/test failures in fix loop
   └─ Creates PR

6. Agent completes, calls POST /api/agent/status:
   └─ Reports success + PR URL
   └─ Task status → EVALUATING (async post-processing)

7. Orchestrator processes completion:
   ├─ Stores artifacts (PR, commit hash)
   ├─ Computes execution metrics (duration, tokens, cost)
   ├─ Updates project status
   ├─ Schedules VM deletion
   └─ Task status → COMPLETED

8. VM cleanup (reaper):
   └─ Deletes GCP instance
   └─ Frees up compute slot
```

---

## Status Diagram

```
                    ┌─────────────────┐
                    │     QUEUED      │
                    └────────┬────────┘
                             │
                      (orchestrator picks up)
                             │
                    ┌────────▼────────┐
                    │  PROVISIONING   │
                    └────────┬────────┘
                             │
                       (agent claims)
                             │
                    ┌────────▼────────┐
             ┌──────│  IN_PROGRESS    │──────┐
             │      └────────┬────────┘      │
             │               │               │
        (timeout)     (completion)    (error)
             │               │               │
        ┌────▼─┐      ┌──────▼──┐    ┌──────▼──┐
        │FAILED│      │EVALUATING│   │ FAILED  │
        └──────┘      └──────┬───┘   └─────────┘
                             │
                    (post-processing done)
                             │
              ┌──────────────┴──────────────┐
              │                             │
         (success)                    (failure)
              │                             │
         ┌────▼──┐                    ┌────▼──┐
         │COMPLETED│                  │FAILED │
         └─────────┘                  └───┬───┘
                                          │
                                  (retry if budget)
                                          │
                                    (new QUEUED)
```

---

## See Also

- [models.py](../backend/app/models.py) — Database schema
- [task_lifecycle.py](../backend/app/task_lifecycle.py) — Lifecycle implementation
- [RUNNER_RUNBOOK.md](RUNNER_RUNBOOK.md) — Runner operation and diagnosis
