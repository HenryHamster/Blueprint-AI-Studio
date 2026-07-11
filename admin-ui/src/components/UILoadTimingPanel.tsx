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

interface PinnedBaseline {
  id: string;
  surface: string;
  sample_type: string;
  p50: number;
  p90: number;
  count: number;
  label: string;
  pinned_at: string;
}

interface UiLoadTimingResponse {
  surfaces: Record<string, TimingSample[]>;
  baselines: PinnedBaseline[];
}

export interface PerTypeStats {
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

export function computeStats(vals: number[]): PerTypeStats {
  if (vals.length === 0) return { count: 0, p50: null, p90: null };
  const sorted = [...vals].sort((a, b) => a - b);
  return { count: vals.length, p50: percentile(sorted, 50), p90: percentile(sorted, 90) };
}

/** Partition samples by cutoffIso and compute p50/p90/count per sample_type for before/after groups. */
export function computePerTypeSplit(
  samples: TimingSample[],
  cutoffIso: string,
): Record<string, { before: PerTypeStats; after: PerTypeStats }> {
  const byType: Record<string, { before: number[]; after: number[] }> = {};
  for (const s of samples) {
    if (!byType[s.sample_type]) byType[s.sample_type] = { before: [], after: [] };
    if (s.ts < cutoffIso) {
      byType[s.sample_type].before.push(s.page_ready_ms);
    } else {
      byType[s.sample_type].after.push(s.page_ready_ms);
    }
  }
  const result: Record<string, { before: PerTypeStats; after: PerTypeStats }> = {};
  for (const [type, groups] of Object.entries(byType)) {
    result[type] = { before: computeStats(groups.before), after: computeStats(groups.after) };
  }
  return result;
}

// ── Cutoff presets ────────────────────────────────────────────────────────────

const CUTOFF_PRESETS = [
  { label: 'Lazy-load wave (2026-07-09 22:09 UTC)', value: '2026-07-09T22:09:00Z' },
  { label: 'Custom', value: 'custom' },
] as const;

// ── Component ────────────────────────────────────────────────────────────────

export function UILoadTimingPanel() {
  const [data, setData] = useState<UiLoadTimingResponse | null>(null);
  const [pollError, setPollError] = useState(false);
  const [preset, setPreset] = useState<string>(CUTOFF_PRESETS[0].value);
  const [customCutoff, setCustomCutoff] = useState('2026-07-09T22:09');

  const cutoffIso = preset === 'custom' ? `${customCutoff}:00Z` : preset;

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

  // ── Compute per-surface / per-sample-type before/after split ──────────────

  // Build baseline lookup: surface -> sample_type -> PinnedBaseline
  const baselineMap: Record<string, Record<string, PinnedBaseline>> = {};
  if (data?.baselines) {
    for (const b of data.baselines) {
      if (!baselineMap[b.surface]) baselineMap[b.surface] = {};
      baselineMap[b.surface][b.sample_type] = b;
    }
  }

  const allSampleTypes = new Set<string>();
  interface SurfaceRow {
    surface: string;
    split: ReturnType<typeof computePerTypeSplit>;
  }
  const rows: SurfaceRow[] = [];

  for (const [surface, samples] of Object.entries(surfaces).sort()) {
    for (const s of samples) allSampleTypes.add(s.sample_type);
    const split = computePerTypeSplit(samples, cutoffIso);
    // Override "before" stats with pinned baseline when available so they
    // survive the 200-sample FIFO cap.
    const surfaceBaselines = baselineMap[surface];
    if (surfaceBaselines) {
      for (const [st, bl] of Object.entries(surfaceBaselines)) {
        if (!split[st]) {
          split[st] = { before: { count: 0, p50: null, p90: null }, after: { count: 0, p50: null, p90: null } };
        }
        split[st].before = { count: bl.count, p50: bl.p50, p90: bl.p90 };
      }
    }
    rows.push({ surface, split });
  }

  const typeList = Array.from(allSampleTypes).sort();

  return (
    <section>
      <div className="flex items-center justify-between mb-2">
        <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest">UI Load Timing</h2>
        <div className="flex items-center gap-2 text-xs">
          <span className="text-neutral-500">Cutoff:</span>
          <select
            value={preset}
            onChange={e => setPreset(e.target.value)}
            className="bg-neutral-800 text-neutral-200 border border-neutral-700 rounded px-2 py-0.5 text-xs"
          >
            {CUTOFF_PRESETS.map(p => (
              <option key={p.value} value={p.value}>{p.label}</option>
            ))}
          </select>
          {preset === 'custom' && (
            <input
              type="datetime-local"
              value={customCutoff}
              onChange={e => setCustomCutoff(e.target.value)}
              className="bg-neutral-800 text-neutral-200 border border-neutral-700 rounded px-2 py-0.5 text-xs"
            />
          )}
        </div>
      </div>
      <div className="overflow-hidden rounded-lg border border-neutral-800/50 bg-neutral-900/50 md:overflow-x-auto">
        <table className="block w-full text-xs md:table">
          <thead className="hidden md:table-header-group">
            <tr className="text-ui-sm text-neutral-500 border-b border-neutral-800/50">
              <th className="text-left px-3 py-2 font-medium whitespace-nowrap" rowSpan={2}>Surface</th>
              {typeList.map(t => (
                <th key={t} colSpan={6} className="text-center px-1 py-2 font-medium text-neutral-400 border-l border-neutral-800/30">
                  {t}
                </th>
              ))}
            </tr>
            <tr className="text-ui-sm text-neutral-500 border-b border-neutral-800/50">
              {typeList.flatMap(t => [
                <th key={`${t}-bef-p50`} className="text-right px-2 py-1 font-medium text-neutral-600">before p50</th>,
                <th key={`${t}-bef-p90`} className="text-right px-2 py-1 font-medium text-neutral-600">p90</th>,
                <th key={`${t}-bef-n`} className="text-right px-2 py-1 font-medium text-neutral-600">N</th>,
                <th key={`${t}-aft-p50`} className="text-right px-2 py-1 font-medium text-green-700">after p50</th>,
                <th key={`${t}-aft-p90`} className="text-right px-2 py-1 font-medium text-green-700">p90</th>,
                <th key={`${t}-aft-n`} className="text-right px-2 py-1 font-medium text-green-700">N</th>,
              ])}
            </tr>
          </thead>
          <tbody className="block divide-y divide-neutral-800/30 md:table-row-group">
            {rows.map(r => (
              <tr key={r.surface} className="block px-3 py-2 md:table-row md:px-0 md:py-0">
                <td className="block pb-1 font-medium text-neutral-200 md:table-cell md:px-3 md:py-2 md:whitespace-nowrap">{r.surface}</td>
                {typeList.flatMap(t => {
                  const split = r.split[t];
                  if (!split) {
                    return [
                      <td key={`${t}-empty`} colSpan={6} className="flex justify-between gap-3 py-1 text-neutral-600 md:table-cell md:px-2 md:py-2 md:text-center">
                        <span className="min-w-0 break-all text-left text-neutral-500 md:hidden">{t} · No samples</span>
                        —
                      </td>,
                    ];
                  }
                  const beforeCells = split.before.count === 0
                    ? [
                        <td key={`${t}-bef-empty`} colSpan={3} className="flex justify-between gap-3 py-1 text-right text-ui-sm text-neutral-600 italic md:table-cell md:px-2 md:py-2 md:text-center">
                          <span className="min-w-0 break-all text-left not-italic text-neutral-500 md:hidden">{t} · Before</span>
                          no baseline — overwritten by 200-sample cap
                        </td>,
                      ]
                    : [
                        <td key={`${t}-bef-p50`} className="flex justify-between gap-3 py-1 text-right tabular-nums text-neutral-500 md:table-cell md:px-2 md:py-2">
                          <span className="min-w-0 break-all text-left md:hidden">{t} · Before p50</span>
                          {fmtMs(split.before.p50)}
                        </td>,
                        <td key={`${t}-bef-p90`} className="flex justify-between gap-3 py-1 text-right tabular-nums text-neutral-600 md:table-cell md:px-2 md:py-2">
                          <span className="min-w-0 break-all text-left text-neutral-500 md:hidden">{t} · Before p90</span>
                          {fmtMs(split.before.p90)}
                        </td>,
                        <td key={`${t}-bef-n`} className="flex justify-between gap-3 py-1 text-right tabular-nums text-neutral-600 md:table-cell md:px-2 md:py-2">
                          <span className="min-w-0 break-all text-left text-neutral-500 md:hidden">{t} · Before N</span>
                          {split.before.count}
                        </td>,
                      ];
                  const afterCells = split.after.count === 0
                    ? [
                        <td key={`${t}-aft-empty`} colSpan={3} className="flex justify-between gap-3 py-1 text-right text-ui-sm text-neutral-600 italic md:table-cell md:px-2 md:py-2 md:text-center">
                          <span className="min-w-0 break-all text-left not-italic text-green-700 md:hidden">{t} · After</span>
                          no post-cutoff data
                        </td>,
                      ]
                    : [
                        <td key={`${t}-aft-p50`} className="flex justify-between gap-3 py-1 text-right tabular-nums text-green-400 md:table-cell md:px-2 md:py-2">
                          <span className="min-w-0 break-all text-left text-green-700 md:hidden">{t} · After p50</span>
                          {fmtMs(split.after.p50)}
                        </td>,
                        <td key={`${t}-aft-p90`} className="flex justify-between gap-3 py-1 text-right tabular-nums text-green-600 md:table-cell md:px-2 md:py-2">
                          <span className="min-w-0 break-all text-left text-green-700 md:hidden">{t} · After p90</span>
                          {fmtMs(split.after.p90)}
                        </td>,
                        <td key={`${t}-aft-n`} className="flex justify-between gap-3 py-1 text-right tabular-nums text-neutral-500 md:table-cell md:px-2 md:py-2">
                          <span className="min-w-0 break-all text-left text-green-700 md:hidden">{t} · After N</span>
                          {split.after.count}
                        </td>,
                      ];
                  return [...beforeCells, ...afterCells];
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
