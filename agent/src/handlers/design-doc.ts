import fs from 'fs';
import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sendLog, validateBranchName,
  persistAndPostStatus, apiGet, cloneWithRetry, runClaudeCode,
} from '../index.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import {
  readClaudeMd, buildContextPrefix, buildFileTree, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, excludeAgentHelpers,
} from '../prompt.js';

// ── Handler: Design Doc Update (Phase 1 of strategic cycle) ──
// Analyzes codebase architecture and incrementally updates the design doc
// via API calls. No JSON dump — each page is saved as the agent discovers it.
export async function handleDesignDocUpdate(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = strVal(payload.base_branch, 'main');
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  statusRef.value = `Cloning ${owner}/${repo} for architecture review...`;
  sendLog(ws, `Starting design document update...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await cloneWithRetry(['clone', '--depth', String(DEFAULT_CLONE_DEPTH), '--branch', baseBranch, cloneUrl, WORKSPACE]);
  writePlaybookHelper(taskId);

  const disallowedTools = configureAgentPermissions('design_doc_update');

  const contextPrefix = buildContextPrefix(task);
  const claudeMd = readClaudeMd();
  const codebaseSummary = claudeMd
    ? `## Codebase Instructions\n${claudeMd}\n\n`
    : '';
  const fileTreeSection = buildFileTree(ws);
  const projectName = strVal(payload.project_name, 'Unknown Project');

  // Build existing pages summary from current design doc
  const designDoc = payload.design_doc as Record<string, unknown> | undefined;
  let existingPagesText = 'No pages exist yet. Build the design document from scratch.';
  if (designDoc && Object.keys(designDoc).length > 0) {
    const pages = Object.entries(designDoc)
      .filter(([k]) => !k.startsWith('_'))
      .map(([k, v]) => {
        const preview = JSON.stringify(v).slice(0, 200);
        return `  ${k}: ${preview}${preview.length >= 200 ? '...' : ''}`;
      });
    if (pages.length > 0) {
      existingPagesText = `${pages.length} pages exist:\n${pages.join('\n')}`;
    }
  }

  // Vision context
  const vision = payload.vision as Record<string, unknown> | undefined;
  const visionSection = vision && Object.keys(vision).length > 0
    ? `\n# Product vision (what SHOULD exist)\n${JSON.stringify(vision, null, 2)}\n`
    : '';

  // Open messages
  const openMessages = Array.isArray(payload.open_messages)
    ? payload.open_messages as Array<Record<string, unknown>>
    : [];
  const openMessagesText = openMessages.length > 0
    ? openMessages.map(m => {
        const age = m.created_at ? (() => {
          const ms = Date.now() - new Date(String(m.created_at)).getTime();
          const h = Math.floor(ms / 3600000);
          return h < 24 ? `${h}h ago` : `${Math.floor(h / 24)}d ago`;
        })() : '?';
        return `- [${strVal(m.type as string)}] (id: ${strVal(m.id as string)}, from: ${strVal(m.from as string)}, open ${age}) ${strVal(m.content as string).slice(0, 200)}`;
      }).join('\n')
    : '';

  // Write the design-doc helper script that the inner agent can call via bash.
  // The script reads ORCHESTRATOR_URL and AGENT_API_TOKEN from env (passed by runClaudeCode)
  // so the token is never written to disk.
  // Uses printf + pipe to avoid shell expansion of JSON values in curl -d.
  const helperScript = `#!/bin/bash
# Design doc wiki tool — updates pages incrementally via API
# Reads ORCHESTRATOR_URL and AGENT_API_TOKEN from env (no secrets on disk)
# Usage:
#   ./design-doc get <key>                — read a section
#   ./design-doc set <key> '<json-value>' — write a section
#   ./design-doc delete <key>
#   ./design-doc list                     — list all section keys

OP="\${1:?Usage: design-doc get|set|delete|list <key> [json-value]}"
KEY="\$2"
VALUE="\$3"
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${taskId}"
CURL_OPTS="-f -sS --max-time 30 --connect-timeout 10"

if [ "\$OP" = "list" ]; then
  printf '{"task_id":"%s","op":"list"}' "\$TASK" | \\
    curl \$CURL_OPTS -X POST "\$API/api/agent/design-doc" \\
      -H "Content-Type: application/json" \\
      -H "X-Agent-Token: \$TOKEN" \\
      --data-binary @-
elif [ "\$OP" = "get" ]; then
  if [ -z "\$KEY" ]; then
    echo "Error: get requires <key>" >&2; exit 1
  fi
  printf '{"task_id":"%s","op":"get","key":"%s"}' "\$TASK" "\$KEY" | \\
    curl \$CURL_OPTS -X POST "\$API/api/agent/design-doc" \\
      -H "Content-Type: application/json" \\
      -H "X-Agent-Token: \$TOKEN" \\
      --data-binary @-
elif [ "\$OP" = "set" ]; then
  if [ -z "\$KEY" ] || [ -z "\$VALUE" ]; then
    echo "Error: set requires <key> and <json-value>" >&2; exit 1
  fi
  printf '{"task_id":"%s","op":"set","key":"%s","value":%s}' "\$TASK" "\$KEY" "\$VALUE" | \\
    curl \$CURL_OPTS -X POST "\$API/api/agent/design-doc" \\
      -H "Content-Type: application/json" \\
      -H "X-Agent-Token: \$TOKEN" \\
      --data-binary @-
elif [ "\$OP" = "delete" ]; then
  if [ -z "\$KEY" ]; then
    echo "Error: delete requires <key>" >&2; exit 1
  fi
  printf '{"task_id":"%s","op":"delete","key":"%s"}' "\$TASK" "\$KEY" | \\
    curl \$CURL_OPTS -X POST "\$API/api/agent/design-doc" \\
      -H "Content-Type: application/json" \\
      -H "X-Agent-Token: \$TOKEN" \\
      --data-binary @-
else
  echo "Unknown op: \$OP. Use get, set, delete, or list." >&2; exit 1
fi
echo ""
`;
  fs.writeFileSync(`${WORKSPACE}/design-doc`, helperScript, { mode: 0o755 });
  excludeAgentHelpers();
  writeMessagesHelper(taskId);

  // CLAUDE.md as persistent reminder
  fs.writeFileSync(`${WORKSPACE}/CLAUDE.md`,
    `# Design Doc Agent\nCRITICAL: Your ONLY output is via \`./design-doc set\` commands.\nAny text you write will be DISCARDED. Only ./design-doc calls persist.\nBefore finishing, confirm you called ./design-doc set at least once.\n`
  );

  const prompt = `${contextPrefix}# YOUR ONLY OUTPUT MECHANISM

Your text output will be DISCARDED. The ONLY way to save your work is the \`./design-doc\` command:

\`\`\`bash
./design-doc set "summary" '"Brief project description"'
./design-doc set "architecture" '{"type": "monolith", "description": "...", "key_decisions": [...]}'
./design-doc set "systems/sys-auth" '{"id": "sys-auth", "name": "Auth", "status": "functional", "description": "...", "modules": ["backend/app/auth.py"]}'
./design-doc set "capabilities/testing" '{"status": "partial", "notes": "Unit tests only"}'
./design-doc set "risks" '["Risk 1", "Risk 2"]'
./design-doc set "notes/observations" '"Any notes for future reference"'
./design-doc delete "systems/sys-removed"
./design-doc list
\`\`\`

Call \`./design-doc set\` AS YOU EXPLORE. Each call saves immediately. Do NOT wait until the end.
Do NOT create files in the repo. Do NOT commit or push. Only use ./design-doc.

# Context

## Project: ${projectName}

## Current pages
${existingPagesText}
${visionSection}
${openMessagesText ? `## Open Messages\n${openMessagesText}\n` : ''}
## Codebase
${codebaseSummary}${fileTreeSection}

# What to do

1. Explore the codebase THOROUGHLY — use subagents for parallel exploration.
2. For each system you find: \`./design-doc set "systems/<id>" '{...}'\`
   Status: mature | functional | partial | stub | missing
3. For capabilities: \`./design-doc set "capabilities/<name>" '{...}'\`
4. For risks: \`./design-doc set "risks" '[...]'\`
5. For notes/observations: \`./design-doc set "notes/<topic>" '"..."'\`
6. Use stable system IDs (sys-auth, sys-pipeline, sys-frontend, etc.)
7. Focus on WHAT IS. Document missing things as missing.
8. Review open messages — respond to user questions/feedback and resolve addressed messages:
   \`./messages resolve <message-id> 'Brief explanation of how it was addressed'\`
   \`./messages send 'Your response or note' QUESTION\`
   Incorporate valid observations into the relevant pages. Resolve messages you've addressed.
9. Review playbook entries (run: ./playbook get) — remove outdated entries, merge duplicates,
   and validate that entries reflect the current codebase state. Use ./playbook delete <key>
   for stale entries.`;

  statusRef.value = 'Running architecture review...';
  let { exitCode } = await runClaudeCode(prompt, ws, statusRef, model, effort, disallowedTools);

  // Post-check: verify the agent actually called ./design-doc
  try {
    const projectData = await apiGet(`/api/admin/projects`) as Array<Record<string, unknown>>;
    const proj = projectData.find(p => p.name === projectName);
    const doc = (proj?.design_doc || {}) as Record<string, unknown>;
    const pageCount = Object.keys(doc).filter(k => !k.startsWith('_')).length;
    if (exitCode === 0 && pageCount === 0) {
      sendLog(ws, '\n[WARNING] Architecture review completed but no ./design-doc calls were made. Retrying...\n');
      const retryResult = await runClaudeCode(
        'You completed your analysis but did NOT call ./design-doc to save any pages. Your text was discarded.\n\n' +
        'You MUST call ./design-doc set now to save your findings.\n' +
        'The ./design-doc command is a bash script in the current directory. Run it via Bash.\n' +
        'Start with: ./design-doc set "summary" \'"Your project summary"\'\n' +
        'Then save systems, capabilities, and risks.',
        ws, statusRef, model, effort, disallowedTools
      );
      exitCode = retryResult.exitCode;
    }
  } catch (e) {
    sendLog(ws, `[WARNING] Could not verify design doc API calls: ${e}\n`);
  }

  // Completion — design doc was updated incrementally via API.
  // Just report status; the backend handler chains to strategic review.
  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    logs: exitCode === 0 ? 'Architecture review completed. Design doc updated via API.' : 'Architecture review failed.',
    ...(exitCode !== 0 ? { failure_category: 'claude_code_failed' } : {}),
  });
}
