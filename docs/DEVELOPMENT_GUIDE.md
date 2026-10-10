# Blueprint Development Guide

This guide covers common development workflows, patterns, and best practices for working on Blueprint.

---

## Development workflow

Use [CONTRIBUTING.md](../CONTRIBUTING.md) for prerequisites, installation, local authentication, database setup, and current verification commands. The credential-free [portfolio demo](portfolio/DEMO.md) runs on port 3002; the operational dashboard runs on port 3001 and needs the backend.

For changes to Blueprint's multi-branch integration flow, check the configured project integration branch before opening a PR. Ordinary local development can start from the current `main`; repository cleanup must preserve deployment and release state independently.

Run targeted backend files, frontend types/lint/unit tests for UI changes, and agent types/unit tests for runtime changes. Do not run the full backend suite or operational frontend bundle on an agent VM. The promotion PR's CI is the full release test gate.

---

## Common Development Tasks

### Adding a New API Endpoint

**1. Define the route in a router file** (`backend/app/routers/`):

```python
# backend/app/routers/my_feature.py
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_session
from app.auth import get_current_user

router = APIRouter(prefix="/api/my-feature", tags=["my-feature"])

@router.get("/items")
async def list_items(
    session: AsyncSession = Depends(get_session),
    user = Depends(get_current_user)
):
    """List all items."""
    # TODO: implement
    return []
```

**2. Register the router in `main.py`:**

```python
# backend/app/main.py
from app.routers.my_feature import router as my_feature_router

# In FastAPI setup:
app.include_router(my_feature_router)
```

**3. Define request/response schemas** in `backend/app/schemas.py`:

```python
class ItemRequest(BaseModel):
    name: str
    description: str | None = None

class ItemResponse(BaseModel):
    id: str
    name: str
    created_at: str
```

**4. Add tests** in `backend/testing/test_my_feature.py`:

```python
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_list_items(client: AsyncClient):
    response = await client.get("/api/my-feature/items")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
```

**5. Document in [API_REFERENCE.md](API_REFERENCE.md)** (if user-facing endpoint).

---

### Modifying the Database Schema

**1. Edit `backend/app/models.py`** to update ORM model:

```python
class MyModel(SQLModel, table=True):
    """Description of the model."""
    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    # New field
    new_field: str | None = None
```

**2. Create a migration:**

```bash
cd backend
alembic revision --autogenerate -m "Add new_field to MyModel"
```

This creates a new migration file in `backend/alembic/versions/`.

**3. Review the migration** (edit if needed for safety):

```python
# backend/alembic/versions/0115_add_new_field.py
def upgrade():
    op.add_column('mymodel', sa.Column('new_field', sa.String, nullable=True))

def downgrade():
    op.drop_column('mymodel', 'new_field')
```

**4. Apply the migration:**

```bash
bash backend/run_migrations.sh
```

**5. Update `EXPECTED_SCHEMA_REVISION`** in `backend/app/main.py`:

```python
EXPECTED_SCHEMA_REVISION = "0115"  # Update to match your migration
```

**6. Test it:**

```bash
bash scripts/test-backend.sh testing/test_models.py
```

**Important:** Never modify existing migration files — they're immutable history. If you made a mistake, create a new migration to fix it.

---

### Adding a New Task Handler

Task handlers are in `agent/src/handlers/`.

**1. Create handler file** (e.g., `agent/src/handlers/my-task.ts`):

```typescript
import type WebSocket from 'ws';
import { sendLog, runClaudeCode, persistAndPostStatus } from '../index.js';

export async function handleMyTask(
  taskId: string,
  payload: Record<string, unknown>,
  task: Record<string, unknown>,
  ghToken: string,
  owner: string,
  repo: string,
  ws: WebSocket | null,
  statusRef: { value: string },
  model?: string,
  effort?: string
) {
  try {
    sendLog(taskId, `Starting my task...`, ws);
    
    // TODO: implement your handler logic
    // - Clone repo, run Claude, create PR, etc.
    
    await persistAndPostStatus(
      taskId,
      'COMPLETED',
      {
        artifact_type: 'PULL_REQUEST',
        pr_url: 'https://...',
        logs: '...',
      }
    );
  } catch (error) {
    sendLog(taskId, `Error: ${error.message}`, ws);
    await persistAndPostStatus(taskId, 'FAILED', {
      logs: error.message,
      failure_category: 'my_task_failed',
    });
  }
}
```

**2. Register in `agent/src/handlers/index.ts`:**

```typescript
import { handleMyTask } from './my-task.js';

export const handlers: Record<AgentTaskType, Handler> = {
  // ... existing ...
  my_task: handleMyTask,
};
```

**3. Add to `AgentTaskType` enum** in `agent/src/taskCapabilities.ts`:

```typescript
export type AgentTaskType =
  | 'create'
  | 'refactor'
  | 'my_task'  // Add your type
  // ... rest ...
```

**4. Add tests** in `agent/src/handlers/my-task.test.ts`:

```typescript
import { describe, it, expect } from 'vitest';
import { handleMyTask } from './my-task.js';

describe('handleMyTask', () => {
  it('should handle the task', async () => {
    // TODO: implement tests
  });
});
```

**5. Document in [AGENT_HANDLERS.md](AGENT_HANDLERS.md)**.

---

### Adding a Frontend Component

Components are in `admin-ui/src/components/`.

**1. Create component file** (`admin-ui/src/components/MyComponent.tsx`):

```typescript
/**
 * MyComponent — Displays a summary of something important.
 *
 * Props:
 *   - title: string — component heading
 *   - data: MyData[] — items to display
 *   - onAction: (id: string) => void — callback when user takes action
 */

import React from 'react';

interface MyComponentProps {
  title: string;
  data: MyData[];
  onAction: (id: string) => void;
}

export default function MyComponent({
  title,
  data,
  onAction,
}: MyComponentProps) {
  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold">{title}</h2>
      {data.map(item => (
        <div key={item.id} className="p-4 border rounded-lg">
          <p>{item.name}</p>
          <button
            onClick={() => onAction(item.id)}
            className="mt-2 px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
          >
            Action
          </button>
        </div>
      ))}
    </div>
  );
}
```

**2. Add unit test** (`admin-ui/src/components/MyComponent.test.tsx`):

```typescript
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MyComponent from './MyComponent';

describe('MyComponent', () => {
  it('renders title', () => {
    render(<MyComponent title="Test" data={[]} onAction={() => {}} />);
    expect(screen.getByText('Test')).toBeInTheDocument();
  });

  it('calls onAction when button clicked', async () => {
    const onAction = vi.fn();
    const data = [{ id: '1', name: 'Item 1' }];
    
    render(<MyComponent title="Test" data={data} onAction={onAction} />);
    const button = screen.getByRole('button');
    
    await userEvent.click(button);
    expect(onAction).toHaveBeenCalledWith('1');
  });
});
```

**3. Add viewport regression test** (`admin-ui/src/components/MyComponent.viewport.test.tsx`):

```typescript
import { test } from '@playwright/experimental-ct-react';
import MyComponent from './MyComponent';

test('MyComponent does not overflow at 375px', async ({ mount }) => {
  const component = await mount(
    <MyComponent title="Test" data={[]} onAction={() => {}} />
  );
  const box = await component.boundingBox();
  if (box) {
    expect(box.width).toBeLessThanOrEqual(375);
  }
});
```

**4. Import in parent**:

```typescript
import MyComponent from '@/components/MyComponent';

export default function Parent() {
  return <MyComponent title="..." data={...} onAction={...} />;
}
```

**5. Document in [FRONTEND_COMPONENTS.md](FRONTEND_COMPONENTS.md)**.

---

## Code Style & Patterns

### Python Backend

**Imports:** Group standard library, third-party, local:
```python
import asyncio
from datetime import datetime
from typing import List

from fastapi import FastAPI
from sqlmodel import select

from app.models import Task
from app.database import get_session
```

**Async functions:** Always use `async def` for DB/network I/O:
```python
async def fetch_task(session: AsyncSession, task_id: UUID) -> Task:
    result = await session.execute(select(Task).where(Task.id == task_id))
    return result.scalar_one_or_none()
```

**Logging:** Use `logger.info()` / `logger.warning()` / `logger.error()`:
```python
import logging
logger = logging.getLogger(__name__)

logger.info(f"Task {task_id} started")
```

**Docstrings:** Brief class/function summary + key params:
```python
def my_function(param: str) -> bool:
    """Check if param is valid.
    
    Args:
        param: The value to validate.
    
    Returns:
        True if valid, False otherwise.
    """
    ...
```

### TypeScript Agent

**Imports:** Similar grouping:
```typescript
import fs from 'fs';
import { spawn } from 'child_process';

import WebSocket from 'ws';
import { extract } from 'json-extract';

import { sendLog, persistAndPostStatus } from '../index.js';
```

**Types:** Always annotate:
```typescript
interface TaskContext {
  taskId: string;
  status: 'IN_PROGRESS' | 'COMPLETED' | 'FAILED';
  logs: string[];
}

async function handleTask(ctx: TaskContext): Promise<void> {
  // ...
}
```

**Comments:** Only explain *why*, not *what*:
```typescript
// Bad: describes what the code does
const result = items.filter(x => x.active);  // Filter active items

// Good: explains why
// Only process items with active=true to avoid task-inactive race condition
const result = items.filter(x => x.active);
```

### React Frontend

**Components:** Functional components with explicit props:
```typescript
interface Props {
  title: string;
  items: Item[];
  onSelect: (id: string) => void;
}

export default function MyComponent({ title, items, onSelect }: Props) {
  return (
    // JSX...
  );
}
```

**Tailwind:** Use existing utility classes, avoid custom CSS:
```typescript
// Good
<div className="p-4 rounded-lg border border-slate-200 bg-white">

// Avoid custom CSS/styled-components
```

**Hooks:** Use `useEffect` for side effects, `useState` for local state:
```typescript
const [data, setData] = useState<T[]>([]);

useEffect(() => {
  fetch('/api/data').then(r => r.json()).then(setData);
}, []); // Empty dependency array = run once on mount
```

---

## Debugging

### Backend

**View logs during development:**
```bash
# In another terminal
docker logs -f blueprint-db  # PostgreSQL
```

**Debug a specific test:**
```bash
# Run single test file
bash scripts/test-backend.sh testing/test_my_module.py
```

**Inspect database:**
```bash
# Connect to Postgres
psql postgresql://postgres:supersecret_blueprint_pass@localhost:5432/postgres

# List tables
\dt

# Query tasks
SELECT id, status, created_at FROM task LIMIT 5;
```

### Frontend

**Use browser DevTools:**
- Open http://localhost:3001
- Press F12 for DevTools
- Network tab shows API calls
- Console shows JS errors

**Disable Vite HMR in `.env` (if hot reload causes issues):**
```bash
# Optional: restart with explicit host
npm run dev -- --host 0.0.0.0
```

### Agent

**View agent logs in admin UI:**
1. Launch a task in admin UI
2. Click on task card to open drawer
3. Switch to "Logs" tab
4. Logs stream in real-time from WebSocket

**In local mode, see subprocess output:**
```bash
# Agent subprocess inherits stdio
# Any `console.log()` appears in your terminal
```

---

## Performance Tips

### Database Queries

**Avoid N+1 queries:**
```python
# Bad: 1 query for project + N queries for tasks
project = await session.get(Project, project_id)
tasks = await session.execute(select(Task).where(Task.project_id == project_id))

# Good: single JOIN query
result = await session.execute(
    select(Project).options(selectinload(Project.tasks))
    .where(Project.id == project_id)
)
```

**Index frequently-filtered columns:**
See `backend/app/models.py` for index definitions.

### Frontend

**Memoize expensive renders:**
```typescript
import { useMemo } from 'react';

// Avoid re-computing unless items change
const sorted = useMemo(
  () => items.sort((a, b) => a.name.localeCompare(b.name)),
  [items]
);
```

---

## Common Pitfalls

### Forgetting to run migrations

```bash
# After pulling code with schema changes
bash backend/run_migrations.sh
```

### Modifying existing migrations

❌ **Never edit a migration file** — it's immutable history.
✅ Create a new migration file if you need to fix something.

### Not updating `EXPECTED_SCHEMA_REVISION`

If you create a new migration but forget to update `EXPECTED_SCHEMA_REVISION` in `main.py`, tests will fail.

### Hardcoding environment variables

❌ Never hardcode API keys or secrets in code.
✅ Always read from `os.getenv()` or `.env` file.

### Missing error handling

❌ Handler crashes if Claude output malformed.
✅ Wrap in try/catch, report FAILED with error details.

### Viewport test inconsistency

If viewport tests fail locally but pass in CI:
- Ensure Playwright is installed: `npx playwright install`
- Run with `npm run test:viewport` (not `npm run test:run`)

---

## Getting Help

- **Code questions?** Open an issue with [code] tag
- **Architecture unclear?** Check docs/codebase-analysis.md
- **Feature request?** Open an issue with [feature-request] tag
- **Bug found?** Check existing issues first, then open new issue with [bug] tag

See CONTRIBUTING.md for contributor guidelines.
