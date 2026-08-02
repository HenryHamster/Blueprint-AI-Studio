import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { formatProjectContext } from './project-context.js';

const __dirname = dirname(fileURLToPath(import.meta.url));

describe('formatProjectContext — Self-Review False-Positive Calibration', () => {
  it('renders the FP calibration block when a category exceeds the FP threshold and has adequate sample size', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          security: {
            miss_rate: 0.05,
            false_positive_rate: 0.8,
            precision: 0.2,
            sr_flag_sample_size: 15,
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('### Self-Review False-Positive Calibration');
    expect(result).toContain('security');
    expect(result).toContain('80% false-positive');
    // Verbose phrasing must explicitly tell the agent NOT to weaken genuine catches
    expect(result).toContain('do not weaken genuine catches');
  });

  it('omits the FP calibration block when no category meets the threshold (low sample size)', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          security: {
            miss_rate: 0.05,
            false_positive_rate: 0.9,
            sr_flag_sample_size: 5, // below 10
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Self-Review False-Positive Calibration');
  });

  it('omits the FP calibration block when FP rate is below threshold', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          security: {
            miss_rate: 0.10,
            false_positive_rate: 0.3, // below 0.5
            sr_flag_sample_size: 20,
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Self-Review False-Positive Calibration');
  });

  it('omits the FP calibration block when self_review_catch_rate is absent', () => {
    const ctx = {};
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Self-Review False-Positive Calibration');
  });

  it('renders blind-spots and FP-calibration blocks side-by-side when both apply (regression check)', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          security: {
            miss_rate: 0.05,
            false_positive_rate: 0.8,
            sr_flag_sample_size: 15,
          },
          'error-handling': {
            miss_rate: 0.6,
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('### Self-Review Blind Spots');
    expect(result).toContain('### Self-Review False-Positive Calibration');
    // Calibration block must come AFTER blind-spots block
    const blindIdx = result.indexOf('### Self-Review Blind Spots');
    const fpIdx = result.indexOf('### Self-Review False-Positive Calibration');
    expect(blindIdx).toBeLessThan(fpIdx);
  });

  it('prefers for_task_type over categories and tags scope note', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          security: {
            miss_rate: 0.10,
            false_positive_rate: 0.6,
            sr_flag_sample_size: 12,
          },
        },
        for_task_type: {
          'type-safety': {
            miss_rate: 0.05,
            false_positive_rate: 0.85,
            sr_flag_sample_size: 20,
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('### Self-Review False-Positive Calibration');
    expect(result).toContain('type-safety');
    expect(result).toContain('(create tasks only)');
    // The category-level entry must NOT appear when for_task_type is preferred.
    expect(result).not.toContain('**security**: flagged');
  });

  it('instructs agents to add reasoning for regressing blind-spot categories', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          correctness: { miss_rate: 0.4 },
          'test-coverage': { miss_rate: 0.3 },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('### Self-Review Blind Spots');
    // Must tell the agent to add a reasoning field for these categories
    expect(result).toContain('reasoning');
    expect(result).toContain('check_results');
    // Instruction must cover both pass and fail outcomes
    expect(result).toContain('pass and fail');
  });

  it('caps the FP calibration block to top-5 categories by FP rate (regression check)', () => {
    const cats: Record<string, { miss_rate: number; false_positive_rate: number; sr_flag_sample_size: number }> = {};
    for (let i = 0; i < 8; i++) {
      cats[`cat-${i}`] = {
        miss_rate: 0.05,
        false_positive_rate: 0.5 + i * 0.05, // 0.50..0.85
        sr_flag_sample_size: 12,
      };
    }
    const ctx = { self_review_catch_rate: { categories: cats } };
    const result = formatProjectContext(ctx, 'create');
    // Highest FP rate categories (cat-7..cat-3) should appear; the lowest (cat-0, cat-1, cat-2) excluded.
    expect(result).toContain('cat-7');
    expect(result).toContain('cat-3');
    expect(result).not.toContain('**cat-0**');
    expect(result).not.toContain('**cat-1**');
    expect(result).not.toContain('**cat-2**');
  });
});

describe('formatProjectContext — SR Recent Miss Examples', () => {
  it('renders section from sr_recent_miss_examples (backend contract)', () => {
    // Shape mirrors what backend/app/routers/agent.py emits as
    // response["project_context"]["sr_recent_miss_examples"].
    const ctx = {
      sr_recent_miss_examples: {
        security: [{ severity: 'CRITICAL', desc: 'SQL injection via user input' }],
        correctness: [{ severity: 'HIGH', desc: 'missed null check' }],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('## Concrete misses from the last cycle');
    expect(result).toContain('**security:**');
    expect(result).toContain('[CRITICAL] SQL injection via user input');
    expect(result).toContain('**correctness:**');
    expect(result).toContain('[HIGH] missed null check');
  });

  it('omits the section when sr_recent_miss_examples is absent or has no examples', () => {
    expect(formatProjectContext({}, 'create')).not.toContain('Concrete misses from the last cycle');
    expect(formatProjectContext({ sr_recent_miss_examples: {} }, 'create')).not.toContain(
      'Concrete misses from the last cycle',
    );
    expect(
      formatProjectContext({ sr_recent_miss_examples: { correctness: [] } }, 'create'),
    ).not.toContain('Concrete misses from the last cycle');
  });

  it('renders the canonical contract fixture (cross-language contract with backend)', () => {
    // Loads the same fixture that backend/testing/test_agent.py asserts the claim
    // endpoint produces. If either side drifts from this shape, both tests must update.
    const fixturePath = join(__dirname, '__fixtures__', 'sr_recent_miss_examples_contract.json');
    const fixture = JSON.parse(readFileSync(fixturePath, 'utf8')) as {
      sr_recent_miss_examples: Record<string, Array<{ severity: string; desc: string }>>;
    };
    const ctx = { sr_recent_miss_examples: fixture.sr_recent_miss_examples };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('## Concrete misses from the last cycle');
    expect(result).toContain('**correctness:**');
    expect(result).toContain('[CRITICAL] Off-by-one in loop boundary check');
    expect(result).toContain('**security:**');
    expect(result).toContain('[MAJOR] User input passed directly to SQL query');
  });

  it('does NOT render from the legacy sr_catch_rate_trends shape (no key/shape collision)', () => {
    // Guards against contract regression — the trends doc shape must not be misread as miss examples.
    const ctx = {
      sr_catch_rate_trends: {
        categories: {
          security: {
            miss_rate: 0.9,
            examples: [{ severity: 'CRITICAL', desc: 'should not render' }],
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Concrete misses from the last cycle');
    expect(result).not.toContain('should not render');
  });
});

describe('formatProjectContext — landed on HEAD marker', () => {
  it('renders [landed on HEAD] for completed tasks with landed_on_head=true', () => {
    const ctx = {
      recently_completed_tasks: [
        {
          id: 'task-1',
          title: 'Fix the auth bug',
          task_type: 'fix',
          status: 'COMPLETED',
          landed_on_head: true,
          pr_url: 'https://github.com/org/repo/pull/42',
        },
      ],
    };
    const result = formatProjectContext(ctx, 'research');
    expect(result).toContain('[landed on HEAD]');
    expect(result).toContain('Fix the auth bug');
  });

  it('does NOT render [landed on HEAD] for completed tasks with landed_on_head=false', () => {
    const ctx = {
      recently_completed_tasks: [
        {
          id: 'task-2',
          title: 'Add feature X',
          task_type: 'create',
          status: 'COMPLETED',
          landed_on_head: false,
        },
      ],
    };
    const result = formatProjectContext(ctx, 'research');
    expect(result).not.toContain('[landed on HEAD]');
    expect(result).toContain('Add feature X');
  });

  it('does NOT render [landed on HEAD] when landed_on_head is absent', () => {
    const ctx = {
      recently_completed_tasks: [
        {
          id: 'task-3',
          title: 'Old task without field',
          task_type: 'fix',
          status: 'COMPLETED',
        },
      ],
    };
    const result = formatProjectContext(ctx, 'research');
    expect(result).not.toContain('[landed on HEAD]');
  });
});

describe('formatProjectContext — BLOCKED active tasks', () => {
  it('renders BLOCKED tasks in active tasks section', () => {
    const ctx = {
      active_tasks: [
        {
          id: 'task-b1',
          title: 'Implement feature Y',
          task_type: 'create',
          status: 'BLOCKED',
          prompt: 'Implement the Y feature for users',
          related_files: [],
        },
      ],
    };
    const result = formatProjectContext(ctx, 'research');
    expect(result).toContain('Implement feature Y');
    expect(result).toContain('BLOCKED');
  });

  it('adds BLOCKED framing note when BLOCKED tasks are present', () => {
    const ctx = {
      active_tasks: [
        {
          id: 'task-b2',
          title: 'Stalled fix task',
          task_type: 'fix',
          status: 'BLOCKED',
          prompt: 'Fix the stalled thing',
          related_files: [],
        },
      ],
    };
    const result = formatProjectContext(ctx, 'research');
    expect(result).toContain('NOTE: Tasks marked BLOCKED');
    expect(result).toContain('do NOT propose findings');
  });

  it('does NOT add BLOCKED framing note when no BLOCKED tasks are present', () => {
    const ctx = {
      active_tasks: [
        {
          id: 'task-a1',
          title: 'Active in-progress task',
          task_type: 'create',
          status: 'IN_PROGRESS',
          prompt: 'Do active work',
          related_files: [],
        },
      ],
    };
    const result = formatProjectContext(ctx, 'research');
    expect(result).not.toContain('NOTE: Tasks marked BLOCKED');
  });
});

describe('formatProjectContext — suppressTaskLists option', () => {
  const ctxWithLists = {
    active_tasks: [
      { task_type: 'fix', title: 'Active fix', status: 'IN_PROGRESS', prompt: 'Fix bug' },
    ],
    queued_tasks: [
      { task_type: 'create', title: 'Queued create' },
    ],
    direction_recent_tasks: [
      { task_type: 'refactor', title: 'Recent refactor', pr_url: 'https://github.com/x/pull/1' },
    ],
    recently_completed_tasks: [
      { task_type: 'docs', title: 'Docs task', pr_url: 'https://github.com/x/pull/2' },
    ],
    sibling_tasks: [
      { status: 'QUEUED', title: 'Sibling', active: false },
    ],
  };

  it('includes all task list sections without suppressTaskLists', () => {
    const result = formatProjectContext(ctxWithLists);
    expect(result).toContain('### Active Tasks');
    expect(result).toContain('### Queued Tasks');
    expect(result).toContain('### Recent Tasks in This Direction');
    expect(result).toContain('### Recently Completed Tasks');
  });

  it('omits active/queued/direction-recent/recently-completed when suppressTaskLists is true', () => {
    const result = formatProjectContext(ctxWithLists, '', '', '', 0, { suppressTaskLists: true });
    expect(result).not.toContain('### Active Tasks');
    expect(result).not.toContain('### Queued Tasks');
    expect(result).not.toContain('### Recent Tasks in This Direction');
    expect(result).not.toContain('### Recently Completed Tasks');
    // sibling tasks remain
    expect(result).toContain('## Sibling Tasks');
  });
});

describe('formatProjectContext — lesson dedup', () => {
  it('collapses lessons with identical last_feedback to a single entry', () => {
    const ctx = {
      lessons: [
        { task_title: 'Task A', iterations: 2, last_feedback: 'Add error handling', failure_class: 'error-handling' },
        { task_title: 'Task B', iterations: 1, last_feedback: 'Add error handling', failure_class: 'error-handling' },
        { task_title: 'Task C', iterations: 3, last_feedback: 'Different lesson', failure_class: 'style' },
      ],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('### Lessons from Past Reviews');
    // 'Add error handling' should appear only once
    const addErrorCount = (result.match(/Add error handling/g) || []).length;
    expect(addErrorCount).toBe(1);
    expect(result).toContain('Different lesson');
  });

  it('collapses lessons with same task_title and failure_class', () => {
    const ctx = {
      lessons: [
        { task_title: 'Feature X', iterations: 1, last_feedback: 'Feedback one', failure_class: 'correctness' },
        { task_title: 'Feature X', iterations: 2, last_feedback: 'Feedback two', failure_class: 'correctness' },
        { task_title: 'Feature Y', iterations: 1, last_feedback: 'Feedback three', failure_class: 'correctness' },
      ],
    };
    const result = formatProjectContext(ctx);
    // Feature X with correctness: only first should appear
    expect(result).toContain('Feature X');
    expect(result).toContain('Feedback one');
    expect(result).not.toContain('Feedback two');
    expect(result).toContain('Feature Y');
  });

  it('does not dedup lessons when they are genuinely distinct', () => {
    const ctx = {
      lessons: [
        { task_title: 'Task A', iterations: 1, last_feedback: 'First lesson here', failure_class: 'correctness' },
        { task_title: 'Task B', iterations: 1, last_feedback: 'Second lesson here', failure_class: 'style' },
      ],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('First lesson here');
    expect(result).toContain('Second lesson here');
  });
});
