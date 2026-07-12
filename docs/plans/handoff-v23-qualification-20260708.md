# Handoff: v23 qualification + promotion (2026-07-08)

State dump for continuing the v23 promotion push in another session/tool.
Everything below was true as of ~08:15 UTC 2026-07-08.

## Where things stand

**Goal of the whole exercise**: get v23 promoted (automain → main), then open
v24 with `docs/plans/token-cost-reduction-plan-20260708.md` as its goal.

**Done so far:**

1. **automain full-suite CI debt fixed.** The full 8-shard matrix was red on
   automain HEAD (baseline: workflow run 28920446080). All causes fixed on
   branch `fix/automain-ci-green-20260708` → **PR #13507** (open, approved to
   merge by Henry). Second dispatch run **28926724775 was fully green** on the
   pre-merge branch.
2. **Engine raced us**: its own fix for #13437 plus two runner-reliability
   rollups landed on automain mid-flight (`ba50da222`). Merged automain back
   into the branch, reconciled (took the engine's non-self-repo fixture for the
   gate test; both sides independently made the same JWT-token fixture fix),
   and bumped two module-size caps its new tests breached
   (`code-change-helpers.test.ts` 1500→1900 via `--force-chronic`
   operator-approved, `done-helper.test.ts` 650→900).
3. **Merged-tree validation**: run 28927429648 failed with exactly ONE new
   failure — the automain merge brought a new `jsonFieldShellFn` export into
   `token-file.ts` and the mock-factory-export-coverage guard flagged two
   factories missing it. Fixed in `b7121b2f7`. **Run 28927982655 is IN FLIGHT**
   (`gh run view 28927982655`). If green → merge PR #13507
   (`gh pr merge 13507 --squash`). If red → fix forward on the branch, re-run
   `gh workflow run ci.yml --ref fix/automain-ci-green-20260708` (attempt-free,
   PRs into automain get ZERO checks by design — dispatch is the only way to
   validate). NOTE: if automain moves again before the merge, expect the same
   dance — merge origin/automain into the branch, reconcile, re-dispatch.
4. **v23 goal realigned with delivered work** (Henry's call: adjust goal to
   match reality rather than let a grade mismatch look bad). New goal = trust
   theme + local-runner fleet reliability + CI-green baseline. Set via
   `POST /api/admin/projects/{PROJECT}/version-goal` (500-char cap).
5. **Merge-grade risk checked — NOT a blocker**: `merge_grade_block_threshold`
   is `0.0` in runtime settings, and `release_promotion.py` arms blocking only
   when `> 0.0`. The blind merge-grade task still runs at qualification and
   records covers_goal/coverage, but a fail cannot hard-block promotion.
   An **unparseable** grade artifact, however, routes to HOLD (infra_failure)
   and retries — annoying but self-healing.

## What's left (the qualify path)

Precondition: PR #13507 merged, automain green.

1. **Attest the 4 acceptance criteria** (all `uncovered`, 0 evidence). This is
   NOT corner-cutting: the evidence recorders are stabilize-gated AND part of
   the unpromoted v23 diff itself — prod runs v22 code, so the mechanisms
   literally cannot self-record until v23 deploys. Same bootstrap circularity
   as v22, which was resolved by operator attest (precedent).
   ```
   POST /api/admin/projects/{PROJECT}/acceptance-criteria/{KEY}/attest
   ```
   Keys: `c-dd953d8c` (live-verify v22 claims — evidence commit 5a77e5bdc +
   live-verification rollup #13437 landed), `c-df6a380c` (merge-time
   test-contract integrity — drift-sentinel #13410 + self-repo smoke gate
   #13319 landed; was stuck `no_mapping`), `c-1a3ec09d` (metrics-integrity
   standing mechanism — sweep + named test + v22 backfill landed),
   `c-986db347` (acceptance_window_fraction false-zero — inferred_test_cochange
   + recompute evidence landed). Include reasoning text: "bootstrap exemption:
   mechanism merged on automain, recorder cannot run until v23 deploys".
   Check the attest endpoint's body shape in
   `backend/app/routers/admin_acceptance_criteria.py` before calling.
2. **Qualify**:
   ```
   POST /api/admin/projects/{PROJECT}/qualify  --data '{}'
   ```
   (empty JSON body required — bare POST gets a 411). Candidate = automain
   HEAD. The qualifier spawns audit + smoketest children (~15 min) and a blind
   merge-grade task (goal is now aligned, see above).
3. **Watch qualification → promotion.** Known tail risks + fixes:
   - `not_promoted` with audit findings → `GET .../audit-findings` is
     authoritative (names file + mechanism); fix by file, merge to automain,
     re-qualify. (Ledger auto-stales findings when HEAD advances.)
   - `stabilize`/`required_check_failed` after promotion PR opens = the FULL
     suite failed on the promotion PR. Should NOT happen — we just made the
     exact same matrix green — but if it does: reconcile against any parallel
     engine fix before pushing (stale branch REVERTS it), never rebase away
     the engine's work.
   - Deploy stuck MONITORING with `no_new_revision` → leader-restart orphaned
     window_start; unstick: `POST /api/admin/deploy-state/mark-stable`.
4. **After promote + deploy confirm**: open v24. Commit
   `docs/plans/token-cost-reduction-plan-20260708.md` to the repo first so the
   strategist can read it, then set the v24 goal to the cost plan's headline
   (cost-per-verified-task down 3-4x; see plan's execution order). Version cut:
   `POST /api/admin/projects/{PROJECT}/version-cut` (check body shape in
   admin_release_version.py).

## Access / mechanics

- API: base URL + key in `.blueprint-config.json` (repo root). Auth header is
  `Authorization: Bearer <api_key>` (NOT X-API-Key).
- Blueprint project id: `37d445e1-0775-4403-bacf-1cd7ed129336`.
- Backend tests: `bash scripts/test-backend.sh testing/test_<x>.py` (Docker
  Postgres harness; never the bare full suite on a small VM). Agent:
  `cd agent && npx tsc --noEmit` + `npx vitest run <files>`. Admin-ui:
  `npx tsc -b --noEmit` (`npm run typecheck` also runs a baseline script that
  false-positives on this Windows machine — CI is authoritative for that bit).
- Full-suite validation for automain-targeted branches:
  `gh workflow run ci.yml --ref <branch>` then `gh run watch`.

## Context that explains the weird stuff

- **Why automain CI debt accumulates silently**: per-commit CI on automain was
  deliberately cut (Actions-minutes cost); `ci.yml` triggers only on PRs into
  `main` + workflow_dispatch. Merge gates use `requires_ci_checks=false`.
  The full matrix normally runs ONCE, at the automain→main promotion PR.
- **What the CI fixes were** (PR #13507, merged commit will carry details):
  2 real code bugs (Blueprint-specific prompt guidance leaking into
  non-Blueprint projects' self-review block; `parent_task_id` missing from two
  `load_only(raiseload=True)` projections after the main→automain back-merge —
  that one bug was ~93 failures), 1 cross-module test-state leak
  (test_max_plan_pool's autouse fixture now clears after yield too), and a
  set of stale test contracts updated to CURRENT intended behavior — the
  behavior changes were all intentional v23 trust-theme work
  (finalized-only sweep counts, fail-closed review_loop verification,
  fleet-no-healthy aggregate alert, `modify` task-type retirement,
  typecheck-not-build admin-ui gate, JWT-shape token validation,
  fresh-reviewer baseline redefinition).
- **Pipeline state**: autonomy was manually paused by Henry (to observe
  DeepSeek costs); in-flight v23 tasks have finished. Per-project cap for
  Blueprint was raised 3→40 (`max_concurrent_tasks_by_project`, API-only
  setting, not in the UI); global `max_concurrent_vms=15` is the binding cap.
  Resume autonomy when ready — qualification itself does not need the
  dispatch loop unpaused, but the stabilize-phase reconcile recorders and any
  audit-finding fix tasks do.
- **v24 planning context**: `docs/plans/token-cost-reduction-plan-20260708.md`
  (updated tonight: cache economics section, worker-residency section, DeepSeek
  two-lane routing decision recorded, off-peak scheduling, turns-per-task
  metric, deflation posture). Related: `docs/plans/resident-workflow-v2-design.md`
  owns the residency design. Strategic stance settled 2026-07-08: no
  self-funding schemes, no startup push — the lever is cost-per-task down
  3-4x. Kaggle witness project kept open as an information-first benchmark,
  gated on DeepSeek-lane validation + per-project spend cap.
