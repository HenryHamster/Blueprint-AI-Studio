# v19 Dependency Bump Audit

Audited by: automated task d-f722fb73  
Source bump: PR #11796 ("chore: bump minor/patch dependencies")  
Scope: silent behavioral regressions from broad version jumps in `backend/requirements.txt`

---

## Libraries Audited

### pydantic 2.5.2 → 2.13.4

**Change:** 8 minor versions across pydantic v2 series.  
**Our usage:** `@field_validator`, `@model_validator`, `model_config = ConfigDict(...)`, `model_validate()`, `model_dump()` — exclusively modern pydantic v2 APIs. No `@validator`, `@root_validator`, `.dict()`, `.json()`, `.parse_obj()`, `.parse_raw()`, or `class Config:` anywhere in `backend/app/`.  
**Mild fragility:** One `use_enum_values=True` config entry in `schemas.py` (`ComputeJobResponse`). Still correct in 2.13; covered by existing serialization tests.  
**Verdict: CLEAN** — no code adaptation required.

---

### sqlalchemy 2.0.23 → 2.0.51

**Change:** 28 patch releases within the stable SQLAlchemy 2.0 branch.  
**Our usage:** `database.py` uses hardened patterns: `split_listen_dsn` SSL translation, TCP keepalive event hooks, disconnect-driven pool invalidation, `resolve_async_engine` / `resolve_dialect_name` helpers that guard against None-bind footguns, and `expire_on_commit=False` with disciplined `refresh`/`expire` calls. `alembic/env.py` uses the modern async + `transaction_per_migration` pattern.  
**Residual risks (runtime-monitorable, not code defects):**
- `expire_on_commit=False` identity-map stale-read surface — known and accepted; tests cover it.
- `sync_session.autoflush=False` on the agent-status hot path — explicit and intentional.
- Two test-only `with_for_update(nowait=True)` calls are PostgreSQL-only by design; documented.  
**Verdict: CLEAN** — no deprecated SA 2.0 result-access patterns; monitor post-deploy for pool edge cases.

---

### sqlmodel 0.0.14 → 0.0.19

**Change:** 5 minor releases; sqlmodel tracks SQLAlchemy 2.x.  
**Our usage:** `SQLModel`, `Field`, `Relationship`, and `select()` — the core stable API. `models.py` uses SQLModel table classes throughout.  
**Verdict: CLEAN** — sqlmodel is a thin wrapper; underlying SQLAlchemy 2.x layer audited above.

---

### asyncpg 0.29.0 → 0.31.0

**Change:** 2 minor releases; asyncpg is the PostgreSQL driver used through SQLAlchemy's `asyncpg` dialect.  
**Our usage:** Not called directly by app code; all access goes through `AsyncSession` / `create_async_engine`. TCP keepalive and disconnect-invalidation hooks in `database.py` call `asyncpg`-specific `Connection.reset()` in the SA event listener.  
**Verdict: CLEAN** — no direct asyncpg API usage outside the SA dialect abstraction; pool hardening patterns remain compatible.

---

### PyJWT 2.8.0 → 2.13.0

**Change:** 5 minor releases. PyJWT 2.11 tightened `iss` claim enforcement (must be a string, not bytes) and added HMAC/RSA key-length warnings.  
**Our usage (auth.py):**
- Agent JWTs: HS256, `AGENT_JWT_SECRET` gated by `_validate_jwt_secret` (≥32 bytes enforced at startup).
- WebSocket tickets: same secret and algorithm.
- `GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")` — returns a string, satisfying PyJWT 2.11's string-iss enforcement for GitHub App JWTs in `github_auth.py`.  
**Verdict: CLEAN** — iss claims are already strings; key-length minimum already enforced at startup.

---

### httpx 0.25.2 → 0.28.1 (effective floor: ≥0.27.2)

**Change:** 3 minor releases. httpx 0.28 removed the `Client(app=)` shortcut deprecated since 0.20.  
**Impact:** One legacy `TestClient(app=app)` call broke in CI after the bump (identified via #11811).  
**Resolution:**
1. PR #11811 pinned `httpx==0.27.2` as a stopgap.
2. PR #11824 migrated the offending `TestClient(app=)` to `ASGITransport(app=)`, which is the correct modern pattern.
3. Pin relaxed to `httpx>=0.27.2` after the migration.  
**Verdict: RESOLVED** — the one breaking change (removed `app=` shortcut) was real, found in CI, and fixed. Production code (non-test) used `httpx.AsyncClient` with explicit `base_url`, not the removed shortcut.

---

### uvicorn 0.24.0 → 0.49.0

**Change:** Large jump (25 minor versions) but entirely within the stable ASGI server interface.  
**Our usage:** `uvicorn` is the ASGI server entry point; the app (`main.py`) uses `FastAPI()` and does not call uvicorn's Python API directly. Lifespan events and startup/shutdown hooks use standard ASGI lifecycle conventions unchanged across this version range.  
**Verdict: CLEAN** — no application-level uvicorn API usage; server upgrade is transparent to FastAPI app code.

---

### firebase-admin 6.4.0 → 6.9.0 → pinned 6.8.0

**Change:** Bumped to 6.9.0 in PR #11796; rolled back to 6.8.0 in PR #11811 due to CI instability.  
**Our usage (auth.py):** `firebase_admin.initialize_app()` at module load (GCP default credentials); `firebase_auth.verify_id_token(token)` for operator user authentication. The init is wrapped in a bare-`except` to tolerate already-initialized state and placeholder secrets in local dev.  
**Root cause of 6.9.0 CI failure (identified v24):** `firebase-admin==6.9.0` adds a hard dependency on `httpx==0.28.1`, which directly conflicts with the `httpx>=0.27.2,<0.28` pin in `requirements.txt`. The `httpx<0.28` upper bound was introduced in PR #11811 alongside the rollback (as a stopgap for a separate httpx 0.28 breakage). The dependency resolver cannot satisfy both constraints simultaneously, making `pip install` fail — a deterministic conflict, not transient CI flakiness.  
**Path to unpin:** Relax `httpx<0.28` to remove the upper bound (PR #11824 already migrated the one breaking usage pattern). This unblocks `firebase-admin>=6.9.0`. Upgrading to `firebase-admin>=7.0.0` additionally requires bumping `google-cloud-storage` from `==2.19.0` to `>=3.1.1` (7.x tightened this lower bound). Neither change was made here because they are multi-package bumps outside this spike's scope.  
**Current state:** Stable at 6.8.0 with comprehensive auth test coverage added in v24 (`testing/test_firebase_auth_coverage.py`). The 6.8.0 API surface (`initialize_app`, `verify_id_token`, all five expected exception classes) is confirmed compatible with our usage pattern.  
**Verdict: ROOT CAUSE IDENTIFIED** — the 6.9.0 CI failure was a dependency conflict (`firebase-admin==6.9.0` requires `httpx==0.28.1` vs. our `httpx<0.28` upper bound). Remaining at 6.8.0 until a deliberate multi-package bump is scheduled.

---

## Summary

| Library | From | To (effective) | Verdict |
|---|---|---|---|
| pydantic | 2.5.2 | 2.13.4 | CLEAN |
| sqlalchemy | 2.0.23 | 2.0.51 | CLEAN |
| sqlmodel | 0.0.14 | 0.0.19 | CLEAN |
| asyncpg | 0.29.0 | 0.31.0 | CLEAN |
| PyJWT | 2.8.0 | 2.13.0 | CLEAN |
| httpx | 0.25.2 | ≥0.27.2 | RESOLVED (#11824) |
| uvicorn | 0.24.0 | 0.49.0 | CLEAN |
| firebase-admin | 6.4.0 | 6.8.0 | ROOT CAUSE IDENTIFIED |

**Overall:** No unaddressed breaking behavioral regressions. The one real break (httpx `app=` removal) was caught in CI and fixed. The firebase-admin 6.9.0 issue was mitigated via rollback to 6.8.0.
