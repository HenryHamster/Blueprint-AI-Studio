#!/usr/bin/env bash
set -euo pipefail

# Reclaim only anonymous, unreferenced volumes from the four dedicated Actions
# Docker daemons.  Freezing the runner unit closes the job-assignment race:
# an existing job exposes Runner.Worker and is skipped, while a new job cannot
# start until the listener is thawed.
readonly STATE_ROOT="${BLUEPRINT_ACTIONS_STATE_ROOT:-/var/lib/blueprint-actions}"
readonly SOCKET_ROOT="${BLUEPRINT_ACTIONS_SOCKET_ROOT:-/run/blueprint-actions}"
readonly CGROUP_ROOT="${BLUEPRINT_ACTIONS_CGROUP_ROOT:-/sys/fs/cgroup}"
readonly LOCK_FILE="${BLUEPRINT_ACTIONS_GC_LOCK_FILE:-/run/lock/blueprint-actions-volume-gc.lock}"
readonly OWNERSHIP_ROOT="${BLUEPRINT_ACTIONS_GC_OWNERSHIP_ROOT:-/run/blueprint-actions-volume-gc}"
readonly PRESSURE_PERCENT=70
readonly MIN_FREE_BYTES=$((30 * 1024 * 1024 * 1024))
readonly WARN_PERCENT=75
readonly WARN_FREE_BYTES=$((20 * 1024 * 1024 * 1024))
readonly CRITICAL_PERCENT=85
readonly CRITICAL_FREE_BYTES=$((10 * 1024 * 1024 * 1024))

frozen_unit=""
frozen_marker=""

log() {
  printf 'blueprint_actions_volume_gc %s\n' "$*"
}

thaw_current() {
  if [[ -n "$frozen_unit" ]]; then
    if ! systemctl thaw "$frozen_unit"; then
      log "severity=critical event=thaw_failed unit=$frozen_unit"
      return 1
    fi
    if [[ -n "$frozen_marker" ]]; then
      rm --force -- "$frozen_marker"
    fi
    frozen_unit=""
    frozen_marker=""
  fi
}

terminate() {
  local status="$1"
  trap - EXIT INT TERM HUP
  thaw_current || status=1
  exit "$status"
}

trap 'thaw_current' EXIT
trap 'terminate 130' INT
trap 'terminate 143' TERM
trap 'terminate 129' HUP

docker_for_slot() {
  local slot="$1"
  shift
  timeout --signal=TERM --kill-after=5s 30s \
    docker --host "unix://$SOCKET_ROOT/$slot/docker.sock" "$@"
}

filesystem_state() {
  local blocks available percent
  read -r blocks available percent < <(
    df --block-size=1 --output=size,avail,pcent "$STATE_ROOT" | \
      awk 'NR == 2 {gsub(/%/, "", $3); print $1, $2, $3}'
  )
  [[ "$blocks" =~ ^[0-9]+$ && "$available" =~ ^[0-9]+$ && "$percent" =~ ^[0-9]+$ ]] || {
    log "severity=critical event=filesystem_state_unreadable"
    return 1
  }
  printf '%s %s %s\n' "$blocks" "$available" "$percent"
}

runner_has_worker() {
  local slot="$1" unit="$2" cgroup expected_cgroup pid comm
  cgroup="$(systemctl show "$unit" --property=ControlGroup --value)"
  expected_cgroup="/system.slice/system-blueprint\\x2dgithub\\x2dactions.slice/blueprint-github-actions@$slot.service"
  [[ "$cgroup" == "$expected_cgroup" ]] || return 2
  [[ -d "$CGROUP_ROOT$cgroup" ]] || return 2

  while IFS= read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] || return 2
    [[ -r "/proc/$pid/comm" ]] || return 2
    IFS= read -r comm < "/proc/$pid/comm" || return 2
    [[ "$comm" != "Runner.Worker" ]] || return 0
  done < <(find "$CGROUP_ROOT$cgroup" -name cgroup.procs -type f -exec cat {} +)
  return 1
}

exec 9>"$LOCK_FILE"
if ! flock --nonblock 9; then
  log "event=skip reason=lock_busy"
  exit 0
fi

install -d -m 0700 -o root -g root "$OWNERSHIP_ROOT"

read -r before_total before_free before_percent < <(filesystem_state)
if (( before_percent < PRESSURE_PERCENT && before_free >= MIN_FREE_BYTES )); then
  log "event=noop usage_percent=$before_percent free_bytes=$before_free"
  exit 0
fi

log "event=start usage_percent=$before_percent free_bytes=$before_free"
pruned_slots=0
skipped_slots=0

for slot in 1 2 3 4; do
  unit="blueprint-github-actions@$slot.service"
  socket="$SOCKET_ROOT/$slot/docker.sock"
  expected_root="$STATE_ROOT/docker-$slot"

  if [[ ! -S "$socket" ]]; then
    log "event=skip slot=$slot reason=socket_missing"
    skipped_slots=$((skipped_slots + 1))
    continue
  fi
  read -r socket_uid socket_mode < <(stat --format='%u %a' "$socket")
  if [[ "$socket_uid" != "0" || "$socket_mode" == *[1-7] ]]; then
    log "severity=warning event=skip slot=$slot reason=unsafe_socket_metadata uid=$socket_uid mode=$socket_mode"
    skipped_slots=$((skipped_slots + 1))
    continue
  fi
  if [[ "$(systemctl is-active "$unit" 2>/dev/null || true)" != "active" ]]; then
    log "event=skip slot=$slot reason=runner_not_active"
    skipped_slots=$((skipped_slots + 1))
    continue
  fi
  if [[ "$(systemctl show "$unit" --property=FreezerState --value)" != "running" ]]; then
    log "event=skip slot=$slot reason=runner_pre_frozen"
    skipped_slots=$((skipped_slots + 1))
    continue
  fi
  actual_root="$(docker_for_slot "$slot" info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  if [[ "$actual_root" != "$expected_root" ]]; then
    log "severity=warning event=skip slot=$slot reason=docker_root_mismatch"
    skipped_slots=$((skipped_slots + 1))
    continue
  fi
  if [[ "$(systemctl show "$unit" --property=FreezerState --value)" != "running" ]]; then
    log "event=skip slot=$slot reason=runner_became_frozen"
    skipped_slots=$((skipped_slots + 1))
    continue
  fi

  frozen_marker="$OWNERSHIP_ROOT/frozen-$slot"
  marker_temporary="$OWNERSHIP_ROOT/.frozen-$slot.$$"
  printf '%s\n' "$unit" > "$marker_temporary"
  chmod 0600 "$marker_temporary"
  mv --force --no-target-directory "$marker_temporary" "$frozen_marker"
  if ! systemctl freeze "$unit"; then
    rm --force -- "$frozen_marker"
    frozen_marker=""
    log "severity=warning event=skip slot=$slot reason=freeze_failed"
    skipped_slots=$((skipped_slots + 1))
    continue
  fi
  frozen_unit="$unit"

  worker_result=0
  runner_has_worker "$slot" "$unit" || worker_result=$?
  if (( worker_result == 0 )); then
    log "event=skip slot=$slot reason=active_worker"
    skipped_slots=$((skipped_slots + 1))
    thaw_current
    continue
  fi
  if (( worker_result != 1 )); then
    log "severity=warning event=skip slot=$slot reason=ambiguous_runner_cgroup"
    skipped_slots=$((skipped_slots + 1))
    thaw_current
    continue
  fi

  running_first="$(docker_for_slot "$slot" ps --quiet 2>/dev/null || printf '__docker_error__')"
  sleep 2
  running_second="$(docker_for_slot "$slot" ps --quiet 2>/dev/null || printf '__docker_error__')"
  if [[ -n "$running_first" || -n "$running_second" ]]; then
    log "event=skip slot=$slot reason=running_or_unreadable_containers"
    skipped_slots=$((skipped_slots + 1))
    thaw_current
    continue
  fi

  images_before="$(docker_for_slot "$slot" image ls --quiet --no-trunc | sort -u | sha256sum | cut -d' ' -f1)"
  volumes_before="$(docker_for_slot "$slot" volume ls --quiet | wc -l)"
  prune_output="$(docker_for_slot "$slot" volume prune --force)"
  images_after="$(docker_for_slot "$slot" image ls --quiet --no-trunc | sort -u | sha256sum | cut -d' ' -f1)"
  volumes_after="$(docker_for_slot "$slot" volume ls --quiet | wc -l)"
  if [[ "$images_before" != "$images_after" ]]; then
    log "severity=critical event=image_set_changed slot=$slot"
    exit 1
  fi
  log "event=pruned slot=$slot volumes_before=$volumes_before volumes_after=$volumes_after output=$(printf '%q' "$prune_output")"
  pruned_slots=$((pruned_slots + 1))
  thaw_current
done

read -r after_total after_free after_percent < <(filesystem_state)
severity="info"
result=0
if (( after_percent >= CRITICAL_PERCENT || after_free < CRITICAL_FREE_BYTES )); then
  severity="critical"
  result=1
elif (( after_percent >= WARN_PERCENT || after_free < WARN_FREE_BYTES )); then
  severity="warning"
fi
log "severity=$severity event=complete usage_before=$before_percent usage_after=$after_percent free_before=$before_free free_after=$after_free pruned_slots=$pruned_slots skipped_slots=$skipped_slots"
exit "$result"
