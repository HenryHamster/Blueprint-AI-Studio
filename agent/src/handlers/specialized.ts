import { execFileSync } from 'child_process';
/**
 * Specialized task handlers — domain-specific operations.
 *
 * - **handleMigrate**: Write database migrations. Clones repo, runs Claude Code,
 *   produces a new Alembic migration file, creates a PR.
 *
 * - **handleSmoketest**: Run smoke tests against a deployment. Clones repo,
 *   executes test scripts, validates deployment health, reports pass/fail.
 *
 * - **handleDebug**: Investigate and diagnose a bug. Clones repo, runs Claude Code
 *   with debugging tools, may run tests, produces analysis and proposed fixes.
 *
 * - **handlePerf**: Profile and optimize performance. Clones repo, runs profilers,
 *   analyzes hot paths, produces optimized code, creates a PR.
 *
 * - **handleInvestigate**: Open-ended investigation of an issue or area. Clones
 *   repo, runs Claude Code exploration, produces findings artifact.
 *
 * - **handleRepoBootstrap**: Inspect a newly onboarded repository and produce
 *   bootstrap metadata (build config, test runners, environment setup guidance).
 *   Does not clone repo or produce PR; artifact-first.
 */

import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sanitizeError, sendLog, validateBranchName, readHandlerOutput,
  persistAndPostStatus, git, runClaudeCode, sendHeartbeatSync,
  setCurrentPromptFingerprint, listOpenPullRequestsForBranch, deriveTaskBranch, matchesRateLimit,
  resolveBaseBranch, resolveTaskBranchNamespace,
} from '../index.js';
import { prepareTaskWorkBranch } from '../task-checkpoint.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import type { BuildConfig } from '../index.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import {
  getTaskTypeHints, PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE, DEFAULT_TOOL_GUIDANCE,
  buildCodeChangeHelperGuidance, codeChangeGuidanceVariant,
  READ_THE_FAILURE_MESSAGE, SIZE_GUARDRAIL_GUIDANCE, PRIOR_ATTEMPT_DIVERGENCE, BEYOND_SCOPE_REPORTING,
  PARALLEL_AUDIT_GUIDANCE, READ_ONLY_BASH_WRITE_NOTE,
  getTaskTypeFocusBlock,
  readClaudeMd, buildContextPrefix, buildFileTree, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook, buildRetryContext, computePromptFingerprint, type PlaybookEntry,
  formatAcceptanceEvidence, formatInvestigateCitationProvenance,
} from '../prompt.js';
import { providerAutoInjectsClaudeMd } from '../providers/runners.js';
import { resolveBuildConfig } from '../build-config.js';
import { runCodeChangeTask } from './code-change.js';
import { isParallelAuditEnabled, resolveCandidateSha } from './audits.js';
import { extractReservedMigrationRevision, buildReservedRevisionGuidance } from './review-fix-migration.js';
import { writeFetchHelper, buildFetchGuidance, PLAN_FETCH_VERBS } from './strategic-helpers.js';
import { repairStructuredOutput } from './structured-output-repair.js';
import { buildPromptSizeTelemetry, setPromptSizeTelemetry } from '../telemetry.js';
export { resolveCandidateSha, handleSecurityAudit, handleSelfAudit, handleReleaseAudit } from './audits.js';

// Provision the read-only ./fetch helper (direction|section|playbook) so a
// specialized handler can resolve refs trimmed from its launch snapshot.
// project_id is set for all task types by the backend; when it is absent we
// provision NOTHING and advertise nothing, so a prompt never promises a helper
// that wasn't written. Returns the ./fetch guidance block (or '' when missing).
function provisionFetchHelper(taskId: string, task: Record<string, unknown>): string {
  const projectId = ((task.project_metadata as Record<string, unknown> | undefined)?.project_id as string | undefined) ?? '';
  if (!projectId) return '';
  writeFetchHelper(taskId, projectId, WORKSPACE, { verbs: PLAN_FETCH_VERBS });
  return buildFetchGuidance(PLAN_FETCH_VERBS);
}

export function isRepoBootstrapOverview(value: unknown): value is {
  tech_stack: string;
  directory_layout: string;
  first_steps_summary: string;
  entry_points?: string;
  test_layout?: string;
  key_conventions?: string;
  environment_requirements?: string;
  source_revision?: string;
} {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  return typeof record.tech_stack === 'string'
    && typeof record.directory_layout === 'string'
    && typeof record.first_steps_summary === 'string';
}

export function isRepoDomainOverview(value: unknown): value is {
  purpose: string;
  key_concepts?: Record<string, string>;
  critical_user_flows?: string[];
  architectural_gotchas?: string[];
  source?: string;
} {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  return typeof record.purpose === 'string' && record.purpose.trim().length >= 20;
}

/**
 * Agent-reported repo_bootstrap command write-back outcome, reported separately
 * from the repo overview. `status` is one of the backend-accepted values
 * (success / missing_commands / writeback_failed); other shapes are rejected so
 * a malformed file degrades to "no payload" (the backend then records the
 * command-memory marker as "unknown" rather than falsely claiming success).
 */
export function isCommandMemoryOutcome(value: unknown): value is {
  status: 'success' | 'missing_commands' | 'writeback_failed';
  detail?: string;
  updated_paths?: string[];
} {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  return record.status === 'success'
    || record.status === 'missing_commands'
    || record.status === 'writeback_failed';
}

const COMMAND_MEMORY_DETAIL_MAX_LENGTH = 2000;
const COMMAND_MEMORY_UPDATED_PATHS_MAX_LENGTH = 50;

function normalizeCommandMemoryOutcome(value: {
  status: 'success' | 'missing_commands' | 'writeback_failed';
  detail?: string;
  updated_paths?: string[];
}): {
  status: 'success' | 'missing_commands' | 'writeback_failed';
  detail?: string;
  updated_paths?: string[];
} {
  const normalized: {
    status: 'success' | 'missing_commands' | 'writeback_failed';
    detail?: string;
    updated_paths?: string[];
  } = { status: value.status };

  if (typeof value.detail === 'string') {
    normalized.detail = value.detail.slice(0, COMMAND_MEMORY_DETAIL_MAX_LENGTH);
  }

  if (Array.isArray(value.updated_paths)) {
    normalized.updated_paths = value.updated_paths
      .filter((p): p is string => typeof p === 'string')
      .slice(0, COMMAND_MEMORY_UPDATED_PATHS_MAX_LENGTH);
  }

  return normalized;
}

// ── Migration layout resolution ──────────────────────────────────────────────
// Blueprint's database layout (`backend/alembic/versions/`, `app/models/`,
// `app/main.py:EXPECTED_SCHEMA_REVISION`) is the DEFAULT. A second repo can
// onboard the `migrate` handler by supplying build_config.migration overrides;
// each unset field falls back to the Blueprint default derived from the first
// configured python directory (or `backend` when no build_config). With no
// overrides this returns exactly the values the prompt previously hardcoded.
export interface ResolvedMigrationLayout {
  versionsDir: string;      // no trailing slash
  modelsDir: string;        // no trailing slash
  alembicCwd: string;       // directory alembic commands run from
  schemaRevisionFile: string; // '' means no schema-revision gate
  schemaRevisionConst: string;
}

export function resolveMigrationLayout(buildConfig?: BuildConfig): ResolvedMigrationLayout {
  const pythonDirs = (buildConfig?.directories ?? []).filter(d => d.type === 'python');
  const pyDir = pythonDirs.length > 0 ? pythonDirs[0].path.replace(/\/$/, '') : 'backend';
  const m = buildConfig?.migration;
  return {
    versionsDir: m?.versions_dir ?? `${pyDir}/alembic/versions`,
    modelsDir: m?.models_dir ?? `${pyDir}/app/models`,
    alembicCwd: m?.alembic_cwd ?? pyDir,
    // schema_revision_file: '' is an explicit "no gate" signal; only an
    // undefined override falls back to the Blueprint default.
    schemaRevisionFile: m?.schema_revision_file ?? `${pyDir}/app/main.py`,
    schemaRevisionConst: m?.schema_revision_const ?? 'EXPECTED_SCHEMA_REVISION',
  };
}

// ── Handler: Migrate (Alembic/SQLAlchemy database migration workflow) ────────
export async function handleMigrate(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);
  const rawBaseBranch = resolveBaseBranch(payload, task);
  const migratePromotionTarget = strVal(payload.promotion_target) || strVal((task.project_metadata as Record<string, unknown> | undefined)?.promotion_target) || undefined;
  const migrateTaskTitle = strVal(task.title);
  const prompt = migrateTaskTitle ? `Task: ${migrateTaskTitle}\n\n${rawPrompt}` : rawPrompt;

  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  if (!prompt) throw new Error('No prompt found in task payloads');

  // Clone
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo}...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: 100,
  });
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const migrateFetchGuidance = provisionFetchHelper(taskId, task);
  const disallowedTools = configureAgentPermissions('migrate');
  const migrateBuildConfig = resolveBuildConfig(task);

  git(['config', 'user.email', migrateBuildConfig?.git_user_email || 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', migrateBuildConfig?.git_user_name || 'Blueprint Agent']);

  const workBranch = deriveTaskBranch('migrate', taskId, resolveTaskBranchNamespace(task));
  await prepareTaskWorkBranch(
    { owner, repo, ghToken, baseBranch, workBranch, taskId, ws },
    { git, listOpenPullRequestsForBranch, sendLog },
  );

  // Migration layout. Blueprint's `backend/alembic/versions/`, `app/models/`,
  // and `app/main.py:EXPECTED_SCHEMA_REVISION` conventions are the DEFAULT, but
  // a non-Blueprint project can override any of them via build_config.migration
  // so the migrate handler works against a different layout. When the project
  // supplies no overrides the emitted prompt is byte-for-byte identical to the
  // prior hardcoded text.
  const migrateLayout = resolveMigrationLayout(migrateBuildConfig);
  const alembicVersionsPath = `${migrateLayout.versionsDir}/`;
  const migrateModelsPath = `${migrateLayout.modelsDir}/`;

  // If the orchestrator reserved a provisional revision for this task, the agent
  // must use it verbatim rather than letting autogenerate pick the next number —
  // that is what lets sibling migration tasks run in parallel without colliding.
  const migrateReserved = extractReservedMigrationRevision(task);
  const migrateReservedGuidance = migrateReserved
    ? buildReservedRevisionGuidance(
        migrateReserved,
        alembicVersionsPath,
        migrateLayout.schemaRevisionFile,
        migrateLayout.schemaRevisionConst,
      )
    : '';

  // Schema-revision step. Only emitted when the project declares a schema
  // revision file (Blueprint: backend/app/main.py). A project with no
  // schema-revision gate sets schema_revision_file to '' and the step (and
  // its step number) drop out cleanly.
  const hasSchemaRevisionGate = migrateLayout.schemaRevisionFile !== '';
  const schemaRevisionStep = hasSchemaRevisionGate
    ? `4. **Bump ${migrateLayout.schemaRevisionConst} (MANDATORY)**: Open \`${migrateLayout.schemaRevisionFile}\` and update the \`${migrateLayout.schemaRevisionConst}\` constant to match the revision string of the migration file you just generated (e.g. \`"0039"\` for \`0039_add_user_role.py\`). This is a hard gate — CI and startup checks will fail if the constant does not match the latest migration revision. Do NOT skip this step.

`
    : '';
  // The schema-revision step occupies number 4 when present; the remaining steps
  // (review/apply/downgrade/finish) renumber so a repo with no schema-revision
  // gate sees a contiguous 4,5,6,7 instead of a gap at 4.
  const reviewStepNum = hasSchemaRevisionGate ? 5 : 4;
  const applyStepNum = reviewStepNum + 1;
  const downgradeStepNum = reviewStepNum + 2;
  const finishStepNum = reviewStepNum + 3;

  const migratePrompt = `You are a database migration agent specializing in Alembic and SQLAlchemy/SQLModel workflows.

Your task: ${prompt}

Follow these steps precisely:

1. **Examine existing migrations**: Look at \`${alembicVersionsPath}\` to understand naming conventions, import patterns, and how previous migrations are structured. Note the revision ID format, how dependencies are chained, and any project-specific patterns.

2. **Make model changes**: Apply the requested SQLModel/SQLAlchemy model changes in the appropriate files (typically under \`${migrateModelsPath}\` or similar). Do NOT manually write migration files — autogenerate will handle that.

3. **Generate migration**: Run \`alembic revision --autogenerate -m "description"\` from the ${migrateLayout.alembicCwd} directory (where alembic.ini lives). Use a concise, snake_case description that matches existing naming conventions.

   IMPORTANT: Do NOT hand-edit the generated migration file unless autogenerate produced clearly incorrect output (e.g., dropped the wrong table, missed an index, used wrong column type). If you must edit, add a comment explaining why.

${schemaRevisionStep}${reviewStepNum}. **Review the generated migration**: Open and carefully read the generated file. Check for:
   - Data loss risks (e.g., \`DROP COLUMN\`, \`DROP TABLE\`, type changes that truncate data)
   - Missing indexes on foreign keys or frequently-queried columns
   - Incorrect \`nullable\` settings
   - Server defaults that should be included for backfills
   - \`op.execute()\` statements needed for data migrations
   If you find issues, fix them and document your changes with inline comments.

${applyStepNum}. **Test the migration applies cleanly**: Run \`alembic upgrade head\` to verify the migration applies without errors. If it fails:
   - Read the error carefully
   - Fix the migration file (add a comment explaining the manual fix)
   - Run \`alembic upgrade head\` again
   - Repeat until it succeeds

${downgradeStepNum}. **Test downgrade** (if the migration is reversible): Run \`alembic downgrade -1\` then \`alembic upgrade head\` to confirm round-trip works. If downgrade is not possible (e.g., data loss), ensure the \`downgrade()\` function raises \`NotImplementedError\` with an explanation.

${finishStepNum}. Once migration applies cleanly, run \`./check test\` (NOT just \`./check\` — migrations touch the schema, so the project's full DB-backed backend test suite must pass before shipping). Then \`./pr open --title "migrate: <description>" --body "<summary>"\` to push and open the PR, then \`./done\` to mark the task complete. Use \`./fail "<reason>"\` if the migration genuinely cannot be made to work (e.g. autogenerate is fundamentally wrong about the schema and you can't recover).

ALREADY-DONE CASE: If you discover the requested migration/model change ALREADY EXISTS on the base branch (e.g. a sibling PR already added the same revision file and model${hasSchemaRevisionGate ? ` and the matching ${migrateLayout.schemaRevisionConst} bump` : ''}), do NOT create a duplicate migration — a second revision with the same logical change would branch the revision chain and break chain-linearity tests. Instead, VERIFY the deliverable is present (name the revision file and model${hasSchemaRevisionGate ? ' and schema-revision value' : ''}) and call \`./done --already-done "<reason naming the existing revision file and model${hasSchemaRevisionGate ? ' and schema-revision value' : ''}, and the sibling PR if known>"\`. This lands the task COMPLETED with no PR and records why. Do NOT just exit — a bare no-diff exit is treated as incomplete and fails.

CRITICAL RULES:
- Never delete or modify existing migration files in \`${alembicVersionsPath}\` (except the one you just generated)
- Never run \`alembic stamp\` or \`alembic downgrade base\` — these can corrupt production databases
- If autogenerate produces an empty migration with no changes, verify that your model changes were saved and the alembic env.py is configured to detect them
- Document any data loss risks prominently using inline comments in the migration file`;

  const migrateClaudeMd = readClaudeMd();
  const migrateClaudeMdSection = (migrateClaudeMd && !providerAutoInjectsClaudeMd()) ? `## Codebase Instructions\n${migrateClaudeMd}\n\n` : '';
  const migratePlaybookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  // Shared launch-context block (sibling/queued tasks, the served direction,
  // quality signals). Degrades to '' when absent, so it is safe even when the
  // migrate payload carries only partial context. See buildContextPrefix.
  const migrateContextPrefix = buildContextPrefix(task);
  const migrateContextSection = migrateContextPrefix ? `${migrateContextPrefix}\n` : '';
  const migrateRetryContext = buildRetryContext(task);
  const migrateFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  const migratePromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const migrateAcceptanceEvidenceNotice = formatAcceptanceEvidence(payload);
  statusRef.value = 'Running migration workflow...';
  const migrateBaseCommit = git(['rev-parse', 'HEAD']);
  const migrateFocusBlock = getTaskTypeFocusBlock('migrate', {
    schemaRevisionFile: migrateLayout.schemaRevisionFile,
    schemaRevisionConst: migrateLayout.schemaRevisionConst,
  });
  const migrateCodeChangeHelper = buildCodeChangeHelperGuidance({ buildConfig: migrateBuildConfig, owner, repo });
  const migrateGuidanceFooter = PLAYBOOK_GUIDANCE + READ_THE_FAILURE_MESSAGE + SIZE_GUARDRAIL_GUIDANCE
    + PRIOR_ATTEMPT_DIVERGENCE + BEYOND_SCOPE_REPORTING;
  const migrateFetchBlock = `

${migrateFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const finalMigratePrompt = migrateClaudeMdSection + migrateAcceptanceEvidenceNotice + migrateContextSection
    + migratePlaybookSection + migrateRetryContext + migrateReservedGuidance + migratePrompt
    + migrateFirstTaskSection + migrateCodeChangeHelper + migrateFocusBlock + migrateGuidanceFooter
    + migratePromptSuffix + migrateFetchBlock;
  const migrateTelemetry = buildPromptSizeTelemetry(finalMigratePrompt, [
    ['claude_md', migrateClaudeMdSection],
    ['context', migrateContextSection],
    ['playbook', migratePlaybookSection],
    ['retry_context', migrateRetryContext],
    ['reserved_revision', migrateReservedGuidance],
    ['core_prompt', migratePrompt],
    ['first_task', migrateFirstTaskSection],
    ['code_change_helper', migrateCodeChangeHelper],
    ['focus_block', migrateFocusBlock],
    ['guidance', migrateGuidanceFooter],
    ['prompt_suffix', migratePromptSuffix],
    ['fetch_tool', migrateFetchBlock],
  ]);
  setPromptSizeTelemetry(migrateTelemetry.prompt_size_bytes, migrateTelemetry.prompt_section_count, migrateTelemetry.prompt_section_sizes);
  const migrateFingerprint = computePromptFingerprint('migrate', migrateBuildConfig, {
    codeChangeGuidanceVariant: codeChangeGuidanceVariant({ buildConfig: migrateBuildConfig, owner, repo }),
  });
  setCurrentPromptFingerprint(migrateFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: migrateFingerprint });
  // 2026-05-04: post-Claude commit/rebase/push/PR pipeline replaced with
  // runCodeChangeTask. Claude calls ./check + ./pr open + ./done from
  // inside its own session. See agent/src/handlers/code-change.ts.
  await runCodeChangeTask({
    taskId,
    prompt: finalMigratePrompt,
    taskPrompt: migratePrompt,
    ws,
    statusRef,
    workBranch,
    baseBranch,
    owner,
    repo,
    testsOnMainBranch: baseBranch === (migratePromotionTarget ?? resolveBaseBranch({}, task)),
    buildConfig: migrateBuildConfig,
    taskCapability: task.task_capability as Partial<Record<'agent_mode' | 'run_shape' | 'output_kind', unknown>> | undefined,
    baseCommit: migrateBaseCommit,
    model,
    effort,
    disallowedTools,
    noChangesMessage: 'No migration changes generated.',
  });
}

// ── Handler: Smoketest (post-merge end-to-end health checks) ─────────────────
export async function handleSmoketest(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = resolveBaseBranch(payload, task);
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  // CRITICAL (C4): the qualifier-smoketest must run against ONE pinned
  // candidate commit, not whatever `automain` has drifted to by the time this
  // VM starts. The release qualifier passes the snapshotted candidate as a
  // full 40-char `candidate_sha` in the task payload. `git clone --branch <X>`
  // (the path prepareWorkspaceForRole takes) accepts a branch/tag name, NOT a
  // SHA — so we clone the branch normally, then fetch+checkout the exact SHA.
  // A plain post-merge smoketest carries no candidate_sha and is unaffected.
  const candidateSha = resolveCandidateSha(payload, task);

  // Clone (read-only — no work branch or push needed). When pinning a
  // candidate SHA, clone with FULL history (depth 0): the snapshotted commit
  // may be older than a shallow clone's depth, and the SHA checkout below must
  // be able to reach it.
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(
    ws,
    candidateSha
      ? `Cloning ${owner}/${repo} @ ${baseBranch}, pinning candidate ${candidateSha.slice(0, 12)}...\n`
      : `Cloning ${owner}/${repo} @ ${baseBranch}...\n`,
  );
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: candidateSha ? 0 : 100,
  });

  // Pin the workspace to the exact candidate commit. `git fetch origin <sha>`
  // brings the precise object (GitHub permits fetching a SHA reachable from a
  // ref; candidate_sha is the snapshotted `automain` HEAD, so it is reachable),
  // then a detached checkout moves the working tree onto it. Fail loud if the
  // SHA cannot be obtained — a smoketest of the wrong commit produces a
  // meaningless qualifier signal.
  if (candidateSha) {
    sendLog(ws, `Fetching + checking out candidate commit ${candidateSha.slice(0, 12)}...\n`);
    try {
      execFileSync('git', ['fetch', 'origin', candidateSha], { cwd: WORKSPACE, stdio: ['ignore', 'pipe', 'pipe'] });
      execFileSync('git', ['checkout', '--detach', candidateSha], { cwd: WORKSPACE, stdio: ['ignore', 'pipe', 'pipe'] });
      sendLog(ws, `Checked out candidate commit ${candidateSha.slice(0, 12)} (detached HEAD).\n`);
    } catch (e) {
      const detail = sanitizeError(e);
      sendLog(ws, `[ERROR] Could not check out candidate commit ${candidateSha}: ${detail}\n`);
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: 'FAILED',
        artifact_type: 'ERROR_LOG',
        failure_category: 'clone_failed',
        logs: sanitize(
          `Qualifier smoketest aborted: could not check out the pinned ` +
          `candidate commit ${candidateSha}. The candidate must be the exact ` +
          `commit the release qualifier snapshotted; running the smoketest ` +
          `against a different commit would produce a meaningless signal. ` +
          `Detail: ${detail}`,
        ),
      });
      return;
    }
  }

  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const smoketestFetchGuidance = provisionFetchHelper(taskId, task);
  const disallowedTools = configureAgentPermissions('smoketest');

  const smoketestBuildConfig = (task.build_config ?? {}) as BuildConfig;
  const smoketestPythonDirs = (smoketestBuildConfig.directories ?? []).filter(d => d.type === 'python');
  const smoketestPyDir = smoketestPythonDirs.length > 0 ? smoketestPythonDirs[0].path.replace(/\/$/, '') : 'backend';
  const smoketestConfigPath = `${smoketestPyDir}/testing/smoketest.yaml`;

  const smoketestRetryContext = buildRetryContext(task);
  const smoketestFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  // Shared launch-context block; degrades to '' when absent (graceful).
  const smoketestContextPrefix = buildContextPrefix(task);
  const smoketestContextSection = smoketestContextPrefix ? `${smoketestContextPrefix}\n` : '';
  const smoketestCoreBody = `You are a smoke test agent. Your job is to run structured health checks on this codebase using the smoketest configuration.

Follow these steps:

1. Read ${smoketestConfigPath} to understand the test suites and guardrails.

2. Run each suite IN ORDER from the config (startup, curated, regression):

   a. STARTUP: Run each command listed in suites.startup.commands from the ${smoketestPyDir}/ directory.
      Record pass/fail for each command.

   b. CURATED: Run pytest with the args from suites.curated.pytest_args.
      Working directory: ${smoketestPyDir}/
      Command: python3 -m pytest <pytest_args from config>
      Record individual test results (name, pass/fail, error message if any).

   c. REGRESSION: Run pytest with the args from suites.regression.pytest_args.
      Working directory: ${smoketestPyDir}/
      Command: python3 -m pytest <pytest_args from config>
      Record individual test results.

3. Check guardrails from the config:
   - Count tests collected for curated and regression suites
   - If count < guardrails.min_test_count for either suite, record a guardrail_violation

4. Write results to /tmp/smoketest_result_${taskId}.json ${READ_ONLY_BASH_WRITE_NOTE} with this structure:
{
  "passed": false,
  "suites": {
    "startup": {"passed": true, "tests": [{"name": "...", "status": "pass", "message": "..."}]},
    "curated": {"passed": true, "tests": [...]},
    "regression": {"passed": false, "tests": [...]}
  },
  "guardrail_violations": [],
  "tests": [<flat list of ALL tests from all suites, for backward compatibility>]
}

Set top-level "passed" to true only if ALL suites passed and there are no guardrail violations.

Use status "error" for collection, setup, environment, or command-execution failures. Use status "skip" only when a test is intentionally inapplicable, never when the test could not run. If no tests can execute, include at least one "error" test, set top-level "passed" to false, and explain the blocker in its message.

IMPORTANT: Always write /tmp/smoketest_result_${taskId}.json ${READ_ONLY_BASH_WRITE_NOTE}, including when tests cannot run. Never leave the file unwritten.`;
  const smoketestFetchBlock = `

${smoketestFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const smoketestPrompt = `${smoketestContextSection}${smoketestRetryContext}${smoketestCoreBody}${smoketestFirstTaskSection}${smoketestFetchBlock}`;
  const smoketestFullPrompt = smoketestPrompt + PLAYBOOK_GUIDANCE;

  statusRef.value = 'Running smoke tests...';
  const smoketestTelemetry = buildPromptSizeTelemetry(smoketestFullPrompt, [
    ['context', smoketestContextSection],
    ['retry_context', smoketestRetryContext],
    ['core_prompt', smoketestCoreBody],
    ['first_task', smoketestFirstTaskSection],
    ['fetch_tool', smoketestFetchBlock],
    ['playbook', PLAYBOOK_GUIDANCE],
  ]);
  setPromptSizeTelemetry(smoketestTelemetry.prompt_size_bytes, smoketestTelemetry.prompt_section_count, smoketestTelemetry.prompt_section_sizes);
  const smoketestFingerprint = computePromptFingerprint('smoketest', smoketestBuildConfig);
  setCurrentPromptFingerprint(smoketestFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: smoketestFingerprint });
  const { exitCode, allOutput } = await runClaudeCode(smoketestFullPrompt, ws, statusRef, model, effort, disallowedTools);

  // Read result file written by Claude
  const rawSmoketestResult = readHandlerOutput(taskId, 'smoketest_result', ws) as Record<string, unknown> | null;
  const smoketestResult = rawSmoketestResult
    && typeof rawSmoketestResult.passed === 'boolean'
    && Array.isArray(rawSmoketestResult.tests)
    ? rawSmoketestResult as { passed: boolean; tests: Array<{ name: string; status: string; message: string }> }
    : null;

  if (exitCode === 0 && smoketestResult) {
    // Live endpoint checks
    const healthCheckUrls: string[] = Array.isArray(payload.health_check_urls)
      ? (payload.health_check_urls as string[])
      : [];
    // Filter URLs: only allow http(s), block metadata endpoints and internal IPs
    const safeUrls = healthCheckUrls.filter(u => {
      try {
        const parsed = new URL(u);
        if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;
        const host = parsed.hostname;
        if (host === '169.254.169.254' || host === 'metadata.google.internal') return false;
        if (host.startsWith('10.') || host.startsWith('192.168.') || host === 'localhost' || host === '127.0.0.1') return false;
        if (host.startsWith('172.') && parseInt(host.split('.')[1]) >= 16 && parseInt(host.split('.')[1]) <= 31) return false;
        return true;
      } catch { return false; }
    });
    if (safeUrls.length > 0) {
      sendLog(ws, '\nLive endpoint checks:\n');
      for (const url of safeUrls) {
        let curlStatus: string;
        let curlMessage: string;
        try {
          execFileSync('curl', ['-sf', '--max-time', '10', url], { encoding: 'utf-8' });
          curlStatus = 'pass';
          curlMessage = 'HTTP 2xx';
          sendLog(ws, `  PASS  ${url}\n`);
        } catch (err: unknown) {
          curlStatus = 'fail';
          const exitErr = err as { status?: number };
          curlMessage = exitErr.status !== undefined ? `curl exited with code ${exitErr.status}` : 'curl failed';
          sendLog(ws, `  FAIL  ${url} — ${curlMessage}\n`);
        }
        smoketestResult.tests.push({ name: `HTTP GET ${url}`, status: curlStatus, message: curlMessage });
      }
      // Health checks may turn a pass into a failure, but never erase an
      // earlier suite/test failure from the parseable result.
      smoketestResult.passed = smoketestResult.passed && smoketestResult.tests.every(t => t.status === 'pass' || t.status === 'skip');
    }

    // Candidate-side env-preflight evidence (Slice 2, design 245212a4). Run the
    // candidate tree's OWN env-manifest introspector and fold the pure-content
    // artifact into the SMOKETEST_RESULT. This is EVIDENCE, never a gate: a
    // failure attaches nothing and MUST NOT change `passed`. Presence-only —
    // the introspector emits env NAMES/SCOPES, never a value.
    try {
      const envPreflightJson = execFileSync(
        'python3', ['-m', 'app.env_preflight_evidence'],
        { cwd: `${WORKSPACE}/${smoketestPyDir}`, encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'] },
      );
      const parsedEnvPreflight: unknown = JSON.parse(envPreflightJson);
      (smoketestResult as Record<string, unknown>).env_preflight = parsedEnvPreflight;
      sendLog(ws, '\nCaptured candidate env-preflight evidence.\n');
    } catch (e: unknown) {
      // Best-effort only — surface a marker but leave `passed` untouched.
      (smoketestResult as Record<string, unknown>).env_preflight_error = sanitizeError(e);
      sendLog(ws, `\n[warn] env-preflight evidence unavailable: ${sanitizeError(e)}\n`);
    }

    const passed = smoketestResult.passed;
    const passCount = smoketestResult.tests.filter(t => t.status === 'pass').length;
    const summary = `Smoke tests ${passed ? 'PASSED' : 'FAILED'}: ${passCount}/${smoketestResult.tests.length} passed`;
    sendLog(ws, `\n${summary}\n`);
    const taskContext = task.context && typeof task.context === 'object'
      ? task.context as Record<string, unknown>
      : {};
    const qualifierSmoketest = taskContext.qualifier_smoketest === true;
    // Qualifier smoketests remain COMPLETED so their artifact feeds the release
    // score. Ordinary smoketest failures use the existing FAILED path, which
    // stores the same artifact and triggers the established repair-task flow.
    const terminalStatus = passed || qualifierSmoketest ? 'COMPLETED' : 'FAILED';
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: terminalStatus,
      artifact_type: 'SMOKETEST_RESULT',
      logs: JSON.stringify(smoketestResult),
      repo_url: `https://github.com/${owner}/${repo}`,
    });
  } else {
    const smoketestUnparseable = exitCode === 0;
    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
      logs: sanitize(allOutput.slice(-10_000)),
      failure_category: matchesRateLimit(allOutput) ? 'rate_limited' : (smoketestUnparseable ? 'smoketest_result_unparseable' : 'claude_code_failed'),
    });
  }
}

// ── Handler: Debug (analyze task/CI/test failures and produce root cause analysis) ──
export async function handleDebug(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const errorLogs = strVal(payload.error_logs) || strVal(payload.failure_context);
  const failedTaskId = strVal(payload.failed_task_id);
  const failedTaskTitle = strVal(payload.failed_task_title) || strVal(task.title);
  const rawBaseBranch = resolveBaseBranch(payload, task);
  validateBranchName(rawBaseBranch, 'base_branch');

  // Clone for read-only analysis (no work branch or push needed)
  statusRef.value = `Cloning ${owner}/${repo} for debug analysis...`;
  sendLog(ws, `Cloning ${owner}/${repo} @ ${rawBaseBranch} for debug analysis...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: rawBaseBranch, baseBranch: rawBaseBranch, depth: 100,
  });
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const debugFetchGuidance = provisionFetchHelper(taskId, task);
  const disallowedTools = configureAgentPermissions('debug');

  // Gather git context upfront to include in the prompt
  const gitLog = git(['log', '--oneline', '-20']);
  const gitDiffStat = (() => { try { return git(['diff', 'HEAD~5...HEAD', '--stat']); } catch { return '(not enough history)'; } })();

  // Load codebase instructions and project context (like other handlers)
  const claudeMd = readClaudeMd();
  const contextPrefix = buildContextPrefix(task);
  const fileTree = buildFileTree(ws);
  const debugRetryContext = buildRetryContext(task);
  const debugFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';

  const debugClaudeMdSection = (claudeMd && !providerAutoInjectsClaudeMd()) ? `\n## Codebase Instructions\n${claudeMd}\n` : '';
  const debugContextSection = contextPrefix ? `\n${contextPrefix}` : '';
  const debugFileTreeSection = fileTree ? `\n## Repository File Tree\n${fileTree}\n` : '';
  const debugCoreBody = `You are a debug analysis agent. Your job is to analyze a task failure and produce a structured root cause analysis. Do NOT make any code changes.

## Failed Task
Title: ${failedTaskTitle}
${failedTaskId ? `Task ID: ${failedTaskId}` : ''}

## Error Logs
\`\`\`
${errorLogs.slice(0, 8000)}
\`\`\`

## Recent Git History (last 20 commits)
\`\`\`
${gitLog}
\`\`\`

## Recent Changes (last 5 commits, file stats)
\`\`\`
${gitDiffStat}
\`\`\`

## Your Analysis Steps

1. Read the error logs carefully. Identify the exact error message, exception type, or test name that failed.

2. Trace the error to its source in the codebase:
   - Search for the file(s) and function(s) mentioned in the error or stack trace
   - Read those files to understand the context around the failure
   - Check if the error is in a test, CI step, import, or runtime code

3. Check the git history for when this issue was introduced:
   - Review the recent commits listed above — look for changes to the relevant files
   - If a recent commit looks responsible, read those changed files
   - Determine whether this is a regression (something that recently broke) or a pre-existing issue

4. Identify the specific root cause — name the file, function, and line where the problem originates.

5. Write your analysis to /tmp/debug_report_${taskId}.json ${READ_ONLY_BASH_WRITE_NOTE} with EXACTLY this structure:
{
  "root_cause": "One clear paragraph explaining what caused the failure, which file/function is responsible, and how it broke",
  "affected_files": ["path/to/file1.ts", "path/to/file2.py"],
  "fix_prompt": "Self-contained prompt for a create-type agent to fix this issue. Include: what the problem is, which files to modify, what the correct behavior should be, and what the fix should look like. Be specific enough that the agent can implement the fix without additional context.",
  "confidence": "high"
}

Set confidence to:
- "high" if you found the exact code causing the failure and have a clear fix
- "medium" if you have a strong hypothesis but some uncertainty
- "low" if the root cause is unclear or spans many systems

IMPORTANT: Always write /tmp/debug_report_${taskId}.json ${READ_ONLY_BASH_WRITE_NOTE} even if you cannot determine the root cause (use confidence "low" and explain what you found in root_cause). Never leave the file unwritten.`;
  const debugPrompt = debugClaudeMdSection + debugRetryContext + debugContextSection + debugFileTreeSection + debugCoreBody;

  statusRef.value = 'Analyzing failure...';
  const debugFingerprint = computePromptFingerprint('debug');
  setCurrentPromptFingerprint(debugFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: debugFingerprint });
  const debugParallelGuidance = isParallelAuditEnabled(task) ? PARALLEL_AUDIT_GUIDANCE : '';
  const debugPlaybookNote = '\nNote: If your investigation revealed a root cause pattern, diagnostic technique, or system behavior that would help future debugging, save it to the playbook.\n';
  const debugFetchBlock = `\n\n${debugFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const debugFullPrompt = debugPrompt + debugFirstTaskSection + debugParallelGuidance + PLAYBOOK_GUIDANCE + debugPlaybookNote + debugFetchBlock;
  const debugTelemetry = buildPromptSizeTelemetry(debugFullPrompt, [
    ['claude_md', debugClaudeMdSection],
    ['retry_context', debugRetryContext],
    ['context', debugContextSection],
    ['file_tree', debugFileTreeSection],
    ['core_prompt', debugCoreBody],
    ['first_task', debugFirstTaskSection],
    ['parallel_audit', debugParallelGuidance],
    ['playbook', PLAYBOOK_GUIDANCE],
    ['playbook_note', debugPlaybookNote],
    ['fetch_tool', debugFetchBlock],
  ]);
  setPromptSizeTelemetry(debugTelemetry.prompt_size_bytes, debugTelemetry.prompt_section_count, debugTelemetry.prompt_section_sizes);
  const { allOutput } = await runClaudeCode(debugFullPrompt, ws, statusRef, model, effort, disallowedTools);

  const debugResult = readHandlerOutput(taskId, 'debug_report', ws) as { root_cause: string; affected_files: string[]; fix_prompt: string; confidence: string } | null;

  if (debugResult) {
    sendLog(ws, `\nDebug analysis complete (confidence: ${debugResult.confidence})\n`);
    sendLog(ws, `Root cause: ${debugResult.root_cause.slice(0, 500)}\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'COMPLETED',
      artifact_type: 'DEBUG_RESULT',
      logs: JSON.stringify(debugResult),
      repo_url: `https://github.com/${owner}/${repo}`,
    });
  } else {
    const debugLogs = sanitize(allOutput.slice(-10_000));
    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
      logs: debugLogs,
      failure_category: matchesRateLimit(allOutput) ? 'rate_limited' : 'claude_code_failed',
    });
  }
}

// ── Handler: Perf (analyze codebase for performance bottlenecks, output findings) ──
export async function handlePerf(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = resolveBaseBranch(payload, task);
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  statusRef.value = `Cloning ${owner}/${repo} for performance analysis...`;
  sendLog(ws, `Starting performance analysis...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const perfFetchGuidance = provisionFetchHelper(taskId, task);
  const disallowedTools = configureAgentPermissions('perf');

  const perfBuildConfig = resolveBuildConfig(task);
  const perfDirs = perfBuildConfig?.directories ?? [];
  const perfPythonDirs = perfDirs.filter(d => d.type === 'python');
  const perfNodeDirs = perfDirs.filter(d => d.type === 'node');
  const hasPerfPyDir = perfPythonDirs.length > 0;
  const hasPerfNodeDir = perfNodeDirs.length > 0;
  const perfPyDir = hasPerfPyDir ? perfPythonDirs[0].path.replace(/\/$/, '') : 'backend';
  const perfNodeDir = hasPerfNodeDir ? perfNodeDirs[0].path.replace(/\/$/, '') : 'admin-ui';
  // Stack-specific anti-pattern hints are appended only when the corresponding
  // directory type is actually detected in buildConfig, so a non-Python /
  // non-frontend project does not get irrelevant ORM / component guidance.
  const perfPyHint = hasPerfPyDir ? ` Check ${perfPyDir}/ for these patterns.` : '';
  const perfNodeHint = hasPerfNodeDir ? ` Check ${perfNodeDir}/ for these patterns.` : '';
  const perfRetryContext = buildRetryContext(task);
  const perfFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  // Shared launch-context block; degrades to '' when absent (graceful).
  const perfContextPrefix = buildContextPrefix(task);
  const perfContextSection = perfContextPrefix ? `${perfContextPrefix}\n` : '';

  const perfCoreBody = `You are a performance analysis agent. You have full access to this repository. Your job is to analyze the codebase for demonstrable performance bottlenecks and produce structured, evidence-based findings.

Analyze the codebase for the following performance anti-patterns. Only flag issues you can directly observe in the code — do NOT speculate about theoretical problems.

1. **N+1 data-access queries**: Relationships or records loaded inside loops (e.g. issuing one query per iteration) where a single batched/eager query would suffice.${perfPyHint}
2. **Missing data-store indexes**: Columns used in WHERE/filter clauses, JOIN/lookup conditions, or ORDER BY/sort that lack an index. Compare schema definitions against query patterns.${perfPyHint}
3. **Synchronous blocking operations**: Network calls, file I/O, or CPU-heavy work done synchronously inside async request handlers or hot paths.
4. **Unpaginated large payload returns**: Endpoints or functions that return full table scans or unbounded lists without a limit/offset or cursor pagination.
5. **Whole-library imports**: Importing an entire library where only a small submodule is used, when the library supports importing just that submodule (bloats the build / startup cost).${perfNodeHint}
6. **Missing memoization of expensive recomputation**: Expensive computations or callbacks recomputed on every invocation/render where the result could be cached or memoized.${perfNodeHint}

Instructions:
1. Read key files in the relevant directories (schema/index definitions, query patterns, request handlers, and any UI/component code) and trace how data flows through the hot paths.
2. For each finding, identify the specific file, function, and line range where the problem is observable.
3. Only report findings that are demonstrably present — cite the exact code pattern that shows the problem.
4. Produce your findings as a JSON array written to /tmp/perf_report_${taskId}.json ${READ_ONLY_BASH_WRITE_NOTE}:

[
  {
    "area": "database",
    "severity": "high",
    "title": "Short description (under 60 chars)",
    "description": "What the problem is, why it matters, and the exact code pattern observed.",
    "fix_prompt": "Self-contained prompt for a create agent to fix this issue. Must describe exactly what to change and where.",
    "related_files": ["path/to/file.ext"]
  }
]

Valid values:
- area: database, api, frontend, agent
- severity: high (measurable user-facing impact), medium (latency/memory concern under load), low (minor inefficiency)

Rules:
- Evidence-based only: every finding must cite the specific file and code pattern that demonstrates the problem
- Do NOT flag theoretical issues (e.g. "this could be slow if data grows") — only flag patterns that are demonstrably inefficient now
- fix_prompt must be fully self-contained — the fixing agent has no context beyond that string
- Limit to 15 findings maximum, prioritized by severity
- IMPORTANT: Write your findings JSON array to /tmp/perf_report_${taskId}.json ${READ_ONLY_BASH_WRITE_NOTE} (NOT in the repository). The file should contain ONLY the JSON array, no markdown wrapping.
- Also output the same JSON array as the final output in your logs.`;
  const perfPrompt = `${perfContextSection}${perfRetryContext}${perfCoreBody}`;

  const perfPerfSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const perfTypeHints = getTaskTypeHints(perfBuildConfig);
  const perfTypeHint = perfTypeHints['perf'] ? '\n\n' + perfTypeHints['perf'] : '';
  statusRef.value = 'Running performance analysis...';
  const perfFingerprint = computePromptFingerprint('perf', perfBuildConfig);
  setCurrentPromptFingerprint(perfFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: perfFingerprint });
  const perfParallelGuidance = isParallelAuditEnabled(task) ? PARALLEL_AUDIT_GUIDANCE : '';
  const perfFetchBlock = `\n\n${perfFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const perfFullPrompt = perfPrompt + perfTypeHint + perfFirstTaskSection + perfPerfSuffix + perfParallelGuidance + PLAYBOOK_GUIDANCE + perfFetchBlock;
  const perfTelemetry = buildPromptSizeTelemetry(perfFullPrompt, [
    ['context', perfContextSection],
    ['retry_context', perfRetryContext],
    ['core_prompt', perfCoreBody],
    ['type_hint', perfTypeHint],
    ['first_task', perfFirstTaskSection],
    ['prompt_suffix', perfPerfSuffix],
    ['parallel_audit', perfParallelGuidance],
    ['playbook', PLAYBOOK_GUIDANCE],
    ['fetch_tool', perfFetchBlock],
  ]);
  setPromptSizeTelemetry(perfTelemetry.prompt_size_bytes, perfTelemetry.prompt_section_count, perfTelemetry.prompt_section_sizes);
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(perfFullPrompt, ws, statusRef, model, effort, disallowedTools);

  const perfFindings = readHandlerOutput(taskId, 'perf_report', ws);
  let logs = perfFindings !== null ? sanitize(JSON.stringify(perfFindings)) : '';
  if (!logs) {
    const repaired = await repairStructuredOutput<unknown[]>({
      mode: 'array',
      requiredKeys: ['severity'],
      schemaLabel: 'specialized_findings',
      first: { exitCode, allOutput, lastResultText },
      reopenPrompt: `Your prior performance analysis response did not end with a parseable JSON findings array.
Output ONLY the JSON array now — each finding must include at least "severity" plus area, title, description, fix_prompt, and related_files. Use [] if there are no findings. No markdown, no code fences, no prose.`,
      ws,
      statusRef,
      model,
      effort,
      disallowedTools,
      retryStatus: 'Repairing performance report JSON...',
    });
    if (repaired.value !== null) {
      logs = sanitize(JSON.stringify(repaired.value));
    } else {
      logs = sanitize(lastResultText || allOutput.slice(-50_000));
    }
  }

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' } : { artifact_type: 'PERF_REPORT' }),
    logs,
  });
}

function formatRecentChanges(recentChanges: unknown): string {
  if (!recentChanges) return '';
  const text = typeof recentChanges === 'string' ? recentChanges.trim() : JSON.stringify(recentChanges, null, 2);
  return text ? `\n\nRecent changes:\n${text.slice(0, 4096)}` : '';
}

// ── Handler: Investigate (analyze issue, output structured diagnosis report) ──
export async function handleInvestigate(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = resolveBaseBranch(payload, task);
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  const userPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);
  // Match handleMigrate (line 133): a missing prompt produces a malformed
  // template "Issue to investigate:\n\nInstructions:..." that Claude can't
  // act on, leading to a 3-second silent failure (status PROVISIONING →
  // IN_PROGRESS → FAILED with "no JSON object with root_cause found in
  // transcript"). Fail loud instead so the orchestrator surfaces a real
  // diagnosis rather than retrying the same empty prompt 3×.
  if (!userPrompt) throw new Error('No prompt found in task payloads (investigate)');

  statusRef.value = `Cloning ${owner}/${repo} for investigation...`;
  sendLog(ws, `Starting investigation...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const investigateFetchGuidance = provisionFetchHelper(taskId, task);
  const disallowedTools = configureAgentPermissions('investigate');
  const investigateRetryContext = buildRetryContext(task);
  const investigateFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  const recentChangesSection = formatRecentChanges(payload.recent_changes);
  // Shared launch-context block; degrades to '' when absent (graceful). This is
  // the equipping gap vs. the near-twin debug handler, which already includes it.
  const investigateContextPrefix = buildContextPrefix(task);
  const investigateContextSection = investigateContextPrefix ? `${investigateContextPrefix}\n` : '';
  // Bug-report citation provenance carried on the spawned task's context (e.g.
  // by admin.py / research_completion when the investigation originates from a
  // bug-report message). Renders the specific cited refs that did not resolve on
  // HEAD — framed as a stronger discount note for a fully-absent report and a
  // softer "verify these refs, defect still actionable" note for a partially
  // grounded one. Empty string when no signal is present (fingerprint-stable).
  const investigateCtx = task.context as Record<string, unknown> | undefined;
  const investigateCitationProvenance = formatInvestigateCitationProvenance(investigateCtx);

  // ── Adversarial spawn-gate grader branch ──
  // spawn_forward_quality_grader(adversarial_mode=True) spawns a task of
  // type "investigate" whose payload prompt (build_adversarial_finding_prompt)
  // already specifies its own output contract: a JSON array of
  // [{hash, verdict, reason}] objects. Wrapping that prompt in the standard
  // investigation template below would instruct Claude to instead write a
  // single {summary, root_cause, ...} object with NO `hash` field, so the
  // backend parser (parse_adversarial_finding_verdicts, anchored on the
  // ['hash','verdict'] keywords) finds no array and every finding fails closed
  // with "no verdict returned". When this is an adversarial grader, run the raw
  // payload prompt and let its [{hash, verdict, reason}] output flow straight
  // through to `logs` unmodified — _resolve_quality_grader_completion reads
  // payload.logs directly.
  if (investigateCtx?.adversarial_grader === true) {
    statusRef.value = 'Running adversarial finding challenge...';
    const challengeFingerprint = computePromptFingerprint('investigate');
    setCurrentPromptFingerprint(challengeFingerprint);
    sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: challengeFingerprint });
    const challengeFetchBlock = `\n\n${investigateFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
    const challengeFullPrompt = `${userPrompt}${challengeFetchBlock}`;
    const challengeTelemetry = buildPromptSizeTelemetry(challengeFullPrompt, [
      ['user_prompt', userPrompt],
      ['fetch_tool', challengeFetchBlock],
    ]);
    setPromptSizeTelemetry(challengeTelemetry.prompt_size_bytes, challengeTelemetry.prompt_section_count, challengeTelemetry.prompt_section_sizes);
    const { exitCode, allOutput, lastResultText } = await runClaudeCode(
      challengeFullPrompt,
      ws, statusRef, model, effort, disallowedTools
    );
    // Locally validate the output is a parseable [{hash, verdict, reason}] array.
    // On a format miss with exitCode===0, perform one bounded focused re-ask
    // (matching the sibling handlers' self-repair path). The challenged hashes are
    // included so the model can re-emit a complete array. On repair success, post
    // the repaired array. On failure, pass the raw output through unchanged so the
    // backend parser fail-closes findings (preserving current fail-closed behavior).
    const challengedHashes = Array.isArray(investigateCtx._adversarial_challenged_hashes)
      ? (investigateCtx._adversarial_challenged_hashes as string[])
      : [];
    const hashListStr = challengedHashes.length > 0
      ? ` The following finding hashes must each appear in the array: ${challengedHashes.map(h => JSON.stringify(h)).join(', ')}.`
      : '';
    const RECOGNIZED_VERDICTS = new Set([
      'pass', 'passed', 'approve', 'approved',
      'fail', 'failed', 'reject', 'rejected',
    ]);
    const repaired = await repairStructuredOutput<Array<Record<string, unknown>>>({
      mode: 'array',
      requiredKeys: ['hash', 'verdict'],
      first: { exitCode, allOutput, lastResultText },
      reopenPrompt: `Your prior adversarial challenge response did not end with a parseable JSON verdict array.
Return ONLY the JSON array. Each element must have "hash", "verdict", and "reason" fields.
The "verdict" value MUST be exactly "pass" or "fail" — no other values are accepted.${hashListStr} No markdown, prose, or code fences.`,
      ws,
      statusRef,
      model,
      effort,
      disallowedTools,
      retryStatus: 'Repairing adversarial challenge verdict array...',
      schemaLabel: 'research_spawn_adversarial_challenge',
      validateParsedValue: (value) => {
        const arr = value as Array<Record<string, unknown>>;
        const unrecognized = arr
          .map(item => {
            const raw = item['verdict'];
            return (typeof raw === 'string' ? raw : '').trim().toLowerCase();
          })
          .filter(v => !RECOGNIZED_VERDICTS.has(v));
        if (unrecognized.length > 0) {
          return {
            ok: false,
            reason: 'parsed_array_invalid_value',
            detail: `unrecognized verdict strings: ${unrecognized.join(', ')}`,
          };
        }
        return { ok: true };
      },
    });
    let challengeLogs: string;
    if (repaired.value !== null && repaired.retry !== null) {
      // A focused re-ask succeeded with recognized verdicts — post the repaired array.
      challengeLogs = sanitize(JSON.stringify(repaired.value));
    } else {
      const rawOutput = lastResultText || (allOutput || '').slice(-50_000);
      // Guard: if the raw output is an investigation-report envelope (a
      // {summary, root_cause, ...} object), the backend parser anchored on a
      // top-level array would find no array and fail-close all findings.  Post
      // the already-extracted valid array instead so the verdicts flow through.
      if (repaired.value !== null) {
        let isInvestigationReportEnvelope = false;
        try {
          const parsed = JSON.parse(rawOutput) as unknown;
          isInvestigationReportEnvelope = (
            parsed !== null &&
            typeof parsed === 'object' &&
            !Array.isArray(parsed) &&
            'summary' in (parsed as Record<string, unknown>)
          );
        } catch { /* not a JSON object — safe to pass through */ }
        if (isInvestigationReportEnvelope) {
          challengeLogs = sanitize(JSON.stringify(repaired.value));
        } else {
          challengeLogs = sanitize(rawOutput);
        }
      } else {
        // First attempt was already parseable (retry===null), repair failed, or
        // exitCode!==0 — pass raw output through unchanged.
        challengeLogs = sanitize(rawOutput);
      }
    }
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
      ...(exitCode !== 0
        ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' }
        : { artifact_type: 'INVESTIGATION_REPORT' }),
      logs: challengeLogs,
    });
    return;
  }

  const investigateCoreBody = `You are an investigation agent. You have full read access to this repository. Your job is to analyze the issue described below and produce a structured diagnosis report.
Use the Recent changes section first to correlate the issue with the latest deploy window and recently merged changes.

Issue to investigate:
${userPrompt}${recentChangesSection}${investigateCitationProvenance}

Instructions:
1. Read relevant source files to understand the codebase and locate the root cause of the issue.
2. DO NOT make any changes to any files — this is a read-only analysis.
3. If the user prompt above frames this as a regression / signal trip / pipeline anomaly, FIRST triage whether what was observed is actually a real regression, or whether it is intended behavior, expected variance, or a known-incomplete pipeline step. When the prompt explicitly asks for a verdict, you MUST emit one.
4. Otherwise, identify the root cause, affected files, and a recommended fix.
5. Write your diagnosis as a JSON object to /tmp/investigation_report_${taskId}.json ${READ_ONLY_BASH_WRITE_NOTE} with EXACTLY this schema:

{
  "summary": "Brief summary of the issue and findings",
  "root_cause": "Detailed explanation of the root cause",
  "affected_files": [
    { "path": "path/to/file.ts", "issue": "Description of the problem in this file" }
  ],
  "recommended_fix": "Concrete description of what needs to be changed to fix the issue",
  "severity": "critical|high|medium|low",
  "confidence": "high|medium|low",
  "verdict": "real_regression|not_a_regression",
  "verdict_reason": "One to three sentences citing concrete evidence (file paths, design doc passages, prior commit SHAs) that justify the verdict. Required when verdict='not_a_regression', optional otherwise."
}

verdict guidance (only meaningful for regression / signal-trip prompts; default 'real_regression' if the prompt isn't framed that way):
- "real_regression": the observed behavior is a genuine bug, regression, or unintended degradation. Diagnose normally and populate root_cause / recommended_fix.
- "not_a_regression": set this when ANY of the following apply, and leave recommended_fix empty (do NOT fabricate a fix):
  * The observed behavior is intentional / by design (feature flag deliberately gating a step, recently merged change altered the baseline, design doc explicitly documents the behavior).
  * Sample size is too small for the prior/current delta to be statistically meaningful.
  * The 'regression' is just a known-incomplete pipeline step (e.g. the agent did not run the full backend test suite — that's documented design, not a bug).
  * The signal observed transient infrastructure noise (one-off VM crash, flaky external dep) with no code-side root cause.
  Your verdict_reason must cite concrete evidence — vague claims ("seems intentional") are rejected; the dispatcher will still suppress respawning for the TTL but operators will lose trust in the verdict.

Confidence scoring criteria (be honest — low confidence triggers follow-up investigation, medium/high triggers an automated remediation task):
- "high": You found the exact code location causing the failure with clear evidence — a specific function, wrong condition, missing check, or faulty expression directly explains the observed behavior. A fix can be written without further investigation.
- "medium": Strong hypothesis supported by code analysis, but some uncertainty remains — likely cause identified but you could not reproduce it, verify it end-to-end, or rule out other contributors.
- "low": Root cause unclear. Multiple plausible explanations, insufficient evidence in the code, or the failure could be environmental/transient. Prefer this over guessing.

recommended_fix guidance:
- Write it as a self-contained prompt for an implementing agent that will NOT see this investigation's full context. Be concrete and specific.
- Good: "Add a null check for \`user.profile\` in \`getUserDisplayName\` (src/user.ts:42) before accessing \`.name\`; return the fallback 'Anonymous' when profile is null."
- Bad: "Fix the error handling" / "Improve null safety" / "Handle the edge case".
- Name the file paths, function names, and exact conditions to change. Include the intended behavior, not just the symptom.

IMPORTANT:
- Write ONLY the JSON object to /tmp/investigation_report_${taskId}.json ${READ_ONLY_BASH_WRITE_NOTE} — no markdown, no code fences, no extra text.
- Also output the same JSON object as the final output in your logs.
- Do NOT modify any files in the repository.
- Base every claim on code you have actually read.`;
  const investigatePrompt = `${investigateContextSection}${investigateRetryContext}${investigateCoreBody}`;

  statusRef.value = 'Running investigation...';
  const investigateFingerprint = computePromptFingerprint('investigate');
  setCurrentPromptFingerprint(investigateFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: investigateFingerprint });
  const investigateParallelGuidance = isParallelAuditEnabled(task) ? PARALLEL_AUDIT_GUIDANCE : '';
  const investigatePlaybookNote = '\nNote: If your investigation revealed a root cause pattern, diagnostic technique, or system behavior that would help future debugging, save it to the playbook.\n';
  const investigateFetchBlock = `\n\n${investigateFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const investigateFullPrompt = investigatePrompt + investigateFirstTaskSection + investigateParallelGuidance + PLAYBOOK_GUIDANCE + investigatePlaybookNote + investigateFetchBlock;
  const investigateTelemetry = buildPromptSizeTelemetry(investigateFullPrompt, [
    ['context', investigateContextSection],
    ['retry_context', investigateRetryContext],
    ['core_prompt', investigateCoreBody],
    ['first_task', investigateFirstTaskSection],
    ['parallel_audit', investigateParallelGuidance],
    ['playbook', PLAYBOOK_GUIDANCE],
    ['playbook_note', investigatePlaybookNote],
    ['fetch_tool', investigateFetchBlock],
  ]);
  setPromptSizeTelemetry(investigateTelemetry.prompt_size_bytes, investigateTelemetry.prompt_section_count, investigateTelemetry.prompt_section_sizes);
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(investigateFullPrompt, ws, statusRef, model, effort, disallowedTools);

  let investigationResult = readHandlerOutput(taskId, 'investigation_report', ws);
  if (investigationResult === null) {
    const requiredFields = ['summary', 'root_cause', 'affected_files', 'recommended_fix', 'severity', 'confidence'];
    const repaired = await repairStructuredOutput<Record<string, unknown>>({
      mode: 'object',
      requiredKeys: ['root_cause', 'recommended_fix'],
      schemaLabel: 'specialized_result',
      first: { exitCode, allOutput, lastResultText },
      reopenPrompt: `${(lastResultText || allOutput || '').slice(-8000) ? `${(lastResultText || allOutput || '').slice(-8000)}\n\n` : ''}Your prior investigation response did not end with a parseable JSON diagnosis object.
Output ONLY the JSON object now — with top-level "summary", "root_cause", "affected_files", "recommended_fix", "severity", and "confidence". Include verdict fields when relevant. No markdown, no code fences, no prose.`,
      ws,
      statusRef,
      model,
      effort,
      disallowedTools,
      retryStatus: 'Repairing investigation report JSON...',
      // Enforce the full 6-field contract here so recordFormatRepairSuccess only
      // fires when the caller will actually accept the value (not merely when the
      // two extractor keys root_cause + recommended_fix happen to parse).
      validateParsedValue: (value) => {
        const obj = value as Record<string, unknown>;
        const missingFields = requiredFields.filter(k => !(k in obj));
        if (missingFields.length > 0) {
          return {
            ok: false,
            reason: 'parsed_object_missing_key',
            detail: `missing required fields: ${missingFields.join(', ')}`,
          };
        }
        return { ok: true };
      },
    });
    const repairedValue = repaired.value;
    if (repairedValue !== null && requiredFields.every(k => k in repairedValue)) {
      investigationResult = repairedValue;
      sendLog(ws, `[WARNING] Investigation JSON recovered from transcript or focused retry rather than file.\n`);
    }

    if (investigationResult === null) {
      // Surface the real Claude exit code + transcript tail so the orchestrator
      // can distinguish "Claude crashed" (3-second deaths with empty allOutput)
      // from "Claude ran but didn't write the report" (exitCode 0, transcript
      // exists, just no JSON). The bare "Failed to read investigation report"
      // message previously hid both modes.
      const tail = (allOutput || '').slice(-2000);
      const lastResultTail = (lastResultText || '').slice(-500);
      const logsBlob =
        `Failed to read investigation report from /tmp/investigation_report_${taskId}.json; ` +
        `no JSON object with root_cause found in transcript ` +
        `(exitCode=${exitCode}, allOutput=${(allOutput || '').length}b, ` +
        `lastResultText=${(lastResultText || '').length}b)\n` +
        `--- lastResultText tail ---\n${lastResultTail}\n` +
        `--- allOutput tail ---\n${tail}`;
      const rateLimitDetected = matchesRateLimit((allOutput || '') + '\n' + (lastResultText || ''));
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: 'FAILED',
        artifact_type: 'ERROR_LOG',
        logs: sanitize(logsBlob),
        failure_category: rateLimitDetected ? 'rate_limited' : 'parse_failed',
      });
      return;
    }
  }

  const logs = sanitize(JSON.stringify(investigationResult));
  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' } : { artifact_type: 'INVESTIGATION_REPORT' }),
    logs,
  });
}

/**
 * Assemble the repo-bootstrap prompt so the automatically-cacheable prefix is
 * maximized: all stable literal/config guidance (core bootstrap instructions,
 * default tool guidance, fetch guidance, CLAUDE.md, playbook guidance + note)
 * forms one contiguous leading prefix, and all task-variable content
 * (contextSection, retryContext) forms a contiguous trailing tail. This is a
 * byte-ORDER change only — every section that shipped before still ships; only
 * its position moves. Anthropic prefix caching bills a cache read at ~10% of base
 * input, so a task-variable byte appearing early would bust the automatic prefix
 * match for everything after it. Because none of the prefix sections read the
 * clock or git HEAD, two builds of an identical payload produce a BYTE-IDENTICAL
 * stable prefix. Mirrors assembleStandardPrompt in standard.ts (direction
 * d-27685510).
 */
export function assembleRepoBootstrapPrompt(s: {
  bootstrapCoreBody: string;
  fetchGuidance: string;
  claudeMdSection: string;
  playbookNote: string;
  contextSection: string;
  retryContext: string;
}): { stablePrefix: string; variableBody: string; fullPrompt: string } {
  const stablePrefix =
    s.bootstrapCoreBody
    + `\n${DEFAULT_TOOL_GUIDANCE}`
    + s.fetchGuidance
    + s.claudeMdSection
    + PLAYBOOK_GUIDANCE
    + s.playbookNote;
  const variableBody = s.contextSection + s.retryContext;
  return { stablePrefix, variableBody, fullPrompt: stablePrefix + variableBody };
}

/**
 * First-cycle repo bootstrap: reads project structure, confirms build/test commands,
 * and seeds design doc sections before implementation tasks fan out.
 *
 * This is a read-only analysis task. The backend injects bootstrap_guidance into the
 * claim response with structured instructions for what to read and write back.
 * On COMPLETED, the backend completion handler stamps bootstrap_completed_at on
 * build_config so the strategic spawn gate allows implementation tasks to proceed.
 */
export async function handleRepoBootstrap(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = resolveBaseBranch(payload, task);
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  statusRef.value = `Cloning ${owner}/${repo} for bootstrap...`;
  sendLog(ws, `Starting repo bootstrap for ${owner}/${repo}...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });
  writePlaybookHelper(taskId);
  const fetchGuidance = provisionFetchHelper(taskId, task);
  const disallowedTools = configureAgentPermissions('investigate');
  const retryContext = buildRetryContext(task);
  const contextPrefix = buildContextPrefix(task);
  const contextSection = contextPrefix ? `${contextPrefix}\n` : '';

  // Extract bootstrap_guidance injected by the backend claim handler.
  const bootstrapGuidance = task.bootstrap_guidance as Record<string, unknown> | undefined;
  const filesToRead = Array.isArray(bootstrapGuidance?.files_to_read)
    ? (bootstrapGuidance.files_to_read as string[]).join('\n- ')
    : 'README.md, package.json, Makefile, Dockerfile, .github/workflows/*.yml';
  const writebackEndpoint = typeof bootstrapGuidance?.build_config_writeback_endpoint === 'string'
    ? bootstrapGuidance.build_config_writeback_endpoint
    : 'PATCH /api/agent/projects/{project_id}/build-config/commands';
  const completionPayloadField = typeof bootstrapGuidance?.completion_payload_field === 'string'
    ? bootstrapGuidance.completion_payload_field
    : 'repo_overview';

  const claudeMdContent = readClaudeMd();
  const claudeMdSection = (claudeMdContent && !providerAutoInjectsClaudeMd()) ? `\n\n## Project Instructions (CLAUDE.md)\n${claudeMdContent}` : '';

  const bootstrapCoreBody = `You are performing a first-cycle repository bootstrap for ${owner}/${repo}.

Your goal: read the repository structure, understand the tech stack and build system, confirm working build/test commands, and write a repo overview so the first strategic cycle starts with grounded repo knowledge.

## Files to read
Read as many of these as exist:
- ${filesToRead}

Also read any detected build/CI manifests you find.

## What to produce
1. **Repo overview**: Write the following JSON object to \`/tmp/bootstrap_overview_${taskId}.json\` ${READ_ONLY_BASH_WRITE_NOTE} (NOT inside the repository):
   \`\`\`json
   {
     "tech_stack": "programming language(s), key frameworks, dependency manager",
     "directory_layout": "brief description of top-level dirs and their purpose",
     "first_steps_summary": "how to clone, install deps, run build, run tests",
     "entry_points": "if discoverable: main executable(s) or server entry files",
     "test_layout": "if discoverable: test directory structure and how to run subsets",
     "key_conventions": "if discoverable: branch naming, commit style, PR conventions",
     "environment_requirements": "if discoverable: required env vars, runtime versions, .env.example notes",
     "source_revision": "checked-out HEAD SHA after clone/checkout"
   }
   \`\`\`
   The file must contain ONLY the JSON object, no markdown wrapping.
   Also include the same object in your terminal COMPLETED status payload as \`${completionPayloadField}\`.

2. **Confirm build/test/lint commands**: Attempt to run the detected build and test commands. If they succeed, include them in the write-back. Also attempt to run the detected lint command for each directory (if one is present in the build config) — lint verification is best-effort: a lint command that fails or is absent should NOT block bootstrap from completing. If the lint command succeeds, include \`confirmed_lint_cmd\` in the per-directory write-back payload. Write back using:
   \`${writebackEndpoint}\`
   with the confirmed commands per directory.

3. **Command-memory outcome**: Record the result of step 2 — separately from the overview — by writing the following JSON object to \`/tmp/command_memory_${taskId}.json\` ${READ_ONLY_BASH_WRITE_NOTE} (NOT inside the repository):
   \`\`\`json
   {
     "status": "success | missing_commands | writeback_failed",
     "detail": "optional short explanation",
     "updated_paths": ["directories you wrote back, e.g. ., backend, admin-ui"]
   }
   \`\`\`
   Use \`success\` only if the write-back call actually succeeded with at least one confirmed command. Use \`missing_commands\` if you could not confirm any runnable command, and \`writeback_failed\` if the write-back call itself errored. The file must contain ONLY the JSON object, no markdown wrapping. This is reported independently of the overview — a good overview does NOT imply confirmed commands.

4. **Domain overview**: Extract product-domain knowledge from the documentation you read and write the following JSON object to \`/tmp/bootstrap_domain_${taskId}.json\` ${READ_ONLY_BASH_WRITE_NOTE} (NOT inside the repository):
   \`\`\`json
   {
     "purpose": "2-3 sentences: what the product does and who its primary users are",
     "key_concepts": {"TermName": "1-sentence definition", "...": "..."},
     "critical_user_flows": ["top user journey 1", "top user journey 2"],
     "architectural_gotchas": ["non-obvious constraint or sharp edge", "..."],
     "source": "primary file used, e.g. README.md"
   }
   \`\`\`
   Read docs/**/*.md, ARCHITECTURE.md, DESIGN.md, OVERVIEW.md, openapi.yaml in addition to the files listed above. Cap the total at ~4000 characters. If README is absent or fewer than 200 characters, scan docs/ and top-level source docstrings. If nothing useful is found, produce a minimal stub: \`{"purpose": "No documentation found.", "key_concepts": {}, "critical_user_flows": [], "architectural_gotchas": [], "source": "none-found"}\`. The file must contain ONLY the JSON object, no markdown wrapping. Include the same object as \`domain_overview\` in the terminal COMPLETED status payload.

5. **Status**: After completing the steps above, post COMPLETED status with \`${completionPayloadField}\` and \`domain_overview\` populated. If you cannot read the repo or run commands, still complete with whatever you found.`;

  const promptFingerprint = computePromptFingerprint('repo_bootstrap');
  setCurrentPromptFingerprint(promptFingerprint);
  sendHeartbeatSync(taskId, 'Running repo bootstrap...', { prompt_fingerprint: promptFingerprint });

  const bootstrapPlaybookNote = '\nNote: Save reusable setup/environment knowledge to the playbook — environment setup steps, build quirks, version requirements, install workarounds, required env vars, and test-setup knowledge a future agent would otherwise rediscover.\n';
  // Cache-optimized assembly: stable guidance leads, task-variable content
  // (contextSection, retryContext) is relocated to a contiguous trailing tail so
  // the leading prefix is byte-identical across wall-clock/HEAD (direction
  // d-27685510). See assembleRepoBootstrapPrompt.
  const { fullPrompt: bootstrapFullPrompt } = assembleRepoBootstrapPrompt({
    bootstrapCoreBody,
    fetchGuidance,
    claudeMdSection,
    playbookNote: bootstrapPlaybookNote,
    contextSection,
    retryContext,
  });
  const bootstrapTelemetry = buildPromptSizeTelemetry(bootstrapFullPrompt, [
    ['context', contextSection],
    ['core_prompt', bootstrapCoreBody],
    ['default_tool', DEFAULT_TOOL_GUIDANCE],
    ['fetch_guidance', fetchGuidance],
    ['claude_md', claudeMdSection],
    ['retry_context', retryContext],
    ['playbook', PLAYBOOK_GUIDANCE],
    ['playbook_note', bootstrapPlaybookNote],
  ]);
  setPromptSizeTelemetry(bootstrapTelemetry.prompt_size_bytes, bootstrapTelemetry.prompt_section_count, bootstrapTelemetry.prompt_section_sizes);
  const { exitCode, allOutput } = await runClaudeCode(
    bootstrapFullPrompt,
    ws, statusRef, model, effort, disallowedTools
  );

  const rateLimitDetected = matchesRateLimit(allOutput || '');
  const parsedBootstrapOverview = exitCode === 0 ? readHandlerOutput(taskId, 'bootstrap_overview', ws) : null;
  const bootstrapOverview = isRepoBootstrapOverview(parsedBootstrapOverview) ? parsedBootstrapOverview : null;
  // Command-memory outcome is read and reported independently of the overview:
  // a present overview must not imply confirmed commands. An absent or malformed
  // file leaves command_memory unset, which the backend degrades to "unknown".
  const parsedCommandMemory = exitCode === 0 ? readHandlerOutput(taskId, 'command_memory', ws) : null;
  const commandMemory = isCommandMemoryOutcome(parsedCommandMemory)
    ? normalizeCommandMemoryOutcome(parsedCommandMemory)
    : null;
  // Domain overview is best-effort: absent or malformed file is a clean no-op.
  const parsedDomainOverview = exitCode === 0 ? readHandlerOutput(taskId, 'bootstrap_domain', ws) : null;
  const domainOverview = isRepoDomainOverview(parsedDomainOverview) ? parsedDomainOverview : null;
  const logs = bootstrapOverview !== null
    ? sanitize(JSON.stringify(bootstrapOverview))
    : sanitize(allOutput ? allOutput.slice(-500) : 'Bootstrap complete');

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0
      ? { artifact_type: 'ERROR_LOG', failure_category: rateLimitDetected ? 'rate_limited' : 'claude_code_failed' }
      : {}),
    ...(bootstrapOverview !== null ? { repo_overview: bootstrapOverview } : {}),
    ...(commandMemory !== null ? { command_memory: commandMemory } : {}),
    ...(domainOverview !== null ? { domain_overview: domainOverview } : {}),
    logs,
  });
}

// ── Handler: Walkthrough E2E (qualifier-spawned live-stack walkthrough) ──────
export async function handleWalkthroughE2e(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = resolveBaseBranch(payload, task);
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  const candidateSha = resolveCandidateSha(payload, task);

  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(
    ws,
    candidateSha
      ? `Cloning ${owner}/${repo} @ ${baseBranch}, pinning candidate ${candidateSha.slice(0, 12)} for walkthrough e2e...\n`
      : `Cloning ${owner}/${repo} @ ${baseBranch} for walkthrough e2e...\n`,
  );
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: candidateSha ? 0 : 100,
  });

  if (candidateSha) {
    sendLog(ws, `Fetching + checking out candidate commit ${candidateSha.slice(0, 12)}...\n`);
    try {
      execFileSync('git', ['fetch', 'origin', candidateSha], { cwd: WORKSPACE, stdio: ['ignore', 'pipe', 'pipe'] });
      execFileSync('git', ['checkout', '--detach', candidateSha], { cwd: WORKSPACE, stdio: ['ignore', 'pipe', 'pipe'] });
      sendLog(ws, `Checked out candidate commit ${candidateSha.slice(0, 12)} (detached HEAD).\n`);
    } catch (e) {
      const detail = sanitizeError(e);
      sendLog(ws, `[ERROR] Could not check out candidate commit ${candidateSha}: ${detail}\n`);
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: 'FAILED',
        artifact_type: 'ERROR_LOG',
        failure_category: 'clone_failed',
        logs: sanitize(
          `Qualifier walkthrough e2e aborted: could not check out the pinned ` +
          `candidate commit ${candidateSha}. Detail: ${detail}`,
        ),
      });
      return;
    }
  }

  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const fetchGuidance = provisionFetchHelper(taskId, task);
  const disallowedTools = configureAgentPermissions('smoketest');

  const retryContext = buildRetryContext(task);
  const contextPrefix = buildContextPrefix(task);
  const contextSection = contextPrefix ? `${contextPrefix}\n` : '';
  const candidateShaClause = candidateSha
    ? `The walkthrough runs against the pinned candidate SHA: ${candidateSha}. Verify the SHA is real with \`git cat-file -t ${candidateSha}\` before writing any evidence.`
    : 'No candidate SHA pinned — run the walkthrough against the current HEAD.';

  const coreBody = `You are a walkthrough e2e agent. Your job is to run the live-stack walkthrough harness and write a structured result file.

${candidateShaClause}

## Steps

1. **Seed fixture data** — run \`python3 scripts/walkthrough/seed_test_data.py\` to idempotently seed the fixture project and owner membership so ADMIN_API_KEY can drive project-scoped endpoints.

2. **Run the walkthrough harness** — run \`node scripts/walkthrough/v26-live-walkthrough.cjs\`. The harness tests live enrollment, heartbeat, and project-tool token flows. Capture its stdout/stderr.

3. **Parse per-flow verdicts** — the harness reports each flow as PASS / FAIL / DEAD_END. Extract these into a structured list.

4. **Write the result file** — write \`/tmp/walkthrough_e2e_result_${taskId}.json\` with this shape:
\`\`\`json
{
  "flows": [
    {
      "flow": "<criterion_key e.g. c-dfdc9d82>",
      "verdict": "PASS",
      "candidate_sha": "<40-char sha>",
      "screenshot_md5": ["<md5>", "..."],
      "heartbeat_confirmed": true,
      "ran_at": "<iso timestamp>"
    }
  ],
  "harness_exit_code": 0,
  "summary": "brief summary"
}
\`\`\`
Only include a flow entry if the harness explicitly tested it. Set verdict to FAIL or DEAD_END when it did not pass. Only PASS flows receive CriterionEvidence rows; FAIL/DEAD_END leave the criterion uncovered — fail-closed.

IMPORTANT: Always write /tmp/walkthrough_e2e_result_${taskId}.json, including when the harness cannot run. Never leave the file unwritten.`;

  const fetchBlock = `\n\n${fetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const fullPrompt = `${contextSection}${retryContext}${coreBody}${fetchBlock}${PLAYBOOK_GUIDANCE}`;

  statusRef.value = 'Running walkthrough e2e harness...';
  const fingerprint = computePromptFingerprint('walkthrough_e2e');
  setCurrentPromptFingerprint(fingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: fingerprint });
  const { exitCode, allOutput } = await runClaudeCode(fullPrompt, ws, statusRef, model, effort, disallowedTools);

  const rawResult = exitCode === 0 ? readHandlerOutput(taskId, 'walkthrough_e2e_result', ws) : null;
  const result = rawResult && typeof rawResult === 'object' && Array.isArray((rawResult as Record<string, unknown>).flows)
    ? rawResult as { flows: unknown[]; harness_exit_code?: number; summary?: string }
    : null;

  const taskContext = task.context && typeof task.context === 'object'
    ? task.context as Record<string, unknown>
    : {};
  const qualifierWalkthrough = taskContext.qualifier_walkthrough_e2e === true;

  if (exitCode === 0 && result) {
    const passCount = result.flows.filter(
      (f) => typeof f === 'object' && f !== null && (f as Record<string, unknown>).verdict === 'PASS'
    ).length;
    const summary = result.summary ?? `Walkthrough e2e: ${passCount}/${result.flows.length} flows PASS`;
    sendLog(ws, `\n${summary}\n`);
    const terminalStatus = passCount > 0 || qualifierWalkthrough ? 'COMPLETED' : 'FAILED';
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: terminalStatus,
      artifact_type: 'WALKTHROUGH_E2E_RESULT',
      logs: JSON.stringify(result),
      repo_url: `https://github.com/${owner}/${repo}`,
    });
  } else {
    const unparseable = exitCode === 0;
    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
      logs: sanitize(allOutput.slice(-10_000)),
      failure_category: matchesRateLimit(allOutput) ? 'rate_limited' : (unparseable ? 'walkthrough_result_unparseable' : 'claude_code_failed'),
    });
  }
}
