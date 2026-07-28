"""Authoritative runtime guard for billable managed execution backends.

This guard is intentionally narrower than service deployment: it blocks new
agent/compute executions on managed infrastructure (GCE and Cloud Run Jobs),
but never the Cloud Run web service that hosts the control plane.  It is read
at every launch boundary as well as by dispatchers before they mark work as
provisioning, so a settings change takes effect without waiting for a restart.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.runtime_config_state import runtime_config


logger = logging.getLogger(__name__)


MANAGED_COMPUTE_SETTING = "managed_compute_enabled"
MANAGED_COMPUTE_DISABLED_REASON = "managed_compute_disabled"


class ManagedComputeDisabled(RuntimeError):
    """Raised when a launch would create new billable managed execution."""


class LocalOnlyExecutionPlacement(ManagedComputeDisabled):
    """Raised when a project's current placement forbids managed execution."""


def is_managed_compute_enabled() -> bool:
    """Return the current fleet-wide managed-execution switch.

    Missing legacy config deliberately means enabled.  That preserves existing
    deployments until an operator explicitly turns the new hard boundary off.
    """

    return bool(runtime_config.get(MANAGED_COMPUTE_SETTING, True))


def managed_compute_disabled_message(backend: str) -> str:
    return (
        f"managed compute is disabled; refusing new {backend} execution "
        f"({MANAGED_COMPUTE_SETTING}=false)"
    )


def assert_managed_compute_enabled(backend: str) -> None:
    """Raise before a managed backend performs any billable launch RPC."""

    if not is_managed_compute_enabled():
        raise ManagedComputeDisabled(managed_compute_disabled_message(backend))


async def is_managed_compute_enabled_authoritative(
    session=None,
    *,
    fresh_connection: bool = False,
) -> bool:
    """Read the fleet boundary from durable state when it can matter for spend.

    ``runtime_config`` remains the fast-path cache for ordinary dispatcher work,
    but it is intentionally *not* authoritative at a billable RPC: Cloud Run
    instances refresh that cache independently.  A local ``False`` wins without
    a query (safe fail-closed); otherwise read ``DeploymentState`` so a disable
    committed on another instance takes effect before a new paid execution.

    A missing setting/row is the legacy deployment case and retains the cached
    default.  If production persistence cannot be read at a billing boundary,
    fail closed rather than guess that an operator's kill switch is still on.
    """

    if not is_managed_compute_enabled():
        return False

    from app.database import AsyncSessionLocal, DATABASE_URL
    from app.models import DeploymentState

    owns_session = session is None
    read_engine = None
    try:
        if owns_session:
            session_factory = AsyncSessionLocal
            if fresh_connection and DATABASE_URL:
                # The final GCE/Cloud-Build guard runs in a worker thread. Do
                # not borrow the application's pooled asyncpg connections across
                # event loops; a short NullPool engine gives that thread its own
                # connection and is used only at a billable RPC boundary.
                from app.database import _engine_connect_args
                from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
                from sqlalchemy.pool import NullPool

                read_engine = create_async_engine(
                    DATABASE_URL,
                    poolclass=NullPool,
                    connect_args=_engine_connect_args(),
                )
                session_factory = async_sessionmaker(read_engine, expire_on_commit=False)
            async with session_factory() as read_session:
                state = await read_session.get(DeploymentState, 1)
        else:
            state = await session.get(DeploymentState, 1)
    except Exception:
        # Test/local installations without a configured database preserve the
        # existing in-memory behaviour.  A configured production database that
        # cannot be consulted must not permit new paid work.
        if not DATABASE_URL or "sqlite" in DATABASE_URL.lower():
            return is_managed_compute_enabled()
        logger.exception(
            "[MANAGED-COMPUTE] Could not read durable managed-compute setting; "
            "failing closed at a billing boundary"
        )
        return False
    finally:
        if read_engine is not None:
            await read_engine.dispose()

    config = getattr(state, "runtime_config_json", None) if state is not None else None
    if isinstance(config, dict) and isinstance(
        config.get(MANAGED_COMPUTE_SETTING), bool
    ):
        return bool(config[MANAGED_COMPUTE_SETTING])
    return is_managed_compute_enabled()


async def assert_managed_compute_enabled_authoritative(
    backend: str,
    *,
    session=None,
) -> None:
    """Raise unless the durable fleet-wide billing boundary permits a launch."""

    if not await is_managed_compute_enabled_authoritative(session=session):
        raise ManagedComputeDisabled(managed_compute_disabled_message(backend))


def local_only_execution_message(backend: str) -> str:
    return (
        f"project execution placement is local_only; refusing new {backend} "
        "execution"
    )


async def _task_is_local_only_authoritative(
    task_id: object | None,
    *,
    fresh_connection: bool = False,
) -> bool:
    """Read the task's current project policy from durable state.

    This is intentionally a final launch fence rather than a cached dispatch
    hint.  A missing task remains backward-compatible for taskless pool and
    maintenance VMs.  A persistence failure on a configured production database
    fails closed because it would otherwise turn an unreadable local-only policy
    into a paid cloud launch.
    """

    if task_id is None:
        return False

    from app.database import AsyncSessionLocal, DATABASE_URL
    from app.models import Project, Task
    from app.resource_envelope import resolve_project_execution_placement_policy

    read_engine = None
    try:
        session_factory = AsyncSessionLocal
        if fresh_connection and DATABASE_URL:
            from app.database import _engine_connect_args
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
            from sqlalchemy.pool import NullPool

            read_engine = create_async_engine(
                DATABASE_URL,
                poolclass=NullPool,
                connect_args=_engine_connect_args(),
            )
            session_factory = async_sessionmaker(read_engine, expire_on_commit=False)
        async with session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return bool(DATABASE_URL and "sqlite" not in DATABASE_URL.lower())
            project = await session.get(Project, task.project_id)
            if project is None:
                return bool(DATABASE_URL and "sqlite" not in DATABASE_URL.lower())
            return (
                resolve_project_execution_placement_policy(project) == "local_only"
            )
    except Exception:
        if not DATABASE_URL or "sqlite" in DATABASE_URL.lower():
            # Unit/local environments may have no durable project row for a
            # synthetic task id.  Preserve their existing non-managed launch
            # tests while production remains fail-closed.
            return False
        logger.exception(
            "[MANAGED-COMPUTE] Could not read durable project placement; "
            "failing closed at a billing boundary"
        )
        return True
    finally:
        if read_engine is not None:
            await read_engine.dispose()


async def assert_task_managed_execution_allowed_authoritative(
    backend: str,
    task_id: object | None,
) -> None:
    """Reject a managed launch if the global or task-level fence forbids it."""

    await assert_managed_compute_enabled_authoritative(backend)
    if await _task_is_local_only_authoritative(task_id):
        raise LocalOnlyExecutionPlacement(local_only_execution_message(backend))


def assert_managed_compute_enabled_authoritative_sync(backend: str) -> None:
    """Synchronous counterpart for billing RPCs executed in worker threads.

    GCE insertion and Cloud Build submission are synchronous client APIs.  They
    run in worker threads, where running a short, fresh event loop is safe and
    lets their final guard read the same durable setting as async paths.  Calling
    this from an event-loop thread would deadlock, so retain the safe cached
    guard there; production call sites are deliberately thread-bound.
    """

    if not is_managed_compute_enabled():
        raise ManagedComputeDisabled(managed_compute_disabled_message(backend))
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        enabled = asyncio.run(
            is_managed_compute_enabled_authoritative(fresh_connection=True)
        )
    else:
        logger.error(
            "[MANAGED-COMPUTE] synchronous authoritative guard called from an "
            "event-loop thread for %s; using fail-safe cached boundary",
            backend,
        )
        enabled = False
    if not enabled:
        raise ManagedComputeDisabled(managed_compute_disabled_message(backend))


def assert_task_managed_execution_allowed_authoritative_sync(
    backend: str,
    task_id: object | None,
) -> None:
    """Thread-safe final fence for synchronous GCE/Cloud Build RPCs."""

    assert_managed_compute_enabled_authoritative_sync(backend)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        local_only = asyncio.run(
            _task_is_local_only_authoritative(task_id, fresh_connection=True)
        )
    else:
        logger.error(
            "[MANAGED-COMPUTE] synchronous task placement guard called from an "
            "event-loop thread for %s; failing closed",
            backend,
        )
        local_only = True
    if local_only:
        raise LocalOnlyExecutionPlacement(local_only_execution_message(backend))


def managed_compute_denial_snapshot(*, backend: str) -> dict[str, Any]:
    """Return durable audit metadata for a dispatch denied by this boundary."""

    return {
        "reason": MANAGED_COMPUTE_DISABLED_REASON,
        "backend": backend,
        "setting": MANAGED_COMPUTE_SETTING,
        "enabled": False,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
