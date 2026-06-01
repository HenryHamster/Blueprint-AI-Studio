import { useState } from 'react';
import { apiFetch } from '../api';
import { usePoller } from './usePoller';

export interface PipelineEfficiencyWindow {
  setup_p50_secs: number | null;
  setup_p95_secs: number | null;
  setup_sample_n: number;
  warm_reuse_rate: number | null;
  warm_reuse_sample_n: number;
  review_iter_median: number | null;
  review_iter_p95: number | null;
  review_iter_sample_n: number;
  vs_prior_setup_p50_delta: number | null;
  vs_prior_warm_reuse_rate_delta: number | null;
  vs_prior_review_iter_median_delta: number | null;
}

export interface EfficiencySnapshot {
  computed_at: string;
  window_24h: PipelineEfficiencyWindow;
  window_7d: PipelineEfficiencyWindow;
}

const POLL_MS = 60_000;

export function useEfficiencySnapshot(projectId?: string | null): EfficiencySnapshot | null {
  const [data, setData] = useState<EfficiencySnapshot | null>(null);

  usePoller(async (signal) => {
    try {
      const params = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
      const res = await apiFetch(`/api/admin/pipeline-efficiency${params}`);
      if (!signal.aborted && res.ok) setData(await res.json() as EfficiencySnapshot);
    } catch { /* endpoint may not be deployed */ }
  }, { intervalMs: POLL_MS, deps: [projectId] });

  return data;
}

export function fmtEffSecs(s: number | null | undefined): string {
  if (s === null || s === undefined) return '—';
  if (s < 1) return '<1s';
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.round(s / 60)}m`;
}

export function fmtEffRate(r: number | null | undefined): string {
  if (r === null || r === undefined) return '—';
  return `${Math.round(r * 100)}%`;
}

export function fmtEffIter(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—';
  return n.toFixed(1);
}
