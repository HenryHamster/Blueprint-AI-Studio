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
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
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

# Cooperative wall-time budget for one precompute tick. ``run_precompute_tick``
# reads elapsed time BETWEEN units of work (never mid-query) and returns a
# partial refresh once this is exceeded; the next tick resumes where this one
# left off (the round-robin offset only advances past projects actually primed).
# This REPLACES the old query-cancelling ``asyncio.timeout`` wrapper, which fired
# mid-``session.execute``, made asyncpg raise ``CancelledError`` and ejected an
# otherwise-healthy pool connection every overrun (~44/hr "[DB] Invalidated pool
# connection after CancelledError", 1:1 timestamp-identical with this tick's
# overrun). Bounded < interval so a normal tick never overlap-stacks. Each unit
# is independently bounded (5s statement_timeout on digest, 30s on health), so
# the tick stays bounded without ever cancelling a query.
PRECOMPUTE_TICK_BUDGET_SECONDS: int = 35

# Last-resort HARD timeout for the whole tick, kept only so a single pathological
# unit that ignores its own statement_timeout (e.g. a wedged connection checkout,
# before any query even runs) can never pin the worker task forever — which would
# make the ``_tick_in_progress`` guard skip every future tick. Individual queries
# are each capped by their own statement_timeout (5s digest / 30s health), but a
# unit issues SEVERAL of them, so 180s is a generous — not a provable — upper
# bound: in the common case the cooperative check returns early at
# ``PRECOMPUTE_TICK_BUDGET_SECONDS`` between units, nowhere near this. A genuine
# multi-query DB wedge (N_queries x statement_timeout) can still trip it — accepted:
# rare, not the chronic every-tick overrun the cooperative budget eliminates. If it
# does fire it cancels a query (the old failure mode), but only in that real wedge.
PRECOMPUTE_TICK_HARD_TIMEOUT_SECONDS: int = 180

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

# Number of projects processed in one tick (round-robin slice). Rather than
# priming all 25 projects every tick, each tick advances a rotating offset so
# every project gets refreshed across multiple ticks. This spreads per-project
# work over multiple 45s cycles, staying well within the 35s budget.
_PROJECTS_PER_TICK_SLICE: int = 5

# Lower statement_timeout for background precompute keys vs the on-demand
# request path. A stalled background tick should not pin a DB connection for
# 30s; skip-on-timeout is safe because the previous L1/L2 cached value remains
# servable until the next tick succeeds.
_BACKGROUND_STATEMENT_TIMEOUT_SECS: int = 5

# Round-robin offset: tracks the starting index in the active project list for
# the next tick. Advanced by _PROJECTS_PER_TICK_SLICE each tick.
_project_tick_offset: int = 0

# Overlap guard: set to True while a tick is executing. If a tick hasn't
# finished when the next interval fires (e.g. a DB hiccup delays the timeout),
# skip rather than stack a second tick on top.
_tick_in_progress: bool = False


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


# Module-level singletons — the live precompute stores for THIS instance.
# Separate stores per payload kind keep their (window, project_id) key spaces
# from colliding: digest is keyed by ``hours``, health by ``lookback_hours``.
_PRECOMPUTE_CACHE = DigestPrecomputeCache()
_HEALTH_PRECOMPUTE_CACHE = DigestPrecomputeCache()


def get_precompute_cache() -> DigestPrecomputeCache:
    """Accessor for the module-level digest precompute cache (tests can clear it)."""
    return _PRECOMPUTE_CACHE


def get_health_precompute_cache() -> DigestPrecomputeCache:
    """Accessor for the module-level health-overview precompute cache."""
    return _HEALTH_PRECOMPUTE_CACHE


# ---------------------------------------------------------------------------
# L2: cross-instance shared cache (Postgres ``digestcache`` table)
# ---------------------------------------------------------------------------
# The L1 caches above are per-instance and only the LEADER's background tick
# fills them, so non-leader instances (and any instance right after a deploy,
# when L1 is cold) would otherwise fall straight through to the heavy on-demand
# compute. The shared table lets the leader's precomputed payload be read by
# every instance with one primary-key SELECT. ALL L2 access is best-effort /
# fail-open: a cache-table error degrades to the existing on-demand path and
# never breaks the endpoint or the precompute tick.

DIGEST_KIND = "digest"
HEALTH_KIND = "health"


def shared_cache_key(kind: str, window: int, project_id: uuid.UUID | None) -> str:
    """Stable string key for an L2 row: ``"{kind}:{window}:{project|global}"``."""
    return f"{kind}:{window}:{project_id if project_id else 'global'}"


@dataclass
class SharedEntry:
    """A payload read back from the shared (L2) cache table."""

    payload: Dict[str, Any]
    computed_at: datetime


async def read_shared(
    cache_key: str,
    *,
    max_age_seconds: float,
    session,
) -> SharedEntry | None:
    """Return the shared (L2) entry for ``cache_key`` if present and fresh.

    Freshness is judged on the stored wall-clock ``computed_at`` — monotonic
    clocks are not comparable across instances/processes, and Cloud Run instance
    clocks are NTP-synced so skew is negligible against the multi-minute
    ``max_age_seconds`` window. Only ``SOURCE_WORKER`` rows are served — the same
    invariant the L1 read enforces via ``get_fresh(worker_only=True)``: only the
    leader's continuously-refreshed worker writes are trustworthy for the long
    window. Today every L2 row is worker-written, so this is structural
    future-proofing against any non-worker writer. Returns ``None`` on miss,
    staleness, non-worker source, or ANY error (fail-open: the caller then uses
    its on-demand fallback).
    """
    try:
        from app.models import DigestCacheEntry
        from app.time_utils import _utcnow

        row = await session.get(DigestCacheEntry, cache_key)
        if row is None or row.computed_at is None:
            return None
        if row.source != SOURCE_WORKER:
            return None
        if (_utcnow() - row.computed_at).total_seconds() > max_age_seconds:
            return None
        return SharedEntry(payload=row.payload, computed_at=row.computed_at)
    except Exception as exc:
        logger.debug(
            "[DIGEST_PRECOMPUTE] shared-cache read failed for %s: %s", cache_key, exc
        )
        # A failed SELECT aborts the current transaction; roll it back so the
        # caller's on-demand fallback compute runs on a clean session instead of
        # raising PendingRollbackError. The read path is invoked before any
        # endpoint writes, so this discards nothing.
        with contextlib.suppress(Exception):
            await session.rollback()
        return None


async def write_shared(
    cache_key: str,
    payload: Dict[str, Any],
    *,
    computed_at: datetime,
    session,
) -> bool:
    """UPSERT a precomputed payload into the shared (L2) table. Fail-open.

    Uses ``session.merge`` (SELECT-then-INSERT/UPDATE by primary key) so it is
    dialect-agnostic (Postgres in prod, SQLite in the test fixture). Commits on
    success. Returns True on success; on any error the in-memory L1 write has
    already succeeded, so a failed L2 write only costs cross-instance sharing for
    one tick.
    """
    try:
        from app.models import DigestCacheEntry

        await session.merge(
            DigestCacheEntry(
                cache_key=cache_key,
                payload=payload,
                computed_at=computed_at,
                source=SOURCE_WORKER,
            )
        )
        await session.commit()
        return True
    except Exception as exc:
        logger.warning(
            "[DIGEST_PRECOMPUTE] shared-cache write failed for %s: %s", cache_key, exc
        )
        with contextlib.suppress(Exception):
            await session.rollback()
        return False


async def cleanup_stale_shared(session, *, older_than_seconds: float) -> int:
    """Delete L2 rows whose ``computed_at`` is older than the cutoff. Fail-open.

    Keys for no-longer-hot views (e.g. a project that went terminal) stop being
    refreshed and would otherwise linger. The table is tiny, so this is a cheap
    occasional sweep. Returns the number of rows deleted (0 on any error).
    """
    try:
        from sqlalchemy import delete
        from app.models import DigestCacheEntry
        from app.time_utils import _utcnow

        cutoff = _utcnow() - timedelta(seconds=older_than_seconds)
        result = await session.execute(
            delete(DigestCacheEntry).where(DigestCacheEntry.computed_at < cutoff)
        )
        await session.commit()
        return result.rowcount or 0
    except Exception as exc:
        logger.debug("[DIGEST_PRECOMPUTE] shared-cache cleanup failed: %s", exc)
        with contextlib.suppress(Exception):
            await session.rollback()
        return 0


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


def payload_age_seconds(generated_at: str | None) -> float | None:
    """Seconds elapsed since a cached payload's own ``generated_at`` stamp.

    Endpoints that serve a ``DigestCache`` payload derive freshness from the
    payload's OWN wire stamp rather than the cache entry's wall age, so a
    served-stale payload reports its true (old) age instead of looking fresh.

    That stamp is written by ``utc_iso(...)``, which since #17890 carries a
    ``+00:00`` offset, while ``_utcnow()`` is naive UTC. Subtracting the two
    directly raises ``TypeError: can't subtract offset-naive and offset-aware
    datetimes``. Every call site had wrapped that subtraction in
    ``except (ValueError, TypeError)``, so the breakage surfaced not as an
    error but as a permanently ``None`` ``precompute_age_seconds`` -- the
    admin UI's staleness dimmer silently going dead on every affected card.

    Parsing is normalised through ``_coerce_naive_utc`` so BOTH formats work:
    the zone-carrying stamp written now, and the zone-less one still sitting
    in any payload cached before this change rolled out.

    Returns ``None`` only when the stamp is absent or genuinely unparseable.
    """
    from app.time_utils import _coerce_naive_utc, _utcnow

    if not generated_at:
        return None
    try:
        parsed = _coerce_naive_utc(datetime.fromisoformat(generated_at))
    except (ValueError, TypeError):
        return None
    return max(0.0, (_utcnow() - parsed).total_seconds())


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
    from app.time_utils import _utcnow

    result = await _compute_digest(
        hours=hours,
        project_id=project_id,
        session=session,
        _statement_timeout_secs=_BACKGROUND_STATEMENT_TIMEOUT_SECS,
    )
    _PRECOMPUTE_CACHE.put(
        hours,
        project_id,
        result,
        computed_wall_iso=result.get("generated_at") or "",
        source=SOURCE_WORKER,
    )
    # L2: publish to the shared table so non-leader / cold-cache instances can
    # serve this payload without re-running the heavy scan. Best-effort.
    await write_shared(
        shared_cache_key(DIGEST_KIND, hours, project_id),
        result,
        computed_at=_utcnow(),
        session=session,
    )


async def _precompute_health_one(
    session,
    *,
    lookback_hours: int,
    project_id: uuid.UUID | None,
) -> None:
    """Compute one hot-key health-overview payload and store it in L1 + L2.

    Health-overview previously had NO precompute (every request live-computed
    the multi-thousand-row scan), which made it the single largest source of the
    2026-06-03 ``statement_timeout`` 500 burst. Lazily imports the router to
    avoid an import cycle (mirrors ``_precompute_one``).
    """
    from app.routers.admin_health import _compute_health_overview
    from app.time_utils import _utcnow

    result = await _compute_health_overview(
        project_id=project_id, lookback_hours=lookback_hours, session=session
    )
    _HEALTH_PRECOMPUTE_CACHE.put(
        lookback_hours,
        project_id,
        result,
        computed_wall_iso=result.get("generated_at") or "",
        source=SOURCE_WORKER,
    )
    await write_shared(
        shared_cache_key(HEALTH_KIND, lookback_hours, project_id),
        result,
        computed_at=_utcnow(),
        session=session,
    )


@dataclass(frozen=True)
class TickResult:
    """Outcome of one precompute tick.

    ``refreshed`` is the number of hot keys successfully refreshed this tick —
    the same meaning the old bare ``int`` return carried, so callers that only
    care about the count read ``result.refreshed``.

    ``partial`` is True when the tick returned early because it reached the
    cooperative wall-time budget BETWEEN units of work: the remaining keys are
    deferred to the next tick, which resumes where this one left off (the
    round-robin offset only advances past projects actually primed). A partial
    tick is a normal, expected outcome under load — NOT an error — and, unlike
    the old query-cancelling ``asyncio.timeout``, it never cancels an in-flight
    query or invalidates a pool connection.
    """

    refreshed: int
    partial: bool = False


async def run_precompute_tick(
    session_factory,
    *,
    budget_seconds: float | None = None,
) -> TickResult:
    """Run one precompute tick over the hot key set, cooperatively time-boxed.

    Returns a :class:`TickResult`: ``refreshed`` counts the hot keys primed this
    tick; ``partial`` is True if the tick stopped early on the cooperative budget.
    Each key computes on a fresh session so one slow/failing key cannot poison the
    others' session. Pure orchestration over ``_precompute_one`` /
    ``_precompute_health_one`` — directly unit-testable with a session-factory stub.

    Budget model (WI-2). The tick reads elapsed wall time BETWEEN whole units of
    work (never mid-query) and returns a partial refresh once ``budget_seconds``
    (default ``PRECOMPUTE_TICK_BUDGET_SECONDS``) is exceeded, handing the remaining
    per-project keys to the next tick. The round-robin ``_project_tick_offset`` only
    advances past projects actually primed, so the next tick resumes where this one
    left off rather than skipping the unprimed tail of the slice. Each unit is
    independently bounded — the digest compute under a 5s LOCAL statement_timeout
    and the health compute under 30s — so a single unit cannot overrun by more than
    that, and the tick never needs a query-cancelling hard timeout to stay bounded.
    This replaces the old ``asyncio.timeout(PRECOMPUTE_TICK_BUDGET_SECONDS)`` wrapper
    that fired mid-``session.execute``, made asyncpg raise ``CancelledError``, and
    ejected an otherwise-healthy pool connection on every overrun (the ~44/hr
    "[DB] Invalidated pool connection after CancelledError" bleed).

    Priming order: the GLOBAL views (both kinds — the most-hit keys) are ALWAYS
    primed first; only the per-project round-robin slice and the L2 prune are
    subject to the cooperative cutoff. Whatever the budget truncates degrades to
    the bounded on-demand path (the prior behavior), never an error.
    """
    global _project_tick_offset

    if budget_seconds is None:
        budget_seconds = float(PRECOMPUTE_TICK_BUDGET_SECONDS)
    started_monotonic = time.monotonic()

    def _budget_exceeded() -> bool:
        return (time.monotonic() - started_monotonic) >= budget_seconds

    refreshed = 0
    partial = False

    async def _prime(label: str, coro_factory) -> None:
        nonlocal refreshed
        try:
            async with session_factory() as session:
                await coro_factory(session)
            refreshed += 1
        except Exception as exc:
            # One key's failure must not stop the rest of the tick.
            logger.warning("[DIGEST_PRECOMPUTE] %s tick failed: %s", label, exc)

    # Active project set — enumerated once and shared by both payload kinds.
    try:
        async with session_factory() as session:
            project_ids = await _active_project_ids(session)
    except Exception as exc:
        logger.warning("[DIGEST_PRECOMPUTE] active-project enumeration failed: %s", exc)
        project_ids = []

    # --- GLOBAL views first (both kinds). The no-project DashboardHome /
    #     CatchUpPanel polls are the most-hit keys and the dominant source of the
    #     health-overview timeouts, so they are ALWAYS primed — the cooperative
    #     cutoff applies only to the per-project round-robin below, so truncation
    #     is spent on lower-priority per-project keys, never the globals. ---
    await _prime(
        "global digest",
        lambda s: _precompute_one(s, hours=_DEFAULT_PRECOMPUTE_HOURS, project_id=None),
    )
    await _prime(
        "global health",
        lambda s: _precompute_health_one(
            s, lookback_hours=_DEFAULT_PRECOMPUTE_HOURS, project_id=None
        ),
    )

    # --- Per-project views: round-robin slice of projects per tick. ---
    # Rotate the full list by the current offset, then take the first slice.
    # This spreads the work evenly: with 25 projects and a slice of 5, each
    # project is hit once every 5 ticks (~225s) rather than every tick.
    if project_ids:
        n = len(project_ids)
        offset = _project_tick_offset % n
        rotated = project_ids[offset:] + project_ids[:offset]
        tick_slice = rotated[:_PROJECTS_PER_TICK_SLICE]
    else:
        n = 0
        offset = 0
        tick_slice = []

    # Prime each project's digest + health together so the round-robin offset can
    # advance by whole projects fully primed. The cooperative budget is checked
    # BETWEEN projects (never mid-query): once exceeded, we stop and mark the tick
    # partial, leaving the unprimed projects for the next tick.
    projects_done = 0
    for project_id in tick_slice:
        if _budget_exceeded():
            partial = True
            break
        await _prime(
            f"project={project_id} digest",
            lambda s, pid=project_id: _precompute_one(
                s, hours=_DEFAULT_PRECOMPUTE_HOURS, project_id=pid
            ),
        )
        await _prime(
            f"project={project_id} health",
            lambda s, pid=project_id: _precompute_health_one(
                s, lookback_hours=_DEFAULT_PRECOMPUTE_HOURS, project_id=pid
            ),
        )
        projects_done += 1

    # Advance the offset only past projects actually primed this tick, so a
    # budget-truncated tick resumes at the first project it did not reach on the
    # next tick (rather than skipping the unprimed tail of the slice).
    if n:
        _project_tick_offset = (offset + projects_done) % n

    # --- Prune L2 rows for keys that are no longer hot (cheap bounded DELETE).
    #     Cutoff = 2x the freshness window so a briefly-restarting worker never
    #     prunes rows a reader might still legitimately serve. Skipped when the
    #     tick already hit its budget — deferring an occasional sweep one tick is
    #     harmless, and skipping it keeps the cooperative bound honest. ---
    if not (partial or _budget_exceeded()):
        try:
            async with session_factory() as session:
                deleted = await cleanup_stale_shared(
                    session, older_than_seconds=PRECOMPUTE_STALE_FALLBACK_SECONDS * 2
                )
            if deleted:
                logger.debug(
                    "[DIGEST_PRECOMPUTE] pruned %d stale shared-cache row(s)", deleted
                )
        except Exception as exc:
            logger.debug("[DIGEST_PRECOMPUTE] shared-cache prune failed: %s", exc)

    return TickResult(refreshed=refreshed, partial=partial)


async def digest_precompute_service() -> None:
    """Leader-only background loop that primes the digest precompute cache.

    Sleeps ``_resolve_interval()`` seconds between ticks. Each tick self-limits
    to ``PRECOMPUTE_TICK_BUDGET_SECONDS`` via a COOPERATIVE wall-time budget
    checked between units of work (see ``run_precompute_tick``): an over-budget
    tick returns a partial refresh — it never cancels an in-flight query — and
    the next tick resumes where it left off. A generous
    ``PRECOMPUTE_TICK_HARD_TIMEOUT_SECONDS`` (>= 3x the cooperative budget) wraps
    the tick purely as a last-resort guard against a wedged unit that ignores its
    own statement_timeout. Because a unit issues several queries (each capped by
    its own statement_timeout), 180s is a generous — not a provable — upper bound:
    the common every-tick path never approaches it, though a genuine multi-query DB
    wedge can still trip it (accepted: rare). The lifecycle monitor restarts the
    loop on crash. Cancellation (shutdown) propagates cleanly.

    The ``_tick_in_progress`` flag prevents tick overlap: if a previous tick
    hasn't finished when the next interval fires (e.g. a hard-timeout wedge held
    a tick past its deadline), the new interval is skipped rather than stacking a
    second concurrent tick.
    """
    global _tick_in_progress
    from app.database import AsyncSessionLocal

    logger.info(
        "[DIGEST_PRECOMPUTE] service started (interval=%ds, budget=%ds, hard_timeout=%ds)",
        _resolve_interval(),
        PRECOMPUTE_TICK_BUDGET_SECONDS,
        PRECOMPUTE_TICK_HARD_TIMEOUT_SECONDS,
    )
    while True:
        await asyncio.sleep(_resolve_interval())
        if _tick_in_progress:
            logger.warning(
                "[DIGEST_PRECOMPUTE] previous tick still running — skipping this interval"
            )
            continue
        _tick_in_progress = True
        try:
            async with asyncio.timeout(PRECOMPUTE_TICK_HARD_TIMEOUT_SECONDS):
                result = await run_precompute_tick(AsyncSessionLocal)
            if result.partial:
                # Normal under load: the cooperative budget stopped the tick
                # between units (no query cancelled); the next tick resumes.
                logger.info(
                    "[DIGEST_PRECOMPUTE] tick hit the %ds cooperative budget — "
                    "refreshed %d key(s), returned partial; next tick resumes "
                    "where this one left off",
                    PRECOMPUTE_TICK_BUDGET_SECONDS,
                    result.refreshed,
                )
            else:
                logger.debug(
                    "[DIGEST_PRECOMPUTE] tick refreshed %d digest key(s)",
                    result.refreshed,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # LAST-RESORT safety only: the cooperative budget returns partial
            # between units long before this generous hard cap, so this fires
            # only if a single unit wedged past its own statement_timeout (e.g. a
            # stuck connection checkout). Abandoning the tick here CAN cancel a
            # query (the old failure mode), but only in a genuine wedge — not the
            # chronic every-tick overrun the cooperative budget eliminated. The
            # next tick retries.
            logger.warning(
                "[DIGEST_PRECOMPUTE] tick exceeded the %ds hard-timeout safety — "
                "abandoning this tick; next tick retries",
                PRECOMPUTE_TICK_HARD_TIMEOUT_SECONDS,
            )
        except Exception as tick_exc:
            logger.error("[DIGEST_PRECOMPUTE] tick error: %s", tick_exc, exc_info=True)
        finally:
            _tick_in_progress = False
