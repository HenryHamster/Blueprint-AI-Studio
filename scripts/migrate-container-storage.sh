#!/usr/bin/env bash
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

# Move the host Docker and containerd stores off a small root filesystem while
# preserving their canonical paths. The cutover is deliberately phased:
#   prepare  - validate and pre-copy while Docker remains online
#   cutover  - stop writers, final-sync, install fail-closed bind mounts, restart
#   verify   - prove the mounts and runtime inventory after cutover/reboot
#   rollback - restore the original root-backed directories (before cleanup)
#   cleanup  - move retained root backups off the OS filesystem after verify

readonly DATA_MOUNT=/srv/data
readonly SYSTEM_DATA_ROOT=$DATA_MOUNT/blueprint-system
readonly DEST_ROOT=$SYSTEM_DATA_ROOT/container-runtime
readonly STATE_ROOT=/var/lib/blueprint/container-storage-migration
readonly DOCKER_SOURCE=/var/lib/docker
readonly CONTAINERD_SOURCE=/var/lib/containerd
readonly DOCKER_DEST="$DEST_ROOT/docker"
readonly CONTAINERD_DEST="$DEST_ROOT/containerd"
readonly DOCKER_BACKUP=/var/lib/docker.blueprint-root-backup
readonly CONTAINERD_BACKUP=/var/lib/containerd.blueprint-root-backup
readonly DOCKER_MOUNT_UNIT=/etc/systemd/system/var-lib-docker.mount
readonly CONTAINERD_MOUNT_UNIT=/etc/systemd/system/var-lib-containerd.mount
readonly DOCKER_DROPIN=/etc/systemd/system/docker.service.d/blueprint-container-storage.conf
readonly CONTAINERD_DROPIN=/etc/systemd/system/containerd.service.d/blueprint-container-storage.conf
readonly PREPARED_MARKER="$STATE_ROOT/prepared"
readonly CUTOVER_MARKER="$STATE_ROOT/cutover-complete"
readonly REBOOT_MARKER="$STATE_ROOT/reboot-verified"
readonly CUTOVER_BOOT_ID="$STATE_ROOT/cutover-boot-id"
readonly DATA_MOUNT_UNIT="$(systemd-escape --path --suffix=mount "$DATA_MOUNT")"
readonly DEST_MANIFEST="$DEST_ROOT/.blueprint-container-storage-migration-v1"

log() { printf '[container-storage] %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

require_root() {
  [[ "$(id -u)" == 0 ]] || die "run as root"
}

assert_self_secure() {
  local script owner mode
  script="$(readlink -f -- "${BASH_SOURCE[0]}")"
  [[ "$script" == "${BASH_SOURCE[0]}" && -f "$script" && ! -L "$script" ]] \
    || die "migration script must be an absolute regular non-symlink file"
  read -r owner mode < <(stat --format='%u %a' "$script")
  [[ "$owner" == 0 && "$mode" == 700 ]] \
    || die "migration script must be root-owned mode 0700"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

assert_regular_directory() {
  local path="$1"
  [[ -d "$path" && ! -L "$path" ]] || die "$path must be a non-symlink directory"
}

assert_root_owned_safe_ancestors() {
  local path="$1" current owner mode
  current="$path"
  while [[ "$current" != / ]]; do
    [[ -e "$current" && ! -L "$current" ]] || die "unsafe or missing path ancestor: $current"
    read -r owner mode < <(stat --format='%u %a' "$current")
    [[ "$owner" == 0 ]] || die "path ancestor is not root-owned: $current"
    (( (8#$mode & 0022) == 0 )) || die "path ancestor is group/world writable: $current"
    current="$(dirname -- "$current")"
  done
}

assert_data_mount() {
  local target fstype options
  target="$(findmnt --noheadings --output TARGET --target "$DATA_MOUNT" | xargs)"
  [[ "$target" == "$DATA_MOUNT" ]] || die "$DATA_MOUNT is not a distinct mountpoint"
  fstype="$(findmnt --noheadings --output FSTYPE --target "$DATA_MOUNT" | xargs)"
  options="$(findmnt --noheadings --output OPTIONS --target "$DATA_MOUNT" | xargs)"
  case "$fstype" in
    ext4) ;;
    xfs)
      xfs_info "$DATA_MOUNT" | grep -Eq 'ftype=1|crc=1' \
        || die "XFS data mount must support d_type (ftype=1)"
      ;;
    *) die "unsupported container data filesystem: $fstype" ;;
  esac
  [[ ",$options," != *,ro,* ]] || die "$DATA_MOUNT is read-only"
  assert_root_owned_safe_ancestors "$DATA_MOUNT"
}

assert_runtime_layout() {
  local docker_root driver_status containerd_root
  docker_root="$(docker info --format '{{.DockerRootDir}}')"
  [[ "$docker_root" == "$DOCKER_SOURCE" ]] || die "unexpected Docker root: $docker_root"
  driver_status="$(docker info --format '{{json .DriverStatus}}')"
  [[ "$driver_status" == *'io.containerd.snapshotter.v1'* ]] \
    || die "Docker is not using the reviewed containerd snapshotter layout"
  containerd_root="$(containerd config dump 2>/dev/null | awk -F' = ' '$1 == "root" {gsub(/["\047]/, "", $2); print $2; exit}')"
  [[ -z "$containerd_root" || "$containerd_root" == "$CONTAINERD_SOURCE" ]] \
    || die "unexpected containerd root: $containerd_root"
}

assert_containerd_namespaces_supported() {
  local namespace
  while IFS= read -r namespace; do
    [[ -z "$namespace" || "$namespace" == moby ]] \
      || die "unreviewed non-Docker containerd namespace: $namespace"
  done < <(ctr namespaces list --quiet)
}

assert_install_paths_free() {
  local path
  for path in "$DOCKER_MOUNT_UNIT" "$CONTAINERD_MOUNT_UNIT" "$DOCKER_DROPIN" "$CONTAINERD_DROPIN"; do
    [[ ! -e "$path" && ! -L "$path" ]] || die "refusing to replace existing unit path: $path"
  done
}

assert_not_migrated() {
  ! mountpoint --quiet "$DOCKER_SOURCE" || die "$DOCKER_SOURCE is already a mountpoint"
  ! mountpoint --quiet "$CONTAINERD_SOURCE" || die "$CONTAINERD_SOURCE is already a mountpoint"
  [[ ! -e "$DOCKER_BACKUP" && ! -e "$CONTAINERD_BACKUP" ]] \
    || die "root backups already exist; verify or rollback the prior attempt"
}

assert_destination_capacity() {
  local required available
  required="$(du --summarize --block-size=1 "$DOCKER_SOURCE" "$CONTAINERD_SOURCE" \
    | awk '{sum += $1} END {print sum}')"
  available="$(df --block-size=1 --output=avail "$DATA_MOUNT" | awk 'NR == 2 {print $1}')"
  (( available >= 2 * required + 20 * 1024 * 1024 * 1024 )) \
    || die "data mount lacks two source copies plus 20 GiB safety margin"
  log "capacity_ok required_bytes=$required available_bytes=$available"
}

prepare_destination_root() {
  if [[ -e "$SYSTEM_DATA_ROOT" || -L "$SYSTEM_DATA_ROOT" ]]; then
    assert_regular_directory "$SYSTEM_DATA_ROOT"
    assert_root_owned_safe_ancestors "$SYSTEM_DATA_ROOT"
  else
    install -d -m 0711 -o root -g root "$SYSTEM_DATA_ROOT"
  fi
  if [[ -e "$DEST_ROOT" || -L "$DEST_ROOT" ]]; then
    assert_regular_directory "$DEST_ROOT"
    [[ -f "$DEST_MANIFEST" && ! -L "$DEST_MANIFEST" ]] \
      || die "non-empty/reused destination lacks the Blueprint ownership manifest"
    [[ "$(cat "$DEST_MANIFEST")" == blueprint-container-storage-migration-v1 ]] \
      || die "destination ownership manifest is invalid"
    [[ "$(stat --format='%u %a' "$DEST_MANIFEST")" == "0 600" ]] \
      || die "destination ownership manifest metadata is unsafe"
  else
    install -d -m 0711 -o root -g root "$DEST_ROOT"
    install -m 0600 -o root -g root /dev/null "$DEST_MANIFEST"
    printf '%s\n' blueprint-container-storage-migration-v1 >"$DEST_MANIFEST"
  fi
  assert_root_owned_safe_ancestors "$DEST_ROOT"
}

assert_required_services_active() {
  systemctl is-active --quiet docker.service containerd.service \
    blueprint-database.service blueprint-api.service blueprint-worker.service \
    blueprint-edge.service blueprint-database-backup.timer \
    || die "all production container services must be active before prepare"
}

assert_no_nested_mounts() {
  local source="$1" target
  while IFS= read -r target; do
    [[ "$target" != "$source/"* ]] \
      || die "nested mount below $source would be skipped by one-filesystem rsync: $target"
  done < <(findmnt --raw --noheadings --output TARGET)
}

capture_inventory() {
  local destination="$1"
  install -d -m 0700 -o root -g root "$destination"
  docker info >"$destination/docker-info.txt"
  docker ps --all --no-trunc >"$destination/containers.txt"
  docker image ls --all --digests --no-trunc >"$destination/images.txt"
  docker volume ls >"$destination/volumes.txt"
  docker system df -v >"$destination/docker-system-df.txt"
  containerd config dump >"$destination/containerd-config.txt"
  ctr namespaces list >"$destination/containerd-namespaces.txt"
  docker ps --all --format '{{.Names}}' | sort >"$destination/container-names.contract"
  docker image ls --all --no-trunc --format '{{.ID}}\t{{.Repository}}:{{.Tag}}\t{{.Digest}}' \
    | sort >"$destination/images.contract"
  docker volume ls --quiet | sort >"$destination/volumes.contract"
  sha256sum "$destination"/*.txt >"$destination/SHA256SUMS"
}

assert_runtime_inventory_preserved() {
  local name
  for name in container-names images volumes; do
    cmp --silent "$STATE_ROOT/pre/$name.contract" "$STATE_ROOT/post/$name.contract" \
      || die "runtime inventory changed during migration: $name"
  done
}

sync_store() {
  local source="$1" destination="$2"
  install -d -m 0711 -o root -g root "$destination"
  chown --reference="$source" "$destination"
  chmod --reference="$source" "$destination"
  rsync --archive --hard-links --acls --xattrs --numeric-ids --one-file-system \
    --delete -- "$source/" "$destination/"
}

assert_sync_clean() {
  local source="$1" destination="$2" output
  output="$(rsync --archive --hard-links --acls --xattrs --numeric-ids \
    --one-file-system --delete --dry-run --itemize-changes -- "$source/" "$destination/")"
  [[ -z "$output" ]] || die "offline rsync verification found differences: $output"
}

assert_bind_source() {
  local target="$1" destination="$2" mounted_target fsroot expected_fsroot
  mounted_target="$(findmnt --noheadings --output TARGET --target "$target" | xargs)"
  [[ "$mounted_target" == "$target" ]] || die "$target is not an exact mountpoint"
  fsroot="$(findmnt --noheadings --output FSROOT --target "$target" | xargs)"
  expected_fsroot="${destination#"$DATA_MOUNT"}"
  [[ "$fsroot" == "$expected_fsroot" ]] \
    || die "$target bind source mismatch: expected $expected_fsroot, got $fsroot"
}

prepare() {
  require_root
  assert_self_secure
  for command in docker containerd ctr rsync findmnt mountpoint systemctl sha256sum \
    systemd-analyze systemd-escape cmp curl pgrep awk grep xargs du df stat readlink; do
    require_command "$command"
  done
  assert_data_mount
  assert_regular_directory "$DOCKER_SOURCE"
  assert_regular_directory "$CONTAINERD_SOURCE"
  assert_root_owned_safe_ancestors "$DOCKER_SOURCE"
  assert_root_owned_safe_ancestors "$CONTAINERD_SOURCE"
  assert_not_migrated
  assert_install_paths_free
  assert_runtime_layout
  assert_containerd_namespaces_supported
  assert_required_services_active
  assert_destination_capacity
  install -d -m 0700 -o root -g root "$STATE_ROOT"
  assert_root_owned_safe_ancestors "$STATE_ROOT"
  prepare_destination_root
  capture_inventory "$STATE_ROOT/pre"
  if [[ "$(docker inspect --format '{{.State.Running}}' blueprint-actions-vm-net 2>/dev/null || true)" == true \
      && "$(docker inspect --format '{{.State.Running}}' blueprint-actions-vm 2>/dev/null || true)" == true ]]; then
    install -m 0600 -o root -g root /dev/null "$STATE_ROOT/actions-vm-was-running"
  fi
  log "pre-copying Docker store"
  sync_store "$DOCKER_SOURCE" "$DOCKER_DEST"
  log "pre-copying containerd store"
  sync_store "$CONTAINERD_SOURCE" "$CONTAINERD_DEST"
  date --utc +%Y-%m-%dT%H:%M:%SZ >"$PREPARED_MARKER"
  chmod 0600 "$PREPARED_MARKER"
  log "PREPARE_COMPLETE state=$STATE_ROOT"
}

write_units() {
  install -d -m 0755 /etc/systemd/system/docker.service.d
  install -d -m 0755 /etc/systemd/system/containerd.service.d
  assert_root_owned_safe_ancestors /etc/systemd/system/docker.service.d
  assert_root_owned_safe_ancestors /etc/systemd/system/containerd.service.d
  cat >"$DOCKER_MOUNT_UNIT" <<EOF
[Unit]
Description=Bind Docker state to the Blueprint data filesystem
RequiresMountsFor=$DATA_MOUNT
After=$DATA_MOUNT_UNIT
Before=docker.service
ConditionPathIsMountPoint=$DATA_MOUNT

[Mount]
What=$DOCKER_DEST
Where=$DOCKER_SOURCE
Type=none
Options=bind

[Install]
WantedBy=local-fs.target
EOF
  cat >"$CONTAINERD_MOUNT_UNIT" <<EOF
[Unit]
Description=Bind containerd state to the Blueprint data filesystem
RequiresMountsFor=$DATA_MOUNT
After=$DATA_MOUNT_UNIT
Before=containerd.service
ConditionPathIsMountPoint=$DATA_MOUNT

[Mount]
What=$CONTAINERD_DEST
Where=$CONTAINERD_SOURCE
Type=none
Options=bind

[Install]
WantedBy=local-fs.target
EOF
  cat >"$DOCKER_DROPIN" <<EOF
[Unit]
Requires=var-lib-docker.mount var-lib-containerd.mount
After=$DATA_MOUNT_UNIT var-lib-docker.mount var-lib-containerd.mount containerd.service
ConditionPathIsMountPoint=$DATA_MOUNT
ConditionPathIsMountPoint=$DOCKER_SOURCE
ConditionPathIsMountPoint=$CONTAINERD_SOURCE
EOF
  cat >"$CONTAINERD_DROPIN" <<EOF
[Unit]
Requires=var-lib-containerd.mount
After=$DATA_MOUNT_UNIT var-lib-containerd.mount
ConditionPathIsMountPoint=$DATA_MOUNT
ConditionPathIsMountPoint=$CONTAINERD_SOURCE
EOF
  chmod 0644 "$DOCKER_MOUNT_UNIT" "$CONTAINERD_MOUNT_UNIT" "$DOCKER_DROPIN" "$CONTAINERD_DROPIN"
}

stop_writers() {
  ! pgrep --full '/scripts/local_runner.py|blueprint-runner' >/dev/null \
    || die "Blueprint local runner is still active; stop its user service before cutover"
  systemctl stop blueprint-database-backup.timer blueprint-database-backup.service
  systemctl stop blueprint-edge.service blueprint-worker.service blueprint-api.service
  systemctl stop blueprint-database.service
  if [[ "$(docker inspect --format '{{.State.Running}}' blueprint-actions-vm 2>/dev/null || true)" == true ]]; then
    docker stop --time 120 blueprint-actions-vm >/dev/null
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' blueprint-actions-vm-net 2>/dev/null || true)" == true ]]; then
    docker stop --time 10 blueprint-actions-vm-net >/dev/null
  fi
  [[ -z "$(docker ps --quiet)" ]] || die "Docker containers remained after explicit service shutdown"
  systemctl stop docker.socket docker.service
  systemctl stop containerd.service
  ! pgrep --exact dockerd >/dev/null || die "dockerd remained after stop"
  ! pgrep --exact containerd >/dev/null || die "containerd remained after stop"
  ! pgrep --full '(^|/)containerd-shim(-[^[:space:]]*)?([[:space:]]|$)' >/dev/null \
    || die "containerd shims remained after stop"
  ! pgrep --full '(^|/)qemu-system-x86_64([[:space:]]|$)' >/dev/null \
    || die "Actions QEMU remained after stop"
  [[ -z "$(systemctl list-units --state=running --no-legend 'docker-*.scope')" ]] \
    || die "Docker container scopes remained after stop"
  assert_no_nested_mounts "$DOCKER_SOURCE"
  assert_no_nested_mounts "$CONTAINERD_SOURCE"
}

start_application_services() {
  systemctl start containerd.service
  systemctl start docker.socket docker.service
  systemctl start blueprint-database.service
  systemctl start blueprint-api.service blueprint-worker.service blueprint-edge.service
  systemctl start blueprint-database-backup.timer
}

start_actions_vm_if_required() {
  [[ -f "$STATE_ROOT/actions-vm-was-running" ]] || return 0
  docker start blueprint-actions-vm-net >/dev/null
  local attempt firewall_ready=false
  for ((attempt = 1; attempt <= 50; attempt++)); do
    if docker exec blueprint-actions-vm-net \
      iptables -C OUTPUT -d 169.254.0.0/16 -j REJECT >/dev/null 2>&1; then
      firewall_ready=true
      break
    fi
    sleep 0.1
  done
  [[ "$firewall_ready" == true ]] || die "Actions VM firewall did not recover"
  docker start blueprint-actions-vm >/dev/null
}

assert_application_healthy() {
  local name status attempt api_ready=false
  for name in blueprint-local-api blueprint-local-worker blueprint-local-postgres; do
    status="$(docker inspect --format '{{.State.Status}}' "$name")"
    [[ "$status" == running ]] || die "$name is not running after migration"
  done
  for ((attempt = 1; attempt <= 60; attempt++)); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' blueprint-local-postgres)"
    [[ "$status" != unhealthy ]] || die "local PostgreSQL became unhealthy"
    if [[ "$status" == healthy ]] \
        && curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8080/health >/dev/null; then
      api_ready=true
      break
    fi
    sleep 1
  done
  [[ "$api_ready" == true ]] || die "local PostgreSQL/API health did not recover"
}

assert_actions_vm_running() {
  [[ -f "$STATE_ROOT/actions-vm-was-running" ]] || return 0
  [[ "$(docker inspect --format '{{.State.Running}}' blueprint-actions-vm-net)" == true ]] \
    || die "Actions VM firewall container is not running"
  [[ "$(docker inspect --format '{{.State.Running}}' blueprint-actions-vm)" == true ]] \
    || die "Actions VM container is not running"
}

rollback_internal() {
  trap - ERR
  local path
  ! pgrep --full '/scripts/local_runner.py|blueprint-runner' >/dev/null \
    || die "stop the Blueprint local runner before rollback"
  systemctl stop blueprint-database-backup.timer blueprint-edge.service \
    blueprint-database-backup.service blueprint-worker.service blueprint-api.service \
    blueprint-database.service
  if systemctl is-active --quiet docker.service; then
    if [[ "$(docker inspect --format '{{.State.Running}}' blueprint-actions-vm 2>/dev/null || true)" == true ]]; then
      docker stop --time 120 blueprint-actions-vm >/dev/null
    fi
    if [[ "$(docker inspect --format '{{.State.Running}}' blueprint-actions-vm-net 2>/dev/null || true)" == true ]]; then
      docker stop --time 10 blueprint-actions-vm-net >/dev/null
    fi
    [[ -z "$(docker ps --quiet)" ]] || die "Docker containers remained before rollback"
  fi
  systemctl stop docker.socket docker.service containerd.service
  ! pgrep --exact dockerd >/dev/null || die "dockerd remained before rollback"
  ! pgrep --exact containerd >/dev/null || die "containerd remained before rollback"
  ! pgrep --full '(^|/)containerd-shim(-[^[:space:]]*)?([[:space:]]|$)' >/dev/null \
    || die "containerd shims remained before rollback"
  ! pgrep --full '(^|/)qemu-system-x86_64([[:space:]]|$)' >/dev/null \
    || die "Actions QEMU remained before rollback"
  [[ -z "$(systemctl list-units --state=running --no-legend 'docker-*.scope')" ]] \
    || die "Docker container scopes remained before rollback"
  systemctl disable var-lib-docker.mount var-lib-containerd.mount 2>/dev/null || true
  for path in "$DOCKER_SOURCE" "$CONTAINERD_SOURCE"; do
    if mountpoint --quiet "$path"; then
      umount "$path"
    fi
    ! mountpoint --quiet "$path" || die "rollback could not unmount $path"
  done
  rm -f -- "$DOCKER_MOUNT_UNIT" "$CONTAINERD_MOUNT_UNIT" "$DOCKER_DROPIN" "$CONTAINERD_DROPIN"
  if [[ -d "$DOCKER_BACKUP" ]]; then
    [[ ! -e "$DOCKER_SOURCE" ]] || rmdir -- "$DOCKER_SOURCE"
    mv -- "$DOCKER_BACKUP" "$DOCKER_SOURCE"
  fi
  if [[ -d "$CONTAINERD_BACKUP" ]]; then
    [[ ! -e "$CONTAINERD_SOURCE" ]] || rmdir -- "$CONTAINERD_SOURCE"
    mv -- "$CONTAINERD_BACKUP" "$CONTAINERD_SOURCE"
  fi
  assert_regular_directory "$DOCKER_SOURCE"
  assert_regular_directory "$CONTAINERD_SOURCE"
  [[ ! -e "$DOCKER_BACKUP" && ! -e "$CONTAINERD_BACKUP" ]] \
    || die "rollback left a backup path unrestored"
  systemctl daemon-reload
  start_application_services
  assert_application_healthy
  start_actions_vm_if_required
  assert_actions_vm_running
}

on_cutover_error() {
  local status="$?"
  trap - ERR
  log "cutover failed; restoring root-backed runtime"
  if ! rollback_internal; then
    log "CRITICAL: automatic rollback failed; leave container services stopped"
    exit 1
  fi
  exit "$status"
}

cutover() {
  require_root
  assert_self_secure
  [[ -f "$PREPARED_MARKER" ]] || die "prepare must complete before cutover"
  assert_data_mount
  assert_not_migrated
  assert_install_paths_free
  trap 'on_cutover_error' ERR
  log "stopping Docker-backed services"
  stop_writers
  log "performing final offline sync"
  sync_store "$DOCKER_SOURCE" "$DOCKER_DEST"
  sync_store "$CONTAINERD_SOURCE" "$CONTAINERD_DEST"
  assert_sync_clean "$DOCKER_SOURCE" "$DOCKER_DEST"
  assert_sync_clean "$CONTAINERD_SOURCE" "$CONTAINERD_DEST"
  mv -- "$DOCKER_SOURCE" "$DOCKER_BACKUP"
  mv -- "$CONTAINERD_SOURCE" "$CONTAINERD_BACKUP"
  install -d -m 0711 -o root -g root "$DOCKER_SOURCE" "$CONTAINERD_SOURCE"
  write_units
  systemctl daemon-reload
  systemd-analyze verify "$DOCKER_MOUNT_UNIT" "$CONTAINERD_MOUNT_UNIT" \
    docker.service containerd.service
  systemctl enable var-lib-docker.mount var-lib-containerd.mount
  systemctl start var-lib-containerd.mount var-lib-docker.mount
  assert_bind_source "$DOCKER_SOURCE" "$DOCKER_DEST"
  assert_bind_source "$CONTAINERD_SOURCE" "$CONTAINERD_DEST"
  findmnt --verify --verbose
  start_application_services
  capture_inventory "$STATE_ROOT/post"
  assert_runtime_inventory_preserved
  assert_application_healthy
  cat /proc/sys/kernel/random/boot_id >"$CUTOVER_BOOT_ID"
  chmod 0600 "$CUTOVER_BOOT_ID"
  date --utc +%Y-%m-%dT%H:%M:%SZ >"$CUTOVER_MARKER"
  chmod 0600 "$CUTOVER_MARKER"
  trap - ERR
  log "CUTOVER_COMPLETE backups_retained=true state=$STATE_ROOT"
}

verify() {
  require_root
  assert_self_secure
  [[ -f "$CUTOVER_MARKER" ]] || die "cutover marker is missing"
  assert_data_mount
  assert_bind_source "$DOCKER_SOURCE" "$DOCKER_DEST"
  assert_bind_source "$CONTAINERD_SOURCE" "$CONTAINERD_DEST"
  assert_runtime_layout
  systemctl is-active --quiet containerd.service docker.service blueprint-database.service \
    blueprint-api.service blueprint-worker.service blueprint-edge.service
  systemctl is-active --quiet blueprint-database-backup.timer
  assert_application_healthy
  capture_inventory "$STATE_ROOT/verify-$(date --utc +%Y%m%dT%H%M%SZ)"
  if [[ "$(cat /proc/sys/kernel/random/boot_id)" != "$(cat "$CUTOVER_BOOT_ID")" ]]; then
    date --utc +%Y-%m-%dT%H:%M:%SZ >"$REBOOT_MARKER"
    chmod 0600 "$REBOOT_MARKER"
    log "post-reboot mount verification recorded"
  else
    log "current-boot verification passed; reboot verification still required before cleanup"
  fi
  # Restart CI only after every migration and application-health assertion has
  # passed, so a queued GitHub job cannot introduce writes into a rollbackable
  # cutover window.
  start_actions_vm_if_required
  assert_actions_vm_running
  log "VERIFY_COMPLETE root_backups=$DOCKER_BACKUP,$CONTAINERD_BACKUP"
}

rollback() {
  require_root
  assert_self_secure
  [[ -d "$DOCKER_BACKUP" && -d "$CONTAINERD_BACKUP" ]] \
    || die "rollback backups are unavailable"
  rollback_internal
  rm -f -- "$CUTOVER_MARKER" "$REBOOT_MARKER" "$CUTOVER_BOOT_ID"
  log "ROLLBACK_COMPLETE"
}

cleanup() {
  require_root
  assert_self_secure
  [[ -f "$REBOOT_MARKER" ]] || die "verify after reboot before cleanup"
  [[ -d "$DOCKER_BACKUP" && -d "$CONTAINERD_BACKUP" ]] \
    || die "retained root backups are unavailable"
  assert_data_mount
  assert_bind_source "$DOCKER_SOURCE" "$DOCKER_DEST"
  assert_bind_source "$CONTAINERD_SOURCE" "$CONTAINERD_DEST"
  assert_runtime_layout
  systemctl is-active --quiet docker.service containerd.service \
    || die "container runtime is not healthy"
  assert_application_healthy
  # Deliberately no recursive deletion. Move retained backups onto the large
  # data filesystem, where they can be removed separately after retention.
  local rollback_root="$DEST_ROOT/root-backup-$(date --utc +%Y%m%dT%H%M%SZ)"
  install -d -m 0700 -o root -g root "$rollback_root"
  mv -- "$DOCKER_BACKUP" "$rollback_root/docker"
  mv -- "$CONTAINERD_BACKUP" "$rollback_root/containerd"
  log "CLEANUP_COMPLETE rollback_copy=$rollback_root"
}

usage() {
  printf 'Usage: %s {prepare|cutover|verify|rollback|cleanup}\n' "$0"
}

case "${1:-}" in
  prepare) prepare ;;
  cutover) cutover ;;
  verify) verify ;;
  rollback) rollback ;;
  cleanup) cleanup ;;
  *) usage; exit 2 ;;
esac
