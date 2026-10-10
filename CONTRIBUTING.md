# Development and verification

Blueprint is a paused personal project. The easiest entry point is the [portfolio demo](docs/portfolio/DEMO.md); operational development requires a database and configured integrations. Read [AGENTS.md](AGENTS.md) before editing lifecycle, release, or conversational code.

## Prerequisites

- Node.js 22+ and npm
- Python 3.11+ for backend and operator tooling
- PostgreSQL with the migration-required `pg_trgm` and `pgcrypto` extensions, or Docker for the repository test harness
- Credentials only for the integrations you intend to exercise

## Credential-free demo

Run these commands from the repository root:

```bash
npm ci
npm run demo
```

Open http://127.0.0.1:3002. See the [demo guide](docs/portfolio/DEMO.md) for a walkthrough, static build, and browser checks.

## Operational dashboard and backend

1. Install the root npm dependencies. Install the backend into a virtual environment and the agent into its own package:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install -r backend/requirements.txt
   npm ci --prefix agent
   ```

2. Copy `.env.example` to `.env` and supply the configuration needed by your execution mode. Use distinct random signing secrets of at least 32 bytes and a valid Fernet encryption key. The sample values are placeholders. Compose reads `.env`; a host Python process does not load it automatically.

3. Start only the development database with `docker compose up -d db`. Set a **host-reachable** `DATABASE_URL` in the backend terminal, using the password configured in `.env`. The template's `db` hostname is for the Compose network; a host process normally uses `127.0.0.1`.

4. Export `AGENT_JWT_SECRET`, `WS_TICKET_SECRET`, and `GITHUB_PAT_ENCRYPTION_KEY` to the backend environment. From `backend/`, run `bash run_migrations.sh`, then start the API:

   ```bash
   # From backend/, with the virtual environment active and configuration set
   export BLUEPRINT_PROCESS_ROLE=api
   uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
   ```

   The API role does not run leader-only scheduling. Running tasks also needs a configured worker role and execution capacity; see the [local control-plane guide](docs/local-control-plane.md) and [runner runbook](docs/RUNNER_RUNBOOK.md).

5. For the dashboard, configure Firebase web authentication through `VITE_FIREBASE_API_KEY`, `VITE_FIREBASE_AUTH_DOMAIN`, and `VITE_FIREBASE_PROJECT_ID`. For trusted local development, `VITE_DASHBOARD_API_TOKEN` can match a backend `ADMIN_API_KEY`; use it only in a local frontend. Run `npm run dev` from the repository root and open http://localhost:3001. Vite proxies `/api` to `127.0.0.1:8080` unless `VITE_DEV_PROXY_TARGET` is set.

Real GitHub/provider workflows additionally require their configured identities and credentials. Consult [configuration](docs/CONFIGURATION.md), [onboarding](docs/ONBOARDING.md), and [coding-agent access](docs/CODING_AGENT_ACCESS.md). The [verification record](docs/portfolio/VERIFICATION.md) identifies the operational checks actually performed for the portfolio cleanup.

## Checks

From the repository root, select the checks appropriate to your change:

```bash
# Demo
npm run demo:test
npm run demo:check             # Requires Playwright Chromium

# Operational frontend
npm run typecheck --workspace admin-ui
npm run lint --workspace admin-ui
npm run test:run --workspace admin-ui

# Agent
npm run typecheck --prefix agent
npm test --prefix agent

# Backend: select files covering the change
bash scripts/test-backend.sh testing/test_task_capabilities.py

# Backend standalone checks without PostgreSQL
bash scripts/test-backend.sh --standalone --no-postgres
```

The backend harness starts and removes its own test database. Its normal route uses Docker; provisioned agent environments may offer the private `--sandbox-postgres` route. Do not point test configuration at a live database. Do not run the full backend suite on an agent VM. The repository's promotion CI owns the full deterministic release gate.

Use frontend type checking on agent VMs; the full operational Vite bundle belongs in CI. The small demo has its own build and browser check. See [TESTING_GUIDE.md](docs/TESTING_GUIDE.md) for viewport tests and deeper test infrastructure.

## Change boundaries

- Use a focused `codex/` or feature branch. Preserve other work and use an isolated checkout for overlapping tasks.
- Keep credentials and generated runtime data out of commits. Use sample fixtures for demonstrations.
- Preserve existing migration history; schema changes need a new chained revision and an updated expected schema revision.
- CI workflows are managed separately. Do not modify `.github/workflows/` during ordinary task work.
- Diagnose test failures before changing assertions. Do not delete or skip a test to make a check pass.
- Keep code, documentation, and evidence consistent. Current implementation and [AGENTS.md](AGENTS.md) boundaries take precedence over historical proposals.

The current [license](LICENSE) reserves reuse rights; this guide documents development and does not grant a contribution or redistribution license.
