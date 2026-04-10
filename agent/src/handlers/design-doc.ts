import fs from 'fs';
import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sendLog, validateBranchName,
  apiPost, cloneWithRetry, runClaudeCode,
} from '../index.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import {
  readClaudeMd, buildContextPrefix, buildFileTree, configureAgentPermissions,
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

  // Write the design-doc helper script that the inner agent can call via bash
  const orchestratorUrl = process.env.ORCHESTRATOR_URL || 'http://localhost:8080';
  // Read the current token from apiHeaders (not env — token may have been refreshed)
  const { getAgentToken } = await import('../index.js');
  const agentToken = getAgentToken();
  const helperScript = `#!/bin/bash
# Design doc wiki tool — updates pages incrementally via API
# Usage:
#   ./design-doc set <key> '<json-value>'
#   ./design-doc delete <key>
#   ./design-doc list

OP="\${1:?Usage: design-doc set|delete|list <key> [json-value]}"
KEY="\$2"
VALUE="\$3"

if [ "$OP" = "list" ]; then
  curl -f -X POST "${orchestratorUrl}/api/agent/design-doc" \\
    -H "Content-Type: application/json" \\
    -H "X-Agent-Token: ${agentToken}" \\
    -d "{\\"task_id\\": \\"${taskId}\\", \\"op\\": \\"list\\"}"
elif [ "$OP" = "set" ]; then
  if [ -z "$KEY" ] || [ -z "$VALUE" ]; then
    echo "Error: set requires <key> and <json-value>" >&2; exit 1
  fi
  curl -f -X POST "${orchestratorUrl}/api/agent/design-doc" \\
    -H "Content-Type: application/json" \\
    -H "X-Agent-Token: ${agentToken}" \\
    -d "{\\"task_id\\": \\"${taskId}\\", \\"op\\": \\"set\\", \\"key\\": \\"$KEY\\", \\"value\\": $VALUE}"
elif [ "$OP" = "delete" ]; then
  if [ -z "$KEY" ]; then
    echo "Error: delete requires <key>" >&2; exit 1
  fi
  curl -f -X POST "${orchestratorUrl}/api/agent/design-doc" \\
    -H "Content-Type: application/json" \\
    -H "X-Agent-Token: ${agentToken}" \\
    -d "{\\"task_id\\": \\"${taskId}\\", \\"op\\": \\"delete\\", \\"key\\": \\"$KEY\\"}"
else
  echo "Unknown op: $OP. Use set, delete, or list." >&2; exit 1
fi
echo ""
`;
  fs.writeFileSync(`${WORKSPACE}/design-doc`, helperScript, { mode: 0o755 });

  const prompt = `${contextPrefix}${codebaseSummary}${fileTreeSection}

# Project
${projectName}

You are an architecture analyst. Your job is to analyze this codebase and build a
design document describing what exists. Do NOT create files in the repository,
do NOT open pull requests, do NOT make any code changes. This is a read-only analysis.

# Current design document pages
${existingPagesText}
${visionSection}
# How to update the design document

Use the \`./design-doc\` command to save pages as you discover things. Each call
saves immediately to the database. Update pages AS YOU EXPLORE — don't wait until the end.

\`\`\`bash
# Set a page (key = page name, value = any JSON)
./design-doc set "summary" '"Brief project description here"'
./design-doc set "architecture" '{"type": "monolith", "description": "FastAPI + React + Node agent", "key_decisions": ["JSONB for flexible schemas", "Event-driven task dispatch"]}'
./design-doc set "systems/sys-auth" '{"id": "sys-auth", "name": "Authentication", "status": "functional", "description": "Firebase auth with JWT", "modules": ["backend/app/auth.py"]}'
./design-doc set "systems/sys-pipeline" '{"id": "sys-pipeline", "name": "Task Pipeline", "status": "mature", "description": "Orchestrator loop dispatching to agent VMs", "modules": ["backend/app/main.py"]}'
./design-doc set "capabilities/testing" '{"status": "partial", "notes": "Unit tests exist, no integration tests"}'
./design-doc set "capabilities/monitoring" '{"status": "missing"}'
./design-doc set "risks" '["Single monolithic main.py (7000+ lines)", "No rate limiting on public API"]'

# Delete a page
./design-doc delete "systems/sys-old-removed"

# List all page keys
./design-doc list
\`\`\`

Use prefixes to organize: \`systems/*\`, \`capabilities/*\`, etc. You can create
any pages you want — if you discover something that deserves its own page (like
a dependency graph or data model inventory), create it.

# Instructions

1. Explore the codebase THOROUGHLY — use subagents for parallel exploration:
   - Spawn multiple subagents to explore different directories simultaneously
   - One for backend, one for frontend, one for agent, one for infra/CI
   - Read entry points, configs, module boundaries, key source files
   - Understand how components connect, not individual function details
   - Use TodoWrite to track what you've explored and what's left
   - This should be a deep analysis, not a quick skim

2. Save pages as you go. For each major system you discover:
   - \`./design-doc set "systems/<id>" '{"id": "...", "name": "...", "status": "mature|functional|partial|stub|missing", "description": "...", "modules": [...]}'
   - Status: mature (battle-tested), functional (works), partial (incomplete), stub (scaffolded), missing (needed but absent)

3. For missing or planned systems (in the vision but absent from code), include a spec:
   \`./design-doc set "systems/<id>" '{"id": "...", "name": "...", "status": "missing", "spec": {"approach": "...", "technology": [...], "integration_points": [...]}}'

4. Document capabilities (cross-cutting concerns):
   \`./design-doc set "capabilities/<name>" '{"status": "...", "notes": "..."}'

5. Document risks:
   \`./design-doc set "risks" '[...]'

6. Keep system IDs stable across revisions (e.g., sys-auth, sys-pipeline, sys-frontend).

7. Focus on WHAT IS, not what should be. Document missing things as missing.
   Only write specs for systems that are missing/stub AND appear in the vision.

Do NOT create any files in the repo. Do NOT commit or push. Only use ./design-doc to save findings.
${openMessagesText ? `\n# Open Messages\n${openMessagesText}\n` : ''}`;

  statusRef.value = 'Running architecture review...';
  const { exitCode } = await runClaudeCode(prompt, ws, statusRef, model, effort, disallowedTools);

  // Completion — design doc was updated incrementally via API.
  // Just report status; the backend handler chains to strategic review.
  await apiPost('/api/agent/status', {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    logs: exitCode === 0 ? 'Architecture review completed. Design doc updated via API.' : 'Architecture review failed.',
  });
}
