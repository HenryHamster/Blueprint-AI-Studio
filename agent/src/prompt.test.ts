import { vi, describe, it, expect, beforeEach } from 'vitest';
import fs from 'fs';

// ── Module mocks ──────────────────────────────────────────────────────────────
// Mock ./index.js entirely so that importing prompt.ts does NOT trigger main().
vi.mock('./index.js', () => ({
  sendLog: vi.fn(),
  WORKSPACE: '/tmp/workspace',
  // prompt.ts has its own local strVal; this export is only needed by callers
  // that import strVal from index — not by prompt.ts itself.
  strVal: (v: unknown, fallback = '') => (typeof v === 'string' ? v : fallback),
  getAgentToken: vi.fn().mockReturnValue(''),
  git: vi.fn().mockReturnValue(''),
  sanitize: (s: string) => s,
}));

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
  buildSelfReviewPrompt,
  extractModuleInterfaces,
  discoverAffectedConsumers,
} from './prompt.js';

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
        failed_approaches: ['JWT in cookies', 'Session-based auth'],
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
    expect(result).toContain('- JWT in cookies');
    expect(result).toContain('- Session-based auth');
    expect(result).toContain('Recent cycles:');
    expect(result).toContain('2/3 merged');
    expect(result).toContain('1/4 merged');
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
    expect(result).toContain('**Failure pattern:**');
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
        { status: 'IN_PROGRESS', title: 'Write documentation', related_files: [] },
      ],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('Sibling Tasks');
    expect(result).toContain('Write documentation');
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
});

// ── configureAgentPermissions ─────────────────────────────────────────────────
describe('configureAgentPermissions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const READ_ONLY_TYPES = [
    'research',
    'plan',
    'review',
    'security_audit',
    'smoketest',
    'debug',
    'perf',
    'investigate',
    'design_doc_update',
    'strategic_review',
  ] as const;

  const WRITE_TYPES = [
    'create',
    'fix',
    'refactor',
    'docs',
    'qa',
    'deps',
    'migrate',
  ] as const;

  it.each(READ_ONLY_TYPES)(
    'returns [Write, Edit, NotebookEdit] for read-only type "%s"',
    (taskType) => {
      const result = configureAgentPermissions(taskType);
      expect(result).toEqual(['Write', 'Edit', 'NotebookEdit']);
    },
  );

  it.each(WRITE_TYPES)(
    'returns [] (no restrictions) for write type "%s"',
    (taskType) => {
      const result = configureAgentPermissions(taskType);
      expect(result).toEqual([]);
    },
  );

  it('returns [] for an unknown task type', () => {
    expect(configureAgentPermissions('unknown_type')).toEqual([]);
  });
});

// ── getTaskTypeHints ──────────────────────────────────────────────────────────
describe('getTaskTypeHints', () => {
  it('returns static hint from TASK_TYPE_HINTS when buildConfig is undefined', () => {
    const result = getTaskTypeHints(undefined)['create'];
    expect(result).toBe(TASK_TYPE_HINTS['create']);
    expect(result).toContain('Run the relevant lint');
  });

  it('returns undefined for unknown task type when buildConfig is undefined', () => {
    const result = getTaskTypeHints(undefined)['unknown_type'];
    expect(result).toBeUndefined();
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
    expect(result).not.toMatch(/  - src\/foo\.ts\n/);
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
});

describe('buildSelfReviewPrompt', () => {
  const baseOpts = {
    diff: 'diff --git a/foo.ts b/foo.ts',
    reviewPlaybook: {},
    claudeMd: '',
  };

  it('includes default build commands when buildConfig is absent', () => {
    const result = buildSelfReviewPrompt(baseOpts);
    expect(result).toContain('## Build Verification');
    expect(result).toContain('npm run build');
    expect(result).toContain('tsc --noEmit');
    expect(result).toContain('py_compile');
  });

  it('includes default build commands when buildConfig has empty directories', () => {
    const result = buildSelfReviewPrompt({ ...baseOpts, buildConfig: { directories: [] } });
    expect(result).toContain('## Build Verification');
    expect(result).toContain('npm run build');
  });

  it('uses buildConfig commands when directories have build_cmd', () => {
    const result = buildSelfReviewPrompt({
      ...baseOpts,
      buildConfig: {
        directories: [{ path: 'frontend', type: 'node', build_cmd: 'npm run build', lint_cmd: '' }],
      },
    });
    expect(result).toContain('## Build Verification');
    expect(result).toContain('`npm run build`');
    expect(result).toContain('frontend/');
    // should NOT contain default fallback text
    expect(result).not.toContain('admin-ui');
  });

  it('uses lint_cmd when build_cmd is absent', () => {
    const result = buildSelfReviewPrompt({
      ...baseOpts,
      buildConfig: {
        directories: [{ path: 'backend', type: 'python', build_cmd: '', lint_cmd: 'flake8 .' }],
      },
    });
    expect(result).toContain('`flake8 .`');
  });

  it('omits build verification section when all directories lack both build_cmd and lint_cmd', () => {
    const result = buildSelfReviewPrompt({
      ...baseOpts,
      buildConfig: {
        directories: [{ path: 'docs', type: 'node', build_cmd: '', lint_cmd: '' }],
      },
    });
    expect(result).not.toContain('## Build Verification');
  });

  it('positions Build Verification before the Self-Review Instructions checklist', () => {
    const result = buildSelfReviewPrompt(baseOpts);
    const buildPos = result.indexOf('## Build Verification');
    const checklistPos = result.indexOf('## Self-Review Instructions');
    expect(buildPos).toBeGreaterThan(-1);
    expect(buildPos).toBeLessThan(checklistPos);
  });

  it('instructs agent to run commands before the checklist', () => {
    const result = buildSelfReviewPrompt(baseOpts);
    expect(result).toContain('fix the errors before proceeding with the review checklist');
  });

  it('renders project-specific review patterns when reviewIssueDistribution is provided', () => {
    const result = buildSelfReviewPrompt({
      ...baseOpts,
      reviewIssueDistribution: { 'test-coverage': 0.35, 'type-safety': 0.25, 'error-handling': 0.20 },
    });
    expect(result).toContain('## Project-Specific Review Patterns');
    expect(result).toContain('test-coverage (35%)');
    expect(result).toContain('type-safety (25%)');
    expect(result).toContain('error-handling (20%)');
    expect(result).toContain('Prioritize these checks');
  });

  it('sorts review issue distribution by frequency descending', () => {
    const result = buildSelfReviewPrompt({
      ...baseOpts,
      reviewIssueDistribution: { 'error-handling': 0.10, 'test-coverage': 0.40, 'type-safety': 0.20 },
    });
    const patternsSection = result.slice(result.indexOf('## Project-Specific Review Patterns'));
    const testCoveragePos = patternsSection.indexOf('test-coverage');
    const typeSafetyPos = patternsSection.indexOf('type-safety');
    const errorHandlingPos = patternsSection.indexOf('error-handling');
    expect(testCoveragePos).toBeLessThan(typeSafetyPos);
    expect(typeSafetyPos).toBeLessThan(errorHandlingPos);
  });

  it('omits project-specific review patterns when reviewIssueDistribution is undefined', () => {
    const result = buildSelfReviewPrompt(baseOpts);
    expect(result).not.toContain('## Project-Specific Review Patterns');
  });

  it('omits project-specific review patterns when reviewIssueDistribution is empty', () => {
    const result = buildSelfReviewPrompt({ ...baseOpts, reviewIssueDistribution: {} });
    expect(result).not.toContain('## Project-Specific Review Patterns');
  });
});

// ── buildSelfReviewChecklist (via buildSelfReviewPrompt) ─────────────────────
describe('buildSelfReviewChecklist', () => {
  const baseOpts = {
    diff: 'diff --git a/foo.ts b/foo.ts',
    reviewPlaybook: {},
    claudeMd: '',
  };

  it('taskScope=small produces a 5-item checklist with severity guide', () => {
    const result = buildSelfReviewPrompt({ ...baseOpts, taskScope: 'small' });
    expect(result).toContain('Correctness');
    expect(result).toContain('Missing test coverage');
    expect(result).toContain('Error handling');
    expect(result).toContain('Security quick-check');
    expect(result).toContain('Import and type verification');
    expect(result).toContain('CRITICAL');
    expect(result).toContain('MAJOR');
    expect(result).not.toContain('Type safety violations');
    expect(result).not.toContain('Scope violations');
    expect(result).not.toContain('Pattern violations');
  });

  it('taskScope=large produces a 9-item comprehensive checklist', () => {
    const result = buildSelfReviewPrompt({ ...baseOpts, taskScope: 'large' });
    expect(result).toContain('Missing test coverage');
    expect(result).toContain('Type safety violations');
    expect(result).toContain('Scope violations');
    expect(result).toContain('Security issues');
    expect(result).toContain('Pattern violations');
    expect(result).toContain('Cross-file consistency');
    expect(result).toContain('Backwards compatibility');
    expect(result).toContain('Data model safety');
    expect(result).toContain('Import/dependency verification');
  });

  it('taskScope=undefined produces the standard 8-item checklist', () => {
    const result = buildSelfReviewPrompt({ ...baseOpts, taskScope: undefined });
    expect(result).toContain('Missing test coverage');
    expect(result).toContain('Type safety violations');
    expect(result).toContain('Scope violations');
    expect(result).toContain('Security issues');
    expect(result).toContain('Pattern violations');
    expect(result).toContain('Cross-file consistency');
    expect(result).toContain('Backwards compatibility');
    expect(result).toContain('Import/dependency verification');
    expect(result).not.toContain('Data model safety');
    expect(result).not.toContain('Correctness');
  });

  it('taskScope=medium (explicit) produces the same 8-item checklist as undefined', () => {
    const resultUndefined = buildSelfReviewPrompt({ ...baseOpts, taskScope: undefined });
    const resultMedium = buildSelfReviewPrompt({ ...baseOpts, taskScope: 'medium' });
    expect(resultMedium).toContain('Missing test coverage');
    expect(resultMedium).toContain('Type safety violations');
    expect(resultMedium).toContain('Scope violations');
    expect(resultMedium).toContain('Security issues');
    expect(resultMedium).toContain('Pattern violations');
    expect(resultMedium).toContain('Cross-file consistency');
    expect(resultMedium).toContain('Backwards compatibility');
    expect(resultMedium).toContain('Import/dependency verification');
    expect(resultMedium).not.toContain('Correctness');
    // medium and undefined produce identical checklist output
    expect(resultMedium).toBe(resultUndefined);
  });
});

// ── extractModuleInterfaces ───────────────────────────────────────────────────
describe('extractModuleInterfaces', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Re-establish defaults so overrides from prior tests don't bleed through
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue('');
    vi.mocked(execFileSync).mockReturnValue(Buffer.from('') as unknown as ReturnType<typeof execFileSync>);
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
      'agent/src/handlers/standard.ts\nagent/src/index.ts\n' as unknown as ReturnType<typeof execFileSync>
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
      .mockReturnValueOnce('backend/app/services.py\n' as unknown as ReturnType<typeof execFileSync>)
      .mockReturnValueOnce('' as unknown as ReturnType<typeof execFileSync>);
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
      .mockReturnValueOnce('other/caller.py\n' as unknown as ReturnType<typeof execFileSync>)
      .mockReturnValueOnce('' as unknown as ReturnType<typeof execFileSync>);
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
      .mockReturnValueOnce('backend/app/services.py\nbackend/app/admin.py\n' as unknown as ReturnType<typeof execFileSync>)
      .mockReturnValueOnce('backend/app/admin.py\n' as unknown as ReturnType<typeof execFileSync>)
      .mockReturnValueOnce('' as unknown as ReturnType<typeof execFileSync>);
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
    vi.mocked(execFileSync).mockReturnValue(Buffer.from('') as unknown as ReturnType<typeof execFileSync>);
  });

  it('returns empty string when relatedFiles is empty', () => {
    expect(discoverAffectedConsumers(null, [])).toBe('');
  });

  it('skips test files in relatedFiles', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/handlers/standard.ts\n' as unknown as ReturnType<typeof execFileSync>
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
      'agent/src/handlers/standard.ts\n' as unknown as ReturnType<typeof execFileSync>
    );
    // existsSync returns false by default — consumer file cannot be read
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toBe('');
  });

  it('filters out test files returned by git grep', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/prompt.test.ts\nbackend/testing/test_models.py\n' as unknown as ReturnType<typeof execFileSync>
    );
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toBe('');
  });

  it('extracts named TypeScript imports from consumer files', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/handlers/standard.ts\n' as unknown as ReturnType<typeof execFileSync>
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

  it('extracts Python imports from consumer files', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'backend/app/orchestrator.py\n' as unknown as ReturnType<typeof execFileSync>
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
      'backend/app/main.py\n' as unknown as ReturnType<typeof execFileSync>
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
      'agent/src/index.ts\n' as unknown as ReturnType<typeof execFileSync>
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
      manyConsumers as unknown as ReturnType<typeof execFileSync>
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
