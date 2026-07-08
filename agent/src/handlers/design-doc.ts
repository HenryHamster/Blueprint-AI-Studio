import fs from 'fs';
/**
 * Design document update handler — refine and expand project design docs.
 *
 * Reads the project's existing design document (stored in project.design_doc
 * JSONB), allows Claude to propose edits, and applies updates to specific
 * sections or entire documents. Used for incremental refinement as the project
 * evolves or new requirements emerge.
 *
 * Unlike the design handler (which creates new design documents from scratch),
 * this task modifies existing design docs: updating strategic decisions, adding
 * new system components, or clarifying ambiguous areas.
 *
 * Does not clone repo or produce PR; updates are stored directly on the project
 * design document artifact.
 */

import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sendLog, validateBranchName,
  persistAndPostStatus, apiGet, runClaudeCode,
  setCurrentPromptFingerprint, sendHeartbeatSync,
  resolveBaseBranch,
} from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import {
  readClaudeMd, buildContextPrefix, buildFileTree, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, writeDesignDocHelper, buildRetryContext, FIRST_TASK_GUIDANCE, DEFAULT_TOOL_GUIDANCE, PLAYBOOK_GUIDANCE,
  computePromptFingerprint,
} from '../prompt.js';
import {
  buildPromptSizeTelemetry, setPromptSizeTelemetry,
} from '../telemetry.js';
import { parseBackendTimestamp } from '../utils/time.js';
import {
  writeFetchHelper, buildFetchGuidance, DESIGN_DOC_FETCH_VERBS,
} from './strategic-helpers.js';

// ── Handler: Design Doc Update (Phase 1 of strategic cycle) ──
// Analyzes codebase architecture and incrementally updates the design doc
// via API calls. No JSON dump — each page is saved as the agent discovers it.
export async function handleDesignDocUpdate(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = resolveBaseBranch(payload, task);
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  statusRef.value = `Cloning ${owner}/${repo} for architecture review...`;
  sendLog(ws, `Starting design document update...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });
  writePlaybookHelper(taskId);

  const disallowedTools = configureAgentPermissions('design_doc_update');

  const contextPrefix = buildContextPrefix(task);
  const claudeMd = readClaudeMd();
  const codebaseSummary = claudeMd
    ? `## Codebase Instructions\n${claudeMd}\n\n`
    : '';
  const fileTreeSection = buildFileTree(ws);
  const projectName = strVal(payload.project_name, 'Unknown Project');
  const projectMetadata = task.project_metadata as Record<string, unknown> | undefined;
  const projectId = strVal(projectMetadata?.project_id);
  const designDocRetryContext = buildRetryContext(task);
  const designDocFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';

  // Build existing pages summary from the current design doc. The backend now
  // ships design_doc as a compact navigation index ({ sections: [...] }) built
  // by _design_doc_index (backend/app/strategic_spawning.py) instead of the full
  // per-section prose — bulky reference content is fetched on demand via
  // `./design-doc get <key>`. Each index entry carries key + optional summary /
  // reconciled status / backing modules / presence_warning. Render that as the
  // Current pages list so the curator sees the real systems/*, capabilities/*,
  // notes/* keys (not one bogus `sections` page). A flat key->value map is still
  // tolerated as a defensive fallback so this handler never mis-renders if the
  // backend reverts to the old shape.
  const designDoc = payload.design_doc as Record<string, unknown> | undefined;
  let existingPagesText = 'No pages exist yet. Build the design document from scratch.';
  if (designDoc) {
    const sections = designDoc.sections;
    let pages: string[];
    if (Array.isArray(sections)) {
      // Index shape: one entry per non-internal section.
      pages = sections
        .filter((s): s is Record<string, unknown> => !!s && typeof s === 'object')
        .map((s) => {
          const key = strVal(s.key);
          const parts: string[] = [];
          if (typeof s.status === 'string' && s.status.trim()) parts.push(`status: ${s.status}`);
          if (typeof s.summary === 'string' && s.summary.trim()) parts.push(s.summary);
          if (Array.isArray(s.modules) && s.modules.length > 0) {
            parts.push(`modules: ${(s.modules as unknown[]).filter(m => typeof m === 'string').join(', ')}`);
          }
          if (typeof s.presence_warning === 'string' && s.presence_warning.trim()) {
            parts.push(`⚠ ${s.presence_warning}`);
          }
          return `  ${key}${parts.length > 0 ? ` — ${parts.join(' | ')}` : ''}`;
        })
        .filter(line => line.trim() !== '');
    } else {
      // Defensive fallback: flat key->value map (legacy shape).
      pages = Object.entries(designDoc)
        .filter(([k]) => !k.startsWith('_'))
        .map(([k, v]) => {
          const preview = JSON.stringify(v).slice(0, 200);
          return `  ${k}: ${preview}${preview.length >= 200 ? '...' : ''}`;
        });
    }
    if (pages.length > 0) {
      existingPagesText = `${pages.length} pages exist (use \`./design-doc get <key>\` to read a section's full content before editing):\n${pages.join('\n')}`;
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
        const age = typeof m.created_at === 'string' ? (() => {
          const d = parseBackendTimestamp(m.created_at);
          if (!d) return '?';
          const ms = Date.now() - d.getTime();
          const h = Math.floor(ms / 3600000);
          return h < 24 ? `${h}h ago` : `${Math.floor(h / 24)}d ago`;
        })() : '?';
        return `- [${strVal(m.type)}] (id: ${strVal(m.id)}, from: ${strVal(m.from)}, open ${age}) ${strVal(m.content).slice(0, 200)}`;
      }).join('\n')
    : '';

  // Shipped changes manifest — tasks merged since last architecture review
  const shippedManifest = Array.isArray(payload.shipped_changes_manifest)
    ? payload.shipped_changes_manifest as Array<Record<string, unknown>>
    : [];
  let shippedManifestSection = '';
  if (shippedManifest.length > 0) {
    const lines: string[] = [];
    for (const group of shippedManifest) {
      const dirId = group.direction_id ? strVal(group.direction_id) : 'unassigned';
      const tasks = Array.isArray(group.tasks) ? group.tasks as Array<Record<string, unknown>> : [];
      const files = Array.isArray(group.all_changed_files) ? group.all_changed_files as string[] : [];
      lines.push(`### Direction: ${dirId} (${tasks.length} task${tasks.length !== 1 ? 's' : ''} merged)`);
      for (const t of tasks) {
        const scope = t.scope ? ` [${strVal(t.scope)}]` : '';
        lines.push(`  - ${strVal(t.title)}${scope}`);
      }
      if (files.length > 0) {
        lines.push(`  Changed files: ${files.slice(0, 20).join(', ')}${files.length > 20 ? ` (+${files.length - 20} more)` : ''}`);
      }
    }
    shippedManifestSection = `## Recently Shipped Changes (since last architecture review)\n${lines.join('\n')}\n`;
  }

  // Write the design-doc helper script that the inner agent can call via bash.
  // The script reads ORCHESTRATOR_URL and AGENT_API_TOKEN from env (passed by
  // runClaudeCode) so the token is never written to disk. set/delete retry on
  // 429/5xx contention; see writeDesignDocHelper in prompt.ts.
  writeDesignDocHelper(taskId);
  writeMessagesHelper(taskId);
  if (projectId) writeFetchHelper(taskId, projectId, WORKSPACE, { verbs: DESIGN_DOC_FETCH_VERBS });
  const designDocFetchGuidance = projectId ? buildFetchGuidance(DESIGN_DOC_FETCH_VERBS) : '';

  // CLAUDE.md as persistent reminder
  fs.writeFileSync(`${WORKSPACE}/CLAUDE.md`,
    `# Design Doc Agent\nCRITICAL: Your ONLY output is via \`./design-doc set\` commands.\nAny text you write will be DISCARDED. Only ./design-doc calls persist.\nBefore finishing, confirm you called ./design-doc set at least once.\n`
  );

  const prompt = `${contextPrefix}${designDocRetryContext}# YOUR ONLY OUTPUT MECHANISM

Your text output will be DISCARDED. The ONLY way to save your work is the \`./design-doc\` command:

\`\`\`bash
./design-doc set "summary" '"Brief project description"'
./design-doc set "architecture" '{"type": "monolith", "description": "...", "key_decisions": [...]}'
./design-doc set "systems/<id>" '{"id": "<id>", "name": "<System Name>", "status": "functional", "description": "...", "modules": ["<path/to/module>"]}'
./design-doc set "capabilities/testing" '{"status": "partial", "notes": "Unit tests only"}'
./design-doc set "risks" '["Risk 1", "Risk 2"]'
./design-doc set "notes/observations" '"Any notes for future reference"'
./design-doc delete "systems/sys-removed"
./design-doc get "systems/<id>"   # read a section's FULL content (the Current pages list below is only an index)
./design-doc list
\`\`\`

The "Current pages" section below is a compact INDEX (key + one-line summary), not the full prose.
Before you edit, merge, or delete an existing page, call \`./design-doc get "<key>"\` to read its full content.

Call \`./design-doc set\` AS YOU EXPLORE. Each call saves immediately. Do NOT wait until the end.
Do NOT create files in the repo. Do NOT commit or push. Only use ./design-doc.

# Context

## Project: ${projectName}

## Current pages
${existingPagesText}
${visionSection}
${openMessagesText ? `## Open Messages\n${openMessagesText}\n` : ''}${shippedManifestSection ? `${shippedManifestSection}\n` : ''}## Codebase
${codebaseSummary}${fileTreeSection}

# What to do

${shippedManifest.length > 0 ? `This is an INCREMENTAL update — do NOT re-survey the entire codebase from scratch.
Focus your analysis on systems touched by recently shipped changes (listed above).
Skip full re-analysis of systems with no files in the manifest.

` : ''}1. ${shippedManifest.length > 0 ? 'Focus exploration on files listed in the Recently Shipped Changes manifest. Use subagents to read those files in parallel.' : 'Explore the codebase THOROUGHLY — use subagents for parallel exploration.'}
2. For each system you find: \`./design-doc set "systems/<id>" '{...}'\`
   Status: mature | functional | partial | stub | missing
3. For capabilities: \`./design-doc set "capabilities/<name>" '{...}'\`
4. For risks: \`./design-doc set "risks" '[...]'\`
5. For notes/observations: \`./design-doc set "notes/<topic>" '"..."'\`
6. Use stable, kebab-case system IDs (sys-<name>) that describe THIS project's actual systems.
7. Focus on WHAT IS. Document missing things as missing.
8. For each system, include a "ready_for_features" field (true/false) with a brief reason.
   A system is ready if other systems can reliably build on it (APIs work, tests pass, config is wired up).
   A system is NOT ready if it's stub/partial, untested, or has unfinished wiring.
   This helps the strategic agent avoid proposing features on shaky foundations.
9. If open messages mention architecture or systems, incorporate those observations into the relevant pages.
   Do NOT resolve messages — the strategic review agent handles message responses and resolution.
10. ACTIVELY CURATE the design doc — it is not an append-only log.
    - Delete systems/capabilities/notes that describe code that no longer exists or no longer matters.
    - Merge duplicate or near-duplicate pages; rename to canonical IDs.
    - Use ./design-doc delete "<key>" liberally. A lean, accurate doc beats a sprawling one.
11. ACTIVELY CURATE the playbook (./playbook get, ./playbook set <key> <value>):
    - Overwrite stale entries with ./playbook set — entries describing fixed bugs, stale APIs, or patterns the codebase no longer uses should be replaced with corrected guidance.
    - Merge duplicates by overwriting one canonical key with the consolidated value. Don't hoard entries "just in case" — keep only what a future agent would actually benefit from.
    - There is no reward for playbook volume. Fewer, sharper entries is better than hundreds of vague ones.`;

  statusRef.value = 'Running architecture review...';
  const promptFingerprint = computePromptFingerprint('design_doc_update');
  setCurrentPromptFingerprint(promptFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });
  const fullPrompt = prompt + designDocFirstTaskSection + `\n\n${PLAYBOOK_GUIDANCE}\n\n${designDocFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const ddTelemetry = buildPromptSizeTelemetry(fullPrompt, [
    ['context_prefix', contextPrefix],
    ['retry_context', designDocRetryContext],
    ['existing_pages', existingPagesText],
    ['vision', visionSection],
    ['open_messages', openMessagesText],
    ['shipped_changes', shippedManifestSection],
    ['codebase_summary', codebaseSummary],
    ['file_tree', fileTreeSection],
    ['first_task', designDocFirstTaskSection],
    ['playbook', PLAYBOOK_GUIDANCE],
    ['fetch_guidance', designDocFetchGuidance],
    ['tool_guidance', DEFAULT_TOOL_GUIDANCE],
  ]);
  setPromptSizeTelemetry(ddTelemetry.prompt_size_bytes, ddTelemetry.prompt_section_count, ddTelemetry.prompt_section_sizes);
  let { exitCode } = await runClaudeCode(
    fullPrompt,
    ws, statusRef, model, effort, disallowedTools,
  );

  // Post-check: verify the agent actually called ./design-doc.
  // design_doc lives only on the project detail endpoint — the /api/admin/projects
  // list payload no longer carries JSONB columns. Skip the check entirely when no
  // project_id is available, otherwise pageCount would be a false 0 and trigger a
  // spurious retry of an already-successful task.
  if (projectId) {
    try {
      const detail = await apiGet(`/api/admin/projects/${projectId}`) as Record<string, unknown>;
      const doc = (detail.design_doc || {}) as Record<string, unknown>;
      const pageCount = Object.keys(doc).filter(k => !k.startsWith('_')).length;
      if (exitCode === 0 && pageCount === 0) {
        sendLog(ws, '\n[WARNING] Architecture review completed but no ./design-doc calls were made. Retrying...\n');
        const retryPrompt = 'You completed your analysis but did NOT call ./design-doc to save any pages. Your text was discarded.\n\n' +
          'You MUST call ./design-doc set now to save your findings.\n' +
          'The ./design-doc command is a bash script in the current directory. Run it via Bash.\n' +
          'Start with: ./design-doc set "summary" \'"Your project summary"\'\n' +
          'Then save systems, capabilities, and risks.';
        const retryTelemetry = buildPromptSizeTelemetry(retryPrompt, [
          ['retry_body', retryPrompt],
        ]);
        setPromptSizeTelemetry(retryTelemetry.prompt_size_bytes, retryTelemetry.prompt_section_count, retryTelemetry.prompt_section_sizes);
        const retryResult = await runClaudeCode(
          retryPrompt,
          ws, statusRef, model, effort, disallowedTools
        );
        exitCode = retryResult.exitCode;
      }
    } catch (e) {
      sendLog(ws, `[WARNING] Could not verify design doc API calls: ${String(e)}\n`);
    }
  } else {
    sendLog(ws, '[WARNING] No project_id in task metadata — skipping design doc post-check.\n');
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
