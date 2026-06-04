import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE,
  strVal,
  sanitize,
  sendLog,
  validateBranchName,
  persistAndPostStatus,
  git,
  runClaudeCode,
  sendHeartbeatSync,
  setCurrentPromptFingerprint,
  snapshotToolTrace,
  extractInvestigatedFiles,
  resolveBaseBranch,
  readHandlerOutput,
} from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import {
  DEFAULT_TOOL_GUIDANCE,
  configureAgentPermissions,
  computePromptFingerprint,
  formatPlaybook,
  formatProjectContext,
  readClaudeMd,
  writeMessagesHelper,
  writePlaybookHelper,
  type PlaybookEntry,
} from '../prompt.js';
import { resolveBuildConfig } from '../build-config.js';
import type { BuildConfig } from '../index.js';
import {
  buildReviewChecklistSection,
  buildReviewDiffAccessHint,
  buildReviewMasterPreamble,
  buildReviewSeverityGuideSection,
} from '../prompts/review-prompt.js';

const PANEL_REVIEW_OUTPUT_TYPE = 'panel_review_verdict';

export const PANEL_REVIEW_LENSES = [
  {
    id: 'correctness',
    label: 'Correctness',
    focus:
      'Logic, data flow, edge cases, null/undefined handling, async ordering, and caller compatibility.',
  },
  {
    id: 'security',
    label: 'Security',
    focus:
      'Input validation, authz/authn drift, secret exposure, injection risks, and unsafe shell/file handling.',
  },
  {
    id: 'code_quality',
    label: 'Code Quality',
    focus:
      'Cross-file contract consistency, simplification opportunities, maintainability, and obvious performance waste.',
  },
  {
    id: 'test_coverage',
    label: 'Test Coverage',
    focus:
      'Whether the changed behavior is exercised specifically enough, including meaningful assertions and regression coverage.',
  },
] as const;

type PanelReviewLensId = (typeof PANEL_REVIEW_LENSES)[number]['id'];

type PanelReviewIssue = {
  severity?: string;
  description: string;
  file?: string;
  line?: number;
  lens?: PanelReviewLensId;
};

type PanelReviewLensVerdict = {
  verdict: 'APPROVE' | 'REQUEST_CHANGES';
  summary: string;
  issues: PanelReviewIssue[];
};

type PanelReviewVerdict = {
  overall_verdict: 'APPROVE' | 'REQUEST_CHANGES';
  summary: string;
  issues: PanelReviewIssue[];
  investigated_files: string[];
  lenses: Record<PanelReviewLensId, PanelReviewLensVerdict>;
};

function normalizeIssue(raw: unknown, lens: PanelReviewLensId): PanelReviewIssue | null {
  if (!raw || typeof raw !== 'object') return null;
  const issue = raw as Record<string, unknown>;
  const description = strVal(issue.description).trim();
  if (!description) return null;

  const normalized: PanelReviewIssue = { description, lens };
  const severity = strVal(issue.severity).trim().toLowerCase();
  if (severity) normalized.severity = severity;
  const file = strVal(issue.file).trim();
  if (file) normalized.file = file;
  const line = typeof issue.line === 'number' ? issue.line : Number(issue.line);
  if (Number.isInteger(line) && line > 0) normalized.line = line;
  return normalized;
}

function normalizeLensVerdict(raw: unknown, lens: PanelReviewLensId): PanelReviewLensVerdict | null {
  if (!raw || typeof raw !== 'object') return null;
  const payload = raw as Record<string, unknown>;
  const verdict = strVal(payload.verdict).trim();
  const summary = strVal(payload.summary || payload.feedback || payload.reasoning).trim();
  if ((verdict !== 'APPROVE' && verdict !== 'REQUEST_CHANGES') || !summary) {
    return null;
  }
  const issues = Array.isArray(payload.issues)
    ? payload.issues
      .map(issue => normalizeIssue(issue, lens))
      .filter((issue): issue is PanelReviewIssue => issue !== null)
    : [];
  return {
    verdict,
    summary,
    issues,
  };
}

function dedupeIssues(issues: PanelReviewIssue[]): PanelReviewIssue[] {
  const seen = new Set<string>();
  const deduped: PanelReviewIssue[] = [];
  for (const issue of issues) {
    const key = [
      issue.lens ?? '',
      issue.severity ?? '',
      issue.file ?? '',
      issue.line ?? '',
      issue.description,
    ].join('::');
    if (seen.has(key)) continue;
    seen.add(key);
    deduped.push(issue);
  }
  return deduped;
}

function normalizePanelReviewVerdict(
  raw: unknown,
  investigatedFiles: string[],
): PanelReviewVerdict | null {
  if (!raw || typeof raw !== 'object') return null;
  const payload = raw as Record<string, unknown>;
  const overallVerdict = strVal(payload.overall_verdict).trim();
  if (overallVerdict !== 'APPROVE' && overallVerdict !== 'REQUEST_CHANGES') return null;
  const summary = strVal(payload.summary || payload.feedback).trim();
  if (!summary) return null;

  const rawLenses = payload.lenses;
  if (!rawLenses || typeof rawLenses !== 'object') return null;
  const lensPayloads = rawLenses as Record<string, unknown>;

  const normalizedLenses = {} as Record<PanelReviewLensId, PanelReviewLensVerdict>;
  for (const lens of PANEL_REVIEW_LENSES) {
    const normalized = normalizeLensVerdict(lensPayloads[lens.id], lens.id);
    if (!normalized) return null;
    normalizedLenses[lens.id] = normalized;
  }

  const issuePool = Array.isArray(payload.issues)
    ? payload.issues
      .map(issue => {
        const explicitLens = strVal((issue as Record<string, unknown> | undefined)?.lens).trim() as PanelReviewLensId;
        const fallbackLens = PANEL_REVIEW_LENSES.some(lens => lens.id === explicitLens)
          ? explicitLens
          : undefined;
        return normalizeIssue(issue, fallbackLens ?? 'correctness');
      })
      .filter((issue): issue is PanelReviewIssue => issue !== null)
    : [];
  const flattenedLensIssues = PANEL_REVIEW_LENSES.flatMap(lens => normalizedLenses[lens.id].issues);
  const issues = dedupeIssues(issuePool.length > 0 ? [...issuePool, ...flattenedLensIssues] : flattenedLensIssues);

  const declaredInvestigated = Array.isArray(payload.investigated_files)
    ? payload.investigated_files.filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
    : [];
  const investigated = Array.from(new Set([...declaredInvestigated, ...investigatedFiles])).slice(0, 50);

  return {
    overall_verdict: overallVerdict,
    summary,
    issues,
    investigated_files: investigated,
    lenses: normalizedLenses,
  };
}

function buildPanelReviewLensSection(): string {
  return PANEL_REVIEW_LENSES.map((lens, index) => (
    `${index + 1}. ${lens.label} (\`${lens.id}\`)\n` +
    `   Focus: ${lens.focus}\n` +
    '   Deliver: one verdict, one short summary, and zero or more substantiated issues.\n'
  )).join('\n');
}

function buildPanelReviewPrompt(args: {
  taskId: string;
  prNumber: number;
  branch: string;
  baseBranch: string;
  changedFiles: string;
  diffShortstat: string;
  originalPrompt: string;
  prTitle: string;
  prBody: string;
  projectContext: string;
  playbookSection: string;
  buildConfig?: BuildConfig;
  claudeMdPrefix: string;
}): string {
  const {
    taskId,
    prNumber,
    branch,
    baseBranch,
    changedFiles,
    diffShortstat,
    originalPrompt,
    prTitle,
    prBody,
    projectContext,
    playbookSection,
    buildConfig,
    claudeMdPrefix,
  } = args;
  const lensSection = buildPanelReviewLensSection();
  return `${claudeMdPrefix}${playbookSection}${projectContext}${buildReviewMasterPreamble(prNumber, branch, baseBranch)}

This is the fixed \`panel_review\` workflow. Do not collapse this into a normal single-pass review.
Run the declared lenses one at a time from clean perspectives, then synthesize one structured verdict object.

${originalPrompt ? `Original task prompt: ${originalPrompt}\n\n` : ''}${prTitle ? `PR title: ${prTitle}\n` : ''}${prBody ? `PR description:\n${prBody}\n\n` : ''}Files changed in this PR:
${changedFiles}${diffShortstat ? `\nDiff volume: ${diffShortstat}\n` : ''}

Declared review lenses:
${lensSection}

Workflow rules:
- Read the diff and any surrounding files you need with \`git diff origin/${baseBranch}...HEAD\` and targeted file reads.
- Keep the lens passes separate. Do not let one lens summary stand in for another.
- Use severities \`critical\`, \`major\`, or \`minor\`.
- Only include substantiated issues with a concrete description. Prefer one primary lens per issue; avoid duplicate issue spam across lenses.
- A lens should return \`REQUEST_CHANGES\` only for blocking defects inside that lens's scope.
- Set \`overall_verdict\` to \`REQUEST_CHANGES\` when any lens returns \`REQUEST_CHANGES\`; otherwise \`APPROVE\`.
- Always populate every declared lens key even when there are no issues. Use an empty \`issues\` array when clean.
- Track the files you actually inspected. Include them in \`investigated_files\`.

${buildReviewDiffAccessHint(baseBranch)}

${buildReviewChecklistSection(buildConfig, baseBranch)}

${buildReviewSeverityGuideSection()}

After finishing all lens passes, write EXACTLY ONE JSON object with this schema:
\`\`\`json
{
  "overall_verdict": "APPROVE|REQUEST_CHANGES",
  "summary": "Short synthesis of the overall panel result.",
  "issues": [
    {
      "severity": "critical|major|minor",
      "description": "What blocks or why it is notable.",
      "file": "path/to/file.ts",
      "line": 123,
      "lens": "correctness|security|code_quality|test_coverage"
    }
  ],
  "investigated_files": ["path/you/read.ts"],
  "lenses": {
    "correctness": {
      "verdict": "APPROVE|REQUEST_CHANGES",
      "summary": "Per-lens synthesis.",
      "issues": []
    },
    "security": {
      "verdict": "APPROVE|REQUEST_CHANGES",
      "summary": "Per-lens synthesis.",
      "issues": []
    },
    "code_quality": {
      "verdict": "APPROVE|REQUEST_CHANGES",
      "summary": "Per-lens synthesis.",
      "issues": []
    },
    "test_coverage": {
      "verdict": "APPROVE|REQUEST_CHANGES",
      "summary": "Per-lens synthesis.",
      "issues": []
    }
  }
}
\`\`\`

IMPORTANT:
- Also write that JSON object to /tmp/${PANEL_REVIEW_OUTPUT_TYPE}_${taskId}.json.
- The file must contain ONLY the JSON object, with no markdown fence or commentary.
- Do not write to /tmp/review_verdict_${taskId}.json; this task has its own structured verdict channel.

${DEFAULT_TOOL_GUIDANCE}`;
}

export async function handlePanelReview(
  taskId: string,
  payload: Record<string, unknown>,
  task: Record<string, unknown>,
  ghToken: string,
  owner: string,
  repo: string,
  ws: WebSocket | null,
  statusRef: { value: string },
  model?: string,
  effort?: string,
) {
  const prNumber = Number(payload.pr_number);
  if (!Number.isInteger(prNumber) || prNumber <= 0) throw new Error(`Invalid pr_number: ${String(payload.pr_number)}`);
  const branch = validateBranchName(strVal(payload.branch), 'branch');
  const baseBranch = validateBranchName(resolveBaseBranch(payload, task), 'base_branch');
  const originalPrompt = strVal(payload.original_prompt);

  statusRef.value = `Cloning ${owner}/${repo} (panel review PR #${prNumber})...`;
  sendLog(ws, `Running panel review on PR #${prNumber} (${branch} -> ${baseBranch})\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;

  let reviewBranchExists = false;
  try {
    const lsOut = execFileSync('git', ['ls-remote', '--heads', cloneUrl, branch], { timeout: 30000 });
    reviewBranchExists = lsOut.toString().trim().length > 0;
  } catch {
    reviewBranchExists = true;
  }
  if (!reviewBranchExists) {
    const skipMsg = `PR head branch '${branch}' no longer exists on ${owner}/${repo} — likely merged and auto-deleted. Skipping panel review.`;
    sendLog(ws, skipMsg + '\n');
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'COMPLETED',
      artifact_type: 'ERROR_LOG',
      logs: skipMsg,
      failure_category: 'PR_BRANCH_DELETED',
    });
    return;
  }

  await prepareWorkspaceForRole({
    workspace: WORKSPACE,
    repoUrl: cloneUrl,
    githubToken: ghToken,
    branch,
    baseBranch,
    depth: DEFAULT_CLONE_DEPTH,
  });
  try {
    git(['fetch', 'origin', `+refs/heads/${baseBranch}:refs/remotes/origin/${baseBranch}`, '--depth', String(DEFAULT_CLONE_DEPTH)]);
  } catch (error) {
    sendLog(ws, `WARNING: Failed to fetch origin/${baseBranch} for diff comparison: ${String(error)}\n`);
  }

  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('panel_review');
  const buildConfig = resolveBuildConfig(task);

  let prTitle = '';
  let prBody = '';
  try {
    const prRes = await fetch(`https://api.github.com/repos/${owner}/${repo}/pulls/${prNumber}`, {
      headers: { Authorization: `token ${ghToken}`, Accept: 'application/vnd.github+json' },
    });
    if (prRes.ok) {
      const prJson = await prRes.json() as Record<string, unknown>;
      prTitle = strVal(prJson.title);
      prBody = strVal(prJson.body);
    }
  } catch {
    // Best effort only.
  }

  let changedFiles = '';
  let diffShortstat = '';
  try {
    changedFiles = sanitize(git(['diff', '--name-only', `origin/${baseBranch}...HEAD`]).trim());
  } catch {
    changedFiles = '';
  }
  try {
    diffShortstat = sanitize(git(['diff', '--shortstat', `origin/${baseBranch}...HEAD`]).trim());
  } catch {
    diffShortstat = '';
  }

  const changedFileList = changedFiles
    .split('\n')
    .map(line => line.trim())
    .filter(Boolean);

  const playbookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const taskContext = task.context as Record<string, unknown> | undefined;
  const projectContextState = task.project_context as Record<string, unknown> | undefined;
  const projectContext = projectContextState
    ? formatProjectContext(
      projectContextState,
      'panel_review',
      strVal(taskContext?.scope, 'medium'),
      strVal(taskContext?.direction_id),
      Array.isArray(payload.related_files) ? payload.related_files.length : 0,
    )
    : '';
  const claudeMd = readClaudeMd();
  const claudeMdPrefix = claudeMd ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const prompt = buildPanelReviewPrompt({
    taskId,
    prNumber,
    branch,
    baseBranch,
    changedFiles,
    diffShortstat,
    originalPrompt,
    prTitle,
    prBody,
    projectContext: projectContext ? `${projectContext}\n\n` : '',
    playbookSection,
    buildConfig,
    claudeMdPrefix,
  });

  statusRef.value = 'Running fixed panel review lenses...';
  const promptFingerprint = computePromptFingerprint('panel_review', buildConfig);
  setCurrentPromptFingerprint(promptFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });

  const { exitCode, allOutput, lastResultText } = await runClaudeCode(
    prompt,
    ws,
    statusRef,
    model,
    effort,
    disallowedTools,
  );

  const investigatedFiles = extractInvestigatedFiles(snapshotToolTrace());
  const fileVerdict = readHandlerOutput(taskId, PANEL_REVIEW_OUTPUT_TYPE, ws);
  const normalizedVerdict = normalizePanelReviewVerdict(fileVerdict, investigatedFiles);

  let logs = '';
  if (normalizedVerdict) {
    logs = JSON.stringify(normalizedVerdict);
    sendLog(ws, `Panel review verdict read from file: ${normalizedVerdict.overall_verdict}\n`);
  } else {
    logs = sanitize(lastResultText || allOutput.slice(-50_000));
  }

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : {}),
    logs,
    ...(exitCode !== 0 ? { failure_category: 'claude_code_failed' } : {}),
    ...(changedFileList.length > 0 ? { changed_files: changedFileList.slice(0, 50) } : {}),
  });
}
