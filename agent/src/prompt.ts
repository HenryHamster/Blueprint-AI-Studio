import fs from 'fs';
import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import { sendLog, WORKSPACE } from './index.js';

// ── Agent Permission Enforcement ────────────────────────────────────────
// Write .claude/settings.json in the workspace to restrict what the inner
// Claude Code agent can do.  Each task type gets appropriate permissions.

const READ_ONLY_TYPES = new Set([
  'research', 'plan', 'review', 'security_audit', 'smoketest',
  'debug', 'perf', 'investigate', 'design_doc_update', 'strategic_review',
]);

// Write types get no restrictions (default behavior). Listed for documentation:
// create, fix, refactor, docs, qa, deps, migrate

/**
 * Configure Claude Code permissions for the current task type.
 * Returns a list of disallowed tools to pass to runClaudeCode's --disallowedTools flag.
 * Also installs git hooks to block commit/push for read-only agents.
 * Call this AFTER cloning the repo but BEFORE running Claude Code.
 */
export function configureAgentPermissions(taskType: string): string[] {
  if (READ_ONLY_TYPES.has(taskType)) {
    // Strip GitHub token from git remote so the agent can't push
    try {
      execFileSync('git', ['remote', 'set-url', 'origin', 'https://github.com/readonly/readonly.git'], { cwd: WORKSPACE });
    } catch { /* git dir may not exist yet */ }

    // Block git commit via pre-commit hook (backup enforcement)
    const hooksDir = `${WORKSPACE}/.git/hooks`;
    try {
      fs.mkdirSync(hooksDir, { recursive: true });
      const blockScript = '#!/bin/sh\necho "ERROR: This is a read-only analysis task. Do NOT commit or push." >&2\nexit 1\n';
      fs.writeFileSync(`${hooksDir}/pre-commit`, blockScript, { mode: 0o755 });
      fs.writeFileSync(`${hooksDir}/pre-push`, blockScript, { mode: 0o755 });
    } catch { /* hooks dir may not exist before clone completes */ }

    return ['Write', 'Edit', 'NotebookEdit'];
  }
  // Write types: no restrictions
  return [];
}

function strVal(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback;
}

// ── Task Type Hints (appended to prompts for known task types) ───────────
export const TASK_TYPE_HINTS: Record<string, string> = {
  'qa': 'Focus on writing tests. Run the existing test suite first to understand patterns. Do not modify production code unless fixing a test setup issue.',
  'migrate': 'This is a database migration task. Check existing Alembic migrations in backend/alembic/versions/ for naming conventions. Use `alembic revision --autogenerate` as a starting point only — always review and clean up generated migrations.',
  'security_audit': 'Focus on identifying security vulnerabilities. Do not implement fixes — create a detailed findings report as your output.',
  'perf': 'Profile before optimizing. Identify the specific bottleneck with measurements before writing any fix.',
  'fix': 'You are fixing issues from a code review. Read ALL review feedback carefully and address every point. Run the relevant test suite to confirm your fix does not introduce regressions. Verify each feedback item is resolved before committing.',
  'docs': 'Read the existing README and documentation files first. Understand the current documentation structure before making changes.',
  'deps': 'Check for breaking changes in major version bumps. Run the full test suite after updating to verify compatibility.',
  'refactor': 'Run the full test suite BEFORE and AFTER changes. Zero test regressions allowed. Do not change external behavior.',
  'investigate': 'This is a read-only investigation. Do NOT modify any files. Write your findings to the output JSON only.',
};

export const DEFAULT_TOOL_GUIDANCE = 'Tool guidance: Use Grep/Glob for code search (not Bash grep/find), Read for file contents, Agent tool for parallel exploration of multiple areas. Use TodoWrite to track multi-step work.';

// ── Shared helper: read CLAUDE.md from workspace ────────────────────────────
export function readClaudeMd(): string {
  const claudeMdPath = `${WORKSPACE}/CLAUDE.md`;
  if (fs.existsSync(claudeMdPath)) {
    return fs.readFileSync(claudeMdPath, 'utf-8').slice(0, 2000);
  }
  return '';
}

// ── Shared prompt assembly helpers ──────────────────────────────────────
export function buildContextPrefix(task: Record<string, unknown>): string {
  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  if (!projectCtx) return '';
  const hasData = Object.keys(projectCtx).some(k => {
    const v = projectCtx[k]; return Array.isArray(v) && v.length > 0;
  });
  return hasData ? formatProjectContext(projectCtx) + '\n\n' : '';
}

export function buildFileTree(ws: WebSocket | null): string {
  try {
    // Get files ranked by recent git activity
    let recentFiles: string[] = [];
    try {
      const gitLog = execFileSync('git', ['log', '--pretty=format:', '--name-only', '-100'],
        { cwd: WORKSPACE, encoding: 'utf-8' });
      const counts: Record<string, number> = {};
      for (const f of gitLog.split('\n').filter(Boolean)) {
        counts[f] = (counts[f] ?? 0) + 1;
      }
      recentFiles = Object.entries(counts)
        .sort((a, b) => b[1] - a[1])
        .map(([f]) => f);
    } catch { /* fallback to unsorted */ }

    // Get all tracked files
    const allFiles = execFileSync('git', ['ls-files'],
      { cwd: WORKSPACE, encoding: 'utf-8' })
      .split('\n')
      .filter(f => f && !f.includes('node_modules/') && !f.includes('dist/'));

    // Merge: recent files first, then remaining alphabetically
    const recentSet = new Set(recentFiles);
    const remaining = allFiles.filter(f => !recentSet.has(f)).sort();
    const sorted = [...recentFiles.filter(f => allFiles.includes(f)), ...remaining];
    const tree = sorted.slice(0, 150).map(f => './' + f).join('\n');

    return tree
      ? `## Repository File Tree (first 150 files, sorted by recent activity)\n\`\`\`\n${tree}\n\`\`\`\n\n`
      : '';
  } catch (e) {
    sendLog(ws, `Warning: file tree generation failed: ${String(e)}\n`);
    return '';
  }
}

export function discoverRelatedTests(ws: WebSocket | null, relatedFiles: string[]): string {
  if (relatedFiles.length === 0) return '';

  try {
    const testEntries: string[] = [];
    const seen = new Set<string>();

    const extractTestNames = (content: string): string[] => {
      const names: string[] = [];
      for (const line of content.split('\n')) {
        const m = line.match(/^\s*(?:def (test_\w+)|it\(['"`]([^'"`]+)['"`]|test\(['"`]([^'"`]+)['"`]|describe\(['"`]([^'"`]+)['"`])/);
        if (m) names.push(m[1] || m[2] || m[3] || m[4]);
      }
      return names;
    };

    const processTestFile = (testPath: string) => {
      const full = `${WORKSPACE}/${testPath}`;
      if (seen.has(testPath) || !fs.existsSync(full)) return;
      seen.add(testPath);
      try {
        const content = fs.readFileSync(full, 'utf-8').split('\n').slice(0, 80).join('\n');
        const names = extractTestNames(content);
        if (names.length > 0) {
          testEntries.push(`- ${testPath}: ${names.slice(0, 10).join(', ')}`);
        } else {
          testEntries.push(`- ${testPath}`);
        }
      } catch { /* skip unreadable */ }
    };

    for (const relFile of relatedFiles) {
      // Convention-based discovery
      if (relFile.startsWith('backend/') && relFile.endsWith('.py')) {
        const base = relFile.replace(/^backend\/app\//, '').replace(/\.py$/, '');
        const candidate = `backend/testing/test_${base.split('/').pop()}.py`;
        processTestFile(candidate);
      } else if (relFile.match(/^admin-ui\/.*\.(tsx?|jsx?)$/) && !relFile.includes('.test.')) {
        const testPath = relFile.replace(/\.(tsx?|jsx?)$/, '.test.$1');
        processTestFile(testPath);
      } else if (relFile.startsWith('agent/')) {
        // Search for any test files in agent/
        try {
          const agentTestFiles = execFileSync('git', ['ls-files', 'agent/'],
            { cwd: WORKSPACE, encoding: 'utf-8' })
            .split('\n')
            .filter(f => f.includes('.test.') || f.includes('test_') || f.includes('/tests/'));
          for (const tf of agentTestFiles) processTestFile(tf);
        } catch { /* skip */ }
      }

      // Grep-based discovery: find test files importing from this module
      try {
        const moduleName = relFile.split('/').pop()?.replace(/\.(tsx?|jsx?|py)$/, '') || '';
        if (moduleName) {
          const grepResult = execFileSync('git', ['grep', '-l', moduleName, '--', '*test*'],
            { cwd: WORKSPACE, encoding: 'utf-8' });
          for (const tf of grepResult.split('\n').filter(Boolean)) {
            processTestFile(tf);
          }
        }
      } catch { /* no matches */ }
    }

    if (testEntries.length === 0) return '';

    const output = `## Related Tests\nThese tests cover files you'll be modifying:\n${testEntries.join('\n')}\n\n`;
    return output.slice(0, 2000);
  } catch (e) {
    sendLog(ws, `Warning: related test discovery failed: ${String(e)}\n`);
    return '';
  }
}

function parseResearchSummary(raw: string): string {
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const items = Array.isArray(parsed) ? parsed : (Array.isArray(parsed.logs) ? parsed.logs : []);
    if (Array.isArray(items) && items.length > 0) {
      return (items as Record<string, unknown>[]).slice(0, 3)
        .map((f) => `${strVal(f.title, '?')}: ${(strVal(f.description) || strVal(f.prompt)).slice(0, 120)}`)
        .join(' | ');
    }
  } catch { /* ignore */ }
  return raw.slice(0, 400);
}

// ── Shared: Format project_context into a markdown section ────────────────
export function formatProjectContext(ctx: Record<string, unknown>): string {
  const CONTEXT_BUDGET = 8000;

  const header = [
    '## Project Context',
    'The following tasks are already active or completed in this project. Avoid duplicating their work.',
    '',
  ];

  // Build active tasks section (always included in full)
  const activeLines: string[] = [];
  const activeTasks = ctx.active_tasks as Array<Record<string, unknown>> | undefined;
  if (activeTasks && activeTasks.length > 0) {
    activeLines.push('### Active Tasks');
    for (const t of activeTasks) {
      const files = (t.related_files as string[] | undefined);
      const filesSuffix = files && files.length > 0 ? ` (files: ${files.join(', ')})` : '';
      activeLines.push(`- [${strVal(t.task_type)}] **${strVal(t.title)}** (${strVal(t.status)}) — ${strVal(t.prompt).slice(0, 400)}${filesSuffix}`);
    }
    activeLines.push('');
  }

  // Build open PRs section (always included in full)
  const prLines: string[] = [];
  const openPRs = ctx.open_prs as string[] | undefined;
  if (openPRs && openPRs.length > 0) {
    prLines.push('### Open PRs');
    for (const pr of openPRs) {
      prLines.push(`- ${pr}`);
    }
    prLines.push('');
  }

  // Build sibling tasks section (always included in full)
  const siblingLines: string[] = [];
  const siblingTasks = ctx.sibling_tasks as Array<Record<string, unknown>> | undefined;
  if (siblingTasks && siblingTasks.length > 0) {
    siblingLines.push('## Sibling Tasks (parallel tasks from the same parent — avoid duplicating their work)');
    for (const t of siblingTasks) {
      const files = (t.related_files as string[] | undefined);
      const filesSuffix = files && files.length > 0 ? ` (files: ${files.join(', ')})` : '';
      siblingLines.push(`- [${strVal(t.status)}] ${strVal(t.title)}${filesSuffix}`);
    }
    siblingLines.push('');
  }

  const fixedContent = [...header, ...activeLines, ...prLines, ...siblingLines].join('\n');
  let budget = CONTEXT_BUDGET - fixedContent.length;

  // Build queued tasks section — fall back to one-liners if over budget
  const queuedTasks = ctx.queued_tasks as Array<Record<string, unknown>> | undefined;
  let queuedLines: string[] = [];
  if (queuedTasks && queuedTasks.length > 0) {
    const fullLines: string[] = ['### Queued Tasks'];
    for (const t of queuedTasks) {
      fullLines.push(`- [${strVal(t.task_type)}] **${strVal(t.title)}** — ${strVal(t.prompt).slice(0, 400)}`);
    }
    fullLines.push('');
    const fullContent = fullLines.join('\n');

    if (fullContent.length <= budget) {
      queuedLines = fullLines;
    } else {
      // Reduce to one-liners
      const shortLines: string[] = ['### Queued Tasks'];
      for (const t of queuedTasks) {
        shortLines.push(`- [${strVal(t.task_type)}] **${strVal(t.title)}**`);
      }
      shortLines.push('');
      queuedLines = shortLines;
    }
    budget -= queuedLines.join('\n').length;
  }

  // Build recently completed section — drop oldest entries to fit budget
  const recentTasks = ctx.recently_completed_tasks as Array<Record<string, unknown>> | undefined;
  let recentLines: string[] = [];
  let recentDropped = 0;
  if (recentTasks && recentTasks.length > 0) {
    const allEntries: string[] = [];
    for (const t of recentTasks) {
      const pr = t.pr_url ? ` (${strVal(t.pr_url)})` : '';
      const summary = t.research_findings_summary ? ` — ${parseResearchSummary(strVal(t.research_findings_summary))}` : '';
      const failureClass = t.failure_class ? ` [${strVal(t.failure_class)}]` : '';
      const failureSuffix = t.failure_reason ? ` (FAILED${failureClass}: ${strVal(t.failure_reason)})` : '';
      const reviewSuffix = (typeof t.review_iterations === 'number' && t.review_iterations > 1) ? ` (${t.review_iterations} review cycles)` : '';
      allEntries.push(`- [${strVal(t.task_type)}] **${strVal(t.title)}**${pr}${summary}${failureSuffix}${reviewSuffix}`);
    }

    // Keep most recent entries that fit within budget
    const headerLine = '### Recently Completed Tasks';
    const kept = allEntries.slice(); // most recent assumed to be last
    while (kept.length > 0) {
      const candidate = [headerLine, ...kept, ''].join('\n');
      if (candidate.length <= budget) break;
      kept.shift(); // drop oldest (first entry)
      recentDropped++;
    }
    if (kept.length > 0) {
      recentLines = [headerLine, ...kept, ''];
      budget -= recentLines.join('\n').length;
    } else {
      recentDropped = allEntries.length;
    }
  }

  // Build lessons section — only if budget allows
  const lessonsData = ctx.lessons as Array<Record<string, unknown>> | undefined;
  let lessonsLines: string[] = [];
  if (lessonsData && lessonsData.length > 0) {
    const candidateLines: string[] = ['### Lessons from Past Reviews'];
    for (const l of lessonsData) {
      const fcSuffix = l.failure_class ? ` [${strVal(l.failure_class)}]` : '';
      candidateLines.push(`- **${strVal(l.task_title)}** (${String(l.iterations)} cycles)${fcSuffix}: ${strVal(l.last_feedback)}`);
    }
    candidateLines.push('');
    const candidateContent = candidateLines.join('\n');
    if (candidateContent.length <= budget) {
      lessonsLines = candidateLines;
      budget -= candidateContent.length;
    }
  }

  // Build pipeline health section — only if budget allows, capped at 400 chars
  const pipelineHealth = ctx.pipeline_health as Record<string, unknown> | null | undefined;
  let healthLines: string[] = [];
  if (pipelineHealth) {
    const parts: string[] = [];

    const successRate = pipelineHealth.success_rate;
    const trend = pipelineHealth.trend as string | undefined;
    if (successRate !== null && successRate !== undefined) {
      const pct = Math.round((successRate as number) * 100);
      const trendStr = trend ? ` (${trend})` : '';
      parts.push(`Success rate: ${pct}%${trendStr}.`);
    }

    const failureDist = pipelineHealth.failure_distribution as Record<string, number> | undefined;
    if (failureDist && Object.keys(failureDist).length > 0) {
      const failStr = Object.entries(failureDist)
        .map(([cls, cnt]) => `${cls} (${cnt})`)
        .join(', ');
      parts.push(`Top failures: ${failStr}.`);
    }

    const dirStats = pipelineHealth.direction_stats as Array<Record<string, unknown>> | undefined;
    if (dirStats && dirStats.length > 0) {
      const dirStr = dirStats
        .map((d) => {
          const did = strVal(d.direction_id).slice(-8);
          const sat = d.possibly_saturated ? ', saturated' : '';
          return `d-${did} merge rate ${d.merge_rate} (${d.cycles} cycles${sat})`;
        })
        .join(', ');
      parts.push(`Direction ROI: ${dirStr}.`);
    }

    if (parts.length > 0) {
      const body = parts.join(' ');
      const candidate = `### Pipeline Health (last 24h)\n${body}\n`;
      if (candidate.length <= 400 && candidate.length <= budget) {
        healthLines = ['### Pipeline Health (last 24h)', body, ''];
      }
    }
  }

  // Append footer if anything was omitted
  const footerLines: string[] = [];
  const omittedCount = recentDropped;
  if (omittedCount > 0) {
    footerLines.push(`_(${omittedCount} more tasks not shown)_`);
    footerLines.push('');
  }

  const allLines = [...header, ...activeLines, ...queuedLines, ...recentLines, ...lessonsLines, ...healthLines, ...prLines, ...siblingLines, ...footerLines];
  return allLines.join('\n');
}
