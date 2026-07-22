# Test Estate Ranked Queue — Classifications and Evidence

> Created: 2026-07-22 | Derived from rubric: `test-estate-rubric.md`
> Scope: `backend/testing/` (404 files, ~799K LOC), `admin-ui` vitest (367 files, ~116K LOC), `agent` vitest (86 files, ~75.5K LOC)

## Total Test Estate Summary

| Area | Files | Non-viewport LOC | Viewport LOC | Standalone | DB-backed |
|------|-------|-------------------|--------------|------------|-----------|
| backend/testing/ | 404 | ~799K | — | ~124 | ~280 |
| admin-ui (unit) | 209 | ~85K | — | all (happy-dom) | — |
| admin-ui (viewport) | 158 | ~31K | included | — | all (Chromium) |
| agent | 86 | ~75.5K | — | all (vitest) | — |
| **Total** | **~857** | **~991K** | — | — | — |

---

## Legend

- **G** = Good (keep as-is)
- **I** = Improve (action needed)
- **C** = Cut (tested functionality removed)
- Priority: P0–P6 per rubric

---

## P0 — BROKEN IMPORTS (Blocks pytest Collection)

These test files reference modules that no longer exist. While they may be lazy imports, they block execution of specific test functions and prevent the file from being fully exercised.

### 1. `backend/testing/test_reaper.py` — 19,521 lines
- **Classification**: I (improve)
- **Priority**: P0
- **Evidence**:
  - Line 3524: `import app.strategic_resident_activation_sweep as activation_sweep` — **function body import** of a deleted module. Raises `ModuleNotFoundError` at runtime if that test function executes.
  - Line 3592: Same import in another test function body.
  - Lines 3539, 3598, 6435: references `engine_owner="strategic_resident"` (deleted engine type).
  - The rest of the file tests live functionality (reaper sweeps, lifecycle events).
- **Action**: fix broken imports — update to the replacement module API (`app.sr_*` or remove the tests for removed resident-specific behavior). Do NOT cut the file; only the resident-specific test functions are cut-eligible.
- **Test coverage impacted**: Reaper module lifecycle tests, alert dispatch tests.

---

## P1 — TESTS FOR REMOVED FUNCTIONALITY (Cut-Candidate)

### 2. `backend/testing/test_task_capabilities.py` — lines 609–614
- **Classification**: C (cut — the assertion block only)
- **Priority**: P1
- **Evidence**:
  - Lines 609–614: Asserts `"strategic_resident_turn"` task-type exists in the task-type registry with specific properties.
  - The `strategic_resident_turn` task type was removed when the strategic resident subsystem was excised.
  - The rest of the file (~2500+ lines) tests live task capability logic.
- **Action**: Remove the 5-line assertion block for `strategic_resident_turn` from the task-type registry test. Do NOT cut the entire file.
- **Test coverage preserved**: All remaining task-type registry assertions.

### 3. `backend/testing/test_task_outcomes.py` — line 1288 (panel_review ref)
- **Classification**: C (cut — the test class only)
- **Priority**: P1
- **Evidence**:
  - `TestPanelLensPassed` class tests `_panel_lens_passed` utility function specific to panel_review.
  - Panel review subsystem is being removed (design confirmed, removal PRs are active).
  - The rest of the file tests generic task outcome logic.
- **Action**: Remove the `TestPanelLensPassed` class (likely 50–100 lines). The rest of the file is Good.
- **Test coverage preserved**: All remaining outcome tests.

---

## P2 — CRITICAL SIZE (> 5000 lines)

These files are serial execution bottlenecks and maintenance hazards. The agent test suite especially is bottlenecked by individual massive test files.

### Backend

| Rank | File | Lines | Classification | Evidence |
|------|------|-------|----------------|----------|
| 4 | `backend/testing/test_release_qualifier.py` | 45,438 | I — split | Largest single test file in the project. Tests the release qualifier end-to-end with many enumerated scenario rows. |
| 5 | `backend/testing/test_admin_routers.py` | 33,340 | I — split | Covers 25+ admin router endpoints. A single file is a CI serialization bottleneck; split by router (runners, tasks, analytics, etc.). |
| 6 | `backend/testing/test_agent.py` | 27,582 | I — split | Tests the main agent router. Large fixture-oriented test class. |
| 7 | `backend/testing/test_compute.py` | 26,292 | I — split | Tests compute backend provisioning. Enumeration of many compute image permutations. |
| 8 | `backend/testing/test_strategic_spawning.py` | 19,685 | I — split | Tests strategic spawning logic. Overlaps with test_strategic_completion.py, test_strategic_state.py. Consider dedup. |
| 9 | `backend/testing/test_reaper.py` | 19,521 | I — split | Also has P0 broken imports. A 19K file with dead resident references. |
| 10 | `backend/testing/test_invariants.py` | 12,851 | I — split | Behavioral invariant tripwires. Many independent assertions that could logically separate. |

### Agent

| Rank | File | Lines | Classification | Evidence |
|------|------|-------|----------------|----------|
| 11 | `agent/src/index.test.ts` | 8,554 | I — split | Largest agent test. Tests main entrypoint: credential management, task lifecycle, LLM runner, heartbeat, crash reporting, terminal statuses. 8K+ lines is a maintenance burden and slow to execute. |
| 12 | `agent/src/prompt.test.ts` | 5,825 | I — split | Tests the prompt builder. Contains 5+ independently-scoped test blocks for formatPlaybook, buildContextPrefix, configureAgentPermissions, etc. Each could be a separate file. |

### Admin-ui

| Rank | File | Lines | Classification | Evidence |
|------|------|-------|----------------|----------|
| 13 | `admin-ui/src/components/MessagesPanel.test.tsx` | 9,482 | I — split | Largest admin-ui non-viewport test. Tests message panel with hundreds of interactions and state permutations. ~9.5K lines is a serial bottleneck. |

---

## P3 — HIGH SIZE (3000–5000 lines)

These are approaching the critical threshold. Proactive splitting is recommended.

### Backend

| Rank | File | Lines | Classification | Evidence |
|------|------|-------|----------------|----------|
| 14 | `backend/testing/test_rollup_management.py` | ~4,500 (est) | I — split | Rollup management test file with multiple independent feature areas. |
| 15 | `backend/testing/test_review_pipeline.py` | ~3,500 (est) | I — split | Tests for review pipeline with modularity assertions that can separate. |
| 16 | `backend/testing/test_digest_metrics.py` | ~3,200 (est) | I — strengthen | Also imports from `app.sr_review_quality_metrics` — check that module-level import still resolves. |

### Agent

| Rank | File | Lines | Classification | Evidence |
|------|------|-------|----------------|----------|
| 17 | `agent/src/handlers/standard.test.ts` | 5,367 | I — split | Second-largest agent test. Tests handleCreate, handleRefactor with prompts, workspace, branches, PRs, lint loops. Overlaps with code-change.test.ts. |
| 18 | `agent/src/handlers/research-plan.test.ts` | 3,963 | I — split | Tests research and plan handlers. Mix of prompt assembly, capability declarations, acceptance rubric. |
| 19 | `agent/src/utils/test-runners.test.ts` | 3,832 | I — split | Tests test runner detection, fixture classification, acceptance probes. Heavily parameterized; fix data could be external. |
| 20 | `agent/src/handlers/review-fix.test.ts` | 3,742 | I — split | Tests review/fix handler. Many independent prompt-building tests that could live separately. |
| 21 | `agent/src/handlers/strategic-sections.test.ts` | 2,870 | I — assess | Tests individual strategic prompt sections. Good assertion quality but large. |

### Admin-ui

| Rank | File | Lines | Classification | Evidence |
|------|------|-------|----------------|----------|
| 22 | `admin-ui/src/components/ComputeJobsPanel.test.tsx` | 4,779 | I — split | Large component test. Could split by interaction pattern (launch, monitor, terminate). |
| 23 | `admin-ui/src/components/VersionLifecyclePanel.test.tsx` | 4,540 | I — split | Large lifecycle panel test. |
| 24 | `admin-ui/src/components/FleetRunnersPanel.test.tsx` | 2,833 | I — assess | Approaching 3K. Monitor for growth. |
| 25 | `admin-ui/src/components/DashboardHome.test.tsx` | 2,733 | I — assess | Approaching 3K. |

---

## P4 — OVER-MOCKED TESTS / WEAK ASSERTIONS

### Agent Tests — Heavy Mock Surface

The agent test suite is the most heavily mocked in the estate. While mocks are sometimes necessary (avoiding real API calls), the cumulative effect is that many tests only verify the mock wiring, not the actual behavior.

| Rank | File | Lines | Classification | Evidence |
|------|------|-------|----------------|----------|
| 26 | `agent/src/handlers/standard.test.ts` | 5,367 | I — reduce mock surface | Uses `vi.mock()` for `../index.js`, `fs`, `child_process`, `prompt.js`, `telemetry.js`, `workspacePrep.js`, `build-config.js`, `test-runners.js`. Most assertions verify mock call arguments, not real task behavior. Many pathed exports replicate real module exports via string lists. |
| 27 | `agent/src/handlers/research-plan.test.ts` | 3,963 | I — reduce mock surface | Same mock pattern: index, fs, child_process, plus per-module mocks. |
| 28 | `agent/src/handlers/review-fix.test.ts` | 3,742 | I — reduce mock surface | Same heavy mock pattern. |
| 29 | `agent/src/handlers/specialized.test.ts` | 2,691 | I — reduce mock surface | Same heavy mock pattern for migrate/debug/perf/investigate handlers. |
| 30 | `agent/src/handlers/strategic.test.ts` | 2,384 | I — reduce mock surface | Same pattern. |
| 31 | `agent/src/handlers/audits.test.ts` | 1,634 | I — reduce mock surface | Same pattern for audit handlers. |
| 32 | `agent/src/handlers/strategic-helpers.test.ts` | 1,377 | I — reduce mock surface | Tests generated helper scripts (./strategic, ./fetch, ./design-doc). Actually writes to temp directory and checks file contents — one of the agent tests with more real behavior. Moderate mock surface. |

### Admin-ui Tests — Mock Factory Infrastructure

| Rank | File | Lines | Classification | Evidence |
|------|------|-------|----------------|----------|
| 33 | `admin-ui/src/components/MessagesPanel.test.tsx` | 9,482 | I — assess mocking | Uses centralized mocks from `src/test/api-mock-complete.ts` and firebase-mock-complete. The mock-complete infrastructure means the test never hits real API shapes. The `mock-factory-export-coverage.test.ts` guard (1170 lines) exists specifically to police this — suggesting the mock-surface problem is acknowledged. |
| 34 | `admin-ui/src/test/mock-factory-export-coverage.test.ts` | 1,170 | I — reduce | The fact that a 1,170-line test file exists just to enforce mock-factory completeness is a code smell. The mock factories should be auto-generated or the test surface should be reduced. |

---

## P5 — MODERATE SIZE (1500–3000 lines) / NON-STANDALONE-ELIGIBLE

When touched, consider splitting or converting.

### Backend — Large DB-backed Tests That Could Shrink

| Rank | File | Lines | Classification | Evidence |
|------|------|-------|----------------|----------|
| 35 | `backend/testing/test_reaper_sweeps.py` | ~2,800 (est) | I — assess | DB-backed reaper sweeps tests. Could some sweep logic be tested standalone? |
| 36 | `backend/testing/test_task_capabilities.py` | ~2,500 | I — consolidate | Has the P1 cut-eligible strategic_resident_turn block. After removal, still ~2,500 lines. |
| 37 | `backend/testing/test_agent_strategic_fetch.py` | ~2,400 | G | Tests agent strategic fetch router. Good DB-backed coverage of live functionality. |
| 38 | `backend/testing/test_compute_image_remote_build.py` | ~2,200 | I — make standalone | Remote build logic has pure functions that could be tested independently of DB. |
| 39 | `backend/testing/test_release_scoring.py` | ~2,000 | G | Good standalone candidate, specific assertions. |
| 40 | `backend/testing/test_digest.py` | ~2,000 | I — refactor | Module-level import from `app.sr_review_quality_metrics` (still exists). Tests digestion metrics. |

### Admin-ui — Component Test Growth

| Rank | File | Lines | Classification | Evidence |
|------|------|-------|----------------|----------|
| 41 | `admin-ui/src/components/SrTrendBreakdown.test.tsx` | 2,519 | I — assess | Large component test with complex fixture data. |
| 42 | `admin-ui/src/components/StrategicContextRail.test.tsx` | 2,299 | I — assess | |
| 43 | `admin-ui/src/components/TaskDetailDrawerMeta.test.tsx` | 2,175 | I — assess | |
| 44 | `admin-ui/src/hooks/useWorkbenchCommands.test.tsx` | 2,133 | I — assess | |
| 45 | `admin-ui/src/components/WarmVmPanel.test.tsx` | 1,769 | I — assess | |

---

## P6 — VIEWPORT TEST INFRASTRUCTURE (Informational)

Viewport tests (158 files) run in real Chromium via Playwright and cannot execute on agent VMs. They are CI-only gates.

| Observation | Detail |
|-------------|--------|
| **Infra-dependency** | All 158 `*.viewport.test.tsx` files require Chromium. The `./check` harness and `scripts/test.sh` skip them gracefully when Docker/Chromium is unavailable. |
| **Execution cost** | Serialized (file parallelism=false) to avoid Playwright route race conditions. This makes the viewport suite among the slowest per-file in the estate. |
| **Coverage utility** | Asserts layout properties (overflow, touch targets, text truncation) that happy-dom cannot check. These tests provide unique value but are expensive. |
| **Current health** | `viewport-coverage.test.ts` and `viewport-required.json` enforce that every operator component has viewport coverage. Escape hatches are tracked in `viewport-escape-hatch-budget.json`. |
| **Recommendation** | Keep as-is. The viewport gate provides unique layout safety that no other test modality covers. Do not add viewport tests for non-operator components. |

---

## GOOD FILES (Keep As-Is) — Representative Selection

These files demonstrate the patterns other tests should follow.

### Backend — Standalone Gold

| File | Lines | Why Good |
|------|-------|----------|
| `backend/testing/test_security.py` | ~800 | Standalone (`--noconftest`). Tests URL security, injection guards. Specific assertions. |
| `backend/testing/test_pipeline.py` | ~1,200 (est) | Standalone. Tests pipeline health. |
| `backend/testing/test_anomaly_detection.py` | ~1,400 (est) | Standalone. Pure logic, no DB. |
| `backend/testing/test_acceptance_criteria.py` | ~1,000 (est) | Standalone. Structural evaluation of acceptance criteria. |
| `backend/testing/test_field_encryption.py` | ~600 (est) | Standalone. Field-level encryption logic. Specific assertions on encrypted output. |
| `backend/testing/test_golden_suite.py` | ~800 (est) | Standalone. Golden task spec validation. |
| `backend/testing/test_json_extract.py` | ~300 (est) | Standalone. Pure JSON extraction logic. |
| `backend/testing/test_time_utils.py` | ~400 (est) | Standalone. Time utility functions. |
| `backend/testing/test_models.py` | ~500 (est) | Standalone. Model validation logic. |
| `backend/testing/test_schema_invariants.py` | ~400 (est) | Standalone. Schema contract invariants. |

### Backend — DB-Backed but Well-Structured

| File | Lines | Why Good |
|------|-------|----------|
| `backend/testing/test_message_audience.py` | ~1,200 (est) | DB-backed but focused scope, specific assertions on audience routing. |
| `backend/testing/test_task_locking.py` | ~1,000 (est) | Focused test of task locking logic. Specific concurrency assertions. |
| `backend/testing/test_operator_inbox_liveness.py` | ~1,500 (est) | Tests inbox liveness endpoint. Well-scoped. |
| `backend/testing/test_claim_analytics.py` | ~800 (est) | Focused analytics tests with specific metric assertions. |
| `backend/testing/test_max_plan_pool.py` | ~900 (est) | Pool capacity logic with specific boundary assertions. |

### Agent — Good Tests

| File | Lines | Why Good |
|------|-------|----------|
| `agent/src/handlers/code-change.test.ts` | 681 | Focused handler test for a specific behavior. Reasonable mock surface. |
| `agent/src/handlers/merge-grade.test.ts` | 505 | Well-scoped, specific assertions on merge-grade evaluation. |
| `agent/src/taskCapabilities.test.ts` | 114 | Small, focused contract test. |
| `agent/src/providers/usage.test.ts` | 86 | Lean utility test. |
| `agent/src/handlers/web-research.test.ts` | 653 | Good scope for a research handler test. |
| `agent/src/handlers/structured-output-repair.test.ts` | 692 | Good parameterized test of repair loop. |

### Admin-ui — Good Tests

| File | Lines | Why Good |
|------|-------|----------|
| `admin-ui/src/hooks/useConversation.test.tsx` | ~1,200 | Tests hook logic with renderHook and act. Focused on real behavior (pagination, optimistic updates). |
| `admin-ui/src/utils/time.test.ts` | ~300 (est) | Pure utility test with typed assertions. |
| `admin-ui/src/utils/taskStatus.test.ts` | ~200 (est) | Utility function tests with specific state-machine assertions. |
| `admin-ui/src/test/viewport-coverage.test.ts` | ~400 (est) | Infrastructure gate that enforces viewport coverage invariants. Unique value. |

---

## CROSS-CUTTING OBSERVATIONS

### 1. The agent suite is the most mock-heavy

The agent test suite (86 files, 75.5K LOC) consistently follows a pattern of mocking every external dependency — `fs`, `child_process`, `index.js`, every handler module. This means many tests verify call-wiring rather than actual behavior. The benefit is speed (no real Git/FS operations), but the cost is that a refactored module with unchanged external behavior can break tests purely on internal mock-wiring details.

**Recommendation**: Introduce integration-test files that test handlers against a real (temp-dir) filesystem and a fake-but-functional API, reducing the mock surface for behavior tests. Keep the mock-based tests for contract/regression checks only.

### 2. The backend suite is the most diverse and fragmented

~70 distinct name prefixes across 404 test files. This is a sign of organic growth. Good and bad aspects:
- **Good**: Clear naming convention (`test_<module>.py`), easy to find the test for any module.
- **Bad**: Some files are massive (45K-line `test_release_qualifier.py`) while feature-adjacent tests live in separate 200-line files. No consistent splitting strategy.

**Recommendation**: Establish a "one test file per submodule, max 1500 lines" convention. Use the module_size_limits.json guardrail (already deployed for production code) for test files.

### 3. The admin-ui suite has a healthy split of unit vs. layout tests

The two-suite architecture (happy-dom for logic + Chromium for layout) is a good pattern. The 158 viewport tests provide unique layout coverage that no other test modality captures. However, the mock-complete infrastructure is a growing maintenance burden (the 1,170-line `mock-factory-export-coverage.test.ts` guard is a symptom).

**Recommendation**: Generate mock factories from TypeScript type definitions rather than maintaining them by hand. This would eliminate most of the mock-factory-export-coverage test body.

### 4. Cut eligibility is extremely narrow in this project

Per project policy, tests may only be cut when the tested functionality is removed. This means the classic "cut" candidates (slow tests, flaky tests, low-value tests) are all **Improve** candidates in this rubric. The only confirmed cut candidates are:
- The `strategic_resident_turn` assertions in `test_task_capabilities.py`
- The `TestPanelLensPassed` class in `test_task_outcomes.py`
- The resident-specific test functions in `test_reaper.py` (after broken imports are fixed)

---

## QUEUE SUMMARY

| Priority | Count | Action |
|----------|-------|--------|
| P0 | 1 file | Fix broken lazy imports |
| P1 | 2 files (small blocks) | Remove cut-eligible assertion blocks |
| P2 | 10 files | Split files > 5000 lines |
| P3 | 12 files | Split files 3000–5000 lines |
| P4 | 9 files | Reduce mock surface / strengthen assertions |
| P5 | 11 files | Fix when touched |
| P6 | 158 files (viewport) | Keep as-is (informational) |

**Total actionable files (P0–P5): ~45 files**
**Total keep-as-is (G): ~800+ files** (majority of the estate is Good)

---

## METHODOLOGY

This queue was constructed from:
1. File size measurements (all files scanned)
2. Import analysis (`grep` for strategic_resident, panel_review references)
3. Mock surface analysis (`grep` for `vi.mock` patterns in agent/admin-ui)
4. Standalone eligibility review (smoketest.yaml curated/regression suites)
5. Cross-reference with project context: active tasks that overlap with these findings (strategic_resident excision, design_doc migration)

No test suite was executed to produce this queue (per sequencing constraint: rubric lands first, baseline measurement is a follow-on task).
