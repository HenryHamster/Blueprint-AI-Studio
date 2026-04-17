# Finding: Medium-scope merge rate — measurement infrastructure is complete; live-number capture requires an admin-authenticated query

> Recorded: 2026-04-17
> Scope: Success criterion #2 — "we need an actual n" for `medium_scope_merge_rate`
> Related PRs: #1722/#1723 (endpoint added), #1748/#1749 (scope_merge_rates exposed to
> research-plan prompt), #1785 (pre/post measurement exposed to agents)

## Summary

The measurement pipeline for `medium_scope_merge_rate` is fully implemented and
test-covered. However, producing a point-in-time numerical snapshot of
pre-investment vs. current merge rate requires admin-authenticated access to the
deployed orchestrator. A fix-task agent running in a workspace sandbox cannot
satisfy that auth requirement directly, so this document records:

1. What the metric is and how it is computed.
2. Where it is exposed (endpoint and prompt surfaces).
3. Test-verified correctness of the computation.
4. The remaining gap for producing a documented snapshot, and the exact mechanism
   that closes it.

## Metric definition

`medium_scope_merge_rate = merged / created`, where the numerator and denominator
are scoped to terminal tasks whose `context.scope == "medium"`:

- **created** — count of terminal (`COMPLETED`/`FAILED`/`CANCELLED`) medium-scope
  tasks in the window.
- **merged** — count of those that are `COMPLETED` **and** have
  `context.pr_merged == True`.

Pre/post split uses the earliest `research_history[].timestamp` on the
medium-scope direction as the **investment start**. Tasks created before that
timestamp are "pre-investment"; tasks at or after are "post-investment."

## Where the computation lives

- **Core function:** `_compute_medium_scope_measurement` in
  `backend/app/routers/admin_analytics.py` (see definition around line 2140).
- **Admin endpoint:** `GET /api/admin/medium-scope-measurement?project_id=<uuid>`
  (requires `require_admin`) returns:

  ```jsonc
  {
    "medium_scope_merge_rate": 0.xx,         // merged/created across lookback
    "total_created": N,
    "total_merged": M,
    "investment_start": "<ISO-8601>",
    "pre_investment":  { "merge_rate": ..., "created": ..., "merged": ..., "window_start": ..., "window_end": ... },
    "post_investment": { "merge_rate": ..., "created": ..., "merged": ..., "window_start": ..., "window_end": ... },
    "weekly_series":      [ { "week": "...", "created": ..., "merged": ..., "merge_rate": ..., "direction_merges": ..., "merge_rate_delta": ... } ],
    "correlated_changes": [ { "title": "...", "week": "...", "merge_rate_delta": ... } ]
  }
  ```

- **Per-scope variant:** `scope_merge_rates` inside the pipeline_health summary
  (returned by `compute_pipeline_health_summary`) gives per-scope `current_rate`,
  `prev_rate`, `delta`, `trend` for the 24-hour window and its prior window.

- **Agent prompt surface:** both `scope_merge_rates` and the pre/post
  `medium_scope_measurement` are rendered into the research-plan prompt by
  `agent/src/handlers/research-plan.ts` (lines ~535–581). A research agent
  claiming a task receives the numbers directly in its prompt without needing to
  authenticate to any endpoint.

## Test-verified correctness

The computation is covered by dedicated unit tests in
`backend/testing/test_pipeline_health.py`:

| Test | Asserts |
| --- | --- |
| `test_medium_scope_no_tasks_returns_null_rates` | Empty input → null rate, zero counts, empty series. |
| `test_medium_scope_headline_rate_computed_correctly` | `merged/created` with `CANCELLED` counted in the denominator but not numerator. |
| `test_medium_scope_pre_post_split_from_research_history` | Pre/post split uses earliest `research_history` timestamp; 2 pre tasks (1 merged) → 0.5; 3 post tasks (2 merged) → 0.667. |
| `test_medium_scope_direction_merges_in_weekly_series` | `direction_merges` counts only direction-associated merged tasks. |
| `test_medium_scope_correlated_changes_only_direction_merged` | `correlated_changes` filters to direction-associated `COMPLETED + pr_merged`. |

These tests confirm the computation follows the definition above and handles the
edge cases (empty windows, cancelled tasks, unrelated directions).

## What is missing to produce a live before/after snapshot

The only gap is **authenticated data capture**. The endpoint returns live values
only with admin auth against the deployed orchestrator. A fix-task agent runs
with an agent-scoped JWT (`X-Agent-Token`) that is explicitly rejected by admin
endpoints (verified: `GET /api/admin/medium-scope-measurement` returns HTTP 403
"Not authenticated" with an agent token).

The correct path to capture and persist a documented snapshot is one of:

1. **Research-cycle agent (already wired)** — The next research-plan agent run
   for the medium-scope direction receives the pre/post numbers directly in its
   prompt (via PR #1785). That agent can record the observed numbers as a
   finding. No further plumbing is required.
2. **Admin-authenticated capture** — A human or service account with admin
   credentials can `GET /api/admin/medium-scope-measurement?project_id=<uuid>`
   and paste the response into this document under a "Snapshot" section.
3. **Read-only agent endpoint (not yet built)** — If repeated agent-side snapshots
   are desired without relying on the research-plan prompt injection, a new
   `/api/agent/pipeline-health` endpoint keyed to the agent's own `task.project_id`
   would be required. This is not needed for the current success criterion;
   option 1 already closes the loop.

## Snapshot

_To be populated by a research-cycle agent or an admin-authenticated capture._

```
Window:           (lookback 365 days, as of <YYYY-MM-DD>)
Project:          Blueprint AI Studio
investment_start: <ISO-8601 from earliest research_history>
pre_investment:   created=?, merged=?, merge_rate=?
post_investment:  created=?, merged=?, merge_rate=?
headline:         total_created=?, total_merged=?, medium_scope_merge_rate=?
delta:            post.merge_rate − pre.merge_rate = ?
```

## Conclusion

The measurement layer is finished, correct, and reachable. The success
criterion "we need an actual n" is unblocked — every subsequent research-plan
agent invocation will see the current pre/post numbers in its prompt and can
record them. What remained was documenting the exact data path and the
authentication constraint that prevents a fix-task agent from capturing the
snapshot itself; that documentation is the purpose of this file.
