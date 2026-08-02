# Agent-Declared Flagging Plan v1

**Date:** 2026-05-20
**Scope:** Replace the backend's *mining* of agent self-review free-text with explicit, structured, agent-*declared* flags. Covers the agent helper-script layer, the self-review prompt, a new backend ingestion endpoint + model, the regression dispatcher's detection input, and removal of the blind-spot theme-mining / triage / denylist machinery.
**Related files:** `agent/src/prompt.ts`, `backend/app/sr_blind_spots.py`, `backend/app/pipeline_regression_dispatcher.py`, `backend/app/routers/agent_status.py`, `backend/app/routers/admin_digest.py`, `backend/app/models.py`, `backend/app/main.py`
**Status:** Superseded (2026-08-01) — historical only; do not implement. The dedicated Fresh Reviewer path was retired. Current review authority is the ordinary exact-head review loop plus the backend blocker ledger and self-review diagnostics. Any future agent-declared flag work needs a new plan against that architecture.

## Summary

Today the orchestrator does not *receive* regression/blind-spot signals — it *mines* them. An agent writes a free-text `blind_spots` string in its self-review JSON; the backend truncates each string to its first 6 words ("theme"), counts how often similar themes recur across ~2000 recent tasks, and once recurrence crosses a floor, promotes the theme to a `RegressionRemediationCase` that spawns an `investigate` task. A keyword heuristic then *triages* the theme, and a regex denylist *overrides* entries agents "should not" have written.

Every layer is an inference on top of an inference. The denylist is the tell: it exists to override the agent — an adversarial posture toward the system's own agents, and a source of perpetual phrasing whack-a-mole (the `suite` vs `test suite` gap that produced the 2026-05-19/20 false-positive cases).

This historical plan proposed replacing inference with declaration. Agents would get an explicit command — `./flag` — to structurally declare a regression. The backend would trust the structured flag and act on it directly. Blind spots would remain task-level context for ordinary review and diagnostics rather than being mined into anything. Aggregate trend detection — which catches what no single agent can see — would remain unchanged.

## Current Architecture (what is being replaced)

- **Emission.** The agent writes `blind_spots: string[]` into the self-review JSON. Schema and semantics live in `agent/src/prompt.ts` (`SELF_REVIEW_PROMPT_VERSION` at `:1727`, schema at `:2159`, semantics at `:2187`–`:2191`).
- **Ingestion + denylist.** `routers/agent_status.py:1077` runs `filter_disallowed_blind_spots` (`sr_blind_spots.py:64`) on every recorded self-review, stripping entries matched by the `DISALLOWED_BLIND_SPOT_PATTERNS` regex (`sr_blind_spots.py:34`). The TS-side `DISALLOWED_BLIND_SPOT_EXAMPLES` (`prompt.ts:1731`) mirrors it, kept in sync by a contract test.
- **Mining.** `sr_blind_spots.py` `_scan_blind_spot_counts` (`:121`) / `aggregate_sr_blind_spot_themes` (`:212`) scan up to 2000 recent completed tasks, normalize each blind_spot to its first 6 lowercase tokens, and count recurrence. `triage_sr_blind_spot_theme` (`:727`) keyword-classifies each theme (`likely_stale_assertion`, etc.).
- **Promotion to regression.** `pipeline_regression_dispatcher.py` runs an `sr_blind_spot` signal (theme handling at `:1851`): themes recurring above a floor become `sr_blind_spot:theme:*` `RegressionRemediationCase`s and spawn `investigate` tasks. `sr_blind_spot` also has a staleness window in `_SIGNAL_STALENESS_WINDOWS` (`:257`).
- **Telemetry.** `admin_digest.py` exposes `_compute_sr_disallowed_blind_spot_rate` (`:347`, a prompt-compliance metric) and `GET /projects/{id}/sr-blind-spots` (`:8016`).

## Problems to Fix

1. **Inference stacked on inference.** 6-token truncation, a recurrence floor, and a keyword triage heuristic are each a guess; their errors compound.
2. **Recurrence is not regression.** A blind-spot phrasing becomes a pipeline "regression" purely by being *common*. "Many agents wrote similar words" carries no regression semantics.
3. **Adversarial denylist.** The backend maintains a regex to override what agents write. Every new phrasing is a fresh gap — unwinnable whack-a-mole.
4. **False escalations.** Intentional behavior ("did not run the full local suite" — CI runs it) and routine self-review caution get escalated into `investigate` tasks and `NEEDS_MANUAL_INTERVENTION` cases.
5. **Latency and opacity.** A genuine regression must recur N times before the floor trips. The agent that saw it first had no channel to say so directly.

## Target Architecture

### Channel 1 — Blind spots: demoted to a per-task reviewer aid

`blind_spots` stays a **free-text** array in the self-review JSON. Free text is useful as task-level context for the ordinary review loop and operator diagnostics ("here is what the author was unsure about"). Free text was never the problem; *mining* it was.

- It is **not** aggregated, themed, counted, triaged, or denylisted.
- It is **not** a regression signal.
- It is shown on the task in the admin UI and may be passed into the ordinary exact-head review context. There is no dedicated reviewer subsystem.
- No schema change, no migration. The entire mining/triage/denylist stack is deleted (see Removals).

### Channel 2 — `./flag`: explicit agent-declared flags

A new agent helper command, generated into the workspace alongside `./done`, `./check`, `./pr`, `./playbook`:

```
./flag --type regression --severity high \
       --summary "rollup_management respawns review tasks without a cap" \
       --files "backend/app/rollup_management.py" \
       [--introduced-by <pr#|sha>] [--details "..."]
```

- `--type` is an enum, extensible. **v1 implements `regression`**; future types (e.g. `risk`) can follow without re-architecting.
- The script POSTs to a new endpoint with the agent's task token (same auth as other agent helper scripts). Follow the established helper-script generation pattern; per `CLAUDE.md`, keep the script body trivial (a single-line JSON build + POST) or use the base64'd-real-file pattern — no multi-line bash heredocs.
- A *command* (not a self-review field) is deliberate: it decouples flagging from task success — an agent can flag the moment it observes something even if its own task later fails — and it is symmetric with the agent's existing `./` vocabulary.
- Available to **all task types**, not just code-change. Ordinary review agents could flag too.

**Backend ingestion.** A new `AgentFlag` record is the structured input. The dispatcher's existing `RegressionRemediationCase` remains the remediation state machine — only the *detection input* changes from mined-themes to explicit flags. An open `AgentFlag(type=regression)` becomes a `RegressionRemediationCase` with `regression_class = "agent_flag:<fingerprint>"`, deduped by a summary/files fingerprint (mirrors the existing residual-case fingerprint approach). Because the signal is high-trust and pre-structured, no recurrence floor is needed — one flag is acted on directly.

### What stays unchanged

- All **aggregate trend signals** in the dispatcher: `pipeline_success_rate`, `fix_loop_divergence`, `persistent_anomaly`, `deploy_health`, `strategic_cycle_stall`. No single agent can see a cross-task trend; the backend's telescope is kept for exactly that.
- `sr_catch_rate` and `sr_false_positive` — these measure self-review *quality* (does SR catch what reviewers catch), a separate concern from blind-spot mining. Untouched.
- The `RegressionRemediationCase` lifecycle, recovery scan, and investigate→fix chain. Only the input feeding new cases changes.

## Key Decisions

- **D1 — Keep aggregate trends; agent declares point regressions.** (user, 2026-05-20) Agent-declared flags are a new high-trust signal *alongside* the trend signals, not a replacement for them.
- **D2 — Build after `feature/automain-lts`.** (user, 2026-05-20) Plan now; implement once that branch is merged & deployed, to avoid colliding with its working-set changes to the dispatcher and `agent_status.py`.
- **D3 — `blind_spots` stays free-text.** It is fine for an LLM reviewer; structuring it would be over-engineering. The fix is demotion (un-mining), not a schema.
- **D4 — Reuse `RegressionRemediationCase`.** `AgentFlag` is the new input; the remediation/recovery machinery is reused as-is.
- **D5 — `./flag` is one extensible command.** v1 = `regression` only; the `--type` enum leaves room to grow.

## Implementation Phases

**Phase 1 — Backend foundation.** Add the `AgentFlag` model to `models.py`; a chained alembic migration; bump `EXPECTED_SCHEMA_REVISION` in `main.py` (per `CLAUDE.md`). Add `POST /api/agent/flags` (task-token auth — see "open questions" re: `task_type` in JWT claims). Tests for the model + endpoint auth.

**Phase 2 — Agent `./flag` command.** Generate the helper script in the agent workspace (the same layer that writes `./done`/`./check`). Document `./flag` in the relevant prompt(s); bump `SELF_REVIEW_PROMPT_VERSION` since the SR prompt text changes. Agent tests: script generation, `bash -n`, POST-shape unit test.

**Phase 3 — Dispatcher swap.** Add an `agent_declared_regression` signal that scans open `AgentFlag(type=regression)` rows → creates/updates `RegressionRemediationCase(regression_class="agent_flag:<fingerprint>")` → spawns the investigate/fix chain. Fingerprint-dedup so multiple agents flagging the same thing converge on one case.

**Phase 4 — Demolition.** Remove the `sr_blind_spot` signal and everything feeding it (see Removals). This is the bulk of the dispatcher diff.

**Phase 5 — Admin UI.** Surface `AgentFlag`s (a flags list/panel + per-task indicator). Keep `blind_spots` shown on task detail as a plain note. Remove the `sr-blind-spots` digest view.

**Phase 6 — Cutover cleanup.** Close any lingering `sr_blind_spot:theme:*` regression cases. Remove dead `ops.py` / digest references. Run the full backend suite (`test-backend.sh --postgres`), agent `tsc --noEmit` + `vitest`, frontend `npm run build`.

## Data & Migration

- **New `agent_flag` table:** `id, project_id, task_id, type, severity, summary, details, files (json), introduced_by, fingerprint, status (open|triaged|resolved|dismissed), resolution, created_at, updated_at`. Migration chained from the latest alembic head; `EXPECTED_SCHEMA_REVISION` bumped to match.
- **Legacy `blind_spots` strings** in historical task records: left as-is. Nothing aggregates them after this change, so they are inert. No backfill (consistent with `project_not_public` — no back-compat shims needed for a tool with no external users).
- **Existing `sr_blind_spot:theme:*` cases:** operator-closed at cutover (the active ones were already closed 2026-05-20).

## Removals (dead code to delete in Phase 4)

- `sr_blind_spots.py`: `DISALLOWED_BLIND_SPOT_PATTERNS`, `DISALLOWED_BLIND_SPOT_EXAMPLES`, `is_disallowed_blind_spot`, `filter_disallowed_blind_spots`, `_scan_blind_spot_counts`, `aggregate_sr_blind_spot_themes`, `aggregate_sr_blind_spot_envelope`, `compute_sr_blind_spot_themes`, `triage_sr_blind_spot_theme`, `stamp_sr_blind_spot_computed_at`, `_STALE_ASSERTION_PHRASES`. (The file may end up empty or reduced to the `sr_catch_rate` / FP / reviewer-disagreement aggregations that legitimately remain — keep those.)
- `prompt.ts`: `DISALLOWED_BLIND_SPOT_EXAMPLES` and its contract test in `backend/testing/test_sr_blind_spots.py`.
- `pipeline_regression_dispatcher.py`: the `sr_blind_spot` signal evaluation, the `sr_blind_spot` entry in `_SIGNAL_STALENESS_WINDOWS`, `is_disallowed_blind_spot` call sites, and `sr_blind_spot:theme:` branches in `_maybe_spawn`.
- `routers/agent_status.py`: the `filter_disallowed_blind_spots` call.
- `routers/admin_digest.py`: `_compute_sr_disallowed_blind_spot_rate`, `GET /projects/{id}/sr-blind-spots`, and the `sr_disallowed_blind_spot_rate` digest field.

## Test Plan

- **Agent:** `./flag` script generation + `bash -n` syntax check (register in the existing generated-shell harness); a unit test asserting the POST body shape.
- **Backend:** `AgentFlag` model + migration round-trip; `POST /api/agent/flags` auth (valid token, wrong project, missing token); dispatcher `agent_declared_regression` (flag → case → spawn; fingerprint dedup; resolved/dismissed flags ignored).
- **Removal hygiene:** delete the blind-spot-theme tests in `test_sr_blind_spots.py` (the tested functionality is gone — per `CLAUDE.md`, removal is allowed only when the functionality itself is removed). Keep and adapt the `sr_catch_rate` / FP / reviewer-disagreement tests.
- Full backend suite (`test-backend.sh --postgres`), agent `npx tsc --noEmit` + `npx vitest run`, `admin-ui` `npm run build`.

## Risks & Open Questions

- **Under-flagging.** Agents may not run `./flag` when they should. Proposed mitigations were clear prompt guidance, allowing ordinary reviewers to flag, and retaining aggregate trend signals for systemic failures. Net detection was expected to *improve* (a flag is immediate; the old floor needed N recurrences).
- **Over-flagging.** Less likely with a deliberate command than with passive free-text. Fingerprint dedup + an optional per-task flag cap contain it.
- **Loss of a "recurring known-risk → prompt gap" signal.** Theme recurrence could, in principle, hint at a prompt weakness. `sr_catch_rate` already covers self-review quality; if a recurring-risk signal is still wanted, it can be rebuilt later as a clean aggregate over `AgentFlag` rows — structured data, no truncation. Out of scope for v1.
- **Endpoint auth.** `POST /api/agent/flags` should use the same task-token auth as other agent endpoints; ties into the `task_type`-in-JWT-claims work tracked separately.
- **`./flag` for non-code-change tasks.** The helper-script writer for non-code-change task types must also emit `./flag`. Confirm the generation seam covers all task types in Phase 2.

## Sequencing

Implement after `feature/automain-lts` merges and deploys. This plan rewrites `pipeline_regression_dispatcher.py` and `routers/agent_status.py`; both carry uncommitted changes on that branch today, so building now would force a collision. Phases 1–2 (new model, endpoint, agent command) are additive and could start earlier if needed; Phases 3–4 (the dispatcher swap and demolition) must wait.
