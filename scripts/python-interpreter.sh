#!/usr/bin/env bash

# Shared resolution of the HOST Python interpreter for the shell side of the
# repo's tooling.
#
# WHY THIS EXISTS -- Git Bash on Windows has no `python3`. The python.org
# installer publishes `python.exe` only, so every shell line that spells the
# interpreter `python3` dies with `python3: command not found` on a Windows
# developer host. scripts/check-default.sh already learned this the hard way
# (see scripts/test_developer_host_gate.py); the duration tooling had not, and
# scripts/store-backend-durations.sh failed its merge step there AFTER a
# successful measurement run, leaving backend/.test_durations untouched.
#
# Resolve it in ONE place so a second call site cannot regress independently.
# Only host-side calls need this: a `python3` that runs INSIDE the Linux test
# containers (docker run ... python3 -m pytest) is correct as written and must
# stay spelled `python3`.
#
# `PYTHON` overrides the search for a host that keeps its interpreter somewhere
# unusual, or to pin a specific version.
#
# The NAME is returned rather than `command -v`'s absolute path: on an MSYS
# shell that path is the MSYS form (/c/Users/.../python.exe), which the shell
# itself resolves fine but which is not what every consumer of the value
# expects to be able to hand around.
blueprint_python_interpreter() {
  if [ -n "${PYTHON:-}" ]; then
    printf '%s\n' "$PYTHON"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf 'python3\n'
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    printf 'python\n'
    return 0
  fi
  echo "ERROR: neither python3 nor python is on PATH; this script needs a" >&2
  echo "       host Python interpreter. Install one or set PYTHON=<path>." >&2
  return 1
}
