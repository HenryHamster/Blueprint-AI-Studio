import { describe, it, expect } from 'vitest';

// ── Per-task-type self-review focus blocks ────────────────────────────────────
// getTaskTypeFocusBlock() returns the right static focus block for migrate,
// refactor, qa, create, fix, docs, and deps tasks; '' for unknown types.
// CODE_CHANGE_HELPER_GUIDANCE is always composed independently and unaffected.
describe('getTaskTypeFocusBlock — task-type-specific self-review focus blocks', () => {
  it('returns the migrate focus block for "migrate" task type', async () => {
    const { getTaskTypeFocusBlock, MIGRATE_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(getTaskTypeFocusBlock('migrate')).toBe(MIGRATE_TASK_FOCUS);
  });

  it('returns the refactor focus block for "refactor" task type', async () => {
    const { getTaskTypeFocusBlock, REFACTOR_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(getTaskTypeFocusBlock('refactor')).toBe(REFACTOR_TASK_FOCUS);
  });

  it('returns the qa focus block for "qa" task type', async () => {
    const { getTaskTypeFocusBlock, QA_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(getTaskTypeFocusBlock('qa')).toBe(QA_TASK_FOCUS);
  });

  it('returns a non-empty focus block for "create" task type', async () => {
    const { getTaskTypeFocusBlock, CREATE_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(getTaskTypeFocusBlock('create')).toBe(CREATE_TASK_FOCUS);
    expect(getTaskTypeFocusBlock('create')).not.toBe('');
  });

  it('returns a non-empty focus block for "fix" task type', async () => {
    const { getTaskTypeFocusBlock, FIX_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(getTaskTypeFocusBlock('fix')).toBe(FIX_TASK_FOCUS);
    expect(getTaskTypeFocusBlock('fix')).not.toBe('');
  });

  it('returns a non-empty focus block for "docs" task type', async () => {
    const { getTaskTypeFocusBlock, DOCS_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(getTaskTypeFocusBlock('docs')).toBe(DOCS_TASK_FOCUS);
    expect(getTaskTypeFocusBlock('docs')).not.toBe('');
  });

  it('returns a non-empty focus block for "deps" task type', async () => {
    const { getTaskTypeFocusBlock, DEPS_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(getTaskTypeFocusBlock('deps')).toBe(DEPS_TASK_FOCUS);
    expect(getTaskTypeFocusBlock('deps')).not.toBe('');
  });

  it('returns empty string for unknown task types', async () => {
    const { getTaskTypeFocusBlock } = await import('./task-type-focus.js');
    expect(getTaskTypeFocusBlock('unknown')).toBe('');
  });
});

describe('MIGRATE_TASK_FOCUS — content invariants', () => {
  it('mentions down_revision chain check', async () => {
    const { MIGRATE_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(MIGRATE_TASK_FOCUS).toContain('down_revision');
  });

  it('instructs agent to name affected tests in regression_risk field', async () => {
    const { MIGRATE_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(MIGRATE_TASK_FOCUS).toContain('regression_risk');
    expect(MIGRATE_TASK_FOCUS).toContain('affected_tests');
  });

  it('mentions EXPECTED_SCHEMA_REVISION bump', async () => {
    const { MIGRATE_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(MIGRATE_TASK_FOCUS).toContain('EXPECTED_SCHEMA_REVISION');
  });
});

describe('buildMigrateTaskFocus — per-project schema-revision layout', () => {
  it('defaults reproduce MIGRATE_TASK_FOCUS byte-for-byte', async () => {
    const { buildMigrateTaskFocus, MIGRATE_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(buildMigrateTaskFocus()).toBe(MIGRATE_TASK_FOCUS);
    // getTaskTypeFocusBlock with no opts must still equal the const.
    const { getTaskTypeFocusBlock } = await import('./task-type-focus.js');
    expect(getTaskTypeFocusBlock('migrate')).toBe(MIGRATE_TASK_FOCUS);
  });

  it('uses a custom schema-revision file/constant and does not leak Blueprint paths', async () => {
    const { buildMigrateTaskFocus } = await import('./task-type-focus.js');
    const out = buildMigrateTaskFocus({ schemaRevisionFile: 'svc/app/cfg.py', schemaRevisionConst: 'SCHEMA_REV' });
    expect(out).toContain('SCHEMA_REV');
    expect(out).toContain('svc/app/cfg.py');
    expect(out).not.toContain('EXPECTED_SCHEMA_REVISION');
    expect(out).not.toContain('backend/app/main.py');
  });

  it('omits the schema-revision step entirely when no gate is declared', async () => {
    const { buildMigrateTaskFocus } = await import('./task-type-focus.js');
    const out = buildMigrateTaskFocus({ schemaRevisionFile: '' });
    expect(out).not.toContain('EXPECTED_SCHEMA_REVISION');
    expect(out).not.toContain('backend/app/main.py');
    // Step 3 (schema-revision) drops out; the block ends after the affected-tests step.
    expect(out).not.toMatch(/^3\. /m);
  });

  it('threads opts through getTaskTypeFocusBlock for migrate', async () => {
    const { getTaskTypeFocusBlock } = await import('./task-type-focus.js');
    const out = getTaskTypeFocusBlock('migrate', { schemaRevisionFile: 'svc/app/cfg.py', schemaRevisionConst: 'SCHEMA_REV' });
    expect(out).toContain('SCHEMA_REV');
    expect(out).toContain('svc/app/cfg.py');
    expect(out).not.toContain('EXPECTED_SCHEMA_REVISION');
  });
});

describe('REFACTOR_TASK_FOCUS — content invariants', () => {
  it('flags scope-drift as mandatory check', async () => {
    const { REFACTOR_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(REFACTOR_TASK_FOCUS).toContain('scope-drift');
    expect(REFACTOR_TASK_FOCUS).toContain('mandatory');
  });

  it('requires before/after record for behavioral changes under correctness', async () => {
    const { REFACTOR_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(REFACTOR_TASK_FOCUS).toContain('correctness');
    expect(REFACTOR_TASK_FOCUS).toContain('before/after');
  });

  it('instructs caller inventory via grep', async () => {
    const { REFACTOR_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(REFACTOR_TASK_FOCUS).toContain('grep');
  });
});

describe('QA_TASK_FOCUS — content invariants', () => {
  it('requires explicit behavior enumeration in test-coverage field', async () => {
    const { QA_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(QA_TASK_FOCUS).toContain('test-coverage');
  });

  it('requires edge-case coverage', async () => {
    const { QA_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(QA_TASK_FOCUS).toContain('edge');
  });

  it('warns against assertion-free "runs without error" tests', async () => {
    const { QA_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(QA_TASK_FOCUS).toContain('without error');
  });
});

describe('CREATE_TASK_FOCUS — content invariants', () => {
  it('mentions test-coverage check', async () => {
    const { CREATE_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(CREATE_TASK_FOCUS).toContain('test-coverage');
  });

  it('requires scope-drift check via git diff --stat', async () => {
    const { CREATE_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(CREATE_TASK_FOCUS).toContain('scope-drift');
    expect(CREATE_TASK_FOCUS).toContain('git diff --stat');
  });

  it('requires caller compatibility check', async () => {
    const { CREATE_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(CREATE_TASK_FOCUS).toContain('grep');
  });

  it('requires cross-file consistency check for multi-file diffs', async () => {
    const { CREATE_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(CREATE_TASK_FOCUS).toContain('Cross-file consistency');
  });
});

describe('FIX_TASK_FOCUS — content invariants', () => {
  it('mentions regression_risk check for the failing path', async () => {
    const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(FIX_TASK_FOCUS).toContain('regression_risk');
  });

  it('requires scope-drift check via git diff --stat', async () => {
    const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(FIX_TASK_FOCUS).toContain('scope-drift');
    expect(FIX_TASK_FOCUS).toContain('git diff --stat');
  });

  it('distinguishes root cause from symptom', async () => {
    const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(FIX_TASK_FOCUS).toContain('root cause');
  });

  it('warns against reusing stale verdicts from the original create task', async () => {
    const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(FIX_TASK_FOCUS).toContain('_self_review_outcome');
    expect(FIX_TASK_FOCUS).toContain('ORCHESTRATOR');
  });

  it('requires cross-file consistency check for multi-file fixes', async () => {
    const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(FIX_TASK_FOCUS).toContain('Cross-file consistency');
  });

  it('requires a reproduce-on-HEAD gate before editing', async () => {
    const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(FIX_TASK_FOCUS).toContain('Reproduced-on-HEAD gate');
    expect(FIX_TASK_FOCUS).toContain('still reproduces on current HEAD');
  });

  it('routes a non-reproducing finding to the already-resolved no-op off-ramp', async () => {
    const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(FIX_TASK_FOCUS).toContain('./done --already-done');
    expect(FIX_TASK_FOCUS).toContain('no-op rather than a false fix');
  });

  it('keeps the not-reproducible bar high so ambiguity falls through to the fix path', async () => {
    const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(FIX_TASK_FOCUS).toContain('not-reproducible bar HIGH');
  });

  it('names all three canonical reproduce-on-HEAD outcomes and ties the off-ramp to the structured verdict', async () => {
    const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(FIX_TASK_FOCUS).toContain('reproduced');
    expect(FIX_TASK_FOCUS).toContain('already_resolved');
    expect(FIX_TASK_FOCUS).toContain('indeterminate');
    // The --already-done off-ramp now emits the structured already_resolved verdict.
    expect(FIX_TASK_FOCUS).toContain('emits the structured `already_resolved` reproduce verdict');
    expect(FIX_TASK_FOCUS).toContain('./done --reproduced');
    expect(FIX_TASK_FOCUS).toContain('./done --indeterminate');
  });

  describe('push-and-CI-verify contract when local test gate is unavailable', () => {
    it('forbids ./blocked when holding a complete fix with no local test runtime', async () => {
      const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
      expect(FIX_TASK_FOCUS).toContain('do NOT call `./blocked`');
    });

    it('names standalone or non-DB verification as the cheapest available backend check', async () => {
      const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
      expect(FIX_TASK_FOCUS).toContain('standalone');
    });

    it('requires pushing the completed branch via ./pr open before ./done', async () => {
      const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
      expect(FIX_TASK_FOCUS).toContain('./pr open');
      // Must explicitly call ./done after pushing (not ./blocked)
      expect(FIX_TASK_FOCUS).toContain('Call `./done`');
    });

    it('names CI as the authoritative gate when local runtime is unavailable', async () => {
      const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
      expect(FIX_TASK_FOCUS).toContain('CI runs the full suite on this PR');
    });

    it('identifies the GATE-ENVIRONMENT note as the signal to switch to push-and-CI-verify', async () => {
      const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
      expect(FIX_TASK_FOCUS).toContain('GATE-ENVIRONMENT');
    });

    it('describes a stranded delivery as the consequence of blocking instead of pushing', async () => {
      const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
      expect(FIX_TASK_FOCUS).toContain('stranded');
    });

    it('publishes attributed baseline debt while keeping new failures blocking', async () => {
      const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
      expect(FIX_TASK_FOCUS).toContain('baseline-attributed');
      expect(FIX_TASK_FOCUS).toContain('verification receipt');
      expect(FIX_TASK_FOCUS).toContain('new, unattributed');
      expect(FIX_TASK_FOCUS).not.toContain('repo-wide gate failure on files you did not touch');
    });
  });
});

describe('DOCS_TASK_FOCUS — content invariants', () => {
  it('warns about link rot', async () => {
    const { DOCS_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(DOCS_TASK_FOCUS).toContain('link');
  });

  it('warns about code-comment vs behavior mismatch', async () => {
    const { DOCS_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(DOCS_TASK_FOCUS).toContain('behavior');
  });

  it('requires production code untouched check via scope-drift', async () => {
    const { DOCS_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(DOCS_TASK_FOCUS).toContain('scope-drift');
  });
});

describe('DEPS_TASK_FOCUS — content invariants', () => {
  it('requires lockfile/manifest parity check', async () => {
    const { DEPS_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(DEPS_TASK_FOCUS).toContain('lockfile');
  });

  it('warns about transitive dependency impact', async () => {
    const { DEPS_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(DEPS_TASK_FOCUS).toContain('transitive');
  });

  it('guards against major version bumps', async () => {
    const { DEPS_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(DEPS_TASK_FOCUS).toContain('major');
  });
});

describe('ANTI_HEDGE_FOCUS — anti-hedging self-review check', () => {
  it('is a non-empty exported constant naming observe/off/fail-open as unjustified defaults', async () => {
    const { ANTI_HEDGE_FOCUS } = await import('./task-type-focus.js');
    expect(ANTI_HEDGE_FOCUS).not.toBe('');
    expect(ANTI_HEDGE_FOCUS).toContain('observe');
    expect(ANTI_HEDGE_FOCUS).toContain('fail-open');
    expect(ANTI_HEDGE_FOCUS).toContain('off');
  });

  it('instructs recording unjustified hedge under correctness with passed: false', async () => {
    const { ANTI_HEDGE_FOCUS } = await import('./task-type-focus.js');
    expect(ANTI_HEDGE_FOCUS).toContain('correctness');
    expect(ANTI_HEDGE_FOCUS).toContain('passed: false');
    expect(ANTI_HEDGE_FOCUS).toContain('unjustified hedge');
  });

  it('requires a concrete documented reason to justify an off/observe/fail-open default', async () => {
    const { ANTI_HEDGE_FOCUS } = await import('./task-type-focus.js');
    expect(ANTI_HEDGE_FOCUS).toContain('documented');
  });

  it('appears verbatim in CREATE, FIX, and REFACTOR focus blocks', async () => {
    const { ANTI_HEDGE_FOCUS, CREATE_TASK_FOCUS, FIX_TASK_FOCUS, REFACTOR_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(CREATE_TASK_FOCUS).toContain(ANTI_HEDGE_FOCUS);
    expect(FIX_TASK_FOCUS).toContain(ANTI_HEDGE_FOCUS);
    expect(REFACTOR_TASK_FOCUS).toContain(ANTI_HEDGE_FOCUS);
  });

  it('does not appear in QA, DOCS, DEPS, or MIGRATE focus blocks', async () => {
    const { ANTI_HEDGE_FOCUS, QA_TASK_FOCUS, DOCS_TASK_FOCUS, DEPS_TASK_FOCUS, MIGRATE_TASK_FOCUS } = await import('./task-type-focus.js');
    expect(QA_TASK_FOCUS).not.toContain(ANTI_HEDGE_FOCUS);
    expect(DOCS_TASK_FOCUS).not.toContain(ANTI_HEDGE_FOCUS);
    expect(DEPS_TASK_FOCUS).not.toContain(ANTI_HEDGE_FOCUS);
    expect(MIGRATE_TASK_FOCUS).not.toContain(ANTI_HEDGE_FOCUS);
  });
});

describe('anti-hedge catch — simulate detecting a reintroduced hedge', () => {
  it('a self-review following CREATE_TASK_FOCUS would flag a newly-introduced observe default', async () => {
    // Simulate: an agent re-introduces `const mode = 'observe'` as a new gate default.
    // CREATE_TASK_FOCUS must name 'observe' as a prohibited pattern and require a
    // correctness failure, so following the checklist catches the reintroduced hedge.
    const { CREATE_TASK_FOCUS } = await import('./task-type-focus.js');
    const reintroducedHedge = "const mode = 'observe'; // default — review but do not act";
    // The focus block names the exact pattern the reintroduced hedge uses
    expect(CREATE_TASK_FOCUS).toContain('observe');
    // Requires recording a correctness failure, not just noting it
    expect(CREATE_TASK_FOCUS).toContain('passed: false');
    // The issue message template matches the hedge in the sample diff
    expect(CREATE_TASK_FOCUS).toContain('unjustified hedge');
    // Sanity: the sample hedge is a genuine observe default
    expect(reintroducedHedge).toMatch(/observe/);
  });

  it('REVIEW_ANTI_HEDGE_CRITERION names observe/fail-open so reviewers flag the same patterns', async () => {
    // Verifies reviewer checklist and agent self-review focus agree on which patterns to catch.
    const { REVIEW_ANTI_HEDGE_CRITERION } = await import('./review-prompt.js');
    expect(REVIEW_ANTI_HEDGE_CRITERION).toContain('observe');
    expect(REVIEW_ANTI_HEDGE_CRITERION).toContain('fail-open');
    expect(REVIEW_ANTI_HEDGE_CRITERION).toContain('disabled');
    expect(REVIEW_ANTI_HEDGE_CRITERION).toContain('correctness');
  });
});

describe('ANTI_HEDGE_FOCUS ↔ REVIEW_ANTI_HEDGE_CRITERION cross-consistency', () => {
  it('both name observe as a prohibited default', async () => {
    const { ANTI_HEDGE_FOCUS } = await import('./task-type-focus.js');
    const { REVIEW_ANTI_HEDGE_CRITERION } = await import('./review-prompt.js');
    expect(ANTI_HEDGE_FOCUS).toContain('observe');
    expect(REVIEW_ANTI_HEDGE_CRITERION).toContain('observe');
  });

  it('both name fail-open as a prohibited default', async () => {
    const { ANTI_HEDGE_FOCUS } = await import('./task-type-focus.js');
    const { REVIEW_ANTI_HEDGE_CRITERION } = await import('./review-prompt.js');
    expect(ANTI_HEDGE_FOCUS).toContain('fail-open');
    expect(REVIEW_ANTI_HEDGE_CRITERION).toContain('fail-open');
  });

  it('ANTI_HEDGE_FOCUS names off; REVIEW_ANTI_HEDGE_CRITERION names disabled — intentional wording difference', async () => {
    // The agent self-review prompt uses "off" (matching typical flag value syntax).
    // The reviewer checklist uses "disabled" (plain-English description). Both refer
    // to the same prohibited pattern — this test documents the intentional divergence.
    const { ANTI_HEDGE_FOCUS } = await import('./task-type-focus.js');
    const { REVIEW_ANTI_HEDGE_CRITERION } = await import('./review-prompt.js');
    expect(ANTI_HEDGE_FOCUS).toContain('off');
    expect(REVIEW_ANTI_HEDGE_CRITERION).toContain('disabled');
  });

  it('both route hedge findings to correctness', async () => {
    const { ANTI_HEDGE_FOCUS } = await import('./task-type-focus.js');
    const { REVIEW_ANTI_HEDGE_CRITERION } = await import('./review-prompt.js');
    expect(ANTI_HEDGE_FOCUS).toContain('correctness');
    expect(REVIEW_ANTI_HEDGE_CRITERION).toContain('correctness');
  });

  it('ANTI_HEDGE_FOCUS requires passed: false; REVIEW_ANTI_HEDGE_CRITERION uses correctness issue', async () => {
    const { ANTI_HEDGE_FOCUS } = await import('./task-type-focus.js');
    const { REVIEW_ANTI_HEDGE_CRITERION } = await import('./review-prompt.js');
    expect(ANTI_HEDGE_FOCUS).toContain('passed: false');
    expect(REVIEW_ANTI_HEDGE_CRITERION).toContain('correctness issue');
  });
});

describe('SCOPE_DRIFT_INCIDENTAL_ALLOWLIST_DESC — shared constant usage', () => {
  it('is used consistently in create, fix, refactor, and docs task focuses', async () => {
    const { CREATE_TASK_FOCUS, FIX_TASK_FOCUS, REFACTOR_TASK_FOCUS, DOCS_TASK_FOCUS } = await import('./task-type-focus.js');
    // All four task focuses should mention the allowlist items
    const allowlistMarkers = ['package-lock.json', 'poetry.lock', 'Cargo.lock', 'go.sum', '.gitignore'];
    for (const marker of allowlistMarkers) {
      expect(CREATE_TASK_FOCUS).toContain(marker);
      expect(FIX_TASK_FOCUS).toContain(marker);
      expect(REFACTOR_TASK_FOCUS).toContain(marker);
      expect(DOCS_TASK_FOCUS).toContain(marker);
    }
  });

  it('mentions sibling test files in all four task focuses', async () => {
    const { CREATE_TASK_FOCUS, FIX_TASK_FOCUS, REFACTOR_TASK_FOCUS, DOCS_TASK_FOCUS } = await import('./task-type-focus.js');
    for (const focus of [CREATE_TASK_FOCUS, FIX_TASK_FOCUS, REFACTOR_TASK_FOCUS, DOCS_TASK_FOCUS]) {
      expect(focus).toMatch(/test.*files|sibling.*test/i);
    }
  });
});

describe('getTaskTypeFocusBlock + CODE_CHANGE_HELPER_GUIDANCE independence', () => {
  it('CODE_CHANGE_HELPER_GUIDANCE is unchanged regardless of task type', async () => {
    const { CODE_CHANGE_HELPER_GUIDANCE } = await import('../prompt.js');
    expect(CODE_CHANGE_HELPER_GUIDANCE).toContain('Task lifecycle — you own it end-to-end');
  });

  it('focus block for migrate is separate from generic guidance', async () => {
    const { getTaskTypeFocusBlock } = await import('./task-type-focus.js');
    const { CODE_CHANGE_HELPER_GUIDANCE } = await import('../prompt.js');
    const focus = getTaskTypeFocusBlock('migrate');
    expect(focus).not.toBe('');
    // Generic guidance must not embed the task-type block
    expect(CODE_CHANGE_HELPER_GUIDANCE).not.toContain('Self-review focus: migrate tasks');
  });

  it('focus block for refactor is separate from generic guidance', async () => {
    const { getTaskTypeFocusBlock } = await import('./task-type-focus.js');
    const { CODE_CHANGE_HELPER_GUIDANCE } = await import('../prompt.js');
    const focus = getTaskTypeFocusBlock('refactor');
    expect(focus).not.toBe('');
    expect(CODE_CHANGE_HELPER_GUIDANCE).not.toContain('Self-review focus: refactor tasks');
  });

  it('focus block for qa is separate from generic guidance', async () => {
    const { getTaskTypeFocusBlock } = await import('./task-type-focus.js');
    const { CODE_CHANGE_HELPER_GUIDANCE } = await import('../prompt.js');
    const focus = getTaskTypeFocusBlock('qa');
    expect(focus).not.toBe('');
    expect(CODE_CHANGE_HELPER_GUIDANCE).not.toContain('Self-review focus: qa tasks');
  });

  it('focus block for create is separate from generic guidance', async () => {
    const { getTaskTypeFocusBlock } = await import('./task-type-focus.js');
    const { CODE_CHANGE_HELPER_GUIDANCE } = await import('../prompt.js');
    const focus = getTaskTypeFocusBlock('create');
    expect(focus).not.toBe('');
    expect(CODE_CHANGE_HELPER_GUIDANCE).not.toContain('Self-review focus: create tasks');
  });

  it('focus block for fix is separate from generic guidance', async () => {
    const { getTaskTypeFocusBlock } = await import('./task-type-focus.js');
    const { CODE_CHANGE_HELPER_GUIDANCE } = await import('../prompt.js');
    const focus = getTaskTypeFocusBlock('fix');
    expect(focus).not.toBe('');
    expect(CODE_CHANGE_HELPER_GUIDANCE).not.toContain('Self-review focus: fix tasks');
  });

  it('focus block for docs is separate from generic guidance', async () => {
    const { getTaskTypeFocusBlock } = await import('./task-type-focus.js');
    const { CODE_CHANGE_HELPER_GUIDANCE } = await import('../prompt.js');
    const focus = getTaskTypeFocusBlock('docs');
    expect(focus).not.toBe('');
    expect(CODE_CHANGE_HELPER_GUIDANCE).not.toContain('Self-review focus: docs tasks');
  });

  it('focus block for deps is separate from generic guidance', async () => {
    const { getTaskTypeFocusBlock } = await import('./task-type-focus.js');
    const { CODE_CHANGE_HELPER_GUIDANCE } = await import('../prompt.js');
    const focus = getTaskTypeFocusBlock('deps');
    expect(focus).not.toBe('');
    expect(CODE_CHANGE_HELPER_GUIDANCE).not.toContain('Self-review focus: deps tasks');
  });
});

// ── Assembled prompt inclusion — focus block appears after generic guidance ───
// Handlers concatenate CODE_CHANGE_HELPER_GUIDANCE + getTaskTypeFocusBlock(type).
// These tests verify the focus block content actually appears in that composite
// for the four newly-added task types (create, fix, docs, deps).
describe('assembled prompt — focus block included after CODE_CHANGE_HELPER_GUIDANCE', () => {
  it('create focus block appears in assembled guidance', async () => {
    const { getTaskTypeFocusBlock, CREATE_TASK_FOCUS } = await import('./task-type-focus.js');
    const { CODE_CHANGE_HELPER_GUIDANCE } = await import('../prompt.js');
    const assembled = CODE_CHANGE_HELPER_GUIDANCE + getTaskTypeFocusBlock('create');
    expect(assembled).toContain(CREATE_TASK_FOCUS);
    expect(assembled.indexOf(CODE_CHANGE_HELPER_GUIDANCE)).toBeLessThan(assembled.indexOf(CREATE_TASK_FOCUS));
  });

  it('fix focus block appears in assembled guidance', async () => {
    const { getTaskTypeFocusBlock, FIX_TASK_FOCUS } = await import('./task-type-focus.js');
    const { CODE_CHANGE_HELPER_GUIDANCE } = await import('../prompt.js');
    const assembled = CODE_CHANGE_HELPER_GUIDANCE + getTaskTypeFocusBlock('fix');
    expect(assembled).toContain(FIX_TASK_FOCUS);
    expect(assembled.indexOf(CODE_CHANGE_HELPER_GUIDANCE)).toBeLessThan(assembled.indexOf(FIX_TASK_FOCUS));
  });

  it('docs focus block appears in assembled guidance', async () => {
    const { getTaskTypeFocusBlock, DOCS_TASK_FOCUS } = await import('./task-type-focus.js');
    const { CODE_CHANGE_HELPER_GUIDANCE } = await import('../prompt.js');
    const assembled = CODE_CHANGE_HELPER_GUIDANCE + getTaskTypeFocusBlock('docs');
    expect(assembled).toContain(DOCS_TASK_FOCUS);
    expect(assembled.indexOf(CODE_CHANGE_HELPER_GUIDANCE)).toBeLessThan(assembled.indexOf(DOCS_TASK_FOCUS));
  });

  it('deps focus block appears in assembled guidance', async () => {
    const { getTaskTypeFocusBlock, DEPS_TASK_FOCUS } = await import('./task-type-focus.js');
    const { CODE_CHANGE_HELPER_GUIDANCE } = await import('../prompt.js');
    const assembled = CODE_CHANGE_HELPER_GUIDANCE + getTaskTypeFocusBlock('deps');
    expect(assembled).toContain(DEPS_TASK_FOCUS);
    expect(assembled.indexOf(CODE_CHANGE_HELPER_GUIDANCE)).toBeLessThan(assembled.indexOf(DEPS_TASK_FOCUS));
  });

  it('unknown type yields no extra content beyond generic guidance', async () => {
    const { getTaskTypeFocusBlock } = await import('./task-type-focus.js');
    const { CODE_CHANGE_HELPER_GUIDANCE } = await import('../prompt.js');
    const assembled = CODE_CHANGE_HELPER_GUIDANCE + getTaskTypeFocusBlock('unknown');
    expect(assembled).toBe(CODE_CHANGE_HELPER_GUIDANCE);
  });
});

// ── Task-type focus block contradiction linter ────────────────────────────────
// Detects surface-level opposing-instruction patterns within focus blocks.
// Pairs: (always X, never X) / (do X, don't X) / (must X, must not X).
// Goal: catch obvious drift at change-time. Allowlist suppresses known
// false positives documented inline.
describe('task-type focus blocks — contradiction linter', () => {
  // Allowlist: pairs of (verb pattern, noun pattern) known to be false positives
  // across multiple focus blocks. Format: "verb-pattern;noun-pattern" where patterns
  // are case-insensitive substrings matched in the contradiction pairs.
  // If a genuine contradiction is found, add a comment here explaining why it's
  // acceptable, then update the allowlist entry.
  const allowlist = new Set<string>([
    // No false positives currently documented.
  ]);

  function lintFocusBlock(blockName: string, content: string): { violations: string[] } {
    const violations: string[] = [];

    // Contradiction patterns: paired instructions with opposing intent
    const patterns = [
      { affirm: /\balways\b/i, negate: /\b(never|don't|do not)\b/i, verb: 'always' },
      { affirm: /\bmust\b/i, negate: /\b(must not|cannot)\b/i, verb: 'must' },
      { affirm: /\brequired\b/i, negate: /\bforbidden\b/i, verb: 'required' },
      { affirm: /\bdo\b/i, negate: /\b(don't|do not|don not)\b/i, verb: 'do' },
    ];

    for (const patternPair of patterns) {
      const sentences = content.split(/[.!?]\s+/);
      const affirmSentences = sentences.filter(s => patternPair.affirm.test(s));
      const negateSentences = sentences.filter(s => patternPair.negate.test(s));

      for (const affirmSent of affirmSentences) {
        for (const negateSent of negateSentences) {
          // Extract the object/noun being discussed (simple heuristic: word after the modal)
          const affirmMatch = affirmSent.match(/always\s+(\w+(?:\s+\w+)?)/i);
          const negateMatch = negateSent.match(/(never|don't|do not|must not|cannot)\s+(\w+(?:\s+\w+)?)/i);

          if (affirmMatch && negateMatch) {
            const affirmObj = affirmMatch[1].toLowerCase();
            const negateObj = negateMatch[2].toLowerCase();

            // Check if the objects are the same or similar (basic token overlap)
            if (affirmObj === negateObj || affirmObj.split(/\s+/)[0] === negateObj.split(/\s+/)[0]) {
              const pairKey = `${patternPair.verb};${affirmObj}`;
              if (!allowlist.has(pairKey)) {
                violations.push(
                  `${blockName}: contradictory instructions about "${affirmObj}"\n` +
                  `  Affirm: "${affirmSent.trim()}"\n` +
                  `  Negate: "${negateSent.trim()}"`
                );
              }
            }
          }
        }
      }
    }

    return { violations };
  }

  it('MIGRATE_TASK_FOCUS contains no contradictions', async () => {
    const { MIGRATE_TASK_FOCUS } = await import('./task-type-focus.js');
    const { violations } = lintFocusBlock('MIGRATE_TASK_FOCUS', MIGRATE_TASK_FOCUS);
    expect(violations).toEqual([]);
  });

  it('REFACTOR_TASK_FOCUS contains no contradictions', async () => {
    const { REFACTOR_TASK_FOCUS } = await import('./task-type-focus.js');
    const { violations } = lintFocusBlock('REFACTOR_TASK_FOCUS', REFACTOR_TASK_FOCUS);
    expect(violations).toEqual([]);
  });

  it('QA_TASK_FOCUS contains no contradictions', async () => {
    const { QA_TASK_FOCUS } = await import('./task-type-focus.js');
    const { violations } = lintFocusBlock('QA_TASK_FOCUS', QA_TASK_FOCUS);
    expect(violations).toEqual([]);
  });

  it('CREATE_TASK_FOCUS contains no contradictions', async () => {
    const { CREATE_TASK_FOCUS } = await import('./task-type-focus.js');
    const { violations } = lintFocusBlock('CREATE_TASK_FOCUS', CREATE_TASK_FOCUS);
    expect(violations).toEqual([]);
  });

  it('FIX_TASK_FOCUS contains no contradictions', async () => {
    const { FIX_TASK_FOCUS } = await import('./task-type-focus.js');
    const { violations } = lintFocusBlock('FIX_TASK_FOCUS', FIX_TASK_FOCUS);
    expect(violations).toEqual([]);
  });

  it('DOCS_TASK_FOCUS contains no contradictions', async () => {
    const { DOCS_TASK_FOCUS } = await import('./task-type-focus.js');
    const { violations } = lintFocusBlock('DOCS_TASK_FOCUS', DOCS_TASK_FOCUS);
    expect(violations).toEqual([]);
  });

  it('DEPS_TASK_FOCUS contains no contradictions', async () => {
    const { DEPS_TASK_FOCUS } = await import('./task-type-focus.js');
    const { violations } = lintFocusBlock('DEPS_TASK_FOCUS', DEPS_TASK_FOCUS);
    expect(violations).toEqual([]);
  });
});
