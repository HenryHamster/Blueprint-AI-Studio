"""Application lifecycle management — startup, shutdown, and background-task monitoring.

Extracted from app.main to reduce module size.
"""

import asyncio
import ctypes
import logging
import os
import warnings
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Dict, List

from sqlalchemy import func as _func
from sqlalchemy import text as _text
from sqlalchemy.orm import load_only
from sqlmodel import select

import app.shared_state as _shared_state
from app.database import AsyncSessionLocal, engine, init_db
from app.models import (
    AllowedUser,
    Alert,
    AlertSeverity,
    Project,
)
from app.time_utils import _utcnow

# Session-scoped advisory lock ID for background-leader election.
# Stored as signed int64 (PostgreSQL bigint) via two's-complement conversion.
_BACKGROUND_LOCK_ID: int = ctypes.c_int64(0xB1EB1EB1EB1EB1EB).value

# pg_locks records a single-bigint advisory lock as three columns: classid =
# high 32 bits of the key, objid = low 32 bits, objsubid = 1. The *unsigned*
# 64-bit key (0xB1EB…) exceeds bigint's max, so the stale-leader reaper matches
# on the two unsigned 32-bit halves rather than reconstructing the whole key.
_BACKGROUND_LOCK_CLASSID: int = (_BACKGROUND_LOCK_ID & 0xFFFFFFFFFFFFFFFF) >> 32
_BACKGROUND_LOCK_OBJID: int = _BACKGROUND_LOCK_ID & 0xFFFFFFFF

# Stale-leader reap threshold. A live leader re-asserts the advisory lock every
# LEADER_KEEPALIVE_SECONDS (default 30s; see lifespan), so its lock-holding
# backend's pg_stat_activity.state_change is never older than one interval. A
# holder older than this has missed several keepalives — it is a leader that was
# SIGKILLed mid-wedge (graceful lock release never ran) or whose event loop is
# hung — so the non-leader watchdog reaps it and leadership recovers in one
# probe cycle instead of waiting on Postgres' passive TCP death detection
# (minutes), or, for a committed-idle wedged process, never. Default 3x the
# keepalive interval so a live leader is provably never reaped.
_LEADER_STALE_REAP_SECONDS: float = float(os.getenv("LEADER_STALE_REAP_SECONDS", "90"))

# Background-service crash-episode escalation. When a single named background
# service crashes >= _BACKGROUND_CRASH_EPISODE_THRESHOLD times inside
# _BACKGROUND_CRASH_EPISODE_WINDOW_SECONDS it is a crash *episode* (a flapping
# loop, not a one-off blip). The monitor emits a deduped ERROR Alert for the
# episode — operator visibility only. This replaced the removed
# ``_spawn_crash_investigations`` apparatus: no autonomous investigate task is
# spawned. The threshold is kept >= the reaper's per-task quarantine streak
# limit so a single poison-pill task is quarantined before its repeated reaper
# crashes can trip an episode (see test_orphan_streak_limit_at_most_episode_threshold).
_BACKGROUND_CRASH_EPISODE_THRESHOLD: int = 3
_BACKGROUND_CRASH_EPISODE_WINDOW_SECONDS: int = 600

logger = logging.getLogger("blueprint")


def _record_crash_and_count(
    crash_timestamps: Dict[str, List[datetime]],
    name: str,
    now_ts: datetime,
    window_seconds: int = _BACKGROUND_CRASH_EPISODE_WINDOW_SECONDS,
) -> int:
    """Append ``now_ts`` to the per-service crash window and prune stale entries.

    Mutates ``crash_timestamps[name]`` in place: the new crash is recorded and
    any timestamp older than ``window_seconds`` is dropped, so a slow trickle of
    unrelated restarts never accumulates into a false episode. Returns the count
    of crashes remaining inside the rolling window (i.e. the value the caller
    compares against ``_BACKGROUND_CRASH_EPISODE_THRESHOLD``).

    Pure bookkeeping — no DB, no I/O. Shared by ``_monitor_background_tasks`` and
    its unit tests so both exercise the same window logic.
    """
    cutoff = now_ts - timedelta(seconds=window_seconds)
    ts_list = crash_timestamps.setdefault(name, [])
    ts_list.append(now_ts)
    crash_timestamps[name] = [ts for ts in ts_list if ts >= cutoff]
    return len(crash_timestamps[name])


async def _emit_crash_alert(
    session_factory,
    name: str,
    cause_str: str,
) -> int:
    """Emit a deduped ``AlertSeverity.ERROR`` per-crash Alert for every project.

    Uses a single batched query to check for existing open alerts rather than
    one query per project. Returns the number of Alert rows actually created.
    """
    alert_title = f"Background service crashed: {name}"
    async with session_factory() as alert_session:
        project_ids = (
            (await alert_session.execute(select(Project.id))).scalars().all()
        )
        if not project_ids:
            return 0
        already_alerted = set(
            (
                await alert_session.execute(
                    select(Alert.project_id).where(
                        Alert.project_id.in_(project_ids),
                        Alert.title == alert_title,
                        Alert.dismissed_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        created = 0
        for project_id in project_ids:
            if project_id in already_alerted:
                continue
            alert_session.add(
                Alert(
                    project_id=project_id,
                    severity=AlertSeverity.ERROR,
                    title=alert_title,
                    message=f"Background service '{name}' crashed with: {cause_str[:300]}. Auto-restarting with backoff.",
                )
            )
            created += 1
        await alert_session.commit()
    return created


async def _emit_crash_episode_alert(
    session_factory,
    name: str,
    crash_count: int,
    window_seconds: int = _BACKGROUND_CRASH_EPISODE_WINDOW_SECONDS,
) -> int:
    """Emit ONE deduped ``AlertSeverity.ERROR`` crash-episode Alert per project.

    Operator-visibility only — this is the survivor of the removed
    ``_spawn_crash_investigations`` apparatus, so it spawns no investigate Task.
    Existing undismissed alerts with the same title are skipped (dedup). Returns
    the number of Alert rows actually created (0 when every project already had
    an open episode alert), which keeps the path directly unit-testable.
    """
    episode_title = f"Background service crash episode: {name}"
    created = 0
    async with session_factory() as ep_session:
        ep_project_ids = (await ep_session.execute(select(Project.id))).scalars().all()
        if not ep_project_ids:
            return 0
        existing_alerts = (
            (
                await ep_session.execute(
                    select(Alert.project_id).where(
                        Alert.project_id.in_(ep_project_ids),
                        Alert.title == episode_title,
                        Alert.dismissed_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        alerted_project_ids = set(existing_alerts)
        for project_id in ep_project_ids:
            if project_id in alerted_project_ids:
                continue
            ep_session.add(
                Alert(
                    project_id=project_id,
                    severity=AlertSeverity.ERROR,
                    title=episode_title,
                    message=(
                        f"Background service '{name}' crashed "
                        f"{crash_count} times in the "
                        f"last {window_seconds // 60} "
                        "minutes — this is a crash episode (flapping), "
                        "not a one-off blip. Auto-restart with backoff is "
                        "still running; investigate the root cause."
                    ),
                )
            )
            created += 1
        await ep_session.commit()
    return created


def _evaluate_reaper_stall(
    *,
    is_halted: bool,
    last_progress: datetime | None,
    now: datetime,
    stall_threshold: float,
) -> tuple[bool, str | None]:
    """Decide whether the reaper watchdog should cancel the reaper task.

    Mirrors the probe in ``_monitor_background_tasks``: a HALTED reaper is never
    cancelled (module-level halt state persists across asyncio.Task restarts, so
    re-spawning only yields another halted reaper). A non-halted reaper is
    cancelled only when its freshest progress heartbeat is older than
    ``stall_threshold`` seconds. Returns ``(should_cancel, reason)``.

    Pure decision logic — no asyncio, no module-global reads — so it can be unit
    tested directly and reused by the live probe.
    """
    if is_halted:
        return False, None
    if last_progress is None:
        # Never recorded progress yet — give the fresh reaper time to start.
        return False, None
    age = (now - last_progress).total_seconds()
    if age > stall_threshold:
        return True, f"no progress heartbeat in {int(age)}s"
    return False, None


async def _seed_initial_admin():
    """Seed the AllowedUser table with an initial admin if empty."""
    async with AsyncSessionLocal() as seed_session:
        count = (
            await seed_session.execute(select(_func.count()).select_from(AllowedUser))
        ).scalar_one()
        if count == 0:
            initial_email = os.getenv("INITIAL_ADMIN_EMAIL")
            if initial_email:
                seed_session.add(AllowedUser(email=initial_email, role="admin"))
                await seed_session.commit()
                logger.info(f"[STARTUP] Seeded initial admin: {initial_email}")
            else:
                logger.warning(
                    "[STARTUP] AllowedUser table is empty and INITIAL_ADMIN_EMAIL not set. No users can sign in."
                )


async def _seed_completion_debt_inventory():
    """Seed the completion-debt ledger from the HEAD dormant-state inventory.

    Best-effort and idempotent (dedupe_key upserts): a seed failure must never
    abort startup, and repeated restarts never create duplicate rows.
    """
    try:
        from app.completion_debt import aseed_completion_debt_inventory

        async with AsyncSessionLocal() as seed_session:
            created = await aseed_completion_debt_inventory(seed_session)
            await seed_session.commit()
        if created:
            logger.info(
                "[STARTUP] Seeded %d completion-debt inventory row(s)", created
            )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[STARTUP] completion-debt inventory seed failed (non-fatal)",
            exc_info=True,
        )


async def _reconcile_dormant_knob_debts():
    """Reconcile dormant-knob debts against the current runtime config.

    Best-effort and idempotent: a reconciliation failure must never abort
    startup, and repeated startup calls should converge to the same ledger
    state even when knobs graduate outside the PATCH handler.
    """
    try:
        from app.completion_debt import areconcile_dormant_knob_debts

        async with AsyncSessionLocal() as seed_session:
            stats = await areconcile_dormant_knob_debts(seed_session)
            await seed_session.commit()
        if stats["opened"] or stats["resolved"]:
            logger.info(
                "[STARTUP] Reconciled dormant-knob debts (opened=%d resolved=%d)",
                stats["opened"],
                stats["resolved"],
            )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[STARTUP] dormant-knob debt reconciliation failed (non-fatal)",
            exc_info=True,
        )


async def _seed_completion_debt_design_doc():
    """Seed the completion-debt design-doc section for all projects.

    Best-effort and idempotent: a seed failure on one project must never block
    startup or other projects. Repeated startup calls never duplicate or churn
    code-authored sections.
    """
    try:
        from app.completion_debt import seed_completion_debt_design_doc_section

        async with AsyncSessionLocal() as seed_session:
            project_ids = (
                (await seed_session.execute(select(Project.id))).scalars().all()
            )
            seeded = 0
            for project_id in project_ids:
                try:
                    if await seed_completion_debt_design_doc_section(
                        seed_session, project_id
                    ):
                        seeded += 1
                        await seed_session.commit()
                except Exception:  # noqa: BLE001
                    await seed_session.rollback()
                    logger.warning(
                        "[STARTUP] completion-debt design-doc seed failed for project %s (non-fatal)",
                        project_id,
                        exc_info=True,
                    )
            if seeded:
                logger.info(
                    "[STARTUP] Seeded completion-debt design-doc section for %d project(s)",
                    seeded,
                )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[STARTUP] completion-debt design-doc seed failed (non-fatal)",
            exc_info=True,
        )


async def _seed_routing_verdict_from_snapshot():
    """Seed in-memory ``provider_routing_verdict_state`` from the persisted snapshot.

    ``provider_routing_verdict_state`` in ``runtime_config`` is only set by
    ``compute_current_axes`` during the periodic snapshot write. On restart the
    key is absent, so ``_apply_routing_verdict()`` treats it as
    INSUFFICIENT_DATA — disabling DeepSeek preference for up to 24 hours.
    This function restores the latest persisted verdict from
    ``project.design_doc["efficiency_baseline"]`` (baseline, snapshot, or
    container level — in that preference order) immediately on startup.

    Best-effort and non-fatal: a seed failure must never abort startup.
    """
    try:
        from app.shared_state import runtime_config as _rc
        from app.efficiency_snapshot import DESIGN_DOC_KEY

        async with AsyncSessionLocal() as seed_session:
            project_rows = (
                (
                    await seed_session.execute(
                        select(Project).options(load_only(Project.id, Project.design_doc, raiseload=True))
                    )
                )
                .scalars()
                .all()
            )
            for project in project_rows:

                if not project or not project.design_doc:
                    continue
                container = project.design_doc.get(DESIGN_DOC_KEY)
                if not container:
                    continue
                # Read the verdict from where write_snapshot actually stores it.
                # Priority (newest-first):
                #   1. snapshots[-1]["provider_routing_verdict"] — latest snapshot,
                #   2. baseline["provider_routing_verdict"] — first-ever snapshot
                #      (fallback when no snapshots exist yet),
                #   3. container-level key (backward compat with data seeded at
                #      the wrong level before the fix).
                provider_routing_verdict = None
                snapshots = container.get("snapshots") or []
                if snapshots:
                    provider_routing_verdict = snapshots[-1].get(
                        "provider_routing_verdict"
                    )
                if not provider_routing_verdict:
                    provider_routing_verdict = (container.get("baseline") or {}).get(
                        "provider_routing_verdict"
                    )
                if not provider_routing_verdict:
                    provider_routing_verdict = container.get(
                        "provider_routing_verdict"
                    )
                if not provider_routing_verdict:
                    continue
                state = provider_routing_verdict.get("state")
                if state:
                    _rc["provider_routing_verdict_state"] = state
                    logger.info(
                        "[STARTUP] Seeded provider_routing_verdict_state=%s from project %s",
                        state,
                        project.id,
                    )
                    return  # First project with a verdict is sufficient
    except Exception:
        logger.warning(
            "[STARTUP] provider_routing_verdict_state seed failed (non-fatal)",
            exc_info=True,
        )


async def _leader_lock_keepalive_tick(leader_conn) -> str:
    """Run one leader-lock keepalive iteration and report the outcome.

    The background-leader advisory lock is SESSION-scoped: it lives on the
    pinned ``leader_conn`` AsyncConnection for the whole process lifetime. If
    that connection dies (network blip, Cloud SQL failover, idle TCP reaped, an
    ``idle_in_transaction``/statement timeout), Postgres releases the lock and a
    peer's non-leader watchdog grabs it — which forces this instance into the
    "prior leader died" respawn loop. This tick keeps the connection warm and,
    on failure, tries to re-acquire the lock on the same connection.

    Always COMMITs after touching the session so the connection is left merely
    *idle* (lock still held) rather than *idle in transaction* — the latter is
    what ``idle_in_transaction_session_timeout`` kills.

    Returns one of:
      * ``"held"``       — ping succeeded, the lock is still held;
      * ``"reacquired"`` — ping failed but the lock was re-acquired on the same
        connection after rollback (a transient blip, no peer took over);
      * ``"lost"``       — ping failed and a peer now owns the lock;
      * ``"error"``      — ping failed and the re-acquire attempt also errored.

    The caller hard-exits on ``"lost"``/``"error"`` so Cloud Run respawns this
    instance through clean leader election rather than running on lost
    leadership. Kept module-level (not a closure) so it is unit-testable.
    """
    try:
        await leader_conn.execute(_text("SELECT 1"))
        await leader_conn.commit()
        return "held"
    except Exception as ka_exc:  # any failure here means the connection is suspect
        logger.warning(
            "[MONITOR] Leader-lock keepalive ping failed (%s); re-acquiring lock",
            ka_exc,
        )
    # Ping failed → the connection (and the session-scoped lock) is likely gone.
    # Best-effort rollback to reset transaction state before re-acquire attempt.
    try:
        await leader_conn.rollback()
    except Exception as rb_exc:  # best-effort connection reset
        logger.debug("[MONITOR] keepalive rollback no-op: %s", rb_exc)
    try:
        reacquired = bool(
            (
                await leader_conn.execute(
                    _text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": _BACKGROUND_LOCK_ID},
                )
            ).scalar()
        )
        await leader_conn.commit()
    except Exception as reacq_exc:
        # Operational (DB unreachable), not a code fault — the message plus the
        # caller's hard-exit are the signal; a full traceback is just noise.
        logger.error("[MONITOR] Leader-lock re-acquire failed: %s", reacq_exc)  # noqa: TRY400
        return "error"
    return "reacquired" if reacquired else "lost"


async def _reap_stale_leader_lock_holder(
    probe_session, stale_secs: float = _LEADER_STALE_REAP_SECONDS
) -> int:
    """Forcibly free the background-leader lock from a dead/wedged holder.

    Called from the non-leader watchdog ONLY after a ``pg_try_advisory_lock``
    probe fails (the lock is held by a peer). Finds the backend holding THIS
    advisory lock (matched on the exact key) whose last activity
    (``pg_stat_activity.state_change``) is older than ``stale_secs`` — i.e. it
    has missed several keepalive ticks and is dead or wedged — and
    ``pg_terminate_backend``s it. Terminating the connection makes Postgres
    release the SESSION-scoped lock immediately, instead of the watchdog waiting
    on passive TCP death detection (minutes) or, for a committed-idle connection
    on a still-alive-but-hung process, forever.

    Returns the number of backends terminated (0 when the holder is live or the
    lock is genuinely free). Safe by construction: a live leader's holder always
    has a fresh ``state_change`` (it re-asserts every LEADER_KEEPALIVE_SECONDS)
    and ``stale_secs`` defaults to 3x that, so an active leader is never reaped.
    Kept module-level (not a closure) so it is unit-testable in isolation.
    """
    pids = (
        (
            await probe_session.execute(
                _text(
                    "SELECT a.pid FROM pg_locks l "
                    "JOIN pg_stat_activity a ON a.pid = l.pid "
                    "WHERE l.locktype = 'advisory' AND l.objsubid = 1 "
                    "AND l.classid::bigint = :classid AND l.objid::bigint = :objid "
                    "AND l.granted AND a.pid <> pg_backend_pid() "
                    "AND a.state_change < now() - (:stale_secs * interval '1 second')"
                ),
                {
                    "classid": _BACKGROUND_LOCK_CLASSID,
                    "objid": _BACKGROUND_LOCK_OBJID,
                    "stale_secs": stale_secs,
                },
            )
        )
        .scalars()
        .all()
    )
    reaped = 0
    for pid in pids:
        terminated = bool(
            (
                await probe_session.execute(
                    _text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
                )
            ).scalar()
        )
        if terminated:
            reaped += 1
            logger.warning(
                "[MONITOR] Reaped stale background-leader lock-holder pid=%s "
                "(idle/wedged > %.0fs); session-scoped advisory lock released — "
                "this watchdog will take over leadership on re-probe.",
                pid,
                stale_secs,
            )
    await probe_session.commit()
    return reaped


def _log_startup_deploy_identity() -> None:
    """Emit startup observability for the deploy-level identity.

    Logs INFO when resolved (deploy-level serving confirmation is active) and
    WARNING when unresolved (only merge-level confirmation is active).  Extracted
    so tests can exercise both branches without invoking the full startup sequence.
    """
    from app.deploy_identity import deploy_repo_identity as _deploy_repo_identity

    identity = _deploy_repo_identity()
    if identity is None:
        logger.warning(
            "[STARTUP] deploy_repo_identity is UNRESOLVED — neither "
            "DEPLOY_REPO_IDENTITY env var nor runtime_config['deploy_repo_identity'] "
            "is set.  Deploy-level serving confirmation (backend_revision, "
            "agent_image) is DISABLED for all projects; only merge-level "
            "confirmation (promotion_merge) is active.  Set "
            "runtime_config['deploy_repo_identity'] via PATCH /api/admin/settings "
            "to enable deploy-level confirmation."
        )
    else:
        logger.info(
            "[STARTUP] deploy_repo_identity resolved to %s/%s — deploy-level "
            "serving confirmation active for matching projects.",
            identity[0],
            identity[1],
        )


@asynccontextmanager
async def lifespan(app):
    """FastAPI lifespan context manager — startup and shutdown hooks."""
    # Root logger was never configured — named loggers propagated to a root with
    # no handlers, so every [ORCHESTRATOR]/[DEPLOY]/[HEALTH]/[MONITOR] line
    # vanished silently. Cloud Run captures stdout+stderr; force a basic
    # formatter+handler here so app logs surface even during startup hangs.
    if not logging.root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
            force=True,
        )
    # Route Python warnings (RuntimeWarning, DeprecationWarning, etc.) through the
    # logging system so they get a proper severity in Cloud Logging. Otherwise
    # they're written raw to stderr with no severity tag and end up classified as
    # INFO/DEFAULT, invisible to the orchestrator-errors dashboard which filters
    # severity>=ERROR. RuntimeWarnings in particular almost always indicate real
    # bugs (unawaited coroutines, sync code in async contexts) so they're elevated
    # to ERROR; everything else stays at WARNING.
    _warnings_logger = logging.getLogger("py.warnings")

    def _show_warning_via_logger(
        message, category, filename, lineno, file=None, line=None
    ):
        formatted = warnings.formatwarning(
            message, category, filename, lineno, line
        ).rstrip()
        if issubclass(category, RuntimeWarning):
            _warnings_logger.error(formatted)
        else:
            _warnings_logger.warning(formatted)

    warnings.showwarning = _show_warning_via_logger
    logger.info("[STARTUP] Logging configured — app logs now visible in Cloud Logging")

    # Late imports to avoid circular dependency with app.main
    from app.main import (
        _verify_schema_version,
        _verify_schema_drift,
        _init_deployment_state,
    )
    from app.notification import _send_webhook_notification
    from app.reaper import reaper_service
    from app.services import (
        account_availability_refresh_service,
        auto_archive_service,
        gcp_cleanup_service,
        gcp_image_cleanup_service,
        secret_reconciliation_service,
        stale_branch_cleanup_service,
    )
    from app.max_plan_pool import load_max_plan_pool
    from app.compute_jobs import compute_job_monitor_service
    from app.orchestrator import orchestrator_worker
    from app.shared_state import runtime_config_sync_service
    from app.github import close_github_client

    # Startup: Initialize DB tables
    await init_db()

    # Verify schema version matches expected migration
    await _verify_schema_version()

    # Verify the live schema actually has every table/column the models require
    # (the version number can be correct while the real schema is short — the
    # back-merge renumber class). Loud, specific failure instead of a cryptic
    # UndefinedColumnError deep in a later query.
    await _verify_schema_drift()

    # Seed initial admin if AllowedUser table is empty
    await _seed_initial_admin()

    # Load persisted Max plan accounts into in-memory pool
    await load_max_plan_pool()

    # Seed provider_routing_verdict_state from the latest persisted snapshot
    # so DeepSeek preference is restored immediately on restart, not gated on
    # the next periodic snapshot write (up to ~1h delay).
    await _seed_routing_verdict_from_snapshot()

    # Check deployment state (detect new deploy, rollback, or normal restart)
    await _init_deployment_state()

    # Seed the completion-debt ledger from the standing HEAD dormant-state
    # inventory (idempotent: dedupe_key upserts, so restarts never duplicate).
    await _seed_completion_debt_inventory()

    # Reconcile dormant-knob debt rows against the current runtime config so
    # env-var/redeploy graduations and live demotions converge immediately.
    await _reconcile_dormant_knob_debts()

    # Seed the completion-debt design-doc section for all projects (idempotent:
    # absent sections are seeded, existing sections only refresh code fields).
    await _seed_completion_debt_design_doc()

    # Log the resolved deploy-level identity so operators can confirm that
    # deploy-level serving confirmation (backend_revision, agent_image) is
    # active.  When unresolved, all promotions fall back to merge-level
    # confirmation only (promotion_merge); this WARNING surfaces the gap so
    # operators know to set runtime_config['deploy_repo_identity'] via
    # PATCH /api/admin/settings.
    _log_startup_deploy_identity()

    # --- Leader election via pg_try_advisory_lock (session-scoped) ---
    # Only one Cloud Run instance should run the leader-only background services.
    # We hold a raw AsyncConnection (not an AsyncSession) open for the entire
    # lifespan so the lock-holding database backend PID is STABLE across keepalive
    # commits. AsyncSession.commit() returns the underlying connection back to the
    # pool, meaning the next execute may run on a DIFFERENT connection — leaving the
    # lock-holding connection idle and vulnerable to _reap_stale_leader_lock_holder
    # terminating it as "stale" (false positive). AsyncConnection keeps the
    # connection checked out until close(), so commit() never changes the PID.
    leader_conn = None
    try:
        leader_conn = await engine.connect()
        _result = await leader_conn.execute(
            _text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": _BACKGROUND_LOCK_ID},
        )
        is_leader = bool(_result.scalar())
    except Exception:
        # Non-Postgres dialect (SQLite in tests) — default to leader so all
        # services start normally; close the dummy connection immediately.
        is_leader = True
        try:
            await leader_conn.close()
        except Exception as _conn_close_exc:
            logger.debug(
                "[STARTUP] Leader connection close (non-Postgres) no-op: %s",
                _conn_close_exc,
            )
        leader_conn = None

    if leader_conn is not None:
        # pg_try_advisory_lock is SESSION-scoped (not the _xact_ variant), so the
        # lock is held until the connection closes — it SURVIVES a COMMIT. Commit
        # now to close the transaction the SELECT auto-opened. Otherwise
        # leader_conn sits *idle in transaction* for the whole process
        # lifetime and Postgres' ``idle_in_transaction_session_timeout`` (60s, set
        # in database.py) terminates the connection — silently releasing the
        # advisory lock and triggering the "prior leader died" respawn loop. A
        # committed connection that is merely idle (no open transaction) is exempt
        # from that timeout, so the lock stays held. The keepalive task (started
        # below on the leader) keeps the connection warm and recovers the lock if
        # the connection is ever dropped anyway.
        try:
            await leader_conn.commit()
        except Exception as _lock_commit_exc:  # keepalive recovers a bad connection
            logger.warning(
                "[STARTUP] Leader lock-connection commit failed (continuing): %s",
                _lock_commit_exc,
            )

    _shared_state.is_leader = is_leader

    if not is_leader:
        # Release the lock-acquisition connection immediately — we did not win the
        # lock, so this connection is unused. Leaving it open would permanently hold
        # one connection from the pool for this instance's entire lifetime.
        if leader_conn is not None:
            try:
                await leader_conn.close()
            except Exception as _nl_conn_exc:
                logger.debug(
                    "[STARTUP] Non-leader: leader_conn close: %s", _nl_conn_exc
                )
            leader_conn = None
        logger.info(
            "[STARTUP] Not background-leader; skipping leader-only services (serving HTTP only)"
        )
        # Per-instance services still run on non-leader instances.
        from app.jwt_revocation import jwt_revocation_service
        from app.event_fanout import (
            agent_log_relay_service,
            event_fanout_service,
            publish_service,
        )
        from app.notification_dispatch import notification_retry_drain_service

        config_sync_task = asyncio.create_task(runtime_config_sync_service())
        jwt_revocation_task_nl = asyncio.create_task(jwt_revocation_service())
        event_fanout_task_nl = asyncio.create_task(event_fanout_service())
        agent_log_relay_task_nl = asyncio.create_task(agent_log_relay_service())
        publish_task_nl = asyncio.create_task(publish_service())
        notification_retry_task_nl = asyncio.create_task(
            notification_retry_drain_service()
        )
        from app.shared_state import background_tasks as _bg_tasks_nl

        _bg_tasks_nl["config_sync"] = config_sync_task
        _bg_tasks_nl["jwt_revocation"] = jwt_revocation_task_nl
        _bg_tasks_nl["event_fanout"] = event_fanout_task_nl
        _bg_tasks_nl["agent_log_relay"] = agent_log_relay_task_nl
        _bg_tasks_nl["publish"] = publish_task_nl
        _bg_tasks_nl["notification_retry_drain"] = notification_retry_task_nl

        # Minimal watchdog: restart per-instance services if they crash, AND
        # periodically probe the leader advisory lock so we can take over
        # when the prior leader dies. Without this probe, Cloud Run can sit
        # in a leadership gap indefinitely: instances that booted while a
        # leader existed picked the non-leader path and never retry, so
        # when the leader is killed (e.g. by a /health 503 from a stuck
        # background loop) the existing instances stay non-leader and no
        # orchestrator cycles run until a new instance happens to start.
        # Incident 2026-05-07: ~10 min leadership gap after liveness kill.
        async def _monitor_non_leader():
            while True:
                try:
                    await asyncio.sleep(30)
                    if config_sync_task.done():
                        exc = (
                            config_sync_task.exception()
                            if not config_sync_task.cancelled()
                            else None
                        )
                        logger.error(
                            f"[MONITOR] config_sync DIED on non-leader (exc={exc}); restarting"
                        )
                        _bg_tasks_nl["config_sync"] = asyncio.create_task(
                            runtime_config_sync_service()
                        )
                    if jwt_revocation_task_nl.done():
                        exc = (
                            jwt_revocation_task_nl.exception()
                            if not jwt_revocation_task_nl.cancelled()
                            else None
                        )
                        logger.error(
                            f"[MONITOR] jwt_revocation DIED on non-leader (exc={exc}); restarting"
                        )
                        _bg_tasks_nl["jwt_revocation"] = asyncio.create_task(
                            jwt_revocation_service()
                        )
                    if event_fanout_task_nl.done():
                        exc = (
                            event_fanout_task_nl.exception()
                            if not event_fanout_task_nl.cancelled()
                            else None
                        )
                        logger.error(
                            f"[MONITOR] event_fanout DIED on non-leader (exc={exc}); restarting"
                        )
                        _bg_tasks_nl["event_fanout"] = asyncio.create_task(
                            event_fanout_service()
                        )
                    if agent_log_relay_task_nl.done():
                        exc = (
                            agent_log_relay_task_nl.exception()
                            if not agent_log_relay_task_nl.cancelled()
                            else None
                        )
                        logger.error(
                            f"[MONITOR] agent_log_relay DIED on non-leader (exc={exc}); restarting"
                        )
                        _bg_tasks_nl["agent_log_relay"] = asyncio.create_task(
                            agent_log_relay_service()
                        )
                    if publish_task_nl.done():
                        exc = (
                            publish_task_nl.exception()
                            if not publish_task_nl.cancelled()
                            else None
                        )
                        logger.error(
                            f"[MONITOR] publish DIED on non-leader (exc={exc}); restarting"
                        )
                        _bg_tasks_nl["publish"] = asyncio.create_task(publish_service())
                    if notification_retry_task_nl.done():
                        exc = (
                            notification_retry_task_nl.exception()
                            if not notification_retry_task_nl.cancelled()
                            else None
                        )
                        logger.error(
                            f"[MONITOR] notification_retry_drain DIED on non-leader "
                            f"(exc={exc}); restarting"
                        )
                        _bg_tasks_nl["notification_retry_drain"] = asyncio.create_task(
                            notification_retry_drain_service()
                        )

                    # Leader-lock probe. Try to acquire on a fresh session;
                    # if we win, the previous leader's session has closed and
                    # we should be running the leader path. Release the lock
                    # immediately and hard-exit so Cloud Run respawns us
                    # through the lifespan startup, where the leader path is
                    # selected cleanly.
                    try:
                        async with AsyncSessionLocal() as _probe_session:
                            _probe_acquired = bool(
                                (
                                    await _probe_session.execute(
                                        _text("SELECT pg_try_advisory_lock(:lock_id)"),
                                        {"lock_id": _BACKGROUND_LOCK_ID},
                                    )
                                ).scalar()
                            )
                            # The first probe failed → the lock is held. If a
                            # stale (dead/wedged) leader still holds it and never
                            # released, reap it and retry the probe in THIS cycle
                            # so leadership recovers in one watchdog interval
                            # instead of waiting on Postgres' passive TCP death
                            # detection (minutes) — or, for a committed-idle
                            # wedged process, never.
                            if (
                                not _probe_acquired
                                and await _reap_stale_leader_lock_holder(_probe_session)
                            ):
                                _probe_acquired = bool(
                                    (
                                        await _probe_session.execute(
                                            _text(
                                                "SELECT pg_try_advisory_lock(:lock_id)"
                                            ),
                                            {"lock_id": _BACKGROUND_LOCK_ID},
                                        )
                                    ).scalar()
                                )
                            if _probe_acquired:
                                # Release before exiting so the respawned
                                # process (or a peer that probes during our
                                # shutdown) can acquire the lock without
                                # waiting for connection-close detection.
                                await _probe_session.execute(
                                    _text("SELECT pg_advisory_unlock(:lock_id)"),
                                    {"lock_id": _BACKGROUND_LOCK_ID},
                                )
                        if _probe_acquired:
                            logger.warning(
                                "[MONITOR] Acquired leader lock from non-leader "
                                "watchdog (prior leader died). Hard-exiting so "
                                "Cloud Run respawns this instance through the "
                                "leader path."
                            )
                            # _exit bypasses the lifespan teardown which is
                            # fine here — non-leader instances hold no
                            # leader-only state and the per-instance services
                            # we own (config_sync) are restartable.
                            import os as _os

                            _os._exit(0)
                    except Exception as _probe_exc:
                        logger.warning(
                            f"[MONITOR] Leader-lock probe failed "
                            f"(stays non-leader this cycle): {_probe_exc}"
                        )
                except asyncio.CancelledError:
                    break
                except Exception as _e:
                    logger.warning(f"[MONITOR] Non-leader watchdog error: {_e}")
                    await asyncio.sleep(10)

        nl_monitor_task = asyncio.create_task(_monitor_non_leader())

        yield

        nl_monitor_task.cancel()
        config_sync_task.cancel()
        jwt_revocation_task_nl.cancel()
        event_fanout_task_nl.cancel()
        agent_log_relay_task_nl.cancel()
        publish_task_nl.cancel()
        notification_retry_task_nl.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    nl_monitor_task,
                    config_sync_task,
                    jwt_revocation_task_nl,
                    event_fanout_task_nl,
                    agent_log_relay_task_nl,
                    publish_task_nl,
                    notification_retry_task_nl,
                    return_exceptions=True,
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.warning("[SHUTDOWN] Non-leader tasks didn't finish within 10s")
        await close_github_client()
        from app.redis_client import close_redis

        await close_redis()
        return

    # This instance owns the background-leader lock — start all background services.

    # Backfill NULL integration_branch / promotion_target from GitHub defaults.
    # Runs once per startup on the leader only; wrapped so an exception here does
    # not prevent service startup (logged + alerted, not raised).
    try:
        async with AsyncSessionLocal() as _bf_session:
            _bf_summary = await _shared_state.backfill_branch_columns(_bf_session)
            _ns_summary = await _shared_state.backfill_task_branch_namespace(
                _bf_session
            )
            await _bf_session.commit()
            logger.info(
                "[STARTUP] Branch backfill complete: %s namespace_backfill: %s",
                _bf_summary,
                _ns_summary,
            )
    except Exception as _bf_exc:
        logger.error(
            "[STARTUP] Branch backfill failed (non-fatal): %s", _bf_exc, exc_info=True
        )

    # Backfill _file_to_system reverse index for all existing design-doc
    # systems/* pages. The index was originally built only from the
    # "modules" field, but most pages use "key_paths" (or "key_files");
    # this sweep applies the corrected field-reading logic to every
    # existing systems page without waiting for each to be re-set.
    try:
        from app.routers.admin_agent_ops import backfill_file_to_system_index

        async with AsyncSessionLocal() as _fts_session:
            _fts_count = await backfill_file_to_system_index(_fts_session)
            await _fts_session.commit()
            logger.info(
                "[STARTUP] _file_to_system backfill complete: %d projects updated",
                _fts_count,
            )
    except Exception as _fts_exc:
        logger.error(
            "[STARTUP] _file_to_system backfill failed (non-fatal): %s",
            _fts_exc,
            exc_info=True,
        )

    # Reclaim lane warm-workspace states whose backing pool VM was deleted while
    # the orchestrator was stopped (crash / deploy / manual removal).  The live
    # path (clear_lanes_for_torn_down_vm) handles in-process teardowns; this
    # sweep closes the gap for out-of-process deletions.
    try:
        from app.improvement_lane_ops import reclaim_orphaned_lane_workspace_states

        async with AsyncSessionLocal() as _reclaim_session:
            _reclaimed = await reclaim_orphaned_lane_workspace_states(_reclaim_session)
            await _reclaim_session.commit()
            logger.info(
                "[STARTUP] Orphaned lane workspace reclaim complete: %d lanes reclaimed",
                _reclaimed,
            )
    except Exception as _reclaim_exc:
        logger.error(
            "[STARTUP] Orphaned lane workspace reclaim failed (non-fatal): %s",
            _reclaim_exc,
            exc_info=True,
        )

    # Start the Reaper Service background task
    reaper_task = asyncio.create_task(reaper_service())

    # Start the GCP Cleanup Service background task
    gcp_cleanup_task = asyncio.create_task(gcp_cleanup_service())

    # Start the GCP Image Pruner (keeps agent image family under the per-project quota)
    gcp_image_cleanup_task = asyncio.create_task(gcp_image_cleanup_service())

    # Start Secret Reconciliation Service (orphaned agent-creds-* cleanup)
    secret_recon_task = asyncio.create_task(secret_reconciliation_service())

    # Start Stale Branch Cleanup Service (removes old blueprint/research/plan branches)
    stale_branch_task = asyncio.create_task(stale_branch_cleanup_service())

    # Start Auto-Archive Service (archives terminal tasks older than N days)
    auto_archive_task = asyncio.create_task(auto_archive_service())

    # Start the Pipeline-Alert Scan (deterministic operator-Alert emitters).
    # Replaces the deleted detect->investigate->fix regression apparatus: every
    # surviving signal (signal-staleness, sr_catch_rate targets, lone-checker,
    # agent-VM resource cluster, pipeline success-rate cliff) is now emitted as a
    # deduped WARNING Alert for an operator — no autonomous remediation is spawned.
    from app.pipeline_alert_scan import pipeline_alert_scan_service

    pipeline_alert_scan_task = asyncio.create_task(pipeline_alert_scan_service())

    # Start the Digest Precompute service (Design-audit #1). The heavy 5000-row
    # JSONB digest scan now runs on this leader-only background timer (~45s) and
    # the result is stored in a lightweight in-process cache; the
    # /api/admin/digest endpoint serves that precomputed payload instead of
    # re-running the scan on a request-serving connection (root cause of the
    # 2026-05-29 pool-starvation hang).
    from app.digest_precompute import digest_precompute_service

    digest_precompute_task = asyncio.create_task(digest_precompute_service())

    # Start the Warm-Pool Maintainer (efficiency direction d-5fd69fe1). Leader-only
    # tick that reconciles the pre-provisioned idle VM pool toward its configured
    # target (provision below / drain above / recycle stale image). Inert while
    # warm_pool_enabled is False.
    from app.warm_pool_maintainer import pool_maintainer_service

    pool_maintainer_task = asyncio.create_task(pool_maintainer_service())

    # Start the compute-job monitor (budgeted external compute substrate).
    compute_job_monitor_task = asyncio.create_task(compute_job_monitor_service())

    # Start the Efficiency Snapshot service (daily per-project axis snapshots).
    async def _efficiency_snapshot_service():
        """Hourly leader-only tick that writes a daily efficiency snapshot per project.

        Cadence is enforced inside ``maybe_write_daily_snapshot`` via the
        ``last_run_at`` marker stored under
        ``project.design_doc["efficiency_baseline"]``, so the in-instance
        sleep can be coarse without producing duplicate snapshots.
        """
        from app.efficiency_snapshot import (
            SNAPSHOT_INTERVAL_SECONDS,
            maybe_alert_pool_payoff_transition,
            maybe_alert_provider_routing_transition,
            maybe_alert_self_repair_burn_share,
            maybe_alert_unpriced_models_coverage,
            maybe_write_daily_snapshot,
        )

        while True:
            await asyncio.sleep(SNAPSHOT_INTERVAL_SECONDS)
            try:
                async with AsyncSessionLocal() as scan_session:
                    project_ids = (
                        (await scan_session.execute(select(Project.id))).scalars().all()
                    )
                    for project_id in project_ids:
                        try:
                            written = await maybe_write_daily_snapshot(
                                scan_session, project_id
                            )
                            if written is not None:
                                await scan_session.commit()
                                logger.info(
                                    "[EFFICIENCY_SNAPSHOT] project=%s wrote daily snapshot",
                                    project_id,
                                )
                        except Exception as proj_exc:
                            await scan_session.rollback()
                            logger.warning(
                                "[EFFICIENCY_SNAPSHOT] project=%s error=%s",
                                project_id,
                                proj_exc,
                            )
                        # Warm-pool payoff transition alert: runs every hourly
                        # tick (independent of the daily snapshot-write cadence)
                        # so a scoped bounded-enable's verdict crossing a decision
                        # boundary pages the operator promptly. Best-effort —
                        # isolated try/except + commit so one project never blocks
                        # the scan.
                        try:
                            fired = await maybe_alert_pool_payoff_transition(
                                scan_session, project_id
                            )
                            await scan_session.commit()
                            if fired is not None:
                                logger.info(
                                    "[EFFICIENCY_SNAPSHOT] project=%s pool-payoff "
                                    "alert fired verdict=%s",
                                    project_id,
                                    fired,
                                )
                        except Exception as alert_exc:
                            await scan_session.rollback()
                            logger.warning(
                                "[EFFICIENCY_SNAPSHOT] project=%s payoff-alert error=%s",
                                project_id,
                                alert_exc,
                            )
                        # Unpriced-models coverage alert: same hourly tick, edge-
                        # triggered, so spend leaking onto unpriced (provider,
                        # model) combos pages the operator instead of silently
                        # zero-costing. Best-effort — isolated try/except + commit.
                        try:
                            unpriced_fired = await maybe_alert_unpriced_models_coverage(
                                scan_session, project_id
                            )
                            await scan_session.commit()
                            if unpriced_fired:
                                logger.info(
                                    "[EFFICIENCY_SNAPSHOT] project=%s unpriced-"
                                    "models alert fired",
                                    project_id,
                                )
                        except Exception as unpriced_exc:
                            await scan_session.rollback()
                            logger.warning(
                                "[EFFICIENCY_SNAPSHOT] project=%s unpriced-alert "
                                "error=%s",
                                project_id,
                                unpriced_exc,
                            )
                        # Self-repair burn-share alert: same hourly tick, edge-
                        # triggered, so elevated repair token overhead pages the
                        # operator instead of silently degrading efficiency. Best-
                        # effort — isolated try/except + commit.
                        try:
                            repair_fired = await maybe_alert_self_repair_burn_share(
                                scan_session, project_id
                            )
                            await scan_session.commit()
                            if repair_fired:
                                logger.info(
                                    "[EFFICIENCY_SNAPSHOT] project=%s "
                                    "self-repair-burn-share alert fired",
                                    project_id,
                                )
                        except Exception as repair_exc:
                            await scan_session.rollback()
                            logger.warning(
                                "[EFFICIENCY_SNAPSHOT] project=%s "
                                "self-repair-burn-share alert error=%s",
                                project_id,
                                repair_exc,
                            )
                        # Provider routing verdict transition alert: same hourly
                        # tick, edge-triggered, so ADOPT/REJECT pages the operator
                        # instead of requiring manual polling. Best-effort.
                        try:
                            routing_fired = (
                                await maybe_alert_provider_routing_transition(
                                    scan_session, project_id
                                )
                            )
                            await scan_session.commit()
                            if routing_fired is not None:
                                logger.info(
                                    "[EFFICIENCY_SNAPSHOT] project=%s "
                                    "routing verdict alert fired verdict=%s",
                                    project_id,
                                    routing_fired,
                                )
                        except Exception as routing_exc:
                            await scan_session.rollback()
                            logger.warning(
                                "[EFFICIENCY_SNAPSHOT] project=%s "
                                "routing-verdict alert error=%s",
                                project_id,
                                routing_exc,
                            )
            except asyncio.CancelledError:
                raise
            except Exception as scan_exc:
                logger.error(
                    "[EFFICIENCY_SNAPSHOT] scan error: %s", scan_exc, exc_info=True
                )

    efficiency_snapshot_task = asyncio.create_task(_efficiency_snapshot_service())

    # Start the Branch Reconciliation service — keeps automain in sync with main.
    async def _branch_reconciliation_service():
        """Periodic leader-only scan: back-merge the LTS branch into the
        integration trunk for all projects so the integration trunk never
        silently falls behind the LTS branch.

        Per project this is a no-op when integration_branch == promotion_target
        (LTS model is inactive) — the gate lives in reconcile_main_into_automain.
        """
        from app.hotfix_backmerge import reconcile_main_into_automain

        _SCAN_INTERVAL = 600  # 10 minutes
        while True:
            await asyncio.sleep(_SCAN_INTERVAL)
            try:
                async with AsyncSessionLocal() as scan_session:
                    project_ids = (
                        (await scan_session.execute(select(Project.id))).scalars().all()
                    )
                    for project_id in project_ids:
                        try:
                            result = await reconcile_main_into_automain(
                                scan_session, project_id
                            )
                            await scan_session.commit()
                            if result.get("status") not in (
                                None,
                                "skipped",
                                "noop_empty_diff",
                            ):
                                logger.info(
                                    "[BRANCH_RECONCILE] project=%s status=%s",
                                    project_id,
                                    result.get("status"),
                                )
                        except Exception as proj_exc:
                            await scan_session.rollback()
                            logger.warning(
                                "[BRANCH_RECONCILE] project=%s error=%s",
                                project_id,
                                proj_exc,
                            )
            except asyncio.CancelledError:
                raise
            except Exception as scan_exc:
                logger.error(
                    "[BRANCH_RECONCILE] scan error: %s", scan_exc, exc_info=True
                )

    branch_reconciliation_task = asyncio.create_task(_branch_reconciliation_service())

    # ── Cluster C3: EVALUATING-timeout recovery as a dedicated background task ──
    # Previously _check_evaluating_timeouts ran INLINE on the orchestrator's
    # monitoring tail every 300s. That coupled the leader's dispatch-cycle
    # liveness to the sweep's serial GitHub I/O (get_pull_request /
    # close_pull_request / _check_and_trigger_rollup per stale EVALUATING parent):
    # a large backlog grew the leader cycle past ORCHESTRATOR_HANG_SECONDS and
    # the watchdog killed it mid-recovery (2026-05-30). Moving it to its own
    # asyncio.Task with a per-tick asyncio.timeout means the dispatch cycle
    # neither awaits it nor is timed by it; a wedged tick is bounded and the
    # supervisor below restarts it independently. Cadence preserved at 300s.
    # Own liveness marker for the extracted sweep (fix D) — separate from the
    # orchestrator_worker's heartbeat so a foreign task can't mask a hung worker.
    _eval_svc_last_progress = _utcnow()

    async def _evaluating_timeout_service():
        """Leader-only periodic EVALUATING-timeout recovery sweep (C3).

        Bounded with a per-tick asyncio.timeout so a hung GitHub call cannot pin
        this task indefinitely; the monitor restarts it on a crash/timeout.

        Liveness (fix D): the sweep pumps THIS task's own heartbeat marker, not
        the orchestrator_worker's shared ``_orchestrator_last_progress`` — a
        foreign task pumping the worker's marker would mask a genuinely hung
        worker from its /health gate + watchdog. This task's own bound is the
        per-tick asyncio.timeout below + _monitor_background_tasks restart on
        crash/timeout; ``_eval_svc_last_progress`` is recorded for future
        per-task supervision/metrics without touching the worker's marker.
        """
        from app.services import _check_evaluating_timeouts

        _SCAN_INTERVAL = 300  # mirrors the prior inline cadence
        _TICK_BUDGET = 270  # < interval, so ticks never overlap-stack

        def _eval_svc_heartbeat() -> None:
            # Own liveness marker — deliberately NOT _mark_orchestrator_progress.
            nonlocal _eval_svc_last_progress
            _eval_svc_last_progress = _utcnow()

        while True:
            await asyncio.sleep(_SCAN_INTERVAL)
            try:
                async with asyncio.timeout(_TICK_BUDGET):
                    await _check_evaluating_timeouts(progress_cb=_eval_svc_heartbeat)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                # Expected, recoverable control flow (not a crash): the next
                # tick retries and the underlying sweep is row-capped + per-call
                # bounded, so a single slow tick is bounded, not fatal.
                logger.warning(
                    "[EVAL_TIMEOUT_SVC] tick exceeded %ds budget — abandoning this "
                    "tick; next tick retries (rows are bounded + heartbeated)",
                    _TICK_BUDGET,
                )
            except Exception as svc_exc:
                logger.error(
                    "[EVAL_TIMEOUT_SVC] tick error: %s", svc_exc, exc_info=True
                )

    evaluating_timeout_task = asyncio.create_task(_evaluating_timeout_service())

    # Start the Orchestrator Worker background task
    orchestrator_task = asyncio.create_task(orchestrator_worker())

    # Per-instance runtime_config sync — propagates admin mutations across
    # Cloud Run instances (each has its own in-memory dict; DB is shared).
    config_sync_task = asyncio.create_task(runtime_config_sync_service())

    # Per-instance JWT revocation listener — each instance keeps its own
    # _terminated_task_ids cache; LISTEN/NOTIFY broadcasts terminations from
    # sibling instances so all caches converge within seconds.
    from app.jwt_revocation import jwt_revocation_service

    jwt_revocation_task = asyncio.create_task(jwt_revocation_service())

    # Per-instance cross-instance WebSocket control-event listener — each
    # instance LISTENs on the project_event channel so a control event
    # broadcast on a sibling Cloud Run instance is re-delivered to THIS
    # instance's locally-connected WebSocket clients (board updates, alerts,
    # deploy-state changes, desktop notifications).
    from app.event_fanout import (
        agent_log_relay_service,
        event_fanout_service,
        publish_service,
    )

    event_fanout_task = asyncio.create_task(event_fanout_service())

    # Per-instance live agent_log relay — buffers per-line log frames received
    # from agents whose stream landed on THIS instance and flushes them as
    # batched NOTIFYs so sibling instances re-deliver them to their own viewer
    # clients (the high-volume agent_log path the discrete control-event fanout
    # deliberately excludes).
    agent_log_relay_task = asyncio.create_task(agent_log_relay_service())

    # Per-instance dedicated pg_notify publish connection — owns ONE persistent
    # raw asyncpg connection used by every control-event / agent_log-batch
    # pg_notify, so the hot messaging path never checks out a pooled SQLAlchemy
    # session and never competes with HTTP request handlers for the pool.
    publish_task = asyncio.create_task(publish_service())

    # Per-instance webhook retry drainer — flushes the external-notification
    # retry queue on its own ~30s schedule so due retries are no longer
    # stranded during quiet periods (the queue is per-instance in memory).
    from app.notification_dispatch import notification_retry_drain_service

    notification_retry_task = asyncio.create_task(notification_retry_drain_service())

    # Refresh per-account model-availability caches every 30 min so a
    # provider-side preview-access change doesn't sit in dispatch rotation
    # for hours (audit E13).
    availability_refresh_task = asyncio.create_task(
        account_availability_refresh_service()
    )

    # Store references for admin restart endpoint
    from app.shared_state import background_tasks as _bg_tasks

    _bg_tasks["orchestrator"] = orchestrator_task
    _bg_tasks["reaper"] = reaper_task
    _bg_tasks["gcp_cleanup"] = gcp_cleanup_task
    _bg_tasks["gcp_image_cleanup"] = gcp_image_cleanup_task
    _bg_tasks["secret_reconciliation"] = secret_recon_task
    _bg_tasks["stale_branch_cleanup"] = stale_branch_task
    _bg_tasks["auto_archive"] = auto_archive_task
    _bg_tasks["pipeline_alert_scan"] = pipeline_alert_scan_task
    _bg_tasks["digest_precompute"] = digest_precompute_task
    _bg_tasks["pool_maintainer"] = pool_maintainer_task
    _bg_tasks["compute_job_monitor"] = compute_job_monitor_task
    _bg_tasks["efficiency_snapshot"] = efficiency_snapshot_task
    _bg_tasks["branch_reconciliation"] = branch_reconciliation_task
    _bg_tasks["evaluating_timeout"] = evaluating_timeout_task
    _bg_tasks["config_sync"] = config_sync_task
    _bg_tasks["jwt_revocation"] = jwt_revocation_task
    _bg_tasks["event_fanout"] = event_fanout_task
    _bg_tasks["agent_log_relay"] = agent_log_relay_task
    _bg_tasks["publish"] = publish_task
    _bg_tasks["notification_retry_drain"] = notification_retry_task
    _bg_tasks["availability_refresh"] = availability_refresh_task

    # Monitor background tasks for silent crashes (with exponential backoff)
    async def _monitor_background_tasks():
        tasks = _bg_tasks
        restart_state: Dict[str, Dict] = {}  # name -> {attempts, last_restart}
        # name -> [crash datetime, ...] within the episode window, used to detect
        # a crash *episode* (flapping service) and escalate to a deduped ERROR Alert.
        crash_timestamps: Dict[str, list] = {}
        # When the watchdog cancels a hung task, it stashes the reason here so the
        # death-detection branch below can include it in the alert/log instead of
        # the useless "Exception: None" that a bare cancellation produces.
        cancel_reasons: Dict[str, str] = {}
        task_factories = {
            "orchestrator": orchestrator_worker,
            "reaper": reaper_service,
            "gcp_cleanup": gcp_cleanup_service,
            "gcp_image_cleanup": gcp_image_cleanup_service,
            "secret_reconciliation": secret_reconciliation_service,
            "stale_branch_cleanup": stale_branch_cleanup_service,
            "auto_archive": auto_archive_service,
            "pipeline_alert_scan": pipeline_alert_scan_service,
            "digest_precompute": digest_precompute_service,
            "pool_maintainer": pool_maintainer_service,
            "compute_job_monitor": compute_job_monitor_service,
            "efficiency_snapshot": _efficiency_snapshot_service,
            "branch_reconciliation": _branch_reconciliation_service,
            "evaluating_timeout": _evaluating_timeout_service,
            "config_sync": runtime_config_sync_service,
            "jwt_revocation": jwt_revocation_service,
            "event_fanout": event_fanout_service,
            "agent_log_relay": agent_log_relay_service,
            "publish": publish_service,
            "notification_retry_drain": notification_retry_drain_service,
            "availability_refresh": account_availability_refresh_service,
        }
        while True:
            try:
                await asyncio.sleep(30)
                # Liveness watchdog for orchestrator: task may be alive (not done())
                # yet stuck inside a cycle — `t.done()` alone misses hangs. Judge
                # liveness with the SAME shared predicate and threshold as the
                # `/health` endpoint (`evaluate_orchestrator_liveness`), gated on
                # the in-cycle progress *heartbeat* — not full-cycle completion.
                # Gating on completion + a hardcoded short threshold previously
                # killed healthy-but-slow cycles in a restart loop (incident
                # 2026-05-28; /health was fixed in PR #6678 but this watchdog
                # silently drifted). The single source of truth prevents re-drift.
                try:
                    from app import orchestrator as _orch

                    orch_task = tasks.get("orchestrator")
                    if orch_task is not None and not orch_task.done():
                        liveness = _orch.evaluate_orchestrator_liveness(_utcnow())
                        if liveness.stalled:
                            logger.error(
                                f"[MONITOR] Orchestrator appears hung — {liveness.reason}. "
                                f"Cancelling task so restart path re-spawns it."
                            )
                            cancel_reasons["orchestrator"] = (
                                f"watchdog cancelled: {liveness.reason}"
                            )
                            orch_task.cancel()
                except Exception as wd_exc:
                    logger.warning(f"[MONITOR] Watchdog check failed: {wd_exc}")
                # Liveness watchdog for reaper: analogous to the orchestrator probe.
                # When halted the reaper is intentionally sleeping — cancelling and
                # restarting it only produces another halted reaper (module-level
                # halt state persists across asyncio.Task restarts within the same
                # process). Skip the cancel so the monitor loop doesn't compound
                # alert noise while the operator investigates. When NOT halted but
                # stale, cancel so the restart path re-spawns a fresh cycle.
                try:
                    # Re-import each iteration so we read the LIVE module globals
                    # (a top-level ``from ... import X`` would freeze the value at
                    # import time). Judge staleness on the FRESHER of the in-cycle
                    # progress heartbeat and the last completed cycle — mirroring
                    # the orchestrator's ``_orchestrator_last_progress or
                    # _orchestrator_last_cycle`` logic (PR #6678). A long-but-
                    # progressing reap (large orphan backlog) is therefore healthy;
                    # only a reaper with no progress for REAPER_HANG_SECONDS is hung.
                    from app.reaper import (
                        is_reaper_halted as _is_reaper_halted,
                        _reaper_last_cycle_completed_at as _reaper_last_cycle,
                        _reaper_last_progress as _reaper_progress,
                        REAPER_HANG_SECONDS as _reaper_stall_threshold,
                    )

                    reaper_task = tasks.get("reaper")
                    if reaper_task is not None and not reaper_task.done():
                        should_cancel, reaper_reason = _evaluate_reaper_stall(
                            is_halted=_is_reaper_halted(),
                            last_progress=_reaper_progress or _reaper_last_cycle,
                            now=_utcnow(),
                            stall_threshold=_reaper_stall_threshold,
                        )
                        if should_cancel:
                            logger.error(
                                "[MONITOR] Reaper appears hung — %s. "
                                "Cancelling task so restart path re-spawns it.",
                                reaper_reason,
                            )
                            cancel_reasons["reaper"] = (
                                f"watchdog cancelled: {reaper_reason}"
                            )
                            reaper_task.cancel()
                        # else: halted intentionally (operator reset required), or
                        # never recorded progress yet (give the fresh reaper time),
                        # or progressing within threshold — leave it running.
                except Exception as reaper_wd_exc:
                    logger.warning(
                        "[MONITOR] Reaper watchdog check failed: %s", reaper_wd_exc
                    )
                for name, t in list(tasks.items()):
                    if t.done():
                        exc = t.exception() if not t.cancelled() else None
                        # A bare cancellation (t.cancelled() with no exception) is
                        # almost always our own watchdog firing — surface the reason
                        # we stashed at cancel time instead of the misleading "None".
                        cancel_reason = cancel_reasons.pop(name, None)
                        if exc is not None:
                            cause_str = str(exc)[:500]
                        elif cancel_reason:
                            cause_str = cancel_reason
                        else:
                            cause_str = "task cancelled (no exception)"
                        logger.error(
                            f"[MONITOR] Background task '{name}' DIED! Cause: {cause_str}"
                        )
                        asyncio.create_task(  # noqa: RUF006 — fire-and-forget crash webhook; the monitor loop outlives it
                            _send_webhook_notification(
                                "SERVICE_CRASHED",
                                f"Background service crashed: {name}",
                                cause_str,
                            )
                        )
                        try:
                            await _emit_crash_alert(
                                AsyncSessionLocal, name, cause_str
                            )
                        except Exception as alert_exc:
                            logger.warning(
                                f"[MONITOR] Failed to create crash alert: {alert_exc}"
                            )

                        # Crash-episode escalation: track per-service crash
                        # timestamps over a rolling window and, when the count
                        # reaches the episode threshold, emit ONE deduped ERROR
                        # Alert per episode (operator visibility only — no
                        # autonomous investigate task is spawned; that apparatus
                        # was removed). The window is pruned each crash so a slow
                        # trickle of unrelated restarts never accumulates into a
                        # false episode.
                        try:
                            crash_count = _record_crash_and_count(
                                crash_timestamps, name, _utcnow()
                            )
                            if crash_count >= _BACKGROUND_CRASH_EPISODE_THRESHOLD:
                                await _emit_crash_episode_alert(
                                    AsyncSessionLocal, name, crash_count
                                )
                                # Reset so the next escalation requires a fresh
                                # episode rather than re-firing every crash.
                                crash_timestamps[name] = []
                        except Exception as _ep_exc:
                            logger.warning(
                                f"[MONITOR] Failed crash-episode escalation for {name}: {_ep_exc}"
                            )

                        factory = task_factories.get(name)
                        # Always restart cancelled tasks too — the watchdog
                        # above intentionally cancels hung tasks expecting
                        # this path to respawn them. Previously the
                        # `not t.cancelled()` guard left cancelled tasks
                        # dead forever, spamming DIED alerts every 30s with
                        # no recovery. Shutdown cancellation is handled
                        # separately (the monitor loop itself is cancelled
                        # during shutdown, exiting before reaching here).
                        if factory:
                            state = restart_state.setdefault(
                                name, {"attempts": 0, "last_restart": None}
                            )
                            now = _utcnow()
                            # Reset attempts if last restart was >5 min ago (ran long enough to be stable)
                            if (
                                state["last_restart"]
                                and (now - state["last_restart"]).total_seconds() > 300
                            ):
                                state["attempts"] = 0
                                # Stable run clears the crash-episode window too —
                                # a service that ran clean for 5+ min is no longer
                                # in an episode, so stale crash timestamps must not
                                # carry over into a future false escalation.
                                crash_timestamps.pop(name, None)
                            backoff = min(
                                2 ** state["attempts"], 300
                            )  # 1s, 2s, 4s, ... cap 5min
                            if backoff > 1:
                                logger.warning(
                                    f"[MONITOR] Backing off {backoff}s before restarting {name} (attempt #{state['attempts'] + 1})"
                                )
                                await asyncio.sleep(backoff)
                            state["attempts"] += 1
                            state["last_restart"] = _utcnow()
                            logger.info(f"[MONITOR] Restarting {name}...")
                            tasks[name] = asyncio.create_task(factory())
                            try:
                                async with AsyncSessionLocal() as alert_session:
                                    project_ids = (
                                        (
                                            await alert_session.execute(
                                                select(Project.id)
                                            )
                                        )
                                        .scalars()
                                        .all()
                                    )
                                    for project_id in project_ids:
                                        alert = Alert(
                                            project_id=project_id,
                                            severity=AlertSeverity.INFO,
                                            title=f"Background service restarted: {name}",
                                            message=f"Service '{name}' was automatically restarted (attempt #{state['attempts']}).",
                                        )
                                        alert_session.add(alert)
                                    await alert_session.commit()
                            except Exception as alert_exc:
                                logger.warning(
                                    f"[MONITOR] Failed to create restart alert: {alert_exc}"
                                )
            except Exception as mon_exc:
                logger.error(
                    f"[MONITOR] Monitor itself hit exception: {mon_exc}", exc_info=True
                )
                await asyncio.sleep(10)  # Brief pause before retrying

    # Leader-lock keepalive: re-assert ownership of the session-scoped advisory
    # lock so a dropped connection can't silently hand leadership to a peer (the
    # "prior leader died" respawn loop). See _leader_lock_keepalive_tick.
    _LEADER_KEEPALIVE_SECONDS = float(os.getenv("LEADER_KEEPALIVE_SECONDS", "30"))

    async def _leader_lock_keepalive():
        while True:
            await asyncio.sleep(_LEADER_KEEPALIVE_SECONDS)
            outcome = await _leader_lock_keepalive_tick(leader_conn)
            if outcome in ("lost", "error"):
                logger.warning(
                    "[MONITOR] Leader advisory lock %s; hard-exiting so Cloud Run "
                    "respawns through clean leader election",
                    "lost to a peer" if outcome == "lost" else "unrecoverable",
                )
                os._exit(0)

    leader_keepalive_task = (
        asyncio.create_task(_leader_lock_keepalive())
        if leader_conn is not None
        else None
    )

    monitor_task = asyncio.create_task(_monitor_background_tasks())

    # Out-of-loop hard watchdog (leader only): a separate OS thread that hard-
    # exits the process if the orchestrator progress heartbeat goes stale past a
    # bound LARGER than ORCHESTRATOR_HANG_SECONDS. The in-loop watchdog above and
    # /health both run on the event loop, so a FULLY blocked loop (sync CPU stall
    # or total pool exhaustion — the class _bounded_leader_step documents it
    # cannot interrupt) starves both, leaving only Cloud Run's slow liveness probe
    # to recycle the container (~6-min wedge windows, prod 2026-06-15). A thread
    # still runs while the loop is blocked, so it bounds that window and dumps all
    # thread stacks on the way out. Gated on live leadership so a follower (which
    # never pumps the heartbeat) is never killed.
    from app.orchestrator import start_orchestrator_hard_watchdog

    start_orchestrator_hard_watchdog(is_guarded=lambda: _shared_state.is_leader)

    yield
    # Shutdown: graceful cleanup with timeout to let in-flight operations finish
    tasks_to_cancel = [
        reaper_task,
        gcp_cleanup_task,
        gcp_image_cleanup_task,
        secret_recon_task,
        stale_branch_task,
        auto_archive_task,
        pipeline_alert_scan_task,
        digest_precompute_task,
        pool_maintainer_task,
        compute_job_monitor_task,
        branch_reconciliation_task,
        orchestrator_task,
        config_sync_task,
        jwt_revocation_task,
        event_fanout_task,
        agent_log_relay_task,
        publish_task,
        notification_retry_task,
        monitor_task,
    ]
    if leader_keepalive_task is not None:
        tasks_to_cancel.append(leader_keepalive_task)
    for t in tasks_to_cancel:
        t.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks_to_cancel, return_exceptions=True), timeout=10.0
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[SHUTDOWN] Background tasks didn't finish within 10s grace period"
        )
    await close_github_client()
    from app.redis_client import close_redis

    await close_redis()
    # Release the connection-scoped advisory lock by closing the dedicated connection.
    if leader_conn is not None:
        try:
            await leader_conn.close()
        except Exception as _close_exc:
            logger.warning(
                "[LIFESPAN] Failed to close leader connection on shutdown: %s",
                _close_exc,
            )
    # Gracefully dispose the main query pool so a Cloud Run drain (SIGTERM)
    # closes pooled asyncpg connections cleanly instead of abandoning them for
    # Cloud SQL to reap — orphaned server-side connections otherwise linger
    # against max_connections during instance scale-in churn. Bounded so a hung
    # close can never stall the shutdown past the grace window.
    from app.database import dispose_engine

    try:
        await asyncio.wait_for(dispose_engine(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning(
            "[SHUTDOWN] engine.dispose() exceeded 5s grace — abandoning pool"
        )
    except Exception as _disp_exc:
        logger.warning("[SHUTDOWN] engine.dispose() failed: %s", _disp_exc)
