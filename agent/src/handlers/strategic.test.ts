import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import fs from 'fs';

// Capture the prompt the handler hands to the thinker (analysis) pass.
let capturedThinkerPrompt: string | undefined;
// Capture the params the handler passes to buildThinkerPrompt so tests can
// assert on derived values (problem_model recovery framing).
let capturedThinkerParams: Record<string, unknown> | undefined;

const {
  INDEX_EXPORT_NAMES,
  STRATEGIC_HELPERS_EXPORT_NAMES,
  DESIGN_DOC_PRESENCE_EXPORT_NAMES,
  RESEARCH_DIRECTION_CONTEXT_EXPORT_NAMES,
  RESEARCH_PLAN_EXPORT_NAMES,
  STRATEGIC_SECTIONS_EXPORT_NAMES,
  TELEMETRY_EXPORT_NAMES,
} = vi.hoisted(() => {
  // Type-safe export name lists: satisfies ensures each name exists in the actual module exports
  type IndexExportKey = keyof typeof import('../index.js');
  type StrategicHelpersExportKey = keyof typeof import('./strategic-helpers.js');
  type DesignDocPresenceExportKey = keyof typeof import('../utils/design-doc-presence.js');
  type ResearchDirectionContextExportKey = keyof typeof import('./research-direction-context.js');
  type ResearchPlanExportKey = keyof typeof import('./research-plan.js');
  type StrategicSectionsExportKey = keyof typeof import('./strategic-sections.js');
  type TelemetryExportKey = keyof typeof import('../telemetry.js');

  return {
    INDEX_EXPORT_NAMES: [
    'AUTH_MODE',
    'AuthFailureError',
    'ClaudeTimeoutError',
    'GIT_REFERENCE_CACHE_ROOT',
    'GracefulShutdownSignal',
    'LLM_CLI_TRANSLATION',
    'LLM_EXECUTION_MODE',
    'MATERIALIZE_SCRIPT_PATH',
    'NoProgressTimeoutError',
    'PROVIDER',
    'RateLimitError',
    'RuntimeUnavailableError',
    'TASK_BRANCH_NAMESPACE_DEFAULT',
    'TASK_TYPE_BRANCH_PREFIX',
    'VmConfigMissingError',
    'WORKSPACE',
    '__resetTaskCapabilityContractLoadedForTesting',
    '__setCommandExistsOverrideForSmoke',
    '__setRunClaudeCodeOverrideForSmoke',
    '__setWarmRetargetDepsForTesting',
    '_buildHeartbeatStatusForTesting',
    '_captureBootCredentialSnapshotForTesting',
    '_captureBootProviderConfigPresenceForTesting',
    '_consumePendingParkDirectiveForTesting',
    '_crashReportForTesting',
    '_getActiveCredentialStateSourceForTesting',
    '_getCrashReportTaskIdForTesting',
    '_getFirstToolUseAtForTesting',
    '_getHandlerDurationForTesting',
    '_getJobModeForTesting',
    '_getRoleClaimTimestampForTesting',
    '_getWarmStartForTesting',
    '_isCancelRequestedForTesting',
    '_materializeClaimCredentialStateForTesting',
    '_resetCancelRequestedForTesting',
    '_resetCredentialStateForTesting',
    '_resetTelemetryForTesting',
    '_setAgentRuntimeFingerprintForTesting',
    '_setAgentSourceShaForTesting',
    '_setAgentTokenForTesting',
    '_setCancelRequestedForTesting',
    '_setHandlerDurationForTesting',
    '_setInWarmPollWindowForTesting',
    '_setJobModeForTesting',
    '_setRoleClaimTimestampForTesting',
    '_setWarmStartForTesting',
    '_startHeartbeatForTesting',
    '_stopHeartbeatForTesting',
    'apiGet',
    'apiPost',
    'apiRequest',
    'appendToLogBuffer',
    'bootstrapPoolMode',
    'buildTargetedPytestRunners',
    'captureTestBaseline',
    'claimTask',
    'clearLastProviderRunErrorReport',
    'clearLogBuffer',
    'clearToolTrace',
    'cloneWithRetry',
    'codexStreamHasTurnCompleted',
    'collectFixLoopEventsForFlush',
    'computePromptFingerprint',
    'computeRefreshDelayMs',
    'condensedAnalysisSections',
    'continueWarmContinuation',
    'createStaleResultEnvelope',
    'deriveTaskBranch',
    'detectLoopDivergence',
    'detectTestRunners',
    'ensureTaskCapabilityContractLoaded',
    'errorFingerprint',
    'estimateTokensFromChars',
    'execFileAsync',
    'extractInvestigatedFiles',
    'extractPytestSelectorsFromOutput',
    'fetchTaskCapabilityContract',
    'fetchVmImageIdentity',
    'getAgentToken',
    'getCurrentPromptFingerprint',
    'getFormatComplianceTelemetry',
    'getLastProviderRunErrorReport',
    'getOperatorDecisionCaptureTelemetry',
    'getLlmTelemetry',
    'getProviderRunner',
    'getResidentTurnLatencyTelemetry',
    'git',
    'gitAsync',
    'isBootstrapToken',
    'isTaskNoLongerActiveError',
    'listOpenPullRequestsForBranch',
    'log',
    'main',
    'markStaleResultFailure',
    'matchesAuthFailure',
    'matchesRateLimit',
    'matchesVmConfigMissing',
    'missingProviderConfigFiles',
    'missingVmProviderConfigFiles',
    'normalizePytestSelectorForRunner',
    'parseFailingTests',
    'parseFailingTestsFromVitestJson',
    'parseLLMCliTranslation',
    'parseStaleResultFile',
    'performWarmRetarget',
    'persistAndPostStatus',
    'pollForNextRole',
    'postTerminalStatusSync',
    'providerSupportsNativeSubagents',
    'pushAndCreatePR',
    'readAgentRuntimeFingerprint',
    'readAgentSourceSha',
    'readHandlerOutput',
    'recordFormatMiss',
    'recordFormatRepairAttempt',
    'recordFormatRepairSuccess',
    'recordInitialOAuthAuthHash',
    'recordLlmCallStart',
    'recordLlmTokens',
    'recordProviderReportedUsage',
    'recordNumTurns',
    'recordOperatorDecisionCapture',
    'recordResidentTurnLatency',
    'recordToolUse',
    'recordToolResultBytes',
    'refreshRuntimeState',
    'registerSizeCapMergeDriver',
    'replayLogBufferRateLimited',
    'requiredProviderConfigFiles',
    'resetFormatComplianceTelemetry',
    'resetLlmTelemetry',
    'resetOperatorDecisionCaptureTelemetry',
    'resetPerRoleModuleState',
    'resetResidentTurnLatencyTelemetry',
    'resolveBaseBranch',
    'resolveCredentialsFromSecretVersion',
    'resolveTaskBranchNamespace',
    'rotateOAuthCredentialsIfChanged',
    'runClaudeCode',
    'runProviderAgent',
    'sanitize',
    'sanitizeError',
    'sendHeartbeatSync',
    'sendLog',
    'sendWsFrame',
    'setCurrentPromptFingerprint',
    'setupWorkspace',
    'shouldAbandonStaleResult',
    'snapshotLogBuffer',
    'snapshotToolTrace',
    'staleResultBackoffMs',
    'strVal',
    'testFixLoop',
    'validateBranchName',
  ] satisfies IndexExportKey[],
    STRATEGIC_HELPERS_EXPORT_NAMES: [
    'DESIGN_DOC_FETCH_VERBS',
    'FETCH_VERB_HELP',
    'PLAN_FETCH_VERBS',
    'RESEARCH_FETCH_VERBS',
    'STRATEGIC_CHALLENGE_VERSION',
    'STRATEGIC_FETCH_VERBS',
    'blockedChallengeRecord',
    'buildFetchGuidance',
    'buildStrategicChallengeExecutorSection',
    'buildStrategicChallengePrompt',
    'parseStrategicChallengeRecord',
    'portfolioOperationFingerprint',
    'renderStrategicFetchVerbHelpLines',
    'runStrategicChallenge',
    'writeFetchHelper',
    'writeStrategicHelpers',
  ] satisfies StrategicHelpersExportKey[],
    DESIGN_DOC_PRESENCE_EXPORT_NAMES: [
    'extractEntryPaths',
    'reconcileDesignDocIndex',
    'reconcileDesignDocPresence',
    'reconcileEntry',
    'reconcileMissingCapabilities',
    'reconcileSystemEntry',
  ] satisfies DesignDocPresenceExportKey[],
    RESEARCH_DIRECTION_CONTEXT_EXPORT_NAMES: [
    'differentiationHintFor',
    'formatCochangeHints',
    'formatDecompositionOutcomes',
    'formatPastRetirementCritiques',
    'formatPreviousDecisionOutcomes',
    'formatRetiredDirectionsSummary',
    'formatRetiredDirectionGrounding',
  ] satisfies ResearchDirectionContextExportKey[],
    RESEARCH_PLAN_EXPORT_NAMES: [
    'CAPABILITY_DECLARATION_DOC',
    'PR_INELIGIBLE_TASK_TYPES',
    'QUALIFICATION_STATE_VOCAB',
    'aggregateDirectionStats',
    'buildAvailableCapabilitiesSection',
    'buildCapabilityVerificationSection',
    'buildEfficiencySnapshotSection',
    'buildEmptyRepoGuidance',
    'buildFilteredFindingsSection',
    'buildMemoCitationsSection',
    'buildOperatorOverridesSection',
    'buildPortfolioComposition',
    'buildRejectedProposalsSection',
    'buildRunningUpdateSupersededSection',
    'buildScopeCalibrationSignal',
    'buildStrategicCompletionLogs',
    'buildThinkerPrompt',
    'buildUserPausedSection',
    'buildVersionPhaseSection',
    'calcMessageAge',
    'classifyFailureCategory',
    'formatCochangeHints',
    'formatCriteriaRubric',
    'formatDecompositionOutcomes',
    'formatPastRetirementCritiques',
    'formatPreviousDecisionOutcomes',
    'formatRetiredDirectionsSummary',
    'handlePlan',
    'handleResearch',
    'normalizePlanSubtaskTypes',
    'parseResearchContext',
    'readProgressArtifact',
    'resolveMemoHandles',
    'runThinkerWithGate',
  ] satisfies ResearchPlanExportKey[],
    STRATEGIC_SECTIONS_EXPORT_NAMES: [
    'buildCapabilityVerificationSection',
    'buildCiComplianceSection',
    'buildCompletionDebtSection',
    'buildClockAndRecentWorkSection',
    'buildConversationThreadSection',
    'buildCycleStartReconciliationSection',
    'buildDirectionPortfolioSection',
    'buildDirHealthSummary',
    'buildDirSummaryText',
    'buildDriftSection',
    'buildFeedbackSection',
    'buildHeldDirectionsSection',
    'buildModelHealthSection',
    'buildOpenMessagesText',
    'buildOutcomeSection',
    'buildPendingFollowupsSection',
    'buildPayloadStalenessSection',
    'buildPruneCandidatesSection',
    'buildQuestionSection',
    'buildSweepComparisonSection',
    'buildSystemMaturitySection',
    'fmtScalar',
    'formatRecentHandledConversation',
  ] satisfies StrategicSectionsExportKey[],
    TELEMETRY_EXPORT_NAMES: [
    'buildPromptSizeTelemetry',
    'computePromptFingerprint',
    'errorFingerprint',
    'estimateTokensFromChars',
    'getCurrentPromptFingerprint',
    'getFormatComplianceTelemetry',
    'getLlmTelemetry',
    'getOperatorDecisionCaptureTelemetry',
    'getResidentTurnLatencyTelemetry',
    'recordFormatMiss',
    'recordFormatRepairAttempt',
    'recordFormatRepairInfraFailure',
    'recordFormatRepairMiss',
    'recordFormatRepairSuccess',
    'recordFormatRepairTokens',
    'recordLlmCallStart',
    'recordLlmTokens',
    'recordNumTurns',
    'recordOperatorDecisionCapture',
    'recordProviderReportedUsage',
    'recordResidentTurnLatency',
    'recordToolResultBytes',
    'recordViewportScreenshotDownload',
    'resetFormatComplianceTelemetry',
    'resetLlmTelemetry',
    'resetOperatorDecisionCaptureTelemetry',
    'resetResidentTurnLatencyTelemetry',
    'restoreOperatorDecisionCaptureTelemetry',
    'setCurrentPromptFingerprint',
    'setPromptSizeTelemetry',
  ] satisfies TelemetryExportKey[],
  };
});

vi.mock('fs', () => ({
  default: {
    existsSync: vi.fn().mockReturnValue(false),
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
  },
}));

vi.mock('../index.js', () => ({
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
  ...Object.fromEntries(INDEX_EXPORT_NAMES.map((name) => [name, vi.fn()])),
  WORKSPACE: '/tmp/workspace',
  strVal: (v: unknown, def = '') => (typeof v === 'string' ? v : def),
  sanitize: (s: string) => s,
  sendLog: vi.fn(),
  readHandlerOutput: vi.fn().mockReturnValue(null),
  persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
  runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
  apiPost: vi.fn().mockResolvedValue({}),
  resolveBaseBranch: () => 'main',
  setCurrentPromptFingerprint: vi.fn(),
  sendHeartbeatSync: vi.fn(),
}));

vi.mock('../workspacePrep.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../workspacePrep.js')>();
  return { ...actual, prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined) };
});

vi.mock('../prompt.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../prompt.js')>();
  return {
    ...actual,
    buildContextPrefix: () => '',
    configureAgentPermissions: () => [],
    writePlaybookHelper: vi.fn(),
    writeMessagesHelper: vi.fn(),
    DEFAULT_TOOL_GUIDANCE: 'Tool guidance: ...',
  };
});

// Default challenge record the mocked runStrategicChallenge resolves with. Tests
// that care about the challenge override it per-case via mockResolvedValueOnce.
// Hoisted so it is initialized before the (hoisted) vi.mock factory references it.
const { DEFAULT_CHALLENGE_RECORD } = vi.hoisted(() => ({
  DEFAULT_CHALLENGE_RECORD: {
    challenge_version: 1,
    status: 'ok' as const,
    evidence_progress: [] as Array<{ direction_id: string; status: string; verdict: string; rationale: string }>,
    retirement_approvals: [] as Array<{ direction_id: string; verdict: string; rationale: string }>,
    retirement_critique_quality: { verdict: 'solid', rationale: '' },
    portfolio_shape_risk: { verdict: 'low', rationale: '' },
  },
}));

vi.mock('./strategic-helpers.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./strategic-helpers.js')>();
  return {
    ...Object.fromEntries(STRATEGIC_HELPERS_EXPORT_NAMES.map((name) => [name, vi.fn()])),
    DESIGN_DOC_FETCH_VERBS: [],
    FETCH_VERB_HELP: actual.FETCH_VERB_HELP,
    PLAN_FETCH_VERBS: [],
    RESEARCH_FETCH_VERBS: [],
    STRATEGIC_CHALLENGE_VERSION: 1,
    STRATEGIC_FETCH_VERBS: [],
    writeStrategicHelpers: vi.fn(),
    // Use the real implementation so prompt blocks that call this get actual verb lines.
    renderStrategicFetchVerbHelpLines: actual.renderStrategicFetchVerbHelpLines,
    // Mocked so the wiring tests do NOT spawn the real challenge runClaudeCode
    // pass — that keeps runClaudeCode.mock.calls[0] the executor prompt for the
    // existing executor-prompt assertions.
    runStrategicChallenge: vi.fn().mockResolvedValue(DEFAULT_CHALLENGE_RECORD),
    // Degraded record the handler substitutes when the challenge persist fails:
    // a blocked record approves nothing, so the executor section renders
    // status=blocked with no approved pairs (matches the backend's fail-closed gate).
    blockedChallengeRecord: (note: string) => ({
      challenge_version: 1,
      status: 'blocked' as const,
      evidence_progress: [],
      retirement_approvals: [],
      retirement_critique_quality: { verdict: 'not_assessed', rationale: note },
      portfolio_shape_risk: { verdict: 'not_assessed', rationale: note },
      portfolio_operations: [],
      notes: note,
    }),
    // Echo the record's status + approved pairs so wiring tests can assert the
    // section is built from the record runStrategicChallenge returned and is
    // threaded into the executor prompt.
    buildStrategicChallengeExecutorSection: (record: {
      status: string;
      evidence_progress: Array<{ direction_id: string; status: string; verdict: string }>;
      retirement_approvals?: Array<{ direction_id: string; verdict: string }>;
    }) => {
      const approved = record.evidence_progress
        .filter(e => e.verdict === 'approved' && (e.status === 'advancing' || e.status === 'met'))
        .map(e => `${e.direction_id}:${e.status}`)
        .join(',');
      const retirementApproved = (record.retirement_approvals || [])
        .filter(r => r.verdict === 'approved')
        .map(r => r.direction_id)
        .join(',');
      return `CHALLENGE_SECTION(status=${record.status};approved=${approved};retirements=${retirementApproved})`;
    },
  };
});

// Use the real parseBackendTimestamp: it is a pure, timezone-safe ISO parser, and
// the prompt-cache tests below assert the exact relative-day rendering it feeds
// (formatRelativeDays / userStateLine). A null stub would make those assertions
// vacuous. Delegating to importOriginal also keeps the mock covering every actual
// export (see expectMockCoversActualNamedExports).
vi.mock('../utils/time.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/time.js')>();
  return { ...actual };
});

vi.mock('../utils/design-doc-presence.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/design-doc-presence.js')>();
  return {
    ...actual,
    ...Object.fromEntries(DESIGN_DOC_PRESENCE_EXPORT_NAMES.map((name) => [name, vi.fn()])),
    reconcileDesignDocPresence: (doc: unknown) => doc,
    // Identity stub: the handler now reconciles the compact index. Return it
    // unchanged so the overview-rendering assertions see the payload's index.
    reconcileDesignDocIndex: (index: unknown) => index,
  };
});

vi.mock('./research-direction-context.js', () => ({
  ...Object.fromEntries(RESEARCH_DIRECTION_CONTEXT_EXPORT_NAMES.map((name) => [name, vi.fn()])),
  differentiationHintFor: () => '',
  formatCochangeHints: () => '',
  formatDecompositionOutcomes: () => '',
  formatPreviousDecisionOutcomes: () => '',
  formatPastRetirementCritiques: () => '',
  formatRetiredDirectionsSummary: () => '',
  formatRetiredDirectionGrounding: () => '',
}));

vi.mock('../prompts/thinker.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../prompts/thinker.js')>();
  return {
    ...actual,
    computeResearchPlanPromptFingerprint: () => 'fp',
    buildThinkerPrompt: (p: Record<string, unknown>) => {
      capturedThinkerParams = p;
      return 'BASE_THINKER_PROMPT';
    },
    // Capture the rendered prompt, then short-circuit so the executor pass never runs.
    runThinkerWithGate: vi.fn((prompt: string) => {
      capturedThinkerPrompt = prompt;
      return Promise.resolve({ ok: false });
    }),
  };
});

vi.mock('./research-plan.js', () => ({
  ...Object.fromEntries(RESEARCH_PLAN_EXPORT_NAMES.map((name) => [name, vi.fn()])),
  PR_INELIGIBLE_TASK_TYPES: [],
  buildStrategicCompletionLogs: () => ({ logs: '' }),
  readProgressArtifact: () => '',
  buildRejectedProposalsSection: () => '',
  buildOperatorOverridesSection: (o: Array<Record<string, unknown>>) =>
    Array.isArray(o) && o.length ? `OPERATOR_OVERRIDES(${o.length})` : '',
  buildRunningUpdateSupersededSection: (signal: Record<string, unknown> | null | undefined) =>
    signal && Number(signal.count) > 0 ? '\n_operator preserved running_update_\n' : '',
  buildUserPausedSection: () => '',
  buildVersionPhaseSection: () => '',
  buildMemoCitationsSection: () => '',
  QUALIFICATION_STATE_VOCAB: new Set<string>(),
  resolveMemoHandles: () => [],
  buildEfficiencySnapshotSection: () => '',
  // Echo the authoritative active-total arg so wiring tests can assert the
  // handler threads payload.direction_counts.active through.
  buildPortfolioComposition: (
    _a: unknown,
    _b: unknown,
    _c: unknown,
    authoritativeActiveTotal?: number,
  ) => `PORTFOLIO(auth=${authoritativeActiveTotal})`,
  // Echo the rendered capability count so wiring tests can assert the handler
  // threads payload.available_capabilities through to the vocabulary section.
  buildAvailableCapabilitiesSection: (caps: Array<Record<string, unknown>> | undefined) =>
    Array.isArray(caps) && caps.length ? `AVAILABLE_CAPS(${caps.length})` : '',
  CAPABILITY_DECLARATION_DOC: 'CAP_DECL_DOC',
}));

vi.mock('./strategic-sections.js', () => ({
  ...Object.fromEntries(STRATEGIC_SECTIONS_EXPORT_NAMES.map((name) => [name, vi.fn()])),
  fmtScalar: (v: unknown, fallback = '?') => {
    if (v === undefined || v === null || v === '') return fallback;
    return typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean' ? String(v) : fallback;
  },
  buildSystemMaturitySection: () => '',
  buildCapabilityVerificationSection: () => '',
  // Echo presence so wiring tests can assert the handler passes
  // payload.cycle_start_reconciliation through to this builder.
  buildCycleStartReconciliationSection: (recon: Record<string, unknown> | undefined) =>
    recon ? `RECON(${Object.keys(recon).length})` : '',
  buildPruneCandidatesSection: (prune: Record<string, unknown> | undefined) =>
    prune ? `PRUNE(${Object.keys(prune).length})` : '',
  buildModelHealthSection: () => '',
  buildDriftSection: () => '',
  buildOutcomeSection: () => '',
  buildClockAndRecentWorkSection: () => ({ clockHeader: '', recentWorkSection: '' }),
  buildConversationThreadSection: () => '',
  buildFeedbackSection: () => '',
  buildQuestionSection: () => '',
  buildOpenMessagesText: () => '',
  formatRecentHandledConversation: () => '',
  // Echo the authoritative count args so wiring tests can assert the handler
  // threads payload.direction_counts through to these builders.
  buildDirSummaryText: (_d: unknown, authoritativeActiveCount?: number) =>
    `DIRSUMMARY(auth=${authoritativeActiveCount})`,
  buildDirHealthSummary: (
    _d: unknown,
    maxActiveDirections: number,
    authoritative?: { active: number; paused: number },
  ) => `DIRHEALTH(max=${maxActiveDirections},auth=${authoritative ? authoritative.active : undefined})`,
  // Echo key args so wiring tests can assert the handler threads
  // payload.direction_counts through to the consolidated section builder.
  buildDirectionPortfolioSection: (
    _d: unknown,
    maxActiveDirections: number,
    _sys: unknown,
    _scope: unknown,
    authoritativeActiveCount?: number,
    authoritativePausedCount?: number,
  ) => `DIRPORTFOLIO(max=${maxActiveDirections},authActive=${authoritativeActiveCount},authPaused=${authoritativePausedCount})`,
  buildSweepComparisonSection: () => '',
  buildCiComplianceSection: () => '',
  buildPayloadStalenessSection: (staleness: Record<string, unknown> | undefined) =>
    staleness && staleness.stale === true ? 'STALENESS(stale)' : '',
  buildHeldDirectionsSection: (held: Array<Record<string, unknown>> | undefined) =>
    held && held.length ? `HELD(${held.length})` : '',
}));

vi.mock('../telemetry.js', () => ({
  ...Object.fromEntries(TELEMETRY_EXPORT_NAMES.map((name) => [name, vi.fn()])),
  restoreOperatorDecisionCaptureTelemetry: vi.fn(),
  buildPromptSizeTelemetry: vi.fn().mockReturnValue({
    prompt_size_bytes: 5000,
    prompt_section_count: 3,
    prompt_section_sizes: [
      { name: 'problem_model', bytes: 2000 },
      { name: 'running_update', bytes: 1500 },
      { name: 'design_doc', bytes: 1000 },
    ],
  }),
  setPromptSizeTelemetry: vi.fn(),
  recordFormatMiss: vi.fn(),
  recordFormatRepairAttempt: vi.fn(),
  recordFormatRepairSuccess: vi.fn(),
  recordFormatRepairTokens: vi.fn(),
  recordFormatRepairInfraFailure: vi.fn(),
  recordFormatRepairMiss: vi.fn(),
  recordLlmCallStart: vi.fn(),
  recordLlmTokens: vi.fn(),
  recordProviderReportedUsage: vi.fn(),
  recordNumTurns: vi.fn(),
  recordViewportScreenshotDownload: vi.fn(),
  resetFormatComplianceTelemetry: vi.fn(),
  resetLlmTelemetry: vi.fn(),
  setCurrentPromptFingerprint: vi.fn(),
  getCurrentPromptFingerprint: vi.fn().mockReturnValue(null),
  getFormatComplianceTelemetry: vi.fn().mockReturnValue({}),
  getLlmTelemetry: vi.fn().mockReturnValue({
    llm_call_count: 0,
    num_turns: 0,
    total_input_tokens: 100,
    total_output_tokens: 50,
    viewport_screenshot_forwarded_count: 0,
  }),
}));

import {
  renderStrategicPromptSuffix,
  resolveStrategicPurpose,
  buildStagedDirectionsSection,
  renderDesignDocIndexOverview,
  buildLocalRunnerSection,
  handleStrategic,
  formatRelativeDays,
} from './strategic.js';
import * as mockedIndexModule from '../index.js';
import { runClaudeCode, apiPost, setCurrentPromptFingerprint, sendHeartbeatSync } from '../index.js';
import * as mockedWorkspacePrepModule from '../workspacePrep.js';
import { expectCalledWith } from '../utils/typed-mock-call.js';
import { VmConfigMissingError } from '../providers/runners.js';
import {
  computeStrategicPromptFingerprint,
  runThinkerWithGate,
  type ThinkerPromptParams,
} from '../prompts/thinker.js';
import * as mockedStrategicHelpersModule from './strategic-helpers.js';
import { runStrategicChallenge } from './strategic-helpers.js';
import * as mockedTimeModule from '../utils/time.js';
import * as mockedDesignDocPresenceModule from '../utils/design-doc-presence.js';
import * as mockedResearchDirectionContextModule from './research-direction-context.js';
import * as mockedResearchPlanModule from './research-plan.js';
import * as mockedStrategicSectionsModule from './strategic-sections.js';
import * as mockedTelemetryModule from '../telemetry.js';
import { recordFormatMiss, recordFormatRepairAttempt, buildPromptSizeTelemetry, setPromptSizeTelemetry } from '../telemetry.js';

async function expectMockCoversActualNamedExports(moduleId: string, mockedModule: Record<string, unknown>) {
  const actual = await vi.importActual<Record<string, unknown>>(moduleId);
  const mockedKeys = new Set(Object.keys(mockedModule));
  for (const exportName of Object.keys(actual).filter((name) => name !== 'default')) {
    expect(
      mockedKeys.has(exportName),
      `Mock for ${moduleId} is missing export ${exportName} -- add it to the factory.`,
    ).toBe(true);
  }
}

describe('mock factory completeness', () => {
  it('covers every real named export for local full-replacement mocks', async () => {
    await expectMockCoversActualNamedExports('../index.js', mockedIndexModule);
    await expectMockCoversActualNamedExports('../workspacePrep.js', mockedWorkspacePrepModule);
    await expectMockCoversActualNamedExports('./strategic-helpers.js', mockedStrategicHelpersModule);
    await expectMockCoversActualNamedExports('../utils/time.js', mockedTimeModule);
    await expectMockCoversActualNamedExports('../utils/design-doc-presence.js', mockedDesignDocPresenceModule);
    await expectMockCoversActualNamedExports('./research-direction-context.js', mockedResearchDirectionContextModule);
    await expectMockCoversActualNamedExports('./research-plan.js', mockedResearchPlanModule);
    await expectMockCoversActualNamedExports('./strategic-sections.js', mockedStrategicSectionsModule);
    await expectMockCoversActualNamedExports('../telemetry.js', mockedTelemetryModule);
  });
});

describe('buildStagedDirectionsSection', () => {
  it('returns empty string when nothing is staged', () => {
    expect(buildStagedDirectionsSection([])).toBe('');
    expect(buildStagedDirectionsSection(undefined as unknown as Array<Record<string, unknown>>)).toBe('');
  });

  it('renders staged entries with their target version and the no-defer framing', () => {
    const out = buildStagedDirectionsSection([
      { id: 'd-1', text: 'next-version feature', priority: 'high', staged_for_version: 4 },
    ]);
    expect(out).toContain('## Staged for next version');
    expect(out).toContain('d-1 [high] — next-version feature (auto-activates when v4 promotes)');
    expect(out).toContain('not a way to defer');
  });
});

describe('renderDesignDocIndexOverview', () => {
  it('returns empty string for an absent or section-less index', () => {
    expect(renderDesignDocIndexOverview(undefined)).toBe('');
    expect(renderDesignDocIndexOverview({})).toBe('');
    expect(renderDesignDocIndexOverview({ sections: [] })).toBe('');
  });

  it('renders systems/capabilities as a status catalog, not raw JSON', () => {
    const out = renderDesignDocIndexOverview({
      sections: [
        {
          key: 'systems/auth',
          status: 'mature',
          summary: 'Session + token auth',
          modules: ['backend/app/auth.py'],
        },
        {
          key: 'capabilities/login',
          status: 'working',
          summary: 'Users can log in',
        },
        { key: 'notes/strategic-context', summary: 'v5 stabilize' },
      ],
    });
    expect(out).toContain('### Systems');
    expect(out).toContain('`systems/auth`');
    expect(out).toContain('[mature]');
    expect(out).toContain('Session + token auth');
    expect(out).toContain('modules: backend/app/auth.py');
    expect(out).toContain('### Capabilities');
    expect(out).toContain('`capabilities/login`');
    expect(out).toContain('[working]');
    expect(out).toContain('### Other sections');
    expect(out).toContain('`notes/strategic-context`');
    // It is an overview, not a JSON dump.
    expect(out).not.toContain('"key":');
    expect(out).not.toContain('{');
    // Teaches on-demand fetch by key.
    expect(out).toContain('./design-doc get <key>');
    expect(out).toContain('./design-doc list');
  });

  it('surfaces a presence_warning on a downgraded entry', () => {
    const out = renderDesignDocIndexOverview({
      sections: [
        {
          key: 'systems/ghost',
          status: 'missing',
          modules: ['backend/app/ghost.py'],
          presence_warning: 'all referenced module file(s) are absent',
        },
      ],
    });
    expect(out).toContain('`systems/ghost`');
    expect(out).toContain('all referenced module file(s) are absent');
  });

  it('renders repo_overview inline bootstrap fields (tech_stack, directory_layout, first_steps)', () => {
    const out = renderDesignDocIndexOverview({
      sections: [
        {
          key: 'repo_overview',
          tech_stack: 'TypeScript, Node.js, Vitest',
          directory_layout: 'agent/ — worker; backend/ — API',
          first_steps_summary: 'cd agent && npm install && npm test',
        },
      ],
    });
    expect(out).toContain('`repo_overview`');
    expect(out).toContain('tech_stack: TypeScript, Node.js, Vitest');
    expect(out).toContain('directory_layout: agent/ — worker; backend/ — API');
    expect(out).toContain('first_steps: cd agent && npm install && npm test');
    // Should appear under Other sections (not systems/capabilities).
    expect(out).toContain('### Other sections');
    expect(out).not.toContain('### Systems');
  });
});

describe('renderStrategicPromptSuffix', () => {
  it('renders prompt_suffix as an "Additional instructions" block', () => {
    const suffix = 'DEPLOY FRAMING: integration-branch landings are not yet live.\n'
      + 'STABILIZE: open a targeted audit for the last qualification findings.';
    const out = renderStrategicPromptSuffix({ prompt_suffix: suffix });
    expect(out).toBe(`\n\nAdditional instructions:\n${suffix}`);
  });

  it('returns empty string when prompt_suffix is absent', () => {
    expect(renderStrategicPromptSuffix({})).toBe('');
    expect(renderStrategicPromptSuffix({ prompt_suffix: '' })).toBe('');
  });
});

// ── Prompt-cache prefix stability (v24 cost lever, direction d-27685510) ─────
// formatRelativeDays previously read the VM wall clock (Date.now()), so an
// identical strategic payload rendered a different userStateLine each day — a
// single wall-clock byte that busts the automatically-cacheable prompt prefix
// (Anthropic prefix-cache reads bill at ~10% of base input). It now renders
// relative to a caller-supplied deterministic reference epoch (the backend-baked
// payload timestamp), so two builds of an identical payload are byte-identical.
describe('resolveStrategicPurpose', () => {
  it('prefers the backend value and retains bounded legacy fallbacks', () => {
    const problemModel = { intent: { purpose: '...', user_purpose: ' durable operator purpose ' } };

    expect(resolveStrategicPurpose({ effective_purpose: ' canonical purpose ' }, problemModel)).toBe('canonical purpose');
    expect(resolveStrategicPurpose({}, problemModel)).toBe('durable operator purpose');
    expect(resolveStrategicPurpose({ vision: { purpose: 'legacy purpose' } }, {})).toBe('legacy purpose');
    expect(resolveStrategicPurpose({}, { intent: { purpose: '<from analysis>' } })).toBeUndefined();
  });
});

describe('formatRelativeDays', () => {
  const iso = '2026-01-01T00:00:00Z';
  const isoMs = Date.parse(iso);

  it('renders days relative to the injected reference epoch, not the wall clock', () => {
    expect(formatRelativeDays(iso, isoMs)).toBe('today');
    expect(formatRelativeDays(iso, isoMs + 86_400_000)).toBe('1 day ago');
    expect(formatRelativeDays(iso, isoMs + 5 * 86_400_000)).toBe('5 days ago');
  });

  it('is deterministic across wall-clock changes for the same (iso, refMs)', () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date('2027-01-01T00:00:00Z'));
      const a = formatRelativeDays(iso, isoMs + 3 * 86_400_000);
      vi.setSystemTime(new Date('2031-06-15T00:00:00Z'));
      const b = formatRelativeDays(iso, isoMs + 3 * 86_400_000);
      expect(a).toBe(b);
      expect(a).toBe('3 days ago');
    } finally {
      vi.useRealTimers();
    }
  });

  it('never returns a negative day count when the reference precedes the timestamp', () => {
    expect(formatRelativeDays(iso, isoMs - 10 * 86_400_000)).toBe('today');
  });

  it('falls back to a clock-free absolute UTC date when no finite reference is supplied', () => {
    expect(formatRelativeDays(iso, NaN)).toBe('on 2026-01-01');
  });

  it('returns empty string for empty or unparseable input', () => {
    expect(formatRelativeDays('', 123)).toBe('');
    expect(formatRelativeDays('not-a-date', 123)).toBe('');
  });
});

describe('handleStrategic userStateLine is byte-identical across wall-clock for an identical payload', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('renders userStateLine deterministically from the payload timestamp, not Date.now()', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      user_last_active_at: '2026-07-08T00:00:00Z',
      running_update_acknowledged_at: '2026-07-11T00:00:00Z',
      // Backend-baked payload-generation timestamp — the deterministic reference.
      payload_staleness: { verdict: 'unknown', generated_at: '2026-07-14T00:00:00Z' },
    };

    vi.useFakeTimers();
    try {
      // Build 1 — one wall clock.
      vi.setSystemTime(new Date('2026-07-14T09:00:00Z'));
      await handleStrategic('task-usl-1', payload, {}, 'tok', 'owner', 'repo', null, { value: '' });
      const line1 = String(capturedThinkerParams?.userStateLine);

      // Build 2 — same payload, wall clock advanced 40 days. The mocked
      // buildThinkerPrompt overwrites capturedThinkerParams on every call.
      vi.setSystemTime(new Date('2026-08-23T09:00:00Z'));
      await handleStrategic('task-usl-2', payload, {}, 'tok', 'owner', 'repo', null, { value: '' });
      const line2 = String(capturedThinkerParams?.userStateLine);

      expect(line1).toBe(line2);
      // Rendered relative to the payload epoch (2026-07-14): active 6 days prior,
      // acknowledged 3 days prior.
      expect(line1).toContain('User last active: 6 days ago');
      expect(line1).toContain('last acknowledged running_update: 3 days ago');
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('strategic prompt fingerprinting', () => {
  const base: ThinkerPromptParams = {
    contextPrefix: '',
    projectName: 'Test Project',
    clockHeader: 'Clock: 2026-05-04',
    isSelfRepo: false,
    userStateLine: 'User last active: today',
    packerMetaSection: '',
    strategicContext: '',
    problemModelSection: '## Your current understanding (problem_model)\n```json\n{\n  "fingerprint": "strategic-prompt"\n}\n```',
    problemModelRecovery: 'present',
    reflectionSection: '## What you previously said was hard\n_(no prior)_',
    selfImprovementBlock: '',
    runningUpdateSection: '## Running update (living document — rewrite, don\'t append)\n_(empty)_',
    designDocJson: '',
    visionJson: '',
    outcomeSection: '',
    previousDecisionOutcomes: '',
    pastRetirementCritiquesSection: '',
    retiredDirectionsSummarySection: '',
    directionPortfolioSection: '## Direction Portfolio\nActive: 0/10 slots.\nNo critical gaps detected.\n\nNo directions exist yet.\n\nNo active directions — portfolio is empty.',
    userPausedSection: '',
    rejectedProposalsSection: '',
    operatorOverridesSection: '',
    modelHealthSection: '',
    systemMaturitySection: '',
    driftSection: '',
    recentWorkSection: '',
    feedbackSection: '',
    questionSection: '',
    threadSection: '',
    taskId: 'test-task-123',
    versionPhaseSection: '',
    memoCitationsSection: '',
    efficiencySnapshotSection: '',
  };

  it('strategic_prompt_fingerprint_changes_with_strategic_prompt_content', () => {
    const withoutSuffix = computeStrategicPromptFingerprint(base, '');
    const withSuffix = computeStrategicPromptFingerprint({
      ...base,
      stagedDirectionsSection: '## Staged for next version\n- d-x [high] — work (auto-activates when v3 promotes)',
    }, 'deployment framing');
    const contentChanged = computeStrategicPromptFingerprint(
      { ...base, projectName: 'Another Project', clockHeader: 'Later clock', userStateLine: 'User last active: yesterday', taskId: 'other-task' },
      '',
    );

    expect(withoutSuffix).toHaveLength(12);
    expect(withSuffix).toHaveLength(12);
    expect(withoutSuffix).not.toBe(withSuffix);
    expect(withoutSuffix).toBe(contentChanged);
  });
});

describe('handleStrategic problem_model recovery framing', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('dropped-but-nonempty: packer dropped problem_model → steer to fetch, not bootstrap', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      // problem_model absent from payload (dropped under byte budget)
      _packer_meta: {
        dropped: [{ key: 'problem_model', reason: 'exceeds remaining budget even after summarize' }],
        trimmed: [],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-d', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params).toBeDefined();
    expect(params?.problemModelRecovery).toBe('fetch');
    const section = String(params?.problemModelSection);
    // Must NOT present this as a first cycle...
    expect(section).not.toContain('this is the first cycle');
    // ...and must steer to fetch-the-full-model-then-edit.
    expect(section).toContain('./fetch problem-model');
    expect(section).toContain('EXISTS but was not shipped in full');
  });

  it('trimmed/summarized: packer summarized problem_model → steer to fetch even with partial JSON', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      problem_model: { intent: { purpose: 'partial' } },
      _packer_meta: {
        dropped: [],
        trimmed: [{ key: 'problem_model', kind: 'summarized' }],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-t', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params?.problemModelRecovery).toBe('fetch');
    const section = String(params?.problemModelSection);
    expect(section).toContain('./fetch problem-model');
    expect(section).not.toContain('this is the first cycle');
  });

  it('dropped version_phase advertises ./fetch version-phase recovery', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      _packer_meta: {
        dropped: [{ key: 'version_phase', reason: 'exceeds remaining budget after floor' }],
        trimmed: [],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-vp-fetch', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const section = String(capturedThinkerParams?.packerMetaSection);
    expect(section).toContain('version_phase');
    expect(section).toContain('./fetch version-phase');
  });

  it('dropped retired/prior-decision sections advertise the section endpoint, not ./fetch direction', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      _packer_meta: {
        // fetch_verb: 'sections' is what the backend schema now carries for these
        // sections (recoverable via GET /projects/{project_id}/sections/{name}).
        dropped: [
          { key: 'retired_directions_summary', reason: 'budget exhausted', bytes: 4096, fetch_verb: 'sections' },
          { key: 'past_retirement_critiques', reason: 'budget exhausted', bytes: 2048, fetch_verb: 'sections' },
        ],
        trimmed: [
          { key: 'previous_decision_outcomes', kind: 'summarized', bytes_original: 8192, bytes_trimmed: 1024, fetch_verb: 'sections' },
        ],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-retired-fetch', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const section = String(capturedThinkerParams?.packerMetaSection);
    // The ./fetch sections <name> CLI verb is advertised for each retired/prior section.
    expect(section).toContain('./fetch sections retired_directions_summary');
    expect(section).toContain('./fetch sections past_retirement_critiques');
    expect(section).toContain('./fetch sections previous_decision_outcomes');
    // The misleading broad per-direction fetch is NOT advertised for these keys.
    expect(section).not.toContain('./fetch direction');
  });

  it('per-key fetch hints appear for dropped and trimmed entries with byte sizes', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      _packer_meta: {
        dropped: [{ key: 'open_messages', reason: 'budget exhausted', bytes: 20480, fetch_verb: 'message' }],
        trimmed: [{ key: 'directions_summary', kind: 'list_trimmed', shown: 5, total: 20, bytes_original: 10240, bytes_trimmed: 2048 }],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-per-key-hints', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const section = String(capturedThinkerParams?.packerMetaSection);
    // Per-key hint for dropped open_messages → message verb (via fetch_verb field)
    expect(section).toContain('./fetch message');
    // Per-key hint for trimmed directions_summary → direction verb (via PAYLOAD_KEY_FETCH_VERB)
    expect(section).toContain('./fetch direction');
    // Byte size info appears for both entries
    expect(section).toContain('20480');   // bytes for open_messages
    expect(section).toContain('10240');   // bytes_original for directions_summary
    expect(section).toContain('2048');    // bytes_trimmed for directions_summary
    // Unrelated verbs are not shown (no generic trailing list)
    expect(section).not.toContain('./fetch gap');
    expect(section).not.toContain('./fetch playbook');
  });

  it('dropped section without backend fetch_verb uses PAYLOAD_KEY_FETCH_VERB fallback', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      _packer_meta: {
        dropped: [{ key: 'retired_directions_summary', reason: 'budget exhausted', bytes: 8192 }],
        trimmed: [],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-drop-fallback', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const section = String(capturedThinkerParams?.packerMetaSection);
    // Fallback hint from PAYLOAD_KEY_FETCH_VERB, shown on the → line (no inline fetch_verb on the drop record)
    expect(section).toContain('./fetch sections retired_directions_summary');
    // Byte size still rendered
    expect(section).toContain('8192');
  });

  it('newly-mapped sections-type keys render correct recovery hints when dropped', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      _packer_meta: {
        dropped: [
          { key: 'self_reflection_log', reason: 'budget exhausted', bytes: 1024 },
          { key: 'anomaly_summary', reason: 'budget exhausted', bytes: 2048 },
          { key: 'outcome_evaluation', reason: 'budget exhausted', bytes: 4096 },
          { key: 'rejected_proposals', reason: 'budget exhausted', bytes: 512 },
          { key: 'past_retirement_critiques', reason: 'budget exhausted', bytes: 3000 },
        ],
        trimmed: [],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-sections-hints', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const section = String(capturedThinkerParams?.packerMetaSection);
    expect(section).toContain('./fetch sections self_reflection_log');
    expect(section).toContain('./fetch sections anomaly_summary');
    expect(section).toContain('./fetch sections outcome_evaluation');
    expect(section).toContain('./fetch sections rejected_proposals');
    expect(section).toContain('./fetch sections past_retirement_critiques');
    // Stale direction-verb entries are gone from previously incorrect mappings
    expect(section).not.toContain('./fetch direction');
  });

  it('true-empty: no packer drop/trim and empty model → bootstrap framing (first cycle)', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      // no problem_model, no packer meta naming it
    };
    const statusRef = { value: '' };
    await handleStrategic('task-e', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params?.problemModelRecovery).toBe('empty');
    const section = String(params?.problemModelSection);
    expect(section).toContain('this is the first cycle');
    expect(section).not.toContain('./fetch problem-model');
  });

  it('present: full model shipped → present framing, no fetch steering', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      problem_model: { intent: { purpose: 'real' }, capabilities: [], gaps: [] },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-p', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params?.problemModelRecovery).toBe('present');
    const section = String(params?.problemModelSection);
    expect(section).toContain('```json');
    expect(section).not.toContain('this is the first cycle');
    expect(section).not.toContain('EXISTS but was not shipped in full');
  });
});

describe('handleStrategic self-repo prompt framing', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('external_project_enable_self_improvement_does_not_render_self_repo_wording', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'External Project',
      is_self_repo: false,
      enable_self_improvement: true,
    };
    const statusRef = { value: '' };
    await handleStrategic('task-ext-improve', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(capturedThinkerParams?.isSelfRepo).toBe(false);
    const block = String(capturedThinkerParams?.selfImprovementBlock);
    expect(block).toContain('# Project improvement');
    expect(block).toContain('eligible for agent-proposed improvement work');
    expect(block).not.toContain('# Self-improvement');
    expect(block).not.toContain('your own prompts, schemas, tools, and');
    expect(block).not.toContain('point of self-repo');
  });

  it('true self-repo preserves self-improvement wording', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      is_self_repo: true,
      enable_self_improvement: true,
    };
    const statusRef = { value: '' };
    await handleStrategic('task-self-improve', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(capturedThinkerParams?.isSelfRepo).toBe(true);
    const block = String(capturedThinkerParams?.selfImprovementBlock);
    expect(block).toContain('# Self-improvement');
    expect(block).toContain('your own prompts, schemas, tools, and');
    expect(block).toContain('point of self-repo');
    expect(block).not.toContain('# Project improvement');
  });

  it('self-repo identity does not enable improvement mode by itself', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      is_self_repo: true,
      enable_self_improvement: false,
    };
    const statusRef = { value: '' };
    await handleStrategic('task-self-no-improve', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(capturedThinkerParams?.isSelfRepo).toBe(true);
    expect(capturedThinkerParams?.selfImprovementBlock).toBe('');
  });
});

describe('handleStrategic executor prompt: atomic staged create-direction', () => {
  it('documents create-direction with "staged": true as a single atomic op (no create-then-stage)', async () => {
    vi.mocked(runClaudeCode).mockClear();
    // Let the thinker gate pass so the executor prompt is actually built and
    // handed to runClaudeCode (its first arg is the executor prompt).
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    });
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: '',
    });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-staged-exec',
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(executorPrompt).toBeDefined();
    expect(executorPrompt).toContain('"staged": true');
    expect(executorPrompt).toContain('one atomic op');
    expect(executorPrompt).toContain('./messages send --stdin <<\'EOF\'');
    // Steers AWAY from the old create-then-stage sequence for new ideas.
    expect(executorPrompt).toContain('Do NOT create-then-stage');
  });

  it('treats problem_model reconstruction as the last resort after bounded repair', async () => {
    vi.mocked(runClaudeCode).mockClear();
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    });
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: '',
    });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-last-resort',
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(executorPrompt).toContain('if the file is still missing, empty, or the JSON is malformed after the bounded repair reopen');
  });


  it('dropped inbox: executor prompt contains fetch-message guidance with correct command when open_message_count > 0 and open_messages dropped', async () => {
    vi.mocked(runClaudeCode).mockClear();
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    });
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: '',
    });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-dropped-inbox',
      {
        project_name: 'Blueprint',
        // open_messages absent (dropped under byte budget) but count is known
        open_message_count: 3,
        _packer_meta: {
          dropped: [{ key: 'open_messages', reason: 'exceeds remaining budget even after summarize' }],
          trimmed: [],
        },
      },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(executorPrompt).toBeDefined();
    // Must render the fallback section (not the full open_messages section)
    expect(executorPrompt).toContain('inbox not shipped');
    expect(executorPrompt).toContain('3 messages available');
    // Must use the correct singular fetch verb with no subcommand
    expect(executorPrompt).toContain('./fetch message`');
    // Must NOT use the invalid 'messages list' form
    expect(executorPrompt).not.toContain('./fetch messages list');
  });

  it('does not record format miss or repair attempt for a command-mode strategic run', async () => {
    vi.mocked(runClaudeCode).mockClear();
    vi.mocked(recordFormatMiss).mockClear();
    vi.mocked(recordFormatRepairAttempt).mockClear();

    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    });

    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: './strategic set-problem-model\n./strategic set-running-update\n./strategic set-retirement-critiques\nProblem model set at revision 42.',
      lastResultText: 'Commands ran successfully.',
    });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-cmd-mode',
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    expect(recordFormatMiss).not.toHaveBeenCalled();
    expect(recordFormatRepairAttempt).not.toHaveBeenCalled();
    // Executor ran (runClaudeCode called at least once for the executor pass)
    expect(vi.mocked(runClaudeCode).mock.calls.length).toBeGreaterThan(0);
  });
});

describe('handleStrategic strategic challenge gate', () => {
  beforeEach(() => {
    vi.mocked(runClaudeCode).mockClear();
    vi.mocked(apiPost).mockClear();
    vi.mocked(runStrategicChallenge).mockReset();
    vi.mocked(runStrategicChallenge).mockResolvedValue(
      DEFAULT_CHALLENGE_RECORD as unknown as Awaited<ReturnType<typeof runStrategicChallenge>>,
    );
  });

  it('handleStrategic runs and persists a challenge before executor', async () => {
    // The challenge pass runs over the thinker analysis, the record is persisted
    // via the set_strategic_challenge op BEFORE the executor runs, and the
    // record's challenge-approved evidence-progress pairs reach the executor
    // prompt with the gate instruction.
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'ANALYSIS_UNDER_CHALLENGE',
    });

    const challengeRecord = {
      challenge_version: 1,
      status: 'ok' as const,
      evidence_progress: [
        { direction_id: 'd-7', status: 'advancing', verdict: 'approved', rationale: 'real evidence' },
        { direction_id: 'd-9', status: 'met', verdict: 'rejected', rationale: 'thin' },
      ],
      retirement_critique_quality: { verdict: 'solid', rationale: 'names the pattern' },
      portfolio_shape_risk: { verdict: 'elevated', rationale: 'concentrated' },
    };
    vi.mocked(runStrategicChallenge).mockResolvedValueOnce(
      challengeRecord as unknown as Awaited<ReturnType<typeof runStrategicChallenge>>,
    );
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-challenge', { project_name: 'Blueprint' }, {}, 'tok', 'owner', 'repo', null, statusRef,
    );

    // The challenge ran over the thinker's analysis output.
    expect(vi.mocked(runStrategicChallenge)).toHaveBeenCalledTimes(1);
    const challengeArgs = vi.mocked(runStrategicChallenge).mock.calls[0][0];
    expect(challengeArgs.analysisText).toBe('ANALYSIS_UNDER_CHALLENGE');

    // The record was persisted via the new store op with the verbatim record.
    const storeCallIdx = vi.mocked(apiPost).mock.calls.findIndex(
      c => c[0] === '/api/agent/strategic'
        && (c[1] as { op?: string }).op === 'set_strategic_challenge',
    );
    expect(storeCallIdx).toBeGreaterThanOrEqual(0);
    const storeBody = vi.mocked(apiPost).mock.calls[storeCallIdx][1] as {
      task_id: string; op: string; data: unknown;
    };
    expect(storeBody.task_id).toBe('task-challenge');
    expect(storeBody.data).toBe(challengeRecord);

    // Persist happened BEFORE the executor's first runClaudeCode invocation.
    const storeOrder = vi.mocked(apiPost).mock.invocationCallOrder[storeCallIdx];
    const executorOrder = vi.mocked(runClaudeCode).mock.invocationCallOrder[0];
    expect(storeOrder).toBeLessThan(executorOrder);

    // The executor prompt carries the challenge section (only the APPROVED
    // advancing/met pair, d-7) plus the evidence-progress gate instruction.
    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(executorPrompt).toContain('CHALLENGE_SECTION(status=ok;approved=d-7:advancing;retirements=)');
    expect(executorPrompt).toContain('Challenge gate');
    expect(executorPrompt).toContain('the independent challenge APPROVED');
  });

  it('persists a blocked/invalid challenge record (no approvals) and still reaches the executor', async () => {
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    });

    const blockedRecord = {
      challenge_version: 1,
      status: 'blocked' as const,
      evidence_progress: [],
      retirement_critique_quality: { verdict: 'not_assessed', rationale: 'challenge exited 1' },
      portfolio_shape_risk: { verdict: 'not_assessed', rationale: 'challenge exited 1' },
    };
    vi.mocked(runStrategicChallenge).mockResolvedValueOnce(
      blockedRecord as unknown as Awaited<ReturnType<typeof runStrategicChallenge>>,
    );
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-blocked', { project_name: 'Blueprint' }, {}, 'tok', 'owner', 'repo', null, statusRef,
    );

    const storeCall = vi.mocked(apiPost).mock.calls.find(
      c => (c[1] as { op?: string }).op === 'set_strategic_challenge',
    );
    expect(storeCall).toBeDefined();
    const storeData = (storeCall?.[1] as { data: typeof blockedRecord } | undefined)?.data;
    expect(storeData?.status).toBe('blocked');

    // A blocked challenge approves nothing — the executor prompt must say so and
    // list no approved pairs.
    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0];
    expect(executorPrompt).toContain('CHALLENGE_SECTION(status=blocked;approved=;retirements=)');
  });

  it('executor prompt carries retirement gate section wired from challenge record', async () => {
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    });

    const challengeWithRetirement = {
      ...DEFAULT_CHALLENGE_RECORD,
      retirement_approvals: [
        { direction_id: 'd-30', verdict: 'approved', rationale: 'real pattern' },
        { direction_id: 'd-31', verdict: 'rejected', rationale: 'shallow' },
      ],
    };
    vi.mocked(runStrategicChallenge).mockResolvedValueOnce(
      challengeWithRetirement as unknown as Awaited<ReturnType<typeof runStrategicChallenge>>,
    );
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });

    await handleStrategic(
      'task-ret-gate', { project_name: 'Blueprint' }, {}, 'tok', 'owner', 'repo', null, { value: '' },
    );

    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0];
    // Mock echoes only approved retirements (d-30, not d-31).
    expect(executorPrompt).toContain('CHALLENGE_SECTION(status=ok;approved=;retirements=d-30)');
    // Static executor instruction mentions deferred response handling.
    expect(executorPrompt).toContain('"status": "deferred"');
    expect(executorPrompt).toContain('SOFT NON-ERROR OUTCOME');
  });

  it('degrades to no-approvals (does NOT fail the task) when challenge persistence fails', async () => {
    // A persist failure must NOT discard the completed analysis and kill the cycle:
    // the backend gate fails closed on a missing record, so the cycle continues and
    // the executor is shown a BLOCKED (no-approvals) view that matches that reality.
    // Regression for task 9aec276e — a transient project-row 429 / abort on this one
    // write burned three full ~25-min strategic analyses and still ended terminal.
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    });

    // The challenge itself produced an OK record with an approved pair — proving the
    // degradation comes from the persist failure, not from a blocked challenge.
    vi.mocked(runStrategicChallenge).mockResolvedValueOnce({
      ...DEFAULT_CHALLENGE_RECORD,
      evidence_progress: [
        { direction_id: 'd-7', status: 'advancing', verdict: 'approved', rationale: 'real' },
      ],
    } as unknown as Awaited<ReturnType<typeof runStrategicChallenge>>);
    // Only the set_strategic_challenge persist fails; later strategic POSTs succeed.
    vi.mocked(apiPost).mockImplementation((_path: string, body?: Record<string, unknown>) => {
      if (body && (body as { op?: string }).op === 'set_strategic_challenge') {
        throw new Error('challenge store failed');
      }
      return Promise.resolve({});
    });

    const statusRef = { value: '' };
    // No throw: the cycle survives the persist failure.
    await handleStrategic(
      'task-challenge-fail', { project_name: 'Blueprint' }, {}, 'tok', 'owner', 'repo', null, statusRef,
    );
    // Restore the default impl so the rejecting one does not leak to later suites
    // (this block's beforeEach only mockClear()s — it does not reset the impl).
    vi.mocked(apiPost).mockReset();
    vi.mocked(apiPost).mockResolvedValue({});

    // The executor still ran — the analysis was NOT discarded.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalled();
    // …and it was shown the BLOCKED view (no approvals), matching the backend's
    // fail-closed gate, NOT the OK record's approved d-7 pair.
    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0];
    expect(executorPrompt).toContain('CHALLENGE_SECTION(status=blocked;approved=;retirements=)');
  });
});

describe('handleStrategic operator_overrides wiring', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('threads payload.operator_overrides into the thinker params', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      operator_overrides: [
        { document: 'running_update', field: 'running_update', agent_value: 'a', at: '2026-06-01' },
        { document: 'vision', field: 'vision.goal', agent_value: 'b', operator_value: 'c', at: '2026-06-02' },
      ],
    };
    const statusRef = { value: '' };
    await handleStrategic('task-ov', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params?.operatorOverridesSection).toBe('OPERATOR_OVERRIDES(2)');
  });

  it('passes empty section when operator_overrides absent', async () => {
    const payload: Record<string, unknown> = { project_name: 'Blueprint' };
    const statusRef = { value: '' };
    await handleStrategic('task-no-ov', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params?.operatorOverridesSection).toBe('');
  });
});

describe('handleStrategic cycle_start_reconciliation wiring', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('threads payload.cycle_start_reconciliation into the thinker params', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      cycle_start_reconciliation: {
        capability_contradictions: [],
        orphaned_gaps: [{ gap_id: 'g-1', name: 'x' }],
        ungrounded_directions: [],
        direction_pipeline: [],
        counts: { active_directions: 3 },
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-recon', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params?.cycleStartReconciliationSection).toBe('RECON(5)');
  });

  it('passes an empty section when cycle_start_reconciliation is absent', async () => {
    const payload: Record<string, unknown> = { project_name: 'Blueprint' };
    const statusRef = { value: '' };
    await handleStrategic('task-recon-empty', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(capturedThinkerParams?.cycleStartReconciliationSection).toBe('');
  });
});

describe('handleStrategic authoritative direction_counts wiring', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('anchors direction views on direction_counts when directions_summary is empty', async () => {
    // directions_summary dropped under budget; direction_counts (critical) carries
    // the true active count + real ceiling.
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      directions_summary: [],
      direction_counts: { active: 12, paused: 1, total: 13, max_active: 15 },
      max_active_directions: 15,
    };
    const statusRef = { value: '' };
    await handleStrategic('task-dc', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const p = capturedThinkerParams;
    // The handler threads the authoritative active count (12) and pauses (1)
    // into the consolidated portfolio section builder.
    expect(String(p?.directionPortfolioSection)).toBe('DIRPORTFOLIO(max=15,authActive=12,authPaused=1)');
  });

  it('falls back to undefined authoritative count + default ceiling when direction_counts absent', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      directions_summary: [],
    };
    const statusRef = { value: '' };
    await handleStrategic('task-dc-absent', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const p = capturedThinkerParams;
    expect(String(p?.directionPortfolioSection)).toBe('DIRPORTFOLIO(max=10,authActive=undefined,authPaused=undefined)');
  });
});

describe('handleStrategic running_update_superseded wiring', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('wires a running_update_superseded section when the payload signal is present', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      running_update_superseded: { count: 2, most_recent_at: '2026-05-20T09:30:00Z' },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-rus', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(String(capturedThinkerParams?.runningUpdateSupersededSection)).toContain(
      'operator preserved running_update'
    );
  });

  it('passes an empty section when no superseded signal is present', async () => {
    const payload: Record<string, unknown> = { project_name: 'Blueprint' };
    const statusRef = { value: '' };
    await handleStrategic('task-rus-empty', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(capturedThinkerParams?.runningUpdateSupersededSection).toBe('');
  });
});

describe('handleStrategic per-gap memory pre-trim wiring', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('extracts gap_ids from problem_model._gap_memory_trimmed and passes them to the thinker', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      problem_model: {
        intent: { purpose: 'real' },
        gaps: [{ id: 'g-long-lived' }, { id: 'g-fresh' }],
        // The backend pre-trim marker: per-gap append-only memory shortened
        // to a recent window before the byte-budget packer ran.
        _gap_memory_trimmed: [
          { gap_id: 'g-long-lived', fields: { history: { shown: 3, total: 41 } } },
        ],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-gm', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params).toBeDefined();
    // The model is still 'present' — the pre-trim is independent of packer drops.
    expect(params?.problemModelRecovery).toBe('present');
    expect(params?.gapMemoryTrimmedGapIds).toEqual(['g-long-lived']);
  });

  it('passes an empty list when no gap memory was trimmed', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      problem_model: { intent: { purpose: 'real' }, gaps: [{ id: 'g-1' }] },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-gm-empty', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(capturedThinkerParams?.gapMemoryTrimmedGapIds).toEqual([]);
  });

  it('drops entries with missing gap_id from the trimmed list', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      problem_model: {
        intent: { purpose: 'real' },
        _gap_memory_trimmed: [
          { gap_id: 'g-a', fields: {} },
          { fields: {} }, // malformed: no gap_id
          { gap_id: 'g-b', fields: {} },
        ],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-gm-mal', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(capturedThinkerParams?.gapMemoryTrimmedGapIds).toEqual(['g-a', 'g-b']);
  });
});

describe('handleStrategic design_doc index rendering', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('renders the design_doc index as an architecture overview (not raw JSON) into designDocJson', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      design_doc: {
        sections: [
          {
            key: 'systems/auth',
            status: 'mature',
            summary: 'Session + token auth',
            modules: ['backend/app/auth.py'],
          },
          { key: 'capabilities/login', status: 'working', summary: 'Users can log in' },
        ],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-dd', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const rendered = String(capturedThinkerParams?.designDocJson);
    // Architecture catalog form, not a JSON dump of the index object.
    expect(rendered).toContain('### Systems');
    expect(rendered).toContain('`systems/auth`');
    expect(rendered).toContain('[mature]');
    expect(rendered).toContain('### Capabilities');
    expect(rendered).not.toContain('"sections"');
    expect(rendered).not.toContain('"key":');
    // Teaches on-demand fetch by key.
    expect(rendered).toContain('./design-doc get <key>');
    expect(rendered).toContain('./design-doc list');
  });

  it('does not crash and renders empty designDocJson on the new shape when sections are absent', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      design_doc: { sections: [] },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-dd-empty', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(capturedThinkerParams).toBeDefined();
    expect(capturedThinkerParams?.designDocJson).toBe('');
  });

  it('derives strategicContext from the notes/strategic-context index entry with a fetch hint', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      design_doc: {
        sections: [
          { key: 'notes/strategic-context', summary: 'v5 stabilize — harden toward qualification' },
        ],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-sc', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const ctx = String(capturedThinkerParams?.strategicContext);
    expect(ctx).toContain('v5 stabilize — harden toward qualification');
    // Cross-cycle running context is not silently lost — a fetch hint remains.
    expect(ctx).toContain('./design-doc get notes/strategic-context');
  });

  it('emits empty strategicContext when no notes/strategic-context entry exists', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      design_doc: { sections: [{ key: 'systems/auth', status: 'mature' }] },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-sc-none', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(capturedThinkerParams?.strategicContext).toBe('');
  });
});

describe('handleStrategic prompt_suffix wiring', () => {
  beforeEach(() => {
    capturedThinkerPrompt = undefined;
    vi.mocked(setCurrentPromptFingerprint).mockClear();
    vi.mocked(sendHeartbeatSync).mockClear();
  });

  it('appends backend prompt_suffix content to the rendered thinker prompt', async () => {
    const suffix = 'STABILIZE GUIDANCE: last qualification flagged audit finding F-123.';
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      prompt_suffix: suffix,
    };
    const statusRef = { value: '' };

    await handleStrategic(
      'task-1', payload, {}, 'tok', 'owner', 'repo', null, statusRef,
    );

    expect(capturedThinkerPrompt).toBeDefined();
    // The analysis-pass content is preserved...
    expect(capturedThinkerPrompt).toContain('BASE_THINKER_PROMPT');
    // ...and the backend-supplied guidance reaches the rendered prompt.
    expect(capturedThinkerPrompt).toContain('Additional instructions:');
    expect(capturedThinkerPrompt).toContain(suffix);
  });

  it('records the strategic fingerprint in prompt telemetry and heartbeat metadata', async () => {
    const suffix = 'STABILIZE GUIDANCE: audit finding F-123.';
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      prompt_suffix: suffix,
    };
    const statusRef = { value: '' };

    await handleStrategic(
      'task-fingerprint', payload, {}, 'tok', 'owner', 'repo', null, statusRef,
    );

    const expected = computeStrategicPromptFingerprint(
      capturedThinkerParams as unknown as ThinkerPromptParams,
      `\n\nAdditional instructions:\n${suffix}`,
    );
    expectCalledWith(setCurrentPromptFingerprint, expected);
    expectCalledWith(sendHeartbeatSync,
      'task-fingerprint',
      'Cloning owner/repo for research...',
      { prompt_fingerprint: expected },
    );
  });

  it('leaves the thinker prompt unchanged when prompt_suffix is absent', async () => {
    const statusRef = { value: '' };
    await handleStrategic(
      'task-2', { project_name: 'Blueprint' }, {}, 'tok', 'owner', 'repo', null, statusRef,
    );
    expect(capturedThinkerPrompt).toBe('BASE_THINKER_PROMPT');
  });

  it('threads available_capabilities into the thinker availableCapabilitiesSection param', async () => {
    const statusRef = { value: '' };
    await handleStrategic(
      'task-3',
      {
        project_name: 'Blueprint',
        available_capabilities: [
          { name: 'seeded-eval-harness', status: 'working' },
          { name: 'payment-core', status: 'planned' },
        ],
      },
      {}, 'tok', 'owner', 'repo', null, statusRef,
    );
    const section = capturedThinkerParams?.availableCapabilitiesSection as string;
    // The vocabulary section renders (mock echoes the count) and carries the
    // uniform provides_capability / prerequisite declaration doc.
    expect(section).toContain('AVAILABLE_CAPS(2)');
    expect(section).toContain('CAP_DECL_DOC');
  });

  it('passes an empty availableCapabilitiesSection when no capabilities are supplied', async () => {
    const statusRef = { value: '' };
    await handleStrategic(
      'task-4', { project_name: 'Blueprint' }, {}, 'tok', 'owner', 'repo', null, statusRef,
    );
    expect(capturedThinkerParams?.availableCapabilitiesSection).toBe('');
  });
});

describe('handleStrategic prompt section telemetry', () => {
  beforeEach(() => {
    capturedThinkerPrompt = undefined;
    vi.mocked(buildPromptSizeTelemetry).mockClear();
    vi.mocked(setPromptSizeTelemetry).mockClear();
  });

  it('test_strategic_handler_emits_prompt_section_telemetry', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      prompt_suffix: 'DEPLOY FRAMING: integration-branch.',
      problem_model: { intent: { purpose: 'real' }, capabilities: [], gaps: [] },
    };
    const statusRef = { value: '' };

    await handleStrategic(
      'task-telemetry', payload, {}, 'tok', 'owner', 'repo', null, statusRef,
    );

    // buildPromptSizeTelemetry must have been called with the assembled prompt
    // and a non-empty list of named section tuples.
    expect(buildPromptSizeTelemetry).toHaveBeenCalledTimes(1);
    const callArgs = vi.mocked(buildPromptSizeTelemetry).mock.calls[0];
    const fullPrompt = callArgs[0];
    const sections = callArgs[1];

    // The full prompt must contain the base thinker prompt content plus the suffix.
    expect(fullPrompt).toContain('BASE_THINKER_PROMPT');
    expect(fullPrompt).toContain('DEPLOY FRAMING: integration-branch.');

    // Section tuples must include the major content-bearing params.
    const sectionNames = new Set(sections.map(([name]) => name));
    expect(sectionNames.has('problem_model')).toBe(true);
    expect(sectionNames.has('running_update')).toBe(true);
    expect(sectionNames.has('context_prefix')).toBe(true);
    expect(sectionNames.has('design_doc')).toBe(true);
    expect(sectionNames.has('direction_portfolio')).toBe(true);

    // setPromptSizeTelemetry must have been called with the telemetry result.
    expect(setPromptSizeTelemetry).toHaveBeenCalledTimes(1);
    const telemetryArgs = vi.mocked(setPromptSizeTelemetry).mock.calls[0];
    expect(telemetryArgs[0]).toBe(5000);   // prompt_size_bytes from mock
    expect(telemetryArgs[1]).toBe(3);       // prompt_section_count from mock
    expect(telemetryArgs[2]).toEqual([
      { name: 'problem_model', bytes: 2000 },
      { name: 'running_update', bytes: 1500 },
      { name: 'design_doc', bytes: 1000 },
    ]);
  });
});

describe('handleStrategic executor prompt: version-signal sibling passthrough', () => {
  it('instructs executor to carry new_version_goal/version_phase_decision/qualify_request as top-level siblings', async () => {
    vi.mocked(runClaudeCode).mockClear();
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    });
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: '',
    });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-sibling-pass',
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0];
    expect(executorPrompt).toBeDefined();

    // All three sibling fields must be named in the executor instructions.
    expect(executorPrompt).toContain('new_version_goal');
    expect(executorPrompt).toContain('version_phase_decision');
    expect(executorPrompt).toContain('qualify_request');

    // The instruction must tell the executor to add them as top-level sibling keys
    // (not nested under problem_model content).
    expect(executorPrompt).toContain('top-level sibling');

    // The instruction must reference the problem_model file edit mechanism
    // (editing /tmp/problem_model_<id>.json IN PLACE) so the siblings reach
    // the backend via the set-problem-model cat call.
    expect(executorPrompt).toContain('IN PLACE');

    // Each field's absence must mean skip (not fabricate).
    expect(executorPrompt).toContain('do not fabricate');
  });
});

describe('handleStrategic pre-merge: new_version_goal written into problem_model JSON', () => {
  it('pre-merges new_version_goal into problem_model JSON when the thinker emits it', async () => {
    const taskId = 'task-premerge-goal';
    const pmPath = `/tmp/problem_model_${taskId}.json`;
    const initialPm = { intent: { purpose: 'ok' }, gaps: [] };

    vi.mocked(runClaudeCode).mockClear();
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
      newVersionGoal: 'Ship reliable error recovery so ops incidents drop by 50%.',
    });

    // fs.readFileSync returns the initial problem_model JSON for the pre-merge read.
    vi.mocked(fs.readFileSync as (path: string, encoding: string) => string).mockImplementation(
      (path: string) => {
        if (path === pmPath) return JSON.stringify(initialPm);
        return '';
      },
    );

    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: '',
    });

    const statusRef = { value: '' };
    await handleStrategic(taskId, { project_name: 'Blueprint' }, {}, 'tok', 'owner', 'repo', null, statusRef);

    // writeFileSync must have been called with the pm path and a JSON that contains the goal field.
    const writeCalls = vi.mocked(fs.writeFileSync).mock.calls;
    const pmWrite = writeCalls.find((c) => c[0] === pmPath);
    expect(pmWrite).toBeDefined();
    const writtenStr = pmWrite?.[1];
    expect(typeof writtenStr).toBe('string');
    const written = JSON.parse(writtenStr as string) as Record<string, unknown>;
    expect(written.new_version_goal).toBe('Ship reliable error recovery so ops incidents drop by 50%.');
  });
});

describe('handleStrategic executor prompt: proposed acceptance criteria handoff', () => {
  beforeEach(() => {
    vi.mocked(runClaudeCode).mockClear();
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(fs.readFileSync as (path: string, encoding: string) => string).mockReturnValue('');
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    });
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: '',
    });
  });

  afterEach(() => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(fs.readFileSync as (path: string, encoding: string) => string).mockReturnValue('');
  });

  it('retries_when_proposed_acceptance_criteria_handoff_missing', async () => {
    const taskId = 'task-criteria-retry';
    const criteriaPath = `/tmp/proposed_criteria_${taskId}.json`;
    const criteriaPayload = '{"criteria":[{"text":"Users can promote only when canonical criteria pass."}]}';
    vi.mocked(fs.existsSync).mockImplementation(
      (path: Parameters<typeof fs.existsSync>[0]) => String(path) === criteriaPath,
    );
    vi.mocked(fs.readFileSync as (path: string, encoding: string) => string).mockImplementation(
      (path: string) => (path === criteriaPath ? criteriaPayload : ''),
    );
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: './strategic set-problem-model\n./strategic set-running-update\n./strategic set-retirement-critiques\nProblem model set at revision 42.',
        lastResultText: 'Commands ran successfully.',
      })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' });

    await handleStrategic(
      taskId,
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      { value: '' },
    );

    expect(vi.mocked(runClaudeCode).mock.calls).toHaveLength(2);
    const retryPrompt = vi.mocked(runClaudeCode).mock.calls[1]?.[0];
    expect(retryPrompt).toContain('./strategic set-acceptance-criteria');
  });

  it('injects the proposed criteria block and conditional command when the thinker wrote a non-empty file', async () => {
    const taskId = 'task-criteria';
    const criteriaPath = `/tmp/proposed_criteria_${taskId}.json`;
    const criteriaPayload = '{"criteria":[{"text":"Users can promote only when canonical criteria pass.","direction_keys":["d-abc"]}]}';
    vi.mocked(fs.existsSync).mockImplementation(
      (path: Parameters<typeof fs.existsSync>[0]) => String(path) === criteriaPath,
    );
    vi.mocked(fs.readFileSync as (path: string, encoding: string) => string).mockImplementation(
      (path: string) => (path === criteriaPath ? criteriaPayload : ''),
    );

    await handleStrategic(
      taskId,
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      { value: '' },
    );

    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(executorPrompt).toContain('# Proposed acceptance criteria');
    expect(executorPrompt).toContain(criteriaPayload);
    expect(executorPrompt).toContain(`./strategic set-acceptance-criteria "$(cat ${criteriaPath})"`);
    expect(executorPrompt).toContain('If the file is absent or empty, SKIP');
    expect(executorPrompt).toContain('do not fabricate a criteria list');
    expect(executorPrompt.indexOf('# Proposed acceptance criteria')).toBeLessThan(
      executorPrompt.indexOf('# Instructions'),
    );
  });

  it('omits the proposed criteria block when the file is absent while keeping the command conditional', async () => {
    const taskId = 'task-no-criteria';

    await handleStrategic(
      taskId,
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      { value: '' },
    );

    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(executorPrompt).not.toContain('# Proposed acceptance criteria');
    expect(executorPrompt).toContain(`./strategic set-acceptance-criteria "$(cat /tmp/proposed_criteria_${taskId}.json)"`);
    expect(executorPrompt).toContain('If the file is absent or empty, SKIP');
  });

  it('documents the retract shape and permanently-uncoverable trigger in the executor prompt', async () => {
    const taskId = 'task-retract-doc';

    await handleStrategic(
      taskId,
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      { value: '' },
    );

    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    // The executor prompt must document the retract array shape so the strategist knows it can retract criteria.
    expect(executorPrompt).toContain('retract');
    expect(executorPrompt).toContain('criterion_key');
    expect(executorPrompt).toContain('reason');
    // Must mention the trigger condition: permanently uncoverable
    expect(executorPrompt).toContain('permanently uncoverable');
    // Must guard against fabrication
    expect(executorPrompt).toContain('Never fabricate retractions');
  });
});

describe('handleStrategic infra-error propagation (vm_config_missing)', () => {
  it('propagates a vm_config_missing death from the required-calls retry instead of swallowing it', async () => {
    // The swallowing retry-catch previously turned a vm_config_missing death into a
    // [WARNING] + a misclassified COMPLETED/claude_code_failed status — the warm VM
    // then stranded into vm_unresponsive. The fix rethrows VmConfigMissingError so it
    // reaches runRoleOnce's classifier for a clean PROVISION_FAILURE requeue.
    vi.mocked(runClaudeCode).mockReset();
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    });
    // Executor (call 0) returns exit 0 with no required-call evidence → triggers the
    // required-calls retry (call 1), which dies vm_config_missing.
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })
      .mockRejectedValueOnce(
        new VmConfigMissingError(
          'provider config file missing on VM (boot/retarget materialization lost)',
        ),
      );

    const statusRef = { value: '' };
    await expect(
      handleStrategic('task-vmcm', { project_name: 'Blueprint' }, {}, 'tok', 'owner', 'repo', null, statusRef),
    ).rejects.toBeInstanceOf(VmConfigMissingError);

    // Restore the shared default so later tests are unaffected.
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });
});

describe('handleStrategic executor prompt: arms-based compute_experiment commission', () => {
  beforeEach(() => {
    vi.mocked(runClaudeCode).mockClear();
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    });
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: '',
    });
  });

  it('executor prompt contains the arms-based spawn-task example', async () => {
    const statusRef = { value: '' };
    await handleStrategic(
      'task-arms-commission',
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0];
    expect(executorPrompt).toBeDefined();
    expect(executorPrompt).toContain('"arms"');
    expect(executorPrompt).toContain('"declared_metric"');
  });

  it('executor prompt arms example includes name and command fields', async () => {
    const statusRef = { value: '' };
    await handleStrategic(
      'task-arms-fields',
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0];
    expect(executorPrompt).toBeDefined();
    // Arms example must include specific arm names unique to the arms example (not the single-command example).
    expect(executorPrompt).toContain('"name": "a"');
    expect(executorPrompt).toContain('"name": "b"');
    // declared_metric documents what the command emits
    expect(executorPrompt).toContain('declared_metric');
  });

  it('instructs output-preserving experiments to use the durable artifact contract', async () => {
    const statusRef = { value: '' };
    await handleStrategic(
      'task-durable-output-contract',
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0];
    expect(executorPrompt).toContain('require_durable_artifact: true');
    expect(executorPrompt).toContain('$BLUEPRINT_ARTIFACT_DIR');
    expect(executorPrompt).toContain('manifest.json');
  });
});

describe('handleStrategic create-direction recovery prompt', () => {
  it('recovery prompt includes gap_id, success_evidence, retirement_condition and omits system_id', async () => {
    vi.mocked(runClaudeCode).mockReset();
    // Thinker returns analysisText with a ## Structured Output section referencing create-direction
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText:
        'Some analysis text.\n\n## Structured Output\n\ncreate-direction: build new feature\n',
    });
    // Executor (call 0): exits 0 with no direction ops in output → triggers recovery
    // Executor output must include the required calls (set-problem-model, set-running-update,
    // set-retirement-critiques) so the required-calls retry does NOT fire as call 1.
    // The create-direction recovery is call 1 only when required calls are satisfied.
    const requiredCallsOutput =
      './strategic set-problem-model "..." \n' +
      './strategic set-running-update /tmp/running_update_task-recovery-schema.md\n' +
      './strategic set-retirement-critiques \'{"no_eligible_retirements": true}\'';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: requiredCallsOutput, lastResultText: '' })
      // Recovery (call 1): exits 0
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-recovery-schema',
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    // The recovery prompt is the second runClaudeCode call
    const recoveryPrompt = vi.mocked(runClaudeCode).mock.calls[1]?.[0];
    expect(recoveryPrompt).toBeDefined();
    expect(recoveryPrompt).toContain('success_evidence');
    expect(recoveryPrompt).toContain('retirement_condition');
    expect(recoveryPrompt).toContain('gap_id');
    expect(recoveryPrompt).not.toContain('system_id');

    // Restore shared default for subsequent tests
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });
});

describe('handleStrategic direction-op post-check: all portfolio verbs', () => {
  // Required calls output shared across tests — satisfies the required-calls check
  // so only the direction-op post-check fires as call 1.
  const requiredCallsOutput =
    './strategic set-problem-model "..." \n' +
    './strategic set-running-update /tmp/running_update_task-portfolio-verbs.md\n' +
    './strategic set-retirement-critiques \'{"no_eligible_retirements": true}\'';

  it('retries_when_thinker_recommended_stage_or_retire_is_skipped: retire-direction recommended but skipped triggers retry', async () => {
    vi.mocked(runClaudeCode).mockReset();
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText:
        'Some analysis text.\n\n## Structured Output\n\nretire-direction: d-abc123 — stale direction\n',
    });
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: requiredCallsOutput, lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-portfolio-verbs',
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    expect(vi.mocked(runClaudeCode).mock.calls).toHaveLength(2);
    const recoveryPrompt = vi.mocked(runClaudeCode).mock.calls[1]?.[0];
    expect(recoveryPrompt).toBeDefined();
    expect(recoveryPrompt).toContain('retire-direction');

    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('update-direction recommended but skipped triggers retry with update-direction in prompt', async () => {
    vi.mocked(runClaudeCode).mockReset();
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText:
        'Analysis.\n\n## Structured Output\n\nupdate-direction: d-xyz — priority bump\n',
    });
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: requiredCallsOutput, lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-portfolio-verbs',
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    expect(vi.mocked(runClaudeCode).mock.calls).toHaveLength(2);
    const recoveryPrompt = vi.mocked(runClaudeCode).mock.calls[1]?.[0];
    expect(recoveryPrompt).toBeDefined();
    expect(recoveryPrompt).toContain('update-direction');

    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('stage-direction recommended but skipped triggers retry; stage_direction in output suppresses retry', async () => {
    // First: stage-direction recommended but executor output has no op → triggers retry
    vi.mocked(runClaudeCode).mockReset();
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText:
        'Analysis.\n\n## Structured Output\n\nstage-direction: d-staged — next version\n',
    });
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: requiredCallsOutput, lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-portfolio-verbs',
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    expect(vi.mocked(runClaudeCode).mock.calls).toHaveLength(2);
    const recoveryPrompt = vi.mocked(runClaudeCode).mock.calls[1]?.[0];
    expect(recoveryPrompt).toContain('stage-direction');

    // Second: stage_direction op present in output → no spurious retry
    vi.mocked(runClaudeCode).mockReset();
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText:
        'Analysis.\n\n## Structured Output\n\nstage-direction: d-staged — next version\n',
    });
    const outputWithStageOp =
      requiredCallsOutput +
      '\n{"op":"stage_direction","status":"ok","direction_id":"d-staged"}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: outputWithStageOp, lastResultText: '' });

    await handleStrategic(
      'task-portfolio-verbs',
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    // Only one call: the executor. No recovery call.
    expect(vi.mocked(runClaudeCode).mock.calls).toHaveLength(1);

    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('approve-direction and reject-direction recommended but skipped triggers retry', async () => {
    vi.mocked(runClaudeCode).mockReset();
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText:
        'Analysis.\n\n## Structured Output\n\napprove-direction: d-qa1\nreject-direction: d-qa2\n',
    });
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: requiredCallsOutput, lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-portfolio-verbs',
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    expect(vi.mocked(runClaudeCode).mock.calls).toHaveLength(2);
    const recoveryPrompt = vi.mocked(runClaudeCode).mock.calls[1]?.[0];
    expect(recoveryPrompt).toContain('approve-direction');
    expect(recoveryPrompt).toContain('reject-direction');

    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });
});

describe('buildLocalRunnerSection runner inventory', () => {
  it('renders project runner UUIDs with safe soft-pin guidance', () => {
    const section = buildLocalRunnerSection({
      ever_registered: true,
      active_total: 1,
      active_gpu: 1,
      runners: [{
        runner_id: '11111111-2222-3333-4444-555555555555',
        hostname: 'iconlab',
        status: 'active',
        live: true,
        last_heartbeat_at: '2026-07-24T12:00:00',
        gpu_count: 7,
        container_ok: true,
        gpu_in_container_ok: true,
        active_compute_jobs: 1,
        max_concurrent_compute: 2,
        workspace_free_bytes: 1024 ** 3,
        container_image_store_free_bytes: 2 * 1024 ** 3,
      }],
    });

    expect(section).toContain('11111111-2222-3333-4444-555555555555');
    expect(section).toContain('iconlab');
    expect(section).toContain('LIVE');
    expect(section).toContain('compute 1/2');
    expect(section).toContain('workspace/disk 1.0 GiB');
    expect(section).toContain('image store 2.0 GiB');
    expect(section).toContain('"runner_target": {"runner_id": "<UUID>", "strict": false}');
    expect(section).toContain('Use `strict: true` ONLY');
  });

  it('does not render runner pin guidance without a project inventory', () => {
    const section = buildLocalRunnerSection({
      ever_registered: true,
      active_total: 0,
      active_gpu: 0,
    });
    expect(section).not.toContain('"runner_target"');
  });
});
