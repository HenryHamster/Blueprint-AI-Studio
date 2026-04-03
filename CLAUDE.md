# Blueprint AI Studio

## File Restrictions
- Do NOT modify files in `.github/workflows/` — CI workflows are managed separately and the GitHub App token cannot push workflow changes
- Do NOT modify `backend/alembic/` migration files — migrations are generated, not hand-edited
- Do NOT modify files in `scripts/` unless explicitly asked

## Code Style
- Frontend: React + TypeScript, Tailwind CSS, functional components
- Backend: Python, FastAPI, SQLAlchemy/SQLModel, async/await
- Agent: TypeScript, Node.js

## Test Guidelines
- When fixing test failures, first diagnose: is the failure in the CODE or the TEST?
- Do NOT delete or skip tests just to make them pass
- If a test is testing the wrong thing (stale API, renamed function), fix the test to correctly test current behavior
- Tests may be removed only when the tested functionality itself was removed
- Do NOT weaken assertions without clear justification
