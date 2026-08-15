#!/bin/bash
# viewport-live.sh — build and capture the live admin-UI SPA against a running backend.
#
# Required env: ORCHESTRATOR_URL, ADMIN_API_KEY
# Optional flags:
#   --upload --task-id <id>   upload PNGs to GCS and publish manifest
#   --widths <csv>            comma or space-separated widths (default: 375 1440)
#
# Exits 0 on success (capture failures are non-fatal).
# Exits 1 if --upload is passed without --task-id.
# Records VIEWPORT_CAPTURE_SKIPPED and exits successfully on Chromium bootstrap
# failure; the CI frontend-viewport job is the enforcing browser gate.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || { echo "viewport-live: cannot cd to repo root" >&2; exit 0; }

# ── args ──────────────────────────────────────────────────────────────────────
UPLOAD=0
TASK_ID=""
WIDTHS="375 1440"
while [ $# -gt 0 ]; do
  case "$1" in
    --upload) UPLOAD=1; shift ;;
    --task-id) TASK_ID="${2:-}"; shift 2 ;;
    --task-id=*) TASK_ID="${1#*=}"; shift ;;
    --widths) WIDTHS="${2:-}"; shift 2 ;;
    --widths=*) WIDTHS="${1#*=}"; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) shift ;;
  esac
done

if [ "$UPLOAD" = "1" ] && [ -z "$TASK_ID" ]; then
  echo "viewport-live: --upload requires --task-id <id>" >&2
  exit 1
fi

: "${ORCHESTRATOR_URL:?viewport-live: ORCHESTRATOR_URL is required}"
: "${ADMIN_API_KEY:?viewport-live: ADMIN_API_KEY is required}"

# Normalise widths: commas → spaces
WIDTHS="${WIDTHS//,/ }"

RUN_ID="${VIEWPORT_RUN_ID:-$$}"
OUT_DIR="/tmp/viewport_live/$RUN_ID"

# ── infra sentinel ─────────────────────────────────────────────────────────────
infra_unavailable() {
  echo "VIEWPORT_CAPTURE_SKIPPED: $1; CI frontend-viewport enforces browser verification"
  exit 0
}

# ── cleanup on exit ────────────────────────────────────────────────────────────
VITE_PID=""
cleanup() {
  if [ -n "$VITE_PID" ]; then
    kill "$VITE_PID" 2>/dev/null || true
  fi
  rm -rf "$REPO_ROOT/admin-ui/dist" 2>/dev/null || true
}
trap cleanup EXIT

# ── 1. build the SPA with token-bypass env ────────────────────────────────────
echo "viewport-live: building admin-ui with VITE_DASHBOARD_API_TOKEN..."
( cd admin-ui && \
  VITE_API_URL="$ORCHESTRATOR_URL" \
  VITE_DASHBOARD_API_TOKEN="$ADMIN_API_KEY" \
  VITE_FIREBASE_API_KEY=ci \
  VITE_FIREBASE_AUTH_DOMAIN=ci.firebaseapp.com \
  VITE_FIREBASE_PROJECT_ID=ci \
  npm run build ) || { echo "viewport-live: build failed" >&2; exit 0; }

# ── 2. serve and wait for readiness ───────────────────────────────────────────
echo "viewport-live: starting vite preview..."
( cd admin-ui && npx vite preview --port 4173 --host 127.0.0.1 ) &
VITE_PID=$!

READY=0
for i in $(seq 1 20); do
  if curl -sf http://127.0.0.1:4173/ >/dev/null 2>&1; then
    READY=1; break
  fi
  sleep 0.5
done

if [ "$READY" = "0" ]; then
  echo "viewport-live: vite preview did not become ready within 10s" >&2
  exit 0
fi
echo "viewport-live: preview ready"

# ── 3. capture ────────────────────────────────────────────────────────────────
mkdir -p "$OUT_DIR"

CAPTURE_LOG="/tmp/viewport_live_capture_${RUN_ID}.log"
node "$REPO_ROOT/scripts/viewport-live-capture.js" \
  "http://127.0.0.1:4173/" "$OUT_DIR" "$WIDTHS" >"$CAPTURE_LOG" 2>&1
CAPTURE_EXIT=$?
cat "$CAPTURE_LOG"

if grep -q "VIEWPORT_GATE_INFRA_UNAVAILABLE" "$CAPTURE_LOG"; then
  infra_unavailable "$(grep "VIEWPORT_GATE_INFRA_UNAVAILABLE" "$CAPTURE_LOG" | head -1 | sed 's/VIEWPORT_GATE_INFRA_UNAVAILABLE: //')"
fi

if [ "$CAPTURE_EXIT" = "75" ]; then
  infra_unavailable "viewport-live-capture.js exited 75"
fi

# ── 4. manifest ───────────────────────────────────────────────────────────────
python3 "$REPO_ROOT/scripts/viewport_manifest.py" \
  "" "$OUT_DIR" "live" "$RUN_ID" "$OUT_DIR/manifest.json" 2>/dev/null || true

# ── 5. upload (optional) ──────────────────────────────────────────────────────
if [ "$UPLOAD" = "1" ]; then
  MANIFEST="$OUT_DIR/manifest.json"
  if [ ! -f "$MANIFEST" ]; then
    echo "viewport-live: --upload: no manifest; nothing to upload" >&2
  else
    : "${AGENT_API_TOKEN:?viewport-live: --upload requires AGENT_API_TOKEN}"
    UP_OK=0
    UP_FAIL=0
    while IFS="$(printf '\t')" read -r _phase _test _width _png _magnitude; do
      [ -z "$_png" ] && continue
      if [ ! -f "$_png" ]; then
        echo "viewport-live: --upload: PNG not found, skipping: $_png" >&2
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
        echo "viewport-live: --upload: signing failed for ${_test}-${_width}-${_phase}" >&2
        UP_FAIL=$((UP_FAIL + 1)); continue
      fi
      if curl -sf -T "$_png" -H "Content-Type: image/png" "$_url"; then
        echo "viewport-live: --upload: uploaded ${_test}-${_width}-${_phase}"
        UP_OK=$((UP_OK + 1))
      else
        echo "viewport-live: --upload: PUT failed for ${_test}-${_width}-${_phase}" >&2
        UP_FAIL=$((UP_FAIL + 1))
      fi
    done <<EOF
$(jq -r '.pairs
  | map(select(.after != null))[0:8][]
  | "after\t\(.test)\t\(.width)\t\(.after)\t\(if .change_pct != null then (.change_pct / 100 | tostring) else "" end)"' \
  "$MANIFEST" 2>/dev/null)
EOF
    if cp "$MANIFEST" "/tmp/viewport_manifest_${TASK_ID}.json" 2>/dev/null; then
      echo "viewport-live: --upload: manifest published to /tmp/viewport_manifest_${TASK_ID}.json"
    fi
    echo "viewport-live: --upload: summary — ${UP_OK} uploaded, ${UP_FAIL} failed"
  fi
fi

echo "viewport-live: done (run_id=$RUN_ID, out=$OUT_DIR)"
exit 0
