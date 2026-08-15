#!/bin/bash
# viewport-snapshot.sh — before/after screenshot capture for admin-ui component
# changes (Phase 0 of the agent-facing visual-verification pilot).
#
# Generalizes ./check's baseline-worktree pattern (agent/src/prompts/check-helper.ts:
# the `origin/$BASE` re-run in a detached worktree) from pass/fail to PIXELS: it
# captures the changed components' viewport renders on the WORKING TREE ("after")
# and on `origin/<base>` ("before"), pairs them, and writes a manifest the agent
# inspects multimodally before opening a PR.
#
# Usage:
#   bash scripts/viewport-snapshot.sh                 # auto-scope vs origin/<base>
#   bash scripts/viewport-snapshot.sh --base main     # explicit base ref
#   bash scripts/viewport-snapshot.sh --current [ComponentName ...]  # AFTER only, no worktree
#   bash scripts/viewport-snapshot.sh TaskCard Kanban # explicit component names
#   bash scripts/viewport-snapshot.sh --upload --task-id <id>  # capture, then
#       PUT changed/new before/after PNGs to GCS and publish the manifest at
#       /tmp/viewport_manifest_<id>.json for ./pr to inject into the PR body.
#       Requires ORCHESTRATOR_URL + AGENT_API_TOKEN in the environment.
#
# It is INFORMATIONAL and never a gate: it exits 0 even when pairs differ. The
# Chromium bootstrap failure is recorded as VIEWPORT_CAPTURE_SKIPPED and exits
# successfully on an agent VM; the CI frontend-viewport job is the enforcing gate.
#
# Output lives under absolute /tmp (NEVER the repo tree) because `./pr` runs
# `git add -A` and src/**/__screenshots__/ is deliberately gitignored.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || { echo "viewport-snapshot: cannot cd to repo root" >&2; exit 0; }

# ── args ────────────────────────────────────────────────────────────────────
BASE="automain"
UPLOAD=0
TASK_ID=""
CURRENT=0
COMPONENTS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE="${2:-}"; shift 2 ;;
    --base=*) BASE="${1#*=}"; shift ;;
    --upload) UPLOAD=1; shift ;;
    --task-id) TASK_ID="${2:-}"; shift 2 ;;
    --task-id=*) TASK_ID="${1#*=}"; shift ;;
    --current) CURRENT=1; shift ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) COMPONENTS+=("$1"); shift ;;
  esac
done

# --upload PUTs the changed/new before/after PNGs to GCS via the task-scoped
# signed-URL endpoint and copies the manifest to a known path so ./pr can inject
# the screenshots block. It is task-bound, so --task-id is mandatory.
if [ "$UPLOAD" = "1" ] && [ -z "$TASK_ID" ]; then
  echo "viewport-snapshot: --upload requires --task-id <id>" >&2
  exit 1
fi

RUN_ID="${VIEWPORT_RUN_ID:-$$}"
SHOT_ROOT="/tmp/viewport_shots/$RUN_ID"
AFTER_DIR="$SHOT_ROOT/after"
BEFORE_DIR="$SHOT_ROOT/before"
MANIFEST="$SHOT_ROOT/manifest.json"
WT="/tmp/viewport_snap_wt"   # dedicated path — NOT /tmp/check_viewport_baseline_wt,
                             # so a concurrent ./check baseline run is never clobbered.

# Same Chromium-bootstrap classification as check-viewport.sh.
BOOTSTRAP_PATTERNS="libatk|libxcomposite|libgbm|libnss3|libasound|cannot open shared object file|executable doesn|host system is missing|please install|npx playwright install|run: playwright install|missing browser|missing chromium|eacces.*unlink.*vite|permission denied.*unlink"

# CI firebase/api env values — mirror check-helper.ts:370 / check-viewport.sh so
# the capture renders exactly what the gate renders.
VPORT_ENV=(
  VITE_API_URL=http://localhost:8080
  VITE_FIREBASE_API_KEY=ci
  VITE_FIREBASE_AUTH_DOMAIN=ci.firebaseapp.com
  VITE_FIREBASE_PROJECT_ID=ci
)

infra_unavailable() {
  echo "VIEWPORT_CAPTURE_SKIPPED: $1; CI frontend-viewport enforces browser verification"
  # Best-effort worktree cleanup before bailing.
  git worktree remove --force "$WT" >/dev/null 2>&1 || true
  exit 0
}

# ── base ref resolution ─────────────────────────────────────────────────────
BASE_REF=""
for ref in "origin/$BASE" "$BASE" "origin/main" "main"; do
  if git rev-parse --verify "$ref" >/dev/null 2>&1; then BASE_REF="$ref"; break; fi
done
if [ -z "$BASE_REF" ]; then
  echo "viewport-snapshot: no base ref found (tried origin/$BASE, $BASE, origin/main, main); diffing working tree only" >&2
fi

# ── scope: which viewport tests to capture ──────────────────────────────────
# Same diff sources + regexes as ./check (check-helper.ts:183-191, 319-326).
FILTER_FILE="$(mktemp)"
trap 'rm -f "$FILTER_FILE"' EXIT

if [ ${#COMPONENTS[@]} -gt 0 ]; then
  for c in "${COMPONENTS[@]}"; do
    name="${c%.tsx}"; name="${name%.viewport.test}"
    vt="src/components/${name}.viewport.test.tsx"
    [ -f "admin-ui/$vt" ] && printf '%s\n' "$vt" >> "$FILTER_FILE"
  done
else
  COMMITTED=""
  if [ -n "$BASE_REF" ]; then
    COMMITTED=$(git diff --name-only "$BASE_REF...HEAD" 2>/dev/null || true)
  fi
  UNCOMMITTED=$(git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null)
  CHANGED=$(printf '%s\n%s\n' "$COMMITTED" "$UNCOMMITTED" | sort -u | grep -v '^$' || true)
  ADMIN_UI_CHANGED=$(printf '%s\n' "$CHANGED" | grep -E '^admin-ui/' || true)

  # Directly-changed viewport tests.
  printf '%s\n' "$ADMIN_UI_CHANGED" | grep -E '^admin-ui/src/components/[^/]+\.viewport\.test\.tsx$' | while IFS= read -r f; do
    [ -f "$f" ] && printf '%s\n' "${f#admin-ui/}" >> "$FILTER_FILE"
  done
  # Changed components → their sibling viewport test (if it exists).
  printf '%s\n' "$ADMIN_UI_CHANGED" | grep -E '^admin-ui/src/components/[^/]+\.tsx$' | grep -v '\.viewport\.test\.tsx$' | while IFS= read -r f; do
    vt="${f%.tsx}.viewport.test.tsx"
    [ -f "$vt" ] && printf '%s\n' "${vt#admin-ui/}" >> "$FILTER_FILE"
  done
fi

FILTER=$(sort -u "$FILTER_FILE" 2>/dev/null | tr '\n' ' ' | sed 's/ *$//')
if [ -z "$FILTER" ]; then
  echo "nothing to snapshot (no changed admin-ui/src/components viewport tests)"
  exit 0
fi
echo "viewport-snapshot: run_id=$RUN_ID base=${BASE_REF:-<none>}"
echo "viewport-snapshot: capturing $(echo "$FILTER" | wc -w | tr -d ' ') viewport test file(s):"
for t in $FILTER; do echo "  - $t"; done

rm -rf "$SHOT_ROOT" 2>/dev/null || true
mkdir -p "$AFTER_DIR" "$BEFORE_DIR"

# ── bootstrap chromium (shared cache → install → with-deps) ─────────────────
bootstrap_chromium() {
  local dir="$1"
  ( cd "$dir" || exit 1
    if [ -d "$HOME/.cache/ms-playwright" ] && ls "$HOME/.cache/ms-playwright"/chromium-* >/dev/null 2>&1; then exit 0; fi
    npx playwright install chromium >/dev/null 2>&1 && exit 0
    npx playwright install --with-deps chromium >/dev/null 2>&1 && exit 0
    exit 1
  )
}

if ! bootstrap_chromium "$REPO_ROOT/admin-ui"; then
  if ! docker info >/dev/null 2>&1; then
    infra_unavailable "Chromium bootstrap failed and Docker unavailable"
  fi
  infra_unavailable "Chromium bootstrap failed (Docker present but capture not run in-container in Phase 0)"
fi

# ── AFTER pass: working tree ────────────────────────────────────────────────
run_capture() {
  # $1 = admin-ui dir, $2 = shot dir, $3 = log path
  local dir="$1" shot="$2" log="$3"
  ( cd "$dir" && VIEWPORT_SHOT_DIR="$shot" env "${VPORT_ENV[@]}" \
      npm run test:viewport:capture -- $FILTER ) >"$log" 2>&1
}

AFTER_LOG="/tmp/viewport_snap_after.log"
echo "viewport-snapshot: AFTER pass (working tree)..."
if ! run_capture "$REPO_ROOT/admin-ui" "$AFTER_DIR" "$AFTER_LOG"; then
  if grep -qiE "$BOOTSTRAP_PATTERNS" "$AFTER_LOG"; then
    tail -20 "$AFTER_LOG" >&2 || true
    infra_unavailable "Chromium bootstrap failure during AFTER capture"
  fi
  # A non-bootstrap failure (e.g. a broken render the agent should see) is NOT
  # fatal: partial PNGs may still have been written. Continue to the manifest.
  echo "viewport-snapshot: AFTER pass reported test failures (non-fatal); see $AFTER_LOG" >&2
  tail -15 "$AFTER_LOG" >&2 || true
fi

# ── BEFORE pass: detached base worktree with overlaid capture machinery ─────
# The capture config + maybeCapture hook only exist on this branch, so overlay
# them onto the base worktree: we hold the test harness constant and vary only
# the component/page source, yielding a true before/after of the COMPONENT.
# --current skips this entirely: AFTER only, no worktree, no npm ci on base.
if [ "$CURRENT" = "1" ]; then
  echo "viewport-snapshot: --current mode — skipping BEFORE pass"
elif [ -n "$BASE_REF" ]; then
  echo "viewport-snapshot: BEFORE pass (base worktree at $BASE_REF)..."
  git worktree remove --force "$WT" >/dev/null 2>&1 || true
  if git worktree add --detach "$WT" "$BASE_REF" >/tmp/viewport_snap_wt.log 2>&1; then
    cp "$REPO_ROOT/admin-ui/vitest.viewport.capture.config.ts" "$WT/admin-ui/vitest.viewport.capture.config.ts"
    cp "$REPO_ROOT/admin-ui/src/test/viewport.ts" "$WT/admin-ui/src/test/viewport.ts"
    BEFORE_LOG="/tmp/viewport_snap_before.log"
    if ( cd "$WT/admin-ui" \
          && npm ci >/tmp/viewport_snap_before_npmci.log 2>&1 \
          && bootstrap_chromium "$WT/admin-ui" \
          && VIEWPORT_SHOT_DIR="$BEFORE_DIR" env "${VPORT_ENV[@]}" \
               npx vitest run --config vitest.viewport.capture.config.ts $FILTER ) >"$BEFORE_LOG" 2>&1; then
      :
    else
      if grep -qiE "$BOOTSTRAP_PATTERNS" "$BEFORE_LOG" 2>/dev/null; then
        echo "viewport-snapshot: BEFORE pass Chromium issue; before shots may be incomplete" >&2
        tail -15 "$BEFORE_LOG" >&2 || true
      else
        echo "viewport-snapshot: BEFORE pass reported failures (non-fatal); see $BEFORE_LOG" >&2
        tail -15 "$BEFORE_LOG" >&2 || true
      fi
    fi
    git worktree remove --force "$WT" >/dev/null 2>&1 || true
  else
    echo "viewport-snapshot: worktree add failed (see /tmp/viewport_snap_wt.log); pairs will be marked 'new'" >&2
  fi
else
  echo "viewport-snapshot: no base ref — skipping BEFORE pass; all pairs will be 'new'" >&2
fi

# ── manifest + table (committed Python helper, invoked by reference) ────────
python3 "$REPO_ROOT/scripts/viewport_manifest.py" "$BEFORE_DIR" "$AFTER_DIR" "${BASE_REF:-$BASE}" "$RUN_ID" "$MANIFEST" || {
  echo "viewport-snapshot: manifest generation failed (non-fatal)" >&2
}

# ── Phase 1: upload changed/new pairs + publish manifest at a known path ─────
# PUTs each changed/new before/after PNG to the private GCS artifact bucket via
# the task-scoped signed-PUT endpoint, then copies the manifest to
# /tmp/viewport_manifest_<task_id>.json where ./pr (pr-helper.ts) reads it to
# render the <!-- blueprint:screenshots --> block. The upload `name` is the
# pair's `test` slug; the backend stores the object under
# sanitize(name)-<width>-<phase>.png, and the PR renderer reconstructs the same
# download name, so the two paths always agree. Bounded to 8 pairs (24 PNGs:
# before+after+diff per changed pair), mirroring the backend's 24-entry context
# cap. Every failure is non-fatal:
# snapshotting stays informational and never gates ./pr.
if [ "$UPLOAD" = "1" ]; then
  if [ ! -f "$MANIFEST" ]; then
    echo "viewport-snapshot: --upload: no manifest at $MANIFEST; nothing to upload" >&2
  else
    : "${ORCHESTRATOR_URL:?viewport-snapshot: --upload requires ORCHESTRATOR_URL}"
    : "${AGENT_API_TOKEN:?viewport-snapshot: --upload requires AGENT_API_TOKEN}"
    UP_OK=0
    UP_FAIL=0
    # One TSV row (phase<TAB>test<TAB>width<TAB>png<TAB>magnitude) per PNG to upload:
    # changed pairs contribute before+after+diff; new pairs contribute after only.
    # magnitude is change_pct/100 (0–1 fraction) attached to the "after" entry so the
    # badge renders; "before" and "diff" rows carry an empty 5th field.
    # Heredoc (not a pipe) so the counters survive in this shell.
    while IFS="$(printf '\t')" read -r _phase _test _width _png _magnitude; do
      [ -z "$_png" ] && continue
      if [ ! -f "$_png" ]; then
        echo "viewport-snapshot: --upload: PNG not found, skipping: $_png" >&2
        UP_FAIL=$((UP_FAIL + 1)); continue
      fi
      if [ -n "$_magnitude" ]; then
        _payload="{\"task_id\":\"${TASK_ID}\",\"name\":\"${_test}\",\"width\":${_width},\"phase\":\"${_phase}\",\"magnitude\":${_magnitude}}"
      else
        _payload="{\"task_id\":\"${TASK_ID}\",\"name\":\"${_test}\",\"width\":${_width},\"phase\":\"${_phase}\"}"
      fi
      _resp=$(curl -sf -X POST \
        -H "X-Agent-Token: ${AGENT_API_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "$_payload" \
        "${ORCHESTRATOR_URL}/api/agent/viewport/screenshot-upload-url") || _resp=""
      _url=$(printf '%s' "$_resp" | jq -r '.upload_url // empty' 2>/dev/null || true)
      if [ -z "$_url" ]; then
        echo "viewport-snapshot: --upload: signing failed for ${_test}-${_width}-${_phase}" >&2
        UP_FAIL=$((UP_FAIL + 1)); continue
      fi
      if curl -sf -T "$_png" -H "Content-Type: image/png" "$_url"; then
        echo "viewport-snapshot: --upload: uploaded ${_test}-${_width}-${_phase}"
        UP_OK=$((UP_OK + 1))
      else
        echo "viewport-snapshot: --upload: PUT failed for ${_test}-${_width}-${_phase}" >&2
        UP_FAIL=$((UP_FAIL + 1))
      fi
    done <<EOF
$(jq -r '.pairs
  | map(select((.status == "changed" or .status == "new") and .width != null))[0:8][]
  | (if .status == "changed" and .before then "before\t\(.test)\t\(.width)\t\(.before)\t" else empty end),
    (if .after then "after\t\(.test)\t\(.width)\t\(.after)\t\(if .change_pct != null then (.change_pct / 100 | tostring) else "" end)" else empty end),
    (if .status == "changed" and .diff then "diff\t\(.test)\t\(.width)\t\(.diff)\t" else empty end)' "$MANIFEST" 2>/dev/null)
EOF
    # Publish the manifest at the path pr-helper.ts reads (task-deterministic).
    if cp "$MANIFEST" "/tmp/viewport_manifest_${TASK_ID}.json" 2>/dev/null; then
      echo "viewport-snapshot: --upload: manifest published to /tmp/viewport_manifest_${TASK_ID}.json"
    else
      echo "viewport-snapshot: --upload: WARN failed to publish manifest copy" >&2
    fi
    echo "viewport-snapshot: --upload: summary — ${UP_OK} uploaded, ${UP_FAIL} failed"
  fi
fi

exit 0
