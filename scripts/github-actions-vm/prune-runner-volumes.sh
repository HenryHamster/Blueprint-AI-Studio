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
readonly ORPHAN_MIN_AGE_SECONDS="${BLUEPRINT_ACTIONS_GC_ORPHAN_MIN_AGE_SECONDS:-7200}"
readonly RUNNER_UNIT_FRAGMENT="/etc/systemd/system/blueprint-github-actions@.service"
readonly DOCKER_UNIT_FRAGMENT="/etc/systemd/system/blueprint-dockerd@.service"
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

inactive_runner_is_quiescent() {
  local slot="$1" unit="$2" expected_state="$3" active sub main_pid load fragment
  local cgroup expected_cgroup processes pid
  active="$(systemctl show "$unit" --property=ActiveState --value)"
  sub="$(systemctl show "$unit" --property=SubState --value)"
  main_pid="$(systemctl show "$unit" --property=MainPID --value)"
  load="$(systemctl show "$unit" --property=LoadState --value)"
  fragment="$(systemctl show "$unit" --property=FragmentPath --value)"
  [[ "$active" == "$expected_state" && "$load" == "loaded" && "$main_pid" == "0" && \
     "$fragment" == "$RUNNER_UNIT_FRAGMENT" ]] || return 1
  [[ ( "$active" == "inactive" && "$sub" == "dead" ) || \
     ( "$active" == "failed" && "$sub" == "failed" ) ]] || return 1
  cgroup="$(systemctl show "$unit" --property=ControlGroup --value)"
  expected_cgroup="/system.slice/system-blueprint\\x2dgithub\\x2dactions.slice/blueprint-github-actions@$slot.service"
  if [[ -z "$cgroup" && ! -e "$CGROUP_ROOT$expected_cgroup" ]]; then
    [[ "$(systemctl is-active "$unit" 2>/dev/null || true)" == "$expected_state" ]]
    return
  fi
  [[ "$cgroup" == "$expected_cgroup" && -d "$CGROUP_ROOT$cgroup" ]] || return 1
  processes="$(find "$CGROUP_ROOT$cgroup" -name cgroup.procs -type f -exec cat {} +)" || return 1
  for pid in $processes; do
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    return 1
  done
  [[ "$(systemctl is-active "$unit" 2>/dev/null || true)" == "$expected_state" ]]
}

docker_boundary_is_exact() {
  local slot="$1" socket="$2" expected_root="$3" account group_row group_name group_gid
  local socket_uid socket_gid socket_mode socket_kind root_uid root_gid root_mode root_kind
  local daemon load active sub main_pid fragment actual_root
  account="blueprint-actions-$slot"
  group_row="$(getent group "$account" 2>/dev/null || true)"
  IFS=: read -r group_name _ group_gid _ <<< "$group_row"
  [[ "$group_name" == "$account" && "$group_gid" =~ ^[0-9]+$ ]] || return 1
  read -r socket_uid socket_gid socket_mode socket_kind < <(stat --format='%u %g %a %F' "$socket")
  [[ "$socket_uid" == "0" && "$socket_gid" == "$group_gid" && \
     "$socket_mode" == "660" && "$socket_kind" == "socket" ]] || return 1
  read -r root_uid root_gid root_mode root_kind < <(stat --format='%u %g %a %F' "$expected_root")
  [[ "$root_uid" == "0" && "$root_gid" == "$group_gid" && \
     "$root_mode" == "710" && "$root_kind" == "directory" ]] || return 1
  daemon="blueprint-dockerd@$slot.service"
  load="$(systemctl show "$daemon" --property=LoadState --value)"
  active="$(systemctl show "$daemon" --property=ActiveState --value)"
  sub="$(systemctl show "$daemon" --property=SubState --value)"
  main_pid="$(systemctl show "$daemon" --property=MainPID --value)"
  fragment="$(systemctl show "$daemon" --property=FragmentPath --value)"
  [[ "$load" == "loaded" && "$active" == "active" && "$sub" == "running" && \
     "$main_pid" =~ ^[1-9][0-9]*$ && "$fragment" == "$DOCKER_UNIT_FRAGMENT" ]] || return 1
  actual_root="$(docker_for_slot "$slot" info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  [[ "$actual_root" == "$expected_root" ]]
}

orphan_container_identity() {
  local slot="$1" id="$2" now_epoch="$3"
  local record actual_id name created status running paused restarting dead image pid extra
  local created_epoch age
  [[ "$id" =~ ^[0-9a-f]{64}$ ]] || return 1
  record="$(docker_for_slot "$slot" inspect --format \
    '{{.Id}}|{{.Name}}|{{.Created}}|{{.State.Status}}|{{.State.Running}}|{{.State.Paused}}|{{.State.Restarting}}|{{.State.Dead}}|{{.Config.Image}}|{{.State.Pid}}' \
    "$id" 2>/dev/null)" || return 1
  IFS='|' read -r actual_id name created status running paused restarting dead image pid extra <<< "$record"
  [[ -z "$extra" && "$actual_id" == "$id" && "$name" == /* && \
     "$status" == "running" && "$running" == "true" && \
     "$paused" == "false" && "$restarting" == "false" && "$dead" == "false" && \
     "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  created_epoch="$(date --date="$created" +%s 2>/dev/null || true)"
  [[ "$created_epoch" =~ ^[0-9]+$ && "$now_epoch" =~ ^[0-9]+$ && \
     "$created_epoch" -le "$now_epoch" ]] || return 1
  age=$((now_epoch - created_epoch))
  (( age >= ORPHAN_MIN_AGE_SECONDS )) || return 1
  printf 'dedicated_actions_container name=%q image=%q age_seconds=%s\n' "$name" "$image" "$age"
}

reap_inactive_orphans() {
  local slot="$1" unit="$2" runner_state="$3" now_epoch="$4"
  local first second id identity repeat after
  first="$(docker_for_slot "$slot" ps --quiet --no-trunc 2>/dev/null || printf '__docker_error__')"
  sleep 2
  second="$(docker_for_slot "$slot" ps --quiet --no-trunc 2>/dev/null || printf '__docker_error__')"
  [[ "$first" != "__docker_error__" && "$first" == "$second" ]] || return 1
  [[ -n "$first" ]] || return 0
  while IFS= read -r id; do
    identity="$(orphan_container_identity "$slot" "$id" "$now_epoch")" || return 1
    repeat="$(orphan_container_identity "$slot" "$id" "$now_epoch")" || return 1
    [[ "$identity" == "$repeat" ]] || return 1
  done <<< "$first"
  inactive_runner_is_quiescent "$slot" "$unit" "$runner_state" || return 1
  while IFS= read -r id; do
    identity="$(orphan_container_identity "$slot" "$id" "$now_epoch")" || return 1
    inactive_runner_is_quiescent "$slot" "$unit" "$runner_state" || return 1
    docker_for_slot "$slot" rm --force -- "$id" >/dev/null || return 1
    log "event=orphan_reaped slot=$slot container=$id identity=$(printf '%q' "$identity")"
  done <<< "$first"
  after="$(docker_for_slot "$slot" ps --quiet --no-trunc 2>/dev/null || printf '__docker_error__')"
  [[ -z "$after" ]]
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
now_epoch="${BLUEPRINT_ACTIONS_GC_NOW_EPOCH:-$(date +%s)}"
[[ "$now_epoch" =~ ^[0-9]+$ && "$ORPHAN_MIN_AGE_SECONDS" =~ ^[0-9]+$ ]] || {
  log "severity=critical event=invalid_time_boundary"
  exit 1
}
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
  if ! docker_boundary_is_exact "$slot" "$socket" "$expected_root"; then
    log "severity=warning event=skip slot=$slot reason=ambiguous_docker_boundary"
    skipped_slots=$((skipped_slots + 1))
    continue
  fi
  runner_state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  if [[ ! "$runner_state" =~ ^(active|inactive|failed)$ ]]; then
    log "event=skip slot=$slot reason=runner_state_ambiguous state=$(printf '%q' "$runner_state")"
    skipped_slots=$((skipped_slots + 1))
    continue
  fi
  if [[ "$runner_state" =~ ^(inactive|failed)$ ]]; then
    if ! inactive_runner_is_quiescent "$slot" "$unit" "$runner_state"; then
      log "severity=warning event=skip slot=$slot reason=ambiguous_inactive_runner"
      skipped_slots=$((skipped_slots + 1))
      continue
    fi
    images_before="$(docker_for_slot "$slot" image ls --quiet --no-trunc | sort -u | sha256sum | cut -d' ' -f1)"
    volumes_before="$(docker_for_slot "$slot" volume ls --quiet | wc -l)"
    if ! reap_inactive_orphans "$slot" "$unit" "$runner_state" "$now_epoch"; then
      log "severity=warning event=skip slot=$slot reason=unclassified_or_changing_containers"
      skipped_slots=$((skipped_slots + 1))
      continue
    fi
    if ! inactive_runner_is_quiescent "$slot" "$unit" "$runner_state" || \
       ! docker_boundary_is_exact "$slot" "$socket" "$expected_root"; then
      log "severity=warning event=skip slot=$slot reason=boundary_changed_after_reap"
      skipped_slots=$((skipped_slots + 1))
      continue
    fi
    prune_output="$(docker_for_slot "$slot" volume prune --force)"
    images_after="$(docker_for_slot "$slot" image ls --quiet --no-trunc | sort -u | sha256sum | cut -d' ' -f1)"
    volumes_after="$(docker_for_slot "$slot" volume ls --quiet | wc -l)"
    if [[ "$images_before" != "$images_after" ]]; then
      log "severity=critical event=image_set_changed slot=$slot"
      exit 1
    fi
    log "event=pruned slot=$slot mode=inactive_recovery volumes_before=$volumes_before volumes_after=$volumes_after output=$(printf '%q' "$prune_output")"
    pruned_slots=$((pruned_slots + 1))
    continue
  fi
  if [[ "$(systemctl show "$unit" --property=FreezerState --value)" != "running" ]]; then
    log "event=skip slot=$slot reason=runner_pre_frozen"
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
