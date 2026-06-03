# Common Development Patterns

This document describes frequently-used patterns and idioms in the Blueprint codebase.

## Table of Contents

- [Database Patterns](#database-patterns)
- [API Endpoint Patterns](#api-endpoint-patterns)
- [Error Handling](#error-handling)
- [Async/Await Patterns](#asyncawait-patterns)
- [Testing Patterns](#testing-patterns)

---

## Database Patterns

### Row Locking for Atomic Updates

When updating shared state that requires mutual exclusion, use `with_for_update()`:

```python
from sqlmodel import select
from sqlalchemy.orm import load_only

stmt = (
    select(Task)
    .where(Task.id == task_id)
    .with_for_update()  # Acquires exclusive lock
)
task = (await session.execute(stmt)).scalars().first()

# Modify task
task.status = TaskStatus.IN_PROGRESS
await session.commit()
```

For high-contention code, use `skip_locked=True` to avoid blocking:

```python
stmt = (
    select(Task)
    .where(Task.status == TaskStatus.QUEUED)
    .with_for_update(skip_locked=True)  # Skip locked rows, don't block
)
task = (await session.execute(stmt)).scalars().first()
```

**See also:** `docs/concurrency.md` for detailed row-locking patterns.

### Selective Column Loading

Use `load_only()` to reduce memory/network overhead for large fetches:

```python
stmt = (
    select(Task)
    .where(Task.project_id == project_id)
    .options(load_only(Task.id, Task.status, Task.created_at))
)
tasks = (await session.execute(stmt)).scalars().all()
```

**Caveat:** If downstream code accesses unloaded columns, SQLAlchemy will lazy-load them, triggering a `MissingGreenlet` error in async context. Either load all needed columns upfront or refetch in a nested session.

### Batch Updates with SQLAlchemy Core

For bulk updates, use `sqlalchemy.update()` instead of ORM iteration:

```python
from sqlalchemy import update
from app.models import Task

stmt = (
    update(Task)
    .where(Task.project_id == project_id, Task.status == TaskStatus.QUEUED)
    .values(status=TaskStatus.CANCELLED)
)
result = await session.execute(stmt)
await session.commit()
# result.rowcount contains the number of updated rows
```

### Naive UTC DateTime Handling

The codebase uses naive (timezone-unaware) UTC datetimes to avoid confusion:

```python
from app.time_utils import _utcnow

now = _utcnow()  # Returns naive UTC datetime, never timezone-aware

# Database columns use NaiveUTCDateTime type (strips tz on read)
from app.models import NaiveUTCDateTime  # Not typically imported; used in model definitions
```

**Important:** Never mix `datetime.now(timezone.utc)` with naive database columns — this causes `TypeError` and silent rollbacks.

---

## API Endpoint Patterns

### Request/Response Validation with Pydantic

All request bodies should be validated via Pydantic schemas:

```python
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas import DeployPayload
from app.auth import get_current_user
from app.database import get_session

router = APIRouter()

@router.post("/api/deploy")
async def deploy(
    payload: DeployPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    # FastAPI automatically validates payload against DeployPayload schema
    # Invalid requests return 422 with field error details
    ...
```

### Rate Limiting

Use the `limiter` from `app.shared_state`:

```python
from app.shared_state import limiter

@router.get("/api/tasks")
@limiter.limit("60/minute")
async def list_tasks(request: Request, ...):
    # Requests exceeding 60/minute get 429 Too Many Requests
    ...
```

### WebSocket Authentication

WebSocket endpoints require explicit ticket-based authentication:

```python
from app.auth import verify_ws_ticket

@router.websocket("/api/projects/{project_id}/ws")
async def websocket_endpoint(websocket: WebSocket, project_id: str):
    await websocket.accept()
    
    try:
        auth_message = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
        ticket = auth_message.get("ticket")
        payload = verify_ws_ticket(ticket)  # Validates and decodes
        
        if payload.get("project_id") != project_id:
            await websocket.close(code=1008, reason="Project ID mismatch")
            return
    except asyncio.TimeoutError:
        await websocket.close(code=1008, reason="Auth timeout")
        return
```

---

## Error Handling

### HTTP Exceptions

Use FastAPI's `HTTPException` for expected error conditions:

```python
from fastapi import HTTPException

# 400 Bad Request
raise HTTPException(status_code=400, detail="Invalid payload")

# 404 Not Found
raise HTTPException(status_code=404, detail="Task not found")

# 409 Conflict
raise HTTPException(status_code=409, detail="Task already in progress")

# 429 Too Many Requests (for transient lock contention)
raise HTTPException(
    status_code=429,
    detail="Resource locked, retry shortly",
    headers={"Retry-After": "2"}
)
```

### Task Status Callbacks

Use the `_on_task_terminal_callbacks` registry to run cleanup on task completion:

```python
from app.models import _on_task_terminal_callbacks

@_on_task_terminal_callbacks.register
async def cleanup_task(task):
    # Called automatically when task reaches COMPLETED/FAILED/CANCELLED
    await cleanup_vm(task.vm_instance_id)
```

---

## Async/Await Patterns

### Timeout Safety

Always wrap long-running operations with `asyncio.wait_for()`:

```python
import asyncio

try:
    result = await asyncio.wait_for(
        long_running_operation(),
        timeout=30.0  # seconds
    )
except asyncio.TimeoutError:
    logger.error("Operation timed out")
    raise HTTPException(status_code=504, detail="Operation timeout")
```

### Session Management in Long Operations

Long-running post-completion work should open a separate session to avoid holding task locks:

```python
async def process_post_completion(task_id):
    # Use a fresh session so we don't hold locks from the prior operation
    async with AsyncSessionLocal() as session:
        task = await session.get(Task, task_id)
        # Do lengthy GitHub I/O, etc.
        await github.create_pr(...)
```

---

## Testing Patterns

### Fixtures and Factories

Use pytest fixtures for common test setup:

```python
@pytest.fixture
async def project(session: AsyncSession) -> Project:
    project = Project(
        name="test-project",
        github_owner="owner",
        github_repo="repo",
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project

async def test_get_project(client, project):
    response = await client.get(f"/api/projects/{project.id}")
    assert response.status_code == 200
    assert response.json()["name"] == "test-project"
```

### Mocking External Calls

Mock GitHub, Cloud Storage, and other external services:

```python
from unittest.mock import patch, AsyncMock

@patch("app.github.create_check_run")
async def test_task_completion(mock_check_run):
    mock_check_run.return_value = {"id": 123}
    
    # Test code that calls github.create_check_run()
    ...
    
    mock_check_run.assert_called_once()
```

### Database Reset Between Tests

Use `conftest.py` to reset the database:

```python
# In backend/testing/conftest.py

@pytest.fixture(autouse=True)
async def reset_db(session: AsyncSession):
    # Runs before each test
    yield
    # Cleanup after each test
    await session.execute(delete(Task))
    await session.execute(delete(Project))
    await session.commit()
```

---

## Utility Functions

### Time Utilities

```python
from app.time_utils import _utcnow, _to_naive_utc

now = _utcnow()  # Current naive UTC time
utc_naive = _to_naive_utc(datetime.now(timezone.utc))  # Strip timezone
```

### Broadcast Messages

```python
from app.broadcast import broadcast_to_project

await broadcast_to_project(
    project_id,
    "agent_log",
    {"task_id": str(task.id), "log": "Agent started"}
)
```

### Shared State Access

```python
import app.shared_state as shared_state

# Record VM heartbeat for warm-pool tracking
shared_state.record_vm_heartbeat(vm_instance_id)

# Query runtime configuration
integration_branch = shared_state.get_project_integration_branch(project_id)
promotion_target = shared_state.get_project_promotion_target(project_id)
```

---

## See Also

- **[DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md)** — Hands-on development workflow
- **[concurrency.md](concurrency.md)** — Detailed concurrency patterns
- **[API_REFERENCE.md](API_REFERENCE.md)** — Full HTTP API specification
- **[DATA_MODELS.md](DATA_MODELS.md)** — Database schema reference
