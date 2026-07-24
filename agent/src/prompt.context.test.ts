import { vi, describe, it, expect, beforeEach } from 'vitest';
import fs from 'fs';

// ── Module mocks ──────────────────────────────────────────────────────────────
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
  buildRetryContext,
  buildAnalysisPrompt,
  parseAnalysisPlanFiles,
  parseAnalysisPlanTestFiles,
  parseAnalysisPlanVerifyFiles,
  extractModuleInterfaces,
  discoverAffectedConsumers,
  formatProvenanceWarnings,
  formatInvestigateCitationProvenance,
  formatAcceptanceEvidence,
} from './prompt.js';

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
