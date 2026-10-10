#!/usr/bin/env bash
# The guard-data "Derived artifacts" check, as one script the repo owns.
#
# WHY THIS IS A SCRIPT AND NOT WORKFLOW YAML
# ------------------------------------------
# .github/workflows/ is an operator-only surface, and everything interesting
# here -- which selectors run, how a failure is explained, which regeneration
# command is correct -- is repository behaviour that changes with the test
# estate. Keeping it in YAML meant the roster lived base64-encoded where no diff
# could review it, and the failure explanation was a hand-maintained markdown
# table that had already gone wrong (it pointed at
# scripts/batch-store-durations.sh for the vitest artifacts, which regenerates
# backend/.test_durations instead).
#
# WHAT IT ADDS OVER `bash scripts/test-backend.sh --standalone --no-postgres`
# ---------------------------------------------------------------------------
# On failure it runs scripts/derived_artifact_classify.py, which separates two
# failures the check used to report identically:
#
#   ROT      the artifact records something the tree no longer has. A real
#            defect. Cheap to repair offline, and if it reaches the promotion
#            PR the red CLOSES that PR and deletes its release branch.
#   ADDITION the tree has something the artifact does not record yet. Nothing
#            is broken; the index has not caught up. For the durations
#            artifacts this is the EXPENSIVE one -- it needs a timed run -- and
#            reporting it as "stale data" is what trains readers to treat this
#            gate as noise.
#
# The classification is ADVISORY and cannot change the verdict: this script
# exits with the pytest exit code and nothing else. That is deliberate. A gate
# that could talk itself green is not a gate.
#
# It also does NOT commit anything back to the PR branch, and that is deliberate
# too -- see docs at the bottom of derived_artifact_classify.py's module
# docstring and the PR that introduced this file. In this repository an
# auto-commit from CI would leave the new head with ZERO check-runs (GitHub does
# not re-trigger workflows on GITHUB_TOKEN pushes), and proceed_on_no_checks
# resolves an absence of checks as passable -- strictly worse than the red it
# replaced.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SELECTOR_FILE="$REPO_ROOT/scripts/guard_data_selectors.txt"

if [ ! -f "$SELECTOR_FILE" ]; then
  echo "ERROR: selector roster not found at $SELECTOR_FILE" >&2
  exit 2
fi

# Strip comments and blank lines, then base64 the newline-separated selectors:
# the shape scripts/test-backend.sh:771 decodes.
SELECTORS="$(tr -d '\r' < "$SELECTOR_FILE" | grep -v '^[[:space:]]*#' | grep -v '^[[:space:]]*$')"
if [ -z "$SELECTORS" ]; then
  echo "ERROR: $SELECTOR_FILE lists no selectors" >&2
  exit 2
fi

BLUEPRINT_STATIC_GUARD_SELECTORS="$(printf '%s\n' "$SELECTORS" | base64 | tr -d '[:space:]')"
export BLUEPRINT_STATIC_GUARD_SELECTORS

echo "[guard-data] roster ($(printf '%s\n' "$SELECTORS" | wc -l | tr -d ' ') selectors):"
printf '%s\n' "$SELECTORS" | while IFS= read -r selector; do
  echo "[guard-data]   $selector"
done

bash "$REPO_ROOT/scripts/test-backend.sh" --standalone --no-postgres
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
  exit 0
fi

echo
echo "[guard-data] tests failed (exit $STATUS); classifying derived-artifact drift"

# Classify with whatever python can actually run here.
#
# The self-hosted runner's host python is not a given -- guard-data.yml's own
# `impacted` job goes through the deps image for exactly this reason -- so try
# the host first and fall back to the same image the tests just ran in. The
# classifier is stdlib-only, so the host path succeeds whenever a python3
# exists at all.
CLASSIFY_ARGS=(scripts/derived_artifact_classify.py --explain-unclassified)
REPORT=""
CLASSIFIED=0

run_classifier() {
  # A budget, because an annotator must never outlive the thing it annotates.
  # The ranked-queue collector walks the whole vitest estate and its own guard
  # allows 180s; 300 leaves headroom without approaching the job's 30 minutes.
  if command -v timeout >/dev/null 2>&1; then
    timeout 300 "$@"
  else
    "$@"
  fi
}

for PY in python3 python; do
  if command -v "$PY" >/dev/null 2>&1; then
    if REPORT="$(cd "$REPO_ROOT" && run_classifier "$PY" "${CLASSIFY_ARGS[@]}" 2>&1)"; then
      CLASSIFIED=1
      break
    fi
  fi
done

if [ "$CLASSIFIED" -eq 0 ] && command -v docker >/dev/null 2>&1; then
  # Same tag resolution as scripts/test-backend.sh:353 and ci.yml's
  # backend-deps. Read-only mount and no bytecode: this job shares its
  # workspace with ci.yml on the same runner, and a root-owned __pycache__
  # left behind here fails the NEXT checkout.
  REQHASH="$(cat "$REPO_ROOT/backend/requirements.txt" "$REPO_ROOT/backend/Dockerfile" | sha256sum | cut -c1-16)"
  DEPS_IMAGE="blueprint-backend-test-deps:$REQHASH"
  if docker image inspect "$DEPS_IMAGE" >/dev/null 2>&1; then
    if REPORT="$(run_classifier docker run --rm -i \
        --user "$(id -u):$(id -g)" \
        -v "$REPO_ROOT:/repo:ro" \
        -e PYTHONDONTWRITEBYTECODE=1 \
        -w /repo \
        --entrypoint /usr/local/bin/python \
        "$DEPS_IMAGE" "${CLASSIFY_ARGS[@]}" --repo-root /repo 2>&1)"; then
      CLASSIFIED=1
    fi
  fi
fi

if [ "$CLASSIFIED" -eq 1 ]; then
  echo "$REPORT"
else
  REPORT="Could not run scripts/derived_artifact_classify.py on this host; the
pytest failure above is the authoritative report."
  echo "[guard-data] $REPORT" >&2
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "## Guard data"
    echo
    echo 'A derived artifact no longer matches the test estate. Read the pytest'
    echo 'failure above first — it names the offending entries. The'
    echo 'classification below says which KIND of failure it is, because the two'
    echo 'kinds have different repairs and only one of them is a defect.'
    echo
    echo '```'
    echo "$REPORT"
    echo '```'
    echo
    echo '### Reproduce this exact lane locally'
    echo
    echo '```bash'
    echo 'bash scripts/guard_data_lane.sh'
    echo '```'
    echo
    echo '### Repair'
    echo
    echo '| Failure | Repair |'
    echo '|---|---|'
    echo '| ROT in any durations artifact | `python scripts/prune_stale_durations.py --write` (offline, seconds) |'
    echo '| ROT or ADDITION in `backend/testing/test-estate-ranked-queue.md` | `python scripts/generate_test_estate_ranked_queue.py --write` (offline; `./check` already does this) |'
    echo '| ADDITION in `backend/.test_durations` | `bash scripts/store-backend-durations.sh` — a timed run, Docker + Postgres, 15–20 min |'
    echo '| ADDITION in `admin-ui/.test_durations.json` | `node scripts/vitest-test-telemetry.mjs --suite admin-ui` |'
    echo '| ADDITION in `agent/.test_durations.json` | `node scripts/vitest-test-telemetry.mjs --suite agent` |'
    echo '| emitter inventory fixture | `python backend/testing/regen_emitter_inventory.py --fix` |'
    echo '| `WEBSOCKET_EMITTER_INVENTORY` in `backend/app/workbench_contracts.py` | hand edit — there is no generator, by design |'
    echo '| `backend/testing/event_type_snapshot.json` | hand edit — it is a ratchet a human must ratify |'
    echo '| `backend/testing/conversational_workday_manifest.json` | hand edit — the `test_count` entries are protection FLOORS |'
    echo
    echo '**Do not "fix" this by deleting the guard, or by deleting the test whose'
    echo 'timing is missing.** Test erosion is one of the two weights this repo'
    echo 'scores releases on, so that trades a red gate for a scored regression.'
    echo 'Do not hand-write a duration either: pytest-split already estimates an'
    echo 'unknown test from the mean, and a fabricated row permanently marks the'
    echo 'file covered so no later run ever measures it.'
  } >> "$GITHUB_STEP_SUMMARY"
fi

# The classification never changes the verdict.
exit "$STATUS"
