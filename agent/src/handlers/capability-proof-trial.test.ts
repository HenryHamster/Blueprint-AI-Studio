import { describe, it, expect } from 'vitest';
import {
  buildCapabilityProofTrialRunner,
  parseTrialSkippedSummary,
} from './capability-proof-trial.js';

const WS = '/tmp/ws';

describe('buildCapabilityProofTrialRunner', () => {
  it('targets Python refs via scripts/test-backend.sh, backend/-relative', () => {
    const runner = buildCapabilityProofTrialRunner(
      ['backend/testing/test_foo.py', 'backend/testing/test_bar.py'],
      WS,
    );
    expect(runner).toEqual({
      cmd: 'bash',
      cwd: WS,
      args: ['scripts/test-backend.sh', 'testing/test_foo.py', 'testing/test_bar.py'],
    });
  });

  it('preserves a pytest ::node selector while stripping backend/ prefix', () => {
    const runner = buildCapabilityProofTrialRunner(
      ['backend/testing/test_foo.py::test_x'],
      WS,
    );
    expect(runner).toEqual({
      cmd: 'bash',
      cwd: WS,
      args: ['scripts/test-backend.sh', 'testing/test_foo.py::test_x'],
    });
  });

  it('targets TS refs via npx vitest run from the owning package dir', () => {
    const runner = buildCapabilityProofTrialRunner(
      ['agent/src/handlers/foo.test.ts', 'agent/src/utils/bar.test.ts'],
      WS,
    );
    expect(runner).toEqual({
      cmd: 'npx',
      cwd: `${WS}/agent`,
      args: ['vitest', 'run', 'src/handlers/foo.test.ts', 'src/utils/bar.test.ts'],
    });
  });

  it('returns null for an empty ref set (mints nothing)', () => {
    expect(buildCapabilityProofTrialRunner([], WS)).toBeNull();
    expect(buildCapabilityProofTrialRunner(['', '   '], WS)).toBeNull();
  });

  it('returns null for an unknown extension (cannot target cleanly)', () => {
    expect(buildCapabilityProofTrialRunner(['docs/readme.md'], WS)).toBeNull();
  });

  it('returns null for a mixed-language ref set', () => {
    expect(
      buildCapabilityProofTrialRunner(['backend/testing/test_a.py', 'agent/src/b.test.ts'], WS),
    ).toBeNull();
  });

  it('returns null for a cross-package TS ref set (no single invocation)', () => {
    expect(
      buildCapabilityProofTrialRunner(['agent/src/a.test.ts', 'admin-ui/src/b.test.ts'], WS),
    ).toBeNull();
  });
});

describe('parseTrialSkippedSummary', () => {
  it('parses passed/skipped/failed counts into total+skipped', () => {
    expect(parseTrialSkippedSummary('5 passed, 2 skipped in 1.2s')).toEqual({ total: 7, skipped: 2 });
    expect(parseTrialSkippedSummary('3 passed, 1 failed')).toEqual({ total: 4, skipped: 0 });
  });

  it('returns undefined when no counts are parseable (do not fabricate)', () => {
    expect(parseTrialSkippedSummary('')).toBeUndefined();
    expect(parseTrialSkippedSummary('some unrelated output')).toBeUndefined();
  });
});
