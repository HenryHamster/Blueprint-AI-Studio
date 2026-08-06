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
    (gpt-5.6-sol). Routine share R is read from the MEASURED effort-tier split
    checked in alongside this script (measured_effort_tier_split.json, written
    by strategic_effort_tier_evidence.py --write-json). R is currently
    UNMEASURED, so the headline is reported UNVERIFIED rather than at an
    assumed 0.70 (the previous constant was the defect). Fail-closed
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

The module also projects MONTHLY spend (criterion c-3fd62b9d). The criterion's
envelope covers BOTH halves of the cost envelope, so the monthly figure is two
terms:

  * DECISION LOOP — per-cycle cost x a cycle-volume anchor. The anchors are
    cited — the measured realized volume (59 cycles / 7-day measurement
    window, direction d-e860fe17 measured basis) and the cadence-backstop
    floor from the ``strategic_max_interval_minutes`` config default
    (runtime_config_state.py:830).
  * TASK EXECUTION — per-task cost x a task-volume anchor, sourced from the
    same VM/LLM cost accounting that backs the existing per-task cost
    reporting (efficiency_snapshot cost_per_verified_task /
    total_cost_per_verified_task, built from get_task_total_vm_cost_usd +
    _task_llm_cost). The measured basis: the non-strategic fleet that merged
    75 PRs over the 7-day window cost $15.46 LLM + $8.00 VM.

No ``target_concurrency`` configuration key exists for the strategic decision
loop, and no per-task cost/volume is fabricated either: when any input is
unavailable the projection refuses to guess and says so explicitly rather than
silently dropping a term to zero.
"""

from __future__ import annotations

import json
import math
import textwrap
from pathlib import Path
from typing import Any, Mapping, cast

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
INPUT_COST_SHARE_BASE = 0.90          # input(+create+read) share of the $4.41
CROSS_CYCLE_SURVIVAL_BASE = 0.0       # P(cache survives the inter-cycle gap)

# Sensitivity points for the routine share -- scenario inputs, NOT the headline.
# The headline routine share is read from the measured effort-tier split
# (measured_effort_tier_split.json), never assumed: the original defect was a
# hardcoded 0.70 constant driving the '34.9% below baseline' headline on an
# UNOBSERVED value. Keeping the table makes the next defect visible the same way.
SENSITIVITY_ROUTINE_SHARES = (0.30, 0.50, 0.70, 0.85, 0.90, 1.00)

# Durable address of the measured routine share R: the checked-in split written
# by strategic_effort_tier_evidence.py --write-json. The headline derives from
# this file and fails closed when R is unmeasured.
MEASURED_SPLIT_PATH = Path(__file__).resolve().parent / "measured_effort_tier_split.json"

# ---------------------------------------------------------------------------
# Cycle-volume inputs (each cited)
# ---------------------------------------------------------------------------

# Strategic-cadence backstop (existing configuration). runtime_config_state.py
# defaults ``strategic_max_interval_minutes`` to 1,440: the wall-clock floor
# that forces a strategic spawn when this long passes without one. 1,440 min is
# a daily backstop -> ~30.4 cycles/month. This is a FLOOR, not the realized
# volume — the cadence spawner also fires on research frequency, operator
# refresh, and state-change wakes (autonomous_research.py:1275-1304).
STRATEGIC_MAX_INTERVAL_MINUTES = 1_440

# Calendar-month length used to scale the 7-day measurement window (the same
# 30.44-day convention the direction's monthly-envelope framing uses).
DAYS_PER_MONTH = 30.44
MINUTES_PER_MONTH = DAYS_PER_MONTH * 24 * 60

# Realized cycle volume over the measurement window. The direction's measured
# basis (d-e860fe17) records 59 strategic cycles over the 7-day window
# ($276.87 total LLM spend / $261.33 strategic across 59 cycles at $4.43 each).
# Scaled to a month this is ~256.6 cycles/month.
MEASURED_CYCLES_PER_7_DAYS = 59

# The cost envelope the monthly projection is judged against (direction
# d-e860fe17 success evidence: "within $3,000"). The envelope covers decision
# loop AND task execution — one budget question, not two.
MONTHLY_ENVELOPE_USD = 3_000.0

# ---------------------------------------------------------------------------
# Task-execution inputs (criterion c-3fd62b9d's second half)
# ---------------------------------------------------------------------------

# Measured per-task execution cost, sourced from the project's VM/LLM cost
# accounting — the same figures that back the existing per-task cost reporting
# (efficiency_snapshot cost_per_verified_task / total_cost_per_verified_task,
# built from get_task_total_vm_cost_usd + _task_llm_cost). Direction
# d-e860fe17 measured basis: over the 7-day window the non-strategic fleet
# that merged 75 PRs cost $15.46 LLM and $8.00 VM. Per-task execution cost is
# therefore (LLM + VM) / tasks.
MEASURED_FLEET_LLM_USD_PER_7_DAYS = 15.46
MEASURED_FLEET_VM_USD_PER_7_DAYS = 8.00
MEASURED_TASKS_PER_7_DAYS = 75


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


def load_measured_split() -> dict[str, Any]:
    """Load the checked-in measured effort-tier split (the durable address of R).

    Returns {} when the file is absent or unreadable — the fail-closed path
    treats that exactly like an unmeasured split.
    """
    try:
        with open(MEASURED_SPLIT_PATH, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def routine_share_from_split(split: Mapping[str, Any]) -> float | None:
    """The measured routine share R, or None when R is unmeasured (fail-closed).

    A split is measured only when ``measurement_status == 'measured'`` AND its
    ``routine_share`` is a finite, non-negative number. Anything else — an
    unmeasured status, a missing/malformed file, a null or non-finite share —
    returns None, so the headline never falls back to an assumed value.
    """
    if split.get("measurement_status") != "measured":
        return None
    value = split.get("routine_share")
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0.0:
        return None
    return float(value)


def headline_routine_share() -> float | None:
    """R used for the headline claim: the measured split's routine share, or
    None when R is unmeasured (the fail-closed case)."""
    return routine_share_from_split(load_measured_split())


def headline_claim() -> dict[str, object]:
    """The >=30%-below-baseline headline, derived from the measured split.

    ``available=False`` (claim UNVERIFIED) when R is unmeasured: the projection
    substitutes no assumed share — the original defect. When measured, the
    claim is computed at the measured R with the honest-case cache settings
    (baseline cache-read share, zero cross-cycle survival) — the same base the
    claim has always used, so the restatement is apples-to-apples.
    """
    r = headline_routine_share()
    if r is None:
        return {
            "available": False,
            "claim_verified": False,
            "routine_share": None,
            "below_baseline_pct": None,
            "cost_per_cycle_usd": None,
            "threshold_pct": 0.30,
            "reason": (
                "routine share R is unmeasured: no completed strategic cycle "
                "with a stamped effort_tier is recorded in the measured split "
                f"({MEASURED_SPLIT_PATH.name}). The >=30%-below-baseline "
                "headline is UNVERIFIED — no assumed routine share is "
                "substituted. Regenerate the split with "
                "backend/scripts/strategic_effort_tier_evidence.py --write-json."
            ),
        }
    cost = project_cycle_cost(r, BASELINE_CACHE_READ_SHARE, CROSS_CYCLE_SURVIVAL_BASE)
    below = (BASELINE_COST_PER_CYCLE_USD - cost) / BASELINE_COST_PER_CYCLE_USD
    return {
        "available": True,
        "claim_verified": below >= 0.30,
        "routine_share": r,
        "below_baseline_pct": below,
        "cost_per_cycle_usd": cost,
        "threshold_pct": 0.30,
        "reason": None,
    }


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


def cadence_cycles_per_month() -> float:
    """Cycle-volume floor from the strategic cadence backstop, cycles/month.

    The wall-clock backstop forces a strategic spawn at least every
    ``strategic_max_interval_minutes`` (default 1,440 = daily). The monthly
    floor is therefore MINUTES_PER_MONTH / STRATEGIC_MAX_INTERVAL_MINUTES
    (~30.4). This is a floor, not the realized volume — the cadence spawner
    also fires on research frequency, operator refresh, and state wakes.
    """
    return MINUTES_PER_MONTH / STRATEGIC_MAX_INTERVAL_MINUTES


def measured_cycles_per_month() -> float:
    """Realized strategic-cycle volume scaled to a month, cycles/month.

    MEASURED_CYCLES_PER_7_DAYS (59 cycles over the 7-day measurement window,
    direction d-e860fe17 measured basis) scaled by DAYS_PER_MONTH / 7.
    """
    return MEASURED_CYCLES_PER_7_DAYS * DAYS_PER_MONTH / 7.0


def mean_task_execution_cost_usd() -> float:
    """Mean per-task execution cost (LLM + VM), USD — measured fleet basis.

    The non-strategic fleet that merged MEASURED_TASKS_PER_7_DAYS (75) PRs over
    the 7-day window cost MEASURED_FLEET_LLM_USD_PER_7_DAYS ($15.46) LLM plus
    MEASURED_FLEET_VM_USD_PER_7_DAYS ($8.00) VM (direction d-e860fe17 measured
    basis). This is the same VM/LLM accounting that backs the existing per-task
    cost reporting, so the term is a measured input, not an estimate.
    """
    return (
        MEASURED_FLEET_LLM_USD_PER_7_DAYS + MEASURED_FLEET_VM_USD_PER_7_DAYS
    ) / MEASURED_TASKS_PER_7_DAYS


def measured_tasks_per_month() -> float:
    """Realized task-execution volume scaled to a month, tasks/month.

    MEASURED_TASKS_PER_7_DAYS (75 merged PRs over the 7-day measurement window)
    scaled by DAYS_PER_MONTH / 7 — the same 30.44-day month convention used for
    the cycle volume.
    """
    return MEASURED_TASKS_PER_7_DAYS * DAYS_PER_MONTH / 7.0


def target_concurrency_status() -> dict:
    """Report whether a stored 'target concurrency' input exists.

    The success criterion (c-3fd62b9d) asks for monthly spend "at the version's
    target concurrency", but no configuration key stores a target concurrency
    for the strategic decision loop (searched runtime_config_state.py defaults
    and the ReleaseVersion model). This surfaces that gap explicitly instead of
    guessing a volume — per the task's instruction to report an unavailable
    input rather than invent one.
    """
    return {
        "available": False,
        "reason": (
            "No 'target_concurrency' configuration exists for the strategic "
            "decision loop (runtime_config_state.py defaults and the "
            "ReleaseVersion model have no such key). The projection therefore "
            "shows monthly spend at the two available volume anchors -- the "
            "measured realized volume and the cadence-backstop floor -- and "
            "reports the volume at which the $3,000 envelope binds. Grading "
            "c-3fd62b9d at a specific concurrency requires an "
            "operator-supplied target cycle volume."
        ),
    }


def project_monthly_spend(
    per_cycle_cost_usd: float,
    cycles_per_month: float | None,
) -> dict[str, object]:
    """Projected monthly spend, USD: per-cycle cost x cycle volume.

    Returns a dict so the arithmetic is explicit and a reader can check it by
    hand (monthly = per_cycle_cost x cycles_per_month). When
    ``cycles_per_month`` is None (no measured volume and no configured cadence
    available) or non-finite/negative, returns ``available=False`` with a
    reason instead of guessing a volume.
    """
    if (
        cycles_per_month is None
        or cycles_per_month < 0
        or not math.isfinite(cycles_per_month)
    ):
        return {
            "available": False,
            "reason": "cycle volume unavailable (no measured volume and no configured cadence)",
            "per_cycle_cost_usd": round(per_cycle_cost_usd, 4),
            "cycles_per_month": None,
            "monthly_spend_usd": None,
        }
    monthly = per_cycle_cost_usd * cycles_per_month
    return {
        "available": True,
        "per_cycle_cost_usd": round(per_cycle_cost_usd, 4),
        "cycles_per_month": round(cycles_per_month, 2),
        "monthly_spend_usd": round(monthly, 2),
    }


def project_monthly_spend_with_tasks(
    per_cycle_cost_usd: float,
    cycles_per_month: float | None,
    per_task_cost_usd: float | None,
    tasks_per_month: float | None,
) -> dict[str, object]:
    """Projected monthly TOTAL spend, USD: decision loop + task execution.

    The $3,000/month envelope (criterion c-3fd62b9d) is one budget question
    covering BOTH the strategist decision loop and per-task execution, so the
    monthly figure is the sum of two terms:

      * decision-loop:  per_cycle_cost_usd x cycles_per_month
      * task-execution: per_task_cost_usd x tasks_per_month

    Returns a dict so the arithmetic is explicit and the split is auditable —
    the reader can sum ``decision_loop_monthly_usd`` + ``task_execution_monthly_usd``
    and get ``total_monthly_spend_usd`` exactly.

    The same refusal-to-fabricate discipline as :func:`project_monthly_spend`
    extends to EVERY input: when any of the volume/cost inputs is unavailable
    (None), negative, or non-finite, the projection returns ``available=False``
    with a reason naming the missing input — it never substitutes a placeholder
    and never silently drops the task term to a zero to make the total look
    complete.
    """
    if (
        cycles_per_month is None
        or cycles_per_month < 0
        or not math.isfinite(cycles_per_month)
        or per_task_cost_usd is None
        or per_task_cost_usd < 0
        or not math.isfinite(per_task_cost_usd)
        or tasks_per_month is None
        or tasks_per_month < 0
        or not math.isfinite(tasks_per_month)
    ):
        missing = [
            label
            for label, value in (
                ("cycle volume", cycles_per_month),
                ("per-task cost", per_task_cost_usd),
                ("task volume", tasks_per_month),
            )
            if value is None or value < 0 or not math.isfinite(value)
        ]
        return {
            "available": False,
            "reason": (
                "monthly total unavailable: "
                + ", ".join(missing)
                + " missing or invalid — refusing to fabricate a term"
            ),
            "decision_loop_monthly_usd": None,
            "task_execution_monthly_usd": None,
            "total_monthly_spend_usd": None,
            "within_envelope": None,
        }
    decision = round(per_cycle_cost_usd * cycles_per_month, 2)
    task_exec = round(per_task_cost_usd * tasks_per_month, 2)
    total = round(decision + task_exec, 2)
    return {
        "available": True,
        "per_cycle_cost_usd": round(per_cycle_cost_usd, 4),
        "cycles_per_month": round(cycles_per_month, 2),
        "per_task_cost_usd": round(per_task_cost_usd, 4),
        "tasks_per_month": round(tasks_per_month, 2),
        "decision_loop_monthly_usd": decision,
        "task_execution_monthly_usd": task_exec,
        "total_monthly_spend_usd": total,
        "monthly_envelope_usd": MONTHLY_ENVELOPE_USD,
        "within_envelope": total <= MONTHLY_ENVELOPE_USD,
    }


def main() -> None:
    print("=" * 78)
    print("v28 projected median per-cycle strategist LLM cost")
    print("=" * 78)

    split = load_measured_split()
    r = routine_share_from_split(split)

    print("\n[0] HEADLINE claim (>=30% below the $4.41 baseline)\n")
    claim = headline_claim()
    if claim["available"]:
        print(f"  Measured routine share R = {claim['routine_share']:.4f} "
              f"({split.get('denominator_cycles', 0)} completed tiered cycles)")
        print(f"  Projected cost at measured R : ${claim['cost_per_cycle_usd']:.2f}/cycle")
        print(f"  Below $4.41 baseline         : {claim['below_baseline_pct']:.1%} "
              f"(threshold {claim['threshold_pct']:.0%})")
        if claim["claim_verified"]:
            print("  HEADLINE CLAIM HOLDS at the measured R.")
        else:
            print("  HEADLINE CLAIM FALSIFIED at the measured R — reported honestly.")
    else:
        print(f"  Routine share R is UNMEASURED (measured split: "
              f"{MEASURED_SPLIT_PATH.name}).")
        print("  The >=30%-below-baseline headline is UNVERIFIED — no assumed")
        print("  routine share is substituted. Regenerate the measured split with:")
        print("    python3 backend/scripts/strategic_effort_tier_evidence.py \\")
        print("      --days 7 --write-json backend/scripts/measured_effort_tier_split.json")

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
        ("Routine share R (measured split)",
         "UNMEASURED — no completed stamped cycles; headline UNVERIFIED" if r is None
         else f"{r:.4f} (realised split, {split.get('denominator_cycles', 0)} completed tiered cycles)"),
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
    print(f"    Bound at full survival: ${cross_cycle_saving_per_cycle(0.70, 1.0):.3f}/cycle "
          "(R=0.70 sensitivity point).")

    print("\n[4] Projected median (base assumptions)\n")
    print(f"  Inputs: input-cost share={INPUT_COST_SHARE_BASE:.2f}, "
          f"cross-cycle survival={CROSS_CYCLE_SURVIVAL_BASE:.0f}")
    if r is None:
        print("  Routine share R: UNMEASURED — no honest base case exists.")
        print("  The headline is reported UNVERIFIED (see [0]); the sensitivity")
        print("  table below shows the tier-only range across scenario inputs.")
    else:
        print(f"  Routine share R (measured): {r:.4f}")
        # Honest case: the reorder does not move the intra-cycle share, and
        # cross-cycle survival is 0, so cache read share stays at baseline 0.55.
        honest = project_cycle_cost(r, BASELINE_CACHE_READ_SHARE,
                                    CROSS_CYCLE_SURVIVAL_BASE)
        print(f"  Honest case at measured R (share stays {BASELINE_CACHE_READ_SHARE:.2f}): "
              f"${honest:.2f}/cycle")
    # Direction-target case: cache read share genuinely rises to 0.80.
    for sr in (0.70, 0.85, 0.90):
        c = project_cycle_cost(sr, 0.80, CROSS_CYCLE_SURVIVAL_BASE)
        print(f"  Direction-target (share=0.80, R={sr:.2f}): ${c:.2f}/cycle")

    print("\n[5] Sensitivity (scenario inputs — NOT the headline)\n")
    print("  The headline derives from the MEASURED split ([0]); this table is")
    print("  the range the claim must survive, kept so the next defect stays visible.")
    print(f"  {'routine share':>14} {'tier-only cost':>16} {'+ share 0.80':>14}")
    for sr in SENSITIVITY_ROUTINE_SHARES:
        tier_only = project_cycle_cost(sr, BASELINE_CACHE_READ_SHARE, 0.0)
        with_target = project_cycle_cost(sr, 0.80, 0.0)
        print(f"  {sr:>14.2f} {('$' + f'{tier_only:.2f}'):>16} {('$' + f'{with_target:.2f}'):>14}")
    print("  (input-cost share fixed at 0.90; cross-cycle survival 0)")
    print("\n  Cache-share sensitivity at R=0.70 (a sensitivity point):")
    for cache_share in (0.55, 0.60, 0.70, 0.80):
        print(f"    share {cache_share:.2f} -> ${project_cycle_cost(0.70, cache_share, 0.0):.2f}/cycle")
    print("\n  Cross-cycle survival sensitivity at R=0.70, share 0.55:")
    for surv in (0.0, 0.10, 1.0):
        print(f"    survival {surv:.2f} -> ${project_cycle_cost(0.70, 0.55, surv):.2f}/cycle")

    print("\n[6] Verdict\n")
    claim = headline_claim()
    if claim["available"]:
        print(f"  Measured R = {claim['routine_share']:.4f}: "
              f"${claim['cost_per_cycle_usd']:.2f}/cycle = "
              f"{claim['below_baseline_pct']:.1%} below the $4.41 baseline.")
        if claim["claim_verified"]:
            print("  The >=30%-below-baseline headline HOLDS at the measured R.")
        else:
            print("  The >=30%-below-baseline headline is FALSIFIED at the measured R.")
            print("  Reporting that falsification is the honest result, not a failure.")
    else:
        print("  Routine share R is UNMEASURED, so the >=30%-below-baseline headline")
        print("  is UNVERIFIED. The claim needs R >= ~0.60 (tier-only); the sensitivity")
        print("  table shows the full range. A measured R below ~0.60 FALSIFIES the")
        print("  claim — reporting that is the honest result, not a failure.")
    print("  Reaching $1.50 needs BOTH the direction's cache-read-share target (0.80)")
    print("  AND a routine share of ~85%+. The prefix reorder alone cannot deliver")
    print("  the share: the only cross-cycle-cacheable content is the ~10.5K-token")
    print("  stable block (vs ~1.5M input tokens/cycle), and the volatile payload")
    print("  changes every cycle. A falsified target is a useful result; a number")
    print("  tuned backwards from $1.50 is not.")

    print("\n[7] Projected monthly spend at a cycle volume (criterion c-3fd62b9d)\n")
    tc = target_concurrency_status()
    print(f"  Target-concurrency input: {'AVAILABLE' if tc['available'] else 'NOT AVAILABLE'}")
    for line in textwrap.wrap(tc["reason"], width=96):
        print(f"      {line}")
    print("\n  Concurrency assumption: cycles run serially — the cadence spawner gates")
    print("    on no-active-strategic (autonomous_research.py:1732-1733), so at most 1")
    print("    concurrent strategic cycle per project at a time.")
    print("\n  Cycle-volume anchors (cycles/month):")
    print(f"    Measured realized volume : {measured_cycles_per_month():>8.1f}  "
          f"({MEASURED_CYCLES_PER_7_DAYS} cycles / 7-day window, d-e860fe17 measured basis)")
    print(f"    Cadence-backstop floor   : {cadence_cycles_per_month():>8.1f}  "
          f"(strategic_max_interval_minutes={STRATEGIC_MAX_INTERVAL_MINUTES} default)")
    print("\n  Task-volume anchor (tasks/month):")
    print(f"    Measured task volume    : {measured_tasks_per_month():>8.1f}  "
          f"({MEASURED_TASKS_PER_7_DAYS} tasks / 7-day window, d-e860fe17 measured basis)")
    print(f"  Per-task execution cost   : ${mean_task_execution_cost_usd():.4f}/task "
          f"(fleet ${MEASURED_FLEET_LLM_USD_PER_7_DAYS:.2f} LLM + "
          f"${MEASURED_FLEET_VM_USD_PER_7_DAYS:.2f} VM over "
          f"{MEASURED_TASKS_PER_7_DAYS} tasks — the VM/LLM accounting behind "
          f"cost_per_verified_task / total_cost_per_verified_task)")

    print("\n  Monthly spend = decision-loop term + task-execution term")
    print("  R is unmeasured, so the per-cycle costs below are SCENARIO labels")
    print("  (sensitivity points), not a verified base.")
    base_cost = project_cycle_cost(0.70, BASELINE_CACHE_READ_SHARE, 0.0)
    target_cost = project_cycle_cost(0.85, 0.80, 0.0)
    for label, cost in (
        ("Scenario R=0.70 (sensitivity point), share 0.55", base_cost),
        ("Direction-target (R=0.85, share 0.80)", target_cost),
    ):
        for vol_label, volume in (
            ("measured volume", measured_cycles_per_month()),
            ("cadence floor", cadence_cycles_per_month()),
        ):
            m = project_monthly_spend(cost, volume)
            print(f"    {label}: ${cost:.2f}/cycle x {volume:>7.1f} ({vol_label}) "
                  f"= ${m['monthly_spend_usd']:>8,.2f}/month (decision loop)")
        # Task-execution term + total at the measured volume (the auditable split).
        mt = project_monthly_spend_with_tasks(
            cost, measured_cycles_per_month(),
            mean_task_execution_cost_usd(), measured_tasks_per_month(),
        )
        print(f"      + task execution at measured volume: "
              f"${mt['task_execution_monthly_usd']:>8,.2f}/month "
              f"(${mt['per_task_cost_usd']:.4f}/task x {mt['tasks_per_month']:.1f} tasks)")
        print(f"      TOTAL = ${mt['total_monthly_spend_usd']:>8,.2f}/month "
              f"({'within' if mt['within_envelope'] else 'OVER'} the "
              f"${mt['monthly_envelope_usd']:,.0f} envelope)")
    print("\n  $3,000/month envelope vs the scenario R=0.70 TOTAL at the measured volume:")
    base_total = project_monthly_spend_with_tasks(
        base_cost, measured_cycles_per_month(),
        mean_task_execution_cost_usd(), measured_tasks_per_month(),
    )
    print(f"    decision loop : ${base_total['decision_loop_monthly_usd']:>8,.2f}/month "
          f"(${base_cost:.2f}/cycle x {base_total['cycles_per_month']:.1f} cycles)")
    print(f"    task execution: ${base_total['task_execution_monthly_usd']:>8,.2f}/month "
          f"(${base_total['per_task_cost_usd']:.4f}/task x "
          f"{base_total['tasks_per_month']:.1f} tasks)")
    base_total_usd = cast(float, base_total["total_monthly_spend_usd"])
    print(f"    TOTAL         : ${base_total_usd:>8,.2f}/month "
          f"= {base_total_usd / MONTHLY_ENVELOPE_USD:.1%} of the "
          f"${MONTHLY_ENVELOPE_USD:,.0f} envelope "
          f"({'within' if base_total['within_envelope'] else 'OVER'})")
    print("    The decision-loop-only half binds at "
          f"{MONTHLY_ENVELOPE_USD / base_cost:,.0f} cycles/month "
          f"(~{MONTHLY_ENVELOPE_USD / base_cost / measured_cycles_per_month():.1f}x "
          f"the measured volume); the combined envelope binds at a lower volume, "
          f"but task volume does not scale with cycle volume at the measured basis, "
          f"so no combined binds-at figure is fabricated.")
    print("\n  If a projection input were unavailable, the projection would refuse to")
    print("  guess rather than fabricate a number:")
    print(f"    no cycle volume : {project_monthly_spend(base_cost, None)}")
    print(f"    no per-task data: {project_monthly_spend_with_tasks(base_cost, measured_cycles_per_month(), None, None)}")


if __name__ == "__main__":
    main()
