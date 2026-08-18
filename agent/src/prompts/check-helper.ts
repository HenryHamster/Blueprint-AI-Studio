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
  projectVerificationTargets,
  projectVerificationDirectories,
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

function readShellFragment(fragmentPath: string): string {
  // Git may materialize checked-in shell fragments with CRLF on Windows.
  // Normalize at the generator boundary so concatenated fragments remain one
  // valid, byte-stable Bash program on every authoring host.
  return fs.readFileSync(fragmentPath, 'utf8').replace(/\r\n?/g, '\n');
}

const _BACKEND_VERIFICATION_PATH = path.join(_CHECK_HELPER_DIR, 'check_backend_verification.sh');
function loadBackendVerificationScript(): string {
  try { return readShellFragment(_BACKEND_VERIFICATION_PATH); } catch {
    return '    echo "[check] required backend verification fragment missing — failing closed"\n    FAILED+=("required backend verification fragment")\n';
  }
}

const _CI_FAILURE_VERIFY_PATH = path.join(_CHECK_HELPER_DIR, 'check_ci_failure_verify.sh');
function loadCiFailureVerifyScript(): string {
  try { return readShellFragment(_CI_FAILURE_VERIFY_PATH); } catch { return ''; }
}

const _MERGE_VERIFY_PATH = path.join(_CHECK_HELPER_DIR, 'check_merge_verify.sh');
function loadMergeVerifyScript(): string {
  try { return readShellFragment(_MERGE_VERIFY_PATH); } catch { return ''; }
}

const _VIEWPORT_UPLOAD_PATH = path.join(_CHECK_HELPER_DIR, 'check_viewport_upload.sh');
function loadViewportUploadScript(): string {
  try { return readShellFragment(_VIEWPORT_UPLOAD_PATH); } catch { return ''; }
}

const _VISUAL_CAPTURE_PATH = path.join(_CHECK_HELPER_DIR, 'check_visual_capture.sh');
function loadVisualCaptureScript(): string {
  try { return readShellFragment(_VISUAL_CAPTURE_PATH); } catch { return ''; }
}

const _SIZE_GUARDRAIL_PATH = path.join(_CHECK_HELPER_DIR, 'check_size_guardrail.sh');
// Unlike the sibling loaders above (which gate OPTIONAL blocks), the size
// guardrail is an ALWAYS-ON gate: a missing/unreadable fragment must fail
// ./check closed, not silently emit a script with no size gate at all.
function loadSizeGuardrailScript(): string {
  try { return readShellFragment(_SIZE_GUARDRAIL_PATH); } catch {
    return '\necho "[check] size guardrail fragment missing from agent build — failing closed"\nFAILED+=("size guardrail")\n';
  }
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

function buildRequiredBackendVerificationBlock(cfg: CodeChangeHelperConfig): string {
  const blocks: string[] = [];
  const targets = projectVerificationTargets(cfg);
  // The self-repo's legacy authoring path predates capability/config-driven
  // verification. It used to receive the raw impacted lane; keep its coverage
  // by routing it through the same attributed fragment instead. Explicit
  // capability declarations remain authoritative and are never inferred.
  if (
    targets.length === 0
    && isBlueprintSelfRepo(cfg.owner, cfg.repo)
    && !cfg.taskCapability
    && !cfg.buildConfig
  ) {
    targets.push({ directory: 'backend', harness: 'scripts/test-backend.sh' });
  }
  for (const { directory: dir, harness } of targets) {
    const dirRe = dir.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const pythonPathRe = dir === '.' ? '.*\\.py$' : `^${dirRe}/.*\\.py$`;
    const testPathRe = dir === '.'
      ? '^(testing|tests)/test_.*\\.py$'
      : `^${dirRe}/(testing|tests)/test_.*\\.py$`;
    const selectorPrefixRe = dir === '.' ? '' : `${dirRe}/`;
    const fragment = loadBackendVerificationScript()
      .replace(/@TASK_ID@/g, cfg.taskId)
      .replace(/@DIR_RE@/g, dirRe)
      .replace(/@PYTHON_PATH_RE@/g, pythonPathRe)
      .replace(/@TEST_PATH_RE@/g, testPathRe)
      .replace(/@SELECTOR_PREFIX_RE@/g, selectorPrefixRe)
      .replace(/@DIR@/g, dir)
      .replace(/@HARNESS@/g, harness);
    const condition = dir === '.'
      ? `[ -n "$(printf '%s\\n' "$CHANGED" | while IFS= read -r f; do case "$f" in *.py) [ -f "$f" ] && printf x && break ;; esac; done)" ]`
      : `[ -n "$(printf '%s\\n' "$CHANGED" | while IFS= read -r f; do case "$f" in ${dir}/*.py) [ -f "$f" ] && printf x && break ;; esac; done)" ]`;
    blocks.push(`if ${condition}; then\n${fragment}fi`);
  }
  return blocks.length > 0 ? `\n${blocks.join('\n\n')}\n` : '';
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
# Receipt paths are derived once, before any early exit can bypass cleanup.
# TASK_ID is the runtime identity when present; the generated task ID remains
# the safe fallback for local/manual invocation.
_VERIFY_TASK_ID="\${TASK_ID:-${cfg.taskId}}"
_VERIFY_RECEIPT="/tmp/project_verification_receipt_\${_VERIFY_TASK_ID}.json"
_VERIFY_HARNESS_RECEIPT="/tmp/backend_verification_harness_\${_VERIFY_TASK_ID}.json"
_VERIFY_CAPABILITY_RECEIPT="/tmp/execution_capability_receipt_\${_VERIFY_TASK_ID}"
if ! rm -f "$_VERIFY_RECEIPT" "$_VERIFY_HARNESS_RECEIPT" "$_VERIFY_CAPABILITY_RECEIPT"; then
  echo "[check] unable to remove prior verification receipt(s) — refusing to run" >&2
  exit 1
fi
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

# Determine changed paths: committed since base + uncommitted. Fail closed
# when the base diff cannot be computed — a silently-empty CHANGED used to
# fall through to "PASS — all checks green" without running anything, making
# "could not compute the diff" indistinguishable from "nothing changed".
BASE="${cfg.baseBranch}"
BASE_DIFF_OK=0
COMMITTED=""
# Try the candidate base refs in order; COMMITTED is set by the first ref whose
# diff computes (possibly to the empty string = genuine no-op). Returns 0 when
# the committed diff is known, 1 when it cannot be computed from any ref.
_try_base_diff() {
  local ref committed
  for ref in "origin/$BASE" "$BASE"; do
    if ! git rev-parse --verify "$ref" >/dev/null 2>&1; then
      continue
    fi
    if ! committed=$(git diff --name-only "$ref...HEAD" 2>/dev/null); then
      continue
    fi
    COMMITTED="$committed"
    return 0
  done
  return 1
}

if ! _try_base_diff; then
  # Base diff uncomputable. A truncated shallow clone is the common cause, so
  # try to repair it before failing closed — but never swallow the failure and
  # report a green run. The final fetch uses the EXPLICIT refspec so
  # refs/remotes/origin/$BASE actually exists in a single-branch shallow clone
  # (a plain \`git fetch origin $BASE\` would only update FETCH_HEAD there).
  _repaired=0
  if git remote get-url origin >/dev/null 2>&1; then
    GIT_TERMINAL_PROMPT=0 git fetch --deepen=200 origin "$BASE" >/dev/null 2>&1 || true
    GIT_TERMINAL_PROMPT=0 git fetch --unshallow origin >/dev/null 2>&1 || true
    GIT_TERMINAL_PROMPT=0 git fetch origin "$BASE":refs/remotes/origin/$BASE >/dev/null 2>&1 || true
    if _try_base_diff; then
      _repaired=1
    fi
  fi
  if [ "$_repaired" -eq 1 ]; then
    BASE_DIFF_OK=1
  fi
else
  BASE_DIFF_OK=1
fi
UNCOMMITTED=$(git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null)
CHANGED=$(printf '%s\\n%s\\n' "$COMMITTED" "$UNCOMMITTED" | sort -u | grep -v '^$' || true)

touches() { printf '%s\\n' "$CHANGED" | grep -q "^$1"; }

admin_ui_runtime_touched() {
  printf '%s\\n' "$CHANGED" | grep -Eq '^(admin-ui/src/|admin-ui/(package\\.json|package-lock\\.json|vite\\.config\\.ts|vitest(\\.viewport)?\\.config\\.ts|eslint\\.config\\.js|eslint-rules/))'
}

FAILED=()

if [ "$BASE_DIFF_OK" -ne 1 ]; then
  echo "[check] ERROR: base diff source unavailable — cannot scope checks. Refusing to report PASS with nothing run; fetch the base branch and re-run (e.g. 'git fetch origin $BASE')." >&2
  FAILED+=("base diff unavailable")
fi

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
    # Bootstrap Playwright Chromium for the optional VM-side viewport check.
    # The baked agent VM already includes the system libs (libatk, libnss,
    # etc.) — and may already have the browser cached, so we cache-check
    # first and fall through to the bare install, with --with-deps as a
    # last-resort fallback for fresh local machines.
    # See admin-ui/CONTRIBUTING.md for developer-machine setup.
    VIEWPORT_BOOTSTRAP_LOG=/tmp/check_viewport_bootstrap.log
    if ( bash -c '
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
    ' ) > "$VIEWPORT_BOOTSTRAP_LOG" 2>&1; then
      echo "[check] admin-ui playwright chromium OK"
    else
      echo "[check] VIEWPORT_CAPTURE_SKIPPED: Chromium substrate unavailable on this VM; CI frontend-viewport enforces browser verification"
      tail -30 "$VIEWPORT_BOOTSTRAP_LOG" || true
      VIEWPORT_RUN=0
    fi
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

  # Run the admin-ui unit suite on EVERY ./check. The guard compares its JUnit
  # identities with an isolated base checkout, so only confirmed inherited
  # failures warn-pass; unavailable attribution remains fail-closed.
  run "admin-ui vitest (attributed)" python3 scripts/vitest_verification_guard.py --suite admin-ui --base "$BASE" --repo-root "$WORKSPACE"
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

  # This stays always-on because ./pr open gates on plain ./check. The shared
  # guard has the same fail-closed, base-attributed contract as admin-ui.
  run "agent vitest (attributed)" python3 scripts/vitest_verification_guard.py --suite agent --base "$BASE" --repo-root "$WORKSPACE"
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
    # The shared static guard runs the same corpus on the task and base trees,
    # then attributes failures by structured route/test/failure-kind identity.
    # It must remain the single baseline decision point: a non-zero base run
    # does not waive unrelated failures introduced by this task.
    run "backend pytest (attributed)" python3 scripts/static_release_guard.py \
      --corpus-only --base "$BASE" --repo-root "$WORKSPACE"
  fi
  # Fast migration-chain guard: no DB, no Docker, sub-second. Runs only
  # when at least one alembic/versions/ file was added or changed.
  if printf '%s\\n' "$CHANGED" | grep -q '^backend/alembic/versions/.*\\.py$'; then
    run "backend migration chain" bash -c 'python3 backend/app/migration_chain.py 2>&1'
  fi
fi
${buildCiFailureVerifyBlock(ciFailureSelectors, cfg.taskId)}
${cfg.mergeVerify ? buildMergeVerifyBlock(cfg.taskId) : ''}
${loadSizeGuardrailScript()}`;
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
    stackBlocks = buildConfigDrivenStackBlocks(
      cfg.buildConfig,
      new Set(projectVerificationDirectories(cfg)),
    );
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
  const script = header + stackBlocks + buildRequiredBackendVerificationBlock(cfg) + visualCaptureBlock + footer;
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
function buildConfigDrivenStackBlocks(
  buildConfig: BuildConfig,
  retainedVerificationDirectories: ReadonlySet<string>,
): string {
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
    const normalizedDirPrefix = dirPrefix.replace(/^(?:\.\/)+/, '').replace(/\/+$/, '') || '.';
    const usesRetainedVerification = retainedVerificationDirectories.has(normalizedDirPrefix);
    if (safeCmd(dir.test_cmd) && !usesRetainedVerification) {
      inner.push(`  if [ "$WITH_TESTS" = "1" ]; then`);
      inner.push(`    run "${dirPrefix} test" bash -c 'cd "${dirPrefix}" && ${dir.test_cmd}'`);
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
