# Engineering case study: coordinating AI-generated repository work

Blueprint explores how to turn agent execution into a process an operator can inspect and control. A model invocation is one step. The surrounding system must decide what may run, recover interrupted work, interpret artifacts, and determine whether a particular revision is ready to merge or serve.

The implementation combines a FastAPI/PostgreSQL control plane, a TypeScript runtime, a React workspace, self-hosted runners, and GCP infrastructure. This is a personal project developed with substantial AI assistance.

## 1. State must survive the executor

**Problem.** An executor may disconnect while editing a repository or reporting completion. Multiple processes can also observe the same task: scheduler, status endpoint, completion handler, and recovery worker. An in-memory process status cannot coordinate them safely.

**Design.** Task and project state live in PostgreSQL. Workers claim work and report progress. Lock helpers reload mutable state under a row lock; admission checks incorporate project pause, archive, and autonomy settings. Recovery loops inspect persisted state, missing heartbeats, and pending completion work.

[`task_locking.py`](../../backend/app/task_locking.py) is a useful compact tour of mutation authority. [`services.py`](../../backend/app/services.py) illustrates bounded recovery sweeps and per-call timeouts, including the rationale recorded after a leader-loop saturation incident. [`test_task_locking.py`](../../backend/testing/test_task_locking.py) exercises contention and concurrent mutations.

**Tradeoff.** Persistence and explicit locks make races inspectable, but they add coordination and contention. External network I/O needs a bounded lifetime and should avoid holding database locks unnecessarily. Tests that use only SQLite cannot establish PostgreSQL locking behavior.

**Discussion point.** Walk through a worker disappearing immediately before or after completion reporting. Explain which state is durable, which actor can retry, and how a duplicate observation is prevented from becoming duplicate authority. Claims about exactly-once external effects require evidence for the specific path; they do not follow from a transaction alone.

## 2. Completion and permission to merge are distinct

**Problem.** A task can report success while its PR fails CI, a review targets an earlier head, or a fix is still active. Approving one revision cannot authorize every future revision of the PR.

**Design.** The review pipeline builds a blocker ledger and tracks revision identity. [`review_merge_authority.py`](../../backend/app/review_merge_authority.py) validates the checked base/head, rebuilds the relevant blockers, and either grants merge authority or requests the review needed to obtain it. [`review_target_drift.py`](../../backend/app/review_target_drift.py) covers target changes.

**Tradeoff.** Revision-specific authority is more reliable than one Boolean approval flag, but it creates more persisted state and reconciliation paths. The implementation's review and release coordination files grew large; extracting cohesive authority decisions is useful, while merely spreading the same state across more files can make it harder to reason about.

**Discussion point.** Explain what should happen when a new commit arrives after green CI and approval. Identify the evidence that remains applicable and what needs to be checked again.

## 3. Deployment needs observed evidence

**Problem.** A merge, promotion, or accepted deployment request does not prove that the intended software is serving. Provider calls can take time while configuration or release authority changes concurrently.

**Design.** Releases persist qualification, promotion, deployment artifacts, and serving state separately. In [`release_serving_observation.py`](../../backend/app/release_serving_observation.py), the system snapshots the relevant authority, observes external providers without holding authority row locks, then applies the result in a short transaction only if the baseline still matches.

[`test_release_serving_observation.py`](../../backend/testing/test_release_serving_observation.py) checks that provider calls run without authority row locks, stale authority is rejected, and the application phase does not replay the provider check.

**Tradeoff.** Observations can become stale and require another pass. This is preferable to applying evidence to a different release, and avoids coupling database lock duration to provider latency. Observed artifact identity establishes the configured serving criterion; it does not establish every application behavior.

**Discussion point.** Trace a successful deployment request followed by an unexpected serving identifier. Explain why the release can remain unconfirmed and where the operator sees that distinction.

## 4. Capability contracts reduce permission drift

**Problem.** Backend task metadata and agent permission classifications can drift when task types evolve. A missing classification must not silently grant write access.

**Design.** The backend owns the task capability registry. The TypeScript runtime maintains handler coverage but loads and validates the permission partition from the backend. [`taskCapabilities.ts`](../../agent/src/taskCapabilities.ts) rejects a missing or malformed contract and refuses permission classification before loading it.

**Tradeoff.** Startup depends on a complete contract. That explicit failure is useful because a partial table is an ambiguous permission boundary. It also requires compatibility testing when introducing a new task type.

## Running the application

The [local UI guide](DEMO.md) starts the existing operator workspace. It uses the real backend and authentication flow. See the [verification record](VERIFICATION.md) for the checks run during cleanup and the remaining setup requirements.

## Engineering limitations and next priorities

The boundaries above demonstrate useful systems principles: durable state, explicit mutation authority, conservative permission defaults, and evidence tied to the revision being acted on. Maintainability is uneven. [`release_qualifier.py`](../../backend/app/release_qualifier.py) exceeds 10,000 lines, [`models.py`](../../backend/app/models.py) exceeds 8,000, and [`MessagesPanel.tsx`](../../admin-ui/src/components/MessagesPanel.tsx) exceeds 3,000. Release coordination, persistence, and UI orchestration need smaller cohesive interfaces before substantial further expansion.

The next engineering work should focus on three things:

1. Extract lifecycle and release decisions by responsibility, with behavior tests around each boundary. A file split is useful only when it reduces the state a reader must understand together.
2. Reduce overlapping persisted compatibility flags and identify the canonical source for each decision. Persisted-state changes require explicit migration and recovery behavior.
3. Establish a reproducible authenticated path from task submission through execution and result interpretation. The cleanup verified targeted backend behavior and the unit suites; it did not verify a complete configured workflow.

The [July operator walkthrough](../walkthroughs/v26-operator-walkthrough.md) records successful and unsuccessful flows on an earlier revision. Current test counts describe coverage executed during cleanup. Neither establishes current end-to-end reliability or agent effectiveness; those claims need identified executions and measured outcomes.
