import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { WORKSPACE } from '../index.js';
import { validateCfgShellSafety, type CodeChangeHelperConfig } from './code-change-helpers.js';

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
  // Idempotent push + PR creation/update. Gates on ./check first.
  // Writes /tmp/pr_url_<task_id> on success so the wrapper + ./done can
  // pick up the URL without re-querying GitHub.
  const _SR_SECTION_SCRIPT_B64 = loadSrSectionScriptB64();
  const _SCREENSHOTS_SECTION_SCRIPT_B64 = loadScreenshotsSectionScriptB64();
  const script = `#!/bin/bash
# ./pr open --title "..." --body "..."  — ship the diff via PR
#
# Gates on ./check (must pass). Then commits any pending changes, pushes
# to ${cfg.workBranch}, and creates a PR (or pushes an update to an
# existing PR for review-fix tasks).
#
# Exits 0 on success (PR URL written to /tmp/pr_url_${cfg.taskId}).
# Non-zero on any failure — error tail printed; fix and retry.

set -uo pipefail
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
  TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
  TASK="${cfg.taskId}"

  # Refresh GitHub token
  TOKEN_RESP=$(curl -f -sS --max-time 30 \\
    -H "X-Agent-Token: $TOKEN" \\
    "$API/api/agent/github/token?task_id=$TASK") || {
    echo "[pr close] Failed to fetch GitHub token from orchestrator" >&2
    exit 1
  }
  GH_TOKEN=$(printf '%s' "$TOKEN_RESP" | jq -r '.data.token // empty')
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
    COMMENT_BODY=$(jq -n --arg msg "Closing: equivalent changes already merged via PR #$SUPERSEDED_BY. No further action needed on this branch." '{body: $msg}')
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

${(cfg.promotionTarget !== undefined ? cfg.baseBranch === cfg.promotionTarget : cfg.testsOnMainBranch) ? `# Mandatory test gate: force WITH_TESTS=1 when the diff touches backend/.
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
# 1. Run ./check as gate (lint always; tests on main branch when --with-tests
#    OR on review-fix path that touches backend changes).
# Branch on WITH_TESTS instead of expanding an array. Older bash + set -u
# aborts on an empty array expansion, so just call ./check directly.
if [ "$WITH_TESTS" = "1" ]; then
  if ! ./check test; then
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
  if [ -s /tmp/check_gate_env_${cfg.taskId}.txt ]; then
    echo ""
    echo "[pr] NOTE: the test gate ran in GATE-ENVIRONMENT mode on this VM:"
    sed 's/^/[pr]   /' /tmp/check_gate_env_${cfg.taskId}.txt
    echo "[pr] Record your faithful test evidence (sanctioned harness run) in the PR body; CI runs the authoritative suite."
  fi
else
  if ! ./check; then
    echo ""
    echo "[pr] ./check failed — fix lint/build then re-run ./pr open."
    exit 1
  fi
fi

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

API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${cfg.taskId}"

# 3. Refresh GitHub token + remote URL right before push. The clone-time token
#    is per-task ephemeral and may be near expiry by the time Claude ships.
TOKEN_RESP=$(curl -f -sS --max-time 30 \\
  -H "X-Agent-Token: $TOKEN" \\
  "$API/api/agent/github/token?task_id=$TASK") || {
  echo "[pr] Failed to fetch fresh GitHub token from orchestrator" >&2
  exit 1
}
GH_PUSH_TOKEN=$(printf '%s' "$TOKEN_RESP" | jq -r '.data.token // empty')
if [ -z "$GH_PUSH_TOKEN" ]; then
  echo "[pr] orchestrator returned no token: $TOKEN_RESP" >&2
  exit 1
fi
git remote set-url origin "https://x-access-token:$GH_PUSH_TOKEN@github.com/${cfg.owner}/${cfg.repo}.git"
unset GH_PUSH_TOKEN  # only needed for the remote URL above

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
# Update existing PR body with fresh SR + screenshots sections (best-effort).
if [ -n "$SR_SECTION" ] || [ -n "$SHOTS_SECTION" ]; then
  _PTRESP=$(curl -f -sS --max-time 30 \\
    -H "X-Agent-Token: $TOKEN" \\
    "$API/api/agent/github/token?task_id=$TASK" 2>/dev/null) || _PTRESP=""
  _PTOKEN=$(printf '%s' "$_PTRESP" | jq -r '.data.token // empty' 2>/dev/null || true)
  if [ -n "$_PTOKEN" ]; then
    # Fetch current PR body. Use -f so non-2xx GET responses (auth/rate-limit/
    # 5xx/404) fail fast rather than yielding an empty body that would let the
    # subsequent PATCH wipe the live PR description with just the section(s).
    _GETBODY_FILE=/tmp/pr_get_body_${cfg.taskId}.json
    if curl -f -sS --max-time 30 \\
        "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/pulls/${cfg.existingPrNumber}" \\
        -H "Authorization: token $_PTOKEN" -H "Accept: application/vnd.github+json" \\
        -o "$_GETBODY_FILE" 2>/dev/null; then
      _CURBODY=$(jq -r '.body // ""' "$_GETBODY_FILE" 2>/dev/null || printf '')
      # Strip any prior blocks (idempotent), then re-append the fresh non-empty ones.
      _UPDBODY=$(printf '%s' "$_CURBODY" | sed '/<!-- blueprint:self-review -->/,/<!-- \\/blueprint:self-review -->/d')
      _UPDBODY=$(printf '%s' "$_UPDBODY" | sed '/<!-- blueprint:screenshots -->/,/<!-- \\/blueprint:screenshots -->/d')
      if [ -n "$SR_SECTION" ]; then
        _UPDBODY=$(printf '%s\\n\\n%s' "$_UPDBODY" "$SR_SECTION")
      fi
      if [ -n "$SHOTS_SECTION" ]; then
        _UPDBODY=$(printf '%s\\n\\n%s' "$_UPDBODY" "$SHOTS_SECTION")
      fi
      _PHTTP=$(jq -n --arg b "$_UPDBODY" '{body: $b}' | curl -sS -o /dev/null -w '%{http_code}' \\
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
PR_PAYLOAD=$(jq -n --arg tid "$TASK" --arg owner "${cfg.owner}" --arg repo "${cfg.repo}" \\
  --arg title "$TITLE" --arg head "${cfg.workBranch}" --arg base "${cfg.baseBranch}" --arg body "$BODY" \\
  '{task_id: $tid, owner: $owner, repo: $repo, title: $title, head: $head, base: $base, body: $body}')
PR_RESP=""
PR_OK=0
for attempt in 1 2 3 4 5; do
  PR_HTTP=$(printf '%s' "$PR_PAYLOAD" | curl -sS -o /tmp/pr_create_resp_${cfg.taskId} -w '%{http_code}' --max-time 90 \\
    -X POST "$API/api/agent/github/pull-requests" \\
    -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-) || PR_HTTP="000"
  if [ "$PR_HTTP" = "200" ] || [ "$PR_HTTP" = "201" ] || [ "$PR_HTTP" = "202" ]; then
    PR_RESP=$(cat /tmp/pr_create_resp_${cfg.taskId})
    PR_OK=1; break
  fi
  if [ "$PR_HTTP" = "429" ] || [ "\${PR_HTTP:0:1}" = "5" ] || [ "$PR_HTTP" = "000" ]; then
    BACKOFF=$((attempt * 3))
    echo "[pr] PR creation attempt $attempt got HTTP=$PR_HTTP — retrying in \${BACKOFF}s..." >&2
    sleep "$BACKOFF"; continue
  fi
  echo "[pr] PR creation attempt $attempt got HTTP=$PR_HTTP (permanent failure):" >&2
  cat /tmp/pr_create_resp_${cfg.taskId} >&2
  rm -f /tmp/pr_create_resp_${cfg.taskId}
  exit 1
done
rm -f /tmp/pr_create_resp_${cfg.taskId}
if [ "$PR_OK" = "0" ]; then
  echo "[pr] PR creation failed after 5 retries — orchestrator unreachable" >&2
  exit 1
fi
PR_URL=$(printf '%s' "$PR_RESP" | jq -r '.data.html_url // empty')
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
#   blocked / unstable → CI gating; auto-merge handles it
#   behind             → out-of-date with base; GitHub or auto-rebase fixes
#   dirty              → real merge conflict; needs rebase + manual resolve
#   draft              → marked draft; no action
#
# Only \`dirty\` triggers the warning + exit 4. Everything else exits 0.
PR_NUMBER=$(printf '%s' "$PR_URL" | grep -oE '[0-9]+$' || true)
if [ -n "$PR_NUMBER" ]; then
  TOKEN_RESP=$(curl -f -sS --max-time 30 \\
    -H "X-Agent-Token: $TOKEN" \\
    "$API/api/agent/github/token?task_id=$TASK" 2>/dev/null) || TOKEN_RESP=""
  GH_TOKEN_PR=$(printf '%s' "$TOKEN_RESP" | jq -r '.data.token // empty' 2>/dev/null || true)
  if [ -n "$GH_TOKEN_PR" ]; then
    PR_MERGEABLE="null"
    PR_STATE="unknown"
    for pr_attempt in 1 2 3 4 5; do
      _PR_DETAIL=$(curl -sS --max-time 20 \\
        -H "Authorization: token $GH_TOKEN_PR" \\
        -H "Accept: application/vnd.github+json" \\
        "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/pulls/$PR_NUMBER" 2>/dev/null || echo "")
      PR_MERGEABLE=$(printf '%s' "$_PR_DETAIL" | jq -r '.mergeable // "null"' 2>/dev/null || echo "null")
      PR_STATE=$(printf '%s' "$_PR_DETAIL" | jq -r '.mergeable_state // "unknown"' 2>/dev/null || echo "unknown")
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
      echo "[pr] note: PR #$PR_NUMBER is behind origin/${cfg.baseBranch}; auto-merge will rebase when CI clears"
    fi
  fi
fi

echo ""
echo "[pr] OK — $PR_URL"
echo "[pr] Next: ./done   (or call ./pr again if you push more commits)"
`;
  fs.writeFileSync(`${WORKSPACE}/pr`, script, { mode: 0o755 });
}
