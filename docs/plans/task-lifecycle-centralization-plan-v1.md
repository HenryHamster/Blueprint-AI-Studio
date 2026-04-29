# Task Lifecycle Centralization Plan v1

**Date:** 2026-04-29
**Scope:** centralize task lifecycle writes across backend, agent-facing APIs, admin APIs, orchestrator/reaper loops, completion handlers, and UI expectations.
**Related groundwork:** `backend/app/task_capabilities.py`, `agent/src/taskCapabilities.ts`, `admin-ui/src/taskCapabilities.ts`

## Summary

Task metadata is now centralized, but task lifecycle behavior is still scattered. The next improvement should introduce a backend lifecycle service that owns task writes, row locking, history/event records, websocket broadcasts, and common retry/claim/terminal side effects.

This is not a payload schema lockdown. Task `payloads` and `context` must remain extensible. The service should provide safe mutation primitives and optional typed convenience wrappers for known fields, but it should not reject unknown context keys, unknown future payload fields, or new task-specific metadata unless a caller explicitly asks for validation.

## Current Scattered Surfaces

### Model and Core State

- `backend/app/models.py:22` defines `TaskStatus`.
- `backend/app/models.py:196` defines `Task`.
- `backend/app/models.py:277` exposes `Task.first_payload`.
- `backend/app/models.py:323` defines `TaskHistory`.
- `backend/app/models.py:338` and `backend/app/models.py:348` have SQLAlchemy hooks for status/status message side effects.
- `backend/app/task_locking.py` provides `task_lock()`, but callers still decide how to mutate, history-log, broadcast, and commit.

### Agent-Facing Lifecycle

- `backend/app/routers/agent.py:668` claims tasks and mutates `status`, `context`, `started_at`, token/runtime fields, and history/broadcast behavior.
- `backend/app/routers/agent.py:2035` handles heartbeats and mutates `last_heartbeat`, `latest_status_message`, metrics, and context trace.
- `backend/app/routers/agent.py:2186` handles warm `next_role` claiming and task liveness.
- `backend/app/routers/agent.py:2329` lets agents spawn subtasks.
- `backend/app/routers/agent_status.py:495` handles agent terminal/progress status. This is one of the densest lifecycle writers: task status, context patches, payload runtime fields, artifacts, history, auto-retry, parent propagation, debug spawn, release/account cleanup, result recording, and post-commit work.

### Admin/User Lifecycle

- `backend/app/admin.py:388` bulk archives terminal tasks.
- `backend/app/admin.py:438` bulk approves, retries, or cancels tasks.
- `backend/app/admin.py:865` manually creates tasks.
- `backend/app/admin.py:1027` and `backend/app/admin.py:1119` archive/unarchive tasks.
- `backend/app/admin.py:1155` manually updates status, approval, retry, merge, and context.
- `backend/app/admin.py:1605` handles rollup approval/merge state.
- `admin-ui/src/hooks/useTaskModal.ts:130` posts new tasks.
- `admin-ui/src/App.tsx:531`, `admin-ui/src/App.tsx:532`, `admin-ui/src/App.tsx:717`, and `admin-ui/src/App.tsx:792` wire user actions to backend lifecycle endpoints.
- `admin-ui/src/components/TaskCard.tsx` and `TaskDetailDrawer` encode status/action assumptions in the UI.

### Background Lifecycle

- `backend/app/orchestrator.py:728` dispatches queued tasks, mutates task context, status, provider/runtime fields, account lease fields, provisioning state, and CI polling state.
- `backend/app/orchestrator.py:2512` onward mutates `ci_task.context` and status repeatedly while polling PR/CI state.
- `backend/app/reaper.py:259` reaps stale provisioning/running tasks, releases resources, requeues, fails, completes PR-closed tasks, clears dependencies, and rewrites retry context.
- `backend/app/routers/projects.py:55` creates initial deployment tasks.
- `backend/app/routers/projects.py:466` cancels a project and cancels active tasks.

### Pipeline and Completion Spawning

- `backend/app/plan_completion.py:135` turns plan output into child tasks and dependency edges.
- `backend/app/research_completion.py:500` turns research findings into child tasks, context, dependency edges, and deferred findings.
- `backend/app/review_pipeline.py:737` spawns safeguard tasks.
- `backend/app/review_pipeline.py:812` spawns review/fix pipeline tasks and mutates source task context.
- `backend/app/task_lifecycle.py:495` requeues parent tasks, cancels/replaces children, and clones task state.
- `backend/app/task_lifecycle.py:1037` handles post-commit and merge/review follow-up lifecycle.
- `backend/app/task_completion.py` contains many terminal handlers and follow-up spawns, including investigation, self-audit, security audit, perf, design doc, and remediation flows.
- `backend/app/strategic_spawning.py:855` and `backend/app/strategic_spawning.py:939` spawn strategic phase tasks.
- `backend/app/self_audit_dispatch.py:209` spawns self-audit tasks.
- `backend/app/direction_lifecycle.py:176` spawns investigation tasks for direction diagnosis.
- `backend/app/routers/directions.py:424` and `backend/app/routers/directions.py:525` spawn direction research and strategic cycle tasks.

## Problems to Fix

1. **No single write contract.** Callers directly assign `task.status`, `task.context`, `task.payloads`, `depends_on`, `archived`, and other fields. Each site has to remember row locks, `flag_modified`, history, broadcasts, and commit order.

2. **Side effects are coupled to random call sites.** Result recording, account release, task history, websocket broadcasts, auto-retry, and parent/child propagation happen in whichever file happens to own that workflow.

3. **Concurrent writes remain fragile.** `task_lock()` exists, but the pattern is still "caller loads task, caller mutates task." Centralizing writes gives the codebase one place to enforce lock freshness and avoid lost updates.

4. **Feature work must understand too many files.** Adding a new agent capability currently requires touching task types, prompt behavior, provider routing, spawn sites, completion behavior, admin/UI actions, and possibly retry/reaper behavior.

5. **UI and agent rely on implicit conventions.** Status names, retryability, terminal state, archiveability, action availability, and context diagnostic keys are replicated in TS.

## Design Goals

- **Centralize lifecycle writes, not business logic all at once.** The first target is mutation mechanics and common side effects. Domain handlers can remain domain-specific while delegating writes to the service.
- **Preserve extensibility.** Unknown context keys and payload fields must remain valid. The service should accept arbitrary `context_patch` and `payload_patch` values.
- **Prefer typed operations for lifecycle semantics.** Status transitions, claim, heartbeat, retry, archive, cancel, spawn, dependency updates, and terminal completion should have named methods.
- **Keep row locking local to writes.** Lifecycle methods should acquire a fresh `task_lock()` unless the caller passes an explicit locked task/session context.
- **Make side effects explicit.** History, broadcasts, result recording, account release, and post-commit hooks should be visible options or helper calls, not incidental copy-paste.
- **Allow hard cutovers inside Blueprint.** No external API compatibility guarantee is needed, but the migration should still be staged to keep tests manageable.

## Non-Goals

- Do not replace all completion logic in one PR.
- Do not turn `task.context` into a strict schema.
- Do not require every new task type to add database columns.
- Do not make admin UI the source of truth for lifecycle rules.
- Do not introduce a heavyweight workflow engine before the current lifecycle is encapsulated.

## Proposed Architecture

### New Backend Package

Create a small package, not one giant file:

```text
backend/app/task_lifecycle_service/
  __init__.py
  service.py
  commands.py
  events.py
  transitions.py
  spawn.py
  context_patch.py
```

Suggested split:

- `service.py`: main `TaskLifecycleService` facade.
- `commands.py`: dataclasses for create/spawn/claim/transition/retry/archive requests.
- `events.py`: append `TaskHistory`, optional future `TaskEvent`, websocket broadcast envelopes.
- `transitions.py`: terminal status helpers, allowed transition policy, retryability/archiveability.
- `spawn.py`: child task creation helpers that preserve payload/context extensibility.
- `context_patch.py`: merge/delete/increment helpers for JSONB context without requiring a schema.

### Core Service Contract

The service should be session-scoped:

```python
service = TaskLifecycleService(session)
```

Core methods:

```python
async def create_task(command: CreateTaskCommand) -> Task
async def spawn_child(command: SpawnTaskCommand) -> Task
async def claim(task_id, command: ClaimTaskCommand) -> Task | None
async def heartbeat(task_id, command: HeartbeatCommand) -> Task | None
async def transition(task_id, command: TransitionTaskCommand) -> Task | None
async def patch_context(task_id, patch: ContextPatch, *, reason: str, actor: str) -> Task | None
async def patch_payload(task_id, patch: PayloadPatch, *, reason: str, actor: str) -> Task | None
async def retry(task_id, command: RetryTaskCommand) -> Task | None
async def cancel(task_id, command: CancelTaskCommand) -> Task | None
async def archive(task_id, *, actor: str, archived: bool = True) -> Task | None
async def update_dependencies(task_id, command: DependencyPatchCommand) -> Task | None
```

The method names carry lifecycle meaning. The command payloads stay extensible:

```python
@dataclass
class ContextPatch:
    set: dict[str, Any] = field(default_factory=dict)
    delete: tuple[str, ...] = ()
    increment: dict[str, int | float] = field(default_factory=dict)
    append_unique: dict[str, Sequence[Any]] = field(default_factory=dict)
```

No `allowed_context_keys` list. No required `payload_fields` list. Domain helpers can define local constants for known keys, but the core patcher should not block new keys.

### Locked Write Context

Support two write modes:

1. **Self-locking methods** for normal callers.
2. **Locked batch methods** for hot loops that need to mutate a row several times before commit.

```python
async with service.locked_task(task_id, allowed_statuses=(TaskStatus.QUEUED,)) as task_ops:
    task_ops.patch_context(ContextPatch(set={"_infra_wait": True}))
    task_ops.transition(TaskStatus.PROVISIONING, reason="dispatch")
    task_ops.add_history("DISPATCHED", details={...})
```

The lock wrapper should:

- acquire `task_lock(session, task_id)`,
- yield a small operation object,
- call `flag_modified` when JSONB fields change,
- queue history/event/broadcast side effects,
- require the caller to commit or use a service `commit()` helper.

### History and Events

Keep `TaskHistory` initially. Optionally add `TaskEvent` in a later phase if the current history table is too coarse.

Every lifecycle write should support:

- `actor`: `agent`, `admin`, `orchestrator`, `reaper`, `pipeline`, `system`
- `reason`: stable reason code, not just prose
- `details`: compact JSON object
- `broadcast`: default true for user-visible status/action changes

Example reason codes:

```text
task.created
task.claimed
task.heartbeat
task.status.completed
task.status.failed
task.retry.auto
task.retry.admin
task.cancel.admin
task.archive.admin
task.context.dispatch_runtime
task.context.review_loop
task.spawn.plan_child
task.spawn.research_child
task.spawn.review_pipeline
task.spawn.investigation
```

## Component Encapsulation Plan

### 1. Backend Task Model Boundary

Keep `Task` as the persistence model. Do not hide all direct reads. Reads can continue through SQLModel queries.

Encapsulate writes:

- New write helpers call `task_lock()`.
- JSONB changes use service patch helpers so `flag_modified` is never forgotten.
- Status changes go through `transition()` unless inside model hooks or tests.
- History writes go through `events.append_history()`.
- Broadcasts go through one service path.

Acceptance:

- Production code should have no direct `task.status = ...` outside the lifecycle service, tests, and temporary allowlisted migration points.
- Production code should have no direct `task.context = ...` for lifecycle mutations outside the lifecycle service, except read-only construction of new task context before `create_task`/`spawn_child`.

### 2. Agent API Boundary

Refactor:

- `routers/agent.py:claim_task` -> `service.claim()`
- `routers/agent.py:agent_heartbeat` -> `service.heartbeat()`
- `routers/agent.py:next_role` -> `service.claim(warm=True)` or `service.claim_next_role()`
- `routers/agent.py:spawn_subtask` -> `service.spawn_child()`
- `routers/agent_status.py:update_agent_status` -> `service.transition()` plus artifact/post-commit orchestration

The agent TypeScript should not gain lifecycle business rules. It should keep posting:

- `/api/agent/claim`
- `/api/agent/heartbeat`
- `/api/agent/status`
- `/api/agent/next-role`

Backend maps those requests onto the lifecycle service.

Acceptance:

- Claim retry/idempotency behavior remains unchanged.
- Heartbeat remains tolerant of stale/in-flight locks and returns retry-friendly statuses.
- Terminal status remains idempotent when a prior terminal POST succeeded but response was lost.

### 3. Admin API Boundary

Refactor:

- manual create -> `service.create_task()`
- bulk retry -> `service.retry(..., actor="admin")`
- single retry/status update -> `service.transition()` or `service.retry()`
- cancel -> `service.cancel()`
- archive/unarchive -> `service.archive()`
- approval/merge pathways -> domain-specific merge code calls service for final status/context/history.

Admin-specific behavior stays in admin routes where it is genuinely UI policy, but task mutation moves into the service.

Acceptance:

- UI calls do not change initially.
- All admin task actions create consistent history and broadcasts.
- Admin retry and bulk retry use the same retry-state reset helper.

### 4. Orchestrator Boundary

Refactor dispatch into a smaller pipeline:

```text
read candidates -> select eligible ids -> service.locked_task(id) -> dispatch mutation -> provision side effect -> service transition/patch on result
```

Move these write patterns behind service methods:

- infra wait set/clear,
- blacklisted/sticky account context,
- provider/runtime context,
- provisioning transition,
- provision retry/fail,
- CI loop context updates,
- CI terminal status updates.

Keep candidate selection read-heavy and SQL-efficient. Do not force every read into service calls.

Acceptance:

- The "loaded once, mutated many times" pattern disappears from orchestrator hot paths.
- Each dispatch/CI mutation happens under a fresh lock or a locked batch.
- CI `review_loop` changes use `patch_context()`.

### 5. Reaper Boundary

Refactor stale task handling into service operations:

- `requeue_stale_task()`
- `fail_stale_task()`
- `complete_pr_closed_task()`
- `release_slot_once()`
- `clear_waiting_dependencies()`

The reaper can retain detection logic. Mutation should go through the service.

Acceptance:

- Resource release markers like `_slot_released` are set atomically with lifecycle transition.
- Reaper retry context patching uses the same context patcher as agent-status retry.
- Dependency cleanup is a named lifecycle operation with tests.

### 6. Completion and Pipeline Boundary

Keep parsing and domain interpretation local:

- plan parsing stays in `plan_completion.py`,
- research finding parsing stays in `research_completion.py`,
- review feedback interpretation stays in `review_pipeline.py`,
- strategic direction logic stays in strategic modules.

Move creation/mutation mechanics into service:

- child tasks from plan/research,
- review/fix/safeguard tasks,
- investigation/remediation follow-ups,
- strategic phase tasks,
- self-audit tasks,
- parent status transitions,
- deferred findings/context patches.

Acceptance:

- A new child/follow-up task is created through one spawn API.
- Spawn APIs accept arbitrary payload/context metadata.
- `TaskHistory(action="SPAWNED")` is generated by the spawn API, not each caller.

### 7. Capability and Routing Boundary

The existing capability registry should remain metadata-only. Extend it only for lifecycle policy that is not payload shape:

- read-only vs write,
- launchable,
- provider-routable,
- default category,
- maybe default retry policy in a later phase.

Do not add:

- required payload fields,
- allowed context fields,
- fixed agent output schema.

Provider routing should call the capability registry for known metadata and gracefully handle unknown task types by using conservative defaults.

### 8. UI Boundary

The admin UI should consume backend-discovered lifecycle/capability metadata over time:

- task type options,
- terminal statuses,
- retryable statuses,
- archiveable statuses,
- available actions per task.

Initial endpoint:

```text
GET /api/admin/task-capabilities
```

Later endpoint:

```text
GET /api/admin/tasks/{id}/actions
```

The UI should still tolerate unknown task fields and display raw context in the drawer.

Acceptance:

- UI no longer manually decides every lifecycle action from scattered local status checks.
- Unknown future context fields are displayed, not discarded.

## Phased Implementation

### Phase 0: Guardrails and Inventory

Deliverables:

- Add `backend/testing/test_task_lifecycle_service_inventory.py`.
- Add a static check that finds direct production writes to:
  - `task.status =`
  - `task.context =`
  - `task.payloads =`
  - `TaskHistory(`
- Maintain an allowlist for files not migrated yet.
- Document each allowlisted direct write with owner and phase.

Why first:

- Prevents the centralization work from drifting.
- Lets parallel agents migrate file groups safely.

Tests:

- Static allowlist test.
- No functional behavior change.

### Phase 1: Service Skeleton and Pure Helpers

Deliverables:

- Add `backend/app/task_lifecycle_service/`.
- Implement `ContextPatch` and JSONB patch semantics.
- Implement transition policy helpers:
  - `is_terminal_status`
  - `is_active_status`
  - `is_retryable_status`
  - `is_archiveable_status`
  - `can_transition`
- Implement history append helper.
- Implement broadcast envelope helper.

No production call sites migrated yet.

Tests:

- Context patch set/delete/increment/append behavior.
- Unknown keys preserved.
- Nested dict values preserved.
- No mutation of input dicts.
- Transition helper matrix.

### Phase 2: Create and Spawn Centralization

Deliverables:

- Implement `create_task()` and `spawn_child()`.
- Migrate:
  - `admin.py:create_task_manually`
  - `routers/projects.py:deploy_blueprint`
  - `routers/directions.py:create_direction_research_task`
  - `routers/directions.py:trigger_strategic_cycle`
  - `plan_completion.py`
  - `research_completion.py`
  - `review_pipeline.py` spawn paths
  - `strategic_spawning.py`
  - `self_audit_dispatch.py`
  - `direction_lifecycle.py`
  - follow-up spawns in `task_completion.py`

Rules:

- The service accepts arbitrary payload/context dicts.
- The service validates only basic persistence invariants:
  - project id exists when required,
  - parent id exists when required,
  - `depends_on` values are UUID strings when provided,
  - title is not empty if caller expects UI-visible task.
- Do not validate task-specific payload fields here.

Tests:

- Spawn history generated consistently.
- Parent/child/dependency fields preserved.
- Future payload/context fields survive round trip.
- Existing plan/research/review spawn tests updated to assert behavior, not direct construction.

### Phase 3: Status, Context, and Retry Mutations

Deliverables:

- Implement:
  - `transition()`
  - `patch_context()`
  - `patch_payload()`
  - `retry()`
  - `cancel()`
  - `archive()`
- Migrate admin retry/cancel/archive/status update.
- Migrate common retry-state reset logic out of `admin.py`, `agent_status.py`, and `reaper.py`.
- Migrate parent requeue mechanics in `task_lifecycle.py` where feasible.

Rules:

- `retry()` resets lifecycle-owned keys using delete/set patches, but does not clear unknown domain keys.
- `patch_context()` can delete known lifecycle keys without blocking unknown keys.
- Terminal transitions are idempotent when the requested terminal state already matches.

Tests:

- Two concurrent `patch_context()` calls on same task do not lose keys.
- Admin retry and bulk retry produce identical reset state.
- Cancel sets suppression metadata when requested.
- Archive rejects non-terminal tasks unless explicitly overridden by a service option used only for maintenance.

### Phase 4: Agent Claim, Heartbeat, and Status Endpoint

Deliverables:

- Migrate `routers/agent.py:claim_task`.
- Migrate warm `next_role` claim path.
- Migrate `agent_heartbeat`.
- Migrate `routers/agent_status.py:update_agent_status` status/context writes.

Keep artifact creation and domain post-processing near `agent_status.py` initially, but have it call service methods for task writes.

Rules:

- Claim/heartbeat/status must use `task_lock()`.
- Agent-visible context filtering remains in agent router response assembly.
- Status endpoint must preserve existing idempotency semantics for lost terminal responses.

Tests:

- Existing `testing/test_agent.py`.
- Existing `testing/test_pipeline.py` and `testing/test_pipeline_health.py`.
- New concurrent status/heartbeat test:
  - heartbeat writes `_tool_trace`;
  - status writes terminal context;
  - assert no context loss.
- Claim replay window behavior remains covered.

### Phase 5: Orchestrator and CI Loop Hot Paths

Deliverables:

- Replace direct task mutation in dispatch loop with locked service batches.
- Replace CI loop `ci_task.context` and status writes with service calls.
- Replace provision failure/retry mutation blocks with named service methods.

Rules:

- Candidate scans remain direct SQL reads.
- Every write is either:
  - a self-locking service method, or
  - inside `async with service.locked_task(...)`.
- Commit/rollback boundaries are explicit after each locked batch.

Tests:

- Existing `testing/test_task_locking.py`.
- Add concurrent dispatch test with two orchestrator coroutines against same queued task.
- Add CI loop context race test for `review_loop` updates.
- Run targeted pipeline health tests.

### Phase 6: Reaper Centralization

Deliverables:

- Migrate stale task requeue/fail/complete paths.
- Migrate dependency cleanup.
- Migrate slot release marker updates.
- Migrate retry context enrichment.

Rules:

- Detection stays in reaper.
- Mutation moves into service.
- Reaper should skip locked rows, not block or throw 500-class errors.

Tests:

- Existing `testing/test_reaper.py`.
- Add race test: reaper attempts to fail a task while agent terminal status arrives; assert terminal state is deterministic and context is merged.
- Add dependency cleanup test through service method.

### Phase 7: UI/Capability Discovery

Deliverables:

- Backend endpoint exposes task capabilities and lifecycle policy:
  - task types,
  - labels,
  - agent mode,
  - launchable/provider/filter flags,
  - status groups,
  - available global actions.
- Admin UI reads endpoint and falls back to local registry if unavailable.
- Optional task-specific actions endpoint returns action availability:
  - retry,
  - cancel,
  - approve,
  - archive,
  - unarchive,
  - view logs.

Rules:

- UI must keep displaying unknown context fields.
- UI should not require frontend deploy for a new backend task type to be visible as raw task type.

Tests:

- Admin UI task capability tests.
- UI action rendering tests for unknown task type.
- Backend endpoint snapshot-like test.

### Phase 8: Remove Allowlist and Enforce Boundary

Deliverables:

- Tighten static test from Phase 0.
- Remove migrated direct-write allowlist entries.
- Add code review convention: task writes go through lifecycle service.

Allowed exceptions:

- `models.py` field definitions/hooks.
- Tests/factories.
- Migrations.
- One-off data repair scripts.
- Creation of unsaved `Task(...)` objects only inside service or tests.

Tests:

- Static direct-write boundary test passes with minimal allowlist.
- Full backend suite.
- Agent typecheck/tests.
- Admin UI build/tests.

## API and Data Shape Notes

### Context Remains Open

The lifecycle service must treat `context` as an open metadata bag. It may provide helpers for common lifecycle-owned keys:

```python
LifecycleContextKeys.INFRA_WAIT
LifecycleContextKeys.RETRY_AFTER
LifecycleContextKeys.REVIEW_LOOP
LifecycleContextKeys.SLOT_RELEASED
```

Those constants are documentation and typo protection, not a whitelist.

### Payloads Remain Open

Task payload validation can stay where it is today. The lifecycle service should not decide whether a `create`, `research`, or future task type has valid task-specific fields.

For internal spawns, call existing `validate_internal_payload()` where callers already do so. The service can accept a `validated_payload=True` or `validate_payload=False` command option if needed, but the default should avoid silently narrowing future capabilities.

### Status Policy Is Closed

Unlike context/payload fields, `TaskStatus` is a real lifecycle state machine. It is acceptable for the service to centralize status groups and transition rules.

Recommended status groups:

```text
queued: QUEUED
active: PROVISIONING, IN_PROGRESS, EVALUATING
terminal: COMPLETED, FAILED, CANCELLED
```

### Event Details Are Open

History/event `details` should be an open JSON object. Use stable reason codes for filtering, but do not constrain detail fields globally.

## Testing Strategy

### Unit Tests

- `test_task_lifecycle_context_patch.py`
- `test_task_lifecycle_transitions.py`
- `test_task_lifecycle_spawn.py`
- `test_task_lifecycle_retry.py`
- `test_task_lifecycle_events.py`

### Concurrency Tests

- Two concurrent `patch_context()` operations preserve both writes.
- Heartbeat and terminal status preserve both `_tool_trace` and terminal diagnostics.
- Dispatch and reaper touching same task produce one valid final status.
- CI loop repeated `review_loop` updates do not overwrite merge/failure flags.

### Integration Tests

Run targeted backend tests after each phase:

```bash
bash scripts/test-backend.sh --postgres testing/test_task_lifecycle_service.py
bash scripts/test-backend.sh --postgres testing/test_agent.py
bash scripts/test-backend.sh --postgres testing/test_task_locking.py testing/test_pipeline_health.py testing/test_pipeline.py
bash scripts/test-backend.sh --postgres testing/test_reaper.py
```

Frontend/agent:

```bash
cd agent && npm run typecheck && npm test
cd admin-ui && npm run build && npm run test:run
```

## Migration and Rollout Order

Recommended PR split:

1. **PR 1:** service skeleton, context patcher, transition helpers, static allowlist test.
2. **PR 2:** create/spawn centralization for low-contention spawn sites.
3. **PR 3:** admin retry/cancel/archive/create/status centralization.
4. **PR 4:** agent claim/heartbeat/status centralization.
5. **PR 5:** orchestrator dispatch and CI loop centralization.
6. **PR 6:** reaper centralization.
7. **PR 7:** backend capability/lifecycle discovery endpoint and UI consumption.
8. **PR 8:** remove allowlist, enforce direct-write boundary.

This order deliberately moves low-risk creation paths before the densest status/terminal paths, then tackles the highest-contention background loops once the service is proven.

## Risks and Mitigations

### Risk: Service Becomes a God Object

Mitigation:

- Keep domain parsing/decision logic in existing modules.
- Service owns writes and common side effects only.
- Split into package modules early.

### Risk: Artificially Blocking New Fields

Mitigation:

- Do not add `allowed_context_keys`.
- Do not add task-specific payload schemas to the lifecycle service.
- Add tests that unknown context/payload fields survive create, spawn, retry, and terminal transitions.

### Risk: Huge Refactor Breaks Pipeline Behavior

Mitigation:

- Phase by caller group.
- Preserve endpoint contracts.
- Keep old tests and add service-level tests before migrating hot paths.
- Use static allowlist to track remaining direct writes.

### Risk: Event/History Duplicates

Mitigation:

- Centralize history creation before adding a new event table.
- Use stable reason codes and task/status/action idempotency keys where needed.

### Risk: Lock Contention Regressions

Mitigation:

- Default background methods to `skip_locked=True`.
- Handler paths return 429/Retry-After for temporary lock contention.
- Keep read-only candidate scans outside write locks.

## Acceptance Criteria

- New task/task-child creation goes through the lifecycle service in migrated modules.
- Status/context/payload mutations in migrated modules go through lifecycle service methods.
- Every lifecycle write is row-locked or explicitly documented as safe without a lock.
- Task history and websocket broadcast behavior is consistent across agent/admin/background writes.
- Unknown context keys and payload fields are preserved by service operations.
- Admin UI can keep working with existing endpoints during rollout.
- Final enforcement test catches new direct production writes to task lifecycle fields.

## First Implementation Slice

Start with Phase 0 and Phase 1:

1. Add `backend/app/task_lifecycle_service/context_patch.py`.
2. Add `backend/app/task_lifecycle_service/transitions.py`.
3. Add `backend/app/task_lifecycle_service/events.py`.
4. Add `backend/app/task_lifecycle_service/service.py` with `patch_context()` and `transition()` only.
5. Add tests proving:
   - context patches preserve unknown fields,
   - concurrent context patches do not lose updates,
   - status transition helper matches current terminal/active semantics,
   - history entries are produced through the service.
6. Add direct-write static inventory test with allowlist, but do not fail existing direct writes outside the allowlist yet.

This slice is useful even before migrating every caller: it creates the central write primitive and a measurable boundary without forcing a broad behavioral refactor in one step.
