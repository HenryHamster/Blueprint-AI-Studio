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
  formatProjectContext,
  configureAgentPermissions,
  discoverRelatedTests,
  extractTestConventions,
  getTaskTypeHints,
  TASK_TYPE_HINTS,
  relativeTimeFromIso,
  synthesizeSuccessPatterns,
  synthesizeFailureHotspots,
  formatProjectBlindSpotSummary,
  formatFixIterationHistory,
  type FixAttempt,
} from './prompt.js';
import { getReadOnlyTaskTypes, getWriteTaskTypes } from './taskCapabilities.js';

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

