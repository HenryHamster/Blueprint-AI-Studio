import fs from 'fs';
import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, readHandlerOutput,
  apiPost, persistAndPostStatus, cloneWithRetry, runClaudeCode,
} from '../index.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import {
  PLAYBOOK_GUIDANCE, readClaudeMd, buildContextPrefix, buildFileTree, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook, excludeAgentHelpers,
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
  await cloneWithRetry(['clone', '--depth', String(DEFAULT_CLONE_DEPTH), '--branch', baseBranch, cloneUrl, WORKSPACE]);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('plan');

  const contextPrefix = buildContextPrefix(task);

  const claudeMd = readClaudeMd();
  const claudeMdPrefix = claudeMd ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const planPlaybookSection = formatPlaybook(task.playbook as Record<string, string> | undefined);

  const planPrompt = `${claudeMdPrefix}${planPlaybookSection}${contextPrefix}You are a planning agent. Your job is to analyze this codebase and decompose the following task into concrete, ordered subtasks that can each be implemented as a single pull request.

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
- IMPORTANT: Write your plan JSON to /tmp/plan_output_${taskId}.json (NOT in the repository). The file should contain ONLY the JSON object, no markdown wrapping.${planPromptSuffix}${PLAYBOOK_GUIDANCE}`;

  statusRef.value = 'Running Claude Code for planning...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(planPrompt, ws, statusRef, model, effort, disallowedTools);

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

  try {
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
      ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : {}),
      logs,
    });
  } catch (e) {
    sendLog(ws, `[ERROR] Failed to post plan completion status: ${e}\n`);
  }
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
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions(strVal(payload.research_mode as string, 'research'));

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
  const researchPlaybookSection = formatPlaybook(task.playbook as Record<string, string> | undefined);

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

        // Research context — restructure to avoid anchoring on small past findings.
        // Show failed approaches and open questions in full (high-signal steering).
        // Summarize established findings as a count to inform without anchoring scope.
        let researchCtx = '';
        if (typeof d.research_context === 'string' && (d.research_context as string).length > 0) {
          const rawCtx = d.research_context as string;
          const sections: Record<string, string[]> = { established: [], failed: [], open: [] };
          let currentSection = '';
          for (const line of rawCtx.split('\n')) {
            const trimmed = line.trim();
            if (trimmed.startsWith('## Established')) { currentSection = 'established'; continue; }
            if (trimmed.startsWith('## Failed')) { currentSection = 'failed'; continue; }
            if (trimmed.startsWith('## Open')) { currentSection = 'open'; continue; }
            if (trimmed.startsWith('- ') && currentSection) {
              sections[currentSection].push(trimmed);
            }
          }

          const ctxParts: string[] = [];
          if (sections.established.length > 0) {
            ctxParts.push(`  ${sections.established.length} findings already completed for this direction (do not re-propose similar scope work).`);
          }
          if (sections.failed.length > 0) {
            ctxParts.push(`  Failed approaches (avoid these):\n${sections.failed.join('\n')}`);
          }
          if (sections.open.length > 0) {
            ctxParts.push(`  Open questions:\n${sections.open.join('\n')}`);
          }
          if (ctxParts.length > 0) {
            researchCtx = `\n  Context:\n${ctxParts.join('\n')}`;
          }
        }

        // History titles: purely for dedup — framed as "already done", not as examples
        let historySection = '';
        if (history.length > 0) {
          const historySlice = history.slice(-5);
          const allTitles = historySlice.flatMap(h =>
            Array.isArray(h.finding_titles) ? h.finding_titles as string[] : []
          );
          if (allTitles.length > 0) {
            historySection = `\n  Already completed (dedup only — do NOT use as scope examples): ${allTitles.join(', ')}`;
          }
        }

        // Notes: show all notes in full (user-written guidance, highest signal)
        const notes = Array.isArray(d.notes) ? d.notes as Array<{text: string; added_at: string}> : [];
        let notesSection = '';
        if (notes.length > 0) {
          const rendered = notes.map(n => `  * ${n.text}`).join('\n');
          notesSection = `\n  Notes:\n${rendered}`;
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

  // Build anomaly signals section for undirected research cycles
  let anomalySection = '';
  if (!currentDirectionId) {
    const rawAnomalies = payload.anomaly_signals;
    if (Array.isArray(rawAnomalies) && rawAnomalies.length > 0) {
      const anomalyLines = (rawAnomalies as Array<{signal: string; severity: string; details: string}>)
        .map(a => `- [${strVal(a.severity)}] ${strVal(a.signal)}. ${strVal(a.details)}`)
        .join('\n');
      anomalySection = `\n## Suggested Investigation Areas\nBased on pipeline performance analysis:\n${anomalyLines}\n`;
    }
  }

  const contextPrefix = buildContextPrefix(task);

  // ── Strategic mode: product-level vision review ────────────────────────
  if (payload.research_mode === 'strategic') {
    const vision = payload.vision as Record<string, unknown> | undefined;
    const directionsSummary = Array.isArray(payload.directions_summary)
      ? payload.directions_summary as Array<Record<string, unknown>>
      : [];
    const projectName = strVal(payload.project_name, 'Unknown Project');

    const openMessages = Array.isArray(payload.open_messages)
      ? payload.open_messages as Array<Record<string, unknown>>
      : [];

    const visionJson = vision && Object.keys(vision).length > 0
      ? JSON.stringify(vision, null, 2)
      : '';

    // Build outcome evaluation section
    const outcomeEval = payload.outcome_evaluation as Record<string, unknown> | undefined;
    let outcomeSection = '';
    if (outcomeEval && typeof outcomeEval === 'object') {
      const window = outcomeEval.window as Record<string, unknown> | undefined;
      const overall = outcomeEval.overall as Record<string, unknown> | undefined;
      const systems = Array.isArray(outcomeEval.systems) ? outcomeEval.systems as Array<Record<string, unknown>> : [];
      const dirOutcomes = Array.isArray(outcomeEval.directions) ? outcomeEval.directions as Array<Record<string, unknown>> : [];

      const lines: string[] = [];
      if (window) {
        lines.push(`# Outcome evaluation (since vision rev ${window.vision_revision || 0})`);
        lines.push(`Window: ${strVal(window.from as string, '?')} to ${strVal(window.to as string, '?')}`);
      }
      if (overall) {
        const sr = typeof overall.success_rate === 'number' ? Math.round(overall.success_rate * 100) : '?';
        lines.push(`Overall: ${overall.cycles || 0} cycles, ${overall.merged || 0} merged, ${overall.failed || 0} failed (${sr}% success rate, trend: ${strVal(overall.trend as string, 'stable')})`);
      }

      if (systems.length > 0) {
        lines.push('');
        lines.push('## System progress');
        lines.push('Each vision system compared against actual direction outcomes:');
        for (const sys of systems) {
          const gap = (sys.directions_count as number) === 0 ? ' [NO DIRECTIONS MAPPED]' : '';
          lines.push(`- ${strVal(sys.name as string)} (${strVal(sys.id as string)}): planned=${strVal(sys.planned_status as string)}, ${sys.directions_count || 0} directions, ${sys.merged || 0} merged, ${sys.failed || 0} failed${gap}`);
        }
      }

      if (dirOutcomes.length > 0) {
        lines.push('');
        lines.push('## Direction outcomes');
        lines.push('What each direction actually produced. Use confirmed findings to judge real impact:');
        for (const d of dirOutcomes) {
          const sysTag = d.system_id ? ` (system: ${strVal(d.system_id as string)})` : '';
          lines.push(`\n### ${strVal(d.text as string)} (${strVal(d.id as string)})${sysTag} [${strVal(d.status as string)}]`);
          lines.push(`Stats: ${d.cycles || 0} cycles, ${d.merged || 0} merged, ${d.failed || 0} failed`);

          const lifetime = d.lifetime as Record<string, number> | undefined;
          if (lifetime) {
            lines.push(`Lifetime: ${lifetime.cycles || 0} total cycles, ${lifetime.merged || 0} total merged`);
          }

          const confirmed = Array.isArray(d.confirmed) ? d.confirmed as string[] : [];
          if (confirmed.length > 0) {
            lines.push(`Confirmed (shipped): ${confirmed.join('; ')}`);
          }
          const proposed = Array.isArray(d.proposed) ? d.proposed as string[] : [];
          if (proposed.length > 0) {
            lines.push(`Proposed (in-flight): ${proposed.join('; ')}`);
          }
          const failedApproaches = Array.isArray(d.failed_approaches) ? d.failed_approaches as string[] : [];
          if (failedApproaches.length > 0) {
            lines.push(`Failed approaches: ${failedApproaches.slice(0, 5).join('; ')}`);
          }
        }
      }

      if (lines.length > 0) {
        outcomeSection = '\n' + lines.join('\n') + '\n';
      }
    }

    const calcMessageAge = (m: Record<string, unknown>): string => {
      if (!m.created_at) return '?';
      const ms = Date.now() - new Date(String(m.created_at)).getTime();
      const h = Math.floor(ms / 3600000);
      return h < 24 ? `${h}h` : `${Math.floor(h / 24)}d`;
    };

    const feedbackMessages = openMessages.filter(m => strVal(m.type) === 'FEEDBACK');
    const otherMessages = openMessages.filter(m => strVal(m.type) !== 'FEEDBACK');

    const feedbackSection = feedbackMessages.length > 0
      ? (() => {
          const feedbackLines = feedbackMessages.map(m => {
            const age = calcMessageAge(m);
            return `- (id: ${strVal(m.id)}, from: ${strVal(m.from)}, open ${age}) ${strVal(m.content).slice(0, 300)}`;
          }).join('\n');
          return `\n## User Feature Requests\nThese FEEDBACK messages are direct user requests. Strongly consider creating a direction for each unaddressed request:\n${feedbackLines}\n`;
        })()
      : '';

    const openMessagesText = otherMessages.length > 0
      ? otherMessages.map(m => {
          const age = calcMessageAge(m);
          return `- [${strVal(m.type)}] (id: ${strVal(m.id)}, from: ${strVal(m.from)}, open ${age}) ${strVal(m.content).slice(0, 200)}`;
        }).join('\n')
      : '';

    const dirSummaryText = directionsSummary.length > 0
      ? directionsSummary.map((d, i) => {
          const parts = [`${d.total_cycles || 0} cycles`];
          if (d.prs_merged) parts.push(`${d.prs_merged} merged`);
          if (d.tasks_failed) parts.push(`${d.tasks_failed} failed`);
          const satNote = d.possibly_saturated ? ' [SATURATED]' : '';
          const sysNote = d.system_id ? ` (system: ${strVal(d.system_id)})` : '';

          // Research context: show established findings count, failed approaches, open questions
          let ctxSection = '';
          if (typeof d.research_context === 'string' && (d.research_context as string).length > 0) {
            const rawCtx = d.research_context as string;
            const sections: Record<string, string[]> = { established: [], failed: [], open: [] };
            let currentSection = '';
            for (const line of rawCtx.split('\n')) {
              const trimmed = line.trim();
              if (trimmed.startsWith('## Established')) { currentSection = 'established'; continue; }
              if (trimmed.startsWith('## Failed')) { currentSection = 'failed'; continue; }
              if (trimmed.startsWith('## Open')) { currentSection = 'open'; continue; }
              if (trimmed.startsWith('- ') && currentSection) {
                sections[currentSection].push(trimmed);
              }
            }
            const ctxParts: string[] = [];
            if (sections.established.length > 0) {
              const confirmed = sections.established.filter(s => s.includes('(confirmed)'));
              const proposed = sections.established.filter(s => s.includes('(proposed)'));
              ctxParts.push(`   ${confirmed.length} confirmed findings, ${proposed.length} proposed`);
            }
            if (sections.failed.length > 0) {
              ctxParts.push(`   Failed approaches: ${sections.failed.slice(0, 3).join('; ')}`);
            }
            if (sections.open.length > 0) {
              ctxParts.push(`   Open questions: ${sections.open.slice(0, 3).join('; ')}`);
            }
            if (ctxParts.length > 0) ctxSection = '\n' + ctxParts.join('\n');
          }

          // Recent findings: what was actually discovered/built
          let findingsSection = '';
          const findings = Array.isArray(d.recent_findings) ? d.recent_findings as string[] : [];
          if (findings.length > 0) {
            findingsSection = `\n   Recent work: ${findings.slice(0, 5).join('; ')}`;
          }

          // Recent failures: why things failed
          let failuresSection = '';
          const failures = Array.isArray(d.recent_failures) ? d.recent_failures as Array<Record<string, string>> : [];
          if (failures.length > 0) {
            const failLines = failures.slice(0, 3).map(f => `${f.title || '?'}: ${f.reason || 'unknown'}`);
            failuresSection = `\n   Recent failures: ${failLines.join('; ')}`;
          }

          return `${i + 1}. [${strVal(d.priority, 'medium')}] ${strVal(d.text)} (id: ${strVal(d.id)})${satNote}${sysNote}\n   Stats: ${parts.join(', ')}${ctxSection}${findingsSection}${failuresSection}`;
        }).join('\n\n')
      : 'No directions exist yet.';

    // Build design doc section — this is the primary input for Phase 2
    const designDoc = payload.design_doc as Record<string, unknown> | undefined;
    const designDocJson = designDoc && Object.keys(designDoc).length > 0
      ? JSON.stringify(designDoc, null, 2)
      : '';

    // Write the strategic helper script (same pattern as design-doc)
    // Uses printf + pipe to avoid shell expansion of JSON in curl -d
    const strategicHelper = `#!/bin/bash
# Strategic review tool — updates vision, directions, and messages via API
# Reads ORCHESTRATOR_URL and AGENT_API_TOKEN from env
OP="\${1:?Usage: strategic set-vision|create-direction|update-direction|retire-direction|create-message|resolve-message <json>}"
shift
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${taskId}"
DATA="\$1"

_call() {
  local attempt=0
  while [ \$attempt -lt 3 ]; do
    if printf '{"task_id":"%s","op":"%s","data":%s}' "\$TASK" "\$1" "\$DATA" | \\
      curl -f -sS --max-time 30 --connect-timeout 10 \\
        -X POST "\$API/api/agent/strategic" \\
        -H "Content-Type: application/json" \\
        -H "X-Agent-Token: \$TOKEN" \\
        --data-binary @-; then
      return 0
    fi
    attempt=\$((attempt + 1))
    [ \$attempt -lt 3 ] && echo "[RETRY \$attempt/3] \$1 failed, retrying in 5s..." >&2 && sleep 5
  done
  echo "[ERROR] \$1 failed after 3 attempts" >&2
  return 1
}

case "\$OP" in
  set-vision)        _call "set_vision" ;;
  create-direction)  _call "create_direction" ;;
  update-direction)  _call "update_direction" ;;
  retire-direction)  _call "retire_direction" ;;
  create-message)    _call "create_message" ;;
  resolve-message)   _call "resolve_message" ;;
  *) echo "Unknown op: \$OP" >&2; exit 1 ;;
esac
echo ""
`;
    fs.writeFileSync(`${WORKSPACE}/strategic`, strategicHelper, { mode: 0o755 });
    excludeAgentHelpers();

    // Also write design-doc helper so strategic agent can read/write sections
    const designDocHelper = `#!/bin/bash
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
OP="\${1:?Usage: design-doc get|set|delete|list <key> [json-value]}"
KEY="\$2"
VALUE="\$3"
if [ "\$OP" = "list" ]; then
  curl -f -X POST "\$API/api/agent/design-doc" -H "Content-Type: application/json" -H "X-Agent-Token: \$TOKEN" -d "{\\"task_id\\": \\"${taskId}\\", \\"op\\": \\"list\\"}"
elif [ "\$OP" = "get" ]; then
  curl -f -X POST "\$API/api/agent/design-doc" -H "Content-Type: application/json" -H "X-Agent-Token: \$TOKEN" -d "{\\"task_id\\": \\"${taskId}\\", \\"op\\": \\"get\\", \\"key\\": \\"\$KEY\\"}"
elif [ "\$OP" = "set" ]; then
  curl -f -X POST "\$API/api/agent/design-doc" -H "Content-Type: application/json" -H "X-Agent-Token: \$TOKEN" -d "{\\"task_id\\": \\"${taskId}\\", \\"op\\": \\"set\\", \\"key\\": \\"\$KEY\\", \\"value\\": \$VALUE}"
elif [ "\$OP" = "delete" ]; then
  curl -f -X POST "\$API/api/agent/design-doc" -H "Content-Type: application/json" -H "X-Agent-Token: \$TOKEN" -d "{\\"task_id\\": \\"${taskId}\\", \\"op\\": \\"delete\\", \\"key\\": \\"\$KEY\\"}"
fi
echo ""
`;
    fs.writeFileSync(`${WORKSPACE}/design-doc`, designDocHelper, { mode: 0o755 });
    excludeAgentHelpers();

    // ── Pass 1: Thinker — analyze freely, write prose ──
    // Let the agent do what it's good at: read code, reason about architecture,
    // write a strategic analysis. No API constraints, no JSON formatting.
    const thinkerPrompt = `${contextPrefix}You are a strategic product architect reviewing this project.

# Project: ${projectName}

## Architecture (design document — current state)
${designDocJson || 'No design document yet.'}

## Vision (current)
${visionJson || 'No vision set yet.'}
${outcomeSection}
## Current research directions
${dirSummaryText}
${feedbackSection}${openMessagesText ? `## Open Messages\n${openMessagesText}\n` : ''}
## Codebase
${codebaseSummary}${researchPlaybookSection}${fileTreeSection}

# Your task

Analyze this project and write a strategic review. Cover:

1. **Vision**: What should this project's purpose, success criteria, and key systems be?
   Define systems with IDs (sys-xxx), names, descriptions, statuses (planned/in_progress/functional/mature), and dependencies.
   Order priorities — which systems matter most right now?

2. **Directions**: What are the 3-5 most impactful research directions?
   For each: a clear description, priority (high/medium/low), which system_id it serves, and rationale.
   If existing directions should be updated or retired, say so with rationale.
   For FEEDBACK messages: If a user has requested a feature or capability, create a direction for it
   (unless one already exists that addresses it). Set priority based on urgency implied by the request.
   After creating the direction, resolve the FEEDBACK message with a note explaining which direction addresses it.

3. **Notes**: Any strategic observations worth preserving for future cycles.

Think about what would deliver the most value to users. Balance infrastructure improvements with product features and user-visible outcomes. Don't default to structural work when product features would have more impact.
Write your analysis as structured prose — it will be processed by a follow-up step.

IMPORTANT: End your analysis with a section titled '## Structured Output' containing:
- A JSON block with the complete vision object (purpose, success_criteria, systems, priorities)
- A list of direction operations (create/update/retire with all required parameters)
This structured section will be used directly by the executor. Make it complete and self-contained.`;

    statusRef.value = 'Running strategic analysis (pass 1: thinking)...';
    const thinkerResult = await runClaudeCode(thinkerPrompt, ws, statusRef, model, effort, disallowedTools);
    const rawAnalysis = thinkerResult.lastResultText || thinkerResult.allOutput.slice(-80_000);
    // Prefer the structured output section if present — it's compact and complete for the executor
    const structuredMatch = rawAnalysis.match(/## Structured Output[\s\S]*$/i);
    const analysisText = structuredMatch ? structuredMatch[0] + '\n\n---\nFull analysis:\n' + rawAnalysis.slice(0, 20_000) : rawAnalysis;

    // ── Pass 2: Executor — translate analysis into API calls ──
    // Short, focused pass. The analysis is done. This just calls the tools.
    fs.writeFileSync(`${WORKSPACE}/CLAUDE.md`,
      `# Executor Agent\nYour ONLY job is to call ./strategic and ./design-doc commands.\nDo NOT analyze code. Do NOT write prose. Just run the bash commands.\n`
    );

    const executorPrompt = `You are an executor. Your ONLY job is to translate the strategic analysis below into API calls using ./strategic and ./design-doc bash commands.

Do NOT analyze code. Do NOT explore the codebase. Do NOT write explanations. Just run the commands.

# Commands available

\`\`\`bash
# Set the vision (REQUIRED)
./strategic set-vision '{"purpose": "...", "success_criteria": ["..."], "systems": [{"id": "sys-x", "name": "...", "description": "...", "status": "planned|in_progress|functional|mature", "depends_on": []}], "priorities": ["sys-x"], "context": "..."}'

# Create research directions (max 5)
# conflicts_with: list direction IDs that touch overlapping code/systems — they won't run in parallel
./strategic create-direction '{"text": "...", "priority": "high|medium|low", "system_id": "sys-x", "rationale": "...", "conflicts_with": ["d-xxx"]}'

# Update existing directions (can set conflicts_with to control parallel execution)
./strategic update-direction '{"direction_id": "d-xxx", "changes": {"priority": "medium", "conflicts_with": ["d-yyy"]}, "rationale": "..."}'

# Retire directions
./strategic retire-direction '{"direction_id": "d-xxx", "rationale": "..."}'

# Save notes
./design-doc set "notes/strategic-review" '"..."'

# Respond to or resolve user messages
./messages resolve <message-id> 'How it was addressed'
./messages send 'Response or note to the user'
\`\`\`

# Existing directions (for update/retire operations)
${dirSummaryText}

${feedbackSection}${openMessagesText ? `# Open Messages (resolve addressed ones, respond to questions)\n${openMessagesText}\n` : ''}
# Strategic analysis to execute

${analysisText.slice(0, 50_000)}

# Instructions

1. Run ./strategic set-vision with the vision described in the analysis.
2. Run ./strategic create-direction for each new direction recommended.
3. Run ./strategic retire-direction for any directions the analysis says to retire.
4. Run ./design-doc set "notes/strategic-review" with key observations.
5. Review open messages — resolve any that have been addressed, respond to user questions.
6. Done. Do not write anything else.`;

    statusRef.value = 'Running strategic execution (pass 2: applying decisions)...';
    let { exitCode, allOutput, lastResultText } = await runClaudeCode(executorPrompt, ws, statusRef, model, effort, disallowedTools);

    // Post-check: verify the executor actually called ./strategic set-vision.
    // Check the output for the set_vision response.
    // Matches the design-doc.ts post-check + retry pattern (lines 185-205).
    try {
      // Check: look for set_vision success response in executor output
      const visionSetInOutput = /"op"\s*:\s*"set_vision"/.test(allOutput);

      if (exitCode === 0 && !visionSetInOutput) {
        sendLog(ws, '\n[WARNING] Strategic executor completed but ./strategic set-vision was not called. Retrying...\n');
        const retryResult = await runClaudeCode(
          'Your previous attempt did not call ./strategic set-vision. This is REQUIRED.\n\n' +
          'Run this command NOW with the vision from your analysis:\n' +
          './strategic set-vision \'{"purpose": "...", "success_criteria": [...], "systems": [...], "priorities": [...]}\'\n' +
          'Extract the vision from the analysis and call the command. Nothing else.',
          ws, statusRef, model, effort, disallowedTools
        );
        exitCode = retryResult.exitCode;
        allOutput = allOutput + '\n' + retryResult.allOutput;
        lastResultText = retryResult.lastResultText || lastResultText;
      }
    } catch (e) {
      sendLog(ws, `[WARNING] Could not verify strategic set-vision call: ${e}\n`);
    }

    // Legacy: try to read /tmp output file (backward compat)
    const strategicOutput = readHandlerOutput(taskId, 'strategic_output', ws);
    let logs = strategicOutput !== null ? sanitize(JSON.stringify(strategicOutput)) : '';
    if (!logs) {
      const jsonMatch = allOutput.match(/\{[\s\S]*?"vision"[\s\S]*?"direction_operations"[\s\S]*?\}\s*$/);
      logs = sanitize(jsonMatch ? jsonMatch[0] : (lastResultText || allOutput.slice(-50_000)));
    }

    try {
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
        ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : { artifact_type: 'RESEARCH_FINDINGS' }),
        logs,
      });
    } catch (e) {
      sendLog(ws, `[ERROR] Failed to post strategic completion status: ${e}\n`);
    }
    return;
  }

  // ── Tactical research mode (default) ──────────────────────────────────
  const recentlyCompleted = Array.isArray(projectCtx?.recently_completed_tasks)
    ? (projectCtx.recently_completed_tasks as Array<{title: string, task_type: string, status?: string}>)
    : [];

  // Build design doc section for tactical research
  const tacticalDesignDoc = payload.design_doc as Record<string, unknown> | undefined;
  const dirDesignCtx = payload.direction_design_context as Record<string, unknown> | undefined;

  // Direction-specific design doc highlight — surfaces relevant system + spec upfront
  let designDocHighlight = '';
  if (dirDesignCtx) {
    const lines: string[] = ['# YOUR DIRECTION\'S ARCHITECTURE CONTEXT'];
    const sys = dirDesignCtx.system as Record<string, unknown> | undefined;
    if (sys) {
      lines.push(`System: ${strVal(sys.name as string)} (${strVal(sys.id as string)}) — status: **${strVal(sys.status as string)}**`);
      if (sys.description) lines.push(`Description: ${strVal(sys.description as string)}`);
      if (sys.spec) {
        const spec = sys.spec as Record<string, unknown>;
        lines.push('Architectural spec (use this as your blueprint):');
        if (spec.approach) lines.push(`  Approach: ${strVal(spec.approach as string)}`);
        if (Array.isArray(spec.technology)) lines.push(`  Technology: ${(spec.technology as string[]).join(', ')}`);
        if (Array.isArray(spec.interfaces)) lines.push(`  Interfaces: ${(spec.interfaces as string[]).join('; ')}`);
        if (Array.isArray(spec.integration_points)) lines.push(`  Integration: ${(spec.integration_points as string[]).join('; ')}`);
        if (spec.notes) lines.push(`  Notes: ${strVal(spec.notes as string)}`);
      }
    } else {
      lines.push(`System ${strVal(dirDesignCtx.system_id as string)}: not yet documented in design doc`);
    }
    const missingCaps = dirDesignCtx.missing_capabilities as Record<string, Record<string, string>> | undefined;
    if (missingCaps && Object.keys(missingCaps).length > 0) {
      lines.push('Missing/partial capabilities relevant to your direction:');
      for (const [name, cap] of Object.entries(missingCaps)) {
        lines.push(`  - ${name}: ${cap.status}${cap.notes ? ` (${cap.notes})` : ''}`);
      }
    }
    lines.push('');
    designDocHighlight = lines.join('\n') + '\n';
  }

  // Design doc: only include key listing + playbook. Full sections available via ./design-doc get <key>.
  let designDocSection = '';
  if (tacticalDesignDoc && Object.keys(tacticalDesignDoc).length > 0) {
    const docKeys = (tacticalDesignDoc._keys as string[]) || Object.keys(tacticalDesignDoc).filter(k => !k.startsWith('_') && !k.startsWith('playbook/'));
    const playbookEntries = Object.entries(tacticalDesignDoc)
      .filter(([k]) => k.startsWith('playbook/'))
      .map(([k, v]) => `- **${k.replace('playbook/', '')}**: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
      .join('\n');
    designDocSection = `# ARCHITECTURE (Design Document)
Available sections (use \`./design-doc get <key>\` to read): ${docKeys.join(', ')}

${playbookEntries ? `## Operational Playbook\n${playbookEntries}\n` : ''}
`;
  }

  const researchPrompt = `${contextPrefix}${researchPlaybookSection}You are a codebase research agent inside Blueprint, an autonomous AI coding system. Your findings spawn implementing agents, get reviewed, and merge as PRs.

# MISSION

Your job is to advance the system toward its directions. HOW you do that depends on what exists:

**If a direction targets something MISSING** (the design document shows it as missing, stub, or
nonexistent): your job is to DESIGN it. Output a single large-scope finding that builds the
foundation. The plan agent will decompose it into ordered implementation steps. Don't output
5 small findings when the system needs 1 foundational one.

**If the design document has a spec for the target system** (look for a "spec" field on the
system entry): use it as your architectural blueprint. The architecture agent analyzed the
full codebase and wrote that spec to fit the existing stack. Follow its approach, technology
choices, and integration points. You should still explore the codebase to verify the spec
makes sense, but don't redesign from scratch when a spec exists.

**If the codebase is nearly empty** (few source files, mostly config/scaffolding): this is
normal for new projects. Use the design doc specs and the project's config files to create
foundational structure. Output a large-scope finding that bootstraps the target system.

**If a direction targets something EXISTING** (the design document shows it as partial, functional,
or mature): your job is to IMPROVE it. Find specific, high-impact changes — missing capabilities,
broken assumptions, architectural bottlenecks, or small fixes that prevent cascade failures.
Quality over quantity — 2 real findings beats 10 incremental ones.

**Sequencing matters.** If your findings have dependencies (e.g., "build cache infrastructure"
must come before "add caching to user API"), use depends_on_indices to order them. The pipeline
respects these — dependent tasks won't start until prerequisites complete.

**Sequence wisely.** If foundational infrastructure is genuinely missing (check research context for gaps),
build it. But if foundations exist, prioritize user-visible features and product improvements over
additional structural refinement. Don't re-propose foundations that are already confirmed.

# CODEBASE CONTEXT

${codebaseSummary}${fileTreeSection}${gitHistorySection}
${designDocHighlight}${designDocSection}# DIRECTIONS
${userPrompt ? `Research focus: ${userPrompt}\n\n` : ''}${directionsText}${crossDirSection}
${anomalySection}
# DEDUP — do not re-propose these
${recentlyCompleted.length > 0
  ? recentlyCompleted.map(t => {
      const status = t.status ?? 'completed';
      const label = status === 'failed' ? '(FAILED)' : status === 'cancelled' ? '(cancelled)' : '(merged)';
      return `- [${t.task_type}] ${t.title} ${label}`;
    }).join('\n')
  : '(none yet)'}

# HOW TO RESEARCH

1. Explore aggressively — spawn subagents for parallel exploration, use WebSearch for best practices, use TodoWrite to track coverage.
2. For each direction, ask: "what would it take for this to be DONE?" Work backward from the goal.
3. Check the design document — if the direction targets something missing, look for a spec on that system entry first. If a spec exists, use it as your design foundation. If no spec, design from scratch. If the direction targets something existing, improve it.
4. Before writing findings, verify you explored all major directories.
5. Finding prompts should describe the PROBLEM and desired outcome — not exact line numbers.
   For foundational findings (scope=large), include: what the system needs and why, recommended
   approach and technology choices, key architectural decisions, and how it fits with existing systems.
   Let implementing agents handle the code, but give them the design.

# SCOPE GUIDANCE

- **Building something new?** Use scope=large. Write the finding prompt as a design brief.
  The plan agent will decompose this into ordered implementation steps.
- **Improving something existing?** Use scope=small or medium as appropriate.
- **Building features on a foundation?** If research context shows a foundation was confirmed
  (e.g., "(confirmed)" entries), output findings that USE it. These are typically scope=medium.

Output ALL meaningful findings you discover — don't stop at one.
A direction may need both foundational work AND incremental improvements.
If you find 5 things worth doing, output 5 findings. Be ambitious — propose
infrastructure, new systems, and architectural changes when the direction calls
for it. Use scope=large freely. The pipeline handles decomposition and review.

# OUTPUT FORMAT

Write findings to /tmp/research_findings_${taskId}.json. Output the JSON array as the last thing in your response.

\`\`\`json
[{
  "type": "feature|improvement|bug|refactor|perf|research|security_audit|qa|deps|docs|migrate|test|direction_proposal|direction_update|direction_note|design_doc_observation",
  "title": "feat: short title (under 60 chars, conventional commit style)",
  "description": "1-3 sentences: what and why",
  "prompt": "Self-contained prompt for the implementing agent. Describe the problem and desired outcome.",
  "scope": "small|medium|large",
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
Routing: large scope → plan agent (decomposition), medium → create agent (single PR), research → sub-research.

- Use scope=large freely — the pipeline has a plan agent for safe decomposition.
- direction_update: suggest rewording/reprioritizing a direction (fields: direction_id, suggested_text, suggested_priority, suggested_paths, description). Max 1 per cycle.
- direction_note: add context to a direction (fields: direction_id, description max 2000 chars). Be honest about progress.
- direction_proposal: propose a new standing direction (max 2 per cycle, held for user approval).
- design_doc_observation: flag an architectural observation for the design document (fields: description, section). Use when you discover something about the system's architecture — module patterns, missing capabilities, structural risks. These are reviewed by the architecture agent, not acted on directly. Max 3 per cycle.
- Optional: /tmp/research_insights_${taskId}.json with [{"summary": "<200 chars", "area": "path"}] for observations not worth implementing.`;

  const researchPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  statusRef.value = 'Running codebase research...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(researchPrompt + researchPromptSuffix + PLAYBOOK_GUIDANCE + '\nNote: If your research uncovered operational knowledge (build patterns, module relationships, common failure modes), save the most reusable findings to the playbook.\n', ws, statusRef, model, effort, disallowedTools);

  // Prefer reading findings from /tmp/ file (avoids output truncation), fall back to output parsing
  const researchFindings = readHandlerOutput(taskId, 'research_findings', ws);
  let logs = researchFindings !== null ? sanitize(JSON.stringify(researchFindings)) : '';
  if (!logs) {
    if (lastResultText && lastResultText.includes('"prompt"')) {
      logs = sanitize(lastResultText);
    } else {
      const jsonMatch = allOutput.match(/\[\s*\{[\s\S]*"prompt"[\s\S]*\}\s*\]/);
      logs = sanitize(jsonMatch ? jsonMatch[0] : (lastResultText || allOutput.slice(-50_000)));
    }
  }

  try {
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
      ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : { artifact_type: 'RESEARCH_FINDINGS' }),
      logs,
    });
  } catch (e) {
    sendLog(ws, `[ERROR] Failed to post research completion status: ${e}\n`);
  }
}
