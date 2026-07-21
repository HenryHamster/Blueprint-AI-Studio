import { vi, describe, it, expect, beforeEach } from 'vitest';
import fs from 'fs';

// ── Module mocks ──────────────────────────────────────────────────────────────
// Mock ./index.js entirely so that importing prompt.ts does NOT trigger main().
vi.mock('./index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/workspace',
    sendLog: vi.fn(),
    getAgentToken: vi.fn().mockReturnValue(''),
    git: vi.fn().mockReturnValue(''),
  };
});

vi.mock('fs', () => ({
  default: {
    existsSync: vi.fn().mockReturnValue(false),
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    appendFileSync: vi.fn(),
    mkdirSync: vi.fn(),
    readdirSync: vi.fn().mockReturnValue([]),
    unlinkSync: vi.fn(),
  },
}));

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(Buffer.from('')),
  execFile: vi.fn(),
  spawn: vi.fn().mockReturnValue({
    stderr: { on: vi.fn() },
    stdout: { on: vi.fn() },
    on: vi.fn(),
    kill: vi.fn(),
  }),
}));

// ── Imports (after mocks) ─────────────────────────────────────────────────────
import { execFileSync } from 'child_process';
import {
  formatPlaybook,
  buildContextPrefix,
  buildRetryContext,
  formatProjectContext,
  configureAgentPermissions,
  discoverRelatedTests,
  extractTestConventions,
  getTaskTypeHints,
  TASK_TYPE_HINTS,
  TASK_TYPES_WITHOUT_HINT,
  VIEWPORT_SNAPSHOT_HINT,
  buildViewportScreenshotSection,
  buildAnalysisPrompt,
  parseAnalysisPlanFiles,
  parseAnalysisPlanTestFiles,
  parseAnalysisPlanVerifyFiles,
  extractModuleInterfaces,
  discoverAffectedConsumers,
  relativeTimeFromIso,
  synthesizeSuccessPatterns,
  synthesizeFailureHotspots,
  formatSelfReviewBlindSpots,
  formatProvenanceWarnings,
  formatInvestigateCitationProvenance,
  formatAcceptanceEvidence,
  formatProjectBlindSpotSummary,
  formatFixIterationHistory,
  formatSelfReviewCalibrationSection,
  writeMessagesHelper,
  writeDesignDocHelper,
  writePlaybookHelper,
  configureResidentAgentPermissions,
  CODE_CHANGE_HELPER_GUIDANCE,
  buildCodeChangeHelperGuidance,
  codeChangeGuidanceVariant,
  computePromptFingerprint,
  CREATE_TASK_FOCUS,
  FIX_TASK_FOCUS,
  REFACTOR_TASK_FOCUS,
  PLAYBOOK_GUIDANCE,
  DEFAULT_TOOL_GUIDANCE,
  READ_THE_FAILURE_MESSAGE,
  SIZE_GUARDRAIL_GUIDANCE,
  PRIOR_ATTEMPT_DIVERGENCE,
  BEYOND_SCOPE_REPORTING,
  READ_ONLY_BASH_WRITE_NOTE,
  SELF_REVIEW_PROMPT_VERSION,
  SHARED_LIFECYCLE_REMAINDER,
  assembleFailureGuidanceBlocks,
  formatSkills,
  formatProjectTools,
  writeSkillHelpers,
  type TaskSkill,
  type TaskToolContent,
  type FixAttempt,
} from './prompt.js';
import { getReadOnlyTaskTypes, getWriteTaskTypes } from './taskCapabilities.js';
import { handlers } from './handlers/index.js';
import { buildThinkerPrompt, type ThinkerPromptParams } from './prompts/thinker.js';

// ── formatPlaybook ────────────────────────────────────────────────────────────
describe('formatPlaybook', () => {
  it('returns empty string for undefined', () => {
    expect(formatPlaybook(undefined)).toBe('');
  });

  it('returns empty string for empty object', () => {
    expect(formatPlaybook({})).toBe('');
  });

  it('formats a single entry with section header and content', () => {
    const result = formatPlaybook({ 'key1': 'value1' });
    expect(result).toContain('### key1');
    expect(result).toContain('value1');
  });

  it('formats multiple entries, each with its own header', () => {
    const result = formatPlaybook({ 'alpha': 'first value', 'beta': 'second value' });
    expect(result).toContain('### alpha');
    expect(result).toContain('first value');
    expect(result).toContain('### beta');
    expect(result).toContain('second value');
  });

  it('wraps output in an Operational Playbook header', () => {
    const result = formatPlaybook({ 'tip': 'do this' });
    expect(result).toContain('## Operational Playbook');
  });

  it('formats entry objects with content field', () => {
    const result = formatPlaybook({
      'key1': { content: 'value1', effectiveness_score: 0.8, confirmed_count: 5, age_days: 10, stale: false },
    });
    expect(result).toContain('### key1');
    expect(result).toContain('value1');
  });

  it('adds caveat for stale entries', () => {
    const result = formatPlaybook({
      'old-tip': { content: 'some advice', effectiveness_score: 0.5, confirmed_count: 2, age_days: 45, stale: true },
    });
    expect(result).toContain('Possibly outdated or low confidence');
    expect(result).toContain('some advice');
  });

  it('adds caveat for low effectiveness entries (below 0.3)', () => {
    const result = formatPlaybook({
      'weak-tip': { content: 'uncertain advice', effectiveness_score: 0.2, confirmed_count: 0, age_days: 5, stale: false },
    });
    expect(result).toContain('Possibly outdated or low confidence');
    expect(result).toContain('uncertain advice');
  });

  it('does not add caveat for high-confidence entries', () => {
    const result = formatPlaybook({
      'good-tip': { content: 'solid advice', effectiveness_score: 0.9, confirmed_count: 10, age_days: 5, stale: false },
    });
    expect(result).not.toContain('Possibly outdated');
    expect(result).toContain('solid advice');
  });

  it('renders relevance reasons for object entries', () => {
    const result = formatPlaybook({
      'targeted-tip': {
        content: 'use this when touching router code',
        effectiveness_score: 0.7,
        confirmed_count: 3,
        age_days: 2,
        stale: false,
        relevance: { reasons: ['same direction', '2 overlapping file(s)'] },
      },
    });
    expect(result).toContain('Relevance: same direction; 2 overlapping file(s)');
    expect(result).toContain('use this when touching router code');
  });

  it('renders branch/HEAD provenance for object entries', () => {
    const result = formatPlaybook({
      'prov-tip': {
        content: 'advice with provenance',
        effectiveness_score: 0.8,
        confirmed_count: 5,
        age_days: 1,
        stale: false,
        provenance: { branch: 'automain', head_sha: 'abc1234', diverged: false },
      },
    });
    expect(result).toContain('Recorded on automain @ abc1234');
    expect(result).not.toContain('Possibly outdated');
  });

  it('flags divergent-provenance entries as possibly outdated', () => {
    const result = formatPlaybook({
      'diverged-tip': {
        content: 'written on a feature branch',
        effectiveness_score: 0.9,
        confirmed_count: 8,
        age_days: 1,
        stale: false,
        provenance: { branch: 'blueprint/feature-x', head_sha: 'def5678', diverged: true },
      },
    });
    expect(result).toContain('Recorded on blueprint/feature-x @ def5678');
    expect(result).toContain('different branch than yours');
    expect(result).toContain('Possibly outdated or low confidence');
  });
});

// ── formatFixIterationHistory ─────────────────────────────────────────────────
describe('formatFixIterationHistory', () => {
  it('returns empty string when no attempts recorded', () => {
    expect(formatFixIterationHistory([])).toBe('');
  });

  it('renders a single attempt with diff stat and error tail', () => {
    const attempts: FixAttempt[] = [
      {
        attemptNumber: 1,
        diffStat: ' agent/src/foo.ts | 12 ++++++++----',
        errorTail: 'TypeError: foo is not a function',
      },
    ];
    const out = formatFixIterationHistory(attempts);
    expect(out).toContain('Previous Fix Attempts');
    expect(out).toContain('Fix iteration 1');
    expect(out).toContain('agent/src/foo.ts | 12');
    expect(out).toContain('TypeError: foo is not a function');
  });

  it('renders multiple attempts in order', () => {
    const attempts: FixAttempt[] = [
      { attemptNumber: 1, diffStat: 'a.ts | 5 +++--', errorTail: 'error A' },
      { attemptNumber: 2, diffStat: 'b.ts | 8 ++++----', errorTail: 'error B' },
    ];
    const out = formatFixIterationHistory(attempts);
    expect(out.indexOf('Fix iteration 1')).toBeLessThan(out.indexOf('Fix iteration 2'));
    expect(out).toContain('error A');
    expect(out).toContain('error B');
  });

  it('renders fallback text when diff stat is empty', () => {
    const attempts: FixAttempt[] = [
      { attemptNumber: 1, diffStat: '', errorTail: 'eslint failed' },
    ];
    expect(formatFixIterationHistory(attempts)).toContain('(no file changes detected)');
  });

  it('truncates accumulated history below the 2KB cap by dropping oldest entries', () => {
    // A long error tail per attempt → many attempts will exceed 2000 chars.
    const longTail = 'X'.repeat(800);
    const attempts: FixAttempt[] = Array.from({ length: 10 }, (_, i) => ({
      attemptNumber: i + 1,
      diffStat: `f${i}.ts | 10 +++++-----`,
      errorTail: longTail,
    }));
    const out = formatFixIterationHistory(attempts);
    expect(out.length).toBeLessThanOrEqual(2000);
    // Most recent attempt MUST survive (newest is most informative for the
    // "don't repeat this" intent); oldest is the one we drop. Check the
    // file-name in the diff stat to disambiguate from "Fix iteration 1" being
    // a prefix of "Fix iteration 10".
    expect(out).toContain('f9.ts');     // attempt 10's diff (i = 9)
    expect(out).not.toContain('f0.ts'); // attempt 1's diff (i = 0) was dropped
  });

  it('preserves header even when only one entry survives truncation', () => {
    const longTail = 'Y'.repeat(2500);  // single entry already exceeds budget
    const attempts: FixAttempt[] = [
      { attemptNumber: 5, diffStat: 'big.ts | 100 +++', errorTail: longTail },
    ];
    const out = formatFixIterationHistory(attempts);
    // Header keeps the agent oriented; the error tail is auto-capped at 400 chars
    // by renderEntry, so a single entry always fits in the 2000-char budget.
    expect(out).toContain('Previous Fix Attempts');
    expect(out).toContain('Fix iteration 5');
  });
});

// ── buildContextPrefix ────────────────────────────────────────────────────────
describe('buildContextPrefix', () => {
  it('returns empty string when task has no project_context', () => {
    expect(buildContextPrefix({})).toBe('');
  });

  it('returns empty string when project_context has only empty arrays', () => {
    const task = { project_context: { active_tasks: [], open_prs: [], sibling_tasks: [] } };
    expect(buildContextPrefix(task)).toBe('');
  });

  it('returns non-empty string ending with \\n\\n when active_tasks has entries', () => {
    const task = {
      project_context: {
        active_tasks: [
          {
            task_type: 'create',
            title: 'Build a feature',
            status: 'IN_PROGRESS',
            prompt: 'Do the thing',
            related_files: [],
          },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toBeTruthy();
    expect(result.endsWith('\n\n')).toBe(true);
  });

  it('threads the task type from payloads[0].type into context output', () => {
    const task = {
      payloads: [{ type: 'review' }],
      project_context: {
        active_tasks: [
          { task_type: 'fix', title: 'Ongoing fix', status: 'IN_PROGRESS', prompt: 'Fix it' },
        ],
      },
    };
    const result = buildContextPrefix(task);
    // The task type is used internally; the context should be non-empty
    expect(result).toBeTruthy();
  });

  it('returns empty string when project_context is undefined', () => {
    expect(buildContextPrefix({ project_context: undefined })).toBe('');
  });

  it('renders ## Remediation Context when task.context has diagnosis_task_id', () => {
    const task = {
      payloads: [{ type: 'create' }],
      context: {
        diagnosis_task_id: 'abc-123',
        severity: 'high',
        diagnosis_confidence: 'medium',
        remediation_attempts: 2,
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Remediation Context');
    expect(result).toContain('diagnosed fix');
    expect(result).toContain('Severity: high');
    expect(result).toContain('Diagnosis confidence: medium');
    expect(result).toContain('Prior remediation attempts on this direction: 2');
  });

  it('omits ## Remediation Context when diagnosis_task_id is absent', () => {
    const task = { payloads: [{ type: 'create' }], context: { severity: 'high' } };
    expect(buildContextPrefix(task)).not.toContain('## Remediation Context');
  });

  it('renders ## Direction Context section with goal, priority and failed approaches when direction_context is present', () => {
    const task = {
      project_context: {
        active_tasks: [
          { task_type: 'create', title: 'Some task', status: 'IN_PROGRESS', prompt: 'Do it' },
        ],
      },
      direction_context: {
        title: 'Improve auth flow',
        priority: 'high',
        failed_approaches: [
          { text: 'JWT in cookies', failure_type: 'CODE_ERROR' },
          { text: 'Session-based auth', failure_type: '' },
        ],
        recent_outcomes: [
          { tasks_created: 3, prs_merged: 2 },
          { tasks_created: 4, prs_merged: 1 },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Direction Context');
    expect(result).toContain('**Goal:** Improve auth flow');
    expect(result).toContain('**Priority:** high');
    expect(result).toContain('**Avoid (already tried and failed):**');
    expect(result).toContain('- JWT in cookies [CODE_ERROR]');
    expect(result).toContain('- Session-based auth');
    expect(result).toContain('Recent cycles:');
    expect(result).toContain('2/3 merged');
    expect(result).toContain('1/4 merged');
  });

  it('renders recency and failure_stage qualifiers for failed approaches when fields are present', () => {
    const now = new Date();
    const twoDaysAgo = new Date(now.getTime() - 2 * 24 * 60 * 60 * 1000);
    const iso = twoDaysAgo.toISOString().slice(0, 10); // YYYY-MM-DD
    const task = {
      direction_context: {
        title: 'Improve auth flow',
        priority: 'high',
        failed_approaches: [
          { text: 'JWT in cookies', failure_type: 'CODE_ERROR', failed_at: iso, failure_stage: 'test' },
          { text: 'Redis session store', failure_type: 'CODE_ERROR', failed_at: iso, failure_stage: '' },
          { text: 'CSRF double submit', failure_type: 'CODE_ERROR', failed_at: '', failure_stage: 'lint' },
        ],
        recent_outcomes: [],
      },
    };
    const result = buildContextPrefix(task);
    // Both recency and stage present → combined qualifier
    expect(result).toContain('- JWT in cookies [CODE_ERROR] (2d ago, failed at test)');
    // Recency only (no stage) → only relative time qualifier
    expect(result).toContain('- Redis session store [CODE_ERROR] (2d ago)');
    // Stage only (no date) → only stage qualifier
    expect(result).toContain('- CSRF double submit [CODE_ERROR] (failed at lint)');
  });

  it('renders legacy string failed_approaches as bullet list (backward compatible)', () => {
    const task = {
      direction_context: {
        title: 'Legacy direction',
        priority: 'medium',
        failed_approaches: ['Bare string approach'],
        recent_outcomes: [],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('- Bare string approach');
  });

  it('renders direction_context without failed approaches or recent_outcomes when arrays are empty', () => {
    const task = {
      direction_context: {
        title: 'Optimize DB queries',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Direction Context');
    expect(result).toContain('**Goal:** Optimize DB queries');
    expect(result).toContain('**Priority:** medium');
    expect(result).not.toContain('Avoid');
    expect(result).not.toContain('Recent cycles');
  });

  it('renders implementation constraints in Direction Context when present', () => {
    const task = {
      direction_context: {
        title: 'Refactor internals',
        priority: 'high',
        implementation_constraints: 'Must not modify the public API surface.',
        failed_approaches: [],
        recent_outcomes: [],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('**Implementation constraints:** Must not modify the public API surface.');
  });

  it('omits implementation constraints line when the field is absent or empty', () => {
    const present = buildContextPrefix({
      direction_context: { title: 'X', priority: 'low', implementation_constraints: '' },
    });
    expect(present).not.toContain('**Implementation constraints:**');
    const missing = buildContextPrefix({
      direction_context: { title: 'X', priority: 'low' },
    });
    expect(missing).not.toContain('**Implementation constraints:**');
  });

  it('does not render direction_context section when direction_context is absent', () => {
    const task = {
      project_context: {
        active_tasks: [
          { task_type: 'review', title: 'Review PR', status: 'IN_PROGRESS', prompt: 'Check it' },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).not.toContain('## Direction Context');
  });

  it('renders ## Project Vision section when task.vision is present', () => {
    const task = {
      payloads: [{ type: 'create' }],
      vision: {
        purpose: 'Build an autonomous coding studio.',
        success_criteria: ['Agents self-improve', 'CI stays green'],
        priorities: ['Stability over features'],
        systems: [
          { name: 'Orchestrator', status: 'functional', description: 'Routes tasks' },
        ],
        revision: 3,
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Project Vision');
    expect(result).toContain('**Purpose:** Build an autonomous coding studio.');
    expect(result).toContain('- Agents self-improve');
    expect(result).toContain('- CI stays green');
    expect(result).toContain('- Stability over features');
    expect(result).toContain('- **Orchestrator** (functional) — Routes tasks');
  });

  it('does not render ## Project Vision when task.vision is absent', () => {
    const task = { payloads: [{ type: 'create' }] };
    expect(buildContextPrefix(task)).not.toContain('## Project Vision');
  });

  it('renders ## Strategic Intent (purpose, focus, context, constraints) when task.strategic_intent is present', () => {
    const task = {
      payloads: [{ type: 'create' }],
      strategic_intent: {
        purpose: 'Ship a reliable autonomous coding studio.',
        current_focus: 'Make operator steering reach workers immediately.',
        context: { project_type: 'internal-tool', team: 'solo' },
        constraints: ['Never break the main branch', 'Keep PRs small'],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Strategic Intent');
    expect(result).toContain('**Purpose:** Ship a reliable autonomous coding studio.');
    expect(result).toContain('**Current focus:** Make operator steering reach workers immediately.');
    expect(result).toContain('- Project type: internal-tool');
    expect(result).toContain('- Team: solo');
    expect(result).toContain('- Never break the main branch');
    expect(result).toContain('- Keep PRs small');
  });

  it('does not render ## Strategic Intent when task.strategic_intent is absent', () => {
    const task = { payloads: [{ type: 'create' }] };
    expect(buildContextPrefix(task)).not.toContain('## Strategic Intent');
  });

  it('renders direction contract fields (completion/success/retirement) in Direction Context', () => {
    const task = {
      direction_context: {
        title: 'Carry live intent',
        priority: 'high',
        contract: {
          completion_criteria: 'Workers render live intent and contract.',
          success_evidence: 'Claim response exposes strategic_intent.',
          retirement_condition: 'Live intent reaches every worker type.',
        },
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Direction Context');
    expect(result).toContain('**Completion criteria:** Workers render live intent and contract.');
    expect(result).toContain('**Success evidence:** Claim response exposes strategic_intent.');
    expect(result).toContain('**Retirement condition:** Live intent reaches every worker type.');
  });

  it('does not render contract lines when direction_context.contract is absent', () => {
    const task = {
      direction_context: { title: 'No contract', priority: 'low' },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Direction Context');
    expect(result).not.toContain('**Completion criteria:**');
    expect(result).not.toContain('**Success evidence:**');
    expect(result).not.toContain('**Retirement condition:**');
  });

  it('renders narrative (established findings, open questions, last cycle) in Direction Context', () => {
    const task = {
      direction_context: {
        title: 'Stabilize queue',
        priority: 'high',
        narrative: {
          established_findings: ['Workers crash under burst', 'Redis is not the bottleneck'],
          open_questions: ['Does autoscaling help?'],
          last_cycle: { tasks_created: 3, prs_merged: 3, prs_rejected: 0 },
        },
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('**Established findings (what we know):**');
    expect(result).toContain('- Workers crash under burst');
    expect(result).toContain('- Redis is not the bottleneck');
    expect(result).toContain('**Open questions (still investigating):**');
    expect(result).toContain('- Does autoscaling help?');
    expect(result).toContain('**Last productive cycle:** 3/3 merged');
  });

  it('renders recent_findings_evidence with claims and sources in Direction Context', () => {
    const task = {
      direction_context: {
        title: 'Research caching',
        priority: 'high',
        recent_findings_evidence: [
          {
            title: 'Redis TTL tuning reduces latency',
            claims: ['p99 dropped 40ms', 'no cache stampede observed'],
            source_refs: [{ ref: 'pr#42', excerpt: 'TTL set to 300s' }, { ref: 'log/bench' }],
          },
          {
            title: 'Memcached not viable',
            claims: ['no cluster mode support'],
            source_refs: [],
          },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('**Recent findings evidence (canonical — full claims and sources):**');
    expect(result).toContain('"Redis TTL tuning reduces latency"');
    expect(result).toContain('Claims: p99 dropped 40ms; no cache stampede observed');
    expect(result).toContain('Sources: pr#42 ("TTL set to 300s"); log/bench');
    expect(result).toContain('"Memcached not viable"');
    expect(result).toContain('Claims: no cluster mode support');
  });

  it('omits recent_findings_evidence section when array is empty', () => {
    const task = {
      direction_context: {
        title: 'x',
        priority: 'medium',
        recent_findings_evidence: [],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).not.toContain('Recent findings evidence');
  });

  it('renders scope_success_rates as a concise merge-rate line in Direction Context', () => {
    const task = {
      direction_context: {
        title: 'x',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        scope_success_rates: {
          medium: { created: 1, merged: 0, failed: 1 },
          small: { created: 4, merged: 3, failed: 1 },
          empty: { created: 0, merged: 0, failed: 0 },
        },
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('Scope merge rates: small 3/4 (75%, 1 failed), medium 0/1 (0%, 1 failed)');
    expect(result).not.toContain('empty');
  });

  it('renders per-scope avg_expansion_ratio as actionable guidance in Direction Context', () => {
    const task = {
      direction_context: {
        title: 'x',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        scope_success_rates: {
          small: { created: 4, merged: 3, failed: 1, avg_expansion_ratio: 1.8 },
          medium: { created: 2, merged: 1, failed: 1 },
        },
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('Direction expansion: small-scope averages 1.8x file expansion');
    expect(result).toContain('budget headroom');
  });

  it('renders review_iteration_rolling_median as human-readable guidance in Direction Context', () => {
    const task = {
      direction_context: {
        title: 'x',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        review_iteration_rolling_median: 3.5,
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('Recent PRs on this direction averaged 3.5 review iterations');
    expect(result).toContain('tight scope merges faster');
  });

  it('appends last-cycle suffix when review_iteration_last_cycle differs from rolling median', () => {
    const task = {
      direction_context: {
        title: 'x',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        review_iteration_rolling_median: 2.0,
        review_iteration_last_cycle: 4.0,
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('averaged 2.0 review iterations (last cycle: 4.0)');
  });

  it('omits last-cycle suffix when review_iteration_last_cycle equals rolling median', () => {
    const task = {
      direction_context: {
        title: 'x',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        review_iteration_rolling_median: 2.0,
        review_iteration_last_cycle: 2.0,
      },
    };
    const result = buildContextPrefix(task);
    expect(result).not.toContain('last cycle');
  });

  it('omits review iteration line when review_iteration_rolling_median is absent', () => {
    const task = {
      direction_context: {
        title: 'x',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).not.toContain('review iterations');
  });

  it('omits failed annotation in scope merge rates when failed count is zero', () => {
    const task = {
      direction_context: {
        title: 'x',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        scope_success_rates: {
          small: { created: 4, merged: 3, failed: 0 },
          medium: { created: 2, merged: 2 },
        },
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('Scope merge rates: small 3/4 (75%), medium 2/2 (100%)');
    expect(result).not.toContain('failed');
  });

  it('renders failure_stage_pattern message in Direction Context when present', () => {
    const task = {
      direction_context: {
        title: 'Fix test flakiness',
        priority: 'high',
        failed_approaches: [],
        recent_outcomes: [],
        failure_stage_pattern: {
          stage: 'code_error',
          rate: 0.86,
          message: 'Recent tasks for this direction frequently fail at the code/test stage — pay extra attention to test coverage and run tests early.',
        },
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Direction Context');
    expect(result).toContain('**Failure pattern (86% of recent failures):**');
    expect(result).toContain('pay extra attention to test coverage');
  });

  it('does not render failure_stage_pattern section when failure_stage_pattern is absent', () => {
    const task = {
      direction_context: {
        title: 'Improve caching',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Direction Context');
    expect(result).not.toContain('**Failure pattern:**');
  });

  it('does not render failure_stage_pattern section when failure_stage_pattern is null', () => {
    const task = {
      direction_context: {
        title: 'Optimize queries',
        priority: 'low',
        failed_approaches: [],
        recent_outcomes: [],
        failure_stage_pattern: null,
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Direction Context');
    expect(result).not.toContain('**Failure pattern:**');
  });

  it('renders direction_prior_analysis as Prior analysis section when present', () => {
    const task = {
      direction_context: {
        title: 'Refactor module',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        direction_prior_analysis: [
          {
            title: 'Prior failed task',
            failure_category: 'CODE_ERROR',
            analysis_plan: 'Update orchestrator.py to call new helper',
            test_failure_summary: 'AssertionError in test_foo',
            pr_changed_files: ['foo.py', 'bar.ts'],
            pr_diff_summary: [
              { file: 'foo.py', added: 10, removed: 5, change_type: 'modified' },
              { file: 'bar.ts', added: 3, removed: 2, change_type: 'added' },
            ],
            previous_error_fingerprint: 'abc123def456',
          },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('**Prior analysis from failed attempts:**');
    expect(result).toContain('- Prior failed task [CODE_ERROR]');
    expect(result).toContain('Analysis plan: Update orchestrator.py to call new helper');
    expect(result).toContain('Test failure: AssertionError in test_foo');
    expect(result).toContain('Files changed: foo.py (+10/-5), bar.ts (+3/-2)');
    expect(result).toContain('Previous error fingerprint: abc123def456');
  });

  it('renders latest_status_message and self_review_outcome in prior analysis', () => {
    const task = {
      direction_context: {
        title: 'Refactor module',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        direction_prior_analysis: [
          {
            title: 'Prior failed task',
            failure_category: 'CODE_ERROR',
            latest_status_message: 'TypeError: cannot read property x of undefined',
            self_review_outcome: {
              verdict: 'FAIL',
              failed_checks: ['lint', 'types'],
              notes: 'lint errors remain',
            },
          },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('Error: TypeError: cannot read property x of undefined');
    expect(result).toContain('Self-review: verdict=FAIL; failed=lint,types; notes=lint errors remain');
  });

  it('renders direction_prior_analysis with only changed files when diff summary is absent', () => {
    const task = {
      direction_context: {
        title: 'Refactor module',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        direction_prior_analysis: [
          {
            title: 'Prior failed task',
            failure_category: 'CODE_ERROR',
            analysis_plan: 'Planned approach',
            pr_changed_files: ['foo.py', 'bar.ts'],
          },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('Files changed: foo.py, bar.ts');
  });

  it('renders review_iterations in prior analysis entries', () => {
    const task = {
      direction_context: {
        title: 'Refactor module',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        direction_prior_analysis: [
          {
            title: 'Almost worked',
            failure_category: 'CODE_ERROR',
            analysis_plan: 'Try X',
            review_iterations: 3,
          },
          {
            title: 'Fundamentally broken',
            failure_category: 'CODE_ERROR',
            analysis_plan: 'Try Y',
            review_iterations: 0,
          },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('Review iterations: 3 (3 cycles before failure)');
    expect(result).toContain('Review iterations: 0 (failed before review)');
  });

  it('renders structured review_feedback in prior analysis entries', () => {
    const task = {
      direction_context: {
        title: 'Refactor module',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        direction_prior_analysis: [
          {
            title: 'Reviewed task',
            failure_category: 'CODE_ERROR',
            analysis_plan: 'Try X',
            review_iterations: 2,
            review_feedback: [
              { severity: 'CRITICAL', description: 'missing null check in handler', file: 'foo.py', line: 42 },
              { severity: 'MAJOR', description: 'no test for edge case', file: 'test_foo.py' },
            ],
          },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('Review feedback: [CRITICAL] missing null check in handler (foo.py:42), [MAJOR] no test for edge case (test_foo.py)');
  });

  it('renders string review_feedback fallback in prior analysis entries', () => {
    const task = {
      direction_context: {
        title: 'Refactor module',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        direction_prior_analysis: [
          {
            title: 'Reviewed task',
            failure_category: 'CODE_ERROR',
            analysis_plan: 'Try X',
            review_iterations: 1,
            review_feedback: 'Reviewer asked for more comprehensive tests covering the new branch logic',
          },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('Review feedback: Reviewer asked for more comprehensive tests covering the new branch logic');
  });

  it('renders scope alongside failure_category in prior analysis header', () => {
    const task = {
      direction_context: {
        title: 'Refactor module',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        direction_prior_analysis: [
          {
            title: 'feat: add caching',
            failure_category: 'CODE_ERROR',
            scope: 'medium',
            analysis_plan: 'Try X',
          },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('- feat: add caching [CODE_ERROR, medium]');
  });

  it('does not render Prior analysis section when direction_prior_analysis is empty or absent', () => {
    const task = {
      direction_context: {
        title: 'Improve caching',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        direction_prior_analysis: [],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).not.toContain('**Prior analysis from failed attempts:**');
  });

  it('renders direction_successful_analysis as Approaches that worked section', () => {
    const task = {
      direction_context: {
        title: 'Refactor module',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        direction_successful_analysis: [
          {
            title: 'Successful task',
            analysis_plan: 'Add helper in services.py and wire into router',
            pr_changed_files: ['services.py', 'router.py'],
          },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('**Approaches that worked:**');
    expect(result).toContain('- Successful task');
    expect(result).toContain('Analysis plan: Add helper in services.py and wire into router');
    expect(result).toContain('Files changed: services.py, router.py');
  });

  it('renders scope tag and review iteration indicator for successful analysis entries', () => {
    const task = {
      direction_context: {
        title: 'Refactor module',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        direction_successful_analysis: [
          {
            title: 'Small fix',
            scope: 'small',
            analysis_plan: 'Touch one helper',
            pr_changed_files: ['svc.py'],
            review_iterations: 0,
          },
          {
            title: 'Medium fix',
            scope: 'medium',
            pr_changed_files: ['a.py', 'b.py'],
            review_iterations: 3,
          },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('- Small fix [small]');
    expect(result).toContain('1st-pass success');
    expect(result).toContain('- Medium fix [medium]');
    expect(result).toContain('3 review cycles');
  });

  it('does not render Approaches that worked when direction_successful_analysis is empty or absent', () => {
    const task = {
      direction_context: {
        title: 'Improve caching',
        priority: 'medium',
        failed_approaches: [],
        recent_outcomes: [],
        direction_successful_analysis: [],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).not.toContain('**Approaches that worked:**');
  });

  it('renders What tends to work with scope-constraint guidance when current scope underperforms', () => {
    const task = {
      context: { scope: 'medium' },
      direction_context: {
        title: 'x',
        priority: 'medium',
        scope_success_rates: {
          small: { created: 5, merged: 4, failed: 1 },
          medium: { created: 3, merged: 0, failed: 3 },
        },
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('**What tends to work:**');
    expect(result).toContain('medium-scope tasks in this direction merge at 0% (0/3)');
    expect(result).toContain('small-scope tasks merge at 80% (4/5)');
    expect(result).toContain('constraining your changes');
  });

  it('does not emit scope guidance when current-scope merge rate is already healthy', () => {
    const task = {
      context: { scope: 'small' },
      direction_context: {
        title: 'x',
        priority: 'medium',
        scope_success_rates: {
          small: { created: 5, merged: 4, failed: 1 },
          medium: { created: 3, merged: 0, failed: 3 },
        },
      },
    };
    const result = buildContextPrefix(task);
    expect(result).not.toContain('**What tends to work:**');
  });

  it('does not emit scope guidance when gap between scopes is small', () => {
    const task = {
      context: { scope: 'medium' },
      direction_context: {
        title: 'x',
        priority: 'medium',
        scope_success_rates: {
          small: { created: 10, merged: 3, failed: 7 },
          medium: { created: 10, merged: 2, failed: 8 },
        },
      },
    };
    const result = buildContextPrefix(task);
    expect(result).not.toContain('**What tends to work:**');
  });

  it('emits test-inclusion guidance when successful tasks touch tests and failed tasks do not', () => {
    const task = {
      direction_context: {
        title: 'x',
        priority: 'medium',
        direction_successful_analysis: [
          { title: 's1', pr_changed_files: ['backend/app/foo.py', 'backend/testing/test_foo.py'] },
          { title: 's2', pr_changed_files: ['agent/src/bar.ts', 'agent/src/bar.test.ts'] },
        ],
        direction_prior_analysis: [
          { title: 'f1', failure_category: 'CODE_ERROR', pr_changed_files: ['backend/app/baz.py'] },
          { title: 'f2', failure_category: 'CODE_ERROR', pr_changed_files: ['backend/app/qux.py'] },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('**What tends to work:**');
    expect(result).toContain('consistently touch test files');
  });

  it('does not emit test-inclusion guidance when both successful and failed tasks touch tests', () => {
    const task = {
      direction_context: {
        title: 'x',
        priority: 'medium',
        direction_successful_analysis: [
          { title: 's1', pr_changed_files: ['foo.py', 'test_foo.py'] },
          { title: 's2', pr_changed_files: ['bar.ts', 'bar.test.ts'] },
        ],
        direction_prior_analysis: [
          { title: 'f1', failure_category: 'CODE_ERROR', pr_changed_files: ['baz.py', 'test_baz.py'] },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).not.toContain('**What tends to work:**');
  });

  it('renders direction-scoped recurring review issues from direction_context.top_first_review_issues', () => {
    const task = {
      direction_context: {
        title: 'x',
        priority: 'medium',
        top_first_review_issues: [
          { description: 'missing validation on user input', count: 3, category: 'correctness' },
          { description: 'test coverage gap for edge cases', count: 2, category: 'tests' },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('**Recurring review issues in this direction (address before submitting):**');
    expect(result).toContain('- "missing validation on user input" (seen 3x, category: correctness)');
    expect(result).toContain('- "test coverage gap for edge cases" (seen 2x, category: tests)');
  });

  it('omits direction-scoped recurring review issues section when the field is empty or missing', () => {
    const withEmpty = buildContextPrefix({
      direction_context: { title: 'x', priority: 'medium', top_first_review_issues: [] },
    });
    const withMissing = buildContextPrefix({
      direction_context: { title: 'x', priority: 'medium' },
    });
    expect(withEmpty).not.toContain('Recurring review issues in this direction');
    expect(withMissing).not.toContain('Recurring review issues in this direction');
  });

  it('renders review_effectiveness per-category trends with trend indicators', () => {
    const task = {
      direction_context: {
        title: 'x',
        priority: 'medium',
        review_effectiveness: {
          categories: {
            correctness: { trend: 'increasing' },
            tests: { trend: 'decreasing' },
            style: { trend: 'stable' },
          },
        },
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('**Review effectiveness trends (per category):**');
    expect(result).toContain('- **correctness**: trending up ⚠');
    expect(result).toContain('- **tests**: trending down');
    expect(result).toContain('- **style**: persistent');
  });

  it('omits review_effectiveness section when categories are missing or empty', () => {
    const empty = buildContextPrefix({
      direction_context: { title: 'x', priority: 'medium', review_effectiveness: { categories: {} } },
    });
    const missing = buildContextPrefix({
      direction_context: { title: 'x', priority: 'medium' },
    });
    expect(empty).not.toContain('Review effectiveness trends');
    expect(missing).not.toContain('Review effectiveness trends');
  });

  it('renders review_quality_feedback calibration note and counts', () => {
    const task = {
      direction_context: {
        title: 'x',
        priority: 'medium',
        review_quality_feedback: {
          reviews_dismissed: 4,
          reviews_overridden: 1,
          user_issues_caught: 2,
          note: 'Self-reviews skew toward false positives on style issues.',
        },
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('**Self-review calibration:**');
    expect(result).toContain('- Self-reviews skew toward false positives on style issues.');
    expect(result).toContain('- self-review calibration — dismissed: 4, overridden: 1, user-caught: 2');
  });

  it('omits review_quality_feedback section when field is absent', () => {
    const result = buildContextPrefix({
      direction_context: { title: 'x', priority: 'medium' },
    });
    expect(result).not.toContain('Self-review calibration');
  });

  it('renders complexity signals as narrative guidance with only factors below 0.8', () => {
    const task = {
      context: {
        complexity_score: 0.3,
        complexity_signals: {
          files: 6,
          expansion_ratio: 2.3,
          modules: 3,
          neighbor_ratio: 0.8,
          factors: { files: 0.75, ratio: 0.6, clusters: 0.9, modules: 0.85, neighbors: 0.65 },
        },
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('**Complexity score:** 30/100');
    expect(result).toContain('This task spans 6 files across 3 modules.');
    expect(result).toContain('Historical expansion ratio of 2.3x');
    expect(result).toContain('High co-change neighbor ratio (0.8)');
    expect(result).toContain('File count (6) is above the high-merge band');
    // modules factor 0.85 is NOT below 0.8 — should be omitted
    expect(result).not.toContain('increase cross-system risk');
    // Should not dump raw key=value signals
    expect(result).not.toContain('Signals: files=6');
    expect(result).not.toMatch(/factors=\{/);
  });

  it('omits complexity signal narrative when score >= 0.5', () => {
    const task = {
      context: {
        complexity_score: 0.7,
        complexity_signals: { files: 6, factors: { files: 0.5 } },
      },
    };
    const result = buildContextPrefix(task);
    expect(result).not.toContain('Complexity score');
    expect(result).not.toContain('This task spans');
  });

  it('returns empty string for relativeTimeFromIso when iso is empty or unparseable', () => {
    expect(relativeTimeFromIso('')).toBe('');
    expect(relativeTimeFromIso('not-a-date')).toBe('');
  });

  it('formats days and hours for relativeTimeFromIso', () => {
    const now = new Date('2026-04-17T12:00:00Z');
    expect(relativeTimeFromIso('2026-04-15', now)).toBe('2d ago');
    expect(relativeTimeFromIso('2026-04-17T09:00:00Z', now)).toBe('3h ago');
    expect(relativeTimeFromIso('2026-04-10', now)).toBe('1w ago');
  });


  it('renders ## Task Scope section for create task with small scope', () => {
    const task = {
      payloads: [{ type: 'create' }],
      context: { scope: 'small' },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Task Scope');
    expect(result).toContain('Scope: small — expect <50 lines across 1-3 files');
  });

  it('renders ## Task Scope section for fix task with medium scope', () => {
    const task = {
      payloads: [{ type: 'fix' }],
      context: { scope: 'medium' },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Task Scope');
    expect(result).toContain('Scope: medium — expect 50-200 lines across 2-5 files');
  });

  it('renders ## Task Scope section for refactor task with large scope', () => {
    const task = {
      payloads: [{ type: 'refactor' }],
      context: { scope: 'large' },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Task Scope');
    expect(result).toContain('Scope: large — cross-component');
  });

  it('includes effort in scope section when set on payload', () => {
    const task = {
      payloads: [{ type: 'create', effort: 'high' }],
      context: { scope: 'medium' },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Task Scope');
    expect(result).toContain('Effort: high');
  });

  it('renders scope calibration note when scope_calibrated_from is present and differs', () => {
    const task = {
      payloads: [{ type: 'create' }],
      context: { scope: 'small', scope_calibrated_from: 'medium' },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Task Scope');
    expect(result).toContain('Scope: small');
    expect(result).toContain('originally scoped as medium but calibrated down to small');
    expect(result).toContain('medium-scope tasks have struggled');
  });

  it('renders upgrade note (decompose) when scope_calibrated_from is smaller than taskScope', () => {
    const task = {
      payloads: [{ type: 'create' }],
      context: { scope: 'large', scope_calibrated_from: 'medium' },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Task Scope');
    expect(result).toContain('Scope: large');
    expect(result).toContain('originally scoped as medium but calibrated up to large');
    expect(result).toContain('Decompose the work');
    expect(result).not.toContain('calibrated down');
  });

  it('includes per-scope merge rates in calibration note when available', () => {
    const task = {
      payloads: [{ type: 'create' }],
      context: { scope: 'small', scope_calibrated_from: 'medium' },
      direction_context: {
        scope_success_rates: {
          small: { created: 10, merged: 8, failed: 2 },
          medium: { created: 4, merged: 1, failed: 3 },
        },
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('small-scope tasks merge at 80% (8/10)');
    expect(result).toContain('medium-scope tasks merge at 25% (1/4)');
  });

  it('omits scope calibration note when scope_calibrated_from matches current scope', () => {
    const task = {
      payloads: [{ type: 'create' }],
      context: { scope: 'small', scope_calibrated_from: 'small' },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Task Scope');
    expect(result).not.toContain('originally scoped as');
    expect(result).not.toContain('calibrated down');
  });

  it('omits scope calibration note when scope_calibrated_from is absent', () => {
    const task = {
      payloads: [{ type: 'create' }],
      context: { scope: 'small' },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Task Scope');
    expect(result).not.toContain('originally scoped as');
  });

  it('does not render ## Task Scope for review task type', () => {
    const task = {
      payloads: [{ type: 'review' }],
      context: { scope: 'small' },
    };
    const result = buildContextPrefix(task);
    expect(result).not.toContain('## Task Scope');
  });

  it('does not render ## Task Scope when scope is absent', () => {
    const task = {
      payloads: [{ type: 'create' }],
      context: {},
    };
    const result = buildContextPrefix(task);
    expect(result).not.toContain('## Task Scope');
  });

  it('renders ## Module-Specific Review History when module_feedback_warnings is present', () => {
    const task = {
      payloads: [{ type: 'create' }],
      context: {},
      project_context: {
        module_feedback_warnings: [
          {
            file: 'backend/app/orchestrator.py',
            top_categories: { 'test-coverage': 0.6, 'error-handling': 0.25 },
            sample_size: 8,
          },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('### Module-Specific Review History');
    expect(result).toContain('backend/app/orchestrator.py');
    expect(result).toContain('60% test-coverage');
    expect(result).toContain('25% error-handling');
    expect(result).toContain('(8 issues)');
  });

  it('does not render module feedback section when module_feedback_warnings is absent', () => {
    const task = {
      payloads: [{ type: 'create' }],
      context: {},
      project_context: {
        active_tasks: [],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).not.toContain('Module-Specific Review History');
  });

  it('renders ## Architecture Context section with system name, status, and readiness', () => {
    const task = {
      project_context: {
        architecture_context: [
          { name: 'Auth', status: 'functional', ready_for_features: true, description: 'Handles JWT tokens.' },
          { name: 'Database', status: 'mature', ready_for_features: false, description: 'ORM layer.' },
        ],
      },
    };
    const result = buildContextPrefix(task);
    expect(result).toContain('## Architecture Context');
    expect(result).toContain('**Auth**: functional, ready for features — Handles JWT tokens.');
    expect(result).toContain('**Database**: mature, not ready for features — ORM layer.');
  });

  it('does not render ## Architecture Context when architecture_context is absent', () => {
    const task = {
      project_context: { active_tasks: [] },
    };
    const result = buildContextPrefix(task);
    expect(result).not.toContain('## Architecture Context');
  });

  it('does not render ## Architecture Context when architecture_context is an empty array', () => {
    const task = {
      project_context: { architecture_context: [] },
    };
    const result = buildContextPrefix(task);
    expect(result).not.toContain('## Architecture Context');
  });
});

// ── synthesizeSuccessPatterns ────────────────────────────────────────────────
describe('synthesizeSuccessPatterns', () => {
  it('returns empty when no relevant signal is present', () => {
    expect(synthesizeSuccessPatterns({}, 'small')).toEqual([]);
    expect(synthesizeSuccessPatterns({ scope_success_rates: {} }, '')).toEqual([]);
  });

  it('requires at least 2 tasks in the comparison scope to avoid noise', () => {
    const out = synthesizeSuccessPatterns(
      {
        scope_success_rates: {
          medium: { created: 4, merged: 0 },
          small: { created: 1, merged: 1 },
        },
      },
      'medium',
    );
    expect(out).toEqual([]);
  });

  it('skips scope guidance when no taskScope is provided', () => {
    const out = synthesizeSuccessPatterns(
      {
        scope_success_rates: {
          small: { created: 5, merged: 4 },
          medium: { created: 3, merged: 0 },
        },
      },
      '',
    );
    expect(out).toEqual([]);
  });

  it('does not emit test-inclusion guidance with only one successful task', () => {
    const out = synthesizeSuccessPatterns(
      {
        direction_successful_analysis: [
          { title: 's', pr_changed_files: ['foo.py', 'test_foo.py'] },
        ],
        direction_prior_analysis: [
          { title: 'f', pr_changed_files: ['bar.py'] },
        ],
      },
      '',
    );
    expect(out).toEqual([]);
  });
});

// ── synthesizeFailureHotspots ────────────────────────────────────────────────
describe('synthesizeFailureHotspots', () => {
  it('lists files appearing in 2+ failed entries and excludes single-occurrence files', () => {
    const out = synthesizeFailureHotspots({
      direction_prior_analysis: [
        { pr_changed_files: ['backend/app/orchestrator.py', 'backend/app/services.py'] },
        { pr_changed_files: ['backend/app/orchestrator.py', 'backend/app/unique.py'] },
        { pr_changed_files: ['backend/app/orchestrator.py', 'backend/app/services.py'] },
      ],
    });
    expect(out).toEqual([
      { file: 'backend/app/orchestrator.py', count: 3 },
      { file: 'backend/app/services.py', count: 2 },
    ]);
  });

  it('returns empty when no file appears in multiple failures', () => {
    const out = synthesizeFailureHotspots({
      direction_prior_analysis: [
        { pr_changed_files: ['a.py'] },
        { pr_changed_files: ['b.py'] },
      ],
    });
    expect(out).toEqual([]);
  });

  it('skips test files even if they appear in multiple failures', () => {
    const out = synthesizeFailureHotspots({
      direction_prior_analysis: [
        { pr_changed_files: ['backend/testing/test_foo.py', 'backend/app/foo.py'] },
        { pr_changed_files: ['backend/testing/test_foo.py', 'backend/app/foo.py'] },
      ],
    });
    expect(out).toEqual([{ file: 'backend/app/foo.py', count: 2 }]);
  });

  it('renders the hotspots line in buildContextPrefix output', () => {
    const result = buildContextPrefix({
      direction_context: {
        title: 'x',
        priority: 'medium',
        direction_prior_analysis: [
          { title: 'f1', pr_changed_files: ['backend/app/orchestrator.py', 'a.py'] },
          { title: 'f2', pr_changed_files: ['backend/app/orchestrator.py', 'b.py'] },
        ],
      },
    });
    expect(result).toContain('Files appearing in multiple recent failures: backend/app/orchestrator.py (2x)');
  });

  it('omits the hotspots line when no file appears in 2+ failures', () => {
    const result = buildContextPrefix({
      direction_context: {
        title: 'x',
        priority: 'medium',
        direction_prior_analysis: [
          { title: 'f1', pr_changed_files: ['a.py'] },
          { title: 'f2', pr_changed_files: ['b.py'] },
        ],
      },
    });
    expect(result).not.toContain('Files appearing in multiple recent failures');
  });
});

// ── formatProjectContext ──────────────────────────────────────────────────────
describe('formatProjectContext', () => {
  it('returns a Project Context header even with empty context', () => {
    const result = formatProjectContext({});
    expect(result).toContain('## Project Context');
  });

  it('includes Active Tasks section when active_tasks is populated', () => {
    const ctx = {
      active_tasks: [
        {
          task_type: 'create',
          title: 'Implement login',
          status: 'IN_PROGRESS',
          prompt: 'Add OAuth login flow',
        },
      ],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('### Active Tasks');
    expect(result).toContain('Implement login');
  });

  it('includes Open PRs section when open_prs is populated', () => {
    const ctx = {
      open_prs: ['PR #42: Add dark mode', 'PR #43: Fix typo in README'],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('### Open PRs');
    expect(result).toContain('PR #42');
  });

  it('includes Sibling Tasks section when sibling_tasks is populated', () => {
    const ctx = {
      sibling_tasks: [
        { status: 'IN_PROGRESS', title: 'Write documentation', related_files: [], active: true },
      ],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('Sibling Tasks');
    expect(result).toContain('Write documentation');
  });

  it('renders failure details (failure_class, error excerpt, changed files) for terminal siblings', () => {
    const ctx = {
      sibling_tasks: [
        {
          status: 'FAILED',
          title: 'Fix auth flow',
          active: false,
          failure_class: 'CODE_ERROR',
          error_excerpt: 'test assertions failed in auth_test.py',
          changed_files: ['src/auth.py', 'src/middleware.py'],
          related_files: ['src/auth.py'],
        },
      ],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('[FAILED] Fix auth flow');
    expect(result).toContain('[CODE_ERROR: test assertions failed in auth_test.py]');
    expect(result).toContain('(changed: src/auth.py, src/middleware.py)');
    // Should not duplicate the related_files-based files: suffix when changed_files is present
    expect(result).not.toContain('(files: src/auth.py)');
  });

  it('falls back to related_files when terminal sibling has no changed_files', () => {
    const ctx = {
      sibling_tasks: [
        {
          status: 'FAILED',
          title: 'Refactor cache',
          active: false,
          failure_class: 'INFRA',
          error_excerpt: 'provisioning failed',
          related_files: ['src/cache.py'],
        },
      ],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('[INFRA: provisioning failed]');
    expect(result).toContain('(files: src/cache.py)');
  });

  it('includes Recently Completed Tasks section when populated', () => {
    const ctx = {
      recently_completed_tasks: [
        {
          task_type: 'create',
          title: 'Completed feature',
          pr_url: 'https://github.com/org/repo/pull/10',
          research_findings_summary: '',
        },
      ],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('### Recently Completed Tasks');
    expect(result).toContain('Completed feature');
  });

  it('includes enriched fields (direction_id, related_files, prompt, changed_files) on completed tasks', () => {
    const ctx = {
      recently_completed_tasks: [
        {
          task_type: 'create',
          title: 'Succeeded task',
          pr_url: 'https://github.com/org/repo/pull/1',
          direction_id: 'd-123',
          related_files: ['a.py', 'b.py', 'c.py', 'd.py'],
          prompt: 'Add caching to the user lookup endpoint to reduce DB load.',
        },
        {
          task_type: 'create',
          title: 'Failed task',
          failure_reason: 'tests failed',
          failure_class: 'CODE_ERROR',
          changed_files: ['x.py', 'y.py'],
        },
      ],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('dir=d-123');
    expect(result).toContain('files: a.py, b.py, c.py +1');
    expect(result).toContain('prompt: Add caching');
    expect(result).toContain('changed: x.py, y.py');
  });

  it('exercises parseResearchSummary via research_findings_summary on completed tasks', () => {
    const findings = JSON.stringify([
      { title: 'Perf finding', description: 'The query is slow because of N+1 joins.' },
      { title: 'Second finding', description: 'Missing index on user_id column.' },
    ]);
    const ctx = {
      recently_completed_tasks: [
        {
          task_type: 'research',
          title: 'Research task',
          research_findings_summary: findings,
        },
      ],
    };
    const result = formatProjectContext(ctx);
    // The summary should be parsed and truncated into the output
    expect(result).toContain('Research task');
    expect(result).toContain('Recently Completed Tasks');
  });

  it('keeps output under ~10 000 chars even with very large context (CONTEXT_BUDGET enforcement)', () => {
    const manyTasks = Array.from({ length: 60 }, (_, i) => ({
      task_type: 'create',
      title: `Task number ${i}`,
      status: 'IN_PROGRESS',
      prompt: `Prompt for task ${i}. `.repeat(10),
    }));
    const ctx = {
      active_tasks: manyTasks.slice(0, 20),
      open_prs: Array.from({ length: 20 }, (_, i) => `PR #${i}: Some long PR title here`),
      sibling_tasks: manyTasks.slice(0, 20).map((t) => ({ ...t, related_files: [] })),
      recently_completed_tasks: manyTasks.slice(0, 20).map((t) => ({ ...t, pr_url: '' })),
    };
    const result = formatProjectContext(ctx);
    expect(result.length).toBeLessThan(10_000);
  });

  it('includes task-type-specific pipeline health stat when by_type is provided', () => {
    const ctx = {
      pipeline_health: {
        success_rate: 0.85,
        trend: 'stable',
        first_attempt_success_rate: {
          overall: 0.72,
          sample_size: 50,
          by_type: { create: 0.9 },
        },
        failure_distribution: { CODE_ERROR: 3, RATE_LIMIT: 1 },
        direction_stats: [],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Pipeline Health');
    expect(result).toContain('Success rate');
    expect(result).toContain('90%');
  });

  it('renders direction_stats with cvr label using conversion_rate (not merge rate)', () => {
    const ctx = {
      pipeline_health: {
        success_rate: 0.85,
        trend: 'stable',
        failure_distribution: {},
        direction_stats: [
          {
            direction_id: 'd-abcdef12345678',
            cycles: 5,
            possibly_saturated: false,
            conversion_rate: 0.42,
            proven_prs_merged: 2,
            consumed_prs_merged: 1,
          },
        ],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('cvr 0.42');
    expect(result).toContain('5 cycles');
    expect(result).toContain('proof 2 proven (1 consumed downstream)');
    expect(result).not.toContain('merge rate');
  });

  it('omits zero-only pipeline direction proof split for legacy payloads', () => {
    const ctx = {
      pipeline_health: {
        success_rate: 0.85,
        trend: 'stable',
        failure_distribution: {},
        direction_stats: [
          {
            direction_id: 'd-legacy12345678',
            cycles: 2,
            possibly_saturated: false,
            conversion_rate: 0.5,
            proven_prs_merged: 0,
            consumed_prs_merged: 0,
          },
        ],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('cvr 0.5');
    expect(result).not.toContain('0 proven');
    expect(result).not.toContain('consumed downstream');
  });

  it('includes scope-specific pipeline health stat when by_scope is provided', () => {
    const ctx = {
      pipeline_health: {
        success_rate: 0.85,
        trend: 'stable',
        first_attempt_success_rate: {
          overall: 0.72,
          sample_size: 50,
          by_type: { create: 0.9 },
          by_scope: { medium: 0.75 },
        },
        failure_distribution: {},
        direction_stats: [],
      },
    };
    const result = formatProjectContext(ctx, 'create', 'medium');
    expect(result).toContain('Your scope (medium): 75%');
  });

  it('shows trend indicators in Common Review Issues when review_effectiveness is provided', () => {
    const ctx = {
      review_issue_distribution: {
        'test-coverage': 0.35,
        'error-handling': 0.25,
        'type-safety': 0.15,
      },
      review_effectiveness: {
        categories: {
          'test-coverage': { trend: 'decreasing' },
          'error-handling': { trend: 'stable' },
          'type-safety': { trend: 'increasing' },
        },
        persistent_issues: [],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('test-coverage');
    expect(result).toContain(', trending down');
    expect(result).toContain(', persistent');
    expect(result).toContain(', trending up');
  });

  it('omits trend indicators when review_effectiveness is absent', () => {
    const ctx = {
      review_issue_distribution: {
        'test-coverage': 0.35,
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('test-coverage');
    expect(result).not.toContain('trending');
    expect(result).not.toContain('persistent');
  });

  it('surfaces persistent review issues in pipeline health', () => {
    const ctx = {
      pipeline_health: {
        success_rate: 0.80,
        trend: 'stable',
        failure_distribution: {},
        direction_stats: [],
        first_attempt_success_rate: { overall: 0.60, sample_size: 15 },
        persistent_review_issues: ['test-coverage', 'error-handling'],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Persistent review issues');
    expect(result).toContain('test-coverage');
    expect(result).toContain('error-handling');
  });

  it('renders SR blind spots block when self_review_catch_rate has qualifying categories', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'error-handling': { miss_rate: 0.8 },
          'test-coverage': { miss_rate: 0.2 },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Self-Review Blind Spots');
    expect(result).toContain('error-handling (80% missed)');
    expect(result).toContain('apply extra scrutiny');
  });

  it('omits SR blind spots block when self_review_catch_rate is absent', () => {
    const ctx = { review_issue_distribution: { 'test-coverage': 0.5 } };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Self-Review Blind Spots');
  });

  it('omits SR blind spots block when self_review_catch_rate has empty categories', () => {
    const ctx = { self_review_catch_rate: { categories: {} } };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Self-Review Blind Spots');
  });

  it('sorts SR blind spots by miss_rate descending and caps at top-5', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'cat-a': { miss_rate: 0.1 },
          'cat-b': { miss_rate: 0.9 },
          'cat-c': { miss_rate: 0.7 },
          'cat-d': { miss_rate: 0.5 },
          'cat-e': { miss_rate: 0.3 },
          'cat-f': { miss_rate: 0.8 },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Self-Review Blind Spots');
    // Top-5 by miss_rate: cat-b(90%), cat-f(80%), cat-c(70%), cat-d(50%), cat-e(30%)
    expect(result).toContain('cat-b (90% missed)');
    expect(result).toContain('cat-f (80% missed)');
    expect(result).not.toContain('cat-a');
    // cat-b should appear before cat-c in the output
    const bIdx = result.indexOf('cat-b');
    const cIdx = result.indexOf('cat-c');
    expect(bIdx).toBeLessThan(cIdx);
  });

  it('renders example issues in SR blind spots block when examples present', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'error-handling': {
            miss_rate: 0.8,
            examples: [
              { severity: 'MAJOR', desc: 'Missing null check in handler' },
              { severity: 'MINOR', desc: 'Unhandled async error' },
            ],
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Self-Review Blind Spots');
    expect(result).toContain('[MAJOR] Missing null check in handler');
    expect(result).toContain('[MINOR] Unhandled async error');
  });

  it('falls back to percentage-only line per category when examples absent', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'error-handling': { miss_rate: 0.8 },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Self-Review Blind Spots');
    expect(result).toContain('error-handling');
    expect(result).toContain('80% missed');
    expect(result).not.toContain('[MAJOR]');
    expect(result).not.toContain('[MINOR]');
  });

  it('falls back to percentage-only when examples is empty array', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'error-handling': { miss_rate: 0.8, examples: [] },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('error-handling');
    expect(result).toContain('80% missed');
    expect(result).not.toContain('[MAJOR]');
  });

  it('still renders category line even when its examples are missing', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'correctness': { miss_rate: 0.9 },
          'error-handling': { miss_rate: 0.7, examples: [{ severity: 'CRITICAL', desc: 'Off-by-one in loop' }] },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    // Both categories should appear
    expect(result).toContain('correctness');
    expect(result).toContain('error-handling');
    // Only the one with examples shows severity tag
    expect(result).toContain('[CRITICAL] Off-by-one in loop');
    // correctness has no examples so no tags for it
    const correctnessIdx = result.indexOf('correctness');
    const criticalIdx = result.indexOf('[CRITICAL]');
    // CRITICAL tag should appear after correctness bullet
    expect(criticalIdx).toBeGreaterThan(correctnessIdx);
  });

  it('shows trajectory hint when previous_hit_rate and current_hit_rate are present', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'error-handling': {
            miss_rate: 0.3,
            current_hit_rate: 0.7,
            previous_hit_rate: 0.8,
            trend: 'decreasing',
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Self-Review Blind Spots');
    // Should show trajectory: 20% → 30% (−10pp regressing)
    expect(result).toContain('last cycle: 20%');
    expect(result).toContain('now: 30%');
    expect(result).toContain('−10pp');
  });

  it('highlights regression when trend is decreasing', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'error-handling': {
            miss_rate: 0.4,
            current_hit_rate: 0.6,
            previous_hit_rate: 0.7,
            trend: 'decreasing',
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('⚠ Regression: scrutinize extra carefully');
    expect(result).toContain('regressing');
  });

  it('shows improvement when trajectory indicates lower miss rate', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'correctness': {
            miss_rate: 0.2,
            current_hit_rate: 0.8,
            previous_hit_rate: 0.6,
            trend: 'increasing',
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('last cycle: 40%');
    expect(result).toContain('now: 20%');
    expect(result).toContain('+20pp improving');
  });

  it('includes co-change budget note for medium-scope tasks', () => {
    const ctx = { cochange_files: ['src/foo.ts', 'src/bar.ts'] };
    const result = formatProjectContext(ctx, 'create', 'medium', '', 3);
    expect(result).toContain('Frequently Co-Changed Files');
    expect(result).toContain('Budget note: you have 7 production file slots remaining');
  });

  it('omits co-change budget note for non-medium scopes', () => {
    const ctx = { cochange_files: ['src/foo.ts'] };
    const smallResult = formatProjectContext(ctx, 'create', 'small', '', 1);
    expect(smallResult).toContain('Frequently Co-Changed Files');
    expect(smallResult).not.toContain('Budget note');
    const unscoped = formatProjectContext(ctx, 'create');
    expect(unscoped).not.toContain('Budget note');
  });

  it('omits persistent review issues line when list is empty or absent', () => {
    const ctx = {
      pipeline_health: {
        success_rate: 0.80,
        trend: 'stable',
        failure_distribution: {},
        direction_stats: [],
        first_attempt_success_rate: { overall: 0.60, sample_size: 15 },
        persistent_review_issues: [],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Persistent review issues');
  });

  it('renders direction-scoped SR calibration block when self_review_catch_rate_for_direction present', () => {
    const ctx = {
      self_review_catch_rate_for_direction: {
        direction_id: 'd-abc123',
        direction_text: 'Self-review catches issues a skilled human reviewer would',
        sample_count: 5,
        miss_rate: 0.6,
        categories: {
          'regression_risk': {
            miss_rate: 0.7,
            examples: [{ severity: 'MAJOR', desc: 'Mocked migration masked breakage' }],
          },
          'scope-drift': { miss_rate: 0.4 },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('For This Direction: SR Calibration');
    expect(result).toContain('Self-review catches issues a skilled human reviewer would');
    expect(result).toContain('5 tasks sampled');
    expect(result).toContain('regression_risk (70% missed)');
    expect(result).toContain('[MAJOR] Mocked migration masked breakage');
    // worst-first ordering
    const regIdx = result.indexOf('regression_risk');
    const drIdx = result.indexOf('scope-drift');
    expect(regIdx).toBeLessThan(drIdx);
  });

  it('omits direction-scoped SR calibration block when self_review_catch_rate_for_direction absent', () => {
    const ctx = {
      self_review_catch_rate: { categories: { 'error-handling': { miss_rate: 0.8 } } },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('For This Direction: SR Calibration');
  });

  it('renders direction-scoped block alongside project-wide Self-Review Blind Spots', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: { 'error-handling': { miss_rate: 0.8 } },
      },
      self_review_catch_rate_for_direction: {
        direction_id: 'd-abc123',
        direction_text: 'Some direction',
        sample_count: 4,
        miss_rate: 0.5,
        categories: { 'regression_risk': { miss_rate: 0.6 } },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Self-Review Blind Spots');
    expect(result).toContain('For This Direction: SR Calibration');
    // The project-wide block should appear before the direction block
    const projectIdx = result.indexOf('Self-Review Blind Spots');
    const dirIdx = result.indexOf('For This Direction: SR Calibration');
    expect(projectIdx).toBeLessThan(dirIdx);
  });

  it('omits direction-scoped block when categories empty', () => {
    const ctx = {
      self_review_catch_rate_for_direction: {
        direction_id: 'd-abc123',
        direction_text: 'x',
        sample_count: 3,
        categories: {},
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('For This Direction: SR Calibration');
  });

  it('renders sr_blind_spot_themes section when themes are provided', () => {
    const ctx = {
      sr_blind_spot_themes: [
        { theme: 'did not verify auth flow', count: 5 },
        { theme: 'untested error handling path', count: 2 },
        { theme: 'race condition in async fetch', count: 1 },
      ],
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Project-wide self-review blind spots (last 14 days');
    expect(result).toContain('did not verify auth flow (5 tasks)');
    expect(result).toContain('untested error handling path (2 tasks)');
    expect(result).toContain('race condition in async fetch (1 task)');
  });

  it('omits sr_blind_spot_themes section when array is empty or absent', () => {
    const empty = formatProjectContext({ sr_blind_spot_themes: [] }, 'create');
    expect(empty).not.toContain('Project-wide self-review blind spots');
    const absent = formatProjectContext({}, 'create');
    expect(absent).not.toContain('Project-wide self-review blind spots');
  });
});

// ── formatProjectBlindSpotSummary ─────────────────────────────────────────────
describe('formatProjectBlindSpotSummary', () => {
  it('returns empty string for empty array', () => {
    expect(formatProjectBlindSpotSummary([])).toBe('');
  });

  it('renders themes with counts and correct singular/plural', () => {
    const result = formatProjectBlindSpotSummary([
      { theme: 'theme one', count: 1 },
      { theme: 'theme two', count: 2 },
    ]);
    expect(result).toContain('- theme one (1 task)');
    expect(result).toContain('- theme two (2 tasks)');
    expect(result).toContain('Project-wide self-review blind spots');
  });

  it('caps at top 8 entries', () => {
    const themes = Array.from({ length: 12 }, (_, i) => ({
      theme: `theme number ${i}`,
      count: 12 - i,
    }));
    const result = formatProjectBlindSpotSummary(themes);
    expect(result).toContain('theme number 0');
    expect(result).toContain('theme number 7');
    expect(result).not.toContain('theme number 8');
    expect(result).not.toContain('theme number 11');
  });
});

// ── configureAgentPermissions ─────────────────────────────────────────────────
describe('configureAgentPermissions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it.each(getReadOnlyTaskTypes())(
    'returns [Write, Edit, NotebookEdit] for read-only type "%s"',
    (taskType) => {
      const result = configureAgentPermissions(taskType);
      expect(result).toEqual(['Write', 'Edit', 'NotebookEdit']);
    },
  );

  it.each(getWriteTaskTypes())(
    'returns [] (no restrictions) for write type "%s"',
    (taskType) => {
      const result = configureAgentPermissions(taskType);
      expect(result).toEqual([]);
    },
  );

  it('returns [] for an unknown task type', () => {
    expect(configureAgentPermissions('unknown_type')).toEqual([]);
  });

  it('installs exit-1 pre-commit and pre-push block hooks for a read-only type', () => {
    configureAgentPermissions('research');
    const writes = vi.mocked(fs.writeFileSync).mock.calls;
    const hookPaths = writes.map((c) => c[0]);
    expect(hookPaths).toContain('/tmp/workspace/.git/hooks/pre-commit');
    expect(hookPaths).toContain('/tmp/workspace/.git/hooks/pre-push');
    for (const [, body] of writes) {
      expect(typeof body).toBe('string');
      expect(body as string).toContain('exit 1');
      expect(body as string).toContain('read-only analysis task');
    }
  });

  // Regression: a write task reusing a warm VM whose prior role was read-only
  // must clear the leftover block hooks, or it can't commit/push its work.
  it('removes leftover read-only block hooks for a write type on a reused warm VM', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string)
      .mockReturnValue(
        '#!/bin/sh\necho "ERROR: This is a read-only analysis task. Do NOT commit or push." >&2\nexit 1\n',
      );

    const result = configureAgentPermissions('fix');

    expect(result).toEqual([]);
    const unlinked = vi.mocked(fs.unlinkSync).mock.calls.map((c) => c[0]);
    expect(unlinked).toContain('/tmp/workspace/.git/hooks/pre-commit');
    expect(unlinked).toContain('/tmp/workspace/.git/hooks/pre-push');
  });

  it('leaves a non-read-only hook untouched for a write type (marker guard)', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string)
      .mockReturnValue('#!/bin/sh\nnpm run lint\n');

    configureAgentPermissions('fix');

    expect(fs.unlinkSync).not.toHaveBeenCalled();
  });

  it('is a no-op for a write type when no leftover hooks exist', () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    configureAgentPermissions('fix');
    expect(fs.unlinkSync).not.toHaveBeenCalled();
  });

  it('gives the strategic resident seat an explicit read-only posture without rewriting origin', () => {
    const result = configureResidentAgentPermissions('strategic_review');

    expect(result).toEqual(['Write', 'Edit', 'NotebookEdit']);
    expect(execFileSync).not.toHaveBeenCalled();
    const writes = vi.mocked(fs.writeFileSync).mock.calls;
    const hookPaths = writes.map((c) => c[0]);
    expect(hookPaths).toContain('/tmp/workspace/.git/hooks/pre-commit');
    expect(hookPaths).toContain('/tmp/workspace/.git/hooks/pre-push');
  });
});

// ── getTaskTypeHints ──────────────────────────────────────────────────────────
describe('getTaskTypeHints', () => {
  it('returns static hint from TASK_TYPE_HINTS when buildConfig is undefined', () => {
    const result = getTaskTypeHints(undefined)['create'];
    expect(result).toBe(TASK_TYPE_HINTS['create']);
    // 2026-05-04: hint now points at ./check / ./pr / ./done helpers (the
    // wrapper-driven lint-loop language was removed in 5c186061).
    expect(result).toContain('./check');
    expect(result).toContain('./pr open');
    expect(result).toContain('./done');
  });

  it('returns undefined for unknown task type when buildConfig is undefined', () => {
    const result = getTaskTypeHints(undefined)['unknown_type'];
    expect(result).toBeUndefined();
  });

  it('fix hint requires reproduce-on-HEAD first with a non-reproducible no-op off-ramp', () => {
    const hint = TASK_TYPE_HINTS['fix'];
    expect(hint).toContain('REPRODUCE FIRST');
    expect(hint).toContain('reproduces on current HEAD');
    expect(hint).toContain('./done --already-done');
  });

  it('fix hint references the structured reproduce verdict and names all three outcomes', () => {
    const hint = TASK_TYPE_HINTS['fix'];
    expect(hint).toContain('already_resolved');
    expect(hint).toContain('reproduce-on-HEAD verdict');
    expect(hint).toContain('indeterminate');
    expect(hint).toContain('./done --reproduced');
    expect(hint).toContain('./done --indeterminate');
  });

  it('fix hint separates code-review fix and defect reproduction into two labeled branches', () => {
    const hint = TASK_TYPE_HINTS['fix'];
    // Two distinct labeled branches
    expect(hint).toContain('Code-review fix');
    expect(hint).toContain('Defect reproduction');
    // Code-review branch (common case) is front-loaded before defect reproduction
    const codeReviewIdx = hint.indexOf('Code-review fix');
    const defectIdx = hint.indexOf('Defect reproduction');
    expect(codeReviewIdx).toBeGreaterThan(-1);
    expect(defectIdx).toBeGreaterThan(-1);
    expect(codeReviewIdx).toBeLessThan(defectIdx);
    // Code-review branch has the end-to-end flow inline (no reproduce-first noise)
    const codeReviewSection = hint.substring(codeReviewIdx, defectIdx);
    expect(codeReviewSection).toContain('./check');
    expect(codeReviewSection).toContain('./pr open');
    expect(codeReviewSection).toContain('./done');
  });

  it('investigate hint asks for the canonical reproduce verdict, not free-text only', () => {
    const hint = TASK_TYPE_HINTS['investigate'];
    expect(hint).toContain('reproduce-on-HEAD verdict');
    expect(hint).toContain('indeterminate');
  });

  it('returns static hint when buildConfig has empty directories array', () => {
    const result = getTaskTypeHints({ directories: [] })['create'];
    expect(result).toBe(TASK_TYPE_HINTS['create']);
  });

  it('generates dynamic hint referencing test commands when buildConfig has directories', () => {
    const result = getTaskTypeHints({
      directories: [
        { path: 'web', type: 'node', lint_cmd: 'npm run lint', build_cmd: 'npm run build', test_cmd: 'npm test' },
      ],
    })['create'];
    expect(result).toContain('npm test');
  });

  it('includes only present commands in dynamic hint (skips missing ones)', () => {
    const result = getTaskTypeHints({
      directories: [
        { path: 'api', type: 'python', test_cmd: 'pytest' },
      ],
    })['create'];
    expect(result).toContain('pytest');
    // No npm commands configured for this python-only directory
    expect(result).not.toContain('npm run lint');
  });

  it('substitutes promotionTarget into release_audit hint when provided', () => {
    const result = getTaskTypeHints(undefined, 'production')['release_audit'];
    expect(result).toContain('(production)');
    expect(result).toContain("production's standards");
    expect(result).not.toContain('(main)');
  });

  it('names a rolling integration promotionTarget (automain) as the promotion branch, not an LTS branch', () => {
    const result = getTaskTypeHints(undefined, 'automain')['release_audit'];
    expect(result).toContain('promotion/integration branch (automain)');
    expect(result).toContain("automain's standards");
    // automain is a rolling integration/rollup branch, not an LTS branch —
    // the dynamic hint must not describe it as one.
    expect(result).not.toContain('LTS branch');
    expect(result).not.toMatch(/LTS/);
  });

  it('substitutes non-main promotionTarget like release/v2 into release_audit hint', () => {
    const result = getTaskTypeHints(undefined, 'release/v2')['release_audit'];
    expect(result).toContain('(release/v2)');
    expect(result).toContain("release/v2's standards");
  });

  it('uses generic LTS / promotion branch wording in release_audit hint when no promotionTarget provided', () => {
    const result = getTaskTypeHints(undefined)['release_audit'];
    expect(result).toContain('LTS / promotion branch');
    expect(result).not.toContain('(main)');
  });

  it('keeps generic LTS / promotion branch wording in release_audit when promotionTarget is absent and buildConfig has dirs', () => {
    const result = getTaskTypeHints({ directories: [{ path: 'src', type: 'node' }] })['release_audit'];
    expect(result).toContain('LTS / promotion branch');
    expect(result).not.toContain('(main)');
  });
});

// ── discoverRelatedTests ──────────────────────────────────────────────────────
describe('discoverRelatedTests', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns empty string when relatedFiles is empty', () => {
    const result = discoverRelatedTests(null, []);
    expect(result).toBe('');
  });

  it('returns empty string when relatedFiles is empty with buildConfig', () => {
    const result = discoverRelatedTests(null, [], {
      directories: [{ path: 'web', type: 'node' }],
    });
    expect(result).toBe('');
  });

  it('falls back to hardcoded discovery when buildConfig is undefined', () => {
    // With no matching test files (fs mocked to not find them), returns empty string
    const result = discoverRelatedTests(null, ['backend/app/main.py'], undefined);
    // The function tries to find test files via fs; since existsSync returns false, it returns ''
    expect(typeof result).toBe('string');
  });

  it('uses buildConfig directory config to discover tests for a matching directory', () => {
    // With buildConfig pointing to a directory, the function looks for tests
    // Since existsSync is mocked to false, no test files are found but no error thrown
    const result = discoverRelatedTests(null, ['myapp/src/utils.ts'], {
      directories: [{ path: 'myapp', type: 'node' }],
    });
    expect(typeof result).toBe('string');
  });

  it('skips files not matching any buildConfig directory', () => {
    const result = discoverRelatedTests(null, ['other/file.ts'], {
      directories: [{ path: 'myapp', type: 'node' }],
    });
    // No directory matches 'other/' so no test entries are added
    expect(result).toBe('');
  });

  it('import-based: issues Python import grep with correct dotted module path', () => {
    const mockExecFileSync = vi.mocked(execFileSync);
    mockExecFileSync.mockReturnValue(Buffer.from(''));
    discoverRelatedTests(null, ['backend/app/orchestrator.py'], undefined);
    const calls = mockExecFileSync.mock.calls;
    const importCall = calls.find(c =>
      Array.isArray(c[1]) && c[1].some((a: unknown) => typeof a === 'string' && a.includes('app.orchestrator'))
    );
    expect(importCall).toBeTruthy();
  });

  it('import-based: constructs correct Python module path from nested file', () => {
    const mockExecFileSync = vi.mocked(execFileSync);
    mockExecFileSync.mockReturnValue(Buffer.from(''));
    discoverRelatedTests(null, ['backend/app/services/auth.py'], undefined);
    const calls = mockExecFileSync.mock.calls;
    const importCall = calls.find(c =>
      Array.isArray(c[1]) && c[1].some((a: unknown) => typeof a === 'string' && a.includes('app.services.auth'))
    );
    expect(importCall).toBeTruthy();
  });

  it('import-based: issues TypeScript import grep with stem-based pattern', () => {
    const mockExecFileSync = vi.mocked(execFileSync);
    mockExecFileSync.mockReturnValue(Buffer.from(''));
    discoverRelatedTests(null, ['agent/src/prompt.ts'], undefined);
    const calls = mockExecFileSync.mock.calls;
    // The import-based grep pattern should reference the stem 'prompt'
    const importCall = calls.find(c =>
      Array.isArray(c[1]) && c[1].some((a: unknown) => typeof a === 'string' && a.includes('/prompt'))
    );
    expect(importCall).toBeTruthy();
  });

  it('import-based: skips TypeScript test files themselves (contains .test.)', () => {
    const mockExecFileSync = vi.mocked(execFileSync);
    mockExecFileSync.mockReturnValue(Buffer.from(''));
    discoverRelatedTests(null, ['agent/src/prompt.test.ts'], undefined);
    const calls = mockExecFileSync.mock.calls;
    // The import grep uses a pattern with "from ['" — should not appear for test files
    const importCall = calls.find(c =>
      Array.isArray(c[1]) && c[1].some((a: unknown) => typeof a === 'string' && a.includes("from ['"))
    );
    expect(importCall).toBeUndefined();
  });
});

// ── extractTestConventions ────────────────────────────────────────────────────
describe('extractTestConventions', () => {
  let mockFs: { existsSync: ReturnType<typeof vi.fn>; readFileSync: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    vi.clearAllMocks();
    vi.resetModules();
    const fs = await import('fs');
    mockFs = fs.default as unknown as typeof mockFs;
    // Clear the module-level cache between tests by using unique dirKeys
  });

  it('returns empty string when no test files exist', () => {
    mockFs.existsSync.mockReturnValue(false);
    const result = extractTestConventions('backend-empty', ['backend/testing/test_foo.py']);
    expect(result).toBe('');
  });

  it('extracts pytest fixture names from python test files', () => {
    mockFs.existsSync.mockReturnValue(true);
    mockFs.readFileSync.mockReturnValue(
      '@pytest_asyncio.fixture\nasync def db_session():\n    pass\n@pytest.fixture\ndef mock_client():\n    pass\n'
    );
    const result = extractTestConventions('backend-fixtures', ['backend/testing/test_services.py']);
    expect(result).toContain('db_session');
    expect(result).toContain('mock_client');
    expect(result).toContain('Fixtures:');
  });

  it('extracts AsyncMock usage from python test files', () => {
    mockFs.existsSync.mockReturnValue(true);
    mockFs.readFileSync.mockReturnValue(
      'from unittest.mock import AsyncMock, patch\n\nAsyncMock()\nMagicMock()\n'
    );
    const result = extractTestConventions('backend-mocks', ['backend/testing/test_agent.py']);
    expect(result).toContain('AsyncMock');
    expect(result).toContain('Mocks:');
  });

  it('extracts vitest patterns from TypeScript test files', () => {
    mockFs.existsSync.mockReturnValue(true);
    mockFs.readFileSync.mockReturnValue(
      'import { vi, describe } from "vitest";\nbeforeEach(() => {});\nvi.fn();\nexpect(x).toBe(1);\n'
    );
    const result = extractTestConventions('agent-vitest', ['agent/src/prompt.test.ts']);
    expect(result).toContain('vitest');
    expect(result).toContain('beforeEach');
  });

  it('returns string under 500 chars', () => {
    mockFs.existsSync.mockReturnValue(true);
    mockFs.readFileSync.mockReturnValue(
      '@pytest.fixture\ndef fixture_one(): pass\n@pytest.fixture\ndef fixture_two(): pass\n' +
      'from unittest.mock import AsyncMock\nAsyncMock()\nbeforeEach(() => {})\nassert x == 1\npytest.raises(ValueError)\n'
    );
    const result = extractTestConventions('backend-size', ['backend/testing/test_x.py']);
    expect(result.length).toBeLessThanOrEqual(500);
  });

  it('returns cached result on second call with same dirKey', () => {
    mockFs.existsSync.mockReturnValue(true);
    mockFs.readFileSync.mockReturnValue('@pytest.fixture\ndef session(): pass\n');
    const first = extractTestConventions('backend-cached', ['backend/testing/test_a.py']);
    // Reset mock to return different content — cached result should be returned
    mockFs.readFileSync.mockReturnValue('');
    const second = extractTestConventions('backend-cached', ['backend/testing/test_b.py']);
    expect(first).toBe(second);
  });
});

// ── buildRetryContext ─────────────────────────────────────────────────────────
describe('buildRetryContext', () => {
  it('returns empty string for first-attempt tasks (no _attempt_number)', () => {
    expect(buildRetryContext({})).toBe('');
  });

  it('returns empty string when _attempt_number is 1', () => {
    expect(buildRetryContext({ context: { _attempt_number: 1 } })).toBe('');
  });

  it('renders failure type and error for a basic retry', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'Test suite failed: assertion error on line 42',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('## Previous Attempt (attempt #1 failed)');
    expect(result).toContain('Failure type: CODE_ERROR');
    expect(result).toContain('Test suite failed: assertion error on line 42');
  });

  it('renders PR URL and changed files when present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'lint error',
        _previous_pr_url: 'https://github.com/org/repo/pull/99',
        _previous_changed_files: ['backend/app/models.py', 'backend/testing/test_models.py'],
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('PR from previous attempt: https://github.com/org/repo/pull/99');
    expect(result).toContain('Files changed in previous attempt:');
    expect(result).toContain('  - backend/app/models.py');
    expect(result).toContain('  - backend/testing/test_models.py');
  });

  it('uses file-specific guidance when changed files are provided', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_changed_files: ['src/foo.ts'],
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Review the files listed above');
    expect(result).not.toContain('run the linter before committing');
  });

  it('uses generic guidance when no changed files are provided', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'AGENT_TIMEOUT',
        _previous_error: 'timed out',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Analyze the error above and avoid the same mistake');
    expect(result).not.toContain('Review the files listed above');
  });

  it('omits PR URL section when _previous_pr_url is absent', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_changed_files: ['src/foo.ts'],
      },
    };
    const result = buildRetryContext(task);
    expect(result).not.toContain('PR from previous attempt');
  });

  it('ends with a blank line', () => {
    const task = { context: { _attempt_number: 2, _previous_failure_class: 'CODE_ERROR', _previous_error: 'err' } };
    const result = buildRetryContext(task);
    expect(result.endsWith('\n')).toBe(true);
  });

  it('renders failure stage when _previous_failure_stage is present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_failure_stage: 'test',
        _previous_error: 'pytest: FAILED',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Failure type: CODE_ERROR (at test stage)');
    expect(result).not.toContain('Failure type: CODE_ERROR\n');
  });

  it('omits stage qualifier when _previous_failure_stage is absent', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'AGENT_TIMEOUT',
        _previous_error: 'timed out',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Failure type: AGENT_TIMEOUT\n');
    expect(result).not.toContain('at stage)');
  });

  it('renders recurring warning when _previous_is_recurring is true', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'test failed',
        _previous_is_recurring: true,
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('recurring failure pattern');
    expect(result).toContain('fundamentally different approach');
  });

  it('does not render recurring warning when _previous_is_recurring is false', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'test failed',
        _previous_is_recurring: false,
      },
    };
    const result = buildRetryContext(task);
    expect(result).not.toContain('recurring failure pattern');
  });

  it('renders error summary with "Error summary:" label when _previous_error_summary is present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'full long error message here',
        _previous_error_summary: 'AssertionError: expected 1 got 2',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Error summary: AssertionError: expected 1 got 2');
    expect(result).not.toContain('Error: full long error message here');
  });

  it('falls back to "Error:" label when only _previous_error is present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'AGENT_TIMEOUT',
        _previous_error: 'timed out after 600s',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Error: timed out after 600s');
    expect(result).not.toContain('Error summary:');
  });

  it('renders enriched diff summary with line counts when _previous_diff_summary is present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'test failed',
        _previous_diff_summary: [
          { file: 'backend/app/models.py', additions: 45, deletions: 12, change_type: 'modified' },
          { file: 'backend/testing/test_models.py', additions: 30, deletions: 0, change_type: 'added' },
        ],
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('backend/app/models.py (modified, +45/-12 lines)');
    expect(result).toContain('backend/testing/test_models.py (added, +30/-0 lines)');
    expect(result).toContain('Files changed in previous attempt:');
  });

  it('uses diff summary over plain file list when both are present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_changed_files: ['src/foo.ts'],
        _previous_diff_summary: [
          { file: 'src/foo.ts', additions: 10, deletions: 5, change_type: 'modified' },
        ],
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('src/foo.ts (modified, +10/-5 lines)');
    // Should not show the plain format alongside the enriched format
    expect(result).not.toMatch(/ {2}- src\/foo\.ts\n/);
  });

  it('renders test failure summary when _previous_test_failure_summary is present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'test_models.py::test_create FAILED',
        _previous_changed_files: ['src/foo.ts'],
        _previous_test_failure_summary: 'FAILED test_models.py::test_create - AssertionError: expected 1 got 2',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Test failures from previous attempt:');
    expect(result).toContain('FAILED test_models.py::test_create');
  });

  it('uses focused test-failure guidance when test failures and changed files are both present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_changed_files: ['src/foo.ts'],
        _previous_test_failure_summary: 'FAILED test_foo.py::test_bar',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Focus your retry on fixing the failing tests');
    expect(result).not.toContain('Review the files listed above');
  });

  it('renders self-review outcome when _previous_self_review_outcome is present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_self_review_outcome: { issues_found: true, fixes_applied: true, categories: [] },
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Self-review outcome:');
    expect(result).toContain('found issues and applied fixes');
  });

  it('renders self-review outcome with no-issues message when fixes_applied is false', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_self_review_outcome: { issues_found: false, fixes_applied: false, categories: [] },
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Self-review ran and found no critical issues');
  });

  it('renders SR-missed categories when SR incorrectly marked them as passed', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_self_review_outcome: {
          verdict: 'pass',
          fixes_applied: false,
          check_results: {
            correctness: { passed: true },
            'error-handling': { passed: true },
          },
        },
        _first_attempt_rejection_categories: {
          correctness: 2,
          'error-handling': 1,
        },
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('missed these categories');
    expect(result).toContain('correctness (reviewer found 2 issues)');
    expect(result).toContain('error-handling (reviewer found 1 issue)');
    expect(result).toContain('scrutinize these categories carefully');
  });

  it('detects SR-missed categories even when category was absent from SR check_results', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_self_review_outcome: {
          verdict: 'pass',
          fixes_applied: false,
          check_results: {
            correctness: { passed: true },
          },
        },
        _first_attempt_rejection_categories: {
          correctness: 2,
          'test-coverage': 1,
        },
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('missed these categories');
    expect(result).toContain('correctness (reviewer found 2 issues)');
    expect(result).toContain('test-coverage (reviewer found 1 issue)');
  });

  it('treats reviewer-origin failures as SR-missed even when check_results marked them failed', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_self_review_outcome: {
          verdict: 'fail',
          fixes_applied: false,
          check_results: {
            correctness: { passed: false, origin: 'reviewer' },
          },
        },
        _first_attempt_rejection_categories: {
          correctness: 2,
        },
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('missed these categories');
    expect(result).toContain('correctness (reviewer found 2 issues)');
  });

  it('uses existing wording when SR correctly identified all reviewer categories', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_self_review_outcome: {
          verdict: 'pass',
          fixes_applied: true,
          check_results: {
            correctness: { passed: false },
            'error-handling': { passed: false },
          },
        },
        _first_attempt_rejection_categories: {
          correctness: 2,
          'error-handling': 1,
        },
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Self-review outcome:');
    expect(result).toContain('found issues and applied fixes');
    expect(result).not.toContain('missed these categories');
  });

  it('uses existing wording when soft-fallback envelope (verdict=unknown, empty check_results)', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_self_review_outcome: {
          verdict: 'unknown',
          fixes_applied: false,
          check_results: {},
        },
        _first_attempt_rejection_categories: {
          correctness: 2,
          'error-handling': 1,
        },
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Self-review outcome:');
    expect(result).not.toContain('missed these categories');
  });

  it('renders previous analysis plan when _previous_analysis_plan is present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'test failed',
        _previous_analysis_plan: '## Pre-Implementation Analysis\n\nModule A depends on Module B.\nChange order: B first, then A.',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Pre-implementation analysis from previous attempt');
    expect(result).toContain('Module A depends on Module B');
    expect(result).toContain('skip re-analysis');
  });

  it('omits analysis plan section when _previous_analysis_plan is absent', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'test failed',
      },
    };
    const result = buildRetryContext(task);
    expect(result).not.toContain('Pre-implementation analysis from previous attempt');
  });

  it('omits retry budget when _max_attempts is absent', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'err',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('## Previous Attempt (attempt #1 failed)');
    expect(result).not.toContain('Retry budget:');
    expect(result).not.toContain('Final attempt');
  });

  it('shows retry budget with attempts remaining when multiple retries remain', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _max_attempts: 4,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'err',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('## Previous Attempt (attempt #1 of 4 failed)');
    expect(result).toContain('Retry budget: attempt 2 of 4 (2 retries remaining after this one)');
    expect(result).not.toContain('Final attempt');
    expect(result).not.toContain('Be conservative');
  });

  it('warns to be conservative when exactly one retry remains after this one', () => {
    const task = {
      context: {
        _attempt_number: 3,
        _max_attempts: 4,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'err',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('attempt 3 of 4');
    expect(result).toContain('1 retry remaining after this one');
    expect(result).toContain('Be conservative');
  });

  it('flags the final attempt and urges a conservative strategy when no retries remain', () => {
    const task = {
      context: {
        _attempt_number: 4,
        _max_attempts: 4,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'err',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('## Previous Attempt (attempt #3 of 4 failed)');
    expect(result).toContain('Final attempt (4 of 4)');
    expect(result).toContain('no retries remain after this one');
    expect(result).toContain('conservative strategy');
    expect(result).toContain('smallest viable change');
  });

  it('ignores inconsistent _max_attempts less than _attempt_number', () => {
    const task = {
      context: {
        _attempt_number: 5,
        _max_attempts: 4,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'err',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('## Previous Attempt (attempt #4 failed)');
    expect(result).not.toContain('Retry budget:');
    expect(result).not.toContain('Final attempt');
  });
});

// ── formatProvenanceWarnings ──────────────────────────────────────────────────
describe('formatProvenanceWarnings', () => {
  it('returns empty string when context is undefined', () => {
    expect(formatProvenanceWarnings(undefined)).toBe('');
  });

  it('returns empty string when _provenance_warnings is absent', () => {
    expect(formatProvenanceWarnings({ scope: 'medium' })).toBe('');
  });

  it('returns empty string when _provenance_warnings is an empty object', () => {
    expect(formatProvenanceWarnings({ _provenance_warnings: {} })).toBe('');
  });

  it('returns empty string when _provenance_warnings is not a dict (defensive)', () => {
    expect(formatProvenanceWarnings({ _provenance_warnings: 'oops' })).toBe('');
    expect(formatProvenanceWarnings({ _provenance_warnings: ['a'] })).toBe('');
    expect(formatProvenanceWarnings({ _provenance_warnings: null })).toBe('');
  });

  it('renders the cited-files-changed signal', () => {
    const result = formatProvenanceWarnings({
      _provenance_warnings: { cited_files_changed_since_generation: true },
    });
    expect(result).toContain('## Stale-premise notice');
    expect(result).toContain('Cited files changed since the finding was generated');
    expect(result).toContain('REPRODUCE-FIRST');
  });

  it('does not render the cited-files signal when the flag is false', () => {
    expect(
      formatProvenanceWarnings({
        _provenance_warnings: { cited_files_changed_since_generation: false },
      }),
    ).toBe('');
  });

  it('renders the broken-citation count and the offending refs', () => {
    const result = formatProvenanceWarnings({
      _provenance_warnings: {
        broken_source_ref_count: 2,
        broken_refs: ['app/foo.py:42', 'app/bar.ts'],
      },
    });
    expect(result).toContain('2 broken/missing citation(s)');
    expect(result).toContain('app/foo.py:42');
    expect(result).toContain('app/bar.ts');
  });

  it('renders the broken-citation signal from refs alone when count is missing', () => {
    const result = formatProvenanceWarnings({
      _provenance_warnings: { broken_refs: ['app/only.py'] },
    });
    expect(result).toContain('1 broken/missing citation(s)');
    expect(result).toContain('app/only.py');
  });

  it('renders the ungrounded-claim count and the offending refs', () => {
    const result = formatProvenanceWarnings({
      _provenance_warnings: {
        ungrounded_claim_count: 3,
        ungrounded_refs: ['claim about retries', 'claim about caching'],
      },
    });
    expect(result).toContain('3 ungrounded claim(s)');
    expect(result).toContain('claim about retries');
    expect(result).toContain('claim about caching');
  });

  it('renders all three signals combined and the no-op/pivot instruction', () => {
    const result = formatProvenanceWarnings({
      _provenance_warnings: {
        cited_files_changed_since_generation: true,
        broken_source_ref_count: 1,
        broken_refs: ['x.py'],
        ungrounded_claim_count: 1,
        ungrounded_refs: ['some claim'],
      },
    });
    expect(result).toContain('Cited files changed since the finding was generated');
    expect(result).toContain('1 broken/missing citation(s)');
    expect(result).toContain('1 ungrounded claim(s)');
    expect(result).toContain('./done --already-done');
    expect(result).toContain('do NOT fabricate');
  });

  it('does not throw on malformed nested fields and renders nothing useful', () => {
    expect(() =>
      formatProvenanceWarnings({
        _provenance_warnings: {
          broken_source_ref_count: 'two',
          broken_refs: 'notalist',
          ungrounded_refs: [123, null, 'real ref'],
        },
      }),
    ).not.toThrow();
    // ungrounded_refs has one stringifiable real ref, so the signal fires for it.
    const result = formatProvenanceWarnings({
      _provenance_warnings: { ungrounded_refs: [123, null, 'real ref'] },
    });
    expect(result).toContain('real ref');
  });
});

// ── formatInvestigateCitationProvenance ───────────────────────────────────────
describe('formatInvestigateCitationProvenance', () => {
  it('returns empty string when context is undefined', () => {
    expect(formatInvestigateCitationProvenance(undefined)).toBe('');
  });

  it('returns empty string when _provenance_warnings is absent', () => {
    expect(formatInvestigateCitationProvenance({ scope: 'medium' })).toBe('');
  });

  it('returns empty string for a non-dict _provenance_warnings (defensive)', () => {
    expect(formatInvestigateCitationProvenance({ _provenance_warnings: 'oops' })).toBe('');
    expect(formatInvestigateCitationProvenance({ _provenance_warnings: ['a'] })).toBe('');
    expect(formatInvestigateCitationProvenance({ _provenance_warnings: null })).toBe('');
  });

  it('returns empty string for a non-absent verdict with no stale refs', () => {
    expect(
      formatInvestigateCitationProvenance({
        _provenance_warnings: { citation_state: 'present', broken_refs: [], ungrounded_refs: [] },
      }),
    ).toBe('');
  });

  it('renders the ABSENT discount note and the stale refs', () => {
    const result = formatInvestigateCitationProvenance({
      _provenance_warnings: {
        citation_state: 'absent',
        broken_refs: ['app/gone.py'],
        ungrounded_refs: ['app/x.py:vanished'],
      },
    });
    expect(result).toContain('## Citation-provenance notice');
    expect(result).toContain('ABSENT on the current');
    expect(result).toContain('discount its actionability');
    expect(result).toContain('app/gone.py');
    expect(result).toContain('app/x.py:vanished');
    // Absent framing must not claim the defect is still actionable.
    expect(result).not.toContain('NOT presumed resolved');
  });

  it('renders the ABSENT note even when no specific stale refs are present', () => {
    const result = formatInvestigateCitationProvenance({
      _provenance_warnings: { citation_state: 'absent', broken_refs: [], ungrounded_refs: [] },
    });
    expect(result).toContain('ABSENT on the current');
    expect(result).toContain('discount its actionability');
  });

  it('renders the PARTIAL note for a grounded report with stale refs', () => {
    const result = formatInvestigateCitationProvenance({
      _provenance_warnings: {
        citation_state: 'present',
        broken_refs: ['app/moved.py'],
        ungrounded_refs: ['app/y.py:renamed'],
      },
    });
    expect(result).toContain('## Citation-provenance notice');
    expect(result).toContain('PARTIALLY grounded');
    expect(result).toContain('NOT presumed resolved');
    expect(result).toContain('remains actionable');
    expect(result).toContain('app/moved.py');
    expect(result).toContain('app/y.py:renamed');
    // Partial framing must not use the stronger absent discount.
    expect(result).not.toContain('discount its actionability');
  });

  it('does not throw on malformed nested fields', () => {
    expect(() =>
      formatInvestigateCitationProvenance({
        _provenance_warnings: {
          citation_state: 'present',
          broken_refs: 'notalist',
          ungrounded_refs: [123, null, 'real ref'],
        },
      }),
    ).not.toThrow();
    const result = formatInvestigateCitationProvenance({
      _provenance_warnings: { citation_state: 'present', ungrounded_refs: [123, null, 'real ref'] },
    });
    expect(result).toContain('real ref');
  });
});

// ── formatAcceptanceEvidence ──────────────────────────────────────────────────
describe('formatAcceptanceEvidence', () => {
  it('returns empty string when acceptance_evidence is absent or malformed', () => {
    expect(formatAcceptanceEvidence(undefined)).toBe('');
    expect(formatAcceptanceEvidence({})).toBe('');
    expect(formatAcceptanceEvidence({ acceptance_evidence: null })).toBe('');
    expect(formatAcceptanceEvidence({ acceptance_evidence: 'oops' })).toBe('');
    expect(formatAcceptanceEvidence({
      acceptance_evidence: { kind: 'named_test', test_path: 'backend/app/handler.py' },
    })).toBe('');
  });

  it('renders named_test obligations with path, test_id, and gate conditions', () => {
    const result = formatAcceptanceEvidence({
      acceptance_evidence: {
        kind: 'named_test',
        test_path: 'testing/test_widget.py',
        test_id: 'test_widget_handles_empty',
      },
    });

    expect(result).toContain('## Acceptance Evidence');
    expect(result).toContain('testing/test_widget.py');
    expect(result).toContain('test_widget_handles_empty');
    expect(result).toContain('real merge-test verdict');
    expect(result).toContain('production code');
    expect(result).toContain('test-only diff fails the gate');
  });

  it('renders observable_behavior and downstream_artifact obligations', () => {
    const observable = formatAcceptanceEvidence({
      acceptance_evidence: {
        kind: 'observable_behavior',
        description: 'the dashboard shows the live serving SHA banner',
      },
    });
    expect(observable).toContain('the dashboard shows the live serving SHA banner');
    expect(observable).toContain('real, non-empty diff');

    const artifact = formatAcceptanceEvidence({
      acceptance_evidence: {
        kind: 'downstream_artifact',
        artifact_id: 'release-deploy-artifact-registry',
      },
    });
    expect(artifact).toContain('release-deploy-artifact-registry');
    expect(artifact).toContain('real, non-empty diff');
  });
});

// ── extractModuleInterfaces ───────────────────────────────────────────────────
describe('extractModuleInterfaces', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Re-establish defaults so overrides from prior tests don't bleed through
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue('');
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
  });

  it('returns empty string when relatedFiles is empty', () => {
    expect(extractModuleInterfaces(null, [])).toBe('');
  });

  it('returns empty string when related files do not exist on disk', () => {
    // existsSync returns false by default
    expect(extractModuleInterfaces(null, ['backend/app/models.py'])).toBe('');
  });

  it('skips non-source files (.json, .md)', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    expect(extractModuleInterfaces(null, ['package.json', 'README.md'])).toBe('');
  });

  it('skips test files (.test.ts, test_.py)', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'export function testHelper(): void {}\n'
    );
    expect(extractModuleInterfaces(null, ['agent/src/prompt.test.ts', 'backend/testing/test_models.py'])).toBe('');
  });

  it('extracts top-level Python function signatures and class names', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'def get_user(user_id: int):\n    pass\n\nclass UserModel:\n    pass\n\ndef create_task(name: str, owner: str):\n    pass\n'
    );
    const result = extractModuleInterfaces(null, ['backend/app/models.py']);
    expect(result).toContain('## Module Interfaces');
    expect(result).toContain('backend/app/models.py');
    expect(result).toContain('def get_user(user_id: int)');
    expect(result).toContain('class UserModel');
    expect(result).toContain('def create_task(name: str, owner: str)');
  });

  it('uses __all__ as authoritative export list for Python files', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      "__all__ = ['get_user', 'create_task']\n\ndef get_user(x):\n    pass\n\ndef _private_helper(x):\n    pass\n"
    );
    const result = extractModuleInterfaces(null, ['backend/app/services.py']);
    expect(result).toContain('get_user');
    expect(result).toContain('create_task');
    // __all__ used — private helper should not appear as a standalone entry
    expect(result).not.toContain('_private_helper');
  });

  it('extracts TypeScript export declarations (function, class, interface, type, const)', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'export function buildPrompt(opts: Opts): string { return ""; }\n' +
      'export async function runHandler(task: Task): Promise<void> {}\n' +
      'export class TaskRunner {}\n' +
      'export interface BuildOpts { foo: string; }\n' +
      'export type TaskType = "create" | "fix";\n' +
      'export const DEFAULT_MODEL = "claude-3";\n'
    );
    const result = extractModuleInterfaces(null, ['agent/src/prompt.ts']);
    expect(result).toContain('## Module Interfaces');
    expect(result).toContain('function buildPrompt');
    expect(result).toContain('function runHandler');
    expect(result).toContain('class TaskRunner');
    expect(result).toContain('interface BuildOpts');
    expect(result).toContain('type TaskType');
    expect(result).toContain('const DEFAULT_MODEL');
  });

  it('includes caller files from git grep when found', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'export function buildPrompt(): string { return ""; }\n'
    );
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/handlers/standard.ts\nagent/src/index.ts\n'
    );
    const result = extractModuleInterfaces(null, ['agent/src/prompt.ts']);
    expect(result).toContain('Imported by:');
    expect(result).toContain('agent/src/handlers/standard.ts');
    expect(result).toContain('agent/src/index.ts');
  });

  it('omits Imported by section when git grep finds no callers', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'export function buildPrompt(): string { return ""; }\n'
    );
    // Default mock returns Buffer.from('') — .split() throws, caught by try/catch → no callers
    const result = extractModuleInterfaces(null, ['agent/src/prompt.ts']);
    expect(result).toContain('## Module Interfaces');
    expect(result).not.toContain('Imported by:');
  });

  it('handles git grep throwing gracefully (no callers shown, exports still shown)', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'export function buildPrompt(): string { return ""; }\n'
    );
    vi.mocked(execFileSync).mockImplementation(() => { throw new Error('git grep failed'); });
    const result = extractModuleInterfaces(null, ['agent/src/prompt.ts']);
    expect(result).toContain('## Module Interfaces');
    expect(result).toContain('function buildPrompt');
    expect(result).not.toContain('Imported by:');
  });

  it('caps output at approximately 2KB', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    const manyExports = Array.from({ length: 80 }, (_, i) =>
      `export function veryLongFunctionName${i}(paramA: string, paramB: number): void {}`
    ).join('\n');
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(manyExports);
    const manyFiles = Array.from({ length: 15 }, (_, i) => `agent/src/module${i}.ts`);
    const result = extractModuleInterfaces(null, manyFiles);
    expect(result.length).toBeLessThanOrEqual(2000);
  });

  it('returns empty string when files have no exportable symbols', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      '// just a comment\nconst x = 1; // non-exported\n'
    );
    expect(extractModuleInterfaces(null, ['agent/src/utils.ts'])).toBe('');
  });

  it('appends Suggested Change Order when one related file imports from another', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'def get_user(user_id: int):\n    pass\n'
    );
    // models callers include services (in related set); services has no related callers
    vi.mocked(execFileSync)
      .mockReturnValueOnce('backend/app/services.py\n')
      .mockReturnValueOnce('');
    const result = extractModuleInterfaces(null, ['backend/app/models.py', 'backend/app/services.py']);
    expect(result).toContain('## Suggested Change Order');
    expect(result).toContain('1. models.py');
    expect(result).toContain('2. services.py');
    expect(result).toContain('imports from models.py');
  });

  it('omits Suggested Change Order when no related files import each other', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'def get_user(user_id: int):\n    pass\n'
    );
    // Both files' callers are outside the related set
    vi.mocked(execFileSync)
      .mockReturnValueOnce('other/caller.py\n')
      .mockReturnValueOnce('');
    const result = extractModuleInterfaces(null, ['backend/app/models.py', 'backend/app/services.py']);
    expect(result).not.toContain('## Suggested Change Order');
  });

  it('appends Suggested Change Order for three-file dependency chain', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'def fn(x: int):\n    pass\n'
    );
    // models callers: [services, admin]; services callers: [admin]; admin callers: []
    vi.mocked(execFileSync)
      .mockReturnValueOnce('backend/app/services.py\nbackend/app/admin.py\n')
      .mockReturnValueOnce('backend/app/admin.py\n')
      .mockReturnValueOnce('');
    const result = extractModuleInterfaces(null, [
      'backend/app/models.py',
      'backend/app/services.py',
      'backend/app/admin.py',
    ]);
    expect(result).toContain('## Suggested Change Order');
    expect(result).toContain('1. models.py');
    expect(result).toContain('2. services.py');
    expect(result).toContain('3. admin.py');
  });
});

// ── discoverAffectedConsumers ─────────────────────────────────────────────────
describe('discoverAffectedConsumers', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue('');
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
  });

  it('returns empty string when relatedFiles is empty', () => {
    expect(discoverAffectedConsumers(null, [])).toBe('');
  });

  it('skips test files in relatedFiles', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/handlers/standard.ts\n'
    );
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.test.ts', 'backend/testing/test_models.py']);
    expect(result).toBe('');
  });

  it('skips non-source files (.json, .md)', () => {
    const result = discoverAffectedConsumers(null, ['package.json', 'README.md']);
    expect(result).toBe('');
  });

  it('returns empty string when git grep finds no consumers', () => {
    // execFileSync throws (exit 1) when git grep finds no matches
    vi.mocked(execFileSync).mockImplementation(() => { throw new Error('no matches'); });
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toBe('');
  });

  it('returns empty string when consumer files do not exist on disk', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/handlers/standard.ts\n'
    );
    // existsSync returns false by default — consumer file cannot be read
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toBe('');
  });

  it('filters out test files returned by git grep', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/prompt.test.ts\nbackend/testing/test_models.py\n'
    );
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toBe('');
  });

  it('extracts named TypeScript imports from consumer files', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/handlers/standard.ts\n'
    );
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'import { buildContextPrefix, discoverRelatedTests, extractModuleInterfaces } from \'../prompt.js\';\n'
    );
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toContain('## Affected Consumers');
    expect(result).toContain('agent/src/prompt.ts');
    expect(result).toContain('agent/src/handlers/standard.ts');
    expect(result).toContain('buildContextPrefix');
    expect(result).toContain('discoverRelatedTests');
    expect(result).toContain('extractModuleInterfaces');
  });

  it('appends call-site usage counts to imported symbols', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/handlers/standard.ts\n'
    );
    vi.mocked(fs.existsSync).mockReturnValue(true);
    // Build a file: import line at line 0, then 79 padding lines so body starts at line 80
    const importLine = 'import { foo, bar } from \'../prompt.js\';';
    const padding = Array(79).fill('').join('\n');
    const body = [
      'function example() {',
      '  foo();',
      '  foo(arg);',
      '  return foo();',
      '  const x = bar;',
      '}',
    ].join('\n');
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      `${importLine}\n${padding}\n${body}\n`
    );
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toContain('foo used 3x');
    expect(result).toContain('bar used 1x');
  });

  it('extracts Python imports from consumer files', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'backend/app/orchestrator.py\n'
    );
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'from app.services import get_task, create_task\n'
    );
    const result = discoverAffectedConsumers(null, ['backend/app/services.py']);
    expect(result).toContain('## Affected Consumers');
    expect(result).toContain('backend/app/services.py');
    expect(result).toContain('backend/app/orchestrator.py');
    expect(result).toContain('get_task');
    expect(result).toContain('create_task');
  });

  it('shows consumer without imports when no import line matches', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'backend/app/main.py\n'
    );
    vi.mocked(fs.existsSync).mockReturnValue(true);
    // File mentions module name but no parseable import line
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      '# uses services module indirectly\nsome_var = services_helper()\n'
    );
    const result = discoverAffectedConsumers(null, ['backend/app/services.py']);
    expect(result).toContain('backend/app/main.py');
    // No imports listed — just the consumer path
    expect(result).not.toContain('imports:');
  });

  it('includes the Affected Consumers header and description', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/index.ts\n'
    );
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'import { buildFileTree } from \'./prompt.js\';\n'
    );
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toContain('## Affected Consumers');
    expect(result).toContain('Verify they remain compatible');
  });

  it('caps output at approximately 2KB', () => {
    const manyConsumers = Array.from({ length: 20 }, (_, i) =>
      `agent/src/consumers/module${i}.ts`
    ).join('\n') + '\n';
    vi.mocked(execFileSync).mockReturnValue(
      manyConsumers
    );
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'import { funcA, funcB, funcC, funcD, funcE, funcF, funcG, funcH } from \'../prompt.js\';\n'
    );
    const manyFiles = Array.from({ length: 10 }, (_, i) => `agent/src/module${i}.ts`);
    const result = discoverAffectedConsumers(null, manyFiles);
    expect(result.length).toBeLessThanOrEqual(2000);
  });

  it('handles git grep throwing gracefully and returns empty string', () => {
    vi.mocked(execFileSync).mockImplementation(() => { throw new Error('git grep failed'); });
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toBe('');
  });
});

// ── buildAnalysisPrompt ──────────────────────────────────────────────────────
describe('buildAnalysisPrompt', () => {
  it('includes the test-planning sections', () => {
    const result = buildAnalysisPrompt({
      rawPrompt: 'Do thing',
      taskTitle: 'Title',
      relatedFiles: ['a.ts'],
      fileTree: '',
    });
    expect(result).toContain('**Tests to add or update**');
    expect(result).toContain('**Tests at risk of breaking**');
  });

  it('includes the callers-to-verify section', () => {
    const result = buildAnalysisPrompt({
      rawPrompt: 'Do thing',
      taskTitle: 'Title',
      relatedFiles: ['a.ts'],
      fileTree: '',
    });
    expect(result).toContain('**Callers to verify after implementation**');
  });

  it('includes the files-to-verify (no changes expected) section', () => {
    const result = buildAnalysisPrompt({
      rawPrompt: 'Do thing',
      taskTitle: 'Title',
      relatedFiles: ['a.ts'],
      fileTree: '',
    });
    expect(result).toContain('**Files to verify (no changes expected)**');
    // The verify section must sit between Files to change and Callers to verify.
    const idxChange = result.indexOf('**Files to change**');
    const idxVerify = result.indexOf('**Files to verify (no changes expected)**');
    const idxCallers = result.indexOf('**Callers to verify after implementation**');
    expect(idxChange).toBeGreaterThan(-1);
    expect(idxVerify).toBeGreaterThan(idxChange);
    expect(idxCallers).toBeGreaterThan(idxVerify);
  });

  it('mentions the file limit when fileLimit is provided', () => {
    const result = buildAnalysisPrompt({
      rawPrompt: 'Do thing',
      taskTitle: 'Title',
      relatedFiles: [],
      fileTree: '',
      fileLimit: 7,
    });
    expect(result).toContain('7 or fewer files');
  });

  it('omits the file-limit clause when fileLimit is not provided', () => {
    const result = buildAnalysisPrompt({
      rawPrompt: 'Do thing',
      taskTitle: 'Title',
      relatedFiles: [],
      fileTree: '',
    });
    expect(result).not.toContain('or fewer files');
  });

  it('embeds the narrowing directive when one is provided', () => {
    const result = buildAnalysisPrompt({
      rawPrompt: 'Do thing',
      taskTitle: 'Title',
      relatedFiles: [],
      fileTree: '',
      narrowingGuidance: 'Cut the plan to fewer files.',
    });
    expect(result).toContain('Narrowing Directive');
    expect(result).toContain('Cut the plan to fewer files.');
  });

  it('includes the total file budget clause when totalFileBudget is provided', () => {
    const result = buildAnalysisPrompt({
      rawPrompt: 'Do thing',
      taskTitle: 'Title',
      relatedFiles: [],
      fileTree: '',
      totalFileBudget: 10,
    });
    expect(result).toContain('Total file budget');
    expect(result).toContain('10');
  });

  it('includes direction-concurrent file targets section when provided', () => {
    const result = buildAnalysisPrompt({
      rawPrompt: 'Do thing',
      taskTitle: 'Title',
      relatedFiles: [],
      fileTree: '',
      directionConcurrentFiles: ['a/b.ts', 'c/d.py'],
    });
    expect(result).toContain('Files currently being modified by sibling tasks in this direction');
    expect(result).toContain('- a/b.ts');
    expect(result).toContain('- c/d.py');
    expect(result.indexOf('sibling tasks in this direction')).toBeLessThan(result.indexOf('## Instructions'));
  });

  it('includes cochange-files section when provided', () => {
    const result = buildAnalysisPrompt({
      rawPrompt: 'Do thing',
      taskTitle: 'Title',
      relatedFiles: [],
      fileTree: '',
      cochangeFiles: ['foo/bar.ts', 'baz/qux.py'],
    });
    expect(result).toContain('Historically co-changed files');
    expect(result).toContain('- foo/bar.ts');
    expect(result).toContain('- baz/qux.py');
    expect(result.indexOf('Historically co-changed files')).toBeLessThan(result.indexOf('## Instructions'));
  });

  it('omits cochange-files section when list is empty or absent', () => {
    const result = buildAnalysisPrompt({
      rawPrompt: 'Do thing',
      taskTitle: 'Title',
      relatedFiles: [],
      fileTree: '',
      cochangeFiles: [],
    });
    expect(result).not.toContain('Historically co-changed files');
  });

  it('omits direction-concurrent section when list is empty or absent', () => {
    const result = buildAnalysisPrompt({
      rawPrompt: 'Do thing',
      taskTitle: 'Title',
      relatedFiles: [],
      fileTree: '',
      directionConcurrentFiles: [],
    });
    expect(result).not.toContain('sibling tasks in this direction');
  });

  it('omits the total file budget clause when totalFileBudget is absent', () => {
    const result = buildAnalysisPrompt({
      rawPrompt: 'Do thing',
      taskTitle: 'Title',
      relatedFiles: [],
      fileTree: '',
    });
    expect(result).not.toContain('Total file budget');
  });

  it('threads diagnosis context into the analysis prompt when present', () => {
    const result = buildAnalysisPrompt({
      rawPrompt: 'Fix the thing',
      taskTitle: 'Title',
      relatedFiles: [],
      fileTree: '',
      diagnosisContext: {
        rootCause: 'Race condition in record_direction_result',
        recommendedFix: 'Acquire lock before mutating dict',
        priorFailedApproaches: ['Added retry loop — still races under load'],
      },
    });
    expect(result).toContain('Diagnosis Context');
    expect(result).toContain('diagnosed remediation');
    expect(result).toContain('Race condition in record_direction_result');
    expect(result).toContain('Acquire lock before mutating dict');
    expect(result).toContain('Prior failed remediation approaches');
    expect(result).toContain('Added retry loop');
  });

  it('omits the diagnosis context block when no diagnosis is provided', () => {
    const result = buildAnalysisPrompt({
      rawPrompt: 'Build feature',
      taskTitle: 'Title',
      relatedFiles: [],
      fileTree: '',
    });
    expect(result).not.toContain('Diagnosis Context');
    expect(result).not.toContain('diagnosed remediation');
  });
});

// ── parseAnalysisPlanFiles ────────────────────────────────────────────────────
describe('parseAnalysisPlanFiles', () => {
  it('returns [] for empty input', () => {
    expect(parseAnalysisPlanFiles('')).toEqual([]);
  });

  it('returns [] when the heading is missing', () => {
    expect(parseAnalysisPlanFiles('No heading here\nSome body')).toEqual([]);
  });

  it('extracts dash-bulleted file paths under the heading', () => {
    const out = parseAnalysisPlanFiles(
      '**Files to change**\n- agent/src/foo.ts — reason\n- agent/src/bar.ts (other reason)\n\n**Interfaces to preserve**\n- something else',
    );
    expect(out).toEqual(['agent/src/foo.ts', 'agent/src/bar.ts']);
  });

  it('extracts numbered file paths and strips backticks', () => {
    const out = parseAnalysisPlanFiles(
      '**Files to change**\n1. `agent/src/foo.ts` — first\n2. `agent/src/bar.ts` — second\n',
    );
    expect(out).toEqual(['agent/src/foo.ts', 'agent/src/bar.ts']);
  });

  it('stops at the next bold heading', () => {
    const out = parseAnalysisPlanFiles(
      '**Files to change**\n- a.ts\n- b.ts\n\n**Implementation approach**\n- not-a-file.ts',
    );
    expect(out).toEqual(['a.ts', 'b.ts']);
  });

  it('skips bullets without a path-like token', () => {
    const out = parseAnalysisPlanFiles(
      '**Files to change**\n- (none — analysis only)\n- agent/src/foo.ts\n',
    );
    expect(out).toEqual(['agent/src/foo.ts']);
  });

  it('deduplicates repeated paths', () => {
    const out = parseAnalysisPlanFiles(
      '**Files to change**\n- a.ts\n- a.ts\n- b.ts\n',
    );
    expect(out).toEqual(['a.ts', 'b.ts']);
  });

  it('handles asterisk and bullet-character markers', () => {
    const out = parseAnalysisPlanFiles(
      '**Files to change**\n* a.ts\n• b.ts\n',
    );
    expect(out).toEqual(['a.ts', 'b.ts']);
  });
});

// ── parseAnalysisPlanVerifyFiles ─────────────────────────────────────────────
describe('parseAnalysisPlanVerifyFiles', () => {
  it('returns [] for empty input', () => {
    expect(parseAnalysisPlanVerifyFiles('')).toEqual([]);
  });

  it('returns [] when the verify heading is missing', () => {
    expect(parseAnalysisPlanVerifyFiles('**Files to change**\n- a.ts\n')).toEqual([]);
  });

  it('extracts bullet items under "Files to verify (no changes expected)"', () => {
    const out = parseAnalysisPlanVerifyFiles(
      '**Files to verify (no changes expected)**\n- consumer/foo.ts — imports changed type\n- consumer/bar.ts (reads re-exported symbol)\n',
    );
    expect(out).toEqual(['consumer/foo.ts', 'consumer/bar.ts']);
  });

  it('extracts numbered-list items with backticks', () => {
    const out = parseAnalysisPlanVerifyFiles(
      '**Files to verify (no changes expected)**\n1. `pkg/a.ts` — first\n2. `pkg/b.ts` — second\n',
    );
    expect(out).toEqual(['pkg/a.ts', 'pkg/b.ts']);
  });

  it('stops at the next bold heading', () => {
    const out = parseAnalysisPlanVerifyFiles(
      '**Files to verify (no changes expected)**\n- a.ts\n- b.ts\n\n**Interfaces to preserve**\n- not-a-file.ts',
    );
    expect(out).toEqual(['a.ts', 'b.ts']);
  });

  it('deduplicates repeated entries', () => {
    const out = parseAnalysisPlanVerifyFiles(
      '**Files to verify (no changes expected)**\n- a.ts\n- a.ts\n- b.ts\n',
    );
    expect(out).toEqual(['a.ts', 'b.ts']);
  });

  it('does not bleed verify entries into parseAnalysisPlanFiles output', () => {
    const analysis =
      '**Files to change**\n- change/one.ts\n\n' +
      '**Files to verify (no changes expected)**\n- verify/one.ts\n- verify/two.ts\n\n' +
      '**Interfaces to preserve**\n- nope\n';
    expect(parseAnalysisPlanFiles(analysis)).toEqual(['change/one.ts']);
    expect(parseAnalysisPlanVerifyFiles(analysis)).toEqual(['verify/one.ts', 'verify/two.ts']);
  });
});

// ── parseAnalysisPlanTestFiles ───────────────────────────────────────────────
describe('parseAnalysisPlanTestFiles', () => {
  it('returns [] when neither test heading is present', () => {
    expect(parseAnalysisPlanTestFiles('**Files to change**\n- a.ts\n')).toEqual([]);
  });

  it('extracts files from "Tests to add or update"', () => {
    const out = parseAnalysisPlanTestFiles(
      '**Tests to add or update**\n- agent/src/foo.test.ts — adds X case\n- agent/src/bar.test.ts (covers Y)\n',
    );
    expect(out).toEqual(['agent/src/foo.test.ts', 'agent/src/bar.test.ts']);
  });

  it('extracts files from "Tests at risk of breaking"', () => {
    const out = parseAnalysisPlanTestFiles(
      '**Tests at risk of breaking**\n- testing/test_x.py — fixture change\n',
    );
    expect(out).toEqual(['testing/test_x.py']);
  });

  it('combines and deduplicates across both test headings', () => {
    const out = parseAnalysisPlanTestFiles(
      '**Tests to add or update**\n- a.test.ts\n- b.test.ts\n\n**Tests at risk of breaking**\n- b.test.ts\n- c.test.ts\n',
    );
    expect(out).toEqual(['a.test.ts', 'b.test.ts', 'c.test.ts']);
  });

  it('skips bullets without a path-like token (e.g. "none expected")', () => {
    const out = parseAnalysisPlanTestFiles(
      '**Tests to add or update**\n- none\n- a.test.ts\n',
    );
    expect(out).toEqual(['a.test.ts']);
  });
});

describe('formatSelfReviewBlindSpots', () => {
  it('returns empty string when self-review outcome is undefined', () => {
    expect(formatSelfReviewBlindSpots(undefined, ['a.ts'])).toBe('');
  });

  it('returns empty string when no blind-spot signal is present', () => {
    const outcome = { verdict: 'PASS', categories: [], truncated_files_read: [] };
    expect(formatSelfReviewBlindSpots(outcome, ['a.ts', 'b.ts'])).toBe('');
  });

  it('lists verified files when truncated_files_read is populated', () => {
    const outcome = { verdict: 'PASS', truncated_files_read: ['a.ts', 'b.ts'] };
    const out = formatSelfReviewBlindSpots(outcome, ['a.ts', 'b.ts', 'c.ts']);
    expect(out).toContain('Self-review verdict: PASS');
    expect(out).toContain('Files the self-review verified');
    expect(out).toContain('- a.ts');
    expect(out).toContain('- b.ts');
    expect(out).toContain('Files NOT verified by self-review');
    expect(out).toContain('- c.ts');
  });

  it('warns when incomplete_review category is present', () => {
    const outcome = {
      verdict: 'PASS',
      categories: ['truncation_not_verified', 'incomplete_review'],
      truncated_files_read: [],
    };
    const out = formatSelfReviewBlindSpots(outcome, ['a.ts', 'b.ts']);
    expect(out).toContain('INCOMPLETE');
    expect(out).toContain('- a.ts');
    expect(out).toContain('- b.ts');
    expect(out).not.toContain('Files the self-review verified');
  });

  it('omits unverified section when all changed files were verified', () => {
    const outcome = { truncated_files_read: ['a.ts', 'b.ts'] };
    const out = formatSelfReviewBlindSpots(outcome, ['a.ts', 'b.ts']);
    expect(out).toContain('Files the self-review verified');
    expect(out).not.toContain('Files NOT verified by self-review');
  });

  it('renders blind_spots verbatim under the Author-flagged uncertainty heading', () => {
    const outcome = {
      verdict: 'PASS',
      truncated_files_read: ['a.ts'],
      blind_spots: ['unsure about the retry path', 'verify the cache key shape'],
    };
    const out = formatSelfReviewBlindSpots(outcome, ['a.ts']);
    expect(out).toContain('**Author-flagged uncertainty (verify each):**');
    expect(out).toContain('- unsure about the retry path');
    expect(out).toContain('- verify the cache key shape');
  });

  it('caps blind_spots at 5 entries', () => {
    const outcome = {
      verdict: 'PASS',
      truncated_files_read: ['a.ts'],
      blind_spots: ['s1', 's2', 's3', 's4', 's5', 's6', 's7'],
    };
    const out = formatSelfReviewBlindSpots(outcome, ['a.ts']);
    expect(out).toContain('- s1');
    expect(out).toContain('- s5');
    expect(out).not.toContain('- s6');
    expect(out).not.toContain('- s7');
  });

  it('truncates individual blind_spot lines over 200 chars', () => {
    const longLine = 'x'.repeat(500);
    const outcome = {
      verdict: 'PASS',
      truncated_files_read: ['a.ts'],
      blind_spots: [longLine],
    };
    const out = formatSelfReviewBlindSpots(outcome, ['a.ts']);
    expect(out).toContain('x'.repeat(200) + '…');
    expect(out).not.toContain('x'.repeat(201));
  });

  it('omits the Author-flagged section when blind_spots is missing/empty/non-array', () => {
    const baseOutcome = { verdict: 'PASS', truncated_files_read: ['a.ts'] };
    expect(formatSelfReviewBlindSpots(baseOutcome, ['a.ts'])).not.toContain('Author-flagged uncertainty');
    expect(formatSelfReviewBlindSpots({ ...baseOutcome, blind_spots: [] }, ['a.ts'])).not.toContain('Author-flagged uncertainty');
    expect(formatSelfReviewBlindSpots({ ...baseOutcome, blind_spots: 'not an array' }, ['a.ts'])).not.toContain('Author-flagged uncertainty');
    expect(formatSelfReviewBlindSpots({ ...baseOutcome, blind_spots: [123, null, ''] }, ['a.ts'])).not.toContain('Author-flagged uncertainty');
  });

  it('returns empty string when blind_spots and all other signals are absent', () => {
    const outcome = { verdict: 'PASS', categories: [], truncated_files_read: [], blind_spots: [] };
    expect(formatSelfReviewBlindSpots(outcome, ['a.ts'])).toBe('');
  });

  it('renders blind_spots when it is the only signal present', () => {
    const outcome = { verdict: 'PASS', blind_spots: ['only-uncertainty'] };
    const out = formatSelfReviewBlindSpots(outcome, ['a.ts']);
    expect(out).toContain('Author-flagged uncertainty');
    expect(out).toContain('- only-uncertainty');
  });

  it('coexists with truncated_files_read and reviewer_findings sections', () => {
    const outcome = {
      verdict: 'PASS',
      truncated_files_read: ['a.ts'],
      reviewer_findings: [{ category: 'correctness', severity: 'HIGH', description: 'oops' }],
      blind_spots: ['author-uncertainty'],
    };
    const out = formatSelfReviewBlindSpots(outcome, ['a.ts', 'b.ts']);
    expect(out).toContain('Files the self-review verified');
    expect(out).toContain('Files NOT verified by self-review');
    expect(out).toContain('Fresh-reviewer findings');
    expect(out).toContain('Author-flagged uncertainty');
    expect(out).toContain('- author-uncertainty');
  });

  it('e2e: schema-documented truncated_files_read flows into reviewer prompt with unverified files listed', () => {
    // Mirrors a self-review JSON an agent would now produce after the schema
    // documents truncated_files_read: verified two of three changed files.
    const outcome = {
      verdict: 'pass',
      fixes_applied: false,
      check_results: { correctness: { passed: true } },
      blind_spots: ['did not re-read c.ts after retry'],
      truncated_files_read: ['a.ts', 'b.ts'],
    };
    const out = formatSelfReviewBlindSpots(outcome, ['a.ts', 'b.ts', 'c.ts']);
    expect(out).toContain('Files NOT verified by self-review (prioritize careful review of these)');
    expect(out).toMatch(/Files NOT verified by self-review[^\n]*\n {2}- c\.ts/);
    expect(out).not.toMatch(/Files NOT verified[^\n]*\n( {2}- [ab]\.ts\n)* {2}- [ab]\.ts/);
  });

  it('renders origin labels on edge bullets when origin is present and stays backward-compatible when absent', () => {
    const outcome = {
      verdict: 'pass',
      edges_considered: [
        { case: 'no-origin handled', handled: true, evidence: 'L1' },
        { case: 'author-handled', handled: true, evidence: 'L2', origin: 'author' },
        { case: 'reviewer-handled', handled: true, evidence: 'L3', origin: 'reviewer' },
        { case: 'no-origin missed', handled: false, blind_spot: 'unknown' },
        { case: 'reviewer-missed', handled: false, blind_spot: 'spotted by reviewer', origin: 'reviewer' },
      ],
    };
    const out = formatSelfReviewBlindSpots(outcome, ['a.ts']);
    // Backward-compat: no suffix when origin absent
    expect(out).toContain('[handled] no-origin handled');
    expect(out).toContain('[UNHANDLED] no-origin missed');
    // With origin, suffix is rendered
    expect(out).toContain('[handled · author] author-handled');
    expect(out).toContain('[handled · reviewer] reviewer-handled');
    expect(out).toContain('[UNHANDLED · reviewer] reviewer-missed');
  });

  it('e2e: schema-documented categories: ["incomplete_review"] surfaces INCOMPLETE warning to reviewer', () => {
    const outcome = {
      verdict: 'pass',
      fixes_applied: false,
      check_results: { correctness: { passed: true } },
      blind_spots: [],
      truncated_files_read: ['a.ts', 'b.ts'],
      categories: ['incomplete_review'],
    };
    const out = formatSelfReviewBlindSpots(outcome, ['a.ts', 'b.ts', 'c.ts']);
    expect(out).toContain('INCOMPLETE');
    expect(out).toContain('Files NOT verified by self-review (prioritize careful review of these)');
    expect(out).toContain('- c.ts');
  });
});

describe('CODE_CHANGE_HELPER_GUIDANCE — truncated_files_read & incomplete_review schema', () => {
  it('documents truncated_files_read in the schema block', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('truncated_files_read');
  });

  it('documents the incomplete_review categories marker', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('incomplete_review');
  });

  it('explains truncated_files_read as a subset of changed files (verification-only)', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toMatch(/truncated_files_read[\s\S]*subset/);
  });
});


describe('CODE_CHANGE_HELPER_GUIDANCE — scope-drift category', () => {
  it('includes scope-drift in the canonical category list', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('`scope-drift`');
  });

  it('includes scope threshold table for small scope', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('small');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('50');
  });

  it('includes scope threshold table for medium scope', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('medium');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('200');
  });

  it('includes scope threshold table for large scope', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('large');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('800');
  });

  it('instructs agent to run git diff --stat', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('git diff --stat');
  });

  it('describes off-scope file modifications as a second trigger', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('related_files');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('Trigger B');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('git diff --name-only');
  });

  it('shows the off-scope issue format with file list', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('modified files outside declared scope');
  });

  it('mentions lockfile allowlist for incidental files', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('package-lock.json');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('poetry.lock');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('Cargo.lock');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('go.sum');
  });

  it('mentions sibling test files as allowlisted', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toMatch(/\.test\.|\.spec\./);
  });

  it('keeps scope-drift as a single canonical category for both triggers', () => {
    // No new category id introduced — both triggers feed scope-drift.
    expect(CODE_CHANGE_HELPER_GUIDANCE).not.toContain('off-scope-files');
    expect(CODE_CHANGE_HELPER_GUIDANCE).not.toContain('off_scope_files');
    // Both trigger labels appear in the same scope-drift section.
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('Trigger A');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('Trigger B');
  });

  it('per-task-type focus blocks reference the off-scope files concept', () => {
    expect(CREATE_TASK_FOCUS).toContain('related_files');
    expect(CREATE_TASK_FOCUS).toContain('git diff --name-only');
    expect(FIX_TASK_FOCUS).toContain('related_files');
    expect(FIX_TASK_FOCUS).toContain('git diff --name-only');
    expect(REFACTOR_TASK_FOCUS).toContain('related_files');
    expect(REFACTOR_TASK_FOCUS).toContain('git diff --name-only');
  });

  it('test_scope_drift_guidance_defined_once — canonical lockfile allowlist appears identically in all scope-drift-enforcing focus blocks', () => {
    // If this fails, scope-drift guidance was edited in one place but not the
    // others. Fix: update SHARED_SCOPE_DRIFT_BODY in task-type-focus.ts so all
    // blocks re-derive from the single constant.
    const CANONICAL_ALLOWLIST = '`package-lock.json`, `poetry.lock`, `Cargo.lock`, `go.sum`';
    expect(CREATE_TASK_FOCUS).toContain(CANONICAL_ALLOWLIST);
    expect(FIX_TASK_FOCUS).toContain(CANONICAL_ALLOWLIST);
    expect(REFACTOR_TASK_FOCUS).toContain(CANONICAL_ALLOWLIST);
  });
});

describe('CODE_CHANGE_HELPER_GUIDANCE — regression_risk category', () => {
  it('includes regression_risk in the canonical category list', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('`regression_risk`');
  });

  it('references the test_baseline file path', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('test_baseline_');
  });

  it('instructs agent to omit check when baseline file is absent', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('omit');
  });
});

describe('CODE_CHANGE_HELPER_GUIDANCE — cross-system-contract category', () => {
  it('includes cross-system-contract in the canonical category list', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('`cross-system-contract`');
  });

  it('fires when diff touches 2+ of {admin-ui, backend, agent}', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toMatch(/admin-ui.*backend.*agent|≥2|>=2/);
  });

  it('instructs agent to grep the other side for matching route definitions', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('grep');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('other');
  });

  it('specifies major severity for URL/route mismatch', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('major');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('minor');
  });

  it('instructs agent to record passed:true when diff is not cross-system', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('diff does not cross system boundaries');
  });
});

// ── TASK_TYPE_HINTS dispatch parity ───────────────────────────────────────
describe('TASK_TYPE_HINTS dispatch parity', () => {
  it('keeps release_audit_challenge opted out of generic hints', () => {
    expect(TASK_TYPES_WITHOUT_HINT.has('release_audit_challenge')).toBe(true);
    expect(TASK_TYPE_HINTS).not.toHaveProperty('release_audit_challenge');
  });

  it('ensures every handler dispatch key is either in TASK_TYPE_HINTS or in TASK_TYPES_WITHOUT_HINT', () => {
    const dispatchKeys = Object.keys(handlers).sort();
    const hintsKeys = Object.keys(TASK_TYPE_HINTS);
    const withoutHintKeys = Array.from(TASK_TYPES_WITHOUT_HINT).sort();

    const offendingKeys = dispatchKeys.filter(
      (key) => !hintsKeys.includes(key) && !withoutHintKeys.includes(key)
    );

    expect(offendingKeys, `Dispatch key(s) missing: ${offendingKeys.join(', ')}`).toStrictEqual([]);
  });

  it('ensures every TASK_TYPE_HINTS key corresponds to a real dispatch handler', () => {
    const dispatchKeys = Object.keys(handlers);
    const hintsKeys = Object.keys(TASK_TYPE_HINTS);

    const orphanKeys = hintsKeys.filter((key) => !dispatchKeys.includes(key));

    expect(orphanKeys, `Orphaned hint key(s): ${orphanKeys.join(', ')}`).toStrictEqual([]);
  });

  it('ensures TASK_TYPES_WITHOUT_HINT has no overlap with TASK_TYPE_HINTS and all entries are real dispatch keys', () => {
    const dispatchKeys = Object.keys(handlers);
    const hintsKeys = Object.keys(TASK_TYPE_HINTS);
    const withoutHintKeys = Array.from(TASK_TYPES_WITHOUT_HINT);

    const overlappingKeys = withoutHintKeys.filter((key) => hintsKeys.includes(key));
    const nonDispatchKeys = withoutHintKeys.filter((key) => !dispatchKeys.includes(key));

    expect(overlappingKeys, `Keys in both: ${overlappingKeys.join(', ')}`).toStrictEqual([]);
    expect(nonDispatchKeys, `Orphaned allowlist key(s): ${nonDispatchKeys.join(', ')}`).toStrictEqual([]);
  });
});

// ── Cross-cutting guidance-block fingerprints (structural invariants) ─────────
// Handler tests in standard.test.ts, review-fix.test.ts, specialized.test.ts,
// design-doc.test.ts, and research-plan.test.ts all detect dropped guidance
// blocks via fingerprint strings (e.g. './playbook set'). These tests pin those
// fingerprint strings to the source constants so a rename here fails immediately
// rather than silently invalidating the downstream assertions.
describe('cross-cutting guidance-block fingerprints — structural invariants for prompt.ts', () => {
  it('PLAYBOOK_GUIDANCE is under 800 bytes and contains ./playbook set', () => {
    expect(Buffer.byteLength(PLAYBOOK_GUIDANCE)).toBeLessThan(800);
    expect(PLAYBOOK_GUIDANCE).toContain('./playbook set');
  });

  it('DEFAULT_TOOL_GUIDANCE contains fingerprint used by handler structural tests', () => {
    expect(DEFAULT_TOOL_GUIDANCE).toContain('Use Grep/Glob for code search');
    expect(DEFAULT_TOOL_GUIDANCE).toContain('./messages send --stdin');
  });

  it('CODE_CHANGE_HELPER_GUIDANCE contains fingerprint used by handler structural tests', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('Task lifecycle — you own it end-to-end');
  });

  it('READ_THE_FAILURE_MESSAGE contains section header fingerprint', () => {
    expect(READ_THE_FAILURE_MESSAGE).toContain('Read The Failure Message Literally');
  });

  it('SIZE_GUARDRAIL_GUIDANCE contains section header fingerprint', () => {
    expect(SIZE_GUARDRAIL_GUIDANCE).toContain('Size Guardrails Are Advisory');
  });

  it('SIZE_GUARDRAIL_GUIDANCE points at the one-command --fix remediation, not an ad-hoc bump', () => {
    // The divergent "current_size + 50-100 lines" hand-bump must be gone — it
    // contradicted the canonical server/checker policy.
    expect(SIZE_GUARDRAIL_GUIDANCE).not.toContain('50-100 lines');
    // It must point at the single deterministic fix command (the checker's
    // --fix mode), which applies the canonical bump policy.
    expect(SIZE_GUARDRAIL_GUIDANCE).toContain('check_size_limits.py --fix');
    // Keeps the "bump, don't refactor" framing.
    expect(SIZE_GUARDRAIL_GUIDANCE).toContain('Bump, Don\'t Refactor');
  });

  it('PRIOR_ATTEMPT_DIVERGENCE contains section header fingerprint', () => {
    expect(PRIOR_ATTEMPT_DIVERGENCE).toContain("If You're Retrying");
  });

  it('BEYOND_SCOPE_REPORTING contains section header fingerprint', () => {
    expect(BEYOND_SCOPE_REPORTING).toContain('When You Find Bigger Problems Than Your Task');
  });

  it('READ_ONLY_BASH_WRITE_NOTE is a non-empty string', () => {
    expect(typeof READ_ONLY_BASH_WRITE_NOTE).toBe('string');
    expect(READ_ONLY_BASH_WRITE_NOTE.length).toBeGreaterThan(0);
  });

  it('READ_ONLY_BASH_WRITE_NOTE names Bash as the write mechanism', () => {
    expect(READ_ONLY_BASH_WRITE_NOTE).toContain('Bash');
  });

  it('READ_ONLY_BASH_WRITE_NOTE states that Write/Edit tools are disabled', () => {
    expect(READ_ONLY_BASH_WRITE_NOTE).toContain('Write');
    expect(READ_ONLY_BASH_WRITE_NOTE).toContain('disabled');
  });
});

// ── SELF_REVIEW_PROMPT_VERSION — version lock ─────────────────────────────────
// Pin the version so trend data correctly attributes the regime change when
// create/fix/docs/deps focus blocks were added. If this test fails, bump the
// version in prompt.ts and reset this assertion to the new value.
describe('SELF_REVIEW_PROMPT_VERSION — regime version lock', () => {
  it('is "8" (current pinned version)', () => {
    expect(SELF_REVIEW_PROMPT_VERSION).toBe('8');
  });

  it('is a non-empty string (used as prompt_version field in self-review JSON)', () => {
    expect(typeof SELF_REVIEW_PROMPT_VERSION).toBe('string');
    expect(SELF_REVIEW_PROMPT_VERSION).not.toBe('');
  });
});

// ── Viewport before/after screenshot pilot ────────────────────────────────────
// The create/fix/refactor hints must reference the committed snapshot script at
// its exact path, gated on an admin-ui/src/components diff.
describe('viewport snapshot pilot — agent prompt wiring', () => {
  it('VIEWPORT_SNAPSHOT_HINT references the committed script path and the components guard', () => {
    expect(VIEWPORT_SNAPSHOT_HINT).toContain('bash scripts/viewport-snapshot.sh');
    expect(VIEWPORT_SNAPSHOT_HINT).toContain('admin-ui/src/components/');
    expect(VIEWPORT_SNAPSHOT_HINT).toContain('375px');
  });

  for (const t of ['create', 'fix', 'refactor']) {
    it(`static TASK_TYPE_HINTS['${t}'] embeds the viewport-snapshot hint`, () => {
      expect(TASK_TYPE_HINTS[t]).toContain('bash scripts/viewport-snapshot.sh');
    });
  }

  for (const t of ['create', 'refactor']) {
    it(`dynamic getTaskTypeHints['${t}'] override keeps the viewport-snapshot hint`, () => {
      const hint = getTaskTypeHints({
        directories: [{ path: 'admin-ui', type: 'node' }],
      })[t];
      expect(hint).toContain('bash scripts/viewport-snapshot.sh');
    });
  }

});

describe('buildViewportScreenshotSection — downloaded PNG wiring', () => {
  it('returns empty string when no screenshots were downloaded', () => {
    expect(buildViewportScreenshotSection([])).toBe('');
  });

  it('lists each downloaded path so the agent can Read the PNGs', () => {
    const section = buildViewportScreenshotSection([
      '/tmp/viewport_fix_t1/Dashboard-375-before.png',
      '/tmp/viewport_fix_t1/Dashboard-375-after.png',
      '/tmp/viewport_fix_t1/Dashboard-375-diff.png',
    ]);
    expect(section).toContain('## Viewport Screenshots');
    expect(section).toContain('Use the Read tool');
    expect(section).toContain('/tmp/viewport_fix_t1/Dashboard-375-before.png');
    expect(section).toContain('/tmp/viewport_fix_t1/Dashboard-375-after.png');
    expect(section).toContain('/tmp/viewport_fix_t1/Dashboard-375-diff.png');
  });

  it('groups before/after/diff paths under one key per component+width', () => {
    const section = buildViewportScreenshotSection([
      '/tmp/viewport_review_t2/Panel-375-before.png',
      '/tmp/viewport_review_t2/Panel-375-after.png',
      '/tmp/viewport_review_t2/Header-768-after.png',
    ]);
    // Exactly two grouping headers (Panel-375 and Header-768), not three.
    expect(section.match(/\*\*[^*]+\*\*/g)).toHaveLength(2);
    expect(section).toContain('**Panel-375**');
    expect(section).toContain('**Header-768**');
  });
});

// ── assembleFailureGuidanceBlocks — structural invariants ─────────────────────
// code-change handlers (standard.ts, review-fix.ts, specialized.ts) delegate to
// assembleFailureGuidanceBlocks() for the five cross-cutting failure-guidance
// blocks. Pins block inclusion and canonical order so a dropped block or
// reordering is caught at change-time.
describe('assembleFailureGuidanceBlocks — structural invariants', () => {
  it('includes all 5 required failure-guidance blocks', () => {
    const blocks = assembleFailureGuidanceBlocks();
    expect(blocks).toContain('Read The Failure Message Literally');    // READ_THE_FAILURE_MESSAGE
    expect(blocks).toContain('Size Guardrails Are Advisory');          // SIZE_GUARDRAIL_GUIDANCE
    expect(blocks).toContain("If You're Retrying");                    // PRIOR_ATTEMPT_DIVERGENCE
    expect(blocks).toContain('When You Find Bigger Problems Than Your Task'); // BEYOND_SCOPE_REPORTING
    expect(blocks).toContain('Edges-Considered Field in Self-Review'); // EDGES_CONSIDERED_GUIDANCE
  });

  it('assembles blocks in canonical order (READ > SIZE > PRIOR > BEYOND > EDGES)', () => {
    const blocks = assembleFailureGuidanceBlocks();
    const rfmIdx = blocks.indexOf('Read The Failure Message Literally');
    const sgIdx  = blocks.indexOf('Size Guardrails Are Advisory');
    const padIdx = blocks.indexOf("If You're Retrying");
    const bsrIdx = blocks.indexOf('When You Find Bigger Problems Than Your Task');
    const ecIdx  = blocks.indexOf('Edges-Considered Field in Self-Review');
    expect(rfmIdx).toBeLessThan(sgIdx);
    expect(sgIdx).toBeLessThan(padIdx);
    expect(padIdx).toBeLessThan(bsrIdx);
    expect(bsrIdx).toBeLessThan(ecIdx);
  });
});

// ── formatSelfReviewCalibrationSection ───────────────────────────────────────
describe('formatSelfReviewCalibrationSection', () => {
  it('returns empty string when source is undefined', () => {
    expect(formatSelfReviewCalibrationSection(undefined)).toBe('');
  });

  it('returns empty string when prompt_version_changed is true', () => {
    const source = {
      categories: { regression_risk: { miss_rate: 0.6, below_floor: true } },
      prompt_version_changed: true,
    };
    expect(formatSelfReviewCalibrationSection(source)).toBe('');
  });

  it('returns empty string when no categories have miss_rate >= 0.5', () => {
    const source = {
      categories: { correctness: { miss_rate: 0.3, below_floor: false } },
    };
    expect(formatSelfReviewCalibrationSection(source)).toBe('');
  });

  it('returns calibration block for below_floor categories using top-level categories', () => {
    const source = {
      categories: { regression_risk: { miss_rate: 0.6, below_floor: true } },
    };
    const result = formatSelfReviewCalibrationSection(source);
    expect(result).toContain('## Self-review calibration');
    expect(result).toContain('regression_risk');
    expect(result).toContain('60%');
  });

  it('caps output at the top 2 categories by miss rate', () => {
    const source = {
      categories: {
        regression_risk: { miss_rate: 0.7, below_floor: true },
        scope_drift: { miss_rate: 0.8, below_floor: true },
        correctness: { miss_rate: 0.6, below_floor: true },
      },
    };
    const result = formatSelfReviewCalibrationSection(source);
    const matches = [...result.matchAll(/Historical pattern:/g)];
    expect(matches.length).toBe(2);
    expect(result).toContain('scope_drift');
    expect(result).toContain('regression_risk');
    expect(result).not.toContain('correctness');
  });

  it('uses by_task_type map when taskType is provided', () => {
    const source = {
      by_task_type: {
        create: { regression_risk: { miss_rate: 0.65, below_floor: true } },
        fix: { correctness: { miss_rate: 0.55, below_floor: true } },
      },
    };
    const result = formatSelfReviewCalibrationSection(source, 'create');
    expect(result).toContain('regression_risk');
    expect(result).not.toContain('correctness');
  });

  it('falls back to for_task_type when by_task_type lacks the requested task type', () => {
    const source = {
      by_task_type: {},
      for_task_type: { scope_drift: { miss_rate: 0.75, below_floor: true } },
    };
    const result = formatSelfReviewCalibrationSection(source, 'create');
    expect(result).toContain('scope_drift');
    expect(result).toContain('75%');
  });

  it('omits entries where below_floor is false when taskType is provided', () => {
    const source = {
      by_task_type: {
        create: {
          regression_risk: { miss_rate: 0.6, below_floor: false },
          scope_drift: { miss_rate: 0.7, below_floor: true },
        },
      },
    };
    const result = formatSelfReviewCalibrationSection(source, 'create');
    expect(result).toContain('scope_drift');
    expect(result).not.toContain('regression_risk');
  });

  it('returns empty string when categories are empty', () => {
    const source = { categories: {} };
    expect(formatSelfReviewCalibrationSection(source)).toBe('');
  });

  it('prepends mixed-version caveat when mixed_prompt_versions is true', () => {
    const source = {
      categories: { regression_risk: { miss_rate: 0.6, below_floor: true } },
      mixed_prompt_versions: true,
    };
    const result = formatSelfReviewCalibrationSection(source);
    expect(result).toContain('## Self-review calibration');
    expect(result).toContain('spans multiple SR prompt versions');
    expect(result).toContain('regression_risk');
  });

  it('omits mixed-version caveat when mixed_prompt_versions is false', () => {
    const source = {
      categories: { regression_risk: { miss_rate: 0.6, below_floor: true } },
      mixed_prompt_versions: false,
    };
    const result = formatSelfReviewCalibrationSection(source);
    expect(result).toContain('## Self-review calibration');
    expect(result).not.toContain('spans multiple SR prompt versions');
  });

  it('does not suppress calibration when mixed_prompt_versions is true (unlike prompt_version_changed)', () => {
    const source = {
      categories: { regression_risk: { miss_rate: 0.6, below_floor: true } },
      mixed_prompt_versions: true,
      prompt_version_changed: false,
    };
    const result = formatSelfReviewCalibrationSection(source);
    expect(result).not.toBe('');
    expect(result).toContain('regression_risk');
  });
});

// ── CODE_CHANGE_HELPER_GUIDANCE — regressing-category reasoning ───────────────
describe('CODE_CHANGE_HELPER_GUIDANCE — regressing-category reasoning', () => {
  it('includes a reasoning field in the schema example for check_results', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('"reasoning"');
  });

  it('requires reasoning for both pass and fail outcomes on regressing categories', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('BOTH pass and fail');
  });

  it('references the Self-Review Blind Spots section as the source of regressing categories', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('Self-Review Blind Spots');
  });

  it('instructs agent to describe what was inspected in the reasoning field', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('what you inspected');
  });

  it('mentions the ./done exit error so agent knows to update SR JSON and re-run', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('reasoning field');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('./done');
  });
});

// ── CODE_CHANGE_HELPER_GUIDANCE — evidence anchors for high-stakes PASS verdicts ──
describe('CODE_CHANGE_HELPER_GUIDANCE — evidence anchors', () => {
  it('requires evidence anchors for PASS on correctness category', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('correctness');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('evidence anchor');
  });

  it('requires evidence anchors for PASS on error-handling category', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('error-handling');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('evidence anchor');
  });

  it('requires evidence anchors for PASS on security category', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('security');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('evidence anchor');
  });

  it('requires evidence anchors for PASS on regression_risk category', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('regression_risk');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('evidence anchor');
  });

  it('requires evidence anchors for PASS on scope-drift category', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('scope-drift');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('evidence anchor');
  });

  it('specifies file:line format as a valid anchor type', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toMatch(/file:line|path\/to\/file\.ts:/);
  });

  it('specifies code snippet format as a valid anchor type', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toMatch(/code snippet|quoted|backtick/);
  });

  it('specifies scope-drift anchor formats (diff stats and file list)', () => {
    // Trigger A (diff stat) is on the same line as the reference; Trigger B and
    // related_files may be on adjacent lines, so use dotAll for the B branch.
    expect(CODE_CHANGE_HELPER_GUIDANCE).toMatch(/Trigger A.*diff.*stat|Trigger B[\s\S]*?related_files/);
  });

  it('mentions that this requirement applies to high-stakes categories', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toMatch(/high-stakes|five categories/);
  });

  it('excludes lower-stakes categories from evidence anchor requirement', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toMatch(/lower-stakes|style-conventions|documentation|performance|architecture/);
  });

  it('documents scope-drift-specific anchor requirements (Trigger A and B)', () => {
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('Trigger A');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('Trigger B');
  });
});


// ── buildCodeChangeHelperGuidance — project-aware ./check intro ──────────────
describe('buildCodeChangeHelperGuidance', () => {
  const SELF = { owner: 'HenryHamster', repo: 'BlueprintAIStudio' };
  const FOREIGN = { owner: 'acme', repo: 'webapp' };
  // A non-Blueprint build_config (no admin-ui/agent/backend, no test-backend.sh).
  const FOREIGN_BUILD_CONFIG = {
    directories: [
      { path: 'frontend', type: 'node', lint_cmd: 'eslint src', build_cmd: 'vite build', test_cmd: 'vitest run' },
      { path: 'api', type: 'python', lint_cmd: 'ruff check', test_cmd: 'pytest -q' },
    ],
  };

  // The repo-agnostic parts must survive every variant.
  const sharedInvariants = (g: string) => {
    expect(g).toContain('Task lifecycle — you own it end-to-end');
    expect(g).toContain('./pr open');
    expect(g).toContain('./done');
    expect(g).toContain('./fail');
    expect(g).toContain('./blocked');
    // self-review schema + the three informational checks live in the remainder
    expect(g).toContain('self_review_');
    expect(g).toContain('scope-drift');
    expect(g).toContain('regression_risk');
    expect(g).toContain('cross-system-contract');
    // Terminal-verb guarantee: every run must end with a terminal verb, and the
    // prompt must explicitly state there is no monitor/watcher that resumes the
    // session (regression guard for the passive "I'll wait for the monitor" exit).
    expect(g).toContain('You MUST end every run with a terminal verb');
    expect(g).toContain('NO external monitor');
    expect(g).toMatch(/will resume (this session|you)/i);
    expect(g).toContain("I'll wait for the monitor");
    expect(g).toContain('not-shipped');
  };

  // Isolate the stack-specific ./check intro (between the preamble and the
  // first shared bullet). Blueprint-only stack identity must not appear HERE —
  // note the preserved cross-system-contract example in the remainder legitimately
  // lists {admin-ui, backend, agent}, so bare-token checks must be scoped to the intro.
  const checkIntroOf = (g: string): string => {
    const start = g.indexOf('executable):');
    const end = g.indexOf('./pr open --title');
    expect(start).toBeGreaterThanOrEqual(0);
    expect(end).toBeGreaterThan(start);
    return g.slice(start, end);
  };

  it('self-repo + no build_config returns CODE_CHANGE_HELPER_GUIDANCE byte-for-byte', () => {
    const g = buildCodeChangeHelperGuidance({ ...SELF });
    expect(g).toBe(CODE_CHANGE_HELPER_GUIDANCE);
    // sanity: it does keep the Blueprint-specific paragraph
    expect(g).toContain('scripts/test-backend.sh');
    expect(checkIntroOf(g)).toContain('admin-ui eslint');
    sharedInvariants(g);
  });

  it('check intro matches generated vitest behavior', () => {
    const g = buildCodeChangeHelperGuidance({ ...SELF });
    const intro = checkIntroOf(g);
    // The generated ./check runs npx vitest run (= npm run test:run) on every
    // admin-ui invocation — assert the intro reflects this accurately.
    expect(intro).toContain('npm run test:viewport');
    expect(intro).toMatch(/npx vitest run|npm run test:run/);
    // Must NOT contain the false claim that the unit suite is skipped.
    expect(intro).not.toMatch(/not the .*test:run/);
    expect(intro).not.toContain('not the jsdom-only');
  });

  it('build_config variant omits Blueprint-only stacks and references configured commands', () => {
    const g = buildCodeChangeHelperGuidance({ buildConfig: FOREIGN_BUILD_CONFIG, ...FOREIGN });
    // scripts/test-backend.sh / "backend ruff" / "agent tsc" / "8 GB" appear ONLY in
    // the Blueprint intro, never the shared remainder — safe to assert on the full string.
    expect(g).not.toContain('scripts/test-backend.sh');
    expect(g).not.toContain('agent tsc');
    expect(g).not.toContain('backend ruff');
    expect(g).not.toContain('8 GB');
    // bare "admin-ui" must be gone from the stack intro (it survives only in the
    // preserved cross-system-contract example in the remainder).
    expect(checkIntroOf(g)).not.toContain('admin-ui');
    // references the project's own directories + commands
    expect(g).toContain('frontend');
    expect(g).toContain('vite build');
    expect(g).toContain('vitest run');
    expect(g).toContain('api');
    expect(g).toContain('pytest -q');
    sharedInvariants(g);
  });

  it('build_config variant is used even on the self-repo when directories are configured', () => {
    const g = buildCodeChangeHelperGuidance({ buildConfig: FOREIGN_BUILD_CONFIG, ...SELF });
    expect(g).not.toContain('scripts/test-backend.sh');
    expect(g).toContain('vite build');
  });

  it('build_config variant explains the honest pytest gate only when a pytest test_cmd is configured', () => {
    // FOREIGN_BUILD_CONFIG carries `api` with `pytest -q` → ./check routes it
    // through the honest gate (harness preference + GATE-ENVIRONMENT), so the
    // intro must tell the agent what that note means and how to act on it.
    const g = buildCodeChangeHelperGuidance({ buildConfig: FOREIGN_BUILD_CONFIG, ...FOREIGN });
    expect(checkIntroOf(g)).toContain('GATE-ENVIRONMENT');
    expect(checkIntroOf(g)).toContain('scripts/test-<dir>.sh');
    expect(checkIntroOf(g)).toContain('record your own faithful test evidence');
    // A config with no pytest-style test command gets no pytest-gate prose.
    const nodeOnly = {
      directories: [{ path: 'web', type: 'node', test_cmd: 'vitest run' }],
    };
    const g2 = buildCodeChangeHelperGuidance({ buildConfig: nodeOnly, ...FOREIGN });
    expect(checkIntroOf(g2)).not.toContain('GATE-ENVIRONMENT');
    expect(g2).toContain('GATE-ENVIRONMENT'); // shared all-code-change lifecycle rule
    sharedInvariants(g);
  });

  it('non-Blueprint repo + no build_config matches the no-op ./check fallback', () => {
    const g = buildCodeChangeHelperGuidance({ ...FOREIGN });
    expect(g).not.toContain('scripts/test-backend.sh');
    expect(g).not.toContain('backend ruff');
    expect(checkIntroOf(g)).not.toContain('admin-ui');
    expect(g).toContain('no configured build_config');
    expect(g).toContain('CI is the backstop');
    // still mentions both check entry points
    expect(g).toContain('./check');
    expect(g).toContain('./check test');
    sharedInvariants(g);
  });

  it('empty directories list is treated as no build_config (falls through to repo branch)', () => {
    const g = buildCodeChangeHelperGuidance({ buildConfig: { directories: [] }, ...SELF });
    expect(g).toBe(CODE_CHANGE_HELPER_GUIDANCE);
  });
});

describe('codeChangeGuidanceVariant + fingerprint divergence', () => {
  const SELF = { owner: 'HenryHamster', repo: 'BlueprintAIStudio' };
  const FOREIGN = { owner: 'acme', repo: 'webapp' };
  const BUILD_CONFIG = { directories: [{ path: 'web', type: 'node', build_cmd: 'npm run build' }] };

  it('labels each branch distinctly', () => {
    expect(codeChangeGuidanceVariant({ ...SELF })).toBe('self-repo');
    expect(codeChangeGuidanceVariant({ ...FOREIGN })).toBe('generic');
    expect(codeChangeGuidanceVariant({ buildConfig: BUILD_CONFIG, ...FOREIGN })).toBe('build-config');
  });

  it('fingerprint diverges when the guidance variant diverges (same task type)', () => {
    const selfFp = computePromptFingerprint('fix', undefined, { codeChangeGuidanceVariant: 'self-repo' });
    const genericFp = computePromptFingerprint('fix', undefined, { codeChangeGuidanceVariant: 'generic' });
    expect(selfFp).not.toBe(genericFp);
  });

  it('omitting the variant keeps the legacy fingerprint unchanged (backward compatible)', () => {
    const legacy = computePromptFingerprint('fix');
    const explicit = computePromptFingerprint('fix', undefined, {});
    expect(explicit).toBe(legacy);
  });
});


// ── writeDesignDocHelper ────────────────────────────────────────────────────────
describe('writeDesignDocHelper', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('writes /tmp/workspace/design-doc at mode 0o755', () => {
    writeDesignDocHelper('task-xyz');
    const call = vi.mocked(fs.writeFileSync).mock.calls.find(
      c => c[0] === '/tmp/workspace/design-doc',
    );
    expect(call).toBeDefined();
    expect(call?.[2]).toEqual({ mode: 0o755 });
  });

  it('preserves the design-doc get|set|delete|list CLI surface and bakes in the task id', () => {
    writeDesignDocHelper('task-xyz');
    const script = capturedScripts['/tmp/workspace/design-doc'];
    expect(script.startsWith('#!/bin/bash')).toBe(true);
    expect(script).toContain('design-doc get|set|delete|list');
    expect(script).toContain('TASK="task-xyz"');
    // POST body shape the backend op handler parses must be unchanged.
    // KEY and TASK are encoded via _json_str (not bare %s) to survive quotes/newlines.
    expect(script).toContain('"op":"set","key":%s,"value":%s');
    expect(script).toContain('"op":"delete","key":%s}');
    expect(script).toContain('$(_json_str "$KEY")');
  });

  it('makes set/delete durable: a retry loop honoring Retry-After on 429/5xx', () => {
    writeDesignDocHelper('task-xyz');
    const script = capturedScripts['/tmp/workspace/design-doc'];
    // Shared bounded retry function wired to the write ops (max=4, 5s/15s/30s backoff).
    expect(script).toContain('_post_with_retry()');
    expect(script).toContain('max=4');
    // set and delete pipe through the shared retry wrapper.
    expect(script).toContain('| \\\n    _post_with_retry "$API/api/agent/design-doc" "design-doc set"');
    expect(script).toContain('| \\\n    _post_with_retry "$API/api/agent/design-doc" "design-doc delete"');
    // Retries on contention (429) and transient server errors (5xx).
    expect(script).toContain('"$http_code" = "429"');
    expect(script).toContain('"$http_code" -ge 500');
    // Honors the Retry-After header before backing off.
    expect(script).toContain('retry-after:');
    expect(script).toContain('sleep "$delay"');
  });

  it('retries curl transport failures (http_code 000) alongside 429/5xx', () => {
    writeDesignDocHelper('task-xyz');
    const script = capturedScripts['/tmp/workspace/design-doc'];
    // curl reports http_code "000" on a transport-level failure (connect
    // timeout, DNS, TLS, dropped connection) — no HTTP response. The shared
    // _post_with_retry wrapper must treat it as transient and retry, not fall
    // through to the non-retryable else branch (which would book a network blip
    // as a permanent write failure and silently drop the write).
    expect(script).toContain('[ "$http_code" = "000" ]');
    // The 000 check shares the SAME retry condition as 429 (one `if` line) so it
    // reuses the existing backoff/sleep path rather than a separate branch.
    expect(script).toMatch(/\[ "\$http_code" = "000" \] \|\| \[ "\$http_code" = "429" \]/);
  });

  it('design-doc escapes adversarial key/value: _json_str routes KEY and TASK through JSON encoder', () => {
    writeDesignDocHelper('task-xyz');
    const script = capturedScripts['/tmp/workspace/design-doc'];
    // _json_str function must be injected so quotes/backslashes/newlines in KEY do not break JSON.
    expect(script).toContain('_json_str()');
    // Every operation (set, delete, get, list) routes KEY and TASK through the encoder.
    expect(script).toContain('$(_json_str "$KEY")');
    expect(script).toContain('$(_json_str "$TASK")');
    // VALUE in set is raw JSON from the caller — must NOT be re-encoded as a string.
    expect(script).not.toContain('$(_json_str "$VALUE")');
    // Old bare-%s encoding must be gone for key and task_id fields.
    expect(script).not.toContain('"task_id":"%s"');
    expect(script).not.toContain('"key":"%s"');
  });

  it('keeps read ops (list/get) fail-fast with no retry', () => {
    writeDesignDocHelper('task-xyz');
    const script = capturedScripts['/tmp/workspace/design-doc'];
    expect(script).toContain('_post_once()');
    // list and get route through the single-shot helper, not the retry loop.
    expect(script).toContain(`printf '{"task_id":%s,"op":"list"}' "$(_json_str "$TASK")" | _post_once`);
    expect(script).toContain(`printf '{"task_id":%s,"op":"get","key":%s}' "$(_json_str "$TASK")" "$(_json_str "$KEY")" | _post_once`);
  });

  it('generated design-doc script passes `bash -n` syntax check', async () => {
    writeDesignDocHelper('task-xyz');
    const script = capturedScripts['/tmp/workspace/design-doc'];
    expect(script).toBeDefined();

    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const tmp = `${realOs.tmpdir().replace(/\\/g, '/')}/design_doc_smoke_${process.pid}_${Date.now()}.sh`;
    realFs.writeFileSync(tmp, script);
    try {
      realCp.execFileSync('bash', ['-n', tmp], { stdio: 'pipe' });
    } finally {
      try { realFs.unlinkSync(tmp); } catch { /* ignore */ }
    }
  });

  it('reads the live token file each invocation and retries a write on 401 (resident write-token fix)', () => {
    writeDesignDocHelper('task-xyz');
    const script = capturedScripts['/tmp/workspace/design-doc'];
    // Fresh live-token resolution, env fallback, no stale hard-`:?` snapshot.
    expect(script).toContain('_load_agent_token()');
    expect(script).toContain('TOKEN=$(cat "${AGENT_TOKEN_FILE}" 2>/dev/null)');
    expect(script).not.toContain('TOKEN="${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"');
    // The shared retry wrapper re-reads the token per attempt and treats 401/403 as
    // a rotated-token retry rather than an immediate non-retryable failure.
    expect(script).toContain('_load_agent_token || return 1');
    // The shared function uses -eq for numeric comparison (not string "= \"401\"").
    expect(script).toContain('[ "$http_code" -eq 401 ] || [ "$http_code" -eq 403 ]');
  });

  // ── Read-only mode (research / plan workspaces) ──────────────────────────────
  // The research/plan prompt advertises `./design-doc get <key>` (and lists
  // section keys to read), so those workspaces must provision a working get/list
  // helper. They must NOT be handed a write path to durable strategic state.
  describe('readOnly mode', () => {
    it('provisions the get/list read surface the research/plan prompt advertises', () => {
      writeDesignDocHelper('task-xyz', '/tmp/workspace', { readOnly: true });
      const script = capturedScripts['/tmp/workspace/design-doc'];
      expect(script.startsWith('#!/bin/bash')).toBe(true);
      expect(script).toContain('design-doc get|list');
      expect(script).toContain('TASK="task-xyz"');
      // get and list still route through the fail-fast single-shot helper.
      expect(script).toContain('_post_once()');
      expect(script).toContain(`printf '{"task_id":%s,"op":"list"}' "$(_json_str "$TASK")" | _post_once`);
      expect(script).toContain(`printf '{"task_id":%s,"op":"get","key":%s}' "$(_json_str "$TASK")" "$(_json_str "$KEY")" | _post_once`);
    });

    it('rejects set/delete (no write path, no retry loop) in read-only workspaces', () => {
      writeDesignDocHelper('task-xyz', '/tmp/workspace', { readOnly: true });
      const script = capturedScripts['/tmp/workspace/design-doc'];
      expect(script).toContain('read-only in this workspace');
      // No durable write path is emitted at all.
      expect(script).not.toContain('_post_with_retry');
      expect(script).not.toContain('"op":"set"');
      expect(script).not.toContain('"op":"delete"');
    });

    it('read-only script passes `bash -n` syntax check', async () => {
      writeDesignDocHelper('task-xyz', '/tmp/workspace', { readOnly: true });
      const script = capturedScripts['/tmp/workspace/design-doc'];
      expect(script).toBeDefined();

      const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
      const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
      const realOs = await vi.importActual<typeof import('node:os')>('node:os');
      const tmp = `${realOs.tmpdir().replace(/\\/g, '/')}/design_doc_ro_smoke_${process.pid}_${Date.now()}.sh`;
      realFs.writeFileSync(tmp, script);
      try {
        realCp.execFileSync('bash', ['-n', tmp], { stdio: 'pipe' });
      } finally {
        try { realFs.unlinkSync(tmp); } catch { /* ignore */ }
      }
    });
  });
});

// ── writeMessagesHelper ──────────────────────────────────────────────────────
describe('writeMessagesHelper', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('preserves non-2xx response bodies so ambiguous short refs show backend candidates', async () => {
    writeMessagesHelper('task-xyz');
    const script = capturedScripts['/tmp/workspace/messages'];
    expect(script).toBeDefined();
    expect(script).toContain('./messages send --stdin [type]');
    // Uses the shared _post_with_retry function (not a private _post_strategic).
    expect(script).toContain('_post_with_retry()');
    // POSTs to the strategic endpoint via the shared retry wrapper.
    expect(script).toContain('_post_with_retry "$API/api/agent/strategic" "messages $OP"');
    expect(script).toContain("-w '\\n%{http_code}'");
    expect(script).toContain('printf \'%s\\n\' "$body" >&2');
    expect(script).toContain('[ERROR] $op_label failed');
    expect(script).not.toContain('curl -f -sS');

    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const tmp = `${realOs.tmpdir().replace(/\\/g, '/')}/messages_smoke_${process.pid}_${Date.now()}.sh`;
    realFs.writeFileSync(tmp, script);
    try {
      realCp.execFileSync('bash', ['-n', tmp], { stdio: 'pipe' });
    } finally {
      try { realFs.unlinkSync(tmp); } catch { /* ignore */ }
    }
  });

  it('reads the live token, captures stdin, retries on 401, and fails loud (resident write-token fix)', async () => {
    writeMessagesHelper('task-xyz');
    const script = capturedScripts['/tmp/workspace/messages'];
    // Fresh live-token resolution with env fallback (no stale hard-`:?` snapshot).
    expect(script).toContain('_load_agent_token()');
    expect(script).toContain('TOKEN=$(cat "${AGENT_TOKEN_FILE}" 2>/dev/null)');
    expect(script).not.toContain('TOKEN="${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"');
    expect(script).toContain('CONTENT="$(cat)"');
    // The JSON body is captured from stdin ONCE via payload=$(cat) in _post_with_retry.
    expect(script).toContain('payload=$(cat)');
    expect(script).toContain('_load_agent_token || return 1');
    // 401/403 → re-read the rotated token and retry via the shared wrapper.
    expect(script).toContain('re-reading rotated agent token and retrying');
    // ...else fail LOUD ("restart the session") rather than silently.
    expect(script).toContain('restart the session');

    // Structural change is still syntactically valid bash.
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const tmp = `${realOs.tmpdir().replace(/\\/g, '/')}/messages_token_smoke_${process.pid}_${Date.now()}.sh`;
    realFs.writeFileSync(tmp, script);
    try {
      realCp.execFileSync('bash', ['-n', tmp], { stdio: 'pipe' });
    } finally {
      try { realFs.unlinkSync(tmp); } catch { /* ignore */ }
    }
  });

  it('retries up to 3 times on 429/5xx, honors Retry-After, then fails loud', async () => {
    writeMessagesHelper('task-xyz');
    const script = capturedScripts['/tmp/workspace/messages'];
    // Shared bounded retry wrapper: max=4 (3 retry slots + the initial attempt).
    expect(script).toContain('max=4');
    expect(script).toContain('while [ $attempt -lt $max ]');
    // Header capture for Retry-After extraction.
    expect(script).toContain('-D "$dump"');
    expect(script).toContain('retry-after:');
    expect(script).toContain('sleep "$delay"');
    // Retries on 429 (lock contention) and 5xx (transient server error).
    expect(script).toContain('"$http_code" = "429"');
    expect(script).toContain('"$http_code" -ge 500');
    // Progress message so the agent can see retry activity.
    expect(script).toContain('[RETRY');
    expect(script).toContain('retrying in');
    // After all retries exhausted, surfaces a real error via the shared wrapper.
    expect(script).toContain('[ERROR] $op_label failed after $max attempts');

    // Generated script must be syntactically valid bash.
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const tmp = `${realOs.tmpdir().replace(/\\/g, '/')}/messages_429_smoke_${process.pid}_${Date.now()}.sh`;
    realFs.writeFileSync(tmp, script);
    try {
      realCp.execFileSync('bash', ['-n', tmp], { stdio: 'pipe' });
    } finally {
      try { realFs.unlinkSync(tmp); } catch { /* ignore */ }
    }
  });
});

// ── writeMessagesHelper stdin-based send (quote-safety) ──────────────────────
describe('writeMessagesHelper stdin-based send', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('reads content from stdin when --stdin flag is used, MSG_TYPE from $3', () => {
    writeMessagesHelper('task-stdin-test');
    const script = capturedScripts['/tmp/workspace/messages'];
    expect(script).toBeDefined();
    // --stdin flag triggers stdin mode
    expect(script).toContain('--stdin');
    // Content is read from stdin with double-quoted command substitution
    expect(script).toContain('CONTENT="$(cat)"');
    // MSG_TYPE is $3 in --stdin mode (first positional after verb and --stdin flag)
    expect(script).toContain('MSG_TYPE="${3:-NOTE}"');
  });

  it('fails loud with a clear error when content is empty', () => {
    writeMessagesHelper('task-stdin-test');
    const script = capturedScripts['/tmp/workspace/messages'];
    expect(script).toContain('Error: send requires message content');
  });

  it('builds payload into PAYLOAD without requiring jq', () => {
    writeMessagesHelper('task-stdin-test');
    const script = capturedScripts['/tmp/workspace/messages'];
    expect(script).toContain('_json_str()');
    expect(script).toContain('PAYLOAD=$(printf');
    expect(script).not.toContain('PAYLOAD=$(jq -n');
    expect(script).toContain('[ERROR] send: failed to serialize message payload');
    // Sends the PAYLOAD (not raw jq pipe) via _post_with_retry
    expect(script).toContain('printf \'%s\' "$PAYLOAD" | _post_with_retry');
  });

  it('sends content with single quotes end-to-end via stdin', async () => {
    writeMessagesHelper('task-q-test');
    const script = capturedScripts['/tmp/workspace/messages'];
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const tmpDir = realOs.tmpdir();
    // Write the messages script
    const scriptPath = realPath.join(tmpDir, `messages_quote_test_${process.pid}.sh`);
    realFs.writeFileSync(scriptPath, script, { mode: 0o755 });
    // Mock curl: captures --data-binary stdin, writes to a file, exits 0 with 200
    const captureFile = realPath.join(tmpDir, `messages_captured_${process.pid}.json`);
    const mockCurlDir = realPath.join(tmpDir, `messages_mockbin_${process.pid}`);
    realFs.mkdirSync(mockCurlDir, { recursive: true });
    const mockCurlPath = realPath.join(mockCurlDir, 'curl');
    realFs.writeFileSync(mockCurlPath, [
      '#!/bin/bash',
      `CAPTURE_FILE="${captureFile}"`,
      '# Read piped stdin (--data-binary @-) and capture it',
      'cat > "$CAPTURE_FILE"',
      "echo '{\"status\":\"ok\"}'",
      'echo "200"',
    ].join('\n'), { mode: 0o755 });
    const contentWithQuote = "Everything's fine and \"quoted\"";
    try {
      const result = realCp.spawnSync('bash', [scriptPath, 'send', '--stdin', 'NOTE'], {
        input: contentWithQuote,
        encoding: 'utf8',
        env: {
          ...process.env,
          ORCHESTRATOR_URL: 'http://mock',
          AGENT_API_TOKEN: 'tok',
          PATH: `${mockCurlDir}:${process.env.PATH}`,
        },
        timeout: 5000,
      });
      expect(result.status).toBe(0);
      const captured = realFs.readFileSync(captureFile, 'utf8').trim();
      const parsed = JSON.parse(captured) as { data: { content: string; type: string } };
      expect(parsed.data.content).toBe(contentWithQuote);
      expect(parsed.data.type).toBe('NOTE');
    } finally {
      try { realFs.unlinkSync(scriptPath); } catch { /* ignore */ }
      try { realFs.unlinkSync(captureFile); } catch { /* ignore */ }
      try { realFs.unlinkSync(mockCurlPath); realFs.rmdirSync(mockCurlDir); } catch { /* ignore */ }
    }
  });

  it('sends content with newlines end-to-end via stdin', async () => {
    writeMessagesHelper('task-nl-test');
    const script = capturedScripts['/tmp/workspace/messages'];
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const tmpDir = realOs.tmpdir();
    const scriptPath = realPath.join(tmpDir, `messages_nl_test_${process.pid}.sh`);
    realFs.writeFileSync(scriptPath, script, { mode: 0o755 });
    const captureFile = realPath.join(tmpDir, `messages_captured_nl_${process.pid}.json`);
    const mockCurlDir = realPath.join(tmpDir, `messages_mockbin_nl_${process.pid}`);
    realFs.mkdirSync(mockCurlDir, { recursive: true });
    const mockCurlPath = realPath.join(mockCurlDir, 'curl');
    realFs.writeFileSync(mockCurlPath, [
      '#!/bin/bash',
      `CAPTURE_FILE="${captureFile}"`,
      'cat > "$CAPTURE_FILE"',
      "echo '{\"status\":\"ok\"}'",
      'echo "200"',
    ].join('\n'), { mode: 0o755 });
    const contentWithNewlines = 'Line one\nLine two has an apostrophe\'s content\nLine three: "quoted"';
    try {
      const result = realCp.spawnSync('bash', [scriptPath, 'send', '--stdin', 'BUG_REPORT'], {
        input: contentWithNewlines,
        encoding: 'utf8',
        env: {
          ...process.env,
          ORCHESTRATOR_URL: 'http://mock',
          AGENT_API_TOKEN: 'tok',
          PATH: `${mockCurlDir}:${process.env.PATH}`,
        },
        timeout: 5000,
      });
      expect(result.status).toBe(0);
      const captured = realFs.readFileSync(captureFile, 'utf8').trim();
      const parsed = JSON.parse(captured) as { data: { content: string; type: string } };
      expect(parsed.data.content).toBe(contentWithNewlines);
      expect(parsed.data.type).toBe('BUG_REPORT');
    } finally {
      try { realFs.unlinkSync(scriptPath); } catch { /* ignore */ }
      try { realFs.unlinkSync(captureFile); } catch { /* ignore */ }
      try { realFs.unlinkSync(mockCurlPath); realFs.rmdirSync(mockCurlDir); } catch { /* ignore */ }
    }
  });

  it('fails loud (non-zero exit + stderr message) when stdin is empty', async () => {
    writeMessagesHelper('task-empty-test');
    const script = capturedScripts['/tmp/workspace/messages'];
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const tmpDir = realOs.tmpdir();
    const scriptPath = realPath.join(tmpDir, `messages_empty_test_${process.pid}.sh`);
    realFs.writeFileSync(scriptPath, script, { mode: 0o755 });
    try {
      const result = realCp.spawnSync('bash', [scriptPath, 'send', '--stdin', 'NOTE'], {
        input: '',
        encoding: 'utf8',
        env: { ...process.env, ORCHESTRATOR_URL: 'http://mock', AGENT_API_TOKEN: 'tok' },
        timeout: 5000,
      });
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain('Error: send requires message content');
    } finally {
      try { realFs.unlinkSync(scriptPath); } catch { /* ignore */ }
    }
  });

  it('generated script passes bash -n syntax check', async () => {
    writeMessagesHelper('task-syntax-test');
    const script = capturedScripts['/tmp/workspace/messages'];
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const tmp = realPath.join(realOs.tmpdir(), `messages_stdin_syntax_${process.pid}.sh`);
    realFs.writeFileSync(tmp, script);
    try {
      realCp.execFileSync('bash', ['-n', tmp], { stdio: 'pipe' });
    } finally {
      try { realFs.unlinkSync(tmp); } catch { /* ignore */ }
    }
  });

  it('send builds valid JSON with jq REMOVED from PATH (control + unicode + quotes)', async () => {
    writeMessagesHelper('task-nojq-send');
    const script = capturedScripts['/tmp/workspace/messages'];
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const tmpDir = realOs.tmpdir();
    const scriptPath = realPath.join(tmpDir, `messages_nojq_send_${process.pid}.sh`);
    realFs.writeFileSync(scriptPath, script, { mode: 0o755 });
    // A "poison" jq at the FRONT of PATH: if the generated script ever shells out
    // to jq it records a marker (and errors), so a jq-free write path is proven
    // by the marker's ABSENCE — not merely by jq being missing.
    const binDir = realPath.join(tmpDir, `messages_nojq_send_bin_${process.pid}`);
    realFs.mkdirSync(binDir, { recursive: true });
    const captureFile = realPath.join(tmpDir, `messages_nojq_send_cap_${process.pid}.json`);
    const jqMarker = realPath.join(tmpDir, `messages_nojq_send_jqcalled_${process.pid}`);
    realFs.writeFileSync(realPath.join(binDir, 'curl'), [
      '#!/bin/bash',
      `cat > "${captureFile}"`,
      "echo '{\"status\":\"ok\"}'",
      'echo "200"',
    ].join('\n'), { mode: 0o755 });
    realFs.writeFileSync(realPath.join(binDir, 'jq'), [
      '#!/bin/bash',
      `echo called > "${jqMarker}"`,
      'exit 1',
    ].join('\n'), { mode: 0o755 });
    // Content mixes an apostrophe, embedded double-quotes, a newline, a tab, a
    // control char (0x01) and multibyte unicode — every branch of _json_str.
    const content = "It's a \"quoted\" line\nsecond\ttabctrl café → π";
    try {
      const result = realCp.spawnSync('bash', [scriptPath, 'send', '--stdin', 'BUG_REPORT'], {
        input: content,
        encoding: 'utf8',
        env: {
          ...process.env,
          ORCHESTRATOR_URL: 'http://mock',
          AGENT_API_TOKEN: 'tok',
          // ONLY binDir on PATH: the real jq is unreachable, coreutils resolve
          // through the poison binDir falling back to the system path we append.
          PATH: `${binDir}:${process.env.PATH}`,
        },
        timeout: 5000,
      });
      expect(result.status).toBe(0);
      // jq was never invoked — the write path is genuinely jq-free.
      expect(realFs.existsSync(jqMarker)).toBe(false);
      const captured = realFs.readFileSync(captureFile, 'utf8').trim();
      const parsed = JSON.parse(captured) as {
        task_id: string; op: string; data: { type: string; content: string; idempotency_key: string };
      };
      expect(parsed.task_id).toBe('task-nojq-send');
      expect(parsed.op).toBe('create_message');
      expect(parsed.data.type).toBe('BUG_REPORT');
      expect(parsed.data.content).toBe(content);
      expect(parsed.data.idempotency_key).toMatch(/^[0-9a-f]{64}$/);
    } finally {
      try { realFs.unlinkSync(scriptPath); } catch { /* ignore */ }
      try { realFs.unlinkSync(captureFile); } catch { /* ignore */ }
      try { realFs.unlinkSync(jqMarker); } catch { /* ignore */ }
      try {
        realFs.unlinkSync(realPath.join(binDir, 'curl'));
        realFs.unlinkSync(realPath.join(binDir, 'jq'));
        realFs.rmdirSync(binDir);
      } catch { /* ignore */ }
    }
  });

  it('resolve builds valid JSON with jq REMOVED from PATH', async () => {
    writeMessagesHelper('task-nojq-resolve');
    const script = capturedScripts['/tmp/workspace/messages'];
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const tmpDir = realOs.tmpdir();
    const scriptPath = realPath.join(tmpDir, `messages_nojq_resolve_${process.pid}.sh`);
    realFs.writeFileSync(scriptPath, script, { mode: 0o755 });
    const binDir = realPath.join(tmpDir, `messages_nojq_resolve_bin_${process.pid}`);
    realFs.mkdirSync(binDir, { recursive: true });
    const captureFile = realPath.join(tmpDir, `messages_nojq_resolve_cap_${process.pid}.json`);
    const jqMarker = realPath.join(tmpDir, `messages_nojq_resolve_jqcalled_${process.pid}`);
    realFs.writeFileSync(realPath.join(binDir, 'curl'), [
      '#!/bin/bash',
      `cat > "${captureFile}"`,
      "echo '{\"status\":\"ok\"}'",
      'echo "200"',
    ].join('\n'), { mode: 0o755 });
    realFs.writeFileSync(realPath.join(binDir, 'jq'), [
      '#!/bin/bash',
      `echo called > "${jqMarker}"`,
      'exit 1',
    ].join('\n'), { mode: 0o755 });
    try {
      const result = realCp.spawnSync('bash', [scriptPath, 'resolve', 'abc123ef', 'Done: it\'s "fixed"'], {
        encoding: 'utf8',
        env: {
          ...process.env,
          ORCHESTRATOR_URL: 'http://mock',
          AGENT_API_TOKEN: 'tok',
          PATH: `${binDir}:${process.env.PATH}`,
        },
        timeout: 5000,
      });
      expect(result.status).toBe(0);
      expect(realFs.existsSync(jqMarker)).toBe(false);
      const captured = realFs.readFileSync(captureFile, 'utf8').trim();
      const parsed = JSON.parse(captured) as {
        task_id: string; op: string; data: { message_id: string; resolution: string };
      };
      expect(parsed.op).toBe('resolve_message');
      expect(parsed.data.message_id).toBe('abc123ef');
      expect(parsed.data.resolution).toBe('Done: it\'s "fixed"');
    } finally {
      try { realFs.unlinkSync(scriptPath); } catch { /* ignore */ }
      try { realFs.unlinkSync(captureFile); } catch { /* ignore */ }
      try { realFs.unlinkSync(jqMarker); } catch { /* ignore */ }
      try {
        realFs.unlinkSync(realPath.join(binDir, 'curl'));
        realFs.unlinkSync(realPath.join(binDir, 'jq'));
        realFs.rmdirSync(binDir);
      } catch { /* ignore */ }
    }
  });
});

// ── writePlaybookHelper (live token resolution) ──────────────────────────────
describe('writePlaybookHelper live token resolution', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('reads the live token file with an env fallback instead of the stale env snapshot', async () => {
    writePlaybookHelper('task-xyz');
    const script = capturedScripts['/tmp/workspace/playbook'];
    expect(script).toBeDefined();
    expect(script).toContain('_load_agent_token()');
    expect(script).toContain('TOKEN=$(cat "${AGENT_TOKEN_FILE}" 2>/dev/null)');
    expect(script).toContain('TOKEN="${AGENT_API_TOKEN:-}"');
    expect(script).not.toContain('TOKEN="${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"');
    expect(script).toContain('-H "X-Agent-Token: $TOKEN"');
    expect(script).toContain('_json_str()');
    expect(script).toContain('PAYLOAD=$(printf');
    expect(script).not.toContain('jq -n --arg tid "$TASK"');

    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const tmp = `${realOs.tmpdir().replace(/\\/g, '/')}/playbook_token_smoke_${process.pid}_${Date.now()}.sh`;
    realFs.writeFileSync(tmp, script);
    try {
      realCp.execFileSync('bash', ['-n', tmp], { stdio: 'pipe' });
    } finally {
      try { realFs.unlinkSync(tmp); } catch { /* ignore */ }
    }
  });

  // Shared harness: run `./playbook set` with jq REMOVED from PATH and a stub
  // `git` controlling whether branch/HEAD provenance is present, then return the
  // JSON body curl captured. Proves the set path builds a valid body jq-free and
  // preserves the conditional branch/head_sha keys.
  async function runPlaybookSetNoJq(gitPresent: boolean): Promise<{ status: number | null; jqCalled: boolean; body: string }> {
    writePlaybookHelper('task-nojq-pb');
    const script = capturedScripts['/tmp/workspace/playbook'];
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const tmpDir = realOs.tmpdir();
    const tag = `${gitPresent ? 'git' : 'nogit'}_${process.pid}`;
    const scriptPath = realPath.join(tmpDir, `playbook_nojq_${tag}.sh`);
    realFs.writeFileSync(scriptPath, script, { mode: 0o755 });
    const binDir = realPath.join(tmpDir, `playbook_nojq_bin_${tag}`);
    realFs.mkdirSync(binDir, { recursive: true });
    const captureFile = realPath.join(tmpDir, `playbook_nojq_cap_${tag}.json`);
    const jqMarker = realPath.join(tmpDir, `playbook_nojq_jqcalled_${tag}`);
    // _post_with_retry parses the HTTP status from the LAST line (curl -w
    // '\n%{http_code}'), so the stub must echo the body then the status code.
    realFs.writeFileSync(realPath.join(binDir, 'curl'), [
      '#!/bin/bash',
      `cat > "${captureFile}"`,
      "echo '{\"status\":\"ok\"}'",
      'echo "200"',
    ].join('\n'), { mode: 0o755 });
    realFs.writeFileSync(realPath.join(binDir, 'jq'), [
      '#!/bin/bash',
      `echo called > "${jqMarker}"`,
      'exit 1',
    ].join('\n'), { mode: 0o755 });
    // Stub git: present => emit a branch + short sha; absent => exit non-zero so
    // the `|| true` guard leaves BRANCH/HEAD_SHA empty.
    realFs.writeFileSync(realPath.join(binDir, 'git'), gitPresent
      ? ['#!/bin/bash', 'if [ "$2" = "--abbrev-ref" ]; then echo "feature/xyz"; else echo "abc1234"; fi'].join('\n')
      : ['#!/bin/bash', 'exit 1'].join('\n'), { mode: 0o755 });
    try {
      const result = realCp.spawnSync('bash', [scriptPath, 'set', 'my-key', "val with 'apos' and \"quote\"\nand newline"], {
        encoding: 'utf8',
        env: {
          ...process.env,
          ORCHESTRATOR_URL: 'http://mock',
          AGENT_API_TOKEN: 'tok',
          PATH: `${binDir}:${process.env.PATH}`,
        },
        timeout: 5000,
      });
      const jqCalled = realFs.existsSync(jqMarker);
      const body = realFs.existsSync(captureFile) ? realFs.readFileSync(captureFile, 'utf8').trim() : '';
      return { status: result.status, jqCalled, body };
    } finally {
      try { realFs.unlinkSync(scriptPath); } catch { /* ignore */ }
      try { realFs.unlinkSync(captureFile); } catch { /* ignore */ }
      try { realFs.unlinkSync(jqMarker); } catch { /* ignore */ }
      try {
        realFs.unlinkSync(realPath.join(binDir, 'curl'));
        realFs.unlinkSync(realPath.join(binDir, 'jq'));
        realFs.unlinkSync(realPath.join(binDir, 'git'));
        realFs.rmdirSync(binDir);
      } catch { /* ignore */ }
    }
  }

  it('set builds valid JSON with jq REMOVED from PATH, including branch/head_sha provenance', async () => {
    const { status, jqCalled, body } = await runPlaybookSetNoJq(true);
    expect(status).toBe(0);
    expect(jqCalled).toBe(false);
    const parsed = JSON.parse(body) as {
      task_id: string; op: string; key: string; value: string; branch?: string; head_sha?: string;
    };
    expect(parsed.task_id).toBe('task-nojq-pb');
    expect(parsed.op).toBe('set');
    expect(parsed.key).toBe('my-key');
    expect(parsed.value).toBe("val with 'apos' and \"quote\"\nand newline");
    expect(parsed.branch).toBe('feature/xyz');
    expect(parsed.head_sha).toBe('abc1234');
  });

  it('set omits branch/head_sha (non-git workspace) with jq REMOVED from PATH', async () => {
    const { status, jqCalled, body } = await runPlaybookSetNoJq(false);
    expect(status).toBe(0);
    expect(jqCalled).toBe(false);
    const parsed = JSON.parse(body) as Record<string, unknown>;
    expect(parsed.op).toBe('set');
    expect(parsed.value).toBe("val with 'apos' and \"quote\"\nand newline");
    // Conditional keys omitted exactly as the old jq `if $br == "" then {}` did.
    expect('branch' in parsed).toBe(false);
    expect('head_sha' in parsed).toBe(false);
  });
});

// ── SHARED_LIFECYCLE_REMAINDER — byte size and category coverage ──────────────
describe('SHARED_LIFECYCLE_REMAINDER — byte size and category coverage', () => {
  const CATEGORIES = [
    'correctness', 'error-handling', 'security', 'style-conventions',
    'type-safety', 'documentation', 'performance', 'architecture',
    'test-coverage', 'scope-drift', 'regression_risk', 'cross-system-contract',
  ];

  it('contains all 12 review category IDs', () => {
    for (const cat of CATEGORIES) {
      expect(SHARED_LIFECYCLE_REMAINDER).toContain(cat);
    }
  });

  it('publishes environment-limited code changes to authoritative CI without hiding real failures', () => {
    expect(SHARED_LIFECYCLE_REMAINDER).toContain('GATE-ENVIRONMENT');
    expect(SHARED_LIFECYCLE_REMAINDER).toContain('do NOT call `./blocked`');
    expect(SHARED_LIFECYCLE_REMAINDER).toContain('`./pr open`');
    expect(SHARED_LIFECYCLE_REMAINDER).toContain('`./done`');
    expect(SHARED_LIFECYCLE_REMAINDER).toContain('A real');
    expect(SHARED_LIFECYCLE_REMAINDER).toContain('test failure still blocks publication');
  });

  it('is under 4500 bytes', () => {
    const byteLen = Buffer.byteLength(SHARED_LIFECYCLE_REMAINDER, 'utf-8');
    expect(byteLen).toBeLessThan(4500);
  });
});
describe('thinker prompt omits the retired criterion-coverage retirement gate', () => {
  it('does not instruct addressing a criterion before retiring a direction (Stage-1 decoupling)', () => {
    const params: ThinkerPromptParams = {
      contextPrefix: '',
      projectName: 'Test Project',
      clockHeader: 'Clock: 2026-05-04',
      isSelfRepo: false,
      userStateLine: 'User last active: today',
      packerMetaSection: '',
      strategicContext: '',
      problemModelSection: '## Your current understanding (problem_model)\n_(empty — bootstrap)_',
      problemModelRecovery: 'empty',
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
      taskId: 'test-task-999',
      versionPhaseSection: '',
      memoCitationsSection: '',
      efficiencySnapshotSection: '',
    };
    const prompt = buildThinkerPrompt(params);
    // Stage-1 decoupling: the criterion-coverage retirement gate was retired,
    // so the thinker no longer instructs the strategist to address a criterion
    // before retiring a criterion-carrying direction. Direction retirement is a
    // pure judgment call; the acceptance contract is graded only at qualify.
    expect(prompt).not.toContain('Criterion-coverage before retirement');
    expect(prompt).not.toContain('direction_owns_uncovered_criterion');
  });
});

// ── formatSkills ─────────────────────────────────────────────────────────────
describe('formatSkills', () => {
  it('returns empty string for undefined', () => {
    expect(formatSkills(undefined)).toBe('');
  });

  it('returns empty string for empty array', () => {
    expect(formatSkills([])).toBe('');
  });

  it('renders a single skill with all fields', () => {
    const skills = [
      { slug: 'deploy', title: 'Deploy', command: 'bash deploy.sh', description: 'Deploy to staging' },
    ];
    const result = formatSkills(skills);
    expect(result).toContain('## Project Skills');
    expect(result).toContain('### Deploy');
    expect(result).toContain('Deploy to staging');
    expect(result).toContain('- Run: `skill-deploy` (or directly: `bash deploy.sh`)');
  });

  it('renders multiple skills each with their own section', () => {
    const skills = [
      { slug: 'lint', title: 'Lint', command: 'npm run lint', description: 'Run linter' },
      { slug: 'test', title: 'Test', command: 'npm test', description: 'Run tests' },
    ];
    const result = formatSkills(skills);
    expect(result).toContain('### Lint');
    expect(result).toContain('### Test');
    expect(result).toContain('skill-lint');
    expect(result).toContain('skill-test');
  });

  it('falls back to slug when title is missing', () => {
    const skills = [{ slug: 'deploy', command: 'bash deploy.sh' }] as unknown as TaskSkill[];
    const result = formatSkills(skills);
    expect(result).toContain('### deploy');
  });

  it('handles missing description gracefully', () => {
    const skills = [{ slug: 'deploy', title: 'Deploy', command: 'bash deploy.sh' }];
    const result = formatSkills(skills);
    // No description paragraph — just the title header + Run line
    expect(result).not.toContain('undefined');
    expect(result).toContain('### Deploy');
    expect(result).toContain('- Run: `skill-deploy`');
  });

  it('handles missing slug gracefully (empty string)', () => {
    const skills = [{ title: 'No slug', command: 'echo hi' }] as unknown as TaskSkill[];
    const result = formatSkills(skills);
    expect(result).toContain('### No slug');
    expect(result).toContain('- Run: `skill-`');
  });

  it('handles non-string slug gracefully', () => {
    const skills = [{ slug: 123, title: 'Bad slug', command: 'echo hi' }] as unknown as TaskSkill[];
    const result = formatSkills(skills);
    // typeof check returns '' for non-string, so slug is empty
    expect(result).toContain('### Bad slug');
    expect(result).toContain('- Run: `skill-`');
  });
});

// ── formatProjectTools ───────────────────────────────────────────────────────
describe('formatProjectTools', () => {
  it('returns empty string for undefined', () => {
    expect(formatProjectTools(undefined)).toBe('');
  });

  it('returns empty string for empty array', () => {
    expect(formatProjectTools([])).toBe('');
  });

  it('renders a notes tool with its markdown text', () => {
    const tools: TaskToolContent[] = [
      { slug: 'notes', title: 'Notes', kind: 'notes', text: '# Hypothesis\n\nThe cache is cold.' },
    ];
    const result = formatProjectTools(tools);
    expect(result).toContain('## Project Tool Content');
    expect(result).toContain('### Notes (notes)');
    expect(result).toContain('The cache is cold.');
  });

  it('renders a whiteboard summary line without dumping strokes', () => {
    const tools: TaskToolContent[] = [
      { slug: 'wb', title: 'Whiteboard', kind: 'whiteboard', text: "Whiteboard 'Whiteboard' has 12 strokes (visual canvas — not shown as text)." },
    ];
    const result = formatProjectTools(tools);
    expect(result).toContain('### Whiteboard (whiteboard)');
    expect(result).toContain('has 12 strokes');
    expect(result).not.toContain('strokes":[');
  });

  it('renders multiple tools each in their own subsection', () => {
    const tools: TaskToolContent[] = [
      { slug: 'notes', title: 'Notes', kind: 'notes', text: 'note body' },
      { slug: 'board', title: 'Board', kind: 'whiteboard', text: 'summary line' },
    ];
    const result = formatProjectTools(tools);
    expect(result).toContain('### Notes (notes)');
    expect(result).toContain('### Board (whiteboard)');
    expect(result).toContain('note body');
    expect(result).toContain('summary line');
  });

  it('skips tools whose text is empty/whitespace', () => {
    const tools: TaskToolContent[] = [
      { slug: 'empty', title: 'Empty', kind: 'notes', text: '   ' },
    ];
    expect(formatProjectTools(tools)).toBe('');
  });

  it('falls back to slug when title is missing', () => {
    const tools = [{ slug: 'notes', kind: 'notes', text: 'body' }] as unknown as TaskToolContent[];
    const result = formatProjectTools(tools);
    expect(result).toContain('### notes (notes)');
  });

  it('omits the kind parenthetical when kind is missing', () => {
    const tools = [{ slug: 'notes', title: 'Notes', text: 'body' }] as unknown as TaskToolContent[];
    const result = formatProjectTools(tools);
    expect(result).toContain('### Notes');
    expect(result).not.toContain('### Notes (');
  });
});

// ── writeSkillHelpers ────────────────────────────────────────────────────────
describe('writeSkillHelpers', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('does nothing for undefined', () => {
    writeSkillHelpers(undefined);
    expect(vi.mocked(fs.writeFileSync)).not.toHaveBeenCalled();
    expect(vi.mocked(fs.appendFileSync)).not.toHaveBeenCalled();
  });

  it('does nothing for empty array', () => {
    writeSkillHelpers([]);
    expect(vi.mocked(fs.writeFileSync)).not.toHaveBeenCalled();
    expect(vi.mocked(fs.appendFileSync)).not.toHaveBeenCalled();
  });

  it('writes an executable script for a valid skill', () => {
    const skills = [{ slug: 'deploy', title: 'Deploy', command: 'bash deploy.sh' }];
    writeSkillHelpers(skills);
    expect(vi.mocked(fs.writeFileSync)).toHaveBeenCalledTimes(1);
    const [path, content, opts] = vi.mocked(fs.writeFileSync).mock.calls[0];
    expect(path).toBe('/tmp/workspace/skill-deploy');
    expect(content).toContain('#!/bin/bash');
    expect(content).toContain('# Project skill: Deploy');
    expect(content).toContain('set -e');
    expect(content).toContain('bash deploy.sh');
    expect(opts).toEqual({ mode: 0o755 });
  });

  it('writes multiple skills', () => {
    const skills = [
      { slug: 'lint', title: 'Lint', command: 'npm run lint' },
      { slug: 'test', title: 'Test', command: 'npm test' },
    ];
    writeSkillHelpers(skills);
    expect(vi.mocked(fs.writeFileSync)).toHaveBeenCalledTimes(2);
    const paths = vi.mocked(fs.writeFileSync).mock.calls.map((c) => c[0]);
    expect(paths).toContain('/tmp/workspace/skill-lint');
    expect(paths).toContain('/tmp/workspace/skill-test');
  });

  it('skips skill with invalid slug (uppercase)', () => {
    const skills = [
      { slug: 'DEPLOY', command: 'bash deploy.sh' },
      { slug: 'valid', command: 'echo ok' },
    ];
    writeSkillHelpers(skills);
    expect(vi.mocked(fs.writeFileSync)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(fs.writeFileSync).mock.calls[0][0]).toBe('/tmp/workspace/skill-valid');
  });

  it('skips skill with leading-hyphen slug', () => {
    const skills = [{ slug: '-deploy', command: 'bash deploy.sh' }];
    writeSkillHelpers(skills);
    expect(vi.mocked(fs.writeFileSync)).not.toHaveBeenCalled();
  });

  it('skips skill with empty slug', () => {
    const skills = [{ slug: '', command: 'echo hi' }];
    writeSkillHelpers(skills);
    expect(vi.mocked(fs.writeFileSync)).not.toHaveBeenCalled();
  });

  it('skips skill with missing command', () => {
    const skills = [{ slug: 'deploy', title: 'Deploy' }];
    writeSkillHelpers(skills);
    expect(vi.mocked(fs.writeFileSync)).not.toHaveBeenCalled();
  });

  it('skips skill with non-string slug', () => {
    const skills = [
      { slug: null, command: 'echo hi' },
      { slug: 42, command: 'echo hi' },
    ];
    writeSkillHelpers(skills);
    expect(vi.mocked(fs.writeFileSync)).not.toHaveBeenCalled();
  });

  it('writes to .git/info/exclude to prevent git tracking', () => {
    const skills = [{ slug: 'deploy', command: 'bash deploy.sh' }];
    writeSkillHelpers(skills);
    // Should append /skill-* glob and /skill-deploy to exclude file
    expect(vi.mocked(fs.appendFileSync)).toHaveBeenCalled();
    const excludeCalls = vi.mocked(fs.appendFileSync).mock.calls.filter(
      (c) => String(c[0]).endsWith('.git/info/exclude'),
    );
    expect(excludeCalls.length).toBeGreaterThanOrEqual(1);
    const written = excludeCalls.map((c) => String(c[1]));
    const allText = written.join('');
    expect(allText).toContain('/skill-*');
    expect(allText).toContain('/skill-deploy');
  });

  it('does not add duplicate /skill-* glob when already excluded', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue('/skill-*\n');
    const skills = [{ slug: 'deploy', command: 'bash deploy.sh' }];
    writeSkillHelpers(skills);
    // The /skill-* glob is already present; it should not be added again.
    const excludeCalls = vi.mocked(fs.appendFileSync).mock.calls.filter(
      (c) => String(c[0]).endsWith('.git/info/exclude'),
    );
    expect(excludeCalls.length).toBe(1);
    expect(String(excludeCalls[0][1])).not.toContain('/skill-*');
    expect(String(excludeCalls[0][1])).toContain('/skill-deploy');
  });

  it('skips .git/info/exclude management when no scripts were written', () => {
    // All skills skipped → addedPatterns empty → no exclude management
    const skills = [{ slug: '', command: 'echo hi' }];
    writeSkillHelpers(skills);
    expect(vi.mocked(fs.appendFileSync)).not.toHaveBeenCalled();
  });

  it('uses provided workspace path', () => {
    const skills = [{ slug: 'deploy', command: 'bash deploy.sh' }];
    writeSkillHelpers(skills, '/custom/path');
    expect(vi.mocked(fs.writeFileSync)).toHaveBeenCalledWith(
      '/custom/path/skill-deploy',
      expect.stringContaining('bash deploy.sh'),
      { mode: 0o755 },
    );
    const excludeCalls = vi.mocked(fs.appendFileSync).mock.calls.filter(
      (c) => String(c[0]) === '/custom/path/.git/info/exclude',
    );
    expect(excludeCalls.length).toBeGreaterThanOrEqual(1);
  });

  it('handles fs write error gracefully', () => {
    vi.mocked(fs.writeFileSync).mockImplementationOnce(() => { throw new Error('ENOSPC'); });
    const skills = [{ slug: 'deploy', command: 'bash deploy.sh' }];
    expect(() => { writeSkillHelpers(skills); }).not.toThrow();
  });
});
