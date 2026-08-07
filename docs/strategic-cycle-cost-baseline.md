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

The cohort is the terminal-task window the efficiency snapshot aggregates over:
terminal tasks (`COMPLETED` / `FAILED` / `CANCELLED`) updated within the last 7
days (`_AXIS_WINDOW_DAYS`), most-recently-updated first, capped at 5000 rows
(`_COHORT_CAP` in `backend/app/efficiency_snapshot.py`). When the cap trips the
cohort is a **recency sample**, and every aggregate — including
`strategic_cycle_cost_usd` and `total_llm_usd` — describes that narrower span,
not the full requested window. The exact effective window is
`cohort_effective_window_start` / `cohort_effective_window_end` on the persisted
efficiency snapshot (operator dashboard / efficiency-baseline read); the agent
endpoint exposes the task count and the capped flag.

## Reading 1

- **Date:** 2026-08-07
- **Integration HEAD:** `9e25341c`
- **Requested window:** 7 days (`_AXIS_WINDOW_DAYS`)
- **Cohort task count:** 5000 (at `_COHORT_CAP`)
- **Cohort capped:** **True** — the cohort hit the 5000-row cap, so the figures
  describe the recency sample of the 5000 most-recently-updated terminal tasks.
- **Effective window:** the span of those 5000 rows; the exact
  `cohort_effective_window_start` / `cohort_effective_window_end` timestamps were
  not captured at reading time and must be recorded on the next reading.
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
- **Effective window:** the span of those 5000 rows (same cohort as Reading 1);
  exact timestamps not captured at reading time.
- `cycle_count`: **212**
- `strategic_cycle_cost_usd`: **1618.175162**
- `cost_per_cycle`: **7.632902**
- `total_llm_usd`: **3446.586451**
- `unpriced_coverage`: **0.9752**

## Why the two readings are flat

Both readings share `cycle_count` 212, i.e. they cover the same cohort. Every
cost lever landed so far (OpenAI cache-read capture, `problem_model` section
cut, no-delta gate, cohort provenance) modifies prompt assembly in
`backend/app/strategic_spawning.py`, which executes only in the deployed
backend. The deployed backend is at origin/main and contains none of them.
**A flat reading here is therefore not evidence about the levers** — the levers
are not running in the code that produced these readings.

## How to take the next reading

1. Call the agent endpoint above:
   `GET /api/agent/projects/{project_id}/efficiency-snapshot` with an agent
   token for the project.
2. Record the five cost fields plus `cohort_task_count` and `cohort_capped`
   from the mapping table.
3. Record the effective window. The agent endpoint reports the count and the
   capped flag; the exact `cohort_effective_window_start` /
   `cohort_effective_window_end` timestamps are on the persisted efficiency
   snapshot (operator dashboard / efficiency-baseline read). Capture them so
   the window is comparable across readings.
4. Record the integration HEAD of the deployed backend (origin/main HEAD at
   reading time) and the date alongside the figures.
5. Append the reading below the previous ones in this file, keeping the same
   field layout so rows stay comparable, and update `BASELINE_READINGS` in
   `backend/scripts/strategic_cycle_cost_projection.py` so section [8] prints
   the new record too.
