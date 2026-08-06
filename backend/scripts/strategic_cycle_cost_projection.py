#!/usr/bin/env python3
"""Project v28's median per-cycle strategist (strategic_review) LLM cost.

v28's headline goal promises a median strategist cycle under $1.50 (down from a
measured $4.41). Nothing in the repo computes that number; this module derives
the projected median from stated inputs, every one cited below, and prices the
two caching regimes SEPARATELY:

  * INTRA-CYCLE turn repetition  — within one cycle, repeated turns re-send the
    fixed prompt + accumulated conversation. Anthropic's automatic prefix cache
    matches the whole fixed payload (TTL 5 min, extended to 1 h when a cached
    prefix keeps being re-sent). This is the DOMINANT caching regime and it is
    ALREADY priced into the measured $4.41 baseline. The v28 prompt reorder
    does not change it: the prompt is rendered once per cycle and byte-identical
    across turns whether or not the stable prefix leads.

  * CROSS-CYCLE prefix reuse    — across separate cycles, only the stable
    instruction block (~42 KB / ~10.5K tokens) is byte-identical. The volatile
    payload it fronts changes every cycle. The v28 stable-prefix reorder
    (PR #16731) extends the cacheable prefix from a few hundred bytes to the
    full ~10.5K-token stable block on turn 1 of a cycle — but ONLY when the
    cache survives the inter-cycle gap (cycle cadence is hours/days, far past
    the 1-hour cache TTL, so in steady state this saving is ~$0.00/cycle).

Conflating these two regimes "is exactly how you manufacture a projection that
hits its target on paper." This module keeps them apart, states every
assumption, shows sensitivity, and reports the number honestly even though it
exceeds $1.50.

Inputs and sources (each cited):
  * Baseline per-cycle cost       $4.41/cycle — task-provided measurement.
    (Design doc d6c4d7fb-… cites $4.43/cycle; strategic_review_effort.py
    docstring cites "~$4.43 per cycle".)
  * Mean input tokens per call    741,992 — task-provided measurement.
    (Design doc cites 737,568.) Per call this is the INCLUSIVE input total:
    fresh input + cache read + cache creation (see _cache_read_share and
    mean_input_tokens_per_call in backend/app/llm_cost_attribution.py).
  * Stable prefix rendered size   42,073 bytes (representative render) —
    measured from agent/src/prompts/thinker.ts buildStrategicStablePrefix with
    projectName='Blueprint AI Studio', isSelfRepo=true,
    selfImprovementBlock=355 B from agent/src/handlers/strategic.ts,
    PLAYBOOK_GUIDANCE / DEFAULT_TOOL_GUIDANCE / READ_ONLY_BASH_WRITE_NOTE
    from agent/src/prompt.ts.
  * Volatile instruction bytes    2,666 B (max denominator: bootstrap/recover +
    gap-memory trim + design-doc overview + current-activity intro + structural
    headers + 591 B direct literal) — measured from the same template render
    with every per-cycle payload field set to ''.
  * Representative volatile        ~104 KB total (2,666 B instruction + ~100 KB
    payload) — a strategic cycle ships a large problem_model JSON, direction
    portfolio, and context blocks (see payload pack in backend/app/
    strategic_spawning.py).
  * Tier mix                       PR #16730: ROUTINE cycles classify to
    effort 'medium' (openai gpt-5.6-terra), CONSEQUENTIAL stay 'max'
    (gpt-5.6-sol). Routine share R is UNOBSERVED (the classifier is inert until
    v28 promotes); assumed base 0.70, sensitivity 0.3-0.9. Fail-closed
    classifier (backend/app/strategic_review_effort.py) means dirty/recovering
    periods push R down.
  * Pricing (per 1M tokens)       backend/app/llm_cost_attribution.py:
      gpt-5.6-sol:   input $5,  output $30, cache read $0.50, cache create $6.25
      gpt-5.6-terra: input $2.5, output $15, cache read $0.25, cache create $3.125
      cache read rate = 0.1 x input; cache create rate = 1.25 x input.
  * Cache read share              0.55 baseline for strategic_review (design
    doc); target >= 0.80 "achieved via a stable cacheable prefix".

Tokens are estimated from measured bytes at 4 bytes/token (standard English-text
approximation; the byte counts themselves are exact, and the cost model is
anchored on the measured $4.41, so the token estimate only affects the tiny
cross-cycle term).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Measured / stated inputs
# ---------------------------------------------------------------------------

# Measured per-cycle cost and per-call inclusive input (task-provided; design
# doc corroborates at $4.43 / 737,568).
BASELINE_COST_PER_CYCLE_USD = 4.41
MEAN_INPUT_TOKENS_PER_CALL = 741_992

# Rendered prompt sizes, measured statically from thinker.ts / prompt.ts.
# (Exact byte counts; token estimate at 4 bytes/token.)
STABLE_PREFIX_BYTES = 42_073          # representative render (rep params)
STABLE_PREFIX_TOKENS = STABLE_PREFIX_BYTES / 4.0   # ~10.5K
VOLATILE_INSTRUCTION_BYTES = 2_666    # max denominator (recover+gap+design blocks)
VOLATILE_PAYLOAD_BYTES_REP = 104_000  # ~100 KB representative payload
TOTAL_PROMPT_BYTES_REP = STABLE_PREFIX_BYTES + VOLATILE_PAYLOAD_BYTES_REP
VOLATILE_INSTRUCTION_TOKENS = VOLATILE_INSTRUCTION_BYTES / 4.0

# Pricing (backend/app/llm_cost_attribution.py). USD per 1M tokens.
SOL_INPUT, SOL_OUTPUT, SOL_READ = 5.0, 30.0, 0.50
TERRA_INPUT, TERRA_OUTPUT, TERRA_READ = 2.5, 15.0, 0.25
CACHE_CREATE_MULTIPLIER = 1.25        # cache create = 1.25 x input rate

# Baseline cache read share for strategic_review (design doc).
BASELINE_CACHE_READ_SHARE = 0.55

# Assumptions (stated explicitly).
ROUTINE_SHARE_BASE = 0.70             # share of cycles classified 'routine'
INPUT_COST_SHARE_BASE = 0.90          # input(+create+read) share of the $4.41
CROSS_CYCLE_SURVIVAL_BASE = 0.0       # P(cache survives the inter-cycle gap)


def effective_input_price_multiplier(read_share: float) -> float:
    """Price per token relative to the full input rate.

    read_share of tokens are cache reads at 0.1x; the rest are fresh at 1.0x.
    (Cache creation is 1.25x but is a single prompt write per cycle, ~36.5K
    tokens, <1% of the cycle's input tokens — treated separately and, at the
    precision of this projection, folded into the fresh bucket.)
    """
    return (1.0 - read_share) * 1.0 + read_share * 0.1


def tier_blended_multiplier(routine_share: float) -> float:
    """Blended price multiplier across the fleet.

    Terra is exactly 0.5x sol on every rate (input, output, cache read, cache
    create). So a cycle that runs terra costs 0.5x a sol cycle, and a fleet
    with routine_share R costs (1 - R)*1.0 + R*0.5 = 1 - 0.5R.
    """
    return 1.0 - 0.5 * routine_share


def cross_cycle_saving_per_cycle(routine_share: float, survival: float) -> float:
    """Per-cycle saving from cross-cycle stable-prefix reuse, USD.

    The reorder lets turn 1 of a cycle cache the ~10.5K-token stable prefix
    instead of re-reading it fresh — but ONLY if the cache survives the
    inter-cycle gap (TTL). The saving is bounded by
    STABLE_PREFIX_TOKENS x (input_rate - read_rate) x survival, once per cycle.
    The terra half of the fleet prices it at half rate.
    """
    sol_delta = STABLE_PREFIX_TOKENS * (SOL_INPUT - SOL_READ) / 1_000_000
    terra_delta = STABLE_PREFIX_TOKENS * (TERRA_INPUT - TERRA_READ) / 1_000_000
    blended_delta = (1.0 - routine_share) * sol_delta + routine_share * terra_delta
    return blended_delta * survival


def project_cycle_cost(
    routine_share: float,
    cache_read_share_after: float,
    survival: float,
    input_cost_share: float = INPUT_COST_SHARE_BASE,
) -> float:
    """Projected median per-cycle cost, USD.

    cost = baseline x tier_multiplier x [input_share x m(after)/m(before)
           + (1 - input_share)]
           - cross_cycle_saving
    """
    before = effective_input_price_multiplier(BASELINE_CACHE_READ_SHARE)
    after = effective_input_price_multiplier(cache_read_share_after)
    cost = (
        BASELINE_COST_PER_CYCLE_USD
        * tier_blended_multiplier(routine_share)
        * (input_cost_share * after / before + (1.0 - input_cost_share))
    )
    return cost - cross_cycle_saving_per_cycle(routine_share, survival)


def rendered_instruction_share() -> dict[str, float]:
    """Stable prefix's share of the rendered prompt, both denominators.

    Mirrors ``measureStrategicPromptInstructionShare`` in
    agent/src/prompts/thinker.ts using the representative measured byte counts.
    The TRUE claim is the instruction denominator (>0.8); against total prompt
    bytes the prefix is well under half because the per-cycle payload dominates.
    """
    instruction_bytes = STABLE_PREFIX_BYTES + VOLATILE_INSTRUCTION_BYTES
    return {
        "stable_prefix_bytes": STABLE_PREFIX_BYTES,
        "volatile_instruction_bytes": VOLATILE_INSTRUCTION_BYTES,
        "instruction_bytes": instruction_bytes,
        "total_prompt_bytes": TOTAL_PROMPT_BYTES_REP,
        "stable_share_of_instruction": STABLE_PREFIX_BYTES / instruction_bytes,
        "stable_share_of_total": STABLE_PREFIX_BYTES / TOTAL_PROMPT_BYTES_REP,
    }


def main() -> None:
    print("=" * 78)
    print("v28 projected median per-cycle strategist LLM cost")
    print("=" * 78)

    print("\n[1] Measured / stated inputs (each cited to its source)\n")
    rows = [
        ("Baseline cost/cycle", f"${BASELINE_COST_PER_CYCLE_USD} (task measurement; design doc $4.43)"),
        ("Mean input tokens/call (inclusive)", f"{MEAN_INPUT_TOKENS_PER_CALL:,} (task measurement; design doc 737,568)"),
        ("Baseline cache read share", f"{BASELINE_CACHE_READ_SHARE:.2f} (design doc)"),
        ("Stable prefix rendered", f"{STABLE_PREFIX_BYTES:,} B ~ {STABLE_PREFIX_TOKENS:,.0f} tokens (measured, thinker.ts)"),
        ("Volatile instruction (max denom.)", f"{VOLATILE_INSTRUCTION_BYTES:,} B ~ {VOLATILE_INSTRUCTION_TOKENS:,.0f} tokens (measured, thinker.ts)"),
        ("Volatile total (rep. payload)", f"{VOLATILE_PAYLOAD_BYTES_REP:,} B ~ {VOLATILE_PAYLOAD_BYTES_REP/4:,.0f} tokens"),
        ("Routine tier / price ratio", "gpt-5.6-terra (medium) vs gpt-5.6-sol (max) = 0.5x every rate (PR #16730, llm_cost_attribution.py)"),
        ("Cache read rate", "0.1 x input rate; cache create 1.25 x input rate (llm_cost_attribution.py)"),
    ]
    for k, v in rows:
        print(f"  {k:<42} {v}")

    print("\n[2] Rendered stable/volatile split (measured bytes -> tokens)\n")
    share = rendered_instruction_share()
    print(f"  stable prefix bytes           {share['stable_prefix_bytes']:>10,}")
    print(f"  volatile instruction bytes    {share['volatile_instruction_bytes']:>10,}")
    print(f"  instruction bytes (denominator) {share['instruction_bytes']:>9,}")
    print(f"  volatile payload (rep.)       {VOLATILE_PAYLOAD_BYTES_REP:>10,}")
    print(f"  total prompt bytes (rep.)     {share['total_prompt_bytes']:>10,}")
    print(f"  stable share of INSTRUCTION   {share['stable_share_of_instruction']:.1%}   <-- the claim that holds")
    print(f"  stable share of TOTAL PROMPT  {share['stable_share_of_total']:.1%}   <-- NOT the claim; payload dominates")
    print("  (the cost model below is anchored on the measured $4.41/cycle, so")
    print("   the token split only feeds the tiny cross-cycle term)")

    print("\n[3] The two caching regimes priced separately\n")
    print("  INTRA-CYCLE (turn repetition): already priced into the $4.41 baseline.")
    print("    The prompt is rendered once per cycle and byte-identical across turns,")
    print("    so turn 2+ re-sends the whole conversation and can cache the fixed")
    print("    payload. The v28 stable-prefix reorder does NOT change this regime.")
    print("  CROSS-CYCLE (prefix reuse): the reorder's only direct effect.")
    print("    Turn 1 of a cycle can cache the ~10.5K-token stable prefix instead of")
    print("    the few hundred bytes the old order allowed. But cycle cadence is")
    print("    hours/days > 1h cache TTL, so survival ~ 0 in steady state.")
    print(f"    Bound at full survival: ${cross_cycle_saving_per_cycle(0.7, 1.0):.3f}/cycle (R=0.7).")

    print("\n[4] Projected median (base assumptions)\n")
    print(f"  Assumptions: routine share R={ROUTINE_SHARE_BASE:.2f}, "
          f"input-cost share={INPUT_COST_SHARE_BASE:.2f}, "
          f"cross-cycle survival={CROSS_CYCLE_SURVIVAL_BASE:.0f}")
    # Honest case: the reorder does not move the intra-cycle share, and cross-
    # cycle survival is 0, so cache read share stays at the baseline 0.55.
    honest = project_cycle_cost(ROUTINE_SHARE_BASE, BASELINE_CACHE_READ_SHARE,
                                CROSS_CYCLE_SURVIVAL_BASE)
    print(f"  Honest case (reorder alone, share stays {BASELINE_CACHE_READ_SHARE:.2f}): "
          f"${honest:.2f}/cycle")
    # Direction-target case: cache read share genuinely rises to 0.80.
    for r in (0.70, 0.85, 0.90):
        c = project_cycle_cost(r, 0.80, CROSS_CYCLE_SURVIVAL_BASE)
        print(f"  Direction-target (share=0.80, R={r:.2f}): ${c:.2f}/cycle")

    print("\n[5] Sensitivity\n")
    print(f"  {'routine share':>14} {'tier-only cost':>16} {'+ share 0.80':>14}")
    for r in (0.30, 0.50, 0.70, 0.85, 0.90, 1.00):
        tier_only = project_cycle_cost(r, BASELINE_CACHE_READ_SHARE, 0.0)
        with_target = project_cycle_cost(r, 0.80, 0.0)
        print(f"  {r:>14.2f} {('$' + f'{tier_only:.2f}'):>16} {('$' + f'{with_target:.2f}'):>14}")
    print("  (input-cost share fixed at 0.90; cross-cycle survival 0)")
    print("\n  Cache-share sensitivity at R=0.70 (the reorder's plausible range):")
    for share in (0.55, 0.60, 0.70, 0.80):
        print(f"    share {share:.2f} -> ${project_cycle_cost(0.70, share, 0.0):.2f}/cycle")
    print("\n  Cross-cycle survival sensitivity at R=0.70, share 0.55:")
    for surv in (0.0, 0.10, 1.0):
        print(f"    survival {surv:.2f} -> ${project_cycle_cost(0.70, 0.55, surv):.2f}/cycle")

    print("\n[6] Verdict\n")
    print("  The projected median is well ABOVE the $1.50 headline target.")
    print(f"  Base case (tier mix R=0.7 alone): ${project_cycle_cost(0.70, 0.55, 0.0):.2f}/cycle.")
    print(f"  Full range (R=0.3-0.9, tier only): $"
          f"{project_cycle_cost(0.90, 0.55, 0.0):.2f} - ${project_cycle_cost(0.30, 0.55, 0.0):.2f}/cycle.")
    print("  Reaching $1.50 needs BOTH the direction's cache-read-share target (0.80)")
    print("  AND a routine share of ~85%+. The prefix reorder alone cannot deliver")
    print("  the share: the only cross-cycle-cacheable content is the ~10.5K-token")
    print("  stable block (vs ~1.5M input tokens/cycle), and the volatile payload")
    print("  changes every cycle. A falsified target is a useful result; a number")
    print("  tuned backwards from $1.50 is not.")


if __name__ == "__main__":
    main()
