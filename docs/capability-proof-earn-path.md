# Capability-proof earn path

## Goal

An existing registry capability can earn `proof_status == "proven"` without a
new feature task advertising `provides_capability`. This is the proof tier used
by capability-gate auto-graduation; structural `backing_path` and
`named_test_present` evidence remain only `path_proven`.

## Flow

1. `commission_proof_earn_loop` intersects demanded, unproven working registry
   capabilities with their `backing_paths`.
2. It verifies every declared backing path at the project HEAD and discovers a
   convention-named test that imports each path.
3. With `capability_proof_execution_trials_enabled` (default on), it enrolls one
   bounded `capability_proof_trial` task containing at most ten discovered test
   files and the HEAD SHA. The reaper also creates synthetic demand for all
   unproven working capabilities when no dependent is blocked, avoiding a gate
   off-mode circularity.
4. The read-only agent handler clones the base branch and runs only those target
   tests. It reports whether the suite ran, passed, and verified at least one
   test; environment failures and skips cannot produce proof.
5. `_handle_capability_proof_trial_completion` accepts only a green result and
   calls `record_capability_named_test_execution_proof`. That recorder writes an
   executed `named_test` evidence reference and ledger entry, which classifiers
   grade as `proven`.

## Safety and lifecycle

The task has a wall-clock limit and file-count cap. A red, skipped, malformed,
or unavailable run records a task-local contradiction only and never demotes a
previous proof. A fresh execution proof is tied to its HEAD SHA; a changed HEAD
or expired proof re-enrolls verification. Operators may pause enrollment and
ingestion with the admin setting `capability_proof_execution_trials_enabled`.

## Relevant code

- `backend/app/capability_proof.py` — discovery, enrollment, and durable proof
  recording.
- `backend/app/reaper_sweeps.py` and `backend/app/autonomous_research.py` —
  scheduled and on-demand commissioning.
- `agent/src/handlers/capability-proof-trial.ts` — bounded targeted execution.
- `backend/app/task_completion.py` — green-result ingestion.
- `backend/app/metrics_integrity_sweep.py` — gate coverage, which counts only
  `proven` working capabilities.
