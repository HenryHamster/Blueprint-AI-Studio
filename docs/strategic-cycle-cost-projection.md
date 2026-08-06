# v28 Projected Median Per-Cycle Strategist LLM Cost

**Date:** 2026-08-06
**Compute:** `python3 backend/scripts/strategic_cycle_cost_projection.py` (the checked-in
reproducible projection; this doc is the narrative form of the same model).

v28's headline goal promises a **median strategist cycle under $1.50**, down from a
measured **$4.41/cycle**. Nothing in the repo computed that number — this artifact does.
It derives the projected median from stated inputs, cites each input to its source,
prices the two caching regimes separately, states every assumption, shows sensitivity,
and reports the number honestly: **the projected median is above $1.50.**

---

## 1. Stated inputs (each cited)

| Input | Value | Source |
|---|---|---|
| Baseline per-cycle cost | **$4.41/cycle** | Task-provided measurement. Corroborated at $4.43/cycle by design doc `d6c4d7fb-…` and by the `strategic_review_effort.py` docstring ("~$4.43 per cycle"). |
| Mean input tokens per call | **741,992** | Task-provided measurement. Design doc cites 737,568. Inclusive per-call total = fresh input + cache read + cache creation (`mean_input_tokens_per_call`, `backend/app/llm_cost_attribution.py:1174-1178`). |
| Baseline cache read share | **0.55** | Design doc (strategic_review vs 0.87 elsewhere; target ≥ 0.80). |
| Stable prefix rendered | **42,073 bytes ≈ 10,518 tokens** | Measured statically: `buildStrategicStablePrefix` in `agent/src/prompts/thinker.ts` with representative params (`projectName='Blueprint AI Studio'`, `isSelfRepo=true`, 355-byte `selfImprovementBlock` from `agent/src/handlers/strategic.ts`, `PLAYBOOK_GUIDANCE`/`DEFAULT_TOOL_GUIDANCE`/`READ_ONLY_BASH_WRITE_NOTE` from `agent/src/prompt.ts`). |
| Volatile instruction text | **2,666 bytes ≈ 666 tokens** | Same static render, every per-cycle payload field set to `''`, with the largest conditional instruction blocks active (recover `798 B` + gap-memory trim `687 B` + design-doc overview `626 B` + current-activity intro `383 B` + 591 B direct literal + structural headers — the 2,666 B figure is the exact stripped render, not the author's byte estimate). |
| Volatile total (representative cycle) | **≈ 104 KB ≈ 26,000 tokens** | 2,666 B instruction + ~100 KB per-cycle payload (large `problem_model` JSON, direction portfolio, context blocks — see the payload pack in `backend/app/strategic_spawning.py`). |
| Tier mix | Routine → `gpt-5.6-terra` (medium), Consequential → `gpt-5.6-sol` (max) | PR #16730 (`backend/app/strategic_review_effort.py`). **Routine share R is unobserved** — the classifier is inert until v28 promotes. |
| Pricing (per 1M tokens) | sol: in `$5` / out `$30` / read `$0.50`; terra: in `$2.5` / out `$15` / read `$0.25`; cache create = 1.25× input, cache read = 0.1× input | `backend/app/llm_cost_attribution.py:50-51,89-96`. |

**Tokens are estimated from measured bytes at 4 bytes/token** (standard English-text
approximation). The byte counts are exact; the cost model is anchored on the measured
$4.41/cycle, so the estimate only affects the tiny cross-cycle term (§4).

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
large. **This regime is already priced into the measured $4.41.** The v28 stable-prefix
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
projected cost = $4.41 · (1 − 0.5R) · [ φ_in · m(ρ_after)/m(ρ_before) + (1 − φ_in) ]
                − cross_cycle_saving(R, survival)
```

Base assumptions: `R = 0.70`, `φ_in = 0.90`, `survival = 0`.

### Honest case — the reorder alone

The reorder does not move the intra-cycle share (`ρ` stays 0.55), and cross-cycle
survival is 0:

```
$4.41 · (1 − 0.35) = $2.87/cycle
```

### Direction-target case — cache share genuinely reaches 0.80

```
m(0.80)/m(0.55) = 0.28/0.505 = 0.5545
R = 0.70:  $4.41 · 0.65 · (0.90·0.5545 + 0.10) = $1.72/cycle
R = 0.85:  $4.41 · 0.575 · 0.5991           = $1.52/cycle
R = 0.90:  $4.41 · 0.55 · 0.5991            = $1.45/cycle
```

---

## 5. Sensitivity

`φ_in = 0.90`, survival = 0.

| Routine share R | Tier-only | + cache share 0.80 |
|---|---|---|
| 0.30 | $3.75 | $2.25 |
| 0.50 | $3.31 | $1.98 |
| 0.70 | $2.87 | $1.72 |
| 0.85 | $2.54 | $1.52 |
| 0.90 | $2.43 | $1.45 |
| 1.00 | $2.21 | $1.32 |

Cache-share sensitivity at R = 0.70: share 0.55 → $2.87, 0.60 → $2.64,
0.70 → $2.18, 0.80 → $1.72.

Cross-cycle survival at R = 0.70, share 0.55: 0.00 → $2.87, 0.10 → $2.86,
1.00 → $2.84 (the term is negligible by construction).

Input-cost share (φ_in) shifts everything proportionally on the input side; the
0.80-2.87 spread in the table is driven far more by `R` and `ρ` than by `φ_in` in the
plausible 0.80-0.95 range.

**Routine share R is the swing factor.** The fail-closed classifier
(`strategic_review_effort.py`) routes any open message, version decision pending,
integrity conflict, prior-dirty cycle, or recent failure to consequential — so
dirty/recovering periods (like the recent 1/11 and 0/3 merge records) push R well
below 0.7. The direction's own health depends on R being high.

---

## 6. Verdict

**The projected median per-cycle strategist cost is $2.87/cycle in the honest base
case — above the $1.50 headline.** The full tier-only range across R = 0.3-0.9 is
$2.43-$3.75.

Reaching $1.50 requires **both** of these to hold simultaneously:

1. The cache-read share genuinely rises to ~0.80 across the whole cycle's input —
   which the stable-prefix reorder alone **cannot deliver**, because the only
   cross-cycle-cacheable content is the ~10.5K-token stable block against ~1.5M
   input tokens per cycle, and the volatile payload changes every cycle; and
2. A routine share of ~85%+ — near the classifier's ceiling and contradicted by
   the current recovering-period signals.

At the direction's own target inputs the number lands at $1.52-1.72 (R = 0.70-0.85),
so the target is on the edge of the model's optimistically-parameterized range — but
the mechanism analysis says the share lever is not available at the claimed size. The
tier-mix lever (PR #16730) is the one reliable cost reduction, and its ceiling alone
(R = 1) is $2.21/cycle.

A falsified target is a real, useful result; a number tuned backwards from $1.50 is
not. The actionable levers that could actually reach $1.50: fewer/smaller turns
(lower per-cycle input), a cheaper routine tier than terra (e.g. a luna-class tier),
or shrinking the volatile payload so more of the prompt becomes cross-cycle
cacheable — none of which is the stable-prefix reorder by itself.
