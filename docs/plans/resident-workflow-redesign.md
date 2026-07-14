# Resident "Workday" Session — Fix + Workflow Redesign

**Goal:** the resident strategist actually works end-to-end and is intuitive:
enroll a runner → it connects → **Workday mode = live agent** → chat replies in
seconds. Fix the crash that blocks it and remove the manual archaeology.
**PR target: `main`** (operator-driven, skip the automain hop for speed).

## What was frustrating (scoped)
1. **Enroll command uses the UI URL** (`…web.app`), not the API URL (`…run.app`) →
   runner heartbeats hit the SPA, silently never register, shows "not started".
2. **Project matching is broken** in the CLI: defaults to the first project
   (NeurIPS), **ignores `--project-id` and `BLUEPRINT_PROJECT_ID`**, no list/
   by-name select — I had to hit the API by hand to find "Blueprint".
3. **Enablement is invisible:** dark flag, no toggle; switching to Workday mode
   does nothing; the Start button appears/disappears with no explanation.
4. **`start_resident_session` crashes:** `MissingGreenlet` — the `except`
   compensation reuses the rolled-back DB session, masking the real error; start
   **hangs >120s** and leaves a half-initialized session (`task_id: null`).
5. **No feedback:** connected vs attached vs task-claimed is opaque; failures show
   up as a client timeout, never a reason.

## Fixes

### 1 · Reliability — backend (unblocks everything)
- `start_resident_session`: run `_release_start_claim` on a **fresh session/
  connection**, and log the ORIGINAL exception first → removes the MissingGreenlet
  mask so the true spawn error is visible.
- Reproduce once (flag on → start) with that in place → fix the underlying spawn
  failure.
- **Fail-fast + clean:** bounded start; on any failure leave **no** orphaned claim
  / no `task_id: null` session; return the reason to the caller (no hang).

### 2 · Intuitive flow — backend + admin-ui
- **Workday mode is the on-switch:** entering `continuous`/Workday mode
  **auto-starts** the resident session when a live runner exists; leaving it stops.
  No separate Start button to hunt for.
- When it can't start, say **why** ("resident sessions disabled" / "no runner
  connected") instead of silence.
- **Status surface:** dark / ready / **active · runner attached · replying** /
  error-with-reason.
- Expose a Settings toggle for `strategic_session_enabled` (or fold enablement into
  the mode switch).

### 3 · Enroll command — admin-ui
- `FleetRunnersPanel.utils.ts`: build `BLUEPRINT_ORCHESTRATOR_URL` from
  `API_BASE_URL` (VITE_API_URL), not `window.location.origin`. + unit test.

### 4 · Project matching — scripts/blueprint.py + blueprint_api.py
- Add `blueprint projects` (id · name list).
- `get_project_id`: honor `--project-id` and `BLUEPRINT_PROJECT_ID` (both ignored today).
- Add `--project <name>` fuzzy match so `strategic start --project blueprint` just works.

## Verify
- admin-ui `npm run typecheck && npx eslint .`; backend targeted `test_strategic_*`
  + resident-session tests.
- **Live smoke before done:** flag on → enroll → Workday mode → chat → sub-minute reply.
- PR → `main`.
