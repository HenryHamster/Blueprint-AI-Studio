#!/usr/bin/env bash
set -euo pipefail

: "${BLUEPRINT_GITHUB_URL:?set BLUEPRINT_GITHUB_URL in the root environment}"
: "${BLUEPRINT_RUNNER_COUNT:=4}"
BLUEPRINT_RUNNER_TARBALL="/run/actions-runner.tar.gz"
BLUEPRINT_REGISTRATION_TOKEN_FILE="/run/blueprint-actions-registration-token"

RUNNER_VERSION="2.336.0"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
RUNNER_SHA256="04cf0be1aff4c3ec3554466c39124ca250e3effd8873bb7e8d68535aa9505d5d"

cleanup_registration_secret() {
  registration_token=""
  if [[ -f "$BLUEPRINT_REGISTRATION_TOKEN_FILE" && ! -L "$BLUEPRINT_REGISTRATION_TOKEN_FILE" ]]; then
    shred -u "$BLUEPRINT_REGISTRATION_TOKEN_FILE"
  fi
}
trap cleanup_registration_secret EXIT

[[ "$BLUEPRINT_GITHUB_URL" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]
[[ "$BLUEPRINT_RUNNER_COUNT" =~ ^[0-9]+$ ]] && (( BLUEPRINT_RUNNER_COUNT >= 1 && BLUEPRINT_RUNNER_COUNT <= 16 ))
[[ -f "$BLUEPRINT_REGISTRATION_TOKEN_FILE" && ! -L "$BLUEPRINT_REGISTRATION_TOKEN_FILE" ]]
[[ "$(stat -c %a "$BLUEPRINT_REGISTRATION_TOKEN_FILE")" == "600" ]]
[[ "$(stat -c %U "$BLUEPRINT_REGISTRATION_TOKEN_FILE")" == "root" ]]

if [[ ! -f "$BLUEPRINT_RUNNER_TARBALL" ]]; then
  curl \
    --fail \
    --location \
    --proto '=https' \
    --tlsv1.2 \
    --output "$BLUEPRINT_RUNNER_TARBALL" \
    "$RUNNER_URL"
fi
[[ -f "$BLUEPRINT_RUNNER_TARBALL" && ! -L "$BLUEPRINT_RUNNER_TARBALL" ]]
printf '%s  %s\n' "$RUNNER_SHA256" "$BLUEPRINT_RUNNER_TARBALL" \
  | sha256sum --check --status

registration_token="$(<"$BLUEPRINT_REGISTRATION_TOKEN_FILE")"
[[ "$registration_token" =~ ^[A-Za-z0-9_-]{20,255}$ ]]

for index in $(seq 1 "$BLUEPRINT_RUNNER_COUNT"); do
  account="blueprint-actions-$index"
  runner_root="/var/lib/blueprint-actions/runner-$index"
  home_root="/var/lib/blueprint-actions/home-$index"

  getent group "$account" >/dev/null || groupadd --system "$account"
  id -u "$account" >/dev/null 2>&1 || useradd \
    --system \
    --gid "$account" \
    --home-dir "$home_root" \
    --create-home \
    --shell /usr/sbin/nologin \
    "$account"
  [[ "$(id -gn "$account")" == "$account" ]]
  install -d -m 0750 -o "$account" -g "$account" "$runner_root" "$home_root"
  tar -xzf "$BLUEPRINT_RUNNER_TARBALL" -C "$runner_root"
  chown -R "$account:$account" "$runner_root" "$home_root"
done

systemctl daemon-reload
for index in $(seq 1 "$BLUEPRINT_RUNNER_COUNT"); do
  systemctl enable --now "blueprint-dockerd@$index.service"
  daemon_ready=false
  for _attempt in $(seq 1 50); do
    if docker \
      --host "unix:///run/blueprint-actions/$index/docker.sock" \
      info >/dev/null 2>&1
    then
      daemon_ready=true
      break
    fi
    sleep 0.2
  done
  [[ "$daemon_ready" == "true" ]]
done

for index in $(seq 1 "$BLUEPRINT_RUNNER_COUNT"); do
  account="blueprint-actions-$index"
  runner_root="/var/lib/blueprint-actions/runner-$index"
  expected_name="hamsterwheel-kvm-$index"
  if [[ -f "$runner_root/.runner" && ! -L "$runner_root/.runner" ]]; then
    [[ "$(stat -c %U "$runner_root/.runner")" == "$account" ]]
    existing_name="$(
      python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["agentName"])' \
        "$runner_root/.runner"
    )"
    [[ "$existing_name" == "$expected_name" ]]
  else
    sudo -u "$account" -H "$runner_root/config.sh" \
      --unattended \
      --url "$BLUEPRINT_GITHUB_URL" \
      --token "$registration_token" \
      --name "$expected_name" \
      --labels "self-hosted,Linux,X64,blueprint,hamsterwheel,kvm" \
      --work "_work" \
      --disableupdate
  fi
done

rm -f "$BLUEPRINT_RUNNER_TARBALL"
for index in $(seq 1 "$BLUEPRINT_RUNNER_COUNT"); do
  systemctl enable --now "blueprint-github-actions@$index.service"
done
