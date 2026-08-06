"""FastAPI application entry point for the Blueprint orchestrator.

Constructs the ASGI ``app``, wires middleware (CORS, rate limiting, request
IDs) and exception handlers, and registers every API router from
``app.routers``. Background workers (orchestrator, reaper, cleanup) are
started and stopped through the ``lifespan`` context manager imported from
``app.lifecycle``. On startup it validates that the live database is at
``EXPECTED_SCHEMA_REVISION`` (kept in lockstep with the latest Alembic
migration) and records deployment state for new-deploy / rollback detection.
"""

import hmac
import logging
import os
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, Any, Callable
from fastapi import (
    FastAPI,
    Depends,
    Header,
    HTTPException,
    Request,
)
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from slowapi.errors import RateLimitExceeded
from app.csrf import CSRFMiddleware
from app.frontend_static import (
    install_frontend_routes,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from app.database import get_session, AsyncSessionLocal
from app.models import (
    Task,
    TaskStatus,
    DeploymentState,
    DeploymentStatus,
)
from app.compute import provision_agent_vm  # noqa: F401  (re-exported for test patches; see main.py:108 docstring)
from app.deployed_sha import resolve_deployed_sha
from app.auth import get_current_user, get_current_user_optional, verify_agent_jwt
from app.admin_helpers import _get_user_project
from app.schemas import ArtifactDownloadResponse
from app.admin import router as admin_router
from app.routers.admin_projects import router as admin_projects_router
from app.routers.admin_project_strategic_session import router as admin_project_strategic_session_router
from app.routers.admin_settings import router as admin_settings_router
from app.routers.admin_migration_exemptions import (
    router as admin_migration_exemptions_router,
)
from app.routers.admin_debug import router as admin_debug_router
from app.routers.admin_pool_accounts import router as admin_pool_accounts_router
from app.routers.admin_project_config import router as admin_project_config_router
from app.routers.admin_project_tools import router as admin_project_tools_router
from app.routers.admin_project_tools import asset_router as admin_project_tools_asset_router
from app.routers.admin_warm_pool import router as admin_warm_pool_router
from app.routers.admin_runners import router as admin_runners_router
from app.routers.runner import router as runner_router
from app.routers.agent import router as agent_router
from app.routers.agent_github import router as agent_github_router
from app.routers.agent_ci_compliance import router as agent_ci_compliance_router
from app.routers.agent_strategic_fetch import router as agent_strategic_fetch_router
from app.routers.agent_strategic_messages import router as agent_strategic_messages_router
from app.strategic_resident_session import router as strategic_resident_session_router
from app.routers.agent_tasks import router as agent_tasks_router
from app.routers.agent_warm_continuity import router as agent_warm_continuity_router
from app.efficiency_snapshot import agent_router as efficiency_snapshot_agent_router
from app.routers.agent_credentials import router as agent_credentials_router
from app.routers.agent_compute_jobs import router as agent_compute_jobs_router
from app.routers.agent_memory_consolidation import router as agent_memory_consolidation_router
from app.routers.agent_strategic_memory import router as agent_strategic_memory_router
from app.routers.admin_compute_jobs import router as admin_compute_jobs_router
from app.routers.admin_strategic_memory import router as admin_strategic_memory_router
from app.routers.github import router as github_router
from app.routers.projects import router as projects_router
from app.routers.directions import router as directions_router
from app.routers.deploy import router as deploy_router
from app.routers.admin_users import router as admin_users_router
from app.routers.admin_notifications import router as admin_notifications_router
from app.routers.admin_analytics import router as admin_analytics_router
from app.routers.admin_digest import router as admin_digest_router
from app.routers.admin_pipeline_observability import (
    metrics_router as admin_metrics_router,
)
from app.routers.admin_activity import router as admin_activity_router
from app.routers.admin_health import router as admin_health_router
from app.routers.admin_evergreen import router as admin_evergreen_router
from app.routers.admin_orchestrator_errors import (
    router as admin_orchestrator_errors_router,
)
from app.routers.admin_scope_measurement import router as admin_scope_measurement_router
from app.routers.agent_status import router as agent_status_router
from app.routers.admin_onboard import router as admin_onboard_router
from app.routers.admin_agent_ops import router as admin_agent_ops_router
from app.routers.admin_auth import router as admin_auth_router
from app.routers.admin_agent_ops import _bump_size_limit  # noqa: F401  re-exported for test patches (test_agent_annotate.py)
from app.routers.admin_acceptance_criteria import router as admin_acceptance_criteria_router
from app.routers.admin_direction_events import router as admin_direction_events_router
from app.routers.admin_state import router as admin_state_router
from app.routers.admin_operator_attention import router as admin_operator_attention_router
from app.routers.admin_service_credentials import (
    router as admin_service_credentials_router,
)
from app.shared_state import limiter
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config
from app import orchestrator as _orch
from app.lifecycle import lifespan
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


# Expected schema revision — must match the latest alembic migration.
# Update this when adding new migrations.
EXPECTED_SCHEMA_REVISION = "0725"

# --- Task Priority Map ---
# Maps priority string labels to integer values stored on Task.priority.
TASK_PRIORITY_MAP: Dict[str, int] = {"high": 100, "medium": 50, "low": 0}

# --- VM teardown lifecycle (extracted to app.vm_lifecycle) ---
# The GCP VM-count cache, the orchestrator-initiated VM-delete tracker, and the
# VM-teardown helpers were extracted to app.vm_lifecycle (P5 service-locator
# decomposition) so the orchestrator/reaper/routers can import them without a
# deferred `from app.main import ...` round-trip through the FastAPI app
# entrypoint. Re-exported here for backward compatibility — callers may still
# `from app.main import _ensure_vm_deleted` and tests may still reference the
# names below, but new code should import from app.vm_lifecycle directly.
# The module itself is imported as _vm_lifecycle so the endpoints below read
# the live, rebound counters via `_vm_lifecycle._last_gcp_vm_count` (a
# `from ... import` binding would go stale when _ensure_vm_deleted rebinds the
# module global).
from app import vm_lifecycle as _vm_lifecycle
from app.vm_lifecycle import (  # noqa: F401  re-exported for back-compat (callers + tests patch app.main.X)
    is_orchestrator_initiated_delete,
    _ensure_vm_deleted,
    fire_and_forget_vm_delete,
    schedule_deferred_backlog_teardown,
    _get_actual_gcp_vm_count,
    _orchestrator_deleted_vms,
    _background_vm_deletes,
)


# --- Max Plan Account Pool ---
# Pool state and logic extracted to max_plan_pool.py. Re-exported here for
# backward compatibility with callers (orchestrator, tests) that import from
# app.main. The 2026-05-03 ruff --fix sweep removed the public-API helpers
# below as "unused" because main.py doesn't call them directly — but tests
# patch them via app.main.X (see test_pipeline_reliability.py:105+) and
# orchestrator code imports them via the same path. noqa: F401 pins them so
# a future --fix can't repeat the regression.
from app.max_plan_pool import (  # noqa: F401
    _account_by_id,
    _max_plan_pool_lock,
    _all_accounts,
    _max_plan_pools,
    load_max_plan_pool,
    _extract_task_model,
    select_max_plan_account,
    release_max_plan_account,
    recheck_disabled_accounts,
)

# --- Deployment State Management ---

# Current revision from Cloud Run (set on startup)
_current_revision: str = os.getenv("K_REVISION", "local-dev")
# Default SHA; overwritten at startup by resolve_deployed_sha() when running on
# Cloud Run (DEPLOY_SHA is never set by the deploy pipeline, so this is "unknown"
# until _init_deployment_state resolves the real serving SHA from the revision).
_current_sha: str = os.getenv("DEPLOY_SHA", "unknown")


# Webhook dedup state moved to app.routers.github


async def _verify_encryption_config():
    """Validate encryption configuration at startup.

    In a production scope (Cloud Run's ``K_REVISION`` or explicit
    ``BLUEPRINT_DEPLOYMENT_MODE=local-production``): require
    ``GITHUB_PAT_ENCRYPTION_KEY`` to be configured — raise ``RuntimeError`` so
    the deploy fails immediately instead of silently storing credentials in
    plaintext.

    In development/test (``K_REVISION`` not set): warn when both the encryption
    key and ``ALLOW_PLAINTEXT_CREDENTIALS`` are absent, so developers know to
    either configure a key or set the opt-in flag.

    **Key-continuity probe** (when encryption is active):
    A per-environment continuity reference ciphertext is persisted in
    ``DeploymentState.runtime_config_json["encryption_continuity_reference"]``.
    On first boot the reference is minted with the current key and stored; on
    subsequent boots the stored reference is verified against the current key
    and boot fails closed on mismatch (wrong-key rotation / misconfiguration).
    """
    from app.field_encryption import (
        ENCRYPTION_KEY_ENV,
        ALLOW_PLAINTEXT_CREDENTIALS_ENV,
        is_encryption_active,
        probe_key_continuity,
    )
    from app.required_runtime_env import (
        LOCAL_DEPLOYMENT_MODE_ENV,
        LOCAL_PRODUCTION_DEPLOYMENT_MODE,
        _verify_required_env,
        active_scopes,
    )

    # Presence enforcement is delegated to the declarative manifest: in either
    # production scope it raises RuntimeError when GITHUB_PAT_ENCRYPTION_KEY is
    # absent. This keeps a single machine-readable source of truth for the boot
    # env contract; value-validity specialisation stays here.
    _verify_required_env()

    local_production = "local_production" in active_scopes()
    if local_production and os.getenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV):
        raise RuntimeError(
            "[STARTUP] ALLOW_PLAINTEXT_CREDENTIALS is forbidden when "
            f"{LOCAL_DEPLOYMENT_MODE_ENV}={LOCAL_PRODUCTION_DEPLOYMENT_MODE}; "
            "configure GITHUB_PAT_ENCRYPTION_KEY instead."
        )

    if is_encryption_active():
        from app.database import AsyncSessionLocal
        from app.models import DeploymentState

        async with AsyncSessionLocal() as session:
            state = await session.get(DeploymentState, 1)
            stored_ref: str | None = None
            if state is not None and state.runtime_config_json:
                stored_ref = state.runtime_config_json.get(
                    "encryption_continuity_reference"
                )

            if stored_ref is not None:
                # Verify existing reference — raises RuntimeError on wrong key.
                probe_key_continuity("startup", reference_ciphertext=stored_ref)
            else:
                # First boot with encryption — mint and persist a continuity
                # reference so subsequent boots can detect wrong-key rotation.
                new_ref = probe_key_continuity("startup")
                if new_ref is not None:
                    if state is None:
                        state = DeploymentState(id=1)
                    if state.runtime_config_json is None:
                        state.runtime_config_json = {}
                    state.runtime_config_json["encryption_continuity_reference"] = new_ref
                    session.add(state)
                    await session.commit()
        return

    production_scope = bool(os.getenv("K_REVISION")) or local_production
    if production_scope:
        # Presence already passed (_verify_required_env would have raised), so the
        # key is set but not a valid Fernet key — the value-validity failure.
        msg = (
            f"[STARTUP] {ENCRYPTION_KEY_ENV} is set but is not a valid Fernet key "
            "— field encryption is unavailable. This is a fatal configuration "
            "error in production. Set the environment variable to a valid Fernet "
            "key before deploying."
        )
        logger.error(msg)
        raise RuntimeError(msg)
    else:
        allow_plaintext = os.getenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV)
        if not allow_plaintext:
            logger.warning(
                "[STARTUP] %s is not set and %s is not set — "
                "field encryption will be a no-op. "
                "Set %s=true if this is intentional for development, "
                "or configure %s for production-grade encryption.",
                ENCRYPTION_KEY_ENV,
                ALLOW_PLAINTEXT_CREDENTIALS_ENV,
                ALLOW_PLAINTEXT_CREDENTIALS_ENV,
                ENCRYPTION_KEY_ENV,
            )
        else:
            logger.warning(
                "[STARTUP] %s is not set but %s=true — "
                "credentials will be stored in plaintext (development mode).",
                ENCRYPTION_KEY_ENV,
                ALLOW_PLAINTEXT_CREDENTIALS_ENV,
            )


async def _verify_schema_version():
    """Verify the database schema matches the expected alembic revision.
    Fail fast on mismatch to prevent runtime errors from missing columns."""
    from sqlalchemy import text as sa_text

    async with AsyncSessionLocal() as session:
        try:
            result = await session.execute(
                sa_text("SELECT version_num FROM alembic_version LIMIT 1")
            )
            row = result.first()
            if not row:
                logger.warning(
                    "[STARTUP] No alembic_version row — database may be uninitialized"
                )
                return
            # Exact equality required — `0055_a` != `0055_b` would slip past startswith.
            # Raise unconditionally so dev environments catch mismatches the same way Cloud Run does.
            if row[0] != EXPECTED_SCHEMA_REVISION:
                msg = (
                    f"Schema version mismatch: database at '{row[0]}', "
                    f"code expects '{EXPECTED_SCHEMA_REVISION}'"
                )
                logger.error(f"[STARTUP] {msg}")
                raise RuntimeError(msg)
        except Exception as e:
            if "alembic_version" in str(e).lower():
                logger.warning(
                    "[STARTUP] alembic_version table not found — skipping schema check"
                )
                return
            raise


async def _verify_schema_drift():
    """Verify the live DB has every table/column the models require.

    Complements _verify_schema_version: the alembic version number can be exactly
    correct while the actual schema is MISSING objects — a back-merge renumber
    leaves ``alembic_version`` ahead of the real schema, so the version check
    passes but a later query hits a column that does not exist (prod incident
    2026-06-04, a silent multi-hour outage). Failing fast here turns that cryptic
    ``UndefinedColumnError`` deep in startup into one clear, specific message.

    Defensive: an uninitialized DB (no ``alembic_version``) is left to
    _verify_schema_version's early return, and a reflection error is logged and
    skipped rather than crashing boot on an infra hiccup — only a genuine missing
    object raises. The ``test_no_schema_drift_against_migrated_head`` invariant
    keeps this from ever false-positiving by asserting models match the migrated
    schema in CI, so a fatal check here is safe.
    """
    from sqlalchemy import inspect as sa_inspect

    from app.deploy_smoketest import reflect_schema_drift

    def _reflect(sync_conn):
        # Reflect only once the schema is actually migrated; an uninitialized DB
        # is handled by _verify_schema_version (parity), not flagged as drift.
        if "alembic_version" not in sa_inspect(sync_conn).get_table_names():
            return None
        return reflect_schema_drift(sync_conn)

    try:
        async with AsyncSessionLocal() as session:
            connection = await session.connection()
            drift = await connection.run_sync(_reflect)
    except Exception as e:
        logger.warning(f"[STARTUP] could not reflect schema for drift check: {e}")
        return

    if drift:
        msg = (
            "Schema drift — the database is missing objects the code requires "
            "(the alembic version can be correct while the real schema is short):"
            "\n  " + "\n  ".join(drift)
        )
        logger.error(f"[STARTUP] {msg}")
        raise RuntimeError(msg)


async def _best_effort_deploy_state_commit(session) -> bool:
    """Commit a DeploymentState bookkeeping change, degrading on DB contention.

    Deploy-detection (revision-change / rollback / new-deploy monitoring) writes
    the singleton ``DeploymentState`` row. Under a multi-instance deploy or general
    DB contention that row UPDATE can hit a lock timeout — and committing it
    UNGUARDED in lifespan startup crashed the boot ("Application startup failed.
    Exiting."), which FAILED the deploy and pinned prod on the old revision
    (incident 2026-06-04). This bookkeeping is non-critical for serving and
    reconciles on the next boot / health tick, so a contended write degrades (log
    + rollback + continue) instead of aborting startup. Returns True on success.
    """
    from sqlalchemy.exc import SQLAlchemyError

    try:
        await session.commit()
        return True
    except SQLAlchemyError:
        logger.warning(
            "[DEPLOY] deploy-state write skipped (DB contention?) — starting anyway; "
            "deploy state reconciles on the next boot / health tick",
            exc_info=True,
        )
        try:
            await session.rollback()
        except SQLAlchemyError:
            logger.exception("[DEPLOY] rollback after failed deploy-state write failed")
        return False


async def _init_deployment_state():
    """Check/initialize deployment state on startup. Detects new deploys and rollbacks."""
    # Resolve the real serving SHA from the running Cloud Run revision before any
    # use of _current_sha (deploy-detection, the singleton row, /api/version all
    # read it). Best-effort: returns None off Cloud Run or on any failure, in
    # which case we keep the existing default ("unknown") and never block startup.
    global _current_sha
    resolved = await resolve_deployed_sha()
    if resolved:
        _current_sha = resolved

    async with AsyncSessionLocal() as session:
        state = await session.get(DeploymentState, 1)

        if not state:
            # First boot — create the singleton row
            # Wrapped in try/except to handle race between multiple Cloud Run instances
            try:
                state = DeploymentState(
                    id=1,
                    current_revision=_current_revision,
                    current_sha=_current_sha,
                    deployed_at=_utcnow(),
                    status=DeploymentStatus.STABLE,
                    last_known_good_revision=_current_revision,
                    last_known_good_sha=_current_sha,
                )
                session.add(state)
                await session.commit()
                logger.info(
                    f"[DEPLOY] First boot — initialized deployment state (revision={_current_revision})"
                )
                return
            except Exception:
                await session.rollback()
                # Another instance likely inserted first — fetch the row they created
                state = await session.get(DeploymentState, 1)
                if not state:
                    raise  # Genuine error, not a race

        # Restore persisted runtime config values so settings survive restarts.
        # Shared with the periodic cross-instance sync (see sync_runtime_config_loop).
        try:
            from app.shared_state import sync_runtime_config_from_state

            restored = await sync_runtime_config_from_state(state)
            if restored:
                logger.info(f"[DEPLOY] Restored runtime config from DB: {restored}")
        except Exception as e:
            logger.warning(
                f"[DEPLOY] Could not restore runtime config (migration pending?): {e}",
                exc_info=True,
            )
        # Restore circuit breaker state — prevents deploy from clearing tripped breakers
        try:
            if state.circuit_breaker_states:
                for proj_id, breaker_data in state.circuit_breaker_states.items():
                    restored = {**breaker_data}
                    if restored.get("opened_at") and isinstance(
                        restored["opened_at"], str
                    ):
                        restored["opened_at"] = datetime.fromisoformat(
                            restored["opened_at"]
                        )
                    _shared_state.circuit_breakers[proj_id] = restored
                logger.info(
                    f"[DEPLOY] Restored circuit breaker state for {len(state.circuit_breaker_states)} projects"
                )
        except Exception as e:
            logger.warning(
                f"[DEPLOY] Could not restore circuit breaker state: {e}", exc_info=True
            )

        # Restore zone health — prevents retry storms on exhausted zones after restart
        try:
            if state.zone_health_state:
                from app.compute import _zone_health, _zone_lock, _zone_failures
                from datetime import timezone as tz

                with _zone_lock:
                    for zone, data in state.zone_health_state.items():
                        if not isinstance(data, dict):
                            continue
                        restored = {}
                        for k, v in data.items():
                            if isinstance(v, str) and k in (
                                "last_success",
                                "last_failure",
                                "cooldown_expires_at",
                            ):
                                try:
                                    restored[k] = datetime.fromisoformat(v)
                                except ValueError:
                                    if k == "cooldown_expires_at":
                                        fallback = datetime.now(tz.utc) + timedelta(
                                            minutes=30
                                        )
                                        logger.warning(
                                            f"[DEPLOY] Corrupted cooldown_expires_at for zone {zone} ({v!r}); failing safe to {fallback.isoformat()}"
                                        )
                                        restored[k] = fallback
                                    elif k == "last_failure":
                                        fallback = datetime.now(tz.utc)
                                        logger.warning(
                                            f"[DEPLOY] Corrupted last_failure for zone {zone} ({v!r}); failing safe to {fallback.isoformat()}"
                                        )
                                        restored[k] = fallback
                                    else:
                                        logger.warning(
                                            f"[DEPLOY] Skipping corrupted zone health key {k} for zone {zone}: {v!r}"
                                        )
                                        continue
                            elif k == "recent_outcomes" and isinstance(v, list):
                                from collections import deque as _deque
                                from app.compute import ZONE_HISTORY_WINDOW

                                valid = [e for e in v if isinstance(e, bool)]
                                if len(valid) < len(v):
                                    logger.warning(
                                        f"[DEPLOY] Filtered {len(v) - len(valid)} non-boolean element(s) from recent_outcomes for zone {zone}"
                                    )
                                restored[k] = _deque(
                                    valid[-ZONE_HISTORY_WINDOW:],
                                    maxlen=ZONE_HISTORY_WINDOW,
                                )
                            elif k == "consecutive_failures":
                                if isinstance(v, int):
                                    restored[k] = v
                                else:
                                    logger.warning(
                                        f"[DEPLOY] consecutive_failures for zone {zone} is not an int ({v!r}), defaulting to 0"
                                    )
                                    restored[k] = 0
                            elif k == "last_failure_reason":
                                if v is None or isinstance(v, str):
                                    restored[k] = v
                                else:
                                    logger.warning(
                                        f"[DEPLOY] last_failure_reason for zone {zone} is not a string ({v!r}), defaulting to None"
                                    )
                                    restored[k] = None
                            else:
                                restored[k] = v
                        _zone_health[zone] = restored
                        # Restore _zone_failures for zones still in cooldown
                        if restored.get("cooldown_expires_at") and restored[
                            "cooldown_expires_at"
                        ] > datetime.now(tz.utc):
                            _zone_failures[zone] = restored.get(
                                "last_failure", datetime.now(tz.utc)
                            )
                logger.info(
                    f"[DEPLOY] Restored zone health state for {len(state.zone_health_state)} zones"
                )
        except Exception as e:
            logger.warning(
                f"[DEPLOY] Could not restore zone health state: {e}", exc_info=True
            )

        # Restore pause/health streaks
        try:
            if state.zone_health_state:
                import app.deploy_health as _dh

                _dh._restore_health_streaks(state.zone_health_state)
                if (
                    _dh._pause_failure_streak
                    or _dh._health_recovery_history
                    or _dh._last_auto_resume_at
                ):
                    logger.info(
                        f"[DEPLOY] Restored health streaks: pause_failures={_dh._pause_failure_streak}, "
                        f"recovery_history={list(_dh._health_recovery_history)}, "
                        f"last_auto_resume_at={_dh._last_auto_resume_at}"
                    )
        except Exception as e:
            logger.warning(
                f"[DEPLOY] Could not restore health streaks: {e}", exc_info=True
            )

        # Restore persistent anomaly state — preserves occurrence counts across restarts
        try:
            pstate = getattr(state, "persistent_anomaly_state", None)
            if pstate and isinstance(pstate, dict):
                raw_anomalies = pstate.get("persistent_anomalies", {})
                for proj_id, proj_anomalies in raw_anomalies.items():
                    restored_proj: dict = {}
                    for k, v in proj_anomalies.items():
                        if not isinstance(v, dict):
                            restored_proj[k] = v
                            continue
                        entry: dict = {}
                        for fk, fv in v.items():
                            # first_seen/last_seen are ISO strings everywhere they're
                            # written and compared (anomaly_detection.py). Coerce any
                            # pre-existing datetime back to isoformat so legacy
                            # persisted pods don't crash the stale-cutoff comparison.
                            if fk in ("first_seen", "last_seen") and isinstance(
                                fv, datetime
                            ):
                                entry[fk] = fv.isoformat()
                            else:
                                entry[fk] = fv
                        restored_proj[k] = entry
                    _shared_state.persistent_anomalies[proj_id] = restored_proj
                raw_adjustments = pstate.get("anomaly_adjustments", {})
                if raw_adjustments:
                    _shared_state.anomaly_adjustments.update(raw_adjustments)
                # Re-apply active adjustments so max_concurrent_vms stays reduced
                if _shared_state.anomaly_adjustments.get("active"):
                    adj = _shared_state.anomaly_adjustments
                    pre_vms = adj.get("pre_adjustment_max_vms")
                    if pre_vms is not None:
                        _shared_state.runtime_config["max_concurrent_vms"] = max(
                            2, pre_vms // 2
                        )
                    logger.info(
                        "[DEPLOY] Re-applied active anomaly adjustments: "
                        f"max_vms={_shared_state.runtime_config['max_concurrent_vms']}, "
                        f"failure_gate={adj.get('failure_gate_threshold')}"
                    )
                logger.info(
                    f"[DEPLOY] Restored anomaly state for {len(raw_anomalies)} projects"
                )
        except Exception as e:
            logger.warning(
                f"[DEPLOY] Could not restore anomaly state: {e}", exc_info=True
            )

        # Seed task_results from DB so fast-recovery has data after restart
        # (in-memory deque is empty otherwise, blocking recovery for ~2h).
        if _shared_state.anomaly_adjustments.get("active"):
            try:
                from app.anomaly_detection import seed_task_results_from_db

                seeded = await seed_task_results_from_db(session)
                logger.info(
                    f"[DEPLOY] Seeded {seeded} task_results from DB for fast-recovery"
                )
            except Exception as e:
                logger.warning(
                    f"[DEPLOY] Could not seed task_results from DB: {e}", exc_info=True
                )

        # Restore direction failure streaks — holds survive restarts silently
        try:
            dstate = getattr(state, "direction_streak_state", None)
            if dstate and isinstance(dstate, dict):
                for direction_id, streak in dstate.items():
                    restored_streak: dict = {}
                    for k, v in streak.items():
                        if isinstance(v, str) and k in ("last_failure_at", "held_at"):
                            try:
                                restored_streak[k] = datetime.fromisoformat(v)
                            except ValueError:
                                restored_streak[k] = v
                        else:
                            restored_streak[k] = v
                    _shared_state.direction_failure_streaks[direction_id] = (
                        restored_streak
                    )
                held_count = sum(1 for s in dstate.values() if s.get("held"))
                logger.info(
                    f"[DEPLOY] Restored direction failure streaks for {len(dstate)} directions "
                    f"({held_count} held)"
                )
        except Exception as e:
            logger.warning(
                f"[DEPLOY] Could not restore direction failure streaks: {e}",
                exc_info=True,
            )

        # Restore pipeline events ring buffer — alert windows survive restarts
        try:
            estate = getattr(state, "pipeline_events_state", None)
            if estate and isinstance(estate, dict):
                from collections import deque as _deque

                events_list = estate.get("events", [])
                restored_events: _deque = _deque(maxlen=500)
                for e in events_list:
                    restored: dict = {}
                    for k, v in e.items():
                        if k == "ts" and isinstance(v, str):
                            try:
                                restored[k] = datetime.fromisoformat(v)
                            except ValueError:
                                restored[k] = v
                        else:
                            restored[k] = v
                    restored_events.append(restored)
                _shared_state.deploy_monitoring["pipeline_events"] = restored_events
                logger.info(f"[DEPLOY] Restored {len(restored_events)} pipeline events")
        except Exception as e:
            logger.warning(
                f"[DEPLOY] Could not restore pipeline events: {e}", exc_info=True
            )

        # Auto-recovery from HALTED after 1 hour cooldown
        if state.status == DeploymentStatus.HALTED:
            try:
                if state.halted_at:
                    elapsed = (_utcnow() - state.halted_at).total_seconds()
                    if elapsed > 3600:  # 1 hour
                        state.rollback_count = 0
                        state.status = DeploymentStatus.MONITORING
                        state.halted_at = None
                        state.deployed_at = _utcnow()
                        # Restore VM capacity so dispatch actually works
                        restored = state.pre_pause_max_vms or int(
                            os.getenv("MAX_CONCURRENT_VMS", "5")
                        )
                        _runtime_config["max_concurrent_vms"] = restored
                        state.pre_pause_max_vms = None
                        session.add(state)
                        await session.commit()
                        logger.warning(
                            f"[DEPLOY] Auto-recovery from HALTED after {elapsed:.0f}s cooldown — max_concurrent_vms restored to {restored}"
                        )
                        await _create_halted_recovery_alerts(session)
                        await session.commit()
                    else:
                        remaining = 3600 - elapsed
                        logger.info(
                            f"[DEPLOY] HALTED — {remaining:.0f}s remaining until auto-recovery"
                        )
            except Exception as e:
                logger.warning(
                    f"[DEPLOY] Could not check HALTED auto-recovery (halted_at column may not exist yet): {e}"
                )

        # Respect paused/halted state — these override the persisted value
        if state.status in (DeploymentStatus.PAUSED, DeploymentStatus.HALTED):
            _runtime_config["max_concurrent_vms"] = 0
            logger.info(
                f"[DEPLOY] max_concurrent_vms forced to 0 — state is {state.status} "
                f"(reason: {state.pause_reason or 'unknown'}). "
                f"Set max VMs > 0 in settings to resume."
            )

        # The deploy-detection writes below touch the DeploymentState singleton.
        # Fail FAST on a contended row (multi-instance deploy / DB stress) so a lock
        # wait can't blow the startup-probe deadline; paired with the best-effort
        # commit below, a contended boot degrades quickly instead of hanging then
        # crashing. Transaction-scoped (reverts on the next commit); the common
        # new-deploy path commits once, so this covers it. No-op off Postgres.
        try:
            from sqlalchemy import text as _sa_text

            await session.execute(_sa_text("SET LOCAL lock_timeout = '2s'"))
        except Exception:
            logger.debug("[DEPLOY] could not set boot lock_timeout (non-postgres?)")

        if state.current_revision == _current_revision:
            # Same revision — normal restart (cold start, scale event, etc.)
            logger.info(
                f"[DEPLOY] Normal restart — same revision {_current_revision}, status={state.status}"
            )
            return

        # Revision changed — is it a new deploy or a rollback?
        if (
            _current_revision == state.last_known_good_revision
            and state.status == DeploymentStatus.MONITORING
        ):
            # We're back on the last known good revision while we were monitoring a new deploy
            # This means the new deploy was rolled back (externally or via Cloud Run)
            state.status = DeploymentStatus.ROLLED_BACK
            state.rolled_back_from_sha = state.current_sha
            state.rollback_reason = (
                "Revision reverted to last known good during monitoring window"
            )
            state.rollback_count += 1
            state.current_revision = _current_revision
            state.current_sha = _current_sha
            state.deployed_at = _utcnow()
            # Snapshot the scalars read after the commit attempts below: a
            # contended commit degrades via rollback inside
            # _best_effort_deploy_state_commit, which expires ``state`` — a
            # subsequent attribute read would raise MissingGreenlet (#14328
            # snapshot-before-rollback pattern).
            rolled_back_from_sha = state.rolled_back_from_sha
            rollback_count = state.rollback_count
            last_known_good_revision = state.last_known_good_revision
            session.add(state)
            await _best_effort_deploy_state_commit(session)

            logger.warning(
                f"[DEPLOY] ROLLBACK DETECTED — reverted from {(rolled_back_from_sha or '')[:8]} to {_current_sha[:8]} "
                f"(consecutive rollbacks: {rollback_count})"
            )

            await _create_rollback_alerts(
                session, rolled_back_from_sha or "", _current_sha
            )
            await _best_effort_deploy_state_commit(session)

            if rollback_count >= 3:
                halted_at = _utcnow()
                state.status = DeploymentStatus.HALTED
                state.halted_at = halted_at
                session.add(state)
                await _best_effort_deploy_state_commit(session)
                logger.error(
                    f"[DEPLOY] HALTED — {rollback_count} consecutive rollbacks, manual intervention required"
                )
                _runtime_config["max_concurrent_vms"] = 0
                await _create_halted_alerts(
                    session,
                    rollback_count,
                    last_known_good_revision,
                    halted_at,
                )
                await _best_effort_deploy_state_commit(session)
            return

        # New deploy detected — update revision tracking. Capture the prior
        # deploy's sha + timestamp first so investigation payloads can compute
        # the prior_deploy_sha..current_sha commit range without a live checkout.
        state.prior_deploy_sha = state.current_sha
        state.current_revision = _current_revision
        state.current_sha = _current_sha
        state.deployed_at = _utcnow()
        state.rolled_back_from_sha = None
        state.rollback_reason = None

        # Only reset monitoring window if not PAUSED/HALTED — those are explicit
        # states that must be cleared by auto-resume or manual action, not by deploys.
        # Resetting monitoring data while paused would lose baseline context.
        if state.status not in (DeploymentStatus.PAUSED, DeploymentStatus.HALTED):
            from app.shared_state import deploy_monitoring

            results = deploy_monitoring["task_results"]
            results_list = list(results)
            # E11: split the deque into 3 chunks and use the median rate as
            # the pre-deploy baseline. A single snapshot lets a transient
            # spike right before deploy poison the whole monitoring window
            # — rollback logic then either over-reads the baseline (skips
            # rollback when the deploy IS bad) or under-reads it (rolls
            # back code that's actually fine). 3-chunk median dampens
            # both directions while still picking up a real degradation.
            if len(results_list) >= 9:
                chunks = 3
                chunk_size = len(results_list) // chunks
                rates: list[float] = []
                for i in range(chunks):
                    start = i * chunk_size
                    end = start + chunk_size if i < chunks - 1 else len(results_list)
                    chunk = results_list[start:end]
                    if not chunk:
                        continue
                    chunk_failures = sum(1 for r in chunk if r[1] == "failure")
                    rates.append(chunk_failures / len(chunk))
                rates.sort()
                # Median of an odd-count list is rates[len // 2].
                deploy_monitoring["pre_deploy_failure_rate"] = rates[len(rates) // 2]
                deploy_monitoring["pre_deploy_failure_rate_samples"] = rates
            elif results_list:
                failures = sum(1 for r in results_list if r[1] == "failure")
                deploy_monitoring["pre_deploy_failure_rate"] = failures / len(
                    results_list
                )
                deploy_monitoring["pre_deploy_failure_rate_samples"] = [
                    deploy_monitoring["pre_deploy_failure_rate"]
                ]
            else:
                # No previous data (idle→active). Use generous baseline to avoid false rollbacks.
                deploy_monitoring["pre_deploy_failure_rate"] = 0.15
                deploy_monitoring["pre_deploy_failure_rate_samples"] = [0.15]
            deploy_monitoring["previous_window_results"] = list(results)
            deploy_monitoring["previous_window_start"] = deploy_monitoring.get(
                "window_start"
            )
            deploy_monitoring["task_results"] = deque(maxlen=100)
            deploy_monitoring["window_start"] = _utcnow()
            state.status = DeploymentStatus.MONITORING
        session.add(state)
        # Snapshot before the commit attempt: a contended commit degrades via
        # rollback inside _best_effort_deploy_state_commit, which expires
        # ``state`` — reading state.status afterwards would raise
        # MissingGreenlet (#14328 snapshot-before-rollback pattern).
        status_snapshot = state.status
        await _best_effort_deploy_state_commit(session)
        logger.info(
            f"[DEPLOY] New deploy detected — revision={_current_revision}, status={status_snapshot}, starting monitoring window"
        )


async def _reconcile_deploy_state_health_tick() -> bool:
    """Periodic leader-only re-detection of the deployed SHA.

    ``DeploymentState.current_sha`` is written only at boot
    (``_init_deployment_state``), and that write is best-effort — a contended
    commit degrades and logs "deploy state reconciles on the next boot / health
    tick", but no such tick existed. A degraded boot write leaves the singleton
    stale for the whole revision's lifetime, and every post-promotion serving
    provider (``backend_revision`` / ``agent_image`` in release_artifacts.py)
    reads the same stale row — a promotion whose SHA IS live can sit at
    ``confirmation_timed_out``/``no_new_revision`` forever.

    This bounded re-detection re-resolves the deployed SHA (same source as boot:
    DEPLOY_SHA env, then Cloud Run image lookups) and reconciles the singleton
    when ``current_sha`` has drifted from what the running revision actually
    serves. Conservative: an unresolved SHA, a missing row, or an unchanged row
    are all no-ops. Returns True when the row was reconciled.
    """
    global _current_sha
    resolved = await resolve_deployed_sha()
    if not resolved:
        return False

    async with AsyncSessionLocal() as session:
        state = await session.get(DeploymentState, 1)
        if state is None:
            return False
        if (
            state.current_sha == resolved
            and state.current_revision == _current_revision
        ):
            return False
        _current_sha = resolved
        state.current_sha = resolved
        state.current_revision = _current_revision
        state.deployed_at = _utcnow()
        session.add(state)
        return await _best_effort_deploy_state_commit(session)


# --- Webhook notification + alert dispatch (extracted) ---
# _send_webhook_notification moved to app.notification; the alert-creation
# helpers (_get_broadcast_limit, the _create_*_alert family, the
# _maybe_create_*_alert family) moved to app.alert_dispatch (P5 service-locator
# decomposition) so the orchestrator/reaper/completion handlers/routers import
# them directly instead of via a deferred `from app.main import ...`. Both are
# re-exported here for backward compatibility — _init_deployment_state below
# and tests still reference these names through app.main.
from app.notification import _send_webhook_notification  # noqa: F401  re-exported for back-compat
from app.alert_dispatch import (  # noqa: F401  re-exported for back-compat (callers + tests patch app.main.X)
    _get_broadcast_limit,
    _create_orchestrator_error_alerts,
    _create_halted_alerts,
    _create_rollback_alerts,
    _create_halted_recovery_alerts,
    _maybe_create_failure_alert,
    _maybe_create_unverified_merge_alert,
    _maybe_create_circuit_breaker_alert,
    _maybe_create_escalation_alert,
    _maybe_create_precondition_blocker_alert,
    _maybe_create_cascade_cancel_alert,
    _create_strategic_alert,
)


class RequestDurationMiddleware(BaseHTTPMiddleware):
    """Middleware to measure admin API request duration and add Server-Timing headers.

    Tracks backend processing time for each request, adds Server-Timing header,
    and logs requests exceeding the slow-request threshold.
    """

    SLOW_REQUEST_THRESHOLD_MS = 500  # Log requests slower than 500ms

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Only measure admin/debug API endpoints
        path = request.url.path
        is_admin_endpoint = path.startswith("/api/admin/") or path.startswith(
            "/api/debug/"
        )
        if not is_admin_endpoint:
            return await call_next(request)

        start_time = time.monotonic()
        response = await call_next(request)
        elapsed_ms = (time.monotonic() - start_time) * 1000

        # Build Server-Timing header: backend total first, then per-step entries
        timing_parts = [f"backend;dur={elapsed_ms:.1f}"]
        per_step_entries = getattr(request.state, "_server_timing_entries", None)
        if per_step_entries:
            for name, ms in per_step_entries:
                timing_parts.append(f"{name};dur={ms:.1f}")
        response.headers["Server-Timing"] = ", ".join(timing_parts)

        # Log slow requests with route template and duration
        if elapsed_ms > self.SLOW_REQUEST_THRESHOLD_MS:
            logger.warning(
                "[ADMIN_PERF] slow request",
                extra={
                    "route": path,
                    "method": request.method,
                    "duration_ms": round(elapsed_ms, 1),
                    "status": response.status_code,
                },
            )

        return response


app = FastAPI(
    title="Blueprint Orchestrator",
    description="Secure Orchestration & State Management",
    lifespan=lifespan,
)


app.include_router(admin_router)
app.include_router(admin_projects_router)
app.include_router(admin_project_strategic_session_router)
app.include_router(admin_settings_router)
app.include_router(admin_migration_exemptions_router)
app.include_router(admin_debug_router)
app.include_router(admin_pool_accounts_router)
app.include_router(admin_project_config_router)
app.include_router(admin_project_tools_router)
app.include_router(admin_project_tools_asset_router)
app.include_router(admin_warm_pool_router)
app.include_router(admin_runners_router)
app.include_router(runner_router)
app.include_router(agent_router)
app.include_router(efficiency_snapshot_agent_router)
app.include_router(agent_github_router)
app.include_router(agent_ci_compliance_router)
app.include_router(agent_strategic_fetch_router)
app.include_router(agent_strategic_messages_router)
app.include_router(strategic_resident_session_router)
app.include_router(agent_tasks_router)
app.include_router(agent_memory_consolidation_router)
app.include_router(agent_strategic_memory_router)
app.include_router(admin_strategic_memory_router)
app.include_router(agent_warm_continuity_router)
app.include_router(github_router)
app.include_router(projects_router)
app.include_router(directions_router)
app.include_router(deploy_router)
app.include_router(admin_users_router)
app.include_router(admin_notifications_router)
app.include_router(admin_analytics_router)
app.include_router(admin_digest_router)
app.include_router(admin_metrics_router)
app.include_router(admin_activity_router)
app.include_router(admin_health_router)
app.include_router(admin_state_router)
app.include_router(admin_acceptance_criteria_router)
app.include_router(admin_operator_attention_router)
app.include_router(admin_service_credentials_router)
app.include_router(admin_evergreen_router)
app.include_router(admin_orchestrator_errors_router)
app.include_router(admin_scope_measurement_router)
app.include_router(agent_status_router)
app.include_router(agent_credentials_router)
app.include_router(agent_compute_jobs_router)
app.include_router(admin_compute_jobs_router)
app.include_router(admin_direction_events_router)
app.include_router(admin_onboard_router)
app.include_router(admin_agent_ops_router)
app.include_router(admin_auth_router)


def _get_zone_health_summary() -> dict:
    """Get zone health summary from compute module, with fallback if not in GCP mode."""
    try:
        from app.compute import get_zone_health_summary

        return get_zone_health_summary()
    except Exception:
        return {"total_zones": 0, "available": 0, "exhausted": []}


def _get_notification_stats() -> dict:
    """Return notification delivery stats from notification_dispatch module."""
    try:
        from app.notification_dispatch import get_notification_stats

        return get_notification_stats()
    except Exception:
        return {}


@app.get("/api/debug/orchestrator")
async def debug_orchestrator(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Diagnostic endpoint: check if the orchestrator loop is alive."""
    from sqlalchemy import func

    queued = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.status == TaskStatus.QUEUED, Task.requires_approval.is_(False))
        )
    ).scalar() or 0
    active = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
        )
    ).scalar() or 0
    ago = None
    if _orch._orchestrator_last_cycle:
        ago = (_utcnow() - _orch._orchestrator_last_cycle).total_seconds()
    # Include deployment state
    deploy_state = None
    try:
        state = await session.get(DeploymentState, 1)
        if state:
            monitoring_since = _shared_state.deploy_monitoring.get("window_start")
            recent_results = list(
                _shared_state.deploy_monitoring.get("task_results", [])
            )[-10:]
            deploy_state = {
                "revision": state.current_revision,
                "sha": state.current_sha[:8] if state.current_sha else "",
                "status": state.status,
                "deployed_at": state.deployed_at.isoformat()
                if state.deployed_at
                else None,
                "last_known_good_sha": state.last_known_good_sha[:8]
                if state.last_known_good_sha
                else "",
                "rollback_count": state.rollback_count,
                "rollback_reason": state.rollback_reason,
                "pause_reason": state.pause_reason,
                "monitoring_since": monitoring_since.isoformat()
                if monitoring_since
                else None,
                "recent_outcomes": [
                    {
                        "created_at": r[0],
                        "status": r[1],
                        "failure_class": r[2] if len(r) > 2 else "",
                    }
                    for r in recent_results
                ],
            }
    except Exception as e:
        logger.warning(
            f"[DEBUG] Failed to fetch deployment state for /api/debug/orchestrator: {e}",
            exc_info=True,
        )

    # Snapshot pool under lock for consistent read
    all_accts = _all_accounts()
    async with _max_plan_pool_lock:
        pool_snapshot = {
            "size": len(all_accts),
            "available": sum(1 for a in all_accts if a.get("enabled", True)),
            "disabled": sum(1 for a in all_accts if not a.get("enabled", True)),
            "accounts": [
                {
                    "name": a["name"],
                    "enabled": a.get("enabled", True),
                    "active": a["active_tasks"],
                    "disabled_at": a["disabled_at"].isoformat()
                    if a.get("disabled_at")
                    else None,
                    "reenable_at": (
                        a["disabled_at"] + timedelta(minutes=30)
                    ).isoformat()
                    if a.get("disabled_at")
                    else None,
                }
                for a in all_accts
            ],
        }

    # Liveness is judged by the in-cycle progress *heartbeat* (the shared
    # evaluate_orchestrator_liveness verdict, same as /health and the watchdog),
    # NOT by full-cycle-completion age. A slow-but-progressing cycle is alive and
    # not hung; only a worker with no heartbeat past ORCHESTRATOR_HANG_SECONDS (or
    # a first cycle that started but never heartbeat) is wedged. `ago` /
    # last_cycle_seconds_ago is preserved purely as an informational slow-cycle
    # signal. See incident 2026-05-28 / orchestrator-operator-health-split.
    liveness = _orch.evaluate_orchestrator_liveness(_utcnow())

    return {
        "orchestrator_alive": liveness.alive,
        "orchestrator_hung": liveness.stalled,
        "orchestrator_progress_age_seconds": liveness.progress_age,
        "orchestrator_cycle_age_seconds": ago,
        "orchestrator_liveness_reason": liveness.reason,
        "last_cycle_seconds_ago": ago,
        "queued_tasks": queued,
        "active_tasks": active,
        "max_concurrent_vms": _runtime_config["max_concurrent_vms"],
        "actual_gcp_vm_count": _vm_lifecycle._last_gcp_vm_count,
        "gcp_vm_count_age_seconds": int(time.time() - _vm_lifecycle._last_gcp_vm_check)
        if _vm_lifecycle._last_gcp_vm_check
        else None,
        "pool_size": pool_snapshot["size"],
        "available_count": pool_snapshot["available"],
        "disabled_count": pool_snapshot["disabled"],
        "pool_accounts": pool_snapshot["accounts"],
        "deploy_state": deploy_state,
        "deploy_monitoring": {
            "window_active": _shared_state.deploy_monitoring["window_start"]
            is not None,
            "task_results_count": len(_shared_state.deploy_monitoring["task_results"]),
            "last_dispatch": _shared_state.deploy_monitoring[
                "last_dispatch_time"
            ].isoformat()
            if _shared_state.deploy_monitoring["last_dispatch_time"]
            else None,
            "last_completion": _shared_state.deploy_monitoring[
                "last_completion_time"
            ].isoformat()
            if _shared_state.deploy_monitoring["last_completion_time"]
            else None,
            "external_health": _shared_state.external_health,
        },
        "zone_health": _get_zone_health_summary(),
        "notification_stats": _get_notification_stats(),
    }


@app.get("/api/health/detailed")
async def health_detailed(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Authenticated endpoint returning structured health status for all system components."""
    import time
    from sqlalchemy import text
    from sqlalchemy import func

    components: Dict[str, Any] = {}

    # --- DB ---
    db_status = "ok"
    db_latency_ms: float | None = None
    try:
        t0 = time.monotonic()
        await session.execute(text("SELECT 1"))
        db_latency_ms = round((time.monotonic() - t0) * 1000, 2)
        if db_latency_ms > 200:
            db_status = "degraded"
    except Exception:
        db_status = "down"
    components["db"] = {"status": db_status, "latency_ms": db_latency_ms}

    # --- GitHub API ---
    gh_ok = _shared_state.external_health.get("github_api", True)
    last_check = _shared_state.external_health.get("last_check")
    components["github_api"] = {
        "status": "ok" if gh_ok else "down",
        "last_check": last_check.isoformat()
        if isinstance(last_check, datetime)
        else last_check,
    }

    provider_health = _shared_state.external_health.get("provider_health", {}) or {}
    dispatch_health = _shared_state.external_health.get("dispatch_health", {}) or {}
    components["provider_apis"] = {
        "status": dispatch_health.get("status", "unknown"),
        "fallback_enabled": dispatch_health.get("fallback_enabled", False),
        "required_providers": dispatch_health.get("required_providers", []),
        "available_providers": dispatch_health.get("available_providers", []),
        "degraded_providers": dispatch_health.get("degraded_providers", []),
        "down_providers": dispatch_health.get("down_providers", []),
        "providers": provider_health,
    }

    # --- Orchestrator ---
    # Primary verdict comes from the shared progress-heartbeat liveness predicate
    # (same as /health and the watchdog), NOT from full-cycle-completion age:
    #   ok        -> healthy (fresh heartbeat)
    #   degraded  -> alive + progressing but the last *completed* cycle is stale
    #                (slow cycle), an informational soft signal only
    #   down      -> genuinely wedged: no heartbeat past ORCHESTRATOR_HANG_SECONDS,
    #                or a first cycle that started but never made progress.
    # cycle_age is preserved as the informational slow-cycle marker.
    liveness = _orch.evaluate_orchestrator_liveness(_utcnow())
    cycle_age: float | None = None
    if _orch._orchestrator_last_cycle:
        cycle_age = (_utcnow() - _orch._orchestrator_last_cycle).total_seconds()
    slow_cycle = cycle_age is not None and cycle_age >= 120
    if liveness.healthy:
        orch_status = "degraded" if slow_cycle else "ok"
    elif liveness.stalled or not liveness.alive:
        orch_status = "down"
    else:
        orch_status = "degraded"
    try:
        queued = (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.status == TaskStatus.QUEUED, Task.requires_approval.is_(False)
                )
            )
        ).scalar() or 0
        active = (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                )
            )
        ).scalar() or 0
    except Exception:
        queued = active = 0
    components["orchestrator"] = {
        "status": orch_status,
        "cycle_age_seconds": cycle_age,
        "progress_age_seconds": liveness.progress_age,
        "queued_tasks": queued,
        "active_tasks": active,
    }

    # --- Zone Health ---
    zone_summary = _get_zone_health_summary()
    total_zones = zone_summary.get("total_zones", 0)
    available_zones = zone_summary.get("available", 0)
    exhausted_zones = zone_summary.get("exhausted", [])
    if total_zones == 0 or available_zones > 0:
        zone_status = "ok"
    elif len(exhausted_zones) < total_zones:
        zone_status = "degraded"
    else:
        zone_status = "down"
    components["zone_health"] = dict(zone_summary, status=zone_status)

    # --- Max Plan Pool ---
    total_accounts = len(_account_by_id)
    available_accounts = sum(
        1 for a in _account_by_id.values() if a.get("enabled", True)
    )
    if total_accounts == 0:
        pool_status = "ok"  # not using Max Plan accounts
    elif available_accounts > 0:
        pool_status = "ok"
    else:
        pool_status = "down"
    by_provider: Dict[str, Dict[str, int]] = {}
    for account in _account_by_id.values():
        provider_name = account.get("provider", "anthropic")
        bucket = by_provider.setdefault(provider_name, {"total": 0, "available": 0})
        bucket["total"] += 1
        if account.get("enabled", True):
            bucket["available"] += 1
    components["max_plan_pool"] = {
        "status": pool_status,
        "total": total_accounts,
        "available": available_accounts,
        "by_provider": by_provider,
    }

    # --- Circuit Breakers ---
    breaker_states: Dict[str, Any] = {}
    for project_id, breaker in _shared_state.circuit_breakers.items():
        breaker_states[project_id] = {
            "state": breaker.get("state", "CLOSED"),
            "failure_count": breaker.get("failure_count", 0),
            "opened_at": breaker["opened_at"].isoformat()
            if breaker.get("opened_at")
            else None,
        }
    components["circuit_breaker"] = {"status": "ok", "projects": breaker_states}

    # --- Autonomous ---
    autonomous_enabled = _runtime_config.get("max_concurrent_vms", 0) > 0
    consecutive_failures = (
        sum(_shared_state.autonomous_failure_counts.values())
        if _shared_state.autonomous_failure_counts
        else 0
    )
    components["autonomous"] = {
        "status": "ok" if consecutive_failures < 3 else "degraded",
        "enabled": autonomous_enabled,
        "consecutive_failures": consecutive_failures,
    }

    # --- GitHub API Quota (populated by _github_request on every response) ---
    # Surfaces X-RateLimit-Remaining so the dashboard can show a live quota
    # widget.  critical < 100, degraded < 500, ok otherwise (unknown = no
    # recent call observed).  top_endpoints shows the per-endpoint call
    # breakdown for the current reset window so we can see what's actually
    # burning the quota when it gets low (instead of guessing).
    gq = dict(_shared_state.github_quota)
    _q_remaining = gq.get("remaining")
    if _q_remaining is None:
        _q_status = "unknown"
    elif _q_remaining < 100:
        _q_status = "critical"
    elif _q_remaining < 500:
        _q_status = "degraded"
    else:
        _q_status = "ok"
    # Top 15 most-called endpoint templates this window.  Caps the payload
    # regardless of how many distinct endpoints we've seen.
    _top_calls = sorted(
        _shared_state.github_call_counts.items(),
        key=lambda kv: kv[1],
        reverse=True,
    )[:15]
    # Cache-hit counters — calls we did NOT send to GitHub thanks to dedup /
    # short-circuit.  Compare against total_calls_this_window: if hits >> calls,
    # the caches are earning their keep.  See github.py:get_check_runs_cached,
    # security.py:scan_pr_for_injection, rollup_management.py:_verify_pr_merged.
    _cache_hits = dict(_shared_state.github_cache_hits)
    # Top 10 consumers by call count this window.
    _top_consumers = sorted(
        _shared_state.github_consumer_counts.items(),
        key=lambda kv: kv[1],
        reverse=True,
    )[:10]
    components["github_quota"] = {
        **gq,
        "status": _q_status,
        "top_endpoints": [{"endpoint": ep, "count": n} for ep, n in _top_calls],
        "total_calls_this_window": sum(_shared_state.github_call_counts.values()),
        "cache_hits_this_window": _cache_hits,
        "calls_avoided_this_window": sum(_cache_hits.values()),
        "consumer_counts": [{"consumer": c, "count": n} for c, n in _top_consumers],
        "consumer_snapshot": dict(_shared_state.github_consumer_counts_snapshot),
        "off_backend_burn_estimate": _shared_state.github_off_backend_estimate.get(
            "cumulative_this_window", 0
        ),
    }

    # --- Pipeline Health (rolling reaper signals) ---
    ph_snapshot = _shared_state.get_pipeline_health_snapshot()
    ph_status = "degraded" if ph_snapshot["alerts_active"] else "ok"
    components["pipeline_health"] = dict(ph_snapshot, status=ph_status)

    # --- Overall Status ---
    statuses = [
        c["status"]
        for c in components.values()
        if isinstance(c, dict) and "status" in c
    ]
    if "down" in statuses:
        overall = "unhealthy"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "healthy"

    return {"status": overall, "components": components, "pipeline_health": ph_snapshot}


@app.get("/api/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    """Return the authenticated user's profile and role."""
    return {
        "user_id": current_user["user_id"],
        "email": current_user["email"],
        "role": current_user["role"],
    }


@app.get("/api/version")
async def get_version():
    """Public endpoint returning current backend version for frontend compatibility checks."""
    return {"sha": _current_sha, "revision": _current_revision}


@app.get("/api/admin/viewport-failures/download-url")
async def get_viewport_failure_screenshot_download_url(
    project_id: uuid.UUID,
    component: str,
    width: int,
    session: AsyncSession = Depends(get_session),
    current_user: dict | None = Depends(get_current_user_optional),
    x_agent_token: str | None = Header(None),
):
    """Generate a 1-hour signed download URL for a viewport failure screenshot.

    Accepts either an admin/Firebase bearer token (existing path) or an agent
    JWT via the ``x-agent-token`` header.  Agent tokens are scoped to the
    uploading task: the reconstructed object URI must match the URI stamped on
    the task's context (``viewport_failure_screenshot_uri``) and the project
    must match the token's ``project_id`` claim.

    The GCS object path is reconstructed to exactly mirror what the agent
    uploads via POST /api/agent/viewport/failure-screenshot-upload-url
    (viewport-failures/{project_id}/{component}-{width}.png), so the regression
    panel can display screenshots from the stable per-project/component/width
    path without a per-task DB lookup. The component is sanitized with the same
    rule the upload endpoint applies so the two paths always agree.

    Signing is delegated to the shared ``app.storage`` helper so this route and
    the general artifact-download resolver share one bucket-allowlisting +
    V4-signing implementation instead of duplicating it.
    """
    from app.storage import (
        ARTIFACT_BUCKET_NAME,
        StorageUriError,
        generate_download_signed_url,
    )
    from app.viewport_evidence import viewport_object_name

    if x_agent_token is not None:
        claims = verify_agent_jwt(x_agent_token)
        task_id = claims.get("sub")
        token_project_id = claims.get("project_id")
        if not task_id:
            raise HTTPException(status_code=403, detail="Agent token has no task scope")
        if str(token_project_id) != str(project_id):
            raise HTTPException(status_code=403, detail="Project mismatch")
        result = await session.execute(
            select(Task).where(Task.id == uuid.UUID(task_id))
        )
        task = result.scalar_one_or_none()
        if task is None:
            raise HTTPException(status_code=403, detail="Task not found")
        object_name = viewport_object_name(project_id, component, width)
        stamped_uri = (task.context or {}).get("viewport_failure_screenshot_uri", "")
        if not stamped_uri or not stamped_uri.endswith(object_name):
            raise HTTPException(
                status_code=403,
                detail="Agent may only download its own uploaded screenshot",
            )
    elif current_user is not None:
        await _get_user_project(project_id, current_user["user_id"], session)
        object_name = viewport_object_name(project_id, component, width)
    else:
        raise HTTPException(status_code=401, detail="Authentication required")

    storage_uri = f"gs://{ARTIFACT_BUCKET_NAME}/{object_name}"

    try:
        url = await generate_download_signed_url(storage_uri, expiration_minutes=60)
    except StorageUriError as exc:
        # Reconstructed path failed validation — a client/config error, not an
        # outage. Surface as 400 without echoing the object path back.
        logger.warning("[VIEWPORT] invalid screenshot reference: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid screenshot reference")
    except Exception as exc:
        logger.warning(
            "[VIEWPORT] signed download URL failed for %s: %s", object_name, exc
        )
        raise HTTPException(status_code=503, detail="Artifact storage unavailable")
    return {"url": url}


@app.get("/api/admin/viewport-shots/download-url")
async def get_viewport_screenshot_download_url(
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    name: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict | None = Depends(get_current_user_optional),
    x_agent_token: str | None = Header(None),
):
    """Signed download URL for a task-scoped viewport screenshot.

    Two access paths:
    - Agent JWT (x-agent-token header): validates token project_id claim, returns JSON {"url": ...}.
      Cross-task access is intentionally allowed within the same project so that review/fix
      agents can download screenshots uploaded by the source implementer task.
    - Firebase-authenticated admin user: validates project access, returns JSON {"url": ...}.

    Unauthenticated requests are rejected with 401. Operators who need unauthenticated
    access to screenshots should use GCS public object permissions on specific objects.

    The GCS object path mirrors what the agent uploads via
    POST /api/agent/viewport/screenshot-upload-url
    (viewport-shots/{project_id}/{task_id}/{name}.png).
    """
    import re as _re

    from app.storage import (
        ARTIFACT_BUCKET_NAME,
        StorageUriError,
        generate_download_signed_url,
    )

    if x_agent_token is not None:
        claims = verify_agent_jwt(x_agent_token)
        token_task_id = claims.get("sub")
        token_project_id = claims.get("project_id")
        if not token_task_id:
            raise HTTPException(status_code=403, detail="Agent token has no task scope")
        # Project-scoped check only: review/fix agents need cross-task access to
        # download screenshots from the source implementer task within the same project.
        if str(token_project_id) != str(project_id):
            raise HTTPException(status_code=403, detail="Project mismatch")
    elif current_user is not None:
        await _get_user_project(project_id, current_user["user_id"], session)
    else:
        raise HTTPException(status_code=401, detail="Authentication required")

    _SAFE_NAME_RE = _re.compile(r"[^A-Za-z0-9._-]")
    safe_name = _SAFE_NAME_RE.sub("_", name)

    object_name = f"viewport-shots/{project_id}/{task_id}/{safe_name}.png"
    storage_uri = f"gs://{ARTIFACT_BUCKET_NAME}/{object_name}"

    try:
        url = await generate_download_signed_url(storage_uri, expiration_minutes=60)
    except StorageUriError as exc:
        logger.warning("[VIEWPORT] invalid screenshot reference: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid screenshot reference")
    except Exception as exc:
        logger.warning(
            "[VIEWPORT] signed download URL failed for %s: %s", object_name, exc
        )
        raise HTTPException(status_code=503, detail="Artifact storage unavailable")

    return {"url": url}


@app.get(
    "/api/admin/artifacts/download-url",
    response_model=ArtifactDownloadResponse,
)
async def resolve_artifact_download_url(
    storage_uri: str,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Resolve a project-owned ``gs://`` artifact reference into a signed GET URL.

    Operators reach this for any storage-backed output — a compute job
    ``result_reference``, a task artifact ``storage_uri``, a compute-job durable
    artifact reference, or a knowledge citation — instead of being limited to
    raw http(s) links. It fails closed:

    * malformed / non-``gs://`` / out-of-bucket references -> 400, no URL leaked;
    * a reference whose owning task is in a different project than ``project_id``
      (or a project the caller can't access) -> 404, treated as not-found so
      cross-project probing learns nothing;
    * references not bound to a resolvable task or compute job -> 404;
    * a storage-layer failure while signing -> 503.

    ``project_id`` is the project the operator is viewing; membership in it is
    enforced before any object is signed.
    """
    from app.storage import (
        compute_job_id_for_artifact_uri,
        StorageUriError,
        generate_download_signed_url,
        parse_artifact_uri,
        verify_artifact_project_ownership,
    )

    # 1. Scheme + bucket allowlist. Reject malformed/out-of-bucket before any
    # DB or storage work so nothing about the object is observable.
    try:
        parse_artifact_uri(storage_uri)
    except StorageUriError as exc:
        logger.info("[ARTIFACT] rejected download reference: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid artifact reference")

    # 2a. User membership in the claimed project.
    await _get_user_project(project_id, current_user["user_id"], session)

    # 2b. Artifact→project ownership via the centralized helper. A reference
    # that doesn't resolve to a task or compute-job prefix is rejected as
    # not-found so unrecognised prefixes can't be probed.
    try:
        await verify_artifact_project_ownership(storage_uri, project_id, session)
    except StorageUriError:
        raise HTTPException(status_code=404, detail="Artifact not found")

    # 2c. Compute-job URIs have an additional guard: only the recorded terminal
    # outputs are signable; unknown siblings under the same prefix are
    # indistinguishable from "not found".
    owning_compute_job_id = compute_job_id_for_artifact_uri(storage_uri)
    if owning_compute_job_id is not None:
        from app.models import ComputeJob, Artifact, ComputeJobFile

        job = await session.get(ComputeJob, owning_compute_job_id)
        allowed_uris: set[str] = set()
        if job and job.result_reference:
            allowed_uris.add(job.result_reference)
        artifact = None
        if job and job.artifact_id is not None:
            artifact = await session.get(Artifact, job.artifact_id)
            if artifact is not None and artifact.storage_uri:
                allowed_uris.add(artifact.storage_uri)
        # Individually-addressable viewable files (images/videos) recorded in the
        # job's `viewable_files` manifest are also signable — they let the canvas
        # preview a run's plots/images inline. Only URIs actually recorded in the
        # manifest are allowed; unknown siblings under the same prefix stay 404.
        if job:
            viewable = (job.polling_metadata or {}).get("viewable_files")
            if isinstance(viewable, list):
                for entry in viewable:
                    if not isinstance(entry, dict):
                        continue
                    viewable_uri = entry.get("storage_uri")
                    if isinstance(viewable_uri, str) and viewable_uri:
                        allowed_uris.add(viewable_uri)
        if storage_uri not in allowed_uris:
            # 2c-iii. Per-file compute job media: individually-addressable
            # viewable files extracted from the bundle are recorded as
            # ComputeJobFile rows — query by storage_uri and verify the
            # owning job matches.
            from sqlalchemy import select as _select

            cjf_query = _select(ComputeJobFile).where(
                ComputeJobFile.storage_uri == storage_uri
            )
            cjf_result = await session.execute(cjf_query)
            cjf = cjf_result.scalar_one_or_none()
            if cjf is None or cjf.compute_job_id != owning_compute_job_id:
                raise HTTPException(status_code=404, detail="Artifact not found")
            # Extension guard: apply the same viewable-extension allowlist that
            # the manifest build path enforces so the ComputeJobFile signing
            # branch cannot authorize non-viewable types.
            from app.storage import (
                viewable_content_type_for_filename as _vctype,
            )
            if _vctype(cjf.safe_relpath) is None:
                raise HTTPException(status_code=404, detail="Artifact not found")
        elif storage_uri != (job.result_reference or "") and (
            artifact is None or storage_uri != artifact.storage_uri
        ):
            # This URI came from the viewable_files manifest — apply the same
            # extension guard that the manifest build path enforces at write time,
            # making the check symmetric across both signing branches.
            from app.storage import (
                viewable_content_type_for_filename as _vctype2,
            )

            uri_basename = storage_uri.rsplit("/", 1)[-1]
            if _vctype2(uri_basename) is None:
                raise HTTPException(status_code=404, detail="Artifact not found")

    # 3. Sign. Validation already passed, so any failure here is a storage
    # outage, not a bad reference.
    try:
        url = await generate_download_signed_url(storage_uri, expiration_minutes=60)
    except StorageUriError:
        # Defensive: should be unreachable since step 1 validated the URI.
        raise HTTPException(status_code=400, detail="Invalid artifact reference")
    except Exception as exc:
        logger.warning("[ARTIFACT] signing failed for %s: %s", storage_uri, exc)
        raise HTTPException(status_code=503, detail="Artifact storage unavailable")
    return {"url": url}


@app.get("/health")
async def health_check():
    """Unauthenticated health endpoint for external monitoring (Cloud Function, load balancer)."""
    from app.shared_state import external_health

    # Non-leader instances don't run the orchestrator — they're healthy HTTP servers
    # but should be clearly distinguished from a leader with a stale/missing cycle.
    if not _shared_state.is_leader:
        return JSONResponse(
            status_code=200,
            content={
                "status": "healthy",
                "is_leader": False,
                "revision": _current_revision,
            },
        )

    # Liveness is gated on the worker's in-cycle progress *heartbeat*, NOT on
    # full-cycle completion. A cycle's wall time scales with the EVALUATING
    # backlog (O(N) CI polling); gating liveness on a fixed 60s full-cycle
    # window killed a slow-but-healthy worker, discarding its progress and
    # spiralling into a Cloud Run restart loop that froze the whole pipeline
    # (incident 2026-05-28). Now only a genuinely wedged worker — no heartbeat
    # for ORCHESTRATOR_HANG_SECONDS — is reported unhealthy (→ liveness restart).
    liveness = _orch.evaluate_orchestrator_liveness(_utcnow())
    orchestrator_alive = liveness.alive
    orchestrator_healthy = liveness.healthy
    progress_age = liveness.progress_age

    # cycle_age = time since the last *completed* cycle. Still surfaced and used
    # as a soft "slow cycle" degraded signal for alerting, but it no longer
    # removes the instance from the LB or triggers a liveness kill.
    cycle_age = None
    if _orch._orchestrator_last_cycle:
        cycle_age = (_utcnow() - _orch._orchestrator_last_cycle).total_seconds()
    slow_cycle = cycle_age is not None and cycle_age >= 120

    # Use the same stalled verdict as the debug endpoint and watchdog, so all
    # surfaces agree: liveness.stalled fires after ORCHESTRATOR_HANG_SECONDS
    # (180s) without a progress heartbeat, which is stricter than the old
    # hardcoded 300s wall-clock check on cycle_started.
    orchestrator_hung = liveness.stalled

    # Only DB is critical for 503 — provider or external API outages shouldn't remove us from LB.
    critical_healthy = external_health.get("db", True)
    degraded_deps = [
        dep for dep in (external_health.get("failed_deps", []) or []) if dep != "db"
    ]
    if orchestrator_hung:
        degraded_deps.append("orchestrator_hung")
    if slow_cycle:
        degraded_deps.append("orchestrator_slow_cycle")

    healthy = orchestrator_healthy and critical_healthy
    status = (
        "healthy"
        if healthy and not degraded_deps
        else "degraded"
        if healthy
        else "unhealthy"
    )
    status_code = 200 if healthy else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": status,
            "is_leader": True,
            "revision": _current_revision,
            "orchestrator_alive": orchestrator_alive,
            "orchestrator_cycle_age_seconds": cycle_age,
            "orchestrator_progress_age_seconds": progress_age,
            "external_deps": {
                k: v
                for k, v in external_health.items()
                if k
                not in (
                    "last_check",
                    "failed_deps",
                    "errors",
                    "provider_health",
                    "dispatch_health",
                )
            },
            "degraded_deps": degraded_deps,
        },
    )


_METRICS_TOKEN = os.getenv("METRICS_TOKEN", "")


@app.get("/metrics")
async def prometheus_metrics(request: Request):
    """Prometheus-compatible metrics endpoint (text/plain exposition format).

    Unauthenticated by default. If METRICS_TOKEN env var is set, requires
    ?token=<value> query param matching that value (403 otherwise).
    """
    if _METRICS_TOKEN:
        token = request.query_params.get("token", "")
        if not hmac.compare_digest(token, _METRICS_TOKEN):
            return JSONResponse(status_code=403, content={"error": "forbidden"})

    cycle_age: float | None = None
    if _orch._orchestrator_last_cycle:
        cycle_age = (_utcnow() - _orch._orchestrator_last_cycle).total_seconds()

    active_vms = sum(a.get("active_tasks", 0) for a in _account_by_id.values())
    consecutive_failures = (
        sum(_shared_state.autonomous_failure_counts.values())
        if _shared_state.autonomous_failure_counts
        else 0
    )

    lines: list[str] = []

    def gauge(
        name: str, help_text: str, value: float, labels: Dict[str, str] | None = None
    ) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        label_str = ""
        if labels:
            label_str = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
        lines.append(f"{name}{label_str} {value}")

    def counter(name: str, help_text: str, value: float) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {value}")

    gauge(
        "blueprint_tasks_queued",
        "Number of tasks currently queued",
        _shared_state.tasks_queued_gauge,
    )
    gauge(
        "blueprint_tasks_active",
        "Number of tasks currently provisioning or in progress",
        _shared_state.tasks_active_gauge,
    )
    counter(
        "blueprint_tasks_completed_total",
        "Cumulative number of completed tasks",
        _shared_state.tasks_completed_total,
    )
    counter(
        "blueprint_tasks_failed_total",
        "Cumulative number of failed tasks",
        _shared_state.tasks_failed_total,
    )
    gauge(
        "blueprint_orchestrator_cycle_age_seconds",
        "Seconds since last orchestrator cycle",
        cycle_age if cycle_age is not None else -1,
    )
    gauge(
        "blueprint_active_vms", "Current active VM count from Max Plan pool", active_vms
    )

    if _shared_state.circuit_breakers:
        lines.append(
            "# HELP blueprint_circuit_breaker_open 1 if circuit breaker is OPEN, 0 otherwise"
        )
        lines.append("# TYPE blueprint_circuit_breaker_open gauge")
        for project_id, breaker in _shared_state.circuit_breakers.items():
            is_open = 1 if breaker.get("state") == "OPEN" else 0
            lines.append(
                f'blueprint_circuit_breaker_open{{project_id="{project_id}"}} {is_open}'
            )

    gauge(
        "blueprint_autonomous_consecutive_failures",
        "Consecutive autonomous task failures",
        consecutive_failures,
    )

    # DB pool invalidation telemetry (handle_error event listener in
    # database.py). Gives us a direct signal whether the cutover-safe
    # connection-eject pattern is firing — lets a future Cloud Run rollout
    # be measured against the 2026-05-04 cascade baseline.
    import contextlib

    with contextlib.suppress(Exception):
        from app.database import get_db_pool_invalidation_stats

        _db_stats = get_db_pool_invalidation_stats()
        counter(
            "blueprint_db_pool_invalidations_total",
            "Cumulative pool connection invalidations from handle_error listener",
            _db_stats.get("invalidate_total", 0),
        )
        gauge(
            "blueprint_db_pool_invalidate_consecutive_failures",
            "Consecutive failures of connection.invalidate() calls (escalates to pool.dispose at threshold)",
            _db_stats.get("invalidate_consecutive_failures", 0),
        )

    # _orchestrator_deleted_vms tracker size — observability into the in-memory
    # delete-tracker that drives reaper preemption-classification correctness.
    with contextlib.suppress(Exception):
        gauge(
            "blueprint_orchestrator_deleted_vms_size",
            "Number of entries in the orchestrator-initiated VM delete tracker",
            len(_vm_lifecycle._orchestrator_deleted_vms),
        )

    # Reaper liveness — counter increments only on a fully-successful cycle.
    # Production 2026-05-04: reaper crashed every cycle for 80+ minutes
    # (MissingGreenlet at reaper.py:724) before operator noticed via stuck
    # task heartbeat ages. Alert on ``last_cycle_age_seconds > 300`` to
    # catch any reaper crash-loop within ~5 successful-cycle windows.
    with contextlib.suppress(Exception):
        from app.reaper import get_reaper_stats

        _reaper_stats = get_reaper_stats()
        counter(
            "blueprint_reaper_cycles_completed_total",
            "Cumulative number of fully-successful reaper cycles",
            _reaper_stats.get("cycles_completed_total", 0),
        )
        gauge(
            "blueprint_reaper_last_cycle_age_seconds",
            "Seconds since last fully-successful reaper cycle (-1 if never completed)",
            _reaper_stats.get("last_cycle_age_seconds", -1),
        )

    # Park/wake operational health (fleet-wide, read-only). Surfaces the PARKED
    # population and the wake-delivery backlog so a wedged delivery sweep is
    # visible in metrics, not just logs. Small, indexed aggregates; guarded so a
    # transient DB error degrades the scrape gracefully rather than failing it.
    with contextlib.suppress(Exception):
        from app.digest_metrics import compute_park_wake_health

        async with AsyncSessionLocal() as _pw_session:
            _pw = await compute_park_wake_health(_pw_session)
        gauge(
            "blueprint_tasks_parked",
            "Number of tasks currently PARKED on an external wake source",
            _pw.get("parked_task_count", 0),
        )
        gauge(
            "blueprint_parked_task_oldest_age_seconds",
            "Age of the oldest PARKED task in seconds (-1 if none)",
            _pw.get("oldest_parked_age_seconds") or -1,
        )
        gauge(
            "blueprint_wake_registrations_fired_undelivered",
            "FIRED TaskWakeRegistrations the wake-delivery sweep has not yet delivered",
            _pw.get("fired_undelivered_count", 0),
        )
        gauge(
            "blueprint_wake_fired_oldest_age_seconds",
            "Age of the oldest FIRED-but-undelivered wake registration in seconds (-1 if none)",
            _pw.get("oldest_fired_age_seconds") or -1,
        )
        gauge(
            "blueprint_wake_delivery_stalled",
            "1 if a FIRED wake registration has gone undelivered past the stall threshold, 0 otherwise",
            1 if _pw.get("delivery_stalled") else 0,
        )

    # Undelivered strategist replies — fleet-wide count and oldest age.
    # Uses the same error-isolated async pattern as park/wake health above
    # so a transient DB error degrades the scrape gracefully.
    with contextlib.suppress(Exception):
        from app.operator_inbox_liveness import compute_undelivered_strategist_replies

        async with AsyncSessionLocal() as _ur_session:
            _ur = await compute_undelivered_strategist_replies(_ur_session)
        gauge(
            "blueprint_undelivered_strategist_replies_total",
            "Total count of undelivered operator-directed strategist replies past the staleness threshold",
            _ur.get("total_count", 0),
        )
        gauge(
            "blueprint_undelivered_strategist_replies_oldest_age_seconds",
            "Age in seconds of the oldest undelivered strategist reply (-1 if none)",
            _ur.get("oldest_age_seconds") or -1,
        )

    body = "\n".join(lines) + "\n"
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(
        content=body, media_type="text/plain; version=0.0.4; charset=utf-8"
    )


# Attach the rate limiter to the app
app.state.limiter = limiter


def _apply_cors_headers(resp: JSONResponse, request: Request) -> JSONResponse:
    """Echo the request Origin onto an error response so the browser surfaces
    the real status code instead of a phantom CORS failure.

    Starlette's CORSMiddleware only adds Access-Control-Allow-Origin to
    responses produced by route handlers — exception-handler responses run
    outside it. Cloud Run's frontend then injects a wildcard `*`, which is
    incompatible with `allow_credentials=true` and the browser rejects it.
    Authenticated callers thus see every 429/5xx as "No 'Access-Control-Allow-
    Origin' header is present" rather than the real status. Echoing the
    specific origin (or `*` when explicitly allowed) restores the actual
    error to the client.

    allowed_origins is defined further down in this module and mutated per
    deployment via ALLOWED_ORIGINS env; resolve at call time.
    """
    origin = request.headers.get("origin")
    if origin and origin in allowed_origins:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Vary"] = "Origin"
    elif "*" in allowed_origins:
        # Non-credentialed wildcard only — browsers forbid Allow-Credentials with *.
        resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


def _cors_aware_rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """slowapi's default 429 handler + CORS headers via _apply_cors_headers.

    Frontend bursts (parallel initial-load fetches) are the typical trigger.
    """
    resp = JSONResponse(
        status_code=429,
        content={
            "error": {"code": 429, "message": f"Rate limit exceeded: {exc.detail}"}
        },
        headers={"Retry-After": "5"},
    )
    return _apply_cors_headers(resp, request)


app.add_exception_handler(RateLimitExceeded, _cors_aware_rate_limit_handler)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    resp = JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.status_code, "message": exc.detail}},
        headers=exc.headers,
    )
    return _apply_cors_headers(resp, request)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Pydantic v2 error dicts may contain non-serializable objects in ctx
    errors = []
    for e in exc.errors():
        clean = dict(e)
        ctx = clean.pop("ctx", None)
        if ctx:
            clean["ctx"] = {k: str(v) for k, v in ctx.items()}
        errors.append(clean)
    resp = JSONResponse(
        status_code=422,
        content={
            "error": {"code": 422, "message": "Validation Error", "details": errors}
        },
    )
    return _apply_cors_headers(resp, request)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception(
        f"[UNHANDLED] {request.method} {request.url.path}: {type(exc).__name__}: {exc}",
    )
    resp = JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
    )
    return _apply_cors_headers(resp, request)


# Add CORS Middleware so the frontend can call these APIs
# In production, set ALLOWED_ORIGINS to the specific frontend domain (e.g., "https://my-app.com")
# Multiple origins can be comma-separated.
def _resolve_allowed_origins(env_value: str) -> list[str]:
    """Parse ALLOWED_ORIGINS env value and reject any wildcard entry.

    A '*' anywhere in the list (whether ALLOWED_ORIGINS='*' or
    ALLOWED_ORIGINS='*,https://foo.com') combined with our
    allow_credentials=True CORS config would let Starlette's CORSMiddleware
    echo arbitrary origins back with credentials — the bypass this guard
    prevents.
    """
    if not env_value:
        return [
            "http://localhost:3000",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
        ]
    parsed = [origin.strip() for origin in env_value.split(",") if origin.strip()]
    if any(o == "*" for o in parsed):
        raise ValueError(
            "ALLOWED_ORIGINS may not contain '*' when Firebase auth is configured: "
            "a wildcard origin with allow_credentials=True lets any site send "
            "authenticated requests on behalf of logged-in users. "
            "Set ALLOWED_ORIGINS to a comma-separated list of explicit origins "
            f"(e.g. 'https://my-app.web.app,http://localhost:3000'). Got: {env_value!r}"
        )
    return parsed


allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
allowed_origins = _resolve_allowed_origins(allowed_origins_env)

app.add_middleware(CSRFMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "Origin",
        "X-Requested-With",
        "X-CSRF-Token",
    ],
)

app.add_middleware(RequestDurationMiddleware)


static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
install_frontend_routes(app, static_dir)


# _github_token_cache, invalidate_github_token_cache, get_github_token_for_task
# — extracted to app.github_token_resolver (re-exported from app.routers.agent).
# GitHub proxy endpoints — moved to app.routers.agent.
