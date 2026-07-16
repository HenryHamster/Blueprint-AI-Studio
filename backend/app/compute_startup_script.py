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

import os

# VM self-destruct watchdog ceiling (seconds). DEFENSE-IN-DEPTH failsafe, NOT a
# per-task wall-clock cap: it only ever fires when the control plane loses track
# of a VM (orphan, reaper halted, network partition) so the instance can't bill
# indefinitely. Real per-task enforcement is the reaper's liveness timeouts
# (no-heartbeat ~5min, no-progress ~8min) which catch a *stalled* task far
# faster; a healthy task heartbeating every 30s never trips them. Set generously
# (4h) so a legitimately long-running task (release_audit auditing a full
# promotion diff; future compute_experiment) is never guillotined mid-run — the
# prior 1h value killed the release audit at the 1h boot mark while it was still
# making steady progress. Env-tunable for ops without a logic redeploy.
DEFAULT_VM_SELF_DESTRUCT_TTL_SECONDS = int(
    os.getenv("VM_SELF_DESTRUCT_TTL_SECONDS", "14400")
)

# Per-task-type self-destruct TTL overrides (seconds). Most tasks use the 4h
# default above; a few task types legitimately run far longer and need a more
# generous failsafe ceiling so the watchdog never guillotines real work.
#
#   compute_experiment (v7, not yet implemented): runs ML / compute jobs that
#   can legitimately take many hours. It gets a 24h ceiling. NOTE this is a
#   deliberate choice to KEEP a (very generous) bounded failsafe rather than
#   EXEMPT compute entirely: an orphaned compute VM is the single most expensive
#   thing the fleet can leak (long-running, and v7 may schedule it on pricier
#   hardware), so an unbounded "never self-destruct" VM is exactly the wrong
#   risk to take. 24h never kills a real <=24h experiment but still caps a
#   runaway. If v7's compute backend grows its own authoritative job-lifecycle
#   cleanup, revisit whether the agent-VM ceiling can be raised further.
#
# This policy only applies to DEDICATED cold VMs provisioned for a KNOWN task
# (build_agent_startup_script, where task_type is available at provision time).
# Warm-pool VMs (build_pool_agent_startup_script) are TASKLESS — they boot before
# a task is claimed and cannot know they will run a compute_experiment, so they
# use the default ceiling. A v7 compute_experiment must therefore provision a
# dedicated VM, not lease a warm-pool one, to receive its 24h ceiling.
_VM_TTL_OVERRIDES_BY_TASK_TYPE: dict[str, int] = {
    "compute_experiment": 86400,  # 24h
}


_PROJECT_VERIFICATION_BOOT_BLOCK = r"""
    # Earn backend-authoring readiness in the same production user boundary as
    # blueprint-agent.service. A baked marker, installed binary, cached image,
    # or root-reachable daemon is not accepted as proof. The /run marker is
    # ephemeral and is written atomically only after both probes exit zero.
    PROJECT_VERIFICATION_MARKER=/run/blueprint-agent/project-verification-v1.ready
    PROJECT_VERIFICATION_LOG=/tmp/project-verification-v1.log
    rm -f "$PROJECT_VERIFICATION_MARKER" "$PROJECT_VERIFICATION_LOG"
    if timeout --signal=TERM --kill-after=5s 120s \
      sudo -u agent -H /opt/agent/bin/project-verification-probe.sh all \
      >"$PROJECT_VERIFICATION_LOG" 2>&1; then
      install -d -m 0755 /run/blueprint-agent
      PROJECT_VERIFICATION_TMP="$PROJECT_VERIFICATION_MARKER.$$"
      printf '%s\n' '{"version":1,"ruff":true,"docker_postgres":true}' \
        >"$PROJECT_VERIFICATION_TMP"
      chmod 0644 "$PROJECT_VERIFICATION_TMP"
      mv -f "$PROJECT_VERIFICATION_TMP" "$PROJECT_VERIFICATION_MARKER"
      echo "PROJECT_VERIFICATION_V1_READY"
    else
      PROJECT_VERIFICATION_EXIT=$?
      LAST_STAGE_STDERR="project verification exit=$PROJECT_VERIFICATION_EXIT: $(tail -c 400 "$PROJECT_VERIFICATION_LOG" 2>/dev/null || echo 'no probe log')"
      echo "PROJECT_VERIFICATION_V1_FAILED: $LAST_STAGE_STDERR" >&2
      false
    fi
    rm -f "$PROJECT_VERIFICATION_LOG"
"""


def resolve_vm_ttl_seconds(task_type: str | None) -> int:
    """Resolve the VM self-destruct ceiling (seconds) for a task type.

    Returns the per-type override when one is registered, else the 4h default.
    Pure / DB-free so the provision path and tests can call it cheaply.
    """
    return _VM_TTL_OVERRIDES_BY_TASK_TYPE.get(
        task_type or "", DEFAULT_VM_SELF_DESTRUCT_TTL_SECONDS
    )


def build_agent_startup_script(
    *,
    task_id: str,
    secret_version_name: str,
    payload_uri_b64: str,
    orchestrator_url: str,
    materialize_manifest_b64: str,
    ttl_seconds: int = DEFAULT_VM_SELF_DESTRUCT_TTL_SECONDS,
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
        err_excerpt="$(printf '%s' "$LAST_STAGE_STDERR" | tr '\n' ' ' | tr '"' "'" | sed -E -e 's/[Aa]uthorization:[[:space:]]*[^[:space:]]+/Authorization: [REDACTED]/g' -e 's/[Bb]earer[[:space:]]+[A-Za-z0-9._-]+/Bearer [REDACTED]/g' -e 's/sk-ant-[A-Za-z0-9_-]+/[REDACTED-ANT-KEY]/g' -e 's/eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+/[REDACTED-JWT]/g' | head -c 400)"
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
    TTL_SECONDS={ttl_seconds}
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

    # 5a. Runtime dependency guard. jq is baked into the canonical Packer and
    # Cloud Run agent images; this repairs stale/custom images before generated
    # helpers fail later with "command not found".
    if ! command -v jq >/dev/null 2>&1; then
      echo "PROVISION_DEP_REPAIR: jq missing; attempting apt install" >&2
      export DEBIAN_FRONTEND=noninteractive
      apt-get update
      apt-get install -y jq
    fi
    if ! command -v jq >/dev/null 2>&1; then
      LAST_STAGE_STDERR="jq missing after startup repair; stale or non-agent image"
      false
    fi

    # 5b. Agent diagnostics — log baked-in code version for debugging
    echo "Agent source files:"
    ls -la /opt/agent/src/handlers/ 2>/dev/null || echo "  (no handlers directory)"
    head -1 /opt/agent/src/index.ts 2>/dev/null || echo "  (index.ts missing)"

{_PROJECT_VERIFICATION_BOOT_BLOCK}

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
      # Route the startup-completed heartbeat through the same trap-safe
      # pattern as _send_beacon: `err="$(curl ...)" || curl_exit=$?`. A plain
      # top-level `VAR="$(curl ...)"` has its exit status set to curl's exit
      # code; under `set -e` a non-zero curl (e.g. 503 during Cloud Run
      # saturation) fires the ERR trap and powers off a HEALTHY VM whose agent
      # service just passed the 45s health gate. The `|| _STARTUP_HB_EXIT=$?`
      # guard absorbs the non-zero exit so the trap never fires.
      _STARTUP_HB_ERR=""
      _STARTUP_HB_EXIT=0
      _STARTUP_HB_ERR="$(curl -sf -X POST "$ORCH_URL/api/agent/heartbeat" \
        -H "Content-Type: application/json" \
        -H "X-Agent-Token: $AGENT_TOKEN" \
        -d '{{"task_id": "{task_id}", "status_message": "Startup script completed, agent service started"}}' \
        --connect-timeout 10 --max-time 15 2>&1 >/dev/null)" || _STARTUP_HB_EXIT=$?
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
      SERVICE_LOG="$(journalctl -u blueprint-agent --no-pager -n 50 2>/dev/null | tr '"' "'" | tr '\\n' ' ' | sed -E -e 's/[Aa]uthorization:[[:space:]]*[^[:space:]]+/Authorization: [REDACTED]/g' -e 's/[Bb]earer[[:space:]]+[A-Za-z0-9._-]+/Bearer [REDACTED]/g' -e 's/sk-ant-[A-Za-z0-9_-]+/[REDACTED-ANT-KEY]/g' -e 's/eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+/[REDACTED-JWT]/g' | tail -c 3000 || echo 'no journal')"
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


def build_pool_agent_startup_script(
    *,
    vm_instance_id: str,
    secret_version_name: str,
    orchestrator_url: str,
    materialize_manifest_b64: str,
    ttl_seconds: int = DEFAULT_VM_SELF_DESTRUCT_TTL_SECONDS,
) -> str:
    """TASKLESS (warm-pool) variant of the agent startup script.

    Differs from :func:`build_agent_startup_script` only in task binding: it has
    NO ``task_id``, so every provisioning beacon / failure report targets
    ``/api/agent/vm-heartbeat`` (VM-scoped auth) with a ``vm_instance_id`` body
    instead of ``/api/agent/heartbeat`` with a ``task_id``. The credential fetch
    + manifest materialization + service start are otherwise identical, and the
    poll-first marker (``BLUEPRINT_POOL_MODE``) is already baked into
    ``/etc/agent.env`` by the taskless manifest, so the agent boots in pool mode
    and polls ``/api/agent/next-role`` for a lease.

    Per CLAUDE.md cross-language guidance: the only embedded non-bash here is the
    pre-existing base64-decoded ``materialize_manifest.py`` and a single-line
    ``python3 -c`` token extraction — no nested multi-line heredocs.
    """
    ORCHESTRATOR_URL = orchestrator_url
    _MATERIALIZE_MANIFEST_B64 = materialize_manifest_b64
    return f"""#!/bin/bash
    set -euo pipefail

    # Pool VMs are taskless: liveness + failure reports go to /api/agent/vm-heartbeat
    # (VM-scoped auth) keyed on vm_instance_id, NOT /api/agent/heartbeat (task-scoped).
    EARLY_TOKEN=""
    LAST_STAGE_STDERR=""
    VM_INSTANCE_ID="{vm_instance_id}"
    _report_failure() {{
      local line=$1
      local cmd="$2"
      echo "FATAL: Pool startup script failed at line $line: $cmd"
      local TOKEN=""
      local ORCH="{ORCHESTRATOR_URL}"
      if [ -f /etc/agent.env ]; then
        TOKEN="$(grep '^AGENT_SECRET_TOKEN=' /etc/agent.env 2>/dev/null | cut -d= -f2- | tr -d '"' || true)"
      fi
      if [ -z "$TOKEN" ] && [ -n "$EARLY_TOKEN" ]; then
        TOKEN="$EARLY_TOKEN"
      fi
      if [ -n "$TOKEN" ] && [ -n "$ORCH" ]; then
        curl -sf -X POST "$ORCH/api/agent/vm-heartbeat" \
          -H "Content-Type: application/json" \
          -H "X-Agent-Token: $TOKEN" \
          -d "$(printf '{{"vm_instance_id": "%s", "pre_claim_failure": "startup contract failed"}}' "$VM_INSTANCE_ID")" \
          --connect-timeout 5 --max-time 10 || true
      fi
      poweroff
    }}
    trap '_report_failure $LINENO "$BASH_COMMAND"' ERR
    echo "Starting Pool Agent VM Initialization (taskless)..."

    # 1. Self-Destruct Mechanism (Defense in Depth)
    TTL_SECONDS={ttl_seconds}
    (
      sleep $TTL_SECONDS
      echo "TTL reached ($TTL_SECONDS s). Gracefully stopping agent service..."
      systemctl stop blueprint-agent 2>/dev/null || true
      sleep 30
      echo "Force terminating VM..."
      poweroff
    ) &
    TTL_PID=$!

    # 2. Retrieve credentials from Secret Manager.
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

    # VM-scoped beacon helper. The VM-scoped token comes from CREDS (env file
    # not written yet at first call).
    _send_vm_beacon() {{
      local tok="$1"
      if [ -z "$tok" ]; then
        echo "[POOL_BEACON_SKIPPED] no token yet" >&2
        return 0
      fi
      local err="" curl_exit=0
      err="$(curl -sf -X POST "{ORCHESTRATOR_URL}/api/agent/vm-heartbeat" \
        -H "Content-Type: application/json" \
        -H "X-Agent-Token: $tok" \
        -d "$(printf '{{"vm_instance_id": "%s"}}' "$VM_INSTANCE_ID")" \
        --connect-timeout 5 --max-time 10 2>&1 >/dev/null)" || curl_exit=$?
      if [ "$curl_exit" -ne 0 ]; then
        echo "[POOL_BEACON_FAILED] ${{err:-curl failed with exit $curl_exit}}" >&2
      fi
    }}
    EARLY_TOKEN="$(printf '%s' "$CREDS" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("env",{{}}).get("AGENT_SECRET_TOKEN",""))' 2>/dev/null || true)"

    # 3. Materialize manifest via the base64-embedded materialize_manifest.py.
    MANIFEST_PATH=/tmp/agent_secret_manifest.json
    PY_STDERR=/tmp/agent_manifest_py.err
    MATERIALIZE_PY=/tmp/materialize_manifest.py
    printf '%s' "$CREDS" > "$MANIFEST_PATH"
    echo '{_MATERIALIZE_MANIFEST_B64}' | base64 -d > "$MATERIALIZE_PY"
    if timeout 60 python3 "$MATERIALIZE_PY" "$MANIFEST_PATH" /etc/agent.env 2>"$PY_STDERR"; then
      :
    else
      echo "materialize_manifest failed: $(cat "$PY_STDERR" 2>/dev/null || echo 'no stderr')" >&2
      rm -f "$MANIFEST_PATH" "$PY_STDERR" "$MATERIALIZE_PY"
      false  # trigger ERR trap
    fi
    rm -f "$MANIFEST_PATH" "$PY_STDERR" "$MATERIALIZE_PY"

    AGENT_TOKEN="$(grep '^AGENT_SECRET_TOKEN=' /etc/agent.env | cut -d= -f2- | tr -d '"' || true)"

    # 4a. Runtime dependency guard. jq is baked into the canonical Packer and
    # Cloud Run agent images; this repairs stale/custom images before generated
    # helpers fail later with "command not found".
    if ! command -v jq >/dev/null 2>&1; then
      echo "POOL_PROVISION_DEP_REPAIR: jq missing; attempting apt install" >&2
      export DEBIAN_FRONTEND=noninteractive
      apt-get update
      apt-get install -y jq
    fi
    if ! command -v jq >/dev/null 2>&1; then
      echo "FATAL: jq missing after startup repair; stale or non-agent image" >&2
      false
    fi

    # 4b. Agent diagnostics — log baked-in code version for debugging.
    echo "Agent source files:"
    ls -la /opt/agent/src/handlers/ 2>/dev/null || echo "  (no handlers directory)"

{_PROJECT_VERIFICATION_BOOT_BLOCK}

    # 5. Start the systemd service. The agent boots poll-first (BLUEPRINT_POOL_MODE
    # is in /etc/agent.env) and polls /api/agent/next-role for a lease.
    echo "Starting blueprint-agent service (pool mode)..."
    systemctl daemon-reload
    timeout 60 systemctl restart blueprint-agent || {{
        echo "Service start failed"
        poweroff
        exit 1
    }}

    # Settle window: confirm the agent stays active before reporting healthy.
    sleep 30
    # shellcheck disable=SC1091
    [ -f /etc/agent.env ] && . /etc/agent.env || true
    AGENT_TOKEN="${{AGENT_SECRET_TOKEN:-}}"
    if systemctl is-active --quiet blueprint-agent; then
      _send_vm_beacon "$AGENT_TOKEN"
      echo "POOL_PROVISION_HEALTH_OK"
    else
      echo "Agent service did not stay healthy through the settle window" >&2
      LAST_STAGE_STDERR="pool agent service did not stay healthy through settle window"
      false
    fi

    # Wait for the service to finish (with TTL guard).
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

    # 6. Cancel TTL watchdog and self-terminate.
    kill $TTL_PID 2>/dev/null || true
    echo "Pool agent finished. Terminating VM..."
    poweroff
    """
