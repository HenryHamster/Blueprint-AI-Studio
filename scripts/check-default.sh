#!/bin/bash
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
WORKSPACE="${WORKSPACE:-/tmp/workspace}"
_VERIFY_TASK_ID="${TASK_ID:-local-developer}"
_VERIFY_RECEIPT="/tmp/project_verification_receipt_${_VERIFY_TASK_ID}.json"
_VERIFY_HARNESS_RECEIPT="/tmp/backend_verification_harness_${_VERIFY_TASK_ID}.json"
_VERIFY_CAPABILITY_RECEIPT="/tmp/execution_capability_receipt_${_VERIFY_TASK_ID}"
if ! rm -f "$_VERIFY_RECEIPT" "$_VERIFY_HARNESS_RECEIPT" "$_VERIFY_CAPABILITY_RECEIPT"; then
  echo "[check] unable to remove prior verification receipt(s) — refusing to run" >&2
  exit 1
fi
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

# Determine changed paths: committed since base + uncommitted. The base diff
# FAILS CLOSED when it cannot be computed — an empty CHANGED must only ever
# mean "the diff was computed and genuinely no files changed", never "we
# could not tell". A truncated/shallow clone is the NORMAL case in CI and on
# task VMs, so the old `git diff ... 2>/dev/null || true` swallowed the exact
# error that should have red-lit the gate: the empty changed-set made every
# downstream stack block skip and the run printed PASS with zero checks.
BASE="automain"
BASE_DIFF_OK=0
COMMITTED=""
_base_diff_error=""
# Try the candidate base refs in order; COMMITTED is set by the first ref whose
# diff computes (possibly to the empty string = genuine no-op). Returns 0 when
# the committed diff is known, 1 when it cannot be computed from any ref.
_try_base_diff() {
  local ref
  _base_diff_error=""
  for ref in "origin/$BASE" "$BASE"; do
    if ! git rev-parse --verify "$ref" >/dev/null 2>&1; then
      continue
    fi
    if ! COMMITTED=$(git diff --name-only "$ref...HEAD" 2>&1); then
      _base_diff_error="git diff --name-only $ref...HEAD failed"
      continue
    fi
    return 0
  done
  return 1
}

if ! _try_base_diff; then
  # Base diff uncomputable. A truncated clone is the common cause, so try to
  # repair it (deepen a shallow repo, then fetch the base ref) before failing
  # closed — but never swallow the failure and report a green run.
  _repaired=0
  if git remote get-url origin >/dev/null 2>&1; then
    GIT_TERMINAL_PROMPT=0 git fetch --deepen=200 origin "$BASE" >/dev/null 2>&1 || true
    GIT_TERMINAL_PROMPT=0 git fetch --unshallow origin >/dev/null 2>&1 || true
    GIT_TERMINAL_PROMPT=0 git fetch origin "$BASE" >/dev/null 2>&1 || true
    if _try_base_diff; then
      _repaired=1
    fi
  fi
  if [ "$_repaired" -ne 1 ]; then
    echo "[check] ERROR: cannot compute the committed diff against base ref '$BASE'." >&2
    echo "[check]   ${_base_diff_error:-neither origin/$BASE nor $BASE resolves in this clone}." >&2
    echo "[check]   The changed-file set is the scope for every gate below; a green run with" >&2
    echo "[check]   an uncomputable diff would report PASS without checking anything." >&2
    echo "[check]   Repair the clone and re-run:" >&2
    echo "[check]     git fetch origin $BASE" >&2
    echo "[check]     git fetch --unshallow   # depth-limited clones: fetch full history" >&2
    exit 2
  fi
fi
BASE_DIFF_OK=1
UNCOMMITTED=$(git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null)
CHANGED=$(printf '%s\n%s\n' "$COMMITTED" "$UNCOMMITTED" | sort -u | grep -v '^$' || true)

touches() { printf '%s\n' "$CHANGED" | grep -q "^$1"; }

admin_ui_runtime_touched() {
  printf '%s\n' "$CHANGED" | grep -Eq '^(admin-ui/src/|admin-ui/(package\.json|package-lock\.json|vite\.config\.ts|vitest(\.viewport)?\.config\.ts|eslint\.config\.js|eslint-rules/))'
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

source scripts/backend-test-route.sh

# The database-free release guard corpus is part of the test gate. The helper
# compares static-test failures with the base tree and uses the existing
# no-Postgres backend harness; it never invokes --fix or changes the cap ledger.
# Keep lint/build-only checks fast: only ./check test pays for this release
# guard. That is the local merge-test path used by ./pr open --with-tests and by
# the mandatory backend publication gate.
if [ "$WITH_TESTS" = "1" ]; then
  if [ -f scripts/static_release_guard.py ]; then
    run "static release guards (task diff)" python3 scripts/static_release_guard.py --base "$BASE" --repo-root "$PWD"
  else
    echo "[check] static release guard helper is missing — refusing to skip the gate"
    FAILED+=("static release guard helper missing")
  fi
else
  echo "[check] static release guard deferred — run './check test' to execute it"
fi

# Idempotent npm install guarded by package-lock IDENTITY, not the mere
# existence of node_modules. A warm/reused VM carries a populated
# node_modules from a prior task; a bare `[ -d node_modules ] || npm ci`
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
# CHANGED lockfile being ignored, and `git clean` at role handoff already
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
  # Write the sentinel ONLY after a zero-exit `npm ci`, so a killed/partial
  # install never leaves a false-positive hash that suppresses a real retry.
  # Recompute `want` post-install in case the lockfile was (re)written.
  want=$(sha256sum package-lock.json 2>/dev/null | cut -d' ' -f1)
  [ -n "$want" ] && printf '%s\n' "$want" > "$sentinel"
}

# Config-driven checks: project supplied a build_config, so run its
# per-directory install/lint/build commands (tests under `./check test`)
# for changed directories. Mirrors runProjectLintChecks in index.ts.
if touches "./"; then
  run ". install" bash -c 'cd "." && npm ci'
  if [ "$FULL_SCOPE" = "1" ]; then
    run ". lint" bash -c 'cd "." && npx eslint .'
  else
    : > /tmp/check_._lint.txt
    printf '%s\n' "$CHANGED" | grep -E '^./.*\.(ts|tsx|js|jsx|cjs|mjs)$' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\0' "${f#./}" >> /tmp/check_._lint.txt
    done
    if [ -s /tmp/check_._lint.txt ]; then
      run ". lint (changed)" bash -c 'cd "." && xargs -0 -a /tmp/check_._lint.txt npx eslint 2>&1'
    else
      echo "[check] no changed JS/TS files in ., skipping lint"
    fi
  fi
  run ". build" bash -c 'cd "." && npm run build'
  if [ "$WITH_TESTS" = "1" ]; then
    run ". test" bash -c 'cd "." && npm test'
  fi
fi

if touches "admin-ui/"; then
  run "admin-ui install" bash -c 'cd "admin-ui" && npm ci'
  if [ "$FULL_SCOPE" = "1" ]; then
    run "admin-ui lint" bash -c 'cd "admin-ui" && npx eslint .'
  else
    : > /tmp/check_admin-ui_lint.txt
    printf '%s\n' "$CHANGED" | grep -E '^admin-ui/.*\.(ts|tsx|js|jsx|cjs|mjs)$' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\0' "${f#admin-ui/}" >> /tmp/check_admin-ui_lint.txt
    done
    if [ -s /tmp/check_admin-ui_lint.txt ]; then
      run "admin-ui lint (changed)" bash -c 'cd "admin-ui" && xargs -0 -a /tmp/check_admin-ui_lint.txt npx eslint 2>&1'
    else
      echo "[check] no changed JS/TS files in admin-ui, skipping lint"
    fi
  fi
  # Use typecheck-only on agent VMs: vite build is OOM-killed (exit 137) under
  # memory cgroup constraints. tsc -b --noEmit catches all type errors; the
  # full vite bundle runs in CI after ./pr open.
  run "admin-ui typecheck" bash -c 'cd "admin-ui" && npm run typecheck'
  if [ "$WITH_TESTS" = "1" ]; then
    run "admin-ui vitest (attributed)" python3 scripts/vitest_verification_guard.py --suite admin-ui --base "$BASE" --repo-root "$WORKSPACE"
  fi
fi

if touches "agent/"; then
  run "agent install" bash -c 'cd "agent" && npm ci'
  if [ "$FULL_SCOPE" = "1" ]; then
    run "agent lint" bash -c 'cd "agent" && npx eslint .'
  else
    : > /tmp/check_agent_lint.txt
    printf '%s\n' "$CHANGED" | grep -E '^agent/.*\.(ts|tsx|js|jsx|cjs|mjs)$' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\0' "${f#agent/}" >> /tmp/check_agent_lint.txt
    done
    if [ -s /tmp/check_agent_lint.txt ]; then
      run "agent lint (changed)" bash -c 'cd "agent" && xargs -0 -a /tmp/check_agent_lint.txt npx eslint 2>&1'
    else
      echo "[check] no changed JS/TS files in agent, skipping lint"
    fi
  fi
  run "agent build" bash -c 'cd "agent" && npm run build'
  if [ "$WITH_TESTS" = "1" ]; then
    run "agent vitest (attributed)" python3 scripts/vitest_verification_guard.py --suite agent --base "$BASE" --repo-root "$WORKSPACE"
  fi
fi

if touches "backend/"; then
  if [ "$FULL_SCOPE" = "1" ]; then
    run "backend lint" bash -c 'cd "backend" && ruff check .'
  else
    : > /tmp/check_backend_lint.txt
    printf '%s\n' "$CHANGED" | grep -E '^backend/.*\.py$' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\0' "${f#backend/}" >> /tmp/check_backend_lint.txt
    done
    if [ -s /tmp/check_backend_lint.txt ]; then
      run "backend lint (changed)" bash -c 'cd "backend" && xargs -0 -a /tmp/check_backend_lint.txt python3 -m ruff check --select E,F,W --ignore E501 2>&1'
    else
      echo "[check] no changed Python files in backend, skipping lint"
    fi
  fi
  if [ -n "$(printf '%s\n' "$CHANGED" | while IFS= read -r f; do case "$f" in backend/*.py) [ -f "$f" ] && printf x && break ;; esac; done)" ]; then
    # required backend-authoring verification. No skip can mint this receipt.
    _VERIFICATION_ROUTE_HELPER_OK=0
    unset -f select_backend_test_route 2>/dev/null || true
    if [ -r scripts/backend-test-route.sh ] \
      && bash -n scripts/backend-test-route.sh >/dev/null 2>&1 \
      && source scripts/backend-test-route.sh \
      && declare -F select_backend_test_route >/dev/null 2>&1; then
      _VERIFICATION_ROUTE_HELPER_OK=1
    else
      echo "[check] required backend verification FAILED — route helper unavailable or invalid"
      FAILED+=("backend verification route helper")
    fi
    _VERIFICATION_DIR='backend'
    _VERIFICATION_HARNESS='scripts/test-backend.sh'
    _VERIFICATION_CHANGED=$(mktemp /tmp/check_verification_changed.XXXXXX)
    : >"$_VERIFICATION_CHANGED"
    printf '%s\n' "$CHANGED" | grep -E '^backend/.*\.py$' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\0' "$f" >>"$_VERIFICATION_CHANGED"
    done
    _VERIFICATION_RUFF_OK=0
    if [ ! -s "$_VERIFICATION_CHANGED" ]; then
      echo "[check] required backend verification FAILED — no changed Python file in $_VERIFICATION_DIR"
      FAILED+=("$_VERIFICATION_DIR verification: no Python target")
    else
      echo "[check] $_VERIFICATION_DIR required ruff..."
      if xargs -0 -a "$_VERIFICATION_CHANGED" python3 -m ruff check --select E,F,W --ignore E501 2>&1; then
        echo "[check] $_VERIFICATION_DIR required ruff OK"
        _VERIFICATION_RUFF_OK=1
      else
        echo "[check] $_VERIFICATION_DIR required ruff FAILED"
        FAILED+=("$_VERIFICATION_DIR required ruff")
      fi
    fi
    _VERIFICATION_SELECTORS=""
    if [ "$_VERIFICATION_RUFF_OK" = "1" ]; then
      _verification_stderr=$(mktemp /tmp/check_verification_resolver.XXXXXX)
      _verification_candidates=$(mktemp /tmp/check_verification_selectors.XXXXXX)
      : >"$_verification_candidates"
      printf '%s\n' "$CHANGED" | grep -E '^backend/(testing|tests)/test_.*\.py$' | while IFS= read -r test_path; do
        [ -f "${test_path%%::*}" ] && printf '%s\n' "$test_path"
      done >>"$_verification_candidates" || true
      if [ "$_VERIFICATION_DIR" = "backend" ] && [ -f backend/testing/impacted_tests.py ]; then
        printf '%s\n' "$CHANGED" | python3 backend/testing/impacted_tests.py \
          >>"$_verification_candidates" 2>"$_verification_stderr"
        _verification_resolver_rc=$?
      else
        _verification_resolver_rc=0
        while IFS= read -r changed_file; do
          [ -n "$changed_file" ] || continue
          case "$changed_file" in
            */test_*.py|*/tests.py) printf '%s\n' "${changed_file#$_VERIFICATION_DIR/}" ;;
            *.py)
              _verification_base=$(basename "$changed_file" .py)
              for candidate in \
                "$_VERIFICATION_DIR/testing/test_${_verification_base}.py" \
                "$_VERIFICATION_DIR/tests/test_${_verification_base}.py"; do
                [ -f "$candidate" ] && printf '%s\n' "${candidate#$_VERIFICATION_DIR/}"
              done
              ;;
          esac
        done < <(printf '%s\n' "$CHANGED" | grep -E '^backend/.*\.py$') \
          >>"$_verification_candidates"
      fi
      if [ "$_verification_resolver_rc" -ne 0 ]; then
        echo "[check] required backend verification resolver FAILED (exit $_verification_resolver_rc)" >&2
        cat "$_verification_stderr" >&2
        FAILED+=("$_VERIFICATION_DIR verification resolver")
      fi
      _VERIFICATION_SELECTORS=$(
        sed 's|^backend/||' "$_verification_candidates" \
          | while IFS= read -r selector; do
              [ -n "$selector" ] || continue
              [ -f "$_VERIFICATION_DIR/${selector%%::*}" ] && printf '%s\n' "$selector"
            done \
          | awk '!seen[$0]++'
      )
      rm -f "$_verification_candidates"
      rm -f "$_verification_stderr"
    fi
    if [ "$_VERIFICATION_RUFF_OK" = "1" ] && [ -z "$_VERIFICATION_SELECTORS" ]; then
      echo "[check] required backend verification FAILED — no impacted pytest file/node was resolved"
      FAILED+=("$_VERIFICATION_DIR verification: no impacted pytest")
    elif [ "$_VERIFICATION_RUFF_OK" = "1" ]; then
      _verification_count=$(printf '%s\n' "$_VERIFICATION_SELECTORS" | grep -c '^' || true)
      if [ "$_verification_count" -gt 8 ]; then
        echo "[check] required backend verification: $_verification_count impacted files; running the 8 most diff-relevant (the remainder are not run here)"
      fi
      mapfile -t _verification_selector_args < <(printf '%s\n' "$_VERIFICATION_SELECTORS" | head -n 8)
      _verification_selectors_json=$(printf '%s\n' "${_verification_selector_args[@]}" | python3 -c 'import json,sys; print(json.dumps([line.rstrip("\n") for line in sys.stdin if line.rstrip("\n")]))')
      _verification_postgres_flag=""
      if [ -z "$_VERIFICATION_HARNESS" ] || [ ! -f "$_VERIFICATION_HARNESS" ]; then
        echo "[check] required backend verification FAILED — sanctioned harness missing: $_VERIFICATION_HARNESS"
        FAILED+=("$_VERIFICATION_DIR verification: harness missing")
      elif [ "$_VERIFICATION_ROUTE_HELPER_OK" = "1" ]; then
        if _verification_postgres_flag="$(select_backend_test_route)"; then
          :
        else
          echo "[check] required backend verification state=runtime_unavailable — PostgreSQL runtime unavailable"
          _verification_capability_tmp="${_VERIFY_CAPABILITY_RECEIPT}.$$"
          if ( umask 077; printf 'backend_verification:runtime_unavailable:v1:%s\n' \
            "$_VERIFY_TASK_ID" >"$_verification_capability_tmp" ) \
            && mv -f "$_verification_capability_tmp" "$_VERIFY_CAPABILITY_RECEIPT"; then
            echo "[check] execution capability receipt: $_VERIFY_CAPABILITY_RECEIPT"
          else
            echo "[check] unable to record backend verification capability receipt" >&2
            rm -f "$_verification_capability_tmp" "$_VERIFY_CAPABILITY_RECEIPT"
            FAILED+=("$_VERIFICATION_DIR verification: capability receipt")
          fi
          FAILED+=("$_VERIFICATION_DIR verification: runtime_unavailable")
        fi
      fi
      if [ -n "${_verification_postgres_flag:-}" ]; then
        echo "[check] $_VERIFICATION_DIR required targeted Postgres pytest..."
        _verification_harness_receipt="$_VERIFY_HARNESS_RECEIPT"
        _verification_attribution_dir=$(mktemp -d /tmp/check_verification_attribution.XXXXXX)
        _verification_current_junit="$_verification_attribution_dir/current.xml"
        rm -f "$_verification_harness_receipt" "$_verification_current_junit"
        _verification_exit=0
        if ( set -o pipefail; BLUEPRINT_VERIFICATION_RESULT_FILE="$_verification_harness_receipt" BLUEPRINT_VERIFICATION_JUNIT="$_verification_current_junit" USE_TEST_POSTGRES=true bash "$_VERIFICATION_HARNESS" "$_verification_postgres_flag" "${_verification_selector_args[@]}" 2>&1 | tail -200 ); then
          :
        else
          _verification_exit=$?
        fi
        if [ -s "$_verification_current_junit" ]; then
          _verification_attribution=$(python3 scripts/backend_verification_guard.py \
            --base "$BASE" --repo-root "$PWD" --current-junit "$_verification_current_junit" \
            --current-exit-code "$_verification_exit" --route="$_verification_postgres_flag" \
            --harness "$_VERIFICATION_HARNESS" "${_verification_selector_args[@]}")
        else
          _verification_attribution='{"state":"execution_failure","blocking":true,"new_failures":{}}'
        fi
        echo "[check] backend verification attribution: $_verification_attribution"
        _verification_state=$(printf '%s' "$_verification_attribution" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state","attribution_unavailable"))' 2>/dev/null || printf attribution_unavailable)
        _verification_tests=$(python3 -c 'import sys,xml.etree.ElementTree as ET; print(sum(1 for _ in ET.parse(sys.argv[1]).getroot().iter("testcase")))' "$_verification_current_junit" 2>/dev/null || printf 0)
        _verification_skipped=$(python3 -c 'import sys,xml.etree.ElementTree as ET; print(sum(1 for c in ET.parse(sys.argv[1]).getroot().iter("testcase") if c.find("skipped") is not None))' "$_verification_current_junit" 2>/dev/null || printf 0)
        if [ "$_verification_tests" -gt 0 ] && [ "$_verification_skipped" -eq "$_verification_tests" ]; then
          _verification_state=skipped_test
        fi
        if [ "$_verification_state" = "passed" ] || [ "$_verification_state" = "preexisting_test_failure" ]; then
          echo "[check] $_VERIFICATION_DIR required targeted Postgres pytest OK — state=$_verification_state"
          _verification_selectors_json=$(printf '%s\n' "${_verification_selector_args[@]}" | python3 -c 'import json,sys; print(json.dumps([line.rstrip("\n") for line in sys.stdin if line.rstrip("\n")]))')
          if python3 -c 'import datetime,json,sys,xml.etree.ElementTree as ET; out,task,directory,harness,selectors,attribution,junit=sys.argv[1:]; d=json.loads(attribution); root=ET.parse(junit).getroot(); cases=list(root.iter("testcase")); skipped=sum(1 for c in cases if c.find("skipped") is not None); failed=sum(1 for c in cases if c.find("failure") is not None or c.find("error") is not None); payload={"version":1,"task_id":task,"directory":directory,"created_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),"ruff":{"command":"python3 -m ruff check --select E,F,W --ignore E501 <changed-python-files>","exit_code":0},"pytest":{"command":"USE_TEST_POSTGRES=true bash "+harness+" <impacted-selectors>","harness":harness,"selectors":json.loads(selectors),"targeted":True,"use_test_postgres":True,"postgres_sidecar":True,"state":d["state"],"current_exit_code":int(d.get("current_exit_code",0)),"tests":len(cases),"skipped":skipped,"failures":failed,"new_failures":d.get("new_failures",{}),"suppressed_failures":d.get("suppressed_failures",{}),"disappeared_failures":d.get("disappeared_failures",{}),"skipped_failures":d.get("skipped_failures",{}),"exit_code":0}}; open(out,"w",encoding="utf-8").write(json.dumps(payload,sort_keys=True)+"\n")' "$_VERIFY_RECEIPT" "$_VERIFY_TASK_ID" "$_VERIFICATION_DIR" "$_VERIFICATION_HARNESS" "$_verification_selectors_json" "$_verification_attribution" "$_verification_current_junit"; then
            echo "[check] verification receipt: $_VERIFY_RECEIPT"
          else
            echo "[check] required backend verification FAILED — could not write attribution receipt"
            FAILED+=("$_VERIFICATION_DIR verification receipt")
          fi
        else
          echo "[check] required backend verification FAILED — state=$_verification_state"
          rm -f "$_VERIFY_RECEIPT"
          FAILED+=("$_VERIFICATION_DIR verification: $_verification_state")
        fi
        rm -rf "$_verification_attribution_dir"
        rm -f "$_verification_harness_receipt"
      fi
    fi
    rm -f "$_VERIFICATION_CHANGED"
  fi
fi

if touches "golden_tasks/"; then
  printf '%s\n' "$CHANGED" | grep -E '^golden_tasks/.*\.py$' | while IFS= read -r f; do
    if [ -f "$f" ]; then
      run "golden_tasks py_compile ${f#golden_tasks/}" bash -c "python3 -m py_compile '$f' 2>&1"
    fi
  done
fi


if [ ${#FAILED[@]} -eq 0 ]; then
  echo ""
  echo "[check] PASS — all checks green"
  exit 0
else
  echo ""
  echo "[check] FAIL — ${#FAILED[@]} check(s) failed: ${FAILED[*]}"
  exit 1
fi
