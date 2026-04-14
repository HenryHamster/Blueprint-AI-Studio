import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName,
  persistAndPostStatus, git, setupWorkspace, pushAndCreatePR,
  runClaudeCode, lintFixLoop, testFixLoop,
} from '../index.js';
import {
  TASK_TYPE_HINTS, DEFAULT_TOOL_GUIDANCE, PLAYBOOK_GUIDANCE,
  readClaudeMd, buildContextPrefix, buildFileTree, discoverRelatedTests, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook,
} from '../prompt.js';

// ── Standard Handler Helper ───────────────────────────────────────────────
export interface StandardHandlerOpts {
  taskId: string;
  payload: Record<string, unknown>;
  task: Record<string, unknown>;
  ghToken: string;
  owner: string;
  repo: string;
  ws: WebSocket | null;
  statusRef: { value: string };
  model?: string;
  effort?: string;
  branchPrefix: string;
  buildPrompt: (context: { contextPrefix: string; parentCtx: string; rawPrompt: string }) => string;
  commitPrefix: string;
  prTitlePrefix: string;
  noChangesMessage?: string;
  runLintFix?: boolean;
  runTestFix?: boolean;
  failOnTestFailure?: boolean;
  lintContext?: string;
  includeTaskTypeHints?: boolean;
}

async function runStandardHandler(opts: StandardHandlerOpts): Promise<void> {
  const {
    taskId, payload, task, ghToken, owner, repo, ws, statusRef,
    model, effort, branchPrefix, buildPrompt, commitPrefix, prTitlePrefix,
    noChangesMessage = 'No file changes.',
    runLintFix = true,
    runTestFix = false,
    failOnTestFailure = false,
    lintContext = 'the code you just wrote',
    includeTaskTypeHints = true,
  } = opts;

  const rawBaseBranch = strVal(payload.base_branch, 'main');
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  const rawTitle = strVal(task.title) || strVal(payload.title) || 'automated changes';
  const taskTitle = rawTitle.replace(/[`$\\!"';&|<>(){}[\]]/g, '');

  const contextPrefix = buildContextPrefix(task);

  const parentCtxObj = (task.context as Record<string, unknown> | undefined)?.parent_context as Record<string, unknown> | undefined;
  const parentCtx = parentCtxObj?.parent_prompt
    ? `## Parent Task Context\nThis task is a subtask of: **${strVal(parentCtxObj.parent_title)}**\nParent goal: ${strVal(parentCtxObj.parent_prompt)}\n\n`
    : '';

  const rawPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);
  const promptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const taskTypeKey = strVal(payload.type);
  const taskTypeHint = includeTaskTypeHints && TASK_TYPE_HINTS[taskTypeKey]
    ? `\n\n${TASK_TYPE_HINTS[taskTypeKey].replace(/\{\{BASE_BRANCH\}\}/g, baseBranch)}`
    : '';

  const claudeMd = readClaudeMd();
  const claudeMdPrefix = claudeMd ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const playbookSection = formatPlaybook(task.playbook as Record<string, string> | undefined);
  const finalPrompt = claudeMdPrefix + playbookSection + buildPrompt({ contextPrefix, parentCtx, rawPrompt }) + taskTypeHint + PLAYBOOK_GUIDANCE + `\nNote: If lint or test fixes required non-obvious steps, save them to the playbook (e.g., ./playbook set "lint-gotcha" "description").\n` + `\n\n${DEFAULT_TOOL_GUIDANCE}` + promptSuffix;

  const { workBranch, shortId } = await setupWorkspace(owner, repo, ghToken, baseBranch, branchPrefix, taskId, ws, statusRef);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions(taskTypeKey);

  const fileTree = buildFileTree(ws);
  const finalPromptWithTree = fileTree + finalPrompt;

  statusRef.value = 'Running Claude Code...';
  // Record HEAD before Claude runs so we can detect commits Claude made (e.g. before rebasing)
  const baseCommit = git(['rev-parse', 'HEAD']);
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(finalPromptWithTree, ws, statusRef, model, effort, disallowedTools);

  const diff = git(['status', '--porcelain']);
  const currentHead = git(['rev-parse', 'HEAD']);
  // Detect changes from BOTH uncommitted files AND commits Claude may have created
  const hasChanges = diff.length > 0 || currentHead !== baseCommit;

  if (hasChanges && exitCode === 0) {
    statusRef.value = 'Pushing changes...';
    sendLog(ws, '\nCommitting and pushing changes...\n');

    // Collect changed paths from both uncommitted changes and any commits Claude made
    const uncommittedPaths = diff.length > 0
      ? diff.split('\n').map(l => l.trim().split(/\s+/).pop() || '')
      : [];
    const committedPaths = currentHead !== baseCommit
      ? git(['diff', '--name-only', `${baseCommit}..HEAD`]).split('\n').filter(Boolean)
      : [];
    const changedPaths = [...new Set([...uncommittedPaths, ...committedPaths])];
    if (runLintFix) {
      await lintFixLoop(changedPaths, ws, statusRef, lintContext, model, effort);
    }

    let testFailuresDetected = false;
    let testsPassed = true;
    if (runTestFix && baseBranch === 'main') {
      const result = await testFixLoop(ws, statusRef, model, effort);
      testFailuresDetected = result.testFailuresDetected;
      testsPassed = result.passed;
    }

    if (failOnTestFailure && baseBranch === 'main' && testFailuresDetected && !testsPassed) {
      sendLog(ws, 'Tests failed after max fix attempts — marking task as FAILED.\n');
      await persistAndPostStatus(taskId, {
        task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
        logs: `Test suite failed after max fix attempts. Last test output:\n${sanitize(allOutput.slice(-10_000))}`,
        failure_category: 'test_failed',
      });
      return;
    }

    git(['add', '-A']);
    const commitNote = testFailuresDetected ? '\n\nNote: test failures detected, manual review recommended' : '';
    // Only create a new commit if there are staged changes — Claude may have
    // already committed, and lint/test fix loops may not have added new changes.
    const staged = git(['status', '--porcelain']);
    if (staged.length > 0) {
      execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `${commitPrefix}: ${taskTitle}\n\nAutomated by Blueprint Agent (task: ${shortId})${commitNote}`], { encoding: 'utf-8' });
    }

    let prBody = `## Summary\n\n${lastResultText.slice(0, 2000) || `Automated changes for: ${taskTitle}`}\n\n`;
    if (runTestFix) {
      const testSummary = testsPassed ? '\u2705 Tests passed' : testFailuresDetected ? '\u26a0\ufe0f Test failures detected' : '\u26a0\ufe0f Could not run tests';
      prBody += `## Test Results\n\n${testSummary}\n\n`;
    }
    prBody += `---\n*Automated by Blueprint Agent*`;

    const prUrl = await pushAndCreatePR(owner, repo, taskId, workBranch, baseBranch, `${prTitlePrefix} ${taskTitle}`, prBody, ws, statusRef);
    const commitHash = git(['rev-parse', 'HEAD']);

    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'COMPLETED', artifact_type: 'PULL_REQUEST',
      commit_hash: commitHash, pr_url: prUrl, repo_url: `https://github.com/${owner}/${repo}`,
    });
  } else if (exitCode === 0) {
    await persistAndPostStatus(taskId, { task_id: taskId, status: 'COMPLETED', logs: noChangesMessage });
  } else {
    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG', logs: sanitize(allOutput.slice(-10_000)),
      failure_category: 'claude_code_failed',
    });
  }
}

// ── Handler: Create (default — make changes, push, open PR) ─────────────────
export async function handleCreate(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);
  // Include title as context so the agent understands intent even if the prompt is terse
  const taskTitleStr = strVal(task.title);
  const prompt = taskTitleStr ? `Task: ${taskTitleStr}\n\n${rawPrompt}` : rawPrompt;
  if (!prompt) throw new Error('No prompt found in task payloads');

  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  const siblingTasks = projectCtx?.sibling_tasks;
  const siblingNote = Array.isArray(siblingTasks) && siblingTasks.length > 0
    ? '\n\nNote: Check the Sibling Tasks listed above and avoid making changes that overlap with or duplicate files they are already working on.'
    : '';

  const overlapWarnings = (task.context as Record<string, unknown> | undefined)?.file_overlap_warnings as Array<{shared_files: string[], with_task: string}> | undefined;
  const overlapWarningText = overlapWarnings && overlapWarnings.length > 0
    ? overlapWarnings.map(w =>
        `WARNING: This task shares files (${w.shared_files.join(', ')}) with parallel task "${w.with_task}". Coordinate carefully or check if that task already addressed this area.`
      ).join('\n') + '\n\n'
    : '';

  const relatedFiles = (
    (payload.related_files as string[] | undefined) ||
    ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
    []
  );
  const relatedTestContext = discoverRelatedTests(ws, relatedFiles);

  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'task',
    buildPrompt: ({ contextPrefix, parentCtx }) => `${contextPrefix}${parentCtx}${relatedTestContext}${overlapWarningText}${prompt}${siblingNote}`,
    commitPrefix: 'blueprint',
    prTitlePrefix: '[Blueprint]',
    noChangesMessage: 'No file changes.',
    runLintFix: true,
    runTestFix: true,
    failOnTestFailure: true,
    lintContext: 'the code you just wrote',
    includeTaskTypeHints: true,
  });
}

// ── Handler: Refactor (improve code quality without changing behavior) ────────
export async function handleRefactor(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);
  if (!rawPrompt) throw new Error('No prompt found in task payloads');

  const refactorOverlapWarnings = (task.context as Record<string, unknown> | undefined)?.file_overlap_warnings as Array<{shared_files: string[], with_task: string}> | undefined;
  const refactorOverlapWarningText = refactorOverlapWarnings && refactorOverlapWarnings.length > 0
    ? refactorOverlapWarnings.map(w =>
        `WARNING: This task shares files (${w.shared_files.join(', ')}) with parallel task "${w.with_task}". Coordinate carefully or check if that task already addressed this area.`
      ).join('\n') + '\n\n'
    : '';

  const refactorTitle = strVal(task.title, 'Refactor');
  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'refactor',
    buildPrompt: ({ contextPrefix, rawPrompt: rp }) =>
      `${contextPrefix}${refactorOverlapWarningText}REFACTOR TASK — STRICT CONSTRAINTS:
- Do NOT add new features or new behavior.
- Do NOT change any observable behavior, API contracts, or public interfaces.
- Focus on ONE refactor concern per task (e.g. extract functions, reduce duplication, improve naming, simplify logic, or remove dead code — pick the most impactful single concern).
- Run the existing test suite BEFORE making any changes to establish a baseline. If tests fail before you start, report them and do not proceed.
- Make your refactor changes.
- Run the test suite AGAIN after your changes. All tests that passed before must still pass. If any test regresses, revert or fix the regression before committing.
- Keep the diff minimal — only change what is necessary for the chosen refactor concern.

Task: ${refactorTitle}

${rp}`,
    commitPrefix: 'refactor',
    prTitlePrefix: 'refactor:',
    noChangesMessage: 'No file changes.',
    runLintFix: true,
    runTestFix: true,
    lintContext: 'the refactored code',
  });
}

// ── Handler: Docs (analyze codebase, generate/update documentation, open PR) ─
export async function handleDocs(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'docs',
    buildPrompt: () => `You are a documentation agent. Your job is to improve the documentation of this codebase.

Steps:
1. Analyze the codebase to understand what it does — read the README, key source files, API routes, configuration, and entry points.
2. Identify documentation gaps:
   - Missing or outdated README sections (installation, usage, architecture, configuration)
   - Undocumented public functions, classes, or modules (missing docstrings)
   - Undocumented API endpoints
   - Missing setup or deployment instructions
3. Generate or update documentation to fill those gaps:
   - Improve the README with accurate installation, usage, and architecture sections
   - Add or update docstrings for public functions and classes
   - Create or update API documentation where applicable
4. Focus on accuracy — only document what actually exists in the code. Do not invent features or describe things that don't exist.
5. Do NOT change any production code. Only modify documentation files (README, docs/, etc.) and add/update docstrings within existing source files.

Be thorough but precise. Every statement in the documentation must reflect the actual code.`,
    commitPrefix: 'docs',
    prTitlePrefix: 'docs:',
    noChangesMessage: 'No documentation changes needed.',
    runLintFix: false,
    runTestFix: false,
  });
}

// ── Handler: QA (analyze coverage, generate tests, open PR) ─────────────────
export async function handleQA(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'task',
    buildPrompt: ({ contextPrefix, parentCtx, rawPrompt }) =>
      `${contextPrefix}${parentCtx}You are a QA agent. Your job is to improve test coverage for this codebase.

${rawPrompt ? `Task context:\n${rawPrompt}\n\n` : ''}Instructions:
1. Analyze the existing test files to understand the test framework, patterns, and conventions used.
2. Identify functions, modules, or code paths that have no or insufficient test coverage.
3. Write comprehensive test files (unit tests and/or integration tests) following the exact same patterns and conventions as the existing tests.
4. Only add new test files or append tests to existing test files — do NOT modify any production code.
5. After writing the tests, run them to verify they pass. Fix any failures before finishing.

IMPORTANT RULES:
- Follow the existing test framework and patterns exactly (same imports, same assertion style, same file naming)
- Do not change production source files under any circumstances
- Each test should be self-contained and not depend on external state
- Cover edge cases and error paths, not just happy paths
- If a test requires mocking, use the same mocking approach as existing tests`,
    commitPrefix: 'test',
    prTitlePrefix: 'test: add test coverage for',
    noChangesMessage: 'No test files added.',
    runLintFix: false,
    runTestFix: false,
  });
}

// ── Handler: Deps (update dependencies to latest minor/patch versions) ─────
export async function handleDeps(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'deps',
    buildPrompt: () => `You are a dependency update agent. Your job is to update this project's dependencies to their latest MINOR and PATCH versions — do NOT make major version bumps as those can introduce breaking changes.

Steps to follow:
1. Detect which package managers are in use by checking for these files:
   - package.json \u2192 npm/yarn/pnpm
   - requirements.txt \u2192 pip
   - pyproject.toml \u2192 poetry or pip
   - Cargo.toml \u2192 cargo
   - go.mod \u2192 go mod
   - Gemfile \u2192 bundler

2. For each package manager found, check what's outdated:
   - npm: run \`npm outdated\` (or \`yarn outdated\` / \`pnpm outdated\`)
   - pip: run \`pip list --outdated\`
   - poetry: run \`poetry show --outdated\`
   - cargo: run \`cargo outdated\`
   - go: run \`go list -u -m all\`

3. Update dependencies to their latest MINOR/PATCH versions only:
   - npm: run \`npm update\` (respects semver ranges) or update package.json version ranges then \`npm install\`
   - pip: update requirements.txt with new versions
   - poetry: run \`poetry update\`
   - cargo: run \`cargo update\`
   - go: run \`go get -u ./...\` (patch only) then \`go mod tidy\`

4. Run the project's test suite to verify nothing broke:
   - Look for test scripts in package.json (e.g. \`npm test\`, \`npm run test\`)
   - Or pytest, cargo test, go test, etc.
   - If tests fail, identify which dependency update caused the failure and revert just that package to its previous version, then re-run tests.

5. Once tests pass, the dependency files should already be modified. Do NOT commit — the agent harness will handle the commit.

IMPORTANT: Only update minor/patch versions. If a package would require a major version bump to get any update, skip it. Focus on security patches and bug fixes.`,
    commitPrefix: 'chore',
    prTitlePrefix: 'chore:',
    noChangesMessage: 'No dependency updates needed.',
    runLintFix: false,
    runTestFix: false,
  });
}
