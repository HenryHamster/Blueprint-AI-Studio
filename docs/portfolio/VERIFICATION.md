# Verification record

Checks run on **2026-10-09 (America/Los_Angeles)** during repository cleanup. The application baseline was `4d83283bde31b8ca7142700224883dfe8607863d`.

The cleanup retains the existing application UI. Its source under `admin-ui/src/` and its `vite.config.ts` match that baseline exactly. `npm run demo` launches that application on port 3002.

## Executed checks

| Check | Result |
| --- | --- |
| Dashboard types | `npm run typecheck --workspace admin-ui` passed after restoring the original UI entry point |
| Full dashboard unit suite | **254 files, 4,732 tests passed** after removing the separate simulated UI and its five tests |
| Full dashboard lint | **0 errors**, 591 existing warnings |
| Existing UI browser check | HTTP 200, original `/src/main.tsx` entry point, Google sign-in screen, and no browser page errors |
| Agent dependency installation and types | `npm ci --prefix agent` and `npm run typecheck --prefix agent` passed |
| Full agent unit suite | **131 files, 6,615 tests passed** |
| Selected backend tests on fresh PostgreSQL | **106 passed, 1 skipped**: capabilities, task locking, serving observation |
| Documentation guard | **1 passed**: revised documentation references no registered retired code paths |
| Snapshot-export tests | **5 passed**: Git identity, provenance, revision selection, content preservation, and refusal to overwrite other work |
| Exporter Python lint | Ruff passed |
| Real backend API startup smoke | Fresh migrations, application lifespan, and schema checks passed; `/health`, `/api/version`, and `/openapi.json` returned HTTP 200 |
| History backup | Git verified a complete bundle; SHA-256 recorded locally |

The PostgreSQL skip is the SQLite-only fallback test. PostgreSQL locking tests ran. The agent suite's subprocess fixtures required host permissions in this environment; the complete suite passed with those permissions.

Agent and backend checks remain applicable because their application source is unchanged. The earlier standalone simulated UI was removed, along with its dedicated tests and screenshots; it is not part of this checkout's presentation.

## Environment and local evidence

- Node.js 22.23.1, npm 10.9.8
- Dashboard Vitest 4.1.10; Playwright 1.61.1 with installed Chromium 131.0.6778.33
- Python 3.12.3, pytest 7.4.4, PostgreSQL 16.15
- Test databases were private temporary instances removed by the harness. The API smoke used a fresh instance and `BLUEPRINT_PROCESS_ROLE=api`.
- Local logs and smoke sources are retained in ignored `.scratch/portfolio/`. Restoration logs use the `ui-restoration-` prefix.

## Reproduce

From the repository root after installing the respective dependencies:

```bash
npm run typecheck --workspace admin-ui
npm run lint --workspace admin-ui
npm run test:run --workspace admin-ui -- --maxWorkers=2
npm run typecheck --prefix agent
npm test --prefix agent -- --maxWorkers=2 --minWorkers=1

bash scripts/test-backend.sh testing/test_task_capabilities.py testing/test_task_locking.py testing/test_release_serving_observation.py
bash scripts/test-backend.sh testing/test_audit_finding_class_guards.py -k long_lived
python3 -m pytest scripts/test_export_portfolio.py -q
```

The normal backend harness uses Docker; this host used the provisioned `--sandbox-postgres --no-testmon` route. Backend startup and dashboard authentication are described in [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Remaining verification

A complete authenticated dashboard workflow, live model task, GitHub mutation, cloud provisioning, and deployment require configured identities and infrastructure. The browser check above covers the existing entry point and sign-in screen. The full backend suite and frontend production bundle remain with the existing promotion CI.

Existing migrations, CI workflows, and the proprietary license are unchanged. GitHub publication and replacement of the original published history have not been performed.
