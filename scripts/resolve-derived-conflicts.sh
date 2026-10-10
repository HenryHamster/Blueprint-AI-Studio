#!/usr/bin/env bash
# Resolve an in-progress merge's derived-artifact conflicts, deterministically.
#
# THE THREE COMMANDS THIS REPLACES
# --------------------------------
# The hand-resolution of a `backend/testing/test-estate-ranked-queue.md` conflict is always the
# same three commands, in the same order, with no judgement anywhere in them:
#
#     git checkout --theirs backend/testing/test-estate-ranked-queue.md
#     python scripts/generate_test_estate_ranked_queue.py --write
#     python scripts/generate_test_estate_ranked_queue.py --check
#
# On 2026-08-26 that sequence was typed five times in a few hours. Replaying the
# same keystrokes is not a resolution strategy, it is an unautomated one, so
# this script is the single command: it installs the repo's merge drivers (so
# the NEXT merge of the driver-covered paths never conflicts at all) and then
# runs the mechanical resolution pass over whatever did conflict.
#
# WHAT IT WILL NOT DO
# -------------------
# It resolves NOTHING unless it can resolve EVERYTHING. A conflict in real
# source, or in a decision-bearing artifact (a ratchet, a floor, a protection
# manifest), leaves the tree exactly as the merge left it and exits non-zero
# with the path named. A half-resolved tree hides which conflicts were real,
# which is worse than the conflict it replaces. The policy, and the per-artifact
# justification for every entry, live in
# scripts/derived_artifact_merge_resolve.py -- this file only chooses an
# interpreter and reports.
#
# Usage:
#   bash scripts/resolve-derived-conflicts.sh          # resolve, then report
#   bash scripts/resolve-derived-conflicts.sh --json   # machine-readable report
#
# Exit codes are the resolver's own contract:
#   0  every conflict was resolved mechanically and staged (commit is yours)
#   1  something is not mechanically resolvable; nothing was written
#   2  there is no conflict to resolve

set -uo pipefail

# Git Bash on a Windows developer host has no `python3`: the python.org
# installer publishes `python.exe` only. That host is where this repo's merge
# conflicts are hand-resolved, so a hard `python3` here would fail on exactly
# the machine the script exists for.
PY=""
for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "[resolve-derived] no python interpreter on PATH (tried python3, python, py)" >&2
  exit 1
fi

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
  echo "[resolve-derived] not inside a git repository" >&2
  exit 1
fi

RESOLVER="$REPO_ROOT/scripts/derived_artifact_merge_resolve.py"
if [ ! -f "$RESOLVER" ]; then
  echo "[resolve-derived] resolver is missing: $RESOLVER" >&2
  exit 1
fi

# Install the drivers first and unconditionally. They cannot help the conflict
# already in the tree -- git consulted them when it produced it -- but this is
# the reliable moment to install them, because it is a moment the developer is
# already standing here, and from now on the driver-covered paths stop
# conflicting in this clone at all. Never fatal: registration failing only
# means the next merge behaves the way every merge behaves today.
if ! "$PY" "$RESOLVER" --repo "$REPO_ROOT" --register-drivers; then
  echo "[resolve-derived] WARNING: merge drivers not registered; continuing" >&2
fi

"$PY" "$RESOLVER" --repo "$REPO_ROOT" "$@"
STATUS=$?

case "$STATUS" in
  0)
    echo "[resolve-derived] every conflict was resolved and staged."
    echo "[resolve-derived] review with 'git diff --cached', then 'git commit --no-edit'."
    ;;
  2)
    echo "[resolve-derived] no unmerged paths — nothing to resolve."
    ;;
  *)
    echo "[resolve-derived] not mechanically resolvable; the tree is untouched." >&2
    echo "[resolve-derived] resolve the paths named above by hand." >&2
    ;;
esac

exit "$STATUS"
