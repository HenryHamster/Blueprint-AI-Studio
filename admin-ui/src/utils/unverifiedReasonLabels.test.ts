import { describe, it, expect } from 'vitest';
import {
  UNVERIFIED_REASON_LABELS,
  humanizeUnverifiedReason,
  labelNoTestsReason,
} from './unverifiedReasonLabels';

describe('UNVERIFIED_REASON_LABELS', () => {
  it('is a non-empty record of string keys to string labels', () => {
    expect(typeof UNVERIFIED_REASON_LABELS).toBe('object');
    expect(Object.keys(UNVERIFIED_REASON_LABELS).length).toBeGreaterThan(0);
    for (const [key, label] of Object.entries(UNVERIFIED_REASON_LABELS)) {
      expect(typeof key).toBe('string');
      expect(typeof label).toBe('string');
      expect(label.length).toBeGreaterThan(0);
    }
  });

  it('contains the canonical reason keys documented in the source', () => {
    const expectedKeys = [
      'no_detectable_test_command',
      'kill_switch_off',
      'internal_staging_base',
      'undetected_runtime',
      'suite_all_skipped',
      'verify_setup_failed',
      'probe_passed',
      'probe_could_not_run',
      'probe_no_artifact',
      'documented_command_could_not_run',
      'documented_command_passed',
      'documented_command_no_artifact',
      'acceptance_test_not_run',
    ];
    for (const key of expectedKeys) {
      expect(UNVERIFIED_REASON_LABELS).toHaveProperty(key);
    }
  });
});

describe('humanizeUnverifiedReason', () => {
  it('returns the mapped label for a known reason key', () => {
    expect(humanizeUnverifiedReason('no_detectable_test_command')).toBe(
      'no runnable test command could be detected',
    );
    expect(humanizeUnverifiedReason('kill_switch_off')).toBe(
      'the no-CI test-gate is disabled (kill-switch off)',
    );
    expect(humanizeUnverifiedReason('suite_all_skipped')).toBe(
      'a test suite was present but every test was skipped (no real verification ran)',
    );
  });

  it('de-tokenizes an unknown reason key to a readable label', () => {
    expect(humanizeUnverifiedReason('some_future_reason')).toBe('some future reason');
    expect(humanizeUnverifiedReason('another-future-reason')).toBe('another future reason');
  });

  it('collapses multiple underscores/hyphens to a single space', () => {
    expect(humanizeUnverifiedReason('multi__underscore___key')).toBe('multi underscore key');
  });

  it('trims surrounding whitespace from the result', () => {
    expect(humanizeUnverifiedReason('_leading_trailing_')).toBe('leading trailing');
  });
});

describe('labelNoTestsReason', () => {
  it('returns null for a null input', () => {
    expect(labelNoTestsReason(null)).toBeNull();
  });

  it('returns null for an undefined input', () => {
    expect(labelNoTestsReason(undefined)).toBeNull();
  });

  it('returns null for an empty string', () => {
    expect(labelNoTestsReason('')).toBeNull();
  });

  it('maps "all_skipped" to its label', () => {
    expect(labelNoTestsReason('all_skipped')).toBe(
      'a test suite was present but every test was skipped (no real verification ran)',
    );
  });

  it('maps "no_real_tests" to its label', () => {
    expect(labelNoTestsReason('no_real_tests')).toBe(
      'no real tests were found (the suite collected only placeholder/empty tests)',
    );
  });

  it('maps "no_suite" to its label', () => {
    expect(labelNoTestsReason('no_suite')).toBe('no suite was detected');
  });

  it('returns null for an unrecognized reason (forward-compat: callers should skip rendering)', () => {
    expect(labelNoTestsReason('unknown_future_reason')).toBeNull();
  });
});
