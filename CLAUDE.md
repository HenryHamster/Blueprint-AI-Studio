# Blueprint AI Studio

## File Restrictions
- Do NOT modify files in `.github/workflows/` — CI workflows are managed separately and the GitHub App token cannot push workflow changes
- Do NOT modify **existing** migration files in `backend/alembic/versions/` — applied history is immutable. Adding a new migration file (next revision, chained from the latest `down_revision`) is fine and expected when `models.py` changes; also bump `EXPECTED_SCHEMA_REVISION` in `backend/app/main.py` to match.

## Code Style
- Frontend: React + TypeScript, Tailwind CSS, functional components
- Backend: Python, FastAPI, SQLAlchemy/SQLModel, async/await
- Agent: TypeScript, Node.js

## Testing
- Backend, targeted — **the default for task work**: `bash scripts/test-backend.sh testing/test_<module>.py [more files]` — run the test files covering what you changed.
- Backend, standalone smoke (no DB): `bash scripts/test-backend.sh --standalone --no-postgres` — invariant/security/pipeline files; this is what `./check test` runs.
- Backend, FULL suite: `bash scripts/test-backend.sh` with no test-file arguments — **never run this on an agent VM.** It builds the Docker image and runs the entire DB-backed suite: 45–60+ min on a 2-vCPU/8 GB VM (OOM-prone), and redundant — CI runs the same suite sharded once at the automain→main promotion PR (which targets `main`). The bare invocation exists for developer machines, not task VMs.
- Frontend (agent VM): `cd admin-ui && npm run typecheck && npx eslint .` — `npm run build` (vite bundle) OOMs on agent VMs; typecheck catches type errors; the full vite build runs in CI. `./check test` also runs `npx vitest run` (the ~1400-test happy-dom unit suite) when admin-ui files changed.
- Agent: `cd agent && npx tsc --noEmit`. `./check test` also runs `npx vitest run` (the agent unit suite) when agent/ files changed.
- JS/TS unit suites: `./check test` runs both `cd agent && npx vitest run` and `cd admin-ui && npx vitest run` for their respective stacks. These match the blocking CI jobs at the promotion PR, so regressions surface at the merge gate instead of weeks later.

## Release Qualification Boundaries
- The automain to main qualifier must never spawn, wait for, respawn, score, or ledger-write a VM `smoketest` child.
- Promotion PR CI is the hard deterministic test gate. CI red and unresolved/pending required CI remain blocking.
- Ordinary post-promotion/deployment `smoketest` tasks are a separate operational check and must remain available.
- Legacy `smoketest_task_id` and `smoketest_enabled` fields are read-model compatibility only; they never gate a current qualification.
- Promotion weights use `audit` and `test_erosion`. Legacy three-key policies are accepted by ignoring and renormalizing the retired `smoketest` weight.

## Test Guidelines
- When fixing test failures, first diagnose: is the failure in the CODE or the TEST?
- Do NOT delete or skip tests just to make them pass
- If a test is testing the wrong thing (stale API, renamed function), fix the test to correctly test current behavior
- Tests may be removed only when the tested functionality itself was removed
- Do NOT weaken assertions without clear justification

## Common Patterns
- `task.first_payload` is the standard accessor for the first payload element (not `(task.payloads or [{}])[0]`)
- Verify before finishing with TARGETED tests: run the backend test files covering your change (`bash scripts/test-backend.sh testing/test_<module>.py`), plus `cd admin-ui && npm run typecheck` if you touched frontend (use this on agent VMs — `npm run build` OOMs there; CI runs the full vite build). Do NOT run the full backend suite on an agent VM — your task PR gets targeted/per-change CI; the full sharded matrix runs once at the automain→main promotion PR.
- New admin endpoints need `@require_project_access` or `@require_role` decorators

## Generating Code for One Language From Another
When one language generates source code for a different language
(e.g. Python emitting a bash script, bash embedding a Python heredoc,
TypeScript emitting a shell helper), escape semantics compound across
every layer. `\n`, `\\`, and `\"` in the generator's source do NOT mean
the same thing in the generated output, and silent mis-nesting is one
of the hardest bug classes to diagnose — the generated output looks
almost-right to the eye while being invalid to the embedded language.

- **Avoid multi-line heredocs of another language inside a generator
  string.** Two layers of escape (generator → embedded) is already
  risky; three (generator → bash → embedded) is where production bugs
  live.
- **Preferred pattern:** the embedded script lives as a real file
  (`.py`, `.js`, etc.) in the repo — directly lintable, directly
  testable. The generator reads that file at module-load time,
  base64-encodes it, and emits an invocation like
  `echo '<blob>' | base64 -d > /tmp/foo.py; python3 /tmp/foo.py ...`.
  Base64 is ASCII-safe so no character in the embedded script needs
  to survive any escape pass.
- **Short one-liners are fine** (`python3 -c '...'`, `jq '...'`,
  `node -e '...'`) when the body is single-line and uses no backslash
  escapes — the risk is in multi-line + backslash combinations.
- When a test harness exists for generated shell (bash -n, shellcheck,
  pattern checks), register new generators there rather than assuming
  they'll be inspected manually.
