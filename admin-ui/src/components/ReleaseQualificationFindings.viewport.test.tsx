import { afterEach, beforeEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ReleaseQualificationFindings } from './ReleaseQualificationFindings';
import {
  expectMinTouchTargetAt,
  expectNoOverflowAt,
  expectNoTextTruncationAt,
} from '../test/viewport';
import type { RequalificationReadiness } from '../api';

function makeReadiness(
  overrides: Partial<RequalificationReadiness> = {},
): RequalificationReadiness {
  return {
    requalification_ready: false,
    open_version_id: 'ver-uuid-open',
    audit_findings_truncated: false,
    gating_total: 4,
    gating_resolved: 2,
    reason: '2 of 4 carried high/medium audit finding(s) still unresolved',
    findings: [
      {
        key: 'adb632b709685086',
        severity: 'high',
        summary:
          'Missing input validation in task payload handler — could allow injection of arbitrary fields',
        state: 'resolved',
      },
      {
        key: 'deadbeef12345678',
        severity: 'medium',
        summary:
          'Admin endpoint lacks rate limiting; repeated calls could exhaust DB connection pool',
        state: 'stale',
      },
      {
        key: 'abcdef0123456789',
        severity: 'high',
        summary:
          'Rollup propagation loop skips already-landed children, freezing declared_unsatisfied verdicts',
        state: 'unresolved',
      },
      {
        key: '1234567890abcdef',
        severity: 'medium',
        summary:
          'Backend inline LLM spend is captured nowhere — every backend-side inline LLM call is dark',
        state: 'unresolved',
      },
    ],
    ...overrides,
  };
}

describe('ReleaseQualificationFindings — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  function makeRender(readiness: RequalificationReadiness) {
    return async function renderFindings() {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ReleaseQualificationFindings
            readiness={readiness}
            projectId="proj-1"
            readOnly={false}
            onRefresh={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
    };
  }

  test('blocked readiness — no overflow at 1280px', async () => {
    await expectNoOverflowAt(1280, makeRender(makeReadiness()));
  });

  test('blocked readiness — no text truncation at 1280px', async () => {
    await expectNoTextTruncationAt(1280, makeRender(makeReadiness()));
  });

  test('blocked readiness — no overflow at 375px', async () => {
    await expectNoOverflowAt(375, makeRender(makeReadiness()));
  });

  test('blocked readiness — no text truncation at 375px', async () => {
    await expectNoTextTruncationAt(375, makeRender(makeReadiness()));
  });

  test('blocked readiness — touch targets at 375px', async () => {
    await expectMinTouchTargetAt(375, makeRender(makeReadiness()));
  });

  test('truncated readiness — no overflow at 1280px', async () => {
    await expectNoOverflowAt(
      1280,
      makeRender(
        makeReadiness({
          audit_findings_truncated: true,
          reason:
            'carried audit findings were truncated at the cap — readiness cannot be asserted on a partial list (2/4 visible gating findings resolved)',
        }),
      ),
    );
  });
});
