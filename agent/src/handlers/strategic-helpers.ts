import fs from 'fs';
import type WebSocket from 'ws';
import { excludeAgentHelpers, writeDesignDocHelper, shellEscapeDoubleQuoted, DEFAULT_TOOL_GUIDANCE } from '../prompt.js';
import { extractJsonObject } from '../json-extract.js';
import { VmConfigMissingError } from '../providers/runners.js';

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

// The full read-only fetch vocabulary. Each verb maps to a GET endpoint under
// /api/agent/projects/{project_id}/... — all share the standard agent auth and
// are callable by any of the project's agent tokens, so the helper works for
// every task type, not just strategic-cycle agents.
export type FetchVerb = 'problem-model' | 'direction' | 'playbook' | 'gap' | 'message' | 'section' | 'knowledge';

// Canonical order of the full vocabulary. STRATEGIC provisions all of these.
// `knowledge` is a SEARCH-shaped verb (token-scoped list/search + get-by-id),
// distinct from the single-id project-path verbs that precede it; it is appended
// last so the historical path-verb ordering is otherwise preserved.
export const STRATEGIC_FETCH_VERBS: FetchVerb[] = [
  'problem-model', 'direction', 'playbook', 'gap', 'message', 'section', 'knowledge',
];

// Read-only verb subsets for non-strategic agents. Research resolves directions,
// the problem-model, and gaps it was handed by reference; plan resolves
// directions, design-doc sections, and playbook entries for architectural
// grounding; design-doc curation resolves directions, design-doc sections,
// playbook entries, gaps, and operator messages for architecture review.
// All subsets are strictly read-only (every verb is a GET).
export const RESEARCH_FETCH_VERBS: FetchVerb[] = ['direction', 'problem-model', 'gap', 'knowledge'];
export const PLAN_FETCH_VERBS: FetchVerb[] = ['direction', 'section', 'playbook', 'knowledge'];
export const DESIGN_DOC_FETCH_VERBS: FetchVerb[] = ['direction', 'section', 'playbook', 'gap', 'message', 'knowledge'];

// Exact case-statement body for each PATH-SUFFIX verb. The spacing is preserved
// verbatim so the path-verb arms stay byte-identical to the original. `knowledge`
// is intentionally absent here — it is SEARCH-shaped (its own base URL, optional
// query filters, no project path) and gets a self-contained arm below.
const FETCH_VERB_CASE: Partial<Record<FetchVerb, string>> = {
  'problem-model': 'problem-model) PATH_SUFFIX="problem-model"',
  'direction':     'direction)  PATH_SUFFIX="directions/$ID"',
  'playbook':      'playbook)   PATH_SUFFIX="playbook/$ID"',
  'gap':           'gap)        PATH_SUFFIX="gaps/$ID"',
  'message':       'message)    PATH_SUFFIX="messages/$ID"',
  'section':       'section)    PATH_SUFFIX="design-doc/$ID"',
};

// Self-contained case arm for the SEARCH-shaped `knowledge` verb. Unlike the
// path-suffix verbs it hits the token-scoped /api/agent/knowledge-documents
// endpoint directly (project resolved from the token, not the URL): a bare
// positional id pulls one document's full body, otherwise optional
// --topic/--direction/--q/--source-kind/--tag flags become URL-encoded query
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
          --q)           QARGS+=(--data-urlencode "q=\${2:-}"); shift 2 ;;
          --tag)         QARGS+=(--data-urlencode "tag=\${2:-}"); shift 2 ;;
          *) echo "[ERROR] fetch knowledge: unknown flag '$1' (use --topic/--direction/--q/--source-kind/--tag or a document id)" >&2; exit 1 ;;
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
const FETCH_VERB_HELP: Record<FetchVerb, string> = {
  'problem-model': '`./fetch problem-model` — the full current problem model (no id)',
  'direction':     '`./fetch direction <id>` — full detail for a direction',
  'playbook':      '`./fetch playbook <key>` — a full playbook entry',
  'gap':           '`./fetch gap <id>` — a full gap record',
  'message':       '`./fetch message <ref>` — an operator message (short ref or UUID)',
  'section':       '`./fetch section <key>` — a full design-doc section',
  'knowledge':     '`./fetch knowledge [--topic T] [--direction D] [--q TERM] [--source-kind K] [--tag T]` — list/search knowledge documents (web_research/design outputs); `./fetch knowledge <id>` for one document\'s full body',
};

function generateFetchScript(projectIdRaw: string, verbs: FetchVerb[]): string {
  const projectId = shellEscapeDoubleQuoted(projectIdRaw);
  // Partition the full vocabulary against the granted subset in canonical order
  // so output is deterministic regardless of the order verbs are passed in.
  const granted = STRATEGIC_FETCH_VERBS.filter(v => verbs.includes(v));
  const rejected = STRATEGIC_FETCH_VERBS.filter(v => !verbs.includes(v));
  const verbList = granted.join('|');
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
KIND="\${1:?Usage: fetch ${verbList} [id]}"
ID="\${2:-}"
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
PROJECT="${projectId}"
[ -n "$PROJECT" ] || { echo "[ERROR] no project_id available; ./fetch is unavailable in this workspace" >&2; exit 1; }

# problem-model takes no id; everything else requires one. For "message" the
# id may be a short ref (first 8+ hex chars of the message UUID — the same ref
# operators see in the admin UI) or a full UUID; the backend resolves it and
# fails loudly on ambiguous/missing refs.
case "$KIND" in
${caseBody}
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
You were launched from a context snapshot that may have trimmed, summarized, or only referenced some items. When a direction, design-doc section, playbook entry, problem-model, or gap you need was shipped trimmed/summarized or only by id, run \`./fetch <verb> [id]\` to pull its full current content before acting on it. Read-only and project-scoped.
Available verbs in this workspace:
${lines}

`;
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

export interface StrategicChallengeRecord {
  challenge_version: number;
  status: 'ok' | 'blocked' | 'invalid';
  evidence_progress: ChallengeEvidenceProgressVerdict[];
  retirement_critique_quality: { verdict: string; rationale: string };
  portfolio_shape_risk: { verdict: string; rationale: string };
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
    retirement_critique_quality: { verdict: 'not_assessed', rationale: note.slice(0, _RATIONALE_MAX) },
    portfolio_shape_risk: { verdict: 'not_assessed', rationale: note.slice(0, _RATIONALE_MAX) },
    notes: note.slice(0, _NOTES_MAX),
  };
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
    // Only an explicitly-ok challenge may carry evidence_progress through.
    evidence_progress: isOk ? normalizeEvidenceProgress(obj.evidence_progress) : [],
    retirement_critique_quality: coerceVerdictBlock(obj.retirement_critique_quality),
    portfolio_shape_risk: coerceVerdictBlock(obj.portfolio_shape_risk),
  };
  const notes = _safeStr(obj.notes).trim().slice(0, _NOTES_MAX);
  if (notes) record.notes = notes;
  return record;
}

// Build the adversarial challenge prompt. The challenger reads the thinker's
// analysis plus the directions/portfolio context the thinker saw, and emits
// ONLY the strict verdict JSON.
export function buildStrategicChallengePrompt(opts: {
  analysisText: string;
  dirSummaryText: string;
  pastRetirementCritiquesSection: string;
  portfolioCompositionText: string;
}): string {
  const analysis = (opts.analysisText || '').slice(0, 40_000);
  const ctxBlocks = [
    opts.dirSummaryText ? `## Existing directions (what the thinker saw)\n${opts.dirSummaryText}` : '',
    opts.portfolioCompositionText ? `## Portfolio composition\n${opts.portfolioCompositionText}` : '',
    opts.pastRetirementCritiquesSection || '',
  ].filter(Boolean).join('\n\n');
  return `# Role: independent strategic challenge (adversarial second opinion)

You are an INDEPENDENT adversarial reviewer. A strategist ("thinker") just produced a strategic analysis containing self-assessed judgments. Your job is to CHALLENGE — not rubber-stamp — three classes of those self-produced judgments BEFORE they are committed:

1. **evidence_progress** — the thinker's claims that a long-running direction is "advancing" or "met". These are self-serving by construction. APPROVE an advancing/met claim ONLY when the analysis presents concrete, observable evidence that the direction's own success_evidence is actually being met. If the evidence is thin, assumed, or absent, REJECT (a stalled reality must not be dressed up as progress). When in doubt, REJECT — approving a false "advancing" is the costly error.
2. **retirement_critique_quality** — whether the thinker's retirement critiques name a real pattern + lesson (solid) or are shallow/boilerplate.
3. **portfolio_shape_risk** — whether the resulting active-direction portfolio is over-concentrated, has coverage gaps, or is over capacity.

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
  "retirement_critique_quality": {"verdict": "solid|shallow|not_assessed", "rationale": "one line"},
  "portfolio_shape_risk": {"verdict": "low|elevated|high", "rationale": "one line"}
}
Rules:
- Add an evidence_progress entry for EVERY direction the thinker rated advancing or met (verdict "approved" only with concrete evidence, otherwise "rejected"). Directions the thinker called stalled need no entry — stalled is advisory and never gated. If the thinker rated none, emit an empty array.
- "retirement_critique_quality" and "portfolio_shape_risk" are REQUIRED keys even when there is little to say.
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
    sendLog(ws, `[INFO] Strategic challenge complete (status=${record.status}, evidence_verdicts=${record.evidence_progress.length}).\n`);
  }
  return record;
}

// Render the challenge record + the evidence-progress gate for the executor
// prompt. The executor may submit an advancing/met verdict only for a
// challenge-approved (direction_id, status) pair listed here.
export function buildStrategicChallengeExecutorSection(record: StrategicChallengeRecord): string {
  // Only ok-status records can approve advancing/met pairs; blocked/invalid
  // records must present an empty approval list to the executor.
  const approved = record.status === 'ok'
    ? record.evidence_progress.filter(
        e => e.verdict === 'approved' && (e.status === 'advancing' || e.status === 'met'),
      )
    : [];
  const approvedList = approved.length > 0
    ? approved.map(e => `  - ${e.direction_id} → ${e.status}`).join('\n')
    : '  - (none — no advancing/met evidence_progress verdict was approved this cycle)';
  const json = JSON.stringify(record);
  const boundedJson = json.length > 8000 ? `${json.slice(0, 8000)}…` : json;
  const statusCaveat = record.status !== 'ok'
    ? `\n_The challenge was **${record.status}** — treat it as approving NOTHING. Do NOT submit any advancing/met evidence_progress this cycle._`
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
    approvedList,
  ].join('\n');
}

export function writeStrategicHelpers(taskId: string, projectId: string, workspace: string): void {
  const strategicScript = generateStrategicScript(taskId);
  fs.writeFileSync(`${workspace}/strategic`, strategicScript, { mode: 0o755 });

  // Strategic agents get the FULL verb set so their ./fetch is byte-identical
  // to the historical output.
  writeFetchHelper(taskId, projectId, workspace, { verbs: STRATEGIC_FETCH_VERBS });

  // Write the ./design-doc CLI via the shared, durable generator (prompt.ts)
  // so strategic_review tasks get the same 429/5xx-retrying set/delete writes
  // as design_doc_update tasks — a single source of truth for the helper.
  writeDesignDocHelper(taskId, workspace);

  excludeAgentHelpers();
}
