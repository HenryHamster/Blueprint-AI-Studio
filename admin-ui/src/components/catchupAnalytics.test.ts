import { describe, test, expect } from 'vitest';
import { sortDesc, trendFor, type TrendHistory } from './catchupAnalytics';

describe('catchupAnalytics', () => {
  describe('sortDesc', () => {
    test('sorts entries by numeric value in descending order', () => {
      const entries: [string, number][] = [['a', 10], ['b', 30], ['c', 20]];
      const result = sortDesc(entries, v => v);
      expect(result).toEqual([['b', 30], ['c', 20], ['a', 10]]);
    });

    test('handles equal values maintaining stability', () => {
      const entries: [string, number][] = [['a', 10], ['b', 10], ['c', 10]];
      const result = sortDesc(entries, v => v);
      expect(result.length).toBe(3);
      expect(result.every(([, v]) => v === 10)).toBe(true);
    });

    test('sorts by extracted key function with complex objects', () => {
      const entries: [string, { count: number }][] = [
        ['a', { count: 5 }],
        ['b', { count: 15 }],
        ['c', { count: 10 }],
      ];
      const result = sortDesc(entries, v => v.count);
      expect(result).toEqual([
        ['b', { count: 15 }],
        ['c', { count: 10 }],
        ['a', { count: 5 }],
      ]);
    });

    test('handles empty array', () => {
      const entries: [string, number][] = [];
      const result = sortDesc(entries, v => v);
      expect(result).toEqual([]);
    });
  });

  describe('trendFor', () => {
    test('returns neutral when history length < 4', () => {
      const history: TrendHistory[] = [
        { failed_by_taxonomy: { error: 5 } },
        { failed_by_taxonomy: { error: 3 } },
      ];
      const result = trendFor(history, 'error');
      expect(result.arrow).toBe('–');
      expect(result.color).toBe('text-neutral-500');
    });

    test('returns up arrow when trend is increasing', () => {
      const history: TrendHistory[] = [
        { failed_by_taxonomy: { error: 5 } },
        { failed_by_taxonomy: { error: 3 } },
        { failed_by_taxonomy: { error: 2 } },
        { failed_by_taxonomy: { error: 8 } },
      ];
      const result = trendFor(history, 'error');
      expect(result.arrow).toBe('↑');
      expect(result.color).toBe('text-red-400');
    });

    test('returns down arrow when trend is decreasing', () => {
      const history: TrendHistory[] = [
        { failed_by_taxonomy: { error: 10 } },
        { failed_by_taxonomy: { error: 12 } },
        { failed_by_taxonomy: { error: 5 } },
        { failed_by_taxonomy: { error: 3 } },
      ];
      const result = trendFor(history, 'error');
      expect(result.arrow).toBe('↓');
      expect(result.color).toBe('text-emerald-400');
    });

    test('returns neutral arrow when values are equal', () => {
      const history: TrendHistory[] = [
        { failed_by_taxonomy: { error: 5 } },
        { failed_by_taxonomy: { error: 5 } },
        { failed_by_taxonomy: { error: 5 } },
        { failed_by_taxonomy: { error: 5 } },
      ];
      const result = trendFor(history, 'error');
      expect(result.arrow).toBe('–');
      expect(result.color).toBe('text-neutral-500');
    });

    test('handles missing taxonomy data gracefully', () => {
      const history: TrendHistory[] = [
        {},
        {},
        { failed_by_taxonomy: { other: 1 } },
        { failed_by_taxonomy: { other: 2 } },
      ];
      const result = trendFor(history, 'error');
      expect(result.arrow).toBe('–');
      expect(result.color).toBe('text-neutral-500');
    });

    test('sums recent 2 periods vs prior 2 periods correctly', () => {
      const history: TrendHistory[] = [
        { failed_by_taxonomy: { category: 10 } },
        { failed_by_taxonomy: { category: 20 } },
        { failed_by_taxonomy: { category: 1 } },
        { failed_by_taxonomy: { category: 2 } },
        { failed_by_taxonomy: { category: 5 } },
      ];
      const result = trendFor(history, 'category');
      expect(result.arrow).toBe('↓');
      expect(result.color).toBe('text-emerald-400');
    });
  });
});
