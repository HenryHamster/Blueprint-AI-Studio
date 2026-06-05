import { describe, it, expect } from 'vitest';
import { formatMergeGateSummary } from './mergeGateSummary';
import type { MergeGateSummary } from '../types/project';

describe('formatMergeGateSummary', () => {
  it('builds the full sentence with a mapped most-common reason', () => {
    const summary: MergeGateSummary = {
      window: 6,
      verified_runs: 4,
      fell_open_runs: 2,
      total_fall_opens: 5,
      most_common_reason: 'no_detectable_test_command',
    };
    expect(formatMergeGateSummary(summary)).toBe(
      'verified 4 of the last 6 merges; fell open 2 times (most common: no runnable test command could be detected)',
    );
  });

  it('de-tokenizes an unknown reason key via the shared humanizer fallback', () => {
    const summary: MergeGateSummary = {
      window: 3,
      verified_runs: 1,
      fell_open_runs: 2,
      total_fall_opens: 2,
      most_common_reason: 'some_future_reason',
    };
    expect(formatMergeGateSummary(summary)).toBe(
      'verified 1 of the last 3 merges; fell open 2 times (most common: some future reason)',
    );
  });

  it('omits the (most common: …) clause when the reason key is null', () => {
    const summary: MergeGateSummary = {
      window: 5,
      verified_runs: 3,
      fell_open_runs: 2,
      total_fall_opens: 2,
      most_common_reason: null,
    };
    expect(formatMergeGateSummary(summary)).toBe('verified 3 of the last 5 merges; fell open 2 times');
  });

  it('drops the fell-open clause entirely when there were no fall-opens', () => {
    const summary: MergeGateSummary = {
      window: 4,
      verified_runs: 4,
      fell_open_runs: 0,
      total_fall_opens: 0,
      most_common_reason: null,
    };
    expect(formatMergeGateSummary(summary)).toBe('verified 4 of the last 4 merges');
  });

  it('singularizes the window and fall-open counts', () => {
    const summary: MergeGateSummary = {
      window: 1,
      verified_runs: 0,
      fell_open_runs: 1,
      total_fall_opens: 1,
      most_common_reason: 'kill_switch_off',
    };
    expect(formatMergeGateSummary(summary)).toBe(
      'verified 0 of the last 1 merge; fell open 1 time (most common: the no-CI test-gate is disabled (kill-switch off))',
    );
  });

  it('returns null for an absent summary (legacy / never-ran record)', () => {
    expect(formatMergeGateSummary(undefined)).toBeNull();
    expect(formatMergeGateSummary(null)).toBeNull();
  });

  it('returns null for an empty window', () => {
    const summary: MergeGateSummary = {
      window: 0,
      verified_runs: 0,
      fell_open_runs: 0,
      total_fall_opens: 0,
      most_common_reason: null,
    };
    expect(formatMergeGateSummary(summary)).toBeNull();
  });
});
