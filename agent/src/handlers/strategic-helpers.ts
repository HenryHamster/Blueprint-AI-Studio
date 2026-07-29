import fs from 'fs';
import path from 'path';
import { createHash } from 'crypto';
import type WebSocket from 'ws';
import { boundedRegularReadShellFn, excludeAgentHelpers, writeDesignDocHelper, shellEscapeDoubleQuoted, DEFAULT_TOOL_GUIDANCE } from '../prompt.js';
import { extractJsonObject } from '../json-extract.js';
import { VmConfigMissingError } from '../providers/runners.js';
import { agentTokenShellFn, jsonStrShellFn } from '../token-file.js';
import { writeComputeImageHelper } from '../compute-image-helper.js';
import { cliOpInput, cliOps } from './strategic-ops-registry.js';

// Strategic shell-helper generators
// Generates three independent bash scripts for strategic-mode task execution:
// - strategic: router for API operations (set-problem-model, create-direction, etc.)
// - fetch: detail-pull for items trimmed by the packer
// - design-doc: CRUD operations for design document sections

// Case arms for CLI ops that need a body beyond the default `_call "<op>"`,
// keyed by the backend op name. set_running_update reads a markdown FILE path
// and wraps its contents as {markdown: "..."} before the usual POST. Kept here
// (not in the pure registry) so the registry stays plain metadata and the
// shared manifest carries no bash. The value is the FULL case arm (label + body
// + `;;`); ops absent here render the uniform default arm.
const SPECIAL_CLI_CASE_BODIES: Record<string, string> = {
  create_message: `  create-message)
    # Operator replies are a native Workday MCP action. Do not make a generic
    # strategic message look like a reply while an operator turn is active.
    if [ -e "$RESIDENT_REPLY_RECEIPT_PATH" ] || [ -L "$RESIDENT_REPLY_RECEIPT_PATH" ]; then
      echo "[ERROR] create-message: this is not the Workday operator reply tool; call reply_to_operator instead" >&2
      exit 2
    elif [ -e "$RESIDENT_REPLY_KEY_PATH" ] || [ -L "$RESIDENT_REPLY_KEY_PATH" ]; then
      echo "[ERROR] create-message: ambient Resident reply key exists without its receipt" >&2
      exit 1
    fi
    _call "create_message"
    ;;`,
  set_running_update: `  set-running-update)
    _call "set_running_update"
    ;;`,
};

// Render the `./strategic` CLI's usage alias list and case body from the single
// canonical registry (strategic-ops-registry.ts) so neither can drift from the
// backend dispatch chain. Every CLI-flagged op becomes a `<alias>) _call "<op>"`
// arm (or its SPECIAL_CLI_CASE_BODIES override); the usage string lists the same
// aliases in the same order.
function renderStrategicCaseBody(): string {
  return cliOps()
    .map(e => SPECIAL_CLI_CASE_BODIES[e.op] ?? `  ${e.alias}) _call "${e.op}" ;;`)
    .join('\n');
}

function renderStrategicHelp(): string {
  return cliOps()
    .map(entry => {
      const line = `${entry.alias}\t${entry.description}`.replace(/'/g, "'\"'\"'");
      return `  echo '${line}'`;
    })
    .join('\n');
}

function renderStrategicSchemaCase(): string {
  return cliOps().map(entry => {
    const input = cliOpInput(entry.op)!;
    const schema = JSON.stringify({
      operation: entry.alias,
      backend_op: entry.op,
      input: input.kind,
      required: input.required ?? [],
      fields: input.fields ?? [],
      example: input.example ?? {},
      notes: input.notes ?? [],
    });
    // JSON.stringify produces no single quotes for our checked-in contracts;
    // shellEscape is still used so a future description/example cannot turn
    // the generated help into executable shell.
    const quoted = schema.replace(/'/g, "'\\\"'\\\"'");
    return `  ${entry.alias}) printf '%s\\n' '${quoted}' ;;`;
  }).join('\n');
}

function generateStrategicScript(
  taskIdRaw: string,
  helperDir: string,
  replyPaths: {
    replyKeyPath?: string;
    replyReceiptPath?: string;
    stagedReplyPath?: string;
  } = {},
): string {
  const taskId = shellEscapeDoubleQuoted(taskIdRaw);
  const replyKeyPath = shellEscapeDoubleQuoted(
    (replyPaths.replyKeyPath
      ?? path.resolve(helperDir, '.resident-reply-idempotency-key')).replace(/\\/g, '/'),
  );
  const replyReceiptPath = shellEscapeDoubleQuoted(
    (replyPaths.replyReceiptPath
      ?? path.resolve(helperDir, '.resident-reply-receipt.json')).replace(/\\/g, '/'),
  );
  const stagedReplyPath = shellEscapeDoubleQuoted(
    (replyPaths.stagedReplyPath
      ?? path.resolve(helperDir, '.resident-reply-staged-message.json')).replace(/\\/g, '/'),
  );
  const caseBody = renderStrategicCaseBody();
  const helpText = renderStrategicHelp();
  const schemaCase = renderStrategicSchemaCase();
  return `#!/bin/bash
# Strategic review tool — updates problem_model, running_update, directions,
# messages via API. Reads ORCHESTRATOR_URL and AGENT_API_TOKEN from env.
# The op vocabulary below is DERIVED from the canonical registry
# (strategic-ops-registry.ts) — do not hand-edit the case/usage list.
_usage() {
  echo "Usage: strategic <operation> (--json '<object>' | --file <path> | --stdin)"
  echo "       strategic help <operation>"
  echo "       strategic schema <operation>"
  echo ""
  echo "Operations:"
${helpText}
}

_schema() {
  case "$1" in
${schemaCase}
    *) echo "Unknown operation for schema: $1" >&2; return 1 ;;
  esac
}

if [ "$#" -eq 0 ] || [ "\${1:-}" = "--help" ] || [ "\${1:-}" = "-h" ]; then
  _usage
  exit 0
fi
if [ "$1" = "help" ]; then
  [ -n "\${2:-}" ] || { _usage; exit 1; }
  _schema "$2"
  exit $?
fi
if [ "$1" = "schema" ]; then
  [ -n "\${2:-}" ] || { _usage; exit 1; }
  _schema "$2"
  exit $?
fi

OP="$1"
shift
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
${agentTokenShellFn()}
${jsonStrShellFn()}
_load_agent_token || exit 1
TASK="${taskId}"
RESIDENT_REPLY_KEY_PATH="${replyKeyPath}"
RESIDENT_REPLY_RECEIPT_PATH="${replyReceiptPath}"
RESIDENT_STAGED_REPLY_PATH="${stagedReplyPath}"
NODE_BIN="\${NODE_BINARY:-node}"
${boundedRegularReadShellFn()}

_validate_json() {
  "$NODE_BIN" -e 'JSON.parse(process.argv[1])' "$DATA" >/dev/null 2>&1 || {
    echo "[ERROR] $OP requires valid JSON input; run 'strategic schema $OP' for the accepted shape" >&2
    exit 2
  }
}

_read_json_input() {
  case "\${1:-}" in
    --json)
      [ "$#" -eq 2 ] || { echo "[ERROR] --json requires exactly one JSON argument" >&2; exit 2; }
      DATA="$2"
      ;;
    --file)
      [ "$#" -eq 2 ] || { echo "[ERROR] --file requires exactly one path" >&2; exit 2; }
      [ -f "$2" ] || { echo "[ERROR] input file not found: $2" >&2; exit 2; }
      DATA=$(cat "$2")
      ;;
    --stdin)
      [ "$#" -eq 1 ] || { echo "[ERROR] --stdin accepts no trailing arguments" >&2; exit 2; }
      DATA=$(cat)
      ;;
    *)
      [ "$#" -eq 1 ] || { echo "[ERROR] use exactly one of --json, --file, or --stdin" >&2; exit 2; }
      echo "[WARN] positional JSON is deprecated; use --json, --file, or --stdin" >&2
      DATA="$1"
      ;;
  esac
  _validate_json
}

_read_running_update_input() {
  case "\${1:-}" in
    --file)
      [ "$#" -eq 2 ] || { echo "[ERROR] --file requires exactly one path" >&2; exit 2; }
      [ -f "$2" ] || { echo "[ERROR] input file not found: $2" >&2; exit 2; }
      DATA=$(printf '{"markdown":%s}' "$(_json_str "$(cat "$2")")")
      ;;
    --stdin)
      [ "$#" -eq 1 ] || { echo "[ERROR] --stdin accepts no trailing arguments" >&2; exit 2; }
      DATA=$(printf '{"markdown":%s}' "$(_json_str "$(cat)")")
      ;;
    --json)
      [ "$#" -eq 2 ] || { echo "[ERROR] --json requires exactly one JSON argument" >&2; exit 2; }
      DATA="$2"
      _validate_json
      ;;
    *)
      # Legacy form: positional markdown file path.
      [ "$#" -eq 1 ] || { echo "[ERROR] use --file, --stdin, or --json" >&2; exit 2; }
      [ -f "$1" ] || { echo "[ERROR] set-running-update: file not found: $1" >&2; exit 2; }
      echo "[WARN] positional file input is deprecated; use --file" >&2
      DATA=$(printf '{"markdown":%s}' "$(_json_str "$(cat "$1")")")
      ;;
  esac
}

if [ "$OP" = "set-running-update" ]; then
  _read_running_update_input "$@"
else
  _read_json_input "$@"
fi

_call() {
  local attempt=0 saw_auth=0 max=3 delay http_code body dump response
  while [ $attempt -lt $max ]; do
    # Re-read the live token each attempt so a token that rotated between tries
    # (the agent refreshes at ~45min) is picked up — the long-lived-session
    # write-token-expiry fix.
    _load_agent_token || return 1
    dump=$(mktemp)
    response=$(printf '{"task_id":"%s","op":"%s","data":%s}' "$TASK" "$1" "$DATA" | \\
      curl -sS --max-time 30 --connect-timeout 10 \\
        -X POST "$API/api/agent/strategic" \\
        -H "Content-Type: application/json" \\
        -H "X-Agent-Token: $TOKEN" \\
        --data-binary @- \\
        -D "$dump" \\
        -w '\\n%{http_code}')
    http_code=$(printf '%s' "$response" | tail -n1)
    body=$(printf '%s' "$response" | head -n -1)
    delay=$(grep -i '^retry-after:' "$dump" 2>/dev/null | tail -n1 | tr -dc '0-9')
    rm -f "$dump"
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
    if [ $attempt -ge $max ]; then break; fi
    # curl emits http_code "000" on a transport-level failure (connect timeout,
    # DNS failure, TLS handshake, dropped connection) — no HTTP response at all.
    # Treat it as a transient failure and retry alongside 429/5xx so a network
    # blip (or a token-budget hiccup) is not booked as a permanent failure.
    if [ "$http_code" = "000" ] || [ "$http_code" = "429" ] || { [ "$http_code" -ge 500 ] && [ "$http_code" -lt 600 ]; }; then
      if [ -z "$delay" ]; then
        # Exponential backoff: 10s, 20s, 40s (floor=10, ceiling=60)
        delay=$((10 * (1 << (attempt - 1))))
        [ "$delay" -gt 60 ] && delay=60
      elif [ "$delay" -lt 10 ]; then
        delay=10
      fi
      echo "[RETRY $attempt/$((max - 1))] $1 failed (HTTP $http_code), retrying in \${delay}s..." >&2
      sleep "$delay"
    elif [ "$http_code" -eq 401 ] || [ "$http_code" -eq 403 ]; then
      saw_auth=1
      echo "[AUTH] $1 rejected (HTTP $http_code) — re-reading rotated agent token and retrying" >&2
      sleep 1
    else
      printf '%s' "$body" >&2
      echo "[ERROR] $1 failed (HTTP $http_code, non-retryable)" >&2
      return 1
    fi
  done
  if [ "$saw_auth" -eq 1 ] && { [ "$http_code" -eq 401 ] || [ "$http_code" -eq 403 ]; }; then
    echo "[ERROR] $1 failed: agent token expired and no refreshed token was available after 3 attempts. The session must be restarted (agent JWT TTL is 1h and the agent's proactive refresh appears wedged)." >&2
  else
    echo "[ERROR] $1 failed after $max attempts (HTTP $http_code)" >&2
  fi
  return 1
}

case "$OP" in
${caseBody}
  *) echo "Unknown op: $OP" >&2; exit 1 ;;
esac
echo ""
`;
}

// The full read-only fetch vocabulary. Each verb maps to a GET endpoint under
// /api/agent/projects/{project_id}/... — all share the standard agent auth and
// are callable by any of the project's agent tokens, so the helper works for
// every task type, not just strategic-cycle agents.
export type FetchVerb = 'problem-model' | 'direction' | 'playbook' | 'gap' | 'message' | 'section' | 'knowledge' | 'compute-sweeps' | 'compute-job' | 'compute-limits' | 'qualification' | 'version-phase' | 'vm-cost' | 'efficiency-snapshot' | 'ci-compliance' | 'capability-trials' | 'sections' | 'task' | 'tasks' | 'completion-debt' | 'ui-load-timing' | 'skills' | 'tools';

// Canonical order of the full vocabulary. STRATEGIC provisions all of these.
// `knowledge` is a SEARCH-shaped verb (token-scoped list/search + get-by-id),
// distinct from the single-id project-path verbs that precede it.
export const STRATEGIC_FETCH_VERBS: FetchVerb[] = [
  'problem-model', 'direction', 'playbook', 'gap', 'message', 'section', 'knowledge', 'compute-sweeps', 'compute-job', 'compute-limits', 'qualification', 'version-phase', 'vm-cost', 'efficiency-snapshot', 'ci-compliance', 'capability-trials', 'sections', 'task', 'tasks', 'completion-debt', 'ui-load-timing', 'skills', 'tools',
];

// Read-only verb subsets for non-strategic agents. Research resolves directions,
// the problem-model, and gaps it was handed by reference; plan resolves
// directions, design-doc sections, and playbook entries for architectural
// grounding; design-doc curation resolves directions, design-doc sections,
// playbook entries, gaps, and operator messages for architecture review.
// All subsets are strictly read-only (every verb is a GET).
export const RESEARCH_FETCH_VERBS: FetchVerb[] = ['direction', 'problem-model', 'gap', 'knowledge', 'qualification'];
export const PLAN_FETCH_VERBS: FetchVerb[] = ['direction', 'section', 'playbook', 'knowledge', 'qualification'];
export const DESIGN_DOC_FETCH_VERBS: FetchVerb[] = ['direction', 'section', 'playbook', 'gap', 'message', 'knowledge'];

// Exact case-statement body for each PATH-SUFFIX verb. The spacing is preserved
// verbatim so the path-verb arms stay byte-identical to the original. `knowledge`
// is intentionally absent here — it is SEARCH-shaped (its own base URL, optional
// query filters, no project path) and gets a self-contained arm below.
const FETCH_VERB_CASE: Partial<Record<FetchVerb, string>> = {
  'problem-model': 'problem-model) PATH_SUFFIX="problem-model"',
  'direction':     'direction)  PATH_SUFFIX="directions${ID:+/$ID}"',
  'playbook':      'playbook)   PATH_SUFFIX="playbook/$ID"',
  'gap':           'gap)        PATH_SUFFIX="gaps/$ID"',
  'message':       'message)    PATH_SUFFIX="messages/${ID:-open}"',
  'section':       'section)    PATH_SUFFIX="design-doc/$ID"',
  'compute-sweeps': 'compute-sweeps) PATH_SUFFIX="compute-sweeps${ID:+/$ID}"',
  'compute-job': 'compute-job) PATH_SUFFIX="compute-jobs/$ID"',
  'compute-limits': 'compute-limits) PATH_SUFFIX="compute-limits"',
  'qualification': 'qualification) PATH_SUFFIX="qualification"',
  'version-phase': 'version-phase) PATH_SUFFIX="version-phase"',
  'vm-cost':            'vm-cost)              PATH_SUFFIX="vm-cost"',
  'efficiency-snapshot': 'efficiency-snapshot)  PATH_SUFFIX="efficiency-snapshot"',
  'ci-compliance':       'ci-compliance)        PATH_SUFFIX="ci-compliance"',
  'capability-trials':   'capability-trials)    PATH_SUFFIX="capability-trials"',
  'sections':            'sections)             PATH_SUFFIX="sections/$ID"',
  'task':                'task)                 PATH_SUFFIX="tasks/$ID"',
  'tasks':               'tasks)                PATH_SUFFIX="tasks"',
  'completion-debt':     'completion-debt)      PATH_SUFFIX="completion-debt"',
  'ui-load-timing':      'ui-load-timing)       PATH_SUFFIX="ui-load-timing"',
  'skills':              'skills)               PATH_SUFFIX="skills"',
  'tools':               'tools)                PATH_SUFFIX="tools"',
};

// Self-contained case arm for the SEARCH-shaped `knowledge` verb. Unlike the
// path-suffix verbs it hits the token-scoped /api/agent/knowledge-documents
// endpoint directly (project resolved from the token, not the URL): a bare
// positional id pulls one document's full body, otherwise optional
// --topic/--direction/--q/--source-kind/--sweep-id/--tag flags become URL-encoded query
// params on the list/search endpoint. It does its own curl and exits, so it
// never falls through to the project-path fetch below.
const KNOWLEDGE_FETCH_CASE = `  knowledge)
    shift
    if [ -n "\${1:-}" ] && [ "\${1#--}" = "$1" ]; then
      kresp=$(curl -sS --max-time 20 --connect-timeout 10 \\
        -X GET "$API/api/agent/knowledge-documents/$1" \\
        -H "X-Agent-Token: $TOKEN" -w '\\n%{http_code}')
    else
      QARGS=()
      while [ $# -gt 0 ]; do
        case "$1" in
          --topic)       QARGS+=(--data-urlencode "topic=\${2:-}"); shift 2 ;;
          --direction)   QARGS+=(--data-urlencode "direction=\${2:-}"); shift 2 ;;
          --source-kind) QARGS+=(--data-urlencode "source_kind=\${2:-}"); shift 2 ;;
          --sweep-id)    QARGS+=(--data-urlencode "sweep_id=\${2:-}"); shift 2 ;;
          --q)           QARGS+=(--data-urlencode "q=\${2:-}"); shift 2 ;;
          --tag)         QARGS+=(--data-urlencode "tag=\${2:-}"); shift 2 ;;
          --lifecycle-status) QARGS+=(--data-urlencode "lifecycle_status=\${2:-}"); shift 2 ;;
          --followups)  QARGS+=(--data-urlencode "lifecycle_status=pending"); shift ;;
          *) echo "[ERROR] fetch knowledge: unknown flag '$1' (use --topic/--direction/--q/--source-kind/--sweep-id/--tag/--lifecycle-status/--followups or a document id)" >&2; exit 1 ;;
        esac
      done
      kresp=$(curl -sS --max-time 20 --connect-timeout 10 --get \\
        "\${QARGS[@]}" "$API/api/agent/knowledge-documents" \\
        -H "X-Agent-Token: $TOKEN" -w '\\n%{http_code}')
    fi
    kcode=$(printf '%s' "$kresp" | tail -n1)
    kbody=$(printf '%s' "$kresp" | head -n -1)
    if [ "$kcode" -ge 200 ] && [ "$kcode" -lt 300 ]; then printf '%s' "$kbody"; echo ""; exit 0; fi
    printf '%s' "$kbody" >&2
    echo "[ERROR] fetch knowledge failed (HTTP $kcode)" >&2
    exit 1
    ;;`;

// One-line description of what each verb resolves, for the prompt guidance block.
export const FETCH_VERB_HELP: Record<FetchVerb, string> = {
  'problem-model': '`./fetch problem-model` — the full current problem model (no id)',
  'direction':     '`./fetch direction [id]` — compact listing of all directions (no id), or full detail for one direction by id',
  'playbook':      '`./fetch playbook <key>` — a full playbook entry',
  'gap':           '`./fetch gap <id>` — a full gap record',
  'message':       '`./fetch message [ref]` — compact listing of open messages (no id), or full detail for one message by short ref or UUID',
  'section':       '`./fetch section <key>` — a full design-doc section',
  'knowledge':     '`./fetch knowledge [--topic T] [--direction D] [--q TERM] [--source-kind K] [--sweep-id S] [--tag T] [--lifecycle-status S] [--followups]` — list/search knowledge documents (web_research/design outputs); `./fetch knowledge <id>` for one document\'s full body; `--followups` is shorthand for `--lifecycle-status pending`',
  'compute-sweeps': '`./fetch compute-sweeps [sweep_id]` — list completed compute experiment sweeps, or fetch one sweep by id',
  'compute-job': '`./fetch compute-job <job_id>` — current execution truth for one compute job: live output, result diagnostics, requested versus observed hardware, deadline, and billing semantics',
  'compute-limits': '`./fetch compute-limits` — active compute constraints and advisory local-runner inventory: deadline range, project concurrency cap, local-runner policy, approval threshold, and spend ceiling',
  'qualification': '`./fetch qualification` — latest qualification verdict for the open version: candidate SHA, confidence, sub_scores, and full audit findings list (no id)',
  'version-phase': '`./fetch version-phase` — compact current version phase/lifecycle context, qualification state, serving/deploy status, and recent closed versions (no id)',
  'vm-cost':            '`./fetch vm-cost` — live VM cost by task type: cost_usd, task/VM count, merged count, and merge-yield per type over the last 7 days (no id)',
  'efficiency-snapshot': '`./fetch efficiency-snapshot` — current efficiency metrics: cost_per_merged_pr, vm_reuse_rate, llm_cost_per_merged_pr, and format_compliance over the last 7 days (no id)',
  'ci-compliance':       '`./fetch ci-compliance` — CI compliance status for the current integration branch HEAD: module-size breaches and branch-literal violations (no id)',
  'capability-trials':   '`./fetch capability-trials` — capability-trial verdicts keyed by cap_name: verdict, lift, n_with/n_without, and accrual projection (no id)',
  'sections':            '`./fetch sections <name>` — freshly-built payload section by name (self_reflection_log, anomaly_summary, playbook_summary, retired_directions_summary, rejected_proposals, past_retirement_critiques, previous_decision_outcomes, outcome_evaluation); use when that section was dropped under budget pressure',
  'task':                '`./fetch task <task_id>` — status and outcome for a single task by id; visible for any STRATEGIC_AGENT-spawned task or its transitive descendants (fields: id, parent_task_id, status, created_at, progress message, pr_url/failure_reason for terminal tasks); includes a `children` list of direct pipeline-spawned subtasks with their own status/task_type/title/pr_url',
  'tasks':               '`./fetch tasks` — list of recent tasks for this project (most recent first, default limit 20); includes STRATEGIC_AGENT-spawned tasks AND their transitive descendants; each item carries parent_task_id (null for strategist roots) to map failing descendants back to the spawned task, plus a `children` list of direct pipeline-spawned subtasks',
  'completion-debt':     '`./fetch completion-debt` — open completion-debt entries for the project (kind, description, created_at, age_days); recovery path when open_completion_debt was dropped from the strategic payload under budget pressure (no id)',
  'ui-load-timing':      '`./fetch ui-load-timing` — admin-UI initial-load timing samples grouped by surface: list of {ts, page_ready_ms, sample_type} ordered oldest-first; use to verify the >50% page-ready improvement retirement threshold (no id)',
  'skills':              '`./fetch skills` — project-declared shell skills from `.blueprint/tools.json`: slug, title, command, and optional description for each; an empty array means no skills are declared (no id)',
  'tools':               '`./fetch tools` — content of the project\'s ENABLED built-in tools (operator\'s notes markdown text, whiteboard presence/stroke-count summary): slug, title, kind, and text for each; empty content is skipped and an empty array means no enabled tool has content (no id)',
};

function shellSingleQuote(value: string): string {
  return `'${value.replace(/'/g, "'\"'\"'")}'`;
}

function renderFetchHelp(granted: FetchVerb[]): string {
  const overview = granted
    .map(verb => `    printf '%s\\n' ${shellSingleQuote(FETCH_VERB_HELP[verb])}`)
    .join('\n');
  const cases = granted
    .map(verb => `    ${verb}) printf '%s\\n' ${shellSingleQuote(FETCH_VERB_HELP[verb])} ;;`)
    .join('\n');
  return `_fetch_help() {
  case "\${1:-}" in
    "")
      echo "Usage: fetch help [verb]"
      echo "       fetch ${granted.join('|')} [id]"
      echo ""
      echo "Available read-only verbs:"
${overview}
      ;;
${cases}
    *) echo "Unknown fetch verb for help: $1 (allowed: ${granted.join('|')})" >&2; return 1 ;;
  esac
}`;
}

function generateFetchScript(projectIdRaw: string, verbs: FetchVerb[]): string {
  const projectId = shellEscapeDoubleQuoted(projectIdRaw);
  // Partition the full vocabulary against the granted subset in canonical order
  // so output is deterministic regardless of the order verbs are passed in.
  const granted = STRATEGIC_FETCH_VERBS.filter(v => verbs.includes(v));
  const rejected = STRATEGIC_FETCH_VERBS.filter(v => !verbs.includes(v));
  const verbList = granted.join('|');
  const fetchHelp = renderFetchHelp(granted);
  const caseBody = [
    ...granted.map(v => v === 'knowledge'
      ? KNOWLEDGE_FETCH_CASE
      : `  ${FETCH_VERB_CASE[v]} ;;`),
    // Verbs in the full vocabulary that are NOT granted here get an explicit
    // "not permitted in this workspace" reject (mirrors writeDesignDocHelper's
    // read-only reject) rather than a generic "unknown kind". For the full set
    // this list is empty, so the strategic script stays byte-identical.
    ...(rejected.length > 0
      ? [`  ${rejected.join('|')}) echo "Error: ./fetch '$KIND' is not permitted in this workspace (allowed: ${verbList})." >&2; exit 1 ;;`]
      : []),
    `  *) echo "Unknown kind: $KIND (use ${verbList})" >&2; exit 1 ;;`,
  ].join('\n');
  return `#!/bin/bash
# Read-only context fetch tool — pull full detail for items shipped trimmed,
# summarized, or only by reference. Reads ORCHESTRATOR_URL, AGENT_API_TOKEN from
# env. project_id is baked in. All endpoints are GET (strictly read-only).
${fetchHelp}
KIND="\${1:?Usage: fetch ${verbList} [id]}"
if [ "$KIND" = "help" ]; then
  _fetch_help "\${2:-}"
  exit $?
fi
ID="\${2:-}"
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
${agentTokenShellFn()}
_load_agent_token || exit 1
PROJECT="${projectId}"
[ -n "$PROJECT" ] || { echo "[ERROR] no project_id available; ./fetch is unavailable in this workspace" >&2; exit 1; }

# problem-model, compute-sweeps, compute-limits, qualification, version-phase, vm-cost, efficiency-snapshot,
# and ci-compliance take no id; everything else requires one. compute-sweeps accepts an
# optional sweep_id. For "message" the id may be a short
# ref (first 8+ hex chars of the message UUID — the same ref operators see in
# the admin UI) or a full UUID; the backend resolves it and fails loudly on
# ambiguous/missing refs.
case "$KIND" in
${caseBody}
esac

if [ "$KIND" != "problem-model" ] && [ "$KIND" != "compute-sweeps" ] && [ "$KIND" != "compute-limits" ] && [ "$KIND" != "qualification" ] && [ "$KIND" != "version-phase" ] && [ "$KIND" != "vm-cost" ] && [ "$KIND" != "efficiency-snapshot" ] && [ "$KIND" != "ci-compliance" ] && [ "$KIND" != "capability-trials" ] && [ "$KIND" != "direction" ] && [ "$KIND" != "message" ] && [ "$KIND" != "tasks" ] && [ "$KIND" != "completion-debt" ] && [ "$KIND" != "ui-load-timing" ] && [ "$KIND" != "skills" ] && [ "$KIND" != "tools" ] && [ -z "$ID" ]; then
  echo "[ERROR] fetch $KIND requires an id argument" >&2
  exit 1
fi

# Fetch with HTTP status awareness. Retry on 429/5xx with exponential backoff
# (10s/20s/40s, ceiling 60s); honor Retry-After header as minimum-10s floor.
# On 401/403: re-read the rotated token and retry ONCE before surfacing the error.
_fetch_url="$API/api/agent/projects/$PROJECT/$PATH_SUFFIX"
_fetch_attempt=0
_fetch_max=3
_fetch_auth_retried=0
while true; do
  _dump=$(mktemp)
  response=$(curl -sS --max-time 20 --connect-timeout 10 \\
    -X GET "$_fetch_url" \\
    -H "X-Agent-Token: $TOKEN" \\
    -D "$_dump" \\
    -w '\\n%{http_code}')
  http_code=$(printf '%s' "$response" | tail -n1)
  body=$(printf '%s' "$response" | head -n -1)
  _retry_after=$(grep -i '^retry-after:' "$_dump" 2>/dev/null | tail -n1 | tr -dc '0-9')
  rm -f "$_dump"
  # On 2xx: success
  if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]; then
    printf '%s' "$body"
    echo ""
    exit 0
  fi
  # On 401/403: re-read the rotated token and retry ONCE (not counted against
  # the transient-error budget — auth retries are a separate concern).
  if { [ "$http_code" -eq 401 ] || [ "$http_code" -eq 403 ]; } && [ $_fetch_auth_retried -eq 0 ]; then
    _fetch_auth_retried=1
    _load_agent_token || exit 1
    continue
  fi
  # On 409: ambiguous ref or constraint violation.
  if [ "$http_code" -eq 409 ]; then
    printf '%s' "$body"
    echo "[AMBIGUOUS] fetch $KIND: multiple candidates found; candidates and minimum ref length in response above" >&2
    echo ""
    exit 1
  fi
  # On 429 or 5xx: retry with exponential backoff (floor=10s, ceiling=60s).
  _fetch_attempt=$((_fetch_attempt + 1))
  if { [ "$http_code" = "429" ] || { [ "$http_code" -ge 500 ] && [ "$http_code" -lt 600 ]; }; } && [ $_fetch_attempt -lt $_fetch_max ]; then
    if [ -n "$_retry_after" ] && [ "$_retry_after" -ge 10 ] 2>/dev/null; then
      _delay=$_retry_after
    else
      _delay=$((10 * (1 << (_fetch_attempt - 1))))
      [ "$_delay" -gt 60 ] && _delay=60
    fi
    echo "[RETRY $_fetch_attempt/$((_fetch_max - 1))] fetch $KIND failed (HTTP $http_code), retrying in \${_delay}s..." >&2
    sleep "$_delay"
    continue
  fi
  # Non-retryable or exhausted retries.
  printf '%s' "$body" >&2
  echo "[ERROR] fetch $KIND failed (HTTP $http_code)" >&2
  echo ""
  exit 1
done
exit 1
`;
}

// Write the read-only ./fetch helper into a workspace, baking ONLY the granted
// verbs into the script's case statement. Disallowed full-vocabulary verbs are
// rejected with a clear "not permitted in this workspace" message. taskId is
// accepted for call-site symmetry with the other writers (the fetch script is
// project-scoped, not task-scoped, so it is not baked into the script).
export function writeFetchHelper(
  _taskId: string,
  projectId: string,
  workspace: string,
  opts: { verbs: FetchVerb[] },
): void {
  const fetchScript = generateFetchScript(projectId, opts.verbs);
  fs.writeFileSync(`${workspace}/fetch`, fetchScript, { mode: 0o755 });
  excludeAgentHelpers();
}

// Render a short prompt block telling the agent the read-only ./fetch helper
// exists, listing ONLY the verbs provisioned for it and when to reach for it.
// Returns '' when no verbs are granted so callers can interpolate it
// unconditionally. Shared by the research and plan handlers.
export function buildFetchGuidance(verbs: FetchVerb[]): string {
  const granted = STRATEGIC_FETCH_VERBS.filter(v => verbs.includes(v));
  if (granted.length === 0) return '';
  const lines = granted.map(v => `- ${FETCH_VERB_HELP[v]}`).join('\n');
  return `# MID-RUN FETCH (./fetch)
You were launched from a context snapshot that may have trimmed, summarized, or only referenced some items. When an available context item below was shipped trimmed/summarized or only by id, run \`./fetch <verb> [id]\` to pull its current content before acting on it. Read-only and project-scoped.
Available verbs in this workspace:
${lines}

`;
}

// Returns the full STRATEGIC_FETCH_VERBS set as an array of one-line
// description strings (the FETCH_VERB_HELP values in canonical order).
// Consumed by strategic.ts to render both the packer-meta recovery bullet list
// and the executor tools reference block from a single canonical source, so
// neither list can drift from the provisioned verb set.
export function renderStrategicFetchVerbHelpLines(): string[] {
  return STRATEGIC_FETCH_VERBS.map(v => FETCH_VERB_HELP[v]);
}

// ── Strategic judgment challenge (independent adversarial second opinion) ────
//
// The strategic loop's self-produced judgments — evidence_progress ratings
// ("this direction is advancing/met"), retirement-critique quality, and the
// resulting portfolio shape — are self-serving by construction. This module
// runs a FRESH, independent adversarial pass over the thinker's analysis that
// re-judges those three classes BEFORE the executor commits them, and produces
// a bounded, versioned record.
//
// The record GATES the executor: an `advancing`/`met` evidence_progress verdict
// may be submitted only for a (direction_id, status) pair the challenge
// APPROVED; `stalled` verdicts stay advisory and are never gated. A blocked
// (challenger could not run / exited non-zero) or invalid (unparseable) outcome
// is persisted as a bounded record that approves NOTHING — it is NOT retried as
// missing executor work, so a challenge-only cycle never masquerades as a
// successful strategic mutation.

export const STRATEGIC_CHALLENGE_VERSION = 1;

const _VALID_EV_STATUS = new Set(['advancing', 'stalled', 'met']);
const _VALID_EV_VERDICT = new Set(['approved', 'rejected']);
// Bound the record so it can never bloat the executor prompt or the JSONB column.
const _MAX_EV_VERDICTS = 50;
const _RATIONALE_MAX = 500;
const _NOTES_MAX = 1000;

export type ChallengeEvidenceStatus = 'advancing' | 'stalled' | 'met';
export type ChallengeEvidenceVerdict = 'approved' | 'rejected';

export interface ChallengeEvidenceProgressVerdict {
  direction_id: string;
  status: ChallengeEvidenceStatus;
  verdict: ChallengeEvidenceVerdict;
  rationale: string;
}

export interface ChallengeRetirementApprovalVerdict {
  direction_id: string;
  verdict: ChallengeEvidenceVerdict;
  rationale: string;
}

export type ChallengePortfolioVerdict = 'approved' | 'rejected' | 'warned' | 'uncertain';

const _PORTFOLIO_OP_ALIASES: Record<string, string> = {
  create: 'create_direction',
  update: 'update_direction',
  retire: 'retire_direction',
  stage: 'stage_direction',
  approve: 'approve_direction',
  reject: 'reject_direction',
  activate: 'activate_direction',
};
const _VALID_PORTFOLIO_OPS = new Set<string>(Object.values(_PORTFOLIO_OP_ALIASES));
const _VALID_PORTFOLIO_VERDICTS = new Set<string>(['approved', 'rejected', 'warned', 'uncertain']);
const _PORTFOLIO_BLOCK_VERDICTS = new Set<string>(['rejected']);
const _PORTFOLIO_WARN_VERDICTS = new Set<string>(['warned', 'uncertain']);
const _HARD_BLOCK_PORTFOLIO_OPS = new Set<string>([
  'create_direction',
  'update_direction',
  'retire_direction',
  'stage_direction',
  'approve_direction',
  'reject_direction',
  'activate_direction',
]);

export interface ChallengePortfolioOperationVerdict {
  op: string;
  direction_id: string;
  text: string;
  fingerprint: string;
  verdict: ChallengePortfolioVerdict;
  rationale: string;
}

export interface StrategicChallengeRecord {
  challenge_version: number;
  status: 'ok' | 'blocked' | 'invalid';
  evidence_progress: ChallengeEvidenceProgressVerdict[];
  retirement_approvals: ChallengeRetirementApprovalVerdict[];
  retirement_critique_quality: { verdict: string; rationale: string };
  portfolio_shape_risk: { verdict: string; rationale: string };
  portfolio_operations: ChallengePortfolioOperationVerdict[];
  notes?: string;
}

type RunClaudeCodeFn = (
  prompt: string,
  ws: WebSocket | null,
  statusRef: { value: string },
  model?: string,
  effort?: string,
  disallowedTools?: string[],
) => Promise<{ exitCode: number; allOutput: string; lastResultText: string }>;

type SendLogFn = (ws: WebSocket | null, message: string) => void;

// A bounded record that approves nothing — used for the blocked (non-zero exit /
// runner error) and invalid (unparseable output) paths so the gate fails closed.
function emptyChallengeRecord(
  status: 'blocked' | 'invalid',
  note: string,
): StrategicChallengeRecord {
  return {
    challenge_version: STRATEGIC_CHALLENGE_VERSION,
    status,
    evidence_progress: [],
    retirement_approvals: [],
    retirement_critique_quality: { verdict: 'not_assessed', rationale: note.slice(0, _RATIONALE_MAX) },
    portfolio_shape_risk: { verdict: 'not_assessed', rationale: note.slice(0, _RATIONALE_MAX) },
    portfolio_operations: [],
    notes: note.slice(0, _NOTES_MAX),
  };
}

// Public: a blocked record (approves nothing) for callers that must degrade a
// cycle to fail-closed AFTER the challenge ran fine but a downstream step failed —
// e.g. the `set_strategic_challenge` persist threw, so the backend holds no record
// (its gate fails closed) and the executor must be shown a matching no-approvals
// view rather than approvals the backend will reject. Mirrors the blocked/invalid
// shape so `buildStrategicChallengeExecutorSection` renders the "approving NOTHING"
// caveats.
export function blockedChallengeRecord(note: string): StrategicChallengeRecord {
  return emptyChallengeRecord('blocked', note);
}

// Only treat genuine strings as strings — never stringify a nested object (which
// would yield "[object Object]"). Mirrors index.ts:strVal without the import.
function _safeStr(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

function coerceVerdictBlock(v: unknown): { verdict: string; rationale: string } {
  if (v && typeof v === 'object' && !Array.isArray(v)) {
    const o = v as Record<string, unknown>;
    const verdict = (_safeStr(o.verdict) || _safeStr(o.assessment) || 'unspecified').trim().slice(0, 64);
    const rationale = (_safeStr(o.rationale) || _safeStr(o.reason)).trim().slice(0, _RATIONALE_MAX);
    return { verdict: verdict || 'unspecified', rationale };
  }
  return { verdict: 'unspecified', rationale: '' };
}

function normalizeEvidenceProgress(raw: unknown): ChallengeEvidenceProgressVerdict[] {
  if (!Array.isArray(raw)) return [];
  const out: ChallengeEvidenceProgressVerdict[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
    const o = item as Record<string, unknown>;
    const direction_id = _safeStr(o.direction_id).trim().slice(0, 64);
    const status = _safeStr(o.status).trim().toLowerCase();
    let verdict = _safeStr(o.verdict).trim().toLowerCase();
    if (!direction_id) continue;
    if (!_VALID_EV_STATUS.has(status)) continue;
    // Fail closed: any verdict that is not an explicit "approved" is treated as
    // a rejection, so a malformed/omitted verdict never auto-approves progress.
    if (!_VALID_EV_VERDICT.has(verdict)) verdict = 'rejected';
    out.push({
      direction_id,
      status: status as ChallengeEvidenceStatus,
      verdict: verdict as ChallengeEvidenceVerdict,
      rationale: _safeStr(o.rationale).trim().slice(0, _RATIONALE_MAX),
    });
    if (out.length >= _MAX_EV_VERDICTS) break;
  }
  return out;
}

// Mirrors normalizeEvidenceProgress for the retirement_approvals list. No status
// field — retirement approvals are a simple (direction_id, verdict, rationale) triple.
// Fail-closed: any verdict that is not an explicit "approved" is treated as rejected.
function normalizeRetirementApprovals(raw: unknown): ChallengeRetirementApprovalVerdict[] {
  if (!Array.isArray(raw)) return [];
  const out: ChallengeRetirementApprovalVerdict[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
    const o = item as Record<string, unknown>;
    const direction_id = _safeStr(o.direction_id).trim().slice(0, 64);
    let verdict = _safeStr(o.verdict).trim().toLowerCase();
    if (!direction_id) continue;
    // Fail closed: any verdict that is not an explicit "approved" is rejected.
    if (!_VALID_EV_VERDICT.has(verdict)) verdict = 'rejected';
    out.push({
      direction_id,
      verdict: verdict as ChallengeEvidenceVerdict,
      rationale: _safeStr(o.rationale).trim().slice(0, _RATIONALE_MAX),
    });
    if (out.length >= _MAX_EV_VERDICTS) break;
  }
  return out;
}

function canonicalPortfolioOp(op: unknown): string | null {
  const name = _safeStr(op).trim().toLowerCase();
  if (_VALID_PORTFOLIO_OPS.has(name)) return name;
  return _PORTFOLIO_OP_ALIASES[name] ?? null;
}

export function portfolioOperationFingerprint(
  op: unknown,
  opts: { directionId?: string; text?: string } = {},
): string {
  const canon = canonicalPortfolioOp(op) ?? _safeStr(op).trim().toLowerCase();
  const did = (opts.directionId ?? '').trim();
  if (did) return `${canon}:${did}`;
  const norm = (opts.text ?? '').trim().toLowerCase().split(/\s+/).filter(Boolean).join(' ');
  if (norm) {
    const digest = createHash('sha256').update(norm).digest('hex').slice(0, 16);
    return `${canon}:text:${digest}`;
  }
  return `${canon}:`;
}

function normalizePortfolioOperations(raw: unknown): ChallengePortfolioOperationVerdict[] {
  if (!Array.isArray(raw)) return [];
  const out: ChallengePortfolioOperationVerdict[] = [];
  const seen = new Set<string>();
  for (const item of raw) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
    const o = item as Record<string, unknown>;
    let canon = canonicalPortfolioOp(o.op);
    if (!canon) continue;
    const verdict = _safeStr(o.verdict).trim().toLowerCase();
    if (!_VALID_PORTFOLIO_VERDICTS.has(verdict)) continue;
    const direction_id = _safeStr(o.direction_id).trim().slice(0, 64);
    const text = _safeStr(o.text).trim();
    // A "stage_direction" verdict carrying text but no direction_id is really a
    // vote to STAGE A NEW direction, which executes as create_direction(staged=true)
    // since the stage path cannot create. Reclassify so this agent-side fingerprint
    // matches the backend's stored create_direction op for the move.
    if (canon === 'stage_direction' && text && !direction_id) {
      canon = 'create_direction';
    }
    const fingerprint = portfolioOperationFingerprint(canon, {
      directionId: direction_id || undefined,
      text: text || undefined,
    });
    if (seen.has(fingerprint)) continue;
    seen.add(fingerprint);
    out.push({
      op: canon,
      direction_id,
      text,
      fingerprint,
      verdict: verdict as ChallengePortfolioVerdict,
      rationale: _safeStr(o.rationale).trim().slice(0, _RATIONALE_MAX),
    });
    if (out.length >= 50) break;
  }
  return out;
}

// Parse the challenger's raw output into a bounded record. Returns an `invalid`
// record (approving nothing) when no well-formed JSON object can be extracted.
export function parseStrategicChallengeRecord(rawText: string): StrategicChallengeRecord {
  let obj: Record<string, unknown> | null = null;
  try {
    // portfolio_shape_risk is a REQUIRED key the challenger always emits, so it
    // is a reliable anchor for locating the verdict object in noisy output.
    obj = extractJsonObject(rawText || '', 'portfolio_shape_risk');
  } catch {
    obj = null;
  }
  if (!obj) {
    return emptyChallengeRecord('invalid', 'challenge output was not parseable JSON');
  }
  // Fail closed: only an explicit 'ok' is treated as approved. A missing, empty,
  // or unrecognized status (e.g. 'invalid', 'error') must NOT default to 'ok' —
  // a malformed challenger response must never silently carry evidence_progress
  // through an implicitly-approved channel.
  const declaredStatus = _safeStr(obj.status).trim().toLowerCase();
  const isOk = declaredStatus === 'ok';
  // Both retirement_critique_quality and portfolio_shape_risk are REQUIRED keys.
  // If either is absent or is not an object, the response is malformed and the
  // gate must fail closed rather than display missing sections as clean.
  const hasRetirementSection = obj.retirement_critique_quality !== null
    && typeof obj.retirement_critique_quality === 'object'
    && !Array.isArray(obj.retirement_critique_quality);
  if (!hasRetirementSection) {
    return emptyChallengeRecord('invalid', 'challenge output missing required section: retirement_critique_quality');
  }
  const hasPortfolioSection = obj.portfolio_shape_risk !== null
    && typeof obj.portfolio_shape_risk === 'object'
    && !Array.isArray(obj.portfolio_shape_risk);
  if (!hasPortfolioSection) {
    return emptyChallengeRecord('invalid', 'challenge output missing required section: portfolio_shape_risk');
  }
  const record: StrategicChallengeRecord = {
    challenge_version: STRATEGIC_CHALLENGE_VERSION,
    status: isOk ? 'ok' : 'blocked',
    // Only an explicitly-ok challenge may carry evidence_progress or retirement_approvals through.
    evidence_progress: isOk ? normalizeEvidenceProgress(obj.evidence_progress) : [],
    retirement_approvals: isOk ? normalizeRetirementApprovals(obj.retirement_approvals) : [],
    retirement_critique_quality: coerceVerdictBlock(obj.retirement_critique_quality),
    portfolio_shape_risk: coerceVerdictBlock(obj.portfolio_shape_risk),
    portfolio_operations: isOk ? normalizePortfolioOperations(obj.portfolio_operations) : [],
  };
  const notes = _safeStr(obj.notes).trim().slice(0, _NOTES_MAX);
  if (notes) record.notes = notes;
  return record;
}

const _CHALLENGE_ANALYSIS_MAX_CHARS = 40_000;
const _CHALLENGE_ANALYSIS_HEAD_CHARS = 20_000;
const _CHALLENGE_ANALYSIS_OMISSION_MARKER =
  '\n\n[... middle of strategic analysis omitted to preserve prompt bound ...]\n\n';

function boundStrategicChallengeAnalysis(rawAnalysis: string): string {
  const analysis = rawAnalysis || '';
  if (analysis.length <= _CHALLENGE_ANALYSIS_MAX_CHARS) return analysis;

  const tailChars = _CHALLENGE_ANALYSIS_MAX_CHARS
    - _CHALLENGE_ANALYSIS_HEAD_CHARS
    - _CHALLENGE_ANALYSIS_OMISSION_MARKER.length;
  return [
    analysis.slice(0, _CHALLENGE_ANALYSIS_HEAD_CHARS),
    _CHALLENGE_ANALYSIS_OMISSION_MARKER,
    analysis.slice(-tailChars),
  ].join('');
}

// Build the adversarial challenge prompt. The challenger reads the thinker's
// analysis plus the directions/portfolio context the thinker saw, and emits
// ONLY the strict verdict JSON.
export function buildStrategicChallengePrompt(opts: {
  analysisText: string;
  dirSummaryText: string;
  pastRetirementCritiquesSection: string;
  portfolioCompositionText: string;
  stagedDirectionsText?: string;
  activeCapacityText?: string;
}): string {
  const analysis = boundStrategicChallengeAnalysis(opts.analysisText);
  const ctxBlocks = [
    opts.dirSummaryText ? `## Existing directions (what the thinker saw)\n${opts.dirSummaryText}` : '',
    opts.portfolioCompositionText ? `## Portfolio composition\n${opts.portfolioCompositionText}` : '',
    opts.activeCapacityText ? `## Active-direction capacity\n${opts.activeCapacityText}` : '',
    opts.stagedDirectionsText ? `## Already-staged (next-version) directions\n${opts.stagedDirectionsText}` : '',
    opts.pastRetirementCritiquesSection || '',
  ].filter(Boolean).join('\n\n');
  return `# Role: independent strategic challenge (adversarial second opinion)

You are an INDEPENDENT adversarial reviewer. A strategist ("thinker") just produced a strategic analysis containing self-assessed judgments. Your job is to CHALLENGE — not rubber-stamp — five classes of those self-produced judgments BEFORE they are committed:

1. **evidence_progress** — the thinker's claims that a long-running direction is "advancing" or "met". These are self-serving by construction. APPROVE an advancing/met claim ONLY when the analysis presents concrete, observable evidence that the direction's own success_evidence is actually being met. If the evidence is thin, assumed, or absent, REJECT (a stalled reality must not be dressed up as progress). When in doubt, REJECT — approving a false "advancing" is the costly error.
2. **retirement_approvals** — for EVERY direction the thinker proposes to retire, judge whether the retirement reasoning names a real, evidenced pattern (approve) or is shallow/boilerplate/unsupported (reject). When in doubt, REJECT — approving an unsupported retirement is the costly error. If the thinker proposes no retirements, emit an empty array.
3. **retirement_critique_quality** — whether the thinker's retirement critiques name a real pattern + lesson (solid) or are shallow/boilerplate.
4. **portfolio_shape_risk** — whether the resulting active-direction portfolio is coherent, over-concentrated, duplicative, missing coverage, or over capacity. A direction is a durable strategic outcome/bet and an active direction opens a recurring autonomous research lane. File, PR, phase, checklist, or one-shot implementation shards are tasks, not directions.
5. **portfolio_operations** — each concrete portfolio MUTATION the thinker proposed: create / update / retire / stage / approve / reject / activate of a direction. Judge each one and its NET effect on portfolio coherence (capacity, concentration, overlap, fan-out, coverage). REJECT create/update proposals that split one durable outcome into task/file/PR shards; prefer bounded tasks under an existing direction. Be STRICT on destructive or fan-out-sensitive moves: REJECT a retire / stage / approve / reject / activate / lane-removing update when the analysis does NOT present a real, evidenced reason. A create can also be rejected when it is genuinely unsafe to commit. Use "warned"/"uncertain" for advisory, subjective concerns that should NOT block; reserve "rejected" for moves that are genuinely unsafe to commit.

You do NOT commit anything. You ONLY emit a verdict record. Be terse and skeptical.

## The thinker's analysis to challenge
${analysis}

${ctxBlocks}

# Output (STRICT)
Output ONLY a single JSON object — no markdown, no code fences, no preamble — with EXACTLY this shape:
{
  "status": "ok",
  "evidence_progress": [
    {"direction_id": "d-xxx", "status": "advancing|stalled|met", "verdict": "approved|rejected", "rationale": "one line"}
  ],
  "retirement_approvals": [
    {"direction_id": "d-xxx", "verdict": "approved|rejected", "rationale": "one line"}
  ],
  "retirement_critique_quality": {"verdict": "solid|shallow|not_assessed", "rationale": "one line"},
  "portfolio_shape_risk": {"verdict": "low|elevated|high", "rationale": "one line"},
  "portfolio_operations": [
    {"op": "create_direction|update_direction|retire_direction|stage_direction|approve_direction|reject_direction|activate_direction", "direction_id": "d-xxx", "text": "<proposed text — creates only>", "verdict": "approved|warned|uncertain|rejected", "rationale": "one line"}
  ]
}
Rules:
- Add an evidence_progress entry for EVERY direction the thinker rated advancing or met (verdict "approved" only with concrete evidence, otherwise "rejected"). Directions the thinker called stalled need no entry — stalled is advisory and never gated. If the thinker rated none, emit an empty array.
- Add a retirement_approvals entry for EVERY direction the thinker proposes to retire (verdict "approved" only when the reasoning names a real evidenced pattern, otherwise "rejected"). When in doubt, REJECT. If the thinker proposes no retirements, emit an empty array.
- "retirement_critique_quality" and "portfolio_shape_risk" are REQUIRED keys even when there is little to say.
- Add a portfolio_operations entry for EVERY concrete create/update/retire/stage/approve/reject/activate the thinker proposed. For ops on an existing direction set "direction_id" (its id); for a create set "text" to the proposed direction text (omit/empty "direction_id"). A NEW direction the thinker proposes to STAGE for the next version is op "create_direction" (set "text", no "direction_id") — it executes as a staged create; "stage_direction" is only for an EXISTING direction (set "direction_id"). Use "rejected" ONLY for destructive/fan-out-sensitive moves that are unsafe to commit; "warned"/"uncertain" for advisory subjective concerns; "approved" for clean moves.
- Output the JSON object and nothing else.`;
}

// Run the challenge pass and return a bounded record. runClaudeCode/sendLog are
// injected so the caller's module-scoped test mocks are honoured (mirrors
// attemptStructuredRepair). A vm_config_missing infra death propagates so the
// orchestrator gets a clean PROVISION_FAILURE requeue; any other runner error
// or a non-zero exit degrades to a blocked record (gate fails closed).
export async function runStrategicChallenge(opts: {
  analysisText: string;
  dirSummaryText: string;
  pastRetirementCritiquesSection: string;
  portfolioCompositionText: string;
  stagedDirectionsText?: string;
  activeCapacityText?: string;
  ws: WebSocket | null;
  statusRef: { value: string };
  model?: string;
  effort?: string;
  disallowedTools: string[];
  runClaudeCode: RunClaudeCodeFn;
  sendLog: SendLogFn;
}): Promise<StrategicChallengeRecord> {
  const { ws, statusRef, model, effort, disallowedTools, runClaudeCode, sendLog } = opts;
  const prompt = `${buildStrategicChallengePrompt(opts)}\n\n${DEFAULT_TOOL_GUIDANCE}`;
  statusRef.value = 'Running strategic challenge (independent adversarial second opinion)...';
  let result: { exitCode: number; allOutput: string; lastResultText: string };
  try {
    result = await runClaudeCode(prompt, ws, statusRef, model, effort, disallowedTools);
  } catch (e) {
    if (e instanceof VmConfigMissingError) throw e;
    sendLog(ws, `[WARNING] Strategic challenge pass errored (recording blocked record): ${String(e)}\n`);
    return emptyChallengeRecord('blocked', `challenge pass errored: ${String(e)}`);
  }
  if (result.exitCode !== 0) {
    sendLog(ws, `[WARNING] Strategic challenge pass exited ${result.exitCode} (recording blocked record).\n`);
    return emptyChallengeRecord('blocked', `challenge pass exited ${result.exitCode}`);
  }
  const record = parseStrategicChallengeRecord(result.lastResultText || result.allOutput);
  if (record.status === 'invalid') {
    sendLog(ws, `[WARNING] Strategic challenge produced unparseable output (recording invalid record; no evidence approvals).\n`);
  } else {
    sendLog(ws, `[INFO] Strategic challenge complete (status=${record.status}, evidence_verdicts=${record.evidence_progress.length}, retirement_approvals=${record.retirement_approvals.length}, portfolio_op_verdicts=${record.portfolio_operations.length}).\n`);
  }
  return record;
}

// Render the challenge record + the evidence-progress gate for the executor
// prompt. The executor may submit an advancing/met verdict only for a
// challenge-approved (direction_id, status) pair listed here.
export function buildStrategicChallengeExecutorSection(record: StrategicChallengeRecord): string {
  // Only ok-status records can approve advancing/met pairs or retirements;
  // blocked/invalid records must present empty approval lists to the executor.
  const approvedEvidence = record.status === 'ok'
    ? record.evidence_progress.filter(
        e => e.verdict === 'approved' && (e.status === 'advancing' || e.status === 'met'),
      )
    : [];
  const approvedEvidenceList = approvedEvidence.length > 0
    ? approvedEvidence.map(e => `  - ${e.direction_id} → ${e.status}`).join('\n')
    : '  - (none — no advancing/met evidence_progress verdict was approved this cycle)';
  const approvedRetirements = record.status === 'ok'
    ? record.retirement_approvals.filter(r => r.verdict === 'approved')
    : [];
  const approvedRetirementList = approvedRetirements.length > 0
    ? approvedRetirements.map(r => `  - ${r.direction_id}`).join('\n')
    : '  - (none — no retirement was approved this cycle)';
  const portfolioOps = record.status === 'ok' ? record.portfolio_operations : [];
  const renderOp = (o: ChallengePortfolioOperationVerdict): string =>
    `  - ${o.op} ${o.direction_id ? o.direction_id : (o.text ? `text:"${o.text.slice(0, 80)}"` : o.fingerprint)}${o.rationale ? ` — ${o.rationale}` : ''}`;
  const rejectedBlocking = portfolioOps.filter(o => _PORTFOLIO_BLOCK_VERDICTS.has(o.verdict) && _HARD_BLOCK_PORTFOLIO_OPS.has(o.op));
  const warned = portfolioOps.filter(
    o => _PORTFOLIO_WARN_VERDICTS.has(o.verdict)
      || (o.verdict === 'rejected' && !_HARD_BLOCK_PORTFOLIO_OPS.has(o.op)),
  );
  const approvedOps = portfolioOps.filter(o => o.verdict === 'approved');
  const rejectedList = rejectedBlocking.length > 0 ? rejectedBlocking.map(renderOp).join('\n') : '  - (none)';
  const warnedList = warned.length > 0 ? warned.map(renderOp).join('\n') : '  - (none)';
  const approvedOpsList = approvedOps.length > 0 ? approvedOps.map(renderOp).join('\n') : '  - (none)';
  const json = JSON.stringify(record);
  const boundedJson = json.length > 8000 ? `${json.slice(0, 8000)}…` : json;
  const statusCaveat = record.status !== 'ok'
    ? `\n_The challenge was **${record.status}** — treat it as approving NOTHING. Do NOT submit any advancing/met evidence_progress this cycle, do NOT call retire-direction, and do NOT rely on any portfolio-operation approval this cycle._`
    : '';
  const portfolioCaveat = record.status !== 'ok'
    ? '\n_The challenge was not OK — it carries NO portfolio-operation verdicts. Hard-blocked ops will be rejected by the backend._'
    : '';
  return [
    '# Independent strategic challenge (adversarial second opinion — advisory gate)',
    "An independent challenge pass re-judged the thinker's self-produced judgments BEFORE you commit them. Its bounded verdict record:",
    '```json',
    boundedJson,
    '```' + statusCaveat,
    '',
    '## Evidence-progress gate (REQUIRED before `set-evidence-progress`)',
    'You may pass an `advancing` or `met` status to `./strategic set-evidence-progress` ONLY for a (direction_id, status) pair the challenge APPROVED below. For any direction the challenge did not approve at that exact status — or if the challenge was blocked/invalid (no approvals) — do NOT submit advancing/met for it: submit `stalled` (always allowed, advisory) or omit it. This gate refines evidence_progress only; it does NOT change retirement critiques, direction caps, staging, version_phase/qualify handling, or operator-wins semantics.',
    'Challenge-approved advancing/met pairs:',
    approvedEvidenceList,
    '',
    '## Retirement gate (REQUIRED before `retire-direction`)',
    'You may call `./strategic retire-direction` ONLY for a direction the challenge APPROVED below. For any direction the challenge did NOT approve — or if the challenge was blocked/invalid — the backend will return HTTP 200 with `"status": "deferred"` instead of committing the retirement.',
    '**A `"status": "deferred"` response is a SOFT NON-ERROR OUTCOME** — it is intentional and final. Log it and move on. Do NOT re-attempt the retire-direction call, do NOT classify it as a failure, and do NOT count it as a missing required call.',
    'Challenge-approved retirement direction IDs:',
    approvedRetirementList,
    '',
    '## Portfolio-operation gate (applies to create/update/retire/stage/approve/reject)',
    'The challenge judged each concrete direction mutation. When you call `./strategic create-direction|update-direction|retire-direction|stage-direction|approve-direction|reject-direction`, the backend matches your call to the verdict below by `direction_id` (existing-direction ops) or by the proposed `text` (creates) — so pass the SAME `direction_id`/`text` the analysis used.' + portfolioCaveat,
    '- **REJECTED (do NOT attempt — hard-blocked):** these destructive / fan-out-sensitive moves were judged unsafe; the backend will reject the call (HTTP 409). Do not retry them this cycle.',
    rejectedList,
    '- **WARNED (advisory — you MAY proceed):** subjective concerns; weigh them but they do not block.',
    warnedList,
    '- **APPROVED (clean):**',
    approvedOpsList,
  ].join('\n');
}

export function writeStrategicHelpers(
  taskId: string,
  projectId: string,
  workspace: string,
  replyPaths: {
    replyKeyPath?: string;
    replyReceiptPath?: string;
    stagedReplyPath?: string;
  } = {},
): void {
  const strategicScript = generateStrategicScript(taskId, workspace, replyPaths);
  fs.writeFileSync(`${workspace}/strategic`, strategicScript, { mode: 0o755 });

  // Strategic agents get the FULL verb set so their ./fetch is byte-identical
  // to the historical output.
  writeFetchHelper(taskId, projectId, workspace, { verbs: STRATEGIC_FETCH_VERBS });
  writeComputeImageHelper(projectId, workspace);

  // Write the ./design-doc CLI via the shared, durable generator (prompt.ts)
  // so strategic_review tasks get the same 429/5xx-retrying set/delete writes
  // as design_doc_update tasks — a single source of truth for the helper.
  writeDesignDocHelper(taskId, workspace);

  excludeAgentHelpers();
}
