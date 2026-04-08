import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName, readHandlerOutput,
  apiPost, cloneWithRetry, runClaudeCode,
} from '../index.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import {
  readClaudeMd, buildContextPrefix, buildFileTree,
} from '../prompt.js';

// ── Handler: Design Doc Update (Phase 1 of strategic cycle) ──
// Analyzes codebase architecture and maintains a living design document.
// This agent SHOULD grind through code — its job is to produce architecture-level output.
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

  const contextPrefix = buildContextPrefix(task);
  const claudeMd = readClaudeMd();
  const codebaseSummary = claudeMd
    ? `## Codebase Instructions\n${claudeMd}\n\n`
    : '';
  const fileTreeSection = buildFileTree(ws);

  const designDoc = payload.design_doc as Record<string, unknown> | undefined;
  const proposedChanges = Array.isArray(payload.proposed_changes)
    ? payload.proposed_changes as Array<Record<string, unknown>>
    : [];
  const projectName = strVal(payload.project_name, 'Unknown Project');

  const existingDocJson = designDoc && Object.keys(designDoc).length > 0
    ? JSON.stringify(designDoc, null, 2)
    : '';

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

  const vision = payload.vision as Record<string, unknown> | undefined;
  const visionJson = vision && Object.keys(vision).length > 0
    ? JSON.stringify(vision, null, 2)
    : '';

  const proposalsText = proposedChanges.length > 0
    ? proposedChanges.map((p, i) => {
        const age = p.proposed_at ? (() => {
          const ms = Date.now() - new Date(String(p.proposed_at)).getTime();
          const h = Math.floor(ms / 3600000);
          return h < 24 ? `${h}h ago` : `${Math.floor(h / 24)}d ago`;
        })() : '?';
        return `${i + 1}. [${strVal(p.id as string)}] (${age}, section: ${strVal(p.section as string, 'general')})\n   ${strVal(p.observation as string)}`;
      }).join('\n')
    : 'No pending proposals.';

  const prompt = `${contextPrefix}${codebaseSummary}${fileTreeSection}

# Project
${projectName}

You are an architecture analyst. Your job is to analyze the codebase and output a
structured JSON document describing the architecture. Do NOT create files in the
repository, do NOT open pull requests, do NOT make any code changes. Your ONLY
output is a JSON file written to /tmp/. This is a read-only analysis task.

# Current design document
${existingDocJson || 'No document exists yet. Build one from scratch by analyzing the codebase.'}
${visionJson ? `
# Product vision (what SHOULD exist)
The strategic agent maintains this vision of what the project should become.
Use this to understand what systems are planned but may not exist yet.
${visionJson}
` : ''}
# Proposed changes from other agents
These are observations from tactical and implementation agents. Review each one.
${proposalsText}

# Instructions

1. Explore the codebase systematically:
   - Read entry points, configs, module boundaries first (package.json, requirements.txt, main files)
   - Understand how components connect, not how individual functions work
   - Identify WHAT exists at the system level
   - Look at directory structure, key config files, CI setup, test structure

2. Review proposed changes from other agents:
   - Validate each observation against the actual code
   - Accept accurate observations, reject incorrect ones
   - Include brief rationale for each decision

3. Update the design document with these sections:
   - **summary**: What this project is, in 1-2 sentences
   - **architecture**: Type (monolith, microservices, etc.), high-level description, key architectural decisions
   - **systems**: Major components/subsystems — their boundaries, status (mature/functional/partial/stub/missing), key modules
   - **capabilities**: Cross-cutting concerns — testing, monitoring, auth, CI/CD, docs, error handling, logging, etc. Status of each.
   - **risks**: Structural concerns, single points of failure, missing fundamentals, scalability issues

4. For **existing systems** (mature/functional/partial): describe WHAT IS accurately. No recommendations
   for things that already work. If something is broken, document it as broken. If a capability exists
   but is incomplete, describe what exists and what's absent.

5. For **missing or planned systems** (in the vision but missing/stub in the codebase): write an
   architectural specification. Add a "spec" field to that system entry with:
   - "approach": How to build it, grounded in the existing codebase's patterns and conventions
   - "technology": Technology choices that fit the existing stack
   - "interfaces": Key interfaces described semantically (what they expose and consume, not exact signatures)
   - "integration_points": How this system connects to existing systems (reference system IDs)
   - "notes": Trade-offs, constraints, alternative approaches considered
   Ground specs in what the codebase actually uses. Don't suggest a different framework or language
   unless the existing stack genuinely can't support the requirement.
   If the codebase is nearly empty, base specs on project setup files and standard patterns for that stack.
   Do NOT write specs for systems with status "mature" or "functional" — those are already built.

6. Keep system IDs stable across revisions. Use short, descriptive IDs like "sys-auth", "sys-pipeline", "sys-frontend".
   If the existing doc already has system IDs, preserve them.

7. Address open messages: if a user corrected the design doc or flagged an inaccuracy, incorporate their feedback. Mark addressed messages as resolved.

8. If you're genuinely uncertain about an architectural decision or need user input, create a message. Max 3 per cycle. Don't block on answers — note assumptions and move on.
${openMessagesText ? `\n# Open Messages\nMessages from users or previous cycles. Address corrections and feedback.\n${openMessagesText}\n` : ''}
# Output

IMPORTANT: Do NOT create any files in the repository. Do NOT commit, push, or open PRs.
Your only output is this JSON file:

Write to /tmp/design_doc_output_${taskId}.json:

\`\`\`json
{
  "design_doc": {
    "summary": "...",
    "architecture": {
      "type": "web_app|api|library|cli|monolith|microservices",
      "description": "High-level architecture overview",
      "key_decisions": ["decision 1", "decision 2"]
    },
    "systems": [
      {
        "id": "sys-short-id",
        "name": "System Name",
        "description": "What it does",
        "modules": ["key/file/paths"],
        "status": "mature|functional|partial|stub|missing",
        "notes": "optional details",
        "spec": {
          "approach": "How to build it (only for missing/stub/planned systems)",
          "technology": ["tech choices fitting the existing stack"],
          "interfaces": ["Semantic description of key interfaces"],
          "integration_points": ["sys-other-id: how it connects"],
          "notes": "Architectural considerations"
        }
      }
    ],
    "capabilities": {
      "capability_name": { "status": "mature|functional|partial|missing", "notes": "..." }
    },
    "risks": ["risk description 1", "risk description 2"]
  },
  "proposal_resolutions": [
    { "id": "pc-xxx", "status": "accepted|rejected", "notes": "brief rationale" }
  ],
  "messages": [
    { "type": "question|note", "content": "Your question about architecture", "context": {} }
  ],
  "resolved_message_ids": ["uuid-of-messages-you-addressed"]
}
\`\`\`

The "spec" field is ONLY for systems with status missing/stub/planned. Omit it for mature/functional systems.
The schema is flexible — add fields if they help capture the architecture accurately.
Keep descriptions concise but informative. Focus on boundaries and connections over implementation details.`;

  statusRef.value = 'Running architecture review...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(prompt, ws, statusRef, model, effort);

  const designDocOutput = readHandlerOutput(taskId, 'design_doc_output', ws);
  let logs = designDocOutput !== null ? sanitize(JSON.stringify(designDocOutput)) : '';
  if (!logs) {
    // Fall back to parsing output for JSON
    const jsonMatch = allOutput.match(/\{[\s\S]*"design_doc"[\s\S]*\}/);
    logs = sanitize(jsonMatch ? jsonMatch[0] : (lastResultText || allOutput.slice(-50_000)));
  }

  await apiPost('/api/agent/status', {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    artifact_type: 'RESEARCH_FINDINGS',
    logs,
  });
}
