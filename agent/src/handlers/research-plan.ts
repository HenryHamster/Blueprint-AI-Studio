import fs from 'fs';
import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog,
  apiPost, cloneWithRetry, runClaudeCode,
} from '../index.js';
import {
  readClaudeMd, buildContextPrefix, buildFileTree,
} from '../prompt.js';

// ── Handler: Plan (decompose complex task into subtasks) ───────────────────
export async function handlePlan(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const prompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);
  const baseBranch = strVal(payload.base_branch, 'main');
  const planPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';

  if (!prompt) throw new Error('Plan task missing prompt');

  // Clone repo — planner needs codebase context for good decomposition
  statusRef.value = `Cloning ${owner}/${repo} for planning...`;
  sendLog(ws, `Planning task decomposition for: ${prompt.slice(0, 200)}\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await cloneWithRetry(['clone', '--depth', '50', '--branch', baseBranch, cloneUrl, WORKSPACE]);

  const contextPrefix = buildContextPrefix(task);

  const claudeMd = readClaudeMd();
  const claudeMdPrefix = claudeMd ? `## Codebase Instructions\n${claudeMd}\n\n` : '';

  const planPrompt = `${claudeMdPrefix}${contextPrefix}You are a planning agent. Your job is to analyze this codebase and decompose the following task into concrete, ordered subtasks that can each be implemented as a single pull request.

Task to decompose:
${prompt}

Instructions:
1. Explore the codebase thoroughly. Read key files, understand the architecture, find relevant patterns.
2. Decompose into substantial subtasks. Prefer fewer, larger subtasks over many tiny ones. A single subtask that modifies 200+ lines across 5+ files is fine — the implementing agent is capable. Only split when subtasks are genuinely independent or have different risk profiles.
3. Identify dependencies between subtasks — which must complete before others can start.
4. Write clear, self-contained prompts for each subtask. Describe the PROBLEM and desired outcome — do not prescribe exact line numbers. The implementing agent has full repo access and will figure out the details.

Output EXACTLY one JSON block as the VERY LAST thing in your response:

{
  "subtasks": [
    {
      "title": "Short title (under 60 chars, conventional commit style: feat:, fix:, refactor:, etc.)",
      "prompt": "Self-contained implementation prompt. Describe WHAT to achieve and key constraints. The agent has full repo access and will figure out implementation details.",
      "depends_on_indices": [],
      "related_files": ["src/main.py", "src/utils.py"],
      "model": "claude-sonnet-4-6 | claude-opus-4-6 | null (inherit from parent)",
      "effort": "low | medium | high | null (inherit from parent)",
      "scope": "medium",
      "risk": "low",
      "type": "create",
      "approval_reason": "Only set when risk=high — brief reason why human approval is needed (omit otherwise)"
    },
    {
      "title": "feat: second subtask",
      "prompt": "...",
      "depends_on_indices": [0],
      "related_files": ["src/main.py"],
      "model": null,
      "effort": null,
      "scope": "large",
      "risk": "medium",
      "type": "create"
    }
  ]
}

Risk guidance:
- risk=high: changes to auth flows, JWT/token handling, database models (models.py), Alembic migrations, core orchestration (main.py worker loops), VM provisioning (compute.py), any change that if wrong could cause data loss or security breach
- risk=medium: new API endpoints, changes to existing API request/response shapes, new agent handler types, frontend state management, WebSocket protocol changes
- risk=low: UI-only changes, new utility functions, log/error message changes, documentation, tests that don't touch production code
When risk=high for a subtask, the task will require human approval before executing. Set this judiciously — only when a mistake would be hard to roll back. When risk=high, also set "approval_reason" to a brief explanation of why this needs approval.

Rules:
- depends_on_indices uses 0-based array positions (e.g., [0, 1] means this subtask depends on the first and second subtasks)
- related_files: list the key files each subtask will create or modify
- Each subtask prompt must be self-contained — don't reference other subtasks by index
- Keep prompts concise — describe what to achieve, not step-by-step how
- Order subtasks so that dependencies flow forward (a subtask can only depend on earlier subtasks)
- If subtasks involve structural refactors of the same files (renames, interface changes, restructuring), set depends_on_indices between them. Small additive changes to shared files can stay parallel.
- Aim for 1-4 subtasks. Prefer 1-2 large subtasks over 4-6 small ones. If the task can be one PR, output a single subtask.
- Do NOT include review, testing, or deployment subtasks — those are handled automatically by the pipeline
- Use model=claude-opus-4-6 and effort=high only for complex architectural subtasks. Leave null for simple additive changes.
- scope: "small" for changes under ~100 lines across 1-3 files with clear, narrow scope, "medium" for changes spanning 3-8 files or 100-300 lines, or touching shared infrastructure, "large" for cross-component architectural changes, 300+ lines, or changes to auth/data models/core orchestration
- The "type" field controls how the subtask is executed: "create" (default) for new features/additions, "refactor" for code quality improvements with no behavior change, "migrate" for database schema changes (Alembic migrations), "qa" for test-only PRs.
- If any subtask modifies SQLAlchemy/SQLModel models (backend/app/models.py) or adds new database tables/columns, include a separate subtask with set type="migrate" (e.g., title "feat: add index for user lookup", type "migrate"). This subtask should depend on the model change subtask. The migration agent will handle Alembic generation.
- If any subtask adds new user-facing functionality, consider whether a test subtask is needed. New API endpoints should have corresponding integration tests. New agent handlers should have at least one smoke test. Add a test subtask with depends_on_indices pointing to the implementation subtask.
- If any subtask changes a public API interface (endpoint signatures, request/response shapes, TypeScript types exported to frontend), include explicit notes in the prompt about backward compatibility requirements.
- For subtasks that involve the agent system (agent/src/index.ts), note that the agent is a monolithic file — be aware of potential conflicts with parallel subtasks touching the same file.
- IMPORTANT: Write your plan JSON to /tmp/plan_output_${taskId}.json (NOT in the repository). The file should contain ONLY the JSON object, no markdown wrapping.${planPromptSuffix}`;

  statusRef.value = 'Running Claude Code for planning...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(planPrompt, ws, statusRef, model, effort);

  // Prefer reading plan from /tmp/ file (avoids output truncation), fall back to output parsing
  let logs = '';

  const planPath = `/tmp/plan_output_${taskId}.json`;
  try {
    if (fs.existsSync(planPath)) {
      logs = sanitize(fs.readFileSync(planPath, 'utf-8'));
      sendLog(ws, `Read plan output from ${planPath}\n`);
    }
  } catch {
    sendLog(ws, `Failed to read ${planPath}, falling back to output parsing\n`);
  }
  if (!logs) {
    if (lastResultText && lastResultText.includes('"subtasks"')) {
      logs = sanitize(lastResultText);
    } else {
      const jsonMatch = allOutput.match(/\{[\s\S]*"subtasks"\s*:\s*\[[\s\S]*\]\s*\}/);
      logs = sanitize(jsonMatch ? jsonMatch[0] : (lastResultText || allOutput.slice(-50_000)));
    }
  }

  // Try to parse plan JSON and spawn subtasks directly from the agent
  if (exitCode === 0 && logs) {
    try {
      let planData: Record<string, unknown> | null = null;
      try {
        planData = JSON.parse(logs) as Record<string, unknown>;
      } catch {
        const match = logs.match(/\{[\s\S]*"subtasks"\s*:\s*\[[\s\S]*\]\s*\}/);
        if (match) planData = JSON.parse(match[0]) as Record<string, unknown>;
      }

      const subtasks = planData?.subtasks;
      if (Array.isArray(subtasks) && subtasks.length > 0 && subtasks.length <= 10) {
        sendLog(ws, `Parsed ${subtasks.length} subtasks from plan output\n`);

        const spawnPayloads = (subtasks as Record<string, unknown>[]).map((st) => ({
          title: strVal(st.title, 'Untitled subtask'),
          description: strVal(st.prompt),
          prompt: strVal(st.prompt),
          type: strVal(st.type, 'create'),
          scope: strVal(st.scope, 'small'),
          risk: strVal(st.risk, 'low'),
          base_branch: baseBranch,
          depends_on_indices: st.depends_on_indices ?? [],
          related_files: st.related_files ?? [],
          ...(st.model ? { model: strVal(st.model) } : {}),
          ...(st.effort ? { effort: strVal(st.effort) } : {}),
          ...(st.approval_reason ? { approval_reason: strVal(st.approval_reason) } : {}),
        }));

        statusRef.value = 'Spawning subtasks...';
        const spawnResult = await apiPost('/api/agent/spawn', {
          task_id: taskId,
          payloads: spawnPayloads,
          requires_approval: false,
          context: { agent_spawned: true },
        }) as Record<string, unknown>;

        const spawnedIds = Array.isArray(spawnResult.task_ids) ? spawnResult.task_ids as string[] : [];
        sendLog(ws, `Spawned ${spawnedIds.length} subtasks: ${spawnedIds.join(', ')}\n`);

        // Include full plan data with spawn info for backend enrichment
        logs = JSON.stringify({
          ...planData,
          agent_spawned: true,
          spawned_task_ids: spawnedIds,
        });
      }
    } catch (err) {
      sendLog(ws, `Agent-side spawn failed: ${err instanceof Error ? err.message : String(err)}, falling back to backend processing\n`);
    }
  }

  await apiPost('/api/agent/status', {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : {}),
    logs,
  });
}

// ── Handler: Research (analyze codebase, output improvement findings) ──────
export async function handleResearch(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const directions = Array.isArray(payload.directions) ? payload.directions as Array<Record<string, unknown>> : [];
  const currentDirectionId = payload.current_direction_id as string | undefined;
  const baseBranch = strVal(payload.base_branch, 'main');
  const userPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);

  // Full clone — research needs history context
  statusRef.value = `Cloning ${owner}/${repo} for research...`;
  sendLog(ws, `Starting codebase research...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await cloneWithRetry(['clone', '--branch', baseBranch, cloneUrl, WORKSPACE]);

  let gitLogOutput = '';
  let gitDiffStatOutput = '';
  try {
    gitLogOutput = execFileSync('git', ['-C', WORKSPACE, 'log', '--oneline', '-30'], { encoding: 'utf-8' }).trim();
  } catch (e) {
    sendLog(ws, `Warning: git log failed: ${String(e)}\n`);
  }
  let gitShortlogOutput = '';
  try {
    gitShortlogOutput = execFileSync('git', ['-C', WORKSPACE, 'shortlog', '-sn', '--no-merges', '-20'], { encoding: 'utf-8' }).trim();
  } catch (e) {
    sendLog(ws, `Warning: git shortlog failed: ${String(e)}\n`);
  }
  try {
    gitDiffStatOutput = execFileSync('git', ['-C', WORKSPACE, 'diff', '--stat', 'HEAD~5..HEAD'], { encoding: 'utf-8' }).trim();
  } catch (e) {
    sendLog(ws, `Warning: git diff --stat failed: ${String(e)}\n`);
  }
  let hotspotsSection = '';
  try {
    const hotspots = execFileSync('git', ['log', '--pretty=format:', '--name-only', '-50'], {cwd: WORKSPACE, encoding: 'utf-8'})
      .split('\n').filter(Boolean);
    const fileCounts = hotspots.reduce<Record<string, number>>((acc, f) => ({...acc, [f]: (acc[f] ?? 0)+1}), {});
    const topFiles = Object.entries(fileCounts).sort((a,b) => b[1]-a[1]).slice(0, 10);
    hotspotsSection = `### Most Changed Files (last 50 commits)\n${topFiles.map(([f,c]) => `- ${f} (${c} changes)`).join('\n')}`;
  } catch (e) {
    sendLog(ws, `Warning: git hotspots failed: ${String(e)}\n`);
  }
  const gitHistorySection = (gitLogOutput || gitDiffStatOutput) ? `## Recent Git History (last 30 commits)
${gitLogOutput || '(unavailable)'}

## Top Contributors (last 20, no merges)
${gitShortlogOutput || '(unavailable)'}

## Recent Changes Summary (last 5 commits diff stat)
${gitDiffStatOutput || '(unavailable)'}

${hotspotsSection}

` : '';

  // Build codebase summary to reduce exploration overhead

  const claudeMd = readClaudeMd();
  const codebaseSummary = claudeMd
    ? `## Codebase Instructions\n${claudeMd}\n\n`
    : '';

  const fileTreeSection = buildFileTree(ws);

  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  const rawActiveTasks = projectCtx?.active_tasks;
  const activeTasks: Array<Record<string, unknown>> = Array.isArray(rawActiveTasks) ? rawActiveTasks as Array<Record<string, unknown>> : [];
  const rawQueuedTasks = projectCtx?.queued_tasks;
  const queuedTasks: Array<Record<string, unknown>> = Array.isArray(rawQueuedTasks) ? rawQueuedTasks as Array<Record<string, unknown>> : [];

  const directionsText = directions.length > 0
    ? directions.map((d, i) => {
        const pathScope = Array.isArray(d.paths) && (d.paths as string[]).length > 0
          ? ` [scope: ${(d.paths as string[]).join(', ')}]`
          : '';
        const history = Array.isArray(d.research_history) ? d.research_history as Array<Record<string, unknown>> : [];
        const agg = (d.history_aggregate || {}) as Record<string, number | string | null>;
        const totalCycles = history.length + ((agg.total_cycles as number) || 0);

        // One-line stats summary
        let statsLine = '';
        if (totalCycles > 0) {
          const totalTasks = history.reduce((s, h) => s + ((h.tasks_created as number) || 0), 0) + ((agg.total_tasks_created as number) || 0);
          const totalMerged = history.reduce((s, h) => s + ((h.prs_merged as number) || 0), 0) + ((agg.total_prs_merged as number) || 0);
          const totalFailed = history.reduce((s, h) => s + ((h.tasks_failed as number) || 0), 0) + ((agg.total_tasks_failed as number) || 0);
          const parts = [`${totalCycles} cycles`, `${totalTasks} tasks`];
          if (totalMerged > 0) parts.push(`${totalMerged} merged`);
          if (totalFailed > 0) parts.push(`${totalFailed} failed`);
          statsLine = `\n  Stats: ${parts.join(', ')}`;
        }

        // Research context (synthesized knowledge) — show in full, this is the good stuff
        const researchCtx = typeof d.research_context === 'string' && d.research_context.length > 0
          ? `\n  Accumulated knowledge:\n${d.research_context}`
          : '';

        // Condensed history: titles only from last 3 cycles
        let historySection = '';
        if (history.length > 0) {
          const historySlice = history.slice(-3);
          const allTitles = historySlice.flatMap(h =>
            Array.isArray(h.finding_titles) ? h.finding_titles as string[] : []
          );
          if (allTitles.length > 0) {
            historySection = `\n  Previous findings (titles — do NOT re-propose): ${allTitles.join(', ')}`;
          }
        }

        // Notes: last 2, truncated to 80 chars
        const notes = Array.isArray(d.notes) ? d.notes as Array<{text: string; added_at: string}> : [];
        let notesSection = '';
        if (notes.length > 0) {
          const recent = notes.slice(-2).map(n => `  \u2022 ${n.text.slice(0, 80)}`).join('\n');
          notesSection = `\n  Notes:\n${recent}`;
        }

        const saturationNote = d.possibly_saturated
          ? '\n  \u26a0 SATURATED: Consider a direction_update or direction_note instead of more findings.'
          : '';

        // Active tasks for this direction (avoid duplicating)
        let activeForDirNote = '';
        if (d.id === currentDirectionId) {
          const dirTasks = [...activeTasks, ...queuedTasks].filter(t => t['direction_id'] === d.id);
          if (dirTasks.length > 0) {
            activeForDirNote = `\n  In-flight: ${dirTasks.map(t => strVal(t['title'])).join(', ')}`;
          }
        }

        return `${i + 1}. [${strVal(d.priority, 'medium')}] ${strVal(d.text)}${pathScope} (id: ${strVal(d.id)})${statsLine}${researchCtx}${historySection}${notesSection}${saturationNote}${activeForDirNote}`;
      }).join('\n\n')
    : 'No specific directions provided. Identify general improvements.';

  // Build cross-direction context from OTHER active directions
  const otherDirections = currentDirectionId
    ? directions.filter(d => d.id !== currentDirectionId && d.status === 'active')
    : [];
  let crossDirSection = '';
  if (otherDirections.length > 0) {
    const snippets = otherDirections
      .filter(d => Array.isArray(d.research_history) && (d.research_history as unknown[]).length > 0)
      .map(d => {
        const history = d.research_history as Array<Record<string, unknown>>;
        const lastCycle = history[history.length - 1];
        const titles = (Array.isArray(lastCycle.finding_titles) ? lastCycle.finding_titles as string[] : []).slice(0, 3).join(', ');
        return `- "${strVal(d.text).slice(0, 80)}": ${String(lastCycle.tasks_created)} tasks (${titles || 'none'})`;
      })
      .slice(0, 3);  // max 3 other directions
    if (snippets.length > 0) {
      crossDirSection = `\n\nOther active directions (recent activity — avoid duplicating):\n${snippets.join('\n')}`;
    }
  }

  const contextPrefix = buildContextPrefix(task);

  const recentlyCompleted = Array.isArray(projectCtx?.recently_completed_tasks)
    ? (projectCtx.recently_completed_tasks as Array<{title: string, task_type: string, status?: string}>)
    : [];

  const researchPrompt = `${contextPrefix}You are a codebase research agent inside Blueprint, an autonomous AI coding system. Your findings spawn implementing agents, get reviewed, and merge as PRs.

# SECTION 1: MISSION

Your job: find what this codebase is MISSING. Not what's slightly wrong — what doesn't exist yet. Each finding you produce should require 100+ lines of real implementation across multiple files.

Before writing any finding, ask: "Would a senior engineer make this a standalone PR, or roll their eyes?" If the latter, it's not a finding.

BAD findings (will be rejected by the pipeline):
- "add X to Y" where the change is <50 lines — wiring, not engineering
- Cosmetic polish, trivial data plumbing, config tweaks, lint rules
- Standalone test additions — bundle into the feature that needs them
- Anything direction notes say is "in place" or "established"

GOOD findings:
- New subsystems or capabilities (cost tracking, knowledge persistence, live WebSocket view)
- Architectural decomposition (splitting monolithic files into modules)
- Cross-cutting changes affecting 3+ components
- Performance fixes for real bottlenecks (N+1 queries, blocking I/O)
- Missing user-facing capabilities that change how someone uses the product

Direction maturity — the MORE cycles, the BIGGER the findings:
- Cycles 1-2: Medium scope minimum. Surface-level gaps OK.
- Cycles 3-4: Large scope only. What's MISSING as a system?
- Cycles 5+: ONLY architectural or new-capability findings. If only small things remain, propose a direction_update or direction_note.

# SECTION 2: CODEBASE CONTEXT

${codebaseSummary}${fileTreeSection}${gitHistorySection}
# SECTION 3: DIRECTIONS
${userPrompt ? `Research focus: ${userPrompt}\n\n` : ''}${directionsText}${crossDirSection}

# SECTION 4: DEDUP — do not re-propose these
${recentlyCompleted.length > 0
  ? recentlyCompleted.map(t => {
      const status = t.status ?? 'completed';
      const label = status === 'failed' ? '(FAILED)' : status === 'cancelled' ? '(cancelled)' : '(merged)';
      return `- [${t.task_type}] ${t.title} ${label}`;
    }).join('\n')
  : '(none yet)'}

# SECTION 5: HOW TO RESEARCH

1. Explore aggressively — spawn subagents for parallel exploration, use WebSearch for best practices, use TodoWrite to track coverage. For each direction, ask: "what would it take for this to be DONE?" Work backward.
2. Think about what the system CAN'T do, not what it does slightly wrong. Missing capabilities > polish.
3. Before writing findings, verify you explored all major directories and have a TodoWrite checklist showing coverage.
4. Self-review: spawn a review subagent that REJECTs any finding <100 lines or touching only 1 file. If <3 survive, explore deeper.
5. Finding prompts should describe the PROBLEM and desired outcome — not exact line numbers. Let implementing agents make design decisions.

# SECTION 6: OUTPUT FORMAT

Write findings to /tmp/research_findings_${taskId}.json. Output the JSON array as the last thing in your response.

\`\`\`json
[{
  "type": "feature|improvement|bug|refactor|perf|research|security_audit|qa|deps|docs|migrate|test|direction_proposal|direction_update|direction_note",
  "title": "feat: short title (under 60 chars, conventional commit style)",
  "description": "1-3 sentences: what and why",
  "prompt": "Self-contained prompt for the implementing agent. Describe the problem and desired outcome.",
  "scope": "medium|large",
  "risk": "low|medium|high",
  "priority": "high|medium|low",
  "related_files": ["path/to/file.py"],
  "depends_on_indices": [],
  "direction_id": "d-xxx or null",
  "model": "claude-opus-4-6|claude-sonnet-4-6|null",
  "effort": "high|medium|low|null"
}]
\`\`\`

Scope: small (<50 lines, 1 file), medium (2-5 files, 50-200 lines), large (cross-component, 200+ lines).
Risk: low (cosmetic), medium (logic/endpoints), high (core logic, auth, data models).

Rules:
- Target 8-12 findings, all scope=medium or large. One scope=small "chore: housekeeping" bundling minor fixes is OK.
- Use scope=large freely — the pipeline has a plan agent for safe decomposition.
- Use model=claude-opus-4-6 and effort=high for large findings.
- Routing: large scope \u2192 plan agent (decomposition), medium \u2192 create agent (single PR), research \u2192 sub-research, direction_proposal \u2192 user review.
- direction_update: suggest rewording/reprioritizing a direction (fields: direction_id, suggested_text, suggested_priority, suggested_paths, description). Max 1 per cycle.
- direction_note: add context to a direction (fields: direction_id, description max 500 chars). Be honest about progress.
- direction_proposal: propose a new standing direction (max 2 per cycle, held for user approval).
- Optional: /tmp/research_insights_${taskId}.json with [{"summary": "<200 chars", "area": "path"}] for observations not worth implementing.`;

  const researchPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  statusRef.value = 'Running codebase research...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(researchPrompt + researchPromptSuffix, ws, statusRef, model, effort);

  // Prefer reading findings from /tmp/ file (avoids output truncation), fall back to output parsing
  let logs = '';
  const findingsPath = `/tmp/research_findings_${taskId}.json`;
  try {
    if (fs.existsSync(findingsPath)) {
      logs = sanitize(fs.readFileSync(findingsPath, 'utf-8'));
      sendLog(ws, `Read research findings from ${findingsPath}\n`);
    }
  } catch {
    sendLog(ws, `Failed to read ${findingsPath}, falling back to output parsing\n`);
  }
  if (!logs) {
    if (lastResultText && lastResultText.includes('"prompt"')) {
      logs = sanitize(lastResultText);
    } else {
      const jsonMatch = allOutput.match(/\[\s*\{[\s\S]*"prompt"[\s\S]*\}\s*\]/);
      logs = sanitize(jsonMatch ? jsonMatch[0] : (lastResultText || allOutput.slice(-50_000)));
    }
  }

  await apiPost('/api/agent/status', {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : { artifact_type: 'RESEARCH_FINDINGS' }),
    logs,
  });
}
