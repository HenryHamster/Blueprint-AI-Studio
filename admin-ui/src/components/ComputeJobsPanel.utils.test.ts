import { describe, expect, it } from 'vitest';

import { computeJobEvidenceTier, computeJobViewableArtifacts } from './ComputeJobsPanel.utils';
import type { ComputeJob } from '../api';

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

  it('prefers the server-provided evidence_tier over client derivation', () => {
    // A payload whose client-derived tier would be `partial` (operator hardware,
    // no result) but which carries a canonical server tier must render the
    // server value — the single source of truth on fresh responses.
    expect(
      computeJobEvidenceTier(
        job({ backend: 'local-bwrap', evidence_tier: 'covered' } as Partial<TierJob>),
      ),
    ).toBe('covered');
  });

  it('falls back to client derivation when server tier is absent/invalid', () => {
    // Older/cached payloads omit evidence_tier (undefined) — the client rule
    // still applies for backward compatibility.
    expect(
      computeJobEvidenceTier(
        job({ backend: 'gcp', per_arm_verdicts: A_RESULT, evidence_tier: undefined } as Partial<TierJob>),
      ),
    ).toBe('attested');
  });
});

describe('computeJobViewableArtifacts', () => {
  const withArtifacts = (viewable: unknown): ComputeJob =>
    ({ viewable_artifacts: viewable } as unknown as ComputeJob);

  it('keeps a kind:"pdf" entry (previously dropped) alongside image/video', () => {
    const artifacts = computeJobViewableArtifacts(
      withArtifacts([
        { filename: 'loss.png', storage_uri: 'gs://b/0-loss.png', kind: 'image', size_bytes: 10 },
        { filename: 'report.pdf', storage_uri: 'gs://b/1-report.pdf', kind: 'pdf', size_bytes: 88_000 },
        { filename: 'rollout.mp4', storage_uri: 'gs://b/2-rollout.mp4', kind: 'video', size_bytes: 20 },
      ]),
    );
    expect(artifacts.map(a => a.kind)).toEqual(['image', 'pdf', 'video']);
    const pdf = artifacts.find(a => a.kind === 'pdf');
    expect(pdf).toMatchObject({ name: 'report.pdf', storage_uri: 'gs://b/1-report.pdf', bytes: 88_000 });
  });

  it('still drops entries with an unknown kind or missing storage_uri', () => {
    const artifacts = computeJobViewableArtifacts(
      withArtifacts([
        { filename: 'x.bin', storage_uri: 'gs://b/x.bin', kind: 'binary', size_bytes: 5 },
        { filename: 'no-uri.pdf', kind: 'pdf', size_bytes: 5 },
      ]),
    );
    expect(artifacts).toEqual([]);
  });
});
