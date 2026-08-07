"""Tests for DeepSeek provider cost attribution accuracy.

Context: The operator reported real DeepSeek billing of ~$55.74/day
(2.6B flash + 1.2B pro tokens, 95–97% cache-hit) while Blueprint's
internal llm_cost_attribution showed only ~$1.66 over 7 days — a 235x
gap. These tests verify:

1. Pricing constants match the exact billing rates in the DeepSeek API docs.
2. Cache-hit pricing path is exercised for DeepSeek (flash cache-read at
   $0.0028/M vs base $0.14/M — a 50x discount).
3. Model-name mismatches silently produce $0 cost and appear in
   unpriced_combos (the most likely cause of the 235x gap).
4. Banked (multi-attempt) token sums accumulate correctly.
5. Provider-reported reconciliation skips DeepSeek (non-Anthropic provider)
   so its token counts are entirely self-reported.
6. compute_llm_cost_breakdown correctly buckets DeepSeek tasks.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.llm_cost_attribution import (
    LLM_CACHE_READ_INPUT_PRICING,
    LLM_TOKEN_PRICING,
    _price_llm_attempt,
    _task_llm_attempts,
    compute_llm_cost_breakdown,
    _compute_llm_cost_pricing_coverage,
)
from app.models import TaskStatus, bank_task_current_attempt
from testing.factories import make_task


# ── Constants pulled from operator billing report ──────────────────────────

# Flash: $0.14/M input (cache-miss), $0.28/M output, $0.0028/M cache-read
_FLASH_INPUT_RATE = 0.14e-6
_FLASH_OUTPUT_RATE = 0.28e-6
_FLASH_CACHE_READ_RATE = 0.0028e-6

# Pro: $0.435/M input (cache-miss), $0.87/M output, $0.003625/M cache-read
_PRO_INPUT_RATE = 0.435e-6
_PRO_OUTPUT_RATE = 0.87e-6
_PRO_CACHE_READ_RATE = 0.003625e-6


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_deepseek_task(model: str, input_tokens: int, output_tokens: int,
                        cache_creation: int = 0, cache_read: int = 0,
                        provider: str = "deepseek", **kw) -> object:
    """Create a DeepSeek task with the given token counts in api_call_summary."""
    summary: dict = {
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
        "llm_call_count": 5,
    }
    if cache_creation:
        summary["total_cache_creation_input_tokens"] = cache_creation
    if cache_read:
        summary["total_cache_read_input_tokens"] = cache_read
    return make_task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary=summary,
        provider=provider,
        model=model,
        **kw,
    )


# ── 1. Pricing constants match billing rates ───────────────────────────────


def test_deepseek_flash_pricing_constants_match_billing_rates():
    """LLM_TOKEN_PRICING for deepseek-v4-flash must match the operator's real
    billing rates exactly (cache-miss $0.14/M input, $0.28/M output)."""
    key = ("deepseek", "deepseek-v4-flash")
    assert key in LLM_TOKEN_PRICING, (
        "deepseek-v4-flash missing from LLM_TOKEN_PRICING — "
        "cost attribution would silently zero-cost every flash task"
    )
    input_rate, output_rate = LLM_TOKEN_PRICING[key]
    assert input_rate == pytest.approx(_FLASH_INPUT_RATE, rel=1e-9), (
        f"Flash input rate {input_rate} != expected {_FLASH_INPUT_RATE}"
    )
    assert output_rate == pytest.approx(_FLASH_OUTPUT_RATE, rel=1e-9), (
        f"Flash output rate {output_rate} != expected {_FLASH_OUTPUT_RATE}"
    )


def test_deepseek_pro_pricing_constants_match_billing_rates():
    """LLM_TOKEN_PRICING for deepseek-v4-pro must match billing ($0.435/M
    cache-miss input, $0.87/M output)."""
    key = ("deepseek", "deepseek-v4-pro")
    assert key in LLM_TOKEN_PRICING, (
        "deepseek-v4-pro missing from LLM_TOKEN_PRICING"
    )
    input_rate, output_rate = LLM_TOKEN_PRICING[key]
    assert input_rate == pytest.approx(_PRO_INPUT_RATE, rel=1e-9)
    assert output_rate == pytest.approx(_PRO_OUTPUT_RATE, rel=1e-9)


def test_deepseek_flash_cache_read_rate_matches_billing():
    """LLM_CACHE_READ_INPUT_PRICING for deepseek-v4-flash must be $0.0028/M —
    50x cheaper than the $0.14/M cache-miss rate."""
    key = ("deepseek", "deepseek-v4-flash")
    assert key in LLM_CACHE_READ_INPUT_PRICING, (
        "deepseek-v4-flash missing from LLM_CACHE_READ_INPUT_PRICING — "
        "cache-hit tokens would be priced at base rate ($0.14/M) instead of "
        "$0.0028/M, overstating cost 50x"
    )
    rate = LLM_CACHE_READ_INPUT_PRICING[key]
    assert rate == pytest.approx(_FLASH_CACHE_READ_RATE, rel=1e-9)
    # Cache-read is 50x cheaper than base input — confirm the ratio.
    base_input_rate, _ = LLM_TOKEN_PRICING[key]
    ratio = base_input_rate / rate
    assert ratio == pytest.approx(50.0, rel=0.01), (
        f"Flash cache-read discount ratio {ratio:.1f}x (expected ~50x)"
    )


def test_deepseek_pro_cache_read_rate_matches_billing():
    """LLM_CACHE_READ_INPUT_PRICING for deepseek-v4-pro must be $0.003625/M."""
    key = ("deepseek", "deepseek-v4-pro")
    assert key in LLM_CACHE_READ_INPUT_PRICING
    rate = LLM_CACHE_READ_INPUT_PRICING[key]
    assert rate == pytest.approx(_PRO_CACHE_READ_RATE, rel=1e-9)


# ── 2. Cache pricing path exercised for DeepSeek ──────────────────────────


def test_deepseek_flash_cache_read_uses_low_rate_not_base_rate():
    """_price_llm_attempt routes DeepSeek flash to LLM_CACHE_READ_INPUT_PRICING
    rather than the fallback `cache_read_rate = input_rate` branch."""
    input_rate, output_rate = LLM_TOKEN_PRICING[("deepseek", "deepseek-v4-flash")]
    cache_read_rate = LLM_CACHE_READ_INPUT_PRICING[("deepseek", "deepseek-v4-flash")]

    # 1M tokens all as cache-read input — no uncached, no output.
    cost_with_cache = _price_llm_attempt(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=1_000_000,
        input_rate=input_rate,
        output_rate=output_rate,
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    expected_with_cache = 1_000_000 * cache_read_rate
    assert cost_with_cache == pytest.approx(expected_with_cache, rel=1e-9), (
        "DeepSeek cache-read tokens must use the $0.0028/M rate, not $0.14/M"
    )

    # Without cache routing — treat all as uncached input.
    cost_without_cache = _price_llm_attempt(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        input_rate=input_rate,
        output_rate=output_rate,
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    # Cache-read should be ~50x cheaper.
    ratio = cost_without_cache / cost_with_cache
    assert ratio == pytest.approx(50.0, rel=0.01), (
        f"Expected ~50x cost reduction for cache-read vs uncached; got {ratio:.1f}x"
    )


def test_deepseek_flash_97pct_cache_hit_cost_far_below_full_price():
    """At 97% cache-hit (operator-reported), per-token cost must be dramatically
    lower than the base $0.14/M rate — this is the primary lever that makes
    3.87B tokens/day cost ~$55/day rather than thousands."""
    input_rate, output_rate = LLM_TOKEN_PRICING[("deepseek", "deepseek-v4-flash")]

    total_input = 2_633_500_000  # operator-reported flash tokens/day
    cache_read = int(total_input * 0.97)

    cost_with_cache_hits = _price_llm_attempt(
        input_tokens=total_input,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=cache_read,
        input_rate=input_rate,
        output_rate=output_rate,
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    # Without cache sub-counts — all input priced at base rate.
    cost_without_cache_tracking = _price_llm_attempt(
        input_tokens=total_input,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        input_rate=input_rate,
        output_rate=output_rate,
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    # Cache-aware pricing must be much cheaper (97% of tokens at 50x discount).
    # Effective ratio: uncached (3%) at full price + cache (97%) at 1/50th price.
    # = (0.03 * 1 + 0.97 * 0.02) / 1 = (0.03 + 0.0194) / 1 ≈ 0.0494 of full cost.
    assert cost_with_cache_hits < cost_without_cache_tracking * 0.10, (
        "97% cache-hit at $0.0028/M should cost <10% of uncached-only pricing at $0.14/M"
    )
    # Sanity: must be positive.
    assert cost_with_cache_hits > 0


def test_deepseek_pro_cache_read_pricing_applied():
    """_price_llm_attempt uses the pro cache-read rate for deepseek-v4-pro."""
    input_rate, output_rate = LLM_TOKEN_PRICING[("deepseek", "deepseek-v4-pro")]
    cache_read_rate = LLM_CACHE_READ_INPUT_PRICING[("deepseek", "deepseek-v4-pro")]

    cost = _price_llm_attempt(
        input_tokens=500_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=500_000,
        input_rate=input_rate,
        output_rate=output_rate,
        provider="deepseek",
        model="deepseek-v4-pro",
    )
    expected = 500_000 * cache_read_rate
    assert cost == pytest.approx(expected, rel=1e-9)


# ── 3. Model-name mismatch silently zeroes cost ───────────────────────────


def test_deepseek_wrong_model_name_produces_zero_cost_in_breakdown():
    """A DeepSeek task whose recorded model string doesn't match any pricing key
    silently produces $0 cost. This is the most likely cause of the 235x gap:
    if the agent writes back a different model string than what was dispatched,
    every token is zero-costed."""
    # 'deepseek-v4' has no entry in LLM_TOKEN_PRICING.
    task = _make_deepseek_task(
        model="deepseek-v4",  # wrong — should be 'deepseek-v4-flash'
        input_tokens=10_000_000,
        output_tokens=2_000_000,
    )
    result = compute_llm_cost_breakdown([task])

    # Cost is silently $0.
    assert result["totals"]["total_cost_usd"] == 0.0, (
        "Wrong model name must silently zero-cost the task "
        "(no exception, no log — just a $0 bucket)"
    )
    # Wrong model string appears in unpriced_combos — the diagnostic signal.
    assert "deepseek:deepseek-v4" in result["unpriced_combos"], (
        "Unpriced (provider, model) combo must surface in unpriced_combos "
        "so operators can detect missing pricing entries"
    )


def test_deepseek_chat_model_alias_zero_cost():
    """'deepseek-chat' is a common API alias that maps to the v4-flash model on
    DeepSeek's end but has no entry in Blueprint's pricing table. Tasks recorded
    with this alias silently accumulate zero cost."""
    task = _make_deepseek_task(
        model="deepseek-chat",
        input_tokens=5_000_000,
        output_tokens=1_000_000,
    )
    result = compute_llm_cost_breakdown([task])

    assert result["totals"]["total_cost_usd"] == 0.0
    assert "deepseek:deepseek-chat" in result["unpriced_combos"]


def test_deepseek_wrong_provider_silences_cost():
    """If the task is recorded under provider='anthropic' but model='deepseek-v4-flash',
    the pricing lookup ('anthropic', 'deepseek-v4-flash') has no entry → $0 cost."""
    task = _make_deepseek_task(
        provider="anthropic",  # wrong provider
        model="deepseek-v4-flash",
        input_tokens=5_000_000,
        output_tokens=1_000_000,
    )
    result = compute_llm_cost_breakdown([task])

    assert result["totals"]["total_cost_usd"] == 0.0


def test_deepseek_model_name_case_insensitive_lookup():
    """Both paths (snapshot and direct) lowercase (provider, model) before
    the pricing lookup — 'DeepSeek' / 'DeepSeek-V4-Flash' must price correctly."""
    task = _make_deepseek_task(
        provider="DeepSeek",  # uppercase
        model="DeepSeek-V4-Flash",  # mixed case
        input_tokens=1_000_000,
        output_tokens=500_000,
    )
    result = compute_llm_cost_breakdown([task])

    # Must NOT be zero — case normalization must happen before the lookup.
    expected_min_cost = 1_000_000 * _FLASH_INPUT_RATE + 500_000 * _FLASH_OUTPUT_RATE
    assert result["totals"]["total_cost_usd"] == pytest.approx(expected_min_cost, rel=1e-6), (
        "Case-insensitive normalization must ensure 'DeepSeek-V4-Flash' prices correctly"
    )


def test_deepseek_correct_model_name_is_priced():
    """A task with the canonical model name 'deepseek-v4-flash' is priced and
    does NOT appear in unpriced_combos."""
    task = _make_deepseek_task(
        model="deepseek-v4-flash",
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    result = compute_llm_cost_breakdown([task])

    assert result["totals"]["total_cost_usd"] > 0
    assert "deepseek:deepseek-v4-flash" not in result["unpriced_combos"]
    # Must appear in by_model as a priced entry.
    flash_row = next(
        (r for r in result["by_model"] if r["model"] == "deepseek-v4-flash"), None
    )
    assert flash_row is not None
    assert flash_row["priced"] is True


# ── 4. Banked (multi-attempt) token aggregation ───────────────────────────


def test_deepseek_banked_attempt_tokens_included_in_cost():
    """After bank_task_current_attempt(), the attempt's tokens appear in
    _task_llm_attempts and contribute to the cost breakdown."""
    task = make_task(
        status=TaskStatus.IN_PROGRESS,
        api_call_summary={
            "total_input_tokens": 2_000_000,
            "total_output_tokens": 500_000,
            "total_cache_creation_input_tokens": 200_000,
            "total_cache_read_input_tokens": 1_500_000,
            "llm_call_count": 10,
        },
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    # Bank the attempt — simulates what happens when a task is retried.
    bank_task_current_attempt(task, stopped_at=_utcnow())

    attempts = _task_llm_attempts(task)
    # The banked attempt must be present.
    assert len(attempts) == 1
    provider, model, inp, out, calls, cc, cr = attempts[0]
    assert provider == "deepseek"
    assert model == "deepseek-v4-flash"
    assert inp == 2_000_000
    assert out == 500_000
    assert cr == 1_500_000

    # Cost must be non-zero.
    result = compute_llm_cost_breakdown([task])
    assert result["totals"]["total_cost_usd"] > 0


def test_deepseek_multi_attempt_task_sums_all_tokens():
    """A task with two banked attempts + one current attempt must sum all
    three attempt rows (not just the last)."""
    task = make_task(
        status=TaskStatus.IN_PROGRESS,
        api_call_summary={
            "total_input_tokens": 1_000_000,
            "total_output_tokens": 200_000,
            "llm_call_count": 5,
        },
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    now = _utcnow()

    # Bank first attempt.
    bank_task_current_attempt(task, stopped_at=now)

    # Set up second attempt.
    task.api_call_summary = {
        "total_input_tokens": 1_500_000,
        "total_output_tokens": 300_000,
        "total_cache_read_input_tokens": 1_200_000,
        "llm_call_count": 7,
    }
    # Bank second attempt.
    bank_task_current_attempt(task, stopped_at=now)

    # Set up current (third) attempt.
    task.api_call_summary = {
        "total_input_tokens": 800_000,
        "total_output_tokens": 100_000,
        "llm_call_count": 3,
    }

    attempts = _task_llm_attempts(task)
    # All three attempts returned.
    assert len(attempts) == 3

    total_input = sum(a[2] for a in attempts)
    total_output = sum(a[3] for a in attempts)
    assert total_input == 3_300_000  # 1M + 1.5M + 0.8M
    assert total_output == 600_000   # 200k + 300k + 100k

    result = compute_llm_cost_breakdown([task])
    assert result["totals"]["total_input_tokens"] == 3_300_000
    assert result["totals"]["total_output_tokens"] == 600_000
    assert result["totals"]["total_cost_usd"] > 0


def test_deepseek_banked_attempt_cache_subcounts_preserved():
    """Cache sub-counts in the live attempt are carried into the banked snapshot
    and used in pricing, not dropped."""
    task = make_task(
        status=TaskStatus.IN_PROGRESS,
        api_call_summary={
            "total_input_tokens": 5_000_000,
            "total_output_tokens": 500_000,
            "total_cache_creation_input_tokens": 100_000,
            "total_cache_read_input_tokens": 4_500_000,  # 90% cache hit
            "llm_call_count": 20,
        },
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    bank_task_current_attempt(task, stopped_at=_utcnow())

    attempts = _task_llm_attempts(task)
    assert len(attempts) == 1
    _provider, _model, inp, _out, _calls, cc, cr = attempts[0]
    assert cc == 100_000
    assert cr == 4_500_000

    # Verify pricing uses the discounted cache-read rate.
    input_rate, output_rate = LLM_TOKEN_PRICING[("deepseek", "deepseek-v4-flash")]
    cache_read_rate = LLM_CACHE_READ_INPUT_PRICING[("deepseek", "deepseek-v4-flash")]

    expected_cost = (
        (5_000_000 - 100_000 - 4_500_000) * input_rate  # uncached
        + 100_000 * input_rate                           # cache creation at base rate
        + 4_500_000 * cache_read_rate                    # cache reads at discount
        + 500_000 * output_rate
    )
    result = compute_llm_cost_breakdown([task])
    assert result["totals"]["total_cost_usd"] == pytest.approx(expected_cost, rel=1e-6)


def test_deepseek_attempt_history_provider_model_used_for_banked_rows():
    """Banked attempt rows carry their own provider/model snapshot. If the task
    later switches provider/model (e.g., fallback to a different model), the
    banked row still prices under the original provider/model."""
    task = make_task(
        status=TaskStatus.IN_PROGRESS,
        api_call_summary={
            "total_input_tokens": 2_000_000,
            "total_output_tokens": 300_000,
            "llm_call_count": 8,
        },
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    bank_task_current_attempt(task, stopped_at=_utcnow())

    # Simulate task switching to a different model on retry.
    task.provider = "anthropic"
    task.model = "claude-sonnet-4-6"
    task.api_call_summary = {
        "total_input_tokens": 1_000_000,
        "total_output_tokens": 200_000,
        "llm_call_count": 4,
    }

    attempts = _task_llm_attempts(task)
    assert len(attempts) == 2
    # First (banked) attempt was deepseek.
    assert attempts[0][0] == "deepseek"
    assert attempts[0][1] == "deepseek-v4-flash"
    # Current attempt is anthropic.
    assert attempts[1][0] == "anthropic"
    assert attempts[1][1] == "claude-sonnet-4-6"

    result = compute_llm_cost_breakdown([task])
    # Both must be priced — two distinct entries in by_model.
    model_names = {r["model"] for r in result["by_model"]}
    assert "deepseek-v4-flash" in model_names
    assert "claude-sonnet-4-6" in model_names


# ── 5. Compute breakdown buckets DeepSeek correctly ───────────────────────


def test_deepseek_appears_in_by_model_and_by_provider():
    """compute_llm_cost_breakdown must include DeepSeek tasks in both
    by_model (per model) and by_provider (per provider) rollups."""
    task = _make_deepseek_task(
        model="deepseek-v4-flash",
        input_tokens=3_000_000,
        output_tokens=600_000,
        cache_read=2_700_000,
    )
    result = compute_llm_cost_breakdown([task])

    # by_provider should have a 'deepseek' entry.
    providers = {r["provider"] for r in result["by_provider"]}
    assert "deepseek" in providers

    # by_model should have deepseek-v4-flash.
    flash_row = next(
        (r for r in result["by_model"] if r["model"] == "deepseek-v4-flash"), None
    )
    assert flash_row is not None
    assert flash_row["provider"] == "deepseek"
    assert flash_row["total_input_tokens"] == 3_000_000
    assert flash_row["total_cache_read_input_tokens"] == 2_700_000
    assert flash_row["priced"] is True
    assert flash_row["total_cost_usd"] > 0


def test_deepseek_flash_and_pro_in_separate_model_buckets():
    """Flash and pro tasks appear as distinct model rows in by_model, not merged."""
    flash_task = _make_deepseek_task(
        model="deepseek-v4-flash",
        input_tokens=2_000_000,
        output_tokens=400_000,
    )
    pro_task = _make_deepseek_task(
        model="deepseek-v4-pro",
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    result = compute_llm_cost_breakdown([flash_task, pro_task])

    model_rows = {r["model"]: r for r in result["by_model"]}
    assert "deepseek-v4-flash" in model_rows
    assert "deepseek-v4-pro" in model_rows

    # They must price at different rates — pro costs more per token.
    flash_cost_per_input = (
        model_rows["deepseek-v4-flash"]["total_cost_usd"]
        / model_rows["deepseek-v4-flash"]["total_input_tokens"]
    )
    pro_cost_per_input = (
        model_rows["deepseek-v4-pro"]["total_cost_usd"]
        / model_rows["deepseek-v4-pro"]["total_input_tokens"]
    )
    # Pro ($0.435/M) is more expensive per token than flash ($0.14/M).
    assert pro_cost_per_input > flash_cost_per_input


def test_deepseek_token_counts_accurate_in_breakdown():
    """Token sub-counts in compute_llm_cost_breakdown must match the input."""
    cache_creation = 500_000
    cache_read = 8_000_000
    total_input = 10_000_000
    output = 1_500_000

    task = _make_deepseek_task(
        model="deepseek-v4-flash",
        input_tokens=total_input,
        output_tokens=output,
        cache_creation=cache_creation,
        cache_read=cache_read,
    )
    result = compute_llm_cost_breakdown([task])

    totals = result["totals"]
    assert totals["total_input_tokens"] == total_input
    assert totals["total_cache_creation_input_tokens"] == cache_creation
    assert totals["total_cache_read_input_tokens"] == cache_read
    assert totals["total_output_tokens"] == output


# ── 6. Provider-reported reconciliation skips DeepSeek ───────────────────


def test_deepseek_skipped_by_provider_reported_reconciliation_without_data():
    """DeepSeek tasks without provider_reported_usage are skipped (task_count=0).

    The module docstring explicitly states: 'OpenAI, Gemini, DeepSeek, and Z.AI
    do not expose per-call usage probes.' Skipping is correct; the real gap is
    that there is NO independent verification of DeepSeek token counts at all.
    """
    from app.cost_telemetry_reconciliation import reconcile_provider_reported_usage

    task = _make_deepseek_task(
        model="deepseek-v4-flash",
        input_tokens=5_000_000,
        output_tokens=1_000_000,
    )
    # No provider_reported_usage (typical for non-Anthropic tasks).
    task.provider_reported_usage = None

    result = reconcile_provider_reported_usage([task])

    assert result.source == "provider_reported"
    assert result.task_count == 0, (
        "DeepSeek tasks without provider_reported_usage must be skipped — "
        "their token counts are entirely self-reported by the agent"
    )
    # Zero checked tasks → tolerance verdict is unproven, not passing (a green
    # verdict from zero evidence is exactly the false confidence this check
    # exists to prevent).
    assert result.provider_reported_within_tolerance is False
    assert result.provider_reported_verified is False


def test_deepseek_with_provider_reported_usage_is_checked():
    """If a DeepSeek task does carry provider_reported_usage (possible because
    DeepSeek uses an Anthropic-compatible endpoint), the reconciler DOES check
    it — the skip is data-driven, not provider-name-based."""
    from app.cost_telemetry_reconciliation import reconcile_provider_reported_usage

    task = _make_deepseek_task(
        model="deepseek-v4-flash",
        input_tokens=5_000_000,
        output_tokens=1_000_000,
        cache_creation=200_000,
        cache_read=4_500_000,
    )
    # Matching provider_reported_usage — should pass reconciliation.
    task.provider_reported_usage = {
        "input_tokens": 300_000,        # uncached
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 200_000,
        "cache_read_input_tokens": 4_500_000,
    }

    result = reconcile_provider_reported_usage([task])

    assert result.task_count == 1, (
        "DeepSeek task WITH provider_reported_usage must be checked, not skipped"
    )
    # Provider total = 300k + 200k + 4500k = 5000k = 5M; agent accumulated = 5M → match.
    assert result.provider_reported_within_tolerance is True


def test_deepseek_provider_reported_detects_deepseek_token_divergence():
    """If agent accumulated more tokens than the provider-reported sum, the
    reconciler fires — this would confirm an accumulation bug."""
    from app.cost_telemetry_reconciliation import (
        PROVIDER_REPORTED_TOLERANCE,
        reconcile_provider_reported_usage,
    )

    task = _make_deepseek_task(
        model="deepseek-v4-flash",
        input_tokens=5_500_000,  # agent accumulated 10% more than provider
        output_tokens=1_000_000,
        cache_creation=200_000,
        cache_read=4_500_000,
    )
    task.provider_reported_usage = {
        "input_tokens": 300_000,
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 200_000,
        "cache_read_input_tokens": 4_500_000,
    }
    # Provider total = 5M; agent accumulated = 5.5M → 10% divergence.

    result = reconcile_provider_reported_usage([task])

    assert result.task_count == 1
    assert result.provider_reported_within_tolerance is False
    assert result.provider_reported_delta > PROVIDER_REPORTED_TOLERANCE


# ── 7. Pricing coverage diagnostic ───────────────────────────────────────


def test_deepseek_flash_is_priced_in_coverage_check():
    """_compute_llm_cost_pricing_coverage marks deepseek-v4-flash as priced."""
    task = _make_deepseek_task(
        model="deepseek-v4-flash",
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    coverage = _compute_llm_cost_pricing_coverage([task])

    assert coverage["coverage_fraction"] == 1.0
    assert coverage["unpriced_tasks"] == 0
    assert coverage["priced_tasks"] == 1
    assert "deepseek:deepseek-v4-flash" not in coverage["unpriced_combos"]


def test_deepseek_pro_is_priced_in_coverage_check():
    """_compute_llm_cost_pricing_coverage marks deepseek-v4-pro as priced."""
    task = _make_deepseek_task(
        model="deepseek-v4-pro",
        input_tokens=500_000,
        output_tokens=100_000,
    )
    coverage = _compute_llm_cost_pricing_coverage([task])

    assert coverage["coverage_fraction"] == 1.0
    assert coverage["unpriced_tasks"] == 0


def test_deepseek_wrong_model_string_appears_in_unpriced_combos():
    """Tasks with unknown model strings surface in unpriced_combos — the
    diagnostic operators need to identify the silent-$0 gap."""
    wrong_model_task = _make_deepseek_task(
        model="deepseek-r1",  # reasoning model — not in pricing table
        input_tokens=2_000_000,
        output_tokens=500_000,
    )
    coverage = _compute_llm_cost_pricing_coverage([wrong_model_task])

    assert "deepseek:deepseek-r1" in coverage["unpriced_combos"]
    assert coverage["unpriced_tasks"] == 1
    assert coverage["priced_tasks"] == 0
    # Coverage fraction is 0.0, not None (because there IS a priceable attempt).
    assert coverage["coverage_fraction"] == 0.0


# ── 8. Gap analysis: volume math ─────────────────────────────────────────


def test_deepseek_billing_volume_math_cost_at_scale():
    """Reproduce the operator's billing math at full scale to confirm pricing
    is arithmetically correct end-to-end.

    Flash: 2.6B tokens, 97% cache-read → expected ~$7–10 at the $0.0028/M rate.
    This test verifies the attribution pipeline produces a non-trivial cost for
    realistic volumes (not zero, not grossly wrong).
    """
    total_input = 2_633_500_000
    cache_read = int(total_input * 0.97)  # 97% cache-hit
    output_tokens = 109_000_000  # rough estimate for associated output

    task = _make_deepseek_task(
        model="deepseek-v4-flash",
        input_tokens=total_input,
        output_tokens=output_tokens,
        cache_read=cache_read,
    )
    result = compute_llm_cost_breakdown([task])

    # Must be non-zero — correct pricing for this volume.
    total_cost = result["totals"]["total_cost_usd"]
    assert total_cost > 5.0, (
        f"2.6B flash tokens with 97% cache-hit should cost ~$7-10 USD; "
        f"got ${total_cost:.4f}. Suggests incorrect pricing path."
    )
    # Must not be wildly over-priced (max ~$369 if all tokens were at full rate).
    assert total_cost < 100.0, (
        f"Cost ${total_cost:.2f} is unexpectedly high — cache-hit discount not applied"
    )


def test_deepseek_zero_api_call_summary_produces_zero_cost():
    """A task with no api_call_summary and no attempt_history produces $0 cost.
    This models the silent capture failure: if the agent never writes back token
    counts, the task is effectively invisible to cost attribution."""
    task = make_task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary=None,
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    result = compute_llm_cost_breakdown([task])

    # No tokens recorded → no cost. But the task_count should still be 0
    # (no attempt rows means the task is invisible to the breakdown).
    assert result["totals"]["total_cost_usd"] == 0.0
    assert result["totals"]["task_count"] == 0, (
        "A task with zero tokens must not inflate task_count — "
        "it is invisible to attribution"
    )


def test_deepseek_tasks_without_tokens_excluded_from_totals():
    """Tasks with zero input AND zero output tokens are excluded from all
    aggregation buckets — they do not pad task_count or distort merge_rate."""
    zero_task = make_task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "llm_call_count": 0,
        },
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    real_task = _make_deepseek_task(
        model="deepseek-v4-flash",
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    result = compute_llm_cost_breakdown([zero_task, real_task])

    # Only the real task contributes.
    assert result["totals"]["task_count"] == 1
    assert result["totals"]["total_input_tokens"] == 1_000_000


# ── 9. Cost computation precision ─────────────────────────────────────────


def test_deepseek_flash_cost_computation_exact_value():
    """Verify _price_llm_attempt produces the exact expected dollar amount for a
    known DeepSeek flash input (no ambiguity about which code path ran)."""
    # 1M uncached input + 0 output.
    expected = 1_000_000 * _FLASH_INPUT_RATE  # $0.14
    input_rate, output_rate = LLM_TOKEN_PRICING[("deepseek", "deepseek-v4-flash")]

    cost = _price_llm_attempt(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        input_rate=input_rate,
        output_rate=output_rate,
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    assert cost == pytest.approx(expected, rel=1e-9)
    assert cost == pytest.approx(0.14, abs=1e-9)  # $0.14 exactly


def test_deepseek_pro_cost_computation_exact_value():
    """Verify _price_llm_attempt for deepseek-v4-pro (no cache) is exact."""
    input_rate, output_rate = LLM_TOKEN_PRICING[("deepseek", "deepseek-v4-pro")]
    # 1M input + 500k output.
    cost = _price_llm_attempt(
        input_tokens=1_000_000,
        output_tokens=500_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        input_rate=input_rate,
        output_rate=output_rate,
        provider="deepseek",
        model="deepseek-v4-pro",
    )
    expected = 1_000_000 * _PRO_INPUT_RATE + 500_000 * _PRO_OUTPUT_RATE
    assert cost == pytest.approx(expected, rel=1e-9)
    # $0.435 + $0.435 = $0.870
    assert cost == pytest.approx(0.870, abs=1e-9)


def test_deepseek_cost_per_model_share_of_total():
    """When both flash and pro are in the window, the cost shares sum to 1.0."""
    flash_task = _make_deepseek_task(
        model="deepseek-v4-flash",
        input_tokens=10_000_000,
        output_tokens=2_000_000,
    )
    pro_task = _make_deepseek_task(
        model="deepseek-v4-pro",
        input_tokens=5_000_000,
        output_tokens=1_000_000,
    )
    result = compute_llm_cost_breakdown([flash_task, pro_task])

    shares = [r["share_of_total_cost"] for r in result["by_model"] if r["share_of_total_cost"] is not None]
    assert len(shares) == 2
    assert sum(shares) == pytest.approx(1.0, abs=1e-4)
