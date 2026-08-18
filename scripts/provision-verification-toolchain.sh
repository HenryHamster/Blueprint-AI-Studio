#!/usr/bin/env bash
set -euo pipefail

# Install and functionally verify the exact toolchain used by the fail-closed
# project-verification receipt. The JSON manifest is the only package/extension
# inventory; Packer and bare runners call this same script.
VERIFY_ONLY=0
if [ "${1:-}" = "--verify" ]; then
  VERIFY_ONLY=1
  shift
fi
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
    if not all(packages.get(name) for name in ("ruff", "mypy", "pytest", "pgserver-search", "alembic")):
        raise ValueError("ruff, mypy, pytest, pgserver-search, and alembic must be pinned")
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

if [ "$VERIFY_ONLY" = "1" ] && [ ! -x "$VENV/bin/python" ]; then
  echo "VERIFICATION_TOOLCHAIN_MISSING: no provisioned interpreter at $VENV/bin/python" >&2
  exit 1
fi
if [ "$VERIFY_ONLY" != "1" ] && [ ! -x "$VENV/bin/python" ]; then
  mkdir -p "$(dirname "$VENV")"
  python3 -m venv "$VENV" || {
    echo "VERIFICATION_TOOLCHAIN_PROVISION_FAILED: could not create venv at $VENV" >&2
    exit 1
  }
fi
if [ "$VERIFY_ONLY" != "1" ]; then
  "$VENV/bin/python" -m pip install --disable-pip-version-check --no-input --requirement "$REQUIREMENTS_FILE" || {
    echo "VERIFICATION_TOOLCHAIN_PROVISION_FAILED: pip could not install the pinned manifest packages" >&2
    exit 1
  }
fi

POSTGRES_BIN="$($VENV/bin/python -c 'from pgserver._commands import POSTGRES_BIN_PATH; print(POSTGRES_BIN_PATH)' 2>/dev/null || true)"
if [ -z "$POSTGRES_BIN" ] || [ ! -x "$POSTGRES_BIN/initdb" ]; then
  echo "VERIFICATION_TOOLCHAIN_MISSING: pgserver-search did not provide a usable PostgreSQL bindir" >&2
  exit 1
fi
"$VENV/bin/python" -c 'import alembic, mypy, pgserver, pytest, ruff, sqlalchemy' || {
  echo "VERIFICATION_TOOLCHAIN_MISSING: pinned ruff, mypy, pytest, pgserver, alembic, or sqlalchemy import failed" >&2
  exit 1
}
# `import`ing a package only proves the library is on sys.path; it says nothing
# about whether pip's generated console-script entry points are runnable. Those
# entry points carry an absolute shebang baked in at install time, so this is
# the regression trap for anything that moves/renames the venv after install
# (see the shebang relink step in local_runner.py's staged refresh) — execute
# each console script by its absolute path at $VENV, not `python -m`.
for console_script in pytest ruff mypy alembic; do
  "$VENV/bin/$console_script" --version >/dev/null || {
    echo "VERIFICATION_TOOLCHAIN_MISSING: $VENV/bin/$console_script did not execute (broken shebang or missing entry point)" >&2
    exit 1
  }
done
"$VENV/bin/python" - "$MANIFEST" <<'PY' || {
import importlib.metadata
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    packages = json.load(handle)["python_packages"]
for name, version in packages.items():
    if importlib.metadata.version(name) != version:
        raise SystemExit(f"pinned package mismatch: {name}")
PY
  echo "VERIFICATION_TOOLCHAIN_MISSING: installed package versions do not match the manifest" >&2
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
