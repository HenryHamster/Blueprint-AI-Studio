import { describe, expect, it } from 'vitest';

import { computeJobEvidenceTier } from './ComputeJobsPanel.utils';

/**
 * `covered` is the tier EvidenceTierBadge renders as "Machine verified".
 *
 * It used to be derived from the backend NAME alone — `backend.startsWith('local-')`
 * — which made it a LOCATION proxy: any job that merely executed on operator
 * hardware claimed verification, even when it produced no result at all. These
 * tests pin the corrected rule: "Machine verified" requires BOTH operator-hardware
 * execution AND a measurable result.
 *
 * Mirrors backend knowledge_completion.py::_compute_evidence_tier.
 */
type TierJob = Parameters<typeof computeJobEvidenceTier>[0];

const job = (over: Partial<TierJob>): TierJob =>
  ({
    backend: '',
    status: 'SUCCEEDED',
    outcome: 'SUCCESS',
    criterion_verdict: null,
    per_arm_verdicts: null,
    ...over,
  }) as TierJob;

const A_RESULT = { armA: { value: 1.5, met: true, cost_usd: null } };

describe('computeJobEvidenceTier', () => {
  it('local run WITH a result is machine-verified (covered)', () => {
    expect(
      computeJobEvidenceTier(job({ backend: 'local-bwrap', per_arm_verdicts: A_RESULT })),
    ).toBe('covered');
  });

  it('runner-subprocess WITH a result is machine-verified (covered)', () => {
    expect(
      computeJobEvidenceTier(
        job({
          backend: 'runner-subprocess',
          criterion_verdict: { metric: 'loss', met: true } as TierJob['criterion_verdict'],
        }),
      ),
    ).toBe('covered');
  });

  it('local run with NO result is NOT machine-verified — it only proves where it ran', () => {
    // The regression this guards: an empty run on operator hardware rendering as
    // "Machine verified". Execution provenance is real, so it is `partial`, not
    // `uncovered` — but nothing was verified, so it must not be `covered`.
    expect(computeJobEvidenceTier(job({ backend: 'local-bwrap' }))).toBe('partial');
    expect(computeJobEvidenceTier(job({ backend: 'runner-subprocess' }))).toBe('partial');
  });

  it('an empty per_arm_verdicts map is not a result', () => {
    expect(
      computeJobEvidenceTier(job({ backend: 'local-container', per_arm_verdicts: {} })),
    ).toBe('partial');
  });

  it('cloud execution is attested, never machine-verified', () => {
    expect(computeJobEvidenceTier(job({ backend: 'gcp', per_arm_verdicts: A_RESULT }))).toBe(
      'attested',
    );
  });

  it('a result with no execution record is backfilled', () => {
    expect(computeJobEvidenceTier(job({ backend: '', per_arm_verdicts: A_RESULT }))).toBe(
      'backfilled',
    );
  });

  it('no backend and no result is uncovered', () => {
    expect(computeJobEvidenceTier(job({}))).toBe('uncovered');
  });
});
