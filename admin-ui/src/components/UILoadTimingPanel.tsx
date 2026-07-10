/* eslint-disable react-refresh/only-export-components */

import { useState } from 'react';
import { apiFetch } from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';

// ── Types ────────────────────────────────────────────────────────────────────

interface TimingSample {
  ts: string;
  page_ready_ms: number;
  sample_type: string;
}

interface UiLoadTimingResponse {
  surfaces: Record<string, TimingSample[]>;
}

interface PerTypeStats {
  count: number;
  p50: number | null;
  p90: number | null;
}

// ── Helpers (exported for testing) ───────────────────────────────────────────

export function percentile(sorted: number[], p: number): number | null {
  if (sorted.length === 0) return null;
  const idx = Math.min(Math.ceil(sorted.length * p / 100) - 1, sorted.length - 1);
  return sorted[Math.max(0, idx)];
}

export function fmtMs(ms: number | null): string {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

// ── Component ────────────────────────────────────────────────────────────────

export function UILoadTimingPanel() {
  const [data, setData] = useState<UiLoadTimingResponse | null>(null);
  const [pollError, setPollError] = useState(false);

  usePoller(async (signal) => {
    try {
      const res = await apiFetch('/api/admin/ui-load-timing');
      if (signal.aborted) return;
      if (!res.ok) { setPollError(true); return; }
      setData(await res.json());
      setPollError(false);
    } catch {
      if (!signal.aborted) setPollError(true);
    }
  }, { intervalMs: DEFAULT_POLL_MS });

  // ── Compute per-surface / per-sample-type percentiles ─────────────────

  const surfaces = data?.surfaces;
  if (!surfaces || Object.keys(surfaces).length === 0) {
    if (pollError) {
      return (
        <section>
          <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">UI Load Timing</h2>
          <div className="text-xs text-red-400 px-1">Timing data unavailable — fetch failed.</div>
        </section>
      );
    }
    return null;
  }

  const allSampleTypes = new Set<string>();
  interface SurfaceRow {
    surface: string;
    stats: Record<string, PerTypeStats>;
  }
  const rows: SurfaceRow[] = [];

  for (const [surface, samples] of Object.entries(surfaces).sort()) {
    const byType: Record<string, number[]> = {};
    for (const s of samples) {
      allSampleTypes.add(s.sample_type);
      if (!byType[s.sample_type]) byType[s.sample_type] = [];
      byType[s.sample_type].push(s.page_ready_ms);
    }
    const stats: Record<string, PerTypeStats> = {};
    for (const [type, vals] of Object.entries(byType)) {
      vals.sort((a, b) => a - b);
      stats[type] = {
        count: vals.length,
        p50: percentile(vals, 50),
        p90: percentile(vals, 90),
      };
    }
    rows.push({ surface, stats });
  }

  const typeList = Array.from(allSampleTypes).sort();

  return (
    <section>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">UI Load Timing</h2>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-ui-sm text-neutral-500 border-b border-neutral-800/50">
              <th className="text-left px-3 py-2 font-medium whitespace-nowrap">Surface</th>
              {typeList.map(t => (
                <th key={t} colSpan={3} className="text-center px-1 py-2 font-medium text-neutral-400 border-l border-neutral-800/30">
                  {t}
                </th>
              ))}
            </tr>
            <tr className="text-ui-sm text-neutral-500 border-b border-neutral-800/50">
              <th />
              {typeList.flatMap(t => [
                <th key={`${t}-p50`} className="text-right px-2 py-1 font-medium">p50</th>,
                <th key={`${t}-p90`} className="text-right px-2 py-1 font-medium">p90</th>,
                <th key={`${t}-n`} className="text-right px-2 py-1 font-medium">N</th>,
              ])}
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-800/30">
            {rows.map(r => (
              <tr key={r.surface}>
                <td className="px-3 py-2 font-medium text-neutral-200 whitespace-nowrap">{r.surface}</td>
                {typeList.flatMap(t => {
                  const s = r.stats[t];
                  return [
                    <td key={`${t}-p50`} className="text-right px-2 py-2 tabular-nums text-neutral-300">{fmtMs(s?.p50)}</td>,
                    <td key={`${t}-p90`} className="text-right px-2 py-2 tabular-nums text-neutral-500">{fmtMs(s?.p90)}</td>,
                    <td key={`${t}-n`} className="text-right px-2 py-2 tabular-nums text-neutral-500">{s?.count ?? '—'}</td>,
                  ];
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
