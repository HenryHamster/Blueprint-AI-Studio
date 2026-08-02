"""Stranded-sidecar recovery sweep.

Extracted from ``app.recovery_post_commit`` to keep each module below its size
cap. Holds the sidecar-sweep constants and ``_sweep_stranded_sidecar_sources``.

Every public name is re-exported from ``app.recovery_post_commit`` (and in turn
from ``app.recovery_sweeps``) so existing call sites and test patch targets keep
resolving unchanged.
"""

import logging
import uuid

from sqlalchemy import func
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app import services
from app.database import resolve_dialect_name
from app.models import Task, TaskStatus

logger = logging.getLogger(__name__)

# ── Stranded-sidecar recovery sweep ───────────────────────────────────────────
_AWAITING_SIDECAR_KEYS = (
    "awaiting_quality_grader",
    "awaiting_adversarial_grader",
    "awaiting_finding_challenge",
)
_SIDECAR_VERDICT_APPLICATION_PENDING_KEY = "sidecar_verdict_application_pending"
_SIDECAR_SWEEP_MAX_PER_CYCLE = 50


async def _clear_stranded_pending_marker(
    session, *, source_id: str, sidecar_id: str
) -> bool:
    """Authoritatively clear a source's application-pending marker after resolution.

    Called after ``trigger_stranded_sidecar_resolution`` returns *without
    raising*. When the resolver terminally concludes "nothing left to apply" —
    the side-car verdict was already resolved / this is a replayed
    terminal status, or the resolver's own view of the source (derived from the
    sidecar's SOURCE_KEY) diverged from the sweep-selected one — it returns
    normally but does NOT clear the durable
    ``_SIDECAR_VERDICT_APPLICATION_PENDING_KEY`` marker on THIS (sweep-selected)
    source.  Without this the sweep re-selects, re-logs
    (``… has unapplied side-car verdict … — retrying application``), and
    re-counts the same ~handful of sources on every ~2-min cycle forever — the
    dominant orchestrator log-volume wedge.

    Re-reads the row FRESH by its plain-string id rather than trusting the
    sweep's in-memory ``source``: the resolver ran on this same session and has
    committed (and, on its best-effort sub-op paths, may have rolled back), and
    ``session.rollback()`` expires every ORM instance even under
    ``expire_on_commit=False`` — a stale ``source.context`` read would fire a
    sync expired-load inside the async session and raise MissingGreenlet.

    Only clears when the marker still references the SAME sidecar we just
    processed; a marker that now points at a freshly-spawned challenger is a
    legitimately-pending application and is left untouched.  Never raises — a
    cleanup failure self-heals on the next sweep.

    Returns True iff a stale marker was cleared.
    """
    try:
        source_uuid = uuid.UUID(source_id)
    except (ValueError, TypeError):
        return False
    try:
        refreshed = (
            (
                await session.execute(
                    select(Task)
                    .options(load_only(Task.id, Task.context, raiseload=True))
                    .where(Task.id == source_uuid)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )
        if refreshed is None:
            return False
        ctx = dict(refreshed.context or {})
        marker = ctx.get(_SIDECAR_VERDICT_APPLICATION_PENDING_KEY)
        if not isinstance(marker, dict) or marker.get("sidecar_task_id") != sidecar_id:
            return False
        ctx.pop(_SIDECAR_VERDICT_APPLICATION_PENDING_KEY, None)
        refreshed.context = ctx
        flag_modified(refreshed, "context")
        session.add(refreshed)
        await session.commit()
        return True
    except Exception:
        logger.debug(
            "[SIDECAR-RECOVERY] pending-marker cleanup failed for source %s",
            source_id,
            exc_info=True,
        )
        try:
            await session.rollback()
        except Exception:
            logger.debug(
                "[SIDECAR-RECOVERY] pending-marker cleanup rollback failed",
                exc_info=True,
            )
        return False


async def _sweep_stranded_sidecar_sources() -> None:
    """Unstrand EVALUATING source tasks whose side-car challenger died silently.

    Side-car challengers (quality graders and finding challenges) normally
    resolve their source task by POSTing terminal
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

            # Snapshot (source id, marker dict, side-car uuid) into plain values
            # up front. A per-source resolution failure below calls
            # session.rollback(), which expires EVERY session-bound ORM instance
            # regardless of expire_on_commit=False; iterating the terminal_sources
            # ORM objects after that would fire a sync lazy-load on source.context
            # inside the AsyncSession -> MissingGreenlet, propagating to the outer
            # handler and aborting the whole sweep. Plain locals never expire; the
            # side-car ORM (which is passed to the resolver) is re-fetched fresh
            # each iteration so it too is unexpired after any prior rollback. The
            # `marker` dict was extracted from source.context before any rollback,
            # so the reference we hold stays valid. (This supersedes the WI-1
            # batch-load of sidecars, whose ORM rows the rollback would also
            # expire; per-iteration session.get re-fetches inside the greenlet.)
            _pending_sources: list = []
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
                _pending_sources.append((source.id, marker, sidecar_uuid))

            retried = 0
            cleared = 0
            # Batch-fetch terminal status for all pending sidecars in one query
            # instead of N individual session.get calls.
            _terminal_statuses = {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
            _all_sidecar_uuids = [su for _, _, su in _pending_sources]
            _terminal_sidecar_ids: set = set()
            if _all_sidecar_uuids:
                _terminal_rows = (
                    await session.execute(
                        select(Task.id).where(
                            Task.id.in_(_all_sidecar_uuids),
                            Task.status.in_(list(_terminal_statuses)),
                        )
                    )
                ).scalars().all()
                _terminal_sidecar_ids = set(_terminal_rows)
            for _source_id, marker, sidecar_uuid in _pending_sources:
                if sidecar_uuid not in _terminal_sidecar_ids:
                    continue
                # Re-fetch via session.get so the object is fresh after any
                # prior rollback (rollbacks expire all cached ORM instances).
                sidecar = await session.get(
                    Task, sidecar_uuid, populate_existing=True
                )
                if sidecar is None or sidecar.status not in _terminal_statuses:
                    continue
                logger.warning(
                    "[SIDECAR-RECOVERY] Source %s has unapplied side-car verdict "
                    "(lane=%s sidecar=%s status=%s) — retrying application",
                    _source_id,
                    marker.get("lane"),
                    sidecar.id,
                    sidecar.status,
                )
                # Capture ids as plain strings BEFORE resolution. The resolver's
                # best-effort sub-op rollbacks (and the connection-drop path)
                # expire every ORM instance even under expire_on_commit=False, so
                # a post-failure ``sidecar.id`` read fires a sync expired-load
                # inside the async session -> MissingGreenlet, which masks the
                # real error AND aborts the rest of the batch.
                _sidecar_id_str = str(sidecar.id)
                _source_id_str = str(_source_id)
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
                    # Resolver raised — the verdict is genuinely unapplied; keep
                    # the marker so a later sweep retries. Do NOT clear here.
                    continue
                # Resolver returned WITHOUT raising: it either applied the verdict
                # (and cleared the marker itself) or terminally concluded there is
                # nothing left to apply (already-resolved / replayed-terminal /
                # source diverged) and left the marker set. Authoritatively clear
                # the sweep-selected source's marker in the latter case so it is
                # never re-stranded and re-logged forever.
                if await _clear_stranded_pending_marker(
                    session,
                    source_id=_source_id_str,
                    sidecar_id=_sidecar_id_str,
                ):
                    cleared += 1
            if retried:
                logger.info(
                    "[SIDECAR-RECOVERY] Retried %d unapplied side-car verdict(s)",
                    retried,
                )
            if cleared:
                logger.info(
                    "[SIDECAR-RECOVERY] Cleared %d stale application-pending "
                    "marker(s) the resolver left set on already-resolved sources",
                    cleared,
                )
    except Exception:
        logger.exception("[SIDECAR-RECOVERY] Stranded-sidecar sweep failed")
