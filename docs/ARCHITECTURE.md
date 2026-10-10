# Architecture

Blueprint separates the operator interface, persistent control plane, and task executor. The backend records intent and execution state in PostgreSQL. Runners provide execution capacity; the TypeScript runtime prepares workspaces, runs task handlers, and reports results.

| Component | Source entry points | Responsibility |
| --- | --- | --- |
| Operator workspace | [App.tsx](../admin-ui/src/App.tsx), [API client](../admin-ui/src/api/) | Projects, conversations, task progress, fleet, and settings |
| HTTP API | [main.py](../backend/app/main.py), [routers](../backend/app/routers/) | Authentication, request handling, and application lifetime |
| Persistence | [models.py](../backend/app/models.py), [database.py](../backend/app/database.py), [migrations](../backend/alembic/versions/) | Durable projects, tasks, attempts, artifacts, and release state |
| Scheduling | [orchestrator.py](../backend/app/orchestrator.py), [runner placement](../backend/app/orchestrator_runner_placement.py) | Eligibility, dependencies, capacity, and execution placement |
| Task lifecycle | [task_locking.py](../backend/app/task_locking.py), [task_lifecycle.py](../backend/app/task_lifecycle.py) | Admission, mutation authority, and state transitions |
| Execution | [agent entry point](../agent/src/index.ts), [handlers](../agent/src/handlers/), [local runner](../scripts/local_runner.py) | Workspace preparation, provider execution, heartbeats, and results |
| Recovery | [reaper.py](../backend/app/reaper.py), [recovery_sweeps.py](../backend/app/recovery_sweeps.py) | Missing workers, orphaned work, and completion retries |
| Review and releases | [review_merge_authority.py](../backend/app/review_merge_authority.py), [release_qualifier.py](../backend/app/release_qualifier.py), [serving observation](../backend/app/release_serving_observation.py) | Revision-specific review, promotion, deployment, and serving confirmation |

## Task flow

1. The API validates project access and payloads, then records a task.
2. The scheduler checks dependencies, project policy, conflicts, and available capacity.
3. A runtime claims the task, prepares its workspace, and executes the configured provider.
4. Heartbeats, logs, checkpoints, and completion results return to the backend.
5. Code changes enter CI and review. Other task types have their own artifact and evaluation contracts.
6. Release qualification, promotion, deployment, and serving confirmation use separate persisted state.

The [task lifecycle guide](TASK_LIFECYCLE.md) covers retries, blocking, cancellation, and completion. Task permissions are defined by the backend [capability registry](../backend/app/task_capabilities.py) and validated by the runtime [contract loader](../agent/src/taskCapabilities.ts).

## API and contracts

With the backend running locally, use http://127.0.0.1:8080/docs for API documentation or http://127.0.0.1:8080/openapi.json for the schema. Request payloads are defined in [payload_schemas.py](../backend/app/payload_schemas.py); persisted fields are defined in [models.py](../backend/app/models.py).
