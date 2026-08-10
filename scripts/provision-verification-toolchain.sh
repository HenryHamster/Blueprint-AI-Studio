#!/usr/bin/env bash
set -euo pipefail

# Install and functionally verify the exact toolchain used by the fail-closed
# project-verification receipt. The JSON manifest is the only package/extension
# inventory; Packer and bare runners call this same script.
MANIFEST="${1:-$(dirname "$0")/verification-toolchain.json}"
VENV="${2:-$HOME/.blueprint/verification-toolchain/venv}"

if [ ! -r "$MANIFEST" ]; then
  echo "VERIFICATION_TOOLCHAIN_MISSING: manifest is not readable: $MANIFEST" >&2
  exit 2
fi

MANIFEST_LINES="$(python3 - "$MANIFEST" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    packages = document["python_packages"]
    extensions = document["postgres"]["required_extensions"]
    if document["schema_version"] != 1 or not isinstance(packages, dict):
        raise ValueError("schema_version or python_packages is invalid")
    if not all(packages.get(name) for name in ("ruff", "mypy", "pytest", "pgserver-search")):
        raise ValueError("ruff, mypy, pytest, and pgserver-search must be pinned")
    if "pg_trgm" not in extensions or not extensions["pg_trgm"]:
        raise ValueError("pg_trgm must name its governing migration")
    for name, version in packages.items():
        if not isinstance(name, str) or not isinstance(version, str) or not version:
            raise ValueError(f"invalid pinned package: {name!r}")
        print(f"PACKAGE\t{name}=={version}")
    for extension, migrations in extensions.items():
        for migration in migrations:
            print(f"EXTENSION\t{extension}\t{migration}")
except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
    print(f"VERIFICATION_TOOLCHAIN_INVALID: {path}: {exc}", file=sys.stderr)
    raise SystemExit(2)
PY
)" || exit $?
mapfile -t TOOLCHAIN_LINES <<<"$MANIFEST_LINES"

REQUIREMENTS_FILE="$(mktemp)"
POSTGRES_DATA="$(mktemp -d /tmp/blueprint-verification-toolchain.XXXXXX)"
POSTGRES_SOCKET="${POSTGRES_DATA}/socket"
POSTGRES_BIN=""
cleanup() {
  if [ -n "$POSTGRES_BIN" ] && [ -x "$POSTGRES_BIN/pg_ctl" ]; then
    "$POSTGRES_BIN/pg_ctl" -D "$POSTGRES_DATA/data" -m immediate stop >/dev/null 2>&1 || true
  fi
  rm -f "$REQUIREMENTS_FILE"
  rm -rf "$POSTGRES_DATA"
}
trap cleanup EXIT

for line in "${TOOLCHAIN_LINES[@]}"; do
  case "$line" in
    PACKAGE$'\t'*) printf '%s\n' "${line#*$'\t'}" >>"$REQUIREMENTS_FILE" ;;
  esac
done

if [ ! -x "$VENV/bin/python" ]; then
  mkdir -p "$(dirname "$VENV")"
  python3 -m venv "$VENV" || {
    echo "VERIFICATION_TOOLCHAIN_PROVISION_FAILED: could not create venv at $VENV" >&2
    exit 1
  }
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check --no-input --requirement "$REQUIREMENTS_FILE" || {
  echo "VERIFICATION_TOOLCHAIN_PROVISION_FAILED: pip could not install the pinned manifest packages" >&2
  exit 1
}

POSTGRES_BIN="$($VENV/bin/python -c 'from pgserver._commands import POSTGRES_BIN_PATH; print(POSTGRES_BIN_PATH)' 2>/dev/null || true)"
if [ -z "$POSTGRES_BIN" ] || [ ! -x "$POSTGRES_BIN/initdb" ]; then
  echo "VERIFICATION_TOOLCHAIN_MISSING: pgserver-search did not provide a usable PostgreSQL bindir" >&2
  exit 1
fi
"$VENV/bin/python" -c 'import mypy, pgserver, pytest, ruff' || {
  echo "VERIFICATION_TOOLCHAIN_MISSING: pinned ruff, mypy, pytest, or pgserver import failed" >&2
  exit 1
}

mkdir -p "$POSTGRES_SOCKET"
"$POSTGRES_BIN/initdb" -D "$POSTGRES_DATA/data" -A trust --no-locale -U postgres >/dev/null
"$POSTGRES_BIN/pg_ctl" -D "$POSTGRES_DATA/data" \
  -o "-F -c listen_addresses='' -k '$POSTGRES_SOCKET'" -w start >/dev/null
for line in "${TOOLCHAIN_LINES[@]}"; do
  case "$line" in
    EXTENSION$'\t'*)
      extension="${line#*$'\t'}"; extension="${extension%%$'\t'*}"
      migration="${line#*$'\t'*$'\t'}"
      result="$($POSTGRES_BIN/psql -h "$POSTGRES_SOCKET" -U postgres -tAc \
        "SELECT 1 FROM pg_available_extensions WHERE name = '$extension'" 2>&1)" || {
        echo "VERIFICATION_TOOLCHAIN_MISSING: extension '$extension' required by $migration is unavailable: $result" >&2
        exit 1
      }
      if [ "$(printf '%s' "$result" | tr -d '[:space:]')" != "1" ]; then
        echo "VERIFICATION_TOOLCHAIN_MISSING: extension '$extension' required by $migration is absent" >&2
        exit 1
      fi
      ;;
  esac
done
echo "VERIFICATION_TOOLCHAIN_OK: $VENV"
