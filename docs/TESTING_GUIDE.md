# Testing Guide

Comprehensive guide to writing, running, and debugging tests across Blueprint's three main layers: backend (Python), frontend (React/TypeScript), and agent (TypeScript).

---

## Overview

Blueprint uses **three test suites**, each with different characteristics:

| Layer | Framework | Speed | Scope | Command |
|-------|-----------|-------|-------|---------|
| **Backend** | pytest | Slow (Docker) | Full integration | `bash scripts/test-backend.sh` |
| **Frontend** | Vitest + Playwright | Medium | Components + viewport | `cd admin-ui && npm run test:viewport` |
| **Agent** | Jest | Fast | Type checking only | `cd agent && npm run build && npm run lint` |

---

## Backend Testing

### Running Tests

#### Full Test Suite (with Docker + PostgreSQL)

```bash
bash scripts/test-backend.sh
```

**What it does:**
1. Builds a Docker image from `backend/Dockerfile`
2. Starts a PostgreSQL container
3. Runs all tests via pytest
4. **Takes ~5-10 minutes on a fresh VM**

**Requirements:**
- Docker and docker-compose installed
- 8GB+ RAM free (OOM at 4GB)
- Network access for downloading base images

#### Specific Test File

```bash
bash scripts/test-backend.sh testing/test_orchestrator.py
```

**What it does:**
1. Builds the Docker image (once)
2. Runs only `test_orchestrator.py`
3. **Much faster** (1-2 minutes)

#### Lightweight Smoke Test (No Docker)

```bash
bash scripts/test-backend.sh --standalone
```

**What it does:**
1. Skips Docker image build
2. Runs only **import checks** and **invariant tests** (module size guardrails, schema revision matching)
3. **Takes ~30 seconds**
4. Good for quick feedback before pushing

**Limitations:**
- Cannot run database-dependent tests
- Does not verify actual database behavior
- Good for catching obvious breakage (import errors, lint, sizes)

### Test Organization

Backend tests are organized by module:

```
backend/testing/
├── conftest.py                    # Shared fixtures, setup/teardown
├── test_orchestrator.py           # orchestrator.py tests
├── test_reaper.py                 # reaper.py tests
├── test_models.py                 # Database model tests
├── test_admin_*.py                # Admin router tests
├── test_agent.py                  # Agent lifecycle (claim, status, heartbeat)
├── test_github.py                 # GitHub integration tests
├── test_failure_classification.py # Failure categorization
├── test_invariants.py             # Module size, schema revision, import guards
└── ... (40+ test files)
```

### Writing Backend Tests

#### Test Fixture Pattern

Use pytest fixtures for database setup, mocking, and teardown:

```python
import pytest
from sqlmodel import Session

@pytest.fixture
async def db_session(async_session_factory) -> AsyncSession:
    """Provide a fresh database session for each test."""
    async with async_session_factory() as session:
        yield session
        await session.rollback()  # Clean up


@pytest.mark.asyncio
async def test_orchestrator_claims_task(db_session):
    """Test that orchestrator transitions QUEUED task to PROVISIONING."""
    # Create a test task
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        status=TaskStatus.QUEUED,
        type="create",
        ...
    )
    db_session.add(task)
    await db_session.commit()

    # Call orchestrator
    await orchestrator_worker(db_session)

    # Verify state changed
    await db_session.refresh(task)
    assert task.status == TaskStatus.PROVISIONING
```

#### Mocking External Services

Mock GitHub API, GCP Compute, and other external services to keep tests fast:

```python
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_pr_merge_with_github_error(db_session):
    """Test fallback when GitHub merge fails."""
    with patch('app.github.merge_pull_request') as mock_merge:
        mock_merge.side_effect = HTTPException(422, "Merge conflict")
        
        result = await attempt_merge(task, gh_token, owner, repo, db_session)
        
        assert result is False
        assert mock_merge.called
```

#### Async Test Pattern

Backend tests are async (pytest-asyncio):

```python
@pytest.mark.asyncio
async def test_async_operation():
    result = await some_async_function()
    assert result == expected
```

### Test Invariants (CI Guards)

Before pushing, ensure these invariants pass:

1. **Module size guardrails** (`test_invariants.py::test_module_within_limit`)
   - Enforces line-count caps per module (see `backend/testing/module_size_limits.json`)
   - If exceeded: **Bump the limit in JSON**, do NOT refactor inline

2. **Schema revision** (`test_invariants.py::test_schema_revision_matches_alembic`)
   - `EXPECTED_SCHEMA_REVISION` in `backend/app/main.py` must match latest Alembic migration
   - If outdated: Update `EXPECTED_SCHEMA_REVISION` to the latest revision

3. **Migration chain shape** (`test_invariants.py::test_gapped_linear_chain_validates_clean`)
   - Non-contiguous Alembic revision ids are valid when the chain remains linear
   - Gaps such as `0354 -> 0365` are normal branch-cut artifacts, not missing migrations
   - Do not edit committed files in `backend/alembic/versions/` to annotate a gap; document or test the policy outside migration history

4. **Import guards** (`test_invariants.py::test_*_import_guard`)
   - Certain symbols must be importable from their canonical location
   - Example: `MAX_REVIEW_FIX_ITERATIONS` must be importable from `app.review_pipeline`

5. **Release criterion coverage** (`test_release_qualifier.py::TestBlindMergeGradeBlock`)
   - `criterion_coverage_incomplete` means at least one version acceptance criterion is not fully covered
   - Uncovered criteria and stuck/partial criteria both block promotion until remapped to active direction work or explicitly attested by an operator

6. **Pre-merge dual-head guard shape** (`test_invariants.py::TestPreMergeDualHeadGuardCoverage`)
   - AST-parses `backend/app/ci_completion.py` and `backend/app/task_completion.py` to pin the exact variable-assignment shape around calls to `enforce_premerge_migration_guard` — verifies the guard result is checked fail-closed (early exit) before any `merge_pull_request` call, and that the effective ok/hold-reason variables are never reassigned after the check
   - If a legitimate refactor changes how the guard result is assigned/unpacked (e.g. introducing an intermediate normalization step), this test WILL break even though nothing is actually less safe — update the test's AST assertions to match the new shape while preserving the same fail-closed/no-reassignment-after-check invariant it's designed to protect; do not just loosen the assertion to make it pass
   - This is a narrow, deliberately strict test (see the class docstring) — it exists specifically to prevent an allowance from quietly widening into a full trust zone, so treat any failure here as worth understanding fully, not skipping

### Common Test Patterns

#### Testing Task State Transitions

```python
async def test_task_transitions_to_completed(db_session):
    """Verify task status flow: QUEUED -> PROVISIONING -> IN_PROGRESS -> COMPLETED."""
    task = Task(id=task_id, status=TaskStatus.QUEUED, ...)
    db_session.add(task)
    await db_session.commit()

    # Simulate provisioning
    task.status = TaskStatus.PROVISIONING
    task.vm_instance_id = "test-vm-123"
    await db_session.commit()

    # Simulate completion
    await task_completion.handle_task_completed(task, db_session)
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
```

#### Testing GitHub Integration

```python
async def test_create_pull_request(db_session):
    """Verify PR creation via GitHub API."""
    with patch('app.github.create_pull_request') as mock_create:
        mock_create.return_value = {"number": 42, "html_url": "https://github.com/..."}
        
        result = await github.create_pull_request(
            gh_token, owner="owner", repo="repo",
            title="Test PR", body="Test body", head="feature", base="main"
        )
        
        assert result["number"] == 42
        mock_create.assert_called_once()
```

---

## Frontend Testing

### Running Tests

#### Build + Type Check

```bash
cd admin-ui
npm run build
npx eslint .
```

#### Viewport Tests (Playwright, browser-based)

```bash
cd admin-ui
npm run test:viewport
```

**What it does:**
1. Launches Playwright headless Chrome
2. Renders each component at 375px width (mobile viewport)
3. Checks for overflow, text truncation, touch target sizing
4. **Takes ~2-3 minutes**

**Requirements:**
- Node.js 18+
- Chromium (downloaded by Playwright on first run)
- 2GB+ RAM

#### Unit Tests (jsdom, fast)

```bash
cd admin-ui
npm run test:run
```

**What it does:**
1. Uses jsdom (in-memory browser simulation)
2. Runs React component unit tests
3. **Takes ~30 seconds**

**Limitations:**
- Does not test real browser layout
- No Playwright browser APIs
- Sufficient for component logic, not styling

### Writing Frontend Tests

#### React Component Test Pattern

```typescript
import { render, screen, within } from '@testing-library/react';
import { DashboardHome } from './DashboardHome';

describe('DashboardHome', () => {
  it('renders task list', () => {
    render(<DashboardHome />);
    
    expect(screen.getByText('Recent Tasks')).toBeInTheDocument();
    expect(screen.getByRole('list')).toBeInTheDocument();
  });

  it('navigates to task detail on click', async () => {
    const { user } = render(<DashboardHome tasks={mockTasks} />);
    
    const taskCard = screen.getByText('Create feature X');
    await user.click(taskCard);
    
    expect(screen.getByText('Task Details')).toBeInTheDocument();
  });
});
```

#### Viewport Component Test Pattern

Components must have a corresponding `.viewport.test.tsx` file:

```typescript
import { expectFullViewportAt } from './viewport-test-utils';
import { DashboardHome } from './DashboardHome';

describe('DashboardHome viewport', () => {
  it('fits 375px mobile viewport', async () => {
    await expectFullViewportAt(
      <DashboardHome />,
      375  // width in pixels
    );
    // Asserts: no horizontal overflow, no text truncation, 44px+ touch targets
  });
});
```

#### Mocking API Calls

```typescript
import { rest } from 'msw';
import { setupServer } from 'msw/node';

const server = setupServer(
  rest.get('/api/admin/digest', (req, res, ctx) => {
    return res(ctx.json({ digest: { ...mockDigest } }));
  })
);

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

it('loads digest data', async () => {
  render(<DashboardHome />);
  
  expect(await screen.findByText('Today')).toBeInTheDocument();
});
```

### Viewport Gate

Every component in `admin-ui/src/components/*.tsx` **must** have a viewport test OR an exemption in `admin-ui/src/test/viewport-exempt.json`.

If a component genuinely cannot fit 375px:
```json
{
  "DashboardHome": "wide table, landscape-only"
}
```

---

## Agent Testing

### Running Tests

#### Type Check

```bash
cd agent
npm run build
```

**What it does:**
1. Runs TypeScript compiler (`tsc`)
2. Checks for type errors
3. **Takes ~10 seconds**

#### Lint

```bash
cd agent
npm run lint
```

**What it does:**
1. Runs ESLint per `.eslintrc.cjs`
2. Checks code style (no console.log in production, etc.)
3. **Takes ~5 seconds**

#### Tests

```bash
cd agent
npm test  # or: npm run test:watch
```

**What it does:**
1. Runs Jest test suite
2. Tests handler functions, utilities, parsing logic
3. **Takes ~30 seconds**

### Writing Agent Tests

#### Handler Unit Test Pattern

```typescript
import { handleCreate } from './handlers/standard';

describe('handleCreate', () => {
  it('clones repo and invokes Claude', async () => {
    const mockGit = jest.fn();
    const mockClaude = jest.fn().mockResolvedValue({ success: true });

    await handleCreate(
      taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort
    );

    expect(mockGit).toHaveBeenCalledWith(expect.stringContaining('clone'));
    expect(mockClaude).toHaveBeenCalled();
  });
});
```

#### Utility Function Test Pattern

```typescript
import { detectLoopDivergence } from './utils/diff-detection';

describe('detectLoopDivergence', () => {
  it('detects when diffs are diverging', () => {
    const diff1 = 'diff --git a/file.ts\n+new code 1';
    const diff2 = 'diff --git a/file.ts\n+different code 2';
    
    const diverged = detectLoopDivergence(diff1, diff2);
    
    expect(diverged).toBe(true);
  });
});
```

---

## CI/CD Integration

### Local Checks Before Pushing

```bash
# Lint/build only for changed stacks
./check

# Include the repo's configured smoke tests for changed stacks
./check test

# Force repo-wide lint/build scope instead of changed-file scoping
./check full
```

### GitHub Actions

On PR:
1. **Lint** (`backend/` ruff, `admin-ui/` eslint, `agent/` eslint)
2. **Type Check** (TypeScript in agent and admin-ui)
3. **Build** (Frontend and agent)
4. **Test** (Full backend test suite with Docker + PostgreSQL)
5. **Viewport** (Mobile layout checks for all components)

Branches that fail any check are blocked from merging.

---

## Debugging Tests

### Backend Test Debugging

View logs while test runs:

```bash
bash scripts/test-backend.sh testing/test_orchestrator.py -s -v
```

Flags:
- `-s` — Show print statements and logging (not captured)
- `-v` — Verbose output
- `--tb=short` — Shorter traceback format

Debug a single test:

```bash
bash scripts/test-backend.sh testing/test_orchestrator.py::test_orchestrator_claims_task -v
```

### Frontend Test Debugging

Debug in VS Code:

```bash
# In .vscode/launch.json:
{
  "type": "node",
  "request": "launch",
  "program": "${workspaceFolder}/admin-ui/node_modules/.bin/vitest",
  "args": ["--inspect-brk", "--no-coverage"],
  "console": "integratedTerminal"
}
```

Then run: `F5` (Debug → Start Debugging)

### Agent Test Debugging

```bash
cd agent
npm test -- --verbose src/handlers/standard.test.ts
```

---

## Best Practices

### General

1. **Write tests for new behavior** — Coverage should increase with features
2. **Test edge cases** — Empty lists, null values, timeouts, rate limits
3. **Mock external services** — Keep tests fast and deterministic
4. **Use descriptive test names** — `test_user_can_create_project_with_invalid_owner` > `test_error`

### Backend

1. **Test async behavior** — Use `@pytest.mark.asyncio` and `await`
2. **Clean up DB state** — Use fixtures to rollback or create fresh session per test
3. **Test error paths** — Assert that failures are caught and logged correctly

### Frontend

1. **Test user interactions** — Click, type, submit; verify state changes
2. **Avoid testing implementation** — Test what users see, not internal state
3. **Mock API calls** — Don't hit real endpoints in tests

### Agent

1. **Test deterministically** — Avoid real git/Claude calls; mock them
2. **Test error handling** — Verify graceful fallbacks for network errors, invalid input
3. **Keep tests fast** — No real VMs, no real API calls

---

## Troubleshooting

### "Test suite OOMs on this 8GB VM"

The full Docker backend test suite requires ~6GB of free RAM. If you see:

```
error: command failed: exit code 137 (killed by OOM)
```

Solutions:
1. Run a single test file: `bash scripts/test-backend.sh testing/test_foo.py`
2. Use lightweight smoke test: `bash scripts/test-backend.sh --standalone`
3. Clean Docker cache: `docker system prune -a`
4. Increase VM memory

### "Tests fail intermittently"

Common causes:
1. **Race conditions** — Tests modifying shared state (e.g., `_shared_state` dicts)
2. **External service flakiness** — GitHub API timeout, GCP quota limit
3. **Database deadlock** — Concurrent transactions on the same row

Fixes:
1. Add synchronization primitives (locks, semaphores)
2. Increase timeout, add retry logic
3. Run serially: `pytest -n 0` (disables pytest-xdist parallel execution)

### "Viewport test fails: element overflows at 375px"

Fix options:
1. **Redesign component** to fit (best)
2. **Add responsive breakpoint** for mobile (good)
3. **Exempt component** in `viewport-exempt.json` (last resort; uses budget)

Each exemption is one unit in the budget (`admin-ui/src/test/viewport-escape-hatch-budget.json`); budget is capped at 20 per PR.

---

## Related Documentation

- [CLAUDE.md](../CLAUDE.md) — Testing section with common patterns
- [.github/workflows/](../.github/workflows/) — CI/CD pipeline definitions
