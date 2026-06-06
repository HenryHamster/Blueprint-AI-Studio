import fs from 'fs';
import { WORKSPACE } from '../index.js';
import type { BuildConfig } from '../index.js';
import { SHELL_CMD_RE } from '../shell-safety.js';
import {
  buildScopedLintOneliner,
  detectLinterKind,
  validateCfgShellSafety,
  isBlueprintSelfRepo,
  type CodeChangeHelperConfig,
} from './code-change-helpers.js';

export function writeCheckHelper(cfg: CodeChangeHelperConfig): void {
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
    # the rest is CI's job. \`set -o pipefail\` so the harness's non-zero
    # exit propagates through \`| tail\` (same rationale as the agent
    # vitest line above — the outer pipefail does not reach a fresh bash -c).
    run "backend pytest" bash -c 'set -o pipefail; bash scripts/test-backend.sh --standalone --no-postgres 2>&1 | tail -200'
  fi
  # Fast migration-chain guard: no DB, no Docker, sub-second. Runs only
  # when at least one alembic/versions/ file was added or changed.
  if printf '%s\\n' "$CHANGED" | grep -q '^backend/alembic/versions/.*\\.py$'; then
    run "backend migration chain" bash -c 'python3 backend/app/migration_chain.py 2>&1'
  fi
fi

# Fast module size-guardrail check: no DB/Docker/network, sub-second. Mirrors
# the line-count caps that backend/testing/test_invariants.py enforces, so a
# size breach surfaces HERE (before ./pr open) instead of only after a PR is
# open and a full CI/Docker round-trip is burned. Scope to whichever stacks
# the diff touched, mapping admin-ui -> frontend (the JSON's section name).
# Invoked BY REFERENCE as a committed script (exactly like the migration-chain
# guard above) — no embedded Python heredoc (see CLAUDE.md). A breach makes
# the checker exit non-zero, which \`run\` records in FAILED so ./check fails.
# Remediate with: python3 backend/testing/check_size_limits.py --fix
SIZE_SECTIONS=""
if touches backend/;          then SIZE_SECTIONS="$SIZE_SECTIONS,backend"; fi
if touches agent/;            then SIZE_SECTIONS="$SIZE_SECTIONS,agent"; fi
if admin_ui_runtime_touched;  then SIZE_SECTIONS="$SIZE_SECTIONS,frontend"; fi
SIZE_SECTIONS="\${SIZE_SECTIONS#,}"
if [ -n "$SIZE_SECTIONS" ]; then
  run "size guardrail ($SIZE_SECTIONS)" bash -c "python3 backend/testing/check_size_limits.py --sections $SIZE_SECTIONS 2>&1"
fi
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
  if (cfg.buildConfig?.directories?.length) {
    stackBlocks = buildConfigDrivenStackBlocks(cfg.buildConfig, cfg.taskId);
  } else if (isBlueprintSelfRepo(cfg.owner, cfg.repo)) {
    stackBlocks = blueprintStackBlocks;
  } else {
    stackBlocks = genericNoConfigStackBlock(cfg.owner, cfg.repo);
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
  const script = header + stackBlocks + footer;
  fs.writeFileSync(`${WORKSPACE}/check`, script, { mode: 0o755 });
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
      _AVAIL_MB=$(awk '/^MemAvailable:/ {print int($2/1024); exit}' /proc/meminfo 2>/dev/null || echo 99999)
      if [ -n "\${ALLOW_LOW_MEM:-}" ] || [ "\${_AVAIL_MB:-99999}" -ge 4096 ]; then
        _GATE_MODE="harness"
      elif grep -qs -- '--standalone' ${harness}; then
        # The harness's own memory guard would refuse the full suite below
        # 4 GB available; its standalone mode is the sanctioned partial run.
        _GATE_MODE="harness-standalone"
      fi
    fi
`
    : '';
  const harnessCases = harness
    ? `      harness)
        run "${dirPrefix} test (sanctioned harness)" bash -c 'set -o pipefail; bash ${harness} 2>&1 | tail -200' ;;
      harness-standalone)
        echo "[check] note: <4GB available — full Docker/Postgres harness left to CI; running its standalone mode"
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
        run "${dirPrefix} test" bash -c 'cd "${dirPrefix}" && ${testCmd}' ;;
      gate-env)
        echo "[check] GATE-ENVIRONMENT: ${dirPrefix} test gate cannot run faithfully on this VM."
        echo "[check]   A bare pytest here would select the in-memory SQLite fallback (no Postgres test env, no Docker for the sanctioned harness), where pre-existing Postgres-only tests fail regardless of your diff."
        echo "[check]   Skipping the unfaithful run instead of blocking on an environment artifact. This is NOT a verdict on your change:"
        echo "[check]   - Validate the diff where the suite CAN faithfully run (the project's Docker/Postgres harness, or targeted single-file runs) and record that evidence in the PR body."
        echo "[check]   - CI runs the authoritative Postgres suite on the PR after ./pr open."
        echo "[check]   - If you have NO faithful test evidence for a test-relevant change, call ./blocked \\"<reason>\\" --needs-capability \\"postgres test environment\\" instead of shipping blind."
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
