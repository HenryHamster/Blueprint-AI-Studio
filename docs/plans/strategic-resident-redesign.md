# Strategic Resident Redesign

**Target:** `automain`

**Status:** Milestones 1 and 2a merged; Milestone 2b post-rebase review converged,
publication pending.

**Last refreshed:** 2026-07-16. M2a merged through PR `#15292` at merge commit
`7b9b8c80b`. M2b is rebased onto `origin/automain` at `ef2d993a8`; migration,
locking, latency, provider-boundary, and affected-suite review/fix loops have
converged. The milestone is ready for publication.

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

### M2b — Mutation-authorized bounded Resident turns — in progress

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
- [ ] Publish and merge M2b.

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

### M3 — Typed operations and human-tool compatibility

- Project-scoped inspection and mutation capabilities with declared scope, risk,
  evidence, approval, idempotency, reconciliation, and receipt shapes.
- Revisioned `ArtifactRef` support for Notes and Whiteboard.
- Share/Ask Resident after autosave flush, source-linked derived work, explicit lane
  ownership, revision-safe writes, and recursive-wakeup prevention.

### M4 — Complete user experience, migration, and rollback

- Finish the split Resident conversation/composer/operation renderer/activity inbox
  architecture and retire Chat/Workday/session assumptions.
- Shadow comparison and rollout health surfaces.
- Atomic per-project engine ownership flip at a checkpoint boundary.
- Rollback by fencing the new generation, reconciling unknown operations, projecting
  the latest checkpoint into legacy inputs, and restoring legacy ownership.
- Retire legacy session blobs and handlers only after the soak.

### M5 — Release audit

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
