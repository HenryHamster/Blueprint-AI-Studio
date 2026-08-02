# Pipeline Operability Repair — Unified Implementation Plan

**Date:** 2026-07-25
**Status:** implementation-ready
**Delivery:** one integration branch and one pull request targeting `automain`
**Intent:** repair the concrete operability failures observed during the long-running
Strategic/MARL session without redesigning the whole pipeline.

## 1. Outcome

This change should make the following operator experience ordinary:

1. Strategic can submit any legitimate task through one `spawn_task` contract.
   Implementation-ready work is neither forced through a misleading `spawn_fix`
   lane nor discarded by a routine per-review quota.
2. Accepted spawn requests are idempotent and durable. An exceptional burst can
   be deferred and drained rather than rejected and forgotten.
3. Account usage is either visible and fresh or visibly unsupported/broken with
   a useful reason. Anthropic must be verified end to end; Codex is attempted
   only through a stable machine-readable interface.
4. Guaranteed-local compute does not require monetary approval and never
   silently falls back to paid cloud execution.
5. Every terminal compute job has useful, durable evidence: readable authored
   source, effective launcher provenance, logs, actual placement/hardware,
   outcome, artifacts, and a compute document even when the workload crashes.
   Before dispatch, the same normalized contract is available as a readable
   run preview with validation, placement, cost, and capability diagnostics.
6. Task workflow state and compute execution outcome are shown as separate
   truths. A cancelled task cannot hide a job that already succeeded.
7. Strategic helpers are self-describing, accept reliable file/stdin input, and
   distinguish safe patches from destructive replacement.
8. Strategic stops after two hours without operator interaction by default,
   using the existing graceful handoff, and exposes one clean durable activity
   line while it runs.
9. Strategic can durably wait for a specific task to finish. Strategic-spawned
   tasks are watched automatically, and a fired watch remains in the Resident
   agenda until Strategic explicitly acknowledges the result.

## 2. Settled product decisions

These decisions are not open during implementation:

- `spawn_fix` is retired and removed. `spawn_task` is the only dispatch contract.
- Spawn limits are abuse circuit breakers, not ordinary work quotas.
- Accepted spawn intent is durable before admission is evaluated.
- Exceptional spawn bursts are deferred and auto-drained FIFO; normal work is
  dispatched immediately.
- The existing project resource envelope and runner pinning contracts remain
  the source of truth for local placement. No new placement toggle or local
  accounting system is introduced.
- Guaranteed-local execution has zero billable compute exposure and no monetary
  approval. Soft local preference remains approval-gated because cloud fallback
  is possible.
- Workload failure is a documented experiment observation. Infrastructure or
  orchestration failure remains a task failure.
- Strict experiment gates may opt into `requires_valid_measurement`.
- Normal problem-model mutation is patch-based. Full replacement is reserved
  for bootstrap/recovery and requires an exact base revision, explicit
  confirmation, and a previewable diff.
- The Strategic backstop is idle/unattended time, not an absolute lifetime.
  Default: 7,200 seconds. Operator interaction re-arms it.
- No broad turn-system redesign. Existing batching and autonomous-turn
  preemption stay intact unless measurement proves a narrower defect.
- Project-state polling remains the wake mechanism. Targeted task watches add
  durable identity and acknowledgement; they do not introduce a generic event
  bus, webhook system, or callback runtime.
- Strategic-spawned tasks receive a one-shot terminal watch by default. Callers
  may opt out for intentionally uninteresting background work.
- This is one PR, but implementation is split into non-overlapping commits and
  workstreams so it can be built and reviewed safely.

## 3. Non-goals

The PR must not expand into:

- general platform or MARL project work;
- historical usage burn-rate analytics;
- new cloud/local scheduling policy;
- GPU-hour pricing or local-resource billing;
- a new compute platform or experiment orchestration engine;
- a rewrite of the Compute Jobs panel;
- interrupting an in-progress operator reply with a newer operator message;
- a broad Strategic dashboard;
- arbitrary event predicates, external webhooks, cron expressions, or a
  general-purpose project automation language;
- removing old task provenance or rewriting existing task/job history;
- modifying existing Alembic migrations;
- modifying `.github/workflows/`.

## 4. Relationship to existing plans

This plan narrows and updates existing work rather than superseding all of it.

### `docs/plans/experiment-loop-redesign-20260711.md`

That plan correctly records that terminal `command_log` already has a viewer in
`ComputeJobsPanel`. This PR does not rebuild it. It closes the remaining seams:

- runner logs stranded as `runner_command_log`;
- logs lost or not normalized on timeout/cancellation races;
- logs absent from task detail, Strategic evidence, and compute documents;
- workload failures skipping document creation;
- authored source hidden behind generated Base64/Bash wrappers;
- requested `machine_type` presented as if it were observed hardware.

### `docs/plans/autonomous-spawn-budget-redesign-v1.md`

The global autonomous-volume backstop remains. This PR removes the separate
Strategic per-review policy fork that turned a productive review into a hard
12-task quota. The new Strategic request admission layer feeds into existing
global autonomy/concurrency/cost protection rather than replacing it.

### `docs/plans/strategic-resident-redesign.md`

That document is historical and explicitly retired. This work retains the
supported Chat/Workday `/strategic-session` path, including its durable replies,
cooperative turn preemption, and bounded unattended cadence. It does not revive
the retired shadow-turn or parallel Resident control-plane architecture.

## 5. Target architecture

### 5.1 Strategic operation contract registry

The backend becomes authoritative for the complete Strategic operation
contract, not only operation names.

Each operation declares:

- backend operation name;
- CLI alias and exposure;
- strict Pydantic input model with `extra="forbid"`;
- summary and examples;
- mutation kind: append, patch, replace, dispatch, or internal;
- input mode: JSON or text file;
- retry classification;
- compatibility aliases/forms;
- task capability source where relevant.

The registry generates:

- JSON Schema/OpenAPI-compatible operation schemas;
- the checked-in cross-language agent manifest;
- `./strategic --help`;
- `./strategic help <op>`;
- `./strategic schema <op>`;
- compact prompt/tool documentation;
- handler/contract parity tests.

Database-dependent validation remains in domain services. Structural
validation, unknown-field rejection, help, and examples come from one source.

### 5.2 Durable Strategic spawn requests

Add a `StrategicSpawnRequest` model/table.

Required fields:

- `id`;
- `project_id`;
- `parent_task_id`;
- `idempotency_key`;
- `task_type`;
- normalized requested specification;
- optional `direction_id`, criterion/debt references, and provenance;
- exact normalized specification hash;
- state: `accepted`, `deferred`, `dispatched`, `cancelled`, or `rejected`;
- admission reason and policy snapshot;
- `not_before`;
- eventual `task_id`;
- timestamps.

Required database guarantees:

- unique `(parent_task_id, idempotency_key)`;
- index on `(state, not_before, created_at)` for draining;
- index on `project_id`;
- foreign keys follow the repository’s established project/task deletion
  policy.

Submission flow:

```text
validate capability and normalize specification
        |
persist request with stable idempotency identity
        |
evaluate admission
        |
normal: create queued Task and mark dispatched
burst: mark deferred and return 202
```

The request survives a timeout, retry, parent-review completion, and temporary
capacity pressure. Retrying the same key returns the same request/task.

### 5.3 Capability-driven task dispatch

Replace the two Strategist booleans with one policy:

```text
disabled | direct | requires_context
```

Initial mapping:

- direct: `research`, `web_research`, `design`, `plan`,
  `compute_experiment`, `create`, `refactor`, `qa`;
- requires context: `fix`, requiring valid PR/branch context;
- disabled: internal/system-only task types.

Direction association is optional and independent of code-versus-research
classification. When a direction is supplied, existing criterion/evidence
wiring remains intact.

There is no second dispatch lane or compatibility execution path for
`spawn_fix`.

### 5.4 Usage attempt state

Keep `UsageSnapshot`/`rate_limit_usage` as the normalized last-good reading.
Extend the persisted account meter state with:

- `attempted_at`;
- `last_success_at`;
- `outcome`;
- `source`;
- safe `error_code`;
- optional HTTP status class;
- `supported`;
- freshness/staleness.

Never persist response bodies, tokens, secrets, or sensitive provider details.
A failed attempt preserves the last-good snapshot and records why it is stale.

Anthropic:

- meter eligibility is based on configured account capability, not whether an
  idle in-memory secret happens to be hydrated;
- rehydrate Secret-Manager credentials immediately before probing and outside
  the pool lock;
- a valid numeric five-hour window is sufficient;
- seven-day data is optional;
- classify authentication, endpoint, timeout, header, and schema failures.

Codex/OpenAI OAuth:

- first perform a compatibility spike against the deployed Codex runtime;
- use only a stable machine-readable app-server/CLI interface;
- isolate each probe in an account-specific ephemeral `CODEX_HOME`;
- serialize refresh-token lineage access;
- if no supported interface exists, persist and show `unsupported`;
- OpenAI API-key billing is not presented as Codex-plan headroom.

### 5.5 Execution-aware monetary approval

Create one shared `ComputeSpendDecision` service used by every compute authoring
and terminal-creation path.

Inputs include the validated project resource envelope, runner target/pin,
fallback policy, declared cap, and approval threshold.

Outputs include:

- execution mode: `local_only` or `cloud_possible`;
- billable estimate;
- whether monetary approval is required;
- threshold;
- stable reason code.

Rules:

| Existing execution contract | Billable approval behavior |
| --- | --- |
| `require_local_runner=true` | `$0`, no monetary approval, wait if unavailable |
| Valid hard runner target | `$0`, no monetary approval, wait if unavailable |
| Valid strict runner target | `$0`, no monetary approval, wait if unavailable |
| Soft runner preference | Existing cloud-capable approval behavior |
| Default/cloud-capable | Existing cloud approval behavior |

The project spend ceiling uses billable exposure. The declared cap/deadline may
remain as a local runtime safety bound but must not be labelled predicted spend.

Existing held tasks are not bulk-cleared based only on stored caps. Either:

- make the change forward-only; or
- re-evaluate only tasks whose current validated contract guarantees local-only
  execution, recording an audit reason.

Use the forward-only path in this PR unless a targeted reconcile endpoint is
already available and trivial to reuse.

### 5.6 Compute execution manifest and runtime provenance

Add versioned JSON fields to `ComputeJob`:

#### `execution_manifest`

Immutable after launch preparation:

- version;
- authored kind and readable source/arms;
- transport kind and source digest;
- effective entrypoint/setup;
- container/image;
- working directory where applicable.

The generated Base64/Bash wrapper remains an execution detail. New UI/document
code reads the authored form from the manifest and never reverse-engineers it.

Legacy behavior:

- known exact legacy wrapper: show a labelled compatibility decode;
- unknown wrapper: show exact raw command;
- never add more heuristic decoders.

#### `runtime_provenance`

Populated at placement/claim and terminalization:

- actual placement/backend;
- requested resources;
- observed/attested hardware;
- runner identity and allocated device indices;
- environment/runtime/container details when available;
- start/finish times;
- exit code;
- failure classification;
- cancellation source/timing;
- log availability/truncation/reference.

`ComputeJob.machine_type` remains backward-compatible as the requested scheduler
shape. UI labels it “Requested,” never “Executed on.”

### 5.7 Canonical terminal logs

Normalize every backend onto one internal terminal log shape at ingestion:

```json
{
  "available": true,
  "text": "redacted bounded combined log",
  "stream": "combined",
  "truncated": false,
  "source": "runner | local_subprocess | gcp"
}
```

Do not require separate stdout and stderr when the current executor combines
them. Do not expose unbounded output.

Runner reports must be normalized before cancellation or terminalization can
bypass the ordinary poller. The API returns a typed `terminal_log`; legacy
`polling_metadata.command_log` remains readable during migration.

### 5.8 One terminal compute-document path

Add idempotent `ensure_compute_execution_document(job_id)`, invoked after every
terminal `ComputeJob` transition independently of parent task state.

Add nullable, indexed, unique `KnowledgeDocument.source_compute_job_id` for
compute execution reports.

The existing compute document gains:

- authored source and effective launcher summary;
- requested versus actual placement/hardware;
- job outcome, exit code, failure class, and cancellation;
- bounded redacted log excerpt and fuller-log reference;
- metrics, criterion result, progress, and artifacts;
- short factual interpretation;
- parent task provenance.

It must be created for:

- successful measurement;
- negative result;
- invalid measurement;
- non-zero workload exit;
- timeout;
- cancellation after execution began;
- provisioning/infrastructure failure where a job row exists.

Task and job semantics:

| Situation | Task workflow truth | Job execution truth |
| --- | --- | --- |
| Hypothesis/criterion not met | completed documented experiment | execution succeeded |
| Workload exits non-zero after starting | completed documented observation, neutral evidence | workload failure |
| Cannot provision/launch | failed | infrastructure failure |
| Task cancelled before start | cancelled | cancelled/not started |
| Task cancelled after job succeeded | cancelled | succeeded and documented |

`requires_valid_measurement=true` may keep strict dependents gated, but does not
erase the execution report.

### 5.9 Safe helper transport and mutations

Canonical CLI forms:

```text
./strategic --help
./strategic help update-direction
./strategic schema update-direction

./strategic update-direction --json '{...}'
./strategic update-direction --file /tmp/update.json
./strategic update-direction --stdin < /tmp/update.json
./strategic set-running-update --file /tmp/running-update.md
```

Exactly one input transport is accepted. JSON is validated locally and sent
without shell command-substitution or heredoc layers. Raw positional JSON stays
temporarily compatible with a warning.

Normalize `update-direction` to direct fields:

```json
{
  "direction_id": "d-123",
  "priority": "high",
  "gap_id": "g-4"
}
```

The nested `{direction_id, changes:{...}}` form remains a one-release adapter.
Mixing forms is rejected. Unknown fields return structured field errors.

Problem-model operations:

- `patch-problem-model`: base revision, patch, and separate transient actions;
- `replace-problem-model`: base revision, full model, `confirm_replace=true`;
- dry-run returns a diff without mutation;
- stale revisions return a structured conflict;
- `set-problem-model` temporarily maps to safe patch semantics and returns a
  deprecation warning.

### 5.10 Minimal Strategic lifecycle/status repair

Retain the existing message-polling and turn model:

- up to 20 messages currently present are combined;
- an operator message preempts autonomous work at the normal poll cadence;
- a message arriving during an operator reply waits for the next turn;
- no message is silently marked handled before its durable reply receipt.

Change:

- default `strategic_session_max_unattended_seconds` from 14,400 to 7,200;
- keep the existing configurable range unless it prevents the new default;
- emit a short pre-stop warning;
- use existing graceful end, summary, message handoff, and authority release.

Add one bounded durable activity object:

- phase: working, waiting, retrying, stopping, ended;
- phase start;
- short last-action summary;
- pending operator-message count;
- next review time where applicable;
- idle time remaining.

Render this as one compact status line. Extend telemetry only with operator batch
size/deferred count and pickup lag for messages that arrived during a reply.
Do not add a dashboard or debounce until evidence justifies it.

### 5.11 Compute authoring clarity and pre-dispatch receipt

The terminal evidence work shortens diagnosis after a failed run, but Strategic
also needs to know what the system understood before it spends a runner slot.

Add one server-owned compute contract preview using the exact normalizer and
policy services used by task/job creation. Do not implement a second advisory
validator.

The preview returns:

- normalized experiment contract and contract version;
- readable authored command/arms;
- effective launcher summary and source digest;
- declared metric/result-file/artifact expectations;
- intended execution mode and fallback policy;
- requested runner capabilities, image, GPU, and tool requirements;
- guaranteed-local versus cloud-possible monetary decision;
- deterministic validation errors and warnings;
- stable failure fields: `phase`, `classification`, `code`, `retryable`, and a
  short corrective hint.

The UI and Strategic helper expose the preview before dispatch/approval. The
eventual `ComputeJob.execution_manifest` records the same normalized contract
and a digest of the preview so the operator can compare authored, approved, and
executed forms.

Add a bounded target-runner preflight only where the existing execution backend
can run it in the same selected environment without creating a second scheduler:

- verify the declared launcher can be materialized;
- run language syntax validation when the authored kind declares a supported
  language;
- verify requested GPU visibility/capability;
- verify image/runtime/tool availability;
- verify the result/artifact destination can be created;
- return the canonical bounded preflight log and failure classification.

Do not infer imports from arbitrary source, synthesize a different workload, or
claim that preflight proves the experiment will succeed. If same-environment
preflight is unavailable, the receipt says `not_supported` and the main job may
still run under the existing policy.

The existing compute document includes the contract preview and preflight
receipt. This turns common trial-and-error failures into one readable chain:

```text
authored contract
  -> normalized preview
  -> selected placement and monetary decision
  -> preflight receipt
  -> effective execution
  -> terminal observation/document
```

### 5.12 Durable targeted Strategic task watches

The Resident already receives `work_state_rev` changes when a
Strategic-created task or descendant changes state. That mechanism wakes
autonomous review promptly, but it only says that the project changed. It does
not preserve the intention “inspect and report task X when it finishes,” so a
later rotating-focus turn can overlook the relevant completion.

Add a narrow `StrategicWatch` persistence model. Version one supports exactly:

```text
target_kind = task
condition = terminal
```

Do not repurpose `TaskWakeRegistration`: that primitive deliberately parks and
later advances a task’s lifecycle. A Resident watch is a notification/agenda
receipt and must not park the long-lived Strategic task. Reuse its
source-identity, deadline, and exactly-once delivery patterns where useful.

Required fields:

- `id`;
- `project_id`;
- `created_by_task_id`;
- optional current `strategic_session_id`;
- `target_task_id`;
- bounded `purpose` describing why Strategic is waiting;
- state: `pending`, `fired`, `acknowledged`, `cancelled`, or `expired`;
- terminal status and bounded result/document references captured on fire;
- timestamps and optional deadline;
- acknowledgement disposition and note.

Required behavior:

1. Unified `spawn_task` creates a terminal watch by default after it creates or
   links the child task. `watch=false` is an explicit opt-out.
2. `watch-task` lets Strategic watch an existing project task. Registering
   against an already-terminal task fires immediately.
3. The existing heartbeat work-state query remains the wake trigger. On a state
   change, the backend resolves pending watches and returns bounded fired-watch
   receipts. No new callback worker is required.
4. Fired receipts are injected prominently into the next autonomous prompt with
   task title, terminal state, document/artifact links, and the original
   purpose.
5. A receipt remains in subsequent prompts until Strategic calls `ack-watch`
   with a disposition such as `reported`, `acted`, `no_action`, or `superseded`.
6. Watches survive Resident process restart and session turnover at project
   scope. A new active Resident inherits pending/fired unacknowledged watches.
7. A bounded pending/fired count appears in the compact Strategic status line.
8. Expiry is a safety cleanup, not silent success: an expired watch produces one
   visible receipt before acknowledgement.

Do not generalize version one to PR checks, arbitrary database predicates,
timers, directions, provider usage, or qualification events. If real usage
shows a second target is needed, add a new explicit target kind and resolver.
Many such states are already represented by tasks and need no separate hook.

## 6. Workstreams and ownership

Use one integration branch created from the latest `origin/automain`, for
example:

```text
codex/pipeline-operability-repair
```

Each implementation agent works in an isolated worktree/branch, makes focused
commits, and hands commit SHAs to the integration owner. Do not let agents edit
the same files concurrently.

### Workstream A — operation contracts and helper transport

Owns:

- backend Strategic contract registry/models/export;
- generated agent manifest;
- generated help/schema;
- `--json`/`--file`/`--stdin`;
- direct `update-direction` normalization;
- problem-model patch/replace operations and compatibility adapters.

Primary files:

- `backend/app/routers/admin_agent_ops.py`;
- new backend Strategic contract module(s);
- `backend/app/problem_model.py`;
- `backend/app/strategic_ops_manifest.json`;
- `agent/src/handlers/strategic-ops-registry.ts`;
- `agent/src/handlers/strategic-helpers.ts`;
- Strategic prompt/executor call sites.

Must land before Workstream B modifies the public `spawn_task` contract.

### Workstream B — unified spawning and durable admission

Owns:

- capability dispatch policy;
- shared spawn specification normalizer and task builder;
- `StrategicSpawnRequest` model/migration/service;
- idempotency;
- abuse deferral and FIFO draining;
- default watch creation for dispatched Strategic tasks;
- no second compatibility dispatch lane;
- compact request visibility/cancellation.

Primary files:

- `backend/app/task_capabilities.py`;
- `backend/app/routers/admin_agent_ops.py` after A’s contract foundation lands;
- `backend/app/runtime_config_state.py`;
- `backend/app/strategic_spawning.py`;
- models/schemas/router/read-model files;
- Strategic prompts/tests that advertise spawning.

Agent B should start with files not owned by A, then rebase after A before
editing `admin_agent_ops.py`.

### Workstream C — account usage reliability

Owns:

- Anthropic probe tolerance and error classification;
- pre-probe secret rehydration;
- durable attempt state;
- API diagnostics/freshness;
- Settings and pool-health rendering;
- Codex machine-readable compatibility spike and guarded adapter if supported.

Primary files:

- `backend/app/providers/anthropic.py`;
- `backend/app/providers/openai.py`;
- `backend/app/max_plan_pool.py`;
- `backend/app/routers/admin_pool_accounts.py`;
- `backend/app/routers/admin_settings.py`;
- account Settings/pool-health frontend components.

This workstream must not change admission-gate behavior.

### Workstream D — compute core and monetary policy

Owns:

- shared `ComputeSpendDecision`;
- all authoring and defense-in-depth call sites;
- canonical terminal-log normalization;
- task/job outcome reconciliation;
- compute contract preview and stable failure classifications;
- same-environment bounded preflight integration where supported;
- execution manifest/runtime provenance;
- actual hardware/placement capture.

Primary files:

- `backend/app/compute_experiment_contract.py`;
- `backend/app/compute_jobs.py`;
- `backend/app/compute_backends.py`;
- `backend/app/routers/runner.py`;
- `backend/app/admin.py`;
- Strategic/research/plan/agent compute creation paths;
- compute models/schemas/admin router.

### Workstream E — compute documents and compute UI

Starts after D’s schemas are stable. Owns:

- idempotent terminal execution-document service;
- `source_compute_job_id`;
- document composition;
- authored/effective/raw source presentation;
- typed terminal-log consumption;
- task/job split-state presentation;
- requested versus executed hardware labels;
- task-detail and Strategic-evidence surfacing.

Primary files:

- `backend/app/knowledge_completion.py`;
- `backend/app/task_completion.py`;
- terminal compute transition call sites coordinated with D;
- `admin-ui/src/components/ComputeJobsPanel.tsx`;
- `admin-ui/src/components/TaskComputeJobsSection.tsx`;
- knowledge/Strategic evidence components.

Do not rebuild the existing terminal-log expander.

### Workstream F — Strategic idle/status polish

Owns:

- two-hour default;
- pre-stop warning;
- bounded durable activity object;
- `StrategicWatch` resolution, heartbeat receipts, prompt injection, and
  acknowledgement operations;
- clean status-line rendering;
- minimal batching/deferred telemetry.

Primary files:

- `backend/app/strategic_resident_session.py`;
- `backend/app/runtime_config_state.py`;
- `backend/app/routers/admin_settings.py`;
- `backend/app/strategic_state_read_model.py`;
- `agent/src/handlers/strategic-resident.ts`;
- `agent/src/handlers/strategic-resident-lib.ts`;
- Strategic live-state frontend components.

This workstream must not alter reply receipt semantics or introduce mid-reply
interruption.

### Integration owner

The primary agent owns:

- integration branch/worktree;
- migration revision allocation and schema-head reconciliation;
- resolving the intentional A/B and D/E seams;
- cross-workstream tests;
- live smoke coordination;
- final independent review;
- PR creation.

## 7. Database migration strategy

Prefer one new migration for the PR, allocated from the live Alembic head at
integration time. Do not assume `0708`; another task may claim it.

Expected schema changes:

- create `strategic_spawn_request`;
- create `strategic_watch`;
- add `ComputeJob.execution_manifest` JSON/JSONB nullable;
- add `ComputeJob.runtime_provenance` JSON/JSONB nullable;
- add nullable `KnowledgeDocument.source_compute_job_id`;
- foreign key and unique/index constraints described above.

After adding the migration:

- bump `EXPECTED_SCHEMA_REVISION` in `backend/app/main.py`;
- add migration invariant/schema tests;
- preserve all existing rows with null-compatible readers;
- do not backfill wrappers or historical documents in this PR.

## 8. Implementation order

### Phase 0 — branch and baselines

1. Fetch and create the integration branch from latest `origin/automain`.
2. Record existing targeted-test baselines before changes.
3. Allocate file ownership and isolated worktrees.
4. Do not start a second migration until the integration owner assigns the
   revision.

### Phase 1 — shared contracts and fastest operator relief

Parallel:

- A: operation contract foundation and helper transport;
- C: Anthropic diagnostics/rehydration and live-verification support;
- D: centralized monetary decision and runner log normalization;
- F: idle-default/status work that does not overlap Settings files until C
  commits.

Acceptance:

- helper help/schema is generated;
- generic file/stdin JSON works;
- guaranteed-local `$15+` experiment bypasses monetary approval;
- compute preview shows the normalized contract, placement, cost decision, and
  capability requirements before dispatch;
- runner terminal log survives cancellation/terminal races;
- Anthropic failure becomes visible and classified.

### Phase 2 — durable models

1. Integration owner creates the single migration/model foundation.
2. B adds durable spawn requests and unified capability dispatch.
3. D adds execution manifest/runtime provenance producers.
4. E adds terminal compute-document identity and service.

Acceptance:

- retrying a spawn request creates exactly one request/task;
- each dispatched Strategic child receives exactly one default terminal watch;
- an abuse-deferred request survives and drains exactly once;
- every new compute job has authored/effective execution provenance;
- every terminal compute job produces at most one execution document.

### Phase 3 — presentation and compatibility

- remove `spawn-fix` from advertised CLI/prompt surfaces;
- retain its compatibility alias;
- add patch/replace problem-model operations;
- update compute/task/knowledge views;
- add usage known/stale/error/unsupported states;
- add compact Strategic activity line.

### Phase 4 — integration hardening

- run all targeted backend suites;
- run agent typecheck and unit suite;
- run admin UI typecheck, ESLint, and unit suite;
- run standalone backend smoke;
- perform live smokes;
- independent review focused on concurrency, auth secrecy, migration safety,
  state reconciliation, and accidental cloud spend;
- fix findings before opening the single PR.

## 9. Test plan

### 9.1 Strategic contract/helper tests

- every backend operation has exactly one contract;
- every public contract has alias, summary, schema, and valid example;
- generated manifest matches backend export;
- unknown fields fail with structured field paths;
- `--help`, `help`, and `schema` work without network;
- `--file` and `--stdin` preserve multiline/nested JSON;
- generated scripts pass `bash -n`;
- canonical and legacy `update_direction` forms normalize identically;
- mixed forms fail;
- patch preserves omitted fields and requires explicit deletion;
- stale base revision conflicts without mutation;
- replace requires revision, confirmation, and supports dry-run;
- transient actions never persist inside the problem model.

Primary suites:

- `backend/testing/test_strategic_ops_registry.py`;
- `backend/testing/test_admin_routers__problem_model.py`;
- `backend/testing/test_problem_model.py`;
- `agent/src/handlers/strategic-helpers.test.ts`;
- `agent/src/handlers/strategic-ops-registry.test.ts`;
- relevant Strategic executor/prompt tests.

### 9.2 Spawn tests

- all direct types use `spawn_task`;
- `fix` requires valid PR/branch context;
- direction-free code tasks remain valid;
- direction-linked tasks retain evidence/criterion wiring;
- same idempotency key under serial and concurrent retries produces one request
  and one task;
- distinct keys with similar titles remain distinct;
- normal work has no per-review cutoff;
- burst requests persist as deferred and auto-drain FIFO exactly once;
- cancelling deferred work prevents task creation;
- a dispatched Strategic child gets one terminal watch by default;
- idempotent spawn replay does not create a second watch;
- `watch=false` suppresses the default watch;
- permanent validation failures are not retryable;
- `spawn_fix` is absent from the operation registry and never creates work;
- only explicit transient failures are retried with the same key.

Primary suites:

- `backend/testing/test_strategic_spawning.py`;
- targeted router/task-capability tests;
- generated helper and thinker prompt tests.

### 9.3 Usage tests

- Anthropic OAuth: five-hour-only, optional seven-day, auth failure, timeout,
  endpoint failure, schema mismatch;
- Anthropic API key: unified headers present and absent;
- idle Secret-Manager-backed account is rehydrated outside the pool lock;
- failure attempt persists while last-good data stays stale;
- cold/non-leader API sees the leader’s last attempt;
- five-minute leader cadence remains independent of admission;
- no duplicate concurrent probes;
- UI renders known, stale, secret unavailable, endpoint error, schema mismatch,
  and unsupported;
- Codex adapter contract tests use captured/redacted machine-readable fixtures
  only if the compatibility spike succeeds.

Primary suites:

- `backend/testing/test_max_plan_pool_usage.py`;
- `backend/testing/test_max_plan_pool.py`;
- `backend/testing/test_admin_pool_accounts.py`;
- account Settings and pool-health frontend tests.

### 9.4 Monetary approval tests

Exercise direct create, rerun, Strategic, plan, research, agent-child, and final
job creation:

- local-required high-cap task launches without monetary approval;
- hard/strict pin behaves the same;
- unavailable required runner holds and never creates GCP work;
- soft preference remains approval-gated above threshold;
- cloud-capable equivalent still requires approval;
- project spend ceiling sees `$0` billable exposure for guaranteed-local work;
- preflight/UI says “$0 billable compute; waits for local runner.”
- contract preview and final creation normalize to the same digest, placement
  class, and monetary decision.

Primary suites:

- `backend/testing/test_compute__experiments.py`;
- `backend/testing/test_compute_gpu.py`;
- `backend/testing/test_compute_runner_dispatch.py`;
- compute modal/preflight frontend tests.

### 9.5 Compute evidence and lifecycle tests

- runner log survives success, failure, timeout, cancel, shutdown, and
  task-cancellation race;
- preview reports normalized source, expected metric/artifact contract,
  placement, capability requirements, and stable validation classifications;
- preview uses the same normalizer as job creation and cannot approve a contract
  that creation rejects for structural reasons;
- supported same-environment preflight captures bounded logs and stable failure
  fields; unsupported preflight is explicit rather than silently skipped;
- typed terminal log serializes identically across runner/local/GCP backends;
- logs are redacted and bounded;
- new jobs return authored source without decoding the wrapper;
- legacy known wrapper decodes with a legacy label;
- unknown legacy wrapper remains raw;
- actual runner hardware is distinct from requested cloud machine type;
- non-zero workload exit produces one document;
- invalid/no measurement produces one document;
- infrastructure failure remains task failure;
- cancelled task plus succeeded job preserves both states and one document;
- repeated terminal handlers remain idempotent;
- strict measurement flag gates dependents without deleting the report.

Primary suites:

- `backend/testing/test_local_runner_terminal_log.py` or equivalent new file;
- `backend/testing/test_admin_compute_jobs.py`;
- `backend/testing/test_compute_experiment_completion.py`;
- `backend/testing/test_compute_park_wake.py`;
- `backend/testing/test_knowledge_completion.py`;
- `backend/testing/test_compute_runner_dispatch.py`;
- `admin-ui/src/components/ComputeJobsPanel.test.tsx`;
- `admin-ui/src/components/TaskComputeJobsSection.test.tsx`;
- knowledge-browser tests.

### 9.6 Strategic lifecycle tests

- default unattended cap is 7,200 seconds;
- operator interaction re-arms the cap;
- idle cap ends through the clean handoff path;
- pre-stop warning is emitted once;
- 20-message burst remains one operator turn with no loss;
- autonomous work remains preemptible;
- message during operator reply is handled next, not dropped;
- `watch-task` on a live task remains pending and fires once at terminal;
- registering an already-terminal task fires immediately;
- default spawn watch survives Resident restart/session turnover;
- fired watch stays in autonomous prompts until `ack-watch`;
- acknowledgement is idempotent and records disposition;
- expired watch yields one visible receipt rather than disappearing;
- compact activity renders working/waiting/retrying/stopping/ended;
- ended status stops ticking.

Primary suites:

- `backend/testing/test_strategic_resident_session.py`;
- `backend/testing/test_workday_resident_invariants.py`;
- `agent/src/handlers/strategic-resident.test.ts`;
- `agent/src/handlers/strategic-resident-lib.test.ts`;
- Strategic live-state frontend tests.

### 9.7 Required commands

Run targeted backend files through:

```text
bash scripts/test-backend.sh testing/<targeted files...>
```

Never run the bare full backend suite on an agent VM.

After integration:

```text
bash scripts/test-backend.sh --standalone --no-postgres
cd agent && npx tsc --noEmit
cd agent && npx vitest run
cd admin-ui && npm run typecheck
cd admin-ui && npx eslint .
cd admin-ui && npx vitest run
```

The integration owner may split JS unit runs by relevant test files during
development, but the complete agent/admin unit suites run before PR publication.

## 10. Live smoke plan

### Usage

Anthropic:

1. Identify one valid dedicated OAuth account.
2. Observe leader progress across a probe cadence.
3. Record only HTTP class and response field names.
4. Verify persisted attempt/last-good state.
5. Read it from the normal admin API.
6. Confirm the UI shows remaining usage/reset or the exact classified failure.
7. Verify normal pool routing still works afterward.

Anthropic API-key probing performs a real provider request and requires explicit
operator approval before the smoke.

Codex:

1. Validate the deployed CLI/app-server compatibility contract in an isolated
   account home.
2. If supported, verify one redacted probe and normal account routing.
3. If unsupported, verify the UI clearly reports unsupported and does not retry
   aggressively.

### Local compute

1. Submit a guaranteed-local compute experiment above the monetary threshold.
2. Inspect the pre-dispatch receipt and confirm normalized source, local-only
   placement, `$0` billable decision, and runner capability requirements.
3. Confirm no manual monetary approval is requested.
4. Confirm runner-subprocess placement, `$0` billable cost, log visibility,
   actual hardware, and execution document.
5. Make the runner unavailable and confirm the task holds without cloud work.
6. Submit the same payload in a cloud-capable project and confirm approval is
   still required.

### Compute failure/document

1. Run a bounded workload that exits non-zero after printing a diagnostic.
2. Verify task/job split state, terminal log, and execution document.
3. Run a valid negative measurement and verify it is documented as negative
   evidence.
4. Exercise cancel-after-job-success and verify the successful job remains
   visible under a cancelled task.

### Strategic

1. Submit multiple messages before a turn and verify one combined response.
2. Submit a message during autonomous work and verify prompt preemption.
3. Spawn a bounded child task and verify one automatic pending watch.
4. Complete the child and verify the Resident wakes, receives the exact fired
   receipt, reports/acts on it, and acknowledges it.
5. Restart the Resident between registration and completion and verify the
   watch survives.
6. Verify the compact activity line and idle remaining time.
7. Use a shortened test-only idle setting to verify warning and graceful end.

## 11. Rollout and compatibility

All new readers tolerate absent fields. Rollout order:

1. schema and backend readers;
2. backend writers/services;
3. agent helper/prompt changes;
4. UI consumers;
5. compatibility warnings;
6. later cleanup after telemetry.

Compatibility retained for one release:

- raw positional Strategic JSON;
- nested `update_direction` wrapper;
- `set_problem_model` alias;
- legacy `polling_metadata.command_log`;
- exact legacy generated-command decoder;
- existing provenance tags `strategic_direct` and `strategic_direct_fix`.

Do not delete the remaining aliases in this PR unless repository deployment
guarantees all running agents are on the new image before the backend changes
serve traffic. `spawn_fix` is already retired and removed.

## 12. Observability

Add only operationally useful signals:

- spawn request state counts and deferral reason;
- idempotent replay count;
- usage last attempt/success/outcome/source;
- usage fleet reliability already present but now rendered;
- compute requested versus actual placement;
- compute preview outcome, validation code, and preview/execution digest match;
- preflight support/outcome/classification;
- terminal-log availability/truncation/source;
- execution-document creation/idempotent replay/failure;
- task/job state divergence;
- Strategic activity phase, idle remaining, batch size, and deferred-message
  pickup lag;
- Strategic watches pending/fired/acknowledged/expired and fire-to-ack latency.

Never log credentials, provider response bodies, authored secret-bearing source,
or unredacted command output.

## 13. Independent review checklist

Before publication, assign a reviewer who did not implement the relevant slice.
Review:

- no duplicate task creation under concurrent retries;
- deferred requests cannot dispatch twice;
- deferred work cannot bypass global autonomy, concurrency, or cost safety;
- default spawn watches are exactly-once and cannot create an autonomous
  busy-loop while awaiting acknowledgement;
- watch receipts cannot expose cross-project task data;
- local-only classification cannot be forged by an unvalidated soft preference;
- cloud fallback always re-enters monetary approval;
- usage probing cannot race or invalidate the active OAuth refresh lineage;
- secrets are rehydrated outside locks and never serialized to status;
- compute documents/logs are bounded and redacted;
- task cancellation cannot erase job evidence;
- migration is additive and compatible;
- helper schemas and backend validation cannot drift;
- compute preview and actual creation cannot drift;
- replacement mutations cannot occur without revision/confirmation;
- Strategic reply receipts and message ownership remain unchanged;
- no `.github/workflows/` or existing migration was modified.

## 14. PR completion gate

The single PR is ready only when:

- all workstream acceptance criteria pass;
- the migration and expected schema revision agree;
- all targeted backend tests pass;
- standalone backend smoke passes;
- agent typecheck and unit suite pass;
- admin UI typecheck, ESLint, and unit suite pass;
- Anthropic usage is either visibly working end to end or visibly classified
  with a verified provider limitation;
- Codex usage is working only if a stable supported interface exists, otherwise
  visibly unsupported;
- guaranteed-local high-cap compute needs no monetary approval and cannot fall
  back to cloud;
- a failing workload yields readable logs and exactly one useful document;
- compute preview clearly states the normalized run, placement, monetary
  decision, capabilities, and any preflight limitation before dispatch;
- spawn retries are idempotent and a synthetic abuse burst defers rather than
  loses work;
- a Strategic-spawned task completion produces one durable fired watch that
  remains actionable until acknowledged, including across Resident restart;
- two-hour idle Strategic shutdown follows the graceful handoff;
- independent review has no unresolved P0/P1 findings;
- the final diff contains no unrelated workspace material.

## 15. Remaining questions

No product question blocks implementation.

Two implementation findings may change the size of a slice but not the intended
behavior:

1. Whether the deployed Codex runtime exposes a stable machine-readable usage
   contract. Unsupported is an acceptable, visible result.
2. Which concrete Anthropic failure occurs in the live environment: credential
   hydration, endpoint/auth, five-hour-only schema, cadence, or persistence.
   The implementation must classify all of them and the smoke identifies the
   active one.
