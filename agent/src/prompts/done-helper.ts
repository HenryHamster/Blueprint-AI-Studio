import fs from 'fs';
import { WORKSPACE } from '../index.js';
import { agentTokenShellFn, jsonStrShellFn, jsonFieldShellFn } from '../token-file.js';
import { validateCfgShellSafety, type CodeChangeHelperConfig } from './code-change-helpers.js';

export function writeDoneHelper(cfg: CodeChangeHelperConfig): void {
  validateCfgShellSafety(cfg);
  // Posts COMPLETED to the orchestrator with the PR URL from /tmp/pr_url_<task_id>.
  // Also writes /tmp/done_<task_id> as a marker for the wrapper to detect on
  // exit and skip its fallback push.
  const agentTokenFn = agentTokenShellFn();
  const jsonStrFn = jsonStrShellFn();
  const jsonFieldFn = jsonFieldShellFn();
  const script = `#!/bin/bash
# ./done — mark task COMPLETED. Requires ./pr open to have run first
# (writes /tmp/pr_url_${cfg.taskId} which this reads). Posts the COMPLETED
# status to the orchestrator with the PR URL, then writes a marker so the
# wrapper exits cleanly without re-doing commit/push.
#
# Usage:
#   ./done                                     # standard: ./check must pass
#   ./done --warn-out-of-scope "<reason>"      # judgment-call escape hatch:
#       ship despite ./check failures when those failures are PRE-EXISTING
#       on origin/${cfg.baseBranch} and out of scope for this PR. Posts a FEEDBACK
#       project message describing the pre-existing issues + your reason
#       so strategic/operators can spawn cleanup work later. The reason
#       text MUST name the specific gate(s) and file(s) that failed and
#       explain why fixing them is out of scope for this change (i.e.
#       the failure already existed on the base branch and remediating
#       it would require unrelated work beyond this task's diff).
#
#       USE ./blocked INSTEAD when the failure requires introducing a
#       new bug or precondition (missing package, infra gap, agent
#       can't even start the work). --warn-out-of-scope is for issues
#       that already existed on the base branch BEFORE this PR.
#
#   ./done --already-done "<reason>"           # no-op / already-satisfied exit:
#       AFFIRMATIVELY signal that the task's deliverable ALREADY EXISTS on
#       origin/${cfg.baseBranch} and no code change is needed — so there is NO
#       diff and NO PR to produce. Marks the task COMPLETED (no PR) with the
#       reason recorded so operators see why there's no PR.
#
#       Use ONLY when you have VERIFIED the deliverable is already present
#       (e.g. a sibling change already added the file / function / endpoint
#       you were asked to add, and producing a duplicate would break an
#       invariant). The reason MUST name the concrete artifact you verified
#       and where you confirmed it lives (≥30 chars): the file path / symbol
#       and the source you checked, plus why duplicating it would be wrong.
#
#       This is NOT a give-up verb. If you could NOT complete the work
#       (errored, gave up, ran out of ideas) use ./fail. If an upstream
#       precondition blocks you use ./blocked. --already-done is strictly for
#       "I checked and the work is genuinely already done."
#   ./done --reproduced "<reason>"             # finding-derived fix path:
#       you confirmed the cited defect reproduced on current HEAD and this PR
#       fixes that reproduced defect. Posts the durable reproduce-on-HEAD
#       verdict {status:"reproduced"} alongside the normal COMPLETED payload.
#   ./done --indeterminate "<reason>"          # finding-derived shipped-but-
#       unproven path: you landed code but could not prove the defect
#       reproduced on current HEAD. Posts the durable verdict
#       {status:"indeterminate"} so scoring stays neutral rather than
#       incorrectly inferring a proven fix.

set -uo pipefail
${agentTokenFn}
${jsonStrFn}
${jsonFieldFn}

WARN_OUT_OF_SCOPE_REASON=""
ALREADY_DONE_REASON=""
REPRODUCED_REASON=""
INDETERMINATE_REASON=""
SKIP_NEXT_KIND=""
for i in "$@"; do
  if [ -n "$SKIP_NEXT_KIND" ]; then
    case "$SKIP_NEXT_KIND" in
      warn) WARN_OUT_OF_SCOPE_REASON="$i" ;;
      already) ALREADY_DONE_REASON="$i" ;;
      reproduced) REPRODUCED_REASON="$i" ;;
      indeterminate) INDETERMINATE_REASON="$i" ;;
    esac
    SKIP_NEXT_KIND=""
    continue
  fi
  case "$i" in
    --warn-out-of-scope)
      SKIP_NEXT_KIND="warn"
      ;;
    --warn-out-of-scope=*)
      WARN_OUT_OF_SCOPE_REASON="\${i#--warn-out-of-scope=}"
      ;;
    --already-done)
      SKIP_NEXT_KIND="already"
      ;;
    --already-done=*)
      ALREADY_DONE_REASON="\${i#--already-done=}"
      ;;
    --reproduced)
      SKIP_NEXT_KIND="reproduced"
      ;;
    --reproduced=*)
      REPRODUCED_REASON="\${i#--reproduced=}"
      ;;
    --indeterminate)
      SKIP_NEXT_KIND="indeterminate"
      ;;
    --indeterminate=*)
      INDETERMINATE_REASON="\${i#--indeterminate=}"
      ;;
  esac
done
if [ "$SKIP_NEXT_KIND" = "warn" ] && [ -z "$WARN_OUT_OF_SCOPE_REASON" ]; then
  echo "Error: --warn-out-of-scope requires a reason argument" >&2
  echo "Usage: ./done --warn-out-of-scope \\"<reason>\\"" >&2
  exit 2
fi
if [ "$SKIP_NEXT_KIND" = "already" ] && [ -z "$ALREADY_DONE_REASON" ]; then
  echo "Error: --already-done requires a reason argument" >&2
  echo "Usage: ./done --already-done \\"<reason naming the artifact you verified>\\"" >&2
  exit 2
fi
if [ "$SKIP_NEXT_KIND" = "reproduced" ] && [ -z "$REPRODUCED_REASON" ]; then
  echo "Error: --reproduced requires a reason argument" >&2
  echo "Usage: ./done --reproduced \\"<what reproduced on HEAD and what this PR fixes>\\"" >&2
  exit 2
fi
if [ "$SKIP_NEXT_KIND" = "indeterminate" ] && [ -z "$INDETERMINATE_REASON" ]; then
  echo "Error: --indeterminate requires a reason argument" >&2
  echo "Usage: ./done --indeterminate \\"<why reproduce-on-HEAD remained ambiguous>\\"" >&2
  exit 2
fi
if [ -n "$WARN_OUT_OF_SCOPE_REASON" ] && [ \${#WARN_OUT_OF_SCOPE_REASON} -lt 30 ]; then
  echo "Error: --warn-out-of-scope reason must be at least 30 chars (you gave \${#WARN_OUT_OF_SCOPE_REASON})." >&2
  echo "       Be specific: which gates failed, which files, why fixing is out of scope." >&2
  exit 2
fi
if [ -n "$WARN_OUT_OF_SCOPE_REASON" ] && [ -n "$ALREADY_DONE_REASON" ]; then
  echo "Error: --warn-out-of-scope and --already-done are mutually exclusive." >&2
  echo "       --already-done means there is no diff/PR; --warn-out-of-scope ships a PR." >&2
  exit 2
fi
_VERDICT_FLAG_COUNT=0
[ -n "$ALREADY_DONE_REASON" ] && _VERDICT_FLAG_COUNT=$((_VERDICT_FLAG_COUNT + 1))
[ -n "$REPRODUCED_REASON" ] && _VERDICT_FLAG_COUNT=$((_VERDICT_FLAG_COUNT + 1))
[ -n "$INDETERMINATE_REASON" ] && _VERDICT_FLAG_COUNT=$((_VERDICT_FLAG_COUNT + 1))
if [ "$_VERDICT_FLAG_COUNT" -gt 1 ]; then
  echo "Error: --already-done, --reproduced, and --indeterminate are mutually exclusive." >&2
  echo "       Pick exactly one reproduce-on-HEAD verdict for this completion." >&2
  exit 2
fi

API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
_load_agent_token
TASK="${cfg.taskId}"

# ── --already-done: affirmative no-op completion (no PR, no diff) ──────────────
# A LEGITIMATE "the deliverable already exists on the base branch" exit. This
# is the ONLY clean path to COMPLETED without a PR — it requires the agent to
# AFFIRMATIVELY assert (with a specific reason) that the work is already done,
# so a task that merely produced no diff because it errored/gave up can NOT
# masquerade as completed (that still routes through ./fail or the wrapper's
# FAILED fallback). Posts COMPLETED with no artifact_type/pr_url; the reason is
# recorded as no_op_reason on the payload AND as the logs/status message so
# operators see why there's no PR.
if [ -n "$ALREADY_DONE_REASON" ]; then
  if [ \${#ALREADY_DONE_REASON} -lt 30 ]; then
    echo "Error: --already-done reason must be at least 30 chars (you gave \${#ALREADY_DONE_REASON})." >&2
    echo "       Name the concrete artifact you verified already exists and where (file path / sibling PR)." >&2
    exit 2
  fi
  # Idempotency: if the marker already exists this is a re-run — no-op cleanly.
  if [ -f /tmp/done_${cfg.taskId} ]; then
    echo "[done] already marked — no-op (already-done: no PR)"
    exit 0
  fi
  ND_LOGS="Completed: no changes needed — deliverable already present. $ALREADY_DONE_REASON"
  # Also emit the canonical reproduce-on-HEAD verdict: an affirmative no-op exit
  # means the deliverable already exists / the reported defect did not reproduce
  # on current HEAD, which is exactly the "already_resolved" verdict. The reason
  # ($r, ≥30 chars) doubles as the verdict reason. Additive + backward-compatible:
  # no_op_reason is still emitted for older backends, and backends that predate
  # ReproduceVerdict drop the unknown field via extra="ignore".
  ND_PAYLOAD=$(printf '{"task_id":%s,"status":"COMPLETED","no_op_reason":%s,"logs":%s,"reproduce_verdict":{"status":"already_resolved","reason":%s}}' \\
    "$(_json_str "$TASK")" \\
    "$(_json_str "$ALREADY_DONE_REASON")" \\
    "$(_json_str "$ND_LOGS")" \\
    "$(_json_str "$ALREADY_DONE_REASON")")
  # Stale-result envelope: persist BEFORE the POST so a mid-retry VM kill can
  # be replayed by the next agent boot (mirrors the PR path below).
  ND_ENVELOPE="/tmp/task_${cfg.taskId}_result.json"
  printf '%s' "$ND_PAYLOAD" > "$ND_ENVELOPE" 2>/dev/null || \\
    echo "[done] WARNING: failed to persist stale-result envelope to $ND_ENVELOPE" >&2
  ND_POSTED=0
  for attempt in 1 2 3 4 5; do
    HTTP=$(printf '%s' "$ND_PAYLOAD" | curl -sS -o /tmp/done_resp_${cfg.taskId} -w '%{http_code}' --max-time 30 \\
      -X POST "$API/api/agent/status" \\
      -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-) || HTTP="000"
    if [ "$HTTP" = "200" ] || [ "$HTTP" = "202" ]; then
      ND_POSTED=1
      break
    fi
    if [ "$HTTP" = "429" ] || [ "\${HTTP:0:1}" = "5" ] || [ "$HTTP" = "000" ]; then
      BACKOFF=$((attempt * 3))
      echo "[done] already-done POST attempt $attempt got HTTP=$HTTP — retrying in \${BACKOFF}s..." >&2
      sleep "$BACKOFF"
      continue
    fi
    echo "[done] already-done POST attempt $attempt got HTTP=$HTTP (permanent failure):" >&2
    cat /tmp/done_resp_${cfg.taskId} >&2
    rm -f /tmp/done_resp_${cfg.taskId}
    exit 1
  done
  rm -f /tmp/done_resp_${cfg.taskId}
  if [ "$ND_POSTED" = "0" ]; then
    echo "[done] already-done POST failed after 5 retries — orchestrator unreachable. Marker NOT written." >&2
    exit 1
  fi
  touch /tmp/done_${cfg.taskId}
  rm -f "$ND_ENVELOPE"
  echo ""
  echo "[done] OK — task COMPLETED with no PR (deliverable already present)."
  echo "[done] reason: $ALREADY_DONE_REASON"
  exit 0
fi

PR_FILE="/tmp/pr_url_${cfg.taskId}"
if [ ! -s "$PR_FILE" ]; then
  echo "Error: $PR_FILE missing or empty — run ./pr open first." >&2
  exit 2
fi
PR_URL=$(cat "$PR_FILE")
# Validate shape — must be a canonical GitHub pull URL.
# Aligned with agent/src/handlers/code-change.ts:274 (same regex).
PR_URL_RE='^https://github\\.com/[^/[:space:]]+/[^/[:space:]]+/pull/[0-9]+$'
if ! printf '%s' "$PR_URL" | grep -qE "$PR_URL_RE"; then
  echo "[done] Error: $PR_FILE contains an invalid PR URL: $PR_URL" >&2
  echo "[done]   Expected: https://github.com/<owner>/<repo>/pull/<number>" >&2
  echo "[done]   ./pr open must produce a canonical pull URL (not /commit/, /compare/, etc.)." >&2
  exit 2
fi

# Idempotency: if marker already exists, this is a re-run — no-op cleanly.
if [ -f /tmp/done_${cfg.taskId} ]; then
  echo "[done] already marked — no-op (PR: $PR_URL)"
  exit 0
fi

# ── Zero-effective-diff guard (defense-in-depth) ──────────────────────────────
# The empty-diff-at-merge incident (PR #14500) merged a code-fix PR whose branch
# netted to ZERO effective diff vs base yet reported COMPLETED via PR. ./pr open
# already refuses to open such a PR, but ./done is the terminal success post, so
# guard it too: if base_commit..HEAD is empty, this branch ships nothing and must
# not report COMPLETED-with-PR. Route to the sanctioned no-op exits instead. Skip
# only when the base commit was never recorded (cannot compute the diff
# faithfully) — the pr-side guard is the primary line of defense there.
_BASE_COMMIT_FILE="/tmp/base_commit_${cfg.taskId}"
if [ -s "$_BASE_COMMIT_FILE" ]; then
  _BASE_COMMIT=$(cat "$_BASE_COMMIT_FILE")
  _EFFECTIVE_DIFF=$(git -C "${WORKSPACE}" diff --name-only "$_BASE_COMMIT..HEAD" 2>/dev/null || true)
  if [ -z "$_EFFECTIVE_DIFF" ]; then
    echo "[done] Branch nets to ZERO effective diff vs base ($_BASE_COMMIT..HEAD is empty)." >&2
    echo "[done] A code-change task with no effective diff must not report COMPLETED as a PR." >&2
    echo "[done] If the deliverable already exists on ${cfg.baseBranch} and no change is needed:" >&2
    echo "[done]   ./done --already-done \\"<reason naming the artifact you verified>\\"" >&2
    echo "[done] If a sibling PR already merged the same changes (branch superseded):" >&2
    echo "[done]   ./pr close --superseded-by <winning-PR-number>   (then ./done)" >&2
    exit 2
  fi
fi

# Optional enrichment: if Claude wrote /tmp/fix_responses_<task_id>.json
# (review-fix dialog-system response), include it in the payload so the
# orchestrator's verify-mode reviewer can chain-of-custody the fix
# rationale forward (review_pipeline.py:1664-1681).
FIX_RESPONSES_FILE="/tmp/fix_responses_${cfg.taskId}.json"
FIX_RESPONSES_JSON=""
if [ -s "$FIX_RESPONSES_FILE" ]; then
  if python3 -c 'import json, sys; json.load(open(sys.argv[1]))' "$FIX_RESPONSES_FILE" >/dev/null 2>&1; then
    FIX_RESPONSES_JSON=$(cat "$FIX_RESPONSES_FILE")
  else
    echo "[done] /tmp/fix_responses_${cfg.taskId}.json exists but is not valid JSON — skipping enrichment" >&2
  fi
fi

# Soft-fallback self-review outcome. Claude should write a structured
# self-review JSON to /tmp/self_review_<task_id>.json before calling
# ./done; we forward it as self_review_outcome on the payload so
# downstream consumers (review_pipeline retry context, admin_digest
# catch-rate metrics, reaper retry pairing) get the signal. If missing
# or invalid, we emit a minimal fallback envelope so the gap is visible
# in metrics but the task doesn't block.
SELF_REVIEW_FILE="/tmp/self_review_${cfg.taskId}.json"
SELF_REVIEW_JSON=""
if [ -s "$SELF_REVIEW_FILE" ]; then
  if python3 -c 'import json, sys; json.load(open(sys.argv[1]))' "$SELF_REVIEW_FILE" >/dev/null 2>&1; then
    SELF_REVIEW_JSON=$(cat "$SELF_REVIEW_FILE")
  else
    echo "[done] /tmp/self_review_${cfg.taskId}.json exists but is not valid JSON — using fallback envelope" >&2
    SELF_REVIEW_JSON='{"verdict":"unknown","check_results":{},"blind_spots":["self-review JSON parsing failed"],"skip_reason":"json_parse_failed"}'
  fi
else
  echo "[done] /tmp/self_review_${cfg.taskId}.json not found — using minimal fallback envelope" >&2
  SKIP_REASON="json_missing"; MARKER="/tmp/self_review_skip_reason_${cfg.taskId}.txt"; [ -s "$MARKER" ] && SKIP_REASON=$(head -c 64 "$MARKER" | tr -dc 'a-z_')
  SELF_REVIEW_JSON='{"verdict":"unknown","check_results":{},"blind_spots":["self-review skipped — investigate"],"skip_reason":"'"$SKIP_REASON"'"}'
fi

# ── Self-review gates (single python3 pass) ────────────────────────────
# One python3 invocation validates both regressing-category reasoning and
# high-stakes evidence anchors. Output: gate:cat:result lines.
REGRESSING_CATS=(${cfg.regressingCategories && cfg.regressingCategories.length > 0 ? cfg.regressingCategories.map((c) => `"${c}"`).join(' ') : ''})
HIGH_STAKES_CATS=("correctness" "error-handling" "security" "regression_risk" "scope-drift")
MISSING_REASONING=()
MISSING_EVIDENCE=()
while IFS=: read -r _GATE _CAT _RESULT; do
  if [ "$_RESULT" = "missing" ]; then
    case "$_GATE" in
      reasoning) MISSING_REASONING+=("$_CAT") ;;
      evidence) MISSING_EVIDENCE+=("$_CAT") ;;
    esac
  fi
done < <(printf '%s' "$SELF_REVIEW_JSON" | python3 -c '
import json, sys, re
d = json.loads(sys.stdin.read())
args = sys.argv[1:]
sep = args.index("--") if "--" in args else len(args)
regressing = args[:sep]
high_stakes = args[sep+1:] if sep < len(args) else []
anchor_re = re.compile(r"[a-zA-Z0-9._/-]+\\.[a-z]+:[0-9]+")
diff_re = re.compile(r"diff.*lines|files|threshold", re.IGNORECASE)
filelist_re = re.compile(r"related_files|modified files|checked.*files?", re.IGNORECASE)
for cat in regressing:
    reasoning = d.get("check_results",{}).get(cat,{}).get("reasoning","")
    if len(reasoning) >= 10 or anchor_re.search(reasoning):
        print(f"reasoning:{cat}:ok")
    else:
        print(f"reasoning:{cat}:missing")
for cat in high_stakes:
    cr = d.get("check_results",{}).get(cat,{})
    passed = str(cr.get("passed", False)).lower() == "true"
    if not passed:
        print(f"evidence:{cat}:ok")
        continue
    reasoning = cr.get("reasoning", "")
    has_anchor = (anchor_re.search(reasoning) or diff_re.search(reasoning) or
                  filelist_re.search(reasoning) or (reasoning and len(reasoning) >= 20))
    print(f"evidence:{cat}:{'ok' if has_anchor else 'missing'}")
' "\${REGRESSING_CATS[@]}" -- "\${HIGH_STAKES_CATS[@]}" 2>/dev/null || true)
if [ "\${#MISSING_REASONING[@]}" -gt 0 ]; then
  echo "[done] Regressing categories need a reasoning field (>=10 chars, or a file:line anchor) in check_results:" >&2
  for _CAT in "\${MISSING_REASONING[@]}"; do echo "  - $_CAT" >&2; done
  echo "Update /tmp/self_review_${cfg.taskId}.json and re-run ./done." >&2
  exit 1
fi
if [ "\${#MISSING_EVIDENCE[@]}" -gt 0 ]; then
  echo "[done] PASS verdicts on high-stakes categories must include evidence anchors (file:line, diff stats, or file list):" >&2
  for _CAT in "\${MISSING_EVIDENCE[@]}"; do echo "  - $_CAT" >&2; done
  echo "Update /tmp/self_review_${cfg.taskId}.json with evidence anchors in reasoning and re-run ./done." >&2
  exit 1
fi

# scope-drift overage gate: if the diff materially exceeds the "large" scope threshold
# (800 lines / 15 files), do not allow scope-drift PASS — the agent must mark it FAIL.
# This catches cases where the diff is unexpectedly huge despite a smaller scope declaration.
_SCOPE_DRIFT_PASSED=$(printf '%s' "$SELF_REVIEW_JSON" | _json_field check_results.scope-drift.passed false 2>/dev/null || echo "false")
if [ "$_SCOPE_DRIFT_PASSED" = "true" ]; then
  _DIFF_STAT=$(git -C "${WORKSPACE}" diff --stat HEAD 2>/dev/null | tail -1 || echo "")
  if [ -n "$_DIFF_STAT" ]; then
    # Parse "X files changed, Y insertions(+), Z deletions(-)" — extract files and total lines
    _FILES=$(printf '%s' "$_DIFF_STAT" | grep -oE '^[[:space:]]*[0-9]+' | xargs)
    _LINES=$(printf '%s' "$_DIFF_STAT" | grep -oE '[0-9]+[[:space:]]*(insertion|deletion)' | grep -oE '[0-9]+' | paste -sd+ | bc 2>/dev/null || echo "0")
    # If diff exceeds large threshold (800 lines, 15 files), block PASS
    if [ "$_FILES" -gt 15 ] || [ "$_LINES" -gt 800 ]; then
      echo "[done] scope-drift cannot be PASS when diff exceeds large threshold (800 lines / 15 files):" >&2
      echo "       Detected: $_LINES lines / $_FILES files" >&2
      echo "       Mark scope-drift as FAIL with the overage details, or revise your scope claim." >&2
      echo "       Update /tmp/self_review_${cfg.taskId}.json and re-run ./done." >&2
      exit 1
    fi
  fi
fi

# Optional enrichment: changed_files from git diff — review-pipeline scope
# analysis, direction credit, rollup management all read this from
# task.context._pr_changed_files. Without it those signals degrade.
CHANGED_FILES_JSON=""
if BASE_COMMIT_FILE="/tmp/base_commit_${cfg.taskId}" && [ -s "$BASE_COMMIT_FILE" ]; then
  BASE_COMMIT=$(cat "$BASE_COMMIT_FILE")
  CHANGED_FILES=$(git -C "${WORKSPACE}" diff --name-only "$BASE_COMMIT..HEAD" 2>/dev/null | head -50 | python3 -c 'import json,sys; lines=[l.rstrip("\\n") for l in sys.stdin.readlines() if l.rstrip("\\n")]; print(json.dumps(lines))')
  if [ -n "$CHANGED_FILES" ] && [ "$CHANGED_FILES" != "[]" ]; then
    CHANGED_FILES_JSON="$CHANGED_FILES"
  fi
fi

CONSUMED_ARTIFACTS_JSON=""
CONSUMED_ARTIFACTS_FILE="/tmp/consumed_artifacts_${cfg.taskId}.json"
if [ -s "$CONSUMED_ARTIFACTS_FILE" ]; then
  if python3 -c 'import json, sys; d=json.load(open(sys.argv[1])); sys.exit(0 if isinstance(d,list) else 1)' "$CONSUMED_ARTIFACTS_FILE" >/dev/null 2>&1; then
    CONSUMED_ARTIFACTS_JSON=$(python3 -c 'import json, sys; d=json.load(open(sys.argv[1])); print(json.dumps(sorted(set(x for x in d if isinstance(x,str) and len(x)>0))))' "$CONSUMED_ARTIFACTS_FILE")
  else
    echo "[done] /tmp/consumed_artifacts_${cfg.taskId}.json exists but is not a JSON array — reporting empty consumed_artifact_ids" >&2
    CONSUMED_ARTIFACTS_JSON="[]"
  fi
fi

# Retry on 429/5xx/network — orchestrator returns 429 on FOR-UPDATE
# contention with Retry-After: 5. Without retry, contended completions
# never reach the orchestrator and the task stays IN_PROGRESS until the
# reaper kicks in minutes later.
# --warn-out-of-scope: post a FEEDBACK project message BEFORE the
# COMPLETED post so the warning is visible to strategic even if the
# COMPLETED post stalls or fails. Best-effort: a message-post failure
# does not block ./done — the warn_out_of_scope_reason field on the
# COMPLETED payload is the durable record.
if [ -n "$WARN_OUT_OF_SCOPE_REASON" ]; then
  WARN_MSG="[shipped via ./done --warn-out-of-scope by task ${cfg.taskId}]
PR: $PR_URL

The agent shipped this diff despite ./check failures, asserting that
the failures are pre-existing on origin/${cfg.baseBranch} and out of
scope for this PR. Reason given:

$WARN_OUT_OF_SCOPE_REASON

Strategic / operators: this is a signal to spawn cleanup work for the
referenced files/gates. The PR itself is intended to merge as-is."
  printf '{"task_id":%s,"op":"create_message","data":{"type":"FEEDBACK","content":%s}}' \\
    "$(_json_str "$TASK")" "$(_json_str "$WARN_MSG")" | \\
    curl -sS --max-time 30 --connect-timeout 10 -X POST "$API/api/agent/strategic" \\
      -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @- \\
    > /tmp/done_warn_msg_${cfg.taskId}.log 2>&1 || \\
    echo "[done] warn-out-of-scope FEEDBACK message post failed (best-effort) — see /tmp/done_warn_msg_${cfg.taskId}.log" >&2
fi

REPRODUCE_VERDICT_STATUS=""
REPRODUCE_VERDICT_REASON=""
if [ -n "$REPRODUCED_REASON" ]; then
  REPRODUCE_VERDICT_STATUS="reproduced"
  REPRODUCE_VERDICT_REASON="$REPRODUCED_REASON"
elif [ -n "$INDETERMINATE_REASON" ]; then
  REPRODUCE_VERDICT_STATUS="indeterminate"
  REPRODUCE_VERDICT_REASON="$INDETERMINATE_REASON"
fi

PAYLOAD=$(python3 -c '
import json, sys
tid = sys.argv[1]
pr = sys.argv[2]
fix_resp_raw = sys.argv[3]
self_review_raw = sys.argv[4]
changed_raw = sys.argv[5]
consumed_raw = sys.argv[6]
warn = sys.argv[7]
repro_status = sys.argv[8]
repro_reason = sys.argv[9]

payload = {
    "task_id": tid,
    "status": "COMPLETED",
    "artifact_type": "PULL_REQUEST",
    "pr_url": pr,
    "logs": "Task complete. PR: " + pr,
    "self_review_outcome": json.loads(self_review_raw),
}

if fix_resp_raw != "null":
    payload["fix_agent_responses"] = json.loads(fix_resp_raw)
if changed_raw != "null":
    payload["changed_files"] = json.loads(changed_raw)
if consumed_raw != "null":
    payload["consumed_artifact_ids"] = json.loads(consumed_raw)
if warn:
    payload["warn_out_of_scope_reason"] = warn
if repro_status:
    payload["reproduce_verdict"] = {
        "status": repro_status,
        "reason": repro_reason,
    }

print(json.dumps(payload))
' "$TASK" "$PR_URL" "\${FIX_RESPONSES_JSON:-null}" "$SELF_REVIEW_JSON" "\${CHANGED_FILES_JSON:-null}" "\${CONSUMED_ARTIFACTS_JSON:-null}" "\${WARN_OUT_OF_SCOPE_REASON:-}" "\${REPRODUCE_VERDICT_STATUS:-}" "\${REPRODUCE_VERDICT_REASON:-}")

# Stale-result envelope: persist BEFORE attempting the POST so that if
# the VM is killed mid-retry, the next agent boot can scan for the
# envelope and replay the COMPLETED post. Mirrors the TS-side behaviour
# in persistAndPostStatus (index.ts:567). Without this, a kill during
# the bash 45s retry window leaves the task IN_PROGRESS until the reaper
# sweeps it as orphan, then re-runs from scratch — duplicate work + retry
# budget burn.
ENVELOPE="/tmp/task_${cfg.taskId}_result.json"
printf '%s' "$PAYLOAD" > "$ENVELOPE" 2>/dev/null || \
  echo "[done] WARNING: failed to persist stale-result envelope to $ENVELOPE" >&2

POSTED=0
for attempt in 1 2 3 4 5; do
  HTTP=$(printf '%s' "$PAYLOAD" | curl -sS -o /tmp/done_resp_${cfg.taskId} -w '%{http_code}' --max-time 30 \\
    -X POST "$API/api/agent/status" \\
    -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-) || HTTP="000"
  if [ "$HTTP" = "200" ] || [ "$HTTP" = "202" ]; then
    POSTED=1
    break
  fi
  if [ "$HTTP" = "429" ] || [ "\${HTTP:0:1}" = "5" ] || [ "$HTTP" = "000" ]; then
    BACKOFF=$((attempt * 3))
    echo "[done] POST attempt $attempt got HTTP=$HTTP — retrying in \${BACKOFF}s..." >&2
    sleep "$BACKOFF"
    continue
  fi
  # 4xx other than 429: don't retry — likely a permanent error.
  echo "[done] POST attempt $attempt got HTTP=$HTTP (permanent failure):" >&2
  cat /tmp/done_resp_${cfg.taskId} >&2
  rm -f /tmp/done_resp_${cfg.taskId}
  exit 1
done
rm -f /tmp/done_resp_${cfg.taskId}
if [ "$POSTED" = "0" ]; then
  echo "[done] POST failed after 5 retries — orchestrator unreachable. Marker NOT written; wrapper will fall back to its own status post." >&2
  exit 1
fi

# Marker is written ONLY after a successful POST. The wrapper inspects it
# to decide whether to skip its fallback completion path.
touch /tmp/done_${cfg.taskId}
# Envelope replayed successfully — remove it so the next agent boot doesn't
# resend a duplicate COMPLETED post for this task ID.
rm -f "$ENVELOPE"
echo ""
echo "[done] OK — wrapper will exit cleanly. PR: $PR_URL"
`;
  fs.writeFileSync(`${WORKSPACE}/done`, script, { mode: 0o755 });
}
