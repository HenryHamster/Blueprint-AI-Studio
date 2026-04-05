# Blueprint AI Studio

## File Restrictions
- Do NOT modify files in `.github/workflows/` — CI workflows are managed separately and the GitHub App token cannot push workflow changes
- Do NOT modify `backend/alembic/` migration files — migrations are generated, not hand-edited
- Do NOT modify files in `scripts/` unless explicitly asked

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
