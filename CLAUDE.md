# Blueprint AI Studio

## Blueprint Access for Coding Agents
- These tools are for developer-hosted Codex and Claude sessions. Blueprint task workers are not provisioned with a reusable `bpsc_` credential; they must keep using their task-scoped runtime helpers and token.
- In a developer host with the Blueprint MCP configured, prefer its tools for operational context. Start with `get_project_snapshot`, then use `list_tasks`, `get_task`, or `list_messages` for bounded drill-down.
- Never read a repository `.blueprint-config.json`, copy an API key into a prompt, or assemble raw `curl`/heredoc calls when the MCP or operator CLI covers the request.
- The MCP surface is intentionally read-only. For a human-authorized mutation, use `./blueprint` on Unix or `.\blueprint.ps1` on Windows with an explicitly selected operator `BLUEPRINT_CONFIG` and `--project`/`--project-id`; never reuse `BLUEPRINT_AGENT_CONFIG` for a mutation. Preserve confirmation and idempotency options.
- See `docs/CODING_AGENT_ACCESS.md` for scoped credential setup and failure semantics.
- A trusted developer-hosted operator may instead be provisioned with the non-owner `project-operator` profile. Its CLI allowlist is limited to exact project task, direction, message, and Strategic actions; unsupported platform/bulk/host commands must fail closed. See `docs/HERMES_OPERATOR_ACCESS.md`. Blueprint task workers must not receive this reusable credential.

## File Restrictions
- Do NOT modify files in `.github/workflows/` — CI workflows are managed separately and the GitHub App token cannot push workflow changes
- `.github/workflows/` is an OPERATOR-ONLY surface: agents may not author workflow files. If a needed change belongs there, raise it as an operator request rather than working around it.
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

## Strategic Resident Boundaries
- **Protect the conversational Workday path.** The `/strategic-session` APIs, project-message acceptance and reply recovery, `StrategicSessionControl`, chat/Workday mode, runner selection, session continuity, and their persisted state are the supported operator conversation surface. Do not remove or replace them as part of Resident cleanup.
- **Protect tool-to-conversation handoff.** Notes and Whiteboard freeze immutable `ArtifactRef` revisions, queue them into the conversation composer, and deliver their exact bounded snapshot/render through the current authenticated Workday message poll. This is part of the supported conversational path, not shadow/control-plane Resident machinery; preserve it when removing retired Resident code.
- **Keep the shadow/control-plane Resident retired.** The separate `StrategicResident` ownership/checkpoint/receipt/rollout machinery and bounded `strategic_resident_turn` task type were an abandoned replacement architecture. Do not restore their models, migrations, task registration/routing, workers, APIs, or UI controls from historical plans.
- **Do not conflate qualifier smoke with operational health.** Qualifier-spawned VM `smoketest` children stay removed. Deterministic promotion CI and ordinary post-promotion health/deployment checks (including separately requested operational `smoketest` tasks) stay available.

## Test Guidelines
- When fixing test failures, first diagnose: is the failure in the CODE or the TEST?
- Do NOT delete or skip tests just to make them pass
- If a test is testing the wrong thing (stale API, renamed function), fix the test to correctly test current behavior
- Tests may be removed only when the tested functionality itself was removed
- Do NOT weaken assertions without clear justification
- **Require self-certifying-diff review.** Keep at least one literal pin for security-relevant constants. A diff that deletes code and its tests together, or replaces a literal assertion with the very constant it changed, is unreviewable on CI even when it merges green; call out that self-certifying diff explicitly and require independent evidence.

## Common Patterns
- `task.first_payload` is the standard accessor for the first payload element (not `(task.payloads or [{}])[0]`)
- Verify before finishing with TARGETED tests: run the backend test files covering your change (`bash scripts/test-backend.sh testing/test_<module>.py`), plus `cd admin-ui && npm run typecheck` if you touched frontend (use this on agent VMs — `npm run build` OOMs there; CI runs the full vite build). Do NOT run the full backend suite on an agent VM — your task PR gets targeted/per-change CI; the full sharded matrix runs once at the automain→main promotion PR.
- New admin endpoints are authorized with dependencies, not decorators: there is no `@require_project_access` or `@require_role` in this codebase. Use router-level `dependencies=[Depends(get_current_user)]`, a handler-level `current_user: dict = Depends(require_admin)`, and then call `await _get_user_project_with_role(project_id, current_user["user_id"], session, min_role=...)` in the body. That helper is the tenant-isolation boundary: absent membership raises 404 (deliberately indistinguishable from a missing project), an insufficient role raises 403.
- Merge conflicts in derived artifacts (`test-estate-ranked-queue.md`, `backend/.test_durations`, `backend/testing/module_size_limits.json`) have exactly one correct resolution and a script that produces it: run `bash scripts/resolve-derived-conflicts.sh` instead of hand-merging them. It regenerates or unions each one, stages the result, and exits non-zero WITHOUT touching the tree if any conflict needs real judgement (exit 2 = nothing to do). Never pick a side of `test-estate-ranked-queue.md` — it is a whole-tree report, so either side is stale the moment the merge lands.

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
