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
runner_temp="${RUNNER_TEMP:-}"

if [[ ! "$attempt_timeout" =~ ^[0-9]+([.][0-9]+)?[smh]$ ]]; then
  echo "invalid viewport timeout: ${attempt_timeout}" >&2
  exit 2
fi
if [[ ! "$kill_after" =~ ^[0-9]+([.][0-9]+)?[smh]$ ]]; then
  echo "invalid viewport kill-after: ${kill_after}" >&2
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
rm -f "$completion_marker"
trap 'rm -f "$completion_marker"' EXIT INT TERM

attempt_status=1
attempt_timed_out=false

run_attempt() {
  rm -f "$completion_marker"
  BLUEPRINT_VIEWPORT_COMPLETION_MARKER="$completion_marker" \
    timeout --signal=TERM --kill-after="$kill_after" "$attempt_timeout" \
    bash -c '
      "$@"
      child_status=$?
      printf "%s\n" "$child_status" > "$BLUEPRINT_VIEWPORT_COMPLETION_MARKER" || exit 125
      exit "$child_status"
    ' _ "$@"
  outer_status=$?

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
}

run_attempt "$@"
if [[ "$attempt_timed_out" == true ]]; then
  echo "::warning::Viewport provider timed out; retrying once in a fresh container" >&2
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  run_attempt "$@"
fi

exit "$attempt_status"
