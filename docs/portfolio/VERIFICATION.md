# Verification record

Verified on **2026-10-09 (America/Los_Angeles)** during the portfolio cleanup. The operational source baseline was `4d83283bde31b8ca7142700224883dfe8607863d`; changes add the standalone demo, snapshot exporter, and presentation cleanup. The exported checkout's `SNAPSHOT.json` records its exact committed source.

## Executed checks

| Check | Result |
| --- | --- |
| Dashboard types, including demo configuration | `npm run typecheck --workspace admin-ui` passed |
| Full dashboard unit suite | **255 files, 4,737 tests passed**; includes five demo workflow tests |
| Full dashboard lint | **0 errors**, 591 existing warnings; new demo source/configuration lint clean |
| Agent locked dependency installation and types | `npm ci --prefix agent` and `npm run typecheck --prefix agent` passed |
| Full agent unit suite | **131 files, 6,615 tests passed** |
| Selected backend tests on fresh PostgreSQL | **106 passed, 1 skipped**: capabilities, task locking, serving observation |
| Documentation guard | **1 passed**: revised documentation references no registered retired code paths |
| Snapshot-export tests | **5 passed**: resolved Git identity, provenance, revision selection, content preservation, and refusal to overwrite other work |
| Exporter Python lint | Ruff passed |
| Demo static build | Passed; generated artifact includes no operational dashboard or auth entry point |
| Built-demo browser checks | Submission, recovery, review, keyboard tabs, reload reset, all views at three viewport widths, static subdirectory hosting, and no API/external requests passed |
| Real backend API startup smoke | Full fresh migration chain, application lifespan, and schema checks passed; `/health`, `/api/version`, and `/openapi.json` returned **HTTP 200**; API role remained a non-leader |
| Presentation links and diff whitespace | Checked locally |
| History backup | Git verified a complete bundle; SHA-256 recorded locally |

The PostgreSQL skip is the SQLite-only no-op fallback test; PostgreSQL-specific locking tests ran. The agent suite's subprocess fixtures require host permissions in this execution environment. The first restricted run was interrupted after process permission failures, then the complete suite passed with those permissions. No assertions were weakened or tests disabled.

## Environment and local evidence

- Node.js **22.23.1**, npm **10.9.8**
- Dashboard Vitest **4.1.10**; Playwright **1.61.1**, using installed Chromium **131.0.6778.33**
- Python **3.12.3**, pytest **7.4.4**, PostgreSQL **16.15**
- Test databases used private temporary PostgreSQL instances removed by the harness. The API smoke used a separate fresh instance, non-production configuration, and `BLUEPRINT_PROCESS_ROLE=api`.
- Local logs and the API smoke source/result are retained in ignored `.scratch/portfolio/`. Curated screenshots are in [`images/`](images/).

## Reproduce the relevant checks

From the repository root after installing the respective locked dependencies:

```bash
npm run demo:test
npm run demo:check
npm run typecheck --workspace admin-ui
npm run lint --workspace admin-ui
npm run test:run --workspace admin-ui -- --maxWorkers=2
npm run typecheck --prefix agent
npm test --prefix agent -- --maxWorkers=2 --minWorkers=1

bash scripts/test-backend.sh testing/test_task_capabilities.py testing/test_task_locking.py testing/test_release_serving_observation.py
bash scripts/test-backend.sh testing/test_audit_finding_class_guards.py -k long_lived
python3 -m pytest scripts/test_export_portfolio.py -q
```

The normal backend harness uses Docker; this host used the provisioned `--sandbox-postgres --no-testmon` route. Browser installation and overrides are documented in the [demo guide](DEMO.md). Backend startup can be exercised using the API-only development setup in [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Boundaries

This verifies the demo, selected backend behavior, source types, unit suites, and a fresh API boot. It does not establish a successful live model task, GitHub mutation, cloud provisioning, production deployment, or end-to-end operational dashboard session. Those require configured identities and separate execution evidence. The full database-backed backend suite and full operational frontend bundle were left to the existing promotion CI.

Existing migration files and CI workflows are unchanged. The current proprietary license is unchanged. Public publication and replacement of the original GitHub history have not been performed.
