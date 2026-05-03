# Blueprint AI Studio

## File Restrictions
- Do NOT modify files in `.github/workflows/` — CI workflows are managed separately and the GitHub App token cannot push workflow changes
- Do NOT modify **existing** migration files in `backend/alembic/versions/` — applied history is immutable. Adding a new migration file (next revision, chained from the latest `down_revision`) is fine and expected when `models.py` changes; also bump `EXPECTED_SCHEMA_REVISION` in `backend/app/main.py` to match.

## Code Style
- Frontend: React + TypeScript, Tailwind CSS, functional components
- Backend: Python, FastAPI, SQLAlchemy/SQLModel, async/await
- Agent: TypeScript, Node.js

## Testing
- Backend: `bash scripts/test-backend.sh` (builds Docker image, runs import check + full suite)
  - Specific file: `bash scripts/test-backend.sh testing/test_agent.py`
  - Standalone only (no DB): `bash scripts/test-backend.sh --standalone`
- Frontend: `cd admin-ui && npm run build && npx eslint .`
- Agent: `cd agent && npx tsc --noEmit`

## Test Guidelines
- When fixing test failures, first diagnose: is the failure in the CODE or the TEST?
- Do NOT delete or skip tests just to make them pass
- If a test is testing the wrong thing (stale API, renamed function), fix the test to correctly test current behavior
- Tests may be removed only when the tested functionality itself was removed
- Do NOT weaken assertions without clear justification

## Common Patterns
- `task.first_payload` is the standard accessor for the first payload element (not `(task.payloads or [{}])[0]`)
- Always run tests before finishing: backend uses Docker (`bash scripts/test-backend.sh`), frontend: `cd admin-ui && npm run build`
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
