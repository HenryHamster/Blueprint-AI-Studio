import fs from 'fs';
import { excludeAgentHelpers, writeDesignDocHelper, shellEscapeDoubleQuoted } from '../prompt.js';

// Strategic shell-helper generators
// Generates three independent bash scripts for strategic-mode task execution:
// - strategic: router for API operations (set-problem-model, create-direction, etc.)
// - fetch: detail-pull for items trimmed by the packer
// - design-doc: CRUD operations for design document sections

function generateStrategicScript(taskIdRaw: string): string {
  const taskId = shellEscapeDoubleQuoted(taskIdRaw);
  return `#!/bin/bash
# Strategic review tool — updates problem_model, running_update, directions,
# messages via API. Reads ORCHESTRATOR_URL and AGENT_API_TOKEN from env.
OP="\${1:?Usage: strategic set-problem-model|set-running-update|reflect|pending-confirm|set-vision|create-direction|update-direction|retire-direction|stage-direction|create-message|resolve-message|spawn-task|set-retirement-critiques|set-evidence-progress <json-or-file>}"
shift
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${taskId}"
DATA="$1"

_call() {
  local attempt=0
  while [ $attempt -lt 3 ]; do
    local response
    response=$(printf '{"task_id":"%s","op":"%s","data":%s}' "$TASK" "$1" "$DATA" | \\
      curl -sS --max-time 30 --connect-timeout 10 \\
        -X POST "$API/api/agent/strategic" \\
        -H "Content-Type: application/json" \\
        -H "X-Agent-Token: $TOKEN" \\
        --data-binary @- \\
        -w '\\n%{http_code}')
    local http_code
    http_code=$(printf '%s' "$response" | tail -n1)
    local body
    body=$(printf '%s' "$response" | head -n -1)
    if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]; then
      printf '%s' "$body"
      return 0
    fi
    if [ "$http_code" -eq 409 ]; then
      printf '%s' "$body"
      echo "[CONSTRAINT] $1 rejected (HTTP 409)" >&2
      return 0
    fi
    attempt=$((attempt + 1))
    [ $attempt -lt 3 ] && echo "[RETRY $attempt/3] $1 failed (HTTP $http_code), retrying in 5s..." >&2 && sleep 5
  done
  echo "[ERROR] $1 failed after 3 attempts" >&2
  return 1
}

case "$OP" in
  set-problem-model)    _call "set_problem_model" ;;
  set-running-update)
    # DATA is a path to the markdown file written by the Thinker. Wrap
    # the file contents as {markdown: "..."} before the usual body build.
    FILE="$DATA"
    [ -f "$FILE" ] || { echo "[ERROR] set-running-update: file not found: $FILE" >&2; exit 1; }
    DATA=$(jq -Rs '{markdown: .}' < "$FILE")
    _call "set_running_update"
    ;;
  reflect)              _call "reflect" ;;
  pending-confirm)      _call "pending_confirm" ;;
  set-vision)           _call "set_vision" ;;
  create-direction)     _call "create_direction" ;;
  update-direction)     _call "update_direction" ;;
  retire-direction)     _call "retire_direction" ;;
  stage-direction)      _call "stage_direction" ;;
  create-message)            _call "create_message" ;;
  resolve-message)           _call "resolve_message" ;;
  spawn-task)                _call "spawn_task" ;;
  set-retirement-critiques)  _call "set_retirement_critiques" ;;
  set-evidence-progress)     _call "set_evidence_progress" ;;
  *) echo "Unknown op: $OP" >&2; exit 1 ;;
esac
echo ""
`;
}

function generateFetchScript(projectIdRaw: string): string {
  const projectId = shellEscapeDoubleQuoted(projectIdRaw);
  return `#!/bin/bash
# Strategic-context fetch tool — pull detail for items the packer trimmed.
# Reads ORCHESTRATOR_URL, AGENT_API_TOKEN from env. project_id is baked in.
KIND="\${1:?Usage: fetch problem-model|direction|playbook|gap|message|section [id]}"
ID="\${2:-}"
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
PROJECT="${projectId}"
[ -n "$PROJECT" ] || { echo "[ERROR] no project_id available; fetch is only usable in strategic-mode tasks" >&2; exit 1; }

# problem-model takes no id; everything else requires one. For "message" the
# id may be a short ref (first 8+ hex chars of the message UUID — the same ref
# operators see in the admin UI) or a full UUID; the backend resolves it and
# fails loudly on ambiguous/missing refs.
case "$KIND" in
  problem-model) PATH_SUFFIX="problem-model" ;;
  direction)  PATH_SUFFIX="directions/$ID" ;;
  playbook)   PATH_SUFFIX="playbook/$ID" ;;
  gap)        PATH_SUFFIX="gaps/$ID" ;;
  message)    PATH_SUFFIX="messages/$ID" ;;
  section)    PATH_SUFFIX="design-doc/$ID" ;;
  *) echo "Unknown kind: $KIND (use problem-model|direction|playbook|gap|message|section)" >&2; exit 1 ;;
esac

if [ "$KIND" != "problem-model" ] && [ -z "$ID" ]; then
  echo "[ERROR] fetch $KIND requires an id argument" >&2
  exit 1
fi

# Fetch with HTTP status awareness. Capture response body and status code
# separately so we can surface HTTP errors clearly.
response=$(curl -sS --max-time 20 --connect-timeout 10 \\
  -X GET "$API/api/agent/projects/$PROJECT/$PATH_SUFFIX" \\
  -H "X-Agent-Token: $TOKEN" \\
  -w '\\n%{http_code}')
http_code=$(printf '%s' "$response" | tail -n1)
body=$(printf '%s' "$response" | head -n -1)

# On 2xx: pass body through as today (success)
if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]; then
  printf '%s' "$body"
  echo ""
  exit 0
fi

# On 409: ambiguous ref or constraint violation. Surface the body (which contains
# candidate refs and minimum hex length) with a clear ambiguity signal so the
# agent knows to retry with a longer ref.
if [ "$http_code" -eq 409 ]; then
  printf '%s' "$body"
  echo "[AMBIGUOUS] fetch $KIND: multiple candidates found; candidates and minimum ref length in response above" >&2
  echo ""
  exit 1
fi

# On any other non-2xx (404, 400, 5xx, etc.): surface the body and report error.
# The agent can then decide how to handle missing/invalid refs.
printf '%s' "$body" >&2
echo "[ERROR] fetch $KIND failed (HTTP $http_code)" >&2
echo ""
exit 1
`;
}

export function writeStrategicHelpers(taskId: string, projectId: string, workspace: string): void {
  const strategicScript = generateStrategicScript(taskId);
  fs.writeFileSync(`${workspace}/strategic`, strategicScript, { mode: 0o755 });

  const fetchScript = generateFetchScript(projectId);
  fs.writeFileSync(`${workspace}/fetch`, fetchScript, { mode: 0o755 });

  // Write the ./design-doc CLI via the shared, durable generator (prompt.ts)
  // so strategic_review tasks get the same 429/5xx-retrying set/delete writes
  // as design_doc_update tasks — a single source of truth for the helper.
  writeDesignDocHelper(taskId, workspace);

  excludeAgentHelpers();
}
