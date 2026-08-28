#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ASSET_DIR="$SCRIPT_DIR/github-actions-vm"
MANAGER="$SCRIPT_DIR/github_actions_vm.py"
CONTROL_UNIT_SOURCE="$SCRIPT_DIR/blueprint-actions-vm.service"
CONTROL_ENV_PATH="$HOME/.blueprint/github-actions-vm-control.env"
CONTROL_UNIT_PATH="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/blueprint-actions-vm.service"

: "${BLUEPRINT_ACTIONS_VM_STATE:=$HOME/.blueprint/github-actions-vm}"
: "${BLUEPRINT_ACTIONS_VM_CPUS:=12}"
: "${BLUEPRINT_ACTIONS_VM_MEMORY_MIB:=32768}"
: "${BLUEPRINT_ACTIONS_VM_DISK_GIB:=80}"
: "${BLUEPRINT_ACTIONS_VM_RUNNERS:=4}"
: "${BLUEPRINT_ACTIONS_VM_SSH_PORT:=2222}"
: "${BLUEPRINT_ACTIONS_VM_SSH_KEY:=$HOME/.ssh/id_ed25519.pub}"
: "${BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY:=${BLUEPRINT_ACTIONS_VM_SSH_KEY%.pub}}"
: "${BLUEPRINT_ACTIONS_VM_KVM_DEVICE:=/dev/kvm}"

HOST_IMAGE="blueprint/github-actions-kvm:24.04"
FIREWALL_CONTAINER="blueprint-actions-vm-net"
QEMU_CONTAINER="blueprint-actions-vm"
CLOUD_IMAGE_URL="https://cloud-images.ubuntu.com/releases/24.04/release-20260725/ubuntu-24.04-server-cloudimg-amd64.img"
CLOUD_IMAGE_SHA256="d1940f7d69d343355e183dff1e08a59852d32e7309baa7a4bad8365b11b005ac"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

validate_uint() {
  local name="$1" value="$2" minimum="$3" maximum="$4"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$name must be an integer"
  (( value >= minimum && value <= maximum )) || die "$name must be between $minimum and $maximum"
}

validate_configuration() {
  require_command docker
  require_command python3
  [[ "$BLUEPRINT_ACTIONS_VM_STATE" = /* ]] || die "BLUEPRINT_ACTIONS_VM_STATE must be absolute"
  [[ "$BLUEPRINT_ACTIONS_VM_STATE" != *".."* ]] || die "state path may not contain '..'"
  [[ "$BLUEPRINT_ACTIONS_VM_STATE" != *[[:space:]]* ]] || die "state path may not contain whitespace"
  [[ "$BLUEPRINT_ACTIONS_VM_SSH_KEY" = /* ]] || die "BLUEPRINT_ACTIONS_VM_SSH_KEY must be absolute"
  [[ "$BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY" = /* ]] \
    || die "BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY must be absolute"
  [[ "$BLUEPRINT_ACTIONS_VM_KVM_DEVICE" = /* ]] || die "BLUEPRINT_ACTIONS_VM_KVM_DEVICE must be absolute"
  validate_uint cpus "$BLUEPRINT_ACTIONS_VM_CPUS" 2 64
  validate_uint memory "$BLUEPRINT_ACTIONS_VM_MEMORY_MIB" 4096 131072
  validate_uint disk "$BLUEPRINT_ACTIONS_VM_DISK_GIB" 24 2048
  validate_uint runners "$BLUEPRINT_ACTIONS_VM_RUNNERS" 1 16
  validate_uint ssh_port "$BLUEPRINT_ACTIONS_VM_SSH_PORT" 1024 65535
  [[ -f "$MANAGER" && -d "$ASSET_DIR" && -f "$CONTROL_UNIT_SOURCE" ]] \
    || die "run this command from a complete Blueprint checkout"
}

validate_control_path() {
  local name="$1" value="$2"
  [[ "$value" =~ ^/[A-Za-z0-9._/-]+$ ]] \
    || die "$name must be an absolute path containing only letters, digits, '.', '_', '-', and '/'"
  [[ "$value" != *".."* ]] || die "$name may not contain '..'"
}

validate_ssh_key_files() {
  [[ -f "$BLUEPRINT_ACTIONS_VM_SSH_KEY" && ! -L "$BLUEPRINT_ACTIONS_VM_SSH_KEY" ]] \
    || die "SSH public key is missing or symlinked: $BLUEPRINT_ACTIONS_VM_SSH_KEY"
  [[ -f "$BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY" && ! -L "$BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY" ]] \
    || die "SSH private key is missing or symlinked: $BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY"
  local key_mode
  key_mode="$(stat -c %a "$BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY")"
  [[ "$key_mode" == "600" || "$key_mode" == "400" ]] || die "SSH private key mode must be 0600 or 0400"
}

verify_user_lingering() {
  local linger_state user_name
  user_name="$(id -un)"
  if ! linger_state="$(loginctl show-user "$user_name" --property=Linger --value 2>/dev/null)"; then
    die "unable to verify systemd user lingering; run 'sudo loginctl enable-linger $user_name' and retry"
  fi
  [[ "$linger_state" == yes ]] \
    || die "systemd user lingering is $linger_state; run 'sudo loginctl enable-linger $user_name' and retry"
}

ssh_arguments() {
  validate_ssh_key_files
  [[ "$BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY" != *","* ]] \
    || die "SSH private key path may not contain commas"
  if [[ ! -e "$BLUEPRINT_ACTIONS_VM_STATE/known_hosts" ]]; then
    install -m 0600 /dev/null "$BLUEPRINT_ACTIONS_VM_STATE/known_hosts"
  fi
  [[ -f "$BLUEPRINT_ACTIONS_VM_STATE/known_hosts" \
      && ! -L "$BLUEPRINT_ACTIONS_VM_STATE/known_hosts" \
      && "$(stat -c %a "$BLUEPRINT_ACTIONS_VM_STATE/known_hosts")" == "600" ]] \
    || die "known_hosts must be a regular file with mode 0600"
}

guest_ssh() {
  ssh_arguments
  docker run --rm --interactive \
    --network "container:$FIREWALL_CONTAINER" \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --read-only \
    --user "$(id -u):$(id -g)" \
    --pids-limit 64 \
    --memory 128m \
    --cpus 0.25 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m \
    --tmpfs /run:rw,nosuid,nodev,noexec,size=4m \
    --mount "type=bind,src=$BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY,dst=/run/ssh_key,readonly" \
    --mount "type=bind,src=$BLUEPRINT_ACTIONS_VM_STATE/known_hosts,dst=/run/known_hosts" \
    --env HOME=/tmp \
    --entrypoint /usr/bin/ssh \
    "$HOST_IMAGE" \
    -i /run/ssh_key \
    -p "$BLUEPRINT_ACTIONS_VM_SSH_PORT" \
    -o BatchMode=yes \
    -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile=/run/known_hosts \
    -o ConnectTimeout=10 \
    blueprint-ci@127.0.0.1 \
    "$@"
}

ensure_state_root() {
  if [[ -L "$BLUEPRINT_ACTIONS_VM_STATE" ]]; then
    die "refusing symlinked state root"
  fi
  install -d -m 0700 "$BLUEPRINT_ACTIONS_VM_STATE"
  [[ "$(stat -c %a "$BLUEPRINT_ACTIONS_VM_STATE")" == "700" ]] || die "state root must have mode 0700"
  local resolved
  resolved="$(realpath -e "$BLUEPRINT_ACTIONS_VM_STATE")"
  [[ "$resolved" == "$BLUEPRINT_ACTIONS_VM_STATE" ]] || die "state root must be canonical"
}

require_disk_headroom() {
  local available minimum
  available="$(df --output=avail -B1 "$BLUEPRINT_ACTIONS_VM_STATE" | tail -n 1 | tr -d '[:space:]')"
  minimum=$((40 * 1024 * 1024 * 1024))
  [[ "$available" =~ ^[0-9]+$ ]] || die "could not determine available storage"
  (( available >= minimum )) || die "at least 40 GiB free is required before VM preparation"
}

container_exists() {
  docker container inspect "$1" >/dev/null 2>&1
}

container_running() {
  [[ "$(docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null || true)" == "true" ]]
}

image_id() {
  docker image inspect --format '{{.Id}}' "$HOST_IMAGE"
}

build_host_image() {
  docker build --pull=false --tag "$HOST_IMAGE" "$ASSET_DIR"
  image_id > "$BLUEPRINT_ACTIONS_VM_STATE/host-image-id"
  chmod 0600 "$BLUEPRINT_ACTIONS_VM_STATE/host-image-id"
}

download_cloud_image() {
  local destination="$BLUEPRINT_ACTIONS_VM_STATE/ubuntu-24.04-amd64.img"
  local temporary="$BLUEPRINT_ACTIONS_VM_STATE/.ubuntu-24.04-amd64.img.download"
  if [[ -f "$destination" ]] && printf '%s  %s\n' "$CLOUD_IMAGE_SHA256" "$destination" | sha256sum --check --status; then
    return
  fi
  require_command curl
  rm -f "$temporary"
  curl --fail --location --proto '=https' --tlsv1.2 --output "$temporary" "$CLOUD_IMAGE_URL"
  printf '%s  %s\n' "$CLOUD_IMAGE_SHA256" "$temporary" | sha256sum --check --status \
    || die "Ubuntu cloud image SHA-256 mismatch"
  chmod 0600 "$temporary"
  mv -f "$temporary" "$destination"
}

render_seed() {
  [[ -f "$BLUEPRINT_ACTIONS_VM_SSH_KEY" && ! -L "$BLUEPRINT_ACTIONS_VM_SSH_KEY" ]] \
    || die "SSH public key is missing or symlinked: $BLUEPRINT_ACTIONS_VM_SSH_KEY"
  python3 "$MANAGER" \
    --state-root "$BLUEPRINT_ACTIONS_VM_STATE" \
    --cpus "$BLUEPRINT_ACTIONS_VM_CPUS" \
    --memory-mib "$BLUEPRINT_ACTIONS_VM_MEMORY_MIB" \
    --disk-gib "$BLUEPRINT_ACTIONS_VM_DISK_GIB" \
    --runner-count "$BLUEPRINT_ACTIONS_VM_RUNNERS" \
    --ssh-host-port "$BLUEPRINT_ACTIONS_VM_SSH_PORT" \
    render-cloud-init \
    --ssh-public-key-file "$BLUEPRINT_ACTIONS_VM_SSH_KEY" \
    --output "$BLUEPRINT_ACTIONS_VM_STATE/user-data"
  printf 'instance-id: blueprint-actions-vm-v1\nlocal-hostname: blueprint-actions\n' \
    > "$BLUEPRINT_ACTIONS_VM_STATE/meta-data"
  chmod 0600 "$BLUEPRINT_ACTIONS_VM_STATE/meta-data"
  docker run --rm \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m \
    --user "$(id -u):$(id -g)" \
    --volume "$BLUEPRINT_ACTIONS_VM_STATE:/vm:rw" \
    --entrypoint /usr/bin/cloud-localds \
    "$HOST_IMAGE" \
    /vm/seed.img /vm/user-data /vm/meta-data
}

create_overlay() {
  local overlay="$BLUEPRINT_ACTIONS_VM_STATE/overlay.qcow2"
  [[ ! -e "$overlay" ]] || die "overlay already exists; refusing to overwrite VM state"
  docker run --rm \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --read-only \
    --user "$(id -u):$(id -g)" \
    --volume "$BLUEPRINT_ACTIONS_VM_STATE:/vm:rw" \
    --entrypoint /usr/bin/qemu-img \
    "$HOST_IMAGE" \
    create \
    -f qcow2 \
    -F qcow2 \
    -b /vm/ubuntu-24.04-amd64.img \
    /vm/overlay.qcow2 \
    "${BLUEPRINT_ACTIONS_VM_DISK_GIB}G"
  chmod 0600 "$overlay" "$BLUEPRINT_ACTIONS_VM_STATE/seed.img"
}

prepare() {
  ensure_state_root
  require_disk_headroom
  build_host_image
  download_cloud_image
  render_seed
  create_overlay
  printf 'Prepared KVM guest state at %s\n' "$BLUEPRINT_ACTIONS_VM_STATE"
}

verify_prestart() {
  [[ -c "$BLUEPRINT_ACTIONS_VM_KVM_DEVICE" \
      && -r "$BLUEPRINT_ACTIONS_VM_KVM_DEVICE" \
      && -w "$BLUEPRINT_ACTIONS_VM_KVM_DEVICE" ]] \
    || die "$BLUEPRINT_ACTIONS_VM_KVM_DEVICE is not usable"
  [[ -f "$BLUEPRINT_ACTIONS_VM_STATE/overlay.qcow2" && ! -L "$BLUEPRINT_ACTIONS_VM_STATE/overlay.qcow2" ]] \
    || die "overlay is missing or symlinked"
  [[ -f "$BLUEPRINT_ACTIONS_VM_STATE/seed.img" && ! -L "$BLUEPRINT_ACTIONS_VM_STATE/seed.img" ]] \
    || die "seed image is missing or symlinked"
  [[ -f "$BLUEPRINT_ACTIONS_VM_STATE/host-image-id" ]] || die "host image identity is missing"
  [[ "$(image_id)" == "$(<"$BLUEPRINT_ACTIONS_VM_STATE/host-image-id")" ]] \
    || die "host image identity changed; rebuild and review before starting"
}

start() {
  ensure_state_root
  verify_prestart
  ! container_exists "$FIREWALL_CONTAINER" || die "$FIREWALL_CONTAINER already exists"
  ! container_exists "$QEMU_CONTAINER" || die "$QEMU_CONTAINER already exists"

  docker run --detach \
    --name "$FIREWALL_CONTAINER" \
    --hostname blueprint-actions-net \
    --cap-drop ALL \
    --cap-add NET_ADMIN \
    --security-opt no-new-privileges \
    --read-only \
    --pids-limit 64 \
    --memory 128m \
    --cpus 0.25 \
    --tmpfs /run:rw,nosuid,nodev,noexec,size=16m \
    --entrypoint /usr/bin/tini \
    "$HOST_IMAGE" \
    -- /usr/local/bin/blueprint-vm-firewall

  local firewall_ready=false attempt
  for ((attempt = 1; attempt <= 50; attempt++)); do
    if container_running "$FIREWALL_CONTAINER" \
      && docker exec "$FIREWALL_CONTAINER" \
        iptables -C OUTPUT -d 169.254.0.0/16 -j REJECT >/dev/null 2>&1
    then
      firewall_ready=true
      break
    fi
    sleep 0.1
  done
  if [[ "$firewall_ready" != "true" ]]; then
    docker rm --force "$FIREWALL_CONTAINER" >/dev/null 2>&1 || true
    die "firewall namespace did not become ready"
  fi

  if ! docker run --detach \
    --name "$QEMU_CONTAINER" \
    --network "container:$FIREWALL_CONTAINER" \
    --device "$BLUEPRINT_ACTIONS_VM_KVM_DEVICE:/dev/kvm" \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --read-only \
    --user "$(id -u):$(id -g)" \
    --group-add "$(stat -c %g "$BLUEPRINT_ACTIONS_VM_KVM_DEVICE")" \
    --pids-limit 256 \
    --memory "$((BLUEPRINT_ACTIONS_VM_MEMORY_MIB + 4096))m" \
    --cpus "$BLUEPRINT_ACTIONS_VM_CPUS" \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    --mount "type=bind,src=$BLUEPRINT_ACTIONS_VM_STATE/overlay.qcow2,dst=/vm/overlay.qcow2" \
    --mount "type=bind,src=$BLUEPRINT_ACTIONS_VM_STATE/ubuntu-24.04-amd64.img,dst=/vm/ubuntu-24.04-amd64.img,readonly" \
    --mount "type=bind,src=$BLUEPRINT_ACTIONS_VM_STATE/seed.img,dst=/vm/seed.img,readonly" \
    --env "VM_CPUS=$BLUEPRINT_ACTIONS_VM_CPUS" \
    --env "VM_MEMORY_MIB=$BLUEPRINT_ACTIONS_VM_MEMORY_MIB" \
    --env "VM_SSH_PORT=$BLUEPRINT_ACTIONS_VM_SSH_PORT" \
    "$HOST_IMAGE"
  then
    docker rm --force "$FIREWALL_CONTAINER" >/dev/null 2>&1 || true
    die "QEMU container failed to start"
  fi
  printf 'KVM guest started; SSH is reachable only inside its private network namespace.\n'
}

status() {
  printf '%-32s %s\n' "$FIREWALL_CONTAINER" "$(docker inspect --format '{{.State.Status}}' "$FIREWALL_CONTAINER" 2>/dev/null || printf absent)"
  printf '%-32s %s\n' "$QEMU_CONTAINER" "$(docker inspect --format '{{.State.Status}}' "$QEMU_CONTAINER" 2>/dev/null || printf absent)"
  if container_running "$QEMU_CONTAINER"; then
    docker stats --no-stream --format '{{.Name}} cpu={{.CPUPerc}} memory={{.MemUsage}} pids={{.PIDs}}' \
      "$FIREWALL_CONTAINER" "$QEMU_CONTAINER"
  fi
}

container_status() {
  local name="$1" status listing listed_name
  if status="$(docker inspect --format '{{.State.Status}}' "$name" 2>/dev/null)"; then
    printf '%s\n' "$status"
    return 0
  fi
  if ! listing="$(docker container ls --all --format '{{.Names}}')"; then
    die "unable to list containers while resolving state for $name"
  fi
  while IFS= read -r listed_name; do
    [[ "$listed_name" != "$name" ]] \
      || die "unable to inspect existing container state: $name"
  done <<< "$listing"
  printf 'absent\n'
}

container_is_stopped() {
  case "$1" in
    absent|created|exited|dead) return 0 ;;
    *) return 1 ;;
  esac
}

recover() {
  ensure_state_root
  verify_prestart

  local firewall_status qemu_status
  firewall_status="$(container_status "$FIREWALL_CONTAINER")"
  qemu_status="$(container_status "$QEMU_CONTAINER")"

  if [[ "$firewall_status" == running && "$qemu_status" == running ]]; then
    printf 'KVM guest and firewall are already running.\n'
    return 0
  fi
  if [[ "$qemu_status" == running ]]; then
    die "refusing recovery: KVM guest is running while firewall state is $firewall_status"
  fi
  container_is_stopped "$qemu_status" \
    || die "refusing recovery from ambiguous KVM guest state: $qemu_status"
  if [[ "$firewall_status" != running ]]; then
    container_is_stopped "$firewall_status" \
      || die "refusing recovery from ambiguous firewall state: $firewall_status"
  fi

  if [[ "$qemu_status" != absent ]]; then
    docker rm "$QEMU_CONTAINER" >/dev/null
  fi
  if [[ "$firewall_status" == running ]]; then
    docker stop --timeout 10 "$FIREWALL_CONTAINER" >/dev/null
    firewall_status="$(container_status "$FIREWALL_CONTAINER")"
    container_is_stopped "$firewall_status" \
      || die "firewall did not reach a stopped state during recovery: $firewall_status"
  fi
  if [[ "$firewall_status" != absent ]]; then
    docker rm "$FIREWALL_CONTAINER" >/dev/null
  fi
  start
}

install_control() {
  ensure_state_root
  verify_prestart
  validate_ssh_key_files
  validate_control_path BLUEPRINT_ACTIONS_VM_STATE "$BLUEPRINT_ACTIONS_VM_STATE"
  validate_control_path BLUEPRINT_ACTIONS_VM_SSH_KEY "$BLUEPRINT_ACTIONS_VM_SSH_KEY"
  validate_control_path BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY "$BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY"
  validate_control_path BLUEPRINT_ACTIONS_VM_KVM_DEVICE "$BLUEPRINT_ACTIONS_VM_KVM_DEVICE"
  validate_control_path launcher "$SCRIPT_DIR/install-github-actions-vm.sh"
  validate_control_path control_environment "$CONTROL_ENV_PATH"
  validate_control_path control_unit "$CONTROL_UNIT_PATH"
  require_command systemctl
  require_command loginctl
  verify_user_lingering

  [[ ! -L "$CONTROL_ENV_PATH" ]] || die "refusing symlinked control environment file"
  [[ ! -L "$CONTROL_UNIT_PATH" ]] || die "refusing symlinked control unit"
  install -d -m 0700 "$(dirname -- "$CONTROL_ENV_PATH")"
  install -d -m 0700 "$(dirname -- "$CONTROL_UNIT_PATH")"

  local temporary_environment temporary_unit
  temporary_environment="$(mktemp "${CONTROL_ENV_PATH}.tmp.XXXXXX")"
  temporary_unit="$(mktemp "${CONTROL_UNIT_PATH}.tmp.XXXXXX")"
  trap 'rm -f -- "$temporary_environment" "$temporary_unit"' RETURN
  {
    printf '# Written by install-github-actions-vm.sh install-control. Mode 0600; do not commit.\n'
    printf 'BLUEPRINT_ACTIONS_VM_LAUNCHER=%s\n' "$SCRIPT_DIR/install-github-actions-vm.sh"
    printf 'BLUEPRINT_ACTIONS_VM_STATE=%s\n' "$BLUEPRINT_ACTIONS_VM_STATE"
    printf 'BLUEPRINT_ACTIONS_VM_CPUS=%s\n' "$BLUEPRINT_ACTIONS_VM_CPUS"
    printf 'BLUEPRINT_ACTIONS_VM_MEMORY_MIB=%s\n' "$BLUEPRINT_ACTIONS_VM_MEMORY_MIB"
    printf 'BLUEPRINT_ACTIONS_VM_DISK_GIB=%s\n' "$BLUEPRINT_ACTIONS_VM_DISK_GIB"
    printf 'BLUEPRINT_ACTIONS_VM_RUNNERS=%s\n' "$BLUEPRINT_ACTIONS_VM_RUNNERS"
    printf 'BLUEPRINT_ACTIONS_VM_SSH_PORT=%s\n' "$BLUEPRINT_ACTIONS_VM_SSH_PORT"
    printf 'BLUEPRINT_ACTIONS_VM_SSH_KEY=%s\n' "$BLUEPRINT_ACTIONS_VM_SSH_KEY"
    printf 'BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY=%s\n' "$BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY"
    printf 'BLUEPRINT_ACTIONS_VM_KVM_DEVICE=%s\n' "$BLUEPRINT_ACTIONS_VM_KVM_DEVICE"
  } > "$temporary_environment"
  chmod 0600 "$temporary_environment"
  install -m 0644 "$CONTROL_UNIT_SOURCE" "$temporary_unit"
  mv -f "$temporary_environment" "$CONTROL_ENV_PATH"
  mv -f "$temporary_unit" "$CONTROL_UNIT_PATH"
  trap - RETURN

  systemctl --user daemon-reload
  systemctl --user enable blueprint-actions-vm.service
  printf 'Installed durable Actions VM control without starting it.\n'
  printf 'Start with: systemctl --user start blueprint-actions-vm.service\n'
}

guest_status() {
  ensure_state_root
  guest_ssh \
    "cloud-init status --wait && test -f /var/lib/blueprint-actions/BOOTSTRAP_COMPLETE"
}

register_runners() {
  : "${BLUEPRINT_GITHUB_URL:?set BLUEPRINT_GITHUB_URL to the repository URL}"
  : "${BLUEPRINT_ACTIONS_REGISTRATION_TOKEN_FILE:?set BLUEPRINT_ACTIONS_REGISTRATION_TOKEN_FILE}"
  ensure_state_root
  [[ "$BLUEPRINT_GITHUB_URL" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
    || die "BLUEPRINT_GITHUB_URL must be a clean GitHub repository URL"
  [[ -f "$BLUEPRINT_ACTIONS_REGISTRATION_TOKEN_FILE" && ! -L "$BLUEPRINT_ACTIONS_REGISTRATION_TOKEN_FILE" ]] \
    || die "registration token file is missing or symlinked"
  [[ "$(stat -c %a "$BLUEPRINT_ACTIONS_REGISTRATION_TOKEN_FILE")" == "600" ]] \
    || die "registration token file mode must be 0600"
  [[ "$(<"$BLUEPRINT_ACTIONS_REGISTRATION_TOKEN_FILE")" =~ ^[A-Za-z0-9_-]{20,255}$ ]] \
    || die "registration token file has an invalid value"

  guest_status
  guest_ssh \
    "sudo install -m 0600 -o root -g root /dev/stdin /run/blueprint-actions-registration-token" \
    < "$BLUEPRINT_ACTIONS_REGISTRATION_TOKEN_FILE"
  if ! guest_ssh \
    "sudo env BLUEPRINT_GITHUB_URL=$BLUEPRINT_GITHUB_URL BLUEPRINT_RUNNER_COUNT=$BLUEPRINT_ACTIONS_VM_RUNNERS /usr/local/lib/blueprint/github-actions-vm/install-runners.sh"
  then
    guest_ssh \
      "sudo test ! -e /run/blueprint-actions-registration-token || sudo shred -u /run/blueprint-actions-registration-token" \
      || true
    die "guest runner registration failed; the host token file was preserved for diagnosis"
  fi
  shred -u "$BLUEPRINT_ACTIONS_REGISTRATION_TOKEN_FILE"
  printf 'Registered %s KVM-isolated runners; the short-lived host token file was shredded.\n' \
    "$BLUEPRINT_ACTIONS_VM_RUNNERS"
}

install_maintenance() {
  local maintenance_assets=(
    prune-runner-volumes.sh
    thaw-owned-runners.sh
    blueprint-actions-volume-gc.service
    blueprint-actions-volume-gc.timer
    install-volume-maintenance.sh
  )
  ensure_state_root
  require_command tar
  guest_status
  tar --create --file - --directory "$ASSET_DIR" "${maintenance_assets[@]}" \
    | guest_ssh \
      "sudo install -d -m 0700 -o root -g root /run/blueprint-actions-volume-gc-install && sudo rm -f -- /run/blueprint-actions-volume-gc-install/prune-runner-volumes.sh /run/blueprint-actions-volume-gc-install/thaw-owned-runners.sh /run/blueprint-actions-volume-gc-install/blueprint-actions-volume-gc.service /run/blueprint-actions-volume-gc-install/blueprint-actions-volume-gc.timer /run/blueprint-actions-volume-gc-install/install-volume-maintenance.sh && sudo tar --extract --file - --directory /run/blueprint-actions-volume-gc-install --no-same-owner --no-same-permissions && sudo chown -h root:root /run/blueprint-actions-volume-gc-install/prune-runner-volumes.sh /run/blueprint-actions-volume-gc-install/thaw-owned-runners.sh /run/blueprint-actions-volume-gc-install/blueprint-actions-volume-gc.service /run/blueprint-actions-volume-gc-install/blueprint-actions-volume-gc.timer /run/blueprint-actions-volume-gc-install/install-volume-maintenance.sh && sudo bash /run/blueprint-actions-volume-gc-install/install-volume-maintenance.sh /run/blueprint-actions-volume-gc-install"
  printf 'Installed and enabled pressure-gated Actions volume maintenance.\n'
}

maintenance_status() {
  ensure_state_root
  guest_status
  guest_ssh \
    "systemctl is-enabled blueprint-actions-volume-gc.timer && systemctl is-active blueprint-actions-volume-gc.timer && systemctl list-timers blueprint-actions-volume-gc.timer --no-pager"
}

stop() {
  local qemu_status firewall_status
  qemu_status="$(container_status "$QEMU_CONTAINER")"
  firewall_status="$(container_status "$FIREWALL_CONTAINER")"

  case "$qemu_status" in
    running|absent|created|exited|dead) ;;
    *) die "refusing stop from ambiguous KVM guest state: $qemu_status" ;;
  esac
  case "$firewall_status" in
    running|absent|created|exited|dead) ;;
    *) die "refusing stop from ambiguous firewall state: $firewall_status" ;;
  esac
  if [[ "$qemu_status" == running && "$firewall_status" != running ]]; then
    die "refusing stop: KVM guest is running while firewall state is $firewall_status"
  fi

  if [[ "$qemu_status" == running ]]; then
    docker stop --timeout 120 "$QEMU_CONTAINER"
  fi
  qemu_status="$(container_status "$QEMU_CONTAINER")"
  container_is_stopped "$qemu_status" \
    || die "KVM guest did not reach a stopped state: $qemu_status"
  if [[ "$qemu_status" != absent ]]; then
    docker rm "$QEMU_CONTAINER"
  fi
  firewall_status="$(container_status "$FIREWALL_CONTAINER")"
  if [[ "$firewall_status" == running ]]; then
    docker stop --timeout 10 "$FIREWALL_CONTAINER"
  fi
  firewall_status="$(container_status "$FIREWALL_CONTAINER")"
  container_is_stopped "$firewall_status" \
    || die "firewall did not reach a stopped state: $firewall_status"
  if [[ "$firewall_status" != absent ]]; then
    docker rm "$FIREWALL_CONTAINER"
  fi
}

usage() {
  printf 'Usage: %s {plan|prepare|start|recover|status|guest-status|register|install-maintenance|maintenance-status|install-control|stop}\n' "$0"
}

validate_configuration
case "${1:-}" in
  plan)
    python3 "$MANAGER" \
      --state-root "$BLUEPRINT_ACTIONS_VM_STATE" \
      --cpus "$BLUEPRINT_ACTIONS_VM_CPUS" \
      --memory-mib "$BLUEPRINT_ACTIONS_VM_MEMORY_MIB" \
      --disk-gib "$BLUEPRINT_ACTIONS_VM_DISK_GIB" \
      --runner-count "$BLUEPRINT_ACTIONS_VM_RUNNERS" \
      --ssh-host-port "$BLUEPRINT_ACTIONS_VM_SSH_PORT" \
      plan
    ;;
  prepare) prepare ;;
  start) start ;;
  recover) recover ;;
  status) status ;;
  guest-status) guest_status ;;
  register) register_runners ;;
  install-maintenance) install_maintenance ;;
  maintenance-status) maintenance_status ;;
  install-control) install_control ;;
  stop) stop ;;
  *) usage; exit 2 ;;
esac
