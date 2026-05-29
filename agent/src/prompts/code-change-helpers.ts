import fs from 'fs';
import { WORKSPACE } from '../index.js';
import type { BuildConfig } from '../index.js';
import { SHELL_CMD_RE } from '../shell-safety.js';
import { writeDoneHelper } from './done-helper.js';
import { writePrHelper } from './pr-helper.js';

// ── Git exclude for agent helper scripts ────────────────────────────────────
// Agent helper scripts (playbook, messages, note, strategic, design-doc) are
// written to WORKSPACE for Claude Code to use. Exclude them from git so they
// don't get committed into PRs via `git add -A`.
export const AGENT_HELPER_NAMES = ['playbook', 'messages', 'note', 'strategic', 'design-doc', 'check', 'pr', 'done', 'fail', 'fetch', 'blocked'];

export function excludeAgentHelpers(): void {
  const excludePath = `${WORKSPACE}/.git/info/exclude`;
  try {
    const existing = fs.existsSync(excludePath) ? fs.readFileSync(excludePath, 'utf-8') : '';
    const lines = existing.split('\n');
    const missing = AGENT_HELPER_NAMES.filter(name => !lines.includes(`/${name}`));
    if (missing.length > 0) {
      const append = missing.map(name => `/${name}`).join('\n') + '\n';
      fs.appendFileSync(excludePath, append);
    }
  } catch { /* .git/info may not exist in shallow clones */ }
}

// ── Code-change task helpers (./check, ./pr, ./done, ./fail) ────────────────
//
// These four helpers replace the wrapper-driven lint/test/push/PR pipeline
// for tasks that produce code changes. Claude owns its own loop:
//
//   edit → ./check → see errors → edit → ./check → ... (one Claude session,
//   full task context, intelligent pivots) → ./pr open --title --body
//   → ./done (mark COMPLETED with PR URL)  OR  ./fail "reason"
//
// The wrapper just runs Claude, watches for ./done|./fail marker files,
// and posts status accordingly. No more wrapper-driven lintFixLoop /
// testFixLoop / pushAndCreatePR for these task types.

export interface CodeChangeHelperConfig {
  taskId: string;
  /** Working branch name, e.g. ``blueprint/task-abc12345``. */
  workBranch: string;
  baseBranch: string;
  owner: string;
  repo: string;
  /** Existing PR number (review-fix path). When set, ./pr open just pushes
   *  to the existing PR instead of creating a new one. */
  existingPrNumber?: number;
  /** When true, ./check test runs the Docker backend test suite for any
   *  task whose changes touch ``backend/``. Set when baseBranch === promotionTarget
   *  (or === 'main' when no promotionTarget is known). */
  testsOnMainBranch: boolean;
  /** Per-project promotion target branch (e.g. 'automain', 'production', 'release/v2').
   *  When set, the test gate fires when baseBranch === promotionTarget, overriding
   *  the testsOnMainBranch boolean. Handlers that read promotion_target from the
   *  task payload should pass it here. */
  promotionTarget?: string;
  /** SR categories whose miss-rate is regressing for this task type.
   *  When set, ./done validates that each has a non-trivial `reasoning` field
   *  in check_results before posting COMPLETED. */
  regressingCategories?: string[];
  /** Resolved project build configuration. When it carries per-directory
   *  commands, the generated ./check runs those instead of the hardcoded
   *  Blueprint admin-ui/agent/backend blocks (the self-repo fallback used
   *  only when no build_config is present). */
  buildConfig?: BuildConfig;
}

/** Write ./check, ./pr, ./done, ./fail, ./blocked to WORKSPACE. Idempotent. */
export function writeCodeChangeHelpers(cfg: CodeChangeHelperConfig): void {
  writeCheckHelper(cfg);
  writePrHelper(cfg);
  writeDoneHelper(cfg);
  writeFailHelper(cfg);
  writeBlockedHelper(cfg);
  excludeAgentHelpers();
}

function writeCheckHelper(cfg: CodeChangeHelperConfig): void {
  validateCfgShellSafety(cfg);
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
`;
  const blueprintStackBlocks = `
if admin_ui_runtime_touched; then
  ( cd admin-ui && [ -d node_modules ] || npm ci ) >/tmp/check_npm_ci.log 2>&1 || {
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
      run "admin-ui eslint (changed)" bash -c 'cd admin-ui && xargs -0 -a /tmp/check_admin_ui_lint.txt npx eslint 2>&1'
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
  #    pattern at .github/workflows/ci.yml line 111-143). PR-only fail →
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
      fi
    fi
  fi

  run "admin-ui build"   bash -c 'cd admin-ui && VITE_API_URL=http://localhost:8080 VITE_FIREBASE_API_KEY=ci VITE_FIREBASE_AUTH_DOMAIN=ci.firebaseapp.com VITE_FIREBASE_PROJECT_ID=ci npm run build 2>&1'
fi

if touches agent/; then
  ( cd agent && [ -d node_modules ] || npm ci ) >/tmp/check_agent_npm_ci.log 2>&1 || {
    echo "[check] agent npm ci FAILED — see /tmp/check_agent_npm_ci.log"
    tail -30 /tmp/check_agent_npm_ci.log
    FAILED+=("agent npm ci")
  }
  run "agent tsc"        bash -c 'cd agent && npx tsc --noEmit 2>&1'

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
      run "agent eslint (changed)" bash -c 'cd agent && xargs -0 -a /tmp/check_agent_lint.txt npx eslint 2>&1'
    else
      echo "[check] no agent JS/TS files changed, skipping eslint"
    fi
  fi
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
      run "backend ruff (changed)"     bash -c 'xargs -0 -a /tmp/check_backend_py.txt python3 -m ruff check --select E,F,W --ignore E501 2>&1'
      run "backend ruff fmt (changed)" bash -c 'xargs -0 -a /tmp/check_backend_py.txt python3 -m ruff format --check 2>&1'
    else
      echo "[check] no backend/*.py files changed, skipping ruff"
    fi
  else
    echo "[check] ruff not installed, skipping backend lint"
  fi
  if [ "$WITH_TESTS" = "1" ] && [ "${(cfg.promotionTarget !== undefined ? cfg.baseBranch === cfg.promotionTarget : cfg.testsOnMainBranch) ? '1' : '0'}" = "1" ]; then
    # --standalone --no-postgres: agent VM is e2-standard-2 (8 GB) and
    # cannot fit Docker build + Postgres sidecar + full pytest suite +
    # agent + Claude subprocess in memory. The full suite OOMs (exit
    # 137) during the mandatory pre-PR gate (53ef061b, b150b4d6, b15b6813
    # all died this way 2026-05-05). Standalone runs the 3 invariant
    # files (security, pipeline, invariants) which catch file-size
    # breaches, security regressions, and basic pipeline assertions —
    # the rest is CI's job.
    run "backend pytest" bash -c 'bash scripts/test-backend.sh --standalone --no-postgres 2>&1 | tail -200'
  fi
  # Fast migration-chain guard: no DB, no Docker, sub-second. Runs only
  # when at least one alembic/versions/ file was added or changed.
  if printf '%s\\n' "$CHANGED" | grep -q '^backend/alembic/versions/.*\\.py$'; then
    run "backend migration chain" bash -c 'python3 backend/app/migration_chain.py 2>&1'
  fi
fi
`;
  // When the project supplies a build_config with directories, generate
  // per-directory check blocks from it. Otherwise fall back to the hardcoded
  // Blueprint blocks above (self-repo behavior is byte-for-byte unchanged).
  const stackBlocks = cfg.buildConfig?.directories?.length
    ? buildConfigDrivenStackBlocks(cfg.buildConfig)
    : blueprintStackBlocks;
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
  const script = header + stackBlocks + footer;
  fs.writeFileSync(`${WORKSPACE}/check`, script, { mode: 0o755 });
}

// Detects the canonical linter kind from a lint_cmd string.
// Returns null for unrecognized commands — those run verbatim (never silently
// rewrite something we don't understand).
export function detectLinterKind(lintCmd: string): 'ruff' | 'eslint' | 'mypy' | 'flake8' | 'pyright' | null {
  const base = lintCmd.replace(/^(python3?\s+-m\s+|npx\s+|pnpm\s+exec\s+)/, '');
  if (/^ruff\b/.test(base)) return 'ruff';
  if (/^eslint\b/.test(base)) return 'eslint';
  if (/^mypy\b/.test(base)) return 'mypy';
  if (/^flake8\b/.test(base)) return 'flake8';
  if (/^pyright\b/.test(base)) return 'pyright';
  return null;
}

/**
 * Builds the scoped lint bash one-liner for a recognized linter.
 *
 * source='check-tmpfile': returns the xargs invocation that reads null-delimited
 *   paths from /tmp/check_<slug>_lint.txt (populated upstream by grep over
 *   $CHANGED). Used inside the ./check generator (buildLintInner).
 *
 * source='git-diff': returns a git diff pipeline that filters changed files in
 *   dirPath and strips the directory prefix before passing to the linter.
 *   Used inside review prompt instruction strings (buildTypeCheckInstructions).
 *
 * Canonical behaviors shared across both call sites:
 * - Strips trailing '.' / './' token (replaced by the scoped file list)
 * - Recognizes python3 -m / npx / pnpm exec prefixes
 * - Hardcodes ruff to --select E,F,W --ignore E501 with python3 -m
 * - Returns null for unrecognized linters — callers must fall back to verbatim
 */
export function buildScopedLintOneliner(
  lintCmd: string,
  dirPath: string,
  baseBranch: string,
  source: 'check-tmpfile' | 'git-diff',
): string | null {
  const kind = detectLinterKind(lintCmd);
  if (!kind) return null;

  const toks = lintCmd.trim().split(/\s+/);
  while (toks.length > 1 && (toks[toks.length - 1] === '.' || toks[toks.length - 1] === './')) toks.pop();
  const execCmd = kind === 'ruff'
    ? 'python3 -m ruff check --select E,F,W --ignore E501'
    : toks.join(' ');

  const dir = dirPath.replace(/\/$/, '');
  const isPy = kind !== 'eslint';
  const extPat = isPy ? '\\.py' : '\\.(ts|tsx|js|jsx|cjs|mjs)';

  if (source === 'check-tmpfile') {
    const slug = dir.replace(/\//g, '_');
    return `xargs -0 -a /tmp/check_${slug}_lint.txt ${execCmd} 2>&1`;
  }
  return `git diff origin/${baseBranch}...HEAD --name-only | grep -E '${extPat}$' | sed 's|${dir}/||' | xargs -r ${execCmd}`;
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
function buildConfigDrivenStackBlocks(buildConfig: BuildConfig): string {
  const dirs = buildConfig.directories ?? [];
  const envLines = Object.entries(buildConfig.env_vars ?? {}).map(
    ([k, v]) => `export ${k}='${v}'`,
  );
  const sections: string[] = [];
  for (const dir of dirs) {
    const dirPrefix = dir.path.replace(/\/$/, '');
    const inner: string[] = [];
    if (dir.install_cmd) inner.push(`  run "${dirPrefix} install" bash -c 'cd "${dirPrefix}" && ${dir.install_cmd}'`);
    if (dir.lint_cmd) inner.push(buildLintInner(dirPrefix, dir.lint_cmd));
    if (dir.build_cmd) inner.push(`  run "${dirPrefix} build" bash -c 'cd "${dirPrefix}" && ${dir.build_cmd}'`);
    if (dir.test_cmd) {
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

const BRANCH_RE = /^[A-Za-z0-9._/-]+$/;
const OWNER_REPO_RE = /^[A-Za-z0-9._-]+$/;
const TASK_ID_RE = /^[A-Za-z0-9-]+$/;
const ENV_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

export function validateCfgShellSafety(cfg: CodeChangeHelperConfig): void {
  if (!TASK_ID_RE.test(cfg.taskId)) {
    throw new Error(`taskId contains unsafe characters for shell interpolation: ${cfg.taskId}`);
  }
  if (!BRANCH_RE.test(cfg.workBranch)) {
    throw new Error(`workBranch contains unsafe characters for shell interpolation: ${cfg.workBranch}`);
  }
  if (!BRANCH_RE.test(cfg.baseBranch)) {
    throw new Error(`baseBranch contains unsafe characters for shell interpolation: ${cfg.baseBranch}`);
  }
  if (!OWNER_REPO_RE.test(cfg.owner)) {
    throw new Error(`owner contains unsafe characters for shell interpolation: ${cfg.owner}`);
  }
  if (!OWNER_REPO_RE.test(cfg.repo)) {
    throw new Error(`repo contains unsafe characters for shell interpolation: ${cfg.repo}`);
  }
  validateBuildConfigShellSafety(cfg.buildConfig);
}

/** Validate every build_config value that gets interpolated into the generated
 *  ./check bash. Throws on any shell metacharacter so the config-driven blocks
 *  stay injection-safe (see CLAUDE.md on generating one language from another). */
function validateBuildConfigShellSafety(buildConfig: BuildConfig | undefined): void {
  if (!buildConfig) return;
  for (const dir of buildConfig.directories ?? []) {
    if (!SHELL_CMD_RE.test(dir.path)) {
      throw new Error(`build_config directory path contains unsafe characters for shell interpolation: ${dir.path}`);
    }
    const cmds: Array<[string, string | undefined]> = [
      ['install_cmd', dir.install_cmd],
      ['lint_cmd', dir.lint_cmd],
      ['build_cmd', dir.build_cmd],
      ['test_cmd', dir.test_cmd],
    ];
    for (const [field, val] of cmds) {
      if (val !== undefined && val !== '' && !SHELL_CMD_RE.test(val)) {
        throw new Error(`build_config ${field} for ${dir.path} contains unsafe characters for shell interpolation: ${val}`);
      }
    }
  }
  for (const [k, v] of Object.entries(buildConfig.env_vars ?? {})) {
    if (!ENV_NAME_RE.test(k)) {
      throw new Error(`build_config env_var name contains unsafe characters for shell interpolation: ${k}`);
    }
    if (!SHELL_CMD_RE.test(v)) {
      throw new Error(`build_config env_var value for ${k} contains unsafe characters for shell interpolation: ${v}`);
    }
  }
}
function writeFailHelper(cfg: CodeChangeHelperConfig): void {
  validateCfgShellSafety(cfg);
  // Explicit failure signal — Claude calls this when it determines the task
  // can't be completed (e.g. scope is wrong, blocker found, conflicting
  // requirements). Posts FAILED with the reason. Wrapper sees the marker
  // and exits cleanly without trying its own fallback push.
  const script = `#!/bin/bash
# ./fail "reason" — mark task FAILED with an operator-readable reason.
# Use when the task can't be completed cleanly: scope is wrong, blocker
# found, conflicting requirements, etc. Don't use for transient lint
# failures — keep iterating with ./check instead.

set -uo pipefail
REASON="\${1:?Usage: ./fail \\"<reason>\\"}"

API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${cfg.taskId}"

if [ -f /tmp/fail_${cfg.taskId} ]; then
  echo "[fail] already marked — no-op"
  exit 0
fi

PAYLOAD=$(jq -n --arg tid "$TASK" --arg r "$REASON" \\
  '{task_id: $tid, status: "FAILED", artifact_type: "ERROR_LOG", logs: $r, failure_category: "agent_self_failed"}')
POSTED=0
for attempt in 1 2 3 4 5; do
  HTTP=$(printf '%s' "$PAYLOAD" | curl -sS -o /tmp/fail_resp_${cfg.taskId} -w '%{http_code}' --max-time 30 \\
    -X POST "$API/api/agent/status" \\
    -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-) || HTTP="000"
  if [ "$HTTP" = "200" ] || [ "$HTTP" = "202" ]; then
    POSTED=1; break
  fi
  if [ "$HTTP" = "429" ] || [ "\${HTTP:0:1}" = "5" ] || [ "$HTTP" = "000" ]; then
    BACKOFF=$((attempt * 3))
    echo "[fail] POST attempt $attempt got HTTP=$HTTP — retrying in \${BACKOFF}s..." >&2
    sleep "$BACKOFF"; continue
  fi
  echo "[fail] POST attempt $attempt got HTTP=$HTTP (permanent failure):" >&2
  cat /tmp/fail_resp_${cfg.taskId} >&2
  rm -f /tmp/fail_resp_${cfg.taskId}
  exit 1
done
rm -f /tmp/fail_resp_${cfg.taskId}
if [ "$POSTED" = "0" ]; then
  echo "[fail] POST failed after 5 retries — orchestrator unreachable. Marker NOT written." >&2
  exit 1
fi

touch /tmp/fail_${cfg.taskId}
echo ""
echo "[fail] Posted FAILED status. Reason: $REASON"
`;
  fs.writeFileSync(`${WORKSPACE}/fail`, script, { mode: 0o755 });
}

function writeBlockedHelper(cfg: CodeChangeHelperConfig): void {
  validateCfgShellSafety(cfg);
  // Explicit precondition-blocked signal — Claude calls this when a real
  // code change has been made and ./check still fails identically (or the
  // failure is at the toolchain/system-library layer, where no code edit
  // can move the needle). Routes to BLOCKED_PRECONDITION (an INFRA class)
  // rather than CODE_ERROR — review-fix iterations don't burn against the
  // chain, and the strategic agent gets a ProjectMessage so it can decide
  // whether to spawn a precondition-fix direction.
  const script = `#!/bin/bash
# ./blocked "<reason>" — mark task BLOCKED_PRECONDITION (no retry burn).
#
# Use when:
#   - You've made a real code edit and ./check fails identically to the
#     previous attempt — the failure isn't moving and another iteration
#     won't help.
#   - The failure is at the toolchain / system-library layer (e.g.
#     "error while loading shared libraries: libatk-1.0.so.0",
#     "No module named 'ruff'", "playwright is not installed",
#     "command not found: <tool>"). No code edit can fix this.
#   - A repo-wide gate fails on files your PR did not touch (baseline
#     debt — pre-existing lint/test failures unrelated to your change).
#
# Don't use for ordinary lint/test failures Claude can fix — keep
# iterating with ./check instead. Don't use for "I don't know how to fix
# this" — that's ./fail. ./blocked is specifically for "no code edit
# CAN fix this on my own."
#
# Effect: marks the task FAILED with failure_category=BLOCKED_PRECONDITION
# (INFRA class — doesn't count toward the review-fix iteration cap), and
# the orchestrator notifies the strategic agent so it can decide whether
# to spawn a precondition-fix direction.

set -uo pipefail
REASON="\${1:?Usage: ./blocked \\"<reason>\\"}"

API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${cfg.taskId}"

if [ -f /tmp/blocked_${cfg.taskId} ]; then
  echo "[blocked] already marked — no-op"
  exit 0
fi

# "BLOCKER:" prefix is the canonical orchestrator-side signal for the
# BLOCKED_PRECONDITION class (see backend/app/failure_classification.py).
# We send both the explicit failure_category AND the prefixed reason so
# the routing is unambiguous regardless of which path the orchestrator
# takes (normalize_agent_failure_category vs the BLOCKER:-prefix
# fast-path in _classify_failure_class).
PAYLOAD=$(jq -n --arg tid "$TASK" --arg r "BLOCKER: $REASON" \\
  '{task_id: $tid, status: "FAILED", artifact_type: "ERROR_LOG", logs: $r, failure_category: "BLOCKED_PRECONDITION"}')
POSTED=0
for attempt in 1 2 3 4 5; do
  HTTP=$(printf '%s' "$PAYLOAD" | curl -sS -o /tmp/blocked_resp_${cfg.taskId} -w '%{http_code}' --max-time 30 \\
    -X POST "$API/api/agent/status" \\
    -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-) || HTTP="000"
  if [ "$HTTP" = "200" ] || [ "$HTTP" = "202" ]; then
    POSTED=1; break
  fi
  if [ "$HTTP" = "429" ] || [ "\${HTTP:0:1}" = "5" ] || [ "$HTTP" = "000" ]; then
    BACKOFF=$((attempt * 3))
    echo "[blocked] POST attempt $attempt got HTTP=$HTTP — retrying in \${BACKOFF}s..." >&2
    sleep "$BACKOFF"; continue
  fi
  echo "[blocked] POST attempt $attempt got HTTP=$HTTP (permanent failure):" >&2
  cat /tmp/blocked_resp_${cfg.taskId} >&2
  rm -f /tmp/blocked_resp_${cfg.taskId}
  exit 1
done
rm -f /tmp/blocked_resp_${cfg.taskId}
if [ "$POSTED" = "0" ]; then
  echo "[blocked] POST failed after 5 retries — orchestrator unreachable. Marker NOT written." >&2
  exit 1
fi

touch /tmp/blocked_${cfg.taskId}
echo ""
echo "[blocked] Posted BLOCKED_PRECONDITION status. Reason: $REASON"
echo "[blocked] Strategic agent will be notified — it may spawn a precondition-fix direction."
`;
  fs.writeFileSync(`${WORKSPACE}/blocked`, script, { mode: 0o755 });
}
