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

function panelReviewLensOutputType(lensId: PanelReviewLensId): string {
  return `panel_review_lens_${lensId}`;
}

/**
 * Code-side synthesis of the per-lens agent passes into the single structured
 * verdict object the backend evaluator + completion handler already consume.
 *
 * Determinism is the point: each lens runs as its OWN agent pass, but the
 * aggregation is plain TypeScript so the emitted shape is guaranteed to satisfy
 * the backend's required-lens + non-empty-summary gate. overall_verdict is
 * REQUEST_CHANGES whenever ANY lens requests changes (fail-toward-blocking), so
 * the panel cannot silently approve when one lens found a blocker.
 */
function synthesizePanelReviewVerdict(
  lensVerdicts: Record<PanelReviewLensId, PanelReviewLensVerdict>,
  investigatedFiles: string[],
): PanelReviewVerdict {
  const lensIds = PANEL_REVIEW_LENSES.map(lens => lens.id);
  const overallVerdict: 'APPROVE' | 'REQUEST_CHANGES' = lensIds.some(
    id => lensVerdicts[id].verdict === 'REQUEST_CHANGES',
  )
    ? 'REQUEST_CHANGES'
    : 'APPROVE';

  const summary = PANEL_REVIEW_LENSES
    .map(lens => `${lens.label} [${lensVerdicts[lens.id].verdict}]: ${lensVerdicts[lens.id].summary}`)
    .join('\n');

  const issues = dedupeIssues(lensIds.flatMap(id => lensVerdicts[id].issues));
  const investigated = Array.from(new Set(investigatedFiles)).slice(0, 50);

  return {
    overall_verdict: overallVerdict,
    summary,
    issues,
    investigated_files: investigated,
    lenses: lensVerdicts,
  };
}

function buildPanelReviewLensPrompt(args: {
  lens: (typeof PANEL_REVIEW_LENSES)[number];
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
    lens,
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
  const outputPath = `/tmp/${panelReviewLensOutputType(lens.id)}_${taskId}.json`;
  return `${claudeMdPrefix}${playbookSection}${projectContext}${buildReviewMasterPreamble(prNumber, branch, baseBranch)}

This is ONE independent pass of the fixed \`panel_review\` multi-agent workflow.
You are the **${lens.label}** reviewer (lens id: \`${lens.id}\`). Other lenses run as
SEPARATE agent passes — do NOT review outside your lens, and do NOT try to produce
an overall panel verdict. Judge this PR solely through your lens.

Your lens focus: ${lens.focus}

${originalPrompt ? `Original task prompt: ${originalPrompt}\n\n` : ''}${prTitle ? `PR title: ${prTitle}\n` : ''}${prBody ? `PR description:\n${prBody}\n\n` : ''}Files changed in this PR:
${changedFiles}${diffShortstat ? `\nDiff volume: ${diffShortstat}\n` : ''}

Rules for your lens pass:
- Read the diff and any surrounding files you need with \`git diff origin/${baseBranch}...HEAD\` and targeted file reads.
- Use severities \`critical\`, \`major\`, or \`minor\`.
- Only include substantiated issues with a concrete description, scoped to your lens.
- Return \`REQUEST_CHANGES\` only for a blocking defect inside your lens's scope; otherwise \`APPROVE\`.
- Use an empty \`issues\` array when your lens is clean.

${buildReviewDiffAccessHint(baseBranch)}

${buildReviewChecklistSection(buildConfig, baseBranch)}

${buildReviewSeverityGuideSection()}

After your lens pass, write EXACTLY ONE JSON object with this schema to ${outputPath}:
\`\`\`json
{
  "verdict": "APPROVE|REQUEST_CHANGES",
  "summary": "Short synthesis of your ${lens.label} lens result.",
  "issues": [
    {
      "severity": "critical|major|minor",
      "description": "What blocks or why it is notable.",
      "file": "path/to/file.ts",
      "line": 123
    }
  ]
}
\`\`\`

IMPORTANT:
- Write the JSON object to ${outputPath} (NOT in the repository).
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

  const promptFingerprint = computePromptFingerprint('panel_review', buildConfig);
  setCurrentPromptFingerprint(promptFingerprint);

  // Genuine multi-agent fan-out: each declared lens runs as its OWN independent
  // runClaudeCode pass from a clean single-lens perspective over the same PR
  // diff. A code-side synthesis then aggregates the per-lens verdicts into the
  // single structured object the backend gate consumes. Fan-out is bounded to
  // the four declared lenses (no unbounded recursion). Fail closed: if any lens
  // pass crashes (exitCode!==0) or yields no normalizable verdict, the whole
  // panel FAILS — overall_verdict must never claim success when a lens never
  // produced a real verdict.
  const lensVerdicts = {} as Record<PanelReviewLensId, PanelReviewLensVerdict>;
  for (const [index, lens] of PANEL_REVIEW_LENSES.entries()) {
    statusRef.value = `Panel review lens ${index + 1}/${PANEL_REVIEW_LENSES.length}: ${lens.label}...`;
    sendLog(ws, `Running panel review lens '${lens.id}' (${index + 1}/${PANEL_REVIEW_LENSES.length})\n`);
    sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });

    const lensPrompt = buildPanelReviewLensPrompt({
      lens,
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

    const { exitCode, allOutput, lastResultText } = await runClaudeCode(
      lensPrompt,
      ws,
      statusRef,
      model,
      effort,
      disallowedTools,
    );

    const rawLensVerdict = readHandlerOutput(taskId, panelReviewLensOutputType(lens.id), ws);
    const normalizedLens = normalizeLensVerdict(rawLensVerdict, lens.id);
    if (exitCode !== 0 || !normalizedLens) {
      const reason = exitCode !== 0
        ? `lens '${lens.id}' agent pass exited ${exitCode}`
        : `lens '${lens.id}' produced no usable verdict`;
      sendLog(ws, `Panel review failing closed: ${reason}\n`);
      const failLogs = sanitize(
        `Panel review fan-out failed: ${reason}.\n` +
        (lastResultText || allOutput.slice(-20_000)),
      );
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: 'FAILED',
        artifact_type: 'ERROR_LOG',
        logs: failLogs,
        failure_category: 'panel_review_lens_failed',
        ...(changedFileList.length > 0 ? { changed_files: changedFileList.slice(0, 50) } : {}),
      });
      return;
    }
    lensVerdicts[lens.id] = normalizedLens;
  }

  const investigatedFiles = extractInvestigatedFiles(snapshotToolTrace());
  const synthesized = synthesizePanelReviewVerdict(lensVerdicts, investigatedFiles);
  const logs = JSON.stringify(synthesized);
  sendLog(ws, `Panel review synthesized verdict: ${synthesized.overall_verdict}\n`);

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: 'COMPLETED',
    logs,
    ...(changedFileList.length > 0 ? { changed_files: changedFileList.slice(0, 50) } : {}),
  });
}
