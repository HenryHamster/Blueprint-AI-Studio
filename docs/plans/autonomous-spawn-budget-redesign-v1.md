# Autonomous spawn budget redesign

## Problem

The old global guard counted every `ORCHESTRATOR` task in the last hour, so
review/fix/panel churn could starve new research.

## Chosen design

- Keep a global backstop ceiling on total autonomous volume.
- Add a separate initiative budget for new research/create-style spawns.
- Classify tasks by lineage and type through `backend/app/autonomous_spawn_budget.py`.
- Default rollout mode stays `observe`; `enforce` switches initiative spawns to
  the new budget gate.

## Behavior

- `observe`: record the budget snapshot and keep only the global backstop.
- `enforce`: block initiative spawns on the initiative budget, while the global
  backstop still applies to runaway total volume.
- Servicing follow-ons are separated from initiative counts so they no longer
  compete with new research for the same hourly budget.

## Code touch-points

- `backend/app/autonomous_spawn_budget.py`
- `backend/app/autonomous_research.py`
- `backend/app/routers/admin_debug.py`
- `backend/app/routers/admin_projects.py`
- `backend/app/routers/admin_settings.py`
- `backend/app/runtime_config_state.py`
- `backend/app/evergreen_backlog.py`
- `scripts/blueprint.py`

## Visibility

- `autonomy-debug` now reports the global backstop and `Project.paused`.
- `autonomous-status` now separates total churn from initiative and servicing
  counts, and surfaces the global backstop headroom.
- `blueprint.py health autonomy` renders the same split counts.

## Rollout

1. Ship in `observe` mode.
2. Watch global headroom and initiative headroom in the admin views.
3. Flip to `enforce` when the observed spawn mix is stable.
