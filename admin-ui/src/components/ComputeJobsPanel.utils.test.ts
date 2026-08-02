import { describe, expect, it } from 'vitest';

import {
  computeJobEvidenceTier,
  computeJobEvidenceGroup,
  computeJobEvidenceTierRank,
  computeJobViewableArtifacts,
  computeJobTerminalLabel,
  fmtBytes,
} from './ComputeJobsPanel.utils';
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

describe('computeJobTerminalLabel', () => {
  it('prefers canonical experiment status over legacy SUCCESS outcome', () => {
    expect(computeJobTerminalLabel({
      ...job({ outcome: 'SUCCESS' }),
      experiment_status: 'CRITERION_NOT_MET',
    } as ComputeJob)).toBe('Criterion not met');
  });
});

describe('fmtBytes', () => {
  it('formats artifact manifest sizes compactly', () => {
    expect(fmtBytes(512)).toBe('512 B');
    expect(fmtBytes(1536)).toBe('2 KB');
  });
});

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

/** Full-job factory for the group/rank helpers (they consult `status` via
 *  isComputeJobActive, so they take a whole ComputeJob rather than the narrow
 *  Pick that computeJobEvidenceTier accepts). */
const fullJob = (over: Partial<ComputeJob>): ComputeJob =>
  ({
    id: 'job-x',
    backend: '',
    status: 'SUCCEEDED',
    outcome: 'SUCCESS',
    criterion_verdict: null,
    per_arm_verdicts: null,
    ...over,
  }) as unknown as ComputeJob;

describe('computeJobEvidenceGroup', () => {
  it('covered (machine-verified) maps to the machine-verified group', () => {
    expect(
      computeJobEvidenceGroup(fullJob({ backend: 'local-bwrap', per_arm_verdicts: A_RESULT })),
    ).toBe('machine-verified');
  });

  it('every non-covered terminal tier collapses to low-trust', () => {
    // attested (cloud), partial (operator hw no result), backfilled (result no
    // execution), uncovered (self-reported) — all NOT machine-verified.
    expect(computeJobEvidenceGroup(fullJob({ backend: 'gcp', per_arm_verdicts: A_RESULT }))).toBe('low-trust'); // attested
    expect(computeJobEvidenceGroup(fullJob({ backend: 'local-bwrap' }))).toBe('low-trust'); // partial
    expect(computeJobEvidenceGroup(fullJob({ backend: '', per_arm_verdicts: A_RESULT }))).toBe('low-trust'); // backfilled
    expect(computeJobEvidenceGroup(fullJob({}))).toBe('low-trust'); // uncovered
  });

  it('active jobs have no tier yet — group is null so a tier filter excludes them', () => {
    expect(computeJobEvidenceGroup(fullJob({ status: 'RUNNING', outcome: null }))).toBeNull();
    expect(computeJobEvidenceGroup(fullJob({ status: 'QUEUED', outcome: null }))).toBeNull();
  });
});

describe('computeJobEvidenceTierRank', () => {
  it('orders highest-trust (covered) below lowest-trust (uncovered)', () => {
    const covered = computeJobEvidenceTierRank(fullJob({ backend: 'local-bwrap', per_arm_verdicts: A_RESULT }));
    const attested = computeJobEvidenceTierRank(fullJob({ backend: 'gcp', per_arm_verdicts: A_RESULT }));
    const partial = computeJobEvidenceTierRank(fullJob({ backend: 'local-bwrap' }));
    const backfilled = computeJobEvidenceTierRank(fullJob({ backend: '', per_arm_verdicts: A_RESULT }));
    const uncovered = computeJobEvidenceTierRank(fullJob({}));
    expect(covered).toBeLessThan(attested);
    expect(attested).toBeLessThan(backfilled);
    expect(backfilled).toBeLessThan(partial);
    expect(partial).toBeLessThan(uncovered);
  });

  it('active jobs (no tier) sort consistently last', () => {
    const active = computeJobEvidenceTierRank(fullJob({ status: 'RUNNING', outcome: null }));
    const terminal = computeJobEvidenceTierRank(fullJob({}));
    expect(active).toBe(Number.MAX_SAFE_INTEGER);
    expect(active).toBeGreaterThan(terminal);
  });

  it('sorting a mixed set by rank ascending yields covered → uncovered → active', () => {
    const jobs = [
      fullJob({ id: 'uncov' }),
      fullJob({ id: 'active', status: 'RUNNING', outcome: null }),
      fullJob({ id: 'cov', backend: 'local-bwrap', per_arm_verdicts: A_RESULT }),
    ];
    const ordered = [...jobs].sort((a, b) => computeJobEvidenceTierRank(a) - computeJobEvidenceTierRank(b));
    expect(ordered.map((j) => j.id)).toEqual(['cov', 'uncov', 'active']);
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
