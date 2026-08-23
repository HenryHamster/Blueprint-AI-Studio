import { describe, it, expect, vi } from 'vitest';
import {
  buildTypeCheckInstructions,
  buildReviewIterationPreamble,
  buildReviewVerdictInstructions,
  buildReviewPromptFingerprintContent,
  REVIEW_DELTA_FOCUS_NOTE,
  REVIEW_FIX_RESPONSES_INSTRUCTIONS,
} from './review-prompt.js';

describe('buildReviewIterationPreamble — delta-focus gating', () => {
  // Round 1 is the full-rigor full-PR pass; the delta-focus note (which scopes
  // later rounds to the fixer's changes + unresolved set) must NOT appear then.
  it('iteration 1 does not include the delta-focus note', () => {
    const out = buildReviewIterationPreamble(1, 6, false, false);
    expect(out).not.toContain('Delta-focus (iteration ≥2)');
    expect(out).toContain('review iteration 1 of 6');
  });

  it('iteration ≥2 includes the delta-focus note verbatim', () => {
    const out = buildReviewIterationPreamble(2, 6, false, false);
    expect(out).toContain(REVIEW_DELTA_FOCUS_NOTE);
    expect(out).toContain('only raise a NEW blocker if the delta introduced one');
  });

  it('a late iteration still includes the delta-focus note', () => {
    const out = buildReviewIterationPreamble(5, 6, false, false);
    expect(out).toContain('Delta-focus (iteration ≥2)');
  });
});

describe('buildTypeCheckInstructions — lint scoping', () => {
  // Reproduces the bug fixed alongside this test: lint_cmd like 'mypy .' was
  // being passed verbatim as the xargs executable, so xargs appended the
  // changed-files list AFTER the '.' and the linter scanned the full tree on
  // top of the changed files. The fix strips trailing '.' / './' tokens.
  const makeConfig = (lintCmd: string) => ({
    directories: [{ path: 'backend', type: 'python', lint_cmd: lintCmd }],
  });

  const xargsTail = (s: string, linterInvocation: string) => {
    // Whitespace-tolerant check for the trailing-dot tail that signals a
    // full-tree scan: `xargs -r <linter-invocation> .` or `... ./`.
    const re = new RegExp(`xargs\\s+-r\\s+${linterInvocation}\\s+\\.\\/?(?:\\s|\`|$)`);
    return re.test(s);
  };

  it.each([
    "svc$(whoami)`rm`;echo|cat&",
    'svc;rm -rf /',
    'svc|tee',
    'svc&wait',
    'svc`id`',
  ])('rejects unsafe build_config path %s before emitting review instructions', async (maliciousPath) => {
    const actualBuildConfig = await vi.importActual<typeof import('../build-config.js')>('../build-config.js');
    const cfg = actualBuildConfig.resolveBuildConfig({
      build_config: {
        directories: [
          {
            path: maliciousPath,
            type: 'python',
            build_cmd: 'python -m build',
          },
        ],
      },
    });
    expect(cfg).toBeUndefined();

    const out = buildTypeCheckInstructions(cfg);
    expect(out).not.toContain(maliciousPath);
    expect(out).toContain('Build, Tests & Lint — who verifies what');
  });

  it('ruff: keeps hardcoded --select E,F,W --ignore E501 override and strips trailing dot', () => {
    const out = buildTypeCheckInstructions(makeConfig('ruff check .'));
    expect(out).toContain('xargs -r python3 -m ruff check --select E,F,W --ignore E501');
    expect(xargsTail(out, 'python3 -m ruff check --select E,F,W --ignore E501')).toBe(false);
  });

  it('mypy: strips trailing dot so xargs gets only the changed-files list', () => {
    const out = buildTypeCheckInstructions(makeConfig('mypy .'));
    expect(out).toContain('xargs -r mypy`');
    expect(xargsTail(out, 'mypy')).toBe(false);
  });

  it('flake8: strips trailing dot', () => {
    const out = buildTypeCheckInstructions(makeConfig('flake8 .'));
    expect(out).toContain('xargs -r flake8`');
    expect(xargsTail(out, 'flake8')).toBe(false);
  });

  it('pyright: strips trailing dot', () => {
    const out = buildTypeCheckInstructions(makeConfig('pyright .'));
    expect(out).toContain('xargs -r pyright`');
    expect(xargsTail(out, 'pyright')).toBe(false);
  });

  it('mypy: strips trailing ./', () => {
    const out = buildTypeCheckInstructions(makeConfig('mypy ./'));
    expect(out).toContain('xargs -r mypy`');
    expect(xargsTail(out, 'mypy')).toBe(false);
  });

  it('eslint via npx: keeps hardcoded npx eslint invocation', () => {
    const out = buildTypeCheckInstructions({
      directories: [{ path: 'admin-ui', type: 'node', lint_cmd: 'npx eslint .' }],
    });
    expect(out).toContain('xargs -r npx eslint`');
    expect(xargsTail(out, 'npx eslint')).toBe(false);
  });

  it('python3 -m mypy: preserves prefix through to the xargs invocation', () => {
    const out = buildTypeCheckInstructions(makeConfig('python3 -m mypy .'));
    expect(out).toContain('xargs -r python3 -m mypy`');
    expect(xargsTail(out, 'python3 -m mypy')).toBe(false);
  });

  it('python3 -m flake8: preserves prefix through to the xargs invocation', () => {
    const out = buildTypeCheckInstructions(makeConfig('python3 -m flake8 .'));
    expect(out).toContain('xargs -r python3 -m flake8`');
    expect(xargsTail(out, 'python3 -m flake8')).toBe(false);
  });

  it('pnpm exec eslint: preserves prefix through to the xargs invocation', () => {
    const out = buildTypeCheckInstructions({
      directories: [{ path: 'admin-ui', type: 'node', lint_cmd: 'pnpm exec eslint .' }],
    });
    expect(out).toContain('xargs -r pnpm exec eslint`');
    expect(xargsTail(out, 'pnpm exec eslint')).toBe(false);
  });

  it('unrecognized lint_cmd (e.g. make lint): emitted verbatim, not rewritten into xargs form', () => {
    const out = buildTypeCheckInstructions({
      directories: [{ path: 'backend', type: 'python', lint_cmd: 'make lint' }],
    });
    expect(out).toContain('make lint');
    expect(out).not.toContain('xargs -r make');
  });

  it('no buildConfig: emits the CI-owns-build / scoped-lint fallback wording', () => {
    const out = buildTypeCheckInstructions(undefined);
    expect(out).toContain('Build, Tests & Lint — who verifies what');
    // CI is named as the authoritative gate and the reviewer is told not to re-run it.
    expect(out).toContain('CI is the authoritative gate');
    expect(out).toMatch(/do NOT re-run the build, type-check, or tests/);
    // The retained reviewer check is scoped lint on changed files.
    expect(out).toContain('python -m ruff check <changed .py files>');
  });

  it('buildConfig with no lint_cmd: states CI owns build/tests and no scoped lint is runnable', () => {
    const out = buildTypeCheckInstructions({
      directories: [{ path: 'backend', type: 'python' }],
    });
    expect(out).toContain('CI is the authoritative gate');
    expect(out).toContain('No reviewer-runnable lint is configured');
  });

  it('buildConfig with only a build_cmd: does NOT emit a build invocation as a gate', () => {
    // CI runs the build authoritatively; the reviewer must not re-run it. A
    // directory with only a build_cmd (no lint_cmd) contributes no reviewer check.
    const out = buildTypeCheckInstructions({
      directories: [{ path: 'admin-ui', type: 'node', build_cmd: 'npm run build' }],
    });
    expect(out).not.toContain('npm run build');
    expect(out).toContain('No reviewer-runnable lint is configured');
  });
});

describe('buildTypeCheckInstructions — CI owns build/tests, reviewer owns scoped lint', () => {
  it('reframes verification: CI is authoritative, reviewer does not re-run build/test and does not block on env failures', () => {
    const out = buildTypeCheckInstructions(undefined);
    expect(out).toContain('CI is the authoritative gate');
    expect(out).not.toContain('automatic REQUEST_CHANGES');
    expect(out).not.toContain('MUST run these');
    // An environment condition (uninstallable / unrunnable) must never be a blocker.
    expect(out).toMatch(/environment condition, never a code defect/);
    expect(out).toMatch(/NOT grounds for REQUEST_CHANGES/);
  });

  it('with a configured lint dir: frames scoped lint as an optional NET-NEW quality check, not a build gate', () => {
    const out = buildTypeCheckInstructions({
      directories: [{ path: 'backend', type: 'python', lint_cmd: 'ruff check .' }],
    });
    // Scoped lint one-liner is still emitted, restricted to changed files.
    expect(out).toContain('xargs -r python3 -m ruff check --select E,F,W --ignore E501');
    expect(out).toContain('NET-NEW');
    expect(out).toContain('OPTIONAL');
    // Not framed as an aggregate build gate, and no automatic-block language.
    expect(out).not.toContain('automatic REQUEST_CHANGES');
    expect(out).not.toContain('MUST run these');
  });

  it('does not instruct the reviewer to run ./check or aggregate build commands', () => {
    const out = buildTypeCheckInstructions({
      directories: [{ path: 'backend', type: 'python', lint_cmd: 'ruff check .' }],
    });
    expect(out).not.toContain('./check');
  });
});


describe('REVIEW_FIX_RESPONSES_INSTRUCTIONS — a step that cannot run is not a failed step', () => {
  const out = REVIEW_FIX_RESPONSES_INSTRUCTIONS;

  it('separates an environment block from a verify step that ran and contradicted the claim', () => {
    // These were one disposition. Conflating them makes a missing tool
    // ("git worktree --write-tree" on git < 2.38) score exactly like a false
    // claim: a MAJOR the fix agent cannot clear by changing any code. The
    // reviewer re-raises, the agent re-argues, and the convergence detectors
    // read that as nonconvergence and park the chain for an operator.
    expect(out).toContain('cannot EXECUTE here');
    expect(out).toContain('NOT the fix agent failing to converge');
    expect(out).toContain('A step that never executed at all is case 4, not this one.');
  });

  it('still fails closed on an environment block, but attributes it to tooling', () => {
    // Degrading gracefully must not mean waving the issue through: the
    // reviewer re-raises at major either way. What changes is that it first
    // has to try a check this sandbox supports, and then has to name the
    // command and the error so the block is legible as tooling.
    expect(out).toContain('an equivalent check this environment does support');
    expect(out).toContain('verify step blocked by environment:');
    expect(out).toContain('quote the exact command and its exact error');
    expect(out).toMatch(/no equivalent exists, re-raise at severity major/);
  });

  it('keeps the unsupported-claim disposition as its own case', () => {
    expect(out).toContain('the CLAIM itself cannot be checked');
    expect(out).toContain('"unverifiable" with severity major');
  });

  it('keeps the sycophancy guard', () => {
    expect(out).toContain('**Sycophancy guard**');
    expect(out).toContain('MUST be rejected');
  });
});


describe('buildReviewVerdictInstructions — block on substantiated code defects, not unverifiable uncertainty', () => {
  const out = buildReviewVerdictInstructions('task-xyz');

  it('keeps the two-verdict structure and no-third-verdict guidance', () => {
    expect(out).toContain('"verdict": "APPROVE"');
    expect(out).toContain('"verdict": "REQUEST_CHANGES"');
    expect(out).toContain('Do NOT emit "COMMENT", "NEUTRAL", "ADVISORY"');
    expect(out).toContain('There is no informational-only path');
  });

  it('keeps the exhaustiveness guidance', () => {
    expect(out).toContain('Exhaustiveness');
    expect(out).toContain('enumerate every blocker');
  });

  it('copies authoritative issue ids when re-raising and omits ids for new findings', () => {
    expect(out).toContain('copy its exact BP-... id');
    expect(out).toContain('omit this field for a genuinely new finding');
    expect(out).toContain('Never hash, recompute, or invent an id');
    expect(out).toContain('"issue_id": "BP-..."');
    expect(out).not.toContain('BP-... (copy');
  });

  it('removes the "when uncertain, pick REQUEST_CHANGES" tie-breaker', () => {
    expect(out).not.toContain('If you are uncertain between APPROVE and REQUEST_CHANGES, pick REQUEST_CHANGES');
    expect(out).not.toContain('another round is cheap');
  });

  it('reframes uncertainty: an unverifiable concern is not grounds to block; trust CI for build/test pass', () => {
    expect(out).toMatch(/Uncertainty rooted in something you could NOT verify .* is NOT grounds to block/);
    expect(out).toContain('trust CI for build/test pass');
    expect(out).toContain('substantiated CODE defect');
  });

  it('still writes the verdict JSON to the per-task /tmp file', () => {
    expect(out).toContain('/tmp/review_verdict_task-xyz.json');
  });

  it('fingerprint content does not contain duplicate file-restrictions sentence', () => {
    const content = buildReviewPromptFingerprintContent();
    // The file-restrictions sentence should appear exactly once in the fingerprint,
    // not duplicated by a standalone constant. This verifies the dead REVIEW_FILE_RESTRICTIONS_NOTE
    // constant has been removed and is not injected into the fingerprint.
    const restrictionsSentence = 'Check that the changes respect the File Restrictions listed above. Changes to restricted files should result in REQUEST_CHANGES.';
    const count = (content.match(new RegExp(restrictionsSentence.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g')) || []).length;
    expect(count).toBe(1);
  });
});
