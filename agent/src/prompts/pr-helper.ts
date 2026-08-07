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

export function writePrHelper(cfg: CodeChangeHelperConfig): void {
  validateCfgShellSafety(cfg);
  // Idempotent push + PR creation/update. Runs a bounded ./check gate first:
  // deterministic failures block; timeout/tool unavailability is receipted.
  // Writes /tmp/pr_url_<task_id> on success so the wrapper + ./done can
  // pick up the URL without re-querying GitHub.
  const _SR_SECTION_SCRIPT_B64 = loadSrSectionScriptB64();
  const _SCREENSHOTS_SECTION_SCRIPT_B64 = loadScreenshotsSectionScriptB64();
  const agentTokenFn = agentTokenShellFn();
  const githubTokenFn = githubTokenShellFn();
  const jsonStrFn = jsonStrShellFn();
  const jsonFieldFn = jsonFieldShellFn();
  const script = `#!/bin/bash
# ./pr open --title "..." --body "..."  — ship the diff via PR
#
# Gates on bounded ./check. Deterministic failures block; timeout/tool
# unavailability requires authoritative CI. Then commits and pushes
# to ${cfg.workBranch}, and creates a PR (or pushes an update to an
# existing PR for review-fix tasks).
#
# Exits 0 on success (PR URL written to /tmp/pr_url_${cfg.taskId}).
# Non-zero on any failure — error tail printed; fix and retry.

set -uo pipefail
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
  echo "[pr] PR_GATE_TIMEOUT_SECONDS=$GATE_TIMEOUT_SECONDS exceeds the 900s safety ceiling; capping it." >&2
  GATE_TIMEOUT_SECONDS=900
fi
GATE_RECEIPT_FILE="/tmp/pr_gate_receipt_${cfg.taskId}.json"
GATE_SECTION=""
GATE_COMMAND="./check"

write_gate_receipt() {
  local status="$1"
  local exit_code="$2"
  local fallback="$3"
  local recorded_at
  recorded_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '{"status":"%s","command":"%s","timeout_seconds":%s,"exit_code":%s,"authoritative_ci_required":%s,"recorded_at":"%s"}\n' \
    "$status" "$GATE_COMMAND" "$GATE_TIMEOUT_SECONDS" "$exit_code" "$fallback" "$recorded_at" > "$GATE_RECEIPT_FILE"
  GATE_SECTION=$(printf '%s\n%s\n%s\n%s\n%s\n%s\n%s' \
    '<!-- blueprint:local-gate -->' \
    '### Local publication gate' \
    "- Status: $status" \
    "- Command: $GATE_COMMAND" \
    "- Deadline: \${GATE_TIMEOUT_SECONDS}s (hard ceiling 900s)" \
    "- Exit code: $exit_code; authoritative CI required: $fallback" \
    '<!-- /blueprint:local-gate -->')
}

run_pr_gate() {
  GATE_COMMAND="./check"
  [ "$#" -gt 0 ] && GATE_COMMAND="./check $*"
  if ! command -v timeout >/dev/null 2>&1; then
    # timeout not installed: run ./check directly without a deadline.
    # A missing verdict (check never ran) must not be treated as a pass;
    # running the check unbounded ensures it can produce an explicit exit code.
    echo "[pr] WARNING: timeout not available — running $GATE_COMMAND without deadline" >&2
    ./check "$@"
    GATE_STATUS=$?
  else
    echo "[pr] Running bounded local gate: $GATE_COMMAND (deadline \${GATE_TIMEOUT_SECONDS}s)"
    timeout --signal=TERM --kill-after=10s "\${GATE_TIMEOUT_SECONDS}s" ./check "$@"
    GATE_STATUS=$?
  fi
  case "$GATE_STATUS" in
    0)
      write_gate_receipt passed "$GATE_STATUS" false
      return 0
      ;;
    124|137)
      write_gate_receipt timed_out "$GATE_STATUS" false
      echo "[pr] $GATE_COMMAND exceeded its bounded \${GATE_TIMEOUT_SECONDS}s local deadline and was terminated." >&2
      echo "[pr] ./check did not complete within the time limit — no verdict was produced." >&2
      echo "[pr] Increase PR_GATE_TIMEOUT_SECONDS, fix the underlying slowness, or resolve" >&2
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
    echo ""
    echo "[pr] ./check failed — fix lint/build then re-run ./pr open."
    exit 1
  fi
fi
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

# 4. Push (with retry — concurrent-push handling)
PUSH_OK=0
for attempt in 1 2 3; do
  if git push --force-with-lease origin "${cfg.workBranch}" 2>/tmp/pr_push.log; then
    PUSH_OK=1
    break
  fi
  if classify_push_error "$(cat /tmp/pr_push.log)"; then
    exit 1
  fi
  if grep -q 'stale info' /tmp/pr_push.log; then
    echo "[pr] push attempt $attempt: stale lease, fetching + retrying..."
    git fetch origin "${cfg.workBranch}" 2>/dev/null || true
    sleep 2
  else
    echo "[pr] push attempt $attempt failed:"; cat /tmp/pr_push.log; sleep 2
  fi
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
