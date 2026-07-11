"""LLM token cost attribution helpers.

Extracted from efficiency_snapshot to keep that module focused on snapshot
persistence and axis computation. This module owns all per-attempt token
pricing logic, per-model/task-type/failure-class cost breakdowns, and the
compact attribution block written into daily snapshots.
"""

from __future__ import annotations

from typing import Any, Dict

from app.landed_signal import is_merged_completion, is_verified_merge
from app.models import (
    Task,
    TaskStatus,
    get_task_current_attempt_cache_input_tokens,
    get_task_current_attempt_llm_totals,
    get_task_total_llm_totals,
    get_task_total_vm_cost_usd,
)

# LLM token pricing table: (provider, model) -> (input_$/token, output_$/token).
# Per-token rates = per-million-token rate / 1_000_000.
#
# Sources: Anthropic, OpenAI, Gemini, Z.AI, and DeepSeek API pricing pages
# refreshed during provider-cost updates. Gemini CLI token counts are estimated
# from prompt/result chars upstream before exit paths.
LLM_TOKEN_PRICING: dict[tuple[str, str], tuple[float, float]] = {
    # Anthropic
    # TEMPORARY model (limited availability ~2 weeks from 2026-06-09). Keep
    # this pricing row after retirement so historical fable runs stay priced
    # in cost dashboards. ($10/$50 per MTok list, platform.claude.com 2026-06.)
    ("anthropic", "claude-fable-5"): (10e-6, 50e-6),
    ("anthropic", "claude-opus-4-8"): (5e-6, 25e-6),
    ("anthropic", "claude-opus-4-7"): (5e-6, 25e-6),
    ("anthropic", "claude-opus-4-6"): (5e-6, 25e-6),
    ("anthropic", "claude-sonnet-4-6"): (3e-6, 15e-6),
    ("anthropic", "claude-haiku-4-5-20251001"): (1e-6, 5e-6),
    # OpenAI (developers.openai.com/api/docs/pricing, refreshed 2026-07-11 at
    # the GPT-5.6 picker cycle; older rows kept so historical runs stay priced).
    ("openai", "gpt-5.6-sol"): (5e-6, 30e-6),
    ("openai", "gpt-5.6-terra"): (2.5e-6, 15e-6),
    ("openai", "gpt-5.6-luna"): (1e-6, 6e-6),
    ("openai", "gpt-5.5"): (5e-6, 30e-6),
    ("openai", "gpt-5.4"): (2.5e-6, 15e-6),
    ("openai", "gpt-5.4-mini"): (0.75e-6, 4.5e-6),
    ("openai", "gpt-5.3-codex"): (1.75e-6, 14e-6),
    # gpt-5.3-codex-spark is ChatGPT-Pro-only with no separately published API
    # rate; priced at the gpt-5.3-codex rate as the closest GA equivalent
    # (openai.com/api/pricing, 2026-05).
    ("openai", "gpt-5.3-codex-spark"): (1.75e-6, 14e-6),
    # gpt-5.2 pre-dates the 5.3 generation (openai.com/api/pricing, 2026-05).
    ("openai", "gpt-5.2"): (2e-6, 8e-6),
    # Gemini GA models (ai.google.dev/gemini-api/docs/pricing, 2026-05)
    ("gemini", "gemini-2.5-pro"): (1.25e-6, 10e-6),
    ("gemini", "gemini-2.5-flash"): (0.30e-6, 2.5e-6),
    ("gemini", "gemini-2.5-flash-lite"): (0.10e-6, 0.40e-6),
    # Gemini 3.x previews have no separately published rate; priced at the
    # closest GA-tier equivalent (ai.google.dev/gemini-api/docs/pricing, 2026-05).
    ("gemini", "gemini-3.1-pro-preview"): (1.25e-6, 10e-6),
    ("gemini", "gemini-3-flash-preview"): (0.30e-6, 2.5e-6),
    ("gemini", "gemini-3.1-flash-lite-preview"): (0.10e-6, 0.40e-6),
    # Z.AI GLM-5.2 API (docs.z.ai/guides/overview/pricing, 2026-06).
    ("zai", "glm-5.2"): (1.40e-6, 4.40e-6),
    # DeepSeek V4 API cache-miss list rates (api-docs.deepseek.com, 2026-07).
    ("deepseek", "deepseek-v4-flash"): (0.14e-6, 0.28e-6),
    ("deepseek", "deepseek-v4-pro"): (0.435e-6, 0.87e-6),
}

# Provider/model combos priced at a proxy rate because the exact variant has no
# separately published API pricing.
LLM_APPROXIMATE_PRICED: set[tuple[str, str]] = {
    ("openai", "gpt-5.3-codex-spark"),  # ChatGPT-Pro-only variant
    ("gemini", "gemini-3.1-pro-preview"),  # Preview variant
    ("gemini", "gemini-3-flash-preview"),  # Preview variant
    ("gemini", "gemini-3.1-flash-lite-preview"),  # Preview variant
}

# Prompt-cache pricing; input totals are inclusive of cache sub-counts.
CACHE_READ_INPUT_RATE_MULTIPLIER = 0.1
CACHE_CREATION_INPUT_RATE_MULTIPLIER = 1.25

# Provider/model cache-read rates; writes/misses use the base input rate.
LLM_CACHE_READ_INPUT_PRICING: dict[tuple[str, str], float] = {
    ("openai", "gpt-5.6-sol"): 0.50e-6,
    ("openai", "gpt-5.6-terra"): 0.25e-6,
    ("openai", "gpt-5.6-luna"): 0.10e-6,
    ("openai", "gpt-5.5"): 0.50e-6,
    ("openai", "gpt-5.4"): 0.25e-6,
    ("openai", "gpt-5.4-mini"): 0.075e-6,
    # Historical/proxy OpenAI entries retain the pricing table's existing base
    # rates and use the same 0.1x cached-input relationship.
    ("openai", "gpt-5.3-codex"): 0.175e-6,
    ("openai", "gpt-5.3-codex-spark"): 0.175e-6,
    ("openai", "gpt-5.2"): 0.20e-6,
    ("deepseek", "deepseek-v4-flash"): 0.0028e-6,
    ("deepseek", "deepseek-v4-pro"): 0.003625e-6,
    ("zai", "glm-5.2"): 0.26e-6,
}

# Key read by efficiency_snapshot consumers.
SELF_REPAIR_BURN_SHARE_KEY = "self_repair_burn_share"


def _bounded_cache_input_counts(
    input_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
) -> tuple[int, int]:
    """Return cache sub-counts that cannot exceed the inclusive input total."""
    total = max(0, int(input_tokens))
    cc = max(0, int(cache_creation_input_tokens))
    cr = max(0, int(cache_read_input_tokens))
    if cc + cr <= total:
        return cc, cr

    # Prefer preserving cache-read tokens when malformed telemetry overflows the
    # inclusive total because read tokens usually have the largest discount.
    cr = min(cr, total)
    cc = min(cc, total - cr)
    return cc, cr


def _price_llm_attempt(
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    input_rate: float,
    output_rate: float,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> float:
    """Price one attempt's tokens, charging provider cache classes truthfully.

    ``input_tokens`` is the INCLUSIVE input total (uncached + cache-creation +
    cache-read). When the attempt carries no cache breakdown the sub-counts are
    ``0``, so this reduces to ``input_tokens * input_rate + output_tokens *
    output_rate`` — byte-for-byte the legacy valuation, which is the required
    fallback for pre-tracking rows.
    """
    provider_key = (provider or "").lower()
    model_key = (model or "").lower()
    cc, cr = _bounded_cache_input_counts(
        input_tokens, cache_creation_input_tokens, cache_read_input_tokens
    )
    if provider_key == "anthropic":
        cache_creation_rate = input_rate * CACHE_CREATION_INPUT_RATE_MULTIPLIER
        cache_read_rate = input_rate * CACHE_READ_INPUT_RATE_MULTIPLIER
    elif (provider_key, model_key) in LLM_CACHE_READ_INPUT_PRICING:
        cache_creation_rate = input_rate
        cache_read_rate = LLM_CACHE_READ_INPUT_PRICING[(provider_key, model_key)]
    else:
        cache_creation_rate = input_rate
        cache_read_rate = input_rate
    uncached = max(0, int(input_tokens) - cc - cr)
    return (
        uncached * input_rate
        + cc * cache_creation_rate
        + cr * cache_read_rate
        + output_tokens * output_rate
    )


def _compute_llm_cost_per_merged_pr(tasks: list[Task]) -> float | None:
    """Total LLM token cost / merged PR count over the window. None when no merges."""
    total_cost = 0.0
    merged = 0
    for t in tasks:
        for (
            provider,
            model,
            input_tokens,
            output_tokens,
            _call_count,
            cache_creation,
            cache_read,
        ) in _task_llm_attempts(t):
            if provider and model:
                pricing = LLM_TOKEN_PRICING.get((provider.lower(), model.lower()))
                if pricing:
                    input_rate, output_rate = pricing
                    total_cost += _price_llm_attempt(
                        input_tokens,
                        output_tokens,
                        cache_creation,
                        cache_read,
                        input_rate,
                        output_rate,
                        provider=provider,
                        model=model,
                    )
        if is_merged_completion(t):
            merged += 1
    if not merged:
        return None
    return round(total_cost / merged, 6)


def _compute_llm_cost_per_verified_task(tasks: list[Task]) -> float | None:
    """Total LLM token cost / CI-verified merge count over the window.

    Uses :func:`is_verified_merge` as the denominator so fall-open (CI-bypassed)
    merges do not dilute the per-verified-task LLM cost. Returns None when no
    verified merges exist.
    """
    total_cost = 0.0
    verified = 0
    for t in tasks:
        for (
            provider,
            model,
            input_tokens,
            output_tokens,
            _call_count,
            cache_creation,
            cache_read,
        ) in _task_llm_attempts(t):
            if provider and model:
                pricing = LLM_TOKEN_PRICING.get((provider.lower(), model.lower()))
                if pricing:
                    input_rate, output_rate = pricing
                    total_cost += _price_llm_attempt(
                        input_tokens,
                        output_tokens,
                        cache_creation,
                        cache_read,
                        input_rate,
                        output_rate,
                        provider=provider,
                        model=model,
                    )
        if is_verified_merge(t):
            verified += 1
    if not verified:
        return None
    return round(total_cost / verified, 6)


_UNPRICED_COMBOS_CAP = 10


def _task_ran_on_fallback(t: Task) -> bool:
    """True when the task's runtime selection diverged from its intended routing.

    Reads the markers stamped by max_plan_pool's selection path:
    ``routing_fallback`` (cross-provider / synthetic no-account) and
    ``model_fallback`` (same-provider preview→GA downgrade). Either marker means
    the resolved provider/model the cost breakdown attributes to is NOT
    necessarily the routing the operator configured, so the bucket should be
    caveated.
    """
    ctx = getattr(t, "context", None) or {}
    if not isinstance(ctx, dict):
        return False
    return bool(ctx.get("routing_fallback") or ctx.get("model_fallback"))


def _llm_attempt_nonnegative_int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and value >= 0 else 0


def _task_llm_attempts(
    task: Task,
) -> list[tuple[str | None, str | None, int, int, int, int, int]]:
    """Per-attempt ``(provider, model, input, output, call_count, cache_creation,
    cache_read)`` rows.

    ``input`` is the INCLUSIVE input total; ``cache_creation`` / ``cache_read``
    are provider-reported cache sub-counts (``0`` for attempts that predate cache
    tracking, which makes the pricing helper fall back to the base input rate).
    """
    history = task.attempt_history or []
    if not history:
        input_tokens, output_tokens, call_count = get_task_total_llm_totals(task)
        cache_creation, cache_read = get_task_current_attempt_cache_input_tokens(task)
        cache_creation, cache_read = _bounded_cache_input_counts(
            input_tokens, cache_creation, cache_read
        )
        return (
            [
                (
                    task.provider,
                    task.model,
                    input_tokens,
                    output_tokens,
                    call_count,
                    cache_creation,
                    cache_read,
                )
            ]
            if input_tokens > 0 or output_tokens > 0
            else []
        )

    def _resolve_pair(attempt: dict) -> tuple[str | None, str | None]:
        ap = attempt.get("provider")
        am = attempt.get("model")
        if ap and am:
            return ap, am
        if not ap and not am:
            return task.provider, task.model
        # Half-filled row: one source has provider, another has model.
        # Blending across sources manufactures chimera combos; degrade instead.
        return None, None

    attempts = []
    for attempt in history:
        input_tokens = _llm_attempt_nonnegative_int(attempt.get("llm_input_tokens"))
        cache_creation, cache_read = _bounded_cache_input_counts(
            input_tokens,
            _llm_attempt_nonnegative_int(
                attempt.get("llm_cache_creation_input_tokens")
            ),
            _llm_attempt_nonnegative_int(attempt.get("llm_cache_read_input_tokens")),
        )
        attempts.append(
            (
                *_resolve_pair(attempt),
                input_tokens,
                _llm_attempt_nonnegative_int(attempt.get("llm_output_tokens")),
                _llm_attempt_nonnegative_int(attempt.get("llm_call_count")),
                cache_creation,
                cache_read,
            )
        )
    current_input, current_output, current_calls = get_task_current_attempt_llm_totals(
        task
    )
    if current_input > 0 or current_output > 0:
        current_cc, current_cr = get_task_current_attempt_cache_input_tokens(task)
        current_cc, current_cr = _bounded_cache_input_counts(
            current_input, current_cc, current_cr
        )
        attempts.append(
            (
                task.provider,
                task.model,
                current_input,
                current_output,
                current_calls,
                current_cc,
                current_cr,
            )
        )
    return attempts


def compute_llm_cost_breakdown(tasks: list[Task]) -> Dict[str, Any]:
    """Per-provider and per-model LLM cost/token rollup over the given tasks.

    Returns a structured dict with:
      - ``by_provider``: list of provider-level rollups.
      - ``by_model``: list of (provider, model) rollups with a ``priced`` flag.
      - ``totals``: aggregate totals plus priced/unpriced task counts.
      - ``unpriced_combos``: capped list of ``provider:model`` strings that
        have non-zero tokens but no entry in :data:`LLM_TOKEN_PRICING`.
      - ``approximate_priced_combos``: capped list of ``provider:model`` strings
        that are priced at a proxy rate (closest GA equivalent) with no
        separately published API rate.

    A task contributes one or more attempt rows to a bucket when it has a
    non-null provider, non-null model, and non-zero aggregate tokens across
    its banked retry history plus the live ``api_call_summary`` snapshot.
    Unpriced (provider, model) combos are still counted in ``task_count`` and feed
    ``unpriced_coverage_fraction`` but contribute ``0.0`` cost. Cost values
    are rounded to 6 decimals and shares to 4 decimals, matching the rest of
    this module.

    Each provider/model row also carries the merge outcome of its tasks:
    ``merged_count`` (tasks landed on the integration branch per
    :func:`is_merged_completion`), ``merge_rate`` (``merged_count /
    task_count``, ``None`` only when the bucket has no tasks), plus
    ``verified_merged_count`` / ``verified_merge_rate`` using
    :func:`is_verified_merge`. ``cost_per_merge`` now divides by the verified
    merge count so degraded fall-open merges do not make spend look cheaper.
    It stays ``None`` for unpriced buckets (cost is understated) and for zero-
    verified-merge buckets (no divide-by-zero, reads as "0 of N verified"
    via the count/rate fields instead).
    """
    from collections import defaultdict

    model_buckets: Dict[tuple[str, str], Dict[str, float]] = defaultdict(
        lambda: {
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
            "llm_call_count": 0,
            "task_count": 0,
            "merged_count": 0,
            "verified_merged_count": 0,
            "cost_usd": 0.0,
        }
    )
    provider_buckets: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
            "llm_call_count": 0,
            "task_count": 0,
            "merged_count": 0,
            "verified_merged_count": 0,
            "cost_usd": 0.0,
        }
    )
    priced_combos: set[tuple[str, str]] = set()
    unpriced_providers: set[str] = set()
    unpriced_combos: list[str] = []
    approximate_priced_combos: list[str] = []
    priced_task_count = 0
    unpriced_task_count = 0
    fallback_task_count = 0
    fallback_cost_usd = 0.0
    total_input = 0
    total_cache_creation = 0
    total_cache_read = 0
    total_output = 0
    total_cost = 0.0
    total_task_count = 0
    total_merged_count = 0
    total_verified_merged_count = 0
    total_tool_result_bytes: int | None = None

    for t in tasks:
        merged = is_merged_completion(t)
        verified = is_verified_merge(t)
        is_fallback = _task_ran_on_fallback(t)
        # Tokens/cost accumulate per attempt-row, but task_count and merge
        # outcomes are TASK-level: each must be credited at most ONCE per
        # distinct bucket the task touched, never once per attempt-row.
        # Collect the distinct buckets this task contributed a row to, then
        # credit task_count and merge outcomes after the attempt loop.
        task_model_keys: set[tuple[str, str]] = set()
        task_provider_keys: set[str] = set()
        task_contributed = False
        task_had_priced = False
        task_had_unpriced = False
        for (
            provider,
            model,
            input_tokens,
            output_tokens,
            call_count,
            cache_creation,
            cache_read,
        ) in _task_llm_attempts(t):
            if input_tokens <= 0 and output_tokens <= 0:
                continue
            if not provider or not model:
                provider = "unattributed"
                model = "unknown"
            else:
                provider = provider.lower()
                model = model.lower()
            key = (provider, model)
            pricing = LLM_TOKEN_PRICING.get(key)
            if pricing:
                input_rate, output_rate = pricing
                cost = _price_llm_attempt(
                    input_tokens,
                    output_tokens,
                    cache_creation,
                    cache_read,
                    input_rate,
                    output_rate,
                    provider=provider,
                    model=model,
                )
                priced_combos.add(key)
                task_had_priced = True
                # Track approximate-priced combos (proxy rate, not exactly published).
                if key in LLM_APPROXIMATE_PRICED:
                    combo = f"{provider}:{model}"
                    if (
                        combo not in approximate_priced_combos
                        and len(approximate_priced_combos) < _UNPRICED_COMBOS_CAP
                    ):
                        approximate_priced_combos.append(combo)
            else:
                cost = 0.0
                task_had_unpriced = True
                unpriced_providers.add(provider)
                combo = f"{provider}:{model}"
                if (
                    combo not in unpriced_combos
                    and len(unpriced_combos) < _UNPRICED_COMBOS_CAP
                ):
                    unpriced_combos.append(combo)

            # fallback_cost_usd is per-attempt (each fallback attempt's spend
            # counts); fallback_task_count is per-task, credited once below.
            if is_fallback:
                fallback_cost_usd += cost

            cc = int(cache_creation)
            cr = int(cache_read)

            mb = model_buckets[key]
            mb["input_tokens"] += int(input_tokens)
            mb["cache_creation_input_tokens"] += cc
            mb["cache_read_input_tokens"] += cr
            mb["output_tokens"] += int(output_tokens)
            mb["llm_call_count"] += call_count
            mb["cost_usd"] += cost

            pb = provider_buckets[provider]
            pb["input_tokens"] += int(input_tokens)
            pb["cache_creation_input_tokens"] += cc
            pb["cache_read_input_tokens"] += cr
            pb["output_tokens"] += int(output_tokens)
            pb["llm_call_count"] += call_count
            pb["cost_usd"] += cost

            total_input += int(input_tokens)
            total_cache_creation += cc
            total_cache_read += cr
            total_output += int(output_tokens)
            total_cost += cost

            task_model_keys.add(key)
            task_provider_keys.add(provider)
            task_contributed = True

        # Credit task_count, merge outcomes, and priced/unpriced counts once
        # per distinct bucket the task touched — never once per attempt-row —
        # so multi-attempt tasks don't inflate task_count or deflate merge_rate.
        if task_contributed:
            for key in task_model_keys:
                model_buckets[key]["task_count"] += 1
            for provider in task_provider_keys:
                provider_buckets[provider]["task_count"] += 1
            total_task_count += 1
            # A task that had at least one priced attempt counts as priced;
            # only tasks with zero priced attempts count as unpriced.
            if task_had_priced:
                priced_task_count += 1
            elif task_had_unpriced:
                unpriced_task_count += 1

        if merged and task_contributed:
            for key in task_model_keys:
                model_buckets[key]["merged_count"] += 1
            for provider in task_provider_keys:
                provider_buckets[provider]["merged_count"] += 1
            total_merged_count += 1

        if verified and task_contributed:
            for key in task_model_keys:
                model_buckets[key]["verified_merged_count"] += 1
            for provider in task_provider_keys:
                provider_buckets[provider]["verified_merged_count"] += 1
            total_verified_merged_count += 1

        # Count each fallback task once (cost already accumulated per-attempt).
        if is_fallback and task_contributed:
            fallback_task_count += 1

        # Accumulate total_tool_result_bytes across attempt_history + live api_call_summary.
        for attempt in t.attempt_history or []:
            trb = attempt.get("total_tool_result_bytes")
            if isinstance(trb, int) and trb >= 0:
                total_tool_result_bytes = (total_tool_result_bytes or 0) + trb
        current_trb = (t.api_call_summary or {}).get("total_tool_result_bytes")
        if isinstance(current_trb, int) and current_trb >= 0:
            total_tool_result_bytes = (total_tool_result_bytes or 0) + current_trb

    def _share(cost: float) -> float | None:
        if total_cost <= 0:
            return None
        return round(cost / total_cost, 4)

    def _merge_rate(merged: float, count: float) -> float | None:
        if count <= 0:
            return None
        return round(merged / count, 4)

    def _cost_per_merge(
        cost: float, verified_merged: float, priced: bool
    ) -> float | None:
        # Only present a confident cost-per-merge when cost is fully known.
        # Unpriced buckets understate cost, so the figure would mislead; a
        # zero-verified-merge bucket would divide by zero.
        if not priced or verified_merged <= 0:
            return None
        return round(cost / verified_merged, 6)

    by_provider = [
        {
            "provider": provider,
            "total_input_tokens": int(pb["input_tokens"]),
            "total_cache_creation_input_tokens": int(pb["cache_creation_input_tokens"]),
            "total_cache_read_input_tokens": int(pb["cache_read_input_tokens"]),
            "total_output_tokens": int(pb["output_tokens"]),
            "total_cost_usd": round(pb["cost_usd"], 6),
            "mean_cost_per_task": round(pb["cost_usd"] / pb["task_count"], 6)
            if pb["task_count"]
            else 0.0,
            "share_of_total_cost": _share(pb["cost_usd"]),
            "task_count": int(pb["task_count"]),
            "llm_call_count": int(pb["llm_call_count"]),
            "merged_count": int(pb["merged_count"]),
            "merge_rate": _merge_rate(pb["merged_count"], pb["task_count"]),
            "verified_merged_count": int(pb["verified_merged_count"]),
            "verified_merge_rate": _merge_rate(
                pb["verified_merged_count"], pb["task_count"]
            ),
            "priced": provider not in unpriced_providers,
            "cost_per_merge": _cost_per_merge(
                pb["cost_usd"],
                pb["verified_merged_count"],
                provider not in unpriced_providers,
            ),
        }
        for provider, pb in sorted(
            provider_buckets.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True
        )
    ]
    by_model = [
        {
            "provider": provider,
            "model": model,
            "total_input_tokens": int(mb["input_tokens"]),
            "total_cache_creation_input_tokens": int(mb["cache_creation_input_tokens"]),
            "total_cache_read_input_tokens": int(mb["cache_read_input_tokens"]),
            "total_output_tokens": int(mb["output_tokens"]),
            "total_cost_usd": round(mb["cost_usd"], 6),
            "mean_cost_per_task": round(mb["cost_usd"] / mb["task_count"], 6)
            if mb["task_count"]
            else 0.0,
            "share_of_total_cost": _share(mb["cost_usd"]),
            "task_count": int(mb["task_count"]),
            "llm_call_count": int(mb["llm_call_count"]),
            "merged_count": int(mb["merged_count"]),
            "merge_rate": _merge_rate(mb["merged_count"], mb["task_count"]),
            "verified_merged_count": int(mb["verified_merged_count"]),
            "verified_merge_rate": _merge_rate(
                mb["verified_merged_count"], mb["task_count"]
            ),
            "priced": (provider, model) in priced_combos,
            "approximate_priced": (provider, model) in LLM_APPROXIMATE_PRICED,
            "cost_per_merge": _cost_per_merge(
                mb["cost_usd"],
                mb["verified_merged_count"],
                (provider, model) in priced_combos,
            ),
        }
        for (provider, model), mb in sorted(
            model_buckets.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True
        )
    ]
    priceable = priced_task_count + unpriced_task_count
    unpriced_coverage_fraction: float | None = (
        round(priced_task_count / priceable, 4) if priceable else None
    )
    return {
        "by_provider": by_provider,
        "by_model": by_model,
        "totals": {
            "total_input_tokens": total_input,
            "total_cache_creation_input_tokens": total_cache_creation,
            "total_cache_read_input_tokens": total_cache_read,
            "total_output_tokens": total_output,
            "total_cost_usd": round(total_cost, 6),
            "task_count": total_task_count,
            "merged_count": total_merged_count,
            "merge_rate": round(total_merged_count / total_task_count, 4)
            if total_task_count
            else None,
            "verified_merged_count": total_verified_merged_count,
            "verified_merge_rate": round(
                total_verified_merged_count / total_task_count, 4
            )
            if total_task_count
            else None,
            "priced_task_count": priced_task_count,
            "unpriced_task_count": unpriced_task_count,
            "unpriced_coverage_fraction": unpriced_coverage_fraction,
            "fallback_task_count": fallback_task_count,
            "fallback_cost_usd": round(fallback_cost_usd, 6),
            "total_tool_result_bytes": total_tool_result_bytes,
        },
        "unpriced_combos": unpriced_combos,
        "approximate_priced_combos": approximate_priced_combos,
    }


def _task_llm_cost(task: Task) -> tuple[float, bool]:
    """Return (total_llm_usd, any_unpriced) for a single task."""
    cost = 0.0
    any_unpriced = False
    for (
        provider,
        model,
        input_tokens,
        output_tokens,
        _call_count,
        cache_creation,
        cache_read,
    ) in _task_llm_attempts(task):
        if input_tokens <= 0 and output_tokens <= 0:
            continue
        if not provider or not model:
            any_unpriced = True
            continue
        key = (provider.lower(), model.lower())
        pricing = LLM_TOKEN_PRICING.get(key)
        if pricing:
            input_rate, output_rate = pricing
            cost += _price_llm_attempt(
                input_tokens,
                output_tokens,
                cache_creation,
                cache_read,
                input_rate,
                output_rate,
                provider=key[0],
                model=key[1],
            )
        else:
            any_unpriced = True
    return cost, any_unpriced


def _pr_number_for_task(task: Task) -> int | None:
    """Extract PR number from first payload or review_loop context."""
    pr = (task.first_payload or {}).get("pr_number")
    if pr is not None:
        try:
            return int(pr)
        except (TypeError, ValueError):
            pass
    loop_pr = (task.context or {}).get("review_loop", {}).get("pr_number")
    if loop_pr is not None:
        try:
            return int(loop_pr)
        except (TypeError, ValueError):
            pass
    return None


def _is_rework_task(task: Task) -> bool:
    """True when a task is rework: a fix task, or a review with iteration > 1."""
    task_type = (task.first_payload or {}).get("type") or ""
    if task_type == "fix":
        return True
    iteration = (task.context or {}).get("review_loop", {}).get("iteration")
    return bool(isinstance(iteration, int) and iteration > 1)


def compute_pr_rework_chain_breakdown(
    tasks: list[Task], *, top_n: int = 10
) -> Dict[str, Any]:
    """Per-PR chain LLM cost split into first-pass vs rework over the given tasks.

    Groups recently-terminal tasks by their PR number (``first_payload.pr_number``
    or ``context.review_loop.pr_number`` as fallback) and prices each task in LLM
    dollars using the shared :data:`LLM_TOKEN_PRICING` table + per-attempt history
    (same helpers as :func:`compute_llm_cost_breakdown` for consistency).  VM
    dollars are included via :func:`get_task_total_vm_cost_usd` alongside LLM
    dollars. Tasks with no resolvable PR number are excluded without error.

    **First-pass** vs **rework** classification:
      - A task is *first-pass* when its type is not ``"fix"`` AND its
        ``context.review_loop.iteration`` is ≤ 1 (or absent). This captures the
        originating implementation task and its first review round.
      - A task is *rework* when its type is ``"fix"`` OR its review loop
        iteration is > 1 (subsequent re-reviews). Fix tasks and re-reviews
        accumulate rework spend.

    Unpriced (provider, model) combos contribute zero LLM cost but are counted;
    chains containing any unpriced task are flagged in ``unpriced_chain_count``.
    Pure DB-free and unit-testable over synthetic task lists.

    Returns::

        {
          "total_pr_chains": int,
          "total_llm_usd": float,
          "total_vm_usd": float,
          "rework_llm_usd": float,
          "rework_vm_usd": float,
          "first_pass_llm_usd": float,
          "first_pass_vm_usd": float,
          "rework_share_of_total_llm_cost": float | None,
          "rework_share_of_total_vm_cost": float | None,
          "unpriced_chain_count": int,
          "top_rework_prs": [
            {
              "pr_number": int,
              "total_llm_usd": float,
              "total_vm_usd": float,
              "rework_llm_usd": float,
              "rework_vm_usd": float,
              "first_pass_llm_usd": float,
              "rework_share": float | None,
              "task_count": int,
              "rework_task_count": int,
              "has_unpriced": bool
            }, ...  (capped at top_n, sorted by rework_llm_usd descending)
          ]
        }
    """
    from collections import defaultdict

    # pr_number -> accumulated per-chain stats
    chains: Dict[int, Dict[str, Any]] = defaultdict(
        lambda: {
            "total_llm_usd": 0.0,
            "total_vm_usd": 0.0,
            "rework_llm_usd": 0.0,
            "rework_vm_usd": 0.0,
            "first_pass_llm_usd": 0.0,
            "first_pass_vm_usd": 0.0,
            "task_count": 0,
            "rework_task_count": 0,
            "has_unpriced": False,
        }
    )

    for t in tasks:
        pr_num = _pr_number_for_task(t)
        if pr_num is None:
            continue
        llm_cost, any_unpriced = _task_llm_cost(t)
        vm_cost = get_task_total_vm_cost_usd(t)
        is_rework = _is_rework_task(t)
        c = chains[pr_num]
        c["task_count"] += 1
        c["total_llm_usd"] += llm_cost
        c["total_vm_usd"] += vm_cost
        if is_rework:
            c["rework_task_count"] += 1
            c["rework_llm_usd"] += llm_cost
            c["rework_vm_usd"] += vm_cost
        else:
            c["first_pass_llm_usd"] += llm_cost
            c["first_pass_vm_usd"] += vm_cost
        if any_unpriced:
            c["has_unpriced"] = True

    total_llm = sum(c["total_llm_usd"] for c in chains.values())
    total_vm = sum(c["total_vm_usd"] for c in chains.values())
    rework_llm = sum(c["rework_llm_usd"] for c in chains.values())
    rework_vm = sum(c["rework_vm_usd"] for c in chains.values())
    first_pass_llm = sum(c["first_pass_llm_usd"] for c in chains.values())
    first_pass_vm = sum(c["first_pass_vm_usd"] for c in chains.values())
    unpriced_chain_count = sum(1 for c in chains.values() if c["has_unpriced"])

    rework_share_llm: float | None = (
        round(rework_llm / total_llm, 4) if total_llm > 0 else None
    )
    rework_share_vm: float | None = (
        round(rework_vm / total_vm, 4) if total_vm > 0 else None
    )

    sorted_chains = sorted(
        chains.items(), key=lambda kv: kv[1]["rework_llm_usd"], reverse=True
    )
    top_rework_prs = [
        {
            "pr_number": pr_num,
            "total_llm_usd": round(c["total_llm_usd"], 6),
            "total_vm_usd": round(c["total_vm_usd"], 6),
            "rework_llm_usd": round(c["rework_llm_usd"], 6),
            "rework_vm_usd": round(c["rework_vm_usd"], 6),
            "first_pass_llm_usd": round(c["first_pass_llm_usd"], 6),
            "rework_share": (
                round(c["rework_llm_usd"] / c["total_llm_usd"], 4)
                if c["total_llm_usd"] > 0
                else None
            ),
            "task_count": c["task_count"],
            "rework_task_count": c["rework_task_count"],
            "has_unpriced": c["has_unpriced"],
        }
        for pr_num, c in sorted_chains[:top_n]
    ]

    return {
        "total_pr_chains": len(chains),
        "total_llm_usd": round(total_llm, 6),
        "total_vm_usd": round(total_vm, 6),
        "rework_llm_usd": round(rework_llm, 6),
        "rework_vm_usd": round(rework_vm, 6),
        "first_pass_llm_usd": round(first_pass_llm, 6),
        "first_pass_vm_usd": round(first_pass_vm, 6),
        "rework_share_of_total_llm_cost": rework_share_llm,
        "rework_share_of_total_vm_cost": rework_share_vm,
        "unpriced_chain_count": unpriced_chain_count,
        "top_rework_prs": top_rework_prs,
    }


def compute_llm_cost_by_task_type(tasks: list[Task]) -> list[Dict[str, Any]]:
    """Per-task-type LLM cost/token rollup over the given tasks.

    Buckets each task by ``task.first_payload.get("type", "unspecified")`` and
    accumulates the full attempt-history token cost (same pricing table and
    same :func:`_task_llm_attempts` helper as :func:`compute_llm_cost_breakdown`
    so totals reconcile). A task that has no payload or no type key is bucketed
    as ``"unspecified"``.

    Merge attribution is TASK-level: a merged task is credited once to its
    type bucket (not once per attempt-row), so merge rates and cost-per-merge
    are consistent with the per-model breakdown.

    ``cost_per_merge`` is ``None`` for any bucket whose total cost is zero (no
    priced tasks) or that has no verified merges — avoids divide-by-zero and
    misleading zero-cost figures.

    Returns a list sorted by ``total_cost_usd`` descending so the heaviest
    spender is first.
    """
    from collections import defaultdict

    buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
            "llm_call_count": 0,
            "task_count": 0,
            "merged_count": 0,
            "verified_merged_count": 0,
            "cost_usd": 0.0,
            # num_turns is None when no task in the bucket reported it, an int otherwise.
            "num_turns": None,
            # total_tool_result_bytes is None when no task reported it (absent tolerance).
            "total_tool_result_bytes": None,
            # prompt_size_bytes is None when no task in the bucket reported it.
            # Accumulated from api_call_summary.prompt_size_bytes and attempt_history rows.
            "prompt_size_bytes": None,
            # Count of tasks in this bucket that reported a non-None prompt_size_bytes.
            # Used as the mean denominator so absent tasks don't dilute the average.
            "prompt_size_reporting_count": 0,
            # Per-section prompt size aggregation: {section_name: total_bytes} and
            # {section_name: reporting_task_count} for mean computation.
            "section_size_totals": {},  # type: dict[str, int]
            "section_size_counts": {},  # type: dict[str, int]
        }
    )
    total_cost = 0.0

    for t in tasks:
        task_type = (t.first_payload or {}).get("type") or "unspecified"
        merged = is_merged_completion(t)
        verified = is_verified_merge(t)

        bucket = buckets[task_type]
        bucket["task_count"] += 1
        if merged:
            bucket["merged_count"] += 1
        if verified:
            bucket["verified_merged_count"] += 1

        task_cost = 0.0
        for (
            provider,
            model,
            input_tokens,
            output_tokens,
            call_count,
            cache_creation,
            cache_read,
        ) in _task_llm_attempts(t):
            if input_tokens <= 0 and output_tokens <= 0:
                continue
            if not provider or not model:
                provider, model = "unattributed", "unknown"
            else:
                provider, model = provider.lower(), model.lower()

            pricing = LLM_TOKEN_PRICING.get((provider, model))
            if pricing:
                input_rate, output_rate = pricing
                cost = _price_llm_attempt(
                    input_tokens,
                    output_tokens,
                    cache_creation,
                    cache_read,
                    input_rate,
                    output_rate,
                    provider=provider,
                    model=model,
                )
            else:
                cost = 0.0

            bucket["input_tokens"] += int(input_tokens)
            bucket["cache_creation_input_tokens"] += int(cache_creation)
            bucket["cache_read_input_tokens"] += int(cache_read)
            bucket["output_tokens"] += int(output_tokens)
            bucket["llm_call_count"] += call_count
            bucket["cost_usd"] += cost
            task_cost += cost

        total_cost += task_cost

        # Accumulate num_turns from attempt_history and current api_call_summary.
        # Only counts when at least one attempt carried the field; absent = None so
        # per-turn metrics degrade gracefully for tasks that predate turn tracking.
        for attempt in t.attempt_history or []:
            turns = attempt.get("num_turns")
            if isinstance(turns, int) and turns >= 0:
                bucket["num_turns"] = (bucket["num_turns"] or 0) + turns
        current_turns = (t.api_call_summary or {}).get("num_turns")
        if isinstance(current_turns, int) and current_turns >= 0:
            bucket["num_turns"] = (bucket["num_turns"] or 0) + current_turns

        # Accumulate total_tool_result_bytes from attempt_history and api_call_summary.
        # None until at least one task reports it; absent key ≠ zero (older agents omit).
        for attempt in t.attempt_history or []:
            trb = attempt.get("total_tool_result_bytes")
            if isinstance(trb, int) and trb >= 0:
                bucket["total_tool_result_bytes"] = (bucket["total_tool_result_bytes"] or 0) + trb
        current_trb = (t.api_call_summary or {}).get("total_tool_result_bytes")
        if isinstance(current_trb, int) and current_trb >= 0:
            bucket["total_tool_result_bytes"] = (bucket["total_tool_result_bytes"] or 0) + current_trb

        # Accumulate prompt_size_bytes from attempt_history and current api_call_summary.
        # Mean is over reporting tasks only (tasks that carried prompt_size_bytes ≥ 0)
        # so absent tasks don't dilute the average; absent buckets yield None.
        task_prompt_size: int | None = None
        for attempt in t.attempt_history or []:
            ps = attempt.get("prompt_size_bytes")
            if isinstance(ps, int) and ps >= 0:
                task_prompt_size = (task_prompt_size or 0) + ps
        current_ps = (t.api_call_summary or {}).get("prompt_size_bytes")
        if isinstance(current_ps, int) and current_ps >= 0:
            task_prompt_size = (task_prompt_size or 0) + current_ps
        if task_prompt_size is not None:
            bucket["prompt_size_bytes"] = (bucket["prompt_size_bytes"] or 0) + task_prompt_size
            bucket["prompt_size_reporting_count"] += 1

        # Accumulate prompt_section_sizes from the same sources as prompt_size_bytes.
        # Per-task aggregation (across attempt_history + api_call_summary), then
        # per-section totals and counts for mean denominator.
        task_sections: dict[str, int] = {}
        for attempt in t.attempt_history or []:
            pss = attempt.get("prompt_section_sizes")
            if isinstance(pss, list):
                for s in pss:
                    _n, _b = s.get("name"), s.get("bytes")
                    if isinstance(_n, str) and isinstance(_b, int) and _b >= 0:
                        task_sections[_n] = task_sections.get(_n, 0) + _b
        cur_pss = (t.api_call_summary or {}).get("prompt_section_sizes")
        if isinstance(cur_pss, list):
            for s in cur_pss:
                _n, _b = s.get("name"), s.get("bytes")
                if isinstance(_n, str) and isinstance(_b, int) and _b >= 0:
                    task_sections[_n] = task_sections.get(_n, 0) + _b
        if task_sections:
            for _n, _b in task_sections.items():
                bucket["section_size_totals"][_n] = (
                    bucket["section_size_totals"].get(_n) or 0
                ) + _b
                bucket["section_size_counts"][_n] = (
                    bucket["section_size_counts"].get(_n) or 0
                ) + 1

    def _share(cost: float) -> float | None:
        if total_cost <= 0:
            return None
        return round(cost / total_cost, 4)

    def _merge_rate(merged: float, count: float) -> float | None:
        if count <= 0:
            return None
        return round(merged / count, 4)

    def _cost_per_merge(cost: float, verified_merged: float) -> float | None:
        if cost <= 0 or verified_merged <= 0:
            return None
        return round(cost / verified_merged, 6)

    rows = [
        {
            "task_type": task_type,
            "total_input_tokens": int(b["input_tokens"]),
            "total_cache_creation_input_tokens": int(b["cache_creation_input_tokens"]),
            "total_cache_read_input_tokens": int(b["cache_read_input_tokens"]),
            "total_output_tokens": int(b["output_tokens"]),
            "total_cost_usd": round(b["cost_usd"], 6),
            "llm_call_count": int(b["llm_call_count"]),
            "task_count": int(b["task_count"]),
            "merged_count": int(b["merged_count"]),
            "merge_rate": _merge_rate(b["merged_count"], b["task_count"]),
            "verified_merged_count": int(b["verified_merged_count"]),
            "verified_merge_rate": _merge_rate(
                b["verified_merged_count"], b["task_count"]
            ),
            "cost_per_merge": _cost_per_merge(
                b["cost_usd"], b["verified_merged_count"]
            ),
            "share_of_total_cost": _share(b["cost_usd"]),
            "mean_input_tokens_per_call": (
                round(b["input_tokens"] / b["llm_call_count"])
                if b["llm_call_count"] > 0
                else None
            ),
            # Per-turn metrics — None when no task in this bucket reported num_turns,
            # or when the denominator is zero (absent-counter tolerant).
            "mean_turns_per_call": (
                round(b["num_turns"] / b["llm_call_count"], 2)
                if b["num_turns"] is not None and b["llm_call_count"] > 0
                else None
            ),
            "mean_input_tokens_per_turn": (
                round(b["input_tokens"] / b["num_turns"])
                if b["num_turns"] is not None and b["num_turns"] > 0
                else None
            ),
            "mean_uncached_input_tokens_per_call": (
                round(
                    (b["input_tokens"] - b["cache_creation_input_tokens"] - b["cache_read_input_tokens"])
                    / b["llm_call_count"]
                )
                if b["llm_call_count"] > 0
                else None
            ),
            "cache_read_share": (
                round(b["cache_read_input_tokens"] / b["input_tokens"], 4)
                if b["input_tokens"] > 0
                else None
            ),
            # Tool-result bytes — None when no task in this bucket reported the field.
            "total_tool_result_bytes": b["total_tool_result_bytes"],
            "mean_tool_result_bytes_per_task": (
                round(b["total_tool_result_bytes"] / b["task_count"])
                if b["total_tool_result_bytes"] is not None and b["task_count"] > 0
                else None
            ),
            # Prompt-size metrics — None when no task in this bucket reported
            # prompt_size_bytes. Mean denominator is the reporting-task count
            # (tasks that carried a non-None prompt_size_bytes), not the total
            # task count, so absent tasks do not dilute the average.
            "total_prompt_size_bytes": b["prompt_size_bytes"],
            "mean_prompt_size_bytes": (
                round(b["prompt_size_bytes"] / b["prompt_size_reporting_count"])
                if b["prompt_size_bytes"] is not None and b["prompt_size_reporting_count"] > 0
                else None
            ),
            # Per-section mean bytes — None when no task in this bucket reported
            # prompt_section_sizes. Each section's mean divides by the count of
            # tasks that reported that specific section.
            "mean_section_sizes": (
                {
                    name: round(
                        b["section_size_totals"][name] / b["section_size_counts"][name]
                    )
                    for name in b["section_size_totals"]
                }
                if b["section_size_totals"]
                else None
            ),
        }
        for task_type, b in sorted(
            buckets.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True
        )
    ]
    return rows


def _compute_llm_cost_pricing_coverage(
    tasks: list[Task],
) -> Dict[str, Any]:
    """Coverage diagnostic: how many token-bearing attempt rows have pricing.

    An attempt is "priceable" when it has non-zero tokens AND a non-null
    (provider, model). A priceable attempt is "priced" when (provider, model)
    is in LLM_TOKEN_PRICING; otherwise it is "unpriced" and silently
    contributes zero to llm_cost_per_merged_pr. Token-bearing attempts with
    missing provider or model are counted explicitly as unattributed.
    """
    priced = 0
    unpriced = 0
    unattributed = 0
    unpriced_combos: list[str] = []
    for t in tasks:
        for (
            provider,
            model,
            input_tokens,
            output_tokens,
            _call_count,
            _cache_creation,
            _cache_read,
        ) in _task_llm_attempts(t):
            if input_tokens <= 0 and output_tokens <= 0:
                continue
            if not provider or not model:
                unattributed += 1
                continue
            provider = provider.lower()
            model = model.lower()
            key = (provider, model)
            if key in LLM_TOKEN_PRICING:
                priced += 1
            else:
                unpriced += 1
                combo = f"{provider}:{model}"
                if (
                    combo not in unpriced_combos
                    and len(unpriced_combos) < _UNPRICED_COMBOS_CAP
                ):
                    unpriced_combos.append(combo)
    priceable = priced + unpriced + unattributed
    coverage_fraction: float | None = (
        round(priced / priceable, 4) if priceable else None
    )
    return {
        "priced_tasks": priced,
        "unpriced_tasks": unpriced,
        "unattributed_tasks": unattributed,
        "coverage_fraction": coverage_fraction,
        "unpriced_combos": unpriced_combos,
    }


def compute_llm_cost_by_failure_class(tasks: list[Task]) -> list[Dict[str, Any]]:
    """Per-failure-class LLM cost rollup over the given tasks.

    Buckets each task by ``task.context['failure_category']`` for failed/cancelled
    tasks, or ``'MERGED'`` for tasks that landed on the integration branch.
    Tasks with no failure_category and no merge signal are bucketed as ``'OTHER'``.

    Returns a list sorted by ``total_cost_usd`` descending so the most expensive
    failure class is first. Each row carries task_count alongside cost so operators
    can distinguish high-volume cheap classes from low-volume expensive ones.
    """
    from collections import defaultdict

    buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"task_count": 0, "cost_usd": 0.0}
    )

    for t in tasks:
        llm_cost, _ = _task_llm_cost(t)
        if is_merged_completion(t):
            failure_class = "MERGED"
        elif t.status == TaskStatus.COMPLETED:
            # COMPLETED but no merged PR — legitimate non-PR task types (research,
            # review, memo, knowledge, strategic). Separate from undiagnosed failures.
            failure_class = "COMPLETED_NO_PR"
        else:
            failure_class = (t.context or {}).get("failure_category") or "OTHER"
        b = buckets[failure_class]
        b["task_count"] += 1
        b["cost_usd"] += llm_cost

    return [
        {
            "failure_class": fc,
            "task_count": b["task_count"],
            "total_cost_usd": round(b["cost_usd"], 6),
        }
        for fc, b in sorted(
            buckets.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True
        )
    ]


def _compact_llm_cost_attribution(
    tasks: list[Task],
) -> Dict[str, Any]:
    """Compact per-driver LLM cost breakdown for durable daily snapshot storage.

    Calls the three existing pure helpers (compute_llm_cost_breakdown,
    compute_llm_cost_by_task_type, compute_pr_rework_chain_breakdown) and keeps
    only the dollar-total fields needed for retroactive drift decomposition:
    per-(provider,model) cost, per-task-type cost, and the first-pass/rework
    split.  Priced rows only in by_model so zero-cost unpriced combos don't
    inflate the block; unpriced_coverage carries the honesty signal.

    self_repair_burn_share is computed as a token-cost ratio when repair token
    data is available (format_repair_input_tokens / format_repair_output_tokens
    from format_compliance_summary): repair_token_count / total_token_count,
    which is a cost-correlated fraction assuming consistent per-token pricing
    across primary and repair calls. Falls back to the call-count ratio
    (repair_attempts / total_llm_call_count) for tasks that predate repair-token
    tracking. None when the denominator is zero.
    """
    from app.digest_metrics import _accumulate_format_compliance

    breakdown = compute_llm_cost_breakdown(tasks)
    by_type_rows = compute_llm_cost_by_task_type(tasks)
    rework = compute_pr_rework_chain_breakdown(tasks)
    fc = _accumulate_format_compliance(tasks)

    total_usd = round(breakdown["totals"]["total_cost_usd"], 6)
    by_model = [
        {
            "provider": r["provider"],
            "model": r["model"],
            "cost_usd": r["total_cost_usd"],
        }
        for r in breakdown["by_model"]
        if r.get("priced") and r["total_cost_usd"] > 0
    ]
    by_task_type = [
        {
            "task_type": r["task_type"],
            "cost_usd": r["total_cost_usd"],
            "mean_input_tokens_per_call": r.get("mean_input_tokens_per_call"),
            "total_tool_result_bytes": r.get("total_tool_result_bytes"),
            "total_prompt_size_bytes": r.get("total_prompt_size_bytes"),
            "mean_prompt_size_bytes": r.get("mean_prompt_size_bytes"),
            "mean_section_sizes": r.get("mean_section_sizes"),
        }
        for r in by_type_rows
        if r["total_cost_usd"] > 0
    ]

    # Prefer token-cost ratio when repair token data is present; fall back to
    # call-count ratio for tasks that predate repair-token tracking.
    # _accumulate_format_compliance always emits repair_input_tokens as int 0
    # when no task carried token fields, so None-check is insufficient —
    # distinguish by whether any repair tokens were actually recorded.
    repair_token_count = (fc.get("repair_input_tokens") or 0) + (
        fc.get("repair_output_tokens") or 0
    )
    repair_attempts = fc.get("repair_attempts") or 0
    if repair_token_count > 0 or repair_attempts == 0:
        # Token-weighted burn share: repair tokens / total fleet tokens.
        # Uses combined input+output tokens as the denominator — a cost-correlated
        # proxy that is accurate for homogeneous-model fleets.
        total_tokens = (
            breakdown["totals"]["total_input_tokens"]
            + breakdown["totals"]["total_output_tokens"]
        )
        self_repair_burn_share: float | None = (
            round(repair_token_count / total_tokens, 4) if total_tokens > 0 else None
        )
    else:
        # Legacy fallback: fraction of LLM calls that are repair reopens.
        # Applies when repair attempts exist but no token data (pre-token-tracking tasks).
        total_llm_calls = sum(r["llm_call_count"] for r in breakdown["by_provider"])
        self_repair_burn_share = (
            round(repair_attempts / total_llm_calls, 4) if total_llm_calls > 0 else None
        )

    # Strategic-cycle cost per event: aggregate LLM cost of design_doc_update
    # and strategic_review tasks, grouped by distinct strategic_cycle_id.
    # Operators use this to distinguish "tasks are more expensive per merge"
    # from "strategic cycles are running more often or with larger contexts".
    _STRATEGIC_CYCLE_TYPES = {"design_doc_update", "strategic_review"}
    strategic_tasks = [
        t
        for t in tasks
        if (t.first_payload or {}).get("type") in _STRATEGIC_CYCLE_TYPES
    ]
    cycle_ids = {t.strategic_cycle_id for t in strategic_tasks if t.strategic_cycle_id}
    cycle_count = len(cycle_ids)
    total_strategic_usd = round(sum(_task_llm_cost(t)[0] for t in strategic_tasks), 6)
    strategic_cycle_cost_per_event: Dict[str, Any] | None = (
        {
            "cycle_count": cycle_count,
            "total_usd": total_strategic_usd,
            "per_event_usd": round(total_strategic_usd / cycle_count, 6)
            if cycle_count > 0
            else None,
        }
        if cycle_count > 0
        else None
    )

    by_failure_class = compute_llm_cost_by_failure_class(tasks)
    merged_count = sum(
        r["task_count"] for r in by_failure_class if r["failure_class"] == "MERGED"
    )

    return {
        "by_model": by_model,
        "by_task_type": by_task_type,
        "by_failure_class": by_failure_class,
        "merged_count": merged_count,
        "rework_share": rework.get("rework_share_of_total_llm_cost"),
        "total_llm_usd": total_usd,
        "unpriced_coverage": breakdown["totals"].get("unpriced_coverage_fraction"),
        "unpriced_combos": breakdown.get("unpriced_combos", []),
        SELF_REPAIR_BURN_SHARE_KEY: self_repair_burn_share,
        "strategic_cycle_cost_per_event": strategic_cycle_cost_per_event,
        "total_tool_result_bytes": breakdown["totals"].get("total_tool_result_bytes"),
    }
