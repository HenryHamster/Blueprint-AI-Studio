"""Trial-arm DB helpers extracted from ``app.digest_metrics``.

Capability-trial arm accounting: cumulative per-arm first-attempt-success
accumulation (used to DECIDE a trial) and recent per-arm assignment accrual
(used to PROJECT how long until the holdback arm reaches its threshold). Both
run dialect-aware SQL aggregates over a passed-in ``AsyncSession`` and never
materialize JSONB-heavy rows.

``app.digest_metrics`` re-exports these symbols so existing import paths (and
``mock.patch`` targets) are unchanged.
"""

import uuid  # referenced in the string type annotations below
from datetime import timedelta
from typing import Dict, Any

from sqlalchemy import and_, case, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import resolve_dialect_name
from app.models import Task, TaskStatus
from app.time_utils import _utcnow


def _json_truthy(value: Any) -> bool:
    """Normalize a JSON-extracted boolean across dialects.

    SQLite ``json_extract`` returns integer 1/0 for JSON true/false; PostgreSQL
    ``->>`` (``.astext``) returns the strings ``'true'``/``'false'``. ORM-loaded
    values may also arrive as native ``bool``. Treat all truthy encodings the same.
    """
    return value in (True, 1, "true", "1")


async def compute_trial_arm_accumulation(
    session: AsyncSession,
    *,
    project_id: "uuid.UUID | None",
    context_key: str,
    assignment_key: "str | None" = None,
) -> Dict[str, Any]:
    """Cumulative per-arm first-attempt-success counts over a trial's whole life.

    The capability-trial verdict must accumulate evidence over the life of the
    trial rather than re-deriving arms from a single sliding digest window — at
    realistic task volume the without-arm never reaches the min-N threshold inside
    any one window, so a windowed basis can never decide.

    Intent-to-treat partitioning: when ``assignment_key`` is provided (e.g.
    ``_repo_map_arm``), arm membership uses COALESCE(assignment_key, context_key),
    matching the windowed ``_compute_first_attempt_breakdowns`` path.  Tasks that
    carry the assignment key are partitioned by it; legacy tasks that pre-date the
    assignment field fall back to the receipt key (``context_key``).  This ensures
    a treatment-assigned task with an empty map (_repo_map_arm=true,
    _had_repo_map=false) lands in the WITH arm, not the WITHOUT arm.

    This runs a dialect-aware SQL AGGREGATE (``func.count()`` GROUP BY the arm-flag
    value and the first-attempt-success value, project-filtered, over terminal
    COMPLETED/FAILED tasks that carry at least one arm indicator and
    ``_first_attempt_succeeded``).  An aggregate count never materializes
    JSONB-heavy rows, so scanning the full trial history is bounded and safe — it
    does NOT widen the digest's own capped terminal-task scan.

    Returns a dict with cumulative ``with_n``, ``without_n``, ``with_succ``,
    ``without_succ``, ``with_rate``, ``without_rate`` and ``lift``
    (with-arm rate − without-arm rate), where rates are ``None`` for an empty arm.
    """
    _assign_key = assignment_key if assignment_key else context_key
    _has_assignment = _assign_key != context_key
    if resolve_dialect_name(session) == "sqlite":
        _primary_expr = func.json_extract(Task.context, f"$.{_assign_key}")
        _fallback_expr = func.json_extract(Task.context, f"$.{context_key}")
        arm_expr = (
            func.coalesce(_primary_expr, _fallback_expr)
            if _has_assignment
            else _primary_expr
        )
        succ_expr = func.json_extract(Task.context, "$._first_attempt_succeeded")
        _assign_flag_expr = (
            case((_primary_expr.is_not(None), 1), else_=0) if _has_assignment else None
        )
        # Delivered = assignment key set AND receipt key also set (treatment ran).
        _receipt_expr_for_delivery = func.json_extract(Task.context, f"$.{context_key}")
        _delivered_flag_expr = (
            case(
                (
                    and_(
                        _primary_expr.is_not(None),
                        _receipt_expr_for_delivery.is_not(None),
                    ),
                    1,
                ),
                else_=0,
            )
            if _has_assignment
            else None
        )
        # Receipt truthy: receipt key is explicitly true (SQLite json_extract returns 1 for JSON true).
        _receipt_truthy_flag_expr = (
            case((_receipt_expr_for_delivery == 1, 1), else_=0)
            if _has_assignment
            else None
        )
    else:
        _primary_expr = Task.context[_assign_key].astext
        _fallback_expr = Task.context[context_key].astext
        arm_expr = (
            func.coalesce(_primary_expr, _fallback_expr)
            if _has_assignment
            else _primary_expr
        )
        succ_expr = Task.context["_first_attempt_succeeded"].astext
        _assign_flag_expr = (
            case((_primary_expr.is_not(None), 1), else_=0) if _has_assignment else None
        )
        # Delivered = assignment key set AND receipt key also set (treatment ran).
        _receipt_expr_for_delivery = Task.context[context_key].astext
        _delivered_flag_expr = (
            case(
                (
                    and_(
                        _primary_expr.is_not(None),
                        _receipt_expr_for_delivery.is_not(None),
                    ),
                    1,
                ),
                else_=0,
            )
            if _has_assignment
            else None
        )
        # Receipt truthy: receipt key is explicitly "true" (PG astext returns string).
        _receipt_truthy_flag_expr = (
            case((_receipt_expr_for_delivery == "true", 1), else_=0)
            if _has_assignment
            else None
        )

    filters = [
        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        arm_expr.is_not(None),
        succ_expr.is_not(None),
    ]
    if project_id is not None:
        filters.append(Task.project_id == project_id)

    with_n = with_succ = without_n = without_succ = 0
    if _has_assignment:
        stmt = (
            select(
                arm_expr,
                succ_expr,
                _assign_flag_expr,
                _delivered_flag_expr,
                _receipt_truthy_flag_expr,
                func.count(),
            )
            .where(*filters)
            .group_by(
                arm_expr,
                succ_expr,
                _assign_flag_expr,
                _delivered_flag_expr,
                _receipt_truthy_flag_expr,
            )
        )
        rows = (await session.execute(stmt)).all()
        with_assigned = with_receipt = without_assigned = without_receipt = 0
        with_assigned_delivered = without_contaminated = 0
        for (
            arm_val,
            succ_val,
            is_assigned_val,
            is_delivered_val,
            receipt_truthy_val,
            cnt,
        ) in rows:
            cnt = int(cnt)
            is_assigned = bool(is_assigned_val)
            if _json_truthy(arm_val):
                with_n += cnt
                if _json_truthy(succ_val):
                    with_succ += cnt
                if is_assigned:
                    with_assigned += cnt
                    if bool(is_delivered_val):
                        with_assigned_delivered += cnt
                else:
                    with_receipt += cnt
            else:
                without_n += cnt
                if _json_truthy(succ_val):
                    without_succ += cnt
                if is_assigned:
                    without_assigned += cnt
                    if bool(receipt_truthy_val):
                        without_contaminated += cnt
                else:
                    without_receipt += cnt
    else:
        stmt = (
            select(arm_expr, succ_expr, func.count())
            .where(*filters)
            .group_by(arm_expr, succ_expr)
        )
        rows = (await session.execute(stmt)).all()
        for arm_val, succ_val, cnt in rows:
            cnt = int(cnt)
            if _json_truthy(arm_val):
                with_n += cnt
                if _json_truthy(succ_val):
                    with_succ += cnt
            else:
                without_n += cnt
                if _json_truthy(succ_val):
                    without_succ += cnt

    with_rate = round(with_succ / with_n, 2) if with_n else None
    without_rate = round(without_succ / without_n, 2) if without_n else None
    lift = (
        round(with_rate - without_rate, 2)
        if with_rate is not None and without_rate is not None
        else None
    )
    result: Dict[str, Any] = {
        "with_n": with_n,
        "without_n": without_n,
        "with_succ": with_succ,
        "without_succ": without_succ,
        "with_rate": with_rate,
        "without_rate": without_rate,
        "lift": lift,
    }
    if _has_assignment:
        result["arm_composition"] = {
            "with_assigned": with_assigned,
            "with_assigned_delivered": with_assigned_delivered,
            "with_receipt": with_receipt,
            "without_assigned": without_assigned,
            "without_receipt": without_receipt,
            "without_contaminated": without_contaminated,
        }
    return result


async def compute_trial_arm_accrual(
    session: AsyncSession,
    *,
    project_id: "uuid.UUID | None",
    assignment_key: str,
    window_days: int = 7,
) -> Dict[str, Any]:
    """Recent per-arm assignment volume over the last ``window_days`` days.

    Unlike :func:`compute_trial_arm_accumulation` — which counts cumulatively over
    the whole life of the trial to DECIDE — this measures the RECENT accrual RATE so
    the operator can project how long until the holdback (without-)arm reaches its
    decision threshold and what adjusting the holdback fraction would buy.

    Only assignment-based trials have a meaningful accrual rate, so this takes the
    ``assignment_key`` directly (no receipt fallback): a task counts toward the WITH
    arm when the assignment flag is truthy and the WITHOUT arm when it is falsy.
    Tasks that pre-date the assignment field (NULL flag) are excluded — they carry no
    randomized arm and so contribute nothing to the forward-looking rate.

    Runs a dialect-aware aggregate ``func.count()`` GROUP BY the arm-flag value over
    terminal COMPLETED/FAILED tasks created within the window, project-filtered. An
    aggregate count never materializes JSONB-heavy rows, so it does not widen the
    digest's own capped terminal-task scan.

    Returns ``{"with_7d": int, "without_7d": int, "window_days": window_days}`` (the
    ``with_7d``/``without_7d`` keys keep the default-7-day naming for callers/UI even
    when ``window_days`` differs).
    """
    if resolve_dialect_name(session) == "sqlite":
        arm_expr = func.json_extract(Task.context, f"$.{assignment_key}")
    else:
        arm_expr = Task.context[assignment_key].astext

    window_start = _utcnow() - timedelta(days=window_days)
    filters = [
        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        arm_expr.is_not(None),
        Task.created_at >= window_start,
    ]
    if project_id is not None:
        filters.append(Task.project_id == project_id)

    stmt = select(arm_expr, func.count()).where(*filters).group_by(arm_expr)
    rows = (await session.execute(stmt)).all()

    with_7d = without_7d = 0
    for arm_val, cnt in rows:
        cnt = int(cnt)
        if _json_truthy(arm_val):
            with_7d += cnt
        else:
            without_7d += cnt

    return {"with_7d": with_7d, "without_7d": without_7d, "window_days": window_days}
