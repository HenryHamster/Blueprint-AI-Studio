"""Stranded-sidecar recovery sweep.

Extracted from ``app.recovery_post_commit`` to keep each module below its size
cap. Holds the sidecar-sweep constants and ``_sweep_stranded_sidecar_sources``.

Every public name is re-exported from ``app.recovery_post_commit`` (and in turn
from ``app.recovery_sweeps``) so existing call sites and test patch targets keep
resolving unchanged.
"""

import logging

from sqlalchemy import func
from sqlalchemy.orm import load_only
from sqlmodel import select

from app import services
from app.database import resolve_dialect_name
from app.models import Task, TaskStatus

logger = logging.getLogger(__name__)

# ── Stranded-sidecar recovery sweep ───────────────────────────────────────────
_AWAITING_SIDECAR_KEYS = (
    "awaiting_quality_grader",
    "awaiting_adversarial_grader",
    "awaiting_safeguard_challenge",
    "awaiting_finding_challenge",
    "awaiting_review_second_opinion",
)
_SIDECAR_VERDICT_APPLICATION_PENDING_KEY = "sidecar_verdict_application_pending"
_SIDECAR_SWEEP_MAX_PER_CYCLE = 50


async def _sweep_stranded_sidecar_sources() -> None:
    """Unstrand EVALUATING source tasks whose side-car challenger died silently.

    Side-car challengers (quality grader, safeguard challenge, finding challenge,
    review second-opinion) normally resolve their source task by POSTing terminal
    status to the agent status-update route.  A side-car that never posts — reaper
    kill, VM death after retries exhausted, cancellation — leaves the source
    parked EVALUATING indefinitely.

    This sweep queries EVALUATING tasks whose awaiting_* key holds a UUID string
    (post-spawn; initial value is ``True``), loads each referenced sidecar, and,
    if the sidecar is terminal (FAILED/CANCELLED), calls the lane resolver via
    ``trigger_stranded_sidecar_resolution``, which constructs a synthetic payload
    and invokes the resolver with existing fail-closed semantics. It also retries
    terminal source tasks that already recorded a side-car verdict but still carry
    the durable application-pending marker.
    """
    import uuid as _uuid

    from app.task_lifecycle import trigger_stranded_sidecar_resolution

    _TERMINAL = {TaskStatus.FAILED, TaskStatus.CANCELLED}

    try:
        async with services.AsyncSessionLocal() as session:
            result = await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.status,
                        Task.context,
                        raiseload=True,
                    )
                )
                .where(Task.status == TaskStatus.EVALUATING)
                .limit(_SIDECAR_SWEEP_MAX_PER_CYCLE)
            )
            evaluating = result.scalars().all()

            # Pre-collect sidecar UUIDs so we can batch-load them in one
            # query instead of issuing a session.get per source task (N+1).
            _sidecar_uuids_to_fetch: list = []
            for _src in evaluating:
                _ctx = _src.context or {}
                for _key in _AWAITING_SIDECAR_KEYS:
                    _ref = _ctx.get(_key)
                    if not _ref or _ref is True:
                        continue
                    try:
                        _sidecar_uuids_to_fetch.append(_uuid.UUID(str(_ref)))
                    except (ValueError, TypeError):
                        continue
                    break
            sidecars_by_id: dict = {}
            if _sidecar_uuids_to_fetch:
                _sidecar_rows = await session.execute(
                    select(Task).where(Task.id.in_(_sidecar_uuids_to_fetch))
                )
                sidecars_by_id = {s.id: s for s in _sidecar_rows.scalars().all()}

            resolved = 0
            for source in evaluating:
                ctx = source.context or {}
                for key in _AWAITING_SIDECAR_KEYS:
                    ref = ctx.get(key)
                    if not ref or ref is True:
                        continue
                    try:
                        sidecar_uuid = _uuid.UUID(str(ref))
                    except (ValueError, TypeError):
                        continue
                    sidecar = sidecars_by_id.get(sidecar_uuid)
                    if sidecar is None or sidecar.status not in _TERMINAL:
                        continue
                    logger.warning(
                        "[SIDECAR-RECOVERY] Source %s stranded EVALUATING "
                        "(key=%s sidecar=%s status=%s) — triggering fail-closed resolution",
                        source.id,
                        key,
                        sidecar.id,
                        sidecar.status,
                    )
                    try:
                        await trigger_stranded_sidecar_resolution(sidecar, session)
                        resolved += 1
                    except Exception:
                        logger.exception(
                            "[SIDECAR-RECOVERY] Resolver failed for sidecar %s / source %s",
                            sidecar.id,
                            source.id,
                        )
                    break  # one sidecar per source per sweep pass
            if resolved:
                logger.info(
                    "[SIDECAR-RECOVERY] Resolved %d stranded source task(s)", resolved
                )
            if len(evaluating) >= _SIDECAR_SWEEP_MAX_PER_CYCLE:
                logger.warning(
                    "[SIDECAR-RECOVERY] Hit per-cycle cap of %d stranded sources — "
                    "remainder deferred to next sweep",
                    _SIDECAR_SWEEP_MAX_PER_CYCLE,
                )

            if resolve_dialect_name(session) == "postgresql":
                pending_marker_present = Task.context.has_key(
                    _SIDECAR_VERDICT_APPLICATION_PENDING_KEY
                )
            else:
                pending_marker_present = func.json_extract(
                    Task.context,
                    f"$.{_SIDECAR_VERDICT_APPLICATION_PENDING_KEY}",
                ).isnot(None)

            pending_result = await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.status,
                        Task.archived,
                        Task.project_id,
                        Task.context,
                        raiseload=True,
                    )
                )
                .where(
                    Task.status == TaskStatus.COMPLETED,
                    Task.archived.is_(False),
                    pending_marker_present,
                )
            )
            terminal_sources = pending_result.scalars().all()
            retried = 0
            for source in terminal_sources:
                ctx = source.context or {}
                marker = ctx.get(_SIDECAR_VERDICT_APPLICATION_PENDING_KEY)
                if not isinstance(marker, dict):
                    continue
                sidecar_ref = marker.get("sidecar_task_id")
                try:
                    sidecar_uuid = _uuid.UUID(str(sidecar_ref))
                except (ValueError, TypeError):
                    continue
                sidecar = await session.get(Task, sidecar_uuid)
                if sidecar is None or sidecar.status not in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }:
                    continue
                logger.warning(
                    "[SIDECAR-RECOVERY] Source %s has unapplied side-car verdict "
                    "(lane=%s sidecar=%s status=%s) — retrying application",
                    source.id,
                    marker.get("lane"),
                    sidecar.id,
                    sidecar.status,
                )
                # Capture ids as plain strings BEFORE resolution. The resolver's
                # best-effort sub-op rollbacks (and the connection-drop path)
                # expire every ORM instance even under expire_on_commit=False, so
                # a post-failure ``source.id`` / ``sidecar.id`` read fires a sync
                # expired-load inside the async session -> MissingGreenlet, which
                # masks the real error AND aborts the rest of the batch.
                _sidecar_id_str = str(sidecar.id)
                _source_id_str = str(source.id)
                try:
                    await trigger_stranded_sidecar_resolution(sidecar, session)
                    retried += 1
                except Exception:
                    logger.exception(
                        "[SIDECAR-RECOVERY] Application retry failed for sidecar %s / source %s",
                        _sidecar_id_str,
                        _source_id_str,
                    )
                    # Roll back the (possibly failed-transaction) session so the
                    # next source in the batch starts clean rather than cascading
                    # PendingRollbackError through the remaining iterations.
                    try:
                        await session.rollback()
                    except Exception:
                        logger.debug(
                            "[SIDECAR-RECOVERY] post-failure rollback failed",
                            exc_info=True,
                        )
            if retried:
                logger.info(
                    "[SIDECAR-RECOVERY] Retried %d unapplied side-car verdict(s)",
                    retried,
                )
    except Exception:
        logger.exception("[SIDECAR-RECOVERY] Stranded-sidecar sweep failed")
