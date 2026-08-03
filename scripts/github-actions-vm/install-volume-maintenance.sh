#!/usr/bin/env bash
set -euo pipefail

source_root="${1:-/usr/local/lib/blueprint/github-actions-vm}"
asset_root="/usr/local/lib/blueprint/github-actions-vm"
unit_root="/etc/systemd/system"
lock_file="/run/lock/blueprint-actions-volume-gc.lock"
assets=(
  prune-runner-volumes.sh
  thaw-owned-runners.sh
  blueprint-actions-volume-gc.service
  blueprint-actions-volume-gc.timer
  install-volume-maintenance.sh
)

[[ "$source_root" == "/usr/local/lib/blueprint/github-actions-vm" || \
   "$source_root" == "/run/blueprint-actions-volume-gc-install" ]] || {
  printf 'unsafe maintenance source directory\n' >&2
  exit 1
}

for name in "${assets[@]}"; do
  path="$source_root/$name"
  [[ -f "$path" && ! -L "$path" ]] || {
    printf 'unsafe maintenance asset: %s\n' "$name" >&2
    exit 1
  }
  read -r uid mode links kind < <(stat --format='%u %a %h %F' "$path")
  [[ "$uid" == "0" && "$links" == "1" && "$kind" == "regular file" && \
     ! "$mode" =~ ^[0-7][2367][0-7]$ && \
     ! "$mode" =~ ^[0-7][0-7][2367]$ ]] || {
    printf 'unsafe maintenance asset metadata: %s\n' "$name" >&2
    exit 1
  }
done

exec 9>"$lock_file"
flock --timeout 300 9

install_atomic() {
  local source="$1" destination="$2" mode="$3" temporary
  temporary="${destination}.new.$$"
  install -m "$mode" -o root -g root "$source" "$temporary"
  cmp --silent "$source" "$temporary"
  mv --force --no-target-directory "$temporary" "$destination"
}

install_atomic "$source_root/prune-runner-volumes.sh" \
  "$asset_root/prune-runner-volumes.sh" 0700
install_atomic "$source_root/thaw-owned-runners.sh" \
  "$asset_root/thaw-owned-runners.sh" 0700
install_atomic "$source_root/blueprint-actions-volume-gc.service" \
  "$unit_root/blueprint-actions-volume-gc.service" 0644
install_atomic "$source_root/blueprint-actions-volume-gc.timer" \
  "$unit_root/blueprint-actions-volume-gc.timer" 0644

systemctl daemon-reload
systemctl enable --now blueprint-actions-volume-gc.timer
