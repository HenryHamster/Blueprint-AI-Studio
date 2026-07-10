import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { WORKSPACE } from '../index.js';
import type { BuildConfig } from '../index.js';
import { SHELL_CMD_RE } from '../shell-safety.js';
import { agentTokenShellFn, jsonFieldShellFn } from '../token-file.js';
import {
  buildScopedLintOneliner,
  detectLinterKind,
  validateCfgShellSafety,
  isBlueprintSelfRepo,
  shouldRunBackendMergeGate,
  sanitizeCiFailureSelectors,
  type CodeChangeHelperConfig,
} from './code-change-helpers.js';

export const BLUEPRINT_ADMIN_UI_ESLINT_CMD = 'npx eslint 2>&1';
export const BLUEPRINT_AGENT_ESLINT_CMD = 'npx eslint 2>&1';
export const BLUEPRINT_AGENT_TSC_CMD = 'npx tsc --noEmit 2>&1';
export const BLUEPRINT_BACKEND_RUFF_CHECK_CMD = 'python3 -m ruff check --select E,F,W --ignore E501 2>&1';
export const BLUEPRINT_BACKEND_RUFF_FORMAT_CMD = 'python3 -m ruff format --check 2>&1';

// ── Lazy-loaded bash templates (extracted to .sh files) ───────────────────────
// Per CLAUDE.md "Generating Code for One Language From Another": multi-line
// bash blocks are extracted to real .sh files (directly lintable, testable).
// The generator reads them at module-load time and injects them directly into
// the generated script — no base64 needed because these are bash fragments
// embedded in a bash generation context (not a different language inside bash).
// Lazy-loaded so test suites which mock fs do not crash on module import.
const _CHECK_HELPER_DIR = path.dirname(fileURLToPath(import.meta.url));

const _CI_FAILURE_VERIFY_PATH = path.join(_CHECK_HELPER_DIR, 'check_ci_failure_verify.sh');
function loadCiFailureVerifyScript(): string {
  try { return fs.readFileSync(_CI_FAILURE_VERIFY_PATH, 'utf8'); } catch { return ''; }
}

const _MERGE_VERIFY_PATH = path.join(_CHECK_HELPER_DIR, 'check_merge_verify.sh');
function loadMergeVerifyScript(): string {
  try { return fs.readFileSync(_MERGE_VERIFY_PATH, 'utf8'); } catch { return ''; }
}

const _IMPACTED_TEST_PATH = path.join(_CHECK_HELPER_DIR, 'check_impacted_test.sh');
function loadImpactedTestScript(): string {
  try { return fs.readFileSync(_IMPACTED_TEST_PATH, 'utf8'); } catch { return ''; }
}

const _VIEWPORT_UPLOAD_PATH = path.join(_CHECK_HELPER_DIR, 'check_viewport_upload.sh');
function loadViewportUploadScript(): string {
  try { return fs.readFileSync(_VIEWPORT_UPLOAD_PATH, 'utf8'); } catch { return ''; }
}

const _VISUAL_CAPTURE_PATH = path.join(_CHECK_HELPER_DIR, 'check_visual_capture.sh');
function loadVisualCaptureScript(): string {
  try { return fs.readFileSync(_VISUAL_CAPTURE_PATH, 'utf8'); } catch { return ''; }
}

/** Emit the ci_failure closed-loop verify block for the generated ./check.
 *
 *  For a ci_failure fix task we know the EXACT pytest node-ids CI reported
 *  failing. The fix loop was previously open: Claude edited, ran ./check
 *  (lint/typecheck + the standalone-3 backend smoke files), then ./pr open
 *  pushed — but ./check never ran the actual CI-failing selectors (the
 *  standalone gate covers ~3 of ~100 test files), so the fix shipped blind and
 *  CI re-failed, burning a ~20-min provision+CI round-trip per iteration.
 *
 *  This block runs those exact selectors LOCALLY via the sanctioned Docker
 *  harness (`scripts/test-backend.sh --postgres <selector...>`). Targeted
 *  single-selector runs fit the agent VM's memory headroom — only the FULL
 *  suite OOMs (that's why the standalone gate exists), and the harness's own
 *  memory guard explicitly exempts `*.py` / `*::*` args. A genuine failure of
 *  a target selector lands in FAILED, so ./check fails closed and ./pr open
 *  (which gates on ./check) cannot push until the CI-failing tests pass.
 *
 *  Infra degradation is graceful: if Docker is unavailable on this VM, the
 *  block can't run the sanctioned harness and a bare local pytest would be the
 *  unfaithful SQLite fallback — so it WARNS and does NOT block (CI remains the
 *  authoritative backstop). A real, runnable, still-failing target test always
 *  blocks. Selectors are pre-sanitized (shell-safe pytest node-ids) by the
 *  caller, so single-quote interpolation here is injection-safe.
 *
 *  Runs on EVERY ./check (not behind WITH_TESTS): a CI failure can occur on a
 *  non-LTS branch where the WITH_TESTS gate is off, and ./pr open gates on
 *  plain ./check there. Verifying the targeted selectors is cheap and exact. */
function buildCiFailureVerifyBlock(selectors: string[], taskId: string): string {
  if (selectors.length === 0) return '';
  // Single-quoted bash literals — selectors cleared PYTEST_SELECTOR_RE.
  const quoted = selectors.map(s => `'${s}'`).join(' ');
  return loadCiFailureVerifyScript()
    .replace(/@CI_FAIL_SELECTORS@/g, quoted)
    .replace(/@TASK_ID@/g, taskId);
}

/** Emit the merge-resolution closed-loop verify block for the generated ./check.
 *
 *  A conflict-resolution fix (the agent rebased/merged the base branch and
 *  resolved conflicts) can silently break tests OUTSIDE the standalone gate's 3
 *  files: a dropped fixture line (FK violation) or a code/test vintage skew
 *  between the branch and a fast-moving base. The standalone `--no-postgres`
 *  gate never runs those DB-backed files, so a bad resolution shipped clean and
 *  CI re-failed, burning the whole review-fix budget (incident: rollup PR #8920,
 *  research task 72e4b062 — 16 iterations exhausted on two tests the resolution
 *  broke, not the diff under review).
 *
 *  This block derives the affected backend test files from $CHANGED (already
 *  computed above): every `backend/testing/test_*.py` the merge touched, plus
 *  `test_<module>.py` for each changed `backend/app/<module>.py` that has one.
 *  It runs them under the sanctioned Postgres harness — bounded (capped; never
 *  the full suite, which OOMs the 8 GB VM — the harness memory guard exempts
 *  targeted *.py args). A still-broken affected test fails this gate, and since
 *  ./pr open gates on ./check, the push is blocked until it passes. Overflow
 *  past the cap is logged (deferred to CI), never silently dropped. Graceful
 *  when Docker is absent: warn + GATE-ENVIRONMENT marker, don't block (CI runs
 *  the authoritative suite on the PR). Derivation uses only $CHANGED + on-disk
 *  test-file existence, so it reflects the RESOLVED tree at ./check time. */
function buildMergeVerifyBlock(taskId: string): string {
  return loadMergeVerifyScript().replace(/@TASK_ID@/g, taskId);
}

/** Emit the impacted-test merge-time gate for the generated ./check.
 *
 *  The standalone `--no-postgres` smoke run alongside this block covers only the
 *  ~10-file invariant set. Test files that exercise the CHANGED modules — by
 *  importing `app.<module>` or by mock-patching `app.<module>...` — and even test
 *  files the PR ITSELF ADDS, never run mechanically before merge, so a contract
 *  break merges green on the task PR and only surfaces at the promotion-PR full
 *  matrix, burning a long diagnosis+re-provision round-trip.
 *
 *  This block derives the impacted backend test set from $CHANGED by piping it
 *  into the committed reverse-dependency resolver
 *  (`backend/testing/impacted_tests.py`) — invoked BY REFERENCE, NOT an embedded
 *  Python heredoc (see CLAUDE.md's "Generating Code for One Language From
 *  Another"). It runs the impacted files under the sanctioned Postgres harness,
 *  bounded (capped; never the full suite, which OOMs the 8 GB VM — the harness
 *  memory guard exempts targeted *.py args). A still-failing impacted test lands
 *  in FAILED, and since ./pr open gates on ./check, the push is blocked until it
 *  passes. Overflow past the cap is logged (deferred to CI), never silently
 *  dropped. Graceful when Docker is absent: warn + GATE-ENVIRONMENT marker,
 *  don't block (CI runs the authoritative suite on the PR). Does nothing (no
 *  output, no failure) when the impacted set is empty. It is additive — it does
 *  NOT replace the standalone smoke run; the two are complementary. */
function buildImpactedTestBlock(taskId: string): string {
  return loadImpactedTestScript().replace(/@TASK_ID@/g, taskId);
}

/** Emit a bash sub-block that uploads the failed viewport screenshot(s) for a
 *  PR-only regression through the signed-URL endpoint.  Every failure mode is
 *  non-fatal (WARN + continue) so the only blocking gate remains the viewport
 *  regression itself.  Emitted only inside the PR-only-regression else-branch
 *  so it never runs for --full or baseline-also-broken paths.
 *
 *  The block:
 *  1. Guards on jq, curl, AGENT_API_TOKEN, and ORCHESTRATOR_URL.
 *  2. Reads admin-ui/test-results/viewport-failures.json (written by the JSON
 *     reporter in vitest.viewport.config.ts).
 *  3. For each failed test, extracts component and width from the fullName
 *     (e.g. "EffortEfficiencyPanel — viewport regression renders without
 *     overflow at 375px") using sed/grep — no eval, no unquoted expansions.
 *  4. Finds the screenshot whose filename best matches that failed test rather
 *     than reusing the first PNG in admin-ui/test-results/.
 *  5. POSTs to the signed-URL endpoint and PUTs the PNG to the returned URL.
 *  6. Logs the storage_uri on success. */
function buildViewportUploadBlock(taskId: string): string {
  return loadViewportUploadScript().replace(/@TASK_ID@/g, taskId);
}

/** Emit the SUCCESS-PATH visual-acceptance capture+upload block.  Presence-gated
 *  on a declared `visual` acceptance-evidence requirement: this function is only
 *  called (and the block only appears in the generated ./check) when
 *  `cfg.visualAcceptance` is set, so a task with no visual declaration produces
 *  a byte-identical ./check.
 *
 *  Distinct from {@link buildViewportUploadBlock} (the FAILURE-path block that
 *  uploads a regression screenshot): this runs when ./check is GREEN, so a
 *  declared visual evidence can actually be SATISFIED on a passing change — not
 *  only when a regression is detected. It:
 *  1. Guards on the admin-ui stack having run (deps + Chromium already
 *     bootstrapped), jq, curl, AGENT_API_TOKEN, ORCHESTRATOR_URL, and all
 *     checks green ($\{#FAILED[@]\} == 0).
 *  2. Runs the capture-mode viewport config (vitest.viewport.capture.config.ts)
 *     scoped to the declared component, with VIEWPORT_SHOT_DIR set — maybeCapture
 *     writes one PNG per (test, width) under that dir.
 *  3. Locates the PNG for EXACTLY the declared width.
 *  4. POSTs {task_id, component, width} to the failure-screenshot-upload-url
 *     endpoint (which stamps the canonical context URI) and PUTs the PNG.
 *
 *  Because the context URI key is a single last-write-wins scalar, it captures
 *  and uploads ONLY the declared (component, width) pair so resolve_visual_evidence
 *  matches. Every failure mode is non-fatal (WARN + continue) — this is NOT a
 *  blocking gate. component/width are validated shell-safe by
 *  validateCfgShellSafety, so single-quote/JSON interpolation here is injection-safe. */
function buildVisualCaptureBlock(taskId: string, component: string, width: number): string {
  return loadVisualCaptureScript()
    .replace(/@TASK_ID@/g, taskId)
    .replace(/@COMPONENT@/g, component)
    .replace(/@WIDTH@/g, String(width));
}

/** Detect whether the given stack can capture visual evidence. Capability-gated
 *  on admin-ui being available and having the viewport harness infrastructure:
 *  - Blueprint self-repo: YES (has hardcoded viewport blocks)
 *  - Config-driven with admin-ui in directories: YES
 *  - Config-driven without admin-ui or foreign repo: NO */
function canCaptureVisual(
  isSelfRepo: boolean,
  buildConfig: BuildConfig | undefined,
): boolean {
  if (isSelfRepo) {
    return true;
  }
  if (buildConfig?.directories?.some(dir => dir.path.replace(/\/$/, '') === 'admin-ui')) {
    return true;
  }
  return false;
}

/** Emit a loud skip message when visual evidence is declared but cannot be
 *  captured. This ensures unmet declarations are observable downstream instead
 *  of silently degraded. Emitted as a best-effort advisory (non-fatal). */
function buildVisualSkipBlock(reason: string): string {
  return `
# ── visual acceptance evidence — cannot capture ────────────────────────────────
# Visual acceptance evidence was declared but this stack cannot capture it.
echo "[check] visual acceptance declared but ${reason} — skipping capture"
# ── end visual acceptance evidence skip ────────────────────────────────────────
`;
}

export function writeCheckHelper(cfg: CodeChangeHelperConfig): void {
  validateCfgShellSafety(cfg);
  const ciFailureSelectors = sanitizeCiFailureSelectors(cfg.ciFailureSelectors);
  const agentTokenFn = agentTokenShellFn();
  const jsonFieldFn = jsonFieldShellFn();
  // Auto-detects which stacks were touched (admin-ui / agent / backend) by
  // inspecting `git diff --name-only` against the base branch + uncommitted.
  // Runs the corresponding lint/build commands plus the admin-ui browser-mode
  // viewport suite. Optional `./check test` also runs the Docker backend test
  // suite when backend/ was touched and tests-on-main is enabled.
  const header = `#!/bin/bash
# ./check — run lint/build (and optionally tests) for changed paths.
# Usage:
#   ./check         # lint+build only — fast, run before each iteration
#   ./check test    # also run tests (slow; mainly before ./pr open)
#   ./check full    # run repo-wide lint (eslint .) + full viewport suite
#                   # WITHOUT scoping. Skips the baseline recheck on
#                   # failure (because the result IS the new baseline).
#                   # Use sparingly — when you're confident your edits
#                   # affect a shared rule / config / global selector
#                   # whose effect a scoped check can't surface, OR
#                   # when a baseline-warned WARN is hiding a real
#                   # regression you want to confirm. Combine with
#                   # 'test' (./check full test) to also run tests.
# Exits 0 on green, non-zero on any failure. Output is the actual error tail
# so you can act on it. Re-run after edits.

set -uo pipefail
${agentTokenFn}
${jsonFieldFn}
WORKSPACE="\${WORKSPACE:-${WORKSPACE}}"
cd "$WORKSPACE" || { echo "Error: cannot cd to $WORKSPACE" >&2; exit 2; }

# Sanity: make sure we're in a git repo. Without this, the diff commands
# below silently swallow errors and CHANGED becomes empty -> all stack
# blocks skipped -> "PASS" with zero actual checks.
git rev-parse --git-dir >/dev/null 2>&1 || {
  echo "Error: $WORKSPACE is not a git repo (missing .git)" >&2
  exit 2
}

WITH_TESTS=0
FULL_SCOPE=0
for arg in "$@"; do
  case "$arg" in
    test|--tests) WITH_TESTS=1 ;;
    full|--full)  FULL_SCOPE=1  ;;
  esac
done

# Fresh GATE-ENVIRONMENT marker per run: cleared here, re-written by the
# config-driven pytest gate below only if this environment still cannot run
# the suite faithfully. ./pr surfaces it after a passing \`./check test\`.
rm -f /tmp/check_gate_env_${cfg.taskId}.txt

# Determine changed paths: committed since base + uncommitted. Verify at
# least one diff source actually worked — silently-empty CHANGED used to
# fall through to "PASS — all checks green" without running anything.
BASE="${cfg.baseBranch}"
COMMITTED=""
if git rev-parse --verify "origin/$BASE" >/dev/null 2>&1; then
  COMMITTED=$(git diff --name-only "origin/$BASE...HEAD" 2>/dev/null || true)
elif git rev-parse --verify "$BASE" >/dev/null 2>&1; then
  COMMITTED=$(git diff --name-only "$BASE...HEAD" 2>/dev/null || true)
else
  echo "[check] WARNING: neither origin/$BASE nor $BASE exists locally — falling back to working-tree diff only" >&2
fi
UNCOMMITTED=$(git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null)
CHANGED=$(printf '%s\\n%s\\n' "$COMMITTED" "$UNCOMMITTED" | sort -u | grep -v '^$' || true)

touches() { printf '%s\\n' "$CHANGED" | grep -q "^$1"; }

admin_ui_runtime_touched() {
  printf '%s\\n' "$CHANGED" | grep -Eq '^(admin-ui/src/|admin-ui/(package\\.json|package-lock\\.json|vite\\.config\\.ts|vitest(\\.viewport)?\\.config\\.ts|eslint\\.config\\.js|eslint-rules/))'
}

FAILED=()

run() {
  local label="$1"; shift
  echo "[check] $label..."
  if "$@"; then
    echo "[check] $label OK"
  else
    echo "[check] $label FAILED"
    FAILED+=("$label")
  fi
}

# Idempotent npm install guarded by package-lock IDENTITY, not the mere
# existence of node_modules. A warm/reused VM carries a populated
# node_modules from a prior task; a bare \`[ -d node_modules ] || npm ci\`
# then skips the install even when package-lock.json changed on this branch,
# leaving STALE deps that mask or fake build/lint/test results. We hash
# package-lock.json and stash the hash in a sentinel inside node_modules,
# reinstalling only when it's absent or mismatched.
#
# NOTE: the sentinel guards lockfile IDENTITY (did the resolved dep set
# change?), NOT node_modules tree-integrity (is every package actually on
# disk and intact?). A matching hash means "this node_modules was last
# installed from THIS lockfile", not "node_modules is provably complete".
# That's the right tradeoff here: the warm-VM failure we're closing is a
# CHANGED lockfile being ignored, and \`git clean\` at role handoff already
# resets a corrupted tree. Call from within the target directory.
npm_ci_if_lock_changed() {
  local want sentinel=node_modules/.pkg-lock-hash
  want=$(sha256sum package-lock.json 2>/dev/null | cut -d' ' -f1)
  # Fail-safe: if we can't compute a hash (missing/unreadable lockfile or no
  # sha256sum), install unconditionally rather than trust a stale tree.
  if [ -n "$want" ] && [ -f "$sentinel" ] && [ "$(cat "$sentinel" 2>/dev/null)" = "$want" ]; then
    return 0
  fi
  npm ci || return 1
  # Write the sentinel ONLY after a zero-exit \`npm ci\`, so a killed/partial
  # install never leaves a false-positive hash that suppresses a real retry.
  # Recompute \`want\` post-install in case the lockfile was (re)written.
  want=$(sha256sum package-lock.json 2>/dev/null | cut -d' ' -f1)
  [ -n "$want" ] && printf '%s\\n' "$want" > "$sentinel"
}
`;
  const blueprintStackBlocks = `
if admin_ui_runtime_touched; then
  ( cd admin-ui && npm_ci_if_lock_changed ) >/tmp/check_npm_ci.log 2>&1 || {
    echo "[check] admin-ui npm ci FAILED — see /tmp/check_npm_ci.log"
    tail -30 /tmp/check_npm_ci.log
    FAILED+=("admin-ui npm ci")
  }
  # Scope eslint to changed files. PR gates shouldn't be blocked by
  # pre-existing errors in untouched files (same rationale as ruff). We
  # do NOT baseline-warn eslint: errors in files the agent edited are
  # the agent's responsibility to fix (or carve out scope for) per the
  # "don't leave preexisting issues" principle. --full forces repo-wide.
  if [ "$FULL_SCOPE" = "1" ]; then
    run "admin-ui eslint (full, --full)" bash -c 'cd admin-ui && npx eslint . 2>&1'
  else
    : > /tmp/check_admin_ui_lint.txt
    printf '%s\\n' "$CHANGED" | grep -E '^admin-ui/.*\\.(ts|tsx|js|jsx|cjs|mjs)$' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\\0' "\${f#admin-ui/}" >> /tmp/check_admin_ui_lint.txt
    done
    if [ -s /tmp/check_admin_ui_lint.txt ]; then
      run "admin-ui eslint (changed)" bash -c 'cd admin-ui && xargs -0 -a /tmp/check_admin_ui_lint.txt ${BLUEPRINT_ADMIN_UI_ESLINT_CMD}'
    else
      echo "[check] no admin-ui JS/TS files changed, skipping eslint"
    fi
  fi

  # Viewport gate: scoped run + baseline recheck.
  #
  # 1) SCOPING: by default, run only the viewport tests whose component
  #    .tsx (or .viewport.test.tsx) was touched. Repo-wide viewport stays
  #    a CI/main concern; on PRs we don't want pre-existing overflow or
  #    import drift in untouched components to block unrelated fixes.
  #    If a non-component admin-ui file changed (shared types, App.tsx,
  #    package.json, src/test/*), fall back to the full suite — that
  #    edit could break any component.
  #
  # 2) BASELINE: if the (scoped or full) run fails, re-run the SAME
  #    subset against origin/$BASE in a detached worktree. Both fail →
  #    pre-existing → warn-pass (mirrors frontend CI's BROKEN/OK
  #    pattern). PR-only fail →
  #    real regression → block. Worktree path keeps the agent's working
  #    tree untouched.
  ADMIN_UI_CHANGED=$(printf '%s\\n' "$CHANGED" | grep -E '^admin-ui/' || true)
  NON_COMPONENT=$(printf '%s\\n' "$ADMIN_UI_CHANGED" | grep -vE '^admin-ui/src/components/[^/]+\\.(viewport\\.test\\.tsx|tsx)$' || true)

  # Check for new admin-ui components without viewport tests.
  # Uses process substitution (\`done < <(...)\`) so the loop runs in the
  # current shell — a piped \`while\` would run in a subshell and any
  # FAILED+= or flag set inside would be lost, leaving the gate
  # advisory-only.
  if [ "$FULL_SCOPE" = "0" ] && [ -n "$ADMIN_UI_CHANGED" ]; then
    NEW_COMPONENTS=$(printf '%s\\n' "$ADMIN_UI_CHANGED" | grep -E '^admin-ui/src/components/[^/]+\\.tsx$' | grep -vE '\\.test\\.tsx$|\\.viewport\\.test\\.tsx$' || true)
    if [ -n "$NEW_COMPONENTS" ]; then
      MISSING_VIEWPORT_TESTS=()
      while IFS= read -r comp; do
        [ -z "$comp" ] && continue
        [ ! -f "$comp" ] && continue
        [ -f "\${comp%.tsx}.viewport.test.tsx" ] && continue
        basename "$comp" .tsx | grep -qE '^[a-z]' && continue
        grep -q '@viewport-test-exempt' "$comp" 2>/dev/null && continue
        MISSING_VIEWPORT_TESTS+=("$comp")
      done < <(printf '%s\\n' "$NEW_COMPONENTS")
      if [ \${#MISSING_VIEWPORT_TESTS[@]} -gt 0 ]; then
        echo "[check] ERROR: New component(s) without viewport test:" >&2
        for comp in "\${MISSING_VIEWPORT_TESTS[@]}"; do
          echo "  $comp" >&2
          echo "  Expected test file: \${comp%.tsx}.viewport.test.tsx" >&2
        done
        echo "  See admin-ui/src/test/viewport.ts for examples" >&2
        FAILED+=("admin-ui new component viewport test")
      fi
    fi
  fi

  : > /tmp/check_viewport_tests.txt
  if [ "$FULL_SCOPE" = "0" ] && [ -z "$NON_COMPONENT" ]; then
    printf '%s\\n' "$ADMIN_UI_CHANGED" | grep -E '^admin-ui/src/components/[^/]+\\.viewport\\.test\\.tsx$' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\\n' "\${f#admin-ui/}" >> /tmp/check_viewport_tests.txt
    done
    printf '%s\\n' "$ADMIN_UI_CHANGED" | grep -E '^admin-ui/src/components/[^/]+\\.tsx$' | grep -v '\\.viewport\\.test\\.tsx$' | while IFS= read -r f; do
      vt="\${f%.tsx}.viewport.test.tsx"
      [ -f "$vt" ] && printf '%s\\n' "\${vt#admin-ui/}" >> /tmp/check_viewport_tests.txt
    done
  fi
  VIEWPORT_FILTER=$(sort -u /tmp/check_viewport_tests.txt 2>/dev/null | tr '\\n' ' ' | sed 's/ *$//')
  VIEWPORT_RUN=1
  if [ "$FULL_SCOPE" = "0" ] && [ -z "$NON_COMPONENT" ] && [ -z "$VIEWPORT_FILTER" ]; then
    echo "[check] no viewport test matches changed components, skipping viewport suite"
    VIEWPORT_RUN=0
  fi

  if [ "$VIEWPORT_RUN" = "1" ]; then
    # Bootstrap Playwright Chromium binary before the viewport gate.
    # The baked agent VM already includes the system libs (libatk, libnss,
    # etc.) — and may already have the browser cached, so we cache-check
    # first and fall through to the bare install, with --with-deps as a
    # last-resort fallback for fresh local machines.
    # See admin-ui/CONTRIBUTING.md for developer-machine setup.
    run "admin-ui playwright chromium" bash -c '
      cd admin-ui || exit 1
      if [ -d "$HOME/.cache/ms-playwright" ] && ls "$HOME/.cache/ms-playwright"/chromium-* >/dev/null 2>&1; then
        exit 0
      fi
      if npx playwright install chromium 2>&1; then
        exit 0
      fi
      if npx playwright install --with-deps chromium 2>&1; then
        exit 0
      fi
      echo "[check] Retrying inside Docker container..." >&2
      docker run --rm -v "$PWD:/work" -w /work mcr.microsoft.com/playwright:v1.59.1-jammy npx playwright install chromium 2>&1 || exit 1
    ' 2>&1
  fi

  if [ "$FULL_SCOPE" = "1" ]; then
    VIEWPORT_FILTER=""
    VIEWPORT_LABEL="admin-ui viewport (full, --full)"
  elif [ -n "$NON_COMPONENT" ]; then
    VIEWPORT_LABEL="admin-ui viewport (full)"
  elif [ -n "$VIEWPORT_FILTER" ]; then
    VIEWPORT_LABEL="admin-ui viewport (changed: $(echo "$VIEWPORT_FILTER" | wc -w | tr -d ' ') file(s))"
  fi

  if [ "$VIEWPORT_RUN" = "1" ]; then
    echo "[check] $VIEWPORT_LABEL..."
    PR_VP_LOG=/tmp/check_viewport_pr.log
    export VIEWPORT_FILTER
    if ( cd admin-ui && VITE_API_URL=http://localhost:8080 VITE_FIREBASE_API_KEY=ci VITE_FIREBASE_AUTH_DOMAIN=ci.firebaseapp.com VITE_FIREBASE_PROJECT_ID=ci npm run test:viewport -- $VIEWPORT_FILTER 2>&1 ) > "$PR_VP_LOG" 2>&1; then
      tail -20 "$PR_VP_LOG"
      echo "[check] $VIEWPORT_LABEL OK"
    elif [ "$FULL_SCOPE" = "1" ]; then
      # --full mode: skip baseline recheck. The result IS the new baseline.
      echo "[check] $VIEWPORT_LABEL FAILED (--full, no baseline recheck)"
      tail -100 "$PR_VP_LOG" || true
      FAILED+=("$VIEWPORT_LABEL")
    else
      echo "[check] $VIEWPORT_LABEL FAILED on PR — checking baseline on origin/$BASE..."
      BASELINE_FAILED=0
      BASELINE_REASON=""
      BASE_REF="origin/$BASE"
      if ! git rev-parse --verify "$BASE_REF" >/dev/null 2>&1; then
        BASE_REF="$BASE"
        git rev-parse --verify "$BASE_REF" >/dev/null 2>&1 || BASELINE_REASON="base ref unavailable"
      fi
      if [ -z "$BASELINE_REASON" ]; then
        WT=/tmp/check_viewport_baseline_wt
        # Stale worktree from a prior run can wedge \`worktree add\`.
        # \`worktree remove --force\` is the safe cleanup (no rm -rf).
        git worktree remove --force "$WT" >/dev/null 2>&1 || true
        if ! git worktree add --detach "$WT" "$BASE_REF" >/tmp/check_viewport_wt.log 2>&1; then
          BASELINE_REASON="worktree add failed (see /tmp/check_viewport_wt.log)"
        else
          MAIN_VP_LOG=/tmp/check_viewport_main.log
          # Baseline run uses the SAME scoped subset — VIEWPORT_FILTER
          # is exported above. If the test files don't exist on main
          # (newly added in this PR), vitest exits 0 with "no tests"
          # and we treat baseline as PASSING (so PR-side failure is
          # real). Acceptable: new test files should pass when added.
          if ( cd "$WT/admin-ui" && npm ci >/tmp/check_viewport_main_npmci.log 2>&1 && { [ -d "$HOME/.cache/ms-playwright" ] && ls "$HOME/.cache/ms-playwright"/chromium-* >/dev/null 2>&1 || npx playwright install chromium || npx playwright install --with-deps chromium; } >/tmp/check_viewport_main_pw.log 2>&1 && VITE_API_URL=http://localhost:8080 VITE_FIREBASE_API_KEY=ci VITE_FIREBASE_AUTH_DOMAIN=ci.firebaseapp.com VITE_FIREBASE_PROJECT_ID=ci npm run test:viewport -- $VIEWPORT_FILTER 2>&1 ) > "$MAIN_VP_LOG" 2>&1; then
            BASELINE_FAILED=0
          else
            BASELINE_FAILED=1
          fi
          git worktree remove --force "$WT" >/dev/null 2>&1 || true
        fi
      fi
      if [ "$BASELINE_FAILED" = "1" ]; then
        echo "[check] $VIEWPORT_LABEL WARN — also broken on $BASE_REF; not blocking PR"
        echo "[check] PR-side tail:"
        tail -30 "$PR_VP_LOG" || true
        echo "[check] Baseline-side tail:"
        tail -30 /tmp/check_viewport_main.log || true
      else
        echo "[check] $VIEWPORT_LABEL FAILED — passes on $BASE_REF, fails on PR. Real regression."
        [ -n "$BASELINE_REASON" ] && echo "[check] (baseline check skipped: $BASELINE_REASON — treating PR failure as real)"
        tail -100 "$PR_VP_LOG" || true
        FAILED+=("$VIEWPORT_LABEL")
${buildViewportUploadBlock(cfg.taskId)}      fi
    fi
  fi

  # Typecheck on agent VM; vite bundle OOMs here and is CI's job.
  run "admin-ui typecheck" bash -c 'cd admin-ui && npm run typecheck 2>&1'

  # Run the admin-ui unit vitest suite on EVERY ./check (not gated behind \`test\`):
  # mirrors the agent-vitest rationale — ./pr open gates on plain ./check, so
  # behind-\`test\` leaves the suite unenforced. Bare \`npx vitest run\` picks up
  # the default vitest config (happy-dom, no Chromium) matching CI's unit shards.
  run "admin-ui vitest" bash -c 'set -o pipefail; cd admin-ui && npx vitest run 2>&1 | tail -80'
fi

if touches agent/; then
  ( cd agent && npm_ci_if_lock_changed ) >/tmp/check_agent_npm_ci.log 2>&1 || {
    echo "[check] agent npm ci FAILED — see /tmp/check_agent_npm_ci.log"
    tail -30 /tmp/check_agent_npm_ci.log
    FAILED+=("agent npm ci")
  }
  run "agent tsc"        bash -c 'cd agent && ${BLUEPRINT_AGENT_TSC_CMD}'

  # Scope agent eslint to changed files. Errors in files the agent
  # edited are the agent's responsibility ("don't leave preexisting
  # issues" principle). --full forces repo-wide.
  if [ "$FULL_SCOPE" = "1" ]; then
    run "agent eslint (full, --full)" bash -c 'cd agent && npx eslint . 2>&1'
  else
    : > /tmp/check_agent_lint.txt
    printf '%s\\n' "$CHANGED" | grep -E '^agent/.*\\.(ts|tsx|js|jsx|cjs|mjs)$' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\\0' "\${f#agent/}" >> /tmp/check_agent_lint.txt
    done
    if [ -s /tmp/check_agent_lint.txt ]; then
      run "agent eslint (changed)" bash -c 'cd agent && xargs -0 -a /tmp/check_agent_lint.txt ${BLUEPRINT_AGENT_ESLINT_CMD}'
    else
      echo "[check] no agent JS/TS files changed, skipping eslint"
    fi
  fi

  # Run the agent vitest suite on EVERY ./check (not gated behind \`test\`):
  # \`./pr open\` gates on plain \`./check\`, so behind-\`test\` would leave the
  # suite unenforced by the PR gate — the exact silent-rot this guards against.
  # The suite is deterministic and ~40s. \`set -o pipefail\` makes the inner
  # bash propagate vitest's non-zero exit through the \`| tail\` (the outer
  # script's pipefail does not reach a fresh \`bash -c\`), so \`run\` records a
  # real failure in FAILED and ./check fails closed.
  run "agent vitest" bash -c 'set -o pipefail; cd agent && npx vitest run 2>&1 | tail -80'
fi

if touches backend/; then
  if command -v ruff >/dev/null 2>&1 || python3 -m ruff --version >/dev/null 2>&1; then
    # Scope ruff to changed *.py files only. Repo-wide ruff is a CI/main
    # concern; on PRs we don't want pre-existing baseline errors to block
    # unrelated fixes. CHANGED already includes both committed-since-base
    # and uncommitted/untracked. Filter to existing files so deletions
    # don't blow up ruff with ENOENT. Write null-delimited entries so xargs
    # -0 doesn't split on whitespace (path-with-space safe).
    : > /tmp/check_backend_py.txt
    printf '%s\\n' "$CHANGED" | grep -E '^backend/.*\\.py$' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\\0' "$f" >> /tmp/check_backend_py.txt
    done
    if [ -s /tmp/check_backend_py.txt ]; then
      run "backend ruff (changed)"     bash -c 'xargs -0 -a /tmp/check_backend_py.txt ${BLUEPRINT_BACKEND_RUFF_CHECK_CMD}'
      run "backend ruff fmt (changed)" bash -c 'xargs -0 -a /tmp/check_backend_py.txt ${BLUEPRINT_BACKEND_RUFF_FORMAT_CMD}'
    else
      echo "[check] no backend/*.py files changed, skipping ruff"
    fi
  else
    echo "[check] ruff not installed, skipping backend lint"
  fi
  if [ "$WITH_TESTS" = "1" ] && [ "${shouldRunBackendMergeGate(cfg) ? '1' : '0'}" = "1" ]; then
    # --standalone --no-postgres: agent VM is e2-standard-2 (8 GB) and
    # cannot fit Docker build + Postgres sidecar + full pytest suite +
    # agent + Claude subprocess in memory. The full suite OOMs (exit
    # 137) during the mandatory pre-PR gate (53ef061b, b150b4d6, b15b6813
    # all died this way 2026-05-05). Standalone runs the ~10-file
    # invariant set (security, pipeline, invariants, audit-finding-class,
    # admin-auth, metric-manifest + emitter-inventory contracts) which
    # catches file-size breaches, security regressions, and basic pipeline
    # and contract assertions — the rest is CI's job.
    # Baseline-recheck: if the standalone suite fails, re-run it on the
    # base branch to distinguish pre-existing failures (WARN, not blocking)
    # from regressions introduced by this PR (real failure, blocking).
    # Mirrors the viewport and eslint baseline-recheck pattern exactly.
    echo "[check] backend pytest..."
    BPYTEST_PR_LOG=/tmp/check_backend_pytest_pr.log
    if ( set -o pipefail; bash scripts/test-backend.sh --standalone --no-postgres 2>&1 | tail -200 ) > "$BPYTEST_PR_LOG" 2>&1; then
      tail -20 "$BPYTEST_PR_LOG"
      echo "[check] backend pytest OK"
    else
      echo "[check] backend pytest FAILED on PR — checking baseline on origin/$BASE..."
      BPYTEST_BASELINE_FAILED=0
      BPYTEST_BASELINE_REASON=""
      BPYTEST_BASE_REF="origin/$BASE"
      if ! git rev-parse --verify "$BPYTEST_BASE_REF" >/dev/null 2>&1; then
        BPYTEST_BASE_REF="$BASE"
        git rev-parse --verify "$BPYTEST_BASE_REF" >/dev/null 2>&1 || BPYTEST_BASELINE_REASON="base ref unavailable"
      fi
      if [ -z "$BPYTEST_BASELINE_REASON" ]; then
        BPYTEST_WT=/tmp/check_backend_pytest_baseline_wt
        # Stale worktree from a prior run can wedge \`worktree add\`.
        # \`worktree remove --force\` is the safe cleanup (no rm -rf).
        git worktree remove --force "$BPYTEST_WT" >/dev/null 2>&1 || true
        if ! git worktree add --detach "$BPYTEST_WT" "$BPYTEST_BASE_REF" >/tmp/check_backend_pytest_wt.log 2>&1; then
          BPYTEST_BASELINE_REASON="worktree add failed (see /tmp/check_backend_pytest_wt.log)"
        else
          BPYTEST_MAIN_LOG=/tmp/check_backend_pytest_main.log
          if ( set -o pipefail; cd "$BPYTEST_WT" && bash scripts/test-backend.sh --standalone --no-postgres 2>&1 | tail -200 ) > "$BPYTEST_MAIN_LOG" 2>&1; then
            BPYTEST_BASELINE_FAILED=0
          else
            BPYTEST_BASELINE_FAILED=1
          fi
          git worktree remove --force "$BPYTEST_WT" >/dev/null 2>&1 || true
        fi
      fi
      if [ "$BPYTEST_BASELINE_FAILED" = "1" ]; then
        echo "[check] backend pytest WARN — also broken on $BPYTEST_BASE_REF; not blocking PR"
        echo "[check] PR-side tail:"
        tail -30 "$BPYTEST_PR_LOG" || true
        echo "[check] Baseline-side tail:"
        tail -30 "$BPYTEST_MAIN_LOG" || true
      else
        echo "[check] backend pytest FAILED — passes on $BPYTEST_BASE_REF, fails on PR. Real regression."
        [ -n "$BPYTEST_BASELINE_REASON" ] && echo "[check] (baseline check skipped: $BPYTEST_BASELINE_REASON — treating PR failure as real)"
        tail -100 "$BPYTEST_PR_LOG" || true
        FAILED+=("backend pytest")
      fi
    fi
${buildImpactedTestBlock(cfg.taskId)}  fi
  # Fast migration-chain guard: no DB, no Docker, sub-second. Runs only
  # when at least one alembic/versions/ file was added or changed.
  if printf '%s\\n' "$CHANGED" | grep -q '^backend/alembic/versions/.*\\.py$'; then
    run "backend migration chain" bash -c 'python3 backend/app/migration_chain.py 2>&1'
  fi
fi
${buildCiFailureVerifyBlock(ciFailureSelectors, cfg.taskId)}
${cfg.mergeVerify ? buildMergeVerifyBlock(cfg.taskId) : ''}

# Fast module size-guardrail check: no DB/Docker/network, sub-second. Mirrors
# the line-count caps that backend/testing/test_invariants.py enforces, so a
# size breach surfaces HERE (before ./pr open) instead of only after a PR is
# open and a full CI/Docker round-trip is burned. Runs UNCONDITIONALLY (all
# sections, not diff-scoped): a rebase/merge can grow a file in a stack the
# diff never touched (e.g. landing a feature whose merge balloons models.py),
# and a size breach is a common, trivial CI failure we never want to burn a
# fix iteration on. The checker is sub-second and skips any section whose
# source tree is absent here, so always-on costs nothing.
# Invoked BY REFERENCE as a committed script (exactly like the migration-chain
# guard above) — no embedded Python heredoc (see CLAUDE.md). A breach makes
# the checker exit non-zero, which \`run\` records in FAILED so ./check fails.
# Remediate with: python3 backend/testing/check_size_limits.py --fix
run "size guardrail" bash -c "python3 backend/testing/check_size_limits.py 2>&1"
`;
  // Stack-block selection (ordered, most specific first):
  //  1. build_config with directories  → config-driven per-directory blocks.
  //  2. no build_config, Blueprint self-repo → the hardcoded admin-ui/agent/
  //     backend blocks above (self-repo behavior is byte-for-byte unchanged).
  //  3. no build_config, foreign repo  → a safe generic no-op fallback. We do
  //     NOT run Blueprint's admin-ui/agent/backend commands against an arbitrary
  //     target repo whose layout we don't know — that would fail spuriously or,
  //     worse, lint/build the wrong tree. The agent is told to configure
  //     build_config (or rely on CI) instead.
  let stackBlocks: string;
  const isSelfRepo = isBlueprintSelfRepo(cfg.owner, cfg.repo);
  if (cfg.buildConfig?.directories?.length) {
    stackBlocks = buildConfigDrivenStackBlocks(cfg.buildConfig, cfg.taskId);
  } else if (isSelfRepo) {
    stackBlocks = blueprintStackBlocks;
  } else {
    stackBlocks = genericNoConfigStackBlock(cfg.owner, cfg.repo);
  }
  // Success-path visual-acceptance capture+upload. Keyed to capability signal
  // rather than generated-string identity: can this stack actually capture
  // visual evidence? Blueprint self-repo YES (has viewport harness), config-driven
  // MAYBE (check if admin-ui is configured), foreign repo NO.
  // - If declared and can capture: emit capture block
  // - If declared but cannot capture: emit loud skip (observable unmet declaration)
  // - If not declared: emit nothing (byte-identical ./check)
  let visualCaptureBlock = '';
  if (cfg.visualAcceptance) {
    const canCapture = canCaptureVisual(isSelfRepo, cfg.buildConfig);
    if (canCapture) {
      visualCaptureBlock = buildVisualCaptureBlock(
        cfg.taskId,
        cfg.visualAcceptance.component,
        cfg.visualAcceptance.width,
      );
    } else {
      const reason = isSelfRepo
        ? 'admin-ui stack not available'
        : 'admin-ui not configured in build_config';
      visualCaptureBlock = buildVisualSkipBlock(reason);
    }
  }
  const footer = `
if [ \${#FAILED[@]} -eq 0 ]; then
  echo ""
  echo "[check] PASS — all checks green"
  exit 0
else
  echo ""
  echo "[check] FAIL — \${#FAILED[@]} check(s) failed: \${FAILED[*]}"
  exit 1
fi
`;
  const script = header + stackBlocks + visualCaptureBlock + footer;
  // For Blueprint self-repo, `check` is a committed static entrypoint that
  // delegates to `.check-task` when present. Write the per-task generated
  // script there (gitignored) so the tracked `check` file stays clean and
  // free of per-run values (taskId, baseBranch, ci_failure selectors).
  const outFile = isBlueprintSelfRepo(cfg.owner, cfg.repo)
    ? `${WORKSPACE}/.check-task`
    : `${WORKSPACE}/check`;
  fs.writeFileSync(outFile, script, { mode: 0o755 });
}

/** Generic fallback ./check body for a non-Blueprint repo that supplied no
 *  build_config. Emits no checks (the Blueprint-specific admin-ui/agent/backend
 *  blocks would be wrong for an unknown layout) — it warns the agent that no
 *  project check commands are configured and passes. owner/repo are validated
 *  shell-safe by validateCfgShellSafety before this runs. */
function genericNoConfigStackBlock(owner: string, repo: string): string {
  return `
# No build_config supplied and this is not the Blueprint self-repo
# (${owner}/${repo}). The hardcoded admin-ui/agent/backend checks are
# Blueprint-specific and would be wrong for an unknown repo layout, so ./check
# runs no project checks here. To get scoped lint/build/test gating, configure
# the project's build_config (directories + lint/build/test commands); CI
# remains the backstop in the meantime.
echo "[check] no build_config configured for ${owner}/${repo} — skipping project checks (CI is the backstop)"
`;
}

// Returns the inner bash block for a directory's lint step. Recognized linters
// are scoped to changed files (FULL_SCOPE=1 runs verbatim for the full tree).
// Unrecognized commands keep the current verbatim behavior.
function buildLintInner(dirPrefix: string, lintCmd: string): string {
  const kind = detectLinterKind(lintCmd);
  if (!kind) return `  run "${dirPrefix} lint" bash -c 'cd "${dirPrefix}" && ${lintCmd}'`;
  const slug = dirPrefix.replace(/\//g, '_');
  const tmpFile = `/tmp/check_${slug}_lint.txt`;
  const isPy = kind !== 'eslint';
  const extPat = isPy ? `\\.py` : `\\.(ts|tsx|js|jsx|cjs|mjs)`;
  const extDesc = isPy ? 'Python' : 'JS/TS';
  // kind !== null is already confirmed above; buildScopedLintOneliner returns null
  // only for unrecognized linters, so the fallback here is unreachable.
  const scopedExec = buildScopedLintOneliner(lintCmd, dirPrefix, '', 'check-tmpfile') ?? '';
  return `  if [ "$FULL_SCOPE" = "1" ]; then
    run "${dirPrefix} lint" bash -c 'cd "${dirPrefix}" && ${lintCmd}'
  else
    : > ${tmpFile}
    printf '%s\\n' "$CHANGED" | grep -E '^${dirPrefix}/.*${extPat}$' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\\0' "\${f#${dirPrefix}/}" >> ${tmpFile}
    done
    if [ -s ${tmpFile} ]; then
      run "${dirPrefix} lint (changed)" bash -c 'cd "${dirPrefix}" && ${scopedExec}'
    else
      echo "[check] no changed ${extDesc} files in ${dirPrefix}, skipping lint"
    fi
  fi`;
}

/** True for a pytest-style test command (bare ``pytest …`` or
 *  ``python[3] -m pytest …``). Same prefix-recognition approach as
 *  detectLinterKind: never rewrite a command we don't understand. */
export function isPytestCmd(testCmd: string): boolean {
  return /^(python3?\s+-m\s+)?pytest(\s|$)/.test(testCmd.trim());
}

/** Honest pytest gate for a config-driven directory (trapped tasks 22cb9d94 /
 *  6b14ec01, 2026-06-06): a bare pytest run silently degrades to an in-memory
 *  SQLite fallback when the test suite selects its database from the
 *  environment (Blueprint conftest: USE_TEST_POSTGRES=true + a postgresql
 *  DATABASE_URL) and that env isn't set — pre-existing Postgres-only tests
 *  then fail REGARDLESS of the diff and the mandatory ./pr gate blocks
 *  finished work on an environment artifact.
 *
 *  Generated decision ladder (all probes run at gate time, in bash):
 *    1. The conventional per-directory Docker harness (scripts/test-<dir>.sh,
 *       Blueprint: scripts/test-backend.sh) + Docker + enough memory →
 *       run the sanctioned harness instead of bare pytest.
 *    2. Harness + Docker but <4 GB available (the harness's own memory guard
 *       would refuse the full suite) → run the harness's standalone mode when
 *       it advertises one — a genuine partial run, never a silent degrade.
 *    3. Otherwise run the configured command verbatim WHEN it is faithful:
 *       the Postgres test env is present, or the suite does not select its
 *       database from the environment at all (no USE_TEST_POSTGRES in its
 *       conftest) — the gate stays fully mandatory on healthy environments.
 *    4. Only the unfaithful SQLite fallback remains → GATE-ENVIRONMENT:
 *       skip the run loudly, append a marker ./pr surfaces after the gate,
 *       and tell the agent to carry faithful evidence (or ./blocked) —
 *       never an indefinite block on a baseline-red environment. */
function buildPytestGateInner(dirPrefix: string, testCmd: string, taskId: string): string {
  // Only a top-level directory maps onto the scripts/test-<dir>.sh harness
  // convention; nested paths skip straight to the faithfulness checks.
  const harness = dirPrefix.includes('/') ? '' : `scripts/test-${dirPrefix}.sh`;
  const harnessProbe = harness
    ? `    if [ -f ${harness} ] && command -v docker >/dev/null 2>&1; then
      _AVAIL_MB=\${BLUEPRINT_MEM_AVAIL_MB:-$(awk '/^MemAvailable:/ {print int($2/1024); exit}' /proc/meminfo 2>/dev/null || echo 99999)}
      if [ -n "\${ALLOW_LOW_MEM:-}" ]; then
        # Explicit opt-in: force the full sanctioned harness regardless of memory.
        _GATE_MODE="harness"
      elif grep -qs -- '--standalone' ${harness}; then
        # Default on the agent VM: the local gate is a SMOKE CHECK; the full
        # Docker/Postgres suite is CI's job (and OOMs the 8 GB agent VM, exit
        # 137). When the harness offers its sanctioned standalone partial, always
        # prefer it — do NOT run the full suite just because memory happens to be
        # free at gate-start (the old "_AVAIL_MB >= 4096 -> full" rule fired the
        # full suite on exactly the VMs too small to survive it).
        _GATE_MODE="harness-standalone"
      elif [ "\${_AVAIL_MB:-99999}" -ge 4096 ]; then
        # Harness has no standalone mode — only then run the full harness, and
        # only with memory headroom for it.
        _GATE_MODE="harness"
      fi
    fi
`
    : '';
  const harnessCases = harness
    ? `      harness)
        run "${dirPrefix} test (sanctioned harness)" bash -c 'set -o pipefail; bash ${harness} 2>&1 | tail -200' ;;
      harness-standalone)
        echo "[check] note: local gate is a smoke check — full Docker/Postgres suite runs in CI; running the harness standalone mode"
        run "${dirPrefix} test (harness standalone)" bash -c 'set -o pipefail; bash ${harness} --standalone --no-postgres 2>&1 | tail -200' ;;
`
    : '';
  return `    # Honest pytest gate: prefer the sanctioned Docker harness; if only an
    # unfaithful SQLite fallback is available, emit GATE-ENVIRONMENT instead
    # of blocking on pre-existing environment-only failures. CI remains the
    # authoritative Postgres run either way.
    _GATE_MODE="bare"
    _PG_ENV_OK=0
    case "\${DATABASE_URL:-}" in
      postgresql*) [ "$(printf '%s' "\${USE_TEST_POSTGRES:-}" | tr '[:upper:]' '[:lower:]')" = "true" ] && _PG_ENV_OK=1 ;;
    esac
${harnessProbe}    if [ "$_GATE_MODE" = "bare" ] && [ "$_PG_ENV_OK" = "0" ] && grep -qs USE_TEST_POSTGRES "${dirPrefix}/conftest.py" "${dirPrefix}"/*/conftest.py; then
      # The suite selects Postgres-vs-SQLite from env, the Postgres test env
      # is absent, and the sanctioned harness cannot run here — a bare run
      # would be the unfaithful SQLite fallback.
      _GATE_MODE="gate-env"
    fi
    case "$_GATE_MODE" in
${harnessCases}      bare)
        _PYTEST_RC=0
        echo "[check] ${dirPrefix} test..."
        ( cd "${dirPrefix}" && ${testCmd} ) || _PYTEST_RC=$?
        if [ "$_PYTEST_RC" = "0" ]; then
          echo "[check] ${dirPrefix} test OK"
        elif [ "$_PYTEST_RC" = "5" ]; then
          echo "[check] ${dirPrefix} test: no tests collected (pytest exit 5) — non-blocking on this environment"
          printf '%s %s\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${dirPrefix}: pytest exit 5 (no tests collected) — non-blocking" >> /tmp/check_gate_env_${taskId}.txt
        elif [ "$_PYTEST_RC" = "2" ]; then
          echo "[check] ${dirPrefix} test: collection error (pytest exit 2, possibly missing deps) — non-blocking"
          printf '%s %s\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${dirPrefix}: pytest exit 2 (collection error) — non-blocking" >> /tmp/check_gate_env_${taskId}.txt
        else
          echo "[check] ${dirPrefix} test FAILED (exit $_PYTEST_RC)"
          FAILED+=("${dirPrefix} test")
        fi ;;
      gate-env)
        echo "[check] GATE-ENVIRONMENT: ${dirPrefix} test gate cannot run faithfully on this VM."
        echo "[check]   A bare pytest here would select the in-memory SQLite fallback (no Postgres test env, no Docker for the sanctioned harness), where pre-existing Postgres-only tests fail regardless of your diff."
        echo "[check]   Skipping the unfaithful run instead of blocking on an environment artifact. This is NOT a verdict on your change:"
        echo "[check]   - Validate the diff where the suite CAN faithfully run (the project's Docker/Postgres harness, or targeted single-file runs) and record that evidence in the PR body."
        echo "[check]   - CI runs the authoritative Postgres suite on the PR after ./pr open."
        echo "[check]   - If you have NO faithful test evidence for a test-relevant change, call ./blocked \\"<reason>\\" --needs-capability \\"the test environment this suite requires\\" instead of shipping blind."
        printf '%s %s\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${dirPrefix}: pytest gate skipped — no Docker for the sanctioned harness and no Postgres test env; a bare run would be an unfaithful SQLite fallback" >> /tmp/check_gate_env_${taskId}.txt ;;
    esac`;
}

/** Generate config-driven per-directory ./check blocks from a project's
 *  build_config. Mirrors the per-directory loop in runProjectLintChecks
 *  (index.ts): for each configured directory whose files changed, run its
 *  install/lint/build commands; test_cmd runs only under `./check test`
 *  (WITH_TESTS). env_vars are exported so all commands inherit them.
 *
 *  All interpolated values (paths, commands, env names/values) are validated
 *  by validateCfgShellSafety against a conservative allowlist that rejects
 *  shell metacharacters (including single quotes), so emitting them as
 *  single-quoted bash literals here is injection-safe — see CLAUDE.md on
 *  generating one language from another. */
function buildConfigDrivenStackBlocks(buildConfig: BuildConfig, taskId: string): string {
  const dirs = buildConfig.directories ?? [];
  const envLines = Object.entries(buildConfig.env_vars ?? {}).map(
    ([k, v]) => `export ${k}='${v}'`,
  );
  const sections: string[] = [];
  // A command is interpolated into a single-quoted bash literal below, so it is
  // only safe to emit when it clears SHELL_CMD_RE. resolveBuildConfig now RETAINS
  // commands with quotes/operators verbatim (so the backend's tests_gated
  // decision and the no-CI gate agree on whether the project has tests); those
  // commands run safely via a `bash -c` argv in the no-CI gate, but they cannot
  // be single-quote-interpolated here. Skip the unsafe ones — `./check` is a
  // best-effort local convenience; the authoritative run is the no-CI gate.
  const safeCmd = (cmd: string | undefined): cmd is string =>
    typeof cmd === 'string' && cmd !== '' && SHELL_CMD_RE.test(cmd);
  for (const dir of dirs) {
    const dirPrefix = dir.path.replace(/\/$/, '');
    const inner: string[] = [];
    if (safeCmd(dir.install_cmd)) inner.push(`  run "${dirPrefix} install" bash -c 'cd "${dirPrefix}" && ${dir.install_cmd}'`);
    if (safeCmd(dir.lint_cmd)) inner.push(buildLintInner(dirPrefix, dir.lint_cmd));
    if (safeCmd(dir.build_cmd)) inner.push(`  run "${dirPrefix} build" bash -c 'cd "${dirPrefix}" && ${dir.build_cmd}'`);
    if (safeCmd(dir.test_cmd)) {
      inner.push(`  if [ "$WITH_TESTS" = "1" ]; then`);
      if (isPytestCmd(dir.test_cmd)) {
        // Bare pytest can silently degrade to an unfaithful SQLite fallback —
        // route through the honest gate (harness preference + GATE-ENVIRONMENT).
        inner.push(buildPytestGateInner(dirPrefix, dir.test_cmd, taskId));
      } else {
        inner.push(`    run "${dirPrefix} test" bash -c 'cd "${dirPrefix}" && ${dir.test_cmd}'`);
      }
      inner.push(`  fi`);
    }
    if (inner.length === 0) continue;
    sections.push(`if touches "${dirPrefix}/"; then\n${inner.join('\n')}\nfi`);
  }
  const comment = `
# Config-driven checks: project supplied a build_config, so run its
# per-directory install/lint/build commands (tests under \`./check test\`)
# for changed directories. Mirrors runProjectLintChecks in index.ts.`;
  const envBlock = envLines.length ? `\n${envLines.join('\n')}` : '';
  const body = sections.length ? `\n${sections.join('\n\n')}` : '';
  return `${comment}${envBlock}${body}\n`;
}
