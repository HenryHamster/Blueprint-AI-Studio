
      # ── viewport screenshot upload (PR-only regression evidence) ──────────
      # Non-fatal: missing JSON / PNG / tools / token → WARN and skip.
      if command -v curl >/dev/null 2>&1 \
          && _load_agent_token 2>/dev/null && [ -n "${ORCHESTRATOR_URL:-}" ]; then
        VP_JSON="admin-ui/test-results/viewport-failures.json"
        if [ -f "$VP_JSON" ]; then
          _vp_slugify() {
            printf '%s' "$1" | sed -E 's/[^A-Za-z0-9]+/-/g; s/^-+|-+$//g'
          }
          _vp_component_from_name() {
            printf '%s' "$1" | sed -E 's/[[:space:]]+viewport[[:space:]].*$//' | sed -E 's/[[:space:]]+[-—–]+$//' | awk '
              {
                last = $NF;
                for (i = 1; i <= NF; i++) {
                  if ($i ~ /^[A-Z][A-Za-z0-9._-]*$/) {
                    comp = $i;
                  }
                }
              }
              END {
                if (comp != "") print comp;
                else if (last != "") print last;
              }
            '
          }
          while IFS=$'\t' read -r _vp_file _vp_name; do
            [ -z "$_vp_name" ] && continue
            # Extract component from the pre-viewport title while preserving
            # compound component names like "MessagesPanel composed with shell
            # panels — viewport regression ...".
            _vp_comp=$(_vp_component_from_name "$_vp_name")
            # Extract width: first run of digits immediately before "px".
            _vp_width=$(printf '%s' "$_vp_name" | grep -oE '[0-9]+px' | head -1 | tr -d 'px')
            if [ -z "$_vp_comp" ] || [ -z "$_vp_width" ]; then
              echo "[check] WARN: viewport upload — could not parse component/width from: $_vp_name"
              continue
            fi
            # Match the screenshot for this exact failed assertion when Vitest
            # emits one. Falling back to a broad glob would risk publishing the
            # wrong pixels for a different failure.
            _vp_name_slug=$(_vp_slugify "$_vp_name")
            _vp_file_slug=$(_vp_slugify "${_vp_file##*/}")
            _vp_png=$(
              find admin-ui/test-results -maxdepth 6 -type f -name "*${_vp_file_slug}*${_vp_name_slug}*.png" 2>/dev/null \
                | sort | head -1 || true
            )
            if [ -z "$_vp_png" ]; then
              _vp_png=$(find admin-ui/test-results -maxdepth 6 -type f -name "*${_vp_name_slug}*.png" 2>/dev/null | sort | head -1 || true)
            fi
            if [ -z "$_vp_png" ]; then
              echo "[check] WARN: viewport upload — no screenshot PNG found for $_vp_comp@${_vp_width}px"
              continue
            fi
            # POST for a signed upload URL.
            _vp_resp=$(curl -sf -X POST \
              -H "Authorization: Bearer $TOKEN" \
              -H "Content-Type: application/json" \
              -d "{\"task_id\":\"@TASK_ID@\",\"component\":\"$_vp_comp\",\"width\":$_vp_width}" \
              "${ORCHESTRATOR_URL}/api/agent/viewport/failure-screenshot-upload-url") || {
              echo "[check] WARN: viewport upload — signing request failed for $_vp_comp@${_vp_width}px"
              continue
            }
            _vp_url=$(printf '%s' "$_vp_resp" | _json_field upload_url 2>/dev/null || true)
            _vp_uri=$(printf '%s' "$_vp_resp" | _json_field storage_uri 2>/dev/null || true)
            if [ -z "$_vp_url" ]; then
              echo "[check] WARN: viewport upload — empty upload_url for $_vp_comp@${_vp_width}px"
              continue
            fi
            # PUT the PNG to the signed URL.
            curl -sf -T "$_vp_png" -H "Content-Type: image/png" "$_vp_url" || {
              echo "[check] WARN: viewport upload — PUT failed for $_vp_comp@${_vp_width}px"
              continue
            }
            # Save a local copy for the fixing agent to read before making changes.
            # The agent cannot open gs:// URIs, so a local path makes the screenshot
            # accessible via its file-reading tool.
            _vp_local_path="/tmp/viewport_failure_@TASK_ID@_${_vp_comp}_${_vp_width}.png"
            cp "$_vp_png" "$_vp_local_path" 2>/dev/null || true
            echo "[check] viewport screenshot uploaded: $_vp_uri ($_vp_comp@${_vp_width}px) — local copy: $_vp_local_path"
          done < <(
            python3 -c '
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for tr in data.get("testResults", []):
    fname = tr.get("name") or tr.get("filepath") or tr.get("file") or tr.get("testFile") or ""
    for ar in tr.get("assertionResults", []):
        if ar.get("status") == "failed":
            title = ar.get("fullName") or ar.get("title") or ""
            print(fname + "\t" + title)
' "$VP_JSON" 2>/dev/null || true
          )
        fi
      fi
      # ── end viewport screenshot upload ───────────────────────────────────
