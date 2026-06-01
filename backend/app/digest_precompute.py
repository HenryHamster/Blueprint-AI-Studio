"""Background precompute for the heavy ``/api/admin/digest`` payload.

Rationale (Design-audit #1 CRITICAL, root cause of the 2026-05-29
orchestrator pool-starvation hang): ``_compute_digest`` loads up to 5000
JSONB-heavy ``Task`` rows and runs the entire analytics pile *inline* on
every admin poll. Even with the TTL/dedup ``DigestCache`` in front of it,
the first poll after every TTL expiry re-runs the full 5000-row scan on a
*request-serving* connection. As the task table grew, that scan started
exceeding the 120s statement_timeout, holding a pooled connection per
attempt and starving the shared pool that the orchestrator main cycle
depends on (the "no progress heartbeat" hang).

Fix: move the heavy computation to a leader-only background timer
(``digest_precompute_service``, ~30-60s cadence). It computes the digest
for the hot key set (the default ``hours=24`` global view plus each active
project's ``hours=24`` view) once per tick on a *background* session and
stores the result in a lightweight in-process module-level cache
(``_PRECOMPUTE_CACHE``) keyed by ``(hours, project_id)``, each entry stamped
with a wall-clock freshness timestamp.

The ``/api/admin/digest`` endpoint then becomes a cheap dict read of the
precomputed result (annotated with ``precomputed_at`` / ``precompute_age_seconds``),
no longer scanning 5000 rows per request. A bounded fallback remains: if the
precompute cache has no fresh WORKER-written entry for the key (e.g. on a fresh
boot, on a non-leader instance, or for a key the precompute worker doesn't
prime), the endpoint computes once via the existing ``DigestCache`` (which
still provides a 15s TTL + in-flight dedup + serve-stale).

Multi-instance freshness invariant: the deploy runs 2-6 instances but the
precompute worker runs ONLY on the leader. Only entries written by that worker
(``SOURCE_WORKER``) are served as fresh-for-``PRECOMPUTE_STALE_FALLBACK_SECONDS``,
because only the worker continuously refreshes them (``get_fresh(worker_only=
True)`` in the read path). A non-leader instance therefore has no servable
precompute entry and falls through to the ``DigestCache`` 15s-TTL path — it
does NOT serve a never-refreshed payload as fresh for the full 300s fallback
window. The endpoint deliberately does NOT opportunistically prime the cache
from an on-demand compute: such a one-shot entry would otherwise be served as
precomputed-for-300s on the next non-leader poll, regressing non-leader digest
freshness from 15s to 5 minutes.

No new DB table / migration — the cache is per-instance in-process module
state, refreshed continuously by the leader's background tick and read by
that same instance's HTTP handlers. Non-leader instances fall through to the
bounded on-demand compute (rare; admin polls are low-volume relative to the
agent fleet).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Hashable, List, Tuple

logger = logging.getLogger(__name__)

# Default digest window the admin UI polls most heavily (DashboardHome /
# CatchUpPanel both default to a 24h catch-up view). The global (no project)
# view plus each active project's view at this window are the hot keys the
# precompute worker primes every tick.
_DEFAULT_PRECOMPUTE_HOURS: int = 24

# Background tick cadence. ~30-60s is frequent enough that the served digest
# is never more than a tick stale, and the heavy scan runs at most once per
# tick per hot key on a background connection instead of once per TTL expiry
# on a request-serving connection. Overridable via runtime_config.
PRECOMPUTE_INTERVAL_SECONDS: int = 45

# Per-key compute budget for one precompute tick. The compute path already
# sets a 30s LOCAL statement_timeout; this wraps the whole coroutine so a
# wedged tick (e.g. waiting on a stuck connection checkout) cannot pin the
# worker. Bounded < interval so ticks never overlap-stack.
PRECOMPUTE_TICK_BUDGET_SECONDS: int = 35

# How long a precomputed entry is considered fresh enough to serve directly.
# Beyond this the endpoint treats the cache as stale and falls back to a
# bounded on-demand compute (so a dead/lagging precompute worker degrades to
# the old behavior rather than serving arbitrarily old data). Generous
# multiple of the tick interval to tolerate a slow tick / brief worker
# restart without thrashing into the fallback.
PRECOMPUTE_STALE_FALLBACK_SECONDS: float = 300.0

# Cap on the number of active projects primed per tick, so a pathological
# project count can't unbound the tick duration.
_MAX_PROJECTS_PER_TICK: int = 25


# Provenance of a cache entry. Only entries written by the leader's background
# WORKER tick may be served as fresh-for-PRECOMPUTE_STALE_FALLBACK_SECONDS,
# because only the worker continuously refreshes them. Any other writer (an
# opportunistic on-demand prime on a non-leader instance, say) produces a
# one-shot entry that is NEVER refreshed, so trusting it for the long window
# would regress freshness from the 15s DigestCache TTL to 5 minutes. See the
# read path in admin_digest.get_digest.
SOURCE_WORKER = "worker"


@dataclass
class _PrecomputeEntry:
    """A single precomputed digest payload plus freshness bookkeeping."""

    result: Dict[str, Any]
    computed_monotonic: float
    computed_wall_iso: str
    # Who wrote this entry. Only ``SOURCE_WORKER`` entries are continuously
    # refreshed by the leader tick and thus trustworthy for the long window;
    # see ``get_fresh(worker_only=...)``.
    source: str


class DigestPrecomputeCache:
    """In-process module-level store of precomputed digest payloads.

    Keyed by ``(hours, project_id_str_or_None)``. Written by the leader's
    background precompute tick and read by the same instance's HTTP handlers.
    Bounded by the small distinct hot-key set; no eviction needed beyond
    overwrite-on-refresh.
    """

    def __init__(self) -> None:
        self._entries: Dict[Hashable, _PrecomputeEntry] = {}

    @staticmethod
    def make_key(hours: int, project_id: uuid.UUID | None) -> Tuple[int, str | None]:
        return (hours, str(project_id) if project_id else None)

    def put(
        self,
        hours: int,
        project_id: uuid.UUID | None,
        result: Dict[str, Any],
        *,
        computed_wall_iso: str,
        source: str = SOURCE_WORKER,
    ) -> None:
        self._entries[self.make_key(hours, project_id)] = _PrecomputeEntry(
            result=result,
            computed_monotonic=time.monotonic(),
            computed_wall_iso=computed_wall_iso,
            source=source,
        )

    def get_fresh(
        self,
        hours: int,
        project_id: uuid.UUID | None,
        *,
        max_age_seconds: float,
        worker_only: bool = False,
    ) -> _PrecomputeEntry | None:
        """Return the entry only if present and within ``max_age_seconds``.

        When ``worker_only`` is set, an entry that was NOT written by the
        leader's background worker (``source != SOURCE_WORKER``) is treated as
        a miss. The read path uses this so a non-worker (e.g. opportunistically
        primed, one-shot, never-refreshed) entry is never served as
        fresh-for-``max_age_seconds`` — that would regress freshness on a
        non-leader instance from the 15s DigestCache TTL to the full fallback
        window.
        """
        entry = self._entries.get(self.make_key(hours, project_id))
        if entry is None:
            return None
        if worker_only and entry.source != SOURCE_WORKER:
            return None
        if (time.monotonic() - entry.computed_monotonic) > max_age_seconds:
            return None
        return entry

    def clear(self) -> None:
        self._entries.clear()


# Module-level singleton — the live precompute store for this instance.
_PRECOMPUTE_CACHE = DigestPrecomputeCache()


def get_precompute_cache() -> DigestPrecomputeCache:
    """Accessor for the module-level precompute cache (tests can clear it)."""
    return _PRECOMPUTE_CACHE


def annotate_precompute_metadata(
    result: Dict[str, Any],
    *,
    precomputed: bool,
    precomputed_at: str | None,
    precompute_age_seconds: float | None,
) -> Dict[str, Any]:
    """Stamp precompute provenance onto a (shallow-copied) digest payload.

    Returns a new dict so the cached object is never mutated by a reader —
    multiple concurrent requests share the same cached ``result`` reference.
    The added keys are purely informational; the rest of the payload is byte
    -identical to what ``_compute_digest`` produced.
    """
    annotated = dict(result)
    annotated["precomputed"] = precomputed
    annotated["precomputed_at"] = precomputed_at
    annotated["precompute_age_seconds"] = (
        round(precompute_age_seconds, 1) if precompute_age_seconds is not None else None
    )
    return annotated


async def _active_project_ids(session) -> List[uuid.UUID]:
    """Return ids of ACTIVE projects worth priming a digest for.

    Filters out terminal projects (COMPLETED / FAILED / CANCELLED) so the
    bounded ``_MAX_PROJECTS_PER_TICK`` slots are spent priming projects an
    operator might actually be polling, not idle/archived ones. An unfiltered
    ``select(Project.id).limit(25)`` would prime whichever 25 rows the planner
    returned — often long-dead projects — leaving live ones uncached and
    falling through to the on-demand path.

    Scalar-only ``select(Project.id)`` — no JSONB columns loaded — so the
    project enumeration itself is cheap and never contributes to the heavy
    scan the precompute exists to bound.
    """
    from sqlmodel import select

    from app.models import Project, ProjectStatus

    rows = (
        await session.execute(
            select(Project.id)
            .where(
                Project.status.not_in(
                    [
                        ProjectStatus.COMPLETED,
                        ProjectStatus.FAILED,
                        ProjectStatus.CANCELLED,
                    ]
                )
            )
            .order_by(Project.created_at.desc())
            .limit(_MAX_PROJECTS_PER_TICK)
        )
    ).all()
    return [r[0] for r in rows]


def _resolve_interval() -> int:
    """Precompute cadence in seconds, overridable via runtime_config."""
    from app.shared_state import runtime_config as _runtime_config

    try:
        val = int(_runtime_config.get("digest_precompute_interval_seconds", 0) or 0)
    except (TypeError, ValueError):
        val = 0
    return val if val > 0 else PRECOMPUTE_INTERVAL_SECONDS


async def _precompute_one(
    session,
    *,
    hours: int,
    project_id: uuid.UUID | None,
) -> None:
    """Compute one hot-key digest and store it in the precompute cache.

    Imported lazily to avoid an import cycle (admin_digest imports nothing
    from this module at module-load time; this module reaches into the
    router only when a tick actually runs).
    """
    from app.routers.admin_digest import _compute_digest

    result = await _compute_digest(hours=hours, project_id=project_id, session=session)
    _PRECOMPUTE_CACHE.put(
        hours,
        project_id,
        result,
        computed_wall_iso=result.get("generated_at") or "",
        source=SOURCE_WORKER,
    )


async def run_precompute_tick(session_factory) -> int:
    """Run one full precompute tick over the hot key set.

    Returns the number of keys successfully refreshed. Each key computes on a
    fresh session so one slow/failing key cannot poison the others' session.
    Pure orchestration over ``_precompute_one`` — directly unit-testable with
    a session-factory stub.
    """
    refreshed = 0

    # Global (no project) view — always primed.
    try:
        async with session_factory() as session:
            await _precompute_one(
                session, hours=_DEFAULT_PRECOMPUTE_HOURS, project_id=None
            )
        refreshed += 1
    except Exception as exc:
        # One key's failure must not stop the rest of the tick.
        logger.warning("[DIGEST_PRECOMPUTE] global digest tick failed: %s", exc)

    # Per-project views for the active project set.
    try:
        async with session_factory() as session:
            project_ids = await _active_project_ids(session)
    except Exception as exc:
        logger.warning("[DIGEST_PRECOMPUTE] active-project enumeration failed: %s", exc)
        project_ids = []

    for project_id in project_ids:
        try:
            async with session_factory() as session:
                await _precompute_one(
                    session, hours=_DEFAULT_PRECOMPUTE_HOURS, project_id=project_id
                )
            refreshed += 1
        except Exception as exc:
            logger.warning(
                "[DIGEST_PRECOMPUTE] project=%s digest tick failed: %s",
                project_id,
                exc,
            )

    return refreshed


async def digest_precompute_service() -> None:
    """Leader-only background loop that primes the digest precompute cache.

    Sleeps ``_resolve_interval()`` seconds between ticks. Each tick is bounded
    by ``PRECOMPUTE_TICK_BUDGET_SECONDS`` so a wedged tick can never pin the
    task; the lifecycle monitor restarts it on crash/timeout. Cancellation
    (shutdown) propagates cleanly.
    """
    from app.database import AsyncSessionLocal

    logger.info(
        "[DIGEST_PRECOMPUTE] service started (interval=%ds, budget=%ds)",
        _resolve_interval(),
        PRECOMPUTE_TICK_BUDGET_SECONDS,
    )
    while True:
        await asyncio.sleep(_resolve_interval())
        try:
            async with asyncio.timeout(PRECOMPUTE_TICK_BUDGET_SECONDS):
                refreshed = await run_precompute_tick(AsyncSessionLocal)
            logger.debug(
                "[DIGEST_PRECOMPUTE] tick refreshed %d digest key(s)", refreshed
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # Expected, recoverable: a single slow tick is abandoned and the
            # next tick retries. The compute path is row-capped + has its own
            # statement_timeout, so a slow tick is bounded, not fatal.
            logger.warning(
                "[DIGEST_PRECOMPUTE] tick exceeded %ds budget — abandoning this "
                "tick; next tick retries",
                PRECOMPUTE_TICK_BUDGET_SECONDS,
            )
        except Exception as tick_exc:
            logger.error(
                "[DIGEST_PRECOMPUTE] tick error: %s", tick_exc, exc_info=True
            )
