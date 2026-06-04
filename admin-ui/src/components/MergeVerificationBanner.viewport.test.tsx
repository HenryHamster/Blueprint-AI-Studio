import { afterEach, describe, test, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';
import { MergeVerificationBanner } from './MergeVerificationBanner';
import type { MergeVerificationVerdict } from '../types/project';

// Read-only banner with no interactive controls — the touch-target check is
// intentionally omitted (there are no buttons/links). Overflow and text-truncation
// are the relevant mobile-readability risks for the long test-command strings.

describe('MergeVerificationBanner — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { act(() => root!.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  function renderVerdict(verdict: MergeVerificationVerdict | null) {
    return async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      host.style.maxWidth = '375px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<MergeVerificationBanner verdict={verdict} />);
      });
    };
  }

  test('unverified warning renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, renderVerdict({ state: 'unverified', test_commands: [] }));
    const banner = host!.querySelector('[data-testid="merge-verification-banner"]');
    expect(banner?.getAttribute('data-verdict-state')).toBe('unverified');
    expect(banner?.textContent).toContain('UNVERIFIED');
  });

  test('tests_gated lists long commands without overflow at 375px', async () => {
    const verdict: MergeVerificationVerdict = {
      state: 'tests_gated',
      test_commands: [
        { path: 'backend/', test_cmd: 'python3 -m pytest backend/testing --maxfail=1 --disable-warnings -q' },
        { path: 'admin-ui/', test_cmd: 'npm test --workspace admin-ui -- --run --reporter=verbose' },
      ],
    };
    await expectNoOverflowAt(375, renderVerdict(verdict));
    const banner = host!.querySelector('[data-testid="merge-verification-banner"]');
    expect(banner?.getAttribute('data-verdict-state')).toBe('tests_gated');
    expect(banner?.textContent).toContain('python3 -m pytest');
  });

  test('tests_gated long commands are not silently clipped at 375px', async () => {
    const verdict: MergeVerificationVerdict = {
      state: 'tests_gated',
      test_commands: [
        { path: 'services/api/', test_cmd: 'go test ./... -run TestVeryLongIntegrationSuiteNameThatWouldOverflow -count=1' },
      ],
    };
    await expectNoTextTruncationAt(375, renderVerdict(verdict));
  });

  test('ci_checks informational state renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, renderVerdict({ state: 'ci_checks', test_commands: [] }));
    const banner = host!.querySelector('[data-testid="merge-verification-banner"]');
    expect(banner?.getAttribute('data-verdict-state')).toBe('ci_checks');
  });

  test('null verdict renders nothing', async () => {
    await act(async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      r.render(<MergeVerificationBanner verdict={null} />);
    });
    expect(host!.querySelector('[data-testid="merge-verification-banner"]')).toBeNull();
  });
});
