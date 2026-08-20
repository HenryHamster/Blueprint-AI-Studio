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

from sqlalchemy import Integer, and_, case, func, literal_column, or_, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import resolve_dialect_name
from app.models import Task, TaskStatus
from app.time_utils import _utcnow


# A small amount of unattributed spend should not erase an otherwise useful
# arm-level cost signal.  Below this coverage, however, an average would be
# misleading because too much of the arm's spend is unknown.
TRIAL_ARM_COST_MIN_PRICED_COVERAGE = 0.90
TRIAL_ARM_COST_UNPRICED_MODEL_PAIR_LIMIT = 10


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
    (with-arm rate - without-arm rate), where rates are ``None`` for an empty arm.
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
        # Delivered = assignment key set AND receipt key is truthy (treatment actually
        # ran) — a present-but-false receipt (e.g. a kill-switch-off task) must not
        # count as delivered, or the futility engagement guard cannot see non-delivery.
        _receipt_expr_for_delivery = func.json_extract(Task.context, f"$.{context_key}")
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
        # Delivered = assignment key set AND receipt key is truthy (treatment actually
        # ran) — a present-but-false receipt (e.g. a kill-switch-off task) must not
        # count as delivered, or the futility engagement guard cannot see non-delivery.
        _receipt_expr_for_delivery = Task.context[context_key].astext
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
                _receipt_truthy_flag_expr,
                func.count(),
            )
            .where(*filters)
            .group_by(
                arm_expr,
                succ_expr,
                _assign_flag_expr,
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
                    # Delivery is receipt TRUTHINESS, not mere presence: a kill-switch-off
                    # task writes a present-but-false receipt (had_diagnostics_after_edit=false),
                    # which must not count as delivered or the futility engagement guard
                    # (futility_min_delivery_rate) can never see non-delivery.
                    if bool(receipt_truthy_val):
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

    # Lift is computed from the UNROUNDED arm rates so a verdict decision never
    # depends on quantization error (rounding each rate to 2dp before subtracting
    # can shift the difference by up to 0.01 either way). Rounding is applied only
    # to the persisted/display with_rate and without_rate fields below.
    _with_rate_raw = with_succ / with_n if with_n else None
    _without_rate_raw = without_succ / without_n if without_n else None
    with_rate = round(_with_rate_raw, 2) if _with_rate_raw is not None else None
    without_rate = round(_without_rate_raw, 2) if _without_rate_raw is not None else None
    lift = (
        _with_rate_raw - _without_rate_raw
        if _with_rate_raw is not None and _without_rate_raw is not None
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


async def compute_trial_arm_cost_delta(
    session: AsyncSession,
    *,
    project_id: "uuid.UUID | None",
    context_key: str,
    assignment_key: "str | None" = None,
) -> Dict[str, Any]:
    """Per-arm average LLM cost delta (with - without) over the trial's whole life.

    Groups terminal current-attempt tokens and banked retry attempt-history
    tokens by (arm, provider, model), applies cache-aware LLM pricing, and
    returns per-arm terminal-task averages and their delta.

    Returns ``{"with_avg_cost_usd": float|None, "without_avg_cost_usd": float|None,
    "cost_delta": float|None}`` where ``cost_delta = with_avg - without_avg``
    (negative means the WITH arm is cheaper). Averages are calculated over
    fully priced tasks and are ``None`` when priced-task coverage is below
    ``TRIAL_ARM_COST_MIN_PRICED_COVERAGE`` or an arm has no priced tasks.
    """
    from app.llm_cost_attribution import (  # avoid circular at module load
        LLM_TOKEN_PRICING,
        _price_llm_attempt,
    )

    _assign_key = assignment_key if assignment_key else context_key
    _has_assignment = _assign_key != context_key

    dialect_name = resolve_dialect_name(session)

    def _nonnegative_int(expr):
        casted = func.coalesce(func.cast(expr, Integer), 0)
        return case((casted > 0, casted), else_=0)

    def _bounded_cache_exprs(input_expr, cache_creation_expr, cache_read_expr):
        bounded_read = case(
            (cache_read_expr > input_expr, input_expr),
            else_=cache_read_expr,
        )
        bounded_creation = case(
            (
                cache_creation_expr + bounded_read > input_expr,
                input_expr - bounded_read,
            ),
            else_=cache_creation_expr,
        )
        return bounded_creation, bounded_read

    if dialect_name == "sqlite":
        _primary_expr = func.json_extract(Task.context, f"$.{_assign_key}")
        _fallback_expr = func.json_extract(Task.context, f"$.{context_key}")
        arm_expr = (
            func.coalesce(_primary_expr, _fallback_expr)
            if _has_assignment
            else _primary_expr
        )
        current_input_expr = _nonnegative_int(
            func.json_extract(Task.api_call_summary, "$.total_input_tokens")
        )
        current_output_expr = _nonnegative_int(
            func.json_extract(Task.api_call_summary, "$.total_output_tokens")
        )
        current_cache_creation_expr = _nonnegative_int(
            func.json_extract(
                Task.api_call_summary,
                "$.total_cache_creation_input_tokens",
            )
        )
        current_cache_read_expr = _nonnegative_int(
            func.json_extract(
                Task.api_call_summary,
                "$.total_cache_read_input_tokens",
            )
        )
        history_len_expr = func.coalesce(
            func.json_array_length(Task.attempt_history_db), 0
        )
        banked_attempt = (
            func.json_each(Task.attempt_history_db)
            .table_valued("key", "value")
            .alias("banked_attempt")
        )
        banked_value = banked_attempt.c.value
        banked_provider_raw = func.nullif(
            func.json_extract(banked_value, "$.provider"), ""
        )
        banked_model_raw = func.nullif(func.json_extract(banked_value, "$.model"), "")
        banked_input_expr = _nonnegative_int(
            func.json_extract(banked_value, "$.llm_input_tokens")
        )
        banked_output_expr = _nonnegative_int(
            func.json_extract(banked_value, "$.llm_output_tokens")
        )
        banked_cache_creation_expr = _nonnegative_int(
            func.json_extract(banked_value, "$.llm_cache_creation_input_tokens")
        )
        banked_cache_read_expr = _nonnegative_int(
            func.json_extract(banked_value, "$.llm_cache_read_input_tokens")
        )
    else:
        _primary_expr = Task.context[_assign_key].astext
        _fallback_expr = Task.context[context_key].astext
        arm_expr = (
            func.coalesce(_primary_expr, _fallback_expr)
            if _has_assignment
            else _primary_expr
        )
        current_input_expr = _nonnegative_int(
            Task.api_call_summary["total_input_tokens"].astext
        )
        current_output_expr = _nonnegative_int(
            Task.api_call_summary["total_output_tokens"].astext
        )
        current_cache_creation_expr = _nonnegative_int(
            Task.api_call_summary["total_cache_creation_input_tokens"].astext
        )
        current_cache_read_expr = _nonnegative_int(
            Task.api_call_summary["total_cache_read_input_tokens"].astext
        )
        history_array_expr = case(
            (
                func.jsonb_typeof(Task.attempt_history_db) == "array",
                Task.attempt_history_db,
            ),
            else_=literal_column("'[]'::jsonb"),
        )
        history_len_expr = func.coalesce(func.jsonb_array_length(history_array_expr), 0)
        banked_attempt = (
            func.jsonb_array_elements(history_array_expr)
            .table_valued("value")
            .lateral("banked_attempt")
        )
        banked_value = banked_attempt.c.value
        banked_provider_raw = func.nullif(banked_value.op("->>")("provider"), "")
        banked_model_raw = func.nullif(banked_value.op("->>")("model"), "")
        banked_input_expr = _nonnegative_int(
            banked_value.op("->>")("llm_input_tokens")
        )
        banked_output_expr = _nonnegative_int(
            banked_value.op("->>")("llm_output_tokens")
        )
        banked_cache_creation_expr = _nonnegative_int(
            banked_value.op("->>")("llm_cache_creation_input_tokens")
        )
        banked_cache_read_expr = _nonnegative_int(
            banked_value.op("->>")("llm_cache_read_input_tokens")
        )

    has_banked_history_expr = history_len_expr > 0
    current_input_for_pricing_expr = current_input_expr + case(
        (has_banked_history_expr, 0),
        else_=_nonnegative_int(Task.llm_input_tokens_banked_db),
    )
    current_output_for_pricing_expr = current_output_expr + case(
        (has_banked_history_expr, 0),
        else_=_nonnegative_int(Task.llm_output_tokens_banked_db),
    )

    current_cache_creation_expr, current_cache_read_expr = _bounded_cache_exprs(
        current_input_for_pricing_expr,
        current_cache_creation_expr,
        current_cache_read_expr,
    )
    current_token_bearing_expr = case(
        (
            or_(
                current_input_for_pricing_expr > 0,
                current_output_for_pricing_expr > 0,
            ),
            1,
        ),
        else_=0,
    )

    banked_cache_creation_expr, banked_cache_read_expr = _bounded_cache_exprs(
        banked_input_expr,
        banked_cache_creation_expr,
        banked_cache_read_expr,
    )
    banked_token_bearing_expr = case(
        (or_(banked_input_expr > 0, banked_output_expr > 0), 1),
        else_=0,
    )
    banked_provider_expr = case(
        (
            and_(banked_provider_raw.is_not(None), banked_model_raw.is_not(None)),
            banked_provider_raw,
        ),
        (
            and_(banked_provider_raw.is_(None), banked_model_raw.is_(None)),
            Task.provider,
        ),
        else_=None,
    )
    banked_model_expr = case(
        (
            and_(banked_provider_raw.is_not(None), banked_model_raw.is_not(None)),
            banked_model_raw,
        ),
        (
            and_(banked_provider_raw.is_(None), banked_model_raw.is_(None)),
            Task.model,
        ),
        else_=None,
    )

    filters = [
        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        arm_expr.is_not(None),
    ]
    if project_id is not None:
        filters.append(Task.project_id == project_id)

    task_count_stmt = select(arm_expr, func.count()).where(*filters).group_by(arm_expr)
    task_count_rows = (await session.execute(task_count_stmt)).all()

    with_task_count = without_task_count = 0
    for arm_val, cnt in task_count_rows:
        if _json_truthy(arm_val):
            with_task_count += int(cnt)
        else:
            without_task_count += int(cnt)

    current_stmt = (
        select(
            arm_expr,
            Task.id,
            Task.provider,
            Task.model,
            func.sum(current_input_for_pricing_expr),
            func.sum(current_output_for_pricing_expr),
            func.sum(current_cache_creation_expr),
            func.sum(current_cache_read_expr),
            func.sum(current_token_bearing_expr),
        )
        .where(*filters)
        .group_by(arm_expr, Task.id, Task.provider, Task.model)
    )
    current_rows = (await session.execute(current_stmt)).all()

    banked_stmt = (
        select(
            arm_expr,
            Task.id,
            banked_provider_expr,
            banked_model_expr,
            func.sum(banked_input_expr),
            func.sum(banked_output_expr),
            func.sum(banked_cache_creation_expr),
            func.sum(banked_cache_read_expr),
            func.sum(banked_token_bearing_expr),
        )
        .select_from(Task)
        .join(banked_attempt, true())
        .where(*filters, has_banked_history_expr)
        .group_by(arm_expr, Task.id, banked_provider_expr, banked_model_expr)
    )
    banked_rows = (await session.execute(banked_stmt)).all()

    current_unpriced_stmt = (
        select(
            arm_expr,
            Task.id,
            Task.provider,
            Task.model,
            current_token_bearing_expr,
        )
        .where(*filters, current_token_bearing_expr > 0)
    )
    current_unpriced_rows = (await session.execute(current_unpriced_stmt)).all()

    banked_unpriced_stmt = (
        select(
            arm_expr,
            Task.id,
            banked_provider_expr,
            banked_model_expr,
            banked_token_bearing_expr,
        )
        .select_from(Task)
        .join(banked_attempt, true())
        .where(*filters, has_banked_history_expr, banked_token_bearing_expr > 0)
    )
    banked_unpriced_rows = (await session.execute(banked_unpriced_stmt)).all()

    with_cost = without_cost = 0.0
    with_priced_token_rows = without_priced_token_rows = 0
    with_unpriced_task_ids: set[str] = set()
    without_unpriced_task_ids: set[str] = set()
    unpriced_model_pairs: set[tuple[str | None, str | None]] = set()
    with_task_costs: dict[str, float] = {}
    without_task_costs: dict[str, float] = {}

    def _has_pricing(provider: Any, model: Any) -> bool:
        if not provider or not model:
            return False
        return (str(provider).lower(), str(model).lower()) in LLM_TOKEN_PRICING

    def _mark_unpriced_task_rows(rows) -> None:
        for arm_val, task_id, provider, model, token_bearing_count in rows:
            if int(token_bearing_count or 0) <= 0 or _has_pricing(provider, model):
                continue
            unpriced_model_pairs.add(
                (str(provider).lower() if provider else None, str(model).lower() if model else None)
            )
            if _json_truthy(arm_val):
                with_unpriced_task_ids.add(str(task_id))
            else:
                without_unpriced_task_ids.add(str(task_id))

    def _accumulate_attempt_rows(rows) -> None:
        nonlocal with_cost, without_cost
        nonlocal with_priced_token_rows, without_priced_token_rows

        for (
            arm_val,
            task_id,
            provider,
            model,
            in_tok,
            out_tok,
            cache_creation,
            cache_read,
            token_bearing_count,
        ) in rows:
            token_bearing_count = int(token_bearing_count or 0)
            if token_bearing_count <= 0:
                continue
            in_tok = int(in_tok or 0)
            out_tok = int(out_tok or 0)
            cache_creation = int(cache_creation or 0)
            cache_read = int(cache_read or 0)
            cost: float | None = None
            if provider and model:
                pricing = LLM_TOKEN_PRICING.get((provider.lower(), model.lower()))
                if pricing:
                    in_rate, out_rate = pricing
                    cost = _price_llm_attempt(
                        in_tok,
                        out_tok,
                        cache_creation,
                        cache_read,
                        in_rate,
                        out_rate,
                        provider=provider,
                        model=model,
                    )
            if _json_truthy(arm_val):
                if cost is not None:
                    with_task_costs[str(task_id)] = (
                        with_task_costs.get(str(task_id), 0.0) + cost
                    )
                    with_priced_token_rows += token_bearing_count
            else:
                if cost is not None:
                    without_task_costs[str(task_id)] = (
                        without_task_costs.get(str(task_id), 0.0) + cost
                    )
                    without_priced_token_rows += token_bearing_count

    _accumulate_attempt_rows(current_rows)
    _accumulate_attempt_rows(banked_rows)
    _mark_unpriced_task_rows(current_unpriced_rows)
    _mark_unpriced_task_rows(banked_unpriced_rows)

    with_priced_task_count = max(with_task_count - len(with_unpriced_task_ids), 0)
    without_priced_task_count = max(
        without_task_count - len(without_unpriced_task_ids), 0
    )
    with_priced_coverage_fraction = (
        with_priced_task_count / with_task_count if with_task_count else 0.0
    )
    without_priced_coverage_fraction = (
        without_priced_task_count / without_task_count if without_task_count else 0.0
    )
    # Exclude an entire task if any of its attempts are unpriced: including a
    # partial task cost would make the nominally "priced" average understate it.
    with_cost = sum(
        cost for task_id, cost in with_task_costs.items() if task_id not in with_unpriced_task_ids
    )
    without_cost = sum(
        cost
        for task_id, cost in without_task_costs.items()
        if task_id not in without_unpriced_task_ids
    )
    with_avg = (
        round(with_cost / with_priced_task_count, 6)
        if with_priced_task_count > 0
        and with_priced_token_rows > 0
        and with_priced_coverage_fraction >= TRIAL_ARM_COST_MIN_PRICED_COVERAGE
        else None
    )
    without_avg = (
        round(without_cost / without_priced_task_count, 6)
        if without_priced_task_count > 0
        and without_priced_token_rows > 0
        and without_priced_coverage_fraction >= TRIAL_ARM_COST_MIN_PRICED_COVERAGE
        else None
    )
    cost_delta = (
        round(with_avg - without_avg, 6)
        if with_avg is not None and without_avg is not None
        else None
    )
    return {
        "with_avg_cost_usd": with_avg,
        "without_avg_cost_usd": without_avg,
        "cost_delta": cost_delta,
        "with_unpriced_task_count": len(with_unpriced_task_ids),
        "without_unpriced_task_count": len(without_unpriced_task_ids),
        "with_priced_task_count": with_priced_task_count,
        "without_priced_task_count": without_priced_task_count,
        "with_priced_coverage_fraction": with_priced_coverage_fraction,
        "without_priced_coverage_fraction": without_priced_coverage_fraction,
        "unpriced_model_pairs": [
            {"provider": provider, "model": model}
            for provider, model in sorted(
                unpriced_model_pairs,
                key=lambda pair: (pair[0] or "", pair[1] or ""),
            )[:TRIAL_ARM_COST_UNPRICED_MODEL_PAIR_LIMIT]
        ],
    }
