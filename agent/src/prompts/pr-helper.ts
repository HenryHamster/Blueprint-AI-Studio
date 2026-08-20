import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { WORKSPACE } from '../index.js';
import { agentTokenShellFn, githubTokenShellFn, jsonStrShellFn, jsonFieldShellFn } from '../token-file.js';
import { validateCfgShellSafety, shouldRunBackendMergeGate, type CodeChangeHelperConfig } from './code-change-helpers.js';

// Load the canonical sr-pr-section.js renderer from disk and base64-encode
// it for safe bash-level injection into the generated ./pr script. Per
// CLAUDE.md "Generating Code for One Language From Another": the embedded
// script lives as a real file (lintable, testable) and the generator reads
// it rather than mirroring its body in a string constant. Lazy-loaded so
// that test suites which mock fs do not crash on module import.
const _SR_SECTION_SCRIPT_PATH = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  '..',
  'sr-pr-section.js',
);
function loadSrSectionScriptB64(): string {
  try {
    return Buffer.from(
      fs.readFileSync(_SR_SECTION_SCRIPT_PATH, 'utf8'),
    ).toString('base64');
  } catch {
    return '';
  }
}

// Same pattern for the viewport-screenshots renderer: a real, lintable/testable
// file on disk, base64-embedded into the generated ./pr so the bash layer never
// has to escape its JS body. Renders the <!-- blueprint:screenshots --> block
// from the viewport snapshot manifest written by `viewport-snapshot.sh --upload`.
const _SCREENSHOTS_SECTION_SCRIPT_PATH = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  '..',
  'screenshots-pr-section.js',
);
function loadScreenshotsSectionScriptB64(): string {
  try {
    return Buffer.from(
      fs.readFileSync(_SCREENSHOTS_SECTION_SCRIPT_PATH, 'utf8'),
    ).toString('base64');
  } catch {
    return '';
  }
}

// Same pattern for the cross-lane guard-suppressions renderer: a real,
// lintable/testable file on disk, base64-embedded into the generated ./pr.
// Renders the <!-- blueprint:guard-suppressions --> block from the aggregate
// `scripts/guard_suppression_receipt.py aggregate --out` writes, so a reviewer
// sees every lane's suppressions (not just the backend pytest lane's) in one
// place at the merge gate.
const _GUARD_SUPPRESSIONS_SECTION_SCRIPT_PATH = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  '..',
  'guard-suppressions-pr-section.js',
);
function loadGuardSuppressionsSectionScriptB64(): string {
  try {
    return Buffer.from(
      fs.readFileSync(_GUARD_SUPPRESSIONS_SECTION_SCRIPT_PATH, 'utf8'),
    ).toString('base64');
  } catch {
    return '';
  }
}

export function writePrHelper(cfg: CodeChangeHelperConfig): void {
  validateCfgShellSafety(cfg);
  // Idempotent push + PR creation/update. Runs a bounded ./check gate first:
  // deterministic failures and the absolute hard timeout block publication.
  // Writes /tmp/pr_url_<task_id> on success so the wrapper + ./done can
  // pick up the URL without re-querying GitHub.
  const _SR_SECTION_SCRIPT_B64 = loadSrSectionScriptB64();
  const _SCREENSHOTS_SECTION_SCRIPT_B64 = loadScreenshotsSectionScriptB64();
  const _GUARD_SUPPRESSIONS_SECTION_SCRIPT_B64 = loadGuardSuppressionsSectionScriptB64();
  const agentTokenFn = agentTokenShellFn();
  const githubTokenFn = githubTokenShellFn();
  const jsonStrFn = jsonStrShellFn();
  const jsonFieldFn = jsonFieldShellFn();
  const script = `#!/bin/bash
# ./pr open --title "..." --body "..."  — ship the diff via PR
#
# Gates on bounded ./check. Deterministic failures and the absolute hard
# timeout block publication. Then commits and pushes
# to ${cfg.workBranch}, and creates a PR (or pushes an update to an
# existing PR for review-fix tasks).
#
# Exits 0 on success (PR URL written to /tmp/pr_url_${cfg.taskId}).
# Non-zero on any failure — error tail printed; fix and retry.

set -uo pipefail
case "\${1:-}" in
  -h|--help)
    echo 'Usage: ./pr open --title <title> --body <body> [--with-tests]  |  ./pr close [--superseded-by <PR#>]'
    exit 0
    ;;
esac
${agentTokenFn}
${githubTokenFn}
${jsonStrFn}
${jsonFieldFn}
WORKSPACE="\${WORKSPACE:-${WORKSPACE}}"
cd "$WORKSPACE" || { echo "Error: cannot cd to $WORKSPACE" >&2; exit 2; }

OP="\${1:-}"
shift || true

# ── ./pr close --superseded-by <PR#> ─────────────────────────────────────────
# Use when post-rebase conflict resolution leaves an empty diff because an
# equivalent sibling PR already merged the same changes.  Closes the current PR
# cleanly, posts a traceability comment, and writes /tmp/pr_url_<task_id> so
# ./done can mark the task COMPLETED-via-supersession.
if [ "$OP" = "close" ]; then
  SUPERSEDED_BY=""
  PR_NUMBER="${cfg.existingPrNumber !== undefined ? String(cfg.existingPrNumber) : ""}"
  while [ $# -gt 0 ]; do
    case "$1" in
      --superseded-by) SUPERSEDED_BY="$2"; shift 2 ;;
      --pr-number)     PR_NUMBER="$2";     shift 2 ;;
      *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
  done

  # Fall back to PR URL written by a prior ./pr open in this session
  if [ -z "$PR_NUMBER" ]; then
    PR_URL_FILE="/tmp/pr_url_${cfg.taskId}"
    if [ -s "$PR_URL_FILE" ]; then
      PR_NUMBER=$(grep -oE '[0-9]+$' "$PR_URL_FILE" || true)
    fi
  fi
  if [ -z "$PR_NUMBER" ]; then
    echo "[pr close] No PR number available. Pass --pr-number <N> or run ./pr open first." >&2
    exit 1
  fi

  API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
  _load_agent_token
  TASK="${cfg.taskId}"

  # Refresh GitHub token
  TOKEN_RESP=$(curl -f -sS --max-time 30 \\
    -H "X-Agent-Token: $TOKEN" \\
    "$API/api/agent/github/token?task_id=$TASK") || {
    echo "[pr close] Failed to fetch GitHub token from orchestrator" >&2
    exit 1
  }
  GH_TOKEN=$(printf '%s' "$TOKEN_RESP" | _json_field data.token)
  if [ -z "$GH_TOKEN" ]; then
    echo "[pr close] orchestrator returned no token: $TOKEN_RESP" >&2
    exit 1
  fi

  # Close the PR via GitHub API
  CLOSE_HTTP=$(curl -sS -o /tmp/pr_close_resp_${cfg.taskId} -w '%{http_code}' --max-time 30 \\
    -X PATCH "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/pulls/$PR_NUMBER" \\
    -H "Authorization: token $GH_TOKEN" \\
    -H "Accept: application/vnd.github+json" \\
    -d '{"state":"closed"}') || CLOSE_HTTP="000"
  if [ "$CLOSE_HTTP" != "200" ]; then
    echo "[pr close] GitHub PATCH /pulls/$PR_NUMBER returned HTTP=$CLOSE_HTTP:" >&2
    cat /tmp/pr_close_resp_${cfg.taskId} >&2
    rm -f /tmp/pr_close_resp_${cfg.taskId}
    exit 1
  fi
  rm -f /tmp/pr_close_resp_${cfg.taskId}

  # Post supersession comment for traceability
  if [ -n "$SUPERSEDED_BY" ]; then
    _comment_msg="Closing: equivalent changes already merged via PR #$SUPERSEDED_BY. No further action needed on this branch."
    COMMENT_BODY=$(printf '{"body":%s}' "$(_json_str "$_comment_msg")")
    curl -sS --max-time 30 \\
      -X POST "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/issues/$PR_NUMBER/comments" \\
      -H "Authorization: token $GH_TOKEN" \\
      -H "Accept: application/vnd.github+json" \\
      -d "$COMMENT_BODY" >/dev/null 2>&1 || true
  fi
  unset GH_TOKEN

  PR_URL="https://github.com/${cfg.owner}/${cfg.repo}/pull/$PR_NUMBER"
  echo "$PR_URL" > /tmp/pr_url_${cfg.taskId}
  echo ""
  echo "[pr] Closed PR #$PR_NUMBER\${SUPERSEDED_BY:+ (superseded by #$SUPERSEDED_BY)}"
  echo "[pr] Next: ./done"
  exit 0
fi

[ "$OP" != "open" ] && { echo "Usage: ./pr open --title <title> --body <body> [--with-tests]  |  ./pr close [--superseded-by <PR#>]"; exit 2; }

TITLE=""
BODY=""
WITH_TESTS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --title) TITLE="$2"; shift 2 ;;
    --body) BODY="$2"; shift 2 ;;
    --with-tests) WITH_TESTS=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ -z "$TITLE" ] && { echo "Error: --title is required" >&2; exit 2; }

# ── Self-review section injection ─────────────────────────────────────────────
# Reads /tmp/self_review_<taskId>.json if present, renders the
# <!-- blueprint:self-review --> block, strips any prior block from BODY
# (idempotent), and appends the fresh one.  No-op when JSON is absent.
SR_FILE="/tmp/self_review_${cfg.taskId}.json"
SR_SKIP_FILE="/tmp/self_review_skip_reason_${cfg.taskId}.txt"
SR_SECTION=""
if [ -f "$SR_FILE" ]; then
  printf '%s' '${_SR_SECTION_SCRIPT_B64}' | base64 -d > /tmp/sr_section_${cfg.taskId}.js
  SR_SECTION=$(node /tmp/sr_section_${cfg.taskId}.js "$SR_FILE" "$SR_SKIP_FILE" 2>/dev/null || true)
  rm -f /tmp/sr_section_${cfg.taskId}.js
fi
if [ -n "$SR_SECTION" ]; then
  BODY=$(printf '%s' "$BODY" | sed '/<!-- blueprint:self-review -->/,/<!-- \\/blueprint:self-review -->/d')
  BODY=$(printf '%s\\n\\n%s' "$BODY" "$SR_SECTION")
fi

# ── Viewport screenshots section injection ────────────────────────────────────
# Renders the <!-- blueprint:screenshots --> block from the snapshot manifest
# that \`viewport-snapshot.sh --upload --task-id <id>\` copies to this known path
# after PUTting the changed/new before/after PNGs to GCS. The renderer emits
# markdown links (NOT inline images) to the admin viewport-shots download
# endpoint, which mints a fresh signed URL per click. No-op when the manifest is
# absent or carries no changed/new pairs (renderer prints nothing).
SHOTS_MANIFEST="/tmp/viewport_manifest_${cfg.taskId}.json"
SHOTS_SECTION=""
if [ -f "$SHOTS_MANIFEST" ] && [ -n '${_SCREENSHOTS_SECTION_SCRIPT_B64}' ]; then
  printf '%s' '${_SCREENSHOTS_SECTION_SCRIPT_B64}' | base64 -d > /tmp/screenshots_section_${cfg.taskId}.js
  SHOTS_SECTION=$(node /tmp/screenshots_section_${cfg.taskId}.js "$SHOTS_MANIFEST" "\${ORCHESTRATOR_URL:-}" "\${PROJECT_ID:-}" "${cfg.taskId}" 2>/dev/null || true)
  rm -f /tmp/screenshots_section_${cfg.taskId}.js
fi
if [ -n "$SHOTS_SECTION" ]; then
  BODY=$(printf '%s' "$BODY" | sed '/<!-- blueprint:screenshots -->/,/<!-- \\/blueprint:screenshots -->/d')
  BODY=$(printf '%s\\n\\n%s' "$BODY" "$SHOTS_SECTION")
fi

${shouldRunBackendMergeGate(cfg) ? `# Mandatory test gate: force WITH_TESTS=1 when the diff touches backend/.
# The agent must not be able to skip backend tests on LTS-branch tasks.
_BASE_GATE="${cfg.baseBranch}"
_CMT_GATE=""
if git rev-parse --verify "origin/$_BASE_GATE" >/dev/null 2>&1; then
  _CMT_GATE=$(git diff --name-only "origin/$_BASE_GATE...HEAD" 2>/dev/null || true)
elif git rev-parse --verify "$_BASE_GATE" >/dev/null 2>&1; then
  _CMT_GATE=$(git diff --name-only "$_BASE_GATE...HEAD" 2>/dev/null || true)
fi
_UNC_GATE=$(git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null)
_CHG_GATE=$(printf '%s\\n%s\\n' "$_CMT_GATE" "$_UNC_GATE" | sort -u | grep -v '^$' || true)
if printf '%s\\n' "$_CHG_GATE" | grep -q '^backend/'; then
  echo "[pr] backend/ touched on LTS branch — forcing WITH_TESTS=1 (mandatory test gate)"
  WITH_TESTS=1
fi
` : ''}
# 1. Run ./check as gate (lint always; tests when --with-tests, or forced on
#    when the backend merge gate applies (self-repo/promotion-target/LTS
#    branch) and the diff touches backend/ — see the WITH_TESTS=1 block above).
# Branch on WITH_TESTS instead of expanding an array. Older bash + set -u
# aborts on an empty array expansion, so just call ./check directly.
GATE_TIMEOUT_SECONDS="\${PR_GATE_TIMEOUT_SECONDS:-600}"
case "$GATE_TIMEOUT_SECONDS" in
  ''|*[!0-9]*|0) GATE_TIMEOUT_SECONDS=600 ;;
esac
if [ "$GATE_TIMEOUT_SECONDS" -gt 900 ]; then
  echo "[pr] PR_GATE_TIMEOUT_SECONDS=$GATE_TIMEOUT_SECONDS exceeds the 900s soft-deadline ceiling; capping it." >&2
  GATE_TIMEOUT_SECONDS=900
fi
GATE_HARD_TIMEOUT_SECONDS="\${PR_GATE_HARD_TIMEOUT_SECONDS:-1800}"
case "$GATE_HARD_TIMEOUT_SECONDS" in
  ''|*[!0-9]*|0) GATE_HARD_TIMEOUT_SECONDS=1800 ;;
esac
if [ "$GATE_HARD_TIMEOUT_SECONDS" -gt 1800 ]; then
  echo "[pr] PR_GATE_HARD_TIMEOUT_SECONDS=$GATE_HARD_TIMEOUT_SECONDS exceeds the 1800s absolute safety ceiling; capping it." >&2
  GATE_HARD_TIMEOUT_SECONDS=1800
fi
if [ "$GATE_HARD_TIMEOUT_SECONDS" -lt "$GATE_TIMEOUT_SECONDS" ]; then
  echo "[pr] PR_GATE_HARD_TIMEOUT_SECONDS=$GATE_HARD_TIMEOUT_SECONDS is below the soft deadline; raising it to \${GATE_TIMEOUT_SECONDS}s." >&2
  GATE_HARD_TIMEOUT_SECONDS="$GATE_TIMEOUT_SECONDS"
fi
GATE_RECEIPT_FILE="/tmp/pr_gate_receipt_${cfg.taskId}.json"
GATE_SECTION=""
GATE_COMMAND="./check"
GATE_ELAPSED_SECONDS=0
GATE_DEADLINE_EXTENDED=false
_VERIFY_TASK_ID="\${TASK_ID:-${cfg.taskId}}"
_VERIFY_RECEIPT="/tmp/project_verification_receipt_\${_VERIFY_TASK_ID}.json"
SUPPRESSED_BASELINE_SECTION=""
GUARD_SUPPRESSIONS_AGGREGATE="/tmp/guard_suppression_aggregate_${cfg.taskId}.json"
GUARD_SUPPRESSIONS_SECTION=""

# A successful backend verification can rely on failures inherited from the
# base branch. Surface those exact structured failure identities in the PR
# body so a passing publication remains honest and reviewable. The receipt is
# optional for stacks without targeted backend verification; malformed or
# unavailable receipts must not change the publication verdict.
render_suppressed_baseline_section() {
  local receipt_file="$1"
  if [ ! -s "$receipt_file" ] || ! command -v python3 >/dev/null 2>&1; then
    return 0
  fi
  # Blueprint's own repo carries the shared multi-lane receipt writer, which
  # renders EVERY attributed lane's suppressions (each identity attributed to
  # the lane that forgave it). A stack without that writer only ever produces
  # the legacy single-lane sidecar, so it falls through to the reader below.
  if [ -r scripts/verification_receipt.py ] \\
    && python3 scripts/verification_receipt.py render-suppressed --receipt "$receipt_file" 2>/dev/null; then
    return 0
  fi
  python3 -c 'import json,sys; payload=json.load(open(sys.argv[1],encoding="utf-8")); pytest=payload.get("pytest"); d=pytest.get("suppressed_failures") if isinstance(pytest,dict) and "suppressed_failures" in pytest else payload.get("suppressed_failures"); lines=["### Suppressed baseline failures"]+["- {}{}{}: {}".format(chr(96),k,chr(96),v.get("detail","") if isinstance(v,dict) else v) for k,v in sorted(d.items())] if isinstance(d,dict) and d else []; print("\\n".join(lines),end="") if lines else None' "$receipt_file" 2>/dev/null || true
}

# Copy the same structured attribution that the PR-body renderer reads into
# the durable gate receipt. This is a projection, not a second attribution
# calculation; malformed or incomplete sidecars simply leave the legacy
# nine-key receipt unchanged.
persist_verification_attribution() {
  if [ ! -s "$_VERIFY_RECEIPT" ] || ! command -v python3 >/dev/null 2>&1; then
    return 0
  fi
  # Same split as the PR-body renderer: the shared writer folds every lane's
  # structured maps into the receipt's closed shape (identities namespaced by
  # lane, attribution_state naming each lane's own state). Stacks without it
  # keep the legacy single-lane projection below.
  if [ -r scripts/verification_receipt.py ] \\
    && python3 scripts/verification_receipt.py project-attribution --gate "$GATE_RECEIPT_FILE" --receipt "$_VERIFY_RECEIPT" 2>/dev/null; then
    return 0
  fi
  python3 -c 'import json,sys; gate_path,verification_path=sys.argv[1:]; gate=json.load(open(gate_path,encoding="utf-8")); payload=json.load(open(verification_path,encoding="utf-8")); pytest=payload.get("pytest"); data=pytest if isinstance(pytest,dict) and "state" in pytest else payload; keys=("new_failures","suppressed_failures","disappeared_failures","skipped_failures"); attrs={"attribution_state":data["state"],**{key:data.get(key,{}) for key in keys}} if isinstance(data,dict) and isinstance(data.get("state"),str) and all(isinstance(data.get(key,{}),dict) for key in keys) else {}; gate.update(attrs); open(gate_path,"w",encoding="utf-8").write(json.dumps(gate,separators=(",",":"))+"\\n")' "$GATE_RECEIPT_FILE" "$_VERIFY_RECEIPT" 2>/dev/null || true
}

# Merge every guard lane's suppression receipt (static, vitest, typecheck,
# viewport, backend pytest — see scripts/guard_suppression_receipt.py) into one
# aggregate file. Fail-open via the CLI's own contract: a write error there
# warns on stderr and still exits 0, so this call never affects GATE_STATUS.
build_guard_suppressions_aggregate() {
  if ! command -v python3 >/dev/null 2>&1 || [ ! -f scripts/guard_suppression_receipt.py ]; then
    return 0
  fi
  python3 scripts/guard_suppression_receipt.py aggregate --task-id "${cfg.taskId}" --out "$GUARD_SUPPRESSIONS_AGGREGATE" >/dev/null 2>&1 || true
}

# Human-readable projection of the aggregate for the PR body: per lane, the
# state it resolved to and the suppression identities it relied on. Absent or
# malformed aggregates render nothing (renderer no-ops on its own).
render_guard_suppressions_section() {
  local aggregate_file="$1"
  if [ ! -s "$aggregate_file" ] || [ -z '${_GUARD_SUPPRESSIONS_SECTION_SCRIPT_B64}' ] || ! command -v node >/dev/null 2>&1; then
    return 0
  fi
  printf '%s' '${_GUARD_SUPPRESSIONS_SECTION_SCRIPT_B64}' | base64 -d > /tmp/guard_suppressions_section_${cfg.taskId}.js
  node /tmp/guard_suppressions_section_${cfg.taskId}.js "$aggregate_file" 2>/dev/null || true
  rm -f /tmp/guard_suppressions_section_${cfg.taskId}.js
}

# Copy the same bounded cross-lane aggregate into the durable gate receipt
# under "guard_suppressions" so it survives on task.context next to the
# existing pytest-only attribution keys. All three buckets
# (suppressed/disappeared/skipped) plus each bucket's producer-side
# observed_count/truncated are carried, so the durable surface can never look
# complete when the producer already capped it. The projection itself lives in
# guard_suppression_receipt.py (project_for_gate_receipt) where it is unit
# tested, per the generated-shell rules in CLAUDE.md. Projection only — the
# backend's sanitize_publication_gate_receipt applies the authoritative
# bound/truncation; a malformed aggregate simply leaves the key out.
persist_guard_suppressions() {
  if [ ! -s "$GUARD_SUPPRESSIONS_AGGREGATE" ] || ! command -v python3 >/dev/null 2>&1 || [ ! -f scripts/guard_suppression_receipt.py ]; then
    return 0
  fi
  python3 scripts/guard_suppression_receipt.py project --aggregate "$GUARD_SUPPRESSIONS_AGGREGATE" --gate-receipt "$GATE_RECEIPT_FILE" >/dev/null 2>&1 || true
}

write_gate_receipt() {
  local status="$1"
  local exit_code="$2"
  local fallback="$3"
  local recorded_at
  GATE_RECEIPT_STATUS="$status"
  recorded_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '{"status":"%s","command":"%s","timeout_seconds":%s,"hard_timeout_seconds":%s,"elapsed_seconds":%s,"deadline_extended":%s,"exit_code":%s,"authoritative_ci_required":%s,"recorded_at":"%s"}\n' \
    "$status" "$GATE_COMMAND" "$GATE_TIMEOUT_SECONDS" "$GATE_HARD_TIMEOUT_SECONDS" "$GATE_ELAPSED_SECONDS" "$GATE_DEADLINE_EXTENDED" "$exit_code" "$fallback" "$recorded_at" > "$GATE_RECEIPT_FILE"
  persist_verification_attribution
  build_guard_suppressions_aggregate
  persist_guard_suppressions
  SUPPRESSED_BASELINE_SECTION=""
  GUARD_SUPPRESSIONS_SECTION=""
  case "$status" in
    passed*) SUPPRESSED_BASELINE_SECTION=$(render_suppressed_baseline_section "$_VERIFY_RECEIPT") ;;
  esac
  GUARD_SUPPRESSIONS_SECTION=$(render_guard_suppressions_section "$GUARD_SUPPRESSIONS_AGGREGATE")
  GATE_SECTION=$(printf '%s\n%s\n%s\n%s\n%s\n%s\n%s' \
    '<!-- blueprint:local-gate -->' \
    '### Local publication gate' \
    "- Status: $status" \
    "- Command: $GATE_COMMAND" \
    "- Deadlines: soft \${GATE_TIMEOUT_SECONDS}s; hard \${GATE_HARD_TIMEOUT_SECONDS}s (absolute ceiling 1800s)" \
    "- Elapsed: \${GATE_ELAPSED_SECONDS}s; soft deadline extended: $GATE_DEADLINE_EXTENDED" \
    "- Exit code: $exit_code; authoritative CI required: $fallback")
  if [ -n "$SUPPRESSED_BASELINE_SECTION" ]; then
    GATE_SECTION=$(printf '%s\n%s' "$GATE_SECTION" "$SUPPRESSED_BASELINE_SECTION")
  fi
  if [ -n "$GUARD_SUPPRESSIONS_SECTION" ]; then
    GATE_SECTION=$(printf '%s\n%s' "$GATE_SECTION" "$GUARD_SUPPRESSIONS_SECTION")
  fi
  GATE_SECTION=$(printf '%s\n%s' "$GATE_SECTION" '<!-- /blueprint:local-gate -->')
}

# A blocked gate normally exits before any PR body exists, so the bounded
# receipt would be lost if the VM later died. Persist it through the
# authenticated HEARTBEAT endpoint, which records telemetry without any task
# state transition.
#
# WHY NOT /api/agent/status: a failing ./check is RETRYABLE — the very next
# lines tell the agent to fix the failures and re-run ./pr open. Posting
# status=FAILED with failure_category=BLOCKED_PRECONDITION would take the
# backend's BLOCKED_PRECONDITION branch (routers/agent_status.py), which
# releases the LLM account slot, retires the LEASED warm-pool member, clears
# vm_instance_id and deletes the VM the agent is still running on. The
# heartbeat channel persists the same sanitized receipt into
# task.context["publication_gate_receipt"] and leaves the task IN_PROGRESS.
record_gate_receipt() {
  local gate_status="$1"
  local api="\${ORCHESTRATOR_URL:-}"
  local receipt_json payload http status_message
  if [ -z "$api" ]; then
    echo "[pr] Warning: cannot persist local-gate receipt — ORCHESTRATOR_URL is unset" >&2
    return 1
  fi
  if ! _load_agent_token; then
    echo "[pr] Warning: cannot persist local-gate receipt — no agent token" >&2
    return 1
  fi
  receipt_json=$(cat "$GATE_RECEIPT_FILE" 2>/dev/null || true)
  if [ -z "$receipt_json" ]; then
    echo "[pr] Warning: cannot persist local-gate receipt — receipt file is empty" >&2
    return 1
  fi
  case "$gate_status" in
    passed*) status_message="local publication gate passed ($gate_status)" ;;
    *) status_message="local publication gate blocked with status $gate_status" ;;
  esac
  payload=$(printf '{"task_id":%s,"status_message":%s,"publication_gate_receipt":%s}' \\
    "$(_json_str "${cfg.taskId}")" \\
    "$(_json_str "$status_message")" \\
    "$receipt_json")
  for attempt in 1 2 3; do
    http=$(printf '%s' "$payload" | curl -sS -o /tmp/pr_gate_receipt_resp_${cfg.taskId} -w '%{http_code}' --max-time 30 \\
      -X POST "$api/api/agent/heartbeat" \\
      -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-) || http="000"
    if [ "$http" = "200" ]; then
      rm -f /tmp/pr_gate_receipt_resp_${cfg.taskId}
      echo "[pr] Persisted local-gate receipt on the task (HTTP=$http)"
      return 0
    fi
    case "$http" in
      # 409 = the task row is locked by a concurrent writer; the heartbeat
      # contract is explicitly "retry next cycle", so it is retryable here too.
      000|409|429|5[0-9][0-9])
        if [ "$attempt" -lt 3 ]; then
          sleep "$attempt"
          continue
        fi
        ;;
    esac
    echo "[pr] Warning: local-gate receipt POST failed with HTTP=$http" >&2
    cat /tmp/pr_gate_receipt_resp_${cfg.taskId} >&2 2>/dev/null || true
    break
  done
  rm -f /tmp/pr_gate_receipt_resp_${cfg.taskId}
  return 1
}

run_pr_gate() {
  GATE_COMMAND="./check"
  [ "$#" -gt 0 ] && GATE_COMMAND="./check $*"
  local gate_started_at gate_finished_at
  gate_started_at=$(date +%s)
  GATE_ELAPSED_SECONDS=0
  GATE_DEADLINE_EXTENDED=false
  if ! command -v timeout >/dev/null 2>&1; then
    # timeout not installed: run ./check directly without a deadline.
    # A missing verdict (check never ran) must not be treated as a pass;
    # running the check unbounded ensures it can produce an explicit exit code.
    echo "[pr] WARNING: timeout not available — running $GATE_COMMAND without deadline" >&2
    ./check "$@"
    GATE_STATUS=$?
  else
    echo "[pr] Running bounded local gate: $GATE_COMMAND (soft deadline \${GATE_TIMEOUT_SECONDS}s; hard deadline \${GATE_HARD_TIMEOUT_SECONDS}s)"
    # The inner timeout makes the historical 600/900s deadline non-destructive:
    # SIGCONT is harmless to a running check and --preserve-status returns the
    # check's eventual real verdict. The outer timeout remains an absolute bound
    # for a genuinely wedged gate and still escalates TERM to KILL after 10s.
    timeout --signal=TERM --kill-after=10s "\${GATE_HARD_TIMEOUT_SECONDS}s" timeout --preserve-status --signal=CONT "\${GATE_TIMEOUT_SECONDS}s" ./check "$@"
    GATE_STATUS=$?
  fi
  gate_finished_at=$(date +%s)
  GATE_ELAPSED_SECONDS=$((gate_finished_at - gate_started_at))
  if [ "$GATE_ELAPSED_SECONDS" -ge "$GATE_TIMEOUT_SECONDS" ]; then
    GATE_DEADLINE_EXTENDED=true
  fi
  case "$GATE_STATUS" in
    0)
      write_gate_receipt passed "$GATE_STATUS" false
      if [ "$GATE_DEADLINE_EXTENDED" = "true" ]; then
        echo "[pr] $GATE_COMMAND completed after the soft \${GATE_TIMEOUT_SECONDS}s deadline and within the hard \${GATE_HARD_TIMEOUT_SECONDS}s bound."
      fi
      return 0
      ;;
    124|137)
      write_gate_receipt timed_out "$GATE_STATUS" false
      echo "[pr] $GATE_COMMAND exceeded its bounded \${GATE_HARD_TIMEOUT_SECONDS}s hard deadline and was terminated." >&2
      echo "[pr] ./check did not complete within the absolute time limit — no verdict was produced." >&2
      echo "[pr] Increase PR_GATE_HARD_TIMEOUT_SECONDS (up to 1800), fix the underlying slowness, or resolve" >&2
      echo "[pr] the failures, then re-run ./pr open." >&2
      return "$GATE_STATUS"
      ;;
    *)
      write_gate_receipt failed "$GATE_STATUS" false
      return "$GATE_STATUS"
      ;;
  esac
}

if [ "$WITH_TESTS" = "1" ]; then
  if ! run_pr_gate test; then
    record_gate_receipt "\${GATE_STATUS}" || true
    echo ""
    echo "[pr] ./check test failed — fix lint/build/tests then re-run ./pr open."
    exit 1
  fi
  # Gate-environment honesty (trapped tasks 22cb9d94 / 6b14ec01, 2026-06-06):
  # ./check may have skipped a pytest gate that cannot run faithfully on this
  # VM (no Docker for the sanctioned harness, no Postgres test env — a bare
  # pytest would degrade to in-memory SQLite where pre-existing Postgres-only
  # tests fail regardless of the diff). Surface the marker so the evidence
  # trail is explicit instead of silently green; CI runs the authoritative
  # suite on the PR.
  if [ "$GATE_STATUS" = "0" ] && [ -s /tmp/check_gate_env_${cfg.taskId}.txt ]; then
    echo ""
    echo "[pr] NOTE: the test gate ran in GATE-ENVIRONMENT mode on this VM:"
    sed 's/^/[pr]   /' /tmp/check_gate_env_${cfg.taskId}.txt
    echo "[pr] Record your faithful test evidence (sanctioned harness run) in the PR body; CI runs the authoritative suite."
    write_gate_receipt passed_with_environment_limitations "$GATE_STATUS" true
  fi
else
  if ! run_pr_gate; then
    record_gate_receipt "\${GATE_STATUS}" || true
    echo ""
    echo "[pr] ./check failed — fix lint/build then re-run ./pr open."
    exit 1
  fi
fi
# Success is a verdict too.  Failure paths have always persisted through the
# heartbeat channel, but a passing rerun previously existed only in the local
# file/PR body.  That left the task's authoritative current receipt red even
# after publication and merge.
record_gate_receipt "\${GATE_RECEIPT_STATUS:-passed}" || true
# Persist the gate result in the PR body; retries replace a stale receipt.
BODY=$(printf '%s' "$BODY" | sed '/<!-- blueprint:local-gate -->/,/<!-- \\/blueprint:local-gate -->/d')
BODY=$(printf '%s\n\n%s' "$BODY" "$GATE_SECTION")


classify_push_error() {
  local msg="$1"
  if printf '%s' "$msg" | grep -qiE 'refusing to allow a GitHub App to create or update workflow|workflow(s)? permission|github app.*workflow'; then

    echo "[pr] GitHub rejected the push because this GitHub App cannot create or update workflow files without the workflows permission." >&2
    echo "[pr] Revert the workflow file(s) below before pushing, or move them to a branch/token path with workflows permission:" >&2
    printf '%s\n' "$_WORKFLOW_CHANGES" | sed 's/^/[pr]   /' >&2
    return 0
  fi
  if printf '%s' "$msg" | grep -qiE 'secret scanning|push protection|GH013|blocked by secret scanning|contains secrets'; then
    echo "[pr] GitHub rejected the push because push protection or secret scanning blocked it." >&2
    echo "[pr] Remove the secret or revert the offending file(s) before pushing, then retry." >&2
    return 0
  fi
  if printf '%s' "$msg" | grep -qiE 'protected branch|branch protection|GH006|required status check|admin rights required'; then
    echo "[pr] GitHub rejected the push because the branch is protected." >&2
    echo "[pr] This push cannot succeed with force-with-lease; push to an unprotected branch or update branch protection." >&2
    return 0
  fi
  return 1
}

# Fail fast on workflow-file edits. The GitHub App installation token used by
# agent pushes cannot create/update workflow files unless the app has explicit
# workflows permission, so these changes can never succeed from this seam.
BASE_REF=""
if git rev-parse --verify "origin/${cfg.baseBranch}" >/dev/null 2>&1; then
  BASE_REF="origin/${cfg.baseBranch}"
elif git rev-parse --verify "${cfg.baseBranch}" >/dev/null 2>&1; then
  BASE_REF="${cfg.baseBranch}"
fi
_WORKFLOW_CHANGES=$(
  {
    if [ -n "$BASE_REF" ]; then
      git -c core.quotepath=false diff --name-only "$BASE_REF...HEAD" 2>/dev/null || true
    fi
    git -c core.quotepath=false diff --name-only --cached 2>/dev/null || true
    git -c core.quotepath=false diff --name-only 2>/dev/null || true
    git -c core.quotepath=false ls-files --others --exclude-standard 2>/dev/null || true
  } | grep '^[.]github/workflows/' | sort -u || true
)
if [ -n "$_WORKFLOW_CHANGES" ]; then
  echo "[pr] GitHub App push blocked: workflow files changed, and this token cannot create or update them without workflows permission." >&2
  echo "[pr] Revert these files before pushing, or move them to a branch/token path with workflows permission:" >&2
  printf '%s\n' "$_WORKFLOW_CHANGES" | sed 's/^/[pr]   /' >&2
  exit 1
fi

# Fail fast on obvious shell-accident junk files before git add -A.
_SHELL_ARTIFACT_CHANGES=$(
  git -c core.quotepath=false status --porcelain 2>/dev/null | while IFS= read -r _line; do
    _path="\${_line:3}"
    case "$_path" in *" -> "*) _path="\${_path##* -> }" ;; esac
    _base="\${_path##*/}"
    case "$_base" in *[A-Za-z0-9._]*|"") ;; *) printf '%s\n' "$_path" ;; esac
  done | sort -u
)
if [ -n "$_SHELL_ARTIFACT_CHANGES" ]; then
  echo "[pr] Suspicious shell-artifact file(s) found in pending changes." >&2
  echo "[pr] Remove these files, or rename them to an intentional non-punctuation name, then re-run ./pr open:" >&2
  printf '%s\n' "$_SHELL_ARTIFACT_CHANGES" | sed 's/^/[pr]   /' >&2
  exit 1
fi

# 2. Stage and commit any pending changes
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git -c user.email=blueprint-agent@automated.dev -c user.name='Blueprint Agent' \\
    commit -m "$TITLE" >/dev/null || {
    echo "[pr] git commit failed" >&2
    git status --short
    exit 1
  }
fi

# Anything to push?
COMMITS_AHEAD=$(git rev-list --count "origin/${cfg.baseBranch}..HEAD" 2>/dev/null || echo 0)
if [ "$COMMITS_AHEAD" = "0" ]; then
  echo "[pr] No commits ahead of ${cfg.baseBranch} — nothing to push."
  echo "[pr] If this branch was superseded (a sibling PR already merged the same changes),"
  echo "[pr] run:  ./pr close --superseded-by <winning-PR-number>"
  echo "[pr] Otherwise make your changes and re-run ./pr open."
  exit 1
fi

# Zero-effective-diff guard (primary defense for the empty-diff-at-merge
# incident). COMMITS_AHEAD>0 only means this branch differs from base by SHA —
# a rebased/superseded branch can be commits-ahead yet contribute ZERO effective
# diff vs base (git diff base...HEAD is empty). Pushing + opening a PR here would
# let a no-op branch flow to ./done and report success. Refuse and route to the
# sanctioned no-op exits instead. Uses the three-dot (merge-base) diff so the
# check matches what a merge actually contributes. Skip only when the base ref is
# unresolvable (BASE_REF empty) — keep the commits-ahead-only behavior there.
if [ -n "$BASE_REF" ]; then
  # Capture the diff's exit STATUS separately from its output. A FAILED git diff
  # (e.g. the merge-base commit is absent on a depth-limited/shallow clone, so
  # "$BASE_REF...HEAD" cannot be resolved) produces empty stdout AND a non-zero
  # exit — indistinguishable from a genuinely-empty diff if we swallow the status
  # with "2>/dev/null || true". Conflating the two hard-blocked real work into
  # no-op dispositions (the empty-PR / no-op-merge merge-accounting-drift class),
  # so we must branch on the exit status, not just on emptiness of the output.
  _EFFECTIVE_DIFF=$(git -c core.quotepath=false diff --name-only "$BASE_REF...HEAD" 2>/tmp/pr_effdiff.log)
  _EFFECTIVE_DIFF_STATUS=$?
  if [ "$_EFFECTIVE_DIFF_STATUS" -ne 0 ]; then
    # Diff failed — most likely the merge-base is missing on a shallow clone.
    # Deepen history so the merge-base becomes reachable, then retry once.
    echo "[pr] effective-diff check: git diff vs base failed (exit $_EFFECTIVE_DIFF_STATUS) — deepening history and retrying (likely a depth-limited clone)." >&2
    git fetch --deepen=200 origin "${cfg.baseBranch}" >/dev/null 2>&1 || true
    git fetch --unshallow origin >/dev/null 2>&1 || true
    _EFFECTIVE_DIFF=$(git -c core.quotepath=false diff --name-only "$BASE_REF...HEAD" 2>/tmp/pr_effdiff.log)
    _EFFECTIVE_DIFF_STATUS=$?
  fi
  if [ "$_EFFECTIVE_DIFF_STATUS" -ne 0 ]; then
    # Still failing after deepen: the check is INCONCLUSIVE, not a proven
    # zero-diff. Do NOT hard-block genuine work on an unresolvable base ref —
    # commits-ahead>0 already held above. Skip the guard and let the push proceed.
    echo "[pr] effective-diff check inconclusive (git diff vs base still failing after deepen) — skipping the zero-diff guard rather than blocking real work:" >&2
    sed 's/^/[pr]   /' /tmp/pr_effdiff.log >&2 2>/dev/null || true
  elif [ -z "$_EFFECTIVE_DIFF" ]; then
    echo "[pr] Branch is $COMMITS_AHEAD commit(s) ahead of ${cfg.baseBranch} but nets to ZERO effective diff vs base." >&2
    echo "[pr] A code-change task that produced no effective diff must NOT open a PR or report success." >&2
    echo "[pr] If a sibling PR already merged the same changes (this branch was superseded)," >&2
    echo "[pr]   run:  ./pr close --superseded-by <winning-PR-number>" >&2
    echo "[pr] If the deliverable already exists on ${cfg.baseBranch} and no change is needed," >&2
    echo "[pr]   run:  ./done --already-done \\"<reason naming the artifact you verified>\\"" >&2
    echo "[pr] Otherwise make your changes and re-run ./pr open." >&2
    exit 1
  fi
fi

API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
_load_agent_token
TASK="${cfg.taskId}"
# _load_github_token's backend fallback reads $TASK_ID (the standard VM env var,
# baked into /etc/agent.env by the orchestrator startup script). Set it from the
# build-time task id so the fallback works even when the ambient shell env does
# not carry TASK_ID — the script already knows its own task id, so it must not
# depend on an external env var to fetch the push token.
TASK_ID="${cfg.taskId}"

# 3. Push. Auth flows through the git credential helper that setupWorkspace
#    configured in this workspace's .git/config at clone time (git-credential.ts).
#    The helper reads the on-disk GitHub token file that the Node agent seeds at
#    clone and keeps fresh on a ~45min cadence, so git push authenticates with a
#    rotated token WITHOUT the installation token ever being written into
#    .git/config or exposed in the git process argv. We therefore do NOT rewrite
#    the origin URL to embed the token (the old _set_github_remote path): that
#    would contradict the token-never-in-.git/config invariant, is redundant with
#    the helper Node's own push relies on, and would leave the secret persisted in
#    .git/config until the next refresh scrubs it. The origin URL stays clean
#    (https://github.com/owner/repo.git); the helper supplies credentials.

# 4. Push with an immutable, explicit compare-and-swap lease.  Refreshing the
# remote-tracking ref after a stale-lease failure silently accepts a competing
# writer as the new lease authority; a later retry can then overwrite that
# writer.  Snapshot the exact remote SHA once, reconcile a lost response only
# when remote == our HEAD, and otherwise fail closed on any competing advance.
_PR_REMOTE_REF="refs/heads/${cfg.workBranch}"
# This task's own unshipped-work checkpoint subject (task-checkpoint.ts
# writes it verbatim).  A tip carrying THIS subject is state we authored on
# an earlier attempt's fail-closed path; prepareTaskWorkBranch already
# restored it into this working tree and publication is documented to be
# what replaces it.  It is recovery state of ours, not a competing writer.
_PR_CHECKPOINT_SUBJECT="blueprint-checkpoint: preserve unshipped task ${cfg.taskId}"
_PR_CHECKPOINT_ADOPTED=0
_pr_tip_is_own_checkpoint() {
  # $1 = the SHA ls-remote just reported for this branch.  True only when
  # that commit is this task's own checkpoint.  Read it locally when the
  # object is already present (prepareTaskWorkBranch fetched the checkpoint
  # it restored); otherwise fetch only the branch tip, preserving the
  # clone's existing shallow/full shape so the pending push is unaffected.
  [ -n "$1" ] || return 1
  _PR_PROBE_REF=refs/remotes/origin/_pr_lease_probe
  if ! git cat-file -e "$1^{commit}" 2>/dev/null; then
    if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
      git fetch --no-tags --quiet --depth=1 origin "+$_PR_REMOTE_REF:$_PR_PROBE_REF" 2>/dev/null || return 1
    else
      git fetch --no-tags --quiet origin "+$_PR_REMOTE_REF:$_PR_PROBE_REF" 2>/dev/null || return 1
    fi
    git cat-file -e "$1^{commit}" 2>/dev/null || return 1
  fi
  _PR_PROBE_SUBJECT=$(git show -s --format=%s "$1" 2>/dev/null || true)
  [ "$_PR_PROBE_SUBJECT" = "$_PR_CHECKPOINT_SUBJECT" ]
}
_PR_EXPECTED_QUERY=$(git ls-remote --heads origin "$_PR_REMOTE_REF" 2>/tmp/pr_ls_remote.log)
_PR_EXPECTED_QUERY_STATUS=$?
if [ "$_PR_EXPECTED_QUERY_STATUS" -ne 0 ]; then
  echo "[pr] cannot establish the exact remote branch lease:" >&2
  sed 's/^/[pr]   /' /tmp/pr_ls_remote.log >&2 2>/dev/null || true
  exit 1
fi
_PR_EXPECTED_REMOTE_SHA=$(printf '%s\n' "$_PR_EXPECTED_QUERY" | awk 'NR == 1 {print $1}')
_PR_LEASE="--force-with-lease=$_PR_REMOTE_REF:$_PR_EXPECTED_REMOTE_SHA"
PUSH_OK=0
for attempt in 1 2 3; do
  if git push "$_PR_LEASE" origin "HEAD:$_PR_REMOTE_REF" 2>/tmp/pr_push.log; then
    PUSH_OK=1
    break
  fi
  if classify_push_error "$(cat /tmp/pr_push.log)"; then
    exit 1
  fi
  _PR_LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null || true)
  _PR_OBSERVED_QUERY=$(git ls-remote --heads origin "$_PR_REMOTE_REF" 2>/tmp/pr_ls_remote.log)
  _PR_OBSERVED_QUERY_STATUS=$?
  if [ "$_PR_OBSERVED_QUERY_STATUS" -ne 0 ]; then
    echo "[pr] push failed and the remote state cannot be reconciled; refusing a blind retry:" >&2
    cat /tmp/pr_push.log >&2
    exit 1
  fi
  _PR_OBSERVED_REMOTE_SHA=$(printf '%s\n' "$_PR_OBSERVED_QUERY" | awk 'NR == 1 {print $1}')
  if [ -n "$_PR_LOCAL_HEAD" ] && [ "$_PR_OBSERVED_REMOTE_SHA" = "$_PR_LOCAL_HEAD" ]; then
    echo "[pr] push response was lost, but the exact remote ref equals local HEAD — reconciled."
    PUSH_OK=1
    break
  fi
  if [ "$_PR_OBSERVED_REMOTE_SHA" != "$_PR_EXPECTED_REMOTE_SHA" ]; then
    # Our own checkpoint is not a competing writer.  Publication is the step
    # that retires it, so refusing here strands finished, check-passing work
    # on a branch nothing else will ever publish (NeurIPS 37eea260, df1c89d0,
    # 9884994c and 0396aaee each blocked permanently this way).  Adopt it as
    # the lease authority exactly ONCE per invocation, and only when the tip
    # carries THIS task's checkpoint subject: a genuine third-party writer
    # still cannot walk the lease forward one race at a time.
    if [ "$_PR_CHECKPOINT_ADOPTED" = "0" ] && _pr_tip_is_own_checkpoint "$_PR_OBSERVED_REMOTE_SHA"; then
      _PR_CHECKPOINT_ADOPTED=1
      _PR_EXPECTED_REMOTE_SHA="$_PR_OBSERVED_REMOTE_SHA"
      _PR_LEASE="--force-with-lease=$_PR_REMOTE_REF:$_PR_EXPECTED_REMOTE_SHA"
      echo "[pr] remote tip $_PR_OBSERVED_REMOTE_SHA is this task's own unshipped-work checkpoint; adopting it as the lease authority and retrying."
      continue
    fi
    echo "[pr] remote branch advanced to a competing SHA; refusing to overwrite it." >&2
    echo "[pr] expected: \${_PR_EXPECTED_REMOTE_SHA:-<absent>}" >&2
    echo "[pr] observed: \${_PR_OBSERVED_REMOTE_SHA:-<absent>}" >&2
    exit 1
  fi
  echo "[pr] push attempt $attempt failed without a remote advance; retrying the original exact lease:"
  cat /tmp/pr_push.log
  sleep 2
done
if [ "$PUSH_OK" = "0" ]; then
  echo "[pr] push failed after 3 attempts" >&2
  exit 1
fi

# 5. Create or update PR via orchestrator-mediated endpoint.
#    Why the orchestrator endpoint instead of \`gh pr create\`:
#    - gh CLI is not installed on agent VMs, and GH_TOKEN isn't in Claude's env.
#    - The endpoint already handles idempotency (returns existing PR if open).
#    - It writes a PULL_REQUEST Artifact row immediately on creation so the
#      reaper can route reaped mid-run tasks to EVALUATING instead of
#      retrying-from-scratch (orphan-PR recovery).
${cfg.existingPrNumber !== undefined ? `
# Existing PR (#${cfg.existingPrNumber}) — review-fix path. Just pushed
# updates; no creation needed. The PULL_REQUEST artifact already exists from
# the original create task, so we don't need to call the endpoint here.
PR_URL="https://github.com/${cfg.owner}/${cfg.repo}/pull/${cfg.existingPrNumber}"
echo "[pr] Pushed updates to existing PR #${cfg.existingPrNumber}"
# Update existing PR body with fresh SR + screenshots + local-gate sections
# (best-effort). The gate receipt is always non-empty after the bounded gate.
if [ -n "$SR_SECTION" ] || [ -n "$SHOTS_SECTION" ] || [ -n "$GATE_SECTION" ]; then
  _PTRESP=$(curl -f -sS --max-time 30 \\
    -H "X-Agent-Token: $TOKEN" \\
    "$API/api/agent/github/token?task_id=$TASK" 2>/dev/null) || _PTRESP=""
  _PTOKEN=$(printf '%s' "$_PTRESP" | _json_field data.token 2>/dev/null || true)
  if [ -n "$_PTOKEN" ]; then
    # Fetch current PR body. Use -f so non-2xx GET responses (auth/rate-limit/
    # 5xx/404) fail fast rather than yielding an empty body that would let the
    # subsequent PATCH wipe the live PR description with just the section(s).
    _GETBODY_FILE=/tmp/pr_get_body_${cfg.taskId}.json
    if curl -f -sS --max-time 30 \\
        "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/pulls/${cfg.existingPrNumber}" \\
        -H "Authorization: token $_PTOKEN" -H "Accept: application/vnd.github+json" \\
        -o "$_GETBODY_FILE" 2>/dev/null; then
      _CURBODY=$(_json_field body < "$_GETBODY_FILE" 2>/dev/null || printf '')
      # Strip any prior blocks (idempotent), then re-append the fresh non-empty ones.
      _UPDBODY=$(printf '%s' "$_CURBODY" | sed '/<!-- blueprint:self-review -->/,/<!-- \\/blueprint:self-review -->/d')
      _UPDBODY=$(printf '%s' "$_UPDBODY" | sed '/<!-- blueprint:screenshots -->/,/<!-- \\/blueprint:screenshots -->/d')
      _UPDBODY=$(printf '%s' "$_UPDBODY" | sed '/<!-- blueprint:local-gate -->/,/<!-- \\/blueprint:local-gate -->/d')
      if [ -n "$SR_SECTION" ]; then
        _UPDBODY=$(printf '%s\\n\\n%s' "$_UPDBODY" "$SR_SECTION")
      fi
      if [ -n "$SHOTS_SECTION" ]; then
        _UPDBODY=$(printf '%s\\n\\n%s' "$_UPDBODY" "$SHOTS_SECTION")
      fi
      if [ -n "$GATE_SECTION" ]; then
        _UPDBODY=$(printf '%s\\n\\n%s' "$_UPDBODY" "$GATE_SECTION")
      fi
      _PHTTP=$(printf '{"body":%s}' "$(_json_str "$_UPDBODY")" | curl -sS -o /dev/null -w '%{http_code}' \\
        --max-time 30 -X PATCH \\
        "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/pulls/${cfg.existingPrNumber}" \\
        -H "Authorization: token $_PTOKEN" -H "Accept: application/vnd.github+json" \\
        -H "Content-Type: application/json" --data-binary @- 2>/dev/null) || _PHTTP="000"
      [ "$_PHTTP" = "200" ] || echo "[pr] Warning: PR body update returned HTTP $_PHTTP — continuing" >&2
    else
      echo "[pr] Warning: GET PR body failed — skipping body section update to avoid wiping description" >&2
    fi
    rm -f "$_GETBODY_FILE"
    unset _PTOKEN
  fi
fi
` : `
PR_PAYLOAD=$(printf '{"task_id":%s,"owner":${JSON.stringify(cfg.owner)},"repo":${JSON.stringify(cfg.repo)},"title":%s,"head":${JSON.stringify(cfg.workBranch)},"base":${JSON.stringify(cfg.baseBranch)},"body":%s}' \\
  "$(_json_str "$TASK")" \\
  "$(_json_str "$TITLE")" \\
  "$(_json_str "$BODY")")
PR_RESP=""
PR_OK=0
PR_HDR_FILE=/tmp/pr_create_hdrs_${cfg.taskId}
for attempt in 1 2 3 4 5; do
  : > "$PR_HDR_FILE"
  PR_HTTP=$(printf '%s' "$PR_PAYLOAD" | curl -sS -o /tmp/pr_create_resp_${cfg.taskId} -D "$PR_HDR_FILE" -w '%{http_code}' --max-time 90 \\
    -X POST "$API/api/agent/github/pull-requests" \\
    -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-) || PR_HTTP="000"
  if [ "$PR_HTTP" = "200" ] || [ "$PR_HTTP" = "201" ] || [ "$PR_HTTP" = "202" ]; then
    PR_RESP=$(cat /tmp/pr_create_resp_${cfg.taskId})
    PR_OK=1; break
  fi
  # Classify the status. RETRYABLE: 401/403 (maybe a stale agent token), 429
  # (rate limited), 5xx (incl. 503 = the backend refusing to create because it
  # could not confirm no-duplicate while the GitHub quota is drained), and 000
  # (curl transport/timeout). Anything else (400/404/409/422 ...) is permanent.
  case "$PR_HTTP" in
    401|403|429|000|5[0-9][0-9]) : ;;
    *)
      echo "[pr] PR creation attempt $attempt got HTTP=$PR_HTTP (permanent failure):" >&2
      cat /tmp/pr_create_resp_${cfg.taskId} >&2
      rm -f /tmp/pr_create_resp_${cfg.taskId} "$PR_HDR_FILE"
      exit 1 ;;
  esac
  # A 401/403 can be a stale agent token — re-read it before backing off.
  case "$PR_HTTP" in
    401|403) _load_agent_token 2>/dev/null || true ;;
  esac
  if [ "$attempt" -ge 5 ]; then
    echo "[pr] PR creation attempt $attempt got HTTP=$PR_HTTP — no attempts left" >&2
    cat /tmp/pr_create_resp_${cfg.taskId} >&2
    break
  fi
  # Backoff: honor a server-sent Retry-After (seconds) when present — the backend
  # returns 503 + Retry-After while the GitHub quota is drained. Cap it so a long
  # reset window (up to ~1h) never blocks the VM past its reaper heartbeat budget.
  # Otherwise exponential backoff (5,10,20,40s) — never the old tight ~3-9s loop.
  RETRY_AFTER=$(grep -i '^Retry-After:' "$PR_HDR_FILE" 2>/dev/null | tail -1 | tr -cd '0-9')
  if [ -n "$RETRY_AFTER" ] && [ "$RETRY_AFTER" -gt 0 ] 2>/dev/null; then
    BACKOFF="$RETRY_AFTER"
  else
    BACKOFF=$((5 * (1 << (attempt - 1))))
  fi
  [ "$BACKOFF" -gt 120 ] && BACKOFF=120
  echo "[pr] PR creation attempt $attempt got HTTP=$PR_HTTP — retrying in \${BACKOFF}s..." >&2
  sleep "$BACKOFF"
done
rm -f /tmp/pr_create_resp_${cfg.taskId} "$PR_HDR_FILE"
if [ "$PR_OK" = "0" ]; then
  echo "[pr] PR creation failed after 5 retries — orchestrator/GitHub quota unavailable" >&2
  exit 1
fi
PR_URL=$(printf '%s' "$PR_RESP" | _json_field data.html_url)
if [ -z "$PR_URL" ]; then
  echo "[pr] orchestrator response had no html_url: $PR_RESP" >&2
  exit 1
fi
`}

# 6. Persist PR URL for ./done + wrapper detection
echo "$PR_URL" > /tmp/pr_url_${cfg.taskId}

# 7. Post-create mergeable check.
#
# GitHub computes mergeable status asynchronously (a few seconds after
# the head commit lands). Poll briefly, then if the PR is in a real
# conflict state (mergeable=false + state=dirty) tell the agent so
# they can rebase and resolve while the workspace is still warm. This
# is dramatically cheaper than letting the orchestrator spawn a fresh
# fix-task from a cold VM that has to re-derive the change intent.
#
# State semantics (from GitHub):
#   clean / has_hooks  → mergeable, will merge / fast-forward fine
#   blocked / unstable → required checks/reviews; Blueprint waits, then
#                        revalidates the exact head/base before managed merge
#   behind             → base advanced; Blueprint must rebase and revalidate
#   dirty              → real merge conflict; needs rebase + manual resolve
#   draft              → marked draft; no action
#
# Only \`dirty\` triggers the warning + exit 4. Everything else exits 0.
PR_NUMBER=$(printf '%s' "$PR_URL" | grep -oE '[0-9]+$' || true)
if [ -n "$PR_NUMBER" ]; then
  TOKEN_RESP=$(curl -f -sS --max-time 30 \\
    -H "X-Agent-Token: $TOKEN" \\
    "$API/api/agent/github/token?task_id=$TASK" 2>/dev/null) || TOKEN_RESP=""
  GH_TOKEN_PR=$(printf '%s' "$TOKEN_RESP" | _json_field data.token 2>/dev/null || true)
  if [ -n "$GH_TOKEN_PR" ]; then
    PR_MERGEABLE="null"
    PR_STATE="unknown"
    for pr_attempt in 1 2 3 4 5; do
      _PR_DETAIL=$(curl -sS --max-time 20 \\
        -H "Authorization: token $GH_TOKEN_PR" \\
        -H "Accept: application/vnd.github+json" \\
        "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/pulls/$PR_NUMBER" 2>/dev/null || echo "")
      PR_MERGEABLE=$(printf '%s' "$_PR_DETAIL" | _json_field mergeable null 2>/dev/null || echo "null")
      PR_STATE=$(printf '%s' "$_PR_DETAIL" | _json_field mergeable_state unknown 2>/dev/null || echo "unknown")
      [ "$PR_MERGEABLE" != "null" ] && break
      sleep 3
    done
    unset GH_TOKEN_PR
    if [ "$PR_MERGEABLE" = "false" ] && [ "$PR_STATE" = "dirty" ]; then
      echo ""
      echo "[pr] WARNING: PR #$PR_NUMBER has merge conflicts with origin/${cfg.baseBranch}"
      echo "[pr]   mergeable=false, mergeable_state=dirty"
      echo "[pr] "
      echo "[pr] You're still in the workspace — resolve here, much cheaper than"
      echo "[pr] spawning a fix-task from a fresh VM that has to re-derive intent."
      echo "[pr] "
      echo "[pr] Resolution flow:"
      echo "[pr]   git fetch origin ${cfg.baseBranch}"
      echo "[pr]   git rebase origin/${cfg.baseBranch}"
      echo "[pr]   # Resolve conflicts in the listed files (use the Edit tool)."
      echo "[pr]   # If a conflict is in a file YOUR PR did not intend to touch,"
      echo "[pr]   # the right move is usually \\\`git checkout --theirs <file>\\\` to"
      echo "[pr]   # take the base branch's version, then re-apply your edits to"
      echo "[pr]   # the files you OWN. Don't accidentally revert sibling work."
      echo "[pr]   git rebase --continue"
      echo "[pr]   ./check       # re-verify before pushing"
      echo "[pr]   ./pr open --title \\"...\\" --body \\"...\\"   # re-push (force-with-lease)"
      echo "[pr] "
      echo "[pr] Limit: ~2 attempts. If you can't resolve cleanly (the conflict is"
      echo "[pr] structural, not just a textual overlap, or it's in code your task"
      echo "[pr] should not be modifying), call \\\`./blocked \\"<reason>\\"\\\` instead."
      echo "[pr] The orchestrator will spawn a fix-task as backstop."
      exit 4
    fi
    if [ "$PR_STATE" = "behind" ]; then
      echo "[pr] note: PR #$PR_NUMBER is behind origin/${cfg.baseBranch}; Blueprint will require a managed rebase and exact-target revalidation before merge"
    fi
  fi
fi

echo ""
echo "[pr] OK — $PR_URL"
echo "[pr] Next: ./done   (or call ./pr again if you push more commits)"
`;
  fs.writeFileSync(`${WORKSPACE}/pr`, script, { mode: 0o755 });
}
