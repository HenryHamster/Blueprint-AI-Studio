"""Agent VM bash startup script builder.

Extracted from compute.py to keep the ~280-line bash template separate from
the Python provisioner machinery. Pure transformation: takes the per-VM
parameters and returns a bash script string.

Note on the embedded bash: every `{var}` is an f-string interpolation,
every `{{...}}` is a literal brace passed through to bash. Local aliases at
the top of the function (ORCHESTRATOR_URL, _MATERIALIZE_MANIFEST_B64)
preserve the exact identifier names from the original inline f-string in
compute.py so the body is byte-identical to its previous form — that
identity matters because any whitespace or escaping change in the bash
script can silently break VM startup in ways that only surface in
production. See CLAUDE.md guidance on cross-language code generation.
"""


def build_agent_startup_script(
    *,
    task_id: str,
    secret_version_name: str,
    payload_uri_b64: str,
    orchestrator_url: str,
    materialize_manifest_b64: str,
) -> str:
    # Aliases preserve identifier names from the pre-extraction f-string so
    # the bash body below remains byte-identical to its compute.py origin.
    ORCHESTRATOR_URL = orchestrator_url
    _MATERIALIZE_MANIFEST_B64 = materialize_manifest_b64
    return f"""#!/bin/bash
    set -euo pipefail

    # Report failures to the orchestrator before powering off — otherwise
    # the reaper has no signal about WHY the VM died and blindly retries.
    # EARLY_TOKEN is populated once CREDS is fetched (step 2) so failures
    # BEFORE /etc/agent.env is written still land a heartbeat.
    EARLY_TOKEN=""
    LAST_STAGE_STDERR=""
    _report_failure() {{
      local line=$1
      local cmd="$2"
      local stack="$3"
      echo "FATAL: Startup script failed at line $line: $cmd"
      local TOKEN=""
      local ORCH="{ORCHESTRATOR_URL}"
      if [ -f /etc/agent.env ]; then
        # tr -d '"' strips the surrounding double quotes that
        # materialize_manifest.py writes per systemd env file format
        # (KEY="value"). Without this strip, the token reaches the
        # orchestrator as `"eyJ..."` (literal quotes) and JWT verify
        # returns 401 — which curl -f surfaces as exit 22 and silently
        # kills the script (task 923f51e8 investigation).
        TOKEN="$(grep '^AGENT_SECRET_TOKEN=' /etc/agent.env 2>/dev/null | cut -d= -f2- | tr -d '"' || true)"
      fi
      if [ -z "$TOKEN" ] && [ -n "$EARLY_TOKEN" ]; then
        TOKEN="$EARLY_TOKEN"
      fi
      if [ -n "$TOKEN" ] && [ -n "$ORCH" ]; then
        local err_excerpt
        # Scrub credential patterns before sending to the orchestrator. A
        # previous stage's stderr (e.g. a curl -v or an oauth refresh
        # rejection) could otherwise embed a Bearer token, an Anthropic
        # API key, or an unrelated JWT in the heartbeat payload.
        err_excerpt="$(printf '%s' "$LAST_STAGE_STDERR" | tr '\n' ' ' | tr '"' "'" | sed -E -e 's/[Aa]uthorization:[[:space:]]*[^[:space:]]+/Authorization: [REDACTED]/g' -e 's/[Bb]earer[[:space:]]+[A-Za-z0-9._-]+/Bearer [REDACTED]/g' -e 's/sk-ant-[A-Za-z0-9_-]+/[REDACTED-ANT-KEY]/g' -e 's/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/[REDACTED-JWT]/g' | head -c 400)"
        # Sanitize cmd + stack for JSON: strip quotes/newlines, cap length.
        local cmd_clean stack_clean
        cmd_clean="$(printf '%s' "$cmd" | tr '\n\t' '  ' | tr '"' "'" | head -c 200)"
        stack_clean="$(printf '%s' "$stack" | tr '\n' ' ' | tr '"' "'" | head -c 100)"
        local msg="STARTUP_FAILED line=$line cmd=[$cmd_clean] stack=[$stack_clean]"
        [ -n "$err_excerpt" ] && msg="$msg | $err_excerpt"
        curl -sf -X POST "$ORCH/api/agent/heartbeat" \
          -H "Content-Type: application/json" \
          -H "X-Agent-Token: $TOKEN" \
          -d "$(printf '{{"task_id": "{task_id}", "status_message": "%s"}}' "$msg")" \
          --connect-timeout 5 --max-time 10 || true
      fi
      poweroff
    }}
    # Capture BASH_COMMAND (the command that triggered ERR) and a compact
    # BASH_LINENO stack so we can tell whether the failure was inside a
    # function, a heredoc'd subcommand, or the main script.
    trap '_report_failure $LINENO "$BASH_COMMAND" "${{BASH_LINENO[*]}}"' ERR
    echo "Starting Agent VM Initialization..."

    # 1. Self-Destruct Mechanism (Defense in Depth)
    # Graceful: stop the service first (SIGTERM), wait 30s for cleanup, then poweroff
    TTL_SECONDS=3600
    (
      sleep $TTL_SECONDS
      echo "TTL reached ($TTL_SECONDS s). Gracefully stopping agent service..."
      systemctl stop blueprint-agent 2>/dev/null || true
      sleep 30
      echo "Force terminating VM..."
      poweroff
    ) &
    TTL_PID=$!

    # 2. Retrieve credentials from Secret Manager (never stored in instance metadata)
    # Retry up to 3 times — IAM policy propagation can take up to 60s after secret creation.
    SECRET_VERSION="{secret_version_name}"
    echo "Fetching agent credentials from Secret Manager..."
    CREDS=""
    for attempt in 1 2 3; do
      if CREDS="$(gcloud secrets versions access "$SECRET_VERSION" 2>&1)"; then
        echo "Secret fetched (attempt $attempt)"
        break
      fi
      echo "Secret fetch failed (attempt $attempt): $CREDS"
      if [ $attempt -lt 3 ]; then
        sleep $((attempt * 10))
      else
        echo "FATAL: Secret Manager access failed after 3 attempts"
        false  # Triggers ERR trap
      fi
    done

    # Beacon A — creds fetched. Fired as early as possible so silent deaths
    # have at least one forensic signal. Extracts token from CREDS inline
    # since /etc/agent.env doesn't exist yet (written by the python block below).
    # See the silent pre-claim death investigation (task 595c235d, 548043b9).
    _send_beacon() {{
      local msg="$1"
      local tok="$2"
      if [ -z "$tok" ]; then
        echo "[BEACON_SKIPPED] no token yet for: $msg" >&2
        return 0
      fi
      # Capture curl's stderr so a failed beacon is at least visible in the
      # systemd journal (and therefore in any AGENT_DIED_EARLY capture later).
      # Without this every beacon failure was silent — the symptom that drove
      # the 2026-04-24 silent-pre-claim investigation.
      #
      # The `|| curl_exit=$?` guard absorbs the substitution's non-zero
      # exit so it can never propagate to errexit. Plain
      # `err="$(curl ... )"` makes bash treat the whole simple-command as
      # exiting with curl's status (per the manual: "When used alone,
      # the assignment statements set the exit status of the command to
      # the exit status of the last command substitution") — combined
      # with set -e and curl -f returning 22 on any 4xx, that silently
      # killed the script with no ERR trap before the 2026-04-27 fix
      # (task 923f51e8 investigation).
      local err="" curl_exit=0
      err="$(curl -sf -X POST "{ORCHESTRATOR_URL}/api/agent/heartbeat" \
        -H "Content-Type: application/json" \
        -H "X-Agent-Token: $tok" \
        -d "$(printf '{{"task_id": "{task_id}", "status_message": "%s"}}' "$msg")" \
        --connect-timeout 5 --max-time 10 2>&1 >/dev/null)" || curl_exit=$?
      if [ "$curl_exit" -ne 0 ]; then
        echo "[BEACON_FAILED] $msg :: ${{err:-curl failed with exit $curl_exit}}" >&2
      fi
    }}
    EARLY_TOKEN="$(printf '%s' "$CREDS" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("env",{{}}).get("AGENT_SECRET_TOKEN",""))' 2>/dev/null || true)"
    _send_beacon "PROVISION_PROGRESS: creds fetched, entering env setup" "$EARLY_TOKEN"

    # 3. Materialize manifest via materialize_manifest.py (base64-embedded
    # script; see backend/app/_agent_scripts/ for rationale).
    MANIFEST_PATH=/tmp/agent_secret_manifest.json
    PY_STDERR=/tmp/agent_manifest_py.err
    MATERIALIZE_PY=/tmp/materialize_manifest.py
    printf '%s' "$CREDS" > "$MANIFEST_PATH"
    echo '{_MATERIALIZE_MANIFEST_B64}' | base64 -d > "$MATERIALIZE_PY"
    PY_EXIT=0
    # timeout 60 bounds hangs. if-test position suppresses ERR on
    # non-zero exit so we can capture stderr before re-raising.
    if timeout 60 python3 "$MATERIALIZE_PY" "$MANIFEST_PATH" /etc/agent.env 2>"$PY_STDERR"; then
      :  # success — fall through
    else
      PY_EXIT=$?
      LAST_STAGE_STDERR="materialize_manifest exit=$PY_EXIT: $(cat "$PY_STDERR" 2>/dev/null || echo 'no stderr')"
      rm -f "$MANIFEST_PATH" "$PY_STDERR" "$MATERIALIZE_PY"
      false  # trigger ERR trap now, _report_failure has EARLY_TOKEN + stderr
    fi
    rm -f "$MANIFEST_PATH" "$PY_STDERR" "$MATERIALIZE_PY"

    # Beacon B — env file materialized. Distinguishes "python manifest
    # block hung" from later-step hangs. Uses the token from the just-written
    # /etc/agent.env so subsequent beacons have a canonical source.
    # tr -d '"' strips the surrounding quotes (systemd env file format)
    # — without it the JWT reaches the orchestrator wrapped in literal
    # quotes and verify_agent_token returns 401 (task 923f51e8).
    AGENT_TOKEN="$(grep '^AGENT_SECRET_TOKEN=' /etc/agent.env | cut -d= -f2- | tr -d '"' || true)"
    _send_beacon "PROVISION_PROGRESS: env file materialized, preparing payload" "$AGENT_TOKEN"

    # Add PAYLOAD_URI if present (validated safe GCS URI — no secrets)
    if [ -n "{payload_uri_b64}" ]; then
        echo "PAYLOAD_URI=\\"$(echo '{payload_uri_b64}' | base64 -d)\\"" >> /etc/agent.env
    fi

    # 4. Download payload if provided
    if [ -n "{payload_uri_b64}" ]; then
        PAYLOAD_URI_DECODED="$(echo '{payload_uri_b64}' | base64 -d)"
        echo "Downloading payload from $PAYLOAD_URI_DECODED..."
        gsutil cp "$PAYLOAD_URI_DECODED" /etc/agent_payload.json
    fi

    # Beacon C — payload downloaded (or skipped). Narrows "gsutil cp hung"
    # vs. systemctl/agent issues to the correct culprit.
    _send_beacon "PROVISION_PROGRESS: payload ready, entering diagnostics" "$AGENT_TOKEN"

    # 5. Agent diagnostics — log baked-in code version for debugging
    echo "Agent source files:"
    ls -la /opt/agent/src/handlers/ 2>/dev/null || echo "  (no handlers directory)"
    head -1 /opt/agent/src/index.ts 2>/dev/null || echo "  (index.ts missing)"

    # 6. Start the systemd service
    echo "Starting blueprint-agent service..."
    # Beacon D — reached systemctl. If the 30s health check at line ~845 then
    # fails, we know it's inside the agent process, not before it.
    _send_beacon "PROVISION_PROGRESS: reached systemctl phase" "$AGENT_TOKEN"
    systemctl daemon-reload
    timeout 60 systemctl restart blueprint-agent || {{
        echo "Service start failed"
        AGENT_TOKEN="$(grep '^AGENT_SECRET_TOKEN=' /etc/agent.env 2>/dev/null | cut -d= -f2- | tr -d '"' || true)"
        ORCH_URL="$(grep '^ORCHESTRATOR_URL=' /etc/agent.env 2>/dev/null | cut -d= -f2- | tr -d '"' || true)"
        SERVICE_LOG="$(journalctl -u blueprint-agent --no-pager -n 30 2>/dev/null | tr '"' "'" | tr '\\n' ' ' || echo 'no journal')"
        curl -sf -X POST "$ORCH_URL/api/agent/heartbeat" \
          -H "Content-Type: application/json" \
          -H "X-Agent-Token: $AGENT_TOKEN" \
          -d '{{"task_id": "{task_id}", "status_message": "PROVISION_HEALTH_FAILED: service start failed. Journal: '"$SERVICE_LOG"'"}}' \
          --connect-timeout 10 --max-time 15 || true
        poweroff
        exit 1  # poweroff is async; exit so bash doesn't continue to step 6b
    }}

    # 6b. Notify orchestrator that startup completed — early health signal
    # Settle window: Type=simple returns as soon as npm is fork()ed, which is
    # not the same as the agent actually running a line of TypeScript. Wait 30s
    # and re-check is-active. To catch agents that boot then die between 30s
    # and 45s (audit E8: "VM ready != healthy"), wait another 15s and re-check
    # the ActiveState explicitly — `is-active --quiet` accepts "activating"
    # which is misleading mid-boot. If either window fails, we skip the success
    # heartbeat and fall through to the wait loop + poweroff so the reaper's
    # no-heartbeat handler sees the failure with its real classification.
    sleep 30
    # Source the env file directly — systemd uses the same format, and a
    # grep|cut pipeline crashes with pipefail+set-e when the pattern is
    # missing. Guard against -u by defaulting to empty.
    # shellcheck disable=SC1091
    [ -f /etc/agent.env ] && . /etc/agent.env || true
    AGENT_TOKEN="${{AGENT_SECRET_TOKEN:-}}"
    ORCH_URL="${{ORCHESTRATOR_URL:-}}"
    _AGENT_STILL_HEALTHY=0
    if systemctl is-active --quiet blueprint-agent && \
       [ "$(systemctl show blueprint-agent --property=ActiveState --value 2>/dev/null)" = "active" ]; then
      sleep 15
      if systemctl is-active --quiet blueprint-agent && \
         [ "$(systemctl show blueprint-agent --property=ActiveState --value 2>/dev/null)" = "active" ]; then
        _AGENT_STILL_HEALTHY=1
      fi
    fi
    if [ "$_AGENT_STILL_HEALTHY" = "1" ]; then
      # Capture curl's stderr so a failed startup-completed heartbeat is at
      # least visible in the systemd journal. Previously this curl used `-sf`
      # with no redirect — when the orchestrator was briefly unreachable the
      # heartbeat silently dropped and the only signal was a missing
      # status_log entry on the orchestrator side. Logged failures land in
      # the journal and therefore in any AGENT_DIED_EARLY capture if the
      # agent later dies before claiming.
      _STARTUP_HB_ERR=""
      _STARTUP_HB_EXIT=0
      _STARTUP_HB_ERR="$(curl -sf -X POST "$ORCH_URL/api/agent/heartbeat" \
        -H "Content-Type: application/json" \
        -H "X-Agent-Token: $AGENT_TOKEN" \
        -d '{{"task_id": "{task_id}", "status_message": "Startup script completed, agent service started"}}' \
        --connect-timeout 10 --max-time 15 2>&1 >/dev/null)"
      _STARTUP_HB_EXIT=$?
      if [ "$_STARTUP_HB_EXIT" -ne 0 ]; then
        echo "PROVISION_HEALTH_FAILED: startup heartbeat curl failed :: ${{_STARTUP_HB_ERR:-curl exit $_STARTUP_HB_EXIT}}" >&2
      else
        echo "PROVISION_HEALTH_OK"
      fi
    else
      # Agent service died within 30s — capture journal and report so the reaper's
      # "Silent pre-claim death" classification gets forensic evidence.  Without this
      # POST, the VM powers off and the orchestrator only knows that no heartbeat
      # ever arrived.  Keep the LAST 3KB (tail) — the error and stack are at the
      # end of the journal, while head would be just systemd boilerplate +
      # the agent's banner line that tells us nothing about why it died.
      echo "Agent service did not stay healthy through the 45s settle window — capturing journal for diagnostics"
      # Same scrub as _report_failure — agent-side accidental log of a
      # bearer token / API key would otherwise land in the heartbeat.
      SERVICE_LOG="$(journalctl -u blueprint-agent --no-pager -n 50 2>/dev/null | tr '"' "'" | tr '\\n' ' ' | sed -E -e 's/[Aa]uthorization:[[:space:]]*[^[:space:]]+/Authorization: [REDACTED]/g' -e 's/[Bb]earer[[:space:]]+[A-Za-z0-9._-]+/Bearer [REDACTED]/g' -e 's/sk-ant-[A-Za-z0-9_-]+/[REDACTED-ANT-KEY]/g' -e 's/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/[REDACTED-JWT]/g' | tail -c 3000 || echo 'no journal')"
      EXIT_STATUS="$(systemctl show blueprint-agent --property=ExecMainStatus --value 2>/dev/null || echo '?')"
      EXIT_CODE="$(systemctl show blueprint-agent --property=ExecMainCode --value 2>/dev/null || echo '?')"
      curl -sf -X POST "$ORCH_URL/api/agent/heartbeat" \
        -H "Content-Type: application/json" \
        -H "X-Agent-Token: $AGENT_TOKEN" \
        -d '{{"task_id": "{task_id}", "status_message": "AGENT_DIED_EARLY: exit='"$EXIT_CODE"'/'"$EXIT_STATUS"' journal='"$SERVICE_LOG"'"}}' \
        --connect-timeout 10 --max-time 15 || true
    fi

    # Wait for the service to finish (with TTL guard)
    POLL_START=$(date +%s)
    while systemctl is-active --quiet blueprint-agent; do
      sleep 5
      ELAPSED=$(( $(date +%s) - POLL_START ))
      if [ $ELAPSED -gt $TTL_SECONDS ]; then
        echo "Service exceeded TTL, stopping..."
        systemctl stop blueprint-agent 2>/dev/null || true
        break
      fi
    done

    # 7. Cancel TTL watchdog (service finished early) and self-terminate
    kill $TTL_PID 2>/dev/null || true
    echo "Agent finished. Terminating VM..."
    poweroff
    """
