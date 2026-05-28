import { describe, it, expect, vi } from 'vitest';
import { buildTypeCheckInstructions } from './review-prompt.js';

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
    expect(out).toContain('Build & Type Safety (MUST run these');
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

  it('no buildConfig: emits the generic fallback wording', () => {
    const out = buildTypeCheckInstructions(undefined);
    expect(out).toContain('Build & Type Safety (MUST run these');
    expect(out).toContain('For TypeScript/JavaScript changes');
    expect(out).toContain('For Python changes');
  });

  it('buildConfig with no build_cmd or lint_cmd: emits "No build commands configured" notice', () => {
    const out = buildTypeCheckInstructions({
      directories: [{ path: 'backend', type: 'python' }],
    });
    expect(out).toContain('No build commands configured');
  });
});
