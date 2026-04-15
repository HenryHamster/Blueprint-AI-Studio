import { vi, describe, it, expect, beforeEach } from 'vitest';

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
import {
  formatPlaybook,
  buildContextPrefix,
  formatProjectContext,
  configureAgentPermissions,
  discoverRelatedTests,
  getTaskTypeHints,
  TASK_TYPE_HINTS,
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
});
