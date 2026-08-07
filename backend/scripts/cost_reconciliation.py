#!/usr/bin/env python3
"""Checked-in cost reconciliation: internal attribution vs a provider billing export.

Context (why this module exists): the operator pulled the real DeepSeek
platform billing export (message f1d05e9b, 2026-08-06). Our ledger reported
~$1.80 of deepseek spend across a seven-day window; the export shows ~$19.77
on Aug-5 alone. Nothing in the system compared internal cost attribution
against an authoritative external invoice, which is why a ~10x gap survived
six cycles of cost work unnoticed.

This module computes that comparison and REPORTS the residual. It does NOT
assert agreement: the residual IS the finding. A green check that hides a 10x
gap is the exact failure this module is built to correct.

What it does
  * For a given provider and date window, sums internally attributed cost
    from RECORDED PER-ATTEMPT usage (the same attempt rows
    ``app.llm_cost_attribution`` prices), never from
    ``Task.estimated_cost_usd`` (that field is the broken estimator the
    operator's export falsified).
  * Compares that total against an OPERATOR-SUPPLIED provider billing export
    for the same window (accepted as a checked-in fixture plus a documented
    path for supplying a fresh one).
  * Emits a structured report:
      - internal total, export total, absolute and fraction residual;
      - count of tasks with recorded usage vs. missing usage, plus an
        estimate of the residual attributable to those missing tasks;
      - a cache-read vs. uncached token breakout on BOTH sides, since
        unrecorded cache-read volume is the suspected dominant term.

A sibling task is making ``provider_reported_usage`` capture universal at the
LLM-call boundary. This module reports honestly BEFORE and AFTER that lands:
with today's data it shows a large residual and a high missing-usage count,
and that is the correct output, not a bug.

The export fixture
  ``deepseek_billing_export_sample.json`` (next to this script) is the checked-in
  sample of the expected DeepSeek platform billing export schema. It is a SAMPLE,
  not the real export. To supply a fresh one:

      python backend/scripts/cost_reconciliation.py \\
          --provider deepseek \\
          --export /path/to/fresh_deepseek_export.json \\
          [--start 2026-07-31] [--end 2026-08-06] [--write-json out.json]

  The export is a JSON document with:
      {
        "provider": "deepseek",              # provider the export covers
        "currency": "USD",
        "exported_at": "2026-08-06T00:00:00Z",
        "window": {"start": "2026-07-31", "end": "2026-08-06"},
        "records": [
          {
            "timestamp": "2026-08-05T12:00:00Z",
            "model": "deepseek-v4-flash",
            "input_tokens": 2000000,          # cache-miss (uncached) input
            "cache_read_input_tokens": 60000000,  # cache-hit input
            "output_tokens": 8000000,
            "cost_usd": 2.688
          },
          ...
        ]
      }
  No credential is hardcoded and no billing API is called from this module.

Pricing
  There is exactly ONE pricing source: ``app.llm_cost_attribution``. The
  internal side is priced with the SAME tables and prompt-cache multipliers the
  ledger uses (``LLM_TOKEN_PRICING`` / ``LLM_CACHE_READ_INPUT_PRICING`` and the
  ``CACHE_*_INPUT_RATE_MULTIPLIER`` constants), resolved lazily through
  ``_default_pricing_context()`` so the module stays stdlib-only at import time
  (the convention used by the sibling strategic_* scripts). ``main()``
  additionally passes those live tables in explicitly so production runs are
  priced exactly as the ledger prices them. There is deliberately NO module-level
  copy of the pricing tables: a hand-maintained mirror would drift from the
  ledger and let the CLI report stale prices while claiming otherwise.
  The export-side per-class cost estimates price the provider's token counts
  with the same rates; the export's own ``cost_usd`` is authoritative for the
  headline totals.

Convention: this module is stdlib-only at import time (no ``app.*`` import at
module level) so the reconciliation functions load and can be tested without a
database. Both the DB read (``_load_task_rows``) and the default pricing source
(``_default_pricing_context``) live behind lazy ``app.*`` imports resolved at
first use.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

def _default_pricing_context() -> dict[str, Any]:
    """Lazily load the ledger's canonical pricing tables and multipliers.

    There is deliberately NO module-level copy of the pricing tables: a
    hand-maintained mirror would drift from ``app.llm_cost_attribution`` and
    let the CLI report stale prices while claiming otherwise. The ``app.*``
    import is deferred so the module stays stdlib-only at import time (the
    convention used by the sibling ``strategic_*`` scripts); it is resolved on
    first pricing use, exactly like the lazy DB import in ``_load_task_rows``.
    """
    from app.llm_cost_attribution import (
        CACHE_CREATION_INPUT_RATE_MULTIPLIER,
        CACHE_READ_INPUT_RATE_MULTIPLIER,
        LLM_CACHE_READ_INPUT_PRICING,
        LLM_TOKEN_PRICING,
    )

    return {
        "token_pricing": LLM_TOKEN_PRICING,
        "cache_read_pricing": LLM_CACHE_READ_INPUT_PRICING,
        "cache_read_multiplier": CACHE_READ_INPUT_RATE_MULTIPLIER,
        "cache_creation_multiplier": CACHE_CREATION_INPUT_RATE_MULTIPLIER,
    }

# Checked-in sample of the DeepSeek platform billing export schema.
SAMPLE_EXPORT_PATH = Path(__file__).resolve().parent / "deepseek_billing_export_sample.json"

# Cost values are rounded to 6 decimals in the report, matching the rest of
# the cost-telemetry modules.
COST_ROUND = 6

# Internal total/token sub-key names that the aggregate helpers accumulate.
_INTERNAL_COST_KEYS = (
    "uncached_cost_usd",
    "cache_read_cost_usd",
    "cache_creation_cost_usd",
    "output_cost_usd",
)
_INTERNAL_TOKEN_KEYS = (
    "uncached_input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)


def _parse_date(value: Any) -> date | None:
    """Parse a YYYY-MM-DD string (or datetime) into a ``date``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # Accept a full ISO timestamp too, taking only its date part.
    return datetime.fromisoformat(text.replace("Z", "+00:00")).date()


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO timestamp (naive or timezone-aware) into a UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _day_start_naive_utc(value: Any, offset_days: int = 0) -> datetime | None:
    """Start-of-day naive UTC datetime for a window bound.

    ``offset_days`` shifts the day (used for the half-open end bound, which is
    the day after the inclusive end date). Returns ``None`` when ``value`` is
    missing/unparseable. Naive because ``Task.created_at`` is stored as naive
    UTC, and naive datetimes bind cleanly to the column in asyncpg queries.
    """
    parsed = _parse_date(value)
    if parsed is None:
        return None
    return datetime(
        parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc
    ).replace(tzinfo=None) + timedelta(days=offset_days)


def _bounded_cache_input_counts(
    input_tokens: int, cache_creation: int, cache_read: int
) -> tuple[int, int]:
    """Cache sub-counts that cannot exceed the inclusive input total."""
    total = max(0, int(input_tokens))
    cc = max(0, int(cache_creation))
    cr = max(0, int(cache_read))
    if cc + cr <= total:
        return cc, cr
    cr = min(cr, total)
    cc = min(cc, total - cr)
    return cc, cr


def _price_attempt(
    attempt: Mapping[str, Any],
    provider: str | None,
    model: str | None,
    pricing: Mapping[tuple[str, str], tuple[float, float]],
    cache_read_pricing: Mapping[tuple[str, str], float],
    cache_read_multiplier: float,
    cache_creation_multiplier: float,
) -> dict[str, float | int] | None:
    """Price one attempt's recorded tokens, charging cache classes truthfully.

    ``input_tokens`` is the INCLUSIVE input total (uncached + cache-creation +
    cache-read), mirroring ``app.llm_cost_attribution._price_llm_attempt``.
    The prompt-cache multipliers come from the caller, which resolves them from
    the canonical ``app.llm_cost_attribution`` source (there is deliberately no
    module-level copy to drift). Returns ``None`` when the (provider, model)
    combo has no pricing row (the tokens are still "recorded usage" for the
    with/missing-usage count, but contribute 0 cost -- the same behavior as the
    ledger's unpriced combos).
    """
    input_tokens = int(attempt.get("input_tokens", 0) or 0)
    output_tokens = int(attempt.get("output_tokens", 0) or 0)
    cc, cr = _bounded_cache_input_counts(
        input_tokens,
        int(attempt.get("cache_creation_input_tokens", 0) or 0),
        int(attempt.get("cache_read_input_tokens", 0) or 0),
    )
    provider_key = (provider or "").lower()
    model_key = (model or "").lower()
    rate = pricing.get((provider_key, model_key))
    if rate is None:
        return None
    input_rate, output_rate = rate
    if provider_key == "anthropic":
        cache_creation_rate = input_rate * cache_creation_multiplier
        cache_read_rate = input_rate * cache_read_multiplier
    else:
        cache_creation_rate = input_rate
        cache_read_rate = cache_read_pricing.get((provider_key, model_key), input_rate)
    uncached = max(0, input_tokens - cc - cr)
    return {
        "uncached_input_tokens": uncached,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cc,
        "output_tokens": output_tokens,
        "uncached_cost_usd": uncached * input_rate,
        "cache_read_cost_usd": cr * cache_read_rate,
        "cache_creation_cost_usd": cc * cache_creation_rate,
        "output_cost_usd": output_tokens * output_rate,
    }


def _aggregate_task_row(
    row: Mapping[str, Any],
    pricing: Mapping[tuple[str, str], tuple[float, float]],
    cache_read_pricing: Mapping[tuple[str, str], float],
    cache_read_multiplier: float,
    cache_creation_multiplier: float,
) -> tuple[dict[str, float | int], bool, int]:
    """Price a task row's recorded attempts; returns
    (totals, has_usage, task_model_fallback_count).

    Each attempt is priced with ITS OWN recorded provider/model (the per-attempt
    pair ``_task_to_row`` stores from ``_task_llm_attempts``), so a task whose
    attempts ran on different tiers prices each attempt at its own rate. The
    task-level provider/model is used ONLY when the per-attempt pair is
    genuinely absent, and every such fallback is counted so silent task-level
    pricing is observable. A half-filled attempt (provider without model, or
    vice versa) is deliberately unpriced rather than blended with the task-level
    values -- matching ``app.llm_cost_attribution._resolve_pair``, which
    degrades such rows to ``(None, None)`` instead of manufacturing chimera
    combos.

    ``has_usage`` is True when ANY attempt recorded positive tokens, whether
    or not its (provider, model) combo is priced -- a task with unpriced tokens
    has recorded usage, it just contributes 0 cost.
    """
    task_provider = row.get("provider")
    task_model = row.get("model")
    attempts = row.get("attempts") or []
    totals: dict[str, float | int] = {
        "uncached_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 0,
        "uncached_cost_usd": 0.0,
        "cache_read_cost_usd": 0.0,
        "cache_creation_cost_usd": 0.0,
        "output_cost_usd": 0.0,
    }
    has_usage = any(
        (
            int(a.get("input_tokens", 0) or 0) > 0
            or int(a.get("output_tokens", 0) or 0) > 0
            or int(a.get("cache_read_input_tokens", 0) or 0) > 0
            or int(a.get("cache_creation_input_tokens", 0) or 0) > 0
        )
        for a in attempts
    )
    task_model_fallback_count = 0
    for attempt in attempts:
        attempt_provider = attempt.get("provider")
        attempt_model = attempt.get("model")
        if attempt_provider and attempt_model:
            eff_provider, eff_model = attempt_provider, attempt_model
        elif not attempt_provider and not attempt_model:
            # Per-attempt pair genuinely absent: fall back to the task-level
            # pair and make the fallback observable to the caller.
            eff_provider, eff_model = task_provider, task_model
            task_model_fallback_count += 1
        else:
            # Half-filled: one source has a provider, the other a model.
            # Blending across sources manufactures chimera combos; degrade.
            eff_provider, eff_model = None, None
        priced = _price_attempt(
            attempt,
            eff_provider,
            eff_model,
            pricing,
            cache_read_pricing,
            cache_read_multiplier,
            cache_creation_multiplier,
        )
        if priced is None:
            continue
        for key in _INTERNAL_TOKEN_KEYS:
            totals[key] = int(totals[key]) + int(priced[key])
        for key in _INTERNAL_COST_KEYS:
            totals[key] = float(totals[key]) + float(priced[key])
    totals["total_cost_usd"] = round(
        sum(float(totals[k]) for k in _INTERNAL_COST_KEYS), COST_ROUND
    )
    return totals, has_usage, task_model_fallback_count


def _aggregate_export_records(
    records: Iterable[Mapping[str, Any]],
    provider: str,
    pricing: Mapping[tuple[str, str], tuple[float, float]],
    cache_read_pricing: Mapping[tuple[str, str], float],
) -> dict[str, float | int]:
    """Aggregate export records into totals + per-class cost estimates.

    ``total_cost_usd`` sums the provider's own ``cost_usd`` per record (the
    authoritative number). The per-class ``*_cost_usd`` estimates price the
    provider's token counts with the ledger's rates; a record whose model is
    unpriced contributes 0 to the estimates but still counts toward
    ``total_cost_usd`` and ``unpriced_record_count``.
    """
    totals: dict[str, float | int] = {
        "total_cost_usd": 0.0,
        "record_count": 0,
        "uncached_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "uncached_cost_usd": 0.0,
        "cache_read_cost_usd": 0.0,
        "output_cost_usd": 0.0,
        "unpriced_record_count": 0,
    }
    provider_key = (provider or "").lower()
    for rec in records:
        input_tokens = int(rec.get("input_tokens", 0) or 0)
        cr = int(rec.get("cache_read_input_tokens", 0) or 0)
        output_tokens = int(rec.get("output_tokens", 0) or 0)
        cost_usd = float(rec.get("cost_usd", 0.0) or 0.0)
        totals["total_cost_usd"] = float(totals["total_cost_usd"]) + cost_usd
        totals["record_count"] = int(totals["record_count"]) + 1
        totals["uncached_input_tokens"] = int(totals["uncached_input_tokens"]) + input_tokens
        totals["cache_read_input_tokens"] = int(totals["cache_read_input_tokens"]) + cr
        totals["output_tokens"] = int(totals["output_tokens"]) + output_tokens

        model_key = (rec.get("model") or "").lower()
        rate = pricing.get((provider_key, model_key))
        if rate is None:
            totals["unpriced_record_count"] = int(totals["unpriced_record_count"]) + 1
            continue
        input_rate, output_rate = rate
        cache_read_rate = cache_read_pricing.get((provider_key, model_key), input_rate)
        totals["uncached_cost_usd"] = (
            float(totals["uncached_cost_usd"]) + input_tokens * input_rate
        )
        totals["cache_read_cost_usd"] = (
            float(totals["cache_read_cost_usd"]) + cr * cache_read_rate
        )
        totals["output_cost_usd"] = (
            float(totals["output_cost_usd"]) + output_tokens * output_rate
        )
    totals["total_cost_usd"] = round(float(totals["total_cost_usd"]), COST_ROUND)
    totals["uncached_cost_usd"] = round(float(totals["uncached_cost_usd"]), COST_ROUND)
    totals["cache_read_cost_usd"] = round(float(totals["cache_read_cost_usd"]), COST_ROUND)
    totals["output_cost_usd"] = round(float(totals["output_cost_usd"]), COST_ROUND)
    return totals


def _round_report(value: float | None, ndigits: int = COST_ROUND) -> float | None:
    return round(value, ndigits) if value is not None else None


def reconcile_provider_cost(
    task_rows: Iterable[Mapping[str, Any]],
    export: Mapping[str, Any],
    provider: str,
    start: str | None = None,
    end: str | None = None,
    pricing: Mapping[tuple[str, str], tuple[float, float]] | None = None,
    cache_read_pricing: Mapping[tuple[str, str], float] | None = None,
    cache_read_multiplier: float | None = None,
    cache_creation_multiplier: float | None = None,
) -> dict[str, Any]:
    """Reconcile internally attributed cost against a provider billing export.

    Args:
        task_rows: Normalized task rows (see ``_task_to_row``). A row carries
            ``id``, ``provider``, ``model``, ``created_at`` (ISO), the
            ``estimated_cost_usd`` field (deliberately IGNORED -- the report
            is computed from recorded usage, never from the estimator), and
            ``attempts`` (a list of per-attempt token dicts). Each attempt
            carries its OWN ``provider``/``model`` (from ``_task_to_row``),
            so a task with mixed-tier attempts prices each attempt at its own
            rate.
        export: The provider billing export JSON document (see module
            docstring for the schema). Only its ``window`` and ``records``
            are read.
        provider: The provider to reconcile (e.g. ``"deepseek"``). Only task
            rows whose ``provider`` matches are included.
        start/end: ``YYYY-MM-DD`` window bounds (inclusive). When omitted,
            the export's declared ``window`` is used.
        pricing / cache_read_pricing / cache_read_multiplier /
        cache_creation_multiplier: The pricing tables and prompt-cache
            multipliers, keyed by ``(provider.lower(), model.lower())`` for
            the tables. When omitted, they resolve lazily to the SINGLE
            canonical source ``app.llm_cost_attribution`` -- there is no
            module-level mirror to silently read stale prices from. ``main()``
            passes the app's live tables in explicitly.

    Returns:
        A structured report dict (the artifact). It NEVER asserts agreement --
        the residual IS the finding.
    """
    if (
        pricing is None
        or cache_read_pricing is None
        or cache_read_multiplier is None
        or cache_creation_multiplier is None
    ):
        canonical = _default_pricing_context()
        if pricing is None:
            pricing = canonical["token_pricing"]
        if cache_read_pricing is None:
            cache_read_pricing = canonical["cache_read_pricing"]
        if cache_read_multiplier is None:
            cache_read_multiplier = canonical["cache_read_multiplier"]
        if cache_creation_multiplier is None:
            cache_creation_multiplier = canonical["cache_creation_multiplier"]

    window = export.get("window") or {}
    start_date = _parse_date(start if start is not None else window.get("start"))
    end_date = _parse_date(end if end is not None else window.get("end"))

    # ── Internal side: price recorded per-attempt usage ───────────────────
    internal: dict[str, float | int] = {
        "total_cost_usd": 0.0,
        "uncached_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 0,
        "uncached_cost_usd": 0.0,
        "cache_read_cost_usd": 0.0,
        "cache_creation_cost_usd": 0.0,
        "output_cost_usd": 0.0,
        "task_count_with_usage": 0,
        # Number of attempts priced at the TASK-level model because the
        # per-attempt pair was genuinely absent. Surfaced so silent task-level
        # pricing (the defect this module fixed) cannot hide in the report.
        "task_model_fallback_attempt_count": 0,
    }
    task_counts = {"total": 0, "with_usage": 0, "missing_usage": 0}
    provider_key = (provider or "").lower()

    for row in task_rows:
        if (row.get("provider") or "").lower() != provider_key:
            continue
        created = _parse_ts(row.get("created_at"))
        if created is None:
            continue
        if start_date is not None and created.date() < start_date:
            continue
        if end_date is not None and created.date() > end_date:
            continue
        task_counts["total"] += 1
        totals, has_usage, fallback_count = _aggregate_task_row(
            row,
            pricing,
            cache_read_pricing,
            cache_read_multiplier,
            cache_creation_multiplier,
        )
        internal["task_model_fallback_attempt_count"] = (
            int(internal["task_model_fallback_attempt_count"]) + fallback_count
        )
        if has_usage:
            task_counts["with_usage"] += 1
            for key in _INTERNAL_TOKEN_KEYS:
                internal[key] = int(internal[key]) + int(totals[key])
            for key in _INTERNAL_COST_KEYS:
                internal[key] = float(internal[key]) + float(totals[key])
        else:
            task_counts["missing_usage"] += 1
    internal["total_cost_usd"] = round(
        sum(float(internal[k]) for k in _INTERNAL_COST_KEYS), COST_ROUND
    )
    internal["task_count_with_usage"] = int(task_counts["with_usage"])
    internal["uncached_cost_usd"] = round(float(internal["uncached_cost_usd"]), COST_ROUND)
    internal["cache_read_cost_usd"] = round(float(internal["cache_read_cost_usd"]), COST_ROUND)
    internal["cache_creation_cost_usd"] = round(float(internal["cache_creation_cost_usd"]), COST_ROUND)
    internal["output_cost_usd"] = round(float(internal["output_cost_usd"]), COST_ROUND)

    # ── Export side: the provider's own numbers for the window ────────────
    kept_records = []
    for rec in export.get("records") or []:
        ts = _parse_ts(rec.get("timestamp"))
        if ts is None:
            continue
        if start_date is not None and ts.date() < start_date:
            continue
        if end_date is not None and ts.date() > end_date:
            continue
        kept_records.append(rec)
    export_totals = _aggregate_export_records(kept_records, provider, pricing, cache_read_pricing)

    # ── Residual: the finding, never a pass/fail assertion ────────────────
    internal_total = float(internal["total_cost_usd"])
    export_total = float(export_totals["total_cost_usd"])
    absolute_residual = export_total - internal_total
    residual_fraction = (
        absolute_residual / export_total if export_total > 0 else None
    )

    total_tasks = int(task_counts["total"])
    missing_usage = int(task_counts["missing_usage"])
    # Proportional estimate of the export cost attributable to missing-usage
    # tasks, under a uniform-per-task cost assumption. Clamped to the total
    # residual so it never claims more than the actual gap.
    missing_usage_estimate = 0.0
    if total_tasks > 0 and missing_usage > 0 and export_total > 0:
        missing_usage_estimate = min(
            (missing_usage / total_tasks) * export_total, max(0.0, absolute_residual)
        )

    return {
        "provider": provider,
        "window": {
            "start": start_date.isoformat() if start_date else None,
            "end": end_date.isoformat() if end_date else None,
        },
        "internal": internal,
        "export": {
            "total_cost_usd": export_total,
            "record_count": int(export_totals["record_count"]),
            "uncached_input_tokens": int(export_totals["uncached_input_tokens"]),
            "cache_read_input_tokens": int(export_totals["cache_read_input_tokens"]),
            "output_tokens": int(export_totals["output_tokens"]),
            "cache_read_cost_usd": float(export_totals["cache_read_cost_usd"]),
            "uncached_cost_usd": float(export_totals["uncached_cost_usd"]),
            "output_cost_usd": float(export_totals["output_cost_usd"]),
            "unpriced_record_count": int(export_totals["unpriced_record_count"]),
        },
        "tasks": {
            "total": total_tasks,
            "with_usage": int(task_counts["with_usage"]),
            "missing_usage": missing_usage,
            "usage_coverage_fraction": (
                _round_report(task_counts["with_usage"] / total_tasks, 4)
                if total_tasks > 0
                else None
            ),
        },
        "residual": {
            "absolute_usd": _round_report(absolute_residual),
            "residual_fraction": _round_report(residual_fraction, 4),
            "missing_usage_residual_estimate_usd": _round_report(
                missing_usage_estimate
            ),
        },
        "cache_read_breakout": {
            "internal_tokens": int(internal["cache_read_input_tokens"]),
            "export_tokens": int(export_totals["cache_read_input_tokens"]),
            "internal_cost_usd": float(internal["cache_read_cost_usd"]),
            "export_cost_usd": float(export_totals["cache_read_cost_usd"]),
        },
        "uncached_breakout": {
            "internal_tokens": int(internal["uncached_input_tokens"]),
            "export_tokens": int(export_totals["uncached_input_tokens"]),
            "internal_cost_usd": float(internal["uncached_cost_usd"]),
            "export_cost_usd": float(export_totals["uncached_cost_usd"]),
        },
    }


# ── ORM -> normalized row conversion (DB path, lazy app imports) ──────────


def _task_to_row(task: Any, attempts: list[tuple]) -> dict[str, Any]:
    """Reduce an ORM ``Task`` + its ``_task_llm_attempts`` rows to a row dict."""
    return {
        "id": str(task.id) if getattr(task, "id", None) else None,
        "provider": task.provider,
        "model": task.model,
        "created_at": (
            task.created_at.isoformat() if getattr(task, "created_at", None) else None
        ),
        "estimated_cost_usd": getattr(task, "estimated_cost_usd", None),
        "attempts": [
            {
                "provider": p,
                "model": m,
                "input_tokens": i,
                "output_tokens": o,
                "call_count": c,
                "cache_creation_input_tokens": cc,
                "cache_read_input_tokens": cr,
            }
            for (p, m, i, o, c, cc, cr) in attempts
        ],
    }


async def _load_task_rows(provider: str, start: str | None, end: str | None) -> list[dict[str, Any]]:
    """Read tasks for the provider in the window and reduce them to rows.

    Lazy ``app.*`` import so the module stays stdlib-only at import time.
    ``_task_llm_attempts`` is the SAME iterator ``app.llm_cost_attribution``
    prices, so the internal tokens here are exactly the ledger's recorded
    per-attempt usage.
    """
    backend_root = Path(__file__).resolve().parents[1]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    from app.database import AsyncSessionLocal
    from app.llm_cost_attribution import _task_llm_attempts
    from app.models import Task
    from sqlalchemy import select

    # Window bounds as naive-UTC datetimes (the model's storage convention):
    # [start 00:00:00, end+1day 00:00:00) so the end date is inclusive.
    start_dt = _day_start_naive_utc(start)
    end_dt_exclusive = _day_start_naive_utc(end, offset_days=1)

    stmt = select(Task).where(Task.provider == provider)
    if start_dt is not None:
        stmt = stmt.where(Task.created_at >= start_dt)
    if end_dt_exclusive is not None:
        stmt = stmt.where(Task.created_at < end_dt_exclusive)

    async with AsyncSessionLocal() as session:
        tasks = (await session.execute(stmt)).scalars().all()
    return [_task_to_row(t, _task_llm_attempts(t)) for t in tasks]


def load_export(path: str | Path) -> dict[str, Any]:
    """Load and minimally validate a provider billing export JSON document."""
    with open(path, encoding="utf-8") as fh:
        export = json.load(fh)
    if not isinstance(export, dict):
        raise ValueError("export must be a JSON object")
    records = export.get("records")
    if not isinstance(records, list):
        raise ValueError("export must contain a \"records\" list")
    return export


# ── CLI ────────────────────────────────────────────────────────────────────


def _fmt_usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.6f}"


def _print_report(
    report: Mapping[str, Any],
    task_rows: Iterable[Mapping[str, Any]],
    export: Mapping[str, Any],
) -> None:
    internal = report["internal"]
    export_totals = report["export"]
    tasks = report["tasks"]
    residual = report["residual"]
    crb = report["cache_read_breakout"]
    ucb = report["uncached_breakout"]

    print("=" * 78)
    print("Cost reconciliation: internal attribution vs provider billing export")
    print(f"  provider : {report['provider']}")
    print(
        f"  window   : {report['window']['start']} .. {report['window']['end']}"
        f"  (export {export.get('exported_at', '?')})"
    )
    print("=" * 78)

    print("\n[1] Internal attribution (recorded per-attempt usage, NEVER estimated_cost_usd)\n")
    print(f"  total_cost_usd             : {_fmt_usd(internal['total_cost_usd'])}")
    print(f"  uncached input tokens      : {internal['uncached_input_tokens']}")
    print(f"  cache-read input tokens    : {internal['cache_read_input_tokens']}")
    print(f"  cache-creation input tokens: {internal['cache_creation_input_tokens']}")
    print(f"  output tokens              : {internal['output_tokens']}")
    print(f"  tasks with recorded usage  : {tasks['with_usage']}")
    fallback_count = internal.get("task_model_fallback_attempt_count", 0)
    if fallback_count:
        print(f"  task-model pricing fallback: {fallback_count} attempts priced at the task-level model")

    print("\n[2] Provider export (operator-supplied, ground truth for the window)\n")
    print(f"  total_cost_usd             : {_fmt_usd(export_totals['total_cost_usd'])}")
    print(f"  record_count               : {export_totals['record_count']}")
    print(f"  uncached input tokens      : {export_totals['uncached_input_tokens']}")
    print(f"  cache-read input tokens    : {export_totals['cache_read_input_tokens']}")
    print(f"  output tokens              : {export_totals['output_tokens']}")
    if export_totals.get("unpriced_record_count"):
        print(f"  unpriced records (est. 0)  : {export_totals['unpriced_record_count']}")

    print("\n[3] Task usage coverage\n")
    print(f"  tasks total                : {tasks['total']}")
    print(f"  with usage                 : {tasks['with_usage']}")
    print(f"  MISSING usage              : {tasks['missing_usage']}")
    coverage = tasks["usage_coverage_fraction"]
    print(f"  usage coverage             : {'n/a' if coverage is None else f'{coverage:.1%}'}")

    print("\n[4] RESIDUAL (export - internal) -- the finding, not a pass/fail\n")
    print(f"  absolute residual          : {_fmt_usd(residual['absolute_usd'])}")
    frac = residual["residual_fraction"]
    print(f"  residual fraction of export: {'n/a' if frac is None else f'{frac:.1%}'}")
    print(
        f"  missing-usage est. residual: "
        f"{_fmt_usd(residual['missing_usage_residual_estimate_usd'])}"
    )
    print("    (proportional estimate: missing/total tasks x export total,")
    print("     clamped to the actual residual -- uniform-per-task assumption)")

    print("\n[5] Cache-read vs uncached breakout (unrecorded cache-read is the\n"
          "    suspected dominant term)\n")
    print(f"  cache-read  internal tokens: {crb['internal_tokens']:>14,}  "
          f"cost {_fmt_usd(crb['internal_cost_usd'])}")
    print(f"  cache-read  export  tokens : {crb['export_tokens']:>14,}  "
          f"cost {_fmt_usd(crb['export_cost_usd'])} (est. from tokens x rate)")
    print(f"  uncached    internal tokens: {ucb['internal_tokens']:>14,}  "
          f"cost {_fmt_usd(ucb['internal_cost_usd'])}")
    print(f"  uncached    export  tokens : {ucb['export_tokens']:>14,}  "
          f"cost {_fmt_usd(ucb['export_cost_usd'])} (est. from tokens x rate)")
    print("\n  A LARGE positive residual with a high missing-usage count is the")
    print("  CORRECT output for today's data -- it is the finding, not a bug.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider", required=True, help="Provider to reconcile (e.g. deepseek)")
    parser.add_argument(
        "--export",
        type=str,
        default=str(SAMPLE_EXPORT_PATH),
        help=(
            "Path to a provider billing export JSON document "
            f"(default: the checked-in sample {SAMPLE_EXPORT_PATH.name})"
        ),
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Window start YYYY-MM-DD (default: the export's declared window)",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="Window end YYYY-MM-DD (default: the export's declared window)",
    )
    parser.add_argument(
        "--write-json",
        type=str,
        default=None,
        metavar="PATH",
        help="Write the structured report as a JSON document to PATH",
    )
    args = parser.parse_args()

    export = load_export(args.export)
    provider = args.provider
    start = args.start
    end = args.end

    export_provider = export.get("provider")
    if export_provider and str(export_provider).lower() != provider.lower():
        print(
            f"WARNING: --provider {provider} does not match the export's declared "
            f"provider {export_provider!r}; the comparison may be meaningless.",
            file=sys.stderr,
        )

    task_rows = asyncio.run(_load_task_rows(provider, start, end))
    # Pass the ledger's LIVE pricing tables in explicitly (matching the
    # module docstring): the CLI is priced exactly as the ledger prices it.
    from app.llm_cost_attribution import (
        CACHE_CREATION_INPUT_RATE_MULTIPLIER,
        CACHE_READ_INPUT_RATE_MULTIPLIER,
        LLM_CACHE_READ_INPUT_PRICING,
        LLM_TOKEN_PRICING,
    )

    report = reconcile_provider_cost(
        task_rows,
        export,
        provider,
        start=start,
        end=end,
        pricing=LLM_TOKEN_PRICING,
        cache_read_pricing=LLM_CACHE_READ_INPUT_PRICING,
        cache_read_multiplier=CACHE_READ_INPUT_RATE_MULTIPLIER,
        cache_creation_multiplier=CACHE_CREATION_INPUT_RATE_MULTIPLIER,
    )
    _print_report(report, task_rows, export)

    if args.write_json:
        out = Path(args.write_json)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nWrote report JSON to {out}")


if __name__ == "__main__":
    main()
