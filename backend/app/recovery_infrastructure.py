"""Infrastructure-readiness recovery sweeps (bootstrap + lane-baseline backfill).

Extracted from ``app.recovery_post_commit`` to keep the post-commit hub below
its size cap. Holds the lane-baseline-backfill sweep (``_sweep_lane_baseline_backfill``)
and the bootstrap-recovery sweep (``_sweep_bootstrap_recovery``), plus their
advisory-lock helpers (``_try_bootstrap_spawn_advisory_lock``,
``_bootstrap_spawn_lock_key``).

Every public name is re-exported from ``app.recovery_sweeps`` (and in turn from
``app.services``) so existing call sites and test patch targets keep resolving
unchanged. Shared bounding infrastructure lives in ``app.services`` and is
referenced through the ``services`` module object so test patches against
``app.services.<name>`` continue to take effect.
"""

import hashlib as _hashlib
import logging
from sqlalchemy import and_, func, or_
from sqlmodel import select

from app import services
from app.deploy_identity import project_is_external_repo_clause
from app.models import Project
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


# ── Lane-baseline backfill sweep ─────────────────────────────────────────────

_LANE_BASELINE_BACKFILL_BATCH_SIZE = 50


async def _sweep_lane_baseline_backfill() -> None:
    """Backfill lane baselines for recently-closed versions lacking a stored entry.

    Runs every ~600s. Iterates active (non-archived) projects and calls
    ``backfill_missing_lane_baselines`` for each.  Each project is independent:
    a failure on one project is logged and skipped without blocking others.
    Best-effort/non-fatal — mirrors the pattern of other leader-cycle sweeps.
    """
    from app.lane_baseline_snapshot import backfill_missing_lane_baselines

    try:
        async with services.AsyncSessionLocal() as session:
            project_ids = list(
                (
                    await session.execute(
                        select(Project.id)
                        .where(Project.archived.is_(False))
                        .order_by(Project.id)
                        .limit(_LANE_BASELINE_BACKFILL_BATCH_SIZE)
                    )
                )
                .scalars()
                .all()
            )

        for project_id in project_ids:
            try:
                backfilled = await backfill_missing_lane_baselines(
                    project_id=project_id
                )
                if backfilled:
                    logger.info(
                        "[LANE_BASELINE] backfill: wrote baselines for v%s on project=%s",
                        backfilled,
                        project_id,
                    )
            except Exception:
                logger.warning(
                    "[LANE_BASELINE] backfill sweep: project=%s failed (non-fatal)",
                    project_id,
                    exc_info=True,
                )
    except Exception:
        logger.exception("[LANE_BASELINE] lane-baseline backfill sweep failed")


# ── Bootstrap recovery sweep ───────────────────────────────────────────────────

_BOOTSTRAP_SWEEP_BATCH_SIZE = 20


def _bootstrap_spawn_lock_key(project_id: object) -> int:
    """Stable 31-bit advisory-lock key for a project's bootstrap spawn.

    Uses SHA-256 (not Python hash()) because hash() is PYTHONHASHSEED-salted
    and two orchestrator processes compute DIFFERENT keys for the same project —
    the advisory lock would never exclude anything across instances.
    """
    digest = _hashlib.sha256(f"bootstrap_spawn:{project_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % (2**31)


async def _try_bootstrap_spawn_advisory_lock(session, project_id: object) -> bool:
    """Try to take the per-project bootstrap-spawn advisory lock (Postgres).

    Transaction-scoped (``pg_try_advisory_xact_lock``) so it is released when
    the transaction commits or rolls back. Best-effort: reduces duplicate
    work between concurrently-ticking orchestrator instances. Non-Postgres
    dialects (tests) report no contention and always return True.
    """
    from sqlalchemy import text as _sql_text

    try:
        got = (
            await session.execute(
                _sql_text("SELECT pg_try_advisory_xact_lock(:k)"),
                {"k": _bootstrap_spawn_lock_key(project_id)},
            )
        ).scalar()
    except Exception:
        return True
    return bool(got)


async def _sweep_bootstrap_recovery() -> None:
    """Enqueue one repo_bootstrap task for each eligible unbootstrapped project.

    Eligible projects are: external — NOT the deploy's own repo, per
    ``project_is_external_repo_clause`` (``is_self_repo`` plus a deploy-identity
    match that hardens the filter against a misset flag) — non-empty
    (repo_is_empty falsy), no bootstrap_completed_at, and no currently-active
    (QUEUED/PROVISIONING/IN_PROGRESS) repo_bootstrap task.

    The sweep is bounded and idempotent: the shared spawn_bootstrap_task helper
    refuses to create a duplicate when one is already active, so repeated ticks
    never pile up duplicate tasks. A per-project transaction-scoped advisory
    lock (``pg_try_advisory_xact_lock``) further guards against the non-atomic
    check-then-act race that lets two concurrent orchestrator instances both
    find no active task and both spawn one in the same second (mirrors the
    strategic double-spawn fix, PR #8927).
    """
    from app.repo_bootstrap import spawn_bootstrap_task

    try:
        async with services.AsyncSessionLocal() as session:
            # Filter ineligible projects IN the query so the LIMIT caps spawnable
            # candidates, not the raw project set. Completed projects accumulate
            # unboundedly over time; applying the limit before filtering them out
            # would starve genuinely-eligible (degraded/failed/never-bootstrapped)
            # projects as the completed set grows past the batch size.
            #
            # bootstrap_completed_at: absent key or null → IS NULL → eligible.
            # repo_is_empty: absent key or 'false' → coalesce to 'false' → != 'true' → eligible.
            # ORDER BY id gives consistent ordering so new projects aren't missed.
            now_iso = _utcnow().isoformat()
            result = await session.execute(
                select(Project)
                .where(
                    project_is_external_repo_clause(),
                    Project.archived.is_(False),
                    Project.build_config["bootstrap_completed_at"]
                    .as_string()
                    .is_(None),
                    func.coalesce(
                        Project.build_config["repo_is_empty"].as_string(), "false"
                    )
                    != "true",
                    # A legacy project with an UNDETERMINED repo_is_empty (JSON
                    # key absent → as_string() IS NULL) may already be stranded
                    # at recovery-exhausted because it was retried blindly before
                    # the self-heal existed. Let it back through the exhaustion
                    # guards so spawn_bootstrap_task can run its emptiness
                    # self-heal (seed + stamp) on it; a determined signal still
                    # obeys the exhaustion guards unchanged.
                    or_(
                        Project.build_config["repo_is_empty"].as_string().is_(None),
                        and_(
                            func.coalesce(
                                Project.build_config[
                                    "bootstrap_recovery_state"
                                ].as_string(),
                                "",
                            )
                            != "exhausted",
                            Project.build_config["bootstrap_recovery_exhausted_at"]
                            .as_string()
                            .is_(None),
                        ),
                    ),
                    or_(
                        Project.build_config["bootstrap_recovery_next_at"]
                        .as_string()
                        .is_(None),
                        Project.build_config["bootstrap_recovery_next_at"].as_string()
                        <= now_iso,
                    ),
                )
                .order_by(Project.id)
                .limit(_BOOTSTRAP_SWEEP_BATCH_SIZE)
            )
            candidates = result.scalars().all()

            spawned = 0
            for project in candidates:
                # Per-project advisory lock guards the non-atomic check-then-act:
                # two concurrent orchestrator instances may both read "no active
                # task" before either's spawn commits, then both insert a duplicate.
                # The transaction-scoped lock serialises spawns per project so only
                # one instance wins; the other sees "already_active" on its next
                # spawn_bootstrap_task call and skips. Non-Postgres (tests) returns
                # True (no contention) so test behaviour is unchanged.
                if not await _try_bootstrap_spawn_advisory_lock(session, project.id):
                    logger.debug(
                        "[BOOTSTRAP-RECOVERY] Another instance holds bootstrap lock "
                        "for project %s — skipping this tick",
                        project.id,
                    )
                    continue
                spawn_result = await spawn_bootstrap_task(
                    project,
                    session,
                    is_first_bootstrap=False,
                    spawn_reason="recovery_sweep",
                    automatic_recovery=True,
                )
                if spawn_result.outcome == "spawned":
                    spawned += 1

            await session.commit()
            if spawned:
                logger.info(
                    "[BOOTSTRAP-RECOVERY] Enqueued %d recovery bootstrap task(s)",
                    spawned,
                )
    except Exception:
        logger.exception("[BOOTSTRAP-RECOVERY] Bootstrap recovery sweep failed")
