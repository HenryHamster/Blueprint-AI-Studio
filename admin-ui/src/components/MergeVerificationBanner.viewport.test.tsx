import { afterEach, describe, test, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';
import { MergeVerificationBanner } from './MergeVerificationBanner';
import type { MergeGateRuntime, MergeVerificationVerdict } from '../types/project';

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

  function renderVerdict(verdict: MergeVerificationVerdict | null, runtime?: MergeGateRuntime | null, blockDegradedEnabled?: boolean) {
    return async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      host.style.maxWidth = '375px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<MergeVerificationBanner verdict={verdict} runtime={runtime} blockDegradedEnabled={blockDegradedEnabled} />);
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

  test('renders the merge-verification-health summary without overflow at 375px', async () => {
    const runtime: MergeGateRuntime = {
      last_run_at: '2026-04-24T11:00:00Z',
      executed_real_tests: true,
      summary: {
        window: 6,
        verified_runs: 4,
        fell_open_runs: 2,
        total_fall_opens: 5,
        most_common_reason: 'no_detectable_test_command',
      },
    };
    await expectNoOverflowAt(375, renderVerdict({ state: 'tests_gated', test_commands: [{ path: 'backend/', test_cmd: 'pytest' }] }, runtime));
    const summary = host!.querySelector('[data-testid="merge-gate-summary"]');
    expect(summary?.textContent).toContain('verified 4 of the last 6 merges');
    expect(summary?.textContent).toContain('fell open 2 times');
    expect(summary?.textContent).toContain('no runnable test command could be detected');
  });

  test('legacy runtime without summary renders no aggregate line', async () => {
    const runtime: MergeGateRuntime = {
      last_run_at: '2026-04-24T11:00:00Z',
      executed_real_tests: true,
    };
    await renderVerdict({ state: 'tests_gated', test_commands: [{ path: 'backend/', test_cmd: 'pytest' }] }, runtime)();
    expect(host!.querySelector('[data-testid="merge-gate-summary"]')).toBeNull();
  });

  test('honest-block badge renders without overflow at 375px when blockDegradedEnabled', async () => {
    const runtime: MergeGateRuntime = {
      last_run_at: '2026-04-24T11:00:00Z',
      executed_real_tests: false,
      last_degraded_block_at: '2026-04-24T10:00:00Z',
      last_degraded_block_reason: 'setup_failure',
    };
    await expectNoOverflowAt(375, renderVerdict(
      { state: 'tests_gated', test_commands: [{ path: 'backend/', test_cmd: 'pytest' }] },
      runtime,
      true,
    ));
    const badge = host!.querySelector('[data-testid="honest-block-badge"]');
    expect(badge).not.toBeNull();
    expect(badge?.textContent).toContain('Honest-block policy active');
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
