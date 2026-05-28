import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { WORKSPACE } from '../index.js';
import type { BuildConfig } from '../index.js';
import { SHELL_CMD_RE } from '../shell-safety.js';

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

interface CodeChangeHelperConfig {
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

// Load the canonical sr-pr-section.js renderer from disk and base64-encode
// it for safe bash-level injection into the generated ./pr script. Per
// CLAUDE.md "Generating Code for One Language From Another": the embedded
// script lives as a real file (lintable, testable) and the generator reads
// it rather than mirroring its body in a string constant. Lazy-loaded so
// that test suites which mock fs do not crash on module import.
const _SR_SECTION_SCRIPT_PATH = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  '..',
  'sr-pr-section.js',
);
function loadSrSectionScriptB64(): string {
  try {
    return Buffer.from(
      fs.readFileSync(_SR_SECTION_SCRIPT_PATH, 'utf8'),
    ).toString('base64');
  } catch {
    return '';
  }
}

const BRANCH_RE = /^[A-Za-z0-9._/-]+$/;
const OWNER_REPO_RE = /^[A-Za-z0-9._-]+$/;
const TASK_ID_RE = /^[A-Za-z0-9-]+$/;
const ENV_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

function validateCfgShellSafety(cfg: CodeChangeHelperConfig): void {
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

function writePrHelper(cfg: CodeChangeHelperConfig): void {
  validateCfgShellSafety(cfg);
  // Idempotent push + PR creation/update. Gates on ./check first.
  // Writes /tmp/pr_url_<task_id> on success so the wrapper + ./done can
  // pick up the URL without re-querying GitHub.
  const _SR_SECTION_SCRIPT_B64 = loadSrSectionScriptB64();
  const script = `#!/bin/bash
# ./pr open --title "..." --body "..."  — ship the diff via PR
#
# Gates on ./check (must pass). Then commits any pending changes, pushes
# to ${cfg.workBranch}, and creates a PR (or pushes an update to an
# existing PR for review-fix tasks).
#
# Exits 0 on success (PR URL written to /tmp/pr_url_${cfg.taskId}).
# Non-zero on any failure — error tail printed; fix and retry.

set -uo pipefail
WORKSPACE="\${WORKSPACE:-${WORKSPACE}}"
cd "$WORKSPACE" || { echo "Error: cannot cd to $WORKSPACE" >&2; exit 2; }

OP="\${1:-}"
shift || true

# ── ./pr close --superseded-by <PR#> ─────────────────────────────────────────
# Use when post-rebase conflict resolution leaves an empty diff because an
# equivalent sibling PR already merged the same changes.  Closes the current PR
# cleanly, posts a traceability comment, and writes /tmp/pr_url_<task_id> so
# ./done can mark the task COMPLETED-via-supersession.
if [ "$OP" = "close" ]; then
  SUPERSEDED_BY=""
  PR_NUMBER="${cfg.existingPrNumber !== undefined ? String(cfg.existingPrNumber) : ""}"
  while [ $# -gt 0 ]; do
    case "$1" in
      --superseded-by) SUPERSEDED_BY="$2"; shift 2 ;;
      --pr-number)     PR_NUMBER="$2";     shift 2 ;;
      *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
  done

  # Fall back to PR URL written by a prior ./pr open in this session
  if [ -z "$PR_NUMBER" ]; then
    PR_URL_FILE="/tmp/pr_url_${cfg.taskId}"
    if [ -s "$PR_URL_FILE" ]; then
      PR_NUMBER=$(grep -oE '[0-9]+$' "$PR_URL_FILE" || true)
    fi
  fi
  if [ -z "$PR_NUMBER" ]; then
    echo "[pr close] No PR number available. Pass --pr-number <N> or run ./pr open first." >&2
    exit 1
  fi

  API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
  TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
  TASK="${cfg.taskId}"

  # Refresh GitHub token
  TOKEN_RESP=$(curl -f -sS --max-time 30 \\
    -H "X-Agent-Token: $TOKEN" \\
    "$API/api/agent/github/token?task_id=$TASK") || {
    echo "[pr close] Failed to fetch GitHub token from orchestrator" >&2
    exit 1
  }
  GH_TOKEN=$(printf '%s' "$TOKEN_RESP" | jq -r '.data.token // empty')
  if [ -z "$GH_TOKEN" ]; then
    echo "[pr close] orchestrator returned no token: $TOKEN_RESP" >&2
    exit 1
  fi

  # Close the PR via GitHub API
  CLOSE_HTTP=$(curl -sS -o /tmp/pr_close_resp_${cfg.taskId} -w '%{http_code}' --max-time 30 \\
    -X PATCH "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/pulls/$PR_NUMBER" \\
    -H "Authorization: token $GH_TOKEN" \\
    -H "Accept: application/vnd.github+json" \\
    -d '{"state":"closed"}') || CLOSE_HTTP="000"
  if [ "$CLOSE_HTTP" != "200" ]; then
    echo "[pr close] GitHub PATCH /pulls/$PR_NUMBER returned HTTP=$CLOSE_HTTP:" >&2
    cat /tmp/pr_close_resp_${cfg.taskId} >&2
    rm -f /tmp/pr_close_resp_${cfg.taskId}
    exit 1
  fi
  rm -f /tmp/pr_close_resp_${cfg.taskId}

  # Post supersession comment for traceability
  if [ -n "$SUPERSEDED_BY" ]; then
    COMMENT_BODY=$(jq -n --arg msg "Closing: equivalent changes already merged via PR #$SUPERSEDED_BY. No further action needed on this branch." '{body: $msg}')
    curl -sS --max-time 30 \\
      -X POST "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/issues/$PR_NUMBER/comments" \\
      -H "Authorization: token $GH_TOKEN" \\
      -H "Accept: application/vnd.github+json" \\
      -d "$COMMENT_BODY" >/dev/null 2>&1 || true
  fi
  unset GH_TOKEN

  PR_URL="https://github.com/${cfg.owner}/${cfg.repo}/pull/$PR_NUMBER"
  echo "$PR_URL" > /tmp/pr_url_${cfg.taskId}
  echo ""
  echo "[pr] Closed PR #$PR_NUMBER\${SUPERSEDED_BY:+ (superseded by #$SUPERSEDED_BY)}"
  echo "[pr] Next: ./done"
  exit 0
fi

[ "$OP" != "open" ] && { echo "Usage: ./pr open --title <title> --body <body> [--with-tests]  |  ./pr close [--superseded-by <PR#>]"; exit 2; }

TITLE=""
BODY=""
WITH_TESTS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --title) TITLE="$2"; shift 2 ;;
    --body) BODY="$2"; shift 2 ;;
    --with-tests) WITH_TESTS=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ -z "$TITLE" ] && { echo "Error: --title is required" >&2; exit 2; }

# ── Self-review section injection ─────────────────────────────────────────────
# Reads /tmp/self_review_<taskId>.json if present, renders the
# <!-- blueprint:self-review --> block, strips any prior block from BODY
# (idempotent), and appends the fresh one.  No-op when JSON is absent.
SR_FILE="/tmp/self_review_${cfg.taskId}.json"
SR_SKIP_FILE="/tmp/self_review_skip_reason_${cfg.taskId}.txt"
SR_SECTION=""
if [ -f "$SR_FILE" ]; then
  printf '%s' '${_SR_SECTION_SCRIPT_B64}' | base64 -d > /tmp/sr_section_${cfg.taskId}.js
  SR_SECTION=$(node /tmp/sr_section_${cfg.taskId}.js "$SR_FILE" "$SR_SKIP_FILE" 2>/dev/null || true)
  rm -f /tmp/sr_section_${cfg.taskId}.js
fi
if [ -n "$SR_SECTION" ]; then
  BODY=$(printf '%s' "$BODY" | sed '/<!-- blueprint:self-review -->/,/<!-- \\/blueprint:self-review -->/d')
  BODY=$(printf '%s\\n\\n%s' "$BODY" "$SR_SECTION")
fi

${(cfg.promotionTarget !== undefined ? cfg.baseBranch === cfg.promotionTarget : cfg.testsOnMainBranch) ? `# Mandatory test gate: force WITH_TESTS=1 when the diff touches backend/.
# The agent must not be able to skip backend tests on LTS-branch tasks.
_BASE_GATE="${cfg.baseBranch}"
_CMT_GATE=""
if git rev-parse --verify "origin/$_BASE_GATE" >/dev/null 2>&1; then
  _CMT_GATE=$(git diff --name-only "origin/$_BASE_GATE...HEAD" 2>/dev/null || true)
elif git rev-parse --verify "$_BASE_GATE" >/dev/null 2>&1; then
  _CMT_GATE=$(git diff --name-only "$_BASE_GATE...HEAD" 2>/dev/null || true)
fi
_UNC_GATE=$(git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null)
_CHG_GATE=$(printf '%s\\n%s\\n' "$_CMT_GATE" "$_UNC_GATE" | sort -u | grep -v '^$' || true)
if printf '%s\\n' "$_CHG_GATE" | grep -q '^backend/'; then
  echo "[pr] backend/ touched on LTS branch — forcing WITH_TESTS=1 (mandatory test gate)"
  WITH_TESTS=1
fi
` : ''}
# 1. Run ./check as gate (lint always; tests on main branch when --with-tests
#    OR on review-fix path that touches backend changes).
# Branch on WITH_TESTS instead of expanding an array. Older bash + set -u
# aborts on an empty array expansion, so just call ./check directly.
if [ "$WITH_TESTS" = "1" ]; then
  if ! ./check test; then
    echo ""
    echo "[pr] ./check test failed — fix lint/build/tests then re-run ./pr open."
    exit 1
  fi
else
  if ! ./check; then
    echo ""
    echo "[pr] ./check failed — fix lint/build then re-run ./pr open."
    exit 1
  fi
fi

# 2. Stage and commit any pending changes
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git -c user.email=blueprint-agent@automated.dev -c user.name='Blueprint Agent' \\
    commit -m "$TITLE" >/dev/null || {
    echo "[pr] git commit failed" >&2
    git status --short
    exit 1
  }
fi

# Anything to push?
COMMITS_AHEAD=$(git rev-list --count "origin/${cfg.baseBranch}..HEAD" 2>/dev/null || echo 0)
if [ "$COMMITS_AHEAD" = "0" ]; then
  echo "[pr] No commits ahead of ${cfg.baseBranch} — nothing to push."
  echo "[pr] If this branch was superseded (a sibling PR already merged the same changes),"
  echo "[pr] run:  ./pr close --superseded-by <winning-PR-number>"
  echo "[pr] Otherwise make your changes and re-run ./pr open."
  exit 1
fi

API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${cfg.taskId}"

# 3. Refresh GitHub token + remote URL right before push. The clone-time token
#    is per-task ephemeral and may be near expiry by the time Claude ships.
TOKEN_RESP=$(curl -f -sS --max-time 30 \\
  -H "X-Agent-Token: $TOKEN" \\
  "$API/api/agent/github/token?task_id=$TASK") || {
  echo "[pr] Failed to fetch fresh GitHub token from orchestrator" >&2
  exit 1
}
GH_PUSH_TOKEN=$(printf '%s' "$TOKEN_RESP" | jq -r '.data.token // empty')
if [ -z "$GH_PUSH_TOKEN" ]; then
  echo "[pr] orchestrator returned no token: $TOKEN_RESP" >&2
  exit 1
fi
git remote set-url origin "https://x-access-token:$GH_PUSH_TOKEN@github.com/${cfg.owner}/${cfg.repo}.git"
unset GH_PUSH_TOKEN  # only needed for the remote URL above

# 4. Push (with retry — concurrent-push handling)
PUSH_OK=0
for attempt in 1 2 3; do
  if git push --force-with-lease origin "${cfg.workBranch}" 2>/tmp/pr_push.log; then
    PUSH_OK=1
    break
  fi
  if grep -q 'stale info' /tmp/pr_push.log; then
    echo "[pr] push attempt $attempt: stale lease, fetching + retrying..."
    git fetch origin "${cfg.workBranch}" 2>/dev/null || true
    sleep 2
  else
    echo "[pr] push attempt $attempt failed:"; cat /tmp/pr_push.log; sleep 2
  fi
done
if [ "$PUSH_OK" = "0" ]; then
  echo "[pr] push failed after 3 attempts" >&2
  exit 1
fi

# 5. Create or update PR via orchestrator-mediated endpoint.
#    Why the orchestrator endpoint instead of \`gh pr create\`:
#    - gh CLI is not installed on agent VMs, and GH_TOKEN isn't in Claude's env.
#    - The endpoint already handles idempotency (returns existing PR if open).
#    - It writes a PULL_REQUEST Artifact row immediately on creation so the
#      reaper can route reaped mid-run tasks to EVALUATING instead of
#      retrying-from-scratch (orphan-PR recovery).
${cfg.existingPrNumber !== undefined ? `
# Existing PR (#${cfg.existingPrNumber}) — review-fix path. Just pushed
# updates; no creation needed. The PULL_REQUEST artifact already exists from
# the original create task, so we don't need to call the endpoint here.
PR_URL="https://github.com/${cfg.owner}/${cfg.repo}/pull/${cfg.existingPrNumber}"
echo "[pr] Pushed updates to existing PR #${cfg.existingPrNumber}"
# Update existing PR body with fresh SR section (best-effort — log on failure)
if [ -n "$SR_SECTION" ]; then
  _PTRESP=$(curl -f -sS --max-time 30 \\
    -H "X-Agent-Token: $TOKEN" \\
    "$API/api/agent/github/token?task_id=$TASK" 2>/dev/null) || _PTRESP=""
  _PTOKEN=$(printf '%s' "$_PTRESP" | jq -r '.data.token // empty' 2>/dev/null || true)
  if [ -n "$_PTOKEN" ]; then
    # Fetch current PR body. Use -f so non-2xx GET responses (auth/rate-limit/
    # 5xx/404) fail fast rather than yielding an empty body that would let the
    # subsequent PATCH wipe the live PR description with just the SR section.
    _GETBODY_FILE=/tmp/pr_get_body_${cfg.taskId}.json
    if curl -f -sS --max-time 30 \\
        "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/pulls/${cfg.existingPrNumber}" \\
        -H "Authorization: token $_PTOKEN" -H "Accept: application/vnd.github+json" \\
        -o "$_GETBODY_FILE" 2>/dev/null; then
      _CURBODY=$(jq -r '.body // ""' "$_GETBODY_FILE" 2>/dev/null || printf '')
      _UPDBODY=$(printf '%s' "$_CURBODY" | sed '/<!-- blueprint:self-review -->/,/<!-- \\/blueprint:self-review -->/d')
      _UPDBODY=$(printf '%s\\n\\n%s' "$_UPDBODY" "$SR_SECTION")
      _PHTTP=$(jq -n --arg b "$_UPDBODY" '{body: $b}' | curl -sS -o /dev/null -w '%{http_code}' \\
        --max-time 30 -X PATCH \\
        "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/pulls/${cfg.existingPrNumber}" \\
        -H "Authorization: token $_PTOKEN" -H "Accept: application/vnd.github+json" \\
        -H "Content-Type: application/json" --data-binary @- 2>/dev/null) || _PHTTP="000"
      [ "$_PHTTP" = "200" ] || echo "[pr] Warning: SR body update returned HTTP $_PHTTP — continuing" >&2
    else
      echo "[pr] Warning: GET PR body failed — skipping SR section update to avoid wiping description" >&2
    fi
    rm -f "$_GETBODY_FILE"
    unset _PTOKEN
  fi
fi
` : `
PR_PAYLOAD=$(jq -n --arg tid "$TASK" --arg owner "${cfg.owner}" --arg repo "${cfg.repo}" \\
  --arg title "$TITLE" --arg head "${cfg.workBranch}" --arg base "${cfg.baseBranch}" --arg body "$BODY" \\
  '{task_id: $tid, owner: $owner, repo: $repo, title: $title, head: $head, base: $base, body: $body}')
PR_RESP=""
PR_OK=0
for attempt in 1 2 3 4 5; do
  PR_HTTP=$(printf '%s' "$PR_PAYLOAD" | curl -sS -o /tmp/pr_create_resp_${cfg.taskId} -w '%{http_code}' --max-time 90 \\
    -X POST "$API/api/agent/github/pull-requests" \\
    -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-) || PR_HTTP="000"
  if [ "$PR_HTTP" = "200" ] || [ "$PR_HTTP" = "201" ] || [ "$PR_HTTP" = "202" ]; then
    PR_RESP=$(cat /tmp/pr_create_resp_${cfg.taskId})
    PR_OK=1; break
  fi
  if [ "$PR_HTTP" = "429" ] || [ "\${PR_HTTP:0:1}" = "5" ] || [ "$PR_HTTP" = "000" ]; then
    BACKOFF=$((attempt * 3))
    echo "[pr] PR creation attempt $attempt got HTTP=$PR_HTTP — retrying in \${BACKOFF}s..." >&2
    sleep "$BACKOFF"; continue
  fi
  echo "[pr] PR creation attempt $attempt got HTTP=$PR_HTTP (permanent failure):" >&2
  cat /tmp/pr_create_resp_${cfg.taskId} >&2
  rm -f /tmp/pr_create_resp_${cfg.taskId}
  exit 1
done
rm -f /tmp/pr_create_resp_${cfg.taskId}
if [ "$PR_OK" = "0" ]; then
  echo "[pr] PR creation failed after 5 retries — orchestrator unreachable" >&2
  exit 1
fi
PR_URL=$(printf '%s' "$PR_RESP" | jq -r '.data.html_url // empty')
if [ -z "$PR_URL" ]; then
  echo "[pr] orchestrator response had no html_url: $PR_RESP" >&2
  exit 1
fi
`}

# 6. Persist PR URL for ./done + wrapper detection
echo "$PR_URL" > /tmp/pr_url_${cfg.taskId}

# 7. Post-create mergeable check.
#
# GitHub computes mergeable status asynchronously (a few seconds after
# the head commit lands). Poll briefly, then if the PR is in a real
# conflict state (mergeable=false + state=dirty) tell the agent so
# they can rebase and resolve while the workspace is still warm. This
# is dramatically cheaper than letting the orchestrator spawn a fresh
# fix-task from a cold VM that has to re-derive the change intent.
#
# State semantics (from GitHub):
#   clean / has_hooks  → mergeable, will merge / fast-forward fine
#   blocked / unstable → CI gating; auto-merge handles it
#   behind             → out-of-date with base; GitHub or auto-rebase fixes
#   dirty              → real merge conflict; needs rebase + manual resolve
#   draft              → marked draft; no action
#
# Only \`dirty\` triggers the warning + exit 4. Everything else exits 0.
PR_NUMBER=$(printf '%s' "$PR_URL" | grep -oE '[0-9]+$' || true)
if [ -n "$PR_NUMBER" ]; then
  TOKEN_RESP=$(curl -f -sS --max-time 30 \\
    -H "X-Agent-Token: $TOKEN" \\
    "$API/api/agent/github/token?task_id=$TASK" 2>/dev/null) || TOKEN_RESP=""
  GH_TOKEN_PR=$(printf '%s' "$TOKEN_RESP" | jq -r '.data.token // empty' 2>/dev/null || true)
  if [ -n "$GH_TOKEN_PR" ]; then
    PR_MERGEABLE="null"
    PR_STATE="unknown"
    for pr_attempt in 1 2 3 4 5; do
      _PR_DETAIL=$(curl -sS --max-time 20 \\
        -H "Authorization: token $GH_TOKEN_PR" \\
        -H "Accept: application/vnd.github+json" \\
        "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/pulls/$PR_NUMBER" 2>/dev/null || echo "")
      PR_MERGEABLE=$(printf '%s' "$_PR_DETAIL" | jq -r '.mergeable // "null"' 2>/dev/null || echo "null")
      PR_STATE=$(printf '%s' "$_PR_DETAIL" | jq -r '.mergeable_state // "unknown"' 2>/dev/null || echo "unknown")
      [ "$PR_MERGEABLE" != "null" ] && break
      sleep 3
    done
    unset GH_TOKEN_PR
    if [ "$PR_MERGEABLE" = "false" ] && [ "$PR_STATE" = "dirty" ]; then
      echo ""
      echo "[pr] WARNING: PR #$PR_NUMBER has merge conflicts with origin/${cfg.baseBranch}"
      echo "[pr]   mergeable=false, mergeable_state=dirty"
      echo "[pr]"
      echo "[pr] You're still in the workspace — resolve here, much cheaper than"
      echo "[pr] spawning a fix-task from a fresh VM that has to re-derive intent."
      echo "[pr]"
      echo "[pr] Resolution flow:"
      echo "[pr]   git fetch origin ${cfg.baseBranch}"
      echo "[pr]   git rebase origin/${cfg.baseBranch}"
      echo "[pr]   # Resolve conflicts in the listed files (use the Edit tool)."
      echo "[pr]   # If a conflict is in a file YOUR PR did not intend to touch,"
      echo "[pr]   # the right move is usually \\\`git checkout --theirs <file>\\\` to"
      echo "[pr]   # take the base branch's version, then re-apply your edits to"
      echo "[pr]   # the files you OWN. Don't accidentally revert sibling work."
      echo "[pr]   git rebase --continue"
      echo "[pr]   ./check       # re-verify before pushing"
      echo "[pr]   ./pr open --title \\"...\\" --body \\"...\\"   # re-push (force-with-lease)"
      echo "[pr]"
      echo "[pr] Limit: ~2 attempts. If you can't resolve cleanly (the conflict is"
      echo "[pr] structural, not just a textual overlap, or it's in code your task"
      echo "[pr] should not be modifying), call \\\`./blocked \\"<reason>\\"\\\` instead."
      echo "[pr] The orchestrator will spawn a fix-task as backstop."
      exit 4
    fi
    if [ "$PR_STATE" = "behind" ]; then
      echo "[pr] note: PR #$PR_NUMBER is behind origin/${cfg.baseBranch}; auto-merge will rebase when CI clears"
    fi
  fi
fi

echo ""
echo "[pr] OK — $PR_URL"
echo "[pr] Next: ./done   (or call ./pr again if you push more commits)"
`;
  fs.writeFileSync(`${WORKSPACE}/pr`, script, { mode: 0o755 });
}

function writeDoneHelper(cfg: CodeChangeHelperConfig): void {
  validateCfgShellSafety(cfg);
  // Posts COMPLETED to the orchestrator with the PR URL from /tmp/pr_url_<task_id>.
  // Also writes /tmp/done_<task_id> as a marker for the wrapper to detect on
  // exit and skip its fallback push.
  const script = `#!/bin/bash
# ./done — mark task COMPLETED. Requires ./pr open to have run first
# (writes /tmp/pr_url_${cfg.taskId} which this reads). Posts the COMPLETED
# status to the orchestrator with the PR URL, then writes a marker so the
# wrapper exits cleanly without re-doing commit/push.
#
# Usage:
#   ./done                                     # standard: ./check must pass
#   ./done --warn-out-of-scope "<reason>"      # judgment-call escape hatch:
#       ship despite ./check failures when those failures are PRE-EXISTING
#       on origin/${cfg.baseBranch} and out of scope for this PR. Posts a FEEDBACK
#       project message describing the pre-existing issues + your reason
#       so strategic/operators can spawn cleanup work later. The reason
#       text MUST include the specific gates/files that failed and why
#       fixing them is out of scope (e.g., "admin-ui eslint reports 4
#       errors in DashboardHome.tsx that pre-date this PR; fixing them
#       requires refactoring the digest-card layout which is not in
#       scope for this auth-flow change").
#
#       USE ./blocked INSTEAD when the failure requires introducing a
#       new bug or precondition (missing package, infra gap, agent
#       can't even start the work). --warn-out-of-scope is for issues
#       that already existed on main BEFORE this PR.

set -uo pipefail

WARN_OUT_OF_SCOPE_REASON=""
SKIP_NEXT=0
for i in "$@"; do
  if [ "$SKIP_NEXT" = "1" ]; then
    WARN_OUT_OF_SCOPE_REASON="$i"
    SKIP_NEXT=0
    continue
  fi
  case "$i" in
    --warn-out-of-scope)
      SKIP_NEXT=1
      ;;
    --warn-out-of-scope=*)
      WARN_OUT_OF_SCOPE_REASON="\${i#--warn-out-of-scope=}"
      ;;
  esac
done
if [ "$SKIP_NEXT" = "1" ] && [ -z "$WARN_OUT_OF_SCOPE_REASON" ]; then
  echo "Error: --warn-out-of-scope requires a reason argument" >&2
  echo "Usage: ./done --warn-out-of-scope \\"<reason>\\"" >&2
  exit 2
fi
if [ -n "$WARN_OUT_OF_SCOPE_REASON" ] && [ \${#WARN_OUT_OF_SCOPE_REASON} -lt 30 ]; then
  echo "Error: --warn-out-of-scope reason must be at least 30 chars (you gave \${#WARN_OUT_OF_SCOPE_REASON})." >&2
  echo "       Be specific: which gates failed, which files, why fixing is out of scope." >&2
  exit 2
fi

PR_FILE="/tmp/pr_url_${cfg.taskId}"
if [ ! -s "$PR_FILE" ]; then
  echo "Error: $PR_FILE missing or empty — run ./pr open first." >&2
  exit 2
fi
PR_URL=$(cat "$PR_FILE")
# Validate shape — must be a canonical GitHub pull URL.
# Aligned with agent/src/handlers/code-change.ts:337 (same regex).
PR_URL_RE='^https://github\\.com/[^/[:space:]]+/[^/[:space:]]+/pull/[0-9]+$'
if ! printf '%s' "$PR_URL" | grep -qE "$PR_URL_RE"; then
  echo "[done] Error: $PR_FILE contains an invalid PR URL: $PR_URL" >&2
  echo "[done]   Expected: https://github.com/<owner>/<repo>/pull/<number>" >&2
  echo "[done]   ./pr open must produce a canonical pull URL (not /commit/, /compare/, etc.)." >&2
  exit 2
fi

API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${cfg.taskId}"

# Idempotency: if marker already exists, this is a re-run — no-op cleanly.
if [ -f /tmp/done_${cfg.taskId} ]; then
  echo "[done] already marked — no-op (PR: $PR_URL)"
  exit 0
fi

# Optional enrichment: if Claude wrote /tmp/fix_responses_<task_id>.json
# (review-fix dialog-system response), include it in the payload so the
# orchestrator's verify-mode reviewer can chain-of-custody the fix
# rationale forward (review_pipeline.py:1664-1681).
FIX_RESPONSES_FILE="/tmp/fix_responses_${cfg.taskId}.json"
FIX_RESPONSES_JSON=""
if [ -s "$FIX_RESPONSES_FILE" ]; then
  if jq empty "$FIX_RESPONSES_FILE" >/dev/null 2>&1; then
    FIX_RESPONSES_JSON=$(cat "$FIX_RESPONSES_FILE")
  else
    echo "[done] /tmp/fix_responses_${cfg.taskId}.json exists but is not valid JSON — skipping enrichment" >&2
  fi
fi

# Soft-fallback self-review outcome. Claude should write a structured
# self-review JSON to /tmp/self_review_<task_id>.json before calling
# ./done; we forward it as self_review_outcome on the payload so
# downstream consumers (review_pipeline retry context, admin_digest
# catch-rate metrics, reaper retry pairing) get the signal. If missing
# or invalid, we emit a minimal fallback envelope so the gap is visible
# in metrics but the task doesn't block.
SELF_REVIEW_FILE="/tmp/self_review_${cfg.taskId}.json"
SELF_REVIEW_JSON=""
if [ -s "$SELF_REVIEW_FILE" ]; then
  if jq empty "$SELF_REVIEW_FILE" >/dev/null 2>&1; then
    SELF_REVIEW_JSON=$(cat "$SELF_REVIEW_FILE")
  else
    echo "[done] /tmp/self_review_${cfg.taskId}.json exists but is not valid JSON — using fallback envelope" >&2
    SELF_REVIEW_JSON='{"verdict":"unknown","check_results":{},"blind_spots":["self-review JSON parsing failed"],"skip_reason":"json_parse_failed"}'
  fi
else
  echo "[done] /tmp/self_review_${cfg.taskId}.json not found — using minimal fallback envelope" >&2
  SKIP_REASON="json_missing"; MARKER="/tmp/self_review_skip_reason_${cfg.taskId}.txt"; [ -s "$MARKER" ] && SKIP_REASON=$(head -c 64 "$MARKER" | tr -dc 'a-z_')
  SELF_REVIEW_JSON='{"verdict":"unknown","check_results":{},"blind_spots":["self-review skipped — investigate"],"skip_reason":"'"$SKIP_REASON"'"}'
fi

# Regressing-category reasoning gate: require non-trivial reasoning for categories
# that have above-floor miss rates for this task type (embedded at write time).
# If any are missing, print the gap and exit so Claude can fix the SR JSON first.
${cfg.regressingCategories && cfg.regressingCategories.length > 0
  ? `REGRESSING_CATS=(${cfg.regressingCategories.map((c) => `"${c}"`).join(' ')})
MISSING_REASONING=()
for _CAT in "\${REGRESSING_CATS[@]}"; do
  _R=$(printf '%s' "$SELF_REVIEW_JSON" | jq -r --arg c "$_CAT" '.check_results[$c].reasoning // ""' 2>/dev/null || echo "")
  if [ "\${#_R}" -lt 10 ]; then
    # Short reasoning passes if it contains a file:line evidence anchor
    _HAS_ANCHOR=0
    if printf '%s' "$_R" | grep -qE '[a-zA-Z0-9._/-]+[.][a-z]+:[0-9]+' 2>/dev/null; then
      _HAS_ANCHOR=1
    fi
    if [ "\${_HAS_ANCHOR}" = "0" ]; then
      MISSING_REASONING+=("$_CAT")
    fi
  fi
done
if [ "\${#MISSING_REASONING[@]}" -gt 0 ]; then
  echo "[done] Regressing categories need a reasoning field (>=10 chars, or a file:line anchor) in check_results:" >&2
  for _CAT in "\${MISSING_REASONING[@]}"; do echo "  - $_CAT" >&2; done
  echo "Update /tmp/self_review_${cfg.taskId}.json and re-run ./done." >&2
  exit 1
fi`
  : '# No regressing categories for this task type — reasoning check skipped.'}

# High-stakes-category evidence-anchor gate: require concrete evidence anchors for PASS verdicts
# on correctness, error-handling, security, regression_risk, and scope-drift. Anchors must be
# file:line, diff stats reference, or file-list reference. If any are missing, print the gap and
# exit so Claude can fix the SR JSON.
HIGH_STAKES_CATS=("correctness" "error-handling" "security" "regression_risk" "scope-drift")
MISSING_EVIDENCE=()
for _CAT in "\${HIGH_STAKES_CATS[@]}"; do
  _PASSED=$(printf '%s' "$SELF_REVIEW_JSON" | jq -r --arg c "$_CAT" '.check_results[$c].passed // false' 2>/dev/null || echo "false")
  _REASONING=$(printf '%s' "$SELF_REVIEW_JSON" | jq -r --arg c "$_CAT" '.check_results[$c].reasoning // ""' 2>/dev/null || echo "")

  if [ "$_PASSED" = "true" ]; then
    # Check if reasoning has evidence anchor: file:line format, diff stats, or file list reference
    _HAS_ANCHOR=0
    # File:line reference (e.g., path/to/file.ts:42)
    if printf '%s' "$_REASONING" | grep -E '[a-zA-Z0-9._/-]+[.][a-z]+:[0-9]+' > /dev/null 2>&1; then
      _HAS_ANCHOR=1
    # Diff-stat reference (e.g., "lines / files" or "diff:")
    elif printf '%s' "$_REASONING" | grep -iE 'diff.*lines|files|threshold' > /dev/null 2>&1; then
      _HAS_ANCHOR=1
    # File-list reference (e.g., "related_files" or list of files)
    elif printf '%s' "$_REASONING" | grep -iE 'related_files|modified files|checked.*files?' > /dev/null 2>&1; then
      _HAS_ANCHOR=1
    # Substantial reasoning (20+ chars)
    elif printf '%s' "$_REASONING" | grep '.' > /dev/null 2>&1 && [ "\${#_REASONING}" -ge 20 ]; then
      _HAS_ANCHOR=1
    fi
    if [ "\${_HAS_ANCHOR}" = "0" ]; then
      MISSING_EVIDENCE+=("$_CAT")
    fi
  fi
done
if [ "\${#MISSING_EVIDENCE[@]}" -gt 0 ]; then
  echo "[done] PASS verdicts on high-stakes categories must include evidence anchors (file:line, diff stats, or file list):" >&2
  for _CAT in "\${MISSING_EVIDENCE[@]}"; do echo "  - $_CAT" >&2; done
  echo "Update /tmp/self_review_${cfg.taskId}.json with evidence anchors in reasoning and re-run ./done." >&2
  exit 1
fi

# scope-drift overage gate: if the diff materially exceeds the "large" scope threshold
# (800 lines / 15 files), do not allow scope-drift PASS — the agent must mark it FAIL.
# This catches cases where the diff is unexpectedly huge despite a smaller scope declaration.
_SCOPE_DRIFT_PASSED=$(printf '%s' "$SELF_REVIEW_JSON" | jq -r '.check_results["scope-drift"].passed // false' 2>/dev/null || echo "false")
if [ "$_SCOPE_DRIFT_PASSED" = "true" ]; then
  _DIFF_STAT=$(git -C "${WORKSPACE}" diff --stat HEAD 2>/dev/null | tail -1 || echo "")
  if [ -n "$_DIFF_STAT" ]; then
    # Parse "X files changed, Y insertions(+), Z deletions(-)" — extract files and total lines
    _FILES=$(printf '%s' "$_DIFF_STAT" | grep -oE '^[[:space:]]*[0-9]+' | xargs)
    _LINES=$(printf '%s' "$_DIFF_STAT" | grep -oE '[0-9]+[[:space:]]*(insertion|deletion)' | grep -oE '[0-9]+' | paste -sd+ | bc 2>/dev/null || echo "0")
    # If diff exceeds large threshold (800 lines, 15 files), block PASS
    if [ "$_FILES" -gt 15 ] || [ "$_LINES" -gt 800 ]; then
      echo "[done] scope-drift cannot be PASS when diff exceeds large threshold (800 lines / 15 files):" >&2
      echo "       Detected: $_LINES lines / $_FILES files" >&2
      echo "       Mark scope-drift as FAIL with the overage details, or revise your scope claim." >&2
      echo "       Update /tmp/self_review_${cfg.taskId}.json and re-run ./done." >&2
      exit 1
    fi
  fi
fi

# Optional enrichment: changed_files from git diff — review-pipeline scope
# analysis, direction credit, rollup management all read this from
# task.context._pr_changed_files. Without it those signals degrade.
CHANGED_FILES_JSON=""
if BASE_COMMIT_FILE="/tmp/base_commit_${cfg.taskId}" && [ -s "$BASE_COMMIT_FILE" ]; then
  BASE_COMMIT=$(cat "$BASE_COMMIT_FILE")
  CHANGED_FILES=$(git -C "${WORKSPACE}" diff --name-only "$BASE_COMMIT..HEAD" 2>/dev/null | head -50 | jq -R . | jq -s .)
  if [ -n "$CHANGED_FILES" ] && [ "$CHANGED_FILES" != "[]" ]; then
    CHANGED_FILES_JSON="$CHANGED_FILES"
  fi
fi

# Retry on 429/5xx/network — orchestrator returns 429 on FOR-UPDATE
# contention with Retry-After: 5. Without retry, contended completions
# never reach the orchestrator and the task stays IN_PROGRESS until the
# reaper kicks in minutes later.
# --warn-out-of-scope: post a FEEDBACK project message BEFORE the
# COMPLETED post so the warning is visible to strategic even if the
# COMPLETED post stalls or fails. Best-effort: a message-post failure
# does not block ./done — the warn_out_of_scope_reason field on the
# COMPLETED payload is the durable record.
if [ -n "$WARN_OUT_OF_SCOPE_REASON" ]; then
  WARN_MSG="[shipped via ./done --warn-out-of-scope by task ${cfg.taskId}]
PR: $PR_URL

The agent shipped this diff despite ./check failures, asserting that
the failures are pre-existing on origin/${cfg.baseBranch} and out of
scope for this PR. Reason given:

$WARN_OUT_OF_SCOPE_REASON

Strategic / operators: this is a signal to spawn cleanup work for the
referenced files/gates. The PR itself is intended to merge as-is."
  jq -n --arg tid "$TASK" --arg c "$WARN_MSG" \\
    '{task_id: $tid, op: "create_message", data: {type: "FEEDBACK", content: $c}}' | \\
    curl -sS --max-time 30 --connect-timeout 10 -X POST "$API/api/agent/strategic" \\
      -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @- \\
    > /tmp/done_warn_msg_${cfg.taskId}.log 2>&1 || \\
    echo "[done] warn-out-of-scope FEEDBACK message post failed (best-effort) — see /tmp/done_warn_msg_${cfg.taskId}.log" >&2
fi

PAYLOAD=$(jq -n --arg tid "$TASK" --arg pr "$PR_URL" \\
  --argjson fix_responses "\${FIX_RESPONSES_JSON:-null}" \\
  --argjson self_review "$SELF_REVIEW_JSON" \\
  --argjson changed_files "\${CHANGED_FILES_JSON:-null}" \\
  --arg warn_reason "\${WARN_OUT_OF_SCOPE_REASON:-}" \\
  '{task_id: $tid, status: "COMPLETED", artifact_type: "PULL_REQUEST", pr_url: $pr, logs: ("Task complete. PR: " + $pr)}
   + (if $fix_responses then {fix_agent_responses: $fix_responses} else {} end)
   + {self_review_outcome: $self_review}
   + (if $changed_files then {changed_files: $changed_files} else {} end)
   + (if ($warn_reason | length) > 0 then {warn_out_of_scope_reason: $warn_reason} else {} end)')

# Stale-result envelope: persist BEFORE attempting the POST so that if
# the VM is killed mid-retry, the next agent boot can scan for the
# envelope and replay the COMPLETED post. Mirrors the TS-side behaviour
# in persistAndPostStatus (index.ts:567). Without this, a kill during
# the bash 45s retry window leaves the task IN_PROGRESS until the reaper
# sweeps it as orphan, then re-runs from scratch — duplicate work + retry
# budget burn.
ENVELOPE="/tmp/task_${cfg.taskId}_result.json"
printf '%s' "$PAYLOAD" > "$ENVELOPE" 2>/dev/null || \
  echo "[done] WARNING: failed to persist stale-result envelope to $ENVELOPE" >&2

POSTED=0
for attempt in 1 2 3 4 5; do
  HTTP=$(printf '%s' "$PAYLOAD" | curl -sS -o /tmp/done_resp_${cfg.taskId} -w '%{http_code}' --max-time 30 \\
    -X POST "$API/api/agent/status" \\
    -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-) || HTTP="000"
  if [ "$HTTP" = "200" ] || [ "$HTTP" = "202" ]; then
    POSTED=1
    break
  fi
  if [ "$HTTP" = "429" ] || [ "\${HTTP:0:1}" = "5" ] || [ "$HTTP" = "000" ]; then
    BACKOFF=$((attempt * 3))
    echo "[done] POST attempt $attempt got HTTP=$HTTP — retrying in \${BACKOFF}s..." >&2
    sleep "$BACKOFF"
    continue
  fi
  # 4xx other than 429: don't retry — likely a permanent error.
  echo "[done] POST attempt $attempt got HTTP=$HTTP (permanent failure):" >&2
  cat /tmp/done_resp_${cfg.taskId} >&2
  rm -f /tmp/done_resp_${cfg.taskId}
  exit 1
done
rm -f /tmp/done_resp_${cfg.taskId}
if [ "$POSTED" = "0" ]; then
  echo "[done] POST failed after 5 retries — orchestrator unreachable. Marker NOT written; wrapper will fall back to its own status post." >&2
  exit 1
fi

# Marker is written ONLY after a successful POST. The wrapper inspects it
# to decide whether to skip its fallback completion path.
touch /tmp/done_${cfg.taskId}
# Envelope replayed successfully — remove it so the next agent boot doesn't
# resend a duplicate COMPLETED post for this task ID.
rm -f "$ENVELOPE"
echo ""
echo "[done] OK — wrapper will exit cleanly. PR: $PR_URL"
`;
  fs.writeFileSync(`${WORKSPACE}/done`, script, { mode: 0o755 });
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
