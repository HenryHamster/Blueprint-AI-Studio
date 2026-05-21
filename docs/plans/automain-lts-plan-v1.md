# Automain Integration Trunk + Release Qualifier — Design & Plan v1

**Date:** 2026-05-19
**Status:** Proposed
**Scope:** Self-repo project (Blueprint). The machinery is project-generic; only
its *activation* is gated on `project.is_self_repo`.
**Source:** Design session (Henry + Claude), grounded in codebase exploration.

## 1. Context & Motivation

The orchestrator deploys from `main` on every merge (`.github/workflows/deploy.yml`
triggers on push to `main`). Blueprint self-improves on its own repo, so the loop is:

> orchestrator spawns work → work merges to `main` → `main` deploys → the
> orchestrator **is now** the merged code → it judges the next round with the
> merged code's standards.

The system has no fixed reference frame. Every gate it uses to judge its own
output — auto-review, quality heuristics, spawn logic, prompts — is itself
editable by that output. When a change subtly weakens the judge, the next round
produces slightly worse work that passes the now-weaker judge, and it ratchets.
Left running unattended, the system drifted into a self-built failure mode: not
one bad commit, but slow degradation because nothing holds still.

**The fix is a fixed reference frame.** Split the branch the pipeline builds on
from the branch the running system deploys from, and only advance the deploy
branch through an explicit, audited qualification gate.

## 2. Goals & Non-Goals

**Goals**
- Break the self-degradation loop: a bad autonomous change cannot reach the
  running system without clearing a gate.
- `main` becomes a human-grade LTS; `automain` becomes the pipeline's integration
  trunk.
- Promotion `automain → main` is gated by an **automated** release qualifier —
  no per-release human approval.
- Confidence is **differential** (measured against `main`, the last known-good)
  and **composite** (CI + test-erosion + audit + smoketest).
- A "version" has a lifecycle (build → stabilize → qualify → promote) driven by
  the strategic layer.
- A working hotfix path for production breakage.

**Non-Goals**
- Calcification alerting (a version that never promotes) — belongs in the
  operator messaging system; explicitly out of scope here.
- Full per-project modular tooling — parked. Only the *project-generic
  qualification battery* (one sliver of that idea) is load-bearing here.
- Backward-compat shims — Blueprint has no external users.

## 3. Architecture Overview

```
feature ──▶ staging ──▶ automain ──[ qualifier ]──▶ main ──▶ prod
blueprint/*  research/*  (integration  (gate, once   (LTS)   (Cloud Run;
             plan/*       trunk)        per version)          deploys on
                                                              main push only)

version on automain:  build ──▶ stabilize ──▶ qualify ──▶ promote ──▶ next version
                      └─────── strategic-agent driven ───────┘
```

- **Branch model.** The existing topology (feature → research/plan staging →
  rollup) is unchanged. The only structural change: rollups retarget from `main`
  to `automain`, and a new promotion step sits above `automain`.
- **Reference frame.** The orchestrator keeps deploying from `main`. Because
  `main` now only advances at promotion, the running system only changes at
  promotion. The agent VM image is built from `agent/**` on `main` (Packer),
  so it freezes with `main` too — no Packer change needed. The judging
  components are therefore pinned to the last audited release.
- **Version lifecycle.** A version is the span of `automain` between two
  promotions. The strategic layer drives it: **build** (feature work), then
  **stabilize** (shift work-mix to audit/cleanup/hardening), then **qualify**
  (run the heavy gate once), then **promote** or fall back to stabilize.
- **Two-tier checking.** Cheap checks (CI, test-erosion) run continuously on
  `automain` as hygiene. The heavy qualification (a `main`-version audit + the
  smoketest) runs **once per version at qualify-time** — not per push.
- **Trusted core.** The promotion *policy* (thresholds + weights) lives in a
  human-owned file. Any autonomous change to it requires human approval. The
  qualifier *code* is ordinary code — it improves on `automain` and self-gates
  through its own promotion. The human owns the rubric, never the per-release click.
- **Hotfix.** Production breakage → expedited fix branched off `main` → merged
  to `main` → back-merged to `automain`.

## 4. Key Decisions

- **D1 — `main` is the LTS; the running system deploys from it.** This is what
  breaks the loop. Cost: pipeline self-improvements take effect only at
  promotion. Accepted — a stable factory building fast-moving products.
- **D2 — No per-release human approval.** The human owns `promotion_policy.yaml`
  (the definition of "confident enough"); the qualifier owns every release
  decision. Without a human-owned policy the system could promote itself by
  lowering its own bar — the drift problem one level up.
- **D3 — Confidence is differential and composite.** You cannot prove `automain`
  correct. You can establish it is *not worse than `main`* plus passes additive
  checks. Every sub-score diffs `main...automain`.
- **D4 — Heavy qualification is episodic.** Triggered by the strategic agent
  when a version reaches qualify-readiness — not on every push.
- **D5 — Versions live in the strategic layer, not the branches.** Branches stay
  dumb; a `ReleaseVersion` row carries phase. Phase-transition cadence is the
  strategic agent's call — no fixed cadence config (revisit only if it misbehaves).
- **D6 — The qualification battery is project-declared.** The qualifier and
  smoketest are generic; the battery (`testing/smoketest.yaml`, health-check
  URLs) comes from project config. This keeps the LTS machinery from becoming
  Blueprint-specific.
- **D7 — Reuse, don't reinvent.** The rollup-target plumbing, `recommended_mode`
  work-mix system, `smoketest` task type, critical-files gate, CI-polling and
  GitHub merge helpers all already exist and are used as-is.

## 5. Implementation Phases

### Phase 0 — Centralize the integration-branch config
*Goal:* one source of truth, so Phase 1 is a one-line flip. *Risk:* very low
(pure refactor; value stays `"main"`).
- Add an `integration_branch` key to the `runtime_config` structure in
  `backend/app/shared_state.py` (default `"main"`; operator-tunable, persists via
  `runtime_config_json`).
- Route autonomous spawn-time `base_branch` defaults through it:
  `autonomous_research.py` (`:873`, `:1450-1453`), `strategic_spawning.py`
  (`:1135`, `:1527`), `admin.py:2378` (`self_audit`).
- Leave alone: `github.py:get_repo_default_branch()` (different concept —
  GitHub repo default for clone fallbacks); `admin.py` *retrieval* fallbacks for
  already-spawned tasks; agent-side `'main'` fallbacks in
  `agent/src/handlers/standard.ts` / `specialized.ts` (safety nets — the
  orchestrator always sends `base_branch` explicitly).
- *Sequencing:* first — every later phase needs the knob. Ships as a no-op.

### Phase 1 — Create `automain`, retarget rollups, freeze `main`
*Goal:* the loop is broken. *Risk:* medium (the structural cutover). Mitigated:
`automain` starts as an exact copy of `main`; promotion is interim-manual until
Phase 3, so a bad `automain` cannot reach production.
- **[HUMAN]** Create `automain` from `main` HEAD in a quiet window (no in-flight
  rollups): `git branch automain main && git push origin automain`.
- **[HUMAN — `.github/workflows/`, cannot be done by the pipeline]** `ci.yml:9`
  is `pull_request: branches: [main]`. Rollup PRs will target `automain`; without
  this change they merge with no CI. Change to `branches: [main, automain]`.
- **[HUMAN — `.github/workflows/`]** Add branch protection on `automain`
  mirroring `main` (require CI), so `merge_pull_request` cannot merge a red PR.
- Flip `integration_branch` from `"main"` to `"automain"`. Because Phase 0
  routed every autonomous spawn site through it, this single change retargets
  research, periodic tasks, `self_audit`, and (via the `base_branch` →
  `parent_base` → rollup-target plumbing in `plan_completion.py:206-221` /
  `rollup_management.py:1200`) every rollup PR.
- `deploy.yml` stays `branches: [main]` — **no change**. This is the crux: `main`
  stops advancing on every merge, so the orchestrator freezes between promotions.
- *Interim:* until Phase 3, a human periodically opens a PR `automain → main`,
  lets CI run, and merges. Acceptable — the loop is already broken by the flip.
- *Gotcha:* in-flight tasks at cutover carry `base_branch:"main"` in persisted
  payloads; their rollups still target `main`. Benign — flip when the queue is
  near-drained.

### Phase 2 — Release state + strategic version-phase awareness
*Goal:* a "version" becomes a first-class entity; the strategic agent drives
build/stabilize transitions. *Risk:* low-medium (new table + payload field).
- **New model `ReleaseVersion`** (`backend/app/models.py`):
  `id`, `project_id` (FK, indexed), `version_number` (monotonic per project),
  `phase` (`"build"`|`"stabilize"`, default `build`), `status`
  (`"open"`|`"promoted"`|`"abandoned"`, default `open`), `opened_at`,
  `phase_changed_at`, `promoted_at`, `base_sha` (nullable), `promoted_sha`
  (nullable), `last_qualification` (JSONB), `qualification_attempts` (default 0),
  `phase_reasoning` (text). A "version" = the row with `status="open"`.
- **Migration** `backend/alembic/versions/0100_releaseversion_table.py`,
  `down_revision = "0099"`. Bump `EXPECTED_SCHEMA_REVISION` in
  `backend/app/main.py:124` from `"0099"` to `"0100"` **in the same PR**. Per the
  migration linter: SHA columns nullable; give other non-null columns
  `server_default`. Seed a version-1 row for the self-repo project.
- **New helper** `backend/app/release_version.py` (mirrors `app/strategic_cycle.py`):
  `open_version`, `current_open_version`, `count_merged_since`.
- **Strategic awareness** — extend the Phase 2 `strategic_review` payload
  (`strategic_spawning.py:1462-1532`) with a small `version_phase` block
  (`version_number`, `phase`, `phase_changed_at`, `tasks_merged_this_version`,
  `last_qualification`); register it in `_STRATEGIC_REVIEW_SCHEMA` (`:324`) at
  `priority:"high"` so it survives `pack_under_budget`.
- **Work-mix biasing** — extend the `mode_hint_lines` assembly
  (`strategic_spawning.py:1333-1350`): when `phase=="stabilize"`, append a
  `prompt_suffix` directing the agent to prefer `audit`/`fundamentals`
  `recommended_mode` and defer net-new feature directions. Reuses the existing
  `recommended_mode` / `direction_mode.py` machinery — no new work-mix mechanism.
- **Inbound** — accept an optional `version_phase_decision` in the strategic
  agent's output; apply it in `strategic_completion.py` next to the
  `recommended_mode` handling; update the open `ReleaseVersion` on a phase change.
- *Sequencing:* before the qualifier (which writes `last_qualification` and opens
  the next version). Version-phase awareness is independently valuable.

### Phase 3 — Release qualifier + automated promotion
*Goal:* `automain → main` gated by an automated composite qualifier. *Risk:*
highest — decides what reaches production. Mitigations: ships behind
`runtime_config["promotion_enabled"]` (default `False`, dry-run); a failed
qualifier only ever *blocks* (system stays in stabilize, which is safe).
- **Trigger:** the strategic agent (which owns phase cadence) emits a `qualify`
  request when an `open` version in `stabilize` is ready. The orchestrator then
  snapshots `automain` HEAD as the candidate SHA and runs the qualifier.
- **The qualifier** (`backend/app/release_qualifier.py`) — an
  orchestrator-coordinated composite, not a single agent task:
  1. *CI green* (cheap, orchestrator-side) — `get_check_runs_cached` on the
     candidate SHA. **Hard blocker.**
  2. *Test-erosion* (cheap) — diff `main...automain` for deleted/weakened tests
     (detail in Phase 4). **Hard blocker** on test-file deletion; graded penalty
     otherwise.
  3. *`main`-version audit* (heavy, agent task) — spawn an audit task that
     **clones `main`** and reviews the `main...automain` diff against `main`'s
     standards (give the audit handler a `diff_range` payload field). Normalized
     to `audit_score ∈ [0,1]`.
  4. *Smoketest* (heavy, agent task) — spawn the existing `smoketest` task type
     against the candidate SHA; `SMOKETEST_RESULT` → `smoketest_score`
     (`passed/total`, 0 if the startup suite failed).
- **Composite** — weighted sum of normalized sub-scores, gated by hard blockers:
  ```
  hard_blockers = [ci not green, smoketest startup suite failed,
                   a test file was deleted]
  if any(hard_blockers): confidence = 0.0   # do not promote
  else: confidence = w_audit*audit + w_test*test_erosion + w_smoke*smoketest
  promote if confidence >= threshold
  ```
  Weights + threshold live in `backend/promotion_policy.yaml` (defaults:
  `audit 0.40 / test_erosion 0.30 / smoketest 0.30`, `threshold 0.75`).
- **Promotion** (when `promote == True` and `promotion_enabled`):
  open a PR `automain → main`, `merge_pull_request` with `merge_method="merge"`
  (preserve integration history; branch protection on `main` runs CI as a final
  gate). Treat a non-merged result as a failed promotion — do not retry blindly.
  `deploy.yml` then fires automatically. Mark the `ReleaseVersion`
  `status="promoted"` + record SHAs/score; open the next version (`build` phase).
- **On `promote == False`:** record the attempt + blocking component on the
  `ReleaseVersion`, keep `status="open"`, set `phase="stabilize"`. The next
  strategic cycle sees the failed `last_qualification` and continues stabilize
  work targeting the blocker.
- **Repurpose the smoketest:** the post-rollup-merge smoketest spawn
  (`task_lifecycle.py:3447`, mirrored in `admin.py:826` / `:1972`) currently
  fires on rollup-merge-to-`main`. After Phase 1 normal rollups merge to
  `automain`; change the condition so it fires on `main` merges (= promotions) as
  a post-promotion production health-check. The qualify-time smoketest is a
  separate spawn from `release_qualifier.py`, marked with a context flag so its
  failure does **not** spawn a fix task.
- *Sequencing:* after Phase 2. Ships in dry-run (`promotion_enabled=False`):
  compute and log the score, do not promote, while interim-manual promotion
  continues.

### Phase 4 — Trusted core, test-erosion, tagging, calibration
*Goal:* harden the qualifier. *Risk:* medium.
- **Trusted core** — add `"backend/promotion_policy.yaml"` to **both**
  `INFRA_FILES` and `CRITICAL_FILES` in `task_lifecycle.py:2517-2531`
  (`touched_critical` is the intersection of the two sets — verified). The
  existing PR-file flagging then sets `requires_approval=True` on any task
  touching it, blocking merge until a human clears it via `admin.py:2088`. **Zero
  new gate code.**
- **Test-erosion check** — fold into the `main`-version audit task (it already
  clones the repo and can `git diff`): emit a structured `test_erosion` block.
  Hard-block on any deleted test file (the differential form of the smoketest's
  existing `guardrails.min_test_count`); graded penalty on net assertion/test
  count reduction.
- **Release tagging** — add `github.create_tag()` (annotated tag via `git/tags`
  + `git/refs`, mirroring `create_branch`). Tag `v{version_number}` on the
  promoted `main` SHA inside `promote()`, wrapped in try/except — a tag failure
  must not fail a promotion. `ReleaseVersion.version_number` is the source of
  truth; the tag is cosmetic. Rollback uses `DeploymentState`
  (`last_known_good_sha` / `prior_deploy_sha`), which already exists.
- **Calibration** — with `promotion_enabled=False`, run the qualifier on every
  interim-manual promotion candidate and log the composite without acting.
  Compare its verdict against the human's decision over ~5–10 versions, tune the
  weights/threshold, then flip `promotion_enabled=True`. After the flip there is
  no human in the release path — the human's only standing role is owning
  `promotion_policy.yaml`.
- *Sequencing:* after Phase 3. Calibration gates the kill-switch flip.

### Phase 5 — Hotfix path + back-merge
*Goal:* production breakage → expedited fix off `main` → merged to `main` →
back-merged to `automain`. *Risk:* low-medium.
- A hotfix is a task with `base_branch="main"` and a `hotfix:true` marker; it
  branches `hotfix/*` off `main`, gets an expedited (high-`priority`) review, and
  merges to `main` → `deploy.yml` fires.
- *Trigger:* operator-initiated, plus the post-promotion smoketest-failure path
  (`agent_status.py:1175`, which already spawns a fix `plan` task) — when the
  failing smoketest's `base_branch` is `main`, spawn the fix with
  `base_branch="main"` and `hotfix=True`.
- *Back-merge:* after a hotfix merges to `main`, the orchestrator opens and
  auto-merges a `main → automain` back-merge PR. On conflict, raise an `Alert`
  and spawn a `plan` task — never leave the branches silently diverged (a failed
  back-merge makes the next promotion conflict).
- *Sequencing:* last — only matters once promotions are automated.

## 6. Risks & Gotchas

1. **`.github/workflows/` is off-limits to the pipeline.** Phase 1's `ci.yml` +
   branch-protection changes are hard human prerequisites and must land before
   the `integration_branch` flip, or rollup PRs merge into `automain` with no CI.
2. **The `main`-version audit must clone `main`, not `automain`.** The single
   most important correctness constraint — it must review the diff against
   `main`'s standards. If it runs on `automain`, the system judges new code with
   the new code's standards: the exact drift this design eliminates.
3. **`merge_pull_request` does not enforce CI itself** — it returns
   `{"merged": False}` on a 405. The promotion path must check the result and
   treat non-merged as a failed promotion. Branch protection is the real gate.
4. **`EXPECTED_SCHEMA_REVISION` bump must ship in the same PR as the `0100`
   migration**, or the schema check in `main.py` fails the deploy.
5. **Two smoketests after Phase 3** (pre-promotion qualifier input vs
   post-promotion production health). The pre-promotion one must be flagged so
   its failure does not spawn a fix task.
6. **`automain`/`main` divergence.** Every promotion closes the gap; hotfixes
   reopen it; the Phase 5 back-merge closes it. The back-merge conflict-alert is
   mandatory, not optional.
7. **Strategic payload size** sits near the 256 KB hard limit. `version_phase` is
   tiny and `priority:"high"`, so it is safe — but verify it survives
   `pack_under_budget`.
8. **Activation is self-repo-gated.** Versions/qualifier activate only for
   `project.is_self_repo`; the machinery stays project-generic (per D6).

## 7. Rollout & Calibration

The loop is broken at the **end of Phase 1** — everything after makes promotion
autonomous and safe. Promotion stays interim-manual through Phases 1–4 while the
qualifier runs in dry-run. Flip `promotion_enabled=True` only after calibration
(§Phase 4) shows the qualifier's verdicts tracking reality. There is no permanent
human gate; calibration is one-time bootstrapping of trust in the gate.

## 8. Open Items / Future Work

- **Calcification alerting** — a version stuck in stabilize/qualify is *safe*
  (`main` stays good) but should notify the operator. `qualification_attempts` is
  recorded so the operator-messaging system can surface it later. Out of scope here.
- **Other projects** opting into versioned releases — the machinery is generic;
  only activation is self-repo-gated today.
- **Full per-project modular tooling** — parked. This design needed only the
  project-generic qualification battery.

## 9. Verification (per phase)

- **Phase 0:** `bash scripts/test-backend.sh --postgres`; confirm
  `integration_branch` resolves to `"main"` and spawned tasks are unchanged.
- **Phase 1:** on a scratch project, confirm new research/rollup PRs target
  `automain`; confirm a push to `automain` does **not** trigger `deploy.yml`;
  confirm a merge to `main` still deploys.
- **Phase 2:** migration up/down clean; `test-backend.sh --postgres`; confirm the
  strategic payload carries `version_phase` and a forced `stabilize` phase biases
  mode hints toward audit/fundamentals.
- **Phase 3:** with `promotion_enabled=False`, run the qualifier on a candidate
  and confirm it logs a composite score and does **not** promote; force each hard
  blocker and confirm `confidence=0`.
- **Phase 4:** confirm a PR touching `promotion_policy.yaml` sets
  `requires_approval=True`; confirm a deleted test file hard-blocks; confirm a
  promotion creates the `v{N}` tag and a tag failure does not abort it.
- **Phase 5:** simulate a post-promotion smoketest failure → confirm a `hotfix`
  task off `main`; confirm the `main → automain` back-merge PR; force a conflict
  and confirm an `Alert` + `plan` task.
- All phases: `cd admin-ui && npm run build` if UI touched; `cd agent &&
  npx tsc --noEmit` if agent touched.

## 10. Critical Files

- `backend/app/shared_state.py` — `integration_branch`, `promotion_enabled`
- `backend/app/autonomous_research.py`, `strategic_spawning.py`,
  `strategic_completion.py` — branch defaults, version-phase awareness
- `backend/app/task_lifecycle.py` — critical-files gate, smoketest repurpose
- `backend/app/models.py` — `ReleaseVersion`
- `backend/alembic/versions/0100_releaseversion_table.py` — new migration
- `backend/app/main.py` — `EXPECTED_SCHEMA_REVISION` bump
- `backend/app/release_qualifier.py`, `release_version.py` — new modules
- `backend/promotion_policy.yaml` — new, trusted core
- `backend/app/github.py` — `create_tag`; reuse merge/PR/check helpers
- `backend/app/routers/agent_status.py` — smoketest result routing, hotfix
- `.github/workflows/ci.yml` — **human-only**: add `automain` to PR branches
