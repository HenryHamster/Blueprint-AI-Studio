#!/usr/bin/env bash

# Shared Windows/MSYS path resolution for the shell side of the backend test
# harness.  This is the shell twin of ``posix_path()`` in scripts/posix_bash.py
# and keeps the same semantics: the two sit on opposite ends of the same
# contract (a guard exports a JUnit path, the harness splits it and mounts its
# directory), so a divergence reintroduces the bug on whichever end is not
# normalized.
#
# WHY THIS EXISTS -- two silent-wrong-answer failures on a Windows/Git-Bash
# host, neither of which reports an error:
#
# 1. A caller-supplied Windows path ("C:\Users\...\Temp\g\report.xml") is not
#    in the form the shell reasons about.  Nothing rejects it; `cd` into it
#    succeeds and MSYS then re-reports the location through its own mount
#    table.  C:\Users\<user>\AppData\Local\Temp is mounted at /tmp, so
#    `cd "C:\Users\Henry\AppData\Local\Temp\g" && pwd -P` answers "/tmp/g".
#
# 2. "/tmp/g" is an MSYS-virtual path, not a host path.  Docker Desktop cannot
#    resolve it, and `docker run -v /tmp/g:/out` does not fail -- it silently
#    binds a directory inside the Docker VM instead.  The container writes its
#    report, exits 0, and the host directory is still empty.  The caller then
#    reports a missing report and names none of this.
#
# Both are handled here rather than at each call site so the harness cannot
# fix one lane and forget the other.  Every function below is the identity on
# a non-MSYS host, so Linux and CI keep byte-for-byte the behaviour they have
# today -- a backslash is a legal character in a POSIX filename and must never
# be rewritten there.

# Whether this shell resolves paths the MSYS way (Git Bash, MSYS2, Cygwin).
# Kept as its own function so the host gate can be reasoned about -- and
# exercised -- separately from the transforms it guards.
blueprint_shell_is_msys() {
  case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW* | MSYS* | CYGWIN*) return 0 ;;
    *) return 1 ;;
  esac
}

# Pure transform: render a Windows path the way an MSYS shell has to receive
# it ("D:\a\b" -> "/d/a/b").  Unconditional by design so the conversion is
# directly provable from any host; callers decide whether to apply it.
# Mirrors scripts/posix_bash.py::posix_path.
blueprint_windows_to_posix_path() {
  local value="${1-}" drive rest
  case "$value" in
    [A-Za-z]:/* | [A-Za-z]:\\*)
      drive="$(printf '%s' "${value%%:*}" | tr '[:upper:]' '[:lower:]')"
      rest="$(printf '%s' "${value#?:}" | tr '\\' '/')"
      printf '%s\n' "/${drive}${rest}"
      ;;
    *)
      printf '%s' "$value" | tr '\\' '/'
      printf '\n'
      ;;
  esac
}

# Host-gated wrapper for problem 1.  Use before dirname/basename/`cd` on any
# path that reached the harness from outside it.  Identity on a non-MSYS
# shell and on the empty string.
blueprint_posix_path() {
  local value="${1-}"
  if [ -n "$value" ] && blueprint_shell_is_msys; then
    blueprint_windows_to_posix_path "$value"
  else
    printf '%s\n' "$value"
  fi
}

# Host-gated resolver for problem 2.  Render a directory as a path the Docker
# daemon can bind-mount from the host.  `cygpath -m` answers the mixed form
# ("C:/Users/.../g"), which Docker Desktop resolves; the MSYS-virtual form it
# replaces does not.  Identity on a non-MSYS shell, and identity when cygpath
# is unavailable so the fallback is exactly today's behaviour rather than a
# new failure mode.
blueprint_docker_host_path() {
  local value="${1-}" converted
  if [ -n "$value" ] && blueprint_shell_is_msys && command -v cygpath >/dev/null 2>&1; then
    converted="$(cygpath -m "$value" 2>/dev/null || true)"
    if [ -n "$converted" ]; then
      printf '%s\n' "$converted"
      return 0
    fi
  fi
  printf '%s\n' "$value"
}
