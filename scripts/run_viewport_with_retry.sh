#!/usr/bin/env bash
set -uo pipefail

if (( $# < 3 )); then
  echo "usage: $0 TIMEOUT CONTAINER_NAME COMMAND [ARG ...]" >&2
  exit 2
fi

attempt_timeout="$1"
container_name="$2"
shift 2

kill_after="${BLUEPRINT_VIEWPORT_KILL_AFTER:-30s}"
# Attempt budget, not "retry once". A single spare attempt is consumed by the
# first transient; a *second*, differently shaped transient inside that retry
# then has no recourse and reds the run. That is exactly how run 32805982004
# failed: attempt 1 lost its browser transport, the retry fired correctly, and
# attempt 2 wall-clock hung after passing 40 of 41 files. Bounded, so a
# genuinely hanging or deterministically broken suite still terminates.
max_attempts="${BLUEPRINT_VIEWPORT_MAX_ATTEMPTS:-3}"
runner_temp="${RUNNER_TEMP:-}"

if [[ ! "$attempt_timeout" =~ ^[0-9]+([.][0-9]+)?[smh]$ ]]; then
  echo "invalid viewport timeout: ${attempt_timeout}" >&2
  exit 2
fi
if [[ ! "$kill_after" =~ ^[0-9]+([.][0-9]+)?[smh]$ ]]; then
  echo "invalid viewport kill-after: ${kill_after}" >&2
  exit 2
fi
if [[ ! "$max_attempts" =~ ^[1-5]$ ]]; then
  echo "invalid viewport max attempts: ${max_attempts}" >&2
  exit 2
fi
if [[ ! "$container_name" =~ ^blueprint-viewport-[A-Za-z0-9_.-]+$ ]]; then
  echo "invalid viewport container name" >&2
  exit 2
fi
if [[ -z "$runner_temp" || ! -d "$runner_temp" ]]; then
  echo "RUNNER_TEMP must name an existing directory" >&2
  exit 2
fi

completion_marker="$(mktemp "${runner_temp%/}/blueprint-viewport-completion.XXXXXX")"
attempt_output="$(mktemp "${runner_temp%/}/blueprint-viewport-output.XXXXXX")"
# Vitest interleaves SGR escapes *inside* the summary line under CI
# (ESC[2m      Tests ESC[22m ESC[1mESC[32m264 passedESC[39m ...), so the words
# "Tests" and "264 passed" are not adjacent in the raw bytes. Classification
# always reads this normalized copy, never the raw capture.
attempt_plain="$(mktemp "${runner_temp%/}/blueprint-viewport-plain.XXXXXX")"
rm -f "$completion_marker"
trap 'rm -f "$completion_marker" "$attempt_output" "$attempt_plain"' EXIT INT TERM

ansi_escape=$'\033'

attempt_status=1
attempt_timed_out=false
attempt_classifiable=true
retry_reason=""

run_attempt() {
  rm -f "$completion_marker"
  : > "$attempt_output"
  : > "$attempt_plain"
  attempt_classifiable=true
  BLUEPRINT_VIEWPORT_COMPLETION_MARKER="$completion_marker" \
    timeout --signal=TERM --kill-after="$kill_after" "$attempt_timeout" \
    bash -c '
      "$@"
      child_status=$?
      printf "%s\n" "$child_status" > "$BLUEPRINT_VIEWPORT_COMPLETION_MARKER" || exit 125
      exit "$child_status"
    ' _ "$@" 2>&1 | tee "$attempt_output"
  pipeline_status=("${PIPESTATUS[@]}")
  outer_status="${pipeline_status[0]}"
  tee_status="${pipeline_status[1]}"

  if [[ -f "$completion_marker" ]]; then
    IFS= read -r attempt_status < "$completion_marker"
    if [[ ! "$attempt_status" =~ ^([0-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5])$ ]]; then
      echo "invalid viewport completion status" >&2
      attempt_status=125
    fi
    attempt_timed_out=false
  else
    attempt_status="$outer_status"
    if [[ "$outer_status" -eq 124 || "$outer_status" -eq 137 ]]; then
      attempt_timed_out=true
    else
      attempt_timed_out=false
    fi
  fi

  if [[ "$tee_status" -ne 0 ]]; then
    echo "::error::Could not capture viewport provider output" >&2
    attempt_status=125
    attempt_timed_out=false
    attempt_classifiable=false
    return
  fi

  if ! LC_ALL=C sed -e "s/${ansi_escape}\[[0-9;?]*[A-Za-z]//g" \
      "$attempt_output" > "$attempt_plain"; then
    # Without a normalized copy we cannot prove that no test failed, so we must
    # not retry. Fail safe, not open.
    echo "::error::Could not normalize viewport output for retry classification" >&2
    attempt_classifiable=false
  fi
}

# ---------------------------------------------------------------------------
# Retry classification
#
# A viewport attempt goes red for two categorically different reasons, and only
# ONE of them may ever be retried:
#
#   (a) A test RAN and produced a failing result - an assertion failure, an
#       exception thrown inside a test, a component that genuinely renders
#       wrong. Retrying this is precisely how a flaky product bug gets
#       laundered into a green CI run. NEVER retried, whatever else the output
#       happens to contain.
#
#   (b) A test NEVER RAN, because the harness broke underneath it: the browser
#       could not fetch a module the dev server was supposed to serve, the test
#       iframe never connected, the browser or page died, or the process
#       wall-clock hung. Nothing was asserted, so there is no result to
#       launder. Retried in a fresh container.
#
# The trap is that (a) and (b) can print the SAME client-side string. A module
# with a real syntax error or an unresolvable import ALSO surfaces in the
# browser as "TypeError: Failed to fetch dynamically imported module: ...",
# because Vite answers the module request with a 500. Keying the retry on that
# string alone would retry real compile errors - verified against vitest 4.1.10
# by planting both defects and capturing the output.
#
# What actually separates them is WHO IS TALKING:
#
#   code is broken -> the DEV SERVER announces why it refused to serve:
#                     "[vite] Internal server error: Failed to resolve import
#                     ...", "Transform failed with 1 error", "Pre-transform
#                     error" - with plugin name, file, line and a source frame.
#   transport drop -> the dev server says NOTHING at all. The only evidence is
#                     the browser's own generic fetch failure. (Confirmed on
#                     run 32805982004: zero "[vite]" lines in the entire job.)
#
# So: veto first on any evidence that a test failed or that the server stated a
# reason for refusing to serve, and only then consider the transport
# signatures. Every veto fails SAFE - no retry, red run, a human looks - which
# is the correct direction to be wrong in.
#
# The transport list is an OR, not the previous three-way AND. That AND was
# pinned to one historical signature and required all of "Failed to fetch
# dynamically imported module:", "Cannot connect to the iframe." and "unhandled
# error during the test run." to co-occur, so a disconnect that presented even
# slightly differently got no retry at all.
# ---------------------------------------------------------------------------
classify_attempt() {
  retry_reason=""

  [[ "$attempt_status" -eq 0 ]] && return
  [[ "$attempt_classifiable" == true ]] || return

  # --- Veto 1: a test executed and reported a failure. ---
  # Summary form: "      Tests  1 failed | 263 passed (264)".
  # A clean transport drop reads "Tests  264 passed (264)" or "Tests  no tests"
  # - zero failures - because nothing got far enough to assert anything.
  if grep -qE '^[[:space:]]*Tests[[:space:]]+.*[0-9]+ failed' "$attempt_plain"; then
    return
  fi
  # Per-file progress form: "|chromium| src/X.viewport.test.tsx (1 test | 1 failed)".
  # Printed DURING the run, so it still vetoes when a wall-clock kill truncates
  # the output before any summary block is written.
  if grep -qE '\([0-9]+ tests?[[:space:]]*\|[[:space:]]*[0-9]+ failed' "$attempt_plain"; then
    return
  fi
  # End-of-run section header for the same thing. ("Failed Suites" is a
  # different section and is deliberately NOT vetoed here - a suite that never
  # loaded is the (b) class, and Veto 2 decides whether the code or the
  # transport was at fault.)
  if grep -Fq 'Failed Tests' "$attempt_plain"; then
    return
  fi

  # --- Veto 2: the dev server refused to serve, and said why. ---
  # Every one of these is a real compile/resolve defect in the code under test.
  local server_refusal
  for server_refusal in \
    '[vite] Internal server error' \
    'Pre-transform error' \
    'Transform failed with' \
    'Failed to resolve import' \
    'Failed to load url'
  do
    if grep -Fq "$server_refusal" "$attempt_plain"; then
      return
    fi
  done

  # --- Retryable: harness failures in which no test produced a result. ---
  if [[ "$attempt_timed_out" == true ]]; then
    retry_reason="hit the ${attempt_timeout} wall clock with no test failure reported"
    return
  fi

  local signature
  for signature in \
    'Failed to fetch dynamically imported module:' \
    'Cannot connect to the iframe.' \
    'Browser connection was closed while running tests' \
    'Target page, context or browser has been closed' \
    'Target crashed' \
    'Page crashed' \
    'browserType.launch' \
    'net::ERR_'
  do
    if grep -Fq "$signature" "$attempt_plain"; then
      retry_reason="lost its browser transport (${signature})"
      return
    fi
  done
}

attempt=1
while :; do
  run_attempt "$@"
  classify_attempt

  if [[ -z "$retry_reason" ]]; then
    break
  fi
  if (( attempt >= max_attempts )); then
    echo "::error::Viewport ${retry_reason} on attempt ${attempt}/${max_attempts}; attempt budget exhausted" >&2
    break
  fi

  echo "::warning::Viewport attempt ${attempt}/${max_attempts} ${retry_reason}; retrying in a fresh container" >&2
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  attempt=$((attempt + 1))
done

exit "$attempt_status"
