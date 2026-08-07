"""Tests for OpenAI prompt-cache read capture and cache-read-rate pricing.

Context: The operator reported gpt-5.6-sol accounting for $207.08 of strategist
LLM spend (56% of total) at ZERO recorded cache reads, while Anthropic-path
calls record cache reads correctly. Root cause was on the AGENT side: the Codex
CLI reports the cached-input sub-count as a top-level ``cached_input_tokens``
sibling inside usage (``{input_tokens, cached_input_tokens, output_tokens}``),
NOT under ``prompt_tokens_details`` / ``input_tokens_details``, so every
codex/OpenAI run's cache hits were dropped at normalization and persisted as
zero cache reads (fixed in agent/src/providers/usage.ts). These tests verify the
BACKEND pricing half of the fix — that once the agent's normalized cache-read
sub-count lands in ``api_call_summary.total_cache_read_input_tokens`` it is
attributed as non-zero cache-read tokens AND priced at the cache-read rate
instead of the base input rate:

1. OpenAI cache-read pricing constants are exactly 0.1x base input rate.
2. ``_price_llm_attempt`` prices a listed OpenAI model's cache reads at the
   explicit cache-read rate, not the base input rate.
3. An OpenAI model WITHOUT a row in ``LLM_CACHE_READ_INPUT_PRICING`` defaults to
   the same 0.1x relationship (the ``elif provider_key == "openai"`` branch) so
   a model added to ``LLM_TOKEN_PRICING`` without a cache row is never silently
   full-billed for cache reads.
4. ``compute_llm_cost_breakdown`` attributes non-zero cache-read tokens AND
   prices them at the cache-read rate for an OpenAI-shaped usage payload.
5. The realistic Codex CLI shape (``{input_tokens, cached_input_tokens,
   output_tokens}``) prices at the discounted rate, not as if every token were
   uncached.
6. Banked (multi-attempt) OpenAI cache sub-counts survive into pricing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.llm_cost_attribution import (
    CACHE_READ_INPUT_RATE_MULTIPLIER,
    LLM_CACHE_READ_INPUT_PRICING,
    LLM_TOKEN_PRICING,
    UNPRICED_FALLBACK_INPUT_RATE,
    UNPRICED_FALLBACK_OUTPUT_RATE,
    _compact_llm_cost_attribution,
    _price_llm_attempt,
    _task_llm_attempts,
    compute_llm_cost_breakdown,
)
from app.models import TaskStatus, bank_task_current_attempt
from testing.factories import make_task


# ── Constants pulled from the OpenAI pricing table ─────────────────────────

# gpt-5.6-sol: $5/M input (cache-miss), $30/M output, $0.50/M cache-read
_SOL_INPUT_RATE = 5e-6
_SOL_OUTPUT_RATE = 30e-6
_SOL_CACHE_READ_RATE = 0.50e-6


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_openai_task(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
    provider: str = "openai",
    **kw,
) -> object:
    """Create an OpenAI task with the given token counts in api_call_summary.

    Mirrors what the agent persists after the usage.ts normalization fix: the
    Codex/OpenAI cache-read sub-count lands in ``total_cache_read_input_tokens``
    and ``total_input_tokens`` is the INCLUSIVE total.
    """
    summary: dict = {
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
        "llm_call_count": 1,
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


# ── 1. OpenAI cache-read pricing constants ─────────────────────────────────


def test_openai_cache_read_rates_are_0_1x_base_input_rate():
    """Every listed OpenAI cache-read rate must be exactly 0.1x its base input
    rate — the relationship the new default branch relies on."""
    openai_cache_rows = [
        (provider, model)
        for (provider, model) in LLM_CACHE_READ_INPUT_PRICING
        if provider == "openai"
    ]
    assert openai_cache_rows, "no OpenAI rows in LLM_CACHE_READ_INPUT_PRICING"
    for key in openai_cache_rows:
        base_input_rate, _ = LLM_TOKEN_PRICING[key]
        rate = LLM_CACHE_READ_INPUT_PRICING[key]
        assert rate == pytest.approx(
            base_input_rate * CACHE_READ_INPUT_RATE_MULTIPLIER, rel=1e-9
        ), (
            f"{key} cache-read rate {rate} != 0.1x base {base_input_rate}"
        )


def test_gpt_5_6_sol_cache_read_rate_matches_billing():
    """gpt-5.6-sol cache-read must be $0.50/M — 10x cheaper than the $5/M
    cache-miss input rate."""
    key = ("openai", "gpt-5.6-sol")
    assert key in LLM_CACHE_READ_INPUT_PRICING, (
        "gpt-5.6-sol missing from LLM_CACHE_READ_INPUT_PRICING — cache-hit "
        "tokens would be priced at the $5/M base rate instead of $0.50/M"
    )
    assert LLM_CACHE_READ_INPUT_PRICING[key] == pytest.approx(
        _SOL_CACHE_READ_RATE, rel=1e-9
    )


# ── 2. Cache pricing path exercised for a listed OpenAI model ──────────────


def test_gpt_5_6_sol_cache_read_uses_discount_rate_not_base_rate():
    """_price_llm_attempt routes gpt-5.6-sol to LLM_CACHE_READ_INPUT_PRICING
    (0.1x) rather than the fallback `cache_read_rate = input_rate` branch."""
    # 1M tokens all as cache-read input — no uncached, no output.
    cost_with_cache = _price_llm_attempt(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=1_000_000,
        input_rate=_SOL_INPUT_RATE,
        output_rate=_SOL_OUTPUT_RATE,
        provider="openai",
        model="gpt-5.6-sol",
    )
    expected_with_cache = 1_000_000 * _SOL_CACHE_READ_RATE
    assert cost_with_cache == pytest.approx(expected_with_cache, rel=1e-9), (
        "gpt-5.6-sol cache-read tokens must use the $0.50/M rate, not $5/M"
    )

    # Without cache routing — all input at the base rate.
    cost_without_cache = _price_llm_attempt(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        input_rate=_SOL_INPUT_RATE,
        output_rate=_SOL_OUTPUT_RATE,
        provider="openai",
        model="gpt-5.6-sol",
    )

    # Cache-read must be exactly 10x cheaper.
    ratio = cost_without_cache / cost_with_cache
    assert ratio == pytest.approx(10.0, rel=0.01), (
        f"Expected 10x cost reduction for cache-read vs uncached; got {ratio:.1f}x"
    )


def test_gpt_5_6_sol_exact_mixed_cost_value():
    """A 1M-input / 100K-output call with 90% cache-read prices to the exact
    expected dollar amount: uncached (100K * $5/M) + cache-read (900K * $0.50/M)
    + output (100K * $30/M) = $3.95, not $8.00 (full-rate)."""
    cache_read = 900_000
    total_input = 1_000_000
    output = 100_000

    cost = _price_llm_attempt(
        input_tokens=total_input,
        output_tokens=output,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=cache_read,
        input_rate=_SOL_INPUT_RATE,
        output_rate=_SOL_OUTPUT_RATE,
        provider="openai",
        model="gpt-5.6-sol",
    )
    expected = (
        (total_input - cache_read) * _SOL_INPUT_RATE
        + cache_read * _SOL_CACHE_READ_RATE
        + output * _SOL_OUTPUT_RATE
    )
    assert cost == pytest.approx(expected, rel=1e-9)
    assert cost == pytest.approx(3.95, abs=1e-9), (
        f"Expected $3.95 (cache-rate); got ${cost:.6f} — if this reads $8.00 "
        "the cache-read discount is not being applied"
    )


# ── 3. Unlisted OpenAI model defaults to 0.1x (new elif branch) ────────────


def test_unlisted_openai_model_defaults_to_0_1x_cache_read_rate():
    """An OpenAI model WITHOUT a row in LLM_CACHE_READ_INPUT_PRICING must still
    price cache reads at 0.1x base (the `elif provider_key == "openai"` branch),
    not the full base input rate (the else fallback)."""
    # Hypothetical future OpenAI model — not in LLM_CACHE_READ_INPUT_PRICING.
    assert ("openai", "gpt-future-unknown") not in LLM_CACHE_READ_INPUT_PRICING

    cost_with_cache = _price_llm_attempt(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=1_000_000,
        input_rate=_SOL_INPUT_RATE,
        output_rate=_SOL_OUTPUT_RATE,
        provider="openai",
        model="gpt-future-unknown",
    )
    expected = 1_000_000 * _SOL_INPUT_RATE * CACHE_READ_INPUT_RATE_MULTIPLIER
    assert cost_with_cache == pytest.approx(expected, rel=1e-9), (
        "Unlisted OpenAI model cache reads must default to 0.1x base rate, "
        "not full base rate"
    )

    cost_without_cache = _price_llm_attempt(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        input_rate=_SOL_INPUT_RATE,
        output_rate=_SOL_OUTPUT_RATE,
        provider="openai",
        model="gpt-future-unknown",
    )
    ratio = cost_without_cache / cost_with_cache
    assert ratio == pytest.approx(10.0, rel=0.01)


def test_breakdown_unlisted_openai_model_uses_0_1x_cache_rate(monkeypatch):
    """The default 0.1x branch is exercised through the FULL breakdown, not just
    the unit helper: a future OpenAI model in LLM_TOKEN_PRICING without a cache
    row prices cache reads at the discounted rate end-to-end."""
    assert ("openai", "gpt-future-model") not in LLM_CACHE_READ_INPUT_PRICING
    monkeypatch.setitem(
        LLM_TOKEN_PRICING,
        ("openai", "gpt-future-model"),
        (_SOL_INPUT_RATE, _SOL_OUTPUT_RATE),
    )

    task = _make_openai_task(
        model="gpt-future-model",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read=900_000,
    )
    result = compute_llm_cost_breakdown([task])
    expected = (
        100_000 * _SOL_INPUT_RATE  # uncached at base rate
        + 900_000 * _SOL_INPUT_RATE * CACHE_READ_INPUT_RATE_MULTIPLIER
        + 100_000 * _SOL_OUTPUT_RATE
    )
    assert result["totals"]["total_cost_usd"] == pytest.approx(expected, rel=1e-6)
    # Sanity: the same tokens at full uncached rates would cost more.
    without_discount = 1_000_000 * _SOL_INPUT_RATE + 100_000 * _SOL_OUTPUT_RATE
    assert result["totals"]["total_cost_usd"] < without_discount
    # The model must be priced (not silently zero-costed).
    row = next(r for r in result["by_model"] if r["model"] == "gpt-future-model")
    assert row["priced"] is True


# ── 4. Breakdown attributes non-zero cache reads + cache-rate pricing ──────


def test_breakdown_attributes_nonzero_cache_reads_and_prices_at_cache_rate():
    """An OpenAI-shaped usage payload (agent normalized cached tokens into
    total_cache_read_input_tokens) produces NON-ZERO cache-read attribution in
    the breakdown AND prices those tokens at the cache-read rate."""
    cache_read = 900_000
    total_input = 1_000_000
    output = 100_000

    task = _make_openai_task(
        model="gpt-5.6-sol",
        input_tokens=total_input,
        output_tokens=output,
        cache_read=cache_read,
    )
    result = compute_llm_cost_breakdown([task])

    totals = result["totals"]
    # Non-zero cache-read attribution — the value that was 0 before the fix.
    assert totals["total_cache_read_input_tokens"] == cache_read
    assert totals["total_input_tokens"] == total_input
    assert totals["total_output_tokens"] == output

    # Cache-rate pricing: $3.95 (0.50 + 0.45 + 3.00), not $8.00 full-rate.
    expected = (
        (total_input - cache_read) * _SOL_INPUT_RATE
        + cache_read * _SOL_CACHE_READ_RATE
        + output * _SOL_OUTPUT_RATE
    )
    assert totals["total_cost_usd"] == pytest.approx(expected, rel=1e-6)
    assert totals["total_cost_usd"] == pytest.approx(3.95, abs=1e-6)

    # The by_model row carries the cache-read attribution too.
    row = next(r for r in result["by_model"] if r["model"] == "gpt-5.6-sol")
    assert row["total_cache_read_input_tokens"] == cache_read
    assert row["total_input_tokens"] == total_input
    assert row["priced"] is True


# ── 5. Realistic Codex CLI shape prices at the discounted rate ─────────────


def test_codex_cli_shape_prices_cache_reads_not_uncached():
    """The exact Codex CLI usage shape that was silently dropped at the agent —
    {input_tokens: 24763, cached_input_tokens: 24448, output_tokens: 122} — now
    reaches the backend as total_input_tokens=24763 / total_cache_read_input_
    tokens=24448 and prices at the cache-read rate, not as if every token were
    uncached."""
    task = _make_openai_task(
        model="gpt-5.6-sol",
        input_tokens=24_763,
        output_tokens=122,
        cache_read=24_448,
    )
    result = compute_llm_cost_breakdown([task])

    totals = result["totals"]
    assert totals["total_cache_read_input_tokens"] == 24_448
    # ~98.7% cache-hit share is attributed, not dropped.
    assert totals["total_input_tokens"] == 24_763

    uncached = 24_763 - 24_448  # 315
    expected = (
        uncached * _SOL_INPUT_RATE
        + 24_448 * _SOL_CACHE_READ_RATE
        + 122 * _SOL_OUTPUT_RATE
    )
    assert totals["total_cost_usd"] == pytest.approx(expected, rel=1e-6)

    # Without cache capture, all 24_763 input tokens would bill at base rate.
    uncached_only = 24_763 * _SOL_INPUT_RATE + 122 * _SOL_OUTPUT_RATE
    assert totals["total_cost_usd"] < uncached_only * 0.25, (
        "Codex CLI cache reads must price at the ~$0.50/M discount rate, "
        "not the $5/M uncached rate"
    )


def test_openai_zero_cache_reads_priced_at_base_rate():
    """Regression guard: a fully-uncached OpenAI call (no cache sub-counts) still
    prices at the base input rate — the new default branch must not discount
    input that was never served from cache."""
    task = _make_openai_task(
        model="gpt-5.6-sol",
        input_tokens=1_000_000,
        output_tokens=100_000,
    )
    result = compute_llm_cost_breakdown([task])

    assert result["totals"]["total_cache_read_input_tokens"] == 0
    expected = 1_000_000 * _SOL_INPUT_RATE + 100_000 * _SOL_OUTPUT_RATE
    assert result["totals"]["total_cost_usd"] == pytest.approx(expected, rel=1e-6)


# ── 6. Banked (multi-attempt) OpenAI cache sub-counts ──────────────────────


def test_openai_banked_attempt_preserves_cache_subcounts():
    """After bank_task_current_attempt(), the OpenAI attempt's cache-read
    sub-count appears in _task_llm_attempts and drives the discounted pricing."""
    task = make_task(
        status=TaskStatus.IN_PROGRESS,
        api_call_summary={
            "total_input_tokens": 5_000_000,
            "total_output_tokens": 500_000,
            "total_cache_read_input_tokens": 4_500_000,  # 90% cache hit
            "llm_call_count": 10,
        },
        provider="openai",
        model="gpt-5.6-sol",
    )
    bank_task_current_attempt(task, stopped_at=_utcnow())

    attempts = _task_llm_attempts(task)
    assert len(attempts) == 1
    provider, model, inp, out, _calls, cc, cr = attempts[0]
    assert provider == "openai"
    assert model == "gpt-5.6-sol"
    assert inp == 5_000_000
    assert out == 500_000
    assert cc == 0
    assert cr == 4_500_000

    expected_cost = (
        (5_000_000 - 4_500_000) * _SOL_INPUT_RATE  # uncached at base rate
        + 4_500_000 * _SOL_CACHE_READ_RATE           # cache reads at discount
        + 500_000 * _SOL_OUTPUT_RATE
    )
    result = compute_llm_cost_breakdown([task])
    assert result["totals"]["total_cache_read_input_tokens"] == 4_500_000
    assert result["totals"]["total_cost_usd"] == pytest.approx(expected_cost, rel=1e-6)


# ── 7. Compact attribution surfaces turns + cache split per task type ──────


def test_compact_attribution_surfaces_turns_and_cache_split_per_task_type():
    """The compact attribution block's per-task-type rows must carry turn counts
    and the cache-creation/cache-read token split, not just prompt bytes.

    ``compute_llm_cost_by_task_type`` already computes these per-bucket fields;
    ``_compact_llm_cost_attribution`` previously dropped them, leaving
    prompt_size_bytes as the only visible cost driver. Prompt bytes are ~5% of
    strategic_review's mean per-call input, so the visible proxy pointed at the
    wrong lever. These fields flow through to the agent efficiency-snapshot
    endpoint and the strategic payload's ``dominant_task_type`` row."""
    import uuid

    project_id = uuid.uuid4()

    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create"}],
        api_call_summary={
            "total_input_tokens": 1_000_000,
            "total_output_tokens": 100_000,
            "llm_call_count": 10,
            "total_cache_creation_input_tokens": 50_000,
            "total_cache_read_input_tokens": 800_000,
            "num_turns": 20,
        },
        provider="openai",
        model="gpt-5.6-sol",
    )

    result = _compact_llm_cost_attribution([task])
    by_type = {r["task_type"]: r for r in result["by_task_type"]}
    assert "create" in by_type, f"create row missing: {list(by_type)}"
    row = by_type["create"]

    # Turn counts — 20 turns across 10 calls; 1M input tokens across 20 turns.
    assert row["mean_turns_per_call"] == 2.0
    assert row["mean_input_tokens_per_turn"] == 50_000

    # Cache split — 800k/1M (80%) cache-read, 50k cache-creation, 150k uncached.
    assert row["total_cache_creation_input_tokens"] == 50_000
    assert row["total_cache_read_input_tokens"] == 800_000
    assert row["cache_read_share"] == 0.8
    assert row["mean_uncached_input_tokens_per_call"] == 15_000


# ── 8. Spend-weighted unpriced coverage + retained unpriced token volume ─────


def test_unpriced_coverage_is_spend_weighted_and_retains_unpriced_token_volume():
    """``unpriced_coverage_fraction`` must be spend-weighted, not a task count,
    and the unpriced rows' token volume must be retained so the pricing blind
    spot is measurable: a 1M-token unpriced row depresses coverage far below the
    2/3 task-count, and a 1K-token one barely moves it off the 1/2 task-count."""

    def _unpriced(in_tok: int, out_tok: int) -> object:
        return make_task(
            status=TaskStatus.COMPLETED,
            api_call_summary={
                "total_input_tokens": in_tok,
                "total_output_tokens": out_tok,
                "llm_call_count": 1,
            },
            provider="fakeprovider",
            model="mystery-model",
        )

    # 1) A 1M-token unpriced row among small priced rows: spend-weighted coverage
    #    must be far below the task-count 2/3 (the unpriced row dominates spend).
    priced = [
        _make_openai_task(model="gpt-5.6-sol", input_tokens=1_000, output_tokens=100)
        for _ in range(2)
    ]
    totals = compute_llm_cost_breakdown(
        [*priced, _unpriced(1_000_000, 100_000)]
    )["totals"]
    priced_cost = 2 * (1_000 * _SOL_INPUT_RATE + 100 * _SOL_OUTPUT_RATE)
    est_unpriced = (
        1_000_000 * UNPRICED_FALLBACK_INPUT_RATE
        + 100_000 * UNPRICED_FALLBACK_OUTPUT_RATE
    )
    assert (totals["priced_task_count"], totals["unpriced_task_count"]) == (2, 1)
    assert totals["unpriced_coverage_fraction"] == round(
        priced_cost / (priced_cost + est_unpriced), 4
    )
    assert totals["unpriced_coverage_fraction"] != round(2 / 3, 4)
    assert totals["estimated_unpriced_cost_usd"] == round(est_unpriced, 6)
    # Retained unpriced token volume — the blind spot is measurable.
    assert totals["unpriced_input_tokens"] == 1_000_000
    assert totals["unpriced_output_tokens"] == 100_000
    assert totals["total_cost_usd"] == round(priced_cost, 6)

    # 2) A 1K-token unpriced row among a large priced row: coverage near 1.0, not
    #    the task-count 1/2 (the blind spot is a sliver of the spend).
    totals2 = compute_llm_cost_breakdown(
        [
            _make_openai_task(
                model="gpt-5.6-sol", input_tokens=1_000_000, output_tokens=100_000
            ),
            _unpriced(1_000, 100),
        ]
    )["totals"]
    priced_cost2 = 1_000_000 * _SOL_INPUT_RATE + 100_000 * _SOL_OUTPUT_RATE
    est_unpriced2 = 1_000 * UNPRICED_FALLBACK_INPUT_RATE + 100 * UNPRICED_FALLBACK_OUTPUT_RATE
    assert totals2["unpriced_coverage_fraction"] == round(
        priced_cost2 / (priced_cost2 + est_unpriced2), 4
    )
    assert totals2["unpriced_coverage_fraction"] != round(1 / 2, 4)
    assert totals2["unpriced_input_tokens"] == 1_000
    assert totals2["unpriced_output_tokens"] == 100

