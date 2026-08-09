#!/usr/bin/env python3
"""Project v28's median per-cycle strategist (strategic_review) LLM cost.

v28's headline goal promises a median strategist cycle under $1.50 (down from a
measured $7.52 baseline). Nothing in the repo computes that number; this module
derives
the projected median from stated inputs, every one cited below, and prices the
two caching regimes SEPARATELY:

  * INTRA-CYCLE turn repetition  — within one cycle, repeated turns re-send the
    fixed prompt + accumulated conversation. Anthropic's automatic prefix cache
    matches the whole fixed payload (TTL 5 min, extended to 1 h when a cached
    prefix keeps being re-sent). This is the DOMINANT caching regime and it is
    ALREADY priced into the measured $7.52 baseline. The v28 prompt reorder
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
  * Baseline per-cycle cost       $7.52/cycle — the MEAN of the recorded
    baseline readings' cost_per_cycle (docs/strategic-cycle-cost-baseline.md,
    BASELINE_READINGS below), derived at module load so the anchor can never
    drift from the instrument again. The earlier task-provided $4.41/cycle
    constant (design doc d6c4d7fb-… cites $4.43; strategic_review_effort.py
    docstring cites "~$4.43 per cycle") is superseded by the recorded baseline.
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
    (gpt-5.6-sol). Routine share R is read from the effort-tier split checked
    in alongside this script (measured_effort_tier_split.json, written by
    strategic_effort_tier_evidence.py --write-json). The split is accepted in
    THREE states: 'measured' (a realised split over stamped cycles), 'replayed'
    (the fail-closed replayed LOWER BOUND, produced with --replay -- a lower
    bound on the classifier's share, never a realised split; it needs only the
    historical StrategicCycle rows plus the checked-in classifier code, so it
    breaks the promotion deadlock that waiting for serving stamps could never
    clear), and 'unmeasured' (neither available -- headline UNVERIFIED, no
    assumed 0.70; the previous constant was the defect). A measured share is
    always preferred over a replayed lower bound; a replayed lower bound is
    explicitly labelled as a lower bound and never presented as measured.
    Fail-closed classifier (backend/app/strategic_review_effort.py) means
    dirty/recovering periods push R down.
  * Pricing (per 1M tokens)       backend/app/llm_cost_attribution.py:
      gpt-5.6-sol:   input $5,  output $30, cache read $0.50, cache create $6.25
      gpt-5.6-terra: input $2.5, output $15, cache read $0.25, cache create $3.125
      cache read rate = 0.1 x input; cache create rate = 1.25 x input.
  * Cache read share              0.55 baseline for strategic_review (design
    doc); target >= 0.80 "achieved via a stable cacheable prefix".

Tokens are estimated from measured bytes at 4 bytes/token (standard English-text
approximation; the byte counts themselves are exact, and the cost model is
anchored on the measured $7.52 baseline, so the token estimate only affects the
tiny cross-cycle term).

Statistic label: each recorded ``cost_per_cycle`` is a MEAN over its cohort's
cycles (``per_event_usd = total_usd / cycle_count`` in
llm_cost_attribution.py), while the projection's headline claim is about a
MEDIAN per-cycle cost. The projection anchors on the mean of the recorded
per-reading means and REPORTS the recorded median alongside it
(:func:`baseline_per_cycle_stats`), so the mean-vs-median gap is stated rather
than silently conflated.

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

The module ALSO records the strategic-cycle cost BASELINE as a durable, dated
measurement record (docs/strategic-cycle-cost-baseline.md). The baseline is the
stated thing future reduction claims are measured against -- section [8] and
:func:`baseline_record` carry it verbatim with cohort provenance (requested
window, cohort task count, whether the cohort hit the 5000-row cap), derived the
same way the efficiency-snapshot read surface computes it.
"""

from __future__ import annotations

import json
import math
import statistics
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

# ---------------------------------------------------------------------------
# Measured / stated inputs
# ---------------------------------------------------------------------------

# Measured per-cycle cost and per-call inclusive input. The per-cycle baseline
# anchor is DERIVED from the recorded baseline readings (defined below) so it
# can never drift from the instrument; MEAN_INPUT_TOKENS_PER_CALL is
# task-provided (design doc corroborates at 737,568).
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
INPUT_COST_SHARE_BASE = 0.90          # input(+create+read) share of the baseline
CROSS_CYCLE_SURVIVAL_BASE = 0.0       # P(cache survives the inter-cycle gap)

# Sensitivity points for the routine share -- scenario inputs, NOT the headline.
# The headline routine share is read from the effort-tier split
# (measured_effort_tier_split.json), never assumed: the original defect was a
# hardcoded 0.70 constant driving the '34.9% below baseline' headline on an
# UNOBSERVED value. The split may carry a measured realised share OR a
# fail-closed replayed LOWER BOUND (measurement_status 'replayed' /
# bound_kind 'fail_closed_lower_bound'); both drive the headline at their own
# labelled value, and neither is an assumed constant. Keeping the table makes
# the next defect visible the same way.
SENSITIVITY_ROUTINE_SHARES = (0.30, 0.50, 0.70, 0.85, 0.90, 1.00)

# Durable address of the routine share R: the checked-in split written by
# strategic_effort_tier_evidence.py --write-json. The headline derives from
# this file and fails closed when R is neither measured nor replayed.
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

# ---------------------------------------------------------------------------
# Baseline measurement record (docs/strategic-cycle-cost-baseline.md)
# ---------------------------------------------------------------------------

# Row cap applied to the efficiency-snapshot terminal-task cohort
# (efficiency_snapshot.py:_COHORT_CAP). A baseline reading is comparable across
# time ONLY when its provenance says whether the cohort hit this cap: a capped
# cohort is a recency SAMPLE (most-recently-updated first), so every aggregate
# it feeds -- including the strategic-cycle cost per event -- describes that
# narrower span, not the full requested window.
COHORT_CAP = 5000

# Requested window for the efficiency-snapshot "current" axes
# (efficiency_snapshot.py:_AXIS_WINDOW_DAYS).
AXIS_WINDOW_DAYS = 7

# The two strategic-cycle cost readings recorded 2026-08-07, verbatim. Each
# field maps to the agent efficiency-snapshot read surface:
#   cycle_count              = llm_cost_attribution.strategic_cycle_cost_per_event.cycle_count
#   strategic_cycle_cost_usd = llm_cost_attribution.strategic_cycle_cost_per_event.total_usd
#   cost_per_cycle           = llm_cost_attribution.strategic_cycle_cost_per_event.per_event_usd
#   total_llm_usd            = llm_cost_attribution.total_llm_usd
#   unpriced_coverage        = llm_cost_attribution.unpriced_coverage
#   cohort_task_count        = data.cohort_task_count
#   cohort_effective_window_* = data.cohort_effective_window_* (agent surface)
# cohort_capped is DERIVED by :func:`baseline_record` from cohort_task_count vs
# COHORT_CAP, so a reading that records the count can never silently mislabel
# the sample.
#
# The two historical readings predate the agent endpoint exposing the effective
# window (see docs/strategic-cycle-cost-baseline.md). Their recorded window is
# the requested query-window bounds the reading's endpoint actually queried
# (7 days ending on the reading date); the exact effective span of the capped
# 5000-row cohort was not captured at the time. Readings taken after the
# endpoint change record the true effective span from the same read surface.
BASELINE_READINGS: list[dict[str, Any]] = [
    {
        "reading": 1,
        "date": "2026-08-07",
        "integration_head": "9e25341c",
        "cycle_count": 212,
        "strategic_cycle_cost_usd": 1610.081459,
        "cost_per_cycle": 7.594724,
        "total_llm_usd": 3439.92,
        "unpriced_coverage": 0.9752,
        "cohort_task_count": 5000,
        "cohort_effective_window_start": "2026-07-31T00:00:00+00:00",
        "cohort_effective_window_end": "2026-08-07T00:00:00+00:00",
    },
    {
        "reading": 2,
        "date": "2026-08-07",
        "integration_head": "83813e87",
        "cycle_count": 212,
        "strategic_cycle_cost_usd": 1618.175162,
        "cost_per_cycle": 7.632902,
        "total_llm_usd": 3446.586451,
        "unpriced_coverage": 0.9752,
        "cohort_task_count": 5000,
        "cohort_effective_window_start": "2026-07-31T00:00:00+00:00",
        "cohort_effective_window_end": "2026-08-07T00:00:00+00:00",
    },
    {
        "reading": 3,
        "date": "2026-08-08",
        "integration_head": "5b97819",
        "cycle_count": 230,
        "strategic_cycle_cost_usd": 1683.2,
        "cost_per_cycle": 7.318261,
        "total_llm_usd": 3390.42,
        "unpriced_coverage": 0.72,
        "cohort_task_count": 5000,
        # First reading to record the true effective window from the agent endpoint
        # (postdates the cohort_effective_window_* exposure in the read surface).
        "cohort_effective_window_start": "2026-08-01T00:00:00+00:00",
        "cohort_effective_window_end": "2026-08-08T00:00:00+00:00",
    },
]

# Separate ledger for complete ``by_serving_sha`` rows. Never append these to
# BASELINE_READINGS: that list is the frozen comparison anchor.
SERVING_SHA_READINGS: list[dict[str, Any]] = []

# A directional cost verdict needs more than a mathematically valid mean. Thirty
# cycles is the explicit minimum evidence rule for this diagnostic surface.
# Keep this as a named policy so a thin cohort cannot silently become a claim.
MINIMUM_COST_VERDICT_N = 30

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

    ``baseline`` is BASELINE_COST_PER_CYCLE_USD — the mean of the recorded
    baseline readings' ``cost_per_cycle`` (section [8]), never a task-provided
    constant. The claim is a MEDIAN while the anchor is a MEAN; the gap is
    labelled by :func:`baseline_per_cycle_stats`.
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
    """Load the checked-in effort-tier split (the durable address of R).

    Returns {} when the file is absent or unreadable — the fail-closed path
    treats that exactly like an unmeasured split.
    """
    try:
        with open(MEASURED_SPLIT_PATH, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _finite_share(value: Any) -> float | None:
    """A finite, non-negative routine share, or None when the value is invalid."""
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0.0:
        return None
    return float(value)


def routine_share_source(split: Mapping[str, Any]) -> dict[str, Any]:
    """The routine share that drives the headline, plus how it was obtained.

    Returns ``{"routine_share": float | None, "status": str, "bound_kind":
    str | None, "denominator_cycles": int | None}`` where
    ``denominator_cycles`` is ALWAYS the basis the returned share is a
    proportion of (never the other view's denominator), and ``status`` is one
    of:

      * ``'measured'``  — a REALISED measured share: either a split with
        ``measurement_status == 'measured'`` and a finite non-negative
        ``routine_share``, or a REPLAYED payload that preserves a finite
        realised share in its ``split['routine']`` view (the measured view the
        replay payload keeps alongside the lower bound). The replayed payload's
        measured view may preempt the lower bound ONLY when it covers the whole
        replayed basis (``measured_denominator_cycles >= denominator_cycles``).
        A stamped subset — e.g. 1 stamped cycle inside a 3-cycle replay — is a
        strictly narrower basis than the fail-closed bound, which already folds
        those stamps in, so preferring it would over-claim the headline on the
        smaller sample; in that case the replayed bound drives instead.
      * ``'replayed'``  — the fail-closed replayed LOWER BOUND: a replayed
        split (``measurement_status == 'replayed'``,
        ``bound_kind == 'fail_closed_lower_bound'``) whose top-level
        ``routine_share`` is a finite, non-negative number over a positive
        ``denominator_cycles``. This is a LOWER BOUND on the classifier's share,
        never a realised split.
      * ``'unmeasured'`` — everything else: no measured share and no valid
        replayed bound. ``routine_share`` is None so the headline fails closed
        (no assumed share).
    """
    measured_view = split.get("split")
    measured_view = measured_view if isinstance(measured_view, Mapping) else {}

    def _count(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(float(value)) or value < 0:
            return None
        return int(value)

    replay_denominator = _count(split.get("denominator_cycles"))

    if split.get("measurement_status") == "measured":
        value = _finite_share(split.get("routine_share"))
        if value is not None:
            return {
                "routine_share": value,
                "status": "measured",
                "bound_kind": None,
                "denominator_cycles": (
                    _count(split.get("measured_denominator_cycles"))
                    if split.get("measured_denominator_cycles") is not None
                    else replay_denominator
                ),
            }

    # A replayed payload keeps the realised measured view in ``split['routine']``
    # over ``measured_denominator_cycles`` stamped cycles. It preempts the lower
    # bound only when that stamped basis is not NARROWER than the replayed one:
    # the replay already folds every stamp in, so a stamped subset is strictly
    # less evidence, and quoting its share against the replay denominator is the
    # over-claim this branch exists to prevent.
    if split.get("measurement_status") == "replayed":
        value = _finite_share(measured_view.get("routine"))
        measured_denominator = _count(split.get("measured_denominator_cycles"))
        covers_replay = (
            measured_denominator is not None
            and measured_denominator > 0
            and (replay_denominator is None or measured_denominator >= replay_denominator)
        )
        if value is not None and covers_replay:
            return {
                "routine_share": value,
                "status": "measured",
                "bound_kind": None,
                "denominator_cycles": measured_denominator,
            }

    # The fail-closed replayed LOWER BOUND (only when no measured view exists,
    # or the measured view covers a strictly narrower basis than the replay).
    if (
        split.get("measurement_status") == "replayed"
        and split.get("bound_kind") == "fail_closed_lower_bound"
        and replay_denominator is not None
        and replay_denominator > 0
    ):
        value = _finite_share(split.get("routine_share"))
        if value is not None:
            return {
                "routine_share": value,
                "status": "replayed",
                "bound_kind": "fail_closed_lower_bound",
                "denominator_cycles": replay_denominator,
            }

    return {
        "routine_share": None,
        "status": "unmeasured",
        "bound_kind": None,
        "denominator_cycles": None,
    }


def routine_share_from_split(split: Mapping[str, Any]) -> float | None:
    """The routine share R that drives the headline, or None when unavailable.

    Delegates to :func:`routine_share_source`: a measured share (realised split
    or the measured view preserved inside a replayed payload), else the
    fail-closed replayed lower bound. Returns None when neither exists, so the
    headline never falls back to an assumed value.
    """
    return routine_share_source(split)["routine_share"]


def headline_routine_share() -> float | None:
    """R used for the headline claim: the measured or replayed split's routine
    share, or None when neither is available (the fail-closed case)."""
    return routine_share_source(load_measured_split())["routine_share"]


# The exact label the headline must carry when the driving share is the
# fail-closed replayed LOWER BOUND (a replayed split is never presented as
# measured).
REPLAYED_LOWER_BOUND_LABEL = (
    "at the fail-closed replayed lower bound "
    "(replayed split, NOT a measured realized split)"
)


def headline_claim() -> dict[str, object]:
    """The >=30%-below-baseline headline, derived from the split on disk.

    ``available=False`` (claim UNVERIFIED) when R is neither measured nor
    replayed: the projection substitutes no assumed share — the original
    defect. When the driving share is the fail-closed replayed LOWER BOUND, the
    headline is reported AT that bound and explicitly labelled as a lower bound
    — ``claim_verified`` is True ONLY when the lower bound itself clears the
    >=30%-below-baseline threshold; a bound below it is reported UNVERIFIED,
    never over-claimed. A measured share always takes precedence over a
    replayed lower bound, but only when the measured basis is not narrower
    than the replayed one (see :func:`routine_share_source`).
    ``denominator_cycles`` is the basis the reported share is a proportion of,
    so a caller never pairs one view's numerator with the other's denominator.
    The claim is computed with the honest-case cache
    settings (baseline cache-read share, zero cross-cycle survival) — the same
    base the claim has always used, so the restatement is apples-to-apples.
    """
    source = routine_share_source(load_measured_split())
    r = source["routine_share"]
    status = source["status"]
    bound_kind = source["bound_kind"]
    stats = baseline_per_cycle_stats()
    if r is None:
        return {
            "available": False,
            "claim_verified": False,
            "routine_share": None,
            "below_baseline_pct": None,
            "cost_per_cycle_usd": None,
            "threshold_pct": 0.30,
            "routine_share_status": "unmeasured",
            "bound_kind": None,
            "denominator_cycles": None,
            "baseline_mean_usd": stats["mean_usd"],
            "baseline_median_usd": stats["median_usd"],
            "mean_median_note": stats["statistic_note"],
            "reason": (
                "routine share R is unmeasured: no measured share and no valid "
                "fail-closed replayed lower bound are recorded in the split "
                f"({MEASURED_SPLIT_PATH.name}). The >=30%-below-baseline "
                "headline is UNVERIFIED — no assumed routine share is "
                "substituted. Regenerate the split with "
                "backend/scripts/strategic_effort_tier_evidence.py --write-json "
                "(measured — needs serving stamps) or add --replay for the "
                "fail-closed lower bound (needs only historical StrategicCycle "
                "rows plus the checked-in classifier code)."
            ),
        }
    cost = project_cycle_cost(r, BASELINE_CACHE_READ_SHARE, CROSS_CYCLE_SURVIVAL_BASE)
    below = (BASELINE_COST_PER_CYCLE_USD - cost) / BASELINE_COST_PER_CYCLE_USD
    if status == "replayed":
        if below >= 0.30:
            reason = REPLAYED_LOWER_BOUND_LABEL
        else:
            reason = (
                REPLAYED_LOWER_BOUND_LABEL
                + f": the replayed lower bound R={r:.4f} itself does NOT clear "
                "the >=30%-below-baseline threshold, so the claim is UNVERIFIED "
                "at the lower bound — a measured realized share could still "
                "clear it, but the fail-closed lower bound cannot."
            )
    else:
        reason = None
    return {
        "available": True,
        "claim_verified": below >= 0.30,
        "routine_share": r,
        "below_baseline_pct": below,
        "cost_per_cycle_usd": cost,
        "threshold_pct": 0.30,
        "routine_share_status": status,
        "bound_kind": bound_kind,
        "denominator_cycles": source["denominator_cycles"],
        "baseline_mean_usd": stats["mean_usd"],
        "baseline_median_usd": stats["median_usd"],
        "mean_median_note": stats["statistic_note"],
        "reason": reason,
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


def cohort_capped(cohort_task_count: Any) -> bool | None:
    """True exactly when a cohort reached the 5000-row cap; None when unknown.

    Mirrors efficiency_snapshot's ``cohort_capped = cohort_task_count >=
    _COHORT_CAP`` so a baseline reading's provenance flag is derived the same
    way the read surface computes it. ``None`` (not False) when the count is
    missing or not a number, so a caller never mistakes "unknown" for
    "uncapped" -- the fail-closed rule this measurement record lives by.
    """
    if isinstance(cohort_task_count, bool) or not isinstance(
        cohort_task_count, (int, float)
    ):
        return None
    return float(cohort_task_count) >= COHORT_CAP


def baseline_record(
    readings: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalize and validate baseline readings with cohort provenance.

    Produces the machine-readable form of the durable baseline doc
    (docs/strategic-cycle-cost-baseline.md). For every reading it:

      * derives ``cohort_capped`` from ``cohort_task_count`` vs ``COHORT_CAP``
        (never trusts a hand-stamped flag);
      * stamps the requested window (``_AXIS_WINDOW_DAYS``) and the cap;
      * validates the cost arithmetic: ``cost_per_cycle`` must equal
        ``strategic_cycle_cost_usd / cycle_count`` (deriving it when a reading
        omits it, and flagging ``cost_per_cycle_consistent`` False when a
        recorded value disagrees).

    A reading that lacks a numeric ``cohort_task_count`` keeps ``cohort_capped
    = None`` -- provenance unknown is reported as unknown, never as uncapped.
    """
    source = list(readings) if readings is not None else BASELINE_READINGS
    normalized: list[dict[str, Any]] = []
    all_cost_consistent = True
    for reading in source:
        record = dict(reading)
        record["requested_window_days"] = AXIS_WINDOW_DAYS
        record["cohort_cap"] = COHORT_CAP
        record["cohort_capped"] = cohort_capped(record.get("cohort_task_count"))
        cycle_count = record.get("cycle_count")
        total = record.get("strategic_cycle_cost_usd")
        cost_consistent = False
        if (
            isinstance(cycle_count, (int, float))
            and not isinstance(cycle_count, bool)
            and cycle_count > 0
            and isinstance(total, (int, float))
            and not isinstance(total, bool)
        ):
            expected = total / cycle_count
            recorded = record.get("cost_per_cycle")
            if recorded is None:
                record["cost_per_cycle"] = round(expected, 6)
                cost_consistent = True
            elif (
                isinstance(recorded, (int, float))
                and not isinstance(recorded, bool)
                and abs(recorded - expected) <= 0.0005
            ):
                cost_consistent = True
        record["cost_per_cycle_consistent"] = cost_consistent
        all_cost_consistent = all_cost_consistent and cost_consistent
        normalized.append(record)
    return {
        "available": True,
        "cohort_cap": COHORT_CAP,
        "requested_window_days": AXIS_WINDOW_DAYS,
        "cost_arithmetic_consistent": all_cost_consistent,
        "readings": normalized,
    }


def baseline_per_cycle_stats(
    readings: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Mean/median reconciliation of the recorded baseline per-cycle costs.

    The projection claims a MEDIAN per-cycle cost but anchors on a MEAN: each
    recorded ``cost_per_cycle`` is ``per_event_usd = total_usd / cycle_count``
    (a mean over its cohort's cycles), and the projection uses the mean of the
    recorded per-reading means as its baseline anchor. This returns both
    statistics plus the reading count and an explicit note labelling the gap,
    so a reader never conflates the two.

    ``readings`` is normalized through :func:`baseline_record` (cohort
    provenance, cost-arithmetic validation), so the stats describe exactly the
    validated record section [8] prints.
    """
    source = baseline_record(readings)["readings"]
    values = [float(r["cost_per_cycle"]) for r in source]
    if not values:
        return {
            "available": False,
            "mean_usd": None,
            "median_usd": None,
            "reading_count": 0,
            "statistic_note": "no baseline readings recorded",
        }
    mean_usd = sum(values) / len(values)
    median_usd = statistics.median(values)
    gap_pct = abs(median_usd - mean_usd) / mean_usd
    conservative = "conservative" if mean_usd >= median_usd else "NOT conservative"
    return {
        "available": True,
        "mean_usd": mean_usd,
        "median_usd": median_usd,
        "reading_count": len(values),
        "statistic_note": (
            f"Each recorded cost_per_cycle is a MEAN over its cohort's cycles "
            f"(per_event_usd = total_usd / cycle_count), while the projection "
            f"claims a MEDIAN per-cycle cost. The projection anchors on the "
            f"mean of the {len(values)} recorded per-reading means "
            f"(${mean_usd:.4f}); the recorded median across those readings is "
            f"${median_usd:.4f} ({gap_pct:.1%} "
            f"{'higher' if median_usd > mean_usd else 'lower'}), so the mean "
            f"anchor is {conservative} relative to the recorded median."
        ),
    }


def _looks_like_git_sha(value: Any) -> bool:
    """True iff ``value`` is a plausible git commit SHA (7-40 hex chars).

    Mirrors :func:`app.deployed_sha._looks_like_git_sha` (duplicated so this
    script stays importable without the app package). A non-blank string is NOT
    a sufficient identity check: ``DeploymentState.current_sha`` initializes to
    the literal ``"unknown"`` when no deploy identity can be resolved, and
    treating that as a valid serving revision would qualify a reading whose
    cohort is attributed to a revision that does not exist.
    """
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not (7 <= len(value) <= 40):
        return False
    return all(c in "0123456789abcdef" for c in value.lower())


def serving_sha_record(
    readings: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalize ``by_serving_sha`` rows without changing the baseline.

    Missing identity or unreconciled arithmetic stays unavailable. The record
    also reports unweighted dispersion across serving slices and a qualification
    verdict based on the sum of their cycle counts. A syntactically valid record
    can therefore still have an unavailable cost verdict when its cohort is
    below :data:`MINIMUM_COST_VERDICT_N`.
    """
    source = list(readings) if readings is not None else SERVING_SHA_READINGS
    normalized: list[dict[str, Any]] = []
    all_valid = True
    for reading in source:
        record = dict(reading)
        sha = record.get("serving_sha")
        sha_valid = _looks_like_git_sha(sha)
        cycle_count = record.get("cycle_count")
        total = record.get("total_usd")
        per_event = record.get("per_event_usd")
        arithmetic_consistent = False
        if (
            isinstance(cycle_count, (int, float))
            and not isinstance(cycle_count, bool)
            and cycle_count > 0
            and math.isfinite(float(cycle_count))
            and isinstance(total, (int, float))
            and not isinstance(total, bool)
            and math.isfinite(float(total))
            and float(total) >= 0
        ):
            expected = float(total) / float(cycle_count)
            if per_event is None:
                record["per_event_usd"] = round(expected, 6)
                arithmetic_consistent = True
            elif (
                isinstance(per_event, (int, float))
                and not isinstance(per_event, bool)
                and math.isfinite(float(per_event))
                and float(per_event) >= 0
                and abs(float(per_event) - expected) <= 0.0005
            ):
                arithmetic_consistent = True
        coverage = record.get("unpriced_coverage")
        coverage_valid = coverage is None or (
            isinstance(coverage, (int, float))
            and not isinstance(coverage, bool)
            and math.isfinite(float(coverage))
            and 0.0 <= float(coverage) <= 1.0
        )
        record["serving_sha_valid"] = sha_valid
        record["cost_arithmetic_consistent"] = arithmetic_consistent
        record["reading_valid"] = sha_valid and arithmetic_consistent and coverage_valid
        all_valid = all_valid and record["reading_valid"]
        normalized.append(record)
    cycle_count = sum(
        int(record["cycle_count"])
        for record in normalized
        if record["reading_valid"]
    )
    dispersion = _per_slice_dispersion(normalized)
    qualification = _cost_verdict(
        normalized,
        all_readings_valid=all_valid,
        cycle_count=cycle_count,
    )
    return {
        "available": bool(normalized) and all_valid,
        "reading_count": len(normalized),
        "all_readings_valid": all_valid,
        "dispersion": dispersion,
        "qualification": qualification,
        "readings": normalized,
    }


def _per_slice_dispersion(readings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize validated per-slice means without inventing a one-slice SD."""
    values = [
        float(record["per_event_usd"])
        for record in readings
        if record.get("reading_valid")
    ]
    result = {
        "available": len(values) > 1,
        "slice_count": len(values),
        "mean_usd": statistics.mean(values) if values else None,
        "median_usd": statistics.median(values) if values else None,
        "stddev_usd": statistics.stdev(values) if len(values) > 1 else None,
    }
    return result


def _parse_cycle_timestamp(value: Any) -> datetime | None:
    """Parse an optional ISO cycle timestamp into an aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _projected_qualification_date(
    readings: Sequence[Mapping[str, Any]], cycle_count: int
) -> str | None:
    """Project the date the minimum n is reached from observed slice cadence."""
    if cycle_count >= MINIMUM_COST_VERDICT_N:
        return None
    bounds: list[tuple[datetime, datetime]] = []
    for record in readings:
        if not record.get("reading_valid"):
            continue
        first = _parse_cycle_timestamp(record.get("first_cycle_at"))
        last = _parse_cycle_timestamp(record.get("last_cycle_at"))
        if first is not None and last is not None:
            bounds.append((first, last))
    if not bounds:
        return None
    first = min(first for first, _ in bounds)
    last = max(last for _, last in bounds)
    elapsed_seconds = (last - first).total_seconds()
    if elapsed_seconds <= 0 or cycle_count <= 0:
        return None
    cycles_per_second = cycle_count / elapsed_seconds
    remaining = MINIMUM_COST_VERDICT_N - cycle_count
    projected = last + timedelta(seconds=remaining / cycles_per_second)
    return projected.date().isoformat()


def _cost_verdict(
    readings: Sequence[Mapping[str, Any]],
    *,
    all_readings_valid: bool,
    cycle_count: int,
) -> dict[str, Any]:
    """Return the fail-closed verdict metadata for a serving-SHA cohort."""
    projected_date = _projected_qualification_date(readings, cycle_count)
    result = {
        "available": False,
        "verdict": "unavailable",
        "reason": None,
        "n": cycle_count,
        "required_n": MINIMUM_COST_VERDICT_N,
        "projected_date": None,
    }
    if not readings or not all_readings_valid:
        result["reason"] = "one or more serving-SHA slices are invalid"
    elif cycle_count < MINIMUM_COST_VERDICT_N:
        result.update(
            verdict="insufficient_sample",
            reason=(
                f"cost verdict requires at least {MINIMUM_COST_VERDICT_N} cycles; "
                f"only {cycle_count} are observed"
            ),
            projected_date=projected_date,
        )
    else:
        result.update(available=True, verdict="available")
    return result


def serving_sha_comparison(
    reading: Mapping[str, Any],
    baseline_cost_usd: float | None = None,
) -> dict[str, Any]:
    """Compare one attributable row to the frozen baseline.

    Invalid identity or partial attribution is never evidence of a change.
    """
    record_bundle = serving_sha_record([reading])
    record = record_bundle["readings"][0]
    qualification = record_bundle["qualification"]
    baseline = (
        BASELINE_COST_PER_CYCLE_USD
        if baseline_cost_usd is None
        else baseline_cost_usd
    )
    metadata = {
        "n": qualification["n"],
        "required_n": qualification["required_n"],
        "projected_date": qualification["projected_date"],
        "dispersion": record_bundle["dispersion"],
    }
    baseline_valid = (
        isinstance(baseline, (int, float))
        and not isinstance(baseline, bool)
        and math.isfinite(float(baseline))
        and baseline > 0
    )
    invalid = not record["reading_valid"] or not baseline_valid
    if invalid or not qualification["available"]:
        return {
            "available": False,
            "verdict": "unavailable" if invalid else qualification["verdict"],
            "serving_sha": record.get("serving_sha"),
            "reason": (
                "serving-SHA reading or baseline is invalid"
                if invalid
                else qualification["reason"]
            ),
            "baseline_cost_per_cycle_usd": (
                None if invalid else float(baseline)
            ),
            "serving_cost_per_cycle_usd": None,
            "delta_usd": None,
            "below_baseline_pct": None,
            **metadata,
        }
    serving_cost = float(record["per_event_usd"])
    delta = float(baseline) - serving_cost
    return {
        "available": True,
        "verdict": "available",
        "serving_sha": record["serving_sha"],
        "cycle_count": record["cycle_count"],
        "baseline_cost_per_cycle_usd": float(baseline),
        "serving_cost_per_cycle_usd": serving_cost,
        "delta_usd": delta,
        "below_baseline_pct": delta / float(baseline),
        **metadata,
    }


# The projection's per-cycle baseline anchor is DERIVED from the recorded
# baseline readings (the mean of the recorded per-reading means) — never a
# task-provided constant. Adding a reading to BASELINE_READINGS moves the
# anchor automatically, so it cannot drift from the instrument again.
BASELINE_COST_PER_CYCLE_USD = cast(
    float, baseline_per_cycle_stats()["mean_usd"]
)


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
    source = routine_share_source(split)
    r = source["routine_share"]
    # ALWAYS the basis of the share actually being printed — never the other
    # view's denominator (a replayed payload's measured view is a stamped
    # SUBSET of the replay denominator).
    basis = source["denominator_cycles"] or 0
    stats = baseline_per_cycle_stats()

    print(f"\n[0] HEADLINE claim (>=30% below the ${BASELINE_COST_PER_CYCLE_USD:.2f} baseline)\n")
    for line in textwrap.wrap(str(stats["statistic_note"]), width=96):
        print(f"  {line}")
    claim = headline_claim()
    if claim["available"]:
        if claim["routine_share_status"] == "replayed":
            print(f"  Replayed routine share R (fail-closed LOWER BOUND) = "
                  f"{claim['routine_share']:.4f} "
                  f"({basis} completed cycles)")
            print("  Headline is AT the fail-closed replayed lower bound (replayed")
            print("  split, NOT a measured realized split).")
        else:
            print(f"  Measured routine share R = {claim['routine_share']:.4f} "
                  f"({basis} completed tiered cycles)")
        print(f"  Projected cost at the driving R : ${claim['cost_per_cycle_usd']:.2f}/cycle")
        print(f"  Below ${BASELINE_COST_PER_CYCLE_USD:.2f} baseline         : "
              f"{claim['below_baseline_pct']:.1%} "
              f"(threshold {claim['threshold_pct']:.0%})")
        if claim["claim_verified"]:
            if claim["routine_share_status"] == "replayed":
                print("  HEADLINE CLAIM HOLDS at the replayed lower bound.")
            else:
                print("  HEADLINE CLAIM HOLDS at the measured R.")
        else:
            if claim["routine_share_status"] == "replayed":
                print("  HEADLINE CLAIM NOT VERIFIED at the replayed lower bound — "
                      "reported honestly.")
            else:
                print("  HEADLINE CLAIM FALSIFIED at the measured R — reported honestly.")
        if claim["reason"]:
            for line in textwrap.wrap(str(claim["reason"]), width=96):
                print(f"      {line}")
    else:
        print(f"  Routine share R is UNMEASURED (split: "
              f"{MEASURED_SPLIT_PATH.name}).")
        print("  The >=30%-below-baseline headline is UNVERIFIED — no assumed")
        print("  routine share is substituted. Regenerate the split with:")
        print("    python3 backend/scripts/strategic_effort_tier_evidence.py \\")
        print("      --days 7 --write-json backend/scripts/measured_effort_tier_split.json")
        print("    (measured — needs serving stamps) or add --replay for the")
        print("    fail-closed lower bound (needs only historical rows)")

    print("\n[1] Measured / stated inputs (each cited to its source)\n")
    rows = [
        ("Baseline cost/cycle",
         f"${BASELINE_COST_PER_CYCLE_USD:.4f} (mean of {stats['reading_count']} "
         "recorded baseline readings — section [8])"),
        ("Mean input tokens/call (inclusive)", f"{MEAN_INPUT_TOKENS_PER_CALL:,} (task measurement; design doc 737,568)"),
        ("Baseline cache read share", f"{BASELINE_CACHE_READ_SHARE:.2f} (design doc)"),
        ("Stable prefix rendered", f"{STABLE_PREFIX_BYTES:,} B ~ {STABLE_PREFIX_TOKENS:,.0f} tokens (measured, thinker.ts)"),
        ("Volatile instruction (max denom.)", f"{VOLATILE_INSTRUCTION_BYTES:,} B ~ {VOLATILE_INSTRUCTION_TOKENS:,.0f} tokens (measured, thinker.ts)"),
        ("Volatile total (rep. payload)", f"{VOLATILE_PAYLOAD_BYTES_REP:,} B ~ {VOLATILE_PAYLOAD_BYTES_REP/4:,.0f} tokens"),
        ("Routine tier / price ratio", "gpt-5.6-terra (medium) vs gpt-5.6-sol (max) = 0.5x every rate (PR #16730, llm_cost_attribution.py)"),
        ("Cache read rate", "0.1 x input rate; cache create 1.25 x input rate (llm_cost_attribution.py)"),
        ("Routine share R (effort-tier split)",
         "UNMEASURED — no measured share and no replayed lower bound; "
         "headline UNVERIFIED" if r is None
         else (
             f"{r:.4f} (replayed fail-closed LOWER BOUND, "
             f"{basis} completed cycles — NOT a "
             "measured realized split)"
             if source["status"] == "replayed"
             else f"{r:.4f} (realised split, {basis} completed tiered cycles)"
         )),
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
    print(f"  (the cost model below is anchored on the measured "
          f"${BASELINE_COST_PER_CYCLE_USD:.2f}/cycle baseline, so")
    print("   the token split only feeds the tiny cross-cycle term)")

    print("\n[3] The two caching regimes priced separately\n")
    print(f"  INTRA-CYCLE (turn repetition): already priced into the "
          f"${BASELINE_COST_PER_CYCLE_USD:.2f} baseline.")
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
        if source["status"] == "replayed":
            print(f"  Routine share R (replayed fail-closed LOWER BOUND): {r:.4f}")
        else:
            print(f"  Routine share R (measured): {r:.4f}")
        # Honest case: the reorder does not move the intra-cycle share, and
        # cross-cycle survival is 0, so cache read share stays at baseline 0.55.
        honest = project_cycle_cost(r, BASELINE_CACHE_READ_SHARE,
                                    CROSS_CYCLE_SURVIVAL_BASE)
        print(f"  Honest case at the driving R (share stays {BASELINE_CACHE_READ_SHARE:.2f}): "
              f"${honest:.2f}/cycle")
    # Direction-target case: cache read share genuinely rises to 0.80.
    for sr in (0.70, 0.85, 0.90):
        c = project_cycle_cost(sr, 0.80, CROSS_CYCLE_SURVIVAL_BASE)
        print(f"  Direction-target (share=0.80, R={sr:.2f}): ${c:.2f}/cycle")

    print("\n[5] Sensitivity (scenario inputs — NOT the headline)\n")
    print("  The headline derives from the effort-tier split ([0]) — the measured")
    print("  share or the replayed fail-closed lower bound; this table is the range")
    print("  the claim must survive, kept so the next defect stays visible.")
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
        if claim["routine_share_status"] == "replayed":
            print(f"  Replayed R (fail-closed LOWER BOUND) = {claim['routine_share']:.4f}: "
                  f"${claim['cost_per_cycle_usd']:.2f}/cycle = "
                  f"{claim['below_baseline_pct']:.1%} below the "
                  f"${BASELINE_COST_PER_CYCLE_USD:.2f} baseline.")
            print("  The headline is AT the fail-closed replayed lower bound (replayed")
            print("  split, NOT a measured realized split).")
            if claim["claim_verified"]:
                print("  The >=30%-below-baseline headline HOLDS at the replayed lower bound.")
            else:
                print("  The >=30%-below-baseline headline is NOT verified at the replayed")
                print("  lower bound: the lower bound itself does not clear the threshold.")
                print("  A measured realized share could still clear it; the fail-closed")
                print("  lower bound cannot. Reporting that is the honest result.")
        else:
            print(f"  Measured R = {claim['routine_share']:.4f}: "
                  f"${claim['cost_per_cycle_usd']:.2f}/cycle = "
                  f"{claim['below_baseline_pct']:.1%} below the "
                  f"${BASELINE_COST_PER_CYCLE_USD:.2f} baseline.")
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
    if r is None:
        print("  R is unmeasured, so the per-cycle costs below are SCENARIO labels")
        print("  (sensitivity points), not a verified base.")
    else:
        print(f"  R = {r:.4f} ({source['status']} split) drives the headline ([0]/[6]);")
        print("  the per-cycle costs below are still SCENARIO labels (sensitivity")
        print("  points), because the monthly envelope is volume-driven, not R-driven.")
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

    print("\n[8] Baseline measurement record (docs/strategic-cycle-cost-baseline.md)\n")
    record = baseline_record()
    for reading in record["readings"]:
        capped = reading["cohort_capped"]
        capped_label = (
            "True (cohort at the 5000-row cap)"
            if capped is True
            else ("None (cohort_task_count not captured)" if capped is None else "False")
        )
        print(f"  Reading {reading['reading']} -- {reading['date']}, integration HEAD "
              f"{reading['integration_head']}:")
        print(f"    cycle_count              : {reading['cycle_count']}")
        print(f"    strategic_cycle_cost_usd : ${reading['strategic_cycle_cost_usd']}")
        print(f"    cost_per_cycle           : ${reading['cost_per_cycle']}")
        print(f"    total_llm_usd            : ${reading['total_llm_usd']}")
        print(f"    unpriced_coverage        : {reading['unpriced_coverage']}")
        print(f"    requested window (days)  : {reading['requested_window_days']}")
        print(f"    cohort_task_count        : {reading['cohort_task_count']}")
        print(f"    cohort_capped            : {capped_label}")
    print("\n  Cohort provenance: readings 1 and 2 share cycle_count 212, i.e. the same "
          "cohort. Reading 3 (2026-08-08, integration HEAD 5b97819) is the first "
          "post-promotion reading: cycle_count 230 and unpriced_coverage 0.72 "
          "(down from 0.9752), meaning 72% of spend still predates v29 serving "
          "and cannot be attributed to post-promotion cycles. Every cost lever "
          "landed so far modifies prompt assembly in "
          "backend/app/strategic_spawning.py, which executes only in the deployed "
          "backend; a flat reading on readings 1 and 2 is therefore not evidence "
          "about the levers.")

    print("[9] Serving-SHA readings (separate from the baseline anchor)\n")
    serving_record = serving_sha_record()
    if not serving_record["readings"]:
        print("  No complete serving-SHA readings recorded yet.")
        print("  Copy a complete by_serving_sha row here only after its SHA and")
        print("  per-event arithmetic are captured; incomplete attribution is")
        print("  intentionally not compared with the baseline.")
    else:
        for serving_reading in serving_record["readings"]:
            comparison = serving_sha_comparison(serving_reading)
            print(f"  serving SHA {serving_reading.get('serving_sha')!r}: "
                  f"{serving_reading.get('cycle_count')} cycles, "
                  f"${serving_reading.get('per_event_usd')} / cycle")
            if comparison["available"]:
                print(f"    vs frozen baseline ${comparison['baseline_cost_per_cycle_usd']:.4f}: "
                      f"{comparison['below_baseline_pct']:.1%} below")
            else:
                print(f"    comparison unavailable: {comparison['reason']}")
    print("  These rows are diagnostic like-for-like readings, not part of "
          "BASELINE_READINGS, and cannot move BASELINE_COST_PER_CYCLE_USD.")


if __name__ == "__main__":
    main()
