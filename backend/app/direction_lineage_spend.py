"""Canonical direction lineage-spend meter.

A direction carries exactly ONE operator-declarable cost envelope
(``cost_budget_usd``). Before this module that envelope was measured by two
disjoint meters that disagreed and neither of which counted LLM tokens: the
digest badge summed live-span VM seconds over the ROLLING digest window, while
the auto-pause gate summed research-cycle estimates. This module is the single
summer both surfaces read, so "spend" means the same dollars everywhere.

Three terms make up lineage spend:

* **VM** — via :func:`app.models.get_task_cost_by_provider_model` so BANKED
  retry attempts count, not just the live span. For lifetime queries any
  residual between that per-pair breakdown and
  :func:`app.models.get_task_total_vm_cost_usd` (legacy rows that banked cost on
  the column without an ``attempt_history`` row) is folded into
  :data:`UNATTRIBUTED_BUCKET` rather than silently dropped.
* **LLM** — via :func:`app.llm_cost_attribution.task_llm_cost_by_provider_model`,
  the same attempt enumeration and pricing path as ``_task_llm_cost``.
* **Compute** — terminal :class:`app.models.ComputeJob` ``final_cost_usd`` for
  the direction's tasks, mirroring ``app.direction_outcomes``. Compute-experiment
  tasks run off-VM, so without this term compute-heavy directions report a
  near-zero spend against a real bill.

Invariant enforced by construction: ``sum(by_provider_model.values()) ==
total_usd`` (to float tolerance). Every dollar added to a total is added to
exactly one bucket.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Sequence

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.llm_cost_attribution import task_llm_cost_by_provider_model
from app.models import (
    ComputeJob,
    ComputeJobStatus,
    Task,
    get_task_cost_by_provider_model,
    get_task_total_vm_cost_usd,
)

logger = logging.getLogger(__name__)

#: Bucket for compute-substrate spend (ComputeJob ledger, no provider/model).
COMPUTE_SUBSTRATE_BUCKET = "compute|substrate"
#: Bucket for VM dollars whose (provider, model) pair could not be resolved.
UNATTRIBUTED_BUCKET = "unknown|unknown"

#: Provenance strings. Downstream consumers surface these verbatim so an
#: operator can always tell WHICH dollars a percentage was computed from.
BASIS_LIFETIME = "lifetime_lineage:vm_banked+llm_tokens+compute_jobs"
BASIS_WINDOW = "window_lineage:vm_banked+llm_tokens"

# Residual smaller than this is float noise, not a dropped dollar.
_RESIDUAL_EPSILON = 1e-9

# Lifetime queries scan every task ever spawned under a direction. Paginate by
# primary key so memory stays bounded no matter how long the lineage is; the
# accumulator folds each page and the page itself is then dropped.
_TASK_PAGE_SIZE = 2000

# Declared ceiling on total lineage rows hydrated per call. Pagination bounds
# PEAK memory but not TOTAL work, and this meter runs on every digest request,
# so an ancient project would otherwise pay a full-lineage scan each time.
# Hitting the ceiling sets ``truncated`` and downgrades the basis string — a
# truncated total is a LOWER BOUND and must never be presented as exact.
_LINEAGE_TASK_ROW_CAP = 20000

# Exactly the columns accumulate_task_spend reads. This meter shares the request
# with the digest's own deliberately-projected task queries; selecting whole Task
# rows would hydrate payloads/status_log/result blobs for the entire lineage AND
# populate columns the digest defers on purpose (notably ``vm_machine_type``,
# which get_task_current_attempt_vm_cost_usd probes via ``__dict__.get``).
def _lineage_load_options():
    return load_only(
        Task.id,
        Task.project_id,
        Task.context,
        Task.provider,
        Task.model,
        Task.vm_provisioned_at,
        Task.vm_stopped_at,
        Task.vm_spot,
        Task.vm_runtime_cost_banked_db,
        Task.attempt_history_db,
        Task.api_call_summary,
        Task.provider_reported_usage,
        Task.llm_input_tokens_banked_db,
        Task.llm_output_tokens_banked_db,
        Task.llm_call_count_banked_db,
        raiseload=True,
    )

# A ComputeJob only has a final bill once it reaches a terminal state. Mirrors
# the set used by app.direction_outcomes so both paths bill the same jobs.
_COMPUTE_TERMINAL_STATUSES = (
    ComputeJobStatus.SUCCEEDED,
    ComputeJobStatus.FAILED,
    ComputeJobStatus.CANCELLED,
    ComputeJobStatus.TIMED_OUT,
)


@dataclass
class DirectionLineageSpend:
    """Total lineage spend for one direction, plus the terms that produced it."""

    total_usd: float = 0.0
    vm_usd: float = 0.0
    llm_usd: float = 0.0
    compute_usd: float = 0.0
    task_count: int = 0
    #: True when at least one attempt carried tokens we could not price, so the
    #: total is a LOWER BOUND. Callers should say so rather than imply exactness.
    llm_unpriced: bool = False
    #: True when the lifetime scan hit :data:`_LINEAGE_TASK_ROW_CAP`, so some of
    #: the lineage was not summed and the total is a LOWER BOUND.
    truncated: bool = False
    by_provider_model: Dict[str, float] = field(default_factory=dict)
    basis: str = BASIS_LIFETIME

    def add_bucket(self, key: str, amount: float) -> None:
        if amount:
            self.by_provider_model[key] = self.by_provider_model.get(key, 0.0) + amount

    def as_dict(self, *, digits: int = 4) -> Dict[str, Any]:
        """Rounded, JSON-safe view for API payloads."""
        return {
            "total_usd": round(self.total_usd, digits),
            "vm_usd": round(self.vm_usd, digits),
            "llm_usd": round(self.llm_usd, digits),
            "compute_usd": round(self.compute_usd, digits),
            "task_count": self.task_count,
            "llm_unpriced": self.llm_unpriced,
            "truncated": self.truncated,
            "by_provider_model": {
                k: round(v, digits) for k, v in self.by_provider_model.items()
            },
            "basis": self.basis,
        }


def accumulate_task_spend(
    acc: DirectionLineageSpend,
    task: Task,
    *,
    window_start: datetime | None = None,
) -> None:
    """Fold one task's VM + LLM spend into ``acc`` (in place).

    ``window_start`` filters BANKED attempts to those that stopped inside the
    window; the live span is always included (window callers pre-filter tasks by
    ``vm_stopped_at``). The banked-column residual reconciliation only applies to
    lifetime sums — a column total carries no timestamp, so it cannot honestly be
    attributed to a window.
    """
    vm_buckets = dict(get_task_cost_by_provider_model(task, window_start=window_start))
    vm_cost = sum(vm_buckets.values())
    if window_start is None:
        column_total = get_task_total_vm_cost_usd(task)
        residual = column_total - vm_cost
        if residual > _RESIDUAL_EPSILON:
            vm_buckets[UNATTRIBUTED_BUCKET] = (
                vm_buckets.get(UNATTRIBUTED_BUCKET, 0.0) + residual
            )
            vm_cost = column_total

    llm_buckets, any_unpriced = task_llm_cost_by_provider_model(task)
    llm_cost = sum(llm_buckets.values())

    acc.task_count += 1
    acc.vm_usd += vm_cost
    acc.llm_usd += llm_cost
    acc.total_usd += vm_cost + llm_cost
    if any_unpriced:
        acc.llm_unpriced = True
    for key, amount in vm_buckets.items():
        acc.add_bucket(key, amount)
    for key, amount in llm_buckets.items():
        acc.add_bucket(key, amount)


def accumulate_compute_job_spend(acc: DirectionLineageSpend, amount: float) -> None:
    """Fold terminal ComputeJob spend into ``acc`` (in place)."""
    amount = float(amount or 0.0)
    if amount <= 0:
        return
    acc.compute_usd += amount
    acc.total_usd += amount
    acc.add_bucket(COMPUTE_SUBSTRATE_BUCKET, amount)


def summarize_task_spend(
    tasks: Iterable[Task],
    *,
    window_start: datetime | None = None,
    basis: str | None = None,
) -> DirectionLineageSpend:
    """Pure, session-free spend summer over a list of ``Task`` rows.

    Returns the total plus its VM/LLM terms and a per-``provider|model``
    breakdown that sums exactly to ``total_usd``. Compute-job spend is not
    reachable without a session, so it stays ``0.0`` here; use
    :func:`load_direction_lineage_spend` for the full lifetime figure.
    """
    acc = DirectionLineageSpend(
        basis=basis or (BASIS_WINDOW if window_start is not None else BASIS_LIFETIME)
    )
    for task in tasks:
        accumulate_task_spend(acc, task, window_start=window_start)
    return acc


async def load_direction_lineage_spend(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    direction_ids: Sequence[str],
) -> Dict[str, DirectionLineageSpend]:
    """Lifetime lineage spend per direction id, over ALL tasks in the lineage.

    Every requested id gets an entry (a direction with no tasks reports a
    genuine zero rather than a missing key, so callers never have to guess
    whether absence means "no spend" or "not measured").

    Task selection uses the established ``Task.context["direction_id"]`` JSON
    predicate — ``task.context`` carries the ``ix_task_context_gin`` GIN index,
    so this stays index-served rather than a full scan.

    This runs on every ``/api/admin/digest`` request, so it is bounded on both
    axes: rows are projected to exactly the columns ``accumulate_task_spend``
    reads (``_lineage_load_options``, ``raiseload=True``) rather than hydrating
    whole ``Task`` rows, and the total row count is ceilinged at
    :data:`_LINEAGE_TASK_ROW_CAP`. Hitting the ceiling logs a warning and marks
    every returned result ``truncated`` with a ``+truncated@N`` basis suffix, so
    a lower-bound total can never be read as an exact one.
    """
    ids: List[str] = sorted({str(d) for d in direction_ids if d})
    if not ids:
        return {}

    results: Dict[str, DirectionLineageSpend] = {
        did: DirectionLineageSpend(basis=BASIS_LIFETIME) for did in ids
    }

    direction_expr = Task.context["direction_id"].as_string()
    last_id: uuid.UUID | None = None
    rows_seen = 0
    truncated = False
    while True:
        # On the final page fetch one row PAST the ceiling: its existence is what
        # proves the lineage was actually truncated, so a lineage of exactly
        # _LINEAGE_TASK_ROW_CAP tasks is reported as complete, not as a lower bound.
        remaining = _LINEAGE_TASK_ROW_CAP - rows_seen
        page_size = (
            _TASK_PAGE_SIZE if remaining > _TASK_PAGE_SIZE else remaining + 1
        )
        stmt = select(Task).where(
            Task.project_id == project_id,
            direction_expr.in_(ids),
        )
        if last_id is not None:
            stmt = stmt.where(Task.id > last_id)
        stmt = (
            stmt.order_by(Task.id)
            .limit(page_size)
            .options(_lineage_load_options())
        )
        batch = (await session.execute(stmt)).scalars().all()
        if not batch:
            break
        if len(batch) > remaining:
            truncated = True
            batch = batch[:remaining]
        for task in batch:
            did = str((task.context or {}).get("direction_id") or "")
            acc = results.get(did)
            if acc is None:
                continue
            accumulate_task_spend(acc, task, window_start=None)
        rows_seen += len(batch)
        if truncated:
            break
        last_id = batch[-1].id
        if len(batch) < page_size:
            break

    if truncated:
        logger.warning(
            "direction lineage spend truncated at %s rows (project=%s, directions=%s): "
            "reported totals are a lower bound",
            _LINEAGE_TASK_ROW_CAP,
            project_id,
            len(ids),
        )
        for acc in results.values():
            acc.truncated = True
            acc.basis = f"{acc.basis}+truncated@{_LINEAGE_TASK_ROW_CAP}"

    # Compute-substrate spend folded as one grouped aggregate (bounded work,
    # independent of lineage length). Without it a compute_experiment direction
    # reports ~$0 while its real bill sits on the ComputeJob ledger.
    cj_stmt = (
        select(direction_expr, func.sum(ComputeJob.final_cost_usd))
        .select_from(ComputeJob)
        .join(Task, ComputeJob.task_id == Task.id)
        .where(
            Task.project_id == project_id,
            direction_expr.in_(ids),
            ComputeJob.final_cost_usd.is_not(None),
            ComputeJob.status.in_(_COMPUTE_TERMINAL_STATUSES),
        )
        .group_by(direction_expr)
    )
    for did, compute_total in (await session.execute(cj_stmt)).all():
        acc = results.get(str(did or ""))
        if acc is not None and compute_total:
            accumulate_compute_job_spend(acc, float(compute_total))

    return results
