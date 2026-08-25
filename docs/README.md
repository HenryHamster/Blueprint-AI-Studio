# Blueprint Documentation Index

Welcome to the Blueprint documentation. This directory contains comprehensive guides for understanding, developing, and maintaining the Blueprint platform.

---

## 📖 Documentation Structure

### Getting Started
- **[../README.md](../README.md)** — Project overview, quick start, architecture, and high-level description
- **[../Deploy.md](../Deploy.md)** — Production deployment on GCP Cloud Run with Terraform and Packer
- **[ONBOARDING.md](ONBOARDING.md)** — Canonical guide for user access, project roles, both GitHub identities, repository creation, collaborators, safe autonomy defaults, and post-onboard configuration
- **[../CONTRIBUTING.md](../CONTRIBUTING.md)** — Contributor environment setup and the clone → branch → test → PR workflow
- **[../CLAUDE.md](../CLAUDE.md)** — Project coding standards, file restrictions, and development guidelines

### API & Data
- **[API_REFERENCE.md](API_REFERENCE.md)** — Complete HTTP endpoint reference
  - Authentication schemes (Firebase, JWT, API key)
  - All public endpoints with request/response examples
  - Error handling and rate limiting
  - WebSocket message format

- **[DATA_MODELS.md](DATA_MODELS.md)** — Data structures and schemas
  - SQLModel ORM models (Project, Task, Artifact, etc.)
  - Pydantic DTOs for API requests/responses
  - Enum definitions and constants
  - Database indexes and schema versioning

- **[PAYLOADS_AND_SCHEMAS.md](PAYLOADS_AND_SCHEMAS.md)** — Task payload reference
  - Payload structure for supported task types
  - Field validation rules and constraints
  - Complete examples for each task type
  - How to extend payloads

### Architecture & Implementation

- **[TASK_LIFECYCLE.md](TASK_LIFECYCLE.md)** — Task state machine and lifecycle
  - Complete status flow diagram
  - State transitions and callbacks
  - Parent-child task relationships
  - Task locking and concurrency
  - Warm VM reuse architecture
  - Circuit breaker mechanism

- **[BACKEND_MODULES.md](BACKEND_MODULES.md)** — Backend Python modules reference
  - Core infrastructure (main.py, models.py, database.py)
  - Orchestration (orchestrator.py, reaper.py, compute.py)
  - Task lifecycle management
  - Autonomous research mode implementation
  - GitHub integration and analytics
  - Summary table of all modules

- **[AGENT_HANDLERS.md](AGENT_HANDLERS.md)** — TypeScript agent task handlers
  - Standard handlers (create, refactor, docs, etc.)
  - Review & fix handlers
  - Research & planning handlers
  - Specialized handlers (security audit, perf, migrate, etc.)
  - Error handling and telemetry
  - Handler registration and testing

- **[FRONTEND_COMPONENTS.md](FRONTEND_COMPONENTS.md)** — React component reference
  - Layout & shell components
  - Task management UI
  - Dashboards and analytics
  - Autonomous mode components
  - Settings & configuration UI
  - Utility components
  - Data hooks (useAppState, useWebSocket)
  - Styling guide (Tailwind CSS)
  - Testing patterns (viewport + unit tests)

### Development & Debugging

- **[DEBUG_GUIDE.md](DEBUG_GUIDE.md)** — Debugging failed tasks and troubleshooting
  - Understanding failure categories
  - Viewing and interpreting task logs
  - Diagnosing common failures (clone, build, test, lint, timeout)
  - Using the admin UI to debug
  - GCP VM inspection
  - Circuit breaker recovery
  - Performance profiling
  - Frequently asked questions

- **[DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md)** — Hands-on development guide
  - Local environment setup
  - Development workflow (branch, test, PR)
  - Common tasks (adding endpoints, modifying schema, handlers, components)
  - Code style & patterns (Python, TypeScript, React)
  - Debugging techniques
  - Performance optimization tips
  - Common pitfalls and solutions

### Operator Experience

- **[RUNNER_RUNBOOK.md](RUNNER_RUNBOOK.md)** — Self-hosted runner enrollment runbook
  - Per-shape steps for WSL PC, second PC, DGX, and lab server
  - Expected `blueprint-runner check` output and GPU detection
  - Common failure fixes and bwrap/container troubleshooting
  - Multi-runner registration, observability, and partitioning

- **[COMPUTE_RUN_LOCAL.md](COMPUTE_RUN_LOCAL.md)** - Running a compute experiment on your own hardware
  - The BLUEPRINT_RESULT_FILE / BLUEPRINT_ARTIFACT_DIR / BLUEPRINT_LOCAL_GPU file contract
  - Exit-code contract (0 met, 1 command failed, 2 nothing measured, 3 criterion not met)
  - Spec files: setup_step, container_image, repo_clone, env, cwd, arms/sweeps
  - `--pull` and using an image you built locally
  - The realised-environment record (image digest, commit, pip freeze)

- **[operator-experience-operating-model.md](operator-experience-operating-model.md)** — Canonical operator IA and decision loops
  - Five operator spaces: Operate, Work, Strategy, Fleet, Policy
  - Core object relationships (project, task, wave, PR, direction, finding, version, runner, account)
  - Decision loops by task type (review/fix, research, compute experiment, PR merge)
  - Useful queue definitions (needs decision, needs intervention, needs review, safe to ignore)
  - Design rules for future implementation tasks

### Analysis & Reference

- **[codebase-analysis.md](codebase-analysis.md)** — High-level codebase overview
  - Architecture summary
  - Technology stack
  - Component communication patterns
  - Module dependencies

- **[db-operations.md](db-operations.md)** — Database operations reference
  - Manual query examples
  - Backup/restore procedures
  - Migration history

- **[COMMON_PATTERNS.md](COMMON_PATTERNS.md)** — Frequently-used code patterns
  - Database patterns (row locking, column loading, batch updates)
  - API endpoint patterns (validation, rate limiting, WebSocket auth)
  - Error handling (HTTP exceptions, callbacks)
  - Async/await patterns (timeouts, session management)
  - Testing patterns (fixtures, mocking, DB reset)

- **[concurrency.md](concurrency.md)** — Concurrency patterns
  - Row-level locking (FOR UPDATE)
  - Async/await patterns
  - Race condition handling

- **[db-migration-plan.md](db-migration-plan.md)** — Database migration strategy

- **[db-deferrals.md](db-deferrals.md)** — Database/infrastructure work deliberately deferred by the 2026-04-29 audit, each with a re-evaluation trigger and current mitigation

---

## 🚀 Quick Navigation by Task

**I want to...**

- **Launch Blueprint locally** → [Quick Start in ../README.md](../README.md#quick-start-local-mode)
- **Deploy to production** → [Deploy.md](../Deploy.md)
- **Enroll a self-hosted runner** → [RUNNER_RUNBOOK.md](RUNNER_RUNBOOK.md)
- **Onboard a new repository** → [ONBOARDING.md](ONBOARDING.md)
- **Understand task lifecycle** → [TASK_LIFECYCLE.md](TASK_LIFECYCLE.md)
- **Debug a failed task** → [DEBUG_GUIDE.md](DEBUG_GUIDE.md)
- **Configure a task with custom payload** → [PAYLOADS_AND_SCHEMAS.md](PAYLOADS_AND_SCHEMAS.md)
- **Use common code patterns** → [COMMON_PATTERNS.md](COMMON_PATTERNS.md) (row locking, API validation, async patterns, etc.)
- **Add a new API endpoint** → [DEVELOPMENT_GUIDE.md — Adding an API Endpoint](DEVELOPMENT_GUIDE.md#adding-a-new-api-endpoint)
- **Modify the database schema** → [DEVELOPMENT_GUIDE.md — Modifying the Database Schema](DEVELOPMENT_GUIDE.md#modifying-the-database-schema)
- **Create a new task handler** → [DEVELOPMENT_GUIDE.md — Adding a Task Handler](DEVELOPMENT_GUIDE.md#adding-a-new-task-handler) + [AGENT_HANDLERS.md](AGENT_HANDLERS.md)
- **Build a new React component** → [DEVELOPMENT_GUIDE.md — Adding a Component](DEVELOPMENT_GUIDE.md#adding-a-frontend-component) + [FRONTEND_COMPONENTS.md](FRONTEND_COMPONENTS.md)
- **Understand the HTTP API** → [API_REFERENCE.md](API_REFERENCE.md)
- **Learn about data models** → [DATA_MODELS.md](DATA_MODELS.md)
- **Understand backend architecture** → [BACKEND_MODULES.md](BACKEND_MODULES.md) + [codebase-analysis.md](codebase-analysis.md)
- **Debug a failing test** → [DEVELOPMENT_GUIDE.md — Debugging](DEVELOPMENT_GUIDE.md#debugging)
- **Optimize a slow query** → [DEVELOPMENT_GUIDE.md — Performance Tips](DEVELOPMENT_GUIDE.md#performance-tips)

---

## 📋 Documentation by File Type

### Backend (Python)
- API reference → [API_REFERENCE.md](API_REFERENCE.md)
- Models & schema → [DATA_MODELS.md](DATA_MODELS.md)
- Module guide → [BACKEND_MODULES.md](BACKEND_MODULES.md)
- Common patterns → [COMMON_PATTERNS.md](COMMON_PATTERNS.md)
- Development guide → [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md) (Python section)
- Environment variables → [../README.md — Configuration](../README.md#configuration)
- Coding standards → [../CLAUDE.md](../CLAUDE.md)

### Agent (TypeScript)
- Task handlers → [AGENT_HANDLERS.md](AGENT_HANDLERS.md)
- Development guide → [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md) (TypeScript section)
- Coding standards → [../CLAUDE.md](../CLAUDE.md)

### Frontend (React + TypeScript)
- Component reference → [FRONTEND_COMPONENTS.md](FRONTEND_COMPONENTS.md)
- Development guide → [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md) (React section)
- Styling → [FRONTEND_COMPONENTS.md](FRONTEND_COMPONENTS.md#styling)
- Testing → [FRONTEND_COMPONENTS.md](FRONTEND_COMPONENTS.md#testing)

### Infrastructure
- Deployment → [../Deploy.md](../Deploy.md)
- Configuration → [../README.md — Configuration](../README.md#configuration)

---

## 🔗 Cross-References

**Task Lifecycle**
1. User creates task via UI (POST /api/deploy) → [API_REFERENCE.md](API_REFERENCE.md)
2. Orchestrator processes → [BACKEND_MODULES.md — orchestrator.py](BACKEND_MODULES.md#orchestratorpy)
3. Agent handler runs → [AGENT_HANDLERS.md](AGENT_HANDLERS.md)
4. Results stored → [DATA_MODELS.md — Task model](DATA_MODELS.md#task)
5. Dashboard displays → [FRONTEND_COMPONENTS.md](FRONTEND_COMPONENTS.md)

**Autonomous Research Cycle**
1. Directions stored → [DATA_MODELS.md](DATA_MODELS.md#direction)
2. Research spawner picks direction → [BACKEND_MODULES.md — strategic_spawning.py](BACKEND_MODULES.md#strategic_spawningpy)
3. Research handler runs → [AGENT_HANDLERS.md — handleResearch](AGENT_HANDLERS.md#handleresearch)
4. Results feed strategic cycle → [BACKEND_MODULES.md — strategic_completion.py](BACKEND_MODULES.md#strategic_completionpy)
5. New directions generated → [BACKEND_MODULES.md — direction_lifecycle.py](BACKEND_MODULES.md#direction_lifecyclepy)

**Real-time Features**
1. Log streaming → [API_REFERENCE.md — WebSocket](API_REFERENCE.md#websocket-apiprojectsproject_idws)
2. Backend broadcast → [BACKEND_MODULES.md — broadcast.py](BACKEND_MODULES.md#broadcastpy)
3. Frontend WebSocket hook → [FRONTEND_COMPONENTS.md — useWebSocket](FRONTEND_COMPONENTS.md#usewebsocket)
4. Component display → [FRONTEND_COMPONENTS.md — TaskDetailDrawer](FRONTEND_COMPONENTS.md#taskdetaildrawer)

---

## 📝 Writing Documentation

When adding new features, update documentation:

1. **New API endpoint?** → Update [API_REFERENCE.md](API_REFERENCE.md)
2. **New database model?** → Update [DATA_MODELS.md](DATA_MODELS.md)
3. **New backend module?** → Update [BACKEND_MODULES.md](BACKEND_MODULES.md)
4. **New task handler?** → Update [AGENT_HANDLERS.md](AGENT_HANDLERS.md)
5. **New React component?** → Update [FRONTEND_COMPONENTS.md](FRONTEND_COMPONENTS.md)
6. **New development pattern?** → Update [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md)

**Documentation checklist:**
- [ ] Include docstrings in code
- [ ] Update relevant docs/ markdown
- [ ] Add cross-references to related docs
- [ ] Include code examples where helpful
- [ ] Update this README if structure changes

---

## 🔍 Key Concepts

### Task Status Flow
```
QUEUED → PROVISIONING → IN_PROGRESS → EVALUATING? → COMPLETED/FAILED/CANCELLED
```
See [TASK_LIFECYCLE.md](TASK_LIFECYCLE.md) for complete lifecycle details, [DATA_MODELS.md — TaskStatus](DATA_MODELS.md#taskstatus) for enum definition, and [BACKEND_MODULES.md — task_lifecycle.py](BACKEND_MODULES.md#task_lifecyclepy) for implementation.

### Task Types
26 bespoke agent task types: create, review, fix, verify_tests, plan, research, strategic_review, design_doc_update, security_audit, qa, deps, docs, smoketest, migrate, debug, perf, investigate, self_audit, release_audit, release_audit_challenge, memo, compute_experiment, web_research, design, refactor, repo_bootstrap.

See [PAYLOADS_AND_SCHEMAS.md](PAYLOADS_AND_SCHEMAS.md) for payload configuration, [AGENT_HANDLERS.md](AGENT_HANDLERS.md) for handler implementation, and `agent/src/taskCapabilities.ts` for the canonical agent-side dispatch list.

### Autonomous Research Cycle
```
Directions → Select → Research Task → Findings → Strategic Review → New Directions
```
See [BACKEND_MODULES.md — autonomous_research.py](BACKEND_MODULES.md#autonomous_researchpy).

### Real-time Architecture
```
Agent (on VM) → Heartbeat + Logs → WebSocket → Admin UI (React)
```
See [API_REFERENCE.md — WebSocket](API_REFERENCE.md#websocket-message-format).

---

## ❓ FAQ

**Q: Where do I find the database schema?**
A: [DATA_MODELS.md](DATA_MODELS.md) describes all models. The actual migration files are in `backend/alembic/versions/`.

**Q: How do I add a new task type?**
A: Add the backend capability entry, add the agent type in `agent/src/taskCapabilities.ts`, create or reuse a handler in `agent/src/handlers/`, register bespoke handlers in `agent/src/handlers/index.ts`, and document the payload/handler in [PAYLOADS_AND_SCHEMAS.md](PAYLOADS_AND_SCHEMAS.md) and [AGENT_HANDLERS.md](AGENT_HANDLERS.md).

**Q: What's the difference between routes in different router files?**
A: Router files in `backend/app/routers/` are organized by feature:
- `projects.py` — Project/task CRUD
- `agent.py` — Agent VM endpoints
- `admin_*.py` — Admin-only endpoints
- `github.py` — GitHub webhooks

**Q: How do I run tests?**
A: See [DEVELOPMENT_GUIDE.md — Running Tests](DEVELOPMENT_GUIDE.md#1-setting-up-your-local-environment).

**Q: Where's the deployment documentation?**
A: See [../Deploy.md](../Deploy.md) for GCP setup, or check `infra/` directory for Terraform code.

---

## 📚 External References

- **FastAPI documentation** — https://fastapi.tiangolo.com/
- **SQLAlchemy** — https://docs.sqlalchemy.org/
- **React** — https://react.dev/
- **Tailwind CSS** — https://tailwindcss.com/
- **PostgreSQL** — https://www.postgresql.org/docs/
- **Claude API** — https://docs.anthropic.com/

---

## 📞 Support

For questions or issues:
1. Check the relevant documentation section above
2. Search existing GitHub issues
3. Open a new issue with [docs] tag if documentation is unclear
4. Propose improvements via PR

---

**Last updated:** 2026-06-11  
**Current schema revision:** see [../backend/app/main.py](../backend/app/main.py)
