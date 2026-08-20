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
import {
  getTaskTypeHints,
  TASK_TYPE_HINTS,
  TASK_TYPES_WITHOUT_HINT,
  VIEWPORT_SNAPSHOT_HINT,
  buildViewportScreenshotSection,
  formatSelfReviewBlindSpots,
  formatSelfReviewCalibrationSection,
  writeMessagesHelper,
  writeDesignDocHelper,
  writePlaybookHelper,
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
  BUILD_WITH_INTENT_GUIDANCE,
  BUILD_ORDER_GUIDANCE,
  READ_ONLY_BASH_WRITE_NOTE,
  SELF_REVIEW_PROMPT_VERSION,
  SHARED_LIFECYCLE_REMAINDER,
  assembleFailureGuidanceBlocks,
  formatSkills,
  formatProjectTools,
  writeSkillHelpers,
  type TaskSkill,
  type TaskToolContent,
} from './prompt.js';
import { handlers } from './handlers/index.js';
import { buildThinkerPrompt, type ThinkerPromptParams } from './prompts/thinker.js';

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

  it('coexists with truncated_files_read sections', () => {
    const outcome = {
      verdict: 'PASS',
      truncated_files_read: ['a.ts'],
      blind_spots: ['author-uncertainty'],
    };
    const out = formatSelfReviewBlindSpots(outcome, ['a.ts', 'b.ts']);
    expect(out).toContain('Files the self-review verified');
    expect(out).toContain('Files NOT verified by self-review');
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
// the former independent-review inputs were consolidated into author self-review.
// If this test fails, bump the version in prompt.ts and reset this assertion.
describe('SELF_REVIEW_PROMPT_VERSION — regime version lock', () => {
  it('is "9" (current pinned version)', () => {
    expect(SELF_REVIEW_PROMPT_VERSION).toBe('9');
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

  it('appends the build-discipline blocks after EDGES (intent then order)', () => {
    const blocks = assembleFailureGuidanceBlocks();
    expect(blocks).toContain('Build With Intent: Decide Before You Draft'); // BUILD_WITH_INTENT_GUIDANCE
    expect(blocks).toContain('Build Order: Design');                        // BUILD_ORDER_GUIDANCE
    const ecIdx  = blocks.indexOf('Edges-Considered Field in Self-Review');
    const bwiIdx = blocks.indexOf('Build With Intent: Decide Before You Draft');
    const boIdx  = blocks.indexOf('Build Order: Design');
    expect(ecIdx).toBeLessThan(bwiIdx);
    expect(bwiIdx).toBeLessThan(boIdx);
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
    // The viewport lane no longer shells out to `npm run test:viewport` (which
    // chained the whole unit suite ahead of the browser run); it routes through
    // the shared per-identity attributor instead.
    expect(intro).not.toContain('npm run test:viewport');
    expect(intro).toContain('scripts/vitest_verification_guard.py');
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

  it('build_config variant explains retained verification only when a pytest test_cmd is configured', () => {
    // FOREIGN_BUILD_CONFIG carries `api` with `pytest -q` → ./check keeps the
    // configured command and uses the retained verifier only when explicitly
    // declared, so the intro must describe that contract accurately.
    const g = buildCodeChangeHelperGuidance({ buildConfig: FOREIGN_BUILD_CONFIG, ...FOREIGN });
    expect(checkIntroOf(g)).toContain('configured command under');
    expect(checkIntroOf(g)).toContain('blueprint-v1');
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

  it('fingerprints failure guidance only when the caller actually emits it', () => {
    const withoutFailureGuidance = computePromptFingerprint('perf');
    const withFailureGuidance = computePromptFingerprint('perf', undefined, {
      emittedFailureGuidance: BUILD_WITH_INTENT_GUIDANCE + BUILD_ORDER_GUIDANCE,
    });
    expect(withFailureGuidance).not.toBe(withoutFailureGuidance);
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
    // POST body shape the backend op handler parses must use json.dumps for all fields.
    // set: task_id, key, value all passed via sys.argv to avoid printf escape issues.
    expect(script).toContain('"op":"set"');
    expect(script).toContain('"op":"delete"');
    // Payload building uses python3 sys.argv, not printf %s substitution.
    expect(script).toContain('python3 -c');
    expect(script).toContain('json.dumps');
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

  it('design-doc payload building: python3 sys.argv passes KEY, TASK, VALUE safely — no printf escape layer', () => {
    writeDesignDocHelper('task-xyz');
    const script = capturedScripts['/tmp/workspace/design-doc'];
    // No printf format-string substitution for payload building — the old pattern
    // broke when _json_str output contained '%' chars that printf re-interpreted.
    expect(script).not.toContain('printf \'{"task_id"');
    // No _json_str in the generated script: replaced by python3 sys.argv passing.
    expect(script).not.toContain('_json_str()');
    expect(script).not.toContain('$(_json_str "$KEY")');
    expect(script).not.toContain('$(_json_str "$TASK")');
    expect(script).not.toContain('$(_json_str "$VALUE")');
    // All ops use python3 -c with sys.argv to route KEY, TASK, VALUE safely.
    expect(script).toContain('python3 -c \'import json,sys');
    // VALUE in set is parsed as JSON via json.loads (validates format, safe from quoting).
    expect(script).toContain('json.loads(v)');
    // python3 availability check so failures are caught at invocation, not deep in a curl call.
    expect(script).toContain('command -v python3 >/dev/null 2>&1');
  });

  it('keeps read ops (list/get) fail-fast with no retry', () => {
    writeDesignDocHelper('task-xyz');
    const script = capturedScripts['/tmp/workspace/design-doc'];
    expect(script).toContain('_post_once()');
    // list and get route through the single-shot helper, not the retry loop.
    expect(script).toContain(`python3 -c 'import json,sys;sys.stdout.write(json.dumps({"task_id":sys.argv[1],"op":"list"}))' "$TASK" | _post_once`);
    expect(script).toContain(`python3 -c 'import json,sys;t,k=sys.argv[1],sys.argv[2];sys.stdout.write(json.dumps({"task_id":t,"op":"get","key":k}))' "$TASK" "$KEY" | _post_once`);
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

  // ── Payload safety: double-quotes, backslashes, newlines in KEY and VALUE ──────
  // These tests execute the python3 one-liners extracted from the generated script
  // to verify they handle adversarial inputs without producing invalid JSON.
  describe('python3 payload builder: adversarial KEY and VALUE inputs', () => {
    let capturedScript: string;

    beforeEach(() => {
      writeDesignDocHelper('task-xyz');
      capturedScript = capturedScripts['/tmp/workspace/design-doc'];
    });

    async function runPy3(args: string[]): Promise<unknown> {
      const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
      // The one-liner embedded in the script — identical to what the shell would run.
      const SET_ONELINER =
        'import json,sys;t,k,v=sys.argv[1],sys.argv[2],sys.argv[3];sys.stdout.write(json.dumps({"task_id":t,"op":"set","key":k,"value":json.loads(v)}))';
      const GET_ONELINER =
        'import json,sys;t,k=sys.argv[1],sys.argv[2];sys.stdout.write(json.dumps({"task_id":t,"op":"get","key":k}))';
      const oneliner = args[0] === 'set' ? SET_ONELINER : GET_ONELINER;
      const pyArgs = args[0] === 'set'
        ? ['task-xyz', args[1], args[2]]   // task, key, value
        : ['task-xyz', args[1]];           // task, key
      const out = realCp.execFileSync(
        'python3', ['-c', oneliner, ...pyArgs],
        { encoding: 'utf8' },
      );
      return JSON.parse(out);
    }

    it('set VALUE containing double-quotes produces valid JSON and round-trips correctly', async () => {
      // Simulate agent passing a JSON string that itself contains double-quotes.
      const jsonValue = JSON.stringify('hello "world"');   // → '"hello \\"world\\""'
      const payload = await runPy3(['set', 'my-key', jsonValue]) as Record<string, unknown>;
      expect(payload['op']).toBe('set');
      expect(payload['key']).toBe('my-key');
      expect(payload['value']).toBe('hello "world"');
    });

    it('set VALUE containing backslashes produces valid JSON and round-trips correctly', async () => {
      const jsonValue = JSON.stringify('path\\to\\file');  // → '"path\\\\to\\\\file"'
      const payload = await runPy3(['set', 'my-key', jsonValue]) as Record<string, unknown>;
      expect(payload['value']).toBe('path\\to\\file');
    });

    it('set VALUE containing newlines produces valid JSON and round-trips correctly', async () => {
      const jsonValue = JSON.stringify('line1\nline2\n');
      const payload = await runPy3(['set', 'my-key', jsonValue]) as Record<string, unknown>;
      expect(payload['value']).toBe('line1\nline2\n');
    });

    it('set KEY containing double-quotes produces valid JSON', async () => {
      const jsonValue = '"simple"';
      const payload = await runPy3(['set', 'section/key "with" quotes', jsonValue]) as Record<string, unknown>;
      expect(payload['key']).toBe('section/key "with" quotes');
    });

    it('set KEY containing backslashes produces valid JSON', async () => {
      const jsonValue = '"simple"';
      const payload = await runPy3(['set', 'path\\subkey', jsonValue]) as Record<string, unknown>;
      expect(payload['key']).toBe('path\\subkey');
    });

    it('set KEY containing newlines produces valid JSON', async () => {
      const jsonValue = '"simple"';
      const payload = await runPy3(['set', 'key\nwith\nnewline', jsonValue]) as Record<string, unknown>;
      expect(payload['key']).toBe('key\nwith\nnewline');
    });

    it('set VALUE containing percent signs does not corrupt payload (printf format-specifier guard)', async () => {
      // The old printf approach could misinterpret %s/%d in _json_str output.
      // python3 sys.argv is immune: % in data is just data.
      const jsonValue = JSON.stringify('100% complete %s %d %i');
      const payload = await runPy3(['set', 'my-key', jsonValue]) as Record<string, unknown>;
      expect(payload['value']).toBe('100% complete %s %d %i');
    });

    it('the generated script contains the expected python3 one-liner for set', () => {
      expect(capturedScript).toContain(
        `python3 -c 'import json,sys;t,k,v=sys.argv[1],sys.argv[2],sys.argv[3];sys.stdout.write(json.dumps({"task_id":t,"op":"set","key":k,"value":json.loads(v)}))' "$TASK" "$KEY" "$VALUE"`,
      );
    });

    it('the generated script contains the expected python3 one-liner for delete', () => {
      expect(capturedScript).toContain(
        `python3 -c 'import json,sys;t,k=sys.argv[1],sys.argv[2];sys.stdout.write(json.dumps({"task_id":t,"op":"delete","key":k}))' "$TASK" "$KEY"`,
      );
    });
  });

  it('reads the live token file each invocation and retries a write on 401 (live write-token fix)', () => {
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
      expect(script).toContain(`python3 -c 'import json,sys;sys.stdout.write(json.dumps({"task_id":sys.argv[1],"op":"list"}))' "$TASK" | _post_once`);
      expect(script).toContain(`python3 -c 'import json,sys;t,k=sys.argv[1],sys.argv[2];sys.stdout.write(json.dumps({"task_id":t,"op":"get","key":k}))' "$TASK" "$KEY" | _post_once`);
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

  it('blocks the old shell helpers as Resident operator-reply paths', () => {
    writeMessagesHelper('task-resident', '/tmp/resident-workspace', {
      residentReplyOnly: true,
    });
    const script = capturedScripts['/tmp/resident-workspace/messages'];
    expect(script).toContain('RESIDENT_REPLY_ONLY="1"');
    expect(script).toContain('resolve is disabled for Resident sessions');
    expect(script).toContain('this is not the Workday operator reply tool; call reply_to_operator instead');
  });

  it('uses external resident reply paths as fallbacks to process-bound authority', () => {
    writeMessagesHelper('task-resident', '/tmp/resident-workspace', {
      residentReplyOnly: true,
      replyKeyPath: '/tmp/resident-runtime/reply-key',
      replyReceiptPath: '/tmp/resident-runtime/reply-receipt.json',
      stagedReplyPath: '/tmp/resident-runtime/staged-reply.json',
    });
    const script = capturedScripts['/tmp/resident-workspace/messages'];
    expect(script).toContain(
      'REPLY_KEY_PATH="${BLUEPRINT_RESIDENT_REPLY_KEY_PATH:-/tmp/resident-runtime/reply-key}"',
    );
    expect(script).toContain(
      'REPLY_RECEIPT_PATH="${BLUEPRINT_RESIDENT_REPLY_RECEIPT_PATH:-/tmp/resident-runtime/reply-receipt.json}"',
    );
    expect(script).toContain(
      'STAGED_REPLY_PATH="${BLUEPRINT_RESIDENT_STAGED_REPLY_PATH:-/tmp/resident-runtime/staged-reply.json}"',
    );
  });

  it('reads the live token, captures stdin, retries on 401, and fails loud (live write-token fix)', async () => {
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
