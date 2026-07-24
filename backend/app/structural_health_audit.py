"""Recurring resource-and-scalability structural-health audit (parent: resource/
scalability structural-health audit lane).

This module is the MUST-HAVE foundation of a recurring structural-health audit
that detects the class of structural problem that recently caused a Postgres
saturation outage: unbounded JSONB fat-row growth in ``task.context``. When the
audit trips, it enrolls a DEDUPLICATED finding into the EXISTING evergreen
backlog ledger (:class:`app.models.EvergreenBacklogItem`) via
:func:`app.evergreen_backlog.enroll_backlog_item` — so a structural finding gets
the SAME research -> reproduce-on-HEAD -> actionability -> implementation triage
as a correctness finding, with NO new bespoke queue and NO schema change.

Hard invariants (mirrors evergreen_backlog.py's design):

* NO schema change. Findings reuse ``EvergreenBacklogItem`` (deduped on
  ``(project_id, finding_key)``); measurements live in the process-local,
  bounded in-memory observability singleton below — no table, no column, no
  Alembic migration.
* Mode gating (``structural_health_audit_mode``): ``off`` never acts; ``observe``
  measures and records the would-be finding but enrolls nothing (default); ``enforce``
  enrolls.
* Detection must not itself become a load problem. The fat-row check measures
  per-row context byte size DB-SIDE using a dialect-safe byte/length aggregate
  over a BOUNDED result set (ORDER BY size DESC LIMIT N) — it NEVER loads full
  ``task.context`` rows or scans the whole tasks table into memory. The
  projection selects only ``id`` / ``project_id`` / the size aggregate.
* Stable finding_key. Every fat-row finding uses
  ``structural-health:fat-row:task-context`` so the same structural finding is
  enrolled at most once per project — a re-detecting cycle enrolls nothing.

EXTENSION SEAM / CONTEXT GAP: the originating operator steer (message
``496f2009``) was truncated after point (2) and may name additional structural
root causes (connection-pool saturation, JSONB bloat, GIN-index overhead, ...);
it could NOT be retrieved through available tooling (``./fetch`` supports only
direction/playbook/section/knowledge/qualification and the 8-char ref is not a
knowledge UUID). This module ships the enumerated fat-row check ONLY and leaves
a clear extension seam: additional checks register their own stable finding_key
and call :func:`enroll_structural_finding` / record into the observability
singleton, then get wired into :func:`run_structural_health_audit` alongside the
fat-row check. A sibling task ("extend structural-health audit checklist axes")
owns those further axes.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List

from sqlalchemy import Text, cast, func, text
from sqlmodel import select

from app.database import resolve_async_engine, resolve_dialect_name
from app.evergreen_backlog import enroll_backlog_item
from app.models import EvergreenBacklogItem, Task
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# --- Closed vocabularies -----------------------------------------------------

#: Audit modes (runtime config ``structural_health_audit_mode``). Mirrors
#: ``evergreen_backlog.FILL_MODES`` exactly.
AUDIT_MODES = frozenset({"off", "observe", "enforce"})

#: Stable finding_key for the fat-row / oversized-``task.context`` check. The
#: evergreen seam dedups on ``(project_id, finding_key)``, so reusing this exact
#: key means the same structural finding is never re-enrolled per project.
FAT_ROW_FINDING_KEY = "structural-health:fat-row:task-context"

#: Stable finding_key for the JSONB dead-tuple bloat check (task table).
JSONB_BLOAT_FINDING_KEY = "structural-health:jsonb-bloat:task"

#: Stable finding_key for GIN-index overhead on the task table.
GIN_OVERHEAD_FINDING_KEY = "structural-health:gin-overhead:task-context"

#: Stable finding_key for connection-pool saturation.
POOL_SATURATION_FINDING_KEY = "structural-health:pool-saturation"

#: Provenance stamped on every structural-audit-enrolled ledger row, distinct
#: from evergreen-fill's ``manual`` / ``deferred_memo`` provenance values so
#: structural findings are attributable on the admin surface.
STRUCTURAL_AUDIT_PROVENANCE = "structural_audit"

# --- Defaults (operator-overridable via runtime_config / PATCH settings) -----

#: Default mode — OBSERVE to measure and record findings without enrolling.
DEFAULT_AUDIT_MODE = "observe"
#: Default cadence between audit sweeps.
DEFAULT_INTERVAL_MINUTES = 60
#: Default fat-row byte threshold (1 MiB). A ``task.context`` above this is a
#: structural fat-row candidate — well below the multi-megabyte rows that caused
#: the saturation outage, but far above a healthy row (low single-digit KiB).
DEFAULT_FAT_ROW_BYTES = 1_048_576
#: Bounded number of largest rows the measurement query returns. Keeps the scan
#: cheap and the observability snapshot small regardless of table size.
TOP_N_LARGEST_ROWS = 20
#: Default dead-tuple ratio threshold for JSONB bloat check (20%). Above this
#: fraction indicates bloat pressure that can inflate JSONB operation costs.
DEFAULT_JSONB_BLOAT_DEAD_RATIO = 0.2
#: Default GIN-index overhead ratio threshold (200% of table size). A GIN index
#: substantially larger than its source table is a signal of pending-list buildup
#: or insufficient autovacuum pressure.
DEFAULT_GIN_OVERHEAD_RATIO = 2.0
#: Default pool-saturation percentage threshold (90%). Above this fraction the
#: connection pool is near capacity, meaning new requests may queue or time out.
DEFAULT_POOL_SATURATION_PCT = 0.9


# --- Bounded DB-side measurement ---------------------------------------------


@dataclass(frozen=True)
class RowSizeMeasurement:
    """One measured row: its ids and its DB-computed context byte size.

    Carries NO context payload — only the bounded measurement, so recording or
    snapshotting a measurement can never itself materialize a fat row.
    """

    task_id: str
    project_id: str
    bytes: int


def _context_byte_size_expr(session):
    """Dialect-safe SQL expression for a row's ``task.context`` byte size.

    Computed entirely DB-side; never loads the column into Python. On Postgres
    ``octet_length(context::text)`` measures the UTF-8 byte size of the JSON
    payload (the logical fat-row size that drives saturation); on SQLite (the
    test backend, where JSONB renders as JSON text) ``length(cast(context AS
    TEXT))`` measures the character length, which equals the byte size for the
    ASCII/JSON content this audit cares about. Mirrors the dialect-safety care
    in :mod:`app.evergreen_backlog` (idempotency-key ``LIKE`` over JSON-path
    predicates).
    """
    text_ctx = cast(Task.context, Text)
    if resolve_dialect_name(session) == "postgresql":
        return func.octet_length(text_ctx)
    return func.length(text_ctx)


async def measure_largest_context_rows(
    session, *, limit: int = TOP_N_LARGEST_ROWS
) -> List[RowSizeMeasurement]:
    """Return the ``limit`` largest ``task.context`` rows, biggest first.

    BOUNDED + DB-side: ``ORDER BY <size> DESC LIMIT N`` over a projection of
    only ``id`` / ``project_id`` / the size aggregate. The whole tasks table is
    never scanned into memory and no full ``context`` payload is ever loaded —
    the detection path cannot itself become the load problem it is detecting.
    """
    size_expr = _context_byte_size_expr(session)
    stmt = (
        select(Task.id, Task.project_id, size_expr.label("ctx_bytes"))
        .order_by(size_expr.desc())
        .limit(max(1, int(limit)))
    )
    rows = (await session.execute(stmt)).all()
    return [
        RowSizeMeasurement(
            task_id=str(task_id),
            project_id=str(project_id),
            bytes=int(ctx_bytes or 0),
        )
        for task_id, project_id, ctx_bytes in rows
    ]


# --- Bounded in-memory observability -----------------------------------------

_RECENT_DECISIONS_MAX = 50
_LARGEST_ROWS_MAX = TOP_N_LARGEST_ROWS


@dataclass
class _StructuralHealthObservability:
    """Process-local, bounded measurements + per-cycle decisions for admin.

    Mirrors :class:`app.evergreen_backlog._FillObservability`: a bounded ring of
    recent decisions plus the most-recent cycle's largest measured rows, so the
    admin surface can show what the audit saw and decided without any new table.
    """

    cycles: int = 0
    enrolled_total: int = 0
    last_decision: Dict[str, Any] | None = None
    largest_rows: List[Dict[str, Any]] = field(default_factory=list)
    recent: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_RECENT_DECISIONS_MAX)
    )
    # Per-axis last decisions (additive; existing keys preserved).
    last_jsonb_bloat_decision: Dict[str, Any] | None = None
    last_gin_overhead_decision: Dict[str, Any] | None = None
    last_pool_saturation_decision: Dict[str, Any] | None = None

    def record(
        self,
        *,
        decision: Dict[str, Any],
        measurements: List[RowSizeMeasurement],
    ) -> None:
        self.cycles += 1
        self.enrolled_total += int(decision.get("enrolled", 0))
        self.last_decision = dict(decision)
        self.largest_rows = [
            {"task_id": m.task_id, "project_id": m.project_id, "bytes": m.bytes}
            for m in measurements[:_LARGEST_ROWS_MAX]
        ]
        self.recent.append(dict(decision))

    def record_axis(self, decision: Dict[str, Any]) -> None:
        """Record a non-fat-row axis decision into the per-axis slot."""
        check = decision.get("check", "")
        self.enrolled_total += int(decision.get("enrolled", 0))
        if check == "jsonb_bloat":
            self.last_jsonb_bloat_decision = dict(decision)
        elif check == "gin_overhead":
            self.last_gin_overhead_decision = dict(decision)
        elif check == "pool_saturation":
            self.last_pool_saturation_decision = dict(decision)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "cycles": self.cycles,
            "enrolled_total": self.enrolled_total,
            "last_decision": dict(self.last_decision) if self.last_decision else None,
            "largest_rows": list(self.largest_rows),
            "recent": list(self.recent),
            "last_jsonb_bloat_decision": (
                dict(self.last_jsonb_bloat_decision)
                if self.last_jsonb_bloat_decision
                else None
            ),
            "last_gin_overhead_decision": (
                dict(self.last_gin_overhead_decision)
                if self.last_gin_overhead_decision
                else None
            ),
            "last_pool_saturation_decision": (
                dict(self.last_pool_saturation_decision)
                if self.last_pool_saturation_decision
                else None
            ),
        }


# Module-global singleton (mirrors evergreen_backlog._OBSERVABILITY).
_OBSERVABILITY = _StructuralHealthObservability()


def get_structural_health_observability() -> Dict[str, Any]:
    """Bounded in-memory audit measurements + recent per-cycle decisions."""
    return _OBSERVABILITY.snapshot()


def _reset_observability_for_tests() -> None:
    """Test helper — clear the process-local counters."""
    global _OBSERVABILITY
    _OBSERVABILITY = _StructuralHealthObservability()


# --- Durable DB writer + reader (survive process restarts) -------------------

_AUDIT_LANE = "structural_health"


async def log_audit_cycle_to_db(
    session,
    *,
    ts: datetime,
    mode: str,
    tripped: bool,
    enrolled: int,
    payload: Dict[str, Any],
) -> None:
    """Write one durable row per audit cycle into AuditLaneDecisionLog.

    Best-effort: any DB error is logged and swallowed so it can never crash the
    reaper cycle or cause a rollback that would undo a legitimately enrolled finding.
    """
    try:
        from app.models import AuditLaneDecisionLog

        row = AuditLaneDecisionLog(
            ts=ts,
            lane=_AUDIT_LANE,
            mode=mode,
            tripped=tripped,
            enrolled=enrolled,
            payload=payload,
        )
        session.add(row)
        await session.flush()
    except Exception:
        logger.warning(
            "[STRUCTURAL] log_audit_cycle_to_db failed — in-memory signal preserved",
            exc_info=True,
        )


async def get_structural_health_observability_from_db(
    session, *, window_hours: int = 24
) -> Dict[str, Any]:
    """Return aggregate observe-window stats from the durable AuditLaneDecisionLog.

    Queries rows within the trailing ``window_hours`` for the structural_health
    lane and returns: total cycles, tripped count (would-have-fired), and enrolled
    count.  Falls back to an empty result rather than raising.
    """
    try:
        from app.models import AuditLaneDecisionLog

        cutoff = _utcnow() - timedelta(hours=window_hours)
        rows = (
            await session.execute(
                select(AuditLaneDecisionLog)
                .where(
                    AuditLaneDecisionLog.lane == _AUDIT_LANE,
                    AuditLaneDecisionLog.ts >= cutoff,
                )
                .order_by(AuditLaneDecisionLog.ts.desc())
                .limit(500)
            )
        ).scalars().all()

        total = len(rows)
        tripped_count = sum(1 for r in rows if r.tripped)
        enrolled_total = sum(r.enrolled for r in rows)
        last_ts = rows[0].ts.isoformat() if rows else None
        return {
            "source": "db",
            "window_hours": window_hours,
            "cycles": total,
            "tripped_count": tripped_count,
            "enrolled_total": enrolled_total,
            "last_cycle_at": last_ts,
        }
    except Exception:
        logger.warning(
            "[STRUCTURAL] get_structural_health_observability_from_db failed",
            exc_info=True,
        )
        return {
            "source": "db",
            "window_hours": window_hours,
            "cycles": 0,
            "tripped_count": 0,
            "enrolled_total": 0,
            "last_cycle_at": None,
        }


# --- Enrollment (deduped, mode-gated) ----------------------------------------


def _fat_row_finding_detail(measurement: RowSizeMeasurement, threshold: int) -> str:
    return (
        "Structural-health audit detected an oversized task.context row "
        f"(task {measurement.task_id}, ~{measurement.bytes} bytes >= threshold "
        f"{threshold} bytes). Unbounded JSONB fat-row growth in task.context "
        "is the structural class that caused a Postgres saturation outage. "
        "Investigate whether the gap still reproduces on HEAD and bound the "
        "growth at its root (e.g. trim/externalize the accumulating field)."
    )


async def _finding_already_enrolled(session, *, project_id, finding_key: str) -> bool:
    """True when a NON-retired ledger row already tracks this finding_key.

    The enrollment seam reopens a ``retired`` row, so a retired finding is NOT
    treated as already-enrolled (it should be requeued). Any live status
    (enrolled / spawned / advanced / verified_stale) means a re-detecting cycle
    must enroll nothing — the acceptance "second cycle enrolls nothing" rule.
    """
    existing = (
        await session.execute(
            select(EvergreenBacklogItem.status).where(
                EvergreenBacklogItem.project_id == project_id,
                EvergreenBacklogItem.finding_key == finding_key,
            )
        )
    ).scalar_one_or_none()
    return existing is not None and existing != "retired"


async def enroll_structural_finding(
    session,
    *,
    project_id,
    finding_key: str,
    title: str,
    detail: str,
) -> EvergreenBacklogItem | None:
    """Enroll a deduped structural finding into the evergreen ledger.

    Returns the newly-created row, or ``None`` when a live row already tracks
    this ``(project_id, finding_key)`` (re-detection → enroll nothing). Reuses
    the existing :func:`app.evergreen_backlog.enroll_backlog_item` seam (so the
    item flows through the exact same downstream triage as a correctness
    finding) and stamps the distinct ``structural_audit`` provenance.
    """
    if await _finding_already_enrolled(
        session, project_id=project_id, finding_key=finding_key
    ):
        return None
    return await enroll_backlog_item(
        session,
        project_id=project_id,
        finding_key=finding_key,
        title=title,
        detail=detail,
        provenance=STRUCTURAL_AUDIT_PROVENANCE,
    )


# --- Helpers for global-finding enrollment -----------------------------------


async def _get_distinct_project_ids(session) -> List[str]:
    """Return distinct project_ids from the Task table (bounded to 50).

    Used to enroll global-infrastructure findings (pool saturation, JSONB bloat,
    GIN overhead) once per project so the finding flows through the same per-project
    triage queue as correctness findings.
    """
    stmt = select(Task.project_id).distinct().limit(50)
    result = await session.execute(stmt)
    return [str(pid) for pid in result.scalars().all() if pid is not None]


# --- JSONB-bloat check (Postgres-only, skips on SQLite) ----------------------


async def run_jsonb_bloat_check(
    session,
    *,
    mode: str,
    dead_ratio_threshold: float,
) -> Dict[str, Any]:
    """Check dead-tuple ratio on the task table as a proxy for JSONB bloat.

    Uses ``pg_stat_user_tables`` — a cheap catalog stat, no lock, no full scan.
    Skips entirely on non-Postgres backends (safe degrade). Enrolls one
    deduplicated finding per project in ``enforce`` mode when the ratio exceeds
    ``dead_ratio_threshold``.
    """
    if resolve_dialect_name(session) != "postgresql":
        decision: Dict[str, Any] = {
            "check": "jsonb_bloat",
            "mode": mode,
            "skipped": True,
            "reason": "non-postgresql backend",
            "enrolled": 0,
        }
        _OBSERVABILITY.record_axis(decision)
        return decision

    try:
        row = (
            await session.execute(
                text(
                    "SELECT n_live_tup, n_dead_tup, "
                    "CASE WHEN n_live_tup + n_dead_tup > 0 "
                    "  THEN n_dead_tup::float / (n_live_tup + n_dead_tup) "
                    "  ELSE 0 END AS dead_ratio "
                    "FROM pg_stat_user_tables WHERE relname = 'task'"
                )
            )
        ).one_or_none()
    except Exception:
        logger.exception("[STRUCTURAL] jsonb_bloat catalog query failed — skipping")
        decision = {
            "check": "jsonb_bloat",
            "mode": mode,
            "skipped": True,
            "reason": "catalog query error",
            "enrolled": 0,
        }
        _OBSERVABILITY.record_axis(decision)
        return decision

    if row is None:
        decision = {
            "check": "jsonb_bloat",
            "mode": mode,
            "skipped": True,
            "reason": "no pg_stat row for task table",
            "enrolled": 0,
        }
        _OBSERVABILITY.record_axis(decision)
        return decision

    n_live, n_dead, dead_ratio = (
        int(row.n_live_tup),
        int(row.n_dead_tup),
        float(row.dead_ratio),
    )
    tripped = dead_ratio >= dead_ratio_threshold

    enrolled = 0
    enrolled_projects: List[str] = []
    if tripped and mode == "enforce":
        project_ids = await _get_distinct_project_ids(session)
        for pid in project_ids:
            item = await enroll_structural_finding(
                session,
                project_id=pid,
                finding_key=JSONB_BLOAT_FINDING_KEY,
                title="Structural health: JSONB dead-tuple bloat on task table",
                detail=(
                    f"pg_stat_user_tables reports dead_ratio={dead_ratio:.2%} "
                    f"(n_live={n_live}, n_dead={n_dead}) on the task table, "
                    f"exceeding threshold {dead_ratio_threshold:.2%}. "
                    "High dead-tuple ratios inflate JSONB operation costs and "
                    "indicate autovacuum pressure. Investigate VACUUM frequency "
                    "and per-table autovacuum settings."
                ),
            )
            if item is not None:
                enrolled += 1
                enrolled_projects.append(pid)
        if enrolled:
            await session.flush()

    decision = {
        "check": "jsonb_bloat",
        "mode": mode,
        "dead_ratio_threshold": dead_ratio_threshold,
        "dead_ratio": round(dead_ratio, 4),
        "n_live": n_live,
        "n_dead": n_dead,
        "tripped": tripped,
        "enrolled": enrolled,
        "enrolled_projects": enrolled_projects,
    }
    _OBSERVABILITY.record_axis(decision)
    return decision


# --- GIN-index overhead check (Postgres-only, skips on SQLite) ---------------


async def run_gin_overhead_check(
    session,
    *,
    mode: str,
    overhead_ratio_threshold: float,
) -> Dict[str, Any]:
    """Check GIN-index size vs table size on the task table.

    A GIN index substantially larger than its source table indicates pending-list
    buildup or insufficient autovacuum pressure (``VACUUM`` cleans GIN pending
    lists). Uses catalog size functions — cheap, no lock, no full scan. Skips
    entirely on non-Postgres backends.
    """
    if resolve_dialect_name(session) != "postgresql":
        decision: Dict[str, Any] = {
            "check": "gin_overhead",
            "mode": mode,
            "skipped": True,
            "reason": "non-postgresql backend",
            "enrolled": 0,
        }
        _OBSERVABILITY.record_axis(decision)
        return decision

    try:
        rows = (
            await session.execute(
                text(
                    "SELECT s.indexrelname, "
                    "  pg_relation_size(s.indexrelid) AS idx_size, "
                    "  pg_relation_size(s.relid) AS tbl_size, "
                    "  CASE WHEN pg_relation_size(s.relid) > 0 "
                    "    THEN pg_relation_size(s.indexrelid)::float "
                    "         / pg_relation_size(s.relid) "
                    "    ELSE 0 END AS overhead_ratio "
                    "FROM pg_stat_user_indexes s "
                    "JOIN pg_index i ON i.indexrelid = s.indexrelid "
                    "JOIN pg_class c ON c.oid = s.indexrelid "
                    "JOIN pg_am a ON a.oid = c.relam "
                    "WHERE s.relname = 'task' AND a.amname = 'gin' "
                    "ORDER BY overhead_ratio DESC LIMIT 5"
                )
            )
        ).all()
    except Exception:
        logger.exception("[STRUCTURAL] gin_overhead catalog query failed — skipping")
        decision = {
            "check": "gin_overhead",
            "mode": mode,
            "skipped": True,
            "reason": "catalog query error",
            "enrolled": 0,
        }
        _OBSERVABILITY.record_axis(decision)
        return decision

    offenders = [
        {
            "index": str(r.indexrelname),
            "idx_size": int(r.idx_size),
            "tbl_size": int(r.tbl_size),
            "overhead_ratio": round(float(r.overhead_ratio), 4),
        }
        for r in rows
        if float(r.overhead_ratio) >= overhead_ratio_threshold
    ]

    enrolled = 0
    enrolled_projects: List[str] = []
    if offenders and mode == "enforce":
        project_ids = await _get_distinct_project_ids(session)
        worst = offenders[0]
        for pid in project_ids:
            item = await enroll_structural_finding(
                session,
                project_id=pid,
                finding_key=GIN_OVERHEAD_FINDING_KEY,
                title="Structural health: GIN-index overhead on task table",
                detail=(
                    f"GIN index '{worst['index']}' on the task table has "
                    f"overhead_ratio={worst['overhead_ratio']:.2f}x "
                    f"(idx_size={worst['idx_size']} bytes, "
                    f"tbl_size={worst['tbl_size']} bytes), "
                    f"exceeding threshold {overhead_ratio_threshold:.2f}x. "
                    "Oversized GIN indexes indicate pending-list buildup. "
                    "Run VACUUM on the task table or tune autovacuum settings."
                ),
            )
            if item is not None:
                enrolled += 1
                enrolled_projects.append(pid)
        if enrolled:
            await session.flush()

    decision = {
        "check": "gin_overhead",
        "mode": mode,
        "overhead_ratio_threshold": overhead_ratio_threshold,
        "gin_indexes_checked": len(rows),
        "offenders": len(offenders),
        "worst_ratio": offenders[0]["overhead_ratio"] if offenders else 0.0,
        "enrolled": enrolled,
        "enrolled_projects": enrolled_projects,
    }
    _OBSERVABILITY.record_axis(decision)
    return decision


# --- Connection-pool saturation check (no DB query) --------------------------


def _read_pool_stats(session) -> Dict[str, Any] | None:
    """Read SQLAlchemy pool statistics synchronously from the engine.

    Returns a dict with ``size``, ``checkedout``, ``overflow``, and
    ``utilization`` keys, or ``None`` when the pool implementation does not
    expose these counters (NullPool / StaticPool used in tests).
    """
    try:
        pool = resolve_async_engine(session).sync_engine.pool
        size = pool.size()
        checkedout = pool.checkedout()
        overflow = pool.overflow()
        capacity = size + max(0, overflow)
        utilization = checkedout / max(1, capacity)
        return {
            "size": size,
            "checkedout": checkedout,
            "overflow": overflow,
            "capacity": capacity,
            "utilization": round(utilization, 4),
        }
    except AttributeError:
        return None


async def run_pool_saturation_check(
    session,
    *,
    mode: str,
    saturation_threshold: float,
) -> Dict[str, Any]:
    """Check whether the connection pool is at or near saturation.

    Reads pool stats synchronously from the SQLAlchemy engine — no new DB
    connection is opened. Degrades gracefully (skips) on pool implementations
    that don't expose ``size()`` / ``checkedout()`` / ``overflow()`` (e.g.
    NullPool / StaticPool used in tests). Enrolls one deduplicated finding per
    project in ``enforce`` mode when utilization >= ``saturation_threshold``.
    """
    stats = _read_pool_stats(session)
    if stats is None:
        decision: Dict[str, Any] = {
            "check": "pool_saturation",
            "mode": mode,
            "skipped": True,
            "reason": "pool does not expose size/checkedout/overflow stats",
            "enrolled": 0,
        }
        _OBSERVABILITY.record_axis(decision)
        return decision

    tripped = stats["utilization"] >= saturation_threshold

    enrolled = 0
    enrolled_projects: List[str] = []
    if tripped and mode == "enforce":
        project_ids = await _get_distinct_project_ids(session)
        for pid in project_ids:
            item = await enroll_structural_finding(
                session,
                project_id=pid,
                finding_key=POOL_SATURATION_FINDING_KEY,
                title="Structural health: DB connection-pool near saturation",
                detail=(
                    f"Connection pool utilization={stats['utilization']:.1%} "
                    f"(checkedout={stats['checkedout']}, capacity={stats['capacity']}, "
                    f"size={stats['size']}, overflow={stats['overflow']}) "
                    f"exceeds threshold {saturation_threshold:.1%}. "
                    "Near-saturated pools cause request queuing and timeouts under "
                    "load spikes. Investigate pool_size / max_overflow settings and "
                    "whether long-running transactions are holding connections."
                ),
            )
            if item is not None:
                enrolled += 1
                enrolled_projects.append(pid)
        if enrolled:
            await session.flush()

    decision = {
        "check": "pool_saturation",
        "mode": mode,
        "saturation_threshold": saturation_threshold,
        **stats,
        "tripped": tripped,
        "enrolled": enrolled,
        "enrolled_projects": enrolled_projects,
    }
    _OBSERVABILITY.record_axis(decision)
    return decision


# --- Audit (pure-ish; takes resolved inputs) ---------------------------------


async def run_fat_row_check(
    session,
    *,
    mode: str,
    threshold_bytes: int,
    top_n: int = TOP_N_LARGEST_ROWS,
) -> Dict[str, Any]:
    """Measure the largest ``task.context`` rows and (in enforce) enroll findings.

    Always measures and records observability (the ``observe`` value of the
    audit). Enrolls a deduped finding per offending project ONLY in ``enforce``.
    Returns a closed-shape decision dict for the observability singleton and the
    sweep return value.
    """
    measurements = await measure_largest_context_rows(session, limit=top_n)
    offenders = [m for m in measurements if m.bytes >= threshold_bytes]

    enrolled = 0
    enrolled_projects: List[str] = []
    if mode == "enforce":
        seen_projects: set[str] = set()
        for off in offenders:
            if off.project_id in seen_projects:
                continue
            seen_projects.add(off.project_id)
            item = await enroll_structural_finding(
                session,
                project_id=off.project_id,
                finding_key=FAT_ROW_FINDING_KEY,
                title="Structural health: oversized task.context fat row",
                detail=_fat_row_finding_detail(off, threshold_bytes),
            )
            if item is not None:
                enrolled += 1
                enrolled_projects.append(off.project_id)
        if enrolled:
            await session.flush()

    decision = {
        "check": "fat_row",
        "mode": mode,
        "threshold_bytes": threshold_bytes,
        "measured": len(measurements),
        "offenders": len(offenders),
        "largest_bytes": measurements[0].bytes if measurements else 0,
        "enrolled": enrolled,
        "enrolled_projects": enrolled_projects,
    }
    _OBSERVABILITY.record(decision=decision, measurements=measurements)
    return decision


# --- Leader-only recurring sweep (called from the reaper cycle) --------------

# Module-level cadence gate, exactly like reaper_sweeps._last_converge_duplicate_scan.
_last_audit_scan: datetime | None = None


def _resolve_audit_config() -> Dict[str, Any]:
    """Resolve the runtime-config-driven audit knobs (mode / interval / thresholds)."""
    from app.shared_state import runtime_config as _runtime_config

    mode = str(
        _runtime_config.get("structural_health_audit_mode", DEFAULT_AUDIT_MODE)
    ).lower()
    if mode not in AUDIT_MODES:
        mode = DEFAULT_AUDIT_MODE
    interval = int(
        _runtime_config.get(
            "structural_health_audit_interval_minutes", DEFAULT_INTERVAL_MINUTES
        )
    )
    threshold = int(
        _runtime_config.get(
            "structural_health_audit_fat_row_bytes", DEFAULT_FAT_ROW_BYTES
        )
    )
    jsonb_bloat_dead_ratio = float(
        _runtime_config.get(
            "structural_health_audit_jsonb_bloat_dead_ratio",
            DEFAULT_JSONB_BLOAT_DEAD_RATIO,
        )
    )
    gin_overhead_ratio = float(
        _runtime_config.get(
            "structural_health_audit_gin_overhead_ratio",
            DEFAULT_GIN_OVERHEAD_RATIO,
        )
    )
    pool_saturation_pct = float(
        _runtime_config.get(
            "structural_health_audit_pool_saturation_pct",
            DEFAULT_POOL_SATURATION_PCT,
        )
    )
    return {
        "mode": mode,
        "interval_minutes": interval,
        "threshold_bytes": threshold,
        "jsonb_bloat_dead_ratio": jsonb_bloat_dead_ratio,
        "gin_overhead_ratio": gin_overhead_ratio,
        "pool_saturation_pct": pool_saturation_pct,
    }


async def run_structural_health_audit(
    session, now: datetime | None = None
) -> Dict[str, Any] | None:
    """Leader-only recurring structural-health sweep (mode + cadence gated).

    Called from the reaper cycle (already leader-only) inside its
    try/except+rollback idiom, so it can never crash the cycle. Returns the
    decision dict when it ran, or ``None`` when gated out (mode off, or the
    cadence interval has not elapsed).

    Runs all four checks (fat_row, jsonb_bloat, gin_overhead, pool_saturation)
    each cycle and commits once if any enrolled findings.
    """
    global _last_audit_scan
    cfg = _resolve_audit_config()
    mode = cfg["mode"]
    if mode == "off":
        return None

    _now = now or _utcnow()
    if _last_audit_scan is not None and (_now - _last_audit_scan) < timedelta(
        minutes=cfg["interval_minutes"]
    ):
        return None
    _last_audit_scan = _now

    fat_row = await run_fat_row_check(
        session, mode=mode, threshold_bytes=cfg["threshold_bytes"]
    )
    jsonb_bloat = await run_jsonb_bloat_check(
        session,
        mode=mode,
        dead_ratio_threshold=cfg["jsonb_bloat_dead_ratio"],
    )
    gin_overhead = await run_gin_overhead_check(
        session,
        mode=mode,
        overhead_ratio_threshold=cfg["gin_overhead_ratio"],
    )
    pool_sat = await run_pool_saturation_check(
        session,
        mode=mode,
        saturation_threshold=cfg["pool_saturation_pct"],
    )

    total_enrolled = (
        fat_row["enrolled"]
        + jsonb_bloat["enrolled"]
        + gin_overhead["enrolled"]
        + pool_sat["enrolled"]
    )
    if total_enrolled:
        await session.commit()
        logger.info(
            "[STRUCTURAL] audit enrolled %d finding(s) total "
            "(fat_row=%d, jsonb_bloat=%d, gin_overhead=%d, pool_sat=%d)",
            total_enrolled,
            fat_row["enrolled"],
            jsonb_bloat["enrolled"],
            gin_overhead["enrolled"],
            pool_sat["enrolled"],
        )
    elif fat_row["offenders"]:
        logger.info(
            "[STRUCTURAL] fat-row audit (mode=%s) saw %d offender(s) "
            "(largest=%d bytes, threshold=%d) — enrolled nothing",
            mode,
            fat_row["offenders"],
            fat_row["largest_bytes"],
            fat_row["threshold_bytes"],
        )

    result = {
        "fat_row": fat_row,
        "jsonb_bloat": jsonb_bloat,
        "gin_overhead": gin_overhead,
        "pool_saturation": pool_sat,
        "total_enrolled": total_enrolled,
    }

    tripped = bool(
        fat_row.get("offenders", 0)
        or jsonb_bloat.get("tripped")
        or gin_overhead.get("offenders", 0)
        or pool_sat.get("tripped")
    )
    payload = {
        "fat_row_offenders": fat_row.get("offenders", 0),
        "jsonb_bloat_tripped": bool(jsonb_bloat.get("tripped")),
        "gin_overhead_offenders": gin_overhead.get("offenders", 0),
        "pool_saturation_tripped": bool(pool_sat.get("tripped")),
        "total_enrolled": total_enrolled,
    }
    await log_audit_cycle_to_db(
        session,
        ts=_now,
        mode=mode,
        tripped=tripped,
        enrolled=total_enrolled,
        payload=payload,
    )

    return result
