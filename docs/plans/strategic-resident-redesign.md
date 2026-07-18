# Strategic Resident Redesign

**Target:** `automain`

**Status:** Milestones 1, 2a, 2b, 3, and 4 merged; Milestone 5 (rollout, legacy
retirement, release audit) remains — its exit criteria (seven-day soak, live
per-project production engine flip, legacy-handler retirement after soak) are
operator- and calendar-gated by design.

**Last refreshed:** 2026-07-17. M2a merged through PR `#15292` at merge commit
`7b9b8c80b`. M2b merged through PR `#15486` at merge commit `3d3689e65` after
migration, locking, latency, provider-boundary, and affected-suite review/fix
loops converged. M3 adds typed operations and revision-safe human-tool handoff;
its capability-boundedness inspection contract converged and it was rebased onto
`origin/automain` `d3031e3c4` (ArtifactRef migration re-chained to `0630`).

This plan tracks the migration from disposable Resident sessions to one durable,
project-owned Strategic Resident. It is intentionally separate from the historical
Chat/Workday session plans.

## Product outcome

- One Strategic Resident owns project strategy and operational outcomes.
- Bounded Tasks perform context-heavy work without becoming the authority.
- Conversation stays calm and meaningful; routine operational events stay in the
  project activity inbox.
- Every mutation is generation-fenced, revision-aware, idempotent, and receipted.
- Human-authored tools remain human-owned unless an operator explicitly hands over
  an exact revision.
- Migration and rollback happen per project without losing conversation,
  checkpoints, decisions, or unknown external effects.

## Design constraints

- The Resident inspects decisive evidence before strategic or high-impact actions.
- Workers may propose strategic memory but cannot directly change the charter or
  working strategy.
- Raw interactive and background histories remain separate and bounded.
- Rapid operator messages coalesce; interactive work preempts background work at
  safe boundaries.
- Unchanged cadence checks make zero model calls.
- Warm provider processes are optional latency optimizations and are valid only for
  the exact checkpoint lineage, provider, model, and instruction set.
- Code editing, large review, open-ended debugging, and repeated CI repair remain
  managed Tasks.
- Routine pipeline activity never enters conversation unless the Resident produces
  a deliberate summary or outcome.

## Milestones

### M1 — Durable conversation foundation — merged

- Durable actor attribution, monotonic sequence, conversation/activity surface,
  handling state, reply linkage, and canonical timestamps.
- Cursor pagination, targeted surrounding pages, reconnect gap filling, stable
  ordering, and idempotent message lookup.
- Atomic durable acceptance plus Resident wakeup/coalescing state.
- Reliable composer states and lost-response recovery without draft loss or
  content-based deduplication.
- Visible, revisioned source chips delivered to bounded Resident context.
- Conversation/activity separation and long-history virtualization.
- Migration `0608_projectmessage_resident_foundation`.

Merged by PR `#15186`, commit `516a94a2f`.

### M2a — Resident control plane and shadow turns — merged

- Add one `StrategicResident` per project with engine ownership, background policy,
  monotonic authority generation, active/successor turn pointers, checkpoint
  pointers, cursors, policies, health, recovery, scheduling, and rollout state.
- Add immutable, versioned, hashed Resident checkpoints and deterministic legacy
  backfill.
- Add durable operation receipts and database-only mutation authority fencing.
- Add bounded `strategic_resident_turn` Tasks with one global active turn and at
  most one coalesced successor per interactive/background lane.
- Run shadow turns with zero model calls while the legacy Resident remains
  authoritative. Store bounded reconstruction manifests, hashes, comparison data,
  and latency only.
- Exclude shadow turns from normal runner dispatch and complete/promote them through
  a dedicated sweep.
- Migration `0611`; restartable backfill and cross-project pointer validation.

Exit gates:

- Focused migration, coalescing, sweep, authority, idempotency, and concurrency
  tests pass.
- Existing conversation, notification, strategic-session, orchestrator, and reaper
  behavior remains green.
- Subagent review/fix iterations converge with no unresolved correctness,
  migration, latency, or ergonomics findings.
- Branch is refreshed from current `origin/automain` before publication.

Current convergence checklist:

- [x] Durable Resident, checkpoint, receipt, cursor, policy, health, scheduling,
  and rollout-state models exist with deterministic legacy projection.
- [x] Runtime and migration legacy projections preserve bounded session summary,
  parsed decisions, and opaque message cursor while excluding examination/session
  history; focused migration/checkpoint tests are green (`14 passed`, one
  PostgreSQL-only smoke test skipped locally).
- [x] Migration-chain hardening recognizes Alembic autocommit blocks and its real
  scan plus seeded positive/negative detector tests are green.
- [x] Shadow turns are backend-managed, runner-ineligible, bounded, and perform
  zero provider/model calls.
- [x] Project conversation messages acquire a weak Project lock before the
  Resident, keeping the acceptance path compatible with Task foreign-key locking
  without an exclusive hot-project lock.
- [x] Finish the authority module split and require frozen active-Task
  generation/checkpoint lineage plus authority/task/checkpoint version and hash
  expectations before any mutation callback can apply a write. Mirror the
  baseline snapshot in PostgreSQL and SQLite receipt triggers.
- [x] Enforce paused background policy at enqueue, promotion, and sweep boundaries
  while preserving interactive wakeups; cover pause transitions with active and
  successor background work.
- [x] Shadow both Phase-1 architecture/design-document starts and Phase-2
  strategy/review transitions.
- [x] Split turn scheduling/coalescing from manifest/completion/sweep and split
  authority execution/validation/result bounding behind stable facades; all new
  modules have tight explicit size caps (the safety-heavy shadow projector is
  capped at 720 lines and the remaining modules below 700).
- [x] Count requested source chips before message/range truncation and preserve
  exact displayed chip fields or durable truncation/hash metadata in comparison
  output.
- [x] Minimize the message sequence-counter critical section and prove concurrent
  send/background-spawn lock ordering on PostgreSQL.
- [x] Standardize every message writer on Project-before-sequence ordering,
  including generic event/check-in/reply paths whose inserts otherwise take an
  implicit Project foreign-key lock; add PostgreSQL steering-send/generic-event
  and explicit resolver/agent lock-order regressions.
- [x] Pre-lock the Resident, checkpoint, and existing turn-pointer Tasks under a
  bounded wait before reserving the conversation sequence, so Resident
  contention cannot convoy unrelated message writers.
- [x] Require factory-owned scan/worker sessions for shadow sweeps and prove one
  project failure cannot roll back caller or sibling work.
- [x] Add direct message-hook, reaper-isolation, orchestrator-final-claim, and
  PostgreSQL concurrency/migration regressions.
- [x] Make the pre-rebase focused and affected suites green, including the full
  strategic spawning suite and the combined parent M2a gate.
- [x] Complete an independent pre-rebase review/fix iteration with no unresolved
  correctness, migration, latency, or ergonomics findings.
- [x] Repeat affected suites after rebasing onto the latest `origin/automain`.
- [x] Complete an independent post-rebase review/fix iteration with no actionable
  correctness, migration, latency, or ergonomics findings.

Merged by PR `#15292`, commit `7b9b8c80b`.

### M2b — Mutation-authorized bounded Resident turns — merged

- Reconstruct turns from stable instructions, charter, working strategy, active
  mission, recent conversation, and explicitly retrieved evidence.
- Use structured turn results: reply, facts, evidence, operations, receipts,
  unknown effects, strategic implications, proposed memory, follow-up, and required
  attention.
- Fence every mutation inside its database transaction.
- Reconcile uncertain external effects before retrying.
- Preserve operator preemption and separate interactive/background raw context.
- Apply checkpoint and strategic-memory changes only through Resident
  reconciliation.
- The ownership-flip transaction must cancel/supersede all shadow active and
  successor pointers atomically before enabling authoritative turns; no queued
  shadow work may be stranded across cutover.

Current implementation checklist:

- [x] Add bounded typed turn results and adversarial result-size validation.
- [x] Reconstruct authoritative interactive/background turns from checkpoint,
  recent conversation, active mission, and explicit revisioned evidence.
- [x] Reconcile proposed memory through Resident-only immutable checkpoint CAS;
  require decisive evidence before strategy-changing or high-impact implications.
- [x] Apply direct typed mutations only through fenced operation receipts and
  reconcile unknown external effects before retry.
- [x] Atomically flip one project from legacy shadow ownership to authoritative
  Resident ownership while cancelling/superseding every shadow pointer.
- [x] Roll back by fencing the authoritative generation, reconciling unknown
  receipts, projecting the latest checkpoint into legacy inputs, and restoring
  legacy ownership.
- [x] Rebase onto `bbf1e51f7`, run focused affected suites, and complete
  iterative post-rebase review/fix convergence.
- [x] Refresh latest `origin/automain` through `ef2d993a8`; the four late commits
  are non-overlapping and the migration/size/diff smoke gates remain green.
- [x] Publish and merge M2b through PR `#15486`, commit `3d3689e65`.

Pre-rebase evidence:

- Claim, failure/reaper, runner/capacity, cutover/message, legacy cadence,
  evidence/receipt, and migration boundary review/fix loops converged.
- Terminal complete/fail/reaper paths durably claim Max-plan slot release before
  best-effort cache repair, preserve concurrent reservations, count legacy active
  identities with a scalar query, and remain replay-safe without an unbounded
  process cache.
- Resident provider routing now fails closed before generic environment fallback;
  the agent requires the server-selected provider and runs the provider child from
  an ephemeral non-repository home/config/cwd with only provider credentials and
  a complete no-tools boundary. Unsupported provider CLIs truthfully wait.
- Late boundary gates are green: authoritative orchestrator holds (`5 passed`),
  provider selector (`9 passed`), capacity (`14 passed`), claim (`10 passed`),
  slot release (`10 passed`), and single-worker agent security/dispatch tests
  (`32 passed`, `525 skipped`) with typecheck clean.
- The combined affected backend gate is green (`333 passed`, `13 skipped`);
  the focused TypeScript agent gate is green (`551 passed`) with typecheck clean.
- The forward-only cutover repair is revision `0627`, chained from refreshed
  `origin/automain`'s single `0626` head. Its direct upgrade-path test covers the
  exact cutover authority boundary after the upstream chain.

Post-rebase evidence:

- Alembic and the standalone migration parser report one `0627` head; the schema
  lock and direct upgrade/downgrade test are green (`2 passed`, one local
  PostgreSQL-only smoke test skipped).
- Message acceptance preserves canonical prefs -> Project -> Resident lock order,
  uses PostgreSQL `FOR NO KEY UPDATE`, refreshes stale ORM identities, recomputes
  legacy session routing after rollback, and never reads SAVEPOINT-expired owner
  state. Workbench persistence is flushed under its existing Project lock before
  owner refresh, keeping the Resident-lock latency window bounded.
- Serialized backend gates are green: capacity/claim/runtime/slot/runner/reaper
  (`58 passed`), ownership/reconciliation/realtime (`100 passed`, `2 skipped`),
  message/locking/latency (`51 passed`, `9 skipped`), and changed spawning cases
  (`6 passed`).
- Single-worker agent security/dispatch tests are green (`102 passed`) and
  TypeScript typecheck is clean. Backend/agent size caps, focused lint, and diff
  checks are clean without cap increases.
- Independent post-rebase review/fix iterations converged on migration parsing,
  upstream handler preservation, provider isolation, lock/FK compatibility,
  stale identity/session routing, workbench autoflush, and SAVEPOINT recovery.

### M3 — Typed operations and human-tool compatibility — in progress

- Project-scoped inspection and mutation capabilities with declared scope, risk,
  evidence, approval, idempotency, reconciliation, and receipt shapes.
- Revisioned `ArtifactRef` support for Notes and Whiteboard.
- Share/Ask Resident after autosave flush, source-linked derived work, explicit lane
  ownership, revision-safe writes, and recursive-wakeup prevention.

Current implementation checklist:

- [x] Declare 38 immutable project-scoped inspection/mutation contracts and expose
  a compact 4.1 KB provider catalog without service internals, credentials, shell,
  or verbose receipt metadata.
- [x] Add fail-closed invocation validation plus bounded, tenant-isolated exact
  Task, lineage, log, failure, and provider inspection.
- [x] Add migration `0628` for immutable Notes/Whiteboard `ArtifactRef` snapshots,
  rendered-image receipt ownership, and a durable project-cleanup outbox with
  lease-generation fencing and bounded retry reconciliation.
- [x] Validate revision/checksum/project ownership when a displayed tool chip is
  sent, and preserve that exact chip through bounded Resident reconstruction.
- [x] Give only the live task/VM/project/authority-bound Resident token access to
  bounded Notes text or a short-lived raster Whiteboard URL.
- [x] Union canonical ArtifactRef grants into new, successor, and recovery turns;
  retrieve only exact task-granted revisions and release authority locks before
  signed-URL generation.
- [x] Flush Notes/Whiteboard autosave before reference creation; preserve explicit
  Share/Ask intent and optional lane ownership through draft, remount, retry, and
  canonical conversation send paths.
- [x] Make composer drafts and tool handoffs immutable per write/event across tabs,
  consume one handoff intent per message, and bound Whiteboard points, nodes,
  embedded bytes, text, image concurrency, browser yields, and total render time.
- [x] Fail closed on human-tool writes and output-link mutations until they can run
  inside the canonical authority/receipt executor; no context-flag-only bypass
  ships in M3.
- [x] Keep the typed catalog latency-bounded and verify full provider input remains
  within its hard byte budget.
- [x] Complete independent backend/UI review-fix iterations until convergence.
- [x] Converge the capability-boundedness inspection contract: tighten evidence
  caps to 4.8 KB projected data / 5.5 KB complete response (documented 48 KB
  aggregate-per-turn = 8 requests x 5.5 KB), make lineage descent a
  deterministic bounded top-N with cycle-safe dedup, guarantee the requested
  root survives both count and byte truncation, and bind the lineage revision
  to every represented member. Mutation-proved bounds tests.
- [x] Refresh from current `origin/automain`, repeat affected gates, publish, and
  merge the M3 milestone.

Post-rebase evidence:

- Rebased onto `origin/automain` at `d3031e3c4`. automain landed migrations
  through `0629_ix_computejob_completed_at`, so the M3 ArtifactRef migration is
  re-chained as `0630_project_tool_artifact_refs` (down_revision `0629`) with
  `EXPECTED_SCHEMA_REVISION` bumped to `0630`; the migration-chain and
  divergence gates report one linear head. The `storage.py` import block and the
  `iam.tf` artifact bucket lifecycle rules were merge-resolved (artifact-refs/
  is retained by not matching any lifecycle rule).
- Focused affected suites are green under SQLite: capability/inspection,
  lineage, runtime/context, and ArtifactRef promotion (`72 passed`, one
  PostgreSQL-only concurrency skip), plus the new mutation-proved
  `test_strategic_resident_inspection_bounds.py` (`10 passed`). Migration-apply
  gate and schema invariants pass; agent TypeScript typecheck is clean.
- The independent boundedness review returned one gating determinism finding
  (a lineage fetch without `ORDER BY` yields non-deterministic truncated
  subsets, breaking revision/checksum stability) plus four minor findings; all
  were fixed and covered by mutation-proved tests before convergence.
- Module size caps bumped to canonical policy for M3 growth
  (`strategic_resident_context.py`, `agent_resident_turn.py`,
  `strategic-resident-turn.ts`); `admin_project_tools.py` cleared its
  chronic-offender gate via operator-approved `--force-chronic`. Scoped Ruff
  and `git diff --check` are clean.

Pre-rebase evidence:

- ArtifactRef promotion, crash reconciliation, cleanup, message-grant, and
  shutdown coverage passes (`33 passed`, one PostgreSQL-only concurrency skip);
  schema head/linear/monotonic gates pass (`4 passed`).
- Typed capability invocation and task inspection pass (`14 passed`); Agent
  capability-loop tests and typecheck remain green.
- Strategic runtime/context reconstruction passes (`12 passed`), including a
  24-message oversized unhandled range that preserves a contiguous cursor prefix
  and grants only the omitted tail's exact ArtifactRefs to its successor.
- Durable pending Whiteboard handoff, immutable composer/handoff storage, retry,
  remount, and rendering bounds pass (`41` focused pending-operation tests,
  `348` full MessagesPanel tests, and `38` related Resident-tool UI tests).
- Independent backend, UI, latency, migration, lifecycle, retention/IAM, and
  maintainability review/fix iterations converged with no actionable findings;
  scoped Ruff, TypeScript, ESLint, Terraform formatting, and diff checks are clean.

### M4 — Fenced operations, strategic memory, and cadence — merged

- Route authorized mutations through operation IDs, expected revisions, authority
  generations, evidence preconditions, approvals, durable receipts, and explicit
  unknown-effect reconciliation.
- Add direct bounded adapters for safe operations and managed-Task escalation for
  code editing, large review, open-ended debugging, and repeated CI repair.
- Reconstruct interactive/background turns independently from charter, working
  strategy, active mission, conversation cursors, and retrieved evidence.
- Admit worker memory proposals only through Resident reconciliation; preserve
  charter, decisions, rationales, commitments, and open loops across rotations.
- Coalesce meaningful wakeups, make unchanged checks zero-model-call, preempt
  background work safely, and reconcile revision-linked StrategicCycle proposals.

Implementation checklist:

- [x] Correct `INSPECTION_CAPABILITY_KINDS` to real capability ids so inspection
  evidence is trusted once receipts are written (PR `#15622`).
- [x] Lift the first fenced mutation off its 501 stub — `strategy.running_update.set`
  through the DB-only `execute_fenced_db_operation`, authority from the signed
  token, generation/CAS fenced (PR `#15630`).
- [x] Write server-authored, context-hash-bound, idempotent inspection receipts so
  mid-turn inspection evidence is citable; exclude them from the operation-claim
  invariants (PR `#15643`).
- [x] Extend the fence to a bounded target task — `task.reprioritize`, CAS on
  `updated_at`, fail-closed against targeting any Resident turn (PR `#15649`).
- [x] Add the two-phase external-effect executor (durable `applying` intent →
  single-owner claim → perform → settle `reconciled`/`failed`); wire `pr.create`
  behind a default-off operator allowlist so it ships inert (PR `#15654`).
- [x] Add the worker memory-proposal admission contract: revision-linked +
  evidence-gated + fail-closed, returning reconciliation-native proposals
  (PR `#15655`).
- [x] Add `signal_project_change`, the production entrypoint for the background
  `project_change` coalescing lane with the zero-model unchanged guarantee
  (PR `#15656`).
- Follow-on wires (bounded slices deferred, not blocking M4): critical
  external effects (`pr.merge`/`deployment.*`) stay 501 pending real approval
  verification; the worker-proposal storage/drain and the `signal_project_change`
  trigger call sites connect their producers in M5-adjacent work.

### M5 — Rollout, legacy retirement, and release audit

- Complete shadow comparisons and rollout health surfaces, then atomically flip
  per-project engine ownership at a checkpoint boundary.
- Rehearse rollback by fencing the new generation, reconciling unknown operations,
  projecting the latest checkpoint into legacy inputs, and restoring ownership.
- Retire session blobs, continuation markers, Chat/Workday state, and legacy
  handlers only after the soak and rollback evidence converge.
- Message-quality and adversarial authority suites.
- PR-to-deployment operational scenario while conversation and human tools continue.
- Failover, context rotation, 10,000-event noise, crash-boundary, prompt-injection,
  migration/rollback, and seven-day soak coverage.
- Requirement-by-requirement evidence audit before removing rollback projections.

## Latency and ergonomics budget

- Durable message acceptance is independent of model or runner availability.
- Duplicate retries return the canonical message without extra wakeups.
- Coalescing work stays inside the existing acceptance transaction and uses bounded
  indexed rows.
- Unchanged background checks perform no provider call.
- Active turn input never changes after claim; successors absorb new events.
- Context reconstruction uses cursors and revisioned references rather than raw
  operational history.
- Provider warmth is never correctness-critical.
- The operator sees accepted versus waiting versus claimed state truthfully.
- Code is separated into message creation, Resident scheduling, checkpoint,
  authority/receipt, conversation UI, and activity UI modules with narrow contracts.

## Publication discipline

Each milestone:

1. Integrates parallel work in an isolated worktree.
2. Runs focused gates, then affected broader suites.
3. Receives independent subagent reviews and fix iterations until convergence.
4. Refreshes from the latest `origin/automain` and repeats affected gates.
5. Publishes a narrowly scoped PR and merges only after final convergence.
