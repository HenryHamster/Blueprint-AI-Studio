export function sortDesc<T>(entries: [string, T][], key: (v: T) => number): [string, T][] {
  return entries.sort((a, b) => key(b[1]) - key(a[1]));
}

export interface TrendHistory {
  failed_by_taxonomy?: Record<string, number>;
}

export function trendFor(history: TrendHistory[], cat: string): { arrow: string; color: string } {
  if (history.length < 4) return { arrow: '–', color: 'text-neutral-500' };
  const sumAt = (arr: TrendHistory[]) => arr.reduce((a, p) => a + (p.failed_by_taxonomy?.[cat] ?? 0), 0);
  const recent = sumAt(history.slice(-2));
  const prior = sumAt(history.slice(-4, -2));
  if (recent > prior) return { arrow: '↑', color: 'text-red-400' };
  if (recent < prior) return { arrow: '↓', color: 'text-emerald-400' };
  return { arrow: '–', color: 'text-neutral-500' };
}
