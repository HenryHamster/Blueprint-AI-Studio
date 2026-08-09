# v28 Projected Median Per-Cycle Strategist LLM Cost

**Date:** 2026-08-06
**Compute:** `python3 backend/scripts/strategic_cycle_cost_projection.py` (the checked-in
reproducible projection; this doc is the narrative form of the same model).

v28's headline goal promises a **median strategist cycle under $1.50**, down from a
measured **$7.52/cycle** baseline. Nothing in the repo computed that number — this artifact does.
It derives the projected median from stated inputs, cites each input to its source,
prices the two caching regimes separately, states every assumption, shows sensitivity,
and reports the number honestly: **the projected median is above $1.50, and the
>=30%-below-baseline headline is UNVERIFIED unless the effort-tier split carries a
measured share or a replayed fail-closed lower bound that itself clears the
threshold.**

The same script also projects **monthly spend** (§7, criterion c-3fd62b9d): the
monthly TOTAL is the decision-loop term (per-cycle cost × a cited cycle-volume anchor)
plus the task-execution term (per-task cost × a cited task-volume anchor), both sourced
from measured data and shown separately so the split is auditable. At the R=0.70
sensitivity point and the measured realized volume the total lands at **~$1,355/month**
($1,253 decision loop + $102 task execution) — comfortably within the $3,000/month
envelope, so the envelope is not the binding constraint at today's cadence.

---

## 1. Stated inputs (each cited)

| Input | Value | Source |
|---|---|---|
| Baseline per-cycle cost | **$7.52/cycle** | The **mean of the recorded baseline readings' `cost_per_cycle`** (`docs/strategic-cycle-cost-baseline.md`, `BASELINE_READINGS` / `baseline_record()` in the script): 7.5947, 7.6329, 7.3183 → mean 7.5153, median 7.5947. Derived at module load so the anchor can never drift from the instrument. The earlier task-provided **$4.41/cycle** constant (design doc `d6c4d7fb-…` cites $4.43; the `strategic_review_effort.py` docstring cites "~$4.43 per cycle") is superseded by the recorded baseline. |
| Mean input tokens per call | **741,992** | Task-provided measurement. Design doc cites 737,568. Inclusive per-call total = fresh input + cache read + cache creation (`mean_input_tokens_per_call`, `backend/app/llm_cost_attribution.py:1174-1178`). |
| Baseline cache read share | **0.55** | Design doc (strategic_review vs 0.87 elsewhere; target ≥ 0.80). |
| Stable prefix rendered | **42,073 bytes ≈ 10,518 tokens** | Measured statically: `buildStrategicStablePrefix` in `agent/src/prompts/thinker.ts` with representative params (`projectName='Blueprint AI Studio'`, `isSelfRepo=true`, 355-byte `selfImprovementBlock` from `agent/src/handlers/strategic.ts`, `PLAYBOOK_GUIDANCE`/`DEFAULT_TOOL_GUIDANCE`/`READ_ONLY_BASH_WRITE_NOTE` from `agent/src/prompt.ts`). |
| Volatile instruction text | **2,666 bytes ≈ 666 tokens** | Same static render, every per-cycle payload field set to `''`, with the largest conditional instruction blocks active (recover `798 B` + gap-memory trim `687 B` + design-doc overview `626 B` + current-activity intro `383 B` + 591 B direct literal + structural headers — the 2,666 B figure is the exact stripped render, not the author's byte estimate). |
| Volatile total (representative cycle) | **≈ 104 KB ≈ 26,000 tokens** | 2,666 B instruction + ~100 KB per-cycle payload (large `problem_model` JSON, direction portfolio, context blocks — see the payload pack in `backend/app/strategic_spawning.py`). |
| Tier mix | Routine → `gpt-5.6-terra` (medium), Consequential → `gpt-5.6-sol` (max) | PR #16730 (`backend/app/strategic_review_effort.py`). **Routine share R is read from the effort-tier split** checked in at `backend/scripts/measured_effort_tier_split.json` (written by `strategic_effort_tier_evidence.py --write-json`). The split is accepted in **three states**: **`measured`** (a realised split over stamped cycles), **`replayed`** (the fail-closed replayed **LOWER BOUND**, produced with `--replay`, labelled `measurement_status 'replayed'` / `bound_kind 'fail_closed_lower_bound'` — a lower bound on the classifier's share, never a realised split), and **`unmeasured`** (neither available). The headline reports UNVERIFIED rather than substituting an assumed 0.70 — the fail-closed wiring that replaces the old constant; a measured share is preferred over a replayed lower bound **only when its stamped basis (`measured_denominator_cycles`) covers the whole replayed basis (`denominator_cycles`)** — a stamped SUBSET is strictly narrower evidence than the bound, which already folds those stamps in, so the bound wins there rather than letting one stamped cycle flip the headline. Whichever view drives it, the reported denominator is that share's own basis, never the other view's. A replayed lower bound is never presented as measured. |
| Pricing (per 1M tokens) | sol: in `$5` / out `$30` / read `$0.50`; terra: in `$2.5` / out `$15` / read `$0.25`; cache create = 1.25× input, cache read = 0.1× input | `backend/app/llm_cost_attribution.py:50-51,89-96`. |

**Tokens are estimated from measured bytes at 4 bytes/token** (standard English-text
approximation). The byte counts are exact; the cost model is anchored on the measured
$7.52/cycle baseline, so the estimate only affects the tiny cross-cycle term (§4).

**Statistic label — mean vs median.** Each recorded `cost_per_cycle` is a **mean**
over its cohort's cycles (`per_event_usd = total_usd / cycle_count`), while the
projection's headline claim is about a **median** per-cycle cost. The projection
anchors on the mean of the recorded per-reading means ($7.5153) and reports the
recorded median ($7.5947, 1.1% higher) alongside it, so the mean-anchor is *not*
conservative relative to this sample's median — the gap is stated, not conflated.

---

## 2. The rendered stable/volatile split

Measured from the actual template render (not the author's byte estimate):

| Region | Bytes | Tokens |
|---|---|---|
| Stable prefix | 42,073 | ~10,518 |
| Volatile instruction text | 2,666 | ~666 |
| **Instruction bytes (denominator)** | **44,739** | **~11,185** |
| Volatile payload (representative) | ~104,000 | ~26,000 |
| Total prompt (representative) | ~146,000 | ~36,500 |

**Stable share of instruction bytes: 94.0%** — the claim that holds. The stable prefix
carries the large majority of *instruction* bytes.

**Stable share of total prompt bytes: 28.8%** — the flattering claim does *not* hold.
Against total prompt bytes the prefix is well under half because the interpolated
per-cycle payload dominates. The test assertion in `agent/src/prompts/thinker.test.ts`
asserts the true claim and names the denominator (instruction bytes).

---

## 3. The two caching regimes — priced separately

Conflating these is how a projection manufactures its target.

### Regime A — INTRA-CYCLE turn repetition (already in the baseline)

Within one cycle, the prompt is rendered **once** and passed to the Claude Code
session; every later turn re-sends the fixed prompt + accumulated conversation. The
automatic prefix cache matches the whole fixed payload (TTL 5 min, extended to 1 h
for a frequently re-sent prefix), so the cache-read share of the cycle's input is
large. **This regime is already priced into the measured $7.52 baseline.** The v28 stable-prefix
reorder does **not** change it: the prompt is byte-identical across turns whether the
stable block leads or trails.

### Regime B — CROSS-CYCLE prefix reuse (the reorder's only direct effect)

Across separate cycles, only the stable instruction block is byte-identical; the
volatile payload changes every cycle. Before v28, a volatile byte appeared early in
the prompt, so turn 1 of a cycle could cache only a few hundred bytes from the prior
cycle. After #16731, turn 1 can cache the full ~10.5K-token stable block — **but only
when the cache survives the inter-cycle gap.** Strategic cycles run on a
hours-to-days cadence, far past the 1-hour cache TTL, so in steady state the survival
probability is ~0 and the saving is ~$0.00/cycle.

**Bound at full survival:** ~10,518 tokens × ($5 − $0.50)/M ≈ **$0.047/cycle** (sol;
half on terra). Even in the best case this is ~1% of the baseline — the cache
boundary lever alone cannot move the median meaningfully.

---

## 4. The model

Let `R` = routine share of cycles, `ρ` = cache-read share of input tokens,
`m(ρ) = (1−ρ)·1.0 + ρ·0.1` = effective input price multiplier (read at 0.1×),
`φ_in` = input-cost share of the baseline.

Terra is exactly **0.5×** sol on every rate, so the fleet price multiplier is
`1 − 0.5R`.

```
projected cost = $7.52 · (1 − 0.5R) · [ φ_in · m(ρ_after)/m(ρ_before) + (1 − φ_in) ]
                − cross_cycle_saving(R, survival)
```

Base assumptions: `φ_in = 0.90`, `survival = 0`. The baseline is **$7.5153** — the mean
of the recorded baseline readings' `cost_per_cycle` (see §1), never a constant.
Routine share `R` is **not assumed** — it is read from the effort-tier split
(`backend/scripts/measured_effort_tier_split.json`): a **measured** realised share, or
the **replayed fail-closed LOWER BOUND** (`--replay`). When neither is available R is
UNMEASURED and the headline is reported **UNVERIFIED**; the R = 0.70 figure that
previously anchored the "honest base" is now a sensitivity scenario, not an input.

### Scenario — tier mix at R = 0.70 (the old assumed base, for reference)

The reorder does not move the intra-cycle share (`ρ` stays 0.55), and cross-cycle
survival is 0:

```
$7.52 · (1 − 0.35) = $4.88/cycle
```

This is a **scenario label, not the headline**: the >=30%-below-baseline claim built
on it is UNVERIFIED until a measured routine share is checked in.

### Direction-target case — cache share genuinely reaches 0.80

```
m(0.80)/m(0.55) = 0.28/0.505 = 0.5545
R = 0.70:  $7.52 · 0.65 · (0.90·0.5545 + 0.10) = $2.93/cycle
R = 0.85:  $7.52 · 0.575 · 0.5991           = $2.59/cycle
R = 0.90:  $7.52 · 0.55 · 0.5991            = $2.48/cycle
```

---

## 5. Sensitivity

`φ_in = 0.90`, survival = 0.

| Routine share R | Tier-only | + cache share 0.80 |
|---|---|---|
| 0.30 | $6.39 | $3.83 |
| 0.50 | $5.64 | $3.38 |
| 0.70 | $4.88 | $2.93 |
| 0.85 | $4.32 | $2.59 |
| 0.90 | $4.13 | $2.48 |
| 1.00 | $3.76 | $2.25 |

Cache-share sensitivity at R = 0.70: share 0.55 → $4.88, 0.60 → $4.49,
0.70 → $3.71, 0.80 → $2.93.

Cross-cycle survival at R = 0.70, share 0.55: 0.00 → $4.88, 0.10 → $4.88,
1.00 → $4.85 (the term is negligible by construction).

Input-cost share (φ_in) shifts everything proportionally on the input side; the
$2.25-$6.39 spread in the table is driven far more by `R` and `ρ` than by `φ_in` in the
plausible 0.80-0.95 range.

**Routine share R is the swing factor.** The >=30%-below-baseline headline requires
R ≥ 0.60 (tier-only `$7.52·(1 − 0.5R) ≤ $5.26 ⇔ R ≥ 0.60`). The table shows why R
matters: at R = 0.30 the tier-only cost is $6.39 — only ~15% below baseline — so the
claim flips within the assumption's range. The fail-closed classifier
(`strategic_review_effort.py`) routes any open message, version decision pending,
integrity conflict, prior-dirty cycle, or recent failure to consequential — so
dirty/recovering periods (like the recent 1/11 and 0/3 merge records) push R well
below 0.7. The direction's own health depends on R being high.

---

## 6. Verdict

**The >=30%-below-baseline headline is UNVERIFIED unless the effort-tier split carries a
measured share or a replayed fail-closed LOWER BOUND that itself clears the threshold.**
The projection fails closed: no assumed routine share is substituted (the old R = 0.70
constant was the defect). The checked-in address
`backend/scripts/measured_effort_tier_split.json` records the honest state and the
regeneration command.

The **replayed lower bound** is an available, explicitly-labelled input and is how the
measurement deadlock is broken: `--replay` re-runs the checked-in classifier
(`strategic_review_effort.py`) on signals reconstructed from historical
`StrategicCycle` rows, so it needs **only the historical rows plus the checked-in
classifier code — no serving stamps** (stamps cannot exist until promotion, which the
criterion that needs the measurement itself blocks). An unavailable signal a ROUTINE
verdict needs (most load-bearing: `open_message_count`) resolves to CONSEQUENTIAL, so
the replayed share is a strict **LOWER BOUND** on the classifier's full-signals share,
never the realized split. When it drives the headline, the headline is reported **AT
that bound**, labelled "at the fail-closed replayed lower bound (replayed split, NOT a
measured realized split)", and `claim_verified` is True ONLY when the bound itself
clears the >=30% threshold (R ≥ 0.60 tier-only). A bound below it is reported
UNVERIFIED with the lower-bound caveat — never an over-claim. Regenerate it with (run
with DB access):

```
python3 backend/scripts/strategic_effort_tier_evidence.py --replay --days 7 \
  --write-json backend/scripts/measured_effort_tier_split.json
```

At the measured-R boundary, the tier-only cost is $5.26/cycle at R = 0.60 (exactly 30%
below the $7.52 baseline) — the claim needs R ≥ 0.60. The full tier-only range across
R = 0.3-0.9 is $4.13-$6.39, and the projected median stays above the $1.50 headline
across the entire range (even R = 1.0 is $3.76/cycle tier-only).

Reaching $1.50 requires **both** of these to hold simultaneously:

1. The cache-read share genuinely rises to ~0.80 across the whole cycle's input —
   which the stable-prefix reorder alone **cannot deliver**, because the only
   cross-cycle-cacheable content is the ~10.5K-token stable block against ~1.5M
   input tokens per cycle, and the volatile payload changes every cycle; and
2. A routine share of ~85%+ — near the classifier's ceiling and contradicted by
   the current recovering-period signals.

At the direction's own target inputs the number lands at $2.59-2.93 (R = 0.70-0.85),
so the target is on the edge of the model's optimistically-parameterized range — but
the mechanism analysis says the share lever is not available at the claimed size. The
tier-mix lever (PR #16730) is the one reliable cost reduction, and its ceiling alone
(R = 1) is $3.76/cycle.

---

## 7. Projected monthly spend (criterion c-3fd62b9d)

The monthly TOTAL is **decision-loop term + task-execution term**:

```
monthly total = (per-cycle cost × cycles/month) + (per-task cost × tasks/month)
```

The inputs are read from existing configuration / measured data, each cited:

| Anchor | Value | Source |
|---|---|---|
| Measured cycle volume | **59 cycles / 7 days → ~256.6/month** | Direction d-e860fe17 measured basis ($276.87 total / $261.33 strategic across 59 cycles). Scaled by `DAYS_PER_MONTH / 7` with `DAYS_PER_MONTH = 30.44`. |
| Cadence-backstop floor | **~30.4 cycles/month** | `strategic_max_interval_minutes = 1,440` default (`runtime_config_state.py:830`) — the wall-clock floor that forces a strategic spawn. A floor, not the realized volume. |
| Measured task volume | **75 tasks / 7 days → ~326.1 tasks/month** | Direction d-e860fe17 measured basis (75 merged PRs over the window). Same 30.44-day month convention. |
| Per-task execution cost | **~$0.3128/task** | Direction d-e860fe17 measured basis: non-strategic fleet cost **$15.46 LLM + $8.00 VM** over 75 tasks. This is the same VM/LLM cost accounting that backs the existing per-task cost reporting (`cost_per_verified_task` / `total_cost_per_verified_task`, built from `get_task_total_vm_cost_usd` + `_task_llm_cost`). |
| Target concurrency | **NOT AVAILABLE** | No `target_concurrency` configuration key exists for the strategic decision loop (searched `runtime_config_state.py` defaults and the `ReleaseVersion` model). The projection surfaces this rather than guessing a volume. |

**Concurrency assumption:** cycles run serially — the cadence spawner gates on
no-active-strategic (`autonomous_research.py:1732-1733`), so at most 1 concurrent
strategic cycle per project at a time.

**Task-slot concurrency is measured, not simulated (fleet sizing).** The 50-slot
fleet-sizing claim no longer rests on the mocked vitest FIFO simulation
(`agent/src/index.pool-mode.test.ts`, retained only as a scheduler-starvation
regression). Peak and time-weighted percentile concurrent task-slot occupancy
(p50 / p90 / p99, plus p95) are computed directly from the task table's
slot-occupancy intervals via `get_fleet_utilization_snapshot`
(`backend/app/fleet_utilization.py`) and surfaced on `/api/admin/fleet-utilization`
as `concurrent_peak` / `concurrent_p50` / `concurrent_p90` / `concurrent_p95` /
`concurrent_p99`. Both that endpoint and `get_task_concurrency_snapshot` read
the single `_overlap_stats_from_task_rows` sweep, so they cannot disagree.
**Denominator:** `concurrent_peak` is the maximum simultaneous slot occupancy
in the window; the percentiles are seconds-weighted over the **active window**
(first slot start to last slot end, with interior idle gaps counted at level 0)
— *not* over the full `window_hours`, so leading/trailing idle does not drag the
distribution toward 0. The slot itself is
`vm_dispatched_at`/`vm_provisioned_at` → `vm_stopped_at` (still-active tasks
extend to now); terminal tasks that never recorded a VM stamp hold no slot.
This is *task-slot* concurrency — the number of fleet slots
busy at once — and is distinct from the *strategic decision-loop* cadence in the
'Target concurrency' row above (which remains unavailable and is not replaced by
this measurement).

```
Scenario R=0.70 (sensitivity point), share 0.55:
  decision loop : $4.88/cycle × 256.6 (measured) = $1,253.31/month
  task execution: $0.3128/task × 326.1 tasks     = $  102.02/month
  TOTAL                                        = $1,355.33/month
Direction-target (R=0.85, share 0.80):
  decision loop : $2.59/cycle × 256.6 (measured) = $  664.12/month
  task execution: $0.3128/task × 326.1 tasks     = $  102.02/month
  TOTAL                                        = $  766.14/month
```

**Envelope verdict:** at the R=0.70 sensitivity point the TOTAL at the measured volume is
**~$1,355/month** — **45.2%** of the $3,000/month envelope, comfortably within it. The
decision-loop-only half binds at **~614 cycles/month** (~2.4× the measured volume);
the combined envelope binds at a lower volume, but task volume does not scale with
cycle volume at the measured basis, so the script asserts no combined binds-at figure
(rather than fabricating a scaling assumption). The envelope is therefore *not* the
binding constraint at today's cadence. (The per-cycle number that feeds it is still
above the $1.50 headline, and the >=30%-below-baseline claim is UNVERIFIED until a
measured share — or a replayed fail-closed lower bound that itself clears the
threshold — is checked in.)

If any input were unavailable (no cycle volume, or no per-task cost/volume), the script
returns `available: False` with a reason naming the missing input instead of
fabricating a number — it never silently drops the task term to a zero.

A falsified target is a real, useful result; a number tuned backwards from $1.50 is
not. The actionable levers that could actually reach $1.50: fewer/smaller turns
(lower per-cycle input), a cheaper routine tier than terra (e.g. a luna-class tier),
or shrinking the volatile payload so more of the prompt becomes cross-cycle
cacheable — none of which is the stable-prefix reorder by itself.
