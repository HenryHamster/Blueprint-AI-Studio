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
# Warm VMs reuse /tmp, so a prior task's per-lane suppression receipts would be
# attributed to this run. Clear them exactly as the verification receipts above.
BLUEPRINT_GUARD_SUPPRESSION_DIR="/tmp/guard_suppression_receipts_${_VERIFY_TASK_ID}"
export BLUEPRINT_GUARD_SUPPRESSION_DIR
if ! rm -f "$_VERIFY_RECEIPT" "$_VERIFY_HARNESS_RECEIPT" "$_VERIFY_CAPABILITY_RECEIPT" \
  || ! rm -rf "$BLUEPRINT_GUARD_SUPPRESSION_DIR"; then
  echo "[check] unable to remove prior verification receipt(s) — refusing to run" >&2
  exit 1
fi
# Every attributed lane merges its own structured block into this one sidecar.
# Exporting the resolved path is what keeps a guard from re-deriving the task
# identity and disagreeing with the gate that already resolved it.
BLUEPRINT_VERIFICATION_RECEIPT="$_VERIFY_RECEIPT"
export BLUEPRINT_VERIFICATION_RECEIPT
cd "$WORKSPACE" || { echo "Error: cannot cd to $WORKSPACE" >&2; exit 2; }

# Sanity: make sure we're in a git repo. Without this, the diff commands
# below silently swallow errors and CHANGED becomes empty -> all stack
# blocks skipped -> "PASS" with zero actual checks.
git rev-parse --git-dir >/dev/null 2>&1 || {
  echo "Error: $WORKSPACE is not a git repo (missing .git)" >&2
  exit 2
}

# Interpreter identity. Every guard lane below invokes `python3` by name. Git
# Bash on a Windows developer host has no `python3` — the python.org installer
# publishes `python.exe` only — so on that host this gate did not merely skip a
# lane, it failed EVERY python-backed lane with `python3: command not found`.
# That is the host where `bp/*` task branches are authored, and a gate that
# cannot execute is a gate nobody runs: it is why the four stale guard-data
# manifests behind promotion PR #17764 were never caught before automain.
# Publish a shim on PATH instead of rewriting the call sites, because
# `xargs python3` and `bash -c '... python3 ...'` resolve an executable and
# would ignore a shell function or alias.
if ! command -v python3 >/dev/null 2>&1; then
  _PY3_FALLBACK="$(command -v python || true)"
  if [ -z "$_PY3_FALLBACK" ]; then
    echo "[check] ERROR: neither python3 nor python is on PATH — every guard lane below needs one" >&2
    exit 2
  fi
  _PY3_SHIM_DIR="${TMPDIR:-/tmp}/blueprint-check-python3-shim.$$"
  if ! mkdir -p "$_PY3_SHIM_DIR" \
    || ! printf '#!/bin/sh\nexec "%s" "$@"\n' "$_PY3_FALLBACK" >"$_PY3_SHIM_DIR/python3" \
    || ! chmod +x "$_PY3_SHIM_DIR/python3"; then
    echo "[check] ERROR: cannot publish a python3 shim in $_PY3_SHIM_DIR" >&2
    exit 2
  fi
  trap 'rm -rf "$_PY3_SHIM_DIR"' EXIT
  PATH="$_PY3_SHIM_DIR:$PATH"
  export PATH
  echo "[check] python3 is absent on PATH; routing python3 to $_PY3_FALLBACK"
fi

# Interpreter ENCODING, the companion to the interpreter identity above. Python
# picks the host locale codec for print(), open() and `subprocess(text=True)`.
# On Linux and in CI that is UTF-8; on the Windows developer host it is cp1252,
# so the SAME guard behaves differently on the two hosts: it emits byte 0x97
# where CI emits an em dash (the captured lane output then fails a strict UTF-8
# decode), and it dies with UnicodeEncodeError the moment it echoes tool output
# containing a character cp1252 lacks -- the box drawing, arrows and check marks
# that pytest/tsc/vitest print. A guard that cannot print its own verdict
# reports nothing. UTF-8 mode makes every python-backed lane below, including
# the inline `python3 -c` helpers, behave the way CI already does. No-op on a
# UTF-8 host.
PYTHONUTF8=1
export PYTHONUTF8

# Install the repo's custom merge drivers into THIS clone.
#
# .gitattributes maps backend/testing/module_size_limits.json and
# backend/.test_durations to merge drivers, but git ignores that mapping unless
# `merge.<name>.driver` is registered in the repository's own config — and a
# clone does not carry git config. Until now the only installer was
# agent/src/index.ts, at agent workspace-prep time, so the FLEET had the drivers
# and a developer clone had them nowhere: on this host those two paths conflicted
# textually on every merge even though the repo ships a correct mechanical merge
# for both. This gate is the one command a developer runs before every
# iteration, which makes it the reliable place to close that asymmetry.
#
# Deliberately never fatal and never in the changed-paths scope: it is workspace
# setup, not a check, and failing to register only leaves merges behaving the way
# they behave today. Runs before the diff below so a fresh clone is configured
# even on a no-op ./check.
if ! python3 scripts/derived_artifact_merge_resolve.py --register-drivers >/dev/null 2>&1; then
  echo "[check] note: custom merge drivers not registered (merges of derived artifacts may conflict)"
fi

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

# Derived-artifact freshness, and repair of what may be repaired.
#
# The size ledger below is ONE derived artifact whose guard happens to be a
# standalone checker, so it was the only one this gate enforced
# unconditionally; every OTHER derived artifact in this repo (the websocket
# emitter inventory fixture, the hand-kept WEBSOCKET_EMITTER_INVENTORY dict, the
# conversational-Workday per-file test counts, the ranked-queue and durations
# artifacts) is guarded by a pytest drift sentinel. Stale guard data is
# self-concealing — it is attributable on exactly the one PR that introduces it
# and inherited-then-warn-passed on every PR after — so anything it slips past
# lands on the automain->main promotion matrix, which is what killed promotion
# PRs #17738 and #17764. This lane runs only the sentinels registered for the
# surfaces THIS diff touches, attributed against base, so it never blocks on
# inherited drift.
#
# `--fix` and the position matter, and both are load-bearing:
#
#  * `--fix` regenerates a drifted artifact that derived_artifact_repair
#    registers as decision-free (today: backend/testing/test-estate-ranked-queue.md, a pure
#    report with no consumer but its own guard) and then RE-RUNS the sentinel to
#    prove the drift cleared. Ratchets, floors, snapshots, caps and measured
#    durations are never regenerated. Reporting alone was not enough: three
#    hand-written repair commits landed in three days, each after a promotion PR
#    had already gone red.
#  * It runs on BOTH paths, and BEFORE the static corpus rather than after.
#    Under `./check test` the corpus runs the whole roster, so a repair that
#    happened after it would be graded against the pre-repair tree and still
#    fail the gate. Ordering it first costs one scoped harness run and makes the
#    corpus see the tree the PR will actually carry.
if [ -f scripts/sentinel_freshness.py ]; then
  run "derived-artifact freshness (registered drift sentinels)" \
    python3 scripts/sentinel_freshness.py --fix --base "$BASE" --repo-root "$PWD"
else
  echo "[check] derived-artifact freshness helper is missing — refusing to skip the gate"
  FAILED+=("sentinel freshness helper missing")
fi

# The database-free size guard is part of every publication gate. The
# baseline-aware size-only path covers all five governed sections and never
# invokes --fix or changes the cap ledger. ./check test additionally runs the
# attributed static corpus through the same helper.
if [ -f scripts/static_release_guard.py ]; then
  if [ "$WITH_TESTS" = "1" ]; then
    run "static release guards (task diff)" python3 scripts/static_release_guard.py --base "$BASE" --repo-root "$PWD"
  else
    run "static size guards (task diff)" python3 scripts/static_release_guard.py --size-only --base "$BASE" --repo-root "$PWD"
  fi
else
  echo "[check] static release guard helper is missing — refusing to skip the gate"
  FAILED+=("static release guard helper missing")
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
  # full vite bundle runs in CI after ./pr open. The build half is attributed
  # against an isolated base checkout through the shared base-attribution guard,
  # so inherited type errors warn-pass while newly introduced ones still block;
  # unavailable attribution is fail-closed. The guard also compiles the
  # test-file project against the base ref, so this is the complete typecheck
  # lane.
  run "admin-ui typecheck (attributed)" python3 scripts/typecheck_verification_guard.py --suite admin-ui --base "$BASE" --repo-root "$WORKSPACE"
  # The viewport-coverage invariants are NOT run as a separate step here:
  # admin-ui/src/test/viewport-coverage.test.ts asserts all five and is part of
  # the unit config's include, so the attributed vitest lane below covers them.
  if [ "$WITH_TESTS" = "1" ]; then
    run "admin-ui vitest (attributed)" python3 scripts/vitest_verification_guard.py --suite admin-ui --base "$BASE" --repo-root "$WORKSPACE"
  fi
  # NOTE: the browser-mode `--suite admin-ui-viewport` lane deliberately does
  # NOT run here. This default gate is the true-developer-checkout path and has
  # no Playwright Chromium bootstrap and no VIEWPORT_CAPTURE_SKIPPED escape, so
  # arming a fail-closed browser lane would hard-block every developer without
  # a downloaded browser. The generated task gate (which does bootstrap
  # Chromium) and CI's frontend-viewport job own browser verification.
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
  # agent's npm run build IS tsc --noEmit; route it through the same guard.
  run "agent tsc (attributed)" python3 scripts/typecheck_verification_guard.py --suite agent --base "$BASE" --repo-root "$WORKSPACE"
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
    _VERIFICATION_DIR='backend'
    _VERIFICATION_HARNESS='scripts/test-backend.sh'
    _record_backend_abort() {
      local state="$1" reason="$2"
      if [ -r scripts/verification_receipt.py ]; then
        python3 scripts/verification_receipt.py backend-pytest-diagnosis \
          --receipt "$_VERIFY_RECEIPT" --task-id "$_VERIFY_TASK_ID" \
          --directory "$_VERIFICATION_DIR" --state "$state" --reason "$reason" \
          --current-exit-code 1 >/dev/null 2>&1 || true
      elif python3 -c 'import json,sys; out,task,directory,state,reason,code=sys.argv[1:];
try:
    payload=json.load(open(out,encoding="utf-8"))
except (OSError,ValueError):
    payload={}
lanes=payload.get("lanes") if isinstance(payload.get("lanes"),dict) else {}
lanes["backend-pytest"]={"lane":"backend-pytest","state":state,"blocking":True,"current_exit_code":int(code),"error":reason,"new_failures":{},"suppressed_failures":{},"disappeared_failures":{},"skipped_failures":{}}
payload["version"]=1; payload["lanes"]=lanes
open(out,"w",encoding="utf-8").write(json.dumps(payload,sort_keys=True)+"\n")' \
          "$_VERIFY_RECEIPT" "$_VERIFY_TASK_ID" "$_VERIFICATION_DIR" "$state" "$reason" 1; then
        :
      fi
    }
    _VERIFICATION_ROUTE_HELPER_OK=0
    unset -f select_backend_test_route 2>/dev/null || true
    if [ -r scripts/backend-test-route.sh ] \
      && bash -n scripts/backend-test-route.sh >/dev/null 2>&1 \
      && source scripts/backend-test-route.sh \
      && declare -F select_backend_test_route >/dev/null 2>&1; then
      _VERIFICATION_ROUTE_HELPER_OK=1
    else
      echo "[check] required backend verification FAILED — route helper unavailable or invalid"
      _record_backend_abort route_helper_unavailable "route helper unavailable or invalid"
      FAILED+=("backend verification route helper")
    fi
    _VERIFICATION_CHANGED=$(mktemp /tmp/check_verification_changed.XXXXXX)
    : >"$_VERIFICATION_CHANGED"
    printf '%s\n' "$CHANGED" | grep -E '^backend/.*\.py$' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\0' "$f" >>"$_VERIFICATION_CHANGED"
    done
    _VERIFICATION_RUFF_OK=0
    if [ ! -s "$_VERIFICATION_CHANGED" ]; then
      echo "[check] required backend verification FAILED — no changed Python file in $_VERIFICATION_DIR"
      _record_backend_abort target_unavailable "no changed Python file in $_VERIFICATION_DIR"
      FAILED+=("$_VERIFICATION_DIR verification: no Python target")
    else
      echo "[check] $_VERIFICATION_DIR required ruff..."
      if xargs -0 -a "$_VERIFICATION_CHANGED" python3 -m ruff check --select E,F,W --ignore E501 2>&1; then
        echo "[check] $_VERIFICATION_DIR required ruff OK"
        _VERIFICATION_RUFF_OK=1
      else
        echo "[check] $_VERIFICATION_DIR required ruff FAILED"
        _record_backend_abort lint_failure "required ruff failed"
        FAILED+=("$_VERIFICATION_DIR required ruff")
      fi
    fi
    _VERIFICATION_SELECTORS=""
    if [ "$_VERIFICATION_RUFF_OK" = "1" ]; then
      _verification_stderr=$(mktemp /tmp/check_verification_resolver.XXXXXX)
      _verification_candidates=$(mktemp /tmp/check_verification_selectors.XXXXXX)
      _verification_mandatory_candidates=$(mktemp /tmp/check_verification_mandatory.XXXXXX)
      : >"$_verification_candidates"
      : >"$_verification_mandatory_candidates"
      if [ "$_VERIFICATION_DIR" = "backend" ] && [ -f backend/testing/impacted_tests.py ]; then
        printf '%s\n' "$CHANGED" | python3 backend/testing/impacted_tests.py \
          --limit 8 --mandatory-output "$_verification_mandatory_candidates" \
          >>"$_verification_candidates" 2>"$_verification_stderr"
        _verification_resolver_rc=$?
      else
        _verification_resolver_rc=0
        printf '%s\n' "$CHANGED" | grep -E '^backend/(testing|tests)/test_.*\.py$' | while IFS= read -r test_path; do
          [ -f "${test_path%%::*}" ] && printf '%s\n' "$test_path"
        done >>"$_verification_candidates" || true
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
        _record_backend_abort resolver_failure "backend verification resolver failed (exit $_verification_resolver_rc)"
        FAILED+=("$_VERIFICATION_DIR verification resolver")
      fi
      _VERIFICATION_ORDINARY_SELECTORS=$(
        sed 's|^backend/||' "$_verification_candidates" \
          | while IFS= read -r selector; do
              [ -n "$selector" ] || continue
              [ -f "$_VERIFICATION_DIR/${selector%%::*}" ] && printf '%s\n' "$selector"
            done \
          | awk '!seen[$0]++'
      )
      _VERIFICATION_MANDATORY_SELECTORS=$(
        sed 's|^backend/||' "$_verification_mandatory_candidates" \
          | while IFS= read -r selector; do
              [ -n "$selector" ] || continue
              [ -f "$_VERIFICATION_DIR/${selector%%::*}" ] && printf '%s\n' "$selector"
            done \
          | awk '!seen[$0]++'
      )
      _VERIFICATION_SELECTORS=$(printf '%s\n%s\n' \
        "$_VERIFICATION_ORDINARY_SELECTORS" "$_VERIFICATION_MANDATORY_SELECTORS" \
        | awk 'NF && !seen[$0]++')
      rm -f "$_verification_candidates"
      rm -f "$_verification_mandatory_candidates"
      rm -f "$_verification_stderr"
    fi
    if [ "$_VERIFICATION_RUFF_OK" = "1" ] && [ -z "$_VERIFICATION_SELECTORS" ]; then
      echo "[check] required backend verification FAILED — no impacted pytest file/node was resolved"
      _record_backend_abort selector_unavailable "no impacted pytest file/node was resolved"
      FAILED+=("$_VERIFICATION_DIR verification: no impacted pytest")
    elif [ "$_VERIFICATION_RUFF_OK" = "1" ]; then
      _verification_ordinary_count=$(printf '%s\n' "$_VERIFICATION_ORDINARY_SELECTORS" | grep -c . || true)
      _verification_mandatory_count=$(printf '%s\n' "$_VERIFICATION_MANDATORY_SELECTORS" | grep -c . || true)
      if [ "$_verification_ordinary_count" -gt 8 ]; then
        echo "[check] required backend verification FAILED — resolver exceeded ordinary selector cap: $_verification_ordinary_count"
        _record_backend_abort selector_cap_exceeded "resolver exceeded ordinary selector cap: $_verification_ordinary_count"
        FAILED+=("$_VERIFICATION_DIR verification: ordinary selector cap")
      fi
      if [ "$_verification_mandatory_count" -gt 0 ]; then
        echo "[check] required backend verification: retaining $_verification_mandatory_count matched surface sentinel(s) past the ordinary cap"
      fi
      mapfile -t _verification_selector_args < <(printf '%s\n' "$_VERIFICATION_SELECTORS")
      mapfile -t _verification_mandatory_selector_args < <(printf '%s\n' "$_VERIFICATION_MANDATORY_SELECTORS")
      _verification_selectors_json=$(printf '%s\n' "${_verification_selector_args[@]}" | python3 -c 'import json,sys; print(json.dumps([line.rstrip("\n") for line in sys.stdin if line.rstrip("\n")]))')
      _verification_mandatory_selectors_encoded=""
      if [ "$_verification_mandatory_count" -gt 0 ]; then
        _verification_mandatory_selectors_encoded=$(printf '%s\n' "${_verification_mandatory_selector_args[@]}" | base64 | tr -d '\n')
      fi
      _verification_postgres_flag=""
      if [ -z "$_VERIFICATION_HARNESS" ] || [ ! -f "$_VERIFICATION_HARNESS" ]; then
        echo "[check] required backend verification FAILED — sanctioned harness missing: $_VERIFICATION_HARNESS"
        _record_backend_abort harness_unavailable "sanctioned harness missing: $_VERIFICATION_HARNESS"
        FAILED+=("$_VERIFICATION_DIR verification: harness missing")
      elif [ "$_VERIFICATION_ROUTE_HELPER_OK" = "1" ]; then
        if _verification_postgres_flag="$(select_backend_test_route)"; then
          :
        else
          echo "[check] required backend verification state=runtime_unavailable — PostgreSQL runtime unavailable"
          _record_backend_abort runtime_unavailable "PostgreSQL runtime unavailable"
          _verification_capability_tmp="${_VERIFY_CAPABILITY_RECEIPT}.$$"
          if ( umask 077; printf 'backend_verification:runtime_unavailable:v1:%s\n' \
            "$_VERIFY_TASK_ID" >"$_verification_capability_tmp" ) \
            && mv -f "$_verification_capability_tmp" "$_VERIFY_CAPABILITY_RECEIPT"; then
            echo "[check] execution capability receipt: $_VERIFY_CAPABILITY_RECEIPT"
          else
            echo "[check] unable to record backend verification capability receipt" >&2
            rm -f "$_verification_capability_tmp" "$_VERIFY_CAPABILITY_RECEIPT"
            _record_backend_abort capability_receipt_failure "unable to record backend verification capability receipt"
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
        if ( set -o pipefail; BLUEPRINT_VERIFICATION_RESULT_FILE="$_verification_harness_receipt" BLUEPRINT_VERIFICATION_JUNIT="$_verification_current_junit" BLUEPRINT_MANDATORY_VERIFICATION_SELECTORS="$_verification_mandatory_selectors_encoded" USE_TEST_POSTGRES=true bash "$_VERIFICATION_HARNESS" "$_verification_postgres_flag" "${_verification_selector_args[@]}" 2>&1 | tail -200 ); then
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
          _verification_attribution=$(printf '{"state":"attribution_unavailable","blocking":true,"current_exit_code":%s,"error":"current JUnit report missing after targeted harness"}' "$_verification_exit")
        fi
        echo "[check] backend verification attribution: $_verification_attribution"
        # A run with many suppressed baseline failures (each carrying a full
        # traceback) can make this blob large enough to blow the OS argv-size
        # limit (E2BIG / "Argument list too long") if passed as a literal CLI
        # argument below. Persist it to a file and pass the PATH instead so
        # receipt-writing scales with suppressed-failure count, not argv size.
        _verification_attribution_file="$_verification_attribution_dir/attribution.json"
        printf '%s' "$_verification_attribution" >"$_verification_attribution_file"
        _verification_state=$(printf '%s' "$_verification_attribution" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state","attribution_unavailable"))' 2>/dev/null || printf attribution_unavailable)
        _verification_blocking=$(printf '%s' "$_verification_attribution" | python3 -c 'import json,sys; print("1" if json.load(sys.stdin).get("blocking", True) else "0")' 2>/dev/null || printf 1)
        _verification_tests=$(python3 -c 'import sys,xml.etree.ElementTree as ET; print(sum(1 for _ in ET.parse(sys.argv[1]).getroot().iter("testcase")))' "$_verification_current_junit" 2>/dev/null || printf 0)
        _verification_skipped=$(python3 -c 'import sys,xml.etree.ElementTree as ET; print(sum(1 for c in ET.parse(sys.argv[1]).getroot().iter("testcase") if c.find("skipped") is not None))' "$_verification_current_junit" 2>/dev/null || printf 0)
        if [ "$_verification_tests" -gt 0 ] && [ "$_verification_skipped" -eq "$_verification_tests" ]; then
          _verification_state=skipped_test
          _verification_blocking=1
          _verification_attribution=$(printf '%s' "$_verification_attribution" | python3 -c 'import json,sys; d=json.load(sys.stdin); d["state"]="skipped_test"; d["blocking"]=True; print(json.dumps(d,sort_keys=True))')
          printf '%s' "$_verification_attribution" >"$_verification_attribution_file"
        fi
        _verification_selectors_json=$(printf '%s\n' "${_verification_selector_args[@]}" | python3 -c 'import json,sys; print(json.dumps([line.rstrip("\n") for line in sys.stdin if line.rstrip("\n")]))')
        # Prefer the Blueprint-repo-local writer (it merges this lane into the
        # multi-lane sidecar), but this fragment is emitted for ANY project whose
        # build_config declares the blueprint-v1 verification protocol, and a
        # foreign workspace has no scripts/verification_receipt.py. Fall back to
        # the self-contained inline writer there rather than failing closed and
        # blocking every ./check in that project. Both read the attribution from a
        # FILE: a run with many suppressed baseline failures (each carrying a full
        # traceback) would otherwise blow the OS argv-size limit (E2BIG).
        # The sidecar is diagnostic evidence, not a pass credential. Write it
        # on both branches; a blocking lane is marked non-publishable by the
        # writer and the publication gate still fails below.
        _verification_receipt_written=0
        if python3 scripts/verification_receipt.py backend-pytest --receipt "$_VERIFY_RECEIPT" --task-id "$_VERIFY_TASK_ID" --directory "$_VERIFICATION_DIR" --harness "$_VERIFICATION_HARNESS" --selectors-json "$_verification_selectors_json" --attribution-file "$_verification_attribution_file" --junit "$_verification_current_junit"; then
          _verification_receipt_written=1
        fi
        if [ "$_verification_receipt_written" = "1" ]; then
          echo "[check] verification receipt: $_VERIFY_RECEIPT"
        else
          echo "[check] required backend verification FAILED — could not write attribution receipt"
          FAILED+=("$_VERIFICATION_DIR verification receipt")
        fi
        if [ "$_verification_blocking" != "0" ]; then
          echo "[check] required backend verification FAILED — state=$_verification_state"
          FAILED+=("$_VERIFICATION_DIR verification: $_verification_state")
        else
          echo "[check] $_VERIFICATION_DIR required targeted Postgres pytest OK — state=$_verification_state"
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


# Merge every lane's suppression receipt into one cross-lane aggregate. Purely
# additive bookkeeping: it never contributes to FAILED.
python3 scripts/guard_suppression_receipt.py aggregate \
  --out "$BLUEPRINT_GUARD_SUPPRESSION_DIR/aggregate.json" || true

if [ ${#FAILED[@]} -eq 0 ]; then
  echo ""
  echo "[check] PASS — all checks green"
  exit 0
else
  echo ""
  echo "[check] FAIL — ${#FAILED[@]} check(s) failed: ${FAILED[*]}"
  exit 1
fi
