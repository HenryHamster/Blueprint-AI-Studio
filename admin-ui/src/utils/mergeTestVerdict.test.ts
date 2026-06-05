import { describe, it, expect } from 'vitest';
import {
  parseMergeTestVerdict,
  deriveMergeTestOutcome,
  probeIsClearFailure,
  documentedCommandIsClearFailure,
} from './mergeTestVerdict';

function derive(raw: unknown) {
  const parsed = parseMergeTestVerdict(raw);
  if (!parsed) throw new Error('expected a parseable verdict');
  return deriveMergeTestOutcome(parsed);
}

describe('parseMergeTestVerdict', () => {
  it('returns null for a non-object', () => {
    expect(parseMergeTestVerdict(null)).toBeNull();
    expect(parseMergeTestVerdict('nope')).toBeNull();
    expect(parseMergeTestVerdict(42)).toBeNull();
  });

  it('parses an old-shape verdict with only detected/passed/output', () => {
    const v = parseMergeTestVerdict({ detected: true, passed: true, output: 'ok' });
    expect(v).toEqual({
      detected: true,
      passed: true,
      output: 'ok',
      probe: null,
      documentedCommands: null,
      partialSetupFailed: false,
      setupFailed: false,
      recovered: false,
      recovered_directories: [],
      noTestsReason: null,
    });
  });

  it('parses the rich artifact fields', () => {
    const v = parseMergeTestVerdict({
      detected: false,
      passed: false,
      output: '',
      probe: { ran: true, ecosystem: 'node', passed: true, could_not_run: false, declared: true, wrote_no_artifact: true },
      documentedCommands: { ran: true, passed: false, could_not_run: false, output: 'boom', label: 'npm run lint' },
      partialSetupFailed: true,
      recovered: true,
      recovered_directories: [{ path: 'pkg/a', type: 'node', test_cmd: 'npm test' }],
      noTestsReason: 'all_skipped',
    });
    expect(v?.probe?.declared).toBe(true);
    expect(v?.probe?.wrote_no_artifact).toBe(true);
    expect(v?.documentedCommands?.label).toBe('npm run lint');
    expect(v?.partialSetupFailed).toBe(true);
    expect(v?.recovered_directories).toHaveLength(1);
    expect(v?.noTestsReason).toBe('all_skipped');
  });

  it('drops an unrecognized noTestsReason', () => {
    expect(parseMergeTestVerdict({ noTestsReason: 'mystery' })?.noTestsReason).toBeNull();
  });
});

describe('clear-failure helpers', () => {
  it('probeIsClearFailure mirrors the backend predicate', () => {
    expect(probeIsClearFailure(null)).toBe(false);
    expect(probeIsClearFailure({ ran: false, passed: false })).toBe(false);
    expect(probeIsClearFailure({ ran: true, could_not_run: true, passed: false })).toBe(false);
    expect(probeIsClearFailure({ ran: true, could_not_run: false, passed: true })).toBe(false);
    expect(probeIsClearFailure({ ran: true, could_not_run: false, passed: false })).toBe(true);
  });

  it('documentedCommandIsClearFailure mirrors the backend predicate', () => {
    expect(documentedCommandIsClearFailure(null)).toBe(false);
    expect(documentedCommandIsClearFailure({ ran: true, could_not_run: true, passed: false })).toBe(false);
    expect(documentedCommandIsClearFailure({ ran: true, could_not_run: false, passed: false })).toBe(true);
  });
});

describe('deriveMergeTestOutcome precedence (mirrors classify_merge_test_verdict)', () => {
  it('documented-command clear failure beats a passing unit suite', () => {
    const r = derive({
      detected: true,
      passed: true,
      output: '',
      documentedCommands: { ran: true, passed: false, could_not_run: false, output: 'lint error', label: 'npm run lint' },
    });
    expect(r.outcome).toBe('blocked');
    expect(r.reason).toBe('documented_command_failed');
    expect(r.detail).toContain('npm run lint');
    expect(r.detail).toContain('lint error');
  });

  it('partialSetupFailed blocks', () => {
    const r = derive({ detected: true, passed: true, output: 'partial', partialSetupFailed: true });
    expect(r.outcome).toBe('blocked');
    expect(r.reason).toBe('partial_setup_failed');
  });

  it('detected=false + clear declared-build probe failure blocks as declared build', () => {
    const r = derive({
      detected: false,
      passed: false,
      output: '',
      probe: { ran: true, passed: false, could_not_run: false, declared: true, output: 'build broke', label: 'make build' },
    });
    expect(r.outcome).toBe('blocked');
    expect(r.reason).toBe('declared_build_failed');
    expect(r.detail).toContain('make build');
  });

  it('detected=false + clear generic probe failure blocks as probe', () => {
    const r = derive({
      detected: false,
      passed: false,
      output: '',
      probe: { ran: true, passed: false, could_not_run: false, output: 'tsc error' },
    });
    expect(r.outcome).toBe('blocked');
    expect(r.reason).toBe('probe_failed');
    expect(r.detail).toContain('tsc error');
  });

  it('detected=false + could-not-run probe falls open to undetected', () => {
    const r = derive({
      detected: false,
      passed: false,
      output: '',
      probe: { ran: true, passed: false, could_not_run: true },
    });
    expect(r.outcome).toBe('undetected');
  });

  it('detected=false + passing probe falls open to undetected', () => {
    const r = derive({
      detected: false,
      passed: false,
      output: '',
      probe: { ran: true, passed: true, could_not_run: false },
    });
    expect(r.outcome).toBe('undetected');
  });

  it('passed unit suite => pass', () => {
    expect(derive({ detected: true, passed: true, output: '' }).outcome).toBe('pass');
  });

  it('detected suite that did not pass => fail', () => {
    const r = derive({ detected: true, passed: false, output: 'FAIL spec' });
    expect(r.outcome).toBe('fail');
    expect(r.reason).toBe('suite_failed');
    expect(r.detail).toContain('FAIL spec');
  });

  it('old-shape verdict (no extras) keeps legacy behavior', () => {
    expect(derive({ detected: false, passed: false, output: '' }).outcome).toBe('undetected');
    expect(derive({ detected: true, passed: true, output: '' }).outcome).toBe('pass');
  });
});
