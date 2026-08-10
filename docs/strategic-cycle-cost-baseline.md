# Strategic-Cycle Cost Baseline

**Date:** 2026-08-07
**Record:** `python3 backend/scripts/strategic_cycle_cost_projection.py` section [8]
prints the machine-readable form of this record (the numbers live in
`BASELINE_READINGS` / `baseline_record()` in that script); this doc is the
narrative form of the same record.

This is a **measurement record**, not a projection. It records two
strategic-cycle cost readings taken against the live (deployed) backend on
2026-08-07, verbatim and dated, so that any future cost-reduction claim has a
stated thing to be measured against. See `docs/strategic-cycle-cost-projection.md`
for the forward-looking projection model. This doc does not predict cost and
does not judge whether cost is going up or down.

**Statistic label — each `cost_per_cycle` is a mean.** Every `cost_per_cycle`
above is `per_event_usd = strategic_cycle_cost_usd / cycle_count` from the
`llm_cost_attribution` read surface — a **mean** over the reading's cohort
cycles, not a median. The projection (`strategic_cycle_cost_projection.py`)
anchors its per-cycle baseline on this record: `BASELINE_COST_PER_CYCLE_USD` is
derived at module load as the mean of the recorded `cost_per_cycle` values
(readings 1–3), so the anchor can never drift from the instrument again, and it
reports the recorded median alongside the mean rather than silently conflating
the two statistics.

## Where the numbers come from

Each reading is the `llm_cost_attribution` block of the agent efficiency-snapshot
read surface, plus the cohort-coverage fields on the same response:

```
GET /api/agent/projects/{project_id}/efficiency-snapshot   (agent token)
```

Field mapping:

| Reading field | Endpoint field |
|---|---|
| `cycle_count` | `data.llm_cost_attribution.strategic_cycle_cost_per_event.cycle_count` |
| `strategic_cycle_cost_usd` | `data.llm_cost_attribution.strategic_cycle_cost_per_event.total_usd` |
| `cost_per_cycle` | `data.llm_cost_attribution.strategic_cycle_cost_per_event.per_event_usd` |
| `total_llm_usd` | `data.llm_cost_attribution.total_llm_usd` |
| `unpriced_coverage` | `data.llm_cost_attribution.unpriced_coverage` |
| `cohort_task_count` | `data.cohort_task_count` |
| `cohort_capped` | `data.cohort_capped` |
| `cohort_effective_window_start` | `data.cohort_effective_window_start` |
| `cohort_effective_window_end` | `data.cohort_effective_window_end` |

The cohort is the terminal-task window the efficiency snapshot aggregates over:
terminal tasks (`COMPLETED` / `FAILED` / `CANCELLED`) updated within the last 7
days (`_AXIS_WINDOW_DAYS`), most-recently-updated first, capped at 5000 rows
(`_COHORT_CAP` in `backend/app/efficiency_snapshot.py`). When the cap trips the
cohort is a **recency sample**, and every aggregate — including
`strategic_cycle_cost_usd` and `total_llm_usd` — describes that narrower span,
not the full requested window. The agent endpoint exposes the exact effective
span the reading covers as `cohort_effective_window_start` /
`cohort_effective_window_end` (the actual oldest/newest `updated_at` of the
loaded cohort, same values the persisted efficiency snapshot records), so a
reading records the window that gives its provenance instead of leaving it
unknown.

## Reading 1

- **Date:** 2026-08-07
- **Integration HEAD:** `9e25341c`
- **Requested window:** 7 days (`_AXIS_WINDOW_DAYS`)
- **Cohort task count:** 5000 (at `_COHORT_CAP`)
- **Cohort capped:** **True** — the cohort hit the 5000-row cap, so the figures
  describe the recency sample of the 5000 most-recently-updated terminal tasks.
- **Effective window:** `2026-07-31T00:00:00+00:00` to
  `2026-08-07T00:00:00+00:00`. This reading predates the agent endpoint
  exposing the effective window, so the recorded values are the requested
  query-window bounds the reading's endpoint actually queried (7 days ending on
  the reading date) rather than the exact span of the capped cohort. Readings
  after the endpoint change record the true effective span.
- `cycle_count`: **212**
- `strategic_cycle_cost_usd`: **1610.081459**
- `cost_per_cycle`: **7.594724**
- `total_llm_usd`: **3439.92**
- `unpriced_coverage`: **0.9752**

## Reading 2

- **Date:** 2026-08-07
- **Integration HEAD:** `83813e87`
- **Requested window:** 7 days (`_AXIS_WINDOW_DAYS`)
- **Cohort task count:** 5000 (at `_COHORT_CAP`)
- **Cohort capped:** **True** — same capped recency sample as Reading 1.
- **Effective window:** `2026-07-31T00:00:00+00:00` to
  `2026-08-07T00:00:00+00:00` — same requested query-window bounds as Reading 1
  (same cohort and reading date).
- `cycle_count`: **212**
- `strategic_cycle_cost_usd`: **1618.175162**
- `cost_per_cycle`: **7.632902**
- `total_llm_usd`: **3446.586451**
- `unpriced_coverage`: **0.9752**

## Reading 3

- **Date:** 2026-08-08
- **Integration HEAD:** `5b97819`
- **Requested window:** 7 days (`_AXIS_WINDOW_DAYS`)
- **Cohort task count:** 5000 (at `_COHORT_CAP`)
- **Cohort capped:** **True** — the cohort hit the 5000-row cap; figures describe
  the recency sample of the 5000 most-recently-updated terminal tasks.
- **Effective window:** `2026-08-01T00:00:00+00:00` to
  `2026-08-08T00:00:00+00:00`. This is the first reading taken after the agent
  endpoint exposed `cohort_effective_window_start` /
  `cohort_effective_window_end`, so these are the **true effective span** of the
  capped cohort (actual oldest/newest `updated_at` of the loaded rows), not the
  requested query-window bounds.
- `cycle_count`: **230**
- `strategic_cycle_cost_usd`: **1683.2**
- `cost_per_cycle`: **7.318261**
- `total_llm_usd`: **3390.42**
- `unpriced_coverage`: **0.72** — 72% of LLM spend in this cohort predates v29
  serving and cannot be attributed to post-promotion cycles. This is the first
  reading after the v29 promotion and reflects the mixed-window nature of a
  capped recency cohort: the most-recently-updated 5000 tasks span approximately
  7 days, of which only the tail falls after the promotion. Attribution will
  improve as the cohort ages out pre-promotion tasks.

## Why the two early readings are flat

Readings 1 and 2 both share `cycle_count` 212, i.e. they cover the same cohort. Every
cost lever landed so far modifies prompt assembly in `backend/app/strategic_spawning.py`,
which executes only in the deployed backend. **A flat reading here is therefore not
evidence about the levers** — the levers are not running in the code that produced
those readings.

## How to take the next reading

1. Call the agent endpoint above:
   `GET /api/agent/projects/{project_id}/efficiency-snapshot` with an agent
   token for the project.
2. Record the five cost fields plus `cohort_task_count`, `cohort_capped`, and
   the `cohort_effective_window_start` / `cohort_effective_window_end`
   timestamps from the mapping table (all on the same response).
3. The effective window is the actual oldest/newest `updated_at` span of the
   loaded cohort, so it is comparable across readings and records exactly what
   the aggregates describe.
4. Record the integration HEAD of the deployed backend (origin/main HEAD at
   reading time) and the date alongside the figures.
5. Append the reading below the previous ones in this file, keeping the same
   field layout so rows stay comparable, and update `BASELINE_READINGS` in
   `backend/scripts/strategic_cycle_cost_projection.py` so section [8] prints
   the new record too.

## Serving-SHA readings are a separate ledger

The efficiency snapshot exposes `data.llm_cost_attribution.strategic_cycle_cost_per_event.by_serving_sha`.
Copy complete rows into `SERVING_SHA_READINGS` in the projection script,
preserving `serving_sha`, `cycle_count`, `total_usd`, `per_event_usd`,
`first_cycle_at`, `last_cycle_at`,
and `unpriced_coverage`.

These rows answer whether a deployed revision's cycles are cheaper, but are
not baseline readings: a serving slice has a different boundary. The script validates
the SHA, coverage range, and
`per_event_usd = total_usd / cycle_count` arithmetic before comparing a row;
missing or ambiguous attribution is unavailable. The comparison uses the
serving row's `per_event_usd` against frozen `BASELINE_COST_PER_CYCLE_USD` and
reports the delta, but never appends the row to `BASELINE_READINGS`. Recording a
serving-SHA result therefore cannot move the anchor it is measured against.

## Cost-verdict qualification

The projection applies `MINIMUM_COST_VERDICT_N = 30`, where `n` is the sum of
`cycle_count` across validated slices. Thin evidence returns
`available: false`, `verdict: "insufficient_sample"`, `n`, `required_n`, and no
cost delta. Valid cycle timestamps also produce `projected_date`; absent
timestamps stay `null` rather than inventing a collection rate.

`serving_sha_record()` reports unweighted `mean_usd`, `median_usd`, and sample `stddev_usd`; one slice has `stddev_usd: null`, and invalid slices are `unavailable`.
