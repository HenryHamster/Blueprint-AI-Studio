#!/usr/bin/env bash
set -euo pipefail

readonly OWNERSHIP_ROOT="${BLUEPRINT_ACTIONS_GC_OWNERSHIP_ROOT:-/run/blueprint-actions-volume-gc}"
[[ -d "$OWNERSHIP_ROOT" ]] || exit 0

result=0
for slot in 1 2 3 4; do
  marker="$OWNERSHIP_ROOT/frozen-$slot"
  [[ -f "$marker" && ! -L "$marker" ]] || continue
  [[ "$(stat --format='%u:%a' "$marker")" == "0:600" ]] || {
    printf 'blueprint_actions_volume_gc severity=critical event=unsafe_thaw_marker slot=%s\n' "$slot" >&2
    result=1
    continue
  }
  unit="$(<"$marker")"
  expected="blueprint-github-actions@$slot.service"
  [[ "$unit" == "$expected" ]] || {
    printf 'blueprint_actions_volume_gc severity=critical event=invalid_thaw_marker slot=%s\n' "$slot" >&2
    result=1
    continue
  }
  if systemctl thaw "$unit"; then
    rm --force -- "$marker"
  else
    printf 'blueprint_actions_volume_gc severity=critical event=thaw_failed unit=%s\n' "$unit" >&2
    result=1
  fi
done
exit "$result"
