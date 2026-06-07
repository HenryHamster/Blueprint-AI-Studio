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
  providerSupportsNativeSubagents,
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
import { writeFetchHelper, buildFetchGuidance, PLAN_FETCH_VERBS } from './strategic-helpers.js';

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

type PanelReviewLensBodyArgs = {
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
  buildConfig?: BuildConfig;
  // Read-only ./fetch guidance block (or '' when project_id is absent). Gated so
  // a prompt never advertises a helper that wasn't written into the workspace.
  fetchGuidance: string;
};

/**
 * The per-lens review instructions, independent of any prompt prefix. Shared by
 * BOTH execution paths so a lens reviewer receives byte-for-byte the same brief
 * whether it runs as its own top-level pass (serial fallback) or as a native
 * sub-agent under one orchestrating pass (parallel path). The lens still records
 * its structured verdict through the existing non-Write output channel
 * (a Bash-written /tmp file read back via readHandlerOutput).
 */
function buildPanelReviewLensBody(args: PanelReviewLensBodyArgs): string {
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
    buildConfig,
    fetchGuidance,
  } = args;
  const outputPath = `/tmp/${panelReviewLensOutputType(lens.id)}_${taskId}.json`;
  return `${buildReviewMasterPreamble(prNumber, branch, baseBranch)}

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

${fetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
}

function buildPanelReviewLensPrompt(args: PanelReviewLensBodyArgs & {
  projectContext: string;
  playbookSection: string;
  claudeMdPrefix: string;
}): string {
  const { projectContext, playbookSection, claudeMdPrefix } = args;
  return `${claudeMdPrefix}${playbookSection}${projectContext}${buildPanelReviewLensBody(args)}`;
}

/**
 * Single orchestrating prompt for the native-subagent (parallel) path. It tells
 * the parent to fan the four declared lenses out as parallel sub-agents via the
 * Task tool in ONE message, then wait for all of them. Fan-out is bounded to
 * exactly the four declared lenses (no per-lens sub-fan-out, no extra agents).
 * Each embedded block is the COMPLETE, unmodified per-lens brief, so a sub-agent
 * receives the same instructions a serial pass would, and each writes its own
 * /tmp verdict file that the handler reads back deterministically.
 */
function buildPanelReviewParentPrompt(args: {
  lensArgs: PanelReviewLensBodyArgs[];
  taskId: string;
  prNumber: number;
  branch: string;
  baseBranch: string;
  projectContext: string;
  playbookSection: string;
  claudeMdPrefix: string;
  fetchGuidance: string;
}): string {
  const {
    lensArgs,
    taskId,
    prNumber,
    branch,
    baseBranch,
    projectContext,
    playbookSection,
    claudeMdPrefix,
    fetchGuidance,
  } = args;
  const lensBlocks = lensArgs
    .map((lensArg, index) => {
      const outputPath = `/tmp/${panelReviewLensOutputType(lensArg.lens.id)}_${taskId}.json`;
      return `═══════════ SUB-AGENT ${index + 1} of ${lensArgs.length}: ${lensArg.lens.label} (lens id: \`${lensArg.lens.id}\`) ═══════════
Spawn ONE Task sub-agent whose verdict file is ${outputPath}. Pass the block below as that sub-agent's COMPLETE prompt, verbatim:

${buildPanelReviewLensBody(lensArg)}`;
    })
    .join('\n\n');

  return `${claudeMdPrefix}${playbookSection}${projectContext}You are the ORCHESTRATOR of the fixed \`panel_review\` multi-agent workflow on PR #${prNumber} (${branch} -> ${baseBranch}).

This workflow has EXACTLY ${lensArgs.length} review lenses. Run them IN PARALLEL:
- Spawn all ${lensArgs.length} lenses as independent sub-agents using the Task tool, issuing ALL ${lensArgs.length} Task calls in a SINGLE message so they execute concurrently.
- Do NOT perform any review yourself. Do NOT spawn fewer or more than ${lensArgs.length} sub-agents. Do NOT let any sub-agent spawn further sub-agents.
- Do NOT write, edit, or modify any repository or verdict file yourself — each sub-agent writes its OWN verdict file.
- Wait for ALL ${lensArgs.length} sub-agents to finish before you finish.

${lensBlocks}

When every sub-agent has completed, confirm that all ${lensArgs.length} \`/tmp/panel_review_lens_*_${taskId}.json\` verdict files exist (the deterministic code-side synthesis fails the whole panel closed if any is missing). You are done once all ${lensArgs.length} verdict files are written; do not modify any repository files.

${fetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
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
  // Provision the read-only ./fetch helper (direction|section|playbook) so each
  // lens reviewer can resolve refs trimmed from its launch snapshot. project_id
  // is set for all task types by the backend; if it is absent we provision
  // NOTHING and advertise nothing, so a prompt never promises an unwritten helper.
  const panelProjectId = ((task.project_metadata as Record<string, unknown> | undefined)?.project_id as string | undefined) ?? '';
  if (panelProjectId) writeFetchHelper(taskId, panelProjectId, WORKSPACE, { verbs: PLAN_FETCH_VERBS });
  const panelFetchGuidance = panelProjectId ? buildFetchGuidance(PLAN_FETCH_VERBS) : '';
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

  // Per-lens review brief, shared by both execution paths. Bounded to exactly
  // the four declared lenses (no unbounded recursion, no per-lens sub-fan-out).
  const lensArgs: PanelReviewLensBodyArgs[] = PANEL_REVIEW_LENSES.map(lens => ({
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
    buildConfig,
    fetchGuidance: panelFetchGuidance,
  }));
  const projectContextSection = projectContext ? `${projectContext}\n\n` : '';

  // Fail-closed terminal status, shared by both paths. The aggregated panel must
  // never claim success when a lens crashed or never produced a real verdict.
  const failPanelClosed = async (reason: string, detail: string) => {
    sendLog(ws, `Panel review failing closed: ${reason}\n`);
    const failLogs = sanitize(`Panel review fan-out failed: ${reason}.\n${detail}`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      logs: failLogs,
      failure_category: 'panel_review_lens_failed',
      ...(changedFileList.length > 0 ? { changed_files: changedFileList.slice(0, 50) } : {}),
    });
  };

  const lensVerdicts = {} as Record<PanelReviewLensId, PanelReviewLensVerdict>;

  if (providerSupportsNativeSubagents()) {
    // Parallel path: ONE orchestrating runClaudeCode pass fans the four lenses
    // out as native Task sub-agents that run concurrently, so wall-clock
    // approaches the slowest single lens instead of the sum of four. A single
    // pass also avoids the per-task prompt-scratch race that concurrent
    // same-task OS passes would hit. The background heartbeat keeps the task
    // live through the parallel window; each sub-agent still writes its own
    // verdict file, and synthesis below fails closed on any missing/unusable
    // verdict or non-zero parent exit.
    statusRef.value = `Panel review: fanning out ${PANEL_REVIEW_LENSES.length} lenses in parallel...`;
    sendLog(ws, `Running panel review lenses in parallel via native sub-agent fan-out (${PANEL_REVIEW_LENSES.length} lenses)\n`);
    sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });

    const parentPrompt = buildPanelReviewParentPrompt({
      lensArgs,
      taskId,
      prNumber,
      branch,
      baseBranch,
      projectContext: projectContextSection,
      playbookSection,
      claudeMdPrefix,
      fetchGuidance: panelFetchGuidance,
    });

    const { exitCode, allOutput, lastResultText } = await runClaudeCode(
      parentPrompt,
      ws,
      statusRef,
      model,
      effort,
      disallowedTools,
    );

    if (exitCode !== 0) {
      await failPanelClosed(
        `orchestrating panel pass exited ${exitCode}`,
        lastResultText || allOutput.slice(-20_000),
      );
      return;
    }

    // Read every lens verdict file; fail closed if ANY lens is missing or
    // unusable. All four launched together, so unlike the serial path there is
    // no short-circuit — we still must require all four to be usable.
    for (const lens of PANEL_REVIEW_LENSES) {
      const rawLensVerdict = readHandlerOutput(taskId, panelReviewLensOutputType(lens.id), ws);
      const normalizedLens = normalizeLensVerdict(rawLensVerdict, lens.id);
      if (!normalizedLens) {
        await failPanelClosed(
          `lens '${lens.id}' produced no usable verdict`,
          lastResultText || allOutput.slice(-20_000),
        );
        return;
      }
      lensVerdicts[lens.id] = normalizedLens;
    }
  } else {
    // Serial fallback: providers without a native sub-agent primitive (codex,
    // gemini) can't fan out, so each declared lens runs as its OWN independent
    // runClaudeCode pass in sequence. Same fail-closed guarantees; just slower.
    for (const [index, lens] of PANEL_REVIEW_LENSES.entries()) {
      statusRef.value = `Panel review lens ${index + 1}/${PANEL_REVIEW_LENSES.length}: ${lens.label}...`;
      sendLog(ws, `Running panel review lens '${lens.id}' (${index + 1}/${PANEL_REVIEW_LENSES.length})\n`);
      sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });

      const lensPrompt = buildPanelReviewLensPrompt({
        ...lensArgs[index],
        projectContext: projectContextSection,
        playbookSection,
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
        await failPanelClosed(reason, lastResultText || allOutput.slice(-20_000));
        return;
      }
      lensVerdicts[lens.id] = normalizedLens;
    }
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
