
# ── visual acceptance evidence capture+upload (success path) ─────────────────
# Presence-gated: emitted only when this task declares a `visual` acceptance
# requirement. Best-effort — every failure mode is WARN + continue, never a
# blocking gate. Runs only when ./check is otherwise GREEN so the captured
# screenshot reflects a passing change, making the declared visual evidence
# satisfiable (the upload stamps the canonical (component, width) context URI
# the acceptance gate resolves against).
if [ ${#FAILED[@]} -eq 0 ] && admin_ui_runtime_touched \
    && command -v curl >/dev/null 2>&1 \
    && _load_agent_token 2>/dev/null && [ -n "${ORCHESTRATOR_URL:-}" ]; then
  _VA_COMPONENT='@COMPONENT@'
  _VA_WIDTH=@WIDTH@
  _VA_SHOT_DIR="/tmp/check_visual_shots_@TASK_ID@"
  rm -rf "$_VA_SHOT_DIR" 2>/dev/null || true
  mkdir -p "$_VA_SHOT_DIR"
  echo "[check] visual acceptance: capturing $_VA_COMPONENT@${_VA_WIDTH}px screenshot..."
  # Capture ONLY the declared component's viewport test(s) — the positional
  # filter is a vitest substring match against the test file path.
  if ! ( cd admin-ui && VIEWPORT_SHOT_DIR="$_VA_SHOT_DIR" VITE_API_URL=http://localhost:8080 VITE_FIREBASE_API_KEY=ci VITE_FIREBASE_AUTH_DOMAIN=ci.firebaseapp.com VITE_FIREBASE_PROJECT_ID=ci npm run test:viewport:capture -- "$_VA_COMPONENT" >/tmp/check_visual_capture_@TASK_ID@.log 2>&1 ); then
    echo "[check] WARN: visual acceptance — capture run failed (non-fatal); see /tmp/check_visual_capture_@TASK_ID@.log"
  fi
  # The PNG for this exact width: maybeCapture writes <dir>/<slug(file)>/<slug(test)>-<width>px.png.
  _VA_PNG=$(find "$_VA_SHOT_DIR" -type f -name "*-${_VA_WIDTH}px.png" 2>/dev/null | sort | head -1 || true)
  if [ -z "$_VA_PNG" ]; then
    echo "[check] WARN: visual acceptance — no screenshot PNG captured for $_VA_COMPONENT@${_VA_WIDTH}px; skipping upload"
  else
    # POST for a signed upload URL bound to the canonical (component, width) object.
    _VA_RESP=$(curl -sf -X POST \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d "{\"task_id\":\"@TASK_ID@\",\"component\":\"$_VA_COMPONENT\",\"width\":$_VA_WIDTH}" \
      "${ORCHESTRATOR_URL}/api/agent/viewport/failure-screenshot-upload-url") || _VA_RESP=""
    if [ -z "$_VA_RESP" ]; then
      echo "[check] WARN: visual acceptance — signing request failed for $_VA_COMPONENT@${_VA_WIDTH}px"
    else
      _VA_URL=$(printf '%s' "$_VA_RESP" | _json_field upload_url 2>/dev/null || true)
      _VA_URI=$(printf '%s' "$_VA_RESP" | _json_field storage_uri 2>/dev/null || true)
      if [ -z "$_VA_URL" ]; then
        echo "[check] WARN: visual acceptance — empty upload_url for $_VA_COMPONENT@${_VA_WIDTH}px"
      elif curl -sf -T "$_VA_PNG" -H "Content-Type: image/png" "$_VA_URL"; then
        echo "[check] visual acceptance screenshot uploaded: $_VA_URI ($_VA_COMPONENT@${_VA_WIDTH}px)"
      else
        echo "[check] WARN: visual acceptance — PUT failed for $_VA_COMPONENT@${_VA_WIDTH}px"
      fi
    fi
  fi
fi
# ── end visual acceptance evidence capture+upload ────────────────────────────
